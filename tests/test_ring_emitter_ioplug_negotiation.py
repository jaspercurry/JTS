# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Emitter-config <-> ring-ioplug geometry contract.

The product ring path couples three values in lockstep:

* Ring A ``n_slots`` (Python/Rust/conf.d default)
* the ioplug's fixed ``period_frames``
* the CamillaDSP ring-emitted chunk/target/queue/rate-adjust shape

This test parses those values from the real sources and checks the relationship
that matters for the 2-slot product default. A future slot default or emitter
change must update the whole set together; an old chunk-256 emitter paired with
the 2-slot ring would fail here because one CamillaDSP read would span the whole
ring buffer.
"""

from __future__ import annotations

import re
from pathlib import Path

from jasper import ring_assets
from jasper.fanin_coupling import (
    DEFAULT_FANIN_RING_SLOTS,
    RING_CAMILLA_CHUNKSIZE,
    RING_CAMILLA_ENABLE_RATE_ADJUST,
    RING_CAMILLA_QUEUELIMIT,
    RING_CAMILLA_TARGET_LEVEL,
)
from jasper.sound.camilla_yaml import emit_flat_ring_config


ROOT = Path(__file__).resolve().parents[1]
CONF_D = ROOT / "deploy" / "alsa" / "conf.d" / "60-jts-ring.conf"
IOPLUG_C = ROOT / "c" / "jts-ring-ioplug" / "pcm_jts_ring.c"


def _device_int_field(yaml: str, field: str) -> int:
    m = re.search(rf"^\s*{re.escape(field)}:\s*(\d+)\s*$", yaml, re.MULTILINE)
    assert m is not None, f"ring emitter must render devices.{field}"
    return int(m.group(1))


def _device_bool_field(yaml: str, field: str) -> bool:
    m = re.search(rf"^\s*{re.escape(field)}:\s*(true|false)\s*$", yaml, re.MULTILINE)
    assert m is not None, f"ring emitter must render devices.{field}"
    return m.group(1) == "true"


def _ring_chunk_fits_ring_buffer(
    *, chunksize: int, period_frames: int, n_slots: int
) -> bool:
    buffer_frames = n_slots * period_frames
    return (
        chunksize < buffer_frames
        and buffer_frames % chunksize == 0
        and chunksize % period_frames == 0
    )


def _ioplug_default_period_frames() -> int:
    """The ioplug's compiled default period (JTS_RING_DEFAULT_PERIOD)."""
    m = re.search(r"#define\s+JTS_RING_DEFAULT_PERIOD\s+(\d+)", IOPLUG_C.read_text())
    assert m is not None, "JTS_RING_DEFAULT_PERIOD must be defined in the ioplug"
    return int(m.group(1))


def _ioplug_advertises_period_and_periods_fixed() -> None:
    """Confirm the ioplug pins BOTH period_bytes and periods as min==max."""
    src = IOPLUG_C.read_text()
    assert re.search(
        r"SND_PCM_IOPLUG_HW_PERIOD_BYTES,\s*period_bytes,\s*\n?\s*period_bytes",
        src,
    ), "the ioplug must pin PERIOD_BYTES as min==max (one slot), the fixed geometry"
    assert re.search(
        r"SND_PCM_IOPLUG_HW_PERIODS,\s*p->n_slots,\s*\n?\s*p->n_slots",
        src,
    ), "the ioplug must pin PERIODS as min==max (== n_slots), the fixed geometry"


def test_ioplug_geometry_is_fixed_min_equals_max():
    _ioplug_advertises_period_and_periods_fixed()


def test_ring_a_default_slots_match_conf_d_and_ioplug_period():
    period_frames = ring_assets.ring_conf_period_frames(str(CONF_D))
    n_slots = ring_assets.ring_conf_n_slots(ring_assets.RING_A_CONF_PCM, str(CONF_D))

    assert period_frames == _ioplug_default_period_frames() == 128
    assert n_slots == DEFAULT_FANIN_RING_SLOTS == 2
    assert n_slots * period_frames == 256


def test_ring_coupled_camilla_emitter_matches_two_slot_ring_geometry():
    yaml = emit_flat_ring_config()
    chunksize = _device_int_field(yaml, "chunksize")
    target_level = _device_int_field(yaml, "target_level")
    queuelimit = _device_int_field(yaml, "queuelimit")
    enable_rate_adjust = _device_bool_field(yaml, "enable_rate_adjust")
    period_frames = ring_assets.ring_conf_period_frames(str(CONF_D))
    n_slots = ring_assets.ring_conf_n_slots(ring_assets.RING_A_CONF_PCM, str(CONF_D))

    assert chunksize == RING_CAMILLA_CHUNKSIZE == 128
    assert target_level == RING_CAMILLA_TARGET_LEVEL == 128
    assert queuelimit == RING_CAMILLA_QUEUELIMIT == 1
    assert enable_rate_adjust is RING_CAMILLA_ENABLE_RATE_ADJUST is False
    assert period_frames is not None and n_slots is not None
    assert _ring_chunk_fits_ring_buffer(
        chunksize=chunksize,
        period_frames=period_frames,
        n_slots=n_slots,
    )


def test_old_chunk_256_would_not_fit_the_two_slot_default():
    period_frames = ring_assets.ring_conf_period_frames(str(CONF_D))
    n_slots = ring_assets.ring_conf_n_slots(ring_assets.RING_A_CONF_PCM, str(CONF_D))
    assert period_frames is not None and n_slots is not None

    assert not _ring_chunk_fits_ring_buffer(
        chunksize=256,
        period_frames=period_frames,
        n_slots=n_slots,
    ), "chunk 256 spans the whole 2-slot ring buffer and must not ship with it"
