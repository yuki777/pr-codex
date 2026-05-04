#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "fixtures"
SCHEMA_PATH = ROOT / "schemas" / "run-plan.schema.json"
SKILL_PATH = ROOT / "skills" / "review" / "SKILL.md"

RISK_TAG_ENUM = [
    "security",
    "data_migration",
    "dependency",
    "infra",
    "test_touch",
    "api_contract",
]


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


def extract_bash_block(containing: str) -> str:
    text = SKILL_PATH.read_text()
    for block in re.findall(r"```bash\n(.*?)\n```", text, flags=re.DOTALL):
        if containing in block:
            return block
    raise AssertionError(f"bash block containing {containing!r} not found")


def extract_jq_filter(block: str) -> str:
    start = block.index("'") + 1
    end = block.rindex("'")
    return block[start:end]


def run_jq(args: list[str], jq_filter: str, input_text: str | None = None) -> object:
    completed = subprocess.run(
        ["jq", *args, jq_filter],
        capture_output=True,
        input=input_text,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            "jq execution failed\n"
            f"args={args}\n"
            f"stdout={completed.stdout}\n"
            f"stderr={completed.stderr}"
        )
    return json.loads(completed.stdout)


def preflight_block() -> str:
    block = extract_bash_block("jq -n --slurpfile metadata")
    required = "&& test -s ~/claude-loop-pr-codex/$org-$repository-$pr_number/run-plan.json"
    if required not in block:
        raise AssertionError("run-plan template must guard with && test -s")
    return block


def duration_block() -> str:
    return extract_bash_block("jq -n --argjson files_changed")


def files_list_block() -> str:
    block = extract_bash_block("gh api repos/$org/$repository/pulls/$pr_number/files --paginate")
    if "--json headRefOid,headRefName,baseRefName,files" in block:
        raise AssertionError("Step 2b must not depend on gh pr view --json files")
    return block


def run_preflight_template(metadata_path: Path, diff_path: Path) -> object:
    return run_jq(
        [
            "-n",
            "--slurpfile",
            "metadata",
            str(metadata_path),
            "--rawfile",
            "diff",
            str(diff_path),
        ],
        extract_jq_filter(preflight_block()),
    )


def run_files_list_template(pages: list[list[dict[str, str]]]) -> object:
    input_text = "".join(json.dumps(page, ensure_ascii=False) + "\n" for page in pages)
    return run_jq(["-s", "-c", "-e"], extract_jq_filter(files_list_block()), input_text=input_text)


def run_duration_template(plan: dict[str, object], started_at: str, finished_at: str) -> object:
    return run_jq(
        [
            "-n",
            "--argjson",
            "files_changed",
            str(plan["files_changed"]),
            "--argjson",
            "hunks",
            str(plan["hunks"]),
            "--argjson",
            "lines_added",
            str(plan["lines_added"]),
            "--argjson",
            "lines_removed",
            str(plan["lines_removed"]),
            "--argjson",
            "risk_tags",
            json.dumps(plan["risk_tags"]),
            "--argjson",
            "selected_hunters",
            json.dumps(plan["selected_hunters"]),
            "--arg",
            "depth_actual",
            str(plan["depth_actual"]),
            "--arg",
            "recommended_mode",
            str(plan["recommended_mode"]),
            "--arg",
            "skip_reason",
            "null" if plan["skip_reason"] is None else str(plan["skip_reason"]),
            "--argjson",
            "estimated_stages",
            str(plan["estimated_stages"]),
            "--argjson",
            "estimated_timeout_ms",
            str(plan["estimated_timeout_ms"]),
            "--arg",
            "started_at",
            started_at,
            "--arg",
            "finished_at",
            finished_at,
        ],
        extract_jq_filter(duration_block()),
    )


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
        elif not json_type_matches(expected_type, value):
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
    diff_path = FIXTURE_ROOT / name / "diff.patch"
    files, hunks, lines_added, lines_removed = parse_diff(diff_path)

    assert metadata["source"]["changed_files"] == len(files), f"{name}: changed_files mismatch"
    assert metadata["source"]["additions"] == lines_added, f"{name}: additions mismatch"
    assert metadata["source"]["deletions"] == lines_removed, f"{name}: deletions mismatch"

    with tempfile.TemporaryDirectory() as tmpdir:
        runtime_metadata_path = Path(tmpdir) / "metadata.json"
        runtime_metadata_path.write_text(json.dumps({"files": files}, ensure_ascii=False, indent=2) + "\n")
        actual = run_preflight_template(runtime_metadata_path, diff_path)

    assert actual == expected, f"{name}: run-plan mismatch\nexpected={expected}\nactual={actual}"
    validate_schema(schema, expected)


def make_diff_text(files: list[str], hunks: int, lines_added: int, lines_removed: int) -> str:
    lines: list[str] = []
    for file in files:
        lines.append(f"diff --git a/{file} b/{file}")
        lines.append(f"--- a/{file}")
        lines.append(f"+++ b/{file}")
    lines.extend("@@ synthetic @@" for _ in range(hunks))
    lines.extend("+added" for _ in range(lines_added))
    lines.extend("-removed" for _ in range(lines_removed))
    return "\n".join(lines) + "\n"


def synthetic_plan(files: list[str], hunks: int, lines_added: int, lines_removed: int) -> object:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        metadata_path = tmp / "metadata.json"
        diff_path = tmp / "pr.diff"
        metadata_path.write_text(json.dumps({"files": files}, ensure_ascii=False, indent=2) + "\n")
        diff_path.write_text(make_diff_text(files, hunks, lines_added, lines_removed))
        return run_preflight_template(metadata_path, diff_path)


def validate_threshold_behavior(schema: dict[str, object]) -> None:
    line_heavy = synthetic_plan([f"src/module_{index}.ts" for index in range(20)], 80, 5001, 0)
    assert line_heavy["depth_actual"] == "standard"
    assert line_heavy["recommended_mode"] == "standard"
    validate_schema(schema, line_heavy)

    focused = synthetic_plan([f"src/file_{index}.ts" for index in range(51)], 120, 800, 400)
    assert focused["recommended_mode"] == "focused"
    assert focused["skip_reason"] is None
    validate_schema(schema, focused)

    skipped = synthetic_plan([f"src/file_{index}.ts" for index in range(101)], 240, 1500, 900)
    assert skipped["recommended_mode"] == "skip"
    assert isinstance(skipped["skip_reason"], str) and skipped["skip_reason"]
    validate_schema(schema, skipped)

    duration_case = run_duration_template(
        focused,
        "2026-05-04T10:00:00+00:00",
        "2026-05-04T10:20:00+00:00",
    )
    assert duration_case["actual_duration_ms"] == 1200000
    assert duration_case["risk_tags"] == focused["risk_tags"]
    validate_schema(schema, duration_case)


def validate_paginated_files_template() -> None:
    page_one = [{"filename": f"src/file_{index}.ts"} for index in range(100)]
    page_two = [{"filename": "src/file_100.ts"}]
    actual = run_files_list_template([page_one, page_two])
    expected = [item["filename"] for item in page_one + page_two]
    assert actual == expected, f"paginated files mismatch\nexpected={expected}\nactual={actual}"


def validate_step5_write_order() -> None:
    text = SKILL_PATH.read_text()
    duration_index = text.index('tmp_run_plan=~/claude-loop-pr-codex/$org-$repository-$pr_number/run-plan.json.tmp')
    completed_index = text.index('{state:"completed",started_at:$started_at,finished_at:$finished_at,exit_code:0,head_sha:$head_sha}')
    if duration_index > completed_index:
        raise AssertionError("run-plan update must be documented before completed status update")

    block = duration_block()
    required_snippets = [
        'tmp_run_plan=~/claude-loop-pr-codex/$org-$repository-$pr_number/run-plan.json.tmp',
        '> "$tmp_run_plan" && test -s "$tmp_run_plan" && mv "$tmp_run_plan" ~/claude-loop-pr-codex/$org-$repository-$pr_number/run-plan.json',
    ]
    for snippet in required_snippets:
        if snippet not in block:
            raise AssertionError(f"duration block missing required snippet: {snippet}")


def validate_schema_contract(schema: dict[str, object]) -> None:
    items = schema["properties"]["risk_tags"]["items"]
    assert items["enum"] == RISK_TAG_ENUM, f"risk_tags enum mismatch: {items['enum']}"


def main() -> None:
    schema = load_json(SCHEMA_PATH)
    validate_schema_contract(schema)
    validate_paginated_files_template()
    for fixture in ("small", "medium", "large"):
        validate_fixture(fixture, schema)
    validate_threshold_behavior(schema)
    validate_step5_write_order()
    print("run-plan validation passed")


if __name__ == "__main__":
    main()
