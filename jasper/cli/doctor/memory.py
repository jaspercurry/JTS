# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-doctor checks — memory domain.

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
import subprocess
from pathlib import Path
from ...memory_policy import (
    DISK_FAIL_PERCENT,
    DISK_WARN_PERCENT,
    MEM_PSI_WARN_AVG60,
    ZRAM_TARGET_PERCENT,
    ZRAM_WARN_PERCENT,
    disk_usage,
    memory_headroom_thresholds,
    memory_pressure,
    zram_usage,
)
from ...wake_events import (
    DEFAULT_MAX_AUDIO_BYTES as _DEFAULT_WAKE_EVENTS_MAX_AUDIO_BYTES,
)
from ._evidence import evidence
from ._registry import doctor_check
from ._shared import CheckResult, _run

# Machine-stable codes naming which branch of a memory check produced a
# result (AGENTS.md: tests pin status + reason, never detail prose).
REASON_RAM_STREAMBOX_TIER = "ram_streambox_tier"
REASON_RAM_UNDERSIZED = "ram_undersized"
REASON_RAM_UNREADABLE = "ram_unreadable"

REASON_MEMORY_HEADROOM_UNREADABLE = "memory_headroom_unreadable"
REASON_MEMORY_HEADROOM_FAIL = "memory_headroom_critical"
REASON_MEMORY_HEADROOM_WARN = "memory_headroom_tight"

REASON_MEMORY_PRESSURE_NO_PSI = "memory_pressure_no_psi"
REASON_MEMORY_PRESSURE_HIGH = "memory_pressure_high"
REASON_MEMORY_PRESSURE_LOW = "memory_pressure_low"

REASON_ZRAM_ABSENT = "zram_absent"
REASON_ZRAM_UNSIZED = "zram_unsized"
REASON_ZRAM_RATIO_UNREADABLE = "zram_ratio_unreadable"
REASON_ZRAM_MANAGED_ELSEWHERE = "zram_managed_elsewhere"
REASON_ZRAM_OVERSIZED = "zram_oversized"

REASON_CGROUP_NOT_LINUX = "cgroup_not_linux"
REASON_CGROUP_UNREADABLE = "cgroup_controllers_unreadable"
REASON_CGROUP_MEMORY_DISABLED = "cgroup_memory_controller_disabled"

REASON_AUDIO_SWAP_DETECTED = "audio_path_swap_detected"
REASON_AUDIO_PATH_SOME_NOT_RUNNING = "audio_path_some_not_running"
REASON_AUDIO_PATH_ALL_SWAP_FREE = "audio_path_all_swap_free"

REASON_DISK_NOT_POSIX = "disk_not_posix"
REASON_DISK_STATVFS_FAILED = "disk_statvfs_failed"
REASON_DISK_ZERO_SIZED = "disk_zero_sized_filesystem"
REASON_DISK_FULL = "disk_full"
REASON_DISK_NEAR_FULL = "disk_near_full"

REASON_STORAGE_ABSENT = "storage_absent"
REASON_STORAGE_NOT_A_DIR = "storage_not_a_dir"
REASON_STORAGE_OVER_THRESHOLD = "storage_over_threshold"

REASON_JOURNALD_NOT_BOOTED = "journald_not_systemd_booted"
REASON_JOURNALD_NOT_PERSISTENT = "journald_not_persistent"
REASON_JOURNALD_CONFIG_UNREADABLE = "journald_config_unreadable"
REASON_JOURNALD_CAP_REGRESSED = "journald_retention_cap_regressed"

@doctor_check()
def check_ram() -> CheckResult:
    kb = evidence.meminfo().get("MemTotal")
    if kb is None:
        return CheckResult(
            "RAM", "skipped", "couldn't read /proc/meminfo",
            reason=REASON_RAM_UNREADABLE,
        )
    mb = kb // 1024
    if mb < 1500:
        # The "recommend a bigger board" signal is a full-speaker sizing
        # check. Streambox is the deliberately-light tier a small board
        # resolves to (a Zero 2 W -> streambox), so a board-size warn there
        # is a false positive — live memory pressure is caught
        # SKU-agnostically by check_memory_headroom.
        if evidence.install_profile_is_streambox():
            return CheckResult(
                "RAM", "ok",
                f"{mb} MB total (streambox tier; live "
                "pressure covered by the memory-headroom "
                "check)",
                reason=REASON_RAM_STREAMBOX_TIER,
            )
        return CheckResult(
            "RAM", "warn",
            f"{mb} MB total — recommend 2GB Pi 5 for v1 stack",
            reason=REASON_RAM_UNDERSIZED,
        )
    return CheckResult("RAM", "ok", f"{mb} MB total")

# "memory-sample" keeps this off the wire while another check is holding a
# large transient allocation of its own. Today that is voice.py's
# check_provider_importable, whose import child costs ~70 MB of MemAvailable
# — enough to cross this check's 100 MB warn threshold on a 1 GB Pi and make
# the doctor report a shortage it caused. A future check that allocates on
# that scale should join the same lane.
@doctor_check(exclusive_group="memory-sample")
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
    fields = evidence.meminfo()
    total_kb = fields.get("MemTotal") or 0
    avail_kb = fields.get("MemAvailable")
    if avail_kb is None or total_kb == 0:
        return CheckResult(
            "memory headroom", "skipped", "couldn't read /proc/meminfo",
            reason=REASON_MEMORY_HEADROOM_UNREADABLE,
        )
    avail_mb = avail_kb // 1024
    total_mb = total_kb // 1024
    pct = (avail_kb * 100) // total_kb
    warn_mb, fail_mb = memory_headroom_thresholds(total_mb)
    if avail_mb < fail_mb:
        return CheckResult(
            "memory headroom", "fail",
            f"only {avail_mb} MB available ({pct}%) — OOM imminent "
            f"(fail threshold {fail_mb} MB)",
            reason=REASON_MEMORY_HEADROOM_FAIL,
        )
    if avail_mb < warn_mb:
        return CheckResult(
            "memory headroom", "warn",
            f"only {avail_mb} MB available ({pct}%) — tight "
            f"(warn threshold {warn_mb} MB)",
            reason=REASON_MEMORY_HEADROOM_WARN,
        )
    return CheckResult(
        "memory headroom", "ok",
        f"{avail_mb} MB available ({pct}%)",
    )


@doctor_check()
def check_memory_pressure() -> CheckResult:
    """Surface kernel memory-stall pressure, which MemAvailable does not show.

    ``check_memory_headroom`` measures how much room is left; PSI measures how
    much time the box already spends waiting on reclaim, swap-in, or page-cache
    thrash. The verdict is the LIVE 60-second average only. The OOM-kill count
    is cumulative since boot, so it rides in the detail and never latches the
    row into a permanent warn.
    """
    name = "memory pressure"
    pressure = memory_pressure()
    psi = pressure.psi_some_avg60
    kills = pressure.oom_kills
    since_boot = "" if not kills else f"; {kills} OOM kill(s) since boot"
    if psi is None:
        # A kill observed without PSI is still an observation, so it is `ok`
        # with the count and never `skipped` (jasper.doctor_contract); the
        # /system tile draws the same line (sections.js: `cur.oom_kill > 0`).
        return CheckResult(
            name, "ok" if kills else "skipped",
            "kernel publishes no PSI (needs psi=1 on the cmdline and "
            f"CONFIG_PSI){since_boot}",
            reason=REASON_MEMORY_PRESSURE_NO_PSI,
        )
    if psi >= MEM_PSI_WARN_AVG60:
        return CheckResult(
            name, "warn",
            f"{psi:.1f}% of the last 60 s stalled on memory "
            f"(warn at {MEM_PSI_WARN_AVG60:.0f}%) — reclaim/zram thrash, not "
            f"just tight headroom{since_boot}",
            reason=REASON_MEMORY_PRESSURE_HIGH,
        )
    return CheckResult(
        name, "ok",
        f"{psi:.1f}% of the last 60 s stalled on memory{since_boot}",
        reason=REASON_MEMORY_PRESSURE_LOW,
    )

@doctor_check()
def check_zram_size_ratio() -> CheckResult:
    """Verify the rpi-swap drop-in sized zram near its target share of
    RAM. The old zramswap default was 100% of RAM, which amplifies
    thrash (more zsmalloc bookkeeping during reclaim);
    ``jasper.memory_policy.ZRAM_TARGET_PERCENT`` is the one number the
    installer sizes to, and ``ZRAM_WARN_PERCENT`` beside it is the band
    this check reads that live sizing against."""
    total_kb = evidence.meminfo().get("MemTotal", 0)
    usage = zram_usage(total_kb=total_kb)
    if usage is None:
        return CheckResult(
            "zram size", "skipped", "no zram0 device (rpi-swap not active)",
            reason=REASON_ZRAM_ABSENT,
        )
    if usage.disksize_bytes == 0:
        return CheckResult(
            "zram size", "skipped", "zram0 present but unsized",
            reason=REASON_ZRAM_UNSIZED,
        )
    if usage.total_bytes == 0:
        # See ZramUsage's docstring: this zero means MemTotal, not disksize.
        return CheckResult(
            "zram size", "skipped",
            "couldn't read MemTotal from /proc/meminfo",
            reason=REASON_ZRAM_RATIO_UNREADABLE,
        )
    pct = usage.percent_of_ram
    zram_mb = usage.disksize_bytes // (1024 * 1024)
    if pct > ZRAM_WARN_PERCENT:
        # If rpi-swap isn't installed, the JTS drop-in is moot — a
        # different package owns the zram device, unactionable here.
        # Detection: /etc/rpi/swap.conf exists iff rpi-swap is the
        # canonical Pi-side zram manager (Trixie default).
        if not Path("/etc/rpi/swap.conf").exists():
            return CheckResult(
                "zram size", "ok",
                f"{zram_mb} MB ({pct}% of RAM) — managed by a different "
                f"zram package (rpi-swap not installed); JTS drop-in is inert",
                reason=REASON_ZRAM_MANAGED_ELSEWHERE,
            )
        status = "ok" if Path("/etc/rpi/swap.conf.d/50-jts.conf").exists() else "warn"
        return CheckResult(
            "zram size", status,
            f"{zram_mb} MB ({pct}% of RAM) — old default; the JTS "
            f"drop-in targets {ZRAM_TARGET_PERCENT}%. If it is present "
            f"(check /etc/rpi/swap.conf.d/50-jts.conf), reboot to apply "
            f"— rpi-swap is a generator (runs at boot, not a service).",
            reason=REASON_ZRAM_OVERSIZED,
        )
    return CheckResult(
        "zram size", "ok", f"{zram_mb} MB ({pct}% of RAM)",
    )

# --- Stage 2 audio-protection checks (shipped 2026-05-24) ---
#
# These verify that the audio-path daemons' pages won't be swapped to
# zram under memory pressure — the failure mode confirmed empirically
# by the 2026-05-24 stress test (splotchy/crushed music as zram
# decompression jitter blew the ALSA buffer timing budget).


@doctor_check()
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
            "cgroup memory", "skipped",
            "/sys/fs/cgroup not present (not Linux?)",
            reason=REASON_CGROUP_NOT_LINUX,
        )
    try:
        controllers = p.read_text().strip().split()
    except OSError:
        return CheckResult(
            "cgroup memory", "skipped", "couldn't read cgroup.controllers",
            reason=REASON_CGROUP_UNREADABLE,
        )
    if "memory" not in controllers:
        return CheckResult(
            "cgroup memory", "fail",
            "memory controller NOT enabled — audio-slice MemorySwapMax=0 "
            "is silently a no-op. Reboot to apply install.sh's cmdline.txt "
            "edit (cgroup_enable=memory).",
            reason=REASON_CGROUP_MEMORY_DISABLED,
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
    "jasper-camilla-crossover",
    "jasper-aec-bridge",
    "jasper-snapclient",
    "jasper-snapserver",
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

@doctor_check()
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
        state = evidence.unit_state(f"{unit}.service")
        pid = state.get("main_pid", 0) if state else 0
        if not pid:
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
            reason=REASON_AUDIO_SWAP_DETECTED,
        )
    if missing:
        running = len(units) - len(missing)
        return CheckResult(
            "audio path no-swap", "ok",
            f"{running} audio daemons running, all swap-free; "
            f"{len(missing)} not running ({', '.join(missing)})",
            reason=REASON_AUDIO_PATH_SOME_NOT_RUNNING,
        )
    return CheckResult(
        "audio path no-swap", "ok",
        f"all {len(units)} audio-path daemons swap-free",
        reason=REASON_AUDIO_PATH_ALL_SWAP_FREE,
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

_GIB = 1024 ** 3


def _disk_warn_percent() -> int:
    """Operator-tunable WARN threshold (percent used). Falls back to
    ``DISK_WARN_PERCENT`` on unset / unparseable / out-of-range values so a
    fat-fingered env line can't silently disable the warning. The FAIL
    threshold is fixed — it is the "writes are about to fail" line, not a
    preference."""
    raw = os.environ.get("JASPER_DISK_WARN_PERCENT", "").strip()
    if not raw:
        return DISK_WARN_PERCENT
    try:
        value = int(raw)
    except ValueError:
        return DISK_WARN_PERCENT
    # Keep it strictly below the fail line and above 0 so the band is
    # always meaningful.
    if value <= 0 or value >= DISK_FAIL_PERCENT:
        return DISK_WARN_PERCENT
    return value


@doctor_check()
def check_disk_space() -> CheckResult:
    """WARN/FAIL on root-filesystem fullness before writes start failing.

    A full root partition is the failure that turns a routine power-cut
    into ext4 corruption (the 2026-05-23 incident class).

    Skips cleanly when the filesystem cannot be measured (non-POSIX dev
    host, zero-sized) — same skip-on-not-applicable posture as the /proc
    and /sys checks above. The path and the numbers are the only detail, so
    it is inherently secret-free."""
    path = "/"
    try:
        usage = disk_usage(path)
    except OSError as e:
        return CheckResult(
            "disk space", "warn",
            f"couldn't statvfs {path}: {e.__class__.__name__}",
            reason=REASON_DISK_STATVFS_FAILED,
        )
    if usage is None:
        return CheckResult(
            "disk space", "skipped", "os.statvfs unavailable — skipped (not POSIX?)",
            reason=REASON_DISK_NOT_POSIX,
        )
    if usage.total_bytes <= 0:
        return CheckResult(
            "disk space", "skipped", f"{path}: zero-sized",
            reason=REASON_DISK_ZERO_SIZED,
        )
    pct_used = int(usage.percent_used)
    warn_pct = _disk_warn_percent()
    summary = f"{path}: {pct_used}% used, {usage.free_bytes / _GIB:.1f} GiB free"
    if pct_used >= DISK_FAIL_PERCENT:
        return CheckResult(
            "disk space", "fail",
            summary + f" — {DISK_FAIL_PERCENT}%+ full; writes will start "
            "failing and an unclean power-cut risks ext4 corruption. Free "
            "space now (prune /var/lib/jasper/wake-events, old correction "
            "sessions, journal: `journalctl --vacuum-size=100M`).",
            reason=REASON_DISK_FULL,
        )
    if pct_used >= warn_pct:
        return CheckResult(
            "disk space", "warn",
            summary + f" — over {warn_pct}% warn threshold "
            "(JASPER_DISK_WARN_PERCENT). Reclaim space before it fills.",
            reason=REASON_DISK_NEAR_FULL,
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
    either the entry cap or :data:`_STORAGE_WALK_MAX_DEPTH` stopped the
    walk early, so the caller can render the figure as a floor.
    Deliberately self-contained (does not reuse jasper.correction.bundles'
    unbounded ``rglob`` helper)
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
    correction subsystem themselves. Absent dir is skipped (the feature
    just hasn't produced data yet, so there is nothing to observe)."""
    if not path.exists():
        return CheckResult(
            label, "skipped", f"{path} absent (no data yet)",
            reason=REASON_STORAGE_ABSENT,
        )
    if not path.is_dir():
        return CheckResult(
            label, "skipped", f"{path} is not a directory",
            reason=REASON_STORAGE_NOT_A_DIR,
        )
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
            reason=REASON_STORAGE_OVER_THRESHOLD,
        )
    return CheckResult(label, "ok", detail)


_DEFAULT_CORRECTION_STORAGE_WARN_BYTES = 512 * 1024 * 1024  # 512 MiB
# Headroom above the *configured* audio cap for the SQLite DB (grows
# forever, ~9 MB/year) plus any transient overshoot before a sweep catches
# up. A fixed allowance, not a fixed threshold, so it stays meaningful
# whatever the cap is set to (default or a deliberate override).
_WAKE_EVENTS_DB_ALLOWANCE_BYTES = 300 * 1024 * 1024  # 300 MiB


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


@doctor_check()
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
            "Review old sessions at http://jts.local/sound/room/ and re-run "
            "only if needed; the newest bundle is what's applied."
        ),
    )


@doctor_check()
def check_wake_events_storage() -> CheckResult:
    """Read-only size warning for the wake-event corpus directory.

    The wake-event telemetry ring caps its WAV storage at
    JASPER_WAKE_EVENTS_MAX_AUDIO_BYTES (128 MiB default) and rolls
    oldest-first, so steady-state size is bounded — but the SQLite DB and
    any transient overshoot above the audio cap still live on the same SD
    card. This surfaces the on-disk total so an operator can catch a ring
    that has drifted well past its audio cap (a sign the reaper is wedged
    or the cap was raised and forgotten). Read-only — the ring owns its
    own oldest-first eviction. Threshold via JASPER_WAKE_EVENTS_STORAGE_WARN_BYTES,
    defaulting to the *configured* audio cap (JASPER_WAKE_EVENTS_MAX_AUDIO_BYTES,
    env override respected) plus a fixed DB/overshoot allowance — so a Pi
    that deliberately keeps a larger cap doesn't get spurious warnings,
    and a healthy ring never warns."""
    wake_dir = Path(
        os.environ.get("JASPER_WAKE_EVENTS_DIR", "/var/lib/jasper/wake-events")
    )
    configured_cap = _storage_warn_bytes(
        "JASPER_WAKE_EVENTS_MAX_AUDIO_BYTES",
        _DEFAULT_WAKE_EVENTS_MAX_AUDIO_BYTES,
    )
    return _storage_check(
        label="wake-events storage",
        path=wake_dir,
        warn_bytes=_storage_warn_bytes(
            "JASPER_WAKE_EVENTS_STORAGE_WARN_BYTES",
            configured_cap + _WAKE_EVENTS_DB_ALLOWANCE_BYTES,
        ),
        knob="JASPER_WAKE_EVENTS_STORAGE_WARN_BYTES",
        note=(
            "Well above the JASPER_WAKE_EVENTS_MAX_AUDIO_BYTES audio cap — "
            "check the ring reaper (journalctl -u jasper-voice | grep "
            "wake_events) or lower the cap."
        ),
    )


# --- Persistent-journal retention check (Tier-5 forensics depend on it) -----
#
# deploy/journald/50-jts-persistent-storage.conf flips RPi OS's volatile
# default to Storage=persistent with a SystemMaxUse retention cap, so a
# watchdog reset's *previous-boot* logs survive (the whole point of Tier 5).
# This check catches the two silent
# regressions that would gut those forensics: persistence getting turned off,
# or the retention cap shrinking below what JTS installs (e.g. a stale/lower
# drop-in winning the precedence merge). Read-only — no journald mutation.

# The JTS-installed drop-in — the "installed value" the effective cap is
# compared against (never a hard-coded byte figure, so this can't drift from
# what deploy/journald/50-jts-persistent-storage.conf actually ships).
_JOURNALD_DROPIN = Path(
    "/etc/systemd/journald.conf.d/50-jts-persistent-storage.conf"
)

_JOURNALD_SIZE_SUFFIX = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}


def _parse_journald_size(raw: str | None) -> int | None:
    """Parse a journald size token to bytes. journald accepts a bare number
    (bytes) or a K/M/G/T suffix (base-1024, e.g. ``500M``). Returns None on
    an empty/unparseable/negative value."""
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    suffix = raw[-1].upper()
    if suffix in _JOURNALD_SIZE_SUFFIX:
        num, mult = raw[:-1], _JOURNALD_SIZE_SUFFIX[suffix]
    else:
        num, mult = raw, 1
    try:
        value = float(num)
    except ValueError:
        return None
    if value < 0:
        return None
    return int(value * mult)


def _journald_setting_last_wins(text: str, key: str) -> str | None:
    """Last-assignment-wins value for ``key=`` in a journald config text
    (comment/blank lines ignored). Mirrors systemd's own "last assignment
    across the merged config wins" rule, so passing the output of
    ``systemd-analyze cat-config`` yields the effective value."""
    val: str | None = None
    for line in text.splitlines():
        s = line.strip()
        if not s or s[0] in "#;":
            continue
        k, sep, v = s.partition("=")
        if sep and k.strip() == key:
            val = v.strip()
    return val


def _systemd_booted() -> bool:
    """True when this host is booted with systemd (so a journald drop-in is
    the applicable config surface). Fails toward False so the check skips
    cleanly on a non-systemd dev host rather than warning about a missing
    Pi-only drop-in."""
    return Path("/run/systemd/system").is_dir()


def _journald_effective_config() -> tuple[str | None, str | None]:
    """Effective ``(Storage, SystemMaxUse)`` as journald resolves them, read
    via ``systemd-analyze cat-config systemd/journald.conf`` — systemd does
    the drop-in precedence merge for us, so the last assignment wins. Returns
    ``(None, None)`` when the tool is unavailable (values then fall back to
    the JTS drop-in alone)."""
    try:
        proc = _run(
            ["systemd-analyze", "cat-config", "systemd/journald.conf"],
            timeout=10.0,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return (None, None)
    if proc.returncode != 0:
        return (None, None)
    text = proc.stdout or ""
    return (
        _journald_setting_last_wins(text, "Storage"),
        _journald_setting_last_wins(text, "SystemMaxUse"),
    )


def _journald_installed_cap_raw() -> str | None:
    """``SystemMaxUse`` from the JTS-installed drop-in, or None when the
    drop-in is absent/unreadable."""
    try:
        text = _JOURNALD_DROPIN.read_text()
    except OSError:
        return None
    return _journald_setting_last_wins(text, "SystemMaxUse")


def _journald_disk_usage() -> str:
    """One-line ``journalctl --disk-usage`` summary, or "" on any failure."""
    try:
        proc = _run(["journalctl", "--disk-usage"], timeout=10.0)
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return ""
    if proc.returncode != 0:
        return ""
    return " ".join((proc.stdout or "").split())


@doctor_check()
def check_journald_persistence() -> CheckResult:
    """Verify the persistent-journal drop-in is in effect: Storage=persistent
    so a watchdog reset's previous-boot logs survive, and the SystemMaxUse
    retention cap has not regressed below the value JTS installs. Read-only;
    effective values come from ``systemd-analyze cat-config`` (systemd merges
    drop-in precedence), the installed cap from the JTS drop-in, and current
    usage from ``journalctl --disk-usage``. Skips cleanly off a systemd host.

    Canonical config: deploy/journald/50-jts-persistent-storage.conf."""
    if not _systemd_booted():
        return CheckResult(
            "journald persistence", "skipped", "no systemd — skipped (not a Pi?)",
            reason=REASON_JOURNALD_NOT_BOOTED,
        )

    storage, eff_cap_raw = _journald_effective_config()
    installed_cap_raw = _journald_installed_cap_raw()
    usage = _journald_disk_usage()
    usage_suffix = f" ({usage})" if usage else ""

    # 1) Persistence off — the whole point of the drop-in is defeated.
    if storage is not None and storage.lower() != "persistent":
        if installed_cap_raw is None:
            fix = (
                " The JTS persistent-journal drop-in is not installed — "
                "re-run install.sh."
            )
        else:
            fix = (
                " The JTS drop-in requests persistent but another config "
                "overrides it (or journald was not restarted) — re-run "
                "install.sh / restart systemd-journald."
            )
        return CheckResult(
            "journald persistence", "warn",
            f"Storage={storage}, not persistent — a watchdog reset's "
            f"previous-boot forensics will not survive a reboot.{fix}",
            reason=REASON_JOURNALD_NOT_PERSISTENT,
        )

    # Drop-in absent and effective config unreadable: can't confirm persistence.
    if installed_cap_raw is None and storage is None:
        return CheckResult(
            "journald persistence", "warn",
            "persistent-journal drop-in not installed and effective config "
            "unreadable — watchdog-reset forensics may be volatile. Re-run "
            "install.sh.",
            reason=REASON_JOURNALD_CONFIG_UNREADABLE,
        )

    # 2) Retention cap regressed below what JTS installs.
    eff_cap = _parse_journald_size(eff_cap_raw)
    installed_cap = _parse_journald_size(installed_cap_raw)
    if eff_cap is not None and installed_cap is not None and eff_cap < installed_cap:
        return CheckResult(
            "journald persistence", "warn",
            f"SystemMaxUse effective {eff_cap_raw} is below the installed "
            f"{installed_cap_raw} — the forensics retention window has "
            f"regressed (a later journald drop-in is shrinking it)"
            f"{usage_suffix}.",
            reason=REASON_JOURNALD_CAP_REGRESSED,
        )

    cap_note = eff_cap_raw or installed_cap_raw or "systemd default"
    storage_note = storage or "persistent (drop-in)"
    return CheckResult(
        "journald persistence", "ok",
        f"Storage={storage_note}, SystemMaxUse={cap_note}{usage_suffix}",
    )
