# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import subprocess
import sys
import types
from dataclasses import replace
from pathlib import Path

import pytest

from jasper import chip_aec_alignment as alignment
from jasper import output_hardware
from jasper.chip_aec_alignment import AlignmentArtifact, AlignmentIdentity
from jasper.cli import aec_init
from jasper.mics import xvf3800


ROOT = Path(__file__).resolve().parents[1]


class _Closeable:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeXvfDevice:
    def __init__(
        self,
        *,
        fail_on: set[str] | None = None,
        readback_overrides: dict[str, list[int | float]] | None = None,
    ) -> None:
        self.dev = _Closeable()
        self.fail_on = fail_on or set()
        self.readback_overrides = readback_overrides or {}
        self.values: dict[str, tuple[int | float, ...]] = {"VERSION": (2, 0, 8)}
        self.writes: list[tuple[str, list[int | float]]] = []
        self.serial = "XVF3800-001"

    def factory_serial(self) -> str:
        return self.serial

    def write(self, name: str, values: list[int | float]) -> None:
        if name in self.fail_on:
            raise RuntimeError(f"{name} failed")
        self.writes.append((name, list(values)))
        self.values[name] = tuple(self.readback_overrides.get(name, values))

    def read(self, name: str) -> tuple[int | float, ...]:
        return self.values[name]


def _install_fake_xvf(monkeypatch, dev: _FakeXvfDevice) -> None:
    fake_xvf = types.ModuleType("jasper.xvf")
    fake_xvf.xvf_host = types.SimpleNamespace(
        find=lambda: dev,
        XvfControlError=RuntimeError,
    )
    monkeypatch.setitem(sys.modules, "jasper.xvf", fake_xvf)


def _stub_amixer(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(args, **_kwargs):
        calls.append(list(args))
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stderr="",
            stdout=(
                "Limits: Playback 0 - 60\n"
                "Front Left: Playback 60 [0.00dB] [on]\n"
            ),
        )

    monkeypatch.setattr(aec_init.subprocess, "run", fake_run)
    return calls


def _write_map(dev: _FakeXvfDevice) -> dict[str, list[int | float]]:
    return {name: values for name, values in dev.writes}


def _output_state(
    *,
    profile_id: str = "apple_usb_c_dongle",
    card_id: str = "A",
    serial: str | None = "DWH53530FLL2FN3A3",
) -> output_hardware.OutputHardwareState:
    child = output_hardware.OutputCardFact(
        card_id=card_id,
        device_id=profile_id,
        serial=serial,
    )
    return output_hardware.OutputHardwareState(
        profile_id=profile_id,
        profile_label=profile_id,
        status="ready",
        physical_output_count=2 if profile_id == "apple_usb_c_dongle" else 8,
        selected_card_id=card_id,
        selected_pcm=f"hw:CARD={card_id},DEV=0",
        child_devices=(child,),
    )


def test_explicit_lab_route_restores_complete_bypass_profile(monkeypatch) -> None:
    dev = _FakeXvfDevice()
    _install_fake_xvf(monkeypatch, dev)
    _stub_amixer(monkeypatch)
    monkeypatch.delenv("JASPER_AEC_CORPUS_CHIP_AEC_ENABLED", raising=False)
    monkeypatch.delenv("JASPER_AEC_CHIP_AEC_ENABLED", raising=False)

    assert aec_init.main() == 0

    writes = _write_map(dev)
    assert writes["SHF_BYPASS"] == [1]
    assert writes["AEC_ASROUTONOFF"] == [0]
    assert writes["AEC_FIXEDBEAMSONOFF"] == [0]
    assert writes["AUDIO_MGR_OP_L"] == [8, 0]
    assert writes["AUDIO_MGR_OP_R"] == [8, 0]
    assert dev.dev.closed is True


def test_corpus_profile_applies_and_verifies_expected_chip_routes(monkeypatch) -> None:
    dev = _FakeXvfDevice()
    _install_fake_xvf(monkeypatch, dev)
    _stub_amixer(monkeypatch)
    monkeypatch.setenv("JASPER_AEC_CORPUS_CHIP_AEC_ENABLED", "1")
    monkeypatch.setenv("JASPER_AEC_CORPUS_CHIP_SYS_DELAY", "233")

    assert aec_init.main() == 0

    writes = _write_map(dev)
    assert writes["SHF_BYPASS"] == [0]
    assert writes["AUDIO_MGR_SYS_DELAY"] == [233]
    assert writes["AEC_ASROUTONOFF"] == [1]
    assert writes["AEC_FIXEDBEAMSONOFF"] == [1]
    assert writes["AEC_FIXEDBEAMSGATING"] == [1]
    assert writes["AUDIO_MGR_OP_L"] == [7, 0]
    assert writes["AUDIO_MGR_OP_R"] == [7, 1]
    assert dev.dev.closed is True


def test_production_chip_profile_uses_chip_flag_and_delay(monkeypatch) -> None:
    dev = _FakeXvfDevice()
    _install_fake_xvf(monkeypatch, dev)
    _stub_amixer(monkeypatch)
    monkeypatch.delenv("JASPER_AEC_CORPUS_CHIP_AEC_ENABLED", raising=False)
    monkeypatch.setenv("JASPER_AEC_CHIP_AEC_ENABLED", "1")
    identity = AlignmentIdentity(
        "xvf3800_legacy_square_6ch",
        "XVF3800-001",
        "firmware",
        xvf3800.SQUARE_FIXED_150_210_PLAN.plan_id,
        xvf3800.chip_aec_fixed_profile_fingerprint(
            xvf3800.SQUARE_FIXED_150_210_PLAN
        ),
        "apple_usb_c_dongle",
        "usb-serial:DWH53530FLL2FN3A3",
        "single:outputd_dac",
        "S16_LE",
        48_000,
        2,
        128,
        384,
    )
    monkeypatch.setattr(
        aec_init, "load_artifact", lambda: AlignmentArtifact(identity, 245)
    )
    monkeypatch.setattr(
        aec_init,
        "collect_reference_queue",
        lambda _pcm: ({"reference_outputs": {}}, (283,) * 8),
    )
    monkeypatch.setattr(
        aec_init, "build_identity", lambda *_args, **_kwargs: identity
    )

    assert aec_init.main() == 0

    writes = _write_map(dev)
    assert writes["SHF_BYPASS"] == [0]
    assert writes["AUDIO_MGR_SYS_DELAY"] == [-38]
    assert writes["AEC_ASROUTONOFF"] == [1]
    assert writes["AEC_FIXEDBEAMSONOFF"] == [1]
    assert writes["AEC_FIXEDBEAMSGATING"] == [1]
    assert writes["AUDIO_MGR_OP_L"] == [7, 0]
    assert writes["AUDIO_MGR_OP_R"] == [7, 1]
    assert dev.dev.closed is True


def test_production_chip_profile_parks_when_commissioning_is_missing(
    monkeypatch,
) -> None:
    dev = _FakeXvfDevice()
    _install_fake_xvf(monkeypatch, dev)
    monkeypatch.delenv("JASPER_AEC_CORPUS_CHIP_AEC_ENABLED", raising=False)
    monkeypatch.setenv("JASPER_AEC_CHIP_AEC_ENABLED", "1")
    monkeypatch.setattr(
        aec_init,
        "load_artifact",
        lambda: (_ for _ in ()).throw(ValueError("missing")),
    )

    assert aec_init.main() == aec_init.COMMISSION_REQUIRED_EXIT
    assert _write_map(dev)["SHF_BYPASS"] == [1]


def test_production_chip_profile_parks_on_physical_output_change(monkeypatch) -> None:
    dev = _FakeXvfDevice()
    _install_fake_xvf(monkeypatch, dev)
    monkeypatch.delenv("JASPER_AEC_CORPUS_CHIP_AEC_ENABLED", raising=False)
    monkeypatch.setenv("JASPER_AEC_CHIP_AEC_ENABLED", "1")
    plan = xvf3800.SQUARE_FIXED_150_210_PLAN
    identity = AlignmentIdentity(
        "xvf3800_legacy_square_6ch", "XVF3800-001", "firmware", plan.plan_id,
        xvf3800.chip_aec_fixed_profile_fingerprint(plan),
        "apple_usb_c_dongle", "usb-serial:DWH53530FLL2FN3A3",
        "single:outputd_dac", "S16_LE", 48_000, 2, 128, 384,
    )
    monkeypatch.setattr(
        aec_init, "load_artifact",
        lambda: AlignmentArtifact(
            replace(identity, output_hardware_key="usb-serial:replacement"), 245
        ),
    )
    monkeypatch.setattr(
        aec_init, "collect_reference_queue",
        lambda _pcm: ({"reference_outputs": {}}, (283,) * 8),
    )
    monkeypatch.setattr(
        aec_init, "build_identity", lambda *_args, **_kwargs: identity
    )

    assert aec_init.main() == aec_init.COMMISSION_REQUIRED_EXIT
    assert _write_map(dev) == {"SHF_BYPASS": [1]}


def test_production_chip_profile_parks_on_physical_xvf_change(monkeypatch) -> None:
    dev = _FakeXvfDevice()
    _install_fake_xvf(monkeypatch, dev)
    monkeypatch.delenv("JASPER_AEC_CORPUS_CHIP_AEC_ENABLED", raising=False)
    monkeypatch.setenv("JASPER_AEC_CHIP_AEC_ENABLED", "1")
    plan = xvf3800.SQUARE_FIXED_150_210_PLAN
    identity = AlignmentIdentity(
        "xvf3800_legacy_square_6ch", "XVF3800-001", "firmware", plan.plan_id,
        xvf3800.chip_aec_fixed_profile_fingerprint(plan),
        "apple_usb_c_dongle", "usb-serial:DWH53530FLL2FN3A3",
        "single:outputd_dac", "S16_LE", 48_000, 2, 128, 384,
    )
    monkeypatch.setattr(
        aec_init, "load_artifact",
        lambda: AlignmentArtifact(replace(identity, xvf_serial="replacement"), 245),
    )
    monkeypatch.setattr(
        aec_init,
        "collect_reference_queue",
        lambda _pcm: ({"reference_outputs": {}}, (283,) * 8),
    )
    monkeypatch.setattr(
        aec_init, "build_identity", lambda *_args, **_kwargs: identity
    )

    assert aec_init.main() == aec_init.COMMISSION_REQUIRED_EXIT
    assert _write_map(dev) == {"SHF_BYPASS": [1]}


def test_production_chip_profile_parks_when_final_edge_format_changes(
    monkeypatch,
) -> None:
    # The commissioned K is only valid for the electrical edge it was measured
    # against, so a moved final-edge format must invalidate the artifact the
    # same way a swapped DAC or XVF does. Exercised through main() so this pins
    # the real CommissionRequired path, not a re-implementation of equality.
    dev = _FakeXvfDevice()
    _install_fake_xvf(monkeypatch, dev)
    monkeypatch.delenv("JASPER_AEC_CORPUS_CHIP_AEC_ENABLED", raising=False)
    monkeypatch.setenv("JASPER_AEC_CHIP_AEC_ENABLED", "1")
    plan = xvf3800.SQUARE_FIXED_150_210_PLAN
    identity = AlignmentIdentity(
        "xvf3800_legacy_square_6ch", "XVF3800-001", "firmware", plan.plan_id,
        xvf3800.chip_aec_fixed_profile_fingerprint(plan),
        "apple_usb_c_dongle", "usb-serial:DWH53530FLL2FN3A3",
        "single:outputd_dac", "S16_LE", 48_000, 2, 128, 384,
    )
    commissioned = replace(identity, output_format="S32_LE")
    # Format is the ONLY difference — everything else still matches live.
    assert commissioned != identity
    assert replace(commissioned, output_format="S16_LE") == identity
    monkeypatch.setattr(
        aec_init, "load_artifact", lambda: AlignmentArtifact(commissioned, 245)
    )
    monkeypatch.setattr(
        aec_init,
        "collect_reference_queue",
        lambda _pcm: ({"reference_outputs": {}}, (283,) * 8),
    )
    monkeypatch.setattr(
        aec_init, "build_identity", lambda *_args, **_kwargs: identity
    )

    assert aec_init.main() == aec_init.COMMISSION_REQUIRED_EXIT
    assert _write_map(dev) == {"SHF_BYPASS": [1]}


def test_build_identity_binds_physical_xvf_and_usb_output() -> None:
    dev = _FakeXvfDevice()
    dev.values["BLD_REPO_HASH"] = (ord("f"), ord("w"), 0)
    identity = aec_init.build_identity(
        dev,
        xvf3800.SQUARE_FIXED_150_210_PLAN,
        {
            "sink_mode": "single_alsa",
            "dac": {
                "pcm": "outputd_dac",
                "format": "S32_LE",
                "sample_rate": 48_000,
                "period_frames": 128,
                "buffer_frames": 256,
            },
        },
        env={
            "JASPER_XVF_VARIANT": "xvf3800_legacy_square_6ch",
            "JASPER_AUDIO_DAC_ID": "apple_usb_c_dongle",
            "JASPER_OUTPUTD_ACTIVE_CHANNELS": "",
        },
        output_state=_output_state(),
    )

    assert identity.output_channels == 2
    assert identity.xvf_serial == "XVF3800-001"
    assert identity.output_hardware_key == "usb-serial:DWH53530FLL2FN3A3"
    assert identity.output_format == "S32_LE"


def test_build_identity_refuses_status_without_a_final_edge_format() -> None:
    # An outputd too old to report dac.format must block, not be guessed
    # around: a default would certify an artifact against an unverified edge.
    dev = _FakeXvfDevice()
    dev.values["BLD_REPO_HASH"] = (ord("f"), ord("w"), 0)
    with pytest.raises(aec_init.ChipInitError) as excinfo:
        aec_init.build_identity(
            dev,
            xvf3800.SQUARE_FIXED_150_210_PLAN,
            {
                "sink_mode": "single_alsa",
                "dac": {
                    "pcm": "outputd_dac",
                    "sample_rate": 48_000,
                    "period_frames": 128,
                    "buffer_frames": 256,
                },
            },
            env={
                "JASPER_XVF_VARIANT": "xvf3800_legacy_square_6ch",
                "JASPER_AUDIO_DAC_ID": "apple_usb_c_dongle",
            },
            output_state=_output_state(),
        )

    assert "dac.format" in str(excinfo.value)


# A fixed epoch instant for the declaration's mtime. Absolute instants on ONE
# clock is the whole point of the guard's comparison, so the tests use literal
# instants rather than "N seconds ago" — nothing here reads a live clock.
_ENV_WRITTEN_AT = 1_786_127_844.0


def _outputd_env(tmp_path, monkeypatch, *, mtime: float = _ENV_WRITTEN_AT):
    """Point the guard at a declaration whose mtime is a fixed epoch instant."""
    path = tmp_path / "outputd.env"
    path.write_text("JASPER_OUTPUTD_DAC_FORMAT=S16_LE\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))
    monkeypatch.setenv("JASPER_OUTPUTD_ENV_FILE", str(path))
    return path


def _stub_start_instant(monkeypatch, instant: float | None) -> None:
    monkeypatch.setattr(
        aec_init, "outputd_main_start_realtime", lambda **_kwargs: instant
    )


def _stub_systemctl_show(monkeypatch, stdout: str, *, returncode: int = 0):
    calls: list[list[str]] = []

    def fake_run(args, **_kwargs):
        calls.append(list(args))
        return subprocess.CompletedProcess(
            args=args, returncode=returncode, stdout=stdout, stderr=""
        )

    monkeypatch.setattr(aec_init.subprocess, "run", fake_run)
    return calls


def test_outputd_start_instant_is_read_from_the_realtime_systemd_property(
    monkeypatch,
) -> None:
    # The property and the --timestamp=unix rendering are both load-bearing: the
    # REALTIME sibling is what shares a clock with the env file's mtime (the
    # monotonic one does not, and mixing them fails open under an NTP step), and
    # without --timestamp=unix systemd emits a locale-formatted string. Pin the
    # argv so either change cannot pass silently.
    #
    # Hardware ground truth for the rendering, probed on jts3 (full tier) and
    # jts4 (streambox tier), both systemd 257 (257.13-1~deb13u1):
    #
    #   RUNNING unit:
    #     systemctl show --timestamp=unix -p ExecMainStartTimestamp --value \
    #         jasper-outputd.service
    #     -> `@1786127844`, rc=0
    #   NEVER-RUN-THIS-BOOT unit: same command
    #     -> EMPTY output, rc=0
    calls = _stub_systemctl_show(monkeypatch, "@1786127844\n")
    monkeypatch.setenv("JASPER_SYSTEMCTL", "/fake/systemctl")

    assert aec_init.outputd_main_start_realtime() == pytest.approx(1_786_127_844.0)
    assert calls == [
        [
            "/fake/systemctl",
            "show",
            "--timestamp=unix",
            "-p",
            "ExecMainStartTimestamp",
            "--value",
            "jasper-outputd.service",
        ]
    ]


def test_outputd_start_instant_probe_is_bounded_per_call(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def fake_run(args, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout="@1786127844\n", stderr=""
        )

    monkeypatch.setattr(aec_init.subprocess, "run", fake_run)
    aec_init.outputd_main_start_realtime()

    assert seen["timeout"] == aec_init.SYSTEMCTL_QUERY_TIMEOUT_SEC


@pytest.mark.parametrize(
    "stdout,returncode",
    [
        # The probed never-run rendering on systemd 257: empty, rc=0.
        ("", 0),
        # Defensive only — systemd 257 prints empty, not a literal zero. Kept in
        # case another version renders it this way; same "nothing has run" case.
        ("@0\n", 0),
        ("0\n", 0),
        ("[not set]\n", 0),    # non-empty but unparseable
        ("@1786127844\n", 1),  # systemctl itself failed
    ],
)
def test_outputd_start_instant_is_unknown_rather_than_guessed(
    monkeypatch, stdout: str, returncode: int
) -> None:
    _stub_systemctl_show(monkeypatch, stdout, returncode=returncode)

    assert aec_init.outputd_main_start_realtime() is None


@pytest.mark.parametrize(
    "stdout,returncode,outcome",
    [
        ("[not set]\n", 0, "unparseable"),
        ("Unit not loaded.\n", 1, "systemctl_failed"),
    ],
)
def test_an_inert_ordering_guard_says_so_in_the_journal(
    monkeypatch, caplog, stdout: str, returncode: int, outcome: str
) -> None:
    # Failing open is defensible; failing open SILENTLY is not. These two paths
    # are anomalies rather than states, so an operator has to be able to see that
    # the guard did not actually run — otherwise it looks exactly like a pass.
    _stub_systemctl_show(monkeypatch, stdout, returncode=returncode)
    caplog.set_level("WARNING", logger="jasper.aec_init")

    assert aec_init.outputd_main_start_realtime() is None

    assert "event=chip_aec_init.ordering_probe" in caplog.text
    assert f"outcome={outcome}" in caplog.text
    assert "ordering guard is inert" in caplog.text


@pytest.mark.parametrize("stdout", ["", "@0\n"])
def test_a_unit_that_never_ran_is_quiet(monkeypatch, caplog, stdout: str) -> None:
    # The legitimate case stays out of the journal: nothing has started, so there
    # is nothing anomalous to report and a WARN would be noise on every box that
    # boots with outputd gated off.
    _stub_systemctl_show(monkeypatch, stdout)
    caplog.set_level("WARNING", logger="jasper.aec_init")

    assert aec_init.outputd_main_start_realtime() is None

    assert "ordering_probe" not in caplog.text


def test_outputd_start_instant_survives_a_host_without_systemctl(
    monkeypatch, caplog
) -> None:
    def fake_run(_args, **_kwargs):
        raise FileNotFoundError("systemctl")

    monkeypatch.setattr(aec_init.subprocess, "run", fake_run)
    caplog.set_level("WARNING", logger="jasper.aec_init")

    assert aec_init.outputd_main_start_realtime() is None
    # Not a systemd host at all — the daemon could never run here, so this is not
    # the inert-guard anomaly the WARN exists for.
    assert "ordering_probe" not in caplog.text


def test_staleness_fails_closed_when_the_systemctl_probe_times_out(
    tmp_path, monkeypatch
) -> None:
    # A wedged systemd is not evidence that outputd is current, so the verdict is
    # stale — with its own reason naming the timeout rather than pretending to
    # know the process instant.
    _outputd_env(tmp_path, monkeypatch)

    def fake_run(args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=5.0)

    monkeypatch.setattr(aec_init.subprocess, "run", fake_run)

    reason = aec_init.outputd_env_staleness()

    assert "did not answer within" in reason
    assert "cannot be confirmed" in reason
    assert "has not loaded the current output declaration" not in reason


def test_staleness_reports_an_outputd_older_than_its_own_declaration(
    tmp_path, monkeypatch
) -> None:
    path = _outputd_env(tmp_path, monkeypatch)
    _stub_start_instant(monkeypatch, _ENV_WRITTEN_AT - 10.0)

    reason = aec_init.outputd_env_staleness()

    assert str(path) in reason
    assert "has not loaded the current output declaration" in reason
    assert "jasper-outputd.service started 10.0s before" in reason


def test_staleness_accepts_an_outputd_started_after_its_declaration(
    tmp_path, monkeypatch
) -> None:
    _outputd_env(tmp_path, monkeypatch)
    _stub_start_instant(monkeypatch, _ENV_WRITTEN_AT + 2.0)

    assert aec_init.outputd_env_staleness() == ""


def test_staleness_fails_closed_when_the_two_instants_tie(
    tmp_path, monkeypatch
) -> None:
    # Direction of the boundary, chosen deliberately: a declaration stamped at
    # exactly the recorded start instant reads as STALE. systemd reads
    # EnvironmentFile= just before forking the main process, so a write that ties
    # the fork instant landed after the read — and a tie is not proof of loading
    # either way. --timestamp=unix also truncates the start to whole seconds, so
    # ties are reachable in the field, not just here.
    _outputd_env(tmp_path, monkeypatch)
    _stub_start_instant(monkeypatch, _ENV_WRITTEN_AT)

    assert "has not loaded the current output declaration" in (
        aec_init.outputd_env_staleness()
    )


@pytest.mark.parametrize(
    "now_realtime,now_monotonic",
    [
        (_ENV_WRITTEN_AT + 1.0, 1_000.0),        # moments later
        (_ENV_WRITTEN_AT + 86_400.0, 2_000.0),   # a day of uptime
        (_ENV_WRITTEN_AT + 3_600.0, 5.0),        # forward NTP step: realtime
        (1_000.0, 900.0),                        # jumped ahead of monotonic
        (_ENV_WRITTEN_AT - 3_600.0, 900.0),      # backward step
    ],
)
def test_staleness_verdict_never_depends_on_the_current_clock(
    tmp_path, monkeypatch, now_realtime: float, now_monotonic: float
) -> None:
    # The SF2 regression. The first implementation compared a CLOCK_REALTIME age
    # against a CLOCK_MONOTONIC age, so a forward realtime step — routine on an
    # RTC-less Pi at boot — made the guard FAIL OPEN and certify exactly the
    # stale outputd the invariant forbids. Comparing recorded INSTANTS on one
    # clock means "now" is not an input at all: the same (start, mtime) pair must
    # yield the same verdict no matter what either clock currently says.
    _outputd_env(tmp_path, monkeypatch)
    _stub_start_instant(monkeypatch, _ENV_WRITTEN_AT - 10.0)
    monkeypatch.setattr(
        aec_init,
        "time",
        types.SimpleNamespace(
            monotonic=lambda: now_monotonic,
            time=lambda: now_realtime,
            sleep=lambda _s: None,
        ),
    )

    assert "has not loaded the current output declaration" in (
        aec_init.outputd_env_staleness()
    )


def test_staleness_survives_a_forward_step_landing_after_the_env_write(
    tmp_path, monkeypatch
) -> None:
    # The gate's concrete scenario, recast for instants: outputd starts, the
    # declaration is written after it (so outputd is genuinely stale), and only
    # THEN does NTP step the clock forward. Neither recorded instant moves — the
    # start is already stamped by systemd and the mtime is already on the file —
    # so the stale verdict has to survive the step.
    _outputd_env(tmp_path, monkeypatch)
    _stub_start_instant(monkeypatch, _ENV_WRITTEN_AT - 30.0)
    stepped = _ENV_WRITTEN_AT + 30 * 86_400.0
    monkeypatch.setattr(
        aec_init,
        "time",
        types.SimpleNamespace(
            monotonic=lambda: 12.0, time=lambda: stepped, sleep=lambda _s: None
        ),
    )

    assert aec_init.outputd_env_staleness() != ""


def test_staleness_is_inert_without_a_declaration_on_this_box(
    tmp_path, monkeypatch
) -> None:
    # A box with no outputd.env has nothing to be stale against, and the check
    # must cost nothing there — no subprocess at all.
    monkeypatch.setenv("JASPER_OUTPUTD_ENV_FILE", str(tmp_path / "absent.env"))
    calls = _stub_systemctl_show(monkeypatch, "@1786127844\n")

    assert aec_init.outputd_env_staleness() == ""
    assert calls == []


def test_staleness_is_inert_when_outputd_never_started_this_boot(
    tmp_path, monkeypatch
) -> None:
    # An absent start instant means only: never started this boot, unparseable,
    # or systemctl failed. It does NOT mean "stopped" — systemd RETAINS
    # ExecMainStartTimestamp after a unit stops, so a stopped outputd that ran
    # this boot still reports its old instant and is caught by the stale branch
    # (see the test below). Nothing has run, so there is nothing to be stale
    # against, and collect_reference_queue's writer-not-ready path owns the
    # diagnosis.
    _outputd_env(tmp_path, monkeypatch)
    _stub_start_instant(monkeypatch, None)

    assert aec_init.outputd_env_staleness() == ""


def test_staleness_catches_a_stopped_outputd_that_ran_before_the_env_write(
    tmp_path, monkeypatch
) -> None:
    # NIT4 from the gate, probed on jts3: systemd keeps ExecMainStartTimestamp
    # after a unit stops (an inactive oneshot that ran this boot still reports a
    # non-zero instant; only a never-run unit reports zero). So a stopped outputd
    # whose last run predates the declaration reads as STALE, not as unknown.
    # That is stricter than passing it through, and it is the safe direction.
    _outputd_env(tmp_path, monkeypatch)
    _stub_start_instant(monkeypatch, _ENV_WRITTEN_AT - 600.0)

    assert "has not loaded the current output declaration" in (
        aec_init.outputd_env_staleness()
    )


def test_require_outputd_env_loaded_hands_the_staleness_reason_to_the_caller(
    monkeypatch,
) -> None:
    # main() logs str(exc) as the park reason and the reconciler surfaces it on
    # /state, so the diagnosis must survive the raise rather than be replaced by
    # a generic message.
    monkeypatch.setattr(
        aec_init,
        "outputd_env_staleness",
        lambda _env=None: "jasper-outputd.service started 4.0s before /x.env",
    )

    with pytest.raises(aec_init.OutputdEnvStale) as excinfo:
        aec_init.require_outputd_env_loaded(timeout=0.0)

    assert str(excinfo.value) == (
        "jasper-outputd.service started 4.0s before /x.env"
    )


def test_require_outputd_env_loaded_returns_when_the_restart_lands(
    monkeypatch,
) -> None:
    # The bounded wait exists so the ordinary race — a queued outputd restart
    # that has not run yet — resolves itself instead of parking the speaker.
    verdicts = ["outputd is stale", "outputd is stale", ""]
    monkeypatch.setattr(
        aec_init, "outputd_env_staleness", lambda _env=None: verdicts.pop(0)
    )

    aec_init.require_outputd_env_loaded(timeout=30.0, interval=0.0)

    assert verdicts == []


def test_require_outputd_env_loaded_is_bounded_and_does_not_spin_forever(
    monkeypatch,
) -> None:
    polls = 0

    def always_stale(_env=None):
        nonlocal polls
        polls += 1
        return "outputd is stale"

    monkeypatch.setattr(aec_init, "outputd_env_staleness", always_stale)

    with pytest.raises(aec_init.OutputdEnvStale):
        aec_init.require_outputd_env_loaded(timeout=0.05, interval=0.01)

    assert 0 < polls < 100


def test_collect_reference_queue_checks_ordering_before_reading_status(
    monkeypatch,
) -> None:
    # The guard has to run before the poll: raised from inside it, an
    # OutputdEnvStale would be swallowed into last_error by the loop's own
    # ChipInitError handler and surface as a generic fault.
    reads: list[str] = []
    monkeypatch.setattr(
        aec_init, "read_status_socket", lambda path: reads.append(path) or {}
    )
    monkeypatch.setattr(
        aec_init,
        "require_outputd_env_loaded",
        lambda **_kwargs: (_ for _ in ()).throw(aec_init.OutputdEnvStale("stale")),
    )

    with pytest.raises(aec_init.OutputdEnvStale):
        aec_init.collect_reference_queue("hw:CARD=Array,DEV=0")

    assert reads == []


def test_production_chip_profile_defers_when_outputd_predates_its_declaration(
    tmp_path, monkeypatch, caplog
) -> None:
    # End to end through main() with the REAL guard: stat, comparison, raise,
    # propagation out of collect_reference_queue, exit code, and event line.
    dev = _FakeXvfDevice()
    _install_fake_xvf(monkeypatch, dev)
    monkeypatch.delenv("JASPER_AEC_CORPUS_CHIP_AEC_ENABLED", raising=False)
    monkeypatch.setenv("JASPER_AEC_CHIP_AEC_ENABLED", "1")
    plan = xvf3800.SQUARE_FIXED_150_210_PLAN
    identity = AlignmentIdentity(
        "xvf3800_legacy_square_6ch", "XVF3800-001", "firmware", plan.plan_id,
        xvf3800.chip_aec_fixed_profile_fingerprint(plan),
        "apple_usb_c_dongle", "usb-serial:DWH53530FLL2FN3A3",
        "single:outputd_dac", "S16_LE", 48_000, 2, 128, 384,
    )
    monkeypatch.setattr(
        aec_init, "load_artifact", lambda: AlignmentArtifact(identity, 245)
    )
    _outputd_env(tmp_path, monkeypatch)
    _stub_start_instant(monkeypatch, _ENV_WRITTEN_AT - 20.0)
    reads: list[str] = []
    monkeypatch.setattr(
        aec_init, "read_status_socket", lambda path: reads.append(path) or {}
    )
    real_guard = aec_init.require_outputd_env_loaded
    monkeypatch.setattr(
        aec_init,
        "require_outputd_env_loaded",
        lambda **kwargs: real_guard(timeout=0.0, **kwargs),
    )
    caplog.set_level("ERROR", logger="jasper.aec_init")

    assert aec_init.main() == aec_init.OUTPUTD_ENV_STALE_EXIT

    # Chip left bypassed, no profile armed, and STATUS never consulted.
    assert _write_map(dev) == {"SHF_BYPASS": [1]}
    assert reads == []
    assert "outcome=deferred" in caplog.text
    assert "has not loaded the current output declaration" in caplog.text
    assert "action=" in caplog.text
    assert "jasper-aec-commission" not in caplog.text


@pytest.mark.parametrize(
    "unit_name",
    ["jasper-aec-reconcile.service", "jasper-aec-init.service"],
)
def test_aec_units_do_not_order_against_time_sync(unit_name: str) -> None:
    # An earlier revision ordered these two After=time-sync.target to "close" the
    # clock-step residual. It cannot: the verdict is a pure function of two stamps
    # already written by OTHER processes (systemd for the start,
    # jasper-audio-hardware-reconcile for the mtime), so ordering the READERS
    # provably changes nothing. Keep it out rather than let a plausible-sounding
    # dependency drift back in and couple mic reconcile to time infrastructure.
    unit = (ROOT / "deploy" / "systemd" / unit_name).read_text(encoding="utf-8")

    assert "time-sync.target" not in unit, unit_name


def test_guard_watches_the_same_env_file_the_outputd_unit_loads() -> None:
    # The guard fails OPEN on an unreadable path (a box with no declaration has
    # nothing to be stale against), so a drift between the path it stats and the
    # one systemd actually loads would silently disable it with every test still
    # green. Pin the pair.
    unit = (
        ROOT / "deploy" / "systemd" / "jasper-outputd.service"
    ).read_text(encoding="utf-8")

    assert f"EnvironmentFile=-{aec_init.DEFAULT_OUTPUTD_ENV_PATH}\n" in unit


def test_settle_wait_fits_inside_the_calling_reconcilers_start_budget() -> None:
    # SF3 from the gate. aec-init's own DefaultTimeoutStartSec is NOT the
    # operative limit: activate_managed_chip_aec runs `systemctl restart
    # jasper-aec-init.service` blocking, so everything aec-init does is spent
    # inside jasper-aec-reconcile.service's TimeoutStartSec. A SIGTERM there is
    # not a clean park — the script has no trap, so the box is left deaf with the
    # alignment status stuck at `checking`. Pin the derivation, not just the
    # numbers, so raising either bound forces a re-derivation.
    unit = (
        ROOT / "deploy" / "systemd" / "jasper-aec-reconcile.service"
    ).read_text(encoding="utf-8")
    budget = next(
        float(line.split("=", 1)[1])
        for line in unit.splitlines()
        if line.startswith("TimeoutStartSec=")
    )

    find_device_worst = 10.0        # _find_device: 10 attempts, 1 s apart
    queue_worst = 30.0              # collect_reference_queue's default timeout
    # The settle deadline is checked after a poll, so one systemctl probe can
    # overrun it.
    settle_worst = aec_init.OUTPUTD_ENV_SETTLE_SEC + aec_init.SYSTEMCTL_QUERY_TIMEOUT_SEC
    inner_worst = find_device_worst + settle_worst + queue_worst

    assert inner_worst == 55.0, "re-derive the budget comment if this moves"
    # Real headroom for the rest of the reconciler (mic-profile and chip-AEC
    # policy subprocesses, env writes, three service restarts).
    assert budget - inner_worst >= 60.0


def test_deferred_and_commission_required_exits_stay_distinct() -> None:
    # The reconciler branches on these two integers to pick between "wait for
    # outputd" and "send a human to the commissioner"; collapsing them would
    # send households at a two-minute sweep session for a scheduling race.
    assert aec_init.OUTPUTD_ENV_STALE_EXIT != aec_init.COMMISSION_REQUIRED_EXIT
    assert aec_init.OUTPUTD_ENV_STALE_EXIT not in (0, 1)


def test_output_hardware_key_uses_deterministic_i2s_profile_and_card() -> None:
    assert aec_init.output_hardware_key(
        {"JASPER_AUDIO_DAC_ID": "hifiberry_dac8x"},
        _output_state(profile_id="hifiberry_dac8x", card_id="DAC8X", serial=None),
    ) == "i2s:hifiberry_dac8x:DAC8X"


def test_chip_profile_refuses_linear_geometry_without_beam_plan(monkeypatch) -> None:
    dev = _FakeXvfDevice()
    _install_fake_xvf(monkeypatch, dev)
    amixer_calls = _stub_amixer(monkeypatch)
    monkeypatch.setenv("JASPER_AEC_CHIP_AEC_ENABLED", "1")
    monkeypatch.setenv("JASPER_XVF_GEOMETRY", "linear")
    monkeypatch.delenv("JASPER_XVF_CHIP_BEAM_PLAN", raising=False)

    assert aec_init.main() == 1
    assert "AEC_FIXEDBEAMSAZIMUTH_VALUES" not in _write_map(dev)
    assert amixer_calls == []
    assert dev.dev.closed is True


def test_corpus_profile_fails_when_critical_write_fails(monkeypatch) -> None:
    dev = _FakeXvfDevice(fail_on={"AUDIO_MGR_OP_L"})
    _install_fake_xvf(monkeypatch, dev)
    amixer_calls = _stub_amixer(monkeypatch)
    monkeypatch.setenv("JASPER_AEC_CORPUS_CHIP_AEC_ENABLED", "1")

    assert aec_init.main() == 1
    assert "AUDIO_MGR_OP_L" not in _write_map(dev)
    assert amixer_calls == []
    assert dev.dev.closed is True


def test_corpus_profile_fails_when_readback_does_not_match(monkeypatch) -> None:
    dev = _FakeXvfDevice(readback_overrides={"AUDIO_MGR_OP_R": [0, 0]})
    _install_fake_xvf(monkeypatch, dev)
    amixer_calls = _stub_amixer(monkeypatch)
    monkeypatch.setenv("JASPER_AEC_CORPUS_CHIP_AEC_ENABLED", "1")

    assert aec_init.main() == 1
    assert _write_map(dev)["SHF_BYPASS"] == [1]
    assert amixer_calls == []
    assert dev.dev.closed is True


def test_init_reports_missing_xvf_control_dependency(monkeypatch, caplog) -> None:
    class MissingXvfControlError(RuntimeError):
        pass

    def fail_find():
        raise MissingXvfControlError("XVF3800 USB control dependencies missing")

    fake_xvf = types.ModuleType("jasper.xvf")
    fake_xvf.xvf_host = types.SimpleNamespace(
        find=fail_find,
        XvfControlError=MissingXvfControlError,
    )
    monkeypatch.setitem(sys.modules, "jasper.xvf", fake_xvf)
    caplog.set_level("ERROR", logger="jasper.aec_init")

    assert aec_init.main() == 1

    assert "event=chip_aec_init" in caplog.text
    assert "dependencies missing" in caplog.text


# ---------------------------------------------------------------------------
# Cadence-aware chip-reference queue collection (#2253)
#
# The pre-#2253 rule — eight consecutive readings, every one at
# reference_sequence_lag == 0, spread <= 16 — encoded a 128-frame mix period's
# timing as if it were universal.  jts.local (period 128, 2.7 ms cadence) met it;
# jts3 (period 1024, 21.3 ms cadence, identical chip-ref geometry) could not, so
# commissioning AND every boot of jasper-aec-init parked the box.  The two
# fixtures below are built to the spread, lag rate, and geometry each box
# measured on 2026-08-08 — that shape is the measurement; the individual
# readings inside it are constructed to carry it.
# ---------------------------------------------------------------------------

REFERENCE_PCM = "hw:CARD=Array,DEV=0"
DAC_RATE = 48_000


class _FakeClock:
    """A monotonic clock the poll loop advances itself, so tests never sleep."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 0.0)


class _WriterStream:
    """outputd STATUS from a box whose chip-ref writer runs at one mix cadence.

    One completed write per mix period is the real contract — outputd enqueues
    one packet per period and the writer records `snd_pcm_delay` when that
    packet's write returns — so how many distinct readings exist is a function of
    elapsed time, not of how often the poll asks.  Reading faster than the cadence
    returns the same write again, which is the case the collector has to tell
    apart from a stall.
    """

    def __init__(
        self,
        clock: _FakeClock,
        *,
        mix_period_frames: int,
        delays,
        lags=(0,),
        stall_after: int | None = None,
    ) -> None:
        self.clock = clock
        self.mix_period_frames = mix_period_frames
        self.period_seconds = mix_period_frames / DAC_RATE
        self.burst = round(
            mix_period_frames * xvf3800.CHIP_AEC_REFERENCE_SAMPLE_RATE_HZ / DAC_RATE
        )
        self.delays = tuple(delays)
        self.lags = tuple(lags)
        self.stall_after = stall_after
        self.reads = 0

    def _writes_completed(self) -> int:
        elapsed = int(self.clock.now / self.period_seconds) + 1
        return elapsed if self.stall_after is None else min(elapsed, self.stall_after)

    def __call__(self, _path: str) -> dict:
        self.reads += 1
        writes = self._writes_completed()
        age_ms = int((self.clock.now - (writes - 1) * self.period_seconds) * 1_000)
        writer = {
            "desired": True,
            "active": True,
            "status": "active",
            "reference_sequence_lag": self.lags[(writes - 1) % len(self.lags)],
            "snd_pcm_delay_sample_age_ms": age_ms,
            "last_write_age_ms": age_ms,
            "snd_pcm_delay_frames": self.delays[(writes - 1) % len(self.delays)],
            "frames_written": writes * self.burst,
        }
        writer.update(dict.fromkeys(aec_init.REFERENCE_WRITER_COUNTER_NAMES, 0))
        return {
            "backend": "alsa",
            "sink_mode": "single",
            "dac": {
                "pcm": "hw:CARD=DAC8x,DEV=0",
                "format": "S32_LE",
                "sample_rate": DAC_RATE,
                "period_frames": self.mix_period_frames,
                "buffer_frames": self.mix_period_frames * 3,
            },
            "mix": {"reference_sequence": writes},
            "reference_outputs": {
                "speaker_reference_source": "outputd_final_electrical",
                "speaker_reference_is_fallback": False,
                "speaker_reference_channels": xvf3800.CHIP_AEC_REFERENCE_CHANNELS,
                "chip_ref_pcm": REFERENCE_PCM,
                "chip_ref_sample_rate": xvf3800.CHIP_AEC_REFERENCE_SAMPLE_RATE_HZ,
                "chip_ref_period_frames": xvf3800.CHIP_AEC_REFERENCE_PERIOD_FRAMES,
                "chip_ref_buffer_frames": xvf3800.CHIP_AEC_REFERENCE_BUFFER_FRAMES,
                "chip_ref_transform": xvf3800.CHIP_AEC_REFERENCE_TRANSFORM,
                "chip_ref_writer": writer,
            },
        }


# jts.local measured: 128-frame mix period, lag 0 on 24 of 24 reads, readings
# spanning 11 frames around the commissioned median of 285.
FINE_CADENCE_DELAYS = (285, 284, 286, 290, 279, 288, 283, 287, 281, 285)
# jts3 measured: 1024-frame mix period, lag 1 on 10 of 24 reads, readings
# spanning 86 frames (362..448).  A 342-frame burst does not fit the writer's
# 256-frame ALSA ring, so every write blocks and completes wherever scheduling
# slack leaves it — which is why the readings sit near the top with a tail.
COARSE_CADENCE_DELAYS = (448, 445, 448, 362, 447, 401, 448, 430, 448, 419)
COARSE_CADENCE_LAGS = (0, 1, 0, 1, 0, 0, 1, 0, 1, 0)


def _install_queue_stream(monkeypatch, stream: _WriterStream, clock: _FakeClock) -> None:
    monkeypatch.setattr(aec_init, "read_status_socket", stream)
    monkeypatch.setattr(aec_init, "require_outputd_env_loaded", lambda **_kwargs: None)
    monkeypatch.setattr(aec_init, "time", clock)


def test_fine_cadence_box_still_settles_in_the_reference_window(monkeypatch) -> None:
    # jts.local's numbers. Widening the rule for coarse cadences must not make
    # the fine one slower or looser than the window K was originally measured
    # with: same 2 s of holding still, same spread, more readings inside it.
    clock = _FakeClock()
    stream = _WriterStream(clock, mix_period_frames=128, delays=FINE_CADENCE_DELAYS)
    _install_queue_stream(monkeypatch, stream, clock)

    _status, queue = aec_init.collect_reference_queue(REFERENCE_PCM)

    assert len(queue) >= alignment.QUEUE_SAMPLE_COUNT
    assert max(queue) - min(queue) <= alignment.QUEUE_MAX_SPREAD
    assert aec_init.QUEUE_MIN_WINDOW_SEC <= clock.now < aec_init.QUEUE_MIN_WINDOW_SEC + 1


def test_coarse_cadence_box_settles_inside_the_collection_budget(monkeypatch) -> None:
    # The #2253 regression, as measured on jts3: two commission attempts and
    # every boot failed here. Steady-state pipelining (lag 1) and a spread the
    # blocking write cannot avoid must both be admissible, and the window has to
    # close well inside the 30 s budget aec-init's caller allows for it.
    clock = _FakeClock()
    stream = _WriterStream(
        clock,
        mix_period_frames=1024,
        delays=COARSE_CADENCE_DELAYS,
        lags=COARSE_CADENCE_LAGS,
    )
    _install_queue_stream(monkeypatch, stream, clock)

    _status, queue = aec_init.collect_reference_queue(REFERENCE_PCM)

    spread = max(queue) - min(queue)
    assert spread == 86, "the fixture carries jts3's measured spread"
    assert spread > alignment.QUEUE_MAX_SPREAD, "the old rule rejected this outright"
    assert 1 in COARSE_CADENCE_LAGS, "the old rule rejected a lag of one outright"
    # The precision rule, not the raw spread, is what the window is held to.
    assert len(queue) == alignment.required_queue_samples(spread)
    assert clock.now < 30.0


def test_the_collected_window_is_one_runtime_sys_delay_accepts(monkeypatch) -> None:
    # Two consumers, one definition of a usable window: whatever the collector
    # hands back at boot goes straight into runtime_sys_delay, so an acceptance
    # rule looser than the re-validation would park every coarse-cadence box with
    # "queue is unstable" immediately after the poll passed.
    clock = _FakeClock()
    stream = _WriterStream(
        clock,
        mix_period_frames=1024,
        delays=COARSE_CADENCE_DELAYS,
        lags=COARSE_CADENCE_LAGS,
    )
    _install_queue_stream(monkeypatch, stream, clock)

    _status, queue = aec_init.collect_reference_queue(REFERENCE_PCM)

    assert alignment.runtime_sys_delay(400, queue) == 400 - alignment.median_samples(
        queue
    )


def test_a_stalled_writer_still_fails_loudly(monkeypatch) -> None:
    # The liveness the removed lag == 0 gate was doing no work for: a writer that
    # stops writing repeats its last reading forever. Repeats are no longer
    # progress evidence, so the wall-clock ages are what have to fail it.
    clock = _FakeClock()
    stream = _WriterStream(
        clock,
        mix_period_frames=1024,
        delays=COARSE_CADENCE_DELAYS,
        lags=COARSE_CADENCE_LAGS,
        stall_after=4,
    )
    _install_queue_stream(monkeypatch, stream, clock)

    with pytest.raises(aec_init.ChipInitError) as excinfo:
        aec_init.collect_reference_queue(REFERENCE_PCM)

    assert "not ready" in str(excinfo.value)
    assert "age_ms is stale" in str(excinfo.value)
    assert clock.now >= 30.0, "a stall consumes the budget rather than short-circuiting"


def test_a_queue_that_never_holds_still_fails_with_its_own_numbers(monkeypatch) -> None:
    # A reference queue whose spread keeps growing cannot carry K at any sample
    # count, and the operator needs the shortfall rather than "not current and
    # active". A reading that ramps every write is the drift shape.
    clock = _FakeClock()
    stream = _WriterStream(
        clock, mix_period_frames=1024, delays=tuple(range(300, 300 + 4_096))
    )
    _install_queue_stream(monkeypatch, stream, clock)

    with pytest.raises(aec_init.ChipInitError) as excinfo:
        aec_init.collect_reference_queue(REFERENCE_PCM)

    reason = str(excinfo.value)
    assert "chip-reference queue spread" in reason
    assert "readings over" in reason and "held" in reason


class _PerturbedStream:
    """A `_WriterStream` whose STATUS is rewritten from the Nth read onward."""

    def __init__(self, stream: _WriterStream, *, after: int, mutate) -> None:
        self.stream = stream
        self.after = after
        self.mutate = mutate

    def __call__(self, path: str) -> dict:
        status = self.stream(path)
        if self.stream.reads > self.after:
            self.mutate(status, self.stream.reads)
        return status


def _writer_of(status: dict) -> dict:
    return status["reference_outputs"]["chip_ref_writer"]


def _collect_perturbed(monkeypatch, *, after: int, mutate, timeout: float) -> str:
    """Run one perturbed collection and return the reason it refused.

    A 1024-frame-period stream is polled at 10.67 ms and writes every 21.33 ms,
    so reads alternate new-write, repeat, new-write.  ``after`` picks which read
    first carries the perturbation and ``timeout`` ends the budget on the poll
    right after it, which is what makes the raised reason that comparison's own
    rather than whatever the loop last said while rebuilding a window around the
    perturbed readings.
    """

    clock = _FakeClock()
    stream = _WriterStream(clock, mix_period_frames=1024, delays=(448,))
    _install_queue_stream(
        monkeypatch, _PerturbedStream(stream, after=after, mutate=mutate), clock
    )
    with pytest.raises(aec_init.ChipInitError) as excinfo:
        aec_init.collect_reference_queue(REFERENCE_PCM, timeout=timeout)
    return str(excinfo.value)


def test_an_unperturbed_stream_closes_a_window(monkeypatch) -> None:
    # The control for the two perturbation tests below: everything they hold
    # constant is already enough to close a window, so the perturbation is the
    # only thing their failure can be attributed to.
    clock = _FakeClock()
    stream = _WriterStream(clock, mix_period_frames=1024, delays=(448,))
    _install_queue_stream(monkeypatch, stream, clock)

    _status, queue = aec_init.collect_reference_queue(REFERENCE_PCM)

    assert len(queue) >= alignment.QUEUE_SAMPLE_COUNT


def test_moving_error_counters_are_not_one_window(monkeypatch) -> None:
    # An xrun, a reopen, or a dropped period splits the readings on either side
    # of it into two different pipeline states; a median taken across the seam is
    # one no instant ever held.
    def tick(status: dict, reads: int) -> None:
        _writer_of(status)["write_xrun_count"] = reads

    reason = _collect_perturbed(monkeypatch, after=2, mutate=tick, timeout=0.03)

    assert "error counters moved" in reason


def test_a_writer_whose_frames_go_backwards_is_not_one_window(monkeypatch) -> None:
    # outputd restarting under the poll reopens the PCM and restarts the writer's
    # frame counter, so readings from before and after belong to different opens.
    # Saying so beats letting the window quietly stop growing until the budget
    # runs out.
    def rewind(status: dict, _reads: int) -> None:
        _writer_of(status)["frames_written"] = 1

    reason = _collect_perturbed(monkeypatch, after=2, mutate=rewind, timeout=0.03)

    assert "did not progress cleanly" in reason


def test_reading_faster_than_the_writer_writes_is_not_a_sample(monkeypatch) -> None:
    # The poll runs at half the mix cadence, so about every other read repeats
    # the write already recorded. Counting a repeat would let a window
    # "stabilise" on a single reading; treating one as a stall would fail every
    # coarse-cadence box.
    clock = _FakeClock()
    stream = _WriterStream(clock, mix_period_frames=1024, delays=(400,))
    _install_queue_stream(monkeypatch, stream, clock)

    _status, queue = aec_init.collect_reference_queue(REFERENCE_PCM)

    assert len(queue) >= alignment.QUEUE_SAMPLE_COUNT
    assert stream.reads > len(queue), "the repeats were read and not counted"
    # One reading per mix period is the ceiling; a counted repeat would let the
    # window close in less wall time than that many writes can take.
    assert clock.now >= (len(queue) - 1) * 1024 / DAC_RATE


def test_the_poll_interval_comes_from_the_snapshots_own_mix_cadence() -> None:
    # Cadence-aware means read out of the STATUS being sampled. A fixed 0.25 s
    # poll is 11.7 mix periods on jts3 — it cannot collect the readings a wider
    # spread needs inside the budget.
    clock = _FakeClock()
    fine = _WriterStream(clock, mix_period_frames=128, delays=(285,))(REFERENCE_PCM)
    coarse = _WriterStream(clock, mix_period_frames=1024, delays=(448,))(REFERENCE_PCM)

    assert aec_init.mix_period_seconds(coarse) == 1024 / DAC_RATE
    assert aec_init.queue_poll_interval(coarse, 0.25) == 1024 / DAC_RATE / 2
    # Half of a 128-frame period is 1.3 ms; the floor keeps the poll off a spin.
    assert (
        aec_init.queue_poll_interval(fine, 0.25) == aec_init.MIN_QUEUE_POLL_INTERVAL_SEC
    )
    assert aec_init.MIN_QUEUE_POLL_INTERVAL_SEC < 1024 / DAC_RATE / 2, (
        "the floor must not clamp jts3's cadence, or writes get skipped"
    )
    # No cadence in the snapshot means no derivation — keep the caller's value.
    assert aec_init.queue_poll_interval({}, 0.25) == 0.25
    assert aec_init.mix_period_seconds({"dac": {"period_frames": 0}}) is None


def test_a_writer_further_behind_than_the_pipelining_ceiling_is_rejected() -> None:
    # Two periods is the ceiling: one being written, one published before the
    # writer dequeued it. Deeper means the writer is losing ground on a producer
    # it is rate-locked to.
    clock = _FakeClock()
    behind = _WriterStream(
        clock,
        mix_period_frames=1024,
        delays=(448,),
        lags=(aec_init.MAX_REFERENCE_PERIODS_IN_FLIGHT + 1,),
    )(REFERENCE_PCM)

    with pytest.raises(aec_init.ChipInitError, match="mix periods behind"):
        aec_init.validate_reference_status(behind, expected_pcm=REFERENCE_PCM)

    at_ceiling = _WriterStream(
        clock,
        mix_period_frames=1024,
        delays=(448,),
        lags=(aec_init.MAX_REFERENCE_PERIODS_IN_FLIGHT,),
    )(REFERENCE_PCM)

    assert (
        aec_init.validate_reference_status(at_ceiling, expected_pcm=REFERENCE_PCM).delay
        == 448
    )
