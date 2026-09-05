#!/usr/bin/env python3
"""Executable documentation checks for Issue #128 effort-free review footer."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEND_SKILL = ROOT / "skills" / "send" / "SKILL.md"
REVIEW_SKILL = ROOT / "skills" / "review" / "SKILL.md"
BUILDER = ROOT / "tasks" / "build_review_payload.py"
README = ROOT / "README.md"


class Issue128DocsTest(unittest.TestCase):
    def test_send_footer_template_has_no_effort(self) -> None:
        skill = SEND_SKILL.read_text(encoding="utf-8")
        for snippet in (
            "レビューは <name> <model> と <name> <model> により行われました。",
            "投稿前検証 (semantic preflight) は Codex gpt-6-astra により行われました。",
            "`effort` は記録のみでフッターには表示しない #128",
            "effort はどのフッター行にも表示しない。実行時の実効 effort は投稿時点で確定できないため、`review_engines[].effort` は記録の検証にだけ使い、表示は name と model に限定する（#128）",
        ):
            self.assertIn(snippet, skill)
        # 旧フッター形式（effort 付き）が残っていないこと。
        self.assertNotIn("<name> <model> (<effort>)", skill)
        self.assertNotIn("gpt-6-astra (high) により行われました", skill)

    def test_builder_renders_only_name_and_model(self) -> None:
        builder = BUILDER.read_text(encoding="utf-8")
        # フッターは name と model だけを描画する。
        self.assertIn("f\"{engine['name'].strip()} {engine['model'].strip()}\"", builder)
        self.assertIn('SEMANTIC_VERIFIER_ENGINE = ("Codex", "gpt-6-astra")', builder)
        # 表示専用の effort 正規化は撤去済み（記録の非空検証だけが残る）。
        self.assertNotIn("display_effort", builder)
        self.assertNotIn("EFFORT_DISPLAY_LABELS", builder)
        self.assertIn('for key in ("name", "model", "effort"):', builder)

    def test_review_skill_keeps_recording_but_hides_display(self) -> None:
        skill = REVIEW_SKILL.read_text(encoding="utf-8")
        for snippet in (
            # 実行構成の記録（#124）は維持する。
            "effort は両 hunter とも最大値に固定する",
            # 表示だけをやめる（#128）。
            "send の builder はフッターに effort を表示せず、記録の検証にだけ使う（フッターに表示するのは name と model のみ。#128）",
            "effort は記録のみで表示しない。#124, #128",
        ):
            self.assertIn(snippet, skill)

    def test_readme_documents_effort_hidden(self) -> None:
        readme = README.read_text(encoding="utf-8")
        self.assertIn("effort は確定できないため表示しない（#128）", readme)


if __name__ == "__main__":
    unittest.main()
