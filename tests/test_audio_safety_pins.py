# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pin the loud-output safety literals documented in HANDOFF-volume.md.

Two documented hardware-safety claims (docs/HANDOFF-volume.md,
"Hearing-safety belt") had no test asserting the actual literals:

1. ``MAX_TTS_GAIN_DB = -6 dB`` — the hardware ceiling on the TTS path,
   enforced by the Python legacy playout (``jasper/audio_io.py``) and
   the shared Rust loudness policy
   (``rust/jasper-tts-protocol/src/loudness.rs``), which fan-in and
   outputd re-export. Outputd also keeps its older ``mixer`` helper
   names as compatibility re-exports for existing callers. The Rust
   crates' own unit tests assert clamp behaviour *relative to* the
   constant, so flipping ``-6.0`` to ``+6.0`` would pass cargo test; the
   Python test only asserted ``<= 0.0``. This pins the literal, daemon
   shims, and outputd compatibility surface.

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

import re
from pathlib import Path

from jasper.audio_io import TtsPlayout

REPO = Path(__file__).resolve().parents[1]

DOCUMENTED_TTS_CEILING_DB = -6.0

RUST_TTS_GAIN_FILES = (
    "rust/jasper-tts-protocol/src/loudness.rs",
)

RUST_SHARED_LOUDNESS_SHIMS = (
    "rust/jasper-fanin/src/loudness.rs",
    "rust/jasper-outputd/src/loudness.rs",
)

RUST_OUTPUTD_MIXER = "rust/jasper-outputd/src/mixer.rs"

RUST_OUTPUTD_MIXER_GAIN_SYMBOLS = (
    "apply_gain_i16",
    "clamp_tts_gain_db",
    "gain_db_to_linear",
    "MAX_TTS_GAIN_DB",
    "MIN_TTS_GAIN_DB",
)

_RUST_CONST_PAT = re.compile(
    r"^pub const MAX_TTS_GAIN_DB: f32 = (-?\d+(?:\.\d+)?);", re.MULTILINE
)

_RUST_SHARED_LOUDNESS_USE_PAT = re.compile(
    r"^pub use jasper_tts_protocol::loudness::\{(?P<body>[^}]+)\};",
    re.MULTILINE | re.DOTALL,
)


def test_rust_tts_gain_ceiling_is_minus_six_db() -> None:
    for rel in RUST_TTS_GAIN_FILES:
        text = (REPO / rel).read_text()
        match = _RUST_CONST_PAT.search(text)
        assert match, f"{rel}: MAX_TTS_GAIN_DB const not found (moved?)"
        assert float(match.group(1)) == DOCUMENTED_TTS_CEILING_DB, (
            f"{rel}: MAX_TTS_GAIN_DB is {match.group(1)}, but "
            "docs/HANDOFF-volume.md promises a -6 dB hardware ceiling "
            "on the TTS path. Retuning it is a hearing-safety decision: "
            "update the doc and this pin together."
        )


def test_rust_daemon_loudness_modules_reexport_shared_tts_gain_policy() -> None:
    for rel in RUST_SHARED_LOUDNESS_SHIMS:
        text = (REPO / rel).read_text()
        assert "pub use jasper_tts_protocol::loudness::*;" in text, (
            f"{rel}: daemon loudness module no longer re-exports the "
            "shared Rust policy. If this is intentional, update the "
            "safety pin and explain how the -6 dB TTS ceiling still "
            "cannot drift between daemons."
        )


def test_outputd_mixer_reexports_shared_tts_gain_helpers() -> None:
    text = (REPO / RUST_OUTPUTD_MIXER).read_text()
    match = _RUST_SHARED_LOUDNESS_USE_PAT.search(text)
    assert match, (
        f"{RUST_OUTPUTD_MIXER}: outputd mixer no longer re-exports TTS gain "
        "helpers from jasper_tts_protocol::loudness. If this is intentional, "
        "update the safety pin and explain how outputd helper callers still "
        "cannot drift from the shared -6 dB policy."
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


def test_python_tts_gain_ceiling_matches_rust() -> None:
    assert TtsPlayout.MAX_TTS_GAIN_DB == DOCUMENTED_TTS_CEILING_DB, (
        "jasper/audio_io.py TtsPlayout.MAX_TTS_GAIN_DB drifted from the "
        "documented -6 dB ceiling (docs/HANDOFF-volume.md) that the Rust "
        "daemons also enforce."
    )


_VOLUME_LIMIT_PAT = re.compile(
    r"^\s*volume_limit:\s*(-?\d+(?:\.\d+)?)\s*$", re.MULTILINE
)


def test_every_static_camilladsp_config_caps_volume_at_zero_db() -> None:
    yamls = sorted((REPO / "deploy" / "camilladsp").glob("*.yml"))
    assert yamls, "deploy/camilladsp/*.yml not found (moved?)"
    for path in yamls:
        rel = path.relative_to(REPO)
        values = _VOLUME_LIMIT_PAT.findall(path.read_text())
        assert values, (
            f"{rel}: no devices.volume_limit — every JTS CamillaDSP "
            "config must carry the 0.0 safety ceiling "
            "(docs/HANDOFF-volume.md, AGENTS.md renderer section)."
        )
        for value in values:
            assert float(value) <= 0.0, (
                f"{rel}: volume_limit {value} is positive — the project "
                "safety ceiling caps the main fader at full scale (0 dB)."
            )
