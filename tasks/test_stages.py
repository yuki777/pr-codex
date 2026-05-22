#!/usr/bin/env python3
"""Regression tests for F4 stage artifacts and status reporting."""

from __future__ import annotations

import copy
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "tasks"
CANDIDATES_SCHEMA = ROOT / "schemas" / "findings.candidates.v1.json"
FINDINGS_SCHEMA = ROOT / "schemas" / "findings.v1.json"
CANDIDATES_VALIDATOR = TASKS / "validate_candidates.py"
STATUS_VALIDATOR = TASKS / "validate_status.py"
SKILL = ROOT / "skills" / "review" / "SKILL.md"
STAGES_DOC = ROOT / "skills" / "review" / "STAGES.md"
README = ROOT / "README.md"
sys.path.insert(0, str(TASKS))

from validate_candidates import validate_candidates  # noqa: E402
from validate_status import validate_status  # noqa: E402


def valid_metadata() -> dict[str, object]:
    return {
        "org": "yuki777",
        "repository": "pr-codex",
        "repository_full_name": "yuki777/pr-codex",
        "pr_number": 40,
        "pr_url": "https://github.com/yuki777/pr-codex/pull/40",
        "head_sha": "0703bf09c8f2ee19f15df958d04e7a225f5580aa",
        "base_sha": "73130b7b20b8987baa5432242dc4af74b39950",
        "branch": "feat/40",
        "base_branch": "main",
        "merge_commit_sha": None,
        "title": "F4 pipeline",
        "files": ["skills/review/SKILL.md"],
    }


def valid_candidates() -> dict[str, object]:
    return {
        "schema_version": "findings.candidates.v1",
        "producer": {
            "name": "pr-codex",
            "version": "1.6.0",
            "run_id": "yuki777-pr-codex-40-0703bf0",
        },
        "pr": {
            "repository": "yuki777/pr-codex",
            "number": 40,
            "base_sha": "73130b7b20b8987baa5432242dc4af74b39950",
            "head_sha": "0703bf09c8f2ee19f15df958d04e7a225f5580aa",
            "merge_commit_sha": None,
        },
        "generated_at": "2026-05-06T08:00:00Z",
        "candidates": [
            {
                "source_agent": "claude",
                "source_ref": "claude-review.md:Must Fix 1",
                "location": {
                    "path": "skills/review/SKILL.md",
                    "start_line": 590,
                    "side": "RIGHT",
                },
                "severity_raw": "must_fix",
                "category_raw": "bug",
                "title": "Verifier must not be bypassed",
                "problem": "A candidate could be posted without the verifier gate.",
                "reason": "The review workflow requires the verifier to own 4-axis decisions.",
                "suggestion": "Persist the candidate and require verified findings before explainer output.",
            }
        ],
    }


class StageArtifactTests(unittest.TestCase):
    def assert_invalid_candidates(self, artifact: dict[str, object], expected_fragment: str, metadata: dict[str, object] | None = None) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_path = Path(tmp) / "findings.candidates.json"
            data_path.write_text(json.dumps(artifact, ensure_ascii=True), encoding="utf-8")
            command = [sys.executable, str(CANDIDATES_VALIDATOR), "--schema", str(CANDIDATES_SCHEMA), "--data", str(data_path)]
            if metadata is not None:
                metadata_path = Path(tmp) / "metadata.json"
                metadata_path.write_text(json.dumps(metadata, ensure_ascii=True), encoding="utf-8")
                command.extend(["--metadata", str(metadata_path)])
            result = subprocess.run(command, check=False, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("INVALID candidates artifact", result.stderr)
        self.assertIn(expected_fragment, result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def assert_invalid_status(self, status: dict[str, object], expected_fragment: str) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            status_path = Path(tmp) / "status.json"
            status_path.write_text(json.dumps(status, ensure_ascii=True), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(STATUS_VALIDATOR), "--data", str(status_path)],
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("INVALID status", result.stderr)
        self.assertIn(expected_fragment, result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_candidates_minimal_shape_passes_and_keeps_verifier_outputs_optional(self) -> None:
        self.assertEqual(validate_candidates(valid_candidates(), valid_metadata()), [])
        artifact = copy.deepcopy(valid_candidates())
        candidate = artifact["candidates"][0]
        candidate["id"] = "candidate-local-id"
        candidate["fingerprint"] = "different-hunter-fingerprint"
        candidate["axes"] = {"real": "unknown"}
        candidate["evidence_level"] = "suspicion"
        candidate["posting"] = {"post_policy": "inline", "explanation_postable": True}
        self.assertEqual(validate_candidates(artifact, valid_metadata()), [])

    def test_candidates_validate_location_and_metadata_without_crashing(self) -> None:
        artifact = copy.deepcopy(valid_candidates())
        artifact["candidates"][0]["location"]["end_line"] = 1
        self.assert_invalid_candidates(artifact, ".location.end_line: must be >= start_line")

        metadata = copy.deepcopy(valid_metadata())
        metadata["head_sha"] = "a" * 40
        self.assert_invalid_candidates(valid_candidates(), "$.pr.head_sha: must match metadata.head_sha", metadata)

    def test_candidates_cli_rejects_wrong_schema_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_path = Path(tmp) / "findings.candidates.json"
            data_path.write_text(json.dumps(valid_candidates(), ensure_ascii=True), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(CANDIDATES_VALIDATOR), "--schema", str(FINDINGS_SCHEMA), "--data", str(data_path)],
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("invalid candidates schema file", result.stderr)
        self.assertIn("$schema.$id", result.stderr)
        self.assertIn("$schema.properties.schema_version.const", result.stderr)
        self.assertNotIn("VALID candidates artifact", result.stdout)

    def test_candidates_schema_rejects_malformed_dates_and_control_strings(self) -> None:
        schema = json.loads(CANDIDATES_SCHEMA.read_text(encoding="utf-8"))
        generated_at_pattern = schema["properties"]["generated_at"]["pattern"]
        self.assertIsNotNone(re.search(generated_at_pattern, "2026-05-06T08:00:00Z"))
        self.assertIsNotNone(re.search(generated_at_pattern, "2026-05-06T08:00:00.123+09:00"))
        self.assertIsNone(re.search(generated_at_pattern, "not-a-date"))
        self.assertIsNone(re.search(generated_at_pattern, "2026-05-06T08:00:00Z\n"))

        string_def = schema["$defs"]["non_empty_string"]
        non_empty_pattern = string_def["pattern"]
        self.assertEqual(string_def["minLength"], 1)
        self.assertIsNotNone(re.search(non_empty_pattern, "review title"))
        self.assertIsNone(re.search(non_empty_pattern, "review title\n"))
        self.assertIsNone(re.search(non_empty_pattern, "review\u0000title"))

    def test_status_stage_fields_are_backward_compatible_and_validated(self) -> None:
        legacy_running = {"state": "running", "started_at": "2026-05-06T08:00:00Z", "head_sha": "abc1234"}
        self.assertEqual(validate_status(legacy_running), [])

        staged_completed = {
            "state": "completed",
            "started_at": "2026-05-06T08:00:00Z",
            "finished_at": "2026-05-06T08:05:00Z",
            "exit_code": 0,
            "head_sha": "abc1234",
            "stage": "explainer",
            "failed_stage": None,
            "stage_durations_ms": {"ranker": 0, "hunter": 300000, "verifier": 0, "explainer": 0},
        }
        self.assertEqual(validate_status(staged_completed), [])

    def test_status_failed_stage_rules_are_enforced(self) -> None:
        self.assert_invalid_status(
            {"state": "completed", "stage": "hunter", "failed_stage": "verifier"},
            "$.failed_stage: must be null unless state=failed",
        )
        self.assert_invalid_status(
            {"state": "failed", "stage": "verifier", "failed_stage": None},
            "$.failed_stage: must name the failed stage",
        )
        self.assert_invalid_status(
            {"state": "failed", "stage_durations_ms": {"poster": 1}},
            "unexpected stages: poster",
        )

    def test_review_skill_documents_logical_stages_without_touching_hunter_templates(self) -> None:
        text = SKILL.read_text(encoding="utf-8")
        for snippet in (
            "logical stage: ranker",
            "logical stage: hunter",
            "logical stage: verifier",
            "logical stage: explainer",
            "findings.candidates.json",
            'python3 "$plugin_root/tasks/validate_candidates.py"',
            'python3 "$plugin_root/tasks/validate_status.py" --data ~/claude-loop-pr-codex/$org-$repository-$pr_number/status.json',
            "failed_stage",
            "selected_hunters` は ranker 出力の interface",
        ):
            self.assertIn(snippet, text)
        self.assertIn('selected_hunters: ["claude", "codex"]', text)
        self.assertGreaterEqual(text.count('python3 "$plugin_root/tasks/validate_status.py"'), 3)
        self.assertIn("status.json` を `tasks/validate_status.py` で検証", README.read_text(encoding="utf-8"))

    def test_stage_docs_define_roles_inputs_outputs_and_halting(self) -> None:
        text = STAGES_DOC.read_text(encoding="utf-8")
        for snippet in (
            "ranker → hunter → verifier → explainer",
            "findings.candidates.json",
            "findings.verified.json",
            "verifier を迂回して GitHub 投稿しない",
            "F8 hook",
            "F11 hook",
        ):
            self.assertIn(snippet, text)
        readme = README.read_text(encoding="utf-8")
        self.assertIn("ranker / hunter / verifier / explainer", readme)
        self.assertIn("findings.candidates.json", readme)


if __name__ == "__main__":
    unittest.main()
