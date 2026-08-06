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
            "レビューは <name> <model> と <name> <model> により行われました。",
            "投稿前検証 (semantic preflight) は Codex gpt-5.6-sol により行われました。",
            "欠落・不正なら deterministic failure として非ゼロ終了する（フッターを省略した投稿は行わない fail-closed。#124）",
            "3 行目（投稿前検証）は `counts.must_fix_total` が 1 件以上の場合のみ builder が追加する",
            "Must Fix 0 件の skip 時は表示しない",
            "`withheld` の存在・件数・カテゴリを新たに公開しない（#120 と整合）",
            "effort はどのフッター行にも表示しない",
            "`review_engines[]` は実行順の `Claude Code`、`Codex` の2件ちょうど",
            "→ 自動レビューフッター（常に最終セクション。#124）とする",
        ):
            self.assertIn(snippet, skill)

    def test_review_records_engines_for_footer(self) -> None:
        skill = REVIEW_SKILL.read_text(encoding="utf-8")
        for snippet in (
            'claude_model="claude-fable-5"',
            '--arg claude_model "$claude_model"',
            "`$claude_model` は、Claude CLI が full model name として受け付ける `claude-fable-5` に固定する",
            "`review_engines` の記録値と hunter の実行モデルは常に一致する（#124）",
            "effort は両 hunter とも最大値に固定する",
            "4a / 4b のコマンドテンプレートのモデル・effort を変更する場合は、この `review_engines` の値も併せて更新する",
            "send の builder はフッターに effort を表示せず、記録の検証にだけ使う（フッターに表示するのは name と model のみ。#128）",
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
        # maximum tier (Claude: max, Codex GPT-5.6 Sol: max).
        codex_efforts = set(EFFORT_OVERRIDE_RE.findall(skill))
        self.assertEqual(codex_efforts, {codex_engine.group(2)})
        self.assertEqual(codex_engine.group(1), "gpt-5.6-sol")
        self.assertEqual(codex_engine.group(2), "max")

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

        claude_model_assignments = re.findall(r'^claude_model="([^"]+)"$', skill, re.MULTILINE)
        self.assertEqual(claude_model_assignments, ["claude-fable-5"])

    def test_builder_requires_complete_review_engine_set(self) -> None:
        builder = BUILDER.read_text(encoding="utf-8")
        self.assertIn('REQUIRED_REVIEW_ENGINE_NAMES = ("Claude Code", "Codex")', builder)
        self.assertIn("engine_names != list(REQUIRED_REVIEW_ENGINE_NAMES)", builder)

    def test_semantic_verifier_constant_matches_send_template(self) -> None:
        builder = BUILDER.read_text(encoding="utf-8")
        skill = SEND_SKILL.read_text(encoding="utf-8")

        constant = re.search(r'SEMANTIC_VERIFIER_ENGINE = \("([^"]+)", "([^"]+)"\)', builder)
        self.assertIsNotNone(constant, "builder must pin the send Step 4.5 verifier engine")
        self.assertEqual(constant.group(1), "Codex")

        send_models = re.findall(r"^\s*-m (\S+) \\$", skill, re.MULTILINE)
        self.assertEqual(len(send_models), 1, send_models)
        self.assertEqual(constant.group(2), send_models[0])

    def test_readme_documents_footer(self) -> None:
        readme = README.read_text(encoding="utf-8")
        for snippet in (
            "自動レビューフッターの付加（body 末尾に pr-codex のバージョン（`producer.version`）とレビューに使ったモデル（実行順の `Claude Code`、`Codex` の2件ちょうどを要求する `metadata.json.review_engines`）を明記し、Must Fix があり Step 4.5 の semantic preflight を実行する投稿では検証側モデルも表示する。effort は確定できないため表示しない（#128）。欠落・不正なら builder が非ゼロ終了する fail-closed。#124）",
            "Codex CLI 側のレビュー (hunter) は、スキル内で `-m gpt-5.6-sol` と `model_reasoning_effort=\"max\"` を指定して実行する（#124）",
        ):
            self.assertIn(snippet, readme)


if __name__ == "__main__":
    unittest.main()
