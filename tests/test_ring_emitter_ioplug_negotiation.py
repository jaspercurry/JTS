# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Emitter-config <-> ring-ioplug <-> CamillaDSP negotiation contract.

The product ring path couples three layers:

1. jts_ring ioplug constraints: fixed period == slot frames, fixed periods ==
   n_slots, fixed buffer == slot_frames*n_slots.
2. ALSA negotiation: CamillaDSP requests a larger buffer, but the ioplug's
   min==max constraints clamp the negotiated outcome to the ring geometry.
3. CamillaDSP acceptance: the shipped v4.1.3 threaded ALSA path must have
   negotiated headroom beyond one Camilla chunk. The old 2-slot/chunk-256
   pairing fails because one read consumes the entire buffer.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from jasper import ring_assets
from jasper.fanin_coupling import (
    DEFAULT_FANIN_RING_SLOTS,
    RING_CAMILLA_CHUNKSIZE,
    RING_CAMILLA_ENABLE_RATE_ADJUST,
    RING_CAMILLA_QUEUELIMIT,
    RING_CAMILLA_TARGET_LEVEL,
    RING_SLOT_FRAMES,
)
from jasper.ring_negotiation import accept, ioplug_constraints, negotiate
from jasper.sound.camilla_yaml import emit_flat_ring_config


ROOT = Path(__file__).resolve().parents[1]
CONF_D = ROOT / "deploy" / "alsa" / "conf.d" / "60-jts-ring.conf"
IOPLUG_C = ROOT / "c" / "jts-ring-ioplug" / "pcm_jts_ring.c"
RUST_FANIN_CONFIG = ROOT / "rust" / "jasper-fanin" / "src" / "config.rs"
ARM_RING_A = ROOT / "scripts" / "ring-proto" / "arm-ring-a.sh"
CAMILLADSP_TAG = "v4.1.3"
CAMILLADSP_COMMIT = "05e9cfc"


def _flat_ring_devices() -> dict:
    return yaml.safe_load(emit_flat_ring_config())["devices"]


def _ioplug_default_period_frames() -> int:
    """The ioplug's compiled default period (JTS_RING_DEFAULT_PERIOD)."""
    m = re.search(
        r"#define\s+JTS_RING_DEFAULT_PERIOD\s+(\d+)",
        IOPLUG_C.read_text(encoding="utf-8"),
    )
    assert m is not None, "JTS_RING_DEFAULT_PERIOD must be defined in the ioplug"
    return int(m.group(1))


def _ioplug_advertises_period_and_periods_fixed() -> None:
    """Confirm the ioplug pins BOTH period_bytes and periods as min==max."""
    src = IOPLUG_C.read_text(encoding="utf-8")
    assert re.search(
        r"SND_PCM_IOPLUG_HW_PERIOD_BYTES,\s*period_bytes,\s*\n?\s*period_bytes",
        src,
    ), "the ioplug must pin PERIOD_BYTES as min==max (one slot)"
    assert re.search(
        r"SND_PCM_IOPLUG_HW_PERIODS,\s*p->n_slots,\s*\n?\s*p->n_slots",
        src,
    ), "the ioplug must pin PERIODS as min==max (== n_slots)"


def test_ioplug_geometry_is_fixed_min_equals_max():
    _ioplug_advertises_period_and_periods_fixed()


def test_ring_a_default_slots_match_conf_d_and_ioplug_period():
    period_frames = ring_assets.ring_conf_period_frames(str(CONF_D))
    n_slots = ring_assets.ring_conf_n_slots(ring_assets.RING_A_CONF_PCM, str(CONF_D))
    rust_config = RUST_FANIN_CONFIG.read_text(encoding="utf-8")

    assert period_frames == _ioplug_default_period_frames() == RING_SLOT_FRAMES
    assert f"pub const RING_SLOT_FRAMES: u32 = {RING_SLOT_FRAMES};" in rust_config
    assert n_slots == DEFAULT_FANIN_RING_SLOTS == 2
    assert n_slots * period_frames == 256


def test_ioplug_constraint_space_derives_from_product_constants():
    space = ioplug_constraints()

    assert space.ok
    assert space.period_frames == RING_SLOT_FRAMES
    assert space.periods == DEFAULT_FANIN_RING_SLOTS
    assert space.buffer_frames == RING_SLOT_FRAMES * DEFAULT_FANIN_RING_SLOTS


def test_camilla_request_is_documented_but_negotiated_outcome_is_fixed():
    """CamillaDSP's request formula is not the asserted outcome.

    Formula source: CamillaDSP v4.1.3 (05e9cfc)
    src/alsa_backend/threaded_buffermanager.rs::
    DeviceBufferManager::calculate_buffer_size. The same request is mirrored by
    scripts/ring-proto/arm-ring-a.sh's 8-slot lab default notes.
    """

    outcome = negotiate(chunksize=RING_CAMILLA_CHUNKSIZE)

    assert outcome.requested_buffer_frames > outcome.negotiated_buffer_frames
    assert outcome.negotiated_buffer_frames == RING_SLOT_FRAMES * DEFAULT_FANIN_RING_SLOTS
    assert outcome.requested_period_frames == outcome.negotiated_buffer_frames // 8
    assert outcome.negotiated_period_frames == RING_SLOT_FRAMES
    assert "8 slots * 128 = 1024-frame buffer" in ARM_RING_A.read_text(
        encoding="utf-8"
    )


def test_ring_coupled_camilla_emitter_matches_two_slot_ring_geometry():
    devices = _flat_ring_devices()
    chunksize = devices["chunksize"]
    target_level = devices["target_level"]
    queuelimit = devices["queuelimit"]
    enable_rate_adjust = devices["enable_rate_adjust"]
    period_frames = ring_assets.ring_conf_period_frames(str(CONF_D))
    n_slots = ring_assets.ring_conf_n_slots(ring_assets.RING_A_CONF_PCM, str(CONF_D))

    assert chunksize == RING_CAMILLA_CHUNKSIZE == 128
    assert target_level == RING_CAMILLA_TARGET_LEVEL == 128
    assert queuelimit == RING_CAMILLA_QUEUELIMIT == 1
    assert enable_rate_adjust is RING_CAMILLA_ENABLE_RATE_ADJUST is False
    assert period_frames == RING_SLOT_FRAMES
    assert n_slots == DEFAULT_FANIN_RING_SLOTS

    outcome = negotiate(chunksize=chunksize, slot_frames=period_frames, n_slots=n_slots)
    ok, reason = accept(outcome, chunk=chunksize, target_level=target_level)
    assert ok, reason


@pytest.mark.parametrize(
    ("n_slots", "chunk", "target_level", "accepted", "reason_fragment"),
    [
        pytest.param(
            8,
            256,
            1536,
            True,
            "accepted",
            id="8-slot chunk-256 target-1536 deployed-day-anchor",
        ),
        pytest.param(
            DEFAULT_FANIN_RING_SLOTS,
            RING_CAMILLA_CHUNKSIZE,
            RING_CAMILLA_TARGET_LEVEL,
            True,
            "accepted",
            id="2-slot chunk-128 product-path-anchor",
        ),
        pytest.param(
            DEFAULT_FANIN_RING_SLOTS,
            256,
            RING_CAMILLA_TARGET_LEVEL,
            False,
            "chunk == entire buffer",
            id="2-slot chunk-256 zero-headroom-rejected-anchor",
        ),
    ],
)
def test_camilladsp_ioplug_acceptance_hardware_anchors(
    n_slots: int,
    chunk: int,
    target_level: int,
    accepted: bool,
    reason_fragment: str,
) -> None:
    """Validate the source-derived model against the three on-hardware anchors.

    CamillaDSP citations for the acceptance layer:
    v4.1.3 (05e9cfc)
    - src/alsa_backend/threaded_device.rs::open_pcm
    - src/alsa_backend/threaded_buffermanager.rs::apply_avail_min
    - src/alsa_backend/threaded_device.rs::AlsaCaptureDevice::start
    - src/alsa_backend/threaded_device.rs::prime_playback_delay
    """

    assert CAMILLADSP_TAG == "v4.1.3"
    assert CAMILLADSP_COMMIT == "05e9cfc"

    outcome = negotiate(chunksize=chunk, n_slots=n_slots)
    ok, reason = accept(outcome, chunk=chunk, target_level=target_level)

    assert ok is accepted
    assert reason_fragment in reason
