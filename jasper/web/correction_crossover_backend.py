# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Correction-side active-crossover measurement backend.

The correction page owns the HTTPS browser surface. Active-speaker measurement
state, capture storage, preset resolution, and acoustic analysis are owned by
``jasper.active_speaker.web_measurement`` so another operator surface does not
need to rediscover the same evidence model.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Mapping

from jasper.active_speaker import web_commissioning, web_measurement
from jasper.log_event import log_event

logger = logging.getLogger(__name__)
CamillaFactory = Callable[[], Any]


def status_payload() -> dict[str, Any]:
    """Return active-crossover targets and saved measurement evidence."""

    payload = web_measurement.status_payload()
    payload["commission"] = web_commissioning.commission_status_payload()
    # Layer-A gate: only active (`active_2_way` / `active_3_way`) speakers have
    # driver/summed targets; a `full_range_passive` speaker has none, so
    # `active=False` is the honest "this speaker has no crossover to tune" flag
    # FOR the envelope-driven page to consume when it lands (revision plan §1 —
    # today `/crossover/envelope` gates on the same derivation; the shipped
    # tab/JS do not read it yet). Derived from the already-computed targets —
    # no extra topology read. Pinned by tests/test_web_correction_crossover_flow.py.
    targets_raw = payload.get("targets")
    targets: dict[str, Any] = targets_raw if isinstance(targets_raw, dict) else {}
    driver_count = len(targets.get("drivers") or [])
    summed_count = len(targets.get("summed") or [])
    payload["active"] = bool(driver_count or summed_count)
    log_event(
        logger,
        "correction.crossover_status",
        status=payload.get("measurements", {}).get("status"),
        driver_targets=driver_count,
        summed_targets=summed_count,
        active=payload["active"],
    )
    return payload


async def start_driver_test(
    raw: dict[str, Any],
    *,
    camilla_factory: CamillaFactory,
    blocking_phase: str | None = None,
) -> dict[str, Any]:
    """Start the safe per-driver audible confirmation path."""

    payload = await web_commissioning.start_driver_test(
        raw,
        camilla_factory=camilla_factory,
        blocking_phase=blocking_phase,
    )
    log_event(
        logger,
        "correction.crossover_driver_test",
        status=payload.get("status"),
        group_id=raw.get("speaker_group_id"),
        role=raw.get("role"),
    )
    return payload


async def confirm_driver_test(
    raw: dict[str, Any],
    *,
    camilla_factory: CamillaFactory,
) -> dict[str, Any]:
    """Record the operator acknowledgement for a per-driver test."""

    payload = await web_commissioning.confirm_driver_test(
        raw,
        camilla_factory=camilla_factory,
    )
    log_event(
        logger,
        "correction.crossover_driver_confirm",
        status=payload.get("status"),
        outcome=raw.get("outcome"),
    )
    return payload


async def abort_driver_test(*, camilla_factory: CamillaFactory) -> dict[str, Any]:
    """Stop any per-driver audible test and re-mute the transient graph."""

    payload = await web_commissioning.abort_driver_test(
        camilla_factory=camilla_factory,
    )
    log_event(
        logger,
        "correction.crossover_driver_abort",
        status=payload.get("status"),
    )
    return payload


async def start_summed_test(
    raw: dict[str, Any],
    *,
    camilla_factory: CamillaFactory,
    blocking_phase: str | None = None,
) -> dict[str, Any]:
    """Run the safe combined-driver audible test."""

    payload = await web_commissioning.start_summed_test(
        raw,
        camilla_factory=camilla_factory,
        blocking_phase=blocking_phase,
    )
    log_event(
        logger,
        "correction.crossover_summed_test",
        status=payload.get("status"),
        group_id=raw.get("speaker_group_id"),
    )
    return payload


async def play_driver_capture_sweep(
    raw: dict[str, Any],
    *,
    camilla_factory: CamillaFactory,
    blocking_phase: str | None = None,
) -> dict[str, Any]:
    """Play a mic-capture sweep through an already-confirmed driver."""

    payload = await web_commissioning.play_driver_capture_sweep(
        raw,
        camilla_factory=camilla_factory,
        blocking_phase=blocking_phase,
    )
    log_event(
        logger,
        "correction.crossover_driver_capture_sweep",
        status=payload.get("status"),
        group_id=raw.get("speaker_group_id"),
        role=raw.get("role"),
    )
    return payload


async def play_summed_capture_sweep(
    raw: dict[str, Any],
    *,
    camilla_factory: CamillaFactory,
    blocking_phase: str | None = None,
) -> dict[str, Any]:
    """Play a mic-capture sweep through an already-tested summed path."""

    payload = await web_commissioning.play_summed_capture_sweep(
        raw,
        camilla_factory=camilla_factory,
        blocking_phase=blocking_phase,
    )
    log_event(
        logger,
        "correction.crossover_summed_capture_sweep",
        status=payload.get("status"),
        group_id=raw.get("speaker_group_id"),
    )
    return payload


def record_driver_capture(raw: Mapping[str, Any], wav_bytes: bytes) -> dict[str, Any]:
    """Analyze one secure browser WAV and record per-driver evidence."""

    payload = web_measurement.record_driver_capture(raw, wav_bytes)
    log_event(
        logger,
        "correction.crossover_driver_capture",
        status="recorded" if payload.get("recorded") else "not_recorded",
        group_id=raw.get("speaker_group_id"),
        role=raw.get("role"),
    )
    return payload


def record_summed_capture(raw: Mapping[str, Any], wav_bytes: bytes) -> dict[str, Any]:
    """Analyze one secure browser WAV and record summed-crossover evidence."""

    payload = web_measurement.record_summed_capture(raw, wav_bytes)
    log_event(
        logger,
        "correction.crossover_summed_capture",
        status="recorded" if payload.get("recorded") else "not_recorded",
        group_id=raw.get("speaker_group_id"),
        verdict=payload.get("verdict"),
    )
    return payload
