#!/usr/bin/env python3
"""Regression tests for provider-reported actual cost extraction."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tasks" / "extract_actual_cost.py"


class ExtractActualCostTest(unittest.TestCase):
    def run_script(self, *args: str) -> dict[str, object]:
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_sums_provider_reported_usd_from_cli_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            claude_log = tmp / "claude.log"
            codex_log = tmp / "codex.log"
            claude_log.write_text("review complete\nTotal cost: $0.0500\n", encoding="utf-8")
            codex_log.write_text("tokens used\ncost_usd: 0.0734\n", encoding="utf-8")

            actual = self.run_script(
                "--component",
                f"claude={claude_log}",
                "--component",
                f"codex={codex_log}",
            )

        self.assertEqual(
            actual,
            {
                "actual_usd": 0.1234,
                "currency": "USD",
                "source": "provider_reported",
                "components": [
                    {"tool": "claude", "actual_usd": 0.05, "source": "cli_log"},
                    {"tool": "codex", "actual_usd": 0.0734, "source": "cli_log"},
                ],
            },
        )

    def test_returns_unavailable_without_guessing_from_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            log = tmp / "codex.log"
            log.write_text("input_tokens=100000 output_tokens=20000\n", encoding="utf-8")

            actual = self.run_script("--component", f"codex={log}")

        self.assertEqual(
            actual,
            {
                "actual_usd": None,
                "currency": "USD",
                "source": "unavailable",
                "components": [],
            },
        )


if __name__ == "__main__":
    unittest.main()
