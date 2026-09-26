"""AEC reference geometry, source migration, and chip reference admission."""
from __future__ import annotations

import logging
import os
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from jasper.cli import aec_bridge
from jasper.aec.bridge_reference import REF_CHANNELS, REF_RATE
from tests._log_events import event_fields
from tests._sounddevice_stub import stub_sounddevice

REPO = Path(__file__).resolve().parents[1]
RETIRED = "alsa"


def _config(ref_source: str) -> aec_bridge.BridgeConfig:
    """A default bridge config with only `ref_source` varied."""
    return replace(aec_bridge.BridgeConfig.from_env(), ref_source=ref_source)


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


# ---------------------------------------------------------------------------
# 2. A retired value converges; an unknown one still fails loudly.
# ---------------------------------------------------------------------------


def test_the_supported_source_is_returned_untouched():
    config = _config(aec_bridge.REF_SOURCE)
    assert aec_bridge.resolved_reference_source(config) is config


def test_the_retired_source_warns_and_falls_back_to_outputd_udp(caplog):
    """A parked box's stale env must not cost the household wake detection.

    The pre-P7-1 reconciler wrote the retired value whenever it parked the
    bridge, so it is still on disk out there. Refusing to start would leave
    jasper-voice bound to a UDP mic nobody feeds — a silent failure — so the
    bridge converges and says so.
    """
    with caplog.at_level(logging.WARNING, logger="jasper.aec_bridge"):
        resolved = aec_bridge.resolved_reference_source(_config(RETIRED))

    assert resolved.ref_source == aec_bridge.REF_SOURCE
    fields = event_fields(caplog, "aec.ref_source_retired")
    assert "jasper-aec-reconcile" in fields["detail"], (
        "the warning must name the command that converges the env file"
    )


@pytest.mark.parametrize("value", ["", "jasper_ref", "chip_ref_tee", "typo"])
def test_an_unknown_source_is_still_a_hard_failure(value):
    """Only the retired spelling is converged. Everything else still refuses.

    Guessing a transport for a name nobody recognises would put the AEC
    engine on a reference the operator did not ask for.
    """
    with pytest.raises(aec_bridge.UnsupportedReferenceSource) as excinfo:
        aec_bridge.resolved_reference_source(_config(value))
    assert repr(value) in str(excinfo.value)






# ---------------------------------------------------------------------------
# 4. The surviving chip-AEC precondition.
#
# Retiring the ALSA source made `main()`'s chip-AEC block a single guard:
# the `ref_source != outputd_udp` half became unreachable and was removed,
# leaving `JASPER_OUTPUTD_CHIP_REF_PCM` as the ONE thing standing between
# `JASPER_AEC_CHIP_AEC_ENABLED=1` and a bridge that forwards the chip beam
# while nothing feeds the chip's USB-IN reference — i.e. chip AEC running
# open-loop against the speaker. Nothing pinned it before.
# ---------------------------------------------------------------------------


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
        aec_bridge,
        "_chip_beam_plan",
        lambda: aec_bridge._mic_profile.SQUARE_FIXED_150_210_PLAN,
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
