#!/usr/bin/env python3
"""Executable documentation checks for Issue #88 send severity options."""

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


class Issue88DocsTest(unittest.TestCase):
    def test_send_skill_documents_severity_option_matrix(self) -> None:
        text = SEND_SKILL.read_text(encoding="utf-8")
        self.assertIn('argument-hint: "[--auto-submit] [--include-should-fix] [--include-nit]"', text)
        usage = section(text, "## 使い方", "## フロー")
        for snippet in (
            "/pr-codex:send",
            "/pr-codex:send --auto-submit",
            "/pr-codex:send --include-should-fix",
            "/pr-codex:send --auto-submit --include-should-fix --include-nit",
            "Must Fixのみを inline comment",
            "Must FixとShould Fixを inline comment",
            "Must FixとShould FixとNitを inline comment",
        ):
            self.assertIn(snippet, usage)

    def test_arg_parser_defines_independent_approval_and_severity_flags(self) -> None:
        text = SEND_SKILL.read_text(encoding="utf-8")
        args = section(text, "### Step 0: 引数解析", "### Step 1:")
        for snippet in (
            "$send_mode = interactive | auto_submit",
            "$include_should_fix = true | false",
            "$include_nit = true | false",
            "--auto-submit",
            "--include-should-fix",
            "--include-nit",
            "順不同",
            "重複オプション",
            "--include-nit は --include-should-fix なしでは unsupported argument",
        ):
            self.assertIn(snippet, args)
        self.assertNotIn("複数オプション: `unsupported argument`", args)

    def test_payload_rules_inline_opted_in_should_fix_and_nit_with_out_of_range_fallback(self) -> None:
        text = SEND_SKILL.read_text(encoding="utf-8")
        extraction = section(text, "### Step 3:", "### Step 3b:")
        for snippet in (
            "$inline_should_fix",
            "$inline_nit",
            "$nit_inline_candidates",
            "posting.post_policy == \"body_summary\" && posting.explanation_postable == true",
            "`$should_fix_candidates` は `location.side` にかかわらず保持し",
            "RIGHT-side guard は抽出時ではなく Step 3.5 の inline 可否判定で適用する",
            "`$nit_inline_candidates` も `location.side` にかかわらず保持し",
            "範囲検証を通った全件",
            "`local_only` / `suppress` / `explanation_postable == false` の Nit は `--include-nit` 指定時でも inline comment に昇格せず",
            "fallback 対象にもならない",
            "diff 範囲外または `location.side != \"RIGHT\"` の Should Fix / Nit は inline comment へ昇格せず",
            "`nits.md`",
        ):
            self.assertIn(snippet, extraction)

        range_rules = section(text, "### Step 3.5:", "### Step 3.75:")
        for snippet in (
            "$must_fix` / `$should_fix_candidates` / `$nit_inline_candidates`",
            "$include_should_fix == true` の `$should_fix_candidates`",
            "$include_nit == true` の `$nit_inline_candidates`",
            "種別 (`Must Fix` / `Should Fix` / `Nit`)",
            "`location.side != \"RIGHT\"` の Should Fix / Nit",
            "退避理由 (`diff 範囲外` / `LEFT-side 非対応`)",
        ):
            self.assertIn(snippet, range_rules)

        payload = section(text, "### Step 4:", "### Step 4.5:")
        for snippet in (
            "comments`: `$must_fix` + `$inline_should_fix` + `$inline_nit`",
            "Should Fix / Nit も `path` / `line` / `side` / `body` を持つ inline comment",
            "diff 範囲外の Must Fix / Should Fix / Nit",
            "## 行コメント不可 (diff 範囲外)",
        ):
            self.assertIn(snippet, payload)
        self.assertNotIn("comments` 配列は Must Fix のみ", payload)

        preflight = section(text, "## STAGE 3: semantic_preflight", "## STAGE 4: payload_consistency")
        for snippet in (
            "許可される severity は default では `must_fix` のみ",
            "`--include-should-fix` 指定時は `must_fix` / `should_fix`",
            "`--include-should-fix --include-nit` 指定時は `must_fix` / `should_fix` / `nit`",
            "diff 範囲外または `location.side != 'RIGHT'` のため `## 行コメント不可 (diff 範囲外)` へ退避された opted-in should_fix / nit",
            "valid exclusion",
            "send 側の明示オプションだけで inline comment に昇格",
        ):
            self.assertIn(snippet, preflight)
        self.assertNotIn("findings[].severity == 'must_fix'", preflight)

    def test_readme_documents_severity_flags_and_safety_boundaries(self) -> None:
        text = README.read_text(encoding="utf-8")
        for snippet in (
            "/pr-codex:send --include-should-fix",
            "/pr-codex:send --auto-submit --include-should-fix --include-nit",
            "`--include-should-fix` は Must Fix + Should Fix を inline comment として投稿する",
            "`--include-nit` は `--include-should-fix` と併用し、Must Fix + 投稿可能な Should Fix + 投稿可能な Nit を inline comment として投稿する",
            "`local_only` / `suppress` / `explanation_postable: false` の Nit は投稿せず",
            "diff 範囲外のものは body の `## 行コメント不可 (diff 範囲外)` へ退避する",
            "unknown option や重複オプションは unsupported argument",
        ):
            self.assertIn(snippet, text)


if __name__ == "__main__":
    unittest.main()
