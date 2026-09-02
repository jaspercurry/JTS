# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Strip credentials out of text bound for logs, ``/state`` or doctor.

Pattern-based, not value-based: the caller never needs the secret in
hand to scrub it. That is what lets the voice reconnect supervisor log a
provider's raw HTTP rejection body without ever opening the secret
compartment files.

Value-based masking — for callers that already hold the secret and want
it masked wherever it appears — lives in
``jasper.web.voice_setup._redact_provider_error``.
"""
from __future__ import annotations

import re

_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")

# The separator group absorbs any quoting around it, so a bare
# `api_key=x`, a `token: "x"` and a JSON `"token":"x"` all redact.
_KEY_VALUE_RE = re.compile(
    r"(?i)\b"
    r"(api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|"
    r"password|psk|token)"
    r"(['\"]?\s*[=:]\s*['\"]?)"
    r"([^'\"\s,;]+)"
)

# Live provider key prefixes: Google (AIza), OpenAI (sk-), xAI (xai-).
_KEY_PREFIX_RE = re.compile(r"\b(?:AIza|sk-|xai-)[A-Za-z0-9_-]{8,}")


def redact_secrets(message: str) -> str:
    """Replace anything credential-shaped in `message` with a placeholder."""
    message = _BEARER_RE.sub("Bearer <redacted>", message)
    message = _KEY_VALUE_RE.sub(
        lambda m: f"{m.group(1)}{m.group(2)}<redacted>",
        message,
    )
    return _KEY_PREFIX_RE.sub(
        lambda m: f"{m.group(0)[:4]}...{m.group(0)[-4:]}",
        message,
    )
