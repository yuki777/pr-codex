#!/usr/bin/env python3
"""Regression tests for Issue #83 BEAR.Sunday bear-review integration docs."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REVIEW_SKILL = ROOT / "skills" / "review" / "SKILL.md"


class BearReviewDocsTest(unittest.TestCase):
    def _skill_text(self) -> str:
        return REVIEW_SKILL.read_text(encoding="utf-8")

    def test_review_flow_uses_simple_jq_bear_sunday_detection(self) -> None:
        text = self._skill_text()
        self.assertIn("### Step 3b: BEAR.Sunday 判定", text)
        self.assertIn("jq -e '.require | has(\"bear/sunday\")'", text)
        self.assertIn("終了コード 0 なら BEAR.Sunday", text)
        self.assertNotIn("detect_bear_sunday.py", text)
        self.assertNotIn("bear-review-context.json", text)

    def test_review_flow_documents_only_bear_sunday_as_detection_signal(self) -> None:
        text = self._skill_text()
        self.assertIn("BEAR.Sunday 判定は `bear/sunday` dependency だけを見る", text)
        self.assertIn("`bear/resource` など他の `bear/*` package", text)
        self.assertIn("layout signal だけでは BEAR.Sunday と判定しない", text)

    def test_hunter_prompts_include_bear_review_guidance_placeholder(self) -> None:
        text = self._skill_text()
        self.assertIn("{BEAR_REVIEW_GUIDANCE}", text)
        self.assertIn("BEAR.Sunday 固有観点", text)
        self.assertIn("既存の verifier / severity classification / posting policy", text)


if __name__ == "__main__":
    unittest.main()
