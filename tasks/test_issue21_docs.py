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
            'Step 5 の opt-in がない限り body には含めない',
            '先頭から最大 3 件',
            '`severity == "nit"`',
            '`posting.post_policy` の値に関わらず GitHub payload には含めず',
            '`nits.md`',
        ]
        for snippet in required_snippets:
            self.assertIn(snippet, step3)

    def test_step375_prompt_makes_should_fix_body_summary_default_no(self) -> None:
        text = SEND_SKILL.read_text(encoding="utf-8")
        step375 = section(text, "### Step 3.75:", "### Step 4:")

        required_snippets = [
            "非ブロッキング改善 (Should Fix) の上位 3 件",
            "default: no",
            "含める場合のみ yes",
            "`yes` / `y` / `はい` 等の明示的な承認",
            "候補先頭から最大 3 件",
        ]
        for snippet in required_snippets:
            self.assertIn(snippet, step375)

    def test_step5_summary_reports_should_fix_and_nits(self) -> None:
        text = SEND_SKILL.read_text(encoding="utf-8")
        step5 = section(text, "### Step 5: 承認プロンプト", "### Step 6:")

        required_snippets = [
            "Should Fix body summary: included <yes|no>",
            "<included_count>/<candidate_count>",
            "default: no",
            "Nit artifact",
            "Nit は PR には載せず nits.md にのみ残します",
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
            "## 非ブロッキング改善 (Should Fix)",
            "上位 3 件まで",
            "1 件あたり 3 行以内",
            "path:L<行>",
            "改善内容 1 行、提案 1 行",
            "## 良い点",
            "## 行コメント不可 (diff 範囲外)",
        ]
        for snippet in required_snippets:
            self.assertIn(snippet, text)

    def test_readme_documents_low_noise_posting_behavior(self) -> None:
        text = README.read_text(encoding="utf-8")
        required_snippets = [
            "`Must Fix` は従来どおり GitHub review の inline comment",
            "`Should Fix` は自動では投稿されない",
            "上位 3 件まで PR body",
            "`Nit` はノイズ抑制のため PR には載せず",
            "`nits.md`",
        ]
        for snippet in required_snippets:
            self.assertIn(snippet, text)


if __name__ == "__main__":
    unittest.main()
