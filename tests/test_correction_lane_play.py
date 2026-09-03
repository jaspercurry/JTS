# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contract: one owner for the correction-lane ``aplay`` spawn (P6c-0/P6c-i).

``jasper.audio_measurement.correction_lane`` owns building and running the
``aplay`` command line that plays a WAV onto the correction lane — the same
consolidation ``tests/test_correction_substream_ssot.py`` enforces for the
lane *name*, applied to the lane *spawn*. Before P6c-0 ten call sites
across six files each assembled their own inline argv; the P6c lane
migration (campaign #2285, U3 arc) would have meant ten synchronized edits.
P6c-i retired P6c-0's per-site argv-order knob (one true argv now) and made
every wrapper spawn under ``CORRECTION_PLAY_UMASK`` — the ring-file mode
policy that becomes load-bearing when P6c-ii arms the lane.

Four groups of checks:

  1. **Golden argv/kwargs.** For every site shape, the helper must produce
     exactly the argv and subprocess kwargs that site's callers rely on:
     the P6c-0 shapes were derived from ``origin/main`` @ ``bb55691a1``'s
     inline spawns, and P6c-i's two deliberate deltas on top are the
     normalized flag order and the added ``umask`` (each golden names
     what it pins).
  2. **The umask mechanism.** Real spawns prove ``umask=`` reaches the
     child and governs file creation for BOTH stdlib APIs the wrappers
     call — including that it overrides a more-restrictive inherited
     umask, which is what lets jasper-correction-web keep its tight
     unit-level ``UMask=0077``. The goldens pin that the wrappers pass the
     constant; these pin that passing it works.
  3. **The conventions guard.** No file under ``jasper/`` outside a small,
     count-pinned allowlist may contain an ``aplay``/``-D`` spawn shape —
     this is what fails when someone adds an eleventh inline site instead
     of calling the helper. Scope is ``jasper/`` only: the four
     stdlib-only lab probe scripts under ``scripts/`` spell the command by
     design (see correction_lane.py's "Scope of the drift guard" docstring
     section — same exemption, same reason).
  4. **The guard proves itself.** Synthetic offender shapes are detected;
     prose, listing probes (``aplay -l``/``-L``), helper-built argv, and
     ``-D``-without-``aplay`` lists are not.

Deliberate non-guards (static shape scan, aimed at the accidental new
site, not adversarial evasion): a variable binary (``[self.aplay_binary,
...]`` — jasper.active_speaker.playback's audio-lab backend, which has its
own FORBIDDEN_TEST_PCM_TOKENS fence), argv assembled by concatenation
(``["aplay"] + rest``), and ``shell=True`` command strings. None exist in
``jasper/`` for the correction lane as of P6c-i — verified by sweep and by
the P6c-0 gate's independent re-derivation, NOT self-enforcing (those
shapes are exactly what the scan cannot see).
"""
from __future__ import annotations

import ast
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from jasper import renderer_lanes as rl
from jasper.audio_measurement.correction_lane import (
    CORRECTION_PLAY_UMASK,
    CORRECTION_SUBSTREAM,
    correction_play_argv,
    correction_play_device,
    exec_correction_play,
    popen_correction_play,
    run_correction_play,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
JASPER_ROOT = REPO_ROOT / "jasper"


@pytest.fixture(autouse=True)
def _hermetic_lane_map(monkeypatch, tmp_path):
    """Every test here starts in the fleet-default transport state.

    The device is lane-map-aware since P6c-ii, and the map path is the
    PRODUCTION file — a test run on a box whose operator armed the lane
    would otherwise flip every unarmed golden. Point the reader at a
    per-test path (no file = nothing armed); tests that arm write their
    own map there.
    """
    monkeypatch.setattr(
        rl, "RENDERER_LANES_ENV", str(tmp_path / "renderer_lanes.env")
    )


# ---------------------------------------------------------------------------
# Check 1 — golden argv/kwargs. P6c-0 shapes derived from origin/main @
# bb55691a1; P6c-i's two deliberate deltas are the normalized flag order
# (P6c-0's per-site knob retired) and the umask on every spawn.
# ---------------------------------------------------------------------------


def test_builder_produces_the_one_true_argv() -> None:
    """``aplay -D <lane> -q <wav>`` — every caller, one order (P6c-i).

    P6c-0 preserved two historical orders behind ``quiet_before_device``
    (web family ``-D … -q``, root-CLI family ``-q -D``; identical to
    ``aplay``). P6c-i retired the knob onto the web-family order — the
    normalization the P6c-0 gate banked — so the builder takes no order
    argument at all: a reintroduced per-site order knob fails here.
    """
    assert correction_play_argv("/tmp/marker.wav") == [
        "aplay", "-D", CORRECTION_SUBSTREAM, "-q", "/tmp/marker.wav",
    ]
    import inspect

    assert list(inspect.signature(correction_play_argv).parameters) == [
        "wav_path"
    ], "the argv builder takes exactly one parameter since P6c-i"


def test_builder_stringifies_path_objects() -> None:
    """Sites passed str(Path) inline; the builder owns that conversion now."""
    assert correction_play_argv(Path("/tmp/x.wav"))[-1] == "/tmp/x.wav"


def test_device_follows_the_lane_map(tmp_path) -> None:
    """The P6c-ii flip, end to end through a real map file: unarmed (the
    fleet default — no map) resolves the aloop alias; an armed map resolves
    the ring plug PCM, on the very next call in the same process (nothing
    caches — the writers are ephemeral, so a restart cannot deliver an
    arm); disarming flips back. The argv builder rides the same answer.

    Devices are asserted through the registry row, which is where both
    names are declared (aloop_device imports the lane-name SSOT;
    ring_device is cross-pinned against the conf.d by the lane tests).
    """
    lane = rl.lane_by_label("correction")
    assert lane is not None
    map_path = Path(rl.RENDERER_LANES_ENV)  # per-test path via the fixture

    assert correction_play_device() == lane.aloop_device == CORRECTION_SUBSTREAM
    assert correction_play_argv("/tmp/x.wav")[2] == lane.aloop_device

    map_path.write_text(rl.render_env_text((lane.label,)))
    assert correction_play_device() == lane.ring_device
    assert correction_play_argv("/tmp/x.wav")[2] == lane.ring_device

    map_path.write_text(rl.render_env_text(()))
    assert correction_play_device() == lane.aloop_device


def test_lane_umask_constant_is_group_write() -> None:
    """0o007 = owner+group rwx preserved, other cleared: a ring created
    0o660 stays writable by the non-creating end (fan-in or the writer),
    which both write the shared header. The constant's comment in
    correction_lane.py carries the full story; this pins the value the
    goldens below show every wrapper passing."""
    assert CORRECTION_PLAY_UMASK == 0o007


def test_popen_wizard_shape(monkeypatch) -> None:
    """Popen(argv, stdout=DEVNULL, stderr=DEVNULL, umask) — the wizard shape.

    Argv + stdio derived from (origin/main @ bb55691a1) sound_setup's three
    Popen sites and web_commissioning's commission-tone Popen; ``umask`` is
    P6c-i's deliberate addition to the spawn kwargs.
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
        "umask": CORRECTION_PLAY_UMASK,
    }


def test_popen_operator_cli_shape(monkeypatch) -> None:
    """Popen(argv, stdout=None, stderr=None, umask) — the inherit-stdio shape.

    ``None`` stdio is Popen's documented default (inherit — aplay's stderr
    stays on a foreground operator's terminal); asserted so a future edit
    that starts redirecting it fails this golden. Argv order is the P6c-i
    normalized one — the pre-P6c-i site spelled ``-q`` first, a
    semantically identical order this golden deliberately no longer pins.
    """
    captured: dict[str, object] = {}

    def fake_popen(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    popen_correction_play("/tmp/noise.wav", stdout=None, stderr=None)
    assert captured["args"] == (
        ["aplay", "-D", CORRECTION_SUBSTREAM, "-q", "/tmp/noise.wav"],
    )
    assert captured["kwargs"] == {
        "stdout": None,
        "stderr": None,
        "umask": CORRECTION_PLAY_UMASK,
    }


async def test_exec_walkthrough_shape(monkeypatch) -> None:
    """create_subprocess_exec("aplay", …, DEVNULL×2, umask) — the async shape.

    Argv + stdio derived from (origin/main @ bb55691a1) the identical
    ``_start_playback`` helpers in sync_flow and balance_flow: program+args
    as separate positionals, both stdio DEVNULL; ``umask`` is P6c-i's
    addition, forwarded through asyncio to the underlying Popen.
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
        "umask": CORRECTION_PLAY_UMASK,
    }


def test_run_cli_shape(monkeypatch) -> None:
    """run(argv, capture_output=True, text=True, timeout, umask) — the
    blocking root-CLI shape (P6c-i).

    Derived from the module-local ``_run`` wrappers the two CLI sites used
    through P6c-0 — aec_commission's ``subprocess.run(list(command),
    capture_output=True, text=True, timeout=timeout)`` and doctor
    ``_shared._run``'s identical call — which the helper reproduces
    byte-for-byte plus the umask, so each site's error policy keeps reading
    the same CompletedProcess fields. ``timeout`` is required: no caller
    gets an unbounded default.
    """
    captured: dict[str, object] = {}
    sentinel = object()

    def fake_run(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return sentinel

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = run_correction_play("/tmp/sine.wav", timeout=6.5)
    assert result is sentinel
    assert captured["args"] == (
        ["aplay", "-D", CORRECTION_SUBSTREAM, "-q", "/tmp/sine.wav"],
    )
    assert captured["kwargs"] == {
        "capture_output": True,
        "text": True,
        "timeout": 6.5,
        "umask": CORRECTION_PLAY_UMASK,
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
    assert captured["kwargs"] == {
        "stdout": subprocess.DEVNULL,
        "stderr": None,
        "umask": CORRECTION_PLAY_UMASK,
    }


async def test_exec_forwards_stdout_and_stderr_independently(monkeypatch) -> None:
    """Same forwarding proof for the asyncio wrapper (same rationale)."""
    import asyncio

    captured: dict[str, object] = {}

    async def fake_exec(*args, **kwargs):
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await exec_correction_play("/tmp/x.wav", stdout=None, stderr=subprocess.DEVNULL)
    assert captured["kwargs"] == {
        "stdout": None,
        "stderr": subprocess.DEVNULL,
        "umask": CORRECTION_PLAY_UMASK,
    }


# ---------------------------------------------------------------------------
# Check 2 — the umask mechanism: real spawns, real files, real modes.
# The goldens above pin that every wrapper PASSES the constant; these pin
# that passing it WORKS — through both stdlib APIs the wrappers call — and
# that it overrides a more-restrictive inherited umask (the
# jasper-correction-web UMask=0077 case, where the unit stays tight and only
# the spawned player is loosened).
# ---------------------------------------------------------------------------

_CHILD_CREATES_FILE = (
    "import sys, os; os.close(os.open(sys.argv[1], "
    "os.O_CREAT | os.O_WRONLY, 0o666))"
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_popen_umask_governs_child_file_creation(tmp_path) -> None:
    """subprocess.Popen(umask=…) sets the CHILD's umask: a 0o666 open lands
    0o660 under 0o007 (group bit KEPT even though this pytest process's own
    umask is almost certainly 0o022, proving the override) and 0o600 under
    0o077 (proving the parameter is live in both directions, i.e. the
    tight-unit case cannot leak INTO the spawn either)."""
    loose = tmp_path / "loose.bin"
    proc = subprocess.Popen(
        [sys.executable, "-c", _CHILD_CREATES_FILE, str(loose)],
        umask=0o007,
    )
    assert proc.wait(timeout=30) == 0
    assert _mode(loose) == 0o660

    tight = tmp_path / "tight.bin"
    proc = subprocess.Popen(
        [sys.executable, "-c", _CHILD_CREATES_FILE, str(tight)],
        umask=0o077,
    )
    assert proc.wait(timeout=30) == 0
    assert _mode(tight) == 0o600


async def test_asyncio_forwards_umask_to_the_child(tmp_path) -> None:
    """asyncio.create_subprocess_exec forwards unknown keywords to the
    underlying Popen — the fact exec_correction_play's docstring claims.
    Proven with a real spawn rather than trusted from the stdlib source:
    a wrong claim here would silently strip the lane's umask from every
    walkthrough spawn."""
    import asyncio

    out = tmp_path / "async.bin"
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _CHILD_CREATES_FILE,
        str(out),
        umask=0o007,
    )
    assert await asyncio.wait_for(proc.wait(), timeout=30) == 0
    assert _mode(out) == 0o660


# ---------------------------------------------------------------------------
# Check 2 — the conventions guard: no new inline aplay/-D spawn shapes.
# ---------------------------------------------------------------------------

# Files allowed to contain aplay/-D spawn shapes, pinned by COUNT so a new
# spawn added to an allowlisted file trips the guard too. Every entry has a
# reason; adding one requires the same.
_ALLOWED_APLAY_SPAWN_SITES = {
    # The owner: correction_play_argv's single argv display (P6c-i retired
    # the second order branch along with the quiet_before_device knob).
    "jasper/audio_measurement/correction_lane.py": 1,
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
