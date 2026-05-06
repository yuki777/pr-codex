#!/usr/bin/env python3
"""Shared helpers for pr-codex Hermes Phase 0 automation.

The helpers intentionally depend only on Python's standard library and shell out to
``gh`` / ``hermes``.  That keeps the scripts usable from Hermes cron sessions and
on minimal macOS hosts without extra package installation.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

DEFAULT_REPO = "yuki777/pr-codex"
DEFAULT_BOARD = "pr-codex"
DEFAULT_TENANT = "yuki777/pr-codex"
DEFAULT_HERMES_ROOT = Path(os.environ.get("PR_CODEX_HERMES_ROOT", "~/.hermes")).expanduser()
DEFAULT_STATE_PATH = DEFAULT_HERMES_ROOT / "automation" / "pr-codex" / "state.json"
DEFAULT_OUTBOX_PATH = DEFAULT_HERMES_ROOT / "automation" / "pr-codex" / "tasks.jsonl"
HERMES_AUTO_MARKER = "<!-- hermes-auto:"
STATE_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class KanbanTask:
    """A normalized task request produced by a watcher/digest script."""

    title: str
    assignee: str
    body: str
    idempotency_key: str
    metadata: dict[str, Any]
    priority: int = 2
    seen_keys: tuple[str, ...] = field(default_factory=tuple)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def contains_hermes_marker(text: str | None) -> bool:
    return HERMES_AUTO_MARKER in (text or "")


def short_sha(value: str | None, length: int = 7) -> str:
    if not value:
        return "unknown"
    return value[:length]


def compact_title(title: str, limit: int = 96) -> str:
    normalized = " ".join((title or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"


def json_dumps(value: Any, *, indent: int | None = None) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=indent)


def load_state(path: Path = DEFAULT_STATE_PATH) -> dict[str, Any]:
    path = path.expanduser()
    if not path.exists():
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "repo": DEFAULT_REPO,
            "seen": {},
            "tasks": [],
            "last_run_at": None,
        }
    with path.open(encoding="utf-8") as f:
        state = json.load(f)
    state.setdefault("schema_version", STATE_SCHEMA_VERSION)
    state.setdefault("seen", {})
    state.setdefault("tasks", [])
    return state


def save_state(state: dict[str, Any], path: Path = DEFAULT_STATE_PATH) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json_dumps(state, indent=2) + "\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
        tmp.write(payload)
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


def seen_contains(state: dict[str, Any], key: str) -> bool:
    seen = state.setdefault("seen", {})
    if isinstance(seen, list):
        # Compatibility with a hypothetical early list-shaped state.
        seen_dict = {item: {"first_seen_at": None} for item in seen}
        state["seen"] = seen_dict
        seen = seen_dict
    return key in seen


def mark_seen(
    state: dict[str, Any],
    keys: Iterable[str],
    *,
    event_kind: str,
    timestamp: str,
    mode: str,
) -> None:
    seen = state.setdefault("seen", {})
    for key in keys:
        seen.setdefault(
            key,
            {
                "first_seen_at": timestamp,
                "event_kind": event_kind,
                "mode": mode,
            },
        )


def append_task_record(
    state: dict[str, Any],
    task: KanbanTask,
    *,
    sink: str,
    timestamp: str,
    result: dict[str, Any] | None = None,
) -> None:
    state.setdefault("tasks", []).append(
        {
            "created_at": timestamp,
            "sink": sink,
            "title": task.title,
            "assignee": task.assignee,
            "idempotency_key": task.idempotency_key,
            "metadata": task.metadata,
            "result": result or {},
        }
    )


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json_dumps(record) + "\n")


def run_command(args: list[str], *, input_text: str | None = None) -> str:
    completed = subprocess.run(
        args,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "command failed\n"
            f"args={args!r}\n"
            f"stdout={completed.stdout}\n"
            f"stderr={completed.stderr}"
        )
    return completed.stdout


def gh_json(args: list[str]) -> Any:
    stdout = run_command(["gh", "api", *args])
    if not stdout.strip():
        return None
    return json.loads(stdout)


def build_kanban_create_command(
    task: KanbanTask,
    *,
    board: str = DEFAULT_BOARD,
    tenant: str = DEFAULT_TENANT,
    hermes_bin: str = "hermes",
) -> list[str]:
    """Build the Hermes CLI command used by cron/scripts.

    The argument surface follows Hermes Agent's documented CLI shape:
    ``hermes kanban --board <slug> create "title" --assignee <profile> ...``.
    """

    return [
        hermes_bin,
        "kanban",
        "--board",
        board,
        "create",
        task.title,
        "--assignee",
        task.assignee,
        "--tenant",
        tenant,
        "--priority",
        str(task.priority),
        "--idempotency-key",
        task.idempotency_key,
        "--body",
        task.body,
        "--json",
    ]


def create_task_with_sink(
    task: KanbanTask,
    *,
    sink: str,
    board: str = DEFAULT_BOARD,
    tenant: str = DEFAULT_TENANT,
    outbox_path: Path = DEFAULT_OUTBOX_PATH,
) -> dict[str, Any]:
    """Create or persist a task through the requested sink.

    ``auto`` uses Hermes when the binary exists and falls back to an append-only
    outbox.  The outbox keeps cron executions auditable on machines that have not
    installed Hermes yet while making tests independent from the local agent.
    """

    effective_sink = sink
    if sink == "auto":
        effective_sink = "hermes" if shutil.which("hermes") else "outbox"

    record = {
        "title": task.title,
        "assignee": task.assignee,
        "body": task.body,
        "idempotency_key": task.idempotency_key,
        "priority": task.priority,
        "metadata": task.metadata,
        "board": board,
        "tenant": tenant,
    }

    if effective_sink == "print":
        print(json_dumps(record, indent=2))
        return {"sink": "print"}

    if effective_sink == "outbox":
        append_jsonl(outbox_path, {"created_at": utcnow_iso(), **record})
        return {"sink": "outbox", "path": str(outbox_path)}

    if effective_sink == "hermes":
        stdout = run_command(build_kanban_create_command(task, board=board, tenant=tenant))
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError:
            parsed = {"stdout": stdout}
        return {"sink": "hermes", "response": parsed}

    raise ValueError(f"unknown sink: {sink}")


def format_task_body(
    *,
    summary: str,
    profile: str,
    metadata: dict[str, Any],
    instructions: str,
) -> str:
    return (
        "## Hermes automation event\n\n"
        f"{summary}\n\n"
        "## Phase 0 safety rail\n\n"
        "This task was generated by the read-only observer. Do not post to GitHub, "
        "push commits, close issues, change labels, assign users, approve, request changes, "
        "or merge unless a later phase explicitly enables that action. Record findings in "
        "Kanban metadata/comments instead.\n\n"
        f"## Worker profile\n\n`{profile}`\n\n"
        "## Instructions\n\n"
        f"{instructions.strip()}\n\n"
        "## Metadata\n\n"
        "```json\n"
        f"{json_dumps(metadata, indent=2)}\n"
        "```\n"
    )


def post_discord(webhook_url: str, content: str) -> None:
    data = json.dumps({"content": content}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310 - user-configured webhook
        if response.status >= 300:
            raise RuntimeError(f"Discord webhook failed with status {response.status}")


def discord_webhook_from_env() -> str | None:
    return os.environ.get("HERMES_DISCORD_WEBHOOK_URL") or os.environ.get("DISCORD_WEBHOOK_URL")


def print_error(message: str) -> None:
    print(message, file=sys.stderr)
