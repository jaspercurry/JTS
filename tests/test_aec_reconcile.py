# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from jasper.accessories.constants import WIIM_REMOTE_2_MIC_DEVICE
from jasper.audio_profile_state import profile_env_updates
from jasper.cli import aec_init
from jasper.control import aec_endpoints
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


def _env_assignments(path: Path) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line and not line.lstrip().startswith("#") and "=" in line
    )


def _shell_function_body(source: str, name: str) -> str:
    match = re.search(
        rf"^{re.escape(name)}\(\)\s*\{{\n(.*?)^\}}$",
        source,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match is not None, f"could not locate shell function {name}"
    return match.group(1)


def _fake_mixer_tools(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "mixer.log"
    script = (
        "#!/usr/bin/env bash\n"
        "printf '%s %s\\n' \"${0##*/}\" \"$*\" >> \"$JASPER_MIXER_LOG\"\n"
        "exit 0\n"
    )
    for name in ("amixer", "alsactl"):
        executable = bin_dir / name
        executable.write_text(script)
        executable.chmod(0o755)
    return bin_dir, log


def _run_reconcile(
    tmp_path: Path,
    *args: str,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    fake_systemctl, systemctl_log = _fake_systemctl(tmp_path)
    env = os.environ.copy()
    # These tests drive the active provider exclusively through the
    # VOICE_PROVIDER_FILE they write. The reconciler also has an env-var
    # fallback (valid_voice_provider "$JASPER_VOICE_PROVIDER"), so an
    # ambient JASPER_VOICE_PROVIDER — which CI sets to "gemini" so
    # jasper.config loads, and which a dev shell often exports — would
    # leak in and make the "parks when provider unset/invalid" cases see
    # a valid provider and never park. Drop it so the file is the only
    # source of truth, matching what each test sets up.
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
            # Redirect the voice-input-absent marker into tmp so the no-mic
            # paths (mark_voice_input_absent) never touch the real
            # /var/lib/jasper on the test host. Per-test overrides via
            # extra_env still win (the marker cases assert on this path).
            "JASPER_VOICE_INPUT_ABSENT_MARKER": str(
                tmp_path / "voice-input-absent"
            ),
            # Same reason: accessory_mic_present shells to
            # `jasper.accessories.mic_env`, which reads the REAL
            # /var/lib/jasper/accessory-mics.env unless redirected. Point it at
            # a tmp path that does not exist by default, so every test starts
            # from "no accessory microphone" regardless of the host.
            "JASPER_ACCESSORY_MIC_ENV_FILE": str(
                tmp_path / "accessory-mics.env"
            ),
            "JASPER_SYSTEMCTL": str(fake_systemctl),
            "JASPER_SYSTEMCTL_LOG": str(systemctl_log),
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
) -> Path:
    env_file = tmp_path / "jasper.env"
    env_file.write_text(
        f"JASPER_MIC_DEVICE={mic_device}\n"
        "JASPER_AEC_UDP_PORT=9876\n"
        "JASPER_AUDIO_DAC_ID=apple_usb_c_dongle\n"
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


def _write_synthetic_xvf_resolver(tmp_path: Path, card: str) -> Path:
    resolver = tmp_path / "synthetic-xvf-resolver"
    resolver.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$*\" == *'jasper.cli.xvf_profile'* ]]; then\n"
        "  printf '%s\\n' \\\n"
        "    'JASPER_XVF_PRESENT=1' \\\n"
        "    'JASPER_XVF_VARIANT=xvf3800_future_variant' \\\n"
        "    'JASPER_XVF_DISPLAY_NAME=Future_XVF3800' \\\n"
        "    'JASPER_XVF_GEOMETRY=future' \\\n"
        f"    'JASPER_XVF_ALSA_CARD={card}' \\\n"
        "    'JASPER_XVF_CAPTURE_CHANNELS=6' \\\n"
        "    'JASPER_XVF_CHIP_BEAM_PLAN=' \\\n"
        "    'JASPER_XVF_CHIP_AEC_SUPPORTED=0' \\\n"
        "    'JASPER_XVF_RECOMMENDED_PROFILE=xvf_chip_aec' \\\n"
        "    \"JASPER_XVF_REASON='future XVF needs a validated beam plan'\" \\\n"
        "    'JASPER_XVF_CHIP_REF_PCM_ACCESS=hw' \\\n"
        "    'JASPER_XVF_CHIP_REF_DEVICE=0' \\\n"
        "    'JASPER_XVF_CHIP_REF_RATE=16000' \\\n"
        "    'JASPER_XVF_CHIP_REF_PERIOD=128' \\\n"
        "    'JASPER_XVF_CHIP_REF_BUFFER=256'\n"
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


def test_reconcile_clears_stale_udp_when_array_is_absent(tmp_path: Path) -> None:
    env_file = _write_env(tmp_path, "udp:9876")
    _write_mode(tmp_path)

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


def test_reconcile_enables_udp_aec_when_array_is_6_channel(tmp_path: Path) -> None:
    env_file = _write_env(tmp_path, "Array")
    _write_mode(tmp_path)
    _write_card(tmp_path, channels=6)

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


def test_reconcile_parks_when_final_outputd_restart_fails(
    tmp_path: Path,
) -> None:
    env_file = _write_env(tmp_path, "Array")
    _write_mode(tmp_path)
    _write_card(tmp_path, channels=6)
    fake = tmp_path / "outputd-failure-systemctl"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$JASPER_SYSTEMCTL_LOG\"\n"
        "[[ \"$*\" == 'restart jasper-outputd.service' ]] && exit 1\n"
        "exit 0\n"
    )
    fake.chmod(0o755)

    result = _run_reconcile(
        tmp_path,
        "--reason",
        "test",
        extra_env={"JASPER_SYSTEMCTL": str(fake)},
    )

    assert result.returncode == 0, result.stderr
    body = env_file.read_text()
    assert "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS=fault" in body
    assert "JASPER_OUTPUTD_REFERENCE_UDP_TARGET=''" in body
    assert _marker(tmp_path).exists()
    commands = _systemctl_log(tmp_path)
    assert "restart jasper-aec-init.service" not in commands
    assert "restart jasper-aec-bridge.service" not in commands
    assert VOICE_RESTART_CMD not in commands


def test_reconcile_keeps_native_writer_and_parks_when_commissioning_is_required(
    tmp_path: Path,
) -> None:
    env_file = _write_env(tmp_path, "Array")
    _write_mode(tmp_path)
    _write_card(tmp_path, channels=6)
    fake = tmp_path / "commission-required-systemctl"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$JASPER_SYSTEMCTL_LOG\"\n"
        "[[ \"$*\" == 'restart jasper-aec-init.service' ]] && exit 1\n"
        "[[ \"$1\" == 'show' ]] && { printf '2\\n'; exit 0; }\n"
        "exit 0\n"
    )
    fake.chmod(0o755)

    result = _run_reconcile(
        tmp_path,
        "--reason",
        "test",
        extra_env={"JASPER_SYSTEMCTL": str(fake)},
    )

    assert result.returncode == 0, result.stderr
    body = env_file.read_text()
    assert "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS=commission_required" in body
    assert "JASPER_AEC_CHIP_AEC_ALIGNMENT_ACTION='Run sudo jasper-aec-commission'" in body
    assert "JASPER_OUTPUTD_CHIP_REF_PCM=hw:CARD=Array,DEV=0" in body
    assert "JASPER_OUTPUTD_REFERENCE_UDP_TARGET=''" in body
    assert _marker(tmp_path).exists()
    commands = _systemctl_log(tmp_path)
    assert "restart jasper-aec-bridge.service" not in commands
    assert VOICE_RESTART_CMD not in commands


def test_reconcile_defers_without_commissioning_when_outputd_predates_its_env(
    tmp_path: Path,
) -> None:
    # aec-init's OUTPUTD_ENV_STALE_EXIT: the live outputd has not loaded
    # /var/lib/jasper/outputd.env, so its STATUS describes an edge nobody is
    # running. That is an ordering race, not a moved artifact — it must NOT send
    # the household to the two-minute commissioner.
    env_file = _write_env(tmp_path, "Array")
    _write_mode(tmp_path)
    _write_card(tmp_path, channels=6)
    fake = tmp_path / "outputd-stale-systemctl"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$JASPER_SYSTEMCTL_LOG\"\n"
        "[[ \"$*\" == 'restart jasper-aec-init.service' ]] && exit 1\n"
        f"[[ \"$1\" == 'show' ]] && {{ printf '{aec_init.OUTPUTD_ENV_STALE_EXIT}\\n'; exit 0; }}\n"
        "exit 0\n"
    )
    fake.chmod(0o755)

    result = _run_reconcile(
        tmp_path, "--reason", "test", extra_env={"JASPER_SYSTEMCTL": str(fake)}
    )

    assert result.returncode == 0, result.stderr
    body = env_file.read_text()
    assert "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS=deferred" in body
    assert "jasper-aec-commission" not in body
    assert (
        "JASPER_AEC_CHIP_AEC_ALIGNMENT_ACTION='Wait for jasper-outputd to "
        "restart, then run the reconciler'"
    ) in body
    # Native reference writer stays configured so the next pass can sample it.
    assert "JASPER_OUTPUTD_CHIP_REF_PCM=hw:CARD=Array,DEV=0" in body
    assert _marker(tmp_path).exists()
    commands = _systemctl_log(tmp_path)
    assert "restart jasper-aec-bridge.service" not in commands
    assert VOICE_RESTART_CMD not in commands


def test_reconcile_branches_on_the_exit_codes_aec_init_actually_returns() -> None:
    # Two cross-language literals: the shell compares ExecMainStatus against
    # integers owned by jasper/cli/aec_init.py. Nothing else pins that pairing,
    # and a silent drift would map "wait for outputd" onto the commissioner park
    # (or onto a generic fault).
    body = _shell_function_body(
        SCRIPT.read_text(encoding="utf-8"), "activate_managed_chip_aec"
    )
    assert f'"$init_status" == "{aec_init.COMMISSION_REQUIRED_EXIT}"' in body
    assert f'"$init_status" == "{aec_init.OUTPUTD_ENV_STALE_EXIT}"' in body


def test_reconcile_parks_if_bridge_fails_after_alignment_reapply(
    tmp_path: Path,
) -> None:
    env_file = _write_env(tmp_path, "Array")
    _write_mode(tmp_path)
    _write_card(tmp_path, channels=6)
    fake = tmp_path / "bridge-failure-systemctl"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$JASPER_SYSTEMCTL_LOG\"\n"
        "[[ \"$*\" == 'restart jasper-aec-bridge.service' ]] && exit 1\n"
        "exit 0\n"
    )
    fake.chmod(0o755)

    result = _run_reconcile(
        tmp_path, "--reason", "test",
        extra_env={"JASPER_SYSTEMCTL": str(fake)},
    )

    assert result.returncode == 0, result.stderr
    body = env_file.read_text()
    assert "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS=fault" in body
    assert "JASPER_OUTPUTD_REFERENCE_UDP_TARGET=''" in body
    assert VOICE_RESTART_CMD not in _systemctl_log(tmp_path)


def test_reconcile_repairs_capture_mixer_before_arming_six_channel_aec(
    tmp_path: Path,
) -> None:
    expected = [
        "amixer -c Array cset name=Headset Capture Switch "
        "on,on,on,on,on,on",
        "amixer -c Array cset name=Headset Capture Volume "
        "60,60,60,60,60,60",
        "alsactl store",
    ]

    for channels, should_repair in ((6, True), (2, False)):
        root = tmp_path / str(channels)
        root.mkdir()
        bin_dir, mixer_log = _fake_mixer_tools(root)
        _write_env(root, "Array")
        _write_mode(root)
        _write_card(root, channels=channels)

        result = _run_reconcile(
            root,
            "--reason",
            "test",
            extra_env={
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "JASPER_MIXER_LOG": str(mixer_log),
            },
        )

        assert result.returncode == 0, result.stderr
        calls = mixer_log.read_text().splitlines() if mixer_log.exists() else []
        assert calls == (expected if should_repair else [])


@pytest.mark.parametrize("provider_id", sorted(VALID_PROVIDER_IDS))
def test_reconcile_accepts_catalog_provider_ids(
    tmp_path: Path,
    provider_id: str,
) -> None:
    _write_env(tmp_path, "Array", voice_provider=provider_id)
    _write_mode(tmp_path)
    _write_card(tmp_path, channels=6)

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    commands = _systemctl_log(tmp_path)
    assert "enable jasper-voice.service" in commands
    assert VOICE_RESTART_CMD in commands


def test_reconcile_parks_voice_when_provider_unset(tmp_path: Path) -> None:
    env_file = _write_env(tmp_path, "Array", voice_provider="")
    _write_mode(tmp_path)
    _write_card(tmp_path, channels=6)

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    assert "JASPER_MIC_DEVICE=udp:9876" in env_file.read_text()
    assert "voice provider unset or invalid; leaving jasper-voice parked" in result.stderr
    commands = _systemctl_log(tmp_path)
    assert "disable --now jasper-voice.service" in commands
    assert VOICE_RESTART_CMD not in commands


def test_reconcile_parks_voice_when_provider_invalid(tmp_path: Path) -> None:
    env_file = _write_env(tmp_path, "Array", voice_provider="bad-provider")
    _write_mode(tmp_path)
    _write_card(tmp_path, channels=6)

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    assert "JASPER_MIC_DEVICE=udp:9876" in env_file.read_text()
    assert "voice provider unset or invalid; leaving jasper-voice parked" in result.stderr
    commands = _systemctl_log(tmp_path)
    assert "disable --now jasper-voice.service" in commands
    assert VOICE_RESTART_CMD not in commands


def test_reconcile_parks_voice_when_provider_manifest_missing(tmp_path: Path) -> None:
    env_file = _write_env(tmp_path, "Array", voice_provider="gemini")
    (tmp_path / "voice_provider_ids").unlink()
    _write_mode(tmp_path)
    _write_card(tmp_path, channels=6)

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    assert "JASPER_MIC_DEVICE=udp:9876" in env_file.read_text()
    assert "voice provider unset or invalid; leaving jasper-voice parked" in result.stderr
    commands = _systemctl_log(tmp_path)
    assert "disable --now jasper-voice.service" in commands
    assert VOICE_RESTART_CMD not in commands


def test_reconcile_parks_voice_when_provider_not_in_manifest(tmp_path: Path) -> None:
    env_file = _write_env(tmp_path, "Array", voice_provider="grok")
    (tmp_path / "voice_provider_ids").write_text("gemini\nopenai\n")
    _write_mode(tmp_path)
    _write_card(tmp_path, channels=6)

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    assert "JASPER_MIC_DEVICE=udp:9876" in env_file.read_text()
    assert "voice provider unset or invalid; leaving jasper-voice parked" in result.stderr
    commands = _systemctl_log(tmp_path)
    assert "disable --now jasper-voice.service" in commands
    assert VOICE_RESTART_CMD not in commands


def test_reconcile_parks_managed_xvf_when_array_is_not_6_channel(tmp_path: Path) -> None:
    env_file = _write_env(tmp_path, "udp:9876")
    _write_mode(tmp_path)
    _write_card(tmp_path, channels=2)

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    body = env_file.read_text()
    assert "JASPER_MIC_DEVICE=udp:9876" in body
    assert "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS=unavailable" in body
    commands = _systemctl_log(tmp_path)
    assert "disable jasper-aec-bridge.service jasper-aec-init.service" in commands
    assert "stop jasper-voice.service jasper-aec-bridge.service" in commands
    assert VOICE_RESTART_CMD not in commands


def test_reconcile_respects_custom_mic_device(tmp_path: Path) -> None:
    env_file = _write_env(tmp_path, "UMIK-2")
    _write_mode(tmp_path)

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


def test_no_local_mic_with_accessory_keeps_voice_up(tmp_path: Path) -> None:
    """Issue #2205: a paired accessory mic satisfies the voice-input gate.

    A box with no local microphone but a published push-to-talk source is a
    working speaker. The reconciler must NOT stamp the gate marker (which would
    make PID 1 skip the start and leave the remote's button dead), and must
    (re)start voice so the source is actually read.
    """
    env_file = _write_env(tmp_path, "udp:9876")
    _write_mode(tmp_path)
    _write_accessory_mics(
        tmp_path, f"JASPER_MANUAL_MIC_SOURCES=wiim_remote_2={WIIM_REMOTE_2_MIC_DEVICE}\n",
    )

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "voice-input-absent").exists()
    commands = _systemctl_log(tmp_path)
    assert "stop jasper-voice.service" not in commands
    assert VOICE_RESTART_CMD in commands
    assert "wiim_remote_2" in result.stderr
    # The stale UDP device is still normalised to a real candidate, and the
    # restart is deferred until AFTER that write — otherwise systemd could
    # start voice against the device the reconciler is about to replace.
    assert "JASPER_MIC_DEVICE=Array" in env_file.read_text()


def test_accessory_voice_restart_is_deferred_past_the_mic_device_write(
    tmp_path: Path,
) -> None:
    """The restart must be queued only AFTER the stale mic device is rewritten.

    ``restart_voice`` uses ``systemctl --no-block``, so systemd can start voice
    while this oneshot is still running. If the restart were issued at the
    stop_voice decision point, voice could read JASPER_MIC_DEVICE=udp:9876 —
    the device the reconciler is in the middle of replacing — bind an unfed UDP
    socket and watchdog-restart. Command ORDER in the systemctl log cannot see
    this, so snapshot the env file at the moment the restart is issued.
    """
    env_file = _write_env(tmp_path, "udp:9876")
    _write_mode(tmp_path)
    _write_accessory_mics(
        tmp_path, f"JASPER_MANUAL_MIC_SOURCES=wiim_remote_2={WIIM_REMOTE_2_MIC_DEVICE}\n",
    )
    snapshotting_systemctl = tmp_path / "systemctl-snapshot"
    snapshotting_systemctl.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" >> "$JASPER_SYSTEMCTL_LOG"\n'
        'if [[ "$*" == *"restart jasper-voice.service"* ]]; then\n'
        '  cp "$JASPER_ENV_FILE" "$JASPER_ENV_SNAPSHOT"\n'
        "fi\n"
        "exit 0\n",
    )
    snapshotting_systemctl.chmod(0o755)
    snapshot = tmp_path / "jasper.env.at-restart"

    result = _run_reconcile(
        tmp_path,
        "--reason",
        "test",
        extra_env={
            "JASPER_SYSTEMCTL": str(snapshotting_systemctl),
            "JASPER_ENV_SNAPSHOT": str(snapshot),
        },
    )

    assert result.returncode == 0, result.stderr
    assert snapshot.exists(), "voice was never restarted"
    assert "JASPER_MIC_DEVICE=Array" in snapshot.read_text()
    assert "JASPER_MIC_DEVICE=udp:9876" not in snapshot.read_text()
    assert "JASPER_MIC_DEVICE=Array" in env_file.read_text()


def test_publishes_local_mic_absent_for_the_daemon(tmp_path: Path) -> None:
    """The daemon half of #2205 needs to know WHICH half satisfied the gate.

    The marker is the AND of both absences, so it cannot say. This reconciler
    owns local-mic presence, so it publishes that half as a fact the daemon
    reads instead of re-deriving — and a `0` here is what lets jasper-voice
    plan zero wake legs and serve the remote's button.
    """
    env_file = _write_env(tmp_path, "udp:9876")
    _write_mode(tmp_path)
    _write_accessory_mics(
        tmp_path,
        f"JASPER_MANUAL_MIC_SOURCES=wiim_remote_2={WIIM_REMOTE_2_MIC_DEVICE}\n",
    )

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    assert _env_assignments(env_file)["JASPER_LOCAL_MIC_PRESENT"] == "0"


def test_publishes_local_mic_present_when_a_candidate_card_exists(
    tmp_path: Path,
) -> None:
    """The positive half. A mic-bearing speaker must NEVER read as absent —
    that would drop its wake leg and leave it deaf until someone pressed a
    button it may not even have."""
    env_file = _write_env(tmp_path, "Array")
    _write_mode(tmp_path)
    _write_card(tmp_path, "Array", channels=2)

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    assert _env_assignments(env_file)["JASPER_LOCAL_MIC_PRESENT"] == "1"


def test_publishes_unknown_for_a_custom_mic_device(tmp_path: Path) -> None:
    """A custom JASPER_MIC_DEVICE is an operator device this script does not
    manage, and whose name need not appear in MIC_CANDIDATES at all — so the
    absence of a candidate card says nothing about it.

    Publishing `0` here would be the dangerous answer: the daemon would drop
    the primary leg and never open the operator's mic. Publishing nothing
    would be nearly as bad, because a stale `0` from an earlier pass would
    survive and do the same thing.
    """
    env_file = _write_env(tmp_path, "UMIK-2")
    _write_mode(tmp_path)
    # Seed the stale value this path must overwrite.
    env_file.write_text(
        env_file.read_text() + "JASPER_LOCAL_MIC_PRESENT=0\n",
    )

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    assert _env_assignments(env_file)["JASPER_LOCAL_MIC_PRESENT"] == "unknown"


def test_local_mic_presence_is_published_before_voice_is_restarted(
    tmp_path: Path,
) -> None:
    """Order matters: restart_voice uses `systemctl --no-block`, so systemd
    can start jasper-voice while this oneshot is still running. If the
    published verdict landed after the restart was queued, the daemon could
    read a stale value and plan the wrong leg set for a whole run."""
    _write_env(tmp_path, "udp:9876")
    _write_mode(tmp_path)
    _write_accessory_mics(
        tmp_path,
        f"JASPER_MANUAL_MIC_SOURCES=wiim_remote_2={WIIM_REMOTE_2_MIC_DEVICE}\n",
    )
    snapshotting_systemctl = tmp_path / "systemctl-snapshot"
    snapshotting_systemctl.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" >> "$JASPER_SYSTEMCTL_LOG"\n'
        'if [[ "$*" == *"restart jasper-voice.service"* ]]; then\n'
        '  cp "$JASPER_ENV_FILE" "$JASPER_ENV_SNAPSHOT"\n'
        "fi\n"
        "exit 0\n",
    )
    snapshotting_systemctl.chmod(0o755)
    snapshot = tmp_path / "jasper.env.at-restart"

    result = _run_reconcile(
        tmp_path,
        "--reason",
        "test",
        extra_env={
            "JASPER_SYSTEMCTL": str(snapshotting_systemctl),
            "JASPER_ENV_SNAPSHOT": str(snapshot),
        },
    )

    assert result.returncode == 0, result.stderr
    assert snapshot.exists(), "voice was never restarted"
    assert _env_assignments(snapshot)["JASPER_LOCAL_MIC_PRESENT"] == "0"


def test_no_local_mic_and_no_accessory_still_parks_voice(tmp_path: Path) -> None:
    """The other half of #2205: absence of BOTH is what the marker claims."""
    _write_env(tmp_path, "udp:9876")
    _write_mode(tmp_path)
    # No accessory-mics.env written at all.

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    marker = tmp_path / "voice-input-absent"
    assert marker.exists()
    assert "no accessory microphone paired" in marker.read_text()
    commands = _systemctl_log(tmp_path)
    assert "stop jasper-voice.service" in commands
    assert VOICE_RESTART_CMD not in commands


def test_malformed_accessory_env_parks_voice(tmp_path: Path) -> None:
    """Fail closed on an unparsable accessory file — and say *that*, not
    "no remote is paired".

    ``Config.from_env`` *raises* on a malformed JASPER_MANUAL_MIC_SOURCES entry,
    and that is not one of jasper-voice's clean-park exits — opening the gate on
    a file the daemon will reject would crash-loop it into
    StartLimitAction=reboot. Parking is the safe answer.

    Parking was never the question. The reason was: this file NAMES
    wiim_remote_2, and the marker used to answer "no accessory microphone
    paired" — byte-identical to the no-file case, and read verbatim by an
    operator through /state.microphone.reason and the doctor headline.
    """
    _write_env(tmp_path, "udp:9876")
    _write_mode(tmp_path)
    # One usable entry beside a broken one: the case where a lenient parser
    # would open the gate for a Config that raises at daemon startup.
    _write_accessory_mics(
        tmp_path,
        f"JASPER_MANUAL_MIC_SOURCES=wiim_remote_2={WIIM_REMOTE_2_MIC_DEVICE},bad\n",
    )

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    marker = tmp_path / "voice-input-absent"
    assert marker.exists()
    reason = marker.read_text()
    assert "could not be determined" in reason
    assert "no accessory microphone paired" not in reason
    # The parser's own sentence — which rule the content broke — reaches the
    # journal, because that sentence IS the remediation.
    assert "refusing to publish accessory mic sources" in result.stderr
    assert "must be source_id=device" in result.stderr
    assert "stop jasper-voice.service" in _systemctl_log(tmp_path)


def test_failed_accessory_probe_parks_with_an_honest_reason(tmp_path: Path) -> None:
    """The partial-/opt/jasper-deploy shape: the interpreter serves
    jasper.cli.xvf_profile normally but cannot answer jasper.accessories.mic_env.

    A remote IS paired and published. The reconciler must still park (fail
    closed) but must NOT assert that no accessory is paired — that reason string
    is surfaced verbatim through /state.microphone.reason and the doctor
    headline, and an operator debugging "my remote does nothing" would be handed
    a confident wrong answer. "I could not tell" and "I checked and there is
    nothing" are different facts."""
    _write_env(tmp_path, "udp:9876")
    (tmp_path / "aec_mode.env").write_text(
        "JASPER_AEC_MODE=auto\nJASPER_AUDIO_INPUT_PROFILE=custom\n",
    )
    _write_accessory_mics(
        tmp_path, f"JASPER_MANUAL_MIC_SOURCES=wiim_remote_2={WIIM_REMOTE_2_MIC_DEVICE}\n",
    )
    partial = tmp_path / "partial-deploy-python"
    partial.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$*\" == *'jasper.accessories.mic_env'* ]]; then\n"
        "  echo 'ModuleNotFoundError: jasper.accessories.mic_env' >&2\n"
        "  exit 1\n"
        "fi\n"
        "exit 0\n",
    )
    partial.chmod(0o755)

    result = _run_reconcile(
        tmp_path,
        "--reason",
        "test",
        extra_env={"JASPER_MIC_PROFILE_PYTHON": str(partial)},
    )

    assert result.returncode == 0, result.stderr
    marker = tmp_path / "voice-input-absent"
    assert marker.exists()
    reason = marker.read_text()
    assert "could not be determined" in reason
    assert "probe failed" in reason
    # The confident-wrong-answer string must NOT appear.
    assert "no accessory microphone paired" not in reason
    assert "accessory mic probe failed" in result.stderr
    # The module's own stderr reaches the journal rather than /dev/null.
    assert "ModuleNotFoundError" in result.stderr


def test_accessory_probe_without_interpreter_parks_voice(tmp_path: Path) -> None:
    """A missing interpreter must degrade to the pre-#2205 behaviour, not to an
    open gate: no accessory verdict means park.

    Pinned on the ``custom`` profile because that is the only shape that
    *reaches* stop_voice without an interpreter — a managed profile parks
    earlier, on the mic-profile resolver being unavailable. Both are fail-closed;
    this asserts the accessory probe adds no third, open-gate outcome.
    """
    _write_env(tmp_path, "udp:9876")
    (tmp_path / "aec_mode.env").write_text(
        "JASPER_AEC_MODE=auto\nJASPER_AUDIO_INPUT_PROFILE=custom\n",
    )
    _write_accessory_mics(
        tmp_path, f"JASPER_MANUAL_MIC_SOURCES=wiim_remote_2={WIIM_REMOTE_2_MIC_DEVICE}\n",
    )

    result = _run_reconcile(
        tmp_path,
        "--reason",
        "test",
        extra_env={
            "JASPER_MIC_PROFILE_PYTHON": str(tmp_path / "no-such-interpreter"),
        },
    )

    assert result.returncode == 0, result.stderr
    marker = tmp_path / "voice-input-absent"
    assert marker.exists()
    assert "could not be determined" in marker.read_text()
    assert "no accessory microphone paired" not in marker.read_text()
    assert "accessory mic probe unavailable" in result.stderr
    assert "stop jasper-voice.service" in _systemctl_log(tmp_path)


def test_accessory_mic_does_not_unpark_managed_xvf(tmp_path: Path) -> None:
    """Scope guard: park_managed_xvf stays accessory-blind on purpose.

    That path leaves JASPER_MIC_DEVICE on the AEC bridge's udp: transport while
    stop_disable_aec has just stopped the bridge. Starting voice there binds an
    unfed UDP socket and watchdog-restarts into StartLimitAction=reboot, so it
    needs the mic device normalised first — a separate change.
    """
    _write_env(tmp_path, "udp:9876")
    _write_profile_mode(tmp_path, "xvf_chip_aec")
    _write_card(tmp_path, channels=2)
    _write_accessory_mics(
        tmp_path, f"JASPER_MANUAL_MIC_SOURCES=wiim_remote_2={WIIM_REMOTE_2_MIC_DEVICE}\n",
    )

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "voice-input-absent").exists()
    assert VOICE_RESTART_CMD not in _systemctl_log(tmp_path)


def test_check_aec_ready_reflects_mode_and_firmware(tmp_path: Path) -> None:
    _write_env(
        tmp_path,
        "Array",
        extra="JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS=ready\n",
    )
    _write_profile_mode(tmp_path, "auto")
    _write_card(tmp_path, channels=6)
    assert _run_reconcile(tmp_path, "--check-aec-ready").returncode == 0

    failed_init = tmp_path / "failed-init-systemctl"
    failed_init.write_text(
        "#!/usr/bin/env bash\n"
        "[[ \"$*\" == 'is-active --quiet jasper-aec-init.service' ]] && exit 1\n"
        "exit 0\n"
    )
    failed_init.chmod(0o755)
    assert _run_reconcile(
        tmp_path, "--check-aec-ready",
        extra_env={"JASPER_SYSTEMCTL": str(failed_init)},
    ).returncode == 1

    (tmp_path / "aec_mode.env").write_text("JASPER_AEC_MODE=disabled\n")
    assert _run_reconcile(tmp_path, "--check-aec-ready").returncode == 1

    (tmp_path / "aec_mode.env").write_text("JASPER_AEC_MODE=auto\n")
    (tmp_path / "asound" / "Array" / "stream0").write_text("Capture:\n  Channels: 2\n")
    assert _run_reconcile(tmp_path, "--check-aec-ready").returncode == 1


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


def test_ensure_mode_file_seeds_default_leg_keys(tmp_path: Path) -> None:
    """Fresh install (no aec_mode.env): the reconciler creates the file
    with the documented defaults — AEC auto, RAW on, DTLN off. These
    must match install.sh's reconcile_aec_state seed verbatim."""
    _write_env(tmp_path, "Array")
    _run_reconcile(tmp_path, "--reason", "test")
    body = (tmp_path / "aec_mode.env").read_text()
    assert "JASPER_AUDIO_INPUT_PROFILE=auto" in body
    assert "JASPER_AEC_MODE=auto" in body
    assert "JASPER_WAKE_LEG_RAW=1" in body
    assert "JASPER_WAKE_LEG_DTLN=0" in body


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


def test_install_leg_seed_matches_control_fallback() -> None:
    """The install-time seed and control's missing-file view stay aligned."""
    install = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
    function = _shell_function_body(install, "reconcile_aec_state")
    key_pattern = "|".join(re.escape(key) for key in _control_leg_defaults())
    pairs = re.findall(rf"({key_pattern})=([01])\\n", function)

    assert len(pairs) == len(_control_leg_defaults()), pairs
    assert dict(pairs) == _control_leg_defaults()


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
    env_file = _write_env(tmp_path, "Array")
    _write_mode(tmp_path)
    _write_card(tmp_path, channels=6)

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    assert "JASPER_MIC_DEVICE=udp:9876" in env_file.read_text()
    assert oct(env_file.stat().st_mode & 0o777) == "0o640"


def test_ensure_mode_file_appends_missing_leg_keys(tmp_path: Path) -> None:
    """Pre-leg-toggle deploy: aec_mode.env has only JASPER_AEC_MODE.
    Reconciler should append the new keys with defaults — preserving
    the operator's mode but picking up new fields on upgrade."""
    (tmp_path / "aec_mode.env").write_text("JASPER_AEC_MODE=disabled\n")
    _write_env(tmp_path, "Array")
    _run_reconcile(tmp_path, "--reason", "test")
    body = (tmp_path / "aec_mode.env").read_text()
    assert "JASPER_AEC_MODE=disabled" in body
    assert "JASPER_WAKE_LEG_RAW=1" in body
    assert "JASPER_WAKE_LEG_DTLN=0" in body
    assert "JASPER_AUDIO_INPUT_PROFILE=direct_mic" in body


def test_fresh_auto_profile_uses_chip_aec_on_supported_6ch_xvf(tmp_path: Path) -> None:
    """A truly fresh aec_mode.env defaults to the canonical auto profile.
    On the recommended 6-channel XVF3800 shape plus a measured output DAC
    profile, that resolves to chip-AEC rather than stacked software legs."""
    _write_env(tmp_path, "Array", extra="JASPER_AUDIO_DAC_ID=apple_usb_c_dongle\n")
    _write_card(tmp_path, channels=6)

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
    _write_env(tmp_path, "Array")
    _write_card(tmp_path, channels=6)

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
    _write_env(
        tmp_path,
        "Array",
        extra=(
            "JASPER_AUDIO_DAC_ID=apple_usb_c_dongle\n"
            "JASPER_XVF_VARIANT=xvf3800_legacy_square_6ch\n"
            "JASPER_XVF_GEOMETRY=square\n"
            "JASPER_XVF_CHIP_BEAM_PLAN=xvf_square_fixed_150_210\n"
            "JASPER_XVF_CHIP_AEC_SUPPORTED=1\n"
        ),
    )
    _write_card(tmp_path, channels=6)

    result = _run_reconcile(
        tmp_path,
        "--reason",
        "test",
        extra_env={"JASPER_MIC_PROFILE_PYTHON": str(tmp_path / "missing-python")},
    )

    assert result.returncode == 0, result.stderr
    assert "mic profile resolver unavailable" in result.stderr
    body = (tmp_path / "jasper.env").read_text()
    assert "JASPER_XVF_CHIP_AEC_SUPPORTED=0" in body
    assert "JASPER_XVF_CHIP_BEAM_PLAN=''" in body
    assert "JASPER_AEC_CHIP_AEC_ENABLED=0" in body
    assert "JASPER_MIC_DEVICE_RAW=udp:" not in body
    assert "JASPER_MIC_DEVICE_CHIP_AEC_150=udp:" not in body
    assert "no AEC3/direct fallback" in result.stderr


@pytest.mark.parametrize(
    ("dac_id", "stderr_phrase"),
    [
        ("hifiberry_dac8x_studio", "HiFiBerry DAC8x Studio needs per-profile"),
        ("mystery_usb_audio", "has no codified chip-AEC calibration"),
    ],
)
def test_auto_profile_parks_when_output_dac_needs_calibration(
    tmp_path: Path,
    dac_id: str,
    stderr_phrase: str,
) -> None:
    """Chip-AEC is gated by both XVF firmware and output DAC timing support.
    Calibration-required and future DAC profiles visibly park; a managed XVF
    never inherits an AEC3/direct fallback."""
    _write_env(tmp_path, "Array", extra=f"JASPER_AUDIO_DAC_ID={dac_id}\n")
    _write_card(tmp_path, channels=6)

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    assert stderr_phrase in result.stderr
    body = (tmp_path / "jasper.env").read_text()
    assert "JASPER_MIC_DEVICE_RAW=udp:" not in body
    assert "JASPER_AEC_CHIP_AEC_ENABLED=0" in body
    assert "JASPER_MIC_DEVICE_CHIP_AEC_150=udp:" not in body
    assert "JASPER_OUTPUTD_CHIP_REF_PCM=''" in body
    assert "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS=unavailable" in body
    assert "JASPER_MIC_DEVICE=Array" in body
    assert "no AEC3/direct fallback" in result.stderr


def test_explicit_chip_profile_parks_for_uncalibrated_output_dac(
    tmp_path: Path,
) -> None:
    """Even an explicit xvf_chip_aec profile is fail-closed for output
    profiles whose reference timing contract requires calibration."""
    _write_env(
        tmp_path,
        "Array",
        extra="JASPER_AUDIO_DAC_ID=dual_apple_usb_c_dac_4ch\n",
    )
    _write_profile_mode(tmp_path, "xvf_chip_aec")
    _write_card(tmp_path, channels=6)

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    assert "measured-sync contract" in result.stderr
    body = (tmp_path / "jasper.env").read_text()
    assert "JASPER_MIC_DEVICE_RAW=udp:" not in body
    assert "JASPER_AEC_CHIP_AEC_ENABLED=0" in body
    assert "JASPER_OUTPUTD_CHIP_REF_PCM=''" in body
    assert "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS=unavailable" in body


def test_testing_profile_cannot_bypass_managed_xvf_product_policy(
    tmp_path: Path,
) -> None:
    """The managed UI testing alias is not the low-level custom escape hatch."""
    _write_env(
        tmp_path,
        "Array",
        extra="JASPER_AUDIO_DAC_ID=mystery_usb_audio\n",
    )
    _write_profile_mode(tmp_path, "xvf_chip_aec_testing")
    _write_card(tmp_path, channels=6)

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    body = (tmp_path / "jasper.env").read_text()
    assert "JASPER_AEC_CHIP_AEC_ENABLED=0" in body
    assert "JASPER_MIC_DEVICE_CHIP_AEC_150=udp:" not in body
    assert "JASPER_MIC_DEVICE_CHIP_AEC_210=udp:" not in body
    assert "JASPER_MIC_DEVICE_RAW=udp:" not in body
    assert "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS=unavailable" in body
    assert "no AEC3/direct fallback" in result.stderr


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
    """A stale custom-looking device must not bypass managed chip-or-park."""
    env_file = _write_env(
        tmp_path,
        "UMIK-2",
        extra="JASPER_AUDIO_DAC_ID=apple_usb_c_dongle\n",
    )
    _write_profile_mode(tmp_path, profile)
    _write_card(tmp_path, channels=6)

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
    env_file = _write_env(tmp_path, "operator-mic")
    _write_profile_mode(tmp_path, "auto")
    _write_card(tmp_path, card="FutureXvf", channels=6)
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
    assert "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS=unavailable" in body
    assert "JASPER_AEC_CHIP_AEC_ENABLED=0" in body
    assert "JASPER_MIC_DEVICE_RAW=udp:" not in body
    assert "future XVF needs a validated beam plan" in result.stderr
    assert (tmp_path / "voice-input-absent").exists()


def test_explicit_chip_profile_uses_static_hifiberry_known_good(
    tmp_path: Path,
) -> None:
    """JTS3 path: measured HiFiBerry DAC8x hardware is codified known-good.

    It must not depend on outputd's live SRO verdict at reconcile time; that
    verdict is useful observability, but it is too noisy to be the boot gate for
    hardware we have already approved.
    """
    _write_env(
        tmp_path,
        "udp:9876",
        extra="JASPER_AUDIO_DAC_ID=hifiberry_dac8x\n",
    )
    _write_profile_mode(tmp_path, "xvf_chip_aec")
    _write_card(tmp_path, channels=6)

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


def test_custom_chip_leg_cannot_bypass_unapproved_dac_gate(
    tmp_path: Path,
) -> None:
    """The low-level layer toggle is not a back door around profile policy."""
    _write_env(
        tmp_path,
        "Array",
        extra="JASPER_AUDIO_DAC_ID=mystery_usb_audio\n",
    )
    _write_mode_with_legs(
        tmp_path,
        mode="auto",
        raw="0",
        dtln="0",
        chip_aec="1",
    )
    with (tmp_path / "aec_mode.env").open("a", encoding="utf-8") as f:
        f.write("JASPER_AUDIO_INPUT_PROFILE=custom\n")
    _write_card(tmp_path, channels=6)

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    assert "custom chip-AEC leg requested" in result.stderr
    body = (tmp_path / "jasper.env").read_text()
    assert "JASPER_AEC_CHIP_AEC_ENABLED=0" in body
    assert "JASPER_MIC_DEVICE_CHIP_AEC_150=udp:" not in body
    assert "JASPER_AEC_CHIP_AEC_DAC_STATUS=needs_calibration" in body


def test_auto_profile_does_not_promote_uncodified_dac_from_short_clock_sample(
    tmp_path: Path,
) -> None:
    """Clock telemetry stays diagnostic; the fixed DAC registry authorizes."""
    _write_env(
        tmp_path,
        "udp:9876",
        extra="JASPER_AUDIO_DAC_ID=mystery_usb_audio\n",
    )
    _write_card(tmp_path, channels=6)

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
    assert "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS=unavailable" in body
    assert "JASPER_MIC_DEVICE_CHIP_AEC_150=udp:" not in body
    assert "JASPER_MIC_DEVICE_RAW=udp:" not in body


def test_explicit_chip_profile_parks_for_compensable_verdict(
    tmp_path: Path,
) -> None:
    """A measured but drifting DAC needs the deferred rate-match layer, so
    `compensable` remains parked pending a supported product path."""
    _write_env(
        tmp_path,
        "udp:9876",
        extra="JASPER_AUDIO_DAC_ID=mystery_usb_audio\n",
    )
    _write_profile_mode(tmp_path, "xvf_chip_aec")
    _write_card(tmp_path, channels=6)

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
    assert "JASPER_MIC_DEVICE_RAW=udp:" not in body
    assert "JASPER_MIC_DEVICE_CHIP_AEC_150=udp:" not in body


@pytest.mark.parametrize(
    ("profile", "channels", "expected"),
    [
        (
            "auto", 6,
            {
                "mic": "udp:9876",
                "raw_udp": False,
                "dtln_udp": False,
                "chip_enabled": "1",
                "ref_source": "outputd_udp",
            },
        ),
        (
            "xvf_chip_aec", 6,
            {
                "mic": "udp:9876",
                "raw_udp": False,
                "dtln_udp": False,
                "chip_enabled": "1",
                "ref_source": "outputd_udp",
            },
        ),
        (
            "xvf_software_aec3", 6,
            {
                "mic": "udp:9876",
                "raw_udp": False,
                "dtln_udp": False,
                "chip_enabled": "1",
                "ref_source": "outputd_udp",
            },
        ),
        (
            "direct_mic", 6,
            {
                "mic": "udp:9876",
                "raw_udp": False,
                "dtln_udp": False,
                "chip_enabled": "1",
                "ref_source": "outputd_udp",
            },
        ),
        (
            "auto", 2,
            {
                "mic": "Array",
                "raw_udp": False,
                "dtln_udp": False,
                "chip_enabled": "0",
                "ref_source": "alsa",
            },
        ),
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
    env_file = _write_env(
        tmp_path,
        "Array",
        extra="JASPER_AUDIO_DAC_ID=apple_usb_c_dongle\n",
    )
    _write_profile_mode(tmp_path, profile)
    _write_card(tmp_path, channels=channels)

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    body = env_file.read_text()
    assert f"JASPER_MIC_DEVICE={expected['mic']}" in body
    assert ("JASPER_MIC_DEVICE_RAW=udp:9877" in body) is expected["raw_udp"]
    assert ("JASPER_MIC_DEVICE_DTLN=udp:9878" in body) is expected["dtln_udp"]
    assert f"JASPER_AEC_CHIP_AEC_ENABLED={expected['chip_enabled']}" in body
    assert f"JASPER_AEC_REF_SOURCE={expected['ref_source']}" in body


def test_aec_on_dual_stream_writes_raw_clears_dtln(tmp_path: Path) -> None:
    """AEC auto + RAW=1 + DTLN=0 → writes raw UDP device, clears
    DTLN device, sets DTLN_ENABLED=0. The default dual-stream OSS
    config."""
    _write_env(tmp_path, "udp:9876", extra="JASPER_AUDIO_DAC_ID=apple_usb_c_dongle\n")
    _write_mode_with_legs(tmp_path, mode="auto", raw="1", dtln="0")
    _write_card(tmp_path, channels=6)
    _run_reconcile(tmp_path, "--reason", "test")
    body = (tmp_path / "jasper.env").read_text()
    assert "JASPER_MIC_DEVICE_RAW=udp:9877" in body
    assert "JASPER_MIC_DEVICE_DTLN=" in body  # explicitly cleared
    assert "JASPER_MIC_DEVICE_DTLN=udp:9878" not in body
    assert "JASPER_AEC_DTLN_ENABLED=0" in body


def test_aec_on_triple_stream_writes_all_three(tmp_path: Path) -> None:
    """AEC auto + RAW=1 + DTLN=1 → writes raw UDP device, DTLN UDP
    device, and DTLN_ENABLED=1. The opt-in 2 GB Pi config."""
    _write_env(tmp_path, "udp:9876")
    _write_mode_with_legs(tmp_path, mode="auto", raw="1", dtln="1")
    _write_card(tmp_path, channels=6)
    _run_reconcile(tmp_path, "--reason", "test")
    body = (tmp_path / "jasper.env").read_text()
    assert "JASPER_MIC_DEVICE_RAW=udp:9877" in body
    assert "JASPER_MIC_DEVICE_DTLN=udp:9878" in body
    assert "JASPER_AEC_DTLN_ENABLED=1" in body


def test_aec_on_single_stream_clears_both_legs(tmp_path: Path) -> None:
    """AEC auto + RAW=0 + DTLN=0 → clears all leg-related env vars.
    The 1 GB Pi minimum config when an operator deliberately opts
    out of the dual-stream default."""
    _write_env(tmp_path, "udp:9876")
    _write_mode_with_legs(tmp_path, mode="auto", raw="0", dtln="0")
    _write_card(tmp_path, channels=6)
    _run_reconcile(tmp_path, "--reason", "test")
    body = (tmp_path / "jasper.env").read_text()
    # All three values present but explicitly empty / 0 — set_env_var
    # always writes the line; the reconciler is the only writer.
    assert "JASPER_MIC_DEVICE_RAW=\n" in body or "JASPER_MIC_DEVICE_RAW=" in body
    assert "JASPER_MIC_DEVICE_RAW=udp:" not in body
    assert "JASPER_MIC_DEVICE_DTLN=udp:" not in body
    assert "JASPER_AEC_DTLN_ENABLED=0" in body
    assert "JASPER_AEC_DTLN_ENABLED=1" not in body


def test_aec_disabled_clears_all_legs_even_when_booleans_on(tmp_path: Path) -> None:
    """AEC disabled → clears every leg env var regardless of the
    boolean state in aec_mode.env. A stale JASPER_MIC_DEVICE_RAW
    when the bridge is off would leave voice listening on a port
    nobody talks to (CPU waste in tight retry). Booleans stay
    intact in aec_mode.env — when AEC is re-enabled they apply
    again on the next reconcile."""
    _write_env(tmp_path, "Array")
    _write_mode_with_legs(tmp_path, mode="disabled", raw="1", dtln="1")
    _run_reconcile(tmp_path, "--reason", "test")
    body = (tmp_path / "jasper.env").read_text()
    assert "JASPER_MIC_DEVICE_RAW=udp:" not in body
    assert "JASPER_MIC_DEVICE_DTLN=udp:" not in body
    assert "JASPER_AEC_DTLN_ENABLED=1" not in body
    # Booleans in mode file are preserved.
    mode_body = (tmp_path / "aec_mode.env").read_text()
    assert "JASPER_WAKE_LEG_RAW=1" in mode_body
    assert "JASPER_WAKE_LEG_DTLN=1" in mode_body


def test_normalize_bool_accepts_yes_no(tmp_path: Path) -> None:
    """Operators editing aec_mode.env by hand might write yes/no or
    true/false rather than 1/0. The reconciler should accept either
    — wizard always writes 1/0, but hand-edits shouldn't silently
    fall through to defaults."""
    _write_env(tmp_path, "udp:9876")
    (tmp_path / "aec_mode.env").write_text(
        "JASPER_AEC_MODE=auto\n"
        "JASPER_WAKE_LEG_RAW=yes\n"
        "JASPER_WAKE_LEG_DTLN=true\n"
    )
    _write_card(tmp_path, channels=6)
    _run_reconcile(tmp_path, "--reason", "test")
    body = (tmp_path / "jasper.env").read_text()
    assert "JASPER_MIC_DEVICE_RAW=udp:9877" in body
    assert "JASPER_MIC_DEVICE_DTLN=udp:9878" in body
    assert "JASPER_AEC_DTLN_ENABLED=1" in body


def test_dtln_alone_is_valid_config(tmp_path: Path) -> None:
    """RAW=0 + DTLN=1 is a valid (if unusual) two-leg config —
    primary AEC3 + tertiary DTLN, no chip-direct. The reconciler
    must honor the user's choice rather than coerce it."""
    _write_env(tmp_path, "udp:9876")
    _write_mode_with_legs(tmp_path, mode="auto", raw="0", dtln="1")
    _write_card(tmp_path, channels=6)
    _run_reconcile(tmp_path, "--reason", "test")
    body = (tmp_path / "jasper.env").read_text()
    assert "JASPER_MIC_DEVICE_RAW=udp:" not in body
    assert "JASPER_MIC_DEVICE_DTLN=udp:9878" in body
    assert "JASPER_AEC_DTLN_ENABLED=1" in body


# ---------- Chip-AEC profile + optional beam legs -------------------------
# JASPER_WAKE_LEG_CHIP_AEC selects the chip-AEC profile carrier
# (JASPER_AEC_CHIP_AEC_ENABLED=1 and primary/session audio on :9876), while
# JASPER_WAKE_LEG_CHIP_AEC_150/_210 are independent advanced opt-ins for
# extra openWakeWord detector instances. Chip-AEC remains mutually exclusive
# with raw/DTLN (single-chip Option-A).


def test_ensure_mode_file_seeds_chip_aec_default(tmp_path: Path) -> None:
    """Fresh install: the mode file gets JASPER_WAKE_LEG_CHIP_AEC=0
    alongside the existing leg defaults. Must match install.sh's seed."""
    _write_env(tmp_path, "Array")
    _run_reconcile(tmp_path, "--reason", "test")
    body = (tmp_path / "aec_mode.env").read_text()
    assert "JASPER_WAKE_LEG_CHIP_AEC=0" in body
    assert "JASPER_WAKE_LEG_CHIP_AEC_150=0" in body
    assert "JASPER_WAKE_LEG_CHIP_AEC_210=0" in body


def test_ensure_mode_file_appends_missing_chip_aec_key(tmp_path: Path) -> None:
    """Pre-chip-AEC deploy: aec_mode.env lacks the chip key. Reconciler
    appends it (default off), preserving the operator's existing keys."""
    (tmp_path / "aec_mode.env").write_text(
        "JASPER_AEC_MODE=auto\n"
        "JASPER_WAKE_LEG_RAW=1\n"
        "JASPER_WAKE_LEG_DTLN=1\n"
    )
    _write_env(tmp_path, "Array")
    _run_reconcile(tmp_path, "--reason", "test")
    body = (tmp_path / "aec_mode.env").read_text()
    assert "JASPER_WAKE_LEG_DTLN=1" in body            # preserved
    assert "JASPER_WAKE_LEG_CHIP_AEC=0" in body        # appended
    assert "JASPER_WAKE_LEG_CHIP_AEC_150=0" in body    # appended
    assert "JASPER_WAKE_LEG_CHIP_AEC_210=0" in body    # appended
    assert "JASPER_AUDIO_INPUT_PROFILE=custom" in body  # raw+DTLN is custom


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
    _write_env(tmp_path, "udp:9876", extra="JASPER_AUDIO_DAC_ID=apple_usb_c_dongle\n")
    _write_mode_with_legs(
        tmp_path,
        mode="auto",
        raw="1",
        dtln="1",
        chip_aec="1",
        chip_aec_150="1",
        chip_aec_210="1",
    )
    (tmp_path / "aec_mode.env").write_text(
        (tmp_path / "aec_mode.env").read_text()
        + "JASPER_AUDIO_INPUT_PROFILE=custom\n"
    )
    _write_card(tmp_path, channels=6)

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    body = (tmp_path / "jasper.env").read_text()
    assert "JASPER_AEC_CHIP_AEC_ENABLED=1" in body
    assert "JASPER_MIC_DEVICE_CHIP_AEC_150=udp:9887" in body
    assert "JASPER_MIC_DEVICE_CHIP_AEC_210=udp:9888" in body
    assert "JASPER_MIC_DEVICE_RAW=udp:" not in body
    assert "JASPER_MIC_DEVICE_DTLN=udp:" not in body


def test_flex_linear_auto_discovers_card_but_does_not_arm_square_chip_beams(
    tmp_path: Path,
) -> None:
    """Flex linear firmware enumerates as L16K6Ch, not Array. With no
    explicit JASPER_AEC_MIC_DEVICE pinned, the reconciler should select
    the present Flex card but refuse the legacy square 150/210 chip plan."""
    _write_env(tmp_path, "udp:9876", extra="JASPER_AUDIO_DAC_ID=apple_usb_c_dongle\n")
    _write_profile_mode(tmp_path, "auto")
    _write_card(tmp_path, card="L16K6Ch", channels=6)

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    body = (tmp_path / "jasper.env").read_text()
    assert "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS=unavailable" in body
    assert "JASPER_XVF_VARIANT=xvf3800_flex_linear_6ch" in body
    assert "JASPER_XVF_GEOMETRY=linear" in body
    assert "JASPER_XVF_CHIP_AEC_SUPPORTED=0" in body
    assert "JASPER_AEC_CHIP_AEC_ENABLED=0" in body
    assert "JASPER_MIC_DEVICE_RAW=udp:" not in body
    assert "JASPER_OUTPUTD_CHIP_REF_PCM=''" in body
    assert "aec_mic=L16K6Ch" in result.stderr
    assert "no validated production chip beam plan" in result.stderr


def test_flex_linear_profile_managed_mode_rederives_stale_array_card(
    tmp_path: Path,
) -> None:
    """The reverse swap is also product behavior: replacing a legacy
    Array-flashed XVF with Flex linear must not leave Array pinned."""
    env_file = _write_env(
        tmp_path,
        "udp:9876",
        extra=(
            "JASPER_AUDIO_DAC_ID=apple_usb_c_dongle\n"
            "JASPER_AEC_MIC_DEVICE=Array\n"
        ),
    )
    _write_profile_mode(tmp_path, "auto")
    _write_card(tmp_path, card="L16K6Ch", channels=6)

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    assert "old=Array new=L16K6Ch" in result.stderr
    body = env_file.read_text()
    assert "JASPER_AEC_MIC_DEVICE=L16K6Ch" in body
    assert "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS=unavailable" in body
    assert "JASPER_XVF_VARIANT=xvf3800_flex_linear_6ch" in body
    assert "JASPER_XVF_CHIP_AEC_SUPPORTED=0" in body
    assert "JASPER_AEC_CHIP_AEC_ENABLED=0" in body
    assert "JASPER_MIC_DEVICE_RAW=udp:" not in body


def test_profile_managed_mic_swap_rederives_stale_aec_card(
    tmp_path: Path,
) -> None:
    """Swapping Flex linear (L16K6Ch) for square/circular XVF (Array)
    must not leave the old card id pinned as the bridge capture device.

    The selected profile is product intent; the detected mic profile owns the
    concrete ALSA card in normal non-custom modes.
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
    _write_profile_mode(tmp_path, "xvf_chip_aec")
    _write_card(tmp_path, card="Array", channels=6)

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


def test_check_only_does_not_mask_stale_profile_managed_aec_card(
    tmp_path: Path,
) -> None:
    """The bridge ExecCondition is read-only and must not say "ready" for
    an env file the bridge itself would still read as the wrong card. The
    normal reconciler pass heals this before enabling/restarting the unit."""
    env_file = _write_env(
        tmp_path,
        "udp:9876",
        extra=(
            "JASPER_AUDIO_DAC_ID=apple_usb_c_dongle\n"
            "JASPER_AEC_MIC_DEVICE=L16K6Ch\n"
        ),
    )
    _write_profile_mode(tmp_path, "xvf_chip_aec")
    _write_card(tmp_path, card="Array", channels=6)

    result = _run_reconcile(tmp_path, "--check-aec-ready")

    assert result.returncode == 1
    assert "JASPER_AEC_MIC_DEVICE=L16K6Ch" in env_file.read_text()


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
    with (tmp_path / "aec_mode.env").open("a", encoding="utf-8") as f:
        f.write("JASPER_AUDIO_INPUT_PROFILE=custom\n")
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
    """Regression for the `printf %q` comma-corruption bug (PR #534's
    bug class, in this script): bash 5.2 %q-escapes commas, turning
    hw:CARD=Array,DEV=0 into hw:CARD=Array\\,DEV=0 — which
    systemd EnvironmentFile= reads literally AND which breaks the
    reconciler's own read-back, marking outputd for a restart on every
    pass (restart churn). Two consecutive runs must converge: identical
    env file, no second outputd restart."""
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


def test_chip_aec_off_clears_chip_vars_keeps_raw_dtln_and_outputd_ref(
    tmp_path: Path,
) -> None:
    """Default software AEC: chip vars cleared, raw/DTLN preserved, and
    the far-end reference still comes from outputd's speaker monitor."""
    _write_env(tmp_path, "udp:9876")
    _write_mode_with_legs(tmp_path, mode="auto", raw="1", dtln="1", chip_aec="0")
    _write_card(tmp_path, channels=6)
    _run_reconcile(tmp_path, "--reason", "test")
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
    commands = _systemctl_log(tmp_path)
    assert "restart jasper-outputd.service" in commands


def test_chip_aec_off_clears_chip_usb_reference_but_keeps_outputd_monitor(
    tmp_path: Path,
) -> None:
    """Leaving chip-AEC mode stops the XVF USB-IN producer but keeps
    outputd's UDP speaker monitor because software AEC now consumes it."""
    _write_env(
        tmp_path,
        "udp:9876",
        extra=(
            "JASPER_AEC_REF_SOURCE=outputd_udp\n"
            "JASPER_OUTPUTD_CHIP_REF_PCM=hw:CARD=Array,DEV=0\n"
            "JASPER_OUTPUTD_REFERENCE_UDP_TARGET=127.0.0.1:9891\n"
        ),
    )
    _write_mode_with_legs(tmp_path, mode="auto", raw="1", dtln="0", chip_aec="0")
    _write_card(tmp_path, channels=6)
    _run_reconcile(tmp_path, "--reason", "test")
    body = (tmp_path / "jasper.env").read_text()
    assert "JASPER_AEC_REF_SOURCE=outputd_udp" in body
    assert "JASPER_OUTPUTD_CHIP_REF_PCM=''" in body
    assert "JASPER_OUTPUTD_REFERENCE_UDP_TARGET=127.0.0.1:9891" in body
    commands = _systemctl_log(tmp_path)
    assert "restart jasper-outputd.service" in commands


def test_chip_aec_cleared_when_aec_disabled(tmp_path: Path) -> None:
    """AEC disabled → chip vars cleared too, even with the chip boolean on.
    The boolean stays in the mode file (intent preserved for re-enable)."""
    _write_env(tmp_path, "Array")
    _write_mode_with_legs(
        tmp_path, mode="disabled", raw="1", dtln="0", chip_aec="1",
    )
    _run_reconcile(tmp_path, "--reason", "test")
    body = (tmp_path / "jasper.env").read_text()
    assert "JASPER_MIC_DEVICE_CHIP_AEC_150=udp:" not in body
    assert "JASPER_MIC_DEVICE_CHIP_AEC_210=udp:" not in body
    assert "JASPER_AEC_CHIP_AEC_ENABLED=1" not in body
    assert "JASPER_AEC_REF_SOURCE=alsa" in body
    assert "JASPER_OUTPUTD_REFERENCE_UDP_TARGET=''" in body
    mode_body = (tmp_path / "aec_mode.env").read_text()
    assert "JASPER_WAKE_LEG_CHIP_AEC=1" in mode_body


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
    assert "JASPER_AEC_REF_SOURCE=alsa" in body


# ---------- Chip-ref observe mode (chip-AEC Layer 0 bootstrap) ------------
# JASPER_AEC_CHIP_REF_OBSERVE (opt-in, default off) arms outputd's chip-ref
# writer FOR DRIFT MEASUREMENT ONLY on the software-AEC3 leg path — the mic
# path stays software AEC3 (chip-AEC NOT armed). It breaks the bootstrap
# deadlock on unapproved independent-clock DACs: the reconciler won't arm
# chip-AEC until drift is measured, but drift can only be measured while the
# writer runs. The estimator then reads real DAC-vs-XVF counters that become
# the calibration. CRITICAL safety property: observe NEVER touches the mic path
# — only adds the chip-ref producer.


def test_ensure_mode_file_seeds_chip_ref_observe_default(tmp_path: Path) -> None:
    """Fresh install: the mode file gets JASPER_AEC_CHIP_REF_OBSERVE=0
    alongside the leg defaults. Must match install.sh's seed verbatim."""
    _write_env(tmp_path, "Array")
    _run_reconcile(tmp_path, "--reason", "test")
    body = (tmp_path / "aec_mode.env").read_text()
    assert "JASPER_AEC_CHIP_REF_OBSERVE=0" in body


def test_ensure_mode_file_appends_missing_chip_ref_observe_key(
    tmp_path: Path,
) -> None:
    """Pre-observe deploy: aec_mode.env lacks the observe key. Reconciler
    appends it (default off), preserving the operator's existing keys."""
    (tmp_path / "aec_mode.env").write_text(
        "JASPER_AEC_MODE=auto\n"
        "JASPER_WAKE_LEG_RAW=1\n"
        "JASPER_WAKE_LEG_DTLN=0\n"
        "JASPER_WAKE_LEG_CHIP_AEC=0\n"
    )
    _write_env(tmp_path, "Array")
    _run_reconcile(tmp_path, "--reason", "test")
    body = (tmp_path / "aec_mode.env").read_text()
    assert "JASPER_WAKE_LEG_RAW=1" in body              # preserved
    assert "JASPER_AEC_CHIP_REF_OBSERVE=0" in body      # appended


def test_chip_ref_observe_arms_writer_but_keeps_software_aec3_mic_path(
    tmp_path: Path,
) -> None:
    """SAFETY-CRITICAL: observe=1 on a software-AEC3 path (uncalibrated DAC
    that falls back from auto) arms outputd's chip-ref writer FOR MEASUREMENT
    but leaves the mic path on software AEC3 — chip-AEC stays disabled and
    the raw/AEC3 leg stays intact. This is the bootstrap path that feeds the
    Layer-0 SRO estimator for DACs that are not yet approved."""
    _write_env(tmp_path, "udp:9876", extra="JASPER_AUDIO_DAC_ID=mystery_usb_audio\n")
    _write_mode_with_legs(
        tmp_path, mode="auto", raw="1", dtln="0", chip_aec="0",
        chip_ref_observe="1",
    )
    _write_card(tmp_path, channels=6)
    result = _run_reconcile(tmp_path, "--reason", "test")
    assert result.returncode == 0, result.stderr
    body = (tmp_path / "jasper.env").read_text()
    # Writer armed for measurement.
    assert "JASPER_OUTPUTD_CHIP_REF_PCM=hw:CARD=Array,DEV=0" in body
    assert "JASPER_OUTPUTD_CHIP_REF_OBSERVE=1" in body
    # Mic path is UNCHANGED: software AEC3 with the raw leg, chip-AEC OFF.
    assert "JASPER_AEC_CHIP_AEC_ENABLED=0" in body
    assert "JASPER_MIC_DEVICE_RAW=udp:9877" in body
    assert "JASPER_MIC_DEVICE_CHIP_AEC_150=udp:" not in body
    assert "JASPER_AEC_REF_SOURCE=outputd_udp" in body
    # The reconciler announces why the writer is on.
    assert "chip-ref observe mode" in result.stderr
    # outputd restarts to pick up the newly-armed writer.
    assert "restart jasper-outputd.service" in _systemctl_log(tmp_path)


def test_chip_ref_observe_off_keeps_writer_off_on_software_aec3(
    tmp_path: Path,
) -> None:
    """observe=0 (default) preserves current behavior: the software-AEC3 path
    leaves the chip-ref writer OFF and the observe flag clear."""
    _write_env(tmp_path, "udp:9876", extra="JASPER_AUDIO_DAC_ID=mystery_usb_audio\n")
    _write_mode_with_legs(
        tmp_path, mode="auto", raw="1", dtln="0", chip_aec="0",
        chip_ref_observe="0",
    )
    _write_card(tmp_path, channels=6)
    result = _run_reconcile(tmp_path, "--reason", "test")
    assert result.returncode == 0, result.stderr
    body = (tmp_path / "jasper.env").read_text()
    assert "JASPER_OUTPUTD_CHIP_REF_PCM=''" in body
    assert "JASPER_OUTPUTD_CHIP_REF_OBSERVE=0" in body
    assert "JASPER_AEC_CHIP_AEC_ENABLED=0" in body
    assert "JASPER_MIC_DEVICE_RAW=udp:9877" in body
    assert "chip-ref observe mode" not in result.stderr


def test_chip_ref_observe_noops_without_chip_capable_mic(tmp_path: Path) -> None:
    """observe=1 but the XVF Array is not 6-channel → the bridge doesn't run,
    so there's no chip-capable mic to source the reference. Observe no-ops:
    the writer stays off and the observe flag is clear (the bridge-down path
    forces observe_flag=0). Guards against arming a producer on the
    direct-mic fallback shape."""
    _write_env(
        tmp_path, "udp:9876", extra="JASPER_AUDIO_DAC_ID=mystery_usb_audio\n"
    )
    _write_mode_with_legs(
        tmp_path, mode="auto", raw="1", dtln="0", chip_aec="0",
        chip_ref_observe="1",
    )
    # 2-channel firmware → not aec_ready → bridge down (no chip reference).
    _write_card(tmp_path, channels=2)
    result = _run_reconcile(tmp_path, "--reason", "test")
    assert result.returncode == 0, result.stderr
    body = (tmp_path / "jasper.env").read_text()
    assert "JASPER_OUTPUTD_CHIP_REF_PCM=''" in body
    assert "JASPER_OUTPUTD_CHIP_REF_OBSERVE=0" in body
    assert "JASPER_AEC_CHIP_AEC_ENABLED=0" in body
    assert "chip-ref observe mode" not in result.stderr


def test_reconcile_parks_voice_and_aec_for_bonded_follower(tmp_path: Path) -> None:
    """The dumb-follower profile: the Python-validated park flag in
    grouping-voice.env parks voice (disable --now, never a boot-window
    start) AND the AEC stack, before any mic/profile logic — a fully
    healthy Array + valid provider must not override role state."""
    _write_env(tmp_path, "Array", voice_provider="gemini")
    _write_mode(tmp_path)
    _write_card(tmp_path, channels=6)
    (tmp_path / "grouping-voice.env").write_text(
        f"{VOICE_TTS_SOCKET_ENV}={OUTPUTD_TTS_SOCKET}\n"
        f"{VOICE_PARK_ENV}=1\n"
    )

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    assert "bonded follower" in result.stderr
    commands = _systemctl_log(tmp_path)
    assert "disable --now jasper-voice.service" in commands
    assert "stop jasper-aec-bridge.service jasper-aec-init.service" in commands
    assert VOICE_RESTART_CMD not in commands
    assert "restart jasper-aec-bridge.service" not in commands


def test_reconcile_unparks_voice_when_flag_absent(tmp_path: Path) -> None:
    """Unbond (or promotion to leader): the flag disappears from
    grouping-voice.env and the very next reconcile resumes the normal
    restart path — recovery needs no operator step."""
    env_file = _write_env(tmp_path, "Array", voice_provider="gemini")
    _write_mode(tmp_path)
    _write_card(tmp_path, channels=6)
    (tmp_path / "grouping-voice.env").write_text(
        f"{VOICE_TTS_SOCKET_ENV}={OUTPUTD_TTS_SOCKET}\n"
    )

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    assert "JASPER_MIC_DEVICE=udp:9876" in env_file.read_text()
    commands = _systemctl_log(tmp_path)
    assert VOICE_RESTART_CMD in commands
    assert "enable jasper-voice.service" in commands


# --- microphone-presence marker (docs/HANDOFF-hotplug-resilience.md) ----
# The reconciler is the single writer of the persistent NEGATIVE marker
# jasper-voice.service gates on (ConditionPathExists=!<marker>). These pin
# both convergence directions: marker CREATED whenever voice is parked for
# no mic, REMOVED whenever a mic is present (incl. the custom-mic path,
# which must never be gated by us). _run_reconcile already redirects the
# marker into tmp_path (see its env setup), so these just locate the file.

def _marker(tmp_path: Path) -> Path:
    return tmp_path / "voice-input-absent"


def test_reconcile_is_noop_while_foreground_commissioner_owns_lifecycle(
    tmp_path: Path,
) -> None:
    env_file = _write_env(tmp_path, "Array")
    before = env_file.read_bytes()
    (tmp_path / "chip-aec-commission-active").write_text("pid=123\n")
    (tmp_path / "proc" / "123").mkdir(parents=True)

    result = _run_reconcile(tmp_path, "--reason", "hotplug")

    assert result.returncode == 0, result.stderr
    assert "foreground commissioner owns AEC lifecycle" in result.stderr
    assert env_file.read_bytes() == before
    assert not (tmp_path / "aec_mode.env").exists()
    assert _systemctl_log(tmp_path) == ""
    assert _run_reconcile(tmp_path, "--check-aec-ready").returncode == 1


def test_reconcile_marks_voice_input_absent_when_no_mic(tmp_path: Path) -> None:
    # No card present at all + a stale udp device -> the no-candidate-mic
    # park path. Voice must be gated off so it can't boot-start and
    # crash-loop into StartLimitAction=reboot.
    _write_env(tmp_path, "udp:9876")
    _write_mode(tmp_path)

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    assert _marker(tmp_path).exists(), result.stderr
    assert "stop jasper-voice.service" in _systemctl_log(tmp_path)


def test_reconcile_marks_voice_input_absent_when_aec_disabled_no_mic(
    tmp_path: Path,
) -> None:
    # The AEC-disabled branch has its own no-mic stop path; it must mark too.
    _write_env(tmp_path, "udp:9876")
    _write_mode(tmp_path, mode="disabled")

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    assert _marker(tmp_path).exists(), result.stderr


def test_reconcile_clears_marker_when_6ch_present(tmp_path: Path) -> None:
    # A stale marker (box previously had no mic) must be removed the moment
    # the 6-channel Array reappears, so the ConditionPathExists gate opens.
    _write_env(tmp_path, "Array")
    _write_mode(tmp_path)
    _write_card(tmp_path, channels=6)
    _marker(tmp_path).write_text("reason=stale\n")

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    assert not _marker(tmp_path).exists(), result.stderr
    assert VOICE_RESTART_CMD in _systemctl_log(tmp_path)


def test_reconcile_keeps_marker_when_managed_xvf_needs_firmware(tmp_path: Path) -> None:
    _write_env(tmp_path, "udp:9876")
    _write_mode(tmp_path)
    _write_card(tmp_path, channels=2)
    _marker(tmp_path).write_text("reason=stale\n")

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    assert _marker(tmp_path).exists(), result.stderr
    assert "supported 6-channel firmware" in _marker(tmp_path).read_text()
    assert VOICE_RESTART_CMD not in _systemctl_log(tmp_path)


def test_reconcile_clears_marker_for_custom_mic(tmp_path: Path) -> None:
    # Custom JASPER_MIC_DEVICE: the reconciler leaves voice config alone and
    # must NOT gate the operator's device — clear any stale marker so voice
    # can start and try it (the daemon's exit-66 park is the safety net).
    _write_env(tmp_path, "hw:9,0")  # not an owned value
    _write_mode(tmp_path)
    _marker(tmp_path).write_text("reason=stale\n")

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    assert "leaving voice config untouched" in result.stderr
    assert not _marker(tmp_path).exists(), result.stderr


def test_reconcile_check_only_does_not_touch_marker(tmp_path: Path) -> None:
    # --check-aec-ready is the bridge's ExecCondition: a pure read, it must
    # never create or remove the marker.
    _write_env(tmp_path, "udp:9876")
    _write_mode(tmp_path)
    _marker(tmp_path).write_text("reason=preexisting\n")

    result = _run_reconcile(tmp_path, "--check-aec-ready")

    # No card -> not aec-ready -> exit 1, but the marker is untouched.
    assert result.returncode == 1
    assert _marker(tmp_path).read_text() == "reason=preexisting\n"
