# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""R6a (ingress transparency) + R4(a) (fader bracket) — pure predicates.

Each of R6a's four elements is tested independently, proved and individually
unproved, over injected status payloads (no socket, no daemon).
"""

from __future__ import annotations

import pytest

from jasper.bass_extension.bench import live_proof
from jasper.bass_extension.bench.derivation import ArtifactHeader


def _mux_status(**overrides: object) -> dict:
    base = {
        "test_source": "correction",
        "active_source": "correction",
        "test_owner": "correction-measurement",
    }
    base.update(overrides)
    return base


def _lane(**overrides: object) -> dict:
    base = {
        "label": "correction",
        "muted": False,
        "xrun_count": 0,
        "catchup_resync_frames": 0,
        "catchup_events": 0,
        "trim": {"trims": 0, "trimmed_frames": 0, "pending": False},
    }
    base.update(overrides)
    return base


def _fanin_status(*, lane: dict | None = None, tts: dict | None = None, output: dict | None = None) -> dict:
    return {
        "inputs": [lane or _lane()],
        "tts": tts
        or {
            "program_duck_active": False,
            "pending_frames": 0,
            "flushed_frames": 3,
            "dropped_audio_frames": 0,
        },
        "output": output or {"sample_rate": 48000},
    }


def _header(**overrides: object) -> ArtifactHeader:
    values = {"sample_rate_hz": 48000, "channels": 2, "bits_per_sample": 16}
    values.update(overrides)
    return ArtifactHeader(**values)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Element (i): mux isolation
# --------------------------------------------------------------------------- #


def test_isolation_proved_when_gate_held() -> None:
    live_proof.prove_isolation(_mux_status())  # does not raise


@pytest.mark.parametrize(
    "field,value",
    [("test_source", "airplay"), ("active_source", "spotify"), ("test_owner", "other-owner")],
)
def test_isolation_unproved_when_any_field_mismatches(field: str, value: str) -> None:
    with pytest.raises(live_proof.IngressProofError):
        live_proof.prove_isolation(_mux_status(**{field: value}))


# --------------------------------------------------------------------------- #
# Element (ii): lane state + counters
# --------------------------------------------------------------------------- #


def test_lane_state_proved_when_clean() -> None:
    live_proof.prove_lane_state(_fanin_status())


def test_lane_state_unproved_when_muted() -> None:
    with pytest.raises(live_proof.IngressProofError):
        live_proof.prove_lane_state(_fanin_status(lane=_lane(muted=True)))


def test_lane_state_unproved_when_resampler_armed() -> None:
    status = _fanin_status(lane=_lane(resampler={"armed": True}))
    with pytest.raises(live_proof.IngressProofError):
        live_proof.prove_lane_state(status)


def test_lane_state_proved_when_resampler_present_but_unarmed() -> None:
    status = _fanin_status(lane=_lane(resampler={"armed": False}))
    live_proof.prove_lane_state(status)  # does not raise


def test_lane_state_unproved_when_trim_pending() -> None:
    status = _fanin_status(lane=_lane(trim={"trims": 1, "trimmed_frames": 4, "pending": True}))
    with pytest.raises(live_proof.IngressProofError):
        live_proof.prove_lane_state(status)


def test_lane_state_unproved_when_lane_absent() -> None:
    with pytest.raises(live_proof.IngressProofError):
        live_proof.prove_lane_state({"inputs": []})


def test_lane_counters_proved_when_unchanged() -> None:
    start = _fanin_status()
    end = _fanin_status()
    live_proof.prove_lane_counters_unchanged(start, end)


@pytest.mark.parametrize("field", ["xrun_count", "catchup_resync_frames", "catchup_events"])
def test_lane_counters_unproved_when_any_counter_changes(field: str) -> None:
    start = _fanin_status()
    end = _fanin_status(lane=_lane(**{field: 1}))
    with pytest.raises(live_proof.IngressProofError):
        live_proof.prove_lane_counters_unchanged(start, end)


# --------------------------------------------------------------------------- #
# Element (iii): no foreign audio at the mix
# --------------------------------------------------------------------------- #


def test_no_foreign_audio_proved_when_clean() -> None:
    live_proof.prove_no_foreign_audio(_fanin_status(), _fanin_status())


def test_no_foreign_audio_unproved_when_duck_active() -> None:
    status = _fanin_status(tts={"program_duck_active": True, "pending_frames": 0, "flushed_frames": 0, "dropped_audio_frames": 0})
    with pytest.raises(live_proof.IngressProofError):
        live_proof.prove_no_foreign_audio(status, status)


def test_no_foreign_audio_unproved_when_pending_frames_nonzero() -> None:
    status = _fanin_status(tts={"program_duck_active": False, "pending_frames": 4, "flushed_frames": 0, "dropped_audio_frames": 0})
    with pytest.raises(live_proof.IngressProofError):
        live_proof.prove_no_foreign_audio(status, status)


@pytest.mark.parametrize("field", ["flushed_frames", "dropped_audio_frames"])
def test_no_foreign_audio_unproved_when_counters_change(field: str) -> None:
    start = _fanin_status()
    end_tts = dict(start["tts"])
    end_tts[field] = end_tts[field] + 1
    end = _fanin_status(tts=end_tts)
    with pytest.raises(live_proof.IngressProofError):
        live_proof.prove_no_foreign_audio(start, end)


def test_no_foreign_audio_unproved_when_tts_block_absent() -> None:
    with pytest.raises(live_proof.IngressProofError):
        live_proof.prove_no_foreign_audio({"inputs": []}, {"inputs": []})


def test_standalone_cue_duck_without_program_duck_flag_still_unproved() -> None:
    """rust/jasper-fanin/src/tts.rs: a standalone cue ducks at cue_duck_gain
    WITHOUT setting program_duck_active — caught via the flushed/dropped
    counter-unchanged check, not the flag alone, per R6a element (iii)'s
    rationale."""

    start = _fanin_status()
    end = _fanin_status(
        tts={
            "program_duck_active": False,  # still false — the flag alone misses this
            "pending_frames": 0,
            "flushed_frames": start["tts"]["flushed_frames"] + 5,  # cue audio flushed
            "dropped_audio_frames": 0,
        }
    )
    with pytest.raises(live_proof.IngressProofError):
        live_proof.prove_no_foreign_audio(start, end)


# --------------------------------------------------------------------------- #
# Element (iv): bit-width / geometry
# --------------------------------------------------------------------------- #


def test_bit_width_geometry_proved_for_16_bit_stereo_match() -> None:
    live_proof.prove_bit_width_geometry(
        artifact_header=_header(), fanin_status=_fanin_status()
    )


def test_bit_width_geometry_unproved_when_not_16_bit() -> None:
    with pytest.raises(live_proof.IngressProofError):
        live_proof.prove_bit_width_geometry(
            artifact_header=_header(bits_per_sample=24), fanin_status=_fanin_status()
        )


def test_bit_width_geometry_unproved_when_channels_mismatch() -> None:
    with pytest.raises(live_proof.IngressProofError):
        live_proof.prove_bit_width_geometry(
            artifact_header=_header(channels=1), fanin_status=_fanin_status()
        )


def test_bit_width_geometry_unproved_when_sample_rate_mismatches() -> None:
    with pytest.raises(live_proof.IngressProofError):
        live_proof.prove_bit_width_geometry(
            artifact_header=_header(sample_rate_hz=44100), fanin_status=_fanin_status()
        )


def test_bit_width_geometry_unproved_when_output_block_absent() -> None:
    with pytest.raises(live_proof.IngressProofError):
        live_proof.prove_bit_width_geometry(artifact_header=_header(), fanin_status={})


# --------------------------------------------------------------------------- #
# The full four-element proof
# --------------------------------------------------------------------------- #


def test_prove_ingress_transparency_passes_when_every_element_holds() -> None:
    inputs = live_proof.IngressProofInputs(
        mux_status=_mux_status(),
        fanin_status_start=_fanin_status(),
        fanin_status_end=_fanin_status(),
        artifact_header=_header(),
    )
    live_proof.prove_ingress_transparency(inputs)


def test_prove_ingress_transparency_fails_on_any_single_unproved_element() -> None:
    inputs = live_proof.IngressProofInputs(
        mux_status=_mux_status(test_owner="someone-else"),
        fanin_status_start=_fanin_status(),
        fanin_status_end=_fanin_status(),
        artifact_header=_header(),
    )
    with pytest.raises(live_proof.IngressProofError):
        live_proof.prove_ingress_transparency(inputs)


# --------------------------------------------------------------------------- #
# R4(a): fader bracket
# --------------------------------------------------------------------------- #


def test_fader_bracket_proved_when_stable_and_unmuted() -> None:
    live_proof.prove_fader_bracket(
        live_proof.FaderBracket(before_db=-35.0, before_muted=False, after_db=-35.0, after_muted=False)
    )


def test_fader_bracket_unproved_when_muted_before() -> None:
    with pytest.raises(live_proof.FaderDriftError):
        live_proof.prove_fader_bracket(
            live_proof.FaderBracket(before_db=-35.0, before_muted=True, after_db=-35.0, after_muted=False)
        )


def test_fader_bracket_unproved_when_muted_after() -> None:
    with pytest.raises(live_proof.FaderDriftError):
        live_proof.prove_fader_bracket(
            live_proof.FaderBracket(before_db=-35.0, before_muted=False, after_db=-35.0, after_muted=True)
        )


def test_fader_bracket_unproved_when_drifted() -> None:
    with pytest.raises(live_proof.FaderDriftError):
        live_proof.prove_fader_bracket(
            live_proof.FaderBracket(before_db=-35.0, before_muted=False, after_db=-34.0, after_muted=False)
        )
