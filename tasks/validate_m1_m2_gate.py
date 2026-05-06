#!/usr/bin/env python3
"""Validate m1-m2-gate.v1 reports."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "m1-m2-gate.v1"
STATUSES = {"pass", "fail", "unknown"}
OVERALL_STATUSES = {"pass", "fail", "blocked_by_unknowns"}
EXPECTED_CRITERIA = [
    "payload_compat_422",
    "must_fix_count_consistency",
    "step_4_5_pass_rate",
    "run_plan_emitted",
    "loop_completion_rate",
    "fixture_scoring_gate",
]
TOP_KEYS = {"schema_version", "evaluated_at", "criteria", "overall_status"}
CRITERION_KEYS = {"name", "status", "evidence"}


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"{path}: cannot read/parse JSON: {exc}") from exc


def is_rfc3339(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return False
    return parsed.tzinfo is not None


def non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and len(value) >= 1


def add_unexpected(errors: list[str], path: str, obj: Any, allowed: set[str]) -> None:
    if isinstance(obj, dict):
        extra = sorted(set(obj) - allowed)
        if extra:
            errors.append(f"{path}: unexpected properties: {', '.join(extra)}")


def require(errors: list[str], path: str, obj: dict[str, Any], required: set[str]) -> None:
    missing = sorted(required - set(obj))
    if missing:
        errors.append(f"{path}: missing required properties: {', '.join(missing)}")


def expected_overall(statuses: list[str]) -> str:
    if "fail" in statuses:
        return "fail"
    if "unknown" in statuses:
        return "blocked_by_unknowns"
    return "pass"


def validate_m1_m2_gate(data: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["$: must be an object"]
    add_unexpected(errors, "$", data, TOP_KEYS)
    require(errors, "$", data, TOP_KEYS)
    if data.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"$.schema_version: must equal {SCHEMA_VERSION}")
    if "evaluated_at" in data and not is_rfc3339(data["evaluated_at"]):
        errors.append("$.evaluated_at: must be RFC3339 date-time with timezone")
    if data.get("overall_status") not in OVERALL_STATUSES:
        errors.append("$.overall_status: invalid value")

    criteria = data.get("criteria")
    if not isinstance(criteria, list):
        errors.append("$.criteria: must be an array")
        return errors
    names: list[str] = []
    statuses: list[str] = []
    for index, item in enumerate(criteria):
        path = f"$.criteria[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{path}: must be an object")
            continue
        add_unexpected(errors, path, item, CRITERION_KEYS)
        require(errors, path, item, CRITERION_KEYS)
        name = item.get("name")
        status = item.get("status")
        if not non_empty_string(name):
            errors.append(f"{path}.name: must be a non-empty string")
        else:
            names.append(name)
        if status not in STATUSES:
            errors.append(f"{path}.status: invalid value")
        else:
            statuses.append(status)
        if not isinstance(item.get("evidence"), dict):
            errors.append(f"{path}.evidence: must be an object")
    missing = [name for name in EXPECTED_CRITERIA if name not in names]
    if missing:
        errors.append(f"$.criteria: missing criteria: {', '.join(missing)}")
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        errors.append(f"$.criteria: duplicate criteria: {', '.join(duplicates)}")
    if data.get("overall_status") in OVERALL_STATUSES and len(statuses) == len(criteria):
        expected = expected_overall(statuses)
        if data.get("overall_status") != expected:
            errors.append(f"$.overall_status: must be {expected} based on criterion statuses")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate m1-m2-gate.v1 report")
    parser.add_argument("--schema", required=True, type=Path, help="schemas/m1-m2-gate.v1.json path")
    parser.add_argument("--data", required=True, type=Path, help="m1-m2-gate.json path")
    args = parser.parse_args()

    try:
        schema = load_json(args.schema)
        data = load_json(args.data)
    except ValueError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    if not isinstance(schema, dict) or schema.get("$id") is None:
        print(f"{args.schema}: invalid m1-m2-gate schema file", file=sys.stderr)
        return 2
    errors = validate_m1_m2_gate(data)
    if errors:
        print("INVALID M1-M2 gate report", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("VALID M1-M2 gate report")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
