# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The snd-aloop renderer lanes' width is ONE fact with two declarers (U2, #2223).

The lanes: each renderer writes ``hw:Loopback,0,N`` through a ``plug:`` alias
whose slave PINS the format, and ``jasper-fanin`` opens the matching
``hw:Loopback,1,N`` capture side at ``mixer::FORMAT``. Those are the two ends of
one substream pair, so the two literals must agree or the capture open fails.

**Both places say so without either knowing about the other.** Before this file,
``mixer::FORMAT`` — the reader, and the one that decides what the aliases must
say — had NO test anywhere in the tree, Rust or Python. This pins the set
together so the width cannot be half-moved: a widening has to move every
declarer in the same commit or fail here with the list of what it missed.

**There was a third, and U4/P7-2 retired it.** ``jasper/cli/aec_tune.py`` used
to open the summed program's dsnoop RAW to record its AEC reference, which made
it a declarer by accident — a raw open cannot absorb a format move. It reads
jasper-outputd's speaker-monitor UDP feed now, so it declares nothing, and the
guard below keeps it that way rather than merely noting it.

**Narrow here is a DECLARATION, not a driver limit.** The per-box program-width
capability is the RING's — ``JASPER_FANIN_RING_WIRE_FORMAT`` plus the
``shm_ring`` coupling, resolved by ``jasper.fanin_coupling.resolve_ring_wire``
and read by every declaring end. snd-aloop carries renderer INGRESS only
(ADR-0100), so this width governs what fan-in reads IN, never what the box
plays out.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# The ALSA token every declarer of these lanes must name.
ALOOP_RENDERER_LANE_FORMAT = "S16_LE"
# The same format in Rust's `alsa::Format` spelling. Deliberately written out
# rather than derived: the underscore difference between the ALSA config token
# and the Rust enum variant is exactly the kind of near-miss a `replace("_","")`
# would paper over.
ALOOP_RENDERER_LANE_FORMAT_RUST = "S16LE"

# Every surviving snd-aloop alias, and the substream each one pins.
RENDERER_LANE_ALIASES = {
    "librespot_substream": "hw:Loopback,0,0",
    "shairport_substream": "hw:Loopback,0,1",
    "bluealsa_substream": "hw:Loopback,0,2",
    "correction_substream": "hw:Loopback,0,4",
}

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


def test_the_reader_declares_the_lanes_narrow():
    """`jasper-fanin`'s ALSA capture format — the declarer the others follow.

    This constant configures every `hw:Loopback,1,N` open. It had no test before
    this one, which meant the lanes' width could be changed in the daemon with
    the whole suite still green while the asound.conf aliases still said S16.
    """
    text = FANIN_MIXER_RS.read_text()
    assert (
        f"pub const FORMAT: Format = Format::{ALOOP_RENDERER_LANE_FORMAT_RUST};" in text
    ), (
        "rust/jasper-fanin/src/mixer.rs must declare the aloop renderer lanes' "
        f"width as Format::{ALOOP_RENDERER_LANE_FORMAT_RUST}. If these lanes are "
        "being widened, every declarer in tests/test_aloop_program_lane_width.py "
        "moves in the same commit."
    )


def test_the_asoundrc_aliases_declare_the_same_width():
    """The renderer aliases' slaves — the playback side of the same pairs.

    The alias itself is `type plug` and would convert, but its SLAVE pin is what
    the substream actually runs; it must equal what fan-in opens on the capture
    half or the pair cannot carry the renderer's audio unconverted.
    """
    rc = _non_comment(ASOUNDRC.read_text())
    for alias, substream in RENDERER_LANE_ALIASES.items():
        block = _pcm_block(rc, alias)
        assert f'pcm "{substream}"' in block, (
            f"pcm.{alias} must pin {substream} as its slave"
        )
        assert f"format {ALOOP_RENDERER_LANE_FORMAT}" in block, (
            f"pcm.{alias}'s slave format must equal jasper-fanin's mixer::FORMAT"
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
    """The third declarer is gone, and this is what keeps it gone (U4/P7-2).

    `aec_tune` used to open the summed program's dsnoop RAW (no `plug:`), so it
    could not absorb a format move — it was the concrete cost of widening. It
    reads jasper-outputd's speaker-monitor UDP feed now, and the tap itself no
    longer exists in asound.conf at all.

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
