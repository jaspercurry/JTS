"""Bounded downloads for opaque runtime model files.

Installer code downloads a small number of ONNX assets that the runtime
loads directly. Keep those fetches explicit and bounded: no indefinite
socket waits, no unbounded response bodies, and no staging without a
hash check when a SHA-256 is available.
"""
from __future__ import annotations

import argparse
import hashlib
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


def openwakeword_stage_assets(
    models_dir: str | os.PathLike[str],
    *,
    active_model: str | None = None,
) -> list[StageAsset]:
    from jasper.wake_models import (
        fallback_openwakeword_assets,
        openwakeword_asset_for_model,
        openwakeword_assets,
        required_openwakeword_assets,
    )

    required_by_key = {asset.key for asset in required_openwakeword_assets()}
    required_by_key.update(asset.key for asset in fallback_openwakeword_assets())
    if active_model:
        active_asset = openwakeword_asset_for_model(active_model)
        if active_asset is not None:
            required_by_key.add(active_asset.key)

    base = Path(models_dir)
    return [
        StageAsset(
            key=asset.key,
            label="openWakeWord asset",
            dest=base / asset.filename,
            url=asset.download_url,
            expected_sha256=asset.download_sha256,
            required=asset.key in required_by_key,
        )
        for asset in openwakeword_assets()
    ]


def wake_model_stage_assets(*, required: bool) -> list[StageAsset]:
    from jasper.wake_models import downloadable

    return [
        StageAsset(
            key=entry.key,
            label="wake model",
            dest=Path(entry.model),
            url=entry.download_url or "",
            expected_sha256=entry.download_sha256,
            required=required,
        )
        for entry in downloadable()
    ]


def dtln_stage_assets(*, required: bool) -> list[StageAsset]:
    from jasper.aec_engines.dtln_models import DTLN_MODELS_DIR, REGISTRY

    assets: list[StageAsset] = []
    for entry in REGISTRY:
        for path, url, expected_sha in entry.files(DTLN_MODELS_DIR):
            assets.append(
                StageAsset(
                    key=f"dtln-{entry.size}-{path.name}",
                    label="DTLN model",
                    dest=path,
                    url=url,
                    expected_sha256=expected_sha,
                    required=required,
                )
            )
    return assets


def seed_default_wake_model_env(
    *,
    log: Callable[[str], None] | None = print,
) -> None:
    from jasper.wake_models import WAKE_MODEL_FILE, default

    if os.path.exists(WAKE_MODEL_FILE):
        return
    entry = default()
    if not os.path.exists(entry.model):
        _log(log, f"  skipping wake_model.env seed: default file missing ({entry.model})")
        return
    os.makedirs(os.path.dirname(WAKE_MODEL_FILE), exist_ok=True)
    tmp = WAKE_MODEL_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    with os.fdopen(fd, "w") as f:
        f.write(f"JASPER_WAKE_MODEL={entry.model}\n")
    os.replace(tmp, WAKE_MODEL_FILE)
    _log(log, f"  seeded {WAKE_MODEL_FILE} -> {entry.key} ({entry.model})")


def active_wake_model(
    *,
    env: Mapping[str, str] = os.environ,
    jasper_env_path: str = "/etc/jasper/jasper.env",
    wake_env_path: str = "/var/lib/jasper/wake_model.env",
) -> str:
    model = env.get("JASPER_WAKE_MODEL", "").strip()
    model = _read_env_file(jasper_env_path).get("JASPER_WAKE_MODEL", model).strip()
    model = _read_env_file(wake_env_path).get("JASPER_WAKE_MODEL", model).strip()
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


def _read_env_file(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    except FileNotFoundError:
        pass
    return values


def _stage_cli(args: argparse.Namespace) -> int:
    required = args.required
    if args.registry == "openwakeword":
        models_dir = os.environ.get("OPENWAKEWORD_MODELS_DIR", "").strip()
        if not models_dir:
            raise SystemExit("OPENWAKEWORD_MODELS_DIR is required for openwakeword staging")
        assets = openwakeword_stage_assets(models_dir, active_model=active_wake_model())
    elif args.registry == "wake":
        assets = wake_model_stage_assets(required=required)
    elif args.registry == "dtln":
        assets = dtln_stage_assets(required=required)
    else:  # pragma: no cover - argparse choices prevent this.
        raise SystemExit(f"unknown registry: {args.registry}")

    result = stage_model_assets(
        assets,
        required_timeout_seconds=args.required_timeout,
        required_retries=args.required_retries,
        optional_timeout_seconds=args.optional_timeout,
        optional_retries=args.optional_retries,
        max_bytes=args.max_bytes,
    )
    if args.registry == "openwakeword" and result.optional_failures:
        print(
            f"  warning: {result.optional_failures} inactive openWakeWord stock "
            "asset(s) failed to download; unavailable rows will be disabled in /wake/.",
            file=sys.stderr,
        )
    if required:
        return 1 if result.required_failures else 0
    return 1 if result.failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage JTS model assets")
    subparsers = parser.add_subparsers(dest="command", required=True)
    stage = subparsers.add_parser("stage")
    stage.add_argument("--registry", choices=("openwakeword", "wake", "dtln"), required=True)
    mode = stage.add_mutually_exclusive_group(required=True)
    mode.add_argument("--required", action="store_true")
    mode.add_argument("--optional", action="store_true")
    stage.add_argument("--required-timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    stage.add_argument("--required-retries", type=int, default=DEFAULT_RETRIES)
    stage.add_argument("--optional-timeout", type=float, default=20.0)
    stage.add_argument("--optional-retries", type=int, default=1)
    stage.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    subparsers.add_parser("seed-wake-default")
    args = parser.parse_args(argv)
    if args.command == "stage":
        return _stage_cli(args)
    if args.command == "seed-wake-default":
        seed_default_wake_model_env()
        return 0
    raise SystemExit(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
