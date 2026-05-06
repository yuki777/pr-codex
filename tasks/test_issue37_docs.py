#!/usr/bin/env python3
"""Executable documentation checks for Issue #37 SARIF tri-layer output."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
SEND_SKILL = ROOT / "skills" / "send" / "SKILL.md"
REVIEW_SKILL = ROOT / "skills" / "review" / "SKILL.md"
REVIEW_CRITERIA = ROOT / "skills" / "review" / "REVIEW_CRITERIA.md"
WORKFLOW = ROOT / ".github" / "workflows" / "validate-run-plan.yml"


class Issue37DocsTest(unittest.TestCase):
    def test_readme_documents_sarif_local_artifact_boundary(self) -> None:
        text = README.read_text(encoding="utf-8")
        for snippet in (
            "findings.sarif",
            "local-only artifact",
            "GitHub Code Scanning への upload",
            "posting.post_policy=suppress",
            "jsonschema",
            "canonical_must_fix != markdown_must_fix != payload_must_fix != sarif_must_fix",
            "result.partialFingerprints.canonical",
            "deterministic UUIDv5",
            "Windows drive",
        ):
            self.assertIn(snippet, text)

    def test_review_skill_generates_and_validates_sarif_from_canonical_tmp(self) -> None:
        text = REVIEW_SKILL.read_text(encoding="utf-8")
        for snippet in (
            "generate_findings_sarif.py --findings",
            "validate_findings_sarif.py --schema",
            "findings.sarif.tmp",
            "schemas/sarif-2.1.0.json",
            "local-only SARIF",
            "Must Fix 件数",
            "コメント可能範囲なし",
        ):
            self.assertIn(snippet, text)

    def test_send_skill_extends_preflight_to_sarif_count_gate(self) -> None:
        text = SEND_SKILL.read_text(encoding="utf-8")
        for snippet in (
            "{SARIF_SCHEMA_PATH}",
            "findings.sarif",
            "sarif_must_fix",
            "must_fix_count_mismatch",
            "sarif_schema_invalid",
            "validate_findings_sarif.py",
            "Code Scanning upload なし",
            "コメント可能範囲なし",
        ):
            self.assertIn(snippet, text)

    def test_review_criteria_documents_post_policy_to_sarif_suppressions(self) -> None:
        text = REVIEW_CRITERIA.read_text(encoding="utf-8")
        for snippet in (
            "SARIF 派生成果物の公開境界",
            "local_only per pr-codex post_policy",
            "`suppress` | SARIF に出力しない",
            "nit` は noise 防止のため `suppressions`",
            "must_fix → error",
        ):
            self.assertIn(snippet, text)

    def test_ci_runs_sarif_tests_without_upload_step(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("tasks.test_findings_sarif", text)
        self.assertNotIn("upload-sarif", text)
        self.assertNotIn("github/codeql-action/upload-sarif", text)


if __name__ == "__main__":
    unittest.main()
