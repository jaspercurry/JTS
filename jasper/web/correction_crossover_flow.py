# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""HTTPS active-crossover microphone measurement flow."""

from __future__ import annotations

import html
from http import HTTPStatus
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from ._common import canonical_header, canonical_page
from .correction_hub import section_tabs

AsyncRunner = Callable[..., Any]
CamillaFactory = Callable[[], Any]


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
    <h2 class="section__title">Active crossover measurement</h2>
    <p class="form-hint">Use this secure mic surface after the basic active-crossover setup is working by ear.</p>
  </section>

  <section id="mic-support" class="info-card" aria-live="polite">
    <h2 class="section__title">Microphone</h2>
    <p id="mic-support-message" class="form-hint">Checking microphone support…</p>
    <button id="check-mic" type="button" class="btn btn--ghost">Check microphone</button>
  </section>

  <section class="info-card">
    <div class="section-head">
      <div>
        <h2 class="section__title">Driver level captures</h2>
        <p class="form-hint">Play each driver, confirm the right driver sounded, then record the secure mic sweep.</p>
      </div>
      <button id="refresh-status" type="button" class="btn btn--ghost">Refresh</button>
    </div>
    <div id="driver-targets" class="measurement-list" aria-live="polite"></div>
  </section>

  <section class="info-card">
    <h2 class="section__title">Summed crossover captures</h2>
    <p class="form-hint">Run the combined crossover test first, then record the secure mic capture here.</p>
    <div id="summed-targets" class="measurement-list" aria-live="polite"></div>
  </section>

  <p id="capture-status" class="capture-status" role="status" aria-live="polite"></p>
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


def handle_status() -> tuple[dict[str, Any], HTTPStatus]:
    from . import correction_crossover_backend as backend

    return backend.status_payload(), HTTPStatus.OK


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
