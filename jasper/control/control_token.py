# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Opt-in shared "control token" for jasper-control's high-impact mutations.

JTS runs on a trusted household LAN with no auth on ``jasper-control``
(``0.0.0.0:8780``): the Host / Origin / Fetch-Metadata guard in
``jasper/net/http_security.py`` only blocks *cross-site browsers* — a plain
``curl`` with no Host/Origin header sails through. That is deliberate for
supported accessories / Home Assistant / Shortcuts trusted-LAN posture, but it means
any device already on the LAN can ``curl`` ``/system/poweroff`` (power
loop), ``/mic/mute`` (defeat the privacy-mic promise), or
``/grouping/set`` (hijack output routing).

This module gates those routes: when a token file exists, exactly the gated
routes require a matching ``X-JTS-Token`` header. The *primitives* are
fail-safe default-off — with no token file, :func:`verify` returns True and a
process can never accidentally lock out the household by appearing
half-configured. The gate is mandatory but invisible in practice:
jasper-control calls :func:`ensure_token` at startup, so the file always
exists (auto-generated, 0640 group jasper — see :func:`ensure_token`), and
``canonical_page`` auto-delivers the value to the same-origin dashboard as a
meta tag behind the read guard, so the household never sees or types it. This
is defense-in-depth against drive-by / CSRF / casual curl on the
annoyance-class routes, not a boundary against a determined LAN device (which
can fetch the page too); the real containment is the daemon hardening + user
drop. SECURITY.md documents the posture; ``jasper-control-token`` remains for
inspecting / rotating the value.

Design notes:

- **Constant-time compare.** :func:`verify` uses
  :func:`hmac.compare_digest`, never ``==`` — a plain equality check
  leaks the token length and prefix through timing.
- **Secret never logged.** Nothing in this module logs the token value.
  The CLI prints it to the operator's own terminal on ``--enable`` /
  ``--show``; the doctor's `control token gate` check reports only the
  *posture* (enabled / disabled), never the secret.
- **Fail-safe direction is FAIL-CLOSED, but only once enabled.** If the
  token file exists with content, a request without the right header is
  rejected. If the file is absent or empty, the gate is simply off — a
  missing file can never *enable* the gate, so a security feature can't
  accidentally lock out the household by appearing half-configured.

The file path defaults to ``/var/lib/jasper/control_token`` and is
overridable via ``JASPER_CONTROL_TOKEN_FILE`` (codified in
``.env.example``) so tests and headless imaging can point it elsewhere.
The module reads :data:`TOKEN_FILE` fresh on every call — the enable /
disable CLI mutates the file out-of-band, and ``jasper-control`` is not
restarted on an enable, so a cached value would go stale.

The read/write/verify mechanics are shared with
:mod:`jasper.control.household_credential` via
:mod:`jasper.control._secret_file`; only the path and this docstring's trust
domain differ.
"""
from __future__ import annotations

import os

from jasper.control import _secret_file

# The token file. Seeded from the env var at import; callers read the
# module attribute (not the env var) so tests can monkeypatch this single
# constant. /var/lib/jasper is the shared state directory (root:jasper 0770),
# the same home as voice_provider.env and the Wi-Fi guardian stash.
TOKEN_FILE = os.environ.get(
    "JASPER_CONTROL_TOKEN_FILE", "/var/lib/jasper/control_token"
)


def token_enforced() -> bool:
    """True iff a non-empty token file exists (the gate is opted in).

    An absent or empty file means default-off: a missing file can never
    enable the gate.
    """
    return _secret_file.is_set(TOKEN_FILE)


def current_token() -> str:
    """The active token, or "" if none exists yet.

    Public reader for surfaces that auto-deliver the token to a same-origin
    dashboard: ``canonical_page`` embeds it as a meta tag behind the
    management-host / Fetch-Metadata read guard. Same read path as
    :func:`verify`, so the embedded value and the verified value never
    disagree.
    """
    return _secret_file.read(TOKEN_FILE)


def ensure_token() -> str:
    """Generate + persist a token if none exists; return the active token.

    Idempotent and atomic (tempfile + ``os.replace`` at mode 0640 group
    jasper). jasper-control calls this once at startup, which keeps the gate
    always armed. An already-present token is returned unchanged, so a
    household's stored (or hand-set) token is never rotated out from under it.

    **0640, not 0600.** The token file lives under ``/var/lib/jasper``, whose
    ``StateDirectory=jasper`` recursive-chown can make its owner
    ``jasper-voice`` rather than jasper-control, and it is read cross-user —
    jasper-web embeds it via ``canonical_page()``. An owner-only 0600 token
    would therefore be unreadable by the non-root jasper-control and
    jasper-web, and because the stored-token read fails safe to "" (gate OFF)
    on EACCES, that would SILENTLY DISABLE the gate. The token is CSRF-grade
    defense-in-depth, not a hard boundary, and its readers are sibling daemons
    already in the trust domain.
    """
    return _secret_file.ensure(TOKEN_FILE)


def verify(provided: str | None) -> bool:
    """True iff this request may proceed past the token gate.

    Default-off: when the gate is not enforced (no/empty token file),
    always True — no behaviour change from today. When enforced, compares
    ``provided`` against the stored token in **constant time** via
    :func:`hmac.compare_digest`; a missing header (``None``) compares as
    the empty string and fails.
    """
    return _secret_file.verify(TOKEN_FILE, provided)
