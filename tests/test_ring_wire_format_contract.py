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

**Two contracts live here, and the second is not implied by the first.** Token
normalization (above) says what ``S32_LE`` means. The ASSISTANT WIDTH VERDICT
(below, U2 PR-2) says what a BOX is — and it is a conjunction: the declared
format AND the ``shm_ring`` coupling, because an snd-aloop substream is an S16
device however the box spelled its format. Pinning only the normalizer left the
two languages free to reach different verdicts about the same box from the same
tokens, which is exactly what happened: the Python assistant-width predicate
keyed on the token alone while ``Config::program_wire_is_wide`` had always
required both halves.

The third declaring end — the PROSE — is pinned in
``tests/test_docs_assistant_width_phrasing.py`` rather than here, because that
lane's environment is minimal and this file imports ``jasper``.

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
    """The parsed function's own text, delimited by things IT owns.

    The end anchor is "the next method in this impl block", not a sibling's
    NAME: anchoring on ``pub fn sample_format_id(`` made an unrelated rename
    break this file spuriously, which is a false alarm on a contract that should
    only fire when the parse itself moves.
    """
    start = source.index("pub fn from_env_value(")
    end = source.find("\n    pub fn ", start + 1)
    if end == -1:  # last method in the impl block
        end = source.index("\n}", start)
    return source[start:end]


def _rust_variant_tokens(source: str) -> dict[str, str]:
    """``{"S16Le": "S16_LE", ...}`` from the daemon's own ``as_str``.

    Anchored on ``impl RingWireFormat`` rather than the first ``pub fn as_str(``
    in the file: ``Coupling`` grew one too (U2 PR-2's startup width line needs
    the transport token), and it is declared earlier, so an unanchored search
    silently read the wrong enum's map.
    """
    impl_at = source.index("impl RingWireFormat {")
    start = source.index("pub fn as_str(", impl_at)
    end = source.index("\n}", start)
    variants = dict(_AS_STR_RE.findall(source[start:end]))
    assert variants, "could not read RingWireFormat::as_str's token map"
    # The map must be the FORMAT vocabulary, not the transport's — the exact
    # confusion the anchor above prevents.
    assert set(variants.values()) == set(RING_WIRE_FORMATS), variants
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
    assert resolve_ring_wire_format(" S32_LE ") == RING_WIRE_FORMAT_WIDE
    # The Rust half of the trim can only be READ from here, never executed, so
    # this accepts either spelling of it and tolerates rustfmt's whitespace —
    # it fires when the trim is REMOVED, not when it is reformatted or rewritten
    # as an equivalent closure.
    assert re.search(
        r"\.\s*map\s*\(\s*(str::trim|\|\s*\w+\s*\|\s*\w+\s*\.\s*trim\s*\(\s*\))\s*\)",
        body,
    ), (
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



# ---------------------------------------------------------------------------
# The ASSISTANT WIDTH verdict (U2 PR-2) — a conjunction, pinned across the two
# languages by re-deriving the Rust rule's own truth table from its source.
# ---------------------------------------------------------------------------

_PROTOCOL_RS = _REPO / "rust" / "jasper-tts-protocol" / "src" / "lib.rs"
# `if wire_format_is_wide && coupling_is_shm_ring { Wide } else { Narrow }`
_FROM_BOX_DECLARATION_RE = re.compile(
    r"pub fn from_box_declaration\(\s*"
    r"wire_format_is_wide: bool,\s*"
    r"coupling_is_shm_ring: bool,\s*"
    r"\) -> TtsWireWidth \{\s*"
    r"if (?P<cond>[^\{]+?)\s*\{\s*"
    r"TtsWireWidth::(?P<then>\w+)\s*\}\s*else\s*\{\s*"
    r"TtsWireWidth::(?P<other>\w+)\s*\}",
    re.S,
)


def _rust_assistant_width_rule() -> "tuple[str, str, str]":
    """(condition, then-variant, else-variant) read out of the Rust source."""
    if not _PROTOCOL_RS.exists():
        pytest.skip(f"rust source not present: {_PROTOCOL_RS}")
    match = _FROM_BOX_DECLARATION_RE.search(_PROTOCOL_RS.read_text(encoding="utf-8"))
    assert match, (
        "could not locate TtsWireWidth::from_box_declaration in "
        f"{_PROTOCOL_RS} — if it was renamed or reshaped, this contract must be "
        "re-pointed rather than deleted"
    )
    return match.group("cond").strip(), match.group("then"), match.group("other")


def test_the_rust_assistant_width_rule_is_the_conjunction_of_both_halves() -> None:
    """The Rust side, re-derived rather than restated.

    If someone relaxes that `&&` to an `||`, or drops the coupling term, this
    fails here — before the Python mirror below is compared against a rule that
    has quietly changed.
    """
    cond, then_variant, else_variant = _rust_assistant_width_rule()
    assert cond == "wire_format_is_wide && coupling_is_shm_ring", cond
    assert then_variant == "Wide"
    assert else_variant == "Narrow"


def test_the_two_languages_reach_the_same_assistant_width_verdict(monkeypatch) -> None:
    """VERDICT PARITY over the full 2x2, not token parity.

    The Rust verdict is evaluated from the rule this file just read out of the
    source; the Python verdict comes from the live predicate. Both are computed
    here — neither side is a hand-written expectation, so a change to either
    that is not matched by the other fails.
    """
    from jasper.fanin_coupling import assistant_wire_is_wide

    cond, then_variant, else_variant = _rust_assistant_width_rule()
    assert cond == "wire_format_is_wide && coupling_is_shm_ring"

    for wire_format in (RING_WIRE_FORMAT, RING_WIRE_FORMAT_WIDE):
        for coupling in ("loopback", "shm_ring"):
            # Evaluate the RUST rule's own condition on these two halves.
            rust_variant = (
                then_variant
                if (wire_format == RING_WIRE_FORMAT_WIDE and coupling == "shm_ring")
                else else_variant
            )
            python_wide = assistant_wire_is_wide(
                wire_format=wire_format, coupling=coupling
            )
            assert python_wide is (rust_variant == "Wide"), (
                f"verdict shear at ({wire_format}, {coupling}): "
                f"rust={rust_variant} python={'Wide' if python_wide else 'Narrow'}"
            )

    # The row that made this contract necessary, named so a reader sees it.
    assert (
        assistant_wire_is_wide(
            wire_format=RING_WIRE_FORMAT_WIDE, coupling="loopback"
        )
        is False
    ), "a declared-wide box that never armed the ring outputs to an S16 substream"


def test_the_assistant_width_defaults_to_the_declared_files(monkeypatch) -> None:
    """Both halves default to a FILE-FRESH read of the same SSOT the daemons use.

    Not ``os.environ``: ``jasper-voice`` and the socket-activated wizards never
    loaded ``fanin.env``, which is the stale-``os.environ`` class AGENTS.md
    canonizes.
    """
    import jasper.fanin.coupling_reconcile as cr
    import jasper.fanin_coupling as fc

    seen = {"format": 0, "coupling": 0}

    def _format() -> str:
        seen["format"] += 1
        return RING_WIRE_FORMAT_WIDE

    def _coupling(*_a, **_k) -> str:
        seen["coupling"] += 1
        return "shm_ring"

    monkeypatch.setattr(fc, "read_declared_ring_wire_format", _format)
    monkeypatch.setattr(cr, "read_persisted_coupling", _coupling)
    assert fc.assistant_wire_is_wide() is True
    assert seen == {"format": 1, "coupling": 1}, (
        "both halves must be read from their own SSOT reader"
    )
