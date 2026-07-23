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
    CLASSIFICATION_SCHEMA_PATH,
    ROUTE_M2,
    SCHEMA_PATH,
    expected_pr_classification,
    expected_rationale,
    load_json,
    schema_matches,
    synthetic_plan,
    validate_pr_classification_semantics,
    validate_run_plan_semantics,
)


class ValidateRunPlanRoutingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = load_json(SCHEMA_PATH)
        cls.classification_schema = load_json(CLASSIFICATION_SCHEMA_PATH)

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
            ("security-medium", ["src/auth/login.ts", *[f"src/small_{index}.ts" for index in range(7)]], 400, 0, "medium", "standard"),
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

    def test_pr_classification_selects_specialists_by_changed_files(self) -> None:
        cases = [
            (
                "docs-only",
                ["README.md", "docs/usage.md"],
                {
                    "primary_type": "docs-only",
                    "all_types": ["docs-only"],
                    "selected_specialists": ["docs"],
                },
            ),
            (
                "workflow-ci",
                [".github/workflows/ci.yml", "Dockerfile"],
                {
                    "primary_type": "workflow-ci",
                    "all_types": ["workflow-ci"],
                    "selected_specialists": ["workflow"],
                },
            ),
            (
                "python-validator-runtime",
                ["tasks/validate_run_plan.py", "tasks/extract_actual_cost.py"],
                {
                    "primary_type": "python-validator-runtime",
                    "all_types": ["python-validator-runtime"],
                    "selected_specialists": ["python"],
                },
            ),
            (
                "security-sensitive",
                ["src/auth/login.py", "tests/test_login.py"],
                {
                    "primary_type": "security-sensitive",
                    "all_types": ["test-only", "security-sensitive"],
                    "selected_specialists": ["tests", "security"],
                },
            ),
            (
                "mixed",
                ["skills/review/SKILL.md", "tests/test_review.py", "README.md"],
                {
                    "primary_type": "mixed",
                    "all_types": ["docs-only", "test-only", "review-skill-contract"],
                    "selected_specialists": ["docs", "tests", "review-skill"],
                },
            ),
        ]

        for name, files, expected_subset in cases:
            with self.subTest(name=name):
                plan = synthetic_plan(files, 2, 30, 5)
                classification = plan["pr_classification"]
                for key, value in expected_subset.items():
                    self.assertEqual(classification[key], value)
                self.assertTrue(classification["read_only"])
                self.assertEqual(classification, expected_pr_classification(dict(plan, _files=files)))
                validate_pr_classification_semantics(self.classification_schema, classification, dict(plan, _files=files))
                validate_run_plan_semantics(self.schema, plan)

    def test_actual_cost_contract_uses_provider_reported_values_only(self) -> None:
        plan = self.valid_plan()
        self.assertEqual(
            plan["cost"],
            {
                "actual_usd": None,
                "currency": "USD",
                "source": "unavailable",
                "components": [],
            },
        )
        validate_run_plan_semantics(self.schema, plan)

        reported = copy.deepcopy(plan)
        reported["cost"] = {
            "actual_usd": 0.1234,
            "currency": "USD",
            "source": "provider_reported",
            "components": [
                {"tool": "claude", "actual_usd": 0.05, "source": "cli_log"},
                {"tool": "codex", "actual_usd": 0.0734, "source": "cli_log"},
            ],
        }
        validate_run_plan_semantics(self.schema, reported)

        missing_cost = copy.deepcopy(plan)
        del missing_cost["cost"]
        self.assertFalse(schema_matches(self.schema, missing_cost))

        estimated = copy.deepcopy(plan)
        estimated["cost"] = {
            "actual_usd": 0.1234,
            "currency": "USD",
            "source": "estimated_from_pricing_table",
            "components": [],
        }
        self.assertFalse(schema_matches(self.schema, estimated))

        pricing_table = copy.deepcopy(plan)
        pricing_table["cost"] = {
            "actual_usd": 0.1234,
            "currency": "USD",
            "source": "provider_reported",
            "components": [],
            "pricing_table": {"gpt-5.5": 1.0},
        }
        self.assertFalse(schema_matches(self.schema, pricing_table))

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
