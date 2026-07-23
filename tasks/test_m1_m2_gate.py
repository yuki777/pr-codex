#!/usr/bin/env python3
"""Tests for M1→M2 gate report generation."""

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
GATE_PATH = TASKS / "m1_m2_gate.py"
sys.path.insert(0, str(TASKS))

from m1_m2_gate import EXPECTED_FIXTURE_CONTRACTS, EXPECTED_FIXTURE_SCORING_GATES, build_report, score_report_gate_consistent  # noqa: E402
from score_fixture import load_json, score_fixture  # noqa: E402
from validate_m1_m2_gate import validate_m1_m2_gate  # noqa: E402
from validate_score_report import validate_score_report  # noqa: E402

EVALUATED_AT = "2026-05-06T00:00:00Z"
FIXTURE_NAMES = ("small", "medium", "large", "positive")


def perfect_score(size: str = "small") -> dict[str, object]:
    expected = load_json(ROOT / "fixtures" / size / "expected-findings.json")
    actual = load_json(ROOT / "fixtures" / size / "scoring-stubs" / "perfect.findings.verified.json")
    return score_fixture(expected, actual, EVALUATED_AT)


def perfect_scores() -> list[dict[str, object]]:
    return [perfect_score(name) for name in FIXTURE_NAMES]


def passing_inputs() -> dict[str, object]:
    return {
        "schema_version": "m1-m2-inputs.v1",
        "payload_422_count": 0,
        "must_fix_count_by_source": {"findings_verified": 2, "review_md": 2, "payload": 2},
        "step_4_5_pass_rate_baseline": 0.78,
        "step_4_5_pass_rate_current": 0.81,
        "run_plan_emitted": True,
        "loop_completion_rate_baseline": 0.92,
        "loop_completion_rate_current": 0.95,
    }


class M1M2GateTest(unittest.TestCase):
    def criteria_by_name(self, report: dict[str, object]) -> dict[str, dict[str, object]]:
        return {item["name"]: item for item in report["criteria"]}

    def test_fixture_gate_threshold_table_matches_fixture_oracles(self) -> None:
        actual = {}
        for size in FIXTURE_NAMES:
            expected = load_json(ROOT / "fixtures" / size / "expected-findings.json")
            actual[expected["fixture_id"]] = expected["scoring_gate"]
        self.assertEqual(actual, EXPECTED_FIXTURE_SCORING_GATES)
        for fixture_id, contract in EXPECTED_FIXTURE_CONTRACTS.items():
            self.assertRegex(contract["oracle_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(len(contract["expected_finding_ids"]), len(contract["expected_breakdown_rows"]))

    def test_all_pass_when_operational_inputs_and_fixture_scores_pass(self) -> None:
        report = build_report(perfect_scores(), passing_inputs(), EVALUATED_AT)
        self.assertEqual(validate_m1_m2_gate(report), [])
        self.assertEqual(report["overall_status"], "pass")
        self.assertTrue(all(item["status"] == "pass" for item in report["criteria"]))

    def test_missing_operational_inputs_are_unknown_not_fail(self) -> None:
        report = build_report(
            perfect_scores(),
            {"schema_version": "m1-m2-inputs.v1"},
            EVALUATED_AT,
        )
        by_name = self.criteria_by_name(report)
        self.assertEqual(by_name["payload_compat_422"]["status"], "unknown")
        self.assertEqual(by_name["fixture_scoring_gate"]["status"], "pass")
        self.assertEqual(report["overall_status"], "blocked_by_unknowns")

    def test_each_operational_gate_can_fail(self) -> None:
        cases = {
            "payload_compat_422": {"payload_422_count": 1},
            "must_fix_count_consistency": {"must_fix_count_by_source": {"findings_verified": 2, "review_md": 1, "payload": 2}},
            "step_4_5_pass_rate": {"step_4_5_pass_rate_baseline": 0.80, "step_4_5_pass_rate_current": 0.70},
            "run_plan_emitted": {"run_plan_emitted": False},
            "loop_completion_rate": {"loop_completion_rate_baseline": 0.95, "loop_completion_rate_current": 0.94},
        }
        for criterion, mutation in cases.items():
            with self.subTest(criterion=criterion):
                inputs = passing_inputs()
                inputs.update(mutation)
                report = build_report(perfect_scores(), inputs, EVALUATED_AT)
                by_name = self.criteria_by_name(report)
                self.assertEqual(by_name[criterion]["status"], "fail")
                self.assertEqual(report["overall_status"], "fail")

    def test_fixture_score_failure_fails_overall_gate(self) -> None:
        expected = load_json(ROOT / "fixtures" / "small" / "expected-findings.json")
        actual = load_json(ROOT / "fixtures" / "small" / "scoring-stubs" / "missed-known-bug.findings.verified.json")
        failing_score = score_fixture(expected, actual, EVALUATED_AT)
        report = build_report(
            [failing_score, perfect_score("medium"), perfect_score("large"), perfect_score("positive")],
            passing_inputs(),
            EVALUATED_AT,
        )
        by_name = self.criteria_by_name(report)
        self.assertEqual(by_name["fixture_scoring_gate"]["status"], "fail")
        self.assertEqual(report["overall_status"], "fail")

    def test_fixture_gate_recomputes_serialized_score_gate_consistency(self) -> None:
        expected = load_json(ROOT / "fixtures" / "small" / "expected-findings.json")
        actual = load_json(ROOT / "fixtures" / "small" / "scoring-stubs" / "false-positive-trap.findings.verified.json")
        edited = score_fixture(expected, actual, EVALUATED_AT)
        for check in edited["gate_checks"]:
            if check["name"] == "false_positive_rate_max":
                check["actual"] = 0.0
                check["passed"] = True
        edited["gate_pass"] = True
        self.assertFalse(score_report_gate_consistent(edited))
        report = build_report(
            [edited, perfect_score("medium"), perfect_score("large"), perfect_score("positive")],
            passing_inputs(),
            EVALUATED_AT,
        )
        by_name = self.criteria_by_name(report)
        self.assertEqual(by_name["fixture_scoring_gate"]["status"], "fail")
        self.assertEqual(report["overall_status"], "fail")

    def test_fixture_gate_requires_oracle_gate_checks(self) -> None:
        expected = load_json(ROOT / "fixtures" / "small" / "expected-findings.json")
        actual = load_json(ROOT / "fixtures" / "small" / "scoring-stubs" / "false-positive-trap.findings.verified.json")
        edited = score_fixture(expected, actual, EVALUATED_AT)
        edited["gate_checks"] = [
            check for check in edited["gate_checks"] if check["name"] != "false_positive_rate_max"
        ]
        edited["gate_pass"] = True
        self.assertFalse(score_report_gate_consistent(edited))
        report = build_report(
            [edited, perfect_score("medium"), perfect_score("large"), perfect_score("positive")],
            passing_inputs(),
            EVALUATED_AT,
        )
        by_name = self.criteria_by_name(report)
        self.assertEqual(by_name["fixture_scoring_gate"]["status"], "fail")
        self.assertEqual(report["overall_status"], "fail")

    def test_fixture_gate_requires_oracle_thresholds(self) -> None:
        expected = load_json(ROOT / "fixtures" / "small" / "expected-findings.json")
        actual = load_json(ROOT / "fixtures" / "small" / "scoring-stubs" / "false-positive-trap.findings.verified.json")
        edited = score_fixture(expected, actual, EVALUATED_AT)
        edited["scoring_gate"]["false_positive_rate_max"] = 1.0
        for check in edited["gate_checks"]:
            if check["name"] == "false_positive_rate_max":
                check["threshold"] = 1.0
                check["passed"] = True
        edited["gate_pass"] = True
        self.assertFalse(score_report_gate_consistent(edited))
        report = build_report(
            [edited, perfect_score("medium"), perfect_score("large"), perfect_score("positive")],
            passing_inputs(),
            EVALUATED_AT,
        )
        by_name = self.criteria_by_name(report)
        self.assertEqual(by_name["fixture_scoring_gate"]["status"], "fail")
        self.assertEqual(report["overall_status"], "fail")

    def test_fixture_gate_rejects_relabelled_score_reports(self) -> None:
        small_report = perfect_score("small")
        score_reports = []
        for size in FIXTURE_NAMES:
            expected = load_json(ROOT / "fixtures" / size / "expected-findings.json")
            report = copy.deepcopy(small_report)
            report["fixture_id"] = expected["fixture_id"]
            report["scoring_gate"] = expected["scoring_gate"]
            report["gate_checks"] = []
            for name, threshold in expected["scoring_gate"].items():
                metric_name = name.removesuffix("_min").removesuffix("_max")
                actual = report[metric_name]
                passed = actual <= threshold if name.endswith("_max") else actual >= threshold
                report["gate_checks"].append(
                    {"name": name, "actual": actual, "threshold": threshold, "passed": passed}
                )
            report["gate_pass"] = all(check["passed"] for check in report["gate_checks"])
            score_reports.append(report)

        self.assertEqual(validate_score_report(score_reports[1]), [])
        self.assertFalse(score_report_gate_consistent(score_reports[1]))
        report = build_report(score_reports, passing_inputs(), EVALUATED_AT)
        by_name = self.criteria_by_name(report)
        self.assertEqual(by_name["fixture_scoring_gate"]["status"], "fail")
        self.assertEqual(report["overall_status"], "fail")
        self.assertFalse(by_name["fixture_scoring_gate"]["evidence"]["records"][1]["oracle_sha256_match"])

    def test_fixture_scoring_requires_exact_expected_fixture_set(self) -> None:
        cases = {
            "missing": [perfect_score("small"), perfect_score("medium"), perfect_score("large")],
            "duplicate": [*perfect_scores(), perfect_score("small")],
            "unknown": [
                dict(perfect_score("small"), fixture_id="unknown-fixture"),
                perfect_score("medium"),
                perfect_score("large"),
                perfect_score("positive"),
            ],
        }
        for name, score_reports in cases.items():
            with self.subTest(name=name):
                report = build_report(score_reports, passing_inputs(), EVALUATED_AT)
                by_name = self.criteria_by_name(report)
                self.assertEqual(by_name["fixture_scoring_gate"]["status"], "fail")
                self.assertEqual(report["overall_status"], "fail")

    def test_impossible_operational_rates_are_unknown_not_pass(self) -> None:
        inputs = passing_inputs()
        inputs["step_4_5_pass_rate_baseline"] = 1.5
        inputs["step_4_5_pass_rate_current"] = 1.46
        inputs["loop_completion_rate_baseline"] = -0.1
        inputs["loop_completion_rate_current"] = -0.05
        report = build_report(perfect_scores(), inputs, EVALUATED_AT)
        by_name = self.criteria_by_name(report)
        self.assertEqual(by_name["step_4_5_pass_rate"]["status"], "unknown")
        self.assertEqual(by_name["loop_completion_rate"]["status"], "unknown")
        self.assertEqual(report["overall_status"], "blocked_by_unknowns")

    def test_no_score_reports_makes_fixture_scoring_unknown(self) -> None:
        report = build_report([], passing_inputs(), EVALUATED_AT)
        by_name = self.criteria_by_name(report)
        self.assertEqual(by_name["fixture_scoring_gate"]["status"], "unknown")
        self.assertEqual(report["overall_status"], "blocked_by_unknowns")

    def test_cli_writes_valid_gate_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            score_paths = []
            for size in FIXTURE_NAMES:
                score_path = tmp_path / f"score-{size}.json"
                score_path.write_text(json.dumps(perfect_score(size), ensure_ascii=True), encoding="utf-8")
                score_paths.append(score_path)
            inputs_path = tmp_path / "m1-m2-inputs.json"
            inputs_path.write_text(json.dumps(passing_inputs(), ensure_ascii=True), encoding="utf-8")
            out = tmp_path / "m1-m2-gate.json"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(GATE_PATH),
                    "--score-reports",
                    *[str(path) for path in score_paths],
                    "--inputs",
                    str(inputs_path),
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
        self.assertEqual(validate_m1_m2_gate(report), [])
        self.assertEqual(report["overall_status"], "pass")


if __name__ == "__main__":
    unittest.main()
