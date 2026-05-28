#!/usr/bin/env python3
"""Executable documentation checks for Issue #96 send approve behavior."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "tasks"
SEND_SKILL = ROOT / "skills" / "send" / "SKILL.md"
REVIEW_SKILL = ROOT / "skills" / "review" / "SKILL.md"
README = ROOT / "README.md"
sys.path.insert(0, str(TASKS))

from validate_preflight_result import validate_preflight_result  # noqa: E402


def section(text: str, start: str, end: str) -> str:
    start_index = text.index(start)
    end_index = text.index(end, start_index)
    return text[start_index:end_index]


class Issue96DocsTest(unittest.TestCase):
    def test_send_payload_event_approves_clean_reviews_with_ci_suppression(self) -> None:
        text = SEND_SKILL.read_text(encoding="utf-8")
        step4 = section(text, "### Step 4: payload の構築", "### Step 4.5:")

        for snippet in (
            '1 件以上あれば `"REQUEST_CHANGES"`',
            'Must Fix が 0 件、かつ `ci-status.json.state` が `failure` / `pending` ではない場合は `"APPROVE"`',
            'Must Fix が 0 件、かつ `ci-status.json.state` が `failure` / `pending` の場合は `"COMMENT"` に抑止',
            '## 確認した範囲',
            "`metadata.json.files[]`",
            "`review.md` / `run-plan.json` / `ci-summary.md`",
            "推測した観点を混ぜない",
            "## CI 状態",
            'event: "APPROVE"` + body (総評 + 良い点 + 確認した範囲)',
        ):
            self.assertIn(snippet, step4)

        self.assertNotIn('0 件なら `"COMMENT"`', step4)
        self.assertNotIn("`\"APPROVE\"` は自動では発行しない", step4)

    def test_preflight_event_rule_matches_approve_behavior(self) -> None:
        text = SEND_SKILL.read_text(encoding="utf-8")
        step45 = section(text, "### Step 4.5:", "### Step 5:")

        for snippet in (
            "Must Fix 0件かつ ci-status.json.state が failure または pending → COMMENT",
            "Must Fix 0件かつ ci-status.json.state が success・skipped・未取得 → APPROVE",
            "payload.event が APPROVE の場合",
            "payload.body に '## 確認した範囲'",
            "confirmation_scope_body_mismatch",
        ):
            self.assertIn(snippet, step45)

    def test_step5_and_error_handling_surface_selected_approve_event(self) -> None:
        text = SEND_SKILL.read_text(encoding="utf-8")
        step5 = section(text, "### Step 5:", "### Step 5.5:")
        errors = section(text, "## エラーハンドリング", "## 実装上の制約")

        for snippet in (
            "event: <REQUEST_CHANGES | APPROVE | COMMENT>",
            "CI gate: <success|failure|pending|skipped|未取得>",
            "failure/pending の場合は APPROVE 抑止",
        ):
            self.assertIn(snippet, step5)

        self.assertIn("Must Fix 0 件の結論として `event: APPROVE`", errors)
        self.assertIn("`ci-status.json.state` が `failure` / `pending` の場合は `event: COMMENT`", errors)

    def test_review_and_readme_document_approve_result(self) -> None:
        review = REVIEW_SKILL.read_text(encoding="utf-8")
        readme = README.read_text(encoding="utf-8")

        for snippet in (
            "総評＋良い点＋確認した範囲の APPROVE レビュー",
            "承認レビューを投稿する場合のみ",
            "CI が failure / pending の場合は send 側で COMMENT に抑止",
        ):
            self.assertIn(snippet, review)

        for snippet in (
            "Must Fix 0 件なら `APPROVE`",
            "`ci-status.json.state` が `failure` / `pending` の場合は自動 `APPROVE` を抑止",
            "CI 状態を body と Step 5 に表示する",
        ):
            self.assertIn(snippet, readme)

        self.assertNotIn("`APPROVE` は自動では出さない", readme)

    def test_confirmation_scope_preflight_rule_is_known(self) -> None:
        result = {
            "schema_version": "preflight-result.v1",
            "verdict": "FAIL",
            "stages": {
                "schema_validation": {"status": "PASS"},
                "range_validation": {"status": "PASS"},
                "semantic_preflight": {"status": "PASS"},
                "payload_consistency": {"status": "FAIL"},
            },
            "violations": [
                {
                    "stage": "payload_consistency",
                    "rule": "confirmation_scope_body_mismatch",
                    "detail": "APPROVE body is missing the reviewed scope section",
                    "severity": "error",
                    "auto_fixable": True,
                    "requires_review_regeneration": False,
                }
            ],
            "auto_fixable_count": 1,
            "requires_human_count": 0,
            "generated_at": "2026-05-28T00:00:00Z",
        }

        self.assertEqual(validate_preflight_result(result), [])


if __name__ == "__main__":
    unittest.main()
