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

from jasper.mic_presence import (
    MIC_ABSENT_ACCESSORY_UNKNOWN,
    MIC_ABSENT_CHIP_AEC_VALIDATING,
    MIC_ABSENT_NO_LOCAL_OR_ACCESSORY,
    MIC_ABSENT_UNKNOWN,
    read_mic_presence,
    voice_park_is_transient,
)


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    state = tmp_path / "xvf3800.json"
    marker = tmp_path / "voice-input-absent"
    monkeypatch.setenv("JASPER_MIC_PROFILE_STATE_PATH", str(state))
    monkeypatch.setenv("JASPER_VOICE_INPUT_ABSENT_MARKER", str(marker))
    # Hermetic: the accessory half is a real file read, so redirect it away
    # from /var/lib/jasper. Tests that need it write `tmp_path/accessory-mics.env`.
    monkeypatch.setenv(
        "JASPER_ACCESSORY_MIC_ENV_FILE", str(tmp_path / "accessory-mics.env"),
    )
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
    marker.write_text(
        f"reason={MIC_ABSENT_NO_LOCAL_OR_ACCESSORY}\n"
        "detail=no candidate microphone present\n"
    )
    mp = read_mic_presence()
    assert mp.present is False
    assert mp.parked is True
    assert mp.absent_confirmed is True
    assert mp.reason == MIC_ABSENT_NO_LOCAL_OR_ACCESSORY
    assert mp.detail == "no candidate microphone present"
    assert mp.as_dict()["detail"] == "no candidate microphone present"
    # The code is the machine axis; the prose beside it is what the headline
    # and /state.microphone.summary render.
    assert "input unavailable — no candidate microphone present" in mp.summary
    assert "reconciles automatically" in mp.summary


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (f"reason={MIC_ABSENT_ACCESSORY_UNKNOWN}\n", MIC_ABSENT_ACCESSORY_UNKNOWN),
        # An older build's free prose, and a marker with no reason line at all:
        # neither may pass through as a code a consumer could switch on.
        ("reason=no candidate microphone present\n", MIC_ABSENT_UNKNOWN),
        ("", MIC_ABSENT_UNKNOWN),
    ],
    ids=("known", "prose", "no-reason-line"),
)
def test_only_a_vocabulary_code_reads_back_as_one(
    paths: tuple[Path, Path], body: str, reason: str
) -> None:
    _, marker = paths
    marker.write_text(body)
    mp = read_mic_presence()
    assert mp.present is False
    assert mp.reason == reason


@pytest.mark.parametrize(
    ("body", "transient"),
    [
        (f"reason={MIC_ABSENT_CHIP_AEC_VALIDATING}\n", True),
        (f"reason={MIC_ABSENT_NO_LOCAL_OR_ACCESSORY}\n", False),
        # Fail-safe: an unreadable code is a real absence, so the shutdown cue
        # (ADR-0239) still fires.
        ("reason=nonsense\n", False),
        ("", False),
    ],
    ids=("validating", "real-absence", "unknown-code", "no-marker-body"),
)
def test_transient_is_a_property_of_the_code(
    paths: tuple[Path, Path], body: str, transient: bool
) -> None:
    _, marker = paths
    marker.write_text(body)
    assert voice_park_is_transient() is transient


def test_marker_wins_over_stale_xvf_json(paths: tuple[Path, Path]) -> None:
    """The gate is the usable-mic verdict: a no-mic marker is authoritative even
    if a stale XVF JSON still claims present."""
    state, marker = paths
    _xvf_json(state, present=True)
    marker.write_text(f"reason={MIC_ABSENT_NO_LOCAL_OR_ACCESSORY}\n")
    mp = read_mic_presence()
    assert mp.present is False
    assert mp.absent_confirmed is True


def test_marker_present_corrupt_json_never_raises(paths: tuple[Path, Path]) -> None:
    state, marker = paths
    state.write_text("{ not json")
    marker.write_text(f"reason={MIC_ABSENT_NO_LOCAL_OR_ACCESSORY}\n")
    mp = read_mic_presence()
    assert mp.present is False  # marker authoritative; corrupt JSON is ignored


# --- the accessory half of the gate (issue #2205) ----------------------------

def test_accessory_only_box_does_not_claim_a_present_microphone(
    paths: tuple[Path, Path], tmp_path: Path,
) -> None:
    """The no-local-mic + paired-remote box must not read as a plain mic box.

    This module cannot see the local mic, so it must not say "present" — an
    operator would act on that. It names the push-to-talk source instead, and
    the doctor's local probes report the local half.
    """
    _, marker = paths
    assert not marker.exists()
    (tmp_path / "accessory-mics.env").write_text(
        "JASPER_MANUAL_MIC_SOURCES=wiim_remote_2=udp:9892\n",
    )
    mp = read_mic_presence()
    assert mp.present is True
    assert mp.absent_confirmed is False
    assert mp.accessory_present is True
    assert mp.accessory_sources == ("wiim_remote_2",)
    # Gate state, never runtime state: this record cannot see whether
    # jasper-voice is up. A no-local-mic box CAN answer since the daemon half
    # of issue #2205 landed, which makes the runtime phrasing more tempting and
    # no more true — "voice input available" / "runs" would still be a
    # present-tense claim about a daemon nothing here looks at.
    assert mp.summary == (
        "voice-input gate open — push-to-talk accessory paired: wiim_remote_2; "
        "this record cannot see the local mic — the doctor's "
        "`mic ALSA card` / `mic capture` checks own that half"
    )
    assert "runs" not in mp.summary
    assert "available" not in mp.summary
    assert mp.as_dict()["accessory_sources"] == ["wiim_remote_2"]


def test_local_xvf_and_accessory_report_both(
    paths: tuple[Path, Path], tmp_path: Path,
) -> None:
    state, _ = paths
    _xvf_json(state, present=True)
    (tmp_path / "accessory-mics.env").write_text(
        "JASPER_MANUAL_MIC_SOURCES=wiim_remote_2=udp:9892\n",
    )
    mp = read_mic_presence()
    assert "present (Array, 6ch" in mp.summary
    assert "push-to-talk accessory paired: wiim_remote_2" in mp.summary


def test_no_accessory_file_leaves_summary_untouched(
    paths: tuple[Path, Path],
) -> None:
    """The accessory wording must not leak onto ordinary single-mic boxes."""
    mp = read_mic_presence()
    assert mp.accessory_present is False
    assert mp.accessory_sources == ()
    assert mp.summary == "present"


def test_unreadable_accessory_file_never_raises_from_the_display_reader(
    paths: tuple[Path, Path], tmp_path: Path, monkeypatch,
) -> None:
    """read_accessory_mic_sources propagates an unreadable file so the GATE
    writer can say "I could not tell". This reader is a display surface and
    must still never raise — pin both halves of that split."""
    from jasper.accessories import mic_env

    def _boom(_path=None):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(
        "jasper.mic_presence.read_accessory_mic_sources", _boom,
    )
    mp = read_mic_presence()
    assert mp.accessory_sources == ()
    assert mp.present is True

    # And the underlying reader really does propagate, rather than this being
    # a test of a mock: a missing file is (), an unreadable one raises.
    missing = tmp_path / "absent.env"
    assert mic_env.read_accessory_mic_sources(str(missing)) == ()
    unreadable = tmp_path / "unreadable.env"
    unreadable.write_text("JASPER_MANUAL_MIC_SOURCES=a=udp:1\n")
    unreadable.chmod(0o000)
    import os as _os
    if _os.access(unreadable, _os.R_OK):
        unreadable.chmod(0o644)
        pytest.skip("cannot make a file unreadable as this user")
    try:
        with pytest.raises(OSError):
            mic_env.read_accessory_mic_sources(str(unreadable))
    finally:
        unreadable.chmod(0o644)


def test_malformed_accessory_file_publishes_nothing(
    paths: tuple[Path, Path], tmp_path: Path,
) -> None:
    """Fail-safe: an unparsable file must never invent an accessory mic."""
    (tmp_path / "accessory-mics.env").write_text(
        "JASPER_MANUAL_MIC_SOURCES=wiim_remote_2\n",
    )
    assert read_mic_presence().accessory_sources == ()


def test_accessory_sources_survive_a_parked_verdict(
    paths: tuple[Path, Path], tmp_path: Path,
) -> None:
    """If the two halves ever disagree (marker written before the accessory was
    published, next reconcile pending), the record shows both facts."""
    _, marker = paths
    marker.write_text("reason=no candidate microphone present\n")
    (tmp_path / "accessory-mics.env").write_text(
        "JASPER_MANUAL_MIC_SOURCES=wiim_remote_2=udp:9892\n",
    )
    mp = read_mic_presence()
    assert mp.present is False
    assert mp.accessory_sources == ("wiim_remote_2",)


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
