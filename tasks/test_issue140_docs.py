#!/usr/bin/env python3
"""Executable documentation checks for Issue #140 body Should Fix summary."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEND_SKILL = ROOT / "skills" / "send" / "SKILL.md"
README = ROOT / "README.md"


class Issue140DocsTest(unittest.TestCase):
    def test_skill_documents_body_should_fix_summary_section(self) -> None:
        skill = SEND_SKILL.read_text(encoding="utf-8")
        for snippet in (
            "builder が body の `## 改善提案` セクションへ `<details><summary>詳細はこちら</summary><div>` の折りたたみ付き箇条書きとして含める（#140 の検証運用",
            "`--include-should-fix` 指定時の Should Fix は従来どおり inline comment とし、`## 改善提案` セクションは追加しない",
            "→ `## 改善提案`（`--include-should-fix` 未指定で postable な Should Fix がある場合。#140）→ `## 行コメント不可 (diff 範囲外)`（存在する場合）",
            "body `## 改善提案` 掲載一覧（`should_fix_summary`。#140）",
            "`payload-manifest.json` の `should_fix_summary` / `counts.should_fix_summary` に記録される（#140）",
        ):
            self.assertIn(snippet, skill)

    def test_readme_documents_default_should_fix_body_placement(self) -> None:
        readme = README.read_text(encoding="utf-8")
        self.assertIn(
            "未指定の場合、同条件の候補は inline にせず、レビュー body の `## 改善提案` セクション（`<details>` 折りたたみ）に記載する（#140 の検証運用）",
            readme,
        )


if __name__ == "__main__":
    unittest.main()
