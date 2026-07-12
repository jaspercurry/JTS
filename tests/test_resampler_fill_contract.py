# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Rust-source contract for the shared resampler underfill safety floor."""

from __future__ import annotations

from pathlib import Path


_ROOT = Path(__file__).resolve().parent.parent
_OUTPUTD_BRIDGE = _ROOT / "rust" / "jasper-outputd" / "src" / "content_bridge.rs"
_FANIN_RESAMPLER = _ROOT / "rust" / "jasper-fanin" / "src" / "lane_resampler.rs"
_SIGNATURE = "fn minimum_safe_fill_frames(&self) -> usize {"


def _function_body(path: Path) -> str:
    source = path.read_text(encoding="utf-8")
    signature_start = source.index(_SIGNATURE)
    body_start = source.index("{", signature_start) + 1
    depth = 1
    for index in range(body_start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[body_start:index]
    raise AssertionError(f"unterminated {_SIGNATURE!r} in {path}")


def _without_whitespace(value: str) -> str:
    return "".join(value.split())


def test_outputd_and_fanin_delegate_underfill_floor_to_shared_resampler() -> None:
    outputd_body = _without_whitespace(_function_body(_OUTPUTD_BRIDGE))
    fanin_body = _without_whitespace(_function_body(_FANIN_RESAMPLER))

    assert outputd_body == (
        "jasper_resampler::minimum_safe_fill_frames("
        "self.period_framesasu32,self.config.max_adjust_ppmasf64,)"
    )
    assert fanin_body == (
        "jasper_resampler::minimum_safe_fill_frames("
        "self.period_framesasu32,self.max_adjust_ppm)"
    )
