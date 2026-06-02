"""Bounded downloads for opaque runtime model files.

Installer code downloads a small number of ONNX assets that the runtime
loads directly. Keep those fetches explicit and bounded: no indefinite
socket waits, no unbounded response bodies, and no staging without a
hash check when a SHA-256 is available.
"""
from __future__ import annotations

import hashlib
import http.client
import os
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path


DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_RETRIES = 3
DEFAULT_MAX_BYTES = 64 * 1024 * 1024
DEFAULT_CHUNK_BYTES = 64 * 1024


class ModelDownloadError(RuntimeError):
    """A model asset download failed after the configured bounded retries."""


def sha256_file(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(DEFAULT_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
            os.replace(tmp_path, dest_path)
            os.chmod(dest_path, 0o644)
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
