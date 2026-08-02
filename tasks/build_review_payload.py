#!/usr/bin/env python3
"""Build deterministic GitHub review payloads and verify payload manifests."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SECTION_RE = re.compile(r"^##\s+", re.MULTILINE)
RANGE_RE = re.compile(r"^L(?P<start>\d+)-L(?P<end>\d+)$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MUST_FIX_HEADING = "## 重大な問題 (Must Fix)"
DEFAULT_REVIEW_SCOPE = "2者レビュー (Claude/Codex hunter) + verifier 3軸 gate"
PR_CODEX_REPO_URL = "https://github.com/yuki777/pr-codex"
# send Step 4.5 semantic preflight engine. Must match the `-m` /
# `model_reasoning_effort` literals in skills/send/SKILL.md; the pairing is
# enforced by tasks/test_issue124_docs.py.
SEMANTIC_VERIFIER_ENGINE = ("Codex", "gpt-5.6-sol", "high")
# Human-facing effort labels: each CLI's maximum tier is normalized to
# "max" in the posted footer, while config/metadata keep the exact literal
# (Claude CLI: max; Codex CLI: xhigh — Codex has no "max" value).
EFFORT_DISPLAY_LABELS = {"xhigh": "max"}
MANIFEST_REQUIRED_ROLES = ("findings", "review", "metadata", "ranges", "payload")
MANIFEST_OPTIONAL_ROLES = ("sarif", "diff", "ci_status", "run_plan", "ci_summary")
MANIFEST_COUNT_KEYS = (
    "must_fix_total",
    "must_fix_inline",
    "must_fix_body",
    "must_fix_withheld",
    "should_fix_inline",
    "nit_inline",
)
MANIFEST_EVENTS = {"REQUEST_CHANGES", "COMMENT", "APPROVE"}
GENERATED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")
REPLAY_BYTE_ROLES = {
    "findings",
    "review",
    "metadata",
    "ranges",
    "payload",
    "ci_status",
    "run_plan",
    "ci_summary",
}



class BuildError(Exception):
    """Report one or more invalid build inputs without a traceback."""

    def __init__(self, errors: str | list[str]) -> None:
        self.errors = [errors] if isinstance(errors, str) else errors
        super().__init__(self.errors[0] if self.errors else "invalid input")


def resolved(path: Path) -> str:
    """Return a stable absolute path without requiring the target to exist."""

    return str(path.expanduser().resolve())


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_required(path: Path, label: str, snapshots: dict[str, str]) -> bytes:
    try:
        data = path.read_bytes()
    except Exception as exc:  # noqa: BLE001 - CLI reports path failures uniformly
        raise BuildError(f"{label}: cannot read {path}: {exc}") from exc
    snapshots[resolved(path)] = sha256_bytes(data)
    return data


def read_optional(path: Path | None, snapshots: dict[str, str]) -> bytes | None:
    if path is None:
        return None
    try:
        data = path.read_bytes()
    except Exception:  # noqa: BLE001 - optional runtime artifacts use the unavailable fallback
        return None
    snapshots[resolved(path)] = sha256_bytes(data)
    return data


def hash_optional(path: Path | None, snapshots: dict[str, str]) -> bool:
    if path is None:
        return False
    try:
        digest = sha256_file(path)
        has_content = path.stat().st_size > 0
    except Exception:  # noqa: BLE001 - optional runtime artifacts use the unavailable fallback
        return False
    snapshots[resolved(path)] = digest
    return has_content


def decode_required(data: bytes, path: Path, label: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BuildError(f"{label}: {path}: must be UTF-8: {exc}") from exc


def parse_required_json(data: bytes, path: Path, label: str) -> Any:
    try:
        return json.loads(decode_required(data, path, label))
    except json.JSONDecodeError as exc:
        raise BuildError(f"{label}: cannot parse JSON {path}: {exc}") from exc


def parse_optional_json(data: bytes | None) -> Any | None:
    if data is None:
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def decode_optional(data: bytes | None) -> str | None:
    if data is None:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def markdown_section(markdown: str, heading: str) -> str:
    heading_re = re.compile(rf"^{re.escape(heading)}[ \t]*$", re.MULTILINE)
    match = heading_re.search(markdown)
    if match is None:
        return ""
    body_start = match.end()
    if body_start < len(markdown) and markdown[body_start] == "\r":
        body_start += 1
    if body_start < len(markdown) and markdown[body_start] == "\n":
        body_start += 1
    next_heading = SECTION_RE.search(markdown, body_start)
    body_end = next_heading.start() if next_heading else len(markdown)
    return markdown[body_start:body_end].strip()


def parse_ranges(text: str, path: Path) -> dict[str, list[tuple[int, int]]]:
    ranges: dict[str, list[tuple[int, int]]] = {}
    errors: list[str] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        if not raw_line.strip():
            continue
        try:
            file_path, range_text = raw_line.split("\t", 1)
        except ValueError:
            errors.append(f"{path}:{line_number}: expected '<path>\\tL<start>-L<end>'")
            continue
        match = RANGE_RE.fullmatch(range_text)
        if not file_path or match is None:
            errors.append(f"{path}:{line_number}: expected '<path>\\tL<start>-L<end>'")
            continue
        start = int(match.group("start"))
        end = int(match.group("end"))
        if start < 1 or end < start:
            errors.append(f"{path}:{line_number}: invalid line range")
            continue
        ranges.setdefault(file_path, []).append((start, end))
    if errors:
        raise BuildError(errors)
    return ranges


def range_contains(ranges: dict[str, list[tuple[int, int]]], path: str, start: int, end: int) -> bool:
    return any(hunk_start <= start <= hunk_end and hunk_start <= end <= hunk_end for hunk_start, hunk_end in ranges.get(path, []))


def security_disclosure_policy(finding: dict[str, Any]) -> Any:
    if finding.get("category") != "security":
        return None
    security = finding.get("security")
    return security.get("disclosure_policy") if isinstance(security, dict) else None


def security_requires_body(finding: dict[str, Any]) -> bool:
    if finding.get("category") != "security":
        return False
    security = finding.get("security")
    if not isinstance(security, dict):
        return False
    disclosure_policy = security.get("disclosure_policy")
    if disclosure_policy == "body_summary_safe":
        return True
    return security.get("severity") in {"critical", "high"} and disclosure_policy not in {"inline_safe", "local_only"}


def withheld_reason(finding: dict[str, Any]) -> str | None:
    if finding.get("severity") != "must_fix" or finding.get("category") != "security":
        return None
    posting = finding.get("posting")
    posting = posting if isinstance(posting, dict) else {}
    if posting.get("post_policy") == "suppress":
        return "suppress"
    if posting.get("post_policy") == "local_only" or security_disclosure_policy(finding) == "local_only":
        return "local_only"
    return None


def validate_build_inputs(findings_data: Any, metadata: Any, markdown: str) -> tuple[list[dict[str, Any]], int]:
    errors: list[str] = []
    if not isinstance(findings_data, dict):
        raise BuildError("findings: top-level value must be an object")
    if findings_data.get("schema_version") != "findings.v1":
        errors.append("findings.schema_version: must equal findings.v1")
    producer = findings_data.get("producer")
    producer = producer if isinstance(producer, dict) else {}
    producer_version = producer.get("version")
    if not isinstance(producer_version, str) or not producer_version.strip():
        errors.append("findings.producer.version: must be a non-empty string")
    raw_findings = findings_data.get("findings")
    if not isinstance(raw_findings, list):
        errors.append("findings: must be an array")
        raw_findings = []
    canonical_findings: list[dict[str, Any]] = []
    for index, raw_finding in enumerate(raw_findings):
        if not isinstance(raw_finding, dict):
            errors.append(f"findings[{index}]: must be an object")
            continue
        canonical_findings.append(raw_finding)

    if not isinstance(metadata, dict):
        errors.append("metadata: top-level value must be an object")
        metadata = {}
    expected_repository = None
    org = metadata.get("org")
    repository = metadata.get("repository")
    if isinstance(org, str) and isinstance(repository, str):
        expected_repository = f"{org}/{repository}"
    if metadata.get("repository_full_name") != expected_repository:
        errors.append("metadata.repository_full_name: must equal '<org>/<repository>'")

    engines = metadata.get("review_engines")
    if not isinstance(engines, list) or not engines:
        errors.append("metadata.review_engines: must be a non-empty array")
    else:
        for index, engine in enumerate(engines):
            if not isinstance(engine, dict):
                errors.append(f"metadata.review_engines[{index}]: must be an object")
                continue
            for key in ("name", "model", "effort"):
                value = engine.get(key)
                if not isinstance(value, str) or not value.strip():
                    errors.append(f"metadata.review_engines[{index}].{key}: must be a non-empty string")

    pr = findings_data.get("pr")
    if not isinstance(pr, dict):
        errors.append("findings.pr: must be an object")
        pr = {}
    for findings_key, metadata_key in (
        ("repository", "repository_full_name"),
        ("number", "pr_number"),
        ("head_sha", "head_sha"),
        ("base_sha", "base_sha"),
    ):
        if pr.get(findings_key) != metadata.get(metadata_key):
            errors.append(f"findings.pr.{findings_key}: must match metadata.{metadata_key}")

    for index, item in enumerate(canonical_findings):
        severity = item.get("severity")
        posting = item.get("posting")
        posting = posting if isinstance(posting, dict) else {}
        location = item.get("location")
        location = location if isinstance(location, dict) else {}
        category = item.get("category")
        security = item.get("security")
        if category == "security" and not isinstance(security, dict):
            errors.append(f"findings[{index}].security: security extension is required")
        high_risk_security = category == "security" and isinstance(security, dict) and security.get("severity") in {"critical", "high"}
        if high_risk_security and posting.get("post_policy") == "inline":
            errors.append(f"findings[{index}].posting.post_policy: high-risk security findings must not use post_policy=inline")
        if severity != "must_fix" and posting.get("post_policy") == "inline":
            errors.append(f"findings[{index}].posting.post_policy: only must_fix findings may use post_policy=inline")
        if severity != "must_fix" or security_requires_body(item) or withheld_reason(item) is not None:
            continue
        if posting.get("post_policy") != "inline":
            errors.append(f"findings[{index}].posting.post_policy: must_fix findings must use post_policy=inline")
        if posting.get("explanation_postable") is not True:
            errors.append(f"findings[{index}].posting.explanation_postable: must_fix findings must set explanation_postable=true")
        if location.get("side") != "RIGHT":
            errors.append(f"findings[{index}].location.side: must_fix findings must target location.side=RIGHT")

    must_fix_count = sum(item.get("severity") == "must_fix" for item in canonical_findings)
    must_fix_markdown = markdown_section(markdown, MUST_FIX_HEADING)
    markdown_count = sum(line.startswith("### ") for line in must_fix_markdown.splitlines()) if must_fix_markdown else 0
    if markdown_count != must_fix_count:
        errors.append(f"review.md Must Fix heading count ({markdown_count}) must equal findings must_fix count ({must_fix_count})")
    if errors:
        raise BuildError(errors)
    return canonical_findings, must_fix_count


def finding_location(finding: dict[str, Any]) -> tuple[str, int, int, bool, str]:
    location = finding.get("location")
    location = location if isinstance(location, dict) else {}
    path = location.get("path")
    start = location.get("start_line")
    end_value = location.get("end_line")
    side = location.get("side")
    if not isinstance(path, str) or not path:
        raise BuildError(f"finding {finding.get('id', '<unknown>')}: location.path must be a non-empty string")
    if not isinstance(start, int) or isinstance(start, bool) or start < 1:
        raise BuildError(f"finding {finding.get('id', '<unknown>')}: location.start_line must be an integer >= 1")
    multiline = end_value is not None
    end = end_value if multiline else start
    if not isinstance(end, int) or isinstance(end, bool) or end < start:
        raise BuildError(f"finding {finding.get('id', '<unknown>')}: location.end_line must be an integer >= start_line")
    return path, start, end, multiline, side if isinstance(side, str) else ""


def location_label(finding: dict[str, Any]) -> str:
    path, start, end, multiline, _ = finding_location(finding)
    lines = f"L{start}-L{end}" if multiline else f"L{start}"
    return f"{path}:{lines}"


def single_line(value: Any) -> str:
    text = value if isinstance(value, str) else ""
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", " ").strip()


def cluster_context(
    finding: dict[str, Any],
    members_by_representative: dict[str, list[dict[str, Any]]],
    active_severities: set[str],
) -> str:
    identifier = finding.get("id")
    members = members_by_representative.get(identifier, []) if isinstance(identifier, str) else []
    if not members:
        return ""
    postable_members: list[dict[str, Any]] = []
    for member in members:
        if member.get("severity") not in active_severities:
            continue
        posting = member.get("posting")
        posting = posting if isinstance(posting, dict) else {}
        if posting.get("post_policy") in {"local_only", "suppress"}:
            continue
        if posting.get("explanation_postable") is not True:
            continue
        if security_disclosure_policy(member) == "local_only":
            continue
        postable_members.append(member)

    displayed_members = postable_members[:5]
    lines = ["", "同一 root cause の影響箇所:"]
    for member in displayed_members:
        path, _start, end, _multiline, _side = finding_location(member)
        problem: Any = member.get("problem")
        if security_requires_body(member):
            security = member.get("security")
            problem = security.get("public_safe_summary") if isinstance(security, dict) else ""
        lines.append(f"- `{path}:L{end}` {single_line(problem)}")
    remaining = len(members) - len(displayed_members)
    if remaining > 0:
        lines.append(f"- 他 {remaining} 件")
    return "\n".join(lines)


def inline_body(
    finding: dict[str, Any],
    members_by_representative: dict[str, list[dict[str, Any]]],
    active_severities: set[str],
) -> str:
    severity = finding.get("severity")
    label = location_label(finding)
    if severity == "must_fix":
        return (
            "🚨 **Must Fix**\n\n"
            f"- 問題: {finding.get('problem', '')}\n"
            f"- 理由: {finding.get('reason', '')}\n"
            f"- 提案: {finding.get('suggestion', '')}"
            f"{cluster_context(finding, members_by_representative, active_severities)}"
        )
    if severity == "should_fix":
        return (
            f"🛠 **Should Fix** `{label}`\n"
            f"- 改善: {single_line(finding.get('problem'))}\n"
            f"- 提案: {single_line(finding.get('suggestion'))}"
        )
    return (
        f"💡 **Nit** `{label}`\n"
        f"- 内容: {single_line(finding.get('problem'))}\n"
        f"- 提案: {single_line(finding.get('suggestion'))}"
    )


def inline_comment(
    finding: dict[str, Any],
    members_by_representative: dict[str, list[dict[str, Any]]],
    active_severities: set[str],
) -> dict[str, Any]:
    path, start, end, multiline, _ = finding_location(finding)
    comment: dict[str, Any] = {
        "path": path,
        "line": end,
        "side": "RIGHT",
        "body": inline_body(finding, members_by_representative, active_severities),
    }
    if multiline:
        comment["start_line"] = start
        comment["start_side"] = "RIGHT"
    return comment


def cluster_maps(findings_data: dict[str, Any], findings: list[dict[str, Any]]) -> tuple[set[str], dict[str, list[dict[str, Any]]]]:
    by_id = {item.get("id"): item for item in findings if isinstance(item.get("id"), str)}
    canonical_order = {item.get("id"): index for index, item in enumerate(findings)}
    non_representatives: set[str] = set()
    members_by_representative: dict[str, list[dict[str, Any]]] = {}
    clusters = findings_data.get("root_cause_clusters")
    if not isinstance(clusters, list):
        return non_representatives, members_by_representative
    for cluster in clusters:
        if not isinstance(cluster, dict):
            continue
        representative = cluster.get("representative_finding_id")
        member_ids = cluster.get("finding_ids")
        if not isinstance(representative, str) or not isinstance(member_ids, list):
            continue
        valid_member_ids = [identifier for identifier in member_ids if isinstance(identifier, str) and identifier in by_id]
        non_representatives.update(identifier for identifier in valid_member_ids if identifier != representative)
        members = [by_id[identifier] for identifier in valid_member_ids if identifier != representative]
        members.sort(key=lambda item: canonical_order.get(item.get("id"), len(findings)))
        members_by_representative[representative] = members
    return non_representatives, members_by_representative


def candidate_kind(severity: str) -> str:
    return {"must_fix": "Must Fix", "should_fix": "Should Fix", "nit": "Nit"}[severity]


def out_of_range_markdown(entry: dict[str, Any]) -> str:
    finding = entry["finding"]
    heading = f"### `{location_label(finding)}`"
    if entry["reason"] == "security disclosure policy":
        security = finding.get("security")
        safe_summary = security.get("public_safe_summary", "") if isinstance(security, dict) else ""
        return f"{heading}\n\n- 問題: {safe_summary}"
    return (
        f"{heading}\n\n"
        f"- 問題: {finding.get('problem', '')}\n"
        f"- 理由: {finding.get('reason', '')}\n"
        f"- 提案: {finding.get('suggestion', '')}"
    )


def review_scope(run_plan: Any | None, must_fix_count: int) -> str:
    if not isinstance(run_plan, dict):
        return DEFAULT_REVIEW_SCOPE
    depth = run_plan.get("depth_actual")
    mode = run_plan.get("recommended_mode")
    risk_tags = run_plan.get("risk_tags")
    if not isinstance(depth, str) or not isinstance(mode, str) or not isinstance(risk_tags, list) or not all(isinstance(tag, str) for tag in risk_tags):
        return DEFAULT_REVIEW_SCOPE
    risk_text = ", ".join(risk_tags) if risk_tags else "なし"
    return (
        f"{DEFAULT_REVIEW_SCOPE}; depth_actual={depth}; recommended_mode={mode}; "
        f"risk_tags={risk_text}; review.md Must Fix 件数={must_fix_count}"
    )


def first_summary_line(text: str | None) -> str:
    if text is None:
        return ""
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def severity_mention(label: str, inline_count: int, body_count: int) -> str:
    """Return one posted-summary line for a severity, or '' when nothing was posted."""

    if inline_count and body_count:
        return f"{label} を inline コメントとして併記しています（一部は行コメント不可のため本文末尾に記載）。"
    if inline_count:
        return f"{label} を inline コメントとして併記しています。"
    if body_count:
        return f"{label} は行コメント不可のため本文末尾に記載しています。"
    return ""


def compose_posted_summary(
    event: str,
    ci_state: str | None,
    *,
    must_fix_inline: int,
    must_fix_body: int,
    should_fix_inline: int,
    should_fix_body: int,
    nit_inline: int,
    nit_body: int,
) -> str:
    """Compose the posted body summary from posted findings only (issue #120).

    The free-form `## 総評` of review.md is never posted: severities absent from
    the payload must not be mentioned, withheld findings leak nothing, and no
    counts are shown — cluster representatives aggregate members, so posted
    comment counts diverge from canonical finding counts.
    """

    visible_must_fix = must_fix_inline + must_fix_body
    lines: list[str] = []
    if event == "REQUEST_CHANGES":
        if visible_must_fix:
            sentence = "Must Fix を検出しました。マージ前に修正が必要です。"
            if must_fix_inline and must_fix_body:
                sentence += "一部は行コメント不可のため本文末尾に記載しています。"
            elif must_fix_body:
                sentence += "行コメント不可のため本文末尾に記載しています。"
            lines.append(sentence)
        else:
            lines.append("このレビューは変更をリクエストします。")
    elif event == "COMMENT":
        lines.append(f"Must Fix はありませんが、CI が {ci_state or '未取得'} のため承認を保留します。")
    else:
        lines.append("Must Fix はありません。承認します。")
    for label, inline_count, body_count in (
        ("Should Fix", should_fix_inline, should_fix_body),
        ("Nit", nit_inline, nit_body),
    ):
        mention = severity_mention(label, inline_count, body_count)
        if mention:
            lines.append(mention)
    return "\n".join(lines)


def display_effort(effort: str) -> str:
    """Return the human-facing label for an engine effort literal."""

    return EFFORT_DISPLAY_LABELS.get(effort, effort)


def compose_review_footer(findings_data: dict[str, Any], metadata_data: dict[str, Any], must_fix_total: int) -> str:
    """Compose the automated-review footer appended to every posted body (issue #124).

    validate_build_inputs guarantees producer.version and review_engines are
    present and well-formed, so the footer always discloses the pr-codex
    version and every hunter engine with its model and effort; deficient
    inputs fail the build (fail-closed) instead of degrading the disclosure.
    Each CLI's maximum effort tier is displayed as "max" while metadata keeps
    the exact execution literal (for example, Codex records "xhigh").

    The semantic-preflight verifier line appears exactly when must_fix_total
    >= 1: send Step 4.5 always runs the Codex semantic preflight for such
    payloads (posting is aborted when it fails) and always skips it when no
    must_fix exists, so a posted body always matches the executed engines.
    The line's wording never mentions Must Fix and its presence is equivalent
    to the public REQUEST_CHANGES event, so withheld findings leak nothing
    (issue #120 disclosure rules).
    """

    version = findings_data["producer"]["version"].strip()
    rendered = [
        f"{engine['name'].strip()} {engine['model'].strip()} ({display_effort(engine['effort'].strip())})"
        for engine in metadata_data["review_engines"]
    ]
    lines = [
        f"これは [pr-codex]({PR_CODEX_REPO_URL}):v{version} による自動レビューです。",
        f"レビューは {' と '.join(rendered)} により行われました。",
    ]
    if must_fix_total >= 1:
        verifier_name, verifier_model, verifier_effort = SEMANTIC_VERIFIER_ENGINE
        lines.append(
            f"投稿前検証 (semantic preflight) は {verifier_name} {verifier_model} "
            f"({display_effort(verifier_effort)}) により行われました。"
        )
    return "---\n\n" + "\n".join(lines)


def build_body(
    summary: str,
    good_points: str,
    event: str,
    metadata: dict[str, Any],
    ci_state: str | None,
    ci_summary: str | None,
    scope: str,
    out_of_range: list[dict[str, Any]],
    footer: str,
) -> str:
    sections = [summary]
    if good_points:
        sections.append(f"## 良い点\n\n{good_points}")
    if event == "APPROVE":
        raw_files = metadata.get("files")
        files = [item for item in raw_files if isinstance(item, str)] if isinstance(raw_files, list) else []
        sections.append(
            "## 確認した範囲\n\n"
            f"- 変更ファイル: {', '.join(files)}\n"
            f"- 検証観点: {scope}\n"
            f"- CI 状態: {ci_state or '未取得'}"
        )
    elif event == "COMMENT":
        ci_lines = ["## CI 状態", "", f"- 状態: {ci_state or '未取得'}"]
        short_summary = first_summary_line(ci_summary)
        if short_summary:
            ci_lines.append(f"- 要約: {short_summary}")
        sections.append("\n".join(ci_lines))
    if out_of_range:
        entries = "\n\n".join(out_of_range_markdown(entry) for entry in out_of_range)
        sections.append(f"## 行コメント不可 (diff 範囲外)\n\n{entries}")
    sections.append(footer)
    return "\n\n".join(sections)


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def write_bytes(path: Path, data: bytes, label: str) -> None:
    try:
        path.write_bytes(data)
    except Exception as exc:  # noqa: BLE001 - report output failures as invalid build operations
        raise BuildError(f"{label}: cannot write {path}: {exc}") from exc


def compose_payload(
    findings_data: Any,
    metadata_data: Any,
    review_text: str,
    ranges: dict[str, list[tuple[int, int]]],
    ci_data: Any,
    run_plan: Any,
    ci_summary: str | None,
    diff_available: bool,
    include_should_fix: bool,
    include_nit: bool,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, int], int]:
    """Compose payload and semantic manifest fields without reading or writing files."""

    canonical_findings, must_fix_total = validate_build_inputs(findings_data, metadata_data, review_text)
    if not markdown_section(review_text, "## 総評"):
        raise BuildError("review.md summary is empty or missing")
    good_points = markdown_section(review_text, "## 良い点")
    ci_state_value = ci_data.get("state") if isinstance(ci_data, dict) else None
    ci_state = ci_state_value if ci_state_value in {"success", "failure", "pending", "skipped"} else None

    raw_metadata_files = metadata_data.get("files")
    metadata_files = (
        {item for item in raw_metadata_files if isinstance(item, str)}
        if isinstance(raw_metadata_files, list)
        else set()
    )
    active_severities = {"must_fix"}
    if include_should_fix:
        active_severities.add("should_fix")
    if include_nit:
        active_severities.add("nit")

    non_representatives, members_by_representative = cluster_maps(findings_data, canonical_findings)
    inline_by_severity: dict[str, list[dict[str, Any]]] = {"must_fix": [], "should_fix": [], "nit": []}
    out_of_range: list[dict[str, Any]] = []
    withheld: list[dict[str, Any]] = []
    for item in canonical_findings:
        identifier = item.get("id")
        if isinstance(identifier, str) and identifier in non_representatives:
            continue
        severity = item.get("severity")
        posting = item.get("posting")
        posting = posting if isinstance(posting, dict) else {}
        selected = severity == "must_fix"
        if severity == "should_fix":
            selected = (
                include_should_fix
                and posting.get("post_policy") == "body_summary"
                and posting.get("explanation_postable") is True
            )
        elif severity == "nit":
            selected = (
                include_nit
                and posting.get("post_policy") == "body_summary"
                and posting.get("explanation_postable") is True
            )
        elif severity not in {"must_fix", "should_fix", "nit"}:
            selected = False
        if not selected:
            continue
        private_reason = withheld_reason(item)
        if private_reason is not None:
            withheld.append({"finding": item, "kind": candidate_kind(severity), "reason": private_reason})
            continue
        if security_requires_body(item):
            out_of_range.append(
                {"finding": item, "kind": candidate_kind(severity), "reason": "security disclosure policy"}
            )
            continue
        path, start, end, _multiline, side = finding_location(item)
        if side != "RIGHT":
            out_of_range.append({"finding": item, "kind": candidate_kind(severity), "reason": "LEFT-side 非対応"})
            continue
        if (
            path not in metadata_files
            or not diff_available
            or not ranges
            or not range_contains(ranges, path, start, end)
        ):
            out_of_range.append({"finding": item, "kind": candidate_kind(severity), "reason": "diff 範囲外"})
            continue
        inline_by_severity[severity].append(item)

    ordered_inline = inline_by_severity["must_fix"] + inline_by_severity["should_fix"] + inline_by_severity["nit"]
    comments = [inline_comment(item, members_by_representative, active_severities) for item in ordered_inline]
    must_fix_body = sum(entry["kind"] == "Must Fix" for entry in out_of_range)
    must_fix_withheld = sum(entry["kind"] == "Must Fix" for entry in withheld)
    should_fix_body = sum(entry["kind"] == "Should Fix" for entry in out_of_range)
    nit_body = sum(entry["kind"] == "Nit" for entry in out_of_range)
    if must_fix_total:
        event = "REQUEST_CHANGES"
    elif ci_state in {"failure", "pending"}:
        event = "COMMENT"
    else:
        event = "APPROVE"

    body = build_body(
        compose_posted_summary(
            event,
            ci_state,
            must_fix_inline=len(inline_by_severity["must_fix"]),
            must_fix_body=must_fix_body,
            should_fix_inline=len(inline_by_severity["should_fix"]),
            should_fix_body=should_fix_body,
            nit_inline=len(inline_by_severity["nit"]),
            nit_body=nit_body,
        ),
        good_points,
        event,
        metadata_data,
        ci_state,
        ci_summary,
        review_scope(run_plan, must_fix_total),
        out_of_range,
        compose_review_footer(findings_data, metadata_data, must_fix_total),
    )
    payload = {"commit_id": metadata_data.get("head_sha"), "event": event, "body": body, "comments": comments}
    comment_map = [
        {"comment_index": index, "finding_id": item.get("id"), "severity": item.get("severity")}
        for index, item in enumerate(ordered_inline)
    ]
    manifest_out_of_range = [
        {"finding_id": entry["finding"].get("id"), "kind": entry["kind"], "reason": entry["reason"]}
        for entry in out_of_range
    ]
    manifest_withheld = [
        {"finding_id": entry["finding"].get("id"), "kind": entry["kind"], "reason": entry["reason"]}
        for entry in withheld
    ]
    semantic_targets = [
        item.get("id")
        for item in canonical_findings
        if item.get("severity") == "must_fix"
    ]
    counts = {
        "must_fix_total": must_fix_total,
        "must_fix_inline": len(inline_by_severity["must_fix"]),
        "must_fix_body": must_fix_body,
        "must_fix_withheld": must_fix_withheld,
        "should_fix_inline": len(inline_by_severity["should_fix"]),
        "nit_inline": len(inline_by_severity["nit"]),
    }
    manifest_core = {
        "schema_version": "payload-manifest.v1",
        "event": event,
        "comment_map": comment_map,
        "out_of_range": manifest_out_of_range,
        "withheld": manifest_withheld,
        "semantic_targets": semantic_targets,
        "counts": counts,
        "flags": {
            "include_should_fix": include_should_fix,
            "include_nit": include_nit,
        },
    }
    return payload, manifest_core, counts, len(out_of_range)


def build(args: argparse.Namespace) -> tuple[str, int, dict[str, int], int]:
    snapshots: dict[str, str] = {}
    findings_bytes = read_required(args.findings, "findings", snapshots)
    review_bytes = read_required(args.review, "review", snapshots)
    metadata_bytes = read_required(args.metadata, "metadata", snapshots)
    ranges_bytes = read_required(args.ranges, "ranges", snapshots)

    findings_data = parse_required_json(findings_bytes, args.findings, "findings")
    metadata_data = parse_required_json(metadata_bytes, args.metadata, "metadata")
    review_text = decode_required(review_bytes, args.review, "review")
    ranges_text = decode_required(ranges_bytes, args.ranges, "ranges")
    ranges = parse_ranges(ranges_text, args.ranges)

    ci_bytes = read_optional(args.ci_status, snapshots)
    run_plan_bytes = read_optional(args.run_plan, snapshots)
    ci_summary_bytes = read_optional(args.ci_summary, snapshots)
    hash_optional(args.sarif, snapshots)
    diff_available = hash_optional(args.diff, snapshots)
    payload, manifest_core, counts, out_of_range_count = compose_payload(
        findings_data,
        metadata_data,
        review_text,
        ranges,
        parse_optional_json(ci_bytes),
        parse_optional_json(run_plan_bytes),
        decode_optional(ci_summary_bytes),
        diff_available,
        args.include_should_fix,
        args.include_nit,
    )

    payload_bytes = json_bytes(payload)
    write_bytes(args.output, payload_bytes, "output")
    snapshots[resolved(args.output)] = sha256_bytes(payload_bytes)

    required_paths = {
        "findings": args.findings,
        "review": args.review,
        "metadata": args.metadata,
        "ranges": args.ranges,
        "payload": args.output,
    }
    manifest_files = {
        role: {"path": resolved(artifact_path), "sha256": snapshots[resolved(artifact_path)]}
        for role, artifact_path in required_paths.items()
    }
    optional_paths = {
        "sarif": args.sarif,
        "diff": args.diff,
        "ci_status": args.ci_status,
        "run_plan": args.run_plan,
        "ci_summary": args.ci_summary,
    }
    for role, artifact_path in optional_paths.items():
        if artifact_path is None:
            continue
        artifact_key = resolved(artifact_path)
        if artifact_key in snapshots:
            manifest_files[role] = {"path": artifact_key, "sha256": snapshots[artifact_key]}

    manifest = {
        "schema_version": manifest_core["schema_version"],
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "event": manifest_core["event"],
        "comment_map": manifest_core["comment_map"],
        "out_of_range": manifest_core["out_of_range"],
        "withheld": manifest_core["withheld"],
        "semantic_targets": manifest_core["semantic_targets"],
        "counts": manifest_core["counts"],
        "flags": manifest_core["flags"],
        "files": manifest_files,
    }
    write_bytes(args.manifest, json_bytes(manifest), "manifest")
    return payload["event"], len(payload["comments"]), counts, out_of_range_count


def verify_manifest(path: Path) -> list[str]:
    manifest_path = resolved(path)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - malformed/missing manifests are invalid artifacts
        return [f"{manifest_path}: cannot read/parse JSON: {exc}"]
    if not isinstance(manifest, dict):
        return [f"{manifest_path}: top-level value must be an object"]

    errors: list[str] = []
    required_types: dict[str, type[Any]] = {
        "schema_version": str,
        "generated_at": str,
        "event": str,
        "comment_map": list,
        "out_of_range": list,
        "withheld": list,
        "semantic_targets": list,
        "counts": dict,
        "flags": dict,
        "files": dict,
    }
    for key, expected_type in required_types.items():
        if key not in manifest:
            errors.append(f"{manifest_path}: {key}: required key is missing")
        elif not isinstance(manifest[key], expected_type):
            errors.append(f"{manifest_path}: {key}: must be a {expected_type.__name__}")

    if manifest.get("schema_version") != "payload-manifest.v1":
        errors.append(f"{manifest_path}: schema_version must equal payload-manifest.v1")

    generated_at = manifest.get("generated_at")
    if isinstance(generated_at, str):
        valid_generated_at = GENERATED_AT_RE.fullmatch(generated_at) is not None
        if valid_generated_at:
            try:
                datetime.strptime(generated_at, "%Y-%m-%dT%H:%M:%S+00:00")
            except ValueError:
                valid_generated_at = False
        if not valid_generated_at:
            errors.append(f"{manifest_path}: generated_at must be UTC YYYY-MM-DDTHH:MM:SS+00:00")

    event = manifest.get("event")
    if isinstance(event, str) and event not in MANIFEST_EVENTS:
        errors.append(f"{manifest_path}: event must be REQUEST_CHANGES, COMMENT, or APPROVE")

    comment_map = manifest.get("comment_map")
    if isinstance(comment_map, list):
        for index, entry in enumerate(comment_map):
            entry_label = f"{manifest_path}: comment_map[{index}]"
            if not isinstance(entry, dict):
                errors.append(f"{entry_label}: must be an object")
                continue
            comment_index = entry.get("comment_index")
            if not isinstance(comment_index, int) or isinstance(comment_index, bool) or comment_index < 0:
                errors.append(f"{entry_label}.comment_index: must be an integer >= 0")
            elif comment_index != index:
                errors.append(f"{entry_label}.comment_index: must equal {index}")
            if not isinstance(entry.get("finding_id"), str):
                errors.append(f"{entry_label}.finding_id: must be a string")
            if entry.get("severity") not in {"must_fix", "should_fix", "nit"}:
                errors.append(f"{entry_label}.severity: must be must_fix, should_fix, or nit")

    out_of_range = manifest.get("out_of_range")
    if isinstance(out_of_range, list):
        for index, entry in enumerate(out_of_range):
            entry_label = f"{manifest_path}: out_of_range[{index}]"
            if not isinstance(entry, dict):
                errors.append(f"{entry_label}: must be an object")
                continue
            if not isinstance(entry.get("finding_id"), str):
                errors.append(f"{entry_label}.finding_id: must be a string")
            if entry.get("kind") not in {"Must Fix", "Should Fix", "Nit"}:
                errors.append(f"{entry_label}.kind: must be Must Fix, Should Fix, or Nit")
            if entry.get("reason") not in {"diff 範囲外", "LEFT-side 非対応", "security disclosure policy"}:
                errors.append(f"{entry_label}.reason: invalid reason")

    withheld = manifest.get("withheld")
    if isinstance(withheld, list):
        for index, entry in enumerate(withheld):
            entry_label = f"{manifest_path}: withheld[{index}]"
            if not isinstance(entry, dict):
                errors.append(f"{entry_label}: must be an object")
                continue
            if not isinstance(entry.get("finding_id"), str):
                errors.append(f"{entry_label}.finding_id: must be a string")
            if entry.get("kind") != "Must Fix":
                errors.append(f"{entry_label}.kind: must equal Must Fix")
            if entry.get("reason") not in {"local_only", "suppress"}:
                errors.append(f"{entry_label}.reason: must be local_only or suppress")

    semantic_targets = manifest.get("semantic_targets")
    if isinstance(semantic_targets, list):
        for index, identifier in enumerate(semantic_targets):
            if not isinstance(identifier, str):
                errors.append(f"{manifest_path}: semantic_targets[{index}]: must be a string")

    counts = manifest.get("counts")
    valid_counts: dict[str, int] = {}
    if isinstance(counts, dict):
        for key in MANIFEST_COUNT_KEYS:
            value = counts.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                errors.append(f"{manifest_path}: counts.{key}: must be an integer >= 0")
            else:
                valid_counts[key] = value
    flags = manifest.get("flags")
    valid_flags: dict[str, bool] = {}
    if isinstance(flags, dict):
        for key in ("include_should_fix", "include_nit"):
            value = flags.get(key)
            if not isinstance(value, bool):
                errors.append(f"{manifest_path}: flags.{key}: must be a boolean")
            else:
                valid_flags[key] = value


    files = manifest.get("files")
    verified_file_bytes: dict[str, bytes] = {}
    verified_roles: set[str] = set()
    verified_nonempty: dict[str, bool] = {}
    verified_paths: dict[str, Path] = {}
    if isinstance(files, dict):
        for role in MANIFEST_REQUIRED_ROLES:
            if role not in files:
                errors.append(f"{manifest_path}: files.{role}: required role is missing")

        allowed_roles = set(MANIFEST_REQUIRED_ROLES) | set(MANIFEST_OPTIONAL_ROLES)
        for role, record in files.items():
            role_label = f"{manifest_path}: files.{role}"
            if role not in allowed_roles:
                errors.append(f"{role_label}: unknown role")
                continue
            if not isinstance(record, dict):
                errors.append(f"{role_label}: must be an object")
                continue
            raw_path = record.get("path")
            expected_digest = record.get("sha256")
            if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
                errors.append(f"{role_label}.path: must be an absolute path")
                continue
            if not isinstance(expected_digest, str) or SHA256_RE.fullmatch(expected_digest) is None:
                errors.append(f"{role_label}.sha256: invalid sha256 digest")
                continue
            target = Path(raw_path)
            try:
                if role in REPLAY_BYTE_ROLES:
                    target_bytes = target.read_bytes()
                    actual_digest = sha256_bytes(target_bytes)
                    has_content = bool(target_bytes)
                else:
                    target_bytes = None
                    actual_digest = sha256_file(target)
                    has_content = target.stat().st_size > 0
            except FileNotFoundError:
                errors.append(f"{raw_path}: missing")
            except Exception as exc:  # noqa: BLE001 - verification lists every unreadable artifact
                errors.append(f"{raw_path}: cannot read: {exc}")
            else:
                if actual_digest != expected_digest:
                    errors.append(f"{raw_path}: sha256 mismatch")
                    continue
                verified_roles.add(role)
                verified_nonempty[role] = has_content
                verified_paths[role] = target
                if target_bytes is not None:
                    verified_file_bytes[role] = target_bytes

    payload_data: Any = None
    payload_bytes = verified_file_bytes.get("payload")
    if payload_bytes is not None:
        try:
            payload_data = json.loads(payload_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            errors.append(f"{manifest_path}: files.payload: cannot parse JSON: {exc}")
        if payload_data is not None and not isinstance(payload_data, dict):
            errors.append(f"{manifest_path}: files.payload: top-level value must be an object")
            payload_data = None
    if isinstance(payload_data, dict):
        payload_comments = payload_data.get("comments")
        if not isinstance(payload_comments, list):
            errors.append(f"{manifest_path}: payload.comments: must be an array")
        elif isinstance(comment_map, list) and len(payload_comments) != len(comment_map):
            errors.append(
                f"{manifest_path}: payload.comments length ({len(payload_comments)}) "
                f"does not match comment_map ({len(comment_map)})"
            )
        payload_event = payload_data.get("event")
        if not isinstance(payload_event, str):
            errors.append(f"{manifest_path}: payload.event: must be a string")
        elif isinstance(event, str) and payload_event != event:
            errors.append(
                f"{manifest_path}: payload.event ({payload_event}) does not match manifest.event ({event})"
            )

    findings_data: Any = None
    findings_bytes = verified_file_bytes.get("findings")
    if findings_bytes is not None:
        try:
            findings_data = json.loads(findings_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            errors.append(f"{manifest_path}: files.findings: cannot parse JSON: {exc}")
        if findings_data is not None and not isinstance(findings_data, dict):
            errors.append(f"{manifest_path}: files.findings: top-level value must be an object")
            findings_data = None

    expected_targets: list[str] = []
    expected_targets_valid = True
    if isinstance(findings_data, dict):
        raw_findings = findings_data.get("findings")
        if not isinstance(raw_findings, list):
            errors.append(f"{manifest_path}: findings.findings: must be an array")
            expected_targets_valid = False
        else:
            for index, finding in enumerate(raw_findings):
                if not isinstance(finding, dict):
                    errors.append(f"{manifest_path}: findings.findings[{index}]: must be an object")
                    expected_targets_valid = False
                    continue
                if finding.get("severity") != "must_fix":
                    continue
                identifier = finding.get("id")
                if not isinstance(identifier, str):
                    errors.append(f"{manifest_path}: findings.findings[{index}].id: must be a string")
                    expected_targets_valid = False
                    continue
                expected_targets.append(identifier)

    semantic_targets_valid = isinstance(semantic_targets, list) and all(
        isinstance(identifier, str) for identifier in semantic_targets
    )
    if expected_targets_valid and isinstance(findings_data, dict) and semantic_targets_valid:
        if set(expected_targets) != set(semantic_targets) or len(expected_targets) != len(semantic_targets):
            errors.append(f"{manifest_path}: semantic_targets do not match findings must_fix ids")

    if isinstance(semantic_targets, list) and "must_fix_total" in valid_counts:
        if valid_counts["must_fix_total"] != len(semantic_targets):
            errors.append(f"{manifest_path}: counts.must_fix_total must equal len(semantic_targets)")

    if isinstance(comment_map, list):
        expected_inline = sum(
            isinstance(entry, dict) and entry.get("severity") == "must_fix"
            for entry in comment_map
        )
        expected_should_fix = sum(
            isinstance(entry, dict) and entry.get("severity") == "should_fix"
            for entry in comment_map
        )
        expected_nit = sum(
            isinstance(entry, dict) and entry.get("severity") == "nit"
            for entry in comment_map
        )
        for key, expected in (
            ("must_fix_inline", expected_inline),
            ("should_fix_inline", expected_should_fix),
            ("nit_inline", expected_nit),
        ):
            if key in valid_counts and valid_counts[key] != expected:
                errors.append(f"{manifest_path}: counts.{key} does not match comment_map")

    if isinstance(out_of_range, list) and "must_fix_body" in valid_counts:
        expected_body = sum(
            isinstance(entry, dict) and entry.get("kind") == "Must Fix"
            for entry in out_of_range
        )
        if valid_counts["must_fix_body"] != expected_body:
            errors.append(f"{manifest_path}: counts.must_fix_body does not match out_of_range")

    if isinstance(withheld, list) and "must_fix_withheld" in valid_counts:
        expected_withheld = sum(
            isinstance(entry, dict) and entry.get("kind") == "Must Fix"
            for entry in withheld
        )
        if valid_counts["must_fix_withheld"] != expected_withheld:
            errors.append(f"{manifest_path}: counts.must_fix_withheld does not match withheld")

    if valid_counts.get("must_fix_total", 0) >= 1 and event != "REQUEST_CHANGES":
        errors.append(f"{manifest_path}: event must be REQUEST_CHANGES when must_fix_total is at least 1")
    replay_ready = (
        isinstance(files, dict)
        and set(files) == verified_roles
        and set(MANIFEST_REQUIRED_ROLES).issubset(verified_roles)
        and len(valid_flags) == 2
        and isinstance(payload_data, dict)
    )
    if replay_ready:
        try:
            replay_findings = parse_required_json(
                verified_file_bytes["findings"],
                verified_paths["findings"],
                "findings",
            )
            replay_metadata = parse_required_json(
                verified_file_bytes["metadata"],
                verified_paths["metadata"],
                "metadata",
            )
            replay_review = decode_required(
                verified_file_bytes["review"],
                verified_paths["review"],
                "review",
            )
            replay_ranges_text = decode_required(
                verified_file_bytes["ranges"],
                verified_paths["ranges"],
                "ranges",
            )
            replay_ranges = parse_ranges(replay_ranges_text, verified_paths["ranges"])
            expected_payload, expected_manifest_core, _expected_counts, _expected_out_of_range = compose_payload(
                replay_findings,
                replay_metadata,
                replay_review,
                replay_ranges,
                parse_optional_json(verified_file_bytes.get("ci_status")),
                parse_optional_json(verified_file_bytes.get("run_plan")),
                decode_optional(verified_file_bytes.get("ci_summary")),
                verified_nonempty.get("diff", False),
                valid_flags["include_should_fix"],
                valid_flags["include_nit"],
            )
        except BuildError as exc:
            for error in exc.errors:
                errors.append(f"{manifest_path}: regeneration failed: {error}")
        else:
            if expected_payload != payload_data:
                missing = object()
                payload_keys = (set(expected_payload) | set(payload_data)) - {"comments"}
                for key in sorted(payload_keys):
                    expected_value = expected_payload.get(key, missing)
                    actual_value = payload_data.get(key, missing)
                    if expected_value != actual_value:
                        errors.append(f"{manifest_path}: payload.{key} does not match regenerated payload")

                expected_comments = expected_payload.get("comments")
                actual_comments = payload_data.get("comments")
                if isinstance(expected_comments, list) and isinstance(actual_comments, list):
                    if len(expected_comments) != len(actual_comments):
                        errors.append(
                            f"{manifest_path}: payload.comments length ({len(actual_comments)}) "
                            f"does not match regenerated payload ({len(expected_comments)})"
                        )
                    for index, (expected_comment, actual_comment) in enumerate(
                        zip(expected_comments, actual_comments)
                    ):
                        if expected_comment == actual_comment:
                            continue
                        if not isinstance(expected_comment, dict) or not isinstance(actual_comment, dict):
                            errors.append(
                                f"{manifest_path}: payload.comments[{index}] "
                                "does not match regenerated payload"
                            )
                            continue
                        comment_fields = set(expected_comment) | set(actual_comment)
                        for field in sorted(comment_fields):
                            expected_value = expected_comment.get(field, missing)
                            actual_value = actual_comment.get(field, missing)
                            if expected_value != actual_value:
                                errors.append(
                                    f"{manifest_path}: payload.comments[{index}].{field} "
                                    "does not match regenerated payload"
                                )
                else:
                    errors.append(f"{manifest_path}: payload.comments does not match regenerated payload")

            # File records were already checked role-by-role against their digests above.
            for key, expected_value in expected_manifest_core.items():
                actual_value = manifest.get(key)
                if actual_value == expected_value:
                    continue
                if key == "comment_map" and isinstance(expected_value, list) and isinstance(actual_value, list):
                    if len(expected_value) != len(actual_value):
                        errors.append(
                            f"{manifest_path}: comment_map length ({len(actual_value)}) "
                            f"does not match regenerated manifest ({len(expected_value)})"
                        )
                    for index, (expected_entry, actual_entry) in enumerate(zip(expected_value, actual_value)):
                        if expected_entry != actual_entry:
                            errors.append(
                                f"{manifest_path}: comment_map[{index}] "
                                "does not match regenerated manifest"
                            )
                    continue
                errors.append(f"{manifest_path}: {key} does not match regenerated manifest")

    return errors


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description="Build a GitHub Reviews API payload or verify its payload manifest")
    cli.add_argument("--verify", action="store_true", help="verify files recorded in --manifest")
    cli.add_argument("--findings", type=Path)
    cli.add_argument("--review", type=Path)
    cli.add_argument("--metadata", type=Path)
    cli.add_argument("--ranges", type=Path)
    cli.add_argument("--ci-status", type=Path)
    cli.add_argument("--run-plan", type=Path)
    cli.add_argument("--ci-summary", type=Path)
    cli.add_argument("--sarif", type=Path)
    cli.add_argument("--diff", type=Path)
    cli.add_argument("--include-should-fix", action="store_true")
    cli.add_argument("--include-nit", action="store_true")
    cli.add_argument("--output", type=Path)
    cli.add_argument("--manifest", type=Path)
    return cli


def main() -> int:
    cli = parser()
    args = cli.parse_args()
    if args.verify:
        build_only_values = (
            args.findings,
            args.review,
            args.metadata,
            args.ranges,
            args.ci_status,
            args.run_plan,
            args.ci_summary,
            args.sarif,
            args.diff,
            args.output,
            args.include_should_fix,
            args.include_nit,
        )
        if args.manifest is None:
            cli.error("--verify requires --manifest")
        if any(build_only_values):
            cli.error("--verify may only be combined with --manifest")
        errors = verify_manifest(args.manifest)
        if errors:
            print("INVALID payload manifest", file=sys.stderr)
            for error in errors:
                print(f"- {error}", file=sys.stderr)
            return 1
        print("payload manifest verified")
        return 0

    required = ("findings", "review", "metadata", "ranges", "output", "manifest")
    missing = [f"--{name.replace('_', '-')}" for name in required if getattr(args, name) is None]
    if missing:
        cli.error(f"build mode requires {', '.join(missing)}")
    try:
        event, comment_count, counts, out_of_range_count = build(args)
    except BuildError as exc:
        print("INVALID review payload inputs", file=sys.stderr)
        for error in exc.errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(
        f"built payload: event={event} comments={comment_count} "
        f"(must_fix={counts['must_fix_inline']} should_fix={counts['should_fix_inline']} nit={counts['nit_inline']}) "
        f"out_of_range={out_of_range_count}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
