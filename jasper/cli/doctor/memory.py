# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-doctor checks — memory domain.

Re-homed verbatim from the original monolithic
``jasper/cli/doctor.py``; see ``jasper/cli/doctor/__init__.py``
for the package overview and ``_registry.py`` for how order is
preserved. No check logic changed in the split.

The disk-pressure checks (``check_disk_space``,
``check_correction_storage``, ``check_wake_events_storage``) live
here rather than in a new module because they share this domain's
shape exactly: a full root filesystem is the same class of
slow-burn resource exhaustion as a full zram device, and a full SD
card on an unclean power-cut is the corruption hazard the whole
resilience ladder (Tier 5 watchdog, persistent journal, OOM ladder)
exists to survive — yet nothing warned before the write failed. They
follow the percentage-with-floor / skip-on-not-applicable conventions
the RAM and zram checks already established."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from ...install_profile import is_streambox_install_profile, read_install_profile
from ._registry import doctor_check
from ._shared import (
    CheckResult,
    _installed_units,
    _meminfo_kb,
    _pid_of_unit,
    _systemctl_show_property,
)

def _install_profile_is_streambox() -> bool:
    """True when this box runs the streambox tier (local audio, no voice brain).

    Fails toward False so a transient marker-read glitch keeps the louder
    full-speaker RAM warning rather than silently suppressing it.
    """
    try:
        return is_streambox_install_profile(read_install_profile())
    except (TypeError, ValueError, OSError):
        return False


@doctor_check(order=33, group="memory")
def check_ram() -> CheckResult:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])
                    mb = kb // 1024
                    if mb < 1500:
                        # The "recommend a bigger board" signal is a
                        # full-speaker sizing check. Streambox is the
                        # deliberately-light tier a small board resolves to
                        # (a Zero 2 W -> streambox), so a board-size warn
                        # there is a false positive — live memory pressure is
                        # caught SKU-agnostically by check_memory_headroom.
                        if _install_profile_is_streambox():
                            return CheckResult(
                                "RAM", "ok",
                                f"{mb} MB total (streambox tier; live "
                                "pressure covered by the memory-headroom "
                                "check)",
                            )
                        return CheckResult(
                            "RAM", "warn",
                            f"{mb} MB total — recommend 2GB Pi 5 for v1 stack",
                        )
                    return CheckResult("RAM", "ok", f"{mb} MB total")
    except Exception:  # noqa: BLE001
        pass
    return CheckResult("RAM", "warn", "couldn't read /proc/meminfo")

def memory_headroom_thresholds(total_mb: int) -> tuple[int, int]:
    """The ``(warn_mb, fail_mb)`` MemAvailable floors for a box with
    ``total_mb`` of RAM — the single source of truth for memory-pressure
    thresholds.

    Percentage-of-RAM with absolute MB floors (the Prometheus node_exporter /
    Pop!_OS ``pop-os/default-settings#163`` convention): warn below
    ``max(100 MB, 10%)``, fail below ``max(30 MB, 3%)``. The ``/system/``
    dashboard mirrors these exact numbers in
    ``deploy/assets/system-status/js/format.js`` (``memoryHeadroomLimits``);
    ``tests/test_system_status_thresholds.py`` pins the two together so the
    memory tile's colour can never again disagree with this check — the drift
    (dashboard fixed-150 MB vs this check) was the original bug."""
    return (
        max(100, total_mb * 10 // 100),
        max(30, total_mb * 3 // 100),
    )


@doctor_check(order=34, group="memory")
def check_memory_headroom() -> CheckResult:
    """Live memory pressure check: WARN if MemAvailable is so low that
    the next ad-hoc allocation will tip the box into zram-thrash.

    Thresholds are percentage-of-RAM with absolute MB floors, so this
    fires sanely on every Pi SKU (1 GB through 16 GB) without needing
    per-tier branching:
      warn if  available < max(100 MB, 10% of total)
      fail if  available < max(30 MB,  3% of total)

    On 1 GB:  warn at 100 MB, fail at 30 MB
    On 2 GB:  warn at 200 MB, fail at 60 MB
    On 8 GB:  warn at 800 MB, fail at 240 MB

    The 2026-05-23 incident shape was MemAvailable falling from
    ~250 MB to single-digit MB over ~10 s as a PIO compile ramped
    up; this check catches that BEFORE the wedge if the operator
    runs the doctor first."""
    total_kb = _meminfo_kb("MemTotal") or 0
    avail_kb = _meminfo_kb("MemAvailable")
    if avail_kb is None or total_kb == 0:
        return CheckResult(
            "memory headroom", "warn", "couldn't read /proc/meminfo",
        )
    avail_mb = avail_kb // 1024
    total_mb = total_kb // 1024
    pct = (avail_kb * 100) // total_kb if total_kb else 0
    warn_mb, fail_mb = memory_headroom_thresholds(total_mb)
    if avail_mb < fail_mb:
        return CheckResult(
            "memory headroom", "fail",
            f"only {avail_mb} MB available ({pct}%) — OOM imminent "
            f"(fail threshold {fail_mb} MB)",
        )
    if avail_mb < warn_mb:
        return CheckResult(
            "memory headroom", "warn",
            f"only {avail_mb} MB available ({pct}%) — tight "
            f"(warn threshold {warn_mb} MB)",
        )
    return CheckResult(
        "memory headroom", "ok",
        f"{avail_mb} MB available ({pct}%)",
    )

@doctor_check(order=35, group="memory")
def check_zram_size_ratio() -> CheckResult:
    """Verify the rpi-swap drop-in sized zram to ≤60% of RAM. The
    old zramswap default was 100% of RAM, which amplifies thrash
    (more zsmalloc bookkeeping during reclaim). Stage 1 of the
    memory-resilience plan reduces this to 50%.

    Skip cleanly if:
      - zram isn't in use at all (older RPi OS / dphys-swapfile setups)
      - rpi-swap isn't installed (Bookworm or earlier — JTS's drop-in
        targets rpi-swap exclusively, so on other zram managers there
        is no actionable fix for the operator from this side)"""
    try:
        zram_size_bytes = int(Path("/sys/block/zram0/disksize").read_text().strip())
    except (OSError, ValueError):
        return CheckResult(
            "zram size", "ok", "no zram0 device (rpi-swap not active)",
        )
    if zram_size_bytes == 0:
        return CheckResult("zram size", "ok", "zram0 present but unsized")
    total_kb = _meminfo_kb("MemTotal") or 0
    if total_kb == 0:
        return CheckResult("zram size", "warn", "couldn't compute ratio")
    total_bytes = total_kb * 1024
    pct = (zram_size_bytes * 100) // total_bytes
    zram_mb = zram_size_bytes // (1024 * 1024)
    if pct > 60:
        # If rpi-swap isn't installed, the JTS drop-in is moot —
        # different package owns the zram device. Don't warn the
        # operator about something they can't fix from this side.
        # Detection: /etc/rpi/swap.conf exists iff rpi-swap is the
        # canonical Pi-side zram manager (Trixie default).
        if not Path("/etc/rpi/swap.conf").exists():
            return CheckResult(
                "zram size", "ok",
                f"{zram_mb} MB ({pct}% of RAM) — managed by a different "
                f"zram package (rpi-swap not installed); JTS drop-in is inert",
            )
        return CheckResult(
            "zram size", "warn",
            f"{zram_mb} MB ({pct}% of RAM) — old default; "
            f"Stage 1 plan recommends 50%. If the drop-in is present "
            f"(check /etc/rpi/swap.conf.d/50-jts.conf), reboot to apply "
            f"— rpi-swap is a generator (runs at boot, not a service).",
        )
    return CheckResult(
        "zram size", "ok", f"{zram_mb} MB ({pct}% of RAM)",
    )

@doctor_check(order=36, group="memory")
def check_mglru_min_ttl() -> CheckResult:
    """Verify MGLRU min_ttl_ms is set to prevent thrashing under
    memory pressure. Stage 1 of the memory-resilience plan ships
    1000 ms via /etc/tmpfiles.d/jts-mglru.conf. Skip cleanly on
    kernels without MGLRU (< 6.1) — the tmpfiles config uses
    `w-` which silently no-ops there."""
    p = Path("/sys/kernel/mm/lru_gen/min_ttl_ms")
    if not p.exists():
        return CheckResult(
            "MGLRU min_ttl", "ok",
            "kernel lacks MGLRU (< 6.1) — thrash prevention via watermarks only",
        )
    try:
        v = int(p.read_text().strip())
    except (OSError, ValueError):
        return CheckResult("MGLRU min_ttl", "warn", "couldn't read value")
    if v == 0:
        return CheckResult(
            "MGLRU min_ttl", "warn",
            "0 ms (default) — thrash prevention disabled. "
            "Run `sudo systemd-tmpfiles --create /etc/tmpfiles.d/jts-mglru.conf` "
            "or re-run install.sh.",
        )
    if v != 1000:
        return CheckResult(
            "MGLRU min_ttl", "ok",
            f"{v} ms (non-default — operator override)",
        )
    return CheckResult("MGLRU min_ttl", "ok", "1000 ms")

_JTS_SYSCTL_CONF = Path("/etc/sysctl.d/99-jts-vm.conf")

@dataclass
class _SysctlConf:
    """Result of parsing the JTS sysctl drop-in.

    `values` — vm.* keys with resolved numeric/string values.
    `unresolved` — vm.* keys whose value is an unsubstituted template
        placeholder like '__VM_MIN_FREE_KBYTES__'. A non-empty list
        means install.sh's sed step failed for that key — the kernel
        will silently use whatever it had before, and the doctor
        should warn so the operator knows their config is broken."""
    values: dict[str, str]
    unresolved: list[str]

def _parse_jts_sysctl_conf() -> _SysctlConf:
    """Parse the JTS sysctl drop-in. Key (after `vm.`) maps to the
    resolved value if it's a real value, or lands in `unresolved` if
    the template substitution failed."""
    values: dict[str, str] = {}
    unresolved: list[str] = []
    if not _JTS_SYSCTL_CONF.exists():
        return _SysctlConf(values=values, unresolved=unresolved)
    try:
        for line in _JTS_SYSCTL_CONF.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if not key.startswith("vm."):
                continue
            # Drop any 'vm.' prefix — we'll match against /proc/sys/vm/<key>.
            sub_key = key[3:]
            # Template placeholder (install.sh's sed step failed)?
            if value.startswith("__") and value.endswith("__"):
                unresolved.append(sub_key)
                continue
            values[sub_key] = value
    except OSError:
        return _SysctlConf(values={}, unresolved=[])
    return _SysctlConf(values=values, unresolved=unresolved)

@doctor_check(order=37, group="memory")
def check_sysctl_drift() -> CheckResult:
    """Verify the vm.* tunings from /etc/sysctl.d/99-jts-vm.conf
    took effect. Drift detection — not a failure, just informational
    so the operator knows whether to re-apply via `sudo sysctl --system`
    or reboot.

    Reads expected values from the installed conf file rather than
    hardcoding, so RAM-dependent values (vm.min_free_kbytes, which
    install.sh computes per-Pi as 2% of RAM) are checked against
    the right target for THIS hardware. On systems with no
    /proc/sys/vm/ at all (e.g. running the doctor in a dev
    container), skip cleanly."""
    conf = _parse_jts_sysctl_conf()
    if not conf.values and not conf.unresolved:
        return CheckResult(
            "vm.* sysctls", "warn",
            f"{_JTS_SYSCTL_CONF} missing or empty — re-run install.sh",
        )
    # Unresolved template placeholders are a higher-priority warning
    # than drift — they mean install.sh's sed step failed and the
    # operator is running with kernel defaults for those knobs (not
    # what they wanted).
    if conf.unresolved:
        return CheckResult(
            "vm.* sysctls", "warn",
            "unsubstituted template placeholder(s) in conf: " +
            ", ".join(f"vm.{k}" for k in conf.unresolved) +
            ". install.sh's sed step likely failed — re-run install.sh.",
        )
    expected = conf.values
    drift = []
    checked = 0
    for key, want in expected.items():
        path = Path(f"/proc/sys/vm/{key}")
        if not path.exists():
            continue  # kernel doesn't expose this knob
        try:
            got = path.read_text().strip()
        except OSError:
            continue
        checked += 1
        if got != want:
            drift.append(f"vm.{key}={got} (want {want})")
    if checked == 0:
        return CheckResult(
            "vm.* sysctls", "ok", "/proc/sys/vm not available (not Linux?)",
        )
    if drift:
        return CheckResult(
            "vm.* sysctls", "warn",
            "drift: " + ", ".join(drift) +
            ". Run `sudo sysctl --system` or check /etc/sysctl.d/99-jts-vm.conf.",
        )
    return CheckResult(
        "vm.* sysctls", "ok", f"all {checked} expected values live",
    )

# OOMScoreAdjust values are the canonical set from jasper._oom_adj —
# shared with install.sh so a future tweak only touches one file.
# See jasper/_oom_adj.py for rationale per daemon.
from ..._oom_adj import EXPECTED as _EXPECTED_OOM_ADJ  # noqa: E402

def _expected_oom_score_adj() -> dict[str, int]:
    return _EXPECTED_OOM_ADJ

@doctor_check(order=38, group="memory")
def check_oom_score_adj() -> CheckResult:
    """Verify critical daemons have the OOMScoreAdjust we configured.
    Checks BOTH the live process (/proc/<pid>/oom_score_adj — the
    kernel's actual victim-selection value) AND the unit-file value
    (systemctl show -p OOMScoreAdjust — what's set for the NEXT
    restart). Live drift means a process was started before the
    new unit landed → next restart fixes it. Configured drift means
    the unit file itself doesn't have the directive → next restart
    *won't* fix it, so we surface both shapes separately."""
    expected = _expected_oom_score_adj()
    # Only verify units this install profile actually installs. The
    # EXPECTED map is the full-speaker set; a streambox does not install
    # the voice/AEC stack, so those absent units are not OOM drift.
    installed = _installed_units(list(expected.keys()))
    if installed is None:
        return CheckResult(
            "OOM score adj", "ok",
            "systemctl unavailable — skipped (not Linux?)",
        )
    expected = {u: v for u, v in expected.items() if u in installed}
    units = list(expected.keys())
    if not units:
        return CheckResult(
            "OOM score adj", "ok", "no managed daemons installed",
        )
    # Batch the value reads — one subprocess per property instead of one
    # per (property × unit). (The LoadState filter above is a third.)
    pids_raw = _systemctl_show_property("MainPID", units)
    configs_raw = _systemctl_show_property("OOMScoreAdjust", units)
    if pids_raw is None or configs_raw is None:
        return CheckResult(
            "OOM score adj", "ok",
            "systemctl unavailable — skipped (not Linux?)",
        )
    live_drift = []   # /proc/PID disagrees with expected
    config_drift = []  # systemctl show disagrees with expected
    missing = []
    for unit, want, pid_str, config_str in zip(
        units, expected.values(), pids_raw, configs_raw,
    ):
        # Parse configured value. systemd returns "0" when
        # OOMScoreAdjust= is absent from the unit (its default).
        try:
            configured = int(config_str) if config_str else 0
        except ValueError:
            configured = None
        if configured is not None and configured != want:
            config_drift.append(f"{unit} unit={configured} (want {want})")
        # Parse PID. systemctl returns "0" when the unit isn't running.
        try:
            pid = int(pid_str) if pid_str else 0
        except ValueError:
            pid = 0
        if pid <= 0:
            missing.append(unit)
            continue
        try:
            got = int(Path(f"/proc/{pid}/oom_score_adj").read_text().strip())
        except (OSError, ValueError):
            continue
        # OpenSSH on Raspberry Pi OS keeps the privileged listener at
        # -1000 even when systemd's unit setting is -250; accepted SSH
        # sessions inherit the unit's moderate value. Treat the unit-file
        # setting as authoritative for ssh so the doctor does not warn on
        # the listener's self-protection.
        if unit == "ssh" and configured == want:
            continue
        if got != want:
            live_drift.append(f"{unit} live={got} (want {want})")
    if config_drift:
        # Unit-file drift is the more serious case — survives restarts.
        return CheckResult(
            "OOM score adj", "warn",
            "UNIT FILE drift (next restart won't fix): " +
            ", ".join(config_drift) +
            ". Re-run install.sh to restore .service files.",
        )
    if live_drift:
        return CheckResult(
            "OOM score adj", "warn",
            "live-process drift (will fix on next restart): " +
            ", ".join(live_drift) +
            ". `systemctl restart <unit>` to apply now.",
        )
    if missing:
        return CheckResult(
            "OOM score adj", "ok",
            f"{len(expected) - len(missing)} daemons protected; "
            f"{len(missing)} not running ({', '.join(missing)})",
        )
    return CheckResult(
        "OOM score adj", "ok",
        f"all {len(expected)} critical daemons protected",
    )

# --- Stage 2 audio-protection checks (shipped 2026-05-24) ---
#
# These verify that the audio-path daemons' pages won't be swapped to
# zram under memory pressure — the failure mode confirmed empirically
# by the 2026-05-24 stress test (splotchy/crushed music as zram
# decompression jitter blew the ALSA buffer timing budget).


@doctor_check(order=41, group="memory")
def check_cgroup_memory_enabled() -> CheckResult:
    """Verify the Linux memory cgroup controller is actually enabled.
    Required for `MemorySwapMax=0` on jts-audio.slice / jts-mic.slice
    to enforce. The Pi 5 DTB defaults to `cgroup_disable=memory`;
    install.sh adds `cgroup_enable=memory` to cmdline.txt to override.
    Failure here means the audio-slice protection is silently a
    no-op — exactly the trap PR1 + PR1.6 documented for the existing
    `MemoryHigh=`/`MemoryMax=` directives."""
    p = Path("/sys/fs/cgroup/cgroup.controllers")
    if not p.exists():
        return CheckResult(
            "cgroup memory", "ok",
            "/sys/fs/cgroup not present (not Linux?)",
        )
    try:
        controllers = p.read_text().strip().split()
    except OSError:
        return CheckResult(
            "cgroup memory", "warn", "couldn't read cgroup.controllers",
        )
    if "memory" not in controllers:
        return CheckResult(
            "cgroup memory", "fail",
            "memory controller NOT enabled — audio-slice MemorySwapMax=0 "
            "is silently a no-op. Reboot to apply install.sh's cmdline.txt "
            "edit (cgroup_enable=memory).",
        )
    return CheckResult(
        "cgroup memory", "ok",
        "controller enabled (audio-slice protection effective)",
    )

# Audio-path daemons that should NEVER accumulate VmSwap. The check
# is permissive about small transient values (kernel sometimes evicts
# a few pages during process startup) but warns if any daemon has
# meaningful swap — that's the 2026-05-24 failure-mode signature.
_AUDIO_PATH_UNITS = (
    "jasper-fanin",
    "jasper-outputd",
    "jasper-camilla",
    "jasper-aec-bridge",
    "shairport-sync",
    "librespot",
    "bluealsa-aplay",
)

def _audio_path_units() -> tuple[str, ...]:
    return _AUDIO_PATH_UNITS

# Threshold for "this daemon has meaningful pages in zram" — well above
# the small (<100 kB) transient that's normal at startup, well below
# the 42 MB observed on aec-bridge during the 2026-05-24 stress.
_AUDIO_VMSWAP_WARN_KB = 1024  # 1 MB

@doctor_check(order=42, group="memory")
def check_audio_path_no_swap() -> CheckResult:
    """Verify audio-path daemons have ~0 pages in zram. If any are
    swapped meaningfully (>1 MB), it means either the slice's
    `MemorySwapMax=0` isn't enforcing (cgroup memory not enabled,
    Slice= not assigned, or daemon not in the slice) — OR pressure
    has already started evicting audio pages, in which case music
    quality is at risk."""
    swapped: list[str] = []
    missing: list[str] = []
    units = _audio_path_units()
    for unit in units:
        pid = _pid_of_unit(unit)
        if pid is None:
            missing.append(unit)
            continue
        try:
            status = Path(f"/proc/{pid}/status").read_text()
        except OSError:
            continue
        vmswap_kb = 0
        for line in status.split("\n"):
            if line.startswith("VmSwap:"):
                try:
                    vmswap_kb = int(line.split()[1])
                except (IndexError, ValueError):
                    pass
                break
        if vmswap_kb > _AUDIO_VMSWAP_WARN_KB:
            swapped.append(f"{unit}={vmswap_kb} kB")
    if swapped:
        return CheckResult(
            "audio path no-swap", "warn",
            "audio-path daemons with pages in zram: " +
            ", ".join(swapped) +
            ". Check Slice= and cgroup_enable=memory; music may glitch "
            "under load until restored.",
        )
    if missing:
        running = len(units) - len(missing)
        return CheckResult(
            "audio path no-swap", "ok",
            f"{running} audio daemons running, all swap-free; "
            f"{len(missing)} not running ({', '.join(missing)})",
        )
    return CheckResult(
        "audio path no-swap", "ok",
        f"all {len(units)} audio-path daemons swap-free",
    )


# --- Disk-pressure checks (the slow-burn resource the resilience ladder
#     exists to survive) -------------------------------------------------
#
# A full SD card is the corruption hazard behind the 2026-05-23 incident
# class: write fails -> in-flight ext4 metadata -> dirty power-cut leaves
# the partition needing recovery (worst case, an unbootable Pi). RAM and
# zram already have live-pressure doctor lines; the root filesystem did
# not. These add the missing early warning. Thresholds mirror the
# memory-headroom check's "fail takes precedence over warn" shape so an
# operator who raises the warn knob can never accidentally suppress the
# fail.

_DEFAULT_DISK_WARN_PERCENT = 85
_DISK_FAIL_PERCENT = 95
_GIB = 1024 ** 3


def _disk_warn_percent() -> int:
    """Operator-tunable WARN threshold (percent used). Falls back to the
    85% default on unset / unparseable / out-of-range values so a fat-
    fingered env line can't silently disable the warning. The FAIL
    threshold (95%) is fixed — it is the "writes are about to fail"
    line, not a preference."""
    raw = os.environ.get("JASPER_DISK_WARN_PERCENT", "").strip()
    if not raw:
        return _DEFAULT_DISK_WARN_PERCENT
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_DISK_WARN_PERCENT
    # Keep it strictly below the fail line and above 0 so the band is
    # always meaningful.
    if value <= 0 or value >= _DISK_FAIL_PERCENT:
        return _DEFAULT_DISK_WARN_PERCENT
    return value


@doctor_check(order=42.1, group="memory")
def check_disk_space() -> CheckResult:
    """WARN/FAIL on root-filesystem fullness before writes start failing.

    A full root partition is the failure that turns a routine power-cut
    into ext4 corruption (the 2026-05-23 incident class). os.statvfs is
    POSIX-portable and needs no subprocess. Uses f_bavail (blocks
    available to a non-root user) for the free figure so the number
    matches what the daemons — none of which run as root for their data
    writes — actually have, but computes "% used" from total vs free so
    the reserved-blocks pool doesn't read as usable headroom.

    Skips cleanly (ok) when os.statvfs is unavailable (non-POSIX dev
    host) — same skip-on-not-applicable posture as the /proc and /sys
    checks above. The path and the numbers are the only detail, so it is
    inherently secret-free."""
    path = "/"
    statvfs = getattr(os, "statvfs", None)
    if statvfs is None:
        return CheckResult(
            "disk space", "ok", "os.statvfs unavailable — skipped (not POSIX?)",
        )
    try:
        st = statvfs(path)
    except OSError as e:
        return CheckResult(
            "disk space", "warn",
            f"couldn't statvfs {path}: {e.__class__.__name__}",
        )
    total = st.f_blocks * st.f_frsize
    if total <= 0:
        return CheckResult("disk space", "ok", f"{path}: zero-sized (skipped)")
    free = st.f_bavail * st.f_frsize
    used = total - free
    pct_used = (used * 100) // total
    free_gib = free / _GIB
    warn_pct = _disk_warn_percent()
    summary = f"{path}: {pct_used}% used, {free_gib:.1f} GiB free"
    if pct_used >= _DISK_FAIL_PERCENT:
        return CheckResult(
            "disk space", "fail",
            summary + f" — {_DISK_FAIL_PERCENT}%+ full; writes will start "
            "failing and an unclean power-cut risks ext4 corruption. Free "
            "space now (prune /var/lib/jasper/wake-events, old correction "
            "sessions, journal: `journalctl --vacuum-size=100M`).",
        )
    if pct_used >= warn_pct:
        return CheckResult(
            "disk space", "warn",
            summary + f" — over {warn_pct}% warn threshold "
            "(JASPER_DISK_WARN_PERCENT). Reclaim space before it fills.",
        )
    return CheckResult("disk space", "ok", summary)


# Bounds for the read-only storage-size walks below. A doctor check must
# never run away on a 1 GB Pi, so the walk is hard-capped on BOTH entries
# examined and directory depth — a corrupted dir with millions of entries
# can't turn a health probe into an I/O storm. When a cap is hit the size
# is reported as a lower bound ("≥") rather than silently undercounted.
_STORAGE_WALK_MAX_ENTRIES = 50_000
_STORAGE_WALK_MAX_DEPTH = 6


def _bounded_dir_size(root: Path) -> tuple[int, bool]:
    """Sum file sizes under ``root`` with a bounded ``os.scandir`` walk.

    Returns ``(total_bytes, truncated)``. ``truncated`` is True when
    either the entry cap or the depth cap stopped the walk early, so the
    caller can render the figure as a floor. Deliberately self-contained
    (does not reuse jasper.correction.bundles' unbounded ``rglob`` helper)
    because a doctor probe must stay total and cheap regardless of how
    pathological the directory has become. Symlinks are not followed
    (``scandir`` is_dir/is_file default) so a stray symlink loop can't
    inflate the count or escape the tree. Per-entry OSErrors are skipped,
    never raised."""
    total = 0
    entries_seen = 0
    truncated = False
    # Iterative DFS with an explicit (path, depth) stack — no recursion,
    # so depth is a hard numeric bound, not call-stack-limited.
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        current, depth = stack.pop()
        try:
            it = os.scandir(current)
        except OSError:
            continue
        with it:
            for entry in it:
                entries_seen += 1
                if entries_seen > _STORAGE_WALK_MAX_ENTRIES:
                    truncated = True
                    return total, truncated
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if depth + 1 <= _STORAGE_WALK_MAX_DEPTH:
                            stack.append((Path(entry.path), depth + 1))
                        else:
                            truncated = True
                    elif entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
    return total, truncated


def _storage_check(
    *,
    label: str,
    path: Path,
    warn_bytes: int,
    knob: str,
    note: str,
) -> CheckResult:
    """Shared body for the read-only storage-size warnings.

    Read-only by contract: this reports growth, it never prunes or
    deletes — retention is owned by the wake-event ring and the
    correction subsystem themselves. Absent dir is ok (the feature just
    hasn't produced data yet)."""
    if not path.exists():
        return CheckResult(label, "ok", f"{path} absent (no data yet)")
    if not path.is_dir():
        return CheckResult(label, "ok", f"{path} is not a directory (skipped)")
    total, truncated = _bounded_dir_size(path)
    mib = total / (1024 * 1024)
    floor = "≥" if truncated else ""
    detail = f"{floor}{mib:.0f} MiB under {path}"
    if truncated:
        detail += " (walk capped; lower bound)"
    if total >= warn_bytes:
        warn_mib = warn_bytes / (1024 * 1024)
        return CheckResult(
            label, "warn",
            detail + f" — over the {warn_mib:.0f} MiB warn threshold "
            f"({knob}). {note}",
        )
    return CheckResult(label, "ok", detail)


_DEFAULT_CORRECTION_STORAGE_WARN_BYTES = 512 * 1024 * 1024  # 512 MiB
_DEFAULT_WAKE_EVENTS_STORAGE_WARN_BYTES = 1300 * 1024 * 1024  # 1.3 GiB


def _storage_warn_bytes(knob: str, default: int) -> int:
    """Tunable byte threshold for a storage warning, falling back to the
    default on unset / unparseable / non-positive values."""
    raw = os.environ.get(knob, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


@doctor_check(order=42.2, group="memory")
def check_correction_storage() -> CheckResult:
    """Read-only size warning for the correction-session directory.

    Each room-correction run keeps sweeps, captures, and (optionally)
    private raw audio under /var/lib/jasper/correction/sessions/. On a
    1 GB-RAM / modest-SD Pi a few un-pruned sessions can quietly eat real
    SD headroom. This only *reports* growth — pruning stays owned by the
    correction subsystem; the doctor must not delete a household's
    measurement evidence. Threshold via
    JASPER_CORRECTION_STORAGE_WARN_BYTES (default 512 MiB)."""
    root = Path(
        os.environ.get("JASPER_CORRECTION_ROOT", "/var/lib/jasper/correction")
    )
    sessions = Path(
        os.environ.get(
            "JASPER_CORRECTION_SESSIONS_DIR", str(root / "sessions"),
        )
    )
    return _storage_check(
        label="correction storage",
        path=sessions,
        warn_bytes=_storage_warn_bytes(
            "JASPER_CORRECTION_STORAGE_WARN_BYTES",
            _DEFAULT_CORRECTION_STORAGE_WARN_BYTES,
        ),
        knob="JASPER_CORRECTION_STORAGE_WARN_BYTES",
        note=(
            "Review old sessions at http://jts.local/correction/ and re-run "
            "only if needed; the newest bundle is what's applied."
        ),
    )


@doctor_check(order=42.3, group="memory")
def check_wake_events_storage() -> CheckResult:
    """Read-only size warning for the wake-event corpus directory.

    The wake-event telemetry ring caps its WAV storage at
    JASPER_WAKE_EVENTS_MAX_AUDIO_BYTES (1 GiB default) and rolls
    oldest-first, so steady-state size is bounded — but the SQLite DB and
    any transient overshoot above the audio cap still live on the same SD
    card. This surfaces the on-disk total so an operator can catch a ring
    that has drifted well past its audio cap (a sign the reaper is wedged
    or the cap was raised and forgotten). Read-only — the ring owns its
    own oldest-first eviction. Threshold via
    JASPER_WAKE_EVENTS_STORAGE_WARN_BYTES (default 1.3 GiB, comfortably
    above the 1 GiB audio cap so a healthy ring never warns)."""
    wake_dir = Path(
        os.environ.get("JASPER_WAKE_EVENTS_DIR", "/var/lib/jasper/wake-events")
    )
    return _storage_check(
        label="wake-events storage",
        path=wake_dir,
        warn_bytes=_storage_warn_bytes(
            "JASPER_WAKE_EVENTS_STORAGE_WARN_BYTES",
            _DEFAULT_WAKE_EVENTS_STORAGE_WARN_BYTES,
        ),
        knob="JASPER_WAKE_EVENTS_STORAGE_WARN_BYTES",
        note=(
            "Well above the JASPER_WAKE_EVENTS_MAX_AUDIO_BYTES audio cap — "
            "check the ring reaper (journalctl -u jasper-voice | grep "
            "wake_events) or lower the cap."
        ),
    )
