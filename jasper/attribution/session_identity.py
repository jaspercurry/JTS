# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""One measurement session's identity, stable across every store.

The rule this makes executable: content hashing stays the VERIFIER and stops
being the index. A finding cites its evidence by a :class:`SessionIdentity`
plus a store-relative locator, and the SHA-256 rides along to verify the bytes,
never to find them. :data:`SESSION_IDENTITY_KEY` is the single JSON key every
store writes it under; :attr:`SessionIdentity.token` is the flat scalar form.
Aliases are a lookup table hanging off the one canonical id, never a second
identity. Writers ship for the crossover-v2 commissioning bundle and the
operator capture ring; the laptop archive inherits identity transitively and
room-correction bundles are deferred to WO-8. No I/O; a value type.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

#: The scheme every identity minted here carries. Bump the integer only for a
#: change a reader of the old shape could not interpret.
SESSION_IDENTITY_SCHEME = "jts-session-1"

#: The one key name every store writes the serialized identity under. Changing
#: it breaks every cross-store join already written.
SESSION_IDENTITY_KEY = "jts_session_identity"

#: Known alias namespaces. Deliberately not closed by validation — a new store
#: may name one this module has never heard of.
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

    ``session_id`` is the canonical, opaque id; on the crossover-v2 path it is
    the commissioning bundle's own, so identity and retention lifetime name the
    same thing. ``aliases`` maps other namespaces to their ids — a lookup
    table, part of equality only because a disagreeing table means two writers
    disagree about the session. ``scheme`` must be
    :data:`SESSION_IDENTITY_SCHEME`; anything else is rejected.
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

        Round-trips through :meth:`from_token`, which does NOT recover the
        alias table.
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
        # ``session_id`` is REQUIRED and its absence gets its own message,
        # rather than falling through to ``__post_init__``'s charset check and
        # reporting a malformed identifier instead of a missing one.
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

    Mutates and returns ``payload`` for use inline at a write site.
    """

    payload[SESSION_IDENTITY_KEY] = identity.to_dict()
    return payload


def read_session_identity(payload: Any) -> SessionIdentity | None:
    """Recover an identity a store stamped, or ``None`` if it carries none.

    ``None`` is the legacy answer and deliberately not an error; a malformed
    identity raises, because that is a writer bug rather than history.
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
