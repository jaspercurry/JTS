# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Rust-source contract for the shared resampler underfill safety floor."""

from __future__ import annotations

from pathlib import Path


_ROOT = Path(__file__).resolve().parent.parent
_RUNTIME_CRATES = _ROOT / "rust"
_FANIN_RESAMPLER = _RUNTIME_CRATES / "jasper-fanin" / "src" / "lane_resampler.rs"
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


def test_fanin_delegates_underfill_floor_to_shared_resampler() -> None:
    """The lane resampler must CALL the shared floor, never re-derive it.

    `minimum_safe_fill_frames` is the physical underfill-unlock threshold; a
    second, drifting copy of that arithmetic is how a lane ends up thrashing
    lock->silence->relock while its config-time validation says the geometry is
    fine. Pinning the body (not just "the helper is mentioned somewhere") is
    what fails if someone inlines the formula back.

    This used to compare TWO owners — outputd's `content_bridge.rs` had the same
    method. That bridge was deleted with the `rate_match` content bridge, so the
    lane resampler is the only in-tree implementor left; `test_no_second_owner_
    of_the_underfill_floor` below is what catches a new one appearing without
    being added here.
    """
    fanin_body = _without_whitespace(_function_body(_FANIN_RESAMPLER))

    assert fanin_body == (
        "jasper_resampler::minimum_safe_fill_frames("
        "self.period_framesasu32,self.max_adjust_ppm)"
    )


def test_no_second_owner_of_the_underfill_floor() -> None:
    """Exactly one crate may define this method.

    The single-owner assertion is the whole point of the contract now that the
    comparison partner is gone: if a second daemon grows its own
    `minimum_safe_fill_frames`, it must be added to the delegation check above
    rather than quietly re-deriving the threshold.
    """
    implementors = sorted(
        path.relative_to(_ROOT).as_posix()
        for path in _RUNTIME_CRATES.rglob("*.rs")
        if _SIGNATURE in path.read_text(encoding="utf-8")
    )

    assert implementors == ["rust/jasper-fanin/src/lane_resampler.rs"], implementors
