# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The outputd topology's wiring: declarative pins plus the steps that run."""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

from jasper.audio_hardware import dac
from jasper.tts_routing import (
    DUCK_TRANSPORT_ENV,
    FANIN_TTS_SOCKET,
    OUTPUTD_TTS_SOCKET,
    OUTPUTD_TTS_SOCKET_ENV,
    VOICE_TTS_SOCKET_ENV,
)
from tests.install_surface import installer_shell_paths, installer_text
from tests.reconcile_fixtures import fake_systemctl
from tests.test_audio_hardware_reconcile import _dual_apple_cards


REPO = Path(__file__).resolve().parents[1]


def _non_comment(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines()
        if not line.lstrip().startswith("#")
    )


def _env_file_text_to_map(text: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def _resolve_systemd_unit_env(
    unit_text: str,
    env_files: dict[str, str],
) -> dict[str, str]:
    """Resolve the unit's Environment* directives in declaration order."""
    env: dict[str, str] = {}
    for raw in unit_text.splitlines():
        line = raw.strip()
        if line.startswith("EnvironmentFile="):
            path = line.partition("=")[2].strip().strip('"').strip("'")
            if path.startswith("-"):
                path = path[1:]
            if path in env_files:
                env.update(_env_file_text_to_map(env_files[path]))
            continue
        if line.startswith("Environment="):
            payload = line.partition("=")[2].strip()
            for assignment in shlex.split(payload):
                if "=" not in assignment:
                    continue
                key, _, value = assignment.partition("=")
                env[key] = value
    return env


def test_asoundrc_no_longer_declares_any_camilla_to_outputd_lane():
    """Both Camilla -> outputd snd-aloop lanes are gone, active and passive.

    A roleful box reaches its DAC over the ACTIVE ring
    (``jts_ring_active_playback``) and a stereo box over Ring B; ADR-0100 makes
    the SHM ring the ONE central transport. Re-declaring either PCM pair would
    restore a SECOND transport for one lane, which the no-legacy-fallback
    doctrine refuses. jasper-outputd opens no ALSA capture PCM at all now, so
    a re-declaration here would have no reader either.
    """
    rc = _non_comment((REPO / "deploy" / "alsa" / "asoundrc.jasper").read_text())
    # Positive control FIRST: every assertion below is an ABSENCE, so an empty
    # or comment-only read would satisfy all of them vacuously. Proving the
    # reader found the SURVIVING renderer ingress is what rules that out.
    assert "pcm.shairport_substream" in rc
    assert 'pcm "hw:Loopback,0,1"' in rc
    for name in (
        "pcm.outputd_active_content_playback",
        "pcm.outputd_active_content_capture",
        "ctl.outputd_active_content_capture",
        "pcm.outputd_content_playback",
        "pcm.outputd_content_capture",
        "ctl.outputd_content_capture",
    ):
        assert name not in rc, f"{name} was re-declared in asoundrc.jasper"
    # Nothing may claim substream 5 or 6 under any alias — the pairs stay free.
    # Deliberately the broader of the two assertions: a re-declaration fails
    # here whatever the PCM is named, because a slave has to spell the
    # substream to reach it. Both halves of both pairs, since a lane needs only
    # one end to come back.
    for substream in ("Loopback,0,5", "Loopback,1,5", "Loopback,0,6", "Loopback,1,6"):
        assert substream not in rc, f"{substream} was re-declared in asoundrc.jasper"


def test_active_path_pcms_never_use_plug_or_plughw():
    """Contract: NO `type plug` / `plughw:` anywhere on the active-crossover
    path. `plug` is the auto-converting channel/rate/format plugin; on a live-
    driver path it could remix 8->4 onto a tweeter (the most dangerous
    fail-open in active mode)."""
    render_lib = (REPO / "deploy" / "lib" / "jasper-asound-render.sh").read_text()
    assert "plughw" not in render_lib
    assert "type plug" not in render_lib


def test_every_single_dac_profile_renders_raw_hw_with_no_plug():
    """Every registered single DAC profile renders `outputd_dac` as a raw
    `type hw` block, never `type plug` — structurally, so the loop covers any
    future single DAC profile automatically."""
    render_lib = REPO / "deploy" / "lib" / "jasper-asound-render.sh"
    for profile in dac.all_profiles():
        if profile.kind != "single":
            continue
        env = os.environ.copy()
        env.update({
            "OUTPUT_DAC_ID": profile.id,
            "OUTPUT_DAC_CARD": "testcard",
            "OUTPUT_DAC_RECOGNIZED": "1",
        })
        result = subprocess.run(
            [
                "bash",
                "-c",
                'source "$1"; jasper_asound_outputd_dac_pcm_block',
                "bash",
                str(render_lib),
            ],
            check=False,
            text=True,
            capture_output=True,
            env=env,
        )
        assert result.returncode == 0, (profile.id, result.stderr)
        assert "type hw" in result.stdout, profile.id
        assert "card testcard" in result.stdout, profile.id
        assert "plug" not in result.stdout, profile.id


def test_asoundrc_declares_outputd_rendered_dac_alias_placeholder():
    rc = _non_comment((REPO / "deploy" / "alsa" / "asoundrc.jasper").read_text())
    render_lib = (REPO / "deploy" / "lib" / "jasper-asound-render.sh").read_text()
    assert "__OUTPUTD_DAC_PCM_BLOCK__" in rc
    assert "__OUTPUTD_DAC_CTL_BLOCK__" in rc
    assert "__OUTPUT_DAC_CARD__" not in rc
    assert "line//__OUTPUT_DAC_CARD__" not in render_lib
    assert "OUTPUT_DAC_RECOGNIZED:-1" in render_lib


def test_install_consumes_reconciled_output_without_reusing_dongle_mixer_card():
    install_sh = installer_text()
    install_without_env_migrations = "\n".join(
        path.read_text(encoding="utf-8")
        for path in installer_shell_paths()
        if path.name != "env-migrations.sh"
    )
    reconcile = (REPO / "deploy" / "bin" / "jasper-audio-hardware-reconcile").read_text()
    assert "select_audio_hardware_roles()" in install_sh
    assert "jasper-audio-hardware-reconcile\" --print-env" in install_sh
    assert "apply_observed_single_policy()" in reconcile
    assert 'OUTPUT_DAC_ID="$OBSERVED_OUTPUT_PROFILE_ID"' in reconcile
    assert 'OUTPUT_DAC_CARD="$OBSERVED_OUTPUT_SELECTED_CARD_ID"' in reconcile
    # Classification is registry-backed and the shell holds no hardware label:
    # the classifier's env emitter names the Apple cards (ADR-0235 R2).
    assert "usb-c to 3.5mm" not in reconcile.lower()
    assert "find_card" not in reconcile
    assert 'DONGLE_CARD="${OBSERVED_OUTPUT_APPLE_CARD_IDS%% *}"' in reconcile
    assert "DAC8X_OUTPUT_CARD=" not in reconcile
    assert "DAC8X_STUDIO_OUTPUT_CARD=" not in reconcile
    assert "jasper_asound_render_template" in install_sh
    assert "asoundrc.jasper.source" in install_sh
    assert "JASPER_AUDIO_DAC_ID" in install_sh
    assert "JASPER_AUDIO_DAC_CARD" in reconcile
    assert "JASPER_OUTPUT_DAC_ROUTE" not in reconcile
    assert "OUTPUT_DAC_ROUTE" not in install_without_env_migrations
    assert "APPLE_DONGLE_PRESENT=1" in reconcile
    assert "APPLE_DONGLE_PRESENT=0" in reconcile
    assert 'APPLE_DONGLE_SERVICE_CARD="auto"' in reconcile


def test_output_dac_route_policy_is_removed_from_renderer_and_reconciler():
    route_lib = (REPO / "deploy" / "lib" / "jasper-asound-render.sh").read_text()
    reconcile = (REPO / "deploy" / "bin" / "jasper-audio-hardware-reconcile").read_text()
    assert "JASPER_OUTPUT_DAC_ROUTE" not in route_lib
    assert "OUTPUT_DAC_ROUTE" not in route_lib
    assert "mono:([1-8])" not in route_lib
    assert "stereo:([1-8]),([1-8])" not in route_lib
    assert "type route" not in route_lib
    assert 'OUTPUT_DAC_ID:-}" == "dual_apple_usb_c_dac_4ch"' in route_lib
    assert "type null" in route_lib
    assert "jasper_asound_route_ignored()" not in reconcile
    assert "event=audio_hardware_reconcile.${name}" in reconcile


def _bash_function(path: Path, name: str) -> str:
    text = path.read_text()
    start = text.index(f"\n{name}() {{")
    return text[start : text.index("\n}\n", start) + 3]


# The role gate that enables/disables these two units is covered end to end by
# tests/test_audio_hardware_reconcile.py, which also proves it is CALLED.
DRIFTED_HEADPHONE_STATE = "  Front Left: Playback 80 [67%] [-20.00dB] [on]"


def _amixer_double(tmp_path: Path) -> tuple[Path, Path]:
    """An `amixer` that records its argv and reports a drifted Headphone."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = bin_dir / "amixer.log"  # absent until amixer is actually invoked
    (bin_dir / "amixer").write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> {shlex.quote(str(log))}\n'
        f"printf '%s\\n' {shlex.quote(DRIFTED_HEADPHONE_STATE)}\n"
    )
    (bin_dir / "amixer").chmod(0o755)
    return bin_dir, log


def _start_monitor(
    tmp_path: Path, bin_dir: Path, board: dict[str, str], *, card: str = "auto"
) -> tuple[subprocess.Popen[bytes], Path]:
    """The drift monitor plus its journal, reading `board` through the
    classifier's own seams."""
    journal = tmp_path / "monitor.log"
    return (
        subprocess.Popen(
            [
                "/bin/bash",
                str(REPO / "deploy" / "bin" / "jasper-headphone-monitor"),
                card, "Headphone",
            ],
            cwd=REPO,
            env={
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "JASPER_OUTPUT_HARDWARE_PYTHON": sys.executable,
                # `true -L` lists nothing, so an empty board stays empty
                # instead of falling back to the dev machine's own cards.
                "JASPER_APLAY": "true",
                **board,
            },
            stdout=journal.open("wb"),
            stderr=subprocess.DEVNULL,
        ),
        journal,
    )


def _await(monitor: subprocess.Popen[bytes], log: Path, expected: tuple[str, ...]):
    """Block until every line in `expected` has reached `log`, or the monitor
    exits. Both are the monitor's own observable transitions, so the verdict
    does not move with machine load; the ceiling is only a hang backstop --
    never a timing assertion (#3092)."""
    deadline = time.monotonic() + 120.0
    text = ""
    while time.monotonic() < deadline:
        text = log.read_text() if log.exists() else ""
        if all(line in text for line in expected):
            return
        code = monitor.poll()
        if code is not None:
            raise AssertionError(f"monitor exited with {code}; {log.name}: {text!r}")
        time.sleep(0.02)
    raise AssertionError(f"monitor never reached {expected}; {log.name}: {text!r}")


def test_the_boot_pin_and_the_drift_monitor_resolve_their_card_at_runtime():
    """Neither helper may carry a card id baked in at install time. The monitor
    unit renders `<helper> auto Headphone` and resolves the dongle on every
    start; the boot pin takes no card argument at all — it reads the card off
    the reconciler's record, and is ordered after alsa-restore so a restored
    snapshot cannot outrun the pin."""
    init_unit = (REPO / "deploy" / "systemd" / "jasper-dac-init.service").read_text()
    monitor_unit = (
        REPO / "deploy" / "systemd" / "jasper-headphone-monitor.service"
    ).read_text()
    assert "ExecStart=/usr/local/bin/jasper-dac-init\n" in init_unit
    assert "After=sound.target alsa-restore.service" in init_unit
    assert "__APPLE_DONGLE_CARD__" not in init_unit
    assert (
        "ExecStart=/usr/local/bin/jasper-headphone-monitor __APPLE_DONGLE_CARD__ Headphone"
        in monitor_unit
    )
    assert 's/__APPLE_DONGLE_CARD__/${APPLE_DONGLE_SERVICE_CARD}/g' in installer_text()


_BOTH_APPLE_PINS = (
    "-c A sset Headphone 100% unmute",
    "-c A_1 sset Headphone 100% unmute",
)


def test_the_drift_monitor_pins_every_apple_card_the_classifier_names(tmp_path):
    """Which attached cards are Apple is the classifier's answer, not a label
    match in the shell (ADR-0235 R2): a board carrying two Apple DACs gets both
    re-pinned, under the card ids the emitter named."""
    bin_dir, log = _amixer_double(tmp_path)
    monitor, _ = _start_monitor(tmp_path, bin_dir, _dual_apple_cards(tmp_path))
    try:
        _await(monitor, log, _BOTH_APPLE_PINS)
    finally:
        monitor.kill()
        monitor.wait()


def test_the_drift_monitor_trusts_an_explicit_configured_card(tmp_path):
    """A non-`auto` argument is an operator override (ADR-0235 R2 carries this
    branch forward from the deleted `resolve_cards`): the monitor pins that
    card directly and never asks the classifier, so no Python is needed."""
    bin_dir, log = _amixer_double(tmp_path)
    empty = tmp_path / "sys" / "class" / "sound"
    empty.mkdir(parents=True)
    (tmp_path / "proc" / "asound").mkdir(parents=True)
    monitor, _ = _start_monitor(
        tmp_path,
        bin_dir,
        {
            "JASPER_SYS_CLASS_SOUND": str(empty),
            "JASPER_PROC_ASOUND": str(tmp_path / "proc" / "asound"),
        },
        card="Dongle_1",
    )
    try:
        _await(monitor, log, ("-c Dongle_1 sset Headphone 100% unmute",))
    finally:
        monitor.kill()
        monitor.wait()


def test_the_drift_monitor_stays_up_and_re_asks_when_a_card_appears(tmp_path):
    """The monitor is enabled on boxes whose dongle comes and goes, and
    jasper-audio-hardware-reconcile never re-execs it (a restart per pass burns
    `StartLimitBurst`). So an absent dongle must be a poll, never an exit, and
    the card set must be re-asked when the board's cards move."""
    bin_dir, log = _amixer_double(tmp_path)
    empty = tmp_path / "sys" / "class" / "sound"
    empty.mkdir(parents=True)
    (tmp_path / "proc" / "asound").mkdir(parents=True)
    monitor, journal = _start_monitor(
        tmp_path,
        bin_dir,
        {
            "JASPER_SYS_CLASS_SOUND": str(empty),
            "JASPER_PROC_ASOUND": str(tmp_path / "proc" / "asound"),
        },
    )
    try:
        # The absent event is the monitor's own proof that it asked, and got
        # no Apple card, BEFORE the board grew one.
        _await(monitor, journal, ("event=apple_dongle.headphone_monitor.absent",))
        assert not log.exists(), "nothing to reset when no dongle is present"
        _dual_apple_cards(tmp_path)
        _await(monitor, log, _BOTH_APPLE_PINS)
    finally:
        monitor.kill()
        monitor.wait()


def _flaky_emitter_python(tmp_path: Path) -> Path:
    """A `python` stand-in whose first `-m jasper.cli.output_hardware --env`
    call fails (an OOM at boot, a transient non-zero exit); every later call
    delegates to the real interpreter. Proves a failed probe is retried on
    the next poll rather than latched (#4027)."""
    counter = tmp_path / "emitter-calls"
    counter.write_text("0", encoding="utf-8")
    fake = tmp_path / "flaky-python"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        f"n=$(cat {shlex.quote(str(counter))})\n"
        f"printf '%s' $((n + 1)) > {shlex.quote(str(counter))}\n"
        'if [[ "$n" == "0" ]]; then\n'
        "  exit 1\n"
        "fi\n"
        f'exec {shlex.quote(sys.executable)} "$@"\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return fake


def test_the_drift_monitor_retries_a_failed_probe_on_the_next_poll(tmp_path):
    """A probe that fails once must not latch the last-known (empty) card set
    forever: the next poll has to re-ask the classifier for real, even though
    the board's card population never changed across the failure (#4027)."""
    bin_dir, log = _amixer_double(tmp_path)
    flaky_python = _flaky_emitter_python(tmp_path)
    monitor, _ = _start_monitor(
        tmp_path,
        bin_dir,
        {
            **_dual_apple_cards(tmp_path),
            "JASPER_OUTPUT_HARDWARE_PYTHON": str(flaky_python),
        },
    )
    try:
        _await(monitor, log, _BOTH_APPLE_PINS)
    finally:
        monitor.kill()
        monitor.wait()


def test_the_drift_monitor_fails_loudly_when_the_classifier_cannot_run(tmp_path):
    """No card set, no work: the monitor names the reconciler's own
    probe-unavailable reason and exits instead of spinning on a stale one."""
    result = subprocess.run(
        [
            "/bin/bash",
            str(REPO / "deploy" / "bin" / "jasper-headphone-monitor"),
            "auto", "Headphone",
        ],
        env={**os.environ, "JASPER_OUTPUT_HARDWARE_PYTHON": str(tmp_path / "absent")},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 1
    assert "reason=python_unavailable" in result.stderr


def test_apple_dongle_udev_rule_escapes_literal_headphone_percent():
    rule = (REPO / "deploy" / "udev" / "99-jasper-apple-dongle.rules").read_text()
    run_line = next(
        line
        for line in rule.splitlines()
        if "RUN+=" in line and not line.lstrip().startswith("#")
    )
    assert "100%% unmute" in run_line
    assert "100% unmute" not in run_line


def test_audio_hardware_reconciler_is_installed_and_udev_triggered():
    install_sh = installer_text()
    unit = (REPO / "deploy" / "systemd" / "jasper-audio-hardware-reconcile.service").read_text()
    rule = (REPO / "deploy" / "udev" / "99-jasper-audio-hardware-reconcile.rules").read_text()
    reconcile = (REPO / "deploy" / "bin" / "jasper-audio-hardware-reconcile").read_text()
    runtime_contract = (REPO / "jasper" / "active_speaker" / "runtime_contract.py").read_text()
    startup_load = (REPO / "jasper" / "active_speaker" / "startup_load.py").read_text()
    assert "deploy/systemd/jasper-audio-hardware-reconcile.service" in install_sh
    assert "deploy/bin/jasper-audio-hardware-reconcile" in install_sh
    assert "deploy/bin/jasper-output-hardware-hotplug" in install_sh
    assert "deploy/bin/jasper-outputd-failure-reconcile" in install_sh
    assert "deploy/lib/jasper-asound-render.sh" in install_sh
    assert "/usr/local/lib/jasper/jasper-asound-render.sh" in install_sh
    assert "99-jasper-audio-hardware-reconcile.rules" in install_sh
    assert "ExecStart=/usr/local/sbin/jasper-audio-hardware-reconcile --reason unit-start" in unit
    assert (
        "ExecCondition=/usr/local/sbin/jasper-audio-hardware-reconcile --reason unit-start --changed" in unit
    )
    # RemainAfterExit would make every later `start` a no-op, so a hot-plug
    # would never reconcile.
    assert not any(
        line.startswith("RemainAfterExit") for line in unit.splitlines()
    )
    assert "Before=jasper-outputd.service" in unit
    before_line = next(
        line for line in unit.splitlines() if line.startswith("Before=")
    )
    assert "jasper-dac-init.service" not in before_line
    assert "jasper-headphone-monitor.service" not in before_line
    assert 'ACTION=="add|remove|change", SUBSYSTEM=="sound", KERNEL=="controlC*"' in rule
    assert 'ENV{SYSTEMD_WANTS}+="jasper-audio-hardware-reconcile.service"' in rule
    assert 'ACTION=="remove", SUBSYSTEM=="usb", ENV{PRODUCT}=="5ac/110a/*"' in rule
    assert 'ACTION=="remove", SUBSYSTEM=="usb", ENV{PRODUCT}=="05ac/110a/*"' in rule
    assert 'RUN+="/usr/local/sbin/jasper-output-hardware-hotplug"' in rule
    hotplug = (REPO / "deploy" / "bin" / "jasper-output-hardware-hotplug").read_text()
    assert "--no-block start jasper-audio-hardware-reconcile.service" in hotplug
    assert "event=audio_hardware_hotplug.reconcile_requested" in hotplug
    assert "/usr/local/sbin/jasper-audio-hardware-reconcile --reason install" in install_sh
    # The cutover gate is width-aware and shared by the composite + single
    # active paths, and trusts the durable runtime contract rather than
    # transient startup-load state: a saved active baseline must stay playable
    # after setup completes.
    assert "active_graph_status()" in reconcile
    assert "active_graph_width_out_of_range" in runtime_contract
    assert "action=park_until_active_graph" in reconcile
    assert 'JASPER_OUTPUTD_BACKEND" "fake"' in reconcile
    assert "JASPER_ACTIVE_SPEAKER_STARTUP_LOAD_STATE" not in reconcile
    assert "JASPER_CAMILLA_STATEFILE" in reconcile
    assert "JASPER_CAMILLA2_STATEFILE" in reconcile
    assert "JASPER_OUTPUT_TOPOLOGY_PATH" in reconcile
    assert "outputd_active_lane_decision" in reconcile
    assert "outputd_active_content_playback" in runtime_contract
    assert "AUDIO_HARDWARE_RECONCILE_UNIT" in startup_load
    assert "_trigger_audio_hardware_reconcile(source=\"active_speaker_startup_load\")" in startup_load
    assert "_trigger_audio_hardware_reconcile(source=\"active_speaker_startup_rollback\")" in startup_load


def test_install_alsa_refreshes_asound_renderer_before_rendering():
    install_sh = installer_text()
    start = install_sh.index("install_alsa() {")
    end = install_sh.index("\nwrite_build_manifest() {", start)
    install_alsa = install_sh[start:end]
    render_lib_install = install_alsa.index(
        "/usr/local/lib/jasper/jasper-asound-render.sh"
    )
    source_template_install = install_alsa.index("asoundrc.jasper.source")
    render_call = install_alsa.index("jasper_asound_render_template")
    assert "deploy/lib/jasper-asound-render.sh" in install_alsa
    assert render_lib_install < source_template_install
    assert render_lib_install < render_call


def test_voice_tts_socket_resolves_fanin_solo_and_outputd_when_bonded(monkeypatch):
    """systemd resolves env directives in order; the bonded override must win.

    Carried through to the values the daemon actually runs on: the resolved
    unit environment is fed to the real config loader, so a unit that stops
    naming the fan-in route fails here rather than at a silent solo box.
    """
    from .doctor_test_support import _fresh_cfg

    unit = (REPO / "deploy" / "systemd" / "jasper-voice.service").read_text()
    assert "EnvironmentFile=-/var/lib/jasper/tts.env" not in unit
    env_directives = [
        line.strip() for line in unit.splitlines()
        if line.strip().startswith(("Environment=", "EnvironmentFile="))
    ]
    assert env_directives[-1] == "EnvironmentFile=-/var/lib/jasper/grouping-voice.env"

    solo = _resolve_systemd_unit_env(unit, {})
    solo_cfg = _fresh_cfg(monkeypatch, GEMINI_API_KEY="AIzaSyTest", **solo)
    assert solo_cfg.tts_outputd_socket == FANIN_TTS_SOCKET
    assert solo_cfg.duck_transport == "fanin"

    bonded = _resolve_systemd_unit_env(
        unit,
        {
            "/var/lib/jasper/grouping-voice.env": (
                f"{VOICE_TTS_SOCKET_ENV}={OUTPUTD_TTS_SOCKET}\n"
                "JASPER_GROUPING_VOICE_PARK=1\n"
            ),
        },
    )
    bonded_cfg = _fresh_cfg(monkeypatch, GEMINI_API_KEY="AIzaSyTest", **bonded)
    assert bonded_cfg.tts_outputd_socket == OUTPUTD_TTS_SOCKET
    assert bonded_cfg.duck_transport == "fanin"
    assert bonded["JASPER_GROUPING_VOICE_PARK"] == "1"

    # The unit owns these names; the reconciler must not become a second writer.
    reconcile = (REPO / "deploy" / "bin" / "jasper-audio-hardware-reconcile").read_text()
    assert "TTS_ENV_FILE" not in reconcile
    assert VOICE_TTS_SOCKET_ENV not in reconcile
    assert DUCK_TRANSPORT_ENV not in reconcile


def test_the_duck_rides_the_same_lane_the_assistant_mixes_into(monkeypatch):
    """Ducking in CamillaDSP while TTS enters ahead of it attenuates the
    assistant's own audio along with the program. Two halves: the loader
    refuses the fan-in socket paired with a Camilla duck, and the daemon's
    selection actually reads `duck_transport` — a selection that ignored it
    would keep every config test green while the box double-ducks.
    """
    from jasper.camilla import Ducker
    from jasper.voice.daemon_main import build_ducker
    from jasper.voice_daemon import FanInDucker

    from .doctor_test_support import _fresh_cfg

    async def target_db() -> float:
        return -20.0

    def ducker(socket: str, transport: str):
        cfg = _fresh_cfg(
            monkeypatch,
            GEMINI_API_KEY="AIzaSyTest",
            **{VOICE_TTS_SOCKET_ENV: socket, DUCK_TRANSPORT_ENV: transport},
        )
        return build_ducker(
            cfg, volume_owner=object(), target_db_provider=target_db,
        )

    with pytest.raises(RuntimeError):
        ducker(FANIN_TTS_SOCKET, "camilla")

    fanin_ducker = ducker(FANIN_TTS_SOCKET, "fanin")
    camilla_ducker = ducker(OUTPUTD_TTS_SOCKET, "camilla")
    assert isinstance(fanin_ducker, FanInDucker)
    assert isinstance(camilla_ducker, Ducker)
    # The fan-in duck must leave Camilla free to act as the master volume.
    assert fanin_ducker.locks_camilla_volume is False
    assert camilla_ducker.locks_camilla_volume is True


def test_fanin_exposes_outputd_compatible_tts_socket():
    """fanin's TTS server is the SOLO production ingress; outputd serves the
    SAME wire protocol for bonded members, gated on the reconciler-set socket
    env (default OFF, so solo outputd stays TTS-free)."""
    main_rs = (REPO / "rust" / "jasper-fanin" / "src" / "main.rs").read_text()
    config_rs = (REPO / "rust" / "jasper-fanin" / "src" / "config.rs").read_text()
    tts_rs = (REPO / "rust" / "jasper-fanin" / "src" / "tts.rs").read_text()
    mixer_rs = (REPO / "rust" / "jasper-fanin" / "src" / "mixer.rs").read_text()
    outputd_main_rs = (REPO / "rust" / "jasper-outputd" / "src" / "main.rs").read_text()
    outputd_config_rs = (REPO / "rust" / "jasper-outputd" / "src" / "config.rs").read_text()
    outputd_lib_rs = (REPO / "rust" / "jasper-outputd" / "src" / "lib.rs").read_text()
    assert f'"{FANIN_TTS_SOCKET}"' in config_rs
    assert "spawn_tts_server(" in main_rs
    # outputd's twin: present, but constructed ONLY when the grouping
    # reconciler set the socket env (no baked-in default path).
    assert "spawn_tts_server(" in outputd_main_rs
    assert "if let Some(path) = &config.tts_socket_path" in outputd_main_rs
    assert (
        f'env_optional("{OUTPUTD_TTS_SOCKET_ENV}")' in outputd_config_rs
    )  # Option, no baked-in default — unset env means solo, TTS off
    assert "pub mod protocol;" not in outputd_lib_rs
    assert "TtsCommand::FlushSync" in tts_rs
    assert "TtsCommand::ProgramDuckOn" in tts_rs
    assert "prepare_period()" in mixer_rs
    # Program ducking is applied to the renderer sum BEFORE TTS is mixed in, so
    # renderer lanes duck while TTS/cues stay unattenuated.
    assert "program_target" in mixer_rs
    assert "ramp_program_duck(" in mixer_rs
    # Both duck paths take the box's `program_width`: the gain stage's rails and
    # mantissa are width-dependent, and a width-blind duck would clamp a
    # spine-scale sum at the i32 rails before the duck could recover it.
    duck_call = "apply_gain_to_sum(\n                &mut self.sum_buf,\n                self.program_duck_current,\n                self.program_width,\n            )"
    assert duck_call in mixer_rs
    assert "self.program_duck_release_step,\n                self.program_width,\n            );" in mixer_rs
    assert "tts.mix_period(&mut self.sum_buf, self.program_width)" in mixer_rs
    assert mixer_rs.index(duck_call) < mixer_rs.index(
        "tts.mix_period(&mut self.sum_buf, self.program_width)"
    )
    # The wire layer (command vocabulary + parser) lives ONCE in the shared
    # crate; both daemons consume it as a path dependency.
    proto_rs = (
        REPO / "rust" / "jasper-tts-protocol" / "src" / "lib.rs"
    ).read_text()
    # The resolved assistant width, published at start. A Rust unit test covers
    # the line's content but cannot see whether anything CALLS the renderer, so
    # both halves are pinned here: invoked, and invoked from the startup path.
    assert "config.assistant_wire_resolved_line()" in main_rs, (
        "fan-in must emit its resolved assistant width at startup; without it a "
        "support read cannot tell a converting mismatch from a coherent box "
        "except by waiting for a once-per-lifetime warn"
    )
    assert 'info!("{}", config.assistant_wire_resolved_line());' in main_rs
    assert "pub fn assistant_wire_resolved_line(" in config_rs
    # The voice half of the pair, so the two lines a support read compares are
    # pinned together rather than one of them drifting away silently.
    assert (
        '"tts_wire.resolved"'
        in (REPO / "jasper" / "audio_io.py").read_text()
    ), "jasper-voice must publish the width it resolved, to pair with fan-in's"

    assert '"PROGRAM_DUCK_ON"' in proto_rs
    # The whole-stereo-frame rule is stated ONCE, in the shared payload reader,
    # and interpolates the verb — `AUDIO` and `AUDIO32` cannot diverge on it.
    assert '{verb} byte length must contain whole stereo frames' in proto_rs
    assert 'strip_prefix("AUDIO ")' in proto_rs
    assert 'strip_prefix("AUDIO32 ")' in proto_rs
    assert "pub fn read_command" in proto_rs
    for crate in ("jasper-fanin", "jasper-outputd"):
        manifest = (REPO / "rust" / crate / "Cargo.toml").read_text()
        assert 'jasper-tts-protocol = { path = "../jasper-tts-protocol" }' in manifest
    assert '"PROGRAM_DUCK_ON"' not in tts_rs  # no drifting local copy


def test_outputd_dual_apple_sink_is_fail_closed_and_final_sink_only():
    config_rs = (REPO / "rust" / "jasper-outputd" / "src" / "config.rs").read_text()
    main_rs = (REPO / "rust" / "jasper-outputd" / "src" / "main.rs").read_text()
    alsa_rs = (REPO / "rust" / "jasper-outputd" / "src" / "alsa_backend.rs").read_text()
    # The transport dispatches on sink SHAPE, not the DAC's name; `dual_apple`
    # survives as a parse alias and the stable `/state` wire value.
    assert "SinkMode::Composite" in config_rs
    assert '"composite" | "dual_apple"' in config_rs
    assert "JASPER_OUTPUTD_DUAL_DAC_A_PCM" in config_rs
    # A PASSIVE composite is a parse-time refusal. The refusal ITSELF is owned
    # by config.rs's `a_passive_composite_parks_and_a_roleful_one_does_not`,
    # which calls `Config::from_env` and reads the error — the right altitude,
    # and the reason there is no mirror of its wording here. What is pinned
    # here is what that test cannot see: the discriminator is the ACTIVE-ring
    # endpoint marker (a roleful composite must still start), and the deleted
    # snd-aloop ACTIVE capture half must not come back as a default.
    assert "sink_mode == SinkMode::Composite && !ring_active_endpoint" in config_rs
    assert '"outputd_active_content_capture"' not in _non_comment_rust(config_rs)
    assert "dual_apple_requires_pre_dsp_tts" not in main_rs
    assert "run_alsa_dual_apple" not in main_rs
    assert "downmix_dual_active_reference" not in main_rs
    assert "enum RuntimeAlsaSink" in main_rs
    assert "Composite(PairedCompositeSink)" in main_rs
    assert "PairedCompositeSink::new(config)" in main_rs
    assert "deinterleave_4ch_to_dual_stereo" in alsa_rs
    assert "aborted on xrun/suspend" in alsa_rs
    assert "delay divergence" in alsa_rs


# ---------------------------------------------------------------------------
# The composite's bounded, linked-group xrun recovery (#2255).
#
# STATIC checks because `PairedCompositeSink` cannot be constructed without two
# live ALSA PCMs. The Rust unit tests cover the pure decisions (`xrun_policy`,
# `baseline_relatch_decision`, `delay_delta_check`, `prime_periods`); these pin
# the WIRING between them, which is what a plausible-looking rewrite would
# silently break.
# ---------------------------------------------------------------------------


# The pre-#2255 child write, verbatim. Positive control for every slicer below:
# an assertion that does not FAIL against this text inspects nothing.
PRE_2255_CHILD_WRITE = '''
fn write_dac_fail_closed<S: Copy>(
    io: &IO<'_, S>,
    pcm_name: &str,
    samples: &[S],
    xrun_count: &mut u64,
) -> Result<()> {
    let frames_total = samples.len() / (CHANNELS as usize);
    let mut frames_done = 0usize;
    while frames_done < frames_total {
        let offset = frames_done * (CHANNELS as usize);
        match io.writei(&samples[offset..]) {
            Ok(0) => {
                anyhow::bail!("outputd dual Apple DAC {pcm_name} writei returned 0 frames");
            }
            Ok(n) => frames_done += n,
            Err(e) => {
                let errno = e.errno();
                if errno == libc::EPIPE || errno == libc::ESTRPIPE {
                    *xrun_count += 1;
                    anyhow::bail!(
                        "outputd dual Apple DAC {pcm_name} aborted on xrun/suspend errno={errno}"
                    );
                }
                return Err(e).context(format!("writing outputd dual Apple DAC {pcm_name}"));
            }
        }
    }
    Ok(())
}
'''


def _epipe_arm(child_write_body: str) -> str:
    """The brace-matched `if errno == libc::EPIPE …` block of a child write.

    `_rust_fn_body` is a brace-matched BLOCK extractor whose `signature`
    argument is just the anchor it searches for, so it slices an `if` block as
    happily as a `fn` body. Used that way here rather than copied, so both the
    scan and the string-literal blanking that makes it brace-safe stay in one
    place.
    """
    return _rust_fn_body(child_write_body, None, "if errno == libc::EPIPE")


def _composite_child_write(alsa_rs: str) -> str:
    return _rust_fn_body(alsa_rs, None, "fn write_dac_fail_closed<")


def test_composite_child_xrun_recovers_the_group_instead_of_bailing_on_the_first_one():
    """#2255: the composite child write recovers a bounded number of times.

    A bare bail on `EPIPE` is exit 1, so `Restart=on-failure`, so
    `StartLimitBurst=5`, so `StartLimitAction=reboot` — a USB dongle burp
    rebooting the speaker, observed live.
    """
    alsa_rs = (REPO / "rust" / "jasper-outputd" / "src" / "alsa_backend.rs").read_text()
    arm = _non_comment_rust(_epipe_arm(_composite_child_write(alsa_rs)))

    # It recovers, through the SHARED budget rather than a second copy of one.
    assert "try_recover(" in arm
    assert "xrun_policy(" in arm
    # The recovery is the GROUP's, and the caller is told so — the re-prime,
    # the group start and the bounded re-latch are all owed after this returns.
    assert "ChildWriteOutcome::GroupRecovered" in arm
    # `linked=` is the ONLY thing on the shared event line that distinguishes a
    # recovered xrun from a refused one.
    assert "event=outputd.xrun source={}" in arm
    assert "linked={}" in arm
    assert "child.linked" in arm.split("eprintln!", 1)[1].split(");", 1)[0], (
        "`linked=` must be fed from the child's actual link state, not a literal"
    )

    # Every remaining bail on this path is guarded: the unlinked refusal and
    # the exhausted budget, and nothing else.
    assert arm.count("anyhow::bail!") == 2
    assert "if !child.linked {" in arm
    unlinked = _rust_fn_body(arm, None, "if !child.linked")
    assert "anyhow::bail!" in unlinked
    assert "try_recover(" not in unlinked, (
        "an unlinked pair must NOT be recovered: there is no atomic group "
        "restart to re-establish A/B alignment with, so its post-recovery skew "
        "is unbounded and unverifiable"
    )


def test_the_composite_xrun_slicer_catches_the_pre_change_bail():
    """The tripwire for the test above: prove it inspects a real EPIPE arm.

    Run the same slicer and the same assertions against the pre-#2255 text.
    Every one of them must FAIL there — otherwise the test above is green for
    reasons that have nothing to do with the code.
    """
    arm = _non_comment_rust(_epipe_arm(PRE_2255_CHILD_WRITE))
    # A real arm, positively identified.
    assert "*xrun_count += 1;" in arm
    assert "aborted on xrun/suspend" in arm
    # And every clause of the live assertion is absent from it.
    assert "try_recover(" not in arm
    assert "xrun_policy(" not in arm
    assert "ChildWriteOutcome::GroupRecovered" not in arm
    assert "if !child.linked {" not in arm
    # The bare bail the live test forbids is exactly what the pre-change arm
    # has: one, unguarded.
    assert arm.count("anyhow::bail!") == 1


def test_both_outputd_write_paths_share_one_recovery_budget():
    """#2255: `xrun_policy` is THE budget, for the single sink and the composite.

    Two copies of the comparison is how the paths drifted apart before: one
    grew a bounded recovery and the other never did.
    """
    alsa_rs = (REPO / "rust" / "jasper-outputd" / "src" / "alsa_backend.rs").read_text()
    single = _non_comment_rust(_rust_fn_body(alsa_rs, None, "fn write_dac_frames<"))
    composite = _non_comment_rust(_composite_child_write(alsa_rs))

    # Both consult the shared policy…
    assert single.count("xrun_policy(") == 2, single
    assert composite.count("xrun_policy(") == 2, composite
    # …and neither re-derives the comparison locally, which is the shape that
    # would let them drift apart again.
    assert "> MAX_RECOVERIES_PER_PERIOD" not in single
    assert "> MAX_RECOVERIES_PER_PERIOD" not in composite
    # Exactly one definition of it exists.
    assert alsa_rs.count("fn xrun_policy(") == 1

    # `Ok(0)` rides the same budget on BOTH paths. A zero-frame return is not an
    # xrun (no stream position moves, no group action is owed), but a hard bail
    # on it puts the composite back on the reboot ladder.
    assert "Ok(0)" in composite
    ok_zero = _rust_fn_body(composite, None, "Ok(0) =>")
    assert "xrun_policy(" in ok_zero
    assert "try_recover(" not in ok_zero


def test_the_composite_recovery_budget_is_per_period_not_per_attempt():
    """#2255: the budget must be declared OUTSIDE the retry loop.

    Declared inside `loop {` it re-zeroes every pass, so a child that xruns
    forever recovers forever — an unbounded loop on outputd's SCHED_FIFO
    playout thread (`LimitRTTIME` kills at ~1 s, then the restart ladder walks
    to `StartLimitAction=reboot`). Every other assertion here stays green under
    that one-line move, so the POSITION is pinned rather than the presence.
    """
    alsa_rs = (REPO / "rust" / "jasper-outputd" / "src" / "alsa_backend.rs").read_text()
    body = _non_comment_rust(
        _rust_fn_body(alsa_rs, "impl PairedCompositeSink", "pub fn write_dual_period(")
    )
    assert body.count("let mut recoveries = 0u32;") == 1, (
        "exactly one per-period budget declaration; a second would reset it mid-period"
    )
    assert body.index("let mut recoveries = 0u32;") < body.index("loop {"), (
        "the recovery budget must be declared BEFORE the retry loop — declared "
        "inside it, it re-zeroes every pass and the recovery becomes unbounded"
    )


def test_composite_reprime_primes_below_the_start_threshold_then_starts_the_group():
    """#2255: the re-prime must not auto-start the group before both children fill.

    `manual_start` sets `start_threshold = buffer_frames`, so writing a full
    buffer to child A STARTS the linked group before child B is primed at all —
    baking in up to a full period (128 frames ≈ 2.667 ms at 48 kHz) of
    permanent A/B skew, which on an active 2-way IS the woofer/tweeter time
    alignment. The order is therefore asserted by position, not by presence.
    """
    alsa_rs = (REPO / "rust" / "jasper-outputd" / "src" / "alsa_backend.rs").read_text()
    body = _non_comment_rust(
        _rust_fn_body(alsa_rs, "impl PairedCompositeSink", "fn reprime_after_group_recovery(")
    )

    depth_at = body.index("prime_periods(")
    silence_at = body.index("fill_silence()")
    write_at = body.index("write_children(")
    start_at = body.index("start_dacs()")
    relatch_at = body.index("relatch_delay_baseline(")

    assert depth_at < write_at, "the prime depth must be computed before priming"
    assert silence_at < write_at, "the re-prime's payload is silence"
    assert write_at < start_at, (
        "the group start must come AFTER both children are primed — a start "
        "before B is filled is the skew this whole path exists to prevent"
    )
    assert start_at < relatch_at, (
        "the baseline may only be re-latched after the group has restarted; "
        "re-latching against a stopped pair blesses an offset that means nothing"
    )

    # The interleave is the shared child write, not a private per-child loop:
    # two loops is how "prime A fully, then prime B" creeps back in.
    assert "io_i16()" not in body and "io_i32()" not in body

    # An xrun DURING the re-prime fails closed rather than recursing: a
    # prepared, un-started stream has no playback position to underrun from, so
    # an EPIPE here means the pair is not in the state the recovery just put it
    # in. Warn-and-continue leaves every other assertion green, so the bail is
    # pinned inside its own arm.
    reprime_xrun = _rust_fn_body(
        body, None, "if self.write_children(recoveries)? == ChildWriteOutcome::GroupRecovered"
    )
    assert "anyhow::bail!" in reprime_xrun
    assert "status=xrun_during_reprime" in reprime_xrun
    assert "reprime_alignment_failures += 1" in reprime_xrun

    # And the prime depth is the shipped startup number, from the shipped
    # function, not a locally re-derived one.
    main_rs = (REPO / "rust" / "jasper-outputd" / "src" / "main.rs").read_text()
    assert "prime_periods(" in main_rs
    assert alsa_rs.count("pub fn prime_periods(") == 1
    assert "fn prime_periods(" not in main_rs


def test_composite_baseline_relatch_is_bounded_by_magnitude_not_by_count():
    """#2255: the re-latch bound is how far the pair MOVED, never how often.

    `snd_pcm_recover` resets a child's stream position, so the baseline must be
    re-latched. Blessing an arbitrary post-fault offset launders the fault the
    divergence guard exists to see, and a count threshold cannot see that harm:
    it is unbounded at count = 1.
    """
    alsa_rs = (REPO / "rust" / "jasper-outputd" / "src" / "alsa_backend.rs").read_text()
    decision = _non_comment_rust(_rust_fn_body(alsa_rs, None, "fn baseline_relatch_decision("))
    # One constant, one meaning: the SAME tolerance the steady-state guard uses.
    assert "max_delta_frames" in decision
    assert ".abs() <= max_delta_frames" in decision
    # The counters exist, but as observables.
    relatch = _non_comment_rust(
        _rust_fn_body(alsa_rs, "impl PairedCompositeSink", "fn relatch_delay_baseline(")
    )
    assert "baseline_relatch_decision(" in relatch
    assert "delay_baseline_relatches += 1" in relatch
    assert "reprime_alignment_failures += 1" in relatch
    # The refusal fails closed rather than re-latching anyway.
    refuse = _rust_fn_body(relatch, None, "BaselineRelatch::Refuse =>")
    assert "anyhow::bail!" in refuse
    assert "self.delay_delta_baseline = Some(" not in _non_comment_rust(refuse)
    # The magnitude bound is enforced in the daemon (fail-closed safety); the
    # COUNT threshold is the doctor's, so no count comparison lives here.
    assert "delay_baseline_relatches >" not in _non_comment_rust(alsa_rs)


def test_a_composite_may_not_be_driven_with_an_unlinked_child_pair():
    """#2255: `link=ok` is a precondition for driving a composite at all.

    The composite's recovery model is built on `snd_pcm_link` — group prepare,
    group re-prime, one atomic group start — so an unlinked pair is a box whose
    recovery model does not hold. It used to gate only the RING ARM, because
    the pair could stay on the snd-aloop transport instead; under one audio
    transport (ADR-0100) there is nothing to stay on, so the gate is
    unconditional and the box parks.
    """
    alsa_rs = (REPO / "rust" / "jasper-outputd" / "src" / "alsa_backend.rs").read_text()
    new_body = _non_comment_rust(
        _rust_fn_body(alsa_rs, "impl PairedCompositeSink", "pub fn new(")
    )
    assert "if !linked {" in new_body
    # Park-class (EX_CONFIG 78), not the restart ladder: whether two devices
    # link is a property of the devices, so every restart answers the same.
    refusal = _rust_fn_body(new_body, None, "if !linked {")
    assert "final_sink_startup(" in refusal
    assert "event=outputd.dual_apple.unlinked_pair_refused" in refusal
    # And the gate is asked BEFORE the sink is handed back.
    assert new_body.index("if !linked {") < new_body.index("Ok(Self {")
    # The knob that used to make this optional is gone with it — an unlinked
    # composite has no route left where it is merely a warning.
    config_rs = (REPO / "rust" / "jasper-outputd" / "src" / "config.rs").read_text()
    assert "JASPER_OUTPUTD_DUAL_REQUIRE_LINK" not in config_rs


def test_composite_child_xruns_are_attributed_per_child_in_state():
    """#2255: which dongle burped is the diagnostic; the recovery is the group's.

    Sink-level `dac_xrun_count` stays exactly what it was (the doctor's
    existing consumer). The per-child counts and the group's recovery
    bookkeeping ride the ALREADY-conditional `dual_apple` block, so a
    single-DAC box's `/state` does not change by a byte.
    """
    alsa_rs = (REPO / "rust" / "jasper-outputd" / "src" / "alsa_backend.rs").read_text()
    state_rs = (REPO / "rust" / "jasper-outputd" / "src" / "state.rs").read_text()

    for source in ("dual_dac_a", "dual_dac_b"):
        assert f'source: "{source}"' in alsa_rs

    # One group recovery per recovered xrun, not two: on a linked pair
    # `snd_pcm_recover` prepares both children, so per-child recovery counters
    # would state a fact that is not true.
    assert "group_recoveries" in alsa_rs
    assert "dac_a_recoveries" not in alsa_rs
    assert "dac_b_recoveries" not in alsa_rs

    dual_block = state_rs.split('push_kv_str_opt(&mut buf, "dac_a_pcm"', 1)[1].split(
        "buf.push_str(r#\"\"mix\":{\"#)", 1
    )[0]
    for key in (
        "dac_a_xruns",
        "dac_b_xruns",
        "group_recoveries",
        "delay_baseline_relatches",
        "reprime_alignment_failures",
    ):
        assert f'"{key}"' in dual_block, f"{key} missing from the /state dual_apple block"

    # Each counter must be plumbed from its OWN field, at both hops. The Rust
    # `/state` test builds a `CompositeStatus` literal and calls neither hop, so
    # a transposed pair reports the wrong dongle's xruns with every test green.
    status = _rust_fn_body(alsa_rs, "impl PairedCompositeSink", "pub fn dual_status(")
    marker = _rust_fn_body(state_rs, None, "pub fn mark_dual_apple_status(")
    for key, field in (
        ("dac_a_xruns", "dac_a_xrun_count"),
        ("dac_b_xruns", "dac_b_xrun_count"),
        ("group_recoveries", "group_recoveries"),
        ("delay_baseline_relatches", "delay_baseline_relatches"),
        ("reprime_alignment_failures", "reprime_alignment_failures"),
    ):
        assert f"{key}: self.{field}," in status, (
            f"CompositeStatus.{key} is not fed from PairedCompositeSink.{field}"
        )
        assert f"self.dual_{key}\n            .store(status.{key}," in marker, (
            f"OutputdState.dual_{key} is not stored from CompositeStatus.{key}"
        )


def test_outputd_composite_children_take_the_declared_edge_width():
    """Both composite children request the registry-declared edge format, prove
    it by readback, and STATUS reports what they negotiated.

    A static source check because `PairedCompositeSink::new` and `configure_pcm`
    cannot be constructed without live ALSA PCMs; the Rust unit tests cover the
    pure pieces and this pins the WIRING between them.
    """
    alsa_rs = (REPO / "rust" / "jasper-outputd" / "src" / "alsa_backend.rs").read_text()
    main_rs = (REPO / "rust" / "jasper-outputd" / "src" / "main.rs").read_text()

    # Count the requests rather than merely finding one: a single child left on
    # `SampleFormat::S16Le` opens at a different width than its sibling, and the
    # pair's negotiated-shape check does not compare formats.
    child_open = alsa_rs.split("impl PairedCompositeSink", 1)[1].split(
        "pub fn counters(", 1
    )[0]
    assert child_open.count('role: "dual_dac_a"') == 1
    assert child_open.count('role: "dual_dac_b"') == 1
    assert child_open.count("format: config.declared_dac_format,") == 2, (
        "both composite children must request the declared edge format"
    )
    # Scoped to the child-open block: the CONTENT lane in the same block
    # legitimately reads `config.content_format`, which is the other hop.
    assert "format: SampleFormat::S16Le," not in child_open

    # The readback branch covers the children, by the shared allowlist.
    assert 'matches!(role, "dac" | "dual_dac_a" | "dual_dac_b")' in alsa_rs
    assert "if is_final_edge_role(role) {" in alsa_rs

    # Child period buffers carry the width, and STATUS reads it from them —
    # `RuntimeAlsaSink::dac_format` must NOT answer with a constant for the
    # composite arm. Matched on the construction's ARGUMENTS rather than one
    # exact line, because rustfmt wraps the fallible call across four lines.
    child_periods_call = alsa_rs.split("let periods = ", 1)[1].split(";", 1)[0]
    assert "ChildPeriods::new(" in child_periods_call
    assert "config.declared_dac_format" in child_periods_call
    assert "config.period_frames" in child_periods_call
    assert "config.content_format" not in child_periods_call
    # Park-class: a refused child width must not restart-loop into
    # `StartLimitAction=reboot`. Scoped to the extracted PRODUCTION slice
    # (whole-file also matches the park-class test that exercises it), and
    # `startswith` because the wrapper has to be the OUTERMOST call — a
    # `ChildPeriods::new(...)` whose `?` fires first is exit-1/reboot class.
    assert child_periods_call.strip().startswith("final_sink_startup(ChildPeriods::new("), (
        f"the composite child-width construction must be wrapped in "
        f"final_sink_startup so a refused width parks at EX_CONFIG 78 instead of "
        f"restart-looping into StartLimitAction=reboot; got {child_periods_call!r}"
    )
    assert "Self::Composite(sink) => sink.dac_format()," in main_rs
    dac_format_fn = main_rs.split("fn dac_format(&self) -> SampleFormat {", 1)[1].split(
        "\n    }", 1
    )[0]
    # Positive anchors first, so a moved signature cannot leave the two absence
    # assertions below passing over an empty extraction.
    assert "Self::Single(sink) => sink.dac_format()," in dac_format_fn
    assert "Self::Composite(sink) => sink.dac_format()," in dac_format_fn
    assert "SampleFormat::S16Le" not in dac_format_fn
    assert "SampleFormat::S32Le" not in dac_format_fn


def test_neither_outputd_sink_opens_a_content_pcm():
    """ADR-0100: the ring is outputd's one upstream, so no sink opens an ALSA
    content capture PCM at all.

    Static because neither `new` can be constructed without live ALSA PCMs.
    The whole-file capture count is the invariant: a re-introduced lane on
    either sink shows up here whatever it is called.
    """
    alsa_rs = (REPO / "rust" / "jasper-outputd" / "src" / "alsa_backend.rs").read_text()
    main_rs = (REPO / "rust" / "jasper-outputd" / "src" / "main.rs").read_text()

    assert "Direction::Capture" not in _non_comment_rust(alsa_rs)
    # The /state stand-in it left behind: one synthetic, both sinks, one
    # vocabulary — no lane negotiated anything to report instead.
    assert "fn synthetic_content_negotiated(config: &Config) -> NegotiatedPcm {" in alsa_rs
    assert _non_comment_rust(alsa_rs).count("synthetic_content_negotiated(config)") == 2
    # And the run loop has one source. A box that declared no ring reaches the
    # park, never a second read path.
    assert "shm_ring.as_mut()" in main_rs
    assert "read_content_period" not in main_rs
    # The no-upstream arm parks by CLASS, not by wording: the marker is what
    # `runtime_error_exit_code` downcasts into EX_CONFIG 78, and pinning the
    # sentence instead would make a reworded remedy a test failure. Sliced from
    # the run loop's own else-arm so the marker cannot drift onto some other
    # error and leave this arm exiting 1 into the restart ladder.
    run_alsa = main_rs.split("fn run_alsa(", 1)[1].split("fn notify_ready", 1)[0]
    no_upstream = run_alsa.split("shm_ring.as_mut()", 1)[1].split("} else {", 1)[1]
    no_upstream = no_upstream.split("\n            }", 1)[0]
    assert "FinalSinkStartupConfigError" in no_upstream, no_upstream
    assert "return Err(" in no_upstream, no_upstream


def test_outputd_single_sink_is_width_parametric_with_mono_reference_fold():
    """The coherent single sink carries width as DATA (a DAC8x rides the same
    path as a 2ch Apple), publishes a stereo reference via a clip-proof 1/N mono
    fold for wide sinks, and counts real clipping instead of a hardwired 0."""
    config_rs = (REPO / "rust" / "jasper-outputd" / "src" / "config.rs").read_text()
    main_rs = (REPO / "rust" / "jasper-outputd" / "src" / "main.rs").read_text()
    alsa_rs = (REPO / "rust" / "jasper-outputd" / "src" / "alsa_backend.rs").read_text()

    # Width is reconciler-supplied data, validated, with the composite shape
    # pinned at 4 and the wide single path kept a pure passthrough.
    assert "JASPER_OUTPUTD_ACTIVE_CHANNELS" in config_rs
    assert "fixed at 4 (two stereo children)" in config_rs

    # The single backend reads + writes the runtime width, not a 2ch literal.
    assert "channels: u16," in alsa_rs
    assert "self.channels as usize" in alsa_rs

    # Mono reference fold (1/N, clip-proof) + honest clip accounting.
    assert "fn fold_reference(" in main_rs
    assert "fn fold_reference_pairwise_composite(" in main_rs
    assert "fn count_full_scale_samples(" in main_rs
    # The wide path folds; the 2ch path stays byte-identical (publishes content).
    assert "fold_reference(&content_buf, content_channels, &mut reference_buf);" in main_rs
    assert "fold_reference_pairwise_composite(&content_buf, &mut reference_buf);" in main_rs
    assert "ref_outputs.publish(&content_buf, next_reference_sequence);" in main_rs


def test_camilla_outputd_config_declares_outputd_lane():
    cutover = (REPO / "deploy" / "camilladsp" / "outputd-cutover.yml").read_text()
    camilla_unit = (REPO / "deploy" / "systemd" / "jasper-camilla.service").read_text()
    # Ring B: the one lane outputd reads (ADR-0100).
    assert 'device: "jts_ring_playback"' in cutover
    assert 'volume_limit: 0.0' in cutover
    # outputd's OWN statefile, never /var/lib/camilladsp/statefile.yml.
    assert "--statefile /var/lib/camilladsp/outputd-statefile.yml" in camilla_unit


def test_shipped_cutover_seed_declares_the_current_program_lane_width():
    """First-boot bytes must equal regenerated bytes.

    The shipped seed is what a box boots on before `jasper-sound
    render-flat-cutover` has ever run. A drift from the emitter pins the ring
    at one geometry on the first Camilla start and another on the first
    regeneration — and the ioplug pins the ring's period bytes min==max, so a
    drifted chunk does not degrade, it fails the open.

    Compared against the EMITTER rather than against literals, so the seed and
    its one writer cannot part company on any axis the parser exposes — which is
    now every axis the ring contract rests on, queue depth and rate-adjust
    included (both were unpinned until `parse_camilla_devices_config` learned
    them).
    """
    from jasper.camilla_config_contract import (
        DEFAULT_PLAYBACK_FORMAT,
        parse_camilla_devices_config,
    )
    from jasper.sound.camilla_yaml import emit_flat_outputd_cutover_config

    cutover = REPO / "deploy" / "camilladsp" / "outputd-cutover.yml"
    emitted = parse_camilla_devices_config(emit_flat_outputd_cutover_config())
    seeded = parse_camilla_devices_config(cutover.read_text(encoding="utf-8"))
    assert seeded["playback_format"] == DEFAULT_PLAYBACK_FORMAT
    for key in (
        "capture_device",
        "playback_device",
        "capture_format",
        "playback_format",
        "chunksize",
        "target_level",
        "queuelimit",
        "enable_rate_adjust",
        "samplerate",
        "volume_limit",
    ):
        assert seeded[key] == emitted[key], key


def _run_ensure_outputd_camilla_statefile(
    tmp_path, *, graph_output: str, graph_status: int = 0, restart_knob: str = "0",
) -> tuple[subprocess.CompletedProcess[str], list[str], list[str]]:
    """Run install.sh's statefile step with its graph command stubbed out.

    `run_captured_command` is the seam: it records the argv the step builds
    without needing /opt/jasper's venv, and drives the branch the step takes.
    """
    workdir = tmp_path / f"run{sum(1 for _ in tmp_path.iterdir())}"
    workdir.mkdir()
    _systemctl, systemctl_log = fake_systemctl(workdir)
    graph_log = workdir / "graph.log"
    step = _bash_function(REPO / "deploy" / "install.sh", "ensure_outputd_camilla_statefile")
    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            "set -uo pipefail\n"
            f'CAMILLA_CONF="{workdir}/camilladsp"\n'
            "run_captured_command() {\n"
            "  local variable=\"$1\"; shift\n"
            f'  printf "%s\\n" "$*" >> "{graph_log}"\n'
            f'  printf -v "$variable" "%s" {shlex.quote(graph_output)}\n'
            f"  return {graph_status}\n"
            "}\n"
            f"{step}\nensure_outputd_camilla_statefile",
        ],
        env={
            "PATH": f"{workdir}:/usr/bin:/bin",
            "JASPER_SYSTEMCTL_LOG": str(systemctl_log),
            "JASPER_RESTART_CAMILLA_ON_STATEFILE_REPAIR": restart_knob,
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    def lines(path: Path) -> list[str]:
        return path.read_text().splitlines() if path.exists() else []

    return result, lines(graph_log), lines(systemctl_log)


def test_install_seeds_the_separate_outputd_statefile_through_the_runtime_contract(
    tmp_path,
):
    """Runtime graph selection belongs to jasper.active_speaker, not install.sh.

    The step must ask the runtime contract — naming outputd's OWN statefile, so
    it never overwrites /var/lib/camilladsp/statefile.yml — and must fail
    closed when the contract refuses.
    """
    result, graph, systemctl = _run_ensure_outputd_camilla_statefile(
        tmp_path, graph_output="statefile written: no",
    )
    assert result.returncode == 0, result.stderr
    assert len(graph) == 1
    argv = graph[0].split()
    assert argv[:2] == ["/opt/jasper/.venv/bin/jasper-active-speaker", "runtime-safe-graph"]
    assert "--write-statefile" in argv
    assert argv[argv.index("--statefile") + 1] == (
        "/var/lib/camilladsp/outputd-statefile.yml"
    )
    assert argv[argv.index("--flat-config") + 1].endswith("/outputd-cutover.yml")
    assert "--ring-flat-config" not in argv
    assert systemctl == []

    refused, _graph, systemctl = _run_ensure_outputd_camilla_statefile(
        tmp_path, graph_output="", graph_status=1,
    )
    assert refused.returncode != 0
    assert systemctl == []


def test_install_restarts_camilla_only_when_it_repaired_the_statefile(tmp_path):
    """The repair bounce is opt-in and conditional on an actual write."""
    def bounces(*, written: str, knob: str) -> list[str]:
        return _run_ensure_outputd_camilla_statefile(
            tmp_path, graph_output=f"statefile written: {written}", restart_knob=knob,
        )[2]

    assert bounces(written="no", knob="1") == []
    assert bounces(written="yes", knob="0") == []
    assert bounces(written="yes", knob="1") == ["restart jasper-camilla.service"]


def test_outputd_parks_on_missing_configured_output_dac_without_reboot_loop():
    outputd_unit = (REPO / "deploy" / "systemd" / "jasper-outputd.service").read_text()
    camilla_unit = (REPO / "deploy" / "systemd" / "jasper-camilla.service").read_text()
    cutover = (REPO / "deploy" / "camilladsp" / "outputd-cutover.yml").read_text()
    recover_rule = (
        REPO / "deploy" / "udev" / "99-jasper-audio-hardware-reconcile.rules"
    ).read_text()
    recover_unit = (
        REPO / "deploy" / "systemd" / "jasper-audio-hardware-reconcile.service"
    ).read_text()
    recover_script = (REPO / "deploy" / "bin" / "jasper-audio-hardware-reconcile").read_text()
    failure_reconcile = (
        REPO / "deploy" / "bin" / "jasper-outputd-failure-reconcile"
    ).read_text()
    assert "StartLimitAction=reboot" in outputd_unit
    assert "Restart=on-failure" in outputd_unit
    assert "RestartPreventExitStatus=78" in outputd_unit
    assert "ExecCondition=/bin/sh -c" in outputd_unit
    assert 'backend="$${JASPER_OUTPUTD_BACKEND:-alsa}"' in outputd_unit
    assert '[ "$$backend" = "fake" ]' in outputd_unit
    assert 'card="$${JASPER_AUDIO_DAC_CARD:-}"' in outputd_unit
    assert '[ -e "/proc/asound/$$card" ]' in outputd_unit
    assert "event=outputd.output_device_gate.park reason=missing_dac" in outputd_unit
    assert 'device: "jts_ring_playback"' in cutover
    assert "outputd_backend=$$backend" in outputd_unit
    assert "exit 1" in outputd_unit
    assert "ExecStartPre=/bin/sh -c" not in outputd_unit
    assert "ExecStopPost=-/usr/local/sbin/jasper-outputd-failure-reconcile" in outputd_unit
    assert "--reason outputd-failure --no-restart" in failure_reconcile
    assert "--reason outputd-config-failure --no-restart" in failure_reconcile
    assert "--no-block restart jasper-outputd.service" in failure_reconcile
    assert "JASPER_OUTPUTD_CONFIG_RETRY_STATE" in failure_reconcile
    assert 'RESULT="${SERVICE_RESULT:-unknown}"' in failure_reconcile
    assert 'STATUS="${EXIT_STATUS:-}"' in failure_reconcile
    assert '"$RESULT" == "success"' in failure_reconcile
    # `exec-condition` is systemd's own SERVICE_RESULT spelling for an
    # ExecCondition skip (systemd.service(5)); the bare `condition` is a literal
    # systemd never emits, and pinning it holds the skip branch dead.
    assert '"$RESULT" == "exec-condition"' in failure_reconcile
    assert 'CONFIG_EXIT_STATUS=78' in failure_reconcile

    assert "JASPER_AUDIO_DAC_CARD" not in camilla_unit
    assert 'ENV{SYSTEMD_WANTS}+="jasper-audio-hardware-reconcile.service"' in recover_rule
    assert "Before=jasper-outputd.service" in recover_unit
    assert "--no-block start jasper-outputd.service" in recover_script
    assert "--no-block restart jasper-outputd.service" in recover_script
    assert "--no-block stop jasper-voice.service jasper-outputd.service" in recover_script


def test_outputd_alsa_loop_publishes_reference_only_after_dac_write():
    """inv-A ordering, both branches: the reference tap publishes what the DAC
    was JUST given, never earlier. Solo publishes the raw content period; the
    bonded TTS branch publishes the post-mix engine period, with the duck
    applied to the CONTENT before the mix so the reference carries the ducked
    program too."""
    main_rs = (REPO / "rust" / "jasper-outputd" / "src" / "main.rs").read_text()
    run_alsa = main_rs.split("fn run_alsa(", 1)[1].split("fn notify_ready", 1)[0]
    # Solo branch. Each needle is the solo call's exact one-line form so it
    # cannot bind to the TTS branch's multi-line call. The one upstream is the
    # ring (ADR-0100), so the read is its `read_period`.
    content_read = run_alsa.index("let read = src.read_period(&mut content_buf);")
    dac_write = run_alsa.index("sink.write_period(&content_buf)?;")
    # Width-2 publishes the content directly; the wide sink folds to a stereo
    # reference first. Either way publish follows the DAC write and precedes
    # the period mark.
    publish = run_alsa.index(
        "ref_outputs.publish(&content_buf, next_reference_sequence);"
    )
    composite_fold = run_alsa.index(
        "fold_reference_pairwise_composite(&content_buf, &mut reference_buf);"
    )
    # REAL clip accounting (a full-scale-sample count), so the no-clip
    # commissioning gate is not vacuously green.
    clipped = run_alsa.index("let clipped = count_full_scale_samples(&content_buf);")
    state = run_alsa.index(
        "state_counters(&sink),",
        clipped,
    )
    assert content_read < dac_write < publish < state
    assert dac_write < clipped < composite_fold < state
    assert "state.mark_period(sink.counters(), reference_sequence, 0)" not in run_alsa
    assert "clipped_samples=0" not in run_alsa
    assert "fn state_counters(" in main_rs

    # Bonded TTS branch — duck → prepare(mix) → DAC write → DAC-true
    # commit → post-mix reference publish → ledger-true state mark.
    duck = run_alsa.index("bridge.content_duck_gain()")
    prepare = run_alsa.index("core.prepare_period_with_content(&content_buf);")
    tts_write = run_alsa.index("sink.write_period(core.output_period())?;")
    commit = run_alsa.index("core.commit_prepared_period_with_dac_delay(")
    tts_publish = run_alsa.index(
        "ref_outputs.publish(core.output_period(), reference_sequence);"
    )
    tts_state = run_alsa.index(
        "state_counters(&sink),",
        tts_publish,
    )
    assert content_read < duck < prepare < tts_write < commit < tts_publish
    assert tts_publish < tts_state


def test_outputd_chip_ref_tee_is_diagnostic_only_and_env_gated():
    main_rs = (REPO / "rust" / "jasper-outputd" / "src" / "main.rs").read_text()
    config_rs = (REPO / "rust" / "jasper-outputd" / "src" / "config.rs").read_text()
    state_rs = (REPO / "rust" / "jasper-outputd" / "src" / "state.rs").read_text()
    run_alsa = main_rs.split("fn run_alsa(", 1)[1].split("fn notify_ready", 1)[0]
    writer = main_rs.split("fn run_chip_ref_writer(", 1)[1].split(
        "fn write_playback_period(",
        1,
    )[0]

    assert "JASPER_OUTPUTD_CHIP_REF_TEE_PATH" in config_rs
    assert "chip_ref_tee_path: env_optional(" in config_rs
    assert "write_chip_ref_tee(&mut tee, &packet.samples, state);" in writer
    assert "write_chip_ref_tee" not in run_alsa
    assert "diagnostic_tee_path" in state_rs
    assert "diagnostic_tee_active" in state_rs
    assert "diagnostic_tee_open_error_count" in state_rs
    assert "mark_chip_ref_tee_open_error" in main_rs


def test_outputd_optional_chip_reference_cannot_gate_dac_playback():
    main_rs = (REPO / "rust" / "jasper-outputd" / "src" / "main.rs").read_text()
    run_alsa = main_rs.split("fn run_alsa(", 1)[1].split("fn notify_ready", 1)[0]
    spawn = main_rs.split("fn spawn_chip_ref_writer(", 1)[1].split(
        "fn run_chip_ref_writer(", 1
    )[0]
    writer = main_rs.split("fn run_chip_ref_writer(", 1)[1].split(
        "fn open_chip_ref_pcm(", 1
    )[0]

    assert "ReferenceSideOutputs::new(config, shutdown, Arc::clone(state));" in run_alsa
    assert "ReferenceSideOutputs::new(config, shutdown, Arc::clone(state))?" not in run_alsa
    assert "ready_rx.recv_timeout" not in spawn
    assert "action=retry_background" in writer
    assert "state.mark_chip_ref_dropped_unavailable();" in writer
    assert "fn run_chip_ref_writer_with<" in main_rs
    assert "fn chip_ref_worker_degrades_then_recovers_without_exiting()" in main_rs


def test_outputd_ready_is_after_alsa_output_is_primed_and_started():
    main_rs = (REPO / "rust" / "jasper-outputd" / "src" / "main.rs").read_text()
    backend_rs = (
        REPO / "rust" / "jasper-outputd" / "src" / "alsa_backend.rs"
    ).read_text()
    main_fn = main_rs.split("fn main() -> Result<()> {", 1)[1].split(
        "fn run_fake(",
        1,
    )[0]
    run_alsa = main_rs.split("fn run_alsa(", 1)[1].split("fn notify_ready", 1)[0]
    sink_open = run_alsa.index("let mut sink = RuntimeAlsaSink::open(config)?;")
    primed = run_alsa.index(
        ".context(sink.prime_context())?;"
    )
    started = run_alsa.index("sink.start()?;")
    ready = run_alsa.index("notify_ready(config)?;")

    assert "notify(NotifyState::Ready)" not in main_fn
    assert "notify_ready(config)?" not in main_fn
    assert sink_open < primed < started < ready
    assert "swp.set_start_threshold(negotiated.buffer_frames as i64)" in backend_rs
    # `prime_periods` lives in `alsa_backend.rs`: the composite's post-recovery
    # re-prime must reach the SAME depth the startup prime reaches, and a second
    # copy of that arithmetic is a second chance to get the threshold wrong.
    assert "fn prime_periods(buffer_frames: u32, period_frames: u32) -> u32" in backend_rs
    assert "fn prime_periods(" not in main_rs
    assert "prime_periods," in main_rs.split("use jasper_outputd::alsa_backend::{", 1)[1]
    assert '"outputd.alsa.primed"' in main_rs


def test_outputd_dual_apple_ready_is_after_multi_period_prime_and_start():
    main_rs = (REPO / "rust" / "jasper-outputd" / "src" / "main.rs").read_text()
    sink_impl = main_rs.split("impl RuntimeAlsaSink", 1)[1].split(
        "fn run_alsa(",
        1,
    )[0]
    run_alsa = main_rs.split("fn run_alsa(", 1)[1].split("fn notify_ready", 1)[0]
    composite_open = sink_impl.index("SinkMode::Composite")
    paired_open = sink_impl.index("PairedCompositeSink::new(config)?")
    sink_open = run_alsa.index("let mut sink = RuntimeAlsaSink::open(config)?;")
    prime_count = run_alsa.index("let prime_periods = prime_periods(")
    prime_loop = run_alsa.index("for _ in 0..prime_periods")
    primed = run_alsa.index(".context(sink.prime_context())?;")
    started = run_alsa.index("sink.start()?;")
    ready = run_alsa.index("notify_ready(config)?;")

    assert composite_open < paired_open
    assert sink_open < prime_count < prime_loop < primed < started < ready
    assert '"outputd.dual_apple.primed"' in main_rs


def test_outputd_state_socket_is_bound_before_thread_spawn():
    main_rs = (REPO / "rust" / "jasper-outputd" / "src" / "main.rs").read_text()
    spawn_state = main_rs.split("fn spawn_state_server(", 1)[1].split(
        "fn period_duration(",
        1,
    )[0]
    bind = spawn_state.index("StateServer::bind(")
    spawn = spawn_state.index(".spawn(move ||")

    assert "StateServer::new" not in main_rs
    assert bind < spawn


def test_outputd_tts_runtime_is_bonded_scoped():
    """outputd's TTS server is scoped to bonded members: the retired API names
    never come back, and the runtime is construction-gated on the
    reconciler-set socket env, so a solo speaker never binds a TTS socket."""
    main_rs = (REPO / "rust" / "jasper-outputd" / "src" / "main.rs").read_text()
    state_rs = (REPO / "rust" / "jasper-outputd" / "src" / "state.rs").read_text()

    for retired in [
        "spawn_tts_client(",
        "TtsQueueMetrics",
        "mark_tts_command_dropped",
    ]:
        assert retired not in main_rs
        assert retired not in state_rs

    gate = main_rs.index("if let Some(path) = &config.tts_socket_path")
    spawn = main_rs.index("spawn_tts_server(")
    assert gate < spawn
    # The STATUS block tells the truth on a solo speaker: the tts
    # section's emitter writes enabled:false when no socket is set.
    tts_block = state_rs.index('"tts":{')
    disabled = state_rs.index(
        'push_kv_bool(&mut buf, "enabled", false);', tts_block
    )
    assert disabled > tts_block


# ---------------------------------------------------------------------------
# The playout thread's no-allocation contract.
# ---------------------------------------------------------------------------

# Functions that run once per DAC period on outputd's realtime playout thread,
# and the tokens that would put an allocator call in one of them.
#
# Static rather than runtime because a unit test cannot see an allocation that
# does not change a value — a review probe added a per-period Vec to
# `prepare_from_buffered_content` and the whole suite stayed green.
# `jasper-outputd.service` runs SCHED_FIFO at priority 35 with
# `LimitRTTIME=200000`, so a malloc blocking on another thread's arena lock is a
# priority inversion, then SIGXCPU, then `StartLimitAction=reboot`.
#
# `.resize(` is deliberately NOT in this list: four of these bodies use it
# behind an `if len != wanted` guard that is a no-op in steady state, the
# crate's documented pattern for a buffer sized once at open.
PERIOD_HOT_FUNCTIONS = [
    # (source file, enclosing `impl` block or None, function signature prefix)
    ("alsa_backend.rs", "impl AlsaBackend", "pub fn write_dac_period("),
    # The ring reader is the one upstream, and it runs on the same SCHED_FIFO
    # thread the writers do.
    ("shm_ring_source.rs", "impl ShmRingSource", "pub fn read_period("),
    # The composite's WRITE half and the split it drives: a per-width arm and a
    # type parameter are the natural place to "just allocate the child buffers
    # here" instead of reusing `ChildPeriods`. Same SCHED_FIFO thread, same
    # reboot-class consequence.
    ("alsa_backend.rs", "impl PairedCompositeSink", "pub fn write_dual_period("),
    # The per-period write body, split out of `write_dual_period` so the
    # post-recovery re-prime reuses the SAME A-then-B interleave; without this
    # entry the split moves the write body out from under the scan above.
    ("alsa_backend.rs", "impl PairedCompositeSink", "fn write_children("),
    ("alsa_backend.rs", None, "fn deinterleave_4ch_to_dual_stereo<"),
    # The packed-24 edge's conversion. Added with the S24_3LE write path: it is
    # the one edge whose staging is BYTES (`samples.len() * 3`), on the same
    # SCHED_FIFO thread. Listed by name because its length arithmetic makes a
    # local `vec![0u8; n]` look reasonable; its siblings are covered by callers.
    ("types.rs", None, "pub fn narrow_period_i24_le("),
    ("main.rs", "impl ReferenceSideOutputs", "fn publish("),
    ("core.rs", "impl OutputCore", "fn prepare_from_buffered_content("),
]

ALLOCATING_TOKENS = [
    "vec![",
    "mem::take(",
    ".to_vec()",
    ".to_owned()",
    "Vec::with_capacity(",
    ".clone()",
    # `.collect` WITHOUT parens on purpose: a turbofish
    # (`.collect::<Vec<_>>()`) does not match the parenthesised spelling.
    ".collect",
    "format!(",
    "String::from(",
    "Box::new(",
]


def _non_comment_rust(text: str) -> str:
    """Drop `//` line comments so a token NAMED in prose is not a violation."""
    return "\n".join(
        line for line in text.splitlines()
        if not line.lstrip().startswith("//")
    )


def _rust_fn_body(text: str, impl_block: str | None, signature: str) -> str:
    """Return the brace-matched body of `signature`, searched inside `impl_block`.

    Brace-matched rather than "until the next `fn`" so a nested closure or match
    cannot truncate the body and hide a token past it. String literals are
    blanked before counting: one unbalanced brace in a string (`let _brace =
    "}";`) closes the counter early and everything after it escapes the scan,
    which a review probe used to smuggle a per-period `vec![` past this guard.
    """
    start = 0
    if impl_block is not None:
        start = text.index(impl_block)
    sig_at = text.index(signature, start)
    open_at = text.index("{", sig_at)
    # Blank literals for COUNTING only; the slice comes from the original text.
    countable = _blank_string_literals(text)
    depth = 0
    for i in range(open_at, len(text)):
        c = countable[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[open_at : i + 1]
    raise AssertionError(f"unbalanced braces after {signature!r}")


def _blank_string_literals(text: str) -> str:
    """Replace every character inside a Rust string literal with a space,
    preserving the text's LENGTH so the caller's index slicing stays valid.

    Escapes are honoured so `"\\"` does not look like an unterminated literal.
    """
    out = list(text)
    in_string = False
    escaped = False
    for i, c in enumerate(text):
        if in_string:
            out[i] = " "
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                out[i] = '"'
                in_string = False
        elif c == '"':
            in_string = True
    return "".join(out)


def test_outputd_period_hot_functions_do_not_allocate():
    """No per-period allocation in the functions the playout thread runs.

    Every buffer these functions touch is sized once, at open, and reused. The
    one known exception is `ChipRefDownsampler::process`, which allocates a Vec
    per period on chip-reference-armed boxes only; fixing it changes the
    chip-ref queue's ownership model, so it is annotated at its definition.
    """
    cache: dict[str, str] = {}
    for filename, impl_block, signature in PERIOD_HOT_FUNCTIONS:
        if filename not in cache:
            cache[filename] = (
                REPO / "rust" / "jasper-outputd" / "src" / filename
            ).read_text()
        body = _rust_fn_body(cache[filename], impl_block, signature)
        # Guard against a silently-empty extraction: a body that matched nothing
        # would pass every assertion below.
        assert len(body) > 120, f"{signature} body looks truncated: {body!r}"
        stripped = _non_comment_rust(body)
        for token in ALLOCATING_TOKENS:
            assert token not in stripped, (
                f"{filename} {signature} allocates on the playout thread "
                f"({token!r}). outputd runs SCHED_FIFO with LimitRTTIME; a "
                f"per-period allocation is a priority-inversion -> SIGXCPU -> "
                f"reboot path. Size the buffer at open and reuse it."
            )


def test_the_no_allocation_guard_can_actually_fail():
    """The guard's own tripwire: prove it inspects a real body and would bite.

    Without this, a broken extractor (wrong anchor, empty body) would leave the
    guard above vacuously green — the exact failure mode it was written to close.
    """
    core_rs = (REPO / "rust" / "jasper-outputd" / "src" / "core.rs").read_text()
    body = _rust_fn_body(core_rs, "impl OutputCore", "fn prepare_from_buffered_content(")
    # A real body, positively identified by content the function must contain.
    assert "mix_saturating(" in body
    assert "observe_content_period(" in body
    # And the token scan is live: injecting one into a copy trips it.
    poisoned = _non_comment_rust(body.replace("let mix_stats", "let _x = vec![0i32; 8]; let mix_stats"))
    assert any(token in poisoned for token in ALLOCATING_TOKENS)

    # The known exception really is outside the guarded set, so the comment
    # above is not describing a function this test silently also covers.
    main_rs = (REPO / "rust" / "jasper-outputd" / "src" / "main.rs").read_text()
    chip = _rust_fn_body(main_rs, "impl ChipRefDownsampler", "fn process(")
    assert "Vec::with_capacity(" in chip
    assert "KNOWN ALLOCATION EXCEPTION" in main_rs

    # Same tripwire for the packed-24 entry: a `None` impl_block in a file the
    # guard reads nowhere else is where a silently-empty extraction would hide.
    types_rs = (REPO / "rust" / "jasper-outputd" / "src" / "types.rs").read_text()
    packed = _rust_fn_body(types_rs, None, "pub fn narrow_period_i24_le(")
    assert "narrow_i32_to_i24_le_slice(" in packed
    assert "I24_LE_BYTES_PER_SAMPLE" in packed
    poisoned = _non_comment_rust(
        packed.replace("if !jasper_resampler", "let _x = vec![0u8; 8]; if !jasper_resampler")
    )
    assert any(token in poisoned for token in ALLOCATING_TOKENS)


def test_the_packed_24_edge_writes_bytes_with_a_byte_frame_stride():
    """The `S24_3LE` arm's ALSA wiring, pinned at the source.

    Three facts, none reachable by a runnable test (they need a live PCM), each
    silently catastrophic if wrong:

    1. The handle is `io_bytes()`: alsa-rs has no `io_*` for a 3-byte format.
    2. The frame stride passed to the shared writer is `channels * 3`, not
       `channels`. `write_dac_frames` advances `frames_done *
       elements_per_frame` and one element here is a BYTE, so `channels` steps
       at a third of the right rate and re-sends most of every period.
    3. The stride parameter is not called `channels`. For the two typed edges
       the values are equal, so "simplifying" it back is invisible there.
    """
    backend = (REPO / "rust" / "jasper-outputd" / "src" / "alsa_backend.rs").read_text()

    writer = _rust_fn_body(backend, None, "fn write_dac_frames<")
    assert "elements_per_frame" in writer, (
        "write_dac_frames' frame stride must be named `elements_per_frame`: at the "
        "packed edge one element is a byte, not a sample"
    )
    assert "samples.len() / elements_per_frame" in _non_comment_rust(writer)
    assert "frames_done * elements_per_frame" in _non_comment_rust(writer)

    arm = _rust_fn_body(backend, "impl AlsaBackend", "pub fn write_dac_period(")
    code = _non_comment_rust(arm)
    # The packed arm exists, converts through the packed wrapper, and hands ALSA
    # bytes.
    assert "SampleFormat::S24_3Le =>" in code
    assert "narrow_period_i24_le(samples, &mut self.dac_pack_buf)" in code
    assert "self.dac.io_bytes()" in code
    # The byte stride, spelled from the shared constant rather than a literal 3.
    assert "channels * jasper_resampler::I24_LE_BYTES_PER_SAMPLE" in code, (
        "the packed arm must pass a BYTE frame stride to write_dac_frames"
    )
    # And the two typed arms still pass the bare channel count, so the stride
    # rename did not quietly change the S16/S32 edges.
    assert code.count("\n                    channels,\n") == 2, code
