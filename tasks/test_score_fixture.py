#!/usr/bin/env python3
"""Regression tests for deterministic fixture scoring."""

from __future__ import annotations

import json
import copy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "tasks"
SCORE_PATH = TASKS / "score_fixture.py"
SCORE_SCHEMA_PATH = ROOT / "schemas" / "score-report.v1.json"
sys.path.insert(0, str(TASKS))

from score_fixture import load_json, score_fixture, validate_fixture_context  # noqa: E402
from validate_score_report import validate_score_report  # noqa: E402

EVALUATED_AT = "2026-05-06T00:00:00Z"


def score(size: str, stub: str) -> dict[str, object]:
    expected = load_json(ROOT / "fixtures" / size / "expected-findings.json")
    actual = load_json(ROOT / "fixtures" / size / "scoring-stubs" / f"{stub}.findings.verified.json")
    report = score_fixture(expected, actual, EVALUATED_AT)
    errors = validate_score_report(report)
    if errors:
        raise AssertionError(errors)
    return report


class ScoreFixtureTest(unittest.TestCase):
    def test_perfect_stubs_pass_all_fixture_gates(self) -> None:
        for size in ("small", "medium", "large", "positive"):
            with self.subTest(size=size):
                report = score(size, "perfect")
                self.assertTrue(report["gate_pass"])
                self.assertEqual(report["exact_pass_rate"], 1.0)
                self.assertEqual(report["acceptable_pass_rate"], 1.0)
                self.assertEqual(report["false_positive_rate"], 0.0)
                self.assertEqual(report["recall_known_bug"], 1.0)

    def test_missed_known_bug_fails_recall_and_gate(self) -> None:
        report = score("small", "missed-known-bug")
        self.assertFalse(report["gate_pass"])
        self.assertEqual(report["recall_known_bug"], 0.0)
        self.assertEqual(report["acceptable_pass_rate"], 0.0)
        first = report["breakdown"][0]
        self.assertEqual(first["match_status"], "missed")
        self.assertIsNone(first["matched_actual_fingerprint"])

    def test_false_positive_trap_counts_promoted_must_fix(self) -> None:
        report = score("small", "false-positive-trap")
        self.assertFalse(report["gate_pass"])
        self.assertEqual(report["false_positive_rate"], 1.0)
        trap_rows = [row for row in report["breakdown"] if row["expected_outcome"] == "known_false_positive_trap"]
        self.assertEqual(len(trap_rows), 1)
        self.assertEqual(trap_rows[0]["match_status"], "false_positive_promoted")
        self.assertFalse(trap_rows[0]["severity_diff"]["acceptable"])
        self.assertEqual(len(report["unmatched_actuals"]), 1)
        self.assertEqual(report["unmatched_actuals"][0]["severity"], "must_fix")

    def test_false_positive_trap_title_match_ignores_wrong_category(self) -> None:
        expected = load_json(ROOT / "fixtures" / "small" / "expected-findings.json")
        actual = copy.deepcopy(load_json(ROOT / "fixtures" / "small" / "scoring-stubs" / "perfect.findings.verified.json"))
        trap = next(item for item in expected["expected_findings"] if item["expected_outcome"] == "known_false_positive_trap")
        overpromotion = copy.deepcopy(actual["findings"][0])
        overpromotion["severity"] = "must_fix"
        overpromotion["category"] = "bug"
        overpromotion["title"] = trap["title"]
        overpromotion["problem"] = trap["title"]
        overpromotion["fingerprint"] = "2" * 64
        overpromotion["id"] = "2" * 64
        actual["findings"].append(overpromotion)
        report = score_fixture(expected, actual, EVALUATED_AT)
        self.assertFalse(report["gate_pass"])
        self.assertEqual(report["false_positive_rate"], 1.0)
        trap_rows = [row for row in report["breakdown"] if row["expected_id"] == trap["id"]]
        self.assertEqual(trap_rows[0]["match_status"], "false_positive_promoted")

    def test_known_bug_matches_by_changed_line_when_category_drifts(self) -> None:
        expected = load_json(ROOT / "fixtures" / "positive" / "expected-findings.json")
        actual = copy.deepcopy(
            load_json(ROOT / "fixtures" / "positive" / "scoring-stubs" / "perfect.findings.verified.json")
        )
        idempotency = next(
            finding for finding in actual["findings"] if finding["location"]["start_line"] == 53
        )
        idempotency["category"] = "bug"

        report = score_fixture(expected, actual, EVALUATED_AT)

        self.assertEqual(report["recall_known_bug"], 1.0)
        self.assertEqual(report["unmatched_actuals"], [])

    def test_partial_axes_drift_separates_exact_from_acceptable(self) -> None:
        report = score("small", "partial-axes-drift")
        self.assertTrue(report["gate_pass"])
        self.assertEqual(report["exact_pass_rate"], 0.0)
        self.assertEqual(report["acceptable_pass_rate"], 1.0)
        row = report["breakdown"][0]
        self.assertFalse(row["axes_diff"]["impactful"]["actual"] == row["axes_diff"]["impactful"]["expected"])
        self.assertTrue(row["axes_diff"]["impactful"]["acceptable"])

    def test_insufficient_evidence_fails_acceptable_scoring(self) -> None:
        expected = load_json(ROOT / "fixtures" / "small" / "expected-findings.json")
        actual = copy.deepcopy(load_json(ROOT / "fixtures" / "small" / "scoring-stubs" / "perfect.findings.verified.json"))
        finding = actual["findings"][0]
        finding["severity"] = "nit"
        finding["evidence_level"] = "suspicion"
        finding["posting"] = {
            "post_policy": "local_only",
            "explanation_postable": False,
            "not_postable_reason": "low_evidence_suspicion",
            "audience": "human_reviewer",
        }
        report = score_fixture(expected, actual, EVALUATED_AT)
        self.assertFalse(report["gate_pass"])
        self.assertEqual(report["acceptable_pass_rate"], 0.0)
        self.assertFalse(report["breakdown"][0]["evidence_level_ok"])

    def test_acceptable_risk_overpromotion_without_location_fails_gate(self) -> None:
        expected = load_json(ROOT / "fixtures" / "small" / "expected-findings.json")
        actual = copy.deepcopy(load_json(ROOT / "fixtures" / "small" / "scoring-stubs" / "perfect.findings.verified.json"))
        overpromotion = copy.deepcopy(actual["findings"][0])
        overpromotion["location"]["path"] = "src/Extension/Application/AbstractAppDocs.php"
        overpromotion["severity"] = "should_fix"
        overpromotion["category"] = "code_quality"
        overpromotion["title"] = "Reviewer suggests adding @since / @removed-in tags"
        overpromotion["problem"] = "Reviewer suggests adding @since / @removed-in tags"
        overpromotion["evidence_level"] = "corroborated"
        overpromotion["axes"] = {"real": "yes", "triggerable": "no", "impactful": "no"}
        overpromotion["fingerprint"] = "1" * 64
        overpromotion["id"] = "1" * 64
        actual["findings"].append(overpromotion)
        report = score_fixture(expected, actual, EVALUATED_AT)
        self.assertFalse(report["gate_pass"])
        self.assertEqual(report["acceptable_pass_rate"], 0.5)
        risk_rows = [row for row in report["breakdown"] if row["expected_id"] == "missing-since-removed-in-tag-trap"]
        self.assertEqual(risk_rows[0]["match_status"], "matched")
        self.assertFalse(risk_rows[0]["severity_diff"]["acceptable"])

    def test_acceptable_risk_title_match_ignores_wrong_category(self) -> None:
        expected = load_json(ROOT / "fixtures" / "small" / "expected-findings.json")
        actual = copy.deepcopy(load_json(ROOT / "fixtures" / "small" / "scoring-stubs" / "perfect.findings.verified.json"))
        risk = next(item for item in expected["expected_findings"] if item["expected_outcome"] == "acceptable_risk")
        overpromotion = copy.deepcopy(actual["findings"][0])
        overpromotion["severity"] = "should_fix"
        overpromotion["category"] = "design"
        overpromotion["title"] = risk["title"]
        overpromotion["problem"] = risk["title"]
        overpromotion["evidence_level"] = "corroborated"
        overpromotion["axes"] = {"real": "yes", "triggerable": "no", "impactful": "no"}
        overpromotion["fingerprint"] = "3" * 64
        overpromotion["id"] = "3" * 64
        actual["findings"].append(overpromotion)
        report = score_fixture(expected, actual, EVALUATED_AT)
        self.assertFalse(report["gate_pass"])
        self.assertEqual(report["acceptable_pass_rate"], 0.5)
        risk_rows = [row for row in report["breakdown"] if row["expected_id"] == risk["id"]]
        self.assertEqual(risk_rows[0]["match_status"], "matched")
        self.assertFalse(risk_rows[0]["severity_diff"]["acceptable"])

    def test_score_report_validator_rejects_contradictory_report(self) -> None:
        report = score("small", "perfect")
        report["counts"]["acceptable_pass"] = 0
        report["gate_pass"] = True
        errors = validate_score_report(report)
        self.assertTrue(any("acceptable_pass_rate" in error for error in errors))
        self.assertTrue(any("counts.acceptable_pass" in error for error in errors))

    def test_score_report_validator_rejects_gate_pass_mismatch(self) -> None:
        report = score("small", "missed-known-bug")
        report["gate_pass"] = True
        errors = validate_score_report(report)
        self.assertIn("$.gate_pass: must equal all(gate_checks[].passed)", errors)

    def test_score_report_validator_binds_gate_check_actual_to_top_level_metric(self) -> None:
        report = score("small", "false-positive-trap")
        for check in report["gate_checks"]:
            if check["name"] == "false_positive_rate_max":
                check["actual"] = 0.0
                check["passed"] = True
        report["gate_pass"] = True
        errors = validate_score_report(report)
        self.assertIn("$.gate_checks[1].actual: must equal $.false_positive_rate", errors)

    def test_score_report_includes_oracle_scoring_gate(self) -> None:
        report = score("medium", "perfect")
        self.assertRegex(report["oracle_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            report["expected_finding_ids"],
            [
                "removed-assert-undefined-index-risk",
                "psalm-type-alias-no-runtime-validation",
                "php74-compat-break-trap",
            ],
        )
        self.assertEqual(
            report["scoring_gate"],
            {
                "acceptable_pass_rate_min": 0.8,
                "exact_pass_rate_min": 0.5,
                "false_positive_rate_max": 0.1,
            },
        )
        self.assertEqual(
            {check["name"] for check in report["gate_checks"]},
            set(report["scoring_gate"]),
        )

    def test_score_report_validator_requires_gate_check_for_each_scoring_gate_threshold(self) -> None:
        report = score("small", "false-positive-trap")
        report["gate_checks"] = [
            check for check in report["gate_checks"] if check["name"] != "false_positive_rate_max"
        ]
        report["gate_pass"] = all(check["passed"] for check in report["gate_checks"])
        errors = validate_score_report(report)
        self.assertIn("$.gate_checks: missing checks from scoring_gate: false_positive_rate_max", errors)

    def test_score_report_validator_binds_gate_check_threshold_to_scoring_gate(self) -> None:
        report = score("small", "false-positive-trap")
        for check in report["gate_checks"]:
            if check["name"] == "false_positive_rate_max":
                check["threshold"] = 1.0
                check["passed"] = True
        report["gate_pass"] = True
        errors = validate_score_report(report)
        self.assertIn("$.gate_checks[1].threshold: must equal $.scoring_gate.false_positive_rate_max", errors)

    def test_fixture_context_must_match_before_scoring(self) -> None:
        expected = load_json(ROOT / "fixtures" / "small" / "expected-findings.json")
        actual = copy.deepcopy(load_json(ROOT / "fixtures" / "small" / "scoring-stubs" / "perfect.findings.verified.json"))
        actual["pr"]["number"] = 999
        errors = validate_fixture_context(expected, actual)
        self.assertTrue(any("context.pr_number" in error for error in errors))

    def test_cli_writes_valid_score_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "score-small.json"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCORE_PATH),
                    "--expected",
                    str(ROOT / "fixtures" / "small" / "expected-findings.json"),
                    "--actual",
                    str(ROOT / "fixtures" / "small" / "scoring-stubs" / "perfect.findings.verified.json"),
                    "--out",
                    str(out),
                    "--evaluated-at",
                    EVALUATED_AT,
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(validate_score_report(report), [])
        self.assertEqual(report["fixture_id"], "bear-sunday-pr164-small")
        self.assertTrue(report["gate_pass"])

    def test_score_report_schema_file_is_valid_json(self) -> None:
        schema = json.loads(SCORE_SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["schema_version"]["const"], "score-report.v1")


if __name__ == "__main__":
    unittest.main()
