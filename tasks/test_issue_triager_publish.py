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
        self.list_calls: list[dict[str, object]] = []
        self.edit_issue_calls = 0
        self.label_calls = 0
        self.milestone_calls = 0
        self.assignee_calls = 0
        self.close_calls = 0
        self.lock_calls = 0

    def list_issue_comments(self, repo: str, issue: int):
        self.list_calls.append({"repo": repo, "issue": issue})
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

    def base_payload(self, next_action="Keep GitHub publication recommendation-only and default-off."):
        return {
            "issue_number": 43,
            "classification": "feature",
            "priority": "medium",
            "suggested_labels": ["enhancement"],
            "dependencies": [28],
            "ready": True,
            "recommended_next_action": next_action,
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
            {"issue_number": 43, "recommended_next_action": "sk-abcdefghijklmnopqrstuvwxyz123456"},
            state=state,
            client=client,
            dry_run=False,
            sink="github",
            env=self.enabled_env(),
        )

        self.assertEqual(report["action"], "skip")
        self.assertEqual(report["skip_reason"], "all-redacted")
        self.assertEqual(len(client.posted), 0)

    def test_protected_prefix_pat_assignment_is_redacted_from_next_action(self):
        state = self.empty_state()
        client = FakeIssueCommentClient()

        report = publisher.publish_issue_triage(
            self.base_payload("Use GH_PAT=fine-grained-value to publish."),
            state=state,
            client=client,
            dry_run=False,
            sink="github",
            env=self.enabled_env(),
        )

        self.assertEqual(report["action"], "published")
        self.assertEqual(len(client.posted), 1)
        body = client.posted[0]["body"]
        self.assertNotIn("GH_PAT", body)
        self.assertNotIn("fine-grained-value", body)
        self.assertIn("env_secret", report["redactions"])
        self.assertIn(
            {"field": "recommended_next_action", "reason": "redacted_content"},
            report["policy_omissions"],
        )

    def test_env_style_token_assignments_are_redacted_from_next_action(self):
        state = self.empty_state()
        client = FakeIssueCommentClient()

        report = publisher.publish_issue_triage(
            self.base_payload("Rotate GITHUB_TOKEN=ghp_example and ACTIONS_RUNTIME_TOKEN='runtime-secret'."),
            state=state,
            client=client,
            dry_run=False,
            sink="github",
            env=self.enabled_env(),
        )

        self.assertEqual(report["action"], "published")
        body = client.posted[0]["body"]
        self.assertNotIn("GITHUB_TOKEN", body)
        self.assertNotIn("ACTIONS_RUNTIME_TOKEN", body)
        self.assertNotIn("ghp_example", body)
        self.assertNotIn("runtime-secret", body)
        self.assertIn("env_secret", report["redactions"])
        self.assertIn(
            {"field": "recommended_next_action", "reason": "redacted_content"},
            report["policy_omissions"],
        )

    def test_leading_underscore_and_space_delimited_token_guidance_is_omitted(self):
        state = self.empty_state()
        client = FakeIssueCommentClient()

        report = publisher.publish_issue_triage(
            self.base_payload("_GITHUB_TOKEN=leading-secret then run with GITHUB_TOKEN ghp_example."),
            state=state,
            client=client,
            dry_run=False,
            sink="github",
            env=self.enabled_env(),
        )

        self.assertEqual(report["action"], "published")
        body = client.posted[0]["body"]
        self.assertNotIn("_GITHUB_TOKEN", body)
        self.assertNotIn("GITHUB_TOKEN", body)
        self.assertNotIn("leading-secret", body)
        self.assertNotIn("ghp_example", body)
        self.assertIn("env_secret", report["redactions"])
        self.assertIn(
            {"field": "recommended_next_action", "reason": "redacted_content"},
            report["policy_omissions"],
        )

    def test_sensitive_env_identifier_without_value_is_forbidden_public_text(self):
        state = self.empty_state()
        client = FakeIssueCommentClient()

        report = publisher.publish_issue_triage(
            self.base_payload("Rotate GITHUB_TOKEN."),
            state=state,
            client=client,
            dry_run=False,
            sink="github",
            env=self.enabled_env(),
        )

        self.assertEqual(report["action"], "published")
        body = client.posted[0]["body"]
        self.assertNotIn("GITHUB_TOKEN", body)
        self.assertIn(
            {"field": "recommended_next_action", "reason": "forbidden_public_terms"},
            report["policy_omissions"],
        )

    def test_cross_repo_issue_refs_are_omitted_before_publication(self):
        state = self.empty_state()
        client = FakeIssueCommentClient()

        report = publisher.publish_issue_triage(
            {
                **self.base_payload(),
                "dependencies": [
                    28,
                    "yuki777/pr-codex#29",
                    "private-org/private-repo#7",
                    "https://github.com/private-org/private-repo/issues/8",
                    "https://github.com/yuki777/pr-codex/issues/30",
                ],
                "related_issues": ["#31", "private-org/private-repo#9"],
            },
            state=state,
            repo="yuki777/pr-codex",
            client=client,
            dry_run=False,
            sink="github",
            env=self.enabled_env(),
        )

        self.assertEqual(report["action"], "published")
        self.assertEqual(len(client.posted), 1)
        body = client.posted[0]["body"]
        self.assertIn("Dependencies: #28, #29, #30", body)
        self.assertIn("related: #31", body)
        self.assertNotIn("private-org", body)
        self.assertNotIn("private-repo", body)
        self.assertNotIn("#7", body)
        self.assertNotIn("#8", body)
        self.assertNotIn("#9", body)
        omitted_fields = [item["field"] for item in report["policy_omissions"]]
        self.assertGreaterEqual(omitted_fields.count("dependencies"), 2)
        self.assertIn("related_issues", omitted_fields)

    def test_non_allowlisted_public_fields_do_not_publish_by_themselves(self):
        state = self.empty_state()
        client = FakeIssueCommentClient()

        report = publisher.publish_issue_triage(
            {
                "issue_number": 43,
                "classification": "security-but-public-raw",
                "priority": "ship after private escalation",
                "suggested_labels": ["prod<secret>"],
                "dependencies": ["internal task after t_051678e3"],
                "summary": "Publish the raw GraphQL payload from /Users/adachi/tmp.",
                "recommended_next_action": "Use OPENAI_API_KEY=secret-value from the Hermes task.",
            },
            state=state,
            client=client,
            dry_run=False,
            sink="github",
            env=self.enabled_env(),
        )

        self.assertEqual(report["action"], "skip")
        self.assertEqual(report["skip_reason"], "no-policy-approved-content")
        self.assertEqual(len(client.posted), 0)
        omitted_fields = {item["field"] for item in report["policy_omissions"]}
        self.assertIn("classification", omitted_fields)
        self.assertIn("priority", omitted_fields)
        self.assertIn("suggested_labels", omitted_fields)
        self.assertIn("dependencies", omitted_fields)
        self.assertIn("summary", omitted_fields)
        self.assertIn("recommended_next_action", omitted_fields)

    def test_generic_summary_alone_is_not_publication_content(self):
        state = self.empty_state()
        client = FakeIssueCommentClient()

        report = publisher.publish_issue_triage(
            {
                "issue_number": 43,
                "summary": "This generic Kanban summary should stay private even when it looks harmless.",
            },
            state=state,
            client=client,
            dry_run=False,
            sink="github",
            env=self.enabled_env(),
        )

        self.assertEqual(report["action"], "skip")
        self.assertEqual(report["skip_reason"], "no-policy-approved-content")
        self.assertEqual(len(client.posted), 0)
        self.assertNotIn("This generic Kanban summary", report["body"])
        self.assertIn("summary", {item["field"] for item in report["policy_omissions"]})

    def test_malformed_optional_fields_are_omitted_when_safe_fields_remain(self):
        state = self.empty_state()
        client = FakeIssueCommentClient()

        report = publisher.publish_issue_triage(
            {
                "issue_number": 43,
                "classification": "feature",
                "priority": "after private customer escalation",
                "suggested_labels": ["enhancement", "bad<label>"],
                "dependencies": [28, "internal roadmap item"],
                "summary": "Document the publication policy.",
                "recommended_next_action": "Keep the publisher disabled by default.",
                # Existing issue labels are not publication proposals and must be ignored.
                "labels": ["do-not-publish-as-suggestion"],
            },
            state=state,
            client=client,
            dry_run=False,
            sink="github",
            env=self.enabled_env(),
        )

        self.assertEqual(report["action"], "published")
        self.assertEqual(len(client.posted), 1)
        body = client.posted[0]["body"]
        self.assertIn("Classification: `feature`", body)
        self.assertIn("Suggested labels (proposal only): `enhancement`", body)
        self.assertIn("Dependencies: #28", body)
        self.assertIn("Keep the publisher disabled by default.", body)
        self.assertNotIn("Document the publication policy.", body)
        self.assertNotIn("after private customer", body)
        self.assertNotIn("bad<label>", body)
        self.assertNotIn("internal roadmap", body)
        self.assertNotIn("do-not-publish-as-suggestion", body)
        omitted_fields = {item["field"] for item in report["policy_omissions"]}
        self.assertIn("priority", omitted_fields)
        self.assertIn("suggested_labels", omitted_fields)
        self.assertIn("dependencies", omitted_fields)
        self.assertIn("summary", omitted_fields)

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

    def test_payload_repo_cannot_redirect_github_publish_target(self):
        state = self.empty_state()
        client = FakeIssueCommentClient()
        payload = {**self.base_payload(), "repo": "attacker/other-repo"}

        report = publisher.publish_issue_triage(
            payload,
            state=state,
            repo="yuki777/pr-codex",
            client=client,
            dry_run=False,
            sink="github",
            env=self.enabled_env(),
        )

        self.assertEqual(report["action"], "skip")
        self.assertEqual(report["skip_reason"], "repo-mismatch")
        self.assertEqual(report["repo"], "yuki777/pr-codex")
        self.assertEqual(report["payload_repo"], "attacker/other-repo")
        self.assertEqual(client.list_calls, [])
        self.assertEqual(client.posted, [])

    def test_matching_payload_repo_uses_configured_repo(self):
        state = self.empty_state()
        client = FakeIssueCommentClient()
        payload = {**self.base_payload(), "repo": "yuki777/pr-codex"}

        report = publisher.publish_issue_triage(
            payload,
            state=state,
            repo="yuki777/pr-codex",
            client=client,
            dry_run=False,
            sink="github",
            env=self.enabled_env(),
        )

        self.assertEqual(report["action"], "published")
        self.assertEqual(client.list_calls, [{"repo": "yuki777/pr-codex", "issue": 43}])
        self.assertEqual(client.posted[0]["repo"], "yuki777/pr-codex")

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
