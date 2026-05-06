#!/usr/bin/env python3
"""Regression tests for preflight-result extraction and validation."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "tasks"
SCHEMA_PATH = ROOT / "schemas" / "preflight-result.v1.json"
VALIDATOR_PATH = TASKS / "validate_preflight_result.py"
sys.path.insert(0, str(TASKS))

from validate_preflight_result import (  # noqa: E402
    emit_markdown,
    expected_counts,
    extract_result_json,
    validate_preflight_result,
)


def valid_result() -> dict[str, object]:
    return {
        "schema_version": "preflight-result.v1",
        "verdict": "PASS",
        "stages": {
            "schema_validation": {"status": "PASS", "note": "schema and context validated"},
            "range_validation": {"status": "PASS", "note": "all inline comments are in diff hunks"},
            "semantic_preflight": {"status": "PASS", "note": "no counterargument found"},
            "payload_consistency": {"status": "PASS", "note": "payload matches review artifacts"},
        },
        "violations": [],
        "auto_fixable_count": 0,
        "requires_human_count": 0,
        "generated_at": "2026-05-06T00:00:00Z",
    }


def auto_fixable_range_violation() -> dict[str, object]:
    return {
        "stage": "range_validation",
        "rule": "line_out_of_hunk",
        "comment_index": 0,
        "detail": "comment line is outside pr.diff.ranges.txt and can be moved to body",
        "severity": "error",
        "auto_fixable": True,
        "requires_review_regeneration": False,
    }


def human_semantic_violation() -> dict[str, object]:
    return {
        "stage": "semantic_preflight",
        "rule": "counterargument_succeeded",
        "finding_id": "f" * 64,
        "detail": "A plausible counterargument exists based on the PR diff.",
        "severity": "error",
        "auto_fixable": False,
        "requires_review_regeneration": True,
    }


class ValidatePreflightResultTest(unittest.TestCase):
    def assert_invalid_without_crash(self, result: dict[str, object], expected_fragment: str) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_path = Path(tmp) / "preflight-result.json"
            data_path.write_text(json.dumps(result, ensure_ascii=True), encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, str(VALIDATOR_PATH), "--schema", str(SCHEMA_PATH), "--data", str(data_path)],
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        self.assertIn("INVALID preflight result", completed.stderr)
        self.assertIn(expected_fragment, completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)

    def test_valid_pass_result(self) -> None:
        self.assertEqual(validate_preflight_result(valid_result()), [])

    def test_skipped_stage_is_invalid_for_final_preflight_result(self) -> None:
        result = valid_result()
        result["stages"]["schema_validation"]["status"] = "SKIPPED"
        self.assert_invalid_without_crash(result, "$.stages.schema_validation.status: must be PASS or FAIL")

    def test_warning_does_not_create_intermediate_verdict(self) -> None:
        result = valid_result()
        result["violations"] = [
            {
                "stage": "semantic_preflight",
                "rule": "cluster_representative_missing_until_f6",
                "finding_id": "abc123",
                "detail": "F6 cluster metadata is not present yet; record as warning only.",
                "severity": "warning",
                "auto_fixable": False,
                "requires_review_regeneration": False,
            }
        ]
        self.assertEqual(validate_preflight_result(result), [])
        result["verdict"] = "PASS_WITH_WARNINGS"
        self.assert_invalid_without_crash(result, "$.verdict: must be PASS or FAIL")

    def test_error_violation_requires_fail_and_counts(self) -> None:
        result = valid_result()
        result["verdict"] = "FAIL"
        result["stages"]["range_validation"]["status"] = "FAIL"
        result["stages"]["semantic_preflight"]["status"] = "FAIL"
        result["violations"] = [auto_fixable_range_violation(), human_semantic_violation()]
        result["auto_fixable_count"] = 1
        result["requires_human_count"] = 1
        self.assertEqual(validate_preflight_result(result), [])

    def test_count_mismatch_is_invalid(self) -> None:
        result = valid_result()
        result["verdict"] = "FAIL"
        result["stages"]["range_validation"]["status"] = "FAIL"
        result["violations"] = [auto_fixable_range_violation()]
        result["auto_fixable_count"] = 0
        self.assert_invalid_without_crash(result, "$.auto_fixable_count")

    def test_all_four_stages_are_required_once(self) -> None:
        result = valid_result()
        del result["stages"]["payload_consistency"]
        self.assert_invalid_without_crash(result, "missing stages: payload_consistency")

    def test_unknown_stage_is_invalid(self) -> None:
        result = valid_result()
        result["stages"]["schema"] = {"status": "PASS"}
        self.assert_invalid_without_crash(result, "$.stages: unexpected properties: schema")

    def test_fail_stage_requires_error_violation(self) -> None:
        result = valid_result()
        result["stages"]["semantic_preflight"]["status"] = "FAIL"
        self.assert_invalid_without_crash(result, "FAIL requires at least one error violation")

    def test_extract_result_json_uses_last_result_json_block(self) -> None:
        first = copy.deepcopy(valid_result())
        second = copy.deepcopy(valid_result())
        second["verdict"] = "FAIL"
        second["stages"]["semantic_preflight"]["status"] = "FAIL"
        second["violations"] = [human_semantic_violation()]
        second["requires_human_count"] = 1
        markdown = (
            "### RESULT_JSON\n"
            "```json\n"
            + json.dumps(first)
            + "\n```\n"
            "some explanation\n"
            "### RESULT_JSON\n"
            "```json\n"
            + json.dumps(second)
            + "\n```\n"
            "VERDICT: FAIL\n"
        )
        self.assertEqual(extract_result_json(markdown), second)


    def test_extract_result_json_rejects_dangling_final_result_heading(self) -> None:
        markdown = (
            "### RESULT_JSON\n```json\n"
            + json.dumps(valid_result())
            + "\n```\n"
            "### RESULT_JSON\n"
            "VERDICT: FAIL\n"
        )
        with self.assertRaisesRegex(ValueError, "after final RESULT_JSON heading"):
            extract_result_json(markdown)

    def test_extract_result_json_requires_matching_final_verdict(self) -> None:
        markdown = (
            "### RESULT_JSON\n```json\n"
            + json.dumps(valid_result())
            + "\n```\n"
            "VERDICT: FAIL\n"
        )
        with self.assertRaisesRegex(ValueError, "verdict must match final VERDICT"):
            extract_result_json(markdown)

    def test_extract_result_json_requires_final_verdict_as_last_line(self) -> None:
        markdown = (
            "### RESULT_JSON\n```json\n"
            + json.dumps(valid_result())
            + "\n```\n"
            "VERDICT: PASS\n"
            "trailing text\n"
        )
        with self.assertRaisesRegex(ValueError, "final VERDICT line"):
            extract_result_json(markdown)

    def test_cli_extracts_and_validates_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            md_path = Path(tmp) / "preflight-codex.md"
            md_path.write_text(
                "### RESULT_JSON\n```json\n"
                + json.dumps(valid_result(), ensure_ascii=True)
                + "\n```\nVERDICT: PASS\n",
                encoding="utf-8",
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    str(VALIDATOR_PATH),
                    "--schema",
                    str(SCHEMA_PATH),
                    "--from-markdown",
                    str(md_path),
                    "--emit-json",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["verdict"], "PASS")


    def test_cli_rejects_markdown_verdict_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            md_path = Path(tmp) / "preflight-codex.md"
            md_path.write_text(
                "### RESULT_JSON\n```json\n"
                + json.dumps(valid_result(), ensure_ascii=True)
                + "\n```\nVERDICT: FAIL\n",
                encoding="utf-8",
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    str(VALIDATOR_PATH),
                    "--schema",
                    str(SCHEMA_PATH),
                    "--from-markdown",
                    str(md_path),
                    "--emit-json",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("verdict must match final VERDICT", completed.stderr)

    def test_auto_fixable_classification_counts_only_error_violations(self) -> None:
        errors = [auto_fixable_range_violation(), human_semantic_violation()]
        self.assertEqual(expected_counts(errors), (1, 1))
        warning = copy.deepcopy(auto_fixable_range_violation())
        warning["severity"] = "warning"
        result = valid_result()
        result["violations"] = [warning]
        self.assertEqual(validate_preflight_result(result), [])


    def test_known_error_rule_classification_is_enforced(self) -> None:
        result = valid_result()
        result["verdict"] = "FAIL"
        result["stages"]["range_validation"]["status"] = "FAIL"
        violation = auto_fixable_range_violation()
        violation["auto_fixable"] = False
        result["violations"] = [violation]
        self.assert_invalid_without_crash(result, "rule line_out_of_hunk must use auto_fixable=true")

    def test_unknown_error_rule_is_invalid_but_warning_rule_is_allowed(self) -> None:
        result = valid_result()
        result["verdict"] = "FAIL"
        result["stages"]["semantic_preflight"]["status"] = "FAIL"
        result["violations"] = [
            {
                "stage": "semantic_preflight",
                "rule": "new_error_rule",
                "detail": "unknown error rule",
                "severity": "error",
                "auto_fixable": False,
                "requires_review_regeneration": True,
            }
        ]
        result["requires_human_count"] = 1
        self.assert_invalid_without_crash(result, "unknown error rule new_error_rule")

    def test_emit_markdown_preserves_legacy_verdict_line(self) -> None:
        markdown = emit_markdown(valid_result())
        self.assertIn("## Stage results", markdown)
        self.assertTrue(markdown.rstrip().endswith("VERDICT: PASS"))


    def test_send_skill_documents_four_stage_pipeline_and_counterargument_polarity(self) -> None:
        skill = (ROOT / "skills" / "send" / "SKILL.md").read_text(encoding="utf-8")
        for snippet in (
            "## STAGE 1: schema_validation",
            "## STAGE 2: range_validation",
            "## STAGE 3: semantic_preflight",
            "## STAGE 4: payload_consistency",
            "counterargument_succeeded",
            "反証成功 = 不採用 / FAIL",
            "preflight-result.json",
            "preflight-prompt.md",
            "Markdown fallback は使わない",
            "shell で prompt 本文を展開してはならない",
            "<  ~/claude-loop-pr-codex/$dir_name/preflight-prompt.md",
            "final `VERDICT:` line",
            "一致しなければ",
        ):
            self.assertIn(snippet, skill)
        self.assertIn('top-level `verdict` は `PASS` / `FAIL` のみ', skill)
        unsafe_shell_prompt_prefix = "--cd ~/claude-loop-pr-codex/$dir_name " + chr(92) + '\n  "'
        self.assertNotIn(unsafe_shell_prompt_prefix, skill)

    def test_result_is_not_mutated_by_validation(self) -> None:
        result = valid_result()
        before = copy.deepcopy(result)
        validate_preflight_result(result)
        self.assertEqual(result, before)


if __name__ == "__main__":
    unittest.main()
