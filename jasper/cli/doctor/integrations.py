# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-doctor checks — integrations domain."""
from __future__ import annotations

import os

from ...config import Config
from ._registry import doctor_check
from ._shared import CheckResult, _exception_detail

# Machine-stable codes naming which branch of an integrations check produced
# a result (AGENTS.md: tests pin status + reason, never detail prose).
REASON_GOOGLE_TOKENS_NOT_CONFIGURED = "google_tokens_not_configured"
REASON_GOOGLE_TOKENS_IMPORT_FAILED = "google_tokens_import_failed"
REASON_GOOGLE_TOKENS_NO_ACCOUNTS = "google_tokens_no_accounts_linked"
REASON_GOOGLE_TOKENS_REFRESH_FAILED = "google_tokens_refresh_failed"

REASON_GOOGLE_ROUTES_NOT_CONFIGURED = "google_routes_not_configured"
REASON_GOOGLE_ROUTES_MISSING_SETUP = "google_routes_missing_setup"
REASON_GOOGLE_ROUTES_INVALID_MODE = "google_routes_invalid_travel_mode"
REASON_GOOGLE_ROUTES_CONFIGURED = "google_routes_configured"

REASON_HOME_ASSISTANT_NOT_CONFIGURED = "home_assistant_not_configured"
REASON_HOME_ASSISTANT_IMPORT_FAILED = "home_assistant_import_failed"
REASON_HOME_ASSISTANT_PROBE_RAISED = "home_assistant_probe_raised"
REASON_HOME_ASSISTANT_UNREACHABLE = "home_assistant_unreachable"
REASON_HOME_ASSISTANT_CONNECTED = "home_assistant_connected"

REASON_CITIBIKE_NOT_CONFIGURED = "citibike_not_configured"
REASON_CITIBIKE_IMPORT_FAILED = "citibike_import_failed"
REASON_CITIBIKE_GBFS_UNREACHABLE = "citibike_gbfs_unreachable"
REASON_CITIBIKE_STATIONS_RETIRED = "citibike_stations_retired"
REASON_CITIBIKE_CONNECTED = "citibike_connected"
REASON_CITIBIKE_CONNECTED_EBIKE_ONLY = "citibike_connected_ebike_only"

@doctor_check(label="Google OAuth", needs_cfg=True)
def check_google_tokens(cfg: Config) -> CheckResult:
    """Verify Google OAuth state is healthy.

    Three states matter:
      - CLIENT_ID/SECRET not set → ok (operator intent: tools off)
      - CLIENT_ID/SECRET set but no accounts linked → warn (wizard
        needs visiting; Calendar/Gmail tools are silently unregistered)
      - At least one account fails to refresh → warn (likely revoked
        or password-changed; user needs to re-link)
    """
    label = "Google OAuth"
    if not cfg.google_enabled:
        return CheckResult(
            label, "ok",
            f"not configured — visit {cfg.google_setup_url} "
            f"to enable Calendar + Gmail tools",
            reason=REASON_GOOGLE_TOKENS_NOT_CONFIGURED,
        )
    try:
        from ...google_creds import GoogleRegistry, valid_access_token
    except ImportError as e:
        return CheckResult(
            label, "warn",
            f"google-auth import failed: {e}. Re-run install.sh.",
            reason=REASON_GOOGLE_TOKENS_IMPORT_FAILED,
        )
    registry = GoogleRegistry.load(cfg.google_accounts_path)
    if not registry.accounts:
        return CheckResult(
            label, "warn",
            f"CLIENT_ID/SECRET set but no accounts linked. Visit "
            f"{cfg.google_setup_url} to link a household member's "
            f"Calendar + Gmail.",
            reason=REASON_GOOGLE_TOKENS_NO_ACCOUNTS,
        )
    healthy: list[str] = []
    broken: list[str] = []
    for a in registry.accounts:
        token = valid_access_token(
            a,
            client_id=cfg.google_client_id,
            client_secret=cfg.google_client_secret,
        )
        if token:
            healthy.append(a.name)
        else:
            broken.append(a.name)
    if broken:
        return CheckResult(
            label, "warn",
            f"refresh failed for {broken}; healthy: {healthy or 'none'}. "
            f"Re-link the broken account(s) at {cfg.google_setup_url}.",
            reason=REASON_GOOGLE_TOKENS_REFRESH_FAILED,
        )
    return CheckResult(
        label, "ok",
        f"{len(healthy)} account(s) refreshed: {', '.join(healthy)}",
    )

@doctor_check(label="Google Routes", needs_cfg=True)
def check_google_routes(cfg: Config) -> CheckResult:
    """Verify Google Routes configuration without making a billable API call."""
    from ... import google_routes

    label = "Google Routes"
    status = google_routes.config_status(os.environ)
    setup_url = f"http://{cfg.hostname}/transit"
    if not status.api_key_present and not status.origin_present:
        return CheckResult(
            label,
            "ok",
            f"not configured — visit {setup_url} to enable travel time",
            reason=REASON_GOOGLE_ROUTES_NOT_CONFIGURED,
        )
    problems: list[str] = []
    if not status.origin_present:
        problems.append("saved speaker location is missing")
    if not status.api_key_present:
        problems.append("GOOGLE_ROUTES_API_KEY is missing")
    if problems:
        return CheckResult(
            label,
            "warn",
            f"{'; '.join(problems)}. Visit {setup_url} to finish setup.",
            reason=REASON_GOOGLE_ROUTES_MISSING_SETUP,
        )
    if not status.default_mode_valid:
        return CheckResult(
            label,
            "warn",
            "configured, but JASPER_TRAVEL_DEFAULT_MODE is invalid; runtime "
            f"falls back to {google_routes.DEFAULT_TRAVEL_MODE}. Fix it at "
            f"{setup_url}.",
            reason=REASON_GOOGLE_ROUTES_INVALID_MODE,
        )
    return CheckResult(
        label,
        "ok",
        f"configured for {status.default_mode}; live API probe skipped to avoid "
        "spending Routes quota. Restrict the key to the Google Routes API.",
        reason=REASON_GOOGLE_ROUTES_CONFIGURED,
    )


@doctor_check(label="Home Assistant", needs_cfg=True)
def check_home_assistant(cfg: Config) -> CheckResult:
    """Verify Home Assistant connectivity for the home_assistant voice tool.

    Three states matter:
      - URL or token not set → ok. Operator intent; the tool is gated on both.
      - Both set, but GET /api/ fails (network, auth, 5xx) → warn with a hint
        pointing at the setup wizard. An integration is not the speaker.
      - Both set, GET /api/ succeeds → ok with the instance name + version.
    """
    import asyncio as _asyncio

    label = "Home Assistant"
    setup_url = f"http://{cfg.hostname}/ha"
    if not cfg.ha_enabled:
        return CheckResult(
            label, "ok",
            f"not configured — visit {setup_url} to enable smart-home control",
            reason=REASON_HOME_ASSISTANT_NOT_CONFIGURED,
        )
    try:
        from ...home_assistant import probe_status
    except ImportError as e:
        return CheckResult(
            label, "warn", f"home_assistant import failed: {e}",
            reason=REASON_HOME_ASSISTANT_IMPORT_FAILED,
        )
    try:
        # force=True bypasses probe_status's 15 s cache: the doctor is an
        # ad-hoc diagnostic, not a polling consumer, and its operator expects
        # fresh ground truth.
        result = _asyncio.run(probe_status(
            cfg.ha_url, cfg.ha_token,
            force=True,
            verify_ssl=cfg.ha_verify_ssl,
        ))
    except Exception as e:  # noqa: BLE001
        return CheckResult(
            label, "warn", f"probe raised: {_exception_detail(e)}",
            reason=REASON_HOME_ASSISTANT_PROBE_RAISED,
        )
    if not result.get("connected"):
        return CheckResult(
            label, "warn",
            f"configured but unreachable at {result.get('url') or cfg.ha_url}: "
            f"{result.get('error') or 'unknown error'}. Re-check the URL "
            f"and token at {setup_url}.",
            reason=REASON_HOME_ASSISTANT_UNREACHABLE,
        )
    name = result.get("instance_name") or "Home Assistant"
    version = result.get("version") or "?"
    return CheckResult(
        label, "ok",
        f"connected to {name} ({version}) at {result.get('url')}",
        reason=REASON_HOME_ASSISTANT_CONNECTED,
    )

@doctor_check(label="Citi Bike", needs_cfg=True)
def check_citibike(cfg: Config) -> CheckResult:
    """Verify Citi Bike GBFS reachability + saved-station resolution.

    Four states:
      - No saved stations → ok. Operator intent; the tool is not registered.
      - Saved stations, GBFS unreachable → warn. Tool will degrade to
        cached / error responses at runtime.
      - Saved stations, GBFS responsive, all saved IDs present in
        the current station_information.json → ok with the count
        (and an "(e-bike-only mode)" suffix when the global flag is
        set).
      - Saved stations, GBFS responsive, one or more saved IDs
        missing → ok with the affected labels. Lyft periodically
        retires stations; the user has to re-pick at /transit/.
    """
    label = "Citi Bike"
    setup_url = f"http://{cfg.hostname}/transit"
    # Transit config does not ride typed Config fields: read the wizard's
    # SSOT env directly, with the same parser the provider uses.
    from ...citibike import parse_saved_stations
    saved = list(parse_saved_stations(os.environ.get("JASPER_CITIBIKE_STATIONS", "")))
    if not saved:
        return CheckResult(
            label, "ok",
            f"not configured — visit {setup_url} to enable",
            reason=REASON_CITIBIKE_NOT_CONFIGURED,
        )
    try:
        from ...citibike import (
            INFO_TTL_SECONDS,
            STATION_INFO_URL,
            fetch_feed,
        )
    except ImportError as e:
        return CheckResult(
            label, "warn", f"citibike module import failed: {e}",
            reason=REASON_CITIBIKE_IMPORT_FAILED,
        )
    try:
        info = fetch_feed(STATION_INFO_URL, INFO_TTL_SECONDS)
    except Exception as e:  # noqa: BLE001
        return CheckResult(
            label, "warn",
            f"GBFS unreachable: {e}. Saved-station drift cannot be "
            f"validated; voice tool will degrade to cached data or "
            f"return {{error}} at runtime.",
            reason=REASON_CITIBIKE_GBFS_UNREACHABLE,
        )
    known_ids = {
        s.get("station_id")
        for s in (info.get("data") or {}).get("stations", [])
        if isinstance(s, dict)
    }
    missing = [(sid, lab) for sid, lab in saved if sid not in known_ids]
    if missing:
        names = ", ".join(lab for _, lab in missing[:3])
        suffix = "" if len(missing) <= 3 else f" (+{len(missing) - 3} more)"
        return CheckResult(
            label, "ok",
            f"{len(missing)}/{len(saved)} saved station(s) no longer in "
            f"GBFS — Lyft retired them: {names}{suffix}. "
            f"Re-pick at {setup_url}.",
            reason=REASON_CITIBIKE_STATIONS_RETIRED,
        )
    ebike_only = (
        os.environ.get("JASPER_CITIBIKE_EBIKE_ONLY", "").strip().lower()
        in {"1", "true", "yes"}
    )
    extra = " (e-bike-only mode)" if ebike_only else ""
    return CheckResult(
        label, "ok",
        f"connected — {len(saved)} saved station"
        f"{'s' if len(saved) != 1 else ''}{extra}",
        reason=(
            REASON_CITIBIKE_CONNECTED_EBIKE_ONLY if ebike_only
            else REASON_CITIBIKE_CONNECTED
        ),
    )
