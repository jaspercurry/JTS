# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pin the Python-side contract with the Rust host-slaved USB clock module
(``rust/jasper-usbsink-audio/src/host_clock.rs``) that this package does NOT
own or edit.

Stage 1 adds a default-OFF ladder that commands the host's USB audio clock
(the gadget's writable "Capture Pitch 1000000" ALSA ctl) to close the
standing rate offset a wired USB source accumulates against the Pi's own
clock. The Rust bridge owns the DLL servo, the per-session compliance probe,
and the ctl writes; this file's job is to pin the two halves of the boundary
this package's Python code actually consumes:

  * the ``host_clock`` JSON block's shape inside ``state.json`` (parsed by
    ``jasper.control.state_aggregate._audio_graph_state`` as a verbatim
    pass-through — no dedicated dataclass reader exists on the Python side,
    unlike the Stage 0 impulse tap, because /state consumers want the raw
    ladder/DLL/probe telemetry rather than a typed wrapper), and
  * the env-key names + defaults + ladder/probe enum vocabulary that
    downstream Python (the doctor check, docs) must agree with the Rust
    daemon on.

This is NOT a test of the Rust implementation (out of scope — the other
implementer owns ``rust/jasper-usbsink-audio/**``); it is a test that OUR
side of the interface matches the documented contract. The Rust-source
grep-pins below ``pytest.skip()`` if ``host_clock.rs`` is not present yet
(the two implementers work in parallel — see
``tests/test_fanin_coupling_rust_contract.py`` for the same idiom), so this
file never blocks the Python side landing first; once the Rust module lands,
these same tests hold both halves to the pinned contract.

See docs/HANDOFF-usb-low-latency.md "Host-slaved USB clock (Stage 1)" for
the full mechanism/ladder/cross-platform-condition writeup.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from jasper.cli.doctor import usbsink as doctor_usbsink
from jasper.control import state_aggregate

_REPO = Path(__file__).resolve().parents[1]
_HOST_CLOCK_RS = _REPO / "rust" / "jasper-usbsink-audio" / "src" / "host_clock.rs"
_ENV_EXAMPLE = _REPO / ".env.example"


def _host_clock_rs_text() -> str:
    if not _HOST_CLOCK_RS.exists():
        pytest.skip(f"rust source not present: {_HOST_CLOCK_RS}")
    return _HOST_CLOCK_RS.read_text(encoding="utf-8")


# The Rust fixture's `assert_eq!` literal is a JSON string wrapped across
# several PHYSICAL source lines for .rs-file readability, using two Rust
# string-literal escapes (https://doc.rust-lang.org/reference/tokens.html
# #string-literals):
#   * `\"` for every quote inside the embedded JSON (the literal is itself
#     a double-quoted Rust string, so its JSON content's `"` must be
#     escaped);
#   * a trailing `\` immediately before a newline — Rust's "string
#     continuation" escape — which tells rustc to elide the newline AND any
#     leading whitespace on the next source line, so the compiled string
#     value has no line breaks despite the source spanning several lines.
# A plain substring search over the raw file text (which still contains the
# real backslashes/newlines/indentation, since Python never compiles the
# Rust source) can never match a wrapped literal like this. These two
# substitutions undo exactly those two escapes — nothing else — so the
# `.py` and `.rs` fixtures can be compared byte-for-byte after decoding
# rather than by hand-picking one hardcoded line range (robust to the
# literal being re-wrapped differently later).
_RUST_STRING_CONTINUATION_RE = re.compile(r"\\\n[ \t]*")


def _decode_rust_string_continuations(raw_source_slice: str) -> str:
    joined = _RUST_STRING_CONTINUATION_RE.sub("", raw_source_slice)
    return joined.replace('\\"', '"')


# --------------------------------------------------------------------------
# The pinned `host_clock` JSON block shape (contracts §1). This exact
# fragment, embedded inside a full state.json document the way the real
# Rust `status_json` embeds the `tap` fragment, is the cross-language wire
# fixture: the Rust crate's own `host_clock_fragment_shape_is_stable` test
# asserts this identical string verbatim. If either side's serialization
# format ever drifts (spacing, key order, field set), one of the two pinned
# tests should catch it. Rust's compact `format!` style never puts spaces
# after `:`/`,` (verified against status_json/status_fragment in
# main.rs/impulse_tap.rs), so this fixture is deliberately compact too.
# --------------------------------------------------------------------------

_PINNED_HOST_CLOCK_FRAGMENT = (
    '{"enabled":false,"ladder":"disabled","pitch_ppm_commanded":0.0,'
    '"fill_frames":0,"fill_slope_ppm":0.00,"fill_variance":0.00,'
    '"dll":{"err_frames":0.00,"locked":false},'
    '"probe":{"last_result":"none","response_ratio":null},'
    '"demotions":0,"transitions":0,"last_transition_reason":"startup"}'
)


def test_pinned_host_clock_fragment_is_valid_json_with_contract_shape():
    obj = json.loads(_PINNED_HOST_CLOCK_FRAGMENT)

    assert obj == {
        "enabled": False,
        "ladder": "disabled",
        "pitch_ppm_commanded": 0.0,
        "fill_frames": 0,
        "fill_slope_ppm": 0.0,
        "fill_variance": 0.0,
        "dll": {"err_frames": 0.0, "locked": False},
        "probe": {"last_result": "none", "response_ratio": None},
        "demotions": 0,
        "transitions": 0,
        "last_transition_reason": "startup",
    }


def test_pinned_host_clock_fragment_matches_rust_fixture_verbatim():
    # Cross-language: the Rust source's own test embeds this identical byte
    # string (its `assert_eq!` literal is wrapped across several source
    # lines using Rust's string-continuation escape purely for .rs-file
    # readability — decode that one escape before comparing, see
    # _decode_rust_string_continuations). A drift in either side's compact-
    # JSON formatting (a stray space, reordered keys, a renamed field, a
    # different float precision) fails here.
    rust_src = _decode_rust_string_continuations(_host_clock_rs_text())
    assert _PINNED_HOST_CLOCK_FRAGMENT in rust_src, (
        "the pinned host_clock JSON fragment is no longer byte-identical to "
        "the Rust crate's own fixture (host_clock_fragment_shape_is_stable) "
        "— update BOTH sides together, they are a single wire contract"
    )


def test_rust_fixture_test_still_exists():
    # Cross-reference: the Rust crate must still carry the test that pins
    # this fragment on its side, or a Rust-side deletion would silently
    # make this Python pin the only remaining evidence (and eventually
    # stale, since nothing on the Rust side would re-verify it).
    rust_src = _host_clock_rs_text()
    assert "fn host_clock_fragment_shape_is_stable" in rust_src, (
        "Rust fixture pinning the host_clock JSON fragment is gone — the "
        "Stage 1 state.json wire contract is unpinned on the Rust side."
    )


def test_full_state_json_embeds_host_clock_as_sibling_of_tap():
    # A realistic full state.json document (mirroring status_json's shape:
    # ...,"tap":{...},"host_clock":{...},"last_progress_epoch_ms":N}) must
    # still parse and expose host_clock as a top-level sibling of tap, per
    # contracts §1 ("top-level key ... sibling of tap").
    document = (
        '{"schema_version":1,"implementation":"rust","updated_at":"2026-07-02T00:00:00.000000Z",'
        '"playing":false,"preempted":false,"host_connected":false,"rms_dbfs":-120.00,'
        '"capture_device":"hw:UAC2Gadget","playback_device":"usbsink_substream",'
        '"sample_rate":48000,"channels":2,"period_frames":256,'
        '"ring":{"fill_periods":0,"capacity_periods":3},'
        '"counters":{"capture_xruns":0,"capture_partial_reads":0,"playback_xruns":0,'
        '"underflow_periods":0,"overflow_events":0,"dropped_periods":0,'
        '"preempt_silence_periods":0,"preempt_dropped_periods":0,'
        '"capture_frames":0,"playback_frames":0},'
        '"tap":{"armed":false,"events_written":0,"events_dropped":0,"threshold":0.200,'
        '"refractory_ms":250,"max_events":4000,"auto_disarm_at_epoch_ms":0,'
        '"path":"/run/jasper-usbsink/impulse-tap.jsonl"},'
        f'"host_clock":{_PINNED_HOST_CLOCK_FRAGMENT},'
        '"last_progress_epoch_ms":0}\n'
    )
    parsed = json.loads(document)
    assert "host_clock" in parsed
    assert parsed["host_clock"] == json.loads(_PINNED_HOST_CLOCK_FRAGMENT)
    # Sibling of tap, not nested inside it or replacing it.
    assert "tap" in parsed
    assert parsed["tap"] != parsed["host_clock"]


# --------------------------------------------------------------------------
# state_aggregate pass-through: /state.audio_graph.rust_bridge.host_clock
# --------------------------------------------------------------------------


def test_audio_graph_state_passes_through_present_host_clock_block():
    host_clock_block = json.loads(_PINNED_HOST_CLOCK_FRAGMENT)
    host_clock_block["enabled"] = True
    host_clock_block["ladder"] = "l0_locked"

    graph = state_aggregate._audio_graph_state(
        usbsink_raw={
            "implementation": "rust",
            "period_frames": 256,
            "ring": {"fill_periods": 1, "capacity_periods": 3},
            "counters": {},
            "host_clock": host_clock_block,
        },
        fanin_status=None,
        outputd_status=None,
    )

    assert graph is not None
    assert graph["rust_bridge"]["host_clock"] == host_clock_block


def test_audio_graph_state_host_clock_is_none_when_block_absent():
    # Pre-Stage-1 build shape: usbsink_raw has no host_clock key at all.
    graph = state_aggregate._audio_graph_state(
        usbsink_raw={
            "implementation": "rust",
            "period_frames": 256,
            "ring": {"fill_periods": 1, "capacity_periods": 3},
            "counters": {},
        },
        fanin_status=None,
        outputd_status=None,
    )

    assert graph is not None
    assert graph["rust_bridge"]["host_clock"] is None


def test_audio_graph_state_host_clock_is_none_when_usbsink_raw_is_none():
    # Daemon not running / state file unreadable: usbsink_raw itself is None.
    graph = state_aggregate._audio_graph_state(
        usbsink_raw=None,
        fanin_status=None,
        outputd_status=None,
    )

    assert graph is not None
    assert graph["rust_bridge"]["host_clock"] is None


def test_audio_graph_state_host_clock_is_none_when_usbsink_raw_not_a_dict():
    # Defensive: a malformed usbsink_raw (e.g. a bare bool/string from a
    # corrupted read) must not raise — every rust_bridge field degrades to
    # None the same way the pre-existing fields already do.
    graph = state_aggregate._audio_graph_state(
        usbsink_raw="not-a-dict",  # type: ignore[arg-type]
        fanin_status=None,
        outputd_status=None,
    )

    assert graph is not None
    assert graph["rust_bridge"]["host_clock"] is None


# --------------------------------------------------------------------------
# Ladder / probe enum vocabulary (contracts §3) — exact lowercase snake
# strings the doctor check and any future consumer must match verbatim.
# --------------------------------------------------------------------------


_PINNED_LADDER_VALUES = (
    "disabled",
    "probing",
    "l0_locked",
    "l1_warn",
    "l2_fallback",
)

_PINNED_PROBE_RESULTS = ("none", "pass", "fail", "aborted")


def test_pinned_ladder_values_are_lowercase_snake():
    for value in _PINNED_LADDER_VALUES:
        assert value == value.lower()
        assert " " not in value


def test_doctor_check_recognizes_every_pinned_ladder_value(monkeypatch, tmp_path):
    # check_usbsink_host_clock's warn/ok branching is keyed on exact ladder
    # strings ("l2_fallback", "l1_warn"); every other pinned value must fall
    # through to "ok" without raising, so a future ladder addition that
    # isn't yet handled degrades to visible-but-unclassified rather than a
    # doctor crash. Exercises the REAL check function against every pinned
    # (ladder, probe_result) combination, not just the dict shape.
    monkeypatch.setattr(doctor_usbsink, "_systemd_is_active", lambda unit: True)
    for ladder in _PINNED_LADDER_VALUES:
        for probe_result in _PINNED_PROBE_RESULTS:
            state_path = tmp_path / f"state-{ladder}-{probe_result}.json"
            state_path.write_text(json.dumps({
                "host_clock": {
                    "enabled": True,
                    "ladder": ladder,
                    "pitch_ppm_commanded": 0.0,
                    "fill_frames": 100,
                    "fill_slope_ppm": 0.0,
                    "fill_variance": 0.0,
                    "dll": {"err_frames": 0.0, "locked": False},
                    "probe": {
                        "last_result": probe_result,
                        "response_ratio": None,
                    },
                    "demotions": 0,
                    "transitions": 0,
                    "last_transition_reason": "startup",
                },
            }))
            with patch.object(doctor_usbsink, "Path") as mock_path:
                def _resolve(p, _target=state_path):
                    if p == "/run/jasper-usbsink/state.json":
                        return _target
                    return Path(p)
                mock_path.side_effect = _resolve

                result = doctor_usbsink.check_usbsink_host_clock()

            assert result.status in {"ok", "warn"}, (
                f"ladder={ladder!r} probe_result={probe_result!r} produced "
                f"status={result.status!r} — the check must never fail "
                "(default-OFF, ladder-only telemetry) or raise."
            )
            if ladder in {"l2_fallback", "l1_warn"}:
                assert result.status == "warn"
            else:
                assert result.status == "ok"


def test_rust_source_still_names_every_pinned_ladder_variant():
    # Cross-check: if the Rust enum ever drops or renames a ladder variant,
    # the Python-side pins above would silently stop covering real states.
    rust_src = _host_clock_rs_text()
    for ladder in _PINNED_LADDER_VALUES:
        assert f'"{ladder}"' in rust_src, (
            f"ladder value {ladder!r} (pinned in _PINNED_LADDER_VALUES) no "
            "longer appears as a JSON string literal in host_clock.rs — "
            "the Rust ladder enum may have renamed/removed a variant."
        )


# --------------------------------------------------------------------------
# Env-key names + defaults, pinned against .env.example prose (contracts
# §2). These are Rust-daemon-local (never touch jasper/config.py, per the
# existing JASPER_USBSINK_* pattern), so tests/test_env_vars_codified.py's
# jasper/**/*.py scanner cannot see them; this is the dedicated pin.
# --------------------------------------------------------------------------

_PINNED_ENV_KEYS_AND_DEFAULTS = {
    "JASPER_USBSINK_HOST_CLOCK": None,  # unset = disabled; no numeric default
    "JASPER_USBSINK_HOST_CLOCK_TARGET_FILL_FRAMES": "384",
    "JASPER_USBSINK_HOST_CLOCK_PROBE_PPM": "300",
    "JASPER_USBSINK_HOST_CLOCK_PROBE_SECONDS": "6",
}


def test_every_pinned_env_key_is_mentioned_in_env_example():
    text = _ENV_EXAMPLE.read_text(encoding="utf-8")
    for key in _PINNED_ENV_KEYS_AND_DEFAULTS:
        assert key in text, (
            f"{key} must have a prose-commented entry in .env.example per "
            "AGENTS.md 'Codify, don't memorise' — every new JASPER_* env "
            "var needs a discoverable home."
        )


def test_pinned_numeric_defaults_appear_in_env_example_prose():
    text = _ENV_EXAMPLE.read_text(encoding="utf-8")
    for key, default in _PINNED_ENV_KEYS_AND_DEFAULTS.items():
        if default is None:
            continue
        # The prose states "(default N ...)" right after the key name; a
        # loose substring check (not a strict regex) keeps this robust to
        # prose rewording while still catching a stale/renumbered default.
        idx = text.index(key)
        window = text[idx: idx + 200]
        assert f"default {default}" in window, (
            f"{key}'s .env.example prose does not mention 'default {default}' "
            f"within 200 chars of the key name — either the documented "
            f"default drifted from the pinned Rust default, or the prose "
            f"needs rewording near the key."
        )


def test_doctor_check_reads_the_pinned_target_fill_frames_default():
    # The doctor's fallback literal ("384") must match the pinned Rust
    # default exactly, or a Pi with the env var genuinely unset would show
    # a fill=.../WRONG_NUMBER detail that doesn't match the daemon's real
    # servo target.
    doctor_src = Path(doctor_usbsink.__file__).read_text(encoding="utf-8")
    pinned_default = _PINNED_ENV_KEYS_AND_DEFAULTS[
        "JASPER_USBSINK_HOST_CLOCK_TARGET_FILL_FRAMES"
    ]
    assert f'"{pinned_default}"' in doctor_src, (
        "check_usbsink_host_clock's target-fill-frames fallback default "
        f"must be the string literal {pinned_default!r}, matching the "
        "pinned Rust daemon default."
    )


def test_rust_source_declares_every_pinned_env_key():
    rust_src = _host_clock_rs_text()
    for key in _PINNED_ENV_KEYS_AND_DEFAULTS:
        assert key in rust_src, (
            f"{key} is pinned as a Stage 1 env key but no longer appears in "
            "host_clock.rs — either the Rust side renamed it (update both "
            "sides together) or the key was removed."
        )


# --------------------------------------------------------------------------
# Non-env constants pinned by contracts §2 — servo clamp, write-suppression
# epsilon/cadence, tick interval. These are NOT env-tunable, so there is no
# .env.example entry; the only place to pin them is against the Rust source
# itself once it exists.
# --------------------------------------------------------------------------


def test_rust_source_pins_the_documented_non_env_constants():
    # Match the actual named-constant declarations (not a bare numeric
    # substring like "1000", which appears constantly throughout the file
    # for unrelated reasons and would make this pin nearly vacuous).
    rust_src = _host_clock_rs_text()
    assert "PITCH_NEUTRAL: i64 = 1_000_000;" in rust_src, (
        "PITCH_NEUTRAL constant (neutral pitch = 1_000_000) not declared "
        "as expected in host_clock.rs"
    )
    assert "MAX_BIAS_PPM: f64 = 1000.0;" in rust_src, (
        "MAX_BIAS_PPM constant (the ±1000 ppm servo clamp, independent of "
        "the wider hw range 750000..1005000) not declared as expected in "
        "host_clock.rs"
    )
    assert "WRITE_EPSILON_PPM: f64 = 10.0;" in rust_src, (
        "WRITE_EPSILON_PPM constant (the ctl write-suppression epsilon) "
        "not declared as expected in host_clock.rs"
    )
    assert "WRITE_MIN_INTERVAL_MS: u64 = 1000;" in rust_src, (
        "WRITE_MIN_INTERVAL_MS constant (the <=1 Hz ctl write cadence "
        "cap) not declared as expected in host_clock.rs"
    )


# --------------------------------------------------------------------------
# Bandwidth derivation (contracts §5) — the cascade defense. Re-derive the
# numbers independently here (not just trust the docs prose) so a future
# change to either loop's constants is caught by re-running the math, not
# just by a stale comment surviving unnoticed.
# --------------------------------------------------------------------------


def test_outer_loop_bandwidth_separation_from_inner_lane_resampler():
    # Inner loop (rust/jasper-fanin/src/lane_resampler.rs via
    # RateController::with_max_resync -> DllConfig::for_rate): adaptive
    # bandwidth clamped to [BW_MIN, BW_MAX] = [0.016, 0.128] Hz
    # (rust/jasper-clock/src/lib.rs).
    bw_min = 0.016
    bw_max = 0.128

    # Outer loop (this module): DllConfig{period:4800.0, rate:48000.0,
    # bw_retune_period:0 (fixed, no adaptive retune)} ticked at exactly
    # 1 Hz. Effective bandwidth = bw * (period/rate) / T_tick.
    outer_period = 4800.0
    outer_rate = 48000.0
    tick_interval_sec = 1.0
    outer_effective_bw = bw_min * (outer_period / outer_rate) / tick_interval_sec

    assert outer_effective_bw == pytest.approx(0.0016)
    # >= 10x separation from the inner loop's LOCKED floor (its narrowest,
    # most jitter-rejecting state) ...
    assert bw_min / outer_effective_bw == pytest.approx(10.0)
    # ... and >= 10x separation from the inner loop's ACQUIRING maximum
    # (its widest, fastest-locking state) too — i.e. >=10x separation in
    # EVERY inner-loop state, not just the locked floor.
    assert bw_max / outer_effective_bw == pytest.approx(80.0)
    assert bw_min / outer_effective_bw >= 10.0
    assert bw_max / outer_effective_bw >= 10.0


def test_rust_clock_crate_bandwidth_clamp_constants_match_derivation_inputs():
    # Cross-check the derivation's assumed BW_MIN/BW_MAX against the actual
    # jasper-clock crate constants, so the derivation test above can't
    # silently drift from the real inner-loop clamp.
    clock_lib = _REPO / "rust" / "jasper-clock" / "src" / "lib.rs"
    assert clock_lib.exists(), f"jasper-clock crate missing: {clock_lib}"
    text = clock_lib.read_text(encoding="utf-8")
    assert "pub const BW_MAX: f64 = 0.128;" in text
    assert "pub const BW_MIN: f64 = 0.016;" in text


def test_fanin_period_frames_default_matches_derivation_citation():
    # The derivation's "inner loop updates once per 256-frame period at
    # 48000 Hz" premise depends on JASPER_FANIN_PERIOD_FRAMES's default;
    # pin it so a fan-in config change doesn't silently invalidate the
    # cited bandwidth-separation math without anyone noticing.
    fanin_config = _REPO / "rust" / "jasper-fanin" / "src" / "config.rs"
    assert fanin_config.exists(), f"jasper-fanin crate missing: {fanin_config}"
    text = fanin_config.read_text(encoding="utf-8")
    assert 'env_u32("JASPER_FANIN_PERIOD_FRAMES", 256)' in text, (
        "JASPER_FANIN_PERIOD_FRAMES default drifted from 256 — the cited "
        "bandwidth-separation derivation (docs/HANDOFF-usb-low-latency.md "
        "'Host-slaved USB clock') assumes this exact value."
    )
