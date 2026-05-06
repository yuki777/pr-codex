from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
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
publisher = load_module("issue_triager_publish")
watch = load_module("pr_codex_watch")


class FakeIssueCommentClient:
    def __init__(self, comments=None):
        self.comments = list(comments or [])
        self.posted: list[dict[str, object]] = []
        self.edit_issue_calls = 0
        self.label_calls = 0
        self.milestone_calls = 0
        self.assignee_calls = 0
        self.close_calls = 0
        self.lock_calls = 0

    def list_issue_comments(self, repo: str, issue: int):
        return list(self.comments)

    def add_issue_comment(self, repo: str, issue: int, body: str):
        comment = {
            "id": 1000 + len(self.posted),
            "body": body,
            "user": {"login": "yuki777"},
            "created_at": "2026-05-06T00:40:00Z",
            "updated_at": "2026-05-06T00:40:00Z",
        }
        self.posted.append({"repo": repo, "issue": issue, "body": body})
        self.comments.append(comment)
        return comment

    # Forbidden side-effect methods: publisher must never call these.
    def edit_issue(self, *args, **kwargs):  # pragma: no cover - should not run
        self.edit_issue_calls += 1

    def set_labels(self, *args, **kwargs):  # pragma: no cover - should not run
        self.label_calls += 1

    def set_milestone(self, *args, **kwargs):  # pragma: no cover - should not run
        self.milestone_calls += 1

    def set_assignees(self, *args, **kwargs):  # pragma: no cover - should not run
        self.assignee_calls += 1

    def close_issue(self, *args, **kwargs):  # pragma: no cover - should not run
        self.close_calls += 1

    def lock_issue(self, *args, **kwargs):  # pragma: no cover - should not run
        self.lock_calls += 1


class IssueTriagerPublishTests(unittest.TestCase):
    def empty_state(self):
        return {"schema_version": 1, "repo": "yuki777/pr-codex", "seen": {}, "tasks": []}

    def base_payload(self, summary="Define safe issue triage publication policy."):
        return {
            "issue_number": 43,
            "classification": "feature",
            "priority": "medium",
            "suggested_labels": ["enhancement"],
            "dependencies": [28],
            "ready": True,
            "public_summary": summary,
            "recommended_next_action": "Keep GitHub publication recommendation-only and default-off.",
        }

    def enabled_env(self):
        return {publisher.PUBLISH_ENV_FLAG: "1"}

    def test_idempotency_same_scrub_hash_posts_once(self):
        state = self.empty_state()
        client = FakeIssueCommentClient()

        first = publisher.publish_issue_triage(
            self.base_payload(),
            state=state,
            client=client,
            dry_run=False,
            sink="github",
            env=self.enabled_env(),
        )
        second = publisher.publish_issue_triage(
            self.base_payload(),
            state=state,
            client=client,
            dry_run=False,
            sink="github",
            env=self.enabled_env(),
        )

        self.assertEqual(first["action"], "published")
        self.assertEqual(second["action"], "skip")
        self.assertEqual(second["skip_reason"], "already-published")
        self.assertEqual(len(client.posted), 1)
        self.assertIn(first["idempotency_key"], state["seen"])

    def test_content_change_appends_new_sentinel_without_editing_existing(self):
        state = self.empty_state()
        client = FakeIssueCommentClient()

        first = publisher.publish_issue_triage(
            self.base_payload("Initial conclusion."),
            state=state,
            client=client,
            dry_run=False,
            sink="github",
            env=self.enabled_env(),
        )
        second = publisher.publish_issue_triage(
            self.base_payload("Updated conclusion after new dependency analysis."),
            state=state,
            client=client,
            dry_run=False,
            sink="github",
            env=self.enabled_env(),
        )

        self.assertEqual(first["action"], "published")
        self.assertEqual(second["action"], "published")
        self.assertNotEqual(first["scrub_hash"], second["scrub_hash"])
        self.assertIn(f"hash={first['scrub_hash']}", client.posted[0]["body"])
        self.assertIn(f"hash={second['scrub_hash']}", client.posted[1]["body"])
        self.assertEqual(len(client.posted), 2)
        self.assertEqual(client.edit_issue_calls, 0)

    def test_trusted_self_comment_is_existing_publication(self):
        state = self.empty_state()
        plan = publisher.build_publication_plan(
            self.base_payload(),
            repo="yuki777/pr-codex",
            issue=43,
            comments=[],
            state=state,
        )
        trusted_comment = {
            "id": 7,
            "body": plan["body"],
            "user": {"login": "yuki777"},
        }
        client = FakeIssueCommentClient([trusted_comment])

        report = publisher.publish_issue_triage(
            self.base_payload(),
            state=state,
            client=client,
            dry_run=False,
            sink="github",
            env=self.enabled_env(),
        )

        self.assertEqual(report["action"], "skip")
        self.assertEqual(report["skip_reason"], "already-published")
        self.assertEqual(report["duplicate_comment_id"], 7)
        self.assertEqual(len(client.posted), 0)

    def test_untrusted_spoofed_sentinel_does_not_dedupe(self):
        state = self.empty_state()
        plan = publisher.build_publication_plan(
            self.base_payload(),
            repo="yuki777/pr-codex",
            issue=43,
            comments=[],
            state=state,
        )
        spoofed_comment = {
            "id": 8,
            "body": plan["body"],
            "user": {"login": "outside-contributor"},
        }
        client = FakeIssueCommentClient([spoofed_comment])

        report = publisher.publish_issue_triage(
            self.base_payload(),
            state=state,
            client=client,
            dry_run=False,
            sink="github",
            env=self.enabled_env(),
        )

        self.assertEqual(report["action"], "published")
        self.assertEqual(len(client.posted), 1)

    def test_scrub_for_public_redacts_secrets_paths_logs_and_operational_details(self):
        raw = "\n".join(
            [
                "sk-abcdefghijklmnopqrstuvwxyz123456",
                "ghp_abcdefghijklmnopqrstuvwxyz1234567890",
                "Authorization: Bearer abc.def-ghi_jkl",
                "OPENAI_API_KEY=super-secret",
                "See /Users/adachi/.agent-orchestrator/projects/pr-codex and /home/bot/work",
                "Cache under ~/.hermes/automation/pr-codex/state.json",
                "Webhook IP 192.168.0.1",
                "Kanban task t_051678e3 from pc-21",
                "```log\nraw private log\n```",
            ]
        )

        scrubbed, categories = common.scrub_for_public(raw)

        self.assertNotIn("abcdefghijklmnopqrstuvwxyz123456", scrubbed)
        self.assertNotIn("super-secret", scrubbed)
        self.assertNotIn("adachi", scrubbed)
        self.assertNotIn("bot/work", scrubbed)
        self.assertNotIn("192.168.0.1", scrubbed)
        self.assertNotIn("t_051678e3", scrubbed)
        self.assertGreaterEqual(len(categories), 6)
        self.assertIn("openai_secret", categories)
        self.assertIn("github_token", categories)
        self.assertIn("bearer_token", categories)
        self.assertIn("env_secret", categories)
        self.assertIn("local_private_path", categories)
        self.assertIn("agent_orchestrator_path", categories)
        self.assertIn("hermes_private_path", categories)
        self.assertIn("raw_log_or_payload", categories)
        self.assertIn("hermes_task_id", categories)

    def test_all_redacted_skip_does_not_post(self):
        state = self.empty_state()
        client = FakeIssueCommentClient()

        report = publisher.publish_issue_triage(
            {"issue_number": 43, "public_summary": "sk-abcdefghijklmnopqrstuvwxyz123456"},
            state=state,
            client=client,
            dry_run=False,
            sink="github",
            env=self.enabled_env(),
        )

        self.assertEqual(report["action"], "skip")
        self.assertEqual(report["skip_reason"], "all-redacted")
        self.assertEqual(len(client.posted), 0)

    def test_disabled_by_default_does_not_call_github_post(self):
        state = self.empty_state()
        client = FakeIssueCommentClient()

        report = publisher.publish_issue_triage(
            self.base_payload(),
            state=state,
            client=client,
            dry_run=False,
            sink="github",
            env={},
        )

        self.assertEqual(report["action"], "skip")
        self.assertEqual(report["skip_reason"], "disabled")
        self.assertFalse(report["github_writes_enabled"])
        self.assertEqual(len(client.posted), 0)

    def test_forbidden_issue_edit_side_effects_are_not_called(self):
        state = self.empty_state()
        client = FakeIssueCommentClient()

        report = publisher.publish_issue_triage(
            self.base_payload(),
            state=state,
            client=client,
            dry_run=False,
            sink="github",
            env=self.enabled_env(),
        )

        self.assertEqual(report["action"], "published")
        self.assertEqual(client.edit_issue_calls, 0)
        self.assertEqual(client.label_calls, 0)
        self.assertEqual(client.milestone_calls, 0)
        self.assertEqual(client.assignee_calls, 0)
        self.assertEqual(client.close_calls, 0)
        self.assertEqual(client.lock_calls, 0)

    def test_watcher_ignores_trusted_issue_triage_publication_update(self):
        state = self.empty_state()
        plan = publisher.build_publication_plan(
            self.base_payload(),
            repo="yuki777/pr-codex",
            issue=43,
            comments=[],
            state=state,
        )
        watcher_state = self.empty_state()
        watcher_state["seen"] = {
            "issue:new:#43": {"first_seen_at": "2026-05-06T00:30:00Z"},
            "issue:update:#43:2026-05-06T00:30:00Z": {"first_seen_at": "2026-05-06T00:30:00Z"},
        }
        snapshot = {
            "issues": [
                {
                    "number": 43,
                    "title": "Hermes Phase 1B",
                    "created_at": "2026-05-06T00:30:00Z",
                    "updated_at": "2026-05-06T00:40:00Z",
                    "html_url": "https://github.com/yuki777/pr-codex/issues/43",
                }
            ],
            "pulls": [],
            "issue_comments": {
                43: [
                    {
                        "id": 9,
                        "body": plan["body"],
                        "user": {"login": "yuki777"},
                        "created_at": "2026-05-06T00:40:00Z",
                        "updated_at": "2026-05-06T00:40:00Z",
                    }
                ]
            },
        }

        events = watch.collect_events(
            snapshot,
            watcher_state,
            repo="yuki777/pr-codex",
            detected_at="2026-05-06T00:41:00Z",
        )

        self.assertEqual(events, [])

    def test_cli_dry_run_json_is_machine_readable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            triage_path = Path(tmpdir) / "triage.json"
            triage_path.write_text(json.dumps(self.base_payload()), encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "issue_triager_publish.py"),
                    "--triage",
                    str(triage_path),
                    "--json",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )

        payload = json.loads(result.stdout)
        self.assertEqual(payload["action"], "dry-run")
        self.assertEqual(payload["skip_reason"], "disabled")
        self.assertIn("sentinel", payload)
        self.assertIn("idempotency_key", payload)


if __name__ == "__main__":
    unittest.main()
