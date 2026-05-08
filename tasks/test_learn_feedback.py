from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "tasks"
README = ROOT / "README.md"
LEARN_SKILL = ROOT / "skills" / "learn" / "SKILL.md"


def load_module(name: str):
    spec = importlib.util.spec_from_file_location(name, TASKS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


learn_feedback = load_module("learn_feedback")


class LearnFeedbackTests(unittest.TestCase):
    def test_learn_skill_falls_back_when_plugin_root_env_is_unset(self):
        text = LEARN_SKILL.read_text(encoding="utf-8")

        self.assertNotIn("${CLAUDE_PLUGIN_ROOT:?", text)
        self.assertIn('if [ -z "${CLAUDE_PLUGIN_ROOT:-}" ]', text)
        self.assertIn("skills/learn/SKILL.md", text)
        self.assertIn('CLAUDE_PLUGIN_ROOT="$(cd "$CLAUDE_PLUGIN_ROOT" && pwd)"', text)

    def test_readme_manual_learn_example_uses_plugin_root_helper_path(self):
        text = README.read_text(encoding="utf-8")

        self.assertIn("python3 $CLAUDE_PLUGIN_ROOT/tasks/learn_feedback.py", text)
        self.assertNotIn("python3 tasks/learn_feedback.py", text)

    def test_readme_manual_learn_example_is_pasteable_shell(self):
        text = README.read_text(encoding="utf-8")
        command = next(
            line
            for line in text.splitlines()
            if "tasks/learn_feedback.py" in line and "--output-dir" in line
        )

        self.assertNotIn("<org>", command)
        self.assertNotIn("<repo>", command)
        self.assertNotIn("<pr>", command)
        result = subprocess.run(
            ["bash", "-n"],
            input=command,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def fixture_payload(self):
        return {
            "repository": "yuki777/pr-codex",
            "pr_number": 54,
            "head_sha": "abc123def456",
            "review_threads": [
                {
                    "id": "PRRT_resolved_1",
                    "isResolved": True,
                    "isOutdated": False,
                    "path": "README.md",
                    "line": 42,
                    "comments": {
                        "nodes": [
                            {
                                "id": "PRRC_resolved_1",
                                "body": "Must Fix: README の説明が古いです。",
                                "url": "https://github.test/review/resolved",
                                "author": {"login": "chatgpt-codex-connector"},
                            }
                        ]
                    },
                },
                {
                    "id": "PRRT_outdated_1",
                    "isResolved": False,
                    "isOutdated": True,
                    "path": "tasks/example.py",
                    "line": 7,
                    "comments": {
                        "nodes": [
                            {
                                "id": "PRRC_outdated_1",
                                "body": "High: 古い差分上の指摘です。",
                                "author": {"login": "chatgpt-codex-connector"},
                            }
                        ]
                    },
                },
                {
                    "id": "PRRT_open_1",
                    "isResolved": False,
                    "isOutdated": False,
                    "path": "tasks/open.py",
                    "line": 99,
                    "comments": {
                        "nodes": [
                            {
                                "id": "PRRC_open_1",
                                "body": "誤検知として明示された指摘",
                                "author": {"login": "chatgpt-codex-connector"},
                            }
                        ]
                    },
                },
                {
                    "id": "PRRT_silent_1",
                    "isResolved": False,
                    "isOutdated": False,
                    "path": "tasks/silent.py",
                    "line": 100,
                    "comments": {
                        "nodes": [
                            {
                                "id": "PRRC_silent_1",
                                "body": "author 無反応のため学習しない",
                                "author": {"login": "chatgpt-codex-connector"},
                            }
                        ]
                    },
                },
            ],
            "labels": [{"name": "pr-codex/false-positive"}],
            "comments": [
                {
                    "id": 9001,
                    "body": "pr-codex/false-positive: PRRT_open_1 は誤検知です。token ghp_abcdefghijklmnopqrstuvwxyz123456 と /home/adachi/private.txt は保存しないでください。",
                    "html_url": "https://github.test/pr/54#issuecomment-9001",
                    "user": {"login": "yuki777"},
                }
            ],
        }

    def test_build_feedback_learning_result_defaults_generated_at_to_current_utc_when_omitted(self):
        before = datetime.now(timezone.utc)

        result, _artifacts = learn_feedback.build_feedback_learning_result(self.fixture_payload())

        generated_at = result["generated_at"]
        self.assertIsInstance(generated_at, str)
        self.assertRegex(generated_at, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        parsed = datetime.strptime(generated_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        after = datetime.now(timezone.utc)
        self.assertGreaterEqual(parsed, before.replace(microsecond=0))
        self.assertLessEqual(parsed, after)

    def test_build_feedback_learning_result_classifies_only_explicit_signals(self):
        result, artifacts = learn_feedback.build_feedback_learning_result(
            self.fixture_payload(), generated_at="2026-05-08T00:00:00Z"
        )

        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["repository"], "yuki777/pr-codex")
        self.assertEqual(result["pr_number"], 54)
        self.assertEqual(result["head_sha"], "abc123def456")
        self.assertEqual(result["summary"], {"addressed": 1, "superseded": 1, "false_positive": 1, "ignored": 1})
        self.assertEqual([artifact["signal"] for artifact in artifacts], ["addressed", "superseded", "false_positive"])
        self.assertEqual({artifact["thread_id"] for artifact in artifacts}, {"PRRT_resolved_1", "PRRT_outdated_1", "PRRT_open_1"})
        ignored = result["ignored_threads"]
        self.assertEqual(ignored, [{"thread_id": "PRRT_silent_1", "reason": "no_explicit_learning_signal"}])

    def test_build_feedback_learning_result_unwraps_review_threads_keyed_by_pr_number(self):
        payload = self.fixture_payload()
        payload["review_threads"] = {
            "53": [
                {
                    "id": "PRRT_other_pr",
                    "isResolved": True,
                    "isOutdated": False,
                    "comments": {"nodes": [{"author": {"login": "chatgpt-codex-connector"}}]},
                }
            ],
            "54": payload["review_threads"],
        }

        result, artifacts = learn_feedback.build_feedback_learning_result(
            payload, generated_at="2026-05-08T00:00:00Z"
        )

        self.assertEqual(
            result["summary"],
            {"addressed": 1, "superseded": 1, "false_positive": 1, "ignored": 1},
        )
        self.assertEqual(
            {artifact["thread_id"] for artifact in artifacts},
            {"PRRT_resolved_1", "PRRT_outdated_1", "PRRT_open_1"},
        )
        self.assertNotIn("PRRT_other_pr", {artifact["thread_id"] for artifact in artifacts})

    def test_build_feedback_learning_result_unwraps_watcher_pr_issue_comments_for_current_pr(self):
        payload = self.fixture_payload()
        payload["comments"] = []
        payload["labels"] = []
        payload["pulls"] = [
            {"number": 53, "labels": [{"name": "unrelated"}]},
            {"number": 54, "labels": [{"name": "pr-codex/false-positive"}]},
        ]
        payload["pr_issue_comments"] = {
            "53": [
                {
                    "id": 9000,
                    "body": "pr-codex/false-positive: PRRT_other_pr は誤検知です。",
                    "html_url": "https://github.test/pr/53#issuecomment-9000",
                    "user": {"login": "yuki777"},
                }
            ],
            "54": [
                {
                    "id": 9001,
                    "body": "pr-codex/false-positive: PRRT_open_1 は誤検知です。",
                    "html_url": "https://github.test/pr/54#issuecomment-9001",
                    "user": {"login": "yuki777"},
                }
            ],
        }
        payload["review_threads"] = {"54": payload["review_threads"]}

        result, artifacts = learn_feedback.build_feedback_learning_result(
            payload, generated_at="2026-05-08T00:00:00Z"
        )

        self.assertEqual(result["summary"], {"addressed": 1, "superseded": 1, "false_positive": 1, "ignored": 1})
        artifact_by_thread = {artifact["thread_id"]: artifact for artifact in artifacts}
        self.assertEqual(artifact_by_thread["PRRT_open_1"]["signal"], "false_positive")
        self.assertEqual(artifact_by_thread["PRRT_open_1"]["feedback_comment_id"], 9001)
        self.assertNotIn("PRRT_other_pr", artifact_by_thread)

    def test_build_feedback_learning_result_prefers_explicit_false_positive_over_resolved_thread(self):
        payload = self.fixture_payload()
        payload["review_threads"][0]["id"] = "PRRT_open_1"
        payload["review_threads"][0]["isResolved"] = True
        payload["review_threads"][0]["isOutdated"] = False
        payload["review_threads"][2]["id"] = "PRRT_open_without_signal"

        result, artifacts = learn_feedback.build_feedback_learning_result(
            payload, generated_at="2026-05-08T00:00:00Z"
        )

        self.assertEqual(result["summary"], {"addressed": 0, "superseded": 1, "false_positive": 1, "ignored": 2})
        artifact_by_thread = {artifact["thread_id"]: artifact for artifact in artifacts}
        self.assertEqual(artifact_by_thread["PRRT_open_1"]["signal"], "false_positive")
        self.assertEqual(artifact_by_thread["PRRT_open_1"]["source"], "label_comment.false_positive")

    def test_build_feedback_learning_result_prefers_label_only_false_positive_over_resolved_or_outdated_threads(
        self,
    ):
        payload = self.fixture_payload()
        payload["comments"] = []

        result, artifacts = learn_feedback.build_feedback_learning_result(
            payload, generated_at="2026-05-08T00:00:00Z"
        )

        self.assertEqual(result["summary"], {"addressed": 0, "superseded": 0, "false_positive": 2, "ignored": 2})
        artifact_by_thread = {artifact["thread_id"]: artifact for artifact in artifacts}
        self.assertEqual(artifact_by_thread["PRRT_resolved_1"]["signal"], "false_positive")
        self.assertEqual(artifact_by_thread["PRRT_resolved_1"]["source"], "pr_label.false_positive")
        self.assertNotIn("feedback_comment_id", artifact_by_thread["PRRT_resolved_1"])
        self.assertEqual(artifact_by_thread["PRRT_outdated_1"]["signal"], "false_positive")
        self.assertEqual(artifact_by_thread["PRRT_outdated_1"]["source"], "pr_label.false_positive")
        self.assertIn(
            {"thread_id": "PRRT_open_1", "reason": "no_explicit_learning_signal"},
            result["ignored_threads"],
        )

    def test_build_feedback_learning_result_ignores_untrusted_false_positive_issue_comment(self):
        payload = self.fixture_payload()
        payload["labels"] = []
        payload["comments"][0]["user"]["login"] = "fork-contributor"
        payload["comments"][0]["authorAssociation"] = "CONTRIBUTOR"

        result, artifacts = learn_feedback.build_feedback_learning_result(
            payload, generated_at="2026-05-08T00:00:00Z"
        )

        self.assertEqual(result["summary"], {"addressed": 1, "superseded": 1, "false_positive": 0, "ignored": 2})
        self.assertNotIn("PRRT_open_1", {artifact["thread_id"] for artifact in artifacts})
        self.assertIn(
            {"thread_id": "PRRT_open_1", "reason": "no_explicit_learning_signal"},
            result["ignored_threads"],
        )

    def test_build_feedback_learning_result_binds_false_positive_issue_comment_to_each_target(self):
        payload = self.fixture_payload()
        payload["comments"][0]["body"] = "pr-codex/false-positive: PRRT_open_1 applies, not PRRT_silent_1"

        result, artifacts = learn_feedback.build_feedback_learning_result(
            payload, generated_at="2026-05-08T00:00:00Z"
        )

        self.assertEqual(result["summary"], {"addressed": 1, "superseded": 1, "false_positive": 1, "ignored": 1})
        artifact_by_thread = {artifact["thread_id"]: artifact for artifact in artifacts}
        self.assertEqual(artifact_by_thread["PRRT_open_1"]["signal"], "false_positive")
        self.assertNotIn("PRRT_silent_1", artifact_by_thread)
        self.assertIn(
            {"thread_id": "PRRT_silent_1", "reason": "no_explicit_learning_signal"},
            result["ignored_threads"],
        )

    def test_build_feedback_learning_result_excludes_not_for_marker_targets(self):
        payload = self.fixture_payload()
        payload["comments"][0]["body"] = "pr-codex/false-positive: PRRT_open_1, not for PRRT_silent_1"

        result, artifacts = learn_feedback.build_feedback_learning_result(
            payload, generated_at="2026-05-08T00:00:00Z"
        )

        self.assertEqual(result["summary"], {"addressed": 1, "superseded": 1, "false_positive": 1, "ignored": 1})
        artifact_by_thread = {artifact["thread_id"]: artifact for artifact in artifacts}
        self.assertEqual(artifact_by_thread["PRRT_open_1"]["signal"], "false_positive")
        self.assertNotIn("PRRT_silent_1", artifact_by_thread)
        self.assertIn(
            {"thread_id": "PRRT_silent_1", "reason": "no_explicit_learning_signal"},
            result["ignored_threads"],
        )

    def test_build_feedback_learning_result_accepts_documented_space_separated_false_positive_issue_comment(self):
        payload = self.fixture_payload()
        payload["comments"][0]["body"] = "pr-codex/false-positive PRRT_open_1 は誤検知です。"

        result, artifacts = learn_feedback.build_feedback_learning_result(
            payload, generated_at="2026-05-08T00:00:00Z"
        )

        self.assertEqual(result["summary"], {"addressed": 1, "superseded": 1, "false_positive": 1, "ignored": 1})
        artifact_by_thread = {artifact["thread_id"]: artifact for artifact in artifacts}
        self.assertEqual(artifact_by_thread["PRRT_open_1"]["signal"], "false_positive")
        self.assertEqual(artifact_by_thread["PRRT_open_1"]["source"], "label_comment.false_positive")

    def test_build_feedback_learning_result_accepts_false_positive_targets_on_following_bullet_lines(self):
        payload = self.fixture_payload()
        payload["comments"][0]["body"] = "pr-codex/false-positive:\n- PRRT_open_1\n- not for PRRT_silent_1"

        result, artifacts = learn_feedback.build_feedback_learning_result(
            payload, generated_at="2026-05-08T00:00:00Z"
        )

        self.assertEqual(result["summary"], {"addressed": 1, "superseded": 1, "false_positive": 1, "ignored": 1})
        artifact_by_thread = {artifact["thread_id"]: artifact for artifact in artifacts}
        self.assertEqual(artifact_by_thread["PRRT_open_1"]["signal"], "false_positive")
        self.assertNotIn("PRRT_silent_1", artifact_by_thread)

    def test_build_feedback_learning_result_accepts_explicit_false_positive_issue_comment_without_label(self):
        payload = self.fixture_payload()
        payload["labels"] = []

        result, artifacts = learn_feedback.build_feedback_learning_result(
            payload, generated_at="2026-05-08T00:00:00Z"
        )

        self.assertEqual(result["summary"], {"addressed": 1, "superseded": 1, "false_positive": 1, "ignored": 1})
        artifact_by_thread = {artifact["thread_id"]: artifact for artifact in artifacts}
        self.assertEqual(artifact_by_thread["PRRT_open_1"]["signal"], "false_positive")
        self.assertEqual(artifact_by_thread["PRRT_open_1"]["source"], "label_comment.false_positive")

    def test_build_feedback_learning_result_accepts_false_positive_reply_in_review_thread(self):
        payload = self.fixture_payload()
        payload["comments"] = []
        payload["review_threads"][2]["comments"]["nodes"].append(
            {
                "id": "PRRC_false_positive_reply",
                "body": "pr-codex/false-positive: この review thread は誤検知です。",
                "url": "https://github.test/review/false-positive-reply",
                "author": {"login": "yuki777"},
            }
        )

        result, artifacts = learn_feedback.build_feedback_learning_result(
            payload, generated_at="2026-05-08T00:00:00Z"
        )

        self.assertEqual(result["summary"], {"addressed": 1, "superseded": 1, "false_positive": 1, "ignored": 1})
        artifact_by_thread = {artifact["thread_id"]: artifact for artifact in artifacts}
        artifact = artifact_by_thread["PRRT_open_1"]
        self.assertEqual(artifact["signal"], "false_positive")
        self.assertEqual(artifact["source"], "review_thread_comment.false_positive")
        self.assertEqual(artifact["feedback_comment_id"], "PRRC_false_positive_reply")
        self.assertEqual(artifact["feedback_comment_url"], "https://github.test/review/false-positive-reply")
        self.assertEqual(artifact["feedback_comment_excerpt"], "pr-codex/false-positive: この review thread は誤検知です。")

    def test_build_feedback_learning_result_accepts_owner_reply_with_mixed_case_login(self):
        payload = self.fixture_payload()
        payload["comments"] = []
        payload["repository"] = "yuki777/pr-codex"
        payload["review_threads"][2]["comments"]["nodes"].append(
            {
                "id": "PRRC_false_positive_reply",
                "body": "pr-codex/false-positive: この review thread は誤検知です。",
                "url": "https://github.test/review/false-positive-reply",
                "author": {"login": "Yuki777"},
            }
        )

        result, artifacts = learn_feedback.build_feedback_learning_result(
            payload, generated_at="2026-05-08T00:00:00Z"
        )

        self.assertEqual(result["summary"], {"addressed": 1, "superseded": 1, "false_positive": 1, "ignored": 1})
        artifact_by_thread = {artifact["thread_id"]: artifact for artifact in artifacts}
        self.assertEqual(artifact_by_thread["PRRT_open_1"]["signal"], "false_positive")
        self.assertEqual(artifact_by_thread["PRRT_open_1"]["source"], "review_thread_comment.false_positive")

    def test_build_feedback_learning_result_ignores_untrusted_false_positive_review_reply(self):
        payload = self.fixture_payload()
        payload["comments"] = []
        payload["labels"] = []
        payload["review_threads"][2]["comments"]["nodes"].append(
            {
                "id": "PRRC_untrusted_false_positive_reply",
                "body": "pr-codex/false-positive: valid finding を消したいです。",
                "url": "https://github.test/review/untrusted-false-positive-reply",
                "author": {"login": "fork-contributor"},
                "authorAssociation": "CONTRIBUTOR",
            }
        )

        result, artifacts = learn_feedback.build_feedback_learning_result(
            payload, generated_at="2026-05-08T00:00:00Z"
        )

        self.assertEqual(result["summary"], {"addressed": 1, "superseded": 1, "false_positive": 0, "ignored": 2})
        self.assertNotIn("PRRT_open_1", {artifact["thread_id"] for artifact in artifacts})
        self.assertIn(
            {"thread_id": "PRRT_open_1", "reason": "no_explicit_learning_signal"},
            result["ignored_threads"],
        )

    def test_build_feedback_learning_result_ignores_false_positive_label_mentions_in_review_replies(self):
        payload = self.fixture_payload()
        payload["comments"] = []
        payload["labels"] = []
        payload["review_threads"][2]["isResolved"] = True
        payload["review_threads"][2]["comments"]["nodes"].append(
            {
                "id": "PRRC_explanatory_reply",
                "body": "pr-codex/false-positive のラベル処理を修正しました。",
                "url": "https://github.test/review/explanatory-reply",
                "author": {"login": "yuki777"},
            }
        )

        result, artifacts = learn_feedback.build_feedback_learning_result(
            payload, generated_at="2026-05-08T00:00:00Z"
        )

        self.assertEqual(result["summary"], {"addressed": 2, "superseded": 1, "false_positive": 0, "ignored": 1})
        artifact_by_thread = {artifact["thread_id"]: artifact for artifact in artifacts}
        self.assertEqual(artifact_by_thread["PRRT_open_1"]["signal"], "addressed")
        self.assertEqual(artifact_by_thread["PRRT_open_1"]["source"], "review_thread.resolved")

    def test_build_feedback_learning_result_honors_custom_review_author_allowlist(self):
        payload = self.fixture_payload()
        payload["review_author"] = "custom-pr-codex-bot"
        payload["review_threads"][0]["comments"]["nodes"][0]["author"]["login"] = "custom-pr-codex-bot"

        result, artifacts = learn_feedback.build_feedback_learning_result(
            payload, generated_at="2026-05-08T00:00:00Z"
        )

        self.assertEqual(result["summary"], {"addressed": 1, "superseded": 0, "false_positive": 0, "ignored": 3})
        self.assertEqual([artifact["thread_id"] for artifact in artifacts], ["PRRT_resolved_1"])
        self.assertIn(
            {"thread_id": "PRRT_outdated_1", "reason": "not_pr_codex_review_thread"},
            result["ignored_threads"],
        )
        self.assertIn(
            {"thread_id": "PRRT_open_1", "reason": "not_pr_codex_review_thread"},
            result["ignored_threads"],
        )

    def test_build_feedback_learning_result_matches_review_authors_case_insensitively(self):
        payload = self.fixture_payload()
        payload["review_author"] = "ChatGPT-Codex-Connector"

        result, artifacts = learn_feedback.build_feedback_learning_result(
            payload, generated_at="2026-05-08T00:00:00Z"
        )

        self.assertEqual(result["summary"], {"addressed": 1, "superseded": 1, "false_positive": 1, "ignored": 1})
        self.assertEqual(
            {artifact["thread_id"] for artifact in artifacts},
            {"PRRT_resolved_1", "PRRT_outdated_1", "PRRT_open_1"},
        )
        self.assertEqual(
            result["ignored_threads"],
            [{"thread_id": "PRRT_silent_1", "reason": "no_explicit_learning_signal"}],
        )

    def test_build_feedback_learning_result_ignores_other_reviewers_threads(self):
        payload = self.fixture_payload()
        payload["review_threads"].append(
            {
                "id": "PRRT_human_resolved",
                "isResolved": True,
                "isOutdated": False,
                "path": "tasks/other.py",
                "line": 12,
                "comments": {
                    "nodes": [
                        {
                            "id": "PRRC_human_resolved",
                            "body": "別レビュアーの resolved 指摘なので pr-codex 学習対象ではない",
                            "url": "https://github.test/review/human-resolved",
                            "author": {"login": "human-reviewer"},
                        }
                    ]
                },
            }
        )

        result, artifacts = learn_feedback.build_feedback_learning_result(
            payload, generated_at="2026-05-08T00:00:00Z"
        )

        self.assertEqual(result["summary"], {"addressed": 1, "superseded": 1, "false_positive": 1, "ignored": 2})
        self.assertNotIn("PRRT_human_resolved", {artifact["thread_id"] for artifact in artifacts})
        self.assertIn(
            {"thread_id": "PRRT_human_resolved", "reason": "not_pr_codex_review_thread"},
            result["ignored_threads"],
        )

    def test_feedback_artifacts_are_public_safe_and_written_idempotently(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = learn_feedback.write_feedback_artifacts(
                self.fixture_payload(), output_dir=Path(tmpdir), generated_at="2026-05-08T00:00:00Z"
            )
            learn_result = json.loads((Path(tmpdir) / "learn-result.json").read_text())
            artifact_paths = sorted((Path(tmpdir) / "feedback-artifacts").glob("*.json"))
            artifact_text = "\n".join(path.read_text() for path in artifact_paths)

        self.assertEqual(result["artifact_count"], 3)
        self.assertEqual(learn_result["artifact_count"], 3)
        self.assertEqual([path.name for path in artifact_paths], [
            "addressed-PRRT_resolved_1.json",
            "false_positive-PRRT_open_1.json",
            "superseded-PRRT_outdated_1.json",
        ])
        self.assertNotIn("ghp_", artifact_text)
        self.assertNotIn("/home/adachi", artifact_text)
        self.assertIn("[REDACTED_TOKEN]", artifact_text)
        self.assertIn("[REDACTED_LOCAL_PATH]", artifact_text)

    def test_feedback_artifacts_sanitize_comments_before_truncating_excerpts(self):
        payload = self.fixture_payload()
        token = "ghp_" + ("a" * 40)
        payload["review_threads"][0]["comments"]["nodes"][0]["body"] = (
            ("x" * 994) + " " + token
        )

        _result, artifacts = learn_feedback.build_feedback_learning_result(
            payload, generated_at="2026-05-08T00:00:00Z"
        )
        artifact_by_thread = {artifact["thread_id"]: artifact for artifact in artifacts}
        excerpt = artifact_by_thread["PRRT_resolved_1"]["comment_excerpts"][0]

        self.assertNotIn("ghp_", excerpt)
        self.assertLessEqual(len(excerpt), 1000)

    def test_feedback_artifacts_redact_common_raw_bearer_tokens_without_key_names(self):
        payload = self.fixture_payload()
        openai_token = "sk" + "-proj-" + "abc123DEF456ghi789JKL012mno345PQR678stu901"
        gitlab_token = "gl" + "pat-" + "abcDEF1234567890abcd"
        bearer_token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ" + ("a" * 24)
        payload["review_threads"][0]["comments"]["nodes"][0]["body"] = (
            f"resolved with OpenAI key {openai_token}, GitLab token {gitlab_token}, "
            f"and header Authorization: Bearer {bearer_token}"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            learn_feedback.write_feedback_artifacts(
                payload, output_dir=Path(tmpdir), generated_at="2026-05-08T00:00:00Z"
            )
            artifact_text = "\n".join(path.read_text() for path in (Path(tmpdir) / "feedback-artifacts").glob("*.json"))

        self.assertNotIn("sk-proj-", artifact_text)
        self.assertNotIn("glpat-", artifact_text)
        self.assertNotIn("Authorization: Bearer", artifact_text)
        self.assertNotIn("eyJhbGci", artifact_text)
        self.assertIn("[REDACTED_TOKEN]", artifact_text)

    def test_feedback_artifacts_redact_basic_authorization_headers(self):
        payload = self.fixture_payload()
        basic_token = "dXNlcjpwYXNz" + ("A" * 24)
        payload["review_threads"][0]["comments"]["nodes"][0]["body"] = (
            f"pasted curl output includes Authorization: Basic {basic_token}"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            learn_feedback.write_feedback_artifacts(
                payload, output_dir=Path(tmpdir), generated_at="2026-05-08T00:00:00Z"
            )
            artifact_text = "\n".join(path.read_text() for path in (Path(tmpdir) / "feedback-artifacts").glob("*.json"))

        self.assertNotIn("Authorization: Basic", artifact_text)
        self.assertNotIn(basic_token, artifact_text)
        self.assertIn("[REDACTED_TOKEN]", artifact_text)

    def test_feedback_artifacts_redact_common_raw_service_tokens_without_key_names(self):
        payload = self.fixture_payload()
        slack_bot_token = "xoxb-" + "123456789012-1234567890123-abcdefghijklmnopqrstuvwxyz"
        slack_user_token = "xoxp-" + "123456789012-123456789012-123456789012-abcdef1234567890"
        stripe_live_key = "sk_live_" + "A" * 24
        stripe_restricted_key = "rk_live_" + "B" * 24
        payload["review_threads"][0]["comments"]["nodes"][0]["body"] = (
            f"service logs include Slack bot {slack_bot_token}, user {slack_user_token}, "
            f"Stripe secret {stripe_live_key}, and restricted {stripe_restricted_key}"
        )
        payload["comments"][0]["body"] = (
            f"pr-codex/false-positive: PRRT_open_1 included {stripe_live_key} and {slack_bot_token}"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            learn_feedback.write_feedback_artifacts(
                payload, output_dir=Path(tmpdir), generated_at="2026-05-08T00:00:00Z"
            )
            artifact_text = "\n".join(path.read_text() for path in (Path(tmpdir) / "feedback-artifacts").glob("*.json"))

        for raw_token in (slack_bot_token, slack_user_token, stripe_live_key, stripe_restricted_key):
            self.assertNotIn(raw_token, artifact_text)
        self.assertNotIn("xoxb-", artifact_text)
        self.assertNotIn("xoxp-", artifact_text)
        self.assertNotIn("sk_live_", artifact_text)
        self.assertNotIn("rk_live_", artifact_text)
        self.assertIn("[REDACTED_TOKEN]", artifact_text)

    def test_feedback_artifacts_redact_npm_and_google_api_keys_without_key_names(self):
        payload = self.fixture_payload()
        npm_token = "npm_" + "a" * 36
        google_api_key = "AIza" + "A" * 35
        payload["review_threads"][0]["comments"]["nodes"][0]["body"] = (
            f"package publish log includes npm token {npm_token} and Google API key {google_api_key}"
        )
        payload["comments"][0]["body"] = (
            f"pr-codex/false-positive: PRRT_open_1 copied {npm_token} and {google_api_key}"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            learn_feedback.write_feedback_artifacts(
                payload, output_dir=Path(tmpdir), generated_at="2026-05-08T00:00:00Z"
            )
            artifact_text = "\n".join(path.read_text() for path in (Path(tmpdir) / "feedback-artifacts").glob("*.json"))

        self.assertNotIn(npm_token, artifact_text)
        self.assertNotIn(google_api_key, artifact_text)
        self.assertNotIn("npm_", artifact_text)
        self.assertNotIn("AIza", artifact_text)
        self.assertIn("[REDACTED_TOKEN]", artifact_text)

    def test_feedback_artifacts_redact_url_userinfo_credentials(self):
        payload = self.fixture_payload()
        credential_url = "https://user:pw@example.com/private/path?debug=true"
        payload["review_threads"][0]["comments"]["nodes"][0]["body"] = (
            f"proxy log includes {credential_url} after retry"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            learn_feedback.write_feedback_artifacts(
                payload, output_dir=Path(tmpdir), generated_at="2026-05-08T00:00:00Z"
            )
            artifact_text = "\n".join(path.read_text() for path in (Path(tmpdir) / "feedback-artifacts").glob("*.json"))

        self.assertNotIn("user:pw", artifact_text)
        self.assertNotIn(credential_url, artifact_text)
        self.assertIn("https://[REDACTED_TOKEN]@example.com/private/path?debug=true", artifact_text)

    def test_feedback_artifacts_redact_session_cookie_headers(self):
        payload = self.fixture_payload()
        payload["review_threads"][0]["comments"]["nodes"][0]["body"] = (
            "copied HTTP log includes Cookie: sessionid=session-cookie-value; theme=dark "
            "and Set-Cookie: pr_codex_session=set-cookie-value; HttpOnly"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            learn_feedback.write_feedback_artifacts(
                payload, output_dir=Path(tmpdir), generated_at="2026-05-08T00:00:00Z"
            )
            artifact_text = "\n".join(path.read_text() for path in (Path(tmpdir) / "feedback-artifacts").glob("*.json"))

        self.assertNotIn("Cookie: sessionid=session-cookie-value", artifact_text)
        self.assertNotIn("Set-Cookie: pr_codex_session=set-cookie-value", artifact_text)
        self.assertNotIn("session-cookie-value", artifact_text)
        self.assertNotIn("set-cookie-value", artifact_text)
        self.assertIn("[REDACTED_TOKEN]", artifact_text)

    def test_feedback_artifacts_redact_quoted_cookie_header_fields(self):
        payload = self.fixture_payload()
        payload["review_threads"][0]["comments"]["nodes"][0]["body"] = (
            'structured headers include {"Cookie": "sessionid=json-cookie-value; theme=dark", '
            '"Set-Cookie": "sid=json-set-cookie-value; HttpOnly"}'
        )
        payload["comments"][0]["body"] = (
            'pr-codex/false-positive: PRRT_open_1 copied {"Cookie": "open-json-cookie"}'
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            learn_feedback.write_feedback_artifacts(
                payload, output_dir=Path(tmpdir), generated_at="2026-05-08T00:00:00Z"
            )
            artifact_text = "\n".join(path.read_text() for path in (Path(tmpdir) / "feedback-artifacts").glob("*.json"))

        for raw_cookie in (
            "json-cookie-value",
            "json-set-cookie-value",
            "open-json-cookie",
        ):
            self.assertNotIn(raw_cookie, artifact_text)
        self.assertIn("[REDACTED_TOKEN]", artifact_text)

    def test_feedback_artifacts_redact_password_assignments_and_aws_access_key_ids(self):
        payload = self.fixture_payload()
        aws_access_key_id = "AK" + "IA" + "ABCDEFGHIJKLMNOP"
        payload["review_threads"][0]["comments"]["nodes"][0]["body"] = (
            f"logs include password=hunter2 and AWS access key {aws_access_key_id}"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            learn_feedback.write_feedback_artifacts(
                payload, output_dir=Path(tmpdir), generated_at="2026-05-08T00:00:00Z"
            )
            artifact_text = "\n".join(path.read_text() for path in (Path(tmpdir) / "feedback-artifacts").glob("*.json"))

        self.assertNotIn("password=hunter2", artifact_text)
        self.assertNotIn("hunter2", artifact_text)
        self.assertNotIn("AKIA", artifact_text)
        self.assertIn("[REDACTED_TOKEN]", artifact_text)

    def test_feedback_artifacts_redact_quoted_secret_assignments(self):
        payload = self.fixture_payload()
        payload["review_threads"][0]["comments"]["nodes"][0]["body"] = (
            'config copied from logs: {"password": "hunter2", "api_key": "value123"} '
            'and yaml_secret: "correct horse battery staple"'
        )
        payload["comments"][0]["body"] = (
            'pr-codex/false-positive: PRRT_open_1 includes password="open sesame" '
            'and "secret_token": "quoted token value"'
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            learn_feedback.write_feedback_artifacts(
                payload, output_dir=Path(tmpdir), generated_at="2026-05-08T00:00:00Z"
            )
            artifact_text = "\n".join(path.read_text() for path in (Path(tmpdir) / "feedback-artifacts").glob("*.json"))

        for raw_secret in (
            "hunter2",
            "value123",
            "correct horse battery staple",
            "open sesame",
            "quoted token value",
        ):
            self.assertNotIn(raw_secret, artifact_text)
        self.assertIn("[REDACTED_TOKEN]", artifact_text)

    def test_feedback_artifacts_redact_host_local_paths_beyond_home_prefixes(self):
        payload = self.fixture_payload()
        payload["review_threads"][0]["comments"]["nodes"][0]["body"] = (
            "logs mention /root/.ssh/id_rsa, /workspace/pr-codex/tasks/learn_feedback.py, "
            "and /var/folders/xy/secret.txt plus /private/var/folders/zz/credential.sock"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            learn_feedback.write_feedback_artifacts(
                payload, output_dir=Path(tmpdir), generated_at="2026-05-08T00:00:00Z"
            )
            artifact_text = "\n".join(path.read_text() for path in (Path(tmpdir) / "feedback-artifacts").glob("*.json"))

        self.assertNotIn("/root/.ssh/id_rsa", artifact_text)
        self.assertNotIn("/workspace/pr-codex", artifact_text)
        self.assertNotIn("/var/folders", artifact_text)
        self.assertNotIn("/private/var/folders", artifact_text)
        self.assertEqual(artifact_text.count("[REDACTED_LOCAL_PATH]"), 5)

    def test_feedback_artifacts_redact_home_relative_credential_paths(self):
        payload = self.fixture_payload()
        payload["review_threads"][0]["comments"]["nodes"][0]["body"] = (
            "logs mention ~/.ssh/id_rsa and ~/.aws/credentials in the fix context"
        )
        payload["comments"][0]["body"] = (
            "pr-codex/false-positive: PRRT_open_1 pasted ~/.config/gh/hosts.yml"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            learn_feedback.write_feedback_artifacts(
                payload, output_dir=Path(tmpdir), generated_at="2026-05-08T00:00:00Z"
            )
            artifact_text = "\n".join(path.read_text() for path in (Path(tmpdir) / "feedback-artifacts").glob("*.json"))

        self.assertNotIn("~/.ssh/id_rsa", artifact_text)
        self.assertNotIn("~/.aws/credentials", artifact_text)
        self.assertNotIn("~/.config/gh/hosts.yml", artifact_text)
        self.assertEqual(artifact_text.count("[REDACTED_LOCAL_PATH]"), 3)

    def test_feedback_artifacts_redact_local_paths_with_spaces(self):
        payload = self.fixture_payload()
        user_config_path = "/Users/example/Library/Application Support/gh/hosts.yml"
        home_secret_path = "/home/example/My Secrets/token.txt"
        payload["review_threads"][0]["comments"]["nodes"][0]["body"] = (
            f"logs mention {user_config_path} before retry"
        )
        payload["comments"][0]["body"] = (
            f"pr-codex/false-positive: PRRT_open_1 pasted {home_secret_path}"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            learn_feedback.write_feedback_artifacts(
                payload, output_dir=Path(tmpdir), generated_at="2026-05-08T00:00:00Z"
            )
            artifact_text = "\n".join(path.read_text() for path in (Path(tmpdir) / "feedback-artifacts").glob("*.json"))

        self.assertNotIn(user_config_path, artifact_text)
        self.assertNotIn(home_secret_path, artifact_text)
        self.assertNotIn("Support/gh/hosts.yml", artifact_text)
        self.assertNotIn("Secrets/token.txt", artifact_text)
        self.assertEqual(artifact_text.count("[REDACTED_LOCAL_PATH]"), 2)

    def test_feedback_artifacts_redact_pasted_private_key_blocks(self):
        payload = self.fixture_payload()
        payload["review_threads"][0]["comments"]["nodes"][0]["body"] = (
            "Please do not learn this pasted credential:\n"
            "-----BEGIN OPENSSH PRIVATE KEY-----\n"
            "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQ==\n"
            "-----END OPENSSH PRIVATE KEY-----\n"
            "The fix is unrelated."
        )
        payload["comments"][0]["body"] = (
            "pr-codex/false-positive: PRRT_open_1 includes another credential block.\n"
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEpAIBAAKCAQEA0notARealCredentialExampleOnly\n"
            "-----END RSA PRIVATE KEY-----"
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            learn_feedback.write_feedback_artifacts(
                payload, output_dir=Path(tmpdir), generated_at="2026-05-08T00:00:00Z"
            )
            artifact_text = "\n".join(path.read_text() for path in (Path(tmpdir) / "feedback-artifacts").glob("*.json"))

        self.assertNotIn("BEGIN OPENSSH PRIVATE KEY", artifact_text)
        self.assertNotIn("END OPENSSH PRIVATE KEY", artifact_text)
        self.assertNotIn("b3BlbnNzaC1rZXktdjE", artifact_text)
        self.assertNotIn("BEGIN RSA PRIVATE KEY", artifact_text)
        self.assertNotIn("END RSA PRIVATE KEY", artifact_text)
        self.assertNotIn("MIIEpAIBAAKCAQEA", artifact_text)
        self.assertIn("[REDACTED_CREDENTIAL_BLOCK]", artifact_text)

    def test_feedback_artifacts_redact_pgp_private_key_blocks(self):
        payload = self.fixture_payload()
        payload["review_threads"][0]["comments"]["nodes"][0]["body"] = (
            "Please do not learn this pasted PGP private key block:\n"
            "-----BEGIN PGP PRIVATE KEY BLOCK-----\n"
            "Version: OpenPGP.js v5.0.0\n\n"
            "lQPGBGNotARealCredentialExampleOnly\n"
            "-----END PGP PRIVATE KEY BLOCK-----\n"
            "The fix is unrelated."
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            learn_feedback.write_feedback_artifacts(
                payload, output_dir=Path(tmpdir), generated_at="2026-05-08T00:00:00Z"
            )
            artifact_text = "\n".join(path.read_text() for path in (Path(tmpdir) / "feedback-artifacts").glob("*.json"))

        self.assertNotIn("BEGIN PGP PRIVATE KEY BLOCK", artifact_text)
        self.assertNotIn("END PGP PRIVATE KEY BLOCK", artifact_text)
        self.assertNotIn("lQPGBGNotARealCredentialExampleOnly", artifact_text)
        self.assertIn("[REDACTED_CREDENTIAL_BLOCK]", artifact_text)

    def test_learn_skill_wires_user_invocation_arguments_to_helper(self):
        skill = (ROOT / "skills" / "learn" / "SKILL.md").read_text(encoding="utf-8")

        for snippet in (
            "$ARGUMENTS",
            "SNAPSHOT_JSON=",
            "OUTPUT_DIR=",
            "--input \"$SNAPSHOT_JSON\"",
            "--output-dir \"$OUTPUT_DIR\"",
        ):
            self.assertIn(snippet, skill)

    def test_learn_skill_preserves_quoted_invocation_arguments(self):
        skill = (ROOT / "skills" / "learn" / "SKILL.md").read_text(encoding="utf-8")

        self.assertIn("Claude が解釈済みの 1 番目の引数", skill)
        self.assertIn("空白を含む quoted path を保持", skill)
        self.assertIn("/pr-codex:learn \"feedback snapshot.json\" \"learn out\"", skill)
        self.assertIn("--input \"feedback snapshot.json\" --output-dir \"learn out\"", skill)
        self.assertNotIn("set -- $ARGUMENTS", skill)

    def test_learn_skill_resolves_helper_from_plugin_root(self):
        skill = (ROOT / "skills" / "learn" / "SKILL.md").read_text(encoding="utf-8")

        for snippet in (
            "CLAUDE_PLUGIN_ROOT=",
            'if [ -z "${CLAUDE_PLUGIN_ROOT:-}" ]',
            "skills/learn/SKILL.md",
            'CLAUDE_PLUGIN_ROOT="$(cd "$CLAUDE_PLUGIN_ROOT" && pwd)"',
            "HELPER=\"$CLAUDE_PLUGIN_ROOT/tasks/learn_feedback.py\"",
            "python3 \"$HELPER\"",
        ):
            self.assertIn(snippet, skill)
        self.assertNotIn("python3 tasks/learn_feedback.py", skill)

    def test_write_feedback_artifacts_removes_stale_signal_files_on_rerun(self):
        payload = self.fixture_payload()
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            learn_feedback.write_feedback_artifacts(
                payload, output_dir=output_dir, generated_at="2026-05-08T00:00:00Z"
            )

            payload_without_false_positive = {**payload, "comments": [], "labels": []}
            result = learn_feedback.write_feedback_artifacts(
                payload_without_false_positive,
                output_dir=output_dir,
                generated_at="2026-05-08T00:01:00Z",
            )
            artifact_paths = sorted((output_dir / "feedback-artifacts").glob("*.json"))

        self.assertEqual(result["summary"], {"addressed": 1, "superseded": 1, "false_positive": 0, "ignored": 2})
        self.assertEqual(result["artifact_count"], 2)
        self.assertEqual(result["artifacts"], [
            "feedback-artifacts/addressed-PRRT_resolved_1.json",
            "feedback-artifacts/superseded-PRRT_outdated_1.json",
        ])
        self.assertEqual([path.name for path in artifact_paths], [
            "addressed-PRRT_resolved_1.json",
            "superseded-PRRT_outdated_1.json",
        ])

    def test_main_expands_user_home_in_input_and_output_dir_arguments(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            home.mkdir()
            payload_path = home / "feedback.json"
            output_dir = home / "claude-loop-pr-codex" / "learn"
            payload_path.write_text(json.dumps(self.fixture_payload()), encoding="utf-8")
            argv = [
                "learn_feedback.py",
                "--input",
                "~/feedback.json",
                "--output-dir",
                "~/claude-loop-pr-codex/learn",
                "--generated-at",
                "2026-05-08T00:00:00Z",
            ]

            with patch.dict(os.environ, {"HOME": str(home)}), patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()):
                exit_code = learn_feedback.main()

            self.assertEqual(exit_code, 0)
            self.assertTrue((output_dir / "learn-result.json").exists())
            self.assertFalse((Path.cwd() / "~").exists())


if __name__ == "__main__":
    unittest.main()
