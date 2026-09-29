# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The ring's wire gates, and the geometry heals the convergence runs.

Two questions a box has to answer before its graph can attach to the ring:

* can the INSTALLED ioplug parse the wire this box resolves? (a stale ``.so``
  beside new daemons — the build degrades to a WARN, so this is the ordinary
  shape of a bad deploy, not an exotic one);
* does every declaring end state the SAME wire?

The reconciler also repairs geometry-mismatched on-disk ring files.
"""

from __future__ import annotations

import pytest

from jasper import ring_header
from jasper.fanin.ring_readiness import (
    ring_edge_width_ready,
    ring_wire_caps_ready,
)
from jasper.dsp_control.fanin_coupling import (
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
from jasper.audio_control.ring_assets import ring_ioplug_wire_supported as _REAL_WIRE_SUPPORTED
from jasper.ring_header import (
    RING_SAMPLE_FORMAT_NAMES,
    RING_SAMPLE_FORMAT_S16LE,
)


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
    import jasper.dsp_control.fanin_coupling as fc

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


def _real_caps_record_compare(monkeypatch):
    """Undo ``force_ring_gates_pass``'s stub of the ioplug RECORD compare.

    That helper stubs ``ring_ioplug_wire_supported`` so the spine tests are not
    refused by a gate that went live when the ring wire's default widened. A
    test whose SUBJECT is that refusal has to put the real predicate back, or it
    would assert against its own stub.
    """
    import jasper.audio_control.ring_assets as ra

    monkeypatch.setattr(ra, "ring_ioplug_wire_supported", _REAL_WIRE_SUPPORTED)


def _armed_env(tmp_path):
    """The env pair a box already converged onto the ring carries.

    outputd.env must hold the COMPLETE reconciler-owned ring key set, not just
    the bridge: a pass that would still move any of them counts as a change and
    takes the ordered spine rather than the no-bounce path. Deriving it from
    ``_outputd_actions`` keeps the fixture honest as that set grows.
    """
    import jasper.fanin.coupling_reconcile as cr

    fanin_env = _write(tmp_path / "fanin.env", "")
    outputd_env = _write(
        tmp_path / "outputd.env",
        cr._apply_actions("", cr._outputd_actions(""))[0],
    )
    assert OUTPUTD_CONTENT_BRIDGE_ENV_VAR in outputd_env.read_text()
    return fanin_env, outputd_env


# --- the ioplug CAPABILITY gate ---------------------------------------------


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
    import jasper.audio_control.ring_assets as ra

    _real_caps_record_compare(monkeypatch)
    monkeypatch.setattr(ra, "RING_IOPLUG_PROVENANCE", str(tmp_path / "absent"))
    ok, detail = ring_wire_caps_ready()
    assert ok is False
    assert "no provenance record" in detail
    # The refusal is ABOUT the wide wire, not about some other axis.
    assert "S32_LE" in detail


def test_caps_gate_refuses_a_wide_wire_with_no_record(monkeypatch, tmp_path):
    import jasper.audio_control.ring_assets as ra

    _real_caps_record_compare(monkeypatch)
    _wide_wire(monkeypatch)
    monkeypatch.setattr(ra, "RING_IOPLUG_PROVENANCE", str(tmp_path / "absent"))
    ok, detail = ring_wire_caps_ready()
    assert ok is False
    assert "no provenance record" in detail


# --- the sample_format axis at BOTH on-disk-header consumers ----------------


def _ring_file(path, *, sample_format, n_slots=2, period=128, channels=2):
    """Write a valid ring header (JRIN magic) with the given geometry."""
    import struct

    hdr = bytearray(ring_header._RING_HEADER_BYTES)
    struct.pack_into("<I", hdr, ring_header._RING_OFF_MAGIC, 0x4A52_494E)
    struct.pack_into("<I", hdr, ring_header._RING_OFF_VERSION, 1)
    struct.pack_into("<I", hdr, ring_header._RING_OFF_RATE, 48000)
    struct.pack_into("<I", hdr, ring_header._RING_OFF_CHANNELS, channels)
    struct.pack_into("<I", hdr, ring_header._RING_OFF_SAMPLE_FORMAT, sample_format)
    struct.pack_into("<I", hdr, ring_header._RING_OFF_PERIOD_FRAMES, period)
    struct.pack_into("<I", hdr, ring_header._RING_OFF_N_SLOTS, n_slots)
    path.write_bytes(bytes(hdr) + b"\x00" * 256)
    return path


def _point_ring_files_at(monkeypatch, tmp_path):
    """Repoint both ring files into the tmpdir. Returns (ring_a, ring_b)."""
    import jasper.audio_control.ring_assets as ra

    ring_a = tmp_path / "program.ring"
    ring_b = tmp_path / "content.ring"
    monkeypatch.setattr(ra, "RING_A_PROGRAM_FILE", str(ring_a))
    monkeypatch.setattr(ra, "RING_B_CONTENT_FILE", str(ring_b))
    monkeypatch.setattr(ra, "RING_CONF_D", str(SHIPPED_RING_CONF_D))
    return ring_a, ring_b


def test_stale_file_guard_deletes_a_format_mismatched_ring(tmp_path, monkeypatch):
    """A stale S16 header is removed even when slots and period still match."""
    import jasper.fanin.coupling_reconcile as cr

    ring_a, ring_b = _point_ring_files_at(monkeypatch, tmp_path)
    # Slots and period MATCH the shipped conf.d; only the format is stale. A
    # guard that compared the old two axes would leave this file in place.
    _ring_file(ring_a, sample_format=ring_header.RING_SAMPLE_FORMAT_S16LE)
    _ring_file(ring_b, sample_format=ring_header.RING_SAMPLE_FORMAT_S32LE)

    cr._delete_stale_ring_files("t", "")

    assert not ring_a.exists(), "a format-stale ring file must be deleted"
    assert ring_b.exists(), "a coherent ring file must be left alone"


# --- the four-ends wire gate, per end ---------------------------------------


@pytest.mark.parametrize(
    ("outputd_text", "expected_substrings"),
    [
        pytest.param(
            "JASPER_OUTPUTD_ACTIVE_CHANNELS=6\n",
            ("6 channels", "outputd (Ring B reader)"),
            id="outputd_channels_the_ring_does_not_carry",
        ),
        pytest.param(
            "JASPER_OUTPUTD_ACTIVE_CHANNELS=stereo\n",
            ("outputd (Ring B reader)", "declares no channel count at all"),
            id="outputd_channels_will_not_parse",
        ),
    ],
)
def test_wire_gate_names_the_end_that_disagrees(
    monkeypatch, outputd_text, expected_substrings
):
    """A mismatched or unparseable outputd channel count names that end."""
    import jasper.audio_control.ring_assets as ra

    monkeypatch.setattr(ra, "RING_CONF_D", str(SHIPPED_RING_CONF_D))
    ok, detail = ring_edge_width_ready(
        outputd_text=outputd_text
    )
    assert ok is False
    for substring in expected_substrings:
        assert substring in detail


def test_wire_gate_compares_outputd_only_once_armed(monkeypatch):
    """THE PR-1 DEFECT, structurally prevented.

    ``JASPER_OUTPUTD_CONTENT_FORMAT`` is written by the audio-hardware
    reconciler, not by this one, so a not-yet-armed box's value is simply
    whatever that reconciler last rendered — not yet proven to match THIS
    arm. Comparing it at preflight would refuse the arm on every box in the
    fleet — the exact shape of the defect this gate's history records. Same
    file, two verdicts, decided by whether the box is already armed.

    The stale token is ``S16_LE`` now — since the ring wire's resolver
    defaults wide, an unreconciled box's leftover narrow declaration is what a
    reconciled box must be refused for. The unreconciled half of the test is
    what proves the verdict is decided by whether the reconciler has written
    outputd.env and not by the token.
    """
    import jasper.audio_control.ring_assets as ra

    monkeypatch.setattr(ra, "RING_CONF_D", str(SHIPPED_RING_CONF_D))
    stale_format = "JASPER_OUTPUTD_CONTENT_FORMAT=S16_LE\n"

    ok_unreconciled, _ = ring_edge_width_ready(outputd_text=stale_format)
    assert ok_unreconciled is True, "an unreconciled box must not be refused for this"

    ok_reconciled, detail = ring_edge_width_ready(
        outputd_text=(
            f"{OUTPUTD_CONTENT_BRIDGE_ENV_VAR}={COUPLING_SHM_RING}\n" + stale_format
        ),
    )
    assert ok_reconciled is False
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
    import jasper.audio_control.ring_assets as ra

    monkeypatch.setattr(ra, "RING_CONF_D", str(SHIPPED_RING_CONF_D))
    reconciled = f"{OUTPUTD_CONTENT_BRIDGE_ENV_VAR}={COUPLING_SHM_RING}\n"

    ok, detail = ring_edge_width_ready(outputd_text=reconciled)
    assert ok is False
    assert "outputd (Ring B reader)" in detail
    # The gate read the ABSENT key as the daemon's own token, not as "unknown".
    assert "S16_LE" in detail

    # Positive control: the key the hardware reconciler writes agrees with the
    # resolved wire, and the same gate is silent.
    ok, detail = ring_edge_width_ready(
        outputd_text=reconciled + "JASPER_OUTPUTD_CONTENT_FORMAT=S32_LE\n",
    )
    assert ok is True, detail


def test_wire_gate_defers_an_absent_conf_d_to_the_asset_gate(monkeypatch, tmp_path):
    """One missing file, one reason. ``ring_assets_ready`` owns the absent
    conf.d; a second refusal here would bury the one that names the fix."""
    import jasper.audio_control.ring_assets as ra

    monkeypatch.setattr(ra, "RING_CONF_D", str(tmp_path / "nope.conf"))
    monkeypatch.setattr(
        ra, "ring_asset_presence", lambda **kw: ra.RingAssetPresence(True, False, True)
    )
    ok, _ = ring_edge_width_ready(outputd_text="")
    assert ok is True


def test_wire_gate_refuses_a_conf_d_that_is_present_but_unreadable(
    monkeypatch, tmp_path
):
    """A torn conf.d is nobody else's refusal to own, so it stays this gate's."""
    import jasper.audio_control.ring_assets as ra

    monkeypatch.setattr(ra, "RING_CONF_D", str(tmp_path / "torn.conf"))
    monkeypatch.setattr(
        ra, "ring_asset_presence", lambda **kw: ra.RingAssetPresence(True, True, True)
    )
    ok, detail = ring_edge_width_ready(outputd_text="")
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
    import jasper.audio_control.ring_assets as ra

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
    ok, detail = ring_edge_width_ready(outputd_text="")
    assert ok is False
    assert "declares no channel count at all" in detail
    assert "jts_ring_capture" in detail
    # The format axis is fine on this file — a refusal citing it would mean the
    # test proved the wrong branch.
    assert "declares no format at all" not in detail


def test_wire_gate_does_not_invent_a_channels_refusal_for_ends_that_state_none(
    monkeypatch,
):
    """The CONTROL for the symmetry above: two ends legitimately say nothing.

    ``CamillaDSP emitted stanzas`` carries a format and no channel count — the
    coupling's kwargs simply have none — and an ABSENT conf.d states nothing on
    either axis while the asset gate owns that refusal. Neither may be reported
    as indeterminate, or the shipped fleet fails a gate it has always passed.
    """
    import jasper.audio_control.ring_assets as ra

    monkeypatch.setattr(ra, "RING_CONF_D", str(SHIPPED_RING_CONF_D))
    ok, detail = ring_edge_width_ready(outputd_text="")
    assert ok is True, detail


def test_wire_gate_passes_on_the_shipped_wire(monkeypatch):
    """The dormancy bar for the wire gate: a fleet box declares one wire at every
    end, so nothing about this rung changes what it does."""
    import jasper.audio_control.ring_assets as ra

    monkeypatch.setattr(ra, "RING_CONF_D", str(SHIPPED_RING_CONF_D))
    ok, detail = ring_edge_width_ready(outputd_text="")
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
    import jasper.audio_control.ring_assets as ra
    from jasper.dsp_control.fanin_coupling import (
        RING_ACTIVE_PLAYBACK_DEVICE,
        RING_WIRE_FORMAT_WIDE,
    )

    monkeypatch.setattr(ra, "RING_CONF_D", str(SHIPPED_RING_CONF_D))
    monkeypatch.setattr(
        "jasper.fanin.ring_readiness.load_topology_for_wire", _mono_two_way_topology
    )
    config = tmp_path / "active-speaker-baseline.yml"
    config.write_text(
        _ring_graph_text(
            device=RING_ACTIVE_PLAYBACK_DEVICE,
            sample_format=RING_SAMPLE_FORMAT_NAMES[RING_SAMPLE_FORMAT_S16LE],
        ),
        encoding="utf-8",
    )
    statefile = tmp_path / "outputd-statefile.yml"
    statefile.write_text(f"config_path: {config}\n", encoding="utf-8")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))

    # The gate reads the graph itself when no snapshot is handed to it, so this
    # walks the same path the arm takes.
    ok, detail = ring_edge_width_ready(outputd_text="")

    assert ok is False, detail
    assert f"loaded CamillaDSP graph (playback {RING_ACTIVE_PLAYBACK_DEVICE})" in detail
    assert f"declares format {RING_SAMPLE_FORMAT_NAMES[RING_SAMPLE_FORMAT_S16LE]}" in detail
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
    ok, detail = ring_edge_width_ready(outputd_text="")
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
    import jasper.audio_control.ring_assets as ra
    from jasper.dsp_control.fanin_coupling import (
        RING_ACTIVE_PLAYBACK_DEVICE,
        RING_WIRE_FORMAT_WIDE,
    )

    monkeypatch.setattr(ra, "RING_CONF_D", str(SHIPPED_RING_CONF_D))
    monkeypatch.setattr(
        "jasper.fanin.ring_readiness.load_topology_for_wire", _mono_two_way_topology
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

    ok, detail = ring_edge_width_ready(outputd_text="")

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
    import jasper.audio_control.ring_assets as ra
    from jasper.dsp_control.fanin_coupling import (
        RING_ACTIVE_PLAYBACK_DEVICE,
        RING_WIRE_FORMAT_WIDE,
        resolve_ring_wire,
    )

    monkeypatch.setattr(ra, "RING_CONF_D", str(SHIPPED_RING_CONF_D))
    monkeypatch.setattr(
        "jasper.fanin.ring_readiness.load_topology_for_wire", _three_way_topology
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
    ok, detail = ring_edge_width_ready(outputd_text="")
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
    ok, detail = ring_edge_width_ready(outputd_text="")
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
    import jasper.audio_control.ring_assets as ra

    monkeypatch.setattr(ra, "RING_CONF_D", str(SHIPPED_RING_CONF_D))
    monkeypatch.setattr(
        "jasper.fanin.ring_readiness.load_topology_for_wire", _mono_two_way_topology
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

    ok, detail = ring_edge_width_ready(outputd_text="")

    assert ok is True, detail
    assert "it names no ring PCM on either lane" in detail


def test_wire_gate_says_so_when_it_could_not_read_the_graph(monkeypatch, tmp_path):
    """An unreadable graph costs the MESSAGE its claim, never the arm its verdict.

    A fresh box has no statefile at all, so refusing here would refuse the
    unattended pass on every new speaker. What must not happen is the gate
    reporting agreement it never checked — which is defect B in one sentence.
    """
    import jasper.audio_control.ring_assets as ra

    monkeypatch.setattr(ra, "RING_CONF_D", str(SHIPPED_RING_CONF_D))
    monkeypatch.setattr(
        "jasper.fanin.ring_readiness.load_topology_for_wire", _mono_two_way_topology
    )
    statefile = tmp_path / "outputd-statefile.yml"
    statefile.write_text(f"config_path: {tmp_path / 'gone.yml'}\n", encoding="utf-8")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))

    ok, detail = ring_edge_width_ready(outputd_text="")

    assert ok is True, detail
    assert "was NOT one of them" in detail
    assert "is unreadable" in detail


def test_topology_read_fails_soft_when_its_module_will_not_import(monkeypatch):
    """An unimportable ``jasper.audio_routes.output_topology`` answers ``None``, not a raise.

    The read defers that module, so the import is one more thing that can fail
    at call time; the exception type it raises with lives in the same module.
    """
    import sys

    from jasper.fanin import ring_readiness as rr

    monkeypatch.setitem(sys.modules, "jasper.audio_routes.output_topology", None)

    assert rr.load_topology_for_wire() is None
