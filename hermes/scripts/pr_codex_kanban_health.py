#!/usr/bin/env python3
"""Check pr-codex Hermes Kanban health and report risky task states."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from _pr_codex_common import (  # type: ignore[import-not-found]
    DEFAULT_BOARD,
    DEFAULT_OUTBOX_PATH,
    DEFAULT_REPO,
    DEFAULT_TENANT,
    KanbanTask,
    create_task_with_sink,
    discord_webhook_from_env,
    json_dumps,
    post_discord,
    run_command,
    utcnow_iso,
)


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def task_id(task: dict[str, Any]) -> str:
    return str(task.get("id") or task.get("task_id") or task.get("uuid") or "unknown")


def task_status(task: dict[str, Any]) -> str:
    return str(task.get("status") or task.get("state") or "unknown")


def task_assignee(task: dict[str, Any]) -> str:
    return str(task.get("assignee") or task.get("profile") or "unassigned")


def task_title(task: dict[str, Any]) -> str:
    return str(task.get("title") or task.get("name") or "untitled")


def task_retry_count(task: dict[str, Any]) -> int:
    for key in ("retry_count", "retries", "attempts", "run_count"):
        value = task.get(key)
        if isinstance(value, int):
            return value
    runs = task.get("runs")
    if isinstance(runs, list):
        return max(len(runs) - 1, 0)
    return 0


def task_age_minutes(task: dict[str, Any], *, now: datetime) -> int | None:
    for key in ("claimed_at", "started_at", "updated_at", "created_at"):
        parsed = parse_time(task.get(key))
        if parsed:
            return int((now - parsed).total_seconds() // 60)
    return None


def load_tasks_from_hermes(board: str) -> list[dict[str, Any]]:
    stdout = run_command(["hermes", "kanban", "--board", board, "list", "--json"])
    payload = json.loads(stdout or "[]")
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("tasks", "items", "data"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


def evaluate_health(
    tasks: list[dict[str, Any]],
    *,
    now: datetime,
    running_minutes: int,
    ready_minutes: int,
    retry_threshold: int,
) -> dict[str, list[dict[str, Any]]]:
    health: dict[str, list[dict[str, Any]]] = {
        "stale_running": [],
        "blocked": [],
        "high_retry": [],
        "stale_ready": [],
    }
    for task in tasks:
        status = task_status(task)
        age = task_age_minutes(task, now=now)
        summary = {
            "id": task_id(task),
            "title": task_title(task),
            "assignee": task_assignee(task),
            "status": status,
            "age_minutes": age,
            "retry_count": task_retry_count(task),
        }
        if status in {"running", "in_progress", "in-progress"} and age is not None and age >= running_minutes:
            health["stale_running"].append(summary)
        if status == "blocked":
            reason = task.get("blocked_reason") or task.get("reason")
            if reason:
                summary["reason"] = reason
            health["blocked"].append(summary)
        if summary["retry_count"] >= retry_threshold:
            health["high_retry"].append(summary)
        if status == "ready" and age is not None and age >= ready_minutes:
            health["stale_ready"].append(summary)
    return health


def render_health(health: dict[str, list[dict[str, Any]]], *, generated_at: str) -> str:
    labels = {
        "stale_running": "Long-running tasks",
        "blocked": "Blocked tasks",
        "high_retry": "High-retry tasks",
        "stale_ready": "Ready tasks not picked up",
    }
    lines = [f"# pr-codex Kanban health ({generated_at})", ""]
    if not any(health.values()):
        lines.append("No Phase 0 Kanban health issues detected.")
        return "\n".join(lines) + "\n"
    for key, label in labels.items():
        lines.extend([f"## {label}", ""])
        items = health.get(key, [])
        if not items:
            lines.append("- none")
        else:
            for item in items:
                age = "unknown" if item.get("age_minutes") is None else f"{item['age_minutes']}m"
                reason = f" reason={item['reason']}" if item.get("reason") else ""
                lines.append(
                    f"- {item['id']} [{item['status']}] {item['title']} "
                    f"assignee={item['assignee']} age={age} retries={item['retry_count']}{reason}"
                )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def create_health_task(
    *,
    report: str,
    repo: str,
    generated_at: str,
    sink: str,
    board: str,
    tenant: str,
    outbox_path: Path,
) -> dict[str, Any]:
    task = KanbanTask(
        title=f"[kanban-health] pr-codex {generated_at[:16]}",
        assignee="sheriff",
        body=report,
        idempotency_key=f"kanban-health:{repo}:{generated_at[:16]}",
        metadata={
            "repo": repo,
            "phase": "0-read-only-observer",
            "event_kind": "kanban-health",
            "generated_at": generated_at,
        },
        priority=1,
        seen_keys=(f"kanban-health:{repo}:{generated_at[:16]}",),
    )
    return create_task_with_sink(task, sink=sink, board=board, tenant=tenant, outbox_path=outbox_path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--board", default=DEFAULT_BOARD)
    parser.add_argument("--tenant", default=DEFAULT_TENANT)
    parser.add_argument("--outbox", type=Path, default=DEFAULT_OUTBOX_PATH)
    parser.add_argument("--tasks-json", type=Path, help="read Hermes task list JSON from a file")
    parser.add_argument("--sink", choices=("auto", "hermes", "outbox", "print", "none"), default="none")
    parser.add_argument("--discord-webhook-url", help="override HERMES_DISCORD_WEBHOOK_URL / DISCORD_WEBHOOK_URL")
    parser.add_argument("--running-minutes", type=int, default=90)
    parser.add_argument("--ready-minutes", type=int, default=60)
    parser.add_argument("--retry-threshold", type=int, default=3)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    generated_at = utcnow_iso()
    now = datetime.now(timezone.utc)
    if args.tasks_json:
        with args.tasks_json.open(encoding="utf-8") as f:
            payload = json.load(f)
        tasks = payload if isinstance(payload, list) else payload.get("tasks", [])
    else:
        tasks = load_tasks_from_hermes(args.board)
    health = evaluate_health(
        tasks,
        now=now,
        running_minutes=args.running_minutes,
        ready_minutes=args.ready_minutes,
        retry_threshold=args.retry_threshold,
    )
    report = render_health(health, generated_at=generated_at)

    discord_result = "skipped"
    webhook_url = args.discord_webhook_url or discord_webhook_from_env()
    if webhook_url and any(health.values()):
        post_discord(webhook_url, report[:1900])
        discord_result = "posted"

    task_result = None
    if args.sink != "none" and any(health.values()):
        task_result = create_health_task(
            report=report,
            repo=args.repo,
            generated_at=generated_at,
            sink=args.sink,
            board=args.board,
            tenant=args.tenant,
            outbox_path=args.outbox,
        )

    if args.json:
        print(json_dumps({"generated_at": generated_at, "health": health, "discord": discord_result, "task_result": task_result}, indent=2))
    else:
        print(report)
        print(f"discord: {discord_result}")
        if task_result is not None:
            print(f"kanban sink: {task_result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
