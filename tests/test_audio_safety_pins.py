# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pin the loud-output safety literals.

Two hardware-safety claims had no test asserting the actual literals:

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

# The two symbols commit 6304556a4 ("Remove fixed TTS gain ceiling",
# 2026-07-01) deleted: the ceiling constant itself, and the helper that
# clamped a requested gain up against it. Today's replacement is
# `sanitize_tts_gain_db` (floor + non-finite only, no ceiling). Both names
# are assembled from fragments so this file does not itself contain the
# banned text — the pin greps source, and a literal here would make the
# grep hit its own guard if the scan ever widens.
REMOVED_TTS_MAX_SYMBOL = "MAX_" + "TTS_GAIN_DB"
REMOVED_TTS_CLAMP_SYMBOL = "clamp_" + "tts_gain_db"
REMOVED_TTS_CEILING_SYMBOLS = (REMOVED_TTS_MAX_SYMBOL, REMOVED_TTS_CLAMP_SYMBOL)

# Files the ban covers, in three groups — every entry is a place a fixed
# ceiling could live, and the group says why.
#
#   DEFINE the shared gain policy (4). Where the removed ceiling was
#   declared, and the modules re-exporting that policy.
#
#   APPLY the policy to samples (3). A ceiling reintroduced at an
#   application site never touches a policy module, so covering only the
#   definers would leave a reintroduced fixed ceiling free to evade the
#   ban pin.
#
#   CARRY the gain command (1). outputd's TTS bridge held
#   `fallback_gain_db: MAX_TTS_GAIN_DB` until 6304556a4 removed it, and
#   still owns the live `TtsCommand::GainDb` handler — today a deliberate
#   no-op with no fallback-gain state behind it, which makes it the
#   natural place for one to grow back. Local gain math has in fact grown
#   here before (deep-audit DA-0590: a private `db_to_linear` duplicating
#   the shared helper).
#
# Of these eight, 6304556a4 deleted ceiling references from five — and not
# uniformly, which is itself the argument for banning both symbols rather
# than one: tts-protocol/loudness.rs (which declared them), outputd/mixer.rs
# (which re-exported them) and fanin/tts.rs carried BOTH; outputd/tts.rs
# carried only the MAX_ constant; core.rs carried only the clamp helper. A
# one-symbol ban would have missed a file either way round.
#
# The other three are covered structurally, not historically: the two daemon
# loudness.rs files re-export the shared policy wholesale, and
# assistant_source.rs postdates the removal — created later by 2ac841f24
# (#2063) — so it is covered because it applies gain today, not because it
# ever carried a ceiling.
#
# Deliberately NOT covered: fanin/state.rs and outputd/state.rs. They are
# pure STATUS renderers — they read a decided gain and serialize it, and
# have no authority to cap one. Adding them would widen the ban past what
# it can honestly claim to guard.
RUST_TTS_GAIN_FILES = (
    "rust/jasper-tts-protocol/src/loudness.rs",
    "rust/jasper-fanin/src/loudness.rs",
    "rust/jasper-outputd/src/loudness.rs",
    "rust/jasper-outputd/src/mixer.rs",
    "rust/jasper-fanin/src/tts.rs",
    "rust/jasper-outputd/src/assistant_source.rs",
    "rust/jasper-outputd/src/core.rs",
    "rust/jasper-outputd/src/tts.rs",
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
        path = REPO / rel
        assert path.is_file(), (
            f"{rel}: file in the TTS gain ban list no longer exists. If gain "
            "code moved, re-point RUST_TTS_GAIN_FILES at its new home — a "
            "silently-vanished entry is a hole in the ban, not a pass."
        )
        text = path.read_text()
        for symbol in REMOVED_TTS_CEILING_SYMBOLS:
            assert symbol not in text, (
                f"{rel}: reintroduced a fixed max TTS gain ceiling "
                f"({symbol}). Assistant loudness should be governed by the "
                "measured target plus the dynamic peak-aware cap, not a "
                "universal source-gain clamp."
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


# Assistant-gain floor: the one fixed bound the shared Rust loudness engine
# applies (sanitize_tts_gain_db floors there, and maps a malformed value there).
# jasper-doctor restates it to judge fan-in's and outputd's published
# final_gain_db, so grep-pin the Rust literal to the doctor's copy — a doctor
# that believes in a floor the engine no longer enforces is exactly how #2345
# happened, with the doctor asserting a [-60, 0] clamp for two months after the
# fixed ceiling was deliberately removed (2026-07-01).
_RUST_SHARED_LOUDNESS = "rust/jasper-tts-protocol/src/loudness.rs"

_MIN_TTS_GAIN_PAT = re.compile(
    r"pub const MIN_TTS_GAIN_DB:\s*f32\s*=\s*(-?\d+(?:\.\d+)?)\s*;"
)


def test_assistant_gain_floor_matches_rust_and_doctor() -> None:
    from jasper.cli.doctor import audio_runtime_fanin

    text = (REPO / _RUST_SHARED_LOUDNESS).read_text()
    match = _MIN_TTS_GAIN_PAT.search(text)
    assert match, (
        f"{_RUST_SHARED_LOUDNESS}: const MIN_TTS_GAIN_DB not found (renamed?). "
        "The doctor's assistant-gain floor is pinned to this literal."
    )
    rust_floor = float(match.group(1))
    assert rust_floor == audio_runtime_fanin._ASSISTANT_GAIN_FLOOR_DB, (
        f"assistant-gain floor drift: Rust MIN_TTS_GAIN_DB={rust_floor} != "
        f"doctor _ASSISTANT_GAIN_FLOOR_DB="
        f"{audio_runtime_fanin._ASSISTANT_GAIN_FLOOR_DB} — keep them in lockstep so "
        "jasper-doctor judges published final_gain_db against the bound the "
        "engine actually enforces."
    )
