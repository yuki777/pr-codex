#!/usr/bin/env python3
"""Unit tests for pr_codex_developer_bridge.py.

These tests are intentionally pure: they do not call gh, git, or hermes.
"""
from __future__ import annotations

import json
import unittest
from unittest import mock

import pr_codex_developer_bridge as bridge


class DeveloperBridgeTests(unittest.TestCase):
    def test_pick_next_issue_respects_tracker_exclusions_pr_closures_and_order(self) -> None:
        issues = [
            {"number": 15, "title": "roadmap", "url": "https://example/issues/15", "labels": []},
            {"number": 43, "title": "publication policy", "url": "https://example/issues/43", "labels": []},
            {"number": 37, "title": "F7 outputs", "url": "https://example/issues/37", "labels": []},
            {"number": 36, "title": "F11 eval gate", "url": "https://example/issues/36", "labels": []},
        ]
        prs = [
            {"number": 44, "title": "policy", "body": "Closes #43", "url": "https://example/pull/44"},
        ]
        tasks = []

        chosen = bridge.pick_next_issue(issues, prs, tasks, explicit_order=[37, 36, 43])

        self.assertIsNotNone(chosen)
        self.assertEqual(chosen["number"], 37)

    def test_pick_next_issue_skips_existing_developer_task_for_issue(self) -> None:
        issues = [
            {"number": 36, "title": "F11 eval gate", "url": "https://example/issues/36", "labels": []},
            {"number": 37, "title": "F7 outputs", "url": "https://example/issues/37", "labels": []},
        ]
        tasks = [
            {"id": "t_existing", "title": "[developer] #36 F11 eval gate", "assignee": "developer", "status": "ready"},
        ]

        chosen = bridge.pick_next_issue(issues, [], tasks, explicit_order=[36, 37])

        self.assertIsNotNone(chosen)
        self.assertEqual(chosen["number"], 37)

    def test_bridge_defers_when_open_pr_or_active_developer_capacity_is_exhausted(self) -> None:
        issues = [{"number": 36, "title": "F11 eval gate", "url": "https://example/issues/36", "labels": []}]
        open_prs = [{"number": 44, "title": "policy", "body": "Closes #43", "url": "https://example/pull/44"}]
        active_dev_tasks = [{"id": "t_active", "title": "[developer] #37", "assignee": "developer", "status": "running"}]

        self.assertEqual(bridge.plan_next_action(issues, open_prs, [], max_open_prs=1)["action"], "defer_open_pr_capacity")
        self.assertEqual(bridge.plan_next_action(issues, [], active_dev_tasks, max_active_developer_tasks=1)["action"], "defer_developer_capacity")

    def test_build_task_body_contains_autonomous_safety_and_tdd_contract(self) -> None:
        issue = {"number": 36, "title": "F11 eval gate", "url": "https://github.com/yuki777/pr-codex/issues/36", "labels": ["enhancement"]}

        body = bridge.build_task_body(issue)

        self.assertIn("PUBLIC REPO SAFETY", body)
        self.assertIn("strict TDD", body)
        self.assertIn("Do not merge the PR yourself", body)
        self.assertIn("Do not push to main", body)
        self.assertIn("Closes #36", body)
        self.assertIn("python3 -m unittest discover -s tasks -p 'test_*.py'", body)

    def test_create_developer_task_uses_worktree_skills_and_idempotency(self) -> None:
        issue = {"number": 36, "title": "F11 eval gate", "url": "https://github.com/yuki777/pr-codex/issues/36", "labels": ["enhancement"]}
        calls = []

        def fake_run(cmd, input_text=None):
            calls.append(cmd)
            return bridge.Completed(0, json.dumps({"task_id": "t_new"}), "")

        created = bridge.create_developer_task(issue, run_cmd=fake_run)

        self.assertEqual(created["task_id"], "t_new")
        cmd = calls[0]
        self.assertIn("--workspace", cmd)
        self.assertEqual(cmd[cmd.index("--workspace") + 1], "worktree")
        self.assertIn("--assignee", cmd)
        self.assertEqual(cmd[cmd.index("--assignee") + 1], "developer")
        self.assertIn("--idempotency-key", cmd)
        self.assertEqual(cmd[cmd.index("--idempotency-key") + 1], "developer:auto:issue:yuki777/pr-codex:36:v1")
        self.assertEqual(cmd.count("--skill"), 2)
        self.assertIn("test-driven-development", cmd)
        self.assertIn("github-pr-workflow", cmd)

    def test_find_must_fix_review_for_current_head_ignores_stale_and_no_blocking(self) -> None:
        pr = {"number": 44, "head_sha": "abc123", "head_ref": "feat/43", "title": "policy"}
        comments = [
            {"body": "<!-- hermes-auto:pr-codex pr-review v1 pr=44 head=old -->\n\nVerdict: Must Fix", "url": "stale"},
            {"body": "<!-- hermes-auto:pr-codex pr-review v1 pr=44 head=abc123 -->\n\nNo blocking findings for this head. I did not identify Must Fix / High issues.", "url": "ok"},
            {"body": "<!-- hermes-auto:pr-codex pr-review v1 pr=44 head=abc123 -->\n\nVerdict: Changes recommended\n\n### Must Fix\n- enforce allow-list", "url": "fix"},
        ]

        review = bridge.find_must_fix_review_for_pr(pr, comments, [])

        self.assertIsNotNone(review)
        self.assertEqual(review["url"], "fix")
        self.assertIn("Must Fix", review["body"])

    def test_find_must_fix_review_returns_none_for_no_blocking_comment(self) -> None:
        pr = {"number": 47, "head_sha": "abc123", "head_ref": "feat/40", "title": "pipeline"}
        comments = [{"body": "<!-- hermes-auto:pr-codex pr-review v1 pr=47 head=abc123 -->\n\nNo blocking findings for this head. I did not identify Must Fix / High-confidence issues.", "url": "ok"}]

        self.assertIsNone(bridge.find_must_fix_review_for_pr(pr, comments, []))

    def test_find_must_fix_review_understands_japanese_review_comments(self) -> None:
        pr = {"number": 51, "head_sha": "abc123", "head_ref": "feat/42", "title": "clusters"}
        comments = [
            {"body": "<!-- hermes-auto:pr-codex pr-review v1 pr=51 head=abc123 -->\n\n## Hermes 自動レビュー\n\n判定: ブロッカーなし。要修正 (Must Fix) はありません。", "url": "ok"},
            {"body": "<!-- hermes-auto:pr-codex pr-review v1 pr=51 head=abc123 -->\n\n## Hermes 自動レビュー\n\n### 要修正 (Must Fix)\n- root-cause cluster の validator を追加してください。", "url": "fix"},
        ]

        review = bridge.find_must_fix_review_for_pr(pr, comments, [])

        self.assertIsNotNone(review)
        self.assertEqual(review["url"], "fix")

    def test_windows_only_path_finding_is_not_actionable_by_default(self) -> None:
        pr = {"number": 60, "head_sha": "abc123", "head_ref": "feat/54", "title": "learn"}
        comments = [
            {
                "body": "<!-- hermes-auto:pr-codex pr-review v1 pr=60 head=abc123 -->\n\n### 要修正 (Must Fix)\n- `tasks/learn_feedback.py` の `LOCAL_PATH_RE` が Windows の drive-root / UNC パスを scrub できていません。",
                "url": "windows-only",
            }
        ]

        self.assertIsNone(bridge.find_must_fix_review_for_pr(pr, comments, []))

    def test_human_scope_correction_after_must_fix_suppresses_repair_task(self) -> None:
        pr = {"number": 60, "head_sha": "abc123", "head_ref": "feat/54", "title": "learn"}
        comments = [
            {
                "body": "<!-- hermes-auto:pr-codex pr-review v1 pr=60 head=abc123 -->\n\n### 要修正 (Must Fix)\n- Windows path handling を直してください。",
                "url": "fix",
                "created_at": "2026-05-08T07:00:00Z",
            },
            {
                "body": "Windows対応関連はすべて対応不要です",
                "url": "human-correction",
                "created_at": "2026-05-08T07:08:00Z",
            },
        ]

        self.assertIsNone(bridge.find_must_fix_review_for_pr(pr, comments, []))

    def test_pick_next_review_fix_skips_existing_repair_task(self) -> None:
        pr = {"number": 44, "head_sha": "abc123", "head_ref": "feat/43", "title": "policy"}
        review = {"body": "<!-- hermes-auto:pr-codex pr-review v1 pr=44 head=abc123 -->\n### Must Fix\n- fix it", "url": "fix"}
        tasks = [{"id": "t_fix", "title": "[developer] PR #44 Must Fix", "assignee": "developer", "status": "ready", "body": "Head under review: abc123"}]

        self.assertIsNone(bridge.pick_next_review_fix([pr], {44: review}, tasks))
        self.assertEqual(bridge.pick_next_review_fix([pr], {44: review}, [])["pr"]["number"], 44)

    def test_build_review_fix_task_body_contains_pr_branch_comment_and_tdd_contract(self) -> None:
        pr = {"number": 44, "head_sha": "abc123", "head_ref": "feat/43", "title": "policy", "url": "https://github.com/yuki777/pr-codex/pull/44"}
        review = {"body": "### Must Fix\n- enforce allow-list", "url": "https://github.com/yuki777/pr-codex/pull/44#issuecomment-1"}

        body = bridge.build_review_fix_task_body(pr, review)

        self.assertIn("PR: #44", body)
        self.assertIn("Branch: feat/43", body)
        self.assertIn("Head under review: abc123", body)
        self.assertIn("issuecomment-1", body)
        self.assertIn("strict TDD", body)
        self.assertIn("Do not merge PR #44", body)
        self.assertIn("Do not push to main", body)

    def test_create_review_fix_task_uses_pr_head_idempotency(self) -> None:
        pr = {"number": 44, "head_sha": "abc123", "head_ref": "feat/43", "title": "policy", "url": "https://github.com/yuki777/pr-codex/pull/44"}
        review = {"body": "### Must Fix\n- enforce allow-list", "url": "https://github.com/yuki777/pr-codex/pull/44#issuecomment-1"}
        calls = []

        def fake_run(cmd, input_text=None):
            calls.append(cmd)
            return bridge.Completed(0, json.dumps({"task_id": "t_fix"}), "")

        created = bridge.create_review_fix_task(pr, review, run_cmd=fake_run)

        self.assertEqual(created["task_id"], "t_fix")
        cmd = calls[0]
        self.assertEqual(cmd[cmd.index("--idempotency-key") + 1], "developer:auto:review-fix:yuki777/pr-codex:44:abc123:v1")
        self.assertEqual(cmd[cmd.index("--workspace") + 1], "worktree")
        self.assertIn("test-driven-development", cmd)
        self.assertIn("github-pr-workflow", cmd)


if __name__ == "__main__":
    unittest.main()

