# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the doctor's SHM-ring transport and lane checks."""

from pathlib import Path

import pytest

from jasper.cli import doctor
import os
import struct
import subprocess
from types import SimpleNamespace
from jasper.audio_hardware.dac import (
    HIFIBERRY_DAC8X_STUDIO_ID,
    LatencyFloor,
    latency_floor_for,
)
from jasper.cli.doctor import (
    audio_runtime_ring,
)
from jasper.fanin_coupling import (
    RING_ACTIVE_PLAYBACK_DEVICE,
    RING_PLAYBACK_DEVICE,
)

from .doctor_test_support import record_active_dac
from ._doctor_audio_runtime_fixtures import (
    _outputd_status_payload,
    _patch_fanin_status_socket,
    _patch_fanin_systemctl,
    _stage_floor_conf,
)


def test_the_arm_waypoint_is_reported_once_by_the_check_that_owns_it(
    monkeypatch, tmp_path
):
    """ONE fact, ONE check. This pins the de-duplication, in both directions.

    The ACTIVE-ring arm waypoint — a loaded graph naming the ACTIVE ring while
    outputd is off the ring bridge — used to surface twice:
    `check_ring_split_transport`
    FAILed on it, and `check_outputd_service` separately elevated the same
    detector's note to a WARN. Same statefile, same two terms, two check names,
    two severities: a household or an operator reading `jasper-doctor` saw one
    problem written up as two, and the louder of the two already carried the
    runnable remedy.

    So `check_outputd_service` is now silent on the waypoint and the split check
    still names it. Asserting only the first half would pass just as well if the
    finding had been dropped altogether, which is the failure this whole wave is
    supposed to avoid — so the FAIL is asserted in the same test.
    """
    from jasper.audio_runtime_plan import (
        output_endpoint_evidence_from_statefiles as _real_endpoint_evidence,
    )
    from jasper.fanin_coupling import RING_ACTIVE_PLAYBACK_DEVICE

    # ONE on-disk statefile pair drives BOTH halves — no stubbed evidence
    # resolution. Canning the endpoint evidence for half one and the loaded
    # config reader for half two feeds the two surfaces from independent
    # fixtures, which lets them disagree about the box without reddening
    # anything. Same bytes to both, or the guard is theatre.
    config = tmp_path / "loaded.yml"
    config.write_text(
        "devices:\n"
        "  samplerate: 48000\n"
        "  capture:\n    type: Alsa\n    device: jts_ring_capture\n"
        f"  playback:\n    type: Alsa\n    device: {RING_ACTIVE_PLAYBACK_DEVICE}\n",
        encoding="utf-8",
    )
    statefile = tmp_path / "outputd-statefile.yml"
    statefile.write_text(f"config_path: {config}\n", encoding="utf-8")
    absent = tmp_path / "crossover-statefile.yml"

    monkeypatch.setattr(
        audio_runtime_ring, "_active_camilla_config_path",
        lambda: (str(statefile), str(config)),
    )
    monkeypatch.setattr(
        "jasper.audio_runtime_plan.DEFAULT_CAMILLA_STATEFILE_PATH", str(statefile)
    )
    monkeypatch.setattr(
        "jasper.audio_runtime_plan.DEFAULT_CAMILLA2_STATEFILE_PATH", str(absent)
    )
    # The bridge has to be STATED to make the graph above a split: an absent
    # key is the ring, and the ring agrees with the ring graph. The STATUS
    # payload is the daemon that came up on THIS env — the retired source and
    # its negotiated ALSA buffer — because the subject here is which check
    # names the split, not whether the daemon reporting it survives startup.
    outputd_env = tmp_path / "outputd.env"
    outputd_env.write_text("JASPER_OUTPUTD_CONTENT_BRIDGE=direct\n", encoding="utf-8")
    monkeypatch.setenv("JASPER_OUTPUTD_ENV_FILE", str(outputd_env))
    _patch_fanin_systemctl(monkeypatch)
    _patch_fanin_status_socket(
        monkeypatch,
        _outputd_status_payload(content_source="alsa", content_buffer_frames=4096),
    )
    # `_patch_fanin_status_socket` also cans `output_endpoint_evidence_from_statefiles`
    # from the STATUS payload — convenient for the checks whose subject is the
    # payload, and fatal for this one, whose subject IS the evidence resolution.
    # Put the real reader back so both halves resolve the statefiles above.
    monkeypatch.setattr(
        "jasper.audio_runtime_plan.output_endpoint_evidence_from_statefiles",
        _real_endpoint_evidence,
    )

    r = doctor.check_outputd_service()

    # Half one: outputd no longer restates the split.
    assert r.status == "ok", r.detail
    assert "arm waypoint" not in r.detail

    # Half two: the check that OWNS the split still fails on it, with the
    # remedy — off the same statefile half one just read.
    split = audio_runtime_ring.check_ring_split_transport()

    assert split.status == "fail", split.detail
    assert "jasper-fanin-coupling-reconcile shm_ring" in split.detail
    # #2285 P2: the retired rollback endpoint must never be printed as a
    # remediation — argparse rejects it, and a command the operator cannot run
    # is worse than none, because they do the work of trying it.
    assert "--endpoint aloop" not in split.detail


# ---- renderer ring lanes: the unarmed fleet default is healthy ----------


def test_unarmed_renderer_lanes_report_ok(monkeypatch):
    """An unarmed box (the fleet default) is healthy, not failing.

    The doctor's status vocabulary is ok|warn|fail (CheckResult.status),
    and render() maps anything else to a red X through its else-branch.
    The first ship of this check returned a novel "skip" for the unarmed
    branch, which made EVERY unarmed box render a failure and exit 1
    (AGENTS.md: "Returns 0 if all critical checks pass"). Unarmed/
    unconfigured-is-ok is the established doctor convention —
    check_chip_reference (this same domain), Spotify auth and Google
    integrations all return ok + a skipped-style detail.
    """
    import jasper.renderer_lanes as rl

    monkeypatch.setattr(rl, "read_armed_labels", lambda *a, **kw: ())
    result = doctor.audio_runtime_ring.check_renderer_ring_lanes()
    assert result.status == "ok"
    assert "no renderer lane armed" in result.detail
    assert "fleet default" in result.detail


def test_unarmed_renderer_lanes_exit_zero_through_render(monkeypatch, capsys):
    """The fleet-wide repro, inverted into a guard: drive the unarmed
    result through the doctor's REAL render/exit path and require exit 0.
    A status outside the vocabulary reaches render()'s else-branch and
    becomes exit 1 — exactly how the "skip" ship failed — so this pin
    breaks on the defect CLASS, not just today's literal."""
    import jasper.renderer_lanes as rl

    monkeypatch.setattr(rl, "read_armed_labels", lambda *a, **kw: ())
    result = doctor.audio_runtime_ring.check_renderer_ring_lanes()
    exit_code = doctor.render([result])
    capsys.readouterr()  # swallow render()'s printed report
    assert exit_code == 0


def _resting_ring_entry(label):
    """An armed lane's STATUS entry: attached, never fed since attach."""
    from jasper.fanin.status import FANIN_INPUT_SOURCE_RING

    return {
        "label": label,
        "source": FANIN_INPUT_SOURCE_RING,
        "frames_read": 0,
        "ring": {
            "attached": True,
            "startup_empty_reads": 40,
            "empty_reads": 0,
            "writer_alive": False,
            "occupancy": 0,
            "epoch_resets": 0,
        },
    }


def test_armed_on_demand_lane_resting_state_is_healthy(monkeypatch):
    """An armed correction lane that has never been fed is RESTING, not
    broken (U3/P6c-ii): its writers are ephemeral measurement spawns, so
    between measurements the ring sits attached with only startup empty
    reads — the same "not a fault" class as a paused renderer. The
    never-fed WARN diagnoses a DAEMON renderer that failed to reopen its
    ring after an arm; applying it to an on-demand lane would put a
    standing false warning on every armed box.

    The detail must also CARRY the carve-out's stated residual: resting
    and broken-at-open are byte-identical to this check, so the healthy
    line must hint at the unproven writer path rather than implying it
    was verified (an operator reading "ok" as "the lane works" would be
    over-reading — the hint is what keeps the reading honest)."""
    import jasper.renderer_lanes as rl

    monkeypatch.setattr(rl, "read_armed_labels", lambda *a, **kw: ("correction",))
    monkeypatch.setattr(
        doctor.audio_runtime_ring,
        "_read_status_socket",
        lambda _path: {"inputs": [_resting_ring_entry("correction")]},
    )
    result = doctor.audio_runtime_ring.check_renderer_ring_lanes()
    assert result.status == "ok"
    assert "on-demand" in result.detail
    assert "no measurement played yet" in result.detail
    assert "a writer that cannot open looks identical here" in result.detail
    assert "run a measurement to confirm" in result.detail


def test_armed_daemon_lane_never_fed_still_warns(monkeypatch):
    """Control for the on-demand carve-out: a unit-ful lane in the same
    never-fed state keeps the WARN and its restart-the-unit remedy — the
    carve-out is keyed on the lane's writers, not applied lane-wide."""
    import jasper.renderer_lanes as rl

    monkeypatch.setattr(
        rl, "read_armed_labels", lambda *a, **kw: ("spotify", "correction")
    )
    monkeypatch.setattr(
        doctor.audio_runtime_ring,
        "_read_status_socket",
        lambda _path: {
            "inputs": [
                _resting_ring_entry("spotify"),
                _resting_ring_entry("correction"),
            ]
        },
    )
    result = doctor.audio_runtime_ring.check_renderer_ring_lanes()
    assert result.status == "warn"
    assert "spotify" in result.detail and "NEVER FED" in result.detail
    assert "librespot.service" in result.detail
    assert "correction:" not in result.detail  # no correction problem entry

# A registered profile that declares NO LatencyFloor, so the no-floor branches
# below exercise the real registry rather than a synthetic id. Asserted rather
# than assumed: declaring a floor for this profile must fail THIS line, not
# silently turn the two no-floor tests into vacuous passes against a branch
# they no longer reach. (That is exactly what a declared DAC8x floor did to
# them in R7a, when they were written against `hifiberry_dac8x`; the guard has
# now caught it a SECOND time, when the InnoMaker HiFi AMP Pro declared jts4's
# measured floor and this moved to the DAC8x Studio.)
NO_FLOOR_DAC_ID = HIFIBERRY_DAC8X_STUDIO_ID
assert latency_floor_for(NO_FLOOR_DAC_ID) is None, (
    f"{NO_FLOOR_DAC_ID} now declares a latency floor; pick another floorless "
    "profile for the no-floor doctor branches"
)


def test_skips_when_no_active_dac_is_recorded(monkeypatch, tmp_path):
    """The env's JASPER_AUDIO_DAC_ID is not consulted: with no record there is
    no declared floor to render (the missing record is another check's warn)."""
    monkeypatch.setenv("JASPER_AUDIO_DAC_ID", "apple_usb_c_dongle")
    monkeypatch.setattr(audio_runtime_ring, "_JTS_RING_CONF_D", str(tmp_path / "60-jts-ring.conf"))

    result = audio_runtime_ring.check_ring_conf_floor_render()

    assert result.status == "ok"
    assert "names no active DAC" in result.detail


def test_skips_when_the_recorded_dac_is_only_partially_present(
    monkeypatch, tmp_path
):
    """A partial single-DAC record is not ACTIVE (the reconciler does not
    drive it), so this renders the same ok/skipped branch as no record at
    all — the conf.d floor is never read for a DAC the reconciler only
    observed, even though the conf.d file itself exists on disk."""
    _stage_floor_conf(
        monkeypatch, tmp_path, dac_id="apple_usb_c_dongle", status="partial"
    )

    result = audio_runtime_ring.check_ring_conf_floor_render()

    assert result.status == "ok"
    assert "names no active DAC" in result.detail


def _conf_text(period_frames):
    return (
        f"pcm.jts_ring_capture {{\n    period_frames {period_frames}\n"
        "    n_slots 2\n}\n"
        f"pcm.jts_ring_playback {{\n    period_frames {period_frames}\n"
        "    n_slots 2\n}\n"
    )


def _synthetic_floor(period_frames):
    return LatencyFloor(
        outputd_period_frames=period_frames,
        outputd_dac_buffer_frames=4 * period_frames,
    )


def test_ok_when_the_dac_declares_no_floor(monkeypatch, tmp_path):
    # State 1: nothing to render — the shipped default stands by rule.
    _stage_floor_conf(monkeypatch, tmp_path, dac_id=NO_FLOOR_DAC_ID)

    result = audio_runtime_ring.check_ring_conf_floor_render()

    assert result.status == "ok"
    assert "no latency floor" in result.detail
    assert "shipped default" in result.detail


def test_ok_when_the_conf_matches_the_declared_floor(monkeypatch, tmp_path):
    # State 2: the golden Apple case — the declared floor IS the shipped 128.
    _stage_floor_conf(monkeypatch, tmp_path, dac_id="apple_usb_c_dongle")

    result = audio_runtime_ring.check_ring_conf_floor_render()

    assert result.status == "ok"
    assert "128" in result.detail
    assert "apple_usb_c_dongle" in result.detail


def test_ok_when_the_floor_exceeds_the_fixed_ring_slot(monkeypatch, tmp_path):
    # State 4, the product boundary: Ring A's slot is fan-in's COMPILE-TIME
    # RING_SLOT_FRAMES, so a DAC whose floor is not exactly that never gets a
    # rendered conf.d this way — shm_ring's floor-optimal period is simply out
    # of reach here until #2147. That is a documented boundary, NOT drift, so
    # it reports ok.
    from jasper.fanin_coupling import RING_SLOT_FRAMES

    monkeypatch.setattr(
        audio_runtime_ring,
        "latency_floor_for",
        lambda _id: _synthetic_floor(2 * RING_SLOT_FRAMES),
    )
    _stage_floor_conf(monkeypatch, tmp_path, dac_id="hifiberry_dac8x")

    result = audio_runtime_ring.check_ring_conf_floor_render()

    assert result.status == "ok"
    # Honest: names both numbers, says what is unavailable and what still works.
    assert str(2 * RING_SLOT_FRAMES) in result.detail
    assert str(RING_SLOT_FRAMES) in result.detail
    assert "shm_ring" in result.detail
    assert "loopback" not in result.detail.lower()
    assert "#2147" in result.detail


def test_warns_when_the_conf_diverges_from_the_declared_floor(
    monkeypatch, tmp_path
):
    # State 5, real drift: the Apple dongle's floor IS renderable (it equals
    # RING_SLOT_FRAMES) but this box's conf.d was never rendered to it. Warn,
    # not fail — an unrendered conf.d is inert until something arms shm_ring
    # against it, so this is a dormant render gap, not a live fault.
    _stage_floor_conf(
        monkeypatch,
        tmp_path,
        dac_id="apple_usb_c_dongle",
        conf_text=_conf_text(1024),
    )

    result = audio_runtime_ring.check_ring_conf_floor_render()

    assert result.status == "warn"
    # Names BOTH numbers and the remedy, not a bare "mismatch".
    assert "128" in result.detail
    assert "1024" in result.detail
    assert "jasper-audio-hardware-reconcile" in result.detail


@pytest.mark.parametrize(
    "conf_text",
    [
        # Torn: the two PCMs disagree, so there is no single geometry.
        "pcm.jts_ring_capture {\n    period_frames 128\n}\n"
        "pcm.jts_ring_playback {\n    period_frames 1024\n}\n",
        # No period_frames line at all.
        "pcm.jts_ring_capture { type jts_ring }\n",
    ],
)
def test_warns_when_the_conf_period_is_indeterminate(
    monkeypatch, tmp_path, conf_text
):
    _stage_floor_conf(
        monkeypatch, tmp_path, dac_id="apple_usb_c_dongle", conf_text=conf_text
    )

    result = audio_runtime_ring.check_ring_conf_floor_render()

    assert result.status == "warn"
    assert "no single period_frames" in result.detail
    assert "redeploy" in result.detail


def test_warns_when_the_conf_is_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(audio_runtime_ring, "_JTS_RING_CONF_D", str(tmp_path / "missing.conf"))
    record_active_dac("apple_usb_c_dongle")

    result = audio_runtime_ring.check_ring_conf_floor_render()

    assert result.status == "warn"
    assert "absent or torn" in result.detail


def test_check_is_registered_in_the_audio_doctor_group():
    from jasper.cli.doctor import _registry

    entry = next(
        c for c in _registry.registered_checks()
        if c.func is audio_runtime_ring.check_ring_conf_floor_render
    )
    assert entry.group == "audio"


# --- #2294: a floor-blocked box must SAY it cannot ring -----------------------


def test_no_floor_detail_names_both_routes_to_a_ring(monkeypatch, tmp_path):
    """`ok` is right, silence was not (issue #2294) — and neither was the old text.

    A DAC with no declared floor leaves outputd on its PACKAGED default period,
    which is not the fixed ring slot, so the conf.d has nothing to render. The
    conf.d is correct either way — hence `ok` — but the household's actual
    question is "why hasn't this box reached shm_ring?", and before #2294 the
    answer appeared on no surface.

    The answer it then gave was wrong. It said shm_ring was "unavailable on this
    box", which is a claim about outputd's RESOLVED period, and this check reads
    only the DECLARED floor. The two diverge through the operator seam: jts4
    (InnoMaker HiFi AMP Pro, floorless at the time) armed shm_ring on 2026-08-14
    with JASPER_OUTPUTD_PERIOD_FRAMES / _DAC_BUFFER_FRAMES hand-set in
    /etc/jasper/jasper.env. So the detail must name BOTH routes to a ring and
    claim no impossibility.
    """
    from jasper.audio_runtime_plan import DEFAULT_OUTPUTD_PERIOD_FRAMES
    from jasper.fanin_coupling import RING_SLOT_FRAMES

    _stage_floor_conf(monkeypatch, tmp_path, dac_id=NO_FLOOR_DAC_ID)

    result = audio_runtime_ring.check_ring_conf_floor_render()

    assert result.status == "ok"
    assert str(DEFAULT_OUTPUTD_PERIOD_FRAMES) in result.detail
    assert str(RING_SLOT_FRAMES) in result.detail
    assert "#2147" in result.detail
    # Route 1: the operator seam, named with the file that carries it.
    assert "JASPER_OUTPUTD_PERIOD_FRAMES" in result.detail
    assert "/etc/jasper/jasper.env" in result.detail
    # Route 2: a declared floor.
    assert "floor" in result.detail
    # And NO impossibility claim, on a check that cannot see the resolved period.
    assert "unavailable" not in result.detail


def test_a_floor_above_the_slot_does_not_claim_ineligibility(monkeypatch, tmp_path):
    """The same falsified claim lived in the floor-exceeds-slot branch.

    A DAC declaring, say, a 256-frame floor gets no rendered conf.d either — but
    an operator JASPER_OUTPUTD_PERIOD_FRAMES still outranks that floor and can
    bring the resolved period back to the slot, exactly as it did on jts4. So
    this branch may say the conf.d is not rendered and name the seam; it may not
    say the ring is unavailable.
    """
    from jasper.fanin_coupling import RING_SLOT_FRAMES

    monkeypatch.setattr(
        audio_runtime_ring,
        "latency_floor_for",
        lambda _id: _synthetic_floor(2 * RING_SLOT_FRAMES),
    )
    _stage_floor_conf(monkeypatch, tmp_path, dac_id="hifiberry_dac8x")

    result = audio_runtime_ring.check_ring_conf_floor_render()

    assert result.status == "ok"
    assert "unavailable" not in result.detail
    assert "JASPER_OUTPUTD_PERIOD_FRAMES" in result.detail


def test_a_matching_floor_does_not_claim_ineligibility(monkeypatch, tmp_path):
    # The mirror of the above: a box that CAN ring must not be told it cannot.
    _stage_floor_conf(monkeypatch, tmp_path, dac_id="apple_usb_c_dongle")

    result = audio_runtime_ring.check_ring_conf_floor_render()

    assert result.status == "ok"
    assert "unavailable" not in result.detail


# ===========================================================================
# check_ring_geometry_coherence
#
# Ring-A geometry must agree across three axes when shm_ring is armed:
# fan-in's resolved JASPER_FANIN_RING_SLOTS through the systemd env chain, the
# conf.d jts_ring_capture, and the on-disk program.ring header. Against the
# header it compares every axis the ioplug attach compares (n_slots,
# period_frames, sample_format, channels); a mismatch on any of them is the
# 2026-07-05 crash-loop class (hw_params EINVAL + ioplug attach_fatal).
# ===========================================================================

def _write_conf(tmp_path, *, capture_n_slots=2):
    conf = tmp_path / "60-jts-ring.conf"
    conf.write_text(
        f"pcm.jts_ring_capture {{\n    period_frames 128\n    n_slots {capture_n_slots}\n}}\n"
        "pcm.jts_ring_playback {\n    period_frames 128\n    n_slots 2\n}\n",
        encoding="utf-8",
    )
    return conf


def _write_ring(
    path,
    *,
    n_slots=2,
    period_frames=128,
    magic=0x4A52_494E,
    rate=48000,
    channels=2,
    sample_format=1,  # SAMPLE_FORMAT_S16LE
):
    hdr = bytearray(128)
    struct.pack_into("<I", hdr, 0, magic)
    struct.pack_into("<I", hdr, 4, 1)  # version
    struct.pack_into("<I", hdr, 8, rate)
    struct.pack_into("<I", hdr, 12, channels)
    struct.pack_into("<I", hdr, 16, sample_format)
    struct.pack_into("<I", hdr, 20, period_frames)
    struct.pack_into("<I", hdr, 24, n_slots)
    path.write_bytes(bytes(hdr) + b"\x00" * 256)


def _stage_ring_geometry(
    monkeypatch,
    tmp_path,
    *,
    fanin_env_text="",
    jasper_env_text="",
    capture_n_slots=2,
):
    conf = _write_conf(tmp_path, capture_n_slots=capture_n_slots)
    fanin_env = tmp_path / "fanin.env"
    jasper_env = tmp_path / "jasper.env"
    fanin_env.write_text(fanin_env_text, encoding="utf-8")
    jasper_env.write_text(jasper_env_text, encoding="utf-8")
    program = tmp_path / "program.ring"
    monkeypatch.setattr(audio_runtime_ring.ring_assets, "RING_CONF_D", str(conf))
    monkeypatch.setattr(audio_runtime_ring, "_JTS_RING_CONF_D", str(conf))
    monkeypatch.setattr(audio_runtime_ring.ring_assets, "RING_A_PROGRAM_FILE", str(program))
    monkeypatch.setattr(
        "jasper.fanin.ring_health.FANIN_ENV_PATH", str(fanin_env)
    )
    monkeypatch.setattr(
        "jasper.fanin.ring_health.JASPER_ENV_PATH", str(jasper_env)
    )
    return fanin_env, program


def test_a_slot_mismatch_is_assessed_with_no_persisted_coupling(monkeypatch, tmp_path):
    """Ring A is never inert, so no persisted token may switch this check off.

    The gate this replaces skipped whenever ``JASPER_FANIN_CAMILLA_COUPLING``
    did not read ``shm_ring`` — which is every box the reconciler has not
    written yet, on which the graph opens Ring A regardless. A crash-loop-class
    geometry mismatch went unreported on exactly those boxes.
    """
    _stage_ring_geometry(monkeypatch, tmp_path, fanin_env_text="JASPER_FANIN_RING_SLOTS=8\n")
    res = audio_runtime_ring.check_ring_geometry_coherence()
    assert res.status == "fail"


def test_ok_when_all_three_axes_agree(monkeypatch, tmp_path):
    _fanin, program = _stage_ring_geometry(monkeypatch, tmp_path)
    _write_ring(program)
    res = audio_runtime_ring.check_ring_geometry_coherence()
    assert res.status == "ok", res.detail
    assert "n_slots=2" in res.detail


def test_ok_detail_reports_the_on_disk_wire(monkeypatch, tmp_path):
    """The ok detail names the wire it compared, not just the slot geometry.

    A reader that can see the whole header should say what it saw — otherwise
    "the Python layer can see the wire" is true only inside tests.
    """
    _fanin, program = _stage_ring_geometry(monkeypatch, tmp_path)
    _write_ring(program)
    res = audio_runtime_ring.check_ring_geometry_coherence()
    assert res.status == "ok", res.detail
    assert "S16_LE/2ch" in res.detail
    assert "48000 Hz" in res.detail


@pytest.mark.parametrize(
    ("kwargs", "axis"),
    [
        ({"sample_format": 2}, "sample_format"),
        ({"channels": 6}, "channels"),
    ],
)
def test_fail_when_the_on_disk_wire_shears(monkeypatch, tmp_path, kwargs, axis):
    """THE HOLE R5b CLOSED, and this test used to assert it stayed open.

    Its earlier form wrote a 44.1 kHz / 6-channel / S32_LE header — a wire the
    fleet does not run — and asserted ``ok``, because the wire axes were
    REPORTED and compared nowhere. That is exactly the silent hole: a ring file
    whose slots and period match while its format or channel count does not
    passed every Python guard, and the first symptom would have been CamillaDSP
    failing the ioplug attach and crash-looping. The inversion is deliberate.

    ``rate`` is NOT in the parametrize list: the conf.d declares no rate, so
    there is no expected value to compare against and the comparator skips that
    axis rather than guessing. It stays reported.
    """
    _fanin, program = _stage_ring_geometry(monkeypatch, tmp_path)
    _write_ring(program, **kwargs)
    res = audio_runtime_ring.check_ring_geometry_coherence()
    assert res.status == "fail", res.detail
    assert axis in res.detail
    assert "jasper-fanin-coupling-reconcile" in res.detail


def test_fail_when_env_disagrees_with_conf(monkeypatch, tmp_path):
    # Default migration class: stale JASPER_FANIN_RING_SLOTS=8 vs conf.d's 2.
    _fanin, program = _stage_ring_geometry(
        monkeypatch, tmp_path, fanin_env_text="JASPER_FANIN_RING_SLOTS=8\n",
    )
    _write_ring(program, n_slots=8)
    res = audio_runtime_ring.check_ring_geometry_coherence()
    assert res.status == "fail"
    assert "2" in res.detail and "8" in res.detail
    assert "crash-loop" in res.detail.lower()


def test_fail_when_base_env_disagrees_with_conf(monkeypatch, tmp_path):
    # The effective env chain is /etc/jasper/jasper.env, then fanin.env. A stale
    # base-env value still controls the next fan-in start when fanin.env has no
    # override, so doctor must not report the product default.
    _fanin, program = _stage_ring_geometry(
        monkeypatch,
        tmp_path,
        jasper_env_text="JASPER_FANIN_RING_SLOTS=8\n",
    )
    _write_ring(program, n_slots=8)
    res = audio_runtime_ring.check_ring_geometry_coherence()
    assert res.status == "fail"
    assert "2" in res.detail and "8" in res.detail
    assert "crash-loop" in res.detail.lower()


def test_ok_when_fanin_env_overrides_stale_base_env(monkeypatch, tmp_path):
    # Later systemd EnvironmentFile wins. The reconciler's fix writes this exact
    # fanin.env override to neutralize stale /etc/jasper/jasper.env residue.
    _fanin, program = _stage_ring_geometry(
        monkeypatch,
        tmp_path,
        fanin_env_text="JASPER_FANIN_RING_SLOTS=2\n",
        jasper_env_text="JASPER_FANIN_RING_SLOTS=8\n",
    )
    _write_ring(program, n_slots=2)
    res = audio_runtime_ring.check_ring_geometry_coherence()
    assert res.status == "ok", res.detail
    assert "n_slots=2" in res.detail


def test_fail_when_on_disk_ring_disagrees(monkeypatch, tmp_path):
    # env + conf.d agree (both 2), but a stale on-disk ring carries 8 slots — the
    # ioplug attach still fails. Caught as the third axis.
    _fanin, program = _stage_ring_geometry(monkeypatch, tmp_path)
    _write_ring(program, n_slots=8)
    res = audio_runtime_ring.check_ring_geometry_coherence()
    assert res.status == "fail"
    assert "on-disk" in res.detail.lower()
    assert "2" in res.detail and "8" in res.detail


def test_fail_when_on_disk_ring_period_disagrees(monkeypatch, tmp_path):
    # Nit-7: env + conf.d + on-disk n_slots all agree (2), but the on-disk ring's
    # period_frames is stale (256 vs conf.d 128). The ioplug attach still fails on
    # the SECOND geometry axis — the slot-only check would miss it. Caught now.
    _fanin, program = _stage_ring_geometry(monkeypatch, tmp_path)
    _write_ring(program, period_frames=256)
    res = audio_runtime_ring.check_ring_geometry_coherence()
    assert res.status == "fail"
    assert "period_frames" in res.detail
    assert "256" in res.detail and "128" in res.detail


def test_ok_when_slots_and_period_both_agree(monkeypatch, tmp_path):
    # The positive: n_slots AND period_frames coherent across env + conf.d + on-disk.
    _fanin, program = _stage_ring_geometry(monkeypatch, tmp_path)
    _write_ring(program, period_frames=128)
    res = audio_runtime_ring.check_ring_geometry_coherence()
    assert res.status == "ok", res.detail
    assert "period_frames=128" in res.detail


def test_warn_when_ring_file_absent_but_env_conf_agree(monkeypatch, tmp_path):
    # Armed, env + conf.d agree, but no valid on-disk ring yet (fan-in restarting).
    # Not a hard failure — the next writer create will be coherent.
    _stage_ring_geometry(monkeypatch, tmp_path)
    # No _write_ring — the program.ring path does not exist.
    res = audio_runtime_ring.check_ring_geometry_coherence()
    assert res.status == "warn"
    assert "no valid ring header" in res.detail.lower()


def test_fail_when_env_value_invalid(monkeypatch, tmp_path):
    # An out-of-range JASPER_FANIN_RING_SLOTS is a hard failure while armed.
    _stage_ring_geometry(
        monkeypatch, tmp_path, fanin_env_text="JASPER_FANIN_RING_SLOTS=99\n",
    )
    res = audio_runtime_ring.check_ring_geometry_coherence()
    assert res.status == "fail"
    assert "invalid" in res.detail.lower()


# ===========================================================================
# check_ring_platform_assets
#
# The inert-phase contract: a MISSING asset is warn (loopback still carries
# audio), an INSTALLED-but-unusable ioplug is fail. The check never touches a
# live ring — the open probe is fully mocked.
# ===========================================================================

# A minimal but COMPLETE three-block conf.d: every PCM name resolves, and none
# declares `format`/`channels`, so the ioplug's own absent-key defaults
# (2ch/S16_LE) answer all three. Needed because the probe now reads its wire off
# THIS text (`_jts_ring_probe_wire` sources `ring_conf_format`/
# `ring_conf_channels`, not the resolver) — a stub declaring only
# `jts_ring_capture` left `jts_ring_playback` indeterminate.
_VALID_RING_CONF = (
    "pcm.jts_ring_capture {\n"
    "    type jts_ring\n"
    '    path "/dev/shm/jts-ring/program.ring"\n'
    "    period_frames 128\n"
    "    n_slots 2\n"
    "}\n"
    "\n"
    "pcm.jts_ring_playback {\n"
    "    type jts_ring\n"
    '    path "/dev/shm/jts-ring/content.ring"\n'
    "    period_frames 128\n"
    "    n_slots 2\n"
    "}\n"
    "\n"
    # The ACTIVE ring block. The probe walks every PCM in _JTS_RING_PCMS, so a
    # stub missing this one leaves it indeterminate exactly as a stub missing
    # jts_ring_playback used to.
    "pcm.jts_ring_active_playback {\n"
    "    type jts_ring\n"
    '    path "/dev/shm/jts-ring/active-content.ring"\n'
    "    period_frames 128\n"
    "    n_slots 2\n"
    "}\n"
)


def _stage_ring_conf(monkeypatch, tmp_path, text=_VALID_RING_CONF):
    """Point `_JTS_RING_CONF_D` at a tmp conf.d declaring every PCM block.

    Standalone helper for the probe-mechanics tests below, which don't stage
    the other P1 assets via `_stage_assets` but still need a readable conf.d
    now that the probe's wire lookup reads one.
    """
    conf = tmp_path / "60-jts-ring.conf"
    conf.write_text(text, encoding="utf-8")
    monkeypatch.setattr(audio_runtime_ring, "_JTS_RING_CONF_D", str(conf))
    return conf


def _stage_assets(monkeypatch, tmp_path, *, so=True, conf=True, shm=True):
    """Point the module constants at tmp paths and create/omit each asset."""
    plugin_dir = tmp_path / "alsa-lib"
    plugin_dir.mkdir()
    so_path = plugin_dir / audio_runtime_ring._JTS_RING_IOPLUG_SO
    if so:
        so_path.write_bytes(b"\x7fELF fake so")
    conf_path = tmp_path / "60-jts-ring.conf"
    if conf:
        conf_path.write_text(_VALID_RING_CONF, encoding="utf-8")
    shm_dir = tmp_path / "jts-ring"
    if shm:
        shm_dir.mkdir()

    monkeypatch.setattr(
        audio_runtime_ring, "_JTS_RING_ALSA_PLUGIN_DIR", str(plugin_dir))
    monkeypatch.setattr(audio_runtime_ring, "_JTS_RING_CONF_D", str(conf_path))
    monkeypatch.setattr(
        audio_runtime_ring, "_JTS_RING_SHM_DIR", str(shm_dir))


# --- check_ring_platform_assets ---------------------------------------
#
# Presence is always load-bearing since ADR-0100: the ring is the only
# transport, so a missing asset is a hard failure rather than a "loopback still
# carries audio" warn. The open-probe rides along ONLY where it cannot disturb
# anything — fan-in inactive AND the ring file absent — because a present but
# unloadable ioplug (the -DPIC / arch-mismatch class) passes presence.


def _probe_must_not_run(monkeypatch):
    """Fail loudly if the check open-probes. It must never touch a live ring."""

    def _boom(pcm, tool):  # pragma: no cover - must never be called
        raise AssertionError("check_ring_platform_assets must not open-probe")

    monkeypatch.setattr(audio_runtime_ring, "_jts_ring_pcm_resolves", _boom)


def _fanin_unit_state(monkeypatch, state: str):
    """Answer `systemctl is-active jasper-fanin.service` with ``state``."""
    monkeypatch.setattr(
        audio_runtime_ring,
        "_run",
        lambda cmd, timeout=5.0: SimpleNamespace(
            returncode=0, stdout=f"{state}\n", stderr=""
        ),
    )


def test_ok_when_all_assets_present(monkeypatch, tmp_path):
    _stage_assets(monkeypatch, tmp_path)
    _fanin_unit_state(monkeypatch, "active")
    _probe_must_not_run(monkeypatch)
    res = audio_runtime_ring.check_ring_platform_assets()
    assert res.status == "ok"


def test_a_live_fanin_is_never_open_probed(monkeypatch, tmp_path):
    """The gate is fan-in's systemd state, not a persisted token.

    Its predecessor derived "is the ring armed?" from
    JASPER_FANIN_CAMILLA_COUPLING, which probed a LIVE ring on every box whose
    key had not been written yet — coupling-auto runs
    After=jasper-fanin.service, so that is every fresh boot.
    """
    _stage_assets(monkeypatch, tmp_path)
    _probe_that_creates_the_ring(monkeypatch, tmp_path)
    _fanin_unit_state(monkeypatch, "active")

    assert audio_runtime_ring._jts_ring_probeable_pcms() == []


def test_a_ring_file_already_on_disk_is_never_open_probed(monkeypatch, tmp_path):
    """An existing ring may still be attached; an absent one cannot be.

    That is what makes "never probe a ring in use" a property of this gate
    rather than a hope about the ioplug's EBUSY.
    """
    _stage_assets(monkeypatch, tmp_path)
    rings = _probe_that_creates_the_ring(monkeypatch, tmp_path)
    _fanin_unit_state(monkeypatch, "inactive")
    rings["jts_ring_capture"].write_bytes(b"live-armed-ring-magic")

    probeable = dict(audio_runtime_ring._jts_ring_probeable_pcms())

    assert "jts_ring_capture" not in probeable
    assert "jts_ring_playback" in probeable


def test_an_ioplug_alsa_cannot_open_is_a_hard_fail(monkeypatch, tmp_path):
    """The -DPIC / arch-mismatch class: present on disk, unloadable by ALSA.

    Presence cannot separate it from a healthy plugin, so without the probe the
    box reports a green ring platform and carries no audio.

    The second assertion is a PASS-THROUGH pin, not a prose pin: the sentinel is
    the test's own, and what is being pinned is that the probe's reason reaches
    the operator rather than being swallowed into "probe failed".
    """
    probe_reason = "probe-reason-sentinel"
    _stage_assets(monkeypatch, tmp_path)
    _fanin_unit_state(monkeypatch, "inactive")
    monkeypatch.setattr(
        audio_runtime_ring,
        "_jts_ring_pcm_resolves",
        lambda pcm, tool: (False, probe_reason),
    )

    res = audio_runtime_ring.check_ring_platform_assets()

    assert res.status == "fail"
    assert probe_reason in res.detail


@pytest.mark.parametrize(
    "staged, expected_fragments",
    [
        ({"so": False}, ["ioplug .so absent", "redeploy"]),
        ({"conf": False}, ["conf.d absent", "redeploy"]),
        ({"shm": False}, ["absent", "redeploy"]),
        (
            {"so": False, "conf": False, "shm": False},
            ["ioplug .so absent", "conf.d absent", "redeploy"],
        ),
    ],
)
def test_any_missing_asset_is_a_hard_fail(
    monkeypatch, tmp_path, staged, expected_fragments
):
    """A missing asset FAILs whatever the persisted file says, and says redeploy.

    There is no second transport to degrade onto: the graph cannot resolve its
    ring devices at all, so there is no "loopback still carries audio" warn to
    fall back to. The verdict must not depend on read_persisted_coupling, so
    this never stubs it, and it must never open-probe a live ring.
    """
    _stage_assets(monkeypatch, tmp_path, **staged)
    _probe_must_not_run(monkeypatch)
    res = audio_runtime_ring.check_ring_platform_assets()
    assert res.status == "fail"
    for fragment in expected_fragments:
        assert fragment in res.detail.lower() or fragment in res.detail


# --- _jts_ring_pcm_resolves (the open-probe helper) -------------------


def test_probe_ok_on_zero_exit(monkeypatch, tmp_path):
    _stage_ring_conf(monkeypatch, tmp_path)
    monkeypatch.setattr(audio_runtime_ring.shutil, "which", lambda t: f"/usr/bin/{t}")
    monkeypatch.setattr(
        audio_runtime_ring, "_run",
        lambda cmd, timeout=5.0: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    ok, detail = audio_runtime_ring._jts_ring_pcm_resolves("jts_ring_capture", "arecord")
    assert ok is True and detail == "resolved"


def test_probe_reports_stderr_on_nonzero_exit(monkeypatch, tmp_path):
    _stage_ring_conf(monkeypatch, tmp_path)
    monkeypatch.setattr(audio_runtime_ring.shutil, "which", lambda t: f"/usr/bin/{t}")
    monkeypatch.setattr(
        audio_runtime_ring, "_run",
        lambda cmd, timeout=5.0: SimpleNamespace(
            returncode=1, stdout="", stderr="ALSA lib: Unknown PCM jts_ring_playback"
        ),
    )
    ok, detail = audio_runtime_ring._jts_ring_pcm_resolves("jts_ring_playback", "aplay")
    assert ok is False
    assert "Unknown PCM" in detail


def test_probe_fails_closed_when_tool_missing(monkeypatch):
    monkeypatch.setattr(audio_runtime_ring.shutil, "which", lambda t: None)
    ok, detail = audio_runtime_ring._jts_ring_pcm_resolves("jts_ring_capture", "arecord")
    assert ok is False
    assert "not found" in detail


def test_probe_fails_closed_when_conf_wire_is_indeterminate(monkeypatch, tmp_path):
    # No conf.d staged at all: `_JTS_RING_CONF_D` still points at its
    # production default, which is unreadable in the test environment. The
    # probe must refuse with a crisp reason rather than pass `None` to ALSA.
    monkeypatch.setattr(audio_runtime_ring.shutil, "which", lambda t: f"/usr/bin/{t}")
    monkeypatch.setattr(audio_runtime_ring, "_JTS_RING_CONF_D", str(tmp_path / "missing.conf"))

    def _must_not_be_called(cmd, timeout=5.0):  # pragma: no cover - must never run
        raise AssertionError("probe ran with an indeterminate wire")

    monkeypatch.setattr(
        audio_runtime_ring, "_run", _must_not_be_called)
    ok, detail = audio_runtime_ring._jts_ring_pcm_resolves("jts_ring_capture", "arecord")
    assert ok is False
    assert "indeterminate" in detail


def test_probe_reports_hang_on_timeout(monkeypatch, tmp_path):
    _stage_ring_conf(monkeypatch, tmp_path)
    monkeypatch.setattr(audio_runtime_ring.shutil, "which", lambda t: f"/usr/bin/{t}")

    def _timeout(cmd, timeout=5.0):
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(
        audio_runtime_ring, "_run", _timeout)
    ok, detail = audio_runtime_ring._jts_ring_pcm_resolves("jts_ring_playback", "aplay")
    assert ok is False
    assert "hung" in detail


def test_probe_uses_devnull_for_capture_and_devzero_for_playback(monkeypatch, tmp_path):
    _stage_ring_conf(monkeypatch, tmp_path)
    monkeypatch.setattr(audio_runtime_ring.shutil, "which", lambda t: f"/usr/bin/{t}")
    seen = {}

    def _capture_cmd(cmd, timeout=5.0):
        seen["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        audio_runtime_ring, "_run", _capture_cmd)

    audio_runtime_ring._jts_ring_pcm_resolves("jts_ring_capture", "arecord")
    assert seen["cmd"][0] == "arecord"
    assert seen["cmd"][-1] == "/dev/null"

    audio_runtime_ring._jts_ring_pcm_resolves("jts_ring_playback", "aplay")
    assert seen["cmd"][0] == "aplay"
    assert seen["cmd"][-1] == "/dev/zero"


# --- residue cleanup (Finding 1: probe must not create a ring file) ---


def _probe_that_creates_the_ring(monkeypatch, tmp_path):
    """Repoint the SHM dir at tmp and make the mocked probe CREATE the ring
    file the ioplug's create-or-attach open would (O_CREAT|O_EXCL). Returns
    the ring Paths keyed by PCM name — one per block in _JTS_RING_PCMS."""
    shm_dir = tmp_path / "jts-ring"
    shm_dir.mkdir(exist_ok=True)  # _stage_assets may have created it already
    monkeypatch.setattr(
        audio_runtime_ring, "_JTS_RING_SHM_DIR", str(shm_dir))
    # Re-stage the conf.d even when _stage_assets already did (same content,
    # so this is a no-op then): the probe now reads its wire off the conf.d
    # text, and the standalone callers below never call _stage_assets at all.
    _stage_ring_conf(monkeypatch, tmp_path)
    monkeypatch.setattr(audio_runtime_ring.shutil, "which", lambda t: f"/usr/bin/{t}")

    def _run_creates(cmd, timeout=5.0):
        # cmd == [tool, "-D", pcm, ...]; emulate the ioplug's O_CREAT|O_EXCL
        # open: create the ring only when absent, never truncate a file that
        # is already there (a live ring the real ioplug would attach to).
        pcm = cmd[2]
        ring = audio_runtime_ring._jts_ring_path_for(pcm)
        if ring is not None and not os.path.exists(ring):
            open(ring, "wb").close()
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        audio_runtime_ring, "_run", _run_creates)
    # Derived from the module's own PCM table so a new ring cannot leave this
    # helper silently covering a subset of what the probe actually opens.
    return {
        pcm: shm_dir / basename for pcm, _tool, basename in audio_runtime_ring._JTS_RING_PCMS
    }


def test_probe_unlinks_a_ring_it_created(monkeypatch, tmp_path):
    rings = _probe_that_creates_the_ring(monkeypatch, tmp_path)
    ok, detail = audio_runtime_ring._jts_ring_pcm_resolves("jts_ring_capture", "arecord")
    assert ok is True and detail == "resolved"
    # The probe created program.ring; residue cleanup must have removed it.
    assert not rings["jts_ring_capture"].exists(), (
        "probe left a ring file behind — violates P1 inertness"
    )


def test_full_check_leaves_no_ring_files(monkeypatch, tmp_path):
    # End-to-end: all assets present, the (mocked) open probe creates EVERY ring
    # file it opens, and after the check NONE of them exists on disk. Walking
    # the probe's own results rather than naming two files is what keeps this an
    # inertness proof for the whole set — including the ACTIVE ring, whose
    # accidental creation would poison the first real arm exactly as Ring A's
    # would.
    #
    # THREE is written out, not derived from the table under test. Comparing the
    # probe's coverage against `len(_JTS_RING_PCMS)` made the assertion
    # self-referential: deleting a ring from that table shrinks both sides
    # together and the test stays green while a shipped PCM goes unprobed. The
    # literal is the claim — a fourth ring must come here and say so.
    _stage_assets(monkeypatch, tmp_path)
    rings = _probe_that_creates_the_ring(monkeypatch, tmp_path)
    res = audio_runtime_ring.check_ring_platform_assets()
    assert res.status == "ok", res.detail
    assert len(rings) == 3, (
        f"the ring conf.d ships three PCMs; the probe covered {sorted(rings)}"
    )
    for pcm, ring in rings.items():
        assert not ring.exists(), f"{pcm} left {ring} behind — violates inertness"


def test_probe_preserves_a_preexisting_live_ring(monkeypatch, tmp_path):
    # A live armed ring pre-exists. The probe (which here would EBUSY on real
    # hardware, but we mock a benign run) must NOT unlink it — only files the
    # probe itself created are removed.
    rings = _probe_that_creates_the_ring(monkeypatch, tmp_path)
    live = rings["jts_ring_capture"]
    live.write_bytes(b"live-armed-ring-magic")
    audio_runtime_ring._jts_ring_pcm_resolves("jts_ring_capture", "arecord")
    assert live.exists(), "residue cleanup removed a pre-existing (live) ring"
    assert live.read_bytes() == b"live-armed-ring-magic"


def test_probe_unlinks_even_when_open_fails(monkeypatch, tmp_path):
    # An ioplug can create the ring FILE and then fail the open (nonzero exit).
    # The residue cleanup runs in a finally, so a failing probe still leaves
    # no ring file behind.
    shm_dir = tmp_path / "jts-ring"
    shm_dir.mkdir()
    monkeypatch.setattr(
        audio_runtime_ring, "_JTS_RING_SHM_DIR", str(shm_dir))
    _stage_ring_conf(monkeypatch, tmp_path)
    monkeypatch.setattr(audio_runtime_ring.shutil, "which", lambda t: f"/usr/bin/{t}")
    ring = shm_dir / "program.ring"

    def _run_creates_then_fails(cmd, timeout=5.0):
        ring.write_bytes(b"half-baked")
        return SimpleNamespace(returncode=1, stdout="", stderr="some open error")

    monkeypatch.setattr(
        audio_runtime_ring, "_run", _run_creates_then_fails)
    ok, _ = audio_runtime_ring._jts_ring_pcm_resolves("jts_ring_capture", "arecord")
    assert ok is False
    assert not ring.exists(), "residue left behind after a failed probe"


# --- The verdict is independent of the persisted token ----------------


def _arm_ring(monkeypatch):
    monkeypatch.setattr(
        "jasper.fanin.ring_health.read_persisted_coupling",
        lambda *a, **k: "shm_ring",
    )


def test_a_missing_asset_fails_whatever_the_persisted_token_says(
    monkeypatch, tmp_path
):
    """The verdict does NOT turn on the coupling any more, and that is the fix.

    It used to: an armed box FAILED and an unarmed one merely WARNED "inert
    platform incomplete (loopback still active)". ADR-0100 retired that route,
    so on a box whose persisted token had not been rewritten yet the warn
    claimed a transport the box does not have — a speaker emitting nothing,
    reported as degraded-but-playing, which is exactly the reported-as-healthy
    case the transport parks exist to prevent.

    Both polarities are driven, because one alone would also pass against a
    check that still branched and happened to be tested on the branch that
    matches.
    """
    _stage_assets(monkeypatch, tmp_path, so=False)

    def _verdict(label):
        res = audio_runtime_ring.check_ring_platform_assets()
        assert res.status == "fail", label
        assert "missing" in res.detail.lower(), label
        # The retired claim is asserted ABSENT: it is worse than silence.
        assert "loopback" not in res.detail.lower(), label
        assert "inert" not in res.detail.lower(), label

    _verdict("persisted token not rewritten yet")
    _arm_ring(monkeypatch)
    _verdict("persisted token names the ring")


# --- The open-probe asks for what the CONF.D DECLARES, never the resolver ----


def test_probe_sources_the_conf_declared_wire_not_the_resolver(monkeypatch, tmp_path):
    """The ioplug advertises EXACTLY the conf-declared format/channels as its
    hardware constraint, so the probe must read `ring_conf_format` /
    `ring_conf_channels` off THIS box's conf.d — never `resolve_ring_wire`.
    The two answer different questions ("what does the file say" vs. "what
    SHOULD this box declare") that are independently gated: conf rendering and
    ring-coupling arm are separate gates, so a box can carry a per-box-rendered
    Ring B conf.d while still sitting coupling-inert. Stage a conf.d rendered
    wide (S32_LE, Ring B 6ch) and make the resolver explode if touched; the
    probe argv must still follow the conf.d, per PCM."""
    conf = tmp_path / "60-jts-ring.conf"
    conf.write_text(
        "pcm.jts_ring_capture {\n"
        "    type jts_ring\n"
        '    path "/dev/shm/jts-ring/program.ring"\n'
        "    period_frames 128\n"
        "    n_slots 2\n"
        "    format S32_LE\n"
        "}\n"
        "\n"
        "pcm.jts_ring_playback {\n"
        "    type jts_ring\n"
        '    path "/dev/shm/jts-ring/content.ring"\n'
        "    period_frames 128\n"
        "    n_slots 2\n"
        "    format S32_LE\n"
        "    channels 6\n"
        "}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(audio_runtime_ring, "_JTS_RING_CONF_D", str(conf))
    monkeypatch.setattr(audio_runtime_ring.shutil, "which", lambda t: f"/usr/bin/{t}")

    import jasper.fanin_coupling as fc

    def _must_not_be_called(*a, **k):  # pragma: no cover - must never run
        raise AssertionError(
            "the probe must never consult resolve_ring_wire — its answer is "
            "independently gated from what the conf.d actually declares"
        )

    monkeypatch.setattr(fc, "resolve_ring_wire", _must_not_be_called)

    seen = {}

    def _capture_cmd(cmd, timeout=5.0):
        seen[cmd[2]] = cmd
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        audio_runtime_ring, "_run", _capture_cmd)

    audio_runtime_ring._jts_ring_pcm_resolves("jts_ring_capture", "arecord")
    audio_runtime_ring._jts_ring_pcm_resolves("jts_ring_playback", "aplay")

    ring_a = seen["jts_ring_capture"]
    ring_b = seen["jts_ring_playback"]
    assert ring_a[ring_a.index("-f") + 1] == "S32_LE"
    assert ring_b[ring_b.index("-f") + 1] == "S32_LE"
    # Ring A declares no `channels` -> the ioplug default (2); Ring B's
    # explicit `channels 6` line must be honored.
    assert ring_a[ring_a.index("-c") + 1] == "2"
    assert ring_b[ring_b.index("-c") + 1] == "6"


def test_probe_asks_for_the_shipped_wire_today(monkeypatch, tmp_path):
    """On the REAL shipped (never-rendered) conf.d, the probe asks for 2
    channels / S32_LE.

    THE FORMAT AXIS NO LONGER REACHES THIS VIA DORMANCY. Every block now
    DECLARES ``format S32_LE`` EXPLICITLY (see
    ``deploy/alsa/conf.d/60-jts-ring.conf``'s own "WIRE FORMAT" header comment)
    — the probe reads a LITERAL in the file, not the ioplug's absent-key
    default. The shipped file changed to spell the token because the
    resolver's default went wide while the C ioplug's compiled-in default
    (mirrored by ``jasper.ring_assets.RING_CONF_DEFAULT_FORMAT``) stayed
    S16_LE: an omitted ``format`` key would now declare the OPPOSITE of what
    every other end of the ring resolves. That same disagreement is what makes
    the ioplug capability gate LIVE fleet-wide now (``ring_wire_caps_ready`` /
    ``ring_ioplug_wire_supported``) rather than dormant — see
    :data:`~jasper.ring_assets.RING_CONF_DEFAULT_FORMAT`'s own docstring.

    THE CHANNELS AXIS IS UNCHANGED: no block declares ``channels``, so it
    still answers via the ioplug's absent-key default (2), not a literal and
    not the resolver.

    Cross-checked against ``resolve_ring_wire``'s answer for the shipped
    topology, which still coincides today — both land on S32_LE/2ch/2ch now —
    a drift between the two independent policies would show up here as a
    failing cross-check, not as the probe's own source."""
    from jasper.fanin_coupling import resolve_ring_wire

    shipped = (
        Path(__file__).resolve().parents[1]
        / "deploy" / "alsa" / "conf.d" / "60-jts-ring.conf"
    )
    conf = tmp_path / "60-jts-ring.conf"
    conf.write_bytes(shipped.read_bytes())
    monkeypatch.setattr(audio_runtime_ring, "_JTS_RING_CONF_D", str(conf))

    wire = resolve_ring_wire()
    for pcm in ("jts_ring_capture", "jts_ring_playback"):
        channels, sample_format = audio_runtime_ring._jts_ring_probe_wire(pcm)
        assert channels == 2
        assert sample_format == "S32_LE"
    assert (wire.sample_format, wire.ring_a_channels, wire.ring_b_channels) == (
        "S32_LE",
        2,
        2,
    )


# --- the ring writer-lock exclusivity guard (audio-graph consolidation #2285,
# --- P9-C). The C ioplug records the residual this closes verbatim: an flock's
# --- identity is the PATHNAME, not the inode, so unlinking `<ring>.writer.lock`
# --- while a writer holds it voids exclusivity SILENTLY and two live writers
# --- proceed with no log line between them. The guard reads the KERNEL's view
# --- (who holds an fd on the lock) because the shared header structurally
# --- cannot answer: `writer_pid` is a single slot a second attach overwrites.
#
# --- These build a synthetic /proc so the shapes are deterministic and the
# --- tests run on any host (macOS has no /proc at all). The same shapes were
# --- observed end-to-end against real processes on a real Linux box; the PR
# --- body carries that transcript.

_SHM = "/dev/shm/jts-ring"
_LOCK = f"{_SHM}/active-content.ring.writer.lock"


def _fake_proc(tmp_path, holders, *, unreadable_pids=()):
    """Build a /proc-shaped tree.

    `holders` maps pid -> list of fd targets (a target ending in " (deleted)"
    reproduces what the kernel shows for an fd on an unlinked file).
    """
    root = tmp_path / "proc"
    root.mkdir(parents=True)
    (root / "self").mkdir()  # a non-numeric entry, must be skipped
    for pid, targets in holders.items():
        fd_dir = root / str(pid) / "fd"
        fd_dir.mkdir(parents=True)
        for i, target in enumerate(targets):
            os.symlink(target, fd_dir / str(i))
    for pid in unreadable_pids:
        fd_dir = root / str(pid) / "fd"
        fd_dir.mkdir(parents=True)
        fd_dir.chmod(0o000)
    return root


def _run_guard(monkeypatch, root):
    monkeypatch.setattr(
        audio_runtime_ring, "_PROC_ROOT", str(root))
    monkeypatch.setattr(
        audio_runtime_ring, "_WRITER_LOCK_CONFIRM_DELAY_SEC", 0.0)
    return audio_runtime_ring.check_ring_writer_lock_exclusivity()


def test_writer_lock_guard_ok_with_one_writer(monkeypatch, tmp_path):
    """NEGATIVE CONTROL: the normal armed box — one C writer holding one ring's
    writer lock, plus the Rust reader's mapping of the ring FILE itself — is
    `ok`. (Scanning maps for the ring file instead would call this a defect:
    the reader mmaps the same file by design.)"""
    root = _fake_proc(
        tmp_path,
        {
            41: [_LOCK, "/dev/null", f"{_SHM}/active-content.ring"],
            42: [f"{_SHM}/active-content.ring", "/dev/snd/pcmC0D0p"],
        },
    )

    result = _run_guard(monkeypatch, root)

    assert result.status == "ok"
    assert "no ring has more than one live writer" in result.detail


def test_writer_lock_guard_ok_when_nothing_holds_a_lock(monkeypatch, tmp_path):
    """An unarmed box holds no writer lock at all — `ok`, not a false alarm."""
    root = _fake_proc(tmp_path, {41: ["/dev/null"], 42: [f"{_SHM}/program.ring"]})

    result = _run_guard(monkeypatch, root)

    assert result.status == "ok"


def test_writer_lock_guard_fails_on_two_live_writers(monkeypatch, tmp_path):
    """POSITIVE CONTROL: the recorded residual. One incumbent holding the
    UNLINKED inode plus a fresh writer on a re-created file at the same
    pathname — two live writers, no log line between them — is `fail`, and the
    detail names both pids and which one is orphaned."""
    root = _fake_proc(
        tmp_path,
        {
            41: [f"{_LOCK} (deleted)"],
            42: [_LOCK],
        },
    )

    result = _run_guard(monkeypatch, root)

    assert result.status == "fail"
    assert "TWO LIVE WRITERS" in result.detail
    assert "pid 41 (lock file unlinked)" in result.detail
    assert "pid 42" in result.detail
    assert _LOCK in result.detail


def test_writer_lock_guard_fails_on_two_writers_without_an_unlink(
    monkeypatch, tmp_path
):
    """Two live holders of the SAME inode is equally a broken SPSC contract,
    so the guard keys on holder COUNT, not on the deleted marker alone."""
    root = _fake_proc(tmp_path, {41: [_LOCK], 42: [_LOCK]})

    result = _run_guard(monkeypatch, root)

    assert result.status == "fail"


def test_writer_lock_guard_ignores_a_contender_that_gave_up(monkeypatch, tmp_path):
    """`acquire_writer_lock` OPENS the lock file and only THEN spins on flock
    for up to JTS_RING_OPEN_LOCK_WAIT_TIMEOUT_MS, so a healthy box legitimately
    shows two fd holders for up to that long. Only pids present in BOTH samples
    count: a contender that has gone by the confirm sample is not the defect."""
    first = _fake_proc(tmp_path, {41: [_LOCK], 42: [_LOCK]})
    second = _fake_proc(tmp_path / "after", {41: [_LOCK]})
    seen = []

    real = audio_runtime_ring._ring_writer_lock_holders

    def sampling(**kwargs):
        seen.append(len(seen))
        root = first if len(seen) == 1 else second
        return real(proc_root=str(root), shm_dir=_SHM)

    monkeypatch.setattr(
        audio_runtime_ring, "_ring_writer_lock_holders", sampling)
    monkeypatch.setattr(
        audio_runtime_ring, "_PROC_ROOT", str(first))
    monkeypatch.setattr(
        audio_runtime_ring, "_WRITER_LOCK_CONFIRM_DELAY_SEC", 0.0)

    result = audio_runtime_ring.check_ring_writer_lock_exclusivity()

    assert len(seen) == 2, "a suspected two-writer read must be CONFIRMED"
    assert result.status == "ok"


def test_writer_lock_guard_warns_on_a_lone_orphaned_holder(monkeypatch, tmp_path):
    """One writer whose lock file was unlinked out from under it: exclusivity
    is ALREADY void (the next opener creates a fresh inode and is not
    excluded), so warn before the second writer arrives."""
    root = _fake_proc(tmp_path, {41: [f"{_LOCK} (deleted)"]})

    result = _run_guard(monkeypatch, root)

    assert result.status == "warn"
    assert "UNLINKED" in result.detail
    assert "pid 41" in result.detail


def test_writer_lock_guard_warns_when_proc_is_partially_unreadable(
    monkeypatch, tmp_path
):
    """A non-root sweep cannot read other users' /proc/<pid>/fd. That is a
    BLIND SPOT, so the guard says so rather than reporting a clean bill."""
    if os.geteuid() == 0:
        pytest.skip("root can read every /proc/<pid>/fd")
    root = _fake_proc(tmp_path, {41: ["/dev/null"]}, unreadable_pids=(77,))
    try:
        result = _run_guard(monkeypatch, root)
    finally:
        (root / "77" / "fd").chmod(0o755)

    assert result.status == "warn"
    assert "partially blind" in result.detail


def test_writer_lock_guard_ignores_locks_outside_the_ring_dir(monkeypatch, tmp_path):
    """Scoped to the ring tmpfs, and to the WRITER lock: some other subsystem's
    `.writer.lock`, and the ring's own `.open.lock` (a transaction lock BOTH
    C and Rust take), are none of this guard's business."""
    root = _fake_proc(
        tmp_path,
        {
            41: ["/var/lib/other/thing.writer.lock"],
            42: ["/var/lib/other/thing.writer.lock"],
            43: [f"{_SHM}/active-content.ring.open.lock"],
            44: [f"{_SHM}/active-content.ring.open.lock"],
        },
    )

    result = _run_guard(monkeypatch, root)

    assert result.status == "ok"


def test_writer_lock_guard_counts_one_pid_once(monkeypatch, tmp_path):
    """A single writer with several fds on one lock is still ONE writer."""
    root = _fake_proc(tmp_path, {41: [_LOCK, _LOCK, f"{_LOCK} (deleted)"]})

    result = _run_guard(monkeypatch, root)

    # Not a fail (one pid), but the unlinked fd still earns the warn.
    assert result.status == "warn"


# ===========================================================================
# check_ring_split_transport (#2285 P2, design §10.3)
#
# The state under test: the loaded CamillaDSP graph and outputd's content
# bridge disagree about the ring, in either direction. Nothing carries the
# program across the post-DSP hop, so the speaker is silent while every daemon
# is healthy. Each conjunct is pinned separately: a two-term conjunction passes
# a single-conjunct test while still being wrong.
# ===========================================================================

RING_BRIDGE = "shm_ring"
DIRECT_BRIDGE = "direct"


def _write_pair(tmp_path, name: str, playback_device: str | None):
    """Write a real statefile + the CamillaDSP config it points at.

    Returns the statefile path. ``playback_device=None`` writes a config with no
    ``playback`` device key at all — the program-bake / parked shape an active
    leader really keeps in its primary statefile.
    """
    config = tmp_path / f"{name}-config.yml"
    playback = (
        f"  playback:\n    type: Alsa\n    device: {playback_device}\n"
        if playback_device
        else "  playback:\n    type: File\n    filename: /dev/null\n"
    )
    config.write_text(
        "devices:\n"
        "  samplerate: 48000\n"
        "  chunksize: 1024\n"
        "  capture:\n    type: Alsa\n    device: jts_ring_capture\n"
        + playback,
        encoding="utf-8",
    )
    statefile = tmp_path / f"{name}-statefile.yml"
    statefile.write_text(f"config_path: {config}\n", encoding="utf-8")
    return statefile


def _arrange(
    monkeypatch,
    tmp_path,
    *,
    bridge: str,
    playback_device: str | None,
    crossover_playback_device: str | None = None,
    primary_config_missing: bool = False,
    grouped_park: bool = False,
    marker_armed: bool = False,
) -> None:
    """Put the box in one (content-bridge, loaded-graph-playback) combination.

    REAL STATEFILES ON DISK, not stubs, and that is the point of this harness.
    The check resolves its evidence through BOTH the primary and the camilla#2
    crossover statefile (first recognized endpoint wins). Stubbing the resolved
    device would let the two halves of that union drift apart without any test
    noticing — which is exactly how the pre-#2285 gap between this check and
    `check_outputd_service`'s transport note survived unseen.
    """
    outputd_env = tmp_path / "outputd.env"
    outputd_env.write_text(
        f"JASPER_OUTPUTD_CONTENT_BRIDGE={bridge}\n", encoding="utf-8"
    )
    # The env-file seam the doctor's layered reader honours for the FIRST layer;
    # the module constant is what the ring-path derivation still reads.
    monkeypatch.setenv("JASPER_OUTPUTD_ENV_FILE", str(outputd_env))
    monkeypatch.setattr(
        "jasper.audio_runtime_plan.DEFAULT_OUTPUTD_ENV_PATH", str(outputd_env)
    )
    # The SECOND env layer, exactly where a bonded member's marker really lives
    # — never in the first file. Writing it here is what proves the doctor reads
    # the merge and not just `outputd.env`.
    grouping_env = tmp_path / "grouping-outputd.env"
    grouping_env.write_text(
        "JASPER_OUTPUTD_DAC_CONTENT_LANE=1\n" if marker_armed else "",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "jasper.multiroom.reconcile.OUTPUTD_GROUPING_ENV_FILE", str(grouping_env)
    )
    monkeypatch.setattr(
        audio_runtime_ring, "_grouped_dac_content_lane_parked", lambda: grouped_park
    )
    primary = _write_pair(tmp_path, "primary", playback_device)
    if primary_config_missing:
        # The statefile parses but names a config that is gone — evidence
        # carries an error string and the primary contributes nothing.
        primary.write_text(
            f"config_path: {tmp_path / 'deleted-config.yml'}\n", encoding="utf-8"
        )
    crossover = _write_pair(tmp_path, "crossover", crossover_playback_device)
    monkeypatch.setattr(
        audio_runtime_ring, "_active_camilla_config_path", lambda: (str(primary), None)
    )
    monkeypatch.setattr(
        "jasper.audio_runtime_plan.DEFAULT_CAMILLA2_STATEFILE_PATH", str(crossover)
    )


@pytest.mark.parametrize(
    "playback_device", [RING_ACTIVE_PLAYBACK_DEVICE, RING_PLAYBACK_DEVICE]
)
def test_a_stranded_ring_fails_loudly(monkeypatch, tmp_path, playback_device) -> None:
    """EITHER post-DSP ring, written while outputd reads its ALSA lane, is SILENT.

    Parametrized over both rings because the fault is the disagreement, not
    which ring is on the graph side: a flat box stranding the stereo ring is as
    silent as a roleful box stranding the active one.
    """
    _arrange(
        monkeypatch, tmp_path, bridge=DIRECT_BRIDGE, playback_device=playback_device
    )

    result = audio_runtime_ring.check_ring_split_transport()

    assert result.status == "fail", result
    # The remedy must be the EXPLICIT ARM command (the §4.2 mode-split
    # contract), not a rollback: there is no longer a rollback direction, and
    # for a roleful box `loopback` is the park rather than a destination.
    assert "jasper-fanin-coupling-reconcile shm_ring" in result.detail
    assert "loopback" not in result.detail


def test_a_bridge_waiting_on_a_ring_nobody_writes_fails_too(
    monkeypatch, tmp_path
) -> None:
    """The other direction of the same disagreement, and just as silent."""
    _arrange(
        monkeypatch,
        tmp_path,
        bridge=RING_BRIDGE,
        playback_device="outputd_content_playback",
    )

    result = audio_runtime_ring.check_ring_split_transport()

    assert result.status == "fail", result
    assert "jasper-fanin-coupling-reconcile shm_ring" in result.detail


def test_a_bonded_follower_on_the_stereo_ring_is_not_a_split(
    monkeypatch, tmp_path
) -> None:
    """THE FALSE FAIL this carve-out exists to prevent.

    A bonded passive follower loads the stereo ring by design while its
    grouping env pins CONTENT_BRIDGE=direct, and it PLAYS — through the
    dac_content lane. The generalized predicate reads that pair as a split, and
    the arm it would prescribe is one `coupling_supported_for_route` refuses for
    exactly this box. `check_ring_transport_park` owns the shape by name, so
    this check stands down on it.

    Its CONTROL is the parametrized fail above: the identical
    (direct bridge, stereo ring) pair with no park FAILs, so what is carved out
    here is the park and not the stereo ring itself.
    """
    _arrange(
        monkeypatch,
        tmp_path,
        bridge=DIRECT_BRIDGE,
        playback_device=RING_PLAYBACK_DEVICE,
        grouped_park=True,
    )

    assert audio_runtime_ring.check_ring_split_transport().status == "ok"


def test_a_marker_armed_member_on_the_stereo_ring_is_not_a_split(
    monkeypatch, tmp_path
) -> None:
    """THE FALSE FAIL the cutover would otherwise create.

    A dumb bonded member declares NO bridge — outputd refuses the marker beside
    one — while its own CamillaDSP still loads the stereo ring nothing reads.
    That pair is `graph_on_ring=True, outputd_on_ring=False`, which the
    generalized predicate calls a split and reports as "this speaker is SILENT"
    about a speaker audibly playing the bond. The marker carves it out, and it
    is read out of the SECOND env layer, which is the only place it ever
    appears.
    """
    _arrange(
        monkeypatch,
        tmp_path,
        bridge="",
        playback_device=RING_PLAYBACK_DEVICE,
        marker_armed=True,
    )

    result = audio_runtime_ring.check_ring_split_transport()
    assert result.status == "ok", result

    # CONTROL: the identical pair with the marker cleared is still the split it
    # has always been, so what is carved out is the marker and not the shape.
    _arrange(
        monkeypatch,
        tmp_path,
        bridge=DIRECT_BRIDGE,
        playback_device=RING_PLAYBACK_DEVICE,
    )
    assert audio_runtime_ring.check_ring_split_transport().status == "fail"


def test_the_marker_stand_down_needs_a_cleared_bridge(monkeypatch, tmp_path) -> None:
    """The stand-down is for a SERVED member, not a merely armed one.

    A marker declared beside a bridge is the pair outputd refuses at startup, so
    this check must neither report the graph-vs-bridge pair coherent nor call it
    a split: `check_ring_transport_park` owns that shape by name (ADR-0220), and
    this check hands it over rather than describing a daemon that cannot run.
    """
    for playback in ("outputd_content_playback", RING_PLAYBACK_DEVICE):
        _arrange(
            monkeypatch,
            tmp_path,
            bridge=RING_BRIDGE,
            playback_device=playback,
            marker_armed=True,
        )
        result = audio_runtime_ring.check_ring_split_transport()
        assert result.status == "ok", (playback, result)
        assert "transport-park" in result.detail, (playback, result)


# --- Per-conjunct pins. A two-term conjunction passes a single-conjunct test
# --- while still being wrong, so each term gets its own negative case.


def test_conjunct_one_the_bridge_term_alone_does_not_fire(monkeypatch, tmp_path) -> None:
    """A direct bridge with a NON-ring graph is a coherent pair, not a split.

    Whether that shape is one the single transport can serve at all belongs to
    ``check_ring_transport_park``; claiming it here would red every bonded box,
    whose grouping env pins ``direct`` on purpose.
    """
    _arrange(
        monkeypatch,
        tmp_path,
        bridge=DIRECT_BRIDGE,
        playback_device="outputd_content_playback",
    )

    assert audio_runtime_ring.check_ring_split_transport().status == "ok"


def test_conjunct_two_the_graph_term_alone_does_not_fire(monkeypatch, tmp_path) -> None:
    """graph@ring with the ring bridge is a correctly ARMED box."""
    _arrange(
        monkeypatch, tmp_path, bridge=RING_BRIDGE,
        playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
    )

    assert audio_runtime_ring.check_ring_split_transport().status == "ok"


@pytest.mark.parametrize("missing", [None, ""])
def test_an_unreadable_graph_does_not_manufacture_a_fault(
    monkeypatch, tmp_path, missing
) -> None:
    """No loaded playback device is no evidence — never a FAIL on the ring side.

    Fail-closed applies to arming, not to diagnosis: inventing a stranded ring
    from an absent reading would make a fresh or non-JTS box report a silent
    speaker it does not have.
    """
    _arrange(monkeypatch, tmp_path, bridge=DIRECT_BRIDGE, playback_device=missing)

    assert audio_runtime_ring.check_ring_split_transport().status == "ok"


# --- The union of BOTH statefiles (#2285 panel finding C1). These two shapes
# --- were measured going quiet from every doctor surface: the folded-away
# --- `check_outputd_service` transport note saw them (it read both statefiles),
# --- and this check did not (it read only the primary). The de-duplication was
# --- only correct once the survivor read what the deleted branch read.


def test_a_program_bake_in_the_primary_does_not_hide_the_ring_in_camilla2(
    monkeypatch, tmp_path
) -> None:
    """An active leader keeps a bake in the primary and its endpoint in camilla#2.

    The primary's graph names no registered output endpoint at all (a File sink
    — the program-bake / parked shape), so reading only the primary answers
    "(none)" and the box reads healthy while camilla#2 writes the ACTIVE ring
    with outputd on its ALSA lane: silent, with every daemon green.
    """
    _arrange(
        monkeypatch,
        tmp_path,
        bridge=DIRECT_BRIDGE,
        playback_device=None,
        crossover_playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
    )

    result = audio_runtime_ring.check_ring_split_transport()

    assert result.status == "fail", result
    assert "jasper-fanin-coupling-reconcile shm_ring" in result.detail


def test_a_primary_statefile_pointing_at_a_deleted_config_does_not_hide_the_split(
    monkeypatch, tmp_path
) -> None:
    """The primary parses but its config is gone — evidence errors, not absence.

    The reader records "devices unavailable" for the primary and falls through
    to camilla#2, which names the ACTIVE ring. Before the union this returned
    the hardcoded `sound_current.yml` rescue's answer and went quiet.
    """
    _arrange(
        monkeypatch,
        tmp_path,
        bridge=DIRECT_BRIDGE,
        playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
        crossover_playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
        primary_config_missing=True,
    )

    assert audio_runtime_ring.check_ring_split_transport().status == "fail"


def test_camilla2_evidence_still_respects_the_bridge_term(
    monkeypatch, tmp_path
) -> None:
    """The union widens the GRAPH term only — the conjunction still holds.

    Without this, widening the reader could have turned the check into a
    one-term alarm that reds every correctly-armed box.
    """
    _arrange(
        monkeypatch,
        tmp_path,
        bridge=RING_BRIDGE,
        playback_device=None,
        crossover_playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
    )

    assert audio_runtime_ring.check_ring_split_transport().status == "ok"


# ---------------------------------------------------------------------------
# The ENDPOINT rung — check_active_ring_path_projection.
#
# The sibling above owns every state where the graph and the bridge disagree
# about the ring. These pin the other side of that partition: with outputd ON
# the ring bridge, the ring PATH lagging its endpoint MARKER.
# ---------------------------------------------------------------------------


def _arrange_projection(monkeypatch, tmp_path, *, env_lines: str):
    """Put outputd.env on disk for the projection check.

    A real file, not a stubbed mapping: the check reads persisted evidence
    precisely BECAUSE outputd is not running in its target state, so what is
    under test includes the read itself. Nothing else is arranged — the bridge
    that gates this check lives in this same file.
    """
    env = tmp_path / "outputd.env"
    env.write_text(env_lines, encoding="utf-8")
    monkeypatch.setenv("JASPER_OUTPUTD_ENV_FILE", str(env))
    monkeypatch.setattr(
        "jasper.audio_runtime_plan.DEFAULT_OUTPUTD_ENV_PATH", str(env)
    )


@pytest.mark.parametrize(
    "marker,carried,derived",
    [
        # The first-arm lag (jts.local, 2026-08-21): marker armed, path Ring B.
        ("1", "/dev/shm/jts-ring/content.ring", "/dev/shm/jts-ring/active-content.ring"),
        # The disarm lag: marker cleared, path still the active ring.
        ("", "/dev/shm/jts-ring/active-content.ring", "/dev/shm/jts-ring/content.ring"),
    ],
)
def test_a_ring_path_lagging_its_marker_fails_with_the_runnable_remedy(
    monkeypatch, tmp_path, marker, carried, derived
) -> None:
    """The waypoint must produce a doctor line, and it must be actionable.

    This is the state ``check_outputd_service`` structurally cannot report:
    outputd refuses the crossed pair at startup, so that check returns its
    systemd failure before ever reaching the transport comparison. Nothing owned
    the finding, and the operator got "the unit is not running" with no cause.
    """
    _arrange_projection(
        monkeypatch,
        tmp_path,
        env_lines=(
            "JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring\n"
            f"JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT={marker}\n"
            f"JASPER_OUTPUTD_SHM_RING_PATH={carried}\n"
        ),
    )

    result = audio_runtime_ring.check_active_ring_path_projection()

    assert result.status == "fail", result
    assert carried in result.detail
    assert derived in result.detail
    # Runnable, and the same pass the ladder's own next step runs.
    assert "jasper-fanin-coupling-reconcile shm_ring" in result.detail


def test_a_converged_ring_pair_is_ok(monkeypatch, tmp_path) -> None:
    """The negative control: an armed box on the active ring must not red."""
    _arrange_projection(
        monkeypatch,
        tmp_path,
        env_lines=(
            "JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring\n"
            "JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT=1\n"
            "JASPER_OUTPUTD_SHM_RING_PATH=/dev/shm/jts-ring/active-content.ring\n"
        ),
    )

    assert audio_runtime_ring.check_active_ring_path_projection().status == "ok"


def test_the_two_ladder_checks_partition_the_bridge(monkeypatch, tmp_path) -> None:
    """Neither rung is unowned, and neither is double-reported.

    The projection check returns ok as soon as outputd is off the ring bridge;
    the split check returns ok as soon as the graph agrees with whatever bridge
    is set. Pinning the handoff means a later edit cannot leave a bridge value
    that both checks ignore — the gap that let the endpoint rung go unowned in
    the first place.
    """
    _arrange_projection(
        monkeypatch,
        tmp_path,
        env_lines=(
            # STATED, because an absent key is the ring: this side of the
            # partition is now something a box has to declare its way onto.
            "JASPER_OUTPUTD_CONTENT_BRIDGE=direct\n"
            "JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT=1\n"
            "JASPER_OUTPUTD_SHM_RING_PATH=/dev/shm/jts-ring/content.ring\n"
        ),
    )
    # Off the ring bridge outputd never reads the ring path, so the projection
    # check stands down — and the split check is the one that owns anything
    # wrong on this side.
    assert audio_runtime_ring.check_active_ring_path_projection().status == "ok"

    _arrange(
        monkeypatch, tmp_path, bridge=RING_BRIDGE,
        playback_device=RING_PLAYBACK_DEVICE,
    )
    assert audio_runtime_ring.check_ring_split_transport().status == "ok"
