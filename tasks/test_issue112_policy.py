#!/usr/bin/env python3
"""Regression tests for Issue #112 halting and reasoning-effort policy."""

from __future__ import annotations

import hashlib
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
            "candidate_updates[]",
            "state_digest_before",
            "state_digest_after",
            "halt_basis_state_digest_after",
            "--previous-candidates",
            "changed_candidate_ids",
            "changed_candidate_count",
            "evidence_added_count",
            "disposition_changed_count",
            "remaining_active_count",
            "untargeted_candidate_ids",
            "untargeted_candidate_count",
            "`time_budget_ms=0` は「round 1 を省略する」ではなく",
            "round 2 は未解決のうち high-risk",
            "round 3 は **round 2 で host-owned state が変化した high-risk 候補だけ**",
            "モデル自身に「続けるか」を判断させてはならない",
            'decision="verified"',
            'decision="refuted"',
            'decision="suppressed"',
            "最終 round より前に追加検証が必要な候補だけ",
            "最終 round では verifier が全対象を terminal state",
            "停止後に ACTIVE candidate を残してはならない",
            '採用できるのは `decision="verified"`',
            "validate_candidates.py",
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

    def test_aggregate_max_rounds_halt_requires_completed_rounds_at_cap(self) -> None:
        report = json.loads(POSITIVE_EVAL.read_text(encoding="utf-8"))

        for run_name in ("baseline", "iterative"):
            metrics = report["aggregate"][run_name]["round_metrics"]
            if metrics["halt_reason"] == "max_rounds":
                self.assertEqual(
                    metrics["rounds_completed"],
                    metrics["max_rounds"],
                    f"aggregate.{run_name} claims max_rounds before reaching the cap",
                )

    def test_positive_eval_provenance_is_complete_and_content_addressed(self) -> None:
        schema = json.loads(EVAL_SCHEMA.read_text(encoding="utf-8"))
        report = json.loads(POSITIVE_EVAL.read_text(encoding="utf-8"))
        self.assertEqual(validate_json_schema_subset(report, schema), [])
        self.assertNotIn("provenance", report["aggregate"]["baseline"])
        self.assertNotIn("provenance", report["aggregate"]["iterative"])

        run_ids: set[str] = set()
        eval_artifacts: set[Path] = set()
        artifact_fields = (
            "prompt_config",
            "fixture",
            "oracle",
            "findings_artifact",
            "score_report",
            "execution_manifest",
            "scorer",
        )
        for fixture in report["fixtures"]:
            for run_name in ("baseline", "iterative"):
                provenance = fixture[run_name]["provenance"]
                run_id = provenance["run_id"]
                self.assertNotIn(run_id, run_ids)
                run_ids.add(run_id)
                self.assertGreaterEqual(provenance["sample_count"], 1)
                self.assertGreaterEqual(provenance["repetitions"], 1)

                for field in artifact_fields:
                    reference = provenance[field]
                    relative_path = Path(reference["path"])
                    self.assertFalse(relative_path.is_absolute())
                    self.assertNotIn("..", relative_path.parts)
                    self.assertNotIn("\\", reference["path"])
                    artifact = ROOT / relative_path
                    self.assertTrue(artifact.is_file(), f"{field} is missing: {artifact}")
                    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
                    self.assertEqual(reference["sha256"], digest, f"{field} digest mismatch")
                    if relative_path.parts[:3] == ("fixtures", "positive", "eval-artifacts"):
                        eval_artifacts.add(relative_path)

                self.assertEqual(
                    provenance["scorer"]["revision"],
                    f"sha256:{provenance['scorer']['sha256']}",
                )
                score_report = json.loads(
                    (ROOT / provenance["score_report"]["path"]).read_text(encoding="utf-8")
                )
                self.assertEqual(provenance["evaluated_at"], score_report["evaluated_at"])
                for metric in (
                    "exact_pass_rate",
                    "acceptable_pass_rate",
                    "false_positive_rate",
                    "recall_known_bug",
                ):
                    self.assertEqual(fixture[run_name]["score_metrics"][metric], score_report[metric])
                execution_manifest = json.loads(
                    (ROOT / provenance["execution_manifest"]["path"]).read_text(encoding="utf-8")
                )
                self.assertEqual(
                    set(execution_manifest),
                    {
                        "schema_version",
                        "run_id",
                        "execution",
                        "sample_count",
                        "repetitions",
                        "evaluated_at",
                    },
                )
                self.assertEqual(execution_manifest["schema_version"], "eval-execution.v1")
                self.assertEqual(execution_manifest["run_id"], run_id)
                self.assertEqual(execution_manifest["execution"], fixture[run_name]["execution"])
                self.assertEqual(execution_manifest["sample_count"], provenance["sample_count"])
                self.assertEqual(execution_manifest["repetitions"], provenance["repetitions"])
                self.assertEqual(execution_manifest["evaluated_at"], provenance["evaluated_at"])

        self.assertEqual(len(run_ids), 2 * len(report["fixtures"]))
        checked_in_artifacts = {
            path.relative_to(ROOT)
            for path in (ROOT / "fixtures" / "positive" / "eval-artifacts").iterdir()
            if path.is_file()
        }
        self.assertEqual(checked_in_artifacts, eval_artifacts)


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
        self.assertEqual(
            effort["baseline"]["score_metrics"],
            effort["iterative"]["score_metrics"],
        )
        self.assertEqual(
            effort["baseline"]["provenance"]["prompt_config"]["sha256"],
            effort["iterative"]["provenance"]["prompt_config"]["sha256"],
        )
        self.assertEqual(
            effort["baseline"]["provenance"]["findings_artifact"]["sha256"],
            effort["iterative"]["provenance"]["findings_artifact"]["sha256"],
        )
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
        self.assertEqual(fable["iterative"]["score_metrics"]["acceptable_pass_rate"], 0.6667)
        self.assertGreater(
            fable["iterative"]["score_metrics"]["acceptable_pass_rate"],
            fable["baseline"]["score_metrics"]["acceptable_pass_rate"],
        )
        self.assertEqual(fable["iterative"]["score_metrics"]["false_positive_rate"], 0.0)


if __name__ == "__main__":
    unittest.main()
