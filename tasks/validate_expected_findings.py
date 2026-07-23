#!/usr/bin/env python3
"""Validate expected-findings.v1 fixture oracle artifacts.

This validator is intentionally stdlib-only, mirroring the runtime artifact
validators. JSON Schema documents the shape, while this module enforces the
portable cross-field rules used by the fixture scoring runner:

* required wrapper/source/finding fields are present;
* enum/range/date/string fields are well formed;
* expected finding ids are unique;
* acceptable_overrides only use the selected profile's relaxed direction; and
* line_range values are ordered.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

SCHEMA_VERSION = "expected-findings.v1"
SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
REPOSITORY_RE = re.compile(r"^[^/\s]+/[^/\s]+$")

SEVERITIES = {"must_fix", "should_fix", "nit", "note"}
CATEGORIES = {"bug", "security", "performance", "tests", "design", "code_quality", "consistency", "runtime_error"}
AXIS_VALUES = {"yes", "no", "unknown"}
AXES = ("real", "triggerable", "impactful")
BLAST_RADII = {"isolated", "component", "systemic", "unknown"}
EVIDENCE_LEVELS = ("suspicion", "corroborated", "trigger_path_identified", "impact_explained", "verified")
EVIDENCE_LEVEL_SET = set(EVIDENCE_LEVELS)
EXPECTED_OUTCOMES = {"known_bug", "known_false_positive_trap", "acceptable_risk", "out_of_scope", "no_expected_finding"}
STRICTNESS_PROFILES = {"must_fix_strict", "should_fix_lax", "noise_filter"}
FIXTURE_TYPES = {"negative_real_world", "positive_seeded"}
PROVENANCE_METHODS = {"frozen_merged_pr", "synthetic_overlay"}
TOP_KEYS = {"schema_version", "fixture_id", "fixture_type", "provenance", "source", "pr_intent", "scoring_gate", "expected_findings"}
SOURCE_KEYS = {
    "repository",
    "pr_number",
    "pr_title",
    "base_sha",
    "head_sha",
    "merge_commit_sha",
    "source_url",
    "license",
    "license_notice_path",
    "frozen_patch_path",
    "retrieved_at",
}
PROVENANCE_KEYS = {"method", "base_snapshot_path", "authored_at", "seeded_bugs"}
SEEDED_BUG_KEYS = {"id", "path", "line", "bug_class", "trigger", "impact"}
SCORING_GATE_KEYS = {"exact_pass_rate_min", "acceptable_pass_rate_min", "false_positive_rate_max", "recall_known_bug_min"}
EXPECTED_FINDING_KEYS = {
    "id",
    "seed_id",
    "expected_outcome",
    "title",
    "category",
    "severity",
    "acceptable_severities",
    "strictness_profile",
    "expected_axes",
    "acceptable_overrides",
    "expected_blast_radius",
    "minimum_evidence_level",
    "location_match",
    "acceptable_alternatives",
    "should_be_caught_by",
    "out_of_scope_reason",
    "oracle_notes",
}
LOCATION_KEYS = {"path", "line", "line_range"}
ALTERNATIVE_KEYS = {"path_glob", "line_range", "title_regex", "problem_regex", "category"}

PROFILE_RELAXED_AXES: dict[str, dict[str, set[str]]] = {
    "must_fix_strict": {
        "real": {"yes", "unknown"},
        "triggerable": {"yes", "unknown"},
        "impactful": {"yes", "unknown"},
    },
    "should_fix_lax": {
        "real": {"yes", "unknown"},
        "triggerable": {"yes", "unknown", "no"},
        "impactful": {"yes", "unknown", "no"},
    },
    "noise_filter": {
        "real": {"no", "unknown"},
        "triggerable": {"no", "unknown"},
        "impactful": {"no", "unknown"},
    },
}


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - CLI reports parse/path failures uniformly
        raise ValueError(f"{path}: cannot read/parse JSON: {exc}") from exc


def non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and len(value) >= 1 and all(
        not (ord(char) <= 0x1F or 0x7F <= ord(char) <= 0x9F or 0xD800 <= ord(char) <= 0xDFFF)
        for char in value
    )


def positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def number_0_1(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= float(value) <= 1


def is_rfc3339(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return False
    return parsed.tzinfo is not None


def is_uri(value: Any) -> bool:
    if not non_empty_string(value):
        return False
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return bool(parsed.scheme and (parsed.netloc or parsed.scheme in {"urn", "file"}))


def add_unexpected(errors: list[str], path: str, obj: Any, allowed: set[str]) -> None:
    if isinstance(obj, dict):
        extra = sorted(set(obj) - allowed)
        if extra:
            errors.append(f"{path}: unexpected properties: {', '.join(extra)}")


def require(errors: list[str], path: str, obj: dict[str, Any], required: set[str]) -> None:
    missing = sorted(required - set(obj))
    if missing:
        errors.append(f"{path}: missing required properties: {', '.join(missing)}")


def validate_string(errors: list[str], path: str, obj: dict[str, Any], key: str) -> None:
    if key in obj and not non_empty_string(obj[key]):
        errors.append(f"{path}.{key}: must be a non-empty UTF-8 string without control/surrogate characters")


def validate_enum(errors: list[str], path: str, value: Any, allowed: set[str], label: str = "invalid value") -> bool:
    if not isinstance(value, str) or value not in allowed:
        errors.append(f"{path}: {label}")
        return False
    return True


def validate_line_range(errors: list[str], path: str, value: Any) -> None:
    if not isinstance(value, list) or len(value) != 2 or any(not positive_int(item) for item in value):
        errors.append(f"{path}: must be [start, end] positive integers")
        return
    if value[1] < value[0]:
        errors.append(f"{path}: end must be >= start")


def validate_axis_object(errors: list[str], path: str, value: Any) -> bool:
    if not isinstance(value, dict):
        errors.append(f"{path}: must be an object")
        return False
    add_unexpected(errors, path, value, set(AXES))
    require(errors, path, value, set(AXES))
    valid = True
    for axis in AXES:
        valid = validate_enum(errors, f"{path}.{axis}", value.get(axis), AXIS_VALUES) and valid
    return valid


def validate_axis_overrides(errors: list[str], path: str, value: Any, profile: Any) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        errors.append(f"{path}: must be an object")
        return
    add_unexpected(errors, path, value, set(AXES))
    relaxed = PROFILE_RELAXED_AXES.get(profile)
    for axis, axis_values in value.items():
        item_path = f"{path}.{axis}"
        if not isinstance(axis_values, list) or not axis_values:
            errors.append(f"{item_path}: must be a non-empty array")
            continue
        if len(axis_values) != len(set(axis_values)):
            errors.append(f"{item_path}: values must be unique")
        for axis_value in axis_values:
            validate_enum(errors, f"{item_path}[]", axis_value, AXIS_VALUES)
        if relaxed is not None:
            invalid = sorted(set(axis_values) - relaxed[axis])
            if invalid:
                errors.append(
                    f"{item_path}: values {invalid} contradict strictness_profile={profile}; "
                    "acceptable_overrides may only relax the selected profile"
                )


def validate_location(errors: list[str], path: str, value: Any) -> None:
    if not isinstance(value, dict):
        errors.append(f"{path}: must be an object")
        return
    add_unexpected(errors, path, value, LOCATION_KEYS)
    require(errors, path, value, {"path"})
    validate_string(errors, path, value, "path")
    if "line" in value and not positive_int(value["line"]):
        errors.append(f"{path}.line: must be an integer >= 1")
    if "line_range" in value:
        validate_line_range(errors, f"{path}.line_range", value["line_range"])


def validate_alternatives(errors: list[str], path: str, value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, list):
        errors.append(f"{path}: must be an array")
        return
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{item_path}: must be an object")
            continue
        add_unexpected(errors, item_path, item, ALTERNATIVE_KEYS)
        for key in ("path_glob", "title_regex", "problem_regex"):
            validate_string(errors, item_path, item, key)
        if "category" in item:
            validate_enum(errors, f"{item_path}.category", item["category"], CATEGORIES)
        if "line_range" in item:
            validate_line_range(errors, f"{item_path}.line_range", item["line_range"])


def validate_provenance(errors: list[str], fixture_type: Any, value: Any) -> set[str]:
    if not isinstance(value, dict):
        errors.append("$.provenance: must be an object")
        return set()
    add_unexpected(errors, "$.provenance", value, PROVENANCE_KEYS)
    require(errors, "$.provenance", value, {"method"})
    method = value.get("method")
    validate_enum(errors, "$.provenance.method", method, PROVENANCE_METHODS)
    if fixture_type == "negative_real_world":
        if method != "frozen_merged_pr":
            errors.append("$.provenance.method: negative_real_world requires frozen_merged_pr")
        if any(key in value for key in ("base_snapshot_path", "authored_at", "seeded_bugs")):
            errors.append("$.provenance: negative_real_world must not declare synthetic seed fields")
        return set()
    if fixture_type != "positive_seeded":
        return set()
    if method != "synthetic_overlay":
        errors.append("$.provenance.method: positive_seeded requires synthetic_overlay")
    require(errors, "$.provenance", value, {"base_snapshot_path", "authored_at", "seeded_bugs"})
    validate_string(errors, "$.provenance", value, "base_snapshot_path")
    if "authored_at" in value and not is_rfc3339(value["authored_at"]):
        errors.append("$.provenance.authored_at: must be RFC3339 date-time with timezone")
    seeded_bugs = value.get("seeded_bugs")
    if not isinstance(seeded_bugs, list) or not seeded_bugs:
        errors.append("$.provenance.seeded_bugs: must be a non-empty array")
        return set()
    seed_ids: set[str] = set()
    for index, seed in enumerate(seeded_bugs):
        path = f"$.provenance.seeded_bugs[{index}]"
        if not isinstance(seed, dict):
            errors.append(f"{path}: must be an object")
            continue
        add_unexpected(errors, path, seed, SEEDED_BUG_KEYS)
        require(errors, path, seed, SEEDED_BUG_KEYS)
        for key in ("id", "path", "bug_class", "trigger", "impact"):
            validate_string(errors, path, seed, key)
        if not positive_int(seed.get("line")):
            errors.append(f"{path}.line: must be an integer >= 1")
        seed_id = seed.get("id")
        if isinstance(seed_id, str):
            if seed_id in seed_ids:
                errors.append(f"{path}.id: duplicate seed id {seed_id}")
            seed_ids.add(seed_id)
    return seed_ids


def validate_expected_findings(data: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["$: must be an object"]

    add_unexpected(errors, "$", data, TOP_KEYS)
    require(errors, "$", data, {"schema_version", "fixture_id", "fixture_type", "provenance", "source", "pr_intent", "expected_findings"})
    if data.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"$.schema_version: must equal {SCHEMA_VERSION}")
    validate_string(errors, "$", data, "fixture_id")
    fixture_type = data.get("fixture_type")
    validate_enum(errors, "$.fixture_type", fixture_type, FIXTURE_TYPES)
    seed_ids = validate_provenance(errors, fixture_type, data.get("provenance"))
    validate_string(errors, "$", data, "pr_intent")

    source = data.get("source")
    if not isinstance(source, dict):
        errors.append("$.source: must be an object")
    else:
        add_unexpected(errors, "$.source", source, SOURCE_KEYS)
        require(errors, "$.source", source, {"repository", "pr_number", "base_sha", "head_sha", "source_url", "license", "frozen_patch_path"})
        repository = source.get("repository")
        if not isinstance(repository, str) or not REPOSITORY_RE.match(repository):
            errors.append("$.source.repository: must be owner/repo")
        if not positive_int(source.get("pr_number")):
            errors.append("$.source.pr_number: must be an integer >= 1")
        for key in ("base_sha", "head_sha", "merge_commit_sha"):
            if key in source and (not isinstance(source[key], str) or not SHA_RE.match(source[key])):
                errors.append(f"$.source.{key}: must be 7-64 hex characters")
        for key in ("pr_title", "license", "license_notice_path", "frozen_patch_path"):
            validate_string(errors, "$.source", source, key)
        if "source_url" in source and not is_uri(source["source_url"]):
            errors.append("$.source.source_url: must be a URI")
        if "retrieved_at" in source and not is_rfc3339(source["retrieved_at"]):
            errors.append("$.source.retrieved_at: must be RFC3339 date-time with timezone")

    scoring_gate = data.get("scoring_gate")
    if scoring_gate is not None:
        if not isinstance(scoring_gate, dict):
            errors.append("$.scoring_gate: must be an object")
        else:
            add_unexpected(errors, "$.scoring_gate", scoring_gate, SCORING_GATE_KEYS)
            for key in SCORING_GATE_KEYS:
                if key in scoring_gate and not number_0_1(scoring_gate[key]):
                    errors.append(f"$.scoring_gate.{key}: must be a number between 0 and 1")

    expected_findings = data.get("expected_findings")
    if not isinstance(expected_findings, list) or not expected_findings:
        errors.append("$.expected_findings: must be a non-empty array")
        return errors

    seen_ids: set[str] = set()
    finding_seed_ids: set[str] = set()
    for index, finding in enumerate(expected_findings):
        fpath = f"$.expected_findings[{index}]"
        if not isinstance(finding, dict):
            errors.append(f"{fpath}: must be an object")
            continue
        add_unexpected(errors, fpath, finding, EXPECTED_FINDING_KEYS)
        require(errors, fpath, finding, {"id", "expected_outcome", "title", "category", "expected_axes", "expected_blast_radius", "strictness_profile", "minimum_evidence_level"})
        identifier = finding.get("id")
        if not non_empty_string(identifier):
            errors.append(f"{fpath}.id: must be a non-empty string")
        elif identifier in seen_ids:
            errors.append(f"{fpath}.id: duplicate expected finding id {identifier}")
        else:
            seen_ids.add(identifier)
        for key in ("title", "out_of_scope_reason", "oracle_notes"):
            validate_string(errors, fpath, finding, key)
        if "seed_id" in finding:
            validate_string(errors, fpath, finding, "seed_id")
            if isinstance(finding["seed_id"], str):
                finding_seed_ids.add(finding["seed_id"])
        if fixture_type == "positive_seeded" and finding.get("expected_outcome") == "known_bug" and "seed_id" not in finding:
            errors.append(f"{fpath}.seed_id: positive_seeded known_bug requires a seed id")
        if fixture_type != "positive_seeded" and "seed_id" in finding:
            errors.append(f"{fpath}.seed_id: only positive_seeded fixtures may reference seeds")
        validate_enum(errors, f"{fpath}.expected_outcome", finding.get("expected_outcome"), EXPECTED_OUTCOMES)
        validate_enum(errors, f"{fpath}.category", finding.get("category"), CATEGORIES)
        validate_enum(errors, f"{fpath}.strictness_profile", finding.get("strictness_profile"), STRICTNESS_PROFILES)
        validate_enum(errors, f"{fpath}.minimum_evidence_level", finding.get("minimum_evidence_level"), EVIDENCE_LEVEL_SET)
        validate_enum(errors, f"{fpath}.expected_blast_radius", finding.get("expected_blast_radius"), BLAST_RADII)
        if "severity" in finding:
            validate_enum(errors, f"{fpath}.severity", finding["severity"], SEVERITIES)
        if "acceptable_severities" in finding:
            values = finding["acceptable_severities"]
            if not isinstance(values, list) or not values:
                errors.append(f"{fpath}.acceptable_severities: must be a non-empty array")
            else:
                if len(values) != len(set(values)):
                    errors.append(f"{fpath}.acceptable_severities: values must be unique")
                for value in values:
                    validate_enum(errors, f"{fpath}.acceptable_severities[]", value, SEVERITIES)
        validate_axis_object(errors, f"{fpath}.expected_axes", finding.get("expected_axes"))
        validate_axis_overrides(errors, f"{fpath}.acceptable_overrides", finding.get("acceptable_overrides"), finding.get("strictness_profile"))
        if "location_match" in finding:
            validate_location(errors, f"{fpath}.location_match", finding["location_match"])
        validate_alternatives(errors, f"{fpath}.acceptable_alternatives", finding.get("acceptable_alternatives"))
        if "should_be_caught_by" in finding:
            values = finding["should_be_caught_by"]
            if not isinstance(values, list) or any(not non_empty_string(value) for value in values):
                errors.append(f"{fpath}.should_be_caught_by: must be an array of non-empty strings")
    if fixture_type == "positive_seeded":
        missing_seed_findings = sorted(seed_ids - finding_seed_ids)
        unknown_seed_references = sorted(finding_seed_ids - seed_ids)
        if missing_seed_findings:
            errors.append(f"$.expected_findings: seeded bugs without known_bug rows: {', '.join(missing_seed_findings)}")
        if unknown_seed_references:
            errors.append(f"$.expected_findings: unknown seed references: {', '.join(unknown_seed_references)}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate expected-findings.v1 fixture oracle")
    parser.add_argument("--schema", required=True, type=Path, help="schemas/expected-findings.v1.json path")
    parser.add_argument("--data", required=True, type=Path, help="expected-findings.json path")
    args = parser.parse_args()

    try:
        schema = load_json(args.schema)
        data = load_json(args.data)
    except ValueError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    if not isinstance(schema, dict) or schema.get("$id") is None:
        print(f"{args.schema}: invalid expected-findings schema file", file=sys.stderr)
        return 2

    errors = validate_expected_findings(data)
    if errors:
        print("INVALID expected-findings artifact", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("VALID expected-findings artifact")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
