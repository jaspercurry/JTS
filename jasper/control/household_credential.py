# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Household shared secret for device-to-device control (the bond fan-out).

JTS's per-device :mod:`jasper.control.control_token` is a CSRF token: it proves
"this request came from a page on my own origin," which is right for
*browser -> its own speaker* and **wrong for machine-to-machine** (each speaker
mints its OWN distinct control token, so a leader has nothing a follower's gate
will accept). This module is the M2M counterpart: ONE secret per household,
minted at the human pairing moment (the ``/rooms/`` bond fan-out), distributed
to each member over the trusted LAN, and presented on the cross-device grouping
path as a STATIC bearer in the ``X-JTS-Household`` header. Distinct file,
distinct header, distinct trust domain (peer identity, not page origin) — the
two never blur. Full design + threat model + the rejected HMAC-signing
alternative: ``docs/HANDOFF-control-plane-auth.md``.

This is a near-line-for-line clone of ``control_token.py`` (the smallest design
that fits the system), with four deliberate differences:

- **Header / file / env are distinct**: ``X-JTS-Household`` vs ``X-JTS-Token``;
  ``/var/lib/jasper/household_secret`` vs ``…/control_token``;
  ``JASPER_HOUSEHOLD_SECRET_FILE`` vs ``JASPER_CONTROL_TOKEN_FILE``.
- **Minted at /bond, never at install/startup.** A lone speaker never needs a
  household secret; absence simply means "not yet paired." So unlike
  ``control_token.ensure_token()`` (called at every jasper-control startup to
  make the CSRF gate mandatory), :func:`ensure` runs only when a human bonds
  speakers (``rooms_setup._save_bond``).
- **:func:`adopt` (new): trust-on-first-use distribution.** A follower receiving
  the bond fan-out persists the leader's secret so subsequent cross-device
  calls verify against it. It refuses to OVERWRITE an existing secret.
- **:func:`clear` (new): drop on unbond** so a speaker can later re-pair.

Design invariants (shared with ``control_token``):

- **Constant-time compare.** :func:`verify` uses :func:`hmac.compare_digest`,
  never ``==`` — equality would leak the secret's length/prefix via timing.
- **Secret never logged.** Nothing here logs the secret value; callers that
  emit observability log only the *transition* (adopted / cleared), never the
  bytes.
- **Fail-SAFE (absent ⇒ accept).** :func:`verify` returns True when no secret is
  stored. This is the OPPOSITE of a blanket fail-closed, and deliberate: the
  secret is DISTRIBUTED over the very ``/grouping/set`` route it gates, so an
  unpaired follower fail-closing would 403 the request that installs it
  (bootstrap deadlock) and a follower that lost its secret could never be
  re-bonded (bricked self-heal). The narrow, stated exposure of the absent
  window is documented on :func:`verify`.
- **Fresh read per call.** :data:`SECRET_FILE` is read on every call —
  jasper-control is not restarted on a bond/unbond, so a cached value would go
  stale after a re-bond.
"""
from __future__ import annotations

import hmac
import os
import secrets

from jasper.atomic_io import atomic_write_text

# The household-secret file. Seeded from the env var at import; callers read the
# module attribute (not the env var) so tests can monkeypatch this single
# constant. /var/lib/jasper is the shared state directory (root:jasper 0770),
# the same home as control_token and the Wi-Fi guardian stash. The service
# users' primary group is jasper, so daemon-created files land group jasper; the
# install migration widens any older owner-only copy on upgrade.
#
# Mode 0640 group jasper (NOT 0600): TWO non-root daemons in the shared `jasper`
# group read+write this file. jasper-web mints it
# (rooms_setup._save_bond -> ensure()), while jasper-control adopts, clears, and
# verifies it (server._post_grouping_set). A 0600 file written by one would be
# unreadable by the other. This mirrors the control_token widening: jasper-web
# embeds that token in management pages and jasper-control verifies it, so both
# files need group-read once the daemons run non-root. Group-read suffices:
# writes go through atomic_write_text (a new tempfile the writer owns, renamed
# over the old — needs dir-write on the group-writable state dir, not
# file-write), and the reader needs only group-read.
SECRET_FILE = os.environ.get(
    "JASPER_HOUSEHOLD_SECRET_FILE", "/var/lib/jasper/household_secret"
)


def _stored_secret() -> str:
    """The stripped household secret on disk, or "" when absent/empty/unreadable.

    Any read error (missing file, permission denied, a directory in its place)
    resolves to "" — i.e. "not yet paired", never a raise: a grouping request
    must not 500 because the secret file couldn't be read.
    """
    try:
        with open(SECRET_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def is_paired() -> bool:
    """True iff a non-empty household secret exists (this speaker is bonded).

    An absent or empty file means "not yet paired."
    """
    return bool(_stored_secret())


def current() -> str:
    """The active household secret, or "" if none exists yet.

    Public reader for the LEADER's fan-out (``_post_grouping_to_member`` attaches
    it as ``X-JTS-Household``) and for ``/state`` / doctor posture surfaces. Same
    read path as :func:`verify`, so the presented value and the verified value
    never disagree.
    """
    return _stored_secret()


def ensure() -> str:
    """Generate + persist a household secret if none exists; return the active one.

    Minted by the LEADER at the pairing moment (``rooms_setup._save_bond``) —
    NOT at install or daemon startup. Idempotent and atomic (tempfile +
    ``os.replace`` at mode 0640, group jasper via the setgid dir — see the
    SECRET_FILE note): an existing secret is returned unchanged, so re-bonding
    the same household reuses it.
    """
    existing = _stored_secret()
    if existing:
        return existing
    secret = secrets.token_urlsafe(32)
    atomic_write_text(SECRET_FILE, secret + "\n", mode=0o640)
    return secret


def adopt(secret: str | None) -> bool:
    """Persist a secret distributed by a paired peer, trust-on-first-use.

    A follower receiving the bond fan-out's ``/grouping/set`` (carrying the
    leader's ``X-JTS-Household``) adopts that secret so every subsequent
    cross-device grouping call verifies against it — turning the fail-safe-open
    bootstrap window into the locked-down steady state. Returns True iff it
    wrote a new secret.

    Refuses to OVERWRITE an existing secret: a member already paired must not be
    silently re-keyed by a different ``X-JTS-Household`` (the residual a shared
    secret cannot close — a malicious LAN device initiating its own bond; see
    docs/HANDOFF-control-plane-auth.md §6). An empty/None value is a no-op. To
    re-key, the household unbonds (which :func:`clear`s) then re-bonds.
    """
    if not secret:
        return False
    if _stored_secret():
        return False
    # 0640 group jasper (see the SECRET_FILE note): a follower's jasper-control
    # writes it here, the leader's jasper-web reads it on a later re-bond.
    atomic_write_text(SECRET_FILE, secret + "\n", mode=0o640)
    return True


def clear() -> None:
    """Drop the household secret (this speaker is no longer paired).

    Called when a bond dissolves (the ``/unbond`` fan-out POSTs
    ``/grouping/set`` ``enabled=false``). Idempotent — a missing file is fine —
    and best-effort: a permission/IO error must not crash the grouping handler,
    so it is swallowed (the stale secret simply persists; a re-bond of the same
    household reuses it). After clearing, :func:`verify` returns to fail-safe
    accept, so the speaker can be re-bonded over the trusted LAN.
    """
    try:
        os.unlink(SECRET_FILE)
    except OSError:
        pass


def verify(provided: str | None) -> bool:
    """True iff this cross-device request may pass the ``/grouping/set`` gate.

    FAIL-SAFE: when no secret is stored (not yet paired, or lost), returns True
    for any input — so the first-bond fan-out that DISTRIBUTES the secret over
    ``/grouping/set`` is not rejected by the gate it is installing, and a
    follower that lost its secret can be re-bonded. When a secret IS stored,
    compares ``provided`` against it in CONSTANT time via
    :func:`hmac.compare_digest`; a missing header (``None``) compares as "" and
    fails.

    Deliberately NOT a blanket fail-closed: absence is a legitimate
    "not-yet-paired" state, not an attack signal. The stated, narrow exposure:
    during the window between secret loss and re-bond a follower's
    ``/grouping/set`` is briefly open to an in-scope casual-curl actor —
    acceptable (annoyance-class, the same transient window ``control_token``
    already has). Pinned by tests so a refactor can't flip it to fail-closed and
    brick re-bonding.
    """
    stored = _stored_secret()
    if not stored:
        return True
    return hmac.compare_digest(provided or "", stored)
