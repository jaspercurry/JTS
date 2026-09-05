# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The declared hardware mixer pins: registry, applier, and doctor verdict."""

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from jasper.audio_hardware.dac import (
    APPLE_USB_C_DONGLE_ID,
    DUAL_APPLE_USB_C_DAC_4CH_ID,
    HIFIBERRY_DAC8X_STUDIO_ID,
    HIFIBERRY_STUDIO_MIXER_CONTROLS,
    MixerControl,
)
from jasper.cli.doctor import audio
from jasper import output_hardware
from jasper.output_hardware import (
    OutputCardFact,
    OutputHardwareState,
    write_state,
)

ROOT = Path(__file__).resolve().parents[1]
DAC_INIT = ROOT / "deploy/bin/jasper-dac-init"

# The HiFiBerry Studio DAC8x kcontrols exactly as the driver declares them in
# sound/soc/bcm/hifiberry_studio_dac8x.c (raspberrypi/linux, rpi-6.12.y):
# (value_min, value_max, tlv_min_centidb, tlv_max_centidb, register_invert).
# The TLVs are DECLARE_TLV_DB_MINMAX(volume_tlv, -10300, 2400) for the master
# and DECLARE_TLV_DB_MINMAX(spkr_tlv, -10300, 0) for the eight output
# channels, and hb_uni_vol_ctls_single carries the ranges and the invert.
STUDIO_VOLUME_FACTS = {
    "Master Playback Volume": (0, 254, -10300, 2400, True),
    **{
        f"Output Ch{channel} Playback Volume": (0, 206, -10300, 0, True)
        for channel in range(8)
    },
}
STUDIO_MUTE_ITEMS = ("unmuted", "muted")
# DB_MINMAX is linear in dB across the control's OWN value range, and the
# driver applies its invert below the userspace value, so unity is the same
# index on both scales: -103 + 127*206/254 == -103 + 103*206/206 == 0.00 dB.
STUDIO_UNITY_INDEX = 206

FAKE_AMIXER = """#!/usr/bin/env python3
import json
import os
import pathlib
import sys

argv = sys.argv[1:]
pathlib.Path(os.environ["FAKE_AMIXER_LOG"]).open("a").write("\\t".join(argv) + "\\n")
if "cget" in argv:
    name = next(arg[5:] for arg in argv if arg.startswith("name="))
    canned = json.loads(pathlib.Path(os.environ["FAKE_AMIXER_CGET"]).read_text())
    if name not in canned:
        sys.exit(1)
    sys.stdout.write(canned[name])
sys.exit(0)
"""


def cget_integer(
    value: int, value_min: int, value_max: int, db_min: int, db_max: int
) -> str:
    """`amixer cget` output for a TLV-readable integer control."""

    return (
        f"  ; type=INTEGER,access=rw---R--,values=1,"
        f"min={value_min},max={value_max},step=1\n"
        f"  : values={value}\n"
        f"  | dBminmax-min={db_min / 100:.2f}dB,max={db_max / 100:.2f}dB\n"
    )


def cget_enum(value: int, items: tuple[str, ...]) -> str:
    lines = [f"  ; type=ENUMERATED,access=rw------,values=1,items={len(items)}"]
    lines += [f"  ; Item #{index} '{name}'" for index, name in enumerate(items)]
    lines += [f"  : values={value}", ""]
    return "\n".join(lines)


def studio_cget(
    *, volumes: dict[str, int] | None = None, mute_item: int = 0
) -> dict[str, str]:
    """Canned `cget` output for every Studio control, pinned unless overridden."""

    volumes = volumes or {}
    canned = {
        name: cget_integer(
            volumes.get(name, STUDIO_UNITY_INDEX), value_min, value_max, db_min, db_max
        )
        for name, (value_min, value_max, db_min, db_max, _) in
        STUDIO_VOLUME_FACTS.items()
    }
    # Only the doctor reads this one back; the applier writes the item by name.
    canned["DAC Mute"] = cget_enum(mute_item, STUDIO_MUTE_ITEMS)
    return canned


def record_dac(profile_id: str, card_id: str, *, children: int = 0) -> None:
    write_state(
        OutputHardwareState(
            profile_id=profile_id,
            profile_label=profile_id,
            status="ready",
            physical_output_count=8,
            selected_card_id=card_id,
            child_devices=tuple(
                OutputCardFact(card_id=f"{card_id}{index}", device_id=profile_id)
                for index in range(children)
            ),
        ),
        os.environ["JASPER_OUTPUT_HARDWARE_STATE_PATH"],
    )


def run_dac_init(
    tmp_path: Path, canned: dict[str, str], **overrides: str
) -> tuple[int, list[list[str]]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    amixer = bin_dir / "amixer"
    amixer.write_text(FAKE_AMIXER)
    amixer.chmod(0o755)
    cget_path = tmp_path / "cget.json"
    cget_path.write_text(json.dumps(canned))
    log = tmp_path / "amixer.log"
    env = dict(os.environ)
    env.update(
        PATH=f"{bin_dir}:{env['PATH']}",
        PYTHONPATH=str(ROOT),
        FAKE_AMIXER_LOG=str(log),
        FAKE_AMIXER_CGET=str(cget_path),
        **overrides,
    )
    before = log.read_text().splitlines() if log.exists() else []
    result = subprocess.run(
        ["/bin/bash", str(DAC_INIT)], env=env, capture_output=True, text=True, cwd=ROOT
    )
    after = log.read_text().splitlines() if log.exists() else []
    return result.returncode, [line.split("\t") for line in after[len(before):]]


# ------------------------------------------------------------------ registry


def test_studio_profile_declares_a_pin_for_every_hardware_gain_stage() -> None:
    names = [control.name for control in HIFIBERRY_STUDIO_MIXER_CONTROLS]
    assert names == [
        "Master Playback Volume",
        *(f"Output Ch{channel} Playback Volume" for channel in range(8)),
        "DAC Mute",
    ]
    assert all(
        control.target_db == 0.0 for control in HIFIBERRY_STUDIO_MIXER_CONTROLS[:-1]
    )
    assert HIFIBERRY_STUDIO_MIXER_CONTROLS[-1].target_enum == "unmuted"


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"target_percent": 100, "target_db": 0.0},
        {"target_db": 0.0, "target_enum": "unmuted"},
        # unmute is a simple-mixer switch, so it only rides with a percent.
        {"target_enum": "unmuted", "unmute": True},
    ],
)
def test_a_control_the_applier_could_not_apply_is_refused(kwargs) -> None:
    with pytest.raises(ValueError):
        MixerControl(name="Master Playback Volume", **kwargs)


@pytest.mark.parametrize("name,facts", sorted(STUDIO_VOLUME_FACTS.items()))
def test_unity_gain_lands_on_the_driver_derived_index(name, facts) -> None:
    """0 dB resolves to the index the driver's own TLV puts unity at."""

    value_min, value_max, db_min, db_max, _invert = facts
    span_db = (db_max - db_min) / 100
    assert (
        value_min + (0.0 - db_min / 100) * (value_max - value_min) / span_db
        == STUDIO_UNITY_INDEX
    )
    assert (
        output_hardware.mixer_index_for_db(
            cget_integer(0, value_min, value_max, db_min, db_max), 0.0
        )
        == STUDIO_UNITY_INDEX
    )


# ------------------------------------------------------------------- applier


def test_the_applier_pins_every_studio_control_at_unity(tmp_path) -> None:
    record_dac(HIFIBERRY_DAC8X_STUDIO_ID, "Studio")
    code, commands = run_dac_init(tmp_path, studio_cget())

    assert code == 0
    writes = [command for command in commands if "cset" in command]
    assert writes == [
        ["-c", "Studio", "cset", f"name={name}", str(STUDIO_UNITY_INDEX)]
        for name in STUDIO_VOLUME_FACTS
    ] + [["-c", "Studio", "cset", "name=DAC Mute", "unmuted"]]


def test_applying_the_pins_twice_writes_the_same_commands(tmp_path) -> None:
    record_dac(HIFIBERRY_DAC8X_STUDIO_ID, "Studio")
    first_code, first = run_dac_init(tmp_path, studio_cget())
    second_code, second = run_dac_init(tmp_path, studio_cget())

    assert (first_code, second_code) == (0, 0)
    assert first == second


def test_the_applier_keeps_the_dongles_simple_mixer_command(tmp_path) -> None:
    record_dac(APPLE_USB_C_DONGLE_ID, "AppleA")
    code, commands = run_dac_init(tmp_path, {})

    assert code == 0
    assert commands == [["-c", "AppleA", "--", "sset", "Headphone", "100%", "unmute"]]


def test_a_record_naming_no_pinned_dac_is_a_clean_skip(tmp_path) -> None:
    record_dac("hifiberry_dac8x", "Card")
    code, commands = run_dac_init(tmp_path, {})

    assert (code, commands) == (0, [])


@pytest.mark.parametrize(
    "tlv",
    [
        # A scale form this applier cannot convert.
        "  | dBscale-min=-103.00dB,step=0.50dB,mute=0\n",
        # Half a scale: read as a whole one, the master's unity target would
        # land on the top of the range instead — +24 dB on this hardware.
        "  | dBminmax-min=-103.00dB\n",
        "",
    ],
)
def test_a_scale_that_cannot_be_read_whole_fails_instead_of_guessing(
    tmp_path, tlv
) -> None:
    record_dac(HIFIBERRY_DAC8X_STUDIO_ID, "Studio")
    canned = studio_cget()
    canned["Master Playback Volume"] = (
        "  ; type=INTEGER,access=rw---R--,values=1,min=0,max=254,step=1\n"
        "  : values=0\n" + tlv
    )
    code, commands = run_dac_init(tmp_path, canned)

    assert code == 1
    assert not [
        command
        for command in commands
        if command[:4] == ["-c", "Studio", "cset", "name=Master Playback Volume"]
    ]


def test_an_absent_record_is_a_skip_but_an_unreadable_one_fails(tmp_path) -> None:
    """`load_state` answers None for both; only one of them is a box that has
    simply not reconciled yet, and the other leaves the gain stage unknown."""

    record = Path(os.environ["JASPER_OUTPUT_HARDWARE_STATE_PATH"])
    assert not record.exists()
    assert run_dac_init(tmp_path, {}) == (0, [])

    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text("{ not json")
    assert run_dac_init(tmp_path, {}) == (1, [])


def test_declared_pins_that_cannot_be_resolved_fail_rather_than_pass(tmp_path) -> None:
    """A registry probe that ran and failed leaves the pins unknown; the unit
    fails instead of reporting a boot that pinned nothing."""

    record_dac(HIFIBERRY_DAC8X_STUDIO_ID, "Studio")
    broken = tmp_path / "broken-python"
    broken.write_text("#!/usr/bin/env bash\nexit 1\n")
    broken.chmod(0o755)
    code, commands = run_dac_init(
        tmp_path, studio_cget(), JASPER_OUTPUT_HARDWARE_PYTHON=str(broken)
    )

    assert (code, commands) == (1, [])


# -------------------------------------------------------------------- doctor


def _doctor_amixer(canned: dict[str, str], sget: str = ""):
    def fake_run(cmd, *_args, **_kwargs):
        if "cget" in cmd:
            name = next(arg[5:] for arg in cmd if arg.startswith("name="))
            if name not in canned:
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            return SimpleNamespace(returncode=0, stdout=canned[name], stderr="")
        return SimpleNamespace(returncode=0, stdout=sget, stderr="")

    return fake_run


@pytest.mark.parametrize(
    "canned,expected",
    [
        (studio_cget(), "ok"),
        # One index below unity on the master is -0.5 dB, which is a pin that
        # did not hold like any other.
        (
            studio_cget(volumes={"Master Playback Volume": STUDIO_UNITY_INDEX - 1}),
            "fail",
        ),
        (studio_cget(volumes={"Master Playback Volume": 180}), "fail"),
        (studio_cget(volumes={"Output Ch5 Playback Volume": 0}), "fail"),
        (studio_cget(mute_item=1), "fail"),
        ({}, "fail"),
    ],
)
def test_doctor_fails_on_any_deviation_from_a_declared_pin(
    monkeypatch, canned, expected
) -> None:
    record_dac(HIFIBERRY_DAC8X_STUDIO_ID, "Studio")
    monkeypatch.setattr(audio, "_run", _doctor_amixer(canned))

    result = audio.check_dac_mixer_pins()

    assert (result.name, result.status) == ("DAC mixer pins", expected)


@pytest.mark.parametrize(
    "sget,expected",
    [
        ("Mono: Playback 120 [100%] [0.00dB] [on]\n", "ok"),
        ("Mono: Playback 90 [75%] [-10.00dB] [on]\n", "fail"),
        ("Mono: Playback 120 [100%] [0.00dB] [off]\n", "fail"),
        ("Mono: Playback 120\n", "fail"),
    ],
)
def test_doctor_reads_a_percent_pin_through_the_simple_mixer(
    monkeypatch, sget, expected
) -> None:
    record_dac(APPLE_USB_C_DONGLE_ID, "AppleA")
    calls: list[list[str]] = []

    def fake_run(cmd, *_args, **_kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout=sget, stderr="")

    monkeypatch.setattr(audio, "_run", fake_run)

    assert audio.check_dac_mixer_pins().status == expected
    assert calls == [["amixer", "-c", "AppleA", "sget", "Headphone"]]


def test_a_composite_missing_a_card_is_never_half_pinned() -> None:
    """Pairing a composite's control groups with fewer cards than it declares
    children would pin part of the pair and report success for the rest."""

    record_dac(DUAL_APPLE_USB_C_DAC_4CH_ID, "Apple", children=1)

    with pytest.raises(ValueError):
        output_hardware.mixer_pins_for_state(output_hardware.load_state())


def test_doctor_checks_every_card_of_a_composite_dac(monkeypatch) -> None:
    """A composite pairs one control group per child, so both dongles of the
    4-channel pair are read, and one drifted card fails the check."""

    record_dac(DUAL_APPLE_USB_C_DAC_4CH_ID, "Apple", children=2)
    calls: list[list[str]] = []

    def fake_run(cmd, *_args, **_kwargs):
        calls.append(cmd)
        low = cmd[2] == "Apple1"
        return SimpleNamespace(
            returncode=0,
            stdout=f"Mono: Playback 90 [{75 if low else 100}%] [0.00dB] [on]\n",
            stderr="",
        )

    monkeypatch.setattr(audio, "_run", fake_run)

    assert audio.check_dac_mixer_pins().status == "fail"
    assert calls == [
        ["amixer", "-c", "Apple0", "sget", "Headphone"],
        ["amixer", "-c", "Apple1", "sget", "Headphone"],
    ]


def test_doctor_skips_a_dac_that_declares_no_pins(monkeypatch) -> None:
    def fail_probe(*_args, **_kwargs):
        raise AssertionError("no mixer probe should run")

    record_dac("hifiberry_dac8x", "Card")
    monkeypatch.setattr(audio, "_run", fail_probe)

    assert audio.check_dac_mixer_pins().status == "skipped"
