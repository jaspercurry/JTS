# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pin the Python-side contract with the COMBO-mode host-slaved USB clock —
fan-in's copy of the Stage 1 servo (``rust/jasper-fanin/src/host_clock.rs`` +
its config knobs in ``rust/jasper-fanin/src/config.rs``).

This is the sole host-clock contract pin: fan-in's DIRECT capture is the only USB
ingress since the aloop solo path (and its usbsink-bridge host clock) was removed
2026-07-10. In combo mode (``JASPER_FANIN_USB_DIRECT=enabled``) fan-in owns the
``hw:UAC2Gadget`` capture, so per the
invariant *the daemon that owns the gadget capture owns the pitch ctl* it drives
the host-clock ladder. The ladder/probe/servo itself is the SHARED
``rust/jasper-host-clock`` crate (byte-identical to solo mode); this file pins
the ``JASPER_FANIN_HOST_CLOCK*`` env-key names + defaults + ranges (Rust-
daemon-local, outside any Python-side scanner's reach — this is the
dedicated pin).

The Rust-source grep-pins ``pytest.skip()`` if the fan-in sources are not
present yet, mirroring the usbsink twin's idiom so the Python side never blocks
the Rust side landing first.
"""
from __future__ import annotations

from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_FANIN_CONFIG_RS = _REPO / "rust" / "jasper-fanin" / "src" / "config.rs"
_FANIN_HOST_CLOCK_RS = _REPO / "rust" / "jasper-fanin" / "src" / "host_clock.rs"
_SHARED_HOST_CLOCK_RS = _REPO / "rust" / "jasper-host-clock" / "src" / "lib.rs"
_FANIN_UNIT = _REPO / "deploy" / "systemd" / "jasper-fanin.service"
_ENV_EXAMPLE = _REPO / ".env.example"


def _fanin_config_text() -> str:
    if not _FANIN_CONFIG_RS.exists():
        pytest.skip(f"rust source not present: {_FANIN_CONFIG_RS}")
    return _FANIN_CONFIG_RS.read_text(encoding="utf-8")


def _fanin_host_clock_text() -> str:
    if not _FANIN_HOST_CLOCK_RS.exists():
        pytest.skip(f"rust source not present: {_FANIN_HOST_CLOCK_RS}")
    return _FANIN_HOST_CLOCK_RS.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Env-key names + defaults, pinned against config.rs + .env.example prose.
# The live fan-in host-clock keys; there is NO target or probe-duration env.
# Correction mode carries no fill setpoint and uses a fixed probe window.
# --------------------------------------------------------------------------

_PINNED_ENV_KEYS = {
    "JASPER_FANIN_HOST_CLOCK": None,  # unset = disabled; no numeric default
    "JASPER_FANIN_HOST_CLOCK_PROBE_PPM": "300",
}


def test_every_pinned_env_key_is_declared_in_fanin_config():
    text = _fanin_config_text()
    for key in _PINNED_ENV_KEYS:
        assert key in text, (
            f"{key} is pinned as a combo host-clock env key but no longer "
            "appears in rust/jasper-fanin/src/config.rs — either it was renamed "
            "(update both sides) or removed."
        )


def test_every_pinned_env_key_is_mentioned_in_env_example():
    text = _ENV_EXAMPLE.read_text(encoding="utf-8")
    for key in _PINNED_ENV_KEYS:
        assert key in text, (
            f"{key} must have a prose-commented entry in .env.example per "
            "AGENTS.md 'Codify, don't memorise'."
        )


def test_pinned_numeric_defaults_appear_in_env_example_prose():
    text = _ENV_EXAMPLE.read_text(encoding="utf-8")
    for key, default in _PINNED_ENV_KEYS.items():
        if default is None:
            continue
        idx = text.index(key)
        window = text[idx: idx + 200]
        assert f"default {default}" in window, (
            f"{key}'s .env.example prose does not mention 'default {default}' "
            "within 200 chars of the key name."
        )


def test_fanin_host_clock_uses_the_shared_crate():
    # The fan-in adapter must compose the SHARED jasper_host_clock ladder, not a
    # forked copy of the servo — the whole point of the extraction.
    text = _fanin_host_clock_text()
    assert "jasper_host_clock" in text, (
        "fan-in host_clock.rs must import the shared jasper_host_clock crate "
        "(the daemon-agnostic ladder/servo), not re-implement it."
    )
    # And the shared crate carries the fragment fixture both daemons pin.
    shared = _SHARED_HOST_CLOCK_RS
    assert shared.exists(), f"shared crate missing: {shared}"
    assert "fn host_clock_fragment_shape_is_stable" in shared.read_text(encoding="utf-8"), (
        "the shared jasper-host-clock crate must still pin the wire fragment."
    )


def test_generation_lifecycle_and_bounded_retry_contract_is_explicit():
    adapter = _fanin_host_clock_text()
    shared = _SHARED_HOST_CLOCK_RS.read_text(encoding="utf-8")
    mixer = (_REPO / "rust" / "jasper-fanin" / "src" / "mixer.rs").read_text(
        encoding="utf-8"
    )

    assert "capture_generation: Arc<AtomicU64>" in adapter
    assert "capture_generation: Arc::clone(&direct_obs.opens)" in mixer, (
        "direct successful-open count must remain the capture-generation SSOT"
    )
    assert "self.control_generation == Some(capture_generation)" in adapter
    assert "event=fanin.host_clock_generation_mismatch" in adapter
    assert "event=fanin.host_clock_control_refresh_succeeded" in adapter
    assert "pub const CONTROL_REOPEN_INTERVAL_MS: u64 = 1_000;" in adapter
    assert "pub const MAX_PROBE_ATTEMPTS: u32 = 2;" in shared
    assert "pub const PROBE_RETRY_SETTLE_SECS: u64 = 10;" in shared
    assert "ProbePhase::RetryWait" in shared
    assert 'Some("probe_noncompliant")' in shared
    assert 'Some("lost_authority")' in shared
    assert 'Some("actuator_unavailable")' in shared
