#!/usr/bin/env python3
"""Executable documentation checks for Issue #73 Bash timeout guidance."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REVIEW_SKILL = ROOT / "skills" / "review" / "SKILL.md"
README = ROOT / "README.md"


def section(text: str, start: str, end: str) -> str:
    start_index = text.index(start)
    end_index = text.index(end, start_index)
    return text[start_index:end_index]


class Issue73DocsTest(unittest.TestCase):
    def test_step_4_hunter_templates_document_background_timeout_semantics(self) -> None:
        text = REVIEW_SKILL.read_text(encoding="utf-8")
        step4_intro = section(text, "### Step 4: レビュー実行", "#### 4a:")
        for snippet in (
            "Claude Code Bash tool の foreground timeout 上限は `600000` ms",
            "`estimated_timeout_ms` / `review_loop.time_budget_ms` は実行予算",
            "Bash tool の foreground timeout 引数として渡さない",
            "`run_in_background: true`",
            "両方の完了通知を待つ",
        ):
            self.assertIn(snippet, step4_intro)

    def test_hunter_sections_no_longer_require_invalid_foreground_timeout(self) -> None:
        text = REVIEW_SKILL.read_text(encoding="utf-8")
        claude_section = section(text, "#### 4a: Claude Code レビュー", "#### 4b:")
        codex_section = section(text, "#### 4b: Codex CLI レビュー", "#### 4c:")
        for hunter_section in (claude_section, codex_section):
            self.assertIn("run_in_background: true", hunter_section)
            self.assertIn("foreground timeout 引数は指定しない", hunter_section)
            self.assertIn("timeout 上限 600000 ms", hunter_section)
            self.assertNotIn("- timeout: `1200000`", hunter_section)

    def test_implementation_constraints_do_not_force_invalid_timeout(self) -> None:
        text = REVIEW_SKILL.read_text(encoding="utf-8")
        constraints = section(text, "## 実装上の制約", "補助注記")
        self.assertIn("Step 4a / 4b は `run_in_background: true`", constraints)
        self.assertIn("foreground timeout 引数を `1200000` に固定してはならない", constraints)
        self.assertNotIn("timeout は必ず `1200000` に固定", constraints)

    def test_readme_mentions_20_minute_budget_is_not_bash_foreground_timeout(self) -> None:
        text = README.read_text(encoding="utf-8")
        for snippet in (
            "20 分は review budget / run-plan budget",
            "Claude Code Bash tool の foreground timeout 上限 600000 ms",
            "review hunters は run_in_background: true",
            "foreground timeout=1200000 は指定しない",
        ):
            self.assertIn(snippet, text)


if __name__ == "__main__":
    unittest.main()
