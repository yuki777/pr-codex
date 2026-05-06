#!/usr/bin/env python3
"""Validate score-report.v1 artifacts emitted by score_fixture.py."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "score-report.v1"
FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
SEVERITIES = {"must_fix", "should_fix", "nit", "note"}
AXIS_VALUES = {"yes", "no", "unknown"}
MATCH_STATUSES = {"matched", "missed", "false_positive_promoted"}
TOP_KEYS = {
    "schema_version",
    "fixture_id",
    "evaluated_at",
    "oracle_sha256",
    "expected_finding_ids",
    "exact_pass_rate",
    "acceptable_pass_rate",
    "false_positive_rate",
    "recall_known_bug",
    "gate_pass",
    "scoring_gate",
    "gate_checks",
    "counts",
    "unmatched_actuals",
    "breakdown",
}
RATE_KEYS = ("exact_pass_rate", "acceptable_pass_rate", "false_positive_rate", "recall_known_bug")
COUNT_KEYS = {
    "axes_target",
    "exact_pass",
    "acceptable_pass",
    "known_bug",
    "known_bug_matched",
    "known_false_positive_trap",
    "false_positive_promoted",
}
BREAKDOWN_KEYS = {
    "expected_id",
    "expected_outcome",
    "matched_actual_fingerprint",
    "match_status",
    "axes_diff",
    "severity_diff",
    "evidence_level_ok",
    "notes",
}
AXIS_DIFF_KEYS = {"expected", "actual", "acceptable"}
SEVERITY_DIFF_KEYS = {"expected", "actual", "acceptable"}
GATE_CHECK_KEYS = {"name", "actual", "threshold", "passed"}
UNMATCHED_ACTUAL_KEYS = {"fingerprint", "severity", "category", "path", "title"}
GATE_CHECK_METRICS = {
    "exact_pass_rate_min": ("exact_pass_rate", ">="),
    "acceptable_pass_rate_min": ("acceptable_pass_rate", ">="),
    "false_positive_rate_max": ("false_positive_rate", "<="),
    "recall_known_bug_min": ("recall_known_bug", ">="),
}
SCORING_GATE_KEYS = set(GATE_CHECK_METRICS)


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"{path}: cannot read/parse JSON: {exc}") from exc


def non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and len(value) >= 1 and all(
        not (ord(char) <= 0x1F or 0x7F <= ord(char) <= 0x9F or 0xD800 <= ord(char) <= 0xDFFF)
        for char in value
    )


def non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def number_0_1(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= float(value) <= 1


def rate(numerator: int, denominator: int, empty_value: float = 1.0) -> float:
    if denominator == 0:
        return empty_value
    return round(numerator / denominator, 4)


def int_or_negative(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else -1


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
        extra = sorted(set(obj) - allowed)
        if extra:
            errors.append(f"{path}: unexpected properties: {', '.join(extra)}")


def require(errors: list[str], path: str, obj: dict[str, Any], required: set[str]) -> None:
    missing = sorted(required - set(obj))
    if missing:
        errors.append(f"{path}: missing required properties: {', '.join(missing)}")


def validate_gate_checks(errors: list[str], value: Any) -> None:
    if not isinstance(value, list):
        errors.append("$.gate_checks: must be an array")
        return
    names: list[str] = []
    for index, item in enumerate(value):
        path = f"$.gate_checks[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{path}: must be an object")
            continue
        add_unexpected(errors, path, item, GATE_CHECK_KEYS)
        require(errors, path, item, GATE_CHECK_KEYS)
        if not non_empty_string(item.get("name")):
            errors.append(f"{path}.name: must be a non-empty string")
        elif item.get("name") not in GATE_CHECK_METRICS:
            errors.append(f"{path}.name: unknown gate check name")
        else:
            names.append(item["name"])
        for key in ("actual", "threshold"):
            if not isinstance(item.get(key), (int, float)) or isinstance(item.get(key), bool):
                errors.append(f"{path}.{key}: must be a number")
        if not isinstance(item.get("passed"), bool):
            errors.append(f"{path}.passed: must be boolean")
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        errors.append(f"$.gate_checks: duplicate checks: {', '.join(duplicates)}")


def validate_expected_finding_ids(errors: list[str], value: Any) -> None:
    if not isinstance(value, list) or not value:
        errors.append("$.expected_finding_ids: must be a non-empty array")
        return
    for index, item in enumerate(value):
        if not non_empty_string(item):
            errors.append(f"$.expected_finding_ids[{index}]: must be a non-empty string")
    duplicates = sorted({item for item in value if isinstance(item, str) and value.count(item) > 1})
    if duplicates:
        errors.append(f"$.expected_finding_ids: duplicate ids: {', '.join(duplicates)}")


def validate_scoring_gate(errors: list[str], value: Any) -> None:
    if not isinstance(value, dict):
        errors.append("$.scoring_gate: must be an object")
        return
    add_unexpected(errors, "$.scoring_gate", value, SCORING_GATE_KEYS)
    if not value:
        errors.append("$.scoring_gate: must define at least one gate check")
    for key in SCORING_GATE_KEYS:
        if key in value and not number_0_1(value[key]):
            errors.append(f"$.scoring_gate.{key}: must be a number between 0 and 1")


def validate_counts(errors: list[str], value: Any) -> None:
    if not isinstance(value, dict):
        errors.append("$.counts: must be an object")
        return
    add_unexpected(errors, "$.counts", value, COUNT_KEYS)
    require(errors, "$.counts", value, COUNT_KEYS)
    for key in COUNT_KEYS:
        if key in value and not non_negative_int(value[key]):
            errors.append(f"$.counts.{key}: must be an integer >= 0")
    if non_negative_int(value.get("exact_pass")) and non_negative_int(value.get("axes_target")) and value["exact_pass"] > value["axes_target"]:
        errors.append("$.counts.exact_pass: must be <= axes_target")
    if non_negative_int(value.get("acceptable_pass")) and non_negative_int(value.get("axes_target")) and value["acceptable_pass"] > value["axes_target"]:
        errors.append("$.counts.acceptable_pass: must be <= axes_target")
    if non_negative_int(value.get("known_bug_matched")) and non_negative_int(value.get("known_bug")) and value["known_bug_matched"] > value["known_bug"]:
        errors.append("$.counts.known_bug_matched: must be <= known_bug")
    if (
        non_negative_int(value.get("false_positive_promoted"))
        and non_negative_int(value.get("known_false_positive_trap"))
        and value["false_positive_promoted"] > value["known_false_positive_trap"]
    ):
        errors.append("$.counts.false_positive_promoted: must be <= known_false_positive_trap")


def validate_breakdown(errors: list[str], value: Any) -> None:
    if not isinstance(value, list):
        errors.append("$.breakdown: must be an array")
        return
    for index, item in enumerate(value):
        path = f"$.breakdown[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{path}: must be an object")
            continue
        add_unexpected(errors, path, item, BREAKDOWN_KEYS)
        require(errors, path, item, BREAKDOWN_KEYS)
        for key in ("expected_id", "expected_outcome", "notes"):
            if not isinstance(item.get(key), str):
                errors.append(f"{path}.{key}: must be a string")
        fingerprint = item.get("matched_actual_fingerprint")
        if fingerprint is not None and (not isinstance(fingerprint, str) or not FINGERPRINT_RE.match(fingerprint)):
            errors.append(f"{path}.matched_actual_fingerprint: must be null or 64 lowercase hex")
        if item.get("match_status") not in MATCH_STATUSES:
            errors.append(f"{path}.match_status: invalid value")
        if not isinstance(item.get("evidence_level_ok"), bool):
            errors.append(f"{path}.evidence_level_ok: must be boolean")
        validate_axes_diff(errors, f"{path}.axes_diff", item.get("axes_diff"))
        validate_severity_diff(errors, f"{path}.severity_diff", item.get("severity_diff"))


def validate_unmatched_actuals(errors: list[str], value: Any) -> None:
    if not isinstance(value, list):
        errors.append("$.unmatched_actuals: must be an array")
        return
    for index, item in enumerate(value):
        path = f"$.unmatched_actuals[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{path}: must be an object")
            continue
        add_unexpected(errors, path, item, UNMATCHED_ACTUAL_KEYS)
        require(errors, path, item, UNMATCHED_ACTUAL_KEYS)
        fingerprint = item.get("fingerprint")
        if not isinstance(fingerprint, str) or not FINGERPRINT_RE.match(fingerprint):
            errors.append(f"{path}.fingerprint: must be 64 lowercase hex")
        if item.get("severity") not in SEVERITIES:
            errors.append(f"{path}.severity: invalid value")
        for key in ("category", "path", "title"):
            if not isinstance(item.get(key), str):
                errors.append(f"{path}.{key}: must be a string")


def validate_axes_diff(errors: list[str], path: str, value: Any) -> None:
    if not isinstance(value, dict):
        errors.append(f"{path}: must be an object")
        return
    for axis, diff in value.items():
        item_path = f"{path}.{axis}"
        if axis not in {"real", "triggerable", "impactful", "general"}:
            errors.append(f"{item_path}: invalid axis")
        if not isinstance(diff, dict):
            errors.append(f"{item_path}: must be an object")
            continue
        add_unexpected(errors, item_path, diff, AXIS_DIFF_KEYS)
        require(errors, item_path, diff, AXIS_DIFF_KEYS)
        if diff.get("expected") not in AXIS_VALUES:
            errors.append(f"{item_path}.expected: invalid value")
        if diff.get("actual") is not None and diff.get("actual") not in AXIS_VALUES:
            errors.append(f"{item_path}.actual: invalid value")
        if not isinstance(diff.get("acceptable"), bool):
            errors.append(f"{item_path}.acceptable: must be boolean")


def validate_severity_diff(errors: list[str], path: str, value: Any) -> None:
    if not isinstance(value, dict):
        errors.append(f"{path}: must be an object")
        return
    add_unexpected(errors, path, value, SEVERITY_DIFF_KEYS)
    require(errors, path, value, SEVERITY_DIFF_KEYS)
    expected = value.get("expected")
    if not isinstance(expected, list) or any(item not in SEVERITIES for item in expected):
        errors.append(f"{path}.expected: must be an array of severities")
    actual = value.get("actual")
    if actual is not None and actual not in SEVERITIES:
        errors.append(f"{path}.actual: invalid value")
    if not isinstance(value.get("acceptable"), bool):
        errors.append(f"{path}.acceptable: must be boolean")


def validate_score_report(data: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["$: must be an object"]
    add_unexpected(errors, "$", data, TOP_KEYS)
    require(errors, "$", data, TOP_KEYS)
    if data.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"$.schema_version: must equal {SCHEMA_VERSION}")
    if not non_empty_string(data.get("fixture_id")):
        errors.append("$.fixture_id: must be a non-empty string")
    if "evaluated_at" in data and not is_rfc3339(data["evaluated_at"]):
        errors.append("$.evaluated_at: must be RFC3339 date-time with timezone")
    if "oracle_sha256" in data and (not isinstance(data["oracle_sha256"], str) or not FINGERPRINT_RE.match(data["oracle_sha256"])):
        errors.append("$.oracle_sha256: must be 64 lowercase hex")
    validate_expected_finding_ids(errors, data.get("expected_finding_ids"))
    for key in RATE_KEYS:
        if key in data and not number_0_1(data[key]):
            errors.append(f"$.{key}: must be a number between 0 and 1")
    if "gate_pass" in data and not isinstance(data["gate_pass"], bool):
        errors.append("$.gate_pass: must be boolean")
    validate_scoring_gate(errors, data.get("scoring_gate"))
    validate_gate_checks(errors, data.get("gate_checks"))
    validate_counts(errors, data.get("counts"))
    validate_unmatched_actuals(errors, data.get("unmatched_actuals"))
    validate_breakdown(errors, data.get("breakdown"))
    validate_internal_consistency(errors, data)
    return errors


def validate_internal_consistency(errors: list[str], data: dict[str, Any]) -> None:
    counts = data.get("counts")
    breakdown = data.get("breakdown")
    expected_ids = data.get("expected_finding_ids")
    scoring_gate = data.get("scoring_gate")
    gate_checks = data.get("gate_checks")
    if isinstance(counts, dict):
        expected_rates = {
            "exact_pass_rate": rate(int_or_negative(counts.get("exact_pass")), int_or_negative(counts.get("axes_target"))),
            "acceptable_pass_rate": rate(int_or_negative(counts.get("acceptable_pass")), int_or_negative(counts.get("axes_target"))),
            "false_positive_rate": rate(
                int_or_negative(counts.get("false_positive_promoted")),
                int_or_negative(counts.get("known_false_positive_trap")),
                empty_value=0.0,
            ),
            "recall_known_bug": rate(int_or_negative(counts.get("known_bug_matched")), int_or_negative(counts.get("known_bug"))),
        }
        for key, expected in expected_rates.items():
            if number_0_1(data.get(key)) and data.get(key) != expected:
                errors.append(f"$.{key}: must equal counts-derived value {expected}")

    if isinstance(breakdown, list) and all(isinstance(item, dict) for item in breakdown) and isinstance(counts, dict):
        breakdown_ids = [item.get("expected_id") for item in breakdown]
        if isinstance(expected_ids, list) and all(isinstance(item, str) for item in expected_ids) and breakdown_ids != expected_ids:
            errors.append("$.expected_finding_ids: must equal breakdown[].expected_id order")
        target_rows = [
            item
            for item in breakdown
            if item.get("expected_outcome") == "known_bug"
            or (item.get("expected_outcome") == "acceptable_risk" and item.get("match_status") == "matched")
        ]
        breakdown_counts = {
            "axes_target": len(target_rows),
            "exact_pass": sum(1 for item in target_rows if row_axes_exact(item)),
            "acceptable_pass": sum(1 for item in target_rows if row_acceptable(item)),
            "known_bug": sum(1 for item in breakdown if item.get("expected_outcome") == "known_bug"),
            "known_bug_matched": sum(
                1
                for item in breakdown
                if item.get("expected_outcome") == "known_bug" and item.get("match_status") == "matched"
            ),
            "known_false_positive_trap": sum(1 for item in breakdown if item.get("expected_outcome") == "known_false_positive_trap"),
            "false_positive_promoted": sum(1 for item in breakdown if item.get("match_status") == "false_positive_promoted"),
        }
        for key, expected in breakdown_counts.items():
            if non_negative_int(counts.get(key)) and counts.get(key) != expected:
                errors.append(f"$.counts.{key}: must equal breakdown-derived value {expected}")

    if isinstance(gate_checks, list) and all(isinstance(item, dict) for item in gate_checks):
        expected_gate_pass = all(item.get("passed") is True for item in gate_checks)
        if isinstance(data.get("gate_pass"), bool) and data.get("gate_pass") is not expected_gate_pass:
            errors.append("$.gate_pass: must equal all(gate_checks[].passed)")
        valid_gate_thresholds: dict[str, float] = {}
        if isinstance(scoring_gate, dict):
            valid_gate_thresholds = {
                key: round(float(scoring_gate[key]), 4)
                for key in SCORING_GATE_KEYS
                if key in scoring_gate and number_0_1(scoring_gate[key])
            }
            check_names = [
                item.get("name")
                for item in gate_checks
                if isinstance(item.get("name"), str) and item.get("name") in GATE_CHECK_METRICS
            ]
            missing = sorted(set(valid_gate_thresholds) - set(check_names))
            unexpected = sorted(set(check_names) - set(valid_gate_thresholds))
            if missing:
                errors.append(f"$.gate_checks: missing checks from scoring_gate: {', '.join(missing)}")
            if unexpected:
                errors.append(f"$.gate_checks: unexpected checks not present in scoring_gate: {', '.join(unexpected)}")
        for index, item in enumerate(gate_checks):
            name = item.get("name")
            actual = item.get("actual")
            threshold = item.get("threshold")
            passed = item.get("passed")
            if not isinstance(name, str) or not isinstance(actual, (int, float)) or not isinstance(threshold, (int, float)) or not isinstance(passed, bool):
                continue
            if name not in GATE_CHECK_METRICS:
                continue
            metric_name, operator = GATE_CHECK_METRICS[name]
            if number_0_1(data.get(metric_name)) and actual != data.get(metric_name):
                errors.append(f"$.gate_checks[{index}].actual: must equal $.{metric_name}")
            if name in valid_gate_thresholds and threshold != valid_gate_thresholds[name]:
                errors.append(f"$.gate_checks[{index}].threshold: must equal $.scoring_gate.{name}")
            expected_passed = actual <= threshold if name.endswith("_max") else actual >= threshold
            if operator == "<=":
                expected_passed = actual <= threshold
            elif operator == ">=":
                expected_passed = actual >= threshold
            if passed is not expected_passed:
                errors.append(f"$.gate_checks[{index}].passed: must match actual/threshold comparison")


def row_axes_exact(item: dict[str, Any]) -> bool:
    axes = item.get("axes_diff")
    return isinstance(axes, dict) and bool(axes) and all(
        isinstance(diff, dict) and diff.get("actual") == diff.get("expected") and diff.get("actual") is not None
        for diff in axes.values()
    )


def row_acceptable(item: dict[str, Any]) -> bool:
    axes = item.get("axes_diff")
    severity = item.get("severity_diff")
    return (
        isinstance(axes, dict)
        and bool(axes)
        and all(isinstance(diff, dict) and diff.get("acceptable") is True for diff in axes.values())
        and isinstance(severity, dict)
        and severity.get("acceptable") is True
        and item.get("evidence_level_ok") is True
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate score-report.v1 artifact")
    parser.add_argument("--schema", required=True, type=Path, help="schemas/score-report.v1.json path")
    parser.add_argument("--data", required=True, type=Path, help="score-report.json path")
    args = parser.parse_args()

    try:
        schema = load_json(args.schema)
        data = load_json(args.data)
    except ValueError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    if not isinstance(schema, dict) or schema.get("$id") is None:
        print(f"{args.schema}: invalid score-report schema file", file=sys.stderr)
        return 2
    errors = validate_score_report(data)
    if errors:
        print("INVALID score report", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("VALID score report")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
