from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "tasks"
SCHEMA = ROOT / "schemas" / "episode.v1.json"


def load_module(name: str):
    spec = importlib.util.spec_from_file_location(name, TASKS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


episode_memory = load_module("episode_memory")


class EpisodeMemoryTests(unittest.TestCase):
    def feedback_artifact(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "repository": "yuki777/pr-codex",
            "pr_number": 58,
            "head_sha": "abc123def456",
            "thread_id": "PRRT_false_positive_1",
            "signal": "false_positive",
            "source": "label_comment.false_positive",
            "path": "tasks/validate_run_plan.py",
            "line": 42,
            "comment_ids": ["PRRC_1"],
            "comment_excerpts": [
                "Must Fix: token ghp_abcdefghijklmnopqrstuvwxyz1234567890 と /Users/adachi/private.txt を含むログを保存している"
            ],
            "urls": ["https://github.test/review/1"],
        }

    def test_schema_file_is_json_and_rejects_raw_comment_payload(self) -> None:
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        episode = episode_memory.build_episode(
            self.feedback_artifact(),
            pr_types=["python-validator-runtime"],
            finding_class="secret-handling",
            generated_at="2026-05-21T00:00:00Z",
        )
        episode_memory.validate_episode(episode, schema=schema)

        raw = dict(episode)
        raw["comment_excerpts"] = ["raw comments must not be stored"]
        with self.assertRaisesRegex(ValueError, "unexpected properties"):
            episode_memory.validate_episode(raw, schema=schema)

    def test_build_episode_scrubs_secrets_local_paths_and_keeps_public_summary_only(self) -> None:
        episode = episode_memory.build_episode(
            self.feedback_artifact(),
            pr_types=["python-validator-runtime"],
            finding_class="secret-handling",
            generated_at="2026-05-21T00:00:00Z",
        )

        encoded = json.dumps(episode, ensure_ascii=False)
        self.assertIn("[REDACTED_TOKEN]", encoded)
        self.assertIn("[REDACTED_LOCAL_PATH]", encoded)
        self.assertNotIn("ghp_ab...7890", encoded)
        self.assertNotIn("/Users/adachi/private.txt", encoded)
        self.assertTrue(episode["public_safe"])
        self.assertEqual(episode["content"], {"summary": episode["content"]["summary"]})

    def test_retrieval_requires_matching_pr_type_path_and_finding_class(self) -> None:
        matching = episode_memory.build_episode(
            self.feedback_artifact(),
            pr_types=["python-validator-runtime"],
            finding_class="secret-handling",
            generated_at="2026-05-21T00:00:00Z",
        )
        unrelated = dict(matching)
        unrelated["episode_id"] = "episode-00000000000000000000000000000000"
        unrelated["paths"] = ["README.md"]

        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "episodes.jsonl"
            episode_memory.append_episode(store, matching)
            episode_memory.append_episode(store, unrelated)

            results = episode_memory.retrieve_episodes(
                store,
                pr_types=["python-validator-runtime"],
                paths=["tasks/validate_run_plan.py"],
                finding_class="secret-handling",
                now="2026-05-21T00:00:00Z",
            )

            self.assertEqual([item["episode_id"] for item in results], [matching["episode_id"]])

            sibling_results = episode_memory.retrieve_episodes(
                store,
                pr_types=["python-validator-runtime"],
                paths=["tasks/other_validator.py"],
                finding_class="secret-handling",
                now="2026-05-21T00:00:00Z",
            )

        self.assertEqual(sibling_results, [])

    def test_retrieval_marks_stale_episode_as_context_only_not_adopt(self) -> None:
        episode = episode_memory.build_episode(
            self.feedback_artifact(),
            pr_types=["python-validator-runtime"],
            finding_class="secret-handling",
            generated_at="2026-01-01T00:00:00Z",
            stale_after_days=30,
        )

        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "episodes.jsonl"
            episode_memory.append_episode(store, episode)
            results = episode_memory.retrieve_episodes(
                store,
                pr_types=["python-validator-runtime"],
                paths=["tasks/validate_run_plan.py"],
                finding_class="secret-handling",
                now="2026-05-21T00:00:00Z",
            )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["freshness"], "stale")
        self.assertEqual(results[0]["use_policy"], "context_only_reverify")
        self.assertNotEqual(results[0]["use_policy"], "auto_adopt")

    def test_retrieval_marks_fresh_episode_as_reverify_not_auto_adopt(self) -> None:
        episode = episode_memory.build_episode(
            self.feedback_artifact(),
            pr_types=["python-validator-runtime"],
            finding_class="secret-handling",
            generated_at="2026-05-20T00:00:00Z",
        )

        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "episodes.jsonl"
            episode_memory.append_episode(store, episode)
            results = episode_memory.retrieve_episodes(
                store,
                pr_types=["python-validator-runtime"],
                paths=["tasks/validate_run_plan.py"],
                finding_class="secret-handling",
                now="2026-05-21T00:00:00Z",
            )

        self.assertEqual(results[0]["freshness"], "fresh")
        self.assertEqual(results[0]["use_policy"], "reverify_current_diff")

    def test_load_episodes_rejects_contaminated_store_text_and_paths(self) -> None:
        episode = episode_memory.build_episode(
            self.feedback_artifact(),
            pr_types=["python-validator-runtime"],
            finding_class="secret-handling",
            generated_at="2026-05-21T00:00:00Z",
        )

        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "episodes.jsonl"
            contaminated = dict(episode)
            contaminated["content"] = {"summary": "leaked token ghp_abcdefghijklmnopqrstuvwxyz"}
            store.write_text(json.dumps(contaminated) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "non-public-safe"):
                episode_memory.load_episodes(store)

            traversal = dict(episode)
            traversal["paths"] = ["../secrets.txt"]
            store.write_text(json.dumps(traversal) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "paths"):
                episode_memory.load_episodes(store)

    def test_cli_writes_jsonl_store_and_retrieves_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            feedback_path = root / "feedback.json"
            store = root / "episodes.jsonl"
            feedback_path.write_text(json.dumps(self.feedback_artifact()), encoding="utf-8")

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = episode_memory.main(
                    [
                        "write",
                        "--feedback-artifact",
                        str(feedback_path),
                        "--store",
                        str(store),
                        "--pr-type",
                        "python-validator-runtime",
                        "--finding-class",
                        "secret-handling",
                        "--generated-at",
                        "2026-05-21T00:00:00Z",
                    ]
                )
            self.assertEqual(exit_code, 0)
            self.assertTrue(store.exists())

            captured = episode_memory.retrieve_for_cli(
                store,
                pr_types=["python-validator-runtime"],
                paths=["tasks/validate_run_plan.py"],
                finding_class="secret-handling",
                now="2026-05-21T00:00:00Z",
            )
            self.assertEqual(captured["episode_count"], 1)
            self.assertEqual(captured["episodes"][0]["finding_class"], "secret-handling")


if __name__ == "__main__":
    unittest.main()
