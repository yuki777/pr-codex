#!/usr/bin/env python3
"""Validate pr-codex status.json stage reporting.

The stage fields are backward-compatible: old status.json files without stage or
failed_stage remain valid. When the new fields are present, this validator
checks that they use the F4 ranker / hunter / verifier / explainer vocabulary
and that failed_stage is only populated for failed runs.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

STAGES = ["ranker", "hunter", "verifier", "explainer"]
STAGE_SET = set(STAGES)
STATES = {"running", "completed", "failed"}
STATUS_KEYS = {"state", "started_at", "finished_at", "head_sha", "exit_code", "stage", "failed_stage", "stage_durations_ms"}


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


def is_non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def validate_status(data: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["$: must be an object"]
    extra = sorted(set(data) - STATUS_KEYS)
    if extra:
        errors.append(f"$: unexpected properties: {', '.join(extra)}")

    state = data.get("state")
    if state not in STATES:
        errors.append("$.state: must be running, completed, or failed")

    if "started_at" in data and not is_rfc3339(data["started_at"]):
        errors.append("$.started_at: must be an RFC3339 date-time string")
    if "finished_at" in data and not is_rfc3339(data["finished_at"]):
        errors.append("$.finished_at: must be an RFC3339 date-time string")
    if "head_sha" in data and (not isinstance(data["head_sha"], str) or not data["head_sha"]):
        errors.append("$.head_sha: must be a non-empty string")
    if "exit_code" in data and not is_non_negative_int(data["exit_code"]):
        errors.append("$.exit_code: must be a non-negative integer")

    stage = data.get("stage") if "stage" in data else None
    failed_stage = data.get("failed_stage") if "failed_stage" in data else None
    if stage is not None and stage not in STAGE_SET:
        errors.append("$.stage: must be null or one of ranker, hunter, verifier, explainer")
    if failed_stage is not None and failed_stage not in STAGE_SET:
        errors.append("$.failed_stage: must be null or one of ranker, hunter, verifier, explainer")
    if state != "failed" and failed_stage is not None:
        errors.append("$.failed_stage: must be null unless state=failed")
    if state == "failed" and "failed_stage" in data and failed_stage is None:
        errors.append("$.failed_stage: must name the failed stage when present on a failed status")
    if state == "completed" and stage not in (None, "explainer"):
        errors.append("$.stage: completed status must use null or explainer")

    durations = data.get("stage_durations_ms")
    if "stage_durations_ms" in data:
        if not isinstance(durations, dict):
            errors.append("$.stage_durations_ms: must be an object")
        else:
            extra_duration_keys = sorted(set(durations) - STAGE_SET)
            if extra_duration_keys:
                errors.append(f"$.stage_durations_ms: unexpected stages: {', '.join(extra_duration_keys)}")
            for key, value in durations.items():
                if key in STAGE_SET and not is_non_negative_int(value):
                    errors.append(f"$.stage_durations_ms.{key}: must be a non-negative integer")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate pr-codex status.json")
    parser.add_argument("--data", required=True, type=Path, help="status.json path")
    args = parser.parse_args()

    try:
        data = load_json(args.data)
    except ValueError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1

    errors = validate_status(data)
    if errors:
        print("INVALID status", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print("VALID status")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
