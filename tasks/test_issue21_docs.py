#!/usr/bin/env python3
"""Executable documentation checks for Issue #21 send behavior."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEND_SKILL = ROOT / "skills" / "send" / "SKILL.md"
REVIEW_CRITERIA = ROOT / "skills" / "review" / "REVIEW_CRITERIA.md"
README = ROOT / "README.md"


def section(text: str, start: str, end: str) -> str:
    start_index = text.index(start)
    end_index = text.index(end, start_index)
    return text[start_index:end_index]


class Issue21DocsTest(unittest.TestCase):
    def test_step3_keeps_should_fix_body_summary_opt_in_and_nits_local(self) -> None:
        text = SEND_SKILL.read_text(encoding="utf-8")
        step3 = section(text, "### Step 3: `findings.verified.json` の解析 (primary)", "### Step 3b:")

        required_snippets = [
            '`severity == "should_fix" && posting.post_policy == "body_summary"`',
            '`$include_should_fix == true` の場合は全件',
            '`$include_nit == true` の場合は全件',
            '`severity == "nit"`',
            'diff 範囲外の Should Fix / Nit は body の `## 行コメント不可 (diff 範囲外)` へ退避',
            '`nits.md`',
        ]
        for snippet in required_snippets:
            self.assertIn(snippet, step3)

    def test_step375_prompt_makes_should_fix_body_summary_default_no(self) -> None:
        text = SEND_SKILL.read_text(encoding="utf-8")
        step375 = section(text, "### Step 3.75:", "### Step 4:")

        required_snippets = [
            "$include_should_fix == true",
            "$inline_should_fix=[]",
            "$include_nit == true",
            "$inline_nit=[]",
            "追加 opt-in prompt は表示しない",
        ]
        for snippet in required_snippets:
            self.assertIn(snippet, step375)

    def test_step5_summary_reports_should_fix_and_nits(self) -> None:
        text = SEND_SKILL.read_text(encoding="utf-8")
        step5 = section(text, "### Step 5: 承認プロンプト", "### Step 6:")

        required_snippets = [
            "Should Fix inline comments: included <yes|no>",
            "<included_count>/<candidate_count>",
            "--include-should-fix で全件",
            "Nit artifact",
            "Should Fix / Nit は指定時に全件 inline comment に含めます",
        ]
        for snippet in required_snippets:
            self.assertIn(snippet, step5)

    def test_step8_reports_nits_artifact_path(self) -> None:
        text = SEND_SKILL.read_text(encoding="utf-8")
        step8 = section(text, "### Step 8: 結果報告", "## エラーハンドリング")
        self.assertIn("nits.md", step8)
        self.assertIn("Nit 件数", step8)

    def test_review_criteria_defines_capped_should_fix_body_summary_format(self) -> None:
        text = REVIEW_CRITERIA.read_text(encoding="utf-8")
        required_snippets = [
            "Should Fix inline comment 整形ルール",
            "上限なし",
            "1 件あたり 3 行以内",
            "path:L<行>",
            "改善内容 1 行、提案 1 行",
                        "## 行コメント不可 (diff 範囲外)",
        ]
        for snippet in required_snippets:
            self.assertIn(snippet, text)

    def test_readme_documents_low_noise_posting_behavior(self) -> None:
        text = README.read_text(encoding="utf-8")
        required_snippets = [
            "`Must Fix` は従来どおり GitHub review の inline comment",
            "`Should Fix` は default では投稿されない",
            "全件を PR の inline comment",
            "`Nit` は default では投稿せず",
            "`nits.md`",
        ]
        for snippet in required_snippets:
            self.assertIn(snippet, text)


if __name__ == "__main__":
    unittest.main()
