# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pin the README invariant: ``jasper-outputd`` is the only normal writer to
the physical DAC.

README ("Important: one final output owner") and AGENTS.md ("Final output —
jasper-outputd") both assert that ``jasper-outputd`` owns the final DAC write
loop and *nothing else normally writes to the physical sink*. Music routes
through renderer loopback lanes → fan-in → CamillaDSP → outputd; assistant
TTS/cues enter fan-in's outputd-compatible TTS socket. No renderer, no
CamillaDSP, no voice daemon opens the physical DAC directly.

**Where the invariant is actually enforced.** The physical DAC is exposed to
daemons through exactly one wired ALSA PCM alias, ``outputd_dac`` (rendered by
``deploy/lib/jasper-asound-render.sh`` to ``type hw; card <detected DAC>``).
Whichever daemon is configured to *open* ``outputd_dac`` is the writer. The
enforcement point is therefore the shipped systemd unit wiring: ``outputd_dac``
is named by exactly one unit (``jasper-outputd.service``, via
``JASPER_OUTPUTD_DAC_PCM``), and the sibling audio daemons route to loopback
lanes / local sockets instead. The rollback alias ``pcm.jasper_out`` (also a
raw ``hw:CARD=...`` on the physical card, kept for pre-outputd rollback) is
deliberately wired into no daemon.

These tests parse the unit files as data (no hardware). They fail loudly if a
future edit points a second daemon at the physical DAC — the exact "two
writers" regression the README invariant forbids. They intentionally do NOT
re-assert outputd's own DAC-PCM env line beyond identifying the writer (that
positive is pinned by ``test_outputd_systemd.py``); the new coverage here is
the cross-unit *sole*-writer claim, which the 2026-07-11 deep audit flagged as
unverified.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SYSTEMD_DIR = REPO / "deploy" / "systemd"

# The one ALSA PCM alias that the outputd topology wires to the physical DAC
# (``type hw; card <detected>`` — see deploy/lib/jasper-asound-render.sh).
PHYSICAL_DAC_PCM = "outputd_dac"

# The pre-outputd rollback alias for the same physical card
# (``pcm.jasper_out`` → ``hw:CARD=<dongle>,DEV=0``). Defined in the ALSA
# template but must stay unwired from every daemon: wiring it back in would
# create a second writer to the physical sink. Matched at a word boundary so
# it never collides with the ``jasper-outputd`` unit name or ``JASPER_OUTPUTD_``
# env prefix.
ROLLBACK_DAC_PCM_RE = re.compile(r"\bjasper_out\b")

# The audio daemons that sit on the output path and could plausibly be
# mis-wired to open the physical DAC. jasper-outputd is the sanctioned writer;
# the rest must route to loopback lanes / local sockets.
PEER_AUDIO_UNITS = (
    "jasper-voice.service",
    "jasper-camilla.service",
    "jasper-fanin.service",
    "jasper-mux.service",
    "jasper-usbsink.service",
)

OUTPUTD_UNIT = "jasper-outputd.service"


def _non_comment_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if not line.lstrip().startswith("#")]


def _unit_text(name: str) -> str:
    return (SYSTEMD_DIR / name).read_text()


def _all_units() -> dict[str, str]:
    return {p.name: p.read_text() for p in sorted(SYSTEMD_DIR.glob("*.service"))}


def test_physical_dac_pcm_is_wired_into_exactly_one_unit():
    """``outputd_dac`` (the physical DAC PCM) is opened by exactly one shipped
    systemd unit — jasper-outputd — and via its ``JASPER_OUTPUTD_DAC_PCM`` env.

    This is the load-bearing pin for "sole normal writer": if a renderer, a new
    daemon, or a copy-pasted unit references ``outputd_dac``, this set grows and
    the test fails.
    """
    units = _all_units()
    assert units, "expected shipped systemd units under deploy/systemd/"

    writers = {
        name
        for name, text in units.items()
        if any(PHYSICAL_DAC_PCM in line for line in _non_comment_lines(text))
    }
    assert writers == {OUTPUTD_UNIT}, (
        "physical DAC PCM 'outputd_dac' must be wired into exactly "
        f"jasper-outputd.service, found: {sorted(writers)}"
    )

    # The writer opens it through the documented env knob, not an incidental
    # comment/token match — so a rename can't leave outputd not-opening while
    # this test still passes on a stray occurrence.
    outputd_non_comment = "\n".join(_non_comment_lines(units[OUTPUTD_UNIT]))
    assert (
        f'Environment="JASPER_OUTPUTD_DAC_PCM={PHYSICAL_DAC_PCM}"'
        in outputd_non_comment
    ), "jasper-outputd must open the physical DAC via JASPER_OUTPUTD_DAC_PCM"


def test_rollback_dac_alias_is_wired_into_no_unit():
    """``pcm.jasper_out`` (the pre-outputd rollback alias for the raw physical
    card) must stay defined-but-unwired: no daemon unit may open it.

    Wiring it into any unit resurrects a second writer to the physical sink.
    """
    offenders = {
        name
        for name, text in _all_units().items()
        if any(ROLLBACK_DAC_PCM_RE.search(line) for line in _non_comment_lines(text))
    }
    assert not offenders, (
        "rollback alias 'jasper_out' must not be wired into any systemd unit; "
        f"found in: {sorted(offenders)}"
    )


def test_peer_audio_daemons_do_not_open_the_physical_dac():
    """The sibling output-path daemons never reference the physical DAC PCM —
    they route to loopback lanes / local sockets, per the topology.
    """
    for name in PEER_AUDIO_UNITS:
        text = _unit_text(name)
        for line in _non_comment_lines(text):
            assert PHYSICAL_DAC_PCM not in line, (
                f"{name} must not open the physical DAC ('outputd_dac'); "
                f"offending line: {line.strip()!r}"
            )


def test_voice_routes_assistant_audio_through_a_socket_not_the_dac():
    """jasper-voice hands assistant TTS/cues to a local socket transport
    (fan-in solo, outputd when bonded) — the "voice/cues route through
    fan-in/outputd sockets, not the DAC" half of the invariant.
    """
    non_comment = "\n".join(_non_comment_lines(_unit_text("jasper-voice.service")))
    assert 'Environment="JASPER_TTS_TRANSPORT=outputd"' in non_comment, (
        "voice must declare the socket TTS transport"
    )
    # The socket endpoint lives under a daemon runtime dir, never a hw device.
    socket_line = next(
        (
            line
            for line in non_comment.splitlines()
            if "JASPER_TTS_OUTPUTD_SOCKET=" in line
        ),
        None,
    )
    assert socket_line is not None, "voice must set JASPER_TTS_OUTPUTD_SOCKET"
    assert "/run/jasper-fanin/" in socket_line or "/run/jasper-outputd/" in socket_line, (
        f"voice TTS socket must be a fan-in/outputd runtime socket: {socket_line.strip()!r}"
    )
    assert PHYSICAL_DAC_PCM not in socket_line
