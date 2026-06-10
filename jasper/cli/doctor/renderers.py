"""jasper-doctor checks — renderers domain.

Re-homed verbatim from the original monolithic
``jasper/cli/doctor.py``; see ``jasper/cli/doctor/__init__.py``
for the package overview and ``_registry.py`` for how order is
preserved. No check logic changed in the split."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional
from ...config import Config
from ...mux_mode_persistence import DEFAULT_PATH as _MUX_MODE_DEFAULT_PATH
from ...music_sources import MUSIC_SOURCES, Source
from ._registry import doctor_check
from ._shared import CheckResult, _run

# ----------------------------------------------------------------------
# Per-renderer health: each daemon's own surface (HTTP / DBus / system).
# ----------------------------------------------------------------------

@doctor_check(order=9, group="renderers", label="librespot.service", needs_cfg=True)
def check_librespot_running(cfg: Config) -> CheckResult:
    """Verify librespot is installed and the systemd unit is active.

    librespot 0.8.0 (rust) replaced go-librespot in the debian-stack
    on 2026-05-07 specifically for the configurable volume curve
    (--volume-ctrl log over 60 dB range). It has no local control
    HTTP, so health is checked via systemd state + binary version."""
    bin_path = "/usr/bin/librespot"
    if not os.path.isfile(bin_path):
        return CheckResult(
            "librespot binary", "fail",
            f"{bin_path} not present. Install: "
            "apt install raspotify (provides librespot via .deb)",
        )
    p = _run(["systemctl", "is-active", "librespot.service"])
    state = p.stdout.strip()
    if state != "active":
        return CheckResult(
            "librespot.service", "fail",
            f"systemctl is-active = '{state}'. Check: "
            "systemctl status librespot",
        )
    # Best-effort version line (librespot prints to stderr at startup)
    return CheckResult(
        "librespot.service", "ok",
        f"{bin_path} active (state file: "
        f"/run/librespot/state.json)",
    )

@doctor_check(order=10, group="renderers")
def check_shairport_sync_ap2() -> CheckResult:
    """Verify shairport-sync is installed with AirPlay 2 support
    AND the systemd unit is active. The Debian Trixie apt package
    is AP1-only; the migration's source-build emits a binary whose
    `-V` output contains 'AirPlay2'."""
    if shutil.which("shairport-sync") is None:
        return CheckResult(
            "shairport-sync AP2", "fail",
            "binary not found. Source-build per deploy/debian-stack/README.md",
        )
    p = _run(["shairport-sync", "-V"])
    out = (p.stdout + p.stderr).strip().split("\n")[0]
    if "AirPlay2" not in out:
        return CheckResult(
            "shairport-sync AP2", "fail",
            f"binary lacks --with-airplay-2 (got: {out!r}). "
            f"Apt's package is AP1-only; rebuild from source.",
        )
    p2 = _run(["systemctl", "is-active", "shairport-sync.service"])
    state = p2.stdout.strip()
    if state != "active":
        return CheckResult(
            "shairport-sync AP2", "fail",
            f"binary OK but systemd state={state}. "
            f"Check: journalctl -u shairport-sync",
        )
    return CheckResult("shairport-sync AP2", "ok", out)

@doctor_check(order=11, group="renderers")
def check_nqptp_running() -> CheckResult:
    """nqptp is required for AirPlay 2 timing. Without it,
    shairport-sync's AP2 path silently fails to handshake."""
    p = _run(["systemctl", "is-active", "nqptp.service"])
    state = p.stdout.strip()
    if state == "active":
        return CheckResult("nqptp", "ok", "active (UDP 319/320)")
    return CheckResult(
        "nqptp", "fail",
        f"state={state}. shairport-sync AP2 will not handshake "
        f"without nqptp running.",
    )

@doctor_check(order=14, group="renderers")
def check_jasper_mux() -> CheckResult:
    """jasper-mux arbitrates which renderer plays when. Without it,
    source selection and guarded handoff stop working; if fan-in has
    restarted into its safe NONE state, music may stay silent."""
    p = _run(["systemctl", "is-active", "jasper-mux.service"])
    state = p.stdout.strip()
    if state == "active":
        return CheckResult(
            "jasper-mux", "ok",
            "active (source selection + latest-source-wins)",
        )
    return CheckResult(
        "jasper-mux", "fail",
        f"state={state}. Source selection and guarded handoff are "
        f"unavailable; fan-in may remain silent until mux is restarted.",
    )

@doctor_check(order=12, group="renderers")
def check_bluealsa() -> CheckResult:
    """bluealsa daemon registers the A2DP profile with bluez;
    bluealsa-aplay forwards incoming A2DP audio to ALSA. Both
    must be active for "phone-as-Bluetooth-source → speaker"
    to work end-to-end."""
    p1 = _run(["systemctl", "is-active", "bluealsa.service"])
    p2 = _run(["systemctl", "is-active", "bluealsa-aplay.service"])
    s1 = p1.stdout.strip()
    s2 = p2.stdout.strip()
    if s1 == "active" and s2 == "active":
        return CheckResult("bluealsa", "ok", "daemon + aplay active")
    return CheckResult(
        "bluealsa", "fail",
        f"bluealsa={s1}, bluealsa-aplay={s2}. "
        f"Check: journalctl -u bluealsa",
    )

@doctor_check(order=13, group="renderers")
def check_bluetooth_pairing_policy() -> CheckResult:
    """Verify the JTS no-code pairing agent is installed and idle-closed."""
    expected_exec = "/opt/jasper/.venv/bin/jasper-bluetooth-agent"
    try:
        p = _run([
            "systemctl",
            "show",
            "bt-agent.service",
            "-p",
            "ActiveState",
            "-p",
            "SubState",
            "-p",
            "ExecStart",
        ])
    except FileNotFoundError:
        return CheckResult(
            "Bluetooth pairing policy",
            "warn",
            "systemctl unavailable — skipped",
        )
    if p.returncode != 0:
        return CheckResult(
            "Bluetooth pairing policy",
            "fail",
            "systemctl show bt-agent.service failed",
        )
    props = {}
    for line in p.stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            props[key] = value
    active = props.get("ActiveState", "")
    sub = props.get("SubState", "")
    if active != "active" or sub != "running":
        return CheckResult(
            "Bluetooth pairing policy",
            "fail",
            f"bt-agent.service state={active}/{sub}; no-code default agent not running",
        )
    exec_start = props.get("ExecStart", "")
    if expected_exec not in exec_start:
        return CheckResult(
            "Bluetooth pairing policy",
            "fail",
            f"bt-agent.service ExecStart is not the JTS no-code agent: {exec_start}",
        )

    try:
        bt = _run(["bluetoothctl", "show"])
    except FileNotFoundError:
        return CheckResult(
            "Bluetooth pairing policy",
            "warn",
            "agent OK, but bluetoothctl unavailable — adapter gate not checked",
        )
    if bt.returncode != 0:
        return CheckResult(
            "Bluetooth pairing policy",
            "warn",
            "agent OK, but bluetoothctl show failed — adapter gate not checked",
        )

    values: dict[str, str] = {}
    for line in bt.stdout.splitlines():
        key, sep, value = line.strip().partition(":")
        if sep:
            values[key] = value.strip().split(" ", 1)[0].lower()
    discoverable = values.get("Discoverable")
    pairable = values.get("Pairable")
    if discoverable is None or pairable is None:
        return CheckResult(
            "Bluetooth pairing policy",
            "warn",
            "agent OK, but adapter Discoverable/Pairable state was not reported",
        )
    if discoverable == "yes" or pairable == "yes":
        return CheckResult(
            "Bluetooth pairing policy",
            "warn",
            f"agent OK, pairing window open (Discoverable={discoverable}, Pairable={pairable})",
        )
    return CheckResult(
        "Bluetooth pairing policy",
        "ok",
        "JTS no-code agent active; pairing window closed",
    )

@doctor_check(order=15, group="renderers", label="Spotify auth", needs_cfg=True)
def check_spotify_cache(cfg: Config) -> CheckResult:
    """Verify Spotify is authenticated. Prefers the multi-account
    registry (per-household-member accounts, the modern path) over the
    legacy single-account cache. Reports OK if either has a usable
    refresh token. The earlier "cache missing" warning was a false
    positive on installs using only the multi-account setup."""
    if not cfg.spotify_enabled:
        return CheckResult("Spotify auth", "ok", "not configured (skipped)")
    # Modern path: per-account registry at spotify_accounts_path.
    try:
        from ...accounts import Registry
        registry = Registry.load(cfg.spotify_accounts_path)
    except Exception:  # noqa: BLE001
        registry = None
    if registry is not None and registry.accounts:
        authed = []
        for acct in registry.accounts:
            try:
                if Path(acct.cache_path).exists():
                    authed.append(acct.name)
            except (OSError, AttributeError):
                pass
        if authed:
            return CheckResult(
                "Spotify auth", "ok",
                f"{len(authed)} account(s) cached: {', '.join(authed)}",
            )
        return CheckResult(
            "Spotify auth", "warn",
            f"{len(registry.accounts)} account(s) registered but no token "
            f"caches found under {Path(cfg.spotify_accounts_path).parent}/"
            f"caches/. Visit {cfg.spotify_setup_url} to re-link.",
        )
    # Fall back to legacy single-account cache for installs that
    # haven't migrated to the multi-account registry.
    p = Path(cfg.spotify_cache_path)
    if not p.exists():
        return CheckResult(
            "Spotify auth", "warn",
            f"no accounts registered ({cfg.spotify_accounts_path}) and "
            f"no legacy cache at {p}. Visit {cfg.spotify_setup_url} to "
            f"link an account.",
        )
    return CheckResult("Spotify auth", "ok", f"legacy cache at {p}")

@doctor_check(order=16, group="renderers", label="Spotify Connect device", needs_cfg=True)
def check_spotify_connect_device(cfg: Config) -> CheckResult:
    """Verify the on-Pi librespot endpoint is visible to at least one
    configured Spotify account, with a broadcast name matching the
    /speaker/ display name (substring match).

    This is the cold-start playback path: when no AirPlay is active,
    `spotify_play` falls through to `resolve_target` → librespot.
    `_find_librespot_id` does a case-insensitive substring match of
    the configured pattern against `sp.devices()[].name`. If the
    pattern doesn't match what librespot is broadcasting, every
    cold-start `play X` returns 'no spotify target device available'
    — a silent severe failure this check catches."""
    label = "Spotify Connect device"
    if not cfg.spotify_enabled:
        return CheckResult(label, "ok", "not configured (skipped)")

    pattern = cfg.spotify_device_name.strip().lower()
    if not pattern:
        return CheckResult(
            label, "fail",
            "speaker name is empty. Visit http://jts.local/speaker/ "
            "and set a display name (default 'JTS').",
        )

    # Build clients and probe each account's sp.devices() for a match.
    try:
        from ...accounts import Registry
        from ...spotify_router import build_clients
        accounts = Registry.load(cfg.spotify_accounts_path)
        result = build_clients(
            accounts,
            client_id=cfg.spotify_client_id,
            redirect_uri=cfg.spotify_redirect_uri,
        )
        clients = result.clients
    except Exception as e:  # noqa: BLE001
        return CheckResult(
            label, "warn",
            f"could not build Spotify clients: {e}. "
            f"This usually means no accounts have OAuth tokens — visit "
            f"{cfg.spotify_setup_url} to link an account.",
        )
    if not clients:
        return CheckResult(
            label, "warn",
            f"no accounts have OAuth tokens (visit {cfg.spotify_setup_url}). "
            f"Once linked, this check will verify librespot visibility.",
        )

    matched_accounts: list[str] = []
    missed_accounts: list[str] = []
    seen_names_overall: set[str] = set()
    for account_name, ac in clients.items():
        try:
            devices = ac.sp.devices()
        except Exception as e:  # noqa: BLE001
            missed_accounts.append(f"{account_name} (devices fetch failed: {e})")
            continue
        names = [(d.get("name") or "") for d in devices.get("devices", [])]
        seen_names_overall.update(names)
        if any(pattern in n.lower() for n in names):
            matched_accounts.append(account_name)
        else:
            missed_accounts.append(account_name)

    if matched_accounts and not missed_accounts:
        return CheckResult(
            label, "ok",
            f"{cfg.spotify_device_name!r} visible to all "
            f"{len(matched_accounts)} account(s): {', '.join(matched_accounts)}",
        )
    if matched_accounts and missed_accounts:
        return CheckResult(
            label, "warn",
            f"{cfg.spotify_device_name!r} visible to {matched_accounts} "
            f"but NOT {missed_accounts}. Cold-start `play X` will work "
            f"only for the matched account(s). Try opening Spotify on the "
            f"missing account and casting to the device once to register it.",
        )
    return CheckResult(
        label, "fail",
        f"no account sees a device matching "
        f"{cfg.spotify_device_name!r}. Devices currently visible to the "
        f"linked accounts: {sorted(seen_names_overall)}. "
        f"Fix: open Spotify on a phone/desktop logged into the linked "
        f"account, click the cast/devices icon, select the speaker "
        f"once to make it discoverable; or verify librespot is running "
        f"(`systemctl status librespot`) and broadcasting "
        f"(`avahi-browse -tr _spotify-connect._tcp`).",
    )

@doctor_check(order=72, group="renderers")
def check_shairport_sync_loopback_plughw() -> CheckResult:
    """Verify the deployed shairport-sync.conf uses a multi-writer-safe
    renderer device.

    Canonical: `shairport_substream` — AirPlay's private fan-in lane.
    jasper-fanin reads the capture side and publishes the summed music
    stream to CamillaDSP/AEC. A stale `jasper_renderer_in` value means
    shairport is still pointed at the retired renderer-side dmix path.

    Legacy `plughw:Loopback,0,0` and raw `hw:Loopback,0,0` are both
    stale now. The raw form is additionally broken because it bypasses
    ALSA's plug layer.

    Check runs against the DEPLOYED file (not the repo) so it catches
    both kinds of drift: branch not yet merged, and manual on-Pi edits."""
    label = "shairport-sync.conf: output_device"
    p = Path("/etc/shairport-sync.conf")
    if not p.exists():
        return CheckResult(
            label, "warn",
            f"{p} missing — shairport-sync may not be installed.",
        )
    try:
        text = p.read_text()
    except OSError as e:
        return CheckResult(label, "warn", f"can't read {p}: {e}")
    # Look for an active (non-comment) output_device line. Comments in
    # shairport-sync.conf use //; libconfig syntax. We tolerate the
    # value being quoted or unquoted, single or double quotes.
    active_lines = [
        ln.strip() for ln in text.splitlines()
        if ln.strip().startswith("output_device")
    ]
    if not active_lines:
        return CheckResult(
            label, "warn",
            "no `output_device` directive found in alsa block; relying "
            "on shairport-sync's default (probably wrong).",
        )
    line = active_lines[0]
    if "shairport_substream" in line:
        return CheckResult(
            label, "ok",
            "shairport_substream (fan-in private AirPlay lane)",
        )
    if "jasper_renderer_in" in line:
        return CheckResult(
            label, "fail",
            "jasper_renderer_in — stale retired dmix path. Re-run "
            "deploy/install.sh so shairport renders to shairport_substream.",
        )
    if 'plughw:Loopback' in line:
        return CheckResult(
            label, "warn",
            "plughw:Loopback,0,0 — stale pre-fan-in wiring. Redeploy "
            "to render shairport_substream, AirPlay's private fan-in lane.",
        )
    if '"hw:Loopback' in line or "'hw:Loopback" in line:
        return CheckResult(
            label, "fail",
            "output_device uses raw `hw:Loopback,0,0` — AirPlay sessions "
            "will be silently rejected because Loopback is locked at "
            "48 kHz and shairport requests 44.1 kHz. Symptom: iPhone / "
            "Mac sees the speaker in the picker but can't establish a session. "
            "Fix: redeploy via `bash scripts/deploy-to-pi.sh`. Source "
            "of truth: deploy/shairport-sync.conf.template.",
        )
    return CheckResult(
        label, "warn",
        f"output_device value not recognized: {line!r}",
    )

# Renderer registry: (label_suffix, runtime_user, parse_function).
# parse_function returns the configured device name, or None if not
# discoverable. Centralising the registry here keeps the probe loop
# below uniform across renderers; adding a fourth renderer is one
# entry.
def _read_first_line_matching(path: Path, predicate) -> Optional[str]:
    """Scan a config file for the first line where `predicate(line)`
    returns truthy. Returns the line stripped, or None."""
    try:
        for ln in path.read_text().splitlines():
            if predicate(ln):
                return ln.strip()
    except OSError:
        return None
    return None

def _renderer_device_shairport() -> Optional[str]:
    """shairport-sync: parse /etc/shairport-sync.conf for output_device.
    Format: `output_device = "shairport_substream";` (libconfig syntax)."""
    ln = _read_first_line_matching(
        Path("/etc/shairport-sync.conf"),
        lambda line: (
            line.lstrip().startswith("output_device")
            and "=" in line
            and not line.lstrip().startswith("//")
        ),
    )
    if not ln:
        return None
    # output_device = "DEVNAME"; — pull the quoted string.
    m = re.search(r'"([^"]+)"', ln) or re.search(r"'([^']+)'", ln)
    return m.group(1) if m else None

def _renderer_device_librespot() -> Optional[str]:
    """librespot: parse the ExecStart= line(s) in librespot.service for
    --device. systemd allows ExecStart to span multiple lines via
    backslash continuation."""
    p = Path("/etc/systemd/system/librespot.service")
    try:
        text = p.read_text()
    except OSError:
        return None
    # Collapse line continuations so we can scan the full ExecStart.
    flat = text.replace("\\\n", " ")
    for ln in flat.splitlines():
        s = ln.strip()
        if not s.startswith("ExecStart=") or "--device" not in s:
            continue
        # --device <DEVNAME>  (may be quoted)
        m = re.search(r"--device\s+(?:'([^']+)'|\"([^\"]+)\"|(\S+))", s)
        if m:
            return m.group(1) or m.group(2) or m.group(3)
    return None

def _renderer_device_bluealsa() -> Optional[str]:
    """bluealsa-aplay: parse the drop-in ExecStart= for --pcm=DEVNAME."""
    # The drop-in is mode-0644 readable; doctor runs as root anyway.
    for path in (
        Path("/etc/systemd/system/bluealsa-aplay.service.d/jts-output.conf"),
        Path("/etc/systemd/system/bluealsa-aplay.service.d/override.conf"),
    ):
        try:
            text = path.read_text()
        except OSError:
            continue
        for ln in text.splitlines():
            s = ln.strip()
            if s.startswith("ExecStart=") and "--pcm=" in s:
                m = re.search(r"--pcm=(\S+)", s)
                if m:
                    return m.group(1)
    return None

def _systemd_user_for(unit: str) -> Optional[str]:
    """Return the User= field of a systemd unit, or None if missing /
    empty (systemd default = root in that case, which the caller
    handles)."""
    try:
        r = subprocess.run(
            ["systemctl", "show", unit, "-p", "User", "--value"],
            capture_output=True, text=True, timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    u = r.stdout.strip()
    return u or None

def _resolve_systemd_env_vars(device: str, unit: str) -> str:
    """Expand `${VAR}` references in a device string using the
    systemd unit's resolved environment.

    Most renderer service files now use literal fan-in lane names, but
    this helper remains useful for operator overrides that use systemd
    environment variables. systemd expands those references at daemon
    start time; when the doctor reads the unit file directly it sees
    the literal `${VAR}` string. Passing that to aplay would fail with
    "Unknown PCM ${VAR}" — a false positive.

    We ask systemd for the unit's resolved environment
    (`systemctl show -p Environment`), which already accounts for
    both `Environment=` directives and `EnvironmentFile=` lookups
    (with the leading-`-` "optional file" semantics). Whatever
    value systemd would substitute at ExecStart time is what we
    pass to aplay.

    Returns the original string unchanged if it contains no
    `${VAR}` references or if resolution fails (best-effort — the
    caller's aplay probe will then fail loudly with a clear
    error, which is the right behavior).
    """
    if "${" not in device:
        return device
    try:
        r = subprocess.run(
            ["systemctl", "show", unit, "-p", "Environment", "--value"],
            capture_output=True, text=True, timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return device
    if r.returncode != 0:
        return device
    # systemd's `Environment` output is a single line of
    # space-separated KEY=VALUE pairs (after merging Environment=
    # directives and any EnvironmentFile= files). Values that
    # contain spaces are quoted, but ALSA PCM names never do, so
    # naive splitting is safe for our use case.
    env_map: dict[str, str] = {}
    for token in r.stdout.split():
        if "=" in token:
            key, _, value = token.partition("=")
            # Strip surrounding quotes systemd may add.
            env_map[key] = value.strip().strip('"').strip("'")

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        return env_map.get(name, match.group(0))

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", _sub, device)

def _probe_open_as_user(device: str, user: Optional[str]) -> tuple[bool, str]:
    """Attempt to open `device` for ~0.1 s of silence playback AS `user`.
    Returns (success, detail). success=True means snd_pcm_open and a
    short write both succeeded; detail is the underlying aplay stderr
    for diagnostics (best-effort short).

    Why aplay + /dev/zero: it exercises the same code path the renderer
    uses (alsalib's snd_pcm_open through the user-space plugin chain)
    while writing only silence — sample-wise additive into any mix,
    so safe to run while music is playing.
    """
    cmd = [
        "timeout", "0.3",
        "aplay", "-q",
        "-D", device,
        "-c", "2", "-r", "48000", "-f", "S16_LE",
        "/dev/zero",
    ]
    if user:
        cmd = ["sudo", "-n", "-u", user, *cmd]
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=3,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return False, f"probe subprocess failed: {e}"
    # Exit code 124 = timeout fired = aplay was happily writing
    # silence for the full 0.3 s, which means open + write succeeded.
    # Exit code 0 = aplay exited cleanly before timeout (rare; means
    # /dev/zero was fully consumed, which won't happen at 0.3 s but
    # still success).
    # Any other code = failure; stderr should explain.
    stderr_tail = (r.stderr or "").strip().splitlines()[-2:]
    detail = " | ".join(stderr_tail)[:200]
    if r.returncode in (0, 124):
        return True, detail
    return False, detail or f"exit={r.returncode}"

_FANIN_PRIVATE_RENDERER_DEVICES = {
    "librespot_substream": 0,
    "shairport_substream": 1,
    "bluealsa_substream": 2,
    "usbsink_substream": 3,
}

def _alsa_busy(detail: str) -> bool:
    return (
        "Device or resource busy" in detail
        or "EBUSY" in detail
        or "errno 16" in detail
    )

def _fanin_lane_busy_owner_matches(device: str, unit: str) -> tuple[bool, str]:
    """Return whether an EBUSY private fan-in lane is owned by `unit`.

    An EBUSY aplay probe proves the PCM name resolved, but it does not
    prove the expected renderer owns the lane. The snd-aloop proc status
    exposes `owner_pid`; systemd cgroups expose the owning unit. Combine
    both so a stale test process cannot make doctor green.
    """
    substream = _FANIN_PRIVATE_RENDERER_DEVICES.get(device)
    if substream is None:
        return False, "not a known fan-in private lane"
    status_path = Path(f"/proc/asound/Loopback/pcm0p/sub{substream}/status")
    try:
        text = status_path.read_text()
    except OSError as e:
        return False, f"could not read {status_path}: {e}"
    m = re.search(r"owner_pid\s*:\s*(\d+)", text)
    if not m:
        return False, f"{status_path} has no owner_pid"
    pid = m.group(1)
    cgroup_path = Path(f"/proc/{pid}/cgroup")
    try:
        cgroup = cgroup_path.read_text()
    except OSError as e:
        return False, f"could not read {cgroup_path}: {e}"
    if f"/{unit}" in cgroup:
        return True, f"busy/owned pid={pid}"
    return False, f"busy but owner pid={pid} cgroup={cgroup.strip()!r}"

@doctor_check(order=73, group="renderers")
def check_renderer_device_resolvable() -> CheckResult:
    """Verify each music renderer can actually open the ALSA device
    it's configured to write to, AS its runtime systemd User=.

    The original bug this catches (PR #223, 2026-05-23): renderer users
    could not read the asoundrc that defined the named ALSA PCMs, so
    snd_pcm_open() returned "Unknown PCM" despite config strings looking
    right. A real open attempt catches that class.

    Fan-in caveat: renderer lanes are intentionally private
    single-writer substreams. If the renderer is already active, a
    second `aplay -D shairport_substream` probe can return EBUSY. We
    accept that only when /proc/asound's owner_pid belongs to the
    expected systemd unit.

    Method: for each known renderer:
      1. Look up its systemd User=.
      2. Parse its config to find the configured ALSA device.
      3. `sudo -u <user> aplay -D <device> /dev/zero` for a short
         duration. Success = device opens and a write goes through.

    Probe is safe to run anytime. It writes only silence. On idle
    fan-in lanes, the open succeeds; on active fan-in lanes, EBUSY is
    accepted as "owned by the renderer."

    Returns:
      ok    — all configured renderers can open their device as their user
      fail  — any renderer can't open its device (this is the bug class)
      warn  — partial info: some renderer's device or user wasn't
              discoverable (likely the renderer isn't installed; treat
              as informational)
    """
    label = "renderer ALSA device resolvable"
    renderers = [
        ("shairport-sync", "shairport-sync.service",
         _renderer_device_shairport),
        ("librespot",      "librespot.service",
         _renderer_device_librespot),
        ("bluealsa-aplay", "bluealsa-aplay.service",
         _renderer_device_bluealsa),
    ]
    failures: list[str] = []
    incomplete: list[str] = []
    successes: list[str] = []
    for name, unit, parse_dev in renderers:
        device = parse_dev()
        if device is None:
            incomplete.append(f"{name}: config not found (not installed?)")
            continue
        # If the parsed device contains a ${VAR} reference, ask systemd
        # what value it would substitute at ExecStart time. Otherwise
        # the aplay probe below will fail with "Unknown PCM ${VAR}" —
        # a false positive, since the running daemon has resolved it.
        resolved_device = _resolve_systemd_env_vars(device, unit)
        user = _systemd_user_for(unit)
        ok, detail = _probe_open_as_user(resolved_device, user)
        who = user or "root"
        # Show both the literal-parsed and resolved values when they
        # differ, so the operator can spot a misconfigured env file
        # without re-reading the unit themselves.
        display = (
            f"{resolved_device}"
            if resolved_device == device
            else f"{resolved_device} (from {device})"
        )
        if ok:
            successes.append(f"{name}({who})→{display}")
        elif (
            resolved_device in _FANIN_PRIVATE_RENDERER_DEVICES
            and _alsa_busy(detail)
        ):
            owned, owner_detail = _fanin_lane_busy_owner_matches(
                resolved_device, unit,
            )
            if owned:
                successes.append(f"{name}({who})→{display} {owner_detail}")
            else:
                failures.append(f"{name}({who})→{display}: {owner_detail}")
        else:
            failures.append(f"{name}({who})→{display}: {detail}")
    if failures:
        return CheckResult(
            label, "fail",
            "; ".join(failures) + ". This is the bug class PR #223 "
            "addressed — verify /etc/asound.conf exists and is mode "
            "0644 so non-root renderer users can resolve user-space "
            "ALSA PCM names. EBUSY is expected only for active fan-in "
            "private lanes; Unknown PCM is always a real failure.",
        )
    if not successes:
        # All renderers were unknown — probably a stripped image.
        return CheckResult(
            label, "warn",
            "; ".join(incomplete) if incomplete
            else "no renderers configured",
        )
    detail = "; ".join(successes)
    if incomplete:
        detail += " (skipped: " + "; ".join(incomplete) + ")"
    return CheckResult(label, "ok", detail)


def _classify_mux_mode(path: Path) -> CheckResult:
    """Classify the persisted jasper-mux source-selection mode at `path`.

    Split from the check so tests can point it at a tmp file. Granular
    on purpose — the runtime reader (`mux_mode_persistence.
    read_manual_source`) deliberately collapses missing/corrupt/unknown
    to None (fail-open to auto), which is right for the daemon but means
    a household's lost pin is silent. The doctor line tells the operator
    WHICH state the file is in."""
    name = "mux mode state"
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        # Normal: auto mode never persisted a pin, or fresh install.
        return CheckResult(name, "ok", "auto (no source pin persisted)")
    except OSError as e:
        return CheckResult(
            name, "warn",
            f"unreadable ({e.__class__.__name__}) — mux falls back to "
            f"auto. Check permissions on {path}",
        )
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return CheckResult(
            name, "warn",
            f"corrupt — mux falls back to auto (a manual source pin, if "
            f"one was set, is lost). Delete to clear: {path}",
        )
    if not isinstance(data, dict) or data.get("mode") != "manual":
        return CheckResult(name, "ok", "auto (latest-source-wins)")
    label = data.get("selected_source")
    try:
        source = Source(label)
    except (TypeError, ValueError):
        source = None
    if source is None or source not in MUSIC_SOURCES:
        return CheckResult(
            name, "warn",
            f"manual pin to unknown source {label!r} — ignored, mux runs "
            f"auto. Re-pin via the landing page or delete {path}",
        )
    return CheckResult(name, "ok", f"manual pin: {source.value}")


@doctor_check(order=76, group="renderers")
def check_mux_mode_state() -> CheckResult:
    """Surface the persisted source-selection mode (auto vs manual pin).

    A corrupt file or a pin to a source that no longer exists is
    fail-open at runtime (mux silently runs auto), so this line is the
    only place an operator learns the household's pin was dropped."""
    path = Path(
        os.environ.get("JASPER_MUX_MODE_STATE_PATH", _MUX_MODE_DEFAULT_PATH),
    )
    return _classify_mux_mode(path)
