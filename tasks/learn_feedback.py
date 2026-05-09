#!/usr/bin/env python3
"""Build public-safe /pr-codex:learn feedback artifacts.

This helper intentionally learns only from explicit post-publication signals:
resolved review threads, outdated review threads, and explicit
``pr-codex/false-positive`` labels/comments.  It does not infer feedback from
silence, bot markers, or the fact that a PR was merged.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FALSE_POSITIVE_LABEL = "pr-codex/false-positive"
FALSE_POSITIVE_MARKER_RE = re.compile(rf"(?m)^\s*{re.escape(FALSE_POSITIVE_LABEL)}(?:\s*:|\s+(?=PRRT_))")
FALSE_POSITIVE_EXCLUSION_RE = re.compile(
    r"(?:^|[,;]\s*|\s+)(?:not(?:\s+for)?|except|excluding|除外|対象外)(?:\b|\s).*$",
    re.IGNORECASE,
)
DEFAULT_REVIEW_AUTHORS = frozenset({"chatgpt-codex-connector"})
CREDENTIAL_BLOCK_RE = re.compile(
    r"-----BEGIN ([A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?)-----[\s\S]*?-----END \1-----",
    re.IGNORECASE,
)
TOKEN_RE = re.compile(
    r"\b(?:authorization\s*:\s*(?:bearer|basic)\s+[^\s,;]+|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[abprs]-[A-Za-z0-9-]{20,}|(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{20,}|sk-proj-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9_-]{20,}|glpat-[A-Za-z0-9_-]{20,}|npm_[A-Za-z0-9_]{20,}|AIza[A-Za-z0-9_-]{35}|AKIA[0-9A-Z]{16}|[\"']?[A-Za-z0-9_]*(?:password|token|secret|api[_-]?key)[A-Za-z0-9_]*[\"']?\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+))",
    re.IGNORECASE,
)
COOKIE_HEADER_RE = re.compile(
    r"(?<![\w-])[\"']?(?:set-cookie|cookie)[\"']?\s*:\s*(?:\"[^\"]*\"|'[^']*'|[^\r\n]+)",
    re.IGNORECASE,
)
URL_USERINFO_RE = re.compile(r"\b([a-z][a-z0-9+.-]*://)[^\s/@:]+(?::[^\s/@]*)?@([^\s`'\")<>]+)", re.IGNORECASE)
LOCAL_PATH_COMPONENT_RE = r"(?:[^\s`'\")<>,;/\\~]+(?:\s+[^\s`'\")<>,;/\\~]+)*(?=[/\\])|[^\s`'\")<>,;/\\~]+)"
LOCAL_PATH_RE = re.compile(
    rf"(?<![\w.-])(?:~|/home|/Users|/mnt/[a-z]|/tmp|/root|/workspace|/private/var/folders|/var/folders|C:\\Users)[/\\]{LOCAL_PATH_COMPONENT_RE}(?:[/\\]{LOCAL_PATH_COMPONENT_RE})*"
)


def sanitize_text(value: str) -> str:
    """Scrub tokens and local paths from public-safe learning artifacts."""

    value = CREDENTIAL_BLOCK_RE.sub("[REDACTED_CREDENTIAL_BLOCK]", value)
    value = URL_USERINFO_RE.sub(r"\1[REDACTED_TOKEN]@\2", value)
    value = COOKIE_HEADER_RE.sub("[REDACTED_TOKEN]", value)
    value = TOKEN_RE.sub("[REDACTED_TOKEN]", value)
    value = LOCAL_PATH_RE.sub("[REDACTED_LOCAL_PATH]", value)
    return value


def sanitize_json(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, list):
        return [sanitize_json(item) for item in value]
    if isinstance(value, dict):
        return {str(key): sanitize_json(item) for key, item in value.items()}
    return value


def comments_for_thread(thread: dict[str, Any]) -> list[dict[str, Any]]:
    comments = (thread.get("comments") or {}).get("nodes") or []
    return [comment for comment in comments if isinstance(comment, dict)]


def current_pr_number(payload: dict[str, Any]) -> Any:
    return payload.get("pr_number") or payload.get("number")


def pr_keyed_list_for_payload(payload: dict[str, Any], field: str, *, allow_missing: bool = False) -> list[dict[str, Any]]:
    """Return the current PR's list from a top-level list or PR-keyed watcher snapshot field."""

    value = payload.get(field)
    if value is None:
        return []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        pr_number = current_pr_number(payload)
        candidate_keys = [pr_number, str(pr_number)]
        for key in candidate_keys:
            if key in value:
                items = value[key] or []
                if not isinstance(items, list):
                    raise ValueError(f"{field}[{key!r}] must be a list")
                return [item for item in items if isinstance(item, dict)]
        if allow_missing:
            return []
        raise ValueError(f"{field} is keyed by PR number but has no entry for pr_number={pr_number!r}")
    raise ValueError(f"{field} must be a list or a PR-number keyed dictionary")


def review_threads_for_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the current PR's review threads from list or PR-keyed snapshots."""

    return pr_keyed_list_for_payload(payload, "review_threads")


def issue_comments_for_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the current PR's issue comments from direct or watcher snapshot fields."""

    comments = pr_keyed_list_for_payload(payload, "comments", allow_missing=True)
    if comments:
        return comments
    return pr_keyed_list_for_payload(payload, "pr_issue_comments", allow_missing=True)


def current_pr_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    pr_number = current_pr_number(payload)
    for pr in payload.get("pulls") or []:
        if not isinstance(pr, dict):
            continue
        if pr.get("number") == pr_number or str(pr.get("number")) == str(pr_number):
            return pr
    return {}


def normalize_review_author_login(login: Any) -> str:
    """Return a case-insensitive GitHub login key for review-author matching."""

    return str(login).casefold() if login else ""


def configured_review_authors(payload: dict[str, Any]) -> set[str]:
    """Return GitHub logins whose review threads belong to pr-codex."""

    authors: set[str] = set()
    raw_authors = payload.get("review_authors") or payload.get("pr_codex_review_authors") or []
    if isinstance(raw_authors, str):
        raw_authors = [raw_authors]
    for author in raw_authors:
        normalized = normalize_review_author_login(author)
        if normalized:
            authors.add(normalized)
    raw_author = payload.get("review_author") or payload.get("pr_codex_review_author")
    normalized_author = normalize_review_author_login(raw_author)
    if normalized_author:
        authors.add(normalized_author)
    return authors or {normalize_review_author_login(author) for author in DEFAULT_REVIEW_AUTHORS}


def comment_author_login(comment: dict[str, Any]) -> str:
    author = comment.get("author") or comment.get("user") or {}
    if isinstance(author, dict):
        return str(author.get("login") or "")
    return ""


def is_pr_codex_review_thread(thread: dict[str, Any], *, review_authors: set[str]) -> bool:
    """Return whether a GitHub review thread originated from pr-codex."""

    comments = comments_for_thread(thread)
    first_comment = comments[0] if comments else {}
    return normalize_review_author_login(comment_author_login(first_comment)) in review_authors


def label_names(payload: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    label_sources = list(payload.get("labels") or [])
    current_pr = current_pr_from_payload(payload)
    label_sources.extend(current_pr.get("labels") or [])
    for label in label_sources:
        if isinstance(label, str):
            names.add(label)
        elif isinstance(label, dict) and label.get("name"):
            names.add(str(label["name"]))
    return names


def has_false_positive_marker(body: str) -> bool:
    """Return whether a comment intentionally marks feedback as false-positive."""

    return bool(FALSE_POSITIVE_MARKER_RE.search(body))


def false_positive_marker_thread_ids(body: str) -> set[str]:
    """Return thread ids directly targeted by false-positive marker lines."""

    thread_ids: set[str] = set()
    lines = body.splitlines()
    for index, line in enumerate(lines):
        marker = FALSE_POSITIVE_MARKER_RE.search(line)
        if not marker:
            continue
        target_lines = [line[marker.end() :]]
        for following_line in lines[index + 1 :]:
            if not following_line.strip() or FALSE_POSITIVE_MARKER_RE.search(following_line):
                break
            if not re.match(r"\s*(?:[-*+]\s+|\d+[.)]\s+)", following_line):
                break
            target_lines.append(following_line)
        for target_text in target_lines:
            first_sentence = re.split(r"[。.!?]", target_text, maxsplit=1)[0]
            positive_targets = FALSE_POSITIVE_EXCLUSION_RE.sub("", first_sentence)
            for match in re.finditer(r"PRRT_[A-Za-z0-9_-]+", positive_targets):
                thread_ids.add(match.group(0))
    return thread_ids


def explicit_false_positive_comment_map(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map targeted thread ids to trusted false-positive issue comment metadata."""
    mapping: dict[str, dict[str, Any]] = {}
    for comment in issue_comments_for_payload(payload):
        if not isinstance(comment, dict):
            continue
        body = str(comment.get("body") or "")
        mentioned = false_positive_marker_thread_ids(body)
        if not mentioned:
            continue
        if not is_trusted_false_positive_comment(comment, payload):
            continue
        for thread_id in mentioned:
            mapping[thread_id] = comment
    return mapping


TRUSTED_FALSE_POSITIVE_REPLY_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})


def repository_owner(payload: dict[str, Any]) -> str:
    repository = str(payload.get("repository") or payload.get("repo") or "")
    return repository.split("/", 1)[0]


def is_trusted_false_positive_comment(comment: dict[str, Any], payload: dict[str, Any]) -> bool:
    """Return whether a false-positive comment is maintainer-controlled."""

    association = str(comment.get("authorAssociation") or comment.get("author_association") or "").upper()
    if association in TRUSTED_FALSE_POSITIVE_REPLY_ASSOCIATIONS:
        return True
    author_login = comment_author_login(comment)
    owner_login = repository_owner(payload)
    return bool(author_login and owner_login and author_login.casefold() == owner_login.casefold())


def is_trusted_false_positive_reply(comment: dict[str, Any], payload: dict[str, Any]) -> bool:
    """Return whether an in-thread false-positive reply is maintainer-controlled."""

    return is_trusted_false_positive_comment(comment, payload)


def explicit_false_positive_review_reply_map(
    payload: dict[str, Any], review_threads: list[dict[str, Any]] | None = None
) -> dict[str, dict[str, Any]]:
    """Map thread ids to trusted in-thread false-positive reply metadata."""
    mapping: dict[str, dict[str, Any]] = {}
    threads = review_threads if review_threads is not None else review_threads_for_payload(payload)
    for thread in threads:
        thread_id = str(thread.get("id") or "")
        if not thread_id:
            continue
        for comment in comments_for_thread(thread)[1:]:
            body = str(comment.get("body") or "")
            if has_false_positive_marker(body) and is_trusted_false_positive_reply(comment, payload):
                mapping[thread_id] = comment
    return mapping


def safe_excerpt(value: Any, *, limit: int = 1000) -> str:
    """Scrub a comment body before truncating it for public artifacts."""

    return sanitize_text(str(value or ""))[:limit]


def artifact_base(payload: dict[str, Any], thread: dict[str, Any], *, signal: str, source: str) -> dict[str, Any]:
    comments = comments_for_thread(thread)
    return {
        "schema_version": 1,
        "repository": payload.get("repository") or payload.get("repo"),
        "pr_number": payload.get("pr_number") or payload.get("number"),
        "head_sha": payload.get("head_sha"),
        "thread_id": thread.get("id"),
        "signal": signal,
        "source": source,
        "path": thread.get("path"),
        "line": thread.get("line"),
        "is_resolved": bool(thread.get("isResolved")),
        "is_outdated": bool(thread.get("isOutdated")),
        "comment_ids": [comment.get("id") for comment in comments if comment.get("id") is not None],
        "comment_excerpts": [safe_excerpt(comment.get("body")) for comment in comments[:5]],
        "urls": [comment.get("url") or comment.get("html_url") for comment in comments if comment.get("url") or comment.get("html_url")],
    }


def build_feedback_learning_result(
    payload: dict[str, Any], *, generated_at: str | None = None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return ``learn-result`` and per-signal artifacts from a GitHub snapshot."""

    if generated_at is None:
        generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    review_threads = review_threads_for_payload(payload)
    review_authors = configured_review_authors(payload)
    false_positive_comments = explicit_false_positive_comment_map(payload)
    false_positive_review_replies = explicit_false_positive_review_reply_map(payload, review_threads)
    has_label_only_false_positive = (
        FALSE_POSITIVE_LABEL in label_names(payload)
        and not false_positive_comments
        and not false_positive_review_replies
    )
    artifacts: list[dict[str, Any]] = []
    ignored: list[dict[str, str]] = []

    for thread in review_threads:
        thread_id = str(thread.get("id") or "")
        if not is_pr_codex_review_thread(thread, review_authors=review_authors):
            ignored.append({"thread_id": thread_id, "reason": "not_pr_codex_review_thread"})
            continue
        if thread_id in false_positive_review_replies:
            artifact = artifact_base(payload, thread, signal="false_positive", source="review_thread_comment.false_positive")
            fp_comment = false_positive_review_replies[thread_id]
            artifact["feedback_comment_id"] = fp_comment.get("id")
            artifact["feedback_comment_url"] = fp_comment.get("html_url") or fp_comment.get("url")
            artifact["feedback_comment_excerpt"] = safe_excerpt(fp_comment.get("body"))
            artifacts.append(artifact)
        elif thread_id in false_positive_comments:
            artifact = artifact_base(payload, thread, signal="false_positive", source="label_comment.false_positive")
            fp_comment = false_positive_comments[thread_id]
            artifact["feedback_comment_id"] = fp_comment.get("id")
            artifact["feedback_comment_url"] = fp_comment.get("html_url") or fp_comment.get("url")
            artifact["feedback_comment_excerpt"] = safe_excerpt(fp_comment.get("body"))
            artifacts.append(artifact)
        elif has_label_only_false_positive and (
            thread.get("isResolved") is True or thread.get("isOutdated") is True
        ):
            artifacts.append(artifact_base(payload, thread, signal="false_positive", source="pr_label.false_positive"))
        elif thread.get("isResolved") is True:
            artifacts.append(artifact_base(payload, thread, signal="addressed", source="review_thread.resolved"))
        elif thread.get("isOutdated") is True:
            artifacts.append(artifact_base(payload, thread, signal="superseded", source="review_thread.outdated"))
        else:
            ignored.append({"thread_id": thread_id, "reason": "no_explicit_learning_signal"})

    sanitized_artifacts = [sanitize_json(artifact) for artifact in artifacts]
    summary = Counter(str(artifact["signal"]) for artifact in sanitized_artifacts)
    summary["ignored"] = len(ignored)
    result = {
        "schema_version": 1,
        "generated_at": generated_at,
        "repository": payload.get("repository") or payload.get("repo"),
        "pr_number": payload.get("pr_number") or payload.get("number"),
        "head_sha": payload.get("head_sha"),
        "artifact_count": len(sanitized_artifacts),
        "summary": {
            "addressed": summary.get("addressed", 0),
            "superseded": summary.get("superseded", 0),
            "false_positive": summary.get("false_positive", 0),
            "ignored": summary.get("ignored", 0),
        },
        "ignored_threads": ignored,
        "learning_policy": {
            "learned_signals": ["resolved thread", "outdated thread", FALSE_POSITIVE_LABEL],
            "ignored_signals": ["author silence", "merge-only", "bot/generated marker only"],
            "public_safety": ["token redaction", "local path redaction", "comment excerpt truncation"],
        },
    }
    return sanitize_json(result), sanitized_artifacts


def safe_filename_part(value: Any) -> str:
    text = str(value or "unknown")
    text = re.sub(r"[^A-Za-z0-9_.-]+", "-", text).strip("-")
    return text[:120] or "unknown"


def write_feedback_artifacts(
    payload: dict[str, Any], *, output_dir: Path, generated_at: str | None = None
) -> dict[str, Any]:
    result, artifacts = build_feedback_learning_result(payload, generated_at=generated_at)
    artifact_dir = output_dir / "feedback-artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    for stale_artifact in artifact_dir.glob("*.json"):
        stale_artifact.unlink()

    written: list[str] = []
    for artifact in artifacts:
        filename = f"{safe_filename_part(artifact['signal'])}-{safe_filename_part(artifact['thread_id'])}.json"
        path = artifact_dir / filename
        path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        written.append(str(path.relative_to(output_dir)))

    result = {**result, "artifacts": sorted(written)}
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "learn-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate /pr-codex:learn feedback artifacts from a GitHub feedback snapshot")
    parser.add_argument("--input", required=True, type=Path, help="JSON snapshot containing repository, pr_number, head_sha, review_threads, labels, comments")
    parser.add_argument("--output-dir", required=True, type=Path, help="Directory for learn-result.json and feedback-artifacts/*.json")
    parser.add_argument("--generated-at", default=None, help="Optional deterministic timestamp for tests/retries")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_path = args.input.expanduser()
    output_dir = args.output_dir.expanduser()
    payload = json.loads(input_path.read_text())
    result = write_feedback_artifacts(payload, output_dir=output_dir, generated_at=args.generated_at)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
