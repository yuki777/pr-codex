#!/usr/bin/env python3
"""Extract provider-reported actual USD cost from CLI logs.

This helper intentionally does not estimate cost from tokens or maintain a
pricing table.  It only records cost values that a CLI/provider already
reported in the logs.  If no such value is found, the result is unavailable.
"""

from __future__ import annotations

import argparse
import json
import re
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

COST_PATTERNS = [
    re.compile(r"(?im)\btotal\s+cost\s*[:=]\s*\$\s*(?P<amount>\d+(?:\.\d+)?)\b"),
    re.compile(r"(?im)\bcost_usd\s*[:=]\s*\$?\s*(?P<amount>\d+(?:\.\d+)?)\b"),
    re.compile(r"(?im)\bactual(?:_usd|\s+usd)?\s+cost\s*[:=]\s*\$?\s*(?P<amount>\d+(?:\.\d+)?)\b"),
]


def quantize_cost(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def extract_reported_cost(text: str) -> Decimal | None:
    matches: list[Decimal] = []
    for pattern in COST_PATTERNS:
        for match in pattern.finditer(text):
            matches.append(Decimal(match.group("amount")))
    if not matches:
        return None
    # Use the last provider-reported value in a log because CLIs commonly append
    # the final total after intermediate progress/cost lines.
    return matches[-1]


def parse_component(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("component must be TOOL=PATH")
    tool, path = value.split("=", 1)
    tool = tool.strip()
    if not tool:
        raise argparse.ArgumentTypeError("component tool name must not be empty")
    return tool, Path(path).expanduser()


def build_cost(components: list[tuple[str, Path]]) -> dict[str, object]:
    reported_components: list[dict[str, object]] = []
    total = Decimal("0")
    for tool, path in components:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            continue
        amount = extract_reported_cost(text)
        if amount is None:
            continue
        total += amount
        reported_components.append(
            {
                "tool": tool,
                "actual_usd": quantize_cost(amount),
                "source": "cli_log",
            }
        )

    if not reported_components:
        return {
            "actual_usd": None,
            "currency": "USD",
            "source": "unavailable",
            "components": [],
        }

    return {
        "actual_usd": quantize_cost(total),
        "currency": "USD",
        "source": "provider_reported",
        "components": reported_components,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--component",
        action="append",
        type=parse_component,
        default=[],
        metavar="TOOL=PATH",
        help="CLI/tool log to scan for provider-reported actual cost",
    )
    args = parser.parse_args()
    print(json.dumps(build_cost(args.component), ensure_ascii=False, indent=2, sort_keys=False))


if __name__ == "__main__":
    main()
