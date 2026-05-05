#!/usr/bin/env python3
"""Validate pr-codex findings.verified.json artifacts.

This is intentionally stdlib-only so review/send gates do not depend on npm,
network access, or user-global caches. It validates the runtime contract encoded
by schemas/findings.v1.json plus cross-field rules JSON Schema Draft 2020-12
cannot express portably (id == fingerprint, recomputed fingerprint, end_line >=
start_line, and practical format checks).
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


def non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and len(value) >= 1


def is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def is_rfc3339_datetime(value: str) -> bool:
    if not isinstance(value, str) or not value or not RFC3339_RE.match(value):
        return False
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return False
    return parsed.tzinfo is not None


def is_uri(value: str) -> bool:
    if not isinstance(value, str) or not value:
        return False
    parsed = urlparse(value)
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
    location = finding.get("location", {})
    path = location.get("path", "")
    category = finding.get("category", "")
    title = finding.get("title", "")
    normalized_title = normalize_title(title) if isinstance(title, str) else ""
    symbol = primary_symbol(title) if isinstance(title, str) else ""
    material = "\x1f".join([path, category, normalized_title, symbol])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def enum_from_schema(schema: dict[str, Any], name: str, fallback: set[str]) -> set[str]:
    values = schema.get("$defs", {}).get(name, {}).get("enum")
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
        errors.append(f"{path}.{key}: must be a non-empty string")


def validate_artifact(schema: dict[str, Any], data: Any) -> list[str]:
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
        if not isinstance(repository, str) or not REPOSITORY_RE.match(repository):
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

    findings = data.get("findings")
    if not isinstance(findings, list):
        errors.append("$.findings: must be an array")
        return errors

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
        if isinstance(fingerprint, str) and FINGERPRINT_RE.match(fingerprint):
            expected = compute_fingerprint(finding)
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

        if finding.get("severity") not in severity:
            errors.append(f"{fpath}.severity: invalid value")
        if finding.get("category") not in category:
            errors.append(f"{fpath}.category: invalid value")
        for key in ("title", "problem", "reason", "suggestion", "category_label"):
            validate_string_field(errors, fpath, finding, key)
        if finding.get("evidence_level") not in evidence_level:
            errors.append(f"{fpath}.evidence_level: invalid value")

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
            if location.get("side") not in {"LEFT", "RIGHT"}:
                errors.append(f"{fpath}.location.side: must be LEFT or RIGHT")

        axes = finding.get("axes")
        if not isinstance(axes, dict):
            errors.append(f"{fpath}.axes: must be an object")
        else:
            add_unexpected(errors, f"{fpath}.axes", axes, AXES_KEYS)
            require_keys(errors, f"{fpath}.axes", axes, AXES_KEYS)
            for key in AXES_KEYS:
                if axes.get(key) not in axis_value:
                    errors.append(f"{fpath}.axes.{key}: invalid value")

        posting = finding.get("posting")
        if not isinstance(posting, dict):
            errors.append(f"{fpath}.posting: must be an object")
        else:
            add_unexpected(errors, f"{fpath}.posting", posting, POSTING_KEYS)
            require_keys(errors, f"{fpath}.posting", posting, {"post_policy", "explanation_postable"})
            if posting.get("post_policy") not in post_policy:
                errors.append(f"{fpath}.posting.post_policy: invalid value")
            if not isinstance(posting.get("explanation_postable"), bool):
                errors.append(f"{fpath}.posting.explanation_postable: must be boolean")
            if "not_postable_reason" in posting and posting.get("not_postable_reason") not in not_postable_reason:
                errors.append(f"{fpath}.posting.not_postable_reason: invalid value")
            if "audience" in posting and posting.get("audience") not in audience:
                errors.append(f"{fpath}.posting.audience: invalid value")
            if posting.get("explanation_postable") is False and "not_postable_reason" not in posting:
                errors.append(f"{fpath}.posting.not_postable_reason: required when explanation_postable=false")
            if finding.get("evidence_level") == "suspicion" and posting.get("explanation_postable") is not False:
                errors.append(f"{fpath}.posting.explanation_postable: must be false when evidence_level=suspicion")
            if posting.get("post_policy") == "local_only" and "audience" not in posting:
                errors.append(f"{fpath}.posting.audience: required when post_policy=local_only")

        if finding.get("severity_disputed") is not None and not isinstance(finding.get("severity_disputed"), bool):
            errors.append(f"{fpath}.severity_disputed: must be boolean")
        if finding.get("severity_disputed") is True:
            for key in ("severity_by_source", "merger_rule_applied", "verifier_required"):
                if key not in finding:
                    errors.append(f"{fpath}.{key}: required when severity_disputed=true")
        severity_by_source = finding.get("severity_by_source")
        if severity_by_source is not None:
            if not isinstance(severity_by_source, dict) or not severity_by_source:
                errors.append(f"{fpath}.severity_by_source: must be a non-empty object")
            else:
                for source, value in severity_by_source.items():
                    if not non_empty_string(source) or value not in severity:
                        errors.append(f"{fpath}.severity_by_source.{source}: invalid severity")
        if "merger_rule_applied" in finding and finding.get("merger_rule_applied") not in merger_rule:
            errors.append(f"{fpath}.merger_rule_applied: invalid value")
        if "verifier_required" in finding and not isinstance(finding.get("verifier_required"), bool):
            errors.append(f"{fpath}.verifier_required: must be boolean")

        evidence = finding.get("evidence")
        if evidence is not None:
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
                    if item.get("type") not in evidence_type:
                        errors.append(f"{epath}.type: invalid value")
                    for key in ("tool", "command", "diagnostic_code", "path", "note"):
                        validate_string_field(errors, epath, item, key)
                    if "line" in item and not is_positive_int(item.get("line")):
                        errors.append(f"{epath}.line: must be an integer >= 1")
                    if "url" in item and not is_uri(item.get("url")):
                        errors.append(f"{epath}.url: must be a URI")

        php = finding.get("php")
        if php is not None:
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
    args = parser.parse_args()

    try:
        schema = load_json(args.schema)
        data = load_json(args.data)
    except ValueError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1

    errors = validate_artifact(schema, data)
    if errors:
        print("INVALID findings artifact", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print("VALID findings artifact")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
