# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the jasper-doctor CamillaDSP-graph checks."""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from jasper import audio_runtime_plan
from jasper.camilla import CamillaUnavailable
from jasper.cli.doctor import (
    _shared,
    audio_runtime_camilla,
    audio_runtime_fanin,
    audio_runtime_outputd,
    correction,
)
from jasper.cli.doctor._evidence import evidence
from jasper.doctor_contract import summarize
from jasper.output_hardware import APPLE_USB_C_DONGLE_DEVICE_ID

from ._doctor_audio_runtime_fixtures import (
    _patch_status_reader,
    _seed_units,
)
from .doctor_test_support import _own_group

_GETCONFIG_READBACK = (
    Path(__file__).parent / "fixtures" / "camilla_readback" / "camilladsp_4.1.3_getconfig.yml"
)


def test_check_camilla_service_ok_when_enabled_and_active(monkeypatch):
    _seed_units()

    result = audio_runtime_camilla.check_camilla_service()

    assert result.status == "ok"


@pytest.mark.parametrize(
    "enabled, active, reason, silent",
    [
        # The clean-stop state (#2163): enabled, cleanly inactive, never
        # `failed`, so neither check_service_runtime_state nor
        # check_camilla_websocket names it.
        ("enabled", "inactive",
         audio_runtime_camilla.REASON_CAMILLA_INACTIVE, True),
        ("disabled", "inactive",
         audio_runtime_camilla.REASON_CAMILLA_UNIT_NOT_ENABLED, True),
        ("not-found", "inactive",
         audio_runtime_camilla.REASON_CAMILLA_UNIT_MISSING, True),
    ],
)
def test_check_camilla_service_failures(monkeypatch, enabled, active, reason, silent):
    _seed_units(enabled=enabled, active=active)

    result = audio_runtime_camilla.check_camilla_service()

    assert (result.status, result.reason, result.speaker_silent) == (
        "fail", reason, silent,
    )


def test_check_camilla_service_a_load_error_is_not_missing(monkeypatch):
    """``load_state == "error"`` pins the pre-existing verdict: it lands on
    the same ``inactive`` fail as a clean stop, never ``missing`` (#2163) —
    only ``"not-found"`` is missing."""
    evidence.seed("units", {
        "jasper-camilla.service": {
            "unit": "jasper-camilla.service",
            "load_state": "error",
            "unit_file_state": "enabled",
            "active_state": "inactive",
        },
    })

    result = audio_runtime_camilla.check_camilla_service()

    assert (result.status, result.reason, result.speaker_silent) == (
        "fail", audio_runtime_camilla.REASON_CAMILLA_INACTIVE, True,
    )


# ------------------------------------------------ CamillaDSP config dir posture
#
# Pins the jts3 2026-07-06 incident: a deploy left /var/lib/camilladsp/configs
# root-only (setgid kept, group-write stripped — mode 2755), so the non-root
# jasper-web user could not atomically write the staged active-speaker config
# and staging failed with PermissionError.


@pytest.mark.parametrize(
    "mode, group, status, reason",
    [
        (0o2775, None, "ok", ""),
        # the exact regression: group-write stripped
        (0o2755, None, "fail", audio_runtime_camilla.REASON_CAMILLA_CONFIG_DIR_NOT_WRITABLE),
        # setgid lost (2775 -> 0775): a root-run process creating a NEW
        # subdirectory later would land it group-root, not group-jasper.
        (0o0775, None, "fail", audio_runtime_camilla.REASON_CAMILLA_CONFIG_DIR_NOT_WRITABLE),
        (
            0o2775, "jts-no-such-group-xyz", "fail",
            audio_runtime_camilla.REASON_CAMILLA_CONFIG_DIR_NOT_WRITABLE,
        ),
        (None, None, "warn", audio_runtime_camilla.REASON_CAMILLA_CONFIG_DIR_MISSING),
    ],
    ids=["group-writable", "group-readonly", "setgid-lost", "wrong-group", "absent"],
)
def test_camilla_configs_writable_verdicts(tmp_path, mode, group, status, reason):
    d = tmp_path / "configs"
    if mode is not None:
        d.mkdir()
        os.chmod(d, mode)

    res = audio_runtime_camilla._camilla_configs_writable_result(
        d, expected_group=group or _own_group()
    )

    assert res.status == status
    assert res.reason == reason


def test_camilla_configs_writable_targets_the_constant_dir(monkeypatch, tmp_path):
    """The decorated check reads CAMILLA_CONFIGS_DIR, so the guard stays
    pointed at the dir the deploy actually permissions."""
    monkeypatch.setattr(audio_runtime_camilla, "CAMILLA_CONFIGS_DIR", tmp_path / "nope")

    res = audio_runtime_camilla.check_camilla_configs_writable()

    assert res.status == "warn"
    assert res.reason == audio_runtime_camilla.REASON_CAMILLA_CONFIG_DIR_MISSING


# ------------------------------------------------------- CamillaDSP websocket


def _camilla_controller(monkeypatch, *, volume, clipped):
    constructed: list[tuple[str, int]] = []

    class Controller:
        def __init__(self, host: str, port: int) -> None:
            constructed.append((host, port))

        async def get_volume_db(self):
            if isinstance(volume, Exception):
                raise volume
            return volume

        async def get_clipped_samples(self):
            if isinstance(clipped, Exception):
                raise clipped
            return clipped

        async def close(self):
            pass

    monkeypatch.setattr(audio_runtime_camilla, "CamillaController", Controller)
    return constructed


@pytest.mark.parametrize(
    "volume, clipped, status, reason",
    [
        (-12.5, 0, "ok", ""),
        (
            CamillaUnavailable("operation exceeded 5.0s"), 0, "fail",
            audio_runtime_camilla.REASON_CAMILLA_UNREACHABLE,
        ),
        # clipped_samples is optional: an unavailable status command must not
        # sink the probe.
        (-18.0, CamillaUnavailable("status command unavailable"), "ok", ""),
        # Non-negotiable #1's live half: a fader above the ceiling is a fail.
        (6.0, 0, "fail", audio_runtime_camilla.REASON_CAMILLA_VOLUME_ABOVE_CEILING),
    ],
    ids=["healthy", "timeout", "clipped-optional", "above-ceiling"],
)
async def test_check_camilla_websocket_verdicts(
    monkeypatch, volume, clipped, status, reason
):
    constructed = _camilla_controller(monkeypatch, volume=volume, clipped=clipped)
    cfg = SimpleNamespace(camilla_host="127.0.0.1", camilla_port=1234)

    result = await audio_runtime_camilla.check_camilla_websocket(cfg)

    assert result.status == status
    assert result.reason == reason
    assert constructed == [("127.0.0.1", 1234)]


@pytest.mark.parametrize(
    "raw, status, reason",
    [
        # CamillaDSP's own GetConfig re-serialization of a shipped graph.
        (_GETCONFIG_READBACK.read_text(), "ok", ""),
        ("devices:\n  samplerate: 48000\n  volume_limit: -3.0\n", "ok", ""),
        (
            "devices:\n  samplerate: 48000\n",
            "fail", audio_runtime_camilla.REASON_LIVE_VOLUME_LIMIT_ABSENT,
        ),
        (
            "devices:\n  samplerate: 48000\n  volume_limit: 6.0\n",
            "fail", audio_runtime_camilla.REASON_LIVE_VOLUME_LIMIT_ABOVE_CEILING,
        ),
        # A nested key is not the global fader ceiling.
        (
            "devices:\n  playback:\n    volume_limit: 0.0\n",
            "fail", audio_runtime_camilla.REASON_LIVE_VOLUME_LIMIT_ABSENT,
        ),
        (None, "skipped", audio_runtime_camilla.REASON_LIVE_VOLUME_LIMIT_NO_ACTIVE_GRAPH),
        (" \n", "skipped", audio_runtime_camilla.REASON_LIVE_VOLUME_LIMIT_NO_ACTIVE_GRAPH),
        (
            CamillaUnavailable("operation exceeded 5.0s"),
            "skipped", audio_runtime_camilla.REASON_LIVE_VOLUME_LIMIT_UNAVAILABLE,
        ),
    ],
    ids=[
        "readback", "below", "omitted", "positive", "nested-only", "no-graph", "blank",
        "unreachable",
    ],
)
async def test_check_camilla_live_volume_limit_verdicts(monkeypatch, raw, status, reason):
    class Controller:
        async def get_active_config_raw(self):
            if isinstance(raw, Exception):
                raise raw
            return raw

        async def close(self):
            pass

    monkeypatch.setattr(audio_runtime_camilla, "primary_controller", Controller)

    result = await audio_runtime_camilla.check_camilla_live_volume_limit()

    assert (result.status, result.reason) == (status, reason)


# ------------------------------------------- CamillaDSP volume_limit (NN #1)


def _point_at_config(monkeypatch, tmp_path, text, *, name="v1.yml"):
    config = tmp_path / name
    config.write_text(text)
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {config}\n")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))
    return config


@pytest.mark.parametrize(
    "text, status, reason",
    [
        ("devices:\n  samplerate: 48000\n  volume_limit: 0.0\n", "ok", ""),
        (
            "devices:\n  samplerate: 48000\n", "fail",
            audio_runtime_camilla.REASON_VOLUME_LIMIT_ABSENT,
        ),
        (
            "devices:\n  samplerate: 48000\n  volume_limit: 6.0\n", "fail",
            audio_runtime_camilla.REASON_VOLUME_LIMIT_ABOVE_CEILING,
        ),
        # Ambiguous ownership never resolves to "capped": a nested or
        # duplicated key is not the global fader ceiling.
        (
            "devices:\n  playback:\n    volume_limit: 0.0\n", "fail",
            audio_runtime_camilla.REASON_VOLUME_LIMIT_ABSENT,
        ),
        (
            "devices:\n  volume_limit: 0.0\ndevices: {volume_limit: 9.0}\n", "fail",
            audio_runtime_camilla.REASON_VOLUME_LIMIT_ABSENT,
        ),
        (
            "devices:\n  volume_limit: 0.0\n  volume_limit: 9.0\n", "fail",
            audio_runtime_camilla.REASON_VOLUME_LIMIT_ABSENT,
        ),
    ],
    ids=[
        "capped", "omitted", "positive", "nested-only", "duplicate-block",
        "duplicate-key",
    ],
)
def test_check_camilla_volume_limit_verdicts(
    monkeypatch, tmp_path, text, status, reason
):
    _point_at_config(monkeypatch, tmp_path, text)

    r = audio_runtime_camilla.check_camilla_volume_limit()

    assert r.status == status
    assert r.reason == reason


def test_check_camilla_volume_limit_fails_on_a_missing_config(monkeypatch, tmp_path):
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {tmp_path / 'gone.yml'}\n")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))

    r = audio_runtime_camilla.check_camilla_volume_limit()

    assert r.status == "fail"
    assert r.reason == correction.REASON_CAMILLA_CONFIG_MISSING


# --------------------------------------------------------- camilla ring chunk


def _stage_ring_config(tmp_path, monkeypatch, chunksize: int, extra: str = "") -> None:
    from jasper.fanin_coupling import RING_CAPTURE_DEVICE, RING_PLAYBACK_DEVICE

    _point_at_config(
        monkeypatch,
        tmp_path,
        "devices:\n"
        "  samplerate: 48000\n"
        f"  chunksize: {chunksize}\n"
        f"{extra}"
        "  capture:\n"
        "    type: Alsa\n"
        f'    device: "{RING_CAPTURE_DEVICE}"\n'
        "  playback:\n"
        "    type: Alsa\n"
        f'    device: "{RING_PLAYBACK_DEVICE}"\n',
        name="ring.yml",
    )


def test_check_camilla_ring_chunk_fails_over_capacity(monkeypatch, tmp_path):
    """jts4's shape: a chunk the ring cannot open, so the box is silent."""
    from jasper.fanin_coupling import ring_capacity_frames

    _stage_ring_config(tmp_path, monkeypatch, ring_capacity_frames() * 4)

    r = audio_runtime_camilla.check_camilla_ring_chunk_fits()

    assert r.status == "fail"
    assert r.reason == audio_runtime_camilla.REASON_RING_CHUNK_ABOVE_CAPACITY


def test_check_camilla_ring_chunk_ok_at_capacity(monkeypatch, tmp_path):
    """jts.local's shape: a floor that exactly fills the ring is fine."""
    from jasper.fanin_coupling import ring_capacity_frames

    _stage_ring_config(tmp_path, monkeypatch, ring_capacity_frames())

    r = audio_runtime_camilla.check_camilla_ring_chunk_fits()

    assert r.status == "ok"


def test_check_camilla_ring_chunk_fails_a_target_over_camillas_ceiling(
    monkeypatch, tmp_path
):
    """The state jts4 actually landed in: chunk fits the ring, box still dead.

    256/4096 passes the ring-capacity half and is still refused by CamillaDSP
    (ceiling is chunk x (queuelimit + 4) = 2048), so the box crash-loops with
    the ring half of this check green.
    """
    _stage_ring_config(
        tmp_path, monkeypatch, 256, extra="  queuelimit: 4\n  target_level: 4096\n",
    )

    r = audio_runtime_camilla.check_camilla_ring_chunk_fits()

    assert r.status == "fail"
    assert r.reason == audio_runtime_camilla.REASON_RING_TARGET_LEVEL_ABOVE_CEILING


def test_check_camilla_ring_chunk_warns_on_a_target_over_the_ring_capacity(
    monkeypatch, tmp_path
):
    """A target the whole ring cannot hold is a fill the graph never reaches.

    The shape a pre-ring-geometry config on disk carries: a DAC floor's 1536
    against a 256-frame ring. It clears CamillaDSP's own chunk x (queuelimit+4)
    ceiling, so only the transport bound catches it.
    """
    from jasper.fanin_coupling import ring_capacity_frames

    capacity = ring_capacity_frames()
    _stage_ring_config(
        tmp_path, monkeypatch, capacity,
        extra=f"  queuelimit: 4\n  target_level: {capacity * 2}\n",
    )

    r = audio_runtime_camilla.check_camilla_ring_chunk_fits()

    assert r.status == "warn"
    assert r.reason == audio_runtime_camilla.REASON_RING_TARGET_LEVEL_ABOVE_CAPACITY
    assert r.speaker_silent is False


def test_check_camilla_ring_chunk_not_applicable_off_the_ring(monkeypatch, tmp_path):
    _point_at_config(
        monkeypatch, tmp_path, "devices:\n  samplerate: 48000\n  chunksize: 1024\n",
    )

    r = audio_runtime_camilla.check_camilla_ring_chunk_fits()

    assert r.status == "skipped"
    assert r.reason == audio_runtime_camilla.REASON_RING_CHUNK_NOT_APPLICABLE


@pytest.mark.parametrize(
    ("check", "expected_status", "expected_reason"),
    [
        (
            audio_runtime_fanin.check_fanin_service,
            "fail",
            audio_runtime_fanin.REASON_FANIN_STATUS_MALFORMED,
        ),
        (
            audio_runtime_fanin.check_fanin_tts_drops,
            "skipped",
            audio_runtime_fanin.REASON_FANIN_TTS_STATUS_NOT_PROBED,
        ),
        (
            audio_runtime_outputd.check_outputd_service,
            "fail",
            audio_runtime_outputd.REASON_OUTPUTD_STATUS_MALFORMED,
        ),
        (
            audio_runtime_outputd.check_aec_clock_drift,
            "skipped",
            audio_runtime_outputd.REASON_AEC_CLOCK_STATUS_UNAVAILABLE,
        ),
    ],
)
def test_status_consumers_classify_non_object_root_without_crashing(
    monkeypatch, check, expected_status, expected_reason
):
    _seed_units()
    _patch_status_reader(monkeypatch, b"[]")

    result = check()

    assert result.status == expected_status
    assert result.reason == expected_reason


# Renderer → ring → fan-in → CamillaDSP → outputd → DAC is the only path out,
# so a fail from any of these three means no source can be heard now. They
# share one systemd ladder, `_shared._service_state_failure`; delete the
# guards below when it is replaced.
_OUTPUT_CHAIN_CHECKS = (
    audio_runtime_fanin.check_fanin_service,
    audio_runtime_camilla.check_camilla_service,
    audio_runtime_outputd.check_outputd_service,
)
_OUTPUT_CHAIN_IDS = [check.__name__ for check in _OUTPUT_CHAIN_CHECKS]


@pytest.mark.parametrize(
    "check, reason",
    [
        (audio_runtime_fanin.check_fanin_service,
         audio_runtime_fanin.REASON_FANIN_INACTIVE),
        (audio_runtime_outputd.check_outputd_service,
         audio_runtime_outputd.REASON_OUTPUTD_INACTIVE),
    ],
    ids=["check_fanin_service", "check_outputd_service"],
)
def test_a_stopped_output_chain_unit_reads_speaker_silent(check, reason):
    """camilla's own ladder rows are pinned by
    ``test_check_camilla_service_failures`` above."""
    _seed_units(active="inactive")

    result = check()

    assert (result.status, result.reason, result.speaker_silent) == (
        "fail", reason, True,
    )


@pytest.mark.parametrize("check", _OUTPUT_CHAIN_CHECKS, ids=_OUTPUT_CHAIN_IDS)
def test_no_systemd_claims_nothing_about_the_speaker(check):
    """Nothing was observed at all, so the row may assert no silence."""
    evidence.seed("units", None)

    result = check()

    assert (result.status, result.reason, result.speaker_silent) == (
        "skipped", _shared.REASON_SYSTEMCTL_UNAVAILABLE, False,
    )


def test_a_broken_output_chain_makes_the_whole_report_read_silent():
    """The report-level consequence an operator sees at the top of a run."""
    _seed_units(active="inactive")

    assert summarize([check() for check in _OUTPUT_CHAIN_CHECKS]) == {
        "fails": 3, "warns": 0, "speaker_silent": True,
    }


def test_audio_runtime_plan_doctor_warns_on_shadowed_knob(monkeypatch):
    plan = audio_runtime_plan.build_audio_runtime_plan(
        base_env={"JASPER_CAMILLA_CHUNKSIZE": "512"},
        outputd_env={"JASPER_CAMILLA_CHUNKSIZE": "256"},
        profile_id=APPLE_USB_C_DONGLE_DEVICE_ID,
        route_mode="solo",
    )
    monkeypatch.setattr(
        audio_runtime_plan,
        "build_audio_runtime_plan_from_system",
        lambda: plan,
    )

    r = audio_runtime_camilla.check_audio_runtime_plan()

    assert r.status == "warn"
    assert r.reason == audio_runtime_camilla.REASON_AUDIO_PLAN_WARNINGS


def test_audio_runtime_plan_doctor_reports_policy_and_emitted_apart(
    monkeypatch, tmp_path
):
    """The line carries BOTH numbers, and a difference is not a finding.

    Reporting policy alone printed a chunk no config on the box carried while
    jts4 crash-looped on 1024. The difference is expected on a clamped box and
    on the ACTIVE ring; the `camilla ring chunk` check owns the failure.
    """
    config = tmp_path / "sound_current.yml"
    config.write_text(
        "devices:\n"
        "  samplerate: 48000\n"
        "  chunksize: 256\n"
        "  target_level: 1536\n"
    )
    plan = audio_runtime_plan.build_audio_runtime_plan(
        outputd_env={
            "JASPER_CAMILLA_CHUNKSIZE": "1024",
            "JASPER_CAMILLA_TARGET_LEVEL": "2048",
        },
        route_mode="solo",
        correction_config_path=str(config),
    )
    monkeypatch.setattr(
        audio_runtime_plan,
        "build_audio_runtime_plan_from_system",
        lambda: plan,
    )

    r = audio_runtime_camilla.check_audio_runtime_plan()

    assert r.status == "ok"
    assert r.reason == ""
    assert plan.camilla_emitted is not None


def test_audio_runtime_plan_doctor_passes_a_ring_armed_bonded_box(monkeypatch):
    """A bonded box on the one transport is not an unsupported route.

    Nothing refuses a grouping route mode any more: the only rule that did
    needed the legacy FIFO round-trip spelling, which no writer emits.
    """
    plan = audio_runtime_plan.build_audio_runtime_plan(
        outputd_env={"JASPER_OUTPUTD_CONTENT_BRIDGE": "shm_ring"},
        route_mode="active_leader",
    )
    monkeypatch.setattr(
        audio_runtime_plan,
        "build_audio_runtime_plan_from_system",
        lambda: plan,
    )

    assert audio_runtime_camilla.check_audio_runtime_plan().status == "ok"
    assert plan.errors == ()


def test_audio_runtime_plan_doctor_fails_usb_route_with_legacy_lab_transport(
    monkeypatch,
):
    # A stale non-direct outputd bridge literal (the REMOVED rate_match, or a
    # typo) is a partial flip: outputd fail-safes it to `direct`, but the route
    # policy compares the raw value, so certification stays red. (transport_pipe was
    # removed 2026-07-11); a non-direct bridge without a matching shm_ring pair is
    # not the one transport, so the USB low-latency route refuses it.
    plan = audio_runtime_plan.build_audio_runtime_plan(
        base_env={
            audio_runtime_plan.AUDIO_ROUTE_PROFILE_KEY: (
                audio_runtime_plan.ROUTE_USB_LOW_LATENCY_48K
            )
        },
        outputd_env={"JASPER_OUTPUTD_CONTENT_BRIDGE": "rate_match"},
        route_mode="solo",
    )
    monkeypatch.setattr(
        audio_runtime_plan,
        "build_audio_runtime_plan_from_system",
        lambda: plan,
    )

    r = audio_runtime_camilla.check_audio_runtime_plan()

    assert r.status == "fail"
    assert r.reason == audio_runtime_camilla.REASON_AUDIO_PLAN_ERRORS


_RAWFILE_DEVICES = """\
devices:
  capture:
    type: RawFile
    filename: "/run/jasper-fanin/camilla.pipe"
  playback:
    type: File
    filename: "/run/jasper-outputd/content.pipe"
filters:
"""

_ALSA_DEVICES = _RAWFILE_DEVICES.replace(
    'type: RawFile\n    filename: "/run/jasper-fanin/camilla.pipe"',
    'type: Alsa\n    device: "plug:jasper_capture"',
)


@pytest.mark.parametrize(
    "text, expected",
    [
        # A RawFile capture beside a File playback: the playback sink must not
        # be misread as the capture type.
        (_RAWFILE_DEVICES, {"capture_type": "RawFile", "playback_type": "File"}),
        (_ALSA_DEVICES, {"capture_type": "Alsa", "playback_type": "File"}),
        # No devices block at all is "nothing to compare", not a wrong answer.
        ("filters:\n  x: 1\n", {}),
    ],
    ids=["rawfile", "alsa", "no-devices-block"],
)
def test_loaded_device_fields_reads_every_lane_from_one_config(
    tmp_path, text, expected
):
    cfg = tmp_path / "c.yml"
    cfg.write_text(text)

    fields = audio_runtime_camilla._loaded_device_fields(cfg)

    assert {k: fields.get(k) for k in expected} == expected
    if not expected:
        assert fields == {}


def test_loaded_device_fields_is_empty_for_a_config_that_is_not_there(tmp_path):
    assert audio_runtime_camilla._loaded_device_fields(tmp_path / "gone.yml") == {}
    assert audio_runtime_camilla._loaded_device_fields(None) == {}


# --- D-list survey finding 1 / wide-output-path PR-1: playback format check --
# Before this check, nothing read the CamillaDSP playback format back off a
# live config — a half-flip (emitter regenerated against one
# DEFAULT_PLAYBACK_FORMAT while the loaded file reflects another) was silent.

_S16_PLAYBACK_CFG = """\
devices:
  samplerate: 48000
  capture:
    type: Alsa
    channels: 2
    device: "plug:jasper_capture"
    format: S32_LE
  playback:
    type: Alsa
    channels: 2
    device: "outputd_content_playback"
    format: S16_LE
filters:
"""

_S32_PLAYBACK_CFG = """\
devices:
  samplerate: 48000
  capture:
    type: Alsa
    channels: 2
    device: "plug:jasper_capture"
    format: S32_LE
  playback:
    type: Alsa
    channels: 2
    device: "outputd_content_playback"
    format: S32_LE
filters:
"""


def _run_format_check(monkeypatch, tmp_path, cfg_text):
    cfg = tmp_path / "sound_current.yml"
    cfg.write_text(cfg_text)
    evidence.seed("camilla_config", (cfg.parent, str(cfg)))
    return audio_runtime_camilla.check_camilla_playback_format()


def test_playback_format_ok_when_alsa_lane_matches_the_wide_default(
    monkeypatch, tmp_path
):
    # Green on a flipped box: the ALSA content lane carries
    # DEFAULT_PLAYBACK_FORMAT, S32_LE since PR-6.
    res = _run_format_check(monkeypatch, tmp_path, _S32_PLAYBACK_CFG)
    assert res.status == "ok"
    assert res.reason == ""


def test_playback_format_fails_on_a_half_flipped_narrow_alsa_lane(
    monkeypatch, tmp_path
):
    # Prove the check CAN STILL FAIL in the post-flip world (mutation rule): an
    # ALSA lane config left at S16_LE after the flip is a half-flipped box — a
    # stale generated file, or an emitter that regenerated against a different
    # constant — and on the raw active lane it is what makes outputd's open fail
    # rather than convert. Red doctor line instead of silence.
    res = _run_format_check(monkeypatch, tmp_path, _S16_PLAYBACK_CFG)
    assert res.status == "fail"
    assert res.reason == audio_runtime_camilla.REASON_PLAYBACK_FORMAT_MISMATCH


def test_playback_format_skipped_when_no_config_loaded(monkeypatch, tmp_path):
    evidence.seed("camilla_config", (tmp_path, None))
    res = audio_runtime_camilla.check_camilla_playback_format()
    assert res.status == "skipped"
    assert res.reason == audio_runtime_camilla.REASON_PLAYBACK_FORMAT_NO_CONFIG


def test_playback_format_skipped_when_config_has_no_format_field(monkeypatch, tmp_path):
    res = _run_format_check(monkeypatch, tmp_path, "filters:\n")
    assert res.status == "skipped"
    assert res.reason == audio_runtime_camilla.REASON_PLAYBACK_FORMAT_FIELD_ABSENT


# --- NIT1 (PR-1 gate review): the check is lane-aware, keyed on playback type -

_S16_FILE_PLAYBACK_CFG = """\
devices:
  samplerate: 48000
  capture:
    type: Alsa
    channels: 2
    device: "plug:jasper_capture"
    format: S32_LE
  playback:
    type: File
    channels: 2
    filename: "/run/jasper-snapserver/snapfifo"
    format: S16_LE
filters:
"""

_S32_FILE_PLAYBACK_CFG = """\
devices:
  samplerate: 48000
  capture:
    type: Alsa
    channels: 2
    device: "plug:jasper_capture"
    format: S32_LE
  playback:
    type: File
    channels: 2
    filename: "/run/jasper-snapserver/snapfifo"
    format: S32_LE
filters:
"""


_S16_RING_PLAYBACK_CFG = """\
devices:
  samplerate: 48000
  capture:
    type: Alsa
    channels: 2
    device: "jts_ring_capture"
    format: S16_LE
  playback:
    type: Alsa
    channels: 2
    device: "jts_ring_playback"
    format: S16_LE
filters:
"""

_S32_RING_PLAYBACK_CFG = _S16_RING_PLAYBACK_CFG.replace(
    '    device: "jts_ring_playback"\n    format: S16_LE',
    '    device: "jts_ring_playback"\n    format: S32_LE',
)


def _pin_ring_wire_narrow(monkeypatch, tmp_path):
    """Pin this box's ring wire to the NARROW token via the operator lever.

    ``JASPER_FANIN_RING_WIRE_FORMAT`` is the only way a box declares S16_LE
    since the resolver's default went WIDE (PR #2601) — nothing else in the
    repo writes it (``jasper.fanin_coupling.RING_WIRE_FORMAT_ENV_VAR``).
    Isolated to a tmp ``fanin.env``, the FIRST file the resolver's chain reads,
    so the pin neither leaks from nor needs the developer host's real
    ``/var/lib/jasper/fanin.env``.
    """
    # Imported BEFORE the patch below: it copies env_load's constants at import
    # time, so importing it inside the patched window would bake in the tmp path.
    import jasper.fanin.coupling_reconcile  # noqa: F401
    import jasper.fanin.ring_readiness as ring_readiness
    from jasper.fanin_coupling import RING_WIRE_FORMAT_ENV_VAR

    fanin_env = tmp_path / "fanin.env"
    fanin_env.write_text(f"{RING_WIRE_FORMAT_ENV_VAR}=S16_LE\n", encoding="utf-8")
    monkeypatch.setattr(ring_readiness, "FANIN_ENV_PATH", str(fanin_env))
    monkeypatch.setattr("jasper.env_load.FANIN_ENV_PATH", str(fanin_env))


def test_playback_format_ok_for_an_armed_ring_pinned_narrow_on_an_otherwise_wide_box(
    monkeypatch, tmp_path
):
    """AN ARMED RING IS ``type: Alsa`` — the File split alone does NOT cover it.

    Its width comes from resolve_ring_wire through the coupling's own kwargs (the
    PR-6 ring ruling), so a ring config at the ring's OWN resolved width is
    HEALTHY even when the general lane wants something else. Keyed
    on the ring's playback device, this must be green; keyed only on the File
    type, it red-lines every armed-ring box — including the certified-latency USB
    box, whose canary criterion is literally "doctor green" — with a remediation
    that regenerates the identical config.

    SINCE THE RING-WIRE DEFAULT FLIP, an UNDECLARED box's ring resolves S32_LE
    too — the same value as ``DEFAULT_PLAYBACK_FORMAT`` — so the two lanes no
    longer differ for free the way they did when narrow was the resolver's
    default. ``_pin_ring_wire_narrow`` declares the operator lever
    (``JASPER_FANIN_RING_WIRE_FORMAT=S16_LE``) so the ring resolves narrow
    while the general (loopback) lane stays wide, recreating the
    two-lanes-can-legitimately-differ shape this test exists to prove.
    """
    from jasper.fanin_coupling import (
        DEFAULT_PLAYBACK_FORMAT,
        RING_PLAYBACK_DEVICE,
        resolve_ring_wire,
    )

    _pin_ring_wire_narrow(monkeypatch, tmp_path)
    assert resolve_ring_wire().sample_format != DEFAULT_PLAYBACK_FORMAT
    assert RING_PLAYBACK_DEVICE in _S16_RING_PLAYBACK_CFG
    res = _run_format_check(monkeypatch, tmp_path, _S16_RING_PLAYBACK_CFG)
    assert res.status == "ok"
    assert res.reason == ""


def test_playback_format_fails_on_a_ring_config_that_drifted_wide(
    monkeypatch, tmp_path
):
    """The ring split must not become "any ring device auto-passes": a config
    declaring a width the box's resolved ring wire does not carry is a genuinely
    broken box and stays red — even though S32 is what the loopback lane wants
    (and, since the ring-wire default flip, what an UNDECLARED box's ring
    resolves to as well), which is exactly the confusion the three-way split
    has to get right.

    This is the check that has to catch it, because the ring LAYOUT accepts both
    S16LE and S32LE: a config drifted to the other one is inside the accept-set,
    so the attach would not refuse it — the ends would simply be built to
    different widths.

    ``_pin_ring_wire_narrow`` declares this box's ring wire S16_LE (the
    operator lever) so the S32_LE config below is a genuine drift again — on an
    undeclared box the same config would simply match the new default and there
    would be nothing here to catch.
    """
    _pin_ring_wire_narrow(monkeypatch, tmp_path)
    res = _run_format_check(monkeypatch, tmp_path, _S32_RING_PLAYBACK_CFG)
    assert res.status == "fail"
    assert res.reason == audio_runtime_camilla.REASON_PLAYBACK_FORMAT_MISMATCH


def test_playback_format_ok_for_file_sink_pinned_narrow_while_the_lane_is_wide(
    monkeypatch, tmp_path
):
    # The bonded-leader pipe sink (and the active-speaker parked graph's
    # /dev/null sink) are pinned to DEFAULT_PIPE_SINK_FORMAT independently of
    # the general program lane (D4), so a File-type S16 config stays green while
    # the ALSA lane is S32 — the two constants now genuinely differ, no
    # monkeypatch needed. Without the lane split this would red-line every
    # healthy pipe-sink leader and parked box.
    from jasper.camilla_config_contract import DEFAULT_PIPE_SINK_FORMAT
    from jasper.fanin_coupling import DEFAULT_PLAYBACK_FORMAT

    assert DEFAULT_PIPE_SINK_FORMAT != DEFAULT_PLAYBACK_FORMAT
    res = _run_format_check(monkeypatch, tmp_path, _S16_FILE_PLAYBACK_CFG)
    assert res.status == "ok"
    assert res.reason == ""


def test_playback_format_fails_on_a_deliberately_wide_file_sink_config(
    monkeypatch, tmp_path
):
    # The lane split must not become "any File type auto-passes": a File
    # sink whose format has genuinely drifted off DEFAULT_PIPE_SINK_FORMAT
    # (S32 here) still fails — even though S32 is what the ALSA lane wants,
    # which is exactly the confusion the lane split has to get right.
    res = _run_format_check(monkeypatch, tmp_path, _S32_FILE_PLAYBACK_CFG)
    assert res.status == "fail"
    assert res.reason == audio_runtime_camilla.REASON_PLAYBACK_FORMAT_MISMATCH


def test_expected_playback_format_names_one_owner_per_lane(monkeypatch, tmp_path):
    """WHICH constant owns the width, lane by lane.

    The check reports one mismatch reason for all three lanes, so the lane
    split is pinned here, on the resolver whose whole output is that pair.
    """
    from jasper.camilla_config_contract import DEFAULT_PIPE_SINK_FORMAT
    from jasper.fanin_coupling import (
        DEFAULT_PLAYBACK_FORMAT,
        RING_ACTIVE_PLAYBACK_DEVICE,
        RING_PLAYBACK_DEVICE,
        resolve_ring_wire,
    )

    _pin_ring_wire_narrow(monkeypatch, tmp_path)
    ring_wire = resolve_ring_wire().sample_format
    assert ring_wire != DEFAULT_PLAYBACK_FORMAT

    assert audio_runtime_camilla._expected_playback_format("File", None) == (
        DEFAULT_PIPE_SINK_FORMAT,
        "DEFAULT_PIPE_SINK_FORMAT",
    )
    for device in (RING_PLAYBACK_DEVICE, RING_ACTIVE_PLAYBACK_DEVICE):
        assert audio_runtime_camilla._expected_playback_format("Alsa", device) == (
            ring_wire,
            "resolve_ring_wire",
        )
    assert audio_runtime_camilla._expected_playback_format(
        "Alsa", "outputd_content_playback"
    ) == (DEFAULT_PLAYBACK_FORMAT, "DEFAULT_PLAYBACK_FORMAT")


def test_no_doctor_remedy_names_a_coupling_the_cli_rejects():
    """THE CLASS, not the three instances below.

    Eight of the audio_runtime_camilla module's remedies named
    `jasper-fanin-coupling-reconcile loopback` — a coupling ADR-0100 removed
    from the CLI's `choices`, so an operator who copied one got `exit 2` and an
    argparse error instead of a fix. A dead remedy is worse than no remedy: it
    spends the reader's trust in the rest of the line.

    EVERY DOCTOR MODULE, not just this one's subject: an operator copies a line
    out of `jasper-doctor` without knowing which module printed it, so the class
    is only closed when the whole package is judged.

    DERIVED FROM THE PRINTED TEXT, never from a list here: every word the doctor
    prints after this command name is judged, and the verdict is the
    reconciler's OWN argparse. Deriving the candidates from a coupling
    vocabulary instead stopped judging the retired token the day that token was
    deleted — exactly when a stale remedy naming it would be hardest to see. The
    rule that makes this safe is one the doctor already keeps: the word after
    this command name is always its ARGUMENT, never English prose.
    """
    import re
    from pathlib import Path

    import jasper.fanin.coupling_reconcile as cr
    from jasper.cli import doctor as doctor_pkg

    modules = sorted(Path(doctor_pkg.__file__).parent.glob("*.py"))
    assert len(modules) > 1, "the doctor package glob found nothing to judge"
    source = "\n".join(m.read_text(encoding="utf-8") for m in modules)
    # Same source line only: a remedy split across lines puts its verb on the
    # next one, and a comment that merely names the command carries none at all.
    named = {
        m.group(1)
        for m in re.finditer(
            r"jasper-fanin-coupling-reconcile[^\S\n]+([A-Za-z_][\w-]*)", source
        )
    }
    assert named, "the doctor stopped printing this remedy at all"

    for token in sorted(named):
        accepted = True
        try:
            cr.main([token, "--help"])
        except SystemExit as exc:
            # 0 = --help printed (the token parsed); 2 = argparse rejected it.
            accepted = exc.code == 0
        assert accepted, (
            f"the doctor prints `jasper-fanin-coupling-reconcile {token}`, which "
            "the CLI rejects"
        )
def _silent_camilla_recover_park(monkeypatch, tmp_path):
    from jasper.control import camilla_recover_state

    _seed_units(active="inactive")
    monkeypatch.setattr(
        camilla_recover_state,
        "snapshot",
        lambda *a, **k: {
            "status": "parked",
            "parked": True,
            "reason": "camilla_start_failed",
            "parked_utc": "2026-01-15T12:00:00Z",
        },
    )
    return audio_runtime_camilla.check_camilla_recover_park


def test_camilla_recover_park_detail_carries_the_writers_own_timestamp(
    monkeypatch, tmp_path
):
    """A malformed parked_utc must still show up verbatim, never drop the line."""
    result = _silent_camilla_recover_park(monkeypatch, tmp_path)()
    assert "2026-01-15T12:00:00Z" in result.detail


def _camilla_recover_park_check(monkeypatch, tmp_path, *, record: str | None, active: str):
    """Drive the real reader + the real unit-active cross-check: a record
    file on disk (or none) plus jasper-camilla's seeded ActiveState."""
    target = tmp_path / "camilla-recover.state"
    if record is not None:
        target.write_text(record)
    monkeypatch.setenv("JASPER_CAMILLA_RECOVER_PARK_STATE", str(target))
    _seed_units(active=active)
    return audio_runtime_camilla.check_camilla_recover_park()


_CAMILLA_PARK_RECORD = (
    "parked_utc=2026-01-15T12:00:00Z\n"
    "reason=camilla_start_failed\n"
    "detail=would not start\n"
    "action=fix it\n"
    "re_arm=restart it\n"
)


@pytest.mark.parametrize(
    "record, active, status, reason",
    [
        (None, "inactive", "ok", ""),
        (_CAMILLA_PARK_RECORD, "inactive",
         "fail", audio_runtime_camilla.REASON_CAMILLA_GRAPH_PARKED),
        (_CAMILLA_PARK_RECORD, "active",
         "warn", audio_runtime_camilla.REASON_CAMILLA_PARK_RECORD_STALE),
    ],
    ids=["no-record", "parked-unit-inactive", "stale-unit-active"],
)
def test_camilla_recover_park_verdicts(
    tmp_path, monkeypatch, record, active, status, reason,
):
    """The three cells record-present crossed with unit-active: no record is
    healthy; a record while camilla is NOT active is a real park (fail); a
    record that survives while camilla IS active is stale (warn), mirroring
    outputd's ``check_outputd_failure_reconcile_park`` (#4930)."""
    result = _camilla_recover_park_check(
        monkeypatch, tmp_path, record=record, active=active,
    )

    assert (result.status, result.reason) == (status, reason)
