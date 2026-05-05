#!/usr/bin/env python3
"""Regression tests for the stdlib-only findings validator."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "tasks"
SCHEMA_PATH = ROOT / "schemas" / "findings.v1.json"
VALIDATOR_PATH = TASKS / "validate_findings.py"
sys.path.insert(0, str(TASKS))

from validate_findings import compute_fingerprint, validate_artifact  # noqa: E402


def valid_artifact() -> dict[str, object]:
    finding: dict[str, object] = {
        "id": "",
        "fingerprint": "",
        "source_agents": ["claude", "codex"],
        "merged_from": ["claude-review.md:Must Fix 1", "codex-review.md:Must Fix 1"],
        "location": {
            "path": "skills/send/SKILL.md",
            "start_line": 323,
            "end_line": 336,
            "side": "RIGHT",
        },
        "severity": "must_fix",
        "category": "bug",
        "title": "`schema_path` placeholder must not be shell-expanded.",
        "problem": "Prompt construction can be corrupted before Codex receives it.",
        "reason": "Shell expansion changes the validation instructions.",
        "suggestion": "Use a shell-safe placeholder and validate before execution.",
        "evidence_level": "verified",
        "axes": {
            "real": "yes",
            "triggerable": "yes",
            "impactful": "yes",
            "general": "yes",
        },
        "posting": {
            "post_policy": "inline",
            "explanation_postable": True,
        },
    }
    fingerprint = compute_fingerprint(finding)
    finding["id"] = fingerprint
    finding["fingerprint"] = fingerprint
    return {
        "schema_version": "findings.v1",
        "producer": {
            "name": "pr-codex",
            "version": "1.4.4",
            "run_id": "yuki777-pr-codex-25-e8763f5",
        },
        "pr": {
            "repository": "yuki777/pr-codex",
            "number": 25,
            "base_sha": "2499605587c910c1911729e90d4c96b61210c628",
            "head_sha": "e8763f5edddeca5be7334ac9131066be09f19a6d",
            "merge_commit_sha": None,
        },
        "generated_at": "2026-05-05T08:02:23Z",
        "findings": [finding],
    }


class ValidateFindingsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    def assert_invalid_without_crash(self, artifact: dict[str, object], expected_fragment: str) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_path = Path(tmp) / "findings.verified.json"
            data_path.write_text(json.dumps(artifact, ensure_ascii=False), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(VALIDATOR_PATH), "--schema", str(SCHEMA_PATH), "--data", str(data_path)],
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("INVALID findings artifact", result.stderr)
        self.assertIn(expected_fragment, result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_minimal_valid_artifact_passes(self) -> None:
        self.assertEqual(validate_artifact(self.schema, valid_artifact()), [])

    def test_compute_fingerprint_defends_against_invalid_input_types(self) -> None:
        mutations = {
            "location-int": lambda f: f.update(location=42),
            "location-str": lambda f: f.update(location="skills/send/SKILL.md"),
            "location-list": lambda f: f.update(location=["skills/send/SKILL.md"]),
            "location-null": lambda f: f.update(location=None),
            "path-non-string": lambda f: f["location"].update(path=123),
            "category-non-string": lambda f: f.update(category=123),
            "title-non-string": lambda f: f.update(title=123),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                artifact = copy.deepcopy(valid_artifact())
                mutate(artifact["findings"][0])
                self.assert_invalid_without_crash(artifact, "fingerprint: expected")

    def test_optional_null_fields_are_schema_invalid(self) -> None:
        for field in ("severity_disputed", "severity_by_source", "evidence", "php"):
            with self.subTest(field=field):
                artifact = copy.deepcopy(valid_artifact())
                artifact["findings"][0][field] = None
                self.assert_invalid_without_crash(artifact, f".{field}:")

    def test_severity_disputed_true_requires_companion_fields(self) -> None:
        artifact = copy.deepcopy(valid_artifact())
        artifact["findings"][0]["severity_disputed"] = True
        self.assert_invalid_without_crash(artifact, "severity_by_source: required when severity_disputed=true")

    def test_evidence_level_suspicion_cannot_be_postable(self) -> None:
        artifact = copy.deepcopy(valid_artifact())
        finding = artifact["findings"][0]
        finding["evidence_level"] = "suspicion"
        finding["posting"]["explanation_postable"] = True
        self.assert_invalid_without_crash(artifact, "must be false when evidence_level=suspicion")

    def test_id_must_equal_recomputed_fingerprint(self) -> None:
        artifact = copy.deepcopy(valid_artifact())
        artifact["findings"][0]["id"] = "0" * 64
        self.assert_invalid_without_crash(artifact, "id must equal fingerprint")


if __name__ == "__main__":
    unittest.main()
