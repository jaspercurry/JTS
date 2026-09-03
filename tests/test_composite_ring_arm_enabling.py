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

The last section is later work on the same sink: the flat graph's PROGRAM-TO-
OUTPUT mapping. #3219 gave the emitter a width and left the composite's mapping
undecided; those tests pin the decision (one program channel per child DAC) and
the typed refusal that lands with it, which closes a hole live-channel counting
could not see.

NOTHING HERE ARMS ANYTHING. These are pure predicate/render tests; the arm of a
real composite box stays an operator ladder gated on item 2 (#2255).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from jasper.active_speaker.camilla_yaml import STARTUP_MUTE_GAIN_DB
from jasper.camilla_config_contract import PeqFilter
from jasper.active_speaker.runtime_contract import (
    _flat_hard_muted_outputs,
    active_ring_channels_for_topology,
    classify_camilla_graph,
    classify_output_contract,
    flat_graph_program_dest_map,
    ring_channels_for_topology,
    topology_sink_is_composite,
    topology_supports_shm_ring,
)
from jasper.output_topology import OUTPUT_TOPOLOGY_KIND, OutputTopology
from jasper.sound.camilla_yaml import (
    FlatChannelPlan,
    emit_flat_outputd_cutover_config,
    emit_sound_config,
    flat_graph_channel_plan,
)
from jasper.sound.profile import SoundProfile

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


def _stereo_topology() -> OutputTopology:
    """An ordinary single-DAC passive stereo box: outputs 0 and 1, one child."""
    return OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "stereo",
        "name": "Plain stereo",
        "status": "draft",
        "hardware": {
            "device_id": "apple_usb_c_dongle",
            "device_label": "Apple",
            "physical_output_count": 2,
            "child_devices": [{
                "child_id": "apple_dac_1",
                "device_id": "apple_usb_c_dongle",
                "device_label": "Apple",
                "serial": "AAA",
                "physical_output_indexes": [0, 1],
            }],
        },
        "speaker_groups": [
            {
                "id": "left", "label": "Left", "kind": "left",
                "mode": "full_range_passive",
                "channels": [{"role": "full_range", "physical_output_index": 0}],
            },
            {
                "id": "right", "label": "Right", "kind": "right",
                "mode": "full_range_passive",
                "channels": [{"role": "full_range", "physical_output_index": 1}],
            },
        ],
        "routing": {"main_left_group_id": "left", "main_right_group_id": "right"},
    })


def _composite_passive_stereo() -> OutputTopology:
    """A PASSIVE stereo composite: L on child A's output 0, R on child B's 2.

    The shape a dual-Apple stereo box really has — each child contributes ONE
    declared full_range output, which is why channel 1 is not output 1 here.
    """
    return _composite_topology([
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


def test_a_passive_composite_still_resolves_no_ring_at_all():
    """Widening the ACTIVE function must not give a PASSIVE composite a ring.

    It is not roleful, so the active arm never runs; Ring B still excludes it.
    Such a box stays on loopback, exactly as before.
    """
    passive = _composite_passive_stereo()
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


def test_declaring_the_floor_is_what_unlocks_the_conf_d_render(tmp_path, capsys):
    """The floor's real payoff, pinned at the command that consumes it: an
    absent floor short-circuits to ``no_declared_floor`` BEFORE the wire is
    resolved, so the ACTIVE block would keep the ioplug default forever."""
    from jasper.cli import audio_config

    conf = tmp_path / "60-jts-ring.conf"
    conf.write_text(Path(RING_CONF_D_SOURCE).read_text(encoding="utf-8"), "utf-8")
    before = conf.read_text(encoding="utf-8")

    rc = audio_config.main(
        ["render-ring-conf-wire", "--profile-id", "", "--conf-d", str(conf)]
    )

    assert rc == 0
    lines = capsys.readouterr().out.splitlines()
    assert "result skipped" in lines
    assert "reason no_declared_floor" in lines
    assert conf.read_text(encoding="utf-8") == before


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

    ``content_lane_format_for_coupling`` takes no coupling argument and
    answers the ring wire's resolved format unconditionally, which defaults
    WIDE — so an UNDECLARED composite converges with no declaration at all and
    this gate never fires for it. The one shape that still reaches the
    refusal is an operator's explicit narrow PIN
    (``JASPER_FANIN_RING_WIRE_FORMAT=S16_LE``, the rollback lever): a
    composite arm that rode such a pin onto the ring, without this gate, would
    quantize the post-crossover per-driver program from 32 to 16 bits.
    """
    from jasper.fanin import coupling_reconcile

    monkeypatch.setattr(
        "jasper.fanin_coupling.read_declared_ring_wire_format", lambda: "S16_LE"
    )
    ok, detail = coupling_reconcile.composite_ring_wire_ready(_composite_active_2way())
    assert ok is False
    assert "S16_LE" in detail
    # The remedy text: "remove JASPER_FANIN_RING_WIRE_FORMAT (or set it to
    # S32_LE)" — the two tokens are separate mentions in the sentence, not one
    # concatenated "KEY=S32_LE" literal.
    assert "remove JASPER_FANIN_RING_WIRE_FORMAT (or set it to S32_LE)" in detail


def test_the_narrow_wire_remedy_names_the_WHOLE_three_step_ladder(monkeypatch):
    """A REMEDY THAT DID NOT WORK, corrected.

    It used to say: set the key, re-run ``jasper-audio-hardware-reconcile``, then
    re-arm. That leaves the box's BOOT GRAPH still narrow — a roleful graph's
    capture and playback formats are baked when it is EMITTED
    (``active_emit_devices`` resolves them once, at emit), and the hardware
    reconciler re-renders the conf.d and outputd's env, not the graph. The
    operator meets the same refusal on the next arm, this time from
    ``ring_edge_width_ready``. Measured on jts.local 2026-08-15: the staged
    anchor declared S16_LE on both lanes before ``baseline-reemit --endpoint
    ring`` and S32_LE on both after.

    So the remedy is the whole ladder, GRAPH FIRST, and all three commands are
    pinned — two of them would read as covered while the load-bearing one was
    missing.

    The SPELLING is pinned too, in the `sudo /opt/jasper/.venv/bin/…` form the
    doctor's own rollback ladder uses (`jasper/cli/doctor/audio_runtime.py`).
    Both strings are operator-copied text for the same three rungs, and only
    that spelling pastes into a shell and works — a bare `jasper-active-speaker`
    is not on an operator's PATH.
    """
    from jasper.fanin import coupling_reconcile

    monkeypatch.setattr(
        "jasper.fanin_coupling.read_declared_ring_wire_format", lambda: "S16_LE"
    )
    _, detail = coupling_reconcile.composite_ring_wire_ready(_composite_active_2way())
    for command in (
        "sudo /opt/jasper/.venv/bin/jasper-active-speaker baseline-reemit "
        "--endpoint ring",
        "sudo systemctl start jasper-audio-hardware-reconcile",
        "sudo /opt/jasper/.venv/bin/jasper-fanin-coupling-reconcile shm_ring",
    ):
        assert command in detail, f"remedy must name `{command}`: {detail}"
    assert detail.index("baseline-reemit") < detail.index(
        "jasper-audio-hardware-reconcile"
    ), "the graph rung must be named FIRST — that ordering is the whole fix"


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


def test_the_ring_conf_journal_carries_the_active_width():
    """The whitelist arm that stops 1d's headline output being dropped silently.

    ``render_ring_conf_if_needed`` parses the renderer's ``key value`` lines
    through a ``case`` whose unmatched arm drops the key with NO diagnostic — so
    a key the renderer prints and this whitelist lacks is invisible rather than
    wrong, which is the shape that hid ``ring_active_channels`` in the first
    place. Pinning the ARM (not only the rendered log line) is that same
    silent-drop lesson applied to the fix for it: the three literal log-line
    pins in ``test_audio_hardware_reconcile.py`` would all still pass if the arm
    were re-added under a misspelled key and the value came from somewhere else.
    """
    script = (REPO_ROOT / "deploy" / "bin" / "jasper-audio-hardware-reconcile").read_text(
        encoding="utf-8"
    )
    assert 'ring_active_channels) channels_active="$value" ;;' in script, (
        "the renderer prints ring_active_channels; without this whitelist arm "
        "the case drops it silently"
    )
    assert "ring_active_channels=${channels_active:-none}" in script, (
        "the parsed value must reach the journal line, or the arm is inert"
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
    'NO CHANGE': it branches on ring PCM NAME membership and carries no channel
    axis at all, so it is already 4-channel-safe.
    """
    import dataclasses

    from jasper.active_speaker.camilla_yaml import ActiveEmitDevices, active_emit_devices

    assert "channels" not in {f.name for f in dataclasses.fields(ActiveEmitDevices)}
    from jasper.fanin_coupling import RING_ACTIVE_PLAYBACK_DEVICE

    for device in (RING_ACTIVE_PLAYBACK_DEVICE, "hw:CARD=Lab,DEV=0"):
        assert active_emit_devices(
            device, topology=_composite_active_2way()
        ) == active_emit_devices(device, topology=_stereo_topology())


# --- the enable != arm rule --------------------------------------------------


def test_the_unattended_pass_refuses_a_composite_carrying_neither_proven_arm(
    monkeypatch, tmp_path
):
    """D5's rule, mechanically: landing 1b-1e ENABLES arming and ARMS NOTHING.

    A composite ``active_2_way`` is roleful, so it meets
    ``ring_roleful_unattended_ready``. With neither proven arm on the box — no
    applied baseline, no staged anchor — the gate's fail-closed default holds
    and no boot, deploy, or reconcile auto-arms this composite. The narrowing
    (§12 decision 1) changed which roleful boxes are admitted, never whether a
    bare one is.
    """
    from jasper.fanin import coupling_reconcile

    gate_names = [name for name, _ in coupling_reconcile.default_ring_gates()]
    assert "ring_roleful_unattended" in gate_names

    monkeypatch.setattr(
        "jasper.active_speaker.baseline_profile.load_applied_baseline_profile_state",
        lambda *a, **k: None,
    )
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(tmp_path / "absent.yml"))
    monkeypatch.setattr(
        "jasper.output_topology.load_output_topology_strict", _composite_active_2way
    )
    ok, detail = coupling_reconcile.ring_roleful_unattended_ready()
    assert ok is False
    assert "jasper-fanin-coupling-reconcile shm_ring" in detail


# --- the composite's flat program-to-output mapping --------------------------
#
# #3219 gave the flat emitter a width and recorded that a composite sink still
# needed "the mixer's dest-to-output mapping decided before a wide graph means
# anything there". This is that decision, and the refusal that lands with it.


def _flat_program_dests(yaml_text: str) -> dict[int, int]:
    """Which PROGRAM channel each mixer dest carries, off the emitted YAML.

    Reads the graph rather than the plan, so these tests judge what CamillaDSP
    would actually do. A dest fed only at the hard-mute floor carries no
    program and is absent from the result.
    """
    payload = yaml.safe_load(yaml_text)
    carried: dict[int, int] = {}
    for entry in payload["mixers"]["master_gain"]["mapping"]:
        live = [
            source for source in entry["sources"]
            if float(source["gain"]) > STARTUP_MUTE_GAIN_DB
        ]
        if len(live) == 1:
            carried[int(entry["dest"])] = int(live[0]["channel"])
    return carried


def test_a_wide_composite_graph_puts_one_program_channel_on_each_child():
    """THE headline pin: L on child A's output, R on child B's — not 0 and 1.

    A dual-Apple stereo box declares outputs 0 and 2, and outputd deinterleaves
    0/1 to child A and 2/3 to child B. An identity mixer would send BOTH program
    channels into child A and leave the declared right speaker silent, so the
    mapping is what makes a wide graph mean anything here.
    """
    topology = _composite_passive_stereo()
    text = emit_flat_outputd_cutover_config(topology=topology, width=4)

    assert _flat_program_dests(text) == {0: 0, 2: 1}


def test_a_wide_composite_graph_terminally_mutes_the_outputs_between():
    """The dead dests are 1 and 3 — BETWEEN the program's two, not after them.

    The surplus rule used to be "past the program's own width"; on a composite
    the unclaimed output sits in the middle, so the mute set follows the mapping
    instead. Proved off the emitted graph by the same structural check the
    runtime contract uses, never off the plan's intent.
    """
    topology = _composite_passive_stereo()
    text = emit_flat_outputd_cutover_config(topology=topology, width=4)

    assert _flat_hard_muted_outputs(text, 4) == frozenset({1, 3})


def test_the_wide_composite_graph_corrects_both_program_channels():
    """Each program-carrying dest keeps its OWN chain, in the right ORIENTATION.

    The pipeline's Mixer runs FIRST, so the per-channel chains belong to DESTS.
    Leaving them on 0 and 1 would apply the right channel's correction to a dead
    output and put uncorrected program on the live one — a gain-structure defect
    the mute set alone cannot see.

    Deliberately ASYMMETRIC (left 111 Hz; right 777 Hz plus a delay): under a
    symmetric profile the two chains are equal, so "each dest has a chain" holds
    whichever way round they are wired and a swapped mapping stays green. The
    distinct room segments are what make the ORIENTATION observable, and the
    left/right room split is the axis that carries a per-seat correction.
    """
    topology = _composite_passive_stereo()
    # Through the PLAN, not hand-written kwargs: the routing under test is the
    # topology's own answer. Only the asymmetric profile is supplied here, which
    # the cutover emitter (a fixed flat profile) has no way to take.
    plan = flat_graph_channel_plan(topology, width=4)
    payload = yaml.safe_load(emit_sound_config(
        SoundProfile(enabled=False),
        width=4,
        muted_outputs=plan.muted_outputs,
        program_dest_map=plan.program_dest_map,
        room_peqs=[PeqFilter(freq=111.0, q=2.0, gain=-3.0)],
        room_peqs_right=[PeqFilter(freq=777.0, q=2.0, gain=-3.0)],
        channel_delays_ms=(0.0, 1.25),
    ))
    chains = {
        int(step["channels"][0]): step["names"]
        for step in payload["pipeline"]
        if step["type"] == "Filter"
    }
    # ROOM filters only. Every graph also carries the preference frame — 5
    # simple bands and an 8-slot advanced pool, idle at 0 dB — and this test is
    # about which SEAT's correction lands on which speaker, not about the frame.
    peq_freq = {
        name: spec["parameters"]["freq"]
        for name, spec in payload["filters"].items()
        if spec["parameters"].get("freq") is not None
        and not name.startswith("sound_")
    }

    assert set(chains) == {0, 1, 2, 3}
    # Child A's declared output carries the LEFT room segment, child B's the
    # RIGHT — a swap lands each seat's correction on the other speaker.
    assert [peq_freq[name] for name in chains[0] if name in peq_freq] == [111.0]
    assert [peq_freq[name] for name in chains[2] if name in peq_freq] == [777.0]
    assert all(name.endswith("_commission_mute") for name in chains[1] + chains[3])


def test_the_checker_refuses_the_identity_shape_on_a_composite():
    """The pre-PR shape — program on 0 and 1, mutes on 2 and 3 — is REFUSED.

    Live-channel COUNTING cannot see this: two live channels against two
    declared outputs balances exactly as the correct graph does. Resolving the
    mapping is what turns the count into the per-index question that catches it.
    """
    topology = _composite_passive_stereo()
    wrong = emit_sound_config(
        SoundProfile(enabled=False), width=4, muted_outputs=(2, 3)
    )

    assert _flat_program_dests(wrong) == {0: 0, 1: 1}
    graph = classify_camilla_graph(topology=topology, text=wrong)
    assert graph.allowed is False
    assert "flat_full_range_graph_wider_than_topology" in {
        issue["code"] for issue in graph.issues
    }


def test_the_checker_admits_the_mapped_shape_on_a_composite():
    """The BOUNDARY for the test above: the mapped graph is allowed.

    Emitter and checker are pinned against each other here — a refusal that
    also rejected the correct graph would be a park, not a fix.
    """
    topology = _composite_passive_stereo()
    text = emit_flat_outputd_cutover_config(topology=topology, width=4)

    assert classify_camilla_graph(topology=topology, text=text).allowed is True


def test_a_wide_graph_whose_mapping_is_undecided_is_refused_by_name():
    """An UNDECIDED mapping plus a wide graph is a typed refusal, not a count.

    Both children must declare exactly one output for the pairing to resolve;
    a composite whose declared outputs sit on ONE child resolves nothing, and a
    wide graph from it can no longer slip through on a balanced live count.
    """
    lopsided = _composite_topology([
        {
            "id": "left", "label": "Left", "kind": "left",
            "mode": "full_range_passive",
            "channels": [{"role": "full_range", "physical_output_index": 0}],
        },
        {
            "id": "right", "label": "Right", "kind": "right",
            "mode": "full_range_passive",
            "channels": [{"role": "full_range", "physical_output_index": 1}],
        },
    ])
    contract = classify_output_contract(lopsided)
    assert flat_graph_program_dest_map(lopsided, contract, width=4) is None

    graph = classify_camilla_graph(
        topology=lopsided,
        text=emit_sound_config(
            SoundProfile(enabled=False), width=4, muted_outputs=(2, 3)
        ),
    )
    assert graph.allowed is False
    assert "flat_full_range_graph_mapping_undecided" in {
        issue["code"] for issue in graph.issues
    }


def test_a_declared_output_that_receives_no_program_is_refused():
    """#3219's inherited hole: a wide graph that renders CLEAN and plays SILENT.

    Its own example — a mono box whose one ``full_range`` output sits past the
    program's channels, rendered at width 4. The mutes are all correct and the
    one declared output is unmuted, so every check that judges *mutes* passes;
    but output 2 is a surplus dest carrying the mixer's mute-floor feed, so
    nothing routes program to it. The map is what makes "this declared output
    receives no program" decidable, which is why #3219 deferred the refusal to
    this PR rather than guessing at it.
    """
    mono_past_program = OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "mono-past-program",
        "name": "Mono on output 2",
        "status": "draft",
        "hardware": {
            "device_id": "hifiberry_dac8x",
            "device_label": "DAC8x",
            "physical_output_count": 8,
            "child_devices": [{
                "child_id": "dac8x", "device_id": "hifiberry_dac8x",
                "device_label": "DAC8x", "serial": "AAA",
                "physical_output_indexes": [0, 1, 2, 3, 4, 5, 6, 7],
            }],
        },
        "speaker_groups": [{
            "id": "mono", "label": "Mono", "kind": "mono",
            "mode": "full_range_passive",
            "channels": [{"role": "full_range", "physical_output_index": 2}],
        }],
        "routing": {"main_left_group_id": "mono", "main_right_group_id": "mono"},
    })
    text = emit_flat_outputd_cutover_config(topology=mono_past_program, width=4)

    # The render completes and output 2 is the ONLY live channel — the exact
    # shape #3219 described, reproduced rather than asserted from its prose.
    assert _flat_hard_muted_outputs(text, 4) == frozenset({0, 1, 3})
    assert 2 not in _flat_program_dests(text)

    graph = classify_camilla_graph(topology=mono_past_program, text=text)
    assert graph.allowed is False
    assert "flat_full_range_graph_declared_output_unfed" in {
        issue["code"] for issue in graph.issues
    }


@pytest.mark.parametrize("width", [2, 4])
def test_the_narrow_composite_answer_is_unchanged(width: int):
    """At the PROGRAM's own width nothing moves — the pre-existing rule stands.

    A 2-wide composite graph cannot express outputs 0 and 2, so the mapping
    stays undecided there and the box keeps counting, which is why a working
    dual-Apple stereo box is still not refused. Only the WIDE answer is new.
    """
    topology = _composite_passive_stereo()
    contract = classify_output_contract(topology)
    resolved = flat_graph_program_dest_map(topology, contract, width=width)

    assert (resolved is None) is (width == 2)
    assert flat_graph_channel_plan(topology, width=2) == FlatChannelPlan()


def test_a_non_composite_box_still_maps_by_index():
    """The identity answer is reported as ABSENCE, so stereo emits unchanged.

    The plan returning ``None`` rather than ``(0, 1)`` is what keeps every
    non-composite emit byte-identical — the emitter reads absence as the
    identity, so there is only one spelling of the ordinary graph.
    """
    stereo = _stereo_topology()
    contract = classify_output_contract(stereo)

    assert flat_graph_program_dest_map(stereo, contract, width=2) == (0, 1)
    assert flat_graph_channel_plan(stereo, width=2).program_dest_map is None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__]))
