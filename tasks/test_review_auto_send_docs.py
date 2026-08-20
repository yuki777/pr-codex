#!/usr/bin/env python3
"""Executable documentation checks for review --auto-send."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REVIEW_SKILL = ROOT / "skills" / "review" / "SKILL.md"
README = ROOT / "README.md"


def section(text: str, start: str, end: str) -> str:
    start_index = text.index(start)
    end_index = text.index(end, start_index)
    return text[start_index:end_index]


class ReviewAutoSendDocsTest(unittest.TestCase):
    def test_review_accepts_auto_send_argument(self) -> None:
        text = REVIEW_SKILL.read_text(encoding="utf-8")
        self.assertIn('argument-hint: "[<PR URL|PR number>] [--auto-send]"', text)

        args = section(text, "## 引数", "## セットアップ")
        for snippet in (
            "$review_target",
            "$auto_send = true | false",
            "`--auto-send`",
            "フラグと位置引数は順不同",
            "重複 `--auto-send`",
            "使える引数は PR URL、PR 番号、--auto-send のみ",
            "depth は自動判定します",
        ):
            self.assertIn(snippet, args)

    def test_step0_normalizes_auto_send_without_depth_side_effects(self) -> None:
        text = REVIEW_SKILL.read_text(encoding="utf-8")
        step0 = section(text, "### Step 0:", "### Step 1:")
        for snippet in (
            "任意の位置に 1 回だけ現れる `--auto-send`",
            "Step 6.5 の auto-send phase だけを有効",
            "$target_mode = \"auto\"` / `$auto_send=true",
            "$target_mode = \"direct\"",
            "PR URL + `--auto-send`",
            "PR 番号 + `--auto-send`",
            "metadata.json.pr_url",
            "Step 1 以降の GitHub API access や local artifact 作成へ進まない",
        ):
            self.assertIn(snippet, step0)

    def test_auto_send_phase_uses_send_contract_direct_mode(self) -> None:
        text = REVIEW_SKILL.read_text(encoding="utf-8")
        step65 = section(text, "### Step 6.5:", "## エラーハンドリング")
        for snippet in (
            "slash command `/pr-codex:send ...` を再帰的に呼び出すのではなく",
            "`$plugin_root/skills/send/SKILL.md`",
            '$ARGUMENTS = "$pr_url --auto-send"',
            "$send_mode=auto_send",
            "$target_mode=direct",
            "$include_should_fix=false",
            "$include_nit=false",
            "投稿対象は Must Fix のみ",
            "Should Fix / Nit を含めたい場合は、auto-send ではなく手動",
            "二重投稿防止",
            "現在 PR head SHA",
            "sent/$dir_name-$head_sha_short",
            "成功報告後の `/clear` も send 側 Step 8 の契約に従って実行",
        ):
            self.assertIn(snippet, step65)

    def test_auto_send_write_exception_is_scoped_to_send_phase(self) -> None:
        text = REVIEW_SKILL.read_text(encoding="utf-8")
        constraints = section(text, "## 実装上の制約", "補助注記")
        for snippet in (
            "$auto_send=true",
            "Step 6.5 だけ",
            "review-payload.json",
            "review-response.json",
            'gh api --method POST "/repos/$org/$repository/pulls/$pr_number/reviews"',
            "send 側 Step 6",
            "Step 7 の `sent/` 移動",
            "gh pr review",
        ):
            self.assertIn(snippet, constraints)

    def test_readme_documents_review_auto_send_usage_and_scope(self) -> None:
        text = README.read_text(encoding="utf-8")
        for snippet in (
            "/pr-codex:review https://github.com/org/repo/pull/123 --auto-send",
            "/pr-codex:review 123 --auto-send",
            "/loop 10m /pr-codex:review --auto-send",
            "/pr-codex:send <PR URL> --auto-send` 相当",
            "投稿対象は Must Fix のみ",
            "Should Fix / Nit は含めない",
            "slash command を再帰実行するのではなく",
        ):
            self.assertIn(snippet, text)


if __name__ == "__main__":
    unittest.main()
