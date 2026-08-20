#!/usr/bin/env python3
"""Docs tests: self-PR suppression contract between send/review SKILL.md and the builder.

GitHub rejects APPROVE / REQUEST_CHANGES reviews on the poster's own PR with
422, so send must detect self-PRs before the builder (fail-closed on unknown
identity) and the builder must suppress the event to COMMENT. These tests pin
the documented contract to the builder implementation.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEND_SKILL = (ROOT / "skills" / "send" / "SKILL.md").read_text(encoding="utf-8")
REVIEW_SKILL = (ROOT / "skills" / "review" / "SKILL.md").read_text(encoding="utf-8")
BUILDER = (ROOT / "tasks" / "build_review_payload.py").read_text(encoding="utf-8")


class SelfPrDocsTest(unittest.TestCase):
    def test_send_documents_step_2b_identity_detection(self) -> None:
        self.assertIn("### Step 2b: self-PR 検知（read-only）", SEND_SKILL)
        self.assertIn("gh api user --jq '.login'", SEND_SKILL)
        self.assertIn("gh api \"repos/$org/$repository/pulls/$pr_number\" --jq '.user.login'", SEND_SKILL)

    def test_send_step_flow_routes_through_step_2b(self) -> None:
        # Step 2 → Step 2b → Step 2.5 の遷移を固定し、Step 2b の迂回を防ぐ
        self.assertIn(
            "- 次アクション: 存在するなら `findings.verified.json` を Read ツールで取得して Step 2b へ。",
            SEND_SKILL,
        )
        self.assertNotIn("Read ツールで取得して Step 3 へ", SEND_SKILL)
        self.assertIn("Step 2b をスキップして Step 2.5 / Step 3 へ進んではならない", SEND_SKILL)
        self.assertIn(
            "- 次アクション: 両者が一致すれば `$self_review=true`、不一致なら `$self_review=false` として保持し、Step 2.5 へ進む。",
            SEND_SKILL,
        )
        step2b = SEND_SKILL.index("### Step 2b: self-PR 検知（read-only）")
        self.assertGreater(step2b, SEND_SKILL.index("### Step 2: メタデータとレビューの読み込み"))
        self.assertLess(step2b, SEND_SKILL.index("### Step 2.5: plugin root / schema / validator path の解決"))

    def test_send_documents_fail_closed_abort_before_builder(self) -> None:
        # 受け入れ条件 6: identity 取得失敗時は builder / preflight を実行せず投稿前に中断する
        self.assertIn(
            "どちらか一方でも失敗（非ゼロ終了または空出力）した場合は **投稿前に中断** する。"
            "builder / Step 4.5 preflight は実行せず、`sent/` 移動も行わない",
            SEND_SKILL,
        )
        self.assertIn(
            "- Step 2b の identity 取得（`gh api user` または PR 作者の取得）が非ゼロ終了または空出力 → 投稿前に中断する（fail-closed）",
            SEND_SKILL,
        )

    def test_send_builder_template_passes_self_review(self) -> None:
        self.assertIn("--self-review $self_review", SEND_SKILL)
        self.assertIn(
            "`$self_review == true` なら、Must Fix 件数と CI 状態にかかわらず `\"COMMENT\"` に抑止する",
            SEND_SKILL,
        )

    def test_send_documents_self_approval_422_diagnosis(self) -> None:
        self.assertIn("Can not approve your own pull request", SEND_SKILL)
        self.assertIn("リトライや event の自動差し替えはしない", SEND_SKILL)

    def test_builder_cli_requires_boolean_self_review(self) -> None:
        self.assertIn('cli.add_argument("--self-review", choices=("true", "false"))', BUILDER)
        match = re.search(r"^    required = \((?P<names>[^)]*)\)$", BUILDER, re.MULTILINE)
        self.assertIsNotNone(match, "build-mode required tuple not found")
        self.assertIn('"self_review"', match.group("names"))

    def test_skill_wording_matches_builder_suppression_lines(self) -> None:
        for constant in (
            "このレビューは PR 作成者自身のアカウントから投稿されているため、承認（APPROVE）ではなくコメントとして投稿します。",
            "このレビューは PR 作成者自身のアカウントから投稿されているため、変更リクエスト（REQUEST_CHANGES）ではなくコメントとして投稿します。",
        ):
            self.assertIn(constant, BUILDER)
            self.assertIn(constant, SEND_SKILL)

    def test_manifest_contract_records_self_review(self) -> None:
        self.assertIn("self-PR 判定（`self_review`", SEND_SKILL)
        self.assertIn('"self_review": manifest_core["self_review"]', BUILDER)
        self.assertIn("old-format manifest generated before self-review recording", BUILDER)

    def test_review_skill_mentions_self_pr_suppression(self) -> None:
        self.assertIn(
            "PR 作成者自身のアカウントで send を実行した場合（self-PR）、GitHub の制約により `APPROVE` / `REQUEST_CHANGES` は send 側で `COMMENT` に抑止される",
            REVIEW_SKILL,
        )


if __name__ == "__main__":
    unittest.main()
