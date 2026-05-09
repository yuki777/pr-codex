#!/usr/bin/env python3
"""Regression tests for read-only CI status artifact generation."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "tasks"
sys.path.insert(0, str(TASKS))

from ci_status import (  # noqa: E402
    build_ci_status,
    build_markdown_summary,
    pull_context_from_rest_payload,
    scrub_public_text,
)


def rollup_item(name: str, status: str, conclusion: str | None = None) -> dict[str, object]:
    return {"name": name, "status": status, "conclusion": conclusion}


class CiStatusTest(unittest.TestCase):
    def test_success_failure_pending_and_skipped_are_classified(self) -> None:
        status = build_ci_status(
            pr={
                "repository": "yuki777/pr-codex",
                "number": 55,
                "head_sha": "a" * 40,
                "base_sha": "b" * 40,
            },
            status_check_rollup=[
                rollup_item("unit", "COMPLETED", "SUCCESS"),
                rollup_item("lint", "COMPLETED", "FAILURE"),
                rollup_item("deploy", "IN_PROGRESS", None),
                rollup_item("docs", "COMPLETED", "SKIPPED"),
            ],
            workflow_runs=[],
            failed_job_logs={},
        )

        self.assertEqual(status["schema_version"], "ci-status.v1")
        self.assertEqual(status["state"], "failure")
        self.assertEqual(status["counts"], {"success": 1, "failure": 1, "pending": 1, "skipped": 1})
        self.assertEqual(status["checks"][1]["state"], "failure")
        self.assertTrue(status["read_only"])

    def test_pending_wins_when_no_failures_exist(self) -> None:
        status = build_ci_status(
            pr={"repository": "octo/example", "number": 1, "head_sha": "c" * 40},
            status_check_rollup=[rollup_item("unit", "QUEUED", None), rollup_item("docs", "COMPLETED", "SUCCESS")],
            workflow_runs=[],
            failed_job_logs={},
        )
        self.assertEqual(status["state"], "pending")

    def test_failed_logs_are_summarized_with_secret_like_text_scrubbed(self) -> None:
        raw_log = """
        build failed
        token ghp_abcdefghijklmnopqrstuvwxyz123456
        Authorization: Bearer secret-value-1234567890
        /home/adachi/projects/customer/private/file.py:12 boom
        AWS_SECRET_ACCESS_KEY=abc1234567890secret
        """
        status = build_ci_status(
            pr={"repository": "octo/example", "number": 2, "head_sha": "d" * 40},
            status_check_rollup=[rollup_item("unit", "COMPLETED", "FAILURE")],
            workflow_runs=[{"name": "CI", "status": "completed", "conclusion": "failure", "databaseId": 123}],
            failed_job_logs={"unit": raw_log},
        )
        summary_text = json.dumps(status["failed_job_summaries"], ensure_ascii=False)
        self.assertIn("[REDACTED_TOKEN]", summary_text)
        self.assertIn("[REDACTED_LOCAL_PATH]", summary_text)
        self.assertNotIn("ghp_", summary_text)
        self.assertNotIn("secret-value", summary_text)
        self.assertNotIn("/home/adachi", summary_text)

    def test_old_gh_fallback_uses_rest_pull_payload_for_pr_context(self) -> None:
        pr = pull_context_from_rest_payload(
            {
                "base": {"repo": {"full_name": "yuki777/pr-codex"}, "sha": "b" * 40, "ref": "main"},
                "head": {"sha": "a" * 40, "ref": "feat/55"},
                "number": 55,
                "html_url": "https://github.com/yuki777/pr-codex/pull/55",
            }
        )
        self.assertEqual(pr["repository"], "yuki777/pr-codex")
        self.assertEqual(pr["head_sha"], "a" * 40)
        self.assertEqual(pr["base_branch"], "main")

    def test_old_gh_fallback_accepts_check_runs_endpoint_shape(self) -> None:
        status = build_ci_status(
            pr={"repository": "octo/example", "number": 1, "head_sha": "e" * 40},
            status_check_rollup={"check_runs": [{"name": "unit", "status": "completed", "conclusion": "success"}]},
            workflow_runs=[],
            failed_job_logs={},
        )
        self.assertEqual(status["state"], "success")
        self.assertEqual(status["checks"][0]["name"], "unit")

    def test_old_gh_fallback_preserves_combined_status_contexts(self) -> None:
        status = build_ci_status(
            pr={"repository": "octo/example", "number": 1, "head_sha": "f" * 40},
            status_check_rollup={
                "state": "failure",
                "statuses": [
                    {"context": "unit", "state": "success", "target_url": "https://example.invalid/unit"},
                    {"context": "lint", "state": "failure", "target_url": "https://example.invalid/lint"},
                    {"context": "deploy", "state": "pending"},
                ],
            },
            workflow_runs=[],
            failed_job_logs={},
        )

        self.assertEqual(status["state"], "failure")
        self.assertEqual(status["counts"], {"success": 1, "failure": 1, "pending": 1, "skipped": 0})
        self.assertEqual([check["name"] for check in status["checks"]], ["unit", "lint", "deploy"])
        self.assertEqual(status["checks"][1]["url"], "https://example.invalid/lint")

    def test_cli_writes_ci_status_json_and_summary_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            pr_path = tmp_path / "pull.json"
            rollup_path = tmp_path / "rollup.json"
            runs_path = tmp_path / "runs.json"
            log_path = tmp_path / "unit.log"
            out_json = tmp_path / "ci-status.json"
            out_md = tmp_path / "ci-summary.md"

            pr_path.write_text(
                json.dumps(
                    {
                        "base": {"repo": {"full_name": "octo/example"}, "sha": "b" * 40, "ref": "main"},
                        "head": {"sha": "a" * 40, "ref": "feature"},
                        "number": 9,
                        "html_url": "https://github.com/octo/example/pull/9",
                    }
                ),
                encoding="utf-8",
            )
            rollup_path.write_text(json.dumps([rollup_item("unit", "COMPLETED", "SUCCESS")]), encoding="utf-8")
            runs_path.write_text(json.dumps({"workflow_runs": []}), encoding="utf-8")
            log_path.write_text("unit ok", encoding="utf-8")

            completed = subprocess.run(
                [
                    sys.executable,
                    str(TASKS / "ci_status.py"),
                    "--pull-json",
                    str(pr_path),
                    "--status-check-rollup-json",
                    str(rollup_path),
                    "--workflow-runs-json",
                    str(runs_path),
                    "--failed-log",
                    f"unit={log_path}",
                    "--out-json",
                    str(out_json),
                    "--out-md",
                    str(out_md),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(out_json.read_text(encoding="utf-8"))["state"], "success")
            markdown = out_md.read_text(encoding="utf-8")
            self.assertIn("# CI summary", markdown)
            self.assertIn("overall: success", markdown)
            self.assertIn("unit", markdown)

    def test_markdown_summary_is_public_safe(self) -> None:
        status = build_ci_status(
            pr={"repository": "octo/example", "number": 2, "head_sha": "d" * 40},
            status_check_rollup=[rollup_item("unit", "COMPLETED", "FAILURE")],
            workflow_runs=[],
            failed_job_logs={"unit": "password=super-secret-value\nfailed"},
        )
        markdown = build_markdown_summary(status)
        self.assertIn("[REDACTED_SECRET]", markdown)
        self.assertNotIn("super-secret", markdown)


if __name__ == "__main__":
    unittest.main()
