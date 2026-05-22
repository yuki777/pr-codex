#!/usr/bin/env python3
"""Regression tests for extracting commentable PR diff ranges."""

from __future__ import annotations

import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXTRACTOR = ROOT / "skills" / "lib" / "extract-diff-ranges.awk"
SEND_SKILL = ROOT / "skills" / "send" / "SKILL.md"
REVIEW_SKILL = ROOT / "skills" / "review" / "SKILL.md"


class ExtractDiffRangesTest(unittest.TestCase):
    def run_extractor(self, diff_text: str) -> str:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as fp:
            fp.write(textwrap.dedent(diff_text).lstrip())
            diff_path = Path(fp.name)
        try:
            result = subprocess.run(
                ["awk", "-f", str(EXTRACTOR), str(diff_path)],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        finally:
            diff_path.unlink(missing_ok=True)
        return result.stdout

    def test_extractor_handles_spaces_renames_len_zero_and_hunk_body_plus_headers(self) -> None:
        output = self.run_extractor(
            r'''
            diff --git a/docs/a b.md b/docs/a b.md
            index 1111111..2222222 100644
            --- a/docs/a b.md
            +++ b/docs/a b.md
            @@ -1,2 +1,3 @@
             unchanged
            +first added
            +++ b/not-a-header
            @@ -10 +11 @@
            -old
            +new
            diff --git a/old name.txt b/new name.txt
            similarity index 80%
            rename from old name.txt
            rename to new name.txt
            --- a/old name.txt
            +++ b/new name.txt
            @@ -5,2 +5,2 @@
            -old
            +new
            diff --git a/deleted.txt b/deleted.txt
            deleted file mode 100644
            --- a/deleted.txt
            +++ /dev/null
            @@ -1,2 +0,0 @@
            -gone
            diff --git a/zero.txt b/zero.txt
            --- a/zero.txt
            +++ b/zero.txt
            @@ -1,0 +5,0 @@
            diff --git a/single.txt b/single.txt
            --- a/single.txt
            +++ b/single.txt
            @@ -1 +1 @@
            -before
            +after
            '''
        )

        self.assertEqual(
            output,
            "docs/a b.md\tL1-L3\n"
            "docs/a b.md\tL11-L11\n"
            "new name.txt\tL5-L6\n"
            "single.txt\tL1-L1\n",
        )

    def test_skills_call_shared_extractor_instead_of_inline_awk_field_variables(self) -> None:
        self.assertTrue(EXTRACTOR.exists(), "shared awk extractor must be shipped")
        for skill in (SEND_SKILL, REVIEW_SKILL):
            text = skill.read_text(encoding="utf-8")
            self.assertIn("skills/lib/extract-diff-ranges.awk", text)
            self.assertIn("awk -f", text)
            self.assertNotIn("match($0", text)
            self.assertNotIn("path = $NF", text)
            self.assertNotIn("spec = $3", text)
            self.assertNotIn("awk の自動フィールド変数", text)


if __name__ == "__main__":
    unittest.main()
