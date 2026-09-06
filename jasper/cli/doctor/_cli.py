# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""``jasper-doctor``'s operator surface: argument parsing, the cfg the checks
run against, the ANSI report and the JSON report the /system dashboard reads.

Usage:
    sudo /opt/jasper/.venv/bin/jasper-doctor             # one shot
    sudo /opt/jasper/.venv/bin/jasper-doctor --core      # post-deploy subset
    sudo /opt/jasper/.venv/bin/jasper-doctor --watch     # loop, 5s
    sudo /opt/jasper/.venv/bin/jasper-doctor --watch -i 2  # loop, 2s

The doctor reads ``/etc/jasper/jasper.env`` and (if present)
``/var/lib/jasper/voice_provider.env`` itself. Exit 0 if all critical
checks pass, 1 otherwise; --watch exits 0 on Ctrl-C."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from types import SimpleNamespace

from ...accounts import DEFAULT_REGISTRY_PATH, LEGACY_CACHE_PATH
from ...camilla_config_contract import DEFAULT_CAMILLA_PORT
from ...librespot_state import DEFAULT_PATH as DEFAULT_LIBRESPOT_STATE
from ...volume_persistence import configured_path as volume_state_path
from ...config import Config
from ...env_load import (
    bounded_env_int,
    load_env_files as _load_env_files,
)
from ...identity import resolve_hostname
from ...install_profile import (
    is_streambox_install_profile,
    read_install_profile,
)
from ...log_event import render_logfmt
from ...secret_redaction import redact_secrets
from ...speaker_name import runtime_name as _speaker_runtime_name
from ...spotify_oauth import resolved_spotify_redirect_uri
from ...usage import DEFAULT_USAGE_DB

from ._harness import run_async
from ._shared import (
    BOLD,
    CheckResult,
    DIM,
    GREEN,
    REASON_CONFIG_ERROR,
    REASON_DOCTOR_CRASHED,
    RED,
    RESET,
    YELLOW,
    _exception_detail,
    check_row,
    summarize,
)


def render(results: list[CheckResult], *, core: bool = False) -> int:
    print()
    print(f"{BOLD}jasper-doctor{RESET}\n")
    for r in results:
        if r.status == "ok":
            color, mark = GREEN, "✓"
        elif r.status == "skipped":
            color, mark = DIM, "-"
        elif r.status == "warn":
            color, mark = YELLOW, "!"
        else:
            color, mark = RED, "✗"
        print(f"  {color}{mark}{RESET} {r.name:24s} {r.detail}")
    print()
    counts = summarize(results)
    fails, warns, silent = (
        counts["fails"], counts["warns"], counts["speaker_silent"],
    )
    if core:
        # The deploy gates on the exit code; this is the journal's copy.
        print(render_logfmt("deploy.health", {
            "status": "fail" if fails else "ok",
            "fail": fails,
            "warn": warns,
            "rows": len(results),
            "speaker_silent": silent,
        }))
    # Silence leads the summary line, in the same phrase for a warn and a
    # fail: the household outcome is the same either way. Severity rides the
    # WORDS — colour and exit code keep their single meaning (red / 1 =
    # something is broken), so a parked box stays deployable (#2145).
    lead = "the speaker is silent — " if silent else ""
    if fails:
        print(f"{RED}{lead}{fails} failed, {warns} warning(s).{RESET}")
        return 1
    if warns:
        tail = "" if silent else " — non-critical"
        print(f"{YELLOW}{lead}{warns} warning(s){tail}.{RESET}")
        return 0
    print(f"{GREEN}all checks passed.{RESET}")
    return 0

def _json_payload(
    results: list[CheckResult],
    *,
    duration_sec: float | None = None,
) -> dict:
    """The flat /system-dashboard schema — one row per check."""
    payload = {
        **summarize(results),
        "generated_at_epoch": time.time(),
        "results": [check_row(r) for r in results],
    }
    if duration_sec is not None:
        payload["duration_sec"] = round(duration_sec, 3)
    return payload


def _error_payload(error: str, *, detail: str, reason: str) -> dict:
    """One row, shaped like every row `_json_payload` emits, for a run that
    produced no results at all (config error, doctor crash)."""
    return {
        "error": redact_secrets(error),
        **_json_payload([CheckResult("jasper-doctor", "fail", detail, reason=reason)]),
    }


def _emit_json(payload: dict, out_path: str | None) -> None:
    """Emit the JSON report to stdout, or atomically to ``out_path``.

    Mode 0640 so the `jasper` group (jasper-control's primary group; the
    root ``jasper-doctor-json.service`` oneshot that writes here runs
    root:jasper) can read the report without it being world-readable."""
    import json as _json
    text = _json.dumps(payload)
    if out_path is None:
        print(text)
        return
    from jasper.atomic_io import atomic_write_text
    atomic_write_text(out_path, text + "\n", mode=0o640)


def render_json(
    results: list[CheckResult],
    out_path: str | None = None,
    *,
    duration_sec: float | None = None,
) -> int:
    """Machine-readable output for the /system dashboard, fetched via
    /system/diagnostics → jasper-control.

    Returns text-render exit semantics (0 = ok/warn only; 1 = a fail) on the
    stdout path. With ``out_path`` the report lands in the file and this
    returns 0 — the file carries the pass/fail, and a non-zero exit would
    needlessly flip the writing oneshot to ``failed``."""
    payload = _json_payload(results, duration_sec=duration_sec)
    _emit_json(payload, out_path)
    if out_path is not None:
        return 0
    return 1 if payload["fails"] else 0

def _watch_line(results: list[CheckResult]) -> str:
    """One-line summary for --watch mode: timestamp, counts, first non-ok
    name."""
    fails = [r for r in results if r.status == "fail"]
    warns = [r for r in results if r.status == "warn"]
    ts = time.strftime("%H:%M:%S")
    if fails:
        first = fails[0].name
        return (
            f"{ts}  {RED}{len(fails)} fail{RESET} "
            f"{YELLOW}{len(warns)} warn{RESET}  first-fail: {first}"
        )
    if warns:
        first = warns[0].name
        return (
            f"{ts}  {GREEN}ok{RESET} "
            f"{YELLOW}{len(warns)} warn{RESET}  first-warn: {first}"
        )
    return f"{ts}  {GREEN}all {len(results)} checks ok{RESET}"

def _local_audio_config_from_env() -> SimpleNamespace:
    """Cfg surface for profiles that run local audio without a voice brain.

    Streambox installs deliberately require no voice provider, so this
    namespace carries only the attributes the retained checks read rather
    than the full voice ``Config``.
    """
    hostname = resolve_hostname()
    spotify_client_id = os.environ.get("SPOTIFY_CLIENT_ID", "")
    return SimpleNamespace(
        usage_db=os.environ.get("JASPER_USAGE_DB", DEFAULT_USAGE_DB),
        camilla_host=os.environ.get("JASPER_CAMILLA_HOST", "127.0.0.1"),
        camilla_port=bounded_env_int(
            "JASPER_CAMILLA_PORT", DEFAULT_CAMILLA_PORT, lo=1, hi=65535,
        ),
        volume_state_path=volume_state_path(),
        librespot_state_path=os.environ.get(
            "JASPER_LIBRESPOT_STATE", DEFAULT_LIBRESPOT_STATE,
        ),
        spotify_client_id=spotify_client_id,
        spotify_redirect_uri=resolved_spotify_redirect_uri(),
        spotify_cache_path=os.environ.get(
            "SPOTIFY_CACHE_PATH",
            LEGACY_CACHE_PATH,
        ),
        spotify_device_name=_speaker_runtime_name(),
        spotify_accounts_path=os.environ.get(
            "JASPER_SPOTIFY_ACCOUNTS_PATH",
            DEFAULT_REGISTRY_PATH,
        ),
        spotify_setup_url=os.environ.get(
            "JASPER_SPOTIFY_SETUP_URL",
            f"http://{hostname}/spotify",
        ),
        spotify_enabled=bool(spotify_client_id),
    )

def _doctor_config_from_env(install_profile: str) -> Config | SimpleNamespace:
    if is_streambox_install_profile(install_profile):
        return _local_audio_config_from_env()
    return Config.from_env()

async def _watch_loop(
    cfg: Config | SimpleNamespace, interval: float, *, core_only: bool = False,
) -> int:
    """Run checks every `interval` seconds, print one line per pass.
    Returns 0 on Ctrl-C."""
    print(
        f"jasper-doctor --watch (interval={interval:.1f}s, "
        f"Ctrl-C to exit)\n",
        flush=True,
    )
    try:
        while True:
            results = await run_async(cfg, core_only=core_only)
            print(_watch_line(results), flush=True)
            await asyncio.sleep(interval)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nexiting", flush=True)
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="jasper-doctor",
        description="JTS preflight diagnostics. Run as root.",
    )
    parser.add_argument(
        "--watch", action="store_true",
        help="Loop the checks until Ctrl-C; one summary line per pass.",
    )
    parser.add_argument(
        "-i", "--interval", type=float, default=5.0,
        help="Seconds between iterations in --watch mode (default 5).",
    )
    parser.add_argument(
        "--core", action="store_true",
        help="Run only the post-deploy core subset: required units, the "
             "audio-path daemons and their parks, renderer ALSA devices and "
             "the management surface. Skips the modules the subset does not "
             "need, so it is cheap on a low-memory box. Works with --json.",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit JSON on stdout instead of the ANSI report. Used by "
             "the /system dashboard's diagnostics disclosure.",
    )
    parser.add_argument(
        "--out", metavar="PATH", default=None,
        help="With --json, write the report atomically to PATH (0640) instead "
             "of stdout, then exit 0. The root jasper-doctor-json.service "
             "oneshot uses this so the non-root jasper-control can serve a "
             "root-fidelity report at /system/diagnostics (WS1 Phase 3b-2).",
    )
    args = parser.parse_args()
    # --out implies --json so the capture oneshot can pass either
    # `--json --out PATH` or a bare `--out PATH`.
    if args.out:
        args.json = True
    _load_env_files()
    try:
        install_profile = read_install_profile()
        # --core needs no cfg, and Config.from_env() fails a provider-less box.
        cfg: Config | SimpleNamespace = (
            SimpleNamespace() if args.core
            else _doctor_config_from_env(install_profile)
        )
    except (RuntimeError, ValueError) as e:
        if args.json:
            _emit_json(
                _error_payload(
                    f"config: {e}",
                    detail=_exception_detail(e),
                    reason=REASON_CONFIG_ERROR,
                ),
                args.out,
            )
            sys.exit(0 if args.out else 1)
        print(f"{RED}config error: {e}{RESET}", file=sys.stderr)
        sys.exit(1)
    if args.watch:
        sys.exit(asyncio.run(
            _watch_loop(cfg, args.interval, core_only=args.core)
        ))
    started_at = time.monotonic()
    try:
        results = asyncio.run(run_async(cfg, core_only=args.core))
    except Exception as e:  # noqa: BLE001
        if args.json:
            detail = _exception_detail(e)
            _emit_json(
                _error_payload(
                    f"doctor crashed: {detail}",
                    detail=detail,
                    reason=REASON_DOCTOR_CRASHED,
                ),
                args.out,
            )
            sys.exit(0 if args.out else 1)
        raise
    if args.json:
        sys.exit(render_json(
            results,
            out_path=args.out,
            duration_sec=time.monotonic() - started_at,
        ))
    sys.exit(render(results, core=args.core))
