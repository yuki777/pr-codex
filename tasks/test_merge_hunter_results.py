#!/usr/bin/env python3
"""Regression tests for deterministic structured hunter-result merging."""

from __future__ import annotations

import copy
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from tasks.validate_candidates import validate_candidates

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "tasks"
HUNTER_SCHEMA = ROOT / "schemas" / "hunter-result.v1.json"
CANDIDATES_SCHEMA = ROOT / "schemas" / "findings.candidates.v1.json"
MERGER = TASKS / "merge_hunter_results.py"
CANDIDATES_VALIDATOR = TASKS / "validate_candidates.py"


def valid_metadata(include_merge_commit_sha: bool = True) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "org": "yuki777",
        "repository": "pr-codex",
        "repository_full_name": "yuki777/pr-codex",
        "pr_number": 108,
        "head_sha": "1" * 40,
        "base_sha": "2" * 40,
    }
    if include_merge_commit_sha:
        metadata["merge_commit_sha"] = "3" * 40
    return metadata


def valid_candidate(**overrides: Any) -> dict[str, Any]:
    candidate: dict[str, Any] = {
        "title": "Unsafe value reaches command execution",
        "severity_suggestion": "must_fix",
        "category_suggestion": "security",
        "path": "tasks/example.py",
        "start_line": 10,
        "end_line": None,
        "side": "RIGHT",
        "problem": "User-controlled input is passed to a shell.",
        "reason": "Shell interpolation permits command injection.",
        "suggestion": "Pass arguments as a list without a shell.",
    }
    candidate.update(overrides)
    return candidate


def valid_hunter(status: str = "findings", candidates: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    if candidates is None:
        candidates = [valid_candidate()] if status == "findings" else []
    return {
        "schema_version": "hunter-result.v1",
        "status": status,
        "candidates": candidates,
        "coverage": {
            "high_risk_paths_checked": ["tasks/example.py"],
            "checks_run": ["traced command construction"],
            "limitations": [],
        },
    }


class MergeHunterResultsTests(unittest.TestCase):
    def run_merge(
        self,
        claude: Any,
        codex: Any,
        *,
        metadata: Any | None = None,
        schema: Any | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], bool, dict[str, Any] | None]:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            claude_path = directory / "claude-review.json"
            codex_path = directory / "codex-review.json"
            metadata_path = directory / "metadata.json"
            output_path = directory / "findings.candidates.json.tmp"
            schema_path = HUNTER_SCHEMA

            claude_path.write_text(json.dumps(claude, ensure_ascii=False), encoding="utf-8")
            codex_path.write_text(json.dumps(codex, ensure_ascii=False), encoding="utf-8")
            metadata_path.write_text(
                json.dumps(valid_metadata() if metadata is None else metadata, ensure_ascii=False),
                encoding="utf-8",
            )
            if schema is not None:
                schema_path = directory / "hunter-schema.json"
                schema_path.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    str(MERGER),
                    "--schema",
                    str(schema_path),
                    "--claude",
                    str(claude_path),
                    "--codex",
                    str(codex_path),
                    "--metadata",
                    str(metadata_path),
                    "--producer-version",
                    "1.7.0",
                    "--output",
                    str(output_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            exists = output_path.exists()
            output = json.loads(output_path.read_text(encoding="utf-8")) if exists else None
        return result, exists, output

    def assert_invalid_hunter(self, claude: dict[str, Any], expected_fragment: str) -> None:
        result, exists, output = self.run_merge(claude, valid_hunter())
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertFalse(exists)
        self.assertIsNone(output)
        self.assertIn("INVALID hunter result", result.stderr)
        self.assertIn(expected_fragment, result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_merges_findings_in_agent_order_and_passes_candidates_validator(self) -> None:
        claude_candidate = valid_candidate()
        codex_candidate = valid_candidate(
            title="Range problem",
            severity_suggestion="should_fix",
            category_suggestion="correctness",
            path="tasks/range.py",
            start_line=21,
            end_line=24,
            side="LEFT",
            problem="The range is skipped.",
            reason="The loop ends one item too soon.",
            suggestion="Include the final item.",
        )
        metadata = valid_metadata()
        result, exists, output = self.run_merge(
            valid_hunter(candidates=[claude_candidate]),
            valid_hunter(candidates=[codex_candidate]),
            metadata=metadata,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(exists)
        self.assertIsNotNone(output)
        assert output is not None
        self.assertEqual(
            result.stdout,
            "merged 2 candidates (claude=1 status=findings, codex=1 status=findings)\n",
        )
        self.assertEqual(output["schema_version"], "findings.candidates.v1")
        self.assertEqual(
            output["producer"],
            {
                "name": "pr-codex",
                "version": "1.7.0",
                "run_id": f"yuki777-pr-codex-108-{'1' * 40}",
            },
        )
        self.assertEqual(
            output["pr"],
            {
                "repository": "yuki777/pr-codex",
                "number": 108,
                "base_sha": "2" * 40,
                "head_sha": "1" * 40,
                "merge_commit_sha": "3" * 40,
            },
        )
        self.assertRegex(output["generated_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")
        self.assertEqual(
            output["candidates"],
            [
                {
                    "candidate_id": "claude-001",
                    "source_agent": "claude",
                    "source_ref": "claude-review.json#candidates[0]",
                    "location": {"path": "tasks/example.py", "start_line": 10, "side": "RIGHT"},
                    "severity_raw": "must_fix",
                    "category_raw": "security",
                    "title": "Unsafe value reaches command execution",
                    "problem": "User-controlled input is passed to a shell.",
                    "reason": "Shell interpolation permits command injection.",
                    "suggestion": "Pass arguments as a list without a shell.",
                },
                {
                    "candidate_id": "codex-001",
                    "source_agent": "codex",
                    "source_ref": "codex-review.json#candidates[0]",
                    "location": {
                        "path": "tasks/range.py",
                        "start_line": 21,
                        "end_line": 24,
                        "side": "LEFT",
                    },
                    "severity_raw": "should_fix",
                    "category_raw": "correctness",
                    "title": "Range problem",
                    "problem": "The range is skipped.",
                    "reason": "The loop ends one item too soon.",
                    "suggestion": "Include the final item.",
                },
            ],
        )
        self.assertEqual(validate_candidates(output, metadata), [])

        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "findings.candidates.json.tmp"
            metadata_path = Path(tmp) / "metadata.json"
            output_path.write_text(json.dumps(output, ensure_ascii=False), encoding="utf-8")
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            validation = subprocess.run(
                [
                    sys.executable,
                    str(CANDIDATES_VALIDATOR),
                    "--schema",
                    str(CANDIDATES_SCHEMA),
                    "--data",
                    str(output_path),
                    "--metadata",
                    str(metadata_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual(validation.returncode, 0, validation.stderr)
        self.assertEqual(validation.stdout, "VALID candidates artifact\n")

    def test_clean_side_contributes_no_candidates(self) -> None:
        codex = valid_hunter(candidates=[valid_candidate(title="Codex only")])
        result, exists, output = self.run_merge(valid_hunter("clean"), codex)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(exists)
        assert output is not None
        self.assertEqual([candidate["title"] for candidate in output["candidates"]], ["Codex only"])
        self.assertEqual(output["candidates"][0]["candidate_id"], "codex-001")

    def test_both_clean_produces_valid_empty_candidates_array(self) -> None:
        metadata = valid_metadata(include_merge_commit_sha=False)
        result, exists, output = self.run_merge(valid_hunter("clean"), valid_hunter("clean"), metadata=metadata)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(exists)
        assert output is not None
        self.assertEqual(output["candidates"], [])
        self.assertNotIn("merge_commit_sha", output["pr"])
        self.assertEqual(validate_candidates(output, metadata), [])

    def test_diff_unavailable_exits_three_without_writing_output(self) -> None:
        cases = [
            ("diff_unavailable", "clean"),
            ("clean", "diff_unavailable"),
            ("diff_unavailable", "diff_unavailable"),
        ]
        for claude_status, codex_status in cases:
            with self.subTest(claude=claude_status, codex=codex_status):
                result, exists, output = self.run_merge(valid_hunter(claude_status), valid_hunter(codex_status))
                self.assertEqual(result.returncode, 3, result.stdout)
                self.assertFalse(exists)
                self.assertIsNone(output)
                self.assertIn(
                    f"HUNTER_DIFF_UNAVAILABLE: claude={claude_status} codex={codex_status}",
                    result.stderr,
                )

    def test_rejects_invalid_hunter_shapes_and_values(self) -> None:
        cases: list[tuple[str, dict[str, Any], str]] = []

        extra = valid_hunter()
        extra["unexpected"] = True
        cases.append(("extra top-level key", extra, "unexpected properties"))

        start_zero = valid_hunter()
        start_zero["candidates"][0]["start_line"] = 0
        cases.append(("start line zero", start_zero, "start_line"))

        reversed_range = valid_hunter()
        reversed_range["candidates"][0]["end_line"] = 9
        cases.append(("reversed range", reversed_range, "must be >= start_line"))

        clean_with_candidate = valid_hunter("clean", [valid_candidate()])
        cases.append(("clean with candidate", clean_with_candidate, "must be empty when status is 'clean'"))

        findings_without_candidate = valid_hunter("findings", [])
        cases.append(("findings without candidate", findings_without_candidate, "must contain at least one item"))

        invalid_severity = valid_hunter()
        invalid_severity["candidates"][0]["severity_suggestion"] = "critical"
        cases.append(("invalid severity", invalid_severity, "severity_suggestion"))

        non_string_severity = valid_hunter()
        non_string_severity["candidates"][0]["severity_suggestion"] = ["must_fix"]
        cases.append(("non-string severity", non_string_severity, "severity_suggestion"))

        non_string_status = valid_hunter()
        non_string_status["status"] = ["findings"]
        cases.append(("non-string status", non_string_status, "$.status"))

        control_title = valid_hunter()
        control_title["candidates"][0]["title"] = "unsafe\u0000title"
        cases.append(("control character", control_title, ".title"))

        missing_coverage_key = valid_hunter()
        del missing_coverage_key["coverage"]["checks_run"]
        cases.append(("coverage key missing", missing_coverage_key, "missing required properties: checks_run"))

        for name, artifact, expected in cases:
            with self.subTest(name=name):
                self.assert_invalid_hunter(artifact, expected)

    def test_reports_all_runtime_validation_errors(self) -> None:
        claude = valid_hunter()
        claude["candidates"][0]["start_line"] = 0
        codex = valid_hunter()
        codex["coverage"]["checks_run"] = [""]
        result, exists, _ = self.run_merge(claude, codex)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(exists)
        self.assertIn("claude", result.stderr)
        self.assertIn("start_line", result.stderr)
        self.assertIn("codex", result.stderr)
        self.assertIn("checks_run[0]", result.stderr)

    def test_rejects_missing_metadata_field(self) -> None:
        metadata = valid_metadata()
        del metadata["repository_full_name"]
        result, exists, output = self.run_merge(valid_hunter(), valid_hunter(), metadata=metadata)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertFalse(exists)
        self.assertIsNone(output)
        self.assertIn("INVALID metadata", result.stderr)
        self.assertIn("repository_full_name", result.stderr)

    def test_rejects_wrong_hunter_schema_file_with_exit_two(self) -> None:
        wrong_schema = json.loads(CANDIDATES_SCHEMA.read_text(encoding="utf-8"))
        result, exists, output = self.run_merge(valid_hunter(), valid_hunter(), schema=wrong_schema)
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertFalse(exists)
        self.assertIsNone(output)
        self.assertIn("invalid hunter schema file", result.stderr)
        self.assertIn("$schema.$id", result.stderr)
        self.assertIn("schema_version.enum", result.stderr)

    def test_output_is_utf8_indented_json_with_trailing_newline(self) -> None:
        claude = valid_hunter(candidates=[valid_candidate(title="日本語の指摘")])
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            claude_path = directory / "claude-review.json"
            codex_path = directory / "codex-review.json"
            metadata_path = directory / "metadata.json"
            output_path = directory / "findings.candidates.json.tmp"
            claude_path.write_text(json.dumps(claude, ensure_ascii=False), encoding="utf-8")
            codex_path.write_text(json.dumps(valid_hunter("clean")), encoding="utf-8")
            metadata_path.write_text(json.dumps(valid_metadata()), encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(MERGER),
                    "--schema",
                    str(HUNTER_SCHEMA),
                    "--claude",
                    str(claude_path),
                    "--codex",
                    str(codex_path),
                    "--metadata",
                    str(metadata_path),
                    "--producer-version",
                    "1.7.0",
                    "--output",
                    str(output_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            text = output_path.read_text(encoding="utf-8") if output_path.exists() else ""
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('\n  "schema_version"', text)
        self.assertIn("日本語の指摘", text)
        self.assertNotIn("\\u65e5", text)
        self.assertTrue(text.endswith("\n"))
        self.assertIsNotNone(re.search(r'"generated_at": "[^\n]+\+00:00"', text))


if __name__ == "__main__":
    unittest.main()
