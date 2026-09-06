# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The one Python redactor for text bound for logs, ``/state`` or doctor.

See ADR-0240.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

# The project's secret-env-name convention, as a suffix rule on its own:
# a name carrying one of these may live only in a secret compartment.
SECRET_ENV_SUFFIX_RE = r"_(?:API_KEY|TOKEN|SECRET|PSK|PASSWORD|PASSPHRASE)"

# What both redactors scrub — the convention plus the one key that predates
# it. `JASPER_MTA_BUSTIME_KEY` is also a documented `/etc/jasper/jasper.env`
# key, so a consumer asking which names belong *only* in a compartment wants
# the suffix rule above instead. Kept in step with
# `JASPER_SECRET_ENV_NAME_RE` in scripts/_diagnostic_redaction.sh.
SECRET_ENV_NAME_RE = (
    rf"(?:[A-Za-z_][A-Za-z0-9_]*{SECRET_ENV_SUFFIX_RE}|JASPER_MTA_BUSTIME_KEY)"
)

# A value run scoped to its own quote character: the closing quote is
# optional so a body clipped mid-value still redacts, and `\n` is excluded
# so an unterminated quote cannot swallow the following lines.
_QUOTED = r"'[^'\n]*'?|\"[^\"\n]*\"?"

# An env-file or `NAME: value` line: the value runs to end of line, which
# is the only way a space-bearing WPA passphrase comes out whole.
_ENV_LINE_RE = re.compile(
    rf"(?m)^([ \t]*{SECRET_ENV_NAME_RE}[ \t]*[=:][ \t]*).*$",
)

# systemd `Environment="NAME=value with spaces"`. The backreference scopes
# the run to the opening quote, so an apostrophe inside a double-quoted
# value does not end it early.
_ENV_QUOTED_RE = re.compile(
    rf"(['\"])({SECRET_ENV_NAME_RE}=)(?:(?!\1)[^\n])*\1",
)

# A convention-named assignment anywhere on the line, value to the next
# whitespace — the bash redactor's last expression. `&`, `)`, `,`, `;` and
# `}` are legal WPA passphrase characters that the keyword rule's value
# class stops at, so without this a mid-line PSK leaks its tail. A quote
# *before* the name is `_ENV_QUOTED_RE`'s shape, not this one.
_ENV_ASSIGN_RE = re.compile(
    rf"(?<![A-Za-z0-9_'\"])({SECRET_ENV_NAME_RE}=)(?:{_QUOTED}|\S+)",
)

_BEARER_RE = re.compile(r"(?i)\b(Bearer)\s+[A-Za-z0-9._~+/=-]{8,}")

# NetworkManager names a *property* `802-11-wireless-security.psk`, and the
# wizard puts its "property is invalid" error in the banner. Only that
# prose is spared; NM's own echo shape `.psk: <value>` still redacts.
_NOT_NM_PROPERTY = r"(?![ \t]*[=:][ \t]*property\b)"

# The left boundary is a lookbehind, not `\b`: `\b` cannot match between
# `_` and a letter, so it never saw the project's own `*_API_KEY` names.
# `[ \t]` around the separator, not `\s`, so it cannot cross a newline.
# `)` and `&` stay out of the bare run so `repr(e)` keeps its paren and a
# query string keeps the parameters after the secret.
_KEY_VALUE_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])"
    r"((?:api[_-]?key|bustime[_-]?key|token|secret|password|passphrase"
    rf"|x-jts-household|psk{_NOT_NM_PROPERTY})"
    r"['\"]?[ \t]*[=:][ \t]*)"
    rf"(?:{_QUOTED}|[^'\"\s,;}}\])&]+)",
)

# A keyword introducing a value, in the two shapes that are not `NAME=`.
# After a colon the value runs to end of line: a colon-introduced
# passphrase is words, not one token (an unquoted colon only — a JSON
# `"password": "…"` stays quote-scoped in the rule above). After a space
# it is one run, floored at 8 — the WPA minimum, below which prose such as
# "password reset link sent" gets eaten — unless it opens with a quote,
# which shell escaping puts *inside* the token (`'don'\''t'`). `wpa-psk`
# is a key-mgmt value, not a secret. An unmatched group renders empty, so
# one template serves both shapes.
_SECRET_WORD_RE = re.compile(
    rf"(?im)(?<![A-Za-z0-9])(password|passphrase|(?<!wpa-)psk){_NOT_NM_PROPERTY}"
    rf"(?:([ \t]*:[ \t]*)\S.*$|([ \t]+)(?:(?:{_QUOTED})\S*|\S{{8,}}))",
)

# `key` alone: every other query-parameter name is already a `_KEY_VALUE_RE`
# keyword.
_URL_PARAM_RE = re.compile(r"(?i)([?&]key=)[^&\s'\"<>]+")

# Live provider key prefixes: Google (AIza), OpenAI (sk-), xAI (xai-),
# Google OAuth client secret (GOCSPX-). See ADR-0240.
_KEY_PREFIX_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:AIza|sk-|xai-|GOCSPX-)[A-Za-z0-9_-]{8,}",
)

_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (_BEARER_RE, r"\1 <redacted>"),
    (_ENV_LINE_RE, r"\1<redacted>"),
    (_ENV_QUOTED_RE, r"\1\2<redacted>\1"),
    (_ENV_ASSIGN_RE, r"\1<redacted>"),
    (_KEY_VALUE_RE, r"\1<redacted>"),
    (_SECRET_WORD_RE, r"\1\2\3<redacted>"),
    (_URL_PARAM_RE, r"\1<redacted>"),
    (_KEY_PREFIX_RE, "<redacted>"),
)


def redact_secrets(message: str, literals: Iterable[str] = ()) -> str:
    """Replace anything credential-shaped in `message` with `<redacted>`.

    `literals` are secret values the caller holds. They are replaced first
    and by value, which is the only way a credential in an unrecognised
    shape comes out — the patterns run after either way (ADR-0240).
    """
    for literal in literals:
        if literal:
            message = message.replace(literal, "<redacted>")
    for pattern, replacement in _RULES:
        message = pattern.sub(replacement, message)
    return message
