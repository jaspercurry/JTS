"""Opt-in shared "control token" for jasper-control's high-impact mutations.

JTS runs on a trusted household LAN with no auth on ``jasper-control``
(``0.0.0.0:8780``): the Host / Origin / Fetch-Metadata guard in
``jasper/http_security.py`` only blocks *cross-site browsers* — a plain
``curl`` with no Host/Origin header sails through. That is deliberate for
the dial / Home Assistant / Shortcuts trusted-LAN posture, but it means
any device already on the LAN can ``curl`` ``/system/poweroff`` (power
loop), ``/mic/mute`` (defeat the privacy-mic promise), or
``/grouping/set`` (hijack output routing).

This module is the security-conscious operator's **opt-in** floor under
those four routes: when a token file exists, exactly the gated routes
require a matching ``X-JTS-Token`` header. It is **default-off** — with
no token file, :func:`verify` returns True and behaviour is byte-for-byte
what it is today. SECURITY.md documents the threat model and the enable
flow (``jasper-control-token --enable``).

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
