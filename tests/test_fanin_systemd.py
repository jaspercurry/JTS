"""Lock down the jasper-fanin.service systemd unit shape.

The unit's resilience-contract fields are load-bearing — they're
the JTS-standard Tier 1+2 / Stage 1+2 protections documented in
docs/HANDOFF-resilience.md and the fan-in-specific design in
docs/HANDOFF-fan-in-daemon.md.

A future config edit that drops `WatchdogSec=`, lowers
`OOMScoreAdjust=` priority, or removes the `Slice=` assignment
would silently regress these protections. These tests catch that.
"""
from __future__ import annotations

import re
from pathlib import Path

from tests.install_surface import installer_text


REPO = Path(__file__).resolve().parents[1]
UNIT_PATH = REPO / "deploy" / "systemd" / "jasper-fanin.service"


def _read_unit() -> str:
    return UNIT_PATH.read_text()


def _value_for(unit_text: str, key: str) -> str | None:
    """Pull the value of a `Key=Value` directive. Returns None if
    absent. Matches the systemd convention: case-sensitive key, no
    whitespace around `=`, value is everything to end-of-line."""
    for line in unit_text.splitlines():
        # Skip section headers and comments.
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("["):
            continue
        if "=" not in stripped:
            continue
        k, _, v = stripped.partition("=")
        if k.strip() == key:
            return v.strip()
    return None


def test_unit_file_exists():
    assert UNIT_PATH.exists(), (
        f"jasper-fanin.service missing at {UNIT_PATH}. "
        f"install.sh's install_systemd_units block needs this file."
    )


def test_type_notify_for_sd_notify_contract():
    """`Type=notify` is required for the sd_notify-based heartbeat.
    With Type=simple systemd doesn't wait for READY=1 and the
    WATCHDOG=1 pings have no effect — silently disabling Tier 2."""
    unit = _read_unit()
    assert _value_for(unit, "Type") == "notify", (
        "jasper-fanin.service must declare Type=notify so systemd "
        "honors the sd_notify watchdog contract. See "
        "docs/HANDOFF-fan-in-daemon.md."
    )


def test_watchdog_sec_set():
    """`WatchdogSec=30s` matches the project-wide Tier 2 cadence
    (jasper-camilla, jasper-aec-bridge, jasper-voice, jasper-control
    all use this value)."""
    unit = _read_unit()
    val = _value_for(unit, "WatchdogSec")
    assert val == "30s", (
        f"jasper-fanin.service must declare WatchdogSec=30s "
        f"(matching the project-wide Tier 2 cadence); got {val!r}"
    )


def test_timeout_stop_sec_short():
    """`TimeoutStopSec=5s` is load-bearing — the 2026-05-11 snd-aloop
    wedge taught us that a daemon with blocked I/O sits on SIGTERM
    for systemd's default 90s and corrupts kernel ALSA state by the
    time SIGKILL fires. 5s escalates fast."""
    unit = _read_unit()
    val = _value_for(unit, "TimeoutStopSec")
    assert val == "5s", (
        f"jasper-fanin.service must declare TimeoutStopSec=5s "
        f"to escalate to SIGKILL fast on a wedged daemon. "
        f"docs/HANDOFF-resilience.md Tier 1+2 section. Got {val!r}"
    )


def test_restart_on_failure():
    """`Restart=on-failure` covers exit nonzero + signal termination
    + watchdog timeout. `Restart=always` is too aggressive (would
    restart on clean signal-shutdown); `Restart=on-watchdog` misses
    the alsa_open-failed case."""
    unit = _read_unit()
    val = _value_for(unit, "Restart")
    assert val == "on-failure", (
        f"jasper-fanin.service must declare Restart=on-failure "
        f"so wedge + crash + non-zero-exit all trigger restart. "
        f"Got {val!r}"
    )


def test_start_limit_action_reboot():
    """T5.1 escalation: if the daemon hits StartLimitBurst within
    StartLimitIntervalSec, systemd cleanly reboots. Critical for
    audio-path daemons — if jasper-fanin is wedging repeatedly,
    something structural is wrong; a clean reboot beats a flapping
    restart loop. See docs/HANDOFF-tier5-watchdog-liveness.md."""
    unit = _read_unit()
    val = _value_for(unit, "StartLimitAction")
    assert val == "reboot", (
        f"jasper-fanin.service must declare StartLimitAction=reboot "
        f"(T5.1 escalation). Not 'reboot-force' — clean reboot is "
        f"important on 1 GB Pi with zram active (dirty pages must "
        f"sync). Got {val!r}"
    )


def test_oom_score_adj_between_camilla_and_aec_bridge():
    """OOM ladder slot. -800 sits between jasper-camilla (-900,
    silence-critical) and jasper-aec-bridge (-700, capture-critical).
    fan-in is the upstream source of the music signal both
    consume; killing it preferentially over Camilla makes sense.
    See docs/HANDOFF-resilience.md "OOM ladder" section."""
    unit = _read_unit()
    val = _value_for(unit, "OOMScoreAdjust")
    assert val == "-800", (
        f"jasper-fanin.service OOMScoreAdjust must be -800 "
        f"(between Camilla's -900 and AEC bridge's -700). "
        f"Got {val!r}"
    )


def test_slice_assignment():
    """`Slice=jts-audio.slice` puts the daemon in the Stage 2
    audio-protection cgroup with MemorySwapMax=0. Audio jitter
    from zram decompression latency is the dominant risk on a
    1 GB Pi 5; this membership shields the work loop's pages."""
    unit = _read_unit()
    val = _value_for(unit, "Slice")
    assert val == "jts-audio.slice", (
        f"jasper-fanin.service must declare Slice=jts-audio.slice "
        f"(Stage 2 audio-protection cgroup). Got {val!r}"
    )


def test_sched_fifo_and_mlockall_settings():
    """Real-time scheduling: SCHED_FIFO at priority 30 +
    LimitMEMLOCK=infinity (the latter lets in-process mlockall
    succeed even when systemd's per-unit default RLIMIT_MEMLOCK
    is too small for the daemon's stacks + audio buffers).
    OSPERT 2024 measured Pi 5 stock-kernel worst-case scheduling
    latency at 36.8ms under stress; SCHED_FIFO + mlockall is the
    floor protection before considering PREEMPT_RT."""
    unit = _read_unit()
    assert _value_for(unit, "CPUSchedulingPolicy") == "fifo"
    assert _value_for(unit, "CPUSchedulingPriority") == "30"
    assert _value_for(unit, "LimitMEMLOCK") == "infinity"


def test_runtime_directory():
    """`RuntimeDirectory=jasper-fanin` makes systemd create
    /run/jasper-fanin/ on start and remove it on stop. The UDS
    socket lives in that dir; without this, the socket would leak
    across daemon-restart events and the bind would race against
    stale-socket cleanup."""
    unit = _read_unit()
    val = _value_for(unit, "RuntimeDirectory")
    assert val == "jasper-fanin", (
        f"jasper-fanin.service must declare RuntimeDirectory=jasper-fanin "
        f"so /run/jasper-fanin/ is auto-managed. Got {val!r}"
    )


def test_environment_files():
    """Config-file chain matches the voice / AEC daemons:
    /etc/jasper/jasper.env (system-wide, required) then an
    optional wizard-owned file (`-` prefix = optional). Same
    pattern lets operators override defaults via either."""
    unit = _read_unit()
    env_files = [
        line.strip().split("=", 1)[1]
        for line in unit.splitlines()
        if line.strip().startswith("EnvironmentFile=")
    ]
    assert "/etc/jasper/jasper.env" in env_files, (
        "jasper-fanin.service must source /etc/jasper/jasper.env"
    )
    # Optional wizard file with the `-` prefix (= no error if missing).
    assert any(
        ef.startswith("-") and "fanin.env" in ef for ef in env_files
    ), "jasper-fanin.service must reference an optional fanin.env wizard file"


def test_exec_start_points_at_installed_binary():
    """`ExecStart=/opt/jasper/bin/jasper-fanin` matches where
    install.sh's build_install_jasper_fanin installs the release
    binary. A divergence between unit and install.sh would let
    systemd start a stale binary or fail with ENOENT."""
    unit = _read_unit()
    val = _value_for(unit, "ExecStart")
    assert val == "/opt/jasper/bin/jasper-fanin", (
        f"jasper-fanin.service ExecStart must be "
        f"/opt/jasper/bin/jasper-fanin (matches install.sh's "
        f"build_install_jasper_fanin destination). Got {val!r}"
    )


def test_input_buffer_frames_sized_for_wifi_burst_absorption():
    """Per-input ALSA ring buffer must be >= 4096 frames so it
    absorbs the worst-case 802.11 A-MPDU inter-burst gap (~40 ms
    observed; we want comfortable headroom, hence 4096 = ~85 ms).

    Below 4096, AirPlay sessions produce ~1 input EPIPE-overrun per
    30-60 s on real hardware, each injecting one period of silence
    into the mixer output. See docs/HANDOFF-fan-in-daemon.md
    "Configuration → Buffer sizing" for the measurement story and
    docs/HANDOFF-airplay.md Pattern A3 for what it fixes.

    The dmix layer (PR #214, which fanin replaces) had buffer_size
    4096; fanin must match that to preserve the burst-absorption
    behaviour the dmix accidentally provided.
    """
    unit = _read_unit()
    # Look in the [Service] section for the Environment= directive
    # — it's the production default, even though operators can
    # override via /var/lib/jasper/fanin.env.
    match = re.search(
        r'^\s*Environment\s*=\s*"?JASPER_FANIN_INPUT_BUFFER_FRAMES=(\d+)"?',
        unit,
        re.MULTILINE,
    )
    assert match is not None, (
        "jasper-fanin.service must set Environment=\"JASPER_FANIN_INPUT_BUFFER_FRAMES=...\" "
        "(production default for per-input ALSA buffer sizing). "
        "See docs/HANDOFF-fan-in-daemon.md 'Buffer sizing'."
    )
    val = int(match.group(1))
    assert val >= 4096, (
        f"JASPER_FANIN_INPUT_BUFFER_FRAMES={val} is below 4096 (~85 ms). "
        f"Below 4096, WiFi A-MPDU burst delivery overruns the input "
        f"ring at ~2 xruns/min on AirPlay. See HANDOFF-airplay.md "
        f"Pattern A3 + HANDOFF-fan-in-daemon.md 'Buffer sizing'."
    )


def test_output_buffer_frames_stays_latency_bounded():
    """Output ALSA ring should not inherit the large input burst
    absorber. 3072 frames (~64 ms at 48 kHz) gives CamillaDSP enough
    read margin without turning the fanin→CamillaDSP/AEC leg into a
    large hidden queue; WiFi burst absorption belongs on input lanes."""
    unit = _read_unit()
    match = re.search(
        r'^\s*Environment\s*=\s*"?JASPER_FANIN_OUTPUT_BUFFER_FRAMES=(\d+)"?',
        unit,
        re.MULTILINE,
    )
    assert match is not None, (
        "jasper-fanin.service must set JASPER_FANIN_OUTPUT_BUFFER_FRAMES "
        "separately from the input burst absorber"
    )
    val = int(match.group(1))
    assert val == 3072


def test_hardening_directives_present():
    """Defense-in-depth filesystem hardening — matches the
    conventions of other jasper-* units. None of these are
    individually load-bearing, but together they constrain the
    blast radius of any compromise of the daemon."""
    unit = _read_unit()
    assert _value_for(unit, "NoNewPrivileges") == "true"
    assert _value_for(unit, "ProtectSystem") == "full"
    assert _value_for(unit, "ProtectHome") == "read-only"
    assert _value_for(unit, "PrivateTmp") == "true"


def test_read_write_paths_include_jasper_state_dirs():
    """ReadWritePaths grants write access to the paths the daemon
    needs even with ProtectSystem=full. /var/lib/jasper for the
    xrun_log ring, /run/jasper-fanin for the UDS socket."""
    unit = _read_unit()
    rwp_lines = [
        line.strip().split("=", 1)[1]
        for line in unit.splitlines()
        if line.strip().startswith("ReadWritePaths=")
    ]
    assert rwp_lines, "jasper-fanin.service must declare ReadWritePaths"
    rwp_combined = " ".join(rwp_lines)
    assert "/var/lib/jasper" in rwp_combined, (
        "ReadWritePaths must include /var/lib/jasper "
        "(for xrun_history.jsonl writes)"
    )
    assert "/run/jasper-fanin" in rwp_combined, (
        "ReadWritePaths must include /run/jasper-fanin "
        "(for the UDS socket; redundant with RuntimeDirectory "
        "but explicit for ProtectSystem=full)"
    )


def test_install_target_is_multi_user():
    """`WantedBy=multi-user.target` matches the conventions of
    other jasper-* daemons."""
    unit = _read_unit()
    val = _value_for(unit, "WantedBy")
    assert val == "multi-user.target"


def test_fanin_starts_before_hot_path_consumers():
    """Fan-in must be initialized before Camilla/renderer consumers try
    to open the summed-reference graph."""
    unit = _read_unit()
    before = _value_for(unit, "Before")
    assert before is not None
    for dep in (
        "jasper-camilla.service",
        "shairport-sync.service",
        "librespot.service",
        "bluealsa-aplay.service",
        "jasper-usbsink.service",
        "jasper-aec-bridge.service",
    ):
        assert dep in before


def test_install_sh_enables_fanin_and_retires_topology_switch():
    """Fan-in is mandatory now: install.sh enables the daemon directly
    and removes the retired dmix/fanin switch state."""
    install_sh = installer_text()
    env_migrations_lib = (
        REPO / "deploy" / "lib" / "install" / "env-migrations.sh"
    ).read_text()
    assert "retire_audio_topology_switch()" in env_migrations_lib, (
        "the installer's env-migrations lib must define the "
        "retire_audio_topology_switch helper"
    )
    call_site = re.search(
        r"^\s*retire_audio_topology_switch(?:\s|$|\s*#)",
        install_sh,
        re.MULTILINE,
    )
    assert call_site is not None, (
        "main() must call retire_audio_topology_switch so stale "
        "/var/lib/jasper/audio_topology.env cannot keep misleading "
        "operators after fan-in became canonical."
    )
    assert "systemctl enable jasper-camilla.service jasper-fanin.service" in install_sh, (
        "install.sh must enable jasper-fanin.service directly; renderer "
        "audio depends on it."
    )
    renderers_lib = (
        REPO / "deploy" / "lib" / "install" / "renderers.sh"
    ).read_text()
    assert "rm -f /usr/local/sbin/jasper-audio-topology" in renderers_lib
    assert "/usr/local/sbin/jasper-audio-topology fanin" not in install_sh
    assert "/usr/local/sbin/jasper-audio-topology fanin" not in renderers_lib


def test_install_sh_restarts_camilla_after_fanin():
    """Camilla captures fan-in's summed output; deploy must not leave it
    holding a stale capture fd after asound/fan-in updates."""
    install_sh = installer_text()
    assert re.search(
        r"systemctl restart jasper-fanin\.service.*?"
        r"systemctl try-restart jasper-camilla\.service",
        install_sh,
        re.DOTALL,
    ), "install.sh must try-restart jasper-camilla after jasper-fanin"


def test_install_sh_builds_and_installs_binary():
    """install.sh must build the Rust crate and install the
    release binary to /opt/jasper/bin/jasper-fanin. Without these
    lines, the unit's ExecStart fails with ENOENT."""
    install_sh = installer_text()
    assert "build_install_jasper_fanin" in install_sh, (
        "install.sh must define + call build_install_jasper_fanin "
        "(builds rust/jasper-fanin and installs the binary). "
        "See deploy/install.sh main()."
    )
    assert "cargo build --release --locked" in install_sh, (
        "install.sh's build step must run `cargo build --release --locked` "
        "so Cargo.lock drift fails deploy instead of resolving live"
    )
    assert "/opt/jasper/bin/jasper-fanin" in install_sh, (
        "install.sh must install the binary to "
        "/opt/jasper/bin/jasper-fanin (matches the unit's ExecStart)"
    )
    assert "rustc cargo" in install_sh, (
        "install.sh's install_deps must apt-install rustc + cargo"
    )
