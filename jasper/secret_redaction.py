# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The one Python redactor for text bound for logs, ``/state`` or doctor.

See ADR-0240.
"""
from __future__ import annotations

import re

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

_BEARER_RE = re.compile(r"(?i)\b(Bearer)\s+[A-Za-z0-9._~+/=-]{8,}")

# The left boundary is a lookbehind, not `\b`: `\b` cannot match between
# `_` and a letter, so it never saw the project's own `*_API_KEY` names.
# `[ \t]` around the separator, not `\s`, so it cannot cross a newline.
# `)` and `&` stay out of the bare run so `repr(e)` keeps its paren and a
# query string keeps the parameters after the secret. `(?<![-.])` spares
# NetworkManager's `802-11-wireless-security.psk: <reason>` error text.
_KEY_VALUE_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])"
    r"((?:api[_-]?key|bustime[_-]?key|token|secret|password|passphrase"
    r"|x-jts-household|(?<![-.])psk)"
    r"['\"]?[ \t]*[=:][ \t]*)"
    rf"(?:{_QUOTED}|[^'\"\s,;}}\])&]+)",
)

# 8 is the WPA passphrase minimum; a shorter run swallows prose such as
# "password reset link sent". `wpa-psk` is a key-mgmt value, not a secret.
_SECRET_WORD_RE = re.compile(
    rf"(?i)\b(password|(?<!wpa-)psk)[ \t]+(?:{_QUOTED}|\S{{8,}})",
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
    (_KEY_VALUE_RE, r"\1<redacted>"),
    (_SECRET_WORD_RE, r"\1 <redacted>"),
    (_URL_PARAM_RE, r"\1<redacted>"),
    (_KEY_PREFIX_RE, "<redacted>"),
)


def redact_secrets(message: str) -> str:
    """Replace anything credential-shaped in `message` with `<redacted>`."""
    for pattern, replacement in _RULES:
        message = pattern.sub(replacement, message)
    return message
