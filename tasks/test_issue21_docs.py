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
            '`severity == "should_fix"` はデフォルト `posting.post_policy == "local_only"`',
            '`posting.post_policy == "body_summary"` の Should Fix だけを opt-in body summary 候補にする',
            'body summary 候補は上位 3 件まで',
            '`severity == "nit"` は常に `posting.post_policy == "local_only"` として扱い、PR payload には含めない',
            '`nits.md`',
            '`severity == "note"` かつ `posting.post_policy == "body_summary"` の短い補足は body 前提欄候補として保持する',
        ]
        for snippet in required_snippets:
            self.assertIn(snippet, step3)

    def test_step5_prompt_makes_should_fix_body_summary_default_no(self) -> None:
        text = SEND_SKILL.read_text(encoding="utf-8")
        step5 = section(text, "### Step 5: 承認プロンプト", "### Step 6:")

        required_snippets = [
            "Should Fix body summary: default no",
            "上位 3 件",
            "yes / y / はい で明示承認された場合のみ body に含める",
            "default: no",
            "Nit は nits.md のみに保存し、GitHub には投稿しません",
        ]
        for snippet in required_snippets:
            self.assertIn(snippet, step5)

    def test_step8_reports_nits_artifact_path(self) -> None:
        text = SEND_SKILL.read_text(encoding="utf-8")
        step8 = section(text, "### Step 8: 結果報告", "## エラーハンドリング")
        self.assertIn("nits.md", step8)
        self.assertIn("Nit ローカル artifact", step8)

    def test_review_criteria_defines_capped_should_fix_body_summary_format(self) -> None:
        text = REVIEW_CRITERIA.read_text(encoding="utf-8")
        required_snippets = [
            "## 非ブロッキング改善 (Should Fix)",
            "上位 3 件まで",
            "1 件あたり 3 行以内",
            "path + 改善内容 1 行 + 提案 1 行",
            "Must Fix セクションより下",
        ]
        for snippet in required_snippets:
            self.assertIn(snippet, text)

    def test_readme_documents_low_noise_posting_behavior(self) -> None:
        text = README.read_text(encoding="utf-8")
        required_snippets = [
            "Must Fix は従来どおり inline review comment",
            "Should Fix はデフォルトではローカルのみ",
            "明示 opt-in した場合だけ上位 3 件を body summary",
            "Nit は nits.md ローカル artifact のみ",
            "GitHub には投稿しない",
        ]
        for snippet in required_snippets:
            self.assertIn(snippet, text)


if __name__ == "__main__":
    unittest.main()
