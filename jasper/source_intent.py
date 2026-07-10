# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Fixed root helper that persists /sources on/off intent (WS1 Phase 3b).

Why this exists
---------------
The non-root ``jasper-control`` restart broker deliberately CANNOT run
``org.freedesktop.systemd1.manage-unit-files`` (``systemctl enable`` /
``disable``). Hardware testing (Pi 5, systemd 257, polkit 126) found that action
is invoked by systemd with **NULL polkit details**, so ``action.lookup("unit")``
is undefined and it cannot be unit-scoped; and ``systemctl restart`` consults it,
so an unconditional grant would silently re-open restart-of-any-unit — defeating
the per-unit ``manage-units`` allowlist that is the whole point of the posture.
See ``deploy/polkit/49-jasper-control.rules`` and
``docs/HANDOFF-privilege-separation.md``.

So the ``/sources/`` wizard (non-root ``jasper-web``) cannot itself persist a
source's on/off choice across reboots — persistence *is* enable/disable, which is
``manage-unit-files``. Before this helper, the wizard's enable/disable failed
with "Interactive authentication required" and the failure was swallowed: the
POST returned 200 while nothing changed.

This module is the fixed root helper that closes that gap, mirroring
``jasper-wifi-scan-repair``: a ``Type=oneshot`` unit
(``jasper-source-intent-reconcile.service``) that ``jasper-web`` kicks through the
broker's ``START_ONLY_UNITS`` grant (``systemctl start`` — which IS
``manage-units`` and IS polkit-scoped). Running as root, it enable/disables the
source units to match the household intent the wizard recorded in the wizard-owned
env file. The oneshot's exit code is the success signal the broker relays back to
the wizard, so a failed apply surfaces as a visible error rather than a lying 200.

Security
--------
The set of units this helper may enable/disable is derived from
:mod:`jasper.local_sources` (the source *intent* units — ``shairport-sync``,
``librespot``, ``jasper-usbsink``) and enforced HERE, inside the root process. The
intent file is UNTRUSTED input: the helper only ever acts on the specific keys it
computes for its own allowlist, and any stray ``JASPER_SOURCE_INTENT_*`` key that
does not map to an allowlisted source unit is rejected loudly (logged + non-zero
exit) rather than acted on. A compromised ``jasper-web`` can therefore at most
flip an already-allowlisted source's persisted enable-state — which it can already
start/stop via the broker — so no new privileged surface is opened.
"""
from __future__ import annotations

import argparse
import logging
import re
import subprocess
from collections.abc import Callable

from jasper.env_file import parse_env_lines
from jasper.local_sources import local_source_lifecycles
from jasper.log_event import log_event

logger = logging.getLogger(__name__)

# The fixed root oneshot unit that runs this module. jasper-web kicks it via the
# broker's START_ONLY_UNITS grant (mirrors jasper-wifi-scan-repair). Keep in
# lockstep with restart_broker.START_ONLY_UNITS + the polkit rule.
RECONCILE_UNIT = "jasper-source-intent-reconcile.service"

# Wizard-owned env file the /sources/ page writes and this helper reads. Group
# `jasper`, mode 0644 (no secrets — just source unit names + enabled/disabled).
# jasper-web writes it (ReadWritePaths=/var/lib/jasper); this root helper reads
# it. Absent file / absent key = "no wizard override" = no-op (install.sh sets
# the shipped-default enabled-state; the wizard is the only override writer).
SOURCE_INTENT_ENV = "/var/lib/jasper/source_intent.env"

_INTENT_KEY_PREFIX = "JASPER_SOURCE_INTENT_"
_ENABLED = "enabled"
_DISABLED = "disabled"

# (rc, stderr) systemctl runner; injectable so tests never shell out.
SystemctlRunner = Callable[[str, bool], "tuple[int, str]"]


def intent_env_key(unit: str) -> str:
    """The ``JASPER_SOURCE_INTENT_*`` env key for a source unit.

    Sanitized to a valid env var name: ``jasper-usbsink.service`` ->
    ``JASPER_SOURCE_INTENT_JASPER_USBSINK_SERVICE``. Deterministic so the wizard
    (writer) and this helper (reader) always agree on the key.
    """
    slug = re.sub(r"[^A-Z0-9]+", "_", unit.upper()).strip("_")
    return f"{_INTENT_KEY_PREFIX}{slug}"


def source_intent_units() -> tuple[str, ...]:
    """The units this helper may enable/disable — the security allowlist.

    Derived from the source-lifecycle registry (every declared music source's
    ``intent_unit``; Bluetooth has none — it is runtime-only DBus power). Sharing
    the registry with :mod:`jasper.web.sources_setup` means the allowlist cannot
    drift from what the wizard toggles.
    """
    units = {
        lc.intent_unit
        for lc in local_source_lifecycles()
        if lc.intent_unit is not None
    }
    return tuple(sorted(units))


def _valid_keys() -> dict[str, str]:
    """Map each allowlisted intent env key -> its unit."""
    return {intent_env_key(unit): unit for unit in source_intent_units()}


def _run_systemctl(unit: str, enabled: bool) -> tuple[int, str]:
    verb = "enable" if enabled else "disable"
    try:
        proc = subprocess.run(
            ["systemctl", verb, unit],
            check=False, timeout=15,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return proc.returncode, (proc.stderr or "").strip()


def _read_intent(env_path: str) -> str:
    try:
        with open(env_path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        # Unreadable file is a real failure — do NOT silently treat as "no
        # intent" (that would let a bad byte mask a household's disable choice).
        raise RuntimeError(f"cannot read {env_path}: {exc}") from exc


def reconcile(
    *,
    env_path: str = SOURCE_INTENT_ENV,
    runner: SystemctlRunner | None = None,
) -> int:
    """Apply the persisted /sources intent. Returns a process exit code.

    Reads ``env_path``; for every allowlisted source unit whose intent key is
    present, runs ``systemctl enable``/``disable`` to match. Returns 0 iff every
    applied unit succeeded AND the file carried no rejected keys; non-zero
    otherwise so the oneshot fails and the broker relays a visible error to the
    wizard (no silent failure). Rejected (non-allowlisted or malformed) keys are
    logged and fail loud — they are never acted on.

    ``runner`` defaults to the real ``systemctl`` runner, resolved at call time
    (not bound as a default arg) so tests can monkeypatch ``_run_systemctl``.
    """
    if runner is None:
        runner = _run_systemctl
    try:
        text = _read_intent(env_path)
    except RuntimeError as exc:
        log_event(logger, "source_intent.read_failed", error=str(exc),
                  level=logging.WARNING)
        return 1

    valid = _valid_keys()
    assignments = {
        key: value.strip().strip("'\"")
        for key, value in parse_env_lines(text)
        if value is not None
    }

    failures = 0
    applied = 0
    for key, value in assignments.items():
        if not key.startswith(_INTENT_KEY_PREFIX):
            continue
        unit = valid.get(key)
        if unit is None:
            # A JASPER_SOURCE_INTENT_* key that does not map to an allowlisted
            # source unit. The wizard never writes these; treat any such key as
            # tampering / drift and refuse it loudly rather than acting on it.
            log_event(logger, "source_intent.rejected_unit", key=key,
                      level=logging.WARNING)
            failures += 1
            continue
        if value == _ENABLED:
            enabled = True
        elif value == _DISABLED:
            enabled = False
        else:
            log_event(logger, "source_intent.bad_value", unit=unit,
                      value=value, level=logging.WARNING)
            failures += 1
            continue
        rc, err = runner(unit, enabled)
        if rc != 0:
            log_event(logger, "source_intent.apply_failed", unit=unit,
                      enabled=enabled, rc=rc, detail=err[:200],
                      level=logging.WARNING)
            failures += 1
            continue
        applied += 1
        log_event(logger, "source_intent.applied", unit=unit, enabled=enabled)

    log_event(logger, "source_intent.reconciled", applied=applied,
              failures=failures)
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jasper-source-intent-reconcile",
        description=(
            "Apply persisted /sources enable/disable intent for the "
            "allowlisted local-source units (root oneshot; WS1 Phase 3b)."
        ),
    )
    parser.add_argument(
        "--env-path", default=SOURCE_INTENT_ENV,
        help="Path to the wizard-owned source intent env file.",
    )
    parser.add_argument(
        "--reason", default="",
        help="Free-text reason for logging context (e.g. 'source toggle').",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.reason:
        log_event(logger, "source_intent.begin", reason=args.reason)
    return reconcile(env_path=args.env_path)


if __name__ == "__main__":
    raise SystemExit(main())
