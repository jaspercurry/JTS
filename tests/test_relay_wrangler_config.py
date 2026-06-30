# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Static guards for the Cloudflare Worker deployment config."""
from __future__ import annotations

import tomllib
from pathlib import Path


def _wrangler_config() -> dict:
    path = Path(__file__).resolve().parents[1] / "relay" / "wrangler.toml"
    return tomllib.loads(path.read_text(encoding="utf-8"))


def test_relay_ratelimit_binding_uses_wrangler_ratelimits_table():
    cfg = _wrangler_config()
    assert "ratelimit" not in cfg

    bindings = cfg.get("ratelimits")
    assert isinstance(bindings, list)
    assert len(bindings) == 1

    binding = bindings[0]
    assert binding["name"] == "RELAY_RATELIMIT"
    assert binding["namespace_id"] == "1001"
    assert binding["simple"] == {"limit": 80, "period": 10}
