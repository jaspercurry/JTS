"""Pin the loud-output safety literals documented in HANDOFF-volume.md.

Two documented hardware-safety claims (docs/HANDOFF-volume.md,
"Hearing-safety belt") had no test asserting the actual literals:

1. ``MAX_TTS_GAIN_DB = -6 dB`` — the hardware ceiling on the TTS path,
   enforced independently in three places: the Python legacy playout
   (``jasper/audio_io.py``), ``rust/jasper-fanin/src/loudness.rs``, and
   ``rust/jasper-outputd/src/mixer.rs``. The Rust crates' own unit
   tests assert clamp behaviour *relative to* the constant, so flipping
   ``-6.0`` to ``+6.0`` would pass cargo test; the Python test only
   asserted ``<= 0.0``. This pins the literal in all three.

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
    "rust/jasper-fanin/src/loudness.rs",
    "rust/jasper-outputd/src/mixer.rs",
)

_RUST_CONST_PAT = re.compile(
    r"^pub const MAX_TTS_GAIN_DB: f32 = (-?\d+(?:\.\d+)?);", re.MULTILINE
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
