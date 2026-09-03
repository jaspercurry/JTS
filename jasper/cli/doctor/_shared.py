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
- ANSI colour constants and the chip-AEC passive check set;
- the genuinely cross-cutting helpers used by more than one
  domain (``_sha256_file``, ``_meminfo_kb``,
  ``_systemctl_show_property``, ``_pid_of_unit``,
  ``_service_runtime_states`` + ``_RUNTIME_STATE_UNITS``,
  ``_loopback_playback_active``).

No logic changed in the split. Names that tests patch (e.g.
``_run``) stay importable here and are re-imported into each
domain module, so a check reads them from its own namespace."""
from __future__ import annotations

import grp
import hashlib
import os
import re
import shlex
import stat as _stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable
from ...env_load import parse_env_file as _shared_parse_env_file
from ...secret_redaction import redact_secrets

GREEN = "\033[32m"

RED = "\033[31m"

YELLOW = "\033[33m"

BOLD = "\033[1m"

RESET = "\033[0m"

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
    # True when THIS result's meaning is "the speaker emits nothing right
    # now". Only a check that can prove that sets it, because only the check
    # knows: a name-matching renderer would have to re-derive the meaning of
    # every branch of every check, which is the same fact in a second voice.
    #
    # `render` reads it on a `warn` or a `fail` and never reads it on an `ok`:
    # a check returning `ok` while asserting silence contradicts itself. A
    # `fail` may carry it — a silent speaker is no less silent for also being
    # broken.
    #
    # It exists so `render`'s summary line can LEAD with the silence instead
    # of ending a parked box's run on "N warning(s) - non-critical". It does
    # NOT change severity or exit code: a parked box is silent, not broken,
    # and `jasper-doctor` must stay exit-0 there so a mid-commission speaker
    # keeps taking deploys (#2145). The wording carries the severity; the exit
    # code keeps its one meaning.
    speaker_silent: bool = False
    # Machine-stable snake_case code naming WHICH branch of the check
    # produced this result — e.g. "bridge_output_ref_silent". `detail` stays
    # the human sentence (and may change wording freely); `reason` is what
    # tests and other automation pin instead of `detail` prose (AGENTS.md:
    # assert types/codes/structured fields, never prose). Default "" for
    # every check that has not adopted a closed reason vocabulary yet — each
    # domain that does defines its own closed set of constants (see
    # `aec.py`'s `_AEC_REASONS` for the first one) rather than sharing one
    # vocabulary across unrelated domains.
    reason: str = ""

DoctorCheck = Callable[[], CheckResult] | tuple[str, Callable[[], CheckResult]]

_EXCEPTION_DETAIL_LIMIT = 240

def _exception_detail(exc: BaseException) -> str:
    message = redact_secrets(str(exc))
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


def _parse_systemd_environment(text: str) -> dict[str, str]:
    """Parse ``systemctl show -p Environment`` output into key/value pairs."""
    text = text.strip()
    if text.startswith("Environment="):
        text = text[len("Environment="):]
    try:
        tokens = shlex.split(text)
    except ValueError:
        tokens = text.split()
    env: dict[str, str] = {}
    for token in tokens:
        if "=" not in token:
            continue
        key, _, value = token.partition("=")
        env[key] = value.strip().strip('"').strip("'")
    return env


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
    can raise on a malformed config (the doctor must stay total). The grouping
    rate-adjust check uses it for ``devices.enable_rate_adjust``. Block-scoped:
    a matching key OUTSIDE ``block:`` does not match. Use only for keys that
    are unambiguous at any depth within their block; depth-sensitive safety
    fields such as ``devices.volume_limit`` use
    :func:`jasper.camilla_config_contract.parse_camilla_devices_config`."""
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

def _group_writable_dir(
    st: os.stat_result, *, expected_group: str, require_setgid: bool = True
) -> tuple[bool, str]:
    """Whether a non-root process in group ``expected_group`` could create or
    replace entries in the directory behind stat result ``st``.

    Three conditions, not one:

    - group-owned by ``expected_group``;
    - the group WRITE *and* SEARCH/execute bits (``0o030``) — POSIX needs
      search on a directory, not just write, to add or remove an entry in
      it, so write alone is not enough;
    - (when ``require_setgid``) the setgid bit, so a subdirectory a
      ROOT-run process later creates inside this tree inherits
      ``expected_group`` from its parent instead of landing group-root (the
      creator's own primary group) and locking the non-root arm out of that
      nested path. Only meaningful for trees a root-run writer can add NEW
      subdirectories to that the non-root arm must also reach — pass
      ``require_setgid=False`` for a plain state dir whose every writer
      already declares its own ``Group=`` in its systemd unit, where
      inheritance is not how that dir gets its group.

    Returns ``(writable, resolved_group_name)`` so callers can build their
    own detail message; the name falls back to the numeric gid string when
    it has no group-database entry.

    ``os.access(path, os.W_OK)`` cannot answer this: jasper-doctor and
    install.sh's helpers run as root, and ``os.access`` reports every path
    writable to the *caller* regardless of its actual mode — root can write
    regardless of mode bits. This is the shared predicate for that
    root-caller blind spot on the write side; ``privsep.py`` /
    ``secret_compartments.py`` document and solve the identical problem on
    the read side.
    """
    try:
        group_name = grp.getgrgid(st.st_gid).gr_name
    except (KeyError, OSError):
        group_name = str(st.st_gid)
    writable = group_name == expected_group and (st.st_mode & 0o030) == 0o030
    if require_setgid:
        writable = writable and bool(st.st_mode & _stat.S_ISGID)
    return writable, group_name

def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()

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
    `check_installed_settings_drift` uses `_systemctl_show_property`
    directly to avoid N subprocess invocations for N units."""
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

    Why this matters: unbatched, the installed-settings drift check would
    call `systemctl show` once per (property × daemon) — a dozen-plus
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

    Returns ``None`` if systemctl cannot answer — absent (dev host), or
    present but returning nothing (no D-Bus, a host not booted with
    systemd, a non-zero exit). Both are "unknown", not "everything is
    installed": treating an empty answer as installed would read every
    unit's directive as its systemd default and fabricate drift on all of
    them. Callers fall through to their existing "skipped" path.

    Why: drift checks verify a PROPERTY of a unit. A unit a profile never
    installs — e.g. the voice/AEC stack on a streambox — has no property
    to drift, and ``systemctl show`` reports its directives as defaults,
    which would read as false drift. Callers filter their expected set to
    this set so the check stays correct on every install profile without
    hard-coding which units each tier runs.
    """
    load_states = _systemctl_show_property("LoadState", units)
    if load_states is None or not any(s.strip() for s in load_states):
        return None
    return {
        u for u, state in zip(units, load_states)
        if state.strip() not in ("not-found", "masked")
    }

def _parked_as_bonded_follower() -> bool:
    """True when this speaker is an ACTIVE bonded multiroom FOLLOWER.

    The dumb-follower profile parks the renderer/source stack while bonded —
    those liveness checks must read
    "parked (bonded follower)" as ok, never as failures against intended
    state. The same idiom serves PR-B's voice/AEC parking. Fail-open to
    NOT-parked: a broken read must never silently mask a real failure on
    a solo speaker."""
    try:
        from ...multiroom.config import load_config
        from ...multiroom.effective_role import (
            effective_local_sources_park_reason,
        )

        return effective_local_sources_park_reason(load_config()) is not None
    except Exception:  # noqa: BLE001 — fail-open
        return False


_RUNTIME_STATE_UNITS = (
    "nginx.service",
    "jasper-outputd.service",
    "jasper-fanin.service",
    "jasper-camilla.service",
    "jasper-voice.service",
    "jasper-aec-bridge.service",
    "jasper-control.service",
    "jasper-mux.service",
    "nqptp.service",
    "shairport-sync.service",
    "librespot.service",
    "bluealsa.service",
    "bluealsa-aplay.service",
    "bt-agent.service",
    # The fan-in coupling-reconcile oneshots (#1233 follow-up): a failed pass
    # (e.g. an arm-abort — env written, fan-in restart aborted because camilla
    # would not stop) parks the unit in `failed` with the evidence only in
    # `systemctl --failed` + the journal; tracking them here makes it
    # doctor-visible.
    "jasper-fanin-coupling-auto.service",
)

# Type=oneshot members of the tracked set: `activating` is their NORMAL
# in-flight state (a reconcile pass running right now), not the stuck-start
# instability it signals on the long-running daemons above — only a real
# `failed` end-state should flag them. A genuinely wedged (stuck-activating)
# oneshot is still caught: its unit's TimeoutStartSec (120s on the one member,
# jasper-fanin-coupling-auto) fires and moves it to `failed`, which this check
# then surfaces on the next run.
_ONESHOT_RUNTIME_STATE_UNITS = frozenset({
    "jasper-fanin-coupling-auto.service",
})

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

    Substream 7 is jasper-fanin's summed output and may be open even
    when every renderer is idle — under `loopback` coupling, the one
    place fan-in still writes the lane since U4/P7-4 dropped the ring
    arm's aloop mirror. Count only input lanes 0..4 for "music active"
    so AEC output health does not confuse the daemon's own output with
    a renderer source. The skip stays unconditional because it has to
    hold on the coupling where the lane does have a writer.

    Used to gate the AEC bridge FAIL: ref-silent windows are only
    diagnostic of a broken reference chain when music IS being routed
    through the loopback. When no renderer is writing, ref-silent is
    the expected state and mic-loud bursts are most likely room voice
    or ambient noise.

    Note the blind spots before using this as "the speaker is silent":
    it observes only the snd-aloop renderer lanes, and two live sources
    open none of them —

    * USB Audio Input, DIRECT-captured by jasper-fanin from
      hw:UAC2Gadget, which opens no playback lane here;
    * any renderer lane ARMED for ring ingress (U3/P6), which writes a
      per-renderer SHM ring instead of its aloop substream.

    So False means "no snd-aloop renderer lane is open", NOT "nothing
    is playing". A caller that needs true output silence must consult
    fan-in's DIRECT lane and the armed lane set as well.

    RETIREMENT. This reader survives the audio-graph consolidation
    (#2285) only because the snd-aloop renderer lanes are still a
    supported configuration — a box on `loopback` coupling, or one whose
    ring platform never armed, still opens subs 0..4 and this is still
    the only place that fact lives. It has nothing left to observe once
    the renderer lanes stop using snd-aloop (every renderer on ring
    ingress AND the per-renderer aloop PCM definitions deleted); delete
    it then, together with its two callers' music-active gates. Do not
    retire it on fleet arming state alone — the fleet being ring-armed
    is not the code dropping aloop support.
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
