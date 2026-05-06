#!/usr/bin/env python3
"""Regression tests for Issue #41 refinement-loop halting semantics."""

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
VALIDATOR_PATH = TASKS / "validate_review_rounds.py"
ROUND_SCHEMA = ROOT / "schemas" / "review-rounds.v1.json"
EVAL_SCHEMA = ROOT / "schemas" / "eval-report.v1.json"
sys.path.insert(0, str(TASKS))

from refinement_loop import (  # noqa: E402
    HaltingPolicy,
    build_review_rounds_artifact,
    evaluate_halting,
    filter_postable_findings,
    sanitize_local_candidate,
    validate_review_rounds_artifact,
)


def policy() -> HaltingPolicy:
    return HaltingPolicy(max_rounds=3, time_budget_ms=1_000, no_new_evidence_rounds=1, repeated_contradiction_limit=2)


class RefinementLoopTest(unittest.TestCase):
    def test_max_rounds_and_time_budget_are_hard_stops(self) -> None:
        rounds = [{"new_evidence_count": 1}, {"new_evidence_count": 1}, {"new_evidence_count": 1}]
        by_rounds = evaluate_halting(policy(), rounds, elapsed_ms=999, active_candidates_count=1)
        self.assertTrue(by_rounds.should_halt)
        self.assertEqual(by_rounds.reason, "max_rounds")

        by_time = evaluate_halting(policy(), [{"new_evidence_count": 1}], elapsed_ms=1_000, active_candidates_count=1)
        self.assertTrue(by_time.should_halt)
        self.assertEqual(by_time.reason, "time_budget")

    def test_no_new_evidence_halts_before_another_round(self) -> None:
        decision = evaluate_halting(policy(), [{"new_evidence_count": 0}], elapsed_ms=100, active_candidates_count=1)
        self.assertTrue(decision.should_halt)
        self.assertEqual(decision.reason, "no_new_evidence")

    def test_repeated_contradiction_is_deterministic_oscillation_guard(self) -> None:
        rounds = [
            {"new_evidence_count": 1, "contradiction_signatures": ["same-finding"]},
            {"new_evidence_count": 1, "contradiction_signatures": [{"signature": "SAME-FINDING"}]},
        ]
        decision = evaluate_halting(policy(), rounds, elapsed_ms=100, active_candidates_count=1)
        self.assertTrue(decision.should_halt)
        self.assertEqual(decision.reason, "repeated_contradiction")
        self.assertIn("same-finding", decision.detail)

    def test_all_failed_or_insufficient_candidates_stop_as_local_only(self) -> None:
        rounds = [{"new_evidence_count": 1, "verifier_fail_count": 1, "insufficient_evidence_count": 1}]
        decision = evaluate_halting(policy(), rounds, elapsed_ms=100, active_candidates_count=0)
        self.assertTrue(decision.should_halt)
        self.assertEqual(decision.reason, "no_active_candidates")

    def test_verifier_failed_candidates_are_sanitized_and_not_postable(self) -> None:
        raw_candidate = {
            "finding_id": "failing-id",
            "fingerprint": "failing-fingerprint",
            "title": "Bad candidate",
            "path": "src/app.ts",
            "line": 10,
            "reason": "verifier_fail",
            "detail": "  counterargument succeeded  ",
            "raw_log": "SECRET_TOKEN=abc123 should not persist",
            "authorization": "Bearer secret",
        }
        sanitized = sanitize_local_candidate(raw_candidate)
        self.assertTrue(sanitized["local_only"])
        self.assertNotIn("raw_log", sanitized)
        self.assertNotIn("authorization", sanitized)
        self.assertEqual(sanitized["detail"], "counterargument succeeded")

        artifact = build_review_rounds_artifact(
            policy=policy(),
            rounds=[
                {
                    "new_evidence_count": 1,
                    "verifier_fail_count": 1,
                    "rejected_candidates": [raw_candidate],
                }
            ],
            elapsed_ms=100,
            active_candidates_count=1,
            generated_at="2026-05-06T00:00:00Z",
        )
        findings = [
            {"id": "failing-id", "fingerprint": "failing-fingerprint", "title": "must stay local"},
            {"id": "passing-id", "fingerprint": "passing-fingerprint", "title": "can post"},
        ]
        self.assertEqual([item["id"] for item in filter_postable_findings(findings, artifact)], ["passing-id"])

    def test_review_rounds_artifact_validates_with_cli(self) -> None:
        artifact = build_review_rounds_artifact(
            policy=policy(),
            rounds=[
                {
                    "input_candidates_count": 2,
                    "output_candidates_count": 1,
                    "new_evidence_count": 1,
                    "verifier_pass_count": 1,
                    "verifier_fail_count": 1,
                    "rejected_candidates": [
                        {
                            "finding_id": "abc",
                            "title": "Suppressed false positive",
                            "reason": "verifier_fail",
                            "detail": "diff evidence contradicted the candidate",
                        }
                    ],
                }
            ],
            elapsed_ms=100,
            active_candidates_count=1,
            generated_at="2026-05-06T00:00:00Z",
            pr={
                "repository": "yuki777/pr-codex",
                "number": 41,
                "base_sha": "a" * 40,
                "head_sha": "b" * 40,
                "merge_commit_sha": None,
            },
        )
        self.assertEqual(validate_review_rounds_artifact(artifact), [])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "review-rounds.json"
            path.write_text(json.dumps(artifact, ensure_ascii=True), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(VALIDATOR_PATH), "--schema", str(ROUND_SCHEMA), "--data", str(path)],
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_review_rounds_validator_rejects_sensitive_rejected_candidate_fields(self) -> None:
        artifact = build_review_rounds_artifact(
            policy=policy(),
            rounds=[{"new_evidence_count": 1, "rejected_candidates": [{"title": "x", "reason": "verifier_fail"}]}],
            elapsed_ms=100,
            active_candidates_count=1,
            generated_at="2026-05-06T00:00:00Z",
        )
        mutated = copy.deepcopy(artifact)
        mutated["rounds"][0]["rejected_candidates"][0]["raw_log"] = "secret"
        errors = validate_review_rounds_artifact(mutated)
        self.assertTrue(any("sensitive/raw key" in error for error in errors))

    def test_docs_and_schemas_expose_round_metrics_for_f11(self) -> None:
        readme = (ROOT / "fixtures" / "README.md").read_text(encoding="utf-8")
        review_skill = (ROOT / "skills" / "review" / "SKILL.md").read_text(encoding="utf-8")
        root_readme = (ROOT / "README.md").read_text(encoding="utf-8")
        eval_schema = json.loads(EVAL_SCHEMA.read_text(encoding="utf-8"))
        eval_example = json.loads((ROOT / "fixtures" / "eval-report.example.json").read_text(encoding="utf-8"))
        review_rounds_schema = json.loads(ROUND_SCHEMA.read_text(encoding="utf-8"))

        for snippet in (
            "round_metrics",
            "rounds_completed",
            "halt_reason",
            "verifier_fail_candidates",
            "repeated_contradiction_events",
        ):
            self.assertIn(snippet, readme)
            self.assertIn(snippet, json.dumps(eval_schema, ensure_ascii=False))
        for snippet in (
            "review-rounds.json",
            "max_rounds",
            "time_budget_ms",
            "no_new_evidence",
            "repeated_contradiction",
            "verifier FAIL",
        ):
            self.assertIn(snippet, root_readme)
            self.assertIn(snippet, review_skill)
        self.assertEqual(review_rounds_schema["properties"]["schema_version"]["const"], "review-rounds.v1")
        self.assertEqual(eval_example["schema_version"], "eval-report.v1")
        self.assertIn("round_metrics", eval_example["aggregate"]["iterative"])


if __name__ == "__main__":
    unittest.main()
