"""jasper-doctor checks — integrations domain.

Re-homed verbatim from the original monolithic
``jasper/cli/doctor.py``; see ``jasper/cli/doctor/__init__.py``
for the package overview and ``_registry.py`` for how order is
preserved. No check logic changed in the split."""
from __future__ import annotations

from ...config import Config
from ._registry import doctor_check
from ._shared import CheckResult

@doctor_check(order=17, group="integrations", label="Google OAuth", needs_cfg=True)
def check_google_tokens(cfg: Config) -> CheckResult:
    """Verify Google OAuth state is healthy.

    Three states matter:
      - CLIENT_ID/SECRET not set → ok (skipped, not enabled)
      - CLIENT_ID/SECRET set but no accounts linked → warn (wizard
        needs visiting; Calendar/Gmail tools are silently unregistered)
      - At least one account fails to refresh → warn (likely revoked
        or password-changed; user needs to re-link)
    """
    label = "Google OAuth"
    if not cfg.google_enabled:
        return CheckResult(
            label, "ok",
            f"not configured (skipped — visit {cfg.google_setup_url} "
            f"to enable Calendar + Gmail tools)",
        )
    try:
        from ...google_creds import GoogleRegistry, valid_access_token
    except ImportError as e:
        return CheckResult(
            label, "fail",
            f"google-auth import failed: {e}. Re-run install.sh.",
        )
    registry = GoogleRegistry.load(cfg.google_accounts_path)
    if not registry.accounts:
        return CheckResult(
            label, "warn",
            f"CLIENT_ID/SECRET set but no accounts linked. Visit "
            f"{cfg.google_setup_url} to link a household member's "
            f"Calendar + Gmail.",
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
        )
    return CheckResult(
        label, "ok",
        f"{len(healthy)} account(s) refreshed: {', '.join(healthy)}",
    )

@doctor_check(order=18, group="integrations", label="Home Assistant", needs_cfg=True)
def check_home_assistant(cfg: Config) -> CheckResult:
    """Verify Home Assistant connectivity for the home_assistant voice tool.

    Three states matter:
      - URL or token not set → ok (skipped, not enabled). The home_assistant
        tool is gated on both being present.
      - Both set, but GET /api/ fails (network, auth, 5xx) → fail with an
        actionable hint pointing at the setup wizard.
      - Both set, GET /api/ succeeds → ok with the instance name + version.

    Mirrors the skip-if-not-configured pattern of check_google_tokens.
    Synchronous wrapper around the async probe so it slots into run_async's
    sync-check list without restructuring.
    """
    import asyncio as _asyncio

    label = "Home Assistant"
    setup_url = f"http://{cfg.hostname}/ha"
    if not cfg.ha_enabled:
        return CheckResult(
            label, "ok",
            f"not configured (skipped — visit {setup_url} to enable "
            f"smart-home control)",
        )
    try:
        from ...home_assistant import probe_status
    except ImportError as e:
        return CheckResult(label, "fail", f"home_assistant import failed: {e}")
    try:
        # force=True bypasses probe_status's 15s cache — the doctor is
        # an ad-hoc diagnostic, not a polling consumer, and the user
        # running `jasper-doctor` expects fresh ground truth.
        result = _asyncio.run(probe_status(
            cfg.ha_url, cfg.ha_token,
            force=True,
            verify_ssl=bool(getattr(cfg, "ha_verify_ssl", True)),
        ))
    except Exception as e:  # noqa: BLE001
        return CheckResult(label, "fail", f"probe raised: {e}")
    if not result.get("connected"):
        return CheckResult(
            label, "fail",
            f"configured but unreachable at {result.get('url') or cfg.ha_url}: "
            f"{result.get('error') or 'unknown error'}. Re-check the URL "
            f"and token at {setup_url}.",
        )
    name = result.get("instance_name") or "Home Assistant"
    version = result.get("version") or "?"
    return CheckResult(
        label, "ok",
        f"connected to {name} ({version}) at {result.get('url')}",
    )

@doctor_check(order=19, group="integrations", label="Citi Bike", needs_cfg=True)
def check_citibike(cfg: Config) -> CheckResult:
    """Verify Citi Bike GBFS reachability + saved-station resolution.

    Four states (mirrors `check_home_assistant`'s skip-if-not-
    configured pattern):
      - No saved stations → ok (skipped). Tool isn't registered.
      - Saved stations, GBFS unreachable → fail. Tool will degrade to
        cached / error responses at runtime.
      - Saved stations, GBFS responsive, all saved IDs present in
        the current station_information.json → ok with the count
        (and an "(e-bike-only mode)" suffix when the global flag is
        set).
      - Saved stations, GBFS responsive, one or more saved IDs
        missing → warn with the affected labels. Lyft periodically
        retires stations; the user has to re-pick at /transit/.
    """
    label = "Citi Bike"
    setup_url = f"http://{cfg.hostname}/transit"
    if not cfg.citibike_enabled:
        return CheckResult(
            label, "ok",
            f"not configured (skipped — visit {setup_url} to enable)",
        )
    try:
        from ...citibike import (
            INFO_TTL_SECONDS,
            STATION_INFO_URL,
            fetch_feed,
        )
    except ImportError as e:
        return CheckResult(label, "fail", f"citibike module import failed: {e}")
    try:
        info = fetch_feed(STATION_INFO_URL, INFO_TTL_SECONDS)
    except Exception as e:  # noqa: BLE001
        return CheckResult(
            label, "fail",
            f"GBFS unreachable: {e}. Saved-station drift cannot be "
            f"validated; voice tool will degrade to cached data or "
            f"return {{error}} at runtime.",
        )
    known_ids = {
        s.get("station_id")
        for s in (info.get("data") or {}).get("stations", [])
        if isinstance(s, dict)
    }
    saved = list(cfg.citibike_stations)
    missing = [(sid, lab) for sid, lab in saved if sid not in known_ids]
    if missing:
        names = ", ".join(lab for _, lab in missing[:3])
        suffix = "" if len(missing) <= 3 else f" (+{len(missing) - 3} more)"
        return CheckResult(
            label, "warn",
            f"{len(missing)}/{len(saved)} saved station(s) no longer in "
            f"GBFS — Lyft retired them: {names}{suffix}. "
            f"Re-pick at {setup_url}.",
        )
    extra = " (e-bike-only mode)" if cfg.citibike_ebike_only else ""
    return CheckResult(
        label, "ok",
        f"connected — {len(saved)} saved station"
        f"{'s' if len(saved) != 1 else ''}{extra}",
    )
