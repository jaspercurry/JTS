# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import contextlib
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import types
from dataclasses import replace
from pathlib import Path

import pytest

from jasper.chip_aec import alignment as alignment
from jasper.chip_aec import shipped as shipped_alignment
from jasper.chip_aec import health as chip_aec_health
from jasper import output_hardware
from jasper.chip_aec.alignment import AlignmentArtifact, AlignmentIdentity
from jasper.chip_aec.health import AlignmentHealth, alignment_health
from jasper.cli import aec_init
from jasper.env_load import parse_env_file
from jasper.mics import xvf3800
from tests._log_events import event_fields, event_records
from tests._socket_paths import short_socket_path_fixture as _short_sock_path_fixture

_IMPORTED_FIXTURES = (_short_sock_path_fixture,)


ROOT = Path(__file__).resolve().parents[1]


_SELECTION = "xvf_chip_aec"


@pytest.fixture(autouse=True)
def alignment_record(tmp_path, monkeypatch) -> Path:
    """Keep the published record off the host's /run for every test here, and
    give every run one mode-file selection to stamp it with."""
    mode_file = tmp_path / "aec_mode.env"
    mode_file.write_text(
        f"JASPER_AUDIO_INPUT_PROFILE={_SELECTION}\n", encoding="utf-8"
    )
    monkeypatch.setenv("JASPER_AEC_MODE_FILE", str(mode_file))
    path = tmp_path / "alignment"
    monkeypatch.setenv("JASPER_AEC_ALIGNMENT_RECORD_FILE", str(path))
    return path


def _published(path: Path) -> AlignmentHealth:
    """The record this run left for jasper-aec-reconcile."""
    return AlignmentHealth.from_env(parse_env_file(str(path)))


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


def test_production_chip_profile_uses_chip_flag_and_delay(
    monkeypatch, alignment_record
) -> None:
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
        aec_init, "load_artifact", lambda: AlignmentArtifact(identity, 245, -38)
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
    # An alignment that matched has nothing to disclose, so the record the
    # reconciler copies into jasper.env is `ready`.
    assert _published(alignment_record) == alignment_health(
        chip_aec_health.APPLIED, selection=_SELECTION
    )


def test_production_chip_profile_parks_when_nothing_is_banked_or_shipped(
    monkeypatch,
) -> None:
    # No artifact and an empty shipped table: there is no K to run from, so the
    # commissioner is still the answer — and the box does not spend the
    # reference-queue budget discovering that a table with no rows cannot
    # match it.
    dev = _FakeXvfDevice()
    _install_fake_xvf(monkeypatch, dev)
    monkeypatch.delenv("JASPER_AEC_CORPUS_CHIP_AEC_ENABLED", raising=False)
    monkeypatch.setenv("JASPER_AEC_CHIP_AEC_ENABLED", "1")
    monkeypatch.setattr(
        aec_init,
        "load_artifact",
        lambda: (_ for _ in ()).throw(ValueError("missing")),
    )
    monkeypatch.setattr(shipped_alignment, "REGISTRY", ())
    monkeypatch.setattr(
        aec_init,
        "collect_reference_queue",
        lambda _pcm: pytest.fail("collected the queue with nothing to match"),
    )

    assert aec_init.main() == aec_init.COMMISSION_REQUIRED_EXIT
    assert _write_map(dev)["SHF_BYPASS"] == [1]


def _live_identity() -> AlignmentIdentity:
    plan = xvf3800.SQUARE_FIXED_150_210_PLAN
    return AlignmentIdentity(
        "xvf3800_legacy_square_6ch", "XVF3800-001", "firmware", plan.plan_id,
        xvf3800.chip_aec_fixed_profile_fingerprint(plan),
        "apple_usb_c_dongle", "usb-serial:DWH53530FLL2FN3A3",
        "single:outputd_dac", "S16_LE", 48_000, 2, 128, 384,
    )


def _arm_chip_aec(monkeypatch, dev, *, artifact, live=None) -> None:
    _install_fake_xvf(monkeypatch, dev)
    _stub_amixer(monkeypatch)
    monkeypatch.delenv("JASPER_AEC_CORPUS_CHIP_AEC_ENABLED", raising=False)
    monkeypatch.setenv("JASPER_AEC_CHIP_AEC_ENABLED", "1")
    monkeypatch.setattr(aec_init, "load_artifact", artifact)
    monkeypatch.setattr(
        aec_init, "collect_reference_queue",
        lambda _pcm: ({"reference_outputs": {}}, (283,) * 8),
    )
    monkeypatch.setattr(
        aec_init, "build_identity",
        lambda *_a, **_k: _live_identity() if live is None else live,
    )


@pytest.mark.parametrize(
    "changes, discloses",
    [
        ({"xvf_serial": "replacement"}, True),
        ({"output_hardware_key": "usb-serial:replacement"}, True),
        # The DAC identity is part of the hardware class K was measured
        # against, so it is hardware-class divergence — the loud kind, still
        # not a park.
        ({"output_id": "different_dac"}, True),
        # xvf_variant/beam_plan/output_format carry no timing story
        # (ADR-0190): moving all three produces no divergence, so nothing is
        # disclosed at all — the chip arms clean.
        (
            {
                "xvf_variant": "other_variant",
                "beam_plan": "other_plan",
                "output_format": "S32_LE",
            },
            False,
        ),
    ],
)
def test_a_commissioned_identity_that_moved_is_applied_and_disclosed(
    monkeypatch, alignment_record, changes, discloses
) -> None:
    # ADR-0101: the proof stopped describing this box, nothing observably
    # broke. The banked K is applied and the chip armed; what the household
    # gets is a disclosure naming the field, not a deaf speaker — or nothing
    # at all, when every moved field is recorded-only.
    dev = _FakeXvfDevice()
    commissioned = replace(_live_identity(), **changes)
    _arm_chip_aec(
        monkeypatch, dev, artifact=lambda: AlignmentArtifact(commissioned, 245, -38)
    )

    assert aec_init.main() == 0

    writes = _write_map(dev)
    assert writes["SHF_BYPASS"] == [0]
    assert writes["AUDIO_MGR_SYS_DELAY"] == [-38]
    # What the disclosure SAYS is pinned once, on the judge
    # (tests/test_chip_aec_health.py); this altitude owns only whether one was
    # published at all.
    assert (
        _published(alignment_record).status
        == chip_aec_health.STATUS_DISCLOSED_STALE
    ) is discloses


def test_an_older_schema_artifact_is_treated_as_nothing_banked(
    monkeypatch, alignment_record, tmp_path
) -> None:
    # An artifact predating the current schema cannot be compared against
    # this box, so the reader refuses it like any other invalid artifact and
    # boot falls back to the shipped hardware-class alignment.
    banked = AlignmentArtifact(_live_identity(), 245, -38).to_dict() | {"schema": 1}
    path = tmp_path / "chip-aec-alignment.json"
    path.write_text(json.dumps(banked), encoding="utf-8")
    with pytest.raises(ValueError):
        alignment.load_artifact(path)

    dev = _FakeXvfDevice()
    _arm_chip_aec(monkeypatch, dev, artifact=lambda: alignment.load_artifact(path))
    row = _shipped_row()
    monkeypatch.setattr(shipped_alignment, "REGISTRY", (row,))

    assert aec_init.main() == 0

    assert _write_map(dev)["AUDIO_MGR_SYS_DELAY"] == [row.sys_delay]


def _shipped_row(
    *, k_samples: int = 245, sys_delay: int = -38, **identity_overrides
) -> shipped_alignment.ShippedAlignment:
    identity = _live_identity()
    fields = {
        name: getattr(identity, name)
        for name in alignment.HARDWARE_CLASS_IDENTITY_FIELDS
    }
    return shipped_alignment.ShippedAlignment(
        label="lab class",
        identity=fields | identity_overrides,
        k_samples=k_samples,
        sys_delay=sys_delay,
    )


def test_a_fresh_install_on_a_shipped_hardware_class_arms_and_discloses(
    monkeypatch, alignment_record
) -> None:
    # ADR-0101 / #2984 Q1: an XVF that is there gets chip AEC. A box that has
    # never been commissioned runs from the proof its hardware class ships and
    # discloses that it is doing so, instead of waiting deaf for a commission.
    dev = _FakeXvfDevice()
    _arm_chip_aec(
        monkeypatch, dev, artifact=lambda: (_ for _ in ()).throw(ValueError("missing"))
    )
    row = _shipped_row()
    monkeypatch.setattr(shipped_alignment, "REGISTRY", (row,))

    assert aec_init.main() == 0

    writes = _write_map(dev)
    assert writes["SHF_BYPASS"] == [0]
    assert writes["AUDIO_MGR_SYS_DELAY"] == [row.sys_delay]
    assert _published(alignment_record).status == chip_aec_health.STATUS_DISCLOSED_STALE


def test_a_fresh_install_on_an_unrecognized_hardware_class_still_parks(
    monkeypatch, alignment_record, caplog
) -> None:
    # The shipped row is for a different DAC, so it says nothing about this
    # box: no K to run from, and the commissioner is the answer. The park
    # names the near-miss field, or a row one firmware flash away is
    # indistinguishable from an empty table.
    dev = _FakeXvfDevice()
    _arm_chip_aec(
        monkeypatch, dev, artifact=lambda: (_ for _ in ()).throw(ValueError("missing"))
    )
    row = _shipped_row(output_id="different_dac")
    monkeypatch.setattr(shipped_alignment, "REGISTRY", (row,))

    with caplog.at_level(logging.ERROR, logger="jasper.aec_init"):
        assert aec_init.main() == aec_init.COMMISSION_REQUIRED_EXIT

    assert _write_map(dev)["SHF_BYPASS"] == [1]
    assert _published(alignment_record) == alignment_health(
        chip_aec_health.COMMISSION_REQUIRED, selection=_SELECTION
    )
    assert row.divergence(_live_identity()) == ("output_id",)


@pytest.mark.parametrize(
    "raised, expected_exit",
    [
        # Nothing is banked either way, so a queue that will not settle leaves
        # the disposition the absent artifact already had.
        (aec_init.ChipInitError("queue never settled"), aec_init.COMMISSION_REQUIRED_EXIT),
        # ...but an outputd that predates its own declaration keeps its own
        # exit, so the reconciler names the ordering race and not the
        # commissioner.
        (aec_init.OutputdEnvStale("declaration not loaded"), aec_init.OUTPUTD_ENV_STALE_EXIT),
    ],
)
def test_a_fresh_install_that_cannot_read_the_queue_keeps_its_own_exit(
    monkeypatch, raised, expected_exit
) -> None:
    dev = _FakeXvfDevice()
    _arm_chip_aec(
        monkeypatch, dev, artifact=lambda: (_ for _ in ()).throw(ValueError("missing"))
    )
    monkeypatch.setattr(shipped_alignment, "REGISTRY", (_shipped_row(),))
    monkeypatch.setattr(
        aec_init,
        "collect_reference_queue",
        lambda _pcm: (_ for _ in ()).throw(raised),
    )

    assert aec_init.main() == expected_exit
    assert _write_map(dev)["SHF_BYPASS"] == [1]


def test_a_shipped_k_the_driver_cap_refuses_parks_instead_of_faulting(
    monkeypatch,
) -> None:
    # Non-negotiable #2 still refuses the write. What changes is the
    # disposition: this box has nothing banked, so it needs commissioning
    # (exit 2, software AEC3) rather than an inspection of a healthy daemon.
    dev = _FakeXvfDevice()
    _arm_chip_aec(
        monkeypatch, dev, artifact=lambda: (_ for _ in ()).throw(ValueError("missing"))
    )
    monkeypatch.setattr(
        shipped_alignment,
        "REGISTRY",
        (
            _shipped_row(
                k_samples=283 + xvf3800.CHIP_AEC_SYS_DELAY_MAX + 1,
                sys_delay=xvf3800.CHIP_AEC_SYS_DELAY_MAX,
            ),
        ),
    )

    assert aec_init.main() == aec_init.COMMISSION_REQUIRED_EXIT
    assert _write_map(dev)["SHF_BYPASS"] == [1]


def test_a_commissioned_artifact_outranks_the_row_shipped_for_its_class(
    monkeypatch, alignment_record
) -> None:
    # Commissioning personalizes: once this unit has measured its own K, the
    # class default is never consulted again.
    dev = _FakeXvfDevice()
    _arm_chip_aec(
        monkeypatch,
        dev,
        artifact=lambda: AlignmentArtifact(_live_identity(), 245, -38),
    )
    monkeypatch.setattr(
        shipped_alignment, "REGISTRY", (_shipped_row(k_samples=300, sys_delay=17),)
    )

    assert aec_init.main() == 0

    assert _write_map(dev)["AUDIO_MGR_SYS_DELAY"] == [-38]
    assert _published(alignment_record) == alignment_health(
        chip_aec_health.APPLIED, selection=_SELECTION
    )


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

    fields = event_fields(caplog, "chip_aec_init.ordering_probe")
    assert fields["outcome"] == outcome
    assert fields["unit"] == aec_init.OUTPUTD_UNIT
    assert int(fields["returncode"]) == returncode


def test_a_unit_that_never_ran_is_quiet(monkeypatch, caplog) -> None:
    # The legitimate case stays out of the journal: nothing has started, so there
    # is nothing anomalous to report and a WARN would be noise on every box that
    # boots with outputd gated off.
    _stub_systemctl_show(monkeypatch, "")
    caplog.set_level("WARNING", logger="jasper.aec_init")

    assert aec_init.outputd_main_start_realtime() is None

    assert event_records(caplog, "chip_aec_init.ordering_probe") == []


def test_a_literal_zero_timestamp_parses_as_the_epoch(monkeypatch) -> None:
    # Not the observed never-run rendering (that is an empty value, see above) —
    # a literal `@0` parses like any other numeric ExecMainStartTimestamp.
    _stub_systemctl_show(monkeypatch, "@0\n")

    assert aec_init.outputd_main_start_realtime() == 0.0


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
    assert event_records(caplog, "chip_aec_init.ordering_probe") == []


def test_outputd_start_instant_warns_on_a_non_missing_binary_oserror(
    monkeypatch, caplog
) -> None:
    # Unlike FileNotFoundError above (no systemctl binary at all — not a
    # systemd host), a PermissionError means systemctl IS present but exec'ing
    # it failed some other way on what IS a systemd host — one of the
    # non-ENOENT OSError subtypes (EACCES here; also ENOEXEC/EAGAIN/ENOMEM,
    # ...) that a prior blanket `except OSError` swallowed silently (PR #2225
    # round-3 nit). This is worth surfacing, same as the other two
    # WARN-logged anomalies.
    def fake_run(_args, **_kwargs):
        raise PermissionError("systemctl")

    monkeypatch.setattr(aec_init.subprocess, "run", fake_run)
    caplog.set_level("WARNING", logger="jasper.aec_init")

    assert aec_init.outputd_main_start_realtime() is None

    fields = event_fields(caplog, "chip_aec_init.ordering_probe")
    assert fields["outcome"] == "systemctl_oserror"
    assert fields["unit"] == aec_init.OUTPUTD_UNIT


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
        aec_init, "load_artifact", lambda: AlignmentArtifact(identity, 245, -38)
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
    fields = event_fields(caplog, "chip_aec_init")
    # `deferred`, not `parked`: the remedy names the outputd unit to wait on
    # rather than sending a household at the commissioner.
    assert fields["outcome"] == "deferred"
    assert aec_init.OUTPUTD_UNIT in fields["action"]


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


def test_a_parked_init_hands_the_box_to_the_reconciler() -> None:
    # Every exit above 0 leaves this oneshot failed, and the reconciler is what
    # turns that into a published disposition (alignment status/reason/action,
    # the voice-input-absent marker). The wake-corpus exit path documents this
    # directive as its backstop when a corpus exit parks an uncommissioned box
    # (issue #2254), so it is load-bearing rather than incidental.
    unit = (
        ROOT / "deploy" / "systemd" / "jasper-aec-init.service"
    ).read_text(encoding="utf-8")

    assert "OnFailure=jasper-aec-reconcile.service\n" in unit


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

    assert event_fields(caplog, "chip_aec_init")["outcome"] == "failed"


# ---------------------------------------------------------------------------
# Chip-reference queue collection over outputd's per-write ring (#2253)
#
# The pre-#2253 rule — eight consecutive STATUS reads, every one at
# reference_sequence_lag == 0, spread <= 16 — encoded a 128-frame mix period's
# timing as if it were universal.  jts.local (period 128, 2.7 ms cadence) met
# it; jts3 (period 1024, 21.3 ms cadence, identical chip-ref geometry) could
# not, so commissioning AND every boot of jasper-aec-init parked the box.
#
# Neither can a poll assemble the window: outputd's state server is one thread
# that sleeps 500 ms between accepts and answers one command per connection, so
# a sequential reader gets ~2 reads/s (measured on jts3: 11 reads in 5.14 s).
# outputd therefore publishes the writer's own recent observations and this
# collector folds them into a sliding window.  The fixtures below are built to
# the spread, cadence, and geometry each box measured on 2026-08-08 — that shape
# is the measurement; the individual readings inside it carry it.
# ---------------------------------------------------------------------------

REFERENCE_PCM = "hw:CARD=Array,DEV=0"
DAC_RATE = 48_000
# What outputd's ring holds (rust/jasper-outputd/src/state.rs
# CHIP_REF_RECENT_WRITES). Restated here because these tests stand in for
# outputd; tests/test_wire_contracts.py pins the two together.
RING_CAPACITY = 256


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
    packet's write returns — so how many distinct observations exist is a
    function of elapsed time, not of how often the poll asks.  What a read
    returns is the last `RING_CAPACITY` of them, which is the whole point: the
    reader's rate and the writer's rate are decoupled.

    A read COSTS TIME, in the order outputd's state server really spends it:
    that thread sleeps up to `ACCEPT_POLL_INTERVAL` before it accepts, THEN
    takes the snapshot, THEN builds and writes the reply.  Modelling that order
    is what makes the collector's `now`-sampling bias measurable at all — with
    a read that advances the clock by zero, every placement is exact by
    construction and the question cannot be asked.  Defaults reproduce jts3's
    measured 11 reads in 5.14 s: a 0.25 s mean accept wait on a 500 ms poll
    cycle plus the collector's own 0.25 s sleep is ~0.5 s a read.
    """

    def __init__(
        self,
        clock: _FakeClock,
        *,
        mix_period_frames: int,
        delays,
        lag: int = 0,
        stall_after: int | None = None,
        capacity: int = RING_CAPACITY,
        accept_wait: float = 0.25,
        reply_latency: float = 0.004,
    ) -> None:
        self.clock = clock
        self.mix_period_frames = mix_period_frames
        self.period_seconds = mix_period_frames / DAC_RATE
        self.burst = round(
            mix_period_frames * xvf3800.CHIP_AEC_REFERENCE_SAMPLE_RATE_HZ / DAC_RATE
        )
        self.delays = tuple(delays)
        self.lag = lag
        self.stall_after = stall_after
        self.capacity = capacity
        self.accept_wait = accept_wait
        self.reply_latency = reply_latency
        self.reads = 0
        # When each reply's snapshot was taken, so a test can compare the
        # instant the collector placed an observation at against the truth,
        # and how many writes the writer had completed by then, so a test can
        # bound the window without reasoning about elapsed time.
        self.snapshot_instants: list[float] = []
        self.writes_at_snapshot: list[int] = []
        self.counters = dict.fromkeys(aec_init.REFERENCE_WRITER_COUNTER_NAMES, 0)

    def _writes_completed(self) -> int:
        elapsed = int(self.clock.now / self.period_seconds) + 1
        return elapsed if self.stall_after is None else min(elapsed, self.stall_after)

    def _recent_writes(self, writes: int) -> list[dict]:
        first = max(writes - self.capacity, 0)
        return [
            {
                "frames_written": (index + 1) * self.burst,
                "snd_pcm_delay_frames": self.delays[index % len(self.delays)],
                "reference_sequence": index + 1 if index else None,
                "age_ms": int(
                    (self.clock.now - index * self.period_seconds) * 1_000
                ),
            }
            for index in range(first, writes)
        ]

    def __call__(self, _path: str) -> dict:
        self.reads += 1
        # The server's accept poll runs BEFORE the snapshot is taken...
        self.clock.now += self.accept_wait
        self.snapshot_instants.append(self.clock.now)
        writes = self._writes_completed()
        self.writes_at_snapshot.append(writes)
        age_ms = int((self.clock.now - (writes - 1) * self.period_seconds) * 1_000)
        writer = {
            "desired": True,
            "active": True,
            "status": "active",
            "reference_sequence_lag": self.lag,
            "snd_pcm_delay_sample_age_ms": age_ms,
            "last_write_age_ms": age_ms,
            "snd_pcm_delay_frames": self.delays[(writes - 1) % len(self.delays)],
            "frames_written": writes * self.burst,
            "recent_writes_capacity": self.capacity,
            "recent_writes": self._recent_writes(writes),
        }
        writer.update(self.counters)
        # ...and building, writing and reading the reply happens after it. This
        # is the only gap between the snapshot and the collector's `now`.
        self.clock.now += self.reply_latency
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


def _install_queue_stream(monkeypatch, stream, clock: _FakeClock) -> None:
    monkeypatch.setattr(aec_init, "read_status_socket", stream)
    monkeypatch.setattr(aec_init, "require_outputd_env_loaded", lambda **_kwargs: None)
    monkeypatch.setattr(aec_init, "time", clock)


def test_fine_cadence_box_still_settles_in_the_reference_window(monkeypatch) -> None:
    # jts.local's numbers. Widening the rule for coarse cadences must not make
    # the fine one slower or looser than the window K was originally measured
    # with: same holding-still span, same spread, more readings inside it.
    clock = _FakeClock()
    stream = _WriterStream(clock, mix_period_frames=128, delays=FINE_CADENCE_DELAYS)
    _install_queue_stream(monkeypatch, stream, clock)

    _status, queue = aec_init.collect_reference_queue(REFERENCE_PCM)

    assert len(queue) >= alignment.QUEUE_SAMPLE_COUNT
    assert max(queue) - min(queue) <= alignment.QUEUE_MAX_SPREAD
    assert aec_init.QUEUE_MIN_WINDOW_SEC <= clock.now < aec_init.QUEUE_MIN_WINDOW_SEC + 1


def test_coarse_cadence_box_settles_inside_the_collection_budget(monkeypatch) -> None:
    # The #2253 regression, as measured on jts3: two commission attempts and
    # every boot failed here. A spread the blocking write cannot avoid must be
    # admissible, and the window has to close well inside the 30 s budget
    # aec-init's caller allows for it — which it can only do because one read
    # carries the writer's history rather than one reading.
    clock = _FakeClock()
    stream = _WriterStream(
        clock, mix_period_frames=1024, delays=COARSE_CADENCE_DELAYS, lag=1
    )
    _install_queue_stream(monkeypatch, stream, clock)

    _status, queue = aec_init.collect_reference_queue(REFERENCE_PCM)

    spread = max(queue) - min(queue)
    assert spread == 86, "the fixture carries jts3's measured spread"
    assert spread > alignment.QUEUE_MAX_SPREAD, "the old rule rejected this outright"
    assert stream.lag == 1, "the old rule rejected a lag of one outright"
    assert len(queue) >= alignment.required_queue_samples(spread)
    assert clock.now < 30.0
    # The whole window came out of the ring, so the collector needed a handful
    # of reads rather than one per reading — the throughput the panel measured.
    assert stream.reads < len(queue)


def test_the_collected_window_is_one_runtime_sys_delay_accepts(monkeypatch) -> None:
    # Two consumers, one definition of a usable window: whatever the collector
    # hands back at boot goes straight into runtime_sys_delay, so an acceptance
    # rule looser than the re-validation would park every coarse-cadence box
    # with "queue is unstable" immediately after the poll passed. The assertion
    # is that runtime_sys_delay RETURNS — a window it rejects raises.
    clock = _FakeClock()
    stream = _WriterStream(
        clock, mix_period_frames=1024, delays=COARSE_CADENCE_DELAYS, lag=1
    )
    _install_queue_stream(monkeypatch, stream, clock)

    _status, queue = aec_init.collect_reference_queue(REFERENCE_PCM)

    assert alignment.runtime_sys_delay(400, queue) == 400 - alignment.median_samples(
        queue
    )


def test_an_outputd_without_the_sample_ring_is_named_not_guessed(
    monkeypatch, caplog
) -> None:
    # An outputd too old to publish the ring cannot be worked around: at ~2
    # STATUS reads a second the single latest reading assembles no window on any
    # box, so guessing would trade a loud refusal for a boot that burns its
    # budget and then blames the queue.
    caplog.set_level("WARNING", logger="jasper.aec_init")
    clock = _FakeClock()
    stream = _WriterStream(clock, mix_period_frames=1024, delays=(448,))

    def without_ring(path: str) -> dict:
        status = stream(path)
        del status["reference_outputs"]["chip_ref_writer"]["recent_writes"]
        return status

    _install_queue_stream(monkeypatch, without_ring, clock)

    with pytest.raises(aec_init.ChipInitError) as excinfo:
        aec_init.collect_reference_queue(REFERENCE_PCM)

    # `ChipInitError` carries no code or structured attribute, so the refusal
    # itself can only be told apart by its message.
    reason = str(excinfo.value)
    assert "no chip-ref sample ring" in reason
    assert "deploy an outputd that reports it" in reason
    # Every numeric field of this refusal is zero, so without the reason the
    # event cannot be told from any other empty-window failure.
    fields = event_fields(caplog, "chip_aec_init.reference_queue")
    assert fields["outcome"] == "unstable"
    assert int(fields["samples"]) == 0
    assert int(fields["spread"]) == 0
    # The refusal's own last error travels into the event verbatim, rather
    # than the event restating it.
    assert fields["reason"] and reason.endswith(fields["reason"])


def test_an_empty_sample_ring_is_a_writer_that_has_not_written_yet(
    monkeypatch,
) -> None:
    # Absent and empty are different states. A writer that has just opened its
    # PCM has nothing to report and the next read will, so an empty ring must
    # not be read as an outputd too old to have one.
    clock = _FakeClock()
    stream = _WriterStream(clock, mix_period_frames=1024, delays=(448,))
    status = stream(REFERENCE_PCM)
    status["reference_outputs"]["chip_ref_writer"]["recent_writes"] = []

    assert (
        aec_init.validate_reference_status(status, expected_pcm=REFERENCE_PCM).writes
        == ()
    )


def test_a_stalled_writer_still_fails_loudly(monkeypatch) -> None:
    # A writer that stops writing keeps publishing the same ring forever. The
    # ring cannot say so — its entries do not know they are stale — so the
    # writer-level wall-clock ages are what have to fail it.
    clock = _FakeClock()
    stream = _WriterStream(
        clock,
        mix_period_frames=1024,
        delays=COARSE_CADENCE_DELAYS,
        stall_after=4,
    )
    _install_queue_stream(monkeypatch, stream, clock)

    with pytest.raises(aec_init.ChipInitError) as excinfo:
        aec_init.collect_reference_queue(REFERENCE_PCM)

    assert "not ready" in str(excinfo.value)
    assert "age_ms is stale" in str(excinfo.value)
    assert clock.now >= 30.0, "a stall consumes the budget rather than short-circuiting"


def test_a_queue_that_never_holds_still_fails_with_its_own_numbers(
    monkeypatch, caplog
) -> None:
    # A reference queue whose median walks away from its start cannot carry K at
    # any sample count, and the operator needs the shortfall — plus the covariate
    # that separates "would not hold still" from "could not be sampled fast
    # enough" — rather than "not current and active".
    caplog.set_level("WARNING", logger="jasper.aec_init")
    clock = _FakeClock()
    # A median walking 22 frames a second — past the 17 f/s the bound admits
    # at the minimum window, but slow enough that the spread it opens is one
    # this cadence can still answer for, so the DRIFT bound is what refuses it
    # rather than the count rule or the retention ceiling. A walking queue
    # widens without limit, so the budget is trimmed to end while that is still
    # the live reason.
    stream = _WriterStream(
        clock,
        mix_period_frames=1024,
        delays=tuple(300 + (index * 22 * 1024) // DAC_RATE for index in range(4_096)),
    )
    _install_queue_stream(monkeypatch, stream, clock)

    with pytest.raises(aec_init.ChipInitError) as excinfo:
        aec_init.collect_reference_queue(REFERENCE_PCM, timeout=5.0)

    reason = str(excinfo.value)
    assert "chip-reference queue spread" in reason
    assert "halves" in reason and "held" in reason
    assert "retention window" not in reason, (
        "a closable-looking shortfall and an unclosable ceiling must stay "
        "distinguishable in the message"
    )
    # Every field by value against the window that was actually refused —
    # a label-only assertion cannot tell a live covariate from a constant.
    fields = event_fields(caplog, "chip_aec_init.reference_queue")
    assert fields["outcome"] == "unstable"
    assert stream.reads > 1
    assert int(fields["status_reads"]) == stream.reads
    assert float(fields["poll_interval_ms"]) == round(
        aec_init.QUEUE_POLL_INTERVAL_SEC * 1_000, 1
    )
    assert int(fields["samples"]) > alignment.QUEUE_SAMPLE_COUNT
    assert int(fields["spread"]) > alignment.QUEUE_MAX_SPREAD
    assert int(fields["required"]) == alignment.required_queue_samples(
        int(fields["spread"])
    )
    assert int(fields["median"]) >= 300, "the fixture's readings start at 300"
    assert int(fields["median_drift"]) > alignment.QUEUE_MAX_MEDIAN_DRIFT, (
        "this queue was refused BECAUSE its halves disagreed; the field has to "
        "say so"
    )
    assert float(fields["held_sec"]) >= aec_init.QUEUE_MIN_WINDOW_SEC
    # The reason travels into the event verbatim rather than being restated
    # there — it is what tells two all-zero refusals apart.
    assert fields["reason"] and reason.endswith(fields["reason"])


def test_a_window_whose_median_walked_is_rejected_on_both_sides_of_the_edge() -> None:
    # The count rule alone cannot see drift: a walking queue satisfies any count
    # you like if you sample long enough. Pin the acceptance boundary itself —
    # at the limit the window is usable, one frame past it is not.
    def window(step: int) -> tuple[int, ...]:
        # Two halves of 200 readings each carrying jts3's measured 86-frame
        # spread, the second offset by `step`, so the half-medians differ by
        # exactly `step`. 400 readings clear the count rule at both steps, which
        # is what leaves drift as the only difference between the two windows.
        half = [362 + (index * 86) // 199 for index in range(200)]
        return tuple(half + [value + step for value in half])

    at_limit = window(alignment.QUEUE_MAX_MEDIAN_DRIFT)
    past_limit = window(alignment.QUEUE_MAX_MEDIAN_DRIFT + 1)
    for candidate in (at_limit, past_limit):
        spread = max(candidate) - min(candidate)
        assert len(candidate) >= alignment.required_queue_samples(spread)

    assert alignment.queue_median_drift(at_limit) == alignment.QUEUE_MAX_MEDIAN_DRIFT
    assert alignment.queue_window_is_stable(at_limit)
    assert (
        alignment.queue_median_drift(past_limit)
        == alignment.QUEUE_MAX_MEDIAN_DRIFT + 1
    )
    assert not alignment.queue_window_is_stable(past_limit)


def test_an_unperturbed_stream_closes_a_window(monkeypatch) -> None:
    # The control for the perturbation tests below: everything they hold
    # constant is already enough to close a window, so the perturbation is the
    # only thing their failure can be attributed to.
    clock = _FakeClock()
    stream = _WriterStream(clock, mix_period_frames=1024, delays=(448,))
    _install_queue_stream(monkeypatch, stream, clock)

    _status, queue = aec_init.collect_reference_queue(REFERENCE_PCM)

    assert len(queue) >= alignment.QUEUE_SAMPLE_COUNT


def test_moving_error_counters_are_not_one_window(monkeypatch) -> None:
    # An xrun, a reopen, or a dropped period splits the readings on either side
    # of it into two different pipeline states; a median taken across the seam
    # is one no instant ever held. A counter moving on every read means no
    # stretch is ever seam-free, so no window can close.
    clock = _FakeClock()
    stream = _WriterStream(clock, mix_period_frames=1024, delays=(448,))

    def tick(path: str) -> dict:
        status = stream(path)
        status["reference_outputs"]["chip_ref_writer"]["write_xrun_count"] = (
            stream.reads
        )
        return status

    _install_queue_stream(monkeypatch, tick, clock)

    with pytest.raises(aec_init.ChipInitError) as excinfo:
        aec_init.collect_reference_queue(REFERENCE_PCM)

    assert "error counters moved" in str(excinfo.value)


def test_the_ring_cannot_rebuild_a_window_across_a_seam(monkeypatch) -> None:
    # The ring is history, so clearing the window at a seam is not enough on its
    # own: the very next read hands the discarded pre-seam observations straight
    # back. Without the admission floor this closes a window that spans the xrun
    # — which is the check going quietly inert, not passing.
    clock = _FakeClock()
    stream = _WriterStream(clock, mix_period_frames=1024, delays=(448,))
    clock.now = 6.0  # a full ring of history exists before the seam

    def tick_once(path: str) -> dict:
        status = stream(path)
        if stream.reads > 1:
            status["reference_outputs"]["chip_ref_writer"]["write_xrun_count"] = 1
        return status

    _install_queue_stream(monkeypatch, tick_once, clock)
    seam_at = 6.0 + aec_init.QUEUE_POLL_INTERVAL_SEC

    _status, queue = aec_init.collect_reference_queue(REFERENCE_PCM)

    # Every reading in the accepted window was written after the seam: the
    # window spans at least the minimum, and it could only start after it.
    assert clock.now - seam_at >= aec_init.QUEUE_MIN_WINDOW_SEC
    assert len(queue) <= (clock.now - seam_at) / (1024 / DAC_RATE) + 1


def test_an_intermittent_counter_tick_still_closes_inside_the_budget(
    monkeypatch,
) -> None:
    # One xrun is not a broken box. It restarts the window — readings either
    # side of it belong to different pipeline states — and the collector has to
    # rebuild and close inside the same budget rather than spending it.
    clock = _FakeClock()
    stream = _WriterStream(
        clock, mix_period_frames=1024, delays=COARSE_CADENCE_DELAYS, lag=1
    )

    def one_tick(path: str) -> dict:
        status = stream(path)
        if stream.reads > 1:
            status["reference_outputs"]["chip_ref_writer"]["write_xrun_count"] = 1
        return status

    _install_queue_stream(monkeypatch, one_tick, clock)

    _status, queue = aec_init.collect_reference_queue(REFERENCE_PCM)

    assert len(queue) >= alignment.required_queue_samples(max(queue) - min(queue))
    assert clock.now < 30.0


def test_a_rewound_write_counter_splits_the_window(monkeypatch) -> None:
    # outputd restarting under the poll builds a new state object, so the
    # writer's cumulative frame counter — the ring entries' own identity —
    # restarts too. Saying so beats letting the window quietly stop growing
    # because every incoming entry looks like one already folded in.
    held = [aec_init.ReferenceWrite(delay=448, frames=4_096, sequence=9, age_ms=0)]
    window = aec_init._merge_writes([], held, now=10.0, floor=0.0)
    assert len(window) == 1, "the control: an ordinary fold keeps the reading"

    forward = [aec_init.ReferenceWrite(delay=448, frames=4_437, sequence=10, age_ms=0)]
    assert len(aec_init._merge_writes(window, forward, now=10.5, floor=0.0)) == 2

    restarted = [aec_init.ReferenceWrite(delay=448, frames=341, sequence=1, age_ms=0)]
    # `match=` stands: ChipInitError carries no code or structured attribute.
    with pytest.raises(aec_init.ChipInitError, match="did not progress cleanly"):
        aec_init._merge_writes(window, restarted, now=10.5, floor=0.0)


def test_a_ring_out_of_write_order_is_refused(monkeypatch) -> None:
    # The entries' identity IS their cumulative frame count, so a ring that is
    # not strictly increasing is not the writer's history and cannot be deduped
    # against.
    clock = _FakeClock()
    stream = _WriterStream(clock, mix_period_frames=1024, delays=(448, 449, 450))
    clock.now = 1.0
    status = stream(REFERENCE_PCM)
    writes = status["reference_outputs"]["chip_ref_writer"]["recent_writes"]
    assert len(writes) >= 3
    writes[1]["frames_written"] = writes[0]["frames_written"]

    # `match=` stands: ChipInitError carries no code or structured attribute.
    with pytest.raises(aec_init.ChipInitError, match="not in write order"):
        aec_init.validate_reference_status(status, expected_pcm=REFERENCE_PCM)


def test_overlapping_reads_are_folded_in_once(monkeypatch) -> None:
    # Consecutive reads return overlapping rings by design — that overlap is
    # what makes a slow reader lossless. Counting an entry twice would let a
    # window "stabilise" on more readings than the writer ever produced, and so
    # on less precision than the count it reports claims to have bought.
    #
    # Asserted on the window's OWN contents, not on elapsed time. An
    # elapsed-time proxy is not this property: duplicates scale with the number
    # of READS while the time headroom scales with the clock, so charging a
    # realistic cost per read silently dissolves the margin and the assertion
    # stops discriminating.
    clock = _FakeClock()
    stream = _WriterStream(clock, mix_period_frames=1024, delays=(400,))
    _install_queue_stream(monkeypatch, stream, clock)

    placed: list[list[tuple[float, aec_init.ReferenceWrite]]] = []
    real_merge = aec_init._merge_writes
    monkeypatch.setattr(
        aec_init,
        "_merge_writes",
        lambda w, r, now, floor: placed.append(real_merge(w, r, now, floor))
        or placed[-1],
    )

    _status, queue = aec_init.collect_reference_queue(REFERENCE_PCM)

    # `frames_written` is cumulative and strictly increasing across the ring —
    # it IS an observation's identity — so a window holding the same write
    # twice is exactly what a broken dedup produces.
    frames = [write.frames for _at, write in placed[-1]]
    assert len(frames) == len(queue)
    assert len(set(frames)) == len(frames), "the same write was folded in twice"
    assert frames == sorted(frames)
    # Independently: the writer cannot have produced more readings than it
    # completed writes between the baseline snapshot and the last one.
    produced = stream.writes_at_snapshot[-1] - stream.writes_at_snapshot[0]
    assert len(queue) <= produced, (
        f"{len(queue)} readings from {produced} writes the writer made"
    )
    # The reads really did overlap, or nothing above is exercised.
    assert stream.reads * RING_CAPACITY > len(queue)


def test_the_window_slides_so_one_outlier_ages_out(monkeypatch) -> None:
    # Grow-only was the defect: a single wild write inflates the spread, the
    # spread inflates the required count, and the run is doomed for as long as
    # that reading stays in the window. It has to leave.
    clock = _FakeClock()
    # One reading 4000 frames off, then a still queue forever after. It sits
    # past write 12 so it lands AFTER the counter baseline — a wild reading
    # before the baseline is excluded by the floor and would never test the
    # horizon at all.
    delays = (448,) * 14 + (4_400,) + (448,) * 4_095
    stream = _WriterStream(clock, mix_period_frames=1024, delays=delays)
    _install_queue_stream(monkeypatch, stream, clock)

    _status, queue = aec_init.collect_reference_queue(REFERENCE_PCM)

    assert max(queue) - min(queue) == 0, "the outlier left the window"
    # A spread of 3952 needs more readings than exist, so the window cannot
    # close until the horizon evicts the outlier — which is the point.
    assert clock.now > aec_init.QUEUE_WINDOW_MAX_SEC
    assert clock.now < 30.0


def test_a_writer_further_behind_than_the_pipelining_ceiling_is_rejected() -> None:
    # Two periods is the ceiling: one being written, one published before the
    # writer dequeued it. Deeper means the writer is losing ground on a producer
    # it is rate-locked to. Pinned on both sides of the edge because this cap is
    # the first-line liveness check on the writer feeding the ring.
    clock = _FakeClock()
    behind = _WriterStream(
        clock,
        mix_period_frames=1024,
        delays=(448,),
        lag=aec_init.MAX_REFERENCE_PERIODS_IN_FLIGHT + 1,
    )(REFERENCE_PCM)

    # `match=` stands: ChipInitError carries no code or structured attribute.
    with pytest.raises(aec_init.ChipInitError, match="mix periods behind"):
        aec_init.validate_reference_status(behind, expected_pcm=REFERENCE_PCM)

    at_ceiling = _WriterStream(
        clock,
        mix_period_frames=1024,
        delays=(448,),
        lag=aec_init.MAX_REFERENCE_PERIODS_IN_FLIGHT,
    )(REFERENCE_PCM)

    assert aec_init.validate_reference_status(
        at_ceiling, expected_pcm=REFERENCE_PCM
    ).writes



def test_the_placement_bias_is_the_reply_not_the_accept_wait(monkeypatch) -> None:
    # Two mechanisms were proposed for how late an observation gets placed.
    # (1) `now` is sampled after the read, so the whole read latency (measured
    # 243-510 ms against a live outputd) biases the placement. (2) outputd's
    # state server sleeps its accept poll BEFORE it takes the snapshot, so that
    # wait precedes the observation and only the reply separates the snapshot
    # from `now`. This measures which is real against a fixture that spends
    # time in the server's actual order — and it is (2): the bias equals the
    # reply latency, and adding a HALF-SECOND accept wait does not move it.
    biases: list[float] = []
    for accept_wait in (0.25, 0.5):
        clock = _FakeClock()
        stream = _WriterStream(
            clock,
            mix_period_frames=1024,
            delays=(448,),
            accept_wait=accept_wait,
            reply_latency=0.004,
        )
        _install_queue_stream(monkeypatch, stream, clock)

        placed: list[list[tuple[float, aec_init.ReferenceWrite]]] = []
        real_merge = aec_init._merge_writes

        def capture(window, writes, now, floor, _real=real_merge, _out=placed):
            merged = _real(window, writes, now, floor)
            _out.append(merged)
            return merged

        monkeypatch.setattr(aec_init, "_merge_writes", capture)
        aec_init.collect_reference_queue(REFERENCE_PCM)
        monkeypatch.setattr(aec_init, "_merge_writes", real_merge)

        # The newest reading of the last read: its true instant is that read's
        # snapshot minus its own age, and the collector placed it at
        # `now - age`. The gap between the two IS the bias.
        window = placed[-1]
        newest_at, newest = max(window, key=lambda item: item[0])
        snapshot_at = stream.snapshot_instants[-1]
        biases.append(newest_at - (snapshot_at - newest.age_ms / 1_000))

    assert all(abs(bias - 0.004) < 1e-6 for bias in biases), biases
    assert biases[0] == pytest.approx(biases[1]), (
        "the accept wait doubled and the placement bias did not move: it "
        "precedes the snapshot, so it is not part of the bias"
    )
    # And it is small against the window it biases, which is what makes
    # sampling `now` after the read the accurate AND conservative choice —
    # sampling before it would move the floor earlier, the loosening direction.
    assert max(biases) < aec_init.QUEUE_MIN_WINDOW_SEC / 100


def test_the_window_never_reaches_back_before_its_counter_baseline(
    monkeypatch,
) -> None:
    # The ring is history. Readings older than the baseline sit in an interval
    # whose lower end this loop never observed the counters at, so a seam
    # inside it is invisible — and a step at the leading edge is invisible to
    # the split-half drift bound by construction. The floor is the baseline
    # instant, so none of them are admitted.
    clock = _FakeClock()
    clock.now = 6.0  # a full ring of history exists before the first read
    stream = _WriterStream(clock, mix_period_frames=1024, delays=(448,))
    _install_queue_stream(monkeypatch, stream, clock)

    placed: list[list[tuple[float, aec_init.ReferenceWrite]]] = []
    real_merge = aec_init._merge_writes
    monkeypatch.setattr(
        aec_init,
        "_merge_writes",
        lambda w, r, now, floor: placed.append(real_merge(w, r, now, floor))
        or placed[-1],
    )

    aec_init.collect_reference_queue(REFERENCE_PCM)

    baseline_at = stream.snapshot_instants[0] + stream.reply_latency
    earliest = min(at for at, _write in placed[-1])
    assert earliest >= baseline_at, (
        "a reading from before the counter baseline was admitted; no instant "
        "in the window's lower half was ever checked for a seam"
    )
    # The ring genuinely offered older readings — otherwise this proves nothing.
    assert stream.snapshot_instants[0] > aec_init.QUEUE_WINDOW_MAX_SEC / 4


def test_the_horizon_is_what_caps_the_spread_a_cadence_can_answer_for() -> None:
    # The retained count is cadence x horizon, so the horizon — not the 30 s
    # budget — is the real ceiling on how wide a queue a box can carry K at.
    # The comment on QUEUE_WINDOW_MAX_SEC states both numbers; derive them.
    for writes_per_sec, stated_cap in ((DAC_RATE / 1024, 109), (DAC_RATE / 128, 309)):
        retained = int(writes_per_sec * aec_init.QUEUE_WINDOW_MAX_SEC)
        assert alignment.required_queue_samples(stated_cap) <= retained
        assert alignment.required_queue_samples(stated_cap + 1) > retained
    # jts3 measures 86 against its cap of 109 — real headroom, and thin enough
    # that shrinking the horizon would put the measured box outside it.
    assert alignment.required_queue_samples(86) <= int(
        DAC_RATE / 1024 * aec_init.QUEUE_WINDOW_MAX_SEC
    )


def test_a_spread_past_the_horizon_names_the_ceiling_not_a_shortfall(
    monkeypatch,
) -> None:
    # A spread wider than the cadence can answer for fails with "more readings
    # than this cadence produces inside the retention window", not with a
    # reading shortfall — the latter sends an operator looking for a longer
    # budget that can never close it.
    clock = _FakeClock()
    # 220 frames of spread at jts3's cadence needs ~1512 readings; the horizon
    # retains 375.
    stream = _WriterStream(
        clock, mix_period_frames=1024, delays=(448, 668) * 2_048
    )
    _install_queue_stream(monkeypatch, stream, clock)

    with pytest.raises(aec_init.ChipInitError) as excinfo:
        aec_init.collect_reference_queue(REFERENCE_PCM)

    reason = str(excinfo.value)
    assert "retention window" in reason
    assert "wider than this output cadence can carry K at" in reason
    assert "held" not in reason, "a closable shortfall would misattribute this"


def test_a_moved_live_queue_arms_the_delay_k_resolves_without_disclosure(
    monkeypatch, alignment_record
) -> None:
    # Every outputd restart re-opens the chip-reference PCM at a different fill,
    # and K - live median is exactly what absorbs it: jts.local commissioned at
    # a median of 200 and booted at 266, applied -21, and the chip converged.
    # A moved queue is the case K answers, not a reason to nag (ADR-0223).
    dev = _FakeXvfDevice()
    _arm_chip_aec(
        monkeypatch, dev, artifact=lambda: AlignmentArtifact(_live_identity(), 245, 45)
    )
    monkeypatch.setattr(
        aec_init, "collect_reference_queue",
        lambda _pcm: ({"reference_outputs": {}}, (266,) * 8),
    )

    assert aec_init.main() == 0

    writes = _write_map(dev)
    assert writes["SHF_BYPASS"] == [0]
    assert writes["AUDIO_MGR_SYS_DELAY"] == [245 - 266]
    assert _published(alignment_record) == alignment_health(
        chip_aec_health.APPLIED, selection=_SELECTION
    )


def test_a_queue_that_resolves_past_the_driver_cap_leaves_the_chip_bypassed(
    monkeypatch, alignment_record
) -> None:
    # The declared CHIP_AEC_SYS_DELAY_MIN..MAX range is the one thing K may not
    # resolve outside of: no delay is written, the chip stays bypassed, and the
    # run fails loudly rather than clamping to an alignment nothing proved.
    dev = _FakeXvfDevice()
    _arm_chip_aec(
        monkeypatch, dev, artifact=lambda: AlignmentArtifact(_live_identity(), 245, -38)
    )
    monkeypatch.setattr(
        aec_init, "collect_reference_queue",
        lambda _pcm: (
            {"reference_outputs": {}},
            (245 - xvf3800.CHIP_AEC_SYS_DELAY_MIN + 1,) * 8,
        ),
    )

    assert aec_init.main() == 1

    writes = _write_map(dev)
    assert writes["SHF_BYPASS"] == [1]
    assert "AUDIO_MGR_SYS_DELAY" not in writes


def test_the_load_bearing_queue_literals_are_what_the_derivations_assume() -> None:
    # Both are load-bearing and both are invisible to fixtures built FROM them.
    # The in-flight cap is the sole first-line replacement for the removed
    # lag == 0 gate; the minimum window is the drift exposure the pre-#2253
    # eight-readings-at-0.25 s window covered (seven intervals = 1.75 s),
    # rounded up.
    assert aec_init.MAX_REFERENCE_PERIODS_IN_FLIGHT == 2
    assert aec_init.QUEUE_MIN_WINDOW_SEC == 2.0
    # This round's own constants, pinned for the same reason: the drift bound
    # survives being widened to 26 against fixtures built from it, which walks
    # back most of the tightening it exists for.
    assert alignment.QUEUE_MAX_MEDIAN_DRIFT == 17
    assert alignment.QUEUE_DRIFT_NOISE_SIGMAS == 3
    # Commissioning-side only since ADR-0223: the causal-window margin
    # `choose_delay` reserves and `_final_timing` re-checks at chirp time.
    assert alignment.MIN_EDGE_MARGIN == 8
    assert (
        aec_init.QUEUE_MIN_WINDOW_SEC
        >= (alignment.QUEUE_SAMPLE_COUNT - 1) * aec_init.QUEUE_POLL_INTERVAL_SEC
    )
    # The sliding horizon must leave room for the minimum window several times
    # over, or an outlier ageing out would take the whole window with it.
    assert aec_init.QUEUE_WINDOW_MAX_SEC >= 4 * aec_init.QUEUE_MIN_WINDOW_SEC


class _LiveClock:
    """Real elapsed seconds, in the shape `_WriterStream` reads its clock.

    Writes are absorbed rather than rejected: `_WriterStream` advances its
    clock to model a server's accept wait, and against a real server that time
    is spent for real. Tests using this clock pass zero for both — and the
    setter checks that, so a future non-zero accept_wait against a live server
    fails loudly instead of being silently discarded (which would make the
    fixture model a delay it is not actually spending).
    """

    def __init__(self) -> None:
        self._started = time.monotonic()

    @property
    def now(self) -> float:
        return time.monotonic() - self._started

    @now.setter
    def now(self, value: float) -> None:
        # `clock.now += 0.0` reads then writes the same instant, microseconds
        # apart; 50 ms tolerates that with room to spare while still catching
        # the smallest advance any caller would plausibly ask for.
        assert value == pytest.approx(self.now, abs=0.05), (
            "_LiveClock cannot be advanced: a real server spends this time "
            "itself, so pass accept_wait=0/reply_latency=0 with it"
        )


class _ThrottledStatusServer:
    """A real AF_UNIX STATUS server with outputd's own throughput ceiling.

    outputd's state server is ONE thread doing a non-blocking accept and
    sleeping `ACCEPT_POLL_INTERVAL` (500 ms) whenever nothing is pending, and it
    answers one command per connection.  Every other test here substitutes
    `read_status_socket` with a free fake, which is exactly how round one of
    #2253 shipped a collector that needed 232 readings at 4 reads/s from a
    socket that serves 2.  This one charges the real price.
    """

    def __init__(self, path, stream, *, accept_interval: float = 0.5) -> None:
        self.path = str(path)
        self.stream = stream
        self.accept_interval = accept_interval
        self.served = 0
        self._stop = threading.Event()
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(self.path)
        self._sock.listen(8)
        self._sock.setblocking(False)
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self._sock.close()
        with contextlib.suppress(OSError):
            os.unlink(self.path)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except (BlockingIOError, OSError):
                self._stop.wait(self.accept_interval)
                continue
            with conn, contextlib.suppress(OSError):
                conn.settimeout(1.0)
                conn.recv(256)
                conn.sendall(json.dumps(self.stream(self.path)).encode() + b"\n")
                self.served += 1


def test_the_collector_closes_against_outputds_real_throughput(
    short_sock_path, monkeypatch
) -> None:
    # jts3's numbers against jts3's socket. 232 readings at spread 86 are
    # unreachable by polling — the blocker that killed round one — and reachable
    # from the ring at the same two reads a second.
    #
    # Real wall clock here, not the fake one: the point is the socket's own
    # pace. The writer's cadence is driven off that same clock so the readings
    # accrue at jts3's 46.9 writes/s, and the budget is trimmed to 20 s so a
    # wedge fails this test well before it wedges the suite.
    monkeypatch.setattr(aec_init, "require_outputd_env_loaded", lambda **_kwargs: None)
    # accept_wait/reply_latency are zero here because the REAL server and the
    # REAL socket spend that time; the fake clock's versions of them exist only
    # to model it where there is no server.
    stream = _WriterStream(
        _LiveClock(),
        mix_period_frames=1024,
        delays=COARSE_CADENCE_DELAYS,
        lag=1,
        accept_wait=0.0,
        reply_latency=0.0,
    )

    started = time.monotonic()
    with _ThrottledStatusServer(short_sock_path, stream) as server:
        _status, queue = aec_init.collect_reference_queue(
            REFERENCE_PCM, socket_path=short_sock_path, timeout=20.0
        )
    elapsed = time.monotonic() - started

    assert max(queue) - min(queue) == 86
    assert len(queue) >= alignment.required_queue_samples(86)
    assert elapsed < 20.0
    # The ceiling this test exists to charge: about two reads a second. Round
    # one needed one read per reading and could never have cleared it.
    assert server.served <= 2 * elapsed + 2
    assert server.served < alignment.required_queue_samples(86)
