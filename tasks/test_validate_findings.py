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
            "version": "1.5.0",
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


def valid_metadata() -> dict[str, object]:
    return {
        "org": "yuki777",
        "repository": "pr-codex",
        "repository_full_name": "yuki777/pr-codex",
        "pr_number": 25,
        "pr_url": "https://github.com/yuki777/pr-codex/pull/25",
        "head_sha": "e8763f5edddeca5be7334ac9131066be09f19a6d",
        "base_sha": "2499605587c910c1911729e90d4c96b61210c628",
        "branch": "feat/16",
        "base_branch": "main",
        "merge_commit_sha": None,
        "title": "Canonical findings",
        "files": ["skills/send/SKILL.md"],
    }


class ValidateFindingsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    def assert_invalid_without_crash(
        self,
        artifact: dict[str, object],
        expected_fragment: str,
        metadata: dict[str, object] | None = None,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_path = Path(tmp) / "findings.verified.json"
            data_path.write_text(json.dumps(artifact, ensure_ascii=True), encoding="utf-8")
            command = [sys.executable, str(VALIDATOR_PATH), "--schema", str(SCHEMA_PATH), "--data", str(data_path)]
            if metadata is not None:
                metadata_path = Path(tmp) / "metadata.json"
                metadata_path.write_text(json.dumps(metadata, ensure_ascii=True), encoding="utf-8")
                command.extend(["--metadata", str(metadata_path)])
            result = subprocess.run(
                command,
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
        self.assertEqual(validate_artifact(self.schema, valid_artifact(), valid_metadata()), [])

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

    def test_evidence_ladder_adoption_thresholds_are_validator_enforced(self) -> None:
        cases = {
            "must-fix-requires-verified": (
                lambda f: f.update(evidence_level="corroborated"),
                ".evidence_level: must_fix findings must use evidence_level=verified",
            ),
            "should-fix-requires-corroborated-or-higher": (
                lambda f: (
                    f.update(severity="should_fix", evidence_level="suspicion"),
                    f.update(
                        posting={
                            "post_policy": "local_only",
                            "explanation_postable": False,
                            "not_postable_reason": "low_evidence_suspicion",
                            "audience": "human_reviewer",
                        }
                    ),
                ),
                ".evidence_level: should_fix findings require evidence_level=corroborated or higher",
            ),
        }
        for name, (mutate, expected_fragment) in cases.items():
            with self.subTest(name=name):
                artifact = copy.deepcopy(valid_artifact())
                mutate(artifact["findings"][0])
                self.assert_invalid_without_crash(artifact, expected_fragment)

    def test_axes_are_required(self) -> None:
        artifact = copy.deepcopy(valid_artifact())
        del artifact["findings"][0]["axes"]
        self.assert_invalid_without_crash(artifact, "missing required properties: axes")

    def test_malformed_evidence_url_is_invalid_without_crash(self) -> None:
        artifact = copy.deepcopy(valid_artifact())
        finding = artifact["findings"][0]
        finding["evidence"] = [{"type": "reference", "url": "http://[bad"}]
        self.assert_invalid_without_crash(artifact, ".evidence[0].url: must be a URI")

    def test_malformed_enum_values_are_invalid_without_crash(self) -> None:
        mutations = {
            "severity-list": (lambda f: f.update(severity=[]), ".severity: invalid value"),
            "category-dict": (lambda f: f.update(category={}), ".category: invalid value"),
            "evidence-level-list": (lambda f: f.update(evidence_level=[]), ".evidence_level: invalid value"),
            "location-side-list": (lambda f: f["location"].update(side=[]), ".location.side: must be LEFT or RIGHT"),
            "axis-value-list": (lambda f: f["axes"].update(real=[]), ".axes.real: invalid value"),
            "post-policy-list": (lambda f: f["posting"].update(post_policy=[]), ".posting.post_policy: invalid value"),
            "not-postable-reason-list": (
                lambda f: f["posting"].update(explanation_postable=False, not_postable_reason=[]),
                ".posting.not_postable_reason: invalid value",
            ),
            "audience-list": (lambda f: f["posting"].update(post_policy="local_only", audience=[]), ".posting.audience: invalid value"),
            "severity-by-source-list": (
                lambda f: f.update(severity_disputed=True, severity_by_source={"claude": []}, merger_rule_applied="none", verifier_required=True),
                ".severity_by_source.claude: invalid severity",
            ),
            "merger-rule-list": (
                lambda f: f.update(severity_disputed=True, severity_by_source={"claude": "must_fix"}, merger_rule_applied=[], verifier_required=True),
                ".merger_rule_applied: invalid value",
            ),
            "evidence-type-list": (lambda f: f.update(evidence=[{"type": []}]), ".evidence[0].type: invalid value"),
        }
        for name, (mutate, expected_fragment) in mutations.items():
            with self.subTest(name=name):
                artifact = copy.deepcopy(valid_artifact())
                mutate(artifact["findings"][0])
                self.assert_invalid_without_crash(artifact, expected_fragment)

    def test_m1_send_contract_is_validator_enforced(self) -> None:
        mutations = {
            "non-must-inline": (
                lambda f: f.update(severity="should_fix"),
                "only must_fix findings may use post_policy=inline",
            ),
            "must-fix-non-inline": (
                lambda f: f["posting"].update(post_policy="body_summary"),
                "must_fix findings must use post_policy=inline",
            ),
            "must-fix-not-postable": (
                lambda f: f["posting"].update(explanation_postable=False, not_postable_reason="other_explained"),
                "must_fix findings must set explanation_postable=true",
            ),
            "must-fix-left-side": (
                lambda f: f["location"].update(side="LEFT"),
                "must_fix findings must target location.side=RIGHT",
            ),
            "must-fix-inline-with-not-postable-reason": (
                lambda f: f["posting"].update(not_postable_reason="security_detail"),
                "not_postable_reason: only allowed when explanation_postable=false",
            ),
        }
        for name, (mutate, expected_fragment) in mutations.items():
            with self.subTest(name=name):
                artifact = copy.deepcopy(valid_artifact())
                mutate(artifact["findings"][0])
                self.assert_invalid_without_crash(artifact, expected_fragment)

    def test_must_fix_four_axes_gate_is_validator_enforced(self) -> None:
        gate_message = (
            "must_fix requires axes={real,triggerable,impactful}=yes "
            "and (general=yes or evidence_level in {impact_explained, verified})"
        )
        mutations = {
            "real-no": (
                lambda f: f["axes"].update(real="no"),
                gate_message,
            ),
            "triggerable-unknown": (
                lambda f: f["axes"].update(triggerable="unknown"),
                gate_message,
            ),
            "impactful-unknown": (
                lambda f: f["axes"].update(impactful="unknown"),
                gate_message,
            ),
            "general-unknown-without-specific-impact": (
                lambda f: (f["axes"].update(general="unknown"), f.update(evidence_level="trigger_path_identified")),
                gate_message,
            ),
            "specific-general-without-impact-evidence": (
                lambda f: (f["axes"].update(general="no"), f.update(evidence_level="trigger_path_identified")),
                gate_message,
            ),
            "suspicion": (
                lambda f: f.update(evidence_level="suspicion"),
                "must_fix findings must not use evidence_level=suspicion",
            ),
        }
        for name, (mutate, expected_fragment) in mutations.items():
            with self.subTest(name=name):
                artifact = copy.deepcopy(valid_artifact())
                mutate(artifact["findings"][0])
                self.assert_invalid_without_crash(artifact, expected_fragment)

    def test_general_no_must_fix_passes_when_verified_specific_impact_is_explained(self) -> None:
        artifact = copy.deepcopy(valid_artifact())
        artifact["findings"][0]["axes"]["general"] = "no"
        artifact["findings"][0]["evidence_level"] = "verified"
        self.assertEqual(validate_artifact(self.schema, artifact), [])

    def test_general_unknown_must_fix_passes_when_verified_specific_impact_is_explained(self) -> None:
        artifact = copy.deepcopy(valid_artifact())
        artifact["findings"][0]["axes"]["general"] = "unknown"
        artifact["findings"][0]["evidence_level"] = "verified"
        self.assertEqual(validate_artifact(self.schema, artifact), [])

    def test_unknown_axes_are_allowed_below_must_fix(self) -> None:
        artifact = copy.deepcopy(valid_artifact())
        finding = artifact["findings"][0]
        finding["severity"] = "should_fix"
        finding["axes"].update(triggerable="unknown", impactful="unknown")
        finding["posting"]["post_policy"] = "body_summary"
        self.assertEqual(validate_artifact(self.schema, artifact), [])

    def test_duplicate_ids_and_fingerprints_are_invalid(self) -> None:
        artifact = copy.deepcopy(valid_artifact())
        artifact["findings"].append(copy.deepcopy(artifact["findings"][0]))
        self.assert_invalid_without_crash(artifact, "duplicate id/fingerprint")

    def test_pr_context_must_match_metadata(self) -> None:
        cases = {
            "canonical-fork-repository": (
                lambda artifact, metadata: artifact["pr"].update(repository="fork/pr-codex"),
                "$.pr.repository: must match metadata.repository_full_name",
            ),
            "metadata-fork-repository": (
                lambda artifact, metadata: metadata.update(repository_full_name="fork/pr-codex"),
                "metadata.repository_full_name must equal metadata org/repository posting target",
            ),
            "pr-number": (
                lambda artifact, metadata: artifact["pr"].update(number=26),
                "$.pr.number: must match metadata.pr_number",
            ),
            "head-sha": (
                lambda artifact, metadata: artifact["pr"].update(head_sha="a" * 40),
                "$.pr.head_sha: must match metadata.head_sha",
            ),
            "base-sha": (
                lambda artifact, metadata: artifact["pr"].update(base_sha="b" * 40),
                "$.pr.base_sha: must match metadata.base_sha",
            ),
        }
        for name, (mutate, expected_fragment) in cases.items():
            with self.subTest(name=name):
                artifact = copy.deepcopy(valid_artifact())
                metadata = copy.deepcopy(valid_metadata())
                mutate(artifact, metadata)
                self.assert_invalid_without_crash(artifact, expected_fragment, metadata)

    def test_lone_surrogate_strings_are_invalid_without_crash(self) -> None:
        cases = {
            "title": "fingerprint: cannot compute canonical fingerprint",
            "problem": ".problem: must be a non-empty UTF-8 string without surrogate/control characters",
        }
        for field, expected_fragment in cases.items():
            with self.subTest(field=field):
                artifact = copy.deepcopy(valid_artifact())
                artifact["findings"][0][field] = "\ud800"
                self.assert_invalid_without_crash(artifact, expected_fragment)

    def test_fingerprint_golden_vector(self) -> None:
        self.assertEqual(
            valid_artifact()["findings"][0]["fingerprint"],
            "ef28e327aaa0533331150e3c6661f3b567c93d3e7de124765e980daa3406a1d1",
        )

    def test_id_must_equal_recomputed_fingerprint(self) -> None:
        artifact = copy.deepcopy(valid_artifact())
        artifact["findings"][0]["id"] = "0" * 64
        self.assert_invalid_without_crash(artifact, "id must equal fingerprint")


if __name__ == "__main__":
    unittest.main()
