# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pin the loud-output safety literals documented in HANDOFF-volume.md.

Two hardware-safety claims (docs/HANDOFF-volume.md,
"Hearing-safety belt") had no test asserting the actual literals:

1. There is intentionally no fixed max-gain ceiling in the TTS path.
   Assistant loudness should follow the measured content target and the
   dynamic peak-aware cap; the old -6 dB source-gain clamp made quiet,
   peaky voices inaudible against music.

2. ``volume_limit: 0.0`` "in every JTS CamillaDSP YAML". The Python
   config *emitters* raise on a positive limit (tests exist), but the
   checked-in static configs under ``deploy/camilladsp/`` had no guard
   beyond a substring check on one file — a positive value edited into
   ``v1.yml`` would ship. This parses every deploy YAML and fails on a
   missing or positive ``volume_limit``.

Rust literals are grep-pinned (cargo is not available in every dev
environment — same technique as ``tests/test_outputd_wiring.py``).
"""
from __future__ import annotations

import math
import re
from pathlib import Path

import pytest
import yaml
from yaml.nodes import MappingNode, ScalarNode

from jasper.audio_io import TtsPlayout

REPO = Path(__file__).resolve().parents[1]
REMOVED_TTS_MAX_SYMBOL = "MAX_" + "TTS_GAIN_DB"

RUST_TTS_GAIN_FILES = (
    "rust/jasper-tts-protocol/src/loudness.rs",
    "rust/jasper-fanin/src/loudness.rs",
    "rust/jasper-outputd/src/loudness.rs",
    "rust/jasper-outputd/src/mixer.rs",
)

RUST_SHARED_LOUDNESS_SHIMS = (
    "rust/jasper-fanin/src/loudness.rs",
    "rust/jasper-outputd/src/loudness.rs",
)

RUST_OUTPUTD_MIXER = "rust/jasper-outputd/src/mixer.rs"

RUST_OUTPUTD_MIXER_GAIN_SYMBOLS = (
    "apply_gain_i16",
    "sanitize_tts_gain_db",
    "gain_db_to_linear",
    "DEFAULT_TTS_GAIN_DB",
    "MIN_TTS_GAIN_DB",
)

_RUST_SHARED_LOUDNESS_USE_PAT = re.compile(
    r"^pub use jasper_tts_protocol::loudness::\{(?P<body>[^}]+)\};",
    re.MULTILINE | re.DOTALL,
)


def test_fixed_tts_gain_ceiling_is_removed() -> None:
    for rel in RUST_TTS_GAIN_FILES:
        text = (REPO / rel).read_text()
        assert REMOVED_TTS_MAX_SYMBOL not in text, (
            f"{rel}: reintroduced a fixed max TTS gain ceiling. Assistant "
            "loudness should be governed by the measured target plus the "
            "dynamic peak-aware cap, not a universal source-gain clamp."
        )
    assert not hasattr(TtsPlayout, REMOVED_TTS_MAX_SYMBOL)


def test_rust_daemon_loudness_modules_reexport_shared_tts_gain_policy() -> None:
    for rel in RUST_SHARED_LOUDNESS_SHIMS:
        text = (REPO / rel).read_text()
        assert "pub use jasper_tts_protocol::loudness::*;" in text, (
            f"{rel}: daemon loudness module no longer re-exports the "
            "shared Rust policy. If this is intentional, update the safety "
            "pin and explain how fan-in/outputd loudness policy still cannot "
            "drift between daemons."
        )


def test_outputd_mixer_reexports_shared_tts_gain_helpers() -> None:
    text = (REPO / RUST_OUTPUTD_MIXER).read_text()
    match = _RUST_SHARED_LOUDNESS_USE_PAT.search(text)
    assert match, (
        f"{RUST_OUTPUTD_MIXER}: outputd mixer no longer re-exports TTS gain "
        "helpers from jasper_tts_protocol::loudness. If this is intentional, "
        "update the safety pin and explain how outputd helper callers still "
        "cannot drift from the shared assistant loudness policy."
    )
    exported = set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", match.group("body")))
    missing = sorted(set(RUST_OUTPUTD_MIXER_GAIN_SYMBOLS) - exported)
    assert not missing, (
        f"{RUST_OUTPUTD_MIXER}: missing shared TTS gain re-export(s): "
        f"{', '.join(missing)}"
    )
    for symbol in RUST_OUTPUTD_MIXER_GAIN_SYMBOLS:
        assert not re.search(rf"^(?:pub\s+)?fn\s+{symbol}\b", text, re.MULTILINE), (
            f"{RUST_OUTPUTD_MIXER}: {symbol} is locally defined instead of "
            "re-exported from jasper_tts_protocol::loudness."
        )
        assert not re.search(
            rf"^(?:pub\s+)?const\s+{symbol}\b", text, re.MULTILINE
        ), (
            f"{RUST_OUTPUTD_MIXER}: {symbol} is locally defined instead of "
            "re-exported from jasper_tts_protocol::loudness."
        )


def _single_mapping_value(node: MappingNode, key: str, *, label: str):
    values = [
        value_node
        for key_node, value_node in node.value
        if isinstance(key_node, ScalarNode) and key_node.value == key
    ]
    assert len(values) == 1, (
        f"{label}: expected exactly one {key!r} key, found {len(values)}"
    )
    return values[0]


def _static_devices_volume_limit(text: str, *, label: str) -> float:
    root = yaml.compose(text, Loader=yaml.SafeLoader)
    assert isinstance(root, MappingNode), f"{label}: root must be a YAML mapping"
    devices = _single_mapping_value(root, "devices", label=label)
    assert isinstance(devices, MappingNode), f"{label}: devices must be a mapping"
    volume_limit = _single_mapping_value(
        devices,
        "volume_limit",
        label=f"{label}: devices",
    )
    assert isinstance(volume_limit, ScalarNode), (
        f"{label}: devices.volume_limit must be a scalar"
    )
    assert volume_limit.tag in {
        "tag:yaml.org,2002:float",
        "tag:yaml.org,2002:int",
    }, f"{label}: devices.volume_limit must be a YAML number"
    try:
        value = float(volume_limit.value)
    except ValueError as exc:
        raise AssertionError(
            f"{label}: devices.volume_limit must be numeric"
        ) from exc
    assert math.isfinite(value), f"{label}: devices.volume_limit must be finite"
    return value


def test_every_static_camilladsp_config_caps_volume_at_zero_db() -> None:
    yamls = sorted((REPO / "deploy" / "camilladsp").glob("*.yml"))
    assert yamls, "deploy/camilladsp/*.yml not found (moved?)"
    for path in yamls:
        rel = path.relative_to(REPO)
        text = path.read_text()
        value = _static_devices_volume_limit(text, label=str(rel))
        assert value <= 0.0, (
            f"{rel}: devices.volume_limit {value} is positive — the project "
            "safety ceiling caps the main fader at full scale (0 dB)."
        )


@pytest.mark.parametrize(
    "text",
    [
        "filters:\n  volume_limit: 0.0\ndevices:\n  samplerate: 48000\n",
        "devices:\n  volume_limit: 0.0\ndevices: {volume_limit: 9.0}\n",
        'devices:\n  volume_limit: 0.0\n"devices":\n  volume_limit: 9.0\n',
        "devices:\n  volume_limit: 0.0\n  volume_limit: 9.0\n",
        "devices:\n  playback:\n    volume_limit: 0.0\n",
        'devices:\n  volume_limit: "0.0"\n',
        "devices:\n  volume_limit: .nan\n",
    ],
)
def test_static_camilla_volume_limit_pin_rejects_ambiguous_ownership(
    text: str,
) -> None:
    with pytest.raises(AssertionError):
        _static_devices_volume_limit(text, label="synthetic.yml")


# Wireless-sub low-pass corner: the bond-config range/default lives in Python
# (jasper.multiroom.config), but the corner is RE-CLAMPED in the Rust outputd
# `sub` pick (rust/jasper-outputd/src/dac_content.rs SUB_*). If the two drift —
# e.g. Python widens the range but Rust does not — the UI/validator would accept
# a corner the DAC silently clamps to a different value (the displayed 60 Hz
# becomes a 40 Hz low-pass). Grep-pin the Rust literals to the Python SSOT, same
# technique as the TTS-gain pins above.
_RUST_SUB_CORNER = "rust/jasper-outputd/src/dac_content.rs"

_SUB_CONST_PAT = lambda name: re.compile(  # noqa: E731
    rf"pub const {name}:\s*f64\s*=\s*(-?\d+(?:_\d+)*(?:\.\d+)?)\s*;"
)


def test_sub_crossover_corner_constants_match_python_and_rust() -> None:
    from jasper.multiroom import config as grouping_config

    text = (REPO / _RUST_SUB_CORNER).read_text()

    def rust_value(name: str) -> float:
        m = _SUB_CONST_PAT(name).search(text)
        assert m, f"{_RUST_SUB_CORNER}: const {name} not found (renamed?)"
        return float(m.group(1).replace("_", ""))

    pairs = (
        ("SUB_DEFAULT_CORNER_HZ", grouping_config.DEFAULT_CROSSOVER_HZ),
        ("SUB_MIN_CORNER_HZ", grouping_config.CROSSOVER_HZ_LO),
        ("SUB_MAX_CORNER_HZ", grouping_config.CROSSOVER_HZ_HI),
    )
    for rust_name, py_value in pairs:
        assert rust_value(rust_name) == py_value, (
            f"sub-corner drift: Rust {rust_name}={rust_value(rust_name)} != "
            f"Python {py_value} — keep dac_content.rs SUB_* and "
            "jasper.multiroom.config crossover constants in lockstep so the "
            "validated corner and the DAC's clamp agree."
        )
