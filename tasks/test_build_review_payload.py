#!/usr/bin/env python3
"""Regression tests for the stdlib-only review payload builder."""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BUILDER_PATH = ROOT / "tasks" / "build_review_payload.py"
HEAD_SHA = "e8763f5edddeca5be7334ac9131066be09f19a6d"
BASE_SHA = "2499605587c910c1911729e90d4c96b61210c628"


def finding(
    identifier: str,
    severity: str = "must_fix",
    *,
    path: str = "src/App.py",
    start_line: int = 10,
    end_line: int | None = None,
    side: str = "RIGHT",
    post_policy: str | None = None,
    explanation_postable: bool = True,
    category: str = "bug",
) -> dict[str, Any]:
    location: dict[str, Any] = {"path": path, "start_line": start_line, "side": side}
    if end_line is not None:
        location["end_line"] = end_line
    if post_policy is None:
        post_policy = "inline" if severity == "must_fix" else "body_summary"
    return {
        "id": identifier,
        "fingerprint": identifier,
        "severity": severity,
        "category": category,
        "title": f"Finding {identifier}",
        "problem": f"problem {identifier}",
        "reason": f"reason {identifier}",
        "suggestion": f"suggestion {identifier}",
        "location": location,
        "posting": {
            "post_policy": post_policy,
            "explanation_postable": explanation_postable,
        },
    }


def artifact(findings: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": "findings.v1",
        "pr": {
            "repository": "acme/widgets",
            "number": 42,
            "head_sha": HEAD_SHA,
            "base_sha": BASE_SHA,
        },
        "findings": findings,
    }


def metadata() -> dict[str, Any]:
    return {
        "org": "acme",
        "repository": "widgets",
        "repository_full_name": "acme/widgets",
        "pr_number": 42,
        "pr_url": "https://github.com/acme/widgets/pull/42",
        "head_sha": HEAD_SHA,
        "base_sha": BASE_SHA,
        "branch": "feature",
        "base_branch": "main",
        "merge_commit_sha": None,
        "title": "Review payload",
        "files": ["src/App.py", "src/Other.py"],
    }


def review_for(findings: list[dict[str, Any]], *, summary: str = "確認済みの総評です。", good_points: str = "変更は局所的です。") -> str:
    headings = "\n\n".join(
        f"### `{item['location']['path']}:L{item['location']['start_line']}`"
        for item in findings
        if item.get("severity") == "must_fix"
    )
    good = f"\n\n## 良い点\n\n{good_points}" if good_points else ""
    return (
        f"## 総評\n\n{summary}\n\n"
        f"## 重大な問題 (Must Fix)\n\n{headings or 'なし'}"
        f"{good}\n\n## 補足\n\nなし\n"
    )


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


class BuildReviewPayloadTest(unittest.TestCase):
    def run_build(
        self,
        directory: Path,
        findings_data: dict[str, Any],
        *,
        metadata_data: dict[str, Any] | None = None,
        review_text: str | None = None,
        ranges_text: str = "src/App.py\tL1-L100\nsrc/Other.py\tL1-L100\n",
        flags: tuple[str, ...] = (),
        ci_status: dict[str, Any] | str | None = None,
        ci_summary: str | None = None,
        run_plan: dict[str, Any] | None = None,
        with_diff: bool = True,
        diff_text: str = "diff --git a/src/App.py b/src/App.py\n",
        with_sarif: bool = False,
    ) -> tuple[subprocess.CompletedProcess[str], Path, Path, dict[str, Path]]:
        findings_path = directory / "findings.verified.json"
        review_path = directory / "review.md"
        metadata_path = directory / "metadata.json"
        ranges_path = directory / "pr.diff.ranges.txt"
        diff_path = directory / "pr.diff"
        payload_path = directory / "review-payload.json"
        manifest_path = directory / "payload-manifest.json"
        write_json(findings_path, findings_data)
        review_path.write_text(review_text if review_text is not None else review_for(findings_data.get("findings", [])), encoding="utf-8")
        write_json(metadata_path, metadata_data if metadata_data is not None else metadata())
        ranges_path.write_text(ranges_text, encoding="utf-8")
        command = [
            sys.executable,
            str(BUILDER_PATH),
            "--findings",
            str(findings_path),
            "--review",
            str(review_path),
            "--metadata",
            str(metadata_path),
            "--ranges",
            str(ranges_path),
            "--output",
            str(payload_path),
            "--manifest",
            str(manifest_path),
        ]
        if with_diff:
            diff_path.write_text(diff_text, encoding="utf-8")
            command.extend(["--diff", str(diff_path)])
        ci_status_path = directory / "ci-status.json"
        if ci_status is not None:
            if isinstance(ci_status, str):
                ci_status_path.write_text(ci_status, encoding="utf-8")
            else:
                write_json(ci_status_path, ci_status)
            command.extend(["--ci-status", str(ci_status_path)])
        ci_summary_path = directory / "ci-summary.md"
        if ci_summary is not None:
            ci_summary_path.write_text(ci_summary, encoding="utf-8")
            command.extend(["--ci-summary", str(ci_summary_path)])
        run_plan_path = directory / "run-plan.json"
        if run_plan is not None:
            write_json(run_plan_path, run_plan)
            command.extend(["--run-plan", str(run_plan_path)])
        sarif_path = directory / "findings.sarif"
        if with_sarif:
            write_json(sarif_path, {"version": "2.1.0"})
            command.extend(["--sarif", str(sarif_path)])
        command.extend(flags)
        completed = subprocess.run(command, check=False, capture_output=True, text=True)
        paths = {
            "findings": findings_path,
            "review": review_path,
            "metadata": metadata_path,
            "ranges": ranges_path,
            "diff": diff_path,
            "ci_status": ci_status_path,
            "sarif": sarif_path,
            "ci_summary": ci_summary_path,
            "run_plan": run_plan_path,
        }
        return completed, payload_path, manifest_path, paths

    def assert_invalid(self, completed: subprocess.CompletedProcess[str], fragment: str) -> None:
        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertIn("INVALID review payload inputs", completed.stderr)
        self.assertIn(fragment, completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)
    def run_verify(self, manifest_path: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(BUILDER_PATH), "--verify", "--manifest", str(manifest_path)],
            check=False,
            capture_output=True,
            text=True,
        )


    def test_builds_request_changes_payload_and_comment_map(self) -> None:
        item = finding("must-1", start_line=10, end_line=12)
        with tempfile.TemporaryDirectory() as tmp:
            completed, payload_path, manifest_path, _ = self.run_build(Path(tmp), artifact([item]))
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(payload_path.read_text(encoding="utf-8"))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["commit_id"], HEAD_SHA)
        self.assertEqual(payload["event"], "REQUEST_CHANGES")
        self.assertEqual(
            payload["comments"],
            [
                {
                    "path": "src/App.py",
                    "line": 12,
                    "side": "RIGHT",
                    "body": "🚨 **Must Fix**\n\n- 問題: problem must-1\n- 理由: reason must-1\n- 提案: suggestion must-1",
                    "start_line": 10,
                    "start_side": "RIGHT",
                }
            ],
        )
        self.assertEqual(manifest["event"], "REQUEST_CHANGES")
        self.assertEqual(manifest["comment_map"], [{"comment_index": 0, "finding_id": "must-1", "severity": "must_fix"}])
        self.assertEqual(
            manifest["counts"],
            {
                "must_fix_total": 1,
                "must_fix_inline": 1,
                "must_fix_body": 0,
                "must_fix_withheld": 0,
                "should_fix_inline": 0,
                "nit_inline": 0,
            },
        )
        self.assertEqual(manifest["withheld"], [])
        self.assertEqual(manifest["semantic_targets"], ["must-1"])
        self.assertEqual(manifest["flags"], {"include_should_fix": False, "include_nit": False})
        self.assertIn("built payload: event=REQUEST_CHANGES comments=1 (must_fix=1 should_fix=0 nit=0) out_of_range=0", completed.stdout)

    def test_builds_approve_body_with_deterministic_review_scope(self) -> None:
        plan = {"depth_actual": "deep", "recommended_mode": "focused", "risk_tags": ["security", "python"]}
        with tempfile.TemporaryDirectory() as tmp:
            completed, payload_path, _, _ = self.run_build(Path(tmp), artifact([]), run_plan=plan)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(payload_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["event"], "APPROVE")
        self.assertEqual(payload["comments"], [])
        body = payload["body"]
        self.assertIn("Must Fix はありません。承認します。", body)
        self.assertIn("- 変更ファイル: src/App.py, src/Other.py", body)
        self.assertIn("depth_actual=deep", body)
        self.assertIn("recommended_mode=focused", body)
        self.assertIn("risk_tags=security, python", body)
        self.assertIn("review.md Must Fix 件数=0", body)
        self.assertIn("- CI 状態: 未取得", body)

    def test_ci_failure_suppresses_approve_to_comment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            completed, payload_path, _, _ = self.run_build(
                Path(tmp), artifact([]), ci_status={"state": "failure"}, ci_summary="unit tests failed\nfull details"
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(payload_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["event"], "COMMENT")
        self.assertIn("Must Fix はありませんが、CI が failure のため承認を保留します。", payload["body"])
        self.assertNotIn("## 確認した範囲", payload["body"])
        self.assertIn("## CI 状態\n\n- 状態: failure\n- 要約: unit tests failed", payload["body"])

    def test_must_fix_out_of_range_is_moved_to_body(self) -> None:
        item = finding("far-away", start_line=120)
        with tempfile.TemporaryDirectory() as tmp:
            completed, payload_path, manifest_path, _ = self.run_build(Path(tmp), artifact([item]))
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(payload_path.read_text(encoding="utf-8"))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["event"], "REQUEST_CHANGES")
        self.assertEqual(payload["comments"], [])
        self.assertIn(
            "Must Fix を検出しました。マージ前に修正が必要です。行コメント不可のため本文末尾に記載しています。",
            payload["body"],
        )
        self.assertIn("### `src/App.py:L120`", payload["body"])
        self.assertIn("- 問題: problem far-away", payload["body"])
        self.assertEqual(manifest["out_of_range"], [{"finding_id": "far-away", "kind": "Must Fix", "reason": "diff 範囲外"}])
        self.assertEqual(manifest["counts"]["must_fix_body"], 1)
    def test_path_absent_from_metadata_files_is_moved_to_body(self) -> None:
        item = finding("not-in-metadata", path="src/Hidden.py")
        with tempfile.TemporaryDirectory() as tmp:
            completed, payload_path, manifest_path, _ = self.run_build(
                Path(tmp),
                artifact([item]),
                ranges_text="src/Hidden.py\tL1-L100\n",
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(payload_path.read_text(encoding="utf-8"))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["comments"], [])
        self.assertIn("problem not-in-metadata", payload["body"])
        self.assertEqual(
            manifest["out_of_range"],
            [{"finding_id": "not-in-metadata", "kind": "Must Fix", "reason": "diff 範囲外"}],
        )
    def test_missing_or_non_list_metadata_files_invalidates_inline_candidates(self) -> None:
        item = finding("invalid-files")
        for files_value in (None, {"src/App.py": True}):
            with self.subTest(files=files_value), tempfile.TemporaryDirectory() as tmp:
                metadata_data = metadata()
                if files_value is None:
                    del metadata_data["files"]
                else:
                    metadata_data["files"] = files_value
                completed, payload_path, manifest_path, _ = self.run_build(
                    Path(tmp),
                    artifact([item]),
                    metadata_data=metadata_data,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                payload = json.loads(payload_path.read_text(encoding="utf-8"))
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

                self.assertEqual(payload["comments"], [])
                self.assertEqual(
                    manifest["out_of_range"],
                    [{"finding_id": "invalid-files", "kind": "Must Fix", "reason": "diff 範囲外"}],
                )



    def test_left_side_opted_in_finding_is_moved_to_body(self) -> None:
        item = finding("left-should", "should_fix", side="LEFT")
        with tempfile.TemporaryDirectory() as tmp:
            completed, payload_path, manifest_path, _ = self.run_build(
                Path(tmp), artifact([item]), flags=("--include-should-fix",)
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(payload_path.read_text(encoding="utf-8"))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["comments"], [])
        self.assertIn("problem left-should", payload["body"])
        self.assertEqual(manifest["out_of_range"], [{"finding_id": "left-should", "kind": "Should Fix", "reason": "LEFT-side 非対応"}])

    def test_missing_diff_makes_every_candidate_out_of_range(self) -> None:
        item = finding("no-diff")
        with tempfile.TemporaryDirectory() as tmp:
            completed, payload_path, _, _ = self.run_build(Path(tmp), artifact([item]), with_diff=False)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(payload_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["comments"], [])
        self.assertIn("problem no-diff", payload["body"])

    def test_empty_diff_makes_every_candidate_out_of_range(self) -> None:
        item = finding("empty-diff")
        with tempfile.TemporaryDirectory() as tmp:
            completed, payload_path, _, _ = self.run_build(Path(tmp), artifact([item]), diff_text="")
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(payload_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["comments"], [])
        self.assertIn("problem empty-diff", payload["body"])

    def test_security_finding_uses_only_public_safe_summary_in_body(self) -> None:
        item = finding("security-1", category="security", post_policy="body_summary")
        item["problem"] = "exploit with curl https://internal"
        item["reason"] = "secret=raw-token"
        item["suggestion"] = "send raw payload"
        item["security"] = {
            "severity": "critical",
            "confidence": "high",
            "exploitability": "proven with curl and secret=raw-token",
            "public_safe_summary": "A sensitive security issue requires private remediation.",
            "disclosure_policy": "body_summary_safe",
        }
        with tempfile.TemporaryDirectory() as tmp:
            completed, payload_path, manifest_path, _ = self.run_build(Path(tmp), artifact([item]))
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(payload_path.read_text(encoding="utf-8"))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        body = payload["body"]
        self.assertIn("A sensitive security issue requires private remediation.", body)
        for private_fragment in ("curl", "internal", "secret=", "raw payload", "exploitability"):
            self.assertNotIn(private_fragment, body)
        self.assertEqual(manifest["out_of_range"], [{"finding_id": "security-1", "kind": "Must Fix", "reason": "security disclosure policy"}])
    def test_local_only_or_suppressed_security_must_fix_is_withheld(self) -> None:
        cases = (
            ("local-only", "local_only", "local_only", "local_only"),
            ("suppressed", "suppress", "body_summary_safe", "suppress"),
        )
        for identifier, post_policy, disclosure_policy, expected_reason in cases:
            with self.subTest(post_policy=post_policy), tempfile.TemporaryDirectory() as tmp:
                item = finding(identifier, category="security", post_policy=post_policy)
                item["problem"] = f"private problem {identifier}"
                item["security"] = {
                    "severity": "high",
                    "confidence": "high",
                    "exploitability": f"private exploit {identifier}",
                    "public_safe_summary": f"public summary {identifier}",
                    "disclosure_policy": disclosure_policy,
                }
                completed, payload_path, manifest_path, _ = self.run_build(Path(tmp), artifact([item]))
                self.assertEqual(completed.returncode, 0, completed.stderr)
                payload = json.loads(payload_path.read_text(encoding="utf-8"))
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

                self.assertEqual(payload["event"], "REQUEST_CHANGES")
                self.assertEqual(payload["comments"], [])
                self.assertIn("このレビューは変更をリクエストします。", payload["body"])
                self.assertNotIn("Must Fix", payload["body"])
                self.assertNotIn("セキュリティ", payload["body"])
                self.assertNotIn(f"private problem {identifier}", payload["body"])
                self.assertNotIn(f"public summary {identifier}", payload["body"])
                self.assertEqual(manifest["out_of_range"], [])
                self.assertEqual(
                    manifest["withheld"],
                    [{"finding_id": identifier, "kind": "Must Fix", "reason": expected_reason}],
                )
                self.assertEqual(manifest["semantic_targets"], [identifier])
                self.assertEqual(manifest["counts"]["must_fix_withheld"], 1)


    def test_cluster_posts_only_representative_and_summarizes_five_members(self) -> None:
        representative = finding("rep", path="src/rep.py", start_line=5)
        members = [finding(f"member-{index}", path=f"src/member{index}.py", start_line=10 + index) for index in range(1, 7)]
        all_findings = [representative, *members]
        for item in all_findings:
            item["root_cause_id"] = "cluster-1"
        data = artifact(all_findings)
        data["root_cause_clusters"] = [
            {
                "id": "cluster-1",
                "summary": "same root cause",
                "representative_finding_id": "rep",
                "finding_ids": [item["id"] for item in all_findings],
            }
        ]
        ranges = "\n".join(f"{item['location']['path']}\tL1-L100" for item in all_findings) + "\n"
        cluster_metadata = metadata()
        cluster_metadata["files"] = [item["location"]["path"] for item in all_findings]
        with tempfile.TemporaryDirectory() as tmp:
            completed, payload_path, manifest_path, _ = self.run_build(
                Path(tmp), data, metadata_data=cluster_metadata, ranges_text=ranges
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(payload_path.read_text(encoding="utf-8"))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(len(payload["comments"]), 1)
        body = payload["comments"][0]["body"]
        self.assertIn("同一 root cause の影響箇所:", body)
        for index in range(1, 6):
            self.assertIn(f"- `src/member{index}.py:L{10 + index}` problem member-{index}", body)
        self.assertNotIn("src/member6.py", body)
        self.assertIn("- 他 1 件", body)
        self.assertEqual(manifest["counts"]["must_fix_total"], 7)
        self.assertEqual(manifest["counts"]["must_fix_inline"], 1)
        self.assertEqual(manifest["semantic_targets"], ["rep", *(f"member-{index}" for index in range(1, 7))])
        self.assertIn("Must Fix を検出しました。マージ前に修正が必要です。", payload["body"])
        self.assertNotIn("Must Fix 1件", payload["body"])
        self.assertNotIn("Must Fix 7件", payload["body"])


    def test_cluster_summary_filters_private_unpostable_and_inactive_members(self) -> None:
        representative = finding("rep")
        private_member = finding("private-member", category="security", post_policy="local_only", start_line=20)
        private_member["problem"] = "do not publish private member"
        private_member["security"] = {
            "severity": "high",
            "confidence": "high",
            "exploitability": "private exploit details",
            "public_safe_summary": "private public-safe summary",
            "disclosure_policy": "local_only",
        }
        unpostable_member = finding(
            "unpostable-member",
            "should_fix",
            start_line=30,
            explanation_postable=False,
        )
        excluded_member = finding("excluded-member", "nit", start_line=40)
        visible_member = finding("visible-member", start_line=50)
        all_findings = [
            representative,
            private_member,
            unpostable_member,
            excluded_member,
            visible_member,
        ]
        data = artifact(all_findings)
        data["root_cause_clusters"] = [
            {
                "id": "cluster-filtered",
                "summary": "same root cause",
                "representative_finding_id": "rep",
                "finding_ids": [item["id"] for item in all_findings],
            }
        ]

        with tempfile.TemporaryDirectory() as tmp:
            completed, payload_path, manifest_path, _ = self.run_build(
                Path(tmp),
                data,
                flags=("--include-should-fix",),
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(payload_path.read_text(encoding="utf-8"))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        body = payload["comments"][0]["body"]
        self.assertIn("problem visible-member", body)
        for private_fragment in (
            "do not publish private member",
            "private public-safe summary",
            "problem unpostable-member",
            "problem excluded-member",
        ):
            self.assertNotIn(private_fragment, body)
        self.assertIn("- 他 3 件", body)
        self.assertEqual(manifest["withheld"], [])
        self.assertEqual(manifest["semantic_targets"], ["rep", "private-member", "visible-member"])

    def test_include_flags_are_independent_and_comments_are_severity_grouped(self) -> None:
        items = [
            finding("nit-1", "nit", start_line=30),
            finding("must-1", start_line=10),
            finding("should-1", "should_fix", start_line=20),
            finding("ignored", "should_fix", start_line=40, post_policy="local_only"),
        ]
        cases = {
            (): ["must-1"],
            ("--include-should-fix",): ["must-1", "should-1"],
            ("--include-nit",): ["must-1", "nit-1"],
            ("--include-should-fix", "--include-nit"): ["must-1", "should-1", "nit-1"],
        }
        for flags, expected_ids in cases.items():
            with self.subTest(flags=flags), tempfile.TemporaryDirectory() as tmp:
                completed, payload_path, manifest_path, _ = self.run_build(Path(tmp), artifact(items), flags=flags)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                payload = json.loads(payload_path.read_text(encoding="utf-8"))
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                self.assertEqual([entry["finding_id"] for entry in manifest["comment_map"]], expected_ids)
                self.assertEqual(len(payload["comments"]), len(expected_ids))
        should_body = payload["comments"][1]["body"]
        nit_body = payload["comments"][2]["body"]
        self.assertTrue(should_body.startswith("🛠 **Should Fix** `src/App.py:L20`"))
        self.assertTrue(nit_body.startswith("💡 **Nit** `src/App.py:L30`"))

    def test_builder_guards_reject_contract_violations(self) -> None:
        base = artifact([finding("must-1")])
        cases: list[tuple[str, dict[str, Any], dict[str, Any], str | None, str]] = []

        wrong_schema = copy.deepcopy(base)
        wrong_schema["schema_version"] = "findings.v2"
        cases.append(("schema", wrong_schema, metadata(), None, "schema_version"))

        wrong_array = copy.deepcopy(base)
        wrong_array["findings"] = {}
        cases.append(("findings-array", wrong_array, metadata(), "## 総評\n\nok\n", "findings: must be an array"))

        wrong_metadata = metadata()
        wrong_metadata["repository_full_name"] = "other/widgets"
        cases.append(("metadata-context", copy.deepcopy(base), wrong_metadata, None, "metadata.repository_full_name"))

        non_must_inline = artifact([finding("should", "should_fix", post_policy="inline")])
        cases.append(("non-must-inline", non_must_inline, metadata(), None, "only must_fix findings may use post_policy=inline"))

        must_body = artifact([finding("must-body", post_policy="body_summary")])
        cases.append(("must-post-policy", must_body, metadata(), None, "must use post_policy=inline"))
        for post_policy in ("local_only", "suppress"):
            non_security_private = artifact(
                [finding(f"must-{post_policy}", post_policy=post_policy)]
            )
            cases.append(
                (
                    f"non-security-{post_policy}",
                    non_security_private,
                    metadata(),
                    None,
                    "must use post_policy=inline",
                )
            )


        must_private = artifact([finding("must-private", explanation_postable=False)])
        cases.append(("must-postable", must_private, metadata(), None, "explanation_postable=true"))

        must_left = artifact([finding("must-left", side="LEFT")])
        cases.append(("must-side", must_left, metadata(), None, "location.side=RIGHT"))

        security_missing = artifact([finding("security-missing", category="security")])
        cases.append(("security-extension", security_missing, metadata(), None, "security extension is required"))

        security_inline_item = finding("security-inline", category="security")
        security_inline_item["security"] = {
            "severity": "high",
            "confidence": "high",
            "exploitability": "likely",
            "public_safe_summary": "Sensitive issue.",
            "disclosure_policy": "body_summary_safe",
        }
        cases.append(("security-inline", artifact([security_inline_item]), metadata(), None, "high-risk security findings must not use post_policy=inline"))
        disclosure_local_inline = copy.deepcopy(security_inline_item)
        disclosure_local_inline["id"] = "security-local-inline"
        disclosure_local_inline["fingerprint"] = "security-local-inline"
        disclosure_local_inline["security"]["disclosure_policy"] = "local_only"
        cases.append(
            (
                "security-local-inline",
                artifact([disclosure_local_inline]),
                metadata(),
                None,
                "high-risk security findings must not use post_policy=inline",
            )
        )


        count_mismatch = artifact([finding("count")])
        cases.append(("count", count_mismatch, metadata(), "## 総評\n\nok\n\n## 重大な問題 (Must Fix)\n\nなし\n", "Must Fix heading count"))

        empty_summary = artifact([])
        cases.append(("summary", empty_summary, metadata(), "## 総評\n\n\n## 重大な問題 (Must Fix)\n\nなし\n", "summary is empty"))

        for name, data, meta, review_text, fragment in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                completed, _, _, _ = self.run_build(Path(tmp), data, metadata_data=meta, review_text=review_text)
                self.assert_invalid(completed, fragment)

    def test_multiline_must_fix_must_fit_inside_one_hunk(self) -> None:
        item = finding("cross-hunk", start_line=10, end_line=20)
        with tempfile.TemporaryDirectory() as tmp:
            completed, payload_path, _, _ = self.run_build(
                Path(tmp), artifact([item]), ranges_text="src/App.py\tL1-L10\nsrc/App.py\tL20-L30\n"
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(payload_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["comments"], [])
        self.assertIn("### `src/App.py:L10-L20`", payload["body"])

    def test_body_sections_follow_required_order(self) -> None:
        item = finding("should-out", "should_fix", start_line=200)
        with tempfile.TemporaryDirectory() as tmp:
            completed, payload_path, _, _ = self.run_build(
                Path(tmp), artifact([item]), flags=("--include-should-fix",), ci_status={"state": "success"}
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            body = json.loads(payload_path.read_text(encoding="utf-8"))["body"]
        offsets = [
            body.index("Must Fix はありません。承認します。"),
            body.index("## 良い点"),
            body.index("## 確認した範囲"),
            body.index("## 行コメント不可 (diff 範囲外)"),
        ]
        self.assertEqual(offsets, sorted(offsets))

    def test_posted_summary_mentions_only_posted_severities(self) -> None:
        items = [
            finding("must-1", start_line=10),
            finding("should-1", "should_fix", start_line=20),
            finding("nit-1", "nit", start_line=30),
        ]
        leaky_review = review_for(
            items,
            summary="全体的に良好です。Should Fix が1件と Nit が1件あります。`internal_token` の漏えいは非公開扱いです。",
        )
        cases: dict[tuple[str, ...], tuple[tuple[str, ...], tuple[str, ...]]] = {
            (): ((), ("Should Fix", "Nit")),
            ("--include-should-fix",): (
                ("Should Fix を inline コメントとして併記しています。",),
                ("Nit",),
            ),
            ("--include-should-fix", "--include-nit"): (
                (
                    "Should Fix を inline コメントとして併記しています。",
                    "Nit を inline コメントとして併記しています。",
                ),
                (),
            ),
        }
        for flags, (mentioned, absent) in cases.items():
            with self.subTest(flags=flags), tempfile.TemporaryDirectory() as tmp:
                completed, payload_path, _, _ = self.run_build(
                    Path(tmp), artifact(items), review_text=leaky_review, flags=flags
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                body = json.loads(payload_path.read_text(encoding="utf-8"))["body"]
                self.assertIn("Must Fix を検出しました。マージ前に修正が必要です。", body)
                self.assertNotIn("全体的に良好です", body)
                self.assertNotIn("internal_token", body)
                for fragment in mentioned:
                    self.assertIn(fragment, body)
                for fragment in absent:
                    self.assertNotIn(fragment, body)

    def test_posted_summary_reflects_inline_and_out_of_range_placement(self) -> None:
        items = [
            finding("must-1", start_line=10),
            finding("should-in", "should_fix", start_line=20),
            finding("should-out", "should_fix", start_line=200),
            finding("nit-out", "nit", start_line=300),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            completed, payload_path, _, _ = self.run_build(
                Path(tmp), artifact(items), flags=("--include-should-fix", "--include-nit")
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            body = json.loads(payload_path.read_text(encoding="utf-8"))["body"]
        self.assertIn("Should Fix を inline コメントとして併記しています（一部は行コメント不可のため本文末尾に記載）。", body)
        self.assertIn("Nit は行コメント不可のため本文末尾に記載しています。", body)
        self.assertIn("## 行コメント不可 (diff 範囲外)", body)
        self.assertIn("problem should-out", body)
        self.assertIn("problem nit-out", body)

        with tempfile.TemporaryDirectory() as tmp:
            completed, payload_path, _, _ = self.run_build(Path(tmp), artifact(items))
            self.assertEqual(completed.returncode, 0, completed.stderr)
            body = json.loads(payload_path.read_text(encoding="utf-8"))["body"]
        self.assertNotIn("Should Fix", body)
        self.assertNotIn("Nit", body)

    def test_manifest_hashes_all_required_files_and_verify_detects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            completed, payload_path, manifest_path, paths = self.run_build(
                directory,
                artifact([]),
                ci_status={"state": "success"},
                ci_summary="all checks passed",
                run_plan={"depth_actual": "standard", "recommended_mode": "standard", "risk_tags": []},
                with_sarif=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            expected_paths = {
                "findings": paths["findings"].resolve(),
                "review": paths["review"].resolve(),
                "metadata": paths["metadata"].resolve(),
                "ranges": paths["ranges"].resolve(),
                "diff": paths["diff"].resolve(),
                "ci_status": paths["ci_status"].resolve(),
                "sarif": paths["sarif"].resolve(),
                "ci_summary": paths["ci_summary"].resolve(),
                "run_plan": paths["run_plan"].resolve(),
                "payload": payload_path.resolve(),
            }
            self.assertEqual(set(manifest["files"]), set(expected_paths))
            for role, expected_path in expected_paths.items():
                record = manifest["files"][role]
                self.assertEqual(Path(record["path"]), expected_path)
                self.assertEqual(hashlib.sha256(expected_path.read_bytes()).hexdigest(), record["sha256"])

            verified = subprocess.run(
                [sys.executable, str(BUILDER_PATH), "--verify", "--manifest", str(manifest_path)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(verified.returncode, 0, verified.stderr)
            self.assertEqual(verified.stdout.strip(), "payload manifest verified")

            paths["review"].write_text("tampered", encoding="utf-8")
            paths["sarif"].unlink()
            tampered = subprocess.run(
                [sys.executable, str(BUILDER_PATH), "--verify", "--manifest", str(manifest_path)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(tampered.returncode, 1)
            self.assertIn(str(paths["review"].resolve()), tampered.stderr)
            self.assertIn("sha256 mismatch", tampered.stderr)
            self.assertIn(str(paths["sarif"].resolve()), tampered.stderr)
            self.assertIn("missing", tampered.stderr)
            self.assertNotIn("Traceback", tampered.stderr)
    def test_verify_rejects_manifest_semantic_tampering(self) -> None:
        findings_data = artifact(
            [
                finding("must-1", start_line=10),
                finding("must-2", start_line=20),
                finding("body-only", start_line=200),
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            completed, payload_path, manifest_path, _ = self.run_build(directory, findings_data)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            verified = self.run_verify(manifest_path)
            self.assertEqual(verified.returncode, 0, verified.stderr)

            missing_payload = copy.deepcopy(manifest)
            del missing_payload["files"]["payload"]
            missing_comment = copy.deepcopy(manifest)
            missing_comment["comment_map"] = []
            changed_event = copy.deepcopy(manifest)
            changed_event["event"] = "APPROVE"
            changed_targets = copy.deepcopy(manifest)
            changed_targets["semantic_targets"] = []
            changed_comment_id = copy.deepcopy(manifest)
            changed_comment_id["comment_map"][0]["finding_id"] = "must-2"
            changed_out_of_range_id = copy.deepcopy(manifest)
            changed_out_of_range_id["out_of_range"][0]["finding_id"] = "not-canonical"
            missing_flags = copy.deepcopy(manifest)
            del missing_flags["flags"]

            tampered_payload = json.loads(payload_path.read_text(encoding="utf-8"))
            tampered_payload["comments"][0]["body"] = "coordinated payload tampering"
            tampered_payload_path = directory / "tampered-payload.json"
            write_json(tampered_payload_path, tampered_payload)
            changed_payload = copy.deepcopy(manifest)
            changed_payload["files"]["payload"] = {
                "path": str(tampered_payload_path.resolve()),
                "sha256": hashlib.sha256(tampered_payload_path.read_bytes()).hexdigest(),
            }

            cases = (
                ("missing-payload", missing_payload, "files.payload"),
                ("comment-map", missing_comment, "payload.comments"),
                ("event", changed_event, "payload.event"),
                ("semantic-targets", changed_targets, "semantic_targets"),
                ("comment-id", changed_comment_id, "comment_map[0]"),
                ("payload-body", changed_payload, "payload.comments[0].body"),
                ("out-of-range-id", changed_out_of_range_id, "out_of_range"),
                ("missing-flags", missing_flags, "flags"),
            )
            for name, altered_manifest, expected_error in cases:
                with self.subTest(name=name):
                    altered_path = directory / f"{name}.json"
                    write_json(altered_path, altered_manifest)
                    invalid = self.run_verify(altered_path)
                    self.assertEqual(invalid.returncode, 1, invalid.stderr)
                    self.assertIn("INVALID payload manifest", invalid.stderr)
                    self.assertIn(expected_error, invalid.stderr)
                    self.assertNotIn("Traceback", invalid.stderr)


    def test_missing_optional_files_use_fallbacks_and_are_not_hashed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            missing_paths = {
                name: directory / name
                for name in ("ci-status.json", "run-plan.json", "ci-summary.md", "findings.sarif", "pr.diff")
            }
            flags: tuple[str, ...] = (
                "--ci-status",
                str(missing_paths["ci-status.json"]),
                "--run-plan",
                str(missing_paths["run-plan.json"]),
                "--ci-summary",
                str(missing_paths["ci-summary.md"]),
                "--sarif",
                str(missing_paths["findings.sarif"]),
                "--diff",
                str(missing_paths["pr.diff"]),
            )
            completed, payload_path, manifest_path, _ = self.run_build(
                directory,
                artifact([]),
                flags=flags,
                with_diff=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(payload_path.read_text(encoding="utf-8"))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["event"], "APPROVE")
        self.assertIn("- 検証観点: 2者レビュー (Claude/Codex hunter) + verifier 4軸 gate", payload["body"])
        self.assertIn("- CI 状態: 未取得", payload["body"])
        self.assertTrue(all(role not in manifest["files"] for role in ("ci_status", "run_plan", "ci_summary", "sarif", "diff")))

    def test_unreadable_ci_json_is_treated_as_not_obtained(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            completed, payload_path, _, _ = self.run_build(Path(tmp), artifact([]), ci_status="not-json")
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(payload_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["event"], "APPROVE")
        self.assertIn("- CI 状態: 未取得", payload["body"])


if __name__ == "__main__":
    unittest.main()
