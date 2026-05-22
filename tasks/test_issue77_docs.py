#!/usr/bin/env python3
"""Executable documentation checks for Issue #77 send --auto-submit."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEND_SKILL = ROOT / "skills" / "send" / "SKILL.md"
README = ROOT / "README.md"


def section(text: str, start: str, end: str) -> str:
    start_index = text.index(start)
    end_index = text.index(end, start_index)
    return text[start_index:end_index]


class Issue77DocsTest(unittest.TestCase):
    def test_send_skill_declares_and_parses_only_auto_submit(self) -> None:
        text = SEND_SKILL.read_text(encoding="utf-8")
        self.assertIn('argument-hint: "[--auto-submit]"', text)
        args = section(text, "### Step 0: 引数解析", "### Step 1:")
        for snippet in (
            "$ARGUMENTS",
            "$send_mode = interactive | auto_submit",
            "`--auto-submit`",
            "unsupported argument",
            "未知オプション",
            "複数オプション",
        ):
            self.assertIn(snippet, args)

    def test_auto_submit_keeps_should_fix_default_no_without_prompt(self) -> None:
        text = SEND_SKILL.read_text(encoding="utf-8")
        step375 = section(text, "### Step 3.75:", "### Step 4:")
        for snippet in (
            "$send_mode == auto_submit",
            "$include_should_fix_body_summary=false",
            "$included_should_fix_body_summary=[]",
            "Should Fix body inclusion prompt は表示しない",
            "default: no",
        ):
            self.assertIn(snippet, step375)

    def test_auto_submit_still_requires_preflight_and_skips_only_final_prompt(self) -> None:
        text = SEND_SKILL.read_text(encoding="utf-8")
        step45 = section(text, "### Step 4.5:", "### Step 5:")
        self.assertIn("`--auto-submit` でもスキップしない", step45)
        self.assertIn("`preflight-result.json.verdict == \"PASS\"`", step45)
        self.assertIn("`preflight-codex.md` の `VERDICT: PASS`", step45)

        step5 = section(text, "### Step 5:", "### Step 5.5:")
        self.assertIn("interactive", step5)
        self.assertIn("auto_submit", step5)
        self.assertIn("最終投稿承認だけをスキップ", step5)
        self.assertIn("承認入力なしで Step 5.5", step5)

    def test_pre_submit_gates_check_head_sha_and_duplicate_response(self) -> None:
        text = SEND_SKILL.read_text(encoding="utf-8")
        gates = section(text, "### Step 5.5:", "### Step 6:")
        for snippet in (
            "review-response.json",
            ".html_url",
            "二重投稿",
            "現在の PR head",
            "metadata.json.head_sha",
            "古い review を自動投稿しない",
            "gh api \"/repos/$org/$repository/pulls/$pr_number\" --jq '.head.sha'",
        ):
            self.assertIn(snippet, gates)

    def test_readme_documents_auto_submit_usage_and_safety(self) -> None:
        text = README.read_text(encoding="utf-8")
        for snippet in (
            "/pr-codex:send --auto-submit",
            "最終承認 prompt なし",
            "Should Fix body summary は default no",
            "Step 4.5 の verifier pipeline はスキップしない",
            "投稿直前に現在の PR head",
            "review-response.json",
            "unknown option は unsupported argument",
        ):
            self.assertIn(snippet, text)


if __name__ == "__main__":
    unittest.main()
