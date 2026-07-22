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
DEFAULT_REVIEW_SCOPE = "2者レビュー (Claude/Codex hunter) + verifier 4軸 gate"


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


def security_requires_body(finding: dict[str, Any]) -> bool:
    if finding.get("category") != "security":
        return False
    security = finding.get("security")
    if not isinstance(security, dict):
        return False
    return security.get("severity") in {"critical", "high"} or security.get("disclosure_policy") != "inline_safe"


def validate_build_inputs(findings_data: Any, metadata: Any, markdown: str) -> tuple[list[dict[str, Any]], int]:
    errors: list[str] = []
    if not isinstance(findings_data, dict):
        raise BuildError("findings: top-level value must be an object")
    if findings_data.get("schema_version") != "findings.v1":
        errors.append("findings.schema_version: must equal findings.v1")
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
        if severity != "must_fix" or security_requires_body(item):
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


def cluster_context(finding: dict[str, Any], members_by_representative: dict[str, list[dict[str, Any]]]) -> str:
    identifier = finding.get("id")
    members = members_by_representative.get(identifier, []) if isinstance(identifier, str) else []
    if not members:
        return ""
    lines = ["", "同一 root cause の影響箇所:"]
    for member in members[:5]:
        path, _start, end, _multiline, _side = finding_location(member)
        problem: Any = member.get("problem")
        if security_requires_body(member):
            security = member.get("security")
            problem = security.get("public_safe_summary") if isinstance(security, dict) else ""
        lines.append(f"- `{path}:L{end}` {single_line(problem)}")
    remaining = len(members) - 5
    if remaining > 0:
        lines.append(f"- 他 {remaining} 件")
    return "\n".join(lines)


def inline_body(finding: dict[str, Any], members_by_representative: dict[str, list[dict[str, Any]]]) -> str:
    severity = finding.get("severity")
    label = location_label(finding)
    if severity == "must_fix":
        return (
            "🚨 **Must Fix**\n\n"
            f"- 問題: {finding.get('problem', '')}\n"
            f"- 理由: {finding.get('reason', '')}\n"
            f"- 提案: {finding.get('suggestion', '')}"
            f"{cluster_context(finding, members_by_representative)}"
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


def inline_comment(finding: dict[str, Any], members_by_representative: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    path, start, end, multiline, _ = finding_location(finding)
    comment: dict[str, Any] = {
        "path": path,
        "line": end,
        "side": "RIGHT",
        "body": inline_body(finding, members_by_representative),
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


def build_body(
    summary: str,
    good_points: str,
    event: str,
    metadata: dict[str, Any],
    ci_state: str | None,
    ci_summary: str | None,
    scope: str,
    out_of_range: list[dict[str, Any]],
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
    return "\n\n".join(sections)


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def write_bytes(path: Path, data: bytes, label: str) -> None:
    try:
        path.write_bytes(data)
    except Exception as exc:  # noqa: BLE001 - report output failures as invalid build operations
        raise BuildError(f"{label}: cannot write {path}: {exc}") from exc


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
    canonical_findings, must_fix_total = validate_build_inputs(findings_data, metadata_data, review_text)
    summary = markdown_section(review_text, "## 総評")
    if not summary:
        raise BuildError("review.md summary is empty or missing")
    good_points = markdown_section(review_text, "## 良い点")
    ranges = parse_ranges(ranges_text, args.ranges)

    ci_bytes = read_optional(args.ci_status, snapshots)
    run_plan_bytes = read_optional(args.run_plan, snapshots)
    ci_summary_bytes = read_optional(args.ci_summary, snapshots)
    hash_optional(args.sarif, snapshots)
    diff_available = hash_optional(args.diff, snapshots)
    ci_data = parse_optional_json(ci_bytes)
    run_plan = parse_optional_json(run_plan_bytes)
    ci_summary = decode_optional(ci_summary_bytes)
    ci_state_value = ci_data.get("state") if isinstance(ci_data, dict) else None
    ci_state = ci_state_value if ci_state_value in {"success", "failure", "pending", "skipped"} else None

    non_representatives, members_by_representative = cluster_maps(findings_data, canonical_findings)
    inline_by_severity: dict[str, list[dict[str, Any]]] = {"must_fix": [], "should_fix": [], "nit": []}
    out_of_range: list[dict[str, Any]] = []
    for item in canonical_findings:
        identifier = item.get("id")
        if isinstance(identifier, str) and identifier in non_representatives:
            continue
        severity = item.get("severity")
        posting = item.get("posting")
        posting = posting if isinstance(posting, dict) else {}
        selected = severity == "must_fix"
        if severity == "should_fix":
            selected = args.include_should_fix and posting.get("post_policy") == "body_summary" and posting.get("explanation_postable") is True
        elif severity == "nit":
            selected = args.include_nit and posting.get("post_policy") == "body_summary" and posting.get("explanation_postable") is True
        elif severity not in {"must_fix", "should_fix", "nit"}:
            selected = False
        if not selected:
            continue
        if security_requires_body(item):
            out_of_range.append({"finding": item, "kind": candidate_kind(severity), "reason": "security disclosure policy"})
            continue
        path, start, end, _multiline, side = finding_location(item)
        if side != "RIGHT":
            out_of_range.append({"finding": item, "kind": candidate_kind(severity), "reason": "LEFT-side 非対応"})
            continue
        if not diff_available or not ranges or not range_contains(ranges, path, start, end):
            out_of_range.append({"finding": item, "kind": candidate_kind(severity), "reason": "diff 範囲外"})
            continue
        inline_by_severity[severity].append(item)

    ordered_inline = inline_by_severity["must_fix"] + inline_by_severity["should_fix"] + inline_by_severity["nit"]
    comments = [inline_comment(item, members_by_representative) for item in ordered_inline]
    must_fix_body = sum(entry["kind"] == "Must Fix" for entry in out_of_range)
    has_must_fix = bool(inline_by_severity["must_fix"] or must_fix_body)
    if has_must_fix:
        event = "REQUEST_CHANGES"
    elif ci_state in {"failure", "pending"}:
        event = "COMMENT"
    else:
        event = "APPROVE"

    body = build_body(
        summary,
        good_points,
        event,
        metadata_data,
        ci_state,
        ci_summary,
        review_scope(run_plan, must_fix_total),
        out_of_range,
    )
    payload = {"commit_id": metadata_data.get("head_sha"), "event": event, "body": body, "comments": comments}
    payload_bytes = json_bytes(payload)
    write_bytes(args.output, payload_bytes, "output")
    snapshots[resolved(args.output)] = sha256_bytes(payload_bytes)

    comment_map = [
        {"comment_index": index, "finding_id": item.get("id"), "severity": item.get("severity")}
        for index, item in enumerate(ordered_inline)
    ]
    manifest_out_of_range = [
        {"finding_id": entry["finding"].get("id"), "kind": entry["kind"], "reason": entry["reason"]}
        for entry in out_of_range
    ]
    counts = {
        "must_fix_total": must_fix_total,
        "must_fix_inline": len(inline_by_severity["must_fix"]),
        "must_fix_body": must_fix_body,
        "should_fix_inline": len(inline_by_severity["should_fix"]),
        "nit_inline": len(inline_by_severity["nit"]),
    }
    manifest = {
        "schema_version": "payload-manifest.v1",
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "event": event,
        "comment_map": comment_map,
        "out_of_range": manifest_out_of_range,
        "counts": counts,
        "files": snapshots,
    }
    write_bytes(args.manifest, json_bytes(manifest), "manifest")
    return event, len(comments), counts, len(out_of_range)


def verify_manifest(path: Path) -> list[str]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - malformed/missing manifests are invalid artifacts
        return [f"{resolved(path)}: cannot read/parse JSON: {exc}"]
    if not isinstance(manifest, dict):
        return [f"{resolved(path)}: top-level value must be an object"]
    errors: list[str] = []
    if manifest.get("schema_version") != "payload-manifest.v1":
        errors.append(f"{resolved(path)}: schema_version must equal payload-manifest.v1")
    files = manifest.get("files")
    if not isinstance(files, dict):
        errors.append(f"{resolved(path)}: files must be an object")
        return errors
    for raw_path, expected_digest in files.items():
        if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
            errors.append(f"{raw_path!r}: manifest file path must be absolute")
            continue
        if not isinstance(expected_digest, str) or SHA256_RE.fullmatch(expected_digest) is None:
            errors.append(f"{raw_path}: invalid sha256 digest")
            continue
        target = Path(raw_path)
        try:
            actual_digest = sha256_file(target)
        except FileNotFoundError:
            errors.append(f"{raw_path}: missing")
        except Exception as exc:  # noqa: BLE001 - verification lists every unreadable artifact
            errors.append(f"{raw_path}: cannot read: {exc}")
        else:
            if actual_digest != expected_digest:
                errors.append(f"{raw_path}: sha256 mismatch")
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
