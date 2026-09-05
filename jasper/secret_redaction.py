# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The one Python redactor for text bound for logs, ``/state`` or doctor.

Every credential-shaped span is replaced whole with ``<redacted>``, and no
caller needs the secret in hand to scrub it.
"""
from __future__ import annotations

import re

# Kept in step with `JASPER_SECRET_ENV_NAME_RE` in
# scripts/_diagnostic_redaction.sh: the project's secret-env-name
# convention plus the one key that predates it.
SECRET_ENV_NAME_RE = (
    r"(?:[A-Za-z_][A-Za-z0-9_]*_(?:API_KEY|TOKEN|SECRET|PSK"
    r"|PASSWORD|PASSPHRASE)|JASPER_MTA_BUSTIME_KEY)"
)

# An env-file or `NAME: value` line: the value runs to end of line, which
# is the only way a space-bearing WPA passphrase comes out whole.
_ENV_LINE_RE = re.compile(
    rf"(?m)^([ \t]*{SECRET_ENV_NAME_RE}[ \t]*[=:][ \t]*).*$",
)

# systemd `Environment="NAME=value with spaces"`, one rule per quote
# character: a single run excluding both quotes stops at an apostrophe
# inside a double-quoted value and leaves the rest of the credential in
# place.
_ENV_SQUOTED_RE = re.compile(rf"'({SECRET_ENV_NAME_RE}=)[^'\n]*'")
_ENV_DQUOTED_RE = re.compile(rf"\"({SECRET_ENV_NAME_RE}=)[^\"\n]*\"")

_BEARER_RE = re.compile(r"(?i)\b(Bearer)\s+[A-Za-z0-9._~+/=-]{8,}")

# The left boundary is a lookbehind, not `\b`: `\b` cannot match between
# `_` and a letter, so it never saw the project's own `*_API_KEY` names.
# A closing quote is optional so a line clipped mid-value still redacts;
# `\n` is excluded from every value run so an unterminated quote cannot
# swallow the following lines. `[ \t]` around the separator, not `\s`, for
# the same reason. `)` and `&` stay out of the bare run so `repr(e)` keeps
# its paren and a query string keeps the parameters after the secret.
_KEY_VALUE_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])"
    r"((?:api[_-]?key|bustime[_-]?key|token|secret|password|passphrase|psk)"
    r"['\"]?[ \t]*[=:][ \t]*)"
    r"(?:'[^'\n]*'?|\"[^\"\n]*\"?|[^'\"\s,;}\])&]+)",
)

# 8 is the WPA passphrase minimum; a shorter run swallows prose such as
# "password reset link sent". `wpa-psk` is a key-mgmt value, not a secret.
_SECRET_WORD_RE = re.compile(
    r"(?i)\b(password|(?<!wpa-)psk)[ \t]+(?:'[^'\n]*'?|\"[^\"\n]*\"?|\S{8,})",
)

# `key` alone: every other query-parameter name is already a `_KEY_VALUE_RE`
# keyword.
_URL_PARAM_RE = re.compile(r"(?i)([?&]key=)[^&\s'\"<>]+")

# `X-JTS-Token` is `_KEY_VALUE_RE`'s `token`; `Household` is nobody else's.
_HEADER_RE = re.compile(r"(?i)\b(X-JTS-Household[ \t]*:[ \t]*)\S+")

# Live provider key prefixes: Google (AIza), OpenAI (sk-), xAI (xai-),
# Google OAuth client secret (GOCSPX-). Replaced, never masked — a masked
# tail is still live credential material once it reaches `/state`.
_KEY_PREFIX_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:AIza|sk-|xai-|GOCSPX-)[A-Za-z0-9_-]{8,}",
)

_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (_BEARER_RE, r"\1 <redacted>"),
    (_ENV_LINE_RE, r"\1<redacted>"),
    (_ENV_SQUOTED_RE, r"'\1<redacted>'"),
    (_ENV_DQUOTED_RE, r'"\1<redacted>"'),
    (_KEY_VALUE_RE, r"\1<redacted>"),
    (_SECRET_WORD_RE, r"\1 <redacted>"),
    (_URL_PARAM_RE, r"\1<redacted>"),
    (_HEADER_RE, r"\1<redacted>"),
    (_KEY_PREFIX_RE, "<redacted>"),
)


def redact_secrets(message: str) -> str:
    """Replace anything credential-shaped in `message` with `<redacted>`."""
    for pattern, replacement in _RULES:
        message = pattern.sub(replacement, message)
    return message
