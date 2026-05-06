#!/usr/bin/env python3
"""Shared helpers for pr-codex Hermes Phase 0 automation.

The helpers intentionally depend only on Python's standard library and shell out to
``gh`` / ``hermes``.  That keeps the scripts usable from Hermes cron sessions and
on minimal macOS hosts without extra package installation.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import hashlib
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
ISSUE_TRIAGE_SENTINEL_KIND = "issue-triage"
ISSUE_TRIAGE_SENTINEL_VERSION = "v1"
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


def repo_project_slug(repo: str) -> str:
    """Return the repo-local slug used in Hermes public sentinels."""

    return ((repo or "").rsplit("/", 1)[-1] or repo or "unknown").strip()


def _record_substitution(
    text: str,
    pattern: re.Pattern[str],
    replacement: str,
    category: str,
    redactions: set[str],
) -> str:
    updated, count = pattern.subn(replacement, text)
    if count:
        redactions.add(category)
    return updated


def scrub_for_public(text: str | None, *, max_chars: int = 800) -> tuple[str, list[str]]:
    """Redact content that must not be posted to a public GitHub issue.

    Returns ``(scrubbed_text, redaction_categories)``. The category list is
    intended for dry-run reports and Kanban metadata.
    """

    scrubbed = "" if text is None else str(text)
    redactions: set[str] = set()

    raw_block_patterns: tuple[tuple[re.Pattern[str], str, str], ...] = (
        (
            re.compile(r"```(?:log|logs|console|shell|bash|zsh|text|output|json|graphql)?\s*\n[\s\S]*?```", re.I),
            "[REDACTED_RAW_LOG_OR_PAYLOAD]",
            "raw_log_or_payload",
        ),
        (
            re.compile(r"(?ms)^Traceback \(most recent call last\):.*?(?=^\S|\Z)"),
            "[REDACTED_RAW_STACK_TRACE]",
            "raw_stack_trace",
        ),
        (
            re.compile(r"(?is)\b(query|mutation)\s+[A-Za-z0-9_]*\s*\{[\s\S]{40,}?\}"),
            "[REDACTED_RAW_GRAPHQL]",
            "raw_graphql_payload",
        ),
    )
    for pattern, replacement, category in raw_block_patterns:
        scrubbed = _record_substitution(scrubbed, pattern, replacement, category, redactions)

    secret_patterns: tuple[tuple[re.Pattern[str], str, str], ...] = (
        (re.compile(r"\bsk_live_\S+", re.I), "[REDACTED_SECRET]", "openai_secret"),
        (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), "[REDACTED_SECRET]", "openai_secret"),
        (re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"), "[REDACTED_GITHUB_TOKEN]", "github_token"),
        (
            re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/\-]+=*"),
            "Bearer [REDACTED]",
            "bearer_token",
        ),
        (
            re.compile(
                r"\b(?:AWS|GH|OPENAI|DISCORD|HERMES|PR_CODEX)_[A-Z0-9_]+\s*[:=]\s*\S+",
                re.I,
            ),
            "[REDACTED_ENV_SECRET]",
            "env_secret",
        ),
        (
            re.compile(
                r"(?i)\b(?:api[_-]?key|access[_-]?token|token|secret|password|credential|client_secret)"
                r"(\s*[:=]\s*)([\"']?)[^\s,;\"']+([\"']?)"
            ),
            "[REDACTED_SECRET_ASSIGNMENT]",
            "secret_assignment",
        ),
        (
            re.compile(r"https://[^\s/@:]+:[^\s/@]+@"),
            "https://[REDACTED_CREDENTIALS]@",
            "url_credentials",
        ),
        (
            re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
            "[REDACTED_IP]",
            "ip_address",
        ),
    )
    for pattern, replacement, category in secret_patterns:
        scrubbed = _record_substitution(scrubbed, pattern, replacement, category, redactions)

    if ".agent-orchestrator/" in scrubbed:
        redactions.add("agent_orchestrator_path")

    private_path_patterns: tuple[tuple[re.Pattern[str], str, str], ...] = (
        (
            re.compile(r"(?<!\w)/Users/[^/\s`'\"]+(?:/[^\s`'\"]*)?"),
            "/Users/<user>/[REDACTED_PATH]",
            "local_private_path",
        ),
        (
            re.compile(r"(?<!\w)/home/[^/\s`'\"]+(?:/[^\s`'\"]*)?"),
            "/home/<user>/[REDACTED_PATH]",
            "local_private_path",
        ),
        (re.compile(r"~/.hermes\S*"), "~/.hermes/[REDACTED_PATH]", "hermes_private_path"),
        (
            re.compile(r"\S*\.agent-orchestrator/\S*"),
            "[REDACTED_AGENT_ORCHESTRATOR_PATH]",
            "agent_orchestrator_path",
        ),
    )
    for pattern, replacement, category in private_path_patterns:
        scrubbed = _record_substitution(scrubbed, pattern, replacement, category, redactions)

    operational_patterns: tuple[tuple[re.Pattern[str], str, str], ...] = (
        (re.compile(r"\bt_[0-9a-f]{8,}\b"), "[REDACTED_HERMES_TASK_ID]", "hermes_task_id"),
        (re.compile(r"\bpc-\d+\b"), "[REDACTED_PROFILE_SESSION]", "profile_session"),
    )
    for pattern, replacement, category in operational_patterns:
        scrubbed = _record_substitution(scrubbed, pattern, replacement, category, redactions)

    scrubbed = "\n".join(" ".join(line.split()) for line in scrubbed.splitlines()).strip()
    if max_chars > 0 and len(scrubbed) > max_chars:
        scrubbed = scrubbed[: max_chars - 1].rstrip() + "…"
        redactions.add("truncated")
    return scrubbed, sorted(redactions)


def public_text_has_substance(text: str) -> bool:
    """Return True when scrubbed text contains more than redaction placeholders."""

    without_placeholders = re.sub(r"\[(?:REDACTED|redacted)[^\]]*\]", "", text, flags=re.I)
    without_paths = without_placeholders.replace("/Users/<user>/", "").replace("/home/<user>/", "")
    without_secret_scaffolding = re.sub(r"(?i)\b(authorization|bearer|basic|token|secret|password)\b", "", without_paths)
    return bool(re.search(r"[A-Za-z0-9ぁ-んァ-ヶ一-龥]", without_secret_scaffolding))


def triage_publish_content_hash(scrubbed_body: str) -> str:
    """Return the short hash used in issue-triage sentinels."""

    return hashlib.sha256(scrubbed_body.encode("utf-8")).hexdigest()[:8]


def triage_publish_idempotency_key(issue_number: int, scrub_hash: str) -> str:
    """Return the publisher-specific idempotency key for a scrubbed conclusion."""

    return f"issue_triage:publish:#{int(issue_number)}:{scrub_hash}"


def build_issue_triage_sentinel(
    *,
    repo: str,
    issue_number: int,
    scrub_hash: str,
    version: str = ISSUE_TRIAGE_SENTINEL_VERSION,
) -> str:
    """Build the public marker for a Hermes issue-triage comment."""

    return (
        f"<!-- hermes-auto:{repo_project_slug(repo)} {ISSUE_TRIAGE_SENTINEL_KIND} "
        f"{version} issue=#{int(issue_number)} hash={scrub_hash} -->"
    )


def parse_issue_triage_sentinels(body: str | None) -> list[dict[str, str]]:
    """Extract issue-triage sentinel attributes from a GitHub comment body."""

    if HERMES_AUTO_MARKER not in (body or ""):
        return []
    pattern = re.compile(
        r"<!--\s*hermes-auto:(?P<project>\S+)\s+issue-triage\s+"
        r"(?P<version>\S+)(?P<attrs>.*?)-->",
        flags=re.IGNORECASE | re.DOTALL,
    )
    sentinels: list[dict[str, str]] = []
    for match in pattern.finditer(body or ""):
        attrs = {
            "project": match.group("project"),
            "version": match.group("version"),
        }
        for key, value in re.findall(r"([A-Za-z_][A-Za-z0-9_-]*)=([^\s>]+)", match.group("attrs")):
            attrs[key] = value.strip("\"'")
        sentinels.append(attrs)
    return sentinels


def split_csv_env(value: str | None) -> set[str]:
    return {item.strip().lower() for item in (value or "").split(",") if item.strip()}


def repo_owner(repo: str) -> str | None:
    owner, _, _ = (repo or "").partition("/")
    return owner or None


def trusted_hermes_auto_authors(repo: str) -> set[str]:
    """Return logins trusted to have produced Hermes marker comments.

    The marker is public and therefore never sufficient on its own.  Phase 0's
    documented deployment uses the repository owner's GitHub auth for generated
    comments, while operators can tighten or extend the allow-list with
    ``PR_CODEX_HERMES_AUTO_AUTHORS``.
    """

    configured = split_csv_env(os.environ.get("PR_CODEX_HERMES_AUTO_AUTHORS"))
    if configured:
        return configured
    owner = repo_owner(repo)
    return {owner.lower()} if owner else set()


def trusted_hermes_auto_apps() -> set[str]:
    """Return GitHub App slugs trusted to have produced Hermes marker comments."""

    return split_csv_env(os.environ.get("PR_CODEX_HERMES_AUTO_APPS"))


def github_actor_login(item: dict[str, Any]) -> str | None:
    user = item.get("user")
    if isinstance(user, dict) and user.get("login"):
        return str(user["login"]).lower()
    author = item.get("author")
    if isinstance(author, dict) and author.get("login"):
        return str(author["login"]).lower()
    return None


def github_app_slug(item: dict[str, Any]) -> str | None:
    app = item.get("performed_via_github_app") or item.get("app")
    if isinstance(app, dict):
        slug = app.get("slug") or app.get("name")
        if slug:
            return str(slug).lower()
    return None


def is_trusted_hermes_auto_item(
    item: dict[str, Any],
    *,
    repo: str,
    trusted_authors: set[str] | None = None,
    trusted_apps: set[str] | None = None,
) -> bool:
    """Return True only for marker-bearing comments from trusted automation.

    GitHub comment/review bodies are untrusted input.  A public marker string
    alone must not suppress feedback tasks, otherwise any commenter could hide
    actionable review feedback by pasting the sentinel.
    """

    if not contains_hermes_marker(item.get("body")):
        return False
    authors = trusted_hermes_auto_authors(repo) if trusted_authors is None else trusted_authors
    apps = trusted_hermes_auto_apps() if trusted_apps is None else trusted_apps
    actor = github_actor_login(item)
    app = github_app_slug(item)
    return bool((actor and actor in authors) or (app and app in apps))


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
