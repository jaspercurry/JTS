# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""``mixer::FORMAT`` — the Rust-side width of fan-in's snd-aloop reads (U2, #2223).

This constant configures every ``hw:Loopback,1,N`` capture open, and it had no
test anywhere in the tree, Rust or Python, before this file: the width could be
changed in the daemon with the whole suite still green. That gap is what this
module closes, and it is the only thing it claims.

**The ALSA side is pinned elsewhere, deliberately.** ``tests/test_fanin_wiring``
asserts each renderer alias's slave format, and the doctor's
``check_fanin_asound_wiring`` compares the deployed file against the same
expectation. Restating those here would make a third copy of one fact, so this
module pins the declarer they all follow and nothing else.

**Narrow here is a DECLARATION, not a driver limit.** snd-aloop carries renderer
INGRESS only (ADR-0100); the per-box program width is the RING's
(``JASPER_FANIN_RING_WIRE_FORMAT`` plus the ``shm_ring`` coupling, resolved by
``jasper.fanin_coupling.resolve_ring_wire``). This width governs what fan-in
reads IN, never what the box plays out.

**There was a second declarer by accident, and U4/P7-2 retired it.**
``jasper/cli/aec_tune.py`` used to open the summed program's dsnoop RAW to
record its AEC reference; a raw open cannot absorb a format move. It reads
jasper-outputd's speaker-monitor UDP feed now, and the tap's PCM definition is
gone from asound.conf entirely.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# The width, in Rust's `alsa::Format` spelling. Deliberately written out rather
# than derived from the ALSA token: the underscore difference between the two
# spellings is exactly the kind of near-miss a `replace("_","")` would paper
# over.
ALOOP_RENDERER_LANE_FORMAT_RUST = "S16LE"

FANIN_MIXER_RS = REPO / "rust" / "jasper-fanin" / "src" / "mixer.rs"
AEC_TUNE = REPO / "jasper" / "cli" / "aec_tune.py"


def test_the_reader_declares_the_lanes_narrow():
    """`jasper-fanin`'s ALSA capture format — the declarer the aliases follow."""
    text = FANIN_MIXER_RS.read_text()
    assert (
        f"pub const FORMAT: Format = Format::{ALOOP_RENDERER_LANE_FORMAT_RUST};" in text
    ), (
        "rust/jasper-fanin/src/mixer.rs must declare the aloop renderer lanes' "
        f"width as Format::{ALOOP_RENDERER_LANE_FORMAT_RUST}. If these lanes are "
        "being widened, the asoundrc slaves pinned in tests/test_fanin_wiring.py "
        "move in the same commit."
    )


def _string_constants(tree: ast.AST) -> tuple[list[ast.Constant], list[ast.Constant]]:
    """Every string Constant in `tree`, split into (docstrings, values)."""
    docstring_ids = set()
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
            docstring_ids.add(id(body[0].value))
    docstrings: list[ast.Constant] = []
    values: list[ast.Constant] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            (docstrings if id(node) in docstring_ids else values).append(node)
    return docstrings, values


def test_the_raw_dsnoop_diagnostic_reader_is_retired():
    """The accidental declarer is gone, and this is what keeps it gone (U4/P7-2).

    Asserted as an ABSENCE over *values* only, docstrings excluded, mirroring
    P7-1's guard in `tests/test_aec_ref_source_retirement.py`: the tool's own
    prose names the retired PCM to explain the retirement, and must neither
    satisfy nor trip the guard against it. A grep would do both. A re-point
    back onto the tap fails here with the reason, not silently at the next
    widening.
    """
    docstrings, values = _string_constants(ast.parse(AEC_TUNE.read_text()))

    # Positive control FIRST — the assertion below is an ABSENCE, so a reader
    # that found nothing would satisfy it vacuously. The tool's prose DOES name
    # the retired tap, which proves both that the walk reached real content and
    # that excluding docstrings is load-bearing rather than decorative.
    assert any("jasper_capture" in node.value for node in docstrings), (
        "aec_tune's own docstrings should still explain the retirement — if "
        "that prose is gone, this guard is no longer reading what it thinks"
    )

    offenders = sorted(
        f"{AEC_TUNE.name}:{node.lineno}: {node.value!r}"
        for node in values
        if "jasper_capture" in node.value or "jasper_ref" in node.value
    )
    assert not offenders, (
        "jasper/cli/aec_tune.py must not name the aloop dsnoop tap — it moved "
        "to jasper-outputd's UDP speaker monitor in U4/P7-2, and re-opening "
        "the raw tap would make it a width declarer again:\n"
        + "\n".join(offenders)
    )
