#!/usr/bin/env python3
"""Phase 0 GitHub watcher for yuki777/pr-codex Hermes automation.

The watcher polls GitHub, converts new/updated Issue/PR/review events into
Hermes Kanban tasks, and records idempotency keys in
``~/.hermes/automation/pr-codex/state.json``.  It never posts to GitHub or pushes
commits; all real work is delegated to profile-specific Kanban tasks.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from _pr_codex_common import (  # type: ignore[import-not-found]
    DEFAULT_BOARD,
    DEFAULT_OUTBOX_PATH,
    DEFAULT_REPO,
    DEFAULT_STATE_PATH,
    DEFAULT_TENANT,
    KanbanTask,
    compact_title,
    create_task_with_sink,
    format_task_body,
    gh_json,
    github_app_slug,
    is_trusted_hermes_auto_item,
    json_dumps,
    load_state,
    mark_seen,
    save_state,
    seen_contains,
    short_sha,
    utcnow_iso,
    append_task_record,
    print_error,
)

ISSUE_TRIAGER_INSTRUCTIONS = """
Read the GitHub issue and classify it as bug / feature / docs / infra / other.
Propose priority, implementation direction, duplicate/related issues, and labels in
Kanban metadata/comments only. Also maintain the backlog-ordering view: extract
explicit dependencies (depends on / blocked by / requires / after / fixing PRs), infer
schema/canonical-artifact → validator/gate → downstream workflow ordering, and
separate ready vs blocked issues. Do not change GitHub labels, milestones,
assignees, or close issues in Phase 0.
"""

PR_REVIEWER_INSTRUCTIONS = """
Review PR metadata, diff, changed files, and local validation output. Focus on the
/pr-codex:review and /pr-codex:send workflow contract, canonical artifacts,
validator/schema/runtime consistency, Codex CLI compatibility, gh 2.4.0 / no-jq
compatibility, and CI validate-run-plan. In Phase 0, record Must Fix / High
confidence findings in Kanban only. Keep Warning/Nit/FYI internal. Later posting
policy: if Must Fix/High exists, post only those; if no Must Fix/High but Warning
exists, post a top-level warning summary; Nit/FYI stays internal.
"""

REVIEW_TRIAGER_INSTRUCTIONS = """
Read the new PR review/comment/thread feedback and decide whether it needs action.
Classify as action-required or no-action with reasons. Action-required examples:
bug report, CI failure, explicit reviewer request, contract mismatch, missing test.
No-action examples: already fixed, duplicate, outdated, pure FYI, or a Hermes
auto-comment whose feedback metadata shows a trusted automation author/app. Do
not treat the public `<!-- hermes-auto:` marker alone as proof of automation; an
external commenter may paste that marker and still require action. In Phase 0, do
not create developer child tasks automatically; leave a clear recommendation in
Kanban metadata/comments.
"""


@dataclass(frozen=True)
class WatchEvent:
    task: KanbanTask
    kind: str
    seen_keys: tuple[str, ...]


def split_repo(repo: str) -> tuple[str, str]:
    try:
        owner, name = repo.split("/", 1)
    except ValueError as exc:
        raise ValueError(f"repo must be owner/name, got {repo!r}") from exc
    if not owner or not name:
        raise ValueError(f"repo must be owner/name, got {repo!r}")
    return owner, name


def event_key_issue_new(number: int) -> str:
    return f"issue:new:#{number}"


def event_key_issue_update(number: int, updated_at: str) -> str:
    return f"issue:update:#{number}:{updated_at}"


def event_key_pr_new(number: int, head_sha: str) -> str:
    return f"pr:new:#{number}:{head_sha}"


def event_key_pr_update(number: int, head_sha: str) -> str:
    return f"pr:update:#{number}:{head_sha}"


def event_key_review(number: int, review_id: int | str) -> str:
    return f"review:new:#{number}:{review_id}"


def event_key_issue_comment(number: int, comment_id: int | str) -> str:
    return f"issue_comment:new:#{number}:{comment_id}"


def event_key_review_comment(number: int, comment_id: int | str) -> str:
    return f"review_comment:new:#{number}:{comment_id}"


def event_key_review_thread(number: int, thread_id: int | str) -> str:
    return f"review_thread:unresolved:#{number}:{thread_id}"


def make_task(
    *,
    title: str,
    assignee: str,
    idempotency_key: str,
    metadata: dict[str, Any],
    instructions: str,
    priority: int,
    seen_keys: tuple[str, ...],
) -> KanbanTask:
    summary = (
        f"Detected `{metadata['event_kind']}` for `{metadata['repo']}` "
        f"at `{metadata['detected_at']}`. Source: {metadata.get('html_url', 'n/a')}"
    )
    return KanbanTask(
        title=title,
        assignee=assignee,
        body=format_task_body(
            summary=summary,
            profile=assignee,
            metadata=metadata,
            instructions=instructions,
        ),
        idempotency_key=idempotency_key,
        metadata=metadata,
        priority=priority,
        seen_keys=seen_keys,
    )


def is_auto_comment_update(item: dict[str, Any], comments: list[dict[str, Any]], *, repo: str) -> bool:
    """Return True when an item's latest update appears to be a Hermes comment.

    GitHub issue/PR ``updated_at`` changes for many reasons.  We only suppress the
    update event when the newest comment contains the Hermes sentinel, was posted
    by trusted Hermes automation, and its timestamp matches the item's latest
    update timestamp.
    """

    if not comments:
        return False
    newest = max(comments, key=lambda c: c.get("updated_at") or c.get("created_at") or "")
    if not is_trusted_hermes_auto_item(newest, repo=repo):
        return False
    comment_timestamp = newest.get("updated_at") or newest.get("created_at")
    return bool(comment_timestamp and item.get("updated_at") == comment_timestamp)


def comments_for(mapping: dict[Any, list[dict[str, Any]]], number: int) -> list[dict[str, Any]]:
    return mapping.get(number) or mapping.get(str(number)) or []


def seen_has_prefix(state: dict[str, Any], prefix: str) -> bool:
    # Normalize old list-shaped state through the shared helper before reading keys.
    seen_contains(state, "__pr_codex_never_seen__")
    return any(str(key).startswith(prefix) for key in state.setdefault("seen", {}).keys())


def collect_issue_events(
    *,
    repo: str,
    issues: list[dict[str, Any]],
    issue_comments: dict[int, list[dict[str, Any]]],
    state: dict[str, Any],
    detected_at: str,
) -> list[WatchEvent]:
    events: list[WatchEvent] = []
    for issue in issues:
        if "pull_request" in issue:
            continue
        number = int(issue["number"])
        title = compact_title(issue.get("title", ""))
        updated_at = issue.get("updated_at") or issue.get("created_at") or detected_at
        new_key = event_key_issue_new(number)
        update_key = event_key_issue_update(number, updated_at)
        base_metadata = {
            "repo": repo,
            "phase": "0-read-only-observer",
            "number": number,
            "title": issue.get("title", ""),
            "html_url": issue.get("html_url"),
            "updated_at": updated_at,
            "detected_at": detected_at,
            "github_type": "issue",
        }
        if not seen_contains(state, new_key):
            seen_keys = (new_key, update_key)
            metadata = {**base_metadata, "event_kind": "issue:new", "seen_keys": list(seen_keys)}
            task = make_task(
                title=f"[issue-triage] #{number} {title}",
                assignee="issue-triager",
                idempotency_key=new_key,
                metadata=metadata,
                instructions=ISSUE_TRIAGER_INSTRUCTIONS,
                priority=2,
                seen_keys=seen_keys,
            )
            events.append(WatchEvent(task=task, kind="issue:new", seen_keys=seen_keys))
        elif not seen_contains(state, update_key) and not is_auto_comment_update(
            issue,
            comments_for(issue_comments, number),
            repo=repo,
        ):
            seen_keys = (update_key,)
            metadata = {**base_metadata, "event_kind": "issue:update", "seen_keys": list(seen_keys)}
            task = make_task(
                title=f"[issue-update] #{number} {title}",
                assignee="issue-triager",
                idempotency_key=update_key,
                metadata=metadata,
                instructions=ISSUE_TRIAGER_INSTRUCTIONS,
                priority=2,
                seen_keys=seen_keys,
            )
            events.append(WatchEvent(task=task, kind="issue:update", seen_keys=seen_keys))
    return events


def collect_pr_events(
    *,
    repo: str,
    pulls: list[dict[str, Any]],
    state: dict[str, Any],
    detected_at: str,
) -> list[WatchEvent]:
    events: list[WatchEvent] = []
    for pr in pulls:
        number = int(pr["number"])
        title = compact_title(pr.get("title", ""))
        head_sha = (pr.get("head") or {}).get("sha") or "unknown"
        new_key = event_key_pr_new(number, head_sha)
        update_key = event_key_pr_update(number, head_sha)
        base_metadata = {
            "repo": repo,
            "phase": "0-read-only-observer",
            "number": number,
            "title": pr.get("title", ""),
            "html_url": pr.get("html_url"),
            "head_sha": head_sha,
            "head_ref": (pr.get("head") or {}).get("ref"),
            "base_ref": (pr.get("base") or {}).get("ref"),
            "updated_at": pr.get("updated_at"),
            "detected_at": detected_at,
            "github_type": "pull_request",
        }
        if not seen_has_prefix(state, f"pr:new:#{number}:"):
            seen_keys = (new_key, update_key)
            metadata = {**base_metadata, "event_kind": "pr:new", "seen_keys": list(seen_keys)}
            task = make_task(
                title=f"[pr-review] #{number} head={short_sha(head_sha)} {title}",
                assignee="pr-reviewer",
                idempotency_key=new_key,
                metadata=metadata,
                instructions=PR_REVIEWER_INSTRUCTIONS,
                priority=1,
                seen_keys=seen_keys,
            )
            events.append(WatchEvent(task=task, kind="pr:new", seen_keys=seen_keys))
        elif not seen_contains(state, update_key):
            seen_keys = (update_key,)
            metadata = {**base_metadata, "event_kind": "pr:update", "seen_keys": list(seen_keys)}
            task = make_task(
                title=f"[pr-review] #{number} head={short_sha(head_sha)} {title}",
                assignee="pr-reviewer",
                idempotency_key=update_key,
                metadata=metadata,
                instructions=PR_REVIEWER_INSTRUCTIONS,
                priority=1,
                seen_keys=seen_keys,
            )
            events.append(WatchEvent(task=task, kind="pr:update", seen_keys=seen_keys))
    return events


def feedback_task(
    *,
    repo: str,
    pr_number: int,
    title_suffix: str,
    key: str,
    event_kind: str,
    html_url: str | None,
    item: dict[str, Any],
    detected_at: str,
) -> WatchEvent:
    metadata = {
        "repo": repo,
        "phase": "0-read-only-observer",
        "number": pr_number,
        "html_url": html_url,
        "detected_at": detected_at,
        "event_kind": event_kind,
        "github_type": "pull_request_feedback",
        "feedback": item,
        "seen_keys": [key],
    }
    task = make_task(
        title=f"[review-feedback] #{pr_number} {title_suffix}",
        assignee="review-triager",
        idempotency_key=key,
        metadata=metadata,
        instructions=REVIEW_TRIAGER_INSTRUCTIONS,
        priority=1,
        seen_keys=(key,),
    )
    return WatchEvent(task=task, kind=event_kind, seen_keys=(key,))


def collect_feedback_events(
    *,
    repo: str,
    pr_number: int,
    reviews: list[dict[str, Any]],
    issue_comments: list[dict[str, Any]],
    review_comments: list[dict[str, Any]],
    review_threads: list[dict[str, Any]],
    state: dict[str, Any],
    detected_at: str,
) -> list[WatchEvent]:
    events: list[WatchEvent] = []

    for review in reviews:
        if is_trusted_hermes_auto_item(review, repo=repo):
            continue
        review_id = review.get("id") or review.get("node_id")
        if review_id is None:
            continue
        key = event_key_review(pr_number, review_id)
        if seen_contains(state, key):
            continue
        state_name = review.get("state") or "review"
        events.append(
            feedback_task(
                repo=repo,
                pr_number=pr_number,
                title_suffix=f"review={review_id} state={state_name}",
                key=key,
                event_kind="review:new",
                html_url=review.get("html_url") or review.get("pull_request_url"),
                item=minimize_feedback_item(review),
                detected_at=detected_at,
            )
        )

    for comment in issue_comments:
        if is_trusted_hermes_auto_item(comment, repo=repo):
            continue
        comment_id = comment.get("id") or comment.get("node_id")
        if comment_id is None:
            continue
        key = event_key_issue_comment(pr_number, comment_id)
        if seen_contains(state, key):
            continue
        events.append(
            feedback_task(
                repo=repo,
                pr_number=pr_number,
                title_suffix=f"comment={comment_id}",
                key=key,
                event_kind="issue_comment:new",
                html_url=comment.get("html_url"),
                item=minimize_feedback_item(comment),
                detected_at=detected_at,
            )
        )

    for comment in review_comments:
        if is_trusted_hermes_auto_item(comment, repo=repo):
            continue
        comment_id = comment.get("id") or comment.get("node_id")
        if comment_id is None:
            continue
        key = event_key_review_comment(pr_number, comment_id)
        if seen_contains(state, key):
            continue
        events.append(
            feedback_task(
                repo=repo,
                pr_number=pr_number,
                title_suffix=f"comment={comment_id}",
                key=key,
                event_kind="review_comment:new",
                html_url=comment.get("html_url"),
                item=minimize_feedback_item(comment),
                detected_at=detected_at,
            )
        )

    for thread in review_threads:
        if thread.get("isResolved") is True:
            continue
        comments = (((thread.get("comments") or {}).get("nodes")) or [])
        if comments and all(is_trusted_hermes_auto_item(comment, repo=repo) for comment in comments):
            continue
        thread_id = thread.get("id")
        if thread_id is None:
            continue
        key = event_key_review_thread(pr_number, thread_id)
        if seen_contains(state, key):
            continue
        events.append(
            feedback_task(
                repo=repo,
                pr_number=pr_number,
                title_suffix=f"thread={str(thread_id)[-8:]}",
                key=key,
                event_kind="review_thread:unresolved",
                html_url=(comments[0] or {}).get("url") if comments else None,
                item=minimize_feedback_item(thread),
                detected_at=detected_at,
            )
        )

    return events


def minimize_feedback_item(item: dict[str, Any]) -> dict[str, Any]:
    """Keep metadata useful but small and avoid duplicating huge comment bodies."""

    allowed = {
        "id",
        "node_id",
        "state",
        "path",
        "line",
        "start_line",
        "position",
        "original_position",
        "created_at",
        "updated_at",
        "submitted_at",
        "html_url",
        "pull_request_url",
        "isResolved",
        "isOutdated",
        "url",
    }
    minimized = {key: value for key, value in item.items() if key in allowed}
    if "body" in item:
        body = item.get("body") or ""
        minimized["body_excerpt"] = body[:1000]
    if "user" in item:
        minimized["author"] = (item.get("user") or {}).get("login")
    if "author" in item and isinstance(item.get("author"), dict):
        minimized["author"] = (item.get("author") or {}).get("login")
    app = github_app_slug(item)
    if app:
        minimized["github_app"] = app
    if "comments" in item:
        nodes = ((item.get("comments") or {}).get("nodes")) or []
        minimized["comments"] = [minimize_feedback_item(node) for node in nodes[:5]]
    return minimized


def collect_events(snapshot: dict[str, Any], state: dict[str, Any], *, repo: str, detected_at: str) -> list[WatchEvent]:
    events: list[WatchEvent] = []
    events.extend(
        collect_issue_events(
            repo=repo,
            issues=snapshot.get("issues", []),
            issue_comments=snapshot.get("issue_comments", {}),
            state=state,
            detected_at=detected_at,
        )
    )
    events.extend(
        collect_pr_events(
            repo=repo,
            pulls=snapshot.get("pulls", []),
            state=state,
            detected_at=detected_at,
        )
    )
    for pr in snapshot.get("pulls", []):
        number = int(pr["number"])
        events.extend(
            collect_feedback_events(
                repo=repo,
                pr_number=number,
                reviews=comments_for(snapshot.get("reviews", {}), number),
                issue_comments=comments_for(snapshot.get("pr_issue_comments", {}), number),
                review_comments=comments_for(snapshot.get("review_comments", {}), number),
                review_threads=comments_for(snapshot.get("review_threads", {}), number),
                state=state,
                detected_at=detected_at,
            )
        )
    return events


def fetch_review_thread_comment_page(thread_id: str, cursor: str) -> dict[str, Any]:
    query = """
query($id: ID!, $cursor: String!) {
  node(id: $id) {
    ... on PullRequestReviewThread {
      comments(first: 100, after: $cursor) {
        nodes {
          id
          body
          createdAt
          url
          author { login }
          authorAssociation
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""
    payload = gh_json([
        "graphql",
        "-f",
        f"query={query}",
        "-f",
        f"id={thread_id}",
        "-f",
        f"cursor={cursor}",
    ]) or {}
    data = payload.get("data", payload) if isinstance(payload, dict) else {}
    node = data.get("node") if isinstance(data, dict) else {}
    return (node or {}).get("comments") or {}


def paginate_review_thread_comments(thread: dict[str, Any]) -> None:
    comments = thread.get("comments") if isinstance(thread, dict) else None
    if not isinstance(comments, dict):
        return
    page_info = comments.get("pageInfo") or {}
    thread_id = str(thread.get("id") or "")
    while thread_id and page_info.get("hasNextPage"):
        cursor = page_info.get("endCursor")
        if not cursor:
            break
        next_comments = fetch_review_thread_comment_page(thread_id, str(cursor))
        comments.setdefault("nodes", [])
        comments["nodes"].extend(next_comments.get("nodes") or [])
        page_info = next_comments.get("pageInfo") or {}
        comments["pageInfo"] = page_info


def fetch_review_threads(owner: str, repo_name: str, pr_number: int) -> list[dict[str, Any]]:
    query = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $cursor) {
        nodes {
          id
          isResolved
          isOutdated
          path
          line
          comments(first: 100) {
            nodes {
              id
              body
              createdAt
              url
              author { login }
              authorAssociation
            }
            pageInfo { hasNextPage endCursor }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""
    threads: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        args = [
            "graphql",
            "-f",
            f"query={query}",
            "-f",
            f"owner={owner}",
            "-f",
            f"name={repo_name}",
            "-F",
            f"number={pr_number}",
        ]
        if cursor:
            args.extend(["-f", f"cursor={cursor}"])
        payload = gh_json(args) or {}
        data = payload.get("data", payload) if isinstance(payload, dict) else {}
        review_threads = (((data.get("repository") or {}).get("pullRequest") or {}).get("reviewThreads")) or {}
        nodes = review_threads.get("nodes") or []
        for thread in nodes:
            paginate_review_thread_comments(thread)
        threads.extend(nodes)
        page_info = review_threads.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
    return threads


def paginated_path(path: str, *, page: int, per_page: int = 100) -> str:
    separator = "&" if "?" in path else "?"
    return f"{path}{separator}per_page={per_page}&page={page}"


def fetch_paginated_list(path: str, *, per_page: int = 100) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    page = 1
    while True:
        payload = gh_json([paginated_path(path, page=page, per_page=per_page)]) or []
        if not isinstance(payload, list):
            raise RuntimeError(f"expected list response for paginated GitHub endpoint: {path}")
        items.extend(payload)
        if len(payload) < per_page:
            return items
        page += 1


def fetch_snapshot(repo: str, *, include_threads: bool = True) -> dict[str, Any]:
    owner, name = split_repo(repo)
    issues = fetch_paginated_list(f"repos/{owner}/{name}/issues?state=open")
    pulls = fetch_paginated_list(f"repos/{owner}/{name}/pulls?state=open")

    issue_comments: dict[int, list[dict[str, Any]]] = {}
    for issue in issues:
        if "pull_request" in issue:
            continue
        number = int(issue["number"])
        if int(issue.get("comments") or 0) > 0:
            issue_comments[number] = fetch_paginated_list(f"repos/{owner}/{name}/issues/{number}/comments")
        else:
            issue_comments[number] = []

    reviews: dict[int, list[dict[str, Any]]] = {}
    pr_issue_comments: dict[int, list[dict[str, Any]]] = {}
    review_comments: dict[int, list[dict[str, Any]]] = {}
    review_threads: dict[int, list[dict[str, Any]]] = {}
    for pr in pulls:
        number = int(pr["number"])
        reviews[number] = fetch_paginated_list(f"repos/{owner}/{name}/pulls/{number}/reviews")
        pr_issue_comments[number] = fetch_paginated_list(f"repos/{owner}/{name}/issues/{number}/comments")
        review_comments[number] = fetch_paginated_list(f"repos/{owner}/{name}/pulls/{number}/comments")
        if include_threads:
            try:
                review_threads[number] = fetch_review_threads(owner, name, number)
            except Exception as exc:  # noqa: BLE001 - keep watcher alive if GraphQL shape changes.
                print_error(f"warning: could not fetch review threads for PR #{number}: {exc}")
                review_threads[number] = []
        else:
            review_threads[number] = []

    return {
        "issues": issues,
        "pulls": pulls,
        "issue_comments": issue_comments,
        "reviews": reviews,
        "pr_issue_comments": pr_issue_comments,
        "review_comments": review_comments,
        "review_threads": review_threads,
    }


def process_events(
    events: list[WatchEvent],
    *,
    state: dict[str, Any],
    sink: str,
    board: str,
    tenant: str,
    outbox_path: Path,
    seed: bool,
    dry_run: bool,
    detected_at: str,
) -> dict[str, Any]:
    processed: list[dict[str, Any]] = []
    for event in events:
        task = event.task
        if seed:
            mark_seen(state, event.seen_keys, event_kind=event.kind, timestamp=detected_at, mode="seed")
            processed.append({"key": task.idempotency_key, "action": "seeded", "title": task.title})
            continue
        if dry_run:
            processed.append({"key": task.idempotency_key, "action": "dry-run", "title": task.title})
            continue
        result = create_task_with_sink(
            task,
            sink=sink,
            board=board,
            tenant=tenant,
            outbox_path=outbox_path,
        )
        mark_seen(state, event.seen_keys, event_kind=event.kind, timestamp=detected_at, mode=sink)
        append_task_record(state, task, sink=sink, timestamp=detected_at, result=result)
        processed.append({"key": task.idempotency_key, "action": result.get("sink", sink), "title": task.title})
    return {"count": len(processed), "events": processed}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=DEFAULT_REPO, help="GitHub repo in owner/name form")
    parser.add_argument("--board", default=DEFAULT_BOARD, help="Hermes Kanban board slug")
    parser.add_argument("--tenant", default=DEFAULT_TENANT, help="Hermes Kanban tenant namespace")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH, help="state JSON path")
    parser.add_argument("--outbox", type=Path, default=DEFAULT_OUTBOX_PATH, help="outbox JSONL path for sink=outbox/auto fallback")
    parser.add_argument(
        "--sink",
        choices=("auto", "hermes", "outbox", "print"),
        default="auto",
        help="where new tasks are written; auto uses hermes if installed, otherwise outbox",
    )
    parser.add_argument("--seed", action="store_true", help="mark current events seen without creating tasks")
    parser.add_argument("--dry-run", action="store_true", help="print summary without writing tasks or state")
    parser.add_argument("--no-review-threads", action="store_true", help="skip GraphQL unresolved review thread polling")
    parser.add_argument("--snapshot", type=Path, help="read a JSON snapshot instead of polling GitHub (for tests/debugging)")
    parser.add_argument("--json", action="store_true", help="print machine-readable summary")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    detected_at = utcnow_iso()
    state = load_state(args.state)
    state["repo"] = args.repo

    if args.snapshot:
        with args.snapshot.open(encoding="utf-8") as f:
            snapshot = json.load(f)
    else:
        snapshot = fetch_snapshot(args.repo, include_threads=not args.no_review_threads)

    events = collect_events(snapshot, state, repo=args.repo, detected_at=detected_at)
    summary = process_events(
        events,
        state=state,
        sink=args.sink,
        board=args.board,
        tenant=args.tenant,
        outbox_path=args.outbox,
        seed=args.seed,
        dry_run=args.dry_run,
        detected_at=detected_at,
    )
    state["last_run_at"] = detected_at
    if not args.dry_run:
        save_state(state, args.state)

    if args.json:
        print(json_dumps({"repo": args.repo, "detected_at": detected_at, **summary}, indent=2))
    else:
        action = "seeded" if args.seed else "processed"
        print(f"pr-codex watcher {action} {summary['count']} event(s) at {detected_at}")
        for item in summary["events"]:
            print(f"- {item['action']}: {item['title']} ({item['key']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
