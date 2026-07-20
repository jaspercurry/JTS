# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The v2 crossover conductor's web host (Wave 5a endpoint binding).

Owns everything between the ``/correction/crossover/v2/*`` POST routes (thin
dispatch branches in :mod:`jasper.web.correction_setup`) and the pure conductor
(:mod:`jasper.active_speaker.crossover_v2_flow`):

* the **durable v2 flow state** (one JSON file) that ``status_payload`` threads
  into the envelope as ``status["crossover_v2"]`` — phase / candidate / verify
  / failure / apply_blocked / needs_recovery / applied;
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
import concurrent.futures
import copy
import hashlib
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from jasper.atomic_io import atomic_write_text
from jasper.active_speaker.crossover_v2_flow import (
    REASON_STATE_TRANSACTION_INTERRUPTED,
    REASON_STATE_TRANSACTION_RECOVERY_REQUIRED,
)
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

# Keep five recent, non-live candidate configs after every terminal v2 Apply
# attempt. This covers a short debugging/recovery history while bounding files
# emitted by success, block, transactional failure, or raised error; the applied
# config, CamillaDSP's reported live path, and the pre-apply Undo stash are
# additional protected files and never consume this allowance.
V2_CANDIDATE_CONFIG_KEEP_RECENT = 5

REVIEW_HOLD_TIMED_OUT_CODE = "review_hold_timed_out"
REVIEW_HOLD_TIMED_OUT_MESSAGE = (
    "The review wait timed out. Return to the speaker page and start the "
    "measurement again."
)
TRANSACTION_INTERRUPTED_CODE = REASON_STATE_TRANSACTION_INTERRUPTED
TRANSACTION_RECOVERY_REQUIRED_CODE = REASON_STATE_TRANSACTION_RECOVERY_REQUIRED

_state_lock = threading.RLock()
_state_path_override: Path | None = None
_PROCESS_INSTANCE_ID = secrets.token_hex(16)


def _read_process_birth_id(pid: int) -> str:
    """Return Linux boot-id + proc start ticks, or empty when unprovable."""

    try:
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(
            encoding="ascii"
        ).strip()
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        # ``comm`` may contain spaces and parentheses; fields after its last
        # ')' begin at proc field 3 (state), and starttime is field 22.
        fields = stat.rsplit(")", 1)[1].strip().split()
        start_ticks = fields[19]
    except (IndexError, OSError, UnicodeError):
        return ""
    return f"{boot_id}:{start_ticks}" if boot_id and start_ticks else ""


_PROCESS_BIRTH_ID = _read_process_birth_id(os.getpid())

_volume_plan_lock = threading.Lock()
_volume_plan: Any = None


class CrossoverV2Refused(ValueError):
    """A v2 endpoint refusal (maps to HTTP 400 in the dispatch ladder)."""


class CrossoverV2LocalSeamError(RuntimeError):
    """A LOCAL play/analyze seam raised ``OSError`` — not a relay-transport death.

    W6 hardware run 3 finding G: the DSP writer lock's ``os.open`` on a
    read-only ``config_dir`` (finding F) raised a bare ``OSError`` from inside
    ``on_armed`` — the exact same exception TYPE
    ``jasper.capture_relay.session.run_capture_plan``'s transport polling
    loop raises on a genuine relay-connection failure (``client.status``
    reaching an unreachable host). ``build_v2_run_and_consume``'s relay-death
    except arm cannot tell those apart by type alone, so it misclassified a
    local filesystem fault as ``relay_timeout``. ``on_armed``/``consume``
    convert a local ``OSError`` to THIS type at the seam boundary before it
    can reach that arm, so it falls through to the catch-all cleanup arm's
    honest ``internal_error`` classification instead — a genuine transport
    ``OSError`` (never wrapped) still hits the relay-death arm unchanged.
    """


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
        state = dict(raw)
        reservation = _reservation_mapping(state)
        if reservation is not None and not _reservation_owner_alive(reservation):
            # A killed web process cannot release its durable reservation. Recover
            # a pre-mutation admission on the next read. Once mutation started the
            # reservation is a transaction journal: keep it fenced until a request
            # can compare durable Layer-A AND live Camilla identities.
            phase = str(reservation.get("phase") or "reserved")
            if phase in {"mutation_started", "recovery_required"}:
                log_event(
                    logger,
                    "correction.crossover_v2_stale_transaction_detected",
                    level=logging.WARNING,
                    operation=str(reservation.get("operation") or "apply"),
                    phase=phase,
                )
                return state

            # Detection and publication stay in the same state critical section.
            # Compare the token from the bytes just read before clearing it so a
            # newer reservation can never be erased by stale cleanup.
            stale_token = str(reservation.get("token") or "")
            try:
                current = json.loads(_state_path().read_text(encoding="utf-8"))
            except (FileNotFoundError, OSError, json.JSONDecodeError):
                return state
            if _reservation_token_value(current) != stale_token:
                return dict(current) if isinstance(current, Mapping) else None
            _apply_pending_terminal_failure(state)
            state["apply_reservation"] = None
            state["updated_at"] = time.time()
            try:
                atomic_write_text(
                    _state_path(),
                    json.dumps(state, indent=2, sort_keys=True) + "\n",
                    mode=0o640,
                    group_from_parent=True,
                    durable=True,
                )
            except OSError:
                log_event(
                    logger,
                    "correction.crossover_v2_stale_reservation_persist_failed",
                    level=logging.WARNING,
                )
            log_event(
                logger,
                "correction.crossover_v2_stale_reservation_recovered",
                operation=str(reservation.get("operation") or "apply"),
            )
        return state


def _reservation_mapping(
    state: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    reservation = (
        state.get("apply_reservation") if isinstance(state, Mapping) else None
    )
    return reservation if isinstance(reservation, Mapping) else None


def _reservation_token_value(state: Mapping[str, Any] | None) -> str:
    reservation = _reservation_mapping(state)
    return str(reservation.get("token") or "") if reservation is not None else ""


def _reservation_owner_alive(reservation: Mapping[str, Any]) -> bool:
    owner_instance = str(reservation.get("owner_instance") or "")
    if owner_instance == _PROCESS_INSTANCE_ID:
        return True
    try:
        owner_pid = int(reservation.get("owner_pid"))
        if owner_pid <= 0:
            return False
        os.kill(owner_pid, 0)
    except (ProcessLookupError, TypeError, ValueError):
        return False
    except PermissionError:
        pass  # Another live uid-owned process is still authoritative.
    expected_birth = str(reservation.get("owner_birth_id") or "")
    actual_birth = _read_process_birth_id(owner_pid)
    # PID existence alone is not ownership: after reboot/reuse it may name an
    # unrelated process. If birth identity cannot be proved, fail closed toward
    # transaction reconciliation rather than trusting the reused PID.
    return bool(expected_birth and actual_birth == expected_birth)


def _apply_reservation_token(state: Mapping[str, Any] | None) -> str:
    reservation = _reservation_mapping(state)
    if reservation is None:
        return ""
    token = str(reservation.get("token") or "")
    if not token or not str(reservation.get("owner_instance") or ""):
        return ""
    return token if _reservation_owner_alive(reservation) else ""


def _reservation_payload(token: str, **fields: Any) -> dict[str, Any]:
    return {
        "token": token,
        "owner_pid": os.getpid(),
        "owner_instance": _PROCESS_INSTANCE_ID,
        "owner_birth_id": _PROCESS_BIRTH_ID,
        **fields,
    }


def _pending_terminal_failure_code(state: Mapping[str, Any] | None) -> str:
    pending = (
        state.get("pending_terminal_failure")
        if isinstance(state, Mapping)
        else None
    )
    return str(pending.get("code") or "") if isinstance(pending, Mapping) else ""


def _apply_pending_terminal_failure(state: dict[str, Any]) -> None:
    """Reconcile a relay death serialized behind an Apply/Undo reservation."""

    code = _pending_terminal_failure_code(state)
    state["pending_terminal_failure"] = None
    if not code:
        return
    state["failure"] = {"code": code}
    state["apply_blocked"] = None
    if _candidate_applied_for_session(
        state, str(state.get("session_id") or "")
    ):
        return
    state["accepted_phases"] = []
    state["gain_plan_db"] = None
    state["candidate"] = None
    state["verify"] = None
    state["verify_priors"] = None
    state["evidence"] = None


def save_v2_state(
    state: Mapping[str, Any], *, reservation_token: str | None = None
) -> None:
    payload = {
        "schema_version": STATE_SCHEMA_VERSION,
        "kind": STATE_KIND,
        "updated_at": time.time(),
        **{k: v for k, v in state.items() if k not in {"schema_version", "kind"}},
    }
    with _state_lock:
        current = load_v2_state()
        current_token = _reservation_token_value(current)
        if current_token and current_token != str(reservation_token or ""):
            raise CrossoverV2Refused(
                "Apply is still finishing; wait for it to complete before "
                "starting over or beginning another measurement"
            )
        atomic_write_text(
            _state_path(),
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            mode=0o640,
            group_from_parent=True,
            durable=True,
        )


def clear_v2_state(*, reservation_token: str | None = None) -> None:
    with _state_lock:
        current_token = _reservation_token_value(load_v2_state())
        if current_token and current_token != str(reservation_token or ""):
            raise CrossoverV2Refused(
                "Apply is still finishing; wait for it to complete before "
                "starting over or beginning another measurement"
            )
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


def _profile_graph_fingerprint(profile: Mapping[str, Any]) -> str:
    """Normalize one sha-pinned compiled profile into Camilla active-raw identity."""

    from jasper.active_speaker.baseline_profile import (
        normalized_camilla_graph_fingerprint,
    )

    config = profile.get("config")
    if not isinstance(config, Mapping):
        raise ValueError("profile config identity is missing")
    path = Path(str(config.get("path") or ""))
    expected_sha = str(config.get("sha256") or "")
    raw = path.read_bytes()
    if not expected_sha or hashlib.sha256(raw).hexdigest() != expected_sha:
        raise ValueError("profile config sha256 does not match")
    return normalized_camilla_graph_fingerprint(raw.decode("utf-8"))


def _candidate_applied_session_id(state: Mapping[str, Any] | None) -> str:
    """Return the session whose measured candidate owns the applied graph.

    ``applied`` is a physical speaker fact and intentionally survives Start over
    so Undo remains reachable. It is not proof that a newly minted Re-measure
    session's candidate was applied. Pre-W5b records had no explicit binding;
    their current session is the only safe additive migration when they still
    carry a candidate.
    """

    if not isinstance(state, Mapping) or state.get("applied") is not True:
        return ""
    explicit = state.get("applied_session_id")
    if explicit is not None:
        return str(explicit or "")
    if isinstance(state.get("candidate"), Mapping):
        return str(state.get("session_id") or "")
    return ""


def _candidate_applied_for_session(
    state: Mapping[str, Any] | None,
    session_id: str,
) -> bool:
    return bool(
        session_id
        and isinstance(state, Mapping)
        and state.get("applied") is True
        and _candidate_applied_session_id(state) == session_id
    )


def _profile_topology_fingerprint(profile: Mapping[str, Any]) -> str:
    snapshot = profile.get("recomposition_snapshot")
    return (
        str(snapshot.get("topology_fingerprint") or "")
        if isinstance(snapshot, Mapping)
        else ""
    )


def _snapshot_pre_apply_profile(
    profile: Mapping[str, Any] | None,
    active_raw: str,
) -> dict[str, Any] | None:
    """Freeze the exact predecessor graph into an immutable Undo target.

    Bass Extension is allowed to rewrite the selected Layer-A YAML in place.
    Its applied profile still describes the same recomposition inputs but may
    therefore carry the pre-rewrite config SHA. Apply calls this helper under
    the DSP writer lock, before its first load, so Undo retains the exact live
    predecessor bytes rather than that stale mutable path.
    """

    if not isinstance(profile, Mapping):
        return None
    config = profile.get("config")
    if not isinstance(config, Mapping):
        raise CrossoverV2Refused(
            "the current speaker profile has no config identity to retain for Undo"
        )
    raw = active_raw.encode("utf-8")
    sha256 = hashlib.sha256(raw).hexdigest()
    source_path = Path(str(config.get("path") or ""))
    if not source_path.name:
        raise CrossoverV2Refused(
            "the current speaker profile has no config path to retain for Undo"
        )
    from jasper.active_speaker.baseline_profile import (
        baseline_candidate_config_path,
        baseline_config_path,
    )

    configured_baseline = baseline_config_path()
    snapshot_path = baseline_candidate_config_path(
        configured_baseline,
        f"undo_{sha256}",
    )
    atomic_write_text(
        snapshot_path,
        active_raw,
        mode=0o640,
        group_from_parent=True,
        durable=True,
    )
    if snapshot_path.read_bytes() != raw:
        raise CrossoverV2Refused(
            "the current speaker graph could not be retained safely for Undo"
        )
    retained = copy.deepcopy(dict(profile))
    retained_config = dict(config)
    retained_config.update({
        "path": str(snapshot_path),
        "basename": snapshot_path.name,
        "exists": True,
        "sha256": sha256,
    })
    retained["config"] = retained_config
    return retained


async def _current_camilla_graph_fingerprint(cam: Any) -> str:
    from jasper.active_speaker.baseline_profile import (
        normalized_camilla_graph_fingerprint,
    )

    getter = getattr(cam, "get_active_config_raw", None)
    if not callable(getter):
        raise RuntimeError("CamillaDSP active graph readback is unavailable")
    raw = await getter(best_effort=False)
    return normalized_camilla_graph_fingerprint(raw)


async def _run_thread_action_to_completion(action: Callable[[], Any]) -> Any:
    """Drain blocking authority work before cancellation may release locks.

    ``asyncio.to_thread`` cannot stop its worker when the awaiting task is
    cancelled. VERIFY holds hardware authority around that worker, so its
    cancellation path must wait for the worker before surrounding lock
    contexts can exit.
    """

    task = asyncio.create_task(asyncio.to_thread(action))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        if not task.cancelled():
            task.exception()  # retrieve a worker failure before propagating cancel
        raise


def _run_with_v2_verify_authority(
    *,
    run_async: Any,
    camilla_factory: Any,
    session_id: str,
    expected_topology: Any,
    action: Callable[[], Any],
) -> Any:
    """Run one VERIFY boundary while its complete authority stays locked.

    VERIFY is acoustically meaningful only while the durable v2 binding,
    Layer-A profile, live Camilla graph, and output topology all identify the
    same applied candidate.  The topology and DSP-writer locks remain held
    across ``action`` itself, not merely across checks on either side: a writer
    therefore cannot install a different graph during the sweep and restore it
    before the post-check.  The result-consumption action includes the
    conductor verdict and durable publication, so acceptance is inside the
    same authority boundary too.
    """

    from jasper.active_speaker.baseline_profile import (
        load_applied_baseline_profile_state_strict,
        topology_config_fingerprint,
    )
    from jasper.dsp_apply import dsp_writer_lock
    from jasper.output_topology import (
        load_output_topology_strict,
        output_topology_mutation_lock,
    )

    expected_topology_fingerprint = topology_config_fingerprint(expected_topology)

    async def _check_and_run(current_topology_fingerprint: str) -> Any:
        async with dsp_writer_lock(
            _v2_dsp_lock_dir(), source="crossover_v2_verify_authority"
        ):
            async def _validate() -> None:
                with _state_lock:
                    current = load_v2_state()
                    if (
                        not current
                        or not _candidate_applied_for_session(current, session_id)
                        or _reservation_mapping(current) is not None
                    ):
                        raise CrossoverV2Refused(
                            "the applied crossover changed during verification; "
                            "refresh the page and review the current sound"
                        )
                    expected_profile_fingerprint = str(
                        current.get("applied_profile_fingerprint") or ""
                    )
                    expected_graph_fingerprint = str(
                        current.get("applied_live_graph_fingerprint") or ""
                    )
                try:
                    state_file_present, applied_profile = (
                        load_applied_baseline_profile_state_strict()
                    )
                    profile_fingerprint = _profile_candidate_identity(
                        applied_profile
                    )
                    profile_graph_fingerprint = (
                        _profile_graph_fingerprint(applied_profile)
                        if isinstance(applied_profile, Mapping)
                        else ""
                    )
                    profile_topology_fingerprint = (
                        _profile_topology_fingerprint(applied_profile)
                        if isinstance(applied_profile, Mapping)
                        else ""
                    )
                    live_graph_fingerprint = (
                        await _current_camilla_graph_fingerprint(camilla_factory())
                    )
                except (OSError, UnicodeError, RuntimeError, ValueError) as exc:
                    raise CrossoverV2Refused(
                        "the applied speaker graph could not be proved during "
                        "verification; refresh the page and review Speaker setup"
                    ) from exc
                if (
                    not state_file_present
                    or profile_fingerprint
                    != expected_profile_fingerprint
                    or profile_graph_fingerprint
                    != expected_graph_fingerprint
                    or live_graph_fingerprint != profile_graph_fingerprint
                    or not profile_topology_fingerprint
                    or profile_topology_fingerprint
                    != current_topology_fingerprint
                ):
                    raise CrossoverV2Refused(
                        "the applied speaker graph or output topology changed during "
                        "verification; refresh the page and measure again"
                    )

            await _validate()
            result = await _run_thread_action_to_completion(action)
            await _validate()
            return result

    with output_topology_mutation_lock():
        current_topology = load_output_topology_strict()
        current_topology_fingerprint = topology_config_fingerprint(current_topology)
        if current_topology_fingerprint != expected_topology_fingerprint:
            raise CrossoverV2Refused(
                "the output topology changed during verification; refresh the "
                "page and measure again"
            )
        return run_async(_check_and_run(current_topology_fingerprint))


def _v2_verify_authority_runner(
    *,
    run_async: Any,
    camilla_factory: Any,
    session_id: str,
    expected_topology: Any,
) -> Callable[[Callable[[], Any]], Any]:
    """Bind the shared automatic/manual VERIFY authority executor."""

    return lambda action: _run_with_v2_verify_authority(
        run_async=run_async,
        camilla_factory=camilla_factory,
        session_id=session_id,
        expected_topology=expected_topology,
        action=action,
    )


def _upgrade_legacy_undo_identity(state: Mapping[str, Any]) -> dict[str, Any]:
    """Derive additive W5b Undo identities only from matching Layer-A authority.

    Pre-W5b schema-1 state has the measured-candidate binding and retained
    predecessor but not the applied Layer-A/live-graph identities.  Migration
    is allowed only when the current applied profile still names that exact
    measured candidate and its config bytes still match the persisted sha.
    The live Camilla graph is checked against the derived graph identity later,
    inside Undo's DSP-writer boundary.
    """

    upgraded = dict(state)
    if (
        not upgraded.get("applied")
        or not isinstance(upgraded.get("pre_apply_profile"), Mapping)
    ):
        return upgraded
    if (
        upgraded.get("applied_profile_fingerprint")
        and upgraded.get("applied_live_graph_fingerprint")
    ):
        return upgraded
    candidate = upgraded.get("candidate")
    measured_fingerprint = (
        str(candidate.get("fingerprint") or "")
        if isinstance(candidate, Mapping)
        else ""
    )
    if not measured_fingerprint:
        return upgraded

    from jasper.active_speaker.baseline_profile import (
        baseline_candidate_fingerprint,
        load_applied_baseline_profile_state,
    )
    current = load_applied_baseline_profile_state()
    source = current.get("source") if isinstance(current, Mapping) else None
    if (
        not isinstance(current, Mapping)
        or not isinstance(source, Mapping)
        or str(source.get("measured_candidate_fingerprint") or "")
        != measured_fingerprint
        or not isinstance(current.get("recomposition_snapshot"), Mapping)
    ):
        return upgraded
    try:
        graph_fingerprint = _profile_graph_fingerprint(current)
    except (OSError, UnicodeError, RuntimeError, ValueError):
        return upgraded
    upgraded["applied_profile_fingerprint"] = baseline_candidate_fingerprint(current)
    upgraded["applied_live_graph_fingerprint"] = graph_fingerprint
    return upgraded


def _reserve_v2_apply(session_id: str, candidate_fingerprint: str) -> str:
    """Atomically reserve one reviewed state frontier through DSP + state commit."""

    from jasper.active_speaker.crossover_v2_flow import PHASE_CHECK, PHASE_MEASURE

    with _state_lock:
        state = load_v2_state()
        candidate = (state or {}).get("candidate")
        accepted = set((state or {}).get("accepted_phases") or ())
        if (
            state is None
            or _reservation_mapping(state) is not None
            or str(state.get("session_id") or "") != session_id
            or not isinstance(candidate, Mapping)
            or str(candidate.get("fingerprint") or "") != candidate_fingerprint
            or not {PHASE_CHECK, PHASE_MEASURE}.issubset(accepted)
            or state.get("failure")
            or _candidate_applied_for_session(state, session_id)
        ):
            raise CrossoverV2Refused(
                "this measurement session changed before Apply reached the "
                "speaker; start the measurement again"
            )
        token = secrets.token_hex(16)
        state["apply_reservation"] = _reservation_payload(
            token,
            operation="apply",
            phase="reserved",
            session_id=session_id,
            candidate_fingerprint=candidate_fingerprint,
        )
        # A fresh admitted attempt supersedes the explanation from an earlier
        # blocked Apply in this same review session.  Clear it at admission so
        # every later exit (apply_failed, exception, or mixed-state recovery)
        # cannot leave the page describing a failure that is no longer current.
        state["apply_blocked"] = None
        save_v2_state(state)
        return token


def _release_v2_apply_reservation(token: str) -> None:
    with _state_lock:
        state = load_v2_state()
        if state is None or _apply_reservation_token(state) != token:
            return
        _apply_pending_terminal_failure(state)
        state["apply_reservation"] = None
        save_v2_state(state, reservation_token=token)


def _reserve_v2_restore(state: Mapping[str, Any]) -> str:
    """Reserve the exact applied/Undo identities through DSP + state commit."""

    with _state_lock:
        current = load_v2_state()
        if (
            current is None
            or _reservation_mapping(current) is not None
            or not current.get("applied")
            or current.get("pre_apply_profile") != state.get("pre_apply_profile")
            or current.get("applied_profile_fingerprint")
            != state.get("applied_profile_fingerprint")
            or current.get("applied_live_graph_fingerprint")
            != state.get("applied_live_graph_fingerprint")
        ):
            raise CrossoverV2Refused(
                "the active crossover changed before Undo could start; review "
                "the current sound before restoring anything"
            )
        token = secrets.token_hex(16)
        current["apply_reservation"] = _reservation_payload(
            token,
            operation="restore",
            phase="reserved",
            applied_profile_fingerprint=current.get(
                "applied_profile_fingerprint"
            ),
        )
        save_v2_state(current)
        return token


def _profile_candidate_identity(profile: Mapping[str, Any] | None) -> str:
    if not isinstance(profile, Mapping) or not isinstance(
        profile.get("recomposition_snapshot"), Mapping
    ):
        return ""
    from jasper.active_speaker.baseline_profile import baseline_candidate_fingerprint

    return baseline_candidate_fingerprint(profile)


def _durable_applied_profile_identity(
    profile: Mapping[str, Any] | None,
) -> str:
    if not isinstance(profile, Mapping) or profile.get("status") != "applied":
        return ""
    return _profile_candidate_identity(profile)


def _mark_apply_mutation_started(
    token: str,
    locked_candidate: Mapping[str, Any],
    *,
    source_live_graph_fingerprint: str,
    retained_pre_apply_profile: Mapping[str, Any] | None = None,
) -> None:
    """Journal exact Apply source/target identities before the first DSP load."""

    pre_apply_profile = retained_pre_apply_profile
    if pre_apply_profile is None:
        pre_apply_profile = locked_candidate.get("applied_recomposition_profile")
    if not isinstance(pre_apply_profile, Mapping):
        pre_apply_profile = None
    target_profile_fingerprint = _profile_candidate_identity(locked_candidate)
    target_live_graph_fingerprint = _profile_graph_fingerprint(locked_candidate)
    topology_fingerprint = _profile_topology_fingerprint(locked_candidate)
    if (
        not target_profile_fingerprint
        or not target_live_graph_fingerprint
        or not source_live_graph_fingerprint
        or not topology_fingerprint
    ):
        raise CrossoverV2Refused(
            "the Apply transaction identities could not be recorded safely"
        )
    with _state_lock:
        state = load_v2_state()
        reservation = _reservation_mapping(state)
        if (
            state is None
            or reservation is None
            or _apply_reservation_token(state) != token
        ):
            raise CrossoverV2Refused(
                "the Apply reservation changed before the speaker update started"
            )
        if _pending_terminal_failure_code(state):
            # A relay can die while _journal_apply_started is awaiting the
            # live predecessor graph, after the earlier freshness check. The
            # state lock makes journal publication the last pre-mutation
            # linearization point: a death that already won must refuse load.
            raise CrossoverV2Refused(
                "the measurement session ended before the speaker update "
                "started; start the measurement again"
            )
        from jasper.active_speaker.baseline_profile import (
            load_applied_baseline_profile_state_strict,
        )

        # Candidate composition has already written a valid staging record.
        # Derive the source from the exact locked predecessor, then use the
        # strict read only to prove the on-disk Layer-A view agrees. Missing,
        # malformed, and unreadable bytes are distinct from genuine no-anchor.
        source_profile_fingerprint = _profile_candidate_identity(
            pre_apply_profile
        )
        source_profile_present = isinstance(pre_apply_profile, Mapping)
        try:
            state_file_present, disk_source = (
                load_applied_baseline_profile_state_strict()
            )
        except (OSError, ValueError) as exc:
            raise CrossoverV2Refused(
                "the current Layer-A state could not be journaled safely"
            ) from exc
        if (
            not state_file_present
            or isinstance(disk_source, Mapping) != source_profile_present
            or _profile_candidate_identity(disk_source)
            != source_profile_fingerprint
        ):
            raise CrossoverV2Refused(
                "the current Layer-A state changed before Apply could start"
            )
        journal = {
            "source_state_file_present": True,
            "source_profile_present": source_profile_present,
            "source_profile_fingerprint": source_profile_fingerprint,
            "source_live_graph_fingerprint": source_live_graph_fingerprint,
            "target_profile_fingerprint": target_profile_fingerprint,
            "target_live_graph_fingerprint": target_live_graph_fingerprint,
            "topology_fingerprint": topology_fingerprint,
            "pre_apply_profile": (
                dict(pre_apply_profile)
                if isinstance(pre_apply_profile, Mapping)
                else None
            ),
        }
        state["apply_reservation"] = {
            **dict(reservation),
            "phase": "mutation_started",
            "journal": journal,
        }
        save_v2_state(state, reservation_token=token)


def _mark_restore_mutation_started(
    token: str,
    retained_profile: Mapping[str, Any],
) -> None:
    """Journal exact Undo source/target identities before the first DSP load."""

    target_profile_fingerprint = _profile_candidate_identity(retained_profile)
    target_live_graph_fingerprint = _profile_graph_fingerprint(retained_profile)
    topology_fingerprint = _profile_topology_fingerprint(retained_profile)
    with _state_lock:
        state = load_v2_state()
        reservation = _reservation_mapping(state)
        if (
            state is None
            or reservation is None
            or _apply_reservation_token(state) != token
            or not target_profile_fingerprint
            or not target_live_graph_fingerprint
            or not topology_fingerprint
        ):
            raise CrossoverV2Refused(
                "the Undo transaction identities could not be recorded safely"
            )
        state["apply_reservation"] = {
            **dict(reservation),
            "phase": "mutation_started",
            "journal": {
                "source_state_file_present": True,
                "source_profile_present": True,
                "source_profile_fingerprint": str(
                    state.get("applied_profile_fingerprint") or ""
                ),
                "source_live_graph_fingerprint": str(
                    state.get("applied_live_graph_fingerprint") or ""
                ),
                "target_profile_fingerprint": target_profile_fingerprint,
                "target_live_graph_fingerprint": target_live_graph_fingerprint,
                "topology_fingerprint": topology_fingerprint,
            },
        }
        save_v2_state(state, reservation_token=token)


def _finish_recovered_apply(
    state: dict[str, Any], journal: Mapping[str, Any]
) -> None:
    reservation = _reservation_mapping(state)
    pending_code = _pending_terminal_failure_code(state)
    state["applied"] = True
    state["applied_session_id"] = (
        str(reservation.get("session_id") or "")
        if reservation is not None
        else ""
    ) or None
    state["failure"] = {"code": pending_code} if pending_code else None
    state["apply_blocked"] = None
    predecessor = journal.get("pre_apply_profile")
    state["pre_apply_profile"] = (
        dict(predecessor) if isinstance(predecessor, Mapping) else None
    )
    state["applied_profile_fingerprint"] = str(
        journal.get("target_profile_fingerprint") or ""
    ) or None
    state["applied_live_graph_fingerprint"] = str(
        journal.get("target_live_graph_fingerprint") or ""
    ) or None
    state["pending_terminal_failure"] = None
    state["apply_reservation"] = None


def _finish_recovered_restore(state: dict[str, Any]) -> None:
    pending_code = _pending_terminal_failure_code(state)
    state.update({
        "session_id": None,
        "applied": False,
        "applied_session_id": None,
        "candidate": None,
        "verify": None,
        "failure": {"code": pending_code} if pending_code else None,
        "apply_blocked": None,
        "pre_apply_profile": None,
        "applied_profile_fingerprint": None,
        "applied_live_graph_fingerprint": None,
        "pending_terminal_failure": None,
        "accepted_phases": [],
        "gain_plan_db": None,
        "verify_priors": None,
        "evidence": None,
        "apply_reservation": None,
    })


def _finish_recovered_superseded(state: dict[str, Any]) -> None:
    """Drop stale v2 authority when another coherent Layer-A graph won."""

    state.update({
        "session_id": None,
        "accepted_phases": [],
        "applied": False,
        "applied_session_id": None,
        "gain_plan_db": None,
        "candidate": None,
        "verify": None,
        "failure": {"code": TRANSACTION_INTERRUPTED_CODE},
        "apply_blocked": None,
        "verify_priors": None,
        "evidence": None,
        "pre_apply_profile": None,
        "applied_profile_fingerprint": None,
        "applied_live_graph_fingerprint": None,
        "pending_terminal_failure": None,
        "apply_reservation": None,
    })


def _v2_dsp_lock_dir() -> Path:
    """Canonical DSP lock directory, with the existing temp-state test seam."""

    if _state_path_override is not None:
        return _state_path().parent
    from jasper.active_speaker.baseline_profile import baseline_config_path

    return baseline_config_path().parent


async def _read_transaction_authority(
    cam: Any,
) -> tuple[bool, bool, Mapping[str, Any] | None, str, str]:
    """Read Layer A and live Camilla while the caller owns the DSP frontier."""

    from jasper.active_speaker.baseline_profile import (
        load_applied_baseline_profile_state_strict,
    )
    layer_a_readable = True
    try:
        current_state_file_present, current_profile = (
            load_applied_baseline_profile_state_strict()
        )
    except (OSError, ValueError) as exc:
        layer_a_readable = False
        current_state_file_present = False
        current_profile = None
        log_event(
            logger,
            "correction.crossover_v2_transaction_recovery_layer_a_unreadable",
            level=logging.ERROR,
            reason=type(exc).__name__,
        )
    try:
        current_live_graph_fingerprint = await _current_camilla_graph_fingerprint(cam)
    except Exception as exc:  # noqa: BLE001 — recovery hardware boundary
        current_live_graph_fingerprint = ""
        log_event(
            logger,
            "correction.crossover_v2_transaction_recovery_read_failed",
            level=logging.ERROR,
            reason=type(exc).__name__,
        )

    authoritative_graph_fingerprint = ""
    if isinstance(current_profile, Mapping):
        try:
            authoritative_graph_fingerprint = _profile_graph_fingerprint(
                current_profile
            )
        except (OSError, UnicodeError, RuntimeError, ValueError):
            pass
    return (
        layer_a_readable,
        current_state_file_present,
        current_profile,
        current_live_graph_fingerprint,
        authoritative_graph_fingerprint,
    )


async def _reconcile_transaction_under_writer_lock(
    cam: Any,
    *,
    token: str,
    operation: str,
    journal: Mapping[str, Any],
    topology_matches: bool,
    in_process: bool,
) -> bool:
    """Classify and publish one journal without releasing the DSP frontier."""

    from jasper.dsp_apply import dsp_writer_lock

    async with dsp_writer_lock(
        _v2_dsp_lock_dir(), source="crossover_v2_transaction_recovery"
    ):
        if topology_matches:
            (
                layer_a_readable,
                current_state_file_present,
                current_profile,
                current_live_graph_fingerprint,
                authoritative_graph_fingerprint,
            ) = await _read_transaction_authority(cam)
        else:
            layer_a_readable = False
            current_state_file_present = False
            current_profile = None
            current_live_graph_fingerprint = ""
            authoritative_graph_fingerprint = ""

        current_profile_present = isinstance(current_profile, Mapping)
        current_profile_fingerprint = _profile_candidate_identity(current_profile)
        current_profile_topology_fingerprint = (
            _profile_topology_fingerprint(current_profile)
            if current_profile_present
            else ""
        )
        source_matches = layer_a_readable and bool(journal) and (
            current_state_file_present
            == bool(journal.get("source_state_file_present"))
            and current_profile_present
            == bool(journal.get("source_profile_present"))
            and current_profile_fingerprint
            == str(journal.get("source_profile_fingerprint") or "")
            and current_live_graph_fingerprint
            == str(journal.get("source_live_graph_fingerprint") or "")
            and (
                not current_profile_present
                or (
                    bool(authoritative_graph_fingerprint)
                    and current_live_graph_fingerprint
                    == authoritative_graph_fingerprint
                )
            )
        )
        target_matches = layer_a_readable and bool(journal) and (
            current_state_file_present
            and current_profile_present
            and current_profile_fingerprint
            == str(journal.get("target_profile_fingerprint") or "")
            and current_live_graph_fingerprint
            == str(journal.get("target_live_graph_fingerprint") or "")
            and bool(authoritative_graph_fingerprint)
            and current_live_graph_fingerprint == authoritative_graph_fingerprint
        )
        authoritative_profile_matches_live = (
            layer_a_readable
            and current_state_file_present
            and current_profile_present
            and bool(authoritative_graph_fingerprint)
            and current_live_graph_fingerprint == authoritative_graph_fingerprint
            and bool(journal.get("topology_fingerprint"))
            and current_profile_topology_fingerprint
            == str(journal.get("topology_fingerprint") or "")
        )

        # Lock order is topology -> DSP -> state. Keep all three through the
        # token recheck and durable publication so neither authority can change
        # between classification and clearing the fence.
        with _state_lock:
            current = load_v2_state()
            current_reservation = _reservation_mapping(current)
            if (
                current is None
                or current_reservation is None
                or str(current_reservation.get("token") or "") != token
            ):
                return False
            if target_matches:
                if operation == "restore":
                    _finish_recovered_restore(current)
                else:
                    _finish_recovered_apply(current, journal)
                save_v2_state(current, reservation_token=token)
                log_event(
                    logger,
                    "correction.crossover_v2_transaction_recovered",
                    operation=operation,
                    outcome="committed",
                )
                return True
            if source_matches:
                if in_process:
                    # An ordinary failed mutation rolled itself back. Preserve
                    # the review/Undo frontier and its actual endpoint outcome;
                    # only a dead owner earns the crash-specific restart copy.
                    _apply_pending_terminal_failure(current)
                    current["apply_reservation"] = None
                    save_v2_state(current, reservation_token=token)
                    log_event(
                        logger,
                        "correction.crossover_v2_transaction_recovered",
                        level=logging.WARNING,
                        operation=operation,
                        outcome="rolled_back",
                    )
                    return True
                code = _pending_terminal_failure_code(current) or (
                    TRANSACTION_INTERRUPTED_CODE
                )
                current["pending_terminal_failure"] = None
                current["failure"] = {"code": code}
                current["apply_blocked"] = None
                current["apply_reservation"] = None
                if operation != "restore":
                    current["accepted_phases"] = []
                    current["gain_plan_db"] = None
                    current["candidate"] = None
                    current["verify"] = None
                    current["verify_priors"] = None
                    current["evidence"] = None
                save_v2_state(current, reservation_token=token)
                log_event(
                    logger,
                    "correction.crossover_v2_transaction_recovered",
                    level=logging.WARNING,
                    operation=operation,
                    outcome="not_committed",
                )
                return True
            if authoritative_profile_matches_live:
                _finish_recovered_superseded(current)
                save_v2_state(current, reservation_token=token)
                log_event(
                    logger,
                    "correction.crossover_v2_transaction_recovered",
                    level=logging.WARNING,
                    operation=operation,
                    outcome="superseded",
                )
                return True
            current["failure"] = {"code": TRANSACTION_RECOVERY_REQUIRED_CODE}
            current["apply_reservation"] = {
                **dict(current_reservation),
                "phase": "recovery_required",
                # The endpoint request has finished all automatic
                # reconciliation it can do. Relinquish its live-process owner
                # while retaining the transaction token/fence so the explicit
                # recovery action can enter immediately, without requiring a
                # jasper-web restart. save_v2_state still refuses unrelated
                # writers whenever any reservation token remains.
                "owner_pid": None,
                "owner_instance": "",
                "owner_birth_id": "",
            }
            save_v2_state(current, reservation_token=token)
            log_event(
                logger,
                "correction.crossover_v2_transaction_recovery_required",
                level=logging.CRITICAL,
                operation=operation,
                topology_matches=topology_matches,
            )
    raise CrossoverV2Refused(
        "the last speaker update was interrupted and its live DSP state cannot "
        "be proved safely; recover the speaker before applying or undoing"
    )


def reconcile_stale_v2_transaction(
    run_async: Any,
    camilla_factory: Any,
    *,
    reservation_token: str | None = None,
    _current_topology_fingerprint: str | None = None,
) -> bool:
    """Reconcile a dead mutation journal before admitting another operation.

    Returns ``True`` when a stale journal was resolved. A mixed or unreadable
    Layer-A/live result stays durably fenced and raises an explicit refusal;
    guessing in that state could overwrite Apply's exact Undo predecessor.
    """

    with _state_lock:
        state = load_v2_state()
        reservation = _reservation_mapping(state)
        if reservation is None:
            return False
        owner_alive = _reservation_owner_alive(reservation)
        if owner_alive and str(reservation.get("token") or "") != str(
            reservation_token or ""
        ):
            return False
        phase = str(reservation.get("phase") or "reserved")
        if phase not in {"mutation_started", "recovery_required"}:
            return False  # load_v2_state already clears safe admissions.
        token = str(reservation.get("token") or "")
        operation = str(reservation.get("operation") or "apply")
        journal = reservation.get("journal")
        if not token or not isinstance(journal, Mapping):
            journal = {}
    if _current_topology_fingerprint is None:
        from jasper.active_speaker.baseline_profile import topology_config_fingerprint
        from jasper.output_topology import (
            load_output_topology_strict,
            output_topology_mutation_lock,
        )

        with output_topology_mutation_lock():
            current_topology = load_output_topology_strict()
            return reconcile_stale_v2_transaction(
                run_async,
                camilla_factory,
                reservation_token=reservation_token,
                _current_topology_fingerprint=topology_config_fingerprint(
                    current_topology
                ),
            )

    journal_topology_fingerprint = str(
        journal.get("topology_fingerprint") or ""
    )
    return run_async(
        _reconcile_transaction_under_writer_lock(
            camilla_factory(),
            token=token,
            operation=operation,
            journal=journal,
            topology_matches=bool(
                journal_topology_fingerprint
                and journal_topology_fingerprint == _current_topology_fingerprint
            ),
            in_process=bool(reservation_token and owner_alive),
        )
    )


def recover_stale_v2_transaction(
    run_async: Any,
    camilla_factory: Any,
) -> dict[str, Any]:
    """Realign live Camilla to durable Layer A, then finish stale v2 recovery.

    This is the explicit household recovery action for a genuinely mixed
    post-crash state. Layer A remains the authority: the endpoint never chooses
    source vs target from the incomplete journal. It validates and reloads the
    exact sha-pinned config named by the durable applied profile under the
    shared DSP-writer lock, then lets :func:`reconcile_stale_v2_transaction`
    classify the now-coherent source, target, or superseding graph.
    """

    try:
        if reconcile_stale_v2_transaction(run_async, camilla_factory):
            return {"status": "recovered"}
    except CrossoverV2Refused:
        pass  # The mixed-state case is the one this action repairs.

    with _state_lock:
        state = load_v2_state()
        reservation = _reservation_mapping(state)
        if (
            reservation is None
            or _reservation_owner_alive(reservation)
            or str(reservation.get("phase") or "") != "recovery_required"
        ):
            raise CrossoverV2Refused(
                "there is no interrupted speaker update that needs recovery"
            )
        journal = reservation.get("journal")
        if not isinstance(journal, Mapping):
            raise CrossoverV2Refused(
                "the interrupted speaker update has no recoverable journal; open "
                "Speaker setup before measuring again"
            )
        operation = str(reservation.get("operation") or "apply")

    async def _reload_layer_a(expected_topology_fingerprint: str) -> None:
        from jasper.active_speaker.baseline_profile import (
            load_applied_baseline_profile_state_strict,
            persist_applied_baseline_profile,
        )
        from jasper.dsp_apply import apply_dsp_config, dsp_writer_lock

        cam = camilla_factory()
        async with dsp_writer_lock(
            _v2_dsp_lock_dir(), source="crossover_v2_explicit_recovery"
        ):
            profile = None
            predecessor = journal.get("pre_apply_profile")
            if operation == "apply" and isinstance(predecessor, Mapping):
                # The journal predecessor is an immutable snapshot of the exact
                # live source bytes. It remains SHA-correct even when Bass
                # Extension had rewritten the selected Layer-A YAML in place.
                if _profile_candidate_identity(predecessor) != str(
                    journal.get("source_profile_fingerprint") or ""
                ):
                    raise CrossoverV2Refused(
                        "the saved pre-update speaker profile does not match the "
                        "interrupted transaction; open Speaker setup"
                    )
                profile = predecessor
            else:
                try:
                    state_file_present, durable_profile = (
                        load_applied_baseline_profile_state_strict()
                    )
                except (OSError, ValueError) as exc:
                    raise CrossoverV2Refused(
                        "the saved speaker profile is unreadable; open Speaker setup "
                        "to rebuild it before measuring again"
                    ) from exc
                if state_file_present and isinstance(durable_profile, Mapping):
                    profile = durable_profile
            if not isinstance(profile, Mapping):
                raise CrossoverV2Refused(
                    "there is no saved speaker profile to recover; open Speaker "
                    "setup to rebuild it before measuring again"
                )
            profile_topology_fingerprint = _profile_topology_fingerprint(profile)
            if (
                not profile_topology_fingerprint
                or profile_topology_fingerprint != expected_topology_fingerprint
                or str(journal.get("topology_fingerprint") or "")
                != expected_topology_fingerprint
            ):
                raise CrossoverV2Refused(
                    "the saved speaker profile belongs to a different output "
                    "topology; open Speaker setup before recovering it"
                )
            config = profile.get("config")
            path = str(config.get("path") or "") if isinstance(config, Mapping) else ""
            sha256 = (
                str(config.get("sha256") or "")
                if isinstance(config, Mapping)
                else ""
            )
            try:
                expected_graph = _profile_graph_fingerprint(profile)
            except (OSError, UnicodeError, RuntimeError, ValueError) as exc:
                raise CrossoverV2Refused(
                    "the saved speaker profile config is missing or changed; open "
                    "Speaker setup to rebuild it before measuring again"
                ) from exc
            if not path or not sha256 or not expected_graph:
                raise CrossoverV2Refused(
                    "the saved speaker profile has no recoverable DSP config; open "
                    "Speaker setup to rebuild it before measuring again"
                )
            apply_state = await apply_dsp_config(
                source="crossover_v2_explicit_recovery",
                candidate_path=path,
                load_config=lambda candidate: cam.set_config_file_path(
                    candidate, best_effort=False
                ),
                get_current_config_path=lambda: cam.get_config_file_path(
                    best_effort=False
                ),
                acquire_lock=False,
                expected_candidate_sha256=sha256,
            )
            persist_applied_baseline_profile(
                profile,
                apply_state=apply_state.to_dict(),
                replace_equivalent=True,
            )

    from jasper.active_speaker.baseline_profile import topology_config_fingerprint
    from jasper.output_topology import (
        load_output_topology_strict,
        output_topology_mutation_lock,
    )

    with output_topology_mutation_lock():
        current_topology = load_output_topology_strict()
        current_topology_fingerprint = topology_config_fingerprint(current_topology)
        run_async(_reload_layer_a(current_topology_fingerprint))
        if not reconcile_stale_v2_transaction(
            run_async,
            camilla_factory,
            _current_topology_fingerprint=current_topology_fingerprint,
        ):
            raise CrossoverV2Refused(
                "the saved speaker sound was restored, but the interrupted update "
                "could not be finalized; open Speaker setup before measuring again"
            )
    log_event(logger, "correction.crossover_v2_transaction_explicitly_recovered")
    return {"status": "recovered"}


def _finish_v2_reservation(
    token: str,
    run_async: Any,
    camilla_factory: Any,
    *,
    current_topology_fingerprint: str | None = None,
) -> None:
    """Release a safe admission or reconcile an in-process mutation exit."""

    state = load_v2_state()
    reservation = _reservation_mapping(state)
    if reservation is None or str(reservation.get("token") or "") != token:
        return
    if str(reservation.get("phase") or "reserved") in {
        "mutation_started",
        "recovery_required",
    }:
        reconcile_stale_v2_transaction(
            run_async,
            camilla_factory,
            reservation_token=token,
            _current_topology_fingerprint=current_topology_fingerprint,
        )
        return
    _release_v2_apply_reservation(token)


def reset_v2_journey_state() -> None:
    """Start-over's v2 clear (W6.10, gate-amended for W6.8's Undo).

    Clears the measurement-JOURNEY fields (session binding, accepted phases,
    candidate, verify, failure, gain plan, priors, evidence) so the envelope
    serves the clean start screen — but when a candidate is APPLIED, preserves
    ``applied`` + ``pre_apply_profile``: those are the ONLY durable pointers the
    v2-aware Undo (:func:`handle_v2_restore`, W6.8) restores from. A full
    :func:`clear_v2_state` here would unlink the sole reference to the retained
    pre-candidate snapshot, leaving the applied graph playing with Undo
    permanently unreachable. Not applied ⇒ full clear, as before.
    """
    with _state_lock:
        state = load_v2_state()
        if state is None:
            return
        if not state.get("applied"):
            clear_v2_state()
            return
        state = _upgrade_legacy_undo_identity(state)
        pre_apply_profile = state.get("pre_apply_profile")
        applied_session_id = _candidate_applied_session_id(state)
        applied_profile_fingerprint = state.get("applied_profile_fingerprint")
        applied_live_graph_fingerprint = state.get(
            "applied_live_graph_fingerprint"
        )
        save_v2_state({
            "session_id": None,
            "accepted_phases": [],
            "applied": True,
            "applied_session_id": applied_session_id or None,
            "gain_plan_db": None,
            "candidate": None,
            "verify": None,
            "failure": None,
            "apply_blocked": None,
            "verify_priors": None,
            "evidence": None,
            "pre_apply_profile": (
                dict(pre_apply_profile)
                if isinstance(pre_apply_profile, Mapping)
                else None
            ),
            "applied_profile_fingerprint": applied_profile_fingerprint,
            "applied_live_graph_fingerprint": applied_live_graph_fingerprint,
        })
    log_event(logger, "correction.crossover_v2_journey_reset_kept_applied")


def observe_apply_success(
    candidate_fingerprint: str,
    *,
    pre_apply_profile: Mapping[str, Any] | None = None,
    applied_profile: Mapping[str, Any] | None = None,
    applied_live_graph_fingerprint: str | None = None,
    session_id: str | None = None,
    reservation_token: str | None = None,
) -> None:
    """Mark the v2 candidate applied — the apply-complete event that arms the
    soft-held VERIFY (§5.2). Called by the v2 apply endpoint on success.

    ``pre_apply_profile`` is the frozen applied-baseline snapshot the apply
    replaced (``build_baseline_profile_candidate``'s
    ``applied_recomposition_profile`` — see ``handle_v2_apply``), stashed here
    because the apply's own persisted profile pops that field once it becomes
    the new applied SSOT. This is the ONLY durable record of what the Undo
    path (``handle_v2_restore``) restores to; ``None`` when this was the
    speaker's first-ever applied crossover (nothing to undo back to).
    """
    from jasper.active_speaker.baseline_profile import baseline_candidate_fingerprint

    with _state_lock:
        state = load_v2_state()
        if state is None:
            raise CrossoverV2Refused(
                "the measurement state disappeared before Apply could commit"
            )
        if _apply_reservation_token(state) != str(reservation_token or ""):
            raise CrossoverV2Refused(
                "the Apply reservation changed before the speaker update committed"
            )
        if session_id is not None and str(state.get("session_id") or "") != session_id:
            log_event(
                logger,
                "correction.crossover_v2_apply_state_changed",
                level=logging.WARNING,
                reason="session_changed_before_commit",
            )
            if reservation_token is not None:
                raise CrossoverV2Refused(
                    "the Apply session changed before the speaker update committed"
                )
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
            if reservation_token is not None:
                raise CrossoverV2Refused(
                    "the Apply candidate changed before the speaker update committed"
                )
            return
        state["applied"] = True
        state["applied_session_id"] = str(state.get("session_id") or "") or None
        pending_failure_code = _pending_terminal_failure_code(state)
        state["failure"] = (
            {"code": pending_failure_code} if pending_failure_code else None
        )
        state["apply_blocked"] = None
        state["pre_apply_profile"] = (
            dict(pre_apply_profile) if isinstance(pre_apply_profile, Mapping) else None
        )
        state["applied_profile_fingerprint"] = (
            baseline_candidate_fingerprint(applied_profile)
            if isinstance(applied_profile, Mapping)
            and isinstance(applied_profile.get("recomposition_snapshot"), Mapping)
            else None
        )
        state["applied_live_graph_fingerprint"] = (
            str(applied_live_graph_fingerprint or "") or None
        )
        state["pending_terminal_failure"] = None
        state["apply_reservation"] = None
        save_v2_state(state, reservation_token=reservation_token)
    log_event(logger, "correction.crossover_v2_applied")


def observe_restore(*, reservation_token: str | None = None) -> None:
    """Clear the durable v2 state after a successful Undo (mirrors
    :func:`observe_apply_success`). Resets the whole flow to a clean
    unmeasured state — matching the reset a pre-apply terminal failure
    already gets (:func:`_persist_terminal_failure`) — so the envelope lands
    back on the pre-measurement screen rather than a half-consistent
    review_apply pointing at the now-undone candidate."""
    with _state_lock:
        state = load_v2_state()
        if state is None:
            return
        current_token = _apply_reservation_token(state)
        if current_token and current_token != str(reservation_token or ""):
            raise CrossoverV2Refused(
                "the Undo reservation changed before restore could commit"
            )
        # A relay death serialized behind Undo remains observable even when
        # the DSP restore itself committed.  Dropping it here made a terminal
        # phone session disappear precisely on the successful-Undo exit.
        _finish_recovered_restore(state)
        save_v2_state(state, reservation_token=reservation_token)


def _applied_gate_for_session(session_id: str) -> bool:
    """The conductor apply seam, bound to its measured-candidate session."""
    state = load_v2_state()
    return _candidate_applied_for_session(state, session_id)


def _applied_gate() -> bool:
    """Current-session probe retained for focused host tests and diagnostics."""

    state = load_v2_state()
    return _candidate_applied_for_session(
        state, str((state or {}).get("session_id") or "")
    )


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
# session-scoped measurement pause (§5.5 + W6.1 — hold voice OFF for the whole
# session, not just per-play)
# --------------------------------------------------------------------------- #
#
# The per-play ``measurement_window()`` (bind_production_play._emit) protected
# each stimulus, but between opening the fixed measurement volume and the first
# play — and in the gaps between plays — nothing held voice paused, so
# jasper-voice's idle reconciler reverted the -20 dB session volume back toward
# the household level within ~200 ms of ``session_volume_opened`` (W6.1 hardware
# run 2). Cap enforcement then silently understates: programs would play hotter
# than admission assumed. Fix: like the room / balance / sync flows, HOLD one
# ``measurement_window`` for the whole session (its MEASURE_PAUSE keeps the idle
# reconciler off), acquired when the volume opens and released on every drain.
#
# ``measurement_window`` is EXCLUSIVE (a single ``_window_active`` mutex — a
# second concurrent window raises), not nestable, so the per-play window is
# nest-SKIPPED while the session holds one (see ``bind_production_play``). The
# held context manager lives here as a process-global entered on jasper-web's
# single background loop; acquire/release are idempotent so a drain that runs
# after the session already released (recover / ceiling / a crash-fresh process)
# is a safe no-op. The paired ``MeasurementAbortTarget`` keeps the coordinator's
# isolation-loss abort effective under a held window: the per-play path
# registers the actual play task, so a mux gate-lease renew failure cancels the
# in-flight sweep (not the long-lived session task) and latches ``failed`` so
# the next play refuses honestly.
_session_pause_cm: Any = None
_session_abort_target: Any = None


async def acquire_session_measurement_pause() -> None:
    """Enter (once) the coordinator measurement window for the whole session.

    Idempotent: if the session already holds it, this is a no-op so a spurious
    second acquire cannot open a second exclusive window. Raises
    ``MeasurementWindowError`` if the window cannot be opened (e.g. a live voice
    session) — the caller surfaces that as a session-open failure.
    """
    global _session_pause_cm, _session_abort_target
    if _session_pause_cm is not None:
        return
    from jasper.correction import coordinator

    target = coordinator.MeasurementAbortTarget()
    cm = coordinator.measurement_window(abort_target=target)
    await cm.__aenter__()
    _session_pause_cm = cm
    _session_abort_target = target
    log_event(logger, "correction.crossover_v2_measurement_pause", action="acquire")


async def release_session_measurement_pause() -> None:
    """Exit the held session measurement window (idempotent).

    Every drain path (close / abandon / ceiling / unresolved-recover) calls
    this; a drain that runs when nothing is held (already released, or a
    crash-fresh process that never entered it) is a safe no-op — never a
    double-release.
    """
    global _session_pause_cm, _session_abort_target
    cm = _session_pause_cm
    if cm is None:
        return
    _session_pause_cm = None
    _session_abort_target = None
    await cm.__aexit__(None, None, None)
    log_event(logger, "correction.crossover_v2_measurement_pause", action="release")


def session_measurement_pause_held() -> bool:
    """True while the session holds the one measurement window (per-play skip)."""
    return _session_pause_cm is not None


def reset_session_measurement_pause_for_tests() -> None:
    """Test seam: drop the held-window reference without an ``__aexit__``."""
    global _session_pause_cm, _session_abort_target
    _session_pause_cm = None
    _session_abort_target = None


async def _play_under_session_pause(play_body: Callable[[], Any]) -> None:
    """Run one play under the session-held window, abort-target registered.

    Before Finding C the per-play window's ENTERING task was the play task, so
    the coordinator's isolation-loss abort (cancel the entering task) stopped
    the sweep. The held window's entering task is the session runner, whose
    cancel would not stop an in-flight play — so the play task registers
    itself as the abort target while playing. A latched abort (gate-lease
    renew failure between plays) refuses the next play with a NAMED error, and
    a cancel that lands mid-play surfaces as the same named error, so the
    runner's cleanup arm persists an honest failure either way.
    """
    from jasper.correction.coordinator import MeasurementWindowError

    target = _session_abort_target
    if target is not None and target.failed:
        raise MeasurementWindowError(
            "measurement isolation was lost (the music-isolation gate lease "
            "could not be renewed); restart the measurement session"
        )
    task = asyncio.current_task()
    if target is not None and task is not None:
        target.register(task)
    try:
        await play_body()
    except asyncio.CancelledError:
        if target is not None and target.failed:
            # The coordinator aborted THIS play on isolation loss — surface a
            # named terminal error (not a bare cancellation) so the session
            # runner's cleanup arm persists it and tells the phone.
            raise MeasurementWindowError(
                "measurement isolation was lost mid-play; playback was "
                "stopped before household music could re-enter the mix"
            ) from None
        raise
    finally:
        if target is not None:
            target.clear()


# --------------------------------------------------------------------------- #
# session-volume recovery + ceiling (§5.5 + W6.1 — recover routing, lazy ceiling)
# --------------------------------------------------------------------------- #

# Bound each session-volume drain (recover / ceiling) — CamillaDSP set+confirm
# is a few RPCs; longer than that means CamillaDSP is wedged, and the drain
# should surface a failure rather than hang the request thread.
_SESSION_VOLUME_DRAIN_TIMEOUT_S = 15.0


def _session_volume_io(camilla_factory: Any) -> tuple[Callable[[float], Any], Callable[[], Any]]:
    """(set, get) main-volume callables for the plan drains, fail-closed on
    CamillaUnavailable so a wedged DSP cannot silently no-op a recovery."""
    from jasper.camilla import CamillaUnavailable

    async def _set(db: float) -> bool:
        try:
            return await camilla_factory().set_volume_db(db, best_effort=False)
        except CamillaUnavailable as exc:
            raise RuntimeError("CamillaDSP is unavailable") from exc

    async def _get() -> float | None:
        try:
            return await camilla_factory().get_volume_db(best_effort=False)
        except CamillaUnavailable as exc:
            raise RuntimeError("CamillaDSP is unavailable") from exc

    return _set, _get


def _release_pause_best_effort(run_async: Any) -> None:
    """Release the session measurement pause from a drain that runs outside the
    runner (recover / ceiling). Idempotent no-op when nothing is held (already
    released, or a crash-fresh process that never entered it)."""
    try:
        run_async(
            release_session_measurement_pause(),
            timeout=_SESSION_VOLUME_DRAIN_TIMEOUT_S,
        )
    except (concurrent.futures.TimeoutError, OSError, RuntimeError, ValueError):
        logger.warning("v2 session measurement-pause release failed", exc_info=True)


def enforce_session_volume_ceiling_if_stale(
    run_async: Any, camilla_factory: Any
) -> bool:
    """Lazy wall-clock-ceiling enforcement (W6.1 — ``enforce_ceiling`` had zero
    callers, so the 1800 s ceiling never existed at runtime).

    Invoked on envelope build (on read) and at session open. Cheap on the happy
    path: ``stale_active`` is an in-memory check, so a healthy session pays
    nothing; only a session that has outlived
    ``DEFAULT_WALL_CLOCK_CEILING_S`` is force-drained here, restoring the
    household volume and releasing any held measurement pause. v2-only. Returns
    True iff a stale session was drained.
    """
    if not v2_flow_active():
        return False
    plan = session_volume_plan()
    try:
        if not plan.stale_active():
            return False
    except (OSError, RuntimeError, ValueError):
        return False
    set_v, get_v = _session_volume_io(camilla_factory)
    try:
        run_async(
            plan.enforce_ceiling(set_v, get_v),
            timeout=_SESSION_VOLUME_DRAIN_TIMEOUT_S,
        )
    except (concurrent.futures.TimeoutError, OSError, RuntimeError, ValueError):
        log_event(
            logger,
            "correction.crossover_v2_ceiling_enforce_failed",
            level=logging.ERROR,
        )
    _release_pause_best_effort(run_async)
    return True


def v2_volume_recovery_active() -> bool:
    """True when the v2 session-volume plan holds a state the recover-volume
    endpoint must drain (unresolved, or a crash-hydrated active plan). The
    legacy-lease path 409s these because they live on the v2 plan, not the
    lease — the observed ``crossover_volume_recovery_not_required`` bug."""
    if not v2_flow_active():
        return False
    try:
        return bool(session_volume_plan().needs_recovery)
    except (OSError, RuntimeError, ValueError):
        return True  # fail-closed: an unreadable state still offers recovery


def recover_session_volume(
    run_async: Any, camilla_factory: Any
) -> tuple[bool, str]:
    """Drain the v2 plan's unresolved / stale-active state (the volume_recovery
    screen's ``recover_volume`` action). Returns ``(succeeded, result_value)``.

    Routes to ``SessionVolumePlan.recover_unresolved`` — the v2 owner of the
    unresolved state — instead of the legacy lease, and releases any held
    measurement pause on success.
    """
    from jasper.active_speaker.session_volume_plan import (
        SessionVolumeRestoreResult,
    )

    plan = session_volume_plan()
    set_v, get_v = _session_volume_io(camilla_factory)
    try:
        result = run_async(
            plan.recover_unresolved(set_v, get_v),
            timeout=_SESSION_VOLUME_DRAIN_TIMEOUT_S,
        )
    except concurrent.futures.TimeoutError:
        log_event(
            logger,
            "correction.crossover_v2_volume_recovery_timeout",
            level=logging.ERROR,
        )
        result = SessionVolumeRestoreResult.FAILED
    succeeded = result is not SessionVolumeRestoreResult.FAILED
    if succeeded:
        _release_pause_best_effort(run_async)
    return succeeded, getattr(result, "value", str(result))


def reconcile_session_volume_for_new_session(
    run_async: Any, camilla_factory: Any
) -> None:
    """Drain any residual session volume before a fresh session opens (W6.1 E1).

    A stale-active is force-drained by the ceiling; a residual owned-active
    leftover from a prior failed session in THIS process (``open`` refuses over
    any non-``None`` state) is drained too, so ``plan.open`` starts clean rather
    than raising ``SessionVolumePlanError`` into the silent
    200→adapter_failed loop observed live (run 2's retry). A latched
    ``unresolved`` / crash-hydrated ``needs_recovery`` state is NOT drained here
    — the caller's ``needs_recovery`` gate refuses it toward the recover-volume
    screen.
    """
    if not v2_flow_active():
        return
    plan = session_volume_plan()
    enforce_session_volume_ceiling_if_stale(run_async, camilla_factory)
    if plan.measurement_volume_db is None or plan.needs_recovery:
        return
    set_v, get_v = _session_volume_io(camilla_factory)
    try:
        run_async(
            plan.abandon(set_v, get_v, reason="stale_session_reset"),
            timeout=_SESSION_VOLUME_DRAIN_TIMEOUT_S,
        )
        log_event(logger, "correction.crossover_v2_stale_session_reset")
    except (concurrent.futures.TimeoutError, OSError, RuntimeError, ValueError):
        log_event(
            logger,
            "correction.crossover_v2_stale_session_reset_failed",
            level=logging.ERROR,
        )
    _release_pause_best_effort(run_async)


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
    session_id = str(state.get("session_id") or "") if state else ""
    applied = _candidate_applied_for_session(state, session_id)
    for phase in CAPTURE_PHASES:
        if phase not in accepted:
            if phase == PHASE_VERIFY and PHASE_MEASURE in accepted and not applied:
                return PHASE_REVIEW_APPLY
            return phase
    return PHASE_DONE


def _session_apply_blocked_issue(
    state: Mapping[str, Any] | None,
) -> dict[str, str] | None:
    """Return the blocked-apply nudge only for the session that produced it.

    Pre-W5b records had no session binding and are intentionally ignored: a
    global stale blocker is less truthful than no blocker on a fresh journey.
    """
    if not isinstance(state, Mapping):
        return None
    blocked = state.get("apply_blocked")
    if not isinstance(blocked, Mapping):
        return None
    session_id = str(state.get("session_id") or "")
    blocked_session_id = str(blocked.get("session_id") or "")
    if not session_id or blocked_session_id != session_id:
        return None
    return {
        "id": str(blocked.get("id") or ""),
        "message": str(blocked.get("message") or ""),
    }


def crossover_v2_status_block(
    *,
    run_async: Any | None = None,
    camilla_factory: Any | None = None,
) -> dict[str, Any] | None:
    """The ``status["crossover_v2"]`` block, or ``None`` when the flow is legacy.

    ``needs_recovery`` comes from the SessionVolumePlan (the W2 gate ruling:
    key on ``needs_recovery``, never ``unresolved_volume_safety`` alone — a
    crash-hydrated active plan surfaces no unresolved payload but still needs
    draining before a new session).
    """
    if not v2_flow_active():
        return None
    if run_async is not None and camilla_factory is not None:
        try:
            reconcile_stale_v2_transaction(run_async, camilla_factory)
        except CrossoverV2Refused:
            # A coherent source/target is repaired automatically. A genuinely
            # mixed state remains fenced and is rendered below with the explicit
            # recovery action rather than taking down the whole status surface.
            pass
    with _state_lock:
        state = load_v2_state()
        if state is not None:
            upgraded = _upgrade_legacy_undo_identity(state)
            if upgraded != state:
                try:
                    save_v2_state(upgraded)
                except CrossoverV2Refused:
                    # An admitted Apply owns the state frontier. Its success
                    # callback writes stronger fresh identities before release.
                    pass
                else:
                    state = upgraded
    try:
        needs_recovery = bool(session_volume_plan().needs_recovery)
    except (OSError, RuntimeError, ValueError):
        needs_recovery = True  # unreadable volume state fails closed
    reservation = _reservation_mapping(state)
    unresolved_transaction = bool(
        reservation
        and not _reservation_owner_alive(reservation)
        and str(reservation.get("phase") or "")
        in {"mutation_started", "recovery_required"}
    )
    block: dict[str, Any] = {
        "phase": _phase_from_state(state),
        "candidate": (state or {}).get("candidate"),
        "verify": (state or {}).get("verify"),
        "failure": (
            {"code": TRANSACTION_RECOVERY_REQUIRED_CODE}
            if unresolved_transaction
            else (state or {}).get("failure")
        ),
        "apply_blocked": _session_apply_blocked_issue(state),
        "needs_recovery": needs_recovery,
        "applied": bool(state and state.get("applied")),
        "candidate_applied": _candidate_applied_for_session(
            state,
            str((state or {}).get("session_id") or ""),
        ),
        "undo_available": bool(
            state
            and state.get("applied")
            and isinstance(state.get("pre_apply_profile"), Mapping)
            and state.get("applied_profile_fingerprint")
            and state.get("applied_live_graph_fingerprint")
        ),
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
    analysis = candidate.analysis if isinstance(candidate.analysis, Mapping) else {}
    return {
        "fingerprint": candidate.fingerprint,
        "program_id": candidate.program_id,
        "trims_db": dict(candidate.role_attenuations_db),
        "alignment": candidate.alignment.to_dict(),
        # Threaded through for the review_apply low-confidence nudge (W6.7
        # ruling 4, crossover_envelope_v2.ALIGNMENT_CONFIDENCE_NUDGE_FLOOR).
        "alignment_confidence": analysis.get("alignment_confidence"),
    }


def persist_conductor_state(
    conductor: Any,
    *,
    failure_code: str | None,
    evidence: Mapping[str, Any] | None = None,
    allow_session_rebind: bool = False,
) -> None:
    """Write the conductor's durable snapshot + host-observed failure state."""
    snap = conductor.snapshot()
    verify_outcome = conductor.verify_outcome
    verify_tracking = conductor.verify_tracking
    verify_payload: dict[str, Any] = {}
    if verify_outcome is not None:
        verify_payload["outcome"] = verify_outcome
    if isinstance(verify_tracking, Mapping):
        verify_payload["tracking"] = dict(verify_tracking)
    state: dict[str, Any] = {
        "session_id": snap.session_id,
        "accepted_phases": list(snap.accepted_phases),
        "applied": snap.applied,
        "applied_session_id": snap.session_id if snap.applied else None,
        "gain_plan_db": dict(snap.gain_plan_db) if snap.gain_plan_db else None,
        "candidate": _candidate_summary(conductor.candidate),
        "verify": verify_payload or None,
        "failure": {"code": failure_code} if failure_code else None,
        "verify_priors": {
            "predicted_sum": _decimate_sum(conductor.measure_predicted_sum),
            "gate_window_ms": conductor.measure_gate_window_ms,
        },
        "evidence": dict(evidence) if evidence else None,
    }
    with _state_lock:
        prior_raw = load_v2_state()
        prior = prior_raw or {}
        if (
            prior_raw is not None
            and str(prior.get("session_id") or "") != snap.session_id
            and not allow_session_rebind
        ):
            raise CrossoverV2Refused(
                "the measurement session changed before its state could be saved"
            )
        # ``applied`` and Undo identity are host-owned. Once a state exists, a
        # conductor snapshot may never regress them (especially after a stale
        # callback wakes after Apply/Undo released its reservation).
        if prior_raw is not None:
            state["applied"] = bool(prior.get("applied"))
            state["applied_session_id"] = (
                snap.session_id
                if snap.applied
                else _candidate_applied_session_id(prior)
            ) or None
        same_session = str(prior.get("session_id") or "") == snap.session_id
        if (
            state["candidate"] is None
            and isinstance(prior.get("candidate"), Mapping)
            and (same_session or snap.applied)
        ):
            state["candidate"] = dict(prior["candidate"])
        if state["evidence"] is None and isinstance(
            prior.get("evidence"), Mapping
        ):
            if same_session:
                state["evidence"] = dict(prior["evidence"])
        # ``pre_apply_profile`` (the Undo stash — observe_apply_success /
        # handle_v2_restore) is not conductor-owned. Its carry-forward MUST
        # remain unconditional across the deliberate VERIFY session rebind.
        state["pre_apply_profile"] = prior.get("pre_apply_profile")
        state["applied_profile_fingerprint"] = prior.get(
            "applied_profile_fingerprint"
        )
        state["applied_live_graph_fingerprint"] = prior.get(
            "applied_live_graph_fingerprint"
        )
        blocked = prior.get("apply_blocked")
        state["apply_blocked"] = (
            dict(blocked)
            if isinstance(blocked, Mapping)
            and str(blocked.get("session_id") or "") == snap.session_id
            else None
        )
        save_v2_state(state)


def _defer_terminal_failure_during_reservation(conductor: Any, code: str) -> bool:
    """Record a relay death without replacing an admitted state frontier."""

    snap = conductor.snapshot()
    with _state_lock:
        state = load_v2_state()
        token = _apply_reservation_token(state)
        if (
            state is None
            or not token
            or str(state.get("session_id") or "") != snap.session_id
        ):
            return False
        state["pending_terminal_failure"] = {
            "code": code,
            "session_id": snap.session_id,
        }
        save_v2_state(state, reservation_token=token)
        return True


def _persist_terminal_failure(conductor: Any, code: str) -> None:
    """Session-terminal persistence (§5.6): pre-apply, capture evidence dies
    with the session (restart at CHECK); post-apply, the applied candidate +
    verify priors survive so ``/v2/verify`` can re-arm.

    An Apply/Undo reservation deliberately prevents another callback from
    replacing the reviewed frontier while the DSP transaction is in flight.
    A terminal callback records a narrow pending marker instead. The
    transaction consumes it at its linearization point: a pre-load Apply
    refuses, a failed transaction invalidates the dead session on release,
    and an already-applied graph retains the failure + Undo route. This caller
    can therefore continue volume/session cleanup without losing the death.
    """
    if _defer_terminal_failure_during_reservation(conductor, code):
        log_event(
            logger,
            "correction.crossover_v2_terminal_failure_deferred",
            level=logging.WARNING,
            failure_code=code,
            reason="state_transaction_reserved",
        )
        return
    try:
        persist_conductor_state(conductor, failure_code=code)
    except CrossoverV2Refused:
        # The reservation may have been admitted between the first probe and
        # the generic snapshot write. Serialize behind it on that race too.
        if _defer_terminal_failure_during_reservation(conductor, code):
            log_event(
                logger,
                "correction.crossover_v2_terminal_failure_deferred",
                level=logging.WARNING,
                failure_code=code,
                reason="state_transaction_reserved",
            )
            return
        log_event(
            logger,
            "correction.crossover_v2_terminal_failure_deferred",
            level=logging.WARNING,
            failure_code=code,
            reason="state_frontier_changed",
        )
        return
    with _state_lock:
        state = load_v2_state()
        if state is None:
            return
        if not _candidate_applied_for_session(
            state, conductor.snapshot().session_id
        ):
            state["accepted_phases"] = []
            state["gain_plan_db"] = None
            state["candidate"] = None
            state["verify"] = None
            state["verify_priors"] = None
            state["evidence"] = None
            state["apply_blocked"] = None
        try:
            save_v2_state(state)
        except CrossoverV2Refused:
            # Same admission race after the conductor snapshot but before the
            # pre-apply invalidation write.
            _defer_terminal_failure_during_reservation(conductor, code)


def _persist_terminal_failure_best_effort(conductor: Any, code: str) -> None:
    """Persist a terminal result without ever pre-empting safety cleanup."""

    try:
        _persist_terminal_failure(conductor, code)
    except OSError as exc:
        # Disk-full/read-only failures are important, but volume restore,
        # measurement-pause release, and relay purge are hardware-safety
        # obligations and must still run independently.
        log_event(
            logger,
            "correction.crossover_v2_terminal_failure_persist_failed",
            level=logging.ERROR,
            failure_code=code,
            reason=type(exc).__name__,
        )


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


def resolve_relay_calibration(setup: Any, device: Any) -> Any:
    """The production mic-calibration resolver for a v2 capture.

    Reuses ``correction_setup._relay_calibration_from_setup`` — the ONE point
    the room + legacy crossover relay flows already use to materialize the
    phone wizard's serial/upload/stored calibration choice as a stored
    ``CalibrationRecord`` (and persist it as the household default mic).
    Returns the record or ``None`` (phone mic / no calibration chosen).
    ``device`` is accepted for parity with the seam signature; the setup
    payload is the authoritative choice today.
    """
    from .correction_setup import _relay_calibration_from_setup

    del device  # the setup payload carries the phone's calibration choice
    return _relay_calibration_from_setup(
        dict(setup) if isinstance(setup, Mapping) else None
    )


def default_setup_calibration_for_v2() -> Any | None:
    """The v2 session's OPTIONAL household-mic prefill hint (W6.12).

    Every v2 capture logged ``crossover_v2_uncalibrated_capture`` even when
    the household had a resolvable stored mic (a UMIK-2 by serial, ingested
    via ``/correction/calibration/fetch``). Root cause: ``resolve_relay_calibration``
    is only as good as what the phone posts in ``setup.calibration`` — and a
    v2 capture-plan session has no calibration-picker screen of its own. The
    LEGACY per-driver crossover flow gets away with this because its
    calibration choice comes from the ``level_ramp`` level-match page the
    household visits FIRST in the same phone tab (the capture page's
    ``setupState`` module variable survives the in-tab hash navigation from
    that page into the driver sweeps that follow); v2 has no preceding
    level-match page (design: CHECK's own pilot pairs solve gain), so it
    never had a carrier for the same hint.

    Reuses ``correction_setup._default_setup_calibration_for_spec`` — the
    SAME household-mic-hint resolver the legacy level-match handlers already
    pass into ``build_level_ramp_spec``. Threaded into
    ``build_v2_session_spec``/``build_v2_verify_session_spec`` via their
    shared ``**spec_kwargs`` forward to ``build_crossover_sweep_spec``
    (W6.12 added the parameter there); the capture page applies it SILENTLY
    (no extra tap) when nothing has already been chosen for that page load.
    Fail-soft: any resolution miss yields no hint, never blocks session open.
    """
    from .correction_setup import _default_setup_calibration_for_spec

    try:
        return _default_setup_calibration_for_spec()
    except (OSError, RuntimeError, ValueError):
        log_event(
            logger,
            "correction.crossover_v2_default_calibration_hint_failed",
            level=logging.WARNING,
        )
        return None


def _setup_calibration_observation(setup: Any) -> tuple[str, str]:
    """What the capture's phone-reported setup held, redacted-safe (W6.13).

    Returns ``(mode, calibration_id)`` for the uncalibrated-capture WARN so a
    live journal line settles empirically whether the phone sent NO setup at
    all (``mode="absent"``) or sent one whose calibration didn't resolve
    (e.g. ``mode="none"``, or a stale ``calibration_id``) — the round-5
    ambiguity. Only the mode and the calibration_id (a stored-record id, not
    a secret) are ever extracted; a serial or an uploaded calibration file
    body never reaches the journal.
    """
    if not isinstance(setup, Mapping):
        return "absent", ""
    calibration = setup.get("calibration")
    if not isinstance(calibration, Mapping):
        return "absent", ""
    return (
        str(calibration.get("mode") or ""),
        str(calibration.get("calibration_id") or ""),
    )


def bind_production_analyze(
    *,
    resolve_calibration: Callable[[Any, Any], Any] | None = resolve_relay_calibration,
    meta: dict[str, Any] | None = None,
) -> Callable[[Any, Any, Any, Any], Any]:
    """The real ``analyze`` seam: CaptureResult → ``analyze_program_capture``.

    Design §5.6.4 applies the mic cal to every gated response, so this binding
    resolves the calibration from the capture's phone-reported setup (the same
    ``_relay_calibration_from_setup`` machinery the legacy relay flows use)
    and threads BOTH the resolved curve and the conductor's declared geometry
    into ``analyze_program_capture``. When no calibration resolves, the
    analysis still runs — relative timing/level stay valid per the design —
    but the fact is never silent: a WARN ``event=`` fires and ``meta``
    (persisted with the session's evidence refs) records the per-phase
    ``{"applied": False}`` annotation.
    """

    def _analyze(program: Any, result: Any, priors: Any, geometry: Any) -> Any:
        from jasper.audio_measurement import program_analysis as _pa

        wav = getattr(result, "wav", result)
        samples, rate = _wav_bytes_to_samples(wav)
        setup = getattr(result, "setup", None)
        record = None
        if resolve_calibration is not None:
            try:
                record = resolve_calibration(
                    setup, getattr(result, "device", None)
                )
            except (OSError, RuntimeError, TypeError, ValueError):
                # A resolver failure downgrades to an annotated-uncalibrated
                # analysis, never a crashed capture — but it is logged.
                log_event(
                    logger,
                    "correction.crossover_v2_calibration_resolve_failed",
                    level=logging.WARNING,
                    phase=program.phase,
                )
                record = None
        curve = getattr(record, "curve", None)
        if record is not None and curve is None:
            # A bare CalibrationCurve (tests / future callers) is accepted too;
            # anything else stays None (annotated uncalibrated, never a crash).
            from jasper.audio_measurement.calibration import CalibrationCurve

            if isinstance(record, CalibrationCurve):
                curve = record
        if curve is None:
            # W6.13 round-5 diagnostic: name what the phone-reported setup
            # actually held at resolve time so a live journal line
            # distinguishes "the phone sent nothing" (setup_mode=absent)
            # from "the phone sent a choice that didn't resolve"
            # (setup_mode=none/stored/..., with its id). Redacted-safe —
            # see _setup_calibration_observation.
            setup_mode, setup_calibration_id = _setup_calibration_observation(
                setup
            )
            log_event(
                logger,
                "correction.crossover_v2_uncalibrated_capture",
                level=logging.WARNING,
                phase=program.phase,
                setup_mode=setup_mode,
                setup_calibration_id=setup_calibration_id,
            )
        if meta is not None:
            meta.setdefault("calibration", {})[program.phase] = {
                "applied": curve is not None,
                "calibration_id": getattr(record, "calibration_id", None),
            }
        return _pa.analyze_program_capture(
            program,
            samples,
            rate,
            calibration=curve,
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
    declared_sensitivities: Mapping[str, float] | None = None,
    config_dir: str | None = None,
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

    ``config_dir`` defaults to the SAME
    :data:`jasper.active_speaker.staging.DEFAULT_CAMILLA_CONFIG_DIR` every
    sibling DSP writer (commissioning apply/verify, ``web_commissioning``,
    ``correction_setup``) locks against — W6 hardware run 3 finding F caught
    this binding still defaulting to the stale literal ``"/etc/camilladsp"``:
    two defects at once. (a) ``jasper-correction-web`` runs
    ``ProtectSystem=full`` with ``ReadWritePaths=/var/lib/jasper
    /var/lib/camilladsp`` only (see ``deploy/jasper-correction-web.service``),
    so opening ``/etc/camilladsp/.dsp_apply.lock`` raised EROFS 70 ms into the
    first play. (b) even under a writable ``/etc``, it would have been the
    WRONG lock identity — every other writer locks
    ``/var/lib/camilladsp/configs/.dsp_apply.lock``, so a real ``/etc``
    lock would not have serialized against them at all.

    ON-DEVICE: not exercised hardware-free; W6 validates acoustically.
    """
    from jasper.active_speaker.crossover_v2_flow import (
        PHASE_VERIFY,
        bind_program_playback_seams,
    )
    from jasper.active_speaker.web_commissioning import DEFAULT_CAMILLA_CONFIG_DIR
    from jasper.audio_measurement.program import write_program_wav

    resolved_config_dir = (
        config_dir if config_dir is not None else str(DEFAULT_CAMILLA_CONFIG_DIR)
    )

    def _play(phase: str, program: Any) -> None:
        bundle_dir = evidence_store.bundle_dir
        wav_rel = f"crossover_v2/{relay_session_id}/{phase}_program.wav"
        wav_path = Path(bundle_dir) / wav_rel
        wav_path.parent.mkdir(parents=True, exist_ok=True)
        write_program_wav(wav_path, program)
        artifact = evidence_store.identify_artifact(wav_rel)

        async def _play_body() -> None:
            from jasper.active_speaker.program_playback import (
                verified_program_aplay,
            )

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
                config_dir=resolved_config_dir,
                program=program,
                wav_path=str(wav_path),
                topology=topology,
                safety_profile=safety_profile,
                role_targets=role_targets,
                session_volume_db=session_volume_db,
                declared_sensitivities=declared_sensitivities,
            )
            await play_program(
                program,
                program_graph_yaml=program_yaml,
                session_volume_plan=session_volume_plan(),
                **seams,
            )

        async def _emit() -> None:
            # The session holds ONE measurement window for its whole life (W6.1
            # — see acquire_session_measurement_pause). ``measurement_window``
            # is exclusive/non-nestable, so nest-SKIP it here when the session
            # already holds it — registering this play task as the window's
            # abort target so an isolation-loss abort still stops the sweep.
            # Only fall back to a per-play window if the session pause is
            # somehow not held.
            if session_measurement_pause_held():
                await _play_under_session_pause(_play_body)
                return
            from jasper.correction import coordinator

            async with coordinator.measurement_window():
                await _play_body()

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


# W6 hardware run 3 finding H: the catch-all cleanup arm below posts a
# terminal host event (§5.10's ``capture_result``) so the phone stops
# recording into silence, then purges the relay session. Purging
# immediately after can race the phone's very next poll — the driver saw a
# bare 404 on the session's own status endpoint ~1 s after the terminal
# event, because the session was already gone. This grace gives the just-
# posted event a bounded window to actually reach the phone (over the
# public relay) before the session disappears; it does not delay the
# household's volume restore, which stays immediate.
TERMINAL_FAILURE_PURGE_GRACE_S = 3.0


def build_v2_run_and_consume(
    conductor: Any,
    *,
    volume: V2VolumeHooks,
    stop_event: threading.Event,
    stop_lock: Any,
    verify_authority: Callable[[Callable[[], Any]], Any] | None = None,
    evidence_refs: Mapping[str, Any] | None = None,
    poll_interval_s: float | None = None,
    timeout_s: float | None = None,
    first_begin_timeout_s: float | None = None,
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
      failure state + volume ABANDON (the §5.5 walked-away guarantee). The
      ``OSError`` here is genuinely the relay TRANSPORT (e.g.
      ``run_capture_plan``'s poll loop reaching an unreachable host) — a
      LOCAL play/analyze seam OSError never reaches this arm: ``on_armed``/
      ``consume`` below convert it to :class:`CrossoverV2LocalSeamError`
      first (W6 hardware run 3 finding G), so it falls through to the
      catch-all arm's ``internal_error`` instead.
    * ``CaptureBeginRefused`` — the conductor already recorded the phase's own
      failure code; persist it + abandon.
    * ``CaptureStopped`` / cancellation — expected control flow: abandon the
      volume, no failure code.
    * ANY other ``Exception`` — the W6.1 catch-all cleanup arm: the seams
      raise open-endedly (``CamillaUnavailable`` is a bare Exception), so
      every non-relay failure posts a terminal host event, persists
      ``program_unplayable`` (program/admission/flow classes) or
      ``internal_error`` (everything else — including
      ``CrossoverV2LocalSeamError``), abandons the volume (releasing the
      session measurement pause), waits a bounded grace period (finding H —
      the just-posted terminal host event must reach the phone before the
      relay session is purged out from under its next poll), purges, and
      re-raises.
    * Plan complete with VERIFY accepted ⇒ CLOSE (exact restore); a completed
      plan that did not reach done (attempt budget exhausted) abandons.
    """

    async def _run_and_consume(client: Any, pi_session: Any) -> None:
        from jasper.capture_relay.client import RelayError
        from jasper.capture_relay.session import (
            HOST_PHASE_CAPTURE_RESULT,
            HOST_PHASE_CAPTURE_SET_EXHAUSTED,
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
            PHASE_REVIEW_APPLY,
            PHASE_VERIFY,
            REASON_INTERNAL_ERROR,
            REASON_PROGRAM_UNPLAYABLE,
            REASON_REGISTRY,
            REASON_RELAY_TIMEOUT,
            V2_FIRST_BEGIN_TIMEOUT_S,
            TRANSIENT_AUTO_RETRY_CODES,
            CrossoverV2FlowError,
        )
        from jasper.active_speaker.program_admission import ProgramAdmissionError
        from jasper.active_speaker.program_playback import ProgramPlaybackError
        from jasper.active_speaker.session_volume_plan import SessionVolumePlanError
        from jasper.correction.coordinator import MeasurementWindowError

        def authorize(index: int, attempt: int, entry: Any = None) -> None:
            with stop_lock:
                if stop_event.is_set():
                    raise CaptureStopped("capture stopped")
            conductor.authorize_begin(index, attempt, entry)

        def _post_sweep_phase_best_effort(phase: str) -> None:
            """Tell the phone playback is starting/finished (§5.10 progress).

            The capture page's ``waitForSweepComplete``
            (``capture-page/js/main.js``) polls ``host_event.phase`` for
            ``"sweep_started"`` / ``"sweep_complete"`` around its own play
            wait and otherwise sits until ITS OWN timeout elapses — the v2
            runner posted neither (W6 run 5), so a real phone could never
            complete a v2 capture. Mirrors the legacy per-driver capture-plan
            flow's ``post_phase`` around its own play call
            (``correction_crossover_flow.py``). Best-effort like that
            sibling: a transient post failure here is a progress-only miss,
            not a capture failure — the existing terminal host event (or the
            phone's own wait timeout) still resolves the phone's wait on any
            real failure.
            """
            armed = conductor.armed_capture
            index, attempt = armed if armed is not None else (None, None)
            try:
                client.post_host_event(
                    pi_session.session_id,
                    pi_session.pull_token,
                    {"phase": phase, "index": index, "attempt": attempt},
                )
            except (OSError, RuntimeError, ValueError):
                logger.warning(
                    "v2 sweep progress host-event post failed", exc_info=True
                )

        def on_armed(state: Any) -> None:
            if stop_event.is_set():
                raise CaptureStopped("capture stopped")
            # Finding G: on_armed's ``conductor.on_armed`` → ``seams.play`` is a
            # LOCAL seam (the DSP writer lock, CamillaController) — an OSError
            # here (e.g. EROFS opening the lock file) is not a relay-transport
            # death and must not be caught by the relay-death arm below.
            _post_sweep_phase_best_effort("sweep_started")
            try:
                play_action = lambda: conductor.on_armed(state)
                if (
                    conductor.current_phase == PHASE_VERIFY
                    and verify_authority is not None
                ):
                    verify_authority(play_action)
                else:
                    play_action()
            except OSError as exc:
                raise CrossoverV2LocalSeamError(str(exc)) from exc
            _post_sweep_phase_best_effort("sweep_complete")

        def consume(index: int, attempt: int, result: Any, entry: Any = None):
            # Same local-seam boundary as on_armed, for consume_capture's
            # analyze and durable-state seams.
            def _consume_and_persist() -> Any:
                verdict = conductor.consume_capture(index, attempt, result, entry)
                code = (
                    verdict.get("code")
                    if isinstance(verdict, Mapping)
                    else None
                )
                persist_conductor_state(
                    conductor,
                    failure_code=(
                        code if not verdict.get("accepted") else None
                    ),
                    evidence=evidence_refs,
                )
                return verdict

            try:
                if (
                    conductor.current_phase == PHASE_VERIFY
                    and verify_authority is not None
                ):
                    return verify_authority(_consume_and_persist)
                return _consume_and_persist()
            except OSError as exc:
                raise CrossoverV2LocalSeamError(str(exc)) from exc

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

        async def _post_terminal_failure_host_event(code: str) -> None:
            """Tell the phone the session is over so it stops waiting (§5.10).

            A play-seam failure escapes ``run_capture_plan`` WITHOUT posting a
            capture verdict, so the phone records into silence and then polls
            ``capture_result`` forever (W6.1 hardware run 2 froze at
            ``capture_authorized``). Address a terminal ``capture_result``
            (accepted=false, carrying the §5.10 reason so the phone can render
            the failure screen) to the armed capture; fall back to
            ``capture_set_exhausted`` when no capture was armed. Best-effort —
            the operator wizard also shows the persisted failure.
            """
            spec = REASON_REGISTRY.get(code)
            armed = conductor.armed_capture
            if armed is not None and spec is not None:
                index, attempt = armed
                event: dict[str, Any] = {
                    "phase": HOST_PHASE_CAPTURE_RESULT,
                    "index": index,
                    "attempt": attempt,
                    "accepted": False,
                    "code": spec.code,
                    "template": spec.template,
                    "reason": spec.message or spec.banner,
                    "banner": spec.banner,
                    "auto_retry": spec.code in TRANSIENT_AUTO_RETRY_CODES,
                }
            else:
                event = {"phase": HOST_PHASE_CAPTURE_SET_EXHAUSTED}
            try:
                await asyncio.to_thread(
                    client.post_host_event,
                    pi_session.session_id,
                    pi_session.pull_token,
                    event,
                )
            except (OSError, RuntimeError, ValueError):
                logger.warning(
                    "v2 terminal host-event post failed", exc_info=True
                )

        async def _post_session_over_host_event(
            *, review_hold_timed_out: bool,
        ) -> None:
            """Tell the phone the whole SESSION ended so its deferred-retry loop
            stops waiting (W6.10 blocker #3).

            A watchdog collapse during the "waiting for apply" REVIEW hold
            (``CaptureTimeout``) otherwise left the phone re-posting the same
            ``begin_capture`` against a still-200 relay session with NO terminal
            signal — it sat on the hold screen forever (Chrome round 2: "the
            phone saw nothing"). Unlike ``_post_terminal_failure_host_event``
            this is session-level (``capture_set_exhausted``), not addressed to
            the last-armed capture (MEASURE was accepted — a per-index
            ``capture_result`` there would misreport it): the collapse is not a
            per-capture verdict. Best-effort — the purge-driven 404 the phone
            reads as ``deadSession`` is the backstop, and this post fails
            harmlessly when the failure was the relay transport itself.
            """
            event: dict[str, Any] = {
                "phase": HOST_PHASE_CAPTURE_SET_EXHAUSTED,
            }
            if review_hold_timed_out:
                event.update({
                    "code": REVIEW_HOLD_TIMED_OUT_CODE,
                    "reason": REVIEW_HOLD_TIMED_OUT_MESSAGE,
                })
            try:
                await asyncio.to_thread(
                    client.post_host_event,
                    pi_session.session_id,
                    pi_session.pull_token,
                    event,
                )
            except (OSError, RuntimeError, ValueError):
                logger.warning(
                    "v2 session-over host-event post failed", exc_info=True
                )

        try:
            opened = await volume.open()
        except (SessionVolumePlanError, MeasurementWindowError) as exc:
            # volume.open() raised BEFORE the capture loop owns cleanup — the
            # relay session is already minted (run 2's retry leaked one here
            # when the prior session's volume state was still open, firing
            # SessionVolumePlanError). Purge it best-effort before surfacing so
            # it cannot linger to worker TTL; the volume hook already released
            # any measurement pause it took.
            log_event(
                logger,
                "correction.crossover_v2_volume_open_failed",
                level=logging.WARNING,
                reason=type(exc).__name__,
            )
            await _purge_best_effort()
            raise CaptureFailed(
                "the measurement volume could not be opened"
            ) from exc
        opened_value = getattr(opened, "value", opened)
        if opened is not None and str(opened_value) != "opened":
            # The plan drained itself (emergency attenuation / failure); the
            # recovery screen keys on needs_recovery via the status block.
            # The freshly-minted relay session must not linger to worker TTL
            # when no capture will ever run against it.
            await _purge_best_effort()
            raise CaptureFailed(
                "the fixed measurement volume could not be confirmed"
            )
        plan_kwargs: dict[str, Any] = {}
        if poll_interval_s is not None:
            plan_kwargs["poll_interval_s"] = poll_interval_s
        if timeout_s is not None:
            plan_kwargs["timeout_s"] = timeout_s
        # The FIRST begin gets the wider v2 placement-reading budget (fold-in);
        # every later window (arm/upload/between-capture) keeps the tight
        # per-phase backstop. The REVIEW hold's own rescope lives in the runner.
        plan_kwargs["first_begin_timeout_s"] = (
            first_begin_timeout_s if first_begin_timeout_s is not None
            else V2_FIRST_BEGIN_TIMEOUT_S
        )
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
            try:
                _persist_terminal_failure_best_effort(conductor, code)
            finally:
                await _abandon_best_effort()
                await _purge_best_effort()
            raise
        except (
            CaptureTimeout,
            CaptureAborted,
            CaptureFailed,
            RelayError,
            OSError,
        ) as exc:
            # Relay-session death (§5.10): relay_timeout ⇒ session restart; the
            # walked-away user's volume is always drained. Tell the phone the
            # session is over BEFORE purging (W6.10 blocker #3) — mirror the
            # catch-all arm's terminal-then-grace-then-purge so a watchdog
            # collapse during the REVIEW hold reaches the phone's deferred-retry
            # loop instead of leaving it polling a still-live session forever.
            await _post_session_over_host_event(
                review_hold_timed_out=(
                    isinstance(exc, CaptureTimeout)
                    and conductor.current_phase == PHASE_REVIEW_APPLY
                ),
            )
            try:
                _persist_terminal_failure_best_effort(
                    conductor, REASON_RELAY_TIMEOUT
                )
            finally:
                await _abandon_best_effort()
                await asyncio.sleep(TERMINAL_FAILURE_PURGE_GRACE_S)
                await _purge_best_effort()
            raise
        except Exception as exc:  # noqa: BLE001 — cleanup-and-reraise, see below
            # CATCH-ALL cleanup arm (W6.1 gate ruling). The seams raise
            # open-endedly — CamillaUnavailable is a bare Exception (a DSP
            # wedge in load/restore escaped the previously-enumerated arms:
            # volume left active, relay session leaked, phone frozen at
            # capture_authorized), analyze/emit raise ValueError/RuntimeError,
            # the held measurement window raises MeasurementWindowError — so
            # ANY non-relay failure gets the same honest cleanup: tell the
            # phone (still polling capture_result), persist a terminal
            # failure, drain the volume (whose hook also releases the session
            # measurement pause), purge the relay session, then RE-RAISE so
            # the outer relay net still logs and flips /status.relay to
            # failed. Program-side classes keep their distinct
            # program_unplayable code; everything else is internal_error.
            code = (
                REASON_PROGRAM_UNPLAYABLE
                if isinstance(
                    exc,
                    (
                        ProgramPlaybackError,
                        ProgramAdmissionError,
                        CrossoverV2FlowError,
                    ),
                )
                else REASON_INTERNAL_ERROR
            )
            await _post_terminal_failure_host_event(code)
            try:
                _persist_terminal_failure_best_effort(conductor, code)
            finally:
                await _abandon_best_effort()
                # Finding H: give the just-posted terminal host event a bounded
                # grace window to reach the phone before the session is purged
                # out from under its next poll. Volume restore above stays
                # immediate — only the purge waits.
                await asyncio.sleep(TERMINAL_FAILURE_PURGE_GRACE_S)
                await _purge_best_effort()
            raise
        # Plan finished without a transport failure.
        done = conductor.current_phase == PHASE_DONE
        try:
            persist_conductor_state(
                conductor,
                failure_code=None if done else conductor.last_failure_code,
                evidence=evidence_refs,
            )
        finally:
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
    # Per-role declared datasheet sensitivities from the design draft's
    # declaration (the one owner of that fact — W6.5). Threaded into every cap
    # resolution AND the play-time readmission so the composed levels and the
    # admission gate can never disagree about a derived HF ceiling.
    declared_sensitivities: dict[str, float] = field(default_factory=dict)


def ensure_crossover_preview_ready() -> dict[str, Any]:
    """Ensure a ready crossover preview exists before a v2 session reads one.

    ``/sound/``'s Preview button was the ONLY historical writer of
    ``active_speaker_crossover_preview.json``; the v2 flow never called it, so
    a household that went straight to ``/correction/`` without visiting
    ``/sound/`` first baked its MEASURE candidate's ``source_preset`` against
    the generic bundled-preset fallback (:func:`~jasper.active_speaker.commission_wiring.resolve_capture_preset`'s
    no-preview branch) — which then can NEVER match a preview generated later,
    so Apply refuses ``measured_candidate_preset_mismatch`` forever. This is
    called at the top of :func:`resolve_conductor_context` — the one place
    both session-open (:func:`prepare_v2_session`) and the verify re-arm
    (:func:`prepare_v2_verify`) resolve the design draft/topology — so the
    fallback branch is never reached from a v2 entry point again.

    Reuses the SAME generator ``/sound/`` drives
    (:func:`~jasper.active_speaker.web_commissioning.regenerate_crossover_preview_from_current_draft`,
    itself a thin wrapper around :func:`~jasper.active_speaker.crossover_preview.save_crossover_preview`)
    rather than reimplementing preview generation. Idempotent: an existing
    preview that is already ``ready_for_protected_staging`` for the CURRENT
    design draft (the freshness/fingerprint check already built into
    :func:`~jasper.active_speaker.crossover_preview.load_crossover_preview`)
    is left byte-untouched — reused, not regenerated. Anything else (absent,
    stale, or blocked) is regenerated once; if the fresh attempt still cannot
    reach ``ready_for_protected_staging`` (an unconfirmed safety profile, a
    blocked design draft, etc.), this raises a named :class:`CrossoverV2Refused`
    pointing at ``/sound/`` instead of leaving the surprise for apply time.
    """
    from jasper.active_speaker.crossover_preview import load_crossover_preview
    from jasper.active_speaker.design_draft import load_design_draft
    from jasper.active_speaker.web_commissioning import (
        regenerate_crossover_preview_from_current_draft,
    )

    preview = load_crossover_preview(current_design_draft=load_design_draft())
    outcome = "reused"
    if preview.get("status") != "ready_for_protected_staging":
        preview = regenerate_crossover_preview_from_current_draft()
        outcome = (
            "generated"
            if preview.get("status") == "ready_for_protected_staging"
            else "refused"
        )
    log_event(
        logger,
        "correction.crossover_v2_preview_ensured",
        outcome=outcome,
        preview_status=str(preview.get("status")),
    )
    if outcome == "refused":
        messages = [
            str(issue.get("message") or issue.get("code"))
            for issue in (preview.get("issues") or [])
            if isinstance(issue, Mapping) and issue.get("severity") == "blocker"
        ]
        raise CrossoverV2Refused(
            "the crossover preview is not ready for measurement; finish "
            "speaker setup at /sound/ first"
            + (": " + "; ".join(messages[:2]) if messages else "")
        )
    return preview


def resolve_conductor_context(status: Mapping[str, Any]) -> V2ConductorContext:
    """Resolve preset/bands/caps/targets/volume from live status + topology.

    Fail-closed: every missing input is a :class:`CrossoverV2Refused` naming
    what to finish first — never a guessed default.
    """
    from jasper.active_speaker.commission_wiring import resolve_capture_preset
    from jasper.active_speaker.crossover_v2_flow import derive_session_volume_db
    from jasper.active_speaker.design_draft import (
        declared_driver_sensitivities,
        load_design_draft,
    )
    from jasper.active_speaker.excitation_safety_plan import (
        ExcitationSafetyPlanError,
        resolve_driver_excitation_ceilings,
    )
    from jasper.active_speaker.playback_route import resolve_active_playback_device
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
    # Ensure a ready crossover preview BEFORE resolving the capture preset —
    # otherwise resolve_capture_preset's no-preview fallback silently bakes
    # the generic bundled preset into every MEASURE candidate (see docstring).
    ensure_crossover_preview_ready()
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
    # The declaration's per-role datasheet sensitivities (the one owner of
    # that fact — W6.5), threaded into every cap resolution below.
    declared_sensitivities = declared_driver_sensitivities(draft)
    roles_bands = []
    caps: dict[str, float] = {}
    for channel, role in enumerate(("woofer", "tweeter")):
        try:
            # program_admission=True: this context exists solely to serve the
            # admission-gated CHECK/MEASURE programs, whose channel routing
            # carries each driver's crossover filter (the tweeter's protective
            # HP included) by construction — the proven-HP path, the same
            # justification as session_volume_plan. Without it the W6.5
            # derived HF ceiling is inert exactly where it matters: these
            # context caps clamp every composed level (CHECK pilot bases,
            # MEASURE back_off_gain, VERIFY min(caps)).
            band, cap = resolve_driver_excitation_ceilings(
                safety_profile,
                role_targets[role],
                program_admission=True,
                declared_sensitivities=declared_sensitivities,
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
        safety_profile,
        [role_targets["woofer"], role_targets["tweeter"]],
        declared_sensitivities=declared_sensitivities,
    )
    playback_device, _playback_device_source = resolve_active_playback_device(
        topology
    )
    playback_device = str(playback_device or "")
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
        # W6 CHECKLIST ITEM: driver_spacing_m stays 0.0 until a declared
        # woofer↔tweeter spacing input exists (topology/preset carry none
        # today), so the §3.2 parallax correction is INERT — the analysis
        # subtracts nothing. Do not assume VERIFY covers this: a missing
        # parallax correction is SELF-CANCELLING at the mic position (the
        # same geometric excess is baked into both MEASURE and VERIFY), so
        # VERIFY passes while the LISTENING POSITION carries the full error
        # (~23° at 2 kHz for 15 cm spacing measured at 1 m).
        # W6 CHECKLIST ITEM (pre-existing): a deliberate household volume
        # action mid-session (dial / voice "louder" / :8780 HTTP) still moves
        # the CamillaDSP main volume — the session measurement pause holds off
        # the idle reconciler, not VolumeCoordinator writes. W6 validation
        # runs hands-off; a session-long volume guard is a follow-up.
        driver_spacing_m=0.0,
        topology=topology,
        playback_device=playback_device,
        role_channels={"woofer": 0, "tweeter": 1},
        declared_sensitivities=declared_sensitivities,
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


def _purge_opened_relay_capture(client: Any, rc: Any) -> None:
    """Best-effort rollback for a relay session minted before host open failed."""

    from jasper.capture_relay.session import purge

    try:
        purge(client, rc.pi_session)
    except (OSError, RuntimeError, ValueError):
        log_event(
            logger,
            "correction.crossover_v2_open_failure_purge_failed",
            level=logging.ERROR,
        )


def _volume_hooks(camilla_factory: Any, context: V2ConductorContext) -> V2VolumeHooks:
    plan = session_volume_plan()
    # The shared IO pair converts CamillaUnavailable (a bare Exception) into
    # RuntimeError, which plan.open's internal catches and set_and_confirm's
    # fail-closed contract actually cover — a DSP wedge during open then drains
    # to a non-OPENED result instead of raising an unhandled exception past the
    # runner (the W6.1 gate's escape class).
    _set, _get = _session_volume_io(camilla_factory)

    async def _open() -> Any:
        # Hold voice paused for the WHOLE session BEFORE setting the volume, so
        # the idle reconciler cannot revert the measurement volume in the gap
        # before the first play (W6.1). If the volume does not actually open —
        # a non-OPENED result OR any raise — release the pause in the finally
        # so a failed open never strands voice paused.
        await acquire_session_measurement_pause()
        opened: Any = None
        try:
            opened = await plan.open(context.session_volume_db, _set, _get)
        finally:
            if str(getattr(opened, "value", opened)) != "opened":
                await release_session_measurement_pause()
        return opened

    async def _close() -> Any:
        try:
            return await plan.close(_set, _get)
        finally:
            await release_session_measurement_pause()

    async def _abandon() -> Any:
        try:
            return await plan.abandon(_set, _get)
        finally:
            await release_session_measurement_pause()

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
    reconcile_stale_v2_transaction(run_async, camilla_factory)
    if session_volume_plan().needs_recovery:
        raise CrossoverV2Refused(
            "the measurement volume needs recovery; recover it before starting "
            "a new session"
        )
    # W6.1 E1/E3-at-open: force-drain a stale-active (ceiling) and reset a
    # residual owned-active leftover so plan.open() starts clean — the silent
    # 200→adapter_failed loop happened when a prior session left the volume
    # active and open() then refused. Runs AFTER the needs_recovery gate (which
    # refused a crash-active / unresolved state toward the recover screen), then
    # re-gates in case a drain here could not confirm and latched unresolved.
    reconcile_session_volume_for_new_session(run_async, camilla_factory)
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
            applied=_candidate_applied_for_session(
                prior_raw, str(prior_raw.get("session_id") or "")
            ),
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
            default_setup_calibration=default_setup_calibration_for_v2(),
        ).with_return_url(return_url)
        rc = correction_adapter.open_capture(
            client,
            spec,
            relay_base=base,
            capture_origin=capture_origin,
            return_url=return_url,
        )
        try:
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
                declared_sensitivities=context.declared_sensitivities,
            )
            analyze = bind_production_analyze(meta=refs)
            verify_authority = _v2_verify_authority_runner(
                run_async=run_async,
                camilla_factory=camilla_factory,
                session_id=relay_session_id,
                expected_topology=context.topology,
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
                    analyze=analyze,
                    publish_check=publish_check,
                    publish_candidate=publish_candidate,
                    apply_complete=lambda: _applied_gate_for_session(
                        relay_session_id
                    ),
                ),
                driver_spacing_m=context.driver_spacing_m,
            )
            persist_conductor_state(
                conductor,
                failure_code=None,
                evidence=refs,
                allow_session_rebind=True,
            )
            holder["run"] = build_v2_run_and_consume(
                conductor,
                volume=_volume_hooks(camilla_factory, context),
                stop_event=stop_event,
                stop_lock=stop_lock,
                verify_authority=verify_authority,
                evidence_refs=refs,
            )
        except Exception:
            _purge_opened_relay_capture(client, rc)
            raise
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
    reconcile_stale_v2_transaction(run_async, camilla_factory)
    if session_volume_plan().needs_recovery:
        raise CrossoverV2Refused(
            "the measurement volume needs recovery; recover it before verifying"
        )
    state = load_v2_state()
    if not state or not _candidate_applied_for_session(
        state, str(state.get("session_id") or "")
    ):
        raise CrossoverV2Refused(
            "verification needs an applied measured crossover; measure and "
            "apply first"
        )
    expected_applied_state = {
        "session_id": state.get("session_id"),
        "applied_session_id": _candidate_applied_session_id(state),
        "applied_profile_fingerprint": state.get(
            "applied_profile_fingerprint"
        ),
        "applied_live_graph_fingerprint": state.get(
            "applied_live_graph_fingerprint"
        ),
        "pre_apply_profile": state.get("pre_apply_profile"),
    }
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
        current = load_v2_state()
        current_binding = (
            _candidate_applied_session_id(current)
            if isinstance(current, Mapping)
            else ""
        )
        if (
            not current
            or not _candidate_applied_for_session(
                current, str(current.get("session_id") or "")
            )
            or _reservation_mapping(current) is not None
            or any(
                (current_binding if key == "applied_session_id" else current.get(key))
                != value
                for key, value in expected_applied_state.items()
            )
        ):
            raise CrossoverV2Refused(
                "the applied crossover changed before verification could start; "
                "refresh the page and review the current sound"
            )
        spec = build_v2_verify_session_spec(
            context.fc_hz,
            acknowledgement_binding=acknowledgement_binding,
            default_setup_calibration=default_setup_calibration_for_v2(),
        ).with_return_url(return_url)

        async def _validate_and_open() -> Any:
            from jasper.active_speaker.baseline_profile import (
                load_applied_baseline_profile_state_strict,
            )
            from jasper.dsp_apply import dsp_writer_lock

            async with dsp_writer_lock(
                _v2_dsp_lock_dir(), source="crossover_v2_verify_admission"
            ):
                current = load_v2_state()
                current_binding = (
                    _candidate_applied_session_id(current)
                    if isinstance(current, Mapping)
                    else ""
                )
                if (
                    not current
                    or not _candidate_applied_for_session(
                        current, str(current.get("session_id") or "")
                    )
                    or _reservation_mapping(current) is not None
                    or any(
                        (
                            current_binding
                            if key == "applied_session_id"
                            else current.get(key)
                        )
                        != value
                        for key, value in expected_applied_state.items()
                    )
                ):
                    raise CrossoverV2Refused(
                        "the applied crossover changed before verification could "
                        "start; refresh the page and review the current sound"
                    )
                try:
                    state_file_present, applied_profile = (
                        load_applied_baseline_profile_state_strict()
                    )
                    profile_fingerprint = _profile_candidate_identity(applied_profile)
                    profile_graph_fingerprint = (
                        _profile_graph_fingerprint(applied_profile)
                        if isinstance(applied_profile, Mapping)
                        else ""
                    )
                    profile_topology_fingerprint = (
                        _profile_topology_fingerprint(applied_profile)
                        if isinstance(applied_profile, Mapping)
                        else ""
                    )
                    live_graph_fingerprint = (
                        await _current_camilla_graph_fingerprint(camilla_factory())
                    )
                except (OSError, UnicodeError, RuntimeError, ValueError) as exc:
                    raise CrossoverV2Refused(
                        "the applied speaker graph could not be proved before "
                        "verification; refresh the page and review Speaker setup"
                    ) from exc
                if (
                    not state_file_present
                    or profile_fingerprint
                    != str(current.get("applied_profile_fingerprint") or "")
                    or profile_graph_fingerprint
                    != str(current.get("applied_live_graph_fingerprint") or "")
                    or live_graph_fingerprint != profile_graph_fingerprint
                    or profile_topology_fingerprint
                    != topology_config_fingerprint(context.topology)
                ):
                    raise CrossoverV2Refused(
                        "the applied speaker graph changed before verification could "
                        "start; refresh the page and review the current sound"
                    )
                return correction_adapter.open_capture(
                    client,
                    spec,
                    relay_base=base,
                    capture_origin=capture_origin,
                    return_url=return_url,
                )

        from jasper.active_speaker.baseline_profile import topology_config_fingerprint
        from jasper.output_topology import (
            load_output_topology_strict,
            output_topology_mutation_lock,
        )

        with output_topology_mutation_lock():
            current_topology = load_output_topology_strict()
            if topology_config_fingerprint(
                current_topology
            ) != topology_config_fingerprint(context.topology):
                raise CrossoverV2Refused(
                    "the output topology changed before verification could start; "
                    "refresh the page and measure again"
                )
            rc = run_async(_validate_and_open())
        try:
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
                declared_sensitivities=context.declared_sensitivities,
            )
            analyze = bind_production_analyze(meta=refs)
            verify_authority = _v2_verify_authority_runner(
                run_async=run_async,
                camilla_factory=camilla_factory,
                session_id=relay_session_id,
                expected_topology=context.topology,
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
                    analyze=analyze,
                    publish_check=_publish_check,
                    publish_candidate=publish_candidate,
                    apply_complete=lambda: _applied_gate_for_session(
                        relay_session_id
                    ),
                ),
                driver_spacing_m=context.driver_spacing_m,
                accepted_phases=(PHASE_CHECK, PHASE_MEASURE),
                applied=True,
                gain_plan_db=state.get("gain_plan_db"),
                index_phase_map={1: PHASE_VERIFY},
                measure_predicted_sum=predicted_sum,
                measure_gate_window_ms=(
                    float(gate_ms)
                    if isinstance(gate_ms, (int, float))
                    else None
                ),
            )
            # Keep the durable candidate/applied facts; rebind the session id.
            persist_conductor_state(
                conductor,
                failure_code=None,
                evidence=refs,
                allow_session_rebind=True,
            )
            holder["run"] = build_v2_run_and_consume(
                conductor,
                volume=_volume_hooks(camilla_factory, context),
                stop_event=stop_event,
                stop_lock=stop_lock,
                verify_authority=verify_authority,
                evidence_refs=refs,
            )
        except Exception:
            _purge_opened_relay_capture(client, rc)
            raise
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


def _profile_config_path(profile: Mapping[str, Any] | None) -> str:
    config = profile.get("config") if isinstance(profile, Mapping) else None
    return str(config.get("path") or "") if isinstance(config, Mapping) else ""


def _prune_v2_candidate_configs_after_attempt(
    run_async: Any,
    cam: Any,
) -> tuple[Path, ...]:
    """Bound candidates after every terminal Apply attempt under DSP authority.

    Success, policy blocks, transactional failures, and raised endpoint errors
    can all leave a newly emitted content-addressed sibling. Re-read the
    authoritative applied/Undo/live paths while holding the shared writer lock;
    any unresolved reference or recovery fence leaks safely instead of guessing.
    """

    async def _prune() -> tuple[Path, ...]:
        from jasper.active_speaker.baseline_profile import (
            baseline_config_path,
            load_applied_baseline_profile_state_strict,
            prune_baseline_candidate_configs,
        )
        from jasper.dsp_apply import dsp_writer_lock

        async with dsp_writer_lock(
            _v2_dsp_lock_dir(), source="crossover_v2_candidate_retention"
        ):
            state = load_v2_state()
            if _reservation_mapping(state) is not None:
                log_event(
                    logger,
                    "correction.crossover_v2_candidate_prune_skipped",
                    level=logging.WARNING,
                    reason="transaction_recovery_fenced",
                )
                return ()
            try:
                _state_file_present, applied_profile = (
                    load_applied_baseline_profile_state_strict()
                )
            except (OSError, ValueError):
                log_event(
                    logger,
                    "correction.crossover_v2_candidate_prune_skipped",
                    level=logging.WARNING,
                    reason="applied_profile_unreadable",
                )
                return ()
            # CamillaClient exposes the authoritative loaded path through the
            # same API used by apply_baseline_profile. Keep retention on that
            # established boundary so it works for the real client as well as
            # every apply-path test double.
            getter = getattr(cam, "get_config_file_path", None)
            if not callable(getter):
                return ()
            live_path = str(await getter(best_effort=False) or "")
            applied_path = _profile_config_path(applied_profile)
            undo_profile = (state or {}).get("pre_apply_profile")
            undo_path = _profile_config_path(
                undo_profile if isinstance(undo_profile, Mapping) else None
            )
            if not live_path:
                reason = "live_config_path_unresolved"
            elif isinstance(applied_profile, Mapping) and not applied_path:
                reason = "applied_config_path_unresolved"
            elif isinstance(undo_profile, Mapping) and not undo_path:
                reason = "undo_config_path_unresolved"
            else:
                reason = ""
            if reason:
                log_event(
                    logger,
                    "correction.crossover_v2_candidate_prune_skipped",
                    level=logging.WARNING,
                    reason=reason,
                )
                return ()
            removed = prune_baseline_candidate_configs(
                baseline_config_path(),
                protected_paths=[applied_path, live_path, undo_path],
                keep_recent=V2_CANDIDATE_CONFIG_KEEP_RECENT,
            )
            if removed:
                log_event(
                    logger,
                    "correction.crossover_v2_candidates_pruned",
                    removed_count=len(removed),
                )
            return removed

    try:
        return run_async(_prune())
    except Exception as exc:  # noqa: BLE001 — cleanup must not mask Apply outcome
        log_event(
            logger,
            "correction.crossover_v2_candidate_prune_failed",
            level=logging.WARNING,
            err=repr(exc),
        )
        return ()


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

    W6 run-6 Blocker M: the seam's own freshness guard
    (``apply_baseline_profile``'s ``expected_candidate_fingerprint``) compares
    against ``baseline_candidate_fingerprint`` — the COMPOSED baseline
    candidate's own identity — never the MEASURED candidate's fingerprint
    this endpoint reviews with the household. Forwarding the measured
    fingerprint straight through made every apply refuse
    ``baseline_candidate_fingerprint_mismatch``, unconditionally. This host
    translates between the two vocabularies: it composes the baseline
    candidate read-only first (``build_baseline_profile_candidate(...,
    write=False)`` — the exact builder the seam itself uses), asserts that
    composition is still bound to the reviewed MEASURED candidate (preserving
    the review-freshness guarantee at the measured-candidate level), then
    passes the COMPOSED candidate's own ``candidate_fingerprint`` through to
    the seam.
    """
    from jasper.active_speaker.baseline_profile import (
        apply_baseline_profile,
        build_baseline_profile_candidate,
        topology_config_fingerprint,
    )
    from jasper.active_speaker.crossover_preview import load_crossover_preview
    from jasper.active_speaker.design_draft import load_design_draft
    from jasper.active_speaker.measured_crossover_candidate import (
        MeasuredCrossoverCandidate,
        MeasuredCrossoverCandidateError,
    )
    from jasper.active_speaker.measurement import load_measurement_state
    from jasper.output_topology import (
        load_output_topology,
        load_output_topology_strict,
        output_topology_mutation_lock,
    )

    if not v2_flow_active():
        raise CrossoverV2Refused(
            "the v2 crossover flow is not active — set JASPER_CROSSOVER_FLOW=v2"
        )
    reconcile_stale_v2_transaction(run_async, camilla_factory)
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
    producer_session_id = str((state or {}).get("session_id") or "")
    accepted_phases = set((state or {}).get("accepted_phases") or ())
    from jasper.active_speaker.crossover_v2_flow import PHASE_CHECK, PHASE_MEASURE

    if (
        not producer_session_id
        or not {PHASE_CHECK, PHASE_MEASURE}.issubset(accepted_phases)
        or (state or {}).get("failure")
        or _candidate_applied_for_session(state, producer_session_id)
    ):
        raise CrossoverV2Refused(
            "this measurement session is no longer ready to apply; start the "
            "measurement again"
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

    # Blocker M translation: compose read-only (the seam's own build_candidate
    # closure re-derives this identically under its writer lock, so this is a
    # deterministic recompose, not a second opinion) and confirm the
    # composition is still bound to the reviewed measured candidate before
    # asking the seam to apply anything.
    reviewed_baseline = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
        write=False,
        tuning_owner="automatic",
        measured_candidate=candidate,
    )
    reviewed_measured_fingerprint = str(
        (reviewed_baseline.get("source") or {}).get("measured_candidate_fingerprint")
        or ""
    )
    if reviewed_measured_fingerprint != expected:
        raise CrossoverV2Refused(
            "the reviewed crossover is no longer current; review the newest "
            "measurement before applying"
        )
    baseline_expected_fingerprint = str(
        reviewed_baseline.get("candidate_fingerprint") or ""
    )

    def _refresh_apply_inputs() -> tuple[Any, Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]:
        current_topology = load_output_topology_strict()
        current_draft = load_design_draft(topology=current_topology)
        current_preview = load_crossover_preview(current_design_draft=current_draft)
        current_measurements = load_measurement_state(current_topology)
        return (
            current_topology,
            current_draft,
            current_preview,
            current_measurements,
        )

    async def _assert_review_still_current() -> None:
        current = load_v2_state()
        reservation = (
            current.get("apply_reservation")
            if isinstance(current, Mapping)
            else None
        )
        if not isinstance(reservation, Mapping) or str(
            reservation.get("token") or ""
        ) != reservation_token or _pending_terminal_failure_code(current):
            raise CrossoverV2Refused(
                "this measurement session changed before Apply reached the "
                "speaker; start the measurement again"
            )

    retained_predecessor: dict[str, Mapping[str, Any] | None] = {"profile": None}

    async def _commit_after_apply(
        payload: Mapping[str, Any], locked_candidate: Mapping[str, Any]
    ) -> None:
        # Runs before apply_baseline_profile releases the shared DSP writer
        # lock. The predecessor comes from THIS in-lock candidate, never the
        # speculative pre-admission composition above.
        pre_apply_profile = retained_predecessor["profile"]
        applied_profile = payload.get("profile")
        live_graph_fingerprint = (
            _profile_graph_fingerprint(applied_profile)
            if isinstance(applied_profile, Mapping)
            else None
        )
        observe_apply_success(
            expected,
            pre_apply_profile=pre_apply_profile,
            applied_profile=(
                applied_profile if isinstance(applied_profile, Mapping) else None
            ),
            applied_live_graph_fingerprint=live_graph_fingerprint,
            session_id=producer_session_id,
            reservation_token=reservation_token,
        )

    async def _journal_apply_started(
        locked_candidate: Mapping[str, Any],
    ) -> None:
        getter = getattr(cam, "get_active_config_raw", None)
        if not callable(getter):
            raise CrossoverV2Refused(
                "the current speaker graph cannot be retained safely for Undo"
            )
        active_raw = await getter(best_effort=False)
        from jasper.active_speaker.baseline_profile import (
            normalized_camilla_graph_fingerprint,
        )

        source_live_graph_fingerprint = normalized_camilla_graph_fingerprint(
            active_raw
        )
        source_profile = locked_candidate.get("applied_recomposition_profile")
        retained_predecessor["profile"] = _snapshot_pre_apply_profile(
            source_profile if isinstance(source_profile, Mapping) else None,
            active_raw,
        )
        _mark_apply_mutation_started(
            reservation_token,
            locked_candidate,
            source_live_graph_fingerprint=source_live_graph_fingerprint,
            retained_pre_apply_profile=retained_predecessor["profile"],
        )

    cam = camilla_factory()
    issue = None
    with output_topology_mutation_lock():
        # Reconciliation in this request must reuse the topology identity
        # proved under the lock it already owns. Re-entering the file lock in
        # _finish_v2_reservation would self-deadlock on a rolled-back Apply.
        locked_topology_fingerprint = topology_config_fingerprint(
            load_output_topology_strict()
        )
        reservation_token = _reserve_v2_apply(producer_session_id, expected)
        try:
            try:
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
                        expected_candidate_fingerprint=(
                            baseline_expected_fingerprint
                        ),
                        # This runs after all candidate proof but BEFORE
                        # apply_dsp_config's load/rollback arm. A stale refusal
                        # never rolls back a candidate that was not loaded.
                        on_candidate_verified=_assert_review_still_current,
                        on_apply_started=_journal_apply_started,
                        on_apply_success=_commit_after_apply,
                        measured_candidate=candidate,
                        refresh_inputs=_refresh_apply_inputs,
                    )
                )
                if payload.get("status") == "blocked":
                    # Publish the nudge while this exact attempt still owns
                    # the reservation. An older blocked response must never
                    # race a newer same-session retry and resurrect stale copy.
                    issue = _blocking_apply_issue(payload)
                    if issue is not None:
                        payload["issue"] = issue
                    _persist_apply_blocked(
                        issue,
                        session_id=producer_session_id,
                        candidate_fingerprint=expected,
                        reservation_token=reservation_token,
                    )
            finally:
                # Success clears this inside the in-lock state commit;
                # blocked/failed admissions release directly. Once mutation
                # started, reconcile before the frontier can be reused.
                _finish_v2_reservation(
                    reservation_token,
                    run_async,
                    camilla_factory,
                    current_topology_fingerprint=locked_topology_fingerprint,
                )
        finally:
            # The compiler can emit a sibling before any terminal outcome,
            # including a block or raised failure. Re-enter the writer boundary
            # and bound that family without masking the Apply result.
            _prune_v2_candidate_configs_after_attempt(run_async, cam)
    if payload.get("status") == "blocked":
        # The compact blocker was persisted under this attempt's reservation;
        # emit the terminal event only after all cleanup has completed.
        log_event(
            logger,
            "correction.crossover_v2_apply_blocked",
            level=logging.WARNING,
            issue_id=(issue or {}).get("id", ""),
        )
    log_event(
        logger,
        "correction.crossover_v2_apply",
        status=payload.get("status"),
        candidate_fingerprint=expected,
    )
    return payload


def handle_v2_restore(
    run_async: Any,
    camilla_factory: Any,
) -> dict[str, Any]:
    """POST /crossover/v2/restore — the v2-aware Undo (§5.2 verify_fail).

    The legacy ``/crossover/restore`` expects a PENDING candidate-apply
    transaction from the per-driver commissioning-run machinery
    (``commissioning_apply.restore_pending_candidate_apply``); a v2 apply
    never creates one — :func:`handle_v2_apply` commits straight through
    :func:`~jasper.active_speaker.baseline_profile.apply_baseline_profile`'s
    atomic transaction, so by the time a household reaches ``verify_fail``
    there is nothing "pending" left for the legacy path to find, and it
    500s (W6 run-8 Blocker Q: ``there is no pending candidate apply to
    restore``). This is the v2-aware replacement: reload the pre-candidate
    applied profile ``handle_v2_apply`` stashed (``pre_apply_profile`` in the
    durable v2 state) through the SAME apply transaction family
    (:func:`~jasper.active_speaker.baseline_profile.restore_applied_baseline_profile`),
    then clear the durable v2 applied/candidate/failure state so the
    envelope returns to a clean measure/review state.

    Never raises a bare exception for an ordinary refusal — every refusal is
    a :class:`CrossoverV2Refused` (maps to 400 at the dispatch ladder,
    exactly like every other v2 endpoint refusal) so a household stuck on a
    bad-sounding candidate always gets a named answer, never a 500.
    """
    from jasper.active_speaker.baseline_profile import (
        restore_applied_baseline_profile,
        topology_config_fingerprint,
    )
    from jasper.output_topology import (
        load_output_topology_strict,
        output_topology_mutation_lock,
    )

    if not v2_flow_active():
        raise CrossoverV2Refused(
            "the v2 crossover flow is not active — set JASPER_CROSSOVER_FLOW=v2"
        )
    reconcile_stale_v2_transaction(run_async, camilla_factory)
    with _state_lock:
        state = load_v2_state()
        if not state or not state.get("applied"):
            raise CrossoverV2Refused(
                "nothing is applied to undo; measure and apply a crossover first"
            )
        upgraded = _upgrade_legacy_undo_identity(state)
        if upgraded != state:
            save_v2_state(upgraded)
            state = upgraded
    pre_apply_profile = state.get("pre_apply_profile")
    if not isinstance(pre_apply_profile, Mapping):
        # Now that persist_conductor_state carries pre_apply_profile forward
        # across every conductor snapshot (W6.12 P0), this branch is reached
        # ONLY on a genuine first-ever apply — there was never an earlier
        # profile to stash. Say so plainly rather than reading like a bug
        # report or a generic "no undo available" error.
        raise CrossoverV2Refused(
            "this is the first measured crossover on this speaker — there's "
            "no earlier one to restore; use Speaker setup to remove it instead"
        )
    snapshot = pre_apply_profile.get("recomposition_snapshot")
    stashed_topology_fingerprint = (
        str(snapshot.get("topology_fingerprint") or "")
        if isinstance(snapshot, Mapping)
        else ""
    )
    if not stashed_topology_fingerprint:
        raise CrossoverV2Refused(
            "the previous crossover does not record which output topology it "
            "was built for; re-measure the crossover before trying to restore it"
        )
    reservation_token: str | None = None
    try:
        with output_topology_mutation_lock():
            current_topology = load_output_topology_strict()
            if (
                stashed_topology_fingerprint
                != topology_config_fingerprint(current_topology)
            ):
                raise CrossoverV2Refused(
                    "the output topology changed since the previous crossover was "
                    "saved; re-measure the crossover before trying to restore it"
                )
            applied_profile_fingerprint = str(
                state.get("applied_profile_fingerprint") or ""
            )
            applied_live_graph_fingerprint = str(
                state.get("applied_live_graph_fingerprint") or ""
            )
            if not applied_profile_fingerprint or not applied_live_graph_fingerprint:
                raise CrossoverV2Refused(
                    "this Undo cannot prove which active speaker graph it would "
                    "replace; re-measure before restoring anything"
                )
            reservation_token = _reserve_v2_restore(state)

            async def _commit_restore(_restored: Mapping[str, Any]) -> None:
                observe_restore(reservation_token=reservation_token)

            async def _journal_restore_started(
                retained: Mapping[str, Any],
            ) -> None:
                _mark_restore_mutation_started(reservation_token, retained)

            cam = camilla_factory()
            payload = run_async(
                restore_applied_baseline_profile(
                    pre_apply_profile,
                    load_config=lambda path: cam.set_config_file_path(
                        path, best_effort=False
                    ),
                    get_current_config_path=lambda: cam.get_config_file_path(
                        best_effort=False
                    ),
                    expected_current_candidate_fingerprint=(
                        applied_profile_fingerprint
                    ),
                    expected_current_graph_fingerprint=(
                        applied_live_graph_fingerprint
                    ),
                    get_current_graph_fingerprint=lambda: (
                        _current_camilla_graph_fingerprint(cam)
                    ),
                    on_restore_started=_journal_restore_started,
                    on_restore_success=_commit_restore,
                )
            )
    finally:
        if reservation_token is not None:
            _finish_v2_reservation(
                reservation_token, run_async, camilla_factory
            )
    log_event(
        logger,
        "correction.crossover_v2_restored",
        status=payload.get("status"),
    )
    return payload


def _blocking_apply_issue(payload: Mapping[str, Any]) -> dict[str, str] | None:
    """The single most relevant blocker from a blocked apply payload.

    ``payload["issues"]`` already carries the full severity-tagged list; this
    picks the first blocker (the seam always orders the real cause before any
    generic trailer issue) so a compact ``{id, message}`` pointer reaches the
    browser without digging through the composed profile.
    """
    issues = payload.get("issues")
    if not isinstance(issues, list):
        return None
    candidates = [issue for issue in issues if isinstance(issue, Mapping)]
    for issue in candidates:
        if issue.get("severity") == "blocker":
            return {
                "id": str(issue.get("code") or ""),
                "message": str(issue.get("message") or ""),
            }
    if candidates:
        first = candidates[0]
        return {
            "id": str(first.get("code") or ""),
            "message": str(first.get("message") or ""),
        }
    return None


def _persist_apply_blocked(
    issue: Mapping[str, str] | None,
    *,
    session_id: str,
    candidate_fingerprint: str,
    reservation_token: str | None = None,
) -> None:
    """Record this attempt's blocked-apply nudge under its reservation."""
    with _state_lock:
        state = load_v2_state()
        candidate = (state or {}).get("candidate")
        if (
            state is None
            or str(state.get("session_id") or "") != session_id
            or not isinstance(candidate, Mapping)
            or str(candidate.get("fingerprint") or "") != candidate_fingerprint
            or _apply_reservation_token(state) != str(reservation_token or "")
        ):
            log_event(
                logger,
                "correction.crossover_v2_apply_blocked_dropped",
                level=logging.INFO,
                reason="producing_attempt_changed",
            )
            return
        state["apply_blocked"] = (
            {**dict(issue), "session_id": session_id} if issue else None
        )
        save_v2_state(state, reservation_token=reservation_token)


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
