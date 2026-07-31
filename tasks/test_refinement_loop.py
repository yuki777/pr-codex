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
CONTROLLER_PATH = TASKS / "refinement_loop.py"
ROUND_SCHEMA = ROOT / "schemas" / "review-rounds.v1.json"
EVAL_SCHEMA = ROOT / "schemas" / "eval-report.v1.json"
sys.path.insert(0, str(TASKS))

from refinement_loop import (  # noqa: E402
    HaltingPolicy,
    REDACTED_IDENTIFIER_PREFIX,
    REDACTED_SENSITIVE_VALUE,
    auto_deep_eligible,
    apply_auto_deep,
    candidate_state_digest,
    candidate_state_delta,
    build_review_rounds_artifact,
    evaluate_halting,
    filter_postable_findings,
    plan_next_round,
    sanitize_local_candidate,
    select_round_targets,
    validate_review_rounds_artifact,
    validate_json_schema_subset,
)


def policy() -> HaltingPolicy:
    return HaltingPolicy(max_rounds=2, time_budget_ms=1_000, no_new_evidence_rounds=1, repeated_contradiction_limit=2)


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

    def test_default_policy_caps_adaptive_review_at_two_rounds(self) -> None:
        self.assertEqual(HaltingPolicy().max_rounds, 2)

    def test_sensitive_run_plan_policy_allows_three_rounds(self) -> None:
        run_plan = {
            "risk_tags": ["security"],
            "review_loop": {
                "halting_policy": {
                    "max_rounds": 3,
                    "time_budget_ms": 1_000,
                    "no_new_evidence_rounds": 1,
                    "repeated_contradiction_limit": 2,
                }
            },
        }
        self.assertEqual(HaltingPolicy.from_mapping(run_plan).max_rounds, 3)

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
                    "target_candidate_ids": ["candidate-a", "candidate-b"],
                    "state_digest_before": "a" * 64,
                    "state_digest_after": "b" * 64,
                    "changed_candidate_ids": ["candidate-a"],
                    "changed_candidate_count": 1,
                    "evidence_added_count": 0,
                    "disposition_changed_count": 1,
                    "untargeted_candidate_ids": [],
                    "untargeted_candidate_count": 0,
                    "remaining_active_count": 1,
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
        rounds = [{"new_evidence_count": 1}, {"new_evidence_count": 1}]
        by_rounds = evaluate_halting(policy(), rounds, elapsed_ms=999, active_candidates_count=1)
        self.assertTrue(by_rounds.should_halt)
        self.assertEqual(by_rounds.reason, "max_rounds")

        by_time = evaluate_halting(policy(), [{"new_evidence_count": 1}], elapsed_ms=1_000, active_candidates_count=1)
        self.assertTrue(by_time.should_halt)
        self.assertEqual(by_time.reason, "time_budget")

    def test_zero_time_budget_still_runs_the_mandatory_first_round(self) -> None:
        candidate = {
            "candidate_id": "first-round",
            "evidence_state": "needs_evidence",
            "evidence_level": "suspicion",
            "axes": {
                "real": "yes",
                "triggerable": "unknown",
                "impactful": "unknown",
            },
        }
        zero_extra_round_budget = HaltingPolicy(
            max_rounds=3,
            time_budget_ms=0,
            no_new_evidence_rounds=1,
            repeated_contradiction_limit=2,
        )

        plan = plan_next_round(
            zero_extra_round_budget,
            rounds=[],
            candidates=[candidate],
            elapsed_ms=0,
        )

        self.assertTrue(plan["should_run"])
        self.assertIsNone(plan["halting"]["reason"])
        self.assertEqual(plan["target_candidate_ids"], ["first-round"])
        self.assertEqual(plan["candidate_updates"], [])
        self.assertIsNone(plan["round_state"])

    def test_no_new_evidence_halts_before_another_round(self) -> None:
        decision = evaluate_halting(policy(), [{"new_evidence_count": 0}], elapsed_ms=100, active_candidates_count=1)
        self.assertTrue(decision.should_halt)
        self.assertEqual(decision.reason, "no_new_evidence")

    def test_halting_uses_latest_untargeted_state(self) -> None:
        decision = evaluate_halting(
            HaltingPolicy(
                max_rounds=3,
                time_budget_ms=1_000,
                no_new_evidence_rounds=1,
                repeated_contradiction_limit=2,
            ),
            [
                {
                    "new_evidence_count": 1,
                    "untargeted_candidate_count": 1,
                },
                {
                    "new_evidence_count": 1,
                    "untargeted_candidate_count": 0,
                    "verifier_pass_count": 1,
                },
            ],
            elapsed_ms=100,
            active_candidates_count=0,
        )

        self.assertTrue(decision.should_halt)
        self.assertEqual(decision.reason, "all_candidates_verified")

    def test_candidate_state_digest_is_order_independent_and_tracks_decision_state(self) -> None:
        candidates = [
            {
                "candidate_id": "b",
                "evidence_state": "needs_evidence",
                "evidence_level": "suspicion",
                "axes": {"real": "yes", "triggerable": "unknown", "impactful": "unknown"},
                "problem": "prose must not affect the controller",
                "location": {"path": "src/b.py", "start_line": 10, "end_line": None, "side": "RIGHT"},
            },
            {
                "candidate_id": "a",
                "evidence_state": "supported",
                "evidence_level": "verified",
                "axes": {"real": "yes", "triggerable": "yes", "impactful": "yes"},
                "location": {"path": "src/a.py", "start_line": 20, "end_line": 21, "side": "RIGHT"},
            },
        ]
        reordered = copy.deepcopy(list(reversed(candidates)))
        reordered[1]["problem"] = "different prose"
        self.assertEqual(candidate_state_digest(candidates), candidate_state_digest(reordered))

        resolved = copy.deepcopy(candidates)
        resolved[0]["evidence_state"] = "supported"
        resolved[0]["evidence_level"] = "verified"
        resolved[0]["axes"] = {"real": "yes", "triggerable": "yes", "impactful": "yes"}
        self.assertNotEqual(candidate_state_digest(candidates), candidate_state_digest(resolved))

        relocated = copy.deepcopy(candidates)
        relocated[0]["location"]["start_line"] = 11
        self.assertNotEqual(candidate_state_digest(candidates), candidate_state_digest(relocated))

        expanded_blast_radius = copy.deepcopy(candidates)
        expanded_blast_radius[0]["blast_radius"] = "systemic"
        self.assertNotEqual(
            candidate_state_digest(candidates),
            candidate_state_digest(expanded_blast_radius),
        )

    def test_candidate_state_tracks_disagreement_kinds_independently(self) -> None:
        before = [
            {
                "candidate_id": "disputed",
                "evidence_state": "supported",
                "evidence_level": "verified",
                "axes": {
                    "real": "yes",
                    "triggerable": "yes",
                    "impactful": "yes",
                },
                "disagreement": False,
                "severity_disputed": False,
                "contradiction": False,
            }
        ]
        before_digest = candidate_state_digest(before)
        changed_digests: set[str] = set()

        for field in ("disagreement", "severity_disputed", "contradiction"):
            with self.subTest(field=field):
                after = copy.deepcopy(before)
                after[0][field] = True
                after_digest = candidate_state_digest(after)
                delta = candidate_state_delta(before, after)

                self.assertNotEqual(before_digest, after_digest)
                self.assertEqual(delta["changed_candidate_ids"], ["disputed"])
                self.assertEqual(delta["disposition_changed_count"], 1)
                changed_digests.add(after_digest)

        self.assertEqual(len(changed_digests), 3)

    def test_candidate_state_delta_tracks_host_owned_round_metrics(self) -> None:
        before = [
            {
                "candidate_id": "high-risk",
                "evidence_state": "needs_evidence",
                "evidence_level": "suspicion",
                "axes": {"real": "yes", "triggerable": "unknown", "impactful": "unknown"},
                "severity_raw": "must_fix",
                "category_raw": "bug",
            }
        ]
        after = copy.deepcopy(before)
        after[0]["evidence_state"] = "supported"
        after[0]["evidence_level"] = "verified"
        after[0]["axes"]["triggerable"] = "yes"
        delta = candidate_state_delta(before, after)
        self.assertEqual(delta["changed_candidate_ids"], ["high-risk"])
        self.assertEqual(delta["changed_candidate_count"], 1)
        self.assertEqual(delta["evidence_added_count"], 1)
        self.assertEqual(delta["disposition_changed_count"], 0)
        self.assertEqual(delta["remaining_active_count"], 1)

    def test_candidate_state_delta_counts_known_axis_value_changes_as_evidence(self) -> None:
        before = [
            {
                "candidate_id": "counterexample",
                "evidence_state": "supported",
                "evidence_level": "verified",
                "axes": {
                    "real": "yes",
                    "triggerable": "yes",
                    "impactful": "yes",
                },
            }
        ]
        after = copy.deepcopy(before)
        after[0]["axes"]["real"] = "no"

        delta = candidate_state_delta(before, after)

        self.assertEqual(delta["changed_candidate_count"], 1)
        self.assertEqual(delta["evidence_added_count"], 1)

    def test_round_two_targets_only_high_risk_disputed_or_evidence_seeking_candidates(self) -> None:
        candidates = [
            {
                "candidate_id": "verified",
                "evidence_state": "supported",
                "evidence_level": "verified",
                "axes": {"real": "yes", "triggerable": "yes", "impactful": "yes"},
                "severity_raw": "must_fix",
                "category_raw": "bug",
            },
            {
                "candidate_id": "low-risk-unknown",
                "evidence_state": "supported",
                "evidence_level": "suspicion",
                "axes": {"real": "yes", "triggerable": "unknown", "impactful": "unknown"},
                "severity_raw": "nit",
                "category_raw": "code_quality",
            },
            {
                "candidate_id": "needs-evidence",
                "evidence_state": "needs_evidence",
                "evidence_level": "suspicion",
                "axes": {"real": "yes", "triggerable": "unknown", "impactful": "unknown"},
                "severity_raw": "should_fix",
                "category_raw": "tests",
            },
            {
                "candidate_id": "high-risk",
                "evidence_state": "supported",
                "evidence_level": "suspicion",
                "axes": {"real": "yes", "triggerable": "yes", "impactful": "unknown"},
                "severity_raw": "must_fix",
                "category_raw": "bug",
            },
            {
                "candidate_id": "disputed",
                "evidence_state": "supported",
                "evidence_level": "verified",
                "axes": {"real": "yes", "triggerable": "yes", "impactful": "yes"},
                "severity_raw": "should_fix",
                "category_raw": "code_quality",
                "severity_disputed": True,
            },
        ]
        self.assertEqual(
            [item["candidate_id"] for item in select_round_targets(candidates, round_index=1)],
            ["verified", "low-risk-unknown", "needs-evidence", "high-risk", "disputed"],
        )
        candidates[0]["decision"] = "verified"
        self.assertEqual(
            [item["candidate_id"] for item in select_round_targets(candidates, round_index=2)],
            ["needs-evidence", "high-risk", "disputed"],
        )

    def test_round_three_targets_only_changed_high_risk_candidates(self) -> None:
        candidates = [
            {
                "candidate_id": "changed-high-risk",
                "evidence_state": "supported",
                "evidence_level": "suspicion",
                "axes": {"real": "yes", "triggerable": "yes", "impactful": "unknown"},
                "severity_raw": "must_fix",
                "category_raw": "bug",
            },
            {
                "candidate_id": "unchanged-high-risk",
                "evidence_state": "supported",
                "evidence_level": "suspicion",
                "axes": {"real": "yes", "triggerable": "yes", "impactful": "unknown"},
                "severity_raw": "must_fix",
                "category_raw": "security",
            },
            {
                "candidate_id": "changed-low-risk",
                "evidence_state": "needs_evidence",
                "evidence_level": "suspicion",
                "axes": {"real": "yes", "triggerable": "unknown", "impactful": "unknown"},
                "severity_raw": "nit",
                "category_raw": "code_quality",
            },
        ]
        selected = select_round_targets(
            candidates,
            round_index=3,
            changed_candidate_ids={"changed-high-risk", "changed-low-risk"},
        )
        self.assertEqual(
            [item["candidate_id"] for item in selected],
            ["changed-high-risk"],
        )

    def test_plan_next_round_uses_previous_round_changed_ids_for_round_three(self) -> None:
        candidates = [
            {
                "candidate_id": "changed-high-risk",
                "evidence_state": "supported",
                "evidence_level": "suspicion",
                "axes": {"real": "yes", "triggerable": "yes", "impactful": "unknown"},
                "severity_raw": "must_fix",
                "category_raw": "bug",
            },
            {
                "candidate_id": "unchanged-high-risk",
                "evidence_state": "supported",
                "evidence_level": "suspicion",
                "axes": {"real": "yes", "triggerable": "yes", "impactful": "unknown"},
                "severity_raw": "must_fix",
                "category_raw": "security",
            },
        ]
        previous_candidates = copy.deepcopy(candidates)
        previous_candidates[0]["axes"]["triggerable"] = "unknown"
        plan = plan_next_round(
            HaltingPolicy(
                max_rounds=3,
                time_budget_ms=1_000,
                no_new_evidence_rounds=1,
                repeated_contradiction_limit=2,
            ),
            rounds=[
                {
                    "new_evidence_count": 1,
                    "state_digest_before": "a" * 64,
                    "state_digest_after": "b" * 64,
                },
                {
                    "new_evidence_count": 1,
                    "state_digest_before": candidate_state_digest(previous_candidates),
                },
            ],
            candidates=candidates,
            previous_candidates=previous_candidates,
            elapsed_ms=100,
        )
        self.assertTrue(plan["should_run"])
        self.assertEqual(plan["round_index"], 3)
        self.assertEqual(plan["target_candidate_ids"], ["changed-high-risk"])
        self.assertEqual(
            plan["round_state"]["changed_candidate_ids"],
            ["changed-high-risk", "unchanged-high-risk"],
        )
        self.assertEqual(plan["round_state"]["changed_candidate_count"], 2)
        self.assertEqual(plan["round_state"]["evidence_added_count"], 1)
        self.assertEqual(plan["round_state"]["disposition_changed_count"], 1)
        self.assertEqual(
            plan["round_state"]["untargeted_candidate_ids"],
            ["unchanged-high-risk"],
        )

    def test_auto_deep_requires_small_verified_conflict_free_candidates(self) -> None:
        verified = [
            {
                "candidate_id": "verified",
                "evidence_state": "supported",
                "evidence_level": "verified",
                "axes": {"real": "yes", "triggerable": "yes", "impactful": "yes"},
                "location": {"path": "src/app.py", "start_line": 10, "side": "RIGHT"},
                "severity": "must_fix",
            }
        ]
        small = {
            "recommended_mode": "standard",
            "depth_actual": "standard",
            "depth_source": "default",
            "routing_decision": {"budget_class": "small"},
        }
        medium = copy.deepcopy(small)
        medium["routing_decision"]["budget_class"] = "medium"
        self.assertTrue(auto_deep_eligible(verified, small))
        self.assertFalse(auto_deep_eligible(verified, medium))

        unknown = copy.deepcopy(verified)
        unknown[0]["axes"]["impactful"] = "unknown"
        self.assertFalse(auto_deep_eligible(unknown, small))

        disputed = copy.deepcopy(verified)
        disputed[0]["severity_disputed"] = True
        self.assertFalse(auto_deep_eligible(disputed, small))

        decision_only = [{"candidate_id": "claimed", "decision": "verified"}]
        self.assertFalse(auto_deep_eligible(decision_only, small))

        focused = copy.deepcopy(small)
        focused["recommended_mode"] = "focused"
        self.assertFalse(auto_deep_eligible(verified, focused))

    def test_apply_auto_deep_updates_run_plan_consistently(self) -> None:
        run_plan = {
            "files_changed": 2,
            "lines_added": 7,
            "lines_removed": 2,
            "risk_tags": ["dependency"],
            "recommended_mode": "standard",
            "depth_actual": "standard",
            "depth_source": "default",
            "depth_reason": "initial standard",
            "depth_requested": None,
            "depth_downgraded": False,
            "depth_downgrade_reason": None,
            "routing_decision": {
                "budget_class": "small",
                "route": "claude+codex",
                "model_profile": "standard",
                "rationale": "old",
            },
            "review_loop": {
                "halting_policy": {
                    "max_rounds": 2,
                },
            },
        }
        updated = apply_auto_deep(
            run_plan,
            {"round_index": 1, "auto_deep_eligible": True},
        )
        self.assertEqual(updated["depth_actual"], "deep")
        self.assertEqual(updated["depth_source"], "auto")
        self.assertEqual(
            updated["depth_reason"],
            "automatic deep after initial candidate gate: small fully resolved candidate set",
        )
        self.assertEqual(updated["routing_decision"]["model_profile"], "deep")
        self.assertEqual(
            updated["routing_decision"]["rationale"],
            "files_changed=2, total_lines=9, risk_tags=[dependency], depth=deep, mode=standard",
        )
        self.assertEqual(
            updated["review_loop"]["halting_policy"]["max_rounds"],
            3,
        )
        self.assertEqual(
            run_plan["review_loop"]["halting_policy"]["max_rounds"],
            2,
        )
        self.assertEqual(run_plan["depth_actual"], "standard")

    def test_apply_auto_deep_requires_initial_standard_state(self) -> None:
        run_plan = {
            "files_changed": 2,
            "lines_added": 7,
            "lines_removed": 2,
            "risk_tags": ["dependency"],
            "recommended_mode": "standard",
            "depth_actual": "deep",
            "depth_source": "auto",
            "routing_decision": {
                "budget_class": "small",
                "route": "claude+codex",
                "model_profile": "deep",
                "rationale": "already deep",
            },
        }
        with self.assertRaisesRegex(ValueError, "initial standard run plan"):
            apply_auto_deep(
                run_plan,
                {"round_index": 1, "auto_deep_eligible": True},
            )

    def test_controller_does_not_classify_unselected_unresolved_candidates_as_verified(self) -> None:
        unresolved_low_risk = [
            {
                "candidate_id": "unresolved-low-risk",
                "evidence_state": "supported",
                "evidence_level": "suspicion",
                "axes": {
                    "real": "yes",
                    "triggerable": "unknown",
                    "impactful": "unknown",
                },
                "severity_raw": "nit",
                "category_raw": "code_quality",
            }
        ]
        plan = plan_next_round(
            policy(),
            rounds=[{"new_evidence_count": 1}],
            candidates=unresolved_low_risk,
            elapsed_ms=100,
        )
        self.assertFalse(plan["should_run"])
        self.assertEqual(plan["halting"]["reason"], "no_active_candidates")
        self.assertEqual(plan["target_candidate_ids"], [])
        self.assertEqual(plan["round_state"]["untargeted_candidate_count"], 1)
        self.assertEqual(plan["round_state"]["remaining_active_count"], 0)

    def test_priority_narrowing_returns_host_owned_suppression_updates(self) -> None:
        previous = [
            {
                "candidate_id": "unresolved-low-risk",
                "evidence_state": "supported",
                "evidence_level": "suspicion",
                "axes": {
                    "real": "yes",
                    "triggerable": "unknown",
                    "impactful": "unknown",
                },
                "severity_raw": "nit",
                "category_raw": "code_quality",
            }
        ]
        digest = candidate_state_digest(previous)

        plan = plan_next_round(
            policy(),
            rounds=[
                {
                    "new_evidence_count": 0,
                    "state_digest_before": digest,
                }
            ],
            candidates=copy.deepcopy(previous),
            previous_candidates=previous,
            elapsed_ms=100,
        )

        self.assertFalse(plan["should_run"])
        self.assertEqual(plan["halting"]["reason"], "no_active_candidates")
        self.assertEqual(plan["target_candidate_ids"], [])
        self.assertEqual(
            plan["candidate_updates"],
            [
                {
                    "candidate_id": "unresolved-low-risk",
                    "decision": "suppressed",
                    "posting": {
                        "post_policy": "local_only",
                        "explanation_postable": False,
                        "not_postable_reason": "low_evidence_suspicion",
                        "audience": "human_reviewer",
                    },
                }
            ],
        )
        self.assertEqual(
            plan["round_state"]["untargeted_candidate_ids"],
            ["unresolved-low-risk"],
        )
        self.assertEqual(plan["round_state"]["untargeted_candidate_count"], 1)
        self.assertEqual(
            plan["round_state"]["untargeted_candidate_ids"],
            ["unresolved-low-risk"],
        )
        self.assertEqual(
            plan["round_state"]["changed_candidate_ids"],
            ["unresolved-low-risk"],
        )
        self.assertEqual(plan["round_state"]["disposition_changed_count"], 1)
        self.assertEqual(plan["round_state"]["remaining_active_count"], 0)
        self.assertNotEqual(
            plan["round_state"]["state_digest_before"],
            plan["round_state"]["state_digest_after"],
        )

    def test_priority_narrowing_overrides_unchanged_state_halt(self) -> None:
        unresolved_low_risk = [
            {
                "candidate_id": "unresolved-low-risk",
                "evidence_state": "supported",
                "evidence_level": "suspicion",
                "axes": {
                    "real": "yes",
                    "triggerable": "unknown",
                    "impactful": "unknown",
                },
                "severity_raw": "nit",
                "category_raw": "code_quality",
            }
        ]
        digest = candidate_state_digest(unresolved_low_risk)
        plan = plan_next_round(
            policy(),
            rounds=[
                {
                    "new_evidence_count": 0,
                    "state_digest_before": digest,
                    "state_digest_after": digest,
                    "changed_candidate_ids": [],
                    "changed_candidate_count": 0,
                    "evidence_added_count": 0,
                    "disposition_changed_count": 0,
                    "remaining_active_count": 1,
                }
            ],
            candidates=unresolved_low_risk,
            elapsed_ms=100,
        )

        self.assertFalse(plan["should_run"])
        self.assertEqual(plan["halting"]["reason"], "no_active_candidates")
        self.assertEqual(plan["round_state"]["untargeted_candidate_count"], 1)
        self.assertEqual(plan["round_state"]["remaining_active_count"], 0)

    def test_review_rounds_records_untargeted_candidates_as_inactive(self) -> None:
        artifact = build_review_rounds_artifact(
            policy=policy(),
            rounds=[
                {
                    "round_index": 1,
                    "actions": ["refine", "challenge", "verify"],
                    "input_candidates_count": 1,
                    "output_candidates_count": 0,
                    "new_evidence_count": 1,
                    "verifier_pass_count": 0,
                    "verifier_fail_count": 0,
                    "insufficient_evidence_count": 0,
                    "contradiction_signatures": [],
                    "rejected_candidates": [],
                    "target_candidate_ids": ["unresolved-low-risk"],
                    "state_digest_before": "a" * 64,
                    "state_digest_after": "b" * 64,
                    "changed_candidate_ids": ["unresolved-low-risk"],
                    "changed_candidate_count": 1,
                    "evidence_added_count": 1,
                    "disposition_changed_count": 0,
                    "untargeted_candidate_ids": ["unresolved-low-risk"],
                    "untargeted_candidate_count": 1,
                    "remaining_active_count": 0,
                }
            ],
            elapsed_ms=100,
            active_candidates_count=0,
        )
        self.assertEqual(artifact["halting"]["reason"], "no_active_candidates")
        self.assertEqual(artifact["metrics"]["suppressed_candidate_count"], 1)
        self.assertEqual(
            artifact["rounds"][0]["untargeted_candidate_ids"],
            ["unresolved-low-risk"],
        )
        schema = json.loads(ROUND_SCHEMA.read_text(encoding="utf-8"))
        self.assertEqual(
            validate_review_rounds_artifact(artifact, schema),
            [],
        )

    def test_controller_requires_explicit_verifier_decision(self) -> None:
        candidate = {
            "candidate_id": "hunter-supported",
            "evidence_state": "supported",
            "evidence_level": "verified",
            "axes": {
                "real": "yes",
                "triggerable": "yes",
                "impactful": "yes",
            },
        }

        plan = plan_next_round(
            policy(),
            rounds=[],
            candidates=[candidate],
            elapsed_ms=100,
        )

        self.assertTrue(plan["should_run"])
        self.assertEqual(plan["target_candidate_ids"], ["hunter-supported"])

    def test_controller_halts_when_no_unresolved_target_remains(self) -> None:
        verified = [
            {
                "candidate_id": "verified",
                "decision": "verified",
                "evidence_state": "supported",
                "evidence_level": "verified",
                "axes": {"real": "yes", "triggerable": "yes", "impactful": "yes"},
            }
        ]
        plan = plan_next_round(
            policy(),
            rounds=[{"new_evidence_count": 1}],
            candidates=verified,
            elapsed_ms=100,
        )
        self.assertFalse(plan["should_run"])
        self.assertEqual(plan["halting"]["reason"], "all_candidates_verified")
        self.assertEqual(plan["target_candidate_ids"], [])
    def test_controller_cli_emits_only_next_round_targets(self) -> None:
        candidates = {
            "candidates": [
                {
                    "candidate_id": "verified",
                    "evidence_state": "supported",
                    "evidence_level": "verified",
                    "decision": "verified",
                    "axes": {"real": "yes", "triggerable": "yes", "impactful": "yes"},
                },
                {
                    "candidate_id": "needs-evidence",
                    "evidence_state": "needs_evidence",
                    "evidence_level": "suspicion",
                    "axes": {"real": "yes", "triggerable": "unknown", "impactful": "unknown"},
                },
            ]
        }
        rounds = {"rounds": [{"new_evidence_count": 1}]}
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            candidates_path = directory / "candidates.json"
            rounds_path = directory / "rounds.json"
            candidates_path.write_text(json.dumps(candidates), encoding="utf-8")
            rounds_path.write_text(json.dumps(rounds), encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(CONTROLLER_PATH),
                    "--plan-next",
                    "--candidates",
                    str(candidates_path),
                    "--rounds",
                    str(rounds_path),
                    "--elapsed-ms",
                    "100",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        plan = json.loads(completed.stdout)
        self.assertTrue(plan["should_run"])
        self.assertEqual(plan["round_index"], 2)
        self.assertEqual(plan["target_candidate_ids"], ["needs-evidence"])


    def test_unchanged_state_digest_halts_even_if_model_claims_new_evidence(self) -> None:
        digest = "a" * 64
        decision = evaluate_halting(
            policy(),
            [
                {
                    "new_evidence_count": 1,
                    "state_digest_before": digest,
                    "state_digest_after": digest,
                }
            ],
            elapsed_ms=100,
            active_candidates_count=1,
        )
        self.assertTrue(decision.should_halt)
        self.assertEqual(decision.reason, "no_new_evidence")

    def test_controller_compares_current_state_to_round_start_digest(self) -> None:
        unresolved = [
            {
                "candidate_id": "needs-evidence",
                "evidence_state": "needs_evidence",
                "evidence_level": "suspicion",
                "axes": {"real": "yes", "triggerable": "unknown", "impactful": "unknown"},
            }
        ]
        digest = candidate_state_digest(unresolved)
        plan = plan_next_round(
            policy(),
            rounds=[
                {
                    "new_evidence_count": 1,
                    "state_digest_before": digest,
                }
            ],
            candidates=unresolved,
            elapsed_ms=100,
        )
        self.assertFalse(plan["should_run"])
        self.assertEqual(plan["halting"]["reason"], "no_new_evidence")
        self.assertEqual(
            plan["candidate_updates"][0]["decision"],
            "suppressed",
        )
        self.assertEqual(plan["round_state"]["remaining_active_count"], 0)

    def test_controller_ignores_model_supplied_after_digest(self) -> None:
        unresolved = [
            {
                "candidate_id": "needs-evidence",
                "evidence_state": "needs_evidence",
                "evidence_level": "suspicion",
                "axes": {"real": "yes", "triggerable": "unknown", "impactful": "unknown"},
            }
        ]
        digest = candidate_state_digest(unresolved)
        plan = plan_next_round(
            policy(),
            rounds=[
                {
                    "new_evidence_count": 1,
                    "state_digest_before": digest,
                    "state_digest_after": "f" * 64,
                }
            ],
            candidates=unresolved,
            elapsed_ms=100,
        )
        self.assertFalse(plan["should_run"])
        self.assertEqual(plan["halting"]["reason"], "no_new_evidence")
        self.assertNotEqual(plan["state_digest"], digest)
        self.assertNotEqual(plan["state_digest"], "f" * 64)
        self.assertEqual(
            plan["candidate_updates"][0]["posting"]["not_postable_reason"],
            "low_evidence_suspicion",
        )

    def test_every_terminal_policy_halt_suppresses_remaining_active_candidates(
        self,
    ) -> None:
        unresolved = [
            {
                "candidate_id": "needs-evidence",
                "evidence_state": "needs_evidence",
                "evidence_level": "suspicion",
                "severity_raw": "must_fix",
                "category_raw": "bug",
                "axes": {
                    "real": "yes",
                    "triggerable": "unknown",
                    "impactful": "unknown",
                },
            }
        ]
        digest = candidate_state_digest(unresolved)
        default_policy = {
            "max_rounds": 3,
            "time_budget_ms": 1_000,
            "no_new_evidence_rounds": 1,
            "repeated_contradiction_limit": 2,
        }
        cases = {
            "max_rounds": (
                {**default_policy, "max_rounds": 1},
                [{"new_evidence_count": 1, "state_digest_before": digest}],
                100,
            ),
            "time_budget": (
                {**default_policy, "time_budget_ms": 100},
                [{"new_evidence_count": 1, "state_digest_before": digest}],
                100,
            ),
            "no_new_evidence": (
                default_policy,
                [{"new_evidence_count": 1, "state_digest_before": digest}],
                100,
            ),
            "repeated_contradiction": (
                default_policy,
                [
                    {
                        "new_evidence_count": 1,
                        "contradiction_signatures": ["same-candidate"],
                    },
                    {
                        "new_evidence_count": 1,
                        "contradiction_signatures": ["same-candidate"],
                        "state_digest_before": digest,
                    },
                ],
                100,
            ),
        }
        expected_update = {
            "candidate_id": "needs-evidence",
            "decision": "suppressed",
            "posting": {
                "post_policy": "local_only",
                "explanation_postable": False,
                "not_postable_reason": "low_evidence_suspicion",
                "audience": "human_reviewer",
            },
        }

        for expected_reason, (
            case_policy,
            rounds,
            elapsed_ms,
        ) in cases.items():
            with self.subTest(expected_reason=expected_reason):
                plan = plan_next_round(
                    case_policy,
                    rounds=rounds,
                    candidates=unresolved,
                    previous_candidates=copy.deepcopy(unresolved),
                    elapsed_ms=elapsed_ms,
                )

                self.assertFalse(plan["should_run"])
                self.assertEqual(plan["halting"]["reason"], expected_reason)
                self.assertEqual(plan["target_candidate_ids"], [])
                self.assertEqual(
                    plan["round_state"]["untargeted_candidate_ids"],
                    ["needs-evidence"],
                )
                self.assertEqual(plan["round_state"]["remaining_active_count"], 0)
                self.assertEqual(plan["candidate_updates"], [expected_update])

    def test_no_new_halt_reason_survives_terminal_suppression_artifact(
        self,
    ) -> None:
        unresolved = [
            {
                "candidate_id": "needs-evidence",
                "evidence_state": "needs_evidence",
                "evidence_level": "suspicion",
                "severity_raw": "must_fix",
                "category_raw": "bug",
                "axes": {
                    "real": "yes",
                    "triggerable": "unknown",
                    "impactful": "unknown",
                },
            }
        ]
        digest = candidate_state_digest(unresolved)
        plan = plan_next_round(
            policy(),
            rounds=[
                {
                    "new_evidence_count": 1,
                    "state_digest_before": digest,
                }
            ],
            candidates=unresolved,
            previous_candidates=copy.deepcopy(unresolved),
            elapsed_ms=100,
        )
        round_result = {
            "actions": ["refine", "challenge", "verify"],
            "input_candidates_count": 1,
            "output_candidates_count": 0,
            "new_evidence_count": 0,
            "verifier_pass_count": 0,
            "verifier_fail_count": 0,
            "insufficient_evidence_count": 0,
            "contradiction_signatures": [],
            "rejected_candidates": [],
            "target_candidate_ids": ["needs-evidence"],
            **plan["round_state"],
        }

        artifact = build_review_rounds_artifact(
            policy=policy(),
            rounds=[round_result],
            elapsed_ms=100,
            active_candidates_count=0,
        )

        self.assertEqual(plan["halting"]["reason"], "no_new_evidence")
        self.assertEqual(
            plan["round_state"]["halt_basis_state_digest_after"],
            digest,
        )
        self.assertNotEqual(plan["round_state"]["state_digest_after"], digest)
        self.assertEqual(artifact["halting"]["reason"], "no_new_evidence")
        schema = json.loads(ROUND_SCHEMA.read_text(encoding="utf-8"))
        self.assertEqual(validate_review_rounds_artifact(artifact, schema), [])

    def test_round_artifact_records_controller_targets_and_state_digest(self) -> None:
        digest = "b" * 64
        artifact = build_review_rounds_artifact(
            policy=policy(),
            rounds=[
                {
                    "input_candidates_count": 1,
                    "output_candidates_count": 1,
                    "new_evidence_count": 1,
                    "verifier_pass_count": 0,
                    "verifier_fail_count": 0,
                    "insufficient_evidence_count": 0,
                    "target_candidate_ids": ["candidate-1"],
                    "state_digest_before": digest,
                    "state_digest_after": digest,
                    "changed_candidate_ids": [],
                    "changed_candidate_count": 0,
                    "evidence_added_count": 0,
                    "disposition_changed_count": 0,
                    "remaining_active_count": 1,
                    "untargeted_candidate_ids": [],
                    "untargeted_candidate_count": 0,
                }
            ],
            elapsed_ms=100,
            active_candidates_count=1,
        )
        round_result = artifact["rounds"][0]
        self.assertEqual(round_result["target_candidate_ids"], ["candidate-1"])
        self.assertEqual(round_result["state_digest_before"], digest)
        self.assertEqual(round_result["state_digest_after"], digest)
        self.assertEqual(round_result["changed_candidate_ids"], [])
        self.assertEqual(round_result["changed_candidate_count"], 0)
        self.assertEqual(round_result["evidence_added_count"], 0)
        self.assertEqual(round_result["disposition_changed_count"], 0)
        self.assertEqual(round_result["remaining_active_count"], 1)
        self.assertEqual(round_result["untargeted_candidate_count"], 0)
        self.assertEqual(round_result["untargeted_candidate_ids"], [])
        self.assertEqual(artifact["halting"]["reason"], "no_new_evidence")
        self.assertEqual(artifact["metrics"]["changed_candidate_count"], 0)
        self.assertEqual(artifact["metrics"]["evidence_added_count"], 0)
        self.assertEqual(artifact["metrics"]["disposition_changed_count"], 0)
        self.assertEqual(artifact["metrics"]["remaining_active_count"], 1)

        schema = json.loads(ROUND_SCHEMA.read_text(encoding="utf-8"))
        self.assertEqual(validate_review_rounds_artifact(artifact, schema), [])


    def test_schema_subset_enforces_numeric_maximum(self) -> None:
        schema = {"type": "number", "minimum": 0, "maximum": 1}

        self.assertEqual(
            validate_json_schema_subset(1, schema, path="$.completion_rate"),
            [],
        )
        errors = validate_json_schema_subset(
            1.01,
            schema,
            path="$.completion_rate",
        )
        self.assertTrue(
            any("$.completion_rate: expected <= 1" in error for error in errors),
            errors,
        )

    def test_round_artifact_rejects_inconsistent_host_state_metrics(self) -> None:
        artifact = self.valid_review_rounds_artifact()
        artifact["rounds"][0].update(
            {
                "changed_candidate_ids": ["candidate-1"],
                "changed_candidate_count": 2,
                "evidence_added_count": 1,
                "disposition_changed_count": 0,
                "remaining_active_count": 0,
                "untargeted_candidate_ids": ["candidate-1"],
            }
        )
        errors = validate_review_rounds_artifact(artifact)
        self.assertTrue(
            any("changed_candidate_count" in error for error in errors),
            errors,
        )
        self.assertTrue(
            any("remaining_active_count" in error for error in errors),
            errors,
        )
        self.assertTrue(
            any("untargeted_candidate_count" in error for error in errors),
            errors,
        )

    def test_round_artifact_requires_host_state_on_every_round(self) -> None:
        artifact = self.valid_review_rounds_artifact()
        for key in (
            "target_candidate_ids",
            "state_digest_before",
            "state_digest_after",
            "changed_candidate_ids",
            "changed_candidate_count",
            "evidence_added_count",
            "disposition_changed_count",
            "untargeted_candidate_count",
            "untargeted_candidate_ids",
            "remaining_active_count",
        ):
            artifact["rounds"][0].pop(key, None)

        errors = validate_review_rounds_artifact(artifact)

        self.assertTrue(
            any("missing required host state fields" in error for error in errors),
            errors,
        )

    def test_repeated_contradiction_is_deterministic_oscillation_guard(self) -> None:
        rounds = [
            {"new_evidence_count": 1, "contradiction_signatures": ["same-finding"]},
            {"new_evidence_count": 1, "contradiction_signatures": [{"signature": "SAME-FINDING"}]},
        ]
        oscillation_policy = HaltingPolicy(
            max_rounds=3,
            time_budget_ms=1_000,
            no_new_evidence_rounds=1,
            repeated_contradiction_limit=2,
        )
        decision = evaluate_halting(oscillation_policy, rounds, elapsed_ms=100, active_candidates_count=1)
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

    def test_non_bearer_authorization_header_values_are_redacted_and_rejected(self) -> None:
        raw_candidate = {
            "finding_id": "auth-header-candidate",
            "title": "Non-Bearer authorization header",
            "reason": "verifier_fail",
            "detail": "Authorization: Token placeholder-credential-value",
        }
        sanitized = sanitize_local_candidate(raw_candidate)
        self.assertEqual(sanitized["detail"], REDACTED_SENSITIVE_VALUE)

        artifact = self.valid_review_rounds_artifact()
        artifact["rounds"][0]["rejected_candidates"][0]["detail"] = "Authorization: Token placeholder-credential-value"
        self.assert_review_rounds_cli_invalid(artifact, "sensitive/raw value is not allowed")

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
        self.assertTrue(sanitized["finding_id"].startswith(REDACTED_IDENTIFIER_PREFIX))
        self.assertTrue(sanitized["fingerprint"].startswith(REDACTED_IDENTIFIER_PREFIX))
        self.assertNotEqual(sanitized["finding_id"], sanitized["fingerprint"])
        self.assertEqual(sanitized["title"], REDACTED_SENSITIVE_VALUE)
        self.assertEqual(sanitized["path"], REDACTED_SENSITIVE_VALUE)
        self.assertEqual(sanitized["detail"], REDACTED_SENSITIVE_VALUE)

        artifact = build_review_rounds_artifact(
            policy=policy(),
            rounds=[
                {
                    "input_candidates_count": 1,
                    "new_evidence_count": 1,
                    "verifier_fail_count": 1,
                    "rejected_candidates": [raw_candidate],
                    "target_candidate_ids": ["candidate-1"],
                    "state_digest_before": "a" * 64,
                    "state_digest_after": "b" * 64,
                    "changed_candidate_ids": ["candidate-1"],
                    "changed_candidate_count": 1,
                    "evidence_added_count": 1,
                    "disposition_changed_count": 1,
                    "untargeted_candidate_count": 0,
                    "remaining_active_count": 0,
                }
            ],
            elapsed_ms=100,
            active_candidates_count=0,
            generated_at="2026-05-06T00:00:00Z",
        )
        rejected = artifact["rounds"][0]["rejected_candidates"][0]
        self.assertEqual(rejected["finding_id"], sanitized["finding_id"])
        self.assertEqual(rejected["fingerprint"], sanitized["fingerprint"])
        self.assertEqual(rejected["title"], REDACTED_SENSITIVE_VALUE)
        self.assertEqual(rejected["path"], REDACTED_SENSITIVE_VALUE)
        self.assertEqual(rejected["detail"], REDACTED_SENSITIVE_VALUE)
        self.assertNotIn("abc123", json.dumps(artifact))
        self.assertEqual(validate_review_rounds_artifact(artifact), [])

    def test_sensitive_identifier_surrogates_still_suppress_posting(self) -> None:
        artifact = build_review_rounds_artifact(
            policy=policy(),
            rounds=[
                {
                    "new_evidence_count": 1,
                    "verifier_fail_count": 1,
                    "rejected_candidates": [
                        {
                            "finding_id": "SECRET_TOKEN=abc123",
                            "fingerprint": "OPENAI_API_KEY=abc123",
                            "title": "Sensitive identifier candidate",
                            "reason": "verifier_fail",
                        }
                    ],
                }
            ],
            elapsed_ms=100,
            active_candidates_count=1,
            generated_at="2026-05-06T00:00:00Z",
        )
        rejected = artifact["rounds"][0]["rejected_candidates"][0]
        self.assertTrue(rejected["finding_id"].startswith(REDACTED_IDENTIFIER_PREFIX))
        self.assertTrue(rejected["fingerprint"].startswith(REDACTED_IDENTIFIER_PREFIX))
        findings = [
            {"id": "SECRET_TOKEN=abc123", "fingerprint": "unrelated", "title": "must stay local by id"},
            {"id": "passing-id", "fingerprint": "OPENAI_API_KEY=abc123", "title": "must stay local by fingerprint"},
            {"id": "passing-id", "fingerprint": "passing-fingerprint", "title": "can post"},
        ]
        self.assertEqual([item["title"] for item in filter_postable_findings(findings, artifact)], ["can post"])

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
                    "input_candidates_count": 1,
                    "output_candidates_count": 0,
                    "new_evidence_count": 1,
                    "contradiction_signatures": [{"path": "src/app.ts", "title": "Some issue"}],
                    "target_candidate_ids": ["candidate-1"],
                    "state_digest_before": "a" * 64,
                    "state_digest_after": "b" * 64,
                    "changed_candidate_ids": ["candidate-1"],
                    "changed_candidate_count": 1,
                    "evidence_added_count": 1,
                    "disposition_changed_count": 1,
                    "untargeted_candidate_count": 0,
                    "remaining_active_count": 0,
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

    def test_review_rounds_validator_rejects_placeholder_redacted_identifiers(self) -> None:
        for key in ("finding_id", "fingerprint"):
            with self.subTest(key=key):
                artifact = self.valid_review_rounds_artifact()
                artifact["rounds"][0]["rejected_candidates"][0][key] = REDACTED_SENSITIVE_VALUE
                self.assert_review_rounds_cli_invalid(artifact, "redacted identifier must use a stable surrogate")

    def test_docs_and_schemas_expose_round_metrics_for_f11(self) -> None:
        readme = (ROOT / "fixtures" / "README.md").read_text(encoding="utf-8")
        review_skill = (ROOT / "skills" / "review" / "SKILL.md").read_text(encoding="utf-8")
        root_readme = (ROOT / "README.md").read_text(encoding="utf-8")
        eval_schema = json.loads(EVAL_SCHEMA.read_text(encoding="utf-8"))
        eval_example = json.loads((ROOT / "fixtures" / "eval-report.example.json").read_text(encoding="utf-8"))
        positive_eval = json.loads(
            (ROOT / "fixtures" / "positive" / "eval-report.json").read_text(encoding="utf-8")
        )
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
        self.assertIn("execution", eval_example["aggregate"]["iterative"])
        self.assertEqual(validate_json_schema_subset(eval_example, eval_schema), [])
        self.assertEqual(validate_json_schema_subset(positive_eval, eval_schema), [])


if __name__ == "__main__":
    unittest.main()
