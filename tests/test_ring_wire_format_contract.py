# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""One env key, two languages, one classification.

``JASPER_FANIN_RING_WIRE_FORMAT`` is the SINGLE input to the ring wire's format
axis. ``jasper-fanin`` normalizes it in Rust
(``RingWireFormat::from_env_value``, ``rust/jasper-fanin/src/config.rs``); the
whole Python control plane normalizes it in
``jasper.fanin_coupling.resolve_ring_wire_format``, which
``resolve_ring_wire`` — and therefore every emitter, renderer, gate and doctor
surface — resolves the box's wire through.

If those two normalizers ever classify one value differently, the box declares
one wire to its writer and another to every declaring end that derives from the
resolver: a sheared attach, which is the failure this whole rung exists to
prevent. So this file pins them against each other by READING the Rust source
and re-deriving its table, rather than by restating a table both sides could
drift from.

Rust-source pins ``pytest.skip()`` when the sources are absent, mirroring
``tests/test_fanin_host_clock_contract.py``'s idiom.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from jasper.fanin_coupling import (
    RING_WIRE_FORMAT,
    RING_WIRE_FORMAT_ENV_VAR,
    RING_WIRE_FORMAT_WIDE,
    RING_WIRE_FORMATS,
    resolve_ring_wire_format,
)

_REPO = Path(__file__).resolve().parents[1]
_FANIN_CONFIG_RS = _REPO / "rust" / "jasper-fanin" / "src" / "config.rs"

# The Rust enum variant -> wire token map, taken from `RingWireFormat::as_str`
# in the same source rather than assumed here.
_AS_STR_RE = re.compile(r"RingWireFormat::(\w+)\s*=>\s*\"([^\"]+)\"")
# `Some("S16_LE") => Ok(RingWireFormat::S16Le),`
_TOKEN_ARM_RE = re.compile(
    r"Some\(\"([^\"]+)\"\)\s*=>\s*Ok\(RingWireFormat::(\w+)\)"
)


def _config_rs() -> str:
    if not _FANIN_CONFIG_RS.exists():
        pytest.skip(f"rust source not present: {_FANIN_CONFIG_RS}")
    return _FANIN_CONFIG_RS.read_text(encoding="utf-8")


def _from_env_value_body(source: str) -> str:
    start = source.index("pub fn from_env_value(")
    end = source.index("pub fn sample_format_id(", start)
    return source[start:end]


def _rust_variant_tokens(source: str) -> dict[str, str]:
    """``{"S16Le": "S16_LE", ...}`` from the daemon's own ``as_str``."""
    start = source.index("pub fn as_str(")
    end = source.index("\n}", start)
    variants = dict(_AS_STR_RE.findall(source[start:end]))
    assert variants, "could not read RingWireFormat::as_str's token map"
    return variants


def test_both_languages_accept_exactly_the_same_wire_tokens() -> None:
    source = _config_rs()
    variants = _rust_variant_tokens(source)
    accepted = {
        token: variants[variant]
        for token, variant in _TOKEN_ARM_RE.findall(_from_env_value_body(source))
    }
    # Each accepted token maps to the variant whose own spelling IS that token —
    # a Rust arm accepting "S16_LE" but resolving the wide variant would be a
    # silent transposition no equality of key sets could catch.
    assert accepted == {token: token for token in accepted}
    assert set(accepted) == set(RING_WIRE_FORMATS), (
        "the Rust daemon and jasper.fanin_coupling accept different wire "
        f"tokens: rust={sorted(accepted)} python={sorted(RING_WIRE_FORMATS)}"
    )
    # And each is resolved to itself on the Python side too.
    for token in RING_WIRE_FORMATS:
        assert resolve_ring_wire_format(token) == token


def test_both_languages_default_an_absent_or_cleared_key_to_narrow() -> None:
    body = _from_env_value_body(_config_rs())
    assert re.search(r"None \| Some\(\"\"\)\s*=>\s*Ok\(RingWireFormat::S16Le\)", body), (
        "the Rust default arm no longer maps unset/empty to the narrow wire"
    )
    assert resolve_ring_wire_format(None) == RING_WIRE_FORMAT
    assert resolve_ring_wire_format("") == RING_WIRE_FORMAT
    # Empty is how this repo's env-file writers CLEAR a key, and both sides trim
    # before matching, so a whitespace-only value is the same fact.
    assert resolve_ring_wire_format("   ") == RING_WIRE_FORMAT
    assert "raw.map(str::trim)" in body, (
        "the Rust arm stopped trimming; Python still does, so a padded value "
        "would classify differently in the two languages"
    )


@pytest.mark.parametrize(
    "raw",
    ["bogus", "s16_le", "S16LE", "S24_3LE", "S16_LE extra", "0"],
)
def test_both_languages_fail_loud_on_a_token_neither_recognizes(raw: str) -> None:
    """Unknown is an ERROR on both sides, never a silent fall back to narrow.

    Rust makes it a config-class fault (exit 78, the unit parks). Python raises,
    and the arm gates turn that into a refusal (``resolve_wire_for_gate``). What
    must never happen is one side guessing narrow while the other refuses: the
    box would then emit and render a wire its own writer will not create.

    ``s16_le`` / ``S16LE`` are in the table on purpose — the match is
    case-SENSITIVE and separator-exact on both sides because the C ioplug's
    ``strcmp`` is.
    """
    body = _from_env_value_body(_config_rs())
    assert re.search(r"Some\(other\)\s*=>\s*Err\(", body), (
        "the Rust catch-all stopped being an error"
    )
    assert raw not in RING_WIRE_FORMATS, "this table must hold only unknowns"
    with pytest.raises(ValueError) as excinfo:
        resolve_ring_wire_format(raw)
    detail = str(excinfo.value)
    assert RING_WIRE_FORMAT_ENV_VAR in detail
    assert RING_WIRE_FORMAT in detail and RING_WIRE_FORMAT_WIDE in detail


def test_the_resolver_answers_the_declared_wire_not_a_policy_constant(
    monkeypatch,
) -> None:
    """The R6/R7 activation path, end to end through the resolver.

    Until 2026-08-11 ``resolve_ring_wire`` returned ``RING_WIRE_FORMAT`` on every
    box with no input at all — so an operator could declare the wide wire to
    ``jasper-fanin`` and every Python end would still emit, render and gate
    narrow, and jts3's wide arm was unreachable by design nobody had noticed.

    Two halves, both needed: the READER classifies the declaration, and
    ``resolve_ring_wire`` actually consults it. Pinning only the reader leaves
    the resolver free to go back to a constant.
    """
    import jasper.fanin_coupling as fc

    assert (
        fc.read_declared_ring_wire_format(env={}) == RING_WIRE_FORMAT
    ), "an undeclared box must stay narrow — that is the fleet's inertness bar"
    assert (
        fc.read_declared_ring_wire_format(
            env={RING_WIRE_FORMAT_ENV_VAR: RING_WIRE_FORMAT_WIDE}
        )
        == RING_WIRE_FORMAT_WIDE
    )

    monkeypatch.setattr(
        fc, "read_declared_ring_wire_format", lambda env=None: RING_WIRE_FORMAT_WIDE
    )
    assert fc.resolve_ring_wire().sample_format == RING_WIRE_FORMAT_WIDE
    monkeypatch.setattr(
        fc, "read_declared_ring_wire_format", lambda env=None: RING_WIRE_FORMAT
    )
    assert fc.resolve_ring_wire().sample_format == RING_WIRE_FORMAT
