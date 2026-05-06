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
    REDACTED_SENSITIVE_VALUE,
    build_review_rounds_artifact,
    evaluate_halting,
    filter_postable_findings,
    sanitize_local_candidate,
    validate_review_rounds_artifact,
)


def policy() -> HaltingPolicy:
    return HaltingPolicy(max_rounds=3, time_budget_ms=1_000, no_new_evidence_rounds=1, repeated_contradiction_limit=2)


class RefinementLoopTest(unittest.TestCase):
    def assert_review_rounds_cli_invalid(self, artifact: dict[str, object], expected_fragment: str) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "review-rounds.json"
            path.write_text(json.dumps(artifact, ensure_ascii=True), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(VALIDATOR_PATH), "--schema", str(ROUND_SCHEMA), "--data", str(path)],
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("INVALID review rounds artifact", result.stderr)
        self.assertIn(expected_fragment, result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def valid_review_rounds_artifact(self) -> dict[str, object]:
        return build_review_rounds_artifact(
            policy=policy(),
            rounds=[
                {
                    "input_candidates_count": 2,
                    "output_candidates_count": 1,
                    "new_evidence_count": 0,
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

    def test_sensitive_candidate_values_are_redacted_before_artifact_write(self) -> None:
        raw_candidate = {
            "finding_id": "SECRET_TOKEN=abc123",
            "fingerprint": "OPENAI_API_KEY=abc123",
            "title": "Authorization: Bearer abc123",
            "path": "raw_log: captured/path",
            "reason": "verifier_fail",
            "detail": "RAW_LOG: SECRET_TOKEN=abc123 should not persist",
        }
        sanitized = sanitize_local_candidate(raw_candidate)
        self.assertEqual(sanitized["finding_id"], REDACTED_SENSITIVE_VALUE)
        self.assertEqual(sanitized["fingerprint"], REDACTED_SENSITIVE_VALUE)
        self.assertEqual(sanitized["title"], REDACTED_SENSITIVE_VALUE)
        self.assertEqual(sanitized["path"], REDACTED_SENSITIVE_VALUE)
        self.assertEqual(sanitized["detail"], REDACTED_SENSITIVE_VALUE)

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
            active_candidates_count=0,
            generated_at="2026-05-06T00:00:00Z",
        )
        rejected = artifact["rounds"][0]["rejected_candidates"][0]
        self.assertEqual(rejected["finding_id"], REDACTED_SENSITIVE_VALUE)
        self.assertEqual(rejected["fingerprint"], REDACTED_SENSITIVE_VALUE)
        self.assertEqual(rejected["title"], REDACTED_SENSITIVE_VALUE)
        self.assertEqual(rejected["path"], REDACTED_SENSITIVE_VALUE)
        self.assertEqual(rejected["detail"], REDACTED_SENSITIVE_VALUE)
        self.assertNotIn("abc123", json.dumps(artifact))
        self.assertEqual(validate_review_rounds_artifact(artifact), [])

    def test_review_rounds_artifact_validates_with_cli(self) -> None:
        artifact = self.valid_review_rounds_artifact()
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

    def test_review_rounds_cli_enforces_declared_schema_required_fields(self) -> None:
        cases = {
            "missing-generated-at": (
                lambda artifact: artifact.pop("generated_at"),
                "missing required properties: generated_at",
            ),
            "missing-halting-detail": (
                lambda artifact: artifact["halting"].pop("detail"),
                "missing required properties: detail",
            ),
            "missing-halting-timing": (
                lambda artifact: artifact["halting"].pop("elapsed_ms"),
                "missing required properties: elapsed_ms",
            ),
            "incomplete-metrics": (
                lambda artifact: artifact.update(metrics={"total_rounds": 1}),
                "missing required properties: posted_candidate_count",
            ),
            "identifierless-rejected-candidate": (
                lambda artifact: artifact["rounds"][0]["rejected_candidates"][0].pop("finding_id"),
                "missing required properties: finding_id",
            ),
        }
        for name, (mutate, expected_fragment) in cases.items():
            with self.subTest(name=name):
                artifact = self.valid_review_rounds_artifact()
                mutate(artifact)
                self.assert_review_rounds_cli_invalid(artifact, expected_fragment)

    def test_review_rounds_cli_enforces_schema_types_arrays_and_metric_types(self) -> None:
        cases = {
            "rounds-not-array": (
                lambda artifact: artifact.update(rounds={}),
                "$.rounds: expected type 'array'",
            ),
            "actions-empty": (
                lambda artifact: artifact["rounds"][0].update(actions=[]),
                "$.rounds[0].actions: expected at least 1 items",
            ),
            "metrics-bool-integer": (
                lambda artifact: artifact["metrics"].update(verifier_fail_candidates=True),
                "$.metrics.verifier_fail_candidates: expected type 'integer'",
            ),
        }
        for name, (mutate, expected_fragment) in cases.items():
            with self.subTest(name=name):
                artifact = self.valid_review_rounds_artifact()
                mutate(artifact)
                self.assert_review_rounds_cli_invalid(artifact, expected_fragment)

    def test_structured_contradiction_fallback_signature_is_schema_safe(self) -> None:
        artifact = build_review_rounds_artifact(
            policy=policy(),
            rounds=[
                {
                    "output_candidates_count": 0,
                    "new_evidence_count": 1,
                    "contradiction_signatures": [{"path": "src/app.ts", "title": "Some issue"}],
                }
            ],
            elapsed_ms=100,
            active_candidates_count=0,
            generated_at="2026-05-06T00:00:00Z",
        )
        signature = artifact["rounds"][0]["contradiction_signatures"][0]
        self.assertEqual(signature, "src/app.ts :: some issue")
        self.assertNotIn("\x1f", signature)
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

    def test_review_rounds_validator_recomputes_derived_metrics(self) -> None:
        artifact = build_review_rounds_artifact(
            policy=policy(),
            rounds=[
                {
                    "output_candidates_count": 2,
                    "new_evidence_count": 0,
                    "verifier_fail_count": 2,
                    "insufficient_evidence_count": 1,
                    "contradiction_signatures": ["same"],
                },
                {
                    "output_candidates_count": 1,
                    "new_evidence_count": 0,
                    "contradiction_signatures": ["same"],
                },
            ],
            elapsed_ms=100,
            active_candidates_count=1,
            generated_at="2026-05-06T00:00:00Z",
        )
        artifact["metrics"].update(
            verifier_fail_candidates=0,
            suppressed_candidate_count=0,
            no_new_evidence_rounds=0,
            repeated_contradiction_events=0,
            insufficient_evidence_events=0,
            oscillation_detected=False,
        )
        self.assert_review_rounds_cli_invalid(artifact, "$.metrics.verifier_fail_candidates: expected 2 from rounds")

    def test_review_rounds_validator_recomputes_posted_candidate_count(self) -> None:
        artifact = self.valid_review_rounds_artifact()
        artifact["metrics"]["posted_candidate_count"] = 0
        self.assert_review_rounds_cli_invalid(artifact, "$.metrics.posted_candidate_count: expected 1 from rounds")

    def test_review_rounds_validator_requires_final_artifact_to_halt(self) -> None:
        artifact = build_review_rounds_artifact(
            policy=policy(),
            rounds=[{"output_candidates_count": 1, "new_evidence_count": 1}],
            elapsed_ms=100,
            active_candidates_count=1,
            generated_at="2026-05-06T00:00:00Z",
        )
        self.assertFalse(artifact["halting"]["should_halt"])
        self.assert_review_rounds_cli_invalid(
            artifact,
            "$.halting.should_halt: final review rounds artifact must halt before publication",
        )

    def test_review_rounds_validator_recomputes_halting_decision(self) -> None:
        artifact = build_review_rounds_artifact(
            policy=HaltingPolicy(max_rounds=1, time_budget_ms=1_000, no_new_evidence_rounds=1, repeated_contradiction_limit=2),
            rounds=[{"output_candidates_count": 1, "new_evidence_count": 1}],
            elapsed_ms=100,
            active_candidates_count=1,
            generated_at="2026-05-06T00:00:00Z",
        )
        cases = {
            "should-halt": (
                lambda mutated: mutated["halting"].update(should_halt=False),
                "$.halting.should_halt: expected True from policy/rounds",
            ),
            "reason": (
                lambda mutated: mutated["halting"].update(reason=None),
                "$.halting.reason: expected 'max_rounds' from policy/rounds",
            ),
            "triggered-at-round": (
                lambda mutated: mutated["halting"].update(triggered_at_round=0),
                "$.halting.triggered_at_round: expected 1 from rounds",
            ),
        }
        for name, (mutate, expected_fragment) in cases.items():
            with self.subTest(name=name):
                mutated = copy.deepcopy(artifact)
                mutate(mutated)
                self.assert_review_rounds_cli_invalid(mutated, expected_fragment)

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

    def test_review_rounds_validator_rejects_sensitive_rejected_candidate_values(self) -> None:
        cases = {
            "detail-raw-log": lambda artifact: artifact["rounds"][0]["rejected_candidates"][0].update(
                detail="RAW_LOG: captured debug payload"
            ),
            "detail-authorization": lambda artifact: artifact["rounds"][0]["rejected_candidates"][0].update(
                detail="Authorization: Bearer abc.def.ghi"
            ),
            "title-token-assignment": lambda artifact: artifact["rounds"][0]["rejected_candidates"][0].update(
                title="SECRET_TOKEN=abc123 leaked"
            ),
            "title-api-key-assignment": lambda artifact: artifact["rounds"][0]["rejected_candidates"][0].update(
                title="api_key = abc123 leaked"
            ),
            "detail-private-key": lambda artifact: artifact["rounds"][0]["rejected_candidates"][0].update(
                detail="-----BEGIN PRIVATE KEY----- MIIEvQIBADAN"
            ),
            "path-raw-log": lambda artifact: artifact["rounds"][0]["rejected_candidates"][0].update(
                path="raw log verifier excerpt"
            ),
            "finding-id-token": lambda artifact: artifact["rounds"][0]["rejected_candidates"][0].update(
                finding_id="SECRET_TOKEN=abc123"
            ),
            "fingerprint-api-key": lambda artifact: artifact["rounds"][0]["rejected_candidates"][0].update(
                fingerprint="OPENAI_API_KEY=abc123"
            ),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                artifact = self.valid_review_rounds_artifact()
                mutate(artifact)
                self.assert_review_rounds_cli_invalid(artifact, "sensitive/raw value is not allowed")

    def test_review_rounds_validator_rejects_sensitive_contradiction_signatures(self) -> None:
        artifact = self.valid_review_rounds_artifact()
        artifact["rounds"][0]["contradiction_signatures"] = ["Authorization: Bearer abc123"]
        self.assert_review_rounds_cli_invalid(artifact, "sensitive/raw value is not allowed")

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
