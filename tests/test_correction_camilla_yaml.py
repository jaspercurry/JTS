"""CamillaDSP YAML emission: structural invariants + base-config compat.

The emitted YAML has to be loadable by CamillaDSP — testing that
end-to-end requires an actual CamillaDSP install, which we don't
have at unit-test time. Instead, we pin the structural invariants:

  - Output is a single YAML document.
  - Devices block matches the cutover topology shape (samplerate,
    capture/playback types, format strings).
  - master_gain mixer is preserved verbatim — Ducker contract
    relies on it.
  - For empty PEQs, the output is functionally equivalent to the
    outputd cutover config (only a header comment differs).
  - For a PEQ list, each filter shows up as a Biquad/Peaking entry
    with the right freq/q/gain values, and the per-channel pipeline
    Filter blocks reference the new names plus `flat`.

We don't depend on a yaml parser at test time (matches the
production constraint of not importing one). String-search
assertions are sufficient for a deterministic emitter.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from jasper.correction.camilla_yaml import emit_correction_config
from jasper.correction.peq import PEQ


def test_empty_peqs_yields_pipeline_with_only_flat():
    """With no PEQ filters, the output should still have the `flat`
    Gain filter and a pipeline that references only `flat` per
    channel — equivalent to deploy/camilladsp/outputd-cutover.yml."""
    yaml = emit_correction_config([])
    # Devices.
    assert "samplerate: 48000" in yaml
    assert "target_level: 2048" in yaml
    assert "volume_limit: 0.0" in yaml
    assert 'device: "plug:jasper_capture"' in yaml
    assert "format: S32_LE" in yaml
    assert 'device: "outputd_content_playback"' in yaml
    assert "format: S16_LE" in yaml
    # Cutover contract: kernel-side rate adjust OR AsyncSinc, never both.
    assert "enable_rate_adjust: true" in yaml
    assert "resampler:" not in yaml
    assert "AsyncSinc" not in yaml
    # master_gain mixer preserved.
    assert "master_gain:" in yaml
    assert "{ in: 2, out: 2 }" in yaml
    # `flat` filter present.
    assert "flat:" in yaml
    assert "type: Gain" in yaml
    # Pipeline references `[flat]` on both channels.
    assert "channels: [0]" in yaml
    assert "channels: [1]" in yaml
    assert "names: [flat]" in yaml
    # No Biquad / Peaking when no PEQs.
    assert "Biquad" not in yaml
    assert "Peaking" not in yaml


def test_peq_filters_emit_with_correct_values():
    peqs = [
        PEQ(freq=80.0, q=4.0, gain=-3.5),
        PEQ(freq=150.0, q=2.5, gain=-2.0),
    ]
    yaml = emit_correction_config(peqs)
    assert "peq_1:" in yaml
    assert "peq_2:" in yaml
    # Frequency / Q / gain literals from each filter.
    assert "freq: 80.0000" in yaml
    assert "q: 4.0000" in yaml
    assert "gain: -3.5000" in yaml
    assert "freq: 150.0000" in yaml
    assert "q: 2.5000" in yaml
    assert "gain: -2.0000" in yaml
    # Both filters typed as Biquad / Peaking.
    assert yaml.count("type: Biquad") == 2
    assert yaml.count("type: Peaking") == 2


def test_pipeline_chains_peqs_before_flat():
    """The per-channel Filter blocks must reference the PEQs in
    order, then `flat` last. Without `flat` last, the pipeline drops
    a filter slot the existing code might rely on (and the diff vs
    the cutover base grows beyond what we want)."""
    peqs = [PEQ(freq=80.0, q=4.0, gain=-3.0), PEQ(freq=200.0, q=2.0, gain=-2.0)]
    yaml = emit_correction_config(peqs)
    # Both channels reference the same PEQ chain in the same order.
    expected = "names: [peq_1, peq_2, flat]"
    assert yaml.count(expected) == 2  # one per channel


def test_master_gain_mixer_unchanged_with_peqs():
    """Whatever the PEQ list is, the master_gain mixer block must
    look like the cutover base's. The Ducker writes main_volume, NOT
    master_gain, but the mixer is the placeholder hook for future
    EQ work — and Phase 1 deliberately preserves it untouched."""
    peqs = [PEQ(freq=80.0, q=4.0, gain=-3.0)]
    yaml_with = emit_correction_config(peqs)
    yaml_empty = emit_correction_config([])
    # Extract the mixers block from each — same content.
    def mixer_block(s: str) -> str:
        start = s.index("mixers:")
        end = s.index("pipeline:", start)
        return s[start:end].strip()
    assert mixer_block(yaml_with) == mixer_block(yaml_empty)


def test_writes_to_out_path(tmp_path: Path):
    out = tmp_path / "correction.yml"
    yaml = emit_correction_config(
        [PEQ(freq=80.0, q=4.0, gain=-3.0)],
        out_path=out,
        measurement_id="abc123",
    )
    assert out.exists()
    contents = out.read_text()
    assert contents == yaml
    # Header comment carries the measurement_id so we can debug
    # which YAML came from which measurement.
    assert "abc123" in contents


def test_out_path_parent_must_exist(tmp_path: Path):
    """Don't auto-create deep directory trees; the caller's flow
    should have made the dir. Surface the misordering as a clear
    error rather than silently creating directories at random."""
    bogus = tmp_path / "does" / "not" / "exist" / "out.yml"
    with pytest.raises(FileNotFoundError):
        emit_correction_config([], out_path=bogus)


def test_emits_pretty_floats_not_repr():
    """Don't let `repr(80.0)` -> '80.0' creep in via `{:.4f}` shorthand
    — CamillaDSP YAML accepts both 80 and 80.0, but the emitter's
    contract is 4-decimal floats so a diff between two near-identical
    measurements doesn't produce surprising number-format churn."""
    peqs = [PEQ(freq=80.123456789, q=4.123456789, gain=-3.123456789)]
    yaml = emit_correction_config(peqs)
    assert "freq: 80.1235" in yaml
    assert "q: 4.1235" in yaml
    assert "gain: -3.1235" in yaml
