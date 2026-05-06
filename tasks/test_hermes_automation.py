from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "hermes" / "scripts"
sys.path.insert(0, str(SCRIPTS))


def load_module(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


common = load_module("_pr_codex_common")
watch = load_module("pr_codex_watch")
health = load_module("pr_codex_kanban_health")


class HermesWatcherTests(unittest.TestCase):
    def empty_state(self):
        return {"schema_version": 1, "repo": "yuki777/pr-codex", "seen": {}, "tasks": []}

    def test_seeded_issue_is_idempotent_until_updated(self):
        state = self.empty_state()
        snapshot = {
            "issues": [
                {
                    "number": 28,
                    "title": "hermes-agent",
                    "created_at": "2026-05-06T00:30:00Z",
                    "updated_at": "2026-05-06T00:30:00Z",
                    "html_url": "https://github.com/yuki777/pr-codex/issues/28",
                }
            ],
            "pulls": [],
            "issue_comments": {28: []},
        }
        events = watch.collect_events(snapshot, state, repo="yuki777/pr-codex", detected_at="2026-05-06T01:00:00Z")
        self.assertEqual([event.kind for event in events], ["issue:new"])
        watch.process_events(
            events,
            state=state,
            sink="print",
            board="pr-codex",
            tenant="yuki777/pr-codex",
            outbox_path=Path("/tmp/unused.jsonl"),
            seed=True,
            dry_run=False,
            detected_at="2026-05-06T01:00:00Z",
        )
        self.assertEqual(
            watch.collect_events(snapshot, state, repo="yuki777/pr-codex", detected_at="2026-05-06T01:10:00Z"),
            [],
        )

        snapshot["issues"][0]["updated_at"] = "2026-05-06T01:11:00Z"
        events = watch.collect_events(snapshot, state, repo="yuki777/pr-codex", detected_at="2026-05-06T01:12:00Z")
        self.assertEqual([event.kind for event in events], ["issue:update"])

    def test_auto_comment_update_is_ignored(self):
        state = self.empty_state()
        state["seen"] = {
            "issue:new:#28": {"first_seen_at": "2026-05-06T00:30:00Z"},
            "issue:update:#28:2026-05-06T00:30:00Z": {"first_seen_at": "2026-05-06T00:30:00Z"},
        }
        snapshot = {
            "issues": [
                {
                    "number": 28,
                    "title": "hermes-agent",
                    "created_at": "2026-05-06T00:30:00Z",
                    "updated_at": "2026-05-06T00:40:00Z",
                    "html_url": "https://github.com/yuki777/pr-codex/issues/28",
                }
            ],
            "pulls": [],
            "issue_comments": {
                28: [
                    {
                        "id": 1,
                        "body": "<!-- hermes-auto:pr-codex pr-review v1 pr=25 head=abc -->\nDone",
                        "created_at": "2026-05-06T00:40:00Z",
                        "updated_at": "2026-05-06T00:40:00Z",
                    }
                ]
            },
        }
        events = watch.collect_events(snapshot, state, repo="yuki777/pr-codex", detected_at="2026-05-06T00:41:00Z")
        self.assertEqual(events, [])

    def test_pr_head_change_becomes_update_not_new(self):
        state = self.empty_state()
        state["seen"] = {
            "pr:new:#25:oldsha": {"first_seen_at": "2026-05-06T00:00:00Z"},
            "pr:update:#25:oldsha": {"first_seen_at": "2026-05-06T00:00:00Z"},
        }
        snapshot = {
            "issues": [],
            "pulls": [
                {
                    "number": 25,
                    "title": "canonical artifact",
                    "html_url": "https://github.com/yuki777/pr-codex/pull/25",
                    "updated_at": "2026-05-06T01:00:00Z",
                    "head": {"sha": "newsha123", "ref": "feat/25"},
                    "base": {"ref": "main"},
                }
            ],
            "reviews": {25: []},
            "pr_issue_comments": {25: []},
            "review_comments": {25: []},
            "review_threads": {25: []},
        }
        events = watch.collect_events(snapshot, state, repo="yuki777/pr-codex", detected_at="2026-05-06T01:01:00Z")
        self.assertEqual([event.kind for event in events], ["pr:update"])
        self.assertIn("head=newsha", events[0].task.title)

    def test_review_feedback_ignores_hermes_marker(self):
        state = self.empty_state()
        snapshot = {
            "issues": [],
            "pulls": [
                {
                    "number": 25,
                    "title": "review me",
                    "html_url": "https://github.com/yuki777/pr-codex/pull/25",
                    "head": {"sha": "abc1234", "ref": "feat/25"},
                    "base": {"ref": "main"},
                }
            ],
            "reviews": {25: []},
            "pr_issue_comments": {
                25: [
                    {"id": 10, "body": "human feedback", "html_url": "https://example.test/human"},
                    {"id": 11, "body": "<!-- hermes-auto:pr-codex --> bot", "html_url": "https://example.test/bot"},
                ]
            },
            "review_comments": {25: []},
            "review_threads": {25: []},
        }
        # Mark the PR itself as seen so this assertion focuses only on feedback.
        state["seen"] = {
            "pr:new:#25:abc1234": {"first_seen_at": "2026-05-06T00:00:00Z"},
            "pr:update:#25:abc1234": {"first_seen_at": "2026-05-06T00:00:00Z"},
        }
        events = watch.collect_events(snapshot, state, repo="yuki777/pr-codex", detected_at="2026-05-06T01:01:00Z")
        self.assertEqual([event.task.idempotency_key for event in events], ["issue_comment:new:#25:10"])

    def test_fetch_paginated_list_accumulates_all_pages(self):
        calls: list[str] = []
        original = watch.gh_json

        def fake_gh_json(args):
            calls.append(args[0])
            if args[0].endswith("page=1"):
                return [{"number": 1}, {"number": 2}]
            if args[0].endswith("page=2"):
                return [{"number": 3}]
            raise AssertionError(f"unexpected call: {args}")

        try:
            watch.gh_json = fake_gh_json
            actual = watch.fetch_paginated_list("repos/yuki777/pr-codex/issues?state=open", per_page=2)
        finally:
            watch.gh_json = original

        self.assertEqual(actual, [{"number": 1}, {"number": 2}, {"number": 3}])
        self.assertEqual(
            calls,
            [
                "repos/yuki777/pr-codex/issues?state=open&per_page=2&page=1",
                "repos/yuki777/pr-codex/issues?state=open&per_page=2&page=2",
            ],
        )


class CommonHelperTests(unittest.TestCase):
    def test_kanban_command_uses_board_assignee_and_idempotency_key(self):
        task = common.KanbanTask(
            title="[pr-review] #25 head=abc review me",
            assignee="pr-reviewer",
            body="body",
            idempotency_key="pr:update:#25:abc",
            metadata={"event_kind": "pr:update"},
            priority=1,
        )
        command = common.build_kanban_create_command(task, board="pr-codex", tenant="yuki777/pr-codex")
        self.assertEqual(command[:5], ["hermes", "kanban", "--board", "pr-codex", "create"])
        self.assertIn("--assignee", command)
        self.assertEqual(command[command.index("--assignee") + 1], "pr-reviewer")
        self.assertIn("--idempotency-key", command)
        self.assertEqual(command[command.index("--idempotency-key") + 1], "pr:update:#25:abc")

    def test_outbox_sink_writes_jsonl(self):
        task = common.KanbanTask(
            title="[issue-triage] #28 hermes-agent",
            assignee="issue-triager",
            body="body",
            idempotency_key="issue:new:#28",
            metadata={"event_kind": "issue:new"},
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            outbox = Path(tmpdir) / "tasks.jsonl"
            result = common.create_task_with_sink(task, sink="outbox", outbox_path=outbox)
            self.assertEqual(result["sink"], "outbox")
            rows = [json.loads(line) for line in outbox.read_text().splitlines()]
        self.assertEqual(rows[0]["idempotency_key"], "issue:new:#28")
        self.assertEqual(rows[0]["assignee"], "issue-triager")


class HealthTests(unittest.TestCase):
    def test_health_detects_blocked_stale_and_retry_tasks(self):
        now = datetime(2026, 5, 6, 2, 0, tzinfo=timezone.utc)
        old = (now - timedelta(minutes=120)).isoformat().replace("+00:00", "Z")
        tasks = [
            {"id": "t1", "title": "running", "status": "running", "assignee": "developer", "started_at": old},
            {"id": "t2", "title": "blocked", "status": "blocked", "assignee": "review-triager", "reason": "needs input"},
            {"id": "t3", "title": "retry", "status": "ready", "assignee": "pr-reviewer", "created_at": old, "retry_count": 3},
        ]
        report = health.evaluate_health(tasks, now=now, running_minutes=90, ready_minutes=60, retry_threshold=3)
        self.assertEqual([item["id"] for item in report["stale_running"]], ["t1"])
        self.assertEqual([item["id"] for item in report["blocked"]], ["t2"])
        self.assertEqual([item["id"] for item in report["high_retry"]], ["t3"])
        self.assertEqual([item["id"] for item in report["stale_ready"]], ["t3"])

    def test_health_accepts_numeric_epoch_timestamps(self):
        now = datetime(2026, 5, 6, 2, 0, tzinfo=timezone.utc)
        old = int((now - timedelta(minutes=120)).timestamp())
        old_ms = old * 1000
        tasks = [
            {"id": "t1", "title": "running", "status": "running", "started_at": old},
            {"id": "t2", "title": "ready", "status": "ready", "created_at": old_ms},
        ]
        report = health.evaluate_health(tasks, now=now, running_minutes=90, ready_minutes=60, retry_threshold=3)
        self.assertEqual([item["id"] for item in report["stale_running"]], ["t1"])
        self.assertEqual([item["id"] for item in report["stale_ready"]], ["t2"])


if __name__ == "__main__":
    unittest.main()
