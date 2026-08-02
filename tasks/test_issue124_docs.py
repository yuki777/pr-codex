#!/usr/bin/env python3
"""Executable documentation checks for Issue #124 automated-review footer."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEND_SKILL = ROOT / "skills" / "send" / "SKILL.md"
REVIEW_SKILL = ROOT / "skills" / "review" / "SKILL.md"
BUILDER = ROOT / "tasks" / "build_review_payload.py"
README = ROOT / "README.md"

EFFORT_OVERRIDE_RE = re.compile(r"-c 'model_reasoning_effort=\"([a-z]+)\"'")


class Issue124DocsTest(unittest.TestCase):
    def test_send_documents_footer_composition(self) -> None:
        skill = SEND_SKILL.read_text(encoding="utf-8")
        for snippet in (
            "body 末尾に必ず自動レビューのフッターを追加する（#124）",
            "`findings.verified.json` の `producer.version` と `metadata.json.review_engines[]`",
            "これは [pr-codex](https://github.com/yuki777/pr-codex):v<producer.version> による自動レビューです。",
            "レビューは <name> <model> (<effort>) と <name> <model> (<effort>) により行われました。",
            "投稿前検証 (semantic preflight) は Codex gpt-5.6-sol (high) により行われました。",
            "欠落・不正なら deterministic failure として非ゼロ終了する（フッターを省略した投稿は行わない fail-closed。#124）",
            "3 行目（投稿前検証）は `counts.must_fix_total` が 1 件以上の場合のみ builder が追加する",
            "Must Fix 0 件の skip 時は表示しない",
            "`withheld` の存在・件数・カテゴリを新たに公開しない（#120 と整合）",
            "表示する effort は CLI 語彙の最大 tier を `max` に正規化する",
            "→ 自動レビューフッター（常に最終セクション。#124）とする",
        ):
            self.assertIn(snippet, skill)

    def test_review_records_engines_for_footer(self) -> None:
        skill = REVIEW_SKILL.read_text(encoding="utf-8")
        for snippet in (
            '--arg claude_model "$claude_model"',
            "`$claude_model` は、現在のメインコンテキストが実行している Claude モデルの ID",
            "`review_engines` の記録値と hunter の実行モデルは常に一致する（#124）",
            "effort は両 hunter とも最大値に固定する",
            "4a / 4b のコマンドテンプレートのモデル・effort を変更する場合は、この `review_engines` の値も併せて更新する",
            "send の builder が表示時に最大 tier を `max` へ正規化する（#124）",
        ):
            self.assertIn(snippet, skill)

    def test_review_engines_literals_match_hunter_templates(self) -> None:
        skill = REVIEW_SKILL.read_text(encoding="utf-8")

        codex_engine = re.search(r'\{name:"Codex",model:"([^"]+)",effort:"([^"]+)"\}', skill)
        self.assertIsNotNone(codex_engine, "review_engines must record the Codex hunter engine")
        codex_models = re.findall(r"^\s*-m (\S+) \\$", skill, re.MULTILINE)
        self.assertEqual(len(codex_models), 1, codex_models)
        self.assertEqual(codex_engine.group(1), codex_models[0])
        # The recorded effort must match every `-c 'model_reasoning_effort="..."'`
        # literal in the review skill, and both hunters are pinned to their
        # maximum tier (Claude: max, Codex: xhigh — Codex has no "max" value).
        codex_efforts = set(EFFORT_OVERRIDE_RE.findall(skill))
        self.assertEqual(codex_efforts, {codex_engine.group(2)})
        self.assertEqual(codex_engine.group(2), "xhigh")

        claude_engine = re.search(r'\{name:"Claude Code",model:(\$[a-z_]+),effort:"([^"]+)"\}', skill)
        self.assertIsNotNone(claude_engine, "review_engines must record the Claude hunter engine")
        claude_efforts = re.findall(r"^\s*--effort (\S+) \\$", skill, re.MULTILINE)
        self.assertEqual(len(claude_efforts), 1, claude_efforts)
        self.assertEqual(claude_engine.group(2), claude_efforts[0])
        self.assertEqual(claude_engine.group(2), "max")

        # The 4a hunter must pin --model to the same variable recorded in review_engines,
        # so the posted footer and the executed hunter model cannot drift (#124 advisory).
        pinned_models = re.findall(r'^\s*--model "(\$[a-z_]+)" \\$', skill, re.MULTILINE)
        self.assertEqual(len(pinned_models), 1, pinned_models)
        self.assertEqual(pinned_models[0], claude_engine.group(1))
        self.assertEqual(pinned_models[0], "$claude_model")

    def test_semantic_verifier_constant_matches_send_template(self) -> None:
        builder = BUILDER.read_text(encoding="utf-8")
        skill = SEND_SKILL.read_text(encoding="utf-8")

        constant = re.search(r'SEMANTIC_VERIFIER_ENGINE = \("([^"]+)", "([^"]+)", "([^"]+)"\)', builder)
        self.assertIsNotNone(constant, "builder must pin the send Step 4.5 verifier engine")
        self.assertEqual(constant.group(1), "Codex")

        send_models = re.findall(r"^\s*-m (\S+) \\$", skill, re.MULTILINE)
        self.assertEqual(len(send_models), 1, send_models)
        self.assertEqual(constant.group(2), send_models[0])

        send_efforts = set(EFFORT_OVERRIDE_RE.findall(skill))
        self.assertEqual(send_efforts, {constant.group(3)})

    def test_readme_documents_footer(self) -> None:
        readme = README.read_text(encoding="utf-8")
        for snippet in (
            "自動レビューフッターの付加（body 末尾に pr-codex のバージョン（`producer.version`）とレビューに使ったモデル・effort（`metadata.json.review_engines`）を明記し、Must Fix があり Step 4.5 の semantic preflight を実行する投稿では検証側モデル・effort も表示する。欠落・不正なら builder が非ゼロ終了する fail-closed。#124）",
            "hunter の reasoning effort は Codex CLI の最大値 `model_reasoning_effort=\"xhigh\"` に固定する（#124",
        ):
            self.assertIn(snippet, readme)


if __name__ == "__main__":
    unittest.main()
