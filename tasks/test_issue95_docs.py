#!/usr/bin/env python3
"""Executable documentation checks for Issue #95 direct send targets."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEND_SKILL = ROOT / "skills" / "send" / "SKILL.md"
REVIEW_SKILL = ROOT / "skills" / "review" / "SKILL.md"
README = ROOT / "README.md"


def section(text: str, start: str, end: str) -> str:
    start_index = text.index(start)
    end_index = text.index(end, start_index)
    return text[start_index:end_index]


class Issue95DocsTest(unittest.TestCase):
    def test_send_accepts_direct_target_argument(self) -> None:
        text = SEND_SKILL.read_text(encoding="utf-8")
        self.assertIn(
            'argument-hint: "[<PR URL|PR number>] [--auto-send] [--include-should-fix] [--include-nit]"',
            text,
        )

        args = section(text, "### Step 0: 引数解析", "### Step 1:")
        for snippet in (
            "$target_mode = auto | direct",
            "フラグと位置引数は順不同",
            "$target_mode=auto",
            "https://github.com/<org>/<repo>/pull/<number>",
            '$target_dir_name = "<org>-<repository>-<pr_number>"',
            "$target_pr_number=<number>",
            "位置引数が2つ以上",
            "unsupported argument",
        ):
            self.assertIn(snippet, args)

    def test_send_direct_mode_resolves_only_requested_review_directory(self) -> None:
        text = SEND_SKILL.read_text(encoding="utf-8")
        step1 = section(text, "### Step 1:", "### Step 2:")
        for snippet in (
            "#### common: plugin root / validator path の早期解決",
            "direct mode / auto mode のどちらでも",
            'test -d "$plugin_root/tasks" && test -d "$plugin_root/schemas"',
            "#### direct mode（PR URL / PR 番号指定）",
            "自動選定はスキップ",
            'awk -F- -v pr="$target_pr_number"',
            'index($0, prefix) == 1',
            '$(NF - 1) == pr',
            "複数件なら曖昧として中断し、PR URL 指定を案内",
            "指定 PR は既に send 済み",
            "指定 PR の completed レビューが無い",
            "status.json",
            'state == "completed"',
            "review.md",
            "findings.verified.json",
            "$dir_name = $target_dir_name",
            "#### auto mode（位置引数なし）",
            '$target_mode=auto` の場合のみ実行する',
        ):
            self.assertIn(snippet, step1)

    def test_review_completed_report_shows_send_commands_with_counts(self) -> None:
        text = REVIEW_SKILL.read_text(encoding="utf-8")
        step6 = section(text, "### Step 6: 結果報告", "## エラーハンドリング")
        for snippet in (
            "failed 終了時は send 案内を出さない",
            "metadata.json.pr_url",
            '$count_must` = `findings[] | select(.severity == "must_fix")',
            "$count_must_inline",
            '.posting.post_policy == "inline"',
            "send 側の primary guard が中断する非inline Must Fix",
            "$count_must_inline != $count_must",
            '.posting.post_policy == "body_summary"',
            ".posting.explanation_postable == true",
            '.location.side == "RIGHT"',
            "pr.diff.ranges.txt",
            "LEFT-side / diff 範囲外 / range 不明",
            "次のアクション（GitHub への投稿）",
            "/pr-codex:send $pr_url --auto-send",
            "/pr-codex:send $pr_url --auto-send --include-should-fix",
            "Must Fix 0 件のため inline は投稿されず",
            "投稿対象の指摘なし",
        ):
            self.assertIn(snippet, step6)

    def test_readme_documents_direct_send_usage(self) -> None:
        text = README.read_text(encoding="utf-8")
        for snippet in (
            "/pr-codex:send https://github.com/org/repo/pull/123 --auto-send",
            "/pr-codex:send 123 --auto-send",
            "URL に対応する completed レビューだけを対象",
            "PR 番号のみ指定が複数 directory に一致した場合は中断",
            "対象 PR URL と Must Fix / inline 投稿可能な Should Fix 件数入り",
        ):
            self.assertIn(snippet, text)


if __name__ == "__main__":
    unittest.main()
