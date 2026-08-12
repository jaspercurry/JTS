# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The snd-aloop program lane's width is ONE fact with four declarers (U2, #2223).

The lane: ``jasper-fanin`` writes its summed program to ``hw:Loopback,0,7`` and
``pcm.jasper_capture`` dsnoops ``hw:Loopback,1,7``. On a ``loopback``-coupled
box that lane IS the program path into CamillaDSP; on a ``shm_ring`` box the
same write is the deliberately lossy aloop MIRROR that keeps the AEC-fallback
and diagnostic taps alive (``Mixer::new``'s ``open_music_output``).

**It is narrow, and four separate places say so without any of them knowing
about the others.** Before this file, ``mixer::FORMAT`` — the writer, and the
one that decides what the other three must say — had NO test anywhere in the
tree, Rust or Python. This pins the set together so the lane's width cannot be
half-moved: a widening has to move every declarer in the same commit or fail
here with the list of what it missed.

**Narrow here is a DECLARATION, not a driver limit**, and that distinction is
load-bearing enough to be a test rather than a comment. An snd-aloop substream
is not "an S16 device": the **post-DSP content lane role** runs ``S32_LE`` on
every box in the fleet, and has since the wide-output-path program's PR-6, on a
substream pair (``hw:Loopback,0/1,6``) on the SAME card. So the reason this
lane is narrow is ``mixer::FORMAT`` and the three literals that follow it —
nothing about snd-aloop.

**The counter-example is a ROLE, not a device, and the difference is real.**
That same pair carries a second, mutually-exclusive role — the bonded
active-follower grouping round-trip — which opens the RAW device at ``S16_LE``
(``jasper.multiroom.reconcile``'s ``GROUPING_LOOPBACK_CAPTURE`` /
``GROUPING_LOOPBACK_CAPTURE_FORMAT``), bypassing the asound.conf aliases
entirely. So "``hw:Loopback,0/1,6`` is wide" would be false as a statement
about the device. What is true, and what the test below asserts, is that the
named post-DSP content PCMs declare ``S32_LE`` — which is all the
counter-example needs: an snd-aloop substream demonstrably CAN carry S32.

Why the campaign has not widened it (U2 PR-3): the box-level program-width
capability is the ring's — ``JASPER_FANIN_RING_WIRE_FORMAT`` plus the
``shm_ring`` coupling, resolved by ``jasper.fanin_coupling.resolve_ring_wire``
and read by every declaring end. A second width mechanism on this lane would
have to move the realtime pacer's own write, teach the asound.conf render path
a per-box format it does not carry today, and fix
``jasper/cli/aec_tune.py``'s RAW dsnoop open — dsnoop does not convert, so that
tool breaks the moment the slave format moves under it. Ownership of the hop
sits with P7 (re-point the dsnoop consumers, drop the mirror) and P9 (remove
snd-aloop), not with U2.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# The ALSA token every declarer of this lane must name.
ALOOP_PROGRAM_LANE_FORMAT = "S16_LE"
# The same format in Rust's `alsa::Format` spelling. Deliberately written out
# rather than derived: the underscore difference between the ALSA config token
# and the Rust enum variant is exactly the kind of near-miss a `replace("_","")`
# would paper over.
ALOOP_PROGRAM_LANE_FORMAT_RUST = "S16LE"

FANIN_MIXER_RS = REPO / "rust" / "jasper-fanin" / "src" / "mixer.rs"
ASOUNDRC = REPO / "deploy" / "alsa" / "asoundrc.jasper"
DOCTOR_AUDIO_RUNTIME = REPO / "jasper" / "cli" / "doctor" / "audio_runtime.py"
AEC_TUNE = REPO / "jasper" / "cli" / "aec_tune.py"


def _non_comment(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def _pcm_block(text: str, name: str) -> str:
    """The body of one `pcm.<name> { ... }` stanza, comments already stripped."""
    start = text.index(f"pcm.{name}")
    tail = text[start:]
    head = len(f"pcm.{name}")
    next_def = re.search(r"^(?:pcm|ctl)\.", tail[head:], re.MULTILINE)
    return tail[: head + next_def.start()] if next_def else tail


# ---------------------------------------------------------------------------
# The four declarers.
# ---------------------------------------------------------------------------


def test_the_writer_declares_the_lane_narrow():
    """`jasper-fanin`'s ALSA output format — the declarer the others follow.

    This constant configures the `hw:Loopback,0,7` open. It had no test before
    this one, which meant the lane's width could be changed in the daemon with
    the whole suite still green while the dsnoop, the doctor, and the raw
    diagnostic reader all still said S16.
    """
    text = FANIN_MIXER_RS.read_text()
    assert (
        f"pub const FORMAT: Format = Format::{ALOOP_PROGRAM_LANE_FORMAT_RUST};" in text
    ), (
        "rust/jasper-fanin/src/mixer.rs must declare the aloop program lane's "
        f"width as Format::{ALOOP_PROGRAM_LANE_FORMAT_RUST}. If this lane is "
        "being widened, every declarer in tests/test_aloop_program_lane_width.py "
        "moves in the same commit — including jasper/cli/aec_tune.py, whose RAW "
        "dsnoop open does not convert."
    )


def test_the_dsnoop_reader_declares_the_same_width():
    """`pcm.jasper_capture`'s slave — what CamillaDSP and the AEC taps share.

    dsnoop is a direct-access plugin: unlike `plug:`, it does NOT convert, so
    this literal must equal what the writer above opens or the lane fails to
    open at all.
    """
    rc = _non_comment(ASOUNDRC.read_text())
    capture = _pcm_block(rc, "jasper_capture")
    assert 'pcm "hw:Loopback,1,7"' in capture, (
        "pcm.jasper_capture must dsnoop fan-in's summed output substream"
    )
    assert f"format {ALOOP_PROGRAM_LANE_FORMAT}" in capture, (
        "the dsnoop slave format must equal jasper-fanin's mixer::FORMAT"
    )


def _slice_between(text: str, start: str, end: str) -> str:
    """The region of `text` from `start` up to the next `end` after it.

    SCOPED ON PURPOSE. Both files below name this format TWICE — once for a
    lane this contract does not own (the renderer substreams in the doctor, the
    mic capture in aec_tune) and once for the dsnoop. A bare substring
    assertion passes while the site that matters moves, which is the
    half-guarded shape that reads as covered and guards nothing.
    """
    begin = text.index(start)
    tail = text[begin + len(start) :]
    stop = tail.index(end)
    return tail[:stop]


def test_the_doctor_pins_the_same_width_on_the_live_box():
    """`check_fanin_asound_wiring` is the on-box half of this contract.

    It substring-matches the rendered /etc/asound.conf, so a widened lane whose
    doctor pin was not moved reports a healthy box that cannot open its own
    capture.
    """
    text = DOCTOR_AUDIO_RUNTIME.read_text()
    # From where the doctor takes the jasper_capture stanza to where it moves on
    # to jasper_ref — the renderer-lane loop that names the same format lives
    # OUTSIDE this window.
    window = _slice_between(
        text,
        '_asound_pcm_block(active, "jasper_capture")',
        '_asound_pcm_block(active, "jasper_ref")',
    )
    assert f'"format {ALOOP_PROGRAM_LANE_FORMAT}"' in window, (
        "check_fanin_asound_wiring must pin the jasper_capture dsnoop slave "
        "format between taking that block and moving on to jasper_ref"
    )


def test_the_raw_dsnoop_diagnostic_reader_declares_the_same_width():
    """`aec_tune` opens the dsnoop RAW (no `plug:`), so it cannot absorb a move.

    Every other in-tree consumer reaches this lane through `plug:jasper_capture`
    or `jasper_ref` and is format-tolerant by construction. This one is the
    exception, and it is the concrete cost of widening the lane.
    """
    text = AEC_TUNE.read_text()
    assert '"jasper_capture",' in text, "aec_tune must still open the raw dsnoop"
    # The argv list of the capture that names the raw dsnoop, only — the mic
    # capture a few lines below asks for the same format for its own reasons.
    argv = _slice_between(text, '"jasper_capture",', "]")
    assert '"-f",' in argv, "the raw dsnoop capture must ask for an explicit format"
    assert f'"{ALOOP_PROGRAM_LANE_FORMAT}",' in argv, (
        "jasper/cli/aec_tune.py opens the RAW dsnoop and asks arecord for an "
        "explicit format; dsnoop does not convert, so this must equal the "
        "slave format"
    )


# ---------------------------------------------------------------------------
# Narrow is a choice, and here is the box's own counter-example.
# ---------------------------------------------------------------------------


def test_a_snd_aloop_substream_is_not_inherently_narrow():
    """The same card already runs a WIDE substream ROLE, today, on every box.

    This is the guard behind the docstring's claim. It exists so a future
    reader cannot re-derive "snd-aloop can only do S16" from the narrow lane
    above and design around a limit that is not there.

    Asserted on the named post-DSP content PCMs, deliberately — NOT on
    "hw:Loopback,0/1,6 is wide", which is false: the bonded active-follower
    role opens that same raw pair at S16_LE. One role being wide is the whole
    counter-example, and it is the honest form of it.
    """
    rc = _non_comment(ASOUNDRC.read_text())
    for name in ("outputd_content_playback", "outputd_content_capture"):
        block = _pcm_block(rc, name)
        assert "hw:Loopback," in block, f"{name} must be an snd-aloop lane"
        assert "format S32_LE" in block, (
            f"{name} is the standing counter-example to 'an snd-aloop "
            "substream is an S16 device' — if it stopped being wide, the "
            "reasoning in this module and in assistant_wire_is_wide's "
            "docstring needs rewriting, not just this assertion"
        )
