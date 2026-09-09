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
import hashlib
import logging
from pathlib import Path

import pytest
import yaml

from jasper.active_speaker import program_playback
from jasper.active_speaker.crossover_v2.composition import bind_program_playback_seams
from jasper.active_speaker.crossover_v2.session_graph import (
    MeasurementSessionGraph,
    SessionGraphError,
    temporary_graph_anchor,
)
from jasper.active_speaker.crossover_v2.tuning_scope import COMPARABILITY_BOUNDARY
from jasper.camilla import CamillaUnavailable
from jasper.sound.profile import build_sound_filters
from tests.test_crossover_v2_tuning_scope import FLAT, SAVED, household_graph
from tests.test_crossover_v2_tuning_scope import tuning_profile as tuning_profile

ENTRY_PATH_NAME = "entry.yml"
GRAPH = "program: graph\n"
_LOGGER = "jasper.active_speaker.crossover_v2.session_graph"


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

    async def normalize_config_raw(self, text, *, best_effort=False):
        return text


def _graph(cam, *, tmp_path, emits=None, emit_scoped=None):
    emitted = emits if emits is not None else []

    def _emit(
        inverted_roles=(), measurement_delays_us=None, level_trims_db=None,
    ) -> str:
        # One distinct text per measurement variant, the way the real emitter
        # produces one: a flipped mixer is different bytes, so is a Delay
        # filter carrying a different coordinate, and so is a mixer source
        # carrying a different gain.
        text = GRAPH
        if inverted_roles:
            text += f"inverted: {inverted_roles}\n"
        if measurement_delays_us:
            text += f"delays: {sorted(measurement_delays_us.items())}\n"
        if level_trims_db:
            text += f"trims: {sorted(level_trims_db.items())}\n"
        emitted.append(text)
        return text

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
        emit_scoped=emit_scoped,
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

    Scope is the MEASUREMENT path only. The ``/sound/`` apply keeps its duck;
    ``test_camilla_controller.py`` pins that it still does.
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
    Path(entry).write_text("entry: rewritten\n")

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


@pytest.mark.parametrize("failure", ["unavailable", "not_live"])
def test_a_failed_restore_retains_the_entry_graph_for_retry(tmp_path, failure):
    cam = FakeCam(entry_path=_entry(tmp_path))
    graph = _graph(cam, tmp_path=tmp_path)
    asyncio.run(graph.install())
    original_confirm = graph._confirm_live
    if failure == "unavailable":
        cam.load_raises = CamillaUnavailable("websocket closed")
    else:
        async def not_live(_cam, _text):
            raise RuntimeError("entry graph is not live")
        graph._confirm_live = not_live

    with pytest.raises(SessionGraphError):
        asyncio.run(graph.restore())

    assert graph.installed is True
    Path(cam.entry_path).unlink()
    cam.load_raises = None
    graph._confirm_live = original_confirm
    asyncio.run(graph.restore())
    assert graph.installed is False
    assert cam.live == "entry: graph\n"


def test_scoped_graphs_have_distinct_cached_identities_and_one_entry_snapshot(tmp_path):
    emitted = []

    def emit_scoped(scope, candidate_id):
        emitted.append((scope, candidate_id))
        return f"scope: {scope}\ncandidate: {candidate_id}\n"

    cam = FakeCam(entry_path=_entry(tmp_path))
    graph = _graph(cam, tmp_path=tmp_path, emit_scoped=emit_scoped)
    with pytest.raises(SessionGraphError):
        graph.installed_graph_yaml()
    for named in ("candidate", "room_candidate"):
        with pytest.raises(SessionGraphError):
            graph.select_scope(named, "")
    scopes = [
        ("drivers", ""), ("base", ""), ("speaker_tune", ""),
        ("candidate", "a"), ("candidate", "b"), ("room_candidate", "a"),
    ]
    fingerprints = {}
    for scope, candidate_id in scopes * 2:
        graph.select_scope(scope, candidate_id)
        fingerprint = asyncio.run(graph.install())
        assert fingerprint == fingerprints.setdefault((scope, candidate_id), fingerprint)
        assert graph.installed_graph_yaml() == cam.live
        emitted_graph = yaml.safe_load(graph.graph_yaml())
        submitted_graph = yaml.safe_load(cam.live)
        if scope != "drivers":
            submitted_graph.pop("description")
        assert emitted_graph == submitted_graph
    assert len(set(fingerprints.values())) == len(scopes)
    assert emitted == scopes[1:]
    assert cam.ops.count("get_path") == 1
    cam.live = "other: graph\n"
    asyncio.run(graph.restore())
    assert cam.live == "entry: graph\n"
    with pytest.raises(SessionGraphError):
        graph.installed_graph_yaml()


@pytest.mark.parametrize("scope", ["base", "speaker_tune", "candidate"])
@pytest.mark.parametrize("change", ["none", "path", "anchor", "live", "unmarked"])
async def test_scoped_startup_recovery_matches_real_graph_and_retained_anchor(
    tmp_path, tuning_profile, scope, change, monkeypatch,
):
    from jasper.active_speaker.measurement_emit import compile_tuning_graph
    from jasper.web import correction_runtime, correction_setup
    from jasper import dsp_apply
    from tests.test_crossover_v2_tuning_scope import _trial_candidate

    text = compile_tuning_graph(
        tuning_profile, scope="base" if scope == "candidate" else scope,
        candidate=_trial_candidate(tuning_profile) if scope == "candidate" else None,
    )
    cam = FakeCam(entry_path=_entry(tmp_path))

    async def live(**_kwargs):
        return await normalize(cam.live)

    async def normalize(text, **_kwargs):
        normalized = yaml.safe_load(text)
        normalized.setdefault("title", None)
        return yaml.safe_dump(normalized)

    cam.get_active_config_raw = live
    cam.normalize_config_raw = normalize
    graph = _graph(cam, tmp_path=tmp_path, emit_scoped=lambda *_: text)
    graph.select_scope(scope, "candidate" if scope == "candidate" else "")
    fingerprint = await graph.install()
    assert graph.graph_yaml() == text
    played = object()

    async def play_wav(*_args, **_kwargs):
        return played

    monkeypatch.setattr(program_playback, "verified_program_aplay", play_wav)
    playback = bind_program_playback_seams(
        cam, bundle_dir=str(tmp_path), artifact=object(), config_dir=str(tmp_path),
        program=object(), wav_path=str(tmp_path / "program.wav"), topology=object(),
        safety_profile={}, role_targets={}, session_volume_db=-30.0,
        graph_yaml=graph.installed_graph_yaml(),
    )
    assert await playback["play_wav"]() is played
    assert await temporary_graph_anchor(cam, await live()) == Path(cam.entry_path)
    if change == "path":
        Path(cam.entry_path).unlink()
        cam.entry_path = str(tmp_path / "later.yml")
    elif change == "anchor":
        Path(cam.entry_path).write_text("entry: changed\n")
    elif change == "live":
        changed = yaml.safe_load(cam.live)
        changed["filters"]["as_tweeter_baseline_gain"]["parameters"]["gain"] = -30.0
        cam.live = yaml.safe_dump(changed)
    elif change == "unmarked":
        cam.live = text
    before = cam.live

    @contextlib.asynccontextmanager
    async def lock(*_args, **_kwargs):
        yield

    monkeypatch.setattr(dsp_apply, "dsp_writer_lock", lock)
    monkeypatch.setattr(correction_runtime, "camilla_controller", lambda: cam)
    await correction_setup._restore_protected_neutral_program_graph()
    assert cam.live == ("entry: graph\n" if change == "none" else before)
    assert fingerprint == hashlib.sha256(text.encode()).hexdigest()[:16]


def test_scope_refusal_cannot_load_an_unrequested_graph(tmp_path):
    cam = FakeCam(entry_path=_entry(tmp_path))
    graph = _graph(cam, tmp_path=tmp_path)
    with pytest.raises(SessionGraphError):
        graph.select_scope("base")
    assert cam.ops == []


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


# --------------------------------------------------------------------------- #
# R-1 — one graph per POLARITY VARIANT, and the entry graph survives the swap
# --------------------------------------------------------------------------- #


def test_a_polarity_variant_is_its_own_graph_with_its_own_fingerprint(tmp_path):
    """The record's provenance must name the graph the stimulus played through.

    A variant that reused the normal graph's fingerprint would point every
    reverse-null record at its non-inverted twin.
    """
    cam = FakeCam(entry_path=_entry(tmp_path))
    graph = _graph(cam, tmp_path=tmp_path)

    normal = asyncio.run(graph.install())
    flipped = asyncio.run(graph.install(("tweeter",)))

    assert graph.installed_graph_yaml() == cam.live
    assert graph.installed_graph_yaml() != graph.graph_yaml()
    assert normal and flipped and normal != flipped
    assert len(graph.emitted) == 2, "one emit per variant"


def test_a_delay_coordinate_is_its_own_graph_with_its_own_fingerprint(tmp_path):
    """R-1's other half. The confirmation plays three coordinates in a row, so a
    cache keyed on polarity alone would hand the second one the first's graph
    and measure a delay nobody asked for — under a fingerprint naming the wrong
    coordinate."""
    cam = FakeCam(entry_path=_entry(tmp_path))
    graph = _graph(cam, tmp_path=tmp_path)

    first = asyncio.run(graph.install(("tweeter",), {"tweeter": 100.0}))
    second = asyncio.run(graph.install(("tweeter",), {"tweeter": 200.0}))
    flipped_only = asyncio.run(graph.install(("tweeter",)))

    assert len({first, second, flipped_only}) == 3
    assert len(graph.emitted) == 3, "one emit per delay variant"


def test_a_level_match_is_its_own_graph_with_its_own_fingerprint(tmp_path):
    """The third variant axis. A level-matched capture and its unmatched twin
    differ ONLY in the mixer gains, so a cache that did not key on them would
    serve the untrimmed graph and bank a record claiming a level match that
    never played."""
    cam = FakeCam(entry_path=_entry(tmp_path))
    graph = _graph(cam, tmp_path=tmp_path)

    unmatched = asyncio.run(graph.install(("tweeter",)))
    matched = asyncio.run(graph.install(("tweeter",), None, {"tweeter": -9.5}))
    deeper = asyncio.run(graph.install(("tweeter",), None, {"tweeter": -10.5}))

    assert len({unmatched, matched, deeper}) == 3
    assert len(graph.emitted) == 3, "one emit per level-match variant"


def test_the_same_level_match_twice_is_emitted_once(tmp_path):
    """The variant cache still does its job on the new axis: every stimulus of
    one walk asks for the same trims and pays one emit between them."""
    cam = FakeCam(entry_path=_entry(tmp_path))
    graph = _graph(cam, tmp_path=tmp_path)

    first = asyncio.run(graph.install(("tweeter",), None, {"tweeter": -9.5}))
    again = asyncio.run(graph.install(("tweeter",), None, {"tweeter": -9.5}))

    assert first == again
    assert len(graph.emitted) == 1


def test_the_same_coordinate_twice_is_emitted_once(tmp_path):
    """The variant cache still does its job: repeats of one coordinate cost a
    liveness proof, not a re-emit."""
    cam = FakeCam(entry_path=_entry(tmp_path))
    graph = _graph(cam, tmp_path=tmp_path)

    first = asyncio.run(graph.install(("tweeter",), {"tweeter": 100.0}))
    again = asyncio.run(graph.install(("tweeter",), {"tweeter": 100.0}))

    assert first == again
    assert len(graph.emitted) == 1


def test_each_variant_is_emitted_at_most_once_however_many_stimuli(tmp_path):
    """MS-13's structural fact survives R-1: still one emit per variant."""
    cam = FakeCam(entry_path=_entry(tmp_path))
    graph = _graph(cam, tmp_path=tmp_path)

    for _ in range(3):
        asyncio.run(graph.install())
        asyncio.run(graph.install(("tweeter",)))

    assert len(graph.emitted) == 2


def test_a_variant_swap_restores_the_ORIGINAL_entry_graph(tmp_path):
    """The trap the variant path could have opened.

    The entry config is read once, before the first install. A second read
    taken during a variant swap would capture the MEASUREMENT graph as the
    thing to put back, and the session would leave the speaker in it.
    """
    entry = _entry(tmp_path)
    cam = FakeCam(entry_path=entry)
    graph = _graph(cam, tmp_path=tmp_path)

    asyncio.run(graph.install())
    asyncio.run(graph.install(("tweeter",)))
    asyncio.run(graph.install())
    asyncio.run(graph.restore())

    assert [op for op in cam.ops if op == "get_path"] == ["get_path"]
    assert cam.live == "entry: graph\n"


def test_a_variant_swap_is_not_logged_as_a_concurrent_writer(tmp_path, caplog):
    """A walk alternating polarities must not cry "somebody stomped us" twice
    per position. A stomp stays a WARNING; our own swap does not."""
    cam = FakeCam(entry_path=_entry(tmp_path))
    graph = _graph(cam, tmp_path=tmp_path)

    asyncio.run(graph.install())
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        asyncio.run(graph.install(("tweeter",)))

    swap = [r for r in caplog.records if r.name == _LOGGER]
    assert len(swap) == 1, "the swap must be journalled exactly once"
    assert swap[0].levelno == logging.INFO
    assert "result=variant" in swap[0].getMessage()


def test_a_variant_swap_over_a_STOMPED_graph_still_discloses_the_stomp(tmp_path, caplog):
    """The disclosure a polarity walk would otherwise lose entirely.

    On an alternating walk every install is a variant change, so a level
    decided from "the text differs" alone would repair a concurrent writer's
    stomp silently for the whole walk. The swap asks the liveness question
    about the PREVIOUS variant, and answers WARNING when it is gone.
    """
    cam = FakeCam(entry_path=_entry(tmp_path))
    graph = _graph(cam, tmp_path=tmp_path)

    asyncio.run(graph.install())
    cam.live = "somebody else's graph\n"
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        asyncio.run(graph.install(("tweeter",)))

    swap = [r for r in caplog.records if r.name == _LOGGER]
    assert len(swap) == 1
    assert swap[0].levelno == logging.WARNING
    assert "result=reinstall" in swap[0].getMessage()


def test_a_stomped_graph_is_still_reported_as_a_reinstall(tmp_path, caplog):
    """Anti-vacuity for the pin above: the WARNING arm must still be reachable,
    and a variant that is stomped rather than swapped must still reach it."""
    cam = FakeCam(entry_path=_entry(tmp_path))
    graph = _graph(cam, tmp_path=tmp_path)

    asyncio.run(graph.install(("tweeter",)))
    cam.live = "somebody else's graph\n"
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        asyncio.run(graph.install(("tweeter",)))

    stomp = [r for r in caplog.records if r.name == _LOGGER]
    assert len(stomp) == 1
    assert stomp[0].levelno == logging.WARNING
    assert "result=reinstall" in stomp[0].getMessage()
    loads = [op for op in cam.ops if isinstance(op, tuple) and op[0] == "set_raw"]
    assert len(loads) == 2, "the stomp is repaired"


# --------------------------------------------------------------------------- #
# the comparability boundary a round banks at entry (#3489)
# --------------------------------------------------------------------------- #
#
# The scoped fingerprint itself is pinned in
# ``tests/test_crossover_v2_tuning_scope.py``; these are the SESSION's use of
# it — a summed sweep restores the household graph and the next routed
# stimulus re-enters on it, which is the one moment a session can see that the
# bytes under its own captures moved.


def _reenter(tmp_path, first: str, second: str):
    """Enter on ``first``, step aside for a summed sweep, re-enter on ``second``."""
    entry = tmp_path / ENTRY_PATH_NAME
    entry.write_text(first, encoding="utf-8")
    cam = FakeCam(entry_path=str(entry))
    graph = _graph(cam, tmp_path=tmp_path)

    asyncio.run(graph.install())
    banked = graph.entry_scope_fingerprint
    asyncio.run(graph.restore())
    entry.write_text(second, encoding="utf-8")
    asyncio.run(graph.install())
    return graph, banked


def test_a_preference_eq_save_between_two_captures_is_not_a_boundary(tmp_path):
    """The false boundary #3489 exists to prevent.

    The household saved taste EQ while the round was walking. Every byte of
    the entry graph moved, but nothing the round measures through did — so the
    session's captures either side of the save are still comparable and it says
    nothing.
    """
    graph, banked = _reenter(
        tmp_path,
        household_graph(build_sound_filters(FLAT)),
        household_graph(build_sound_filters(SAVED)),
    )

    assert banked
    assert graph.entry_scope_fingerprint == banked
    assert graph.comparability_boundary is False


def test_a_tuning_layer_change_between_two_captures_latches_the_boundary(
    tmp_path, caplog,
):
    """The boundary that must fire, and the anchor that must NOT move with it.

    An out-of-band reconcile changed a per-driver trim mid-round. The session's
    banked entry stays what it entered on — a round keeps its entry graph — and
    the disclosure is loud, because two captures either side of this went
    through different tuning layers.
    """
    saved = build_sound_filters(SAVED)
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        graph, banked = _reenter(
            tmp_path,
            household_graph(saved),
            household_graph(saved, woofer_gain_db=-4.0),
        )

    assert graph.entry_scope_fingerprint == banked
    assert graph.comparability_boundary is True
    disclosed = [
        record
        for record in caplog.records
        if record.name == _LOGGER
        and f"result={COMPARABILITY_BOUNDARY}" in record.getMessage()
    ]
    assert len(disclosed) == 1
    assert disclosed[0].levelno == logging.WARNING


def test_an_unnameable_entry_graph_makes_no_comparison_at_all(tmp_path):
    """An absent measurement is not evidence of a defect.

    The entry graph does not parse, so the session cannot name what it entered
    on. It must not then claim comparability by hashing nothing — and it must
    not refuse the install either.
    """
    graph, banked = _reenter(tmp_path, "- not: a mapping\n", "- still: not one\n")

    assert banked == ""
    assert graph.entry_scope_fingerprint == ""
    assert graph.comparability_boundary is False
