#!/usr/bin/env python3
"""Validate review-rounds.v1 artifacts emitted by /pr-codex:review."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from refinement_loop import validate_review_rounds_artifact


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - CLI reports parse/path failures uniformly
        raise ValueError(f"{path}: cannot read/parse JSON: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a review-rounds.v1 artifact")
    parser.add_argument("--schema", type=Path, help="accepted for workflow symmetry; semantic checks are stdlib-only")
    parser.add_argument("--data", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        data = load_json(args.data)
    except ValueError as exc:
        print(f"INVALID review rounds artifact: {exc}", file=sys.stderr)
        return 1

    errors = validate_review_rounds_artifact(data)
    if errors:
        print("INVALID review rounds artifact", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print("review rounds artifact valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
