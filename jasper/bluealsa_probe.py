# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared backoff for BlueALSA CLI probes.

`bluealsa-cli list-pcms` is a useful read-only way to discover whether a
Bluetooth A2DP sink is active, but when the caller lacks D-Bus permission the
system bus logs a rejection for every subprocess. A short process-local
negative cache keeps failed probes from becoming a journal flood while still
recovering quickly after BlueALSA or policy is fixed.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

BLUEALSA_PROBE_FAILURE_TTL_SEC = 60.0
_ACTIVE_TRANSPORT_PATH_RE = re.compile(
    rb"(/org/bluealsa/hci\d+/dev_[A-F0-9_]+/a2dpsnk/source)"
)

_suppressed_until = 0.0
_last_failure = ""


def probe_suppressed() -> bool:
    return time.monotonic() < _suppressed_until


def suppression_remaining_sec() -> float:
    return max(0.0, _suppressed_until - time.monotonic())


def last_failure() -> str:
    return _last_failure


def note_probe_failure(reason: str, logger: logging.Logger) -> None:
    global _last_failure, _suppressed_until
    _last_failure = reason
    _suppressed_until = time.monotonic() + BLUEALSA_PROBE_FAILURE_TTL_SEC
    logger.debug(
        "bluealsa-cli list-pcms failed (%s); suppressing probes for %.0fs",
        reason, BLUEALSA_PROBE_FAILURE_TTL_SEC,
    )


def note_probe_success() -> None:
    global _last_failure, _suppressed_until
    _last_failure = ""
    _suppressed_until = 0.0


def _reset_for_tests() -> None:
    note_probe_success()


async def list_pcms(logger: logging.Logger) -> bytes | None:
    """Return `bluealsa-cli list-pcms` stdout, or None on any probe failure."""
    if probe_suppressed():
        logger.debug(
            "bluealsa-cli list-pcms probe suppressed for %.1fs after %s",
            suppression_remaining_sec(),
            last_failure() or "previous failure",
        )
        return None

    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "bluealsa-cli", "list-pcms",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        # asyncio.timeout(), NOT asyncio.wait_for(): see the note on
        # RendererClient.selected_source (jasper/renderer.py). This probe is
        # one call earlier on the same directly-awaited chain out of
        # VolumeObserver._run that _busctl_set_property sits on -- _tick ->
        # apply_active_source_transition -> _set_push_source_for_handoff ->
        # _set_bluetooth -> here (jasper/volume_coordinator.py) -- so leaving
        # it on wait_for would keep that cancellation-only loop immortal even
        # with the set-property call fixed (#2003). Do not "simplify" this
        # back to wait_for while 3.11 is supported.
        async with asyncio.timeout(2.0):
            stdout, _ = await proc.communicate()
    except FileNotFoundError:
        note_probe_failure("FileNotFoundError", logger)
        return None
    except asyncio.TimeoutError:
        if proc is not None:
            try:
                proc.kill()
                await proc.wait()
            except (ProcessLookupError, OSError, AttributeError):
                pass
        note_probe_failure("TimeoutError", logger)
        return None

    if proc.returncode != 0:
        note_probe_failure(f"rc={proc.returncode}", logger)
        return None
    note_probe_success()
    return stdout


async def active_transport_path(logger: logging.Logger) -> str | None:
    """Return the first active A2DP-sink transport reported by BlueALSA."""

    stdout = await list_pcms(logger)
    if stdout is None:
        return None
    match = _ACTIVE_TRANSPORT_PATH_RE.search(stdout)
    return match.group(1).decode("ascii") if match else None
