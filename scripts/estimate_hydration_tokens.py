#!/usr/bin/env python3
"""Reproduce the before/after size estimate for checked-in hydration fixtures.

This provider-neutral estimate uses UTF-8 JSON bytes divided by four. It is a
stable comparison proxy, not a claim about any one model tokenizer.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from profile_os.hydration import model_facing_hydration


FIXTURE = Path(__file__).resolve().parents[1] / "tests/fixtures/hydration_packets.json"


def encoded_size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")).encode("utf-8"))


def estimate_tokens(byte_count: int) -> int:
    return math.ceil(byte_count / 4)


def main() -> None:
    fixtures = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for name, pair in fixtures.items():
        before = pair["before"]
        expected = pair["after"]
        actual = model_facing_hydration(before)
        if actual != expected:
            raise SystemExit(f"{name}: projection does not match checked-in after fixture")
        before_bytes = encoded_size(before)
        after_bytes = encoded_size(actual)
        before_tokens = estimate_tokens(before_bytes)
        after_tokens = estimate_tokens(after_bytes)
        saved = before_tokens - after_tokens
        print(f"{name}: {before_bytes} -> {after_bytes} UTF-8 JSON bytes; "
              f"~{before_tokens} -> ~{after_tokens} tokens; ~{saved} saved")


if __name__ == "__main__":
    main()
