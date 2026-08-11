# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Ring v2 R7b — the ACTIVE ring's citizenship contracts.

The rung's structural claim is that a roleful box's post-crossover ring is told
apart from the full-range stereo ring by its NAME and by an endpoint MARKER,
never by its width — because on the first box this serves (jts3, a 2-way) both
rings are 2 channels and no width test can separate them. Everything here pins
some half of that claim, plus the inertness bar: no box changes behaviour until
an operator explicitly arms one.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from jasper import ring_assets
from jasper.active_speaker import camilla_yaml as active_camilla_yaml
from jasper.active_speaker.playback import FORBIDDEN_TEST_PCM_TOKENS
from jasper.active_speaker.runtime_contract import (
    GRAPH_APPROVED_ACTIVE_RUNTIME,
    GraphSafety,
    OUTPUTD_ACTIVE_PLAYBACK_DEVICE,
    OUTPUTD_ACTIVE_RING_PLAYBACK_DEVICE,
    OUTPUTD_LEGAL_ENDPOINT_DEVICES,
    _outputd_endpoint_width,
    active_ring_channels_for_topology,
    ring_channels_for_topology,
    topology_supports_shm_ring,
)
from jasper.fanin_coupling import (
    DEFAULT_OUTPUTD_ACTIVE_RING_PATH,
    OUTPUTD_RING_ACTIVE_ENDPOINT_ENV_VAR,
    RING_ACTIVE_PLAYBACK_DEVICE,
    RING_PLAYBACK_DEVICE,
    ring_active_endpoint_armed,
)

REPO = Path(__file__).resolve().parent.parent
RING_CONF = REPO / "deploy/alsa/conf.d/60-jts-ring.conf"
OUTPUTD_CONFIG_RS = REPO / "rust/jasper-outputd/src/config.rs"
HARDWARE_RECONCILE = REPO / "deploy/bin/jasper-audio-hardware-reconcile"


# --------------------------------------------------------------------------
# Local topology builders. Deliberately self-contained rather than imported
# from a sibling test module: no other test file does that, and it would need
# a sys.path insert plus an E402 suppression per import.
# --------------------------------------------------------------------------


def _topology(groups, routing=None, *, device_id="hifiberry_dac8x", outputs=8):
    from jasper.output_topology import OUTPUT_TOPOLOGY_KIND, OutputTopology

    return OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "bench",
        "name": "Bench speaker",
        "status": "draft",
        "hardware": {
            "device_id": device_id,
            "device_label": device_id,
            "physical_output_count": outputs,
            "card_id": "DAC8",
        },
        "speaker_groups": groups,
        "routing": routing or {},
    })


def _active_group(kind: str, mode: str, start: int) -> dict:
    roles = ("woofer", "tweeter") if mode == "active_2_way" else (
        "woofer",
        "mid",
        "tweeter",
    )
    channels = []
    for offset, role in enumerate(roles):
        channel = {
            "role": role,
            "physical_output_index": start + offset,
            "identity_verified": True,
        }
        if role == "tweeter":
            channel.update({
                "startup_muted": True,
                "protection_required": True,
                "protection_status": "software_guard_requested",
            })
        channels.append(channel)
    return {
        "id": kind,
        "label": f"{kind.title()} speaker",
        "kind": kind,
        "mode": mode,
        "channels": channels,
    }


def _active_topology(layout: str, mode: str):
    """A roleful (active-crossover) topology — the shape that HAS an active ring."""
    if layout == "mono":
        return _topology([_active_group("mono", mode, 0)], {"mono_group_id": "mono"})
    step = 2 if mode == "active_2_way" else 3
    return _topology(
        [_active_group("left", mode, 0), _active_group("right", mode, step)],
        {"main_left_group_id": "left", "main_right_group_id": "right"},
    )


def _full_range_stereo():
    """A passive stereo box — Ring B eligible, no active ring."""
    return _topology(
        [
            {
                "id": side,
                "label": f"{side.title()} speaker",
                "kind": side,
                "mode": "full_range_passive",
                "channels": [{"role": "full_range", "physical_output_index": index}],
            }
            for index, side in enumerate(("left", "right"))
        ],
        {"main_left_group_id": "left", "main_right_group_id": "right"},
    )


def _subwoofer_topology():
    """Roleful, but a single output — below the ring layout's 2-channel floor."""
    return _topology(
        [
            {
                "id": "sub",
                "label": "Subwoofer",
                "kind": "subwoofer",
                "mode": "subwoofer",
                "channels": [{"role": "subwoofer", "physical_output_index": 0}],
            }
        ],
        {"subwoofer_group_ids": ["sub"]},
    )


def _apple_dongle_shipped_default():
    """A shipped-default single-sink box: no groups, ONE recorded child device."""
    from jasper.output_topology import OUTPUT_TOPOLOGY_KIND, OutputTopology

    return OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "bench",
        "name": "Bench speaker",
        "status": "draft",
        "hardware": {
            "device_id": "apple_usb_c_dongle",
            "device_label": "Apple USB-C dongle",
            "physical_output_count": 2,
            "card_id": "A",
            "child_devices": [
                {
                    "child_id": "a",
                    "device_id": "apple_usb_c_dongle",
                    "device_label": "Apple A",
                    "physical_output_indexes": [0, 1],
                }
            ],
        },
        "speaker_groups": [],
        "routing": {},
    })


def _dual_apple_stereo():
    """A composite sink: TWO child devices, so >1 clock domain — no ring at all."""
    from jasper.output_topology import OUTPUT_TOPOLOGY_KIND, OutputTopology

    return OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "bench",
        "name": "Bench speaker",
        "status": "draft",
        "hardware": {
            "device_id": "dual_apple_usb_c_dac_4ch",
            "device_label": "Dual Apple",
            "physical_output_count": 4,
            "card_id": "A",
            "child_devices": [
                {
                    "child_id": "a",
                    "device_id": "apple_usb_c_dongle",
                    "device_label": "Apple A",
                    "physical_output_indexes": [0, 1],
                },
                {
                    "child_id": "b",
                    "device_id": "apple_usb_c_dongle",
                    "device_label": "Apple B",
                    "physical_output_indexes": [2, 3],
                },
            ],
        },
        "speaker_groups": [],
        "routing": {},
    })


def _ring_file(path, *, channels=2, sample_format=None, period=128, n_slots=2):
    """Write a VALID ring header at a chosen geometry (the attach-compared axes)."""
    import struct

    from jasper import ring_assets as ra

    if sample_format is None:
        sample_format = ra.RING_SAMPLE_FORMAT_S16LE
    hdr = bytearray(ra._RING_HEADER_BYTES)
    struct.pack_into("<I", hdr, ra._RING_OFF_MAGIC, 0x4A52_494E)
    struct.pack_into("<I", hdr, ra._RING_OFF_VERSION, 1)
    struct.pack_into("<I", hdr, ra._RING_OFF_RATE, 48000)
    struct.pack_into("<I", hdr, ra._RING_OFF_CHANNELS, channels)
    struct.pack_into("<I", hdr, ra._RING_OFF_SAMPLE_FORMAT, sample_format)
    struct.pack_into("<I", hdr, ra._RING_OFF_PERIOD_FRAMES, period)
    struct.pack_into("<I", hdr, ra._RING_OFF_N_SLOTS, n_slots)
    path.write_bytes(bytes(hdr) + b"\x00" * 256)
    return path


def _two_way_preset() -> dict:
    """A minimal MONO 2-way preset — the jts3 shape (woofer + horn tweeter)."""
    return {
        "artifact_schema_version": 1,
        "kind": "jts_active_speaker_preset",
        "preset_id": "DE250-E150HE44-v1",
        "name": "DE250 + E150HE-44 test preset",
        "way_count": 2,
        "channel_map": {
            "layout": "mono",
            "outputs": [
                {
                    "index": index,
                    "side": "mono",
                    "driver_role": role,
                    "label": f"mono {role}",
                    "startup_muted": True,
                }
                for index, role in enumerate(("woofer", "tweeter"))
            ],
        },
        "drivers": {
            "woofer": {
                "manufacturer": "Dayton Audio",
                "model": "Epique E150HE-44",
                "fs_hz": 40,
                "rated_power_w": 60,
            },
            "tweeter": {
                "manufacturer": "B&C Speakers",
                "model": "DE250",
                "rated_power_w": 60,
            },
        },
        "crossover_regions": [{
            "id": "woofer_tweeter",
            "lower_driver": "woofer",
            "upper_driver": "tweeter",
            "fc_hz": 1600,
            "target_type": "LinkwitzRiley",
            "order": 4,
            "lower_polarity": "non-inverted",
            "upper_polarity": "non-inverted",
            "delay_target_driver": "woofer",
            "delay_range_ms": [0.05, 0.30],
            "null_depth_threshold_db": 25,
        }],
        "safety": {
            "initial_sweep_level_db_spl": 65,
            "max_commissioning_level_db_spl": 85,
            "escalation_step_db": 5,
            "require_physical_tweeter_protection": True,
            "require_channel_identity_before_drivers": True,
            "emergency_stop_required": True,
        },
    }


def _mono_two_way_preset():
    from jasper.active_speaker import ActiveSpeakerPreset

    return ActiveSpeakerPreset.from_mapping(_two_way_preset())


# --------------------------------------------------------------------------
# 1. The name is the role carrier — and the SPELLING is load-bearing.
# --------------------------------------------------------------------------


def test_the_active_ring_name_does_not_contain_the_stereo_rings_name():
    """The spelling that makes the whole design work, pinned in both directions.

    ``_forbidden_playback_token`` is a case-insensitive SUBSTRING test and the
    STEREO ring is a forbidden active-playback token. This name was chosen so
    that test separates the two rings: had it been spelled
    ``jts_ring_playback_active`` — the equally natural word order — every active
    emit targeting it would self-block, and the rung would silently not work.
    """
    assert RING_PLAYBACK_DEVICE not in RING_ACTIVE_PLAYBACK_DEVICE
    # And the near-miss that WOULD have self-blocked, so the reason is visible.
    assert RING_PLAYBACK_DEVICE in "jts_ring_playback_active"
    assert active_camilla_yaml._forbidden_playback_token(
        RING_ACTIVE_PLAYBACK_DEVICE
    ) is None
    assert active_camilla_yaml._forbidden_playback_token(RING_PLAYBACK_DEVICE) == (
        RING_PLAYBACK_DEVICE
    )
    assert (
        active_camilla_yaml._forbidden_playback_token("jts_ring_playback_active")
        == RING_PLAYBACK_DEVICE
    )


def test_forbidden_active_playback_tokens_is_a_walking_class_guard():
    """EVERY forbidden token vs EVERY legitimate active device, walked.

    A per-token assertion cannot see a new token that overlaps a legitimate
    device, nor a new device that a standing token happens to match. Walking the
    cross-product is what makes "half-guarded" impossible here: adding either
    side re-runs the whole matrix.
    """
    legitimate = (
        OUTPUTD_ACTIVE_PLAYBACK_DEVICE,
        RING_ACTIVE_PLAYBACK_DEVICE,
    )
    assert active_camilla_yaml.FORBIDDEN_ACTIVE_PLAYBACK_TOKENS, "guard is empty"
    for device in legitimate:
        for token in active_camilla_yaml.FORBIDDEN_ACTIVE_PLAYBACK_TOKENS:
            assert token.lower() not in device.lower(), (
                f"forbidden token {token!r} is a substring of the LEGITIMATE "
                f"active device {device!r} — every active emit onto it would be "
                "refused"
            )
        assert active_camilla_yaml._forbidden_playback_token(device) is None


def test_the_stereo_ring_is_a_forbidden_active_playback_target():
    # Q5: an active graph carries post-crossover per-driver channels; Ring B
    # carries a full-range stereo program to a stereo sink. Targeting it would
    # put per-driver audio on a full-range path.
    assert RING_PLAYBACK_DEVICE in active_camilla_yaml.FORBIDDEN_ACTIVE_PLAYBACK_TOKENS


def test_both_rings_are_forbidden_test_pcm_targets():
    """An audio-lab tone must never be injected into either ring.

    Sharper than "wrong output": a ring is single-producer, and its epoch
    takeover ACCEPTS a second writer where a raw ALSA ``hw`` device would refuse
    with EBUSY. So a stray tone is admitted, not rejected — and on the active
    ring it lands post-crossover, where a full-range tone reaches a compression
    driver.
    """
    from jasper.active_speaker.playback import _forbidden_test_pcm_token

    for device in (RING_PLAYBACK_DEVICE, RING_ACTIVE_PLAYBACK_DEVICE):
        assert _forbidden_test_pcm_token(device) is not None, device
    assert RING_ACTIVE_PLAYBACK_DEVICE in FORBIDDEN_TEST_PCM_TOKENS


# --------------------------------------------------------------------------
# 2. Cross-language literals — four declarers, one spelling each.
# --------------------------------------------------------------------------


def test_the_active_device_name_is_spelled_identically_everywhere():
    conf = RING_CONF.read_text(encoding="utf-8")
    rust = OUTPUTD_CONFIG_RS.read_text(encoding="utf-8")
    shell = HARDWARE_RECONCILE.read_text(encoding="utf-8")

    assert f"pcm.{RING_ACTIVE_PLAYBACK_DEVICE} {{" in conf
    assert ring_assets.RING_ACTIVE_CONF_PCM == RING_ACTIVE_PLAYBACK_DEVICE
    assert OUTPUTD_ACTIVE_RING_PLAYBACK_DEVICE == RING_ACTIVE_PLAYBACK_DEVICE
    assert (
        f'RING_ACTIVE_OUTPUTD_PLAYBACK_DEVICE="{RING_ACTIVE_PLAYBACK_DEVICE}"' in shell
    )
    # The Rust side names the PATH, not the PCM (it never resolves ALSA names).
    assert (
        f'DEFAULT_ACTIVE_SHM_RING_PATH: &str = "{DEFAULT_OUTPUTD_ACTIVE_RING_PATH}"'
        in rust
    )


def test_the_active_ring_path_is_spelled_identically_everywhere():
    conf = RING_CONF.read_text(encoding="utf-8")
    assert f'path "{DEFAULT_OUTPUTD_ACTIVE_RING_PATH}"' in conf
    assert ring_assets.RING_ACTIVE_CONTENT_FILE == DEFAULT_OUTPUTD_ACTIVE_RING_PATH
    # ...and it is NOT the stereo ring's file.
    from jasper.fanin_coupling import DEFAULT_OUTPUTD_RING_PATH

    assert DEFAULT_OUTPUTD_ACTIVE_RING_PATH != DEFAULT_OUTPUTD_RING_PATH


def test_the_endpoint_marker_key_is_spelled_identically_in_both_languages():
    rust = OUTPUTD_CONFIG_RS.read_text(encoding="utf-8")
    shell = HARDWARE_RECONCILE.read_text(encoding="utf-8")
    assert f'env_bool("{OUTPUTD_RING_ACTIVE_ENDPOINT_ENV_VAR}", false)' in rust
    assert OUTPUTD_RING_ACTIVE_ENDPOINT_ENV_VAR in shell


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1", True),
        ("true", True),
        ("TRUE", True),
        ("  yes  ", True),
        ("on", True),
        ("", False),
        ("0", False),
        ("false", False),
        ("no", False),
        ("maybe", False),
    ],
)
def test_armed_vocabulary_matches_rust_env_bool(raw, expected):
    """"Armed" must mean the same thing in Python and in the daemon.

    The Rust reader accepts exactly ``1|true|yes|on`` case-insensitively after a
    trim. A Python reader that accepted more would arm a box the daemon then
    refuses; one that accepted less would report a box unarmed while it runs
    armed. Both are the same class of bug — two answers for one fact.
    """
    assert (
        ring_active_endpoint_armed({OUTPUTD_RING_ACTIVE_ENDPOINT_ENV_VAR: raw})
        is expected
    )
    rust = OUTPUTD_CONFIG_RS.read_text(encoding="utf-8")
    assert '"1" | "true" | "yes" | "on"' in rust


def test_an_absent_marker_reads_unarmed():
    assert ring_active_endpoint_armed({}) is False


# --------------------------------------------------------------------------
# 3. The width SPLIT (C-B1) — one field per ring end.
# --------------------------------------------------------------------------


ROLEFUL_CASES = [
    ("mono", "active_2_way", 2),
    ("mono", "active_3_way", 3),
    ("stereo", "active_2_way", 4),
    ("stereo", "active_3_way", 6),
]


@pytest.mark.parametrize("layout,mode,width", ROLEFUL_CASES)
def test_a_roleful_topology_resolves_an_active_width_and_no_ring_b_width(
    layout, mode, width
):
    """THE SPLIT, and why it is two functions rather than one widened one.

    ``ring_channels_for_topology`` answers for Ring B and its answer is stamped
    into the ``pcm.jts_ring_playback`` conf.d block. Had it been widened to
    return the ACTIVE width for a roleful box, that width would land in the
    STEREO block — and on the 2-way row below, where the active width is also 2,
    the corruption is numerically invisible. So the roleful answer stays ``None``
    there and lives in its own function.
    """
    topo = _active_topology(layout, mode)
    assert ring_channels_for_topology(topo) is None
    assert active_ring_channels_for_topology(topo) == width


@pytest.mark.parametrize(
    "name,factory",
    [
        ("apple shipped default", _apple_dongle_shipped_default),
        ("full-range stereo", _full_range_stereo),
    ],
)
def test_a_non_roleful_topology_has_ring_b_and_no_active_ring(name, factory):
    topo = factory()
    assert ring_channels_for_topology(topo) == 2
    assert active_ring_channels_for_topology(topo) is None, name


@pytest.mark.parametrize(
    "name,factory",
    [
        ("dual-apple composite", _dual_apple_stereo),
        ("subwoofer (single output, below the ring floor)", _subwoofer_topology),
    ],
)
def test_neither_ring_exists_for_composite_or_sub_only_topologies(name, factory):
    topo = factory()
    assert ring_channels_for_topology(topo) is None, name
    assert active_ring_channels_for_topology(topo) is None, name


def test_topology_supports_shm_ring_stays_false_for_roleful():
    """THE FORBIDDEN ONE-LINER, named so a future reader does not "fix" it.

    Making ``topology_supports_shm_ring`` return True for a roleful topology is
    the obvious-looking way to give those boxes a ring. It is forbidden, and the
    damage is not in this function — it is in its two other consumers:

    - the unattended ``--auto`` default pass would find every remaining gate
      passing on a roleful box and ARM IT with no operator present; the marker
      would be absent, so outputd would refuse the pairing and park a silent
      speaker (C-B2);
    - ``jasper.sound.camilla_yaml``'s flat-cutover defusal gate protects exactly
      the boxes the widening would re-expose to a full-range stereo graph on a
      compression driver.

    The active ring is admitted by ``ring_topology_ready``'s own arm instead,
    where the endpoint proof is in scope.
    """
    for layout, mode, _width in ROLEFUL_CASES:
        topo = _active_topology(layout, mode)
        assert topology_supports_shm_ring(topo) is False
        # ...even though an active ring genuinely exists for it.
        assert active_ring_channels_for_topology(topo) is not None


def test_the_resolved_wire_carries_the_active_width_as_its_own_field():
    from jasper.fanin_coupling import resolve_ring_wire

    roleful = resolve_ring_wire(_active_topology("stereo", "active_3_way"))
    assert roleful.ring_active_channels == 6
    # Ring A and Ring B are UNMOVED — the whole point of the fifth field.
    assert roleful.ring_a_channels == 2
    assert roleful.ring_b_channels == 2

    passive = resolve_ring_wire(_full_range_stereo())
    assert passive.ring_active_channels is None


# --------------------------------------------------------------------------
# 4. The endpoint accept-set (and the dangerous near-miss).
# --------------------------------------------------------------------------


def _endpoint_graph(device: str, channels: int = 2) -> GraphSafety:
    return GraphSafety(
        classification=GRAPH_APPROVED_ACTIVE_RUNTIME,
        allowed=True,
        playback_device=device,
        playback_channels=channels,
    )


@pytest.mark.parametrize(
    "device", [OUTPUTD_ACTIVE_PLAYBACK_DEVICE, RING_ACTIVE_PLAYBACK_DEVICE]
)
def test_both_transports_of_the_active_lane_are_accepted_endpoints(device):
    width, problem, accepted = _outputd_endpoint_width(_endpoint_graph(device), 8)
    assert (width, problem, accepted) == (2, None, device)


def test_the_stereo_ring_is_rejected_as_an_outputd_endpoint():
    """The rejection uses the DANGEROUS NEAR-MISS, not an arbitrary string.

    An arbitrary device (``"nonsense"``) would be rejected by an accept-set that
    WRONGLY included the stereo ring, so it proves nothing about the boundary
    that matters. ``jts_ring_playback`` is the one wrong device a real box could
    plausibly be pointed at, and it carries a full-range stereo program — so
    accepting it as an active endpoint would mean outputd opening a per-driver
    DAC lane against full-range audio.
    """
    width, problem, accepted = _outputd_endpoint_width(
        _endpoint_graph(RING_PLAYBACK_DEVICE), 8
    )
    assert width is None
    assert problem == "active_outputd_lane_missing"
    assert accepted is None
    assert RING_PLAYBACK_DEVICE not in OUTPUTD_LEGAL_ENDPOINT_DEVICES


def test_the_accepted_device_rides_the_decision_so_the_marker_derives_from_it():
    """One classification, two answers — never two classifications.

    The reconciler writes the width AND the endpoint marker. Deriving the marker
    from a SECOND read of the graph is how the two would come to describe
    different graphs (a re-emit landing between the reads is enough). The
    decision therefore reports which endpoint it accepted.
    """
    from jasper.active_speaker.runtime_contract import OutputdActiveLaneDecision

    decision = OutputdActiveLaneDecision(
        ok=True, width=2, reason="x", endpoint_device=RING_ACTIVE_PLAYBACK_DEVICE
    )
    assert decision.to_dict()["endpoint_device"] == RING_ACTIVE_PLAYBACK_DEVICE
    # And an unsuccessful decision names no endpoint.
    assert OutputdActiveLaneDecision(ok=False, width=None, reason="x").endpoint_device is None


# --------------------------------------------------------------------------
# 5. The WRITER side — one helper, one decision (safety-B1). Walking.
# --------------------------------------------------------------------------


def test_every_active_lane_write_site_writes_the_pair():
    """Walk EVERY write of the lane key and fail on one that skips its pair.

    ``JASPER_OUTPUTD_ACTIVE_LANE`` and ``JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT``
    are ONE FACT with two consumers. outputd bails at startup on the incoherent
    pair, because that combination can only mean this writer is broken — so a
    site that states one without the other does not produce a subtly wrong box,
    it produces a parked one.

    Enumerating the sites rather than asserting "the helper exists" is what makes
    this a guard: a future branch that reaches for ``set_env_file_var_if_changed``
    directly is exactly the regression, and it would pass any test that only
    checked the helper's own body.
    """
    text = HARDWARE_RECONCILE.read_text(encoding="utf-8")
    # The helper's OWN body is the one legitimate direct writer; everything
    # outside it must route through the helper.
    before, _, rest = text.partition("set_outputd_active_lane_pair() {")
    assert rest, "the pair helper is gone — every write site is now unguarded"
    helper, _, after = rest.partition("\n}\n")
    outside = before + after

    lane_writes = [
        line.strip()
        for line in outside.splitlines()
        if "JASPER_OUTPUTD_ACTIVE_LANE" in line
        and "set_env_file_var_if_changed" in line
    ]
    assert lane_writes == [], (
        "these lines write JASPER_OUTPUTD_ACTIVE_LANE directly instead of "
        f"through set_outputd_active_lane_pair: {lane_writes}"
    )
    pair_calls = [
        line.strip()
        for line in outside.splitlines()
        if re.match(r"^\s*set_outputd_active_lane_pair\b", line)
    ]
    # Four branches state the lane: composite, active, non-active, parked.
    assert len(pair_calls) == 4, pair_calls
    # The helper writes BOTH keys, and it is the ONLY writer of the marker.
    assert "JASPER_OUTPUTD_ACTIVE_LANE" in helper
    assert OUTPUTD_RING_ACTIVE_ENDPOINT_ENV_VAR in helper
    outside_code = [
        line
        for line in outside.splitlines()
        if OUTPUTD_RING_ACTIVE_ENDPOINT_ENV_VAR in line
        and not line.lstrip().startswith("#")
    ]
    assert outside_code == [], (
        "the endpoint marker is WRITTEN outside the pair helper (prose "
        f"references are fine): {outside_code}"
    )


def test_the_marker_is_set_by_positive_equality_never_by_negation():
    """A negative test would INVERT into a spurious arm on an unknown device.

    ``lane == 1 && device == <active ring>`` fails closed for anything it does
    not recognize. ``device != <alsa lane>`` — the tempting shorthand — would set
    the marker for an empty device, a typo, or any future endpoint.
    """
    text = HARDWARE_RECONCILE.read_text(encoding="utf-8")
    helper = text.split("set_outputd_active_lane_pair() {", 1)[1].split("\n}\n", 1)[0]
    assert '== "$RING_ACTIVE_OUTPUTD_PLAYBACK_DEVICE"' in helper
    code = [
        line for line in helper.splitlines() if not line.lstrip().startswith("#")
    ]
    assert not any("!=" in line for line in code), code


# --------------------------------------------------------------------------
# 6. The transport SHAPE (E4).
# --------------------------------------------------------------------------


def test_the_active_shape_is_selected_by_the_marker_not_by_the_observed_device():
    """E4's discriminator, pinned where it would be tempting to use the device.

    Selecting the shape from ``camilla_playback_device`` would make
    ``transport_coherence_errors``' playback comparison VACUOUS: it would derive
    the expectation from the value it is checking, so a graph pointed at the
    wrong ring would define itself correct. The marker is an independent fact
    written by a different owner, which is what gives the comparison something to
    disagree with.
    """
    from jasper.audio_runtime_plan import (
        TRANSPORT_SHM_RING,
        TRANSPORT_SHM_RING_ACTIVE,
        transport_topology_for_coupling,
    )

    stereo = transport_topology_for_coupling("shm_ring", outputd_env={})
    assert stereo.name == TRANSPORT_SHM_RING
    assert stereo.camilla_to_outputd["camilla_playback_device"] == RING_PLAYBACK_DEVICE

    # The marker flips the shape — while the observed device is passed as the
    # WRONG one, proving the device is not what decides.
    active = transport_topology_for_coupling(
        "shm_ring",
        outputd_env={OUTPUTD_RING_ACTIVE_ENDPOINT_ENV_VAR: "1"},
        camilla_playback_device=RING_PLAYBACK_DEVICE,
    )
    assert active.name == TRANSPORT_SHM_RING_ACTIVE
    assert (
        active.camilla_to_outputd["camilla_playback_device"]
        == RING_ACTIVE_PLAYBACK_DEVICE
    )


def test_every_declared_transport_shape_is_reachable_and_vice_versa():
    """Exhaustiveness, both directions — a shape nobody produces is a dead arm.

    E4 chose NAMED shapes over threading a device value precisely so consumers
    can match exhaustively and fail loud on one nobody handled. That argument is
    only worth anything if the declared set and the produced set are the same
    set: a declared-but-unreachable shape is a branch no test can ever cover,
    and a reachable-but-undeclared one is the unhandled case the design claims
    cannot exist.
    """
    from jasper.audio_runtime_plan import (
        TRANSPORT_SHAPES,
        transport_topology_for_coupling,
    )

    produced = {
        transport_topology_for_coupling("loopback").name,
        transport_topology_for_coupling("shm_ring", outputd_env={}).name,
        transport_topology_for_coupling(
            "shm_ring",
            outputd_env={OUTPUTD_RING_ACTIVE_ENDPOINT_ENV_VAR: "1"},
        ).name,
    }
    assert produced == set(TRANSPORT_SHAPES)


def test_a_crossed_ring_path_is_a_coherence_error_before_the_daemon_bails():
    from jasper.audio_runtime_plan import transport_coherence_errors

    errors = transport_coherence_errors(
        coupling="shm_ring",
        outputd_env={
            OUTPUTD_RING_ACTIVE_ENDPOINT_ENV_VAR: "1",
            "JASPER_OUTPUTD_CONTENT_BRIDGE": "shm_ring",
            "JASPER_OUTPUTD_SHM_RING_PATH": "/dev/shm/jts-ring/content.ring",
        },
    )
    assert any("may read only" in e for e in errors), errors


def test_a_ring_device_under_a_loopback_plan_is_reported_not_ignored():
    """Both rings have NO outputd capture pairing, and the absence is meaningful.

    outputd reads a ring FILE, so no ring PCM has a paired snd-aloop capture. A
    Camilla graph naming one while the plan says loopback is a half-flipped box:
    the graph writes a ring nobody reads. Before the membership fix this fell
    through to silence.
    """
    from jasper.audio_runtime_plan import transport_coherence_errors

    for device in (RING_PLAYBACK_DEVICE, RING_ACTIVE_PLAYBACK_DEVICE):
        errors = transport_coherence_errors(
            coupling="loopback",
            outputd_env={},
            camilla_devices={"playback_device": device},
        )
        assert any("no registered outputd capture" in e for e in errors), (
            f"{device}: {errors}"
        )


def test_the_active_ring_is_a_recognized_output_endpoint():
    """An unrecognized endpoint degrades every consumer to "coherence unknown".

    That is the D5 permanent-red-line shape: a healthy box looking unverifiable
    forever rather than failing loudly once.
    """
    from jasper.audio_runtime_plan import output_endpoint_evidence_from_statefiles

    src = Path(output_endpoint_evidence_from_statefiles.__code__.co_filename)
    assert src.exists()
    # Behavioural: the recognized-endpoint set is what gates
    # endpoint_recognized, so assert membership through the module's own name.
    from jasper import audio_runtime_plan

    text = Path(audio_runtime_plan.__file__).read_text(encoding="utf-8")
    block = text.split("output_endpoints = {", 1)[1].split("}", 1)[0]
    assert "RING_ACTIVE_PLAYBACK_DEVICE" in block
    assert "RING_PLAYBACK_DEVICE" in block


# --------------------------------------------------------------------------
# 7. Arming stays EXPLICIT (C-B2 / R-SF4 / E2).
# --------------------------------------------------------------------------


def test_the_unattended_pass_has_its_own_roleful_exclusion():
    """Independent of every eligibility predicate, on purpose.

    ``ring_topology_ready`` now has an arm that ADMITS a roleful topology, so the
    auto pass can no longer rely on "roleful boxes fail the topology gate". This
    gate asks only whether the box is roleful and refuses if it is — so a future
    change to any eligibility predicate cannot re-open unattended arming of a
    crossover speaker.
    """
    from jasper.fanin.coupling_reconcile import default_ring_gates

    names = [name for name, _gate in default_ring_gates()]
    assert "ring_not_roleful" in names
    # ...and it runs BEFORE the topology gate, so its coarser refusal is the one
    # an operator of a crossover box reads.
    assert names.index("ring_not_roleful") < names.index("ring_topology")


def test_the_roleful_exclusion_refuses_a_roleful_box_and_permits_a_passive_one(
    monkeypatch,
):
    from jasper.fanin import coupling_reconcile

    monkeypatch.setattr(
        "jasper.output_topology.load_output_topology_strict",
        lambda *a, **k: _active_topology("mono", "active_2_way"),
    )
    ok, detail = coupling_reconcile.ring_not_roleful_ready()
    assert ok is False
    assert "explicit" in detail

    monkeypatch.setattr(
        "jasper.output_topology.load_output_topology_strict",
        lambda *a, **k: _full_range_stereo(),
    )
    ok, _detail = coupling_reconcile.ring_not_roleful_ready()
    assert ok is True


def test_the_roleful_exclusion_fails_closed_on_an_unreadable_topology(monkeypatch):
    from jasper.fanin import coupling_reconcile
    from jasper.output_topology import OutputTopologyError

    def _boom(*a, **k):
        raise OutputTopologyError("corrupt")

    monkeypatch.setattr("jasper.output_topology.load_output_topology_strict", _boom)
    ok, detail = coupling_reconcile.ring_not_roleful_ready()
    assert ok is False
    assert "fail-closed" in detail


def test_a_roleful_topology_is_admitted_only_with_a_staged_endpoint(monkeypatch):
    """E2: the width alone is not enough — the ENDPOINT must be staged.

    A roleful box that resolves an active width but has no marker (or a conf.d
    block still on the shipped default) would arm into a daemon that refuses the
    pairing. Both halves are checked separately because they have different
    remedies.
    """
    from jasper.fanin import coupling_reconcile

    monkeypatch.setattr(
        "jasper.output_topology.load_output_topology_strict",
        lambda *a, **k: _active_topology("stereo", "active_3_way"),
    )

    # No marker -> refused, naming the reconciler.
    monkeypatch.setattr(
        "jasper.fanin_coupling.ring_active_endpoint_armed", lambda env=None: False
    )
    ok, detail = coupling_reconcile.ring_topology_ready(strict_unreadable=True)
    assert ok is False
    assert "marker" in detail

    # Marker present but the conf.d block still declares the shipped default
    # while this box needs 6 -> refused, naming the conf.d.
    monkeypatch.setattr(
        "jasper.fanin_coupling.ring_active_endpoint_armed", lambda env=None: True
    )
    monkeypatch.setattr(
        "jasper.ring_assets.ring_conf_channels", lambda pcm, conf_d=None: 2
    )
    ok, detail = coupling_reconcile.ring_topology_ready(strict_unreadable=True)
    assert ok is False
    assert "channels=2" in detail

    # Both staged -> admitted.
    monkeypatch.setattr(
        "jasper.ring_assets.ring_conf_channels", lambda pcm, conf_d=None: 6
    )
    ok, detail = coupling_reconcile.ring_topology_ready(strict_unreadable=True)
    assert ok is True
    assert "ACTIVE-ring eligible" in detail


# --------------------------------------------------------------------------
# 8. The stale-file guard covers the third ring.
# --------------------------------------------------------------------------


def _point_all_ring_files_at(monkeypatch, tmp_path):
    """Repoint all THREE ring files into a tmpdir, against the shipped conf.d."""
    import jasper.ring_assets as ra

    paths = {
        "a": tmp_path / "program.ring",
        "b": tmp_path / "content.ring",
        "active": tmp_path / "active-content.ring",
    }
    monkeypatch.setattr(ra, "RING_A_PROGRAM_FILE", str(paths["a"]))
    monkeypatch.setattr(ra, "RING_B_CONTENT_FILE", str(paths["b"]))
    monkeypatch.setattr(ra, "RING_ACTIVE_CONTENT_FILE", str(paths["active"]))
    monkeypatch.setattr(ra, "RING_CONF_D", str(RING_CONF))
    return paths


def test_the_stale_active_ring_file_is_deleted_like_the_other_two(
    tmp_path, monkeypatch
):
    """BEHAVIOURAL, not a source scan — the active file must actually be cleared.

    A stale ``active-content.ring`` is a create-or-attach fault for the writer,
    exactly as a stale Ring A is. It is also the file most able to go stale on
    its own: a re-commission that moves a 2-way to a 3-way changes ONLY the
    active ring's width, so nothing else on the box looks different.

    Deliberately mutated on the CHANNELS axis, because that is the axis this rung
    added for this file and the one whose absence a format-only test could not
    see. The other two files are coherent here, so this also proves the guard
    does not simply delete everything.
    """
    import jasper.fanin.coupling_reconcile as cr
    import jasper.ring_assets as ra
    paths = _point_all_ring_files_at(monkeypatch, tmp_path)
    _ring_file(paths["a"], sample_format=ra.RING_SAMPLE_FORMAT_S16LE)
    _ring_file(paths["b"], sample_format=ra.RING_SAMPLE_FORMAT_S16LE)
    # The conf.d's active block declares the ioplug default (2); this file says 6.
    _ring_file(
        paths["active"], sample_format=ra.RING_SAMPLE_FORMAT_S16LE, channels=6
    )

    cr._delete_stale_ring_files("t", "")

    assert not paths["active"].exists(), (
        "a width-stale ACTIVE ring file must be deleted — the writer's "
        "create-or-attach would otherwise fail against it"
    )
    assert paths["a"].exists() and paths["b"].exists(), (
        "coherent ring files must be left alone"
    )


def test_the_confirm_predicate_escalates_on_a_stale_active_ring_file(
    tmp_path, monkeypatch
):
    """The two halves must mean the same thing by "coherent".

    The CONFIRM predicate decides whether to escalate to the arm that runs the
    delete above. If it did not consider the active file while the delete did, a
    stale active ring would read as coherent, the escalation would never fire,
    and the file the arm is ready to remove would sit there forever.
    """
    import jasper.fanin.coupling_reconcile as cr
    import jasper.ring_assets as ra
    paths = _point_all_ring_files_at(monkeypatch, tmp_path)
    _ring_file(paths["a"], sample_format=ra.RING_SAMPLE_FORMAT_S16LE)
    _ring_file(paths["b"], sample_format=ra.RING_SAMPLE_FORMAT_S16LE)

    # Positive control: all three coherent -> stay lightweight.
    _ring_file(paths["active"], sample_format=ra.RING_SAMPLE_FORMAT_S16LE)
    needed, detail = cr._ring_confirm_needs_self_heal("")
    assert needed is False, detail

    # Only the ACTIVE file drifts -> escalate.
    _ring_file(
        paths["active"], sample_format=ra.RING_SAMPLE_FORMAT_S16LE, channels=6
    )
    needed, detail = cr._ring_confirm_needs_self_heal("")
    assert needed is True
    assert "channels" in detail
    assert "stale-file self-heal" in detail


# --------------------------------------------------------------------------
# 9. INERTNESS — nothing moves until an operator arms.
# --------------------------------------------------------------------------


def test_the_shipped_conf_d_declares_the_active_block_at_the_ioplug_defaults():
    """The third block ships STATIC and inert.

    ``format``/``channels`` are omitted exactly as in the other two blocks, so
    the block declares the ioplug's own defaults — a complete wire that no box
    has to render. That is what lets ``render_ring_conf_wire`` only ever
    SUBSTITUTE inside an existing block, never append one to a file every ALSA
    client parses.
    """
    conf = RING_CONF.read_text(encoding="utf-8")
    body = conf.split(f"pcm.{RING_ACTIVE_PLAYBACK_DEVICE} {{", 1)[1].split("}", 1)[0]
    assert "format" not in body
    assert "channels" not in body
    assert "period_frames 128" in body
    assert "n_slots 2" in body
    assert ring_assets.ring_conf_channels(
        ring_assets.RING_ACTIVE_CONF_PCM, str(RING_CONF)
    ) == ring_assets.RING_CONF_DEFAULT_CHANNELS


def test_the_emitters_default_to_todays_literals_byte_for_byte():
    """Q6's inertness claim, proven by EMISSION IDENTITY rather than argued.

    ``queuelimit``/``enable_rate_adjust`` became parameters so an active-ring
    sink can take 1/false. Every other emit must be unchanged — so emit with the
    defaults and with the old literals passed explicitly, and require the bytes
    to be identical.
    """
    preset = _mono_two_way_preset()
    for emit in (
        active_camilla_yaml.emit_active_speaker_startup_config,
        active_camilla_yaml.emit_active_speaker_commissioning_config,
    ):
        default = emit(preset, playback_device=OUTPUTD_ACTIVE_PLAYBACK_DEVICE)
        explicit = emit(
            preset,
            playback_device=OUTPUTD_ACTIVE_PLAYBACK_DEVICE,
            queuelimit=4,
            enable_rate_adjust=True,
        )
        assert default == explicit, emit.__name__
        assert "  queuelimit: 4" in default
        assert "  enable_rate_adjust: true" in default


def test_the_ring_geometry_reaches_the_emitted_yaml():
    """...and the parameters actually carry the ring's values when asked."""
    preset = _mono_two_way_preset()
    yaml = active_camilla_yaml.emit_active_speaker_startup_config(
        preset,
        playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
        queuelimit=1,
        enable_rate_adjust=False,
    )
    assert "  queuelimit: 1" in yaml
    assert "  enable_rate_adjust: false" in yaml
    assert f'device: "{RING_ACTIVE_PLAYBACK_DEVICE}"' in yaml


def test_an_active_ring_emit_refuses_a_width_the_ring_cannot_carry(monkeypatch):
    """The emitter-side twin of the endpoint width bound.

    A width outside the ring's accept-set is not a config that fails later — the
    ioplug's attach compares the channel count field-by-field, so it CRASHES the
    ring instead of refusing it. Refusing at emit means the thing that caused the
    shear reports it.
    """
    from jasper.active_speaker.camilla_yaml import ActiveSpeakerConfigError

    with pytest.raises(ActiveSpeakerConfigError, match="active-ring playback"):
        active_camilla_yaml._assert_ring_playback_width(RING_ACTIVE_PLAYBACK_DEVICE, 9)
    with pytest.raises(ActiveSpeakerConfigError, match="active-ring playback"):
        active_camilla_yaml._assert_ring_playback_width(RING_ACTIVE_PLAYBACK_DEVICE, 1)
    # In range: silent.
    active_camilla_yaml._assert_ring_playback_width(RING_ACTIVE_PLAYBACK_DEVICE, 8)
    # A NON-ring device is never judged — this is a no-op on every box today.
    active_camilla_yaml._assert_ring_playback_width(OUTPUTD_ACTIVE_PLAYBACK_DEVICE, 99)


def test_the_flat_lane_is_refused_on_a_roleful_box_so_its_ring_kwargs_cannot_stomp():
    """Why ``capture_kwargs_for_coupling`` may keep answering STEREO-only.

    Those kwargs feed the FLAT program emitter and are merged LAST by
    ``apply_capture_precedence``, so anything they name overwrites what the
    caller resolved — handing a roleful box the full-range STEREO ring would be
    a genuine stomp. The reason it cannot happen is structural, not ordering:
    the flat program lane is refused outright on a roleful topology, because a
    full-range graph on a crossover box would reach a protected tweeter. This
    pins that refusal, since it is the load-bearing half of the argument.
    """
    from jasper.active_speaker.runtime_contract import (
        flat_program_graph_blocked_reason,
    )
    from jasper.fanin_coupling import capture_kwargs_for_coupling

    # The kwargs are unconditionally the STEREO ring's...
    kwargs = capture_kwargs_for_coupling("shm_ring")
    assert kwargs["playback_device"] == RING_PLAYBACK_DEVICE

    # ...and every topology that HAS an active ring refuses the lane they serve.
    for layout, mode, _width in ROLEFUL_CASES:
        topo = _active_topology(layout, mode)
        assert active_ring_channels_for_topology(topo) is not None
        assert flat_program_graph_blocked_reason(topo) is not None, (
            f"{layout}/{mode}: an active-ring box must refuse the flat program "
            "lane, or these stereo kwargs could reach its emit"
        )
    # A passive box legitimately runs that lane, so the refusal is not blanket.
    assert flat_program_graph_blocked_reason(_full_range_stereo()) is None


def test_resolve_output_layout_keeps_the_alsa_lane_until_the_marker_is_set(
    monkeypatch,
):
    """The single chooser, and its default is exactly today's answer."""
    from jasper.output_topology import resolve_output_layout

    topo = _active_topology("mono", "active_2_way")

    monkeypatch.setattr(
        "jasper.fanin_coupling.ring_active_endpoint_armed", lambda env=None: False
    )
    layout = resolve_output_layout(topo, env={})
    assert layout.playback_device == OUTPUTD_ACTIVE_PLAYBACK_DEVICE

    monkeypatch.setattr(
        "jasper.fanin_coupling.ring_active_endpoint_armed", lambda env=None: True
    )
    armed = resolve_output_layout(topo, env={})
    assert armed.playback_device == RING_ACTIVE_PLAYBACK_DEVICE
    # The SOURCE token is unchanged, which is why nothing keyed on it (the
    # baseline handoff-issue path) needed an edit.
    assert armed.playback_device_source == layout.playback_device_source
    assert armed.transport_channel_count == layout.transport_channel_count
