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

from jasper.audio_hardware import dac
from jasper.tts_routing import (
    FANIN_TTS_SOCKET,
    OUTPUTD_TTS_SOCKET,
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
    reconcile = (REPO / "jasper" / "audio_hardware" / "reconcile.py").read_text()
    assert "select_audio_hardware_roles()" in install_sh
    assert "jasper-audio-hardware-reconcile\" --print-env" in install_sh
    # Classification is registry-backed and the shell holds no hardware label:
    # the classifier's env emitter names the Apple cards (ADR-0235 R2).
    assert "usb-c to 3.5mm" not in reconcile.lower()
    assert "find_card" not in reconcile
    assert "DAC8X_OUTPUT_CARD=" not in reconcile
    assert "DAC8X_STUDIO_OUTPUT_CARD=" not in reconcile
    assert "jasper_asound_render_template" in install_sh
    assert "asoundrc.jasper.source" in install_sh
    assert "JASPER_AUDIO_DAC_ID" in install_sh
    assert "JASPER_OUTPUT_DAC_ROUTE" not in reconcile
    assert "OUTPUT_DAC_ROUTE" not in install_without_env_migrations


def test_output_dac_route_policy_is_removed_from_renderer_and_reconciler():
    route_lib = (REPO / "deploy" / "lib" / "jasper-asound-render.sh").read_text()
    reconcile = (REPO / "jasper" / "audio_hardware" / "reconcile.py").read_text()
    assert "JASPER_OUTPUT_DAC_ROUTE" not in route_lib
    assert "OUTPUT_DAC_ROUTE" not in route_lib
    assert "mono:([1-8])" not in route_lib
    assert "stereo:([1-8]),([1-8])" not in route_lib
    assert "type route" not in route_lib
    assert 'OUTPUT_DAC_ID:-}" == "dual_apple_usb_c_dac_4ch"' in route_lib
    assert "type null" in route_lib
    assert "jasper_asound_route_ignored()" not in reconcile


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
    tmp_path: Path,
    bin_dir: Path,
    board: dict[str, str],
    *,
    card: str = "auto",
    control: str | None = None,
) -> tuple[subprocess.Popen[bytes], Path]:
    """The drift monitor plus its journal, reading `board` through the
    classifier's own seams. Argv matches the unit's: the card alone, so the
    control under test is the one the emitter names."""
    journal = tmp_path / "monitor.log"
    return (
        subprocess.Popen(
            [
                "/bin/bash",
                str(REPO / "deploy" / "bin" / "jasper-headphone-monitor"),
                card, *([control] if control is not None else []),
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


def _await(
    monitor: subprocess.Popen[bytes], log: Path, expected: tuple[str, ...]
) -> None:
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


def _event_fields(line: str) -> dict[str, str]:
    """One `event=... key=value` journal line as its fields."""
    return dict(token.split("=", 1) for token in line.split() if "=" in token)


def _empty_board(tmp_path: Path) -> dict[str, str]:
    sys_class = tmp_path / "sys" / "class" / "sound"
    proc_asound = tmp_path / "proc" / "asound"
    sys_class.mkdir(parents=True)
    proc_asound.mkdir(parents=True)
    return {
        "JASPER_SYS_CLASS_SOUND": str(sys_class),
        "JASPER_PROC_ASOUND": str(proc_asound),
    }


def test_the_boot_pin_and_the_drift_monitor_resolve_their_card_at_runtime():
    """Neither helper may carry a card id — or a mixer control name — baked in
    at install time. The monitor unit renders `<helper> auto` and resolves both
    the dongle and the control it pins on every start; the boot pin takes no
    argument at all — it reads the card off the reconciler's record, and is
    ordered after alsa-restore so a restored snapshot cannot outrun the pin."""
    init_unit = (REPO / "deploy" / "systemd" / "jasper-dac-init.service").read_text()
    monitor_unit = (
        REPO / "deploy" / "systemd" / "jasper-headphone-monitor.service"
    ).read_text()
    assert "ExecStart=/usr/local/bin/jasper-dac-init\n" in init_unit
    assert "After=sound.target alsa-restore.service" in init_unit
    assert "__APPLE_DONGLE_CARD__" not in init_unit
    assert (
        "ExecStart=/usr/local/bin/jasper-headphone-monitor __APPLE_DONGLE_CARD__\n"
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
    card directly and never asks the classifier, so no Python is needed — and
    naming the card is therefore also naming the control on it."""
    bin_dir, log = _amixer_double(tmp_path)
    monitor, _ = _start_monitor(
        tmp_path,
        bin_dir,
        _empty_board(tmp_path),
        card="Dongle_1",
        control="Headphone",
    )
    try:
        _await(monitor, log, ("-c Dongle_1 sset Headphone 100% unmute",))
    finally:
        monitor.kill()
        monitor.wait()


def test_the_drift_monitor_refuses_an_explicit_card_with_no_control(tmp_path):
    """The override skips the emitter, so it carries no control either: naming
    a card without one leaves nothing to pin, and the monitor has to say so and
    exit rather than poll a control name it never resolved."""
    result = subprocess.run(
        [
            "/bin/bash",
            str(REPO / "deploy" / "bin" / "jasper-headphone-monitor"),
            "Dongle_1",
        ],
        env={**os.environ, **_empty_board(tmp_path)},
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 1
    fields = _event_fields(result.stderr.strip())
    assert fields["event"] == "apple_dongle.headphone_monitor.failed"
    assert fields["reason"] == "control_required"


def test_the_drift_monitor_stays_up_and_re_asks_when_a_card_appears(tmp_path):
    """The monitor is enabled on boxes whose dongle comes and goes, and
    jasper-audio-hardware-reconcile never re-execs it (a restart per pass burns
    `StartLimitBurst`). So an absent dongle must be a poll, never an exit, and
    the card set must be re-asked when the board's cards move."""
    bin_dir, log = _amixer_double(tmp_path)
    monitor, journal = _start_monitor(
        tmp_path,
        bin_dir,
        _empty_board(tmp_path),
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
            "auto",
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
    reconcile = (REPO / "jasper" / "audio_hardware" / "reconcile.py").read_text()
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
    # The kernel prints the USB uevent as `PRODUCT=%x/%x/%x`, so the vendor id
    # arrives unpadded: a second `05ac` spelling matches nothing and only
    # doubles the RUN if the format ever changes under it.
    apple_matches = [
        line
        for line in rule.splitlines()
        if "ENV{PRODUCT}" in line and not line.lstrip().startswith("#")
    ]
    assert apple_matches == [
        'ACTION=="remove", SUBSYSTEM=="usb", ENV{PRODUCT}=="5ac/110a/*", '
        'RUN+="/usr/local/sbin/jasper-output-hardware-hotplug"'
    ]
    assert 'RUN+="/usr/local/sbin/jasper-output-hardware-hotplug"' in rule
    hotplug = (REPO / "deploy" / "bin" / "jasper-output-hardware-hotplug").read_text()
    assert "--no-block start jasper-audio-hardware-reconcile.service" in hotplug
    assert "event=audio_hardware_hotplug.reconcile_requested" in hotplug
    assert "/usr/local/sbin/jasper-audio-hardware-reconcile --reason install" in install_sh
    # The cutover gate is width-aware and shared by the composite + single
    # active paths, and trusts the durable runtime contract rather than
    # transient startup-load state: a saved active baseline must stay playable
    # after setup completes.
    assert "active_graph_width_out_of_range" in runtime_contract
    assert "JASPER_ACTIVE_SPEAKER_STARTUP_LOAD_STATE" not in reconcile
    assert "AUDIO_HARDWARE_RECONCILE_UNIT" in startup_load
    assert "_trigger_audio_hardware_reconcile(source=\"active_speaker_startup_load\")" in startup_load
    assert "_trigger_audio_hardware_reconcile(source=\"active_speaker_startup_rollback\")" in startup_load


def test_install_alsa_refreshes_asound_renderer_before_rendering():
    """install_alsa renders /etc/asound.conf through the renderer lib install.sh
    sources from the checkout; the on-box copy the runtime reconciler falls back
    to is the support-file install's, pinned by the destination-set harness in
    tests/test_install_core_audio_graph_loop.py."""
    install_sh = installer_text()
    start = install_sh.index("install_alsa() {")
    end = install_sh.index("\nwrite_build_manifest() {", start)
    install_alsa = install_sh[start:end]
    source_template_install = install_alsa.index("asoundrc.jasper.source")
    render_call = install_alsa.index("jasper_asound_render_template")
    assert 'source "${REPO_DIR}/deploy/lib/jasper-asound-render.sh"' in install_sh
    assert source_template_install < render_call


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
    assert bonded["JASPER_GROUPING_VOICE_PARK"] == "1"

    # The unit owns these names; the reconciler must not become a second writer.
    reconcile = (REPO / "jasper" / "audio_hardware" / "reconcile.py").read_text()
    assert "TTS_ENV_FILE" not in reconcile
    assert VOICE_TTS_SOCKET_ENV not in reconcile


def test_fanin_tts_socket_default_matches_the_python_constant():
    """fan-in bakes its assistant-TTS socket path as a Rust default; every
    Python consumer resolves ``jasper.tts_routing.FANIN_TTS_SOCKET``. Rust owns
    the value and Python mirrors it, so the two owners are compared here once.

    outputd's twin has no baked default — it binds only when the grouping
    reconciler sets ``JASPER_OUTPUTD_TTS_SOCKET`` — so there the Python
    constant IS the owner, and what the reconciler writes is pinned by
    ``tests/test_multiroom_rate_adjust.py``.
    """
    config_rs = (REPO / "rust" / "jasper-fanin" / "src" / "config.rs").read_text()
    assert f'"{FANIN_TTS_SOCKET}"' in config_rs, (
        f"jasper-fanin no longer defaults its TTS socket to {FANIN_TTS_SOCKET} "
        "— jasper.tts_routing.FANIN_TTS_SOCKET must move with it"
    )


def test_camilla_outputd_config_declares_outputd_lane():
    cutover = (REPO / "deploy" / "camilladsp" / "outputd-cutover.yml").read_text()
    camilla_unit = (REPO / "deploy" / "systemd" / "jasper-camilla.service").read_text()
    # Ring B: the one lane outputd reads (ADR-0100).
    assert 'device: "jts_ring_playback"' in cutover
    assert 'volume_limit: 0.0' in cutover
    # outputd's OWN statefile, never /var/lib/camilladsp/statefile.yml.
    assert "--statefile /var/lib/camilladsp/outputd-statefile.yml" in camilla_unit


def _emit_shipped_cutover_config(monkeypatch, tmp_path) -> str:
    """The emitter call the shipped seed must match, made deterministic.

    ``emit_flat_outputd_cutover_config()`` resolves the ring wire through
    ``read_declared_ring_wire_format`` — a FILE-FRESH read of
    ``/var/lib/jasper/fanin.env`` then ``/etc/jasper/jasper.env`` with no
    parameter seam — and, with no ``topology`` passed, loads the saved
    topology from ``JASPER_OUTPUT_TOPOLOGY_PATH``/the default path. Neither
    is in conftest's ``_isolate_host_state_paths`` allowlist, so on a roleful
    box (one with a real fanin.env or saved topology) this call would emit
    THAT box's wire/topology rather than the plain flat-stereo identity graph
    the shipped seed is. Point both at absent tmp paths so the result is the
    hermetic default everywhere, laptop or roleful box alike.
    """
    monkeypatch.setattr("jasper.env_load.FANIN_ENV_PATH", str(tmp_path / "fanin.env"))
    monkeypatch.setattr("jasper.env_load.BASE_ENV_PATH", str(tmp_path / "jasper.env"))
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(tmp_path / "topology.json"))

    from jasper.sound.camilla_yaml import emit_flat_outputd_cutover_config

    return emit_flat_outputd_cutover_config()


def test_shipped_cutover_seed_declares_the_current_program_lane_width(
    monkeypatch, tmp_path,
):
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
    from jasper.camilla_config_contract import parse_camilla_devices_config
    from jasper.fanin_coupling import DEFAULT_PLAYBACK_FORMAT

    cutover = REPO / "deploy" / "camilladsp" / "outputd-cutover.yml"
    emitted = parse_camilla_devices_config(
        _emit_shipped_cutover_config(monkeypatch, tmp_path)
    )
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


def test_shipped_cutover_seed_is_byte_identical_to_the_emitter(monkeypatch, tmp_path):
    """The shipped fallback must equal the emitter's own bytes, in full.

    The field-by-field check above only pins the ``devices:`` block. Nothing
    pinned ``filters:``/``mixers:``/``pipeline:`` or the header comment, so
    #4369's header reword drifted from this file unnoticed and the emitter's
    filter chain (every `sound_*` identity stage, kept present so a live EQ
    patch never needs a pipeline reload) is not in the shipped copy at all.
    install.sh always re-renders this file at deploy time (`_render_
    outputd_cutover_configs`), so a real box never plays the shipped bytes —
    but the checked-in copy is what a reader, or a box whose render step
    failed, actually sees. Regenerate deliberately, after review, with a
    fanin.env/jasper.env/topology pointed at absent paths — NOT a bare call —
    so a regen on a roleful box cannot bake that box's wire format or saved
    topology (its mutes) into the shipped seed::

        PYTHONPATH=$PWD .venv/bin/python -c \\
            "import os; \\
             os.environ['JASPER_OUTPUT_TOPOLOGY_PATH'] = '/tmp/absent-topology.json'; \\
             import jasper.env_load as e; \\
             e.FANIN_ENV_PATH = '/tmp/absent-fanin.env'; \\
             e.BASE_ENV_PATH = '/tmp/absent-jasper.env'; \\
             from jasper.sound.camilla_yaml import emit_flat_outputd_cutover_config as g; \\
             open('deploy/camilladsp/outputd-cutover.yml', 'w').write(g())"
    """
    cutover = REPO / "deploy" / "camilladsp" / "outputd-cutover.yml"
    assert cutover.read_text(encoding="utf-8") == _emit_shipped_cutover_config(
        monkeypatch, tmp_path
    )


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
