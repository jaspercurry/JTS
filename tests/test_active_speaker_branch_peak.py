# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Rendering ONE stimulus through the applied graph, per driver branch.

Two things are pinned here and they answer different questions. The
ARITHMETIC tests drive the render against cases whose answer is known in
closed form — an identity graph, a mono-sum mixer, a plain gain, a delay — so
the machinery is checked against something other than itself. The REFUSAL
tests pin the fail-conservative contract: every shape the module cannot model
exactly raises, because a silently-skipped attenuator under-reports a branch
peak and that is the one direction that raises a volume ceiling unsafely.
"""
from __future__ import annotations

import math

import numpy as np
import pytest
from scipy.io import wavfile

from jasper.active_speaker import branch_peak
from jasper.active_speaker.branch_peak import (
    BranchPeakError,
    branch_peaks_for_targets,
    stimulus_branch_peaks_dbfs,
)

MONO_SUM_GAIN_DB = 20.0 * math.log10(0.5)  # -6.020599913…, the split mixer's leg


def _devices(*, rate: int = 48000, capture: int = 2, playback: int = 2) -> dict:
    return {
        "samplerate": rate,
        "capture": {"channels": capture},
        "playback": {"channels": playback},
    }


def _passthru_mixer() -> dict:
    return {
        "passthru": {
            "channels": {"in": 2, "out": 2},
            "mapping": [
                {
                    "dest": d,
                    "sources": [{"channel": d, "gain": 0.0, "inverted": False}],
                }
                for d in (0, 1)
            ],
        }
    }


def _split_mixer() -> dict:
    """The active-speaker split: L+R mono-summed onto every driver output."""
    return {
        "split_active_2way": {
            "channels": {"in": 2, "out": 2},
            "mapping": [
                {
                    "dest": d,
                    "sources": [
                        {"channel": 0, "gain": MONO_SUM_GAIN_DB, "inverted": False},
                        {"channel": 1, "gain": MONO_SUM_GAIN_DB, "inverted": False},
                    ],
                }
                for d in (0, 1)
            ],
        }
    }


def _tone_wav(path, *, peak_dbfs: float = -12.0, hz: float = 300.0, seconds: float = 1.0):
    """A tone on channel 0 only, silent on channel 1 — the worst-case shape.

    Deliberately one-sided: it is what makes the mono-sum mixer's -6.02 dB per
    leg visible as a real 6.02 dB, which a correlated L==R stimulus would hide
    (its two legs sum back to unity).
    """
    rate = 48000
    n = int(rate * seconds)
    t = np.arange(n) / rate
    amp = 10.0 ** (peak_dbfs / 20.0)
    stereo = np.zeros((n, 2))
    stereo[:, 0] = amp * np.sin(2.0 * np.pi * hz * t)
    wavfile.write(str(path), rate, (stereo * 32767.0).astype(np.int16))
    return path


def _full_band_dbfs(path) -> float:
    """The same max-over-all-channels reading ``seat_level.stimulus_peak_dbfs`` takes."""
    _rate, data = wavfile.read(str(path))
    array = np.asarray(data)
    peak = float(np.abs(array.astype(np.float64)).max())
    return 20.0 * math.log10(peak / float(np.iinfo(array.dtype).max))


# --- arithmetic ------------------------------------------------------------


def test_an_identity_graph_reproduces_the_full_band_peak(tmp_path):
    """The consistency contract with ``seat_level.stimulus_peak_dbfs``.

    A graph that does nothing must render a branch peak equal to the full-band
    peak the ceiling formula is otherwise fed — same WAV decode, same integer
    normalisation, same max-over-channels reading. If these two ever disagree,
    the per-branch ceiling and the full-band ceiling stop being comparable and
    the fallback silently changes the answer instead of only widening it.
    """
    wav = _tone_wav(tmp_path / "s.wav")
    config = {
        "devices": _devices(),
        "filters": {},
        "mixers": _passthru_mixer(),
        "pipeline": [{"type": "Mixer", "name": "passthru"}],
    }
    peaks = stimulus_branch_peaks_dbfs(config, wav, output_channels={"a": 0})
    assert peaks["a"] == pytest.approx(_full_band_dbfs(wav), abs=0.01)


def test_the_split_mixers_mono_sum_costs_exactly_its_leg_gain(tmp_path):
    wav = _tone_wav(tmp_path / "s.wav")
    config = {
        "devices": _devices(),
        "filters": {},
        "mixers": _split_mixer(),
        "pipeline": [{"type": "Mixer", "name": "split_active_2way"}],
    }
    peaks = stimulus_branch_peaks_dbfs(config, wav, output_channels={"w": 0, "t": 1})
    expected = _full_band_dbfs(wav) + MONO_SUM_GAIN_DB
    assert peaks["w"] == pytest.approx(expected, abs=0.02)
    assert peaks["t"] == pytest.approx(expected, abs=0.02)


def test_a_gain_moves_one_branch_and_leaves_its_sibling_alone(tmp_path):
    """Including the inverted spelling: JTS3's tweeter gain is inverted, and a
    polarity flip must not read as an attenuation."""
    wav = _tone_wav(tmp_path / "s.wav")
    config = {
        "devices": _devices(),
        "filters": {
            "trim": {
                "type": "Gain",
                "parameters": {"gain": -4.6081, "inverted": True, "mute": False},
            }
        },
        "mixers": _split_mixer(),
        "pipeline": [
            {"type": "Mixer", "name": "split_active_2way"},
            {"type": "Filter", "channels": [1], "names": ["trim"]},
        ],
    }
    peaks = stimulus_branch_peaks_dbfs(config, wav, output_channels={"w": 0, "t": 1})
    summed = _full_band_dbfs(wav) + MONO_SUM_GAIN_DB
    assert peaks["w"] == pytest.approx(summed, abs=0.02)
    assert peaks["t"] == pytest.approx(summed - 4.6081, abs=0.02)


def test_a_muted_gain_silences_its_branch(tmp_path):
    wav = _tone_wav(tmp_path / "s.wav")
    config = {
        "devices": _devices(),
        "filters": {
            "off": {
                "type": "Gain",
                "parameters": {"gain": 0.0, "inverted": False, "mute": True},
            }
        },
        "mixers": _split_mixer(),
        "pipeline": [
            {"type": "Mixer", "name": "split_active_2way"},
            {"type": "Filter", "channels": [1], "names": ["off"]},
        ],
    }
    peaks = stimulus_branch_peaks_dbfs(config, wav, output_channels={"t": 1})
    assert peaks["t"] < -200.0


def test_a_delay_is_all_pass_and_never_moves_a_branch_peak(tmp_path):
    wav = _tone_wav(tmp_path / "s.wav")
    config = {
        "devices": _devices(),
        "filters": {
            "d": {"type": "Delay", "parameters": {"delay": 1.5, "unit": "ms"}}
        },
        "mixers": _split_mixer(),
        "pipeline": [
            {"type": "Mixer", "name": "split_active_2way"},
            {"type": "Filter", "channels": [0], "names": ["d"]},
        ],
    }
    peaks = stimulus_branch_peaks_dbfs(config, wav, output_channels={"w": 0})
    assert peaks["w"] == pytest.approx(
        _full_band_dbfs(wav) + MONO_SUM_GAIN_DB, abs=0.02
    )


def test_the_crossover_pair_splits_the_program_between_the_branches(tmp_path):
    """A 300 Hz tone reaches the woofer through its low-pass essentially whole
    and the tweeter not at all — the very fact the full-band bound cannot know
    and therefore has to assume away."""
    wav = _tone_wav(tmp_path / "s.wav", hz=300.0)
    config = {
        "devices": _devices(),
        "filters": {
            "lp": {
                "type": "BiquadCombo",
                "parameters": {
                    "type": "LinkwitzRileyLowpass", "freq": 1648.7, "order": 4,
                },
            },
            "hp": {
                "type": "BiquadCombo",
                "parameters": {
                    "type": "LinkwitzRileyHighpass", "freq": 1648.7, "order": 4,
                },
            },
        },
        "mixers": _split_mixer(),
        "pipeline": [
            {"type": "Mixer", "name": "split_active_2way"},
            {"type": "Filter", "channels": [0], "names": ["lp"]},
            {"type": "Filter", "channels": [1], "names": ["hp"]},
        ],
    }
    peaks = stimulus_branch_peaks_dbfs(config, wav, output_channels={"w": 0, "t": 1})
    summed = _full_band_dbfs(wav) + MONO_SUM_GAIN_DB
    assert peaks["w"] == pytest.approx(summed, abs=0.2)
    assert peaks["t"] < summed - 25.0


def test_a_limiter_is_modelled_as_a_pass_through_on_purpose(tmp_path):
    """The one non-linear stage. It can only ever REDUCE a peak, so ignoring it
    over-reports the branch and binds the ceiling tighter — the safe direction.
    Modelling its soft-clip curve could only ever raise the ceiling."""
    wav = _tone_wav(tmp_path / "s.wav")
    without = {
        "devices": _devices(),
        "filters": {},
        "mixers": _passthru_mixer(),
        "pipeline": [{"type": "Mixer", "name": "passthru"}],
    }
    with_limiter = {
        **without,
        "filters": {
            "lim": {
                "type": "Limiter",
                "parameters": {"soft_clip": True, "clip_limit": -1.0},
            }
        },
        "pipeline": [
            {"type": "Mixer", "name": "passthru"},
            {"type": "Filter", "channels": [0], "names": ["lim"]},
        ],
    }
    bare = stimulus_branch_peaks_dbfs(without, wav, output_channels={"a": 0})
    limited = stimulus_branch_peaks_dbfs(with_limiter, wav, output_channels={"a": 0})
    assert limited["a"] == pytest.approx(bare["a"], abs=1e-9)


def test_a_stimulus_longer_than_one_render_block_is_still_exact(tmp_path):
    """The overlap-save loop, mutation-guarded by length: a five-second WAV
    crosses several blocks, and an off-by-one in the discarded head or the hop
    would show as a changed identity peak."""
    wav = _tone_wav(tmp_path / "long.wav", seconds=5.0)
    config = {
        "devices": _devices(),
        "filters": {},
        "mixers": _passthru_mixer(),
        "pipeline": [{"type": "Mixer", "name": "passthru"}],
    }
    peaks = stimulus_branch_peaks_dbfs(config, wav, output_channels={"a": 0})
    assert peaks["a"] == pytest.approx(_full_band_dbfs(wav), abs=0.01)


def test_camilladsp_readback_spellings_are_accepted(tmp_path):
    """CamillaDSP's own ``GetConfig`` dialect back-fills ``description``,
    ``bypassed``, ``scale``, ``subsample`` and per-source ``mute`` as ``null``
    and may spell a single channel as the scalar ``channel: N``. None of those
    is a refusal — reading only the emitted dialect would make this fall back
    to the conservative bound on every real box."""
    wav = _tone_wav(tmp_path / "s.wav")
    config = {
        "devices": _devices(),
        "filters": {
            "g": {
                "type": "Gain",
                "description": None,
                "parameters": {
                    "gain": -3.0, "inverted": False, "mute": False, "scale": None,
                },
            },
            "d": {
                "type": "Delay",
                "description": None,
                "parameters": {"delay": 0.0, "unit": "ms", "subsample": None},
            },
        },
        "mixers": {
            "split_active_2way": {
                "channels": {"in": 2, "out": 2},
                "mapping": [
                    {
                        "dest": d,
                        "mute": None,
                        "sources": [
                            {
                                "channel": c,
                                "gain": MONO_SUM_GAIN_DB,
                                "inverted": False,
                                "mute": None,
                                "scale": None,
                            }
                            for c in (0, 1)
                        ],
                    }
                    for d in (0, 1)
                ],
            }
        },
        "pipeline": [
            {"type": "Mixer", "name": "split_active_2way", "bypassed": None},
            {"type": "Filter", "channel": 0, "names": ["g", "d"], "bypassed": None},
        ],
    }
    peaks = stimulus_branch_peaks_dbfs(config, wav, output_channels={"w": 0})
    assert peaks["w"] == pytest.approx(
        _full_band_dbfs(wav) + MONO_SUM_GAIN_DB - 3.0, abs=0.02
    )


# --- fail-conservative refusals -------------------------------------------


def _refuses(config, wav, **kwargs):
    with pytest.raises(BranchPeakError):
        stimulus_branch_peaks_dbfs(
            config, wav, output_channels=kwargs.pop("output_channels", {"a": 0})
        )


@pytest.mark.parametrize(
    "filter_spec, why",
    [
        ({"type": "Conv", "parameters": {"filename": "x.wav"}}, "an FIR"),
        ({"type": "Volume", "parameters": {"ramp_time": 200}}, "a runtime fader"),
        (
            {"type": "Biquad", "parameters": {"type": "LinkwitzTransform",
                                              "freq_act": 40.0, "q_act": 0.7,
                                              "freq_target": 30.0, "q_target": 0.7}},
            "a biquad shape the shared evaluator would silently treat as Peaking",
        ),
        (
            {"type": "BiquadCombo", "parameters": {"type": "ButterworthHighpass",
                                                   "freq": 60.0, "order": 2}},
            "a combo the crossover evaluator does not realise",
        ),
        ({"type": "Gain", "parameters": {"gain": 1.0, "scale": "volts"}}, "a gain unit"),
        (
            {"type": "Delay", "parameters": {"delay": 2.0, "unit": "furlongs"}},
            "a delay unit",
        ),
    ],
)
def test_an_unmodelled_filter_refuses_rather_than_being_skipped(
    tmp_path, filter_spec, why
):
    """The load-bearing safety property. Skipping an unknown filter would
    under-report a branch peak, and an under-reported branch peak RAISES the
    derived ceiling — the only direction that is unsafe. Every one of these
    becomes the caller's fallback to the conservative full-band bound."""
    wav = _tone_wav(tmp_path / "s.wav")
    config = {
        "devices": _devices(),
        "filters": {"x": filter_spec},
        "mixers": _passthru_mixer(),
        "pipeline": [
            {"type": "Mixer", "name": "passthru"},
            {"type": "Filter", "channels": [0], "names": ["x"]},
        ],
    }
    _refuses(config, wav)


def test_a_delay_past_the_render_overlap_refuses(tmp_path):
    """Beyond the discarded head the render would wrap instead of delaying."""
    wav = _tone_wav(tmp_path / "s.wav")
    config = {
        "devices": _devices(),
        "filters": {
            "d": {"type": "Delay", "parameters": {"delay": 500.0, "unit": "ms"}}
        },
        "mixers": _passthru_mixer(),
        "pipeline": [
            {"type": "Mixer", "name": "passthru"},
            {"type": "Filter", "channels": [0], "names": ["d"]},
        ],
    }
    _refuses(config, wav)


def test_a_bypassed_pipeline_step_refuses(tmp_path):
    wav = _tone_wav(tmp_path / "s.wav")
    config = {
        "devices": _devices(),
        "filters": {},
        "mixers": _passthru_mixer(),
        "pipeline": [{"type": "Mixer", "name": "passthru", "bypassed": True}],
    }
    _refuses(config, wav)


def test_an_undefined_filter_name_refuses(tmp_path):
    wav = _tone_wav(tmp_path / "s.wav")
    config = {
        "devices": _devices(),
        "filters": {},
        "mixers": _passthru_mixer(),
        "pipeline": [
            {"type": "Mixer", "name": "passthru"},
            {"type": "Filter", "channels": [0], "names": ["ghost"]},
        ],
    }
    _refuses(config, wav)


def test_an_unknown_pipeline_step_or_mixer_refuses(tmp_path):
    wav = _tone_wav(tmp_path / "s.wav")
    _refuses(
        {
            "devices": _devices(),
            "filters": {},
            "mixers": _passthru_mixer(),
            "pipeline": [{"type": "Processor", "name": "compressor"}],
        },
        wav,
    )
    _refuses(
        {
            "devices": _devices(),
            "filters": {},
            "mixers": {},
            "pipeline": [{"type": "Mixer", "name": "absent"}],
        },
        wav,
    )


def test_a_rate_the_shared_evaluator_does_not_model_refuses(tmp_path):
    """The evaluator prewarps at 48 kHz. Modelling a 44.1 kHz graph with it
    would place every corner frequency wrong, so both the graph's rate and the
    stimulus's are checked."""
    wav = _tone_wav(tmp_path / "s.wav")
    _refuses(
        {
            "devices": _devices(rate=44100),
            "filters": {},
            "mixers": _passthru_mixer(),
            "pipeline": [{"type": "Mixer", "name": "passthru"}],
        },
        wav,
    )
    rate = 44100
    t = np.arange(rate) / rate
    stereo = np.zeros((rate, 2))
    stereo[:, 0] = 0.25 * np.sin(2.0 * np.pi * 300.0 * t)
    odd = tmp_path / "odd.wav"
    wavfile.write(str(odd), rate, (stereo * 32767.0).astype(np.int16))
    _refuses(
        {
            "devices": _devices(),
            "filters": {},
            "mixers": _passthru_mixer(),
            "pipeline": [{"type": "Mixer", "name": "passthru"}],
        },
        odd,
    )


def test_a_channel_count_mismatch_refuses(tmp_path):
    """A mono stimulus against a two-channel capture: the mono/multiway
    mismatch, which would otherwise be rendered against the wrong legs."""
    rate = 48000
    t = np.arange(rate) / rate
    mono = tmp_path / "mono.wav"
    wavfile.write(
        str(mono), rate, (0.25 * np.sin(2 * np.pi * 300 * t) * 32767.0).astype(np.int16)
    )
    _refuses(
        {
            "devices": _devices(),
            "filters": {},
            "mixers": _passthru_mixer(),
            "pipeline": [{"type": "Mixer", "name": "passthru"}],
        },
        mono,
    )


def test_an_output_channel_the_graph_does_not_reach_refuses(tmp_path):
    wav = _tone_wav(tmp_path / "s.wav")
    _refuses(
        {
            "devices": _devices(),
            "filters": {},
            "mixers": _passthru_mixer(),
            "pipeline": [{"type": "Mixer", "name": "passthru"}],
        },
        wav,
        output_channels={"a": 7},
    )


def test_a_stimulus_past_the_render_bound_refuses(tmp_path, monkeypatch):
    """Peak memory has exactly one bound and this is it.

    Everything after the decode is per-block, so the decoded float64 copy is the
    high-water mark and the frame count is what caps it. Driven by lowering the
    bound rather than by writing a 60-second WAV, so the guard is pinned without
    the fixture cost it exists to prevent.
    """
    # The shipped bound leaves a real seat-leveling stimulus far inside it:
    # seconds of program against a 60-second ceiling. Asserted BEFORE the
    # monkeypatch, which is still in force for the rest of this test.
    assert branch_peak._MAX_STIMULUS_SAMPLES == 48_000 * 60

    wav = _tone_wav(tmp_path / "s.wav", seconds=1.0)
    monkeypatch.setattr(branch_peak, "_MAX_STIMULUS_SAMPLES", 4096)
    _refuses(
        {
            "devices": _devices(),
            "filters": {},
            "mixers": _passthru_mixer(),
            "pipeline": [{"type": "Mixer", "name": "passthru"}],
        },
        wav,
    )


def test_no_requested_channels_refuses(tmp_path):
    wav = _tone_wav(tmp_path / "s.wav")
    _refuses(
        {
            "devices": _devices(),
            "filters": {},
            "mixers": _passthru_mixer(),
            "pipeline": [{"type": "Mixer", "name": "passthru"}],
        },
        wav,
        output_channels={},
    )


# --- the target adapter ----------------------------------------------------


def test_branch_peaks_for_targets_keys_by_fingerprint_via_output_index(tmp_path):
    """The pairing that makes the ceiling safe.

    A driver target's ``output_index`` IS the CamillaDSP playback channel:
    ``staging`` builds every ``OutputChannel.index`` from the topology's
    ``physical_output_index``, ``camilla_yaml._emit_split_mixer`` uses that
    index as the mixer ``dest``, and the per-role pipeline step names the same
    index in ``channels:``. Pairing a driver's cap with a SIBLING's branch peak
    would be arbitrary, so this pins that the adapter reads the index rather
    than target order — note the tweeter here sits on channel 0, the woofer on
    channel 1, exactly as the shipped 2-way fixture spells it.
    """
    wav = _tone_wav(tmp_path / "s.wav")
    config = {
        "devices": _devices(),
        "filters": {
            "quiet": {
                "type": "Gain",
                "parameters": {"gain": -20.0, "inverted": False, "mute": False},
            }
        },
        "mixers": _split_mixer(),
        "pipeline": [
            {"type": "Mixer", "name": "split_active_2way"},
            {"type": "Filter", "channels": [0], "names": ["quiet"]},
        ],
    }
    peaks = branch_peaks_for_targets(
        config,
        wav,
        [
            {"target_fingerprint": "fp-tweeter", "output_index": 0},
            {"target_fingerprint": "fp-woofer", "output_index": 1},
        ],
    )
    summed = _full_band_dbfs(wav) + MONO_SUM_GAIN_DB
    # The -20 dB trim sits on channel 0, so it must land on the TWEETER's key.
    assert peaks["fp-tweeter"] == pytest.approx(summed - 20.0, abs=0.02)
    assert peaks["fp-woofer"] == pytest.approx(summed, abs=0.02)


def test_a_target_without_a_usable_output_index_refuses(tmp_path):
    wav = _tone_wav(tmp_path / "s.wav")
    config = {
        "devices": _devices(),
        "filters": {},
        "mixers": _passthru_mixer(),
        "pipeline": [{"type": "Mixer", "name": "passthru"}],
    }
    with pytest.raises(BranchPeakError):
        branch_peaks_for_targets(
            config, wav, [{"target_fingerprint": "fp", "output_index": None}]
        )
    with pytest.raises(BranchPeakError):
        branch_peaks_for_targets(config, wav, [{"output_index": 0}])
