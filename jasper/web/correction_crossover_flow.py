# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""HTTPS active-crossover microphone measurement flow."""

from __future__ import annotations

import html
import logging
import math
import threading
from http import HTTPStatus
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urlparse

from ..log_event import log_event
from ._common import canonical_header, canonical_page
from .correction_hub import section_tabs

logger = logging.getLogger(__name__)

AsyncRunner = Callable[..., Any]
CamillaFactory = Callable[[], Any]

# Relay-capture kinds this flow bridges. Both ride the shared `crossover_sweep`
# capture spec + the single relay transport (`correction_adapter`) — the same
# seam the room and sync flows use — so a phone can carry a driver or summed
# crossover sweep in place of a same-origin `postWav`. Kind-specific behaviour is
# only which play/record pair runs; the transport is identical.
CROSSOVER_RELAY_KINDS = ("driver", "summed", "verification")
# The capture page polls host progress every 250 ms by default. Keep a stopped
# session readable for several polls before deleting its one-time relay state.
CROSSOVER_CANCEL_OBSERVATION_GRACE_S = 1.0


def render_page(hostname: str, csrf_token: str = "") -> bytes:
    header = canonical_header(
        "Correction",
        back_href=f"http://{html.escape(hostname, quote=True)}/",
    )
    body = f"""
{header}
<main class="page correction-measurement crossover-page" data-required-sr="48000">
  {section_tabs("crossover")}

  <section class="info-card info-card--accent">
    <p class="eyebrow">Speaker layer</p>
    <h2 class="section__title">Calibrate the active crossover</h2>
    <p id="crossover-verdict" class="form-hint">Checking the speaker…</p>
  </section>

  <section class="info-card" aria-label="Crossover calibration progress">
    <ol id="crossover-steps" class="wizard-steps"></ol>
    <div id="crossover-nudges" aria-live="polite"></div>
  </section>

  <section id="crossover-review" class="info-card hidden" aria-label="Measured crossover candidate">
    <p class="eyebrow">Review before apply</p>
    <h2 class="section__title">Measured crossover candidate</h2>
    <div id="crossover-review-body"></div>
  </section>

  <section class="info-card" aria-live="polite">
    <div id="crossover-action" class="measurement-row__actions"></div>
    <div id="crossover-relay" class="hidden">
      <p id="crossover-relay-status" class="form-hint"></p>
      <a id="crossover-relay-link" class="btn btn--primary hidden" href="#">Open phone capture</a>
      <button id="crossover-relay-stop" class="btn btn--danger hidden" type="button">Stop measurement</button>
    </div>
    <p id="capture-status" class="capture-status" role="status" aria-live="polite"></p>
  </section>
</main>
<script type="module" src="/assets/correction/js/crossover/main.js"></script>
"""
    return canonical_page(
        "Crossover measurement — JTS speaker",
        body,
        csrf_token=csrf_token,
        page_css_href="/assets/correction/crossover.css",
    )


def _one(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    if not values:
        return None
    value = values[-1].strip()
    return value or None


def _bool(value: str | None) -> bool | None:
    if value is None:
        return None
    return value.lower() in {"1", "true", "yes", "y", "on"}


def _float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _request_payload(handler: Any) -> dict[str, Any]:
    query = parse_qs(urlparse(handler.path).query, keep_blank_values=False)
    payload: dict[str, Any] = {}
    for key in (
        "speaker_group_id",
        "role",
        "playback_id",
        "summed_test_id",
        "calibration_id",
        "measurement_mode",
        "polarity",
        "delay_target_role",
        "notes",
    ):
        value = _one(query, key)
        if value is not None:
            payload[key] = value
    for key in ("test_level_dbfs", "crossover_fc_hz", "delay_ms"):
        number_value = _float(_one(query, key))
        if number_value is not None:
            payload[key] = number_value
    for key in ("has_mic_calibration", "expect_null"):
        flag_value = _bool(_one(query, key))
        if flag_value is not None:
            payload[key] = flag_value
    return payload


def handle_status(
    *, relay: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], HTTPStatus]:
    from . import correction_crossover_backend as backend

    payload = backend.status_payload()
    payload["relay"] = dict(relay) if relay else None
    return payload, HTTPStatus.OK


def handle_envelope(
    *, relay: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], HTTPStatus]:
    """GET /crossover/envelope: the server-computed commissioning screen envelope
    the dumb frontend renders each step from (revision plan §3.2), aligned with
    the room flow's envelope-driven pattern. Additive alongside /crossover/status;
    passive speakers get ``active=False`` (Layer A hidden)."""
    from jasper.active_speaker.crossover_envelope import (
        build_crossover_envelope_logged,
    )

    status, _ = handle_status(relay=relay)
    return build_crossover_envelope_logged(status), HTTPStatus.OK


def handle_apply(
    raw: Mapping[str, Any],
    run_async: AsyncRunner,
    camilla_factory: CamillaFactory,
    *,
    blocking_phase: str | None = None,
) -> tuple[dict[str, Any], HTTPStatus]:
    """Compile and atomically apply the explicitly selected profile owner."""
    from . import correction_crossover_backend as backend

    tuning_owner = str(raw.get("tuning_owner") or "")
    expected_candidate_fingerprint = str(
        raw.get("expected_candidate_fingerprint") or ""
    )
    if tuning_owner not in {"manual", "automatic"}:
        return {
            "status": "refused",
            "error": "Choose manual or automatic crossover tuning before applying.",
        }, HTTPStatus.BAD_REQUEST
    if blocking_phase is not None:
        return {
            "status": "refused",
            "reason": "measurement_in_progress",
            "blocking_phase": blocking_phase,
            "next_step": "Finish the active measurement before applying the crossover.",
        }, HTTPStatus.CONFLICT
    payload = run_async(
        backend.apply_profile(
            tuning_owner=tuning_owner,
            expected_candidate_fingerprint=expected_candidate_fingerprint,
            camilla_factory=camilla_factory,
        ),
        timeout=30.0,
    )
    if payload.get("status") not in {"applied", "applied_unverified"}:
        return payload, HTTPStatus.CONFLICT

    # This explicit user action is the terminal owner of commissioning.  The
    # generic backend is also used mid-flow to preserve a legacy manual graph,
    # so ending the durable safe-playback session there would release mutual
    # exclusion before the automatic measurements begin.
    from jasper.active_speaker.safe_playback import stop_safe_playback_session

    try:
        closed = stop_safe_playback_session(reason="crossover_profile_applied")
    except Exception as exc:  # noqa: BLE001 — applied DSP remains truthful
        issue = {
            "severity": "blocker",
            "code": "crossover_commissioning_close_failed",
            "message": (
                "The crossover was applied, but its measurement session could "
                "not be closed. Retry before starting room correction."
            ),
        }
        payload = dict(payload)
        payload["issues"] = [issue, *(payload.get("issues") or [])]
        payload["commissioning_session"] = {
            "status": "close_failed",
            "last_action": "crossover_profile_applied",
        }
        log_event(
            logger,
            "correction.crossover_commissioning_session",
            level=logging.ERROR,
            exc_info=True,
            status="close_failed",
            owner=tuning_owner,
            reason=type(exc).__name__,
        )
        return payload, HTTPStatus.CONFLICT

    payload = dict(payload)
    payload["commissioning_session"] = {
        "status": str(closed.get("status") or "unknown"),
        "session_id": closed.get("session_id"),
        "last_action": closed.get("last_action"),
    }
    log_event(
        logger,
        "correction.crossover_commissioning_session",
        status=payload["commissioning_session"]["status"],
        owner=tuning_owner,
        session_id=payload["commissioning_session"]["session_id"],
    )
    return payload, HTTPStatus.OK


def handle_restore(
    run_async: AsyncRunner,
    camilla_factory: CamillaFactory,
    *,
    blocking_phase: str | None = None,
) -> tuple[dict[str, Any], HTTPStatus]:
    """Restore one crash-interrupted strict candidate apply."""

    from . import correction_crossover_backend as backend

    if blocking_phase is not None:
        return {
            "status": "refused",
            "reason": "measurement_in_progress",
            "blocking_phase": blocking_phase,
        }, HTTPStatus.CONFLICT
    payload = run_async(
        backend.restore_commissioning_candidate(camilla_factory=camilla_factory),
        timeout=30.0,
    )
    return payload, (
        HTTPStatus.OK
        if payload.get("status") == "rolled_back"
        else HTTPStatus.CONFLICT
    )


def handle_driver_test(
    raw: dict[str, Any],
    run_async: AsyncRunner,
    camilla_factory: CamillaFactory,
    *,
    blocking_phase: str | None = None,
) -> tuple[dict[str, Any], HTTPStatus]:
    from . import correction_crossover_backend as backend

    payload = run_async(
        backend.start_driver_test(
            raw,
            camilla_factory=camilla_factory,
            blocking_phase=blocking_phase,
        ),
        timeout=45.0,
    )
    return payload, (
        HTTPStatus.CONFLICT if payload.get("status") == "refused" else HTTPStatus.OK
    )


def handle_driver_confirm(
    raw: dict[str, Any],
    run_async: AsyncRunner,
    camilla_factory: CamillaFactory,
) -> tuple[dict[str, Any], HTTPStatus]:
    from . import correction_crossover_backend as backend

    payload = run_async(
        backend.confirm_driver_test(raw, camilla_factory=camilla_factory),
        timeout=20.0,
    )
    return payload, HTTPStatus.OK


def handle_driver_abort(
    run_async: AsyncRunner,
    camilla_factory: CamillaFactory,
) -> tuple[dict[str, Any], HTTPStatus]:
    from . import correction_crossover_backend as backend

    payload = run_async(
        backend.abort_driver_test(camilla_factory=camilla_factory),
        timeout=20.0,
    )
    return payload, HTTPStatus.OK


def handle_summed_test(
    raw: dict[str, Any],
    run_async: AsyncRunner,
    camilla_factory: CamillaFactory,
    *,
    blocking_phase: str | None = None,
) -> tuple[dict[str, Any], HTTPStatus]:
    from . import correction_crossover_backend as backend

    payload = run_async(
        backend.start_summed_test(
            raw,
            camilla_factory=camilla_factory,
            blocking_phase=blocking_phase,
        ),
        timeout=45.0,
    )
    return payload, (
        HTTPStatus.CONFLICT if payload.get("status") == "refused" else HTTPStatus.OK
    )


def handle_driver_capture_sweep(
    raw: dict[str, Any],
    run_async: AsyncRunner,
    camilla_factory: CamillaFactory,
    *,
    blocking_phase: str | None = None,
) -> tuple[dict[str, Any], HTTPStatus]:
    from . import correction_crossover_backend as backend

    payload = run_async(
        backend.play_driver_capture_sweep(
            raw,
            camilla_factory=camilla_factory,
            blocking_phase=blocking_phase,
        ),
        timeout=30.0,
    )
    return payload, (
        HTTPStatus.CONFLICT if payload.get("status") == "refused" else HTTPStatus.OK
    )


def handle_summed_capture_sweep(
    raw: dict[str, Any],
    run_async: AsyncRunner,
    camilla_factory: CamillaFactory,
    *,
    blocking_phase: str | None = None,
) -> tuple[dict[str, Any], HTTPStatus]:
    from . import correction_crossover_backend as backend

    payload = run_async(
        backend.play_summed_capture_sweep(
            raw,
            camilla_factory=camilla_factory,
            blocking_phase=blocking_phase,
        ),
        timeout=30.0,
    )
    return payload, (
        HTTPStatus.CONFLICT if payload.get("status") == "refused" else HTTPStatus.OK
    )


def handle_summed_capture(
    handler: Any,
    wav_body: bytes,
) -> tuple[dict[str, Any], HTTPStatus]:
    from . import correction_crossover_backend as backend

    payload = backend.record_summed_capture(_request_payload(handler), wav_body)
    return payload, HTTPStatus.OK


# ----------------------------------------------------------------------
# Phone-mic relay transport (P7). Same one transport + one upload seam the
# room and sync flows ride: the phone runs the trusted capture page and the Pi
# pulls the E2E-verified WAV back through the stateless relay, feeding the SAME
# `record_driver_capture` / `record_summed_capture` analysis a same-origin
# `postWav` reaches. GATED + default-off (mirrors /relay/capture): with no
# `JASPER_CAPTURE_RELAY_BASE` the flow keeps the same-origin `postWav` path
# byte-identical and the relay endpoint returns a clear "not configured" error.
# On-device: the acoustic capture is not exercised hardware-free — its H2
# sanity-check line rides the P7 checklist, exactly like the room/sync relay.
# ----------------------------------------------------------------------


def relay_kind_from_raw(raw: dict[str, Any]) -> str:
    """The crossover relay capture kind, validated at the thin HTTP boundary.

    Kept small + strict at the boundary (extensibility doctrine): an unknown
    kind fails loud rather than silently defaulting to a known path."""
    kind = str((raw or {}).get("kind") or "").strip().lower()
    if kind not in CROSSOVER_RELAY_KINDS:
        raise ValueError(
            f"crossover relay kind must be one of {CROSSOVER_RELAY_KINDS}, "
            f"got {kind!r}"
        )
    return kind


def relay_driver_label(raw: dict[str, Any]) -> str:
    """Server-driven capture-page copy naming the driver/speaker under test.

    The `crossover_sweep` spec's screen copy comes from the Pi (no web deploy to
    relabel a driver), so this composes the label the phone shows from the
    request's role/group — mirroring the same-origin ``roleLabel`` in the JS."""
    role = str((raw or {}).get("role") or "").strip()
    if role:
        return f"{role[:1].upper()}{role[1:]} driver"
    return "summed crossover"


def playback_issue_text(payload: Any, fallback: str) -> str:
    """The one operator-facing reason a capture-sweep payload failed.

    Mirrors the same-origin JS ``issueMessage`` (crossover/main.js) — the
    canonical reader of these payloads: first ``issues[].message/label/code``,
    then the nested ``playback.issues``, then ``next_step``, then ``reason``,
    else the fallback. So a refusal ("Finish the other measurement…") or a
    rollback failure surfaces its real reason instead of a generic "did not
    play"."""
    if not isinstance(payload, dict):
        return fallback
    for source in (payload, payload.get("playback")):
        if not isinstance(source, dict):
            continue
        for issue in source.get("issues") or []:
            if not isinstance(issue, dict):
                continue
            text = issue.get("message") or issue.get("label") or issue.get("code")
            if text:
                return str(text)
    for key in ("next_step", "reason"):
        value = payload.get(key)
        if value:
            return str(value)
    return fallback


def capture_sweep_played(payload: Any) -> bool:
    """Whether a driver/summed capture-sweep payload reports real audio.

    The REAL play payloads nest ``audio_emitted`` under ``playback`` (top level
    is ``{status, playback, playback_id, [summed_test_id], test_level_dbfs,
    sweep_meta, commission}``); only refused/blocked payloads carry a top-level
    ``audio_emitted: False``. This mirrors the same-origin JS's canonical read
    (``assertCapturePlayback``: ``payload.status === 'completed' &&
    playback.audio_emitted``) so the relay and browser paths agree on what
    "the sweep played" means. Pinned against the REAL play function's return in
    tests/test_web_correction_crossover_flow.py.
    """
    if not isinstance(payload, dict):
        return False
    playback = payload.get("playback")
    playback = playback if isinstance(playback, dict) else {}
    return payload.get("status") == "completed" and bool(
        playback.get("audio_emitted")
    )


def ensure_automatic_measurement_profile(
    status: dict[str, Any],
    run_async: AsyncRunner,
    camilla_factory: CamillaFactory,
    *,
    status_loader: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Migrate a safe legacy manual graph before automatic measurement.

    A pre-snapshot manual profile is a valid playback anchor, but it cannot be
    the source of truth for per-role automatic excitation. ``Tune
    automatically`` is one sequential intent, so this boundary performs the
    existing manual-preservation transaction when—and only when—the setup
    contract proves the current source is an exact match. Unsafe preservation
    fails before relay registration or audio.
    """
    from . import correction_crossover_backend as backend

    setup = status.get("setup")
    setup = setup if isinstance(setup, dict) else {}
    applied = setup.get("applied_crossover")
    applied = applied if isinstance(applied, dict) else {}
    if applied.get("valid") is True:
        return status

    reason = str(applied.get("reason") or "")
    preservation = setup.get("manual_preservation")
    preservation = preservation if isinstance(preservation, dict) else {}
    baseline_profile = setup.get("baseline_profile")
    baseline_profile = baseline_profile if isinstance(baseline_profile, dict) else {}
    if reason == "active_applied_profile_snapshot_missing":
        if preservation.get("ready") is not True:
            detail = str(
                preservation.get("detail")
                or applied.get("detail")
                or "the current manual crossover cannot be preserved safely"
            )
            log_event(
                logger,
                "correction.crossover_legacy_profile_autopreserve",
                status="refused",
                reason=preservation.get("reason") or reason,
            )
            raise ValueError(detail)
        payload = run_async(
            backend.apply_profile(
                tuning_owner="manual",
                expected_candidate_fingerprint=str(
                    baseline_profile.get("candidate_fingerprint") or ""
                ),
                camilla_factory=camilla_factory,
            ),
            timeout=30.0,
        )
        if payload.get("status") != "applied":
            detail = playback_issue_text(
                payload,
                "the current manual crossover could not be preserved safely",
            )
            log_event(
                logger,
                "correction.crossover_legacy_profile_autopreserve",
                status="failed",
                reason=detail,
            )
            raise ValueError(detail)
        loader = status_loader or backend.status_payload
        status = loader()
        setup = status.get("setup")
        setup = setup if isinstance(setup, dict) else {}
        applied = setup.get("applied_crossover")
        applied = applied if isinstance(applied, dict) else {}
        log_event(
            logger,
            "correction.crossover_legacy_profile_autopreserve",
            status="applied" if applied.get("valid") is True else "failed",
            owner=applied.get("owner"),
        )

    if setup.get("status") != "ready":
        raise ValueError(
            str(
                setup.get("detail")
                or "the protected crossover setup is no longer ready"
            )
        )
    if applied.get("valid") is not True:
        raise ValueError(
            str(
                applied.get("detail")
                or "apply a protected crossover profile before measuring it"
            )
        )
    return status


def validate_current_capture_context(
    status: Mapping[str, Any],
    *,
    current_topology_id: str,
    expected_topology_id: str,
    expected_profile_context_id: str,
    expected_comparison_set: Mapping[str, Any],
    kind: str,
    speaker_group_id: str,
    role: str = "",
    capture_geometry: str,
    expected_target_fingerprint: str,
) -> None:
    """Reject a stale relay link before it can emit a crossover sweep.

    The POST-time checks make the link understandable; this arm-time check is
    the safety boundary.  Every mutable identity that defines comparable
    acoustic evidence is read again immediately before playback.
    """
    from jasper.active_speaker.capture_geometry import comparison_set_valid

    if current_topology_id != expected_topology_id:
        raise ValueError(
            "the speaker topology changed after this link was created; "
            "run the crossover level check again"
        )
    setup = status.get("setup")
    setup = setup if isinstance(setup, Mapping) else {}
    applied = setup.get("applied_crossover")
    applied = applied if isinstance(applied, Mapping) else {}
    protected = setup.get("protected_profile")
    protected = protected if isinstance(protected, Mapping) else {}
    if (
        setup.get("status") != "ready"
        or applied.get("valid") is not True
        or str(protected.get("candidate_fingerprint") or "")
        != expected_profile_context_id
    ):
        raise ValueError(
            "the protected crossover setup changed after this link was created; "
            "run the crossover level check again"
        )
    level = status.get("level_match")
    level = level if isinstance(level, Mapping) else {}
    geometry = str(capture_geometry or "").lower()
    reference_locks = level.get("reference_axis_driver_locks")
    reference_locks = (
        reference_locks if isinstance(reference_locks, Mapping) else {}
    )
    reference_lock = reference_locks.get(f"{speaker_group_id}:{role.lower()}")
    geometry_level_ready = level.get("ready") is True
    if kind == "driver" and geometry == "reference_axis":
        geometry_level_ready = bool(
            not isinstance(reference_lock, bool)
            and isinstance(reference_lock, (int, float))
            and math.isfinite(float(reference_lock))
            and float(reference_lock) <= 0
        )
    if (
        level.get("valid") is not True
        or not geometry_level_ready
        or str(level.get("context_id") or "") != expected_profile_context_id
    ):
        raise ValueError(
            "the crossover measurement level changed after this link was created; "
            "run the level check again"
        )
    measurements = status.get("measurements")
    measurements = measurements if isinstance(measurements, Mapping) else {}
    current_set = measurements.get("active_comparison_set")
    expected_set = dict(expected_comparison_set)
    if (
        not isinstance(current_set, Mapping)
        or not comparison_set_valid(current_set)
        or str(current_set.get("topology_id") or "") != expected_topology_id
        or str(current_set.get("profile_context_id") or "")
        != expected_profile_context_id
        or current_set.get("comparison_set_id")
        != expected_set.get("comparison_set_id")
        or current_set.get("fingerprint") != expected_set.get("fingerprint")
    ):
        raise ValueError(
            "the crossover comparison set changed after this link was created; "
            "run the level check again"
        )
    targets = status.get("targets")
    targets = targets if isinstance(targets, Mapping) else {}
    rows = targets.get("drivers" if kind == "driver" else "summed")
    rows = rows if isinstance(rows, list) else []
    target = next(
        (
            item
            for item in rows
            if isinstance(item, Mapping)
            and str(item.get("speaker_group_id") or "") == speaker_group_id
            and (
                kind != "driver"
                or str(item.get("role") or "").lower() == role.lower()
            )
        ),
        None,
    )
    fingerprint_key = "target_fingerprint" if kind == "driver" else "group_fingerprint"
    if (
        not isinstance(target, Mapping)
        or str(target.get(fingerprint_key) or "") != expected_target_fingerprint
    ):
        raise ValueError(
            "the crossover measurement target changed after this link was created; "
            "create a new capture link"
        )


def validate_current_level_target_context(
    status: Mapping[str, Any],
    *,
    current_topology_id: str,
    expected_topology_id: str,
    expected_profile_context_id: str,
    speaker_group_id: str,
    role: str,
    expected_target_fingerprint: str,
) -> None:
    """Reject a stale driver-level link before its isolated tone can load."""

    if current_topology_id != expected_topology_id:
        raise ValueError(
            "the speaker topology changed after this link was created; "
            "start driver level matching again"
        )
    setup = status.get("setup")
    setup = setup if isinstance(setup, Mapping) else {}
    protected = setup.get("protected_profile")
    protected = protected if isinstance(protected, Mapping) else {}
    applied = setup.get("applied_crossover")
    applied = applied if isinstance(applied, Mapping) else {}
    if (
        setup.get("status") != "ready"
        or applied.get("valid") is not True
        or str(protected.get("candidate_fingerprint") or "")
        != expected_profile_context_id
    ):
        raise ValueError(
            "the protected crossover setup changed after this link was created; "
            "start driver level matching again"
        )
    targets = status.get("targets")
    targets = targets if isinstance(targets, Mapping) else {}
    rows = targets.get("drivers")
    rows = rows if isinstance(rows, list) else []
    target = next(
        (
            item
            for item in rows
            if isinstance(item, Mapping)
            and str(item.get("speaker_group_id") or "") == speaker_group_id
            and str(item.get("role") or "").lower() == role.lower()
        ),
        None,
    )
    if (
        not expected_target_fingerprint
        or not isinstance(target, Mapping)
        or str(target.get("target_fingerprint") or "")
        != expected_target_fingerprint
    ):
        raise ValueError(
            "the driver level target changed after this link was created; "
            "create a new level-check link"
        )


async def run_crossover_relay_transport(
    client: Any,
    pi_session: Any,
    *,
    run_async: AsyncRunner,
    play_sequence: Callable[[Callable[[], None]], Any],
    validate_playback: Callable[[Any], None],
    prepare_armed: Callable[[Any, Any], None],
    validate_capture: Callable[[Any], None] | None = None,
    post_host_event: Callable[[str, str, dict[str, Any]], Any] | None = None,
    begin_finishing: Callable[[], bool] | None = None,
    begin_commit: Callable[[], bool] | None = None,
    on_failure: Callable[[BaseException], None] | None = None,
    ambient_duration_s: float = 0.0,
    stop_event: threading.Event | None = None,
) -> tuple[Any, Any]:
    """Capture one relay WAV around one bounded host-owned play sequence.

    This is the single crossover relay transport for both the existing isolated
    driver path and the strict summed-region host.  It owns phone liveness,
    progress events, Stop/drain/purge, and the transition from recorder close to
    evidence commit.  The injected play sequence owns all DSP and safety policy.
    """

    import asyncio

    from jasper.capture_relay.session import (
        CaptureActivityProbe,
        CaptureStopped,
        purge,
        run_capture,
        validate_capture_acknowledgement,
    )
    from jasper.active_speaker.test_signal_plan import (
        CROSSOVER_CAPTURE_HARD_TIMEOUT_S,
        CROSSOVER_CAPTURE_PLAY_DEADLINE_S,
    )

    stop_event = stop_event or threading.Event()
    ambient_duration_s = max(0.0, float(ambient_duration_s))

    def raise_if_stopped() -> None:
        if stop_event.is_set():
            raise CaptureStopped("capture stopped")

    def post_phase(phase: str, **extra: Any) -> None:
        if post_host_event is not None:
            post_host_event(
                pi_session.session_id,
                pi_session.pull_token,
                {"phase": phase, **extra},
            )

    def post_terminal_best_effort(phase: str, **extra: Any) -> None:
        if post_host_event is None:
            return
        try:
            post_phase(phase, **extra)
        except (RuntimeError, OSError, ValueError):
            logger.warning(
                "crossover relay terminal host-event post failed",
                exc_info=True,
            )

    async def publish_cancelled_then_purge() -> None:
        await asyncio.to_thread(post_terminal_best_effort, "sweep_cancelled")
        if post_host_event is not None:
            await asyncio.sleep(CROSSOVER_CANCEL_OBSERVATION_GRACE_S)
        await asyncio.to_thread(purge, client, pi_session)

    async def play_while_phone_active(
        activity: Any,
        on_sweep_ready: Callable[[], None],
    ) -> Any:
        async def watch_phone() -> None:
            while True:
                raise_if_stopped()
                await asyncio.to_thread(activity.assert_active)
                raise_if_stopped()
                await asyncio.sleep(0.2)

        play_task = asyncio.create_task(play_sequence(on_sweep_ready))
        watch_task = asyncio.create_task(watch_phone())
        try:
            done, _pending = await asyncio.wait(
                {play_task, watch_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if watch_task in done:
                error = watch_task.exception()
                if error is None:
                    raise RuntimeError(
                        "phone capture activity watchdog stopped unexpectedly"
                    )
                raise error
            return await play_task
        finally:
            for task in (play_task, watch_task):
                if not task.done():
                    task.cancel()
            settled: list[Any] = list(
                await asyncio.gather(
                    play_task, watch_task, return_exceptions=True
                )
            )
            play_result = settled[0]
            if isinstance(play_result, BaseException) and not isinstance(
                play_result, asyncio.CancelledError
            ):
                raise play_result

    played: dict[str, Any] = {}

    def on_armed(state: Any) -> None:
        try:
            raise_if_stopped()
            acknowledgement = validate_capture_acknowledgement(
                state, pi_session.spec
            )
            required = pi_session.spec.acknowledgement
            if acknowledgement is None or required is None:
                raise ValueError(
                    "confirm the microphone placement before starting the sweep"
                )
            prepare_armed(state, acknowledgement)
            if ambient_duration_s > 0.0:
                post_phase("ambient_started", duration_s=ambient_duration_s)
            activity = CaptureActivityProbe(client, pi_session)

            def confirm_active_and_post_started() -> None:
                raise_if_stopped()
                activity.assert_active()
                raise_if_stopped()
                post_phase("sweep_started")

            payload = run_async(
                asyncio.wait_for(
                    play_while_phone_active(
                        activity,
                        confirm_active_and_post_started,
                    ),
                    timeout=CROSSOVER_CAPTURE_PLAY_DEADLINE_S,
                ),
                timeout=CROSSOVER_CAPTURE_HARD_TIMEOUT_S - 2.0,
            )
            validate_playback(payload)
            played["payload"] = payload
            if begin_finishing is not None and not begin_finishing():
                raise CaptureStopped("capture stopped")
            post_phase("sweep_complete")
        except (RuntimeError, OSError, ValueError) as exc:
            if not isinstance(exc, CaptureStopped):
                post_terminal_best_effort("sweep_failed", error=str(exc))
            raise

    capture_task = asyncio.create_task(
        asyncio.to_thread(
            run_capture,
            client,
            pi_session,
            on_armed=on_armed,
            stop_requested=stop_event.is_set,
        )
    )
    capture_received = False
    try:
        try:
            result = await asyncio.shield(capture_task)
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
            await publish_cancelled_then_purge()
            raise
        capture_received = True
        raise_if_stopped()
        if validate_capture is not None:
            validate_capture(result)
        raise_if_stopped()
        if begin_commit is not None and not begin_commit():
            raise CaptureStopped("capture stopped")
        await asyncio.to_thread(purge, client, pi_session)
        return result, played.get("payload")
    except (RuntimeError, OSError, ValueError) as exc:
        if on_failure is not None:
            on_failure(exc)
        if isinstance(exc, CaptureStopped):
            await publish_cancelled_then_purge()
        elif capture_received:
            await asyncio.to_thread(purge, client, pi_session)
        raise


def build_crossover_relay_run_and_consume(
    raw: dict[str, Any],
    run_async: AsyncRunner,
    camilla_factory: CamillaFactory,
    *,
    post_host_event: Callable[[str, str, dict[str, Any]], Any] | None = None,
    blocking_phase: Callable[[], str | None] | None = None,
    validate_capture: Callable[[Any], None] | None = None,
    prepare_play: Callable[[], Any] | None = None,
    restore_play: Callable[[], Any] | None = None,
    driver_locked_main_volume_db: Callable[[], float | None] | None = None,
    comparison_set: Mapping[str, Any] | None = None,
    applied_profile: Mapping[str, Any] | None = None,
    target_fingerprint: str = "",
    current_comparison_set: Callable[[], Mapping[str, Any]] | None = None,
    validate_current_context: Callable[[], None] | None = None,
    reserve_repeat_attempt: Callable[[], Mapping[str, Any]] | None = None,
    finish_failed_repeat_attempt: Callable[[Mapping[str, Any], str], None] | None = None,
    begin_finishing: Callable[[], bool] | None = None,
    begin_commit: Callable[[], bool] | None = None,
    ambient_duration_s: float | None = None,
    stop_event: threading.Event | None = None,
    stop_lock: Any = None,
) -> Callable[[Any, Any], Any]:
    """Return the relay ``run_and_consume(client, pi_session)`` coroutine.

    On ``armed`` (phone recording), plays the driver/summed **capture sweep**
    through the existing safe commissioning playback path — the SAME
    ``play_driver_capture_sweep`` / ``play_summed_capture_sweep`` a same-origin
    capture triggers, so the loud-output safety + fan-in gating still apply — and
    publishes ``sweep_complete`` so the phone stops recording (the room-flow
    contract). After the verified WAV arrives, feeds it (with the played
    ``sweep_meta`` / ``playback_id`` / ``test_level_dbfs``) into the SAME
    ``record_driver_capture`` / ``record_summed_capture`` analysis. The deconv
    reference is regenerated from the played ``sweep_meta`` (not the phone WAV),
    so the phone is a pure recorder.

    ``blocking_phase`` is the SERVER-computed measurement mutual-exclusion probe
    (`correction_setup._crossover_blocking_phase`), evaluated **fresh at armed
    time** — the phone can arm up to ~2 minutes after the POST, exactly when a
    room/balance/sync measurement may have started; a stale (or, worse,
    client-supplied) value would let this sweep play over another measurement
    and silently corrupt both captures. Never taken from the request body.
    """
    from . import correction_crossover_backend as backend

    stop_event = stop_event or threading.Event()
    stop_lock = stop_lock or threading.Lock()
    kind = relay_kind_from_raw(raw)
    group_id = str((raw or {}).get("speaker_group_id") or "").strip()
    role = str((raw or {}).get("role") or "").strip().lower()
    # Stash the play response between on_armed (poll thread) and the post-capture
    # record so the record carries the exact sweep the Pi played.
    played: dict[str, Any] = {}
    repeat_reservation: dict[str, Any] = {}
    placement_proof: dict[str, Any] = {}
    frozen_capture_preset: Any = None
    if isinstance(applied_profile, Mapping):
        snapshot = applied_profile.get("recomposition_snapshot")
        snapshot = snapshot if isinstance(snapshot, Mapping) else {}
        preset_raw = snapshot.get("preset")
        if isinstance(preset_raw, dict):
            from jasper.active_speaker.profile import ActiveSpeakerPreset

            frozen_capture_preset = ActiveSpeakerPreset.from_mapping(preset_raw)

    def _finish_reservation_failure(error: BaseException) -> None:
        if not repeat_reservation or finish_failed_repeat_attempt is None:
            return
        reservation = dict(repeat_reservation)
        repeat_reservation.clear()
        failure_type = type(error).__name__
        finish_failed_repeat_attempt(reservation, failure_type)

    if ambient_duration_s is None:
        from jasper.active_speaker.test_signal_plan import (
            CROSSOVER_AMBIENT_DURATION_S,
        )

        ambient_duration_s = CROSSOVER_AMBIENT_DURATION_S
    ambient_duration_s = max(0.0, float(ambient_duration_s))

    async def _play(
        on_sweep_ready: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        from jasper.active_speaker.web_commissioning import FaninGateContext
        from jasper.correction import coordinator
        import asyncio

        # This sweep runs inside the measurement window's own held gate
        # (owner=coordinator.MEASUREMENT_GATE_OWNER). The tone/sweep must
        # select under that SAME owner rather than claiming its own
        # standalone gate — a second owner is refused outright — and must
        # restore the window's label (not release) when it ends. See
        # FaninGateContext.
        fanin_gate_context = FaninGateContext(
            owner=coordinator.MEASUREMENT_GATE_OWNER,
            restore_label=coordinator.MEASUREMENT_FANIN_LABEL,
        )

        # Re-evaluate the mutual-exclusion probe NOW (at play time, not POST
        # time) — the play functions refuse with reason=measurement_in_progress
        # when another measurement holds the speaker.
        async with coordinator.measurement_window():
            try:
                if prepare_play is not None:
                    prepared = await prepare_play()
                    if prepared is False:
                        raise RuntimeError(
                            "could not reassert the locked crossover measurement level"
                        )
                phase = blocking_phase() if blocking_phase is not None else None
                # Household audio stays paused for the whole controlled quiet
                # interval. The probe has locked a safe volume but contributes
                # no SNR verdict; only signal-bounded quiet evidence vs the
                # following deconvolved sweep does.
                await asyncio.sleep(ambient_duration_s)
                if on_sweep_ready is not None:
                    await asyncio.to_thread(on_sweep_ready)
                if kind == "driver":
                    locked_main_volume_db = (
                        driver_locked_main_volume_db()
                        if driver_locked_main_volume_db is not None
                        else None
                    )
                    if (
                        driver_locked_main_volume_db is not None
                        and locked_main_volume_db is None
                    ):
                        raise RuntimeError(
                            "the geometry-scoped driver level lock is unavailable"
                        )
                    return await backend.play_driver_capture_sweep(
                        {"speaker_group_id": group_id, "role": role},
                        camilla_factory=camilla_factory,
                        blocking_phase=phase,
                        applied_profile=(
                            dict(applied_profile)
                            if isinstance(applied_profile, Mapping)
                            else None
                        ),
                        locked_main_volume_db=locked_main_volume_db,
                        volume_lease_prepared=prepare_play is not None,
                        fanin_gate_context=fanin_gate_context,
                    )
                summed_play_raw = {"speaker_group_id": group_id}
                for key in (
                    "expect_null",
                    "crossover_fc_hz",
                    "polarity",
                    "delay_ms",
                    "delay_target_role",
                ):
                    if key in raw:
                        summed_play_raw[key] = raw[key]
                return await backend.play_summed_capture_sweep(
                    summed_play_raw,
                    camilla_factory=camilla_factory,
                    blocking_phase=phase,
                    volume_lease_prepared=prepare_play is not None,
                )
            finally:
                # Restore before measurement_window resumes household audio.
                if restore_play is not None:
                    # Cancellation must not let measurement_window resume
                    # household audio before the volume/graph rollback ends.
                    # Shield keeps the cleanup task alive; the loop absorbs
                    # cancellation until cleanup is actually complete, then
                    # re-raises it to the caller.
                    cleanup = asyncio.create_task(restore_play())
                    cancelled = False
                    while not cleanup.done():
                        try:
                            await asyncio.shield(cleanup)
                        except asyncio.CancelledError:
                            cancelled = True
                    restored = cleanup.result()
                    if restored is False:
                        raise RuntimeError(
                            "the crossover measurement volume was not restored"
                        )
                    if cancelled:
                        raise asyncio.CancelledError

    async def _run_and_consume(client: Any, pi_session: Any) -> None:
        from jasper.capture_relay.session import CaptureStopped
        from jasper.active_speaker.capture_geometry import (
            normalized_placement_proof,
        )

        def _prepare_armed(state: Any, _acknowledgement: Any) -> None:
            if validate_current_context is not None:
                validate_current_context()
            elif current_comparison_set is not None:
                current = current_comparison_set()
                expected = comparison_set or {}
                if (
                    current.get("comparison_set_id")
                    != expected.get("comparison_set_id")
                    or current.get("fingerprint") != expected.get("fingerprint")
                ):
                    raise ValueError(
                        "the crossover measurement level changed after this "
                        "link was created; run the level check again"
                    )
            required = pi_session.spec.acknowledgement
            assert required is not None
            placement_proof.clear()
            placement_proof.update(
                normalized_placement_proof(
                    policy_id=required.id,
                    acknowledgement_binding=required.binding_id,
                    relay_session_id=pi_session.session_id,
                    capture_page=state.capture_page,
                    speaker_group_id=group_id,
                    role=role if kind == "driver" else "summed",
                    target_fingerprint=target_fingerprint,
                    comparison_set=comparison_set or {},
                )
            )
            if kind == "driver" and reserve_repeat_attempt is not None:
                repeat_reservation.clear()
                try:
                    with stop_lock:
                        if stop_event.is_set():
                            raise CaptureStopped("capture stopped")
                        repeat_reservation.update(reserve_repeat_attempt())
                except (OSError, RuntimeError, ValueError) as exc:
                    log_event(
                        logger,
                        "correction.crossover_repeat_persistence_failed",
                        level=logging.ERROR,
                        reason=type(exc).__name__,
                        op="reserve",
                    )
                    raise

        def _validate_playback(payload: Any) -> None:
            if not capture_sweep_played(payload):
                raise ValueError(
                    playback_issue_text(
                        payload,
                        "the crossover capture sweep did not play — "
                        "confirm the driver first",
                    )
                )

        def _record_failure(error: BaseException) -> None:
            try:
                _finish_reservation_failure(error)
            except (RuntimeError, OSError, ValueError) as persist_exc:
                log_event(
                    logger,
                    "correction.crossover_repeat_persistence_failed",
                    level=logging.ERROR,
                    exc_info=True,
                    reason=type(persist_exc).__name__,
                )

        result, play_payload = await run_crossover_relay_transport(
            client,
            pi_session,
            run_async=run_async,
            play_sequence=_play,
            validate_playback=_validate_playback,
            prepare_armed=_prepare_armed,
            validate_capture=validate_capture,
            post_host_event=post_host_event,
            begin_finishing=begin_finishing,
            begin_commit=begin_commit,
            on_failure=_record_failure,
            ambient_duration_s=ambient_duration_s,
            stop_event=stop_event,
        )
        played.clear()
        if isinstance(play_payload, Mapping):
            played.update(play_payload)
        if repeat_reservation:
            played["repeat_reservation"] = dict(repeat_reservation)

        # Calibration identity and measurement mode are injected by the host
        # from the level-check setup. ``validate_capture`` verifies that the
        # realized capture device is still that same microphone before any
        # acoustic evidence is recorded.
        # All of these ride the play payload's TOP level (the wrapper hoists
        # them out of the nested `playback`): sweep_meta, playback_id,
        # test_level_dbfs, summed_test_id — the same fields the same-origin JS
        # reads off its POST response (`playbackPayload.test_level_dbfs`, …).
        record_raw: dict[str, Any] = {
            "speaker_group_id": group_id,
            "sweep_meta": played.get("sweep_meta"),
            "playback_id": played.get("playback_id"),
            "excitation": played.get("excitation"),
            "ambient_duration_s": ambient_duration_s,
            "repeat_reservation": played.get("repeat_reservation"),
        }
        noise_floor = getattr(result, "noise_floor", None)
        if isinstance(noise_floor, Mapping):
            noise_floor_dbfs = noise_floor.get("rms_dbfs")
            if isinstance(noise_floor_dbfs, (int, float)):
                record_raw["noise_floor_dbfs"] = float(noise_floor_dbfs)
        for key in ("calibration_id", "measurement_mode"):
            value = raw.get(key)
            if value:
                record_raw[key] = value
        if kind == "driver":
            record_raw["role"] = role
            record_raw["test_level_dbfs"] = played.get("test_level_dbfs")
            record_kwargs: dict[str, Any] = {
                "placement_proof": placement_proof,
                "repeat_store": backend.level_lease(),
                "admission_handoff": played.get("capture_admission"),
            }
            if frozen_capture_preset is not None:
                record_kwargs["preset"] = frozen_capture_preset
            try:
                record_payload = backend.record_driver_capture(
                    record_raw,
                    result.wav,
                    **record_kwargs,
                )
            except (OSError, RuntimeError, ValueError) as exc:
                if repeat_reservation and finish_failed_repeat_attempt is not None:
                    try:
                        _finish_reservation_failure(exc)
                    except (OSError, RuntimeError, ValueError) as persist_exc:
                        log_event(
                            logger,
                            "correction.crossover_repeat_persistence_failed",
                            level=logging.ERROR,
                            exc_info=True,
                            reason=type(persist_exc).__name__,
                        )
                raise
        else:
            record_raw["summed_test_id"] = played.get("summed_test_id") or played.get(
                "playback_id"
            )
            # These fields define which region/polarity the capture analyzes.
            # The relay is transport only; dropping them here silently relabels
            # every capture as the lowest in-phase region.
            for key in (
                "expect_null",
                "crossover_fc_hz",
                "polarity",
                "delay_ms",
                "delay_target_role",
            ):
                if key in raw:
                    record_raw[key] = raw[key]
            record_payload = backend.record_summed_capture(
                record_raw,
                result.wav,
                placement_proof=placement_proof,
                preset=frozen_capture_preset,
            )
        log_event(
            logger,
            "correction.crossover_relay_recorded",
            kind=kind,
            group_id=group_id,
            role=role or None,
            recorded=bool(record_payload.get("recorded")),
            snr_verdict=((record_payload.get("acoustic") or {}).get("snr") or {}).get(
                "verdict"
            ),
            repeat_attempts=(record_payload.get("repeat_progress") or {}).get(
                "attempts"
            ),
            excitation_source=(record_raw.get("excitation") or {}).get(
                "gain_source"
            ),
            effective_peak_dbfs=(record_raw.get("excitation") or {}).get(
                "effective_peak_dbfs"
            ),
            placement_schema=placement_proof.get("schema_version"),
            placement_policy=placement_proof.get("policy_id"),
            comparison_set_id=placement_proof.get("comparison_set_id"),
        )

    return _run_and_consume
