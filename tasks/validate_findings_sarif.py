#!/usr/bin/env python3
"""Validate pr-codex findings.sarif derived artifacts.

This helper keeps SARIF validation offline by loading the bundled OASIS SARIF
2.1.0 schema and enforcing the subset plus pr-codex cross-artifact invariants
used by generate_findings_sarif.py.  It mirrors the existing validator style:
exit 0 for VALID, exit 1 for contract failures, and exit 2 for CLI/read errors.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from generate_findings_sarif import CATEGORY_RULES, SEVERITY_TO_LEVEL, parse_ranges, range_contains

SARIF_SCHEMA_TITLE = "Static Analysis Results Format (SARIF) Version 2.1.0 JSON Schema"
FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$")
SECTION_HEADING_RE = re.compile(r"^##\s+", re.MULTILINE)
MUST_FIX_HEADING = "## 重大な問題 (Must Fix)"
OUT_OF_RANGE_HEADING = "## 行コメント不可 (diff 範囲外)"


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - report JSON/path failures uniformly
        raise ValueError(f"{path}: cannot read/parse JSON: {exc}") from exc


def is_non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def section(markdown: str, heading: str) -> str:
    start = markdown.find(heading)
    if start < 0:
        return ""
    body_start = markdown.find("\n", start)
    if body_start < 0:
        return ""
    next_match = SECTION_HEADING_RE.search(markdown, body_start + 1)
    end = next_match.start() if next_match else len(markdown)
    return markdown[body_start:end].strip()


def markdown_must_fix_count(path: Path) -> int:
    text = path.read_text(encoding="utf-8")
    body = section(text, MUST_FIX_HEADING)
    if not body:
        return 0
    return sum(1 for line in body.splitlines() if line.startswith("### "))


def payload_must_fix_count(path: Path) -> int:
    data = load_json(path)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: payload must be an object")
    comments = data.get("comments", [])
    if not isinstance(comments, list):
        raise ValueError(f"{path}: payload.comments must be an array")
    body = data.get("body")
    out_of_range_count = 0
    if isinstance(body, str):
        out_of_range = section(body, OUT_OF_RANGE_HEADING)
        out_of_range_count = sum(1 for line in out_of_range.splitlines() if line.startswith("### "))
    return len(comments) + out_of_range_count


def canonical_must_fix_count(findings: Any) -> int:
    if not isinstance(findings, dict) or not isinstance(findings.get("findings"), list):
        return -1
    return sum(1 for finding in findings["findings"] if isinstance(finding, dict) and finding.get("severity") == "must_fix")


def sarif_must_fix_count(sarif: Any) -> int:
    if not isinstance(sarif, dict):
        return -1
    return sum(
        1
        for run in sarif.get("runs", [])
        if isinstance(run, dict)
        for result in run.get("results", [])
        if isinstance(result, dict) and result.get("level") == "error"
    )


def validate_schema_file(schema: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(schema, dict):
        return ["$schema_file: must be an object"]
    if schema.get("title") != SARIF_SCHEMA_TITLE:
        errors.append("$schema_file.title: must be the bundled OASIS SARIF 2.1.0 schema")
    if "sarif-schema-2.1.0.json" not in str(schema.get("$id", "")):
        errors.append("$schema_file.$id: must identify sarif-schema-2.1.0.json")
    return errors


def validate_sarif_shape(data: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["$: must be an object"]
    if data.get("version") != "2.1.0":
        errors.append("$.version: must equal '2.1.0'")
    runs = data.get("runs")
    if not isinstance(runs, list) or len(runs) != 1:
        errors.append("$.runs: must contain exactly one run")
        return errors
    run = runs[0]
    if not isinstance(run, dict):
        return ["$.runs[0]: must be an object"]
    tool = run.get("tool")
    driver = tool.get("driver") if isinstance(tool, dict) else None
    if not isinstance(driver, dict):
        errors.append("$.runs[0].tool.driver: must be an object")
    else:
        if not is_non_empty_string(driver.get("name")):
            errors.append("$.runs[0].tool.driver.name: must be a non-empty string")
        if not is_non_empty_string(driver.get("version")):
            errors.append("$.runs[0].tool.driver.version: must be a non-empty string")
        rules = driver.get("rules")
        expected_rule_ids = [f"pr-codex/{category}" for category in CATEGORY_RULES]
        if not isinstance(rules, list):
            errors.append("$.runs[0].tool.driver.rules: must be an array")
        else:
            actual_rule_ids = [rule.get("id") for rule in rules if isinstance(rule, dict)]
            if actual_rule_ids != expected_rule_ids:
                errors.append("$.runs[0].tool.driver.rules: must fixed-list all 8 pr-codex category rules in schema order")
    invocations = run.get("invocations")
    if not isinstance(invocations, list) or not invocations:
        errors.append("$.runs[0].invocations: must be a non-empty array")
    else:
        invocation = invocations[0]
        if not isinstance(invocation, dict) or invocation.get("executionSuccessful") is not True:
            errors.append("$.runs[0].invocations[0].executionSuccessful: must be true")
        properties = invocation.get("properties") if isinstance(invocation, dict) else None
        if not isinstance(properties, dict) or not is_non_empty_string(properties.get("executionId")):
            errors.append("$.runs[0].invocations[0].properties.executionId: must carry producer.run_id")
    provenance = run.get("versionControlProvenance")
    if not isinstance(provenance, list) or not provenance or not isinstance(provenance[0], dict):
        errors.append("$.runs[0].versionControlProvenance: must describe repository/head revision")
    else:
        if not is_non_empty_string(provenance[0].get("repositoryUri")):
            errors.append("$.runs[0].versionControlProvenance[0].repositoryUri: must be a non-empty string")
        if not is_non_empty_string(provenance[0].get("revisionId")):
            errors.append("$.runs[0].versionControlProvenance[0].revisionId: must be a non-empty string")
    results = run.get("results")
    if not isinstance(results, list):
        errors.append("$.runs[0].results: must be an array")
        return errors
    for index, result in enumerate(results):
        rpath = f"$.runs[0].results[{index}]"
        if not isinstance(result, dict):
            errors.append(f"{rpath}: must be an object")
            continue
        if "fixes" in result:
            errors.append(f"{rpath}.fixes: must be omitted in M2; suggestions stay in message.text")
        rule_id = result.get("ruleId")
        if rule_id not in {f"pr-codex/{category}" for category in CATEGORY_RULES}:
            errors.append(f"{rpath}.ruleId: must be pr-codex/<category>")
        rule_index = result.get("ruleIndex")
        if not isinstance(rule_index, int) or isinstance(rule_index, bool) or rule_index < 0:
            errors.append(f"{rpath}.ruleIndex: must be a non-negative integer")
        if not isinstance(result.get("guid"), str) or not GUID_RE.match(result.get("guid", "")):
            errors.append(f"{rpath}.guid: must be a SARIF GUID")
        if result.get("level") not in {"error", "warning", "note", "none"}:
            errors.append(f"{rpath}.level: must be error/warning/note/none")
        message = result.get("message")
        if not isinstance(message, dict) or not is_non_empty_string(message.get("text")):
            errors.append(f"{rpath}.message.text: must be a non-empty string")
        elif "<absolute-path>" not in message.get("text", "") and re.search(r"/Users/|/home/|/private/var/", message.get("text", "")):
            errors.append(f"{rpath}.message.text: must not leak host absolute paths")
        partial = result.get("partialFingerprints")
        if not isinstance(partial, dict) or not isinstance(partial.get("canonical"), str) or not FINGERPRINT_RE.match(partial.get("canonical", "")):
            errors.append(f"{rpath}.partialFingerprints.canonical: must be the canonical finding fingerprint")
        properties = result.get("properties")
        if not isinstance(properties, dict):
            errors.append(f"{rpath}.properties: must be an object")
            continue
        severity = properties.get("severity")
        if severity not in SEVERITY_TO_LEVEL:
            errors.append(f"{rpath}.properties.severity: invalid pr-codex severity")
        elif result.get("level") != SEVERITY_TO_LEVEL[severity]:
            errors.append(f"{rpath}.level: must map from pr-codex severity {severity}")
        if properties.get("source_finding_id") != (partial or {}).get("canonical"):
            errors.append(f"{rpath}.properties.source_finding_id: must equal partialFingerprints.canonical")
        if properties.get("post_policy") == "suppress":
            errors.append(f"{rpath}: post_policy=suppress findings must not be emitted to SARIF")
        if severity == "nit" and not result.get("suppressions"):
            errors.append(f"{rpath}.suppressions: nit findings must be suppressed to avoid SARIF noise")
        if properties.get("post_policy") == "local_only" and not result.get("suppressions"):
            errors.append(f"{rpath}.suppressions: local_only findings must use SARIF suppression")
        if properties.get("category") == "security" and severity == "must_fix" and properties.get("security_severity_label") != "high":
            errors.append(f"{rpath}.properties.security_severity_label: security must_fix must be labelled high")
        if not isinstance(properties.get("axes"), dict):
            errors.append(f"{rpath}.properties.axes: must expose canonical axes")
        if not is_non_empty_string(properties.get("evidence_level")):
            errors.append(f"{rpath}.properties.evidence_level: must expose canonical evidence_level")
        validate_result_location(errors, rpath, result)
    return errors


def validate_result_location(errors: list[str], rpath: str, result: dict[str, Any]) -> None:
    locations = result.get("locations")
    if not isinstance(locations, list) or not locations:
        errors.append(f"{rpath}.locations: must be a non-empty array")
        return
    location = locations[0]
    physical = location.get("physicalLocation") if isinstance(location, dict) else None
    if not isinstance(physical, dict):
        errors.append(f"{rpath}.locations[0].physicalLocation: must be an object")
        return
    artifact = physical.get("artifactLocation")
    if not isinstance(artifact, dict) or not is_non_empty_string(artifact.get("uri")):
        errors.append(f"{rpath}.locations[0].physicalLocation.artifactLocation.uri: must be a non-empty repository path")
    elif str(artifact.get("uri")).startswith("/"):
        errors.append(f"{rpath}.locations[0].physicalLocation.artifactLocation.uri: must not be an absolute path")
    region = physical.get("region")
    if not isinstance(region, dict):
        errors.append(f"{rpath}.locations[0].physicalLocation.region: must be an object")
        return
    start_line = region.get("startLine")
    end_line = region.get("endLine")
    if not is_positive_int(start_line):
        errors.append(f"{rpath}.locations[0].physicalLocation.region.startLine: must be an integer >= 1")
    if not is_positive_int(end_line):
        errors.append(f"{rpath}.locations[0].physicalLocation.region.endLine: must be an integer >= 1")
    if is_positive_int(start_line) and is_positive_int(end_line) and end_line < start_line:
        errors.append(f"{rpath}.locations[0].physicalLocation.region.endLine: must be >= startLine")


def validate_against_findings(sarif: dict[str, Any], findings_artifact: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(findings_artifact, dict):
        return ["$findings: must be an object"]
    raw_findings = findings_artifact.get("findings")
    if not isinstance(raw_findings, list):
        return ["$findings.findings: must be an array"]
    expected: dict[str, dict[str, Any]] = {}
    suppressed_ids: set[str] = set()
    for index, finding in enumerate(raw_findings):
        if not isinstance(finding, dict):
            errors.append(f"$findings.findings[{index}]: must be an object")
            continue
        identifier = finding.get("id")
        posting = finding.get("posting") if isinstance(finding.get("posting"), dict) else {}
        if not isinstance(identifier, str):
            continue
        if posting.get("post_policy") == "suppress":
            suppressed_ids.add(identifier)
        else:
            expected[identifier] = finding

    results = sarif["runs"][0].get("results", []) if isinstance(sarif.get("runs"), list) and sarif.get("runs") else []
    actual_ids: set[str] = set()
    for result_index, result in enumerate(results):
        if not isinstance(result, dict):
            continue
        rpath = f"$.runs[0].results[{result_index}]"
        properties = result.get("properties") if isinstance(result.get("properties"), dict) else {}
        identifier = properties.get("source_finding_id")
        if not isinstance(identifier, str):
            continue
        if identifier in suppressed_ids:
            errors.append(f"{rpath}.properties.source_finding_id: suppress finding leaked into SARIF")
        finding = expected.get(identifier)
        if finding is None:
            errors.append(f"{rpath}.properties.source_finding_id: not found in findings.verified.json")
            continue
        actual_ids.add(identifier)
        location = finding.get("location") if isinstance(finding.get("location"), dict) else {}
        region = result["locations"][0]["physicalLocation"]["region"]
        artifact = result["locations"][0]["physicalLocation"]["artifactLocation"]
        expected_end = location.get("end_line", location.get("start_line"))
        if artifact.get("uri") != location.get("path"):
            errors.append(f"{rpath}.locations[0].physicalLocation.artifactLocation.uri: must match canonical path")
        if region.get("startLine") != location.get("start_line"):
            errors.append(f"{rpath}.locations[0].physicalLocation.region.startLine: must match canonical start_line")
        if region.get("endLine") != expected_end:
            errors.append(f"{rpath}.locations[0].physicalLocation.region.endLine: must match canonical end_line/start_line")
        if result.get("partialFingerprints", {}).get("canonical") != finding.get("fingerprint"):
            errors.append(f"{rpath}.partialFingerprints.canonical: must match canonical fingerprint")
    missing = sorted(set(expected) - actual_ids)
    if missing:
        errors.append(f"$.runs[0].results: missing canonical finding ids: {', '.join(missing[:3])}")
    if len(results) != len(expected):
        errors.append(f"$.runs[0].results: expected {len(expected)} emitted result(s), got {len(results)}")
    return errors


def validate_ranges(sarif: dict[str, Any], ranges_path: Path) -> list[str]:
    errors: list[str] = []
    ranges = parse_ranges(ranges_path)
    results = sarif["runs"][0].get("results", []) if isinstance(sarif.get("runs"), list) and sarif.get("runs") else []
    for index, result in enumerate(results):
        if not isinstance(result, dict):
            continue
        physical = result["locations"][0]["physicalLocation"]
        file_path = physical["artifactLocation"].get("uri")
        region = physical["region"]
        if not range_contains(ranges, file_path, region.get("startLine"), region.get("endLine")):
            errors.append(f"$.runs[0].results[{index}].locations[0]: SARIF region is outside pr.diff.ranges.txt")
    return errors


def validate_count_consistency(sarif: dict[str, Any], findings: Any | None, markdown: Path | None, payload: Path | None) -> list[str]:
    counts: dict[str, int] = {"sarif_must_fix": sarif_must_fix_count(sarif)}
    if findings is not None:
        counts["canonical_must_fix"] = canonical_must_fix_count(findings)
    if markdown is not None:
        counts["markdown_must_fix"] = markdown_must_fix_count(markdown)
    if payload is not None:
        counts["payload_must_fix"] = payload_must_fix_count(payload)
    if any(value < 0 for value in counts.values()) or len(set(counts.values())) != 1:
        return ["must_fix_count_mismatch: " + ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))]
    return []


def validate_findings_sarif(
    schema: Any,
    sarif: Any,
    *,
    findings: Any | None = None,
    ranges_path: Path | None = None,
    markdown_path: Path | None = None,
    payload_path: Path | None = None,
) -> list[str]:
    errors = validate_schema_file(schema)
    errors.extend(validate_sarif_shape(sarif))
    if errors:
        return errors
    if isinstance(sarif, dict) and findings is not None:
        errors.extend(validate_against_findings(sarif, findings))
    if isinstance(sarif, dict) and ranges_path is not None:
        errors.extend(validate_ranges(sarif, ranges_path))
    if isinstance(sarif, dict):
        errors.extend(validate_count_consistency(sarif, findings, markdown_path, payload_path))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate pr-codex findings.sarif derived artifacts")
    parser.add_argument("--schema", required=True, type=Path, help="schemas/sarif-2.1.0.json path")
    parser.add_argument("--data", required=True, type=Path, help="findings.sarif path")
    parser.add_argument("--findings", type=Path, help="findings.verified.json source for cross-artifact validation")
    parser.add_argument("--ranges", type=Path, help="pr.diff.ranges.txt path for RIGHT-side location validation")
    parser.add_argument("--markdown", type=Path, help="review.md path for Must Fix count consistency")
    parser.add_argument("--payload", type=Path, help="review-payload.json path for Must Fix count consistency")
    parser.add_argument("--emit-counts", action="store_true", help="print Must Fix counts JSON after validation")
    args = parser.parse_args()

    try:
        schema = load_json(args.schema)
        sarif = load_json(args.data)
        findings = load_json(args.findings) if args.findings else None
        errors = validate_findings_sarif(
            schema,
            sarif,
            findings=findings,
            ranges_path=args.ranges,
            markdown_path=args.markdown,
            payload_path=args.payload,
        )
    except Exception as exc:  # noqa: BLE001 - keep CLI errors concise
        print(f"INVALID findings SARIF artifact: {exc}", file=sys.stderr)
        return 2

    if errors:
        print("INVALID findings SARIF artifact", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    if args.emit_counts:
        counts = {"sarif_must_fix": sarif_must_fix_count(sarif)}
        if findings is not None:
            counts["canonical_must_fix"] = canonical_must_fix_count(findings)
        if args.markdown:
            counts["markdown_must_fix"] = markdown_must_fix_count(args.markdown)
        if args.payload:
            counts["payload_must_fix"] = payload_must_fix_count(args.payload)
        print(json.dumps(counts, ensure_ascii=False, sort_keys=True))
    else:
        print("VALID findings SARIF artifact")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
