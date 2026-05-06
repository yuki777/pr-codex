#!/usr/bin/env python3
"""Generate the M1→M2 gate report.

Inputs that are not available yet are recorded as status=unknown instead of
being downgraded to failure. This lets deterministic fixture scoring run in CI
while operational measurements are supplied by manual/deep eval runs.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "tasks"
if str(TASKS) not in sys.path:
    sys.path.insert(0, str(TASKS))

from validate_m1_m2_gate import validate_m1_m2_gate  # noqa: E402
from validate_score_report import validate_score_report  # noqa: E402

CRITERIA_ORDER = [
    "payload_compat_422",
    "must_fix_count_consistency",
    "step_4_5_pass_rate",
    "run_plan_emitted",
    "loop_completion_rate",
    "fixture_scoring_gate",
]


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"{path}: cannot read/parse JSON: {exc}") from exc


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def criterion(name: str, status: str, evidence: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "status": status, "evidence": evidence}


def status_payload_compat(inputs: dict[str, Any]) -> dict[str, Any]:
    if "payload_422_count" not in inputs:
        return criterion("payload_compat_422", "unknown", {"reason": "payload_422_count missing"})
    value = inputs.get("payload_422_count")
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return criterion("payload_compat_422", "unknown", {"payload_422_count": value, "reason": "invalid count"})
    return criterion("payload_compat_422", "pass" if value == 0 else "fail", {"payload_422_count": value})


def status_must_fix_consistency(inputs: dict[str, Any]) -> dict[str, Any]:
    counts = inputs.get("must_fix_count_by_source")
    required = ["findings_verified", "review_md", "payload"]
    if not isinstance(counts, dict):
        return criterion("must_fix_count_consistency", "unknown", {"reason": "must_fix_count_by_source missing"})
    missing = [key for key in required if key not in counts]
    if missing:
        return criterion("must_fix_count_consistency", "unknown", {"counts": counts, "missing": missing})
    values = [counts[key] for key in required]
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in values):
        return criterion("must_fix_count_consistency", "unknown", {"counts": counts, "reason": "invalid count"})
    return criterion("must_fix_count_consistency", "pass" if len(set(values)) == 1 else "fail", {"counts": {key: counts[key] for key in required}})


def status_step_4_5(inputs: dict[str, Any]) -> dict[str, Any]:
    baseline = inputs.get("step_4_5_pass_rate_baseline")
    current = inputs.get("step_4_5_pass_rate_current")
    if not is_number(baseline) or not is_number(current):
        return criterion(
            "step_4_5_pass_rate",
            "unknown",
            {"baseline": baseline, "current": current, "reason": "baseline/current missing or invalid"},
        )
    threshold = round(float(baseline) - 0.05, 4)
    return criterion(
        "step_4_5_pass_rate",
        "pass" if float(current) >= threshold else "fail",
        {"baseline": float(baseline), "current": float(current), "allowed_min": threshold},
    )


def status_run_plan(inputs: dict[str, Any]) -> dict[str, Any]:
    if "run_plan_emitted" not in inputs:
        return criterion("run_plan_emitted", "unknown", {"reason": "run_plan_emitted missing"})
    value = inputs.get("run_plan_emitted")
    if not isinstance(value, bool):
        return criterion("run_plan_emitted", "unknown", {"run_plan_emitted": value, "reason": "must be boolean"})
    return criterion("run_plan_emitted", "pass" if value else "fail", {"run_plan_emitted": value})


def status_loop_completion(inputs: dict[str, Any]) -> dict[str, Any]:
    baseline = inputs.get("loop_completion_rate_baseline")
    current = inputs.get("loop_completion_rate_current")
    if not is_number(baseline) or not is_number(current):
        return criterion(
            "loop_completion_rate",
            "unknown",
            {"baseline": baseline, "current": current, "reason": "baseline/current missing or invalid"},
        )
    return criterion(
        "loop_completion_rate",
        "pass" if float(current) >= float(baseline) else "fail",
        {"baseline": float(baseline), "current": float(current)},
    )


def status_fixture_scoring(score_reports: list[dict[str, Any]]) -> dict[str, Any]:
    if not score_reports:
        return criterion("fixture_scoring_gate", "unknown", {"reason": "no score reports supplied", "records": []})
    records = [
        {
            "fixture_id": report.get("fixture_id"),
            "gate_pass": report.get("gate_pass"),
            "acceptable_pass_rate": report.get("acceptable_pass_rate"),
            "false_positive_rate": report.get("false_positive_rate"),
            "recall_known_bug": report.get("recall_known_bug"),
        }
        for report in score_reports
    ]
    passed = all(report.get("gate_pass") is True for report in score_reports)
    return criterion("fixture_scoring_gate", "pass" if passed else "fail", {"records": records})


def overall(criteria: list[dict[str, Any]]) -> str:
    statuses = [str(item.get("status")) for item in criteria]
    if "fail" in statuses:
        return "fail"
    if "unknown" in statuses:
        return "blocked_by_unknowns"
    return "pass"


def build_report(score_reports: list[dict[str, Any]], inputs: dict[str, Any], evaluated_at: str) -> dict[str, Any]:
    criteria = [
        status_payload_compat(inputs),
        status_must_fix_consistency(inputs),
        status_step_4_5(inputs),
        status_run_plan(inputs),
        status_loop_completion(inputs),
        status_fixture_scoring(score_reports),
    ]
    return {
        "schema_version": "m1-m2-gate.v1",
        "evaluated_at": evaluated_at,
        "criteria": criteria,
        "overall_status": overall(criteria),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate pr-codex M1→M2 gate report")
    parser.add_argument("--score-reports", nargs="*", type=Path, default=[], help="score-report.v1 JSON files")
    parser.add_argument("--inputs", required=True, type=Path, help="m1-m2-inputs.v1 JSON path")
    parser.add_argument("--out", required=True, type=Path, help="output m1-m2-gate.v1 JSON path")
    parser.add_argument("--evaluated-at", default=utc_now(), help="RFC3339 timestamp for deterministic tests")
    parser.add_argument("--schema", type=Path, default=ROOT / "schemas" / "m1-m2-gate.v1.json")
    parser.add_argument("--score-schema", type=Path, default=ROOT / "schemas" / "score-report.v1.json")
    args = parser.parse_args()

    try:
        schema = load_json(args.schema)
        score_schema = load_json(args.score_schema)
        inputs = load_json(args.inputs)
        score_reports = [load_json(path) for path in args.score_reports]
    except ValueError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    if not isinstance(schema, dict) or schema.get("$id") is None:
        print(f"{args.schema}: invalid m1-m2-gate schema file", file=sys.stderr)
        return 2
    if not isinstance(score_schema, dict) or score_schema.get("$id") is None:
        print(f"{args.score_schema}: invalid score-report schema file", file=sys.stderr)
        return 2
    if not isinstance(inputs, dict):
        print("INVALID M1-M2 inputs: must be an object", file=sys.stderr)
        return 1
    if inputs.get("schema_version") not in {None, "m1-m2-inputs.v1"}:
        print("INVALID M1-M2 inputs: schema_version must be m1-m2-inputs.v1 when present", file=sys.stderr)
        return 1
    report_errors: list[str] = []
    for index, report in enumerate(score_reports):
        report_errors.extend(f"score_reports[{index}]: {error}" for error in validate_score_report(report))
    if report_errors:
        print("INVALID score reports", file=sys.stderr)
        for error in report_errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    report = build_report(score_reports, inputs, args.evaluated_at)
    errors = validate_m1_m2_gate(report)
    if errors:
        print("INVALID generated M1-M2 gate report", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
