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
never opens the gate: the marker gets written and PID 1 skips the start cleanly
instead. Partial acceptance would be the dangerous answer.

It raises rather than returning ``()``, and that is the whole point. Both
outcomes park, so the *verdict* was never in question — but they are different
*facts*, and the fact is what an operator reads. Collapsing "I read it and it
is corrupt" into "no accessory is paired" told somebody debugging "my remote
does nothing" that no remote was paired while a file naming that remote sat on
disk. Raising also completes the mirror this module exists for: the daemon's
parser raises on exactly these three conditions with exactly these messages, so
the two now agree about *why* a file is unusable and not merely that it is.
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


class ManualMicSourcesError(ValueError):
    """The published file exists and was read, but its content is not parsable.

    A ``ValueError`` subclass on purpose: display surfaces
    (``jasper.mic_presence``) already degrade the whole "I could not determine
    the accessory half" family to "no accessory" behind one
    ``except (OSError, UnicodeDecodeError, ValueError)``, so they need no edit
    to keep never raising. The gate writer, which must tell the two apart, does
    not catch it.
    """


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
    """Source ids in ``body``, or ``()`` when it publishes none.

    Raises ``ManualMicSourcesError`` when the key IS present with a value that
    does not parse exactly. That is a different fact from "publishes none", and
    the caller that writes the gate marker says which one it saw out loud.

    Mirrors ``jasper.config._env_mapping``'s validation — same three
    conditions, same three messages — so this reader and the daemon's own
    parser can never disagree about whether a file is usable *or about why*.
    An absent key, an empty value, and a value of only separators all publish
    nothing without raising: those are answers, not failures.
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
            raise ManualMicSourcesError(
                f"{MANUAL_MIC_SOURCES_KEY} entries must be source_id=device, "
                "separated by commas"
            )
        if any(ch.isspace() for ch in key):
            raise ManualMicSourcesError(
                f"{MANUAL_MIC_SOURCES_KEY} source ids must not contain whitespace"
            )
        if key in ids:
            raise ManualMicSourcesError(
                f"{MANUAL_MIC_SOURCES_KEY} contains duplicate source id {key!r}"
            )
        ids.append(key)
    return tuple(ids)


def read_accessory_mic_sources(path: str | None = None) -> tuple[str, ...]:
    """Source ids currently published by ``jasper-accessory-reconcile``.

    Read FRESH from disk on every call, never from ``os.environ``: the status
    daemons (``jasper-control``, ``jasper-doctor``) load their environment once
    at start and are not restarted when a remote is paired or forgotten, so a
    cached value goes stale exactly when it matters.

    Three outcomes, deliberately NOT collapsed into one. Every one of them
    parks voice, so the *verdict* is the same; the *fact* is not, and the fact
    is what reaches an operator through ``/state.microphone.reason`` and the
    doctor headline:

    * **No file, or a file that publishes nothing** → ``()``. A real answer: no
      accessory microphone is paired.
    * **A file that exists but cannot be read** (``EACCES``, ``EIO``,
      undecodable bytes) → the ``OSError``/``UnicodeDecodeError``
      **propagates**. "I could not look" is not "I looked and there is
      nothing".
    * **A file that reads but does not parse** → ``ManualMicSourcesError``.
      "I read it and it is corrupt" is not "no remote is paired" either — and
      that collapse is the one that told an operator no remote was paired while
      a file naming their remote sat on disk.

    Display surfaces that must never raise (``jasper.mic_presence``) catch all
    three themselves and degrade to ``()``.
    """
    target = path or accessory_mic_env_path()
    try:
        with open(target, encoding="utf-8") as handle:
            body = handle.read()
    except FileNotFoundError:
        return ()
    return parse_manual_mic_sources(body)


def main(argv: list[str] | None = None) -> int:
    """Print published source ids, one comma-separated line; exit 0 when read.

    The shell contract for ``deploy/bin/jasper-aec-reconcile``:

    * **exit 0, non-empty stdout** — read succeeded, these sources are published.
    * **exit 0, empty stdout** — read succeeded, nothing is published.
    * **non-zero exit** — the probe could not answer (module unimportable,
      unreadable file, **unparsable file**, killed). Never let this look like
      "nothing is published": the caller must be able to say "I could not tell"
      in the marker reason.

    Exit status is deliberately *not* overloaded to carry the answer, because
    Python already spends non-zero on its own failures — a missing module and
    "no remote paired" both exited 1 under the previous contract, and the
    reconciler reported the second when the first was true. Stack traces are
    left on stderr for the journal.

    A corrupt file gets a one-line message instead of a traceback: the parser
    already knows exactly which of the three rules the content broke, and that
    sentence is the remediation. Everything else keeps its traceback, where the
    stack IS the diagnostic.
    """
    del argv
    try:
        sources = read_accessory_mic_sources()
    except ManualMicSourcesError as exc:
        sys.stderr.write(f"refusing to publish accessory mic sources: {exc}\n")
        return 1
    sys.stdout.write(",".join(sources) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
