# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Renderer-ingress lane map (U3 / P6a) — the activation rule and its writer.

Three things are pinned here, in rising order of how expensive they are to get
wrong:

1. **The default is nothing.** An unarmed box behaves byte-identically to one
   on which this mechanism does not exist. Every fail-safe direction (missing
   file, empty value, malformed value) resolves to "nothing armed".
2. **Both ends move together.** The renderer's device and fan-in's armed set
   come from one render of one fact, so a half-flip — one end on the ring, the
   other on snd-aloop, i.e. silence — is not representable.
3. **The two language mirrors agree.** fan-in derives the ring path in Rust and
   the conf.d declares it in ALSA config; a divergence would leave fan-in
   reading a ring nothing writes, which presents as a silent source with a
   healthy-looking daemon.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import subprocess
from pathlib import Path

import pytest

from jasper import renderer_lanes as rl
from jasper import ring_assets
from jasper.fanin_coupling import resolve_ring_wire_format
from tests.shairport_template_helpers import SHAIRPORT_TEMPLATE, template_value

REPO = Path(__file__).resolve().parent.parent
FANIN_CONFIG_RS = REPO / "rust" / "jasper-fanin" / "src" / "config.rs"
RING_CAPTURE_RS = REPO / "rust" / "jasper-fanin" / "src" / "mixer" / "ring_capture.rs"
LANES_CONF = REPO / "deploy" / "alsa" / "conf.d" / "61-jts-renderer-lanes.conf"
LIBRESPOT_UNIT = REPO / "deploy" / "systemd" / "librespot.service"
FANIN_UNIT = REPO / "deploy" / "systemd" / "jasper-fanin.service"
TMPFILES = REPO / "deploy" / "tmpfiles" / "jts-ring.conf"
SERVICE_USERS = REPO / "deploy" / "lib" / "install" / "service-users.sh"
RING_PLATFORM = REPO / "deploy" / "lib" / "install" / "ring-platform.sh"
RENDERERS_SH = REPO / "deploy" / "lib" / "install" / "renderers.sh"


# --- 1. The default is nothing -------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [None, "", " ", ",", ",,", "  ,  ,  "],
)
def test_every_empty_shape_means_nothing_armed(raw):
    """Unset, empty, and whitespace/comma-only all mean NO lanes armed.

    This is the fail-safe direction and it is load-bearing in both languages:
    an empty value that read as "arm everything" would flip the whole fleet on
    a malformed write.
    """
    assert rl.parse_armed_labels(raw) == ()


def test_a_missing_lane_map_reads_as_nothing_armed(tmp_path):
    assert rl.read_armed_labels(str(tmp_path / "absent.env")) == ()


def test_an_unreadable_lane_map_reads_as_nothing_armed(tmp_path):
    """A directory where a file should be is unreadable, not empty — and still
    must resolve to the shipped state rather than raising into a doctor check
    or a reconcile pass."""
    d = tmp_path / "lane.env"
    d.mkdir()
    assert rl.read_armed_labels(str(d)) == ()


def test_the_unarmed_render_names_every_lanes_aloop_device():
    text = rl.render_env_text(())
    assert f"{rl.FANIN_RING_LANES_KEY}=\n" in text
    for lane in rl.RENDERER_LANES:
        assert f"{lane.device_key}={lane.aloop_device}\n" in text


# --- 2. Both ends move together ------------------------------------------


def test_arming_moves_both_ends_in_one_render():
    """The renderer's device and fan-in's armed set are derived from ONE fact.

    Mutating either half alone is what produces the failure this test exists to
    make unrepresentable: a renderer writing snd-aloop while fan-in reads a ring
    (silence), or a renderer writing a ring nothing reads (silence).
    """
    text = rl.render_env_text(("spotify",))
    assert f"{rl.FANIN_RING_LANES_KEY}=spotify\n" in text
    assert "JASPER_LIBRESPOT_DEVICE=librespot_ring_lane\n" in text


def test_disarming_writes_the_aloop_device_rather_than_omitting_the_key(tmp_path):
    """A disarm must WRITE the aloop device, not drop the line.

    systemd's EnvironmentFile leaves a previously-set variable in place when a
    later file omits it only if the same file set it — but the failure this
    guards is simpler and worse: if the render omitted the key, a box whose file
    already carried the armed device would keep it after a disarm, and rollback
    would silently do nothing.
    """
    path = str(tmp_path / "lanes.env")
    rl.render_renderer_lanes_env(("spotify",), path=path)
    assert "librespot_ring_lane" in Path(path).read_text()

    rl.render_renderer_lanes_env((), path=path)
    text = Path(path).read_text()
    assert "JASPER_LIBRESPOT_DEVICE=librespot_substream\n" in text
    assert "librespot_ring_lane" not in text
    assert rl.read_armed_labels(path) == ()


def test_the_render_round_trips_through_its_own_reader(tmp_path):
    """The written file IS the intent record — there is no second file that
    could disagree with it."""
    path = str(tmp_path / "lanes.env")
    rl.render_renderer_lanes_env(("spotify",), path=path)
    assert rl.read_armed_labels(path) == ("spotify",)
    # Derived from the registry so a new lane extends this rather than breaking
    # it: the armed lane gets its ring device, every other lane its aloop one.
    expected = {rl.FANIN_RING_LANES_KEY: "spotify"}
    for lane in rl.RENDERER_LANES:
        expected[lane.device_key] = (
            lane.ring_device if lane.label == "spotify" else lane.aloop_device
        )
    assert rl.fanin_env_expectations(path) == expected
    # And the un-armed lanes really are named — an omitted key would leave a
    # previously-armed device in place on the next read.
    assert len(expected) == 1 + len(rl.RENDERER_LANES)


def test_the_render_is_write_on_change(tmp_path):
    path = str(tmp_path / "lanes.env")
    first = rl.render_renderer_lanes_env(("spotify",), path=path)
    assert first.changed is True
    mtime = Path(path).stat().st_mtime_ns

    second = rl.render_renderer_lanes_env(("spotify",), path=path)
    assert second.changed is False
    assert Path(path).stat().st_mtime_ns == mtime, "an unchanged map must not churn the file"


def test_the_render_refuses_an_unknown_label(tmp_path):
    # "minidisc" will never be a lane; earlier drafts used "airplay" as the
    # unknown example, which P6d registered — an unknown-label fixture must
    # be a name the registry can never grow.
    path = str(tmp_path / "lanes.env")
    with pytest.raises(ValueError, match="unknown renderer lane"):
        rl.render_renderer_lanes_env(("minidisc",), path=path)
    assert not Path(path).exists(), "a refused render must write nothing"


def test_the_lane_map_is_world_readable(tmp_path):
    """Every renderer user must be able to read it; it carries no secret."""
    path = str(tmp_path / "lanes.env")
    rl.render_renderer_lanes_env(("spotify",), path=path)
    assert Path(path).stat().st_mode & 0o777 == rl.RENDERER_LANES_ENV_MODE


# --- Arming preconditions -------------------------------------------------


def test_arming_is_refused_without_the_ring_platform():
    """A renderer whose jts_ring PCM cannot resolve is a SILENT source, and
    silence is the failure mode hardest to trace back to an arm command — so
    the arm fails closed instead."""
    reason = rl.arm_refusal_reason(
        "spotify", assets_present=False, missing_assets=("ioplug .so absent",)
    )
    assert reason is not None
    assert "ioplug .so absent" in reason


def test_arming_is_allowed_with_the_ring_platform_present():
    assert rl.arm_refusal_reason("spotify", assets_present=True) is None


def test_arming_an_unknown_lane_is_refused_even_with_assets():
    reason = rl.arm_refusal_reason("nope", assets_present=True)
    assert reason is not None and "unknown lane" in reason


# --- 3. The mirrors agree -------------------------------------------------


def _rust_const(name: str) -> str:
    text = FANIN_CONFIG_RS.read_text()
    m = re.search(rf'pub const {name}: &str = "([^"]+)";', text)
    assert m, f"{name} not found in {FANIN_CONFIG_RS}"
    return m.group(1)


def test_the_ring_directory_and_prefix_match_the_rust_mirror():
    """fan-in DERIVES the ring path in Rust; this module derives it in Python;
    the conf.d spells it literally. All three must agree or fan-in reads a ring
    nothing writes — a silent source with a healthy-looking daemon."""
    assert _rust_const("RING_SHM_DIR") == rl.RING_SHM_DIR
    assert _rust_const("RENDERER_RING_PREFIX") == rl.RENDERER_RING_PREFIX


def test_every_lanes_ring_path_is_declared_verbatim_in_the_confd():
    conf = LANES_CONF.read_text()
    for lane in rl.RENDERER_LANES:
        path = rl.renderer_ring_path(lane.label)
        assert f'path "{path}"' in conf, (
            f"{lane.label}'s ring path {path} is not declared in "
            f"{LANES_CONF.name}; fan-in would read a ring nothing writes"
        )


def test_every_lanes_ring_device_is_declared_in_the_confd():
    conf = LANES_CONF.read_text()
    for lane in rl.RENDERER_LANES:
        assert re.search(rf"^pcm\.{re.escape(lane.ring_device)}\s*\{{", conf, re.M), (
            f"{lane.ring_device} (what {lane.renderer}'s --device becomes when "
            "armed) has no PCM block"
        )


def test_the_confd_ring_geometry_matches_the_shipped_fanin_geometry():
    """The conf.d numbers are the derivation rule evaluated at the shipped
    fan-in geometry. If either fan-in default moves, these must move with it or
    the ring header comparison refuses the attach."""
    fanin_unit = FANIN_UNIT.read_text()
    fanin_rs = FANIN_CONFIG_RS.read_text()

    m = re.search(r'JASPER_FANIN_INPUT_BUFFER_FRAMES=(\d+)"', fanin_unit)
    assert m, "the fan-in unit no longer declares an input buffer default"
    shipped_buffer = int(m.group(1))

    m = re.search(r'env_u32_positive\("JASPER_FANIN_PERIOD_FRAMES", (\d+)\)', fanin_rs)
    assert m, "fan-in's period default moved"
    shipped_period = int(m.group(1))

    expected_slots = shipped_buffer // shipped_period
    conf = LANES_CONF.read_text()
    for lane in rl.RENDERER_LANES:
        block = re.search(
            rf"pcm\.jts_ring_lane_{lane.label}\s*\{{(.*?)\n\}}", conf, re.S
        )
        assert block, f"no ring block for {lane.label}"
        body = block.group(1)
        assert re.search(rf"period_frames\s+{shipped_period}\b", body), (
            f"{lane.label}'s conf.d slot must be one fan-in period "
            f"({shipped_period})"
        )
        assert re.search(rf"n_slots\s+{expected_slots}\b", body), (
            f"{lane.label}'s conf.d depth must be the aloop cushion it replaces "
            f"({shipped_buffer}/{shipped_period} = {expected_slots} slots)"
        )


@pytest.mark.parametrize("label", rl.MIGRATABLE_LABELS)
def test_the_confd_ring_slave_is_plug_wrapped_at_the_lane_wire(label):
    """EVERY lane's `plug:` wrapper is pinned at the lane wire — 48 kHz, 2ch,
    converting through `defaults.pcm.rate_converter` (the knob whose fallback
    to ALSA's linear resampler cost ~12 dB of 4-8 kHz in the 2026-05 AEC
    investigation). Each renderer's native emit differs (librespot 44.1 kHz
    S24_3, shairport 44.1 kHz S32, Bluetooth codec-dependent, correction
    WAV-dependent); the wire they convert TO is one boundary and must not
    drift per lane.

    The WIDTH is declared exactly once per lane, on the ring PCM — the box's
    one resolved wire, which fan-in builds the same lane's attach geometry
    from. A `format` on the plug slave would be a second declaration that
    could shear from it, so its absence is pinned too.

    Was a librespot-only test from P6a through P6d's build — the same
    "every ring-writing renderer" generalization gap `_unit_text`'s
    docstring records for the unit pins, found here by a P6d survive-shape
    mutation (an airplay plug at `rate 44100` passed the suite; so would
    the bluealsa and correction wires have). Parametrized so the next lane
    cannot reopen it."""
    lane = rl.lane_by_label(label)
    assert lane is not None
    conf = LANES_CONF.read_text()
    block = re.search(
        rf"pcm\.{re.escape(lane.ring_device)}\s*\{{(.*?)\n\}}", conf, re.S
    )
    assert block, f"no plug block for {lane.ring_device}"
    body = block.group(1)
    assert "type plug" in body, f"{label}: wrapper must be type plug"
    assert f'pcm "{rl.ring_conf_pcm_name(label)}"' in body
    assert "rate 48000" in body, f"{label}: plug slave must pin rate 48000"
    assert "channels 2" in body, f"{label}: plug slave must pin 2 channels"
    assert "format" not in body, (
        f"{label}: the plug slave must not declare a width — the ring PCM owns it"
    )

    ring = re.search(
        rf"pcm\.{re.escape(rl.ring_conf_pcm_name(label))}\s*\{{(.*?)\n\}}",
        conf,
        re.S,
    )
    assert ring, f"no ring block for {label}"
    assert f"format {resolve_ring_wire_format(None)}" in ring.group(1), (
        f"{label}'s ring must declare the width an undeclared box resolves; "
        "fan-in attaches at that wire and refuses a sheared header"
    )


def test_the_airplay_plug_conversion_story_matches_the_template():
    """The airplay plug block's comment states what shairport EMITS —
    48 kHz S32, from the rendered conf's `output_rate` / `output_format`
    — which is a cross-file claim about the template. Pin the template's
    side so the conversion story cannot silently drift out from under the
    conf.d comment (found unpinned by a P6d survive-shape mutation:
    `output_rate = 88200` passed the whole suite).

    The rate must also equal the lane's slave rate: shairport 5.x probes
    the device with resampling disabled and exact-rate matching, so any
    other value is struck from its permissible rate set and every session
    dies choosing an output bit depth."""
    template = (REPO / "deploy" / "shairport-sync.conf.template").read_text()
    assert re.search(r"^\s*output_rate = 48000;", template, re.M), (
        "the airplay conf.d conversion story says shairport emits 48 kHz, "
        "matching the lane's 48000 slave; the template no longer sets "
        "output_rate = 48000 — update both together"
    )
    assert re.search(r'^\s*output_format = "S32";', template, re.M), (
        "the airplay conf.d conversion story says shairport emits S32; the "
        "template no longer sets output_format = \"S32\" — update both "
        "together"
    )


# --- Unit wiring ----------------------------------------------------------


def _unit_text(lane) -> str:
    """The installed unit (or drop-in) text for a lane's renderer.

    A lane's renderer is configured either by its own unit or by a drop-in over
    a packaged one; both are `deploy/systemd/...` sources, and every test below
    walks RENDERER_LANES rather than naming a single renderer — which is the
    whole point, since P6a's "every ring-writing renderer unit" test read only
    librespot's.
    """
    assert lane.unit is not None, (
        f"lane {lane.label!r} is unitless (ephemeral writers) — there is no "
        "unit text to pin; route through the per-identity branches instead "
        "(see the '_lane_*_fact' dispatchers and P6c-i's unitless-writer "
        "section below)"
    )
    direct = REPO / "deploy" / "systemd" / lane.unit
    if direct.exists():
        return direct.read_text()
    dropin_dir = REPO / "deploy" / "systemd" / f"{lane.unit}.d"
    assert dropin_dir.is_dir(), f"no unit or drop-in dir for {lane.unit}"
    # ORDERING ASYMMETRY, safe only because of the signpost below. This joins
    # drop-ins in sorted() order and the pins `re.search` the FIRST match, but
    # systemd applies the LATER-lexical drop-in — so a directive duplicated
    # across two files would be read here as the losing copy. That is exactly
    # the duplication `jts-output.conf`'s restart-cadence comment forbids (this
    # PR nearly created it before the SF-B refutation), so no machinery here:
    # keep one owner per directive and the first match is the only match.
    texts = [p.read_text() for p in sorted(dropin_dir.glob("*.conf"))]
    assert texts, f"no drop-in .conf for {lane.unit}"
    return "\n".join(texts)


# --- Per-lane writer facts, dispatched on unit-ful vs unitless -------------
#
# The `correction` lane (P6c-ii) is the first UNITLESS lane: its writers are
# EPHEMERAL aplay spawns from four process identities (jasper-web wizards,
# the root jasper-correction-web, the root streambox variant, root operator
# CLIs) — no renderer unit exists to read. Every unit-reading pin routes on
# `lane.unit is None` to a per-identity or per-mechanism alternative fact.
# The routing machinery landed one PR ahead (P6c-i) with instructive
# failures where the unitless contract was undecided; the row's arrival
# replaced each with its decision — device/map coupling became the
# call-time reader pins below, and writer liveness became the ephemeral
# enumeration in the C header. NEVER a bare skip.

WEB_UNIT = REPO / "deploy" / "jasper-web.service"
CORRECTION_WEB_UNIT = REPO / "deploy" / "jasper-correction-web.service"
STREAMBOX_WEB_UNIT = REPO / "deploy" / "jasper-web-streambox.service"
RING_SHM_HEADER = REPO / "c" / "jts-ring-ioplug" / "jts_ring_shm.h"

#: The C header's enumeration marker for the correction lane's writers —
#: the ephemeral counterpart of the `- <unit> RestartSec=N` entries. Spelled
#: once here; `_assert_unitless_liveness_facts` pins the header carries it.
EPHEMERAL_LIVENESS_MARKER = "- correction lane: EPHEMERAL aplay writers"


def _assert_unitless_liveness_facts(lane):
    """The unitless liveness alternative (decided at P6c-ii).

    A unit-ful ring writer needs `RestartSec > liveness window` and a
    generous start limit because systemd AUTO-respawns it: a SIGKILLed
    writer's frozen-fresh heartbeat refuses a successor for up to the
    window, and a fast respawn loop with a tight start limit PARKS the
    unit — the one non-self-healing shape.

    An ephemeral writer has NO respawn machinery to constrain: nothing
    auto-respawns an aplay child. The programmatic re-spawners that exist
    (sound_setup's volume-floor loop and summed-test loop) STOP on the
    first nonzero exit instead of retrying — a killed writer inside the
    window surfaces as one cleanly-failed session, not a loop — and every
    other spawn is human-paced (wizard retry, CLI re-run), slower than the
    2 s window by orders of magnitude. So the cadence facts are
    structurally n/a; what remains pinnable is the C header's writer
    enumeration ("a writer missing from that list is one nobody checked"),
    which must name the lane's ephemeral cadence class explicitly.
    """
    assert lane.unit is None
    header = RING_SHM_HEADER.read_text()
    assert EPHEMERAL_LIVENESS_MARKER in header, (
        "jts_ring_shm.h's writer-cadence enumeration must name the "
        "correction lane's ephemeral writers — every ring writer's cadence "
        "is enumerated there so the set is checkable rather than assumed"
    )


def _assert_unitless_device_facts(lane):
    """The unitless device/map coupling (decided at P6c-ii).

    A unit-ful lane couples through the rendered env file: the unit's
    ExecStart substitutes ${device_key}, defaulted to the aloop device and
    overridden by the map loaded last. An ephemeral writer reads no env —
    jasper.audio_measurement.correction_lane.correction_play_device() is
    the lane's one transport reader, resolving the SAME map through the
    SAME device_for rule at every call. Pinned here end-to-end through a
    real map file: unarmed -> aloop device; armed -> ring device; and the
    read is call-time FRESH (a flip lands on the very next call with no
    restart), which is the unitless replacement for the load-order pin —
    ordering cannot apply where nothing caches.
    """
    from jasper.audio_measurement.correction_lane import (
        correction_play_device,
    )

    assert lane.unit is None
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        map_path = str(pathlib.Path(tmp) / "renderer_lanes.env")
        original = rl.RENDERER_LANES_ENV
        rl.RENDERER_LANES_ENV = map_path
        try:
            # No map file at all — the shipped fleet state.
            assert correction_play_device() == lane.aloop_device
            # Armed: the map file is written and the SAME process resolves
            # the ring device on the next call — no import-time caching.
            pathlib.Path(map_path).write_text(
                rl.render_env_text((lane.label,))
            )
            assert correction_play_device() == lane.ring_device
            # Disarmed again: freshness in the other direction too.
            pathlib.Path(map_path).write_text(rl.render_env_text(()))
            assert correction_play_device() == lane.aloop_device
        finally:
            rl.RENDERER_LANES_ENV = original


def _assert_unitless_writer_umask_facts():
    """The unitless UMask alternative: the spawn helper owns the ring-file
    mode for every ephemeral writer identity.

    Unit-based writers need `UMask=0007` because the unit is the only thing
    wrapping the renderer process. An ephemeral writer's spawn point IS a
    process wrapper — jasper.audio_measurement.correction_lane passes
    CORRECTION_PLAY_UMASK on every spawn (pinned per-wrapper by
    tests/test_correction_lane_play.py's goldens; proven to reach the child
    by its mechanism tests). So:

    * The constant equals the unit-writers' 0007 — one mode rule, two
      delivery mechanisms.
    * jasper-web needs NO UMask= line for the lane (its spawns carry the
      umask), so none is demanded here — and none of its OTHER duties want
      one.
    * jasper-correction-web KEEPS its deliberately tight `UMask=0077` (its
      own correction profiles/bundles stay owner-only) across its drop to
      `User=jasper-web`: the per-spawn umask overrides in the CHILD only,
      which is the whole reason the umask rides the spawn rather than the
      units. Its ring-group half moved to
      `_assert_unitless_writer_group_facts` when the drop landed.
    """
    from jasper.audio_measurement.correction_lane import CORRECTION_PLAY_UMASK

    assert CORRECTION_PLAY_UMASK == 0o007
    correction_web = CORRECTION_WEB_UNIT.read_text()
    assert re.search(r"^UMask=0077$", correction_web, re.M), (
        "jasper-correction-web's tight unit UMask=0077 is part of the "
        "unitless-lane design (per-spawn umask overrides it in the child); "
        "if the unit changed, re-derive the correction-lane umask story"
    )


def _assert_unitless_writer_group_facts():
    """The unitless group alternative, per writer identity.

    Same rule as `test_non_root_ring_writers_join_the_ring_group` (root
    writes the 2775 root-owned directory regardless — the P6b root
    exemption; group membership is only required of non-root writers):

    * jasper-web and jasper-correction-web — both non-root, both spawning
      wizard aplay children, and both running as the same `jasper-web`
      account, so each unit must carry jts-ring.
    * The streambox variant — root (no `User=`), exempt.
    * Root operator CLIs (aec_commission / doctor aec_probe) —
      root by the doctor/CLI contract and unit-less by nature, exempt the
      same way.
    """
    for unit in (WEB_UNIT, CORRECTION_WEB_UNIT):
        text = unit.read_text()
        assert re.search(r"^User=jasper-web$", text, re.M), (
            f"{unit.name}'s ring-group requirement below is derived from it "
            "running non-root as jasper-web; re-derive it if the user changed"
        )
        assert re.search(
            rf"^SupplementaryGroups=.*\b{re.escape(rl.RING_GROUP)}\b", text, re.M
        ), (
            f"{unit.name} runs non-root and its wizard-spawned aplay children "
            f"must be able to create the correction lane's ring: "
            f"{rl.RING_GROUP} membership is required (U3/P6c-i)"
        )
    assert not re.search(r"^User=", STREAMBOX_WEB_UNIT.read_text(), re.M), (
        f"{STREAMBOX_WEB_UNIT.name} was root (P6b root exemption — no "
        "jts-ring needed) when the unitless-writer facts were pinned; a "
        "User= drop must add group membership and extend this pin"
    )


def _run_conf_renderer(lane, tmp: pathlib.Path, map_text: str | None) -> str:
    """Run the lane's REAL conf-renderer script against a tmp world.

    Returns the rendered conf text. The template carries only the device
    placeholder — the script substitutes what it knows and refuses to
    install a conf with any placeholder left, so a minimal template
    exercises the real substitution path. Every env-file input is pointed
    into ``tmp`` (mostly at nonexistent paths, which the script treats as
    absent — its shipped fallbacks), so the run is hermetic on any host.
    """
    script = REPO / lane.conf_renderer
    template = tmp / "template.conf"
    target = tmp / "rendered.conf"
    map_path = tmp / "renderer_lanes.env"
    template.write_text('alsa = {\n    output_device = "__RENDERER_DEVICE__";\n};\n')
    if map_text is None:
        if map_path.exists():
            map_path.unlink()
    else:
        map_path.write_text(map_text)
    env = os.environ.copy()
    env.update(
        {
            "JASPER_SHAIRPORT_TEMPLATE": str(template),
            "JASPER_SHAIRPORT_CONF": str(target),
            "JASPER_RENDERER_LANES_ENV": str(map_path),
            # Absent inputs — the script's own missing-file fallbacks.
            "JASPER_AIRPLAY_MODE_ENV": str(tmp / "no-airplay-mode.env"),
            "JASPER_ENV_FILE": str(tmp / "no-jasper.env"),
            "JASPER_SPEAKER_NAME_FILE": str(tmp / "no-speaker.env"),
            "JASPER_CAMILLA_STATEFILE": str(tmp / "no-statefile.yml"),
            "JASPER_CAMILLA_DEFAULT_CONFIG": str(tmp / "no-camilla.yml"),
            "JASPER_FANIN_ENV_FILE": str(tmp / "no-fanin.env"),
            "JASPER_OUTPUTD_ENV_FILE": str(tmp / "no-outputd.env"),
            "JASPER_GROUPING_AIRPLAY_ENV_FILE": str(tmp / "no-grouping.env"),
            "JASPER_FANIN_STATUS_SOCKET": str(tmp / "no-fanin.sock"),
            "JASPER_OUTPUTD_STATUS_SOCKET": str(tmp / "no-outputd.sock"),
        }
    )
    result = subprocess.run(
        ["bash", str(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return target.read_text()


def _assert_conf_renderer_device_facts(lane):
    """The conf-renderer device/map coupling (decided at P6d).

    shairport's device is neither an ExecStart env substitution nor a
    per-spawn Python read: /etc/shairport-sync.conf is a DERIVED artifact
    the unit's ExecStartPre re-renders from a template on every start, and
    the renderer script reads the lane map's already-rendered device line
    (never the armed set — the armed→device rule stays stated once, in
    jasper.renderer_lanes). Pinned end to end through the REAL script:
    no map → the shipped aloop device (byte-identical fleet default);
    armed → the ring device; disarmed → aloop again. The re-invocation IS
    the freshness fact: the script reads the map per run and the unit runs
    it per start, so a flip lands on the next start exactly as it does for
    the ExecStart-substitution lanes.
    """
    assert lane.conf_renderer is not None
    import tempfile

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = pathlib.Path(tmp_str)
        # No map at all — the shipped fleet state.
        rendered = _run_conf_renderer(lane, tmp, None)
        assert f'output_device = "{lane.aloop_device}"' in rendered
        # Armed: the SAME script invocation path picks up the map.
        rendered = _run_conf_renderer(
            lane, tmp, rl.render_env_text((lane.label,))
        )
        assert f'output_device = "{lane.ring_device}"' in rendered
        # Disarmed again: freshness in the rollback direction too.
        rendered = _run_conf_renderer(lane, tmp, rl.render_env_text(()))
        assert f'output_device = "{lane.aloop_device}"' in rendered


def _assert_conf_renderer_static_facts(lane):
    """The cross-language literal pins for a conf-renderer lane.

    The script is bash, the map's writer is Python, and neither imports the
    other — so the shared spellings are pinned verbatim, exactly as the
    Rust mirrors are:

    * the script names the PRODUCTION map path (its test override rides
      ``JASPER_RENDERER_LANES_ENV``, which must also exist);
    * it reads THIS lane's ``device_key``;
    * its fallback assignment is the lane's aloop device — the
      byte-identical no-map default;
    * the unit invokes the script in ``ExecStartPre`` — the coupling that
      makes "the flip lands on the unit's next start" true.

    And the option-(c) drift guard, stated as intent: the device must have
    exactly ONE statement, the rendered conf. shairport accepts backend
    args after ``--`` (``shairport-sync -- -d <device>``), and an ExecStart
    that carried one — or a unit that loaded the map as an
    ``EnvironmentFile=`` nothing consumes — would create a second statement
    with shairport's parser owning precedence, so every conf reader (the
    doctor's parse, an operator's cat) would read the LOSING copy. That is
    the drift class this campaign exists to kill; these assertions block it
    permanently.
    """
    assert lane.conf_renderer is not None
    script = (REPO / lane.conf_renderer).read_text()
    assert rl.RENDERER_LANES_ENV in script, (
        f"{lane.conf_renderer} must read the production lane map path"
    )
    assert "JASPER_RENDERER_LANES_ENV" in script, (
        f"{lane.conf_renderer} must honor the map-path override its "
        "hermetic tests ride"
    )
    assert lane.device_key in script, (
        f"{lane.conf_renderer} must read {lane.device_key} from the map"
    )
    assert f'renderer_device="{lane.aloop_device}"' in script, (
        f"{lane.conf_renderer}'s no-map fallback must be the shipped "
        f"aloop device {lane.aloop_device!r} verbatim"
    )
    unit = _unit_text(lane)
    script_name = pathlib.Path(lane.conf_renderer).name
    assert re.search(
        rf"^ExecStartPre=.*{re.escape(script_name)}", unit, re.M
    ), (
        f"{lane.unit} must run {script_name} in ExecStartPre — that is "
        "what makes a lane flip land on the unit's next start"
    )
    assert f"${{{lane.device_key}}}" not in unit, (
        f"{lane.unit} must NOT substitute {lane.device_key} itself — the "
        "conf renderer is the single delivery path (see docstring)"
    )
    assert f"EnvironmentFile=-{rl.RENDERER_LANES_ENV}" not in unit, (
        f"{lane.unit} must not load the lane map it does not consume — a "
        "false mechanism statement (see docstring)"
    )
    execstart = re.search(r"^ExecStart=(.+)$", unit, re.M)
    assert execstart, lane.unit
    assert re.fullmatch(r"\S*shairport-sync\s*", execstart.group(1)), (
        f"{lane.unit}'s ExecStart grew arguments ({execstart.group(1)!r}) — "
        "re-review against the one-statement-of-the-device guard before "
        "accepting any (see docstring: `-- -d` would silently override the "
        "rendered conf)"
    )


def _lane_device_fact(lane):
    if lane.unit is None:
        _assert_unitless_device_facts(lane)
        return
    if lane.conf_renderer is not None:
        _assert_conf_renderer_device_facts(lane)
        _assert_conf_renderer_static_facts(lane)
        return
    unit = _unit_text(lane).replace("\\\n", " ")
    # librespot spells it --device, bluealsa-aplay --pcm; what matters is
    # that the value is the indirection, not which flag carries it.
    assert f"${{{lane.device_key}}}" in unit, (
        f"{lane.renderer} must read its device from {lane.device_key}"
    )
    assert f'Environment="{lane.device_key}={lane.aloop_device}"' in unit, (
        f"{lane.renderer}'s in-unit DEFAULT must be the shipped snd-aloop "
        "device, so a box with no lane map behaves byte-identically"
    )


def _lane_map_order_fact(lane):
    if lane.unit is None:
        # Ordering cannot apply where nothing caches: the unitless
        # replacement is the call-time-freshness half of the device pin.
        _assert_unitless_device_facts(lane)
        return
    if lane.conf_renderer is not None:
        # No env chain to order: the script reads the map file per
        # invocation and the unit invokes it per start, so the freshness
        # half of the device pin is the replacement fact here too — plus
        # the static ExecStartPre/one-statement guards.
        _assert_conf_renderer_device_facts(lane)
        _assert_conf_renderer_static_facts(lane)
        return
    unit = _unit_text(lane)
    assert f"EnvironmentFile=-{rl.RENDERER_LANES_ENV}" in unit, lane.unit
    default_at = unit.index(f'Environment="{lane.device_key}=')
    envfile_at = unit.index(f"EnvironmentFile=-{rl.RENDERER_LANES_ENV}")
    assert envfile_at > default_at, (
        f"{lane.unit}: the lane map must load AFTER the Environment= "
        "default, or an arm cannot take effect"
    )


def _lane_umask_fact(lane):
    if lane.unit is None:
        _assert_unitless_writer_umask_facts()
        return
    unit = _unit_text(lane)
    assert re.search(r"^UMask=0007$", unit, re.M), (
        f"{lane.unit} writes a ring and must set UMask=0007"
    )


def _lane_group_fact(lane):
    if lane.unit is None:
        _assert_unitless_writer_group_facts()
        return
    unit = _unit_text(lane)
    user = re.search(r"^User=(\S+)", unit, re.M)
    in_group = bool(
        re.search(r"^SupplementaryGroups=.*\bjts-ring\b", unit, re.M)
    )
    if user is None or user.group(1) == "root":
        return  # root: capable without the group
    assert in_group, (
        f"{lane.unit} runs as {user.group(1)!r} (non-root) and must be in "
        f"{rl.RING_GROUP!r} to create its ring"
    )


def test_every_renderer_unit_reads_its_device_from_the_lane_map():
    for lane in rl.RENDERER_LANES:
        _lane_device_fact(lane)


def test_both_ends_load_the_same_lane_map_file_last():
    """One file, both ends — for EVERY migrated lane, not just the first.

    The ordering half is the load-bearing one and it is per-unit: systemd
    applies a later `EnvironmentFile=` over an earlier `Environment=`, so a
    renderer that loads the map BEFORE its own default would keep the default
    and the arm would silently do nothing. That is invisible except as "the
    lane never moved", which is exactly the failure this pins.
    """
    assert f"EnvironmentFile=-{rl.RENDERER_LANES_ENV}" in FANIN_UNIT.read_text()
    for lane in rl.RENDERER_LANES:
        _lane_map_order_fact(lane)


def test_every_ring_writing_renderer_unit_sets_the_umask():
    """UMask is required on EVERY ring-writing renderer, whatever its user.

    The directory's setgid bit fixes a new ring file's GROUP; only the umask
    fixes its MODE. The ioplug creates the ring `0660 & ~umask`, so under
    systemd's default 0022 it lands 0640 — group-readable, NOT group-writable.
    That bites a non-root reader even when the WRITER is root, which is why this
    is required of the root renderers too and not only the group-based ones.
    (A unitless lane satisfies the same rule through the spawn helper — see
    `_assert_unitless_writer_umask_facts`.)
    """
    for lane in rl.RENDERER_LANES:
        _lane_umask_fact(lane)


def test_non_root_ring_writers_join_the_ring_group():
    """Group membership is required only where the renderer is NOT root.

    Root writes a 2775 root-owned directory regardless, so demanding
    `SupplementaryGroups=jts-ring` of a root renderer would be cargo-culted
    ceremony. This asserts the real rule: a unit that declares a non-root
    `User=` must join the group; one that declares none (systemd's root) need
    not, and must not be failed for it. (A unitless lane applies the same
    rule per writer identity — see `_assert_unitless_writer_group_facts`.)
    """
    for lane in rl.RENDERER_LANES:
        _lane_group_fact(lane)


def test_the_ring_directory_group_matches_what_the_installer_creates():
    tmpfiles = TMPFILES.read_text()
    m = re.search(r"^d /dev/shm/jts-ring (\d+) (\S+) (\S+)", tmpfiles, re.M)
    assert m, "the tmpfiles entry no longer declares the ring directory"
    mode, owner, group = m.groups()
    # 3775 = sticky + setgid + group-write. Each bit is load-bearing:
    #   7xx group-write — both ends WRITE the ring header, so each needs write
    #        access to a file the other may have created;
    #   2xxx setgid     — a new ring inherits the directory's group, so it is
    #        group-writable to the other end whichever end created it;
    #   1xxx sticky     — a group member may only DELETE or RENAME its own ring.
    #        Without it, group-write on a shared directory lets any member unlink
    #        ANY file in it — a compromised renderer could remove Ring A or
    #        Ring B, regardless of those files' own modes.
    assert mode == "3775", (
        "the ring directory needs sticky + setgid + group-write; dropping any "
        "one of the three either breaks the shared header or widens deletion "
        "across every ring on the box"
    )
    assert owner == "root"

    users = SERVICE_USERS.read_text()
    assert f"groupadd -r {group}" in users, (
        f"the tmpfiles entry wants group {group!r} but the installer never "
        "creates it; systemd-tmpfiles would fail and the directory would keep "
        "its old ownership"
    )
    # At least one migrated renderer must actually use the group, or creating it
    # is dead ceremony. (Root renderers legitimately do not — see
    # `test_non_root_ring_writers_join_the_ring_group`. Unitless lanes have no
    # unit text to scan; their group story is per writer identity —
    # `_assert_unitless_writer_group_facts` — and cannot satisfy or violate
    # THIS directory-level sanity, so they contribute nothing here.)
    users = [
        _unit_text(lane) for lane in rl.RENDERER_LANES if lane.unit is not None
    ]
    assert any(
        re.search(rf"^SupplementaryGroups=.*\b{re.escape(group)}\b", u, re.M)
        for u in users
    ), f"no migrated renderer joins {group!r} — the group would be unused"


def test_the_installer_adds_the_renderer_user_to_the_ring_group():
    """`pi` is the distro's login account, not one this installer creates, so
    the membership needs an explicit guarded usermod — a `useradd -G` would
    never fire on an existing box."""
    users = SERVICE_USERS.read_text()
    assert "usermod -aG jts-ring pi" in users
    assert "getent passwd pi" in users, (
        "a box brought up with a custom user has no `pi`; an unguarded usermod "
        "would fail the install under set -euo pipefail"
    )


def test_the_installer_adds_jasper_web_to_the_ring_group():
    """The installer keeps jasper-web's NSS record consistent with its unit.

    The RUNTIME grant does not depend on this: systemd's
    `SupplementaryGroups=` adds the group to the spawned process directly
    (the group need only exist, not list the user in passwd/NSS). What the
    passwd record serves is the narrower, real set of NON-systemd
    consumers — `sudo -u jasper-web` probes and operator shells — plus
    service-users.sh's own stated convention that the -G lists match each
    unit's `SupplementaryGroups=`. Both delivery paths of that consistency
    are pinned: the useradd -G (fresh boxes) and — because useradd is
    skipped when the user already exists — the guarded usermod (every box
    installed before P6c-i), same shape as the `pi` membership above and
    the jasper-secrets upgrade blocks."""
    users = SERVICE_USERS.read_text()
    useradd_lines = [
        ln for ln in users.splitlines()
        if "useradd" in ln and " jasper-web" in ln
    ]
    assert useradd_lines, "service-users.sh must `useradd ... jasper-web`"
    # Same membership-pattern shape as test_systemd_hardening's
    # install-contract check: the group must appear IN the -G list, not
    # merely anywhere on the line (a comment fragment would satisfy a
    # whole-line substring).
    line = useradd_lines[0]
    assert "-G jts-ring" in line or ",jts-ring" in line, (
        "fresh installs must put jasper-web in jts-ring via its useradd -G"
    )
    assert "usermod -aG jts-ring jasper-web" in users, (
        "pre-P6c-i boxes skip the useradd; the upgrade path needs the "
        "idempotent usermod"
    )


# shairport-sync's jts-ring membership has TWO delivery paths in TWO files
# (P6d): unlike every other member, its USER is created in renderers.sh
# (the source-build section), not service-users.sh. Each path gets its OWN
# test function below — deliberately not one function with both assert
# groups, because pytest short-circuits at the first failing assert: a
# mutation of the fresh path would leave the upgrade-path asserts
# never-executed, so their green would be composition, not observation,
# and no single run could demonstrate the two guards independently
# (found by the P6d panel's correctness lens).


def test_the_installer_fresh_path_adds_shairport_sync_to_the_ring_group():
    """FRESH boxes: renderers.sh's `useradd -G` hard-lists jts-ring.

    Safe to hard-list because create_jasper_service_users (the group's
    creator) runs before install_renderers in install.sh, and an ordering
    regression fails LOUDLY (useradd exit 6 under set -euo pipefail)
    rather than silently. This test is the fresh path's sole guard; the
    upgrade path has its own test below.
    """
    renderers = RENDERERS_SH.read_text()
    useradd_lines = [
        ln for ln in renderers.splitlines()
        if "useradd" in ln and " shairport-sync" in ln
    ]
    assert useradd_lines, "renderers.sh must `useradd ... shairport-sync`"
    line = useradd_lines[0]
    assert "-G audio,jts-ring" in line or ",jts-ring" in line, (
        "fresh installs must put shairport-sync in jts-ring via its "
        "useradd -G (renderers.sh)"
    )


def test_the_installer_upgrade_path_adds_shairport_sync_to_the_ring_group():
    """UPGRADE boxes: service-users.sh carries the guarded usermod.

    The useradd in renderers.sh is skipped when the user already exists
    (every box installed before P6d), so the membership needs the
    idempotent `usermod -aG` — guarded on the account existing, because
    on a fresh box install_renderers has not run yet when
    create_jasper_service_users does (the `pi` shape). This test is the
    upgrade path's sole guard; the fresh path has its own test above.
    """
    users = SERVICE_USERS.read_text()
    assert "usermod -aG jts-ring shairport-sync" in users, (
        "pre-P6d boxes skip the useradd; the upgrade path needs the "
        "guarded idempotent usermod in service-users.sh"
    )
    assert "getent passwd shairport-sync" in users, (
        "on a FRESH box install_renderers has not run when "
        "create_jasper_service_users does, so the usermod must be guarded "
        "on the account existing — an unguarded one would fail the install"
    )


# The P6c-i synthetic-row apparatus (a SimpleNamespace stand-in driven
# through every pin plus a routing test expecting instructive
# "P6c-ii must decide" failures) RETIRED with the real `correction` row:
# every branch it rehearsed is now exercised natively by the row itself on
# every run of the pins above, and the former contract gaps hold their
# decisions (`_assert_unitless_device_facts` /
# `_assert_unitless_liveness_facts`). Graduation, not deletion of coverage.


def test_unitless_correction_writer_facts():
    """The correction lane's per-identity facts, asserted directly.

    Redundant with the routed pins above BY DESIGN (they reach the same
    assertions through the row): this direct call keeps the unitless facts
    diagnosable as their own failure — a drift names this test, not a
    generic per-lane loop."""
    _assert_unitless_writer_umask_facts()
    _assert_unitless_writer_group_facts()


def test_stray_unit_text_on_a_unitless_row_is_guided():
    """A `_unit_text` call on the unitless row must stay a guided
    AssertionError pointing at the per-identity branches, never a TypeError
    from pathlib swallowing None."""
    lane = rl.lane_by_label("correction")
    assert lane is not None and lane.unit is None
    with pytest.raises(AssertionError, match="unitless"):
        _unit_text(lane)


def test_the_installer_ships_the_renderer_lane_confd():
    platform = RING_PLATFORM.read_text()
    assert "61-jts-renderer-lanes.conf" in platform
    assert re.search(
        r'install -m 0644 "\$\{lanes_src\}" /etc/alsa/conf\.d/61-jts-renderer-lanes\.conf',
        platform,
    ), "the lane PCMs must be system-wide 0644 so non-root renderer users resolve them"


# --- fan-in's own contract ------------------------------------------------


def _audio_runtime():
    """The doctor's audio_runtime module, imported through the package.

    `jasper.cli.doctor` populates a global check registry at import and refuses
    a duplicate order, so importing a submodule directly on a fresh interpreter
    registers the same checks twice. Every other doctor test imports the
    package for exactly this reason.
    """
    from jasper.cli import doctor

    return doctor.audio_runtime


def test_the_doctor_lane_roster_follows_the_armed_set(tmp_path):
    """An armed lane reports its RING PATH as its STATUS `pcm`, so the doctor's
    roster check must follow the map or it diagnoses a correctly-armed box as
    drifted."""
    path = str(tmp_path / "lanes.env")
    rl.render_renderer_lanes_env(("spotify",), path=path)

    roster = dict(_audio_runtime()._fanin_expected_inputs(lanes_env=path))
    assert roster["spotify"] == rl.renderer_ring_path("spotify")
    assert roster["airplay"] == "hw:Loopback,1,1", "unarmed lanes are untouched"


def test_the_doctor_lane_roster_is_the_shipped_one_when_nothing_is_armed(tmp_path):
    audio_runtime = _audio_runtime()
    path = str(tmp_path / "absent.env")
    assert (
        audio_runtime._fanin_expected_inputs(lanes_env=path)
        == audio_runtime._FANIN_EXPECTED_ALOOP_INPUTS
    )


def test_the_rust_lane_selector_consults_only_the_armed_set():
    """fan-in must NOT consult the CamillaDSP coupling to decide a lane's
    transport: they are independent transports, and keying on the coupling
    would arm every ring-coupled box by deploy with no per-box source pass."""
    rs = FANIN_CONFIG_RS.read_text()
    m = re.search(
        r"pub fn lane_is_renderer_ring\(&self, label: &str\) -> bool \{(.*?)\n    \}",
        rs,
        re.S,
    )
    assert m, "lane_is_renderer_ring moved or changed shape"
    body = m.group(1)
    assert "renderer_ring_lanes" in body
    assert "coupling" not in body.lower(), (
        "the lane selector must not read the CamillaDSP coupling"
    )


def test_the_ring_writer_pid_offset_matches_the_ring_layout():
    """`ring_writer_pid` reads the header by RAW BYTE OFFSET, so it is pinned to
    the layout the ring crate owns.

    A silent drift here does not crash — it returns some OTHER header field's
    bytes as a pid. The doctor would then compare a garbage pid's cgroup, decide
    the ring is held by a stranger, and FAIL a perfectly healthy armed lane (or,
    worse, coincidentally match and accept a real stray writer). Pinning the
    offset and the width against `rust/jasper-ring/src/layout.rs` is the only
    thing standing between that and a rename.
    """
    layout = (REPO / "rust" / "jasper-ring" / "src" / "layout.rs").read_text()
    m = re.search(r"pub const OFF_WRITER_PID: usize = (\d+);", layout)
    assert m, "OFF_WRITER_PID moved or changed shape in the ring layout"
    off = int(m.group(1))

    src = (REPO / "jasper" / "renderer_lanes.py").read_text()
    fn = re.search(
        r"def ring_writer_pid\(label: str\) -> int \| None:(.*?)(?=\n\ndef |\Z)",
        src,
        re.S,
    )
    assert fn, "ring_writer_pid moved or changed shape"
    body = fn.group(1)
    assert f"header[{off}:{off + 8}]" in body, (
        f"ring_writer_pid must read the writer pid at byte offset {off}..{off + 8} "
        f"(rust/jasper-ring/src/layout.rs OFF_WRITER_PID = {off}); a drifted "
        "offset silently returns another field's bytes as a pid"
    )
    assert '"little"' in body, "the ring header is little-endian"

    hb = re.search(r"pub const HEADER_BYTES: usize = (\d+);", layout)
    assert hb, "HEADER_BYTES moved"
    assert f"fh.read({hb.group(1)})" in body, (
        "the reader must read the full header the layout declares"
    )


def test_the_doctor_reads_the_ring_source_token_from_the_status_module():
    """The doctor's armed-lane check compares STATUS `source` against a token.
    It must take that token from `jasper.fanin.status` — the module that owns
    the vocabulary the Rust serializer publishes — not spell it locally."""
    from jasper.fanin.status import FANIN_INPUT_SOURCE_RING

    assert FANIN_INPUT_SOURCE_RING == "ring"
    mixer = (REPO / "rust" / "jasper-fanin" / "src" / "mixer.rs").read_text()
    assert f'Self::Ring => "{FANIN_INPUT_SOURCE_RING}",' in mixer, (
        "the Rust LaneSource token and the Python constant have drifted; the "
        "doctor would then never recognise an armed lane as a ring lane"
    )


def test_the_rust_ring_path_derivation_matches_this_module():
    """Both sides derive `<dir>/<prefix><label>.ring` from the label alone —
    there is no per-ring env key that could disagree."""
    rs = FANIN_CONFIG_RS.read_text()
    m = re.search(
        r"pub fn renderer_ring_path\(label: &str\) -> String \{\n\s*format!\(\"([^\"]+)\"\)",
        rs,
    )
    assert m, "renderer_ring_path moved or changed shape"
    template = m.group(1)
    rendered = (
        template.replace("{RING_SHM_DIR}", rl.RING_SHM_DIR)
        .replace("{RENDERER_RING_PREFIX}", rl.RENDERER_RING_PREFIX)
        .replace("{label}", "spotify")
    )
    assert rendered == rl.renderer_ring_path("spotify")


def test_the_ring_lane_reader_never_calls_the_aloop_catchup_drain():
    """A bounded ring cannot back up past its own depth the way an aloop capture
    ring can, so `drain_input_excess` has nothing to do on this arm — and
    calling it would try to `avail_update` a PCM the lane does not have."""
    rs = RING_CAPTURE_RS.read_text()
    # A mention in prose is the explanation; a CALL would be the bug.
    assert not re.search(r"^\s*drain_input_excess\(", rs, re.M)
    # And the mixer's dispatch must not route the ring arm through it either.
    mixer = (REPO / "rust" / "jasper-fanin" / "src" / "mixer.rs").read_text()
    ring_arm = re.search(
        r"\} else if input\.ring\.is_some\(\) \{(.*?)\n            \} else if",
        mixer,
        re.S,
    )
    assert ring_arm, "the ring dispatch arm moved"
    # Strip comments before looking for the call: the arm EXPLAINS why it does
    # not drain, and the explanation must not read as the thing it forbids.
    code = "\n".join(
        line for line in ring_arm.group(1).splitlines()
        if not line.strip().startswith("//")
    )
    assert "drain_input_excess" not in code


# --- Arm preconditions: each must actually refuse ------------------------
#
# Everything here would otherwise produce SILENCE rather than an error the
# operator could connect to the arm command, which is the whole selection rule
# for what belongs in the preflight.


def test_arming_is_refused_without_the_lane_confd():
    """The lane conf.d declares the ring PCM and its plug: wrapper. Without it
    the renderer's device resolves to nothing — a silent source."""
    reason = rl.arm_refusal_reason(
        "spotify", assets_present=True, lane_conf_present=False
    )
    assert reason is not None
    assert rl.RENDERER_LANES_CONF_D in reason


def test_arming_is_refused_when_the_renderer_user_is_not_in_the_ring_group():
    """Without group membership the renderer's ioplug cannot create its ring
    under a 2775/3775 directory, so the lane never receives a frame."""
    reason = rl.arm_refusal_reason(
        "spotify", assets_present=True, lane_conf_present=True,
        user_in_ring_group=False,
    )
    assert reason is not None
    assert rl.RING_GROUP in reason


def test_arming_is_refused_on_an_inexpressible_geometry():
    """fan-in REFUSES this at config with a park (exit 78). Surfacing it here
    puts it where the operator is watching instead of in the journal after the
    fact. period 128 with the shipped 4096 buffer derives 32 slots."""
    reason = rl.arm_refusal_reason(
        "spotify", assets_present=True, lane_conf_present=True,
        user_in_ring_group=True, input_buffer_frames=4096, period_frames=128,
    )
    assert reason is not None
    assert "whole-slot" in reason


def test_an_unanswerable_precondition_does_not_refuse():
    """`None` means "could not determine on this host" — a dev laptop has no
    jts-ring group and no installed conf.d. Refusing on an unanswerable
    question would make the arm impossible to run anywhere but a live Pi, which
    is a worse failure than a preflight that occasionally cannot check."""
    assert rl.arm_refusal_reason(
        "spotify", assets_present=True, lane_conf_present=None,
        user_in_ring_group=None, input_buffer_frames=None, period_frames=None,
    ) is None


def test_the_shipped_geometry_is_expressible():
    """Sanity floor for the check above: the fleet's own numbers must pass, or
    the preflight would refuse every real box."""
    assert rl.arm_refusal_reason(
        "spotify", assets_present=True, lane_conf_present=True,
        user_in_ring_group=True, input_buffer_frames=4096, period_frames=256,
    ) is None
    assert rl.renderer_ring_slots(4096, 256) == 16


@pytest.mark.parametrize(
    "buffer_frames,period,expected",
    [
        (4096, 256, 16),   # the shipped geometry, exactly at RING_SLOTS_MAX
        (2048, 256, 8),
        (512, 256, 2),     # the floor
        (4096, 128, None), # 32 slots — above the max
        (256, 256, None),  # 1 slot — below the min
        (4000, 256, None), # not a whole number of slots
        (4096, 0, None),   # degenerate
    ],
)
def test_the_python_slot_derivation_matches_the_rust_rule(
    buffer_frames, period, expected
):
    assert rl.renderer_ring_slots(buffer_frames, period) == expected


def test_the_python_slot_derivation_mirrors_the_rust_bounds():
    """Both sides must reject the same range, or the arm would accept a
    geometry fan-in then parks on."""
    rs = FANIN_CONFIG_RS.read_text()
    lo = re.search(r"pub const RING_SLOTS_MIN: u32 = (\d+);", rs)
    hi = re.search(r"pub const RING_SLOTS_MAX: u32 = (\d+);", rs)
    assert lo and hi
    assert int(lo.group(1)) == rl.RING_SLOTS_MIN
    assert int(hi.group(1)) == rl.RING_SLOTS_MAX


# --- Stale-ring self-heal -------------------------------------------------


def _shipped_ring_format_id() -> int:
    """The header ``sample_format`` id an UNDECLARED box's lanes carry — the
    same wire the conf.d declares and fan-in attaches at."""
    token = resolve_ring_wire_format(None)
    return next(
        i for i, name in ring_assets.RING_SAMPLE_FORMAT_NAMES.items() if name == token
    )


def _write_ring_header(path, *, n_slots, period_frames, channels=2, fmt=None):
    """A minimal valid jts_ring header, so the coherence comparator has
    something real to judge. Field offsets come from
    rust/jasper-ring/src/layout.rs."""
    import struct

    if fmt is None:
        fmt = _shipped_ring_format_id()
    sample_bytes = 2 if fmt == ring_assets.RING_SAMPLE_FORMAT_S16LE else 4
    header = bytearray(128)
    header[0:4] = struct.pack("<I", 0x4A52_494E)   # MAGIC 'JRIN'
    header[4:8] = struct.pack("<I", 1)             # VERSION
    header[8:12] = struct.pack("<I", 48000)        # rate
    header[12:16] = struct.pack("<I", channels)
    header[16:20] = struct.pack("<I", fmt)
    header[20:24] = struct.pack("<I", period_frames)
    header[24:28] = struct.pack("<I", n_slots)
    payload = n_slots * period_frames * channels * sample_bytes
    pathlib.Path(path).write_bytes(bytes(header) + b"\0" * payload)


def test_a_geometry_mismatched_ring_is_deleted(tmp_path, monkeypatch):
    """A ring left from a PRIOR geometry is a create-or-ATTACH error, not
    something either end recovers from: the renderer would fail its open and the
    lane would stay silent until someone ran `rm` by hand. Clearing it is what
    makes the arm lever re-pullable — the exact trap the format axis sprang on
    Ring A's rollback.

    **And it clears the ring FILE ONLY.** The two adjacent lock files are the
    cross-language mutexes that make a concurrent create-or-attach safe;
    deleting one opens an inode-tear window — a holder keeps its flock on the
    now-unlinked inode while the next opener creates a fresh file and locks
    THAT, so two processes hold "exclusive" locks on different inodes and
    neither excludes the other. It buys nothing either, since a lock file
    carries no geometry and so is never the thing that is stale. Asserted here
    rather than left to the comment, because a comment does not fail a build.
    """
    monkeypatch.setattr(rl, "RING_SHM_DIR", str(tmp_path))
    path = rl.renderer_ring_path("spotify")
    _write_ring_header(path, n_slots=8, period_frames=256)   # conf.d says 16
    assert pathlib.Path(path).exists()

    # Stage BOTH sidecars, exactly as a live box has them.
    open_lock = pathlib.Path(path + ".open.lock")
    writer_lock = pathlib.Path(path + ".writer.lock")
    open_lock.write_bytes(b"")
    writer_lock.write_bytes(b"")

    reason = rl.delete_stale_ring("spotify", conf_d=str(LANES_CONF))
    assert reason is not None, "a sheared ring must be cleared"
    assert "n_slots" in reason
    assert not pathlib.Path(path).exists()

    assert open_lock.exists(), (
        "delete_stale_ring must NOT unlink <ring>.open.lock — it is the "
        "cross-language open-transaction mutex, and removing it lets two "
        "openers lock different inodes and stop excluding each other"
    )
    assert writer_lock.exists(), (
        "delete_stale_ring must NOT unlink <ring>.writer.lock — it is what "
        "makes writer exclusivity fd-scoped; removing it would let two writers "
        "each hold an 'exclusive' lock on a different inode"
    )


def test_a_coherent_ring_is_left_alone(tmp_path, monkeypatch):
    """Deleting a HEALTHY ring would tear down a live lane on every arm/disarm
    of some OTHER lane — the self-heal must be surgical."""
    monkeypatch.setattr(rl, "RING_SHM_DIR", str(tmp_path))
    path = rl.renderer_ring_path("spotify")
    _write_ring_header(path, n_slots=16, period_frames=256)  # matches conf.d

    assert rl.delete_stale_ring("spotify", conf_d=str(LANES_CONF)) is None
    assert pathlib.Path(path).exists(), "a coherent ring must survive"


def test_an_absent_ring_is_not_an_error(tmp_path, monkeypatch):
    """The overwhelmingly common case: nothing to clear."""
    monkeypatch.setattr(rl, "RING_SHM_DIR", str(tmp_path))
    assert rl.delete_stale_ring("spotify", conf_d=str(LANES_CONF)) is None


def test_a_magicless_ring_is_left_for_the_writer_to_reclaim(tmp_path, monkeypatch):
    """A file with no JRIN magic is one the writer reclaims itself. Deleting it
    here would race that reclaim for no benefit."""
    monkeypatch.setattr(rl, "RING_SHM_DIR", str(tmp_path))
    path = rl.renderer_ring_path("spotify")
    pathlib.Path(path).write_bytes(b"\0" * 4096)

    assert rl.delete_stale_ring("spotify", conf_d=str(LANES_CONF)) is None
    assert pathlib.Path(path).exists()


def test_every_detach_reason_has_its_own_remedy():
    """A remedy-per-token guard, so a P6b-d token cannot ship remedy-less.

    The remedy function used to fall through to the "ring does not exist yet"
    text for anything it did not name, which sent an operator looking for a
    missing file when the real cause was an orphaned mapping. A new token added
    in Rust without a remedy here would silently inherit the same wrong advice.
    """
    from jasper.cli import doctor

    rs = (
        REPO / "rust" / "jasper-fanin" / "src" / "mixer" / "ring_capture.rs"
    ).read_text()
    block = re.search(
        r"pub\(super\) const fn as_str\(self\) -> &'static str \{(.*?)\n    \}",
        rs,
        re.S,
    )
    assert block, "the detach-reason token table moved"
    tokens = re.findall(r'=> "([a-z_]+)"', block.group(1))
    assert len(tokens) >= 4, tokens

    remedies = {t: doctor.audio_runtime._ring_detach_remedy(t) for t in tokens}
    # The fallback text, identified by what only it says.
    fallback = doctor.audio_runtime._ring_detach_remedy("__no_such_token__")
    for token, text in remedies.items():
        assert text, token
        if token == "unavailable":
            continue  # unavailable IS the fallback's subject
        assert text != fallback, (
            f"detach reason {token!r} has no remedy of its own and falls through "
            f"to the 'ring does not exist yet' text, which is the wrong advice "
            f"for it"
        )


@pytest.mark.parametrize(
    "raw",
    ["spotify\x1c", "\x1cspotify", "spotify\x1d", "spotify\x1e", "spotify\x1f"],
)
def test_the_label_trim_matches_rusts_not_pythons(raw):
    """`str.strip()` removes the C0 information separators; `str::trim()` does
    not, because they are not Unicode White_Space. Measured, that covers 5 of 11
    separator cases — enough for this parser to call a lane armed that fan-in
    calls un-armed, off identical bytes. The Python side must keep them."""
    parsed = rl.parse_armed_labels(raw)
    assert parsed == (raw,), (
        f"{raw!r} must survive the trim intact, exactly as Rust's trim() leaves "
        f"it; got {parsed!r}"
    )


def test_the_label_trim_still_strips_real_whitespace():
    """The floor for the test above: ordinary whitespace must still go, or the
    fail-safe empty-value handling breaks.

    ``\xa0`` (NBSP) is here deliberately: it IS Unicode ``White_Space``, so both
    languages strip it. A reimplementation as ``strip(" \\t\\n\\r\\v\\f")``
    would keep every other pin in this file green while silently breaking NBSP,
    which is exactly the shape of a plausible "simplification".
    """
    assert rl.parse_armed_labels("  spotify  ") == ("spotify",)
    assert rl.parse_armed_labels("\t spotify \n") == ("spotify",)
    assert rl.parse_armed_labels("\xa0spotify\xa0") == ("spotify",)
    assert rl.parse_armed_labels("\x0bspotify\x0c") == ("spotify",)
    assert rl.parse_armed_labels(" , \t , ") == ()
    assert rl.parse_armed_labels("\xa0,\xa0") == ()


def test_the_rust_parser_uses_trim_not_a_wider_strip():
    """Pin the Rust side of the same contract."""
    rs = FANIN_CONFIG_RS.read_text()
    fn = re.search(r"fn env_csv_labels\(name: &str\) -> Vec<String> \{(.*?)\n\}", rs, re.S)
    assert fn, "env_csv_labels moved"
    assert ".map(str::trim)" in fn.group(1)


def test_the_effective_geometry_chain_matches_the_fanin_unit():
    """The preflight models fan-in's env chain; if the unit gains or reorders an
    EnvironmentFile the model must follow, or the arm approves a geometry the
    next daemon start will not use."""
    unit = FANIN_UNIT.read_text()
    files = re.findall(r"^EnvironmentFile=-?(\S+)", unit, re.M)
    assert files, "the fan-in unit declares no EnvironmentFile"
    assert list(rl.FANIN_ENV_CHAIN) == files, (
        f"renderer_lanes.FANIN_ENV_CHAIN {list(rl.FANIN_ENV_CHAIN)} has drifted "
        f"from the unit's own order {files}"
    )
    for key, default in rl.FANIN_UNIT_DEFAULTS.items():
        assert f'Environment="{key}={default}"' in unit, (
            f"{key}'s modelled unit default {default} is not what the unit sets"
        )
    # The rest fall through to fan-in's OWN defaults, which live in Rust — a
    # different source, and one this model must not claim the unit provides.
    rs = FANIN_CONFIG_RS.read_text()
    for key, default in rl.FANIN_RUST_DEFAULTS.items():
        assert re.search(
            rf'env_u32(?:_positive)?\("{re.escape(key)}", {default}\)', rs
        ), f"{key}'s modelled Rust default {default} is not what Config uses"
        assert f'Environment="{key}=' not in unit, (
            f"{key} is modelled as a Rust default but the unit now sets it too"
        )


def test_the_effective_geometry_prefers_the_later_file(tmp_path):
    """Later EnvironmentFile beats earlier, and every file beats the in-unit
    default — the precedence a single-file read gets wrong."""
    early = tmp_path / "jasper.env"
    late = tmp_path / "fanin.env"
    early.write_text("JASPER_FANIN_PERIOD_FRAMES=128\n")
    late.write_text("JASPER_FANIN_PERIOD_FRAMES=512\n")
    chain = (str(early), str(late))

    value, source = rl.resolve_effective_fanin_value(
        "JASPER_FANIN_PERIOD_FRAMES", chain=chain
    )
    assert (value, source) == (512, str(late))

    # Absent everywhere -> the unit default, named as such.
    value, source = rl.resolve_effective_fanin_value(
        "JASPER_FANIN_INPUT_BUFFER_FRAMES", chain=chain
    )
    assert (value, source) == (4096, "unit-default")
    # A key the unit does NOT set falls through to fan-in's own default, named
    # as such rather than mislabelled as the unit's.
    value, source = rl.resolve_effective_fanin_value(
        "JASPER_FANIN_PERIOD_FRAMES", chain=(str(tmp_path / "absent.env"),)
    )
    assert (value, source) == (256, "rust-default")


def test_the_arm_refuses_a_period_128_box_by_default(tmp_path):
    """The B2 box shape, driven through the PREFLIGHT rather than a flag: a box
    whose env chain says period=128 derives 32 slots and must be refused
    WITHOUT the operator passing anything."""
    fanin = tmp_path / "fanin.env"
    fanin.write_text("JASPER_FANIN_PERIOD_FRAMES=128\n")
    chain = (str(tmp_path / "jasper.env"), str(fanin))

    buf, per, provenance = rl.effective_lane_geometry(chain=chain)
    assert (buf, per) == (4096, 128)
    assert str(fanin) in provenance

    reason = rl.arm_refusal_reason(
        "spotify", assets_present=True, lane_conf_present=True,
        user_in_ring_group=True, input_buffer_frames=buf, period_frames=per,
    )
    assert reason is not None and "whole-slot" in reason


def test_the_doctor_probe_outlasts_the_ring_writer_lock_wait():
    """The probe's kill guard MUST exceed BOTH of the ring's lock waits.

    A contended open spends both waits inside `snd_pcm_prepare` before it
    either wins the lock or is refused. Killing it first exits 124, which
    `_classify_probe` reads as FAILED — correctly, since nothing finished, but
    the box is healthy: an open that wins the race at the end of those waits
    would be reported as a broken device, and one that is refused would never
    reach `_ring_lane_busy_owner_matches` (the pid→cgroup ownership proof) in
    exactly the contended case that proof exists for.

    Pinned cross-language for the same reason `OFF_WRITER_PID` is: the two
    values live in different languages and either could be changed alone.
    """
    header = (REPO / "c" / "jts-ring-ioplug" / "jts_ring_shm.h").read_text()
    m = re.search(
        r"#define JTS_RING_OPEN_LOCK_WAIT_TIMEOUT_MS (\d+)ull", header
    )
    assert m, "the ring's lock-wait constant moved or changed shape"
    lock_wait_sec = int(m.group(1)) / 1000.0

    src = (REPO / "jasper" / "cli" / "doctor" / "renderers.py").read_text()
    m = re.search(r'_PROBE_TIMEOUT_SEC = "([0-9.]+)"', src)
    assert m, "_PROBE_TIMEOUT_SEC moved or changed shape"
    probe_sec = float(m.group(1))

    assert probe_sec > 2 * lock_wait_sec, (
        f"the doctor probe runs for {probe_sec}s but a fully contended open "
        f"spends BOTH lock waits ({lock_wait_sec}s each; they add rather than "
        f"overlap) inside snd_pcm_prepare before it wins the lock or is "
        f"refused. A probe killed first exits 124 = FAILED, so a healthy box "
        f"reports a broken device. Raise the probe timeout, or lower the lock "
        f"wait; do not leave them crossed"
    )
    # And the dependency is named at BOTH ends, so neither reads as arbitrary.
    assert "JTS_RING_OPEN_LOCK_WAIT_TIMEOUT_MS" in src, (
        "the Python side must name the C constant it depends on"
    )
    assert "_PROBE_TIMEOUT_SEC" in header, (
        "the C constant must name the doctor probe as a dependent"
    )


def test_the_c0_normalization_survives_the_FILE_read_end_to_end(tmp_path):
    """The parser-level trim was not enough on its own — pin the whole path.

    ``_env_file_value`` runs BEFORE ``parse_armed_labels``, and it used to trim
    with Python's ``str.strip()``. So a value of ``spotify\x1c`` in a real file
    had its separator eaten before the matched parser saw it: this side reported
    the lane ARMED while ``jasper-fanin``'s ``env_csv_labels`` saw an unknown
    label and refused with a config-class park. Two languages, one file, opposite
    answers — precisely what the matched trim exists to prevent.

    The CLI direction was always safe (raw arguments reach the parser directly
    and are refused loudly); it was the FILE direction that lied. This drives a
    real file on disk, because that is the only way to exercise the reader.
    """
    lanes = tmp_path / "renderer_lanes.env"
    lanes.write_text(
        "JASPER_FANIN_RENDERER_RING_LANES=spotify\x1c\n"
        "JASPER_LIBRESPOT_DEVICE=librespot_substream\n"
    )

    armed = rl.read_armed_labels(str(lanes))
    assert armed == ("spotify\x1c",), (
        "the separator must SURVIVE the file read, so this side sees the same "
        f"label fan-in's env_csv_labels sees; got {armed!r}"
    )
    assert "spotify" not in armed, (
        "reporting the clean label would mean this side calls the lane armed "
        "while fan-in parks on an unknown label, off identical bytes"
    )
    # And the drift surface inherits the honest answer rather than an
    # armed-and-consistent one the daemon would refuse.
    assert rl.fanin_env_expectations(str(lanes))[rl.FANIN_RING_LANES_KEY] == (
        "spotify\x1c"
    )
    # The label is not one fan-in knows, so nothing is armed in the sense that
    # matters: it would refuse at config rather than ingress a ring.
    assert rl.lane_by_label(armed[0]) is None


def test_a_trailing_separator_on_the_LINE_is_not_eaten_either(tmp_path):
    """The line-level strip ran before value extraction, so it could eat a
    separator that belonged to the value. Same bug, one layer earlier."""
    lanes = tmp_path / "renderer_lanes.env"
    lanes.write_bytes(
        b"JASPER_FANIN_RENDERER_RING_LANES=spotify\x1c\n"
        b"JASPER_LIBRESPOT_DEVICE=librespot_substream\n"
    )
    assert rl.read_armed_labels(str(lanes)) == ("spotify\x1c",)


def test_the_file_reader_still_strips_ordinary_whitespace(tmp_path):
    """The floor: the reader must still handle a normal file, including the
    line terminator it has always removed."""
    lanes = tmp_path / "renderer_lanes.env"
    lanes.write_text(
        "# a comment\n"
        "  JASPER_FANIN_RENDERER_RING_LANES = spotify \n"
        "JASPER_LIBRESPOT_DEVICE=librespot_ring_lane\r\n"
    )
    assert rl.read_armed_labels(str(lanes)) == ("spotify",)
    assert rl.fanin_env_expectations(str(lanes))["JASPER_LIBRESPOT_DEVICE"] == (
        "librespot_ring_lane"
    )


def test_the_written_map_round_trips_a_clean_label(tmp_path):
    """The writer's own output must read back identically — the normalization
    must not have made the ordinary path lossy."""
    path = str(tmp_path / "lanes.env")
    rl.render_renderer_lanes_env(("spotify",), path=path)
    assert rl.read_armed_labels(path) == ("spotify",)


# --- Label-agnostic arming (U3 / P6b) -------------------------------------
#
# The registry is supposed to make a new lane one tuple entry. These prove that
# claim mechanically for EVERY registered lane, rather than trusting that the
# next one behaves like the first.


@pytest.mark.parametrize("label", rl.MIGRATABLE_LABELS)
def test_any_registered_lane_arms_and_disarms_through_the_same_path(label, tmp_path):
    """`--arm <label>` works for every registered lane with no per-lane code.

    P6a's arming tests all named "spotify", so nothing proved the mechanism was
    label-agnostic — a second lane could have needed a special case and no test
    would have said so.
    """
    lane = rl.lane_by_label(label)
    assert lane is not None
    path = str(tmp_path / "lanes.env")

    outcome = rl.render_renderer_lanes_env((label,), path=path)
    assert outcome.changed and outcome.armed == (label,)
    assert rl.read_armed_labels(path) == (label,)

    env = rl.fanin_env_expectations(path)
    assert env[rl.FANIN_RING_LANES_KEY] == label
    assert env[lane.device_key] == lane.ring_device, (
        f"arming {label!r} must point {lane.renderer} at its ring device"
    )
    # Every OTHER lane stays on its aloop device — arming one lane must not
    # move another.
    for other in rl.RENDERER_LANES:
        if other.label != label:
            assert env[other.device_key] == other.aloop_device, (
                f"arming {label!r} moved {other.label!r} too"
            )

    rl.render_renderer_lanes_env((), path=path)
    env = rl.fanin_env_expectations(path)
    assert rl.read_armed_labels(path) == ()
    assert env[lane.device_key] == lane.aloop_device, (
        f"disarming {label!r} must return {lane.renderer} to its aloop device"
    )


def test_arming_every_lane_at_once_is_coherent(tmp_path):
    """The armed set is a SET, not a single lane. All-armed must name every ring
    device and drop none — the shape a fully-migrated box will run at P6d."""
    path = str(tmp_path / "lanes.env")
    rl.render_renderer_lanes_env(rl.MIGRATABLE_LABELS, path=path)
    assert rl.read_armed_labels(path) == rl.MIGRATABLE_LABELS
    env = rl.fanin_env_expectations(path)
    assert env[rl.FANIN_RING_LANES_KEY] == ",".join(rl.MIGRATABLE_LABELS)
    for lane in rl.RENDERER_LANES:
        assert env[lane.device_key] == lane.ring_device


@pytest.mark.parametrize("label", rl.MIGRATABLE_LABELS)
def test_every_lane_has_a_distinct_ring_path_device_and_key(label):
    """No two lanes may share a ring file, a PCM name, or an env key.

    A collision would be silent and catastrophic: two renderers writing one ring
    is two writers on an SPSC transport, and two lanes sharing an env key means
    arming one arms both.
    """
    lane = rl.lane_by_label(label)
    assert lane is not None
    others = [x for x in rl.RENDERER_LANES if x.label != label]
    assert rl.renderer_ring_path(label) not in {
        rl.renderer_ring_path(o.label) for o in others
    }
    assert lane.ring_device not in {o.ring_device for o in others}
    assert lane.aloop_device not in {o.aloop_device for o in others}
    assert lane.device_key not in {o.device_key for o in others}
    assert lane.unit not in {o.unit for o in others}


@pytest.mark.parametrize("label", rl.MIGRATABLE_LABELS)
def test_every_registered_label_is_a_real_fanin_lane(label):
    """A label absent from fan-in's compiled-in default input_renderers is
    refused at config (ConfigClassError -> park) UNLESS
    JASPER_FANIN_INPUT_RENDERERS overrides it in the deployed env — this pins
    the compiled-in default, not the guaranteed runtime outcome."""
    rs = FANIN_CONFIG_RS.read_text()
    m = re.search(
        r'"JASPER_FANIN_INPUT_RENDERERS",\s*&\[(.*?)\]', rs, re.S
    )
    assert m, f"input_renderers default array not found in {FANIN_CONFIG_RS}"
    # Resolve bare identifiers (named constants) as well as quoted literals —
    # a scrape that only matched `"..."` would silently miss a label spelled
    # via a `pub const NAME: &str = "...";` reference (issue #3461).
    consts = dict(re.findall(r'pub const (\w+): &str = "([^"]+)";', rs))
    raw_items = [x.strip().strip('"') for x in m.group(1).split(",") if x.strip()]
    fanin_labels = [consts.get(item, item) for item in raw_items]
    assert label in fanin_labels, (
        f"lane label {label!r} is missing from fan-in's compiled-in default "
        f"input_renderers {fanin_labels} (scraped from {FANIN_CONFIG_RS.name}); "
        "unless JASPER_FANIN_INPUT_RENDERERS overrides it in the deployed "
        "env, fan-in will not recognize this lane"
    )


@pytest.mark.parametrize("label", rl.MIGRATABLE_LABELS)
def test_every_lane_declares_its_ring_and_plug_blocks_in_the_confd(label):
    """Both conf.d blocks, for every lane — the ring PCM and the plug wrapper
    the renderer's device actually names."""
    lane = rl.lane_by_label(label)
    assert lane is not None
    conf = LANES_CONF.read_text()
    assert re.search(
        rf"^pcm\.{re.escape(rl.ring_conf_pcm_name(label))}\s*\{{", conf, re.M
    ), f"{label}: no jts_ring block"
    assert re.search(rf"^pcm\.{re.escape(lane.ring_device)}\s*\{{", conf, re.M), (
        f"{label}: no plug block for {lane.ring_device}"
    )
    # The plug's slave must be THIS lane's ring, not another's.
    block = re.search(
        rf"pcm\.{re.escape(lane.ring_device)}\s*\{{(.*?)\n\}}", conf, re.S
    )
    assert block
    assert f'pcm "{rl.ring_conf_pcm_name(label)}"' in block.group(1), (
        f"{label}'s plug wrapper does not point at its own ring"
    )


# --- Root renderers, and the derived device map (U3 / P6b) -----------------
#
# Both of these were found GREEN by the P6b mutation harness — the fix and the
# generalization each shipped without a guard, which is exactly the shape a
# mutation pass exists to catch.


@pytest.mark.parametrize("root_spelling", ["root", "0", None])
def test_a_root_renderer_is_capable_without_group_membership(root_spelling):
    """Root writes the 2775 ring directory regardless of `jts-ring`.

    This is not a nicety. `bluealsa-aplay` runs as root (its packaged unit sets
    no `User=`, nor does the JTS drop-in), and before P6b this function answered
    **False** for it — which would have REFUSED every arm of that lane on every
    box, for a permission it already had. `None` is included because that is
    what systemd reports for a unit with no `User=`, and it means root.
    """
    assert rl.renderer_user_in_ring_group(root_spelling) is True


def test_a_root_renderer_is_not_refused_by_the_arm_preflight():
    """The end-to-end consequence of the above: the preflight must pass."""
    assert (
        rl.arm_refusal_reason(
            "bluealsa",
            assets_present=True,
            lane_conf_present=True,
            user_in_ring_group=rl.renderer_user_in_ring_group(None),
            input_buffer_frames=4096,
            period_frames=256,
        )
        is None
    )


def test_a_nonroot_user_outside_the_group_is_still_refused():
    """The floor for the test above — the group check must not become a
    rubber stamp that returns True for everyone."""
    assert rl.renderer_user_in_ring_group("nobody-not-a-real-user-xyz") in (
        False,
        None,
    ), "a non-root user must not be reported capable merely because root is"


def test_the_doctor_ring_device_map_covers_every_registered_lane():
    """The EBUSY-owner map must be DERIVED, not hand-listed.

    P6a hand-listed `{"librespot_ring_lane": "spotify"}`. A lane added to the
    registry but forgotten there still probes, still hits EBUSY when its
    renderer is playing, and then fails `check_renderer_device_resolvable` with
    "not a known fan-in ring lane" — a red doctor on a healthy box, from a
    missing dict entry. This fails if the map ever regresses to a literal.
    """
    from jasper.cli.doctor import renderers as rdoc

    mapping = rdoc._ring_renderer_devices()
    assert mapping == {
        lane.ring_device: lane.label for lane in rl.RENDERER_LANES
    }
    for lane in rl.RENDERER_LANES:
        assert mapping.get(lane.ring_device) == lane.label, (
            f"{lane.label}'s ring device is missing from the doctor's map; its "
            "EBUSY-owner path would report 'not a known fan-in ring lane'"
        )
    assert len(mapping) == len(rl.RENDERER_LANES)


# --- Restart cadence vs the ring's liveness window (U3 / P6b, SF-B) --------


def _ring_liveness_window_sec() -> float:
    """The secondary guard's window, read from the C constant that owns it."""
    header = (REPO / "c" / "jts-ring-ioplug" / "jts_ring_shm.h").read_text()
    m = re.search(r"#define JTS_RING_WRITER_LIVENESS_TIMEOUT_NS (\d+)ull", header)
    assert m, "the ring's writer-liveness constant moved or changed shape"
    return int(m.group(1)) / 1_000_000_000.0


@pytest.mark.parametrize("label", rl.MIGRATABLE_LABELS)
def test_every_ring_writing_renderer_restarts_slower_than_the_liveness_window(label):
    """A ring writer's RestartSec MUST exceed the writer-liveness window.

    A SIGKILLed writer's flock drops with its fd, but it leaves `writer_pid`
    stamped and its heartbeat frozen-fresh, so the secondary guard refuses a new
    writer for up to that window. A renderer that respawns faster races its own
    predecessor into an avoidable -EBUSY, fails, and respawns again — and with a
    tight start limit that loop PARKS the unit, which is the source dead until
    an operator intervenes. It is the one non-self-healing shape in this design.

    Pinned cross-file, same shape as the doctor-probe pin: the value lives in a
    systemd unit and the bound lives in a C header, and neither may move alone.
    """
    lane = rl.lane_by_label(label)
    assert lane is not None
    if lane.unit is None:
        _assert_unitless_liveness_facts(lane)
        return
    unit = _unit_text(lane)
    m = re.search(r"^RestartSec=(\d+(?:\.\d+)?)", unit, re.M)
    assert m, (
        f"{lane.unit} writes a ring but declares no RestartSec. The PACKAGED "
        "unit's cadence is not visible to this repo, so it must be overridden "
        "here rather than assumed"
    )
    restart_sec = float(m.group(1))
    window = _ring_liveness_window_sec()
    assert restart_sec > window, (
        f"{lane.unit} restarts in {restart_sec}s but a killed ring writer stays "
        f"refused for up to {window}s — a faster respawn races its own "
        "predecessor into EBUSY and can trip the start limit"
    )


@pytest.mark.parametrize("label", rl.MIGRATABLE_LABELS)
def test_every_ring_writing_renderer_tolerates_a_burst_of_refusals(label):
    """The start limit must not park the unit over a run of legitimate EBUSYs.

    RestartSec alone is not enough: systemd's default limit (5 starts in 10 s)
    is what turns a transient refusal loop into a parked unit. Each ring writer
    declares a window generous enough that a burst cannot reach it.
    """
    lane = rl.lane_by_label(label)
    assert lane is not None
    if lane.unit is None:
        _assert_unitless_liveness_facts(lane)
        return
    unit = _unit_text(lane)
    burst = re.search(r"^StartLimitBurst=(\d+)", unit, re.M)
    interval = re.search(r"^StartLimitIntervalSec=(\d+)", unit, re.M)
    assert burst and interval, (
        f"{lane.unit} writes a ring and must declare its own start limit; "
        "systemd's default 5-in-10s parks the unit on an EBUSY burst"
    )
    assert int(burst.group(1)) > 5, (
        f"{lane.unit}'s StartLimitBurst={burst.group(1)} is no better than "
        "systemd's default"
    )


def _enumerated_writer_cadences() -> dict[str, str]:
    """``{unit: RestartSec}`` as the C header's writer enumeration declares it.

    Matches the ENUMERATION ENTRY's shape — `//   - <unit>  RestartSec=<n>` —
    not a bare substring. A substring test passes on any incidental mention
    (the drop-in's own PATH contains the unit name), which made the first
    version of this guard survive deleting the entry it exists to require.
    """
    header = RING_SHM_HEADER.read_text()
    entries = dict(re.findall(r"^//\s+-\s+(\S+)\s+RestartSec=(\d+)", header, re.M))
    assert entries, "the header's writer-cadence enumeration parsed empty"
    return entries


#: Enumerated ring writers that do NOT clear the platform's `StartLimitBurst
#: > 5` bar, mapped to the value each declares today. One entry:
#: `jasper-camilla` at 5 — systemd's own default, on the same AXIS-1 unit the
#: header already records as sitting ON the liveness window and EG-7 records as
#: the one ring writer carrying no `UMask`. Phase 1 fixes the unit it touches
#: (jasper-snapclient) and does not claim the invariant holds fleet-wide.
#: Written down rather than folded into a weaker bar, so the next writer added
#: below the bar cannot land silently; pinned to the exact value, so the
#: exemption fails the moment the gap is closed and can be deleted.
_BURST_BAR_EXEMPTIONS = {"jasper-camilla": 5}


def _unit_text_by_name(name: str) -> str:
    """The shipped unit (or drop-in) text for an ENUMERATED writer's name.

    The lane-keyed `_unit_text` cannot serve here: this guard walks the header,
    which enumerates writers that are not renderer lanes at all. Accepts the
    header's own spelling, with or without the `.service` suffix.
    """
    candidates = [name] if name.endswith(".service") else [name, f"{name}.service"]
    for candidate in candidates:
        direct = REPO / "deploy" / "systemd" / candidate
        if direct.exists():
            return direct.read_text()
        dropin_dir = REPO / "deploy" / "systemd" / f"{candidate}.d"
        if dropin_dir.is_dir():
            texts = [p.read_text() for p in sorted(dropin_dir.glob("*.conf"))]
            if texts:
                return "\n".join(texts)
    raise AssertionError(
        f"the header enumerates {name!r} as a ring writer, but no unit or "
        "drop-in of that name ships in deploy/systemd/ — the enumeration names "
        "something this repo does not install"
    )


def test_every_enumerated_ring_writer_declares_the_cadence_the_header_claims():
    """THE ENUMERATION IS EXECUTABLE, and the header is its single owner.

    "Every ring writer's cadence, so the set is checkable rather than assumed"
    is a claim the header makes about itself; this walks it. For each entry:
    the unit ships, its RestartSec is the enumerated value, that value is not
    BELOW the liveness window, and it declares its own start limit instead of
    inheriting systemd's default 5-in-10s.

    Why the header and not `RENDERER_LANES`: this guard used to iterate the
    lanes, so it structurally could not see a writer that is not a renderer —
    and jasper-snapclient became exactly that. The companion below keeps every
    lane present in the enumeration, so neither direction can rot.

    RestartSec is compared with `>=`, not `>`, because one enumerated writer
    genuinely sits ON the window: `jasper-camilla` restarts in 2 s, which the
    header records in so many words. Below the window is the shape this pin
    exists to refuse — a respawn that races its own predecessor's frozen
    heartbeat into -EBUSY. A writer that must strictly CLEAR the window says so
    where it is decided: the renderer lanes in the parametrized pin above, and
    jasper-snapclient in its own unit contract test.

    StartLimitBurst keeps its numeric bar, `> 5`, with ONE named exemption:
    `jasper-camilla` at exactly 5 (`_BURST_BAR_EXEMPTIONS`). Same shape as the
    `>=` relaxation above and as EG-7's `UMask` record — the gap is real, it is
    an AXIS-1 unit this phase does not touch, and it is written down rather
    than absorbed into a weaker bar that would let the NEXT writer land under
    it silently. The exemption is pinned to the exact value, so a regression
    below it fails here and closing the gap makes the exemption itself fail
    until it is deleted.
    """
    window = _ring_liveness_window_sec()
    for unit, enumerated in sorted(_enumerated_writer_cadences().items()):
        text = _unit_text_by_name(unit)
        declared = re.search(r"^RestartSec=(\d+(?:\.\d+)?)", text, re.M)
        assert declared, (
            f"the header enumerates {unit} at RestartSec={enumerated} but the "
            "unit declares none — the PACKAGED cadence is not visible to this "
            "repo, so it must be overridden here rather than assumed"
        )
        assert declared.group(1) == enumerated, (
            f"the header says {unit} restarts in {enumerated}s but the unit "
            f"declares {declared.group(1)}s"
        )
        assert float(declared.group(1)) >= window, (
            f"{unit} restarts in {declared.group(1)}s but a killed ring writer "
            f"stays refused for up to {window}s — a faster respawn races its "
            "own predecessor into EBUSY and can trip the start limit"
        )
        burst = re.search(r"^StartLimitBurst=(\d+)", text, re.M)
        interval = re.search(r"^StartLimitIntervalSec=(\d+)", text, re.M)
        assert burst and interval, (
            f"{unit} writes a ring and must declare its own start limit; "
            "systemd's default 5-in-10s parks the unit on an EBUSY burst"
        )
        declared_burst = int(burst.group(1))
        exempt = _BURST_BAR_EXEMPTIONS.get(unit)
        if exempt is not None:
            assert declared_burst == exempt, (
                f"{unit} is this file's ONE recorded exception to the ring "
                f"writers' StartLimitBurst > 5 bar, at {exempt}. It now "
                f"declares {declared_burst}: if the gap was closed, DELETE the "
                "_BURST_BAR_EXEMPTIONS entry so the bar applies to it like "
                "every other writer; if this is a regression, restore the value"
            )
            continue
        assert declared_burst > 5, (
            f"{unit} writes a ring and declares StartLimitBurst="
            f"{declared_burst}, no better than systemd's default 5-in-10s. A "
            "run of legitimate EBUSY refusals then PARKS the unit — the one "
            "non-self-healing shape in this design"
        )


def test_the_ring_liveness_window_is_enumerated_for_every_writer():
    """Every renderer lane appears in the header's enumeration. A writer missing
    from that list is one nobody checked — which is how bluealsa-aplay reached
    P6b as the third writer with its cadence unknown to the repo.

    The ADD direction (an entry whose unit or cadence is wrong) is the
    companion above; this is the DROP direction, keyed on the lane registry.
    """
    entries = _enumerated_writer_cadences()
    for lane in rl.RENDERER_LANES:
        if lane.unit is None:
            # Ephemeral writers have no unit cadence to enumerate; the
            # header instead names the lane's cadence CLASS explicitly, so
            # the "every writer is enumerated" property still holds.
            _assert_unitless_liveness_facts(lane)
            continue
        assert lane.unit in entries, (
            f"{lane.unit} writes a ring but has no `- {lane.unit} RestartSec=N` "
            "entry beside the liveness constant it must clear"
        )
    assert "jasper-camilla" in entries, "the Ring B writer dropped out"
    assert "jasper-snapclient.service" in entries, (
        "the grouping-ingress writer dropped out"
    )


def test_the_arm_asks_the_group_predicate_even_for_a_root_renderer(
    tmp_path, monkeypatch, capsys
):
    """The arm must CALL `renderer_user_in_ring_group`, not short-circuit past it.

    P6a's caller wrote `rl.renderer_user_in_ring_group(user) if user else None`,
    so a renderer with no `User=` (root — which bluealsa-aplay is) never reached
    the predicate: the root-is-capable branch was dead from production and the
    arm saw an indistinguishable "unknown". Restoring that short-circuit must
    fail here.

    This is a behavioural guard, not a source grep: it records the actual calls.
    """
    from jasper.cli import audio_config as ac

    calls: list[object] = []

    def recording(user, group=rl.RING_GROUP):
        calls.append(user)
        return True

    monkeypatch.setattr(rl, "renderer_user_in_ring_group", recording)
    monkeypatch.setattr(ac, "_renderer_unit_user", lambda unit: None)  # root
    # Imported inside the function, so patch it at its source module.
    import jasper.ring_assets as ring_assets

    monkeypatch.setattr(
        ring_assets, "ring_asset_presence",
        lambda: type("P", (), {"all_present": True, "missing": lambda self: ()})(),
    )
    monkeypatch.setattr(rl, "RENDERER_LANES_CONF_D", str(tmp_path / "conf"))
    (tmp_path / "conf").write_text("")

    args = argparse.Namespace(
        arm=["bluealsa"], disarm=[], set=None,
        path=str(tmp_path / "lanes.env"),
        input_buffer_frames=4096, period_frames=256,
    )
    rc = ac._cmd_renderer_lanes(args)
    assert rc == 0, capsys.readouterr().err

    assert calls == [None], (
        "the arm must ask the predicate exactly once, with the root renderer's "
        f"None; got {calls!r}. An empty list means the caller short-circuited "
        "and the root branch is dead again"
    )


def test_the_arm_cli_handles_the_unitless_lane(tmp_path, monkeypatch, capsys):
    """Arming `correction` preflights the row's non-root writer identity and
    names NO renderer unit in restart_required.

    Both halves were real crashes before P6c-ii's CLI edits (the banked
    "zero-edit" claim was false): `_renderer_unit_user(None)` fed
    ``Path(base, None)`` a ``TypeError``, and the restart set's
    ``" ".join(sorted({lane.unit ...}))`` cannot join ``None``. The unitless
    row instead supplies ``arm_preflight_user`` (jasper-web — its root
    writer identities pass the group predicate trivially), and the flip
    restarts jasper-fanin ONLY: the ephemeral writers read the map fresh on
    their next spawn.
    """
    from jasper.cli import audio_config as ac

    calls: list[object] = []

    def recording(user, group=rl.RING_GROUP):
        calls.append(user)
        return True

    monkeypatch.setattr(rl, "renderer_user_in_ring_group", recording)
    import jasper.ring_assets as ring_assets

    monkeypatch.setattr(
        ring_assets, "ring_asset_presence",
        lambda: type("P", (), {"all_present": True, "missing": lambda self: ()})(),
    )
    monkeypatch.setattr(rl, "RENDERER_LANES_CONF_D", str(tmp_path / "conf"))
    (tmp_path / "conf").write_text("")

    args = argparse.Namespace(
        arm=["correction"], disarm=[], set=None,
        path=str(tmp_path / "lanes.env"),
        input_buffer_frames=4096, period_frames=256,
    )
    rc = ac._cmd_renderer_lanes(args)
    out = capsys.readouterr()
    assert rc == 0, out.err

    assert calls == ["jasper-web"], (
        "the unitless lane's preflight must ask about its arm_preflight_user; "
        f"got {calls!r}"
    )
    restart_lines = [
        line for line in out.out.splitlines()
        if line.startswith("restart_required")
    ]
    assert restart_lines == ["restart_required jasper-fanin.service"], (
        "a unitless flip restarts fan-in only — no renderer unit exists to "
        f"bounce; got {restart_lines!r}"
    )


# --- #2344 fourth state: ingress-armed + active-endpoint-unarmed -----------
#
# "Is this end of the graph a ring end?" is answered by RING_PCM_DEVICES
# membership of the graph's resolved PLAYBACK device — the CamillaDSP-endpoint
# axis, read by the emit derivation and by the arm's width gate. The correction
# lane arms the INGRESS axis: what measurement spawns OPEN, never what the graph
# plays out of. The two tests below pin that independence from both directions,
# so the fourth state (ingress armed, endpoint still aloop) cannot be mistaken
# for an armed endpoint.


def test_ingress_lane_devices_are_never_camilla_endpoint_rings():
    """Neither of the correction lane's devices may join RING_PCM_DEVICES.

    Set-membership there is what the emit derivation keys on
    (`active_emit_devices` / `capture_device_for_playback`: "is this end of the
    graph a ring end?") and what the CamillaDSP-endpoint guard keys on
    (`ring_edge_width_ready`). Adding an ingress device to that set would make
    arming ingress read as "the active graph plays out of a ring" — the exact
    conflation #2344's fourth state exists to rule out. Until #2412's Wave 3 it
    was also the commissioning gate's own predicate
    (`resolved_playback_device not in RING_PCM_DEVICES`); that gate now proves
    both ends of the emitted graph agree, which reads the same set through the
    derivation rather than beside it.
    """
    from jasper.fanin_coupling import RING_PCM_DEVICES

    lane = rl.lane_by_label("correction")
    assert lane is not None
    assert lane.ring_device not in RING_PCM_DEVICES
    assert lane.aloop_device not in RING_PCM_DEVICES


@pytest.mark.parametrize("ingress_armed", [False, True])
@pytest.mark.parametrize("endpoint_ringed", [False, True])
def test_ingress_arming_and_endpoint_transport_are_independent_axes(
    ingress_armed, endpoint_ringed, tmp_path
):
    """All four states of (ingress armed?, endpoint ringed?): each axis
    answers from its own source, unmoved by the other.

    The endpoint answer is RING_PCM_DEVICES membership of the resolved
    playback device (the resolved device comes from the CamillaDSP statefile,
    which the lane map never feeds); the ingress answer is the transport reader
    over the lane map (which the statefile never feeds). The fourth state —
    ingress armed while the endpoint is still the aloop tap — must therefore
    still read as a NON-ring endpoint while the measurement spawns move to the
    ring, so nothing downstream treats an armed ingress as an armed endpoint.
    """
    from jasper.audio_measurement.correction_lane import (
        CORRECTION_SUBSTREAM,
        correction_play_device,
    )
    from jasper.fanin_coupling import RING_PCM_DEVICES, RING_PLAYBACK_DEVICE

    lane = rl.lane_by_label("correction")
    assert lane is not None

    map_path = str(pathlib.Path(tmp_path) / "renderer_lanes.env")
    original = rl.RENDERER_LANES_ENV
    rl.RENDERER_LANES_ENV = map_path
    try:
        if ingress_armed:
            pathlib.Path(map_path).write_text(
                rl.render_env_text((lane.label,))
            )
        playback_device = (
            RING_PLAYBACK_DEVICE if endpoint_ringed else "jasper_out"
        )
        endpoint_is_ring = playback_device in RING_PCM_DEVICES

        # The endpoint axis answers from the playback device alone.
        assert endpoint_is_ring == endpoint_ringed
        # The ingress axis answers from the lane map alone.
        expected_device = (
            lane.ring_device if ingress_armed else CORRECTION_SUBSTREAM
        )
        assert correction_play_device() == expected_device
    finally:
        rl.RENDERER_LANES_ENV = original


# --- The conf-renderer lane (airplay, U3 / P6d) ----------------------------


def test_conf_renderer_airplay_facts():
    """The airplay lane's conf-renderer facts, asserted directly.

    Redundant with the routed pins above BY DESIGN (they reach the same
    assertions through the row): this direct call keeps the conf-renderer
    facts diagnosable as their own failure — a drift names this test, not
    a generic per-lane loop. Same shape as
    `test_unitless_correction_writer_facts`.
    """
    lane = rl.lane_by_label("airplay")
    assert lane is not None and lane.conf_renderer is not None
    _assert_conf_renderer_device_facts(lane)
    _assert_conf_renderer_static_facts(lane)


def test_the_conf_renderer_path_is_real():
    """The row's `conf_renderer` names a file that exists in the repo — a
    renamed/moved script would otherwise fail only at the subprocess pins,
    with a less obvious message."""
    lane = rl.lane_by_label("airplay")
    assert lane is not None and lane.conf_renderer is not None
    assert (REPO / lane.conf_renderer).is_file(), lane.conf_renderer


def _arm_via_cli(monkeypatch, tmp_path, capsys, labels):
    """Run a real `_cmd_renderer_lanes` arm with the host probes stubbed.

    Same stubbing shape as
    `test_the_arm_asks_the_group_predicate_even_for_a_root_renderer`:
    presence/conf/user answered locally so the decision logic runs for
    real, off-box.
    """
    from jasper.cli import audio_config as ac

    monkeypatch.setattr(
        rl, "renderer_user_in_ring_group", lambda user, group=rl.RING_GROUP: True
    )
    monkeypatch.setattr(ac, "_renderer_unit_user", lambda unit: None)
    import jasper.ring_assets as ring_assets

    monkeypatch.setattr(
        ring_assets, "ring_asset_presence",
        lambda: type("P", (), {"all_present": True, "missing": lambda self: ()})(),
    )
    monkeypatch.setattr(rl, "RENDERER_LANES_CONF_D", str(tmp_path / "conf"))
    (tmp_path / "conf").write_text("")

    args = argparse.Namespace(
        arm=list(labels), disarm=[], set=None,
        path=str(tmp_path / "lanes.env"),
        input_buffer_frames=4096, period_frames=256,
    )
    rc = ac._cmd_renderer_lanes(args)
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    return captured.out


def test_arming_airplay_prints_the_transport_advisory(
    monkeypatch, tmp_path, capsys
):
    """A NEWLY armed lane with an `arm_advisory` prints it — the operator
    carries AirPlay's sync boundary to the box's source pass from this
    terminal, not from remembering to open a doc."""
    out = _arm_via_cli(monkeypatch, tmp_path, capsys, ["airplay"])
    assert "advisory airplay: " in out
    lane = rl.lane_by_label("airplay")
    assert lane is not None and lane.arm_advisory
    assert lane.arm_advisory in out, (
        "the CLI must print the row's advisory verbatim, not a paraphrase"
    )


def test_arming_a_lane_without_an_advisory_prints_none(
    monkeypatch, tmp_path, capsys
):
    """Control for the advisory print: a no-advisory lane arms silently.
    Guards against the print firing unconditionally (which would teach
    operators to skim past it)."""
    out = _arm_via_cli(monkeypatch, tmp_path, capsys, ["spotify"])
    assert "advisory" not in out
    lane = rl.lane_by_label("spotify")
    assert lane is not None and lane.arm_advisory is None


def test_the_advisorys_sync_numbers_match_the_templates_live_values():
    """Every sync number in the advisory must follow the shipped template.

    The advisory is operator-facing but the template owns the values. Pin all
    three restated numbers so retuning any one cannot leave stale arm guidance.
    """
    template = SHAIRPORT_TEMPLATE.read_text(encoding="utf-8")

    lane = rl.lane_by_label("airplay")
    assert lane is not None and lane.arm_advisory
    expected = (
        f"drift_tolerance={template_value(template, 'drift_tolerance_in_seconds')}",
        "resync_threshold at "
        f"{template_value(template, 'resync_threshold_in_seconds')}",
        "audio_backend_buffer_desired_length at "
        f"{template_value(template, 'audio_backend_buffer_desired_length_in_seconds')}",
    )
    for claim in expected:
        assert claim in lane.arm_advisory, (
            f"the advisory must carry the template's live value: {claim}"
        )
