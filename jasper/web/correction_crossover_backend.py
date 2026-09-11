# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Correction-side active-crossover levels and saved measurement status."""

from __future__ import annotations

import logging
import math
import threading
from typing import Any, Mapping

from jasper.active_speaker import web_commissioning
from jasper.active_speaker.crossover_v2.conductor_context import conductor_status
from jasper.log_event import log_event

logger = logging.getLogger(__name__)


class CrossoverLevelLease:
    """In-process repeat-progress cache feeding the crossover status payload.

    Tracks the comparison context a level run is keyed to and caches the
    durable repeat-admission snapshot (``set_durable_repeat_progress``) so
    ``/crossover/status`` can render it (``level_match_snapshot``) without
    re-reading the repeat-admission ledger on every poll. A thin domain
    owner: single-flight lifetime and observability for the active-crossover
    status/reset surface. It deliberately owns no CamillaDSP client.
    """

    def __init__(self) -> None:
        self.session_id = "active-crossover"
        self._level_result_lock = threading.RLock()
        self.context_id: str | None = None
        self.noise_floor_db = None
        self.mic_calibration = None
        self.input_device = None
        self._repeat_lock = threading.RLock()
        self._repeat_failures: dict[str, dict[str, Any]] = {}
        self._durable_repeat_progress: dict[str, Any] = {}

    def invalidate_comparison_context(self) -> None:
        """Drop a prior lock/setup before a newly acquired level run begins."""

        with self._level_result_lock:
            self.context_id = None
            self.noise_floor_db = None
            self.mic_calibration = None
            self.input_device = None
            self._repeat_failures = {}
            self._durable_repeat_progress = {}
        log_event(
            logger,
            "correction.crossover_level_context_invalidated",
        )

    def set_durable_repeat_progress(self, payload: Mapping[str, Any]) -> None:
        from jasper.active_speaker.crossover_eligibility import (
            mapping_sequence,
            nonnegative_int,
        )
        from jasper.active_speaker.repeat_admission import MAX_RESERVATIONS

        def public_result(value: Any) -> dict[str, Any] | None:
            if not isinstance(value, Mapping):
                return None
            attempt = nonnegative_int(value.get("attempt"))
            accepted = value.get("accepted")
            if not 1 <= attempt <= MAX_RESERVATIONS or not isinstance(accepted, bool):
                return None
            public: dict[str, Any] = {
                "attempt": attempt,
                "accepted": accepted,
            }
            string_fields = (
                "reject_reason",
                "failure_type",
                "snr_verdict",
                "worst_band_id",
                "phase",
            )
            numeric_fields = (
                "estimated_snr_db",
                "snr_shortfall_db",
                "validity_floor_hz",
            )
            bool_fields = ("clipping", "above_validity_floor", "audio_emitted")
            for key in string_fields:
                item = value.get(key)
                if item is None:
                    continue
                if not isinstance(item, str):
                    return None
                public[key] = item
            for key in numeric_fields:
                item = value.get(key)
                if item is None:
                    continue
                if (
                    isinstance(item, bool)
                    or not isinstance(item, (int, float))
                    or not math.isfinite(float(item))
                ):
                    return None
                public[key] = float(item)
            for key in bool_fields:
                item = value.get(key)
                if item is None:
                    continue
                if not isinstance(item, bool):
                    return None
                public[key] = item
            return public

        def malformed_entry() -> dict[str, Any]:
            return {
                "target_id": None,
                "target_fingerprint": None,
                "attempts": 0,
                "status": "malformed",
                "inflight": False,
                "results": [],
                "reason": "malformed_durable_repeat_state",
                "updated_at": None,
            }

        def public_entry(value: Any) -> dict[str, Any]:
            if not isinstance(value, Mapping):
                return malformed_entry()
            attempts = nonnegative_int(value.get("attempts"))
            raw_results = value.get("results")
            result_items = mapping_sequence(raw_results)
            results = [public_result(item) for item in result_items]
            status = value.get("status")
            target_id = value.get("target_id")
            target_fingerprint = value.get("target_fingerprint")
            reason = value.get("reason")
            updated_at = value.get("updated_at")
            inflight = value.get("inflight")
            projected_results = [result for result in results if result is not None]
            result_attempts = [result["attempt"] for result in projected_results]
            full_coverage = (
                inflight is None
                and result_attempts == list(range(1, attempts + 1))
            )
            interrupted_coverage = bool(
                status == "aborted"
                and inflight is None
                and result_attempts == list(range(1, attempts))
            )
            inflight_coverage = bool(
                isinstance(inflight, str)
                and inflight
                and status == "active"
                and result_attempts == list(range(1, attempts))
            )
            if (
                not 1 <= attempts <= MAX_RESERVATIONS
                or not isinstance(raw_results, (list, tuple))
                or len(result_items) != len(raw_results)
                or any(result is None for result in results)
                or not (full_coverage or interrupted_coverage or inflight_coverage)
                or not isinstance(target_id, str)
                or not target_id
                or not isinstance(target_fingerprint, str)
                or not target_fingerprint
                or (reason is not None and not isinstance(reason, str))
                or (updated_at is not None and not isinstance(updated_at, str))
                or (
                    inflight is not None
                    and (not isinstance(inflight, str) or not inflight)
                )
                or (status != "active" and inflight is not None)
                or status not in {"active", "ready", "completed", "refused", "aborted"}
            ):
                return malformed_entry()
            return {
                "target_id": target_id,
                "target_fingerprint": target_fingerprint,
                "attempts": attempts,
                "status": status,
                # Boolean state is enough for orphan detection; the unguessable
                # completion token and process owner never belong in /status.
                "inflight": bool(inflight),
                "results": projected_results,
                "reason": reason,
                "updated_at": updated_at,
            }

        with self._repeat_lock:
            raw_targets = payload.get("targets") or {}
            raw_targets = raw_targets if isinstance(raw_targets, Mapping) else {}
            public_targets = {
                target_id: public_entry(entry)
                for target_id, entry in raw_targets.items()
                if isinstance(target_id, str) and target_id
            }
            comparison = payload.get("comparison")
            public_comparison = None
            if isinstance(comparison, Mapping):
                comparison_set_id = comparison.get("comparison_set_id")
                fingerprint = comparison.get("fingerprint")
                if isinstance(comparison_set_id, str) and isinstance(fingerprint, str):
                    public_comparison = {
                        "comparison_set_id": comparison_set_id,
                        "fingerprint": fingerprint,
                    }
            schema_version = payload.get("schema_version")
            kind = payload.get("kind")
            durable_status = payload.get("status")
            durable_updated_at = payload.get("updated_at")
            self._durable_repeat_progress = {
                "schema_version": (
                    schema_version
                    if isinstance(schema_version, int)
                    and not isinstance(schema_version, bool)
                    else None
                ),
                "kind": kind if isinstance(kind, str) else None,
                "status": (durable_status if isinstance(durable_status, str) else None),
                "comparison": public_comparison,
                "targets": public_targets,
                "updated_at": (
                    durable_updated_at if isinstance(durable_updated_at, str) else None
                ),
            }
            raw_failures = payload.get("failures") or {}
            raw_failures = (
                raw_failures if isinstance(raw_failures, Mapping) else {}
            )
            failures = {
                target_id: public_entry(entry)
                for target_id, entry in raw_failures.items()
                if isinstance(target_id, str) and target_id
            }
            for target_id, entry in public_targets.items():
                if isinstance(entry, Mapping) and entry.get("status") in {
                    "aborted",
                    "refused",
                    "malformed",
                }:
                    failures[str(target_id)] = dict(entry)
            for target_id, failure in failures.items():
                if isinstance(failure, Mapping):
                    self._repeat_failures[str(target_id)] = dict(failure)

    def repeat_snapshot(self) -> dict[str, Any]:
        from jasper.active_speaker.commissioning_capture import (
            DEFAULT_REPEAT_TARGET,
        )

        from jasper.active_speaker.repeat_admission import (
            MAX_ATTEMPTS,
            measurement_attempts,
        )
        from jasper.active_speaker.crossover_eligibility import (
            mapping_sequence,
            nonnegative_int,
        )

        with self._repeat_lock:
            targets: dict[str, Any] = {}
            # Playback admission is the authority for attempts, including
            # captures that failed in transport before acoustic analysis.  Use
            # its ledger for user-facing counts so the UI cannot promise a
            # fifth attempt while the safety gate correctly refuses one.
            durable_targets = self._durable_repeat_progress.get("targets") or {}
            for target_id, raw in durable_targets.items():
                if not isinstance(raw, Mapping):
                    continue
                entry = dict(raw)
                results = list(mapping_sequence(entry.get("results")))
                attempts = nonnegative_int(entry.get("attempts"))
                accepted = sum(
                    1 for result in results if result.get("accepted") is True
                )
                targets[str(target_id)] = {
                    "comparison_set_id": (
                        self._durable_repeat_progress.get("comparison") or {}
                    ).get("comparison_set_id"),
                    "target_fingerprint": entry.get("target_fingerprint"),
                    "attempts": attempts,
                    "accepted": accepted,
                    "target": DEFAULT_REPEAT_TARGET,
                    # Gate on the audible MEASUREMENT budget, not the raw
                    # reservation counter: a set that spent a couple of
                    # refunded transport failures still has audio attempts left.
                    "needed_recapture": (
                        entry.get("status") == "active"
                        and measurement_attempts(results) < MAX_ATTEMPTS
                        and accepted < DEFAULT_REPEAT_TARGET
                    ),
                    "status": entry.get("status"),
                }

            return {
                "targets": targets,
                "failures": dict(self._repeat_failures),
                "durable": dict(self._durable_repeat_progress),
            }

    def level_match_snapshot(
        self, *, current_context_id: str | None = None
    ) -> dict[str, Any]:
        context_valid = (
            current_context_id is None
            or self.context_id == current_context_id
        )
        return {
            "context_id": self.context_id,
            "valid": context_valid,
            "repeats": self.repeat_snapshot(),
        }


_LEVEL_LEASE = CrossoverLevelLease()


def level_lease() -> CrossoverLevelLease:
    return _LEVEL_LEASE


class MeasurementJourneyResetRefused(RuntimeError):
    """Raised when the scoped crossover reset cannot run safely right now."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def reset_measurement_journey() -> dict[str, Any]:
    """Clear the active-crossover MEASUREMENT JOURNEY in place.

    The scoped sibling of the nuclear ``/sound/`` Advanced-setup reset
    (``jasper.active_speaker.reset.clear_active_speaker_setup_state``):
    restarts the guided capture flow — comparison set, level locks, driver
    captures, summed validation, and the compiled-but-not-loaded protected
    candidate — without losing driver research
    (``design_draft``) or disturbing whatever audio graph is currently
    applied/loaded (``baseline_profile``, ``startup_load``). See
    ``jasper.active_speaker.reset`` for the artifact-by-artifact rationale.

    Callers MUST stop any in-flight capture/level-match session before
    calling this — see ``_handle_crossover_reset`` in
    ``jasper.web.correction_setup``, which reuses the capture-cancel path
    first. This function only owns the in-process lease and the durable
    journey files; it never touches CamillaDSP.
    """

    from jasper.active_speaker.reset import (
        active_speaker_measurement_journey_paths,
        active_speaker_setup_state_paths,
        clear_active_speaker_measurement_journey,
    )

    lease = level_lease()
    lease.invalidate_comparison_context()

    reset_result = clear_active_speaker_measurement_journey()
    # Report what actually happened, not the static intent: a file that failed
    # to unlink lands in ``errors`` and flips ``status`` to ``partial`` — the
    # UI must not paint that green. ``cleared`` are the files this call removed;
    # ``missing`` were already absent (also fine); ``errors`` are the honest
    # failures. ``kept`` is the by-design KEEP set (design_draft, baseline_profile,
    # startup_load), which this call never touches.
    cleared_ids = sorted(e["id"] for e in reset_result.get("cleared", []))
    missing_ids = sorted(e["id"] for e in reset_result.get("missing", []))
    error_ids = sorted(e["id"] for e in reset_result.get("errors", []))
    kept_ids = sorted(
        set(active_speaker_setup_state_paths())
        - set(active_speaker_measurement_journey_paths())
    )
    log_event(
        logger,
        "correction.crossover_reset",
        status=reset_result.get("status"),
        cleared=cleared_ids,
        missing=missing_ids,
        errors=error_ids,
        kept=kept_ids,
    )
    return {
        **reset_result,
        "cleared_ids": cleared_ids,
        "missing_ids": missing_ids,
        "error_ids": error_ids,
        "kept_ids": kept_ids,
    }


def status_payload() -> dict[str, Any]:
    """Return active-crossover targets and saved measurement evidence."""

    payload = conductor_status()
    payload["commission"] = web_commissioning.commission_status_payload()
    targets_raw = payload.get("targets")
    targets: dict[str, Any] = targets_raw if isinstance(targets_raw, dict) else {}
    driver_count = len(targets.get("drivers") or [])
    summed_count = len(targets.get("summed") or [])
    from jasper.active_speaker.baseline_profile import (
        load_applied_baseline_profile_state,
    )
    # The envelope gates the measurement flow on the driver safety profile's
    # own confirmed-and-current verdict (evaluate_driver_safety_profile), not
    # on "protected setup" readiness alone: JTS3 hardware evidence showed an
    # operator admitted through level locks into driver sweeps while the
    # profile still self-described as incomplete, only refused by the deep
    # excitation admission after burning acceptance repeats. Load fresh (not
    # the design draft's own stale save-time evaluation) so a topology change
    # since the last save is honoured; unreadable is reported as None so the
    # envelope fails closed rather than silently treating it as authorized.
    if payload["active"]:
        from jasper.active_speaker.design_draft import load_design_draft
        from jasper.output_topology import load_output_topology

        try:
            safety_topology = load_output_topology()
            safety_draft = load_design_draft(topology=safety_topology)
            payload["driver_safety_profile_evaluation"] = safety_draft.get(
                "driver_safety_profile_evaluation"
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            payload["driver_safety_profile_evaluation"] = None
    # Level evidence is tied to the immutable profile that is actually loaded,
    # not the mutable next-design candidate. Capturing the first driver updates
    # candidate evidence and must not invalidate the safe active graph or its
    # near-field gain reference.
    setup_profile = payload["setup"].get("protected_profile")
    current_context_id = (
        str(setup_profile.get("candidate_fingerprint") or "") or None
        if isinstance(setup_profile, Mapping)
        else None
    )
    from jasper.active_speaker import repeat_admission

    comparison_set = (payload.get("measurements") or {}).get(
        "active_comparison_set"
    )
    try:
        durable_repeats = repeat_admission.snapshot(
            comparison_set if isinstance(comparison_set, Mapping) else None
        )
    except (OSError, RuntimeError, ValueError) as exc:
        durable_repeats = {
            "status": "unavailable",
            "targets": {},
            "error": str(exc),
        }
    _LEVEL_LEASE.set_durable_repeat_progress(durable_repeats)
    payload["level_match"] = _LEVEL_LEASE.level_match_snapshot(
        current_context_id=current_context_id
    )
    payload["applied_profile"] = load_applied_baseline_profile_state()
    # v2 session state (Wave 5a). Fail-soft: an unreadable v2 state must
    # never take down the whole status surface.
    try:
        from .correction_crossover_v2_status import crossover_v2_status_block

        v2_block = crossover_v2_status_block()
    except (OSError, RuntimeError, TypeError, ValueError):
        logger.warning("crossover v2 status block unavailable", exc_info=True)
        v2_block = None
    if v2_block is not None:
        payload["crossover_v2"] = v2_block
    logger.debug(
        "crossover status active=%s drivers=%d summed=%d",
        payload["active"],
        driver_count,
        summed_count,
    )
    return payload
