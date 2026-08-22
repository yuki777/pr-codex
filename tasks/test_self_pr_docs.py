#!/usr/bin/env python3
"""Docs tests: self-PR suppression contract between send/review SKILL.md and the builder.

GitHub rejects APPROVE / REQUEST_CHANGES reviews on the poster's own PR with
422, so send must detect self-PRs before the builder (fail-closed on unknown
identity) and the builder must suppress the event to COMMENT. These tests pin
the documented contract to the builder implementation.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEND_SKILL = (ROOT / "skills" / "send" / "SKILL.md").read_text(encoding="utf-8")
REVIEW_SKILL = (ROOT / "skills" / "review" / "SKILL.md").read_text(encoding="utf-8")
BUILDER = (ROOT / "tasks" / "build_review_payload.py").read_text(encoding="utf-8")


class SelfPrDocsTest(unittest.TestCase):
    def step2b_bash_block(self) -> str:
        section = SEND_SKILL.split("### Step 2b: self-PR 検知（read-only）", 1)[1].split(
            "### Step 2.5: plugin root / schema / validator path の解決", 1
        )[0]
        blocks = re.findall(r"```bash\n(.*?)\n```", section, re.DOTALL)
        self.assertEqual(len(blocks), 1, "Step 2b must be one fail-closed execution unit")
        return blocks[0]

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
            "- 次アクション: 終了コード 0 の標準出力を `$self_review=true|false` として保持し、Step 2.5 へ進む。",
            SEND_SKILL,
        )
        step2b = SEND_SKILL.index("### Step 2b: self-PR 検知（read-only）")
        self.assertGreater(step2b, SEND_SKILL.index("### Step 2: メタデータとレビューの読み込み"))
        self.assertLess(step2b, SEND_SKILL.index("### Step 2.5: plugin root / schema / validator path の解決"))

    def test_send_documents_fail_closed_abort_before_builder(self) -> None:
        # 受け入れ条件 6: identity 取得失敗時は builder / preflight を実行せず投稿前に中断する
        self.assertIn(
            "**投稿前に中断** する。builder / Step 4.5 preflight は実行せず、`sent/` 移動も行わない",
            SEND_SKILL,
        )
        self.assertIn(
            "- Step 2b の identity 取得（`gh api user` または PR 作者の取得）が非ゼロ終了または空出力 → 投稿前に中断する（fail-closed）",
            SEND_SKILL,
        )

    def test_send_step2b_identity_gate_executes_fail_closed_with_stub(self) -> None:
        # 受け入れ条件 6: SKILL の実テンプレートを fake gh で実行し、identity
        # 取得失敗時に後続の builder / preflight へ到達しないことを固定する。
        identity_gate = self.step2b_bash_block()
        self.assertNotIn("; then", identity_gate)
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            gh_stub = directory / "gh"
            gh_stub.write_text(
                """#!/bin/sh
printf '%s\\n' "$2" >> "$GH_CALLS"
if [ "$2" = "user" ]; then
  result="${GH_USER_RESULT:-ok}"
  login="${GH_USER_LOGIN:-reviewer}"
else
  result="${GH_PR_RESULT:-ok}"
  login="${GH_PR_LOGIN:-author}"
fi
if [ "$result" = "fail" ]; then
  exit 42
fi
if [ "$result" = "empty" ]; then
  exit 0
fi
printf '%s\\n' "$login"
""",
                encoding="utf-8",
            )
            gh_stub.chmod(0o755)
            calls_path = directory / "gh-calls.txt"
            stages_path = directory / "stages.txt"
            workflow = (
                f"{identity_gate}\n"
                "printf 'builder\\n' >> \"$STAGES\"\n"
                "printf 'preflight\\n' >> \"$STAGES\"\n"
            )

            def run_gate(
                *,
                user_result: str = "ok",
                pr_result: str = "ok",
                user_login: str = "reviewer",
                pr_login: str = "author",
            ) -> subprocess.CompletedProcess[str]:
                calls_path.unlink(missing_ok=True)
                stages_path.unlink(missing_ok=True)
                env = {
                    **os.environ,
                    "PATH": f"{directory}{os.pathsep}{os.environ.get('PATH', '')}",
                    "GH_CALLS": str(calls_path),
                    "STAGES": str(stages_path),
                    "GH_USER_RESULT": user_result,
                    "GH_PR_RESULT": pr_result,
                    "GH_USER_LOGIN": user_login,
                    "GH_PR_LOGIN": pr_login,
                    "org": "example",
                    "repository": "repo",
                    "pr_number": "137",
                }
                return subprocess.run(
                    ["/bin/bash", "-c", workflow],
                    check=False,
                    capture_output=True,
                    text=True,
                    env=env,
                )

            same_identity = run_gate(pr_login="reviewer")
            self.assertEqual(same_identity.returncode, 0, same_identity.stderr)
            self.assertEqual(same_identity.stdout, "true\n")
            self.assertEqual(calls_path.read_text(encoding="utf-8"), "user\nrepos/example/repo/pulls/137\n")
            self.assertEqual(stages_path.read_text(encoding="utf-8"), "builder\npreflight\n")

            different_identity = run_gate()
            self.assertEqual(different_identity.returncode, 0, different_identity.stderr)
            self.assertEqual(different_identity.stdout, "false\n")
            self.assertEqual(calls_path.read_text(encoding="utf-8"), "user\nrepos/example/repo/pulls/137\n")
            self.assertEqual(stages_path.read_text(encoding="utf-8"), "builder\npreflight\n")

            failures = (
                ({"user_result": "fail"}, "gh api user", "user\n"),
                ({"user_result": "empty"}, "gh api user", "user\n"),
                (
                    {"pr_result": "fail"},
                    "gh api repos/example/repo/pulls/137",
                    "user\nrepos/example/repo/pulls/137\n",
                ),
                (
                    {"pr_result": "empty"},
                    "gh api repos/example/repo/pulls/137",
                    "user\nrepos/example/repo/pulls/137\n",
                ),
            )
            for kwargs, failed_api, expected_calls in failures:
                with self.subTest(**kwargs):
                    failed = run_gate(**kwargs)
                    self.assertNotEqual(failed.returncode, 0)
                    self.assertIn(failed_api, failed.stderr)
                    self.assertIn("gh auth status", failed.stderr)
                    self.assertIn("再実行", failed.stderr)
                    self.assertEqual(calls_path.read_text(encoding="utf-8"), expected_calls)
                    self.assertFalse(stages_path.exists(), "builder / preflight must not run")

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

    def test_send_error_handling_preserves_self_review_event_precedence(self) -> None:
        self.assertIn(
            "`$self_review == true` なら CI 状態にかかわらず `event: COMMENT`",
            SEND_SKILL,
        )
        self.assertIn(
            "`$self_review == false` かつ CI 抑止が無い場合のみ Must Fix 0 件の結論として `event: APPROVE`",
            SEND_SKILL,
        )

    def test_footer_docstring_describes_self_review_comment(self) -> None:
        self.assertNotIn("presence is equivalent\n    to the public REQUEST_CHANGES event", BUILDER)
        self.assertRegex(BUILDER, r"For self-review\s+COMMENT payloads, the posted summary")

    def test_review_skill_mentions_self_pr_suppression(self) -> None:
        self.assertIn(
            "PR 作成者自身のアカウントで send を実行した場合（self-PR）、GitHub の制約により `APPROVE` / `REQUEST_CHANGES` は send 側で `COMMENT` に抑止される",
            REVIEW_SKILL,
        )


if __name__ == "__main__":
    unittest.main()
