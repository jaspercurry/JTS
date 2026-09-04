# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the doctor's CamillaDSP-graph checks and runtime plan."""

import pytest

from jasper import audio_runtime_plan
from jasper.cli import doctor
from jasper.output_hardware import (
    APPLE_USB_C_DONGLE_DEVICE_ID,
)
from jasper.cli.doctor import (
    audio_runtime_camilla,
)

from ._doctor_audio_runtime_fixtures import (
    _fake_systemctl,
    _patch_fanin_status_socket,
    _patch_fanin_systemctl,
)


def _patch_camilla_systemctl(monkeypatch, *, enabled="enabled", active="active"):
    monkeypatch.setattr(
        doctor.audio_runtime_camilla, "_run", _fake_systemctl(enabled, active)
    )


def test_check_camilla_service_ok_when_enabled_and_active(monkeypatch):
    _patch_camilla_systemctl(monkeypatch)

    result = doctor.check_camilla_service()

    assert result.status == "ok"
    assert result.detail == "enabled and active"


def test_check_camilla_service_fails_on_a_clean_stop(monkeypatch):
    """The #2163 state: enabled, cleanly inactive, never `failed`.

    `check_service_runtime_state` returns ok for this, and
    `check_camilla_websocket` reports it as an unreachable websocket.
    """
    _patch_camilla_systemctl(monkeypatch, active="inactive")

    result = doctor.check_camilla_service()

    assert result.status == "fail"
    assert "enabled but state=inactive" in result.detail
    assert "jasper-camilla-recover" in result.detail


def test_check_camilla_service_fails_when_disabled(monkeypatch):
    _patch_camilla_systemctl(monkeypatch, enabled="disabled", active="inactive")

    result = doctor.check_camilla_service()

    assert result.status == "fail"
    assert "state=disabled" in result.detail
    assert "systemctl enable --now jasper-camilla.service" in result.detail


def test_check_camilla_service_fails_when_unit_is_not_installed(monkeypatch):
    _patch_camilla_systemctl(monkeypatch, enabled="not-found", active="inactive")

    result = doctor.check_camilla_service()

    assert result.status == "fail"
    assert "not installed" in result.detail


@pytest.mark.parametrize(
    ("check", "expected_status"),
    [
        (doctor.check_fanin_service, "fail"),
        (doctor.check_fanin_tts_drops, "ok"),
        (doctor.check_outputd_service, "fail"),
        (doctor.check_aec_clock_drift, "ok"),
    ],
)
def test_status_consumers_classify_non_object_root_without_crashing(
    monkeypatch, check, expected_status
):
    """A STATUS reply whose JSON root is a list reaches every consumer as a
    classified verdict, never as an escaping exception."""
    _patch_fanin_systemctl(monkeypatch)
    _patch_fanin_status_socket(monkeypatch, b"[]")

    assert check().status == expected_status


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

    r = doctor.check_audio_runtime_plan()

    assert r.status == "warn"
    assert "one knob has two homes" in r.detail


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

    r = doctor.check_audio_runtime_plan()

    assert r.status == "ok"
    assert "camilla_policy=1024/2048" in r.detail
    assert "camilla_emitted=256/1536" in r.detail


def test_audio_runtime_plan_doctor_passes_a_ring_armed_bonded_box(monkeypatch):
    """A bonded box on the one transport is not an unsupported route.

    Nothing refuses a grouping route mode any more: the only rule that did
    needed the legacy FIFO round-trip spelling, which no writer emits.
    """
    plan = audio_runtime_plan.build_audio_runtime_plan(
        fanin_env={"JASPER_FANIN_CAMILLA_COUPLING": "shm_ring"},
        outputd_env={"JASPER_OUTPUTD_CONTENT_BRIDGE": "shm_ring"},
        route_mode="active_leader",
    )
    monkeypatch.setattr(
        audio_runtime_plan,
        "build_audio_runtime_plan_from_system",
        lambda: plan,
    )

    assert doctor.check_audio_runtime_plan().status == "ok"
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
        fanin_env={"JASPER_FANIN_CAMILLA_COUPLING": "shm_ring"},
        outputd_env={"JASPER_OUTPUTD_CONTENT_BRIDGE": "rate_match"},
        route_mode="solo",
    )
    monkeypatch.setattr(
        audio_runtime_plan,
        "build_audio_runtime_plan_from_system",
        lambda: plan,
    )

    r = doctor.check_audio_runtime_plan()

    assert r.status == "fail"
    assert "is not the one transport" in r.detail


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
    monkeypatch.setattr(
        audio_runtime_camilla, "_active_camilla_config_path", lambda: (cfg.parent, str(cfg))
    )
    return audio_runtime_camilla.check_camilla_playback_format()


def test_playback_format_ok_when_alsa_lane_matches_the_wide_default(
    monkeypatch, tmp_path
):
    # Green on a flipped box: the ALSA content lane carries
    # DEFAULT_PLAYBACK_FORMAT, S32_LE since PR-6.
    res = _run_format_check(monkeypatch, tmp_path, _S32_PLAYBACK_CFG)
    assert res.status == "ok"
    assert "S32_LE" in res.detail


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
    assert "S16_LE" in res.detail
    assert "S32_LE" in res.detail
    assert "DEFAULT_PLAYBACK_FORMAT" in res.detail


def test_playback_format_ok_when_no_config_loaded(monkeypatch, tmp_path):
    monkeypatch.setattr(
        audio_runtime_camilla, "_active_camilla_config_path", lambda: (tmp_path, None)
    )
    res = audio_runtime_camilla.check_camilla_playback_format()
    assert res.status == "ok"


def test_playback_format_ok_when_config_has_no_format_field(monkeypatch, tmp_path):
    res = _run_format_check(monkeypatch, tmp_path, "filters:\n")
    assert res.status == "ok"


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
    from jasper.fanin_coupling import RING_WIRE_FORMAT_ENV_VAR

    fanin_env = tmp_path / "fanin.env"
    fanin_env.write_text(f"{RING_WIRE_FORMAT_ENV_VAR}=S16_LE\n", encoding="utf-8")
    monkeypatch.setattr(
        "jasper.fanin.ring_health.FANIN_ENV_PATH", str(fanin_env)
    )
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.FANIN_ENV_PATH", str(fanin_env)
    )


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
    from jasper.camilla_config_contract import DEFAULT_PLAYBACK_FORMAT
    from jasper.fanin_coupling import RING_PLAYBACK_DEVICE, resolve_ring_wire

    _pin_ring_wire_narrow(monkeypatch, tmp_path)
    assert resolve_ring_wire().sample_format != DEFAULT_PLAYBACK_FORMAT
    assert RING_PLAYBACK_DEVICE in _S16_RING_PLAYBACK_CFG
    res = _run_format_check(monkeypatch, tmp_path, _S16_RING_PLAYBACK_CFG)
    assert res.status == "ok"
    assert "S16_LE" in res.detail
    assert "resolve_ring_wire" in res.detail


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
    assert "S32_LE" in res.detail
    assert "S16_LE" in res.detail
    assert "resolve_ring_wire" in res.detail


def test_playback_format_ok_for_file_sink_pinned_narrow_while_the_lane_is_wide(
    monkeypatch, tmp_path
):
    # The bonded-leader pipe sink (and the active-speaker parked graph's
    # /dev/null sink) are pinned to DEFAULT_PIPE_SINK_FORMAT independently of
    # the general program lane (D4), so a File-type S16 config stays green while
    # the ALSA lane is S32 — the two constants now genuinely differ, no
    # monkeypatch needed. Without the lane split this would red-line every
    # healthy pipe-sink leader and parked box.
    from jasper.camilla_config_contract import (
        DEFAULT_PIPE_SINK_FORMAT,
        DEFAULT_PLAYBACK_FORMAT,
    )

    assert DEFAULT_PIPE_SINK_FORMAT != DEFAULT_PLAYBACK_FORMAT
    res = _run_format_check(monkeypatch, tmp_path, _S16_FILE_PLAYBACK_CFG)
    assert res.status == "ok"
    assert "S16_LE" in res.detail


def test_playback_format_fails_on_a_deliberately_wide_file_sink_config(
    monkeypatch, tmp_path
):
    # The lane split must not become "any File type auto-passes": a File
    # sink whose format has genuinely drifted off DEFAULT_PIPE_SINK_FORMAT
    # (S32 here) still fails — even though S32 is what the ALSA lane wants,
    # which is exactly the confusion the lane split has to get right.
    res = _run_format_check(monkeypatch, tmp_path, _S32_FILE_PLAYBACK_CFG)
    assert res.status == "fail"
    assert "S32_LE" in res.detail
    assert "S16_LE" in res.detail
    assert "DEFAULT_PIPE_SINK_FORMAT" in res.detail


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
