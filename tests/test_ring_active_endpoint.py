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

import contextlib
import io
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from jasper import ring_assets

from .doctor_test_support import record_active_dac
from jasper.active_speaker import camilla_yaml as active_camilla_yaml
from jasper.camilla_config_contract import (
    DEFAULT_CAPTURE_FORMAT,
    DEFAULT_CAPTURE_DEVICE,
    RETIRED_ALOOP_CAPTURE_DEVICE,
    parse_camilla_devices_config,
)
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
    DEFAULT_OUTPUTD_RING_PATH,
    OUTPUTD_RING_ACTIVE_ENDPOINT_ENV_VAR,
    RING_ACTIVE_PLAYBACK_DEVICE,
    RING_CAMILLA_CHUNKSIZE,
    RING_CAMILLA_TARGET_LEVEL,
    RING_CAPTURE_DEVICE,
    RING_PLAYBACK_DEVICE,
    ring_active_endpoint_armed,
    resolve_ring_wire,
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


def _apple_dongle_passive_stereo():
    """The shipped Apple sink after explicit passive-stereo authorization."""
    from jasper.output_topology import OutputTopology

    raw = _apple_dongle_shipped_default().to_dict()
    passive = _full_range_stereo().to_dict()
    raw["speaker_groups"] = passive["speaker_groups"]
    raw["routing"] = passive["routing"]
    return OutputTopology.from_mapping(raw)


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
            },
            "tweeter": {
                "manufacturer": "B&C Speakers",
                "model": "DE250",
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
        ("apple passive stereo", _apple_dongle_passive_stereo),
        ("full-range stereo", _full_range_stereo),
    ],
)
def test_a_non_roleful_topology_has_ring_b_and_no_active_ring(name, factory):
    topo = factory()
    assert ring_channels_for_topology(topo) == 2
    assert active_ring_channels_for_topology(topo) is None, name


def test_an_unconfigured_shipped_default_has_no_ring_b_or_active_ring():
    topo = _apple_dongle_shipped_default()
    assert ring_channels_for_topology(topo) is None
    assert active_ring_channels_for_topology(topo) is None


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


def test_the_active_ring_is_the_only_accepted_endpoint():
    """#2285 P2: one member, and the RETIRED lane is refused by name.

    This used to be parametrized over both transports of the active lane. The
    snd-aloop lane's PCM definitions were deleted (#2534) and its MEMBERSHIP
    with it, so the second case is now a REJECTION rather than a second accept —
    and that rejection is why ``OUTPUTD_ACTIVE_PLAYBACK_DEVICE`` still has a
    name at all. Every box commissioned before the retirement has a graph on
    disk spelling it, so this is the shape the width probe actually meets.
    """
    width, problem, accepted = _outputd_endpoint_width(
        _endpoint_graph(RING_ACTIVE_PLAYBACK_DEVICE), 8
    )
    assert (width, problem, accepted) == (2, None, RING_ACTIVE_PLAYBACK_DEVICE)

    retired = _outputd_endpoint_width(
        _endpoint_graph(OUTPUTD_ACTIVE_PLAYBACK_DEVICE), 8
    )
    assert retired == (None, "active_outputd_lane_missing", None)
    assert OUTPUTD_ACTIVE_PLAYBACK_DEVICE not in OUTPUTD_LEGAL_ENDPOINT_DEVICES


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
    # FIVE branches state the lane. The composite became TWO in P8b item 1f:
    # it may now be an ACTIVE-ring endpoint, so it stages the pair when the
    # accepted graph names the ring device and keeps its historical
    # unconditional clear on every other path (an aloop composite stays byte-
    # identical). The other three are unchanged: active, non-active, parked.
    assert len(pair_calls) == 5, pair_calls
    assert (
        'set_outputd_active_lane_pair "1" "$DUAL_APPLE_ACTIVE_ENDPOINT_DEVICE"'
        ' && changed=1' in pair_calls
    ), (
        "the composite's ring-staging call is gone — no composite box can be "
        "armed without it, whatever the Python preflights admit"
    )
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
    ``transport_coherence_report``' playback comparison VACUOUS: it would derive
    the expectation from the value it is checking, so a graph pointed at the
    wrong ring would define itself correct. The marker is an independent fact
    written by a different owner, which is what gives the comparison something to
    disagree with.
    """
    from jasper.fanin_coupling import COUPLING_SHM_RING, TRANSPORT_SHM_RING_ACTIVE
    from jasper.transport_coherence import transport_topology_for_coupling

    stereo = transport_topology_for_coupling("shm_ring", outputd_env={})
    assert stereo.name == COUPLING_SHM_RING
    assert stereo.camilla_to_outputd["camilla_playback_device"] == RING_PLAYBACK_DEVICE

    # The marker alone flips the shape: the observed Camilla playback device is
    # not an input to this function at all, which is what keeps the comparison
    # from deriving its expectation from the value it checks.
    active = transport_topology_for_coupling(
        "shm_ring",
        outputd_env={OUTPUTD_RING_ACTIVE_ENDPOINT_ENV_VAR: "1"},
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
    from jasper.fanin_coupling import TRANSPORT_SHAPES
    from jasper.transport_coherence import transport_topology_for_coupling

    from jasper.multiroom.dac_content_ring import DAC_CONTENT_LANE_ENV

    produced = {
        transport_topology_for_coupling("loopback").name,
        transport_topology_for_coupling("shm_ring", outputd_env={}).name,
        transport_topology_for_coupling(
            "shm_ring",
            outputd_env={OUTPUTD_RING_ACTIVE_ENDPOINT_ENV_VAR: "1"},
        ).name,
        # A DUMB bonded member: the dac-content marker, and no bridge — the only
        # shape a marker-armed box may carry, because outputd refuses the marker
        # beside a declared bridge of any value.
        transport_topology_for_coupling(
            "shm_ring",
            outputd_env={DAC_CONTENT_LANE_ENV: "1"},
        ).name,
    }
    assert produced == set(TRANSPORT_SHAPES)


def test_a_crossed_ring_path_is_the_first_arm_waypoint_not_a_refusal():
    """The pair is REPORTED before the daemon bails — as a waypoint, not an error.

    outputd's allowlist still refuses the crossed pair at its own startup; that
    is the fail-closed floor and it is untouched. What this layer reports is a
    ring PATH lagging its MARKER, and a lag is not a contradiction: the path is
    the marker's projection, with one derivation and one writer, so the state is
    always one pass from converged. Reporting it as an error refused the marker
    whose absence was the only reason the path had not moved — the deadlock that
    took jts.local's DSP graph down on 2026-08-21.

    ``errors`` must be CLEAN here, or the reconciler still exits 78.
    """
    from jasper.transport_coherence import transport_coherence_report

    crossed = {
        OUTPUTD_RING_ACTIVE_ENDPOINT_ENV_VAR: "1",
        "JASPER_OUTPUTD_CONTENT_BRIDGE": "shm_ring",
        "JASPER_OUTPUTD_SHM_RING_PATH": DEFAULT_OUTPUTD_RING_PATH,
    }
    report = transport_coherence_report(coupling="shm_ring", outputd_env=crossed)
    assert any("FIRST-ARM waypoint" in n for n in report.notes), report
    assert any(DEFAULT_OUTPUTD_ACTIVE_RING_PATH in n for n in report.notes), report
    assert report.errors == (), report


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
    from jasper.transport_coherence import transport_coherence_report

    stereo = transport_coherence_report(
        coupling="loopback",
        outputd_env={},
        camilla_devices={"playback_device": RING_PLAYBACK_DEVICE},
    )
    assert any("no registered outputd capture" in e for e in stereo.errors), stereo
    assert stereo.notes == (), stereo

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
    # #2285 P2: the note used to offer a rollback exit as well
    # (`baseline-reemit --endpoint aloop`). That endpoint is retired, so the
    # note must say there is only one way out and must NOT name a command
    # argparse now rejects — a remediation the operator cannot run is worse
    # than none.
    assert "no rollback direction" in note, note
    assert "--endpoint aloop" not in note, note


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


def test_the_unattended_pass_has_its_own_roleful_gate():
    """Independent of every eligibility predicate, on purpose.

    ``ring_topology_ready`` has an arm that ADMITS a roleful topology, so the
    auto pass cannot rely on "roleful boxes fail the topology gate". This gate
    asks the roleful question on its own — so a future change to any eligibility
    predicate cannot re-open unattended arming of a crossover speaker by accident.
    """
    from jasper.fanin.coupling_reconcile import default_ring_gates

    names = [name for name, _gate in default_ring_gates()]
    assert "ring_roleful_unattended" in names
    # ...and it runs BEFORE the topology gate, so its coarser refusal is the one
    # an operator of a crossover box reads.
    assert names.index("ring_roleful_unattended") < names.index("ring_topology")


def _no_applied_profile_and_no_anchor(monkeypatch, tmp_path):
    """A roleful box carrying NEITHER proven arm — the design's N8 case."""
    monkeypatch.setattr(
        "jasper.active_speaker.baseline_profile.load_applied_baseline_profile_state",
        lambda *a, **k: None,
    )
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(tmp_path / "absent.yml"))


def test_the_roleful_gate_admits_a_passive_box_and_refuses_a_roleful_one(
    monkeypatch, tmp_path
):
    """The fail-closed DEFAULT, both polarities.

    Not-roleful is admitted exactly as before the narrowing. Roleful with
    neither proven arm is still refused — that is N8, and the refusal names a
    RUNNABLE remediation rather than leaving the operator with a verdict.
    """
    from jasper.fanin import coupling_reconcile

    _no_applied_profile_and_no_anchor(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "jasper.output_topology.load_output_topology_strict",
        lambda *a, **k: _active_topology("mono", "active_2_way"),
    )
    ok, detail = coupling_reconcile.ring_roleful_unattended_ready()
    assert ok is False
    assert "jasper-fanin-coupling-reconcile shm_ring" in detail

    monkeypatch.setattr(
        "jasper.output_topology.load_output_topology_strict",
        lambda *a, **k: _full_range_stereo(),
    )
    ok, _detail = coupling_reconcile.ring_roleful_unattended_ready()
    assert ok is True


def test_the_roleful_gate_fails_closed_on_an_unreadable_topology(monkeypatch):
    from jasper.fanin import coupling_reconcile
    from jasper.output_topology import OutputTopologyError

    def _boom(*a, **k):
        raise OutputTopologyError("corrupt")

    monkeypatch.setattr("jasper.output_topology.load_output_topology_strict", _boom)
    ok, detail = coupling_reconcile.ring_roleful_unattended_ready()
    assert ok is False
    assert "fail-closed" in detail


# --- the steps-1-2 corollary: the auto pass now finishes step 3 --------------


def _steps_one_and_two_box(monkeypatch, tmp_path):
    """Stage a roleful box that has completed ladder steps 1 and 2, NOT step 3.

    Every state here comes from the REAL writers' artifacts rather than a
    synthetic gate flag:

    * **step 1** (``baseline-reemit --endpoint ring``) leaves the box's
      published all-muted anchor loaded AT the ring endpoint — the graph, the
      statefile and the staged record are real files, via ``_stage_box``;
    * **step 2** (``jasper-audio-hardware-reconcile``) writes outputd's endpoint
      marker into ``outputd.env`` and renders the conf.d block at this box's
      resolved active width (4 for the composite);
    * **step 3** (``jasper-fanin-coupling-reconcile shm_ring``) is the ONLY
      writer of the operator-choice marker, and it is exactly what has not run.

    ``force_ring_gates_pass`` covers the infrastructure gates (asset presence,
    geometry, the ioplug provenance record a dev host has no way to satisfy). It
    does NOT stub the roleful gate, the topology gate, or the endpoint proof —
    the three this test is actually about — so the chain stays non-vacuous.
    """
    import jasper.ring_assets as ra
    from jasper.fanin import coupling_reconcile as cr
    from jasper.fanin_coupling import (
        OUTPUTD_RING_ACTIVE_ENDPOINT_ENV_VAR,
        RING_ACTIVE_PLAYBACK_DEVICE,
        RING_CAPTURE_DEVICE,
    )
    from tests.test_composite_ring_arm_enabling import _composite_active_2way
    from tests.test_fanin_coupling_reconcile import force_ring_gates_pass
    from tests.test_ring_anchor_arm_acceptance import _graph_yaml, _stage_box

    force_ring_gates_pass(monkeypatch)

    _stage_box(
        tmp_path,
        monkeypatch,
        graph_yaml=_graph_yaml(
            capture_device=RING_CAPTURE_DEVICE,
            playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
            fmt="S32_LE",
        ),
    )
    monkeypatch.setattr(
        "jasper.output_topology.load_output_topology_strict", _composite_active_2way
    )
    monkeypatch.setattr(
        "jasper.active_speaker.baseline_profile.load_applied_baseline_profile_state",
        lambda *a, **k: None,
    )

    outputd_env = tmp_path / "outputd.env"
    outputd_env.write_text(
        f"{OUTPUTD_RING_ACTIVE_ENDPOINT_ENV_VAR}=1\n", encoding="utf-8"
    )
    monkeypatch.setattr(cr, "OUTPUTD_ENV_PATH", str(outputd_env))

    conf_d = tmp_path / "60-jts-ring.conf"
    conf_d.write_text(
        "pcm.jts_ring_capture {\n    format S32_LE\n}\n"
        "pcm.jts_ring_playback {\n    format S32_LE\n}\n"
        f"pcm.{ra.RING_ACTIVE_CONF_PCM} {{\n"
        "    format S32_LE\n    channels 4\n}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ra, "RING_CONF_D", str(conf_d))


# --- the two proven arms (§4.7, owner ruling §12 decision 1) -----------------


def _applied_profile_for(topology, *, fingerprint: str | None = None):
    """An applied Layer-A record whose snapshot matches ``topology`` by default."""
    from jasper.active_speaker.baseline_profile import topology_config_fingerprint

    return {
        "status": "applied",
        "recomposition_snapshot": {
            "schema_version": 1,
            "domain": "full",
            "topology_id": topology.topology_id,
            "topology_fingerprint": (
                topology_config_fingerprint(topology)
                if fingerprint is None
                else fingerprint
            ),
        },
    }


def test_arm_one_admits_an_applied_baseline_that_still_matches_the_hardware(
    monkeypatch, tmp_path
):
    """Arm 1: the graph a human already approved for THESE drivers."""
    from jasper.fanin import coupling_reconcile

    topology = _active_topology("mono", "active_2_way")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(tmp_path / "absent.yml"))
    monkeypatch.setattr(
        "jasper.output_topology.load_output_topology_strict", lambda *a, **k: topology
    )
    monkeypatch.setattr(
        "jasper.active_speaker.baseline_profile.load_applied_baseline_profile_state",
        lambda *a, **k: _applied_profile_for(topology),
    )

    ok, detail = coupling_reconcile.ring_roleful_unattended_ready()
    assert ok is True
    assert "applied active-speaker profile" in detail


def test_arm_one_refuses_a_baseline_whose_fingerprint_no_longer_matches(
    monkeypatch, tmp_path
):
    """A real DAC swap: the record is applied, the hardware moved under it.

    The gate falls through to the fail-closed default and NAMES the emitter's
    own blocker code, so the refusal says which fact failed.
    """
    from jasper.fanin import coupling_reconcile

    topology = _active_topology("mono", "active_2_way")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(tmp_path / "absent.yml"))
    monkeypatch.setattr(
        "jasper.output_topology.load_output_topology_strict", lambda *a, **k: topology
    )
    monkeypatch.setattr(
        "jasper.active_speaker.baseline_profile.load_applied_baseline_profile_state",
        lambda *a, **k: _applied_profile_for(topology, fingerprint="a-different-box"),
    )

    ok, detail = coupling_reconcile.ring_roleful_unattended_ready()
    assert ok is False
    assert "applied_baseline_snapshot_topology_stale" in detail
    assert "jasper-fanin-coupling-reconcile shm_ring" in detail


def test_arm_two_admits_the_all_muted_anchor_before_it_reaches_the_ring(
    monkeypatch, tmp_path
):
    """Arm 2, and the reason it cannot be ``ring_endpoint_anchor_converged``.

    The anchor is staged at the ALSA endpoint a roleful box plays BEFORE it is
    armed. Endpoint and wire are what an arm CHANGES, so an identity question
    that included them would refuse every box this arm exists to admit.
    """
    from jasper.active_speaker.runtime_contract import OUTPUTD_ACTIVE_PLAYBACK_DEVICE
    from jasper.fanin import coupling_reconcile
    from tests.test_composite_ring_arm_enabling import _composite_active_2way
    from tests.test_ring_anchor_arm_acceptance import _graph_yaml, _stage_box

    monkeypatch.setattr(
        "jasper.active_speaker.baseline_profile.load_applied_baseline_profile_state",
        lambda *a, **k: None,
    )
    _stage_box(
        tmp_path,
        monkeypatch,
        graph_yaml=_graph_yaml(
            capture_device="hw:Loopback,1,7",
            playback_device=OUTPUTD_ACTIVE_PLAYBACK_DEVICE,
            fmt="S32_LE",
        ),
    )
    monkeypatch.setattr(
        "jasper.output_topology.load_output_topology_strict",
        _composite_active_2way,
    )

    ok, detail = coupling_reconcile.ring_roleful_unattended_ready()
    assert ok is True
    assert "all-muted" in detail


def test_arm_two_refuses_an_anchor_that_is_not_terminally_muted(monkeypatch, tmp_path):
    """The anchor's whole safety claim is the mute. One live output withdraws it."""
    from jasper.active_speaker.runtime_contract import OUTPUTD_ACTIVE_PLAYBACK_DEVICE
    from jasper.fanin import coupling_reconcile
    from tests.test_composite_ring_arm_enabling import _composite_active_2way
    from tests.test_ring_anchor_arm_acceptance import _graph_yaml, _stage_box

    monkeypatch.setattr(
        "jasper.active_speaker.baseline_profile.load_applied_baseline_profile_state",
        lambda *a, **k: None,
    )
    _stage_box(
        tmp_path,
        monkeypatch,
        graph_yaml=_graph_yaml(
            capture_device="hw:Loopback,1,7",
            playback_device=OUTPUTD_ACTIVE_PLAYBACK_DEVICE,
            fmt="S32_LE",
            mute_gains_db={1: -20.0},
        ),
    )
    monkeypatch.setattr(
        "jasper.output_topology.load_output_topology_strict",
        _composite_active_2way,
    )

    ok, detail = coupling_reconcile.ring_roleful_unattended_ready()
    assert ok is False
    assert "jasper-fanin-coupling-reconcile shm_ring" in detail


def test_the_roleful_gate_refuses_a_corrupt_applied_record_with_a_remedy(
    monkeypatch, tmp_path
):
    """A corrupt applied record refuses LOCALLY, with the runnable arm named.

    ``_load_saved_state`` catches ``FileNotFoundError``/``OSError``/
    ``JSONDecodeError`` and returns ``None``, but a non-UTF-8 byte — the
    documented SD-card / power-cut truncation — raises ``UnicodeDecodeError``
    (a ``ValueError``) straight past it. ``resolve_auto_decision``'s backstop
    would fail closed on that anyway, but its sentence carries NO remediation.
    Caught in the gate so every refusal keeps the same shape.
    """
    import jasper.active_speaker.baseline_profile as bp
    from jasper.fanin import coupling_reconcile

    record = tmp_path / "baseline_profile.json"
    record.write_bytes(b'{"status": "applied", "x": "\xff\xfe not utf-8"}')
    monkeypatch.setattr(bp, "baseline_profile_state_path", lambda *a, **k: record)
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(tmp_path / "absent.yml"))
    monkeypatch.setattr(
        "jasper.output_topology.load_output_topology_strict",
        lambda *a, **k: _active_topology("mono", "active_2_way"),
    )

    # The raise really does escape the shared loader — otherwise this test would
    # be pinning the gate against a hazard that no longer exists.
    with pytest.raises(UnicodeDecodeError):
        bp.load_applied_baseline_profile_state()

    ok, detail = coupling_reconcile.ring_roleful_unattended_ready()
    assert ok is False
    assert "could not be read" in detail
    assert "jasper-fanin-coupling-reconcile shm_ring" in detail


def test_the_roleful_gate_does_not_ask_the_divergence_question(monkeypatch, tmp_path):
    """SCOPE PIN: ``applied_profile_displacement`` is phase 0c's, not this gate's.

    The convergence design places applied-record DIVERGENCE at phase 0c (N5) and
    arm 1 at the fingerprint compare only. Pulling the displacement read forward
    into this gate would move a P7 decision into a P6 predicate, so a spy proves
    it is never consulted.

    ALL THREE outcomes are driven, and the third is why this reads the way it
    does: an earlier version set an absent statefile in every config, so
    ``graph.note`` was always set and ARM 2'S BRANCH NEVER RAN — a displacement
    read placed there would have passed the spy. The anchor config below is what
    makes the guard cover the branch it claims to.
    """
    import jasper.active_speaker.baseline_profile as bp
    from jasper.active_speaker.runtime_contract import OUTPUTD_ACTIVE_PLAYBACK_DEVICE
    from jasper.fanin import coupling_reconcile
    from tests.test_composite_ring_arm_enabling import _composite_active_2way
    from tests.test_ring_anchor_arm_acceptance import _graph_yaml, _stage_box

    calls: list[str] = []
    monkeypatch.setattr(
        bp,
        "applied_profile_displacement",
        lambda *a, **k: calls.append("asked") or "",
    )

    # 1 + 2: the default refusal, and arm 1 admitting.
    topology = _active_topology("mono", "active_2_way")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(tmp_path / "absent.yml"))
    monkeypatch.setattr(
        "jasper.output_topology.load_output_topology_strict", lambda *a, **k: topology
    )
    outcomes = []
    for profile in (None, _applied_profile_for(topology)):
        monkeypatch.setattr(
            bp, "load_applied_baseline_profile_state", lambda *a, **k: profile
        )
        outcomes.append(coupling_reconcile.ring_roleful_unattended_ready()[0])

    # 3: arm 2's branch, actually entered — a readable staged anchor.
    monkeypatch.setattr(bp, "load_applied_baseline_profile_state", lambda *a, **k: None)
    _stage_box(
        tmp_path,
        monkeypatch,
        graph_yaml=_graph_yaml(
            capture_device="hw:Loopback,1,7",
            playback_device=OUTPUTD_ACTIVE_PLAYBACK_DEVICE,
            fmt="S32_LE",
        ),
    )
    monkeypatch.setattr(
        "jasper.output_topology.load_output_topology_strict", _composite_active_2way
    )
    outcomes.append(coupling_reconcile.ring_roleful_unattended_ready()[0])

    # Refuse, arm 1, arm 2 — three distinct paths, so the spy covers them all.
    assert outcomes == [False, True, True]
    assert calls == []


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
    # S32_LE — every block in the shipped conf.d now DECLARES `format S32_LE`
    # explicitly (the ring-wire default flip), so a "coherent" fixture file must
    # carry it too; S16_LE here would itself be a format-axis mismatch and get
    # deleted, defeating "the other two files are coherent" below.
    _ring_file(paths["a"], sample_format=ra.RING_SAMPLE_FORMAT_S32LE)
    _ring_file(paths["b"], sample_format=ra.RING_SAMPLE_FORMAT_S32LE)
    # The conf.d's active block declares the ioplug default (2); this file says 6.
    _ring_file(
        paths["active"], sample_format=ra.RING_SAMPLE_FORMAT_S32LE, channels=6
    )

    cr._delete_stale_ring_files("t", "")

    assert not paths["active"].exists(), (
        "a width-stale ACTIVE ring file must be deleted — the writer's "
        "create-or-attach would otherwise fail against it"
    )
    assert paths["a"].exists() and paths["b"].exists(), (
        "coherent ring files must be left alone"
    )


# --------------------------------------------------------------------------
# 9. INERTNESS — nothing moves until an operator arms.
# --------------------------------------------------------------------------


def test_the_shipped_conf_d_declares_the_active_block_wide_but_its_channels_at_the_ioplug_default():
    """The third block ships STATIC, split across its two geometry axes.

    CHANNELS is still OMITTED exactly as in the other two blocks, so it
    declares the ioplug's own default (2) — a box with no active ring never
    has to render this block, and that omission is what lets
    ``render_ring_conf_wire`` only ever SUBSTITUTE inside an existing block,
    never append one to a file every ALSA client parses.

    FORMAT is no longer omitted. Since the ring-wire default flip, an omitted
    key would declare the C ioplug's compiled-in S16_LE — the OPPOSITE of what
    every OTHER ring end resolves on an undeclared box — so all three blocks,
    this one included, now spell ``format S32_LE`` explicitly (see the conf.d's
    own "WIRE FORMAT" header comment). The literal is still fixed rather than
    per-box rendered, so the block stays exactly as STATIC as the channels axis.
    """
    conf = RING_CONF.read_text(encoding="utf-8")
    body = conf.split(f"pcm.{RING_ACTIVE_PLAYBACK_DEVICE} {{", 1)[1].split("}", 1)[0]
    assert "format S32_LE" in body
    assert "channels" not in body
    assert "period_frames 128" in body
    assert "n_slots 2" in body
    assert ring_assets.ring_conf_channels(
        ring_assets.RING_ACTIVE_CONF_PCM, str(RING_CONF)
    ) == ring_assets.RING_CONF_DEFAULT_CHANNELS
    assert ring_assets.ring_conf_format(
        ring_assets.RING_ACTIVE_CONF_PCM, str(RING_CONF)
    ) == "S32_LE"


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
    # S32_LE matches the shipped conf.d's own explicit `format S32_LE` (every
    # block declares it since the ring-wire default flip) — S16_LE here would
    # make the "control" render below a REAL rewrite, not the no-op the
    # comment underneath it depends on.
    wire = RingWire(
        period_frames=ra.RING_SLOT_FRAMES,
        sample_format="S32_LE",
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
    from jasper.cli.doctor import (
        audio_runtime_fanin,
        audio_runtime_ring,
    )
    from jasper.cli.doctor._registry import registered_checks

    # Importing the module is what runs its @doctor_check decorators; naming the
    # functions below is also what makes that import USED, so this needs no lint
    # suppression (the repo's suppression ceiling is a real gate, not a
    # formality — and it counts the marker's TEXT, so even quoting one here
    # would spend a slot).
    expected = (
        audio_runtime_fanin.check_fanin_coupling,
        audio_runtime_ring.check_ring_conf_floor_render,
        audio_runtime_ring.check_ring_platform_assets,
    )
    registered = {entry.func.__name__ for entry in registered_checks()}
    for func in expected:
        assert func.__name__ in registered, (
            f"{func.__name__} is no longer a registered doctor check"
        )
    # ...and the private helper must NOT have been swept in.
    assert audio_runtime_fanin._requires_roleful_graph.__name__ not in registered


def test_the_floor_render_ok_names_the_roleful_reason_a_box_cannot_ring(monkeypatch):
    """An ``ok`` that means "this box still will not ring" has to SAY WHY.

    The check reads the DAC floor against the conf.d. On any box whose DAC
    declares a matching floor that pair reads green — and a ROLEFUL box still
    may not ring, because the unattended pass arms the active ring only for a
    box already carrying a proven graph (``ring_roleful_unattended_ready``).
    Reporting only
    "period_frames matches" there answers a question nobody asked and leaves the
    real one ("why won't this box ring?") unanswered, which is the same
    defect #2294 fixed for the floor half.
    """
    from jasper.cli.doctor import audio_runtime_ring

    record_active_dac("test_dac")
    monkeypatch.setattr(audio_runtime_ring, "latency_floor_for", lambda dac_id: None)

    monkeypatch.setattr(audio_runtime_ring, "_requires_roleful_graph", lambda: True)
    roleful = audio_runtime_ring.check_ring_conf_floor_render()
    assert roleful.status == "ok"
    assert "ROLEFUL" in roleful.detail
    assert "baseline-reemit --endpoint ring" in roleful.detail

    # A PASSIVE box gets exactly today's sentence — the note is additive, not a
    # rewrite, so the existing #2294 answer is untouched where it was right.
    monkeypatch.setattr(audio_runtime_ring, "_requires_roleful_graph", lambda: False)
    passive = audio_runtime_ring.check_ring_conf_floor_render()
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
    from jasper.cli.doctor import audio_runtime_ring
    from jasper.fanin_coupling import RING_SLOT_FRAMES

    floor = latency_floor_for("hifiberry_dac8x")
    assert floor is not None and floor.outputd_period_frames == RING_SLOT_FRAMES, (
        "this test exists for the MATCHING-floor branch; the DAC8X floor moved"
    )
    conf = tmp_path / "60-jts-ring.conf"
    conf.write_text(RING_CONF.read_text(encoding="utf-8"), encoding="utf-8")

    record_active_dac("hifiberry_dac8x")
    monkeypatch.setattr(audio_runtime_ring, "_JTS_RING_CONF_D", str(conf))

    monkeypatch.setattr(audio_runtime_ring, "_requires_roleful_graph", lambda: True)
    roleful = audio_runtime_ring.check_ring_conf_floor_render()
    assert roleful.status == "ok"
    assert "matches" in roleful.detail
    assert "ROLEFUL" in roleful.detail, (
        "a matching floor on a roleful box reads green while the box still "
        "cannot ring — the ok must say why"
    )

    monkeypatch.setattr(audio_runtime_ring, "_requires_roleful_graph", lambda: False)
    passive = audio_runtime_ring.check_ring_conf_floor_render()
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
    from jasper.cli.doctor import audio_runtime_fanin
    from jasper.fanin_coupling import COUPLING_SHM_RING, OUTPUTD_CONTENT_BRIDGE_SHM_RING

    monkeypatch.setattr(audio_runtime_fanin, "_requires_roleful_graph", lambda: True)
    monkeypatch.setattr(
        "jasper.fanin.ring_health.read_persisted_coupling",
        lambda *a, **k: COUPLING_SHM_RING,
    )
    monkeypatch.setattr(
        "jasper.env_file.read_value",
        lambda text, key: OUTPUTD_CONTENT_BRIDGE_SHM_RING,
    )
    monkeypatch.setattr(
        "jasper.fanin_coupling.ring_active_endpoint_armed", lambda env=None: False
    )
    monkeypatch.setattr(
        audio_runtime_fanin,
        "_active_camilla_config_path",
        lambda *a, **k: ("/tmp/statefile.yml", "/tmp/loaded.yml"),
    )
    monkeypatch.setattr(
        audio_runtime_fanin,
        "read_camilla_devices_config",
        lambda path: {
            "capture_type": "Alsa",
            "capture_device": "jts_ring_capture",
            "playback_device": RING_ACTIVE_PLAYBACK_DEVICE,
        },
    )
    result = audio_runtime_fanin.check_fanin_coupling()
    assert result.status == "warn", result.detail
    assert "no ring is expected here at all" in result.detail
    assert f"(expected {RING_PLAYBACK_DEVICE})" not in result.detail
    assert "baseline-reemit --endpoint ring" in result.detail

    # A PASSIVE box keeps the plain expectation and the plain remedy — the
    # honest phrasing is scoped to the case where the stereo ring is forbidden.
    monkeypatch.setattr(audio_runtime_fanin, "_requires_roleful_graph", lambda: False)
    passive = audio_runtime_fanin.check_fanin_coupling()
    assert passive.status == "warn", passive.detail
    assert f"(expected {RING_PLAYBACK_DEVICE})" in passive.detail
    assert "baseline-reemit" not in passive.detail


def test_the_coupling_warn_on_an_armed_box_names_the_forward_ladder_not_a_rollback(
    monkeypatch,
):
    """An ARMED roleful box on the RETIRED endpoint is sent forward, never back.

    This is the state every box commissioned before #2534 is in once its marker
    arms: the loaded graph still spells ``outputd_active_content_playback``. The
    warn must name the one ladder that converges.

    It used to pin a ROLLBACK route instead, whose destination no longer exists
    (ADR-0100). What survives, and what this asserts, is that the ARMED branch
    still WARNs and still hands over a ladder an operator can actually run —
    the branch has no other test.
    """
    from jasper.active_speaker.runtime_contract import OUTPUTD_ACTIVE_PLAYBACK_DEVICE
    from jasper.cli.doctor import audio_runtime_fanin
    from jasper.fanin_coupling import COUPLING_SHM_RING, OUTPUTD_CONTENT_BRIDGE_SHM_RING

    monkeypatch.setattr(audio_runtime_fanin, "_requires_roleful_graph", lambda: True)
    monkeypatch.setattr(
        "jasper.fanin.ring_health.read_persisted_coupling",
        lambda *a, **k: COUPLING_SHM_RING,
    )
    monkeypatch.setattr(
        "jasper.env_file.read_value",
        lambda text, key: OUTPUTD_CONTENT_BRIDGE_SHM_RING,
    )
    monkeypatch.setattr(
        "jasper.fanin_coupling.ring_active_endpoint_armed", lambda env=None: True
    )
    monkeypatch.setattr(
        audio_runtime_fanin,
        "_active_camilla_config_path",
        lambda *a, **k: ("/tmp/statefile.yml", "/tmp/loaded.yml"),
    )
    monkeypatch.setattr(
        audio_runtime_fanin,
        "read_camilla_devices_config",
        lambda path: {
            "capture_type": "Alsa",
            "capture_device": RING_CAPTURE_DEVICE,
            "playback_device": OUTPUTD_ACTIVE_PLAYBACK_DEVICE,
        },
    )
    result = audio_runtime_fanin.check_fanin_coupling()
    assert result.status == "warn", result.detail
    # The plain expectation, because an armed box IS expected to be on the ring.
    assert f"(expected {RING_ACTIVE_PLAYBACK_DEVICE})" in result.detail
    # The FORWARD ladder, and nothing that argparse or the PCM layer has retired.
    assert "baseline-reemit --endpoint ring" in result.detail
    assert "--endpoint aloop" not in result.detail
    assert "loopback (step 3)" not in result.detail
    assert "2332" not in result.detail

    # The CLEARED-marker case (the sibling test above) still reads differently:
    # with no marker, no ring is expected at all, so the honest phrasing
    # replaces the plain expectation. Asserted here so the two branches cannot
    # quietly collapse into one message.
    monkeypatch.setattr(
        "jasper.fanin_coupling.ring_active_endpoint_armed", lambda env=None: False
    )
    cleared = audio_runtime_fanin.check_fanin_coupling()
    assert cleared.status == "warn", cleared.detail
    assert "no ring is expected here at all" in cleared.detail
    assert f"(expected {RING_ACTIVE_PLAYBACK_DEVICE})" not in cleared.detail


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

    import yaml

    parked = active_camilla_yaml.emit_active_speaker_parked_config
    assert "queuelimit" not in inspect.signature(parked).parameters
    devices = yaml.safe_load(parked(output_count=2))["devices"]
    assert devices["queuelimit"] == 4
    assert devices["enable_rate_adjust"] is False


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
    kwargs = capture_kwargs_for_coupling()
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


def test_resolve_output_layout_answers_the_ring_without_reading_the_marker(
    monkeypatch,
):
    """The single chooser, and it no longer branches on the endpoint marker.

    #2285 P2 renamed this from ``..._keeps_the_alsa_lane_until_the_marker_is_set``
    — a title the change falsifies outright, since nothing keeps the ALSA lane
    any more. What it pinned was the branch that made this chooser a FIXED
    POINT: the marker derives from the loaded graph and the graph's device
    derived from the marker, so no automated pass could move a box between
    transports. Deleting that branch is what makes the roleful path convergent,
    and the successor property is that the marker is not consulted AT ALL —
    driven from both stub values, because a chooser still reading it would
    answer the retired lane in the first half.
    """
    from jasper.output_topology import resolve_output_layout

    topo = _active_topology("mono", "active_2_way")

    layouts = []
    for marker_armed in (False, True):
        monkeypatch.setattr(
            "jasper.fanin_coupling.ring_active_endpoint_armed",
            lambda env=None, armed=marker_armed: armed,
        )
        layout = resolve_output_layout(topo, env={})
        assert layout.playback_device == RING_ACTIVE_PLAYBACK_DEVICE, marker_armed
        layouts.append(layout)

    # The SOURCE token is unchanged, which is why nothing keyed on it (the
    # baseline handoff-issue path) needed an edit.
    unarmed, armed = layouts
    assert armed.playback_device_source == unarmed.playback_device_source
    assert armed.transport_channel_count == unarmed.transport_channel_count


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
#
# There is no ROLLBACK ordering: recovery is forward only, by re-arming, and a
# roleful box on `loopback` is parked, not rolled back.
# --------------------------------------------------------------------------


def _emit_active_baseline(preset, device, *, topology=None):
    """Emit a roleful baseline graph named at ``device``, as production would.

    Composes exactly what ``recompose_applied_baseline_yaml`` composes — the
    whole ``active_emit_devices`` result, not a hand-picked subset of it. That is
    load-bearing rather than tidy: while this helper forwarded only the queue
    pair, every ladder walk below emitted the ring with the box's program-lane
    FORMAT and its loopback chunk/target, and passed — which is how defect A
    reached jts3 (2026-08-11, captures/r7b-jts3-arm2-20260811T132227Z) through
    four green ladder tests.

    Every field is still named EXPLICITLY here, so nothing arrives automatically:
    a field added to ``ActiveEmitDevices`` and not added to this call is the same
    subset-forwarding defect one level up.
    ``test_every_emit_devices_field_reaches_the_emitter`` is the guard that walks
    ``dataclasses.fields`` at BOTH forwarding sites and fails on exactly that.
    """
    from jasper.active_speaker.camilla_yaml import (
        active_emit_devices,
        emit_active_speaker_baseline_config,
    )

    devices = active_emit_devices(device, topology=topology)
    return emit_active_speaker_baseline_config(
        preset,
        playback_device=device,
        capture_device=devices.capture_device,
        capture_format=devices.capture_format,
        playback_format=devices.playback_format,
        chunksize=devices.chunksize,
        target_level=devices.target_level,
        queuelimit=devices.queuelimit,
        enable_rate_adjust=devices.enable_rate_adjust,
        corrections={
            "woofer": {"gain_db": 0.0, "delay_ms": 0.0},
            "tweeter": {"gain_db": 0.0, "delay_ms": 0.0},
        },
        baseline_id="arm-walk",
    )


def _applied_ring_baseline(tmp_path):
    """A real APPLIED profile on the jts3-shaped box, for the production seam.

    Built from ``tests/active_speaker_fixtures`` — the suite's SHARED builders,
    not a sibling test module — so the topology this guard drives is the same
    bench box every other active-speaker test means.
    """
    from jasper.active_speaker.baseline_profile import build_baseline_profile_candidate
    from jasper.active_speaker.crossover_preview import build_crossover_preview
    from tests.active_speaker_fixtures import (
        mono_output_topology,
        standard_design_draft,
        standard_measurements,
        valid_camilla_config,
    )

    topology = mono_output_topology()
    draft = standard_design_draft(topology)
    applied = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=build_crossover_preview(draft),
        measurements=standard_measurements(topology, tmp_path),
        write=False,
        state_path=tmp_path / "active-speaker-profile.json",
        config_path=tmp_path / "configs" / "active-speaker-baseline.yml",
        validate=valid_camilla_config,
    )
    applied["status"] = "applied"
    return topology, applied


def _startup_anchor_site(topology, out_dir, *, playback_device=None):
    """Drive ``stage_protected_startup_config``'s emit against the ring.

    The BOOT anchor (#2364). It is the graph a mid-commission box — the
    fleet-typical composite — actually boots from, so a half-moved device
    contract here is a durable artifact, not a transient one. Like the candidate
    site, ``playback_device`` is overridable only so a caller can drive the same
    site at the ALSA active lane as a control.
    """
    from jasper.active_speaker.crossover_preview import build_crossover_preview
    from jasper.active_speaker.staging import stage_protected_startup_config
    from tests.active_speaker_fixtures import standard_design_draft

    preview = build_crossover_preview(standard_design_draft(topology))

    def call_site():
        stage_protected_startup_config(
            topology,
            crossover_preview=preview,
            playback_device=playback_device or RING_ACTIVE_PLAYBACK_DEVICE,
            config_dir=out_dir,
            # Pinned to tmp_path: the default is the live
            # `/var/lib/jasper/active_speaker_staged_config.json`, so omitting it
            # makes a Pi-side test run overwrite a real speaker's staged metadata.
            metadata_path=out_dir / "staged_metadata.json",
            run_config_check=False,
        )

    return call_site


def _driver_commissioning_site(topology, out_dir, *, playback_device):
    """Drive ``prepare_driver_commissioning_config``'s emit at ``playback_device``.

    The AUDIBLE per-driver emit (#2412), and the seventh site in the walk. It
    forwarded only the device NAME, so every other half of its device contract
    — the capture lane, the two formats, the chunk/target/queue geometry — took
    the emitter's snd-aloop defaults.

    ``playback_device`` is REQUIRED here rather than defaulted, which is the one
    way this entry differs from its six siblings, and the reason is historical:
    this builder used to carry a ``commissioning_transport_supported`` gate that
    refused every member of ``RING_PCM_DEVICES`` before the emitter was reached,
    so the ring would have recorded no kwargs at all and the walk drove it on
    the ALSA active lane. #2412's Wave 3 replaced that refusal with a both-ends
    coherence proof, so the ring is reachable here and the walk drives it there
    like its siblings — the arm where every field's answer differs from the
    emitter's default, which is what makes a dropped field visible. The explicit
    parameter stays because a caller passing the device it means is clearer than
    one relying on the marker, and because the ALSA arm is still worth driving.
    """
    from jasper.active_speaker.staging import prepare_driver_commissioning_config

    def call_site():
        prepare_driver_commissioning_config(
            topology,
            speaker_group_id="mono",
            role="woofer",
            playback_device=playback_device,
            config_dir=out_dir,
            run_config_check=False,
        )

    return call_site


def _recorded_emit_kwargs(
    monkeypatch, call_site, emitter="emit_active_speaker_baseline_config"
):
    """Run ``call_site``, returning the kwargs it handed ``emitter``.

    ``emitter`` names which emit function to record, because the candidate
    builder has TWO branches and they take the same device contract: the solo
    baseline emit and the wireless follower's driver-domain emit.
    """
    from jasper.active_speaker import camilla_yaml as cy

    seen: dict = {}
    real = getattr(cy, emitter)

    def recorder(preset, **kwargs):
        seen.update(kwargs)
        return real(preset, **kwargs)

    monkeypatch.setattr(cy, emitter, recorder)
    # baseline_profile and staging each imported their emitter by name at module
    # import, so those production seams have their own binding to rebind. Sites
    # whose import is function-local — crossover-v2's Stage 1 emit resolves the
    # attribute at call time — have no second binding, and neither module
    # imported their emitter at all. Rebinding what exists is not a weakening: a
    # site whose binding this MISSES records nothing, and the caller's walk then
    # reports every field missing rather than passing quietly.
    import jasper.active_speaker.baseline_profile as bp
    import jasper.active_speaker.staging as staging

    for module in (bp, staging):
        if hasattr(module, emitter):
            monkeypatch.setattr(module, emitter, recorder)
    call_site()
    return seen


def _ring_candidate_site(
    topology,
    tmp_path,
    *,
    driver_domain=False,
    playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
):
    """Drive ``build_baseline_profile_candidate``'s WRITE emit against the ring.

    The production CANDIDATE site (#2338), and the reason this guard grew past
    two entries: its sink is marker-aware (``resolve_active_playback_device``)
    while its capture lane and latency geometry took the emitter's ALSA-lane
    defaults — playback=ring meeting capture=tap, the same half-moved graph one
    emit site over. Both of its branches take the whole contract, so both are
    walked: the solo baseline emit and the wireless follower's driver-domain
    emit.

    ``playback_device`` is overridable ONLY so a caller can drive the SAME site
    at the ALSA active lane as a control — a ring-specific claim proven without
    one is a claim about the site, not about the ring.
    """
    from jasper.active_speaker.baseline_profile import build_baseline_profile_candidate
    from jasper.active_speaker.crossover_preview import build_crossover_preview
    from tests.active_speaker_fixtures import (
        standard_design_draft,
        standard_measurements,
        valid_camilla_config,
    )

    draft = standard_design_draft(topology)
    out = tmp_path / ("driver_domain" if driver_domain else "solo")

    def call_site():
        build_baseline_profile_candidate(
            topology,
            design_draft=draft,
            crossover_preview=build_crossover_preview(draft),
            measurements=standard_measurements(topology, out),
            write=True,
            state_path=out / "active-speaker-profile.json",
            config_path=out / "configs" / "active-speaker-baseline.yml",
            playback_device=playback_device,
            driver_domain=driver_domain,
            program_channel="left" if driver_domain else None,
            validate=valid_camilla_config,
        )

    return call_site


def _crossover_v2_program_site(
    topology,
    *,
    playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
):
    """Drive the session measurement graph's CHECK/MEASURE emit — issue #2450.

    Stage 1 of the crossover-v2 flow (the isolated per-driver sweeps that BUILD
    the crossover) is the ONLY phase group that emits its own graph: the four
    ``SUMMED_SWEEP_PHASES`` play into the box's already-active production graph,
    where no independently-specified capture lane exists to mismatch. So this
    site's sink was marker-aware (``resolve_active_playback_device``) while its
    capture lane, wire format and latency geometry took the emitter's ALSA-lane
    defaults — playback=ring meeting capture=tap, the fifth instance of the
    subset-forwarding class and the one that reached the DoD box.

    **The guard follows the site rather than dying with it**, twice now. Wave 6b
    moved the emit out of the per-stimulus body into the session's one
    measurement graph; the door PR moved it again, out of the web host's closure
    and into ``active_speaker.measurement_emit``, where the wizard and the
    ``jasper-measure`` door reach the SAME one. There is exactly one forwarding
    site left, so this drives it directly — no host, no transport, no
    monkeypatching of either. MS-1's blast radius is unchanged and still the
    widest of the seven: a half-derived device block poisons every stimulus of
    every session that emits through here.

    ``playback_device`` is overridable ONLY so a caller can drive the SAME site
    at the ALSA active lane as a control; a ring-specific claim proven without
    one is a claim about the site, not about the ring.
    """
    from jasper.active_speaker.measurement_emit import (
        MeasurementGraphProfile,
        emit_measurement_graph,
    )

    emitted: list[str] = []
    profile = MeasurementGraphProfile(
        preset=_mono_two_way_preset(),
        topology=topology,
        role_channels={"woofer": 0, "tweeter": 1},
        playback_device=playback_device,
    )

    def call_site():
        emitted.append(emit_measurement_graph(profile))

    call_site.emitted = emitted
    return call_site


def _crossover_v2_program_graph(topology, **kwargs) -> str:
    """The graph text the crossover-v2 CHECK/MEASURE site really emitted."""
    site = _crossover_v2_program_site(topology, **kwargs)
    site()
    assert len(site.emitted) == 1, (
        f"expected exactly one program emit, saw {len(site.emitted)}"
    )
    return site.emitted[0]


def test_the_crossover_v2_program_graph_follows_the_arm_in_both_directions(
    tmp_path, monkeypatch
):
    """Issue #2450: Stage 1's graph names BOTH ends of the transport it rides.

    The trap this closes is quiet by construction. On an armed box fan-in writes
    Ring A and stops feeding the snd-aloop tap, so a graph whose sink is the ring
    while its source is ``plug:jasper_capture`` captures a device nobody writes:
    the excitation WAV reaches the speaker (``correction_play_device`` is
    ring-aware) and CamillaDSP passes digital silence, with every daemon healthy
    and every existing gate satisfied — the capture-channel check compares 2 == 2
    and the arm's width gate only holds ring-NAMED lanes to the wire.

    BOTH branches of ``active_emit_devices``, because either half alone is a
    weaker claim than it reads as: ring-only would pass for a site that
    hard-codes the ring lane, and non-ring-only would pass for the defect
    itself.

    #2285 P2: the second half used to be reached by an UNARMED box, which is
    what its "inertness" framing meant. Nothing resolves the snd-aloop lane on
    its own any more — the ring is the one legal endpoint — so it is now reached
    only by an explicit lab/CI override, and what it still proves is that a
    NON-RING sink gets the emitter's own defaults untouched.
    """
    from tests.active_speaker_fixtures import mono_output_topology

    topology = mono_output_topology()
    wire = resolve_ring_wire(topology)

    # --- ARMED: playback resolves to the active ring. ----------------------
    ring_graph = _crossover_v2_program_graph(topology)
    ring = parse_camilla_devices_config(ring_graph)
    assert ring["playback_device"] == RING_ACTIVE_PLAYBACK_DEVICE
    assert ring["capture_device"] == RING_CAPTURE_DEVICE
    # ONE wire for both ends: the three rings share it, and a graph carrying the
    # box's program-lane default instead is a sheared attach waiting at the arm.
    assert ring["capture_format"] == wire.sample_format
    assert ring["playback_format"] == wire.sample_format
    assert ring["chunksize"] == RING_CAMILLA_CHUNKSIZE
    assert ring["target_level"] == RING_CAMILLA_TARGET_LEVEL
    # The queue pair has no parsed field, so it is read off the emitted text —
    # the same way the sibling ring-geometry pin above reads it.
    assert "  queuelimit: 1" in ring_graph
    assert "  enable_rate_adjust: false" in ring_graph

    # --- NON-RING: the SAME site at an explicitly-named ALSA lane. ---------
    # Not merely "capture is the emitter default": byte-identical to the emit
    # this site made before it derived anything, so a caller naming a non-ring
    # sink cannot notice the derivation at all.
    aloop_graph = _crossover_v2_program_graph(
        topology, playback_device=OUTPUTD_ACTIVE_PLAYBACK_DEVICE,
    )
    aloop = parse_camilla_devices_config(aloop_graph)
    assert aloop["playback_device"] == OUTPUTD_ACTIVE_PLAYBACK_DEVICE
    assert aloop["capture_device"] == DEFAULT_CAPTURE_DEVICE
    assert aloop["capture_format"] == DEFAULT_CAPTURE_FORMAT
    assert aloop_graph == active_camilla_yaml.emit_active_speaker_program_config(
        _mono_two_way_preset(),
        role_channels={"woofer": 0, "tweeter": 1},
        playback_device=OUTPUTD_ACTIVE_PLAYBACK_DEVICE,
    )


def test_ring_candidate_refuses_a_typod_wire_as_a_typed_config_error(
    tmp_path, monkeypatch
):
    """A typo'd ``JASPER_FANIN_RING_WIRE_FORMAT`` refuses as the TYPED class.

    This site derives its device block BEFORE it has an ``issues`` list to put a
    blocker in — the cached fast-return exits above that point, and the
    fingerprint has to describe the graph that will actually be emitted — so it
    refuses by raising, and the TYPE of the raise is the whole contract. The
    parser hands up a bare ``ValueError``
    (:func:`jasper.fanin_coupling.resolve_ring_wire_format`); what leaves this
    function must be ``ActiveSpeakerConfigError``, the class
    ``jasper.multiroom.follower_config`` catches on its way to
    ``fall_back_to_solo()``. That ``except`` names a SUBCLASS, so it does not
    catch the bare parent: with one, an armed + bonded box carrying one typo'd
    env line falls back to solo and logs
    ``multiroom.reconcile.active_follower_blocked``; without one it aborts the
    grouping reconcile inside the gate whose documented job is that fallback.

    Being a ``ValueError`` subclass, the typed class leaves every surface that
    already renders this as a clean refusal exactly as it was.
    """
    import jasper.active_speaker as active_speaker
    from jasper.fanin_coupling import RING_WIRE_FORMAT_ENV_VAR
    from tests.active_speaker_fixtures import mono_output_topology

    fanin_env = tmp_path / "fanin.env"
    fanin_env.write_text(f"{RING_WIRE_FORMAT_ENV_VAR}=s32le\n", encoding="utf-8")
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.FANIN_ENV_PATH", str(fanin_env)
    )

    topology = mono_output_topology()
    with pytest.raises(active_speaker.ActiveSpeakerConfigError) as caught:
        _ring_candidate_site(topology, tmp_path / "ring")()

    # NON-DEGENERATE: assert the raise really is the wire parser's, and that the
    # conversion carries its sentence rather than a type name. The operator has
    # to see the var and the token they typed.
    detail = str(caught.value)
    assert RING_WIRE_FORMAT_ENV_VAR in detail
    assert "s32le" in detail, "the operator needs to see the value they typed"

    # CONTROL: the same site, the same typo'd file, the ALSA active lane. The
    # wire is resolved only for a ring sink, so a typo cannot block an unarmed
    # box's ordinary candidate build — a conversion that refused every box would
    # satisfy the assertions above.
    _ring_candidate_site(
        topology,
        tmp_path / "alsa",
        playback_device=OUTPUTD_ACTIVE_PLAYBACK_DEVICE,
    )()


def test_every_emit_devices_field_reaches_the_emitter(tmp_path, monkeypatch):
    """WALK ``dataclasses.fields(ActiveEmitDevices)`` at EVERY forwarding site.

    This PR's whole defect class is a caller taking a SUBSET of a derived device
    contract — the ring re-emit forwarded the queue pair and let the format,
    latency geometry and capture lane default. Every site that forwards the
    contract now names every field explicitly, which is the readable shape but
    also the re-armable one: adding a field to ``ActiveEmitDevices`` and
    forgetting one call site fails silently in exactly the original way.

    So the guard is a WALK, not a list: it drives each site with a recording
    emitter and asserts that for EVERY field, the value that reached the emitter
    is the value the derivation produced. Same shape as
    ``test_every_active_lane_write_site_writes_the_pair`` — enumerate the sites,
    do not assert "the helper exists".

    Each entry names the DEVICE it is driven at, and the expectation is derived
    per site from that device rather than once for all of them. Every site runs
    at the ring, where every field's answer differs from the emitter's default —
    including the seventh, which could not until #2412's Wave 3 replaced
    ``prepare_driver_commissioning_config``'s blanket ring refusal with a
    both-ends coherence proof (see ``_driver_commissioning_site``).
    """
    import dataclasses

    from jasper.active_speaker.baseline_profile import recompose_applied_baseline_yaml
    from jasper.active_speaker.camilla_yaml import (
        ActiveEmitDevices,
        active_emit_devices,
    )

    fields = [f.name for f in dataclasses.fields(ActiveEmitDevices)]
    assert fields, "ActiveEmitDevices lost its fields; this guard is now vacuous"

    topology, applied = _applied_ring_baseline(tmp_path)

    sites = {
        "recompose_applied_baseline_yaml": (
            lambda: recompose_applied_baseline_yaml(
                topology,
                applied_profile=applied,
                playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
            ),
            "emit_active_speaker_baseline_config",
            RING_ACTIVE_PLAYBACK_DEVICE,
        ),
        "tests._emit_active_baseline": (
            lambda: _emit_active_baseline(
                _mono_two_way_preset(),
                RING_ACTIVE_PLAYBACK_DEVICE,
                topology=topology,
            ),
            "emit_active_speaker_baseline_config",
            RING_ACTIVE_PLAYBACK_DEVICE,
        ),
        "build_baseline_profile_candidate": (
            _ring_candidate_site(topology, tmp_path),
            "emit_active_speaker_baseline_config",
            RING_ACTIVE_PLAYBACK_DEVICE,
        ),
        "build_baseline_profile_candidate(driver_domain)": (
            _ring_candidate_site(topology, tmp_path, driver_domain=True),
            "emit_active_speaker_driver_domain_config",
            RING_ACTIVE_PLAYBACK_DEVICE,
        ),
        # Issue #2450 — the fifth instance, and the first outside the
        # baseline/driver-domain family: crossover-v2's Stage 1 emit. It is in
        # the walk for the same reason the others are, but it is also why the
        # walk had to stop being a family: this site lives in ``jasper.web``
        # and emits a DIFFERENT emitter, so nothing about the four entries
        # above could have covered it.
        #
        # Wave 6b moved the emit from the per-stimulus body to the session's
        # one measurement graph (PC-3); the door PR moved it out of the web
        # host into ``active_speaker.measurement_emit``, which two front ends
        # now share. Same emitter, same forwarding, one site — and a subset
        # poisons every stimulus of every session, so the entry stays and gets
        # stricter, not weaker.
        "measurement_emit.emit_measurement_graph": (
            _crossover_v2_program_site(topology),
            "emit_active_speaker_program_config",
            RING_ACTIVE_PLAYBACK_DEVICE,
        ),
        # Issue #2364 — the sixth instance, and the one the arm ladder walks
        # over: the all-muted durable BOOT anchor. It forwarded only the device
        # NAME, so a mid-commission box re-staged at the ring got a ring sink
        # over the snd-aloop tap in the artifact it BOOTS from. A third emitter
        # again, and a third module binding, which is why the recorder rebinds
        # `staging` too.
        "stage_protected_startup_config(boot anchor)": (
            _startup_anchor_site(topology, tmp_path / "anchor"),
            "emit_active_speaker_commissioning_config",
            RING_ACTIVE_PLAYBACK_DEVICE,
        ),
        # Issue #2412 — the seventh instance, and the anchor's own twin: the
        # AUDIBLE per-driver commissioning emit, in the same module, calling the
        # same emitter. It forwarded only the device NAME, so a caller re-pointed
        # at any ring PCM got a sink there over the snd-aloop tap. Driven at the
        # ring since Wave 3 lifted the refusal that kept it off — see its helper.
        "prepare_driver_commissioning_config(audible emit)": (
            _driver_commissioning_site(
                topology,
                tmp_path / "commission",
                playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
            ),
            "emit_active_speaker_commissioning_config",
            RING_ACTIVE_PLAYBACK_DEVICE,
        ),
    }
    for label, (call_site, emitter, device) in sites.items():
        expected = active_emit_devices(device, topology=topology)
        seen = _recorded_emit_kwargs(monkeypatch, call_site, emitter)
        missing = [name for name in fields if name not in seen]
        assert not missing, (
            f"{label} does not forward {missing} from ActiveEmitDevices — the "
            "subset-forwarding defect, re-armed"
        )
        for name in fields:
            assert seen[name] == getattr(expected, name), (
                f"{label} forwarded {name}={seen[name]!r}, but the derivation "
                f"says {getattr(expected, name)!r}"
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


def _outputd_env(
    *,
    marker: str | None,
    ring_path: str | None = None,
    content_bridge: str | None = None,
) -> str:
    lines = ["JASPER_OUTPUTD_SINK=single_alsa", "JASPER_OUTPUTD_ACTIVE_LANE=1"]
    if marker is not None:
        lines.append(f"{OUTPUTD_RING_ACTIVE_ENDPOINT_ENV_VAR}={marker}")
    if ring_path is not None:
        lines.append(f"JASPER_OUTPUTD_SHM_RING_PATH={ring_path}")
    if content_bridge is not None:
        # Ring A and the post-DSP ring are ONE coupling, so a box on the
        # ``shm_ring`` coupling always carries the matching bridge. Modelling a
        # ring-coupled box without it describes a state no writer produces.
        lines.append(f"JASPER_OUTPUTD_CONTENT_BRIDGE={content_bridge}")
    return "\n".join(lines) + "\n"


def _ring_path_written(actions):
    from jasper.fanin_coupling import OUTPUTD_RING_PATH_ENV_VAR

    for action in actions:
        if action.action == "set" and action.key == OUTPUTD_RING_PATH_ENV_VAR:
            return action.value
    return None


def test_the_arm_sequence_completes_from_an_unarmed_roleful_box(monkeypatch):
    """ARM, walked from the state a real box is in: marker ABSENT.

    #2285 P2 inverted this test's opening. It used to EXECUTE the fixed point
    the panel proved — with no marker the chooser answered the ALSA lane, a
    re-emit reproduced it, and the marker derived absent again, three iterations
    with no movement, so nothing but an explicit ``--endpoint`` could arm a box.
    Deleting the chooser's marker branch dissolved that loop, so the same three
    iterations are kept and run to the OPPOSITE conclusion: they CONVERGE. The
    loop is the evidence either way; only its verdict moved.

    ``--endpoint ring`` then remains the explicit rung, and the rest of the
    ladder follows it.
    """
    from jasper.cli.active_speaker import _baseline_reemit_endpoint
    from jasper.fanin.coupling_reconcile import _outputd_actions
    from jasper.output_topology import resolve_output_layout

    topology = _active_topology("mono", "active_2_way")
    preset = _mono_two_way_preset()

    # --- The convergence, executed rather than asserted. ------------------
    # The marker is stubbed CLEAR throughout: an auto-resolving re-emit still
    # reaches the ring and still derives the marker SET, which is only true
    # because the chooser stopped reading it.
    monkeypatch.setattr(
        "jasper.fanin_coupling.ring_active_endpoint_armed", lambda env=None: False
    )
    device = resolve_output_layout(topology, env={}).playback_device
    for _ in range(3):
        graph = _emit_active_baseline(preset, device, topology=topology)
        assert _derived_marker(graph, topology) == RING_ACTIVE_PLAYBACK_DEVICE
        device = resolve_output_layout(topology, env={}).playback_device
        assert device == RING_ACTIVE_PLAYBACK_DEVICE, (
            "an auto-resolving re-emit must reach the ring with no marker set — "
            "otherwise the roleful path is manual again"
        )

    # --- Step 1: baseline-reemit --endpoint ring. -------------------------
    armed_device, source = _baseline_reemit_endpoint(topology, "ring")
    assert armed_device == RING_ACTIVE_PLAYBACK_DEVICE
    assert source == "explicit_endpoint_ring"
    ring_graph = _emit_active_baseline(preset, armed_device, topology=topology)
    assert f'device: "{RING_ACTIVE_PLAYBACK_DEVICE}"' in ring_graph
    # The ring's own queue handshake rode along, because the emit went through
    # the shared resolver rather than each caller remembering.
    assert "  queuelimit: 1" in ring_graph
    assert "  enable_rate_adjust: false" in ring_graph
    # ...and so did the rest of the sink's contract. Step 1 is the rung that
    # DECLARES the wire, so what it writes has to be the resolver's answer and
    # the certified ring geometry — not the box's program-lane default and its
    # loopback LatencyFloor, which is what shipped and sheared on jts3.
    wire = resolve_ring_wire(topology)
    assert f"    format: {wire.sample_format}" in ring_graph
    assert f"  chunksize: {RING_CAMILLA_CHUNKSIZE}" in ring_graph
    assert f"  target_level: {RING_CAMILLA_TARGET_LEVEL}" in ring_graph
    # ...INCLUDING the capture half. The coupling is end-to-end: under shm_ring
    # fan-in writes Ring A and stops feeding the snd-aloop tap, so a graph that
    # moved only its sink captures a device nobody writes — silence with every
    # daemon healthy. Both lanes move on the same rung or neither does.
    ring_devices = parse_camilla_devices_config(ring_graph)
    assert ring_devices["capture_device"] == RING_CAPTURE_DEVICE
    assert ring_devices["capture_format"] == wire.sample_format
    assert ring_devices["playback_device"] == RING_ACTIVE_PLAYBACK_DEVICE

    # --- Step 2: the hardware reconciler derives the marker FROM that graph.
    assert _derived_marker(ring_graph, topology) == RING_ACTIVE_PLAYBACK_DEVICE

    # --- Step 3: the coupling reconciler converges the ring PATH. ---------
    # The unarmed stub is dropped first: from here the marker is a REAL value
    # the previous step wrote into outputd.env, and step 3 must read that file
    # rather than a predicate this test is holding down.
    monkeypatch.undo()
    actions = _outputd_actions(_outputd_env(marker="1"))
    assert _ring_path_written(actions) == DEFAULT_OUTPUTD_ACTIVE_RING_PATH


def _coherence_errors(*, coupling, capture, playback, outputd_env=None):
    from jasper.transport_coherence import transport_coherence_report

    env = {
        "JASPER_OUTPUTD_ACTIVE_LANE": "1",
        "JASPER_OUTPUTD_CONTENT_PCM": "outputd_active_content_capture",
        **(outputd_env or {}),
    }
    return transport_coherence_report(
        coupling=coupling,
        outputd_env=env,
        camilla_devices={
            "capture_device": capture,
            "capture_channels": 2,
            "playback_device": playback,
            "playback_channels": 2,
        },
    ).errors


def test_the_capture_device_comparison_names_the_quiet_trap_not_every_graph():
    """DEFENSE IN DEPTH for the quiet trap: a ring plan whose graph still sources
    the snd-aloop tap is named, not silently accepted.

    The trap is quiet precisely because the axes that DO get compared cannot see
    it: the plan compares capture CHANNELS and Ring A is stereo exactly like the
    snd-aloop tap, and the arm's width gate only holds ring-NAMED lanes to the
    wire, so a tap capture is not-inspected rather than refused. Only the DEVICE
    comparison catches it.

    The MIRROR of this — a non-ring plan whose graph captures Ring A — is no
    longer a contradiction and no longer checked: under ADR-0100 fan-in serves
    Ring A or parks, so a Ring A capture is the correct half whatever the
    persisted token says.
    """
    armed_env = {
        "JASPER_OUTPUTD_CONTENT_BRIDGE": "shm_ring",
        OUTPUTD_RING_ACTIVE_ENDPOINT_ENV_VAR: "1",
        "JASPER_OUTPUTD_SHM_RING_PATH": DEFAULT_OUTPUTD_ACTIVE_RING_PATH,
    }

    armed = _coherence_errors(
        coupling="shm_ring",
        capture=RETIRED_ALOOP_CAPTURE_DEVICE,
        playback=RING_ACTIVE_PLAYBACK_DEVICE,
        outputd_env=armed_env,
    )
    assert any(
        "shm_ring but Camilla capture" in err and RING_CAPTURE_DEVICE in err
        for err in armed
    ), armed

    # CONTROL 1 — the mid-arm WAYPOINT is not an error. Both halves have moved
    # while the coupling is still loopback; that is the state step 1 exists to
    # create, and calling it an error deadlocks the ladder from the capture side
    # exactly as the playback side deadlocked it before #2329.
    assert _coherence_errors(
        coupling="loopback",
        capture=RING_CAPTURE_DEVICE,
        playback=RING_ACTIVE_PLAYBACK_DEVICE,
    ) == ()

    # CONTROL 2 — the fully armed ring pair is clean, on both capture halves a
    # box can present.
    assert _coherence_errors(
        coupling="shm_ring",
        capture=RING_CAPTURE_DEVICE,
        playback=RING_ACTIVE_PLAYBACK_DEVICE,
        outputd_env=armed_env,
    ) == ()

    # CONTROL 3 — the retired snd-aloop pair, on BOTH capture halves a box can
    # present it with. The Ring A half is the shape the retired HALF-moved-graph
    # guard called an error and this comparison must not: fan-in serves Ring A
    # whatever the persisted token says (ADR-0100), so a Ring A capture is the
    # correct half and only the PLAYBACK side of this pair is retired.
    from jasper.camilla_config_contract import (
        DEFAULT_OUTPUTD_CAPTURE_DEVICE,
        RETIRED_ALOOP_PLAYBACK_DEVICE,
    )

    for capture_half in (RETIRED_ALOOP_CAPTURE_DEVICE, RING_CAPTURE_DEVICE):
        assert _coherence_errors(
            coupling="loopback",
            capture=capture_half,
            playback=RETIRED_ALOOP_PLAYBACK_DEVICE,
            outputd_env={"JASPER_OUTPUTD_CONTENT_PCM": DEFAULT_OUTPUTD_CAPTURE_DEVICE},
        ) == (), capture_half


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
    from jasper.fanin.coupling_reconcile import _outputd_actions

    coherent = _outputd_actions(_outputd_env(marker="1"))
    assert _ring_path_written(coherent) == DEFAULT_OUTPUTD_ACTIVE_RING_PATH

    # The state the blocker described — arming an UNMARKED box — cannot name the
    # active ring at all, so it cannot produce the crossed pair that made outputd
    # admit-then-park. It resolves the stereo ring, which a roleful graph never
    # names, so the box lands on silence rather than on a full-range program
    # reaching a compression driver.
    unmarked = _outputd_actions(_outputd_env(marker=""))
    assert _ring_path_written(unmarked) != DEFAULT_OUTPUTD_ACTIVE_RING_PATH


def _run_validate_outputd_env(
    tmp_path,
    capsys,
    *,
    graph_yaml: str,
    topology,
    coupling: str,
    marker: str | None,
    ring_path: str | None = None,
    content_bridge: str | None = None,
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
    outputd_env.write_text(
        _outputd_env(
            marker=marker, ring_path=ring_path, content_bridge=content_bridge
        ),
        encoding="utf-8",
    )
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


def test_the_convergence_walk_clears_the_validator_the_reconciler_actually_runs(
    tmp_path, capsys, monkeypatch
):
    """The missing layer: the walk goes THROUGH `validate-outputd-env`.

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

    THE SECOND MISSING LAYER (2026-08-11, attempt 2). Clearing the validator is
    not the whole of step 3: ``ring_edge_width_ready`` is the preflight that runs
    inside the coupling reconciler, and on jts3 it returned READY over a graph
    declaring ``S32_LE`` against a resolver answering ``S16_LE``
    (``captures/r7b-jts3-arm2-20260811T132227Z``, file 13). This walk now asserts
    both halves of that rung — that step 1 emits the RESOLVED wire, and that the
    gate inspects the emitted graph rather than reporting agreement it never
    checked.
    """
    from jasper.cli.active_speaker import _baseline_reemit_endpoint
    from jasper.fanin.coupling_reconcile import _outputd_actions
    from jasper.fanin.ring_health import (
        LoadedCamillaGraph,
        ring_edge_width_ready,
    )

    topology = _active_topology("mono", "active_2_way")
    preset = _mono_two_way_preset()

    # --- Step 1: the operator names the endpoint; the graph moves first. ---
    armed_device, _source = _baseline_reemit_endpoint(topology, "ring")
    assert armed_device == RING_ACTIVE_PLAYBACK_DEVICE
    ring_graph = _emit_active_baseline(preset, armed_device, topology=topology)
    # What step 1 DECLARES is the resolver's answer, not the box's program-lane
    # default — the shear that halted attempt 2 one rung later.
    wire = resolve_ring_wire(topology)
    assert f"    format: {wire.sample_format}" in ring_graph

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
    # The note names the state and the ONE exit — a box can be left here.
    # (#2285 P2: it used to name a rollback exit too. That endpoint is retired,
    # so the note says so instead of printing a command argparse rejects.)
    assert "goes silent at the next CamillaDSP load" in out, out
    assert "jasper-fanin-coupling-reconcile shm_ring" in out, out
    assert "no rollback direction" in out, out
    assert "--endpoint aloop" not in out, out

    # --- Step 3, preflight: the width gate reads the graph step 1 wrote. ---
    # The gate resolves the wire from the box's SAVED topology, so this walk
    # hands it the same roleful topology the emit used — otherwise the box under
    # test has no active-ring width and the gate would be answering about a
    # different speaker.
    import jasper.ring_assets as ring_assets_module
    from jasper.camilla_config_contract import parse_camilla_devices_config

    monkeypatch.setattr(
        "jasper.fanin.ring_health.load_topology_for_wire", lambda: topology
    )
    monkeypatch.setattr(ring_assets_module, "RING_CONF_D", str(RING_CONF))

    ok, detail = ring_edge_width_ready(
        fanin_text="",
        outputd_text="",
        graph=LoadedCamillaGraph(
            path="step1-artifact.yml",
            devices=parse_camilla_devices_config(ring_graph),
        ),
    )
    assert ok is True, detail
    # It COUNTED the graph, and says so. A message that claimed agreement
    # without inspecting this end is the defect, not the wording.
    assert f"loaded CamillaDSP graph (playback {RING_ACTIVE_PLAYBACK_DEVICE})" in detail
    assert "was NOT one of them" not in detail

    # --- Step 3: with step 2's marker on disk, the path converges. ---------
    actions = _outputd_actions(_outputd_env(marker="1"))
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


def _first_arm_on_a_stereo_ring_box(tmp_path, capsys, monkeypatch, *, graph_yaml=None):
    """Drive the validator over the FIRST-ARM state of a previously-stereo box.

    The distinguishing input is the persisted COUPLING: ``shm_ring``, not
    ``loopback``. A box that already ran the full-range stereo ring resolves the
    ACTIVE-ring shape the instant the marker arms, while the ring PATH — written
    by the OTHER reconciler, which runs after — still carries Ring B's file. The
    ladder walk above cannot reach this state, because on a loopback box the plan
    stays ``loopback`` until step 3 moves the coupling and the path together.

    ``load_topology_for_wire`` is pinned to the topology under test: the ACTIVE
    ring's width is the one per-topology axis of that shape, so a box with no
    saved topology would be answering about a different speaker.
    """

    topology = _active_topology("mono", "active_2_way")
    monkeypatch.setattr(
        "jasper.fanin.ring_health.load_topology_for_wire", lambda: topology
    )
    if graph_yaml is None:
        graph_yaml = _emit_active_baseline(
            _mono_two_way_preset(), RING_ACTIVE_PLAYBACK_DEVICE, topology=topology
        )
    return _run_validate_outputd_env(
        tmp_path,
        capsys,
        graph_yaml=graph_yaml,
        topology=topology,
        coupling="shm_ring",
        marker="1",
        ring_path=DEFAULT_OUTPUTD_RING_PATH,
        content_bridge="shm_ring",
    )


def test_the_first_arm_of_a_box_already_on_the_stereo_ring_clears_the_validator(
    tmp_path, capsys, monkeypatch
):
    """The jts.local deadlock (2026-08-21), walked through the REAL validator.

    Loading a valid roleful startup graph on a previously-passive dual-Apple
    composite wedged two reconcilers against each other. The marker's writer
    (``jasper-audio-hardware-reconcile``) validates its own candidate here,
    BEFORE the path's writer has run; the candidate is the live ``outputd.env``
    copied forward plus the newly-armed marker, so it carried Ring B's path. The
    validator refused, the reconciler exited 78, and ``jasper-camilla``'s
    ``Requires=`` on that unit took the DSP graph down — which also disabled the
    box's own rollback, because rollback needs the websocket of the daemon the
    refusal had just killed.

    Neither half could move first: the path is DERIVED from the marker
    (``_outputd_ring_path_for``) and the marker was gated on the path. This
    asserts the loop is open — the candidate validates, so the marker lands —
    and then that the very next pass of the path's own writer converges the pair.
    """
    rc, out = _first_arm_on_a_stereo_ring_box(tmp_path, capsys, monkeypatch)

    assert rc == 0, out
    assert out.startswith("ok note="), out
    assert "FIRST-ARM waypoint" in out, out
    # The note names the state, why it is silence rather than wrong audio, and
    # the one command that converges it. An operator standing here reads this
    # line out of the journal as event=audio_hardware_reconcile.outputd_env_note.
    assert DEFAULT_OUTPUTD_RING_PATH in out, out
    assert "outputd refuses the pair" in out, out
    assert "jasper-fanin-coupling-reconcile shm_ring" in out, out

    # ...and the pass that note names actually converges the pair, from the
    # marker this validation just allowed to be written.
    from jasper.fanin.coupling_reconcile import _outputd_actions

    actions = _outputd_actions(_outputd_env(marker="1", ring_path=DEFAULT_OUTPUTD_RING_PATH)
    )
    assert _ring_path_written(actions) == DEFAULT_OUTPUTD_ACTIVE_RING_PATH


def test_a_crossed_ring_pair_over_a_graph_off_the_active_ring_still_fails(
    tmp_path, capsys, monkeypatch
):
    """The crossed-pair refusal is scoped, not removed.

    Same armed marker and same stale Ring B path as the first-arm walk above —
    the ONE difference is the loaded graph, which here plays the full-range
    stereo ring instead of the active one. That is not a rung of any ladder: a
    first arm loads the roleful graph BEFORE the marker is derived from it, so a
    marker armed over a graph that is not on the active ring means nothing is
    writing the ring outputd would be pointed at, and converging the path would
    aim outputd at a ring with no writer.

    This is why the first-arm note is safe to proceed on: it never stands alone
    on a wrecked box. The device comparison in the same branch still returns an
    ERROR, and the reconciler still refuses.
    """
    stereo_ring_graph = (
        "devices:\n"
        "  samplerate: 48000\n"
        "  channels: 2\n"
        "  capture:\n"
        "    type: Alsa\n"
        "    device: jts_ring_capture\n"
        "  playback:\n"
        "    type: Alsa\n"
        f"    device: {RING_PLAYBACK_DEVICE}\n"
    )

    rc, out = _first_arm_on_a_stereo_ring_box(
        tmp_path, capsys, monkeypatch, graph_yaml=stereo_ring_graph
    )

    assert rc == 1, out
    assert RING_PLAYBACK_DEVICE in out, out
    assert RING_ACTIVE_PLAYBACK_DEVICE in out, out
    # A refusal, not a note dressed up as one: the caller keys on the exit code,
    # and `ok note=` is what tells it to proceed.
    assert not out.startswith("ok note="), out


def test_the_unarmed_ring_path_projection_never_carries_the_active_file_forward():
    """The DISARM direction of the same pair, which used to be sticky.

    ``_outputd_ring_path_for`` is the pair's only derivation, and the biconditional
    it serves runs both ways: the active ring file may be read only by an armed
    endpoint, exactly as an armed endpoint may read only that file. Preserving
    whatever the key held on the unarmed side meant a box whose marker was
    cleared while the coupling stayed ``shm_ring`` — the active-lane decision
    losing its hardware proof — kept pointing outputd at a ring whose writer had
    just been stood down, and every later pass preserved it again. Nothing
    converged, so the crossed pair became permanent in that direction.

    Falling back to Ring B's default is what makes the projection TOTAL: whichever
    half moved last, ONE pass of this reconciler converges the other. That is the
    recovery path for an interrupted transition, and it is why the waypoint above
    can be a note rather than a refusal.
    """
    from jasper.fanin.coupling_reconcile import _outputd_actions

    stuck = _outputd_actions(_outputd_env(marker="", ring_path=DEFAULT_OUTPUTD_ACTIVE_RING_PATH),
    )
    assert _ring_path_written(stuck) == DEFAULT_OUTPUTD_RING_PATH

    # An operator's own Ring B path is still honoured — the fallback is scoped to
    # the ONE file the allowlist reserves, not to every non-default value.
    custom = _outputd_actions(_outputd_env(marker="", ring_path="/dev/shm/operator.ring")
    )
    assert _ring_path_written(custom) == "/dev/shm/operator.ring"

    # ...and the armed side is unchanged: it discards whatever the key held.
    armed = _outputd_actions(_outputd_env(marker="1", ring_path="/dev/shm/operator.ring"),
    )
    assert _ring_path_written(armed) == DEFAULT_OUTPUTD_ACTIVE_RING_PATH


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


def test_the_retired_aloop_endpoint_is_refused_by_the_parser_itself(capsys):
    """#2285 P2's ONE negative guard for the retired ``--endpoint aloop``.

    Nine ``baseline-reemit`` tests used to carry a matched ``["ring", "aloop"]``
    parametrize, so the rollback direction was re-proved nine times. It is now
    refused once, and refused EARLY — ``choices=("ring",)`` means argparse
    rejects it before any of that machinery runs, which is why exit 2 (argparse
    usage error) rather than the command's own 1 is the assertion that matters:
    a 1 would mean the command accepted the flag and then failed somewhere.

    The parser is driven through ``main`` rather than ``build_parser`` because
    ``main`` is what an operator and every remediation string invoke.
    """
    from jasper.cli.active_speaker import main

    with pytest.raises(SystemExit) as excinfo:
        main(["baseline-reemit", "--endpoint", "aloop"])
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "invalid choice: 'aloop'" in err, err

    # NON-VACUITY: the surviving choice really does get past the parser. Without
    # this, a parser that rejected EVERY endpoint would satisfy the assertion
    # above. `--statefile` points nowhere on purpose; reaching a non-2 exit is
    # the whole claim.
    assert main([
        "baseline-reemit", "--endpoint", "ring", "--statefile", "/nonexistent/x.yml"
    ]) != 2


def test_an_unrecognised_explicit_endpoint_raises_rather_than_auto_resolving():
    """DEFENCE IN DEPTH behind the parser: the resolver refuses what it cannot name.

    ``_baseline_reemit_endpoint`` returns ``(device, provenance)``, and an
    endpoint string it does not recognise must NOT fall through to the
    auto-resolver. Falling through would answer the ring anyway and report the
    provenance as AUTO — so a caller asking for a retired endpoint would be told
    it got what it asked for, on a transport it did not choose. The wrong answer
    is silent by construction, which is why a raise rather than a log.

    ``choices=("ring",)`` is the only entry point today, so this guards the
    NEXT one: a wizard, a script, or a follow-up command calling the resolver
    directly has no parser in front of it.
    """
    from jasper.cli.active_speaker import _baseline_reemit_endpoint

    topology = _active_topology("mono", "active_2_way")

    with pytest.raises(ValueError) as excinfo:
        _baseline_reemit_endpoint(topology, "bogus")
    # It names what it was given and what it will accept, so the caller can fix
    # the call from the message alone.
    assert "bogus" in str(excinfo.value)
    assert RING_ACTIVE_PLAYBACK_DEVICE in str(excinfo.value)

    # The retired endpoint reaches the same refusal, not a quiet auto answer.
    with pytest.raises(ValueError):
        _baseline_reemit_endpoint(topology, "aloop")

    # CONTROLS. `ring` is explicit and says so; omitting it is the auto answer
    # and says THAT — the provenance split is the thing a fall-through erases.
    assert _baseline_reemit_endpoint(topology, "ring") == (
        RING_ACTIVE_PLAYBACK_DEVICE,
        "explicit_endpoint_ring",
    )
    device, source = _baseline_reemit_endpoint(topology, None)
    assert device == RING_ACTIVE_PLAYBACK_DEVICE
    assert source != "explicit_endpoint_ring"


def test_baseline_reemit_writes_the_live_artifact_and_repoints_the_statefile(
    monkeypatch, tmp_path
):
    """The command that four surfaces said completed the arm now actually does.

    It wrote NOTHING by default before this round — no --out meant "emit and
    report" — so every claim that re-emitting closes the stale-artifact gap was
    false. The resolved device is asserted, not just the write: a mode that
    silently fell back to the auto answer would reintroduce the fixed point.
    """
    from jasper.cli.active_speaker import main

    h = _reemit_harness(monkeypatch, tmp_path)
    code = main([
        "baseline-reemit",
        "--endpoint", "ring",
        "--statefile", str(h.statefile),
    ])
    assert code == 0
    assert h.seen["playback_device"] == RING_ACTIVE_PLAYBACK_DEVICE
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
    from jasper.fanin_coupling import TRANSPORT_SHM_RING_ACTIVE
    from jasper.transport_coherence import (
        transport_coherence_report,
        transport_topology_for_coupling,
    )
    from jasper.fanin.coupling_reconcile import _outputd_actions
    from jasper.fanin_coupling import (
        COUPLING_SHM_RING,
        DEFAULT_OUTPUTD_RING_PATH,
        OUTPUTD_RING_PATH_ENV_VAR,
    )

    # Hostile input: the file already names the STEREO ring while the marker
    # says armed. The old code preserved that value verbatim.
    hostile = _outputd_env(marker="1", ring_path=DEFAULT_OUTPUTD_RING_PATH)
    actions = _outputd_actions(hostile)
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
    errors = transport_coherence_report(
        coupling=COUPLING_SHM_RING,
        outputd_env=converged,
        camilla_devices={"playback_device": RING_ACTIVE_PLAYBACK_DEVICE},
    ).errors
    assert not [e for e in errors if "may read only" in e], errors

    # ...and the CROSSED pair the reconciler can no longer produce IS still
    # reported by that twin — a positive control, so the clean result above is
    # evidence the crossing is absent rather than evidence the check is inert.
    # It is reported as the FIRST-ARM waypoint rather than as a refusal: the
    # comparison is the same one, and only its disposition moved, because a
    # refusal there blocks the writer that converges the pair.
    crossed = dict(converged, **{OUTPUTD_RING_PATH_ENV_VAR: DEFAULT_OUTPUTD_RING_PATH})
    crossed_report = transport_coherence_report(
        coupling=COUPLING_SHM_RING,
        outputd_env=crossed,
        camilla_devices={"playback_device": RING_ACTIVE_PLAYBACK_DEVICE},
    )
    assert [n for n in crossed_report.notes if "FIRST-ARM waypoint" in n], crossed_report
    assert crossed_report.errors == (), crossed_report


# --------------------------------------------------------------------------
# 13. STEP 1 ON A BOX WITH NO APPLIED BASELINE (#2285, #2364).
#
# `baseline-reemit` refused outright without an APPLIED baseline. But the
# fleet-typical composite box (jts.local today, jts5 after re-fit) is
# mid-commission BY DESIGN: its boot graph is the all-muted staged startup
# ANCHOR, not a baseline. Every layer BELOW step 1 already admits that shape —
# `GRAPH_ALL_MUTED_ACTIVE_STARTUP` is a legal outputd endpoint class,
# `jts_ring_active_playback` is a legal endpoint device, and
# `active_ring_endpoint_proof` reads only the marker and the conf.d — so the
# refusal was the ONLY thing standing between that box and the arm ladder.
#
# These drive the real command against a real staged artifact and a real
# statefile. The re-stage, the re-proof, the publish and the repoint all run;
# only the box's persisted design inputs are supplied by the fixture.
# --------------------------------------------------------------------------


def _anchor_reemit_harness(
    monkeypatch,
    tmp_path,
    *,
    applied=None,
    decision_status=None,
    reproof=None,
    commission_loaded=False,
):
    """Stage `baseline-reemit` on a box whose boot graph is the startup anchor.

    Real staging, real classification, real publish. The safe-graph decision is
    the one thing stubbed: it reads a box's whole persisted evidence set, has its
    own tests, and what THIS command owns is which class it accepts and what it
    does about it.
    """
    from types import SimpleNamespace

    from jasper.active_speaker.runtime_contract import (
        GRAPH_ALL_MUTED_ACTIVE_STARTUP,
        GRAPH_DRIVER_DOMAIN_BASELINE,
        GRAPH_PARKED_ALL_MUTED,
        GraphSafety,
    )
    from jasper.active_speaker import startup_load
    from jasper.active_speaker.crossover_preview import build_crossover_preview
    from jasper.cli import active_speaker as cli
    from tests.active_speaker_fixtures import (
        mono_output_topology,
        standard_design_draft,
    )

    topology = mono_output_topology()
    draft = standard_design_draft(topology)

    live_dir = tmp_path / "live"
    live_dir.mkdir(parents=True, exist_ok=True)
    live_config = live_dir / "active_speaker_staged_startup.yml"
    live_config.write_text("stale: true\n", encoding="utf-8")
    live_meta = live_dir / "active_speaker_staged_config.json"
    live_meta.write_text('{"status": "stale"}', encoding="utf-8")

    statefile = tmp_path / "outputd-statefile.yml"
    statefile.write_text("config_path: /somewhere/else.yml\n", encoding="utf-8")

    monkeypatch.setattr(cli, "load_output_topology_strict", lambda *a, **k: topology)
    monkeypatch.setattr(
        "jasper.active_speaker.baseline_profile.load_applied_baseline_profile_state",
        lambda *a, **k: applied or {},
    )
    monkeypatch.setattr(
        "jasper.active_speaker.design_draft.load_design_draft", lambda *a, **k: draft
    )
    # The box's persisted preview. Built from the SAME saved draft the real
    # loader would read it against, so the command's "derive from persisted
    # state only" contract is exercised rather than bypassed.
    monkeypatch.setattr(
        "jasper.active_speaker.crossover_preview.load_crossover_preview",
        lambda *a, **k: build_crossover_preview(draft),
    )
    # Redirect only the DEFAULT (no-argument) answer — "where does this box keep
    # its live anchor". A call that explicitly names a directory or path still
    # goes where it was directed, which is what keeps the command's own
    # stage-into-a-temp-dir-first step actually isolated. A blunter stub that
    # ignored its arguments would send the proof run straight at the live
    # artifact and make the preview test below vacuous.
    from jasper.active_speaker import staging as staging_mod

    real_config_path = staging_mod.staged_config_path
    real_metadata_path = staging_mod.staged_metadata_path

    def _staged_config_path(*, config_dir=None, path=None):
        if config_dir is None and path is None:
            return live_config
        return real_config_path(config_dir=config_dir, path=path)

    def _staged_metadata_path(path=None):
        return live_meta if path is None else real_metadata_path(path)

    monkeypatch.setattr(staging_mod, "staged_config_path", _staged_config_path)
    monkeypatch.setattr(staging_mod, "staged_metadata_path", _staged_metadata_path)

    # Commission-load state is stubbed in BOTH directions on purpose: the live
    # default path would otherwise decide this test's outcome from whatever the
    # dev machine happens to have on disk.
    monkeypatch.setattr(
        "jasper.active_speaker.commission_load.load_commission_load_state",
        lambda *a, **k: (
            {"status": "loaded", "target": "mono/tweeter",
             "candidate_config_path": "/var/lib/camilladsp/configs/commissioning.yml"}
            if commission_loaded
            else {}
        ),
    )

    # (safe-graph status, classification, which slot carries it). The last two
    # are the DISCRIMINATOR cases: `preserve_current` is also how an approved
    # runtime graph and a driver-domain baseline are preserved, so a check keyed
    # on the status alone would accept them and re-stage an all-muted anchor over
    # a commissioned box.
    shapes = {
        None: ("select_active_startup", GRAPH_ALL_MUTED_ACTIVE_STARTUP, "fallback"),
        "parked": ("parked_muted", GRAPH_PARKED_ALL_MUTED, "fallback"),
        "preserve_approved": (
            "preserve_current", GRAPH_APPROVED_ACTIVE_RUNTIME, "current",
        ),
        "preserve_driver_domain": (
            "preserve_current", GRAPH_DRIVER_DOMAIN_BASELINE, "current",
        ),
    }
    status, classification, slot = shapes[decision_status]
    graph = GraphSafety(classification=classification, allowed=True, issues=())
    decision = SimpleNamespace(
        status=status,
        selected_config_path=None,
        current_graph=graph if slot == "current" else None,
        preferred_graph=None,
        fallback_graph=graph if slot == "fallback" else None,
        issues=(),
    )

    # Stub ONLY the pre-check — "what is this box booting from?" — which reads a
    # whole persisted evidence set this fixture does not build. The command's
    # RE-PROOF calls the same selector with an explicit `current_config_path`
    # (the freshly-staged bytes), and that call is passed through to the real
    # function: the re-proof is the fail-closure claim these tests exist to
    # check, so stubbing it would make them prove nothing.
    real_safe_graph = cli.safe_graph_for_current_topology

    unsafe_reproof = SimpleNamespace(
        status="blocked",
        selected_config_path=None,
        current_graph=GraphSafety(
            classification="unsafe",
            allowed=False,
            issues=({"severity": "blocker", "code": "x", "message": "nope"},),
        ),
        preferred_graph=None,
        fallback_graph=None,
        issues=({"severity": "blocker", "code": "x", "message": "nope"},),
    )

    def _safe_graph(topology_arg, **kwargs):
        if kwargs.get("current_config_path") is None:
            return decision
        if reproof == "unsafe":
            return unsafe_reproof
        return real_safe_graph(topology_arg, **kwargs)

    # Both bindings: the CLI runs the pre-check, the engine runs the re-proof.
    monkeypatch.setattr(cli, "safe_graph_for_current_topology", _safe_graph)
    monkeypatch.setattr(
        startup_load, "safe_graph_for_current_topology", _safe_graph
    )

    return SimpleNamespace(
        topology=topology,
        live_config=live_config,
        live_meta=live_meta,
        statefile=statefile,
    )


def test_reemit_staged_startup_anchor_reports_facts_and_prints_nothing(
    monkeypatch, tmp_path, capsys
):
    """The engine hands back a report; the operator text is the CLI's (row 7.4).

    Asserted on the report's own fields rather than on printed lines, in both
    directions — a re-emit and a refusal — because a pin on the text would put
    the surface concern back into the engine that this boundary took out of it.
    """
    from jasper.active_speaker.runtime_contract import GRAPH_ALL_MUTED_ACTIVE_STARTUP
    from jasper.active_speaker.startup_load import reemit_staged_startup_anchor

    h = _anchor_reemit_harness(monkeypatch, tmp_path)
    done = reemit_staged_startup_anchor(
        h.topology,
        device=RING_ACTIVE_PLAYBACK_DEVICE,
        source="explicit_endpoint_ring",
        statefile=h.statefile,
    )
    assert done.reason is None, done
    assert done.classification == GRAPH_ALL_MUTED_ACTIVE_STARTUP, done
    assert done.written_path == h.live_config, done
    assert (done.device, done.source) == (
        RING_ACTIVE_PLAYBACK_DEVICE, "explicit_endpoint_ring",
    ), done
    assert done.preview is False and done.statefile_written is True, done
    assert done.byte_count == len(h.live_config.read_text(encoding="utf-8")), done

    refused = _anchor_reemit_harness(monkeypatch, tmp_path, commission_loaded=True)
    stopped = reemit_staged_startup_anchor(
        refused.topology,
        device=RING_ACTIVE_PLAYBACK_DEVICE,
        source="explicit_endpoint_ring",
        statefile=refused.statefile,
    )
    assert stopped.reason == "commission_load_active", stopped
    assert stopped.active_target == "mono/tweeter", stopped
    assert stopped.written_path is None and stopped.byte_count == 0, stopped

    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", ""), captured


def test_baseline_reemit_restages_the_startup_anchor_at_the_ring(
    monkeypatch, tmp_path
):
    """A mid-commission box gets step 1.

    BOTH device halves are asserted, because the whole failure mode this ladder
    step exists to prevent is a graph that moves its sink and leaves its source
    behind — a ring sink over the snd-aloop tap is silence with every daemon
    reporting healthy.

    #2285 P2 dropped the ``aloop`` half of this parametrize (and the
    ``..._at_both_endpoints`` name it justified). It re-proved the rollback
    direction to the same depth as the arm; that direction is retired, and the
    parser now refuses it once, in
    ``test_the_retired_aloop_endpoint_is_refused_by_the_parser_itself``.
    """
    from jasper.cli.active_speaker import main

    h = _anchor_reemit_harness(monkeypatch, tmp_path)
    code = main([
        "baseline-reemit",
        "--endpoint", "ring",
        "--statefile", str(h.statefile),
    ])
    assert code == 0

    published = h.live_config.read_text(encoding="utf-8")
    assert f'device: "{RING_ACTIVE_PLAYBACK_DEVICE}"' in published, published
    assert f'device: "{RING_CAPTURE_DEVICE}"' in published, published

    # The metadata is what the runtime contract FOLLOWS to find this candidate,
    # so a published graph its metadata does not name is an anchor pointing at
    # nothing.
    meta = json.loads(h.live_meta.read_text(encoding="utf-8"))
    assert meta["config"]["path"] == str(h.live_config), meta["config"]
    assert meta["config"]["basename"] == h.live_config.name
    assert meta["metadata_path"] == str(h.live_meta)
    assert meta["status"] == "staged", meta["status"]

    # ...and the boot pointer now names it.
    assert f"config_path: {h.live_config}" in h.statefile.read_text(encoding="utf-8")


def test_baseline_reemit_prefers_the_applied_baseline_over_the_anchor(
    monkeypatch, tmp_path
):
    """Precedence: a commissioned box behaves EXACTLY as it did before.

    jts3 has an applied baseline and is the box this ladder was proven on. If the
    anchor path could win there, this change would have silently re-pointed a
    commissioned speaker at a freshly-staged all-muted graph — a working speaker
    going quiet. So the applied path must win, and the anchor must be left
    untouched on disk.
    """
    from jasper.cli.active_speaker import main

    anchor = _anchor_reemit_harness(monkeypatch, tmp_path)
    baseline = _reemit_harness(monkeypatch, tmp_path)

    code = main([
        "baseline-reemit",
        "--endpoint", "ring",
        "--statefile", str(baseline.statefile),
    ])
    assert code == 0
    # The APPLIED artifact was rewritten...
    assert baseline.artifact.read_text(encoding="utf-8") == "graph: 1\n"
    # ...and the anchor was not touched at all.
    assert anchor.live_config.read_text(encoding="utf-8") == "stale: true\n"
    assert anchor.live_meta.read_text(encoding="utf-8") == '{"status": "stale"}'


def test_baseline_reemit_refuses_a_box_on_neither_accepted_graph(
    monkeypatch, tmp_path
):
    """Neither class -> refuse by NAME, write nothing, and say what to do.

    A parked box has no roleful boot graph to move, so re-staging one would be
    inventing a commissioning state the operator never reached. The refusal names
    what was found and both accepted classes, so an operator is never left
    guessing which of the two this box was supposed to be.
    """
    from jasper.cli.active_speaker import main

    h = _anchor_reemit_harness(monkeypatch, tmp_path, decision_status="parked")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main([
            "baseline-reemit",
            "--endpoint", "ring",
            "--statefile", str(h.statefile),
        ])
    text = out.getvalue()

    assert code == 1
    assert "parked_all_muted" in text, text
    assert "approved_active_runtime" in text, text
    assert "all_muted_active_startup" in text, text
    # Nothing moved — all THREE live surfaces, like the sibling refusal tests.
    assert h.live_config.read_text(encoding="utf-8") == "stale: true\n"
    assert h.live_meta.read_text(encoding="utf-8") == '{"status": "stale"}'
    assert "config_path: /somewhere/else.yml" in h.statefile.read_text(encoding="utf-8")


def test_baseline_reemit_out_is_preview_only_for_the_anchor(
    monkeypatch, tmp_path
):
    """--out keeps its inspection semantics on the anchor path too.

    An operator reaching for a preview is not asking to arm the box, and on this
    path "the live artifact" is three things — the graph, the staged metadata
    that locates it, and the statefile — so all three are checked.
    """
    from jasper.cli.active_speaker import main

    h = _anchor_reemit_harness(monkeypatch, tmp_path)
    preview = tmp_path / "preview.yml"
    code = main([
        "baseline-reemit",
        "--endpoint", "ring",
        "--out", str(preview),
        "--statefile", str(h.statefile),
    ])
    assert code == 0
    assert f'device: "{RING_ACTIVE_PLAYBACK_DEVICE}"' in preview.read_text("utf-8")
    assert h.live_config.read_text(encoding="utf-8") == "stale: true\n"
    assert h.live_meta.read_text(encoding="utf-8") == '{"status": "stale"}'
    assert "config_path: /somewhere/else.yml" in h.statefile.read_text(encoding="utf-8")


def test_baseline_reemit_help_names_both_accepted_graph_classes():
    """The command's own --help states what it accepts, so prose cannot drift.

    The classification tokens are read from the runtime contract rather than
    typed here: a help string naming a class the contract has renamed is exactly
    the drift this pins against.
    """
    from jasper.active_speaker.runtime_contract import (
        GRAPH_ALL_MUTED_ACTIVE_STARTUP,
        GRAPH_APPROVED_ACTIVE_RUNTIME,
    )
    from jasper.cli.active_speaker import build_parser

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["baseline-reemit", "--help"])
    help_text = out.getvalue()

    assert GRAPH_APPROVED_ACTIVE_RUNTIME in help_text, help_text
    assert GRAPH_ALL_MUTED_ACTIVE_STARTUP in help_text, help_text


def test_baseline_reemit_anchor_refusal_writes_nothing_at_all(
    monkeypatch, tmp_path
):
    """A re-staged anchor that fails the re-proof must not reach disk.

    This is the fail-closure that matters most on this path, and it is STRICTER
    than the applied path's equivalent for a structural reason: the anchor lives
    at a FIXED path, so overwriting it IS moving the box's boot graph — there is
    no separate pointer to withhold. A half-written arm would leave a
    mid-commission speaker booting from a graph the runtime contract rejects,
    which is worse than not arming at all.

    All three live surfaces are checked, because "wrote nothing" on this path
    means three files: the graph, the staged metadata that LOCATES it, and the
    statefile.
    """
    from jasper.cli.active_speaker import main

    h = _anchor_reemit_harness(monkeypatch, tmp_path, reproof="unsafe")
    code = main([
        "baseline-reemit",
        "--endpoint", "ring",
        "--statefile", str(h.statefile),
    ])
    assert code == 1
    assert h.live_config.read_text(encoding="utf-8") == "stale: true\n"
    assert h.live_meta.read_text(encoding="utf-8") == '{"status": "stale"}'
    assert "config_path: /somewhere/else.yml" in h.statefile.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "decision_status,expected_class",
    [
        ("preserve_approved", GRAPH_APPROVED_ACTIVE_RUNTIME),
        ("preserve_driver_domain", "driver_domain_baseline"),
    ],
)
def test_baseline_reemit_refuses_a_preserved_non_anchor_graph(
    monkeypatch, tmp_path, decision_status, expected_class
):
    """The CLASSIFICATION discriminator, not the status — pinned (review C-SF1).

    `startup_anchor_from_decision` deliberately checks the classification and
    not just `decision.status`, because `preserve_current` is ALSO how an
    approved runtime graph and a driver-domain baseline are preserved. Its
    docstring says so; nothing tested it, and a status-only variant passed the
    whole suite.

    The box this protects is reachable, not hypothetical: a roleful box whose
    loaded graph classifies `approved_active_runtime` while its
    `active_speaker_baseline_profile.json` is missing or unreadable. `is_baseline`
    is derived from the graph's own `# Source:` marker, with no dependence on
    that state file — so `applied` is falsy, this path is entered, and the
    selector answers `preserve_current` + `approved_active_runtime`. Without the
    discriminator the command would re-stage an all-muted anchor over a
    COMMISSIONED speaker and repoint its boot statefile at it: silence at the
    next CamillaDSP restart.

    #2285 P2 dropped the second endpoint. The guard sits before any endpoint is
    used, so the rollback direction only ever re-entered it by the same door.
    """
    from jasper.cli.active_speaker import main

    h = _anchor_reemit_harness(
        monkeypatch, tmp_path, decision_status=decision_status
    )
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main([
            "baseline-reemit",
            "--endpoint", "ring",
            "--statefile", str(h.statefile),
        ])
    text = out.getvalue()

    assert code == 1
    # The refusal NAMES what it found, so an operator is not left guessing.
    assert expected_class in text, text
    # Nothing moved, on any of the three live surfaces.
    assert h.live_config.read_text(encoding="utf-8") == "stale: true\n"
    assert h.live_meta.read_text(encoding="utf-8") == '{"status": "stale"}'
    assert "config_path: /somewhere/else.yml" in h.statefile.read_text(encoding="utf-8")


def test_baseline_reemit_refuses_while_a_commission_load_is_active(
    monkeypatch, tmp_path
):
    """Single-flight against a live commission load (review H-N1).

    The path this command publishes over is not only the boot graph — it is the
    universal RE-MUTE anchor of the audible commissioning flow. Four controls
    reload exactly it, and one of them is the operator's own by-ear stop during a
    Stage-5 ramp (`commission-ramp ack --outcome too_loud`). Re-pointing that
    file at a different endpoint while a driver is armed at level moves the stop
    button while it is the thing standing between a driver and a bad load.

    So this refuses, exactly as `_cmd_commission_load` already refuses for the
    same shared artifact, and the refusal is checked to write nothing.
    """
    from jasper.cli.active_speaker import main

    h = _anchor_reemit_harness(monkeypatch, tmp_path, commission_loaded=True)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main([
            "baseline-reemit",
            "--endpoint", "ring",
            "--statefile", str(h.statefile),
        ])
    text = out.getvalue()

    assert code == 1
    assert "commission-rollback" in text, text
    assert h.live_config.read_text(encoding="utf-8") == "stale: true\n"
    assert h.live_meta.read_text(encoding="utf-8") == '{"status": "stale"}'
    assert "config_path: /somewhere/else.yml" in h.statefile.read_text(encoding="utf-8")


def test_baseline_reemit_force_overrides_the_commission_load_refusal(
    monkeypatch, tmp_path
):
    """`--force` is the documented escape hatch, and it actually passes.

    Same escape the sibling refusal offers. Asserted because a guard whose
    override does not work is a guard an operator cannot get past in the one
    situation they need to.
    """
    from jasper.cli.active_speaker import main

    h = _anchor_reemit_harness(monkeypatch, tmp_path, commission_loaded=True)
    code = main([
        "baseline-reemit",
        "--endpoint", "ring",
        "--force",
        "--statefile", str(h.statefile),
    ])
    assert code == 0
    assert h.live_config.read_text(encoding="utf-8") != "stale: true\n"
    assert f"config_path: {h.live_config}" in h.statefile.read_text(encoding="utf-8")


def test_baseline_reemit_published_evidence_names_no_temp_proof_dir(
    monkeypatch, tmp_path
):
    """Published staged evidence must not name the deleted proof directory.

    The anchor is proved in a `TemporaryDirectory` that is gone by the time the
    metadata lands, and `validate_camilla_config` records the file it was handed
    in BOTH `path` and (when the camilladsp binary exists) `argv`. Publishing
    those verbatim writes evidence pointing at a path that cannot exist.

    Nothing reads those two fields today — `_staged_candidate_ready` takes only
    `status` — so this is evidence honesty rather than behaviour. It is pinned
    because published evidence is exactly what a later reader trusts without
    re-deriving, and because the publish block's comment claims it.
    """
    import json as _json

    from jasper.active_speaker import staging as staging_mod
    from jasper.cli.active_speaker import main

    h = _anchor_reemit_harness(monkeypatch, tmp_path)

    # Force a validation result that carries the proof path in both fields, the
    # shape a Pi with camilladsp installed produces.
    real_stage = staging_mod.stage_protected_startup_config

    def _stage_with_validation(topology_arg, **kwargs):
        payload = real_stage(topology_arg, **kwargs)
        proof = payload["config"]["path"]
        payload["config"]["validation"] = {
            "status": "valid",
            "path": proof,
            "argv": ["/usr/local/bin/camilladsp", "--check", proof],
        }
        return payload

    monkeypatch.setattr(
        staging_mod, "stage_protected_startup_config", _stage_with_validation
    )

    code = main([
        "baseline-reemit",
        "--endpoint", "ring",
        "--statefile", str(h.statefile),
    ])
    assert code == 0

    published = _json.loads(h.live_meta.read_text(encoding="utf-8"))
    blob = _json.dumps(published)
    assert "jts-reemit-anchor-" not in blob, blob
    validation = published["config"]["validation"]
    assert validation["path"] == str(h.live_config), validation
    assert str(h.live_config) in validation["argv"], validation
    # The VERDICT still travels — only its locations were corrected.
    assert validation["status"] == "valid", validation


def test_baseline_reemit_publishes_the_anchor_pair_durably(monkeypatch, tmp_path):
    """BOTH halves of the anchor pair are fsynced, not just the graph.

    The graph is written `durable=True` because it is the box's next boot graph.
    Its staged metadata is what LOCATES that graph — the runtime contract follows
    `config.path` — so a power cut that keeps the durable half and loses the
    non-durable one leaves the box off its anchor. It parks silent and still
    takes deploys, so this is availability rather than safety, but the two halves
    should survive together and the fix is one kwarg.
    """
    from jasper import atomic_io
    from jasper.cli.active_speaker import main

    h = _anchor_reemit_harness(monkeypatch, tmp_path)
    text_calls: list[tuple[str, dict]] = []
    json_calls: list[tuple[str, dict]] = []
    real_text = atomic_io.atomic_write_text
    real_json = atomic_io.atomic_write_json

    def _spy_text(path, text, **kwargs):
        text_calls.append((str(path), kwargs))
        return real_text(path, text, **kwargs)

    def _spy_json(path, payload, **kwargs):
        json_calls.append((str(path), kwargs))
        return real_json(path, payload, **kwargs)

    monkeypatch.setattr(atomic_io, "atomic_write_text", _spy_text)
    monkeypatch.setattr(atomic_io, "atomic_write_json", _spy_json)

    code = main([
        "baseline-reemit",
        "--endpoint", "ring",
        "--statefile", str(h.statefile),
    ])
    assert code == 0

    graph_writes = [c for c in text_calls if c[0] == str(h.live_config)]
    meta_writes = [c for c in json_calls if c[0] == str(h.live_meta)]
    assert graph_writes, text_calls
    assert meta_writes, json_calls
    assert all(c[1].get("durable") for c in graph_writes), graph_writes
    assert all(c[1].get("durable") for c in meta_writes), meta_writes


def _probe_anchor_lock(lock_path) -> bool:
    """True when some open file description already holds ``lock_path``.

    A second descriptor on the same file is a faithful probe even inside the
    writer's own process — flock(2): "If a process uses open(2) ... to obtain
    more than one file descriptor for the same file, these file descriptors are
    treated independently by flock()."
    """
    import fcntl
    import os

    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def test_baseline_reemit_publishes_the_anchor_pair_under_one_lock(
    monkeypatch, tmp_path
):
    """The publish holds the pair's shared lock across BOTH halves (#2518).

    This command is the SECOND writer of the anchor pair; the first is
    `stage_protected_startup_config`, which every /sound/ route reaches. Neither
    publishes both halves in one filesystem operation, so a hold covering only
    one of them still leaves the window where one graph's metadata lands over
    another's bytes — and #2285's convergence design would invoke this command
    unattended, making the other racer a machine rather than only a human.

    Two claims, because either alone is insufficient: the lock is HELD at each
    write, and it is the SAME file the stager derives for this pair. A publish
    that dutifully locked a file nobody else takes would pass the first claim
    and serialize nothing.
    """
    from jasper import atomic_io
    from jasper.active_speaker import staging as staging_mod
    from jasper.cli.active_speaker import main

    h = _anchor_reemit_harness(monkeypatch, tmp_path)
    lock_path = staging_mod.staged_anchor_lock_path(h.live_config)
    held_at: dict[str, bool] = {}
    real_text = atomic_io.atomic_write_text
    real_json = atomic_io.atomic_write_json

    def _spy_text(path, text, **kwargs):
        if Path(path) == h.live_config:
            held_at["graph"] = _probe_anchor_lock(lock_path)
        return real_text(path, text, **kwargs)

    def _spy_json(path, payload, **kwargs):
        if Path(path) == h.live_meta:
            held_at["metadata"] = _probe_anchor_lock(lock_path)
        return real_json(path, payload, **kwargs)

    monkeypatch.setattr(atomic_io, "atomic_write_text", _spy_text)
    monkeypatch.setattr(atomic_io, "atomic_write_json", _spy_json)

    code = main([
        "baseline-reemit",
        "--endpoint", "ring",
        "--statefile", str(h.statefile),
    ])

    assert code == 0
    assert held_at == {"graph": True, "metadata": True}, held_at
    # Positive control: released on the way out, so the probe discriminates.
    assert _probe_anchor_lock(lock_path) is False


def test_baseline_reemit_refuses_a_held_anchor_and_publishes_nothing(
    monkeypatch, tmp_path
):
    """A contended publish waits a BOUNDED time, then writes nothing at all.

    Deploys and operator ladder steps invoke this command, and #2285's
    convergence design would invoke it unattended, so an open-ended wait would
    hang the caller rather than fail it. The refusal keeps the command's
    existing "a refusal writes nothing" contract: the outgoing anchor, its
    metadata, and the statefile are all left exactly as they were, so a retry
    starts from an intact box.
    """
    import fcntl
    import os
    import time

    from jasper.active_speaker import staging as staging_mod
    from jasper.cli.active_speaker import main

    h = _anchor_reemit_harness(monkeypatch, tmp_path)
    lock_path = staging_mod.staged_anchor_lock_path(h.live_config)
    monkeypatch.setattr(staging_mod, "STAGED_ANCHOR_LOCK_TIMEOUT_SEC", 0.2)

    holder = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    out = io.StringIO()
    try:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.write(holder, b"pid 4242 the-other-writer\n")
        started = time.monotonic()
        with contextlib.redirect_stdout(out):
            code = main([
                "baseline-reemit",
                "--endpoint", "ring",
                "--statefile", str(h.statefile),
            ])
        waited = time.monotonic() - started
    finally:
        os.close(holder)

    text = out.getvalue()
    assert code == 1
    assert "NOTHING was written" in text, text
    assert "the-other-writer" in text, text
    # Bounded: the proof run plus a 200 ms wait, not an open-ended block.
    assert waited < 20.0, waited
    # The outgoing pair AND the statefile survive untouched.
    assert h.live_config.read_text(encoding="utf-8") == "stale: true\n"
    assert h.live_meta.read_text(encoding="utf-8") == '{"status": "stale"}'
    assert "config_path: /somewhere/else.yml" in h.statefile.read_text(
        encoding="utf-8"
    )


def test_anchor_and_driver_commission_refusals_use_DISTINCT_reason_strings(
    monkeypatch, tmp_path
):
    """The two single-flight refusals are machine-readable and NOT the same token.

    `baseline-reemit` answers `commission_load_active`; its sibling
    `commission-load` answers `commission_load_already_active`. They refuse for
    related reasons over the same shared artifact, which is exactly why a caller
    parsing `--json` has to be able to tell them apart: one says "an anchor
    re-emit was refused", the other says "a second driver arm was refused", and
    the remedies differ.

    Pinned APART rather than together — asserting only that each is non-empty,
    or that both contain "commission_load", would let a future tidy-up collapse
    them into one token and silently merge two distinct outcomes.
    """
    import json as _json

    from jasper.cli.active_speaker import main

    h = _anchor_reemit_harness(monkeypatch, tmp_path, commission_loaded=True)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main([
            "baseline-reemit",
            "--endpoint", "ring",
            "--json",
            "--statefile", str(h.statefile),
        ])
    assert code == 1
    payload = _json.loads(out.getvalue())
    assert payload["status"] == "refused", payload
    assert payload["reason"] == "commission_load_active", payload
    # The sibling's token, read from its own source rather than retyped, so this
    # pin tracks a rename instead of going quietly vacuous after one.
    sibling = (
        Path(__file__).resolve().parents[1] / "jasper/cli/active_speaker.py"
    ).read_text(encoding="utf-8")
    assert '"reason": "commission_load_already_active",' in sibling, (
        "the sibling refusal's token changed; re-derive whether these two are "
        "still meant to be distinct"
    )
    assert payload["reason"] != "commission_load_already_active"
    # The refusal is actionable as DATA too, not only as prose.
    assert payload["active_target"] == "mono/tweeter", payload
