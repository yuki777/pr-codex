#!/usr/bin/env python3
"""Executable documentation checks for Issue #127 fail-fast diff / timestamp contract."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REVIEW_SKILL = ROOT / "skills" / "review" / "SKILL.md"
VALIDATOR = ROOT / "tasks" / "validate_status.py"

# Fenced template forms (1 code block = 1 shell execution unit).
WORKDIR_TEMPLATE = "```bash\ninstall -d ~/claude-loop-pr-codex/$org-$repository-$pr_number\n```"
STARTED_AT_TEMPLATE = "```bash\ndate -u +%Y-%m-%dT%H:%M:%S+00:00\n```"
METADATA_TEMPLATE = "gh api repos/$org/$repository/pulls/$pr_number --jq '"
CI_GATE_TEMPLATE = (
    "gh api repos/$org/$repository/pulls/$pr_number > "
    "~/claude-loop-pr-codex/$org-$repository-$pr_number/ci-pull.json"
)
FILES_TEMPLATE = "gh api repos/$org/$repository/pulls/$pr_number/files --paginate"
RUNNING_TEMPLATE = (
    '\'{state:"running",started_at:$started_at,head_sha:$head_sha,'
    'stage:"ranker",failed_stage:null}\''
)
DIFF_TEMPLATE = (
    "gh pr diff $pr_number --repo $org/$repository > "
    "~/claude-loop-pr-codex/$org-$repository-$pr_number/pr.diff"
)
RANGES_TEMPLATE = 'awk -f "$plugin_root/skills/lib/extract-diff-ranges.awk"'
STEP3A_CI_REFETCH = "ci-workflow-runs.json"
FIRST_CLONE_TEMPLATE = (
    "gh repo clone $org/$repository "
    "~/claude-loop-pr-codex/$org-$repository-$pr_number/clone-claude"
)
NO_HEAD_SHA_FAILED_TEMPLATE = (
    'jq -n --arg started_at "$started_at" --arg finished_at "$finished_at" '
    '--arg failed_stage "$failed_stage" '
    "'{state:\"failed\",started_at:$started_at,finished_at:$finished_at,"
    "exit_code:1,stage:$failed_stage,failed_stage:$failed_stage}'"
)


class Issue127DocsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.skill = REVIEW_SKILL.read_text(encoding="utf-8")

    def test_template_order_is_fail_fast(self) -> None:
        # 作業ディレクトリ作成 → $started_at 取得 → Step 2b の metadata / files 取得
        # → state=running 書き込み → diff 生成 → Step 3a の CI artifact 再取得
        # → clone 初回作成 の順（#127）。
        markers = (
            ("workdir", WORKDIR_TEMPLATE),
            ("started_at", STARTED_AT_TEMPLATE),
            ("metadata", METADATA_TEMPLATE),
            ("ci_gate", CI_GATE_TEMPLATE),
            ("files", FILES_TEMPLATE),
            ("running", RUNNING_TEMPLATE),
            ("diff", DIFF_TEMPLATE),
            ("ranges", RANGES_TEMPLATE),
            ("step3a_ci_refetch", STEP3A_CI_REFETCH),
            ("first_clone", FIRST_CLONE_TEMPLATE),
        )
        positions = []
        for name, marker in markers:
            index = self.skill.find(marker)
            self.assertNotEqual(index, -1, f"missing template marker: {name}")
            positions.append((name, index))
        for (prev_name, prev_index), (next_name, next_index) in zip(positions, positions[1:]):
            self.assertLess(
                prev_index,
                next_index,
                f"template order violated: {prev_name} must appear before {next_name}",
            )

    def test_relocated_templates_are_not_duplicated(self) -> None:
        # 前倒しした作業ディレクトリ作成・running 書き込み・diff 生成の旧位置が
        # 残っていないこと（テンプレートは 1 箇所ずつ）。
        self.assertEqual(self.skill.count(WORKDIR_TEMPLATE), 1)
        self.assertEqual(self.skill.count(RUNNING_TEMPLATE), 1)
        self.assertEqual(self.skill.count(DIFF_TEMPLATE), 1)
        self.assertEqual(self.skill.count(RANGES_TEMPLATE), 1)
        # $started_at（Step 2b）と $finished_at（Step 5 冒頭）の 2 箇所だけ。
        self.assertEqual(self.skill.count(STARTED_AT_TEMPLATE), 2)

    def test_workdir_and_started_at_secured_on_step2b_entry(self) -> None:
        for snippet in (
            "#### 作業ディレクトリ作成と `$started_at` の取得（Step 2b 進出確定直後。#127）",
            "候補が Step 2b へ進むことが確定した直後（`$target_mode == \"auto\"` は Step 2 の reviewer / approve / status 判定の通過直後、`$target_mode == \"direct\"` は Step 0 の解決と直接指定 PR の status 判定の通過直後）",
            "auto mode で候補をスキップして次候補へ進んだ場合は、次候補で作業ディレクトリ作成と `$started_at` の取得を改めて実行する",
            "`$started_at` は実際の処理開始時刻を表し、`running` の 30 分 stale 判定と `actual_duration_ms` の基準になる",
            "作業ディレクトリと `$started_at` は Step 2b 進出確定直後に確保済みのため、ここでは再作成しない",
        ):
            self.assertIn(snippet, self.skill)

    def test_running_write_happens_at_selection_confirmation(self) -> None:
        for snippet in (
            "### Step 3: 選定確定（`state=running` 書き込み）と PR 差分の取得（fail-fast）",
            "選定確定後の全区間（PR 差分取得・CI artifact 再取得・clone・hunter 実行）で二重実行防止を有効にする（#127）",
            "gate 不通過候補に「running から 30 分」の再評価ペナルティは発生しない",
            # CI success gate 不通過候補は従来どおり status.json を書かずにスキップする。
            "満たさない場合は `status.json` を更新せず、この候補をスキップして Step 2 の次候補へ戻る",
        ):
            self.assertIn(snippet, self.skill)

    def test_step2b_failure_contract_is_explicit(self) -> None:
        for snippet in (
            # metadata 失敗: $head_sha 未取得 → head_sha キー省略テンプレート。
            "`$head_sha` が未取得のため Step 5 の **`head_sha` キーを省略した failed 更新テンプレート**（`$failed_stage=ranker`）を実行して、その回は終了する（#127）",
            # files 失敗: $head_sha 取得済み → 通常テンプレート。
            "Step 5 の failed 更新テンプレート（`$failed_stage=ranker`。`$head_sha` は取得済みのため `head_sha` キーを含む通常のテンプレート）を実行して、その回は終了する（#127）",
            # Step 5 側の head_sha キー省略テンプレートの使い方。
            "`validate_status.py` は `head_sha` を「存在する場合のみ検証する」optional キーとして扱うため、未取得時は空文字列を渡さず `head_sha` キー自体を省略する（#127）",
        ):
            self.assertIn(snippet, self.skill)
        self.assertIn(NO_HEAD_SHA_FAILED_TEMPLATE, self.skill)

    def test_finished_at_is_defined_on_every_failed_path(self) -> None:
        for snippet in (
            "また、必ず直前に `date -u +%Y-%m-%dT%H:%M:%S+00:00` で `$finished_at` を取得してから実行する（`$finished_at` の取得は正常系 Step 5 冒頭だけでなく、Step 2b の metadata / files 取得失敗・Step 3 の diff 取得失敗・clone 失敗・Step 3a の CI artifact 再取得失敗・Step 3a の再取得後に非 success となった中止を含む、すべての failed 分岐で必須。#127）",
            "`gh pr diff` が失敗または空出力の場合はここで非ゼロ終了し、Step 3a の CI artifact 再取得・clone は行わず、`date -u +%Y-%m-%dT%H:%M:%S+00:00` で `$finished_at` を取得してから Step 5 の failed 更新（`$failed_stage=ranker`）を実行して、その回は終了する",
        ):
            self.assertIn(snippet, self.skill)

    def test_step3a_non_success_closes_running(self) -> None:
        # Step 3 で書いた running を Step 3a の非 success 中止時に放置しない
        # （放置すると次回 /loop まで 30 分の stale 回収待ちになる）。
        for snippet in (
            "`date -u +%Y-%m-%dT%H:%M:%S+00:00` で `$finished_at` を取得してから Step 5 の failed 更新（`$failed_stage=ranker`）を実行して `running` を必ず閉じ、Step 2 の次候補へ戻る",
            "- Step 3a の再取得で `ci-status.json.state` が `success` 以外になった（`$target_mode == \"auto\"`） → `date -u` により `$finished_at` を取得し、`state=failed`（`failed_stage=ranker`）で記録して Step 3 で書いた `running` を必ず閉じ、Step 2 の次候補へ戻る",
        ):
            self.assertIn(snippet, self.skill)
        # 旧契約（running 未書き込み前提のスキップ文言）が残っていないこと。
        self.assertNotIn("`status.json` を `running` に更新しないまま", self.skill)

    def test_diff_failure_report_contract(self) -> None:
        for snippet in (
            "#### diff 取得失敗時の報告契約（#127）",
            "raw stderr はディスクに永続化しない（メモリ上で即サニタイズし、匿名化済みの診断だけを扱う）",
            "一時エラーの可能性があります。`state=failed` は次回の /loop で自動的に再試行されます",
            "原因を推測で断定しない。「恒久的」「変更ファイルが 300 件を超えているため」等の断定と、`files_changed` などサイズによる選定前スキップの改修提案を禁止する",
        ):
            self.assertIn(snippet, self.skill)

    def test_head_sha_omitted_failed_status_passes_validator(self) -> None:
        # head_sha キーを省略した failed status.json は validator を通過し、
        # 空文字列の head_sha は拒否される（省略が必須である根拠）。
        status = {
            "state": "failed",
            "started_at": "2026-08-05T00:00:00+00:00",
            "finished_at": "2026-08-05T00:00:30+00:00",
            "exit_code": 1,
            "stage": "ranker",
            "failed_stage": "ranker",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            omitted = Path(tmpdir) / "status-omitted.json"
            omitted.write_text(json.dumps(status), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(VALIDATOR), "--data", str(omitted)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            empty = Path(tmpdir) / "status-empty.json"
            empty.write_text(json.dumps({**status, "head_sha": ""}), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(VALIDATOR), "--data", str(empty)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("head_sha", result.stderr)


if __name__ == "__main__":
    unittest.main()
