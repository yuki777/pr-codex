#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import shlex
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "fixtures"
SCHEMA_PATH = ROOT / "schemas" / "run-plan.schema.json"
CLASSIFICATION_SCHEMA_PATH = ROOT / "schemas" / "pr-classification.schema.json"
SKILL_PATH = ROOT / "skills" / "review" / "SKILL.md"

RISK_TAG_ENUM = [
    "security",
    "data_migration",
    "dependency",
    "infra",
    "test_touch",
    "api_contract",
]

PR_TYPE_ENUM = [
    "docs-only",
    "test-only",
    "workflow-ci",
    "review-skill-contract",
    "python-validator-runtime",
    "security-sensitive",
    "mixed",
]

SPECIALIST_ENUM = [
    "generic",
    "docs",
    "tests",
    "workflow",
    "review-skill",
    "python",
    "security",
]

SENSITIVE_RISK_TAGS = {"security", "data_migration"}
ROUTE_M2 = "claude+codex"

RISK_TAG_CASES = [
    ("src/auth/login.go", {"security"}),
    ("config/oauth.json", {"security"}),
    ("notauth.go", set()),
    ("db/seed.sql", {"data_migration"}),
    ("composer.json", {"dependency"}),
    (".github/workflows/ci.yml", {"infra"}),
    ("tests/foo_test.go", {"test_touch"}),
    ("src/UserTest.php", {"test_touch"}),
    ("latest.go", set()),
    ("attest.go", set()),
    ("api/openapi.yaml", {"api_contract"}),
    ("docs/swagger.json", {"api_contract"}),
    ("schema.graphql", {"data_migration", "api_contract"}),
    ("proto/service.proto", {"api_contract"}),
    ("notopenapi.go", set()),
    ("frontend/swagger-foo-helper.go", set()),
    ("myschema.graphql.bak", set()),
]

ESCAPE_RULE_TEXT = '`\\` → `\\\\`、`"` → `\\"`、`$` → `\\$`、`` ` `` → `\\``'

DEPTH_REASON_LARGE_DEFAULT = "changed lines > 5000; selected standard to preserve the 20 minute timeout"
DEPTH_REASON_AUTO_DEEP = (
    "risk_tags include security or data_migration and PR size is <= 20 files / <= 1500 changed lines; selected deep"
)
DEPTH_REASON_DEFAULT_STANDARD = "no high-risk small-PR signal; selected default standard"


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
    if "set -o pipefail && gh api repos/$org/$repository/pulls/$pr_number/files --paginate" not in block:
        raise AssertionError("Step 2b paginated files template must enable pipefail")
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
    routing_decision = plan["routing_decision"]
    assert isinstance(routing_decision, dict)
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
            "--argjson",
            "pr_classification",
            json.dumps(plan["pr_classification"]),
            "--arg",
            "depth_actual",
            str(plan["depth_actual"]),
            "--arg",
            "depth_source",
            str(plan["depth_source"]),
            "--arg",
            "depth_reason",
            str(plan["depth_reason"]),
            "--arg",
            "depth_requested",
            "null" if plan["depth_requested"] is None else str(plan["depth_requested"]),
            "--argjson",
            "depth_downgraded",
            json.dumps(plan["depth_downgraded"]),
            "--arg",
            "depth_downgrade_reason",
            "null" if plan["depth_downgrade_reason"] is None else str(plan["depth_downgrade_reason"]),
            "--arg",
            "recommended_mode",
            str(plan["recommended_mode"]),
            "--arg",
            "skip_reason",
            "null" if plan["skip_reason"] is None else str(plan["skip_reason"]),
            "--arg",
            "budget_class",
            str(routing_decision["budget_class"]),
            "--arg",
            "model_profile",
            str(routing_decision["model_profile"]),
            "--arg",
            "route",
            str(routing_decision["route"]),
            "--arg",
            "rationale",
            str(routing_decision["rationale"]),
            "--argjson",
            "estimated_stages",
            str(plan["estimated_stages"]),
            "--argjson",
            "estimated_timeout_ms",
            str(plan["estimated_timeout_ms"]),
            "--argjson",
            "review_loop",
            json.dumps(plan["review_loop"]),
            "--argjson",
            "cost",
            json.dumps(plan.get("cost", {"actual_usd": None, "currency": "USD", "source": "unavailable", "components": []})),
            "--argjson",
            "rounds_completed",
            "2",
            "--arg",
            "halt_reason",
            "all_candidates_verified",
            "--argjson",
            "verifier_fail_candidates",
            "1",
            "--argjson",
            "suppressed_candidate_count",
            "1",
            "--argjson",
            "no_new_evidence_rounds",
            "0",
            "--argjson",
            "repeated_contradiction_events",
            "0",
            "--argjson",
            "insufficient_evidence_events",
            "0",
            "--argjson",
            "oscillation_detected",
            "false",
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
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    if expected == "boolean":
        return isinstance(value, bool)
    raise ValueError(f"unsupported schema type: {expected}")


def validate_schema(schema: dict[str, object], value: object, path: str = "$") -> None:
    if "$ref" in schema:
        ref = schema["$ref"]
        if ref == "pr-classification.schema.json":
            validate_schema(load_json(CLASSIFICATION_SCHEMA_PATH), value, path)
            return
        raise AssertionError(f"{path}: unsupported schema $ref {ref!r}")

    expected_type = schema.get("type")
    if expected_type is not None:
        if isinstance(expected_type, list):
            if not any(json_type_matches(item, value) for item in expected_type):
                raise AssertionError(f"{path}: expected one of {expected_type}, got {type(value).__name__}")
        elif not json_type_matches(expected_type, value):
            raise AssertionError(f"{path}: expected {expected_type}, got {type(value).__name__}")

    if "const" in schema and value != schema["const"]:
        raise AssertionError(f"{path}: expected const {schema['const']!r}, got {value!r}")

    if "enum" in schema and value not in schema["enum"]:
        raise AssertionError(f"{path}: expected one of {schema['enum']}, got {value!r}")

    for index, child_schema in enumerate(schema.get("allOf", [])):
        validate_schema(child_schema, value, f"{path}.allOf[{index}]")

    if "if" in schema:
        branch = schema.get("then") if schema_matches(schema["if"], value) else schema.get("else")
        if branch:
            validate_schema(branch, value, path)

    if value is None:
        return

    if isinstance(value, (int, float)) and "minimum" in schema and value < schema["minimum"]:
        raise AssertionError(f"{path}: expected >= {schema['minimum']}, got {value}")

    if isinstance(value, str) and "minLength" in schema and len(value) < schema["minLength"]:
        raise AssertionError(f"{path}: expected length >= {schema['minLength']}, got {len(value)}")
    if isinstance(value, str) and "maxLength" in schema and len(value) > schema["maxLength"]:
        raise AssertionError(f"{path}: expected length <= {schema['maxLength']}, got {len(value)}")
    if isinstance(value, str) and "pattern" in schema and not re.search(str(schema["pattern"]), value):
        raise AssertionError(f"{path}: expected pattern {schema['pattern']!r}, got {value!r}")

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise AssertionError(f"{path}: expected at least {schema['minItems']} items, got {len(value)}")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise AssertionError(f"{path}: expected at most {schema['maxItems']} items, got {len(value)}")
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


def schema_matches(schema: dict[str, object], value: object) -> bool:
    try:
        validate_schema(schema, value)
    except AssertionError:
        return False
    return True


def total_lines(plan: dict[str, object]) -> int:
    return int(plan["lines_added"]) + int(plan["lines_removed"])


def sensitive_risk_count(plan: dict[str, object]) -> int:
    risk_tags = plan["risk_tags"]
    if not isinstance(risk_tags, list):
        raise AssertionError("$.risk_tags: expected array before routing validation")
    return sum(1 for tag in risk_tags if tag in SENSITIVE_RISK_TAGS)


def expected_budget_class(plan: dict[str, object]) -> str:
    files_changed = int(plan["files_changed"])
    lines = total_lines(plan)
    if files_changed <= 10 and lines <= 500 and sensitive_risk_count(plan) == 0:
        return "small"
    if files_changed <= 50 and lines <= 5000:
        return "medium"
    return "large"


def expected_model_profile(plan: dict[str, object]) -> str:
    recommended_mode = plan["recommended_mode"]
    depth_actual = plan["depth_actual"]
    if recommended_mode == "standard" and depth_actual == "deep":
        return "deep"
    if recommended_mode == "standard" and depth_actual == "standard":
        return "standard"
    if recommended_mode in {"focused", "skip"}:
        return "focused-fallback"
    raise AssertionError(f"$.recommended_mode: unsupported value for routing validation: {recommended_mode!r}")


def expected_rationale(plan: dict[str, object]) -> str:
    risk_tags = plan["risk_tags"]
    if not isinstance(risk_tags, list) or not all(isinstance(tag, str) for tag in risk_tags):
        raise AssertionError("$.risk_tags: expected string array before routing validation")
    return (
        f"files_changed={plan['files_changed']}, "
        f"total_lines={total_lines(plan)}, "
        f"risk_tags=[{','.join(risk_tags)}], "
        f"depth={plan['depth_actual']}, "
        f"mode={plan['recommended_mode']}"
    )


def _files(plan: dict[str, object]) -> list[str]:
    raw_files = plan.get("_files") or plan.get("files")
    if raw_files is None:
        # synthetic/preflight plans do not expose file names publicly; they store
        # the deterministic classification artifact instead.
        classification = plan.get("pr_classification")
        if isinstance(classification, dict):
            return []
        raise AssertionError("plan files unavailable for classification validation")
    if not isinstance(raw_files, list) or not all(isinstance(item, str) for item in raw_files):
        raise AssertionError("plan files must be a string array")
    return raw_files


def _is_docs_file(path: str) -> bool:
    return bool(re.search(r"(^|/)(docs?/|README([.]|$)|CHANGELOG([.]|$)|CONTRIBUTING([.]|$))|[.](md|mdx|rst|adoc|txt)$", path, re.I))


def _is_test_file(path: str) -> bool:
    return bool(re.search(r"(^|/)(tests?|spec)(/|$)|(^|/)[^/]*[._-](test|spec)[.][^/]+$", path, re.I) or re.search(r"(^|/)[^/]*(Test|Spec)[.][^/]+$", path))


def _is_workflow_file(path: str) -> bool:
    return bool(re.search(r"(^|/)([.]github/workflows/|Dockerfile$|docker-compose|helm/|k8s/|terraform/|deploy/|ops/)", path, re.I))


def _is_review_skill_file(path: str) -> bool:
    return bool(re.search(r"(^|/)skills/(review|send)/|(^|/)schemas/(findings|run-plan|pr-classification)", path, re.I))


def _is_python_runtime_file(path: str) -> bool:
    return bool(re.search(r"(^|/)tasks/.*[.]py$|(^|/)schemas/.*[.]json$", path, re.I))


def expected_pr_classification(plan: dict[str, object]) -> dict[str, object]:
    files = _files(plan)
    risk_tags = plan.get("risk_tags", [])
    if not isinstance(risk_tags, list) or not all(isinstance(tag, str) for tag in risk_tags):
        raise AssertionError("$.risk_tags: expected string array before classification validation")

    flags = {
        "docs-only": any(_is_docs_file(path) for path in files),
        "test-only": any(_is_test_file(path) for path in files),
        "workflow-ci": any(_is_workflow_file(path) for path in files),
        "review-skill-contract": any(_is_review_skill_file(path) for path in files),
        "python-validator-runtime": any(_is_python_runtime_file(path) for path in files),
        "security-sensitive": "security" in risk_tags,
    }
    all_types = [type_name for type_name in PR_TYPE_ENUM if type_name != "mixed" and flags[type_name]]
    if not all_types:
        all_types = ["docs-only"] if files and all(_is_docs_file(path) for path in files) else ["test-only"] if files and all(_is_test_file(path) for path in files) else []
    if not all_types:
        all_types = []

    if "security-sensitive" in all_types:
        primary_type = "security-sensitive"
    elif len(all_types) == 1:
        primary_type = all_types[0]
    else:
        primary_type = "mixed"

    specialist_map = {
        "docs-only": "docs",
        "test-only": "tests",
        "workflow-ci": "workflow",
        "review-skill-contract": "review-skill",
        "python-validator-runtime": "python",
        "security-sensitive": "security",
    }
    selected_specialists = [specialist_map[type_name] for type_name in all_types]
    if not selected_specialists:
        selected_specialists = ["generic"]
    rationale = f"types=[{','.join(all_types)}], specialists=[{','.join(selected_specialists)}], read_only=true"
    return {
        "primary_type": primary_type,
        "all_types": all_types,
        "selected_specialists": selected_specialists,
        "rationale": rationale,
        "read_only": True,
    }


def validate_pr_classification_semantics(
    schema: dict[str, object], classification: dict[str, object], plan: dict[str, object]
) -> None:
    validate_schema(schema, classification)
    expected = expected_pr_classification(plan)
    for key, expected_value in expected.items():
        actual = classification.get(key)
        if actual != expected_value:
            raise AssertionError(f"$.pr_classification.{key}: expected {expected_value!r}, got {actual!r}")


def validate_routing_decision(plan: dict[str, object]) -> None:
    routing_decision = plan.get("routing_decision")
    if not isinstance(routing_decision, dict):
        raise AssertionError("$.routing_decision: expected object")

    expected = {
        "budget_class": expected_budget_class(plan),
        "route": ROUTE_M2,
        "model_profile": expected_model_profile(plan),
        "rationale": expected_rationale(plan),
    }
    for key, expected_value in expected.items():
        actual = routing_decision.get(key)
        if actual != expected_value:
            raise AssertionError(
                f"$.routing_decision.{key}: expected {expected_value!r}, got {actual!r}"
            )

    rationale = routing_decision["rationale"]
    if not isinstance(rationale, str) or len(rationale) > 240:
        raise AssertionError("$.routing_decision.rationale: expected string length <= 240")


def validate_cost(plan: dict[str, object]) -> None:
    cost = plan.get("cost")
    if not isinstance(cost, dict):
        raise AssertionError("$.cost: expected object")
    source = cost.get("source")
    actual_usd = cost.get("actual_usd")
    components = cost.get("components")
    if source == "unavailable":
        if actual_usd is not None or components != []:
            raise AssertionError("$.cost: unavailable source must have null actual_usd and empty components")
        return
    if source != "provider_reported":
        raise AssertionError(f"$.cost.source: unsupported value {source!r}")
    if not isinstance(actual_usd, (int, float)) or isinstance(actual_usd, bool):
        raise AssertionError("$.cost.actual_usd: provider_reported cost must be numeric")
    if not isinstance(components, list) or not components:
        raise AssertionError("$.cost.components: provider_reported cost must include at least one component")
    component_total = 0.0
    for component in components:
        if not isinstance(component, dict):
            raise AssertionError("$.cost.components[]: expected object")
        amount = component.get("actual_usd")
        if not isinstance(amount, (int, float)) or isinstance(amount, bool):
            raise AssertionError("$.cost.components[].actual_usd: expected number")
        component_total += float(amount)
    if round(component_total, 4) != round(float(actual_usd), 4):
        raise AssertionError("$.cost.actual_usd: expected sum of component actual_usd values")


def validate_run_plan_semantics(schema: dict[str, object], plan: dict[str, object]) -> None:
    validate_schema(schema, plan)
    classification_schema = load_json(CLASSIFICATION_SCHEMA_PATH)
    classification = plan.get("pr_classification")
    if not isinstance(classification, dict):
        raise AssertionError("$.pr_classification: expected object")
    validate_schema(classification_schema, classification)
    validate_routing_decision(plan)
    validate_cost(plan)


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
    validate_run_plan_semantics(schema, expected)


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


def synthetic_plan(
    files: list[str],
    hunks: int,
    lines_added: int,
    lines_removed: int,
) -> object:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        metadata_path = tmp / "metadata.json"
        diff_path = tmp / "pr.diff"
        metadata_path.write_text(json.dumps({"files": files}, ensure_ascii=False, indent=2) + "\n")
        diff_path.write_text(make_diff_text(files, hunks, lines_added, lines_removed))
        return run_preflight_template(metadata_path, diff_path)


def validate_threshold_behavior(schema: dict[str, object]) -> None:
    compact_default = synthetic_plan(["src/module.ts"], 1, 100, 50)
    assert compact_default["depth_actual"] == "standard"
    assert compact_default["depth_source"] == "default"
    assert compact_default["depth_requested"] is None
    assert compact_default["depth_reason"] == DEPTH_REASON_DEFAULT_STANDARD
    assert compact_default["depth_downgraded"] is False
    assert compact_default["depth_downgrade_reason"] is None
    validate_schema(schema, compact_default)

    security_auto = synthetic_plan(["src/auth/login.go"], 2, 100, 50)
    assert security_auto["risk_tags"] == ["security"]
    assert security_auto["depth_actual"] == "deep"
    assert security_auto["depth_source"] == "auto"
    assert security_auto["depth_requested"] is None
    assert security_auto["depth_reason"] == DEPTH_REASON_AUTO_DEEP
    validate_schema(schema, security_auto)

    line_heavy = synthetic_plan([f"src/module_{index}.ts" for index in range(20)], 80, 5001, 0)
    assert line_heavy["depth_actual"] == "standard"
    assert line_heavy["depth_source"] == "default"
    assert line_heavy["depth_requested"] is None
    assert line_heavy["depth_reason"] == DEPTH_REASON_LARGE_DEFAULT
    assert line_heavy["depth_downgraded"] is False
    assert line_heavy["recommended_mode"] == "standard"
    assert line_heavy["routing_decision"]["model_profile"] == "standard"
    validate_run_plan_semantics(schema, line_heavy)

    focused = synthetic_plan([f"src/file_{index}.ts" for index in range(51)], 120, 800, 400)
    assert focused["recommended_mode"] == "focused"
    assert focused["skip_reason"] is None
    assert focused["depth_actual"] == "standard"
    assert focused["routing_decision"]["model_profile"] == "focused-fallback"
    validate_run_plan_semantics(schema, focused)

    skipped = synthetic_plan([f"src/file_{index}.ts" for index in range(101)], 240, 1500, 900)
    assert skipped["recommended_mode"] == "skip"
    assert isinstance(skipped["skip_reason"], str) and skipped["skip_reason"]
    assert skipped["depth_actual"] == "standard"
    assert skipped["routing_decision"]["model_profile"] == "focused-fallback"
    validate_run_plan_semantics(schema, skipped)

    duration_case = run_duration_template(
        focused,
        "2026-05-04T10:00:00+00:00",
        "2026-05-04T10:20:00+00:00",
    )
    assert duration_case["actual_duration_ms"] == 1200000
    assert duration_case["risk_tags"] == focused["risk_tags"]
    assert duration_case["review_loop"]["round_metrics"]["rounds_completed"] == 2
    assert duration_case["review_loop"]["round_metrics"]["halt_reason"] == "all_candidates_verified"
    assert duration_case["review_loop"]["round_metrics"]["verifier_fail_candidates"] == 1
    validate_schema(schema, duration_case)
    assert duration_case["depth_source"] == focused["depth_source"]
    assert duration_case["depth_reason"] == focused["depth_reason"]
    assert duration_case["depth_downgraded"] == focused["depth_downgraded"]
    assert duration_case["routing_decision"] == focused["routing_decision"]
    validate_run_plan_semantics(schema, duration_case)


def validate_routing_matrix(schema: dict[str, object]) -> None:
    # Section 2 of Issue #39 is authoritative: standard+deep maps to
    # model_profile=deep, and line-heavy (>5000) PRs become budget_class=large.
    cases = [
        (
            [f"src/small_{index}.ts" for index in range(5)],
            20,
            200,
            0,
            "small",
            "standard",
            "standard",
            "standard",
        ),
        (
            [f"src/line_heavy_{index}.ts" for index in range(5)],
            80,
            6000,
            0,
            "large",
            "standard",
            "standard",
            "standard",
        ),
        (
            ["src/auth/security.ts", *[f"src/security_case_{index}.ts" for index in range(29)]],
            60,
            2000,
            0,
            "medium",
            "standard",
            "standard",
            "standard",
        ),
        (
            [f"src/focused_{index}.ts" for index in range(60)],
            100,
            3000,
            0,
            "large",
            "focused",
            "standard",
            "focused-fallback",
        ),
        (
            ["db/migrations/001.sql", *[f"src/skipped_{index}.ts" for index in range(119)]],
            200,
            5000,
            3000,
            "large",
            "skip",
            "standard",
            "focused-fallback",
        ),
        (
            ["src/auth/login.ts", *[f"src/sensitive_small_{index}.ts" for index in range(7)]],
            20,
            400,
            0,
            "medium",
            "standard",
            "deep",
            "deep",
        ),
    ]
    for files, hunks, lines_added, lines_removed, budget_class, recommended_mode, depth_actual, model_profile in cases:
        plan = synthetic_plan(files, hunks, lines_added, lines_removed)
        routing_decision = plan["routing_decision"]
        assert plan["recommended_mode"] == recommended_mode, plan
        assert plan["depth_actual"] == depth_actual, plan
        assert routing_decision["budget_class"] == budget_class, plan
        assert routing_decision["route"] == ROUTE_M2, plan
        assert routing_decision["model_profile"] == model_profile, plan
        assert routing_decision["rationale"] == expected_rationale(plan), plan
        validate_run_plan_semantics(schema, plan)


def validate_risk_tag_detection() -> None:
    for path, expected_tags in RISK_TAG_CASES:
        actual = synthetic_plan([path], 1, 1, 0)
        actual_tags = set(actual["risk_tags"])
        assert actual_tags == expected_tags, (
            f"{path}: risk_tags mismatch\n"
            f"expected={sorted(expected_tags)}\n"
            f"actual={sorted(actual_tags)}"
        )


def validate_paginated_files_template() -> None:
    page_one = [{"filename": f"src/file_{index}.ts"} for index in range(100)]
    page_two = [{"filename": "src/file_100.ts"}]
    actual = run_files_list_template([page_one, page_two])
    expected = [item["filename"] for item in page_one + page_two]
    assert actual == expected, f"paginated files mismatch\nexpected={expected}\nactual={actual}"


def validate_paginated_files_pipefail() -> None:
    jq_filter = extract_jq_filter(files_list_block())
    partial_page = json.dumps([{"filename": "src/partial.ts"}])
    command = (
        "set -o pipefail && "
        f"(printf '%s\\n' {shlex.quote(partial_page)}; exit 42) | jq -sce {shlex.quote(jq_filter)}"
    )
    completed = subprocess.run(
        ["bash", "-c", command],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode == 0:
        raise AssertionError(
            "paginated files pipeline must fail when upstream exits non-zero after partial output\n"
            f"stdout={completed.stdout}\n"
            f"stderr={completed.stderr}"
        )


def validate_step5_write_order() -> None:
    text = SKILL_PATH.read_text()
    duration_index = text.index('tmp_run_plan=~/claude-loop-pr-codex/$org-$repository-$pr_number/run-plan.json.tmp')
    completed_index = text.index('{state:"completed",started_at:$started_at,finished_at:$finished_at,exit_code:0,head_sha:$head_sha,stage:"explainer",failed_stage:null}')
    if duration_index > completed_index:
        raise AssertionError("run-plan update must be documented before completed status update")

    block = duration_block()
    required_snippets = [
        'tmp_run_plan=~/claude-loop-pr-codex/$org-$repository-$pr_number/run-plan.json.tmp',
        '> "$tmp_run_plan" && test -s "$tmp_run_plan" && mv "$tmp_run_plan" ~/claude-loop-pr-codex/$org-$repository-$pr_number/run-plan.json',
        '--argjson review_loop "$review_loop_json"',
        '--argjson cost "$cost_json"',
        'cost: $cost,',
        'review_loop: ($review_loop | .round_metrics = {',
    ]
    for snippet in required_snippets:
        if snippet not in block:
            raise AssertionError(f"duration block missing required snippet: {snippet}")

    text = SKILL_PATH.read_text()
    if "tasks/extract_actual_cost.py" not in text or "pricing table による推定は行わず" not in text:
        raise AssertionError("Step 5 must extract provider-reported actual cost without pricing-table estimates")


def validate_completed_head_check_before_files() -> None:
    text = SKILL_PATH.read_text()
    head_index = text.index('gh api repos/$org/$repository/pulls/$pr_number --jq')
    head_block = extract_bash_block("gh api repos/$org/$repository/pulls/$pr_number --jq")
    forbidden_snippets = ["gh pr view", "headRefOid", "baseRefOid", ".head.repo.full_name"]
    for snippet in forbidden_snippets:
        if snippet in head_block:
            raise AssertionError(f"Step 2b metadata template must not depend on unsupported gh pr view field: {snippet}")
    required_snippets = [".base.repo.full_name", ".head.sha", ".base.sha", ".head.ref", ".base.ref", ".merge_commit_sha"]
    for snippet in required_snippets:
        if snippet not in head_block:
            raise AssertionError(f"Step 2b metadata template missing required gh api field: {snippet}")
    saved_head_section_index = text.index('#### `state == "completed"` の場合の保存済み `head_sha` 比較')
    saved_head_index = text.index("jq -r '.head_sha' ~/claude-loop-pr-codex/$org-$repository-$pr_number/metadata.json")
    files_index = text.index("gh api repos/$org/$repository/pulls/$pr_number/files --paginate")
    if not (head_index < saved_head_index < files_index):
        raise AssertionError("completed head_sha comparison must be documented before paginated files fetch")

    text_between = text[saved_head_section_index:files_index]
    required_snippets = [
        "一致するなら PR 変更ファイル一覧は取得せず",
        "異なれば追加コミットありとしてこの候補を選定し、PR 変更ファイル一覧の取得へ進む",
    ]
    for snippet in required_snippets:
        if snippet not in text_between:
            raise AssertionError(f"completed head_sha comparison docs missing required snippet: {snippet}")


def validate_step2b_jq_allowlist_docs() -> None:
    text = SKILL_PATH.read_text()
    stale_snippets = [
        "`gh api` や `gh pr view` の `--jq` フラグは使わない",
        "テンプレートはすべて `| jq` パイプ形式で統一",
    ]
    for snippet in stale_snippets:
        if snippet in text:
            raise AssertionError(f"Step 2b metadata docs still contain stale --jq prohibition: {snippet}")

    required_snippets = [
        "Step 2b の metadata 取得テンプレートだけは",
        "`gh api ... --jq '...'` を明示的に使う",
        "`gh pr view --jq` は使わない",
        "title: .title",
        "pr_url: .html_url",
        "missing title",
        "missing pr_url",
    ]
    for snippet in required_snippets:
        if snippet not in text:
            raise AssertionError(f"Step 2b metadata allowlist docs missing required snippet: {snippet}")


def validate_review_argument_docs() -> None:
    text = SKILL_PATH.read_text()
    required_snippets = [
        'argument-hint: "[<PR URL|PR number>] [--auto-send]"',
        "The user invoked this with: `$ARGUMENTS`",
        "$review_target",
        "$auto_send = true | false",
        "--auto-send",
        "重複 `--auto-send`",
        "https://github.com/<org>/<repo>/pull/<number>",
        "PR 番号",
        "depth は自動判定します",
        "複数引数",
        "unsupported argument",
        "silent ignore",
        "Step 0: 引数解析と直接指定の解決",
        "`$target_mode = \"direct\"`",
        "`depth_actual`（`standard` / `deep`）と `recommended_mode`（`standard` / `focused` / `skip`）は直交した軸",
        "{DEPTH_GUIDANCE}",
        "`depth_requested` は常に `null`",
        "`depth_downgraded` は常に `false`",
    ]
    for snippet in required_snippets:
        if snippet not in text:
            raise AssertionError(f"review argument docs missing required snippet: {snippet}")

    if "depth_actual = \"deep\"\n    else \"deep\"" in text:
        raise AssertionError("depth default must not remain deep")
    for removed in ("/pr-codex:review --deep", "/pr-codex:review --standard", 'argument-hint: "[--deep|--standard]"'):
        if removed in text:
            raise AssertionError(f"removed depth option still documented in review skill: {removed}")

    readme = (ROOT / "README.md").read_text()
    readme_snippets = [
        "## Depth control",
        "/pr-codex:review https://github.com/org/repo/pull/123",
        "/pr-codex:review https://github.com/org/repo/pull/123 --auto-send",
        "/pr-codex:review 123",
        "/pr-codex:review 123 --auto-send",
        "`depth_source=auto`",
        "`depth_requested=null`",
        "`depth_downgraded=false`",
        "`depth_source=default`, `depth_downgraded=false`, `depth_reason` に大規模ガード理由を記録",
        "`recommended_mode` (`standard` / `focused` / `skip`) は depth とは直交する別軸",
        "GitHub への自動投稿範囲は depth では拡大しない",
        "`--auto-send` でも default の投稿対象は Must Fix のみ",
    ]
    for snippet in readme_snippets:
        if snippet not in readme:
            raise AssertionError(f"README depth docs missing required snippet: {snippet}")
    for removed in ("/pr-codex:review --deep", "/pr-codex:review --standard", "`depth_source` は `argument`"):
        if removed in readme:
            raise AssertionError(f"removed depth option still documented in README: {removed}")


def validate_review_preflight_supplement_docs() -> None:
    text = SKILL_PATH.read_text()
    line = single_line_containing(text, "## 補足` に preflight 情報")
    required_terms = [
        'skip_reason != null',
        'recommended_mode != "standard"',
        'depth_actual != "standard"',
        'depth_source != "default"',
        "`changed lines > 5000`",
        'files_changed',
        'lines_added',
        'lines_removed',
        'depth_reason',
        'risk_tags',
    ]
    for term in required_terms:
        if term not in line:
            raise AssertionError(f"review preflight supplement docs missing required term: {term}")


def single_line_containing(text: str, marker: str) -> str:
    matches = [line for line in text.splitlines() if marker in line]
    if len(matches) != 1:
        raise AssertionError(f"expected exactly one line containing {marker!r}, got {len(matches)}")
    return matches[0]


def extract_escape_rule(line: str, pattern: str, name: str) -> str:
    match = re.search(pattern, line)
    if not match:
        raise AssertionError(f"{name} escape rule not found in line: {line}")
    return match.group("rule")


def validate_escape_rule_docs() -> None:
    text = SKILL_PATH.read_text()
    criteria_line = single_line_containing(
        text,
        "4a / 4b の Bash コマンド文字列中の `{REVIEW_CRITERIA}`",
    )
    if "バッククォート (`) を" in criteria_line:
        raise AssertionError("{REVIEW_CRITERIA} preprocessing must not keep the old backtick-only escape rule")
    if ESCAPE_RULE_TEXT not in criteria_line or "共通のエスケープ規則" not in criteria_line:
        raise AssertionError("{REVIEW_CRITERIA} preprocessing must reference the common 4-character escape rule")

    preprocessing_line = single_line_containing(
        text,
        "{REVIEW_CRITERIA}` / `{RUN_PLAN_GUIDANCE}` / `{DEPTH_GUIDANCE}` / `{BEAR_REVIEW_GUIDANCE}` を bash double-quote 内へ差し込む前",
    )
    constraint_line = single_line_containing(
        text,
        "10. Step 4a / 4b のプロンプト中に含まれる `{REVIEW_CRITERIA}`",
    )
    preprocessing_rule = extract_escape_rule(
        preprocessing_line,
        r"4つとも (?P<rule>.+?) の順でエスケープする",
        "Step 4 preprocessing",
    )
    constraint_rule = extract_escape_rule(
        constraint_line,
        r"差し込み前に \*\*(?P<rule>.+?)\*\* の順でエスケープする",
        "allowlist rule #10",
    )
    if preprocessing_rule != ESCAPE_RULE_TEXT:
        raise AssertionError(f"Step 4 preprocessing escape rule mismatch: {preprocessing_rule}")
    if constraint_rule != ESCAPE_RULE_TEXT:
        raise AssertionError(f"allowlist rule #10 escape rule mismatch: {constraint_rule}")
    if preprocessing_rule != constraint_rule:
        raise AssertionError("Step 4 preprocessing and allowlist rule #10 escape rules must match")


def validate_schema_contract(schema: dict[str, object]) -> None:
    items = schema["properties"]["risk_tags"]["items"]
    assert items["enum"] == RISK_TAG_ENUM, f"risk_tags enum mismatch: {items['enum']}"

    review_loop_schema = schema["properties"].get("review_loop")
    assert isinstance(review_loop_schema, dict), "run-plan schema must include review_loop"
    halting_policy = review_loop_schema["properties"]["halting_policy"]
    for key in ("max_rounds", "time_budget_ms", "no_new_evidence_rounds", "repeated_contradiction_limit"):
        assert key in halting_policy["properties"], f"halting_policy missing {key}"
    round_metrics = review_loop_schema["properties"]["round_metrics"]
    for key in ("rounds_completed", "halt_reason", "verifier_fail_candidates", "repeated_contradiction_events"):
        assert key in round_metrics["properties"], f"round_metrics missing {key}"
    assert schema["properties"]["depth_actual"]["enum"] == ["deep", "standard"]
    assert schema["properties"]["depth_source"]["enum"] == ["auto", "default"]
    assert schema["properties"]["depth_requested"]["const"] is None
    assert schema["properties"]["depth_downgraded"]["const"] is False
    assert schema["properties"]["depth_downgrade_reason"]["const"] is None

    cost_schema = schema["properties"].get("cost")
    assert isinstance(cost_schema, dict), "run-plan schema must include cost"
    assert cost_schema["additionalProperties"] is False
    assert cost_schema["properties"]["source"]["enum"] == ["provider_reported", "unavailable"]
    assert cost_schema["properties"]["currency"]["const"] == "USD"

    skip_plan = synthetic_plan([f"src/file_{index}.ts" for index in range(101)], 1, 1, 0)
    validate_run_plan_semantics(schema, skip_plan)

    missing_skip_reason = dict(skip_plan, skip_reason=None)
    if schema_matches(schema, missing_skip_reason):
        raise AssertionError("schema must reject skip recommended_mode with null skip_reason")

    focused_with_reason = dict(
        synthetic_plan([f"src/file_{index}.ts" for index in range(51)], 1, 1, 0),
        skip_reason="unexpected",
    )
    if schema_matches(schema, focused_with_reason):
        raise AssertionError("schema must reject non-skip recommended_mode with non-null skip_reason")

    missing_routing = dict(skip_plan)
    del missing_routing["routing_decision"]
    if schema_matches(schema, missing_routing):
        raise AssertionError("schema must reject missing routing_decision")

    routing_schema = schema["properties"]["routing_decision"]
    routing_decision = dict(skip_plan["routing_decision"])
    extra_routing = dict(skip_plan, routing_decision=dict(routing_decision, provider="private-model"))
    if schema_matches(schema, extra_routing):
        raise AssertionError("schema must reject extra routing_decision properties")

    invalid_route = dict(skip_plan, routing_decision=dict(routing_decision, route="claude+codex+specialist"))
    if schema_matches(schema, invalid_route):
        raise AssertionError("schema must reject M2 route enum violations")

    invalid_profile = dict(skip_plan, routing_decision=dict(routing_decision, model_profile="gpt-5.5"))
    if schema_matches(schema, invalid_profile):
        raise AssertionError("schema must reject provider/model-like model_profile enum violations")

    invalid_rationale = dict(skip_plan, routing_decision=dict(routing_decision, rationale="x" * 241))
    if schema_matches(schema, invalid_rationale):
        raise AssertionError("schema must reject overlong routing rationale")

    mismatched_profile = dict(skip_plan, routing_decision=dict(routing_decision, model_profile="standard"))
    try:
        validate_run_plan_semantics(schema, mismatched_profile)
    except AssertionError as exc:
        if "$.routing_decision.model_profile" not in str(exc):
            raise
    else:
        raise AssertionError("validator must reject model_profile inconsistent with recommended_mode/depth_actual")

    assert routing_schema["properties"]["route"]["enum"] == [ROUTE_M2]
    assert routing_schema["properties"]["model_profile"]["enum"] == ["standard", "deep", "focused-fallback"]

    default_with_request = dict(synthetic_plan(["src/module.ts"], 1, 1, 0), depth_requested="standard")
    if schema_matches(schema, default_with_request):
        raise AssertionError("schema must reject any non-null depth_requested")

    default_with_downgrade = dict(synthetic_plan(["src/module.ts"], 1, 1, 0), depth_downgraded=True)
    if schema_matches(schema, default_with_downgrade):
        raise AssertionError("schema must reject depth_downgraded=true because depth options were removed")

    default_with_deep_actual = dict(synthetic_plan(["src/module.ts"], 1, 1, 0), depth_actual="deep")
    if schema_matches(schema, default_with_deep_actual):
        raise AssertionError("schema must reject depth_source=default with depth_actual=deep")

    auto_with_standard_actual = dict(synthetic_plan(["src/auth/login.go"], 1, 1, 0), depth_actual="standard")
    if schema_matches(schema, auto_with_standard_actual):
        raise AssertionError("schema must reject depth_source=auto with depth_actual=standard")

    not_downgraded_with_reason = dict(
        synthetic_plan(["src/module.ts"], 1, 1, 0),
        depth_downgrade_reason="unexpected",
    )
    if schema_matches(schema, not_downgraded_with_reason):
        raise AssertionError("schema must reject depth_downgrade_reason when not downgraded")


def main() -> None:
    schema = load_json(SCHEMA_PATH)
    validate_schema_contract(schema)
    validate_paginated_files_template()
    validate_paginated_files_pipefail()
    for fixture in ("small", "medium", "large"):
        validate_fixture(fixture, schema)
    validate_threshold_behavior(schema)
    validate_routing_matrix(schema)
    validate_risk_tag_detection()
    validate_completed_head_check_before_files()
    validate_step2b_jq_allowlist_docs()
    validate_review_argument_docs()
    validate_review_preflight_supplement_docs()
    validate_step5_write_order()
    validate_escape_rule_docs()
    print("run-plan validation passed")


if __name__ == "__main__":
    main()
