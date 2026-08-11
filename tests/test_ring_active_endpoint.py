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
from types import SimpleNamespace

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

    The device side is DERIVED from ``OUTPUTD_LEGAL_ENDPOINT_DEVICES`` — the
    registry that decides what outputd will accept — not restated as a local
    tuple. A local literal only walks the devices this test file happens to know
    about, so a poisoned or extended registry is invisible to it: mutation-
    proven, by adding the forbidden stereo ring to that registry and watching a
    literal-driven version stay green.
    """
    legitimate = tuple(sorted(OUTPUTD_LEGAL_ENDPOINT_DEVICES))
    assert legitimate, "the legal-endpoint registry is empty"
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

    outputd reads a ring FILE, so no ring PCM has a paired snd-aloop capture, and
    a Camilla graph naming one while the plan says loopback writes a ring nobody
    reads. Before the membership fix this fell through to silence.

    The two rings get OPPOSITE dispositions from that same absence, and the split
    is the whole point of this test:

    - ``jts_ring_playback`` (stereo) stays a hard ERROR. No ladder creates that
      pairing — it is a forbidden token for every active emitter — so a graph
      naming it is a half-flipped box with no documented next step.
    - ``jts_ring_active_playback`` is a NOTE. It is the mid-arm waypoint the
      documented ladder's step 1 creates on purpose; reporting it as an error
      deadlocked the ladder on jts3 (2026-08-11, exit 78).
    """
    from jasper.audio_runtime_plan import (
        transport_coherence_errors,
        transport_coherence_report,
    )

    stereo = transport_coherence_report(
        coupling="loopback",
        outputd_env={},
        camilla_devices={"playback_device": RING_PLAYBACK_DEVICE},
    )
    assert any("no registered outputd capture" in e for e in stereo.errors), stereo
    assert stereo.notes == (), stereo
    # The thin accessor carries the SAME verdict — a caller that only
    # refuses-or-proceeds must not see the stereo ring soften.
    assert any(
        "no registered outputd capture" in e
        for e in transport_coherence_errors(
            coupling="loopback",
            outputd_env={},
            camilla_devices={"playback_device": RING_PLAYBACK_DEVICE},
        )
    )

    active = transport_coherence_report(
        coupling="loopback",
        outputd_env={},
        camilla_devices={"playback_device": RING_ACTIVE_PLAYBACK_DEVICE},
    )
    assert active.errors == (), active
    assert len(active.notes) == 1, active
    note = active.notes[0]
    assert RING_ACTIVE_PLAYBACK_DEVICE in note, note
    # The note names the state AND both exits, because a box can be LEFT here.
    #
    # The silence claim is deliberately CONDITIONAL. This layer's evidence is the
    # graph on DISK (the statefile), never the graph CamillaDSP has loaded, and
    # neither ladder rung reloads Camilla — so a box at the waypoint is usually
    # still playing, and goes silent at the next load. Asserting a bare "this box
    # is SILENT" here would pin an overclaim into doctor's WARN detail.
    assert "on disk" in note, note
    assert "goes silent at the next CamillaDSP load" in note, note
    assert "is SILENT" not in note, note
    assert "jasper-audio-hardware-reconcile" in note, note
    assert "jasper-fanin-coupling-reconcile shm_ring" in note, note
    assert "baseline-reemit --endpoint aloop" in note, note


def test_the_active_ring_is_a_recognized_output_endpoint():
    """An unrecognized endpoint degrades every consumer to "coherence unknown".

    That is the D5 permanent-red-line shape: a healthy box looking unverifiable
    forever rather than failing loudly once.
    """
    from jasper.audio_runtime_plan import output_endpoint_evidence_from_statefiles

    def _statefile(tmp, config_path):
        tmp.write_text(f"config_path: {config_path}\n", encoding="utf-8")
        return tmp

    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        graph = root / "active.yml"
        graph.write_text(
            "devices:\n"
            "  playback:\n"
            "    type: Alsa\n"
            f'    device: "{RING_ACTIVE_PLAYBACK_DEVICE}"\n'
            "    channels: 2\n",
            encoding="utf-8",
        )
        evidence = output_endpoint_evidence_from_statefiles(
            _statefile(root / "statefile.yml", graph)
        )
        # BEHAVIOURAL: drive the real reader with a real active-ring statefile
        # and require it to RECOGNIZE the endpoint. The previous version of this
        # test read this module's source and grepped the set literal for a
        # constant NAME — which passes unchanged if the set is never consulted,
        # and cannot see the D5 shape it claims to pin. Mutation-checked: with
        # the RING_ACTIVE_PLAYBACK_DEVICE entry removed from output_endpoints,
        # this assertion fails.
        assert evidence.endpoint_recognized is True, evidence
        assert evidence.devices["playback_device"] == RING_ACTIVE_PLAYBACK_DEVICE


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


def test_a_fourth_ring_block_must_render_or_fail_loud(monkeypatch, tmp_path):
    """The renderer WALKS ``RING_CONF_PCMS`` — proven by adding a fourth entry.

    The per-block channels loop used to be its own literal tuple beside that
    constant, so the two were free to disagree: a fourth ring added to
    RING_CONF_PCMS would ship a conf.d block the renderer silently never touched,
    and the ioplug would attach it at whatever the shipped default said. Neither
    outcome is acceptable, so the loop is driven by the constant and an entry
    with no declared width raises.
    """
    from jasper import ring_assets as ra
    from jasper.fanin_coupling import RingWire

    conf = tmp_path / "60-jts-ring.conf"
    conf.write_text(RING_CONF.read_text(encoding="utf-8"), encoding="utf-8")
    wire = RingWire(
        period_frames=ra.RING_SLOT_FRAMES,
        sample_format="S16_LE",
        ring_a_channels=2,
        ring_b_channels=2,
        ring_active_channels=None,
    )
    # Control: the shipped three render (here, a no-op — every value already
    # matches), so the failure below is the fourth entry and not the fixture.
    assert ra.render_ring_conf_wire(wire, conf_d=str(conf)).changed is False

    monkeypatch.setattr(
        ra, "RING_CONF_PCMS", (*ra.RING_CONF_PCMS, "jts_ring_fourth_playback")
    )
    with pytest.raises(ValueError, match="jts_ring_fourth_playback"):
        ra.render_ring_conf_wire(wire, conf_d=str(conf))


def test_the_ring_doctor_checks_are_still_registered():
    """The checks this rung edits must remain REGISTERED doctor checks.

    Caught for real during this fix round: a plain helper added immediately
    above ``check_fanin_coupling`` landed BETWEEN its ``@doctor_check``
    decorator and the function, so the decorator registered the helper and the
    coupling check silently left the doctor. Every direct-call test still
    passed, because they call the function, not the registry. This asserts the
    registry.
    """
    from jasper.cli.doctor import audio_runtime
    from jasper.cli.doctor._registry import registered_checks

    # Importing the module is what runs its @doctor_check decorators; naming the
    # functions below is also what makes that import USED, so this needs no lint
    # suppression (the repo's suppression ceiling is a real gate, not a
    # formality — and it counts the marker's TEXT, so even quoting one here
    # would spend a slot).
    expected = (
        audio_runtime.check_fanin_coupling,
        audio_runtime.check_ring_conf_floor_render,
        audio_runtime.check_ring_platform_assets,
    )
    registered = {entry.func.__name__ for entry in registered_checks()}
    for func in expected:
        assert func.__name__ in registered, (
            f"{func.__name__} is no longer a registered doctor check"
        )
    # ...and the private helper must NOT have been swept in.
    assert audio_runtime._requires_roleful_graph.__name__ not in registered


def test_the_floor_render_ok_names_the_roleful_reason_a_box_cannot_ring(monkeypatch):
    """An ``ok`` that means "this box still will not ring" has to SAY WHY.

    The check reads the DAC floor against the conf.d. On any box whose DAC
    declares a matching floor that pair reads green — and a ROLEFUL box still
    does not ring, because the active ring is explicit-arm-only. Reporting only
    "period_frames matches" there answers a question nobody asked and leaves the
    real one ("why is this box on loopback?") unanswered, which is the same
    defect #2294 fixed for the floor half.
    """
    from jasper.cli.doctor import audio_runtime

    monkeypatch.setattr(audio_runtime, "_active_audio_dac_id", lambda: "test_dac")
    monkeypatch.setattr(audio_runtime, "latency_floor_for", lambda dac_id: None)

    monkeypatch.setattr(audio_runtime, "_requires_roleful_graph", lambda: True)
    roleful = audio_runtime.check_ring_conf_floor_render()
    assert roleful.status == "ok"
    assert "ROLEFUL" in roleful.detail
    assert "baseline-reemit --endpoint ring" in roleful.detail

    # A PASSIVE box gets exactly today's sentence — the note is additive, not a
    # rewrite, so the existing #2294 answer is untouched where it was right.
    monkeypatch.setattr(audio_runtime, "_requires_roleful_graph", lambda: False)
    passive = audio_runtime.check_ring_conf_floor_render()
    assert passive.status == "ok"
    assert "ROLEFUL" not in passive.detail
    assert roleful.detail.startswith(passive.detail.rstrip())


def test_the_matching_floor_ok_still_names_the_roleful_reason(monkeypatch, tmp_path):
    """jts3's ACTUAL post-R7a case: the floor MATCHES and the box still cannot ring.

    Now that the DAC8X declares a 128-frame floor, jts3 reaches this check's
    happy path — "period_frames matches" — while remaining unable to ring, for
    the one reason the floor says nothing about. This is the branch the ruling
    was really about, so it is pinned separately from the no-floor one.
    """
    from jasper.audio_hardware.dac import latency_floor_for
    from jasper.cli.doctor import audio_runtime
    from jasper.fanin_coupling import RING_SLOT_FRAMES

    floor = latency_floor_for("hifiberry_dac8x")
    assert floor is not None and floor.outputd_period_frames == RING_SLOT_FRAMES, (
        "this test exists for the MATCHING-floor branch; the DAC8X floor moved"
    )
    conf = tmp_path / "60-jts-ring.conf"
    conf.write_text(RING_CONF.read_text(encoding="utf-8"), encoding="utf-8")

    monkeypatch.setattr(audio_runtime, "_active_audio_dac_id", lambda: "hifiberry_dac8x")
    monkeypatch.setattr(audio_runtime, "_JTS_RING_CONF_D", str(conf))

    monkeypatch.setattr(audio_runtime, "_requires_roleful_graph", lambda: True)
    roleful = audio_runtime.check_ring_conf_floor_render()
    assert roleful.status == "ok"
    assert "matches" in roleful.detail
    assert "ROLEFUL" in roleful.detail, (
        "a matching floor on a roleful box reads green while the box still "
        "cannot ring — the ok must say why"
    )

    monkeypatch.setattr(audio_runtime, "_requires_roleful_graph", lambda: False)
    passive = audio_runtime.check_ring_conf_floor_render()
    assert passive.status == "ok"
    assert "ROLEFUL" not in passive.detail


def test_the_coupling_warn_names_the_recovery_ladder_and_never_the_forbidden_ring(
    monkeypatch,
):
    """Safety N1: a roleful box with a CLEARED marker must not be told to expect
    the stereo ring — its emitters refuse that device by name — and the warn must
    name the command that finishes the arm.

    Severity stays ``warn`` deliberately: under the arm ladder this is a
    mid-procedure transient (graph moved, marker not yet re-derived), not a
    landing state.
    """
    from jasper.cli.doctor import audio_runtime
    from jasper.fanin_coupling import COUPLING_SHM_RING, OUTPUTD_CONTENT_BRIDGE_SHM_RING

    monkeypatch.setattr(audio_runtime, "_requires_roleful_graph", lambda: True)
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.read_persisted_coupling",
        lambda *a, **k: COUPLING_SHM_RING,
    )
    monkeypatch.setattr(
        "jasper.fanin_coupling.resolve_outputd_content_bridge",
        lambda raw: OUTPUTD_CONTENT_BRIDGE_SHM_RING,
    )
    monkeypatch.setattr(
        "jasper.fanin_coupling.ring_active_endpoint_armed", lambda env=None: False
    )
    monkeypatch.setattr(
        audio_runtime,
        "_active_camilla_config_path",
        lambda *a, **k: ("/tmp/statefile.yml", "/tmp/loaded.yml"),
    )
    monkeypatch.setattr(audio_runtime, "_loaded_capture_type", lambda path: "Alsa")
    monkeypatch.setattr(
        audio_runtime,
        "_loaded_device_field",
        lambda path, lane, field: (
            RING_ACTIVE_PLAYBACK_DEVICE if lane == "playback" else "jts_ring_capture"
        ),
    )
    result = audio_runtime.check_fanin_coupling()
    assert result.status == "warn", result.detail
    assert "no ring is expected here at all" in result.detail
    assert f"(expected {RING_PLAYBACK_DEVICE})" not in result.detail
    assert "baseline-reemit --endpoint ring" in result.detail

    # A PASSIVE box keeps the plain expectation and the plain remedy — the
    # honest phrasing is scoped to the case where the stereo ring is forbidden.
    monkeypatch.setattr(audio_runtime, "_requires_roleful_graph", lambda: False)
    passive = audio_runtime.check_fanin_coupling()
    assert passive.status == "warn", passive.detail
    assert f"(expected {RING_PLAYBACK_DEVICE})" in passive.detail
    assert "baseline-reemit" not in passive.detail


def _emitter_required_kwargs(emit):
    """The non-default arguments each emitter needs beyond preset+playback_device.

    Derived from the signature rather than hard-coded per emitter, so a new
    required argument surfaces as a KeyError here instead of silently dropping
    that emitter out of the byte-identity loop.
    """
    import inspect

    extras = {
        "role_channels": {"woofer": 0, "tweeter": 1},
        "program_channel": "mono",
        "corrections": {
            "woofer": {"gain_db": 0.0, "delay_ms": 0.0},
            "tweeter": {"gain_db": 0.0, "delay_ms": 0.0},
        },
    }
    required = [
        p.name
        for p in inspect.signature(emit).parameters.values()
        if p.default is inspect.Parameter.empty
        and p.name not in ("preset", "playback_device")
    ]
    return {name: extras[name] for name in required}


def test_the_emitters_default_to_todays_literals_byte_for_byte():
    """Q6's inertness claim, proven by EMISSION IDENTITY rather than argued.

    ``queuelimit``/``enable_rate_adjust`` became parameters so an active-ring
    sink can take 1/false. Every other emit must be unchanged — so emit with the
    defaults and with the old literals passed explicitly, and require the bytes
    to be identical.

    ALL FIVE parameterized emitters, not a sample. Two of them were looped here
    and the other three were verified by hand once; a spot-check is not a
    standing guard, and the whole point of the claim is that the set is
    complete. The sixth (parked) emitter is excluded because it never took the
    parameters — pinned separately below.
    """
    preset = _mono_two_way_preset()
    emitters = (
        active_camilla_yaml.emit_active_speaker_startup_config,
        active_camilla_yaml.emit_active_speaker_commissioning_config,
        active_camilla_yaml.emit_active_speaker_program_config,
        active_camilla_yaml.emit_active_speaker_baseline_config,
        active_camilla_yaml.emit_active_speaker_driver_domain_config,
    )
    for emit in emitters:
        kwargs = _emitter_required_kwargs(emit)
        default = emit(
            preset, playback_device=OUTPUTD_ACTIVE_PLAYBACK_DEVICE, **kwargs
        )
        explicit = emit(
            preset,
            playback_device=OUTPUTD_ACTIVE_PLAYBACK_DEVICE,
            queuelimit=4,
            enable_rate_adjust=True,
            **kwargs,
        )
        assert default == explicit, emit.__name__
        assert "  queuelimit: 4" in default, emit.__name__
        assert "  enable_rate_adjust: true" in default, emit.__name__

    # The count is the claim: every emitter that TAKES the pair is in the loop.
    import inspect

    takes_the_pair = [
        name
        for name, fn in vars(active_camilla_yaml).items()
        if name.startswith("emit_active_speaker_")
        and callable(fn)
        and "queuelimit" in inspect.signature(fn).parameters
    ]
    assert len(takes_the_pair) == len(emitters), (
        f"{sorted(takes_the_pair)} take queuelimit but only {len(emitters)} are "
        "byte-identity checked"
    )


def test_the_parked_emitter_keeps_its_own_literals():
    """The deliberate exclusion, pinned so it reads as a choice, not an omission."""
    import inspect

    parked = active_camilla_yaml.emit_active_speaker_parked_config
    assert "queuelimit" not in inspect.signature(parked).parameters
    source = inspect.getsource(parked)
    assert "queuelimit: 4" in source
    assert "enable_rate_adjust: false" in source


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


def test_the_width_refusal_actually_fires_through_an_emitter(monkeypatch):
    """...and it is WIRED IN, not merely correct in isolation.

    The assertion above exercises the helper directly, which proves the rule and
    nothing about whether any emitter calls it — neutering the helper's body
    left the whole suite green. This drives a REAL emit through
    ``emit_active_speaker_startup_config`` (one of the five call sites) and
    requires the emitter to refuse.

    The emitter derives ``output_count`` from the preset, and no legal preset
    lands outside 2..8, so the ring's own upper bound is narrowed for the
    duration instead. Everything on the emit path stays real — real preset, real
    derived width, real guard — and only the accept-set boundary moves, which is
    the one value a fixture cannot otherwise reach.

    The other four call sites are held by the source walk below: one live emit
    proves the wiring exists, the walk proves none of the five lost it.
    """
    import re as _re

    from jasper.active_speaker.camilla_yaml import ActiveSpeakerConfigError

    preset = _mono_two_way_preset()  # 2 outputs (woofer + tweeter)
    monkeypatch.setattr(
        "jasper.active_speaker.runtime_contract.MAX_RING_CHANNELS", 1
    )
    with pytest.raises(ActiveSpeakerConfigError, match="active-ring playback"):
        active_camilla_yaml.emit_active_speaker_startup_config(
            preset,
            playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
        )
    # The SAME emit onto the ALSA lane is untouched by the guard — so the
    # refusal above is the ring rule firing, not a generic width complaint.
    active_camilla_yaml.emit_active_speaker_startup_config(
        preset,
        playback_device=OUTPUTD_ACTIVE_PLAYBACK_DEVICE,
    )
    # ...and with the real bound restored, the ring emit succeeds — so the
    # refusal is the bound, not the device.
    monkeypatch.undo()
    active_camilla_yaml.emit_active_speaker_startup_config(
        preset,
        playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
    )

    # All five emitters still call it. A count, because the number is the claim:
    # every active emitter that can name a ring must ask.
    source = Path(active_camilla_yaml.__file__).read_text(encoding="utf-8")
    call_sites = _re.findall(r"^\s+_assert_ring_playback_width\(", source, _re.M)
    assert len(call_sites) == 5, (
        f"expected 5 emitter call sites of _assert_ring_playback_width, found "
        f"{len(call_sites)} — a new active emitter must ask, or an existing one "
        "stopped asking"
    )


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


# --------------------------------------------------------------------------
# 11. THE ARM SEQUENCE, END TO END (R7b panel round 2, B1 + B2).
#
# The panel's finding was not that a step was wrong — it was that the sequence
# did not EXIST. The marker derives from the graph and the graph's device
# derived from the marker: a fixed point that held in BOTH directions, so a box
# could neither arm nor release, and every test pinned a state no writer
# produced. These walk the REAL functions, in order, from the states a box is
# actually in.
#
#   ARM:      baseline-reemit --endpoint ring
#          -> jasper-audio-hardware-reconcile   (marker derives 1)
#          -> jasper-fanin-coupling-reconcile shm_ring
#   ROLLBACK: baseline-reemit --endpoint aloop
#          -> jasper-audio-hardware-reconcile   (marker derives 0)
#          -> jasper-fanin-coupling-reconcile loopback
# --------------------------------------------------------------------------


def _emit_active_baseline(preset, device):
    """Emit a roleful baseline graph named at ``device``, as production would."""
    from jasper.active_speaker.camilla_yaml import (
        active_sink_queue_params,
        emit_active_speaker_baseline_config,
    )

    queuelimit, rate_adjust = active_sink_queue_params(device)
    return emit_active_speaker_baseline_config(
        preset,
        playback_device=device,
        queuelimit=queuelimit,
        enable_rate_adjust=rate_adjust,
        corrections={
            "woofer": {"gain_db": 0.0, "delay_ms": 0.0},
            "tweeter": {"gain_db": 0.0, "delay_ms": 0.0},
        },
        baseline_id="arm-walk",
    )


def _derived_marker(graph_yaml, topology, *, cap=8):
    """Run the REAL marker derivation: graph bytes -> classification -> device.

    This is the chain ``jasper-audio-hardware-reconcile`` runs — it classifies
    the graph the statefile points at and turns the ACCEPTED endpoint device
    into ``JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT``. Returning the device (not a
    bool) keeps the shell's positive-equality mapping visible to the caller,
    which is the half that must never become a negation.

    ``evidence_source="desired"`` is the in-memory twin of the reconciler's
    ``persisted_boot`` read: same classifier, same authority checks, sourced
    from bytes instead of from the statefile — which is what lets this walk the
    sequence without staging a live box. It is also the exact call
    ``baseline-reemit`` re-proves with, so the graph this test derives a marker
    from is the graph that command would have agreed to write.
    """
    from jasper.active_speaker.runtime_contract import classify_bass_extension_graph

    graph = classify_bass_extension_graph(
        topology,
        evidence_source="desired",
        graph_text=graph_yaml,
        applied_baseline_state={},
        desired_profile=None,
    )
    _width, _problem, device = _outputd_endpoint_width(graph, cap)
    return device


def _outputd_env(*, marker: str | None, ring_path: str | None = None) -> str:
    lines = ["JASPER_OUTPUTD_SINK=single_alsa", "JASPER_OUTPUTD_ACTIVE_LANE=1"]
    if marker is not None:
        lines.append(f"{OUTPUTD_RING_ACTIVE_ENDPOINT_ENV_VAR}={marker}")
    if ring_path is not None:
        lines.append(f"JASPER_OUTPUTD_SHM_RING_PATH={ring_path}")
    return "\n".join(lines) + "\n"


def _ring_path_written(actions):
    from jasper.fanin_coupling import OUTPUTD_RING_PATH_ENV_VAR

    for action in actions:
        if action.action == "set" and action.key == OUTPUTD_RING_PATH_ENV_VAR:
            return action.value
    return None


def test_the_arm_sequence_completes_from_an_unarmed_roleful_box(monkeypatch):
    """ARM, walked from the state a real box is in: marker ABSENT.

    The fixed point the panel proved: with no marker, the single chooser answers
    the ALSA lane, a re-emit reproduces the ALSA lane, and the marker derives
    absent again — three iterations, no movement. ``--endpoint ring`` is the
    operator act that moves the GRAPH first, and the rest of the ladder follows.
    """
    from jasper.cli.active_speaker import _baseline_reemit_endpoint
    from jasper.fanin.coupling_reconcile import COUPLING_SHM_RING, _outputd_actions
    from jasper.output_topology import resolve_output_layout

    topology = _active_topology("mono", "active_2_way")
    preset = _mono_two_way_preset()

    # --- The fixed point, executed rather than asserted. ------------------
    monkeypatch.setattr(
        "jasper.fanin_coupling.ring_active_endpoint_armed", lambda env=None: False
    )
    device = resolve_output_layout(topology, env={}).playback_device
    for _ in range(3):
        graph = _emit_active_baseline(preset, device)
        assert _derived_marker(graph, topology) == OUTPUTD_ACTIVE_PLAYBACK_DEVICE
        device = resolve_output_layout(topology, env={}).playback_device
        assert device == OUTPUTD_ACTIVE_PLAYBACK_DEVICE, (
            "an auto-resolving re-emit reproduced the ALSA lane, as designed — "
            "so nothing in this loop can ever arm the box"
        )

    # --- Step 1: baseline-reemit --endpoint ring. -------------------------
    armed_device, source = _baseline_reemit_endpoint(topology, "ring")
    assert armed_device == RING_ACTIVE_PLAYBACK_DEVICE
    assert source == "explicit_endpoint_ring"
    ring_graph = _emit_active_baseline(preset, armed_device)
    assert f'device: "{RING_ACTIVE_PLAYBACK_DEVICE}"' in ring_graph
    # The ring's own queue handshake rode along, because the emit went through
    # the shared resolver rather than each caller remembering.
    assert "  queuelimit: 1" in ring_graph
    assert "  enable_rate_adjust: false" in ring_graph

    # --- Step 2: the hardware reconciler derives the marker FROM that graph.
    assert _derived_marker(ring_graph, topology) == RING_ACTIVE_PLAYBACK_DEVICE

    # --- Step 3: the coupling reconciler converges the ring PATH. ---------
    # The unarmed stub is dropped first: from here the marker is a REAL value
    # the previous step wrote into outputd.env, and step 3 must read that file
    # rather than a predicate this test is holding down.
    monkeypatch.undo()
    actions = _outputd_actions(COUPLING_SHM_RING, _outputd_env(marker="1"))
    assert _ring_path_written(actions) == DEFAULT_OUTPUTD_ACTIVE_RING_PATH


def test_the_release_sequence_completes_from_an_armed_roleful_box(monkeypatch):
    """ROLLBACK, walked from marker=1 — the direction the same cycle also blocked.

    E1's ordering was coupling -> re-emit(aloop) -> hardware reconciler; B1
    amended it to put the re-emit FIRST, for the same reason the arm needs it:
    the marker has to have a graph to re-derive FROM. Either way the release
    only completes because the endpoint is named explicitly.
    """
    from jasper.cli.active_speaker import _baseline_reemit_endpoint
    from jasper.fanin.coupling_reconcile import COUPLING_LOOPBACK, _outputd_actions
    from jasper.fanin_coupling import OUTPUTD_RING_PATH_ENV_VAR
    from jasper.output_topology import resolve_output_layout

    topology = _active_topology("mono", "active_2_way")
    preset = _mono_two_way_preset()

    # Armed: the chooser answers the ring, so an auto re-emit re-derives the
    # marker SET. The same cycle, inverted.
    monkeypatch.setattr(
        "jasper.fanin_coupling.ring_active_endpoint_armed", lambda env=None: True
    )
    assert (
        resolve_output_layout(topology, env={}).playback_device
        == RING_ACTIVE_PLAYBACK_DEVICE
    )
    auto_graph = _emit_active_baseline(preset, RING_ACTIVE_PLAYBACK_DEVICE)
    assert _derived_marker(auto_graph, topology) == RING_ACTIVE_PLAYBACK_DEVICE

    # --- Step 1: baseline-reemit --endpoint aloop. ------------------------
    released_device, source = _baseline_reemit_endpoint(topology, "aloop")
    assert released_device == OUTPUTD_ACTIVE_PLAYBACK_DEVICE
    assert source == "explicit_endpoint_aloop"
    aloop_graph = _emit_active_baseline(preset, released_device)
    # Byte-identical to the pre-arm graph: the rollback RESTORES the artifact,
    # it does not synthesize a third shape.
    assert aloop_graph == _emit_active_baseline(preset, OUTPUTD_ACTIVE_PLAYBACK_DEVICE)

    # --- Step 2: the reconciler re-derives the marker CLEARED. ------------
    assert _derived_marker(aloop_graph, topology) == OUTPUTD_ACTIVE_PLAYBACK_DEVICE

    # --- Step 3: the coupling reconciler unsets the ring keys entirely. ----
    # Same reason as the arm walk: past this point the marker is the real
    # cleared value on disk, not the armed stub this test was holding.
    monkeypatch.undo()
    actions = _outputd_actions(COUPLING_LOOPBACK, _outputd_env(marker=""))
    assert _ring_path_written(actions) is None
    assert any(
        action.action == "unset" and action.key == OUTPUTD_RING_PATH_ENV_VAR
        for action in actions
    )


def test_every_mid_sequence_state_is_silence_or_coherent_never_wrong_audio():
    """Walk the ladder's INTERMEDIATE states — the ones a crash can strand a box in.

    The bar is not "each step works"; it is that no state BETWEEN two steps
    plays the wrong audio. Each intermediate is either coherent (a working
    pairing) or silence (outputd refuses to attach and parks loudly). The
    seeder walk banked the acoustic half — every branch of the graph seeder on
    a real roleful topology lands on the roleful graph or on ring zero-fill
    silence, never on a full-range flat graph — so what is pinned here is the
    PAIRING that decides which of those two a box gets.

    State A (after step 1, before step 2): the graph names the ring, the marker
    is absent, the coupling is still loopback. outputd reads snd-aloop while
    CamillaDSP writes a ring nobody reads — SILENCE, and the ALSA lane outputd
    reads is simply unwritten. Not wrong audio.

    State B (after step 2, before step 3): marker set, coupling still loopback.
    outputd's allowlist is scoped to the ShmRing bridge, so under ``direct`` the
    marker grants nothing and the box keeps working on snd-aloop. That scoping
    is E1/vN1, and it is what makes this state benign rather than a park.

    State C (step 3, marker set): coherent by construction, because the path is
    DERIVED from the marker rather than preserved.
    """
    from jasper.fanin.coupling_reconcile import COUPLING_SHM_RING, _outputd_actions

    coherent = _outputd_actions(COUPLING_SHM_RING, _outputd_env(marker="1"))
    assert _ring_path_written(coherent) == DEFAULT_OUTPUTD_ACTIVE_RING_PATH

    # The state the blocker described — arming an UNMARKED box — cannot name the
    # active ring at all, so it cannot produce the crossed pair that made outputd
    # admit-then-park. It resolves the stereo ring, which a roleful graph never
    # names, so the box lands on silence rather than on a full-range program
    # reaching a compression driver.
    unmarked = _outputd_actions(COUPLING_SHM_RING, _outputd_env(marker=""))
    assert _ring_path_written(unmarked) != DEFAULT_OUTPUTD_ACTIVE_RING_PATH


def _run_validate_outputd_env(
    tmp_path,
    capsys,
    *,
    graph_yaml: str,
    topology,
    coupling: str,
    marker: str | None,
    dac_id: str = "hifiberry_dac8x",
) -> tuple[int, str]:
    """Run the REAL validator `jasper-audio-hardware-reconcile` shells to.

    The bash reconciler's ``validate_outputd_env_stage`` runs exactly
    ``python -m jasper.cli.audio_config validate-outputd-env`` with these six
    path flags; this drives that same entry point in-process. It is the layer
    the ladder walks above do NOT touch — they call ``_outputd_actions`` and the
    marker derivation directly — which is why all four of them passed while the
    real ladder deadlocked at step 2 on jts3.
    """
    from jasper.cli.audio_config import main as audio_config_main
    from jasper.output_topology import save_output_topology

    graph = tmp_path / "graph.yml"
    graph.write_text(graph_yaml, encoding="utf-8")
    statefile = tmp_path / "outputd-statefile.yml"
    statefile.write_text(f"config_path: {graph}\n", encoding="utf-8")
    topology_path = tmp_path / "output_topology.json"
    save_output_topology(topology, path=topology_path)
    base_env = tmp_path / "jasper.env"
    base_env.write_text(f"JASPER_AUDIO_DAC_ID={dac_id}\n", encoding="utf-8")
    outputd_env = tmp_path / "outputd.env"
    outputd_env.write_text(_outputd_env(marker=marker), encoding="utf-8")
    fanin_env = tmp_path / "fanin.env"
    fanin_env.write_text(
        f"JASPER_FANIN_CAMILLA_COUPLING={coupling}\n", encoding="utf-8"
    )

    capsys.readouterr()
    rc = audio_config_main(
        [
            "validate-outputd-env",
            "--base-env",
            str(base_env),
            "--outputd-env",
            str(outputd_env),
            "--fanin-env",
            str(fanin_env),
            "--camilla-statefile",
            str(statefile),
            "--camilla2-statefile",
            str(tmp_path / "crossover-statefile.yml"),
            "--output-topology",
            str(topology_path),
        ]
    )
    return rc, capsys.readouterr().out


def test_the_arm_ladder_clears_the_validator_the_reconciler_actually_runs(
    tmp_path, capsys
):
    """The missing layer: the ladder walked THROUGH `validate-outputd-env`.

    The four walks above drive the emit, marker-derivation, and coupling layers
    and all passed — while the real ladder deadlocked on jts3 (2026-08-11) at
    step 2 with exit 78, ``preserved=1``, detail ``post-DSP route has no
    registered outputd capture for Camilla playback jts_ring_active_playback``
    (``captures/r7b-jts3-arm-20260811T111338Z``, file 15). None of them called
    the validator, so none could see it.

    Both waypoint states run here, because the marker does NOT change which
    branch the validator takes — the transport plan is chosen from the persisted
    COUPLING, which is still loopback until step 3:

      ARM-1  graph names the active ring, marker ABSENT   (after step 1)
      ARM-2  graph names the active ring, marker = "1"    (after step 2)

    Both must validate CLEAN, or the ladder cannot advance.
    """
    from jasper.cli.active_speaker import _baseline_reemit_endpoint
    from jasper.fanin.coupling_reconcile import COUPLING_SHM_RING, _outputd_actions

    topology = _active_topology("mono", "active_2_way")
    preset = _mono_two_way_preset()

    # --- Step 1: the operator names the endpoint; the graph moves first. ---
    armed_device, _source = _baseline_reemit_endpoint(topology, "ring")
    assert armed_device == RING_ACTIVE_PLAYBACK_DEVICE
    ring_graph = _emit_active_baseline(preset, armed_device)

    # --- ARM-1: marker absent. The state step 1 leaves behind. -------------
    rc, out = _run_validate_outputd_env(
        tmp_path,
        capsys,
        graph_yaml=ring_graph,
        topology=topology,
        coupling="loopback",
        marker=None,
    )
    assert rc == 0, out
    assert out.startswith("ok note="), out
    assert "arm waypoint" in out, out

    # --- Step 2: the marker derives from that same graph. ------------------
    assert _derived_marker(ring_graph, topology) == RING_ACTIVE_PLAYBACK_DEVICE

    # --- ARM-2: marker set, coupling still loopback. The jts3 state. -------
    rc, out = _run_validate_outputd_env(
        tmp_path,
        capsys,
        graph_yaml=ring_graph,
        topology=topology,
        coupling="loopback",
        marker="1",
    )
    assert rc == 0, out
    assert out.startswith("ok note="), out
    # The note names the state and BOTH exits — a box can be left here.
    assert "goes silent at the next CamillaDSP load" in out, out
    assert "jasper-fanin-coupling-reconcile shm_ring" in out, out
    assert "baseline-reemit --endpoint aloop" in out, out

    # --- Step 3: with step 2's marker on disk, the path converges. ---------
    actions = _outputd_actions(COUPLING_SHM_RING, _outputd_env(marker="1"))
    assert _ring_path_written(actions) == DEFAULT_OUTPUTD_ACTIVE_RING_PATH


def test_the_stereo_ring_under_a_loopback_plan_still_fails_the_validator(
    tmp_path, capsys
):
    """The loaded gun keeps its refusal, through the SAME entry point.

    Softening the ACTIVE ring must not soften ``jts_ring_playback``. That device
    carries a full-range stereo program an active emitter may never target, no
    ladder produces it, and there is no next step that makes it coherent — so a
    graph naming it under a loopback plan stays a hard refusal with the verbatim
    detail the reconciler logs.
    """
    stereo_ring_graph = (
        "devices:\n"
        "  samplerate: 48000\n"
        "  channels: 2\n"
        "  capture:\n"
        "    type: Alsa\n"
        "    device: jasper_dsp_in\n"
        "  playback:\n"
        "    type: Alsa\n"
        f"    device: {RING_PLAYBACK_DEVICE}\n"
    )

    rc, out = _run_validate_outputd_env(
        tmp_path,
        capsys,
        graph_yaml=stereo_ring_graph,
        topology=_active_topology("mono", "active_2_way"),
        coupling="loopback",
        marker=None,
    )

    assert rc == 1, out
    assert "no registered outputd capture" in out, out
    assert RING_PLAYBACK_DEVICE in out, out
    assert "note=" not in out, out


def _reemit_harness(monkeypatch, tmp_path, *, classification=None, yaml_text="graph: 1\n"):
    """Stage `baseline-reemit` around a real on-disk artifact + statefile.

    The recompose and the classifier are stubbed — they have their own tests, and
    what this command owns is WHERE the bytes land and WHAT it refuses to write.
    Everything the command itself decides (destination, atomicity, repoint,
    refusal) runs for real.
    """
    from jasper.active_speaker.runtime_contract import (
        GRAPH_APPROVED_ACTIVE_RUNTIME,
        GraphSafety,
    )
    from jasper.cli import active_speaker as cli

    artifact = tmp_path / "configs" / "active_speaker_baseline_candidate_abc.yml"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("stale: true\n", encoding="utf-8")
    statefile = tmp_path / "outputd-statefile.yml"
    statefile.write_text("config_path: /somewhere/else.yml\n", encoding="utf-8")

    applied = {"status": "applied", "config": {"path": str(artifact)}}
    seen: dict[str, object] = {}

    monkeypatch.setattr(
        cli, "load_output_topology_strict", lambda *a, **k: _active_topology(
            "mono", "active_2_way"
        )
    )
    monkeypatch.setattr(
        "jasper.active_speaker.baseline_profile.load_applied_baseline_profile_state",
        lambda *a, **k: applied,
    )
    monkeypatch.setattr(
        "jasper.active_speaker.baseline_profile.promote_applied_baseline_candidate",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "jasper.bass_extension.profile.evaluate_bass_extension_profile",
        lambda **k: SimpleNamespace(status="rejected", profile=None),
    )

    def _recompose(topology, **kwargs):
        seen["playback_device"] = kwargs.get("playback_device")
        return yaml_text, []

    monkeypatch.setattr(
        "jasper.active_speaker.baseline_profile.recompose_applied_baseline_yaml",
        _recompose,
    )
    monkeypatch.setattr(
        "jasper.active_speaker.runtime_contract.classify_bass_extension_graph",
        lambda *a, **k: GraphSafety(
            classification=classification or GRAPH_APPROVED_ACTIVE_RUNTIME,
            allowed=classification is None,
            issues=(
                ()
                if classification is None
                else ({"severity": "blocker", "code": "x", "message": "nope"},)
            ),
        ),
    )
    return SimpleNamespace(
        artifact=artifact, statefile=statefile, seen=seen, applied=applied
    )


@pytest.mark.parametrize(
    "endpoint,expected",
    [("ring", RING_ACTIVE_PLAYBACK_DEVICE), ("aloop", OUTPUTD_ACTIVE_PLAYBACK_DEVICE)],
)
def test_baseline_reemit_writes_the_live_artifact_and_repoints_the_statefile(
    monkeypatch, tmp_path, endpoint, expected
):
    """The command that four surfaces said completed the arm now actually does.

    It wrote NOTHING by default before this round — no --out meant "emit and
    report" — so every claim that re-emitting closes the stale-artifact gap was
    false. Both device modes are checked: the arm and the rollback are the same
    code path with a different endpoint, and a mode that silently fell back to
    the auto answer would reintroduce the fixed point.
    """
    from jasper.cli.active_speaker import main

    h = _reemit_harness(monkeypatch, tmp_path)
    code = main([
        "baseline-reemit",
        "--endpoint", endpoint,
        "--statefile", str(h.statefile),
    ])
    assert code == 0
    assert h.seen["playback_device"] == expected
    # The bytes landed on the artifact the statefile and the classifier read...
    assert h.artifact.read_text(encoding="utf-8") == "graph: 1\n"
    # ...and the boot pointer now names it.
    assert f"config_path: {h.artifact}" in h.statefile.read_text(encoding="utf-8")


def test_baseline_reemit_refusal_writes_nothing_at_all(monkeypatch, tmp_path):
    """A graph that fails the re-proof must not reach disk — artifact OR statefile.

    This is the whole reason the classification runs before the write rather than
    after it: a half-written arm leaves the box pointing at a graph the runtime
    contract rejects, which is strictly worse than not arming.
    """
    from jasper.cli.active_speaker import main

    h = _reemit_harness(monkeypatch, tmp_path, classification="unsafe")
    code = main([
        "baseline-reemit",
        "--endpoint", "ring",
        "--statefile", str(h.statefile),
    ])
    assert code == 1
    assert h.artifact.read_text(encoding="utf-8") == "stale: true\n"
    assert "config_path: /somewhere/else.yml" in h.statefile.read_text(encoding="utf-8")


def test_baseline_reemit_out_is_preview_only(monkeypatch, tmp_path):
    """--out keeps its inspection semantics and is NAMED as preview.

    It must not touch the live artifact or the statefile — an operator reaching
    for a preview is not asking to arm the box.
    """
    from jasper.cli.active_speaker import main

    h = _reemit_harness(monkeypatch, tmp_path)
    preview = tmp_path / "preview.yml"
    code = main([
        "baseline-reemit",
        "--endpoint", "ring",
        "--out", str(preview),
        "--statefile", str(h.statefile),
    ])
    assert code == 0
    assert preview.read_text(encoding="utf-8") == "graph: 1\n"
    assert h.artifact.read_text(encoding="utf-8") == "stale: true\n"
    assert "config_path: /somewhere/else.yml" in h.statefile.read_text(encoding="utf-8")


def test_baseline_reemit_publishes_atomically(monkeypatch, tmp_path):
    """The module's own convention: publish through atomic_io, never write_text.

    A torn artifact is a graph CamillaDSP can fail to load on its next restart,
    which on a roleful box is a silent speaker. The previous --out path used a
    bare ``write_text`` against a module family that atomically publishes
    everywhere else.
    """
    from jasper.cli.active_speaker import main

    h = _reemit_harness(monkeypatch, tmp_path)
    calls: list[tuple] = []
    import jasper.atomic_io as atomic_io

    real = atomic_io.atomic_write_text

    def _spy(path, text, **kwargs):
        calls.append((str(path), kwargs))
        return real(path, text, **kwargs)

    monkeypatch.setattr(atomic_io, "atomic_write_text", _spy)
    code = main([
        "baseline-reemit",
        "--endpoint", "ring",
        "--statefile", str(h.statefile),
    ])
    assert code == 0
    assert [c for c in calls if c[0] == str(h.artifact)], calls
    # ...and durably, because these bytes are the box's next boot graph.
    assert calls[0][1].get("durable") is True


def test_the_crossed_pair_is_unreachable_from_the_reconciler():
    """The crossed state the allowlist exists to refuse is now unconstructible.

    Before the convergence, ``_outputd_actions`` PRESERVED whatever ring path
    the file carried, so an armed box could be handed the stereo ring's path —
    the exact pair outputd bails on. Deriving the path from the marker means the
    reconciler cannot emit that pair for any input text, checked here against
    the PR's own Python-side coherence twin rather than by re-stating the rule.
    """
    from jasper.audio_runtime_plan import (
        TRANSPORT_SHM_RING_ACTIVE,
        transport_coherence_errors,
        transport_topology_for_coupling,
    )
    from jasper.fanin.coupling_reconcile import COUPLING_SHM_RING, _outputd_actions
    from jasper.fanin_coupling import (
        DEFAULT_OUTPUTD_RING_PATH,
        OUTPUTD_RING_PATH_ENV_VAR,
    )

    # Hostile input: the file already names the STEREO ring while the marker
    # says armed. The old code preserved that value verbatim.
    hostile = _outputd_env(marker="1", ring_path=DEFAULT_OUTPUTD_RING_PATH)
    actions = _outputd_actions(COUPLING_SHM_RING, hostile)
    assert _ring_path_written(actions) == DEFAULT_OUTPUTD_ACTIVE_RING_PATH, (
        "an armed box was handed the stereo ring's path — this is the pair "
        "outputd bails on at startup (exit 78, silent speaker)"
    )

    # ...and the env the reconciler produces passes the coherence twin that
    # mirrors outputd's startup allowlist, so the reconciler cannot commit what
    # the daemon would refuse to start on.
    converged = {
        OUTPUTD_RING_ACTIVE_ENDPOINT_ENV_VAR: "1",
        OUTPUTD_RING_PATH_ENV_VAR: _ring_path_written(actions),
        "JASPER_OUTPUTD_CONTENT_BRIDGE": "shm_ring",
    }
    plan = transport_topology_for_coupling(
        COUPLING_SHM_RING, outputd_env=converged
    )
    assert plan.name == TRANSPORT_SHM_RING_ACTIVE
    errors = transport_coherence_errors(
        coupling=COUPLING_SHM_RING,
        outputd_env=converged,
        camilla_devices={"playback_device": RING_ACTIVE_PLAYBACK_DEVICE},
    )
    assert not [e for e in errors if "may read only" in e], errors

    # ...and the CROSSED pair the reconciler can no longer produce IS still
    # reported by that twin — a positive control, so the clean result above is
    # evidence the crossing is absent rather than evidence the check is inert.
    crossed = dict(converged, **{OUTPUTD_RING_PATH_ENV_VAR: DEFAULT_OUTPUTD_RING_PATH})
    crossed_errors = transport_coherence_errors(
        coupling=COUPLING_SHM_RING,
        outputd_env=crossed,
        camilla_devices={"playback_device": RING_ACTIVE_PLAYBACK_DEVICE},
    )
    assert [e for e in crossed_errors if "may read only" in e], crossed_errors
