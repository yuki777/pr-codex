#!/usr/bin/env python3
"""Executable documentation checks for Issue #75 run-plan routing rules."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REVIEW_SKILL = ROOT / "skills" / "review" / "SKILL.md"


class Issue75DocsTest(unittest.TestCase):
    def _run_plan_section(self) -> str:
        text = REVIEW_SKILL.read_text(encoding="utf-8")
        start = text.index("`depth_actual`（`standard` / `deep`）")
        end = text.index("```bash", start)
        return text[start:end]

    def test_run_plan_rules_are_documented_as_single_canonical_matrix(self) -> None:
        section = self._run_plan_section()
        self.assertIn("判定ロジックの canonical source は直後の `jq` テンプレート", section)
        self.assertIn("| 条件 | depth_actual | recommended_mode | budget_class | model_profile |", section)
        self.assertIn("| `risk_tags` に `security` または `data_migration` を含み、`files_changed <= 20` かつ `total_lines <= 1500` | `deep`（auto） | file-count rules | line-count rules | mode/depth rules |", section)
        self.assertIn("| `files_changed > 100` | depth rules | `skip` | `large` | `focused-fallback` |", section)

    def test_deprecated_prose_rule_headings_are_removed(self) -> None:
        section = self._run_plan_section()
        self.assertNotIn("モード切替の暫定ルール", section)
        self.assertNotIn("予算・routing の派生ルール", section)
        self.assertNotIn("- 明示 `--deep`", section)
        self.assertNotIn("downgrade", section)
        self.assertNotIn("- `budget_class =", section)


if __name__ == "__main__":
    unittest.main()
