# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the mic-presence single-source-of-truth reader.

The contract under test: presence is *generic* (the reconciler's gate marker,
true for any mic type), and the XVF runtime-profile JSON is XVF-only
*enrichment* — never the presence verdict. The headline regression guard is
``test_non_xvf_mic_present_is_not_reported_absent``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from jasper.mic_presence import read_mic_presence


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    state = tmp_path / "xvf3800.json"
    marker = tmp_path / "voice-input-absent"
    monkeypatch.setenv("JASPER_MIC_PROFILE_STATE_PATH", str(state))
    monkeypatch.setenv("JASPER_VOICE_INPUT_ABSENT_MARKER", str(marker))
    return state, marker


def _xvf_json(state: Path, *, present: bool = True, **over: object) -> None:
    payload: dict[str, object] = {
        "schema_version": 1,
        "present": present,
        "variant_id": "xvf3800_legacy_square_6ch",
        "display_name": "Seeed ReSpeaker XVF3800",
        "alsa_card_name": "Array",
        "capture_channels": 6,
        "recommended_profile": "xvf_software_aec3",
        "chip_aec_supported": False,
        "reason": "",
    }
    payload.update(over)
    state.write_text(json.dumps(payload))


# --- presence is generic (the gate marker), with XVF detail as enrichment ----

def test_no_marker_present_xvf_enriched(paths: tuple[Path, Path]) -> None:
    state, _ = paths
    _xvf_json(state, present=True)
    mp = read_mic_presence()
    assert mp.present is True
    assert mp.is_xvf is True
    assert mp.alsa_card == "Array"
    assert mp.capture_channels == 6
    assert mp.absent_confirmed is False
    assert "present (Array, 6ch" in mp.summary


def test_no_marker_present_chip_aec(paths: tuple[Path, Path]) -> None:
    state, _ = paths
    _xvf_json(
        state, present=True, chip_aec_supported=True,
        recommended_profile="xvf_chip_aec",
    )
    mp = read_mic_presence()
    assert mp.is_xvf is True
    assert mp.chip_aec_supported is True
    assert "chip-AEC capable" in mp.summary


def test_non_xvf_mic_present_is_not_reported_absent(paths: tuple[Path, Path]) -> None:
    """Regression guard (B1): a present non-XVF mic (UMIK-2 / custom device)
    leaves the gate marker cleared but publishes no XVF profile
    (``present=False``). It MUST read as present — never "no microphone"."""
    state, _ = paths
    _xvf_json(
        state, present=False, alsa_card_name="", variant_id="",
        capture_channels=None, reason="No supported XVF3800 ALSA card detected",
    )
    mp = read_mic_presence()
    assert mp.present is True  # a mic IS present — the gate marker is cleared
    assert mp.is_xvf is False  # it just isn't an XVF
    assert mp.absent_confirmed is False
    assert mp.summary == "present"


def test_no_marker_no_json_failopen_present(paths: tuple[Path, Path]) -> None:
    """Reconciler hasn't published + no parked marker -> fail open to present
    (matches the gate's default-open posture; the doctor probe is the backstop)."""
    mp = read_mic_presence()
    assert mp.present is True
    assert mp.is_xvf is False


# --- absence is the gate marker (generic, any mic type) ----------------------

def test_marker_present_is_absent(paths: tuple[Path, Path]) -> None:
    _, marker = paths
    marker.write_text("reason=no candidate microphone present\n")
    mp = read_mic_presence()
    assert mp.present is False
    assert mp.parked is True
    assert mp.absent_confirmed is True
    assert mp.reason == "no candidate microphone present"
    assert "not detected" in mp.summary
    assert "auto-starts" in mp.summary


def test_marker_wins_over_stale_xvf_json(paths: tuple[Path, Path]) -> None:
    """The gate is the usable-mic verdict: a no-mic marker is authoritative even
    if a stale XVF JSON still claims present."""
    state, marker = paths
    _xvf_json(state, present=True)
    marker.write_text("reason=no usable microphone present\n")
    mp = read_mic_presence()
    assert mp.present is False
    assert mp.absent_confirmed is True


def test_marker_present_corrupt_json_never_raises(paths: tuple[Path, Path]) -> None:
    state, marker = paths
    state.write_text("{ not json")
    marker.write_text("reason=broken\n")
    mp = read_mic_presence()
    assert mp.present is False  # marker authoritative; corrupt JSON is ignored


# --- writer<->reader schema contract (Tier-3 guard) --------------------------

def test_contract_reader_keys_match_as_dict_schema() -> None:
    """Every field ``read_mic_presence`` pulls from the enrichment JSON exists
    in ``RuntimeProfile.as_dict()`` — so a schema rename can't silently break
    the reader (the env-line and JSON projections both derive from as_dict)."""
    from jasper.mics import xvf3800

    profile = xvf3800.RuntimeProfile(
        present=False, variant=None, alsa_card_name="", capture_channels=None,
        chip_beam_plan=None, reason="x",
    )
    keys = set(profile.as_dict())
    for field in (
        "present", "alsa_card_name", "variant_id", "display_name",
        "capture_channels", "recommended_profile", "chip_aec_supported",
    ):
        assert field in keys, f"read_mic_presence reads '{field}' but as_dict() dropped it"
