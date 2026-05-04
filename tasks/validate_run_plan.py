#!/usr/bin/env python3
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "schemas" / "run-plan.schema.json"
FIXTURE_ROOT = ROOT / "fixtures"

DEPENDENCY_FILES = {
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lockb",
    "composer.json",
    "composer.lock",
    "gemfile",
    "gemfile.lock",
    "go.mod",
    "go.sum",
    "cargo.toml",
    "cargo.lock",
    "requirements.txt",
    "poetry.lock",
    "pyproject.toml",
}

SECURITY_RE = re.compile(
    r"(^|/)(auth|oauth|permission|policy|guard|acl|session|csrf|jwt|token|secret|password|security|middleware)(/|$|\.)",
    re.IGNORECASE,
)
DATA_MIGRATION_RE = re.compile(
    r"(^|/)(migrations?|schema|ddl|sql|seed|database|db|prisma|alembic|flyway|liquibase)(/|$|\.)",
    re.IGNORECASE,
)
INFRA_RE = re.compile(
    r"(^|/)(\.github/|Dockerfile$|docker-compose|helm/|k8s/|terraform/|deploy/|ops/)",
    re.IGNORECASE,
)
TEST_TOUCH_RE = re.compile(
    r"(^|/)(tests?|spec)(/|$)|(^|/).*(Test|Spec)\.[^/]+$",
    re.IGNORECASE,
)
API_CONTRACT_RE = re.compile(
    r"(openapi|swagger|schema\.graphql|\.proto$)",
    re.IGNORECASE,
)


def load_json(path: Path) -> object:
    with path.open() as f:
        return json.load(f)


def parse_diff(diff_path: Path) -> tuple[list[str], int, int, int]:
    files: list[str] = []
    hunks = 0
    lines_added = 0
    lines_removed = 0
    for line in diff_path.read_text().splitlines():
        if line.startswith("diff --git "):
            match = re.match(r"diff --git a/(.+?) b/(.+)$", line)
            if match:
                files.append(match.group(2))
        elif line.startswith("@@"):
            hunks += 1
        elif line.startswith("+") and not line.startswith("+++"):
            lines_added += 1
        elif line.startswith("-") and not line.startswith("---"):
            lines_removed += 1
    return files, hunks, lines_added, lines_removed


def infer_risk_tags(files: list[str]) -> list[str]:
    tags: list[str] = []
    normalized = [path.lower() for path in files]

    def add(tag: str, matched: bool) -> None:
        if matched and tag not in tags:
            tags.append(tag)

    add("security", any(SECURITY_RE.search(path) for path in normalized))
    add("data_migration", any(DATA_MIGRATION_RE.search(path) for path in normalized))
    add("dependency", any(Path(path).name.lower() in DEPENDENCY_FILES for path in normalized))
    add("infra", any(INFRA_RE.search(path) for path in normalized))
    add("test_touch", any(TEST_TOUCH_RE.search(path) for path in normalized))
    add("api_contract", any(API_CONTRACT_RE.search(path) for path in normalized))
    return tags


def build_run_plan(files: list[str], hunks: int, lines_added: int, lines_removed: int) -> dict[str, object]:
    files_changed = len(files)
    total_lines = lines_added + lines_removed
    risk_tags = infer_risk_tags(files)
    sensitive_risk_count = sum(1 for tag in risk_tags if tag in {"security", "data_migration"})

    if files_changed > 100:
        recommended_mode = "skip"
        skip_reason = "files_changed > 100: /loop では skip 提案、手動では警告のみ。M1 の既定では focused fallback を適用"
        estimated_stages = 6
    elif files_changed > 50:
        recommended_mode = "focused"
        skip_reason = None
        estimated_stages = 5
    else:
        recommended_mode = "standard"
        skip_reason = None
        estimated_stages = 4

    depth_actual = "standard" if total_lines > 5000 else "deep"
    estimated_timeout_ms = min(
        1200000,
        300000
        + files_changed * 30000
        + hunks * 15000
        + total_lines * 100
        + sensitive_risk_count * 90000,
    )

    return {
        "files_changed": files_changed,
        "hunks": hunks,
        "lines_added": lines_added,
        "lines_removed": lines_removed,
        "risk_tags": risk_tags,
        "selected_hunters": ["claude", "codex"],
        "depth_actual": depth_actual,
        "recommended_mode": recommended_mode,
        "skip_reason": skip_reason,
        "estimated_stages": estimated_stages,
        "estimated_timeout_ms": estimated_timeout_ms,
        "actual_duration_ms": None,
        "actual_tokens": None,
    }


def with_actual_duration(plan: dict[str, object], started_at: str, finished_at: str) -> dict[str, object]:
    started = datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%S+00:00")
    finished = datetime.strptime(finished_at, "%Y-%m-%dT%H:%M:%S+00:00")
    updated = dict(plan)
    updated["actual_duration_ms"] = int((finished - started).total_seconds() * 1000)
    return updated


def json_type_matches(expected: str, value: object) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "string":
        return isinstance(value, str)
    if expected == "null":
        return value is None
    raise ValueError(f"unsupported schema type: {expected}")


def validate_schema(schema: dict[str, object], value: object, path: str = "$") -> None:
    expected_type = schema.get("type")
    if expected_type is not None:
        if isinstance(expected_type, list):
            if not any(json_type_matches(item, value) for item in expected_type):
                raise AssertionError(f"{path}: expected one of {expected_type}, got {type(value).__name__}")
        else:
            if not json_type_matches(expected_type, value):
                raise AssertionError(f"{path}: expected {expected_type}, got {type(value).__name__}")

    if "enum" in schema and value not in schema["enum"]:
        raise AssertionError(f"{path}: expected one of {schema['enum']}, got {value!r}")

    if value is None:
        return

    if isinstance(value, (int, float)) and "minimum" in schema and value < schema["minimum"]:
        raise AssertionError(f"{path}: expected >= {schema['minimum']}, got {value}")

    if isinstance(value, str) and "minLength" in schema and len(value) < schema["minLength"]:
        raise AssertionError(f"{path}: expected length >= {schema['minLength']}, got {len(value)}")

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise AssertionError(f"{path}: expected at least {schema['minItems']} items, got {len(value)}")
        if schema.get("uniqueItems") and len(value) != len({json.dumps(item, sort_keys=True) for item in value}):
            raise AssertionError(f"{path}: expected unique items")
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(value):
                validate_schema(item_schema, item, f"{path}[{index}]")
        return

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        for key in required:
            if key not in value:
                raise AssertionError(f"{path}: missing required property {key!r}")
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(properties)
            if extra:
                raise AssertionError(f"{path}: unexpected properties {sorted(extra)}")
        for key, child_schema in properties.items():
            if key in value:
                validate_schema(child_schema, value[key], f"{path}.{key}")


def validate_fixture(name: str, schema: dict[str, object]) -> None:
    metadata = load_json(FIXTURE_ROOT / name / "metadata.json")
    expected = load_json(FIXTURE_ROOT / name / "run-plan.expected.json")
    files, hunks, lines_added, lines_removed = parse_diff(FIXTURE_ROOT / name / "diff.patch")

    assert metadata["source"]["changed_files"] == len(files), f"{name}: changed_files mismatch"
    assert metadata["source"]["additions"] == lines_added, f"{name}: additions mismatch"
    assert metadata["source"]["deletions"] == lines_removed, f"{name}: deletions mismatch"

    actual = build_run_plan(files, hunks, lines_added, lines_removed)
    assert actual == expected, f"{name}: run-plan mismatch\nexpected={expected}\nactual={actual}"
    validate_schema(schema, expected)


def validate_threshold_behavior(schema: dict[str, object]) -> None:
    line_heavy_files = [f"src/module_{index}.ts" for index in range(20)]
    line_heavy = build_run_plan(line_heavy_files, hunks=80, lines_added=5001, lines_removed=0)
    assert line_heavy["depth_actual"] == "standard"
    assert line_heavy["recommended_mode"] == "standard"
    validate_schema(schema, line_heavy)

    focused_files = [f"src/file_{index}.ts" for index in range(51)]
    focused = build_run_plan(focused_files, hunks=120, lines_added=800, lines_removed=400)
    assert focused["recommended_mode"] == "focused"
    assert focused["skip_reason"] is None
    validate_schema(schema, focused)

    skip_files = [f"src/file_{index}.ts" for index in range(101)]
    skipped = build_run_plan(skip_files, hunks=240, lines_added=1500, lines_removed=900)
    assert skipped["recommended_mode"] == "skip"
    assert isinstance(skipped["skip_reason"], str) and skipped["skip_reason"]
    validate_schema(schema, skipped)

    duration_case = with_actual_duration(
        build_run_plan(["src/file.ts"], hunks=1, lines_added=1, lines_removed=0),
        "2026-05-04T10:00:00+00:00",
        "2026-05-04T10:20:00+00:00",
    )
    assert duration_case["actual_duration_ms"] == 1200000
    validate_schema(schema, duration_case)


def main() -> None:
    schema = load_json(SCHEMA_PATH)
    for fixture in ("small", "medium", "large"):
        validate_fixture(fixture, schema)
    validate_threshold_behavior(schema)
    print("run-plan validation passed")


if __name__ == "__main__":
    main()
