# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Environment gates shared by paid or physically consequential voice evals."""

from __future__ import annotations

import os

import pytest


def playback_skip() -> bool:
    """Return whether the operator disabled real-world speaker actions."""

    return os.environ.get("JASPER_VOICE_EVAL_SKIP_PLAYBACK", "").strip() == "1"


def require_google(harness, *, product: str) -> None:
    """Skip when Google credentials or a linked account are unavailable."""

    clients = harness.test_state.get("google_clients")
    product_lower = product.lower()
    if clients is None:
        pytest.skip(
            "voice-eval: Google not configured (no GOOGLE_CLIENT_ID / "
            f"GOOGLE_CLIENT_SECRET) — {product_lower} tools not registered. Set "
            "the env + link an account at jts.local/google to run this."
        )
    if not clients.list_account_names():
        pytest.skip(
            "voice-eval: Google CLIENT_ID/SECRET set but no account "
            f"linked — {product_lower} tools not registered. Link one at "
            "jts.local/google to run this scenario."
        )
