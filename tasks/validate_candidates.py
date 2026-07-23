#!/usr/bin/env python3
"""Validate pr-codex findings.candidates.json artifacts.

This validator intentionally checks a looser hunter → verifier contract than
findings.v1. Candidate ids/fingerprints, 4-axis values, evidence_level, and
posting policy are verifier outputs, so candidates may omit them and do not need
id == fingerprint. The validator focuses on shape, safe strings, line range
sanity, and PR metadata consistency.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

EXPECTED_SCHEMA_VERSION = "findings.candidates.v1"
EXPECTED_SCHEMA_ID = "https://raw.githubusercontent.com/yuki777/pr-codex/main/schemas/findings.candidates.v1.json"
SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
REPOSITORY_RE = re.compile(r"^[^/\s]+/[^/\s]+$")
RFC3339_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")

TOP_LEVEL_KEYS = {"schema_version", "producer", "pr", "generated_at", "candidates"}
PRODUCER_KEYS = {"name", "version", "run_id"}
PR_KEYS = {"repository", "number", "base_sha", "head_sha", "merge_commit_sha"}
CANDIDATE_KEYS = {
    "candidate_id",
    "source_agent",
    "source_ref",
    "evidence_state",
    "location",
    "severity_raw",
    "category_raw",
    "title",
    "problem",
    "reason",
    "suggestion",
    "id",
    "fingerprint",
    "evidence_level",
    "axes",
    "blast_radius",
    "verification_hint",
    "posting",
}
LOCATION_KEYS = {"path", "start_line", "end_line", "side", "diff_hunk_ref"}
AXES_KEYS = {"real", "triggerable", "impactful"}
POSTING_KEYS = {"post_policy", "explanation_postable", "not_postable_reason", "audience"}
AXIS_VALUES = {"yes", "no", "unknown"}
BLAST_RADII = {"isolated", "component", "systemic", "unknown"}
EVIDENCE_STATES = {"supported", "needs_evidence"}
EVIDENCE_LEVELS = {"suspicion", "corroborated", "trigger_path_identified", "impact_explained", "verified"}
POST_POLICIES = {"inline", "body_summary", "local_only", "suppress"}
NOT_POSTABLE_REASONS = {"depends_on_pr_external_context", "security_detail", "too_long", "low_evidence_suspicion", "private_dependency", "other_explained"}
AUDIENCES = {"human_reviewer", "eval_harness", "future_memory"}


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"{path}: cannot read/parse JSON: {exc}") from exc


def is_safe_json_string(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False
    return all(unicodedata.category(char) not in {"Cc", "Cs"} for char in value)


def non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and len(value) >= 1 and is_safe_json_string(value)


def is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def is_rfc3339_datetime(value: Any) -> bool:
    if not isinstance(value, str) or not RFC3339_RE.match(value):
        return False
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return False
    return parsed.tzinfo is not None


def add_unexpected(errors: list[str], path: str, obj: Any, allowed: set[str]) -> None:
    if isinstance(obj, dict):
        extra = sorted(set(obj) - allowed)
        if extra:
            errors.append(f"{path}: unexpected properties: {', '.join(extra)}")


def validate_schema_file(schema: Any) -> list[str]:
    """Ensure --schema is the candidates schema, not just any JSON schema.

    The runtime gate intentionally requires callers to pass
    schemas/findings.candidates.v1.json so wiring mistakes (for example passing
    schemas/findings.v1.json) fail before artifact validation.
    """

    if not isinstance(schema, dict):
        return ["$schema: must be an object"]

    errors: list[str] = []
    if schema.get("$id") != EXPECTED_SCHEMA_ID:
        errors.append(f"$schema.$id: must equal '{EXPECTED_SCHEMA_ID}'")

    properties = schema.get("properties")
    if not isinstance(properties, dict):
        errors.append("$schema.properties: must be an object")
        return errors

    schema_version = properties.get("schema_version")
    if not isinstance(schema_version, dict) or schema_version.get("const") != EXPECTED_SCHEMA_VERSION:
        errors.append(f"$schema.properties.schema_version.const: must equal '{EXPECTED_SCHEMA_VERSION}'")

    candidates = properties.get("candidates")
    if not isinstance(candidates, dict) or candidates.get("type") != "array":
        errors.append("$schema.properties.candidates.type: must equal 'array'")

    return errors


def require_keys(errors: list[str], path: str, obj: dict[str, Any], required: set[str]) -> None:
    missing = sorted(required - set(obj))
    if missing:
        errors.append(f"{path}: missing required properties: {', '.join(missing)}")


def validate_string_field(errors: list[str], path: str, obj: dict[str, Any], key: str) -> None:
    if key in obj and not non_empty_string(obj[key]):
        errors.append(f"{path}.{key}: must be a non-empty UTF-8 string without surrogate/control characters")


def validate_pr(errors: list[str], data: dict[str, Any], metadata: Any | None) -> None:
    pr = data.get("pr")
    if not isinstance(pr, dict):
        errors.append("$.pr: must be an object")
        return
    add_unexpected(errors, "$.pr", pr, PR_KEYS)
    require_keys(errors, "$.pr", pr, {"repository", "number", "base_sha", "head_sha"})
    if not isinstance(pr.get("repository"), str) or not REPOSITORY_RE.match(str(pr.get("repository"))):
        errors.append("$.pr.repository: must be owner/repo")
    if not is_positive_int(pr.get("number")):
        errors.append("$.pr.number: must be an integer >= 1")
    for key in ("base_sha", "head_sha"):
        if not isinstance(pr.get(key), str) or not SHA_RE.match(str(pr.get(key))):
            errors.append(f"$.pr.{key}: must be 7-64 hex characters")
    merge_commit_sha = pr.get("merge_commit_sha")
    if merge_commit_sha is not None and (not isinstance(merge_commit_sha, str) or not SHA_RE.match(merge_commit_sha)):
        errors.append("$.pr.merge_commit_sha: must be null or 7-64 hex characters")

    if metadata is None:
        return
    if not isinstance(metadata, dict):
        errors.append("$metadata: must be an object")
        return
    comparisons = [
        ("repository", "repository_full_name", "$.pr.repository"),
        ("number", "pr_number", "$.pr.number"),
        ("head_sha", "head_sha", "$.pr.head_sha"),
        ("base_sha", "base_sha", "$.pr.base_sha"),
    ]
    for pr_key, metadata_key, path in comparisons:
        if metadata_key in metadata and pr.get(pr_key) != metadata.get(metadata_key):
            errors.append(f"{path}: must match metadata.{metadata_key}")


def validate_candidates(data: Any, metadata: Any | None = None) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["$: must be an object"]
    add_unexpected(errors, "$", data, TOP_LEVEL_KEYS)
    require_keys(errors, "$", data, TOP_LEVEL_KEYS)
    if data.get("schema_version") != EXPECTED_SCHEMA_VERSION:
        errors.append(f"$.schema_version: must equal '{EXPECTED_SCHEMA_VERSION}'")
    if not is_rfc3339_datetime(data.get("generated_at")):
        errors.append("$.generated_at: must be RFC3339 date-time with timezone")

    producer = data.get("producer")
    if not isinstance(producer, dict):
        errors.append("$.producer: must be an object")
    else:
        add_unexpected(errors, "$.producer", producer, PRODUCER_KEYS)
        require_keys(errors, "$.producer", producer, PRODUCER_KEYS)
        for key in PRODUCER_KEYS:
            validate_string_field(errors, "$.producer", producer, key)

    validate_pr(errors, data, metadata)

    candidates = data.get("candidates")
    if not isinstance(candidates, list):
        errors.append("$.candidates: must be an array")
        return errors

    for index, candidate in enumerate(candidates):
        cpath = f"$.candidates[{index}]"
        if not isinstance(candidate, dict):
            errors.append(f"{cpath}: must be an object")
            continue
        add_unexpected(errors, cpath, candidate, CANDIDATE_KEYS)
        require_keys(
            errors,
            cpath,
            candidate,
            {"source_agent", "evidence_state", "location", "severity_raw", "category_raw", "title", "problem", "reason", "suggestion"},
        )
        for key in (
            "candidate_id",
            "source_agent",
            "source_ref",
            "severity_raw",
            "category_raw",
            "title",
            "problem",
            "reason",
            "suggestion",
            "id",
            "fingerprint",
            "verification_hint",
        ):
            validate_string_field(errors, cpath, candidate, key)
        if candidate.get("evidence_state") not in EVIDENCE_STATES:
            errors.append(f"{cpath}.evidence_state: invalid value")
        if "evidence_level" in candidate and candidate["evidence_level"] not in EVIDENCE_LEVELS:
            errors.append(f"{cpath}.evidence_level: invalid value")
        if "blast_radius" in candidate and candidate["blast_radius"] not in BLAST_RADII:
            errors.append(f"{cpath}.blast_radius: invalid value")

        location = candidate.get("location")
        if not isinstance(location, dict):
            errors.append(f"{cpath}.location: must be an object")
        else:
            add_unexpected(errors, f"{cpath}.location", location, LOCATION_KEYS)
            require_keys(errors, f"{cpath}.location", location, {"path", "start_line", "side"})
            validate_string_field(errors, f"{cpath}.location", location, "path")
            validate_string_field(errors, f"{cpath}.location", location, "diff_hunk_ref")
            if not is_positive_int(location.get("start_line")):
                errors.append(f"{cpath}.location.start_line: must be an integer >= 1")
            if "end_line" in location:
                if not is_positive_int(location.get("end_line")):
                    errors.append(f"{cpath}.location.end_line: must be an integer >= 1")
                elif is_positive_int(location.get("start_line")) and location["end_line"] < location["start_line"]:
                    errors.append(f"{cpath}.location.end_line: must be >= start_line")
            if location.get("side") not in {"LEFT", "RIGHT"}:
                errors.append(f"{cpath}.location.side: must be LEFT or RIGHT")

        axes = candidate.get("axes")
        if "axes" in candidate:
            if not isinstance(axes, dict):
                errors.append(f"{cpath}.axes: must be an object")
            else:
                add_unexpected(errors, f"{cpath}.axes", axes, AXES_KEYS)
                for key, value in axes.items():
                    if value not in AXIS_VALUES:
                        errors.append(f"{cpath}.axes.{key}: invalid value")

        posting = candidate.get("posting")
        if "posting" in candidate:
            if not isinstance(posting, dict):
                errors.append(f"{cpath}.posting: must be an object")
            else:
                add_unexpected(errors, f"{cpath}.posting", posting, POSTING_KEYS)
                if "post_policy" in posting and posting["post_policy"] not in POST_POLICIES:
                    errors.append(f"{cpath}.posting.post_policy: invalid value")
                if "explanation_postable" in posting and not isinstance(posting["explanation_postable"], bool):
                    errors.append(f"{cpath}.posting.explanation_postable: must be boolean")
                if "not_postable_reason" in posting and posting["not_postable_reason"] not in NOT_POSTABLE_REASONS:
                    errors.append(f"{cpath}.posting.not_postable_reason: invalid value")
                if "audience" in posting and posting["audience"] not in AUDIENCES:
                    errors.append(f"{cpath}.posting.audience: invalid value")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate pr-codex findings.candidates.json")
    parser.add_argument("--schema", required=True, type=Path, help="schemas/findings.candidates.v1.json path")
    parser.add_argument("--data", required=True, type=Path, help="findings.candidates.json path")
    parser.add_argument("--metadata", type=Path, help="metadata.json path for PR posting target consistency checks")
    args = parser.parse_args()

    try:
        schema = load_json(args.schema)
        data = load_json(args.data)
        metadata = load_json(args.metadata) if args.metadata else None
    except ValueError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1

    schema_errors = validate_schema_file(schema)
    if schema_errors:
        print(f"{args.schema}: invalid candidates schema file", file=sys.stderr)
        for error in schema_errors:
            print(f"- {error}", file=sys.stderr)
        return 2

    errors = validate_candidates(data, metadata)
    if errors:
        print("INVALID candidates artifact", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print("VALID candidates artifact")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
