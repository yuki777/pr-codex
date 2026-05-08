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
from pathlib import Path
from typing import Any

FALSE_POSITIVE_LABEL = "pr-codex/false-positive"
DEFAULT_REVIEW_AUTHORS = frozenset({"chatgpt-codex-connector"})
TOKEN_RE = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-proj-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9_-]{20,}|glpat-[A-Za-z0-9_-]{20,}|[A-Za-z0-9_]*(?:token|secret|api[_-]?key)[A-Za-z0-9_]*\s*[:=]\s*[^\s,;]+)",
    re.IGNORECASE,
)
LOCAL_PATH_RE = re.compile(r"(?<![\w.-])(?:/home|/Users|/mnt/[a-z]|/tmp|C:\\Users)[^\s`'\")]+")


def sanitize_text(value: str) -> str:
    """Scrub tokens and local paths from public-safe learning artifacts."""

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


def configured_review_authors(payload: dict[str, Any]) -> set[str]:
    """Return GitHub logins whose review threads belong to pr-codex."""

    authors: set[str] = set(DEFAULT_REVIEW_AUTHORS)
    raw_authors = payload.get("review_authors") or payload.get("pr_codex_review_authors") or []
    if isinstance(raw_authors, str):
        raw_authors = [raw_authors]
    for author in raw_authors:
        if author:
            authors.add(str(author))
    raw_author = payload.get("review_author") or payload.get("pr_codex_review_author")
    if raw_author:
        authors.add(str(raw_author))
    return authors


def comment_author_login(comment: dict[str, Any]) -> str:
    author = comment.get("author") or comment.get("user") or {}
    if isinstance(author, dict):
        return str(author.get("login") or "")
    return ""


def is_pr_codex_review_thread(thread: dict[str, Any], *, review_authors: set[str]) -> bool:
    """Return whether a GitHub review thread originated from pr-codex."""

    comments = comments_for_thread(thread)
    first_comment = comments[0] if comments else {}
    return comment_author_login(first_comment) in review_authors


def label_names(payload: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for label in payload.get("labels") or []:
        if isinstance(label, str):
            names.add(label)
        elif isinstance(label, dict) and label.get("name"):
            names.add(str(label["name"]))
    return names


def explicit_false_positive_comment_map(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map mentioned thread ids to their false-positive comment metadata."""

    mapping: dict[str, dict[str, Any]] = {}
    for comment in payload.get("comments") or []:
        if not isinstance(comment, dict):
            continue
        body = str(comment.get("body") or "")
        if FALSE_POSITIVE_LABEL not in body:
            continue
        mentioned = set(re.findall(r"PRRT_[A-Za-z0-9_-]+", body))
        for thread_id in mentioned:
            mapping[thread_id] = comment
    return mapping


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
        "comment_excerpts": [str(comment.get("body") or "")[:1000] for comment in comments[:5]],
        "urls": [comment.get("url") or comment.get("html_url") for comment in comments if comment.get("url") or comment.get("html_url")],
    }


def build_feedback_learning_result(
    payload: dict[str, Any], *, generated_at: str | None = None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return ``learn-result`` and per-signal artifacts from a GitHub snapshot."""

    labels = label_names(payload)
    review_authors = configured_review_authors(payload)
    false_positive_comments = explicit_false_positive_comment_map(payload)
    artifacts: list[dict[str, Any]] = []
    ignored: list[dict[str, str]] = []

    for thread in payload.get("review_threads") or []:
        if not isinstance(thread, dict):
            continue
        thread_id = str(thread.get("id") or "")
        if not is_pr_codex_review_thread(thread, review_authors=review_authors):
            ignored.append({"thread_id": thread_id, "reason": "not_pr_codex_review_thread"})
            continue
        if FALSE_POSITIVE_LABEL in labels and thread_id in false_positive_comments:
            artifact = artifact_base(payload, thread, signal="false_positive", source="label_comment.false_positive")
            fp_comment = false_positive_comments[thread_id]
            artifact["feedback_comment_id"] = fp_comment.get("id")
            artifact["feedback_comment_url"] = fp_comment.get("html_url") or fp_comment.get("url")
            artifact["feedback_comment_excerpt"] = str(fp_comment.get("body") or "")[:1000]
            artifacts.append(artifact)
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
    payload = json.loads(args.input.read_text())
    result = write_feedback_artifacts(payload, output_dir=args.output_dir, generated_at=args.generated_at)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
