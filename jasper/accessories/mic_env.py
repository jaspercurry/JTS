# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Accessory push-to-talk mic sources — the published fact and its format.

``jasper-accessory-reconcile`` is the single *writer* of
``/var/lib/jasper/accessory-mics.env``: it publishes one
``JASPER_MANUAL_MIC_SOURCES=<id>=<device>[,<id>=<device>...]`` line while a
mic-bearing accessory profile is paired, and removes the file when none is.
``jasper-voice`` consumes it as an ``EnvironmentFile=`` and parses it through
``Config.manual_mic_sources``.

This module exists because a *second* reader appeared: the voice-input start
gate. "Is there usable voice input on this box?" is an OR across two
independently-owned facts —

* a locally-attached microphone, owned by ``jasper-aec-reconcile``, and
* a paired accessory microphone, owned by ``jasper-accessory-reconcile``.

The gate marker (``jasper.voice.input_presence``) is the AND of their absences,
so the reconciler that writes it and the status surfaces that read it both need
the accessory half. Rather than let each of them re-derive the file path and the
entry format, the writer and every reader share this module —
``deploy/bin/jasper-aec-reconcile`` (bash) included: it shells out to this
module's ``python -m`` entry point instead of parsing the file in shell, the
same posture ``observe_mic_profile_state`` already uses for the mic-profile
resolver and ``grouping_voice_parked`` uses for bond validity.

Note the asymmetry with BlueZ: nothing here looks at pairing state. The
published env file *is* the accessory verdict, already validated by its owner.
Readers must not re-derive it from D-Bus.

**Strictness is deliberate and fail-closed.** ``Config.from_env`` *raises* on a
malformed ``JASPER_MANUAL_MIC_SOURCES`` entry, and that exception is not one of
the clean-park exits — a hand-corrupted file would crash-loop ``jasper-voice``
into ``StartLimitAction=reboot``. So a file this reader cannot parse *exactly*
publishes nothing: the gate marker gets written and PID 1 skips the start
cleanly instead. Partial acceptance would be the dangerous answer.
"""
from __future__ import annotations

import os
import sys
from collections.abc import Mapping

# Keep in lockstep with jasper-voice.service's EnvironmentFile= line and
# jasper.env_load.ENV_FILES. deploy/bin/jasper-aec-reconcile deliberately
# carries NO copy — it calls this module. tests/test_voice_input_gate.py
# asserts the agreement.
DEFAULT_ACCESSORY_MIC_ENV_FILE = "/var/lib/jasper/accessory-mics.env"

# Must match the key jasper/config.py parses into Config.manual_mic_sources.
MANUAL_MIC_SOURCES_KEY = "JASPER_MANUAL_MIC_SOURCES"


def accessory_mic_env_path() -> str:
    """Resolved accessory-mic env path (env override wins, for tests)."""
    return os.environ.get(
        "JASPER_ACCESSORY_MIC_ENV_FILE",
        DEFAULT_ACCESSORY_MIC_ENV_FILE,
    )


def render_manual_mic_env(sources: Mapping[str, str]) -> str:
    """The exact file body for ``sources`` — empty string means "no file".

    Empty sources render to ``""`` rather than an empty assignment: the writer
    unlinks the file instead, so a reader never sees a published key with no
    value.
    """
    if not sources:
        return ""
    value = ",".join(
        f"{source}={device}" for source, device in sorted(sources.items())
    )
    return f"{MANUAL_MIC_SOURCES_KEY}={value}\n"


def parse_manual_mic_sources(body: str) -> tuple[str, ...]:
    """Source ids in ``body``, or ``()`` when it publishes none *or is unparsable*.

    Mirrors ``jasper.config._env_mapping``'s validation so this reader and the
    daemon's own parser can never disagree about whether a file is usable. See
    the module docstring for why anything short of an exact parse must resolve
    to "publishes nothing".
    """
    raw: str | None = None
    for line in body.splitlines():
        line = line.strip()
        if line.startswith(f"{MANUAL_MIC_SOURCES_KEY}="):
            # Last assignment wins, mirroring systemd EnvironmentFile=.
            raw = line[len(MANUAL_MIC_SOURCES_KEY) + 1:].strip()
    if raw is None:
        return ()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        raw = raw[1:-1]
    ids: list[str] = []
    for part in raw.replace("\n", ",").split(","):
        item = part.strip()
        if not item:
            continue
        key, separator, value = item.partition("=")
        key = key.strip()
        value = value.strip()
        if not separator or not key or not value:
            return ()
        if any(ch.isspace() for ch in key) or key in ids:
            return ()
        ids.append(key)
    return tuple(ids)


def read_accessory_mic_sources(path: str | None = None) -> tuple[str, ...]:
    """Source ids currently published by ``jasper-accessory-reconcile``.

    Read FRESH from disk on every call, never from ``os.environ``: the status
    daemons (``jasper-control``, ``jasper-doctor``) load their environment once
    at start and are not restarted when a remote is paired or forgotten, so a
    cached value goes stale exactly when it matters.

    Fail-safe to ``()``: a missing, unreadable, or malformed file must never
    *invent* an accessory microphone, because that would hold the voice-input
    gate open on a box with no usable input at all.
    """
    try:
        with open(path or accessory_mic_env_path(), encoding="utf-8") as handle:
            body = handle.read()
    except (OSError, UnicodeDecodeError, ValueError):
        return ()
    return parse_manual_mic_sources(body)


def main(argv: list[str] | None = None) -> int:
    """Print published source ids; exit 0 when at least one is published.

    The shell contract for ``deploy/bin/jasper-aec-reconcile``: exit status is
    the answer, stdout is the detail for its log line. Non-zero on *any*
    problem, so a broken interpreter or a corrupt file reads as "no accessory
    microphone" — which is exactly the pre-existing behaviour.
    """
    del argv
    sources = read_accessory_mic_sources()
    if not sources:
        return 1
    sys.stdout.write(",".join(sources) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
