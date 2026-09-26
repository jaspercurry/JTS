# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contract: one owner for the correction-lane ``aplay`` spawn.

``jasper.audio_measurement.correction_lane`` owns building and running the
``aplay`` command line that plays a WAV onto the correction lane — the same
consolidation ``tests/test_correction_substream_ssot.py`` enforces for the
lane *name*, applied to the lane *spawn*. For every site shape, the helper
must produce exactly the argv and subprocess kwargs that site's callers rely
on.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from jasper.audio_measurement.correction_lane import (
    CORRECTION_SUBSTREAM,
    correction_play_argv,
    correction_play_device,
    exec_correction_play,
    popen_correction_play,
    run_correction_play,
)


def test_builder_produces_the_one_true_argv() -> None:
    """``aplay -D <lane> -q <wav>`` — every caller, one order.

    The builder takes no order argument at all: a reintroduced per-site
    order knob fails here.
    """
    assert correction_play_argv("/tmp/marker.wav") == [
        "aplay", "-D", CORRECTION_SUBSTREAM, "-q", "/tmp/marker.wav",
    ]
    import inspect

    assert list(inspect.signature(correction_play_argv).parameters) == [
        "wav_path"
    ], "the argv builder takes exactly one parameter"


def test_builder_stringifies_path_objects() -> None:
    """Sites passed str(Path) inline; the builder owns that conversion now."""
    assert correction_play_argv(Path("/tmp/x.wav"))[-1] == "/tmp/x.wav"


def test_device_is_the_aloop_lane() -> None:
    """The correction lane's PCM, and the argv builder riding the same answer."""
    assert correction_play_device() == CORRECTION_SUBSTREAM
    assert correction_play_argv("/tmp/x.wav")[2] == CORRECTION_SUBSTREAM


def test_popen_wizard_shape(monkeypatch) -> None:
    """Popen(argv, stdout=DEVNULL, stderr=DEVNULL) — the wizard shape."""
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


def test_popen_operator_cli_shape(monkeypatch) -> None:
    """Popen(argv, stdout=None, stderr=None) — the inherit-stdio shape.

    ``None`` stdio is Popen's documented default (inherit — aplay's stderr
    stays on a foreground operator's terminal); asserted so a future edit
    that starts redirecting it fails this golden.
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
    }


async def test_exec_walkthrough_shape(monkeypatch) -> None:
    """create_subprocess_exec("aplay", …, DEVNULL×2) — the async shape.

    Program+args as separate positionals, both stdio DEVNULL.
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


def test_run_cli_shape(monkeypatch) -> None:
    """run(argv, capture_output=True, text=True, timeout) — the blocking
    root-CLI shape.

    Reproduces the two CLI sites' own ``subprocess.run`` call byte for
    byte, so each site's error policy keeps reading the same
    CompletedProcess fields. ``timeout`` is required: no caller gets an
    unbounded default.
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
    }
