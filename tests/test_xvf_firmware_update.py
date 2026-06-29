# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from jasper.cli import xvf_firmware_update
from jasper.mics import xvf3800


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
