# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Runs the registered jasper-doctor checks — profile skips, bounded
concurrency, exclusive lanes and the per-row timeout — and returns the
results in registry order.

Check membership and order are owned by
:mod:`~jasper.cli.doctor._registry`."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from functools import partial
from types import SimpleNamespace
from typing import Awaitable, Callable

from ...config import Config
from ...install_profile import (
    is_streambox_install_profile,
    read_install_profile,
)

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
    CheckResult,
    DoctorCheck,
    REASON_CHECK_TIMED_OUT,
    REASON_NOT_INSTALLED,
    _check_name,
    _run_async_doctor_check,
    _run_doctor_check,
)


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
    if is_streambox_install_profile(install_profile) and (
        entry.module in STREAMBOX_OMITTED_DOCTOR_MODULES
        or entry.func.__name__ in STREAMBOX_OMITTED_DOCTOR_CHECKS
    ):
        return "not installed (streambox profile)"
    return ""


@dataclass(frozen=True)
class _RunnableDoctorCheck:
    name: str
    check: DoctorCheck | Callable[[], Awaitable[CheckResult]]
    is_async: bool = False
    exclusive_group: str = ""


def _already_decided(result: CheckResult) -> CheckResult:
    return result


def _build_doctor_checks(
    cfg: Config | SimpleNamespace,
    install_profile: str,
    *,
    core_only: bool = False,
    modules: frozenset[str] | None = None,
) -> list[_RunnableDoctorCheck]:
    checks: list[_RunnableDoctorCheck] = []
    for entry in registered_checks(core_only=core_only, modules=modules):
        name = _registered_check_name(entry)
        skip_detail = _doctor_skip_detail(entry, install_profile)
        if skip_detail:
            skipped = _profile_skip_result(entry, detail=skip_detail)
            checks.append(
                _RunnableDoctorCheck(
                    name, (name, partial(_already_decided, skipped))
                )
            )
            continue
        call = partial(entry.func, cfg) if entry.needs_cfg else entry.func
        checks.append(
            _RunnableDoctorCheck(
                name,
                call if entry.is_async else (name, call),  # type: ignore[arg-type]
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


async def run_async(
    cfg: Config | SimpleNamespace,
    *,
    core_only: bool = False,
    modules: frozenset[str] | None = None,
    max_concurrency: int = DOCTOR_MAX_CONCURRENCY,
    check_timeout: float = DOCTOR_CHECK_TIMEOUT_SECONDS,
) -> list[CheckResult]:
    """Run every registered check and return the results in registry order.

    Checks run concurrently (most are subprocess/socket/file probes) but
    results are gathered in registry order so CLI and dashboard output stay
    stable. ``exclusive_group=`` serializes hardware-sensitive probes within
    that lane while unrelated checks continue. ``modules``, when given,
    restricts the run to that module set (see ``registered_checks``).
    """
    evidence.reset()
    evidence.set_check_timeout(check_timeout)
    install_profile = read_install_profile()
    checks = _build_doctor_checks(
        cfg, install_profile, core_only=core_only, modules=modules,
    )
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
