# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""One measurement session's identity, stable across every store.

**The problem this exists to fix** (``docs/historical/attribution-stage-plan.md`` §6, on
WO-0's corpus sweep): the 2026-07 corpus was split across four stores that
share no identifier — the laptop ``captures/`` archive, the crossover-v2
commissioning bundles, the room-correction bundles, and the operator capture
ring — with one capture's identity living in three unrelated namespaces at
once (bundle id, relay capture-session id, epoch-microsecond stamp). The
**SHA-256 of the WAV bytes was the only reliable join**, and directory mtime
was worse than useless: it actively misroutes (the ~08:27 morning session
lives in bundle ``7f54494228cc``, while the bundle whose mtime *is* 08:27
holds the previous evening's run).

So the rule this module makes executable, from §6: **content hashing stays the
verifier; it must stop being the index.** A finding cites its evidence by a
:class:`SessionIdentity` plus a store-relative locator; the SHA-256 rides
along to *verify* the bytes are the ones that were cited, never to *find*
them.

**Scope as shipped — which of the four stores actually writes this.** Two do,
and the gap is stated rather than implied:

* **crossover-v2 commissioning bundle** — writer shipped. The bundle session
  id is the canonical value, and the cloud artifact and finding set both
  carry it.
* **operator capture ring** — writer shipped, and it is the store that needed
  it most: it carried no session id of any kind, only the epoch-microsecond
  filename stamp.
* **laptop `captures/` archive** — **no writer here.** It inherits identity
  transitively, from ring sidecars pulled off the Pi, which now carry the key.
  ``EVIDENCE_STORE_LAPTOP_ARCHIVE`` exists so a finding can *cite* an archive
  file; nothing in this repo stamps one.
* **room-correction bundles** — **deferred to WO-8**, which is the rung that
  adopts the room line onto this library (§3.5's SoC boundary). The type is
  ready for it; the writer is not this WO's.

So "survives every hop" is true of every hop WO-1 owns, and the two remaining
hops are named above rather than left for a reader to discover as an absence.

**The shape.** One identifier, one canonical key name, written unchanged into
every store that holds a byproduct of the session:

* :data:`SESSION_IDENTITY_KEY` is the single JSON key every store writes it
  under. A reader that finds that key anywhere — a bundle artifact, a capture
  ring sidecar, a laptop archive manifest, a future harness artifact — knows
  it holds this session's identity without knowing which store it came from.
* :attr:`SessionIdentity.token` is the flat string form
  (``jts-session-1:<session_id>``), for filenames, log lines, and any place
  that can only carry a scalar. The scheme prefix makes it self-identifying
  and makes a future shape change detectable rather than silent.

**Aliases are not second identities.** The other namespaces are real and
already minted (the relay session id, for instance, is minted *after* the
bundle and is not derivable from it), so :attr:`SessionIdentity.aliases`
records them as a lookup table hanging off the one canonical id. A reader
holding only a ``cap_…`` relay id can therefore resolve to the session; a
reader holding the session can name every alias it was known by. The
canonical ``session_id`` is what everything else is keyed on.

This module performs no I/O and imports nothing from the flow. It is a value
type: construct it, serialize it, compare it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

#: The scheme every identity minted by this module carries. Bump the integer
#: only for a change that a reader of the old shape could not interpret; the
#: prefix exists so such a change is detectable instead of silently wrong.
SESSION_IDENTITY_SCHEME = "jts-session-1"

#: **The one key name.** Every store writes the serialized identity under this
#: key. Changing it breaks every cross-store join that has already been
#: written, so it is a constant, not a parameter.
SESSION_IDENTITY_KEY = "jts_session_identity"

#: Known alias namespaces. Not closed by validation — a new store may name a
#: namespace this module has never heard of, and refusing it would push that
#: store back to having no join at all — but listed so the common ones stay
#: spelled the same way.
ALIAS_RELAY_SESSION_ID = "relay_session_id"

_TOKEN_RE = re.compile(r"[A-Za-z0-9_.\-]{1,128}")
_MAX_ALIASES = 16


class SessionIdentityError(ValueError):
    """A session identity is malformed, ambiguous, or not serializable."""


def _identifier(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or _TOKEN_RE.fullmatch(value) is None:
        raise SessionIdentityError(
            f"{field_name} must be 1-128 chars of [A-Za-z0-9_.-]"
        )
    return value


@dataclass(frozen=True)
class SessionIdentity:
    """The identifier that survives every hop.

    Args:
      session_id: the canonical, opaque id. On the crossover-v2 path this is
        the commissioning bundle's own session id, because Q-C's
        bundle-lifetime ruling makes the bundle the retention unit — the
        identity and the lifetime then name the same thing, which is the
        property that keeps a finding from outliving its evidence.
      aliases: other namespaces this same session is known by, as
        ``{namespace: id}``. A lookup table, never a second identity: two
        identities are equal when their ``session_id`` and ``scheme`` match,
        and the alias table is part of that equality only because a
        disagreeing table means two writers disagree about the session, which
        is worth surfacing rather than smoothing over.
      scheme: :data:`SESSION_IDENTITY_SCHEME`. Rejected if anything else — a
        reader that cannot interpret the shape must not guess at it.
    """

    session_id: str
    aliases: Mapping[str, str] = field(default_factory=dict)
    scheme: str = SESSION_IDENTITY_SCHEME

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "session_id", _identifier(self.session_id, field_name="session_id")
        )
        if self.scheme != SESSION_IDENTITY_SCHEME:
            raise SessionIdentityError(
                f"unsupported session identity scheme: {self.scheme!r}"
            )
        if not isinstance(self.aliases, Mapping):
            raise SessionIdentityError("aliases must be a mapping")
        if len(self.aliases) > _MAX_ALIASES:
            raise SessionIdentityError(
                f"at most {_MAX_ALIASES} aliases may ride one identity"
            )
        frozen = {
            _identifier(key, field_name="alias namespace"): _identifier(
                value, field_name=f"alias {key}"
            )
            for key, value in self.aliases.items()
        }
        object.__setattr__(self, "aliases", MappingProxyType(dict(sorted(frozen.items()))))

    @property
    def token(self) -> str:
        """The flat string form — ``jts-session-1:<session_id>``.

        For filenames, log lines, and any carrier that can hold only a scalar.
        Round-trips through :meth:`from_token`, which does **not** recover the
        alias table: a token is the identity, aliases are decoration.
        """
        return f"{self.scheme}:{self.session_id}"

    def with_alias(self, namespace: str, value: str) -> "SessionIdentity":
        """This identity plus one more alias. Never mutates; re-validates."""

        return SessionIdentity(
            session_id=self.session_id,
            aliases={**dict(self.aliases), namespace: value},
            scheme=self.scheme,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "scheme": self.scheme,
            "session_id": self.session_id,
            "aliases": dict(self.aliases),
        }

    @classmethod
    def from_mapping(cls, raw: Any) -> "SessionIdentity":
        if not isinstance(raw, Mapping):
            raise SessionIdentityError("session identity must be an object")
        unknown = set(raw) - {"scheme", "session_id", "aliases"}
        if unknown:
            raise SessionIdentityError(
                f"session identity has unknown fields: {sorted(unknown)}"
            )
        aliases = raw.get("aliases") or {}
        if not isinstance(aliases, Mapping):
            raise SessionIdentityError("aliases must be an object")
        # ``session_id`` is REQUIRED, and its absence gets its own message.
        # This method already refuses unknown fields; it did not require the
        # one field that matters, so a mapping missing it fell through to
        # ``__post_init__``'s charset check and reported a malformed
        # identifier rather than a missing one. (``Finding.from_mapping``
        # checks presence explicitly — this was the inconsistent sibling.)
        session_id = raw.get("session_id")
        if not isinstance(session_id, str):
            raise SessionIdentityError(
                "session identity is missing a string session_id"
            )
        return cls(
            session_id=session_id,
            aliases=dict(aliases),
            scheme=str(raw.get("scheme") or SESSION_IDENTITY_SCHEME),
        )

    @classmethod
    def from_token(cls, token: Any) -> "SessionIdentity":
        """Parse the flat form. Aliases are not carried by a token."""

        if not isinstance(token, str) or token.count(":") != 1:
            raise SessionIdentityError(
                "session identity token must be exactly '<scheme>:<session_id>'"
            )
        scheme, _, session_id = token.partition(":")
        return cls(session_id=session_id, scheme=scheme)


def stamp_session_identity(
    payload: dict[str, Any], identity: SessionIdentity
) -> dict[str, Any]:
    """Write ``identity`` into ``payload`` under :data:`SESSION_IDENTITY_KEY`.

    The one helper every store should call, so the key name is spelled in one
    place. Mutates and returns ``payload`` for use inline at a write site.
    """

    payload[SESSION_IDENTITY_KEY] = identity.to_dict()
    return payload


def read_session_identity(payload: Any) -> SessionIdentity | None:
    """Recover an identity a store stamped, or ``None`` if it carries none.

    ``None`` is the **legacy** answer, and it is deliberately not an error:
    every artifact written before this module existed has no identity key, and
    the corpus those artifacts came from is exactly what WO-0 had to read. A
    malformed identity, by contrast, raises — that is a writer bug, not
    history.
    """

    if not isinstance(payload, Mapping):
        return None
    raw = payload.get(SESSION_IDENTITY_KEY)
    if raw is None:
        return None
    return SessionIdentity.from_mapping(raw)


__all__ = [
    "ALIAS_RELAY_SESSION_ID",
    "SESSION_IDENTITY_KEY",
    "SESSION_IDENTITY_SCHEME",
    "SessionIdentity",
    "SessionIdentityError",
    "read_session_identity",
    "stamp_session_identity",
]
