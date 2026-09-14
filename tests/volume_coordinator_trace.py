# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Byte-identity harness for the ``volume_coordinator.py`` extraction series.

``scripted_trace()`` drives one coordinator through every write path (set,
adjust, mute, observe, handoff, reconcile, context, locked/measuring/offline
writes) across five source profiles and returns the ordered outcome. Each PR
in the split runs it on ``origin/main`` and on its own branch and diffs the
two hashes — a mismatch means the extraction changed behavior, not just
location. Not a pytest test: no assertions live here, only the fixture.
"""
from __future__ import annotations

import asyncio
import os
import tempfile


def scripted_trace() -> list:
    return asyncio.run(_run())


async def _run() -> list:
    tmp = tempfile.mkdtemp(prefix="vctrace-")
    os.environ["JASPER_VOLUME_DIAGNOSTICS_PATH"] = os.path.join(tmp, "volume_policy.json")
    os.environ["JASPER_SOUND_SETTINGS_PATH"] = os.path.join(tmp, "settings.json")
    from tests.test_volume_coordinator import (
        _FakeCamilla, _RecordingCoordinator, _build,
    )
    from jasper.volume_coordinator import VolumeCoordinator
    from jasper.music_sources import Source
    from jasper.assistant_volume import EffectiveVolumeContext

    class _Cam(_FakeCamilla):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.graph_answers = [False, False, True, False]
            self.graph_calls = 0

        def graph_mutation_in_progress(self):
            self.graph_calls += 1
            return self.graph_answers[self.graph_calls % len(self.graph_answers)]

    out: list = []
    published: list = []

    async def _publisher(ctx: EffectiveVolumeContext) -> bool:
        published.append((round(ctx.canonical_db, 4), round(ctx.downstream_db, 4),
                          round(ctx.tts_envelope_lufs, 4), ctx.muted))
        return True

    for name, active, selected in (
        ("idle", {}, None),
        ("airplay", {"airplay_active": True}, Source.AIRPLAY.value),
        ("spotify", {"spotify_active": True}, Source.SPOTIFY.value),
        ("bluetooth", {"bluetooth_active": True}, Source.BLUETOOTH.value),
        ("usbsink", {"usbsink_active": True}, Source.USBSINK.value),
    ):
        published.clear()
        probes = {"idle": None, "airplay": _t, "spotify": _f,
                  "bluetooth": _none, "usbsink": None}
        cls = VolumeCoordinator if name == "usbsink" else _RecordingCoordinator
        coord, cam, store = _build(
            cls, __import__("pathlib").Path(tempfile.mkdtemp(dir=tmp)),
            active=active, selected=selected, level=50,
            handoff_settle_sec=0.0, push_settle_sec=0.0,
            volume_context_publisher=_publisher,
            duck_active_probe=probes[name],
        )
        cam.__class__ = _Cam
        cam.graph_answers = [False, False, True, False]
        cam.graph_calls = 0
        steps: list = []
        steps.append(("initialize", await coord.initialize()))
        for pct in (70, 1, 0, 35, 100):
            steps.append((f"set:{pct}", await coord.set_listening_level(pct)))
        steps.append(("adjust:-15", await coord.adjust_listening_level(-15)))
        steps.append(("mute", await coord.mute()))
        steps.append(("toggle", _state(await coord.toggle_mute())))
        steps.append(("set_muted:T", _state(await coord.set_muted(True))))
        steps.append(("unmute", await coord.unmute()))
        for src, native in ((Source.AIRPLAY, 42), (Source.SPOTIFY, 42),
                            (Source.BLUETOOTH, 64), (Source.USBSINK, 42),
                            (Source.IDLE, 42)):
            steps.append((f"observe:{src.value}",
                          await coord.observe_source_volume(src, native)))
            steps.append((f"observe2:{src.value}",
                          await coord.observe_source_volume(src, native, initial=True)))
            steps.append((f"rev:{src.value}",
                          coord.source_observation_revision(src) is not None))
        steps.append(("state", _state(coord.get_volume_state())))
        steps.append(("persisted", coord.load_persisted_level(), coord.is_muted(),
                      coord.get_listening_level()))
        for prev, cur in ((Source.IDLE, Source.SPOTIFY), (Source.SPOTIFY, Source.AIRPLAY),
                          (Source.SPOTIFY, Source.BLUETOOTH), (Source.AIRPLAY, Source.USBSINK)):
            h = await coord.prepare_source_handoff(prev, cur, reason="trace")
            steps.append((f"prepare:{prev.value}>{cur.value}", h.result, h.detail, h.level,
                          h.push_ok, h.camilla_guarded, _r(h.guard_db), _r(h.camilla_before_db)))
            steps.append(("finalize", await coord.finalize_source_handoff(h)))
            steps.append(("abort", await coord.abort_source_handoff(h)))
        cam._db = 12.0
        await coord.maybe_reconcile_camilla()
        steps.append(("reconcile_loud", _r(cam._db)))
        cam._db = -40.0
        await coord.maybe_reconcile_camilla()
        steps.append(("reconcile_deep_quiet", _r(cam._db)))
        steps.append(("target_db", _r(await coord.get_camilla_target_db())))
        ctx = await coord.effective_volume_context()
        steps.append(("context", _r(ctx.canonical_db), _r(ctx.downstream_db),
                      _r(ctx.tts_envelope_lufs), ctx.muted))
        coord.note_voice_session(True, camilla_volume_locked=True)
        steps.append(("locked_set", await coord.set_listening_level(20)))
        coord.note_voice_session(False)
        await coord.note_measurement_active(True)
        try:
            await coord.set_listening_level(80)
            steps.append(("measuring_set", "allowed"))
        except Exception as e:  # noqa: BLE001 - the trace records the type, whatever it is
            steps.append(("measuring_set", type(e).__name__))
        await coord.note_measurement_active(False)
        cam.unavailable = True
        steps.append(("offline_set", await coord.set_listening_level(45)))
        steps.append(("offline_ctx", _r((await coord.effective_volume_context()).downstream_db)))
        cam.unavailable = False
        steps.append(("owner_db", _r(await coord.volume_owner.current_fader_db()
                                     if hasattr(coord.volume_owner, "current_fader_db") else None)))
        rec = store.load()
        out.append((
            name,
            steps,
            [(k, _r(v) if isinstance(v, float) else v) for k, v in cam.events],
            [_r(v) for v in cam.loudness_calls],
            *[getattr(coord, n, None) for n in
              ("airplay_writes", "spotify_writes", "bt_writes", "camilla_writes")],
            (rec.listening_level, _r(rec.main_volume_db), rec.pre_mute_level,
             rec.mute_token is not None),
            list(published),
        ))
    return out


async def _t():
    return True


async def _f():
    return False


async def _none():
    return None


def _state(s):
    return (s.listening_level, s.pre_mute_level, s.effective_percent, s.muted,
            s.mute_token is not None)


def _r(v):
    return None if v is None else round(float(v), 4)
