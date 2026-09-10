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
PINNED_HEADPHONE_STATE = "  Front Left: Playback 120 [100%] [0.00dB] [on]"


def _amixer_double(
    tmp_path: Path, *, state: str = DRIFTED_HEADPHONE_STATE,
) -> tuple[Path, Path]:
    """An `amixer` that records its argv and reports whatever
    `bin_dir/amixer.state` holds, plus the `alsactl monitor` the drift
    monitor blocks on — its events are lines appended to
    `bin_dir/alsactl.events`."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = bin_dir / "amixer.log"  # absent until amixer is actually invoked
    reported = bin_dir / "amixer.state"
    reported.write_text(state + "\n", encoding="utf-8")
    (bin_dir / "amixer").write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> {shlex.quote(str(log))}\n'
        f"cat {shlex.quote(str(reported))}\n"
    )
    (bin_dir / "amixer").chmod(0o755)
    events = bin_dir / "alsactl.events"
    events.write_text("", encoding="utf-8")
    started = bin_dir / "alsactl.log"  # one line per monitor opened
    started.write_text("", encoding="utf-8")
    (bin_dir / "alsactl").write_text(
        "#!/usr/bin/env bash\n"
        '[[ "$1" == "monitor" ]] || exit 0\n'
        f'printf "%s\\n" "$*" >> {shlex.quote(str(started))}\n'
        f"exec tail -n +1 -f {shlex.quote(str(events))}\n"
    )
    (bin_dir / "alsactl").chmod(0o755)
    return bin_dir, log


def _start_monitor(
    tmp_path: Path,
    bin_dir: Path,
    board: dict[str, str],
    *,
    card: str = "auto",
    control: str | None = None,
    capture_stderr: bool = False,
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
            stderr=subprocess.STDOUT if capture_stderr else subprocess.DEVNULL,
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
        # `alsactl monitor` takes exactly one <card>, so each armed card gets
        # its OWN scoped process -- never a bare, unscoped `monitor` that
        # would in practice only ever see card 0 (#4772 review). The
        # per-card processes are backgrounded from a process-substitution
        # subshell, so their own argv-logging line can lag the reset that
        # `_await` above already waited for -- poll for it too.
        deadline = time.monotonic() + 30.0
        started: list[str] = []
        while time.monotonic() < deadline:
            started = (bin_dir / "alsactl.log").read_text().splitlines()
            if sorted(started) == ["monitor hw:A", "monitor hw:A_1"]:
                break
            assert monitor.poll() is None, f"monitor exited; alsactl.log: {started!r}"
            time.sleep(0.02)
        assert sorted(started) == ["monitor hw:A", "monitor hw:A_1"]
    finally:
        monitor.kill()
        monitor.wait()


def test_the_drift_monitor_reads_the_control_only_when_an_event_says_so(
    tmp_path,
):
    """The 1 Hz `amixer` poll is gone: nothing re-reads the control until
    `alsactl monitor` reports it moved (#4121). Starting pinned at 100 %
    there is nothing to heal, so the reset that follows an event line — with
    exactly one read per card before it — is the loop waking on the event and
    not on a clock."""
    bin_dir, log = _amixer_double(tmp_path, state=PINNED_HEADPHONE_STATE)
    monitor, journal = _start_monitor(
        tmp_path, bin_dir, _dual_apple_cards(tmp_path),
    )
    try:
        _await(
            monitor,
            journal,
            (
                "event=apple_dongle.headphone_monitor.state card=A ",
                "event=apple_dongle.headphone_monitor.state card=A_1 ",
            ),
        )
        assert "sset" not in log.read_text()  # already at 100 %, nothing to do

        # Several times the 1 s poll this replaced. A slow machine can only
        # make this pass for the wrong reason, never fail: the event-driven
        # loop reads nothing while idle however long it waits.
        time.sleep(3.0)
        assert log.read_text().count("sget") == 2  # one per card, at start

        (bin_dir / "amixer.state").write_text(
            DRIFTED_HEADPHONE_STATE + "\n", encoding="utf-8"
        )
        (bin_dir / "alsactl.events").write_text(
            "node hw:0,0,0 value|info\n", encoding="utf-8"
        )

        _await(monitor, log, _BOTH_APPLE_PINS)
        assert log.read_text().count("sget") == 4  # one per card, twice
    finally:
        monitor.kill()
        monitor.wait()


def _await_count(path: Path, n: int, monitor: subprocess.Popen[bytes]) -> float:
    """Block until the decimal counter at `path` reaches at least `n`,
    returning the `time.monotonic()` it did. Only a hang backstop -- the
    caller compares two returned timestamps, never this deadline (#3092)."""
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        text = path.read_text().strip() if path.exists() else ""
        if text.isdigit() and int(text) >= n:
            return time.monotonic()
        code = monitor.poll()
        if code is not None:
            raise AssertionError(f"monitor exited with {code} awaiting count {n}")
        time.sleep(0.02)
    raise AssertionError(f"count at {path} never reached {n}")


def test_the_drift_monitor_backoff_grows_caps_then_decays_after_a_full_window(
    tmp_path,
):
    """`alsactl monitor` exits when a card it holds is removed, or never
    connects at all (a permission problem, say). Falling back to polling for
    good would leave a dongle plugged back in unwatched, so the stream is
    re-opened instead, behind a backoff that grows and is capped so a
    monitor that cannot start would not spin (#4121) -- and decays back to
    base once the reopened stream has proven itself connected through one
    full read window, rather than staying inflated by an old failure
    forever (#4772 review). The base/cap are overridable env vars, a test
    seam documented in the script header, so this asserts real elapsed time
    without waiting out the 5 s production cap and fails if a sleep is
    optimized away.
    """
    bin_dir, log = _amixer_double(tmp_path, state=PINNED_HEADPHONE_STATE)
    counter = bin_dir / "alsactl.count"
    counter.write_text("0", encoding="utf-8")
    starts = bin_dir / "alsactl.starts"
    starts.write_text("", encoding="utf-8")
    (bin_dir / "alsactl").write_text(
        "#!/usr/bin/env bash\n"
        '[[ "$1" == "monitor" ]] || exit 0\n'
        f'n=$(cat {shlex.quote(str(counter))})\n'
        f'printf "%s" $((n + 1)) > {shlex.quote(str(counter))}\n'
        f'printf "%s\\n" "$*" >> {shlex.quote(str(starts))}\n'
        # Invocations 1 and 2 fail to open the control device at all
        # (exec_failed); invocation 3 connects and stays up -- long enough
        # to span one full MONITOR_POLL_SEC read window before it, too,
        # exits cleanly (eof), the way a card removal would end it.
        'if [[ "$n" -lt 2 ]]; then\n'
        "  exit 1\n"
        "fi\n"
        "sleep 7\n"
        "exit 0\n"
    )
    (bin_dir / "alsactl").chmod(0o755)

    monitor, journal = _start_monitor(
        tmp_path,
        bin_dir,
        {
            "JASPER_HEADPHONE_MONITOR_BACKOFF_BASE_SEC": "1",
            "JASPER_HEADPHONE_MONITOR_BACKOFF_CAP_SEC": "3",
        },
        card="Dongle_1",
        control="Headphone",
        capture_stderr=True,
    )
    try:
        t1 = _await_count(counter, 1, monitor)
        t2 = _await_count(counter, 2, monitor)  # after the base (1 s) sleep
        t3 = _await_count(counter, 3, monitor)  # after growth capped at 3 s
        assert 0.4 <= (t2 - t1) <= 2.2, "base backoff sleep was skipped or wrong"
        assert 1.3 <= (t3 - t2) <= 4.2, "grown/capped backoff sleep was skipped or wrong"

        # Invocation 3's `sleep 7` outlives one 5 s read window while
        # connected, decaying the backoff back to base before it exits.
        t4 = _await_count(counter, 4, monitor)
        elapsed_after_death = t4 - (t3 + 7.0)
        assert 0.2 <= elapsed_after_death <= 2.2, (
            "post-decay backoff was not back near base "
            f"(observed {elapsed_after_death:.2f}s, expected ~1s not ~3s)"
        )

        mode_lines = [
            line for line in journal.read_text().splitlines()
            if "headphone_monitor.mode" in line
        ]
        assert mode_lines == [
            "event=apple_dongle.headphone_monitor.mode mode=poll reason=exec_failed",
            "event=apple_dongle.headphone_monitor.mode mode=monitor reason=-",
            "event=apple_dongle.headphone_monitor.mode mode=poll reason=eof",
        ]
    finally:
        monitor.kill()
        monitor.wait()


def test_the_drift_monitor_falls_back_to_polling_without_alsactl(tmp_path):
    """A box with no `alsactl` at all (a stripped image, a PATH problem) must
    still self-heal: mode is poll from the first pass, reason=not_installed,
    logged once, and sweeps keep happening on the MONITOR_POLL_SEC clock so
    drift is still caught (#4772 review)."""
    bin_dir, log = _amixer_double(tmp_path)
    # `_amixer_double` seeds a working fake `alsactl` too; remove it so
    # `command -v alsactl` genuinely fails, same as a stripped image.
    (bin_dir / "alsactl").unlink()

    monitor, journal = _start_monitor(
        tmp_path, bin_dir, {}, card="Dongle_1", control="Headphone",
    )
    try:
        _await(
            monitor,
            journal,
            ("event=apple_dongle.headphone_monitor.mode mode=poll reason=not_installed",),
        )
        _await(monitor, log, ("-c Dongle_1 sset Headphone 100% unmute",))
        # A second `sget` well after one MONITOR_POLL_SEC proves the poll
        # loop is still alive and re-sweeping on its clock, not stuck after
        # the first pass -- the fake amixer never un-drifts on its own, so
        # `sset` fires only the once (sweep_cards only reacts to an observed
        # state CHANGE, and this one never changes) while `sget` keeps
        # ticking every poll.
        deadline = time.monotonic() + 20.0
        while log.read_text().count("sget") < 3:
            assert time.monotonic() < deadline, "poll fallback never swept again"
            code = monitor.poll()
            assert code is None, f"monitor exited with {code}"
            time.sleep(0.05)
        assert journal.read_text().count(
            "mode=poll reason=not_installed"
        ) == 1
    finally:
        monitor.kill()
        monitor.wait()


def test_the_drift_monitor_floors_how_often_it_reads_and_resets(tmp_path):
    """The reset writes a control, and the monitor hears about that write like
    any other. A control that keeps moving would otherwise put the two in a
    fight at event rate; the `sleep 1` poll this replaced capped resets at
    one a second (#4121) -- and the review widened that same 1/s floor to
    the `sget` reads themselves, coalescing a burst of events into one sweep
    instead of one read per event (#4772 review)."""
    bin_dir, log = _amixer_double(tmp_path)
    state = shlex.quote(str(bin_dir / "amixer.state"))
    (bin_dir / "amixer").write_text(  # a control that moves on every read
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> {shlex.quote(str(log))}\n'
        '[[ "$*" == *sget* ]] || exit 0\n'
        f"cur=$(cat {state})\n"
        'if [[ "$cur" == *"[100%]"* ]]; then\n'
        f"  printf '%s\\n' {shlex.quote(DRIFTED_HEADPHONE_STATE)} > {state}\n"
        "else\n"
        f"  printf '%s\\n' {shlex.quote(PINNED_HEADPHONE_STATE)} > {state}\n"
        "fi\n"
        'printf "%s\\n" "$cur"\n'
    )
    (bin_dir / "amixer").chmod(0o755)
    (bin_dir / "alsactl").write_text(
        "#!/usr/bin/env bash\n"
        '[[ "$1" == "monitor" ]] || exit 0\n'
        "while :; do printf 'node hw:0,0,0 value\\n'; sleep 0.01; done\n"
    )
    (bin_dir / "alsactl").chmod(0o755)

    monitor, _ = _start_monitor(
        tmp_path,
        bin_dir,
        _empty_board(tmp_path),
        card="Dongle_1",
        control="Headphone",
    )
    try:
        _await(monitor, log, ("-c Dongle_1 sset Headphone 100% unmute",))
        time.sleep(3.5)
    finally:
        monitor.kill()
        monitor.wait()

    calls = log.read_text()
    sget_count = calls.count("sget")
    sset_count = calls.count("sset")
    # The fake control moves on literally every read (~100/s), so an
    # unfloored loop would show hundreds of `sget`s over 3.5 s; floored to
    # ~1/s it stays in the single digits -- proof the burst was coalesced,
    # not read once per event.
    assert 1 <= sget_count <= 8, sget_count
    assert 1 <= sset_count <= sget_count, (sset_count, sget_count)


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

        # Each card gets `alsactl monitor` scoped to it (`alsactl monitor`
        # takes exactly one <card>), so a dongle plugged in later joins the
        # watched set only because the moved population re-opens the stream
        # with a process per card -- not none (the fallback poll would find
        # it too, slowly) and not one per event.
        (bin_dir / "amixer.state").write_text(
            PINNED_HEADPHONE_STATE + "\n", encoding="utf-8"
        )
        with (bin_dir / "alsactl.events").open("a", encoding="utf-8") as events:
            events.write("node hw:0,0,0 value|info\n")

        _await(
            monitor,
            journal,
            (
                "event=apple_dongle.headphone_monitor.change card=A "
                "control=Headphone from=[67%]_[-20.00dB]_[on] "
                "to=[100%]_[0.00dB]_[on]",
            ),
        )
        assert sorted(
            (bin_dir / "alsactl.log").read_text().splitlines()
        ) == ["monitor hw:A", "monitor hw:A_1"]
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
