#!/usr/bin/env python3
"""Generate SARIF v2.1.0 from pr-codex findings.verified.json.

The converter is intentionally one-way: findings.verified.json remains the
canonical source of truth and SARIF is a local derived artifact.  The output is
stable for the same canonical input; result GUIDs are deterministic UUIDv5
values derived from finding.id because SARIF's official result.guid field must
be a GUID while pr-codex fingerprints are 64-character SHA-256 hex strings.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from pathlib import Path
from typing import Any

EXPECTED_FINDINGS_SCHEMA_VERSION = "findings.v1"
CATEGORY_RULES = [
    "bug",
    "security",
    "performance",
    "tests",
    "design",
    "code_quality",
    "consistency",
    "runtime_error",
]
SEVERITY_TO_LEVEL = {
    "must_fix": "error",
    "should_fix": "warning",
    "nit": "note",
    "note": "none",
}
FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
ABSOLUTE_PATH_RE = re.compile(r"(?<![\w:])(?:/Users/[^\s:`)]+|/home/[^\s:`)]+|/private/var/[^\s:`)]+)")
TOKEN_RE = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,})\b")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


class SarifGenerationError(ValueError):
    """Raised when canonical findings cannot be safely converted to SARIF."""


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - CLI should report path/JSON errors uniformly
        raise SarifGenerationError(f"{path}: cannot read/parse JSON: {exc}") from exc


def scrub_text(value: Any) -> str:
    """Apply the same public-artifact safety posture to SARIF messages.

    pr-codex findings should already be repository-relative and scrubbed before
    send, but SARIF can later be uploaded to external systems.  Keep the local
    artifact free of host absolute paths, common access-token sentinels, and raw
    control characters so it does not weaken the GitHub-posting scrub policy.
    """
    text = value if isinstance(value, str) else ""
    text = ABSOLUTE_PATH_RE.sub("<absolute-path>", text)
    text = TOKEN_RE.sub("<redacted-token>", text)
    return CONTROL_RE.sub("", text)


def collapse_line(value: Any) -> str:
    return " ".join(scrub_text(value).split())


def is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def source_finding_id(finding: dict[str, Any], path: str) -> str:
    identifier = finding.get("id")
    if not isinstance(identifier, str) or not FINGERPRINT_RE.match(identifier):
        raise SarifGenerationError(f"{path}.id: must be a 64-character lowercase SHA-256 hex string")
    fingerprint = finding.get("fingerprint")
    if fingerprint != identifier:
        raise SarifGenerationError(f"{path}: id must equal fingerprint before SARIF generation")
    return identifier


def deterministic_guid(fingerprint: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"https://github.com/yuki777/pr-codex/findings/{fingerprint}"))


def parse_ranges(path: Path | None) -> dict[str, list[tuple[int, int]]] | None:
    if path is None:
        return None
    ranges: dict[str, list[tuple[int, int]]] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        raise SarifGenerationError(f"{path}: cannot read pr.diff.ranges.txt: {exc}") from exc
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        if not raw_line.strip():
            continue
        try:
            file_path, range_text = raw_line.split("\t", 1)
            if not range_text.startswith("L") or "-L" not in range_text:
                raise ValueError
            start_text, end_text = range_text[1:].split("-L", 1)
            start = int(start_text)
            end = int(end_text)
        except ValueError as exc:
            raise SarifGenerationError(f"{path}:{line_number}: expected '<path>\\tL<start>-L<end>'") from exc
        if start < 1 or end < start:
            raise SarifGenerationError(f"{path}:{line_number}: invalid line range")
        ranges.setdefault(file_path, []).append((start, end))
    return ranges


def range_contains(ranges: dict[str, list[tuple[int, int]]] | None, file_path: str, start_line: int, end_line: int) -> bool:
    if ranges is None:
        return True
    return any(start <= start_line <= end and start <= end_line <= end for start, end in ranges.get(file_path, []))


def finding_location(finding: dict[str, Any], path: str, ranges: dict[str, list[tuple[int, int]]] | None) -> dict[str, Any]:
    location = finding.get("location")
    if not isinstance(location, dict):
        raise SarifGenerationError(f"{path}.location: must be an object")
    file_path = location.get("path")
    if not isinstance(file_path, str) or not file_path:
        raise SarifGenerationError(f"{path}.location.path: must be a non-empty repository-relative path")
    if file_path.startswith("/") or ".." in Path(file_path).parts:
        raise SarifGenerationError(f"{path}.location.path: SARIF output requires repository-relative paths")
    if location.get("side") != "RIGHT":
        raise SarifGenerationError(f"{path}.location.side: SARIF output currently supports RIGHT side only")
    start_line = location.get("start_line")
    if not is_positive_int(start_line):
        raise SarifGenerationError(f"{path}.location.start_line: must be an integer >= 1")
    end_line = location.get("end_line", start_line)
    if not is_positive_int(end_line) or end_line < start_line:
        raise SarifGenerationError(f"{path}.location.end_line: must be >= start_line")
    if not range_contains(ranges, file_path, start_line, end_line):
        raise SarifGenerationError(f"{path}.location: {file_path}:L{start_line}-L{end_line} is outside pr.diff.ranges.txt")
    return {"path": file_path, "start_line": start_line, "end_line": end_line}


def posting_policy(finding: dict[str, Any], path: str) -> dict[str, Any]:
    posting = finding.get("posting")
    if not isinstance(posting, dict):
        raise SarifGenerationError(f"{path}.posting: must be an object")
    post_policy = posting.get("post_policy")
    if post_policy not in {"inline", "body_summary", "local_only", "suppress"}:
        raise SarifGenerationError(f"{path}.posting.post_policy: invalid post policy")
    return posting


def suppression_for(finding: dict[str, Any], path: str) -> list[dict[str, str]]:
    posting = posting_policy(finding, path)
    post_policy = posting.get("post_policy")
    severity = finding.get("severity")
    suppressions: list[dict[str, str]] = []
    if post_policy == "local_only":
        suppressions.append(
            {
                "kind": "external",
                "status": "accepted",
                "justification": "local_only per pr-codex post_policy",
            }
        )
    # Defense in depth for Issue #37 risk guardrail: Nit is never a PR-posted
    # signal, so it must be suppressed in SARIF even if older canonical data uses
    # post_policy=body_summary for send-local nits.md generation.
    if severity == "nit" and not suppressions:
        suppressions.append(
            {
                "kind": "external",
                "status": "accepted",
                "justification": "nit local-only noise suppression per pr-codex policy",
            }
        )
    if severity == "nit" and not suppressions:
        raise SarifGenerationError(f"{path}: nit findings must carry SARIF suppressions")
    return suppressions


def fixed_rules() -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = []
    for category in CATEGORY_RULES:
        rules.append(
            {
                "id": f"pr-codex/{category}",
                "name": f"pr-codex {category}",
                "shortDescription": {"text": f"pr-codex {category} finding"},
                "fullDescription": {"text": f"Canonical pr-codex review finding in category '{category}'."},
                "properties": {"category": category},
            }
        )
    return rules


def result_message(finding: dict[str, Any]) -> str:
    parts = [
        scrub_text(finding.get("title")),
        "",
        f"問題: {scrub_text(finding.get('problem'))}",
        f"理由: {scrub_text(finding.get('reason'))}",
        f"提案: {scrub_text(finding.get('suggestion'))}",
    ]
    return "\n".join(parts).strip()


def build_result(finding: dict[str, Any], index: int, ranges: dict[str, list[tuple[int, int]]] | None) -> dict[str, Any] | None:
    path = f"$.findings[{index}]"
    severity = finding.get("severity")
    if severity not in SEVERITY_TO_LEVEL:
        raise SarifGenerationError(f"{path}.severity: invalid severity")
    category = finding.get("category")
    if category not in CATEGORY_RULES:
        raise SarifGenerationError(f"{path}.category: invalid category")
    posting = posting_policy(finding, path)
    if posting.get("post_policy") == "suppress":
        return None
    if severity != "must_fix" and posting.get("post_policy") == "inline":
        raise SarifGenerationError(f"{path}.posting.post_policy: only must_fix may use inline")
    if severity == "must_fix" and (posting.get("post_policy") != "inline" or posting.get("explanation_postable") is not True):
        raise SarifGenerationError(f"{path}.posting: must_fix SARIF emission expects the same inline-postable contract as send")

    identifier = source_finding_id(finding, path)
    location = finding_location(finding, path, ranges)
    rule_id = f"pr-codex/{category}"
    properties: dict[str, Any] = {
        "source_finding_id": identifier,
        "severity": severity,
        "category": category,
        "evidence_level": finding.get("evidence_level"),
        "axes": finding.get("axes"),
        "post_policy": posting.get("post_policy"),
        "explanation_postable": posting.get("explanation_postable"),
    }
    if category == "security" and severity == "must_fix":
        properties["security_severity_label"] = "high"
    if posting.get("audience") is not None:
        properties["audience"] = posting.get("audience")
    if posting.get("not_postable_reason") is not None:
        properties["not_postable_reason"] = posting.get("not_postable_reason")

    result: dict[str, Any] = {
        "ruleId": rule_id,
        "ruleIndex": CATEGORY_RULES.index(category),
        "guid": deterministic_guid(identifier),
        "level": SEVERITY_TO_LEVEL[severity],
        "message": {"text": result_message(finding)},
        "locations": [
            {
                "physicalLocation": {
                    "artifactLocation": {"uri": location["path"]},
                    "region": {"startLine": location["start_line"], "endLine": location["end_line"]},
                }
            }
        ],
        "partialFingerprints": {"canonical": identifier},
        "properties": properties,
    }
    suppressions = suppression_for(finding, path)
    if suppressions:
        result["suppressions"] = suppressions
    if severity == "nit" and "suppressions" not in result:
        raise SarifGenerationError(f"{path}: nit findings must carry SARIF suppressions")
    return result


def repository_uri(repository: Any) -> str:
    if isinstance(repository, str) and "/" in repository and not repository.startswith("http"):
        return f"https://github.com/{repository}"
    if isinstance(repository, str) and repository.startswith("https://"):
        return repository
    return "https://github.com/yuki777/pr-codex"


def build_sarif(findings_artifact: dict[str, Any], metadata: dict[str, Any] | None = None, ranges: dict[str, list[tuple[int, int]]] | None = None) -> dict[str, Any]:
    if findings_artifact.get("schema_version") != EXPECTED_FINDINGS_SCHEMA_VERSION:
        raise SarifGenerationError("$.schema_version: generate_findings_sarif.py supports findings.v1 only")
    producer = findings_artifact.get("producer")
    if not isinstance(producer, dict):
        raise SarifGenerationError("$.producer: must be an object")
    pr = findings_artifact.get("pr")
    if not isinstance(pr, dict):
        raise SarifGenerationError("$.pr: must be an object")
    raw_findings = findings_artifact.get("findings")
    if not isinstance(raw_findings, list):
        raise SarifGenerationError("$.findings: must be an array")

    effective_ranges = ranges
    results: list[dict[str, Any]] = []
    for index, finding in enumerate(raw_findings):
        if not isinstance(finding, dict):
            raise SarifGenerationError(f"$.findings[{index}]: must be an object")
        result = build_result(finding, index, effective_ranges)
        if result is not None:
            results.append(result)

    repository = pr.get("repository")
    head_sha = pr.get("head_sha")
    branch = metadata.get("branch") if isinstance(metadata, dict) else None
    run_id = producer.get("run_id") if isinstance(producer.get("run_id"), str) else "pr-codex"
    sarif = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": producer.get("name", "pr-codex"),
                        "version": producer.get("version", "0.0.0"),
                        "informationUri": "https://github.com/yuki777/pr-codex",
                        "rules": fixed_rules(),
                    }
                },
                "invocations": [{"executionSuccessful": True, "properties": {"executionId": run_id}}],
                "versionControlProvenance": [
                    {
                        "repositoryUri": repository_uri(repository),
                        "revisionId": head_sha,
                        **({"branch": branch} if isinstance(branch, str) and branch else {}),
                    }
                ],
                "automationDetails": {"id": run_id},
                "results": results,
                "properties": {
                    "pr_codex": {
                        "schema_version": findings_artifact.get("schema_version"),
                        "repository": repository,
                        "pr_number": pr.get("number"),
                        "head_sha": pr.get("head_sha"),
                        "base_sha": pr.get("base_sha"),
                        "generated_at": findings_artifact.get("generated_at"),
                        "local_only": True,
                    }
                },
            }
        ],
    }
    return sarif


def must_fix_count(sarif: dict[str, Any]) -> int:
    return sum(
        1
        for run in sarif.get("runs", [])
        if isinstance(run, dict)
        for result in run.get("results", [])
        if isinstance(result, dict) and result.get("level") == "error"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate local SARIF v2.1.0 from pr-codex findings.verified.json")
    parser.add_argument("--findings", required=True, type=Path, help="findings.verified.json path")
    parser.add_argument("--metadata", type=Path, help="metadata.json path for branch/repository context")
    parser.add_argument("--ranges", type=Path, help="pr.diff.ranges.txt path; when provided, emitted SARIF locations must be in RIGHT-side diff hunks")
    parser.add_argument("--output", type=Path, help="write findings.sarif to this path; stdout is used when omitted")
    parser.add_argument("--emit-counts", action="store_true", help="print a compact count report instead of SARIF")
    args = parser.parse_args()

    try:
        findings = load_json(args.findings)
        metadata = load_json(args.metadata) if args.metadata else None
        ranges = parse_ranges(args.ranges)
        sarif = build_sarif(findings, metadata=metadata if isinstance(metadata, dict) else None, ranges=ranges)
    except SarifGenerationError as exc:
        print(f"INVALID findings SARIF input: {exc}", file=sys.stderr)
        return 1

    if args.emit_counts:
        print(json.dumps({"sarif_must_fix": must_fix_count(sarif), "sarif_results": len(sarif["runs"][0]["results"])}))
        return 0

    payload = json.dumps(sarif, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
