# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The v2 crossover conductor's web host (Wave 5a endpoint binding).

Owns everything between the ``/correction/crossover/v2/*`` POST routes (thin
dispatch branches in :mod:`jasper.web.correction_setup`) and the pure conductor
(:mod:`jasper.active_speaker.crossover_v2_flow`):

* the **durable v2 flow state** (one JSON file) that ``status_payload`` threads
  into the envelope as ``status["crossover_v2"]`` — phase / candidate / verify
  / failure / needs_recovery / applied;
* the **session volume plan** singleton (one fixed measurement volume per
  session, §5.5) and its open/close/abandon wiring — including the
  walked-away guarantee: every terminal relay outcome drains the restore-once
  path;
* the **production seam bindings** — real ``analyze_program_capture``, real
  evidence-store publication (publish → tamper-checked reopen, §5.6), the real
  CamillaController-backed program playback via
  :func:`jasper.active_speaker.crossover_v2_flow.bind_program_playback_seams`,
  and the apply gate reading the durable applied flag;
* the **plan runner host** (:func:`build_v2_run_and_consume`) that drives the
  REAL :func:`jasper.capture_relay.session.run_capture_plan` on a worker
  thread, mirroring ``build_crossover_relay_plan_run_and_consume``'s thread
  model (``asyncio.to_thread`` + shielded cancel-drain + purge on teardown +
  ``stop_event``/``stop_lock`` stop semantics);
* the **host-owned error mapping**: ``CaptureTimeout`` / relay-session death →
  ``relay_timeout`` failure state + volume abandon; conductor failure codes →
  ``status["crossover_v2"]["failure"]``.

Session binding (§5.6): the durable state is keyed to the relay session id. A
new ``/v2/session`` POST hydrates through
:meth:`CrossoverV2Conductor.hydrate`, which invalidates CHECK/MEASURE evidence
for a different session; ``/v2/verify`` re-arms VERIFY only (a 1-entry plan)
from the persisted post-apply state, per §5.2's re-verify action.

ON-DEVICE: the acoustic playback binding is not exercised hardware-free (same
status as the room/sync relay flows) — W6 validates it end-to-end on JTS3.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from jasper.atomic_io import atomic_write_text
from jasper.log_event import log_event

logger = logging.getLogger(__name__)

STATE_SCHEMA_VERSION = 1
STATE_KIND = "jts_crossover_v2_flow_state"
DEFAULT_V2_STATE_PATH = Path(
    "/var/lib/jasper/active_speaker_crossover_v2_state.json"
)

# The wizard-facing relay kind label (mirrors the legacy
# "crossover_sweep:<kind>" labels so /status.relay consumers need no new
# vocabulary beyond the prefix).
V2_RELAY_KIND_SESSION = "crossover_v2:session"
V2_RELAY_KIND_VERIFY = "crossover_v2:verify"

# Downsample ceiling for the persisted predicted-sum verify prior — enough
# resolution for the ±1.5 dB [Fc/2, 2Fc] comparison at 1/6-octave smoothing
# while keeping the durable state file small.
MAX_PERSISTED_SUM_POINTS = 512

_state_lock = threading.Lock()
_state_path_override: Path | None = None

_volume_plan_lock = threading.Lock()
_volume_plan: Any = None


class CrossoverV2Refused(ValueError):
    """A v2 endpoint refusal (maps to HTTP 400 in the dispatch ladder)."""


# --------------------------------------------------------------------------- #
# flow selector + durable state
# --------------------------------------------------------------------------- #


def v2_flow_active() -> bool:
    from jasper.active_speaker.crossover_flow import (
        CROSSOVER_FLOW_V2,
        active_crossover_flow,
    )

    return active_crossover_flow() == CROSSOVER_FLOW_V2


def _state_path() -> Path:
    return _state_path_override or DEFAULT_V2_STATE_PATH


def set_state_path_for_tests(path: str | Path | None) -> None:
    """Test seam: point the durable v2 state at a temp file (None resets)."""
    global _state_path_override
    with _state_lock:
        _state_path_override = Path(path) if path is not None else None


def load_v2_state() -> dict[str, Any] | None:
    """Read the durable v2 flow state; malformed/missing reads as ``None``."""
    with _state_lock:
        try:
            raw = json.loads(_state_path().read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError):
            log_event(
                logger,
                "correction.crossover_v2_state_unreadable",
                level=logging.WARNING,
            )
            return None
    if (
        not isinstance(raw, Mapping)
        or raw.get("kind") != STATE_KIND
        or raw.get("schema_version") != STATE_SCHEMA_VERSION
    ):
        return None
    return dict(raw)


def save_v2_state(state: Mapping[str, Any]) -> None:
    payload = {
        "schema_version": STATE_SCHEMA_VERSION,
        "kind": STATE_KIND,
        "updated_at": time.time(),
        **{k: v for k, v in state.items() if k not in {"schema_version", "kind"}},
    }
    with _state_lock:
        atomic_write_text(
            _state_path(),
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            mode=0o640,
            group_from_parent=True,
        )


def clear_v2_state() -> None:
    with _state_lock:
        try:
            _state_path().unlink()
        except FileNotFoundError:
            pass
        except OSError:
            log_event(
                logger,
                "correction.crossover_v2_state_clear_failed",
                level=logging.WARNING,
            )


def observe_apply_success(candidate_fingerprint: str) -> None:
    """Mark the v2 candidate applied — the apply-complete event that arms the
    soft-held VERIFY (§5.2). Called by the v2 apply endpoint on success."""
    state = load_v2_state()
    if state is None:
        return
    candidate = state.get("candidate")
    stored = (
        str(candidate.get("fingerprint") or "")
        if isinstance(candidate, Mapping)
        else ""
    )
    if stored and candidate_fingerprint and stored != candidate_fingerprint:
        log_event(
            logger,
            "correction.crossover_v2_apply_fingerprint_mismatch",
            level=logging.WARNING,
        )
        return
    state["applied"] = True
    state["failure"] = None
    save_v2_state(state)
    log_event(logger, "correction.crossover_v2_applied")


def _applied_gate() -> bool:
    """The conductor's ``apply_complete`` seam: reads the durable applied flag."""
    state = load_v2_state()
    return bool(state and state.get("applied") is True)


# --------------------------------------------------------------------------- #
# session volume plan singleton (§5.5)
# --------------------------------------------------------------------------- #


def session_volume_plan() -> Any:
    """The one durable-state-backed SessionVolumePlan this process owns."""
    global _volume_plan
    from jasper.active_speaker.session_volume_plan import (
        DEFAULT_SESSION_VOLUME_STATE_PATH,
        SessionVolumePlan,
    )

    with _volume_plan_lock:
        if _volume_plan is None:
            _volume_plan = SessionVolumePlan(
                state_path=DEFAULT_SESSION_VOLUME_STATE_PATH
            )
        return _volume_plan


def set_volume_plan_for_tests(plan: Any) -> None:
    global _volume_plan
    with _volume_plan_lock:
        _volume_plan = plan


# --------------------------------------------------------------------------- #
# status threading (S1b)
# --------------------------------------------------------------------------- #


def _phase_from_state(state: Mapping[str, Any] | None) -> str:
    from jasper.active_speaker.crossover_v2_flow import (
        CAPTURE_PHASES,
        PHASE_DONE,
        PHASE_MEASURE,
        PHASE_REVIEW_APPLY,
        PHASE_VERIFY,
    )

    accepted = set(
        state.get("accepted_phases") or () if isinstance(state, Mapping) else ()
    )
    applied = bool(state and state.get("applied"))
    for phase in CAPTURE_PHASES:
        if phase not in accepted:
            if phase == PHASE_VERIFY and PHASE_MEASURE in accepted and not applied:
                return PHASE_REVIEW_APPLY
            return phase
    return PHASE_DONE


def crossover_v2_status_block() -> dict[str, Any] | None:
    """The ``status["crossover_v2"]`` block, or ``None`` when the flow is legacy.

    ``needs_recovery`` comes from the SessionVolumePlan (the W2 gate ruling:
    key on ``needs_recovery``, never ``unresolved_volume_safety`` alone — a
    crash-hydrated active plan surfaces no unresolved payload but still needs
    draining before a new session).
    """
    if not v2_flow_active():
        return None
    state = load_v2_state()
    try:
        needs_recovery = bool(session_volume_plan().needs_recovery)
    except (OSError, RuntimeError, ValueError):
        needs_recovery = True  # unreadable volume state fails closed
    block: dict[str, Any] = {
        "phase": _phase_from_state(state),
        "candidate": (state or {}).get("candidate"),
        "verify": (state or {}).get("verify"),
        "failure": (state or {}).get("failure"),
        "needs_recovery": needs_recovery,
        "applied": bool(state and state.get("applied")),
        "session_id": (state or {}).get("session_id"),
    }
    return block


# --------------------------------------------------------------------------- #
# conductor persistence
# --------------------------------------------------------------------------- #


def _decimate_sum(predicted_sum: Any) -> dict[str, Any] | None:
    if predicted_sum is None:
        return None
    freqs, mags = predicted_sum
    n = len(freqs)
    if n == 0:
        return None
    step = max(1, n // MAX_PERSISTED_SUM_POINTS)
    return {
        "freqs_hz": [float(f) for f in freqs[::step]],
        "magnitude_db": [float(m) for m in mags[::step]],
    }


def _candidate_summary(candidate: Any) -> dict[str, Any] | None:
    if candidate is None:
        return None
    return {
        "fingerprint": candidate.fingerprint,
        "program_id": candidate.program_id,
        "trims_db": dict(candidate.role_attenuations_db),
        "alignment": candidate.alignment.to_dict(),
    }


def persist_conductor_state(
    conductor: Any,
    *,
    failure_code: str | None,
    evidence: Mapping[str, Any] | None = None,
) -> None:
    """Write the conductor's durable snapshot + host-observed failure state."""
    snap = conductor.snapshot()
    verify_outcome = conductor.verify_outcome
    state: dict[str, Any] = {
        "session_id": snap.session_id,
        "accepted_phases": list(snap.accepted_phases),
        "applied": snap.applied,
        "gain_plan_db": dict(snap.gain_plan_db) if snap.gain_plan_db else None,
        "candidate": _candidate_summary(conductor.candidate),
        "verify": (
            {"outcome": verify_outcome} if verify_outcome is not None else None
        ),
        "failure": {"code": failure_code} if failure_code else None,
        "verify_priors": {
            "predicted_sum": _decimate_sum(conductor.measure_predicted_sum),
            "gate_window_ms": conductor.measure_gate_window_ms,
        },
        "evidence": dict(evidence) if evidence else None,
    }
    prior = load_v2_state() or {}
    # The applied flag is host-durable (set by the apply endpoint) — never
    # regressed by a conductor snapshot that predates it.
    if prior.get("applied") is True and prior.get("session_id") == snap.session_id:
        state["applied"] = True
    if state["candidate"] is None and isinstance(prior.get("candidate"), Mapping):
        if prior.get("session_id") == snap.session_id:
            state["candidate"] = dict(prior["candidate"])
    if state["evidence"] is None and isinstance(prior.get("evidence"), Mapping):
        if prior.get("session_id") == snap.session_id:
            state["evidence"] = dict(prior["evidence"])
    save_v2_state(state)


def _persist_terminal_failure(conductor: Any, code: str) -> None:
    """Session-terminal persistence (§5.6): pre-apply, capture evidence dies
    with the session (restart at CHECK); post-apply, the applied candidate +
    verify priors survive so ``/v2/verify`` can re-arm."""
    persist_conductor_state(conductor, failure_code=code)
    state = load_v2_state()
    if state is None:
        return
    if not state.get("applied"):
        state["accepted_phases"] = []
        state["gain_plan_db"] = None
    save_v2_state(state)


# --------------------------------------------------------------------------- #
# production seam bindings (S1a/S1e)
# --------------------------------------------------------------------------- #


def _wav_bytes_to_samples(wav_bytes: bytes) -> tuple[Any, int]:
    import io

    import numpy as np
    from scipy.io import wavfile

    rate, data = wavfile.read(io.BytesIO(wav_bytes))
    if data.ndim > 1:
        data = data[:, 0]
    if np.issubdtype(data.dtype, np.integer):
        scale = float(np.iinfo(data.dtype).max)
        samples = data.astype(np.float64) / scale
    else:
        samples = data.astype(np.float64)
    return samples, int(rate)


def bind_production_analyze(
    *,
    calibration: Any = None,
    geometry: Any = None,
) -> Callable[[Any, bytes, Any], Any]:
    """The real ``analyze`` seam: WAV bytes → ``analyze_program_capture``."""
    from jasper.audio_measurement.program_analysis import analyze_program_capture

    def _analyze(program: Any, wav: bytes, priors: Any) -> Any:
        samples, rate = _wav_bytes_to_samples(wav)
        return analyze_program_capture(
            program,
            samples,
            rate,
            calibration=calibration,
            geometry=geometry,
            priors=priors,
        )

    return _analyze


def open_v2_evidence_store(topology: Any) -> tuple[Any, str]:
    """Open a fresh v2 commissioning bundle + its exact evidence store (§5.6).

    Every v2 measurement session gets its own retention-bounded bundle under
    ``sessions_dir()`` (the same SC-4 bundle machinery the legacy flow uses),
    and every phase artifact is published through the store's write-once +
    tamper-checked-reopen path. Returns ``(store, bundle_session_id)``.
    """
    from jasper.active_speaker.bundles import open_bundle
    from jasper.active_speaker.commissioning_evidence_store import (
        CommissioningEvidenceStore,
    )

    info = open_bundle(topology, calibration_id="")
    if not isinstance(info, Mapping) or not info.get("session_id"):
        raise CrossoverV2Refused(
            "could not open a commissioning evidence bundle for this session"
        )
    session_id = str(info["session_id"])
    store = CommissioningEvidenceStore.open(
        Path(str(info["bundle_dir"])), expected_session_id=session_id
    )
    return store, session_id


def bind_evidence_publishers(
    store: Any, relay_session_id: str
) -> tuple[Callable[[Any, Mapping[str, Any]], None], Callable[[Any], None], dict[str, Any]]:
    """Real ``publish_check`` / ``publish_candidate`` seams (§5.6).

    CHECK publishes the ambient report + solved gain plan; MEASURE publishes
    the full candidate dict and re-opens it through
    ``MeasuredCrossoverCandidate.from_mapping`` — the same tamper check the
    apply path runs, so a candidate that cannot survive exact reopen never
    becomes reviewable. Artifact fingerprints land in the returned ``refs``
    mapping (persisted into the durable state for the status surface).
    """
    refs: dict[str, Any] = {"bundle_session_id": store.session_id}

    def publish_check(gain_plan: Any, ambient_report: Mapping[str, Any]) -> None:
        artifact = store.publish_json_artifact(
            f"crossover_v2/{relay_session_id}/check.json",
            {
                "schema_version": 1,
                "kind": "jts_crossover_v2_check_evidence",
                "relay_session_id": relay_session_id,
                "gain_plan_db": dict(gain_plan.gain_db),
                "predicted_peak_dbfs": gain_plan.predicted_peak_dbfs,
                "snr_floor_ok": gain_plan.snr_floor_ok,
                "ambient_report": dict(ambient_report),
            },
        )
        refs["check_artifact"] = artifact.fingerprint

    def publish_candidate(candidate: Any) -> None:
        from jasper.active_speaker.measured_crossover_candidate import (
            MeasuredCrossoverCandidate,
        )

        artifact = store.publish_json_artifact(
            f"crossover_v2/{relay_session_id}/candidate.json",
            candidate.to_dict(),
        )
        reopened_raw = store.reopen_json_artifact(artifact)
        reopened = MeasuredCrossoverCandidate.from_mapping(reopened_raw)
        if reopened.fingerprint != candidate.fingerprint:
            raise RuntimeError(
                "published measured candidate changed on exact readback"
            )
        refs["candidate_artifact"] = artifact.fingerprint
        log_event(
            logger,
            "correction.crossover_v2_candidate_published",
            relay_session_id=relay_session_id,
            candidate_fingerprint=candidate.fingerprint,
            artifact_fingerprint=artifact.fingerprint,
        )

    return publish_check, publish_candidate, refs


def bind_production_play(
    *,
    run_async: Any,
    camilla_factory: Any,
    evidence_store: Any,
    relay_session_id: str,
    topology: Any,
    preset: Any,
    role_channels: Mapping[str, int],
    playback_device: str,
    safety_profile: Mapping[str, Any],
    role_targets: Mapping[str, str],
    session_volume_db: float,
    config_dir: str = "/etc/camilladsp",
) -> Callable[[str, Any], None]:
    """The real ``play`` seam: program WAV → admitted playback through the DSP.

    CHECK/MEASURE render + publish the program WAV into the session's evidence
    bundle, emit the channel-routed program graph
    (``emit_active_speaker_program_config``), and ride
    :func:`jasper.active_speaker.program_playback.play_program` with the
    CamillaController seams from ``bind_program_playback_seams``; VERIFY plays
    its mono WAV through the APPLIED production graph (verified-aplay only —
    no graph load). Both run inside the mux measurement window so the
    correction lane actually reaches the speaker.

    ON-DEVICE: not exercised hardware-free; W6 validates acoustically.
    """
    from jasper.active_speaker.crossover_v2_flow import (
        PHASE_VERIFY,
        bind_program_playback_seams,
    )
    from jasper.audio_measurement.program import write_program_wav

    def _play(phase: str, program: Any) -> None:
        bundle_dir = evidence_store.bundle_dir
        wav_rel = f"crossover_v2/{relay_session_id}/{phase}_program.wav"
        wav_path = Path(bundle_dir) / wav_rel
        wav_path.parent.mkdir(parents=True, exist_ok=True)
        write_program_wav(wav_path, program)
        artifact = evidence_store.identify_artifact(wav_rel)

        async def _emit() -> None:
            from jasper.active_speaker.program_playback import (
                verified_program_aplay,
            )
            from jasper.correction import coordinator

            async with coordinator.measurement_window():
                if phase == PHASE_VERIFY:
                    # The applied production graph IS the system under test —
                    # no graph load, just the verified WAV into the lane.
                    await verified_program_aplay(
                        bundle_dir, artifact, timeout_s=60.0
                    )
                    return
                from jasper.active_speaker.camilla_yaml import (
                    emit_active_speaker_program_config,
                )
                from jasper.active_speaker.program_playback import play_program

                program_yaml = emit_active_speaker_program_config(
                    preset,
                    role_channels=dict(role_channels),
                    playback_device=playback_device,
                )
                seams = bind_program_playback_seams(
                    camilla_factory(),
                    bundle_dir=str(bundle_dir),
                    artifact=artifact,
                    config_dir=config_dir,
                    program=program,
                    wav_path=str(wav_path),
                    topology=topology,
                    safety_profile=safety_profile,
                    role_targets=role_targets,
                    session_volume_db=session_volume_db,
                )
                await play_program(
                    program,
                    program_graph_yaml=program_yaml,
                    session_volume_plan=session_volume_plan(),
                    **seams,
                )

        run_async(_emit())

    return _play


# --------------------------------------------------------------------------- #
# the plan-runner host (S1a/S1c)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class V2VolumeHooks:
    """The session-volume lifecycle the runner drives (§5.5)."""

    open: Callable[[], Any]      # async
    close: Callable[[], Any]     # async
    abandon: Callable[[], Any]   # async


def build_v2_run_and_consume(
    conductor: Any,
    *,
    volume: V2VolumeHooks,
    stop_event: threading.Event,
    stop_lock: Any,
    evidence_refs: Mapping[str, Any] | None = None,
    poll_interval_s: float | None = None,
    timeout_s: float | None = None,
) -> Callable[[Any, Any], Any]:
    """The async ``run_and_consume(client, pi_session)`` for one v2 session.

    Mirrors ``build_crossover_relay_plan_run_and_consume``'s thread model: the
    REAL :func:`jasper.capture_relay.session.run_capture_plan` runs on a
    worker thread (``asyncio.to_thread``), the awaiting task shields it
    through cancellation (Stop drains the runner before purging), and the
    relay session is purged on every exit path.

    Host-owned error mapping (S1c):

    * ``CaptureTimeout`` / ``CaptureAborted`` / ``RelayError`` / ``OSError`` /
      generic ``CaptureFailed`` — relay-session death ⇒ ``relay_timeout``
      failure state + volume ABANDON (the §5.5 walked-away guarantee).
    * ``CaptureBeginRefused`` — the conductor already recorded the phase's own
      failure code; persist it + abandon.
    * ``CaptureStopped`` / cancellation — expected control flow: abandon the
      volume, no failure code.
    * Plan complete with VERIFY accepted ⇒ CLOSE (exact restore); a completed
      plan that did not reach done (attempt budget exhausted) abandons.
    """

    async def _run_and_consume(client: Any, pi_session: Any) -> None:
        from jasper.capture_relay.client import RelayError
        from jasper.capture_relay.session import (
            CaptureAborted,
            CaptureBeginRefused,
            CaptureFailed,
            CaptureStopped,
            CaptureTimeout,
            purge,
            run_capture_plan,
        )
        from jasper.active_speaker.crossover_v2_flow import (
            PHASE_DONE,
            REASON_RELAY_TIMEOUT,
        )

        def authorize(index: int, attempt: int, entry: Any = None) -> None:
            with stop_lock:
                if stop_event.is_set():
                    raise CaptureStopped("capture stopped")
            conductor.authorize_begin(index, attempt, entry)

        def on_armed(state: Any) -> None:
            if stop_event.is_set():
                raise CaptureStopped("capture stopped")
            conductor.on_armed(state)

        def consume(index: int, attempt: int, result: Any, entry: Any = None):
            verdict = conductor.consume_capture(index, attempt, result, entry)
            code = verdict.get("code") if isinstance(verdict, Mapping) else None
            persist_conductor_state(
                conductor,
                failure_code=code if not verdict.get("accepted") else None,
                evidence=evidence_refs,
            )
            return verdict

        async def _purge_best_effort() -> None:
            try:
                await asyncio.to_thread(purge, client, pi_session)
            except (OSError, RuntimeError, ValueError):
                logger.warning("v2 relay purge failed", exc_info=True)

        async def _abandon_best_effort() -> None:
            try:
                await volume.abandon()
            except (OSError, RuntimeError, ValueError):
                log_event(
                    logger,
                    "correction.crossover_v2_volume_abandon_failed",
                    level=logging.CRITICAL,
                )

        opened = await volume.open()
        opened_value = getattr(opened, "value", opened)
        if opened is not None and str(opened_value) != "opened":
            # The plan drained itself (emergency attenuation / failure); the
            # recovery screen keys on needs_recovery via the status block.
            raise CaptureFailed(
                "the fixed measurement volume could not be confirmed"
            )
        plan_kwargs: dict[str, Any] = {}
        if poll_interval_s is not None:
            plan_kwargs["poll_interval_s"] = poll_interval_s
        if timeout_s is not None:
            plan_kwargs["timeout_s"] = timeout_s
        capture_task = asyncio.create_task(
            asyncio.to_thread(
                run_capture_plan,
                client,
                pi_session,
                authorize_begin=authorize,
                on_armed=on_armed,
                consume_capture=consume,
                stop_requested=stop_event.is_set,
                **plan_kwargs,
            )
        )
        try:
            try:
                await asyncio.shield(capture_task)
            except asyncio.CancelledError:
                stop_event.set()
                while not capture_task.done():
                    try:
                        await asyncio.shield(capture_task)
                    except asyncio.CancelledError:
                        continue
                    except (OSError, RuntimeError, ValueError):
                        break
                if capture_task.done() and not capture_task.cancelled():
                    capture_task.exception()
                await _abandon_best_effort()
                await _purge_best_effort()
                raise
        except CaptureStopped:
            await _abandon_best_effort()
            await _purge_best_effort()
            raise
        except CaptureBeginRefused:
            # The conductor's own budget refusal — its failure code is already
            # in _last_reason; persist the terminal state.
            code = conductor.last_failure_code or REASON_RELAY_TIMEOUT
            _persist_terminal_failure(conductor, code)
            await _abandon_best_effort()
            await _purge_best_effort()
            raise
        except (CaptureTimeout, CaptureAborted, CaptureFailed, RelayError, OSError):
            # Relay-session death (§5.10): relay_timeout ⇒ session restart; the
            # walked-away user's volume is always drained.
            _persist_terminal_failure(conductor, REASON_RELAY_TIMEOUT)
            await _abandon_best_effort()
            await _purge_best_effort()
            raise
        # Plan finished without a transport failure.
        done = conductor.current_phase == PHASE_DONE
        persist_conductor_state(
            conductor,
            failure_code=None if done else conductor.last_failure_code,
            evidence=evidence_refs,
        )
        if done:
            try:
                await volume.close()
            except (OSError, RuntimeError, ValueError):
                log_event(
                    logger,
                    "correction.crossover_v2_volume_close_failed",
                    level=logging.CRITICAL,
                )
        else:
            await _abandon_best_effort()
        await _purge_best_effort()

    return _run_and_consume


# --------------------------------------------------------------------------- #
# conductor context resolution (production inputs)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class V2ConductorContext:
    """Everything the production conductor needs, resolved from live status."""

    preset: Any
    roles_bands: tuple
    fc_hz: float
    driver_caps_dbfs: dict[str, float]
    role_targets: dict[str, str]
    safety_profile: Mapping[str, Any]
    session_volume_db: float
    driver_spacing_m: float
    topology: Any
    playback_device: str
    role_channels: dict[str, int]


def resolve_conductor_context(status: Mapping[str, Any]) -> V2ConductorContext:
    """Resolve preset/bands/caps/targets/volume from live status + topology.

    Fail-closed: every missing input is a :class:`CrossoverV2Refused` naming
    what to finish first — never a guessed default.
    """
    from jasper.active_speaker.commission_wiring import resolve_capture_preset
    from jasper.active_speaker.crossover_v2_flow import derive_session_volume_db
    from jasper.active_speaker.design_draft import load_design_draft
    from jasper.active_speaker.excitation_safety_plan import (
        ExcitationSafetyPlanError,
        resolve_driver_excitation_ceilings,
    )
    from jasper.audio_measurement.program import RoleBand
    from jasper.output_topology import load_output_topology

    if not status.get("active"):
        raise CrossoverV2Refused(
            "this speaker has no active crossover to measure"
        )
    setup = status.get("setup")
    if not isinstance(setup, Mapping) or setup.get("status") != "ready":
        raise CrossoverV2Refused(
            "protected speaker setup is not ready; finish it before measuring"
        )
    topology = load_output_topology()
    preset = resolve_capture_preset(topology)
    if preset.way_count != 2:
        raise CrossoverV2Refused("the v2 conductor flow is scoped to 2-way presets")
    draft = load_design_draft(topology=topology)
    safety_profile = draft.get("driver_safety_profile")
    if not isinstance(safety_profile, Mapping):
        raise CrossoverV2Refused(
            "driver safety limits are not confirmed; finish the driver details "
            "in speaker setup"
        )
    targets_raw = status.get("targets")
    drivers = (
        targets_raw.get("drivers") if isinstance(targets_raw, Mapping) else None
    ) or []
    role_targets: dict[str, str] = {}
    for target in drivers:
        if isinstance(target, Mapping):
            role = str(target.get("role") or "").lower()
            fingerprint = str(target.get("target_fingerprint") or "")
            if role and fingerprint:
                role_targets[role] = fingerprint
    if set(role_targets) != {"woofer", "tweeter"}:
        raise CrossoverV2Refused(
            "the woofer and tweeter measurement targets are not both active"
        )
    roles_bands = []
    caps: dict[str, float] = {}
    for channel, role in enumerate(("woofer", "tweeter")):
        try:
            band, cap = resolve_driver_excitation_ceilings(
                safety_profile, role_targets[role]
            )
        except (ExcitationSafetyPlanError, ValueError) as exc:
            raise CrossoverV2Refused(
                f"the {role}'s safe excitation limits could not be resolved"
            ) from exc
        roles_bands.append(RoleBand(role, channel, band))
        caps[role] = float(cap)
    region = preset.crossover_regions[0]
    fc_hz = float(region.fc_hz)
    session_volume_db = derive_session_volume_db(
        safety_profile, [role_targets["woofer"], role_targets["tweeter"]]
    )
    playback_device = str(getattr(topology, "playback_device", None) or "")
    if not playback_device:
        raise CrossoverV2Refused(
            "the active output device is not declared; finish speaker setup"
        )
    return V2ConductorContext(
        preset=preset,
        roles_bands=tuple(roles_bands),
        fc_hz=fc_hz,
        driver_caps_dbfs=caps,
        role_targets=role_targets,
        safety_profile=safety_profile,
        session_volume_db=session_volume_db,
        driver_spacing_m=0.0,  # W6: thread the declared driver spacing (see report)
        topology=topology,
        playback_device=playback_device,
        role_channels={"woofer": 0, "tweeter": 1},
    )


# --------------------------------------------------------------------------- #
# endpoint preparation (S1a/S1d)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class V2PreparedSession:
    """What the correction_setup dispatch needs to host one v2 relay session."""

    label: str
    open: Callable[..., Any]
    run_and_consume: Callable[[Any, Any], Any]
    request_stop: Callable[[], None]


def _volume_hooks(run_async_unused: Any, camilla_factory: Any, context: V2ConductorContext) -> V2VolumeHooks:
    plan = session_volume_plan()

    async def _get() -> Any:
        return await camilla_factory().get_volume_db(best_effort=False)

    async def _set(db: float) -> Any:
        return await camilla_factory().set_volume_db(db, best_effort=False)

    async def _open() -> Any:
        return await plan.open(context.session_volume_db, _set, _get)

    async def _close() -> Any:
        return await plan.close(_set, _get)

    async def _abandon() -> Any:
        return await plan.abandon(_set, _get)

    return V2VolumeHooks(open=_open, close=_close, abandon=_abandon)


def prepare_v2_session(
    raw: Mapping[str, Any],
    *,
    status: Mapping[str, Any],
    run_async: Any,
    camilla_factory: Any,
) -> V2PreparedSession:
    """Prepare the ``POST /crossover/v2/session`` relay hosting (S1a).

    Gates (fail-closed, before any relay registration): the flow selector, the
    volume-recovery gate (``needs_recovery`` — the W2 ruling), and the
    conductor-context resolution. Hydration (S1d): the durable state is
    hydrated through :meth:`CrossoverV2Conductor.hydrate` with the NEW relay
    session id — a prior session's CHECK/MEASURE evidence is invalidated per
    §5.6 (and logged); the fresh session starts at CHECK.
    """
    from jasper.active_speaker.crossover_v2_flow import (
        CrossoverV2Conductor,
        V2ConductorSnapshot,
        V2FlowSeams,
        build_v2_session_spec,
    )
    from jasper.capture_relay import correction_adapter

    if not v2_flow_active():
        raise CrossoverV2Refused(
            "the v2 crossover flow is not active — set JASPER_CROSSOVER_FLOW=v2"
        )
    if session_volume_plan().needs_recovery:
        raise CrossoverV2Refused(
            "the measurement volume needs recovery; recover it before starting "
            "a new session"
        )
    context = resolve_conductor_context(status)
    evidence_store, _bundle_id = open_v2_evidence_store(context.topology)
    acknowledgement_binding = secrets.token_urlsafe(24)
    stop_event = threading.Event()
    stop_lock = threading.Lock()

    prior_raw = load_v2_state()
    prior_snapshot = (
        V2ConductorSnapshot(
            session_id=str(prior_raw.get("session_id") or ""),
            accepted_phases=tuple(prior_raw.get("accepted_phases") or ()),
            applied=bool(prior_raw.get("applied")),
            gain_plan_db=prior_raw.get("gain_plan_db"),
        )
        if isinstance(prior_raw, Mapping)
        else None
    )

    holder: dict[str, Any] = {}

    def _open(client: Any, base: str, capture_origin: str, return_url: str) -> Any:
        spec = build_v2_session_spec(
            context.roles_bands,
            context.fc_hz,
            acknowledgement_binding=acknowledgement_binding,
        ).with_return_url(return_url)
        rc = correction_adapter.open_capture(
            client,
            spec,
            relay_base=base,
            capture_origin=capture_origin,
            return_url=return_url,
        )
        # The conductor + publishers bind to the MINTED relay session id.
        relay_session_id = rc.pi_session.session_id
        publish_check, publish_candidate, refs = bind_evidence_publishers(
            evidence_store, relay_session_id
        )
        play = bind_production_play(
            run_async=run_async,
            camilla_factory=camilla_factory,
            evidence_store=evidence_store,
            relay_session_id=relay_session_id,
            topology=context.topology,
            preset=context.preset,
            role_channels=context.role_channels,
            playback_device=context.playback_device,
            safety_profile=context.safety_profile,
            role_targets=context.role_targets,
            session_volume_db=context.session_volume_db,
        )
        conductor = CrossoverV2Conductor.hydrate(
            prior_snapshot,
            session_id=relay_session_id,
            source_preset=context.preset,
            roles_bands=context.roles_bands,
            fc_hz=context.fc_hz,
            driver_caps_dbfs=context.driver_caps_dbfs,
            session_volume_db=context.session_volume_db,
            seams=V2FlowSeams(
                play=play,
                analyze=bind_production_analyze(),
                publish_check=publish_check,
                publish_candidate=publish_candidate,
                apply_complete=_applied_gate,
            ),
            driver_spacing_m=context.driver_spacing_m,
        )
        persist_conductor_state(conductor, failure_code=None, evidence=refs)
        holder["run"] = build_v2_run_and_consume(
            conductor,
            volume=_volume_hooks(run_async, camilla_factory, context),
            stop_event=stop_event,
            stop_lock=stop_lock,
            evidence_refs=refs,
        )
        return rc

    async def _run(client: Any, pi_session: Any) -> None:
        await holder["run"](client, pi_session)

    def _request_stop() -> None:
        with stop_lock:
            stop_event.set()

    return V2PreparedSession(
        label=V2_RELAY_KIND_SESSION,
        open=_open,
        run_and_consume=_run,
        request_stop=_request_stop,
    )


def prepare_v2_verify(
    raw: Mapping[str, Any],
    *,
    status: Mapping[str, Any],
    run_async: Any,
    camilla_factory: Any,
) -> V2PreparedSession:
    """Prepare ``POST /crossover/v2/verify`` — the §5.2 re-verify re-arm.

    Requires a durable post-apply state (MEASURE accepted + applied). Opens a
    NEW relay session hosting a 1-entry verify-only plan; the conductor is
    rebuilt in verify-only mode (CHECK/MEASURE marked accepted, applied,
    verify priors rehydrated from the durable state), with relay index 1
    mapped to VERIFY.
    """
    import numpy as np

    from jasper.active_speaker.crossover_v2_flow import (
        PHASE_CHECK,
        PHASE_MEASURE,
        PHASE_VERIFY,
        CrossoverV2Conductor,
        V2FlowSeams,
        build_v2_verify_session_spec,
    )
    from jasper.capture_relay import correction_adapter

    if not v2_flow_active():
        raise CrossoverV2Refused(
            "the v2 crossover flow is not active — set JASPER_CROSSOVER_FLOW=v2"
        )
    if session_volume_plan().needs_recovery:
        raise CrossoverV2Refused(
            "the measurement volume needs recovery; recover it before verifying"
        )
    state = load_v2_state()
    if not state or not state.get("applied"):
        raise CrossoverV2Refused(
            "verification needs an applied measured crossover; measure and "
            "apply first"
        )
    context = resolve_conductor_context(status)
    evidence_store, _bundle_id = open_v2_evidence_store(context.topology)
    priors_raw = state.get("verify_priors") or {}
    sum_raw = priors_raw.get("predicted_sum") if isinstance(priors_raw, Mapping) else None
    predicted_sum = None
    if isinstance(sum_raw, Mapping) and sum_raw.get("freqs_hz"):
        predicted_sum = (
            np.asarray(sum_raw["freqs_hz"], dtype=float),
            np.asarray(sum_raw["magnitude_db"], dtype=float),
        )
    gate_ms = (
        priors_raw.get("gate_window_ms") if isinstance(priors_raw, Mapping) else None
    )
    acknowledgement_binding = secrets.token_urlsafe(24)
    stop_event = threading.Event()
    stop_lock = threading.Lock()
    holder: dict[str, Any] = {}

    def _open(client: Any, base: str, capture_origin: str, return_url: str) -> Any:
        spec = build_v2_verify_session_spec(
            context.fc_hz,
            acknowledgement_binding=acknowledgement_binding,
        ).with_return_url(return_url)
        rc = correction_adapter.open_capture(
            client,
            spec,
            relay_base=base,
            capture_origin=capture_origin,
            return_url=return_url,
        )
        relay_session_id = rc.pi_session.session_id
        _publish_check, publish_candidate, refs = bind_evidence_publishers(
            evidence_store, relay_session_id
        )
        play = bind_production_play(
            run_async=run_async,
            camilla_factory=camilla_factory,
            evidence_store=evidence_store,
            relay_session_id=relay_session_id,
            topology=context.topology,
            preset=context.preset,
            role_channels=context.role_channels,
            playback_device=context.playback_device,
            safety_profile=context.safety_profile,
            role_targets=context.role_targets,
            session_volume_db=context.session_volume_db,
        )
        conductor = CrossoverV2Conductor(
            session_id=relay_session_id,
            source_preset=context.preset,
            roles_bands=context.roles_bands,
            fc_hz=context.fc_hz,
            driver_caps_dbfs=context.driver_caps_dbfs,
            session_volume_db=context.session_volume_db,
            seams=V2FlowSeams(
                play=play,
                analyze=bind_production_analyze(),
                publish_check=_publish_check,
                publish_candidate=publish_candidate,
                apply_complete=_applied_gate,
            ),
            driver_spacing_m=context.driver_spacing_m,
            accepted_phases=(PHASE_CHECK, PHASE_MEASURE),
            applied=True,
            gain_plan_db=state.get("gain_plan_db"),
            index_phase_map={1: PHASE_VERIFY},
            measure_predicted_sum=predicted_sum,
            measure_gate_window_ms=(
                float(gate_ms) if isinstance(gate_ms, (int, float)) else None
            ),
        )
        # Keep the durable candidate/applied facts; rebind the session id.
        persist_conductor_state(conductor, failure_code=None, evidence=refs)
        holder["run"] = build_v2_run_and_consume(
            conductor,
            volume=_volume_hooks(run_async, camilla_factory, context),
            stop_event=stop_event,
            stop_lock=stop_lock,
            evidence_refs=refs,
        )
        return rc

    async def _run(client: Any, pi_session: Any) -> None:
        await holder["run"](client, pi_session)

    def _request_stop() -> None:
        with stop_lock:
            stop_event.set()

    return V2PreparedSession(
        label=V2_RELAY_KIND_VERIFY,
        open=_open,
        run_and_consume=_run,
        request_stop=_request_stop,
    )


# --------------------------------------------------------------------------- #
# apply (the existing baseline transaction, W4 seam)
# --------------------------------------------------------------------------- #


def handle_v2_apply(
    raw: Mapping[str, Any],
    run_async: Any,
    camilla_factory: Any,
) -> dict[str, Any]:
    """POST /crossover/v2/apply — apply the reviewed measured candidate.

    Reopens the published candidate artifact through
    ``MeasuredCrossoverCandidate.from_mapping`` (the tamper check), gates on
    the reviewed ``expected_candidate_fingerprint``, and rides the EXISTING
    atomic apply-with-rollback transaction —
    ``apply_baseline_profile(measured_candidate=...)`` (the W4 seam) — then
    marks the durable v2 state applied, which arms the deferred VERIFY.
    """
    from jasper.active_speaker.baseline_profile import apply_baseline_profile
    from jasper.active_speaker.crossover_preview import load_crossover_preview
    from jasper.active_speaker.design_draft import load_design_draft
    from jasper.active_speaker.measured_crossover_candidate import (
        MeasuredCrossoverCandidate,
        MeasuredCrossoverCandidateError,
    )
    from jasper.active_speaker.measurement import load_measurement_state
    from jasper.output_topology import load_output_topology

    if not v2_flow_active():
        raise CrossoverV2Refused(
            "the v2 crossover flow is not active — set JASPER_CROSSOVER_FLOW=v2"
        )
    expected = str(raw.get("expected_candidate_fingerprint") or "")
    if not expected:
        raise CrossoverV2Refused("expected_candidate_fingerprint is required")
    state = load_v2_state()
    candidate_ref = (state or {}).get("candidate")
    evidence = (state or {}).get("evidence") or {}
    if not isinstance(candidate_ref, Mapping) or not candidate_ref.get("fingerprint"):
        raise CrossoverV2Refused(
            "no measured crossover candidate is ready to apply; measure first"
        )
    if str(candidate_ref["fingerprint"]) != expected:
        raise CrossoverV2Refused(
            "the reviewed crossover is no longer current; review the newest "
            "measurement before applying"
        )
    candidate_payload = raw.get("candidate")
    if not isinstance(candidate_payload, Mapping):
        # Endpoint contract: the wizard posts only the fingerprint; the host
        # reopens the artifact from the evidence bundle recorded at publish.
        candidate_payload = _reopen_candidate_artifact(state, evidence)
    try:
        candidate = MeasuredCrossoverCandidate.from_mapping(candidate_payload)
    except MeasuredCrossoverCandidateError as exc:
        raise CrossoverV2Refused(str(exc)) from exc
    if candidate.fingerprint != expected:
        raise CrossoverV2Refused(
            "the persisted candidate does not match the reviewed fingerprint"
        )
    topology = load_output_topology()
    draft = load_design_draft(topology=topology)
    preview = load_crossover_preview(current_design_draft=draft)
    measurements = load_measurement_state(topology)
    cam = camilla_factory()
    payload = run_async(
        apply_baseline_profile(
            topology,
            design_draft=draft,
            crossover_preview=preview,
            measurements=measurements,
            load_config=lambda path: cam.set_config_file_path(
                path, best_effort=False
            ),
            get_current_config_path=lambda: cam.get_config_file_path(
                best_effort=False
            ),
            tuning_owner="automatic",
            expected_candidate_fingerprint=expected,
            measured_candidate=candidate,
        )
    )
    if payload.get("status") == "applied":
        observe_apply_success(expected)
    log_event(
        logger,
        "correction.crossover_v2_apply",
        status=payload.get("status"),
        candidate_fingerprint=expected,
    )
    return payload


def _reopen_candidate_artifact(
    state: Mapping[str, Any] | None, evidence: Mapping[str, Any]
) -> Mapping[str, Any]:
    """Reopen the published candidate JSON from the session's evidence bundle."""
    from jasper.active_speaker.bundles import sessions_dir

    session_id = str((state or {}).get("session_id") or "")
    bundle_session = str(evidence.get("bundle_session_id") or "")
    candidates = []
    root = sessions_dir()
    try:
        bundle_dirs = (
            [root / bundle_session] if bundle_session else sorted(root.iterdir())
        )
    except OSError:
        bundle_dirs = []
    for bundle in bundle_dirs:
        path = (
            bundle / "evidence" / "v1" / "artifacts" / "crossover_v2"
            / session_id / "candidate.json"
        )
        if path.is_file():
            candidates.append(path)
    if not candidates:
        raise CrossoverV2Refused(
            "the published measured candidate could not be found; measure again"
        )
    try:
        return json.loads(candidates[-1].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CrossoverV2Refused(
            "the published measured candidate could not be reopened"
        ) from exc
