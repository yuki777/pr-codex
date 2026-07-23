#!/usr/bin/env python3
"""Tests for expected-findings.v1 fixture oracle validation."""

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
SCHEMA_PATH = ROOT / "schemas" / "expected-findings.v1.json"
VALIDATOR_PATH = TASKS / "validate_expected_findings.py"
sys.path.insert(0, str(TASKS))

from validate_expected_findings import validate_expected_findings  # noqa: E402


class ValidateExpectedFindingsTest(unittest.TestCase):
    def load_fixture(self, name: str) -> dict[str, object]:
        return json.loads((ROOT / "fixtures" / name / "expected-findings.json").read_text(encoding="utf-8"))

    def assert_invalid_cli(self, data: dict[str, object], expected_fragment: str) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "expected-findings.json"
            path.write_text(json.dumps(data, ensure_ascii=True), encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, str(VALIDATOR_PATH), "--schema", str(SCHEMA_PATH), "--data", str(path)],
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        self.assertIn("INVALID expected-findings artifact", completed.stderr)
        self.assertIn(expected_fragment, completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)

    def test_existing_fixture_oracles_are_valid(self) -> None:
        for name in ("small", "medium", "large", "positive"):
            with self.subTest(name=name):
                self.assertEqual(validate_expected_findings(self.load_fixture(name)), [])

    def test_cli_accepts_existing_fixture_oracles(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(VALIDATOR_PATH),
                "--schema",
                str(SCHEMA_PATH),
                "--data",
                str(ROOT / "fixtures" / "small" / "expected-findings.json"),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("VALID expected-findings artifact", completed.stdout)

    def test_duplicate_expected_ids_are_rejected(self) -> None:
        data = self.load_fixture("small")
        data["expected_findings"].append(copy.deepcopy(data["expected_findings"][0]))
        self.assert_invalid_cli(data, "duplicate expected finding id")

    def test_override_cannot_contradict_selected_profile(self) -> None:
        data = self.load_fixture("small")
        data["expected_findings"][0]["strictness_profile"] = "must_fix_strict"
        data["expected_findings"][0]["acceptable_overrides"] = {"real": ["no"]}
        self.assert_invalid_cli(data, "acceptable_overrides may only relax")

    def test_line_range_must_be_ordered(self) -> None:
        data = self.load_fixture("large")
        data["expected_findings"][0]["location_match"]["line_range"] = [20, 10]
        self.assert_invalid_cli(data, "end must be >= start")


if __name__ == "__main__":
    unittest.main()
