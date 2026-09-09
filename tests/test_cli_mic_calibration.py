# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""``jasper-mic-calibration`` — the door that registers the household mic.

One behavior per verb: what each publishes, and that the two writing verbs
are the only thing that moves the durable record
(``jasper.audio_measurement.household_mic``). The vendor lookup is faked at
the function boundary, so no test here reaches the network.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from jasper.audio_measurement import calibration
from jasper.audio_measurement.household_mic import read_household_mic
from jasper.cli import _refusal, mic_calibration

SAMPLE_CAL = "20 -1\n100 0\n1000 1\n"


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Both durable locations under tmp_path; the record path is returned."""
    record_path = tmp_path / "household_mic.json"
    monkeypatch.setenv("JASPER_CORRECTION_HOUSEHOLD_MIC_PATH", str(record_path))
    monkeypatch.setenv("JASPER_CORRECTION_CALIBRATION_DIR", str(tmp_path / "cal"))
    return record_path


def _run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
    code = mic_calibration.main(argv)
    return code, json.loads(capsys.readouterr().out)


def test_models_lists_every_model_the_fetchers_know(capsys):
    code, answer = _run(["models"], capsys)

    assert code == _refusal.EXIT_OK
    assert {model["key"] for model in answer["models"]} == set(
        calibration.SUPPORTED_MODELS
    )
    assert all(model["provider"] for model in answer["models"])


def test_fetch_stores_the_vendor_calibration_and_remembers_the_mic(
    store: Path, monkeypatch, capsys,
):
    """The vendor lookup is the seam; everything downstream of it is real."""

    def fake_fetch(*, model_key, serial, orientation, root, opener=None):
        return calibration.store_calibration(
            text=SAMPLE_CAL, provider="dayton_audio", model=model_key,
            label="Dayton Audio iMM-6 / iMM-6C",
            source="https://vendor.example/cal.txt", serial=serial,
            orientation=orientation, root=root,
        )

    monkeypatch.setattr(calibration, "fetch_vendor_calibration", fake_fetch)

    code, answer = _run(
        ["fetch", "--model", "dayton_imm6", "--serial", "700-1234",
         "--orientation", "0deg"],
        capsys,
    )

    assert code == _refusal.EXIT_OK
    assert answer["calibration"]["provider"] == "dayton_audio"
    assert answer["calibration"]["point_count"] == 3
    stored = read_household_mic(path=store)
    assert stored is not None
    assert stored.model_key == "dayton_imm6"
    assert stored.serial_display == "1234"  # last 4 only; the raw serial never lands
    assert answer["household_mic"] == stored.to_dict()


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (calibration.CalibrationNotFoundError, mic_calibration.REFUSE_VENDOR_NOT_FOUND),
        (
            calibration.CalibrationUpstreamError,
            mic_calibration.REFUSE_VENDOR_UNREACHABLE,
        ),
        (ValueError, mic_calibration.REFUSE_LOOKUP_INVALID),
    ],
)
def test_a_failed_vendor_lookup_refuses_by_name_and_writes_nothing(
    store: Path, monkeypatch, capsys, error, reason,
):
    def fake_fetch(**_kwargs):
        raise error("the vendor said no")

    monkeypatch.setattr(calibration, "fetch_vendor_calibration", fake_fetch)

    code, document = _run(
        ["fetch", "--model", "dayton_imm6", "--serial", "700-1234"], capsys
    )

    assert code == _refusal.EXIT_REFUSED
    assert document["reason"] == reason
    assert document["status"] == "refused"
    assert not store.exists()


def test_upload_stores_a_local_file_and_remembers_the_mic(store: Path, tmp_path, capsys):
    path = tmp_path / "lab.txt"
    path.write_text(SAMPLE_CAL)

    code, answer = _run(["upload", str(path), "--label", "Lab mic"], capsys)

    assert code == _refusal.EXIT_OK
    assert answer["calibration"]["provider"] == "manual_upload"
    # A measurement-mic file states the mic's RESPONSE unless told otherwise,
    # so the mic reading 1 dB low at 20 Hz becomes a +1 dB correction.
    assert answer["calibration"]["sign_convention"] == "response"
    stored = read_household_mic(path=store)
    assert stored is not None
    assert stored.label == "Lab mic"
    assert stored.serial_display is None  # an upload need carry no serial


def test_upload_of_something_that_is_not_a_calibration_is_unreadable(
    store: Path, tmp_path, capsys,
):
    path = tmp_path / "bad.txt"
    path.write_text("this is not a calibration file")

    code, document = _run(["upload", str(path)], capsys)

    assert code == _refusal.EXIT_UNREADABLE
    assert document["reason"] == mic_calibration.REASON_FILE_UNREADABLE
    assert not store.exists()


def test_show_prints_the_record_and_its_resolved_calibration(
    store: Path, tmp_path, capsys,
):
    path = tmp_path / "lab.txt"
    path.write_text(SAMPLE_CAL)
    assert mic_calibration.main(["upload", str(path)]) == _refusal.EXIT_OK
    capsys.readouterr()

    code, answer = _run(["show"], capsys)

    assert code == _refusal.EXIT_OK
    stored = read_household_mic(path=store)
    assert stored is not None
    assert answer["household_mic"] == stored.to_dict()
    assert answer["calibration"]["calibration_id"] == stored.calibration_id
    assert answer["record_path"] == str(store)


def test_show_separates_no_record_from_a_record_whose_calibration_is_gone(
    store: Path, tmp_path, capsys,
):
    """Two states an operator acts on differently: register a mic, or
    register it again because the stored curve is no longer on disk."""
    code, document = _run(["show"], capsys)
    assert code == _refusal.EXIT_REFUSED
    assert document["reason"] == mic_calibration.REFUSE_NONE_REGISTERED

    path = tmp_path / "lab.txt"
    path.write_text(SAMPLE_CAL)
    assert mic_calibration.main(["upload", str(path)]) == _refusal.EXIT_OK
    capsys.readouterr()
    for stale in (tmp_path / "cal").rglob("*.json"):
        stale.unlink()

    code, document = _run(["show"], capsys)

    assert code == _refusal.EXIT_REFUSED
    assert document["reason"] == mic_calibration.REFUSE_UNRESOLVABLE
    assert document["detail"]["record_path"] == str(store)
