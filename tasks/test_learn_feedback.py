from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "tasks"


def load_module(name: str):
    spec = importlib.util.spec_from_file_location(name, TASKS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


learn_feedback = load_module("learn_feedback")


class LearnFeedbackTests(unittest.TestCase):
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
                    "comments": {"nodes": [{"id": "PRRC_outdated_1", "body": "High: 古い差分上の指摘です。"}]},
                },
                {
                    "id": "PRRT_open_1",
                    "isResolved": False,
                    "isOutdated": False,
                    "path": "tasks/open.py",
                    "line": 99,
                    "comments": {"nodes": [{"id": "PRRC_open_1", "body": "誤検知として明示された指摘"}]},
                },
                {
                    "id": "PRRT_silent_1",
                    "isResolved": False,
                    "isOutdated": False,
                    "path": "tasks/silent.py",
                    "line": 100,
                    "comments": {"nodes": [{"id": "PRRC_silent_1", "body": "author 無反応のため学習しない"}]},
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

    def test_feedback_artifacts_redact_common_raw_bearer_tokens_without_key_names(self):
        payload = self.fixture_payload()
        openai_token = "sk" + "-proj-" + "abc123DEF456ghi789JKL012mno345PQR678stu901"
        gitlab_token = "gl" + "pat-" + "abcDEF1234567890abcd"
        payload["review_threads"][0]["comments"]["nodes"][0]["body"] = (
            f"resolved with OpenAI key {openai_token} and GitLab token {gitlab_token}"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            learn_feedback.write_feedback_artifacts(
                payload, output_dir=Path(tmpdir), generated_at="2026-05-08T00:00:00Z"
            )
            artifact_text = "\n".join(path.read_text() for path in (Path(tmpdir) / "feedback-artifacts").glob("*.json"))

        self.assertNotIn("sk-proj-", artifact_text)
        self.assertNotIn("glpat-", artifact_text)
        self.assertIn("[REDACTED_TOKEN]", artifact_text)

    def test_write_feedback_artifacts_removes_stale_signal_files_on_rerun(self):
        payload = self.fixture_payload()
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            learn_feedback.write_feedback_artifacts(
                payload, output_dir=output_dir, generated_at="2026-05-08T00:00:00Z"
            )

            payload_without_false_positive = {**payload, "comments": []}
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


if __name__ == "__main__":
    unittest.main()
