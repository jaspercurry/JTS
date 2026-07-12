# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Download and flash the XVF3800 firmware selected by the mic registry."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ..mics import xvf3800


STATE_PATH = Path("/var/lib/jasper/xvf-firmware-update.json")
UPDATE_UNITS = (
    "jasper-voice.service",
    "jasper-aec-bridge.service",
    "jasper-aec-init.service",
)
RECONCILE_UNIT = "jasper-aec-reconcile.service"
DOWNLOAD_IO_TIMEOUT_SEC = 60.0
DOWNLOAD_TOTAL_TIMEOUT_SEC = 120.0
PRE_FLASH_TIMEOUT_BUDGET_SEC = 150.0
STOP_UNITS_TIMEOUT_SEC = 30.0
DFU_FLASH_TIMEOUT_SEC = 120.0
REENUMERATION_TIMEOUT_SEC = 30.0
RECONCILE_TIMEOUT_SEC = 45.0
PROFILE_POLL_INTERVAL_SEC = 1.0
# The systemd unit's outer timeout must exceed this whole post-download path.
# The second reconcile is the handled failure path when the first reconcile
# itself times out. The poll interval covers the re-enumeration loop's final
# sleep overshoot.
POST_DOWNLOAD_TIMEOUT_BUDGET_SEC = (
    STOP_UNITS_TIMEOUT_SEC
    + DFU_FLASH_TIMEOUT_SEC
    + REENUMERATION_TIMEOUT_SEC
    + 2 * RECONCILE_TIMEOUT_SEC
    + PROFILE_POLL_INTERVAL_SEC
)
EXPECTED_UPDATE_ERRORS = (
    OSError,
    RuntimeError,
    subprocess.SubprocessError,
    TimeoutError,
    urllib.error.URLError,
)


class _DownloadDeadlineExpired(BaseException):
    """Signal sentinel kept outside Exception/OSError catch hierarchies."""


def _require_pre_flash_budget(update_started_at: float) -> None:
    """Refuse before touching DFU if pre-flash work consumed its safe window."""

    elapsed = time.monotonic() - update_started_at
    if elapsed > PRE_FLASH_TIMEOUT_BUDGET_SEC:
        raise TimeoutError(
            f"refusing to start microphone flash after {elapsed:.1f}s of "
            f"pre-flash work (limit {PRE_FLASH_TIMEOUT_BUDGET_SEC:g}s)"
        )


def _write_state(
    *,
    state: str,
    detail: str,
    target: xvf3800.FirmwareUpdateTarget | None = None,
    error: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "state": state,
        "detail": detail,
        "error": error,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if target is not None:
        payload["target"] = target.as_dict()
    if extra:
        payload.update(extra)
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, STATE_PATH)


def _run(argv: list[str], *, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _run_dfu_flash(firmware_path: Path) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [
            "dfu-util",
            "-R",
            "-e",
            "-a",
            str(xvf3800.DFU_ALT_SETTING),
            "-D",
            str(firmware_path),
        ],
        capture_output=True,
        text=True,
        timeout=DFU_FLASH_TIMEOUT_SEC,
    )
    transcript = f"{result.stdout}\n{result.stderr}"
    if result.returncode == 0:
        return result
    # With -R the chip can disappear during USB reset after a successful
    # manifest. dfu-util then returns nonzero even though the flash completed.
    if "Download done." in transcript and "Done!" in transcript:
        return result
    raise subprocess.CalledProcessError(
        result.returncode,
        result.args,
        output=result.stdout,
        stderr=result.stderr,
    )


@contextlib.contextmanager
def _download_deadline():
    """Bound the entire pre-flash download, not each socket operation alone.

    The updater is a Linux systemd service running on the main thread, so a
    real-time interval timer can interrupt DNS/connect/read stalls without
    leaving a worker behind. Preserve any caller-owned alarm for test and
    embedding hygiene even though the production CLI owns its process.
    """

    timeout_s = DOWNLOAD_TOTAL_TIMEOUT_SEC

    def _expired(_signum, _frame) -> None:
        # TimeoutError is also OSError, which urllib/socket internals may catch
        # and swallow. This private BaseException must cross that boundary;
        # _download_and_verify translates it after signal state is restored.
        raise _DownloadDeadlineExpired

    previous_handler = signal.signal(signal.SIGALRM, _expired)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, timeout_s)
    started = time.monotonic()
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            elapsed = time.monotonic() - started
            remaining = max(previous_timer[0] - elapsed, 1e-6)
            signal.setitimer(signal.ITIMER_REAL, remaining, previous_timer[1])


def _download_and_verify(target: xvf3800.FirmwareUpdateTarget, dest: Path) -> str:
    hasher = hashlib.sha256()
    total = 0
    try:
        with _download_deadline():
            with urllib.request.urlopen(
                target.url,
                timeout=DOWNLOAD_IO_TIMEOUT_SEC,
            ) as response:
                raw_length = response.headers.get("Content-Length")
                if raw_length:
                    try:
                        content_length = int(raw_length)
                    except ValueError:
                        content_length = -1
                    if content_length != target.expected_size_bytes:
                        raise RuntimeError(
                            "firmware download size mismatch before read: "
                            f"got {raw_length}, "
                            f"expected {target.expected_size_bytes}"
                        )
                with dest.open("wb") as f:
                    while True:
                        chunk = response.read(1024 * 128)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > target.expected_size_bytes:
                            raise RuntimeError(
                                "firmware download exceeded expected size: "
                                f"got at least {total}, "
                                f"expected {target.expected_size_bytes}"
                            )
                        hasher.update(chunk)
                        f.write(chunk)
    except _DownloadDeadlineExpired as exc:
        raise TimeoutError(
            f"firmware download exceeded {DOWNLOAD_TOTAL_TIMEOUT_SEC:g}s "
            "total deadline"
        ) from exc
    if total != target.expected_size_bytes:
        raise RuntimeError(
            f"firmware download size mismatch: got {total}, "
            f"expected {target.expected_size_bytes}"
        )
    digest = hasher.hexdigest()
    if digest.lower() != target.sha256.lower():
        raise RuntimeError(
            f"downloaded firmware hash mismatch: got {digest}, "
            f"expected {target.sha256}"
        )
    return digest


def _reconcile_after_failure() -> str:
    try:
        _run(["systemctl", "restart", RECONCILE_UNIT], timeout=RECONCILE_TIMEOUT_SEC)
    except (OSError, subprocess.SubprocessError) as exc:
        return str(exc)
    return ""


def _wait_for_expected_profile(
    target: xvf3800.FirmwareUpdateTarget,
    *,
    timeout_s: float = REENUMERATION_TIMEOUT_SEC,
) -> xvf3800.RuntimeProfile:
    deadline = time.monotonic() + timeout_s
    last = xvf3800.detect_runtime_profile()
    while time.monotonic() < deadline:
        last = xvf3800.detect_runtime_profile()
        if (
            last.variant_id == target.to_variant_id
            and last.capture_channels == target.expected_capture_channels
        ):
            return last
        time.sleep(PROFILE_POLL_INTERVAL_SEC)
    raise RuntimeError(
        "microphone did not re-enumerate with expected firmware: "
        f"wanted {target.to_variant_id}/{target.expected_capture_channels}ch, "
        f"got {last.variant_id or 'unknown'}/{last.capture_channels or 'unknown'}ch"
    )


def update(target_id: str = "") -> dict[str, Any]:
    update_started_at = time.monotonic()
    profile = xvf3800.detect_runtime_profile()
    target = (
        xvf3800.FIRMWARE_UPDATE_TARGETS_BY_ID.get(target_id)
        if target_id else xvf3800.firmware_update_target_for_profile(profile)
    )
    if target is None:
        raise RuntimeError(
            "no safe firmware update target for detected microphone: "
            f"{profile.variant_id or profile.reason}"
        )
    if profile.variant_id not in target.from_variant_ids:
        raise RuntimeError(
            f"refusing to flash {target.target_id}: detected "
            f"{profile.variant_id or 'unknown'}, expected one of "
            f"{', '.join(target.from_variant_ids)}"
        )

    _write_state(
        state="downloading",
        detail=f"Downloading {target.filename} from upstream.",
        target=target,
        extra={"detected": profile.as_dict()},
    )
    cleanup_needed = False
    try:
        with tempfile.TemporaryDirectory(prefix="jasper-xvf-fw-") as tmpdir:
            firmware_path = Path(tmpdir) / target.filename
            digest = _download_and_verify(target, firmware_path)
            _write_state(
                state="flashing",
                detail="Firmware hash verified; flashing microphone over DFU.",
                target=target,
                extra={"detected": profile.as_dict(), "sha256": digest},
            )
            _require_pre_flash_budget(update_started_at)
            cleanup_needed = True
            _run(
                ["systemctl", "stop", *UPDATE_UNITS],
                timeout=STOP_UNITS_TIMEOUT_SEC,
            )
            _run_dfu_flash(firmware_path)

        _write_state(
            state="verifying",
            detail="Waiting for microphone to re-enumerate with 6-channel firmware.",
            target=target,
        )
        verified = _wait_for_expected_profile(target)
        _write_state(
            state="reconciling",
            detail="Firmware verified; reconciling microphone and AEC services.",
            target=target,
            extra={"verified": verified.as_dict()},
        )
        _run(["systemctl", "restart", RECONCILE_UNIT], timeout=RECONCILE_TIMEOUT_SEC)
        cleanup_needed = False
    except EXPECTED_UPDATE_ERRORS as exc:
        if cleanup_needed:
            cleanup_error = _reconcile_after_failure()
            if cleanup_error:
                raise RuntimeError(
                    f"{exc}; recovery reconcile failed: {cleanup_error}"
                ) from exc
        raise
    result = {
        "detected": profile.as_dict(),
        "verified": verified.as_dict(),
        "target": target.as_dict(),
    }
    _write_state(
        state="success",
        detail="Microphone firmware update completed.",
        target=target,
        extra=result,
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Download and flash a safe XVF3800 firmware update.",
    )
    parser.add_argument(
        "--target",
        default="",
        help="optional target id; default chooses from the detected mic profile",
    )
    parser.add_argument("--json", action="store_true", help="print JSON result")
    args = parser.parse_args(argv)
    try:
        result = update(args.target)
    except EXPECTED_UPDATE_ERRORS as exc:
        _write_state(state="failed", detail="Firmware update failed.", error=str(exc))
        print(f"jasper-xvf-firmware-update: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
