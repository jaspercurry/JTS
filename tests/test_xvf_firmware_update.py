# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import signal
import time
from pathlib import Path

import pytest

from jasper.cli import xvf_firmware_update
from jasper.mics import xvf3800


ROOT = Path(__file__).resolve().parents[1]
UNIT_PATH = ROOT / "deploy/systemd/jasper-xvf-firmware-update.service"


def _unit_timeout_start_sec() -> float:
    for line in UNIT_PATH.read_text(encoding="utf-8").splitlines():
        if line.startswith("TimeoutStartSec="):
            return float(line.partition("=")[2])
    raise AssertionError(f"{UNIT_PATH} has no TimeoutStartSec")


class _FakeResponse:
    def __init__(self, payload: bytes, *, content_length: str | None = None) -> None:
        self._payload = payload
        self._offset = 0
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = content_length

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args) -> None:
        return None

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            n = len(self._payload) - self._offset
        chunk = self._payload[self._offset:self._offset + n]
        self._offset += len(chunk)
        return chunk


def _runtime_profile(
    variant: xvf3800.FirmwareVariant,
) -> xvf3800.RuntimeProfile:
    return xvf3800.RuntimeProfile(
        present=True,
        variant=variant,
        alsa_card_name=variant.alsa_card_name,
        capture_channels=variant.capture_channels,
        chip_beam_plan=None,
        reason="test",
    )


def _target_for_payload(payload: bytes, *, expected_size: int | None = None):
    return xvf3800.FirmwareUpdateTarget(
        target_id="test",
        from_variant_ids=(xvf3800.VARIANT_2CH.variant_id,),
        to_variant_id=xvf3800.VARIANT_6CH.variant_id,
        label="Test firmware",
        geometry="square",
        filename="test.bin",
        url="https://example.invalid/test.bin",
        sha256=hashlib.sha256(payload).hexdigest(),
        expected_size_bytes=len(payload) if expected_size is None else expected_size,
        upstream_dir_url="https://example.invalid/",
    )


def test_unit_timeout_outlasts_the_firmware_operation_budget() -> None:
    """systemd must not kill dfu-util during a valid post-download update."""
    unit_timeout = _unit_timeout_start_sec()
    required = (
        xvf_firmware_update.PRE_FLASH_TIMEOUT_BUDGET_SEC
        + xvf_firmware_update.POST_DOWNLOAD_TIMEOUT_BUDGET_SEC
        + 30.0
    )
    assert unit_timeout >= required, (
        f"firmware updater TimeoutStartSec={unit_timeout:g}s must cover its "
        f"{xvf_firmware_update.PRE_FLASH_TIMEOUT_BUDGET_SEC:g}s pre-flash limit, "
        f"{xvf_firmware_update.POST_DOWNLOAD_TIMEOUT_BUDGET_SEC:g}s handled "
        "post-download path, and scheduling margin"
    )
    assert (
        xvf_firmware_update.DOWNLOAD_TOTAL_TIMEOUT_SEC
        < xvf_firmware_update.PRE_FLASH_TIMEOUT_BUDGET_SEC
    )


def test_download_has_a_real_total_deadline(monkeypatch, tmp_path: Path) -> None:
    """A slow trickle cannot consume the unit clock and expose a mid-DFU kill."""

    class _SlowResponse(_FakeResponse):
        def read(self, n: int = -1) -> bytes:
            try:
                time.sleep(1.0)
            except OSError:
                # The deadline interrupt must not look like a socket error;
                # urllib is allowed to catch those inside its read machinery.
                return b""
            return super().read(n)

    payload = b"firmware"
    target = _target_for_payload(payload)
    monkeypatch.setattr(xvf_firmware_update, "DOWNLOAD_TOTAL_TIMEOUT_SEC", 0.01)
    monkeypatch.setattr(
        xvf_firmware_update.urllib.request,
        "urlopen",
        lambda *a, **kw: _SlowResponse(payload),
    )

    with pytest.raises(TimeoutError, match="total deadline"):
        xvf_firmware_update._download_and_verify(target, tmp_path / "fw.bin")


def test_download_deadline_restores_prior_signal_state(monkeypatch) -> None:
    old_handler = object()
    signal_calls = []
    timer_calls = []
    monotonic_values = iter((100.0, 101.0))

    def fake_signal(sig, handler):
        signal_calls.append((sig, handler))
        return old_handler

    def fake_setitimer(which, seconds, interval=0.0):
        timer_calls.append((which, seconds, interval))
        if len(timer_calls) == 1:
            return (5.0, 2.0)
        return (0.0, 0.0)

    monkeypatch.setattr(xvf_firmware_update.signal, "signal", fake_signal)
    monkeypatch.setattr(xvf_firmware_update.signal, "setitimer", fake_setitimer)
    monkeypatch.setattr(
        xvf_firmware_update.time,
        "monotonic",
        lambda: next(monotonic_values),
    )

    with xvf_firmware_update._download_deadline():
        pass

    assert signal_calls[0][0] == signal.SIGALRM
    assert callable(signal_calls[0][1])
    assert signal_calls[1] == (signal.SIGALRM, old_handler)
    assert timer_calls == [
        (signal.ITIMER_REAL, xvf_firmware_update.DOWNLOAD_TOTAL_TIMEOUT_SEC, 0.0),
        (signal.ITIMER_REAL, 0, 0.0),
        (signal.ITIMER_REAL, 4.0, 2.0),
    ]


def test_update_refuses_to_enter_dfu_after_pre_flash_budget(
    monkeypatch,
    tmp_path: Path,
) -> None:
    profile = _runtime_profile(xvf3800.VARIANT_2CH)
    target = xvf3800.FIRMWARE_UPDATE_TARGETS_BY_ID["legacy_square_6ch"]
    monotonic_values = iter((0.0, xvf_firmware_update.PRE_FLASH_TIMEOUT_BUDGET_SEC + 1))

    monkeypatch.setattr(xvf_firmware_update, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(xvf3800, "detect_runtime_profile", lambda: profile)
    monkeypatch.setattr(
        xvf3800,
        "firmware_update_target_for_profile",
        lambda _profile: target,
    )
    monkeypatch.setattr(
        xvf_firmware_update,
        "_download_and_verify",
        lambda _target, _dest: target.sha256,
    )
    monkeypatch.setattr(
        xvf_firmware_update.time,
        "monotonic",
        lambda: next(monotonic_values),
    )
    monkeypatch.setattr(
        xvf_firmware_update,
        "_run",
        lambda *_args, **_kwargs: pytest.fail("must not stop services"),
    )
    monkeypatch.setattr(
        xvf_firmware_update,
        "_run_dfu_flash",
        lambda *_args, **_kwargs: pytest.fail("must not enter DFU"),
    )

    with pytest.raises(TimeoutError, match="refusing to start microphone flash"):
        xvf_firmware_update.update()


def test_download_and_verify_rejects_content_length_mismatch(
    monkeypatch, tmp_path: Path,
) -> None:
    payload = b"firmware"
    target = _target_for_payload(payload, expected_size=len(payload) + 1)
    monkeypatch.setattr(
        xvf_firmware_update.urllib.request,
        "urlopen",
        lambda *a, **kw: _FakeResponse(payload, content_length=str(len(payload))),
    )

    with pytest.raises(RuntimeError, match="size mismatch before read"):
        xvf_firmware_update._download_and_verify(target, tmp_path / "fw.bin")


def test_download_and_verify_rejects_stream_larger_than_manifest(
    monkeypatch, tmp_path: Path,
) -> None:
    payload = b"firmware"
    target = _target_for_payload(payload, expected_size=len(payload) - 1)
    monkeypatch.setattr(
        xvf_firmware_update.urllib.request,
        "urlopen",
        lambda *a, **kw: _FakeResponse(payload),
    )

    with pytest.raises(RuntimeError, match="exceeded expected size"):
        xvf_firmware_update._download_and_verify(target, tmp_path / "fw.bin")


def test_failed_flash_after_service_stop_reconciles_before_raising(
    monkeypatch, tmp_path: Path,
) -> None:
    calls: list[tuple[str, ...]] = []
    profile = _runtime_profile(xvf3800.VARIANT_2CH)
    target = xvf3800.FIRMWARE_UPDATE_TARGETS_BY_ID["legacy_square_6ch"]

    monkeypatch.setattr(xvf_firmware_update, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(xvf3800, "detect_runtime_profile", lambda: profile)
    monkeypatch.setattr(
        xvf3800, "firmware_update_target_for_profile", lambda p: target,
    )
    monkeypatch.setattr(
        xvf_firmware_update,
        "_download_and_verify",
        lambda target, dest: target.sha256,
    )

    def fake_run(argv, *, timeout=60.0):
        calls.append(tuple(argv))
        return None

    def fail_flash(path):
        raise RuntimeError("flash boom")

    monkeypatch.setattr(xvf_firmware_update, "_run", fake_run)
    monkeypatch.setattr(xvf_firmware_update, "_run_dfu_flash", fail_flash)

    with pytest.raises(RuntimeError, match="flash boom"):
        xvf_firmware_update.update()

    assert ("systemctl", "stop", *xvf_firmware_update.UPDATE_UNITS) in calls
    assert (
        "systemctl",
        "restart",
        xvf_firmware_update.RECONCILE_UNIT,
    ) in calls
