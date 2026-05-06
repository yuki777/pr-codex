#!/usr/bin/env python3
"""Build a daily Phase 0 digest for pr-codex Hermes automation."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from _pr_codex_common import (  # type: ignore[import-not-found]
    DEFAULT_BOARD,
    DEFAULT_OUTBOX_PATH,
    DEFAULT_REPO,
    DEFAULT_STATE_PATH,
    DEFAULT_TENANT,
    KanbanTask,
    create_task_with_sink,
    discord_webhook_from_env,
    gh_json,
    json_dumps,
    load_state,
    post_discord,
    save_state,
    today_utc,
    utcnow_iso,
)
from pr_codex_watch import split_repo  # type: ignore[import-not-found]


def fetch_digest_snapshot(repo: str) -> dict[str, Any]:
    owner, name = split_repo(repo)
    issues = gh_json([f"repos/{owner}/{name}/issues?state=open&per_page=100"]) or []
    open_issues = [issue for issue in issues if "pull_request" not in issue]
    pulls = gh_json([f"repos/{owner}/{name}/pulls?state=open&per_page=100"]) or []
    check_runs: dict[int, dict[str, int]] = {}
    for pr in pulls:
        sha = (pr.get("head") or {}).get("sha")
        if not sha:
            continue
        try:
            payload = gh_json([f"repos/{owner}/{name}/commits/{sha}/check-runs?per_page=100"]) or {}
        except Exception:
            payload = {}
        counts = Counter(run.get("conclusion") or run.get("status") or "unknown" for run in payload.get("check_runs", []))
        check_runs[int(pr["number"])] = dict(counts)
    return {"issues": open_issues, "pulls": pulls, "check_runs": check_runs}


def task_counts_for_today(state: dict[str, Any], day: str) -> Counter[str]:
    counts: Counter[str] = Counter()
    for task in state.get("tasks", []):
        created_at = task.get("created_at") or ""
        if created_at.startswith(day):
            event_kind = ((task.get("metadata") or {}).get("event_kind")) or "unknown"
            counts[event_kind] += 1
    return counts


def render_digest(repo: str, snapshot: dict[str, Any], state: dict[str, Any], *, generated_at: str) -> str:
    day = generated_at[:10]
    issues = snapshot.get("issues", [])
    pulls = snapshot.get("pulls", [])
    task_counts = task_counts_for_today(state, day)

    lines = [
        f"# pr-codex daily digest ({day})",
        "",
        f"Generated at: `{generated_at}`",
        f"Repository: `{repo}`",
        "",
        "## Open issues",
    ]
    if issues:
        for issue in sorted(issues, key=lambda item: int(item["number"])):
            labels = ", ".join(label.get("name", "") for label in issue.get("labels", [])) or "no labels"
            lines.append(f"- #{issue['number']} {issue.get('title', '')} ({labels})")
    else:
        lines.append("- none")

    lines.extend(["", "## Open PRs"])
    if pulls:
        for pr in sorted(pulls, key=lambda item: int(item["number"])):
            checks = snapshot.get("check_runs", {}).get(int(pr["number"]), {})
            checks_text = ", ".join(f"{name}:{count}" for name, count in sorted(checks.items())) or "no checks"
            draft = " draft" if pr.get("draft") else ""
            lines.append(
                f"- #{pr['number']} {pr.get('title', '')}{draft} "
                f"head={(pr.get('head') or {}).get('sha', 'unknown')[:7]} checks=({checks_text})"
            )
    else:
        lines.append("- none")

    lines.extend(["", "## Hermes watcher tasks created today"])
    if task_counts:
        for kind, count in sorted(task_counts.items()):
            lines.append(f"- {kind}: {count}")
    else:
        lines.append("- none")

    lines.extend(
        [
            "",
            "## Phase 0 reminder",
            "- Watchers only detect deltas and create/record Kanban tasks.",
            "- No GitHub comments, label changes, pushes, approvals, or merges are automatic in Phase 0.",
        ]
    )
    return "\n".join(lines) + "\n"


def create_digest_task(
    *,
    digest: str,
    repo: str,
    generated_at: str,
    sink: str,
    board: str,
    tenant: str,
    outbox_path: Path,
) -> dict[str, Any]:
    day = generated_at[:10]
    task = KanbanTask(
        title=f"[daily-digest] pr-codex {day}",
        assignee="sheriff",
        body=digest,
        idempotency_key=f"daily-digest:{repo}:{day}",
        metadata={
            "repo": repo,
            "phase": "0-read-only-observer",
            "event_kind": "daily-digest",
            "generated_at": generated_at,
        },
        priority=3,
        seen_keys=(f"daily-digest:{repo}:{day}",),
    )
    return create_task_with_sink(task, sink=sink, board=board, tenant=tenant, outbox_path=outbox_path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument("--board", default=DEFAULT_BOARD)
    parser.add_argument("--tenant", default=DEFAULT_TENANT)
    parser.add_argument("--outbox", type=Path, default=DEFAULT_OUTBOX_PATH)
    parser.add_argument("--sink", choices=("auto", "hermes", "outbox", "print", "none"), default="none")
    parser.add_argument("--discord-webhook-url", help="override HERMES_DISCORD_WEBHOOK_URL / DISCORD_WEBHOOK_URL")
    parser.add_argument("--snapshot", type=Path, help="read digest snapshot JSON instead of polling GitHub")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    generated_at = utcnow_iso()
    state = load_state(args.state)
    if args.snapshot:
        with args.snapshot.open(encoding="utf-8") as f:
            snapshot = json.load(f)
    else:
        snapshot = fetch_digest_snapshot(args.repo)
    digest = render_digest(args.repo, snapshot, state, generated_at=generated_at)

    discord_result = "skipped"
    webhook_url = args.discord_webhook_url or discord_webhook_from_env()
    if webhook_url:
        post_discord(webhook_url, digest[:1900])
        discord_result = "posted"

    task_result: dict[str, Any] | None = None
    if args.sink != "none":
        task_result = create_digest_task(
            digest=digest,
            repo=args.repo,
            generated_at=generated_at,
            sink=args.sink,
            board=args.board,
            tenant=args.tenant,
            outbox_path=args.outbox,
        )
        state.setdefault("tasks", []).append(
            {
                "created_at": generated_at,
                "sink": args.sink,
                "title": f"[daily-digest] pr-codex {today_utc()}",
                "assignee": "sheriff",
                "idempotency_key": f"daily-digest:{args.repo}:{today_utc()}",
                "metadata": {"event_kind": "daily-digest", "repo": args.repo},
                "result": task_result,
            }
        )
        save_state(state, args.state)

    if args.json:
        print(json_dumps({"generated_at": generated_at, "discord": discord_result, "task_result": task_result, "digest": digest}, indent=2))
    else:
        print(digest)
        print(f"discord: {discord_result}")
        if task_result is not None:
            print(f"kanban sink: {task_result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
