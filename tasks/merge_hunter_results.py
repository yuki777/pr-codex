#!/usr/bin/env python3
"""Validate and merge structured Claude and Codex hunter results."""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EXPECTED_HUNTER_SCHEMA_ID = (
    "https://raw.githubusercontent.com/yuki777/pr-codex/main/schemas/hunter-result.v1.json"
)
EXPECTED_HUNTER_SCHEMA_VERSION = "hunter-result.v1"
OUTPUT_SCHEMA_VERSION = "findings.candidates.v1"

TOP_LEVEL_KEYS = {"schema_version", "status", "candidates", "coverage"}
COVERAGE_KEYS = {"high_risk_paths_checked", "checks_run", "limitations"}
CANDIDATE_KEYS = {
    "title",
    "severity_suggestion",
    "category_suggestion",
    "path",
    "start_line",
    "end_line",
    "side",
    "problem",
    "reason",
    "suggestion",
}
METADATA_REQUIRED_KEYS = {
    "org",
    "repository",
    "repository_full_name",
    "pr_number",
    "head_sha",
    "base_sha",
}
HUNTER_STATUSES = {"findings", "clean", "diff_unavailable"}
SEVERITY_SUGGESTIONS = {"must_fix", "should_fix", "nit", "note"}
SIDES = {"LEFT", "RIGHT"}


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


def add_unexpected(errors: list[str], path: str, obj: Any, allowed: set[str]) -> None:
    if isinstance(obj, dict):
        extra = sorted(set(obj) - allowed)
        if extra:
            errors.append(f"{path}: unexpected properties: {', '.join(extra)}")


def require_keys(errors: list[str], path: str, obj: dict[str, Any], required: set[str]) -> None:
    missing = sorted(required - set(obj))
    if missing:
        errors.append(f"{path}: missing required properties: {', '.join(missing)}")


def validate_string_field(errors: list[str], path: str, obj: dict[str, Any], key: str) -> None:
    if key in obj and not non_empty_string(obj[key]):
        errors.append(
            f"{path}.{key}: must be a non-empty UTF-8 string without surrogate/control characters"
        )


def validate_schema_file(schema: Any) -> list[str]:
    """Ensure --schema is the structured hunter result schema."""

    if not isinstance(schema, dict):
        return ["$schema: must be an object"]

    errors: list[str] = []
    if schema.get("$id") != EXPECTED_HUNTER_SCHEMA_ID:
        errors.append(f"$schema.$id: must equal '{EXPECTED_HUNTER_SCHEMA_ID}'")

    properties = schema.get("properties")
    if not isinstance(properties, dict):
        errors.append("$schema.properties: must be an object")
        return errors

    schema_version = properties.get("schema_version")
    if (
        not isinstance(schema_version, dict)
        or schema_version.get("enum") != [EXPECTED_HUNTER_SCHEMA_VERSION]
    ):
        errors.append(
            "$schema.properties.schema_version.enum: "
            f"must equal ['{EXPECTED_HUNTER_SCHEMA_VERSION}']"
        )

    candidates = properties.get("candidates")
    if not isinstance(candidates, dict) or candidates.get("type") != "array":
        errors.append("$schema.properties.candidates.type: must equal 'array'")

    return errors


def validate_string_array(errors: list[str], path: str, value: Any) -> None:
    if not isinstance(value, list):
        errors.append(f"{path}: must be an array")
        return
    for index, item in enumerate(value):
        if not non_empty_string(item):
            errors.append(
                f"{path}[{index}]: must be a non-empty UTF-8 string without "
                "surrogate/control characters"
            )


def validate_candidate(errors: list[str], candidate: Any, index: int) -> None:
    path = f"$.candidates[{index}]"
    if not isinstance(candidate, dict):
        errors.append(f"{path}: must be an object")
        return

    add_unexpected(errors, path, candidate, CANDIDATE_KEYS)
    require_keys(errors, path, candidate, CANDIDATE_KEYS)

    for key in ("title", "category_suggestion", "path", "problem", "reason", "suggestion"):
        validate_string_field(errors, path, candidate, key)

    severity = candidate.get("severity_suggestion")
    if not isinstance(severity, str) or severity not in SEVERITY_SUGGESTIONS:
        errors.append(
            f"{path}.severity_suggestion: must be one of must_fix, should_fix, nit, note"
        )
    side = candidate.get("side")
    if not isinstance(side, str) or side not in SIDES:
        errors.append(f"{path}.side: must be LEFT or RIGHT")

    start_line = candidate.get("start_line")
    if not is_positive_int(start_line):
        errors.append(f"{path}.start_line: must be an integer >= 1")

    end_line = candidate.get("end_line")
    if end_line is not None:
        if not is_positive_int(end_line):
            errors.append(f"{path}.end_line: must be null or an integer >= 1")
        elif is_positive_int(start_line) and end_line < start_line:
            errors.append(f"{path}.end_line: must be >= start_line")


def validate_hunter_result(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    add_unexpected(errors, "$", data, TOP_LEVEL_KEYS)
    require_keys(errors, "$", data, TOP_LEVEL_KEYS)

    if data.get("schema_version") != EXPECTED_HUNTER_SCHEMA_VERSION:
        errors.append(
            f"$.schema_version: must equal '{EXPECTED_HUNTER_SCHEMA_VERSION}'"
        )

    status = data.get("status")
    if not isinstance(status, str) or status not in HUNTER_STATUSES:
        errors.append("$.status: must be one of findings, clean, diff_unavailable")

    candidates = data.get("candidates")
    if not isinstance(candidates, list):
        errors.append("$.candidates: must be an array")
    else:
        if status == "findings" and not candidates:
            errors.append("$.candidates: must contain at least one item when status is 'findings'")
        elif isinstance(status, str) and status in {"clean", "diff_unavailable"} and candidates:
            errors.append(f"$.candidates: must be empty when status is '{status}'")
        for index, candidate in enumerate(candidates):
            validate_candidate(errors, candidate, index)

    coverage = data.get("coverage")
    if not isinstance(coverage, dict):
        errors.append("$.coverage: must be an object")
    else:
        add_unexpected(errors, "$.coverage", coverage, COVERAGE_KEYS)
        require_keys(errors, "$.coverage", coverage, COVERAGE_KEYS)
        for key in ("high_risk_paths_checked", "checks_run", "limitations"):
            if key in coverage:
                validate_string_array(errors, f"$.coverage.{key}", coverage[key])

    return errors


def validate_metadata(metadata: Any) -> list[str]:
    if not isinstance(metadata, dict):
        return ["$metadata: must be an object"]

    errors: list[str] = []
    require_keys(errors, "$metadata", metadata, METADATA_REQUIRED_KEYS)
    for key in ("org", "repository", "repository_full_name", "head_sha", "base_sha"):
        validate_string_field(errors, "$metadata", metadata, key)
    if "pr_number" in metadata and not is_positive_int(metadata["pr_number"]):
        errors.append("$metadata.pr_number: must be an integer >= 1")
    return errors


def merge_candidate(agent: str, candidate: dict[str, Any], index: int) -> dict[str, Any]:
    location = {
        "path": candidate["path"],
        "start_line": candidate["start_line"],
    }
    if candidate["end_line"] is not None:
        location["end_line"] = candidate["end_line"]
    location["side"] = candidate["side"]

    return {
        "candidate_id": f"{agent}-{index + 1:03d}",
        "source_agent": agent,
        "source_ref": f"{agent}-review.json#candidates[{index}]",
        "location": location,
        "severity_raw": candidate["severity_suggestion"],
        "category_raw": candidate["category_suggestion"],
        "title": candidate["title"],
        "problem": candidate["problem"],
        "reason": candidate["reason"],
        "suggestion": candidate["suggestion"],
    }


def build_output(
    claude: dict[str, Any],
    codex: dict[str, Any],
    metadata: dict[str, Any],
    producer_version: str,
) -> dict[str, Any]:
    pr = {
        "repository": metadata["repository_full_name"],
        "number": metadata["pr_number"],
        "base_sha": metadata["base_sha"],
        "head_sha": metadata["head_sha"],
    }
    merge_commit_sha = metadata.get("merge_commit_sha")
    if non_empty_string(merge_commit_sha):
        pr["merge_commit_sha"] = merge_commit_sha

    candidates = [
        merge_candidate(agent, candidate, index)
        for agent, result in (("claude", claude), ("codex", codex))
        for index, candidate in enumerate(result["candidates"])
    ]

    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "producer": {
            "name": "pr-codex",
            "version": producer_version,
            "run_id": (
                f"{metadata['org']}-{metadata['repository']}-{metadata['pr_number']}-"
                f"{metadata['head_sha']}"
            ),
        },
        "pr": pr,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "candidates": candidates,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate and merge structured Claude and Codex hunter results"
    )
    parser.add_argument("--schema", required=True, type=Path, help="hunter-result.v1.json path")
    parser.add_argument("--claude", required=True, type=Path, help="Claude hunter result path")
    parser.add_argument("--codex", required=True, type=Path, help="Codex hunter result path")
    parser.add_argument("--metadata", required=True, type=Path, help="metadata.json path")
    parser.add_argument("--producer-version", required=True, help="pr-codex producer version")
    parser.add_argument("--output", required=True, type=Path, help="merged candidates output path")
    args = parser.parse_args()

    try:
        schema = load_json(args.schema)
    except ValueError as exc:
        print(f"{args.schema}: invalid hunter schema file", file=sys.stderr)
        print(f"- {exc}", file=sys.stderr)
        return 2

    schema_errors = validate_schema_file(schema)
    if schema_errors:
        print(f"{args.schema}: invalid hunter schema file", file=sys.stderr)
        for error in schema_errors:
            print(f"- {error}", file=sys.stderr)
        return 2

    hunter_results: dict[str, dict[str, Any]] = {}
    for agent, path in (("claude", args.claude), ("codex", args.codex)):
        try:
            result = load_json(path)
        except ValueError as exc:
            print(f"INVALID hunter result: {agent}: {exc}", file=sys.stderr)
            return 1
        if not isinstance(result, dict):
            print(
                f"INVALID hunter result: {agent}: top-level value must be an object",
                file=sys.stderr,
            )
            return 1
        hunter_results[agent] = result

    runtime_errors: list[tuple[str, str]] = []
    for agent in ("claude", "codex"):
        runtime_errors.extend(
            (agent, error) for error in validate_hunter_result(hunter_results[agent])
        )
    if runtime_errors:
        for agent, error in runtime_errors:
            print(f"INVALID hunter result: {agent}: {error}", file=sys.stderr)
        return 1

    claude = hunter_results["claude"]
    codex = hunter_results["codex"]
    if "diff_unavailable" in {claude["status"], codex["status"]}:
        print(
            "HUNTER_DIFF_UNAVAILABLE: "
            f"claude={claude['status']} codex={codex['status']}",
            file=sys.stderr,
        )
        return 3

    try:
        metadata = load_json(args.metadata)
    except ValueError as exc:
        print(f"INVALID metadata: {exc}", file=sys.stderr)
        return 1

    metadata_errors = validate_metadata(metadata)
    if metadata_errors:
        print("INVALID metadata", file=sys.stderr)
        for error in metadata_errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    if not non_empty_string(args.producer_version):
        print(
            "INVALID producer version: must be a non-empty UTF-8 string without "
            "surrogate/control characters",
            file=sys.stderr,
        )
        return 1

    output = build_output(claude, codex, metadata, args.producer_version)
    try:
        args.output.write_text(
            json.dumps(output, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001
        print(f"INVALID output: {args.output}: cannot write JSON: {exc}", file=sys.stderr)
        return 1

    claude_count = len(claude["candidates"])
    codex_count = len(codex["candidates"])
    print(
        f"merged {claude_count + codex_count} candidates "
        f"(claude={claude_count} status={claude['status']}, "
        f"codex={codex_count} status={codex['status']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
