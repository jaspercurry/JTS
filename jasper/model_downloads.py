# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Bounded downloads for opaque runtime model files.

Installer code downloads a small number of ONNX assets that the runtime
loads directly. Keep those fetches explicit and bounded: no indefinite
socket waits, no unbounded response bodies, and no staging without a
hash check when a SHA-256 is available.

A leaf module: it does not know which registries (wake models,
openWakeWord assets, DTLN bundles, ...) exist. Each registry builds its
own `list[StageAsset]` and calls `stage_model_assets`; the CLI wiring a
`--registry` name to its provider lives in `jasper.cli.model_downloads`,
which is free to import those registries. See #4726.
"""
from __future__ import annotations

import http.client
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from jasper.env_load import BASE_ENV_PATH, parse_env_file
from jasper.json_fields import sha256_file


DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_RETRIES = 3
DEFAULT_MAX_BYTES = 64 * 1024 * 1024
DEFAULT_CHUNK_BYTES = 64 * 1024


class ModelDownloadError(RuntimeError):
    """A model asset download failed after the configured bounded retries."""


@dataclass(frozen=True)
class StageAsset:
    """One model-like asset that install.sh may stage."""

    key: str
    label: str
    dest: Path
    url: str
    expected_sha256: str | None
    required: bool


@dataclass(frozen=True)
class StageResult:
    required_failures: int = 0
    optional_failures: int = 0

    @property
    def failures(self) -> int:
        return self.required_failures + self.optional_failures


def download_model_file(
    url: str,
    dest: str | os.PathLike[str],
    *,
    expected_sha256: str | None,
    label: str,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    retries: int = DEFAULT_RETRIES,
    max_bytes: int = DEFAULT_MAX_BYTES,
    log: Callable[[str], None] | None = print,
    retry_backoff_seconds: float = 1.0,
) -> None:
    """Download ``url`` to ``dest`` atomically with bounded retries.

    The caller owns required-vs-optional policy. This helper only
    guarantees that a single asset fetch has explicit time and byte
    limits and that a staged file matches ``expected_sha256`` when one
    is provided.
    """
    if retries < 1:
        raise ValueError("retries must be >= 1")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be > 0")
    if max_bytes <= 0:
        raise ValueError("max_bytes must be > 0")

    dest_path = Path(dest)
    tmp_path = Path(f"{dest_path}.tmp")
    last_error: BaseException | None = None
    for attempt in range(1, retries + 1):
        _log(
            log,
            f"  download attempt {attempt}/{retries}: {label} "
            f"(timeout={timeout_seconds:g}s, max={max_bytes} bytes)",
        )
        try:
            _download_once(
                url,
                tmp_path,
                timeout_seconds=timeout_seconds,
                max_bytes=max_bytes,
            )
            if expected_sha256 is not None:
                got = sha256_file(tmp_path)
                if got != expected_sha256:
                    raise ModelDownloadError(
                        f"hash mismatch after download: got {got}, "
                        f"expected {expected_sha256}",
                    )
            os.chmod(tmp_path, 0o644)
            os.replace(tmp_path, dest_path)
            return
        except (
            OSError,
            TimeoutError,
            http.client.HTTPException,
            ssl.SSLError,
            urllib.error.URLError,
            urllib.error.HTTPError,
            ModelDownloadError,
        ) as exc:
            last_error = exc
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
            _log(log, f"  failed attempt {attempt}/{retries}: {label}: {exc}")
            if attempt < retries and retry_backoff_seconds > 0:
                time.sleep(retry_backoff_seconds)

    raise ModelDownloadError(
        f"{label} download failed after {retries} attempt(s): {last_error}",
    )


def stage_model_assets(
    assets: list[StageAsset],
    *,
    required_timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    required_retries: int = DEFAULT_RETRIES,
    optional_timeout_seconds: float = 20.0,
    optional_retries: int = 1,
    max_bytes: int = DEFAULT_MAX_BYTES,
    downloader: Callable[..., None] = download_model_file,
    hasher: Callable[[str | os.PathLike[str]], str] = sha256_file,
    log: Callable[[str], None] | None = print,
    err_log: Callable[[str], None] | None = lambda msg: print(msg, file=sys.stderr),
) -> StageResult:
    """Stage registered model assets with shared exists/hash/download policy."""
    required_failures = 0
    optional_failures = 0
    for asset in assets:
        dest = asset.dest
        if dest.exists() and dest.stat().st_size > 0:
            if asset.expected_sha256 is None or hasher(dest) == asset.expected_sha256:
                _log(log, f"  {asset.label} present: {dest.name}")
                continue
            _log(log, f"  {asset.label} hash mismatch, re-downloading: {dest.name}")
            dest.unlink()

        _log(log, f"  downloading {asset.label}: {dest.name}")
        _log(log, f"    from: {asset.url}")
        _log(log, f"    to:   {dest}")
        try:
            downloader(
                asset.url,
                dest,
                expected_sha256=asset.expected_sha256,
                label=f"{asset.label} {dest.name}",
                timeout_seconds=(
                    required_timeout_seconds
                    if asset.required
                    else optional_timeout_seconds
                ),
                retries=required_retries if asset.required else optional_retries,
                max_bytes=max_bytes,
            )
        except ModelDownloadError as exc:
            kind = "required" if asset.required else "optional"
            _log(err_log, f"  {kind} {asset.label} failed: {dest.name}: {exc}")
            if asset.required:
                required_failures += 1
            else:
                optional_failures += 1

    return StageResult(
        required_failures=required_failures,
        optional_failures=optional_failures,
    )


def active_wake_model(
    *,
    env: Mapping[str, str] = os.environ,
    jasper_env_path: str = BASE_ENV_PATH,
    wake_env_path: str = "/var/lib/jasper/wake_model.env",
) -> str:
    model = env.get("JASPER_WAKE_MODEL", "").strip()
    model = parse_env_file(jasper_env_path).get("JASPER_WAKE_MODEL", model).strip()
    model = parse_env_file(wake_env_path).get("JASPER_WAKE_MODEL", model).strip()
    return model or "hey_jarvis"


def _download_once(
    url: str,
    tmp_path: Path,
    *,
    timeout_seconds: float,
    max_bytes: int,
) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "JTS-install"})
    total = 0
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        declared = response.headers.get("Content-Length")
        if declared is not None:
            try:
                declared_bytes = int(declared)
            except ValueError:
                declared_bytes = 0
            if declared_bytes > max_bytes:
                raise ModelDownloadError(
                    f"declared Content-Length={declared_bytes} exceeds "
                    f"max_bytes={max_bytes}",
                )
        with tmp_path.open("wb") as out:
            while True:
                chunk = response.read(DEFAULT_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ModelDownloadError(
                        f"response exceeded max_bytes={max_bytes}",
                    )
                out.write(chunk)


def _log(log: Callable[[str], None] | None, msg: str) -> None:
    if log is not None:
        log(msg)
