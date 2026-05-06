#!/usr/bin/env python3
"""Regression tests for deterministic fixture scoring."""

from __future__ import annotations

import json
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

from score_fixture import load_json, score_fixture  # noqa: E402
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
        for size in ("small", "medium", "large"):
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

    def test_partial_axes_drift_separates_exact_from_acceptable(self) -> None:
        report = score("small", "partial-axes-drift")
        self.assertTrue(report["gate_pass"])
        self.assertEqual(report["exact_pass_rate"], 0.0)
        self.assertEqual(report["acceptable_pass_rate"], 1.0)
        row = report["breakdown"][0]
        self.assertFalse(row["axes_diff"]["impactful"]["actual"] == row["axes_diff"]["impactful"]["expected"])
        self.assertTrue(row["axes_diff"]["impactful"]["acceptable"])

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
