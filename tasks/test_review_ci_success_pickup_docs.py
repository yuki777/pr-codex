#!/usr/bin/env python3
"""Executable documentation checks for auto-pickup CI success gating."""

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


class ReviewCiSuccessPickupDocsTest(unittest.TestCase):
    def test_auto_search_roughly_filters_successful_statuses(self) -> None:
        text = REVIEW_SKILL.read_text(encoding="utf-8")
        step1 = section(text, "### Step 1:", "### Step 2:")

        for snippet in (
            "自動選定では CI が pass している PR だけをピックアップ",
            "Search API では `status:success` で粗く絞る",
            "/search/issues?q=is:pr+state:open+draft:false+review-requested:$MY_LOGIN+status:success",
            "`status:success` - commit status / checks が success の PR だけを粗く絞る",
            "候補確定前に Step 2b で current head の CI status を必ず再取得",
            "`success` 以外ならスキップ",
        ):
            self.assertIn(snippet, step1)

    def test_auto_selection_requires_current_head_ci_success(self) -> None:
        text = REVIEW_SKILL.read_text(encoding="utf-8")
        step2 = section(text, "### Step 2:", "### Step 3:")

        for snippet in (
            "Step 2b の CI success gate を通過した最初の1件を選定",
            'CI が `success` の候補がない場合は何もせず終了',
            '`ci-status.json.state == "success"`',
            "`ci-status.json.head_sha == $head_sha`",
            "`ci-status.json.state != \"success\"`",
            "この PR を選定せず Step 2 の次候補に戻る",
            "`$target_mode == \"direct\"` ではこの gate を実行しない",
            "`status.json` を更新せず",
            'jq -e --arg head_sha "$head_sha"',
            'and .head_sha == $head_sha',
            'and .state == "success"',
            "check-runs API はページング対象",
            "pagination なしで最初のページだけを gate 入力にしてはならない",
        ):
            self.assertIn(snippet, step2)

    def test_step3a_rechecks_auto_gate_but_direct_mode_only_records_context(self) -> None:
        text = REVIEW_SKILL.read_text(encoding="utf-8")
        step3a = section(text, "### Step 3a:", "failed job log")

        for snippet in (
            "review hunter を起動する直前に再取得",
            "再取得した `ci-status.json.state` が `success` ではなくなっていた場合",
            # #127: running は Step 3 で書き込み済みのため、放置せず failed で閉じる。
            "Step 5 の failed 更新（`$failed_stage=ranker`）を実行して `running` を必ず閉じ、Step 2 の次候補へ戻る",
            "`$target_mode == \"direct\"` では CI success gate としては扱わず",
            "`failure` / `pending` も reviewer へ渡す context",
            "`$target_mode == \"auto\"` で `success` 以外なら、`date -u` で `$finished_at` を取得してから Step 5 の failed 更新（`$failed_stage=ranker`）で `running` を閉じ、Step 2 の次候補へ戻る",
        ):
            self.assertIn(snippet, step3a)

    def test_readme_documents_auto_pickup_success_gate_and_direct_mode_exception(self) -> None:
        text = README.read_text(encoding="utf-8")

        for snippet in (
            "`review-requested` かつ `status:success`",
            'current head の `ci-status.json.state == "success"`',
            "CI が `failure` / `pending` / `skipped` / 未取得の候補はスキップ",
            "直接指定時は review requested / approve 済み判定 / CI success gate をスキップ",
            "`/pr-codex:review` の自動選定では current head",
            "PR URL / PR番号で直接指定した場合は CI success gate を適用せず",
        ):
            self.assertIn(snippet, text)


if __name__ == "__main__":
    unittest.main()
