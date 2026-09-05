# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""``jasper-doctor`` — preflight diagnostic CLI (package entry).

This ``__init__`` is the registration imports for the per-domain check
modules, the harness that runs them, and the CLI; a check is imported
from the module that owns it.

Usage:
    sudo /opt/jasper/.venv/bin/jasper-doctor             # one shot
    sudo /opt/jasper/.venv/bin/jasper-doctor --watch     # loop, 5s
    sudo /opt/jasper/.venv/bin/jasper-doctor --watch -i 2  # loop, 2s

The doctor reads ``/etc/jasper/jasper.env`` and (if present)
``/var/lib/jasper/voice_provider.env`` itself. Exit 0 if all critical
checks pass, 1 otherwise; --watch exits 0 on Ctrl-C.

Check membership and order are owned by
:mod:`~jasper.cli.doctor._registry`."""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import os
import sys
import time
from types import SimpleNamespace
from typing import Awaitable, Callable
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
    STREAMBOX_INSTALL_PROFILE,
    install_role_for_profile,
    read_install_profile,
)
from ...speaker_name import runtime_name as _speaker_runtime_name
from ...spotify_oauth import resolved_spotify_redirect_uri
from ...usage import DEFAULT_USAGE_DB

from ._evidence import (
    DOCTOR_CHECK_TIMEOUT_SECONDS,
    DOCTOR_MAX_CONCURRENCY,
    evidence,
)
from ._registry import (
    STREAMBOX_OMITTED_DOCTOR_CHECKS,
    STREAMBOX_OMITTED_DOCTOR_MODULES,
    registered_checks,
)
from ._shared import (
    BOLD,
    CheckResult,
    DIM,
    DoctorCheck,
    GREEN,
    REASON_CHECK_TIMED_OUT,
    REASON_CONFIG_ERROR,
    REASON_DOCTOR_CRASHED,
    REASON_NOT_INSTALLED,
    RED,
    RESET,
    YELLOW,
    _check_name,
    _exception_detail,
    _run_async_doctor_check,
    _run_doctor_check,
    check_row,
    summarize,
)
from . import env as env
from . import voice as voice
from . import audio as audio
from . import boot_config as boot_config
from . import wake as wake
from . import renderers as renderers
from . import integrations as integrations
from . import privsep as privsep
from . import secret_compartments as secret_compartments
from . import web as web
from . import research as research
from . import correction as correction
from . import memory as memory
from . import drift as drift
from . import resilience as resilience
from . import aec as aec
from . import audio_runtime_fanin as audio_runtime_fanin
from . import audio_runtime_camilla as audio_runtime_camilla
from . import audio_runtime_ring as audio_runtime_ring
from . import audio_runtime_outputd as audio_runtime_outputd
from . import usbsink as usbsink
from . import network as network
from . import peering as peering
from . import grouping as grouping

def _registered_check_name(entry) -> str:
    """One naming rule for every calling convention, so a check cannot be
    named one thing on a full box and another on a streambox."""
    return entry.label or _check_name(entry.func)


def _profile_skip_result(entry, *, detail: str) -> CheckResult:
    return CheckResult(
        _registered_check_name(entry),
        "skipped",
        detail,
        reason=REASON_NOT_INSTALLED,
    )


def _doctor_skip_detail(entry, install_profile: str) -> str:
    role = install_role_for_profile(install_profile)
    if role == STREAMBOX_INSTALL_PROFILE and (
        entry.module in STREAMBOX_OMITTED_DOCTOR_MODULES
        or entry.func.__name__ in STREAMBOX_OMITTED_DOCTOR_CHECKS
    ):
        return "not installed (streambox profile)"
    return ""


def render(results: list[CheckResult]) -> int:
    print()
    print(f"{BOLD}jasper-doctor{RESET}\n")
    fails = warns = 0
    silent = False
    for r in results:
        if r.status == "ok":
            color, mark = GREEN, "✓"
        elif r.status == "skipped":
            color, mark = DIM, "-"
        else:
            silent = silent or r.speaker_silent
            if r.status == "warn":
                color, mark = YELLOW, "!"
                warns += 1
            else:
                color, mark = RED, "✗"
                fails += 1
        print(f"  {color}{mark}{RESET} {r.name:24s} {r.detail}")
    print()
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
        "error": error,
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
            "/var/lib/jasper-intsecrets/.spotify-cache",
        ),
        spotify_device_name=_speaker_runtime_name(),
        spotify_accounts_path=os.environ.get(
            "JASPER_SPOTIFY_ACCOUNTS_PATH",
            "/var/lib/jasper-intsecrets/spotify/accounts.json",
        ),
        spotify_setup_url=os.environ.get(
            "JASPER_SPOTIFY_SETUP_URL",
            f"http://{hostname}/spotify",
        ),
        spotify_enabled=bool(spotify_client_id),
    )

def _doctor_config_from_env(install_profile: str) -> Config | SimpleNamespace:
    if install_role_for_profile(install_profile) == STREAMBOX_INSTALL_PROFILE:
        return _local_audio_config_from_env()
    return Config.from_env()

async def _watch_loop(cfg: Config | SimpleNamespace, interval: float) -> int:
    """Run checks every `interval` seconds, print one line per pass.
    Returns 0 on Ctrl-C."""
    print(
        f"jasper-doctor --watch (interval={interval:.1f}s, "
        f"Ctrl-C to exit)\n",
        flush=True,
    )
    try:
        while True:
            results = await run_async(cfg)
            print(_watch_line(results), flush=True)
            await asyncio.sleep(interval)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nexiting", flush=True)
        return 0


@dataclass(frozen=True)
class _RunnableDoctorCheck:
    name: str
    check: DoctorCheck | Callable[[], Awaitable[CheckResult]]
    is_async: bool = False
    exclusive_group: str = ""


def _build_doctor_checks(
    cfg: Config | SimpleNamespace,
    install_profile: str,
) -> list[_RunnableDoctorCheck]:
    checks: list[_RunnableDoctorCheck] = []
    for entry in registered_checks():
        name = _registered_check_name(entry)
        skip_detail = _doctor_skip_detail(entry, install_profile)
        if skip_detail:
            skipped = _profile_skip_result(entry, detail=skip_detail)
            checks.append(
                _RunnableDoctorCheck(
                    name, (name, lambda skipped=skipped: skipped)
                )
            )
            continue
        fn = entry.func
        call = (
            (lambda fn=fn, cfg=cfg: fn(cfg))
            if entry.needs_cfg
            else (lambda fn=fn: fn())
        )
        checks.append(
            _RunnableDoctorCheck(
                name,
                call if entry.is_async else (name, call),
                is_async=entry.is_async,
                exclusive_group=entry.exclusive_group,
            )
        )
    return checks


async def _run_runnable_doctor_check(
    runnable: _RunnableDoctorCheck,
) -> CheckResult:
    if runnable.is_async:
        return await _run_async_doctor_check(
            runnable.name,
            runnable.check,  # type: ignore[arg-type]
        )
    return await asyncio.to_thread(
        _run_doctor_check,
        runnable.check,  # type: ignore[arg-type]
    )


async def _run_runnable_with_timeout(
    runnable: _RunnableDoctorCheck,
    timeout: float,
) -> CheckResult:
    # Outer row-level guard only: `asyncio.to_thread` cannot kill a worker
    # already inside a blocking syscall, and asyncio waits for
    # default-executor threads during shutdown. Blocking probes must stay
    # bounded by their own subprocess/socket timeouts too.
    try:
        return await asyncio.wait_for(
            _run_runnable_doctor_check(runnable),
            timeout=timeout,
        )
    except TimeoutError:
        return CheckResult(
            runnable.name,
            "fail",
            f"check timed out after {timeout:g}s",
            reason=REASON_CHECK_TIMED_OUT,
        )


async def _run_runnable_bounded(
    runnable: _RunnableDoctorCheck,
    semaphore: asyncio.Semaphore,
    exclusive_locks: dict[str, asyncio.Lock],
    timeout: float,
) -> CheckResult:
    async with semaphore:
        if runnable.exclusive_group:
            async with exclusive_locks[runnable.exclusive_group]:
                return await _run_runnable_with_timeout(runnable, timeout)
        return await _run_runnable_with_timeout(runnable, timeout)


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
        cfg = _doctor_config_from_env(install_profile)
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
        sys.exit(asyncio.run(_watch_loop(cfg, args.interval)))
    started_at = time.monotonic()
    try:
        results = asyncio.run(run_async(cfg))
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
    sys.exit(render(results))

if __name__ == "__main__":
    main()

async def run_async(
    cfg: Config | SimpleNamespace,
    *,
    max_concurrency: int = DOCTOR_MAX_CONCURRENCY,
    check_timeout: float = DOCTOR_CHECK_TIMEOUT_SECONDS,
) -> list[CheckResult]:
    """Run every registered check and return the results in registry order.

    Checks run concurrently (most are subprocess/socket/file probes) but
    results are gathered in registry order so CLI and dashboard output stay
    stable. ``exclusive_group=`` serializes hardware-sensitive probes within
    that lane while unrelated checks continue.
    """
    evidence.reset()
    evidence.set_check_timeout(check_timeout)
    install_profile = read_install_profile()
    checks = _build_doctor_checks(cfg, install_profile)
    semaphore = asyncio.Semaphore(max_concurrency)
    exclusive_locks = {
        c.exclusive_group: asyncio.Lock()
        for c in checks
        if c.exclusive_group
    }
    return list(await asyncio.gather(*[
        _run_runnable_bounded(c, semaphore, exclusive_locks, check_timeout)
        for c in checks
    ]))
