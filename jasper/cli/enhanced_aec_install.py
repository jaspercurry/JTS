# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Privileged background installer for the optional WebRTC AEC3 v2 engine."""
from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any

from jasper.atomic_io import advisory_file_lock
from jasper.enhanced_aec import (
    INSTALL_LOCK_PATH,
    SOURCE_ROOT,
    VENV_ROOT,
    EnhancedAecError,
    desired_fingerprint,
    extension_sha256,
    install_profile_supports_enhanced_aec,
    parse_target_manifest,
    read_intent,
    status,
    write_installed_marker,
    write_job_state,
)

logger = logging.getLogger(__name__)

CACHE_ROOT = Path("/opt/jasper/.cache/enhanced-aec")
WEBRTC_CACHE_ROOT = Path("/opt/jasper/.cache/webrtc-aec3-v2")
CONTAINED_BUILD = Path("/usr/local/sbin/jasper-contained-build")

DOWNLOAD_TIMEOUT_SEC = 210.0
COMMAND_TIMEOUT_SEC = 900.0
INSTALL_LOCK_TIMEOUT_SEC = 60.0
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_FAILURE_OUTPUT_CHARS = 4096
SUBPROCESS_OUTPUT_SPOOL_BYTES = 64 * 1024

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_SECRET_VALUE_RE = re.compile(
    r"(?i)\b(api[_-]?key|authorization|password|secret|token)"
    r"(\s*[:=]\s*)(\S+)"
)
_URL_CREDENTIAL_RE = re.compile(r"(https?://)[^/\s:@]+:[^/\s@]+@")


class SourceChanged(EnhancedAecError):
    """The deploy source changed after the background build snapshot."""


def _bounded_failure_output(raw: str | bytes | None) -> str:
    """Return a safe tail for durable, user-visible installer errors."""

    if raw is None:
        return ""
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    text = _ANSI_ESCAPE_RE.sub("", text)
    text = _SECRET_VALUE_RE.sub(r"\1\2[redacted]", text)
    text = _URL_CREDENTIAL_RE.sub(r"\1[redacted]@", text)
    text = "".join(
        char if char in "\n\t" or char.isprintable() else "?"
        for char in text
    )
    return text[-MAX_FAILURE_OUTPUT_CHARS:].strip()


def _command_name(argv: list[str]) -> str:
    return Path(argv[0]).name if argv else "command"


def _spooled_failure_tail(output) -> str:
    output.flush()
    output.seek(0, os.SEEK_END)
    # Read enough bytes to cover the bounded character tail without loading a
    # 30-minute compiler transcript back into RAM.
    size = output.tell()
    output.seek(max(size - (MAX_FAILURE_OUTPUT_CHARS * 4), 0))
    return _bounded_failure_output(output.read())


def _run(
    argv: list[str],
    *,
    timeout: float = COMMAND_TIMEOUT_SEC,
    env: dict[str, str] | None = None,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    if capture_output:
        try:
            return subprocess.run(
                argv,
                check=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
        except subprocess.CalledProcessError as exc:
            detail = _bounded_failure_output(exc.stderr or exc.stdout)
            message = f"{_command_name(argv)} exited with status {exc.returncode}"
            if detail:
                message += f"; output tail:\n{detail}"
            raise EnhancedAecError(message) from exc
        except subprocess.TimeoutExpired as exc:
            detail = _bounded_failure_output(exc.stderr or exc.stdout)
            message = f"{_command_name(argv)} timed out after {timeout:g} seconds"
            if detail:
                message += f"; output tail:\n{detail}"
            raise EnhancedAecError(message) from exc

    # Meson/pip can emit a large transcript during a 30-minute build. Spool it
    # out of process memory and retain only a bounded diagnostic tail on error.
    with tempfile.SpooledTemporaryFile(
        max_size=SUBPROCESS_OUTPUT_SPOOL_BYTES,
        mode="w+b",
    ) as output:
        try:
            completed = subprocess.run(
                argv,
                check=True,
                stdout=output,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                env=env,
            )
            return subprocess.CompletedProcess(
                argv,
                completed.returncode,
                stdout="",
                stderr="",
            )
        except subprocess.CalledProcessError as exc:
            detail = _spooled_failure_tail(output)
            message = f"{_command_name(argv)} exited with status {exc.returncode}"
            if detail:
                message += f"; output tail:\n{detail}"
            raise EnhancedAecError(message) from exc
        except subprocess.TimeoutExpired as exc:
            detail = _spooled_failure_tail(output)
            message = f"{_command_name(argv)} timed out after {timeout:g} seconds"
            if detail:
                message += f"; output tail:\n{detail}"
            raise EnhancedAecError(message) from exc


def _run_contained(
    label: str,
    argv: list[str],
    *,
    timeout: float = COMMAND_TIMEOUT_SEC,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    if not CONTAINED_BUILD.is_file():
        raise EnhancedAecError(
            f"contained-build helper is missing: {CONTAINED_BUILD}"
        )
    return _run(
        [str(CONTAINED_BUILD), label, "--", *argv],
        timeout=timeout,
        env=env,
    )


def _compile_jobs() -> int:
    # This command prints one integer, so bounded in-memory capture is useful.
    result = _run(
        [str(CONTAINED_BUILD), "--jobs-cpp"],
        timeout=10.0,
        capture_output=True,
    )
    try:
        jobs = int(result.stdout.strip())
    except ValueError as exc:
        raise EnhancedAecError(
            f"contained-build helper returned invalid job count: {result.stdout!r}"
        ) from exc
    return max(jobs, 1)


def _snapshot_source(source_root: Path, destination: Path) -> str:
    """Copy build inputs under the short deploy/activation lock."""

    with advisory_file_lock(
        INSTALL_LOCK_PATH,
        mode=0o600,
        group_from_parent=False,
        timeout_sec=INSTALL_LOCK_TIMEOUT_SEC,
    ):
        fingerprint = desired_fingerprint(source_root)
        shutil.copytree(
            source_root,
            destination,
            ignore=shutil.ignore_patterns(
                "build", "dist", "*.egg-info", "__pycache__", "*.so",
            ),
        )
    return fingerprint


def _download_archive(url: str, sha256: str, destination: Path) -> None:
    _run(
        [
            "curl",
            "--fail",
            "--location",
            "--retry",
            "3",
            "--retry-connrefused",
            "--connect-timeout",
            "20",
            "--max-time",
            "180",
            "--max-filesize",
            str(MAX_ARCHIVE_BYTES),
            "--proto",
            "=https",
            "--tlsv1.2",
            "--output",
            str(destination),
            url,
        ],
        timeout=DOWNLOAD_TIMEOUT_SEC,
    )
    if destination.stat().st_size > MAX_ARCHIVE_BYTES:
        raise EnhancedAecError(
            f"enhanced-AEC source archive exceeds {MAX_ARCHIVE_BYTES} bytes"
        )
    actual = extension_sha256(destination)
    if actual.lower() != sha256.lower():
        raise EnhancedAecError(
            f"enhanced-AEC source hash mismatch: got {actual}, expected {sha256}"
        )


def _extract_archive(archive: Path, destination: Path) -> Path:
    extraction_root = destination / "unpacked"
    extraction_root.mkdir(parents=True)
    with tarfile.open(archive, "r:*") as bundle:
        # Python 3.13's data filter rejects absolute paths, traversal, devices,
        # and other archive shapes that a privileged installer must not accept.
        bundle.extractall(extraction_root, filter="data")
    children = [path for path in extraction_root.iterdir() if path.name != "__MACOSX"]
    if len(children) != 1 or not children[0].is_dir():
        raise EnhancedAecError(
            "enhanced-AEC source archive must contain one top-level directory"
        )
    return children[0]


def _prepare_webrtc_source(
    target: dict[str, str],
    *,
    cache_root: Path = WEBRTC_CACHE_ROOT,
) -> Path:
    source_id = (
        f"{target['WEBRTC_AEC3_COMMIT']}:{target['WEBRTC_AEC3_SHA256']}"
    )
    source_root = cache_root / "src"
    build_root = source_root / "builddir"
    static_archive = (
        build_root
        / "webrtc"
        / "modules"
        / "audio_processing"
        / "libwebrtc-audio-processing-2.a"
    )
    provenance = cache_root / "source.archive"
    try:
        provenance_matches = provenance.read_text(encoding="utf-8").strip() == source_id
    except OSError:
        provenance_matches = False
    if static_archive.is_file() and provenance_matches:
        return cache_root

    shutil.rmtree(source_root, ignore_errors=True)
    cache_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".enhanced-aec-download-",
        dir=cache_root,
    ) as raw_tmp:
        temp_root = Path(raw_tmp)
        archive = temp_root / "source.tar.gz"
        _download_archive(
            target["WEBRTC_AEC3_ARCHIVE_URL"],
            target["WEBRTC_AEC3_SHA256"],
            archive,
        )
        extracted = _extract_archive(archive, temp_root)
        os.replace(extracted, source_root)

    _run_contained(
        "webrtc-aec3-setup",
        [
            "meson",
            "setup",
            str(build_root),
            str(source_root),
            "-Ddefault_library=static",
            "-Db_pie=true",
            "-Dc_args=-fPIC",
            "-Dcpp_args=-fPIC",
            "--buildtype=release",
        ],
    )
    _run_contained(
        "webrtc-aec3",
        [
            "meson",
            "compile",
            "-C",
            str(build_root),
            "-j",
            str(_compile_jobs()),
        ],
    )
    if not static_archive.is_file():
        raise EnhancedAecError(
            f"WebRTC build completed without expected archive: {static_archive}"
        )
    provenance.write_text(source_id + "\n", encoding="utf-8")
    return cache_root


def _build_v2_wheel(
    source_snapshot: Path,
    webrtc_prefix: Path,
    destination: Path,
    *,
    venv_root: Path = VENV_ROOT,
) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update({
        "JASPER_AEC3_BUILD_MODE": "v2-only",
        "WEBRTC_AEC3_V2_PREFIX": str(webrtc_prefix),
    })
    # pyproject.toml's build-system table makes pip use an isolated PEP 517
    # environment provisioned with the exact-pinned setuptools + pybind11.
    _run_contained(
        "jasper-aec3-v2",
        [
            str(venv_root / "bin" / "python"),
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-cache-dir",
            "--wheel-dir",
            str(destination),
            str(source_snapshot),
        ],
        env=env,
    )
    wheels = sorted(destination.glob("jasper_aec3-*.whl"))
    if len(wheels) != 1:
        raise EnhancedAecError(
            f"expected one enhanced-AEC wheel, found {len(wheels)}"
        )
    return wheels[0]


def _extract_v2_extension(wheel: Path, destination: Path) -> Path:
    import zipfile

    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(wheel) as bundle:
        candidates = [
            name
            for name in bundle.namelist()
            if name.startswith("jasper_aec3/_aec3_v2") and name.endswith(".so")
        ]
        if len(candidates) != 1:
            raise EnhancedAecError(
                f"expected one _aec3_v2 extension in wheel, found {len(candidates)}"
            )
        member = candidates[0]
        target = destination / Path(member).name
        with bundle.open(member) as source, target.open("wb") as output:
            shutil.copyfileobj(source, output)
    target.chmod(0o755)
    return target


def _probe_staged(extension: Path, *, venv_root: Path = VENV_ROOT) -> None:
    script = r"""
import importlib
import importlib.util
import sys

import jasper_aec3
v1 = importlib.import_module("jasper_aec3._aec3")
zero = b"\0" * 320
assert len(v1.Aec3().process(zero, zero)) == len(zero)
sys.modules.pop("jasper_aec3._aec3_v2", None)
spec = importlib.util.spec_from_file_location("jasper_aec3._aec3_v2", sys.argv[1])
assert spec is not None and spec.loader is not None
v2 = importlib.util.module_from_spec(spec)
sys.modules["jasper_aec3._aec3_v2"] = v2
spec.loader.exec_module(v2)
assert len(v2.Aec3V2().process(zero, zero)) == len(zero)
"""
    _run(
        [str(venv_root / "bin" / "python"), "-c", script, str(extension)],
        timeout=30.0,
    )


def _probe_active(*, venv_root: Path = VENV_ROOT) -> None:
    script = r"""
import jasper_aec3
zero = b"\0" * 320
assert jasper_aec3.HAS_V2
assert len(jasper_aec3.Aec3().process(zero, zero)) == len(zero)
assert len(jasper_aec3.Aec3V2().process(zero, zero)) == len(zero)
"""
    _run(
        [str(venv_root / "bin" / "python"), "-c", script],
        timeout=30.0,
    )


def _v1_package_dir(venv_root: Path = VENV_ROOT) -> Path:
    candidates = [
        path.parent
        for path in venv_root.glob(
            "lib/python*/site-packages/jasper_aec3/_aec3*.so"
        )
        if not path.name.startswith("_aec3_v2")
    ]
    unique = sorted(set(candidates))
    if len(unique) != 1:
        raise EnhancedAecError(
            "mandatory AEC3 v1 package is missing or ambiguous; "
            f"found {len(unique)} package directories"
        )
    return unique[0]


def _activate(
    staged_extension: Path,
    fingerprint: str,
    *,
    source_root: Path = SOURCE_ROOT,
    venv_root: Path = VENV_ROOT,
) -> Path:
    """Recheck deploy identity, atomically replace v2, then probe and mark."""

    with advisory_file_lock(
        INSTALL_LOCK_PATH,
        mode=0o600,
        group_from_parent=False,
        timeout_sec=INSTALL_LOCK_TIMEOUT_SEC,
    ):
        current_fingerprint = desired_fingerprint(source_root)
        if current_fingerprint != fingerprint:
            raise SourceChanged(
                "JTS changed while enhanced AEC was building; refusing to "
                "activate an extension for the previous source."
            )

        package_dir = _v1_package_dir(venv_root)
        destination = package_dir / staged_extension.name
        staged_copy = package_dir / f".{staged_extension.name}.staging-{os.getpid()}"
        backup = package_dir / f".{staged_extension.name}.rollback-{os.getpid()}"
        shutil.copy2(staged_extension, staged_copy)
        staged_copy.chmod(0o755)
        had_previous = destination.is_file()
        if had_previous:
            os.link(destination, backup)
        try:
            os.replace(staged_copy, destination)
            _probe_active(venv_root=venv_root)
        # This is the activation transaction's cleanup-and-reraise boundary:
        # import/probe code can raise open-endedly, but every failure must
        # restore the prior extension before propagating unchanged.
        except Exception:  # noqa: BLE001
            if had_previous and backup.exists():
                os.replace(backup, destination)
            else:
                destination.unlink(missing_ok=True)
            raise
        finally:
            staged_copy.unlink(missing_ok=True)
            backup.unlink(missing_ok=True)

        # Remove ABI-stale siblings only after the selected module probes.
        for sibling in package_dir.glob("_aec3_v2*.so"):
            if sibling != destination:
                sibling.unlink(missing_ok=True)

        # Activation proof is deliberately last: readers never see "current"
        # before both mandatory v1 and enhanced v2 processed a real frame.
        write_installed_marker(
            fingerprint=fingerprint,
            extension_sha256=extension_sha256(destination),
            extension_name=destination.name,
        )
        return destination


def _refresh_aec_runtime() -> str:
    """Ask the existing reconciler to restart the bridge onto verified v2.

    Activation is already committed when this runs. A scheduling failure must
    therefore remain a warning—the live bridge safely stays on v1 and the new
    engine will be selected at its next ordinary restart.

    The kick is INTENT, not drift: the verified v2 engine lives in a venv, so
    nothing the reconciler's change gate inspects (env files, the install
    manifest, published accessory/grouping facts) moves — a bare kick would be
    gated off as "no voice-relevant change". The one-shot marker below is the
    declared-intent path the gate honors; a systemctl kick can carry no
    arguments, which is why it is a file. The path literal mirrors
    VOICE_RESTART_INTENT_MARKER in deploy/bin/jasper-aec-reconcile (pinned by
    tests/test_aec_reconcile.py). Marker-write failures fall through to the
    kick on purpose: a gated-off refresh degrades exactly like a failed kick —
    v2 waits for the next ordinary restart, already this function's documented
    fallback.
    """

    marker = Path("/run/jasper-aec-reconcile/voice-restart-intent")
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("enhanced_aec_v2_activation\n", encoding="utf-8")
    except OSError as exc:
        logger.warning(
            "enhanced-aec: could not declare restart intent at %s (%s); "
            "the reconciler may gate the refresh off as a no-change pass",
            marker,
            exc,
        )
    try:
        _run(
            [
                "systemctl",
                "restart",
                "--no-block",
                "jasper-aec-reconcile.service",
            ],
            timeout=10.0,
        )
    except (OSError, subprocess.SubprocessError, EnhancedAecError) as exc:
        return str(exc)
    return ""


def install(
    *,
    source_root: Path = SOURCE_ROOT,
    venv_root: Path = VENV_ROOT,
    cache_root: Path = CACHE_ROOT,
) -> dict[str, Any]:
    if not install_profile_supports_enhanced_aec():
        return {"changed": False, "reason": "voice_brain_not_installed"}
    if not read_intent()["requested"]:
        return {"changed": False, "reason": "not_requested"}

    current = status(
        source_root=source_root,
        venv_root=venv_root,
        machine="aarch64",
    )
    if current["current"]:
        return {"changed": False, "reason": "already_current"}

    cache_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".enhanced-aec-job-",
        dir=cache_root,
    ) as raw_job:
        job_root = Path(raw_job)
        source_snapshot = job_root / "jasper_aec3"
        write_job_state(
            "snapshotting",
            "Capturing the exact JTS source for the background build.",
        )
        fingerprint = _snapshot_source(source_root, source_snapshot)
        target = parse_target_manifest(source_snapshot)

        write_job_state(
            "downloading",
            "Preparing the verified WebRTC AEC3 source.",
            desired_fingerprint=fingerprint,
        )
        webrtc_prefix = _prepare_webrtc_source(target)

        write_job_state(
            "building",
            "Building the enhanced engine in a low-priority contained job.",
            desired_fingerprint=fingerprint,
        )
        wheel = _build_v2_wheel(
            source_snapshot,
            webrtc_prefix,
            job_root / "wheel",
            venv_root=venv_root,
        )
        staged_extension = _extract_v2_extension(wheel, job_root / "staged")

        write_job_state(
            "verifying",
            "Verifying standard and enhanced echo cancellation.",
            desired_fingerprint=fingerprint,
        )
        _probe_staged(staged_extension, venv_root=venv_root)

        write_job_state(
            "activating",
            "Activating the verified enhanced engine.",
            desired_fingerprint=fingerprint,
        )
        activated = _activate(
            staged_extension,
            fingerprint,
            source_root=source_root,
            venv_root=venv_root,
        )
        refresh_error = _refresh_aec_runtime()
        detail = (
            "Enhanced echo cancellation is installed."
            if not refresh_error
            else (
                "Enhanced echo cancellation is installed. The live audio "
                "refresh could not be scheduled, so it will take effect at "
                "the next audio restart."
            )
        )
        write_job_state(
            "success",
            detail,
            desired_fingerprint=fingerprint,
            extra={
                "installed_fingerprint": fingerprint,
                "extension": activated.name,
                "runtime_refresh": (
                    "scheduled" if not refresh_error else "next_restart"
                ),
                "runtime_refresh_error": refresh_error,
            },
        )
        return {
            "changed": True,
            "fingerprint": fingerprint,
            "extension": str(activated),
            "runtime_refresh": (
                "scheduled" if not refresh_error else "next_restart"
            ),
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Install the optional enhanced WebRTC AEC3 engine.",
    )
    parser.add_argument("--json", action="store_true", help="print JSON result")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        result = install()
    except SourceChanged as exc:
        write_job_state("stale", str(exc), error="")
        print(f"jasper-enhanced-aec-install: {exc}", file=sys.stderr)
        return 0
    except (
        EnhancedAecError,
        OSError,
        subprocess.SubprocessError,
        tarfile.TarError,
    ) as exc:
        write_job_state(
            "failed",
            (
                "Enhanced echo cancellation was not installed. Standard echo "
                "cancellation remains active."
            ),
            error=str(exc),
        )
        print(f"jasper-enhanced-aec-install: {exc}", file=sys.stderr)
        return 1
    if args.json:
        import json

        print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
