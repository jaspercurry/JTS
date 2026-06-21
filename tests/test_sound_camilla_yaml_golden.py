# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Byte-identity golden for ``emit_sound_config``.

PR-2 (docs/HANDOFF-dsp-graph-carrier.md "Sharing — one stereo-domain
prefix builder") extracts the program-domain room-PEQ → preference →
headroom assembly out of ``jasper.sound.camilla_yaml`` into the neutral
``jasper.camilla_stereo_prefix.build_stereo_prefix``. The refactor must be
**behavior-neutral**: every existing case must emit the SAME BYTES.

These goldens pin the full emitted YAML for a representative set of
profiles covering every prefix branch (flat, simple boost + preamp,
custom PEQ + room cuts, room boost → headroom, leader-bake L/R + delays,
bonded-leader pipe sink). The expected outputs were captured from the
pre-refactor emitter and live as fixtures under
``tests/fixtures/stereo_prefix_golden/``.

Regenerate (only after a *deliberate*, reviewed output change):

    .venv/bin/python tests/test_sound_camilla_yaml_golden.py

A plain ``pytest`` run never regenerates — it only compares.
"""

from __future__ import annotations

from pathlib import Path

from jasper.camilla_config_contract import PeqFilter
from jasper.sound.camilla_yaml import emit_sound_config
from jasper.sound.profile import ParametricBand, SimpleEq, SoundProfile

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "stereo_prefix_golden"


def _profile(**kwargs) -> SoundProfile:
    # Pin updated_at so construction is deterministic (it is not emitted into
    # the YAML, but keeping it fixed avoids any incidental coupling).
    kwargs.setdefault("updated_at", "")
    return SoundProfile(**kwargs)


# name -> (profile, emit_sound_config kwargs). One source of truth for both
# the comparison test and the regenerator below.
GOLDEN_CASES: dict[str, tuple[SoundProfile, dict]] = {
    # Flat, enabled, no room correction: minimal prefix (just `flat`).
    "flat": (_profile(enabled=True, curve_id="flat"), {"profile_id": "flat-id"}),
    # Simple 5-band boost with an explicit headroom trim → single sound_preamp.
    "simple_boost_trim": (
        _profile(
            enabled=True,
            curve_id="flat",
            simple_eq=SimpleEq(sub_bass_db=3.0, bass_db=6.0, treble_db=3.0),
        ),
        {"output_trim_db": 4.0, "profile_id": "simple-id"},
    ),
    # Stock curve + a custom parametric band + cuts-only room correction.
    "harman_custom_peq_room_cuts": (
        _profile(
            enabled=True,
            curve_id="harman",
            simple_eq=SimpleEq(mid_db=-1.0),
            parametric_bands=(
                ParametricBand(
                    enabled=True, biquad_type="Peaking", freq_hz=2000.0,
                    gain_db=-2.0, q=2.0,
                ),
                ParametricBand(
                    enabled=True, biquad_type="Highpass", freq_hz=30.0,
                    gain_db=0.0, q=0.7,
                ),
            ),
        ),
        {
            "room_peqs": [
                PeqFilter(freq=80.0, q=4.0, gain=-3.0),
                PeqFilter(freq=140.0, q=2.0, gain=-1.5),
            ],
            "profile_id": "harman-id",
        },
    ),
    # Room correction with positive boosts → worst-case additive headroom trim.
    "room_boost_headroom": (
        _profile(enabled=False, curve_id="bk", simple_eq=SimpleEq()),
        {
            "room_peqs": [
                PeqFilter(freq=45.0, q=5.0, gain=2.0),
                PeqFilter(freq=80.0, q=6.0, gain=-4.0),
                PeqFilter(freq=120.0, q=4.0, gain=1.0),
            ],
        },
    ),
    # Leader-bake: distinct per-seat room chains + channel delays + headroom +
    # preamp. Exercises every per-channel and shared branch at once.
    "leader_bake_delays": (
        _profile(
            enabled=True,
            curve_id="harman",
            simple_eq=SimpleEq(bass_db=2.0),
        ),
        {
            "room_peqs": [PeqFilter(freq=80.0, q=4.0, gain=-3.0)],
            "room_peqs_right": [
                PeqFilter(freq=120.0, q=3.0, gain=-2.0),
                PeqFilter(freq=4000.0, q=2.0, gain=1.0),
            ],
            "channel_delays_ms": (1.5, 0.5),
            "output_trim_db": 4.0,
            "profile_id": "leader-id",
        },
    ),
    # Bonded-leader pipe sink (File backend, rate_adjust off) with leader-bake.
    "pipe_sink_leader": (
        _profile(enabled=True, curve_id="harman", simple_eq=SimpleEq()),
        {
            "room_peqs": [PeqFilter(freq=80.0, q=4.0, gain=-3.0)],
            "room_peqs_right": [PeqFilter(freq=120.0, q=2.0, gain=-2.0)],
            "enable_rate_adjust": False,
            "playback_pipe_path": "/run/jasper-snapserver/snapfifo",
            "profile_id": "pipe-id",
        },
    ),
}


def _emit(name: str) -> str:
    profile, kwargs = GOLDEN_CASES[name]
    return emit_sound_config(profile, **kwargs)


def test_emit_sound_config_byte_identical_goldens():
    """Full-output equality for every representative profile. A diff here
    means the refactor changed emitted bytes — investigate, do not
    regenerate blindly."""
    missing = [n for n in GOLDEN_CASES if not (FIXTURE_DIR / f"{n}.yml").exists()]
    assert not missing, f"missing golden fixtures: {missing} (run the regenerator)"
    for name in GOLDEN_CASES:
        expected = (FIXTURE_DIR / f"{name}.yml").read_text()
        assert _emit(name) == expected, f"golden mismatch for {name!r}"


def _regenerate() -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for name in GOLDEN_CASES:
        (FIXTURE_DIR / f"{name}.yml").write_text(_emit(name))
        print(f"wrote {name}.yml")


if __name__ == "__main__":
    _regenerate()
