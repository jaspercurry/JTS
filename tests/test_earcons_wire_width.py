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

import numpy as np
import pytest

from jasper.assistant_loudness import SPINE_SCALE, measure_pcm_24k_mono
from jasper.voice.earcons import (
    generate_listening_chirp,
    generate_mute_click,
    synthetic_audio_profile,
)

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
        "chirp_on": generate_listening_chirp(going_on=True, wide=wide),
        "chirp_off": generate_listening_chirp(going_on=False, wide=wide),
        "mute_on": generate_mute_click(going_on=True, wide=wide),
        "mute_off": generate_mute_click(going_on=False, wide=wide),
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
    assert generate_listening_chirp(going_on=True) == generate_listening_chirp(
        going_on=True, wide=False
    )
    assert generate_mute_click(going_on=False) == generate_mute_click(
        going_on=False, wide=False
    )


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


# ---------------------------------------------------------------------------
# The loudness profile must describe the SOUND, not the container.
# ---------------------------------------------------------------------------


def test_an_earcon_reports_the_same_loudness_at_both_widths():
    """Outputd decides gain from this profile; a width-dependent number would
    make a wide box play its earcons at a different level."""
    narrow = generate_listening_chirp(going_on=True)
    wide = generate_listening_chirp(going_on=True, wide=True)
    n = measure_pcm_24k_mono(narrow)
    w = measure_pcm_24k_mono(wide, wide=True)
    assert abs(n.source_lufs - w.source_lufs) < 0.05
    assert abs(n.source_peak_dbfs - w.source_peak_dbfs) < 0.05
    assert n.total_duration_sec == pytest.approx(w.total_duration_sec, abs=1e-3)


def test_the_synthetic_profile_carries_the_width_through_to_the_measurement():
    wide = generate_mute_click(going_on=True, wide=True)
    narrow = generate_mute_click(going_on=True)
    wide_profile = synthetic_audio_profile(
        model="synthetic-mute-click", voice="unmute", pcm=wide, wide=True
    )
    narrow_profile = synthetic_audio_profile(
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
    wide = generate_listening_chirp(going_on=True, wide=True)
    correct = measure_pcm_24k_mono(wide, wide=True)
    misread = measure_pcm_24k_mono(wide)
    assert abs(correct.source_lufs - misread.source_lufs) > 1.0
