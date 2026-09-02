#!/usr/bin/env python3
"""Executable documentation checks for Issue #143 footer models and scope removal."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEND_SKILL = ROOT / "skills" / "send" / "SKILL.md"
REVIEW_SKILL = ROOT / "skills" / "review" / "SKILL.md"
BUILDER = ROOT / "tasks" / "build_review_payload.py"
README = ROOT / "README.md"


class Issue143DocsTest(unittest.TestCase):
    def test_send_approve_section_drops_review_scope_line(self) -> None:
        skill = SEND_SKILL.read_text(encoding="utf-8")
        self.assertIn("検証観点の行は出力しない（#143）", skill)
        self.assertIn("- 変更ファイル: <$reviewed_files>\n    - CI 状態: <$ci_status_state または \"未取得\">", skill)
        self.assertNotIn("$reviewed_scope", skill)
        self.assertNotIn("- 検証観点:", skill)

    def test_builder_never_renders_review_scope(self) -> None:
        builder = BUILDER.read_text(encoding="utf-8")
        self.assertNotIn("検証観点", builder)
        self.assertNotIn("DEFAULT_REVIEW_SCOPE", builder)
        self.assertNotIn("def review_scope", builder)

    def test_review_captures_actual_models_from_execution_evidence(self) -> None:
        skill = REVIEW_SKILL.read_text(encoding="utf-8")
        for snippet in (
            # Step 4a keeps the structured output but adds the CLI result
            # wrapper so modelUsage becomes runtime evidence for the footer.
            "  --output-format json \\",
            ">  ~/claude-loop-pr-codex/$org-$repository-$pr_number/claude-review.result.json \\",
            "&& jq '.structured_output | if type == \"object\" then . else error(\"structured_output missing\") end'",
            # Step 4c transcribes the actually-used model names into metadata.
            "**実使用モデルの転記 (必須)** を行う（#143）",
            "`modelUsage` に複数モデルがある場合は `outputTokens` が最大のモデルを採用し、`codex.log` の `model:` 行は先頭の 1 件だけを使う",
            '.review_engines[0].model = ($claude_result[0].modelUsage | to_entries | max_by(.value.outputTokens) | .key)',
            '.review_engines[1].model = ($codex_log | split("\\n") | map(select(startswith("model: ")))[0] | ltrimstr("model: "))',
            # Transcription failure is fail-closed: no posting with a footer
            # that does not match the executed engines.
            "Step 4c の実使用モデル転記テンプレートが非ゼロ終了",
        ):
            self.assertIn(snippet, skill)

    def test_send_and_readme_document_transcribed_footer_models(self) -> None:
        send = SEND_SKILL.read_text(encoding="utf-8")
        readme = README.read_text(encoding="utf-8")
        self.assertIn(
            "Step 4c が実行証跡（`claude-review.result.json` の `modelUsage` / `codex.log` の `model:` 行）から実際に使用されたモデル名へ `model` を上書きする #143",
            send,
        )
        self.assertIn("実際に使用されたモデル名を使う。転記に失敗した場合はレビューを failed とし、実行事実と一致しないフッターでは投稿しない（#143）", readme)


if __name__ == "__main__":
    unittest.main()
