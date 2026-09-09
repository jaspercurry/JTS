# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Composing the engine around a host: the binder, and the play-seam plumbing.

What a FRONT END calls to stand the engine up, in engine vocabulary only:
:func:`bind_engine_seams`, :func:`bind_program_playback_seams` and
:func:`confirm_graph_is_live`. No ``jasper.web`` import may enter this module —
the bank's other reader is an LLM-over-SSH surface (ADR-0188 §4) that
constructs the same engine and must not pull the web host in.
"""

from __future__ import annotations

import asyncio
from functools import partial
from itertools import count
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping

from .playback_transaction import PlaybackTransaction
from .program_transaction import (
    Compose,
    ProgramForStimulus,
    ProgramPlaybackTransaction,
    StimulusCapture,
)
from .session_seams import EngineSeams, RecordStore, VolumeClaim

if TYPE_CHECKING:  # pragma: no cover - typing only
    from jasper.audio_measurement.program import ExcitationProgram

__all__ = [
    "bind_engine_seams",
    "bind_program_playback_seams",
    "bind_program_composer",
    "confirm_graph_is_live",
]


def bind_engine_seams(
    *,
    session_graph: Any,
    records: RecordStore,
    volume_claim: VolumeClaim,
    session_volume_plan: Any,
    compose_stimulus: Compose,
    capture_stimulus: StimulusCapture | None = None,
) -> EngineSeams:
    """Bind the engine's graph, volume, records and playback owners."""
    play: PlaybackTransaction = ProgramPlaybackTransaction(
        compose=compose_stimulus,
        session_volume_plan=session_volume_plan,
        capture=capture_stimulus,
    )
    return EngineSeams(
        graph=session_graph,
        volume=volume_claim,
        records=records,
        play=play,
    )


async def confirm_graph_is_live(cam: Any, submitted_yaml: str) -> None:
    """Prove the graph CamillaDSP is running is the one just submitted.

    Submitted TEXT cannot be compared against ``GetConfig``: a readback is a
    default-filled, normalized SUPERSET, so ``ReadConfig`` canonicalizes first
    and strict equality applies to that. Evidence, and what was NOT measured:
    ``docs/historical/crossover-measurement-v2-campaign-record.md``,
    "Confirming a program graph is live".
    """
    from jasper.camilla import CamillaConfigRejected

    from ..commissioning_admission import (
        ActiveCommissioningAdmissionError,
        running_graph_fingerprint,
    )
    from ..program_playback import ProgramPlaybackError

    try:
        normalized = await cam.normalize_config_raw(submitted_yaml, best_effort=False)
        if not isinstance(normalized, str):
            raise CamillaConfigRejected("normalization returned no config")
    except CamillaConfigRejected as exc:
        raise ProgramPlaybackError("program graph normalization failed") from exc
    try:
        matched = running_graph_fingerprint(
            await cam.get_active_config_raw(best_effort=False)
        ) == running_graph_fingerprint(normalized)
    except ActiveCommissioningAdmissionError as exc:
        raise ProgramPlaybackError("program graph readback is invalid") from exc
    if not matched:
        raise ProgramPlaybackError("program graph load was not confirmed")


def bind_program_playback_seams(
    cam: Any,
    *,
    bundle_dir: str,
    artifact: Any,
    config_dir: str,
    program: "ExcitationProgram",
    wav_path: str,
    topology: Any,
    safety_profile: Mapping[str, Any],
    role_targets: Mapping[str, str],
    session_volume_db: float,
    declared_sensitivities: Mapping[str, float] | None = None,
    timeout_s: float = 60.0,
    graph_yaml: str,
    summed: bool = False,
    bass_profile_summary: Mapping[str, Any] | None = None,
    phase: str = "",
    before_play: Callable[[Any, Any, str], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """The real CamillaController-backed seams for :func:`play_program`.

    Returns the keyword mapping ``play_program(program,
    session_volume_plan=..., **bind_program_playback_seams(...))`` consumes.
    ``writer_lock`` is held across the play so no other DSP writer can replace
    the measurement graph mid-capture; ``readmit`` re-reads the WAV bytes fresh
    rather than trusting the composed program.
    """
    from jasper.dsp_apply import dsp_writer_lock

    from ..program_admission import (
        readmit_program_from_wav,
        readmit_summed_program_from_wav,
    )
    from ..program_playback import verified_program_aplay

    if not graph_yaml:
        raise ValueError("playback requires its installed measurement graph")

    async def _play_wav() -> Any:
        await confirm_graph_is_live(cam, graph_yaml)
        if before_play is not None:
            await before_play(program, artifact, phase or program.phase)
        return await verified_program_aplay(bundle_dir, artifact, timeout_s=timeout_s)

    async def _readmit() -> Any:
        # ``declared_sensitivities`` MUST match what the session composed
        # against: readmission re-resolves every cap, so dropping it here would
        # refuse a program composed at a different HF ceiling.
        arguments = dict(
            topology=topology,
            safety_profile=safety_profile,
            role_targets=role_targets,
            session_volume_db=session_volume_db,
            declared_sensitivities=declared_sensitivities,
        )
        readmit: Callable[[], Any]
        if not summed:
            readmit = partial(readmit_program_from_wav, program, wav_path, **arguments)
        else:
            if bass_profile_summary is not None:
                arguments["bass_profile_summary"] = bass_profile_summary
            readmit = partial(
                readmit_summed_program_from_wav, program, wav_path,
                graph_yaml=graph_yaml, **arguments,
            )
        return await asyncio.to_thread(readmit)

    return {
        "play_wav": _play_wav,
        "readmit": _readmit,
        "writer_lock": lambda: dsp_writer_lock(
            config_dir, source="crossover_v2_program"
        ),
    }


def bind_program_composer(
    *,
    program_for_spec: Callable[[Any, float | None], "ExcitationProgram"],
    store: Any,
    capture_session_id: str,
    cam_factory: Callable[[], Any],
    config_dir: str,
    topology: Any,
    safety_profile: Mapping[str, Any],
    role_targets: Mapping[str, str],
    session_volume_db: float,
    declared_sensitivities: Mapping[str, float] | None = None,
    before_play: Callable[[Any, Any, str], Awaitable[None]] | None = None,
    graph_yaml: Callable[[], str],
    bass_profile_summary: Callable[[Any], Mapping[str, Any]] | None = None,
) -> Compose:
    """Render each take once and bind admission, locked graph proof and playback.

    ``graph_yaml`` supplies the installed graph, including any driver overlays.
    ``before_play`` runs after its live proof, inside the same writer lock.
    ``bass_profile_summary`` answers, per spec, which bass rung that graph is
    allowed to carry; unbound, admission requires no bass stage at all.
    """
    from jasper.audio_measurement.program import write_program_wav

    from .measure_spec import GRAPH_SCOPE_DRIVERS

    ordinals = count()
    bundle_dir = Path(store.bundle_dir)

    async def compose(
        *, spec: Any, position_deg: int | None = None, prompt: str = "",
        level_db: float = 0.0, stimulus_dbfs: float | None = None,
    ) -> ProgramForStimulus:
        program = program_for_spec(spec, stimulus_dbfs)
        expected_graph = graph_yaml()
        if not expected_graph:
            raise ValueError("playback has no installed measurement graph")
        phase = spec.program_phase or program.phase
        wav_rel = (
            f"crossover_v2/{capture_session_id}/"
            f"{phase}_{next(ordinals):02d}_program.wav"
        )
        wav_path = bundle_dir / wav_rel

        def render() -> Any:
            wav_path.parent.mkdir(parents=True, exist_ok=True)
            write_program_wav(str(wav_path), program)
            return store.identify_artifact(wav_rel)

        artifact = await asyncio.to_thread(render)
        return ProgramForStimulus(program, bind_program_playback_seams(
            cam_factory(), bundle_dir=str(bundle_dir), artifact=artifact,
            config_dir=config_dir, program=program, wav_path=str(wav_path),
            topology=topology, safety_profile=safety_profile,
            role_targets=role_targets, session_volume_db=session_volume_db,
            declared_sensitivities=declared_sensitivities,
            graph_yaml=expected_graph, summed=spec.graph_scope != GRAPH_SCOPE_DRIVERS,
            bass_profile_summary=(
                None if bass_profile_summary is None else bass_profile_summary(spec)
            ),
            phase=phase, before_play=before_play,
        ))

    return compose
