"""Opt-in shared "control token" for jasper-control's high-impact mutations.

JTS runs on a trusted household LAN with no auth on ``jasper-control``
(``0.0.0.0:8780``): the Host / Origin / Fetch-Metadata guard in
``jasper/http_security.py`` only blocks *cross-site browsers* — a plain
``curl`` with no Host/Origin header sails through. That is deliberate for
the dial / Home Assistant / Shortcuts trusted-LAN posture, but it means
any device already on the LAN can ``curl`` ``/system/poweroff`` (power
loop), ``/mic/mute`` (defeat the privacy-mic promise), or
``/grouping/set`` (hijack output routing).

This module gates those routes: when a token file exists, exactly the gated
routes require a matching ``X-JTS-Token`` header. The *primitives* are
fail-safe default-off — with no token file, :func:`verify` returns True and a
process can never accidentally lock out the household by appearing
half-configured. WS1 Phase 2 makes the gate **mandatory but invisible**:
jasper-control calls :func:`ensure_token` at startup, so the file always
exists (auto-generated, 0600), and ``canonical_page`` auto-delivers the value
to the same-origin dashboard as a meta tag behind the read guard — the
household never sees or types it. This is defense-in-depth against drive-by /
CSRF / casual curl on the annoyance-class routes, not a boundary against a
determined LAN device (which can fetch the page too); the real containment is
the daemon hardening + user drop. SECURITY.md and
docs/HANDOFF-privilege-separation.md document the posture; ``jasper-control-token``
remains for inspecting / rotating the value.

Design notes:

- **Constant-time compare.** :func:`verify` uses
  :func:`hmac.compare_digest`, never ``==`` — a plain equality check
  leaks the token length and prefix through timing.
- **Secret never logged.** Nothing in this module logs the token value.
  The CLI prints it to the operator's own terminal on ``--enable`` /
  ``--show``; the doctor and ``/state`` report only the *posture*
  (enabled / disabled), never the secret.
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
"""
from __future__ import annotations

import hmac
import os
import secrets
import tempfile

# The token file. Seeded from the env var at import; callers read the
# module attribute (not the env var) so tests can monkeypatch this single
# constant. /var/lib/jasper is the wizard/secret directory (0750, root),
# the same home as voice_provider.env and the Wi-Fi guardian stash.
TOKEN_FILE = os.environ.get(
    "JASPER_CONTROL_TOKEN_FILE", "/var/lib/jasper/control_token"
)


def _stored_token() -> str:
    """The stripped token on disk, or "" when absent/empty/unreadable.

    Trailing newline (and surrounding whitespace) is stripped so a token
    written with ``echo`` and one written atomically by the CLI compare
    equal. Any read error (missing file, permission denied) resolves to
    "" — i.e. "gate not configured", never a raise: a control request
    must not 500 because the optional token file couldn't be read.
    """
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def token_enforced() -> bool:
    """True iff a non-empty token file exists (the gate is opted in).

    An absent or empty file means default-off: a missing file can never
    enable the gate.
    """
    return bool(_stored_token())


def current_token() -> str:
    """The active token, or "" if none exists yet.

    Public reader for surfaces that auto-deliver the token to a same-origin
    dashboard — ``canonical_page`` embeds it as a meta tag behind the
    management-host / Fetch-Metadata read guard (WS1 Phase 2, the "invisible"
    delivery: zero household friction, defense-in-depth against drive-by/CSRF,
    not a boundary against a determined LAN device — see
    docs/HANDOFF-privilege-separation.md). Same read path as :func:`verify`, so
    the embedded value and the verified value never disagree.
    """
    return _stored_token()


def ensure_token() -> str:
    """Generate + persist a token if none exists; return the active token.

    Idempotent and atomic (tempfile + ``os.replace`` at mode 0600). jasper-control
    calls this once at startup, which makes the gate *always armed* — the
    destructive routes require a token with no operator action. Auto-generation
    is what turns #712's opt-in floor into the Phase-2 mandatory-but-invisible
    gate. Returns an already-present token unchanged, so a household's stored
    token (or a hand-set one) is never rotated out from under it.
    """
    existing = _stored_token()
    if existing:
        return existing
    token = secrets.token_urlsafe(32)
    _write_atomic(token)
    return token


def _write_atomic(token: str) -> None:
    """Write ``token`` to :data:`TOKEN_FILE` atomically at mode 0600.

    Mirrors the ``jasper-control-token`` CLI writer: a tempfile in the same
    directory, ``fchmod 0600`` *before* the rename so the secret is never even
    briefly world-readable, then ``os.replace`` for an atomic swap. The secret
    value is never logged.
    """
    directory = os.path.dirname(TOKEN_FILE) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".control_token.")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(token + "\n")
        os.replace(tmp, TOKEN_FILE)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def verify(provided: str | None) -> bool:
    """True iff this request may proceed past the token gate.

    Default-off: when the gate is not enforced (no/empty token file),
    always True — no behaviour change from today. When enforced, compares
    ``provided`` against the stored token in **constant time** via
    :func:`hmac.compare_digest`; a missing header (``None``) compares as
    the empty string and fails.
    """
    stored = _stored_token()
    if not stored:
        return True
    return hmac.compare_digest(provided or "", stored)
