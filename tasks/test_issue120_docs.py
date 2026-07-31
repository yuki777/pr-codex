#!/usr/bin/env python3
"""Executable documentation checks for Issue #120 posted-summary generation."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEND_SKILL = ROOT / "skills" / "send" / "SKILL.md"
README = ROOT / "README.md"


class Issue120DocsTest(unittest.TestCase):
    def test_posted_summary_is_builder_generated_from_posted_findings_only(self) -> None:
        skill = SEND_SKILL.read_text(encoding="utf-8")
        for snippet in (
            "投稿 body 先頭の総評（`$posted_summary`）は builder が投稿対象の finding だけから決定論的に生成し、`review.md` の自由文総評は GitHub へ投稿しない",
            "`$posted_summary`（投稿用総評）: builder が投稿対象の finding と event だけから決定論的に生成する",
            "`review.md` の `## 総評` は転記しない",
            "件数は表示せず、投稿しない severity と `withheld` には一切言及しない（#120）",
        ):
            self.assertIn(snippet, skill)

    def test_posted_summary_shows_placement_without_counts(self) -> None:
        skill = SEND_SKILL.read_text(encoding="utf-8")
        for snippet in (
            "「Must Fix を検出しました。マージ前に修正が必要です。」とし、件数は表示しない（cluster 代表への集約により canonical 件数と投稿コメント数が一致しないため）",
            "severity ごとに投稿先（inline / 行コメント不可による本文末尾）を 1 行で付加する",
            "投稿 0 件の severity には言及しない",
        ):
            self.assertIn(snippet, skill)

    def test_withheld_only_summary_discloses_nothing(self) -> None:
        skill = SEND_SKILL.read_text(encoding="utf-8")
        self.assertIn(
            "event の言い換えだけの汎用文「このレビューは変更をリクエストします。」とし、withheld の存在・件数・カテゴリを新たに公開しない",
            skill,
        )

    def test_event_rule_counts_canonical_must_fix(self) -> None:
        skill = SEND_SKILL.read_text(encoding="utf-8")
        for snippet in (
            "canonical（`findings.verified.json`）の Must Fix が 1 件以上あれば `\"REQUEST_CHANGES\"`",
            "cluster 非代表 member / `withheld` を含む",
            "`event` は canonical（`findings.verified.json`）の Must Fix 件数（cluster 非代表 member / `withheld` を含む）で決める",
            "canonical Must Fix が1件以上（cluster 非代表 member / withheld / body 末尾へ退避した範囲外 Must Fix を含む）→ REQUEST_CHANGES",
        ):
            self.assertIn(snippet, skill)

    def test_review_md_summary_is_nonempty_gate_only(self) -> None:
        skill = SEND_SKILL.read_text(encoding="utf-8")
        for snippet in (
            "`## 総評` 直下の本文（後続セクション見出しの直前まで。前後の空行はトリム）は非空 gate にのみ使う（空なら中断。投稿 body へは転記しない）",
            "`review.md` の `## 総評` セクションが空 or 見つからない → ユーザーに通知して処理中断",
        ):
            self.assertIn(snippet, skill)

    def test_body_templates_use_posted_summary_placeholder(self) -> None:
        skill = SEND_SKILL.read_text(encoding="utf-8")
        self.assertIn("<$posted_summary>", skill)
        self.assertIn("body のセクション順は必ず `総評`（`$posted_summary`）", skill)
        self.assertNotIn("<$summary>", skill)

    def test_readme_documents_posted_summary_generation(self) -> None:
        readme = README.read_text(encoding="utf-8")
        self.assertIn(
            "投稿用総評の生成（投稿対象の finding と event だけから決定論的に生成し、件数は表示せず、投稿しない severity や非公開 finding には言及しない。#120）",
            readme,
        )


if __name__ == "__main__":
    unittest.main()
