#!/usr/bin/env python3
"""Deterministic refinement-loop controller for /pr-codex:review.

The review skill still performs the actual reasoning in Claude's main context,
but this module defines the stable halting semantics used by documentation,
artifacts, and tests.  It intentionally has no network or model dependency.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "review-rounds.v1"
HALT_REASONS = {
    None,
    "max_rounds",
    "time_budget",
    "no_new_evidence",
    "repeated_contradiction",
    "all_candidates_verified",
    "no_active_candidates",
}
REJECTED_REASONS = {
    "verifier_fail",
    "repeated_contradiction",
    "insufficient_evidence",
    "out_of_scope",
    "duplicate",
    "no_new_evidence",
}
SENSITIVE_KEY_FRAGMENTS = (
    "raw",
    "log",
    "secret",
    "token",
    "password",
    "authorization",
    "api_key",
    "private_key",
)


@dataclass(frozen=True)
class HaltingPolicy:
    max_rounds: int = 3
    time_budget_ms: int = 1_200_000
    no_new_evidence_rounds: int = 1
    repeated_contradiction_limit: int = 2
    verifier_fail_policy: str = "local_artifact_only"
    insufficient_evidence_policy: str = "suppress_to_local_artifact"

    @classmethod
    def from_mapping(cls, value: dict[str, Any] | None) -> "HaltingPolicy":
        if value is None:
            return cls()
        return cls(
            max_rounds=int(value.get("max_rounds", cls.max_rounds)),
            time_budget_ms=int(value.get("time_budget_ms", cls.time_budget_ms)),
            no_new_evidence_rounds=int(value.get("no_new_evidence_rounds", cls.no_new_evidence_rounds)),
            repeated_contradiction_limit=int(
                value.get("repeated_contradiction_limit", cls.repeated_contradiction_limit)
            ),
            verifier_fail_policy=str(value.get("verifier_fail_policy", cls.verifier_fail_policy)),
            insufficient_evidence_policy=str(
                value.get("insufficient_evidence_policy", cls.insufficient_evidence_policy)
            ),
        )

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.max_rounds < 1:
            errors.append("policy.max_rounds must be >= 1")
        if self.time_budget_ms < 0:
            errors.append("policy.time_budget_ms must be >= 0")
        if self.no_new_evidence_rounds < 1:
            errors.append("policy.no_new_evidence_rounds must be >= 1")
        if self.repeated_contradiction_limit < 1:
            errors.append("policy.repeated_contradiction_limit must be >= 1")
        if self.verifier_fail_policy != "local_artifact_only":
            errors.append("policy.verifier_fail_policy must be local_artifact_only")
        if self.insufficient_evidence_policy != "suppress_to_local_artifact":
            errors.append("policy.insufficient_evidence_policy must be suppress_to_local_artifact")
        return errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_rounds": self.max_rounds,
            "time_budget_ms": self.time_budget_ms,
            "no_new_evidence_rounds": self.no_new_evidence_rounds,
            "repeated_contradiction_limit": self.repeated_contradiction_limit,
            "verifier_fail_policy": self.verifier_fail_policy,
            "insufficient_evidence_policy": self.insufficient_evidence_policy,
        }


@dataclass(frozen=True)
class HaltingDecision:
    should_halt: bool
    reason: str | None
    detail: str

    def to_dict(self, *, elapsed_ms: int, triggered_at_round: int) -> dict[str, Any]:
        return {
            "should_halt": self.should_halt,
            "reason": self.reason,
            "detail": self.detail,
            "elapsed_ms": elapsed_ms,
            "triggered_at_round": triggered_at_round,
        }


def _as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _round_new_evidence_count(round_result: dict[str, Any]) -> int:
    if "new_evidence_count" in round_result:
        return _as_int(round_result.get("new_evidence_count"))
    evidence = round_result.get("new_evidence")
    if isinstance(evidence, list):
        return len(evidence)
    return 0


def _trailing_no_new_evidence_rounds(rounds: list[dict[str, Any]]) -> int:
    count = 0
    for round_result in reversed(rounds):
        if _round_new_evidence_count(round_result) == 0:
            count += 1
        else:
            break
    return count


def _normalise_signature(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        for key in ("signature", "finding_id", "fingerprint", "id"):
            raw = value.get(key)
            if isinstance(raw, str) and raw.strip():
                return raw.strip().lower()
        path = str(value.get("path", "")).strip().lower()
        title = " ".join(str(value.get("title", "")).casefold().split())
        return f"{path}\x1f{title}" if path or title else ""
    return " ".join(str(value).casefold().split())


def contradiction_counts(rounds: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for round_result in rounds:
        values = round_result.get("contradiction_signatures", [])
        if not isinstance(values, list):
            continue
        for value in values:
            signature = _normalise_signature(value)
            if signature:
                counts[signature] = counts.get(signature, 0) + 1
    return counts


def evaluate_halting(
    policy: HaltingPolicy | dict[str, Any] | None,
    rounds: list[dict[str, Any]],
    *,
    elapsed_ms: int,
    active_candidates_count: int,
) -> HaltingDecision:
    """Evaluate whether the next refine/challenge/verify round should start.

    Precedence is deterministic: hard budget guards first, oscillation guard,
    terminal candidate states, then no-new-evidence.  This makes repeated
    contradiction and insufficient-evidence behavior reproducible across runs.
    """

    resolved_policy = policy if isinstance(policy, HaltingPolicy) else HaltingPolicy.from_mapping(policy)
    errors = resolved_policy.validate()
    if errors:
        raise ValueError("; ".join(errors))

    elapsed = max(0, int(elapsed_ms))
    active = max(0, int(active_candidates_count))
    rounds_completed = len(rounds)

    if elapsed >= resolved_policy.time_budget_ms:
        return HaltingDecision(True, "time_budget", "time budget exhausted before starting another round")
    if rounds_completed >= resolved_policy.max_rounds:
        return HaltingDecision(True, "max_rounds", "max rounds reached")

    repeated = [
        key for key, count in contradiction_counts(rounds).items() if count >= resolved_policy.repeated_contradiction_limit
    ]
    if repeated:
        return HaltingDecision(True, "repeated_contradiction", f"repeated contradiction signature: {sorted(repeated)[0]}")

    if active == 0:
        verifier_failures = sum(_as_int(round_result.get("verifier_fail_count")) for round_result in rounds)
        insufficient = sum(_as_int(round_result.get("insufficient_evidence_count")) for round_result in rounds)
        if verifier_failures == 0 and insufficient == 0:
            return HaltingDecision(True, "all_candidates_verified", "no active candidates remain after verification")
        return HaltingDecision(True, "no_active_candidates", "all remaining candidates were suppressed locally")

    trailing_empty = _trailing_no_new_evidence_rounds(rounds)
    if trailing_empty >= resolved_policy.no_new_evidence_rounds:
        return HaltingDecision(True, "no_new_evidence", "no new evidence was found in the latest round(s)")

    return HaltingDecision(False, None, "continue")


def _is_sensitive_key(key: str) -> bool:
    lowered = key.casefold()
    return any(fragment in lowered for fragment in SENSITIVE_KEY_FRAGMENTS)


def sanitize_local_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    """Return a sanitized local-only rejected-candidate record.

    Round artifacts must explain why a candidate was not posted, without keeping
    raw logs or secret-bearing fields.  Unknown fields are dropped by design.
    """

    allowed = {"finding_id", "fingerprint", "title", "path", "line", "reason", "detail"}
    sanitized: dict[str, Any] = {"local_only": True}
    for key in allowed:
        if key not in candidate or _is_sensitive_key(key):
            continue
        value = candidate[key]
        if isinstance(value, str):
            value = " ".join(value.split())[:500]
        sanitized[key] = value
    reason = sanitized.get("reason")
    if reason not in REJECTED_REASONS:
        sanitized["reason"] = "verifier_fail"
    if "title" not in sanitized:
        sanitized["title"] = "suppressed candidate"
    return sanitized


def sanitize_round_result(round_result: dict[str, Any], *, round_index: int) -> dict[str, Any]:
    rejected = round_result.get("rejected_candidates", [])
    if not isinstance(rejected, list):
        rejected = []
    actions = round_result.get("actions", ["refine", "challenge", "verify"])
    if not isinstance(actions, list) or not actions:
        actions = ["refine", "challenge", "verify"]
    return {
        "round_index": _as_int(round_result.get("round_index"), round_index) or round_index,
        "actions": [str(action) for action in actions],
        "input_candidates_count": max(0, _as_int(round_result.get("input_candidates_count"))),
        "output_candidates_count": max(0, _as_int(round_result.get("output_candidates_count"))),
        "new_evidence_count": max(0, _round_new_evidence_count(round_result)),
        "verifier_pass_count": max(0, _as_int(round_result.get("verifier_pass_count"))),
        "verifier_fail_count": max(0, _as_int(round_result.get("verifier_fail_count"))),
        "insufficient_evidence_count": max(0, _as_int(round_result.get("insufficient_evidence_count"))),
        "contradiction_signatures": [
            signature
            for signature in (_normalise_signature(item) for item in round_result.get("contradiction_signatures", []))
            if signature
        ],
        "rejected_candidates": [sanitize_local_candidate(item) for item in rejected if isinstance(item, dict)],
    }


def build_review_rounds_artifact(
    *,
    policy: HaltingPolicy | dict[str, Any] | None = None,
    rounds: list[dict[str, Any]],
    elapsed_ms: int,
    active_candidates_count: int,
    generated_at: str | None = None,
    pr: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resolved_policy = policy if isinstance(policy, HaltingPolicy) else HaltingPolicy.from_mapping(policy)
    sanitized_rounds = [sanitize_round_result(round_result, round_index=index + 1) for index, round_result in enumerate(rounds)]
    decision = evaluate_halting(
        resolved_policy,
        sanitized_rounds,
        elapsed_ms=elapsed_ms,
        active_candidates_count=active_candidates_count,
    )
    metrics = round_metrics(sanitized_rounds, active_candidates_count=active_candidates_count)
    artifact: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "policy": resolved_policy.to_dict(),
        "rounds": sanitized_rounds,
        "halting": decision.to_dict(elapsed_ms=max(0, int(elapsed_ms)), triggered_at_round=len(sanitized_rounds)),
        "metrics": metrics,
    }
    if pr is not None:
        artifact["pr"] = copy.deepcopy(pr)
    return artifact


def round_metrics(rounds: list[dict[str, Any]], *, active_candidates_count: int) -> dict[str, Any]:
    verifier_failures = sum(_as_int(round_result.get("verifier_fail_count")) for round_result in rounds)
    insufficient = sum(_as_int(round_result.get("insufficient_evidence_count")) for round_result in rounds)
    rejected = sum(len(round_result.get("rejected_candidates", [])) for round_result in rounds)
    repeated_events = sum(
        max(0, count - 1)
        for count in contradiction_counts(rounds).values()
        if count > 1
    )
    no_new_evidence_rounds = sum(1 for round_result in rounds if _round_new_evidence_count(round_result) == 0)
    suppressed = max(rejected, verifier_failures + insufficient)
    return {
        "total_rounds": len(rounds),
        "posted_candidate_count": max(0, int(active_candidates_count)),
        "verifier_fail_candidates": verifier_failures,
        "suppressed_candidate_count": suppressed,
        "no_new_evidence_rounds": no_new_evidence_rounds,
        "repeated_contradiction_events": repeated_events,
        "insufficient_evidence_events": insufficient,
        "oscillation_detected": repeated_events > 0,
    }


def rejected_candidate_ids(artifact: dict[str, Any]) -> set[str]:
    rejected: set[str] = set()
    for round_result in artifact.get("rounds", []):
        if not isinstance(round_result, dict):
            continue
        for candidate in round_result.get("rejected_candidates", []):
            if not isinstance(candidate, dict):
                continue
            for key in ("finding_id", "fingerprint"):
                value = candidate.get(key)
                if isinstance(value, str) and value:
                    rejected.add(value)
    return rejected


def filter_postable_findings(findings: list[dict[str, Any]], artifact: dict[str, Any]) -> list[dict[str, Any]]:
    """Remove verifier-failed/local-only candidates from publishable findings."""

    rejected = rejected_candidate_ids(artifact)
    postable: list[dict[str, Any]] = []
    for finding in findings:
        identifiers = {value for value in (finding.get("id"), finding.get("fingerprint")) if isinstance(value, str)}
        if identifiers & rejected:
            continue
        postable.append(finding)
    return postable



JSON_TYPE_NAMES = {"object", "array", "integer", "number", "string", "boolean", "null"}


def _json_type_matches(expected: str, value: Any) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    raise ValueError(f"unsupported JSON schema type: {expected}")


def _resolve_schema_ref(root_schema: dict[str, Any], ref: str) -> dict[str, Any]:
    if not ref.startswith("#/"):
        raise ValueError(f"unsupported schema ref: {ref}")
    current: Any = root_schema
    for raw_part in ref[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or part not in current:
            raise ValueError(f"unresolvable schema ref: {ref}")
        current = current[part]
    if not isinstance(current, dict):
        raise ValueError(f"schema ref does not point to an object: {ref}")
    return current


def _is_rfc3339_datetime(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return False
    return parsed.tzinfo is not None


def validate_json_schema_subset(
    value: Any,
    schema: dict[str, Any],
    *,
    root_schema: dict[str, Any] | None = None,
    path: str = "$",
) -> list[str]:
    """Validate the Draft 2020-12 subset used by review-rounds.v1.

    This is intentionally small and stdlib-only, but it covers the declared
    runtime contract: $ref, type, const, enum, required, properties,
    additionalProperties=false, items, minItems, minLength, minimum, pattern,
    and date-time format.  It prevents the bundled runtime gate from drifting
    from schemas/review-rounds.v1.json.
    """

    errors: list[str] = []
    root = root_schema or schema

    if "$ref" in schema:
        try:
            resolved = _resolve_schema_ref(root, str(schema["$ref"]))
        except ValueError as exc:
            return [f"{path}: {exc}"]
        errors.extend(validate_json_schema_subset(value, resolved, root_schema=root, path=path))
        schema = {key: item for key, item in schema.items() if key != "$ref"}
        if not schema:
            return errors

    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: expected const {schema['const']!r}")

    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: expected one of {schema['enum']!r}")

    expected_type = schema.get("type")
    if expected_type is not None:
        types = expected_type if isinstance(expected_type, list) else [expected_type]
        if not all(isinstance(item, str) and item in JSON_TYPE_NAMES for item in types):
            errors.append(f"{path}: unsupported schema type declaration {expected_type!r}")
        elif not any(_json_type_matches(item, value) for item in types):
            errors.append(f"{path}: expected type {expected_type!r}, got {type(value).__name__}")
            return errors

    if value is None:
        return errors

    if isinstance(value, str):
        if "minLength" in schema and len(value) < int(schema["minLength"]):
            errors.append(f"{path}: expected length >= {schema['minLength']}")
        if "pattern" in schema:
            try:
                if re.search(str(schema["pattern"]), value) is None:
                    errors.append(f"{path}: does not match pattern {schema['pattern']!r}")
            except re.error as exc:
                errors.append(f"{path}: invalid schema pattern: {exc}")
        if schema.get("format") == "date-time" and not _is_rfc3339_datetime(value):
            errors.append(f"{path}: must be RFC3339 date-time")

    if isinstance(value, (int, float)) and not isinstance(value, bool) and "minimum" in schema:
        if value < schema["minimum"]:
            errors.append(f"{path}: expected >= {schema['minimum']}")

    if isinstance(value, list):
        if "minItems" in schema and len(value) < int(schema["minItems"]):
            errors.append(f"{path}: expected at least {schema['minItems']} items")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                errors.extend(validate_json_schema_subset(item, item_schema, root_schema=root, path=f"{path}[{index}]"))
        return errors

    if isinstance(value, dict):
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        required = schema.get("required") if isinstance(schema.get("required"), list) else []
        missing = [key for key in required if isinstance(key, str) and key not in value]
        if missing:
            errors.append(f"{path}: missing required properties: {', '.join(missing)}")
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(properties))
            if extra:
                errors.append(f"{path}: unexpected properties: {', '.join(extra)}")
        for key, child_schema in properties.items():
            if key in value and isinstance(child_schema, dict):
                errors.extend(validate_json_schema_subset(value[key], child_schema, root_schema=root, path=f"{path}.{key}"))

    return errors


def validate_review_rounds_artifact(data: dict[str, Any], schema: dict[str, Any] | None = None) -> list[str]:
    errors: list[str] = []
    if schema is not None:
        errors.extend(validate_json_schema_subset(data, schema))
    if not isinstance(data, dict):
        return errors + ["artifact must be an object"]
    if data.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"$.schema_version: must be {SCHEMA_VERSION}")
    policy_raw = data.get("policy")
    if not isinstance(policy_raw, dict):
        errors.append("$.policy: must be an object")
    else:
        try:
            errors.extend(HaltingPolicy.from_mapping(policy_raw).validate())
        except (TypeError, ValueError) as exc:
            errors.append(f"$.policy: invalid halting policy: {exc}")
    rounds = data.get("rounds")
    if not isinstance(rounds, list):
        errors.append("$.rounds: must be an array")
        rounds = []
    for index, round_result in enumerate(rounds):
        path = f"$.rounds[{index}]"
        if not isinstance(round_result, dict):
            errors.append(f"{path}: must be an object")
            continue
        allowed_round_keys = {
            "round_index",
            "actions",
            "input_candidates_count",
            "output_candidates_count",
            "new_evidence_count",
            "verifier_pass_count",
            "verifier_fail_count",
            "insufficient_evidence_count",
            "contradiction_signatures",
            "rejected_candidates",
        }
        extra_round_keys = sorted(set(round_result) - allowed_round_keys)
        if extra_round_keys:
            errors.append(f"{path}: unexpected properties: {', '.join(extra_round_keys)}")
        actions = round_result.get("actions")
        if (
            not isinstance(actions, list)
            or not actions
            or any(action not in {"refine", "challenge", "verify"} for action in actions)
        ):
            errors.append(f"{path}.actions: must contain refine/challenge/verify values")
        for key in (
            "round_index",
            "input_candidates_count",
            "output_candidates_count",
            "new_evidence_count",
            "verifier_pass_count",
            "verifier_fail_count",
            "insufficient_evidence_count",
        ):
            value = round_result.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < (1 if key == "round_index" else 0):
                errors.append(f"{path}.{key}: invalid integer")
        for candidate_index, candidate in enumerate(round_result.get("rejected_candidates", [])):
            cpath = f"{path}.rejected_candidates[{candidate_index}]"
            if not isinstance(candidate, dict):
                errors.append(f"{cpath}: must be an object")
                continue
            allowed_candidate_keys = {"local_only", "finding_id", "fingerprint", "title", "path", "line", "reason", "detail"}
            extra_candidate_keys = sorted(set(candidate) - allowed_candidate_keys)
            if extra_candidate_keys:
                errors.append(f"{cpath}: unexpected properties: {', '.join(extra_candidate_keys)}")
            if candidate.get("local_only") is not True:
                errors.append(f"{cpath}.local_only: must be true")
            if not isinstance(candidate.get("title"), str) or not candidate.get("title"):
                errors.append(f"{cpath}.title: must be non-empty")
            if candidate.get("reason") not in REJECTED_REASONS:
                errors.append(f"{cpath}.reason: invalid reason")
            for key in candidate:
                if _is_sensitive_key(key):
                    errors.append(f"{cpath}: sensitive/raw key is not allowed: {key}")
    halting = data.get("halting")
    if not isinstance(halting, dict):
        errors.append("$.halting: must be an object")
    else:
        reason = halting.get("reason")
        if reason not in HALT_REASONS:
            errors.append("$.halting.reason: invalid halt reason")
        if not isinstance(halting.get("should_halt"), bool):
            errors.append("$.halting.should_halt: must be a boolean")
    metrics = data.get("metrics")
    if not isinstance(metrics, dict):
        errors.append("$.metrics: must be an object")
    elif metrics.get("total_rounds") != len(rounds):
        errors.append("$.metrics.total_rounds: must equal len(rounds)")
    return errors


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build or validate review-rounds.v1 artifacts")
    parser.add_argument("--validate", type=Path, help="validate an existing review-rounds.v1 artifact")
    parser.add_argument("--emit-empty", action="store_true", help="emit an empty initialized artifact")
    parser.add_argument("--policy", type=Path, help="policy JSON used with --emit-empty")
    args = parser.parse_args(argv)

    if args.validate:
        try:
            data = load_json(args.validate)
        except Exception as exc:  # noqa: BLE001 - CLI should report parse errors uniformly
            print(f"INVALID review rounds artifact: cannot read JSON: {exc}", file=sys.stderr)
            return 1
        errors = validate_review_rounds_artifact(data)
        if errors:
            print("INVALID review rounds artifact", file=sys.stderr)
            for error in errors:
                print(f"- {error}", file=sys.stderr)
            return 1
        print("review rounds artifact valid")
        return 0

    if args.emit_empty:
        policy = load_json(args.policy) if args.policy else None
        print(json.dumps(build_review_rounds_artifact(policy=policy, rounds=[], elapsed_ms=0, active_candidates_count=0), indent=2))
        return 0

    parser.error("one of --validate or --emit-empty is required")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
