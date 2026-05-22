#!/usr/bin/env python3
"""Regression checks for Issue #70/#71 plugin-root and SARIF setup robustness."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
REVIEW_SKILL = ROOT / "skills" / "review" / "SKILL.md"
SEND_SKILL = ROOT / "skills" / "send" / "SKILL.md"
SCHEMA_PATH = ROOT / "schemas" / "sarif-2.1.0.json"

import sys
sys.path.insert(0, str(ROOT / "tasks"))

from generate_findings_sarif import build_sarif  # noqa: E402
from test_findings_sarif import canonical_artifact, metadata  # noqa: E402
import validate_findings_sarif as sarif_validator  # noqa: E402


class Issue70PluginRootSetupTest(unittest.TestCase):
    def test_skill_templates_define_plugin_root_fallback_before_tool_invocations(self) -> None:
        for path in (REVIEW_SKILL, SEND_SKILL):
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path):
                self.assertIn('plugin_root="${CLAUDE_PLUGIN_ROOT:-', text)
                self.assertIn('CLAUDE_CODE_PLUGIN_CACHE_DIR', text)
                self.assertIn('CLAUDE_CONFIG_DIR', text)
                self.assertIn('key=lambda p: (p.stat().st_mtime_ns, str(p))', text)
                self.assertIn('python3 "$plugin_root/tasks/', text)
                self.assertNotIn("Path.home().glob('.claude/plugins/cache/**/pr-codex/tasks/validate_findings.py')", text)
                self.assertNotIn('python3 $CLAUDE_PLUGIN_ROOT/tasks/', text)
                self.assertNotIn('python3 ${CLAUDE_PLUGIN_ROOT}/tasks/', text)
                self.assertNotIn('echo "$CLAUDE_PLUGIN_ROOT"', text)
                self.assertNotIn('${CLAUDE_PLUGIN_ROOT}` の位置を実値に置換', text)
                self.assertNotIn('validator_path` / `schema_path` の実値へ置換', text)

                in_bash = False
                block = []
                start_line = 0
                for line_number, line in enumerate(text.splitlines(), start=1):
                    if line.strip() == '```bash':
                        in_bash = True
                        block = []
                        start_line = line_number
                        continue
                    if in_bash and line.strip() == '```':
                        block_text = "\n".join(block)
                        if '$plugin_root' in block_text:
                            self.assertIn(
                                'plugin_root="${CLAUDE_PLUGIN_ROOT:-',
                                block_text,
                                f"{path}:{start_line} uses $plugin_root without recomputing it",
                            )
                        in_bash = False
                        continue
                    if in_bash:
                        block.append(line)

    def test_readme_documents_no_manual_absolute_path_replacement_when_env_missing(self) -> None:
        text = README.read_text(encoding="utf-8")
        for snippet in (
            "CLAUDE_PLUGIN_ROOT が未設定",
            "plugin_root=\"${CLAUDE_PLUGIN_ROOT:-",
            "手動で絶対パスに置換しない",
            "pr-codex/tasks/validate_findings.py",
            "CLAUDE_CODE_PLUGIN_CACHE_DIR",
            "CLAUDE_CONFIG_DIR",
        ):
            self.assertIn(snippet, text)


class Issue71SarifJsonschemaSetupTest(unittest.TestCase):
    def test_sarif_validator_falls_back_to_builtin_shape_checks_without_jsonschema(self) -> None:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        findings = canonical_artifact()
        sarif = build_sarif(findings, metadata=metadata(), ranges={"src/App.php": [(1, 20)]})

        with mock.patch.object(sarif_validator, "jsonschema", None):
            errors = sarif_validator.validate_findings_sarif(schema, sarif, findings=findings)

        self.assertEqual(errors, [])

    def test_sarif_validator_builtin_shape_checks_cover_emitted_sarif_fields_without_jsonschema(self) -> None:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        findings = canonical_artifact()
        sarif = build_sarif(findings, metadata=metadata(), ranges={"src/App.php": [(1, 20)]})
        sarif["runs"][0]["tool"]["driver"]["informationUri"] = 123
        sarif["runs"][0]["automationDetails"]["id"] = 456

        with mock.patch.object(sarif_validator, "jsonschema", None):
            errors = sarif_validator.validate_findings_sarif(schema, sarif, findings=findings)

        self.assertTrue(any("tool.driver.informationUri" in error for error in errors), errors)
        self.assertTrue(any("automationDetails.id" in error for error in errors), errors)

    def test_readme_documents_pep668_safe_jsonschema_install_options(self) -> None:
        text = README.read_text(encoding="utf-8")
        for snippet in (
            "jsonschema>=4,<5",
            "python3 -m venv",
            "--break-system-packages",
            "PEP 668",
        ):
            self.assertIn(snippet, text)


if __name__ == "__main__":
    unittest.main()
