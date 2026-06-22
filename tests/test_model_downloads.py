# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from jasper import model_downloads


class _FakeResponse:
    def __init__(self, payload: bytes, *, content_length: str | None = None):
        self._payload = payload
        self._offset = 0
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = content_length

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self, size: int) -> bytes:
        if self._offset >= len(self._payload):
            return b""
        chunk = self._payload[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def test_download_model_file_writes_verified_file(monkeypatch, tmp_path: Path):
    payload = b"onnx payload"
    calls = []

    def fake_urlopen(request, *, timeout):
        calls.append((request, timeout))
        return _FakeResponse(payload, content_length=str(len(payload)))

    monkeypatch.setattr(model_downloads.urllib.request, "urlopen", fake_urlopen)
    dest = tmp_path / "model.onnx"

    model_downloads.download_model_file(
        "https://example.invalid/model.onnx",
        dest,
        expected_sha256=_sha(payload),
        label="test model",
        timeout_seconds=7.0,
        retries=1,
        log=None,
    )

    assert dest.read_bytes() == payload
    assert not (tmp_path / "model.onnx.tmp").exists()
    assert calls[0][0].get_full_url() == "https://example.invalid/model.onnx"
    assert calls[0][0].get_header("User-agent") == "JTS-install"
    assert calls[0][1] == 7.0
    assert dest.stat().st_mode & 0o777 == 0o644


def test_download_model_file_retries_then_succeeds(monkeypatch, tmp_path: Path):
    payload = b"retry payload"
    calls = {"n": 0}

    def fake_urlopen(request, *, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("slow server")
        return _FakeResponse(payload)

    monkeypatch.setattr(model_downloads.urllib.request, "urlopen", fake_urlopen)
    dest = tmp_path / "model.onnx"

    model_downloads.download_model_file(
        "https://example.invalid/model.onnx",
        dest,
        expected_sha256=_sha(payload),
        label="test model",
        retries=2,
        retry_backoff_seconds=0,
        log=None,
    )

    assert calls["n"] == 2
    assert dest.read_bytes() == payload


def test_download_model_file_rejects_hash_mismatch(monkeypatch, tmp_path: Path):
    def fake_urlopen(request, *, timeout):
        return _FakeResponse(b"wrong")

    monkeypatch.setattr(model_downloads.urllib.request, "urlopen", fake_urlopen)
    dest = tmp_path / "model.onnx"

    with pytest.raises(model_downloads.ModelDownloadError, match="hash mismatch"):
        model_downloads.download_model_file(
            "https://example.invalid/model.onnx",
            dest,
            expected_sha256=_sha(b"expected"),
            label="test model",
            retries=1,
            log=None,
        )

    assert not dest.exists()
    assert not Path(f"{dest}.tmp").exists()


def test_download_model_file_rejects_declared_oversize(
    monkeypatch,
    tmp_path: Path,
):
    def fake_urlopen(request, *, timeout):
        return _FakeResponse(b"", content_length="9")

    monkeypatch.setattr(model_downloads.urllib.request, "urlopen", fake_urlopen)
    dest = tmp_path / "model.onnx"

    with pytest.raises(model_downloads.ModelDownloadError, match="Content-Length"):
        model_downloads.download_model_file(
            "https://example.invalid/model.onnx",
            dest,
            expected_sha256=None,
            label="test model",
            max_bytes=8,
            retries=1,
            log=None,
        )

    assert not dest.exists()


def test_download_model_file_rejects_streamed_oversize(
    monkeypatch,
    tmp_path: Path,
):
    def fake_urlopen(request, *, timeout):
        return _FakeResponse(b"abcdefghi")

    monkeypatch.setattr(model_downloads.urllib.request, "urlopen", fake_urlopen)
    dest = tmp_path / "model.onnx"

    with pytest.raises(model_downloads.ModelDownloadError, match="max_bytes"):
        model_downloads.download_model_file(
            "https://example.invalid/model.onnx",
            dest,
            expected_sha256=None,
            label="test model",
            max_bytes=8,
            retries=1,
            log=None,
        )

    assert not dest.exists()
    assert not Path(f"{dest}.tmp").exists()


def test_download_model_file_validates_bounds(tmp_path: Path):
    with pytest.raises(ValueError, match="retries"):
        model_downloads.download_model_file(
            "https://example.invalid/model.onnx",
            tmp_path / "model.onnx",
            expected_sha256=None,
            label="test model",
            retries=0,
        )
