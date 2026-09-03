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

from typing import TYPE_CHECKING, Any, Mapping

from .playback_transaction import PlaybackTransaction
from .program_transaction import (
    Compose,
    ProgramPlaybackTransaction,
    StimulusCapture,
)
from .session_seams import EngineSeams, RecordStore, VolumeClaim

if TYPE_CHECKING:  # pragma: no cover - typing only
    from jasper.audio_measurement.program import ExcitationProgram

__all__ = [
    "NoRoutedPhasesGraph",
    "bind_engine_seams",
    "bind_program_playback_seams",
    "confirm_graph_is_live",
]


class NoRoutedPhasesGraph:
    """The graph slot for a stage that measures nothing through one.

    Stage 2 is verify-class on every tier: it plays its summed sweep through the
    APPLIED graph, so there is no per-driver branch to flip, delay or trim and
    any such coordinate is a caller error (ruling S12 — never bank a record
    naming a coordinate that was not played). ``""`` is the seam's own spelling
    for "the host cannot name the graph".
    """

    async def install(
        self,
        inverted_roles: tuple[str, ...] = (),
        measurement_delays_us: Mapping[str, float] | None = None,
        level_trims_db: Mapping[str, float] | None = None,
    ) -> str:
        if inverted_roles:
            raise ValueError(
                "this stage measures through the applied graph and has no "
                "per-driver branch to invert; cannot flip "
                + ", ".join(sorted(inverted_roles))
            )
        if measurement_delays_us:
            raise ValueError(
                "this stage measures through the applied graph and has no "
                "per-driver branch to delay; cannot delay "
                + ", ".join(sorted(measurement_delays_us))
            )
        if level_trims_db:
            raise ValueError(
                "this stage measures through the applied graph and has no "
                "per-driver branch to trim; cannot level-match "
                + ", ".join(sorted(level_trims_db))
            )
        return ""

    async def patch(self, changes: Mapping[str, Any]) -> None:
        return None

    async def restore(self) -> None:
        return None


def bind_engine_seams(
    *,
    session_graph: Any,
    records: RecordStore,
    volume_claim: VolumeClaim,
    session_volume_plan: Any,
    compose_stimulus: Compose,
    capture_stimulus: StimulusCapture | None = None,
    routed_phases: bool = True,
) -> EngineSeams:
    """The engine's four seams from a host's parts — one binder, any caller.

    ``routed_phases=False`` binds :class:`NoRoutedPhasesGraph`: a verify-class
    stage grades through the APPLIED graph and must not swap the measurement
    graph in and straight back out.
    """
    play: PlaybackTransaction = ProgramPlaybackTransaction(
        compose=compose_stimulus,
        session_volume_plan=session_volume_plan,
        capture=capture_stimulus,
    )
    return EngineSeams(
        graph=session_graph if routed_phases else NoRoutedPhasesGraph(),
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
) -> dict[str, Any]:
    """The real CamillaController-backed seams for :func:`play_program`.

    Returns the keyword mapping ``play_program(program,
    session_volume_plan=..., **bind_program_playback_seams(...))`` consumes.
    ``writer_lock`` is held across the play so no other DSP writer can replace
    the measurement graph mid-capture; ``readmit`` re-reads the WAV bytes fresh
    rather than trusting the composed program.
    """
    from jasper.dsp_apply import dsp_writer_lock

    from ..program_admission import readmit_program_from_wav
    from ..program_playback import verified_program_aplay

    async def _play_wav() -> Any:
        return await verified_program_aplay(bundle_dir, artifact, timeout_s=timeout_s)

    async def _readmit() -> Any:
        # ``declared_sensitivities`` MUST match what the session composed
        # against: readmission re-resolves every cap, so dropping it here would
        # refuse a program composed at a different HF ceiling.
        return readmit_program_from_wav(
            program,
            wav_path,
            topology=topology,
            safety_profile=safety_profile,
            role_targets=role_targets,
            session_volume_db=session_volume_db,
            declared_sensitivities=declared_sensitivities,
        )

    return {
        "play_wav": _play_wav,
        "readmit": _readmit,
        "writer_lock": lambda: dsp_writer_lock(
            config_dir, source="crossover_v2_program"
        ),
    }
