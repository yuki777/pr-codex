#!/usr/bin/env python3
"""Regression tests for the run-plan routing artifact."""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "tasks"
sys.path.insert(0, str(TASKS))

from validate_run_plan import (  # noqa: E402
    ROUTE_M2,
    SCHEMA_PATH,
    expected_rationale,
    load_json,
    schema_matches,
    synthetic_plan,
    validate_run_plan_semantics,
)


class ValidateRunPlanRoutingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = load_json(SCHEMA_PATH)

    def valid_plan(self) -> dict[str, object]:
        plan = synthetic_plan([f"src/file_{index}.ts" for index in range(5)], 8, 120, 80)
        self.assertIsInstance(plan, dict)
        return plan  # type: ignore[return-value]

    def test_small_default_standard_routes_to_standard_profile(self) -> None:
        plan = self.valid_plan()
        routing = plan["routing_decision"]

        self.assertEqual(plan["recommended_mode"], "standard")
        self.assertEqual(plan["depth_actual"], "standard")
        self.assertEqual(
            routing,
            {
                "budget_class": "small",
                "route": ROUTE_M2,
                "model_profile": "standard",
                "rationale": expected_rationale(plan),
            },
        )
        validate_run_plan_semantics(self.schema, plan)

    def test_budget_and_profile_matrix_uses_section2_rules(self) -> None:
        cases = [
            ("line-heavy", [f"src/line_heavy_{index}.ts" for index in range(5)], 6000, 0, "large", "standard"),
            ("security-medium", ["src/auth/login.ts", *[f"src/small_{index}.ts" for index in range(7)]], 400, 0, "medium", "deep"),
            ("focused-large", [f"src/focused_{index}.ts" for index in range(60)], 3000, 0, "large", "focused-fallback"),
            ("skip-large", ["db/migrations/001.sql", *[f"src/skip_{index}.ts" for index in range(119)]], 5000, 3000, "large", "focused-fallback"),
        ]

        for name, files, lines_added, lines_removed, budget_class, model_profile in cases:
            with self.subTest(name=name):
                plan = synthetic_plan(files, 12, lines_added, lines_removed)
                routing = plan["routing_decision"]
                self.assertEqual(routing["budget_class"], budget_class)
                self.assertEqual(routing["model_profile"], model_profile)
                self.assertEqual(routing["route"], ROUTE_M2)
                self.assertEqual(routing["rationale"], expected_rationale(plan))
                validate_run_plan_semantics(self.schema, plan)

    def test_schema_rejects_missing_extra_and_bad_enum_routing_fields(self) -> None:
        plan = self.valid_plan()
        without_routing = copy.deepcopy(plan)
        del without_routing["routing_decision"]
        self.assertFalse(schema_matches(self.schema, without_routing))

        extra = copy.deepcopy(plan)
        extra["routing_decision"]["provider"] = "private-model"
        self.assertFalse(schema_matches(self.schema, extra))

        bad_route = copy.deepcopy(plan)
        bad_route["routing_decision"]["route"] = "claude+codex+specialist"
        self.assertFalse(schema_matches(self.schema, bad_route))

        bad_profile = copy.deepcopy(plan)
        bad_profile["routing_decision"]["model_profile"] = "gpt-5.5"
        self.assertFalse(schema_matches(self.schema, bad_profile))

    def test_validator_rejects_inconsistent_derived_fields(self) -> None:
        plan = self.valid_plan()

        bad_profile = copy.deepcopy(plan)
        bad_profile["routing_decision"]["model_profile"] = "deep"
        with self.assertRaisesRegex(AssertionError, r"routing_decision\.model_profile"):
            validate_run_plan_semantics(self.schema, bad_profile)

        bad_rationale = copy.deepcopy(plan)
        bad_rationale["routing_decision"]["rationale"] = "LLM picked a private model"
        with self.assertRaisesRegex(AssertionError, r"routing_decision\.rationale"):
            validate_run_plan_semantics(self.schema, bad_rationale)

    def test_schema_file_is_json(self) -> None:
        # Keeps the workflow failure readable if the schema is edited by hand.
        json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
