# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""One watched summed sweep per level reading, without banking a take."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

from jasper.audio_measurement.playback import PlaybackObservation
from jasper.audio_measurement.program import PROGRAM_SAMPLE_RATE_HZ, ExcitationProgram
from jasper.audio_measurement.wired_capture import WiredMicDevice, WiredSplMonitor, make_wired_recorder

from .auto_level import reading_budget
from .capture_provenance import _stimulus_peak_dbfs
from .crossover_v2.composition import bind_program_composer
from .crossover_v2.door import set_measurement_loudness
from .crossover_v2.measure_spec import MeasureSpec
from .crossover_v2.program_transaction import StimulusCaptureError, StimulusCaptureStopped
from .crossover_v2.programs import SessionExcitation
from .crossover_v2.wired_stimulus import WiredStimulusCapture
from .program_playback import play_program
from .restore_wait import resilient_restore
from .seat_level_reference import StimulusProvenance
from .volume_latch import hold_fader_at, read_fader_db

# Seconds for graph/fader confirmation and the recorder's capture tail per sweep.
READING_OVERHEAD_S = 6.0


def watchdog_seconds(start: float, ceiling_db: float, sweep_s: float) -> float:
    return reading_budget(start, ceiling_db) * (sweep_s + READING_OVERHEAD_S) + 30.0


class SweepLevelReader:
    def __init__(
        self, *, excitation: SessionExcitation, candidate: Any, graph: Any,
        cam: Any, plan: Any, store: Any, context: Any, device: WiredMicDevice,
        monitor: WiredSplMonitor, config_dir: str, bundle_id: str,
    ) -> None:
        self.excitation, self.graph, self.cam, self.plan = excitation, graph, cam, plan
        self.device, self.monitor = device, monitor
        self.bundle_id = bundle_id
        self.provenance: StimulusProvenance | None = None
        self.first = True
        self.capture = WiredStimulusCapture(
            device=device, bundle_dir=Path(store.bundle_dir), spl_monitor=monitor,
        )
        self.spec = MeasureSpec(kind="baseline", graph_scope="candidate", candidate_id=candidate.fingerprint)
        self.compose: Any = bind_program_composer(
            program_for_spec=lambda _spec, _peak: self.program(), store=store,
            capture_session_id=bundle_id, cam_factory=lambda: cam,
            config_dir=config_dir, topology=context.topology,
            safety_profile=context.safety_profile, role_targets=context.role_targets,
            declared_sensitivities=context.declared_sensitivities,
            graph_yaml=graph.installed_graph_yaml,
            bass_extension_for_spec=lambda _spec: candidate.bass_extension,
            before_play=self._before_play,
        )

    def program(self) -> ExcitationProgram:
        return self.excitation.verify_program(courtesy_prelude=self.first, leading_pilots=False)

    async def _before_play(self, spec: Any, program: Any, artifact: Any, phase: str) -> None:
        await hold_fader_at(self.excitation.session_volume_db, self.cam.get_volume_db,
                            context="seat_level_sweep")
        peak = _stimulus_peak_dbfs(program)
        assert peak is not None
        self.provenance = StimulusProvenance(
            program_id=program.program_id, phase=phase, wav_sha256=artifact.sha256,
            peak_dbfs=peak, bundle_id=self.bundle_id,
        )

    async def read_level(self) -> float:
        gain = await read_fader_db(self.cam.get_volume_db)
        if gain is None or gain > 0.0:
            raise StimulusCaptureStopped("volume_latch_unconfirmed", "The fader is unreadable", PlaybackObservation(emission="not_started"))
        self.excitation = replace(self.excitation, session_volume_db=gain)
        await set_measurement_loudness(self.cam, gain)
        await self.graph.install()
        stimulus = await self.compose(spec=self.spec, level_db=gain)

        async def play() -> None:
            await play_program(stimulus.program, session_volume_plan=self.plan, **stimulus.seams)

        try:
            await self.capture.around(play, program=stimulus.program)
        except StimulusCaptureStopped:
            # The capture wrapper carries the code; the watch owns the stop's value.
            if self.monitor.error is not None:
                raise self.monitor.error
            raise
        answer = self.capture.take_answer()
        if answer is None:
            raise StimulusCaptureError("The sweep produced no SPL observation")
        self.first = False
        return float((answer.capture_integrity or {})["spl"]["max_window_db_spl"])

    async def read_ambient(self) -> float:
        recorder = make_wired_recorder(
            self.device, sample_rate_hz=PROGRAM_SAMPLE_RATE_HZ, max_capture_s=READING_OVERHEAD_S,
        )
        recorder.spl_monitor = self.monitor
        self.monitor.reset()
        try:
            # Drain startup before abort, including when a second cancel arrives.
            await resilient_restore(asyncio.to_thread(recorder.start))
            await asyncio.sleep(1.0)
        finally:
            await resilient_restore(asyncio.to_thread(recorder.abort))
        if recorder.failure is not None:
            raise recorder.failure
        return self.monitor.max_window_db_spl
