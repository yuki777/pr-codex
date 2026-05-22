#!/usr/bin/env python3
"""Executable documentation checks for Issue #72 review-rounds counter semantics."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROUND_SCHEMA = ROOT / "schemas" / "review-rounds.v1.json"
REVIEW_SKILL = ROOT / "skills" / "review" / "SKILL.md"
README = ROOT / "README.md"


class Issue72DocsTest(unittest.TestCase):
    def test_review_rounds_schema_defines_active_remaining_counters(self) -> None:
        schema = json.loads(ROUND_SCHEMA.read_text(encoding="utf-8"))
        round_props = schema["$defs"]["round"]["properties"]
        metrics_props = schema["$defs"]["metrics"]["properties"]

        output_description = round_props["output_candidates_count"]["description"]
        self.assertIn("remaining ACTIVE", output_description)
        self.assertIn("AFTER this round", output_description)
        self.assertIn("input_candidates_count - (verifier_pass_count + verifier_fail_count + insufficient_evidence_count) + new active candidates", output_description)
        self.assertIn("0 when all candidates were verified/rejected", output_description)

        posted_description = metrics_props["posted_candidate_count"]["description"]
        self.assertIn("remaining ACTIVE candidates", posted_description)
        self.assertIn("output_candidates_count of the final round", posted_description)
        self.assertIn("NOT the count of findings included in findings.verified.json", posted_description)

    def test_review_skill_gives_concrete_counter_example(self) -> None:
        text = REVIEW_SKILL.read_text(encoding="utf-8")
        for snippet in (
            "review-rounds カウンタ定義",
            "input_candidates_count=2",
            "verifier_pass_count=2",
            "output_candidates_count=0",
            "posted_candidate_count=0",
            "canonical findings に載った数ではない",
        ):
            self.assertIn(snippet, text)

    def test_readme_warns_posted_candidate_count_is_not_posted_findings(self) -> None:
        text = README.read_text(encoding="utf-8")
        for snippet in (
            "review-rounds カウンタ",
            "posted_candidate_count",
            "remaining ACTIVE candidates",
            "findings.verified.json の件数ではない",
        ):
            self.assertIn(snippet, text)


if __name__ == "__main__":
    unittest.main()
