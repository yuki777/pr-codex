#!/usr/bin/env python3
"""Regression tests for direct preflight-result JSON validation."""

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
SEMANTIC_SCHEMA_PATH = ROOT / "schemas" / "preflight-semantic.v1.json"
VALIDATOR_PATH = TASKS / "validate_preflight_result.py"
sys.path.insert(0, str(TASKS))

from validate_preflight_result import emit_markdown, expected_counts, validate_preflight_result  # noqa: E402


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
        "finding_id": None,
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
        "comment_index": None,
        "detail": "A plausible counterargument exists based on the PR diff.",
        "severity": "error",
        "auto_fixable": False,
        "requires_review_regeneration": True,
    }


def sarif_count_mismatch_violation() -> dict[str, object]:
    return {
        "stage": "schema_validation",
        "rule": "must_fix_count_mismatch",
        "finding_id": None,
        "comment_index": None,
        "detail": "canonical=2 markdown=2 payload=1 sarif=2",
        "severity": "error",
        "auto_fixable": False,
        "requires_review_regeneration": True,
    }

def payload_manifest(
    *,
    inline_must_fix: bool = True,
    out_of_range_must_fix: bool = False,
) -> dict[str, object]:
    comment_map = [
        {
            "comment_index": 3,
            "finding_id": "inline-must-fix",
            "severity": "must_fix" if inline_must_fix else "should_fix",
        }
    ]
    out_of_range = []
    if out_of_range_must_fix:
        out_of_range.append(
            {
                "finding_id": "out-of-range-must-fix",
                "kind": "Must Fix (outside diff)",
                "reason": "No changed line can host the comment.",
            }
        )
    return {
        "schema_version": "payload-manifest.v1",
        "comment_map": comment_map,
        "out_of_range": out_of_range,
    }


def semantic_result(*decisions: dict[str, str]) -> dict[str, object]:
    return {
        "schema_version": "preflight-semantic.v1",
        "decisions": list(decisions),
    }


def semantic_decision(
    finding_id: str,
    decision: str,
    counterargument: str = "The strongest counterargument does not change the result.",
    note: str = "",
) -> dict[str, str]:
    return {
        "finding_id": finding_id,
        "decision": decision,
        "counterargument": counterargument,
        "note": note,
    }



class ValidatePreflightResultTest(unittest.TestCase):
    def run_semantic_composition(
        self,
        semantic: dict[str, object] | None,
        manifest: dict[str, object],
        *,
        skipped: bool = False,
        emit: str = "--emit-json",
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "payload-manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            command = [
                sys.executable,
                str(VALIDATOR_PATH),
                "--schema",
                str(SCHEMA_PATH),
            ]
            if skipped:
                command.append("--semantic-skipped")
            else:
                semantic_path = Path(tmp) / "preflight-semantic.json"
                semantic_path.write_text(json.dumps(semantic), encoding="utf-8")
                command.extend(["--from-semantic", str(semantic_path)])
            command.extend(["--manifest", str(manifest_path)])
            if emit:
                command.append(emit)
            return subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
            )

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
                "comment_index": None,
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

    def test_sarif_must_fix_count_mismatch_is_schema_validation_fail(self) -> None:
        result = valid_result()
        result["verdict"] = "FAIL"
        result["stages"]["schema_validation"]["status"] = "FAIL"
        result["violations"] = [sarif_count_mismatch_violation()]
        result["requires_human_count"] = 1
        self.assertEqual(validate_preflight_result(result), [])

    def test_sarif_count_mismatch_cannot_be_auto_fixed(self) -> None:
        result = valid_result()
        result["verdict"] = "FAIL"
        result["stages"]["schema_validation"]["status"] = "FAIL"
        violation = sarif_count_mismatch_violation()
        violation["auto_fixable"] = True
        result["violations"] = [violation]
        result["auto_fixable_count"] = 1
        self.assert_invalid_without_crash(result, "rule must_fix_count_mismatch must use auto_fixable=false")

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

    def test_stage_note_is_required(self) -> None:
        result = valid_result()
        del result["stages"]["schema_validation"]["note"]
        self.assert_invalid_without_crash(
            result,
            "$.stages.schema_validation: missing required properties: note",
        )

    def test_stage_note_accepts_empty_string(self) -> None:
        result = valid_result()
        result["stages"]["schema_validation"]["note"] = ""
        self.assertEqual(validate_preflight_result(result), [])

    def test_stage_note_rejects_control_characters(self) -> None:
        result = valid_result()
        result["stages"]["schema_validation"]["note"] = "invalid\nnote"
        self.assertIn(
            "$.stages.schema_validation.note: must be a string without control characters",
            validate_preflight_result(result),
        )

    def test_violation_references_are_required(self) -> None:
        for missing_field in ("finding_id", "comment_index"):
            with self.subTest(missing_field=missing_field):
                result = valid_result()
                result["verdict"] = "FAIL"
                result["stages"]["range_validation"]["status"] = "FAIL"
                violation = auto_fixable_range_violation()
                del violation[missing_field]
                result["violations"] = [violation]
                result["auto_fixable_count"] = 1
                self.assert_invalid_without_crash(
                    result,
                    f"missing required properties: {missing_field}",
                )

    def test_nullable_violation_references_are_valid(self) -> None:
        result = valid_result()
        result["verdict"] = "FAIL"
        result["stages"]["range_validation"]["status"] = "FAIL"
        violation = auto_fixable_range_violation()
        violation["comment_index"] = None
        result["violations"] = [violation]
        result["auto_fixable_count"] = 1
        self.assertEqual(validate_preflight_result(result), [])

    def test_non_null_violation_references_are_validated(self) -> None:
        result = valid_result()
        result["verdict"] = "FAIL"
        result["stages"]["range_validation"]["status"] = "FAIL"
        violation = auto_fixable_range_violation()
        violation["finding_id"] = ""
        violation["comment_index"] = True
        result["violations"] = [violation]
        result["auto_fixable_count"] = 1
        errors = validate_preflight_result(result)
        self.assertIn(
            "$.violations[0].finding_id: must be null or a non-empty string without control characters",
            errors,
        )
        self.assertIn(
            "$.violations[0].comment_index: must be null or a non-negative integer",
            errors,
        )

    def test_cli_validates_data_directly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_path = Path(tmp) / "preflight-result.json"
            data_path.write_text(json.dumps(valid_result()), encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(VALIDATOR_PATH),
                    "--schema",
                    str(SCHEMA_PATH),
                    "--data",
                    str(data_path),
                    "--emit-json",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), valid_result())

    def test_cli_rejects_invalid_direct_data(self) -> None:
        result = valid_result()
        result["schema_version"] = "wrong"
        with tempfile.TemporaryDirectory() as tmp:
            data_path = Path(tmp) / "preflight-result.json"
            data_path.write_text(json.dumps(result), encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(VALIDATOR_PATH),
                    "--schema",
                    str(SCHEMA_PATH),
                    "--data",
                    str(data_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual(completed.returncode, 1)
        self.assertIn("INVALID preflight result", completed.stderr)
        self.assertIn("$.schema_version: must be preflight-result.v1", completed.stderr)

    def test_cli_emit_markdown_uses_validated_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_path = Path(tmp) / "preflight-result.json"
            data_path.write_text(json.dumps(valid_result()), encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(VALIDATOR_PATH),
                    "--schema",
                    str(SCHEMA_PATH),
                    "--data",
                    str(data_path),
                    "--emit-markdown",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("## Stage results", completed.stdout)
        self.assertTrue(completed.stdout.rstrip().endswith("VERDICT: PASS"))

    def test_cli_rejects_wrong_schema_wiring(self) -> None:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        wrong_schemas = {
            "id": {
                **schema,
                "$id": "https://example.invalid/preflight-result.v1.json",
            },
            "version": {
                **schema,
                "properties": {
                    **schema["properties"],
                    "schema_version": {"type": "string", "enum": ["wrong"]},
                },
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            data_path = Path(tmp) / "preflight-result.json"
            data_path.write_text(json.dumps(valid_result()), encoding="utf-8")
            for name, wrong_schema in wrong_schemas.items():
                with self.subTest(name=name):
                    schema_path = Path(tmp) / f"{name}.json"
                    schema_path.write_text(json.dumps(wrong_schema), encoding="utf-8")
                    completed = subprocess.run(
                        [
                            sys.executable,
                            str(VALIDATOR_PATH),
                            "--schema",
                            str(schema_path),
                            "--data",
                            str(data_path),
                        ],
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(completed.returncode, 2)
                    self.assertIn("invalid preflight-result schema file", completed.stderr)

    def test_cli_rejects_removed_from_markdown_option(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(VALIDATOR_PATH),
                "--schema",
                str(SCHEMA_PATH),
                "--data",
                str(SCHEMA_PATH),
                "--from-markdown",
                str(SCHEMA_PATH),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("unrecognized arguments: --from-markdown", completed.stderr)


    def test_auto_fixable_classification_counts_only_error_violations(self) -> None:
        errors = [auto_fixable_range_violation(), human_semantic_violation()]
        self.assertEqual(expected_counts(errors), (1, 1))
        warning = {
            "stage": "semantic_preflight",
            "rule": "cluster_representative_missing_until_f6",
            "finding_id": "abc123",
            "comment_index": None,
            "detail": "F6 cluster metadata is not present yet; record as warning only.",
            "severity": "warning",
            "auto_fixable": False,
            "requires_review_regeneration": False,
        }
        result = valid_result()
        result["violations"] = [warning]
        self.assertEqual(validate_preflight_result(result), [])

    def test_known_rule_cannot_be_downgraded_to_warning(self) -> None:
        result = valid_result()
        result["violations"] = [human_semantic_violation()]
        result["violations"][0]["severity"] = "warning"
        self.assert_invalid_without_crash(result, "known rule counterargument_succeeded must use severity=error")

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
                "finding_id": None,
                "comment_index": None,
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

    def test_emit_markdown_omits_null_violation_references(self) -> None:
        result = valid_result()
        result["verdict"] = "FAIL"
        result["stages"]["range_validation"] = {"status": "FAIL", "note": ""}
        result["violations"] = [auto_fixable_range_violation()]
        result["violations"][0]["comment_index"] = None
        result["auto_fixable_count"] = 1
        markdown = emit_markdown(result)
        self.assertNotIn("finding_id=", markdown)
        self.assertNotIn("comment_index=", markdown)
        self.assertIn("- range_validation: FAIL\n", markdown)
        self.assertTrue(markdown.rstrip().endswith("VERDICT: FAIL"))

    def test_preflight_semantic_schema_is_strict_structured_output_compatible(self) -> None:
        schema = json.loads(SEMANTIC_SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["schema_version"]["enum"], ["preflight-semantic.v1"])
        self.assertEqual(schema["required"], list(schema["properties"]))
        decision_schema = schema["properties"]["decisions"]["items"]
        self.assertFalse(decision_schema["additionalProperties"])
        self.assertEqual(decision_schema["required"], list(decision_schema["properties"]))

        def assert_no_restricted_keywords(node: object) -> None:
            if isinstance(node, dict):
                for keyword in ("minimum", "format", "minLength"):
                    self.assertNotIn(keyword, node)
                for value in node.values():
                    assert_no_restricted_keywords(value)
            elif isinstance(node, list):
                for value in node:
                    assert_no_restricted_keywords(value)

        assert_no_restricted_keywords(schema)

    def test_from_semantic_all_confirmed_composes_pass(self) -> None:
        manifest = payload_manifest(inline_must_fix=True, out_of_range_must_fix=True)
        semantic = semantic_result(
            semantic_decision("inline-must-fix", "confirmed"),
            semantic_decision("out-of-range-must-fix", "confirmed"),
        )
        completed = self.run_semantic_composition(semantic, manifest)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["verdict"], "PASS")
        self.assertEqual(result["violations"], [])
        self.assertEqual(result["auto_fixable_count"], 0)
        self.assertEqual(result["requires_human_count"], 0)
        self.assertEqual(
            result["stages"]["semantic_preflight"],
            {
                "status": "PASS",
                "note": "decisions: 2 confirmed / 0 refuted / 0 insufficient_evidence",
            },
        )
        for stage in ("schema_validation", "range_validation", "payload_consistency"):
            self.assertEqual(
                result["stages"][stage],
                {
                    "status": "PASS",
                    "note": "validated by deterministic host-side validators",
                },
            )

    def test_from_semantic_refuted_composes_fail_with_counterargument(self) -> None:
        counterargument = "The diff already guards the allegedly unsafe path."
        completed = self.run_semantic_composition(
            semantic_result(
                semantic_decision(
                    "inline-must-fix",
                    "refuted",
                    counterargument=counterargument,
                )
            ),
            payload_manifest(),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["verdict"], "FAIL")
        self.assertEqual(result["auto_fixable_count"], 0)
        self.assertEqual(result["requires_human_count"], 1)
        self.assertEqual(
            result["violations"],
            [
                {
                    "stage": "semantic_preflight",
                    "rule": "counterargument_succeeded",
                    "finding_id": "inline-must-fix",
                    "comment_index": 3,
                    "detail": counterargument,
                    "severity": "error",
                    "auto_fixable": False,
                    "requires_review_regeneration": True,
                }
            ],
        )
        self.assertEqual(result["stages"]["semantic_preflight"]["status"], "FAIL")

    def test_from_semantic_insufficient_evidence_composes_new_rule(self) -> None:
        counterargument = "The available diff does not establish whether the path is reachable."
        completed = self.run_semantic_composition(
            semantic_result(
                semantic_decision(
                    "out-of-range-must-fix",
                    "insufficient_evidence",
                    counterargument=counterargument,
                )
            ),
            payload_manifest(inline_must_fix=False, out_of_range_must_fix=True),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["verdict"], "FAIL")
        self.assertEqual(result["requires_human_count"], 1)
        self.assertEqual(result["violations"][0]["rule"], "insufficient_evidence")
        self.assertIsNone(result["violations"][0]["comment_index"])

    def test_from_semantic_rejects_finding_id_set_mismatch_and_duplicates(self) -> None:
        cases = {
            "missing": semantic_result(),
            "extra": semantic_result(
                semantic_decision("inline-must-fix", "confirmed"),
                semantic_decision("extra", "confirmed"),
            ),
            "duplicate": semantic_result(
                semantic_decision("inline-must-fix", "confirmed"),
                semantic_decision("inline-must-fix", "confirmed"),
            ),
        }
        for name, semantic in cases.items():
            with self.subTest(name=name):
                completed = self.run_semantic_composition(semantic, payload_manifest())
                self.assertEqual(completed.returncode, 1)
                self.assertIn("INVALID preflight result", completed.stderr)
                self.assertIn("finding_id", completed.stderr)

    def test_semantic_skipped_composes_pass_when_no_must_fix_exists(self) -> None:
        completed = self.run_semantic_composition(
            None,
            payload_manifest(inline_must_fix=False),
            skipped=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["verdict"], "PASS")
        self.assertEqual(
            result["stages"]["semantic_preflight"],
            {
                "status": "PASS",
                "note": "skipped: no must_fix findings in payload",
            },
        )

    def test_semantic_skipped_rejects_manifest_with_must_fix(self) -> None:
        completed = self.run_semantic_composition(None, payload_manifest(), skipped=True)
        self.assertEqual(completed.returncode, 1)
        self.assertIn("INVALID preflight result", completed.stderr)
        self.assertIn("must_fix", completed.stderr)

    def test_data_and_semantic_modes_are_mutually_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_path = Path(tmp) / "preflight-result.json"
            semantic_path = Path(tmp) / "preflight-semantic.json"
            manifest_path = Path(tmp) / "payload-manifest.json"
            data_path.write_text(json.dumps(valid_result()), encoding="utf-8")
            semantic_path.write_text(
                json.dumps(semantic_result(semantic_decision("inline-must-fix", "confirmed"))),
                encoding="utf-8",
            )
            manifest_path.write_text(json.dumps(payload_manifest()), encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(VALIDATOR_PATH),
                    "--schema",
                    str(SCHEMA_PATH),
                    "--data",
                    str(data_path),
                    "--from-semantic",
                    str(semantic_path),
                    "--manifest",
                    str(manifest_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("not allowed with argument", completed.stderr)

    def test_semantic_modes_require_manifest_pair(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(VALIDATOR_PATH),
                "--schema",
                str(SCHEMA_PATH),
                "--semantic-skipped",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("--manifest", completed.stderr)


    def test_send_skill_documents_hybrid_pipeline_and_counterargument_polarity(self) -> None:
        skill = (ROOT / "skills" / "send" / "SKILL.md").read_text(encoding="utf-8")
        for snippet in (
            "static Python + Codex semantic",
            "#### 4 stage と担当",
            "`schema_validation`",
            "`range_validation`",
            "`semantic_preflight`",
            "`payload_consistency`",
            "static stage が 1 つでも FAIL の場合は Codex を呼ばず fail-closed",
            "confirmed / refuted / insufficient_evidence",
            "counterargument_succeeded",
            "`insufficient_evidence` | `semantic_preflight`",
            "反証成功 = 不採用 / FAIL",
            "preflight-result.json",
            "preflight-prompt.md",
            "preflight-semantic.json",
            "Markdown fallback は使わない",
            "shell で prompt 本文を展開してはならない",
            "<  ~/claude-loop-pr-codex/$dir_name/preflight-prompt.md",
            "--output-schema $semantic_schema_path",
            "--output-last-message ~/claude-loop-pr-codex/$dir_name/preflight-semantic.json",
            "--from-semantic ~/claude-loop-pr-codex/$dir_name/preflight-semantic.json",
            "--semantic-skipped --manifest ~/claude-loop-pr-codex/$dir_name/payload-manifest.json",
            "JSON オブジェクト 1 個だけ",
            "同一 prompt の 3 回リトライはしない",
            "既知 rule は severity=error",
        ):
            self.assertIn(snippet, skill)
        self.assertIn('top-level `verdict` は `PASS` / `FAIL` のみ', skill)
        self.assertNotIn("### RESULT_JSON", skill)
        self.assertNotIn("--from-markdown", skill)
        self.assertNotIn("## STAGE 1: schema_validation", skill)
        unsafe_shell_prompt_prefix = "--cd ~/claude-loop-pr-codex/$dir_name " + chr(92) + '\n  "'
        self.assertNotIn(unsafe_shell_prompt_prefix, skill)

    def test_result_is_not_mutated_by_validation(self) -> None:
        result = valid_result()
        before = copy.deepcopy(result)
        validate_preflight_result(result)
        self.assertEqual(result, before)


if __name__ == "__main__":
    unittest.main()
