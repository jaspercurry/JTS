# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from jasper import wake_legs
from jasper.chip_aec import health as chip_aec_health
from jasper.accessories.constants import WIIM_REMOTE_2_MIC_DEVICE
from jasper.audio_profile_state import ALL_PROFILES, profile_env_updates
from jasper.chip_aec.health import AlignmentHealth, alignment_health
from jasper.cli import aec_init
from jasper.env_load import parse_env_file
from jasper.control import aec_endpoints
from jasper.mics import xvf3800
from jasper.multiroom.tts_route import VOICE_PARK_ENV
from jasper.tts_routing import OUTPUTD_TTS_SOCKET, VOICE_TTS_SOCKET_ENV
from jasper.usb_mic import (
    USB_MIC_RAW_XVF_LEG,
    read_usb_mic_leg,
    usb_mic_enabled,
    write_usb_mic_enabled,
    write_usb_mic_leg,
)
from jasper.voice.catalog import VALID_PROVIDER_IDS, provider_ids_manifest_text
from tests.reconcile_fixtures import (
    fake_systemctl as _fake_systemctl,
    systemctl_log as _systemctl_log,
)
from tests.status_socket_fixtures import JsonStatusSocket


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "bin" / "jasper-aec-reconcile"
VOICE_RESTART_CMD = "--no-block restart jasper-voice.service"

# The registry constants `jasper-xvf-profile --env` publishes (ADR-0235), read
# from the module the emitter reads so a resolver double cannot drift from the
# registry it stands in for.
_REGISTRY_ENV: tuple[tuple[str, str], ...] = (
    ("JASPER_XVF_SUPPORTED_ALSA_CARDS", ",".join(xvf3800.ALSA_CARD_NAMES)),
    (
        "JASPER_XVF_RECOMMENDED_CHANNELS",
        str(xvf3800.RECOMMENDED_CAPTURE_CHANNELS),
    ),
    ("JASPER_XVF_MIXER_CAPTURE_SWITCH", xvf3800.MIXER_CAPTURE_SWITCH),
    ("JASPER_XVF_MIXER_CAPTURE_VOLUME", xvf3800.MIXER_CAPTURE_VOLUME),
    ("JASPER_XVF_MIXER_VOLUME_MAX", str(xvf3800.MIXER_VOLUME_MAX)),
)
# printf arguments for the resolver doubles below. Doubly quoted on purpose:
# the inner quote is the emitter's own (the reconciler evals the line), the
# outer one is for the double's own shell.
_REGISTRY_ENV_ARGS = " ".join(
    shlex.quote(f"{key}={shlex.quote(value)}") for key, value in _REGISTRY_ENV
)
# The subset write_mic_profile_env re-publishes into jasper.env, so a staged
# env file looks like one an earlier pass wrote.
_PERSISTED_REGISTRY_ENV = "".join(
    f"{key}={value}\n"
    for key, value in _REGISTRY_ENV
    if key in (
        "JASPER_XVF_SUPPORTED_ALSA_CARDS",
        "JASPER_XVF_RECOMMENDED_CHANNELS",
    )
)


def _control_leg_defaults() -> dict[str, str]:
    """Return control's missing-key defaults in systemd-env form."""
    return {
        "JASPER_WAKE_LEG_RAW": "1" if aec_endpoints._LEG_DEFAULT_RAW else "0",
        "JASPER_WAKE_LEG_DTLN": "1" if aec_endpoints._LEG_DEFAULT_DTLN else "0",
        "JASPER_WAKE_LEG_CHIP_AEC": (
            "1" if aec_endpoints._LEG_DEFAULT_CHIP_AEC else "0"
        ),
        "JASPER_WAKE_LEG_CHIP_AEC_150": (
            "1" if aec_endpoints._LEG_DEFAULT_CHIP_AEC_150 else "0"
        ),
        "JASPER_WAKE_LEG_CHIP_AEC_210": (
            "1" if aec_endpoints._LEG_DEFAULT_CHIP_AEC_210 else "0"
        ),
    }


def _unit_command_indices(
    lines: list[str], verb: str, unit: str
) -> list[int]:
    """Positions of `systemctl <verb> ... <unit> ...` in the fake's log.

    systemctl takes many units per invocation, so the reconciler's teardown
    helpers batch them; match on the verb plus the unit rather than on one
    exact argument string.
    """
    return [
        index
        for index, line in enumerate(lines)
        if line.split()[:1] == [verb] and unit in line.split()[1:]
    ]


# deploy/lib/jasper-env-file.sh writes an empty value as a quoted empty
# string, never a bare `KEY=`.
_EMPTY = "''"


def _env_assignments(path: Path) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line and not line.lstrip().startswith("#") and "=" in line
    )


def _event_values(stderr: str, event: str, field: str) -> list[str]:
    """Every `field=` value, one per stderr line carrying `event=<event>`.

    Captures up to the next `key=` token or end of line, never to the next
    space alone: several of this reconciler's fields carry embedded spaces
    (absence-marker `reason=` prose, ALSA mixer control names), so `field`
    need not be the last key=value pair on the line. Same anchor-on-the-next-
    known-field idiom the pass-summary `candidates=(.*?) legs=` pin already
    relies on (ADR-0235 PR 12).
    """
    return re.findall(
        rf"^.*\bevent={re.escape(event)}(?= |$).*\b{re.escape(field)}=(.*?)(?= \S+=|$)",
        stderr,
        flags=re.MULTILINE,
    )


def _alignment_record(tmp_path: Path) -> Path:
    """Where jasper-aec-init publishes the verdict for the pass it ran."""
    return tmp_path / "alignment"


def _publish_record(tmp_path: Path, health: AlignmentHealth) -> None:
    """Leave a record behind from a pass that already finished.

    The reconciler clears the record before it restarts jasper-aec-init, so
    this is the leftover of an EARLIER pass, never the verdict of the one under
    test — for that, see `_publishing_init_systemctl`.
    """
    _alignment_record(tmp_path).write_text(health.to_shell(), encoding="utf-8")


def _publishing_init_systemctl(tmp_path: Path, health: AlignmentHealth) -> Path:
    """A systemctl double whose jasper-aec-init restart publishes `health` and
    then succeeds — the shape of the real oneshot, which writes its record
    during the run the reconciler is waiting on.
    """
    return _systemctl_double(
        tmp_path,
        "init-publishes-systemctl",
        "if [[ \"$*\" == 'restart jasper-aec-init.service' ]]; then\n"
        "cat > \"$JASPER_AEC_ALIGNMENT_RECORD_FILE\" <<'JTSRECORD'\n"
        f"{health.to_shell()}"
        "JTSRECORD\n"
        "fi\n",
    )


def _fake_mixer_tools(tmp_path: Path, failing: str = "") -> tuple[Path, Path]:
    """A logging amixer/alsactl double. `failing` (amixer or alsactl), if
    given, exits 1 after logging — a fake that fails on demand, so
    ensure_capture_mixer_open's per-invocation event=aec_reconcile.mixer_repair
    is exercised without a real mixer."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "mixer.log"
    # argv is logged PIPE-joined, never space-joined: the XVF capture mixer
    # control names carry spaces, so a space-joined log cannot tell one
    # argument from three and would read as covered over a quoting regression.
    for name in ("amixer", "alsactl"):
        exit_code = 1 if name == failing else 0
        executable = bin_dir / name
        executable.write_text(
            "#!/usr/bin/env bash\n"
            "IFS='|'\n"
            "printf '%s|%s\\n' \"${0##*/}\" \"$*\" >> \"$JASPER_MIXER_LOG\"\n"
            f"exit {exit_code}\n"
        )
        executable.chmod(0o755)
    return bin_dir, log


def _run_reconcile(
    tmp_path: Path,
    *args: str,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the reconciler against a tmp_path-rooted copy of every file, marker
    and unit-control surface it reads or writes. Nothing here may reach the
    host's real /var/lib/jasper, /etc/jasper or /run.

    Absent-by-default is deliberate for four of them: no alignment record
    (so a pass reads "fully ready"), no accessory-mics.env (so a pass starts
    from "no accessory microphone"), and no install manifest or restart stamp
    (so a pass that does not opt in keeps the unconditional voice restart —
    an unprovable build is a restart, see installed_build_matches_stamp).
    """
    fake_systemctl, systemctl_log = _fake_systemctl(tmp_path)
    env = os.environ.copy()
    # The reconciler falls back to $JASPER_VOICE_PROVIDER when the provider
    # file says nothing, and CI exports it so jasper.config loads. Drop it, or
    # the provider-park cases see a valid provider and never park.
    env.pop("JASPER_VOICE_PROVIDER", None)
    env.update(
        {
            "JASPER_ENV_FILE": str(tmp_path / "jasper.env"),
            "JASPER_AEC_MODE_FILE": str(tmp_path / "aec_mode.env"),
            "JASPER_VOICE_PROVIDER_FILE": str(tmp_path / "voice_provider.env"),
            "JASPER_VOICE_PROVIDER_IDS_FILE": str(tmp_path / "voice_provider_ids"),
            "JASPER_GROUPING_VOICE_ENV_FILE": str(
                tmp_path / "grouping-voice.env"
            ),
            "JASPER_ASOUND_ROOT": str(tmp_path / "asound"),
            "JASPER_MIC_PROFILE_STATE_PATH": str(tmp_path / "xvf3800.json"),
            "JASPER_AEC_COMMISSION_MARKER": str(
                tmp_path / "chip-aec-commission-active"
            ),
            "JASPER_PROC_ROOT": str(tmp_path / "proc"),
            "JASPER_VOICE_INPUT_ABSENT_MARKER": str(
                tmp_path / "voice-input-absent"
            ),
            "JASPER_AEC_ALIGNMENT_RECORD_FILE": str(_alignment_record(tmp_path)),
            "JASPER_ACCESSORY_MIC_ENV_FILE": str(
                tmp_path / "accessory-mics.env"
            ),
            "JASPER_INSTALL_MANIFEST": str(tmp_path / "build.txt"),
            "JASPER_VOICE_RESTART_STAMP": str(
                tmp_path / "run" / "voice-restart.stamp"
            ),
            "JASPER_VOICE_RESTART_INTENT_MARKER": str(
                tmp_path / "run" / "voice-restart-intent"
            ),
            "JASPER_AEC_BRIDGE_READY_MARKER": str(
                tmp_path / "run" / "aec-bridge-ready"
            ),
            "JASPER_SYSTEMCTL": str(fake_systemctl),
            "JASPER_SYSTEMCTL_LOG": str(systemctl_log),
            # The script's Python bridges: pin the interpreter running the
            # tests, not whatever `python3` PATH offers. A bare system python3
            # can lack numpy and fail the measurement-registry bridge into its
            # fail-open branch.
            "JASPER_MIC_PROFILE_PYTHON": sys.executable,
            # Hermetic: always source the repo's shared env-file lib, never
            # a (possibly stale) installed copy under /usr/local/lib.
            "JASPER_ENV_FILE_LIB": str(
                ROOT / "deploy" / "lib" / "jasper-env-file.sh"
            ),
        }
    )
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        check=False,
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )


def _write_env(
    tmp_path: Path,
    mic_device: str,
    extra: str = "",
    voice_provider: str = "gemini",
    aec_port: int | None = 9876,
) -> Path:
    env_file = tmp_path / "jasper.env"
    port_line = "" if aec_port is None else f"JASPER_AEC_UDP_PORT={aec_port}\n"
    env_file.write_text(
        f"JASPER_MIC_DEVICE={mic_device}\n"
        f"{port_line}"
        "JASPER_AUDIO_DAC_ID=apple_usb_c_dongle\n"
        f"{_PERSISTED_REGISTRY_ENV}"
        f"{extra}"
    )
    if voice_provider:
        (tmp_path / "voice_provider.env").write_text(
            f"JASPER_VOICE_PROVIDER={voice_provider}\n"
        )
    (tmp_path / "voice_provider_ids").write_text(provider_ids_manifest_text())
    return env_file


def _write_mode(tmp_path: Path, mode: str = "auto") -> None:
    (tmp_path / "aec_mode.env").write_text(f"JASPER_AEC_MODE={mode}\n")


def _write_profile_mode(tmp_path: Path, profile: str) -> None:
    updates = profile_env_updates(profile)
    (tmp_path / "aec_mode.env").write_text(
        "".join(f"{key}={value}\n" for key, value in updates.items())
    )


def _write_card(tmp_path: Path, card: str = "Array", channels: int = 6) -> None:
    card_dir = tmp_path / "asound" / card
    card_dir.mkdir(parents=True)
    (card_dir / "stream0").write_text(
        f"Playback:\n  Status: Stop\nCapture:\n  Channels: {channels}\n"
    )


def _ready_marker(tmp_path: Path) -> Path:
    """The volatile POSITIVE verdict jasper-aec-bridge.service reads through
    ``ConditionPathExists=``. This reconciler is its single writer;
    ``_run_reconcile`` redirects it into tmp_path."""
    return tmp_path / "run" / "aec-bridge-ready"


def _prepublish_ready_marker(tmp_path: Path) -> None:
    """Stand in for an earlier pass that admitted the bridge."""
    marker = _ready_marker(tmp_path)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("reason=previous\n")


def _marker(tmp_path: Path) -> Path:
    """The persistent NEGATIVE gate marker jasper-voice.service reads through
    ``ConditionPathExists=!``. This reconciler is its single writer;
    ``_run_reconcile`` redirects it into tmp_path."""
    return tmp_path / "voice-input-absent"


def _stage(
    tmp_path: Path,
    mic: str,
    *,
    extra: str = "",
    voice_provider: str = "gemini",
    aec_port: int | None = 9876,
    mode: str | None = None,
    profile: str | None = None,
    card: str = "Array",
    channels: int | None = None,
    bonded: bool = False,
) -> Path:
    """Put tmp_path into one pass-start state; return the env file."""
    env_file = _write_env(
        tmp_path,
        mic,
        extra=extra,
        voice_provider=voice_provider,
        aec_port=aec_port,
    )
    if mode is not None:
        _write_mode(tmp_path, mode)
    if profile is not None:
        _write_profile_mode(tmp_path, profile)
    if channels is not None:
        _write_card(tmp_path, card=card, channels=channels)
    if bonded:
        (tmp_path / "grouping-voice.env").write_text(
            f"{VOICE_PARK_ENV}=1\n", encoding="utf-8"
        )
    return env_file


def _systemctl_double(tmp_path: Path, name: str, body: str) -> Path:
    """A logging ``systemctl`` stand-in; `body` runs before the default exit 0."""
    executable = tmp_path / name
    executable.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$JASPER_SYSTEMCTL_LOG\"\n"
        f"{body}"
        "exit 0\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def _double_name(unit: str) -> str:
    """A unit name that is safe as an executable's file name.

    macOS SIGKILLs an executable whose name ends in `.service`, so a double
    named after the unit it fakes never runs and every call through it looks
    like a failure.
    """
    return unit.removesuffix(".service")


def _systemctl_failing(tmp_path: Path, unit: str) -> Path:
    """A double whose ``restart <unit>`` fails."""
    return _systemctl_double(
        tmp_path,
        f"systemctl-restart-fails-{_double_name(unit)}",
        f"[[ \"$*\" == 'restart {unit}' ]] && exit 1\n",
    )


def _systemctl_reporting(tmp_path: Path, verb: str, unit: str, status: int) -> Path:
    """A double whose ``<verb> ... <unit>`` query answers `status`.

    systemd's query exits are a class, not a value: 3 is "inactive" for
    ``is-active`` and 1 is "disabled" for ``is-enabled``.
    """
    return _systemctl_double(
        tmp_path,
        f"systemctl-{verb}-{status}-{_double_name(unit)}",
        f'if [[ "$1" == "{verb}" && "$*" == *"{unit}"* ]]; then\n'
        f"  exit {status}\n"
        "fi\n",
    )


def _python_double(
    tmp_path: Path,
    name: str,
    *,
    failing_module: str,
    stderr_message: str = "",
    passthrough: bool = True,
) -> Path:
    """An interpreter that fails one of the script's Python bridges.

    ``passthrough`` serves every other bridge from the real interpreter; the
    partial-/opt/jasper-deploy shape sets it False so nothing else answers
    either.
    """
    echo = f"  echo '{stderr_message}' >&2\n" if stderr_message else ""
    tail = (
        f'exec "{sys.executable}" "$@"\n' if passthrough else "exit 0\n"
    )
    executable = tmp_path / name
    executable.write_text(
        "#!/usr/bin/env bash\n"
        f"if [[ \"$*\" == *'{failing_module}'* ]]; then\n"
        f"{echo}"
        "  exit 1\n"
        "fi\n"
        f"{tail}",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def _write_synthetic_xvf_resolver(
    tmp_path: Path,
    card: str,
    *,
    chip_beam_plan: str = "",
    chip_aec_supported: str = "0",
    policy_exit: int = 0,
) -> Path:
    """A mic-profile resolver double.

    ``policy_exit`` is the exit status it gives the separate chip-AEC DAC
    policy query, so a pass can have a working mic profile and a broken gate
    resolver — the shape the runtime-env carry exists for. The alignment
    vocabulary shares that shim module but not its failure: it goes to the real
    interpreter.
    """
    resolver = tmp_path / "synthetic-xvf-resolver"
    resolver.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$*\" == *'--alignment'* ]]; then\n"
        f"  exec {shlex.quote(sys.executable)} \"$@\"\n"
        "fi\n"
        "if [[ \"$*\" == *'jasper.cli.chip_aec_policy'* ]]; then\n"
        f"  exit {policy_exit}\n"
        "fi\n"
        "if [[ \"$*\" == *'jasper.cli.xvf_profile'* ]]; then\n"
        "  printf '%s\\n' \\\n"
        "    'JASPER_XVF_PRESENT=1' \\\n"
        "    'JASPER_XVF_VARIANT=xvf3800_future_variant' \\\n"
        "    'JASPER_XVF_DISPLAY_NAME=Future_XVF3800' \\\n"
        "    'JASPER_XVF_GEOMETRY=future' \\\n"
        f"    'JASPER_XVF_ALSA_CARD={card}' \\\n"
        "    'JASPER_XVF_CAPTURE_CHANNELS=6' \\\n"
        f"    'JASPER_XVF_CHIP_BEAM_PLAN={chip_beam_plan}' \\\n"
        f"    'JASPER_XVF_CHIP_AEC_SUPPORTED={chip_aec_supported}' \\\n"
        "    'JASPER_XVF_RECOMMENDED_PROFILE=xvf_chip_aec' \\\n"
        "    \"JASPER_XVF_REASON='future XVF needs a validated beam plan'\" \\\n"
        "    'JASPER_XVF_CHIP_REF_PCM_ACCESS=hw' \\\n"
        "    'JASPER_XVF_CHIP_REF_DEVICE=0' \\\n"
        "    'JASPER_XVF_CHIP_REF_RATE=16000' \\\n"
        "    'JASPER_XVF_CHIP_REF_PERIOD=128' \\\n"
        "    'JASPER_XVF_CHIP_REF_BUFFER=256' \\\n"
        f"    {_REGISTRY_ENV_ARGS}\n"
        "fi\n"
    )
    resolver.chmod(0o755)
    return resolver


def _outputd_status_payload(
    *,
    verdict: str,
    status: str = "locked",
    observe: bool = True,
    writer_enabled: bool = True,
) -> dict:
    return {
        "reference_outputs": {
            "chip_ref_pcm": "hw:CARD=Array,DEV=0",
            "chip_ref_writer": {"enabled": writer_enabled},
            "aec_clock": {
                "chip_ref_sro_ppm": 3.2 if verdict == "coherent" else 42.0,
                "sro_estimator_status": status,
                "verdict": verdict,
                "verdict_reason": f"{verdict}/{status}",
                "observe": observe,
            },
        },
    }


def test_candidate_default_is_the_mic_registry_card_list(tmp_path: Path) -> None:
    """With no operator override the default candidate list is the registry's
    own card names — the script keeps no copy of them (ADR-0235). The detected
    card is prepended, so compare the deduplicated order."""
    _stage(tmp_path, "udp:9876", mode="auto")

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    field = re.search(r" candidates=(.*?) legs=", result.stderr)
    assert field is not None, result.stderr
    candidates = list(dict.fromkeys(field.group(1).split()))
    assert candidates == list(xvf3800.ALSA_CARD_NAMES)


def test_reconcile_clears_stale_udp_when_array_is_absent(tmp_path: Path) -> None:
    env_file = _stage(tmp_path, "udp:9876", mode="auto")

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    assert "JASPER_MIC_DEVICE=Array" in env_file.read_text()
    commands = _systemctl_log(tmp_path)
    assert "stop jasper-aec-bridge.service jasper-aec-init.service" in commands
    assert "disable jasper-aec-bridge.service jasper-aec-init.service" in commands
    assert "stop jasper-voice.service" in commands
    assert VOICE_RESTART_CMD not in commands
    lines = commands.splitlines()
    assert lines.index("stop jasper-voice.service") < lines.index(
        "stop jasper-aec-bridge.service jasper-aec-init.service",
    )


@pytest.mark.parametrize(
    ("mic_device", "channels", "wants_bridge"),
    (("Array", 6, True), ("udp:9876", None, False)),
    ids=("array-present", "array-absent"),
)
def test_voice_wants_the_bridge_only_while_the_bridge_carries_the_mic(
    tmp_path: Path,
    mic_device: str,
    channels: int | None,
    wants_bridge: bool,
) -> None:
    """`Wants=` starts a unit even when it is disabled, so the want cannot live
    statically in jasper-voice.service: a box with no local mic — a streambox
    answering through a paired Bluetooth remote — would pull the whole AEC
    stack up anyway.
    """
    systemd_dir = tmp_path / "systemd"
    dropin = systemd_dir / "jasper-voice.service.d" / "10-aec-bridge-want.conf"
    dropin.parent.mkdir(parents=True)
    if not wants_bridge:
        # Seed ONLY here, so this case proves REMOVAL rather than
        # never-created, and the other proves CREATION rather than survival.
        dropin.write_text("[Unit]\nWants=jasper-aec-bridge.service\n")

    _stage(tmp_path, mic_device, mode="auto", channels=channels)

    result = _run_reconcile(
        tmp_path,
        "--reason",
        "test",
        extra_env={"JASPER_SYSTEMD_DIR": str(systemd_dir)},
    )

    assert result.returncode == 0, result.stderr
    assert dropin.exists() is wants_bridge
    if wants_bridge:
        assert "Wants=jasper-aec-bridge.service" in dropin.read_text()


def test_reconcile_enables_udp_aec_when_array_is_6_channel(tmp_path: Path) -> None:
    env_file = _stage(tmp_path, "Array", mode="auto", channels=6)

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    body = env_file.read_text()
    assert "JASPER_MIC_DEVICE=udp:9876" in body
    assert "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS=ready" in body
    assert "JASPER_OUTPUTD_CHIP_REF_PCM=hw:CARD=Array,DEV=0" in body
    assert "JASPER_OUTPUTD_REFERENCE_UDP_TARGET=127.0.0.1:9891" in body
    assert "JASPER_OUTPUTD_CHIP_REF_PERIOD_FRAMES=128" in body
    assert "JASPER_OUTPUTD_CHIP_REF_BUFFER_FRAMES=256" in body
    commands = _systemctl_log(tmp_path)
    assert "enable jasper-aec-init.service jasper-aec-bridge.service" in commands
    assert "reset-failed jasper-aec-init.service" in commands
    assert "reset-failed jasper-aec-bridge.service" in commands
    assert "is-failed --quiet" not in commands
    assert "restart jasper-aec-init.service" in commands
    assert "restart jasper-aec-bridge.service" in commands
    assert "enable jasper-voice.service" in commands
    assert VOICE_RESTART_CMD in commands
    lines = commands.splitlines()
    assert lines.count("restart jasper-outputd.service") == 1
    assert lines.index("restart jasper-outputd.service") < lines.index(
        "restart jasper-aec-init.service"
    )
    assert lines.index("restart jasper-aec-init.service") < lines.index(
        "restart jasper-aec-bridge.service"
    )
    assert lines.index("restart jasper-aec-bridge.service") < lines.index(
        VOICE_RESTART_CMD
    )


def test_bridge_ready_marker_publish_and_revoke_are_events(tmp_path: Path) -> None:
    """Ready-marker publish/revoke (ADR-0235 :187-204) are the most
    load-bearing verdicts in the file — jasper-aec-bridge.service's
    StartLimitAction=reboot gates on the marker — and had no event= line
    (G12). Every pass withdraws first (unconditional), then
    republishes only where a verdict settles (ADR-0224)."""
    _stage(tmp_path, "Array", mode="auto", channels=6)

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    assert _event_values(
        result.stderr, "aec_reconcile.bridge_ready", "state"
    ) == ["revoked", "published"]


def test_bridge_ready_marker_stays_revoked_with_no_candidate_mic(
    tmp_path: Path,
) -> None:
    """The mirror case: nothing admits the bridge, so the unconditional
    top-of-pass revoke fires but the marker is never republished."""
    _stage(tmp_path, "udp:9876", mode="auto")

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    states = _event_values(result.stderr, "aec_reconcile.bridge_ready", "state")
    assert states and set(states) == {"revoked"}


@pytest.mark.parametrize(
    ("current_mic", "changed"),
    [("udp:9876", "1"), ("Array", "0")],
    ids=("stale-current", "already-selected"),
)
def test_direct_mic_selected_event_for_a_non_6_channel_custom_mic(
    tmp_path: Path, current_mic: str, changed: str
) -> None:
    """Same shape as the aec_disabled direct-mic events above (G12): a
    custom-profile card below the AEC channel threshold is still a usable
    plain mic, and the pass that falls back to it must be greppable too."""
    _stage(
        tmp_path, current_mic, mode="auto", profile="custom", card="Array", channels=2
    )

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    assert _event_values(
        result.stderr, "aec_reconcile.direct_mic_selected", "reason"
    ) == ["not_6_channel"]
    assert _event_values(
        result.stderr, "aec_reconcile.direct_mic_selected", "changed"
    ) == [changed]


def test_voice_input_absent_marker_mark_carries_the_reason(tmp_path: Path) -> None:
    """The absence marker's success path (ADR-0235 :1625) had no event= line
    (G12); jasper-voice.service gates ExecStart on the
    marker's absence, so this is what a no-input box's journal shows."""
    _stage(tmp_path, "udp:9876", mode="auto")

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    assert _event_values(
        result.stderr, "aec_reconcile.voice_input_absent", "state"
    ) == ["marked"]
    [reason] = _event_values(
        result.stderr, "aec_reconcile.voice_input_absent", "reason"
    )
    assert reason
    # stop_voice's park is a real absence, not the chip-AEC validation
    # bounce's — no `transient=1` line (ADR-0239).
    assert _marker(tmp_path).read_text().splitlines() == [f"reason={reason}"]


def test_validation_bounce_marks_the_park_transient(tmp_path: Path) -> None:
    """activate_managed_chip_aec's own park (:1897) is the ~8 s chip-AEC
    validation round trip, not a real absence — it marks `transient=1` so
    the daemon's shutdown cue (ADR-0239) skips it. The pass's own
    `restart_voice` clears the marker before `_run_reconcile` returns, so a
    fake systemctl snapshots it at the stop that immediately follows the
    write (mirrors `_drive_alignment_disposition`'s CHECKING branch)."""
    _stage(tmp_path, "Array", profile="auto", channels=6)
    snapshot = tmp_path / "checking-marker.env"
    fake = _systemctl_double(
        tmp_path,
        "checking-marker-snapshot-systemctl",
        "[[ \"$*\" == 'stop jasper-voice.service jasper-aec-bridge.service'"
        f" && ! -f {shlex.quote(str(snapshot))} ]]"
        f" && cp \"$JASPER_VOICE_INPUT_ABSENT_MARKER\" {shlex.quote(str(snapshot))}\n",
    )

    result = _run_reconcile(
        tmp_path, "--reason", "test", extra_env={"JASPER_SYSTEMCTL": str(fake)}
    )

    assert result.returncode == 0, result.stderr
    assert snapshot.read_text().splitlines() == [
        "reason=validating commissioned chip-AEC alignment",
        "transient=1",
    ]
    assert not _marker(tmp_path).exists()


def test_the_bridge_ready_revoke_precedes_the_absence_mark(tmp_path: Path) -> None:
    """G13's ordering, pinned where it already holds. The unconditional
    top-of-pass revoke (:204, ADR-0224) runs before this pass decides
    anything, so by the time a no-candidate pass marks the absence the
    bridge's next start is already a skipped ConditionPathExists — and a
    condition skip does not count toward StartLimitBurst=4 /
    StartLimitAction=reboot. No second revoke was added on the absence path:
    it could only re-emit a verdict this pass has already published.
    ADR-0235 R6."""
    _stage(tmp_path, "udp:9876", mode="auto")

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    verdicts = re.findall(r"\bevent=(\S+) state=(\S+)", result.stderr)
    assert verdicts.index(
        ("aec_reconcile.bridge_ready", "revoked")
    ) < verdicts.index(("aec_reconcile.voice_input_absent", "marked"))


def test_voice_input_absent_marker_clear_carries_the_markers_own_reason(
    tmp_path: Path,
) -> None:
    """`clear`'s reason is whatever the marker body it just removed carried —
    not a description of what un-parked voice this pass. Free-prose today
    (jasper/mic_presence.py `_marker_reason`); emitted as-is (ADR-0235
    PR 12), not a code vocabulary this PR does not own.

    ``profile="custom"``, not ``mode="auto"``: a bare auto pass over a
    real 6-channel XVF card resolves the managed chip-AEC profile and marks
    (then clears) its OWN commissioning-validation reason, which would
    overwrite the one under test before this pass's clear ever reads it.
    """
    _marker(tmp_path).write_text("reason=stale-no-mic\n")
    _stage(tmp_path, "Array", profile="custom", channels=6)

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    assert _event_values(
        result.stderr, "aec_reconcile.voice_input_absent", "state"
    ) == ["cleared"]
    assert _event_values(
        result.stderr, "aec_reconcile.voice_input_absent", "reason"
    ) == ["stale-no-mic"]


@pytest.mark.parametrize(
    "selection", ["auto", "xvf_chip_aec", "xvf_chip_aec_testing"]
)
def test_the_alignment_record_names_the_selection_it_was_written_under(
    tmp_path: Path, selection: str
) -> None:
    """Every write site is guarded on a non-custom profile, and stamps it.

    The stamp is what lets the consumer in jasper.audio_profile_state tell a
    live verdict from one the last managed pass left behind.
    """
    env_file = _stage(tmp_path, "Array", profile=selection, channels=6)

    assert _run_reconcile(tmp_path, "--reason", "test").returncode == 0

    body = env_file.read_text()
    assert f"JASPER_AEC_CHIP_AEC_ALIGNMENT_SELECTION={selection}" in body
    assert "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS=" in body


def test_a_custom_pass_neither_writes_nor_clears_an_inherited_record(
    tmp_path: Path,
) -> None:
    """A custom profile writing nothing is only the first half: it does not
    clear or rewrite the record it inherits either, which is how a leftover
    outlives the selection that produced it — and why the stamp, rather than
    the record's presence, is what tells the two apart.
    """
    seeded = (
        "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS=disclosed_stale\n"
        "JASPER_AEC_CHIP_AEC_ALIGNMENT_REASON='output DAC has no codified timing'\n"
        "JASPER_AEC_CHIP_AEC_ALIGNMENT_ACTION='Run sudo jasper-aec-commission'\n"
        "JASPER_AEC_CHIP_AEC_ALIGNMENT_SELECTION=xvf_chip_aec\n"
    )
    env_file = _stage(tmp_path, "Array", extra=seeded, profile="custom", channels=6)

    assert _run_reconcile(tmp_path, "--reason", "test").returncode == 0

    surviving = [
        line
        for line in env_file.read_text().splitlines()
        if line.startswith("JASPER_AEC_CHIP_AEC_ALIGNMENT_")
    ]
    assert surviving == seeded.splitlines()


@pytest.mark.parametrize(
    "failing_unit",
    ["jasper-outputd.service", "jasper-aec-bridge.service"],
)
def test_a_unit_that_will_not_come_up_faults_and_parks(
    tmp_path: Path, failing_unit: str
) -> None:
    """Both ends of the chip-AEC bring-up: the output owner that has to carry
    the settled reference vector, and the bridge that has to carry the mic.
    Either failing records the fault, clears the live reference target, and
    parks rather than starting voice onto a carrier nothing feeds."""
    env_file = _stage(tmp_path, "Array", mode="auto", channels=6)

    result = _run_reconcile(
        tmp_path,
        "--reason",
        "test",
        extra_env={"JASPER_SYSTEMCTL": str(_systemctl_failing(tmp_path, failing_unit))},
    )

    assert result.returncode == 0, result.stderr
    body = env_file.read_text()
    assert "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS=fault" in body
    assert "JASPER_OUTPUTD_REFERENCE_UDP_TARGET=''" in body
    assert _marker(tmp_path).exists()
    # The bridge case publishes before its restart, so the park has to take the
    # verdict back — while that restart's Restart=on-failure is re-arming every
    # 2 s into StartLimitAction=reboot.
    assert not _ready_marker(tmp_path).exists()
    commands = _systemctl_log(tmp_path)
    assert VOICE_RESTART_CMD not in commands
    if failing_unit == "jasper-outputd.service":
        # The bounce precedes the stack, so neither unit is reached at all.
        assert "restart jasper-aec-init.service" not in commands
        assert "restart jasper-aec-bridge.service" not in commands


def test_a_killed_init_faults_rather_than_reading_back_the_previous_pass(
    tmp_path: Path,
) -> None:
    """aec-init writes AND unlinks its record from one `finally` a SIGKILL, an
    OOM kill or an unmet `Requires=` never reaches, and a shipped box's
    steady-state record is `disclosed_stale`. So the reconciler clears the
    record before every init restart: a pass that published nothing is a fault,
    not an inherited "run the commissioner".
    """
    env_file = _stage(tmp_path, "Array", mode="auto", channels=6)
    _publish_record(
        tmp_path,
        alignment_health(
            chip_aec_health.COMMISSION_REQUIRED, selection="xvf_chip_aec"
        ),
    )

    result = _run_reconcile(
        tmp_path,
        "--reason",
        "test",
        extra_env={
            "JASPER_SYSTEMCTL": str(
                _systemctl_failing(tmp_path, "jasper-aec-init.service")
            )
        },
    )

    assert result.returncode == 0, result.stderr
    published = parse_env_file(str(env_file))
    assert published[chip_aec_health.STATUS_KEY] == chip_aec_health.STATUS_FAULT
    assert not _alignment_record(tmp_path).exists()


def _failed_init_systemctl(
    tmp_path: Path, disposition: str, *, bridge: str = "active"
) -> Path:
    """A systemctl double whose jasper-aec-init restart fails after publishing
    `disposition`'s record — what the real oneshot leaves behind on a non-zero
    exit.

    `bridge` picks how the AEC bridge behaves afterwards: it comes up
    (`active`), its restart fails outright (`restart_fails`), or its restart
    reports success because the unit's start condition SKIPPED it (`skipped`) —
    the case where a bare exit-status check would certify a bridge that is not
    running.
    """
    assert bridge in {"active", "restart_fails", "skipped"}
    record = alignment_health(
        disposition,
        selection=aec_init.alignment_selection(
            {"JASPER_AEC_MODE_FILE": str(tmp_path / "aec_mode.env")}
        ),
    ).to_shell()
    return _systemctl_double(
        tmp_path,
        f"init-{disposition}-{bridge}-systemctl",
        "if [[ \"$*\" == 'restart jasper-aec-init.service' ]]; then\n"
        "cat > \"$JASPER_AEC_ALIGNMENT_RECORD_FILE\" <<'JTSRECORD'\n"
        f"{record}"
        "JTSRECORD\n"
        "exit 1\n"
        "fi\n"
        + (
            "[[ \"$*\" == 'restart jasper-aec-bridge.service' ]] && exit 1\n"
            if bridge == "restart_fails"
            else ""
        )
        + (
            ""
            if bridge == "active"
            else "[[ \"$*\" == 'is-active --quiet jasper-aec-bridge.service' ]]"
            " && exit 3\n"
        ),
    )


@pytest.mark.parametrize(
    "disposition",
    [
        chip_aec_health.COMMISSION_REQUIRED,
        # The ordering race is not a moved artifact, so it must NOT send the
        # household to the two-minute commissioner.
        chip_aec_health.OUTPUTD_ENV_STALE,
    ],
)
def test_an_unappliable_alignment_runs_software_aec3_and_discloses(
    tmp_path: Path, disposition: str
) -> None:
    # ADR-0101: neither disposition says anything observably broke, so the box
    # keeps hearing on the software AEC3 leg instead of going silently deaf.
    # What the record itself says is
    # test_every_published_record_is_the_one_chip_aec_health_writes' subject.
    env_file = _stage(tmp_path, "Array", mode="auto", channels=6)
    fake = _failed_init_systemctl(tmp_path, disposition)

    result = _run_reconcile(
        tmp_path, "--reason", "test", extra_env={"JASPER_SYSTEMCTL": str(fake)}
    )

    assert result.returncode == 0, result.stderr
    body = env_file.read_text()
    # The software-AEC3 leg shape: chip beams off, the raw leg and outputd's
    # far-end reference on, and the bridge's own UDP carrier still the mic.
    assert "JASPER_AEC_CHIP_AEC_ENABLED=0" in body
    assert "JASPER_MIC_DEVICE_RAW=udp:9877" in body
    assert "JASPER_OUTPUTD_REFERENCE_UDP_TARGET=127.0.0.1:9891" in body
    assert "JASPER_MIC_DEVICE=udp:9876" in body
    # Voice comes back, and the gate that would have kept it off is cleared.
    assert not _marker(tmp_path).exists()
    commands = _systemctl_log(tmp_path)
    assert "restart jasper-aec-bridge.service" in commands
    assert VOICE_RESTART_CMD in commands


@pytest.mark.parametrize("bridge", ["active", "restart_fails", "skipped"])
def test_a_disclosed_pass_settles_its_mic_leg_before_its_one_output_bounce(
    tmp_path: Path, bridge: str
) -> None:
    # The verdict precedes the publication: jasper-outputd is bounced once, on
    # the leg vector the pass settled on, so it can never load the attempt's.
    #
    # Three shapes reach the verdict. The bridge comes up and carries software
    # AEC3; its restart fails; or its restart exits 0 because the ready-marker
    # condition SKIPPED the unit — `systemctl restart` cannot tell the last two
    # apart, so the unit is asked. Without a bridge nothing writes udp:9876, and
    # jasper-voice bound to an unfed socket stalls into WatchdogSec=30s and the
    # unit's StartLimitAction=reboot, so those two take the direct mic.
    carried = bridge == "active"
    env_file = _stage(tmp_path, "Array", mode="auto", channels=6)
    fake = _failed_init_systemctl(
        tmp_path, chip_aec_health.COMMISSION_REQUIRED, bridge=bridge
    )

    result = _run_reconcile(
        tmp_path, "--reason", "test", extra_env={"JASPER_SYSTEMCTL": str(fake)}
    )

    assert result.returncode == 0, result.stderr
    body = env_file.read_text()
    assert "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS=disclosed_stale" in body
    assert f"JASPER_MIC_DEVICE={'udp:9876' if carried else 'Array'}" in body
    assert not _marker(tmp_path).exists()
    # The disclose route publishes before it asks the unit, so a bridge that
    # never came up has to leave the verdict withdrawn.
    assert _ready_marker(tmp_path).exists() is carried
    lines = _systemctl_log(tmp_path).splitlines()
    assert VOICE_RESTART_CMD in lines
    # aec-init's own run is the hand-off into the disclose path; the bounce
    # before it armed the chip-reference producer aec-init samples.
    handover = lines.index("restart jasper-aec-init.service")
    disclosed = lines[handover:]
    bounces = _unit_command_indices(disclosed, "restart", "jasper-outputd.service")
    assert len(bounces) == 1
    if carried:
        # The bridge is asked before the output owner is told, so the bounce
        # carries the verdict rather than racing it.
        restarted = _unit_command_indices(
            disclosed, "restart", "jasper-aec-bridge.service"
        )
        assert restarted and max(restarted) < bounces[0]
        return
    # An enabled+failed bridge would Restart=on-failure every 2 s into its own
    # StartLimitAction=reboot, and a transient success would grab the
    # single-open XVF capture device jasper-voice now holds. Stop AND disable —
    # and that teardown writes the settled legs, so it precedes the bounce.
    stopped = _unit_command_indices(disclosed, "stop", "jasper-aec-bridge.service")
    disabled = _unit_command_indices(disclosed, "disable", "jasper-aec-bridge.service")
    assert stopped and disabled
    assert max(disabled) < bounces[0]
    assert max(disabled) < lines.index(VOICE_RESTART_CMD)
    # The UDP legs go with the bridge: a stale JASPER_MIC_DEVICE_RAW=udp: leaves
    # voice's secondary capture spinning on a port nobody writes.
    assert "JASPER_MIC_DEVICE_RAW=udp:" not in body


def test_reconcile_discloses_an_applied_alignment_its_proof_no_longer_matches(
    tmp_path: Path,
) -> None:
    # jasper-aec-init armed the chip from the banked K and published its own
    # `disclosed_stale` verdict; the reconciler copies that into the env file
    # verbatim, and the stack stays up.
    env_file = _stage(tmp_path, "Array", mode="auto", channels=6)
    fake = _publishing_init_systemctl(
        tmp_path,
        alignment_health(
            chip_aec_health.APPLIED,
            selection="auto",
            identity_diff=("xvf_serial",),
        ),
    )

    result = _run_reconcile(
        tmp_path, "--reason", "test", extra_env={"JASPER_SYSTEMCTL": str(fake)}
    )

    assert result.returncode == 0, result.stderr
    body = env_file.read_text()
    assert "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS=disclosed_stale" in body
    assert (
        "JASPER_AEC_CHIP_AEC_ALIGNMENT_REASON='commissioned alignment was "
        "measured on a different unit (xvf_serial)'"
    ) in body
    assert "JASPER_AEC_CHIP_AEC_ALIGNMENT_ACTION='Run sudo jasper-aec-commission'" in body
    # Chip-AEC is armed and carrying the mic, exactly as a `ready` box would be.
    assert "JASPER_AEC_CHIP_AEC_ENABLED=1" in body
    assert "JASPER_MIC_DEVICE=udp:9876" in body
    assert not _marker(tmp_path).exists()
    commands = _systemctl_log(tmp_path)
    assert "restart jasper-aec-bridge.service" in commands
    assert VOICE_RESTART_CMD in commands


def test_a_disclosure_reason_reaches_the_env_file_without_apostrophes(
    tmp_path: Path,
) -> None:
    # This reason is the only free text routed into $ENV_FILE, which systemd
    # reads through EnvironmentFile=. deploy/lib/jasper-env-file.sh's '\''
    # idiom for an embedded apostrophe is read differently by bash `source`
    # and by systemd's parser, so the daemons and this script would disagree
    # on the value. Strip them at the boundary instead.
    env_file = _stage(tmp_path, "Array", mode="auto", channels=6)
    fake = _publishing_init_systemctl(
        tmp_path,
        AlignmentHealth(
            chip_aec_health.STATUS_DISCLOSED_STALE,
            "this box's proof moved",
            chip_aec_health.ACTION_RECOMMISSION,
            "auto",
        ),
    )

    result = _run_reconcile(
        tmp_path, "--reason", "test", extra_env={"JASPER_SYSTEMCTL": str(fake)}
    )

    assert result.returncode == 0, result.stderr
    reason = _env_assignments(env_file)["JASPER_AEC_CHIP_AEC_ALIGNMENT_REASON"]
    assert reason == "'this boxs proof moved'"
    # And the file still round-trips through the reader every daemon uses.
    assert "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS=disclosed_stale" in (
        env_file.read_text()
    )


def _drive_alignment_disposition(tmp_path: Path, disposition: str) -> Path:
    """Put the box in the one state that lands on `disposition`; return the
    file its published record is read back from."""
    env_file = tmp_path / "jasper.env"
    if disposition == chip_aec_health.XVF_ABSENT:
        # A managed selection whose XVF is not on the bus. Bonded, so the role
        # park exits the pass before a later site can rewrite the record.
        _stage(tmp_path, "Array", profile="xvf_chip_aec", bonded=True)
        assert _run_reconcile(tmp_path, "--reason", "test").returncode == 0
        return env_file

    _stage(tmp_path, "Array", profile="auto", channels=6)
    if disposition == chip_aec_health.CHECKING:
        # The transient inside activate_managed_chip_aec: snapshot the env file
        # at the voice stop that immediately follows the write, since the pass
        # goes on to overwrite it with its verdict.
        snapshot = tmp_path / "checking.env"
        fake = _systemctl_double(
            tmp_path,
            "checking-snapshot-systemctl",
            "[[ \"$*\" == 'stop jasper-voice.service jasper-aec-bridge.service'"
            f" && ! -f {shlex.quote(str(snapshot))} ]]"
            f" && cp \"$JASPER_ENV_FILE\" {shlex.quote(str(snapshot))}\n",
        )
        extra_env = {"JASPER_SYSTEMCTL": str(fake)}
    elif disposition in {
        chip_aec_health.COMMISSION_REQUIRED, chip_aec_health.OUTPUTD_ENV_STALE
    }:
        extra_env = {
            "JASPER_SYSTEMCTL": str(_failed_init_systemctl(tmp_path, disposition))
        }
    elif disposition == chip_aec_health.REAPPLY_FAILED:
        # aec-init failed without publishing a verdict of its own.
        extra_env = {
            "JASPER_SYSTEMCTL": str(
                _systemctl_failing(tmp_path, "jasper-aec-init.service")
            )
        }
    elif disposition == chip_aec_health.REFERENCE_PRODUCER_DOWN:
        extra_env = {
            "JASPER_SYSTEMCTL": str(
                _systemctl_failing(tmp_path, "jasper-outputd.service")
            )
        }
    elif disposition == chip_aec_health.BRIDGE_FAILED:
        extra_env = {
            "JASPER_SYSTEMCTL": str(
                _systemctl_failing(tmp_path, "jasper-aec-bridge.service")
            )
        }
    else:
        assert disposition == chip_aec_health.APPLIED
        extra_env = {}
    result = _run_reconcile(tmp_path, "--reason", "test", extra_env=extra_env)
    assert result.returncode == 0, result.stderr
    return tmp_path / "checking.env" if disposition == chip_aec_health.CHECKING \
        else env_file


@pytest.mark.parametrize(
    "disposition",
    [
        chip_aec_health.APPLIED,
        chip_aec_health.COMMISSION_REQUIRED,
        chip_aec_health.OUTPUTD_ENV_STALE,
        chip_aec_health.REAPPLY_FAILED,
        chip_aec_health.REFERENCE_PRODUCER_DOWN,
        chip_aec_health.BRIDGE_FAILED,
        chip_aec_health.CHECKING,
        chip_aec_health.XVF_ABSENT,
    ],
)
def test_every_published_record_is_the_one_chip_aec_health_writes(
    tmp_path: Path, disposition: str
) -> None:
    """The reconciler publishes jasper.chip_aec.health's record verbatim, for
    every disposition it can reach, stamped with the mode file's selection."""
    published = _drive_alignment_disposition(tmp_path, disposition)

    values = parse_env_file(str(published))
    selection = parse_env_file(str(tmp_path / "aec_mode.env"))[
        "JASPER_AUDIO_INPUT_PROFILE"
    ]
    assert {key: values.get(key) for key in chip_aec_health.ENV_KEYS} == (
        alignment_health(disposition, selection=selection).to_env()
    )


@pytest.mark.parametrize(
    ("detected_channels", "repairs"),
    [(xvf3800.RECOMMENDED_FIRMWARE.capture_channels, True), (2, False)],
)
def test_reconcile_repairs_capture_mixer_before_arming_six_channel_aec(
    tmp_path: Path, detected_channels: int, repairs: bool
) -> None:
    channels = xvf3800.RECOMMENDED_FIRMWARE.capture_channels
    expected = [
        f"amixer|-c|Array|cset|name={xvf3800.MIXER_CAPTURE_SWITCH}|"
        + ",".join(["on"] * channels),
        f"amixer|-c|Array|cset|name={xvf3800.MIXER_CAPTURE_VOLUME}|"
        + ",".join([str(xvf3800.MIXER_VOLUME_MAX)] * channels),
        "alsactl|store",
    ]
    bin_dir, mixer_log = _fake_mixer_tools(tmp_path)
    _stage(tmp_path, "Array", mode="auto", channels=detected_channels)

    result = _run_reconcile(
        tmp_path,
        "--reason",
        "test",
        extra_env={
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "JASPER_MIXER_LOG": str(mixer_log),
        },
    )

    assert result.returncode == 0, result.stderr
    calls = mixer_log.read_text().splitlines() if mixer_log.exists() else []
    assert calls == (expected if repairs else [])


@pytest.mark.parametrize(
    ("failing", "controls"),
    [
        ("amixer", [xvf3800.MIXER_CAPTURE_SWITCH, xvf3800.MIXER_CAPTURE_VOLUME]),
        ("alsactl", ["alsactl_store"]),
    ],
)
def test_mixer_repair_failure_is_one_event_per_invocation(
    tmp_path: Path, failing: str, controls: list[str]
) -> None:
    """ensure_capture_mixer_open swallows every amixer/alsactl error so a
    mixer failure never parks voice — but each failing invocation must still
    become one greppable event=aec_reconcile.mixer_repair line, or a
    silent-mute regression has no signal (ADR-0235 PR 12 / G12)."""
    bin_dir, mixer_log = _fake_mixer_tools(tmp_path, failing=failing)
    _stage(tmp_path, "Array", mode="auto", channels=6)

    result = _run_reconcile(
        tmp_path,
        "--reason",
        "test",
        extra_env={
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "JASPER_MIXER_LOG": str(mixer_log),
        },
    )

    assert result.returncode == 0, result.stderr
    assert (
        _event_values(result.stderr, "aec_reconcile.mixer_repair", "control")
        == controls
    )
    # Non-fatal: the pass still arms AEC on the same run.
    assert "JASPER_MIC_DEVICE=udp:9876" in (tmp_path / "jasper.env").read_text()


@pytest.mark.parametrize("provider_id", sorted(VALID_PROVIDER_IDS))
def test_reconcile_accepts_catalog_provider_ids(
    tmp_path: Path,
    provider_id: str,
) -> None:
    _stage(tmp_path, "Array", voice_provider=provider_id, mode="auto", channels=6)

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    commands = _systemctl_log(tmp_path)
    assert "enable jasper-voice.service" in commands
    assert VOICE_RESTART_CMD in commands


def _no_provider_manifest(tmp_path: Path) -> None:
    (tmp_path / "voice_provider_ids").unlink()


def _short_provider_manifest(tmp_path: Path) -> None:
    (tmp_path / "voice_provider_ids").write_text("gemini\nopenai\n")


@pytest.mark.parametrize(
    ("provider", "stage_manifest"),
    [
        ("", None),
        ("bad-provider", None),
        ("gemini", _no_provider_manifest),
        ("grok", _short_provider_manifest),
    ],
    ids=("unset", "not-an-id", "no-manifest", "not-in-manifest"),
)
def test_reconcile_parks_voice_for_an_unusable_provider(
    tmp_path: Path,
    provider: str,
    stage_manifest: Callable[[Path], None] | None,
) -> None:
    """Four ways the active provider fails to resolve; one park. The mic work
    still happens — parking voice is not parking the box."""
    env_file = _stage(
        tmp_path, "Array", voice_provider=provider, mode="auto", channels=6
    )
    if stage_manifest is not None:
        stage_manifest(tmp_path)

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    assert "JASPER_MIC_DEVICE=udp:9876" in env_file.read_text()
    commands = _systemctl_log(tmp_path)
    assert "disable --now jasper-voice.service" in commands
    assert VOICE_RESTART_CMD not in commands


def test_reconcile_captures_directly_when_managed_xvf_is_not_6_channel(
    tmp_path: Path,
) -> None:
    """#2984 Q2: a 2-channel XVF is a usable plain microphone.

    Echo cancellation needs the 6-channel endpoint, so the box says so and
    keeps hearing on the chip's direct capture instead of going deaf.
    """
    env_file = _stage(tmp_path, "udp:9876", mode="auto", channels=2)
    _marker(tmp_path).write_text("reason=stale\n")

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    body = env_file.read_text()
    assert "JASPER_MIC_DEVICE=Array" in body
    assert "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS=disclosed_stale" in body
    assert "DFU flash to 6-channel firmware" in body
    # No bridge, so no UDP leg may be advertised to voice.
    assert "JASPER_MIC_DEVICE_RAW=udp:" not in body
    assert "JASPER_AEC_CHIP_AEC_ENABLED=0" in body
    commands = _systemctl_log(tmp_path)
    assert "disable jasper-aec-bridge.service jasper-aec-init.service" in commands
    assert VOICE_RESTART_CMD in commands
    assert not _marker(tmp_path).exists()


def test_reconcile_respects_custom_mic_device(tmp_path: Path) -> None:
    env_file = _stage(tmp_path, "UMIK-2", mode="auto")

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    assert "JASPER_MIC_DEVICE=UMIK-2" in env_file.read_text()
    commands = _systemctl_log(tmp_path)
    assert "disable jasper-aec-bridge.service jasper-aec-init.service" in commands
    assert "stop jasper-voice.service" not in commands
    assert VOICE_RESTART_CMD not in commands


def _write_accessory_mics(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "accessory-mics.env"
    path.write_text(body, encoding="utf-8")
    return path


def _pair_remote(tmp_path: Path) -> Path:
    """The accessory owner's real publish shape (source_id=device)."""
    return _write_accessory_mics(
        tmp_path,
        f"JASPER_MANUAL_MIC_SOURCES=wiim_remote_2={WIIM_REMOTE_2_MIC_DEVICE}\n",
    )


def test_no_local_mic_with_accessory_keeps_voice_up(tmp_path: Path) -> None:
    """Issue #2205: a paired accessory mic satisfies the voice-input gate.

    A box with no local microphone but a published push-to-talk source is a
    working speaker. The reconciler must NOT stamp the gate marker (which would
    make PID 1 skip the start and leave the remote's button dead), and must
    (re)start voice so the source is actually read.
    """
    env_file = _stage(tmp_path, "udp:9876", mode="auto")
    _pair_remote(tmp_path)

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    assert not _marker(tmp_path).exists()
    commands = _systemctl_log(tmp_path)
    assert "stop jasper-voice.service" not in commands
    assert VOICE_RESTART_CMD in commands
    assert "wiim_remote_2" in result.stderr
    assert "JASPER_MIC_DEVICE=Array" in env_file.read_text()


def test_the_env_file_voice_will_read_is_complete_before_the_restart(
    tmp_path: Path,
) -> None:
    """``restart_voice`` uses ``systemctl --no-block``, so systemd can start
    jasper-voice while this oneshot is still running. Both facts the daemon
    plans its legs from — the mic device it opens and the local-mic verdict it
    counts legs against — must already be on disk when the restart is queued,
    or voice binds the udp: socket the reconciler is mid-replacement of and
    watchdog-restarts. Command ORDER in the systemctl log cannot see this, so
    snapshot the env file at the moment the restart is issued.
    """
    env_file = _stage(tmp_path, "udp:9876", mode="auto")
    _pair_remote(tmp_path)
    snapshot = tmp_path / "jasper.env.at-restart"
    snapshotting = _systemctl_double(
        tmp_path,
        "systemctl-snapshot",
        'if [[ "$*" == *"restart jasper-voice.service"* ]]; then\n'
        '  cp "$JASPER_ENV_FILE" "$JASPER_ENV_SNAPSHOT"\n'
        "fi\n",
    )

    result = _run_reconcile(
        tmp_path,
        "--reason",
        "test",
        extra_env={
            "JASPER_SYSTEMCTL": str(snapshotting),
            "JASPER_ENV_SNAPSHOT": str(snapshot),
        },
    )

    assert result.returncode == 0, result.stderr
    assert snapshot.exists(), "voice was never restarted"
    at_restart = _env_assignments(snapshot)
    assert at_restart["JASPER_MIC_DEVICE"] == "Array"
    assert at_restart["JASPER_LOCAL_MIC_PRESENT"] == "0"
    assert "JASPER_MIC_DEVICE=Array" in env_file.read_text()


def _stale_local_mic_verdict(tmp_path: Path) -> None:
    env_file = tmp_path / "jasper.env"
    env_file.write_text(env_file.read_text() + "JASPER_LOCAL_MIC_PRESENT=0\n")


@pytest.mark.parametrize(
    ("mic", "channels", "prepare", "expected"),
    [
        ("udp:9876", None, _pair_remote, "0"),
        ("Array", 2, None, "1"),
        ("UMIK-2", None, _stale_local_mic_verdict, "unknown"),
    ],
    ids=("accessory-only", "candidate-card", "custom-device"),
)
def test_publishes_the_local_mic_half_of_the_voice_input_gate(
    tmp_path: Path,
    mic: str,
    channels: int | None,
    prepare: Callable[[Path], object] | None,
    expected: str,
) -> None:
    """The daemon half of #2205 needs to know WHICH half satisfied the gate.

    The marker is the AND of both absences, so it cannot say; this reconciler
    owns local-mic presence and publishes that half as a fact. `0` lets
    jasper-voice plan zero wake legs and serve the remote's button. A
    mic-bearing speaker must never read `0` — that drops its wake leg. And a
    custom device this script does not manage reads `unknown`, overwriting a
    stale `0`: neither `0` (the daemon would never open the operator's mic) nor
    silence (the stale value survives and does the same) is safe.
    """
    env_file = _stage(tmp_path, mic, mode="auto", channels=channels)
    if prepare is not None:
        prepare(tmp_path)

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    assert _env_assignments(env_file)["JASPER_LOCAL_MIC_PRESENT"] == expected


_CUSTOM_PROFILE_MODE = "JASPER_AEC_MODE=auto\nJASPER_AUDIO_INPUT_PROFILE=custom\n"


def _no_accessory_file(tmp_path: Path) -> dict[str, str]:
    _write_mode(tmp_path)
    return {}


def _malformed_accessory_file(tmp_path: Path) -> dict[str, str]:
    _write_mode(tmp_path)
    # One usable entry beside a broken one: the case where a lenient parser
    # would open the gate for a Config that raises at daemon startup.
    _write_accessory_mics(
        tmp_path,
        f"JASPER_MANUAL_MIC_SOURCES=wiim_remote_2={WIIM_REMOTE_2_MIC_DEVICE},bad\n",
    )
    return {}


def _accessory_probe_fails(tmp_path: Path) -> dict[str, str]:
    # The partial-/opt/jasper-deploy shape: the interpreter answers
    # jasper.cli.xvf_profile but not jasper.accessories.mic_env.
    (tmp_path / "aec_mode.env").write_text(_CUSTOM_PROFILE_MODE)
    _pair_remote(tmp_path)
    return {
        "JASPER_MIC_PROFILE_PYTHON": str(
            _python_double(
                tmp_path,
                "partial-deploy-python",
                failing_module="jasper.accessories.mic_env",
                stderr_message="ModuleNotFoundError: jasper.accessories.mic_env",
                passthrough=False,
            )
        )
    }


def _accessory_probe_unavailable(tmp_path: Path) -> dict[str, str]:
    (tmp_path / "aec_mode.env").write_text(_CUSTOM_PROFILE_MODE)
    _pair_remote(tmp_path)
    return {"JASPER_MIC_PROFILE_PYTHON": str(tmp_path / "no-such-interpreter")}


@pytest.mark.parametrize(
    ("prepare", "reason_says", "journal_says"),
    [
        (_no_accessory_file, ("no accessory microphone paired",), ()),
        (
            _malformed_accessory_file,
            ("could not be determined",),
            # The parser's own sentence — which rule the content broke — reaches
            # the journal, because that sentence IS the remediation.
            ("refusing to publish accessory mic sources", "must be source_id=device"),
        ),
        (
            _accessory_probe_fails,
            ("could not be determined", "probe failed"),
            # The module's own stderr reaches the journal, not /dev/null.
            ("accessory mic probe failed", "ModuleNotFoundError"),
        ),
        (
            _accessory_probe_unavailable,
            ("could not be determined",),
            ("accessory mic probe unavailable",),
        ),
    ],
    ids=("no-file", "malformed", "probe-fails", "no-interpreter"),
)
def test_the_park_marker_names_the_fact_the_probe_actually_established(
    tmp_path: Path,
    prepare: Callable[[Path], dict[str, str]],
    reason_says: tuple[str, ...],
    journal_says: tuple[str, ...],
) -> None:
    """Every no-accessory-verdict route fails CLOSED — ``Config.from_env``
    raises on a malformed source list, and opening the gate on a file the
    daemon rejects crash-loops it into StartLimitAction=reboot.

    Parking is never the question; the reason is. This marker's text is read
    verbatim through /state.microphone.reason and the doctor headline, so only
    the route that actually checked may answer "no accessory microphone
    paired". "I could not tell" and "I checked and there is nothing" are
    different facts.

    The last two routes are pinned on the ``custom`` profile because that is
    the only shape that reaches stop_voice without a working interpreter — a
    managed profile parks earlier, on the mic-profile resolver.
    """
    _write_env(tmp_path, "udp:9876")
    extra_env = prepare(tmp_path)

    result = _run_reconcile(tmp_path, "--reason", "test", extra_env=extra_env)

    assert result.returncode == 0, result.stderr
    reason = _marker(tmp_path).read_text()
    for phrase in reason_says:
        assert phrase in reason
    if "no accessory microphone paired" not in reason_says:
        assert "no accessory microphone paired" not in reason
    for phrase in journal_says:
        assert phrase in result.stderr
    commands = _systemctl_log(tmp_path)
    assert "stop jasper-voice.service" in commands
    assert VOICE_RESTART_CMD not in commands


def test_accessory_mic_does_not_unpark_managed_xvf(tmp_path: Path) -> None:
    """Scope guard: park_managed_xvf stays accessory-blind on purpose.

    That path leaves JASPER_MIC_DEVICE on the AEC bridge's udp: transport while
    stop_disable_aec has just stopped the bridge. Starting voice there binds an
    unfed UDP socket and watchdog-restarts forever (park_managed_xvf owns why
    that loop never escalates to a reboot).

    Reached through the kept park — no eligible capture device at all, not a
    firmware or DAC disposition; those disclose and keep hearing.
    """
    _stage(tmp_path, "udp:9876", profile="xvf_chip_aec")
    _pair_remote(tmp_path)

    result = _run_reconcile(
        tmp_path,
        "--reason",
        "test",
        extra_env={"JASPER_MIC_PROFILE_PYTHON": str(tmp_path / "missing-python")},
    )

    assert result.returncode == 0, result.stderr
    assert _marker(tmp_path).exists()
    assert VOICE_RESTART_CMD not in _systemctl_log(tmp_path)


def _aec_disabled_direct_mic_box(tmp_path: Path) -> None:
    """The operator's opt-out on a non-XVF mic: `stop_aec_and_clear_legs`."""
    _stage(
        tmp_path,
        "UsbMic",
        extra="JASPER_MIC_DEVICE_CANDIDATES=UsbMic\n",
        mode="disabled",
        card="UsbMic",
        channels=2,
    )


def _two_channel_managed_xvf_box(tmp_path: Path) -> None:
    """A managed XVF below the 6-channel endpoint the bridge reads: the park."""
    _stage(tmp_path, "Array", profile="auto", channels=2)


def test_a_ready_pass_publishes_the_verdict_before_it_starts_the_bridge(
    tmp_path: Path,
) -> None:
    """The bridge's ConditionPathExists is evaluated by PID 1 when it runs the
    start job, and a condition-skipped restart still exits 0 — so a pass that
    restarted the bridge before publishing would silently leave it down."""
    _stage(
        tmp_path,
        "Array",
        extra="JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS=ready\n",
        profile="auto",
        channels=6,
    )
    witness, _ = _fake_systemctl(
        tmp_path, name="systemctl-witness", witness="JASPER_AEC_BRIDGE_READY_MARKER"
    )

    result = _run_reconcile(
        tmp_path, "--reason", "test", extra_env={"JASPER_SYSTEMCTL": str(witness)}
    )

    assert result.returncode == 0, result.stderr
    assert _ready_marker(tmp_path).exists()
    assert (
        "present=1 restart jasper-aec-bridge.service"
        in _systemctl_log(tmp_path).splitlines()
    )


@pytest.mark.parametrize(
    "stage",
    [_aec_disabled_direct_mic_box, _two_channel_managed_xvf_box],
    ids=("aec-disabled", "two-channel"),
)
def test_a_not_ready_pass_withdraws_the_verdict(
    tmp_path: Path, stage: Callable[[Path], None]
) -> None:
    """Both teardown shapes withdraw it — the direct-mic/opt-out route that
    clears the legs and the managed park — so nothing can restart the bridge
    back on top of the mic the pass just handed to jasper-voice."""
    _prepublish_ready_marker(tmp_path)
    stage(tmp_path)

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    assert not _ready_marker(tmp_path).exists()


def test_a_pass_that_dies_before_it_settles_withdraws_the_verdict(
    tmp_path: Path,
) -> None:
    """Fail closed, and with no exit trap: the pass withdraws before it starts
    re-deriving and only republishes where a verdict settles, so one that never
    settles leaves nothing standing for hardware nobody re-checked. Reached by
    making the env file unwritable (its parent is a regular file), which aborts
    the pass under `set -e` at its first env write."""
    _stage(tmp_path, "Array", profile="auto", channels=6)
    _prepublish_ready_marker(tmp_path)
    (tmp_path / "blocker").write_text("not a directory\n")

    result = _run_reconcile(
        tmp_path,
        "--reason",
        "test",
        extra_env={"JASPER_ENV_FILE": str(tmp_path / "blocker" / "jasper.env")},
    )

    assert result.returncode != 0
    assert not _ready_marker(tmp_path).exists()


def test_the_managed_aec3_fallback_publishes_the_verdict(tmp_path: Path) -> None:
    """A managed XVF whose chip leg the DAC gate refuses still needs the bridge:
    software AEC3 on the same UDP carrier IS the wake path there, so the
    disclose route publishes exactly like the chip route above."""
    _stage(
        tmp_path,
        "Array",
        extra=(
            "JASPER_AUDIO_DAC_ID=mystery_usb_audio\n"
            "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS=disclosed_stale\n"
        ),
        profile="auto",
        channels=6,
    )

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    assert _ready_marker(tmp_path).exists()


# ---------- Wake-detection leg mapping ------------------------------------
# The reconciler maps two booleans in aec_mode.env to three underlying
# env vars in jasper.env that the bridge + voice each read at startup.
# These tests pin the mapping + the "clear-on-bridge-off" behavior.


def _write_mode_with_legs(
    tmp_path: Path,
    mode: str = "auto",
    raw: str = "1",
    dtln: str = "0",
    chip_aec: str | None = None,
    chip_aec_150: str | None = None,
    chip_aec_210: str | None = None,
    chip_ref_observe: str | None = None,
) -> None:
    body = (
        "JASPER_AUDIO_INPUT_PROFILE=custom\n"
        f"JASPER_AEC_MODE={mode}\n"
        f"JASPER_WAKE_LEG_RAW={raw}\n"
        f"JASPER_WAKE_LEG_DTLN={dtln}\n"
    )
    # When chip_aec is None the key is omitted, so ensure_mode_file
    # appends the default (0) — exercising the pre-chip-AEC upgrade path.
    if chip_aec is not None:
        body += f"JASPER_WAKE_LEG_CHIP_AEC={chip_aec}\n"
    if chip_aec_150 is not None:
        body += f"JASPER_WAKE_LEG_CHIP_AEC_150={chip_aec_150}\n"
    if chip_aec_210 is not None:
        body += f"JASPER_WAKE_LEG_CHIP_AEC_210={chip_aec_210}\n"
    # Same upgrade-path contract for the opt-in observe key.
    if chip_ref_observe is not None:
        body += f"JASPER_AEC_CHIP_REF_OBSERVE={chip_ref_observe}\n"
    (tmp_path / "aec_mode.env").write_text(body)


def test_ensure_mode_file_seeds_every_documented_default(tmp_path: Path) -> None:
    """Fresh install (no aec_mode.env): the reconciler creates the file with
    the documented defaults (pinned against control's view in the next test)."""
    _write_env(tmp_path, "Array")

    _run_reconcile(tmp_path, "--reason", "test")

    seeded = _env_assignments(tmp_path / "aec_mode.env")
    assert {
        key: seeded.get(key)
        for key in (
            "JASPER_AUDIO_INPUT_PROFILE",
            "JASPER_AEC_MODE",
            "JASPER_WAKE_LEG_RAW",
            "JASPER_WAKE_LEG_DTLN",
            "JASPER_WAKE_LEG_CHIP_AEC",
            "JASPER_WAKE_LEG_CHIP_AEC_150",
            "JASPER_WAKE_LEG_CHIP_AEC_210",
            "JASPER_AEC_CHIP_REF_OBSERVE",
        )
    } == {
        "JASPER_AUDIO_INPUT_PROFILE": "auto",
        "JASPER_AEC_MODE": "auto",
        "JASPER_WAKE_LEG_RAW": "1",
        "JASPER_WAKE_LEG_DTLN": "0",
        "JASPER_WAKE_LEG_CHIP_AEC": "0",
        "JASPER_WAKE_LEG_CHIP_AEC_150": "0",
        "JASPER_WAKE_LEG_CHIP_AEC_210": "0",
        "JASPER_AEC_CHIP_REF_OBSERVE": "0",
    }


@pytest.mark.parametrize("existing_mode", [None, "JASPER_AEC_MODE=disabled\n"])
def test_reconciler_leg_defaults_match_control_fallback(
    tmp_path: Path,
    existing_mode: str | None,
) -> None:
    """Fresh and upgrade seeds must match control's pre-reconcile view.

    ``jasper-control`` can read a missing or partial mode file before the
    reconciler has seeded its keys. A drift here would make the API report
    different operator intent from the state the reconciler subsequently
    persists and applies.
    """
    if existing_mode is not None:
        (tmp_path / "aec_mode.env").write_text(existing_mode, encoding="utf-8")
    _write_env(tmp_path, "Array")

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    actual = _env_assignments(tmp_path / "aec_mode.env")
    expected = _control_leg_defaults()
    assert {key: actual.get(key) for key in expected} == expected


def test_reconcile_preserves_existing_mode_file_dir_mode(tmp_path: Path) -> None:
    """The reconciler must NOT re-chmod an existing /var/lib/jasper.

    /var/lib/jasper is 0770 root:jasper (ensure_state_dir) so the now-non-root
    daemons can write group-shared state. The mode-file seed (and the shared
    jasper-env-file.sh writer) re-moded the dir to 0755 on every boot/udev
    reconcile, stripping that group-write bit — the same class as #827, two
    sibling sites away. Pin that a pre-created 0770 dir survives a reconcile
    that seeds the mode file into it.
    """
    state_dir = tmp_path / "var-lib-jasper"
    state_dir.mkdir()
    state_dir.chmod(0o770)
    _write_env(tmp_path, "Array")

    result = _run_reconcile(
        tmp_path,
        "--reason",
        "test",
        extra_env={"JASPER_AEC_MODE_FILE": str(state_dir / "aec_mode.env")},
    )

    assert result.returncode == 0, result.stderr
    assert (state_dir / "aec_mode.env").exists()  # seeded into the dir
    assert oct(state_dir.stat().st_mode & 0o777) == "0o770"


def test_reconcile_keeps_jasper_env_group_readable(tmp_path: Path) -> None:
    """jasper-control fresh-reads jasper.env after AEC reconciles.

    The install migration sets /etc/jasper/jasper.env to root:jasper 0640.
    Reconciler rewrites must keep the group-read bit; otherwise /state.aec
    falls back to jasper-control's stale startup environment and reports
    chip-AEC as pending after the runtime env has actually been applied.
    """
    env_file = _stage(tmp_path, "Array", mode="auto", channels=6)

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    assert "JASPER_MIC_DEVICE=udp:9876" in env_file.read_text()
    assert oct(env_file.stat().st_mode & 0o777) == "0o640"


@pytest.mark.parametrize(
    ("existing", "expected"),
    [
        (
            "JASPER_AEC_MODE=disabled\n",
            {
                "JASPER_AEC_MODE": "disabled",
                "JASPER_WAKE_LEG_RAW": "1",
                "JASPER_WAKE_LEG_DTLN": "0",
                "JASPER_AUDIO_INPUT_PROFILE": "direct_mic",
            },
        ),
        (
            "JASPER_AEC_MODE=auto\n"
            "JASPER_WAKE_LEG_RAW=1\n"
            "JASPER_WAKE_LEG_DTLN=1\n",
            {
                "JASPER_WAKE_LEG_DTLN": "1",
                "JASPER_WAKE_LEG_CHIP_AEC": "0",
                "JASPER_WAKE_LEG_CHIP_AEC_150": "0",
                "JASPER_WAKE_LEG_CHIP_AEC_210": "0",
                # raw+DTLN is not a selectable product profile.
                "JASPER_AUDIO_INPUT_PROFILE": "custom",
            },
        ),
        (
            "JASPER_AEC_MODE=auto\n"
            "JASPER_WAKE_LEG_RAW=1\n"
            "JASPER_WAKE_LEG_DTLN=0\n"
            "JASPER_WAKE_LEG_CHIP_AEC=0\n",
            {
                "JASPER_WAKE_LEG_RAW": "1",
                "JASPER_AEC_CHIP_REF_OBSERVE": "0",
            },
        ),
    ],
    ids=("pre-leg-toggle", "pre-chip-aec", "pre-chip-ref-observe"),
)
def test_ensure_mode_file_appends_missing_keys_and_keeps_the_rest(
    tmp_path: Path, existing: str, expected: dict[str, str]
) -> None:
    """Each upgrade shape a deployed box can arrive in: the reconciler appends
    what the build added, preserves what the operator set, and re-derives the
    profile from the resulting leg set."""
    (tmp_path / "aec_mode.env").write_text(existing)
    _write_env(tmp_path, "Array")

    _run_reconcile(tmp_path, "--reason", "test")

    actual = _env_assignments(tmp_path / "aec_mode.env")
    assert {key: actual.get(key) for key in expected} == expected


def test_fresh_auto_profile_uses_chip_aec_on_supported_6ch_xvf(tmp_path: Path) -> None:
    """A truly fresh aec_mode.env defaults to the canonical auto profile.
    On the recommended 6-channel XVF3800 shape plus a measured output DAC
    profile, that resolves to chip-AEC rather than stacked software legs."""
    _stage(
        tmp_path,
        "Array",
        extra="JASPER_AUDIO_DAC_ID=apple_usb_c_dongle\n",
        channels=6,
    )

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    mode = (tmp_path / "aec_mode.env").read_text()
    assert "JASPER_AUDIO_INPUT_PROFILE=auto" in mode
    body = (tmp_path / "jasper.env").read_text()
    assert "JASPER_MIC_DEVICE=udp:9876" in body
    assert "JASPER_AEC_CHIP_AEC_ENABLED=1" in body
    assert "JASPER_MIC_DEVICE_CHIP_AEC_150=udp:" not in body
    assert "JASPER_MIC_DEVICE_CHIP_AEC_210=udp:" not in body
    assert "JASPER_MIC_DEVICE_RAW=udp:" not in body
    assert "JASPER_MIC_DEVICE_DTLN=udp:" not in body


def test_raw_usb_export_intent_survives_reconcile_without_changing_managed_xvf_voice(
    tmp_path: Path,
) -> None:
    """USB-only raw intent cannot weaken the managed chip-AEC voice profile."""

    usb_mic_intent = tmp_path / "usb_mic.env"
    write_usb_mic_enabled(True, usb_mic_intent)
    write_usb_mic_leg(USB_MIC_RAW_XVF_LEG, usb_mic_intent)
    _stage(tmp_path, "Array", channels=6)

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    assert usb_mic_enabled(usb_mic_intent) is True
    assert read_usb_mic_leg(usb_mic_intent) == USB_MIC_RAW_XVF_LEG
    body = (tmp_path / "jasper.env").read_text()
    assert "JASPER_MIC_DEVICE=udp:9876" in body
    assert "JASPER_AEC_CHIP_AEC_ENABLED=1" in body
    assert "JASPER_MIC_DEVICE_RAW=udp:" not in body
    assert "JASPER_MIC_DEVICE_DTLN=udp:" not in body
    assert "JASPER_MIC_DEVICE_CHIP_AEC_150=udp:" not in body
    assert "JASPER_MIC_DEVICE_CHIP_AEC_210=udp:" not in body


def test_mic_profile_resolver_failure_clears_stale_chip_support(
    tmp_path: Path,
) -> None:
    """The resolver owns geometry truth; stale JASPER_XVF_* env must not
    keep chip-AEC armed when the resolver is unavailable."""
    _stage(
        tmp_path,
        "Array",
        extra=(
            "JASPER_AUDIO_DAC_ID=apple_usb_c_dongle\n"
            "JASPER_XVF_VARIANT=xvf3800_legacy_square_6ch\n"
            "JASPER_XVF_GEOMETRY=square\n"
            "JASPER_XVF_CHIP_BEAM_PLAN=xvf_square_fixed_150_210\n"
            "JASPER_XVF_CHIP_AEC_SUPPORTED=1\n"
        ),
        channels=6,
    )

    result = _run_reconcile(
        tmp_path,
        "--reason",
        "test",
        extra_env={"JASPER_MIC_PROFILE_PYTHON": str(tmp_path / "missing-python")},
    )

    assert result.returncode == 0, result.stderr
    body = (tmp_path / "jasper.env").read_text()
    assert "JASPER_XVF_CHIP_AEC_SUPPORTED=0" in body
    assert "JASPER_XVF_CHIP_BEAM_PLAN=''" in body
    assert "JASPER_AEC_CHIP_AEC_ENABLED=0" in body
    assert "JASPER_MIC_DEVICE_CHIP_AEC_150=udp:" not in body
    # Disarmed, not deafened: the 6-channel mic still carries software AEC3.
    # The alignment record is NOT rewritten — with no interpreter its vocabulary
    # is unresolvable, so the last one stands (ADR-0101).
    assert f"JASPER_MIC_DEVICE_RAW={_RAW_PORT}" in body
    assert "JASPER_MIC_DEVICE=udp:9876" in body
    assert not _marker(tmp_path).exists()


def _broken_dac_policy_gate(tmp_path: Path) -> Path:
    """A resolver whose mic profile still answers but whose DAC policy query
    fails — the shape the runtime-env carry exists for."""
    return _write_synthetic_xvf_resolver(
        tmp_path,
        "Array",
        chip_beam_plan="xvf_square_fixed_150_210",
        chip_aec_supported="1",
        policy_exit=1,
    )


@pytest.mark.parametrize(
    "selection", ["auto", "xvf_chip_aec", "xvf_chip_aec_testing"]
)
def test_an_unevaluable_dac_gate_carries_the_last_resolved_verdict(
    tmp_path: Path, selection: str
) -> None:
    """ADR-0101: an unmeasured gate is not a "no".

    A resolver that cannot answer must not knock a commissioned box off
    chip-AEC; the verdict it last resolved for this same DAC stands, and the
    disclosure says it is carried. The managed path always queries the
    PRODUCTION gate, even under the testing alias, so a record keyed to one
    selection alone would leave the other nothing to carry.
    """
    env_file = _stage(
        tmp_path,
        "Array",
        extra="JASPER_AUDIO_DAC_ID=apple_usb_c_dongle\n",
        profile=selection,
        channels=6,
    )

    first = _run_reconcile(tmp_path, "--reason", "test")
    assert first.returncode == 0, first.stderr
    assert "JASPER_AEC_CHIP_AEC_DAC_STATUS=approved" in env_file.read_text()

    second = _run_reconcile(
        tmp_path,
        "--reason",
        "test",
        extra_env={
            "JASPER_MIC_PROFILE_PYTHON": str(_broken_dac_policy_gate(tmp_path))
        },
    )

    assert second.returncode == 0, second.stderr
    body = env_file.read_text()
    assert "JASPER_AEC_CHIP_AEC_DAC_STATUS=approved" in body
    assert "JASPER_AEC_CHIP_AEC_DAC_SOURCE=runtime_env_carried" in body
    # The carried verdict is what keeps the chip leg armed.
    assert "JASPER_AEC_CHIP_AEC_ENABLED=1" in body
    assert "JASPER_MIC_DEVICE=udp:9876" in body
    assert not _marker(tmp_path).exists()


def test_a_status_only_record_still_carries(
    tmp_path: Path,
) -> None:
    """The status IS the verdict: `approved` is what the automatic profile arms on.

    Reading a carried record as not-permitted would be the exact drop the carry
    exists to prevent, and would then persist that contradiction.
    """
    env_file = _stage(
        tmp_path,
        "Array",
        extra=(
            "JASPER_AUDIO_DAC_ID=apple_usb_c_dongle\n"
            "JASPER_AEC_CHIP_AEC_DAC_ID=apple_usb_c_dongle\n"
            "JASPER_AEC_CHIP_AEC_DAC_STATUS=approved\n"
            "JASPER_AEC_CHIP_AEC_DAC_SOURCE=static\n"
            "JASPER_AEC_CHIP_AEC_DAC_DETAIL='approved for production chip-AEC'\n"
        ),
        profile="auto",
        channels=6,
    )

    result = _run_reconcile(
        tmp_path,
        "--reason",
        "test",
        extra_env={
            "JASPER_MIC_PROFILE_PYTHON": str(_broken_dac_policy_gate(tmp_path))
        },
    )

    assert result.returncode == 0, result.stderr
    body = env_file.read_text()
    assert "JASPER_AEC_CHIP_AEC_DAC_STATUS=approved" in body
    assert "JASPER_AEC_CHIP_AEC_DAC_SOURCE=runtime_env_carried" in body
    assert "JASPER_AEC_CHIP_AEC_ENABLED=1" in body


@pytest.mark.parametrize(
    ("profile", "dac_id", "stderr_phrase"),
    [
        (None, "hifiberry_dac8x_studio", "HiFiBerry DAC8x Studio needs per-profile"),
        (None, "mystery_usb_audio", "has no codified chip-AEC calibration"),
        ("xvf_chip_aec", "dual_apple_usb_c_dac_4ch", "measured-sync contract"),
        # The managed UI testing alias is not the low-level custom escape hatch.
        ("xvf_chip_aec_testing", "mystery_usb_audio", ""),
    ],
    ids=("auto-studio", "auto-uncodified", "explicit-chip", "testing-alias"),
)
def test_an_uncodified_output_dac_discloses_and_runs_software_aec3(
    tmp_path: Path,
    profile: str | None,
    dac_id: str,
    stderr_phrase: str,
) -> None:
    """ADR-0101: the DAC gate is a quality signal, not an admission gate.

    Uncodified output timing keeps chip-AEC unselected under every managed
    selection — including the explicit chip profile and its testing alias —
    but the 6-channel mic still carries software AEC3 and the box discloses
    what it lost instead of parking the voice stack deaf.
    """
    _stage(
        tmp_path,
        "Array",
        extra=f"JASPER_AUDIO_DAC_ID={dac_id}\n",
        profile=profile,
        channels=6,
    )

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    if stderr_phrase:
        assert stderr_phrase in result.stderr
    body = (tmp_path / "jasper.env").read_text()
    # The software-AEC3 leg shape, on the bridge's own UDP carrier.
    assert "JASPER_AEC_CHIP_AEC_ENABLED=0" in body
    assert "JASPER_MIC_DEVICE_RAW=udp:9877" in body
    assert "JASPER_MIC_DEVICE_CHIP_AEC_150=udp:" not in body
    assert "JASPER_MIC_DEVICE_CHIP_AEC_210=udp:" not in body
    assert "JASPER_MIC_DEVICE=udp:9876" in body
    assert "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS=disclosed_stale" in body
    assert "JASPER_AEC_CHIP_AEC_ALIGNMENT_ACTION='Run sudo jasper-aec-commission'" in body
    assert not _marker(tmp_path).exists()
    assert VOICE_RESTART_CMD in _systemctl_log(tmp_path)


@pytest.mark.parametrize(
    "profile",
    [
        "auto",
        "xvf_chip_aec",
        "xvf_chip_aec_testing",
        "xvf_software_aec3",
        "direct_mic",
    ],
)
def test_present_managed_xvf_overrides_stale_non_owned_mic_device_for_every_profile(
    tmp_path: Path,
    profile: str,
) -> None:
    """A stale custom-looking device must not bypass managed chip policy."""
    env_file = _stage(
        tmp_path,
        "UMIK-2",
        extra="JASPER_AUDIO_DAC_ID=apple_usb_c_dongle\n",
        profile=profile,
        channels=6,
    )

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    body = env_file.read_text()
    assert "custom JASPER_MIC_DEVICE=UMIK-2" not in result.stderr
    assert "JASPER_MIC_DEVICE=udp:9876" in body
    assert "JASPER_AEC_CHIP_AEC_ENABLED=1" in body
    assert "JASPER_MIC_DEVICE_RAW=udp:" not in body


def test_resolver_discovered_future_xvf_reaches_managed_policy(
    tmp_path: Path,
) -> None:
    """A new resolver-known card needs no matching shell registry edit."""
    env_file = _stage(
        tmp_path, "operator-mic", profile="auto", card="FutureXvf", channels=6
    )
    resolver = _write_synthetic_xvf_resolver(tmp_path, "FutureXvf")

    result = _run_reconcile(
        tmp_path,
        "--reason",
        "test",
        extra_env={"JASPER_MIC_PROFILE_PYTHON": str(resolver)},
    )

    assert result.returncode == 0, result.stderr
    body = env_file.read_text()
    assert "custom JASPER_MIC_DEVICE=operator-mic" not in result.stderr
    assert "JASPER_XVF_PRESENT=1" in body
    assert "JASPER_XVF_ALSA_CARD=FutureXvf" in body
    assert "JASPER_AEC_MIC_DEVICE=FutureXvf" in body
    # No production beam plan, so no chip beams — but the mic is 6-channel, so
    # software AEC3 carries the wake path and the reason reaches the doctor.
    assert "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS=disclosed_stale" in body
    reason = _env_assignments(env_file)["JASPER_AEC_CHIP_AEC_ALIGNMENT_REASON"]
    assert reason == "'future XVF needs a validated beam plan'"
    assert "JASPER_AEC_CHIP_AEC_ENABLED=0" in body
    assert "JASPER_MIC_DEVICE_RAW=udp:9877" in body
    assert "JASPER_MIC_DEVICE=udp:9876" in body
    assert not _marker(tmp_path).exists()
    assert VOICE_RESTART_CMD in _systemctl_log(tmp_path)


def test_explicit_chip_profile_uses_static_hifiberry_known_good(
    tmp_path: Path,
) -> None:
    """JTS3 path: measured HiFiBerry DAC8x hardware is codified known-good.

    It must not depend on outputd's live SRO verdict at reconcile time; that
    verdict is useful observability, but it is too noisy to be the boot gate for
    hardware we have already approved.
    """
    _stage(
        tmp_path,
        "udp:9876",
        extra="JASPER_AUDIO_DAC_ID=hifiberry_dac8x\n",
        profile="xvf_chip_aec",
        channels=6,
    )

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    assert "outputd aec_clock permits chip-AEC" not in result.stderr
    body = (tmp_path / "jasper.env").read_text()
    assert "JASPER_MIC_DEVICE=udp:9876" in body
    assert "JASPER_AEC_CHIP_AEC_ENABLED=1" in body
    assert "JASPER_MIC_DEVICE_CHIP_AEC_150=udp:" not in body
    assert "JASPER_MIC_DEVICE_CHIP_AEC_210=udp:" not in body
    assert "JASPER_MIC_DEVICE_RAW=udp:" not in body
    assert "JASPER_OUTPUTD_CHIP_REF_PCM=hw:CARD=Array,DEV=0" in body
    assert "JASPER_OUTPUTD_CHIP_REF_OBSERVE=0" in body


def test_custom_chip_leg_is_honoured_on_an_unapproved_dac(
    tmp_path: Path,
) -> None:
    """ADR-0101: the operator's explicit leg survives an uncodified DAC.

    The gate is a quality signal here, so the leg stands and the log line is
    the disclosure. The DAC's own verdict still reaches the status surfaces.
    """
    _write_env(tmp_path, "Array", extra="JASPER_AUDIO_DAC_ID=mystery_usb_audio\n")
    _write_mode_with_legs(tmp_path, mode="auto", raw="0", dtln="0", chip_aec="1")
    _write_card(tmp_path, channels=6)

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    assert "custom chip-AEC leg requested" in result.stderr
    body = (tmp_path / "jasper.env").read_text()
    assert "JASPER_AEC_CHIP_AEC_ENABLED=1" in body
    assert "JASPER_MIC_DEVICE_RAW=udp:" not in body
    assert "JASPER_AEC_CHIP_AEC_DAC_STATUS=needs_calibration" in body


def test_auto_profile_does_not_promote_uncodified_dac_from_short_clock_sample(
    tmp_path: Path,
) -> None:
    """Clock telemetry stays diagnostic; the fixed DAC registry authorizes."""
    _stage(
        tmp_path,
        "udp:9876",
        extra="JASPER_AUDIO_DAC_ID=mystery_usb_audio\n",
        channels=6,
    )

    with JsonStatusSocket(
        _outputd_status_payload(verdict="coherent", status="locked"),
        name="outputd.sock",
    ) as socket_path:
        result = _run_reconcile(
            tmp_path,
            "--reason",
            "test",
            extra_env={"JASPER_OUTPUTD_CONTROL_SOCKET": str(socket_path)},
        )

    assert result.returncode == 0, result.stderr
    body = (tmp_path / "jasper.env").read_text()
    assert "JASPER_AEC_CHIP_AEC_ENABLED=0" in body
    assert "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS=disclosed_stale" in body
    assert "JASPER_MIC_DEVICE_CHIP_AEC_150=udp:" not in body


def test_explicit_chip_profile_does_not_arm_for_compensable_verdict(
    tmp_path: Path,
) -> None:
    """A measured but drifting DAC needs the deferred rate-match layer, so
    `compensable` stays unarmed pending a supported product path — on software
    AEC3, not parked."""
    _stage(
        tmp_path,
        "udp:9876",
        extra="JASPER_AUDIO_DAC_ID=mystery_usb_audio\n",
        profile="xvf_chip_aec",
        channels=6,
    )

    with JsonStatusSocket(
        _outputd_status_payload(verdict="compensable", status="locked"),
        name="outputd.sock",
    ) as socket_path:
        result = _run_reconcile(
            tmp_path,
            "--reason",
            "test",
            extra_env={"JASPER_OUTPUTD_CONTROL_SOCKET": str(socket_path)},
        )

    assert result.returncode == 0, result.stderr
    assert "verdict=compensable" in result.stderr
    body = (tmp_path / "jasper.env").read_text()
    assert "JASPER_AEC_CHIP_AEC_ENABLED=0" in body
    assert "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS=disclosed_stale" in body
    assert "JASPER_MIC_DEVICE_CHIP_AEC_150=udp:" not in body


# Every managed selection lands on the same chip-AEC vector once the hardware
# supports it; the profile is intent, and the detected mic decides.
_CHIP_AEC_VECTOR = {"mic": "udp:9876", "chip_enabled": "1"}


@pytest.mark.parametrize(
    ("profile", "channels", "expected"),
    [
        ("auto", 6, _CHIP_AEC_VECTOR),
        ("xvf_chip_aec", 6, _CHIP_AEC_VECTOR),
        ("xvf_software_aec3", 6, _CHIP_AEC_VECTOR),
        ("direct_mic", 6, _CHIP_AEC_VECTOR),
        ("auto", 2, {"mic": "Array", "chip_enabled": "0"}),
    ],
)
def test_profile_env_updates_are_consumed_by_reconciler(
    tmp_path: Path,
    profile: str,
    channels: int,
    expected: dict[str, object],
) -> None:
    """Pin the Python profile writer to the Bash runtime policy.

    `jasper.audio_profile_state.profile_env_updates()` is what the control
    API writes, while `jasper-aec-reconcile` is what applies the runtime
    env. This test catches drift between the two implementations before a
    new profile or alias ships with mismatched Python/Bash behavior.
    """
    env_file = _stage(
        tmp_path,
        "Array",
        extra="JASPER_AUDIO_DAC_ID=apple_usb_c_dongle\n",
        profile=profile,
        channels=channels,
    )

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    values = _env_assignments(env_file)
    assert values["JASPER_MIC_DEVICE"] == expected["mic"]
    assert values["JASPER_MIC_DEVICE_RAW"] == _EMPTY
    assert values["JASPER_MIC_DEVICE_DTLN"] == _EMPTY
    assert values["JASPER_AEC_CHIP_AEC_ENABLED"] == expected["chip_enabled"]
    assert values["JASPER_AEC_REF_SOURCE"] == "outputd_udp"


# The shell variables the reconciler evals the shim's output into, named here
# rather than imported so a rename in the shim shows up as a failure. Seeded
# with a sentinel, so a profile that emits no vector is visibly left alone.
_SHIM_SENTINEL = "unchanged"
_SHIM_LEG_VARS = (
    "AEC_MODE",
    "LEG_RAW",
    "LEG_DTLN",
    "LEG_CHIP_AEC",
    "LEG_CHIP_AEC_150",
    "LEG_CHIP_AEC_210",
)
_SHIM_VARS = ("AUDIO_INPUT_PROFILE", *_SHIM_LEG_VARS)
_SHIM_ENV_KEYS = (
    "JASPER_AEC_MODE",
    "JASPER_WAKE_LEG_RAW",
    "JASPER_WAKE_LEG_DTLN",
    "JASPER_WAKE_LEG_CHIP_AEC",
    "JASPER_WAKE_LEG_CHIP_AEC_150",
    "JASPER_WAKE_LEG_CHIP_AEC_210",
)
# profile -> (normalized name, effective profile without chip-AEC, with it).
# `None` is "emits no vector"; the vectors themselves come from
# profile_env_updates, so drift in that table fails here.
_PROFILE_VECTORS = {
    "auto": ("auto", "xvf_software_aec3", "xvf_chip_aec"),
    "xvf_chip_aec": ("xvf_chip_aec", "xvf_software_aec3", "xvf_chip_aec"),
    "xvf_chip_aec_testing": (
        "xvf_chip_aec_testing",
        "xvf_software_aec3",
        "xvf_chip_aec",
    ),
    "xvf_chip_aec_test": ("xvf_chip_aec_testing", "xvf_software_aec3", "xvf_chip_aec"),
    "xvf_software_aec3": ("xvf_software_aec3", "xvf_software_aec3", "xvf_software_aec3"),
    "direct_mic": ("direct_mic", "direct_mic", "direct_mic"),
    "custom": ("custom", None, None),
}


def _expected_legs(effective: str | None) -> tuple[str, ...]:
    if effective is None:
        return (_SHIM_SENTINEL,) * len(_SHIM_LEG_VARS)
    updates = profile_env_updates(effective)
    return tuple(updates[key] for key in _SHIM_ENV_KEYS)


def _eval_shim(*shim_args: str) -> dict[str, str]:
    """Run the shim behind the same `eval` the reconciler uses."""
    shim = shlex.join(
        [sys.executable, "-m", "jasper.cli.audio_input_profile", *shim_args]
    )
    script = (
        "".join(f"{name}={_SHIM_SENTINEL}\n" for name in _SHIM_VARS)
        + f'eval "$({shim})"\n'
        + "".join(f'printf "%s=%s\\n" {name} "${name}"\n' for name in _SHIM_VARS)
    )
    shell = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        check=False,
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    assert shell.returncode == 0, shell.stderr
    return dict(line.split("=", 1) for line in shell.stdout.splitlines())


@pytest.mark.parametrize("chip_available", ("0", "1"))
@pytest.mark.parametrize("profile", (*ALL_PROFILES, "xvf_chip_aec_test"))
def test_reconciler_evals_the_python_profile_tables(
    profile: str,
    chip_available: str,
) -> None:
    """The reconciler carries no profile vocabulary of its own.

    `jasper.audio_profile_state` owns the alias table and the profile ->
    wake-leg vectors; the shell evals what `jasper.cli.audio_input_profile`
    prints into exactly these variables. Driving that eval the way the script
    does pins both sides to one table — `xvf_chip_aec_test` is the alias
    Python accepted and Bash demoted to `custom`. A profile with no row here
    fails on the lookup, so a new one cannot ship untested.
    """
    normalized, without_chip, with_chip = _PROFILE_VECTORS[profile]
    effective = with_chip if chip_available == "1" else without_chip

    assert _eval_shim(
        f"--profile={profile}", f"--chip-available={chip_available}"
    ) == dict(zip(_SHIM_VARS, (normalized, *_expected_legs(effective))))


def test_chip_aec_test_alias_reaches_the_testing_profile(tmp_path: Path) -> None:
    """The alias Bash used to demote to `custom` now arms the testing profile."""
    _write_env(tmp_path, "Array")
    (tmp_path / "aec_mode.env").write_text(
        "JASPER_AUDIO_INPUT_PROFILE=xvf_chip_aec_test\n"
        "JASPER_AEC_MODE=auto\n"
        "JASPER_WAKE_LEG_RAW=1\n"
        "JASPER_WAKE_LEG_DTLN=0\n"
        "JASPER_WAKE_LEG_CHIP_AEC=0\n"
        "JASPER_WAKE_LEG_CHIP_AEC_150=0\n"
        "JASPER_WAKE_LEG_CHIP_AEC_210=0\n"
    )
    _write_card(tmp_path, channels=6)

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    values = _env_assignments(tmp_path / "jasper.env")
    assert values["JASPER_AEC_CHIP_AEC_TESTING_REQUESTED"] == "1"
    assert values["JASPER_AEC_CHIP_AEC_ENABLED"] == "1"


@pytest.mark.parametrize(
    ("selection", "carried"),
    [("auto", True), ("xvf_chip_aec_test", False), ("bogus", False)],
)
def test_resolver_down_carries_routable_selections_and_demotes_the_rest(
    tmp_path: Path,
    selection: str,
    carried: bool,
) -> None:
    """With no interpreter the script cannot resolve an alias or a typo, so it
    carries only a name the vocabulary itself has. A carried name still routes
    through managed profile policy; anything else is `custom`, which keeps that
    policy off and the operator's legs as written.

    The alignment record is carried the same way (ADR-0101): with no
    interpreter this pass measured nothing, so the last record must stand
    rather than be overwritten with a blank or a guess.
    """
    stale = alignment_health(
        chip_aec_health.COMMISSION_REQUIRED, selection="xvf_chip_aec"
    )
    env_file = _write_env(tmp_path, "Array", extra=stale.to_shell())
    (tmp_path / "aec_mode.env").write_text(
        f"JASPER_AUDIO_INPUT_PROFILE={selection}\n"
        "JASPER_AEC_MODE=auto\n"
        "JASPER_WAKE_LEG_RAW=0\n"
        "JASPER_WAKE_LEG_DTLN=1\n"
        "JASPER_WAKE_LEG_CHIP_AEC=0\n"
        "JASPER_WAKE_LEG_CHIP_AEC_150=0\n"
        "JASPER_WAKE_LEG_CHIP_AEC_210=0\n"
    )
    _write_card(tmp_path, channels=6)

    result = _run_reconcile(
        tmp_path,
        "--reason",
        "test",
        extra_env={"JASPER_MIC_PROFILE_PYTHON": str(tmp_path / "missing-python")},
    )

    assert result.returncode == 0, result.stderr
    values = _env_assignments(env_file)
    record = parse_env_file(str(env_file))
    assert {key: record.get(key) for key in chip_aec_health.ENV_KEYS} == stale.to_env()
    if carried:
        # Managed policy overrode the operator's legs onto software AEC3.
        assert values["JASPER_MIC_DEVICE_RAW"] == _RAW_PORT
        assert values["JASPER_MIC_DEVICE_DTLN"] == _EMPTY
    else:
        assert values["JASPER_MIC_DEVICE_RAW"] == _EMPTY
        assert values["JASPER_MIC_DEVICE_DTLN"] == _DTLN_PORT


_RAW_PORT = f"udp:{wake_legs.by_token('off').udp_port}"
_DTLN_PORT = f"udp:{wake_legs.by_token('dtln').udp_port}"


@pytest.mark.parametrize(
    ("raw", "dtln", "expected"),
    [
        # The default dual-stream OSS config.
        ("1", "0", {"raw": _RAW_PORT, "dtln": _EMPTY, "enabled": "0"}),
        # The opt-in 2 GB Pi config.
        ("1", "1", {"raw": _RAW_PORT, "dtln": _DTLN_PORT, "enabled": "1"}),
        # The 1 GB Pi minimum, when an operator opts out of the default.
        ("0", "0", {"raw": _EMPTY, "dtln": _EMPTY, "enabled": "0"}),
        # Unusual but valid: primary AEC3 + tertiary DTLN, no chip-direct.
        ("0", "1", {"raw": _EMPTY, "dtln": _DTLN_PORT, "enabled": "1"}),
        # Hand-edited booleans normalise; the wizard only ever writes 1/0.
        ("yes", "true", {"raw": _RAW_PORT, "dtln": _DTLN_PORT, "enabled": "1"}),
    ],
    ids=("dual", "triple", "single", "dtln-only", "hand-edited"),
)
def test_the_wake_leg_booleans_map_to_the_udp_leg_devices(
    tmp_path: Path, raw: str, dtln: str, expected: dict[str, str]
) -> None:
    """Each leg var is always written, empty when its leg is off: this
    reconciler is their only writer, and a stale ``JASPER_MIC_DEVICE_RAW=udp:``
    leaves voice's secondary capture spinning on a port nobody feeds."""
    _write_env(tmp_path, "udp:9876", extra="JASPER_AUDIO_DAC_ID=apple_usb_c_dongle\n")
    _write_mode_with_legs(tmp_path, mode="auto", raw=raw, dtln=dtln)
    _write_card(tmp_path, channels=6)

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    values = _env_assignments(tmp_path / "jasper.env")
    assert values["JASPER_MIC_DEVICE"] == f"udp:{wake_legs.by_token('on').udp_port}"
    assert values["JASPER_MIC_DEVICE_RAW"] == expected["raw"]
    assert values["JASPER_MIC_DEVICE_DTLN"] == expected["dtln"]
    assert values["JASPER_AEC_DTLN_ENABLED"] == expected["enabled"]


@pytest.mark.parametrize("chip_aec", [None, "1"], ids=("software-legs", "chip-leg"))
def test_aec_disabled_clears_every_leg_and_keeps_the_operator_booleans(
    tmp_path: Path, chip_aec: str | None
) -> None:
    """Disabled clears the software legs AND the chip vars, whatever the
    booleans say — a leg pointing at a bridge that is not running leaves voice
    retrying a dead port. The booleans stay in the mode file so re-enabling
    applies them again on the next pass.
    """
    _write_env(tmp_path, "Array")
    _write_mode_with_legs(
        tmp_path, mode="disabled", raw="1", dtln="1", chip_aec=chip_aec
    )

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    body = (tmp_path / "jasper.env").read_text()
    assert "JASPER_MIC_DEVICE_RAW=udp:" not in body
    assert "JASPER_MIC_DEVICE_DTLN=udp:" not in body
    assert "JASPER_AEC_DTLN_ENABLED=1" not in body
    assert "JASPER_MIC_DEVICE_CHIP_AEC_150=udp:" not in body
    assert "JASPER_MIC_DEVICE_CHIP_AEC_210=udp:" not in body
    assert "JASPER_AEC_CHIP_AEC_ENABLED=1" not in body
    assert "JASPER_AEC_REF_SOURCE=outputd_udp" in body
    assert "JASPER_OUTPUTD_REFERENCE_UDP_TARGET=''" in body
    # No card staged, so no candidate mic; the custom "Array" device is
    # neither udp: nor unset, so the reconciler leaves it alone.
    assert _event_values(
        result.stderr, "aec_reconcile.no_candidate_mic", "current"
    ) == ["Array"]
    assert _event_values(
        result.stderr, "aec_reconcile.no_candidate_mic", "cleared"
    ) == ["0"]
    mode_values = _env_assignments(tmp_path / "aec_mode.env")
    assert mode_values["JASPER_WAKE_LEG_RAW"] == "1"
    assert mode_values["JASPER_WAKE_LEG_DTLN"] == "1"
    if chip_aec is not None:
        assert mode_values["JASPER_WAKE_LEG_CHIP_AEC"] == chip_aec


# ---------- Chip-AEC profile + optional beam legs -------------------------
# JASPER_WAKE_LEG_CHIP_AEC selects the chip-AEC profile carrier
# (JASPER_AEC_CHIP_AEC_ENABLED=1 and primary/session audio on :9876), while
# JASPER_WAKE_LEG_CHIP_AEC_150/_210 are independent advanced opt-ins for
# extra openWakeWord detector instances. Chip-AEC remains mutually exclusive
# with raw/DTLN (single-chip Option-A).


def test_chip_aec_on_sets_carrier_and_clears_raw_dtln(tmp_path: Path) -> None:
    """AEC auto + 6-ch + CHIP_AEC=1 sets the chip-AEC carrier but leaves
    extra chip-beam detector device vars empty unless their per-beam
    booleans are on."""
    _write_env(tmp_path, "udp:9876", extra="JASPER_AUDIO_DAC_ID=apple_usb_c_dongle\n")
    _write_mode_with_legs(tmp_path, mode="auto", raw="1", dtln="1", chip_aec="1")
    _write_card(tmp_path, channels=6)
    _run_reconcile(tmp_path, "--reason", "test")
    body = (tmp_path / "jasper.env").read_text()
    assert "JASPER_AEC_CHIP_AEC_ENABLED=1" in body
    assert "JASPER_MIC_DEVICE_CHIP_AEC_150=udp:" not in body
    assert "JASPER_MIC_DEVICE_CHIP_AEC_210=udp:" not in body
    assert "JASPER_AEC_REF_SOURCE=outputd_udp" in body
    assert "JASPER_AEC_OUTPUTD_REF_UDP_HOST=127.0.0.1" in body
    assert "JASPER_AEC_OUTPUTD_REF_UDP_PORT=9891" in body
    assert "JASPER_OUTPUTD_CHIP_REF_PCM=hw:CARD=Array,DEV=0" in body
    assert "JASPER_OUTPUTD_REFERENCE_UDP_TARGET=127.0.0.1:9891" in body
    assert "JASPER_OUTPUTD_CHIP_REF_SAMPLE_RATE=16000" in body
    assert "JASPER_OUTPUTD_CHIP_REF_PERIOD_FRAMES=128" in body
    assert "JASPER_OUTPUTD_CHIP_REF_BUFFER_FRAMES=256" in body
    assert "JASPER_MIC_DEVICE_RAW=udp:" not in body
    assert "JASPER_MIC_DEVICE_DTLN=udp:" not in body
    assert "JASPER_AEC_DTLN_ENABLED=1" not in body
    commands = _systemctl_log(tmp_path)
    assert "restart jasper-outputd.service" in commands


def test_chip_aec_extra_beam_toggles_set_chip_device_vars(tmp_path: Path) -> None:
    _write_env(
        tmp_path,
        "Array",
        extra="JASPER_AUDIO_DAC_ID=apple_usb_c_dongle\n",
        aec_port=None,
    )
    _write_mode_with_legs(
        tmp_path,
        mode="auto",
        raw="1",
        dtln="1",
        chip_aec="1",
        chip_aec_150="1",
        chip_aec_210="1",
    )
    _write_card(tmp_path, channels=6)

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    body = (tmp_path / "jasper.env").read_text()
    assert "JASPER_AEC_CHIP_AEC_ENABLED=1" in body
    assert f"JASPER_MIC_DEVICE=udp:{wake_legs.by_token('on').udp_port}" in body
    assert (
        f"JASPER_MIC_DEVICE_CHIP_AEC_150="
        f"udp:{wake_legs.by_token('chip_aec_150').udp_port}"
    ) in body
    assert (
        f"JASPER_MIC_DEVICE_CHIP_AEC_210="
        f"udp:{wake_legs.by_token('chip_aec_210').udp_port}"
    ) in body
    assert "JASPER_MIC_DEVICE_RAW=udp:" not in body
    assert "JASPER_MIC_DEVICE_DTLN=udp:" not in body


@pytest.mark.parametrize(
    "stale_card", ["", "JASPER_AEC_MIC_DEVICE=Array\n"], ids=("fresh", "stale-array")
)
def test_flex_linear_is_discovered_but_never_arms_the_square_chip_plan(
    tmp_path: Path, stale_card: str
) -> None:
    """Flex linear firmware enumerates as L16K6Ch, not Array. The reconciler
    selects the present Flex card — re-deriving a legacy Array pin left by the
    XVF it replaced — but refuses the legacy square 150/210 chip plan and falls
    back to software AEC3."""
    env_file = _stage(
        tmp_path,
        "udp:9876",
        extra=f"JASPER_AUDIO_DAC_ID=apple_usb_c_dongle\n{stale_card}",
        profile="auto",
        card="L16K6Ch",
        channels=6,
    )

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    body = env_file.read_text()
    assert "JASPER_AEC_MIC_DEVICE=L16K6Ch" in body
    assert "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS=disclosed_stale" in body
    assert "JASPER_XVF_VARIANT=xvf3800_flex_linear_6ch" in body
    assert "JASPER_XVF_GEOMETRY=linear" in body
    assert "JASPER_XVF_CHIP_AEC_SUPPORTED=0" in body
    assert "JASPER_AEC_CHIP_AEC_ENABLED=0" in body
    assert "JASPER_MIC_DEVICE_RAW=udp:9877" in body
    assert "JASPER_OUTPUTD_CHIP_REF_PCM=''" in body
    assert "aec_mic=L16K6Ch" in result.stderr
    assert ("old=Array new=L16K6Ch" in result.stderr) is bool(stale_card)


def test_profile_managed_mic_swap_rederives_stale_aec_card(
    tmp_path: Path,
) -> None:
    """Swapping Flex linear (L16K6Ch) for square/circular XVF (Array)
    must not leave the old card id pinned as the bridge capture device.

    The selected profile is product intent; the detected mic profile owns the
    concrete ALSA card in normal non-custom modes.
    """
    env_file = _stage(
        tmp_path,
        "udp:9876",
        extra=(
            "JASPER_AUDIO_DAC_ID=apple_usb_c_dongle\n"
            "JASPER_AEC_MIC_DEVICE=L16K6Ch\n"
            "JASPER_OUTPUTD_CHIP_REF_PCM=hw:CARD=L16K6Ch,DEV=0\n"
        ),
        profile="xvf_chip_aec",
        card="Array",
        channels=6,
    )

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    assert "event=aec_reconcile.aec_mic_device_rederived" in result.stderr
    assert "old=L16K6Ch new=Array" in result.stderr
    body = env_file.read_text()
    assert "JASPER_AEC_MIC_DEVICE=Array" in body
    assert "JASPER_MIC_DEVICE=udp:9876" in body
    assert "JASPER_AEC_CHIP_AEC_ENABLED=1" in body
    assert "JASPER_MIC_DEVICE_CHIP_AEC_150=udp:" not in body
    assert "JASPER_OUTPUTD_CHIP_REF_PCM=hw:CARD=Array,DEV=0" in body
    assert "hw:CARD=L16K6Ch,DEV=0" not in body


def test_custom_profile_preserves_hand_pinned_aec_card_and_chip_ref(
    tmp_path: Path,
) -> None:
    """`custom` is the low-level escape hatch.

    Even when the XVF profile detector sees a different supported card first,
    a custom profile must not rewrite the operator-pinned AEC capture card or
    its matching chip-reference PCM.
    """
    env_file = _write_env(
        tmp_path,
        "udp:9876",
        extra=(
            "JASPER_AUDIO_DAC_ID=apple_usb_c_dongle\n"
            "JASPER_AEC_MIC_DEVICE=L16K6Ch\n"
            "JASPER_OUTPUTD_CHIP_REF_PCM=hw:CARD=L16K6Ch,DEV=0\n"
        ),
    )
    _write_mode_with_legs(
        tmp_path,
        mode="auto",
        raw="1",
        dtln="0",
        chip_aec="0",
        chip_ref_observe="1",
    )
    _write_card(tmp_path, card="Array", channels=6)
    _write_card(tmp_path, card="L16K6Ch", channels=6)

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    assert "event=aec_reconcile.aec_mic_device_rederived" not in result.stderr
    assert "aec_mic=L16K6Ch" in result.stderr
    body = env_file.read_text()
    assert "JASPER_AEC_MIC_DEVICE=L16K6Ch" in body
    assert "JASPER_OUTPUTD_CHIP_REF_PCM=hw:CARD=L16K6Ch,DEV=0" in body
    assert "JASPER_MIC_DEVICE=udp:9876" in body
    assert "JASPER_MIC_DEVICE_RAW=udp:9877" in body


def test_chip_aec_comma_values_idempotent_across_runs(tmp_path: Path) -> None:
    """Two consecutive passes must converge: identical env file, no second
    outputd restart.

    bash 5.2's `printf %q` escapes commas, turning hw:CARD=Array,DEV=0 into
    hw:CARD=Array\\,DEV=0 — which systemd EnvironmentFile= reads literally and
    which breaks this script's own read-back, marking outputd for a restart on
    every pass.
    """
    _write_env(tmp_path, "udp:9876", extra="JASPER_AUDIO_DAC_ID=apple_usb_c_dongle\n")
    _write_mode_with_legs(tmp_path, mode="auto", raw="0", dtln="0", chip_aec="1")
    _write_card(tmp_path, channels=6)

    result = _run_reconcile(tmp_path, "--reason", "test")
    assert result.returncode == 0, result.stderr
    first_body = (tmp_path / "jasper.env").read_text()
    assert "JASPER_OUTPUTD_CHIP_REF_PCM=hw:CARD=Array,DEV=0" in first_body
    assert "restart jasper-outputd.service" in _systemctl_log(tmp_path)

    (tmp_path / "systemctl.log").unlink()
    result = _run_reconcile(tmp_path, "--reason", "test")
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "jasper.env").read_text() == first_body
    assert "restart jasper-outputd.service" not in _systemctl_log(tmp_path)


@pytest.mark.parametrize(
    "seeded_chip_producer",
    [
        "",
        # The leaving-chip-AEC-mode transition: the XVF USB-IN producer was
        # armed on the previous pass and has to be stood down.
        "JASPER_AEC_REF_SOURCE=outputd_udp\n"
        "JASPER_OUTPUTD_CHIP_REF_PCM=hw:CARD=Array,DEV=0\n"
        "JASPER_OUTPUTD_REFERENCE_UDP_TARGET=127.0.0.1:9891\n",
    ],
    ids=("fresh", "leaving-chip-mode"),
)
def test_chip_aec_off_stands_down_the_chip_producer_not_the_speaker_monitor(
    tmp_path: Path, seeded_chip_producer: str
) -> None:
    """Default software AEC: the chip vars and the XVF USB-IN reference go,
    but outputd's UDP speaker monitor stays — software AEC3 is what consumes
    it — as do the raw/DTLN legs."""
    _write_env(tmp_path, "udp:9876", extra=seeded_chip_producer)
    _write_mode_with_legs(tmp_path, mode="auto", raw="1", dtln="1", chip_aec="0")
    _write_card(tmp_path, channels=6)

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    body = (tmp_path / "jasper.env").read_text()
    assert "JASPER_MIC_DEVICE_CHIP_AEC_150=udp:" not in body
    assert "JASPER_MIC_DEVICE_CHIP_AEC_210=udp:" not in body
    assert "JASPER_AEC_CHIP_AEC_ENABLED=0" in body
    assert "JASPER_AEC_REF_SOURCE=outputd_udp" in body
    assert "JASPER_OUTPUTD_REFERENCE_UDP_TARGET=127.0.0.1:9891" in body
    assert "JASPER_OUTPUTD_CHIP_REF_PCM=''" in body
    assert "JASPER_MIC_DEVICE_RAW=udp:9877" in body
    assert "JASPER_MIC_DEVICE_DTLN=udp:9878" in body
    assert "JASPER_AEC_DTLN_ENABLED=1" in body
    assert "restart jasper-outputd.service" in _systemctl_log(tmp_path)


def test_chip_aec_not_armed_without_6ch_firmware(tmp_path: Path) -> None:
    """CHIP_AEC=1 but the mic isn't 6-channel → the bridge doesn't run, so
    the chip vars stay cleared. The chip leg is structurally gated on the
    6-ch firmware (the bridge-running branch is the only one that arms it)."""
    _write_env(tmp_path, "udp:9876", extra="JASPER_AUDIO_DAC_ID=apple_usb_c_dongle\n")
    _write_mode_with_legs(
        tmp_path, mode="auto", raw="0", dtln="0", chip_aec="1",
    )
    _write_card(tmp_path, channels=2)
    _run_reconcile(tmp_path, "--reason", "test")
    body = (tmp_path / "jasper.env").read_text()
    assert "JASPER_MIC_DEVICE_CHIP_AEC_150=udp:" not in body
    assert "JASPER_AEC_CHIP_AEC_ENABLED=1" not in body
    assert "JASPER_AEC_REF_SOURCE=outputd_udp" in body


# ---------- Chip-ref observe mode (chip-AEC Layer 0 bootstrap) ------------
# JASPER_AEC_CHIP_REF_OBSERVE (opt-in, default off) arms outputd's chip-ref
# writer FOR DRIFT MEASUREMENT ONLY on the software-AEC3 leg path — the mic
# path stays software AEC3 (chip-AEC NOT armed). It breaks the bootstrap
# deadlock on unapproved independent-clock DACs: the reconciler won't arm
# chip-AEC until drift is measured, but drift can only be measured while the
# writer runs. The estimator then reads real DAC-vs-XVF counters that become
# the calibration. CRITICAL safety property: observe NEVER touches the mic path
# — only adds the chip-ref producer.


@pytest.mark.parametrize(
    ("observe", "channels", "chip_ref_pcm", "observe_flag", "announced"),
    [
        ("1", 6, "hw:CARD=Array,DEV=0", "1", True),
        ("0", 6, "''", "0", False),
        # 2-channel firmware → not aec_ready → bridge down, so there is no
        # chip-capable mic to source the reference from.
        ("1", 2, "''", "0", False),
    ],
    ids=("armed", "off", "no-chip-capable-mic"),
)
def test_chip_ref_observe_arms_only_the_writer_never_the_mic_path(
    tmp_path: Path,
    observe: str,
    channels: int,
    chip_ref_pcm: str,
    observe_flag: str,
    announced: bool,
) -> None:
    """The bootstrap path that feeds the Layer-0 SRO estimator for a DAC that
    is not yet approved, and the no-op arms either side of it.

    The mic path never moves: chip-AEC stays disabled on every row, so observe
    can only ever ADD the chip-ref producer. Arming a producer on the
    direct-mic fallback shape is the case the third row refuses.
    """
    _write_env(tmp_path, "udp:9876", extra="JASPER_AUDIO_DAC_ID=mystery_usb_audio\n")
    _write_mode_with_legs(
        tmp_path,
        mode="auto",
        raw="1",
        dtln="0",
        chip_aec="0",
        chip_ref_observe=observe,
    )
    _write_card(tmp_path, channels=channels)

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    values = _env_assignments(tmp_path / "jasper.env")
    assert values["JASPER_OUTPUTD_CHIP_REF_PCM"] == chip_ref_pcm
    assert values["JASPER_OUTPUTD_CHIP_REF_OBSERVE"] == observe_flag
    assert values["JASPER_AEC_CHIP_AEC_ENABLED"] == "0"
    assert ("chip-ref observe mode" in result.stderr) is announced
    if channels == 6:
        assert values["JASPER_MIC_DEVICE_RAW"] == "udp:9877"
        assert values["JASPER_AEC_REF_SOURCE"] == "outputd_udp"
        assert "JASPER_MIC_DEVICE_CHIP_AEC_150=udp:" not in (
            tmp_path / "jasper.env"
        ).read_text()
    if announced:
        # outputd restarts to pick up the newly-armed writer.
        assert "restart jasper-outputd.service" in _systemctl_log(tmp_path)


def test_reconcile_parks_voice_and_aec_for_bonded_follower(tmp_path: Path) -> None:
    """The dumb-follower profile: the Python-validated park flag in
    grouping-voice.env parks voice (disable --now, never a boot-window
    start) AND the AEC stack, before any mic/profile logic — a fully
    healthy Array + valid provider must not override role state."""
    _stage(tmp_path, "Array", mode="auto", channels=6)
    (tmp_path / "grouping-voice.env").write_text(
        f"{VOICE_TTS_SOCKET_ENV}={OUTPUTD_TTS_SOCKET}\n"
        f"{VOICE_PARK_ENV}=1\n"
    )

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    commands = _systemctl_log(tmp_path)
    assert "disable --now jasper-voice.service" in commands
    assert "stop jasper-aec-bridge.service jasper-aec-init.service" in commands
    assert VOICE_RESTART_CMD not in commands
    assert "restart jasper-aec-bridge.service" not in commands


def test_reconcile_unparks_voice_when_flag_absent(tmp_path: Path) -> None:
    """Unbond (or promotion to leader): the flag disappears from
    grouping-voice.env and the very next reconcile resumes the normal
    restart path — recovery needs no operator step."""
    env_file = _stage(tmp_path, "Array", mode="auto", channels=6)
    (tmp_path / "grouping-voice.env").write_text(
        f"{VOICE_TTS_SOCKET_ENV}={OUTPUTD_TTS_SOCKET}\n"
    )

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    assert "JASPER_MIC_DEVICE=udp:9876" in env_file.read_text()
    commands = _systemctl_log(tmp_path)
    assert VOICE_RESTART_CMD in commands
    assert "enable jasper-voice.service" in commands


# --- microphone-presence marker -------------------------------------------
# Both convergence directions: the marker is CREATED whenever voice is parked
# for no mic, and REMOVED whenever a mic is present — including the custom-mic
# path, which this script must never gate.


def test_reconcile_is_noop_while_foreground_commissioner_owns_lifecycle(
    tmp_path: Path,
) -> None:
    """Any pass under a LIVE marker that is not the commissioner's own
    reason-keyed arm call — hotplug from its volatile XVF reset, timers,
    deploys — mutates nothing but the bridge verdict, which it withdraws: the
    commissioner has stopped the bridge for its audible measurement and nothing
    may restart it until the cleanup pass republishes."""
    env_file = _write_env(tmp_path, "Array")
    before = env_file.read_bytes()
    (tmp_path / "chip-aec-commission-active").write_text("pid=123\n")
    (tmp_path / "proc" / "123").mkdir(parents=True)
    _prepublish_ready_marker(tmp_path)

    result = _run_reconcile(tmp_path, "--reason", "hotplug")

    assert result.returncode == 0, result.stderr
    assert env_file.read_bytes() == before
    assert not (tmp_path / "aec_mode.env").exists()
    assert not (tmp_path / "xvf3800.json").exists()
    assert _systemctl_log(tmp_path) == ""
    assert not _ready_marker(tmp_path).exists()


def test_live_commission_marker_arm_reason_pass_arms_reference_vector_only(
    tmp_path: Path,
) -> None:
    """The commissioner's own reason-keyed call under its live marker is the
    one arm dispatch: it publishes the final chip-reference vector (so the
    preflight can find outputd's native chip-ref writer) and hands outputd a
    start — nothing else. Voice, the bridge, aec-init, the wizard mode file,
    and the mic-profile state cache all stay owned by the commissioner, and
    the bridge verdict stays withdrawn."""
    from jasper.cli.aec_commission import ARM_RECONCILE_REASON

    env_file = _write_env(
        tmp_path,
        "Array",
        extra=(
            "JASPER_MIC_DEVICE_RAW=udp:9877\n"
            "JASPER_OUTPUTD_REFERENCE_UDP_TARGET=127.0.0.1:9891\n"
            "JASPER_AEC_CHIP_AEC_ENABLED=0\n"
        ),
    )
    (tmp_path / "chip-aec-commission-active").write_text("pid=123\n")
    (tmp_path / "proc" / "123").mkdir(parents=True)

    result = _run_reconcile(tmp_path, "--reason", ARM_RECONCILE_REASON)

    assert result.returncode == 0, result.stderr
    values = _env_assignments(env_file)
    # The reference vector: chip-ref writer armed, live UDP reference target
    # cleared, and the software legs it replaces cleared with it.
    assert values["JASPER_OUTPUTD_CHIP_REF_PCM"] == "hw:CARD=Array,DEV=0"
    assert values["JASPER_OUTPUTD_REFERENCE_UDP_TARGET"] == "''"
    assert values["JASPER_AEC_CHIP_AEC_ENABLED"] == "1"
    assert values["JASPER_OUTPUTD_CHIP_REF_OBSERVE"] == "0"
    assert values["JASPER_OUTPUTD_CHIP_REF_SAMPLE_RATE"] == "16000"
    assert values["JASPER_OUTPUTD_CHIP_REF_PERIOD_FRAMES"] == "128"
    assert values["JASPER_OUTPUTD_CHIP_REF_BUFFER_FRAMES"] == "256"
    assert values["JASPER_MIC_DEVICE_RAW"] == "''"
    # Mutual exclusion holds for everything but the vector: the voice mic
    # selection, the wizard mode file, the state cache, and every unit except
    # outputd are untouched.
    assert values["JASPER_MIC_DEVICE"] == "Array"
    assert not (tmp_path / "aec_mode.env").exists()
    assert not (tmp_path / "xvf3800.json").exists()
    assert _systemctl_log(tmp_path).splitlines() == [
        "reset-failed jasper-outputd.service",
        "restart jasper-outputd.service",
    ]
    assert not _ready_marker(tmp_path).exists()
    # A repeated arm call converges: an unchanged vector hands outputd
    # nothing to restart.
    rerun = _run_reconcile(tmp_path, "--reason", ARM_RECONCILE_REASON)
    assert rerun.returncode == 0, rerun.stderr
    assert _systemctl_log(tmp_path).splitlines() == [
        "reset-failed jasper-outputd.service",
        "restart jasper-outputd.service",
    ]


# Every route that can take jasper-aec-bridge down, with the pass-start state
# that reaches it and whether it ends with voice gated off.
_BRIDGE_STOP_ROUTES: dict[str, tuple[dict[str, object], dict[str, str], bool]] = {
    "no_local_mic": ({"mic": "udp:9876", "mode": "auto"}, {}, True),
    "aec_disabled_no_mic": ({"mic": "udp:9876", "mode": "disabled"}, {}, True),
    # hw:9,0 is not an owned value.
    "custom_mic": ({"mic": "hw:9,0", "mode": "auto"}, {}, False),
    "bonded_follower": (
        {"mic": "udp:9876", "mode": "auto", "channels": 6, "bonded": True},
        {},
        False,
    ),
    # No interpreter for the mic-profile resolver, so no eligible capture
    # device can be named: managed_xvf_policy_applies with a park reason.
    "unresolvable_managed_xvf": (
        {"mic": "udp:9876", "profile": "xvf_chip_aec"},
        {"JASPER_MIC_PROFILE_PYTHON": "/nonexistent/python3"},
        True,
    ),
    "two_channel_xvf": ({"mic": "udp:9876", "mode": "auto", "channels": 2}, {}, False),
    "six_channel_xvf": ({"mic": "Array", "mode": "auto", "channels": 6}, {}, False),
}


@pytest.mark.parametrize("route", list(_BRIDGE_STOP_ROUTES), ids=str)
def test_no_route_stops_the_bridge_before_it_gates_voice(
    tmp_path: Path, route: str
) -> None:
    """One pin for every route that can take jasper-aec-bridge down.

    Stopping the bridge kills the udp: carrier JASPER_MIC_DEVICE may still
    name, and an unfed udp: socket opens fine — so the daemon's exit-66 park
    never fires and only the ConditionPathExists marker keeps voice from
    starting into a watchdog restart loop. Any pass that writes the marker at
    all must therefore have written it by the time the first bridge stop goes
    out. Routes that never write it (a usable mic, or a role park that is not
    a mic decision) are exempt by construction, not by ordering.
    """
    staging, extra_env, parks = _BRIDGE_STOP_ROUTES[route]
    _stage(tmp_path, **staging)  # type: ignore[arg-type]
    witness, log = _fake_systemctl(
        tmp_path, name="witness", witness="JASPER_VOICE_INPUT_ABSENT_MARKER"
    )

    result = _run_reconcile(
        tmp_path,
        "--reason",
        "test",
        extra_env={
            "JASPER_SYSTEMCTL": str(witness),
            "JASPER_SYSTEMCTL_LOG": str(log),
            **extra_env,
        },
    )

    assert result.returncode == 0, result.stderr
    assert _marker(tmp_path).exists() is parks, result.stderr
    lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    gated = [i for i, line in enumerate(lines) if line.startswith("present=1 ")]
    bridge_stops = [
        i
        for i, line in enumerate(lines)
        if line.split()[1:2] == ["stop"]
        and "jasper-aec-bridge.service" in line.split()[2:]
    ]
    # Every route in the table reaches a bridge stop; a staging change that
    # stopped reaching one would otherwise pass this test vacuously.
    assert bridge_stops, lines
    if parks:
        assert gated and gated[0] <= bridge_stops[0], lines
    elif gated:
        # A route that marks only for the length of its own rebuild (and
        # clears again before it ends) owes the same ordering.
        assert gated[0] <= bridge_stops[0], lines


def test_a_failed_marker_write_still_parks_voice(tmp_path: Path) -> None:
    """The gate's own failure mode may not fall back to a guard that cannot
    fire: with the marker unwritable, the unit is disabled instead."""
    _stage(tmp_path, "udp:9876", mode="auto")

    result = _run_reconcile(
        tmp_path,
        "--reason",
        "test",
        extra_env={
            "JASPER_VOICE_INPUT_ABSENT_MARKER": str(
                tmp_path / "absent-dir" / "voice-input-absent"
            )
        },
    )

    assert result.returncode == 0, result.stderr
    commands = _systemctl_log(tmp_path).splitlines()
    assert "disable jasper-voice.service" in commands


@pytest.mark.parametrize(
    ("mic", "channels", "expected_command"),
    [
        ("Array", 6, VOICE_RESTART_CMD),
        # A custom device is the operator's; we must not gate it, and the
        # route never restarts voice — so it is where the `enable` half of the
        # gate has to be, lifting a `disable` an earlier failed mark left.
        ("hw:9,0", None, "enable jasper-voice.service"),
    ],
    ids=("six-channel-array", "custom-device"),
)
def test_a_usable_mic_clears_a_stale_marker(
    tmp_path: Path, mic: str, channels: int | None, expected_command: str
) -> None:
    """The ConditionPathExists gate must open the moment a mic is available
    again; on the custom route the daemon's own exit-66 park is the net."""
    _stage(tmp_path, mic, mode="auto", channels=channels)
    _marker(tmp_path).write_text("reason=stale\n")

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    assert not _marker(tmp_path).exists(), result.stderr
    assert expected_command in _systemctl_log(tmp_path).splitlines()


# --- measurement-class mic identity + hotplug change-gating ----------------
#
# Two coupled behaviours, both driven by the same udev rule
# (deploy/udev/99-jasper-aec-reconcile.rules fires on EVERY sound-card
# add|remove, id-agnostic on purpose — the policy lives in the reconciler):
#
#   1. a calibrated measurement microphone is never selected as the voice
#      input, whatever the candidate list says; and
#   2. a reconcile pass that resolves to no voice-relevant change does not
#      restart jasper-voice or bounce the AEC stack.
#
# (2) is a GUARD, so most of what follows is its positive-control half: the
# cases that must still restart. A gate nobody can trip guards nothing, and a
# gate that suppresses a needed restart leaves a deaf speaker until the next
# hardware event.

UMIK2_USB_ID = "2752:002b"
XVF_USB_ID = "2886:001a"


def _write_manifest(tmp_path: Path, sha: str = "abc1234") -> Path:
    """Write the install manifest the change gate reads as code identity."""
    manifest = tmp_path / "build.txt"
    manifest.write_text(
        f"JASPER_GIT_SHA={sha}\n"
        "JASPER_GIT_BRANCH=main\n"
        "JASPER_INSTALL_AT=2026-08-18T00:00:00+00:00\n"
        "JASPER_INSTALL_STATUS=ok\n",
        encoding="utf-8",
    )
    return manifest


@pytest.fixture(autouse=True)
def _gate_armed_by_default(tmp_path: Path) -> None:
    """Every test in this file runs with an install manifest present.

    The voice-restart change gate is OFF without one — record_voice_restart_stamp
    refuses to write a stamp it cannot tie to a build, so no pass can ever
    skip — and a gate that is off across the behavioral suite cannot turn a
    suppressed-but-needed restart into a red test. Single-pass tests are
    unaffected (their first pass has no stamp and restarts regardless);
    multi-pass transition tests run gate-armed by construction. The
    missing-manifest positive control deletes the file explicitly.
    """
    _write_manifest(tmp_path)


def _write_usb_card(
    tmp_path: Path, card: str, usb_id: str, *, channels: int = 2
) -> None:
    """A capture card that also exposes /proc/asound/<card>/usbid."""
    card_dir = tmp_path / "asound" / card
    card_dir.mkdir(parents=True, exist_ok=True)
    (card_dir / "stream0").write_text(
        f"Playback:\n  Status: Stop\nCapture:\n  Channels: {channels}\n"
    )
    (card_dir / "usbid").write_text(f"{usb_id}\n")


def _clear_systemctl_log(tmp_path: Path) -> None:
    log = tmp_path / "systemctl.log"
    if log.exists():
        log.unlink()


def _armed_chip_aec_box(tmp_path: Path, alignment: str = "ready") -> None:
    """A commissioned chip-AEC speaker: 6-channel XVF, provider set, a
    verified install, and one reconcile pass already run so the env file, the
    alignment status, and the /run stamp all describe the running state.

    This is the state a measurement-mic hotplug arrives in, and the state in
    which a second identical pass must change nothing. `alignment` picks which
    armed state the baseline pass lands in — `disclosed_stale` is the same
    stack with jasper-aec-init's disclosure standing.
    """
    _stage(tmp_path, "Array", mode="auto", channels=6)
    extra_env = {}
    if alignment == "disclosed_stale":
        extra_env["JASPER_SYSTEMCTL"] = str(
            _publishing_init_systemctl(
                tmp_path,
                alignment_health(
                    chip_aec_health.APPLIED,
                    selection="auto",
                    identity_diff=("xvf_serial",),
                ),
            )
        )
    first = _run_reconcile(tmp_path, "--reason", "install", extra_env=extra_env)
    assert first.returncode == 0, first.stderr
    # Sanity: the baseline pass really did arm the chip-AEC path and restart
    # voice. Without this the "skipped" assertions below could pass against a
    # box that never armed anything.
    assert VOICE_RESTART_CMD in _systemctl_log(tmp_path), first.stderr
    body = (tmp_path / "jasper.env").read_text()
    assert f"JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS={alignment}" in body
    assert "JASPER_AEC_CHIP_AEC_ENABLED=1" in body
    _clear_systemctl_log(tmp_path)


# --- the guard: an unchanged pass leaves the voice path alone --------------


@pytest.mark.parametrize("alignment", ["ready", "disclosed_stale"])
def test_unchanged_pass_skips_the_voice_restart_and_the_chip_aec_bounce(
    tmp_path: Path, alignment: str
) -> None:
    # `disclosed_stale` with the chip armed is as settled as `ready`, and it is
    # long-lived by design — re-bouncing it on every udev sound-card event,
    # deploy and wizard save for as long as the disclosure stands would spend
    # ~8 s of deafness each time on a box where nothing changed.
    _armed_chip_aec_box(tmp_path, alignment)

    result = _run_reconcile(tmp_path, "--reason", "systemd")

    assert result.returncode == 0, result.stderr
    commands = _systemctl_log(tmp_path)
    assert VOICE_RESTART_CMD not in commands
    assert "stop jasper-voice.service" not in commands
    assert "restart jasper-aec-init.service" not in commands
    assert "restart jasper-aec-bridge.service" not in commands
    assert "event=aec_reconcile.chip_aec_bounce_skipped" in result.stderr
    assert "reason=no_voice_relevant_change" in result.stderr
    assert f"alignment={alignment}" in result.stderr
    # Skipping the bounce is not skipping the verdict: every pass withdraws it
    # before re-deriving, so a settled box has to be re-admitted or the next
    # `systemctl restart jasper-aec-bridge` from any other owner is skipped.
    assert _ready_marker(tmp_path).exists()


def test_a_software_fallback_disclosure_keeps_bouncing_so_the_race_can_heal(
    tmp_path: Path,
) -> None:
    # The other disclosed sub-state: chip NOT armed because aec-init could not
    # apply the alignment. That one must keep re-running the sequence, or the
    # outputd ordering race (exit 3) it came from could never resolve.
    _stage(
        tmp_path,
        "Array",
        extra=(
            "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS=disclosed_stale\n"
            "JASPER_AEC_CHIP_AEC_ENABLED=0\n"
        ),
        mode="auto",
        channels=6,
    )

    result = _run_reconcile(tmp_path, "--reason", "systemd")

    assert result.returncode == 0, result.stderr
    assert "event=aec_reconcile.chip_aec_bounce_skipped" not in result.stderr
    assert "restart jasper-aec-init.service" in _systemctl_log(tmp_path)


def test_a_settled_disclosed_aec3_box_re_arms_nothing_on_an_unchanged_pass(
    tmp_path: Path,
) -> None:
    """The disclosed AEC3 fallback is a STEADY state, not a retry.

    An uncodified DAC (or a mic with no beam plan) never becomes codified by
    re-running the sequence, so re-arming on every udev event, deploy and
    wizard save would re-program the chip and drop the AEC3 carrier forever.
    """
    _stage(
        tmp_path,
        "Array",
        extra="JASPER_AUDIO_DAC_ID=mystery_usb_audio\n",
        mode="auto",
        channels=6,
    )

    first = _run_reconcile(tmp_path, "--reason", "install")
    assert first.returncode == 0, first.stderr
    body = (tmp_path / "jasper.env").read_text()
    assert "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS=disclosed_stale" in body
    assert "JASPER_MIC_DEVICE=udp:9876" in body
    assert VOICE_RESTART_CMD in _systemctl_log(tmp_path), first.stderr
    _clear_systemctl_log(tmp_path)

    second = _run_reconcile(tmp_path, "--reason", "systemd")

    assert second.returncode == 0, second.stderr
    commands = _systemctl_log(tmp_path)
    assert "restart jasper-aec-init.service" not in commands
    assert "restart jasper-aec-bridge.service" not in commands
    assert VOICE_RESTART_CMD not in commands
    assert "event=aec_reconcile.disclosed_bounce_skipped" in second.stderr
    # And the box is still the hearing one the first pass produced.
    assert (tmp_path / "jasper.env").read_text() == body


def test_a_revoked_dac_verdict_flips_a_settled_chip_aec_box_onto_aec3(
    tmp_path: Path,
) -> None:
    """The TRANSITION pass the settled-pass skip must never swallow.

    Every key this pass writes before the bounce gate — the DAC gate record and
    the alignment status — is voice-irrelevant by design, so the gate sees no
    voice-relevant change yet. What makes the pass a change is the leg vector it
    is ABOUT to publish, which is why that publication stays ahead of the gate:
    a box left running chip legs on a revoked DAC while /state reports software
    AEC3 would persist that contradiction on every following pass.
    """
    _armed_chip_aec_box(tmp_path)
    env_file = tmp_path / "jasper.env"
    env_file.write_text(
        env_file.read_text().replace(
            "JASPER_AUDIO_DAC_ID=apple_usb_c_dongle",
            "JASPER_AUDIO_DAC_ID=mystery_usb_audio",
        )
    )

    result = _run_reconcile(tmp_path, "--reason", "udev")

    assert result.returncode == 0, result.stderr
    body = env_file.read_text()
    assert "JASPER_AEC_CHIP_AEC_ENABLED=0" in body
    assert "JASPER_MIC_DEVICE_RAW=udp:9877" in body
    assert "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS=disclosed_stale" in body
    commands = _systemctl_log(tmp_path)
    assert "restart jasper-aec-init.service" in commands, result.stderr
    assert VOICE_RESTART_CMD in commands
    assert "event=aec_reconcile.disclosed_bounce_skipped" not in result.stderr


def test_measurement_mic_hotplug_does_not_bounce_the_voice_assistant(
    tmp_path: Path,
) -> None:
    """Plugging a UMIK-2 in to take a room measurement fires the id-agnostic
    sound-card udev rule. An unconditional restart on every mic-bearing branch
    costs ~8 s of deafness measured on jts3, up to ~55 s worst case."""
    _armed_chip_aec_box(tmp_path)
    _write_usb_card(tmp_path, "UMIK2", UMIK2_USB_ID, channels=1)

    result = _run_reconcile(tmp_path, "--reason", "systemd")

    assert result.returncode == 0, result.stderr
    commands = _systemctl_log(tmp_path)
    assert VOICE_RESTART_CMD not in commands
    assert "stop jasper-voice.service" not in commands
    assert "restart jasper-aec-init.service" not in commands
    # The armed configuration is untouched: the speaker keeps hearing.
    body = (tmp_path / "jasper.env").read_text()
    assert "JASPER_MIC_DEVICE=udp:9876" in body
    assert "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS=ready" in body


def test_unchanged_software_aec3_pass_skips_the_stack_bounce_too(
    tmp_path: Path,
) -> None:
    """The software-AEC3 path bounces init+bridge through enable_start_aec,
    the same class of outage. The two are gated together: bouncing the bridge
    while leaving voice up would strand the daemon on a dead UDP carrier.

    Uses the `custom` profile because every managed-XVF profile resolves
    through managed chip policy instead (apply_audio_input_profile), so a
    selectable product profile could never reach enable_start_aec here.
    """
    _stage(tmp_path, "Array", profile="custom", channels=6)
    first = _run_reconcile(tmp_path, "--reason", "install")
    assert first.returncode == 0, first.stderr
    assert VOICE_RESTART_CMD in _systemctl_log(tmp_path), first.stderr
    _clear_systemctl_log(tmp_path)

    result = _run_reconcile(tmp_path, "--reason", "systemd")

    assert result.returncode == 0, result.stderr
    commands = _systemctl_log(tmp_path)
    assert VOICE_RESTART_CMD not in commands
    assert "restart jasper-aec-bridge.service" not in commands
    assert "event=aec_reconcile.aec_stack_bounce_skipped" in result.stderr
    # The voice-side gate logs its own skip on this path (restart_voice runs
    # after the stack skip) — the third stable skip event, pinned here.
    assert "event=aec_reconcile.voice_restart_skipped" in result.stderr


# --- positive controls: the cases that MUST still restart ------------------


def test_a_real_mic_change_still_restarts_voice(tmp_path: Path) -> None:
    """The control that makes the guard meaningful. Same box, same build, but
    JASPER_MIC_DEVICE no longer matches what the pass resolves — the gate must
    not swallow that."""
    _armed_chip_aec_box(tmp_path)
    env_file = tmp_path / "jasper.env"
    env_file.write_text(
        env_file.read_text().replace(
            "JASPER_MIC_DEVICE=udp:9876", "JASPER_MIC_DEVICE=Array"
        )
    )

    result = _run_reconcile(tmp_path, "--reason", "systemd")

    assert result.returncode == 0, result.stderr
    commands = _systemctl_log(tmp_path)
    assert VOICE_RESTART_CMD in commands
    assert "restart jasper-aec-init.service" in commands
    assert "event=aec_reconcile.chip_aec_bounce_skipped" not in result.stderr


def _new_build(tmp_path: Path) -> dict[str, str]:
    # A deploy changes no env value on a settled box, and
    # scripts/deploy-to-pi.sh deliberately does not restart jasper-voice
    # itself — this reconciler is what rolls new Python into the daemon.
    _write_manifest(tmp_path, sha="def5678")
    return {}


def _unprovable_build(tmp_path: Path) -> dict[str, str]:
    (tmp_path / "build.txt").unlink()
    return {}


def _newly_paired_accessory(tmp_path: Path) -> dict[str, str]:
    # jasper.accessories.reconcile.refresh_voice_input starts this unit
    # WITHOUT stopping voice, so "a live push-to-talk session picks up the new
    # source". The published sources live outside jasper.env, so the env change
    # test cannot see them — they are part of the stamp instead.
    _pair_remote(tmp_path)
    return {}


@pytest.mark.parametrize(
    "apply_change",
    [_new_build, _unprovable_build, _newly_paired_accessory],
    ids=("new-build", "no-manifest", "paired-accessory"),
)
def test_a_change_the_env_file_cannot_see_still_restarts_voice(
    tmp_path: Path, apply_change: Callable[[Path], dict[str, str]]
) -> None:
    """Every fact the daemon starts from that jasper.env does not carry. A
    gate blind to one of these leaves voice on the previous build, or a
    freshly-paired remote dead, until the next unrelated hardware event."""
    _armed_chip_aec_box(tmp_path)
    extra_env = apply_change(tmp_path)

    result = _run_reconcile(tmp_path, "--reason", "systemd", extra_env=extra_env)

    assert result.returncode == 0, result.stderr
    # The accessory probe must RESOLVE: a parse failure restarts through the
    # fail-open branch instead and would prove nothing about the stamp.
    assert "accessory mic probe failed" not in result.stderr
    assert VOICE_RESTART_CMD in _systemctl_log(tmp_path)


@pytest.mark.parametrize(
    ("verb", "status", "also_expect"),
    [
        ("is-active", 3, ()),
        # Enabled-ness is part of "already running as configured": a
        # disabled-but-active voice evaporates on the next boot.
        ("is-enabled", 1, ("enable jasper-voice.service",)),
    ],
    ids=("stopped", "disabled"),
)
def test_a_voice_unit_not_running_as_configured_still_restarts(
    tmp_path: Path, verb: str, status: int, also_expect: tuple[str, ...]
) -> None:
    """Nothing changed, but the daemon is not up the way the pass expects.
    Skipping here would leave the speaker deaf until the next hardware event
    — the exact failure the gate must never produce."""
    _armed_chip_aec_box(tmp_path)
    double = _systemctl_reporting(tmp_path, verb, "jasper-voice.service", status)

    result = _run_reconcile(
        tmp_path,
        "--reason",
        "systemd",
        extra_env={"JASPER_SYSTEMCTL": str(double)},
    )

    assert result.returncode == 0, result.stderr
    commands = _systemctl_log(tmp_path)
    assert VOICE_RESTART_CMD in commands
    for command in also_expect:
        assert command in commands


def test_a_downed_aec_bridge_forces_the_voice_restart_with_it(
    tmp_path: Path,
) -> None:
    """The stack bounce and the voice restart are gated TOGETHER. When the
    bridge is down, enable_start_aec rebuilds it — and that pass must also
    restart voice even though no env value changed, so the daemon and its UDP
    carrier always come from the same pass (enable_start_aec's own
    VOICE_RESTART_NEEDED=1). Skipping voice while the bridge bounces is the
    split the gate must never produce."""
    _stage(tmp_path, "Array", profile="custom", channels=6)
    first = _run_reconcile(tmp_path, "--reason", "install")
    assert first.returncode == 0, first.stderr
    _clear_systemctl_log(tmp_path)
    # The bridge is down but voice is up: the stack skip cannot engage, so the
    # pass rebuilds the bridge.
    result = _run_reconcile(
        tmp_path,
        "--reason",
        "systemd",
        extra_env={
            "JASPER_SYSTEMCTL": str(
                _systemctl_reporting(
                    tmp_path, "is-active", "jasper-aec-bridge.service", 3
                )
            )
        },
    )

    assert result.returncode == 0, result.stderr
    commands = _systemctl_log(tmp_path)
    assert "restart jasper-aec-bridge.service" in commands
    assert VOICE_RESTART_CMD in commands


def test_a_not_ready_alignment_still_reverifies_the_chip_stack(
    tmp_path: Path,
) -> None:
    """The chip-AEC skip demands alignment "ready" DIRECTLY, on top of the env
    change test. The env layer alone cannot be trusted with this: the
    alignment STATUS keys are deliberately in VOICE_IRRELEVANT_ENV_KEYS, so a
    box whose stored status says anything but ready — with everything else
    unchanged — would sail through a gate that forgot this operand, and stay
    un-reverified forever."""
    _armed_chip_aec_box(tmp_path)
    env_file = tmp_path / "jasper.env"
    env_file.write_text(
        env_file.read_text().replace(
            "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS=ready",
            "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS=failed",
        )
    )

    result = _run_reconcile(tmp_path, "--reason", "systemd")

    assert result.returncode == 0, result.stderr
    commands = _systemctl_log(tmp_path)
    assert "restart jasper-aec-init.service" in commands
    assert VOICE_RESTART_CMD in commands
    assert "event=aec_reconcile.chip_aec_bounce_skipped" not in result.stderr
    # And the re-verification restored the truth.
    assert "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS=ready" in env_file.read_text()


def _present_park_marker(tmp_path: Path) -> dict[str, str]:
    # A pass that would clear the park marker is UN-parking voice; bringing it
    # back is the whole point.
    _marker(tmp_path).write_text("reason=stale\n")
    return {}


def _invalidated_voice_provider(tmp_path: Path) -> dict[str, str]:
    # voice_provider.env is not in jasper.env, so the env change test cannot
    # see a provider that went away. restart_voice's park branch has to run.
    (tmp_path / "voice_provider.env").write_text("JASPER_VOICE_PROVIDER=\n")
    return {}


def _unreadable_accessory_probe(tmp_path: Path) -> dict[str, str]:
    # The fail-open branch. "I could not tell" is not "nothing is paired": a
    # probe that cannot answer must not be allowed to look like an unchanged
    # input.
    broken = _python_double(
        tmp_path,
        "broken-accessory-python",
        failing_module="jasper.accessories.mic_env",
    )
    return {"JASPER_MIC_PROFILE_PYTHON": str(broken)}


@pytest.mark.parametrize(
    ("apply_change", "expect_command"),
    [
        (_present_park_marker, VOICE_RESTART_CMD),
        (_invalidated_voice_provider, "disable --now jasper-voice.service"),
        (_unreadable_accessory_probe, VOICE_RESTART_CMD),
    ],
    ids=("park-marker", "invalid-provider", "unreadable-probe"),
)
def test_a_park_branch_trigger_still_reaches_restart_voice(
    tmp_path: Path,
    apply_change: Callable[[Path], dict[str, str]],
    expect_command: str,
) -> None:
    """Three independent inputs to restart_voice's park branch that the env
    change test cannot see. Each must still reach it."""
    _armed_chip_aec_box(tmp_path)
    extra_env = apply_change(tmp_path)

    result = _run_reconcile(tmp_path, "--reason", "systemd", extra_env=extra_env)

    assert result.returncode == 0, result.stderr
    assert expect_command in _systemctl_log(tmp_path)
    assert not _marker(tmp_path).exists()


def test_a_bond_and_an_unbond_both_restart_the_leaders_voice(
    tmp_path: Path,
) -> None:
    """The other owner-published fact voice starts from: grouping-voice.env.

    jasper.multiroom.reconcile (step 3b) rewrites it on bond/unbond — the
    leader's TTS socket flip — and kicks this reconciler to do the restart,
    without stopping voice and without touching jasper.env. For a non-parked
    leader the file's CONTENT is the only visible change, so it is part of
    the stamp; a gate blind to it leaves the leader on the wrong TTS route
    until the next unrelated hardware event."""
    _armed_chip_aec_box(tmp_path)
    grouping = tmp_path / "grouping-voice.env"

    # Bond: the leader's grouping-derived voice env appears.
    grouping.write_text(f"{VOICE_TTS_SOCKET_ENV}={OUTPUTD_TTS_SOCKET}\n")
    result = _run_reconcile(tmp_path, "--reason", "systemd")
    assert result.returncode == 0, result.stderr
    assert VOICE_RESTART_CMD in _systemctl_log(tmp_path)

    # Settled: the stamp absorbed the bonded content, so the next unchanged
    # pass skips again — the restart above was the content change, not noise.
    _clear_systemctl_log(tmp_path)
    result = _run_reconcile(tmp_path, "--reason", "systemd")
    assert result.returncode == 0, result.stderr
    assert VOICE_RESTART_CMD not in _systemctl_log(tmp_path)

    # Unbond: back to solo. The leader must pick the solo TTS route back up.
    _clear_systemctl_log(tmp_path)
    grouping.unlink()
    result = _run_reconcile(tmp_path, "--reason", "systemd")
    assert result.returncode == 0, result.stderr
    assert VOICE_RESTART_CMD in _systemctl_log(tmp_path)


def test_a_repeat_install_pass_still_restarts_voice(tmp_path: Path) -> None:
    """install.sh's in-install kick runs BEFORE write_build_manifest seals
    the transaction, so that pass reads the PREVIOUS build's manifest — and
    on the Pi-local `sudo bash install.sh` path there is no later kick to
    roll the freshly copied /opt code into the daemon. An install pass is
    therefore declared intent, never change-gated."""
    _armed_chip_aec_box(tmp_path)

    result = _run_reconcile(tmp_path, "--reason", "install")

    assert result.returncode == 0, result.stderr
    assert (
        "event=aec_reconcile.voice_restart_intent reason=install"
        in result.stderr
    )
    assert VOICE_RESTART_CMD in _systemctl_log(tmp_path)


def test_a_declared_intent_marker_defeats_the_gate_once(tmp_path: Path) -> None:
    """The enhanced-AEC v2 activation changes nothing the gate inspects (the
    verified engine lives in a venv), and its systemctl kick can carry no
    arguments — so it declares intent through the one-shot marker. The pass
    that acts on it consumes it: the next unchanged pass skips again."""
    _armed_chip_aec_box(tmp_path)
    marker = tmp_path / "run" / "voice-restart-intent"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("enhanced_aec_v2_activation\n")

    result = _run_reconcile(tmp_path, "--reason", "systemd")

    assert result.returncode == 0, result.stderr
    assert (
        "event=aec_reconcile.voice_restart_intent reason=enhanced_aec_v2_activation"
        in result.stderr
    )
    assert VOICE_RESTART_CMD in _systemctl_log(tmp_path)
    assert not marker.exists()

    _clear_systemctl_log(tmp_path)
    result = _run_reconcile(tmp_path, "--reason", "systemd")
    assert result.returncode == 0, result.stderr
    assert VOICE_RESTART_CMD not in _systemctl_log(tmp_path)


def test_intent_marker_path_literal_agrees_across_writer_and_consumer() -> None:
    """The marker path is duplicated in the Python writer
    (jasper/cli/enhanced_aec_install.py) and the bash consumer, because a
    systemctl kick can carry no arguments. This is the drift pin — same
    pattern as the voice-input-absent marker's path test."""
    literal = "/run/jasper-aec-reconcile/voice-restart-intent"
    assert literal in SCRIPT.read_text(encoding="utf-8")
    assert literal in (
        ROOT / "jasper" / "cli" / "enhanced_aec_install.py"
    ).read_text(encoding="utf-8")


def test_resolver_detected_hardware_drift_on_disk_still_restarts_voice(
    tmp_path: Path,
) -> None:
    """The change test compares against the PASS-START env file, never against
    shell variables the profile resolver's eval already overwrote — a resolved
    value compared with itself can never trip. Model: the stored XVF facts went
    stale relative to the hardware; the resolver re-derives the truth, and that
    write must count as a voice-relevant change."""
    _armed_chip_aec_box(tmp_path)
    env_file = tmp_path / "jasper.env"
    body = env_file.read_text()
    assert "JASPER_XVF_CAPTURE_CHANNELS=6" in body
    env_file.write_text(
        body.replace(
            "JASPER_XVF_CAPTURE_CHANNELS=6", "JASPER_XVF_CAPTURE_CHANNELS=2"
        )
    )

    result = _run_reconcile(tmp_path, "--reason", "systemd")

    assert result.returncode == 0, result.stderr
    assert "JASPER_XVF_CAPTURE_CHANNELS=6" in env_file.read_text()
    assert VOICE_RESTART_CMD in _systemctl_log(tmp_path)


def _armed_direct_mic_box(tmp_path: Path) -> None:
    """A settled non-XVF direct-mic speaker (AEC disabled), one pass run so
    the env file and the /run stamp describe the running state."""
    _stage(
        tmp_path,
        "UsbMic",
        extra="JASPER_MIC_DEVICE_CANDIDATES=UsbMic\n",
        mode="disabled",
        card="UsbMic",
        channels=2,
    )
    first = _run_reconcile(tmp_path, "--reason", "install")
    assert first.returncode == 0, first.stderr
    assert VOICE_RESTART_CMD in _systemctl_log(tmp_path), first.stderr
    _clear_systemctl_log(tmp_path)


def test_direct_mic_pass_with_a_live_bridge_still_restarts_voice(
    tmp_path: Path,
) -> None:
    """stop_disable_aec's is-active coupling, positive arm: the RUNNING
    daemon's environment is unobservable, and a live bridge is the one
    observable hint that a UDP topology may still be in use — tearing it
    down must carry a voice restart with it. The default systemctl double
    reports every unit active, so this pass stops a "live" stale bridge."""
    _armed_direct_mic_box(tmp_path)

    result = _run_reconcile(tmp_path, "--reason", "systemd")

    assert result.returncode == 0, result.stderr
    assert VOICE_RESTART_CMD in _systemctl_log(tmp_path)


def test_unchanged_direct_mic_pass_with_bridge_down_skips_the_restart(
    tmp_path: Path,
) -> None:
    """The same coupling's no-op arm: with the bridge already down, stopping
    it changes nothing, and an unchanged direct-mic pass leaves the daemon
    alone — otherwise every AEC-disabled box would bounce voice on every
    hardware event."""
    _armed_direct_mic_box(tmp_path)

    result = _run_reconcile(
        tmp_path,
        "--reason",
        "systemd",
        extra_env={
            "JASPER_SYSTEMCTL": str(
                _systemctl_reporting(
                    tmp_path, "is-active", "jasper-aec-bridge.service", 3
                )
            )
        },
    )

    assert result.returncode == 0, result.stderr
    assert VOICE_RESTART_CMD not in _systemctl_log(tmp_path)
    assert "event=aec_reconcile.voice_restart_skipped" in result.stderr


@pytest.mark.parametrize(
    "stale_seed", ["", "JASPER_AEC_MIC_DEVICE=UMIK2\n"], ids=("fallback", "stale-seed")
)
def test_a_six_channel_measurement_card_never_arms_the_aec_stack(
    tmp_path: Path, stale_seed: str
) -> None:
    """aec_ready gates on channel count; a hypothetical 6-channel measurement
    card must not pass it into the software-AEC stack, and the
    all-measurement fallback name must not hand the instrument to any later
    consumer either (it seeds JASPER_MIC_DEVICE, which an accessory-cleared
    park gate would let jasper-voice open).

    The second row reaches aec_ready's own measurement-class refusal, where
    JASPER_AEC_MIC_DEVICE already NAMES the instrument — a stale seed from a
    build predating the fallback fix, or a hand edit. The fixed fallback never
    runs when the seed is set, so that refusal is the last line before the
    software-AEC stack opens a measurement mic.
    """
    _stage(
        tmp_path,
        "udp:9876",
        extra=f"JASPER_MIC_DEVICE_CANDIDATES=UMIK2\n{stale_seed}",
        mode="auto",
    )
    _write_usb_card(tmp_path, "UMIK2", UMIK2_USB_ID, channels=6)

    result = _run_reconcile(tmp_path, "--reason", "systemd")

    assert result.returncode == 0, result.stderr
    commands = _systemctl_log(tmp_path)
    assert "enable jasper-aec-init.service jasper-aec-bridge.service" not in commands
    body = (tmp_path / "jasper.env").read_text()
    assert "JASPER_MIC_DEVICE=udp:9876" not in body
    assert _marker(tmp_path).exists()
    if not stale_seed:
        # The fallback is the stock first candidate — a real card name that
        # simply parks while absent — never the instrument, never stale UDP.
        assert "JASPER_MIC_DEVICE=Array" in body
        assert "JASPER_MIC_DEVICE=UMIK2" not in body


def test_descriptive_only_churn_does_not_trip_the_gate(tmp_path: Path) -> None:
    """The reason VOICE_IRRELEVANT_ENV_KEYS exists.

    JASPER_AEC_CHIP_AEC_DAC_DETAIL carries outputd's live `chip_ref_sro_ppm=`
    clock estimate, which moves on essentially every pass on a chip-AEC box.
    If a descriptive key counted as a change, the gate would be permanently
    tripped on exactly the hardware whose bounce it exists to stop — a guard
    nobody can trip. Simulated by storing a different detail so the next pass
    resolves one that differs.
    """
    _armed_chip_aec_box(tmp_path)
    env_file = tmp_path / "jasper.env"
    body = env_file.read_text()
    assert "JASPER_AEC_CHIP_AEC_DAC_DETAIL=" in body
    env_file.write_text(
        re.sub(
            r"^JASPER_AEC_CHIP_AEC_DAC_DETAIL=.*$",
            "JASPER_AEC_CHIP_AEC_DAC_DETAIL='stale chip_ref_sro_ppm=1.7'",
            body,
            flags=re.MULTILINE,
        )
    )

    result = _run_reconcile(tmp_path, "--reason", "systemd")

    assert result.returncode == 0, result.stderr
    assert VOICE_RESTART_CMD not in _systemctl_log(tmp_path)
    # Still refreshed on disk — excluded from the CHANGE test, not the write.
    assert "stale chip_ref_sro_ppm" not in env_file.read_text()


def test_voice_irrelevant_keys_are_all_keys_the_script_writes() -> None:
    """Drift guard on the exclusion list: a typo, or a key that stops being
    written, silently promotes a descriptive key back to voice-relevant and
    re-arms the bounce the gate exists to stop. Invisible at runtime."""
    source = SCRIPT.read_text(encoding="utf-8")
    match = re.search(
        r'VOICE_IRRELEVANT_ENV_KEYS="(.*?)"\n', source, flags=re.DOTALL
    )
    assert match is not None, "could not locate VOICE_IRRELEVANT_ENV_KEYS"
    declared = set(match.group(1).replace("\\\n", " ").split())
    assert declared, "VOICE_IRRELEVANT_ENV_KEYS parsed empty"
    written = set(re.findall(r'set_env_var "\$ENV_FILE" (\w+)', source))
    assert declared <= written, sorted(declared - written)


# --- measurement-class exclusion from the candidate set --------------------


def test_measurement_mic_is_never_selected_even_on_a_widened_candidate_list(
    tmp_path: Path,
) -> None:
    """Defense in depth. The registry card list is a closed allowlist no
    measurement mic appears in, so this only bites for an operator who widened
    JASPER_MIC_DEVICE_CANDIDATES — and then it must bite: a UMIK-2 carries no
    wake or AEC contract."""
    _stage(
        tmp_path,
        "udp:9876",
        extra="JASPER_MIC_DEVICE_CANDIDATES=UMIK2,Array\n",
        mode="auto",
    )
    _write_usb_card(tmp_path, "UMIK2", UMIK2_USB_ID, channels=1)

    result = _run_reconcile(tmp_path, "--reason", "systemd")

    assert result.returncode == 0, result.stderr
    body = (tmp_path / "jasper.env").read_text()
    # Not selected, and not left as the published mic identity either: the
    # real (absent) card is what the stale udp: value is cleared to.
    assert "JASPER_MIC_DEVICE=Array" in body
    assert "JASPER_MIC_DEVICE=UMIK2" not in body
    assert "JASPER_AEC_MIC_DEVICE=UMIK2" not in body
    # No usable voice input, so voice parks — the honest outcome.
    assert "stop jasper-voice.service" in _systemctl_log(tmp_path)
    assert _marker(tmp_path).exists()


def test_a_non_measurement_usb_card_is_still_selectable(tmp_path: Path) -> None:
    """The control for the exclusion: a USB capture card whose id is NOT in the
    measurement registry (here the XVF voice array's own id) must still be
    chosen. An over-broad filter would leave the speaker deaf.

    Deliberately not named `Array` — that name is what the XVF profile
    resolver keys on, and a detected managed XVF routes through managed chip
    policy rather than direct selection.
    """
    _stage(
        tmp_path,
        "udp:9876",
        extra="JASPER_MIC_DEVICE_CANDIDATES=USBMIC\n",
        mode="auto",
    )
    _write_usb_card(tmp_path, "USBMIC", XVF_USB_ID, channels=2)

    result = _run_reconcile(tmp_path, "--reason", "systemd")

    assert result.returncode == 0, result.stderr
    assert "JASPER_MIC_DEVICE=USBMIC" in (tmp_path / "jasper.env").read_text()
    assert VOICE_RESTART_CMD in _systemctl_log(tmp_path)


def test_measurement_registry_probe_failure_excludes_nothing(
    tmp_path: Path,
) -> None:
    """Fails OPEN. Refusing to classify must never be able to leave a speaker
    with no microphone at all — the closed allowlist is still standing
    underneath."""
    # Breaks ONLY the measurement-registry bridge; every other bridge (chiefly
    # the XVF profile resolver, whose failure would route this box down the
    # managed-XVF park path instead) keeps working.
    broken = _python_double(
        tmp_path,
        "broken-measurement-python",
        failing_module="jasper.cli.measurement_mic",
    )
    _stage(
        tmp_path,
        "udp:9876",
        extra="JASPER_MIC_DEVICE_CANDIDATES=UMIK2\n",
        mode="auto",
    )
    _write_usb_card(tmp_path, "UMIK2", UMIK2_USB_ID, channels=1)

    result = _run_reconcile(
        tmp_path,
        "--reason",
        "systemd",
        extra_env={"JASPER_MIC_PROFILE_PYTHON": str(broken)},
    )

    assert result.returncode == 0, result.stderr
    assert "JASPER_MIC_DEVICE=UMIK2" in (tmp_path / "jasper.env").read_text()


def test_measurement_exclusion_costs_no_interpreter_without_a_usb_card(
    tmp_path: Path,
) -> None:
    """A card with no usbid (absent, I2S, virtual) cannot be a registered USB
    measurement mic, so the resolver is never spawned for it. Proven with an
    interpreter that fails loudly if it is asked for the measurement
    registry."""
    tripwire = _python_double(
        tmp_path,
        "tripwire-python",
        failing_module="jasper.cli.measurement_mic",
        stderr_message="measurement resolver was spawned",
    )
    # stream0 only, no usbid.
    _stage(tmp_path, "Array", mode="auto", channels=6)

    result = _run_reconcile(
        tmp_path,
        "--reason",
        "systemd",
        extra_env={"JASPER_MIC_PROFILE_PYTHON": str(tripwire)},
    )

    assert result.returncode == 0, result.stderr
    assert "measurement resolver was spawned" not in result.stderr


