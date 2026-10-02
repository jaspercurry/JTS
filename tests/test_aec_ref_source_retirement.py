"""AEC reference geometry and chip reference admission."""
from __future__ import annotations

import logging
import os
from pathlib import Path
from unittest.mock import MagicMock

from jasper.cli import aec_bridge
from jasper.aec.bridge_reference import REF_CHANNELS, REF_RATE
from tests._log_events import event_fields
from tests._sounddevice_stub import stub_sounddevice

REPO = Path(__file__).resolve().parents[1]


def test_the_reference_geometry_matches_outputd_the_producer():
    """48 kHz stereo is jasper-outputd's fact, not the bridge's free parameter.

    Cross-language pin against the producer's own source. Asserting the
    constants against each other would be self-referential — moving one would
    move both sides and stay green — so they are checked against
    `rust/jasper-outputd/src/types.rs`, where the value is fixed and
    `Config::from_env` refuses any other rate.
    """
    types_rs = (REPO / "rust" / "jasper-outputd" / "src" / "types.rs").read_text()

    assert f"pub const SAMPLE_RATE: u32 = {REF_RATE:_};" in types_rs, (
        "REF_RATE must equal jasper-outputd's core sample rate; "
        "the reference datagrams are that daemon's playout periods"
    )
    assert f"pub const CHANNELS: u16 = {REF_CHANNELS};" in types_rs, (
        "REF_CHANNELS must equal jasper-outputd's channel count; "
        "the reference is stereo whatever the sink's width"
    )


def _arm_chip_aec(monkeypatch, tmp_path, *, chip_ref_pcm: str) -> None:
    """Env + seams that get `main()` as far as the chip-AEC guard."""
    monkeypatch.setenv("JASPER_AEC_CHIP_AEC_ENABLED", "1")
    monkeypatch.setenv("JASPER_OUTPUTD_CHIP_REF_PCM", chip_ref_pcm)
    # Never touch /run from a test.
    monkeypatch.setenv(
        "JASPER_AEC_BRIDGE_STATS_PATH", str(tmp_path / "aec_bridge_stats.json")
    )
    # A validated beam plan is checked BEFORE this guard and parks on the
    # same EX_CONFIG with its own reason; stub it so a missing plan cannot
    # masquerade as this guard firing.
    monkeypatch.setattr(
        aec_bridge._mic_profile,
        "chip_beam_plan_from_env",
        lambda _env: aec_bridge._mic_profile.SQUARE_FIXED_150_210_PLAN,
    )


def test_chip_aec_refuses_to_start_without_a_chip_reference_producer(
    monkeypatch, tmp_path, caplog
):
    """`JASPER_AEC_CHIP_AEC_ENABLED=1` requires JASPER_OUTPUTD_CHIP_REF_PCM.

    Without it, outputd never feeds the XVF's USB-IN reference, so the chip
    cancels against nothing while the bridge forwards its beam as the live
    mic — echo straight back into the session with no software AEC3 behind
    it (chip-AEC mode bypasses the engine). Fail closed instead.
    """
    _arm_chip_aec(monkeypatch, tmp_path, chip_ref_pcm="")
    sd_mod = MagicMock()
    stub_sounddevice(monkeypatch, sd_mod)

    with caplog.at_level(logging.ERROR, logger="jasper.aec_bridge"):
        assert aec_bridge.main() == os.EX_CONFIG

    fields = event_fields(caplog, "aec_bridge.park")
    assert fields["reason"] == "chip_aec_without_chip_reference"
    assert "JASPER_OUTPUTD_CHIP_REF_PCM" in fields["detail"]
    # Failed at THIS guard, not incidentally at a later one: the guard sits
    # ahead of mic validation, so the mic was never even queried.
    sd_mod.query_devices.assert_not_called()


def test_the_chip_reference_guard_lets_a_configured_producer_through(
    monkeypatch, tmp_path
):
    """Positive control for the test above.

    Without this, deleting the guard's park would still leave the negative
    test green for the wrong reason — `main()` exits on the missing mic a few
    lines later either way. Here the guard is satisfied, so reaching mic
    validation proves it passed rather than short-circuited, and the DIFFERENT
    exit code (66, not 78) is what says which guard fired.
    """
    _arm_chip_aec(monkeypatch, tmp_path, chip_ref_pcm="hw:Array,0")
    sd_mod = MagicMock()
    sd_mod.query_devices.side_effect = ValueError("no such device")
    stub_sounddevice(monkeypatch, sd_mod)

    assert aec_bridge.main() == os.EX_NOINPUT
    sd_mod.query_devices.assert_called_once()
