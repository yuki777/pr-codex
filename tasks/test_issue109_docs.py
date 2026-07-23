#!/usr/bin/env python3
"""Executable documentation checks for Issue #109 asymmetric hunters and staged criteria."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REVIEW_SKILL = ROOT / "skills" / "review" / "SKILL.md"
HUNTER_CRITERIA = ROOT / "skills" / "review" / "HUNTER_CRITERIA.md"
VERIFIER_POLICY = ROOT / "skills" / "review" / "VERIFIER_POLICY.md"
EXPLAINER_POLICY = ROOT / "skills" / "review" / "EXPLAINER_POLICY.md"
LEGACY_CRITERIA = ROOT / "skills" / "review" / "REVIEW_CRITERIA.md"


def section(text: str, start: str, end: str) -> str:
    start_index = text.index(start)
    end_index = text.index(end, start_index)
    return text[start_index:end_index]


class Issue109DocsTest(unittest.TestCase):
    def test_criteria_is_split_per_stage_and_legacy_file_is_gone(self) -> None:
        self.assertTrue(HUNTER_CRITERIA.exists())
        self.assertTrue(VERIFIER_POLICY.exists())
        self.assertTrue(EXPLAINER_POLICY.exists())
        self.assertFalse(LEGACY_CRITERIA.exists())

        skill = REVIEW_SKILL.read_text(encoding="utf-8")
        self.assertIn("`HUNTER_CRITERIA.md` に外出ししている", skill)
        self.assertNotIn("REVIEW_CRITERIA.md", skill)

    def test_hunter_prompts_have_asymmetric_roles(self) -> None:
        text = REVIEW_SKILL.read_text(encoding="utf-8")
        claude_section = section(text, "#### 4a: Claude Code レビュー", "#### 4b:")
        codex_section = section(text, "#### 4b: Codex CLI レビュー", "#### 4c:")

        self.assertIn("目的と重点役割（Goal — Claude hunter）", claude_section)
        self.assertIn("missing change", claude_section)
        self.assertIn("architecture / UX / 運用への影響", claude_section)

        self.assertIn("目的と重点役割（Goal — Codex hunter）", codex_section)
        self.assertIn("caller / callee、データフロー、契約・schema の整合", codex_section)
        self.assertIn("test / config / migration / permission", codex_section)
        self.assertIn("反例探索", codex_section)

        for hunter_section in (claude_section, codex_section):
            self.assertIn("共通責務", hunter_section)

    def test_hunter_prompts_use_five_section_structure(self) -> None:
        text = REVIEW_SKILL.read_text(encoding="utf-8")
        for bounds in (("#### 4a: Claude Code レビュー", "#### 4b:"), ("#### 4b: Codex CLI レビュー", "#### 4c:")):
            hunter_section = section(text, *bounds)
            for heading in (
                "## 目的と重点役割（Goal",
                "## 信頼境界（Trusted vs untrusted）",
                "## 読み取り境界（Read boundaries — 分析範囲と投稿範囲の二層）",
                "## 出力 schema（Output schema — 必ず厳守）",
                "## 停止条件（Stop conditions）",
            ):
                self.assertIn(heading, hunter_section)
            self.assertIn("untrusted なレビュー対象データ", hunter_section)

    def test_two_layer_scope_is_documented(self) -> None:
        criteria = HUNTER_CRITERIA.read_text(encoding="utf-8")
        self.assertIn("## 分析範囲と投稿範囲（二層）", criteria)
        self.assertIn("caller / callee、関連する schema・config・migration・test まで読んで確認してよい", criteria)
        self.assertIn("この PR が導入した問題、またはこの PR が顕在化させた問題のみ", criteria)
        self.assertIn("RIGHT 側", criteria)

    def test_generic_design_findings_require_concrete_impact(self) -> None:
        criteria = HUNTER_CRITERIA.read_text(encoding="utf-8")
        self.assertIn("具体的な不具合・保守不能・運用リスクにつながる場合のみ", criteria)
        self.assertIn("原則名を根拠にした指摘", criteria)
        self.assertIn("純粋なスタイルの好みは candidates にしない", criteria)

    def test_agreement_is_priority_signal_not_evidence(self) -> None:
        verifier = VERIFIER_POLICY.read_text(encoding="utf-8")
        self.assertIn("## 二者一致の扱い", verifier)
        self.assertIn("独立した証拠として扱わない", verifier)
        self.assertIn("二者一致は含めない", verifier)

        criteria = HUNTER_CRITERIA.read_text(encoding="utf-8")
        self.assertIn("二者の同一指摘は独立した証拠にはならない", criteria)

        skill = REVIEW_SKILL.read_text(encoding="utf-8")
        self.assertIn("二者の同一指摘は独立した証拠として扱わず", skill)
        self.assertIn("二者一致だけを理由に `corroborated` 以上へ上げない", skill)

    def test_verifier_and_explainer_policies_are_wired_into_step4c(self) -> None:
        text = REVIEW_SKILL.read_text(encoding="utf-8")
        step4c = section(text, "#### 4c: レビュー結果の統合", "### Step 5:")
        self.assertIn("VERIFIER_POLICY.md", step4c)
        self.assertIn("EXPLAINER_POLICY.md", step4c)

        verifier = VERIFIER_POLICY.read_text(encoding="utf-8")
        for snippet in ("## 3軸ゲート", "エビデンスラダー", "## security extension", "## Root-cause clustering"):
            self.assertIn(snippet, verifier)

        explainer = EXPLAINER_POLICY.read_text(encoding="utf-8")
        for snippet in ("## review.md セクション構成", "## Should Fix inline comment 整形ルール", "## SARIF 派生成果物の公開境界"):
            self.assertIn(snippet, explainer)


if __name__ == "__main__":
    unittest.main()
