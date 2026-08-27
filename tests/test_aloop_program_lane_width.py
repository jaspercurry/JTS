# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The snd-aloop program lane's width is ONE fact with two declarers (U2, #2223).

The lane: ``jasper-fanin`` used to write its summed program to
``hw:Loopback,0,7`` and ``pcm.jasper_capture`` dsnoops ``hw:Loopback,1,7``. The
ring is the only fan-in → CamillaDSP transport (ADR-0100), so the lane has
neither a writer nor a reader: U4/P7-2 moved the last reader off the tap and
U4/P7-4 dropped the lossy aloop MIRROR that used to shadow the ring onto it.
``mixer::FORMAT`` still decides the width of the per-renderer capture inputs on
the same card, and the shipped dsnoop still declares it, so the two have to keep
agreeing until the tap's definition is removed.

**It is narrow, and both places say so without either knowing about the
other.** Before this file, ``mixer::FORMAT`` — the writer, and the one that
decides what the other must say — had NO test anywhere in the tree, Rust or
Python. This pins the set together so the lane's width cannot be half-moved: a
widening has to move every declarer in the same commit or fail here with the
list of what it missed.

**There was a third, and U4/P7-2 retired it.** ``jasper/cli/aec_tune.py``
used to open the dsnoop RAW to record its AEC reference, which made it a
declarer by accident — a raw open cannot absorb a format move. It reads
jasper-outputd's speaker-monitor UDP feed now, so it declares nothing about
this lane, and the guard below keeps it that way rather than merely noting it.

**Narrow here is a DECLARATION, not a driver limit**, and that distinction is
load-bearing enough to be a test rather than a comment. An snd-aloop substream
is not "an S16 device": the **post-DSP content lane role** runs ``S32_LE`` on
every box in the fleet, and has since the wide-output-path program's PR-6, on a
substream pair (``hw:Loopback,0/1,6``) on the SAME card. So the reason this
lane is narrow is ``mixer::FORMAT`` and the literals that follow it — nothing
about snd-aloop.

**And the counter-example is a ROLE, not a device.** What is true, and what the
test below asserts, is that the named post-DSP content PCMs declare ``S32_LE`` —
which is all the counter-example needs: an snd-aloop substream demonstrably CAN
carry S32. It is not a statement about ``hw:Loopback,0/1,6``, which a raw opener
could take at any width the card accepts. (One did: the bonded active-endpoint
grouping ingress opened that pair raw at ``S16_LE`` until it moved to
``jasper.multiroom.grouping_ring``'s SHM ring.)

Why the campaign has not widened it (U2 PR-3): the box-level program-width
capability is the ring's — ``JASPER_FANIN_RING_WIRE_FORMAT`` plus the
``shm_ring`` coupling, resolved by ``jasper.fanin_coupling.resolve_ring_wire``
and read by every declaring end. A second width mechanism on this lane would
have to move the realtime pacer's own write and teach the asound.conf render
path a per-box format it does not carry today. It no longer has to fix a raw
dsnoop open: ``jasper/cli/aec_tune.py`` was the one reader that could not
absorb a format move, and P7-2 moved it off the tap. Ownership of the hop sits
with P7 (re-point the dsnoop consumers, drop the mirror — both halves landed,
at P7-2 and P7-4) and P9 (remove snd-aloop), not with U2.
"""

from __future__ import annotations

import ast
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
# The two declarers, and the guard that keeps the retired third retired.
# ---------------------------------------------------------------------------


def test_the_writer_declares_the_lane_narrow():
    """`jasper-fanin`'s ALSA output format — the declarer the others follow.

    This constant configures the `hw:Loopback,0,7` open. It had no test before
    this one, which meant the lane's width could be changed in the daemon with
    the whole suite still green while the dsnoop and the doctor both still said
    S16.
    """
    text = FANIN_MIXER_RS.read_text()
    assert (
        f"pub const FORMAT: Format = Format::{ALOOP_PROGRAM_LANE_FORMAT_RUST};" in text
    ), (
        "rust/jasper-fanin/src/mixer.rs must declare the aloop program lane's "
        f"width as Format::{ALOOP_PROGRAM_LANE_FORMAT_RUST}. If this lane is "
        "being widened, every declarer in tests/test_aloop_program_lane_width.py "
        "moves in the same commit."
    )


def test_the_dsnoop_reader_declares_the_same_width():
    """`pcm.jasper_capture`'s slave — CamillaDSP's capture side of the lane.

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


def _docstring_node_ids(tree: ast.AST) -> set[int]:
    """Ids of every node that is a docstring rather than a value."""
    ids = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", None)
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            ids.add(id(body[0].value))
    return ids


def test_the_raw_dsnoop_diagnostic_reader_is_retired():
    """The fourth declarer is gone, and this is what keeps it gone (U4/P7-2).

    `aec_tune` used to open the dsnoop RAW (no `plug:`), so it could not absorb
    a format move — it was the concrete cost of widening this lane. It reads
    jasper-outputd's speaker-monitor UDP feed now. The only in-tree consumer
    left, CamillaDSP, reaches the lane through `plug:jasper_capture` and is
    format-tolerant by construction. (The `jasper_ref` plug alias would be too,
    but nothing has opened it since U4/P7-3.)

    Asserted as an ABSENCE over *values* only, docstrings excluded, mirroring
    P7-1's guard in `tests/test_aec_ref_source_retirement.py`: the tool's own
    prose names the retired PCM to explain the retirement, and must neither
    satisfy nor trip the guard against it. A grep would do both. A re-point
    back onto the tap fails here with the reason, not silently at the next
    widening.
    """
    tree = ast.parse(AEC_TUNE.read_text())
    docstrings = _docstring_node_ids(tree)
    offenders = sorted(
        f"{AEC_TUNE.name}:{node.lineno}: {node.value!r}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and ("jasper_capture" in node.value or "jasper_ref" in node.value)
        and id(node) not in docstrings
    )
    assert not offenders, (
        "jasper/cli/aec_tune.py must not name the aloop dsnoop tap — it moved "
        "to jasper-outputd's UDP speaker monitor in U4/P7-2, and re-opening "
        "the raw tap would make it a width declarer again:\n"
        + "\n".join(offenders)
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
    "hw:Loopback,0/1,6 is wide"; the module docstring's "the counter-example is
    a ROLE, not a device" owns why.
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
