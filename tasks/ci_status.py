#!/usr/bin/env python3
"""Build read-only CI status artifacts for pr-codex review/send gates.

The helper is intentionally offline-friendly: callers may pass JSON captured from
GitHub read-only endpoints (`pulls/{n}`, `statusCheckRollup`, workflow runs, and
selected failed-job logs). It normalizes those inputs into `ci-status.json` and a
public-safe `ci-summary.md` without performing writes, reruns, or cancellations.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"), "[REDACTED_TOKEN]"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "[REDACTED_TOKEN]"),
    (re.compile(r"AIza[0-9A-Za-z_-]{20,}"), "[REDACTED_TOKEN]"),
    (re.compile(r"npm_[0-9A-Za-z]{20,}"), "[REDACTED_TOKEN]"),
    (re.compile(r"(?i)\b(authorization:\s*bearer\s+)[^\s]+"), r"\1[REDACTED_TOKEN]"),
    (re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key|aws_secret_access_key)\s*[=:]\s*[^\s]+"), r"\1=[REDACTED_SECRET]"),
    (re.compile(r"(?i)\b[A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|API_KEY)[A-Z0-9_]*\s*=\s*[^\s]+"), "[REDACTED_SECRET]"),
    (re.compile(r"/(?:Users|home)/[^\s:'\"]+(?:/[^\s:'\"]+)*"), "[REDACTED_LOCAL_PATH]"),
    (re.compile(r"[A-Za-z]:\\\\Users\\\\[^\s:'\"]+(?:\\\\[^\s:'\"]+)*"), "[REDACTED_LOCAL_PATH]"),
)


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def scrub_public_text(text: str, *, max_lines: int = 20, max_chars: int = 4000) -> str:
    """Return a public-safe, bounded summary string."""
    scrubbed = text
    for pattern, replacement in SECRET_PATTERNS:
        scrubbed = pattern.sub(replacement, scrubbed)
    lines = [line.rstrip() for line in scrubbed.splitlines() if line.strip()]
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"... truncated {len(lines) - max_lines} lines ..."]
    result = "\n".join(lines)
    if len(result) > max_chars:
        result = result[:max_chars] + "... [truncated]"
    return result


def _norm_lower(value: Any) -> str:
    return str(value or "").strip().lower()


def normalize_check_state(item: Mapping[str, Any]) -> str:
    status = _norm_lower(item.get("status") or item.get("state"))
    conclusion = _norm_lower(item.get("conclusion"))
    if status and status not in {"completed", "success", "failure", "error", "cancelled", "skipped"}:
        return "pending"
    if conclusion in {"success", "neutral"} or status == "success":
        return "success"
    if conclusion in {"failure", "startup_failure", "timed_out", "action_required"} or status in {"failure", "error"}:
        return "failure"
    if conclusion in {"cancelled"} or status == "cancelled":
        return "failure"
    if conclusion in {"skipped"} or status == "skipped":
        return "skipped"
    if status == "completed" and not conclusion:
        return "success"
    return "pending"


def normalize_rollup_items(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if isinstance(raw, dict):
        if "nodes" in raw:
            raw = raw["nodes"]
        elif "statusCheckRollup" in raw:
            raw = raw["statusCheckRollup"]
        elif "contexts" in raw:
            raw = raw["contexts"]
        elif "check_runs" in raw:
            raw = raw["check_runs"]
        elif "statuses" in raw:
            raw = raw["statuses"]
        else:
            raw = raw.get("checks", [])
    if not isinstance(raw, list):
        raise ValueError("status check rollup must be a list or object containing nodes/checks")

    checks: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"status check item at index {index} must be an object")
        name = str(item.get("name") or item.get("context") or item.get("workflowName") or f"check-{index + 1}")
        normalized = {
            "name": name,
            "status": item.get("status") or item.get("state") or "UNKNOWN",
            "conclusion": item.get("conclusion"),
            "state": normalize_check_state(item),
        }
        if item.get("detailsUrl") or item.get("target_url"):
            normalized["url"] = item.get("detailsUrl") or item.get("target_url")
        checks.append(normalized)
    return checks


def normalize_workflow_runs(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if isinstance(raw, dict):
        raw = raw.get("workflow_runs", raw.get("runs", []))
    if not isinstance(raw, list):
        raise ValueError("workflow runs must be a list or object containing workflow_runs")
    runs: list[dict[str, Any]] = []
    for index, run in enumerate(raw):
        if not isinstance(run, dict):
            raise ValueError(f"workflow run at index {index} must be an object")
        runs.append(
            {
                "id": run.get("databaseId") or run.get("id"),
                "name": run.get("name") or run.get("workflowName") or f"workflow-{index + 1}",
                "status": run.get("status"),
                "conclusion": run.get("conclusion"),
                "state": normalize_check_state(run),
                "url": run.get("html_url") or run.get("url"),
            }
        )
    return runs


def pull_context_from_rest_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    base = payload.get("base") or {}
    head = payload.get("head") or {}
    base_repo = base.get("repo") if isinstance(base, Mapping) else {}
    repository = base_repo.get("full_name") if isinstance(base_repo, Mapping) else None
    pr = {
        "repository": repository,
        "number": payload.get("number"),
        "url": payload.get("html_url"),
        "head_sha": head.get("sha") if isinstance(head, Mapping) else None,
        "base_sha": base.get("sha") if isinstance(base, Mapping) else None,
        "head_branch": head.get("ref") if isinstance(head, Mapping) else None,
        "base_branch": base.get("ref") if isinstance(base, Mapping) else None,
    }
    missing = [key for key in ("repository", "number", "head_sha") if not pr.get(key)]
    if missing:
        raise ValueError(f"pull REST payload missing required fields: {', '.join(missing)}")
    return pr


def _overall_state(states: Iterable[str]) -> str:
    seen = list(states)
    if any(state == "failure" for state in seen):
        return "failure"
    if any(state == "pending" for state in seen):
        return "pending"
    if seen and all(state == "skipped" for state in seen):
        return "skipped"
    return "success"


def build_ci_status(
    *,
    pr: Mapping[str, Any],
    status_check_rollup: Any,
    workflow_runs: Any,
    failed_job_logs: Mapping[str, str] | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    checks = normalize_rollup_items(status_check_rollup)
    runs = normalize_workflow_runs(workflow_runs)
    counts = {"success": 0, "failure": 0, "pending": 0, "skipped": 0}
    for check in checks:
        counts[check["state"]] += 1
    state = _overall_state([check["state"] for check in checks] + [run["state"] for run in runs])

    summaries: list[dict[str, str]] = []
    for name, log in (failed_job_logs or {}).items():
        summaries.append({"job": str(name), "summary": scrub_public_text(log)})

    return {
        "schema_version": "ci-status.v1",
        "generated_at": generated_at or now_utc(),
        "read_only": True,
        "policy": {"github_writes": False, "rerun": False, "cancel": False, "raw_logs_persisted": False},
        "pr": dict(pr),
        "head_sha": pr.get("head_sha"),
        "state": state,
        "counts": counts,
        "checks": checks,
        "workflow_runs": runs,
        "failed_job_summaries": summaries,
    }


def build_markdown_summary(status: Mapping[str, Any]) -> str:
    pr = status.get("pr", {}) if isinstance(status.get("pr"), Mapping) else {}
    lines = [
        "# CI summary",
        "",
        f"- repository: {pr.get('repository', '')}",
        f"- PR: {pr.get('number', '')}",
        f"- head_sha: {status.get('head_sha', '')}",
        f"- overall: {status.get('state', '')}",
        "- policy: read-only; no rerun/cancel/write",
        "",
        "## Checks",
    ]
    checks = status.get("checks", [])
    if checks:
        for check in checks:
            if isinstance(check, Mapping):
                lines.append(f"- {check.get('name')}: {check.get('state')} ({check.get('status')}/{check.get('conclusion')})")
    else:
        lines.append("- none reported")
    lines.extend(["", "## Failed job summaries"])
    summaries = status.get("failed_job_summaries", [])
    if summaries:
        for summary in summaries:
            if isinstance(summary, Mapping):
                lines.append(f"### {summary.get('job')}")
                lines.append(scrub_public_text(str(summary.get("summary", ""))))
                lines.append("")
    else:
        lines.append("- none")
    return scrub_public_text("\n".join(lines), max_lines=200, max_chars=20000) + "\n"


def _read_json(path: str | None, default: Any) -> Any:
    if not path:
        return default
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _read_failed_logs(values: list[str]) -> dict[str, str]:
    logs: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ValueError("--failed-log must use name=/path/to/log format")
        name, path = value.split("=", 1)
        logs[name] = Path(path).read_text(encoding="utf-8", errors="replace")
    return logs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pull-json", required=True, help="REST pulls/{number} JSON file")
    parser.add_argument("--status-check-rollup-json", required=True, help="statusCheckRollup JSON file")
    parser.add_argument("--workflow-runs-json", help="workflow runs JSON file")
    parser.add_argument("--failed-log", action="append", default=[], help="failed job log as job=/path/to/log")
    parser.add_argument("--out-json", required=True, help="output ci-status.json path")
    parser.add_argument("--out-md", required=True, help="output ci-summary.md path")
    args = parser.parse_args(argv)

    pr = pull_context_from_rest_payload(_read_json(args.pull_json, {}))
    status = build_ci_status(
        pr=pr,
        status_check_rollup=_read_json(args.status_check_rollup_json, []),
        workflow_runs=_read_json(args.workflow_runs_json, []),
        failed_job_logs=_read_failed_logs(args.failed_log),
    )
    out_json = Path(args.out_json)
    out_md = Path(args.out_md)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    out_md.write_text(build_markdown_summary(status), encoding="utf-8")
    print(f"wrote {out_json}")
    print(f"wrote {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
