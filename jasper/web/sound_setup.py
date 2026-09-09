# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared backend for three Sound pages: /sound/eq/, /sound/speaker/, /sound/output/.

nginx names the page in ``X-JTS-Sound-Page`` and strips the public prefix, so
the routes this server answers are the bare paths listed in
``do_GET``/``do_POST`` below. EQ owns preference profiles, Speaker setup owns
the driver/layout domain, Output owns the I2S HAT and volume shaping.

The page is built on the canonical design system (jasper.web._common.
canonical_page + /assets/app.css). The view's Off / Saved / Draft tabs
ARE the live source: Off auditions bypass, Saved applies a chosen
profile, Draft hot-loads the working bands via /live-draft while editing
and commits via the Save footer. All durable writes go through /apply;
the safety floor (volume_limit, headroom preamp, room-PEQ preservation)
lives in the backend and is untouched here.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import sys
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from jasper.log_event import log_event
from jasper.sound.profile import (
    PROFILE_LIBRARY_PATH,
    PROFILE_PATH,
    SoundProfile,
    delete_named_profile,
    load_profile,
    rename_named_profile,
    save_named_profile,
)
from jasper.sound.settings import (
    load_sound_settings,
    output_trim_db as _output_trim,  # aliased so the probe's kwarg can't shadow it
)

from ._common import (
    JsonBodyError,
    begin_request,
    bonded_follower_active,
    bonded_follower_leader_web_url,
    canonical_header,
    canonical_page,
    guard_mutating_request,
    guard_read_request,
    json_island,
    read_json_object,
    reject_csrf,
    send_html_response,
    send_json_response,
    send_route_failure,
)
from .volume_floor_tone import VOLUME_FLOOR_TONE_SESSION
from .sound_active_speaker import (
    OutputHardwareRequestConflict,
    OutputTopologyRevisionConflict,
    _active_speaker_baseline_profile_apply_payload,
    _active_speaker_baseline_profile_payload,
    _active_speaker_calibration_level_payload,
    _active_speaker_channel_identity_save_payload,
    _active_speaker_channel_protection_save_payload,
    _active_speaker_check_path_safety_payload,
    _active_speaker_commission_load_payload,
    _active_speaker_commission_ramp_abort_payload,
    _active_speaker_commission_ramp_ack_payload,
    _active_speaker_commission_ramp_step_payload,
    _active_speaker_commission_rollback_payload,
    _active_speaker_commission_state_payload,
    _active_speaker_commissioning_view_payload,
    _active_speaker_crossover_preview_save_payload,
    _active_speaker_design_draft_payload,
    _active_speaker_design_draft_save_payload,
    _active_speaker_driver_measurement_payload,
    _active_speaker_driver_research_request_payload,
    _active_speaker_finish_commissioning_payload,
    _active_speaker_load_startup_config_payload,
    _active_speaker_rollback_startup_config_payload,
    _active_speaker_stage_config_payload,
    _active_speaker_stop_payload,
    _active_speaker_stop_summed_test_tone,
    _active_speaker_summed_test_level_payload,
    _active_speaker_summed_test_payload,
    _active_speaker_summed_validation_active_conflict,
    _active_speaker_summed_validation_payload,
    _output_topology_payload,
    _repin_output_topology_payload,
    _reset_output_topology_payload,
    _save_i2s_hat_payload,
    _save_output_topology_payload,
)

# `_GET_JSON_ROUTES` names its builders as strings and `_json_route_payload`
# resolves them off THIS module at call time, so a read-only route's builder
# has to be bound here even where no call site spells it out.
from .sound_active_speaker import (  # noqa: F401 - resolved by name
    _active_speaker_bringup_preflight_payload,
    _active_speaker_channel_identity_payload,
    _active_speaker_crossover_preview_payload,
    _active_speaker_environment_payload,
    _active_speaker_measurements_payload,
    _active_speaker_safe_playback_payload,
    _active_speaker_staged_config_payload,
    _active_speaker_startup_load_payload,
    _active_speaker_tuning_handoff_payload,
)

# The crossover writer and the audition entry point are reached through this
# module by jasper/web/correction_crossover_v2.py and
# jasper/calibration_agent/sound_actions.py.
from .sound_active_speaker import apply_measured_crossover_geometry  # noqa: F401
from .sound_profile_apply import audition_profile  # noqa: F401
from .sound_profile_apply import (
    _EQ_CARRIER_NOT_PROBED,
    _apply_profile,
    _apply_settings,
    _audition_profile,
    _camilla,
    _carrier_refusal,
    _live_draft_profile,
    _state_payload,
)

logger = logging.getLogger(__name__)

_FOLLOWER_BLOCKED_CONTENT_DSP_POSTS = frozenset({
        "/apply",
        "/audition",
        "/live-draft",
        "/settings",
        "/volume-floor/audition",
        "/volume-floor/stop",
        "/profiles/save",
        "/profiles/rename",
        "/profiles/delete",
})

DEFAULT_CONFIG_DIR = "/var/lib/camilladsp/configs"
MAX_JSON_BYTES = 64 * 1024

#: The public path of each ``X-JTS-Sound-Page`` mode nginx may set. Any other
#: header value renders EQ, the one page every profile serves.
_PAGE_PATHS = {
    "eq": "/sound/eq/",
    "speaker": "/sound/speaker/",
    "output": "/sound/output/",
}


def _coerce_page_mode(page_mode: str) -> str:
    return page_mode if page_mode in _PAGE_PATHS else "eq"


#: /sound/speaker/ renders the link to its own child row (docs/web-ia.md §1).
#: The href is RELATIVE so it stays on the origin the household is already on;
#: an absolute one would land on the self-signed 443 origin (issue #2632).
_CROSSOVER_CHILD_LINK = """<section class="info-card">
    <p class="form-hint">Measure the crossover between this speaker's drivers
    and set the filters that protect them.</p>
    <div class="form-actions"><a class="btn" href="crossover/">Active speaker</a></div>
  </section>"""


def _sound_page_island(*, page_mode: str, follower: bool) -> str:
    """The one ``sound-page-data`` island every /sound/ shell renders.

    The editor's filter and slope pickers are built from the crossover
    vocabulary carried here, read from the compiler rather than restated, so a
    value the compiler cannot build is never presented. The defaults ride along
    because the picker must pre-select the same member ``crossover_preview``
    would fill in.
    """

    from jasper.active_speaker.crossover_preview import (
        DEFAULT_FILTER_TYPE,
        DEFAULT_SLOPE_DB_PER_OCTAVE,
    )
    from jasper.active_speaker.declaration_vocabulary import (
        supported_declaration_filter_types,
        supported_declaration_slopes_db_per_octave,
    )

    return json_island(
        "sound-page-data",
        {
            "mode": page_mode,
            "follower": follower,
            "crossover_vocabulary": {
                "filter_types": list(supported_declaration_filter_types()),
                "slopes_db_per_octave": list(
                    supported_declaration_slopes_db_per_octave()
                ),
                "default_filter_type": DEFAULT_FILTER_TYPE,
                "default_slope_db_per_octave": DEFAULT_SLOPE_DB_PER_OCTAVE,
            },
        },
    )


def _follower_sound_html(csrf_token: str = "", *, page_mode: str) -> bytes:
    """Render one split Sound page for a bonded active follower.

    A bonded follower delegates the PROGRAM domain (content EQ, room
    correction, volume shaping) to the pair leader but still owns its LOCAL
    driver domain (the per-driver crossover / limiter / tweeter high-pass that
    protects the DAC it drives). Speaker setup keeps the delegation card and
    mounts the same active-speaker UI as a solo box; EQ and Output are
    delegation-only pages with a path back to local Speaker setup.

    The page island tells main.js to boot in follower speaker mode: only the
    active-speaker section, no Off/Saved/Draft editor or now-playing plot.
    Content-DSP POSTs still 409 (``_FOLLOWER_BLOCKED_CONTENT_DSP_POSTS``); the
    active-speaker commissioning/crossover endpoints are allowed.
    """
    page_mode = _coerce_page_mode(page_mode)
    leader_path = _PAGE_PATHS[page_mode]
    leader_sound_url = bonded_follower_leader_web_url(leader_path)
    leader_link = (
        '<a class="btn btn--primary" href="'
        + html.escape(leader_sound_url)
        + '">Open leader sound</a>'
        if leader_sound_url
        else ""
    )
    page_island = _sound_page_island(page_mode=page_mode, follower=True)
    title = (
        "EQ" if page_mode == "eq"
        else "Speaker setup" if page_mode == "speaker"
        else "Output"
    )
    local_setup = (
        '<div id="view-body"></div>'
        '<div class="status-line" id="status" role="status" aria-live="polite"></div>'
        '<link rel="modulepreload" href="/assets/sound-profile/js/topology.js">'
        '<script type="module" src="/assets/sound-profile/js/main.js"></script>'
        if page_mode == "speaker"
        else ""
    )
    local_setup_link = (
        '<a class="btn" href="/sound/speaker/">Open local speaker setup</a>'
        if page_mode != "speaker"
        else ""
    )
    header = canonical_header(title, back_href="/sound/", back_label="Sound", back_id="back")
    body = f"""
{header}
<main class="page">
  <section class="info-card info-card--accent" role="note">
    <h2 class="section__title">Sound is controlled by the pair leader</h2>
    <p class="form-hint">This speaker is an active follower, so content EQ,
    room correction, and volume shaping are rendered by the leader while the
    pair is active. Local crossover and driver-protection work stays with the
    speaker that owns the DAC path.</p>
    <div class="form-actions">
      {leader_link}
      {local_setup_link}
      <a class="btn" href="/sound/pair/">Manage pair</a>
    </div>
  </section>
  {local_setup}
</main>
{page_island}
"""
    return canonical_page(
        title,
        body,
        csrf_token=csrf_token,
        page_css_href="/assets/sound-profile/sound.css",
    )


def _index_html(csrf_token: str = "", *, page_mode: str = "eq") -> bytes:
    page_mode = _coerce_page_mode(page_mode)
    if bonded_follower_active():
        return _follower_sound_html(csrf_token, page_mode=page_mode)
    title = (
        "EQ" if page_mode == "eq"
        else "Speaker setup" if page_mode == "speaker"
        else "Output"
    )
    eq_tabs_html = (
        '<div><div class="segmented" role="tablist" aria-label="Sound source">'
        '<button class="segmented__btn" id="tab-off" data-view="off" aria-pressed="true">Off</button>'
        '<button class="segmented__btn" id="tab-saved" data-view="saved" aria-pressed="false">Saved</button>'
        '<button class="segmented__btn" id="tab-draft" data-view="draft" aria-pressed="false">Draft</button>'
        '</div></div>'
    )
    editor_chrome = (
        canonical_header(
            title, back_href="/sound/", back_label="Sound", back_id="back",
            tabs_html=eq_tabs_html, tabs_id="eq-tabs",
        )
        + """
<main class="page">
  <section class="now-playing" id="now-playing">
    <div class="row-between">
      <h2 class="eyebrow">Now playing</h2>
      <span class="now-playing__label" id="live-label">Bypass</span>
    </div>
    <div class="graph-card">
      <svg class="eq-graph" id="plot" viewBox="0 0 620 200" preserveAspectRatio="none"
           role="img" aria-label="EQ response preview"></svg>
    </div>
    <div class="sr-only" id="plot-summary" aria-live="polite"></div>
  </section>
  <div id="view-body"></div>
  <div class="status-line" id="status" role="status" aria-live="polite"></div>
</main>
"""
        if page_mode == "eq"
        else canonical_header(title, back_href="/sound/", back_label="Sound", back_id="back")
        + f"""
<main class="page">
  <div id="view-body"></div>
  <div class="status-line" id="status" role="status" aria-live="polite"></div>
  {_CROSSOVER_CHILD_LINK if page_mode == "speaker" else ""}
</main>
"""
    )
    page_island = _sound_page_island(page_mode=page_mode, follower=False)
    body = editor_chrome + page_island + (
        '<link rel="modulepreload" href="/assets/sound-profile/js/topology.js">'
        '<script type="module" src="/assets/sound-profile/js/main.js"></script>'
    )
    return canonical_page(
        title,
        body,
        csrf_token=csrf_token,
        page_css_href="/assets/sound-profile/sound.css",
    )


#: Read-only GET routes whose entire handler is "send this payload, or
#: answer 502 under this event name", dispatched once in :func:`_make_handler`;
#: a route that needs the handler's own state (the two commissioning views
#: close over ``camilla_factory``) stays spelled out there. The log-event drift
#: pin reads the event strings here. The builder is NAMED, not captured: a
#: table holding the objects it had at import time would answer with a builder
#: the module no longer has.
_GET_JSON_ROUTES: dict[str, tuple[str, str]] = {
    "/output-topology": ("_output_topology_payload", "sound.output_topology"),
    "/active-speaker/design-draft": (
        "_active_speaker_design_draft_payload",
        "sound.active_speaker_design_draft",
    ),
    "/active-speaker/crossover-preview": (
        "_active_speaker_crossover_preview_payload",
        "sound.active_speaker_crossover_preview",
    ),
    "/active-speaker/measurements": (
        "_active_speaker_measurements_payload",
        "sound.active_speaker_measurements",
    ),
    "/active-speaker/baseline-profile": (
        "_active_speaker_baseline_profile_payload",
        "sound.active_speaker_baseline_profile",
    ),
    "/active-speaker/tuning-handoff": (
        "_active_speaker_tuning_handoff_payload",
        "sound.active_speaker_tuning_handoff",
    ),
    "/active-speaker/environment": (
        "_active_speaker_environment_payload",
        "sound.active_speaker_environment",
    ),
    "/active-speaker/safe-playback": (
        "_active_speaker_safe_playback_payload",
        "sound.active_speaker_safe_playback",
    ),
    "/active-speaker/calibration-level": (
        "_active_speaker_calibration_level_payload",
        "sound.active_speaker_calibration_level",
    ),
    "/active-speaker/bringup-preflight": (
        "_active_speaker_bringup_preflight_payload",
        "sound.active_speaker_bringup_preflight",
    ),
    "/active-speaker/startup-load": (
        "_active_speaker_startup_load_payload",
        "sound.active_speaker_startup_load",
    ),
    "/active-speaker/staged-config": (
        "_active_speaker_staged_config_payload",
        "sound.active_speaker_staged_config",
    ),
    "/active-speaker/channel-identity": (
        "_active_speaker_channel_identity_payload",
        "sound.active_speaker_channel_identity",
    ),
}


def _json_route_payload(builder: str) -> dict[str, Any]:
    """Call one :data:`_GET_JSON_ROUTES` builder, resolved at call time."""
    fn: Callable[[], dict[str, Any]] = getattr(sys.modules[__name__], builder)
    return fn()


def _requested_page_mode(headers: Any) -> str:
    """Which split page a request is for: nginx sets the header per location."""
    return _coerce_page_mode(headers.get("X-JTS-Sound-Page", "eq"))


def _eq_carrier_block(
    profile: SoundProfile,
    *,
    config_dir: str | Path,
    camilla_factory: Callable[[], Any],
    output_trim_db: float,
) -> Any:
    """Probe the LOADED graph for /state: a refusal, ``None``, or "not probed".

    ``output_trim_db`` is the household's real trim, computed as the apply path
    computes it: the emitter folds the trim into ``total_headroom_db`` against
    ``MAX_PROGRAM_HEADROOM_DB``, so probing at 0 dB would report a graph
    hostable that the save then refuses.

    Fail-OPEN: an unreachable CamillaDSP, an empty path, or a probe that blows
    up returns "not probed" and the page keeps its editor. The /apply and
    /settings refusals stay the fail-closed gate.
    """
    from jasper.sound.graph_carrier import eq_block_for_loaded_config

    try:
        current_path = asyncio.run(
            camilla_factory().get_config_file_path(best_effort=True)
        )
        if not current_path:
            return _EQ_CARRIER_NOT_PROBED
        return eq_block_for_loaded_config(
            profile,
            current_path=current_path,
            config_dir=config_dir,
            output_trim_db=output_trim_db,
        )
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        logger.warning("sound: eq-carrier probe unavailable", exc_info=True)
        return _EQ_CARRIER_NOT_PROBED


def _make_handler(
    *,
    profile_path: str | Path,
    library_path: str | Path,
    config_dir: str | Path,
    camilla_factory: Callable[[], Any] = _camilla,
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            logger.info("%s - %s", self.address_string(), fmt % args)

        def _send_html(self, body: bytes, *, status: int = 200) -> None:
            send_html_response(self, body, status=status)

        def _send_json(self, payload: dict[str, Any], *, status: int = 200) -> None:
            self._json_response_started = True
            send_json_response(self, payload, status=status)

        def _read_json(self, *, max_bytes: int = MAX_JSON_BYTES) -> dict[str, Any]:
            return read_json_object(self, max_bytes=max_bytes)

        def do_GET(self) -> None:  # noqa: N802
            path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
            if path not in {
                "/",
                "/state",
                "/output-topology",
                "/active-speaker/design-draft",
                "/active-speaker/crossover-preview",
                "/active-speaker/measurements",
                "/active-speaker/baseline-profile",
                "/active-speaker/tuning-handoff",
                "/active-speaker/environment",
                "/active-speaker/safe-playback",
                "/active-speaker/calibration-level",
                "/active-speaker/bringup-preflight",
                "/active-speaker/startup-load",
                "/active-speaker/commission-state",
                "/active-speaker/commissioning-view",
                "/active-speaker/staged-config",
                "/active-speaker/channel-identity",
            }:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if not guard_read_request(self):
                return
            if path == "/":
                ctx = begin_request(self)
                self._send_html(
                    _index_html(
                        ctx["csrf_token"],
                        page_mode=_requested_page_mode(self.headers),
                    )
                )
                return
            if path == "/state":
                profile = load_profile(profile_path)
                settings = load_sound_settings()
                # Only /sound/eq/ renders the editor, and the probe is a
                # dry-run recompose of the loaded graph, so no other page pays
                # for it — it keeps the "not probed" default.
                eq_block: Any = _EQ_CARRIER_NOT_PROBED
                if _requested_page_mode(self.headers) == "eq":
                    eq_block = _eq_carrier_block(
                        profile,
                        config_dir=config_dir,
                        camilla_factory=camilla_factory,
                        output_trim_db=_output_trim(profile, settings),
                    )
                self._send_json(
                    _state_payload(
                        profile,
                        library_path=library_path,
                        include_library=True,
                        settings_snapshot=settings,
                        eq_block=eq_block,
                    )
                )
                return
            json_route = _GET_JSON_ROUTES.get(path)
            if json_route is not None:
                builder, event = json_route
                try:
                    self._send_json(_json_route_payload(builder))
                except Exception as e:  # noqa: BLE001
                    send_route_failure(
                        self._send_json, e, logger=logger, event=event,
                    )
                return
            if path == "/active-speaker/commission-state":
                try:
                    self._send_json(
                        asyncio.run(
                            _active_speaker_commission_state_payload(
                                camilla_factory=camilla_factory,
                            )
                        )
                    )
                except Exception as e:  # noqa: BLE001
                    send_route_failure(
                        self._send_json, e, logger=logger,
                        event="sound.active_speaker_commission",
                    )
                return
            if path == "/active-speaker/commissioning-view":
                try:
                    self._send_json(
                        asyncio.run(
                            _active_speaker_commissioning_view_payload(
                                camilla_factory=camilla_factory,
                            )
                        )
                    )
                except Exception as e:  # noqa: BLE001
                    send_route_failure(
                        self._send_json, e, logger=logger,
                        event="sound.active_speaker_commissioning_view",
                    )
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            self._json_response_started = False
            path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
            if path not in {
                "/apply",
                "/audition",
                "/live-draft",
                "/preview",
                "/settings",
                "/volume-floor/audition",
                "/volume-floor/stop",
                "/active-speaker/design-draft",
                "/active-speaker/driver-research-request",
                "/active-speaker/crossover-preview",
                "/active-speaker/stop",
                "/active-speaker/calibration-level",
                "/active-speaker/channel-identity",
                "/active-speaker/channel-protection",
                "/active-speaker/stage-config",
                "/active-speaker/check-path-safety",
                "/active-speaker/load-startup-config",
                "/active-speaker/rollback-startup-config",
                "/active-speaker/commission-load",
                "/active-speaker/commission-rollback",
                "/active-speaker/commission-ramp-step",
                "/active-speaker/commission-ramp-ack",
                "/active-speaker/commission-ramp-abort",
                "/active-speaker/driver-measurement",
                "/active-speaker/summed-test",
                "/active-speaker/summed-test/level",
                "/active-speaker/summed-test/stop",
                "/active-speaker/summed-validation",
                "/active-speaker/baseline-profile",
                "/active-speaker/baseline-profile/apply",
                "/active-speaker/baseline-profile/save-and-apply",
                "/output-topology",
                "/output-topology/reset",
                "/output-topology/repin",
                "/profiles/save",
                "/profiles/rename",
                "/profiles/delete",
                "/i2s-hat",
            }:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if not guard_mutating_request(self):
                reject_csrf(self)
                return
            if path in _FOLLOWER_BLOCKED_CONTENT_DSP_POSTS and bonded_follower_active():
                log_event(
                    logger,
                    "sound.follower_content_dsp_blocked",
                    path=path,
                )
                self._send_json(
                    {
                        "error": (
                            "sound profile is controlled on the pair leader "
                            "while this speaker is a follower"
                        ),
                    },
                    status=HTTPStatus.CONFLICT,
                )
                return
            try:
                raw = self._read_json(max_bytes=MAX_JSON_BYTES)
                if path == "/i2s-hat":
                    profile_id = raw.get("profile_id")
                    if profile_id is not None and not isinstance(profile_id, str):
                        self._send_json(
                            {"error": "profile_id must be a string or null"},
                            status=400,
                        )
                        return
                    try:
                        payload, result = _save_i2s_hat_payload(profile_id)
                    except ValueError as e:
                        self._send_json({"error": str(e)}, status=HTTPStatus.BAD_REQUEST)
                        return
                    except (OSError, RuntimeError) as e:
                        self._send_json({"error": str(e)}, status=502)
                        return
                    if not result.get("ok"):
                        error = result.get("error") or result.get("stderr")
                        payload["error"] = str(error or "hardware apply failed")
                    self._send_json(payload, status=200 if result.get("ok") else 502)
                    return
                if path == "/active-speaker/stop":
                    self._send_json(_active_speaker_stop_payload())
                    return
                if path == "/active-speaker/calibration-level":
                    self._send_json(_active_speaker_calibration_level_payload(raw))
                    return
                if path == "/active-speaker/channel-identity":
                    try:
                        self._send_json(
                            _active_speaker_channel_identity_save_payload(raw)
                        )
                    except (OSError, RuntimeError) as e:
                        send_route_failure(
                            self._send_json, e, logger=logger,
                            event="sound.active_speaker_channel_identity",
                            error=type(e).__name__,
                        )
                    return
                if path == "/active-speaker/channel-protection":
                    try:
                        self._send_json(
                            _active_speaker_channel_protection_save_payload(raw)
                        )
                    except OSError as e:
                        send_route_failure(
                            self._send_json, e, logger=logger,
                            event="sound.active_speaker_channel_protection",
                            error=type(e).__name__,
                        )
                    return
                if path == "/active-speaker/stage-config":
                    self._send_json(_active_speaker_stage_config_payload(raw))
                    return
                if path == "/active-speaker/design-draft":
                    from jasper.active_speaker.design_draft import (
                        ActiveSpeakerDesignDraftRevisionConflict,
                    )

                    try:
                        self._send_json(_active_speaker_design_draft_save_payload(raw))
                    except ActiveSpeakerDesignDraftRevisionConflict as e:
                        payload = _active_speaker_design_draft_payload()
                        payload["error"] = str(e)
                        self._send_json(payload, status=HTTPStatus.CONFLICT)
                    except OSError as e:
                        send_route_failure(
                            self._send_json, e, logger=logger,
                            event="sound.active_speaker_design_draft_save",
                            error=type(e).__name__,
                        )
                    return
                if path == "/active-speaker/driver-research-request":
                    self._send_json(
                        _active_speaker_driver_research_request_payload(raw)
                    )
                    return
                if path == "/active-speaker/crossover-preview":
                    try:
                        self._send_json(
                            _active_speaker_crossover_preview_save_payload()
                        )
                    except OSError as e:
                        send_route_failure(
                            self._send_json, e, logger=logger,
                            event="sound.active_speaker_crossover_preview_save",
                            error=type(e).__name__,
                        )
                    return
                if path == "/active-speaker/driver-measurement":
                    try:
                        self._send_json(_active_speaker_driver_measurement_payload(raw))
                    except OSError as e:
                        send_route_failure(
                            self._send_json, e, logger=logger,
                            event="sound.active_speaker_driver_measurement",
                            error=type(e).__name__,
                        )
                    return
                if path == "/active-speaker/summed-test":
                    try:
                        self._send_json(
                            asyncio.run(
                                _active_speaker_summed_test_payload(
                                    raw,
                                    camilla_factory=camilla_factory,
                                )
                            )
                        )
                    except OSError as e:
                        send_route_failure(
                            self._send_json, e, logger=logger,
                            event="sound.active_speaker_summed_test",
                            error=type(e).__name__,
                        )
                    return
                if path == "/active-speaker/summed-test/level":
                    try:
                        self._send_json(
                            asyncio.run(
                                _active_speaker_summed_test_level_payload(
                                    raw,
                                    camilla_factory=camilla_factory,
                                )
                            )
                        )
                    except OSError as e:
                        send_route_failure(
                            self._send_json, e, logger=logger,
                            event="sound.active_speaker_summed_test_level",
                            error=type(e).__name__,
                        )
                    return
                if path == "/active-speaker/summed-test/stop":
                    reason = str(raw.get("reason") or "operator_stop")
                    self._send_json(
                        _active_speaker_stop_summed_test_tone(reason=reason)
                    )
                    return
                if path == "/active-speaker/summed-validation":
                    try:
                        conflict = _active_speaker_summed_validation_active_conflict(
                            raw
                        )
                        if conflict is not None:
                            log_event(
                                logger,
                                "sound.active_speaker_summed_validation",
                                status="blocked",
                                reason="active_summed_test_running",
                                group_id=str(conflict.get("speaker_group_id")),
                                active_playback_id=str((
                                        conflict.get("active_summed_test", {})
                                        if isinstance(
                                            conflict.get("active_summed_test"), dict
                                        )
                                        else {}
                                ).get("playback_id")),
                            )
                            self._send_json(conflict, status=HTTPStatus.CONFLICT)
                            return
                        self._send_json(_active_speaker_summed_validation_payload(raw))
                    except OSError as e:
                        send_route_failure(
                            self._send_json, e, logger=logger,
                            event="sound.active_speaker_summed_validation",
                            error=type(e).__name__,
                        )
                    return
                if path == "/active-speaker/baseline-profile":
                    try:
                        self._send_json(
                            _active_speaker_baseline_profile_payload(write=True)
                        )
                    except OSError as e:
                        send_route_failure(
                            self._send_json, e, logger=logger,
                            event="sound.active_speaker_baseline_profile",
                            error=type(e).__name__,
                        )
                    return
                if path == "/active-speaker/baseline-profile/apply":
                    self._send_json(
                        asyncio.run(
                            _active_speaker_baseline_profile_apply_payload(
                                expected_candidate_fingerprint=str(
                                    raw.get("expected_candidate_fingerprint") or ""
                                ),
                                camilla_factory=camilla_factory,
                            )
                        )
                    )
                    return
                if path == "/active-speaker/baseline-profile/save-and-apply":
                    self._send_json(
                        asyncio.run(
                            _active_speaker_finish_commissioning_payload(
                                expected_candidate_fingerprint=str(
                                    raw.get("expected_candidate_fingerprint") or ""
                                ),
                                camilla_factory=camilla_factory,
                            )
                        )
                    )
                    return
                if path == "/active-speaker/check-path-safety":
                    self._send_json(
                        asyncio.run(
                            _active_speaker_check_path_safety_payload(
                                camilla_factory=camilla_factory,
                            )
                        )
                    )
                    return
                if path == "/active-speaker/load-startup-config":
                    self._send_json(
                        asyncio.run(
                            _active_speaker_load_startup_config_payload(
                                camilla_factory=camilla_factory,
                            )
                        )
                    )
                    return
                if path == "/active-speaker/rollback-startup-config":
                    self._send_json(
                        asyncio.run(
                            _active_speaker_rollback_startup_config_payload(
                                camilla_factory=camilla_factory,
                            )
                        )
                    )
                    return
                if path == "/active-speaker/commission-load":
                    self._send_json(
                        asyncio.run(
                            _active_speaker_commission_load_payload(
                                raw, camilla_factory=camilla_factory
                            )
                        )
                    )
                    return
                if path == "/active-speaker/commission-rollback":
                    self._send_json(
                        asyncio.run(
                            _active_speaker_commission_rollback_payload(
                                camilla_factory=camilla_factory
                            )
                        )
                    )
                    return
                if path == "/active-speaker/commission-ramp-step":
                    self._send_json(
                        asyncio.run(
                            _active_speaker_commission_ramp_step_payload(
                                raw, camilla_factory=camilla_factory
                            )
                        )
                    )
                    return
                if path == "/active-speaker/commission-ramp-ack":
                    self._send_json(
                        asyncio.run(
                            _active_speaker_commission_ramp_ack_payload(
                                raw, camilla_factory=camilla_factory
                            )
                        )
                    )
                    return
                if path == "/active-speaker/commission-ramp-abort":
                    self._send_json(
                        asyncio.run(
                            _active_speaker_commission_ramp_abort_payload(
                                camilla_factory=camilla_factory
                            )
                        )
                    )
                    return
                if path == "/output-topology":
                    try:
                        self._send_json(
                            _save_output_topology_payload(raw, require_revision=True)
                        )
                    except OutputTopologyRevisionConflict as e:
                        log_event(
                            logger,
                            "sound.output_topology_save",
                            level=logging.WARNING,
                            result="conflict",
                            error=type(e).__name__,
                        )
                        payload = _output_topology_payload()
                        payload["error"] = str(e)
                        self._send_json(payload, status=HTTPStatus.CONFLICT)
                    except (OSError, RuntimeError) as e:
                        send_route_failure(
                            self._send_json, e, logger=logger,
                            event="sound.output_topology_save",
                            error=type(e).__name__,
                        )
                    return
                if path == "/output-topology/reset":
                    try:
                        self._send_json(_reset_output_topology_payload(raw))
                    except OutputHardwareRequestConflict as e:
                        payload = _output_topology_payload()
                        payload["error"] = str(e)
                        payload["conflict"] = e.code
                        self._send_json(payload, status=HTTPStatus.CONFLICT)
                    except ValueError as e:
                        self._send_json({"error": str(e)}, status=HTTPStatus.BAD_REQUEST)
                    except (OSError, RuntimeError) as e:
                        log_event(
                            logger,
                            "sound.output_topology_reset",
                            level=logging.ERROR,
                            exc_info=True,
                            result="error",
                            error=type(e).__name__,
                        )
                        message = (
                            "JTS could not confirm whether speaker setup was reset. "
                            "Review the current setup and try again."
                        )
                        try:
                            payload = _output_topology_payload()
                        except (OSError, RuntimeError, ValueError):
                            payload = {}
                        payload["error"] = message
                        payload["reset"] = {
                            "status": "needs_attention",
                            "message": message,
                        }
                        self._send_json(payload, status=502)
                    return
                if path == "/output-topology/repin":
                    try:
                        self._send_json(_repin_output_topology_payload(raw))
                    except OutputHardwareRequestConflict as e:
                        payload = _output_topology_payload()
                        payload["error"] = str(e)
                        payload["conflict"] = e.code
                        self._send_json(payload, status=HTTPStatus.CONFLICT)
                    except ValueError as e:
                        self._send_json({"error": str(e)}, status=HTTPStatus.BAD_REQUEST)
                    except (OSError, RuntimeError) as e:
                        log_event(
                            logger,
                            "sound.output_topology_repin",
                            level=logging.ERROR,
                            exc_info=True,
                            result="error",
                            error=type(e).__name__,
                        )
                        message = (
                            "JTS could not confirm whether the new DAC was pinned. "
                            "Review the current setup and try again."
                        )
                        try:
                            payload = _output_topology_payload()
                        except (OSError, RuntimeError, ValueError):
                            payload = {}
                        payload["error"] = message
                        payload["repin"] = {
                            "status": "needs_attention",
                            "message": message,
                        }
                        self._send_json(payload, status=502)
                    return
                if path == "/settings":
                    try:
                        payload = asyncio.run(
                            _apply_settings(
                                raw,
                                profile_path=profile_path,
                                library_path=library_path,
                                config_dir=config_dir,
                                camilla_factory=camilla_factory,
                            )
                        )
                    except OSError as e:
                        logger.exception("sound settings save failed")
                        self._send_json({"error": str(e)}, status=502)
                        return
                    self._send_json(payload)
                    return
                if path == "/volume-floor/audition":
                    try:
                        self._send_json(
                            asyncio.run(
                                VOLUME_FLOOR_TONE_SESSION.start_or_update(
                                    raw,
                                    camilla_factory=camilla_factory,
                                )
                            )
                        )
                    except (OSError, RuntimeError, ValueError, TypeError) as e:
                        logger.exception("volume floor audition failed")
                        self._send_json({"error": str(e)}, status=502)
                    return
                if path == "/volume-floor/stop":
                    try:
                        self._send_json(
                            asyncio.run(
                                VOLUME_FLOOR_TONE_SESSION.stop(
                                    camilla_factory=camilla_factory,
                                    reason=str(raw.get("reason") or "stop"),
                                )
                            )
                        )
                    except (OSError, RuntimeError, ValueError, TypeError) as e:
                        logger.exception("volume floor tone stop failed")
                        self._send_json({"error": str(e)}, status=502)
                    return
                if path.startswith("/profiles/"):
                    try:
                        if path == "/profiles/save":
                            requested_id = str(raw.get("id") or "")
                            entry = save_named_profile(
                                SoundProfile.from_mapping(raw.get("profile")),
                                name=raw.get("name"),
                                path=library_path,
                                profile_id=requested_id,
                            )
                            action = "update" if requested_id == entry.id else "create"
                            log_event(
                                logger,
                                "sound.profile_library",
                                action=action,
                                profile_id=entry.id,
                                curve=entry.profile.curve_id,
                                bands=len(entry.profile.parametric_bands),
                            )
                            payload = _state_payload(
                                load_profile(profile_path),
                                library_path=library_path,
                                include_library=True,
                            )
                            payload["profile_entry"] = entry.to_payload()
                        elif path == "/profiles/rename":
                            entry = rename_named_profile(
                                str(raw.get("id") or ""),
                                name=str(raw.get("name") or ""),
                                path=library_path,
                            )
                            log_event(
                                logger,
                                "sound.profile_library",
                                action="rename",
                                profile_id=entry.id,
                                curve=entry.profile.curve_id,
                                bands=len(entry.profile.parametric_bands),
                            )
                            payload = _state_payload(
                                load_profile(profile_path),
                                library_path=library_path,
                                include_library=True,
                            )
                            payload["profile_entry"] = entry.to_payload()
                        else:
                            deleted_id = str(raw.get("id") or "")
                            delete_named_profile(deleted_id, path=library_path)
                            log_event(
                                logger,
                                "sound.profile_library",
                                action="delete",
                                profile_id=deleted_id,
                            )
                            payload = _state_payload(
                                load_profile(profile_path),
                                library_path=library_path,
                                include_library=True,
                            )
                            payload["deleted_profile_id"] = deleted_id
                    except OSError as e:
                        logger.exception("sound profile library update failed")
                        self._send_json({"error": str(e)}, status=502)
                        return
                    self._send_json(payload)
                    return
                if path in {"/audition", "/live-draft"}:
                    raw_profile = raw.get("profile", raw)
                else:
                    raw_profile = raw
                profile = SoundProfile.from_mapping(raw_profile)
            except (JsonBodyError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as e:
                self._send_json({"error": str(e)}, status=400)
                return
            except (OSError, RuntimeError) as e:
                if self._json_response_started:
                    raise
                log_event(
                    logger,
                    "sound.post_dispatch_failed",
                    path=path,
                    error_type=type(e).__name__,
                    level=logging.ERROR,
                    exc_info=True,
                )
                self._send_json({"error": str(e)}, status=502)
                return
            if path == "/preview":
                self._send_json(_state_payload(profile))
                return
            try:
                if path in {"/audition", "/live-draft"}:
                    if path == "/live-draft":
                        expected_epoch = raw.get("dsp_write_epoch")
                        if not isinstance(expected_epoch, str) or not expected_epoch:
                            self._send_json(
                                {"error": "missing dsp_write_epoch"},
                                status=400,
                            )
                            return
                        payload = asyncio.run(
                            _live_draft_profile(
                                profile,
                                expected_dsp_write_epoch=expected_epoch,
                                config_dir=config_dir,
                                profile_path=profile_path,
                                camilla_factory=camilla_factory,
                            )
                        )
                    else:
                        audition_mode = str(raw.get("mode") or "draft")
                        if audition_mode not in {"bypass", "applied", "draft"}:
                            audition_mode = "draft"
                        payload = asyncio.run(
                            _audition_profile(
                                profile,
                                audition_mode=audition_mode,
                                profile_path=profile_path,
                                library_path=library_path,
                                config_dir=config_dir,
                                camilla_factory=camilla_factory,
                            )
                        )
                else:
                    payload = asyncio.run(
                        _apply_profile(
                            profile,
                            profile_path=profile_path,
                            library_path=library_path,
                            config_dir=config_dir,
                            camilla_factory=camilla_factory,
                        )
                    )
            except Exception as e:  # noqa: BLE001
                refusal = _carrier_refusal(e)
                if refusal is not None:
                    # The loaded graph cannot host EQ: a known, handled state,
                    # not a server error. 200 with a typed body, NOT the 409
                    # used for the follower-block — the page reads
                    # reason_code/message from the body, and a 4xx would be
                    # swallowed by its `if (!resp.ok) throw` into a generic
                    # error, losing the honest reason.
                    log_event(
                        logger,
                        "sound.eq_blocked",
                        path=path,
                        reason=refusal.reason_code,
                    )
                    self._send_json(refusal.to_payload())
                    return
                logger.exception("sound profile apply failed")
                self._send_json({"error": str(e)}, status=502)
                return
            self._send_json(payload)

    return Handler


def make_server(
    target,
    *,
    profile_path: str | Path | None = None,
    library_path: str | Path | None = None,
    config_dir: str | Path | None = None,
) -> ThreadingHTTPServer:
    from ..platform import systemd

    return systemd.make_http_server(
        target,
        _make_handler(
            profile_path=profile_path
            or os.environ.get(
                "JASPER_SOUND_PROFILE_PATH",
                PROFILE_PATH,
            ),
            library_path=library_path
            or os.environ.get(
                "JASPER_SOUND_PROFILE_LIBRARY_PATH",
                PROFILE_LIBRARY_PATH,
            ),
            config_dir=config_dir
            or os.environ.get(
                "JASPER_SOUND_CONFIG_DIR",
                DEFAULT_CONFIG_DIR,
            ),
        ),
    )
