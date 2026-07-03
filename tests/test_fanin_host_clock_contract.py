# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pin the Python-side contract with the COMBO-mode host-slaved USB clock —
fan-in's copy of the Stage 1 servo (``rust/jasper-fanin/src/host_clock.rs`` +
its config knobs in ``rust/jasper-fanin/src/config.rs``).

Twin of ``tests/test_usbsink_host_clock_contract.py``. In combo mode
(``JASPER_FANIN_USB_DIRECT=enabled`` + ``JASPER_USBSINK_AUDIO_STANDBY=1``)
fan-in — not the usbsink bridge — owns the ``hw:UAC2Gadget`` capture, so per the
invariant *the daemon that owns the gadget capture owns the pitch ctl* it drives
the host-clock ladder. The ladder/probe/servo itself is the SHARED
``rust/jasper-host-clock`` crate (byte-identical to solo mode); this file pins
the two halves of the boundary this package's Python code consumes:

  * the ``JASPER_FANIN_HOST_CLOCK*`` env-key names + defaults + ranges (Rust-
    daemon-local, so ``tests/test_env_vars_codified.py``'s ``jasper/**`` scanner
    can't see them — this is the dedicated pin), and
  * the ``/state.audio_graph.fanin.host_clock`` pass-through shape
    (``jasper.control.state_aggregate._audio_graph_state``).

The Rust-source grep-pins ``pytest.skip()`` if the fan-in sources are not
present yet, mirroring the usbsink twin's idiom so the Python side never blocks
the Rust side landing first.

See docs/HANDOFF-usb-low-latency.md "USB DIRECT (combo mode)" →
"Host-slaved USB clock in combo mode".
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from jasper.control import state_aggregate

_REPO = Path(__file__).resolve().parents[1]
_FANIN_CONFIG_RS = _REPO / "rust" / "jasper-fanin" / "src" / "config.rs"
_FANIN_HOST_CLOCK_RS = _REPO / "rust" / "jasper-fanin" / "src" / "host_clock.rs"
_SHARED_HOST_CLOCK_RS = _REPO / "rust" / "jasper-host-clock" / "src" / "lib.rs"
_FANIN_UNIT = _REPO / "deploy" / "systemd" / "jasper-fanin.service"
_ENV_EXAMPLE = _REPO / ".env.example"

# The pinned disabled-block fragment for the combo (fan-in) daemon. It shares the
# shared-crate wire SHAPE with the usbsink twin, but the ONE field that differs by
# daemon is `obs_mode`: fan-in ALWAYS builds its config with `ObsMode::Correction`
# (a lane resampler sits between the gadget ring and the mix, so the fill slope is
# dead weight and the probe/servo run on the resampler's own correction ppm),
# whereas usbsink solo is `ObsMode::Fill`. `correction_ppm` is the additive
# CORRECTION-mode observable (0 while disabled). Combo boxes get exactly this shape
# under /state.audio_graph.fanin.host_clock.
_PINNED_HOST_CLOCK_FRAGMENT = (
    '{"enabled":false,"ladder":"disabled","obs_mode":"correction","pitch_ppm_commanded":0.0,'
    '"fill_frames":0,"fill_slope_ppm":0.00,"fill_variance":0.00,"correction_ppm":0.00,'
    '"dll":{"err_frames":0.00,"locked":false},'
    '"probe":{"last_result":"none","response_ratio":null,"waiting_for_lock":false},'
    '"demotions":0,"transitions":0,"last_transition_reason":"startup"}'
)


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
# The three JASPER_FANIN_HOST_CLOCK* keys mirror usbsink's; there is NO target
# env (the setpoint is the resampler's held target, derived).
# --------------------------------------------------------------------------

_PINNED_ENV_KEYS = {
    "JASPER_FANIN_HOST_CLOCK": None,  # unset = disabled; no numeric default
    "JASPER_FANIN_HOST_CLOCK_PROBE_PPM": "300",
    "JASPER_FANIN_HOST_CLOCK_PROBE_SECONDS": "6",
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


def test_fanin_host_clock_is_default_off_literal_gate():
    # The gate is the exact-`enabled` literal idiom (fail-safe, opt-in), and it
    # WARNS on a non-`enabled` non-empty value (unlike the sibling flags that
    # silently stay off) — mirroring the usbsink literal idiom so a typo leaves
    # a breadcrumb rather than silently disabling a safety feature.
    text = _fanin_config_text()
    assert 'std::env::var("JASPER_FANIN_HOST_CLOCK")' in text
    assert 'eq_ignore_ascii_case("enabled")' in text
    assert "event=fanin.host_clock_config_ignored" in text


def test_fanin_probe_ranges_match_usbsink_contract():
    # The two servos share one probe contract: ppm 200..=800, secs 5..=10.
    text = _fanin_config_text()
    assert "(200..=800).contains(&host_clock_probe_ppm)" in text, (
        "the fan-in probe-ppm range must be 200..=800 (the ~163 ppm Windows "
        "deadband floor + the ±1000 ppm validity ceiling), matching usbsink."
    )
    assert "(5..=10).contains(&host_clock_probe_secs)" in text, (
        "the fan-in probe-seconds range must be 5..=10, matching usbsink."
    )


def test_no_fanin_host_clock_target_env_key():
    # The setpoint is DERIVED from the resampler's held target (target +
    # cushion) — NOT a second env knob that could fight the inner loop. Pin the
    # absence so nobody reintroduces a target env.
    config = _fanin_config_text()
    host_clock = _fanin_host_clock_text()
    assert "JASPER_FANIN_HOST_CLOCK_TARGET" not in config, (
        "there must be NO JASPER_FANIN_HOST_CLOCK_TARGET env key — the setpoint "
        "is the resampler's held target (shared with the inner controller)."
    )
    assert "JASPER_FANIN_HOST_CLOCK_TARGET" not in host_clock
    # And the adapter derives the setpoint from the resampler's held target.
    assert "target_fill_frames" in host_clock, (
        "the fan-in host-clock adapter must derive its setpoint from the "
        "resampler's target_fill_frames (the held target)."
    )


def test_fanin_host_clock_runs_the_correction_observable_mode():
    # Combo mode must select `ObsMode::Correction` in build_config — the whole
    # point of this redesign. With a lane resampler between the gadget ring and
    # the mix, the fill slope is dead (the resampler flattens it), so the probe /
    # L0 servo run on the resampler's own correction ppm. usbsink solo stays FILL.
    text = _fanin_host_clock_text()
    assert "ObsMode::Correction" in text, (
        "fan-in build_config must pass ObsMode::Correction — the combo-mode "
        "probe/servo observable is the resampler correction ppm, not the fill "
        "slope (the resampler absorbs the host clock and flattens the fill)."
    )
    # And the correction observable is threaded from the resampler's live gauge.
    assert "correction_milli_ppm" in text, (
        "fan-in build_obs must decode the resampler's correction gauge "
        "(ratio_milli_ppm) into Obs.correction_ppm — the combo-mode observable."
    )
    # The shared crate must define the typed observable-mode enum both sides use.
    shared = _SHARED_HOST_CLOCK_RS.read_text(encoding="utf-8")
    assert "pub enum ObsMode" in shared, (
        "the shared jasper-host-clock crate must define the typed ObsMode enum "
        "(Fill / Correction) — the observable mode is explicit, not inferred."
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


# --------------------------------------------------------------------------
# /state.audio_graph.fanin.host_clock pass-through (C8).
# --------------------------------------------------------------------------


def _fanin_status_with_host_clock(host_clock_block) -> dict:
    return {
        "inputs": [
            {"label": "usbsink", "source": "direct", "resampler": {"armed": True}},
        ],
        "host_clock": host_clock_block,
    }


def test_audio_graph_passes_through_present_fanin_host_clock_block():
    block = json.loads(_PINNED_HOST_CLOCK_FRAGMENT)
    block["enabled"] = True
    block["ladder"] = "l0_locked"
    graph = state_aggregate._audio_graph_state(
        usbsink_raw=None,
        fanin_status=_fanin_status_with_host_clock(block),
        outputd_status=None,
    )
    assert graph is not None
    assert graph["fanin"]["host_clock"] == block


def test_audio_graph_fanin_host_clock_none_when_key_absent():
    # A combo build with no host_clock in the fan-in STATUS (feature never
    # rendered a block) → None, a definite "no evidence".
    graph = state_aggregate._audio_graph_state(
        usbsink_raw=None,
        fanin_status={"inputs": []},
        outputd_status=None,
    )
    assert graph is not None
    assert graph["fanin"]["host_clock"] is None


def test_audio_graph_fanin_host_clock_none_when_fanin_status_none():
    graph = state_aggregate._audio_graph_state(
        usbsink_raw=None,
        fanin_status=None,
        outputd_status=None,
    )
    assert graph is not None
    assert graph["fanin"]["host_clock"] is None


def test_audio_graph_fanin_host_clock_none_when_fanin_status_not_a_dict():
    # Defensive: a malformed fan-in status must degrade to None, not raise.
    graph = state_aggregate._audio_graph_state(
        usbsink_raw=None,
        fanin_status="not-a-dict",  # type: ignore[arg-type]
        outputd_status=None,
    )
    assert graph is not None
    assert graph["fanin"]["host_clock"] is None


# --------------------------------------------------------------------------
# MSRV guard — the fan-in host_clock.rs is ALSA-feature code that cannot compile
# on the macOS dev host, so a post-1.75 std API there only fails on Linux CI.
# Same cheap grep-pin as the usbsink twin.
# --------------------------------------------------------------------------

_POST_MSRV_STD_METHODS = (
    (".is_none_or(", "1.82"),
    (".div_ceil(", "1.73 slice / 1.79 NonZero"),
    (".next_multiple_of(", "1.73"),
    (".isqrt(", "1.84"),
    (".midpoint(", "1.85"),
    (".trim_ascii(", "1.80"),
    (".split_at_checked(", "1.80"),
    (".last_chunk(", "1.80"),
)


def test_fanin_host_clock_uses_no_post_msrv_std_apis():
    text = _fanin_host_clock_text()
    cargo = (_REPO / "rust" / "jasper-fanin" / "Cargo.toml").read_text(encoding="utf-8")
    assert 'rust-version = "1.75"' in cargo, (
        "jasper-fanin's declared MSRV changed — update this guard's method list."
    )
    offenders: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.lstrip()
        if stripped.startswith("//"):
            continue
        for token, since in _POST_MSRV_STD_METHODS:
            if token in line:
                offenders.append(
                    f"host_clock.rs:{lineno}: {token!r} (std-stable since {since}) "
                    "exceeds MSRV 1.75 — clippy incompatible_msrv will fail CI."
                )
    assert not offenders, "post-MSRV std API in jasper-fanin host_clock.rs:\n" + "\n".join(
        offenders
    )
