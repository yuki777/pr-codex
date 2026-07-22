#!/usr/bin/env python3
"""Executable documentation checks for Issue #21 send behavior."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEND_SKILL = ROOT / "skills" / "send" / "SKILL.md"
EXPLAINER_POLICY = ROOT / "skills" / "review" / "EXPLAINER_POLICY.md"
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
            '`severity == "should_fix" && posting.post_policy == "body_summary" && posting.explanation_postable == true`',
            '`$include_should_fix == true` の場合は範囲検証を通った全件',
            '`$nit_inline_candidates`',
            '`severity == "nit"`',
            '`local_only` / `suppress` / `explanation_postable == false` の Nit は `--include-nit` 指定時でも inline comment に昇格せず',
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
            "--include-should-fix で投稿可能候補を含める",
            "Nit artifact",
            "Should Fix / Nit は指定時に投稿可能なものを inline comment に含めます",
        ]
        for snippet in required_snippets:
            self.assertIn(snippet, step5)

    def test_step8_reports_nits_artifact_path(self) -> None:
        text = SEND_SKILL.read_text(encoding="utf-8")
        step8 = section(text, "### Step 8: 結果報告", "## エラーハンドリング")
        self.assertIn("nits.md", step8)
        self.assertIn("Nit 件数", step8)

    def test_step8_clears_context_only_after_successful_send(self) -> None:
        text = SEND_SKILL.read_text(encoding="utf-8")
        step8 = section(text, "### Step 8: 結果報告", "## エラーハンドリング")
        required_snippets = [
            "成功報告を出した直後に slash command として `/clear` を単独で実行",
            "GitHub 投稿と `sent/` 移動が両方成功した後だけ実行",
            "失敗時、承認拒否時、Step 4.5 verifier FAIL、Step 5.5 safety gate 中断、または Step 7 失敗時には実行しない",
            "`/clear` に `/pr-codex:review` など後続コマンドを同じ行で連結してはならない",
            "context reset: 成功報告後に `/clear` を実行",
        ]
        for snippet in required_snippets:
            self.assertIn(snippet, step8)

    def test_explainer_policy_defines_capped_should_fix_body_summary_format(self) -> None:
        text = EXPLAINER_POLICY.read_text(encoding="utf-8")
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
            "`post_policy: body_summary` かつ `explanation_postable: true` の候補を PR の inline comment",
            "`Nit` は default では投稿せず",
            "`local_only` / `suppress` / `explanation_postable: false` の Nit は投稿せず",
            "`nits.md`",
        ]
        for snippet in required_snippets:
            self.assertIn(snippet, text)

    def test_readme_documents_post_send_clear_policy(self) -> None:
        text = README.read_text(encoding="utf-8")
        required_snippets = [
            "成功報告後に `/clear` を単独で実行して新しい conversation へ移る",
            "失敗時、承認拒否時、verifier FAIL、safety gate 中断、または `sent/` 移動失敗時には `/clear` しない",
        ]
        for snippet in required_snippets:
            self.assertIn(snippet, text)


if __name__ == "__main__":
    unittest.main()
