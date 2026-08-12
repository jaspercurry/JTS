# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The docs state the assistant width as a CONJUNCTION, not a token.

Split out of ``tests/test_ring_wire_format_contract.py`` so a file in the docs
bundle need not import ``jasper`` to check prose. This file's job is pure text
reads — one document (``docs/HANDOFF-audio-graph-consolidation.md``) plus
``jasper/fanin_coupling.py`` read AS TEXT for the token cross-check — and it
imports STDLIB ONLY (plus pytest, the runner), asserted mechanically at the
bottom of this file rather than promised here.

**Scoped to that one document deliberately.** It is where both regressions this
file guards actually happened, and it is the prose a reader reaches first. The
other HANDOFFs are clean; widening is the right fix if a claim reappears
elsewhere, and widening the FILE LIST is that fix — never loosening the
matching, which is named-phrase by design (see the tuples below).

BE PRECISE ABOUT WHY, because the obvious reason is not the true one. The docs
bundle is NOT a minimal environment: ten files already in it import numpy
(``test_calibration_agent_*``, ``test_capture_page_js``,
``test_prepare_wake_*``, ``test_wake_review``,
``test_waveform_fusion_experiment``), so the lane that runs the bundle installs
it. The genuinely minimal environment is the ``python-policy`` job
(``uv sync --locked --group fast-landing``), and it runs exactly one file —
``DOCS_POLICY_TEST_FILES == {"tests/test_ci_classifier.py"}``. So registering a
``jasper``-importing test in the bundle does not by itself reach the minimal
env; what reached it was an autouse fixture in ``tests/conftest.py``, which runs
at the setup of every test everywhere.

This file is therefore HYGIENE, not the fix for that outage: it keeps a
bundle entry free of a package import chain that nothing holds numpy-free, so
the day ``DOCS_POLICY_TEST_FILES`` grows or ``jasper.fanin_coupling`` gains a
dependency, this guard is not what breaks.

WHAT THESE PIN. The assistant IPC wire is wide only when BOTH declared halves
are (``JASPER_FANIN_RING_WIRE_FORMAT=S32_LE`` AND the ``shm_ring`` coupling),
because fan-in's aloop write is pinned narrow however the box spelled its
format. That rule is stated in three places — Rust
(``TtsWireWidth::from_box_declaration``), Python
(``jasper.fanin_coupling.assistant_wire_is_wide``), and the prose a reader
reaches first. ``tests/test_ring_wire_format_contract.py`` pins the two code
ends against each other; this file pins the third, because a doc that teaches
the superseded token-only rule is drift even when every test is green — and it
happened: the boundary table taught the old rule while the arc row 230 lines
below taught the new one.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_CONSOLIDATION_DOC = _REPO / "docs" / "HANDOFF-audio-graph-consolidation.md"

# The wide wire's token. A LITERAL rather than an import, because importing
# `jasper.fanin_coupling` is exactly the coupling this file exists to avoid —
# and it cannot drift, because `test_the_wide_token_literal_matches_its_python_owner`
# below reads the owning module's source as text and pins them equal.
RING_WIRE_FORMAT_WIDE = "S32_LE"

# The superseded rule, in the spellings the doc actually used before the panel
# round. Named phrases, not a general pattern: this must fire on the regression
# and stay silent through ordinary rewording.
_TOKEN_ONLY_ASSISTANT_WIDTH_CLAIMS = (
    "both ends derive the width from the one `JASPER_FANIN_RING_WIRE_FORMAT`",
    "a box speaks only when its one `JASPER_FANIN_RING_WIRE_FORMAT` declaration is wide",
)

# The superseded REASON for the coupling half, which is a different defect from
# the rule above: the rule was right and its justification was false. An
# snd-aloop substream is not an S16 device — the post-DSP content lane role on
# `hw:Loopback,0/1,6` runs `S32_LE` on every box in the fleet. The coupling half
# holds because fan-in's aloop write is pinned narrow by `mixer::FORMAT`, which
# `tests/test_aloop_program_lane_width.py` pins along with the counter-example.
#
# Guarded because it already came back once: U2 PR-3 corrected this sentence at
# six sites and it SURVIVED in the U2 arc row of this same document — inside the
# very cell that PR rewrote, because the rewrite appended a paragraph and
# re-committed the false first half. A reader who believes snd-aloop cannot
# carry S32 will design around a limit that is not there.
_FALSE_SND_ALOOP_WIDTH_CLAIMS = (
    "an snd-aloop substream is an S16 device",
    "an snd-aloop output is an S16 substream",
)


def test_the_docs_do_not_restate_the_superseded_token_only_width_rule() -> None:
    """The assistant width is a CONJUNCTION; the docs must not say otherwise.

    Two places in this one file described the same rule, and when the rule
    changed only one of them was updated — so the boundary table (which a reader
    reaches first) taught the token-only rule while the arc row below it taught
    the conjunction. A mutation reverting that clause left every suite green.

    Asserted as the absence of the DEAD phrasings rather than the presence of
    the live one: a positive prose match would break on any honest rewrite,
    which is how a guard gets deleted instead of fixed.
    """
    if not _CONSOLIDATION_DOC.exists():
        pytest.skip(f"doc not present: {_CONSOLIDATION_DOC}")
    text = _CONSOLIDATION_DOC.read_text(encoding="utf-8")
    stale = [claim for claim in _TOKEN_ONLY_ASSISTANT_WIDTH_CLAIMS if claim in text]
    assert not stale, (
        "the assistant wire's width needs BOTH declared halves "
        f"({RING_WIRE_FORMAT_WIDE} AND the shm_ring coupling); these clauses "
        "state the superseded token-only rule:\n"
        + "\n".join(f"  {claim}" for claim in stale)
    )


def test_the_docs_do_not_restate_the_false_snd_aloop_width_reason() -> None:
    """The coupling half is right; "snd-aloop can only do S16" never was.

    A separate guard from the one above because it is a separate defect class:
    that one catches a doc teaching the WRONG RULE, this one catches a doc
    teaching the right rule for a REASON THAT IS NOT TRUE. The second is the
    more durable kind of drift — every test stays green, and the next design
    inherits a hardware limit that does not exist.
    """
    if not _CONSOLIDATION_DOC.exists():
        pytest.skip(f"doc not present: {_CONSOLIDATION_DOC}")
    text = _CONSOLIDATION_DOC.read_text(encoding="utf-8")
    stale = [claim for claim in _FALSE_SND_ALOOP_WIDTH_CLAIMS if claim in text]
    assert not stale, (
        "an snd-aloop substream is not an S16 device — the post-DSP content "
        "lane role on hw:Loopback,0/1,6 runs S32_LE on every box. The coupling "
        "half holds because fan-in's aloop write is pinned narrow "
        "(mixer::FORMAT). These clauses state the false reason:\n"
        + "\n".join(f"  {claim}" for claim in stale)
    )


def test_the_docs_name_both_halves_wherever_they_explain_the_assistant_width() -> None:
    """And the live wording does carry the coupling half.

    The negative test above cannot tell "correctly rewritten" from "the
    explanation was deleted". This one is coarse on purpose — it asks only that
    each place introducing the `AUDIO32` verb as a per-box choice mentions the
    coupling somewhere in the same table row or paragraph, not that it uses any
    particular sentence.
    """
    if not _CONSOLIDATION_DOC.exists():
        pytest.skip(f"doc not present: {_CONSOLIDATION_DOC}")
    text = _CONSOLIDATION_DOC.read_text(encoding="utf-8")
    blocks = [
        block
        for block in text.split("\n\n")
        if "AUDIO32" in block and RING_WIRE_FORMAT_WIDE in block
    ]
    assert blocks, "the doc no longer explains the wide assistant verb at all"
    for block in blocks:
        # The bare word "coupling" is NOT enough, and mutation testing is how
        # that was learned: `jasper.fanin_coupling` — the module these blocks
        # legitimately cite — contains it as a substring, so a block could lose
        # every statement about the coupling and still pass. Match on the token
        # or the env key, neither of which can appear incidentally in an
        # identifier.
        assert "shm_ring" in block or "JASPER_FANIN_CAMILLA_COUPLING" in block, (
            "a block introduces the wide assistant verb without naming the "
            f"coupling half:\n{block[:400]}"
        )


# ---------------------------------------------------------------------------
# The two properties this file's EXISTENCE rests on, both asserted mechanically.
# ---------------------------------------------------------------------------


def test_the_wide_token_literal_matches_its_python_owner() -> None:
    """The literal above is the same byte the package declares.

    Read as TEXT, not imported — importing `jasper.fanin_coupling` is the
    dependency this file was split out to shed. A text read keeps the single
    source of truth without taking the import back.
    """
    owner = _REPO / "jasper" / "fanin_coupling.py"
    if not owner.exists():
        pytest.skip(f"source not present: {owner}")
    match = re.search(
        r'^RING_WIRE_FORMAT_WIDE = "([^"]+)"$',
        owner.read_text(encoding="utf-8"),
        re.M,
    )
    assert match, "could not locate RING_WIRE_FORMAT_WIDE in jasper/fanin_coupling.py"
    assert match.group(1) == RING_WIRE_FORMAT_WIDE


def test_this_file_imports_stdlib_only_so_the_docs_lane_can_run_it() -> None:
    """THE REASON THIS FILE EXISTS, asserted rather than asserted-in-prose.

    The `python-policy` job installs only the `fast-landing` dependency group —
    no numpy — and `pytest-matrix` runs `needs: python-policy`, so anything that
    cannot import there takes the entire Python matrix down with it. That is not
    hypothetical: an autouse fixture importing `jasper.audio_io` errored all 93
    of that job's tests at setup and skipped the whole matrix behind it.

    This file is stdlib-only so it stays runnable in that environment whether or
    not it is ever added to `DOCS_POLICY_TEST_FILES`.

    Parses THIS file's own source, so the property is checked against the code
    rather than against a habit — adding an import is what fails it.
    """
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # a relative import is by definition not stdlib
                roots.add(f".{node.module or ''}")
            elif node.module:
                roots.add(node.module.split(".", 1)[0])

    # pytest is the runner; every lane that can run a test at all has it.
    allowed = set(sys.stdlib_module_names) | {"pytest"}
    offenders = sorted(roots - allowed)
    assert not offenders, (
        "this file is registered in DOCS_TEST_FILES and must import stdlib only; "
        f"these are neither stdlib nor pytest: {offenders}"
    )
    # And the specific one that would undo the split, named so the failure reads
    # as the regression it is rather than as a generic policy violation.
    assert "jasper" not in roots, (
        "importing `jasper` here re-couples the docs lane to the package's "
        "dependency chain — the exact coupling this file was split out to shed"
    )
