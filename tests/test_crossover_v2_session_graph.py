# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""One measurement graph per session — install once, prove per stimulus, put back.

The graph load/restore bracket these pin used to live per stimulus in
``program_playback.play_program``; wave 6b moved it here and made it
session-scoped. The restore-failure pins came with it rather than dying with the
site they happened to watch — a graph that will not come back leaves the speaker
in the measurement graph, which is exactly as loud a fact as it ever was.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging

import pytest

from jasper.active_speaker.crossover_v2.session_graph import (
    MeasurementSessionGraph,
    SessionGraphError,
)
from jasper.camilla import CamillaUnavailable

ENTRY_PATH_NAME = "entry.yml"
GRAPH = "program: graph\n"


class FakeCam:
    def __init__(self, *, entry_path, load_ok=True, load_raises=None):
        self.entry_path = entry_path
        self.load_ok = load_ok
        self.load_raises = load_raises
        self.ops: list = []
        self.ducked: list[bool] = []
        self.live: str | None = None

    async def get_config_file_path(self, *, best_effort=False):
        self.ops.append("get_path")
        return self.entry_path

    async def set_active_config_raw(self, text, *, best_effort=False, duck=True):
        self.ops.append(("set_raw", text))
        self.ducked.append(duck)
        if self.load_raises is not None:
            raise self.load_raises
        if not self.load_ok:
            return False
        self.live = text
        return True

    async def patch_config(self, patch, *, best_effort=False):
        self.ops.append(("patch", patch))
        return True


def _graph(cam, *, tmp_path, emits=None):
    emitted = emits if emits is not None else []

    def _emit() -> str:
        emitted.append(GRAPH)
        return GRAPH

    async def _confirm(c, submitted):
        if c.live != submitted:
            raise RuntimeError("graph load was not confirmed")

    @contextlib.asynccontextmanager
    async def _lock():
        cam.ops.append("lock")
        try:
            yield
        finally:
            cam.ops.append("unlock")

    graph = MeasurementSessionGraph(
        emit=_emit,
        cam_factory=lambda: cam,
        writer_lock=_lock,
        confirm_live=_confirm,
    )
    graph.emitted = emitted  # type: ignore[attr-defined]
    return graph


def _entry(tmp_path):
    path = tmp_path / ENTRY_PATH_NAME
    path.write_text("entry: graph\n", encoding="utf-8")
    return str(path)


def test_the_graph_is_emitted_once_no_matter_how_many_stimuli(tmp_path):
    """MS-13's 'once, before the first stimulus' becomes a structural fact.

    The emitter runs ``_assert_program_graph_proven`` on every call, so emitting
    once per session is what turns the proof from a scheduling promise into
    something the code cannot get wrong.
    """
    cam = FakeCam(entry_path=_entry(tmp_path))
    graph = _graph(cam, tmp_path=tmp_path)

    for _ in range(4):
        asyncio.run(graph.install())

    assert graph.emitted == [GRAPH]


def test_four_stimuli_cost_one_load_not_four(tmp_path):
    """The whole point: install is idempotent while the graph is still live."""
    cam = FakeCam(entry_path=_entry(tmp_path))
    graph = _graph(cam, tmp_path=tmp_path)

    for _ in range(4):
        asyncio.run(graph.install())

    loads = [op for op in cam.ops if isinstance(op, tuple) and op[0] == "set_raw"]
    assert len(loads) == 1


def test_a_graph_another_writer_replaced_is_put_back_not_measured_through(tmp_path):
    """Ruling S6's pipeline-health check, in ruling S10's shape.

    A concurrent DSP writer replacing the running graph must not silently
    become the graph the next stimulus measures through. Detecting it REPAIRS
    and discloses — it never refuses to play.
    """
    cam = FakeCam(entry_path=_entry(tmp_path))
    graph = _graph(cam, tmp_path=tmp_path)
    asyncio.run(graph.install())

    cam.live = "somebody: else\n"  # a /sound/ apply landed mid-session
    asyncio.run(graph.install())

    loads = [op for op in cam.ops if isinstance(op, tuple) and op[0] == "set_raw"]
    assert len(loads) == 2
    assert cam.live == GRAPH


def test_the_reinstall_is_disclosed_loudly(tmp_path, caplog):
    cam = FakeCam(entry_path=_entry(tmp_path))
    graph = _graph(cam, tmp_path=tmp_path)
    asyncio.run(graph.install())
    cam.live = "somebody: else\n"

    with caplog.at_level(logging.WARNING, logger="jasper.active_speaker.crossover_v2.session_graph"):
        asyncio.run(graph.install())

    assert "result=reinstall" in caplog.text


def test_the_entry_graph_is_snapshotted_once_not_per_install(tmp_path):
    """A reinstall must NOT re-read the entry path.

    By then the running graph is the measurement graph (or somebody else's), so
    a fresh snapshot would record the wrong thing to go back to — and on a
    ``set_active_config_raw`` path the statefile still names the real entry.
    """
    cam = FakeCam(entry_path=_entry(tmp_path))
    graph = _graph(cam, tmp_path=tmp_path)
    asyncio.run(graph.install())
    cam.live = "somebody: else\n"
    asyncio.run(graph.install())

    assert cam.ops.count("get_path") == 1


def test_an_unreadable_liveness_answer_is_never_treated_as_live(tmp_path):
    """Fail-closed on the probe, not just on the load.

    ``confirm_graph_is_live``'s two strict reads raise ``CamillaUnavailable``,
    a bare ``Exception`` subclass. If the probe swallowed that as "still live"
    the next stimulus would measure through a graph nobody proved.
    """
    cam = FakeCam(entry_path=_entry(tmp_path))
    graph = _graph(cam, tmp_path=tmp_path)
    asyncio.run(graph.install())

    async def _unavailable(_c, _submitted):
        raise CamillaUnavailable("websocket closed")

    graph._confirm_live = _unavailable  # type: ignore[assignment]
    with pytest.raises(CamillaUnavailable):
        asyncio.run(graph.install())
    # It got as far as trying to put the graph back, rather than returning a
    # cheerful fingerprint for a graph it could not see.
    loads = [op for op in cam.ops if isinstance(op, tuple) and op[0] == "set_raw"]
    assert len(loads) == 2


def test_no_entry_config_refuses_before_loading_anything(tmp_path):
    cam = FakeCam(entry_path=None)
    graph = _graph(cam, tmp_path=tmp_path)

    with pytest.raises(SessionGraphError, match="no current DSP config"):
        asyncio.run(graph.install())

    assert not [op for op in cam.ops if isinstance(op, tuple) and op[0] == "set_raw"]


def test_neither_swap_ducks_the_fader(tmp_path):
    """Wave 6d: the measurement-swap duck goes.

    The 40 dB / ``MAIN_VOLUME_RAMP_SETTLE_S`` bracket exists because replacing
    the pipeline under live household audio can step the graph's own gain by
    tens of dB at an unchanged volume. Neither condition holds for this graph:
    the session already claimed the fader at its declared measurement level and
    holds the measurement window, so nothing is playing for a step to be loud
    against — the ramp was 0.94 s per swapping stimulus spent on silence.

    Scope is the MEASUREMENT path only. ``/sound/`` and ``/correction/`` apply
    keep their duck; ``test_camilla_controller.py`` pins that they still do.
    """
    cam = FakeCam(entry_path=_entry(tmp_path))
    graph = _graph(cam, tmp_path=tmp_path)

    asyncio.run(graph.install())
    asyncio.run(graph.restore())

    assert cam.ducked == [False, False]


def test_both_swaps_ride_setconfig_and_never_repoint_the_statefile(tmp_path):
    """The crash-recovery-MUTED invariant, moved here with the swap it guards.

    Install and restore both ride ``set_active_config_raw`` (CamillaDSP
    ``SetConfig``), never ``set_config_file_path``. The persisted config path
    keeps naming the staged all-muted anchor, so a crash or reboot anywhere in a
    measurement session comes back muted rather than into the measurement graph
    — which is now live for the whole session rather than for one stimulus, so
    the invariant matters MORE than it did, not less.
    """
    calls: list[str] = []

    class _RecordingCam(FakeCam):
        async def set_config_file_path(self, path, *, best_effort=False):
            calls.append("set_config_file_path")
            return True

        async def set_active_config_raw(
            self, text, *, best_effort=False, duck=True,
        ):
            calls.append("set_active_config_raw")
            return await super().set_active_config_raw(
                text, best_effort=best_effort, duck=duck,
            )

    cam = _RecordingCam(entry_path=_entry(tmp_path))
    graph = _graph(cam, tmp_path=tmp_path)
    asyncio.run(graph.install())
    asyncio.run(graph.restore())

    assert calls == ["set_active_config_raw", "set_active_config_raw"]
    assert "set_config_file_path" not in calls


def test_restore_puts_the_entry_graph_back_and_is_a_noop_twice(tmp_path):
    entry = _entry(tmp_path)
    cam = FakeCam(entry_path=entry)
    graph = _graph(cam, tmp_path=tmp_path)
    asyncio.run(graph.install())

    asyncio.run(graph.restore())
    assert cam.live == "entry: graph\n"

    before = len(cam.ops)
    asyncio.run(graph.restore())
    assert cam.ops[before:] == []  # idempotent: nothing held, nothing written


def test_an_install_that_raised_still_leaves_restore_able_to_put_it_back(tmp_path):
    """The seam's explicit contract for a half-run install.

    ``SessionGraph.install`` promises that "an install that raises after arming
    half a graph must leave ``restore`` able to put that half back". The entry
    snapshot is therefore taken BEFORE the load, so a load that fails partway
    still leaves a target to go back to.
    """
    cam = FakeCam(entry_path=_entry(tmp_path), load_ok=False)
    graph = _graph(cam, tmp_path=tmp_path)

    with pytest.raises(SessionGraphError, match="was not confirmed"):
        asyncio.run(graph.install())

    assert graph.installed is True
    cam.load_ok = True
    asyncio.run(graph.restore())
    assert cam.live == "entry: graph\n"


def test_restore_without_an_install_is_a_noop(tmp_path):
    """Every drain path calls restore without asking whether anything is held."""
    cam = FakeCam(entry_path=_entry(tmp_path))
    graph = _graph(cam, tmp_path=tmp_path)
    asyncio.run(graph.restore())
    assert cam.ops == []


def test_an_unavailable_dsp_during_restore_raises_a_named_error_and_says_so(
    tmp_path, caplog
):
    """Re-pointed from ``play_program._safe_restore`` (wave 6a).

    ``set_active_config_raw(best_effort=False)`` raises ``CamillaUnavailable``,
    which is not an ``OSError`` subclass — the failure mode that used to escape
    the restore's own catch set. A restore that cannot put the entry graph back
    leaves the speaker in the measurement graph, so it is CRITICAL and named,
    never a bare raise nobody logged.
    """
    entry = _entry(tmp_path)
    cam = FakeCam(entry_path=entry)
    graph = _graph(cam, tmp_path=tmp_path)
    asyncio.run(graph.install())
    cam.load_raises = CamillaUnavailable("websocket closed")

    with caplog.at_level(logging.CRITICAL, logger="jasper.active_speaker.crossover_v2.session_graph"):
        with pytest.raises(SessionGraphError, match="websocket closed"):
            asyncio.run(graph.restore())

    assert "action=restore" in caplog.text
    assert "result=failed" in caplog.text


def test_a_rejected_restore_is_a_different_line_from_a_raised_one(tmp_path, caplog):
    entry = _entry(tmp_path)
    cam = FakeCam(entry_path=entry)
    graph = _graph(cam, tmp_path=tmp_path)
    asyncio.run(graph.install())
    cam.load_ok = False

    with caplog.at_level(logging.CRITICAL, logger="jasper.active_speaker.crossover_v2.session_graph"):
        with pytest.raises(SessionGraphError, match="could not be restored"):
            asyncio.run(graph.restore())

    assert "result=rejected" in caplog.text


def test_a_failed_restore_does_not_leave_the_session_owning_a_graph(tmp_path):
    """A second drain must not re-enter the same failing path.

    Every drain arm calls restore; if the first failure left the entry path
    held, the close arm's error would be replaced by the abandon arm's identical
    one and the original cause would be lost.
    """
    cam = FakeCam(entry_path=_entry(tmp_path))
    graph = _graph(cam, tmp_path=tmp_path)
    asyncio.run(graph.install())
    cam.load_raises = CamillaUnavailable("websocket closed")

    with pytest.raises(SessionGraphError):
        asyncio.run(graph.restore())

    assert graph.installed is False
    cam.load_raises = None
    asyncio.run(graph.restore())  # no second raise


def test_patch_refuses_before_there_is_a_graph_to_patch(tmp_path):
    cam = FakeCam(entry_path=_entry(tmp_path))
    graph = _graph(cam, tmp_path=tmp_path)

    with pytest.raises(SessionGraphError, match="no measurement graph"):
        asyncio.run(graph.patch({"filters": {}}))


def test_patch_changes_one_candidate_without_reinstalling(tmp_path):
    cam = FakeCam(entry_path=_entry(tmp_path))
    graph = _graph(cam, tmp_path=tmp_path)
    asyncio.run(graph.install())
    before = len([op for op in cam.ops if isinstance(op, tuple) and op[0] == "set_raw"])

    asyncio.run(graph.patch({"filters": {"gain": {}}}))

    after = len([op for op in cam.ops if isinstance(op, tuple) and op[0] == "set_raw"])
    assert after == before
    assert ("patch", {"filters": {"gain": {}}}) in cam.ops
