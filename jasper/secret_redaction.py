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

# systemd `Environment="NAME=value with spaces"`.
_ENV_QUOTED_RE = re.compile(rf"(['\"])({SECRET_ENV_NAME_RE}=)[^'\"]*\1")

_BEARER_RE = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}")

# The left boundary is a lookbehind, not `\b`: `\b` cannot match between
# `_` and a letter, so it never saw the project's own `*_API_KEY` names.
# The leading group absorbs owner prefixes so the full name survives.
_KEY_VALUE_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])"
    r"((?:[A-Za-z0-9-]+_)*"
    r"(?:api[_-]?key|bustime[_-]?key|token|secret|password|passphrase|psk)"
    r"['\"]?\s*[=:]\s*)"
    r"(?:(['\"])[^'\"]*\2|[^'\"\s,;}\]]+)",
)

# 8 is the WPA passphrase minimum; a shorter run swallows prose such as
# "password reset link sent".
_SECRET_WORD_RE = re.compile(
    r"(?i)\b(password|psk)\s+(?:(['\"])[^'\"]*\2|\S{8,})",
)

_URL_PARAM_RE = re.compile(
    r"(?i)([?&](?:key|api[_-]?key|token|access[_-]?token)=)[^&\s'\"<>]+",
)

_HEADER_RE = re.compile(r"(?i)\b(X-JTS-(?:Token|Household)\s*:\s*)\S+")

# Live provider key prefixes: Google (AIza), OpenAI (sk-), xAI (xai-),
# Google OAuth client secret (GOCSPX-). Replaced, never masked — a masked
# tail is still live credential material once it reaches `/state`.
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
    (_HEADER_RE, r"\1<redacted>"),
    (_KEY_PREFIX_RE, "<redacted>"),
)


def redact_secrets(message: str) -> str:
    """Replace anything credential-shaped in `message` with `<redacted>`."""
    for pattern, replacement in _RULES:
        message = pattern.sub(replacement, message)
    return message
