# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Composing the engine around a host: the binder, and the play-seam plumbing.

Three things live here, and they share one reason: they are what a FRONT END
calls to stand the engine up, and none of them is web vocabulary — so homing
them in the web module made the 8,000-line host a required import for any
second caller. Ruling: the walk's UI is the web wizard, but reading the bank is
an LLM-over-SSH surface (ADR-0188 §4) — a runner on that surface constructs the
same engine through this module and never imports :mod:`jasper.web`.

* :func:`bind_engine_seams` — the engine's four seams as ONE constructor call,
  taking only engine vocabulary. Host policy stays with the host: which claim,
  which record store, and what refusal a missing volume owner renders are the
  CALLER's inputs, not decisions made here.
* :func:`bind_program_playback_seams` — the real CamillaController-backed
  seams for :func:`~..program_playback.play_program`, lifted whole from the
  flow file (band AE of its dissolution map).
* :func:`confirm_graph_is_live` — the fail-closed proof that the graph
  CamillaDSP runs is the one just submitted; the session graph's
  ``confirm_live`` slot.

**No ``jasper.web`` import may enter this module** — it sits under the
MS-17/engine import pins, and that constraint is the module's point.
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

    Stage 2 is verify-class on every tier: it plays its summed sweep through
    the APPLIED production graph and takes no routed per-driver capture. A
    session bound to the real measurement graph would therefore swap the whole
    DSP chain, step aside on the first summed phase, and swap back — a full
    graph load and restore for zero measurements, carrying every stranding
    exposure an installed graph carries and buying nothing.

    Reports ``""`` as its fingerprint, which is the seam's own spelling for
    *"the host cannot name the graph"* — honest here, because there is no
    measurement graph to name.

    **It cannot take any per-driver measurement coordinate, and says so
    instead of ignoring it.** The flip, the delay and the level match all live
    in the measurement graph's per-driver branch, and this stage measures
    through the APPLIED one; a silently dropped ``inverted_roles`` would play a
    normal capture and bank a record claiming an inverted one, a silently
    dropped delay would bank a record naming a coordinate it never played, and
    a silently dropped level match would bank a record claiming branches that
    were never levelled. That is the exact lie ruling S12 exists to refuse, so
    any of them bound to this stage is a caller error and raises like one.
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

    What this owns: the routed/summed graph switch, the play transaction's
    construction, and the ``EngineSeams`` assembly. What it deliberately does
    NOT own: resolving the claim (rank policy and the refusal copy a missing
    owner renders are the host's) and building the record store (its state I/O
    is the host's). A second front end supplies its own parts and never touches
    :mod:`jasper.web`.

    ``session_volume_plan`` is the plan object ``play_program`` asserts
    against — the durable half of the volume story. The CLAIM is the fader's
    one owner-ranked hold; both are the caller's, already resolved, because a
    binder that reached for process globals would bind only inside the
    process that has them.

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

    Contract: prove the SUBMITTED graph is live, tolerate benign serializer
    normalization, reject a different graph. Submitted TEXT vs ``GetConfig``
    cannot — a readback is a default-filled, normalized SUPERSET — so
    ``ReadConfig`` canonicalizes first and STRICT equality still applies.
    Evidence, and what was NOT measured:
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
    session_volume_plan=..., **bind_program_playback_seams(...))`` consumes:

    * ``play_wav`` — the verified-WAV source
      (:func:`jasper.active_speaker.program_playback.verified_program_aplay`):
      sha256-bound bytes through the stable-fd aplay path to
      ``correction_substream``.
    * ``readmit`` — :func:`jasper.active_speaker.program_admission.readmit_program_from_wav`
      from a FRESH byte readback (the play-time gate).
    * ``writer_lock`` — :func:`jasper.dsp_apply.dsp_writer_lock` on the shared
      generated-config dir, held across the play so no other DSP writer can
      replace the measurement graph mid-capture.

    **The graph seams left this binding.** ``read_current_config_path``,
    ``load_program_graph`` and ``restore_graph`` existed to swap the program
    graph in and out around every stimulus;
    :class:`~.session_graph.MeasurementSessionGraph` now installs
    it once per session and proves it before each one, so their per-stimulus
    transport — two ``SetConfig`` calls, two ducks and the readback that
    confirmed each — is gone rather than moved. :func:`confirm_graph_is_live`
    is still the proof; the session graph is what calls it.
    """
    from jasper.dsp_apply import dsp_writer_lock

    from ..program_admission import readmit_program_from_wav
    from ..program_playback import verified_program_aplay

    async def _play_wav() -> Any:
        return await verified_program_aplay(bundle_dir, artifact, timeout_s=timeout_s)

    async def _readmit() -> Any:
        # ``declared_sensitivities`` MUST match what the session composed
        # against: readmission re-resolves every cap, so a program composed at
        # the W6.5-derived HF ceiling would be refused here at the legacy one
        # if the mapping were dropped on this side.
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
