#!/usr/bin/env python3
"""Score a pr-codex fixture oracle against findings.verified.json.

The runner is deterministic and stdlib-only. It compares fixture oracle rows
(`expected-findings.v1`) with canonical runtime output (`findings.v1`) using a
semantic matching key rather than ids: expected ids are human/oracle labels,
whereas actual ids are deterministic fingerprints.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "tasks"
if str(TASKS) not in sys.path:
    sys.path.insert(0, str(TASKS))

from validate_expected_findings import validate_expected_findings  # noqa: E402
from validate_findings import validate_artifact as validate_findings_artifact  # noqa: E402
from validate_score_report import validate_score_report  # noqa: E402

AXES = ("real", "triggerable", "impactful")
SEVERITY_RANK = {"note": 0, "nit": 1, "should_fix": 2, "must_fix": 3}
EVIDENCE_RANK = {
    "suspicion": 0,
    "corroborated": 1,
    "trigger_path_identified": 2,
    "impact_explained": 3,
    "verified": 4,
}
STOPWORDS = {
    "a",
    "an",
    "and",
    "as",
    "at",
    "be",
    "by",
    "class",
    "claims",
    "for",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "reviewer",
    "should",
    "the",
    "to",
    "with",
}


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"{path}: cannot read/parse JSON: {exc}") from exc


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def rate(numerator: int, denominator: int, empty_value: float = 1.0) -> float:
    if denominator == 0:
        return empty_value
    return round(numerator / denominator, 4)


def actual_key(finding: dict[str, Any]) -> tuple[str, str]:
    location = finding.get("location") if isinstance(finding.get("location"), dict) else {}
    path = location.get("path") if isinstance(location.get("path"), str) else ""
    category = finding.get("category") if isinstance(finding.get("category"), str) else ""
    return path, category


def expected_key(expected: dict[str, Any]) -> tuple[str, str] | None:
    location = expected.get("location_match")
    if not isinstance(location, dict) or not isinstance(location.get("path"), str):
        return None
    category = expected.get("category") if isinstance(expected.get("category"), str) else ""
    return location["path"], category


def location_matches(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    expected_location = expected.get("location_match")
    actual_location = actual.get("location")
    if not isinstance(expected_location, dict) or not isinstance(actual_location, dict):
        return False
    if expected_location.get("path") != actual_location.get("path"):
        return False
    line_range = expected_location.get("line_range")
    start_line = actual_location.get("start_line")
    end_line = actual_location.get("end_line", start_line)
    if (
        not isinstance(line_range, list)
        or len(line_range) != 2
        or not all(isinstance(line, int) and not isinstance(line, bool) for line in line_range)
        or not isinstance(start_line, int)
        or isinstance(start_line, bool)
        or not isinstance(end_line, int)
        or isinstance(end_line, bool)
    ):
        return False
    return start_line <= line_range[1] and end_line >= line_range[0]


def location_candidate_indexes(
    expected: dict[str, Any], actuals: list[dict[str, Any]]
) -> list[int]:
    return [
        index
        for index, actual in enumerate(actuals)
        if location_matches(expected, actual)
        and title_keyword_match(expected.get("title"), actual.get("title"))
    ]


def axes_hamming(expected: dict[str, Any], actual: dict[str, Any]) -> int:
    expected_axes = expected.get("expected_axes") if isinstance(expected.get("expected_axes"), dict) else {}
    actual_axes = actual.get("axes") if isinstance(actual.get("axes"), dict) else {}
    return sum(1 for axis in AXES if expected_axes.get(axis) != actual_axes.get(axis))


def choose_best_actual(expected: dict[str, Any], actuals: list[dict[str, Any]], candidate_indexes: list[int], used: set[int]) -> int | None:
    available = [index for index in candidate_indexes if index not in used]
    if not available:
        return None
    available.sort(
        key=lambda index: (
            0 if location_matches(expected, actuals[index]) else 1,
            0 if expected_key(expected) == actual_key(actuals[index]) else 1,
            axes_hamming(expected, actuals[index]),
            0 if blast_radius_matches(expected, actuals[index]) else 1,
            -SEVERITY_RANK.get(str(actuals[index].get("severity")), -1),
            str(actuals[index].get("fingerprint") or actuals[index].get("id") or ""),
            index,
        )
    )
    return available[0]


def keyword_candidate_indexes(expected: dict[str, Any], actuals: list[dict[str, Any]]) -> list[int]:
    return [
        index
        for index, actual in enumerate(actuals)
        if title_keyword_match(expected.get("title"), actual.get("title"))
    ]


def tokenize(text: Any) -> set[str]:
    if not isinstance(text, str):
        return set()
    return {token for token in re.findall(r"[a-z0-9]+", text.lower()) if len(token) >= 2 and token not in STOPWORDS}


def title_keyword_match(expected_title: Any, actual_title: Any) -> bool:
    expected_tokens = tokenize(expected_title)
    actual_tokens = tokenize(actual_title)
    if not expected_tokens or not actual_tokens:
        return False
    overlap = expected_tokens & actual_tokens
    return len(overlap) >= min(2, len(expected_tokens))


def actual_matches_trap(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    key = expected_key(expected)
    if key is not None and key == actual_key(actual):
        return True
    return title_keyword_match(expected.get("title"), actual.get("title"))


def severity_allowed(expected: dict[str, Any]) -> list[str]:
    acceptable = expected.get("acceptable_severities")
    if isinstance(acceptable, list) and all(isinstance(item, str) for item in acceptable):
        return list(acceptable)
    severity = expected.get("severity")
    return [severity] if isinstance(severity, str) else ["note", "nit", "should_fix", "must_fix"]


def severity_is_acceptable(expected: dict[str, Any], actual_severity: Any) -> bool:
    return isinstance(actual_severity, str) and actual_severity in set(severity_allowed(expected))


def trap_severity_is_promoted(expected: dict[str, Any], actual_severity: Any) -> bool:
    if not isinstance(actual_severity, str) or actual_severity not in SEVERITY_RANK:
        return False
    allowed = severity_allowed(expected) if "acceptable_severities" in expected else []
    if allowed:
        return SEVERITY_RANK[actual_severity] > max(SEVERITY_RANK[item] for item in allowed if item in SEVERITY_RANK)
    return actual_severity == "must_fix"


def allowed_axis_values(expected: dict[str, Any], axis: str) -> set[str]:
    expected_axes = expected.get("expected_axes") if isinstance(expected.get("expected_axes"), dict) else {}
    allowed = {expected_axes[axis]} if isinstance(expected_axes.get(axis), str) else set()
    overrides = expected.get("acceptable_overrides")
    if isinstance(overrides, dict) and isinstance(overrides.get(axis), list):
        allowed.update(item for item in overrides[axis] if isinstance(item, str))
    return allowed


def axes_diff(expected: dict[str, Any], actual: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    expected_axes = expected.get("expected_axes") if isinstance(expected.get("expected_axes"), dict) else {}
    actual_axes = actual.get("axes") if actual is not None and isinstance(actual.get("axes"), dict) else {}
    diff: dict[str, dict[str, Any]] = {}
    for axis in AXES:
        expected_value = expected_axes.get(axis)
        actual_value = actual_axes.get(axis)
        diff[axis] = {
            "expected": expected_value if isinstance(expected_value, str) else "unknown",
            "actual": actual_value if isinstance(actual_value, str) else None,
            "acceptable": isinstance(actual_value, str) and actual_value in allowed_axis_values(expected, axis),
        }
    return diff


def axes_exact(expected: dict[str, Any], actual: dict[str, Any] | None) -> bool:
    if actual is None:
        return False
    expected_axes = expected.get("expected_axes") if isinstance(expected.get("expected_axes"), dict) else {}
    actual_axes = actual.get("axes") if isinstance(actual.get("axes"), dict) else {}
    return all(expected_axes.get(axis) == actual_axes.get(axis) for axis in AXES)


def axes_acceptable(expected: dict[str, Any], actual: dict[str, Any] | None) -> bool:
    if actual is None:
        return False
    actual_axes = actual.get("axes") if isinstance(actual.get("axes"), dict) else {}
    return all(isinstance(actual_axes.get(axis), str) and actual_axes[axis] in allowed_axis_values(expected, axis) for axis in AXES)


def blast_radius_diff(expected: dict[str, Any], actual: dict[str, Any] | None) -> dict[str, Any]:
    expected_value = expected.get("expected_blast_radius")
    actual_value = actual.get("blast_radius") if actual is not None else None
    matches = (
        isinstance(expected_value, str)
        and isinstance(actual_value, str)
        and actual_value == expected_value
    )
    return {
        "expected": expected_value if isinstance(expected_value, str) else "unknown",
        "actual": actual_value if isinstance(actual_value, str) else None,
        "acceptable": matches,
    }


def blast_radius_matches(expected: dict[str, Any], actual: dict[str, Any] | None) -> bool:
    if actual is None:
        return False
    expected_value = expected.get("expected_blast_radius")
    actual_value = actual.get("blast_radius")
    return (
        isinstance(expected_value, str)
        and isinstance(actual_value, str)
        and actual_value == expected_value
    )


def evidence_ok(expected: dict[str, Any], actual: dict[str, Any] | None) -> bool:
    if actual is None:
        return False
    minimum = expected.get("minimum_evidence_level")
    actual_level = actual.get("evidence_level")
    if not isinstance(minimum, str) or not isinstance(actual_level, str):
        return False
    return EVIDENCE_RANK.get(actual_level, -1) >= EVIDENCE_RANK.get(minimum, 99)


def severity_diff(expected: dict[str, Any], actual: dict[str, Any] | None) -> dict[str, Any]:
    actual_severity = actual.get("severity") if actual is not None else None
    return {
        "expected": severity_allowed(expected),
        "actual": actual_severity if isinstance(actual_severity, str) else None,
        "acceptable": severity_is_acceptable(expected, actual_severity),
    }


def contributes_to_axes_target(expected: dict[str, Any], actual: dict[str, Any] | None) -> bool:
    outcome = expected.get("expected_outcome")
    if outcome == "known_bug":
        return True
    # acceptable_risk is optional: absence is not recall loss, but if the model
    # raises it, score the axes/severity to prevent over-promotion regressions.
    return outcome == "acceptable_risk" and actual is not None


def match_expected_to_actuals(expected_findings: list[dict[str, Any]], actuals: list[dict[str, Any]]) -> dict[int, int]:
    groups: dict[tuple[str, str], list[int]] = {}
    for index, actual in enumerate(actuals):
        groups.setdefault(actual_key(actual), []).append(index)

    matches: dict[int, int] = {}
    used: set[int] = set()
    for expected_index, expected in enumerate(expected_findings):
        if expected.get("expected_outcome") == "known_false_positive_trap":
            continue
        key = expected_key(expected)
        candidate_indexes = groups.get(key, []) if key is not None else []
        if expected.get("expected_outcome") == "known_bug":
            expected_location = expected.get("location_match")
            if isinstance(expected_location, dict) and "line_range" in expected_location:
                location_indexes = set(location_candidate_indexes(expected, actuals))
                candidate_indexes = [
                    index for index in candidate_indexes if index in location_indexes
                ]
        elif expected.get("expected_outcome") == "acceptable_risk":
            candidate_indexes = list(
                dict.fromkeys(candidate_indexes + keyword_candidate_indexes(expected, actuals))
            )
        best = choose_best_actual(expected, actuals, candidate_indexes, used)
        if best is not None:
            matches[expected_index] = best
            used.add(best)
    return matches


def build_breakdown(expected_findings: list[dict[str, Any]], actuals: list[dict[str, Any]], matches: dict[int, int]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    counts = {
        "axes_target": 0,
        "exact_pass": 0,
        "acceptable_pass": 0,
        "known_bug": 0,
        "known_bug_matched": 0,
        "known_false_positive_trap": 0,
        "false_positive_promoted": 0,
    }
    breakdown: list[dict[str, Any]] = []
    for expected_index, expected in enumerate(expected_findings):
        outcome = expected.get("expected_outcome")
        if outcome == "known_false_positive_trap":
            counts["known_false_positive_trap"] += 1
            # Trap scoring is evaluated against all actual findings, not only
            # unmatched ones. A trap may share the same path/category as a real
            # expected row; if a model emits the trap title with blocking
            # severity, it must still count as a false positive even if greedy
            # matching consumed that actual for recall.
            relevant = [actual for actual in actuals if actual_matches_trap(expected, actual)]
            promoted = [actual for actual in relevant if trap_severity_is_promoted(expected, actual.get("severity"))]
            actual = promoted[0] if promoted else (relevant[0] if relevant else None)
            is_promoted = bool(promoted)
            if is_promoted:
                counts["false_positive_promoted"] += 1
            breakdown.append(
                {
                    "expected_id": str(expected.get("id")),
                    "expected_outcome": str(outcome),
                    "matched_actual_fingerprint": actual.get("fingerprint") if actual is not None else None,
                    "match_status": "false_positive_promoted" if is_promoted else "matched",
                    "axes_diff": axes_diff(expected, actual),
                    "blast_radius_diff": blast_radius_diff(expected, actual),
                    "severity_diff": severity_diff(expected, actual),
                    "evidence_level_ok": evidence_ok(expected, actual) if actual is not None else True,
                    "notes": "trap promoted above acceptable severity" if is_promoted else "trap not promoted",
                }
            )
            continue

        actual = actuals[matches[expected_index]] if expected_index in matches else None
        if outcome == "known_bug":
            counts["known_bug"] += 1
            if actual is not None:
                counts["known_bug_matched"] += 1
        target = contributes_to_axes_target(expected, actual)
        exact_ok = axes_exact(expected, actual) and blast_radius_matches(expected, actual)
        acceptable_ok = (
            axes_acceptable(expected, actual)
            and blast_radius_matches(expected, actual)
            and severity_is_acceptable(expected, actual.get("severity") if actual is not None else None)
            and evidence_ok(expected, actual)
        )
        if target:
            counts["axes_target"] += 1
            counts["exact_pass"] += 1 if exact_ok else 0
            counts["acceptable_pass"] += 1 if acceptable_ok else 0
        note = "matched by location/category" if actual is not None else "expected finding was not detected"
        if outcome == "acceptable_risk" and actual is None:
            note = "optional acceptable_risk was not raised"
        breakdown.append(
            {
                "expected_id": str(expected.get("id")),
                "expected_outcome": str(outcome),
                "matched_actual_fingerprint": actual.get("fingerprint") if actual is not None else None,
                "match_status": "matched" if actual is not None else "missed",
                "axes_diff": axes_diff(expected, actual),
                "blast_radius_diff": blast_radius_diff(expected, actual),
                "severity_diff": severity_diff(expected, actual),
                "evidence_level_ok": evidence_ok(expected, actual),
                "notes": note,
            }
        )
    return breakdown, counts


def summarize_unmatched_actuals(actuals: list[dict[str, Any]], matches: dict[int, int]) -> list[dict[str, Any]]:
    matched = set(matches.values())
    summaries: list[dict[str, Any]] = []
    for index, actual in enumerate(actuals):
        severity = actual.get("severity")
        if index in matched or severity not in {"must_fix", "should_fix"}:
            continue
        path, category = actual_key(actual)
        summaries.append(
            {
                "fingerprint": str(actual.get("fingerprint") or actual.get("id") or ""),
                "severity": severity,
                "category": category,
                "path": path,
                "title": str(actual.get("title") or ""),
            }
        )
    return summaries


def gate_checks(scoring_gate: Any, metrics: dict[str, float]) -> list[dict[str, Any]]:
    if not isinstance(scoring_gate, dict):
        return []
    checks: list[dict[str, Any]] = []
    mapping = {
        "exact_pass_rate_min": ("exact_pass_rate", ">="),
        "acceptable_pass_rate_min": ("acceptable_pass_rate", ">="),
        "false_positive_rate_max": ("false_positive_rate", "<="),
        "recall_known_bug_min": ("recall_known_bug", ">="),
    }
    for threshold_name, (metric_name, operator) in mapping.items():
        if threshold_name not in scoring_gate:
            continue
        actual = metrics[metric_name]
        threshold = round(float(scoring_gate[threshold_name]), 4)
        passed = actual >= threshold if operator == ">=" else actual <= threshold
        checks.append({"name": threshold_name, "actual": actual, "threshold": threshold, "passed": passed})
    return checks


def report_scoring_gate(scoring_gate: Any) -> dict[str, float]:
    """Return a deterministic, report-safe copy of the fixture scoring gate."""
    if not isinstance(scoring_gate, dict):
        return {}
    mapping = {
        "exact_pass_rate_min": "exact_pass_rate_min",
        "acceptable_pass_rate_min": "acceptable_pass_rate_min",
        "false_positive_rate_max": "false_positive_rate_max",
        "recall_known_bug_min": "recall_known_bug_min",
    }
    copied: dict[str, float] = {}
    for key in mapping:
        if key in scoring_gate and isinstance(scoring_gate[key], (int, float)) and not isinstance(scoring_gate[key], bool):
            copied[key] = round(float(scoring_gate[key]), 4)
    return copied


def expected_finding_ids(expected_findings: list[dict[str, Any]]) -> list[str]:
    return [str(item.get("id")) for item in expected_findings]


def validate_fixture_context(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    source = expected.get("source")
    pr = actual.get("pr")
    if not isinstance(source, dict) or not isinstance(pr, dict):
        return ["expected.source and actual.pr must be objects"]
    comparisons = [
        ("repository", source.get("repository"), pr.get("repository")),
        ("pr_number", source.get("pr_number"), pr.get("number")),
        ("base_sha", source.get("base_sha"), pr.get("base_sha")),
        ("head_sha", source.get("head_sha"), pr.get("head_sha")),
    ]
    for label, expected_value, actual_value in comparisons:
        if expected_value != actual_value:
            errors.append(f"context.{label}: expected {expected_value!r}, actual {actual_value!r}")
    if source.get("merge_commit_sha") is not None and pr.get("merge_commit_sha") is not None and source.get("merge_commit_sha") != pr.get("merge_commit_sha"):
        errors.append(
            "context.merge_commit_sha: "
            f"expected {source.get('merge_commit_sha')!r}, actual {pr.get('merge_commit_sha')!r}"
        )
    return errors


def score_fixture(expected: dict[str, Any], actual: dict[str, Any], evaluated_at: str) -> dict[str, Any]:
    expected_findings = [item for item in expected.get("expected_findings", []) if isinstance(item, dict)]
    actuals = [item for item in actual.get("findings", []) if isinstance(item, dict)]
    matches = match_expected_to_actuals(expected_findings, actuals)
    breakdown, counts = build_breakdown(expected_findings, actuals, matches)
    metrics = {
        "exact_pass_rate": rate(counts["exact_pass"], counts["axes_target"]),
        "acceptable_pass_rate": rate(counts["acceptable_pass"], counts["axes_target"]),
        "false_positive_rate": rate(counts["false_positive_promoted"], counts["known_false_positive_trap"], empty_value=0.0),
        "recall_known_bug": rate(counts["known_bug_matched"], counts["known_bug"]),
    }
    checks = gate_checks(expected.get("scoring_gate"), metrics)
    return {
        "schema_version": "score-report.v1",
        "fixture_id": expected.get("fixture_id"),
        "evaluated_at": evaluated_at,
        "oracle_sha256": canonical_sha256(expected),
        "expected_finding_ids": expected_finding_ids(expected_findings),
        **metrics,
        "gate_pass": all(check["passed"] for check in checks),
        "scoring_gate": report_scoring_gate(expected.get("scoring_gate")),
        "gate_checks": checks,
        "counts": counts,
        "unmatched_actuals": summarize_unmatched_actuals(actuals, matches),
        "breakdown": breakdown,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Score a fixture expected-findings oracle against findings.verified.json")
    parser.add_argument("--expected", required=True, type=Path, help="fixtures/<size>/expected-findings.json")
    parser.add_argument("--actual", required=True, type=Path, help="findings.verified.json to score")
    parser.add_argument("--out", required=True, type=Path, help="output score-report.v1 JSON path")
    parser.add_argument("--evaluated-at", default=utc_now(), help="RFC3339 timestamp for deterministic tests")
    parser.add_argument("--expected-schema", type=Path, default=ROOT / "schemas" / "expected-findings.v1.json")
    parser.add_argument("--findings-schema", type=Path, default=ROOT / "schemas" / "findings.v1.json")
    parser.add_argument("--score-schema", type=Path, default=ROOT / "schemas" / "score-report.v1.json")
    args = parser.parse_args()

    try:
        expected_schema = load_json(args.expected_schema)
        findings_schema = load_json(args.findings_schema)
        score_schema = load_json(args.score_schema)
        expected = load_json(args.expected)
        actual = load_json(args.actual)
    except ValueError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    if not isinstance(expected_schema, dict) or expected_schema.get("$id") is None:
        print(f"{args.expected_schema}: invalid expected-findings schema file", file=sys.stderr)
        return 2
    if not isinstance(findings_schema, dict) or findings_schema.get("$id") is None:
        print(f"{args.findings_schema}: invalid findings schema file", file=sys.stderr)
        return 2
    if not isinstance(score_schema, dict) or score_schema.get("$id") is None:
        print(f"{args.score_schema}: invalid score-report schema file", file=sys.stderr)
        return 2

    errors = [f"expected: {error}" for error in validate_expected_findings(expected)]
    errors.extend(f"actual: {error}" for error in validate_findings_artifact(findings_schema, actual))
    errors.extend(f"context: {error}" for error in validate_fixture_context(expected, actual))
    if errors:
        print("INVALID fixture scoring inputs", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    report = score_fixture(expected, actual, args.evaluated_at)
    report_errors = validate_score_report(report)
    if report_errors:
        print("INVALID generated score report", file=sys.stderr)
        for error in report_errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
