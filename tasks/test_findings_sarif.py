#!/usr/bin/env python3
"""Regression tests for Issue #37 SARIF derived output."""

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
SCHEMA_PATH = ROOT / "schemas" / "sarif-2.1.0.json"
GENERATOR_PATH = TASKS / "generate_findings_sarif.py"
VALIDATOR_PATH = TASKS / "validate_findings_sarif.py"
sys.path.insert(0, str(TASKS))

from generate_findings_sarif import SarifGenerationError, build_sarif, must_fix_count  # noqa: E402
from validate_findings import compute_fingerprint  # noqa: E402
from validate_findings_sarif import validate_findings_sarif  # noqa: E402


def make_finding(
    *,
    severity: str,
    category: str,
    title: str,
    path: str,
    line: int,
    post_policy: str,
    evidence_level: str = "corroborated",
    explanation_postable: bool = True,
) -> dict[str, object]:
    finding: dict[str, object] = {
        "id": "",
        "fingerprint": "",
        "source_agents": ["claude", "codex"],
        "merged_from": [f"claude-review.md:{title}", f"codex-review.md:{title}"],
        "location": {"path": path, "start_line": line, "side": "RIGHT"},
        "severity": severity,
        "category": category,
        "title": title,
        "problem": f"{title} exposes /Users/adachi/private.txt in diagnostic context.",
        "reason": "The generated artifact must preserve review context without leaking host absolute paths.",
        "suggestion": "Keep repository-relative paths and redact host-local details.",
        "evidence_level": evidence_level,
        "axes": {"real": "yes", "triggerable": "yes", "impactful": "yes", "general": "yes"},
        "posting": {"post_policy": post_policy, "explanation_postable": explanation_postable},
    }
    if post_policy == "local_only":
        finding["posting"]["audience"] = "human_reviewer"
    if not explanation_postable:
        finding["posting"]["not_postable_reason"] = "other_explained"
    if severity == "must_fix":
        finding["evidence_level"] = "verified"
        finding["posting"] = {"post_policy": "inline", "explanation_postable": True}
    fingerprint = compute_fingerprint(finding)
    finding["id"] = fingerprint
    finding["fingerprint"] = fingerprint
    return finding


def canonical_artifact(findings: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "schema_version": "findings.v1",
        "producer": {"name": "pr-codex", "version": "1.6.0", "run_id": "yuki777-pr-codex-37-deadbee"},
        "pr": {
            "repository": "yuki777/pr-codex",
            "number": 37,
            "base_sha": "a" * 40,
            "head_sha": "b" * 40,
            "merge_commit_sha": None,
        },
        "generated_at": "2026-05-06T00:00:00Z",
        "findings": findings if findings is not None else default_findings(),
    }


def default_findings() -> list[dict[str, object]]:
    return [
        make_finding(
            severity="must_fix",
            category="security",
            title="`token` must not be logged",
            path="src/App.php",
            line=10,
            post_policy="inline",
        ),
        make_finding(
            severity="should_fix",
            category="bug",
            title="`retry` should preserve failures",
            path="src/App.php",
            line=12,
            post_policy="body_summary",
        ),
        make_finding(
            severity="nit",
            category="code_quality",
            title="`name` can be clearer",
            path="src/App.php",
            line=14,
            post_policy="body_summary",
        ),
    ]


def metadata() -> dict[str, object]:
    return {
        "org": "yuki777",
        "repository": "pr-codex",
        "repository_full_name": "yuki777/pr-codex",
        "pr_number": 37,
        "pr_url": "https://github.com/yuki777/pr-codex/pull/37",
        "head_sha": "b" * 40,
        "base_sha": "a" * 40,
        "branch": "feat/37",
        "base_branch": "main",
        "title": "SARIF output",
        "files": ["src/App.php"],
    }


def range_text(path: str = "src/App.php", start: int = 1, end: int = 20) -> str:
    return f"{path}\tL{start}-L{end}\n"


def refresh_fingerprint(finding: dict[str, object]) -> dict[str, object]:
    finding["id"] = ""
    finding["fingerprint"] = ""
    fingerprint = compute_fingerprint(finding)
    finding["id"] = fingerprint
    finding["fingerprint"] = fingerprint
    return finding


class FindingsSarifTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    def assert_cli_invalid(self, completed: subprocess.CompletedProcess[str], expected: str) -> None:
        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        self.assertIn(expected, completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)

    def test_normal_three_severity_artifact_validates_and_maps_policy(self) -> None:
        artifact = canonical_artifact()
        sarif = build_sarif(artifact, metadata=metadata(), ranges={"src/App.php": [(1, 20)]})
        self.assertEqual(validate_findings_sarif(self.schema, sarif, findings=artifact), [])
        results = sarif["runs"][0]["results"]
        self.assertEqual([result["level"] for result in results], ["error", "warning", "note"])
        self.assertEqual(results[0]["properties"]["security_severity_label"], "high")
        self.assertNotIn("suppressions", results[1])
        self.assertEqual(results[2]["suppressions"][0]["kind"], "external")
        self.assertEqual(sarif["runs"][0]["tool"]["driver"]["rules"][0]["id"], "pr-codex/bug")
        self.assertEqual(len(sarif["runs"][0]["tool"]["driver"]["rules"]), 8)
        self.assertNotIn("/Users/adachi", results[0]["message"]["text"])
        self.assertNotIn("fixes", results[0])
        self.assertEqual(must_fix_count(sarif), 1)

    def test_windows_absolute_paths_are_scrubbed_from_messages_and_rejected_as_locations(self) -> None:
        finding = make_finding(
            severity="should_fix",
            category="bug",
            title=r"`path` exposes C:\Users\alice\repo\secret.txt",
            path="src/App.php",
            line=10,
            post_policy="body_summary",
        )
        finding["problem"] = r"Drive path C:\Users\alice\repo\secret.txt leaked into the review."
        finding["reason"] = r"UNC path \\buildbox\share\repo\secret.txt is host-local context."
        finding["suggestion"] = "Do not emit file:///C:/Users/alice/repo/secret.txt in derived artifacts."
        refresh_fingerprint(finding)
        sarif = build_sarif(canonical_artifact([finding]), metadata=metadata(), ranges={"src/App.php": [(1, 20)]})
        message = sarif["runs"][0]["results"][0]["message"]["text"]
        self.assertIn("<absolute-path>", message)
        self.assertNotIn(r"C:\Users\alice", message)
        self.assertNotIn(r"\\buildbox\share", message)
        self.assertNotIn("file:///C:/Users/alice", message)
        self.assertNotIn(":/Users/alice", message)
        self.assertNotIn("/Users/alice", message)

        invalid_location = copy.deepcopy(finding)
        invalid_location["location"] = {"path": r"C:\Users\alice\repo\src\App.php", "start_line": 10, "side": "RIGHT"}
        with self.assertRaisesRegex(SarifGenerationError, "repository-relative paths"):
            build_sarif(canonical_artifact([invalid_location]), metadata=metadata(), ranges=None)

    def test_empty_findings_still_emit_valid_empty_results(self) -> None:
        artifact = canonical_artifact([])
        sarif = build_sarif(artifact, metadata=metadata(), ranges={})
        self.assertEqual(sarif["runs"][0]["results"], [])
        self.assertEqual(validate_findings_sarif(self.schema, sarif, findings=artifact), [])

    def test_generator_fails_when_location_is_outside_ranges(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            findings_path = tmp_path / "findings.verified.json"
            metadata_path = tmp_path / "metadata.json"
            ranges_path = tmp_path / "pr.diff.ranges.txt"
            output_path = tmp_path / "findings.sarif"
            findings_path.write_text(json.dumps(canonical_artifact(), ensure_ascii=True), encoding="utf-8")
            metadata_path.write_text(json.dumps(metadata(), ensure_ascii=True), encoding="utf-8")
            ranges_path.write_text(range_text(end=9), encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(GENERATOR_PATH),
                    "--findings",
                    str(findings_path),
                    "--metadata",
                    str(metadata_path),
                    "--ranges",
                    str(ranges_path),
                    "--output",
                    str(output_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        self.assert_cli_invalid(completed, "outside pr.diff.ranges.txt")

    def test_generator_fails_when_ranges_file_is_provided_but_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            findings_path = tmp_path / "findings.verified.json"
            metadata_path = tmp_path / "metadata.json"
            ranges_path = tmp_path / "pr.diff.ranges.txt"
            output_path = tmp_path / "findings.sarif"
            findings_path.write_text(json.dumps(canonical_artifact(), ensure_ascii=True), encoding="utf-8")
            metadata_path.write_text(json.dumps(metadata(), ensure_ascii=True), encoding="utf-8")
            ranges_path.write_text("", encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(GENERATOR_PATH),
                    "--findings",
                    str(findings_path),
                    "--metadata",
                    str(metadata_path),
                    "--ranges",
                    str(ranges_path),
                    "--output",
                    str(output_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        self.assert_cli_invalid(completed, "outside pr.diff.ranges.txt")

    def test_post_policy_suppress_does_not_leak_to_sarif(self) -> None:
        suppressed = make_finding(
            severity="should_fix",
            category="performance",
            title="`cache` should be local only",
            path="src/App.php",
            line=16,
            post_policy="suppress",
        )
        artifact = canonical_artifact(default_findings() + [suppressed])
        sarif = build_sarif(artifact, metadata=metadata(), ranges={"src/App.php": [(1, 20)]})
        emitted_ids = {result["properties"]["source_finding_id"] for result in sarif["runs"][0]["results"]}
        self.assertNotIn(suppressed["id"], emitted_ids)
        self.assertEqual(validate_findings_sarif(self.schema, sarif, findings=artifact), [])

    def test_local_only_uses_sarif_suppression(self) -> None:
        local_only = make_finding(
            severity="should_fix",
            category="design",
            title="`helper` should remain reviewer-only",
            path="src/App.php",
            line=18,
            post_policy="local_only",
        )
        artifact = canonical_artifact([local_only])
        sarif = build_sarif(artifact, metadata=metadata(), ranges={"src/App.php": [(1, 20)]})
        result = sarif["runs"][0]["results"][0]
        self.assertEqual(result["level"], "warning")
        self.assertEqual(result["suppressions"][0]["justification"], "local_only per pr-codex post_policy")

    def test_note_severity_maps_to_sarif_none_level(self) -> None:
        note = make_finding(
            severity="note",
            category="tests",
            title="`coverage` is informational only",
            path="src/App.php",
            line=19,
            post_policy="local_only",
        )
        artifact = canonical_artifact([note])
        sarif = build_sarif(artifact, metadata=metadata(), ranges={"src/App.php": [(1, 20)]})
        result = sarif["runs"][0]["results"][0]
        self.assertEqual(result["level"], "none")
        self.assertEqual(validate_findings_sarif(self.schema, sarif, findings=artifact), [])

    def test_partial_fingerprint_and_guid_are_stable_across_runs(self) -> None:
        artifact = canonical_artifact()
        first = build_sarif(artifact, metadata=metadata(), ranges={"src/App.php": [(1, 20)]})
        second = build_sarif(copy.deepcopy(artifact), metadata=metadata(), ranges={"src/App.php": [(1, 20)]})
        first_result = first["runs"][0]["results"][0]
        second_result = second["runs"][0]["results"][0]
        self.assertEqual(first_result["partialFingerprints"]["canonical"], second_result["partialFingerprints"]["canonical"])
        self.assertEqual(first_result["guid"], second_result["guid"])

    def test_cli_generates_and_validates_counts_against_markdown_and_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            findings_path = tmp_path / "findings.verified.json"
            metadata_path = tmp_path / "metadata.json"
            ranges_path = tmp_path / "pr.diff.ranges.txt"
            sarif_path = tmp_path / "findings.sarif"
            review_path = tmp_path / "review.md"
            payload_path = tmp_path / "review-payload.json"
            findings_path.write_text(json.dumps(canonical_artifact(), ensure_ascii=True), encoding="utf-8")
            metadata_path.write_text(json.dumps(metadata(), ensure_ascii=True), encoding="utf-8")
            ranges_path.write_text(range_text(), encoding="utf-8")
            review_path.write_text("## 総評\n\nok\n\n## 重大な問題 (Must Fix)\n\n### `src/App.php:L10`\n", encoding="utf-8")
            payload_path.write_text(json.dumps({"comments": [{"path": "src/App.php", "line": 10, "side": "RIGHT", "body": "x"}]}), encoding="utf-8")
            generated = subprocess.run(
                [sys.executable, str(GENERATOR_PATH), "--findings", str(findings_path), "--metadata", str(metadata_path), "--ranges", str(ranges_path), "--output", str(sarif_path)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(generated.returncode, 0, generated.stderr)
            validated = subprocess.run(
                [
                    sys.executable,
                    str(VALIDATOR_PATH),
                    "--schema",
                    str(SCHEMA_PATH),
                    "--data",
                    str(sarif_path),
                    "--findings",
                    str(findings_path),
                    "--ranges",
                    str(ranges_path),
                    "--markdown",
                    str(review_path),
                    "--payload",
                    str(payload_path),
                    "--emit-counts",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual(validated.returncode, 0, validated.stderr)
        self.assertEqual(json.loads(validated.stdout), {"canonical_must_fix": 1, "markdown_must_fix": 1, "payload_must_fix": 1, "sarif_must_fix": 1})

    def test_payload_count_includes_out_of_range_body_section(self) -> None:
        artifact = canonical_artifact()
        sarif = build_sarif(artifact, metadata=metadata(), ranges={"src/App.php": [(1, 20)]})
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sarif_path = tmp_path / "findings.sarif"
            findings_path = tmp_path / "findings.verified.json"
            review_path = tmp_path / "review.md"
            payload_path = tmp_path / "review-payload.json"
            sarif_path.write_text(json.dumps(sarif), encoding="utf-8")
            findings_path.write_text(json.dumps(artifact), encoding="utf-8")
            review_path.write_text("## 重大な問題 (Must Fix)\n\n### `src/App.php:L10`\n", encoding="utf-8")
            payload_path.write_text(
                json.dumps(
                    {
                        "comments": [],
                        "body": "ok\n\n## 行コメント不可 (diff 範囲外)\n\n### `src/App.php:L10`\n\n- 問題: x\n",
                    }
                ),
                encoding="utf-8",
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    str(VALIDATOR_PATH),
                    "--schema",
                    str(SCHEMA_PATH),
                    "--data",
                    str(sarif_path),
                    "--findings",
                    str(findings_path),
                    "--markdown",
                    str(review_path),
                    "--payload",
                    str(payload_path),
                    "--emit-counts",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["payload_must_fix"], 1)

    def test_validator_fails_when_ranges_file_is_provided_but_empty(self) -> None:
        artifact = canonical_artifact()
        sarif = build_sarif(artifact, metadata=metadata(), ranges=None)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sarif_path = tmp_path / "findings.sarif"
            findings_path = tmp_path / "findings.verified.json"
            ranges_path = tmp_path / "pr.diff.ranges.txt"
            sarif_path.write_text(json.dumps(sarif), encoding="utf-8")
            findings_path.write_text(json.dumps(artifact), encoding="utf-8")
            ranges_path.write_text("", encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(VALIDATOR_PATH),
                    "--schema",
                    str(SCHEMA_PATH),
                    "--data",
                    str(sarif_path),
                    "--findings",
                    str(findings_path),
                    "--ranges",
                    str(ranges_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        self.assert_cli_invalid(completed, "outside pr.diff.ranges.txt")

    def test_validator_rejects_windows_absolute_paths_in_messages_and_locations(self) -> None:
        artifact = canonical_artifact()
        sarif = build_sarif(artifact, metadata=metadata(), ranges={"src/App.php": [(1, 20)]})
        sarif["runs"][0]["results"][0]["message"]["text"] = r"Leaked C:\Users\alice\repo\secret.txt"
        sarif["runs"][0]["results"][1]["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] = r"C:\Users\alice\repo\src\App.php"
        errors = validate_findings_sarif(self.schema, sarif, findings=artifact)
        self.assertTrue(any("message.text: must not leak host absolute paths" in error for error in errors), errors)
        self.assertTrue(any("artifactLocation.uri: must be a repository-relative URI path" in error for error in errors), errors)

    def test_validator_rejects_unsafely_scrubbed_file_uri_suffixes(self) -> None:
        artifact = canonical_artifact()
        sarif = build_sarif(artifact, metadata=metadata(), ranges={"src/App.php": [(1, 20)]})
        sarif["runs"][0]["results"][0]["message"]["text"] = "Leaked <absolute-path>:/Users/alice/repo/secret.txt"
        errors = validate_findings_sarif(self.schema, sarif, findings=artifact)
        self.assertTrue(any("message.text: must not leak host absolute paths" in error for error in errors), errors)

    def test_validator_rederives_guid_severity_and_post_policy_from_canonical(self) -> None:
        local_only = make_finding(
            severity="should_fix",
            category="design",
            title="`helper` should remain reviewer-only",
            path="src/App.php",
            line=18,
            post_policy="local_only",
        )
        artifact = canonical_artifact([local_only])
        sarif = build_sarif(artifact, metadata=metadata(), ranges={"src/App.php": [(1, 20)]})
        result = sarif["runs"][0]["results"][0]
        result["guid"] = "00000000-0000-4000-8000-000000000000"
        result["ruleId"] = "pr-codex/bug"
        result["ruleIndex"] = 0
        result["level"] = "none"
        result["properties"]["severity"] = "note"
        result["properties"]["category"] = "bug"
        result["properties"]["post_policy"] = "body_summary"
        result.pop("suppressions", None)
        errors = validate_findings_sarif(self.schema, sarif, findings=artifact)
        for expected in (
            "guid: must equal deterministic UUIDv5",
            "properties.severity: must match canonical severity",
            "level: must map from canonical severity should_fix",
            "properties.category: must match canonical category",
            "properties.post_policy: must match canonical posting.post_policy",
            "suppressions: canonical local_only findings must use SARIF suppression",
        ):
            self.assertTrue(any(expected in error for error in errors), errors)

    def test_validator_requires_suppression_for_canonical_nit_even_if_sarif_local_severity_changes(self) -> None:
        nit = make_finding(
            severity="nit",
            category="code_quality",
            title="`name` can be clearer",
            path="src/App.php",
            line=14,
            post_policy="body_summary",
        )
        artifact = canonical_artifact([nit])
        sarif = build_sarif(artifact, metadata=metadata(), ranges={"src/App.php": [(1, 20)]})
        result = sarif["runs"][0]["results"][0]
        result["level"] = "warning"
        result["properties"]["severity"] = "should_fix"
        result.pop("suppressions", None)
        errors = validate_findings_sarif(self.schema, sarif, findings=artifact)
        self.assertTrue(any("properties.severity: must match canonical severity" in error for error in errors), errors)
        self.assertTrue(any("suppressions: canonical nit findings must be suppressed" in error for error in errors), errors)

    def test_validator_reports_must_fix_count_mismatch(self) -> None:
        artifact = canonical_artifact()
        sarif = build_sarif(artifact, metadata=metadata(), ranges={"src/App.php": [(1, 20)]})
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sarif_path = tmp_path / "findings.sarif"
            findings_path = tmp_path / "findings.verified.json"
            review_path = tmp_path / "review.md"
            sarif_path.write_text(json.dumps(sarif), encoding="utf-8")
            findings_path.write_text(json.dumps(artifact), encoding="utf-8")
            review_path.write_text("## 重大な問題 (Must Fix)\n\nなし\n", encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, str(VALIDATOR_PATH), "--schema", str(SCHEMA_PATH), "--data", str(sarif_path), "--findings", str(findings_path), "--markdown", str(review_path)],
                check=False,
                capture_output=True,
                text=True,
            )
        self.assert_cli_invalid(completed, "must_fix_count_mismatch")

    def test_official_schema_rejects_invalid_sarif_types(self) -> None:
        artifact = canonical_artifact()
        sarif = build_sarif(artifact, metadata=metadata(), ranges={"src/App.php": [(1, 20)]})
        sarif["runs"][0]["tool"]["driver"]["informationUri"] = 123
        sarif["runs"][0]["automationDetails"]["id"] = 456
        errors = validate_findings_sarif(self.schema, sarif, findings=artifact)
        self.assertTrue(errors, "official SARIF schema violations must be reported")
        self.assertTrue(any("official SARIF schema violation" in error for error in errors), errors)
        self.assertTrue(any("informationUri" in error for error in errors), errors)
        self.assertTrue(any("automationDetails.id" in error for error in errors), errors)

    def test_small_fixture_diff_end_to_end_generation(self) -> None:
        finding = make_finding(
            severity="should_fix",
            category="consistency",
            title="`AbstractApp` deprecation should keep migration guidance",
            path="src-deprecated/Extension/Application/AbstractApp.php",
            line=12,
            post_policy="body_summary",
        )
        fixture_metadata = json.loads((ROOT / "fixtures" / "small" / "metadata.json").read_text(encoding="utf-8"))["source"]
        artifact = canonical_artifact([finding])
        artifact["pr"] = {
            "repository": fixture_metadata["repository"],
            "number": fixture_metadata["pr_number"],
            "base_sha": fixture_metadata["base_sha"],
            "head_sha": fixture_metadata["head_sha"],
            "merge_commit_sha": fixture_metadata["merge_commit_sha"],
        }
        ranges = {"composer.json": [(23, 33)], "src-deprecated/Extension/Application/AbstractApp.php": [(10, 20)]}
        sarif = build_sarif(artifact, metadata={"branch": "fixture/small"}, ranges=ranges)
        self.assertEqual(validate_findings_sarif(self.schema, sarif, findings=artifact), [])
        self.assertEqual(sarif["runs"][0]["versionControlProvenance"][0]["repositoryUri"], "https://github.com/bearsunday/BEAR.Sunday")


if __name__ == "__main__":
    unittest.main()
