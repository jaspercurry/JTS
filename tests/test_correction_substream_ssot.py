# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contract: one owner for the correction/commissioning fan-in lane name
(issue #1789).

``jasper.audio_measurement.correction_lane.CORRECTION_SUBSTREAM`` is the
single source of truth for the ALSA pcm alias ``"correction_substream"``.
Before this test existed the literal was independently re-declared under
five differently-named constants (and a few bare literals) across eleven
files in ``jasper.correction``, ``jasper.active_speaker``, ``jasper.web``,
and ``jasper.cli`` — nothing enforced that the copies agreed, so a rename
would have silently diverged.

Three checks:

  1. **The drift guard.** No file under ``jasper/`` other than the owning
     module declares a string constant whose value is EXACTLY
     ``"correction_substream"``. This is the test that fails when someone
     adds a sixth copy. Matched by VALUE from the parsed AST — the same
     technique ``tests/test_correction_boundary_ssot.py`` uses for the
     room-correction band edge — so prose that merely *mentions* the lane
     (docstrings, comments, diagnostic messages) is not a false positive,
     and neither are the differently-valued derived tags like
     ``"correction_substream_continuous_tone"``: those are different string
     values, not this one. Only ``jasper/`` is scanned; see
     ``jasper/audio_measurement/correction_lane.py``'s "Scope of the drift
     guard" docstring section for what legitimately spells the literal
     outside it (the checked-in ALSA config, four stdlib-only lab probes)
     and why.
  2. **Co-ownership.** The sites that used to hold their own named copy of
     the literal still resolve to the SSOT's value, not a reintroduced local
     override.
  3. **The placement promise, proven rather than merely stated.**
     ``correction_lane.py`` lives in ``jasper.audio_measurement`` instead of
     ``jasper.correction.playback`` specifically so four socket-activated
     modules never pay for ``numpy``. That promise had no test until a
     reviewer demonstrated the gap empirically: adding
     ``from .playback import play_wav as play_wav`` to
     ``jasper/audio_measurement/__init__.py`` drags ``numpy`` into all four
     consumers while checks 1 and 2 above — and every other guard in this
     repo — stayed green, because none of them execute the consumers' import
     chains. ``test_lane_constant_consumers_stay_numpy_free`` closes that gap
     by actually importing each consumer in a subprocess with ``numpy`` and
     ``scipy`` poisoned in ``sys.modules`` (the same technique
     ``tests/test_web_wizard_import_chain.py`` uses for the wizards' other
     heavy deps), so a transitive re-import anywhere in the chain — including
     a stray convenience re-export in this module's own package
     ``__init__.py`` — raises immediately instead of silently passing because
     an already-installed copy happened to mask it.
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from jasper import renderer_lanes
from jasper.audio_measurement.correction_lane import CORRECTION_SUBSTREAM
from jasper.correction import playback

REPO_ROOT = Path(__file__).resolve().parents[1]
JASPER_ROOT = REPO_ROOT / "jasper"

# The one file allowed to declare the literal.
OWNING_MODULE = JASPER_ROOT / "audio_measurement" / "correction_lane.py"


def _literal_occurrences(path: Path) -> list[tuple[int, str]]:
    """Every place in ``path`` where a string constant is EXACTLY the lane
    literal, as (line number, source line) pairs.

    AST-based and matched by value rather than scanned as text, for the same
    reason ``test_correction_boundary_ssot.py`` parses rather than
    text-searches: prose that merely names the lane is not a re-declaration
    (its literal is a longer string, not an exact match), and neither is a
    longer derived value like ``"correction_substream_continuous_tone"``.
    """
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    lines = source.splitlines()
    return [
        (node.lineno, lines[node.lineno - 1].strip())
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and node.value == CORRECTION_SUBSTREAM
    ]


def test_no_new_literal_declarations_outside_the_owning_module() -> None:
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{lineno}: {line}"
        for path in sorted(JASPER_ROOT.rglob("*.py"))
        if path != OWNING_MODULE
        for lineno, line in _literal_occurrences(path)
    ]
    assert not offenders, (
        "the correction/commissioning fan-in lane literal "
        f"{CORRECTION_SUBSTREAM!r} was re-declared outside its owning module "
        f"({OWNING_MODULE.relative_to(REPO_ROOT)}) — import "
        "CORRECTION_SUBSTREAM from jasper.audio_measurement.correction_lane "
        "instead of spelling the string again:\n" + "\n".join(offenders)
    )


def test_owning_module_value_is_the_expected_lane_name() -> None:
    # Pins the value itself, so a silent edit to the SSOT is visible here
    # rather than only surfacing later as a runtime ALSA "no such pcm"
    # failure.
    assert CORRECTION_SUBSTREAM == "correction_substream"


def test_known_consuming_sites_resolve_to_the_ssot() -> None:
    """The sites that still hold a named copy of the LANE NAME agree with it.

    Since P6c-ii the lane's ACTIVE device is not a constant anywhere — it is
    ``correction_play_device()``'s per-call answer (the owning module's
    transport reader, pinned by ``tests/test_renderer_ring_lanes.py``'s
    unitless device facts). What remains constant is the lane's NAME on the
    shipped snd-aloop transport, and exactly two consumers still hold a
    named copy of it: ``jasper.correction.playback``'s Room-compat
    re-export, and the ``correction`` row's ``aloop_device`` in
    ``jasper.renderer_lanes`` (which imports the SSOT rather than
    respelling the literal).

    Seven historical aliases are gone rather than listed here: P6c-0
    dissolved ``sound_setup.VOLUME_FLOOR_TONE_ALSA_DEVICE``,
    ``sync_flow.PLAYBACK_DEVICE``, and ``balance_flow.PLAYBACK_DEVICE``;
    P6c-ii dissolved ``web_commissioning.COMMISSION_TONE_ALSA_DEVICE``
    (and sound_setup's import of it),
    ``program_playback.CORRECTION_SUBSTREAM``, and
    ``correction.playback.DEFAULT_ALSA_DEVICE`` — those surfaces resolve
    the device through the reader at call time instead of naming it.
    """
    assert playback.CORRECTION_SUBSTREAM == CORRECTION_SUBSTREAM
    lane = renderer_lanes.lane_by_label("correction")
    assert lane is not None and lane.aloop_device == CORRECTION_SUBSTREAM


def test_guard_detects_a_reintroduced_literal(tmp_path) -> None:
    """The guard must fail on what it exists to catch, and not on prose."""
    offender = tmp_path / "offender.py"
    offender.write_text('ALSA_DEVICE = "correction_substream"\n')
    hits = _literal_occurrences(offender)
    assert hits == [(1, 'ALSA_DEVICE = "correction_substream"')]

    clean = tmp_path / "clean.py"
    clean.write_text(
        '"""Docstring mentioning correction_substream in prose."""\n'
        "# comment about correction_substream\n"
        'DERIVED_TAG = "correction_substream_continuous_tone"\n'
        "from jasper.audio_measurement.correction_lane import CORRECTION_SUBSTREAM\n"
        "ALSA_DEVICE = CORRECTION_SUBSTREAM\n"
    )
    assert _literal_occurrences(clean) == []


# ---------------------------------------------------------------------------
# Check 3 — the placement promise, proven under a poisoned import.
# ---------------------------------------------------------------------------

# The consumers whose numpy-freeness is the whole reason the constant lives in
# jasper.audio_measurement.correction_lane and not jasper.correction.playback.
_LIGHT_CONSUMERS = (
    "jasper.web.sound_setup",
    "jasper.web.sync_flow",
    "jasper.active_speaker.web_commissioning",
)

# Poisoned so a transitive re-import can't hide behind an already-installed
# copy — same technique as test_web_wizard_import_chain.py's
# test_wizard_module_imports_without_heavy_deps.
_HEAVY_MODULES = ("numpy", "scipy")


@pytest.mark.parametrize("module", _LIGHT_CONSUMERS)
def test_lane_constant_consumers_stay_numpy_free(module: str) -> None:
    poison = "".join(f"sys.modules[{name!r}] = None\n" for name in _HEAVY_MODULES)
    code = f"import sys\n{poison}import {module}\nprint('ok')\n"
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, (
        f"{module} cannot be imported with {_HEAVY_MODULES} poisoned — its "
        "import chain now transitively imports numpy or scipy. This is the "
        "placement promise jasper/audio_measurement/correction_lane.py's "
        "docstring makes: the constant lives there, not in "
        "jasper.correction.playback, precisely so this socket-activated "
        "module never pays for numpy on a 1 GB Pi. Check whether "
        "jasper/audio_measurement/__init__.py grew a convenience re-export "
        "of a heavy sibling module.\n\n"
        f"{result.stderr}"
    )
    assert result.stdout.strip() == "ok"
