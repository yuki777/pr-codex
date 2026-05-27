#!/usr/bin/env python3
"""Build and retrieve public-safe pr-codex episode memory artifacts."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "tasks"
if str(TASKS) not in sys.path:
    sys.path.insert(0, str(TASKS))

from learn_feedback import sanitize_text  # noqa: E402

SCHEMA_PATH = ROOT / "schemas" / "episode.v1.json"

ALLOWED_KEYS = {
    "schema_version",
    "episode_id",
    "repository",
    "source_pr_number",
    "source_head_sha",
    "source_thread_id",
    "source_url",
    "created_at",
    "stale_after_days",
    "pr_types",
    "paths",
    "finding_class",
    "signal",
    "source",
    "public_safe",
    "content",
}
REQUIRED_KEYS = ALLOWED_KEYS - {"source_url"}
CONTENT_KEYS = {"summary"}
SIGNALS = {"addressed", "superseded", "false_positive"}
RFC3339_Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
REPOSITORY_RE = re.compile(r"^[^/\s]+/[^/\s]+$")
ABSOLUTE_PATH_RE = re.compile(r"^(?:/|~|[A-Za-z]:\\)")
TRAVERSAL_PATH_RE = re.compile(r"(?:^|/)\.\.(?:/|$)")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(value: str) -> datetime:
    if not RFC3339_Z_RE.match(value):
        raise ValueError(f"not an RFC3339 UTC timestamp: {value}")
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def safe_unique_strings(values: list[str] | tuple[str, ...] | set[str], *, field: str) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = str(raw).strip()
        if not value:
            continue
        if field == "paths" and (ABSOLUTE_PATH_RE.search(value) or TRAVERSAL_PATH_RE.search(value)):
            continue
        if value not in seen:
            normalized.append(value)
            seen.add(value)
    if not normalized:
        raise ValueError(f"{field}: at least one safe value is required")
    return normalized


def episode_id_for(material: dict[str, Any]) -> str:
    digest = hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return f"episode-{digest[:32]}"


def build_episode(
    feedback_artifact: dict[str, Any],
    *,
    pr_types: list[str] | tuple[str, ...] | set[str],
    finding_class: str,
    generated_at: str | None = None,
    stale_after_days: int = 90,
) -> dict[str, Any]:
    """Convert one /pr-codex:learn artifact into a public-safe reusable episode."""

    created_at = generated_at or utc_now()
    paths = safe_unique_strings([str(feedback_artifact.get("path") or "")], field="paths")
    normalized_pr_types = safe_unique_strings(list(pr_types), field="pr_types")
    finding_class = str(finding_class or feedback_artifact.get("signal") or "general").strip()
    if not finding_class:
        raise ValueError("finding_class: required")
    excerpts = feedback_artifact.get("comment_excerpts") or []
    if not isinstance(excerpts, list):
        excerpts = [str(excerpts)]
    summary_source = "\n".join(str(item) for item in excerpts[:3]) or str(feedback_artifact.get("signal") or "episode")
    summary = sanitize_text(summary_source)[:1000] or "[REDACTED]"
    source_urls = [str(url) for url in feedback_artifact.get("urls") or [] if url]
    base = {
        "repository": feedback_artifact.get("repository"),
        "source_pr_number": feedback_artifact.get("pr_number"),
        "source_head_sha": feedback_artifact.get("head_sha"),
        "source_thread_id": feedback_artifact.get("thread_id"),
        "pr_types": normalized_pr_types,
        "paths": paths,
        "finding_class": finding_class,
        "signal": feedback_artifact.get("signal"),
    }
    episode: dict[str, Any] = {
        "schema_version": 1,
        "episode_id": episode_id_for(base),
        "repository": str(feedback_artifact.get("repository") or ""),
        "source_pr_number": int(feedback_artifact.get("pr_number") or 0),
        "source_head_sha": str(feedback_artifact.get("head_sha") or ""),
        "source_thread_id": str(feedback_artifact.get("thread_id") or ""),
        "created_at": created_at,
        "stale_after_days": int(stale_after_days),
        "pr_types": normalized_pr_types,
        "paths": paths,
        "finding_class": finding_class,
        "signal": str(feedback_artifact.get("signal") or ""),
        "source": str(feedback_artifact.get("source") or ""),
        "public_safe": True,
        "content": {"summary": summary},
    }
    if source_urls:
        episode["source_url"] = sanitize_text(source_urls[0])
    validate_episode(episode)
    return episode


def ensure_public_safe_text(value: str, *, field: str) -> None:
    """Reject strings that still contain secret-like values or local paths."""

    if sanitize_text(value) != value:
        raise ValueError(f"{field}: contains non-public-safe text")


def validate_episode(episode: dict[str, Any], *, schema: dict[str, Any] | None = None) -> None:
    """Small stdlib validator for the episode schema contract."""

    _schema = schema or load_json(SCHEMA_PATH)
    del _schema  # schema is parsed by tests; semantic checks below stay stdlib-only.
    extra = sorted(set(episode) - ALLOWED_KEYS)
    if extra:
        raise ValueError(f"unexpected properties: {', '.join(extra)}")
    missing = sorted(REQUIRED_KEYS - set(episode))
    if missing:
        raise ValueError(f"missing required properties: {', '.join(missing)}")
    if episode.get("schema_version") != 1:
        raise ValueError("schema_version must be 1")
    if not re.match(r"^episode-[0-9a-f]{32}$", str(episode.get("episode_id"))):
        raise ValueError("episode_id: invalid")
    if not REPOSITORY_RE.match(str(episode.get("repository"))):
        raise ValueError("repository: invalid")
    if not isinstance(episode.get("source_pr_number"), int) or episode["source_pr_number"] < 1:
        raise ValueError("source_pr_number: invalid")
    source_head_sha = str(episode.get("source_head_sha"))
    if not 7 <= len(source_head_sha) <= 64:
        raise ValueError("source_head_sha: invalid")
    source_thread_id = str(episode.get("source_thread_id"))
    if not source_thread_id:
        raise ValueError("source_thread_id: invalid")
    ensure_public_safe_text(source_thread_id, field="source_thread_id")
    source_url = episode.get("source_url")
    if source_url is not None:
        if not isinstance(source_url, str) or not source_url:
            raise ValueError("source_url: invalid")
        ensure_public_safe_text(source_url, field="source_url")
    parse_utc(str(episode.get("created_at")))
    if not isinstance(episode.get("stale_after_days"), int) or not 1 <= episode["stale_after_days"] <= 3650:
        raise ValueError("stale_after_days: invalid")
    for field in ("pr_types", "paths"):
        values = episode.get(field)
        if not isinstance(values, list) or not values or any(not isinstance(item, str) or not item for item in values):
            raise ValueError(f"{field}: invalid")
        if len(values) != len(set(values)):
            raise ValueError(f"{field}: duplicates are not allowed")
    if any(ABSOLUTE_PATH_RE.search(path) or TRAVERSAL_PATH_RE.search(path) for path in episode["paths"]):
        raise ValueError("paths: absolute/local/traversal paths are not allowed")
    if not str(episode.get("finding_class")):
        raise ValueError("finding_class: invalid")
    source = episode.get("source")
    if not isinstance(source, str) or not source:
        raise ValueError("source: invalid")
    ensure_public_safe_text(source, field="source")
    if episode.get("signal") not in SIGNALS:
        raise ValueError("signal: invalid")
    if episode.get("public_safe") is not True:
        raise ValueError("public_safe must be true")
    content = episode.get("content")
    if not isinstance(content, dict):
        raise ValueError("content: invalid")
    content_extra = sorted(set(content) - CONTENT_KEYS)
    if content_extra:
        raise ValueError(f"content: unexpected properties: {', '.join(content_extra)}")
    summary = content.get("summary")
    if not isinstance(summary, str) or not 1 <= len(summary) <= 1000:
        raise ValueError("content.summary: invalid")
    ensure_public_safe_text(summary, field="content.summary")


def append_episode(store: Path, episode: dict[str, Any]) -> None:
    validate_episode(episode)
    store.parent.mkdir(parents=True, exist_ok=True)
    with store.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(episode, ensure_ascii=False, sort_keys=True) + "\n")


def load_episodes(store: Path) -> list[dict[str, Any]]:
    if not store.exists():
        return []
    episodes: list[dict[str, Any]] = []
    for line_number, line in enumerate(store.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            episode = json.loads(line)
            validate_episode(episode)
        except Exception as exc:  # noqa: BLE001 - include line in CLI error
            raise ValueError(f"{store}:{line_number}: invalid episode: {exc}") from exc
        episodes.append(episode)
    return episodes


def path_matches(episode_paths: list[str], candidate_paths: list[str]) -> bool:
    """Conservative path gate: reuse only exact path episodes.

    Episode memory is meant to prevent repeated false positives, not to infer
    repository-wide policy from one file. Directory-level widening can leak
    stale or unrelated context into nearby files, so callers must write a
    separate episode for each path they want to reuse.
    """

    return bool(set(episode_paths) & set(candidate_paths))


def freshness_for(episode: dict[str, Any], *, now: str) -> str:
    age_days = (parse_utc(now) - parse_utc(str(episode["created_at"]))).days
    return "fresh" if age_days <= int(episode["stale_after_days"]) else "stale"


def retrieve_episodes(
    store: Path,
    *,
    pr_types: list[str] | tuple[str, ...] | set[str],
    paths: list[str] | tuple[str, ...] | set[str],
    finding_class: str,
    now: str | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    now = now or utc_now()
    normalized_pr_types = set(safe_unique_strings(list(pr_types), field="pr_types"))
    normalized_paths = safe_unique_strings(list(paths), field="paths")
    finding_class = str(finding_class).strip()
    results: list[dict[str, Any]] = []
    for episode in load_episodes(store):
        if not (set(episode["pr_types"]) & normalized_pr_types):
            continue
        if episode["finding_class"] != finding_class:
            continue
        if not path_matches(episode["paths"], normalized_paths):
            continue
        freshness = freshness_for(episode, now=now)
        results.append(
            {
                **episode,
                "freshness": freshness,
                "use_policy": "reverify_current_diff" if freshness == "fresh" else "context_only_reverify",
            }
        )
    results.sort(key=lambda item: (item["freshness"] != "fresh", item["created_at"]), reverse=False)
    return results[:limit]


def retrieve_for_cli(
    store: Path,
    *,
    pr_types: list[str] | tuple[str, ...] | set[str],
    paths: list[str] | tuple[str, ...] | set[str],
    finding_class: str,
    now: str | None = None,
) -> dict[str, Any]:
    episodes = retrieve_episodes(store, pr_types=pr_types, paths=paths, finding_class=finding_class, now=now)
    return {
        "schema_version": 1,
        "generated_at": now or utc_now(),
        "episode_count": len(episodes),
        "retrieval_policy": {
            "requires_pr_type_path_and_finding_class": True,
            "stale_policy": "context_only_reverify",
        },
        "episodes": episodes,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Write/read repo-local pr-codex episode memory")
    subcommands = parser.add_subparsers(dest="command", required=True)
    write = subcommands.add_parser("write")
    write.add_argument("--feedback-artifact", required=True, type=Path)
    write.add_argument("--store", required=True, type=Path)
    write.add_argument("--pr-type", action="append", required=True)
    write.add_argument("--finding-class", required=True)
    write.add_argument("--generated-at")
    write.add_argument("--stale-after-days", type=int, default=90)

    retrieve = subcommands.add_parser("retrieve")
    retrieve.add_argument("--store", required=True, type=Path)
    retrieve.add_argument("--pr-type", action="append", required=True)
    retrieve.add_argument("--path", action="append", required=True)
    retrieve.add_argument("--finding-class", required=True)
    retrieve.add_argument("--now")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "write":
        artifact = load_json(args.feedback_artifact)
        episode = build_episode(
            artifact,
            pr_types=args.pr_type,
            finding_class=args.finding_class,
            generated_at=args.generated_at,
            stale_after_days=args.stale_after_days,
        )
        append_episode(args.store, episode)
        print(json.dumps({"written": True, "episode_id": episode["episode_id"]}, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "retrieve":
        result = retrieve_for_cli(
            args.store,
            pr_types=args.pr_type,
            paths=args.path,
            finding_class=args.finding_class,
            now=args.now,
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    raise AssertionError(f"unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
