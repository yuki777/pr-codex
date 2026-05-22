#!/usr/bin/env python3
"""Regression tests for Issue #83 BEAR.Sunday bear-review integration."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "tasks"
if str(TASKS) not in sys.path:
    sys.path.insert(0, str(TASKS))

from detect_bear_sunday import detect_bear_sunday

REVIEW_SKILL = ROOT / "skills" / "review" / "SKILL.md"


class BearSundayDetectionTest(unittest.TestCase):
    def test_detects_bear_sunday_from_composer_dependency_and_records_skill_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            (repo / "composer.json").write_text(
                json.dumps({"require": {"bear/sunday": "^1.0", "php": "^8.2"}}),
                encoding="utf-8",
            )
            skill = Path(tmp) / "BEAR.Skills" / ".claude" / "skills" / "bear-review" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("# bear-review\n", encoding="utf-8")

            result = detect_bear_sunday(repo, [skill])

            self.assertTrue(result["is_bear_sunday"])
            self.assertEqual(result["framework_detected"], "bear-sunday")
            self.assertIn("composer:bear/sunday", result["detection_signals"])
            self.assertEqual(result["bear_review"]["status"], "available")
            self.assertEqual(result["bear_review"]["skill_path"], str(skill))

    def test_detects_bear_sunday_from_multiple_layout_signals_without_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            (repo / "src" / "Resource" / "Page").mkdir(parents=True)
            (repo / "src" / "Module").mkdir(parents=True)
            (repo / "src" / "Resource" / "Page" / "Index.php").write_text("<?php\n", encoding="utf-8")
            (repo / "src" / "Module" / "AppModule.php").write_text("<?php\n", encoding="utf-8")

            result = detect_bear_sunday(repo, [])

            self.assertTrue(result["is_bear_sunday"])
            self.assertEqual(result["framework_detected"], "bear-sunday")
            self.assertIn("layout:src/Resource", result["detection_signals"])
            self.assertIn("layout:src/Module", result["detection_signals"])
            self.assertEqual(result["bear_review"]["status"], "unavailable")
            self.assertEqual(result["bear_review"]["skip_reason"], "bear-review skill unavailable")

    def test_non_bear_project_is_not_detected_even_if_one_layout_signal_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            (repo / "src" / "Resource").mkdir(parents=True)
            (repo / "composer.json").write_text(
                json.dumps({"require": {"symfony/console": "^7.0"}}),
                encoding="utf-8",
            )

            result = detect_bear_sunday(repo, [])

            self.assertFalse(result["is_bear_sunday"])
            self.assertIsNone(result["framework_detected"])
            self.assertEqual(result["bear_review"]["status"], "not_applicable")


class BearReviewDocsTest(unittest.TestCase):
    def _skill_text(self) -> str:
        return REVIEW_SKILL.read_text(encoding="utf-8")

    def test_review_flow_documents_bear_review_context_artifact_and_graceful_fallback(self) -> None:
        text = self._skill_text()
        self.assertIn("### Step 3b: BEAR.Sunday / bear-review context artifact の生成", text)
        self.assertIn("detect_bear_sunday.py", text)
        self.assertIn("bear-review-context.json", text)
        self.assertIn("bear_review.status == \"unavailable\"", text)
        self.assertIn("通常レビューは継続", text)

    def test_hunter_prompts_include_bear_review_guidance_placeholder(self) -> None:
        text = self._skill_text()
        self.assertIn("{BEAR_REVIEW_GUIDANCE}", text)
        self.assertIn("BEAR.Sunday 固有観点", text)
        self.assertIn("既存の verifier / severity classification / posting policy", text)


if __name__ == "__main__":
    unittest.main()
