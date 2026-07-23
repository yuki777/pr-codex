#!/usr/bin/env python3
"""Regression tests for Issue #112 halting and reasoning-effort policy."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "tasks"
sys.path.insert(0, str(TASKS))

from refinement_loop import HaltingPolicy, validate_json_schema_subset  # noqa: E402

REVIEW_SKILL = ROOT / "skills" / "review" / "SKILL.md"
SEND_SKILL = ROOT / "skills" / "send" / "SKILL.md"
EVAL_SCHEMA = ROOT / "schemas" / "eval-report.v1.json"
POSITIVE_EVAL = ROOT / "fixtures" / "positive" / "eval-report.json"
HUNTER_CRITERIA = ROOT / "skills" / "review" / "HUNTER_CRITERIA.md"
VERIFIER_POLICY = ROOT / "skills" / "review" / "VERIFIER_POLICY.md"
README = ROOT / "README.md"


class Issue112PolicyTest(unittest.TestCase):
    def test_default_round_cap_is_two(self) -> None:
        self.assertEqual(HaltingPolicy().max_rounds, 2)

    def test_review_policy_uses_adaptive_three_round_hard_cap(self) -> None:
        text = REVIEW_SKILL.read_text(encoding="utf-8")
        self.assertIn(
            'if depth_actual == "deep" or sensitive_risk_count > 0 then 3 else 2 end',
            text,
        )
        self.assertIn("hard cap 3", text)

    def test_review_skill_delegates_round_control_to_host_controller(self) -> None:
        text = REVIEW_SKILL.read_text(encoding="utf-8")
        step4c = text[text.index("#### 4c:") : text.index("### Step 5:")]
        for snippet in (
            "refinement_loop.py\" --plan-next",
            "refinement_loop.py\" --apply-auto-deep",
            "target_candidate_ids[]",
            "state_digest_before",
            "state_digest_after",
            "--previous-candidates",
            "changed_candidate_ids",
            "changed_candidate_count",
            "evidence_added_count",
            "disposition_changed_count",
            "remaining_active_count",
            "round 2 は未解決のうち high-risk",
            "round 3 は **round 2 で host-owned state が変化した high-risk 候補だけ**",
            "モデル自身に「続けるか」を判断させてはならない",
        ):
            self.assertIn(snippet, step4c)

    def test_general_axis_is_replaced_by_non_gating_blast_radius(self) -> None:
        skill = REVIEW_SKILL.read_text(encoding="utf-8")
        verifier = VERIFIER_POLICY.read_text(encoding="utf-8")
        hunter = HUNTER_CRITERIA.read_text(encoding="utf-8")
        step4c = skill[skill.index("#### 4c:") : skill.index("### Step 5:")]
        hunter_prompts = skill[skill.index("#### 4a:") : skill.index("#### 4c:")]

        self.assertIn("## 3軸ゲート", verifier)
        self.assertIn("blast_radius", verifier)
        self.assertIn("Must Fix gate には使わない", verifier)
        self.assertNotIn("GENERAL", verifier)
        self.assertNotIn("axes.general", step4c)
        self.assertNotIn("GENERAL=yes", step4c)
        for field in (
            "evidence_state",
            "evidence_level_suggestion",
            "axes_suggestion",
            "blast_radius_suggestion",
        ):
            self.assertGreaterEqual(hunter_prompts.count(field), 2)
        self.assertIn("needs_evidence", hunter)
        self.assertIn("verified finding だけ", hunter)

    def test_positive_fixture_is_in_manual_m1_m2_gate(self) -> None:
        text = README.read_text(encoding="utf-8")
        self.assertIn("3 個の既知バグ", text)
        gate_command = text[
            text.index("python3 tasks/m1_m2_gate.py") :
            text.index("```", text.index("python3 tasks/m1_m2_gate.py"))
        ]
        self.assertIn("--out artifacts/score-positive.json", text)
        self.assertIn("artifacts/score-positive.json", gate_command)

    def test_semantic_preflight_uses_evaluated_high_effort(self) -> None:
        text = SEND_SKILL.read_text(encoding="utf-8")
        command = text[text.index("#### Codex semantic コマンド") : text.index("#### preflight-result")]
        self.assertIn("-m gpt-5.6-sol", command)
        self.assertIn("-c 'model_reasoning_effort=\"high\"'", command)
        self.assertLess(
            command.index("model_reasoning_effort"),
            command.index("  exec "),
        )

    def test_positive_eval_records_quality_preserving_reductions(self) -> None:
        schema = json.loads(EVAL_SCHEMA.read_text(encoding="utf-8"))
        report = json.loads(POSITIVE_EVAL.read_text(encoding="utf-8"))
        self.assertEqual(validate_json_schema_subset(report, schema), [])
        fixtures = {item["fixture_id"]: item for item in report["fixtures"]}

        rounds = fixtures["pr-codex-positive-seeded-001-round-policy"]
        self.assertEqual(rounds["baseline"]["score_metrics"], rounds["iterative"]["score_metrics"])
        self.assertLess(
            rounds["iterative"]["round_metrics"]["rounds_completed"],
            rounds["baseline"]["round_metrics"]["rounds_completed"],
        )
        self.assertLess(
            rounds["iterative"]["execution"]["avg_tokens"],
            rounds["baseline"]["execution"]["avg_tokens"],
        )
        for run_name in ("baseline", "iterative"):
            metrics = rounds[run_name]["round_metrics"]
            for field in (
                "changed_candidate_count",
                "evidence_added_count",
                "disposition_changed_count",
                "remaining_active_count",
            ):
                self.assertIn(field, metrics)

        effort = fixtures["pr-codex-positive-seeded-001-preflight-effort"]
        self.assertEqual(effort["baseline"]["score_metrics"], effort["iterative"]["score_metrics"])
        self.assertEqual(effort["baseline"]["execution"]["reasoning_effort"], "xhigh")
        self.assertEqual(effort["iterative"]["execution"]["reasoning_effort"], "high")
        self.assertLess(
            effort["iterative"]["execution"]["avg_duration_ms"],
            effort["baseline"]["execution"]["avg_duration_ms"],
        )
        self.assertLess(
            effort["iterative"]["execution"]["avg_tokens"],
            effort["baseline"]["execution"]["avg_tokens"],
        )

        fable = fixtures["pr-codex-positive-seeded-001-fable-prompt"]
        self.assertEqual(fable["baseline"]["execution"]["model"], "claude-fable-5")
        self.assertEqual(fable["iterative"]["score_metrics"]["acceptable_pass_rate"], 1.0)
        self.assertGreater(
            fable["iterative"]["score_metrics"]["acceptable_pass_rate"],
            fable["baseline"]["score_metrics"]["acceptable_pass_rate"],
        )
        self.assertEqual(fable["iterative"]["score_metrics"]["false_positive_rate"], 0.0)


if __name__ == "__main__":
    unittest.main()
