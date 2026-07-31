#!/usr/bin/env python3
"""Executable documentation checks for Issue #74 Step 4c single current flow."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REVIEW_SKILL = ROOT / "skills" / "review" / "SKILL.md"


class Issue74DocsTest(unittest.TestCase):
    def _step4c(self) -> str:
        text = REVIEW_SKILL.read_text(encoding="utf-8")
        start = text.index("#### 4c: レビュー結果の統合")
        end = text.index("`review.md` 本文についても", start)
        return text[start:end]

    def test_step4c_uses_one_current_numbered_flow(self) -> None:
        section = self._step4c()
        numbered = [int(match.group(1)) for match in re.finditer(r"^([0-9]+)\. ", section, re.MULTILINE)]
        self.assertEqual(numbered, list(range(1, 16)))

    def test_step4c_documents_all_current_artifacts_and_validators(self) -> None:
        section = self._step4c()
        for snippet in (
            "findings.candidates.json",
            "findings.verified.json",
            "review-rounds.json",
            "findings.sarif",
            "validate_candidates.py",
            "validate_findings.py",
            "validate_review_rounds.py",
            "generate_findings_sarif.py",
            "validate_findings_sarif.py",
        ):
            self.assertIn(snippet, section)

    def test_step4c_mv_order_is_single_and_includes_review_rounds_before_sarif(self) -> None:
        section = self._step4c()
        self.assertEqual(section.count("temp file を final artifact に反映する際は"), 1)
        publish_section = section[section.index("temp file を final artifact に反映する際は") :]
        mv_targets = re.findall(r"mv .*?/([^/\s]+)\.tmp .*?/([^/\s]+)$", publish_section, re.MULTILINE)
        self.assertEqual(
            mv_targets[:5],
            [
                ("review.md", "review.md"),
                ("findings.candidates.json", "findings.candidates.json"),
                ("findings.verified.json", "findings.verified.json"),
                ("review-rounds.json", "review-rounds.json"),
                ("findings.sarif", "findings.sarif"),
            ],
        )

    def test_step4c_removed_deprecated_verified_only_flow(self) -> None:
        text = REVIEW_SKILL.read_text(encoding="utf-8")
        section = self._step4c()
        for haystack in (text, section):
            self.assertNotIn("findings.verified.json` / `review.md` / `review-rounds.json`", haystack)
            self.assertNotIn("`findings.verified.json` だけが残る状態", haystack)
            self.assertNotIn("review.md` を先に反映し、その後 `findings.verified.json`、最後に `review-rounds.json`", haystack)
            self.assertNotIn("schema / fingerprint validation のために `python3 $CLAUDE_PLUGIN_ROOT/tasks/validate_findings.py ...`", haystack)


if __name__ == "__main__":
    unittest.main()
