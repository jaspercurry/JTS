# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contract: one owner for the correction-lane ``aplay`` spawn (P6c-0).

``jasper.audio_measurement.correction_lane`` owns building (and, for the
direct-spawn sites, running) the ``aplay`` command line that plays a WAV
onto the correction lane — the same consolidation
``tests/test_correction_substream_ssot.py`` enforces for the lane *name*,
applied to the lane *spawn*. Before P6c-0 ten call sites across six files
each assembled their own inline argv; the P6c lane migration (campaign
#2285, U3 arc) would have meant ten synchronized edits.

Three groups of checks:

  1. **Golden argv/kwargs.** P6c-0 is a pure refactor: for every migrated
     site shape, the helper must produce exactly the argv and subprocess
     kwargs the site spelled inline on ``origin/main`` @ ``bb55691a1``.
     Each golden cites the pre-refactor site(s) it was derived from.
  2. **The conventions guard.** No file under ``jasper/`` outside a small,
     count-pinned allowlist may contain an ``aplay``/``-D`` spawn shape —
     this is what fails when someone adds an eleventh inline site instead
     of calling the helper. Scope is ``jasper/`` only: the five
     stdlib-only lab probe scripts under ``scripts/`` spell the command by
     design (see correction_lane.py's "Scope of the drift guard" docstring
     section — same exemption, same reason).
  3. **The guard proves itself.** Synthetic offender shapes are detected;
     prose, listing probes (``aplay -l``/``-L``), helper-built argv, and
     ``-D``-without-``aplay`` lists are not.

Deliberate non-guards (static shape scan, aimed at the accidental new
site, not adversarial evasion): a variable binary (``[self.aplay_binary,
...]`` — jasper.active_speaker.playback's audio-lab backend, which has its
own FORBIDDEN_TEST_PCM_TOKENS fence), argv assembled by concatenation
(``["aplay"] + rest``), and ``shell=True`` command strings. None exist in
``jasper/`` today for the correction lane.
"""
from __future__ import annotations

import ast
import subprocess
from pathlib import Path

from jasper.audio_measurement.correction_lane import (
    CORRECTION_SUBSTREAM,
    correction_play_argv,
    exec_correction_play,
    popen_correction_play,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
JASPER_ROOT = REPO_ROOT / "jasper"


# ---------------------------------------------------------------------------
# Check 1 — golden argv/kwargs, derived from origin/main @ bb55691a1.
# ---------------------------------------------------------------------------


def test_builder_web_family_order_matches_pre_refactor_sites() -> None:
    """``aplay -D <lane> -q <wav>`` — the web/wizard family order.

    Derived from the inline argv at (origin/main @ bb55691a1):
      * jasper/web/sound_setup.py       _LoopingVolumeFloorTone._run
      * jasper/web/sound_setup.py       _commission_tone_start (Popen)
      * jasper/web/sound_setup.py       summed-test loop (Popen)
      * jasper/active_speaker/web_commissioning.py  commission tone (Popen)
      * jasper/web/sync_flow.py         _start_playback (create_subprocess_exec)
      * jasper/web/balance_flow.py      _start_playback (create_subprocess_exec)
    All six spelled ["aplay", "-D", <lane alias>, "-q", str(wav)].
    """
    assert correction_play_argv("/tmp/marker.wav") == [
        "aplay", "-D", CORRECTION_SUBSTREAM, "-q", "/tmp/marker.wav",
    ]
    # Default order is the web-family order (and matches
    # jasper.audio_measurement.playback's own spawns).
    assert correction_play_argv("/tmp/marker.wav") == correction_play_argv(
        "/tmp/marker.wav", quiet_before_device=False
    )


def test_builder_cli_family_order_matches_pre_refactor_sites() -> None:
    """``aplay -q -D <lane> <wav>`` — the root-CLI family order.

    Derived from the inline argv at (origin/main @ bb55691a1):
      * jasper/cli/aec_commission.py       _Session._capture  (timeout=5)
      * jasper/cli/aec_commission.py       _Session.adapt     (timeout=120)
      * jasper/cli/doctor/aec_probe.py     _play_and_assess_probe
      * jasper/cli/aec_tune.py             inject-noise Popen
    All four spelled ("aplay", "-q", "-D", CORRECTION_SUBSTREAM, str(wav)).
    """
    assert correction_play_argv("/tmp/sine.wav", quiet_before_device=True) == [
        "aplay", "-q", "-D", CORRECTION_SUBSTREAM, "/tmp/sine.wav",
    ]


def test_builder_stringifies_path_objects() -> None:
    """Sites passed str(Path) inline; the builder owns that conversion now."""
    assert correction_play_argv(Path("/tmp/x.wav"))[-1] == "/tmp/x.wav"


def test_popen_wizard_shape_matches_pre_refactor_sites(monkeypatch) -> None:
    """Popen(argv, stdout=DEVNULL, stderr=DEVNULL) — the wizard Popen shape.

    Derived from (origin/main @ bb55691a1) jasper/web/sound_setup.py's
    three Popen sites and jasper/active_speaker/web_commissioning.py's
    commission-tone Popen: positional argv, stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL, no other kwargs.
    """
    captured: dict[str, object] = {}
    sentinel = object()

    def fake_popen(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return sentinel

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    proc = popen_correction_play(
        "/tmp/tone.wav", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    assert proc is sentinel
    assert captured["args"] == (
        ["aplay", "-D", CORRECTION_SUBSTREAM, "-q", "/tmp/tone.wav"],
    )
    assert captured["kwargs"] == {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }


def test_popen_operator_cli_shape_matches_pre_refactor_site(monkeypatch) -> None:
    """Popen(argv) with inherited stdio — jasper/cli/aec_tune.py's shape.

    The pre-refactor site (origin/main @ bb55691a1) passed ONLY the argv:
    ``subprocess.Popen(["aplay", "-q", "-D", CORRECTION_SUBSTREAM,
    str(noise_wav)])``. The helper passes ``stdout=None, stderr=None``
    explicitly — ``None`` is ``subprocess.Popen``'s documented default for
    both (inherit the parent's stdio), so the spawn semantics are
    identical; the kwargs are asserted here so a future edit that starts
    redirecting the operator CLI's aplay stderr fails this golden.
    """
    captured: dict[str, object] = {}

    def fake_popen(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    popen_correction_play(
        "/tmp/noise.wav", stdout=None, stderr=None, quiet_before_device=True
    )
    assert captured["args"] == (
        ["aplay", "-q", "-D", CORRECTION_SUBSTREAM, "/tmp/noise.wav"],
    )
    assert captured["kwargs"] == {"stdout": None, "stderr": None}


async def test_exec_walkthrough_shape_matches_pre_refactor_sites(monkeypatch) -> None:
    """create_subprocess_exec("aplay", "-D", <lane>, "-q", wav, DEVNULL×2).

    Derived from (origin/main @ bb55691a1) the identical
    ``_start_playback`` helpers in jasper/web/sync_flow.py and
    jasper/web/balance_flow.py: program+args as separate positionals,
    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL.
    """
    import asyncio

    captured: dict[str, object] = {}
    sentinel = object()

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return sentinel

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    proc = await exec_correction_play(
        "/tmp/marker.wav",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    assert proc is sentinel
    assert captured["args"] == (
        "aplay", "-D", CORRECTION_SUBSTREAM, "-q", "/tmp/marker.wav",
    )
    # asyncio.subprocess.DEVNULL IS subprocess.DEVNULL (re-exported int).
    assert captured["kwargs"] == {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }


def test_popen_forwards_stdout_and_stderr_independently(monkeypatch) -> None:
    """Forwarding proof, not a site golden: both current popen sites pass
    symmetric values (DEVNULL/DEVNULL or None/None), so the site goldens
    above cannot distinguish a swapped or aliased forwarding bug. Distinct
    values here can."""
    captured: dict[str, object] = {}

    def fake_popen(*args, **kwargs):
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    popen_correction_play("/tmp/x.wav", stdout=subprocess.DEVNULL, stderr=None)
    assert captured["kwargs"] == {"stdout": subprocess.DEVNULL, "stderr": None}


async def test_exec_forwards_stdout_and_stderr_independently(monkeypatch) -> None:
    """Same forwarding proof for the asyncio wrapper (same rationale)."""
    import asyncio

    captured: dict[str, object] = {}

    async def fake_exec(*args, **kwargs):
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await exec_correction_play("/tmp/x.wav", stdout=None, stderr=subprocess.DEVNULL)
    assert captured["kwargs"] == {"stdout": None, "stderr": subprocess.DEVNULL}


# ---------------------------------------------------------------------------
# Check 2 — the conventions guard: no new inline aplay/-D spawn shapes.
# ---------------------------------------------------------------------------

# Files allowed to contain aplay/-D spawn shapes, pinned by COUNT so a new
# spawn added to an allowlisted file trips the guard too. Every entry has a
# reason; adding one requires the same.
_ALLOWED_APLAY_SPAWN_SITES = {
    # The owner: correction_play_argv's two order branches.
    "jasper/audio_measurement/correction_lane.py": 2,
    # The heavier shared machinery (play_wav's one-shot spawn + TonePlayer's
    # continuous-tone spawn). Its alsa_device is a required caller parameter
    # — the policy-free neutral leaf, pinned by
    # tests/test_audio_measurement_playback.py::
    # test_neutral_surface_requires_owner_policy — so these are parameterized
    # spawns, not inline correction-lane spawns.
    "jasper/audio_measurement/playback.py": 2,
    # Renderer-DEVICE resolvability probe (plays /dev/zero on renderer pcms
    # such as shairport_substream — never the correction lane).
    "jasper/cli/doctor/renderers.py": 1,
}


def _aplay_spawn_shapes(path: Path) -> list[int]:
    """Line numbers of aplay/-D spawn shapes in ``path``.

    A "spawn shape" is any list/tuple display, or any call's direct
    positional arguments, whose string constants include BOTH exact
    ``"aplay"`` and exact ``"-D"``. That catches the three real shapes —
    ``Popen([...])`` / ``subprocess.run([...])`` argv lists, wrapper calls
    fed literal tuples (``_run(("aplay", ...), ...)``), and
    ``create_subprocess_exec("aplay", "-D", ...)`` varargs — while prose,
    listing probes (no ``-D``), and ``-D``-with-a-variable-binary lists
    (no exact ``"aplay"``) do not match. Matched by AST constant value,
    same technique as tests/test_correction_substream_ssot.py.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)):
            elements = node.elts
        elif isinstance(node, ast.Call):
            elements = node.args
        else:
            continue
        values = {
            element.value
            for element in elements
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        }
        if {"aplay", "-D"} <= values:
            hits.append(node.lineno)
    return hits


def test_no_inline_aplay_spawn_shapes_outside_the_allowlist() -> None:
    offenders: list[str] = []
    for path in sorted(JASPER_ROOT.rglob("*.py")):
        rel = str(path.relative_to(REPO_ROOT))
        hits = _aplay_spawn_shapes(path)
        allowed = _ALLOWED_APLAY_SPAWN_SITES.get(rel, 0)
        if len(hits) != allowed:
            offenders.append(f"{rel}: lines {hits} (allowlisted count: {allowed})")
    assert not offenders, (
        "inline aplay/-D spawn shape(s) drifted from the allowlist. A new "
        "correction-lane play site must call correction_play_argv / "
        "popen_correction_play / exec_correction_play from "
        "jasper.audio_measurement.correction_lane instead of spelling its "
        "own argv (P6c-0); a genuinely other-device spawn gets an "
        "allowlist entry here WITH a reason:\n" + "\n".join(offenders)
    )


# ---------------------------------------------------------------------------
# Check 3 — the guard proves itself on synthetic offenders and non-offenders.
# ---------------------------------------------------------------------------


def test_guard_detects_the_three_spawn_shapes(tmp_path) -> None:
    offender = tmp_path / "offender.py"
    offender.write_text(
        "import asyncio\n"
        "import subprocess\n"
        "DEV = 'somewhere'\n"
        "\n"
        "def popen_shape(p):\n"
        "    # Deliberately no '-q': the guard keys on 'aplay' + '-D' only,\n"
        "    # and this line fails the self-test if the rule over-tightens.\n"
        "    return subprocess.Popen(['aplay', '-D', DEV, str(p)])\n"
        "\n"
        "def wrapper_tuple_shape(run, p):\n"
        "    run(('aplay', '-q', '-D', DEV, str(p)), timeout=5)\n"
        "\n"
        "async def varargs_shape(p):\n"
        "    return await asyncio.create_subprocess_exec(\n"
        "        'aplay', '-D', DEV, '-q', str(p),\n"
        "    )\n"
    )
    assert len(_aplay_spawn_shapes(offender)) == 3


def test_guard_ignores_prose_probes_and_helper_built_argv(tmp_path) -> None:
    clean = tmp_path / "clean.py"
    clean.write_text(
        '"""Docstring: verify with `aplay -D correction_substream x.wav`."""\n'
        "import subprocess\n"
        "from jasper.audio_measurement.correction_lane import correction_play_argv\n"
        "# comment: aplay -D somewhere\n"
        "\n"
        "def listing_probe():\n"
        "    return subprocess.run(['aplay', '-l'], capture_output=True)\n"
        "\n"
        "def ring_probe(tool, pcm):\n"
        "    return subprocess.run([tool, '-D', pcm, '/dev/zero'])\n"
        "\n"
        "def migrated_site(p):\n"
        "    return subprocess.Popen(correction_play_argv(p))\n"
    )
    assert _aplay_spawn_shapes(clean) == []
