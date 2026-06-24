# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared primitives for the jasper-doctor check package.

This is the base layer every per-domain check module imports
from. It holds, **verbatim from the original**
``jasper/cli/doctor.py``:

- the :class:`CheckResult` dataclass and the ``DoctorCheck``
  type alias (the union that lets a list entry be either a bare
  callable or a ``(label, callable)`` tuple);
- the crash-isolation harness (``_run_doctor_check`` /
  ``_run_async_doctor_check`` / ``_normalize_doctor_check`` /
  ``_check_name`` / ``_crashed_check_result`` and the
  secret-redacting ``_exception_detail``), unchanged so one
  crashing check still cannot abort the run;
- ``_run`` (the subprocess wrapper) and ``_parse_env_file``;
- ANSI colour constants and the chip-AEC passive constants;
- the genuinely cross-cutting helpers used by more than one
  domain (``_sha256_file``, ``_meminfo_kb``,
  ``_systemctl_show_property``, ``_pid_of_unit``,
  ``_service_runtime_states`` + ``_RUNTIME_STATE_UNITS``,
  ``_active_audio_dac_id``, ``_loopback_playback_active``).

No logic changed in the split. Names that tests patch (e.g.
``_run``) stay importable here and are re-imported into each
domain module, so a check reads them from its own namespace."""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable
from ...audio_hardware.dac import APPLE_USB_C_DONGLE_ID
from ...audio_validation import DAC8X_DAC_ID
from ...env_load import parse_env_file as _shared_parse_env_file

GREEN = "\033[32m"

RED = "\033[31m"

YELLOW = "\033[33m"

BOLD = "\033[1m"

RESET = "\033[0m"

_KNOWN_CHIP_AEC_PASSIVE_HARDWARE = frozenset({
    ("xvf3800", DAC8X_DAC_ID),
})

_CHIP_AEC_PASSIVE_REQUIRED_CHECKS = frozenset({
    "runtime_profile",
    "mic_detected",
    "runtime_env",
    "service_state",
    "dac_reference",
    "wake_legs",
    "outputd_reference_health",
    "bridge_counter_window",
    "chip_profile_readback",
    "chip_convergence",
})

@dataclass
class CheckResult:
    name: str
    status: str  # "ok" | "warn" | "fail"
    detail: str = ""

DoctorCheck = Callable[[], CheckResult] | tuple[str, Callable[[], CheckResult]]

_EXCEPTION_DETAIL_LIMIT = 240

_BEARER_SECRET_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")

_KEY_VALUE_SECRET_RE = re.compile(
    r"(?i)\b"
    r"(api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|"
    r"password|psk|token)"
    r"\s*([=:])\s*(['\"]?)([^'\"\s,;]+)"
)

_SECRET_PREFIX_RE = re.compile(r"\b(?:AIza|sk-|xai-)[A-Za-z0-9_-]{8,}")

def _redact_exception_message(message: str) -> str:
    message = _BEARER_SECRET_RE.sub("Bearer <redacted>", message)
    message = _KEY_VALUE_SECRET_RE.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}<redacted>",
        message,
    )
    return _SECRET_PREFIX_RE.sub(
        lambda m: f"{m.group(0)[:4]}...{m.group(0)[-4:]}",
        message,
    )

def _exception_detail(exc: BaseException) -> str:
    message = _redact_exception_message(str(exc))
    if len(message) > _EXCEPTION_DETAIL_LIMIT:
        message = message[: _EXCEPTION_DETAIL_LIMIT - 3] + "..."
    if not message:
        return type(exc).__name__
    return f"{type(exc).__name__}: {message}"

def _crashed_check_result(name: str, exc: BaseException) -> CheckResult:
    return CheckResult(
        name,
        "fail",
        f"check crashed: {_exception_detail(exc)}",
    )

def _check_name(check: Callable[[], CheckResult]) -> str:
    name = getattr(check, "__name__", "doctor check")
    if name == "<lambda>":
        return "doctor check"
    if name.startswith("check_"):
        name = name[len("check_"):]
    return name.replace("_", " ")

def _normalize_doctor_check(
    entry: DoctorCheck,
) -> tuple[str, Callable[[], CheckResult]]:
    if isinstance(entry, tuple):
        return entry
    return _check_name(entry), entry

def _run_doctor_check(entry: DoctorCheck) -> CheckResult:
    name, check = _normalize_doctor_check(entry)
    try:
        return check()
    except Exception as e:  # noqa: BLE001
        return _crashed_check_result(name, e)

async def _run_async_doctor_check(
    name: str,
    check: Callable[[], Awaitable[CheckResult]],
) -> CheckResult:
    try:
        return await check()
    except Exception as e:  # noqa: BLE001
        return _crashed_check_result(name, e)

def _run(cmd: list[str], timeout: float = 5.0) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

def _parse_env_file(path: str) -> dict[str, str]:
    """Back-compat wrapper for tests and external doctor consumers."""

    return _shared_parse_env_file(path)


def _camilla_block_field(text: str, block: str, key: str) -> str | None:
    """Scan a CamillaDSP config (text) for ``key`` inside the top-level
    ``block:`` (e.g. ``devices`` / ``mixers``), returning the raw value after
    the colon (comment + surrounding-quote stripped), or None if the block or
    key is absent.

    The value is ``""`` for a key whose value is a nested block (a mixer name
    like ``channel_select:``), so ``_camilla_block_field(...) is not None`` is
    the presence test. Returns the FIRST match within the block.

    This is the doctor's DELIBERATELY fail-soft way to read a CamillaDSP config
    field — a plain line scan that never raises, unlike ``yaml.safe_load`` which
    can raise on a malformed config (the doctor must stay total). It is the one
    home for the idiom: ``check_camilla_volume_limit`` (``devices.volume_limit``),
    ``check_grouping_rate_adjust`` (``devices.enable_rate_adjust``), and
    ``check_grouping_leader_pipe`` (``devices.playback`` pipe scan) all go
    through it. Block-scoped: a matching key OUTSIDE ``block:`` does not match.
    Use only for keys that are unambiguous within their block (the value is the
    first indented ``key:`` line, at any depth)."""
    in_block = False
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not raw.startswith((" ", "\t")):
            in_block = stripped == f"{block}:"
            continue
        if not in_block:
            continue
        match = re.match(rf"^\s+{re.escape(key)}:\s*([^#]*)", raw)
        if match:
            return match.group(1).strip().strip("'\"")
    return None

def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()

def _active_audio_dac_env() -> dict[str, str]:
    env = _shared_parse_env_file("/etc/jasper/jasper.env")
    return {
        "id": (
            os.environ.get("JASPER_AUDIO_DAC_ID")
            or env.get("JASPER_AUDIO_DAC_ID")
            or APPLE_USB_C_DONGLE_ID
        ),
        "card": (
            os.environ.get("JASPER_AUDIO_DAC_CARD")
            or env.get("JASPER_AUDIO_DAC_CARD")
            or "A"
        ),
    }

def _active_audio_dac_id() -> str:
    return _active_audio_dac_env()["id"]

def _meminfo_kb(field: str) -> int | None:
    """Read a single field (e.g. 'MemAvailable') from /proc/meminfo
    in KiB. Returns None on read error."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith(field + ":"):
                    return int(line.split()[1])
    except Exception:  # noqa: BLE001
        return None
    return None

def _pid_of_unit(unit: str) -> int | None:
    """Best-effort single-unit PID lookup. Returns None if the unit
    isn't running, or if systemctl isn't available (dev host).

    Used only when a caller wants just one PID. The batch caller
    `check_oom_score_adj` uses `_systemctl_show_property` directly
    to avoid N subprocess invocations for N units."""
    try:
        out = _run(
            ["systemctl", "show", "-p", "MainPID", "--value", f"{unit}.service"],
        ).stdout.strip()
        pid = int(out)
        return pid if pid > 0 else None
    except (subprocess.SubprocessError, ValueError, FileNotFoundError):
        return None

def _systemctl_show_property(prop: str, units: list[str]) -> list[str] | None:
    """Batch read of one systemd property across multiple units. One
    subprocess call returns N values (one per unit, in input order).

    Returns:
        list of values (length == len(units)), OR None if systemctl
        is unavailable (dev host).

    Why this matters: before the batch, check_oom_score_adj called
    `systemctl show` once per (property × daemon) — a dozen-plus
    subprocess invocations per doctor run. Batched, it's one invocation
    per property (LoadState, MainPID, OOMScoreAdjust), a large
    constant-factor win on the Pi.

    Wire format note: `systemctl show -p X --value <u1> <u2> ... <uN>`
    emits `value1\\n\\nvalue2\\n\\n...valueN\\n`. The separator is
    `\\n\\n` (blank line between values), NOT plain `\\n`. We split
    on that explicitly.
    """
    try:
        out = _run(
            ["systemctl", "show", "-p", prop, "--value"] +
            [f"{u}.service" for u in units],
            # Wider timeout — listing N units takes longer than 1.
            timeout=10.0,
        ).stdout
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    # Strip trailing newline before splitting so the last value isn't
    # followed by a phantom empty element.
    text = out.rstrip("\n")
    # systemctl separates per-unit values with a blank line (\n\n) when
    # multiple units are requested with --value. Splitting on \n alone
    # would produce 2N-1 elements for N units; split on \n\n to get N.
    if not text:
        # All units returned empty values (e.g. all not-running).
        # Still need len(units) entries.
        return [""] * len(units)
    if "\n\n" in text:
        parts = text.split("\n\n")
    else:
        # Single unit, or systemd version that doesn't emit blank
        # separators. Fall back to plain \n split.
        parts = text.split("\n")
    if len(parts) != len(units):
        # Unexpected shape — degrade gracefully so the caller can
        # surface "skipped" rather than crash.
        return None
    return parts


def _installed_units(units: list[str]) -> set[str] | None:
    """Subset of ``units`` whose unit file is actually installed.

    "Installed" means ``LoadState`` is neither ``not-found`` (no unit
    file) nor ``masked`` (symlinked to /dev/null) — i.e. an effective
    unit file exists to carry a directive. A unit that exists but is
    broken (``error`` / ``bad-setting``) is intentionally KEPT so its
    drift still surfaces rather than being silently hidden.

    Returns ``None`` if systemctl is unavailable (dev host), so callers
    can fall through to their existing "skipped" path.

    Why: drift checks (OOM score, StartLimitAction) verify a PROPERTY of
    a unit. A unit a profile never installs — e.g. the voice/AEC stack on
    a streambox — has no property to drift, and ``systemctl show`` reports
    its directives as defaults, which would read as false drift. Callers
    filter their expected set to this set so the check stays correct on
    every install profile without hard-coding which units each tier runs.
    """
    load_states = _systemctl_show_property("LoadState", units)
    if load_states is None:
        return None
    return {
        u for u, state in zip(units, load_states)
        if state.strip() not in ("not-found", "masked")
    }

def _parked_as_bonded_follower() -> bool:
    """True when this speaker is an ACTIVE bonded multiroom FOLLOWER.

    The dumb-follower profile (HANDOFF-multiroom Increment 5) parks the
    renderer/source stack while bonded — those liveness checks must read
    "parked (bonded follower)" as ok, never as failures against intended
    state. The same idiom serves PR-B's voice/AEC parking. Fail-open to
    NOT-parked: a broken read must never silently mask a real failure on
    a solo speaker."""
    try:
        from ...multiroom.config import is_bonded_follower, load_config

        return is_bonded_follower(load_config())
    except Exception:  # noqa: BLE001 — fail-open
        return False


_RUNTIME_STATE_UNITS = (
    "jasper-outputd.service",
    "jasper-fanin.service",
    "jasper-camilla.service",
    "jasper-voice.service",
    "jasper-aec-bridge.service",
    "jasper-control.service",
    "jasper-mux.service",
    "shairport-sync.service",
    "librespot.service",
    "bluealsa-aplay.service",
)

def _service_runtime_states() -> dict[str, dict[str, object]] | None:
    try:
        proc = _run(
            [
                "systemctl", "show", "--no-page",
                "--property=Id",
                "--property=LoadState",
                "--property=ActiveState",
                "--property=SubState",
                "--property=Result",
                "--property=NRestarts",
            ] + list(_RUNTIME_STATE_UNITS),
            timeout=10.0,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    from ...control.system_metrics import SystemSampler

    return SystemSampler._parse_systemctl_show_units(proc.stdout)

def _loopback_playback_active() -> bool:
    """True if any renderer is currently writing the music-chain loopback.

    Checked by reading `/proc/asound/Loopback/pcm0p/sub*/status`: an open
    subdevice prints `state: …\\nowner_pid: …`, a closed one prints the
    single word `closed`. The presence of any non-closed sub means a
    renderer (shairport / librespot / bluealsa) is producing right now.

    In fan-in topology, substream 7 is jasper-fanin's summed output and
    may be open even when every renderer is idle. Count only input
    lanes 0..4 for "music active" so AEC output health does not
    confuse the daemon's own output with a renderer source.

    Used to gate the AEC bridge FAIL: ref-silent windows are only
    diagnostic of a broken dsnoop when music IS being routed through the
    loopback. When no renderer is writing, ref-silent is the expected
    state and mic-loud bursts come from non-loopback sources (TTS via
    jasper_out, voice in the room).
    """
    import glob
    for status_path in glob.glob("/proc/asound/Loopback/pcm0p/sub*/status"):
        m = re.search(r"/sub(\d+)/status$", status_path)
        if m and int(m.group(1)) > 4:
            continue
        try:
            with open(status_path, encoding="utf-8") as f:
                first_line = f.readline().strip()
        except OSError:
            continue
        if first_line and first_line != "closed":
            return True
    return False
