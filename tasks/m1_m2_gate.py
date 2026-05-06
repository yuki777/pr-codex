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
EXPECTED_FIXTURE_IDS = {
    "bear-sunday-pr164-small",
    "bear-sunday-pr143-medium",
    "bear-sunday-pr171-large",
}
EXPECTED_FIXTURE_SCORING_GATES = {
    "bear-sunday-pr164-small": {
        "acceptable_pass_rate_min": 0.8,
        "false_positive_rate_max": 0.1,
    },
    "bear-sunday-pr143-medium": {
        "exact_pass_rate_min": 0.5,
        "acceptable_pass_rate_min": 0.8,
        "false_positive_rate_max": 0.1,
    },
    "bear-sunday-pr171-large": {
        "acceptable_pass_rate_min": 0.7,
        "false_positive_rate_max": 0.15,
    },
}
SCORE_GATE_METRICS = {
    "exact_pass_rate_min": ("exact_pass_rate", ">="),
    "acceptable_pass_rate_min": ("acceptable_pass_rate", ">="),
    "false_positive_rate_max": ("false_positive_rate", "<="),
    "recall_known_bug_min": ("recall_known_bug", ">="),
}


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"{path}: cannot read/parse JSON: {exc}") from exc


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_rate(value: Any) -> bool:
    return is_number(value) and 0 <= float(value) <= 1


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
    if not is_rate(baseline) or not is_rate(current):
        return criterion(
            "step_4_5_pass_rate",
            "unknown",
            {"baseline": baseline, "current": current, "reason": "baseline/current missing or not a rate in [0, 1]"},
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
    if not is_rate(baseline) or not is_rate(current):
        return criterion(
            "loop_completion_rate",
            "unknown",
            {"baseline": baseline, "current": current, "reason": "baseline/current missing or not a rate in [0, 1]"},
        )
    return criterion(
        "loop_completion_rate",
        "pass" if float(current) >= float(baseline) else "fail",
        {"baseline": float(baseline), "current": float(current)},
    )


def status_fixture_scoring(score_reports: list[dict[str, Any]]) -> dict[str, Any]:
    if not score_reports:
        return criterion("fixture_scoring_gate", "unknown", {"reason": "no score reports supplied", "records": []})
    fixture_ids = [report.get("fixture_id") for report in score_reports]
    duplicate_ids = sorted({fixture_id for fixture_id in fixture_ids if fixture_ids.count(fixture_id) > 1})
    supplied_ids = {fixture_id for fixture_id in fixture_ids if isinstance(fixture_id, str)}
    missing_ids = sorted(EXPECTED_FIXTURE_IDS - supplied_ids)
    unknown_ids = sorted(supplied_ids - EXPECTED_FIXTURE_IDS)
    records = [
        {
            "fixture_id": report.get("fixture_id"),
            "gate_pass": report.get("gate_pass"),
            "gate_consistent": score_report_gate_consistent(report),
            "required_scoring_gate": EXPECTED_FIXTURE_SCORING_GATES.get(str(report.get("fixture_id")), {}),
            "reported_scoring_gate": report.get("scoring_gate"),
            "acceptable_pass_rate": report.get("acceptable_pass_rate"),
            "false_positive_rate": report.get("false_positive_rate"),
            "recall_known_bug": report.get("recall_known_bug"),
        }
        for report in score_reports
    ]
    structure_ok = not duplicate_ids and not missing_ids and not unknown_ids
    passed = structure_ok and all(score_report_gate_consistent(report) for report in score_reports)
    evidence = {"records": records}
    if missing_ids:
        evidence["missing_fixture_ids"] = missing_ids
    if duplicate_ids:
        evidence["duplicate_fixture_ids"] = duplicate_ids
    if unknown_ids:
        evidence["unknown_fixture_ids"] = unknown_ids
    return criterion("fixture_scoring_gate", "pass" if passed else "fail", evidence)


def score_report_gate_consistent(report: dict[str, Any]) -> bool:
    fixture_id = report.get("fixture_id")
    if not isinstance(fixture_id, str) or fixture_id not in EXPECTED_FIXTURE_SCORING_GATES:
        return False
    required_gate = EXPECTED_FIXTURE_SCORING_GATES[fixture_id]
    raw_reported_gate = report.get("scoring_gate")
    if not isinstance(raw_reported_gate, dict) or set(raw_reported_gate) != set(required_gate):
        return False
    reported_gate = normalized_scoring_gate(report.get("scoring_gate"))
    if reported_gate != required_gate:
        return False
    gate_checks = report.get("gate_checks")
    if report.get("gate_pass") is not True or not isinstance(gate_checks, list) or not gate_checks:
        return False
    check_names = [check.get("name") for check in gate_checks if isinstance(check, dict)]
    if len(check_names) != len(set(check_names)):
        return False
    if set(check_names) != set(required_gate):
        return False
    for check in gate_checks:
        if not isinstance(check, dict):
            return False
        name = check.get("name")
        if name not in SCORE_GATE_METRICS:
            return False
        metric_name, operator = SCORE_GATE_METRICS[name]
        actual = check.get("actual")
        threshold = check.get("threshold")
        passed = check.get("passed")
        if not is_number(actual) or not is_number(threshold) or not is_number(report.get(metric_name)):
            return False
        if float(threshold) != required_gate[name]:
            return False
        if actual != report.get(metric_name):
            return False
        expected_passed = actual <= threshold if operator == "<=" else actual >= threshold
        if passed is not expected_passed or passed is not True:
            return False
    return True


def normalized_scoring_gate(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    gate: dict[str, float] = {}
    for name in SCORE_GATE_METRICS:
        threshold = value.get(name)
        if is_rate(threshold):
            gate[name] = round(float(threshold), 4)
    return gate


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
