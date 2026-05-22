#!/usr/bin/env python3
"""Detect BEAR.Sunday projects and optional bear-review skill availability.

The helper is intentionally read-only and dependency-free so pr-codex can run it
inside review setup without making the overall review fail when BEAR.Skills is
not installed.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable

BEAR_PACKAGES = {
    "bear/sunday",
    "bear/resource",
    "bear/package",
    "ray/di",
}


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _composer_signals(repo_dir: Path) -> list[str]:
    composer = _load_json(repo_dir / "composer.json")
    if not composer:
        return []
    signals: list[str] = []
    for section in ("require", "require-dev"):
        deps = composer.get(section)
        if not isinstance(deps, dict):
            continue
        for package in deps:
            package_name = str(package).lower()
            if package_name in BEAR_PACKAGES or package_name.startswith("bear/"):
                signals.append(f"composer:{package_name}")
    return sorted(set(signals))


def _layout_signals(repo_dir: Path) -> list[str]:
    candidates = [
        ("layout:src/Resource", repo_dir / "src" / "Resource"),
        ("layout:src/Module", repo_dir / "src" / "Module"),
        ("layout:src/Provider", repo_dir / "src" / "Provider"),
    ]
    return [name for name, path in candidates if path.exists()]


def _first_existing(paths: Iterable[Path]) -> Path | None:
    for path in paths:
        if path.exists() and path.is_file():
            return path
    return None


def detect_bear_sunday(repo_dir: Path, bear_review_skill_paths: Iterable[Path]) -> dict[str, Any]:
    """Return a deterministic BEAR.Sunday detection result for ``repo_dir``."""
    repo_dir = repo_dir.resolve()
    composer_signals = _composer_signals(repo_dir)
    layout_signals = _layout_signals(repo_dir)
    signals = composer_signals + layout_signals

    # A BEAR composer dependency is authoritative.  Without composer evidence,
    # require at least two layout signals to avoid classifying any generic
    # project with a single Resource directory as BEAR.Sunday.
    is_bear_sunday = bool(composer_signals) or len(layout_signals) >= 2
    framework_detected = "bear-sunday" if is_bear_sunday else None

    skill_path = _first_existing(Path(p).expanduser() for p in bear_review_skill_paths)
    if not is_bear_sunday:
        bear_review = {
            "status": "not_applicable",
            "skill_path": str(skill_path) if skill_path else None,
            "skip_reason": "not BEAR.Sunday",
        }
    elif skill_path:
        bear_review = {
            "status": "available",
            "skill_path": str(skill_path),
            "skip_reason": None,
        }
    else:
        bear_review = {
            "status": "unavailable",
            "skill_path": None,
            "skip_reason": "bear-review skill unavailable",
        }

    return {
        "schema_version": "bear-review-context.v1",
        "repo_dir": str(repo_dir),
        "framework_detected": framework_detected,
        "is_bear_sunday": is_bear_sunday,
        "detection_signals": signals,
        "bear_review": bear_review,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-dir", required=True, type=Path)
    parser.add_argument("--bear-review-skill", action="append", default=[], type=Path)
    parser.add_argument("--out-json", type=Path)
    args = parser.parse_args()

    result = detect_bear_sunday(args.repo_dir, args.bear_review_skill)
    text = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.out_json:
        args.out_json.parent.mkdir(parents=True, exist_ok=True)
        args.out_json.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
