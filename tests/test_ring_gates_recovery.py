# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The ring's wire gates, and the geometry heals the convergence runs.

Two questions a box has to answer before its graph can attach to the ring:

* can the INSTALLED ioplug parse the wire this box resolves? (a stale ``.so``
  beside new daemons — the build degrades to a WARN, so this is the ordinary
  shape of a bad deploy, not an exotic one);
* does every declaring end state the SAME wire?

Plus the two heals the reconciler runs on every pass: a shear-prone stale
``JASPER_FANIN_RING_SLOTS`` and a geometry-mismatched on-disk ring file.
"""

from __future__ import annotations

import pytest

from jasper.fanin.ring_health import (
    ring_edge_width_ready,
    ring_wire_caps_ready,
)
from jasper.fanin_coupling import (
    COUPLING_ENV_VAR,
    COUPLING_SHM_RING,
    OUTPUTD_CONTENT_BRIDGE_ENV_VAR,
)

# Reuse the reconcile suite's hermetic env isolation, daemon recorder, and the
# helper that forces the non-wire preflights to pass. Redefining them here would
# be a second answer to "what does an armable box look like".
from tests.test_fanin_coupling_reconcile import (
    SHIPPED_RING_CONF_D,
    _write,
    force_ring_gates_pass,
    isolate_base_jasper_env,
)

# Captured at import, BEFORE any fixture can stub the module attribute — see
# :func:`_real_caps_record_compare`.
from jasper.ring_assets import ring_ioplug_wire_supported as _REAL_WIRE_SUPPORTED


@pytest.fixture(autouse=True)
def _isolate_base_jasper_env(tmp_path, monkeypatch):
    """These tests resolve fan-in's wire format through the jasper.env ->
    fanin.env chain, so the developer host's /etc state must not reach them."""
    isolate_base_jasper_env(tmp_path, monkeypatch)


@pytest.fixture
def _ring_assets_present(monkeypatch):
    """Every non-wire ring preflight passes, so these tests exercise the gates
    they are about rather than the asset/geometry gates ahead of them."""
    force_ring_gates_pass(monkeypatch)


def _wide_wire(monkeypatch):
    """Resolve a wire that renders a conf.d field a pre-ring-v2 ioplug refuses.

    Since the resolver's default went wide this is what an isolated env already
    answers; it stays explicit so these tests state the wire they are about
    rather than inheriting it.
    """
    import jasper.fanin_coupling as fc

    monkeypatch.setattr(
        fc,
        "resolve_ring_wire",
        lambda topology=None: fc.RingWire(
            sample_format="S32_LE",
            ring_a_channels=2,
            ring_b_channels=2,
            period_frames=fc.RING_SLOT_FRAMES,
        ),
    )


def _narrow_pin(monkeypatch):
    """The OPERATOR'S ROLLBACK PIN — the one wire that still needs no capability.

    ``JASPER_FANIN_RING_WIRE_FORMAT=S16_LE`` resolves the token the C ioplug
    compiles in, so the wire forces no ``format`` key by the capability
    predicate's rule. Before the resolver's default went wide this was every
    box; it is now a deliberate act.
    """
    import jasper.fanin_coupling as fc

    monkeypatch.setattr(
        fc,
        "resolve_ring_wire",
        lambda topology=None: fc.RingWire(
            sample_format=fc.RING_WIRE_FORMAT,
            ring_a_channels=2,
            ring_b_channels=2,
            period_frames=fc.RING_SLOT_FRAMES,
        ),
    )


def _real_caps_record_compare(monkeypatch):
    """Undo ``force_ring_gates_pass``'s stub of the ioplug RECORD compare.

    That helper stubs ``ring_ioplug_wire_supported`` so the spine tests are not
    refused by a gate that went live when the ring wire's default widened. A
    test whose SUBJECT is that refusal has to put the real predicate back, or it
    would assert against its own stub.
    """
    import jasper.ring_assets as ra

    monkeypatch.setattr(ra, "ring_ioplug_wire_supported", _REAL_WIRE_SUPPORTED)


def _armed_env(tmp_path):
    """The env pair a box already converged onto the ring carries.

    outputd.env must hold the COMPLETE reconciler-owned ring key set, not just
    the bridge: a pass that would still move any of them counts as a change and
    takes the ordered spine rather than the no-bounce path. Deriving it from
    ``_outputd_actions`` keeps the fixture honest as that set grows.
    """
    import jasper.fanin.coupling_reconcile as cr

    fanin_env = _write(
        tmp_path / "fanin.env", f"{COUPLING_ENV_VAR}={COUPLING_SHM_RING}\n"
    )
    outputd_env = _write(
        tmp_path / "outputd.env",
        cr._apply_actions("", cr._outputd_actions(""))[0],
    )
    assert OUTPUTD_CONTENT_BRIDGE_ENV_VAR in outputd_env.read_text()
    return fanin_env, outputd_env


# --- the ioplug CAPABILITY gate ---------------------------------------------


def test_caps_gate_is_inert_only_on_an_operator_narrow_pin(monkeypatch):
    """What is LEFT of this gate's dormancy, stated as a behaviour.

    A wire at the ioplug's own compiled-in token renders no conf.d field beyond
    its defaults, so the gate answers ok WITHOUT reading a provenance record or
    hashing a plugin. That short-circuit used to describe the whole fleet. Since
    the ring wire's resolver defaults WIDE it describes exactly one box: one an
    operator has pinned back to `S16_LE`.
    """
    _narrow_pin(monkeypatch)
    ok, detail = ring_wire_caps_ready()
    assert ok is True
    assert "no conf.d field beyond" in detail


def test_caps_gate_is_live_on_an_undeclared_box(monkeypatch, tmp_path):
    """THE FLIP'S FLEET CONSEQUENCE, at the gate that acts on it.

    An undeclared box resolves the wide wire, which differs from the ioplug's
    compiled-in conf.d default, so its conf.d carries a `format` line and this
    gate performs a real record compare on every pass. A box whose last deploy
    took the ioplug-build WARN is REFUSED here — a roleful box's content lane
    parks (ADR-0178) — instead of arming into a CamillaDSP that cannot open
    the ring.

    The wire is NOT stubbed here: it is resolved from the (isolated, empty) env
    chain exactly as a real undeclared box resolves it, so this fails if the
    resolver's default is ever moved back without moving this pin.
    """
    import jasper.ring_assets as ra

    _real_caps_record_compare(monkeypatch)
    monkeypatch.setattr(ra, "RING_IOPLUG_PROVENANCE", str(tmp_path / "absent"))
    ok, detail = ring_wire_caps_ready()
    assert ok is False
    assert "no provenance record" in detail
    # The refusal is ABOUT the wide wire, not about some other axis.
    assert "S32_LE" in detail


def test_caps_gate_refuses_a_wide_wire_with_no_record(monkeypatch, tmp_path):
    import jasper.ring_assets as ra

    _real_caps_record_compare(monkeypatch)
    _wide_wire(monkeypatch)
    monkeypatch.setattr(ra, "RING_IOPLUG_PROVENANCE", str(tmp_path / "absent"))
    ok, detail = ring_wire_caps_ready()
    assert ok is False
    assert "no provenance record" in detail


# --- the slot migration declines when the WIRE is sheared -------------------


def _migrate(tmp_path, monkeypatch, *, fanin_text: str):
    """Run the slot migration against ``fanin_text`` on the SHIPPED conf.d.

    Returns (post_migration_text, records) where ``records`` is the log_event
    result tokens the migration emitted — the migration's only externally
    visible statement about what it decided.
    """
    import jasper.fanin.coupling_reconcile as cr
    import jasper.ring_assets as ra

    monkeypatch.setattr(ra, "RING_CONF_D", str(SHIPPED_RING_CONF_D))
    path = _write(tmp_path / "fanin.env", fanin_text)

    records: list[str] = []
    real_log_event = cr.log_event

    def _capture(logger, event, **kw):
        records.append(str(kw.get("result", "")))
        return real_log_event(logger, event, **kw)

    monkeypatch.setattr(cr, "log_event", _capture)
    snapshot = cr._read_snapshot(path)
    result, _healed = cr._migrate_stale_fanin_ring_slots(snapshot, "t")
    return result.text, records


def test_slot_migration_writes_the_coherent_value_when_only_slots_are_stale(
    tmp_path, monkeypatch
):
    """POSITIVE CONTROL for the decline below: the migration does fire.

    Without this, a decline test proves only that the function wrote nothing —
    which a broken migration that never writes would also satisfy.
    """
    from jasper.fanin_coupling import RING_SLOTS_ENV_VAR

    text, records = _migrate(
        tmp_path, monkeypatch, fanin_text=f"{RING_SLOTS_ENV_VAR}=8\n"
    )
    assert "stale_ring_slots_overridden" in records
    assert f"{RING_SLOTS_ENV_VAR}=2" in text


def test_slot_migration_declines_when_the_wire_format_is_sheared(
    tmp_path, monkeypatch
):
    """It does not converge an axis it does not own, and says so.

    Writing the slot count while fan-in and the conf.d disagree about the WIRE
    would make the geometry look repaired — the operator reads
    ``stale_ring_slots_overridden`` as progress — while the arm still cannot
    succeed. The wire gate is the one that refuses with the reason that actually
    describes the box, so the migration steps aside and leaves it to say so.

    THE SHEAR IS SPELLED THE OTHER WAY ROUND NOW. The shipped conf.d declares
    the WIDE wire, so declaring ``S32_LE`` here would AGREE with it and shear
    nothing. The operator's narrow pin is what disagrees with the shipped file
    — same two ends, same disagreement, opposite tokens.
    """
    from jasper.fanin_coupling import (
        RING_SLOTS_ENV_VAR,
        RING_WIRE_FORMAT,
        RING_WIRE_FORMAT_ENV_VAR,
    )

    text, records = _migrate(
        tmp_path,
        monkeypatch,
        # Both true at once: a stale slot count the migration WOULD write, and a
        # wire shear that must stop it.
        fanin_text=(
            f"{RING_SLOTS_ENV_VAR}=8\n"
            f"{RING_WIRE_FORMAT_ENV_VAR}={RING_WIRE_FORMAT}\n"
        ),
    )
    assert "stale_ring_slots_override_declined" in records
    assert "stale_ring_slots_overridden" not in records
    assert f"{RING_SLOTS_ENV_VAR}=8" in text, (
        "the declined migration must leave the stale value alone, not half-write it"
    )


def test_slot_migration_declines_on_a_sheared_channel_count(tmp_path, monkeypatch):
    """The channels axis declines the write for the same reason the format does."""
    from jasper.fanin_coupling import RING_SLOTS_ENV_VAR
    import jasper.ring_assets as ra

    # A conf.d whose Ring-A block declares a channel count fan-in's fixed-stereo
    # mixer cannot produce.
    conf = tmp_path / "sheared.conf"
    conf.write_text(
        "pcm.jts_ring_capture {\n    period_frames 128\n    n_slots 2\n"
        "    channels 4\n}\n"
        "pcm.jts_ring_playback {\n    period_frames 128\n    n_slots 2\n}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ra, "RING_CONF_D", str(conf))

    import jasper.fanin.coupling_reconcile as cr

    path = _write(tmp_path / "fanin.env", f"{RING_SLOTS_ENV_VAR}=8\n")
    records: list[str] = []
    real_log_event = cr.log_event
    monkeypatch.setattr(
        cr,
        "log_event",
        lambda logger, event, **kw: (
            records.append(str(kw.get("result", ""))) or real_log_event(
                logger, event, **kw
            )
        ),
    )
    out, healed = cr._migrate_stale_fanin_ring_slots(cr._read_snapshot(path), "t")
    assert "stale_ring_slots_override_declined" in records
    assert healed is False
    assert f"{RING_SLOTS_ENV_VAR}=8" in out.text


# --- the sample_format axis at BOTH on-disk-header consumers ----------------


def _ring_file(path, *, sample_format, n_slots=2, period=128, channels=2):
    """Write a valid ring header (JRIN magic) with the given geometry."""
    import struct

    import jasper.ring_assets as ra

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


def _point_ring_files_at(monkeypatch, tmp_path):
    """Repoint both ring files into the tmpdir. Returns (ring_a, ring_b)."""
    import jasper.ring_assets as ra

    ring_a = tmp_path / "program.ring"
    ring_b = tmp_path / "content.ring"
    monkeypatch.setattr(ra, "RING_A_PROGRAM_FILE", str(ring_a))
    monkeypatch.setattr(ra, "RING_B_CONTENT_FILE", str(ring_b))
    monkeypatch.setattr(ra, "RING_CONF_D", str(SHIPPED_RING_CONF_D))
    return ring_a, ring_b


def test_stale_file_guard_deletes_a_format_mismatched_ring(tmp_path, monkeypatch):
    """WHAT MAKES THE WIRE ROLLBACK LEVER ONE-SHOT-ABLE, as a behaviour.

    While this guard was blind to ``sample_format``, forcing the wire narrow
    again left the WIDE file on disk: the writer rejected it at attach as a
    config-class fault and the box PARKED until someone ran ``rm`` by hand — so
    the lever worked once and then needed an operator. The file must be cleared
    here, on an axis where slots and period both still match.

    THE STALE TOKEN IS THE NARROW ONE NOW. The resolved wire is wide on an
    undeclared box, so a leftover S16 header is what disagrees with it — the
    same guard, the same axis, the roles swapped by the resolver's default. The
    rollback direction the docstring describes is now the routine one, and it
    lands on the OTHER file: an operator pinning narrow leaves a wide header
    behind, which this guard clears the same way.
    """
    import jasper.fanin.coupling_reconcile as cr
    import jasper.ring_assets as ra

    ring_a, ring_b = _point_ring_files_at(monkeypatch, tmp_path)
    # Slots and period MATCH the shipped conf.d; only the format is stale. A
    # guard that compared the old two axes would leave this file in place.
    _ring_file(ring_a, sample_format=ra.RING_SAMPLE_FORMAT_S16LE)
    _ring_file(ring_b, sample_format=ra.RING_SAMPLE_FORMAT_S32LE)

    cr._delete_stale_ring_files("t", "")

    assert not ring_a.exists(), "a format-stale ring file must be deleted"
    assert ring_b.exists(), "a coherent ring file must be left alone"


# --- the four-ends wire gate, per end ---------------------------------------


def test_wire_gate_names_the_end_that_disagrees(monkeypatch):
    """A refusal must say WHICH end declared what. A bare "mismatch" leaves an
    operator with four files to read and no order to read them in.

    The disagreeing token is the NARROW one now: the shipped conf.d and the
    resolver both answer wide, so a fan-in snapshot still carrying an ``S16_LE``
    declaration is the end out of step.
    """
    import jasper.ring_assets as ra

    monkeypatch.setattr(ra, "RING_CONF_D", str(SHIPPED_RING_CONF_D))
    ok, detail = ring_edge_width_ready(
        fanin_text="JASPER_FANIN_RING_WIRE_FORMAT=S16_LE\n", outputd_text=""
    )
    assert ok is False
    assert "fan-in (Ring A writer)" in detail
    assert "S16_LE" in detail


def test_wire_gate_compares_outputd_only_once_armed(monkeypatch):
    """THE PR-1 DEFECT, structurally prevented.

    ``JASPER_OUTPUTD_CONTENT_FORMAT`` is written by the audio-hardware
    reconciler, not by this one, so a not-yet-armed box's value is simply
    whatever that reconciler last rendered — not yet proven to match THIS
    arm. Comparing it at preflight would refuse the arm on every box in the
    fleet — the exact shape of the defect this gate's history records. Same
    file, two verdicts, decided by whether the box is already armed.

    The stale token is ``S16_LE`` now — since the ring wire's resolver
    defaults wide, an unarmed box's leftover narrow declaration is what an
    armed box must be refused for. The unarmed half of the test is what
    proves the verdict is decided by ``armed`` and not by the token.
    """
    import jasper.ring_assets as ra

    monkeypatch.setattr(ra, "RING_CONF_D", str(SHIPPED_RING_CONF_D))
    loopback_outputd = "JASPER_OUTPUTD_CONTENT_FORMAT=S16_LE\n"

    ok_unarmed, _ = ring_edge_width_ready(fanin_text="", outputd_text=loopback_outputd)
    assert ok_unarmed is True, "a not-yet-armed box must not be refused for this"

    ok_armed, detail = ring_edge_width_ready(
        fanin_text=f"{COUPLING_ENV_VAR}={COUPLING_SHM_RING}\n",
        outputd_text=loopback_outputd,
    )
    assert ok_armed is False
    assert "outputd (Ring B reader)" in detail


def test_wire_gate_reads_an_absent_outputd_key_as_the_daemon_default(monkeypatch):
    """An unset ``JASPER_OUTPUTD_CONTENT_FORMAT`` DECLARES ``S16_LE``.

    That is outputd's own compiled-in fallback
    (``rust/jasper-outputd/src/config.rs``), not an unknown, and reading absence
    as a DECLARATION rather than as indeterminate is the property this pins.

    WHAT THE DECLARATION NOW MEANS. While the ring wire was narrow by default,
    that fallback happened to agree with the resolved wire and an armed box with
    no key written passed. Since the resolver defaults WIDE it disagrees — and
    the refusal is correct, not a false alarm: outputd really would read S16
    slots out of an S32 ring. The remedy is the hardware reconciler, which is
    that key's single writer and re-derives it from the coupling on every pass.
    The positive control below is what keeps this a test of the COMPARISON
    rather than of "absence always refuses".
    """
    import jasper.ring_assets as ra

    monkeypatch.setattr(ra, "RING_CONF_D", str(SHIPPED_RING_CONF_D))
    armed = f"{COUPLING_ENV_VAR}={COUPLING_SHM_RING}\n"

    ok, detail = ring_edge_width_ready(fanin_text=armed, outputd_text="")
    assert ok is False
    assert "outputd (Ring B reader)" in detail
    # The gate read the ABSENT key as the daemon's own token, not as "unknown".
    assert "S16_LE" in detail

    # Positive control: the key the hardware reconciler writes on an armed box
    # agrees with the resolved wire, and the same gate is silent.
    ok, detail = ring_edge_width_ready(
        fanin_text=armed, outputd_text="JASPER_OUTPUTD_CONTENT_FORMAT=S32_LE\n"
    )
    assert ok is True, detail


def test_wire_gate_refuses_an_outputd_channel_width_the_ring_does_not_carry(
    monkeypatch,
):
    """The channels axis has teeth independently of the format axis."""
    import jasper.ring_assets as ra

    monkeypatch.setattr(ra, "RING_CONF_D", str(SHIPPED_RING_CONF_D))
    ok, detail = ring_edge_width_ready(
        fanin_text="", outputd_text="JASPER_OUTPUTD_ACTIVE_CHANNELS=6\n"
    )
    assert ok is False
    assert "6 channels" in detail
    assert "outputd (Ring B reader)" in detail


def test_wire_gate_defers_an_absent_conf_d_to_the_asset_gate(monkeypatch, tmp_path):
    """One missing file, one reason. ``ring_assets_ready`` owns the absent
    conf.d; a second refusal here would bury the one that names the fix."""
    import jasper.ring_assets as ra

    monkeypatch.setattr(ra, "RING_CONF_D", str(tmp_path / "nope.conf"))
    monkeypatch.setattr(
        ra, "ring_asset_presence", lambda **kw: ra.RingAssetPresence(True, False, True)
    )
    ok, _ = ring_edge_width_ready(fanin_text="", outputd_text="")
    assert ok is True


def test_wire_gate_refuses_a_conf_d_that_is_present_but_unreadable(
    monkeypatch, tmp_path
):
    """A torn conf.d is nobody else's refusal to own, so it stays this gate's."""
    import jasper.ring_assets as ra

    monkeypatch.setattr(ra, "RING_CONF_D", str(tmp_path / "torn.conf"))
    monkeypatch.setattr(
        ra, "ring_asset_presence", lambda **kw: ra.RingAssetPresence(True, True, True)
    )
    ok, detail = ring_edge_width_ready(fanin_text="", outputd_text="")
    assert ok is False
    assert "declares no format at all" in detail


def test_wire_gate_refuses_an_indeterminate_channel_count_like_an_indeterminate_format(
    monkeypatch, tmp_path
):
    """SYMMETRY. The two axes must treat "cannot be read" the same way.

    The reachable shape is a PRESENT conf.d whose block declares ``channels``
    twice with different values — ``ring_conf_channels`` answers None for
    exactly that torn file. The format axis already refused such a block; the
    channels axis passed it silently, so a box could arm on a channel count
    nothing had actually agreed. Note the format here is single and CORRECT, so
    the refusal can only be coming from the channels axis.
    """
    import jasper.ring_assets as ra

    torn = tmp_path / "torn.conf"
    torn.write_text(
        "pcm.jts_ring_capture {\n    period_frames 128\n    n_slots 2\n"
        "    format S16_LE\n    channels 2\n    channels 4\n}\n"
        "pcm.jts_ring_playback {\n    period_frames 128\n    n_slots 2\n}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ra, "RING_CONF_D", str(torn))
    monkeypatch.setattr(
        ra, "ring_asset_presence", lambda **kw: ra.RingAssetPresence(True, True, True)
    )
    ok, detail = ring_edge_width_ready(fanin_text="", outputd_text="")
    assert ok is False
    assert "declares no channel count at all" in detail
    assert "jts_ring_capture" in detail
    # The format axis is fine on this file — a refusal citing it would mean the
    # test proved the wrong branch.
    assert "declares no format at all" not in detail


def test_wire_gate_refuses_an_outputd_channel_count_that_will_not_parse(monkeypatch):
    """The other reachable indeterminate: a malformed outputd channels value.

    This is why the excuse is a PER-AXIS flag rather than a reuse of ``note``.
    The outputd end carries a note explaining why its FORMAT is not compared
    before arming — and if that note also excused its channels, a value that
    will not parse as an int would pass here on every unarmed box, which is
    every box about to arm.
    """
    import jasper.ring_assets as ra

    monkeypatch.setattr(ra, "RING_CONF_D", str(SHIPPED_RING_CONF_D))
    ok, detail = ring_edge_width_ready(
        fanin_text="", outputd_text="JASPER_OUTPUTD_ACTIVE_CHANNELS=stereo\n"
    )
    assert ok is False
    assert "outputd (Ring B reader)" in detail
    assert "declares no channel count at all" in detail


def test_wire_gate_does_not_invent_a_channels_refusal_for_ends_that_state_none(
    monkeypatch,
):
    """The CONTROL for the symmetry above: two ends legitimately say nothing.

    ``CamillaDSP emitted stanzas`` carries a format and no channel count — the
    coupling's kwargs simply have none — and an ABSENT conf.d states nothing on
    either axis while the asset gate owns that refusal. Neither may be reported
    as indeterminate, or the shipped fleet fails a gate it has always passed.
    """
    import jasper.ring_assets as ra

    monkeypatch.setattr(ra, "RING_CONF_D", str(SHIPPED_RING_CONF_D))
    ok, detail = ring_edge_width_ready(fanin_text="", outputd_text="")
    assert ok is True, detail


def test_wire_gate_passes_on_the_shipped_wire(monkeypatch):
    """The dormancy bar for the wire gate: a fleet box declares one wire at every
    end, so nothing about this rung changes what it does."""
    import jasper.ring_assets as ra

    monkeypatch.setattr(ra, "RING_CONF_D", str(SHIPPED_RING_CONF_D))
    ok, detail = ring_edge_width_ready(fanin_text="", outputd_text="")
    assert ok is True, detail
    assert "declaring ends state one ring wire" in detail
    # An unarmed fleet box loads a NON-ring graph, so the graph is not one of
    # the ends — and the message says which ends it actually had, by name.
    assert "fan-in (Ring A writer)" in detail
    assert "outputd (Ring B reader)" in detail
    assert "loaded CamillaDSP graph was NOT one of them" in detail


# --- the LOADED graph as a declaring end (defect B) --------------------------


def _mono_two_way_topology():
    """The jts3 shape: a roleful mono 2-way on a single coherent 8-ch DAC.

    Built from the suite's shared fixture rather than re-declared here, so the
    active-ring width this gate is held to is the same one every other
    active-speaker test means by "the bench box".
    """
    from tests.active_speaker_fixtures import mono_output_topology

    return mono_output_topology()


def _ring_graph_text(*, device, sample_format, channels=2):
    return (
        "---\n"
        "devices:\n"
        "  samplerate: 48000\n"
        "  chunksize: 128\n"
        "  target_level: 128\n"
        "  capture:\n"
        "    type: Alsa\n"
        "    channels: 2\n"
        '    device: "plug:jasper_capture"\n'
        "    format: S32_LE\n"
        "  playback:\n"
        "    type: Alsa\n"
        f"    channels: {channels}\n"
        f'    device: "{device}"\n'
        f"    format: {sample_format}\n"
    )


def test_wire_gate_refuses_the_jts3_graph_shear_and_names_the_graph_end(
    monkeypatch, tmp_path
):
    """THE DEFECT-B SHAPE: the loaded ACTIVE-ring graph declares a wire the
    resolver does not, while every env end agrees.

    HISTORY (unchanged, and the reason this test exists). On jts3 (2026-08-11,
    ``captures/r7b-jts3-arm2-20260811T132227Z`` files 12 and 13) the resolver
    said ``S16_LE`` and the graph said ``S32_LE``; the box returned ``(True,
    'all declaring ends state one ring wire (S16_LE, Ring A 2ch, Ring B 2ch)')``
    — the gate proved two of the three ends that mattered and reported three, so
    step 3 would have attached CamillaDSP to the ring at ``S32_LE`` against an
    ioplug opening at its ``S16_LE`` default.

    THE TOKENS ARE SWAPPED HERE, the shape is not. Since the ring wire's
    resolver defaults WIDE, a graph declaring ``S32_LE`` now AGREES and would
    shear nothing; the stale narrow graph is the live shape — which is exactly
    what a box carries after this flip until its boot graph is re-emitted,
    because a roleful graph's capture and playback formats are baked when it is
    EMITTED. So this is no longer only archaeology: it is the refusal a
    not-yet-re-emitted box meets, and it must name the file to fix.
    """
    import jasper.ring_assets as ra
    from jasper.fanin_coupling import (
        RING_ACTIVE_PLAYBACK_DEVICE,
        RING_WIRE_FORMAT,
        RING_WIRE_FORMAT_WIDE,
    )

    monkeypatch.setattr(ra, "RING_CONF_D", str(SHIPPED_RING_CONF_D))
    monkeypatch.setattr(
        "jasper.fanin.ring_health.load_topology_for_wire", _mono_two_way_topology
    )
    config = tmp_path / "active-speaker-baseline.yml"
    config.write_text(
        _ring_graph_text(
            device=RING_ACTIVE_PLAYBACK_DEVICE,
            sample_format=RING_WIRE_FORMAT,
        ),
        encoding="utf-8",
    )
    statefile = tmp_path / "outputd-statefile.yml"
    statefile.write_text(f"config_path: {config}\n", encoding="utf-8")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))

    # The gate reads the graph itself when no snapshot is handed to it, so this
    # walks the same path the arm takes.
    ok, detail = ring_edge_width_ready(fanin_text="", outputd_text="")

    assert ok is False, detail
    assert f"loaded CamillaDSP graph (playback {RING_ACTIVE_PLAYBACK_DEVICE})" in detail
    assert f"declares format {RING_WIRE_FORMAT}" in detail
    assert str(config) in detail, "the refusal must name the file to fix"

    # CONTROL: the same box with the resolver's own answer in the graph passes,
    # and the ok message now COUNTS the graph instead of excusing it. Without
    # this, a gate that refused every ring graph would satisfy the assertions
    # above while blocking every legitimate arm.
    config.write_text(
        _ring_graph_text(
            device=RING_ACTIVE_PLAYBACK_DEVICE, sample_format=RING_WIRE_FORMAT_WIDE
        ),
        encoding="utf-8",
    )
    ok, detail = ring_edge_width_ready(fanin_text="", outputd_text="")
    assert ok is True, detail
    assert f"loaded CamillaDSP graph (playback {RING_ACTIVE_PLAYBACK_DEVICE})" in detail
    assert "was NOT one of them" not in detail


def test_wire_gate_refuses_a_graph_whose_active_width_is_not_the_resolved_one(
    monkeypatch, tmp_path
):
    """The CHANNELS axis of the same end, held to the ACTIVE ring's width.

    The active ring's width is a THIRD number — not Ring A's stereo program and
    not Ring B's — so a graph declaring 4 post-crossover outputs on a box whose
    topology drives 2 must be refused against ``ring_active_channels``, never
    quietly compared to a stereo 2 that happens to match.

    The graph's FORMAT is the resolved (wide) one on purpose, so the channels
    axis is the only thing that disagrees — a graph that also sheared on format
    would be refused either way and prove nothing about this axis.
    """
    import jasper.ring_assets as ra
    from jasper.fanin_coupling import (
        RING_ACTIVE_PLAYBACK_DEVICE,
        RING_WIRE_FORMAT_WIDE,
    )

    monkeypatch.setattr(ra, "RING_CONF_D", str(SHIPPED_RING_CONF_D))
    monkeypatch.setattr(
        "jasper.fanin.ring_health.load_topology_for_wire", _mono_two_way_topology
    )
    config = tmp_path / "active-speaker-baseline.yml"
    config.write_text(
        _ring_graph_text(
            device=RING_ACTIVE_PLAYBACK_DEVICE,
            sample_format=RING_WIRE_FORMAT_WIDE,
            channels=4,
        ),
        encoding="utf-8",
    )
    statefile = tmp_path / "outputd-statefile.yml"
    statefile.write_text(f"config_path: {config}\n", encoding="utf-8")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))

    ok, detail = ring_edge_width_ready(fanin_text="", outputd_text="")

    assert ok is False, detail
    assert "declares 4 channels, expected 2" in detail
    assert f"loaded CamillaDSP graph (playback {RING_ACTIVE_PLAYBACK_DEVICE})" in detail


def _three_way_topology():
    """A roleful mono 3-WAY: the one shape where the ring widths disagree.

    Ring B resolves 2 (a roleful box has no stereo ring, so the wire falls back
    to the shipped stereo declaration) while the ACTIVE ring resolves 3. Every
    other fixture in this campaign is a 2-way, where both are 2 — so every pin
    written on one of those passes just as well against code that reads the
    wrong ring's width. This is the fixture that can tell them apart.
    """
    from tests.active_speaker_fixtures import mono_output_topology

    return mono_output_topology(mode="active_3_way")


def _stage_graph(monkeypatch, tmp_path, text):
    """Point the statefile at a graph the gate will read on its own."""
    config = tmp_path / "active-speaker-baseline.yml"
    config.write_text(text, encoding="utf-8")
    statefile = tmp_path / "outputd-statefile.yml"
    statefile.write_text(f"config_path: {config}\n", encoding="utf-8")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))
    return config


def test_wire_gate_holds_the_active_ring_to_its_OWN_width_not_ring_bs(
    monkeypatch, tmp_path
):
    """THE ONE COINCIDENCE THIS CAMPAIGN KEEPS RESTING ON: active width == 2.

    ``_wire_channels_for_ring`` must answer ``ring_active_channels`` for the
    ACTIVE ring, never ``ring_b_channels``. On every 2-way fixture in this suite
    those are both 2, so a mutation swapping them survives the entire file — it
    did survive 306 tests when the resilience lens ran it. A 3-way box is where
    they separate: Ring B resolves 2 (a roleful box has no stereo ring), the
    active ring resolves 3.

    Both directions, because either alone is satisfiable by the wrong constant:
    the CORRECT graph (3 outputs) must be accepted, and a graph declaring Ring
    B's 2 must be REFUSED.
    """
    import jasper.ring_assets as ra
    from jasper.fanin_coupling import (
        RING_ACTIVE_PLAYBACK_DEVICE,
        RING_WIRE_FORMAT_WIDE,
        resolve_ring_wire,
    )

    monkeypatch.setattr(ra, "RING_CONF_D", str(SHIPPED_RING_CONF_D))
    monkeypatch.setattr(
        "jasper.fanin.ring_health.load_topology_for_wire", _three_way_topology
    )

    wire = resolve_ring_wire(_three_way_topology())
    assert wire.ring_active_channels == 3 and wire.ring_b_channels == 2, (
        "this fixture stopped discriminating the two ring widths; the test below "
        "would pass against code reading either one"
    )

    # The box's OWN active width is accepted.
    _stage_graph(
        monkeypatch,
        tmp_path,
        _ring_graph_text(
            device=RING_ACTIVE_PLAYBACK_DEVICE,
            sample_format=RING_WIRE_FORMAT_WIDE,
            channels=3,
        ),
    )
    ok, detail = ring_edge_width_ready(fanin_text="", outputd_text="")
    assert ok is True, detail

    # Ring B's width is NOT the active ring's, and stating it is refused —
    # naming the active end and both numbers.
    _stage_graph(
        monkeypatch,
        tmp_path,
        _ring_graph_text(
            device=RING_ACTIVE_PLAYBACK_DEVICE,
            sample_format=RING_WIRE_FORMAT_WIDE,
            channels=2,
        ),
    )
    ok, detail = ring_edge_width_ready(fanin_text="", outputd_text="")
    assert ok is False, detail
    assert "declares 2 channels, expected 3" in detail
    assert f"loaded CamillaDSP graph (playback {RING_ACTIVE_PLAYBACK_DEVICE})" in detail


def test_wire_gate_holds_a_non_ring_graph_to_nothing(monkeypatch, tmp_path):
    """The dormancy control for the graph end.

    A loaded graph on the ALSA active lane declares S32_LE for a transport that
    is not the ring. Holding it to the ring's wire would refuse the arm on every
    box that has not run step 1 yet — the PR-1 defect shape, re-introduced from
    the other side.
    """
    import jasper.ring_assets as ra

    monkeypatch.setattr(ra, "RING_CONF_D", str(SHIPPED_RING_CONF_D))
    monkeypatch.setattr(
        "jasper.fanin.ring_health.load_topology_for_wire", _mono_two_way_topology
    )
    config = tmp_path / "active-speaker-baseline.yml"
    config.write_text(
        _ring_graph_text(
            device="outputd_active_content_playback", sample_format="S32_LE"
        ),
        encoding="utf-8",
    )
    statefile = tmp_path / "outputd-statefile.yml"
    statefile.write_text(f"config_path: {config}\n", encoding="utf-8")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))

    ok, detail = ring_edge_width_ready(fanin_text="", outputd_text="")

    assert ok is True, detail
    assert "it names no ring PCM on either lane" in detail


def test_a_declared_narrow_pin_moves_the_resolver_and_the_refusal(
    monkeypatch, tmp_path
):
    """The R6/R7 activation input, walked: one env key moves the whole answer.

    Before PR #2335 ``resolve_ring_wire`` pinned the format narrow with no
    input, so declaring a wire to ``jasper-fanin`` moved fan-in and nothing else
    — the arm was unreachable rather than refused. The resolver reads the same
    key the daemon does now, so the declaration moves the WHOLE answer, and the
    refusal lands on whichever end has not caught up.

    THE DECLARATION THAT DOES THIS IS THE NARROW PIN NOW. With the resolver
    defaulting wide and the shipped conf.d spelling ``S32_LE``, declaring wide
    changes nothing to disagree about. An operator's ``S16_LE`` moves the
    resolver to narrow and leaves the still-wide conf.d as the end out of step —
    whose remedy is the hardware reconciler's render, exactly as before. Same
    mechanism, same remedy, the tokens exchanged.
    """
    import jasper.ring_assets as ra
    from jasper.fanin import coupling_reconcile as cr
    from jasper.fanin_coupling import (
        RING_WIRE_FORMAT,
        RING_WIRE_FORMAT_ENV_VAR,
        resolve_ring_wire,
    )

    monkeypatch.setattr(ra, "RING_CONF_D", str(SHIPPED_RING_CONF_D))
    fanin_env = tmp_path / "fanin.env"
    fanin_env.write_text(
        f"{RING_WIRE_FORMAT_ENV_VAR}={RING_WIRE_FORMAT}\n", encoding="utf-8"
    )
    monkeypatch.setattr(cr, "FANIN_ENV_PATH", str(fanin_env))
    monkeypatch.setattr(
        "jasper.fanin.ring_health.FANIN_ENV_PATH", str(fanin_env)
    )

    assert resolve_ring_wire().sample_format == RING_WIRE_FORMAT

    ok, detail = ring_edge_width_ready(
        fanin_text=fanin_env.read_text(encoding="utf-8"), outputd_text=""
    )
    assert ok is False, detail
    assert f"resolves to {RING_WIRE_FORMAT}" in detail
    # fan-in agrees (it IS the input); the shipped conf.d does not.
    assert "fan-in (Ring A writer)" not in detail
    assert "conf.d jts_ring_capture" in detail


def test_an_unparseable_declared_wire_refuses_instead_of_raising(
    monkeypatch, tmp_path
):
    """A typo must REFUSE the arm, not traceback out of it.

    ``resolve_ring_wire`` fails loud on a token neither language recognizes —
    correct for an emitter, and the same verdict ``jasper-fanin`` reaches before
    parking. But the arm has already written the ring env by the time the
    preflights run, so an uncaught exception here would skip the snapshot restore
    that makes a refused arm non-destructive: the box would be left holding the
    partial flip. Both wire-reading gates resolve through ``resolve_wire_for_gate``
    for exactly that reason.
    """
    import jasper.ring_assets as ra
    from jasper.fanin import coupling_reconcile as cr
    from jasper.fanin_coupling import RING_WIRE_FORMAT_ENV_VAR

    monkeypatch.setattr(ra, "RING_CONF_D", str(SHIPPED_RING_CONF_D))
    fanin_env = tmp_path / "fanin.env"
    fanin_env.write_text(f"{RING_WIRE_FORMAT_ENV_VAR}=s16le\n", encoding="utf-8")
    monkeypatch.setattr(cr, "FANIN_ENV_PATH", str(fanin_env))
    monkeypatch.setattr(
        "jasper.fanin.ring_health.FANIN_ENV_PATH", str(fanin_env)
    )

    for gate in (cr.ring_edge_width_ready, cr.ring_wire_caps_ready):
        ok, detail = gate()
        assert ok is False, f"{gate.__name__} did not refuse"
        assert RING_WIRE_FORMAT_ENV_VAR in detail
        # Fails closed (ADR-0100), never a fallback — the reason code this gate
        # actually carries, not the English sentence around it.
        assert "ADR-0100" in detail


def test_wire_gate_says_so_when_it_could_not_read_the_graph(monkeypatch, tmp_path):
    """An unreadable graph costs the MESSAGE its claim, never the arm its verdict.

    A fresh box has no statefile at all, so refusing here would refuse the
    unattended pass on every new speaker. What must not happen is the gate
    reporting agreement it never checked — which is defect B in one sentence.
    """
    import jasper.ring_assets as ra

    monkeypatch.setattr(ra, "RING_CONF_D", str(SHIPPED_RING_CONF_D))
    monkeypatch.setattr(
        "jasper.fanin.ring_health.load_topology_for_wire", _mono_two_way_topology
    )
    statefile = tmp_path / "outputd-statefile.yml"
    statefile.write_text(f"config_path: {tmp_path / 'gone.yml'}\n", encoding="utf-8")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))

    ok, detail = ring_edge_width_ready(fanin_text="", outputd_text="")

    assert ok is True, detail
    assert "was NOT one of them" in detail
    assert "is unreadable" in detail


