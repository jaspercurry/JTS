# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Generated earcons bake at the box's wire width (U2 PR-2, #2223).

An earcon is rendered in float and then quantized once. Before this, that
quantization was always S16 — so the recipe's float detail was flattened onto
the 16-bit grid at BAKE time, before the resampler and long before the wide
wire could have carried it. A wide box now bakes at the wire's own grid.

Same two bars as ``tests/test_tts_wire_width.py``:

* the narrow bake is frozen, pinned against hashes captured by running
  ``origin/main``'s ``jasper/`` tree;
* the wide bake carries sub-S16-LSB detail, asserted as a contrast with what
  the narrow bake did with the same recipe.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pytest

from jasper.assistant_loudness import SPINE_SCALE, measure_pcm_24k_mono
from jasper.voice import earcons
from jasper.voice.earcons import (
    _CHIME_ASCENDING,
    _I32_MAX,
    _I32_MIN,
    _PCM32_FULL_SCALE,
    _generate_listening_chirp,
    _generate_mute_click,
    _normalized,
    _render_layers,
    _synthetic_audio_profile,
    _to_pcm16,
    _to_pcm32,
)

_REPO = Path(__file__).resolve().parents[1]

# Captured by running `git archive origin/main jasper/` and re-baking each
# earcon with the pre-change `_to_pcm16` at 1caff2304 (2026-08-12). These are
# the bytes the fleet has always heard.
_NARROW_GOLDEN = {
    "chirp_on": (
        29_090,
        "753141b9d5fb6aa67a3c413f3cb05a1d7ad9b604c40ca2da66b80c807b5ee912",
    ),
    "chirp_off": (
        29_090,
        "c8d9b3de2b71e14c89e61811aadcca9940d3e99795b55bf99522759f3637115e",
    ),
    "mute_on": (
        24_386,
        "7ec535973d1880829b61ae2626b3f9070de03f4c27ebc95c93fe280d8f8e80b2",
    ),
    "mute_off": (
        24_386,
        "63d80560e0059cb1828d2b11d20a2f8465b1cba4205ea95635c0ba23327ff68e",
    ),
}


def _bakes(*, wide: bool) -> dict[str, bytes]:
    return {
        "chirp_on": _generate_listening_chirp(going_on=True, wide=wide),
        "chirp_off": _generate_listening_chirp(going_on=False, wide=wide),
        "mute_on": _generate_mute_click(going_on=True, wide=wide),
        "mute_off": _generate_mute_click(going_on=False, wide=wide),
    }


# ---------------------------------------------------------------------------
# Narrow: frozen.
# ---------------------------------------------------------------------------


def test_every_narrow_earcon_bake_is_byte_identical_to_its_committed_golden():
    for name, pcm in _bakes(wide=False).items():
        expected_len, expected_sha = _NARROW_GOLDEN[name]
        assert len(pcm) == expected_len, name
        assert hashlib.sha256(pcm).hexdigest() == expected_sha, name


def test_the_default_bake_is_the_narrow_one():
    """A caller that says nothing gets exactly what it always got."""
    assert _generate_listening_chirp(going_on=True) == _generate_listening_chirp(
        going_on=True, wide=False
    )
    assert _generate_mute_click(going_on=False) == _generate_mute_click(
        going_on=False, wide=False
    )


def _probe_buffer() -> list[float]:
    """A buffer long enough that its head sits outside `_normalized`'s tail fade.

    The fade is `_TAIL_FADE_SEC * _SR` = 120 samples, so anything shorter than
    that is entirely inside it and measures the fade rather than the packer.
    """
    head = [1.0, 0.31, -0.31, 0.077, -0.077, 0.013, -0.013]
    return head + [0.0] * 500


def test_the_narrow_packer_truncates_toward_zero_as_it_always_has():
    """`int(v * 32767.0)` truncates toward zero, and that is deliberate.

    Rounding "better" here would change the earcon on every box in the fleet.
    The probe is asserted to DISTINGUISH truncation from rounding first — a
    probe on which both agree would pin nothing.
    """
    buf = _probe_buffer()
    envelope = list(_normalized(buf))
    distinguishing = [
        i
        for i, v in enumerate(envelope[: len(buf) - 120])
        if int(v * 32_767.0) != round(v * 32_767.0)
    ]
    assert distinguishing, "the probe must reach a value the two rules disagree on"

    samples = np.frombuffer(_to_pcm16(buf), dtype="<i2").astype(np.int64)
    for i in distinguishing:
        assert samples[i] == int(envelope[i] * 32_767.0), (
            f"sample {i} was rounded, not truncated"
        )
    # Truncation toward zero is symmetric; floor is not. The recipe head is a
    # mirrored pair, so this separates the two.
    assert samples[1] == -samples[2]
    assert samples[3] == -samples[4]


# ---------------------------------------------------------------------------
# Wide: carries the recipe's own detail.
# ---------------------------------------------------------------------------


def test_the_wide_bake_is_the_same_sound_with_sub_lsb_detail_the_narrow_lost():
    for name, wide_pcm in _bakes(wide=True).items():
        narrow_pcm = _bakes(wide=False)[name]
        wide = np.frombuffer(wide_pcm, dtype="<i4").astype(np.int64)
        narrow = np.frombuffer(narrow_pcm, dtype="<i2").astype(np.int64)
        assert len(wide) == len(narrow), name
        assert len(wide_pcm) == 2 * len(narrow_pcm), name
        # Same signal at 2^16 times the scale: every wide sample is within one
        # narrow step of the narrow sample's promotion.
        assert np.all(np.abs(wide - narrow * SPINE_SCALE) <= SPINE_SCALE), name
        # THE CONTRAST: detail below the S16 grid, which the narrow bake had no
        # code for. A silent earcon would trivially satisfy the bound above.
        carried = int(np.count_nonzero(wide % int(SPINE_SCALE)))
        assert carried > len(wide) // 2, (
            f"{name}: only {carried}/{len(wide)} wide samples carry sub-LSB "
            "detail — the wide bake is not keeping what it claims to"
        )


def test_the_wide_full_scale_is_the_promoted_narrow_one_not_the_containers():
    """Level, not headroom.

    `i32::MAX` is one step above `32767 << 16`. Baking to the container's own
    top code would make every wide earcon a hair louder than its narrow twin
    and break the equivalence above — a level change disguised as a width
    change.
    """
    assert _PCM32_FULL_SCALE == 32_767 * 65_536
    assert _PCM32_FULL_SCALE == 32_767 << 16
    assert _PCM32_FULL_SCALE < _I32_MAX
    assert _I32_MIN == -(2 ** 31)
    assert _I32_MAX == 2 ** 31 - 1


def test_the_wide_packer_rounds_to_nearest_rather_than_truncating():
    """A NEW edge, so it takes the house rule: round-to-nearest, no dither.

    Note what is NOT asserted: saturation. `_normalized` scales every buffer to
    `_TARGET_PEAK` (0.5), so neither packer's clamp is reachable through the
    recipe path — the wide clamp mirrors the narrow one as defence in depth,
    and claiming a test covers it would be the "half-guarded site reads as
    covered" mistake.
    """
    buf = _probe_buffer()
    envelope = list(_normalized(buf))
    distinguishing = [
        i
        for i, v in enumerate(envelope[: len(buf) - 120])
        if int(v * _PCM32_FULL_SCALE) != round(v * _PCM32_FULL_SCALE)
    ]
    assert distinguishing, "the probe must reach a value the two rules disagree on"

    samples = np.frombuffer(_to_pcm32(buf), dtype="<i4").astype(np.int64)
    for i in distinguishing:
        assert samples[i] == round(envelope[i] * _PCM32_FULL_SCALE), (
            f"sample {i} was truncated, not rounded to nearest"
        )
    assert samples.max() <= _I32_MAX
    assert samples.min() >= _I32_MIN


def test_both_packers_share_one_normalization_and_one_fade():
    """The bakes differ ONLY in their final quantizer.

    `_normalized` is the shared envelope; if a packer grew its own scaling or
    its own fade the two would stop describing the same sound, which is the
    drift this factoring exists to prevent.
    """
    buf = _render_layers(_CHIME_ASCENDING.layers)
    envelope = list(_normalized(buf))
    narrow = np.frombuffer(_to_pcm16(buf), dtype="<i2").astype(np.float64)
    wide = np.frombuffer(_to_pcm32(buf), dtype="<i4").astype(np.float64)
    assert len(envelope) == len(narrow) == len(wide)
    # Each packer is its own constant times the SAME envelope.
    for i in (0, len(buf) // 3, len(buf) // 2, len(buf) - 1):
        assert abs(narrow[i] - int(envelope[i] * 32_767.0)) < 1.0
        assert abs(wide[i] - round(envelope[i] * _PCM32_FULL_SCALE)) < 1.0


# ---------------------------------------------------------------------------
# The loudness profile must describe the SOUND, not the container.
# ---------------------------------------------------------------------------


def test_an_earcon_reports_the_same_loudness_at_both_widths():
    """Outputd decides gain from this profile; a width-dependent number would
    make a wide box play its earcons at a different level."""
    narrow = _generate_listening_chirp(going_on=True)
    wide = _generate_listening_chirp(going_on=True, wide=True)
    n = measure_pcm_24k_mono(narrow)
    w = measure_pcm_24k_mono(wide, wide=True)
    assert abs(n.source_lufs - w.source_lufs) < 0.05
    assert abs(n.source_peak_dbfs - w.source_peak_dbfs) < 0.05
    assert n.total_duration_sec == pytest.approx(w.total_duration_sec, abs=1e-3)


def test_the_synthetic_profile_carries_the_width_through_to_the_measurement():
    wide = _generate_mute_click(going_on=True, wide=True)
    narrow = _generate_mute_click(going_on=True)
    wide_profile = _synthetic_audio_profile(
        model="synthetic-mute-click", voice="unmute", pcm=wide, wide=True
    )
    narrow_profile = _synthetic_audio_profile(
        model="synthetic-mute-click", voice="unmute", pcm=narrow
    )
    assert wide_profile.confidence == 1.0, "a wide bake must measure, not fall back"
    assert abs(wide_profile.source_lufs - narrow_profile.source_lufs) < 0.05
    assert abs(wide_profile.source_peak_dbfs - narrow_profile.source_peak_dbfs) < 0.05


def test_measuring_a_wide_buffer_as_narrow_is_visibly_wrong():
    """The `wide=` flag is load-bearing, not cosmetic.

    Reading S32 bytes as S16 does not merely rescale — it reinterprets each
    sample as two — so the guard is that the mis-read answer is far off, which
    is what makes forgetting the flag a loud failure rather than a quiet one.
    """
    wide = _generate_listening_chirp(going_on=True, wide=True)
    correct = measure_pcm_24k_mono(wide, wide=True)
    misread = measure_pcm_24k_mono(wide)
    assert abs(correct.source_lufs - misread.source_lufs) > 1.0


def test_earcons_module_is_the_one_the_worktree_owns():
    """Guard against a shared venv resolving `jasper` to another checkout."""
    assert Path(earcons.__file__).resolve().parents[2] == _REPO
