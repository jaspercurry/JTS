# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""HTTPS active-crossover microphone measurement flow."""

from __future__ import annotations

import html
import logging
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
CROSSOVER_RELAY_KINDS = ("driver", "summed")


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

  <section class="info-card" aria-live="polite">
    <div id="crossover-action" class="measurement-row__actions"></div>
    <div id="crossover-relay" class="hidden">
      <p id="crossover-relay-status" class="form-hint"></p>
      <a id="crossover-relay-link" class="btn btn--primary hidden" href="#">Open phone capture</a>
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
) -> tuple[dict[str, Any], HTTPStatus]:
    """Compile and atomically apply the explicitly selected profile owner."""
    from . import correction_crossover_backend as backend

    tuning_owner = str(raw.get("tuning_owner") or "")
    if tuning_owner not in {"manual", "automatic"}:
        return {
            "status": "refused",
            "error": "Choose manual or automatic crossover tuning before applying.",
        }, HTTPStatus.BAD_REQUEST
    payload = run_async(
        backend.apply_profile(
            tuning_owner=tuning_owner,
            camilla_factory=camilla_factory,
        ),
        timeout=30.0,
    )
    if payload.get("status") != "applied":
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


def handle_driver_capture(
    handler: Any,
    wav_body: bytes,
) -> tuple[dict[str, Any], HTTPStatus]:
    from . import correction_crossover_backend as backend

    payload = backend.record_driver_capture(_request_payload(handler), wav_body)
    return payload, HTTPStatus.OK


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
    """The crossover relay capture kind (``driver`` | ``summed``), validated.

    Kept small + strict at the boundary (extensibility doctrine): an unknown
    kind fails loud rather than silently defaulting to one of the two paths."""
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
    comparison_set: Mapping[str, Any] | None = None,
    target_fingerprint: str = "",
    current_comparison_set: Callable[[], Mapping[str, Any]] | None = None,
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

    kind = relay_kind_from_raw(raw)
    group_id = str((raw or {}).get("speaker_group_id") or "").strip()
    role = str((raw or {}).get("role") or "").strip().lower()
    # Stash the play response between on_armed (poll thread) and the post-capture
    # record so the record carries the exact sweep the Pi played.
    played: dict[str, Any] = {}
    placement_proof: dict[str, Any] = {}

    async def _play() -> dict[str, Any]:
        from jasper.correction import coordinator

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
                if kind == "driver":
                    return await backend.play_driver_capture_sweep(
                        {"speaker_group_id": group_id, "role": role},
                        camilla_factory=camilla_factory,
                        blocking_phase=phase,
                    )
                return await backend.play_summed_capture_sweep(
                    {"speaker_group_id": group_id},
                    camilla_factory=camilla_factory,
                    blocking_phase=phase,
                )
            finally:
                # Restore before measurement_window resumes household audio.
                if restore_play is not None:
                    await restore_play()

    def _post_phase(session_id: str, pull_token: str, phase: str, **extra: Any) -> None:
        """Post a REQUIRED progress event; failures propagate.

        The phone deadline-waits on ``sweep_complete`` — a swallowed post
        failure would leave it recording to its hard timeout with no
        breadcrumb. Mirrors the room path (`_run_relay_measurement_sweep`),
        where a failed ``sweep_started``/``sweep_complete`` post fails the
        capture."""
        if post_host_event is None:
            return
        post_host_event(session_id, pull_token, {"phase": phase, **extra})

    def _post_failed(session_id: str, pull_token: str, error: str) -> None:
        """Post the terminal ``sweep_failed`` best-effort — we are already on
        the failure path, so a post failure is logged at WARNING, not raised."""
        if post_host_event is None:
            return
        try:
            post_host_event(
                session_id, pull_token, {"phase": "sweep_failed", "error": error}
            )
        except (RuntimeError, OSError, ValueError):
            logger.warning(
                "crossover relay sweep_failed host-event post failed",
                exc_info=True,
            )

    async def _run_and_consume(client: Any, pi_session: Any) -> None:
        import asyncio

        from jasper.capture_relay.session import (
            purge,
            run_capture,
            validate_capture_acknowledgement,
        )

        def _on_armed(state: Any) -> None:
            # Called from run_capture's poll thread. `run_async` posts the play
            # coroutine back to the correction event loop (run_coroutine_threadsafe)
            # and blocks THIS thread — never the loop — until the sweep finishes.
            try:
                if current_comparison_set is not None:
                    current = current_comparison_set()
                    expected = comparison_set or {}
                    if (
                        current.get("comparison_set_id")
                        != expected.get("comparison_set_id")
                        or current.get("fingerprint")
                        != expected.get("fingerprint")
                    ):
                        raise ValueError(
                            "the crossover measurement level changed after this "
                            "link was created; run the level check again"
                        )
                acknowledgement = validate_capture_acknowledgement(
                    state,
                    pi_session.spec,
                )
                required = pi_session.spec.acknowledgement
                if acknowledgement is None or required is None:
                    raise ValueError(
                        "confirm the microphone placement before starting the sweep"
                    )
                from jasper.active_speaker.capture_geometry import (
                    normalized_placement_proof,
                )

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
                _post_phase(
                    pi_session.session_id,
                    pi_session.pull_token,
                    "sweep_started",
                )
                payload = run_async(_play(), timeout=45.0)
                if not capture_sweep_played(payload):
                    raise ValueError(playback_issue_text(
                        payload,
                        "the crossover capture sweep did not play — "
                        "confirm the driver first",
                    ))
                played.clear()
                played.update(payload)
                _post_phase(
                    pi_session.session_id,
                    pi_session.pull_token,
                    "sweep_complete",
                )
            except (RuntimeError, OSError, ValueError) as exc:
                _post_failed(
                    pi_session.session_id,
                    pi_session.pull_token,
                    str(exc),
                )
                raise

        # run_capture blocks until the phone finishes recording, so it MUST run
        # off the correction event loop (mirrors the room flow's
        # `await asyncio.to_thread(run_and_store, ...)`) — otherwise the whole
        # capture window would freeze the loop the play coroutine schedules onto.
        result = await asyncio.to_thread(
            run_capture, client, pi_session, on_armed=_on_armed
        )
        purge(client, pi_session)

        if validate_capture is not None:
            validate_capture(result)

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
        }
        for key in ("calibration_id", "measurement_mode"):
            value = raw.get(key)
            if value:
                record_raw[key] = value
        if kind == "driver":
            record_raw["role"] = role
            record_raw["test_level_dbfs"] = played.get("test_level_dbfs")
            record_payload = backend.record_driver_capture(
                record_raw,
                result.wav,
                placement_proof=placement_proof,
            )
        else:
            record_raw["summed_test_id"] = played.get("summed_test_id") or played.get(
                "playback_id"
            )
            record_payload = backend.record_summed_capture(
                record_raw,
                result.wav,
                placement_proof=placement_proof,
            )
        log_event(
            logger,
            "correction.crossover_relay_recorded",
            kind=kind,
            group_id=group_id,
            role=role or None,
            recorded=bool(record_payload.get("recorded")),
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
