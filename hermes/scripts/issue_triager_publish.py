#!/usr/bin/env python3
"""Dry-run/default-off publisher for Hermes issue-triager comments.

Phase 1B defines when an issue-triager conclusion is safe to publish to a
GitHub Issue.  The default behavior is recommendation-only dry-run reporting;
real GitHub writes require both ``--publish --sink github`` and
``PR_CODEX_HERMES_ISSUE_TRIAGE_PUBLISH=1``.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any, Iterable, Protocol

from _pr_codex_common import (  # type: ignore[import-not-found]
    DEFAULT_OUTBOX_PATH,
    DEFAULT_REPO,
    DEFAULT_STATE_PATH,
    ISSUE_TRIAGE_SENTINEL_VERSION,
    append_jsonl,
    build_issue_triage_sentinel,
    gh_json,
    is_trusted_hermes_auto_item,
    json_dumps,
    load_state,
    mark_seen,
    parse_issue_triage_sentinels,
    public_text_has_substance,
    save_state,
    scrub_for_public,
    seen_contains,
    triage_publish_content_hash,
    triage_publish_idempotency_key,
)

PUBLISH_ENV_FLAG = "PR_CODEX_HERMES_ISSUE_TRIAGE_PUBLISH"
POLICY_PHASE = "1b-issue-triage-publication-policy"
MAX_PUBLIC_FIELD_CHARS = 800
MAX_LIST_ITEMS = 8
ALLOWED_CLASSIFICATIONS = frozenset({"bug", "feature", "docs", "infra", "other"})
ALLOWED_PRIORITIES = frozenset({"low", "medium", "high", "critical", "urgent", "p0", "p1", "p2", "p3", "p4"})
SAFE_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._/-]{0,49}$")
SAFE_PUBLIC_TEXT_FORBIDDEN_CHARS_RE = re.compile(r"[{}<>`$|\\]")
SAFE_PUBLIC_TEXT_FORBIDDEN_TERMS_RE = re.compile(
    r"(?i)\b("
    r"api[_ -]?key|authorization|bearer|credential|password|private[_ -]?path|raw[_ -]?(?:log|payload)|"
    r"secret|stack[_ -]?trace|token|graphql|rest[_ -]?payload|"
    r"hermes[_ -]?task|profile[_ -]?session"
    r")\b|/Users/|/home/|~/.hermes|\.agent-orchestrator|t_[0-9a-f]{8,}|pc-\d+"
)


class IssueCommentClient(Protocol):
    def list_issue_comments(self, repo: str, issue: int) -> list[dict[str, Any]]: ...

    def add_issue_comment(self, repo: str, issue: int, body: str) -> dict[str, Any]: ...


class GitHubIssueCommentClient:
    """Tiny GitHub Issues comments client using ``gh api`` only."""

    def list_issue_comments(self, repo: str, issue: int) -> list[dict[str, Any]]:
        owner, name = split_repo(repo)
        comments: list[dict[str, Any]] = []
        page = 1
        while True:
            payload = gh_json([f"repos/{owner}/{name}/issues/{issue}/comments?per_page=100&page={page}"]) or []
            if not isinstance(payload, list):
                raise RuntimeError("expected GitHub issue comments API to return a list")
            comments.extend(payload)
            if len(payload) < 100:
                return comments
            page += 1

    def add_issue_comment(self, repo: str, issue: int, body: str) -> dict[str, Any]:
        owner, name = split_repo(repo)
        payload = gh_json([
            "--method",
            "POST",
            f"repos/{owner}/{name}/issues/{issue}/comments",
            "-f",
            f"body={body}",
        ])
        return payload if isinstance(payload, dict) else {"response": payload}


def split_repo(repo: str) -> tuple[str, str]:
    owner, _, name = repo.partition("/")
    if not owner or not name:
        raise ValueError(f"repo must be owner/name, got {repo!r}")
    return owner, name


def _first_present(payload: dict[str, Any], names: Iterable[str]) -> Any:
    for name in names:
        if name in payload and payload[name] not in (None, ""):
            return payload[name]
    for container_name in ("metadata", "issue", "triage"):
        container = payload.get(container_name)
        if not isinstance(container, dict):
            continue
        for name in names:
            if name in container and container[name] not in (None, ""):
                return container[name]
    return None


def _scrub_value(value: Any, *, max_chars: int = MAX_PUBLIC_FIELD_CHARS) -> tuple[str, list[str]]:
    text, redactions = scrub_for_public("" if value is None else str(value), max_chars=max_chars)
    return text, redactions


def _append_omission(omissions: list[dict[str, str]], field: str, reason: str) -> None:
    omissions.append({"field": field, "reason": reason})


def _policy_text_value(
    value: Any,
    *,
    field: str,
    omissions: list[dict[str, str]],
    redactions: list[str],
    max_chars: int = MAX_PUBLIC_FIELD_CHARS,
    max_lines: int = 3,
) -> str | None:
    """Return a public-safe free-text field only if it passes the Phase 1B policy."""

    if value in (None, ""):
        return None
    raw = str(value)
    if len(raw) > max_chars:
        _append_omission(omissions, field, "overlong")
        return None
    if raw.count("\n") + 1 > max_lines:
        _append_omission(omissions, field, "too_many_lines")
        return None
    text, categories = _scrub_value(raw, max_chars=max_chars)
    redactions.extend(categories)
    if categories:
        _append_omission(omissions, field, "redacted_content")
        return None
    if not public_text_has_substance(text):
        _append_omission(omissions, field, "no_public_substance")
        return None
    if SAFE_PUBLIC_TEXT_FORBIDDEN_CHARS_RE.search(text):
        _append_omission(omissions, field, "forbidden_characters")
        return None
    if SAFE_PUBLIC_TEXT_FORBIDDEN_TERMS_RE.search(text):
        _append_omission(omissions, field, "forbidden_public_terms")
        return None
    if "http://" in text.lower() or "https://" in text.lower():
        _append_omission(omissions, field, "urls_not_allowed_in_free_text")
        return None
    return text


def _policy_enum_value(
    value: Any,
    *,
    field: str,
    allowed: frozenset[str],
    omissions: list[dict[str, str]],
) -> str | None:
    if value in (None, ""):
        return None
    normalized = str(value).strip().lower()
    if normalized not in allowed:
        _append_omission(omissions, field, "not_allowlisted")
        return None
    return normalized


def _policy_labels(
    value: Any,
    *,
    omissions: list[dict[str, str]],
    redactions: list[str],
    max_items: int = MAX_LIST_ITEMS,
) -> list[str]:
    if value in (None, ""):
        return []
    items = value if isinstance(value, (list, tuple, set)) else [value]
    labels: list[str] = []
    for item in items:
        text, categories = _scrub_value(item, max_chars=50)
        redactions.extend(categories)
        if categories:
            _append_omission(omissions, "suggested_labels", "redacted_content")
            continue
        if not SAFE_LABEL_RE.fullmatch(text):
            _append_omission(omissions, "suggested_labels", "invalid_label_shape")
            continue
        if SAFE_PUBLIC_TEXT_FORBIDDEN_TERMS_RE.search(text):
            _append_omission(omissions, "suggested_labels", "forbidden_public_terms")
            continue
        if text not in labels:
            labels.append(text)
        if len(labels) >= max_items:
            break
    return labels


def _normalize_text_list(value: Any, *, max_chars: int = 120, max_items: int = MAX_LIST_ITEMS) -> tuple[list[str], list[str]]:
    if value in (None, ""):
        return [], []
    items = value if isinstance(value, (list, tuple, set)) else [value]
    normalized: list[str] = []
    redactions: list[str] = []
    for item in items:
        text, categories = _scrub_value(item, max_chars=max_chars)
        redactions.extend(categories)
        if text and text not in normalized:
            normalized.append(text)
        if len(normalized) >= max_items:
            break
    return normalized, sorted(set(redactions))


def _normalize_issue_ref(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, int):
        return f"#{value}"
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return f"#{text}"
    if text.startswith("#") and text[1:].isdigit():
        return text
    marker = "/issues/"
    if "github.com/" in text and marker in text:
        number = text.rsplit(marker, 1)[-1].split("/", 1)[0].split("#", 1)[0]
        if number.isdigit():
            return f"#{number}"
    if "/" in text and "#" in text:
        owner_repo, number = text.rsplit("#", 1)
        if owner_repo and number.isdigit() and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", owner_repo):
            return f"{owner_repo}#{number}"
    return None


def _normalize_issue_refs(
    value: Any,
    *,
    field: str,
    omissions: list[dict[str, str]],
    max_items: int = MAX_LIST_ITEMS,
) -> list[str]:
    if value in (None, ""):
        return []
    items = value if isinstance(value, (list, tuple, set)) else [value]
    refs: list[str] = []
    for item in items:
        ref = _normalize_issue_ref(item)
        if not ref:
            _append_omission(omissions, field, "invalid_issue_ref")
            continue
        if ref not in refs:
            refs.append(ref)
        if len(refs) >= max_items:
            break
    return refs


def _bool_or_none(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "ready", "1"}:
            return True
        if lowered in {"false", "no", "blocked", "0"}:
            return False
    return None


def render_issue_triage_body(payload: dict[str, Any]) -> dict[str, Any]:
    """Render the scrubbed body without the sentinel and report safety metadata."""

    redactions: list[str] = []
    policy_omissions: list[dict[str, str]] = []
    lines = [
        "## Hermes issue triage",
        "",
        "Recommendation only. No labels, milestones, assignees, title edits, locks, or close/reopen actions were changed.",
    ]
    public_values_for_substance: list[str] = []

    classification = _policy_enum_value(
        _first_present(payload, ("classification", "category")),
        field="classification",
        allowed=ALLOWED_CLASSIFICATIONS,
        omissions=policy_omissions,
    )
    if classification:
        lines.append(f"- Classification: `{classification}`")
        public_values_for_substance.append(classification)

    priority = _policy_enum_value(
        _first_present(payload, ("priority",)),
        field="priority",
        allowed=ALLOWED_PRIORITIES,
        omissions=policy_omissions,
    )
    if priority:
        lines.append(f"- Priority: `{priority}`")
        public_values_for_substance.append(priority)

    labels = _policy_labels(
        _first_present(payload, ("suggested_labels",)),
        omissions=policy_omissions,
        redactions=redactions,
    )
    if labels:
        lines.append(f"- Suggested labels (proposal only): {', '.join(f'`{label}`' for label in labels)}")
        public_values_for_substance.extend(labels)

    ready = _bool_or_none(_first_present(payload, ("ready",)))
    blocked_by = _normalize_issue_refs(
        _first_present(payload, ("blocked_by",)),
        field="blocked_by",
        omissions=policy_omissions,
    )
    dependencies = _normalize_issue_refs(
        _first_present(payload, ("dependencies", "depends_on")),
        field="dependencies",
        omissions=policy_omissions,
    )
    duplicate_of = _normalize_issue_refs(
        _first_present(payload, ("duplicate_of",)),
        field="duplicate_of",
        omissions=policy_omissions,
    )
    related = _normalize_issue_refs(
        _first_present(payload, ("related_issues", "related")),
        field="related_issues",
        omissions=policy_omissions,
    )

    if duplicate_of or related:
        refs = []
        if duplicate_of:
            refs.append("duplicate: " + ", ".join(duplicate_of))
        if related:
            refs.append("related: " + ", ".join(related))
        lines.append(f"- Duplicate/related Issues: {'; '.join(refs)}")
        public_values_for_substance.extend(duplicate_of + related)

    if dependencies:
        lines.append(f"- Dependencies: {', '.join(dependencies)}")
        public_values_for_substance.extend(dependencies)
    if blocked_by:
        lines.append(f"- Blocked by: {', '.join(blocked_by)}")
        public_values_for_substance.extend(blocked_by)

    if ready is not None:
        status = "ready" if ready else "blocked"
        lines.append(f"- Ready/blocked: `{status}`")
        public_values_for_substance.append(status)

    if _first_present(payload, ("public_summary", "summary")) not in (None, ""):
        _append_omission(omissions=policy_omissions, field="summary", reason="not_allowlisted")

    next_action_text = _policy_text_value(
        _first_present(payload, ("recommended_next_action", "next_action")),
        field="recommended_next_action",
        omissions=policy_omissions,
        redactions=redactions,
        max_lines=3,
    )
    if next_action_text:
        lines.extend(["", f"Recommended next action: {next_action_text}"])
        public_values_for_substance.append(next_action_text)

    lines.extend(
        [
            "",
            "Public-safety note: raw logs, credentials, tokens, local private paths, and private Hermes operational details are omitted or redacted before publication.",
        ]
    )
    body = "\n".join(lines).rstrip() + "\n"
    # Final pass catches accidental secrets introduced by formatting.
    body, body_redactions = scrub_for_public(body, max_chars=4_000)
    redactions.extend(body_redactions)
    has_substance = any(public_text_has_substance(value) for value in public_values_for_substance)
    return {
        "body_without_sentinel": body + "\n",
        "redactions": sorted(set(redactions)),
        "policy_omissions": policy_omissions,
        "has_public_substance": has_substance,
        "rendered_from_allowlisted_fields": True,
    }


def normalize_issue_number(payload: dict[str, Any], explicit_issue: int | None = None) -> int:
    if explicit_issue is not None:
        return int(explicit_issue)
    raw = _first_present(payload, ("issue_number", "number", "issue"))
    if isinstance(raw, dict):
        raw = raw.get("number")
    if raw is None:
        raise ValueError("issue number is required; pass --issue or include issue_number in triage JSON")
    return int(raw)


def normalize_repo(payload: dict[str, Any], default_repo: str) -> str:
    return str(_first_present(payload, ("repo", "repository", "repository_full_name")) or default_repo)


def existing_issue_triage_comment(
    comments: Iterable[dict[str, Any]],
    *,
    repo: str,
    scrub_hash: str,
) -> dict[str, Any] | None:
    """Find a trusted existing Hermes issue-triage comment with the same hash."""

    for comment in comments:
        if not is_trusted_hermes_auto_item(comment, repo=repo):
            continue
        for sentinel in parse_issue_triage_sentinels(comment.get("body")):
            if sentinel.get("version") == ISSUE_TRIAGE_SENTINEL_VERSION and sentinel.get("hash") == scrub_hash:
                return comment
    return None


def build_publication_plan(
    payload: dict[str, Any],
    *,
    repo: str,
    issue: int,
    comments: Iterable[dict[str, Any]],
    state: dict[str, Any],
) -> dict[str, Any]:
    rendered = render_issue_triage_body(payload)
    if not rendered["has_public_substance"]:
        omission_reasons = {item["reason"] for item in rendered["policy_omissions"]}
        if rendered["redactions"] and omission_reasons <= {"redacted_content"}:
            skip_reason = "all-redacted"
        else:
            skip_reason = "no-policy-approved-content" if rendered["policy_omissions"] else "all-redacted"
        return {
            "phase": POLICY_PHASE,
            "policy_version": ISSUE_TRIAGE_SENTINEL_VERSION,
            "repo": repo,
            "issue_number": issue,
            "action": "skip",
            "skip_reason": skip_reason,
            "redactions": rendered["redactions"],
            "policy_omissions": rendered["policy_omissions"],
            "body": rendered["body_without_sentinel"],
            "github_writes_enabled": False,
        }

    scrub_hash = triage_publish_content_hash(rendered["body_without_sentinel"])
    idempotency_key = triage_publish_idempotency_key(issue, scrub_hash)
    sentinel = build_issue_triage_sentinel(repo=repo, issue_number=issue, scrub_hash=scrub_hash)
    body = f"{sentinel}\n\n{rendered['body_without_sentinel']}"
    duplicate = existing_issue_triage_comment(comments, repo=repo, scrub_hash=scrub_hash)
    state_duplicate = seen_contains(state, idempotency_key)
    base = {
        "phase": POLICY_PHASE,
        "policy_version": ISSUE_TRIAGE_SENTINEL_VERSION,
        "repo": repo,
        "issue_number": issue,
        "scrub_hash": scrub_hash,
        "idempotency_key": idempotency_key,
        "sentinel": sentinel,
        "body": body,
        "redactions": rendered["redactions"],
        "policy_omissions": rendered["policy_omissions"],
        "rendered_from_allowlisted_fields": True,
        "github_writes_enabled": False,
    }
    if duplicate or state_duplicate:
        return {
            **base,
            "action": "skip",
            "skip_reason": "already-published",
            "duplicate_comment_id": (duplicate or {}).get("id") or (duplicate or {}).get("node_id"),
            "state_duplicate": state_duplicate,
        }
    return {**base, "action": "pending", "skip_reason": None}


def publish_issue_triage(
    payload: dict[str, Any],
    *,
    state: dict[str, Any],
    repo: str = DEFAULT_REPO,
    issue: int | None = None,
    comments: Iterable[dict[str, Any]] | None = None,
    client: IssueCommentClient | None = None,
    dry_run: bool = True,
    sink: str = "print",
    env: dict[str, str] | None = None,
    outbox_path: Path = DEFAULT_OUTBOX_PATH,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """Evaluate and optionally append an issue-triager GitHub comment."""

    effective_repo = normalize_repo(payload, repo)
    effective_issue = normalize_issue_number(payload, issue)
    effective_env = os.environ if env is None else env
    effective_client = client or GitHubIssueCommentClient()
    fetched_comments = list(comments) if comments is not None else effective_client.list_issue_comments(effective_repo, effective_issue)
    plan = build_publication_plan(
        payload,
        repo=effective_repo,
        issue=effective_issue,
        comments=fetched_comments,
        state=state,
    )
    enabled = effective_env.get(PUBLISH_ENV_FLAG) == "1"
    plan = {**plan, "dry_run": dry_run, "sink": sink, "github_writes_enabled": enabled}

    if plan["action"] == "skip":
        if plan.get("skip_reason") == "already-published" and not dry_run and plan.get("idempotency_key"):
            mark_seen(
                state,
                [plan["idempotency_key"]],
                event_kind="issue_triage:publish",
                timestamp=timestamp or "deduped-existing-comment",
                mode="dedupe",
            )
        return plan

    if dry_run:
        return {**plan, "action": "dry-run", "skip_reason": "dry-run" if enabled else "disabled"}

    if not enabled:
        return {**plan, "action": "skip", "skip_reason": "disabled"}

    if sink == "github":
        response = effective_client.add_issue_comment(effective_repo, effective_issue, plan["body"])
        mark_seen(
            state,
            [plan["idempotency_key"]],
            event_kind="issue_triage:publish",
            timestamp=timestamp or "published",
            mode="github",
        )
        return {**plan, "action": "published", "skip_reason": None, "result": response}

    if sink == "outbox":
        append_jsonl(outbox_path, {"kind": "issue_triage_publish", **plan})
        return {**plan, "action": "outbox", "skip_reason": "outbox-only"}

    if sink == "print":
        return {**plan, "action": "would-post", "skip_reason": "print-sink"}

    raise ValueError(f"unknown sink: {sink}")


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def load_comments(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    payload = load_json(path)
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        comments = payload.get("comments") or payload.get("items") or []
        if isinstance(comments, list):
            return comments
    raise ValueError(f"comments file must be a JSON list or object with comments/items: {path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--triage", type=Path, help="structured issue-triager handoff JSON")
    parser.add_argument("--issue", type=int, help="GitHub issue number; overrides triage JSON")
    parser.add_argument("--repo", default=DEFAULT_REPO, help="GitHub repo in owner/name form")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH, help="publisher/watcher state JSON path")
    parser.add_argument("--comments", type=Path, help="existing issue comments JSON for duplicate detection")
    parser.add_argument(
        "--fetch-comments",
        action="store_true",
        help="fetch comments from GitHub for duplicate detection; implied for --publish --sink github",
    )
    parser.add_argument("--outbox", type=Path, default=DEFAULT_OUTBOX_PATH, help="outbox JSONL path for sink=outbox")
    parser.add_argument("--sink", choices=("print", "outbox", "github"), default="print")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", default=True, help="do not write to GitHub (default)")
    parser.add_argument("--publish", dest="dry_run", action="store_false", help="allow writes when env flag and sink permit it")
    parser.add_argument("--json", action="store_true", help="print machine-readable report")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = load_json(args.triage) if args.triage else {}
    state = load_state(args.state)
    client = GitHubIssueCommentClient()
    comments: list[dict[str, Any]] | None
    if args.comments:
        comments = load_comments(args.comments)
    elif args.fetch_comments or (not args.dry_run and args.sink == "github"):
        repo = normalize_repo(payload, args.repo)
        issue = normalize_issue_number(payload, args.issue)
        comments = client.list_issue_comments(repo, issue)
    else:
        comments = []
    report = publish_issue_triage(
        payload,
        state=state,
        repo=args.repo,
        issue=args.issue,
        comments=comments,
        client=client,
        dry_run=args.dry_run,
        sink=args.sink,
        outbox_path=args.outbox,
    )
    if not args.dry_run and report["action"] in {"published", "skip"}:
        save_state(state, args.state)
    if args.json:
        print(json_dumps(report, indent=2))
    else:
        print(
            f"issue-triager publish {report['action']} "
            f"#{report['issue_number']} {report.get('idempotency_key', '')}".rstrip()
        )
        if report.get("skip_reason"):
            print(f"skip_reason: {report['skip_reason']}")
        if report.get("body"):
            print("\n--- candidate body ---")
            print(report["body"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
