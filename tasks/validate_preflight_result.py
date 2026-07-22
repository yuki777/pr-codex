#!/usr/bin/env python3
"""Validate /pr-codex:send preflight-result.json artifacts.

The Step 4.5 verifier writes JSON directly to preflight-result.json using
Codex structured output. This stdlib-only helper validates the runtime
contract encoded by schemas/preflight-result.v1.json plus cross-field rules
JSON Schema cannot express portably:

* all four verifier pipeline stages are present in the stages object;
* top-level verdict is FAIL iff at least one error violation exists;
* each FAIL stage has at least one error violation, and error stages are FAIL;
* auto_fixable_count / requires_human_count match error violations; and
* PASS_WITH_WARNINGS is not accepted as a verdict.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

EXPECTED_SCHEMA_VERSION = "preflight-result.v1"
EXPECTED_SCHEMA_ID = "https://raw.githubusercontent.com/yuki777/pr-codex/main/schemas/preflight-result.v1.json"
STAGES = [
    "schema_validation",
    "range_validation",
    "semantic_preflight",
    "payload_consistency",
]
STAGE_SET = set(STAGES)
STAGE_STATUSES = {"PASS", "FAIL"}
VERDICTS = {"PASS", "FAIL"}
SEVERITIES = {"error", "warning"}
TOP_LEVEL_KEYS = {
    "schema_version",
    "verdict",
    "stages",
    "violations",
    "auto_fixable_count",
    "requires_human_count",
    "generated_at",
}
STAGE_RESULT_KEYS = {"status", "note"}
VIOLATION_KEYS = {
    "stage",
    "rule",
    "finding_id",
    "comment_index",
    "detail",
    "severity",
    "auto_fixable",
    "requires_review_regeneration",
}


RULE_CLASSIFICATION: dict[str, tuple[str, bool, bool]] = {
    "schema_version_mismatch": ("schema_validation", False, True),
    "findings_validator_failed": ("schema_validation", False, True),
    "sarif_schema_invalid": ("schema_validation", False, True),
    "must_fix_count_mismatch": ("schema_validation", False, True),
    "id_fingerprint_mismatch": ("schema_validation", False, True),
    "pr_context_mismatch": ("schema_validation", False, True),
    "path_not_in_files": ("range_validation", True, False),
    "line_out_of_hunk": ("range_validation", True, False),
    "multi_hunk_span": ("range_validation", True, False),
    "severity_misclassification": ("semantic_preflight", True, False),
    "non_must_fix_inline_inclusion": ("semantic_preflight", True, False),
    "axes_gate_violation": ("semantic_preflight", False, True),
    "evidence_level_violation": ("semantic_preflight", False, True),
    "counterargument_succeeded": ("semantic_preflight", False, True),
    "event_mismatch": ("payload_consistency", True, False),
    "summary_body_mismatch": ("payload_consistency", True, False),
    "good_points_body_mismatch": ("payload_consistency", True, False),
    "confirmation_scope_body_mismatch": ("payload_consistency", True, False),
    "must_fix_count_mismatch_findings_vs_md": ("payload_consistency", False, True),
}



def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - report JSON/path failures uniformly
        raise ValueError(f"{path}: cannot read/parse JSON: {exc}") from exc


def validate_schema_file(schema: Any) -> list[str]:
    """Ensure --schema is the expected preflight-result schema."""
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
    if not isinstance(schema_version, dict) or schema_version.get("enum") != [EXPECTED_SCHEMA_VERSION]:
        errors.append(
            f"$schema.properties.schema_version.enum: must equal ['{EXPECTED_SCHEMA_VERSION}']"
        )
    return errors


def is_safe_string(value: Any) -> bool:
    return isinstance(value, str) and all(
        not (ord(char) <= 0x1F or 0x7F <= ord(char) <= 0x9F or 0xD800 <= ord(char) <= 0xDFFF)
        for char in value
    )


def is_non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and len(value) >= 1 and all(
        not (ord(char) <= 0x1F or 0x7F <= ord(char) <= 0x9F or 0xD800 <= ord(char) <= 0xDFFF)
        for char in value
    )


def is_non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def is_rfc3339(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return False
    return parsed.tzinfo is not None


def add_unexpected(errors: list[str], path: str, obj: Any, allowed: set[str]) -> None:
    if isinstance(obj, dict):
        unexpected = sorted(set(obj) - allowed)
        if unexpected:
            errors.append(f"{path}: unexpected properties: {', '.join(unexpected)}")




def validate_stage_results(errors: list[str], data: dict[str, Any]) -> None:
    stages = data.get("stages")
    if not isinstance(stages, dict):
        errors.append("$.stages: must be an object")
        return
    add_unexpected(errors, "$.stages", stages, STAGE_SET)
    missing = [stage for stage in STAGES if stage not in stages]
    if missing:
        errors.append(f"$.stages: missing stages: {', '.join(missing)}")

    for stage_name in STAGES:
        if stage_name not in stages:
            continue
        item = stages[stage_name]
        path = f"$.stages.{stage_name}"
        if not isinstance(item, dict):
            errors.append(f"{path}: must be an object")
            continue
        add_unexpected(errors, path, item, STAGE_RESULT_KEYS)
        missing_properties = [field for field in ("status", "note") if field not in item]
        if missing_properties:
            errors.append(f"{path}: missing required properties: {', '.join(missing_properties)}")
        if "status" in item and item.get("status") not in STAGE_STATUSES:
            errors.append(f"{path}.status: must be PASS or FAIL")
        if "note" in item and not is_safe_string(item["note"]):
            errors.append(f"{path}.note: must be a string without control characters")


def validate_violations(errors: list[str], data: dict[str, Any]) -> None:
    violations = data.get("violations")
    if not isinstance(violations, list):
        errors.append("$.violations: must be an array")
        return
    for index, violation in enumerate(violations):
        path = f"$.violations[{index}]"
        if not isinstance(violation, dict):
            errors.append(f"{path}: must be an object")
            continue
        add_unexpected(errors, path, violation, VIOLATION_KEYS)
        required = VIOLATION_KEYS
        missing = sorted(required - set(violation))
        if missing:
            errors.append(f"{path}: missing required properties: {', '.join(missing)}")
        if violation.get("stage") not in STAGE_SET:
            errors.append(f"{path}.stage: must be one of {', '.join(STAGES)}")
        if violation.get("severity") not in SEVERITIES:
            errors.append(f"{path}.severity: must be error or warning")
        for field in ("rule", "detail"):
            if field in violation and not is_non_empty_string(violation[field]):
                errors.append(f"{path}.{field}: must be a non-empty string")
        if (
            "finding_id" in violation
            and violation["finding_id"] is not None
            and not is_non_empty_string(violation["finding_id"])
        ):
            errors.append(
                f"{path}.finding_id: must be null or a non-empty string without control characters"
            )
        if (
            "comment_index" in violation
            and violation["comment_index"] is not None
            and not is_non_negative_int(violation["comment_index"])
        ):
            errors.append(f"{path}.comment_index: must be null or a non-negative integer")
        for field in ("auto_fixable", "requires_review_regeneration"):
            if field in violation and not isinstance(violation[field], bool):
                errors.append(f"{path}.{field}: must be a boolean")

        rule = violation.get("rule")
        if isinstance(rule, str):
            classification = RULE_CLASSIFICATION.get(rule)
            if classification is None:
                if violation.get("severity") == "error":
                    errors.append(f"{path}.rule: unknown error rule {rule}")
            else:
                expected_stage, expected_auto_fixable, expected_requires_regeneration = classification
                if violation.get("severity") != "error":
                    errors.append(f"{path}.severity: known rule {rule} must use severity=error")
                if violation.get("stage") != expected_stage:
                    errors.append(f"{path}.stage: rule {rule} must use stage={expected_stage}")
                if violation.get("auto_fixable") is not expected_auto_fixable:
                    errors.append(f"{path}.auto_fixable: rule {rule} must use auto_fixable={str(expected_auto_fixable).lower()}")
                if violation.get("requires_review_regeneration") is not expected_requires_regeneration:
                    errors.append(
                        f"{path}.requires_review_regeneration: rule {rule} must use "
                        f"requires_review_regeneration={str(expected_requires_regeneration).lower()}"
                    )


def expected_counts(error_violations: list[dict[str, Any]]) -> tuple[int, int]:
    auto_fixable_count = sum(1 for item in error_violations if item.get("auto_fixable") is True)
    requires_human_count = sum(
        1
        for item in error_violations
        if item.get("auto_fixable") is not True or item.get("requires_review_regeneration") is True
    )
    return auto_fixable_count, requires_human_count


def validate_preflight_result(data: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["$: must be an object"]

    add_unexpected(errors, "$", data, TOP_LEVEL_KEYS)
    required = {
        "schema_version",
        "verdict",
        "stages",
        "violations",
        "auto_fixable_count",
        "requires_human_count",
        "generated_at",
    }
    missing = sorted(required - set(data))
    if missing:
        errors.append(f"$: missing required properties: {', '.join(missing)}")

    if data.get("schema_version") != EXPECTED_SCHEMA_VERSION:
        errors.append(f"$.schema_version: must be {EXPECTED_SCHEMA_VERSION}")
    if data.get("verdict") not in VERDICTS:
        errors.append("$.verdict: must be PASS or FAIL")
    if "generated_at" in data and not is_rfc3339(data["generated_at"]):
        errors.append("$.generated_at: must be an RFC3339 date-time string")
    for field in ("auto_fixable_count", "requires_human_count"):
        if field in data and not is_non_negative_int(data[field]):
            errors.append(f"$.{field}: must be a non-negative integer")

    validate_stage_results(errors, data)
    validate_violations(errors, data)

    violations = data.get("violations")
    stages = data.get("stages")
    if isinstance(violations, list) and all(isinstance(item, dict) for item in violations):
        error_violations = [item for item in violations if item.get("severity") == "error"]
        expected_verdict = "FAIL" if error_violations else "PASS"
        if data.get("verdict") in VERDICTS and data.get("verdict") != expected_verdict:
            errors.append(f"$.verdict: must be {expected_verdict} based on error violations")

        auto_fixable_count, requires_human_count = expected_counts(error_violations)
        if is_non_negative_int(data.get("auto_fixable_count")) and data.get("auto_fixable_count") != auto_fixable_count:
            errors.append("$.auto_fixable_count: must equal error violations where auto_fixable=true")
        if is_non_negative_int(data.get("requires_human_count")) and data.get("requires_human_count") != requires_human_count:
            errors.append(
                "$.requires_human_count: must equal error violations requiring regeneration or not auto-fixable"
            )

        failing_stages = {item.get("stage") for item in error_violations if item.get("stage") in STAGE_SET}
        if isinstance(stages, dict):
            for stage in STAGES:
                item = stages.get(stage)
                if not isinstance(item, dict):
                    continue
                status = item.get("status")
                if stage in failing_stages and status == "PASS":
                    errors.append(f"$.stages.{stage}.status: must be FAIL because the stage has error violations")
                if stage not in failing_stages and status == "FAIL":
                    errors.append(f"$.stages.{stage}.status: FAIL requires at least one error violation in that stage")
    return errors


def emit_markdown(data: dict[str, Any]) -> str:
    lines = ["# preflight-result", ""]
    lines.append(f"schema_version: {data.get('schema_version')}")
    lines.append(f"verdict: {data.get('verdict')}")
    lines.append(f"auto_fixable_count: {data.get('auto_fixable_count')}")
    lines.append(f"requires_human_count: {data.get('requires_human_count')}")
    lines.append("")
    lines.append("## Stage results")
    stages = data.get("stages", {})
    if isinstance(stages, dict):
        for stage in STAGES:
            item = stages.get(stage, {})
            note = item.get("note") if isinstance(item, dict) else None
            suffix = f" — {note}" if note else ""
            status = item.get("status") if isinstance(item, dict) else "MISSING"
            lines.append(f"- {stage}: {status}{suffix}")
    lines.append("")
    lines.append("## 違反一覧")
    violations = data.get("violations", [])
    if not violations:
        lines.append("- なし")
    else:
        for index, violation in enumerate(violations, start=1):
            finding = (
                f" finding_id={violation['finding_id']}" if violation.get("finding_id") is not None else ""
            )
            comment = (
                f" comment_index={violation['comment_index']}"
                if violation.get("comment_index") is not None
                else ""
            )
            lines.append(
                f"{index}. [{violation.get('severity')}] {violation.get('stage')} "
                f"{violation.get('rule')}{finding}{comment}: {violation.get('detail')} "
                f"(auto_fixable={violation.get('auto_fixable')}, "
                f"requires_review_regeneration={violation.get('requires_review_regeneration')})"
            )
    lines.append("")
    lines.append(f"VERDICT: {data.get('verdict')}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate pr-codex preflight-result.json")
    parser.add_argument("--schema", required=True, type=Path, help="preflight-result.v1.json path")
    parser.add_argument("--data", required=True, type=Path, help="preflight-result.json path")
    parser.add_argument("--emit-json", action="store_true", help="print validated JSON")
    parser.add_argument("--emit-markdown", action="store_true", help="print compatible preflight-codex.md markdown")
    args = parser.parse_args()


    try:
        schema = load_json(args.schema)
    except ValueError as exc:
        print(f"{args.schema}: invalid preflight-result schema file", file=sys.stderr)
        print(f"- {exc}", file=sys.stderr)
        return 2

    schema_errors = validate_schema_file(schema)
    if schema_errors:
        print(f"{args.schema}: invalid preflight-result schema file", file=sys.stderr)
        for error in schema_errors:
            print(f"- {error}", file=sys.stderr)
        return 2

    try:
        data = load_json(args.data)
    except ValueError as exc:
        print("INVALID preflight result", file=sys.stderr)
        print(f"- {exc}", file=sys.stderr)
        return 1

    errors = validate_preflight_result(data)
    if errors:
        print("INVALID preflight result", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    if args.emit_json:
        print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False))
    elif args.emit_markdown:
        print(emit_markdown(data), end="")
    else:
        print("valid preflight result")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
