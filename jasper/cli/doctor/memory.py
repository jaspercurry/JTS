"""jasper-doctor checks — memory domain.

Re-homed verbatim from the original monolithic
``jasper/cli/doctor.py``; see ``jasper/cli/doctor/__init__.py``
for the package overview and ``_registry.py`` for how order is
preserved. No check logic changed in the split."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from ._registry import doctor_check
from ._shared import (
    CheckResult,
    _meminfo_kb,
    _pid_of_unit,
    _systemctl_show_property,
)

@doctor_check(order=33, group="memory")
def check_ram() -> CheckResult:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])
                    mb = kb // 1024
                    if mb < 1500:
                        return CheckResult(
                            "RAM", "warn",
                            f"{mb} MB total — recommend 2GB Pi 5 for v1 stack",
                        )
                    return CheckResult("RAM", "ok", f"{mb} MB total")
    except Exception:  # noqa: BLE001
        pass
    return CheckResult("RAM", "warn", "couldn't read /proc/meminfo")

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
    # Percentage-with-floor pattern — see Prometheus node_exporter
    # alert conventions and Pop!_OS pop-os/default-settings#163.
    fail_mb = max(30, total_mb * 3 // 100)
    warn_mb = max(100, total_mb * 10 // 100)
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
    units = list(_EXPECTED_OOM_ADJ.keys())
    # Batch both systemctl-show calls — one subprocess per property
    # instead of one per (property × unit).
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
        units, _EXPECTED_OOM_ADJ.values(), pids_raw, configs_raw,
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
            f"{len(_EXPECTED_OOM_ADJ) - len(missing)} daemons protected; "
            f"{len(missing)} not running ({', '.join(missing)})",
        )
    return CheckResult(
        "OOM score adj", "ok",
        f"all {len(_EXPECTED_OOM_ADJ)} critical daemons protected",
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
    for unit in _AUDIO_PATH_UNITS:
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
        running = len(_AUDIO_PATH_UNITS) - len(missing)
        return CheckResult(
            "audio path no-swap", "ok",
            f"{running} audio daemons running, all swap-free; "
            f"{len(missing)} not running ({', '.join(missing)})",
        )
    return CheckResult(
        "audio path no-swap", "ok",
        f"all {len(_AUDIO_PATH_UNITS)} audio-path daemons swap-free",
    )
