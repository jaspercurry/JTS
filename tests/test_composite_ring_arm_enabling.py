# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""P8b item 1 (sub-items 1b-1f): the composite becomes ring-ARMABLE.

The design's acceptance bullets, as tests. Every one of these pins a promise
made in prose somewhere in the diff:

- **1b** ``active_ring_channels_for_topology`` answers 4 for a composite
  ``active_2_way``; still refuses duplicate and non-contiguous indices; and the
  BOUNDARY — ``ring_channels_for_topology`` / ``topology_supports_shm_ring``
  still give the excluded answer for the same fixture — is asserted, not assumed.
- **1c** the composite's ``LatencyFloor`` period equals ``RING_SLOT_FRAMES``
  WITH ITS REASON, and equals its children's floor.
- **1d** the conf.d ACTIVE block renders ``channels 4``.
- **1e** a composite may not arm the ring at the narrow wire.
- **1f** the reconciler/emitter walk's load-bearing verdicts.

NOTHING HERE ARMS ANYTHING. These are pure predicate/render tests; the arm of a
real composite box stays an operator ladder gated on item 2 (#2255).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jasper.active_speaker.runtime_contract import (
    active_ring_channels_for_topology,
    ring_channels_for_topology,
    topology_sink_is_composite,
    topology_supports_shm_ring,
)
from jasper.output_topology import OUTPUT_TOPOLOGY_KIND, OutputTopology

REPO_ROOT = Path(__file__).resolve().parents[1]


# --- fixtures ----------------------------------------------------------------


def _active_group(kind: str, start: int) -> dict:
    """A 2-way speaker group owning outputs ``start`` and ``start + 1``."""
    channels = []
    for offset, role in enumerate(("woofer", "tweeter")):
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
        "mode": "active_2_way",
        "channels": channels,
    }


def _composite_topology(groups: list[dict], *, routing: dict | None = None) -> OutputTopology:
    """A saved dual-Apple composite: 4 outputs, child A owns 0/1, child B 2/3.

    The child ``physical_output_indexes`` are the FLAT pairs
    ``topology_hardware_from_state`` writes for this profile (``[2i, 2i+1]``),
    and ``_dual_apple_clock_issues`` blocks any composite whose children do not
    cover ``range(4)`` exactly once — so this is the only shape a real saved
    composite can have.
    """
    return OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "dual-active",
        "name": "Dual Apple active 2-way",
        "status": "draft",
        "hardware": {
            "device_id": "dual_apple_usb_c_dac_4ch",
            "device_label": "Dual Apple",
            # HARD-PINNED at 4 by ``OutputHardware.validate`` — the composite
            # profile requires exactly four physical outputs, so every fixture
            # here indexes the same 0..3 space a real box would.
            "physical_output_count": 4,
            "child_devices": [
                {
                    "child_id": "apple_dac_1",
                    "device_id": "apple_usb_c_dongle",
                    "device_label": "Apple A",
                    "serial": "AAA",
                    "physical_output_indexes": [0, 1],
                },
                {
                    "child_id": "apple_dac_2",
                    "device_id": "apple_usb_c_dongle",
                    "device_label": "Apple B",
                    "serial": "BBB",
                    "physical_output_indexes": [2, 3],
                },
            ],
        },
        "speaker_groups": groups,
        "routing": routing
        or {"main_left_group_id": "left", "main_right_group_id": "right"},
    })


def _composite_active_2way() -> OutputTopology:
    """The real shape: left = woofer 0 / tweeter 1, right = woofer 2 / tweeter 3.

    Note this is also the CLEAN per-child shape — each speaker's drivers sit on
    one child, so ``cross_child_group_verdicts`` reports nothing. A crossover
    straddling the two dongles is a separate, already-shipped warning (#2486).
    """
    return _composite_topology([_active_group("left", 0), _active_group("right", 2)])


# --- 1b: the Python gate chain ----------------------------------------------


def test_composite_active_2way_resolves_a_four_channel_active_ring():
    """THE arm-enabling assertion. Was ``None`` before P8b item 1b."""
    topology = _composite_active_2way()
    assert topology_sink_is_composite(topology) is True, (
        "fixture must actually be composite or this test proves nothing"
    )
    assert active_ring_channels_for_topology(topology) == 4


def test_the_composite_width_comes_from_flat_contiguous_indices():
    """The design deferred ONE datum to implementation time: are a composite's
    ``physical_output_index`` values flat ``0..3`` or child-relative ``0..1``
    twice? They are FLAT, and this is the tree fact that settles it — the same
    index space the children's ``physical_output_indexes`` use, which is why no
    child-ordinal composition is needed in the arm.
    """
    topology = _composite_active_2way()
    owned = sorted(
        index
        for child in topology.hardware.child_devices
        for index in child.physical_output_indexes
    )
    assigned = sorted(
        channel.physical_output_index
        for group in topology.speaker_groups
        for channel in group.channels
        if channel.physical_output_index is not None
    )
    assert owned == [0, 1, 2, 3]
    assert assigned == owned, (
        "children and speaker groups must index the SAME flat output space; if "
        "this ever diverges the composite arm needs child-ordinal composition"
    )


def test_composite_still_refuses_duplicate_indices():
    """The duplicate refusal is REUSED by the composite arm, not relaxed.

    This is the shape that would arise if a future writer made the indices
    child-relative: two outputs both claiming 0. It must refuse, never guess.
    """
    left = _active_group("left", 0)
    right = _active_group("right", 0)  # collides with left on 0 and 1
    right["id"] = "right"
    right["kind"] = "right"
    topology = _composite_topology([left, right])
    assert topology_sink_is_composite(topology) is True
    assert active_ring_channels_for_topology(topology) is None


def test_composite_still_refuses_non_contiguous_indices():
    """The contiguity refusal is likewise reused.

    A mono 3-way driving outputs 0, 1 and **3** leaves a hole at 2: three
    assignments, three distinct indices (so the duplicate guard passes), but
    ``max + 1 == 4 != 3``. That is not a width the emitted graph and the ring
    can both mean the same thing by, so it refuses.

    The gap has to live INSIDE 0..3: ``OutputHardware.validate`` pins this
    profile at exactly four physical outputs, so a composite fixture cannot
    reference a fifth. Which is itself more evidence for the flat index space.
    """
    mono = {
        "id": "mono",
        "label": "Mono speaker",
        "kind": "mono",
        "mode": "active_3_way",
        "channels": [
            {"role": "woofer", "physical_output_index": 0, "identity_verified": True},
            {"role": "mid", "physical_output_index": 1, "identity_verified": True},
            {
                "role": "tweeter",
                "physical_output_index": 3,
                "identity_verified": True,
                "startup_muted": True,
                "protection_required": True,
                "protection_status": "software_guard_requested",
            },
        ],
    }
    topology = _composite_topology([mono], routing={"mono_group_id": "mono"})
    assert topology_sink_is_composite(topology) is True
    assert active_ring_channels_for_topology(topology) is None


def test_the_ring_b_boundary_is_asserted_not_assumed():
    """THE BOUNDARY the design names as the forbidden one-liner.

    ``ring_channels_for_topology`` answers for Ring B — the full-range stereo
    program — and its answer is stamped into ``pcm.jts_ring_playback``. If it
    ever returned the ACTIVE width for a composite, that width would land in the
    STEREO ring's block. Asserted on the SAME fixture that now resolves 4, so
    the two answers are proven to differ rather than assumed to.
    """
    topology = _composite_active_2way()
    assert active_ring_channels_for_topology(topology) == 4
    assert ring_channels_for_topology(topology) is None
    assert topology_supports_shm_ring(topology) is False


def test_a_passive_composite_still_resolves_no_ring_at_all():
    """Widening the ACTIVE function must not give a PASSIVE composite a ring.

    It is not roleful, so the active arm never runs; Ring B still excludes it.
    Such a box stays on loopback, exactly as before.
    """
    passive = _composite_topology([
        {
            "id": "left",
            "label": "Left",
            "kind": "left",
            "mode": "full_range_passive",
            "channels": [{"role": "full_range", "physical_output_index": 0}],
        },
        {
            "id": "right",
            "label": "Right",
            "kind": "right",
            "mode": "full_range_passive",
            "channels": [{"role": "full_range", "physical_output_index": 2}],
        },
    ])
    assert active_ring_channels_for_topology(passive) is None
    assert ring_channels_for_topology(passive) is None
    assert topology_supports_shm_ring(passive) is False


# --- 1c: the composite LatencyFloor -----------------------------------------


def test_composite_floor_period_equals_ring_slot_with_its_reason():
    """The period pin, WITH the reason, so it fails if either number moves.

    The equality is what clears ``_cmd_render_ring_conf_wire``'s
    ``ring_slot_fixed_128`` refusal without needing issue #2147. Asserting only
    ``== 128`` would survive ``RING_SLOT_FRAMES`` moving to 64 while the floor
    stayed 128 — which is exactly the drift that would make the render refuse
    while this test stayed green.
    """
    from jasper.audio_hardware.dac import latency_floor_for
    from jasper.fanin_coupling import RING_SLOT_FRAMES

    floor = latency_floor_for("dual_apple_usb_c_dac_4ch")
    assert floor is not None, "the composite must DECLARE a floor or nothing renders"
    assert floor.outputd_period_frames == RING_SLOT_FRAMES
    assert RING_SLOT_FRAMES == 128, (
        "the reason this equality matters is that the ring slot is 128; if the "
        "slot moved, re-derive the composite floor rather than re-pinning this"
    )


def test_composite_floor_equals_its_children():
    """Both children ARE the Apple dongle, so the composite's floor is the
    measured Apple floor — the same silicon at the same rate, twice. A different
    number here would claim a timing property no dongle was measured at."""
    from jasper.audio_hardware.dac import (
        DUAL_APPLE_USB_C_DAC_4CH,
        latency_floor_for,
    )

    child_ids = set(DUAL_APPLE_USB_C_DAC_4CH.child_profile_ids)
    assert child_ids == {"apple_usb_c_dongle"}, (
        "this contract holds because every child is the same profile; a "
        "heterogeneous composite needs a re-derived floor, not this assertion"
    )
    child_floor = latency_floor_for("apple_usb_c_dongle")
    assert child_floor is not None
    assert DUAL_APPLE_USB_C_DAC_4CH.latency_floor == child_floor


def test_declaring_the_floor_is_what_unlocks_the_conf_d_render():
    """The floor's real payoff, pinned at the command that consumes it: an
    absent floor short-circuits to ``no_declared_floor`` BEFORE the wire is
    resolved, so the ACTIVE block would keep the ioplug default forever."""
    source = (REPO_ROOT / "jasper" / "cli" / "audio_config.py").read_text(
        encoding="utf-8"
    )
    assert 'print("reason no_declared_floor")' in source, (
        "the skip path this floor exists to avoid moved; re-derive 1c's rationale"
    )


# --- 1d: the conf.d ACTIVE block renders channels 4 -------------------------


def test_conf_d_active_block_renders_four_channels_for_the_composite(tmp_path):
    """END TO END through the real renderer: topology -> wire -> conf.d text."""
    from jasper.fanin_coupling import resolve_ring_wire
    from jasper.ring_assets import (
        RING_ACTIVE_CONF_PCM,
        render_ring_conf_wire,
        ring_conf_channels,
    )

    conf = tmp_path / "60-jts-ring.conf"
    conf.write_text(Path(RING_CONF_D_SOURCE).read_text(encoding="utf-8"), "utf-8")
    # The shipped block starts at the ioplug default, or the test proves nothing.
    assert ring_conf_channels(RING_ACTIVE_CONF_PCM, conf_d=str(conf)) in (None, 2)

    wire = resolve_ring_wire(_composite_active_2way())
    assert wire.ring_active_channels == 4
    render_ring_conf_wire(wire, conf_d=str(conf))
    assert ring_conf_channels(RING_ACTIVE_CONF_PCM, conf_d=str(conf)) == 4


# The shipped conf.d, read from the repo rather than /etc (this is a
# hardware-free test).
RING_CONF_D_SOURCE = REPO_ROOT / "deploy" / "alsa" / "conf.d" / "60-jts-ring.conf"


# --- 1e: the wide ring wire --------------------------------------------------


def test_a_composite_may_not_arm_the_ring_at_the_narrow_wire(monkeypatch):
    """The width REGRESSION refusal.

    ``content_lane_format_for_coupling`` answers S32_LE under ``loopback`` and
    ``resolve_ring_wire().sample_format`` under ``shm_ring`` — narrow by
    default. So a composite arm that did not also declare the wide wire would
    quantize the post-crossover per-driver program from 32 to 16 bits.
    """
    from jasper.fanin import coupling_reconcile

    monkeypatch.setattr(
        coupling_reconcile, "load_topology_for_wire", _composite_active_2way
    )
    monkeypatch.setattr(
        "jasper.fanin_coupling.read_declared_ring_wire_format", lambda: "S16_LE"
    )
    ok, detail = coupling_reconcile.composite_ring_wire_ready(_composite_active_2way())
    assert ok is False
    assert "S16_LE" in detail
    assert "JASPER_FANIN_RING_WIRE_FORMAT=S32_LE" in detail


def test_a_composite_at_the_wide_wire_passes_the_rule(monkeypatch):
    monkeypatch.setattr(
        "jasper.fanin_coupling.read_declared_ring_wire_format", lambda: "S32_LE"
    )
    from jasper.fanin import coupling_reconcile

    ok, detail = coupling_reconcile.composite_ring_wire_ready(_composite_active_2way())
    assert ok is True
    assert "S32_LE" in detail


def test_the_wide_wire_rule_leaves_every_non_composite_box_alone(monkeypatch):
    """jts3's roleful DAC8x arm and every stereo-ring box keep today's wire."""
    from jasper.fanin import coupling_reconcile

    monkeypatch.setattr(
        "jasper.fanin_coupling.read_declared_ring_wire_format", lambda: "S16_LE"
    )
    ok, detail = coupling_reconcile.composite_ring_wire_ready(None)
    assert ok is True
    assert "does not apply" in detail


def test_the_wide_wire_rule_is_wired_into_the_active_arm(monkeypatch, tmp_path):
    """HALF-GUARDED SITES READ AS COVERED. The rule is only worth anything if
    ``ring_topology_ready`` actually calls it, so assert the refusal reaches the
    gate both arming paths use — not just the helper in isolation."""
    from jasper.fanin import coupling_reconcile

    monkeypatch.setattr(
        coupling_reconcile,
        "load_output_topology_strict",
        _composite_active_2way,
        raising=False,
    )
    monkeypatch.setattr(
        "jasper.output_topology.load_output_topology_strict", _composite_active_2way
    )
    monkeypatch.setattr(
        "jasper.fanin_coupling.read_declared_ring_wire_format", lambda: "S16_LE"
    )
    ok, detail = coupling_reconcile.ring_topology_ready()
    assert ok is False
    assert "WIDE wire" in detail, detail


# --- 1f: the reconciler / emitter walk --------------------------------------


def test_the_reconciler_stages_the_composite_active_lane_pair():
    """``apply_observed_composite_policy`` used to DISCARD the endpoint device
    ``active_graph_status`` returns, so no composite could ever be staged no
    matter what the Python preflights admitted."""
    script = (REPO_ROOT / "deploy" / "bin" / "jasper-audio-hardware-reconcile").read_text(
        encoding="utf-8"
    )
    assert 'DUAL_APPLE_ACTIVE_ENDPOINT_DEVICE="${graph_status#* }"' in script
    assert (
        'set_outputd_active_lane_pair "1" "$DUAL_APPLE_ACTIVE_ENDPOINT_DEVICE"'
        in script
    )


def test_an_aloop_composite_still_clears_the_pair():
    """SCOPED TO THE OBSERVED-BROKEN PATH. A composite whose accepted graph does
    NOT name the ring device keeps the unconditional clear it has today."""
    script = (REPO_ROOT / "deploy" / "bin" / "jasper-audio-hardware-reconcile").read_text(
        encoding="utf-8"
    )
    marker = (
        'if [[ "$DUAL_APPLE_ACTIVE_ENDPOINT_DEVICE" == '
        '"$RING_ACTIVE_OUTPUTD_PLAYBACK_DEVICE" ]]; then'
    )
    assert marker in script
    head, _, tail = script.partition(marker)
    assert 'set_outputd_active_lane_pair "" ""' in tail.split("fi", 1)[0]


def test_outputd_admits_a_composite_active_ring_endpoint():
    """The Rust half of the same gate: ``ring_active_ok`` no longer carries the
    ``SingleAlsa``-only term. Without this the Python chain would admit an arm
    that outputd then refuses at exit 78 — enabled on one side, parked on the
    other."""
    source = (REPO_ROOT / "rust" / "jasper-outputd" / "src" / "config.rs").read_text(
        encoding="utf-8"
    )
    assert "matches!(sink_mode, SinkMode::SingleAlsa | SinkMode::Composite)" in source
    # And the stereo-only features must NOT have been widened with it.
    assert (
        "sink_mode == SinkMode::SingleAlsa && content_channels == 2 && !active_lane"
        in source
    ), "is_full_range_stereo_lr_sink must still exclude the composite"


def test_active_emit_devices_needs_no_composite_change():
    """The design named this as the function to walk. THE WALK'S VERDICT IS
    'NO CHANGE', recorded as a test so the claim is checkable rather than
    asserted in a PR body: it branches on ring PCM NAME membership and carries
    no channel axis at all, so it is already 4-channel-safe.
    """
    import inspect

    from jasper.active_speaker.camilla_yaml import ActiveEmitDevices, active_emit_devices

    source = inspect.getsource(active_emit_devices)
    assert "if playback_device not in RING_PCM_DEVICES:" in source
    assert "composite" not in source.lower(), (
        "active_emit_devices must stay composite-agnostic"
    )
    assert not hasattr(ActiveEmitDevices, "channels"), (
        "it carries no channel axis, which is why a 4-ch composite needs nothing "
        "from it"
    )


# --- the enable != arm rule --------------------------------------------------


def test_the_unattended_pass_still_refuses_every_roleful_box():
    """D5's rule, mechanically: landing 1b-1e ENABLES arming and ARMS NOTHING.

    A composite ``active_2_way`` is roleful, and ``ring_not_roleful_ready`` is
    the unattended pass's dedicated roleful exclusion — so no boot, deploy, or
    reconcile can auto-arm a composite. Arming stays an explicit operator CLI
    action, gated further on item 2 (#2255) landing.
    """
    from jasper.fanin import coupling_reconcile

    gate_names = [name for name, _ in coupling_reconcile.default_ring_gates()]
    assert "ring_not_roleful" in gate_names

    import jasper.output_topology as ot

    original = ot.load_output_topology_strict
    ot.load_output_topology_strict = _composite_active_2way
    try:
        ok, detail = coupling_reconcile.ring_not_roleful_ready()
    finally:
        ot.load_output_topology_strict = original
    assert ok is False
    assert "explicit" in detail


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__]))
