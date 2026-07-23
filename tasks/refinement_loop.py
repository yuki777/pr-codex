#!/usr/bin/env python3
"""Deterministic refinement-loop controller for /pr-codex:review.

The review skill still performs the actual reasoning in Claude's main context,
but this module defines the stable halting semantics used by documentation,
artifacts, and tests.  It intentionally has no network or model dependency.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
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
REDACTED_SENSITIVE_VALUE = "[redacted sensitive review-round content]"
REDACTED_IDENTIFIER_PREFIX = "redacted-id-sha256:"
STATE_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
SENSITIVE_VALUE_PATTERNS = (
    ("raw-log marker", re.compile(r"\braw[\s_-]?logs?\b", re.IGNORECASE)),
    (
        "authorization header",
        re.compile(r"\bauthorization\s*[:=]\s*\S+(?:\s+\S+)?", re.IGNORECASE),
    ),
    (
        "credential assignment",
        re.compile(
            r"\b[a-z0-9_-]*(?:api[_-]?key|token|secret|password)[a-z0-9_-]*\s*[:=]\s*['\"]?\S+",
            re.IGNORECASE,
        ),
    ),
    ("private key header", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", re.IGNORECASE)),
)


@dataclass(frozen=True)
class HaltingPolicy:
    max_rounds: int = 2
    time_budget_ms: int = 1_200_000
    no_new_evidence_rounds: int = 1
    repeated_contradiction_limit: int = 2
    verifier_fail_policy: str = "local_artifact_only"
    insufficient_evidence_policy: str = "suppress_to_local_artifact"

    @classmethod
    def from_mapping(cls, value: dict[str, Any] | None) -> "HaltingPolicy":
        if value is None:
            return cls()
        review_loop = value.get("review_loop")
        if isinstance(review_loop, dict):
            value = review_loop
        halting_policy = value.get("halting_policy")
        if isinstance(halting_policy, dict):
            value = halting_policy
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
        if self.max_rounds > 3:
            errors.append("policy.max_rounds must be <= 3")
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

def _candidate_identifier(candidate: dict[str, Any]) -> str:
    for key in ("candidate_id", "fingerprint", "id", "finding_id"):
        value = candidate.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    fallback = {
        "path": candidate.get("path")
        or (candidate.get("location") or {}).get("path")
        if isinstance(candidate.get("location"), dict)
        else candidate.get("path"),
        "line": candidate.get("start_line")
        or (candidate.get("location") or {}).get("start_line")
        if isinstance(candidate.get("location"), dict)
        else candidate.get("start_line"),
        "title": candidate.get("title"),
    }
    material = json.dumps(fallback, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"anonymous:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


def _candidate_axes(candidate: dict[str, Any]) -> dict[str, Any]:
    axes = candidate.get("axes")
    if not isinstance(axes, dict):
        axes = candidate.get("axes_suggestion")
    return axes if isinstance(axes, dict) else {}


def _candidate_state(candidate: dict[str, Any]) -> dict[str, Any]:
    axes = _candidate_axes(candidate)
    raw_location = candidate.get("location")
    if isinstance(raw_location, dict):
        location = {
            key: raw_location.get(key)
            for key in ("path", "start_line", "end_line", "side")
        }
    else:
        location = {
            "path": candidate.get("path"),
            "start_line": candidate.get("start_line"),
            "end_line": candidate.get("end_line"),
            "side": candidate.get("side"),
        }
    return {
        "id": _candidate_identifier(candidate),
        "evidence_state": candidate.get("evidence_state"),
        "evidence_level": candidate.get("evidence_level")
        or candidate.get("evidence_level_suggestion"),
        "axes": {
            key: axes.get(key)
            for key in ("real", "triggerable", "impactful")
        },
        "location": location,
        "category": candidate.get("category")
        or candidate.get("category_raw")
        or candidate.get("category_suggestion"),
        "severity": candidate.get("severity")
        or candidate.get("severity_raw")
        or candidate.get("severity_suggestion"),
        "decision": candidate.get("decision"),
        "disagreement": bool(
            candidate.get("disagreement")
            or candidate.get("severity_disputed")
            or candidate.get("contradiction")
        ),
    }


def candidate_state_digest(candidates: list[dict[str, Any]]) -> str:
    """Hash only host-owned candidate state, never model prose."""

    states = sorted(
        (_candidate_state(candidate) for candidate in candidates if isinstance(candidate, dict)),
        key=lambda state: state["id"],
    )
    material = json.dumps(states, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def candidate_state_delta(
    before: list[dict[str, Any]], after: list[dict[str, Any]]
) -> dict[str, Any]:
    """Compute round state transitions from candidate snapshots on the host."""

    before_states = {
        state["id"]: state
        for state in (
            _candidate_state(candidate)
            for candidate in before
            if isinstance(candidate, dict)
        )
    }
    after_states = {
        state["id"]: state
        for state in (
            _candidate_state(candidate)
            for candidate in after
            if isinstance(candidate, dict)
        )
    }
    changed_ids = sorted(
        candidate_id
        for candidate_id in set(before_states) | set(after_states)
        if before_states.get(candidate_id) != after_states.get(candidate_id)
    )

    def evidence_progressed(
        before_state: dict[str, Any] | None,
        after_state: dict[str, Any] | None,
    ) -> bool:
        if after_state is None:
            return False
        if before_state is None:
            return True
        evidence_state_rank = {"needs_evidence": 0, "supported": 1}
        evidence_level_rank = {
            None: -1,
            "suspicion": 0,
            "corroborated": 1,
            "trigger_path_identified": 2,
            "impact_explained": 3,
            "verified": 4,
        }
        before_axes = before_state.get("axes")
        after_axes = after_state.get("axes")
        before_known_axes = sum(
            value in {"yes", "no"}
            for value in before_axes.values()
        ) if isinstance(before_axes, dict) else 0
        after_known_axes = sum(
            value in {"yes", "no"}
            for value in after_axes.values()
        ) if isinstance(after_axes, dict) else 0
        return (
            evidence_state_rank.get(after_state.get("evidence_state"), -1)
            > evidence_state_rank.get(before_state.get("evidence_state"), -1)
            or evidence_level_rank.get(after_state.get("evidence_level"), -1)
            > evidence_level_rank.get(before_state.get("evidence_level"), -1)
            or after_known_axes > before_known_axes
        )

    def disposition_state(state: dict[str, Any] | None) -> tuple[Any, Any]:
        if state is None:
            return None, None
        return state.get("decision"), state.get("disagreement")

    return {
        "state_digest_before": candidate_state_digest(before),
        "state_digest_after": candidate_state_digest(after),
        "changed_candidate_ids": changed_ids,
        "changed_candidate_count": len(changed_ids),
        "evidence_added_count": sum(
            evidence_progressed(
                before_states.get(candidate_id),
                after_states.get(candidate_id),
            )
            for candidate_id in changed_ids
        ),
        "disposition_changed_count": sum(
            disposition_state(before_states.get(candidate_id))
            != disposition_state(after_states.get(candidate_id))
            for candidate_id in changed_ids
        ),
        "remaining_active_count": sum(
            _candidate_requires_refinement(candidate)
            for candidate in after
            if isinstance(candidate, dict)
        ),
    }


def auto_deep_eligible(
    candidates: list[dict[str, Any]], run_plan: dict[str, Any] | None
) -> bool:
    """Allow automatic deep mode only for small, fully resolved initial state."""

    if not candidates or not isinstance(run_plan, dict):
        return False
    if (
        run_plan.get("recommended_mode") != "standard"
        or run_plan.get("depth_actual") != "standard"
        or run_plan.get("depth_source") != "default"
    ):
        return False
    routing = run_plan.get("routing_decision")
    if not isinstance(routing, dict) or routing.get("budget_class") != "small":
        return False

    severities_by_location: dict[tuple[str, int], set[str]] = {}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            return False
        if candidate.get("decision") not in {None, "verified"}:
            return False
        if candidate.get("evidence_state") != "supported":
            return False
        evidence_level = candidate.get("evidence_level") or candidate.get(
            "evidence_level_suggestion"
        )
        if evidence_level != "verified":
            return False
        axes = _candidate_axes(candidate)
        if any(axes.get(key) not in {"yes", "no"} for key in ("real", "triggerable", "impactful")):
            return False
        if (
            candidate.get("disagreement")
            or candidate.get("severity_disputed")
            or candidate.get("contradiction")
        ):
            return False
        location = candidate.get("location")
        if isinstance(location, dict):
            path = location.get("path")
            line = location.get("start_line")
        else:
            path = candidate.get("path")
            line = candidate.get("start_line")
        severity = (
            candidate.get("severity")
            or candidate.get("severity_raw")
            or candidate.get("severity_suggestion")
        )
        if (
            not isinstance(path, str)
            or not path
            or not isinstance(line, int)
            or isinstance(line, bool)
            or line < 1
            or not isinstance(severity, str)
            or not severity
        ):
            return False
        severities_by_location.setdefault((path, line), set()).add(severity)
    return all(len(severities) == 1 for severities in severities_by_location.values())

def apply_auto_deep(
    run_plan: dict[str, Any],
    controller_plan: dict[str, Any],
) -> dict[str, Any]:
    """Apply the controller's round-1 auto-deep decision without model discretion."""

    if (
        controller_plan.get("round_index") != 1
        or controller_plan.get("auto_deep_eligible") is not True
    ):
        raise ValueError("controller plan does not authorize round-1 auto-deep")
    if (
        run_plan.get("depth_actual") != "standard"
        or run_plan.get("depth_source") != "default"
        or run_plan.get("depth_requested") is not None
        or run_plan.get("depth_downgraded") is not False
        or run_plan.get("depth_downgrade_reason") is not None
    ):
        raise ValueError("auto-deep requires an initial standard run plan")
    if run_plan.get("recommended_mode") != "standard":
        raise ValueError("auto-deep requires recommended_mode=standard")
    routing = run_plan.get("routing_decision")
    if not isinstance(routing, dict) or routing.get("budget_class") != "small":
        raise ValueError("auto-deep requires a small routing budget")
    risk_tags = run_plan.get("risk_tags")
    if not isinstance(risk_tags, list) or not all(isinstance(tag, str) for tag in risk_tags):
        raise ValueError("run plan risk_tags must be a string array")
    try:
        files_changed = int(run_plan["files_changed"])
        total_lines = int(run_plan["lines_added"]) + int(run_plan["lines_removed"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("run plan size fields must be integers") from exc

    updated = copy.deepcopy(run_plan)
    updated["depth_actual"] = "deep"
    updated["depth_source"] = "auto"
    updated["depth_reason"] = (
        "automatic deep after initial candidate gate: small fully resolved candidate set"
    )
    updated["depth_requested"] = None
    updated["depth_downgraded"] = False
    updated["depth_downgrade_reason"] = None
    updated_routing = updated["routing_decision"]
    updated_routing["model_profile"] = "deep"
    updated_routing["rationale"] = (
        f"files_changed={files_changed}, total_lines={total_lines}, "
        f"risk_tags=[{','.join(risk_tags)}], depth=deep, mode=standard"
    )
    review_loop = updated.get("review_loop")
    if isinstance(review_loop, dict):
        halting_policy = review_loop.get("halting_policy")
        if isinstance(halting_policy, dict):
            halting_policy["max_rounds"] = 3
    return updated


def _candidate_requires_refinement(candidate: dict[str, Any]) -> bool:
    decision = candidate.get("decision")
    if decision in {"verified", "refuted", "suppressed"}:
        return False
    if (
        candidate.get("disagreement")
        or candidate.get("severity_disputed")
        or candidate.get("contradiction")
    ):
        return True
    if candidate.get("evidence_state") != "supported":
        return True
    evidence_level = candidate.get("evidence_level") or candidate.get(
        "evidence_level_suggestion"
    )
    if evidence_level != "verified":
        return True
    axes = _candidate_axes(candidate)
    return any(axes.get(key) not in {"yes", "no"} for key in ("real", "triggerable", "impactful"))


def _normalise_candidate_label(value: Any) -> str:
    return str(value or "").strip().casefold().replace("-", "_").replace(" ", "_")


def _candidate_has_disagreement(candidate: dict[str, Any]) -> bool:
    return bool(
        candidate.get("disagreement")
        or candidate.get("severity_disputed")
        or candidate.get("contradiction")
    )


def _candidate_is_high_risk(
    candidate: dict[str, Any], run_plan: dict[str, Any] | None = None
) -> bool:
    severity = _normalise_candidate_label(
        candidate.get("severity")
        or candidate.get("severity_raw")
        or candidate.get("severity_suggestion")
    )
    category = _normalise_candidate_label(
        candidate.get("category")
        or candidate.get("category_raw")
        or candidate.get("category_suggestion")
    )
    blast_radius = _normalise_candidate_label(candidate.get("blast_radius"))
    risk_tags = {
        _normalise_candidate_label(tag)
        for tag in (run_plan or {}).get("risk_tags", [])
        if isinstance(tag, str)
    }
    return (
        severity == "must_fix"
        or category in {"security", "runtime_error"}
        or blast_radius == "systemic"
        or bool(risk_tags & {"security", "data_migration"})
    )


def _candidate_is_round_two_priority(
    candidate: dict[str, Any], run_plan: dict[str, Any] | None = None
) -> bool:
    return (
        _candidate_is_high_risk(candidate, run_plan)
        or _candidate_has_disagreement(candidate)
        or candidate.get("evidence_state") == "needs_evidence"
    )


def select_round_targets(
    candidates: list[dict[str, Any]],
    *,
    round_index: int,
    changed_candidate_ids: set[str] | None = None,
    run_plan: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Apply the deterministic Round 1/2/3 narrowing policy."""

    typed_candidates = [
        candidate for candidate in candidates if isinstance(candidate, dict)
    ]
    if round_index <= 1:
        return typed_candidates
    unresolved = [
        candidate
        for candidate in typed_candidates
        if _candidate_requires_refinement(candidate)
    ]
    if round_index == 2:
        return [
            candidate
            for candidate in unresolved
            if _candidate_is_round_two_priority(candidate, run_plan)
        ]
    if round_index == 3:
        changed_ids = changed_candidate_ids or set()
        return [
            candidate
            for candidate in unresolved
            if _candidate_identifier(candidate) in changed_ids
            and _candidate_is_high_risk(candidate, run_plan)
        ]
    return []


def plan_next_round(
    policy: HaltingPolicy | dict[str, Any] | None,
    *,
    rounds: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    elapsed_ms: int,
    run_plan: dict[str, Any] | None = None,
    previous_candidates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    round_index = len(rounds) + 1
    state_digest = candidate_state_digest(candidates)
    evaluation_rounds = copy.deepcopy(rounds)
    round_state: dict[str, Any] | None = None
    previous_changed_ids: set[str] = set()
    if previous_candidates is not None:
        if not evaluation_rounds:
            raise ValueError("previous candidates require at least one completed round")
        round_state = candidate_state_delta(previous_candidates, candidates)
        latest = evaluation_rounds[-1]
        supplied_before = latest.get("state_digest_before")
        if (
            isinstance(supplied_before, str)
            and supplied_before != round_state["state_digest_before"]
        ):
            raise ValueError(
                "previous candidate snapshot does not match the latest round start digest"
            )
        latest.update(round_state)
        latest["new_evidence_count"] = round_state["evidence_added_count"]
        previous_changed_ids = set(round_state["changed_candidate_ids"])
    elif evaluation_rounds:
        latest = evaluation_rounds[-1]
        raw_changed_ids = latest.get("changed_candidate_ids")
        if isinstance(raw_changed_ids, list):
            previous_changed_ids = {
                value for value in raw_changed_ids if isinstance(value, str) and value
            }
        if isinstance(latest.get("state_digest_before"), str):
            latest["state_digest_after"] = state_digest
        round_state = {
            key: latest[key]
            for key in (
                "state_digest_before",
                "state_digest_after",
                "changed_candidate_ids",
                "changed_candidate_count",
                "evidence_added_count",
                "disposition_changed_count",
                "remaining_active_count",
            )
            if key in latest
        }
    targets = select_round_targets(
        candidates,
        round_index=round_index,
        changed_candidate_ids=previous_changed_ids,
        run_plan=run_plan,
    )
    decision = evaluate_halting(
        policy,
        evaluation_rounds,
        elapsed_ms=elapsed_ms,
        active_candidates_count=len(targets),
    )
    return {
        "round_index": round_index,
        "should_run": not decision.should_halt,
        "actions": ["refine", "challenge", "verify"] if not decision.should_halt else [],
        "target_candidate_ids": [_candidate_identifier(candidate) for candidate in targets],
        "state_digest": state_digest,
        "round_state": round_state,
        "auto_deep_eligible": auto_deep_eligible(candidates, run_plan),
        "halting": decision.to_dict(
            elapsed_ms=max(0, int(elapsed_ms)),
            triggered_at_round=len(rounds),
        ),
    }




def _round_new_evidence_count(round_result: dict[str, Any]) -> int:
    if "evidence_added_count" in round_result:
        return _as_int(round_result.get("evidence_added_count"))
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
        return f"{path} :: {title}" if path or title else ""
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
    if rounds:
        before = rounds[-1].get("state_digest_before")
        after = rounds[-1].get("state_digest_after")
        if (
            isinstance(before, str)
            and isinstance(after, str)
            and before
            and before == after
        ):
            return HaltingDecision(
                True,
                "no_new_evidence",
                "candidate state digest did not change in the latest round",
            )

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


def _sensitive_value_reason(value: str) -> str | None:
    return next((reason for reason, pattern in SENSITIVE_VALUE_PATTERNS if pattern.search(value)), None)


def _normalise_artifact_string(value: str) -> str:
    return " ".join(value.split())[:500]


def _redacted_identifier(value: str) -> str:
    return f"{REDACTED_IDENTIFIER_PREFIX}{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _safe_identifier_value(value: str) -> str:
    normalized = _normalise_artifact_string(value)
    if _sensitive_value_reason(normalized) is not None:
        return _redacted_identifier(normalized)
    return normalized


def _redact_sensitive_value(value: str) -> str:
    normalized = _normalise_artifact_string(value)
    if _sensitive_value_reason(normalized) is not None:
        return REDACTED_SENSITIVE_VALUE
    return normalized


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
            value = (
                _safe_identifier_value(value)
                if key in {"finding_id", "fingerprint"}
                else _redact_sensitive_value(value)
            )
        sanitized[key] = value
    if "finding_id" not in sanitized and isinstance(sanitized.get("fingerprint"), str) and sanitized["fingerprint"]:
        sanitized["finding_id"] = sanitized["fingerprint"]
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
    sanitized = {
        "round_index": _as_int(round_result.get("round_index"), round_index) or round_index,
        "actions": [str(action) for action in actions],
        "input_candidates_count": max(0, _as_int(round_result.get("input_candidates_count"))),
        "output_candidates_count": max(0, _as_int(round_result.get("output_candidates_count"))),
        "new_evidence_count": max(0, _round_new_evidence_count(round_result)),
        "verifier_pass_count": max(0, _as_int(round_result.get("verifier_pass_count"))),
        "verifier_fail_count": max(0, _as_int(round_result.get("verifier_fail_count"))),
        "insufficient_evidence_count": max(0, _as_int(round_result.get("insufficient_evidence_count"))),
        "contradiction_signatures": [
            _redact_sensitive_value(signature)
            for signature in (_normalise_signature(item) for item in round_result.get("contradiction_signatures", []))
            if signature
        ],
        "rejected_candidates": [sanitize_local_candidate(item) for item in rejected if isinstance(item, dict)],
    }
    for key in ("target_candidate_ids", "changed_candidate_ids"):
        values = round_result.get(key)
        if isinstance(values, list):
            sanitized[key] = [
                _safe_identifier_value(value)
                for value in values
                if isinstance(value, str) and value
            ]
    for key in ("state_digest_before", "state_digest_after"):
        value = round_result.get(key)
        if isinstance(value, str):
            sanitized[key] = value.strip().casefold()
    for key in (
        "changed_candidate_count",
        "evidence_added_count",
        "disposition_changed_count",
        "remaining_active_count",
    ):
        if key in round_result:
            sanitized[key] = max(0, _as_int(round_result.get(key)))
    return sanitized


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
    changed_candidates = sum(
        _as_int(round_result.get("changed_candidate_count"))
        for round_result in rounds
    )
    evidence_added = sum(
        _as_int(round_result.get("evidence_added_count"))
        for round_result in rounds
    )
    disposition_changed = sum(
        _as_int(round_result.get("disposition_changed_count"))
        for round_result in rounds
    )
    remaining_active = max(0, int(active_candidates_count))
    if rounds and "remaining_active_count" in rounds[-1]:
        remaining_active = max(
            0,
            _as_int(rounds[-1].get("remaining_active_count")),
        )
    return {
        "total_rounds": len(rounds),
        "posted_candidate_count": max(0, int(active_candidates_count)),
        "verifier_fail_candidates": verifier_failures,
        "suppressed_candidate_count": suppressed,
        "no_new_evidence_rounds": no_new_evidence_rounds,
        "repeated_contradiction_events": repeated_events,
        "insufficient_evidence_events": insufficient,
        "changed_candidate_count": changed_candidates,
        "evidence_added_count": evidence_added,
        "disposition_changed_count": disposition_changed,
        "remaining_active_count": remaining_active,
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


def _finding_identifier_values(finding: dict[str, Any]) -> set[str]:
    identifiers: set[str] = set()
    for value in (finding.get("id"), finding.get("fingerprint")):
        if isinstance(value, str) and value:
            identifiers.add(_safe_identifier_value(value))
    return identifiers


def filter_postable_findings(findings: list[dict[str, Any]], artifact: dict[str, Any]) -> list[dict[str, Any]]:
    """Remove verifier-failed/local-only candidates from publishable findings."""

    rejected = rejected_candidate_ids(artifact)
    postable: list[dict[str, Any]] = []
    for finding in findings:
        identifiers = _finding_identifier_values(finding)
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
    previous_state_digest_after: str | None = None
    previous_target_ids: set[str] | None = None
    previous_changed_ids: set[str] | None = None
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
            "target_candidate_ids",
            "state_digest_before",
            "state_digest_after",
            "changed_candidate_ids",
            "changed_candidate_count",
            "evidence_added_count",
            "disposition_changed_count",
            "remaining_active_count",
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
        for key in (
            "changed_candidate_count",
            "evidence_added_count",
            "disposition_changed_count",
            "remaining_active_count",
        ):
            if key not in round_result:
                continue
            value = round_result.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                errors.append(f"{path}.{key}: invalid integer")
        target_candidate_ids = round_result.get("target_candidate_ids")
        if target_candidate_ids is not None:
            if (
                not isinstance(target_candidate_ids, list)
                or any(not isinstance(value, str) or not value for value in target_candidate_ids)
            ):
                errors.append(f"{path}.target_candidate_ids: must be an array of non-empty strings")
            else:
                target_ids = set(target_candidate_ids)
                if len(target_ids) != len(target_candidate_ids):
                    errors.append(f"{path}.target_candidate_ids: must contain unique values")
                if round_result.get("input_candidates_count") != len(target_candidate_ids):
                    errors.append(
                        f"{path}.input_candidates_count: must equal len(target_candidate_ids)"
                    )
                if previous_target_ids is not None and not target_ids <= previous_target_ids:
                    errors.append(
                        f"{path}.target_candidate_ids: later rounds must be a monotonic subset"
                    )
                previous_target_ids = target_ids
                if index >= 2:
                    if previous_changed_ids is None:
                        errors.append(
                            f"{path}.target_candidate_ids: Round 3 requires prior changed_candidate_ids"
                        )
                    elif not target_ids <= previous_changed_ids:
                        errors.append(
                            f"{path}.target_candidate_ids: Round 3 must target only candidates changed in Round 2"
                        )
        state_metric_keys = {
            "changed_candidate_ids",
            "changed_candidate_count",
            "evidence_added_count",
            "disposition_changed_count",
            "remaining_active_count",
        }
        present_state_metric_keys = state_metric_keys & set(round_result)
        if present_state_metric_keys and present_state_metric_keys != state_metric_keys:
            missing = ", ".join(sorted(state_metric_keys - present_state_metric_keys))
            errors.append(f"{path}: incomplete host state metrics; missing {missing}")
        changed_candidate_ids = round_result.get("changed_candidate_ids")
        current_changed_ids: set[str] | None = None
        if changed_candidate_ids is not None:
            if (
                not isinstance(changed_candidate_ids, list)
                or any(
                    not isinstance(value, str) or not value
                    for value in changed_candidate_ids
                )
            ):
                errors.append(
                    f"{path}.changed_candidate_ids: must be an array of non-empty strings"
                )
            else:
                current_changed_ids = set(changed_candidate_ids)
                if len(current_changed_ids) != len(changed_candidate_ids):
                    errors.append(
                        f"{path}.changed_candidate_ids: must contain unique values"
                    )
                if round_result.get("changed_candidate_count") != len(
                    changed_candidate_ids
                ):
                    errors.append(
                        f"{path}.changed_candidate_count: must equal len(changed_candidate_ids)"
                    )
        changed_count = round_result.get("changed_candidate_count")
        for key in ("evidence_added_count", "disposition_changed_count"):
            value = round_result.get(key)
            if (
                isinstance(changed_count, int)
                and not isinstance(changed_count, bool)
                and isinstance(value, int)
                and not isinstance(value, bool)
                and value > changed_count
            ):
                errors.append(f"{path}.{key}: must not exceed changed_candidate_count")
        if (
            "remaining_active_count" in round_result
            and round_result.get("remaining_active_count")
            != round_result.get("output_candidates_count")
        ):
            errors.append(
                f"{path}.remaining_active_count: must equal output_candidates_count"
            )
        previous_changed_ids = current_changed_ids
        state_digest_before = round_result.get("state_digest_before")
        state_digest_after = round_result.get("state_digest_after")
        for key, value in (
            ("state_digest_before", state_digest_before),
            ("state_digest_after", state_digest_after),
        ):
            if value is not None and (
                not isinstance(value, str) or STATE_DIGEST_RE.fullmatch(value) is None
            ):
                errors.append(f"{path}.{key}: must be a 64-character lowercase SHA-256")
        if (
            previous_state_digest_after is not None
            and isinstance(state_digest_before, str)
            and STATE_DIGEST_RE.fullmatch(state_digest_before)
            and state_digest_before != previous_state_digest_after
        ):
            errors.append(f"{path}.state_digest_before: state digest chain is discontinuous")
        previous_state_digest_after = (
            state_digest_after
            if isinstance(state_digest_after, str) and STATE_DIGEST_RE.fullmatch(state_digest_after)
            else None
        )
        for signature_index, signature in enumerate(round_result.get("contradiction_signatures", [])):
            if isinstance(signature, str):
                sensitive_reason = _sensitive_value_reason(signature)
                if sensitive_reason is not None:
                    errors.append(
                        f"{path}.contradiction_signatures[{signature_index}]: "
                        f"sensitive/raw value is not allowed ({sensitive_reason})"
                    )
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
            if not isinstance(candidate.get("finding_id"), str) or not candidate.get("finding_id"):
                errors.append(f"{cpath}.finding_id: stable identifier is required")
            if not isinstance(candidate.get("title"), str) or not candidate.get("title"):
                errors.append(f"{cpath}.title: must be non-empty")
            if candidate.get("reason") not in REJECTED_REASONS:
                errors.append(f"{cpath}.reason: invalid reason")
            for key in candidate:
                if _is_sensitive_key(key):
                    errors.append(f"{cpath}: sensitive/raw key is not allowed: {key}")
                value = candidate.get(key)
                if isinstance(value, str):
                    if key in {"finding_id", "fingerprint"} and value == REDACTED_SENSITIVE_VALUE:
                        errors.append(f"{cpath}.{key}: redacted identifier must use a stable surrogate")
                    sensitive_reason = _sensitive_value_reason(value)
                    if sensitive_reason is not None:
                        errors.append(
                            f"{cpath}.{key}: sensitive/raw value is not allowed ({sensitive_reason})"
                        )
    expected_posted = 0
    if rounds and isinstance(rounds[-1], dict):
        expected_posted = max(0, _as_int(rounds[-1].get("output_candidates_count")))
    halting = data.get("halting")
    if not isinstance(halting, dict):
        errors.append("$.halting: must be an object")
    else:
        reason = halting.get("reason")
        if reason not in HALT_REASONS:
            errors.append("$.halting.reason: invalid halt reason")
        should_halt = halting.get("should_halt")
        if not isinstance(should_halt, bool):
            errors.append("$.halting.should_halt: must be a boolean")
        elif should_halt is not True:
            errors.append("$.halting.should_halt: final review rounds artifact must halt before publication")
        elapsed_ms = halting.get("elapsed_ms")
        if not isinstance(elapsed_ms, int) or isinstance(elapsed_ms, bool) or elapsed_ms < 0:
            errors.append("$.halting.elapsed_ms: invalid integer")
        detail = halting.get("detail")
        if isinstance(detail, str):
            sensitive_reason = _sensitive_value_reason(detail)
            if sensitive_reason is not None:
                errors.append(f"$.halting.detail: sensitive/raw value is not allowed ({sensitive_reason})")
        triggered_at_round = halting.get("triggered_at_round")
        if not isinstance(triggered_at_round, int) or isinstance(triggered_at_round, bool) or triggered_at_round < 0:
            errors.append("$.halting.triggered_at_round: invalid integer")
        if (
            isinstance(policy_raw, dict)
            and isinstance(should_halt, bool)
            and isinstance(elapsed_ms, int)
            and not isinstance(elapsed_ms, bool)
            and all(isinstance(round_result, dict) for round_result in rounds)
        ):
            try:
                expected_decision = evaluate_halting(
                    HaltingPolicy.from_mapping(policy_raw),
                    rounds,
                    elapsed_ms=elapsed_ms,
                    active_candidates_count=expected_posted,
                )
            except (TypeError, ValueError) as exc:
                errors.append(f"$.halting: cannot recompute halting decision: {exc}")
            else:
                if should_halt != expected_decision.should_halt:
                    errors.append(
                        "$.halting.should_halt: "
                        f"expected {expected_decision.should_halt!r} from policy/rounds, got {should_halt!r}"
                    )
                if reason != expected_decision.reason:
                    errors.append(
                        "$.halting.reason: "
                        f"expected {expected_decision.reason!r} from policy/rounds, got {reason!r}"
                    )
                expected_triggered_at_round = len(rounds)
                if triggered_at_round != expected_triggered_at_round:
                    errors.append(
                        "$.halting.triggered_at_round: "
                        f"expected {expected_triggered_at_round} from rounds, got {triggered_at_round!r}"
                    )
    metrics = data.get("metrics")
    if not isinstance(metrics, dict):
        errors.append("$.metrics: must be an object")
    else:
        if metrics.get("total_rounds") != len(rounds):
            errors.append("$.metrics.total_rounds: must equal len(rounds)")
        expected_metrics = round_metrics(rounds, active_candidates_count=expected_posted)
        for key, expected_value in expected_metrics.items():
            if metrics.get(key) != expected_value:
                errors.append(f"$.metrics.{key}: expected {expected_value!r} from rounds, got {metrics.get(key)!r}")
    return errors


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan refinement rounds or validate review-rounds.v1 artifacts")
    parser.add_argument("--validate", type=Path, help="validate an existing review-rounds.v1 artifact")
    parser.add_argument("--emit-empty", action="store_true", help="emit an empty initialized artifact")
    parser.add_argument("--plan-next", action="store_true", help="emit the deterministic next-round plan")
    parser.add_argument("--apply-auto-deep", action="store_true", help="emit an auto-deep run-plan update")
    parser.add_argument("--policy", type=Path, help="halting-policy JSON")
    parser.add_argument("--candidates", type=Path, help="candidate JSON used with --plan-next")
    parser.add_argument(
        "--previous-candidates",
        type=Path,
        help="host snapshot from the start of the completed round used with --plan-next",
    )
    parser.add_argument("--rounds", type=Path, help="round-state JSON used with --plan-next")
    parser.add_argument("--run-plan", type=Path, help="run-plan JSON used for the auto-deep gate")
    parser.add_argument("--controller-plan", type=Path, help="next-round plan used with --apply-auto-deep")
    parser.add_argument("--elapsed-ms", type=int, default=0, help="elapsed milliseconds used with --plan-next")
    args = parser.parse_args(argv)

    if args.apply_auto_deep:
        if args.run_plan is None or args.controller_plan is None:
            parser.error("--apply-auto-deep requires --run-plan and --controller-plan")
        try:
            run_plan_data = load_json(args.run_plan)
            controller_plan_data = load_json(args.controller_plan)
            if not isinstance(run_plan_data, dict) or not isinstance(controller_plan_data, dict):
                raise ValueError("run plan and controller plan must be JSON objects")
            updated = apply_auto_deep(run_plan_data, controller_plan_data)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            print(f"INVALID auto-deep controller input: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(updated, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    if args.plan_next:
        if args.candidates is None or args.rounds is None:
            parser.error("--plan-next requires --candidates and --rounds")
        try:
            candidate_data = load_json(args.candidates)
            previous_candidate_data = (
                load_json(args.previous_candidates)
                if args.previous_candidates
                else None
            )
            round_data = load_json(args.rounds)
            policy_data = load_json(args.policy) if args.policy else None
            run_plan_data = load_json(args.run_plan) if args.run_plan else None
            candidates = (
                candidate_data
                if isinstance(candidate_data, list)
                else candidate_data.get("candidates")
                if isinstance(candidate_data, dict)
                else None
            )
            rounds = (
                round_data
                if isinstance(round_data, list)
                else round_data.get("rounds")
                if isinstance(round_data, dict)
                else None
            )
            previous_candidates = (
                previous_candidate_data
                if isinstance(previous_candidate_data, list)
                else previous_candidate_data.get("candidates")
                if isinstance(previous_candidate_data, dict)
                else None
            )
            if not isinstance(candidates, list) or not all(
                isinstance(candidate, dict) for candidate in candidates
            ):
                raise ValueError("candidates JSON must be an array or an object with a candidates array")
            if previous_candidate_data is not None and (
                not isinstance(previous_candidates, list)
                or not all(
                    isinstance(candidate, dict)
                    for candidate in previous_candidates
                )
            ):
                raise ValueError(
                    "previous candidates JSON must be an array or an object with a candidates array"
                )
            if not isinstance(rounds, list) or not all(
                isinstance(round_result, dict) for round_result in rounds
            ):
                raise ValueError("rounds JSON must be an array or an object with a rounds array")
            if isinstance(policy_data, dict):
                if isinstance(policy_data.get("review_loop"), dict):
                    policy_data = policy_data["review_loop"]
                if isinstance(policy_data.get("halting_policy"), dict):
                    policy_data = policy_data["halting_policy"]
            plan = plan_next_round(
                policy_data,
                rounds=rounds,
                candidates=candidates,
                elapsed_ms=args.elapsed_ms,
                run_plan=run_plan_data if isinstance(run_plan_data, dict) else None,
                previous_candidates=previous_candidates,
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            print(f"INVALID refinement controller input: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

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

    parser.error("one of --plan-next, --validate, or --emit-empty is required")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
