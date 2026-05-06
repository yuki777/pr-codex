#!/usr/bin/env python3
"""Validate pr-codex findings.verified.json artifacts.

This is intentionally stdlib-only so review/send gates do not depend on npm,
network access, or user-global caches. It validates the runtime contract encoded
by schemas/findings.v1.json plus cross-field rules JSON Schema Draft 2020-12
cannot express portably (id == fingerprint, recomputed fingerprint, end_line >=
start_line, metadata PR context consistency, and practical format checks).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
RFC3339_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")
FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
REPOSITORY_RE = re.compile(r"^[^/\s]+/[^/\s]+$")
BACKTICK_SYMBOL_RE = re.compile(r"`([^`]+)`")

TOP_LEVEL_KEYS = {"schema_version", "producer", "pr", "generated_at", "findings"}
PRODUCER_KEYS = {"name", "version", "run_id"}
PR_KEYS = {"repository", "number", "base_sha", "head_sha", "merge_commit_sha"}
LOCATION_KEYS = {"path", "start_line", "end_line", "side", "diff_hunk_ref"}
AXES_KEYS = {"real", "triggerable", "impactful", "general"}
POSTING_KEYS = {"post_policy", "explanation_postable", "not_postable_reason", "audience"}
FINDING_KEYS = {
    "id",
    "fingerprint",
    "source_agents",
    "merged_from",
    "location",
    "severity",
    "category",
    "category_label",
    "title",
    "problem",
    "reason",
    "suggestion",
    "evidence_level",
    "axes",
    "posting",
    "severity_disputed",
    "severity_by_source",
    "merger_rule_applied",
    "verifier_required",
    "evidence",
    "php",
}
EVIDENCE_KEYS = {"type", "tool", "command", "diagnostic_code", "path", "line", "url", "note"}
PHP_KEYS = {"symbol", "composer_package", "language_version"}


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - report JSON/path failures uniformly
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


def is_rfc3339_datetime(value: str) -> bool:
    if not isinstance(value, str) or not value or not is_safe_json_string(value) or not RFC3339_RE.match(value):
        return False
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return False
    return parsed.tzinfo is not None


def is_uri(value: str) -> bool:
    if not isinstance(value, str) or not value or not is_safe_json_string(value):
        return False
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return bool(parsed.scheme and (parsed.netloc or parsed.scheme in {"urn", "file"}))


def normalize_title(title: str) -> str:
    normalized = unicodedata.normalize("NFKC", title).lower()
    normalized = " ".join(normalized.split()).strip()
    while normalized and unicodedata.category(normalized[-1]).startswith("P"):
        normalized = normalized[:-1].rstrip()
    return normalized


def primary_symbol(title: str) -> str:
    match = BACKTICK_SYMBOL_RE.search(title)
    return match.group(1).strip() if match else ""


def compute_fingerprint(finding: dict[str, Any]) -> str:
    raw_location = finding.get("location")
    location = raw_location if isinstance(raw_location, dict) else {}

    raw_path = location.get("path")
    path = raw_path if isinstance(raw_path, str) else ""

    raw_category = finding.get("category")
    category = raw_category if isinstance(raw_category, str) else ""

    raw_title = finding.get("title")
    title = raw_title if isinstance(raw_title, str) else ""

    normalized_title = normalize_title(title)
    symbol = primary_symbol(title)
    material = "\x1f".join([path, category, normalized_title, symbol])
    return hashlib.sha256(material.encode("utf-8", errors="strict")).hexdigest()


def enum_from_schema(schema: dict[str, Any], name: str, fallback: set[str]) -> set[str]:
    if not isinstance(schema, dict):
        return fallback
    defs = schema.get("$defs")
    if not isinstance(defs, dict):
        return fallback
    definition = defs.get(name)
    if not isinstance(definition, dict):
        return fallback
    values = definition.get("enum")
    return set(values) if isinstance(values, list) and all(isinstance(v, str) for v in values) else fallback


def add_unexpected(errors: list[str], path: str, obj: Any, allowed: set[str]) -> None:
    if not isinstance(obj, dict):
        return
    extra = sorted(set(obj) - allowed)
    if extra:
        errors.append(f"{path}: unexpected properties: {', '.join(extra)}")


def require_keys(errors: list[str], path: str, obj: dict[str, Any], required: set[str]) -> None:
    missing = sorted(required - set(obj))
    if missing:
        errors.append(f"{path}: missing required properties: {', '.join(missing)}")


def validate_string_field(errors: list[str], path: str, obj: dict[str, Any], key: str) -> None:
    if key in obj and not non_empty_string(obj[key]):
        errors.append(f"{path}.{key}: must be a non-empty UTF-8 string without surrogate/control characters")


def validate_enum_value(errors: list[str], path: str, value: Any, allowed: set[str], message: str = "invalid value") -> bool:
    if not isinstance(value, str) or value not in allowed:
        errors.append(f"{path}: {message}")
        return False
    return True


def validate_unique_finding_identity(
    errors: list[str],
    fpath: str,
    index: int,
    identifier: Any,
    fingerprint: Any,
    seen_ids: dict[str, int],
    seen_fingerprints: dict[str, int],
) -> None:
    prior_indexes: set[int] = set()
    if isinstance(identifier, str) and FINGERPRINT_RE.match(identifier):
        prior_id = seen_ids.get(identifier)
        if prior_id is not None:
            prior_indexes.add(prior_id)
        else:
            seen_ids[identifier] = index
    if isinstance(fingerprint, str) and FINGERPRINT_RE.match(fingerprint):
        prior_fingerprint = seen_fingerprints.get(fingerprint)
        if prior_fingerprint is not None:
            prior_indexes.add(prior_fingerprint)
        else:
            seen_fingerprints[fingerprint] = index
    if prior_indexes:
        matched = ", ".join(f"$.findings[{prior}]" for prior in sorted(prior_indexes))
        errors.append(f"{fpath}: duplicate id/fingerprint (matches {matched})")


def validate_m1_posting_contract(
    errors: list[str],
    fpath: str,
    finding: dict[str, Any],
    location: Any,
    posting: dict[str, Any],
    valid_severity: set[str],
    valid_evidence_level: set[str],
    valid_post_policy: set[str],
) -> None:
    severity_value = finding.get("severity")
    evidence_level_value = finding.get("evidence_level")
    post_policy_value = posting.get("post_policy")

    severity_is_valid = isinstance(severity_value, str) and severity_value in valid_severity
    evidence_level_is_valid = isinstance(evidence_level_value, str) and evidence_level_value in valid_evidence_level
    post_policy_is_valid = isinstance(post_policy_value, str) and post_policy_value in valid_post_policy

    if severity_is_valid and evidence_level_is_valid:
        if severity_value == "must_fix" and evidence_level_value != "verified":
            errors.append(f"{fpath}.evidence_level: must_fix findings must use evidence_level=verified")
        if severity_value == "should_fix" and evidence_level_value == "suspicion":
            errors.append(f"{fpath}.evidence_level: should_fix findings require evidence_level=corroborated or higher")

    if not (severity_is_valid and post_policy_is_valid):
        return

    if severity_value == "must_fix":
        if not isinstance(location, dict) or location.get("side") != "RIGHT":
            errors.append(f"{fpath}.location.side: must_fix findings must target location.side=RIGHT")
        if post_policy_value != "inline":
            errors.append(f"{fpath}.posting.post_policy: must_fix findings must use post_policy=inline")
        if posting.get("explanation_postable") is not True:
            errors.append(f"{fpath}.posting.explanation_postable: must_fix findings must set explanation_postable=true")
    elif post_policy_value == "inline":
        errors.append(f"{fpath}.posting.post_policy: only must_fix findings may use post_policy=inline")


def validate_must_fix_four_axes_gate(
    errors: list[str],
    fpath: str,
    finding: dict[str, Any],
    axes: Any,
    valid_axis_values: set[str],
    valid_evidence_levels: set[str],
) -> None:
    """Enforce the F2 REAL/TRIGGERABLE/IMPACTFUL/GENERAL gate for Must Fix findings."""
    if finding.get("severity") != "must_fix" or not isinstance(axes, dict):
        return

    axis_values = {key: axes.get(key) for key in AXES_KEYS}
    evidence_level = finding.get("evidence_level")
    if not all(isinstance(value, str) and value in valid_axis_values for value in axis_values.values()):
        return
    if not isinstance(evidence_level, str) or evidence_level not in valid_evidence_levels:
        return

    if evidence_level == "suspicion":
        errors.append(f"{fpath}.evidence_level: must_fix findings must not use evidence_level=suspicion")

    passes_gate = (
        axis_values["real"] == "yes"
        and axis_values["triggerable"] == "yes"
        and axis_values["impactful"] == "yes"
        and (axis_values["general"] == "yes" or evidence_level in {"impact_explained", "verified"})
    )
    if not passes_gate:
        errors.append(
            f"{fpath}.severity: must_fix requires axes={{real,triggerable,impactful}}=yes and (general=yes or evidence_level in {{impact_explained, verified}})"
        )


def validate_pr_metadata_context(errors: list[str], data: dict[str, Any], metadata: Any) -> None:
    if metadata is None:
        return
    if not isinstance(metadata, dict):
        errors.append("$metadata: must be an object")
        return

    required_metadata = {
        "repository_full_name": str,
        "org": str,
        "repository": str,
        "pr_number": int,
        "head_sha": str,
        "base_sha": str,
    }
    for key, expected_type in required_metadata.items():
        value = metadata.get(key)
        if expected_type is int:
            if not is_positive_int(value):
                errors.append(f"$metadata.{key}: must be an integer >= 1")
        elif not non_empty_string(value):
            errors.append(f"$metadata.{key}: must be a non-empty UTF-8 string without surrogate/control characters")

    repository_full_name = metadata.get("repository_full_name")
    org = metadata.get("org")
    repository = metadata.get("repository")
    if isinstance(repository_full_name, str) and not REPOSITORY_RE.match(repository_full_name):
        errors.append("$metadata.repository_full_name: must be owner/repo")
    if isinstance(repository_full_name, str) and isinstance(org, str) and isinstance(repository, str):
        posting_repository = f"{org}/{repository}"
        if repository_full_name != posting_repository:
            errors.append("$.pr.repository: metadata.repository_full_name must equal metadata org/repository posting target")

    pr = data.get("pr")
    if not isinstance(pr, dict):
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


def validate_artifact(schema: dict[str, Any], data: Any, metadata: Any | None = None) -> list[str]:
    severity = enum_from_schema(schema, "severity", {"must_fix", "should_fix", "nit", "note"})
    axis_value = enum_from_schema(schema, "axis_value", {"yes", "no", "unknown"})
    evidence_level = enum_from_schema(
        schema,
        "evidence_level",
        {"suspicion", "corroborated", "trigger_path_identified", "impact_explained", "verified"},
    )
    post_policy = enum_from_schema(schema, "post_policy", {"inline", "body_summary", "local_only", "suppress"})
    not_postable_reason = enum_from_schema(
        schema,
        "not_postable_reason",
        {"depends_on_pr_external_context", "security_detail", "too_long", "low_evidence_suspicion", "private_dependency", "other_explained"},
    )
    audience = enum_from_schema(schema, "audience", {"human_reviewer", "eval_harness", "future_memory"})
    merger_rule = enum_from_schema(schema, "merger_rule_applied", {"none", "conservative_min_until_verifier_available", "verifier_decided"})
    evidence_type = enum_from_schema(schema, "evidence_type", {"manual_review", "static_analysis", "test", "ci_log", "reference"})
    category = enum_from_schema(
        schema,
        "category",
        {"bug", "security", "performance", "tests", "design", "code_quality", "consistency", "runtime_error"},
    )

    errors: list[str] = []
    if not isinstance(data, dict):
        return ["$: must be an object"]

    add_unexpected(errors, "$", data, TOP_LEVEL_KEYS)
    require_keys(errors, "$", data, TOP_LEVEL_KEYS)

    if data.get("schema_version") != "findings.v1":
        errors.append("$.schema_version: must equal 'findings.v1'")

    generated_at = data.get("generated_at")
    if not isinstance(generated_at, str) or not is_rfc3339_datetime(generated_at):
        errors.append("$.generated_at: must be RFC3339 date-time with timezone")

    producer = data.get("producer")
    if not isinstance(producer, dict):
        errors.append("$.producer: must be an object")
    else:
        add_unexpected(errors, "$.producer", producer, PRODUCER_KEYS)
        require_keys(errors, "$.producer", producer, PRODUCER_KEYS)
        for key in PRODUCER_KEYS:
            validate_string_field(errors, "$.producer", producer, key)

    pr = data.get("pr")
    if not isinstance(pr, dict):
        errors.append("$.pr: must be an object")
    else:
        add_unexpected(errors, "$.pr", pr, PR_KEYS)
        require_keys(errors, "$.pr", pr, {"repository", "number", "base_sha", "head_sha"})
        repository = pr.get("repository")
        if not isinstance(repository, str) or not is_safe_json_string(repository) or not REPOSITORY_RE.match(repository):
            errors.append("$.pr.repository: must be owner/repo")
        if not is_positive_int(pr.get("number")):
            errors.append("$.pr.number: must be an integer >= 1")
        for key in ("base_sha", "head_sha"):
            value = pr.get(key)
            if not isinstance(value, str) or not SHA_RE.match(value):
                errors.append(f"$.pr.{key}: must be 7-64 hex characters")
        merge_commit_sha = pr.get("merge_commit_sha")
        if merge_commit_sha is not None and (not isinstance(merge_commit_sha, str) or not SHA_RE.match(merge_commit_sha)):
            errors.append("$.pr.merge_commit_sha: must be null or 7-64 hex characters")

    validate_pr_metadata_context(errors, data, metadata)

    findings = data.get("findings")
    if not isinstance(findings, list):
        errors.append("$.findings: must be an array")
        return errors

    seen_ids: dict[str, int] = {}
    seen_fingerprints: dict[str, int] = {}

    for index, finding in enumerate(findings):
        fpath = f"$.findings[{index}]"
        if not isinstance(finding, dict):
            errors.append(f"{fpath}: must be an object")
            continue
        add_unexpected(errors, fpath, finding, FINDING_KEYS)
        require_keys(
            errors,
            fpath,
            finding,
            {
                "id",
                "fingerprint",
                "source_agents",
                "merged_from",
                "location",
                "severity",
                "category",
                "title",
                "problem",
                "reason",
                "suggestion",
                "evidence_level",
                "axes",
                "posting",
            },
        )

        fingerprint = finding.get("fingerprint")
        identifier = finding.get("id")
        if not isinstance(fingerprint, str) or not FINGERPRINT_RE.match(fingerprint):
            errors.append(f"{fpath}.fingerprint: must be 64 lowercase hex characters")
        if not isinstance(identifier, str) or not FINGERPRINT_RE.match(identifier):
            errors.append(f"{fpath}.id: must be 64 lowercase hex characters")
        if identifier != fingerprint:
            errors.append(f"{fpath}: id must equal fingerprint")
        validate_unique_finding_identity(errors, fpath, index, identifier, fingerprint, seen_ids, seen_fingerprints)
        if isinstance(fingerprint, str) and FINGERPRINT_RE.match(fingerprint):
            try:
                expected = compute_fingerprint(finding)
            except UnicodeEncodeError as exc:
                errors.append(f"{fpath}.fingerprint: cannot compute canonical fingerprint from non-UTF-8-safe inputs: {exc}")
            else:
                if fingerprint != expected:
                    errors.append(f"{fpath}.fingerprint: expected {expected} from canonical path/category/title/primary_symbol inputs")

        source_agents = finding.get("source_agents")
        if not isinstance(source_agents, list) or not source_agents or any(not non_empty_string(v) for v in source_agents):
            errors.append(f"{fpath}.source_agents: must be a non-empty array of non-empty strings")
        elif len(source_agents) != len(set(source_agents)):
            errors.append(f"{fpath}.source_agents: must be unique")

        merged_from = finding.get("merged_from")
        if not isinstance(merged_from, list) or not merged_from or any(not non_empty_string(v) for v in merged_from):
            errors.append(f"{fpath}.merged_from: must be a non-empty array of non-empty strings")

        validate_enum_value(errors, f"{fpath}.severity", finding.get("severity"), severity)
        validate_enum_value(errors, f"{fpath}.category", finding.get("category"), category)
        for key in ("title", "problem", "reason", "suggestion", "category_label"):
            validate_string_field(errors, fpath, finding, key)
        validate_enum_value(errors, f"{fpath}.evidence_level", finding.get("evidence_level"), evidence_level)

        location = finding.get("location")
        if not isinstance(location, dict):
            errors.append(f"{fpath}.location: must be an object")
        else:
            add_unexpected(errors, f"{fpath}.location", location, LOCATION_KEYS)
            require_keys(errors, f"{fpath}.location", location, {"path", "start_line", "side"})
            validate_string_field(errors, f"{fpath}.location", location, "path")
            validate_string_field(errors, f"{fpath}.location", location, "diff_hunk_ref")
            if not is_positive_int(location.get("start_line")):
                errors.append(f"{fpath}.location.start_line: must be an integer >= 1")
            if "end_line" in location:
                if not is_positive_int(location.get("end_line")):
                    errors.append(f"{fpath}.location.end_line: must be an integer >= 1")
                elif is_positive_int(location.get("start_line")) and location["end_line"] < location["start_line"]:
                    errors.append(f"{fpath}.location.end_line: must be >= start_line")
            side = location.get("side")
            if not isinstance(side, str) or side not in {"LEFT", "RIGHT"}:
                errors.append(f"{fpath}.location.side: must be LEFT or RIGHT")

        axes = finding.get("axes")
        if not isinstance(axes, dict):
            errors.append(f"{fpath}.axes: must be an object")
        else:
            add_unexpected(errors, f"{fpath}.axes", axes, AXES_KEYS)
            require_keys(errors, f"{fpath}.axes", axes, AXES_KEYS)
            for key in AXES_KEYS:
                validate_enum_value(errors, f"{fpath}.axes.{key}", axes.get(key), axis_value)
            validate_must_fix_four_axes_gate(errors, fpath, finding, axes, axis_value, evidence_level)

        posting = finding.get("posting")
        if not isinstance(posting, dict):
            errors.append(f"{fpath}.posting: must be an object")
        else:
            add_unexpected(errors, f"{fpath}.posting", posting, POSTING_KEYS)
            require_keys(errors, f"{fpath}.posting", posting, {"post_policy", "explanation_postable"})
            validate_enum_value(errors, f"{fpath}.posting.post_policy", posting.get("post_policy"), post_policy)
            if not isinstance(posting.get("explanation_postable"), bool):
                errors.append(f"{fpath}.posting.explanation_postable: must be boolean")
            if "not_postable_reason" in posting:
                validate_enum_value(errors, f"{fpath}.posting.not_postable_reason", posting.get("not_postable_reason"), not_postable_reason)
                if posting.get("explanation_postable") is not False:
                    errors.append(f"{fpath}.posting.not_postable_reason: only allowed when explanation_postable=false")
                if posting.get("post_policy") == "inline":
                    errors.append(f"{fpath}.posting.not_postable_reason: must not be present when post_policy=inline")
            if "audience" in posting:
                validate_enum_value(errors, f"{fpath}.posting.audience", posting.get("audience"), audience)
            if posting.get("explanation_postable") is False and "not_postable_reason" not in posting:
                errors.append(f"{fpath}.posting.not_postable_reason: required when explanation_postable=false")
            if finding.get("evidence_level") == "suspicion" and posting.get("explanation_postable") is not False:
                errors.append(f"{fpath}.posting.explanation_postable: must be false when evidence_level=suspicion")
            if posting.get("post_policy") == "local_only" and "audience" not in posting:
                errors.append(f"{fpath}.posting.audience: required when post_policy=local_only")
            validate_m1_posting_contract(errors, fpath, finding, location, posting, severity, evidence_level, post_policy)

        if "severity_disputed" in finding and not isinstance(finding["severity_disputed"], bool):
            errors.append(f"{fpath}.severity_disputed: must be boolean")
        if finding.get("severity_disputed") is True:
            for key in ("severity_by_source", "merger_rule_applied", "verifier_required"):
                if key not in finding:
                    errors.append(f"{fpath}.{key}: required when severity_disputed=true")
        if "severity_by_source" in finding:
            severity_by_source = finding["severity_by_source"]
            if not isinstance(severity_by_source, dict) or not severity_by_source:
                errors.append(f"{fpath}.severity_by_source: must be a non-empty object")
            else:
                for source, value in severity_by_source.items():
                    if not non_empty_string(source):
                        errors.append(f"{fpath}.severity_by_source: keys must be non-empty strings")
                    else:
                        validate_enum_value(errors, f"{fpath}.severity_by_source.{source}", value, severity, "invalid severity")
        if "merger_rule_applied" in finding:
            validate_enum_value(errors, f"{fpath}.merger_rule_applied", finding.get("merger_rule_applied"), merger_rule)
        if "verifier_required" in finding and not isinstance(finding.get("verifier_required"), bool):
            errors.append(f"{fpath}.verifier_required: must be boolean")

        if "evidence" in finding:
            evidence = finding["evidence"]
            if not isinstance(evidence, list):
                errors.append(f"{fpath}.evidence: must be an array")
            else:
                for evidence_index, item in enumerate(evidence):
                    epath = f"{fpath}.evidence[{evidence_index}]"
                    if not isinstance(item, dict):
                        errors.append(f"{epath}: must be an object")
                        continue
                    add_unexpected(errors, epath, item, EVIDENCE_KEYS)
                    require_keys(errors, epath, item, {"type"})
                    validate_enum_value(errors, f"{epath}.type", item.get("type"), evidence_type)
                    for key in ("tool", "command", "diagnostic_code", "path", "note"):
                        validate_string_field(errors, epath, item, key)
                    if "line" in item and not is_positive_int(item.get("line")):
                        errors.append(f"{epath}.line: must be an integer >= 1")
                    if "url" in item and not is_uri(item.get("url")):
                        errors.append(f"{epath}.url: must be a URI")

        if "php" in finding:
            php = finding["php"]
            if not isinstance(php, dict):
                errors.append(f"{fpath}.php: must be an object")
            else:
                add_unexpected(errors, f"{fpath}.php", php, PHP_KEYS)
                for key in PHP_KEYS:
                    validate_string_field(errors, f"{fpath}.php", php, key)

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate pr-codex findings.verified.json")
    parser.add_argument("--schema", required=True, type=Path, help="schemas/findings.v1.json path")
    parser.add_argument("--data", required=True, type=Path, help="findings.verified.json path")
    parser.add_argument("--metadata", type=Path, help="metadata.json path for PR posting target consistency checks")
    args = parser.parse_args()

    try:
        schema = load_json(args.schema)
        data = load_json(args.data)
        metadata = load_json(args.metadata) if args.metadata else None
    except ValueError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1

    errors = validate_artifact(schema, data, metadata)
    if errors:
        print("INVALID findings artifact", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print("VALID findings artifact")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
