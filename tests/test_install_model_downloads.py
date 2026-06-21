# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for installer-facing model staging helpers."""
from __future__ import annotations

from pathlib import Path

from jasper.model_downloads import (
    DEFAULT_MAX_BYTES,
    ModelDownloadError,
    StageAsset,
    openwakeword_stage_assets,
    stage_model_assets,
)


def _asset(path: Path, *, required: bool = True) -> StageAsset:
    return StageAsset(
        key=path.name,
        label="test model",
        dest=path,
        url="https://example.invalid/model.onnx",
        expected_sha256="expected",
        required=required,
    )


def test_stage_skips_present_file_with_matching_hash(tmp_path: Path) -> None:
    dest = tmp_path / "model.onnx"
    dest.write_bytes(b"already here")
    calls: list[object] = []

    result = stage_model_assets(
        [_asset(dest)],
        downloader=lambda *args, **kwargs: calls.append((args, kwargs)),
        hasher=lambda path: "expected",
        log=None,
    )

    assert result.failures == 0
    assert calls == []
    assert dest.read_bytes() == b"already here"


def test_stage_redownloads_hash_mismatch_with_required_limits(tmp_path: Path) -> None:
    dest = tmp_path / "model.onnx"
    dest.write_bytes(b"stale")
    calls: list[dict[str, object]] = []

    def downloader(url: str, path: Path, **kwargs: object) -> None:
        calls.append({"url": url, "path": path, **kwargs})
        path.write_bytes(b"fresh")

    result = stage_model_assets(
        [_asset(dest, required=True)],
        downloader=downloader,
        hasher=lambda path: "wrong",
        log=None,
    )

    assert result.failures == 0
    assert dest.read_bytes() == b"fresh"
    assert calls == [
        {
            "url": "https://example.invalid/model.onnx",
            "path": dest,
            "expected_sha256": "expected",
            "label": "test model model.onnx",
            "timeout_seconds": 30.0,
            "retries": 3,
            "max_bytes": DEFAULT_MAX_BYTES,
        }
    ]


def test_stage_uses_optional_retry_policy_and_counts_failures(tmp_path: Path) -> None:
    required = _asset(tmp_path / "required.onnx", required=True)
    optional = _asset(tmp_path / "optional.onnx", required=False)
    calls: list[tuple[str, int, int]] = []

    def downloader(url: str, path: Path, **kwargs: object) -> None:
        calls.append((
            path.name,
            int(kwargs["retries"]),
            int(kwargs["max_bytes"]),
        ))
        raise ModelDownloadError("nope")

    result = stage_model_assets(
        [required, optional],
        downloader=downloader,
        log=None,
        err_log=None,
    )

    assert result.required_failures == 1
    assert result.optional_failures == 1
    assert calls == [
        ("required.onnx", 3, DEFAULT_MAX_BYTES),
        ("optional.onnx", 1, DEFAULT_MAX_BYTES),
    ]


def test_openwakeword_runtime_fallback_and_active_assets_are_required(
    tmp_path: Path,
) -> None:
    assets = openwakeword_stage_assets(tmp_path, active_model="alexa")
    required = {asset.key for asset in assets if asset.required}
    optional = {asset.key for asset in assets if not asset.required}

    assert {"embedding_model", "melspectrogram", "silero_vad"} <= required
    assert "hey_jarvis" in required
    assert "alexa" in required
    assert "weather" in optional
