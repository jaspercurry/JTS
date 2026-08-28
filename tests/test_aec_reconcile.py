# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import re
import subprocess
import sys
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
            # jasper-aec-init's disclosure hand-off. Absent by default, so
            # every pass reads "the alignment is fully ready" unless a test
            # writes one; never the host's real /run copy.
            "JASPER_AEC_ALIGNMENT_DISCLOSURE_FILE": str(
                tmp_path / "alignment-disclosure"
            ),
            # Same reason: accessory_mic_present shells to
            # `jasper.accessories.mic_env`, which reads the REAL
            # /var/lib/jasper/accessory-mics.env unless redirected. Point it at
            # a tmp path that does not exist by default, so every test starts
            # from "no accessory microphone" regardless of the host.
            "JASPER_ACCESSORY_MIC_ENV_FILE": str(
                tmp_path / "accessory-mics.env"
            ),
            # Same reason again: the voice-restart change gate reads the
            # install manifest and stamps /run. Neither may touch the real
            # host, and both start ABSENT so every pass that does not opt in
            # keeps the unconditional restart (an unprovable build is a
            # restart — see installed_build_matches_stamp).
            "JASPER_INSTALL_MANIFEST": str(tmp_path / "build.txt"),
            "JASPER_VOICE_RESTART_STAMP": str(
                tmp_path / "run" / "voice-restart.stamp"
            ),
            "JASPER_VOICE_RESTART_INTENT_MARKER": str(
                tmp_path / "run" / "voice-restart-intent"
            ),
            "JASPER_SYSTEMCTL": str(fake_systemctl),
            "JASPER_SYSTEMCTL_LOG": str(systemctl_log),
            # The interpreter the script's Python bridges run under. Pin it to
            # the one running the tests instead of whatever `python3` PATH
            # happens to offer: on the Pi this is /opt/jasper/.venv/bin/python
            # (the whole dependency set), while a bare system python3 can lack
            # numpy and silently fail the measurement-registry bridge into its
            # fail-open branch. Per-test overrides still win.
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
    resolver — the shape the runtime-env carry exists for.
    """
    resolver = tmp_path / "synthetic-xvf-resolver"
    resolver.write_text(
        "#!/usr/bin/env bash\n"
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
    env_file = _write_env(tmp_path, "Array")
    _write_profile_mode(tmp_path, selection)
    _write_card(tmp_path, channels=6)

    assert _run_reconcile(tmp_path, "--reason", "test").returncode == 0

    body = env_file.read_text()
    assert f"JASPER_AEC_CHIP_AEC_ALIGNMENT_SELECTION={selection}" in body
    assert "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS=" in body


def test_a_custom_pass_neither_writes_nor_clears_an_inherited_record(
    tmp_path: Path,
) -> None:
    """The property the reading rule rests on, not just half of it.

    A custom profile writing nothing is only the first half: it does not clear
    or rewrite the record it inherits either, which is exactly how a leftover
    outlives the selection that produced it — and why the stamp, rather than
    the record's presence, has to be what tells the two apart.
    """
    seeded = (
        "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS=disclosed_stale\n"
        "JASPER_AEC_CHIP_AEC_ALIGNMENT_REASON='output DAC has no codified timing'\n"
        "JASPER_AEC_CHIP_AEC_ALIGNMENT_ACTION='Run sudo jasper-aec-commission'\n"
        "JASPER_AEC_CHIP_AEC_ALIGNMENT_SELECTION=xvf_chip_aec\n"
    )
    env_file = _write_env(tmp_path, "Array", extra=seeded)
    _write_profile_mode(tmp_path, "custom")
    _write_card(tmp_path, channels=6)

    assert _run_reconcile(tmp_path, "--reason", "test").returncode == 0

    surviving = [
        line
        for line in env_file.read_text().splitlines()
        if line.startswith("JASPER_AEC_CHIP_AEC_ALIGNMENT_")
    ]
    assert surviving == seeded.splitlines()


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


def _init_exit_systemctl(tmp_path: Path, status: int, *, bridge: str = "active") -> Path:
    """A systemctl double whose jasper-aec-init restart exits `status`.

    `bridge` picks how the AEC bridge behaves afterwards: it comes up
    (`active`), its restart fails outright (`restart_fails`), or its restart
    reports success because the unit's ExecCondition SKIPPED it (`skipped`) —
    the case where a bare exit-status check would certify a bridge that is not
    running.
    """
    assert bridge in {"active", "restart_fails", "skipped"}
    fake = tmp_path / f"init-exit-{status}-{bridge}-systemctl"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$JASPER_SYSTEMCTL_LOG\"\n"
        "[[ \"$*\" == 'restart jasper-aec-init.service' ]] && exit 1\n"
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
        )
        + f"[[ \"$1\" == 'show' ]] && {{ printf '{status}\\n'; exit 0; }}\n"
        "exit 0\n"
    )
    fake.chmod(0o755)
    return fake


@pytest.mark.parametrize(
    "status, action",
    [
        (
            aec_init.COMMISSION_REQUIRED_EXIT,
            "JASPER_AEC_CHIP_AEC_ALIGNMENT_ACTION='Run sudo jasper-aec-commission'",
        ),
        # The ordering race is not a moved artifact, so it must NOT send the
        # household to the two-minute commissioner.
        (
            aec_init.OUTPUTD_ENV_STALE_EXIT,
            "JASPER_AEC_CHIP_AEC_ALIGNMENT_ACTION='Wait for jasper-outputd to "
            "restart, then run the reconciler'",
        ),
    ],
)
def test_an_unappliable_alignment_runs_software_aec3_and_discloses(
    tmp_path: Path, status: int, action: str
) -> None:
    # ADR-0101: neither exit code says anything observably broke, so the box
    # keeps hearing on the software AEC3 leg and carries the reason/action to
    # the doctor and /state instead of going silently deaf.
    env_file = _write_env(tmp_path, "Array")
    _write_mode(tmp_path)
    _write_card(tmp_path, channels=6)
    fake = _init_exit_systemctl(tmp_path, status)

    result = _run_reconcile(
        tmp_path, "--reason", "test", extra_env={"JASPER_SYSTEMCTL": str(fake)}
    )

    assert result.returncode == 0, result.stderr
    body = env_file.read_text()
    assert "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS=disclosed_stale" in body
    assert action in body
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


@pytest.mark.parametrize("bridge", ["restart_fails", "skipped"])
def test_a_disclosed_box_takes_the_direct_mic_when_the_bridge_is_not_active(
    tmp_path: Path, bridge: str
) -> None:
    # Nothing writes udp:9876 without the bridge, and jasper-voice bound to an
    # unfed socket stalls into WatchdogSec=30s and the unit's
    # StartLimitAction=reboot. Both shapes must reach the direct mic: a failed
    # restart, and a restart that exits 0 because the ExecCondition SKIPPED the
    # unit — `systemctl restart` cannot tell those apart, so the unit is asked.
    env_file = _write_env(tmp_path, "Array")
    _write_mode(tmp_path)
    _write_card(tmp_path, channels=6)
    fake = _init_exit_systemctl(
        tmp_path, aec_init.COMMISSION_REQUIRED_EXIT, bridge=bridge
    )

    result = _run_reconcile(
        tmp_path, "--reason", "test", extra_env={"JASPER_SYSTEMCTL": str(fake)}
    )

    assert result.returncode == 0, result.stderr
    body = env_file.read_text()
    assert "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS=disclosed_stale" in body
    assert "JASPER_MIC_DEVICE=Array" in body
    assert not _marker(tmp_path).exists()
    commands = _systemctl_log(tmp_path)
    assert VOICE_RESTART_CMD in commands
    # An enabled+failed bridge would Restart=on-failure every 2 s into its own
    # StartLimitAction=reboot, and a transient success would grab the
    # single-open XVF capture device jasper-voice now holds. Stop AND disable.
    lines = commands.splitlines()
    stopped = _unit_command_indices(lines, "stop", "jasper-aec-bridge.service")
    disabled = _unit_command_indices(lines, "disable", "jasper-aec-bridge.service")
    assert stopped and disabled
    assert max(disabled) < lines.index(VOICE_RESTART_CMD)
    # The UDP legs go with the bridge: a stale JASPER_MIC_DEVICE_RAW=udp: leaves
    # voice's secondary capture spinning on a port nobody writes.
    assert "JASPER_MIC_DEVICE_RAW=udp:" not in body


@pytest.mark.parametrize("bridge", ["restart_fails", "skipped"])
def test_a_disclosed_fallback_hands_the_output_owner_one_settled_bounce(
    tmp_path: Path, bridge: str
) -> None:
    # The fallback used to arm the software-AEC3 legs, bounce jasper-outputd,
    # find the stack down, clear the legs and bounce it again — two outages of
    # the output owner in one pass, the second undoing the first. The verdict
    # now precedes the publication: outputd is restarted once, after the leg
    # vector the pass settles on is already on disk, so it can never load the
    # attempt's vector.
    _write_env(tmp_path, "Array")
    _write_mode(tmp_path)
    _write_card(tmp_path, channels=6)
    fake = _init_exit_systemctl(
        tmp_path, aec_init.COMMISSION_REQUIRED_EXIT, bridge=bridge
    )

    result = _run_reconcile(
        tmp_path, "--reason", "test", extra_env={"JASPER_SYSTEMCTL": str(fake)}
    )

    assert result.returncode == 0, result.stderr
    lines = _systemctl_log(tmp_path).splitlines()
    # Reading aec-init's exit status is the hand-off into the disclose path;
    # the bounce before it armed the chip-reference producer aec-init samples.
    handover = lines.index("show -p ExecMainStatus --value jasper-aec-init.service")
    disclosed = lines[handover:]
    bounces = _unit_command_indices(disclosed, "restart", "jasper-outputd.service")
    assert len(bounces) == 1
    # And it lands after the teardown that writes the settled legs, so no
    # contradictory vector is observable between the write and the restart.
    disabled = _unit_command_indices(disclosed, "disable", "jasper-aec-bridge.service")
    assert disabled and max(disabled) < bounces[0]


def test_reconcile_discloses_an_applied_alignment_its_proof_no_longer_matches(
    tmp_path: Path,
) -> None:
    # jasper-aec-init armed the chip from the banked K and left its one-line
    # reason behind; the reconciler publishes that verbatim as `disclosed_stale`
    # with the commissioner as the action, and the stack stays up.
    env_file = _write_env(tmp_path, "Array")
    _write_mode(tmp_path)
    _write_card(tmp_path, channels=6)
    disclosure = tmp_path / "alignment-disclosure"
    disclosure.write_text("commissioned alignment was measured on a different unit\n")

    result = _run_reconcile(
        tmp_path,
        "--reason",
        "test",
        extra_env={"JASPER_AEC_ALIGNMENT_DISCLOSURE_FILE": str(disclosure)},
    )

    assert result.returncode == 0, result.stderr
    body = env_file.read_text()
    assert "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS=disclosed_stale" in body
    assert (
        "JASPER_AEC_CHIP_AEC_ALIGNMENT_REASON='commissioned alignment was "
        "measured on a different unit'"
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
    env_file = _write_env(tmp_path, "Array")
    _write_mode(tmp_path)
    _write_card(tmp_path, channels=6)
    disclosure = tmp_path / "alignment-disclosure"
    disclosure.write_text("this box's proof moved\n")

    result = _run_reconcile(
        tmp_path,
        "--reason",
        "test",
        extra_env={"JASPER_AEC_ALIGNMENT_DISCLOSURE_FILE": str(disclosure)},
    )

    assert result.returncode == 0, result.stderr
    reason = _env_assignments(env_file)["JASPER_AEC_CHIP_AEC_ALIGNMENT_REASON"]
    assert reason == "'this boxs proof moved'"
    # And the file still round-trips through the reader every daemon uses.
    assert "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS=disclosed_stale" in (
        env_file.read_text()
    )


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


def test_reconcile_captures_directly_when_managed_xvf_is_not_6_channel(
    tmp_path: Path,
) -> None:
    """#2984 Q2: a 2-channel XVF is a usable plain microphone.

    Echo cancellation needs the 6-channel endpoint, so the box says so and
    keeps hearing on the chip's direct capture instead of going deaf.
    """
    env_file = _write_env(tmp_path, "udp:9876")
    _write_mode(tmp_path)
    _write_card(tmp_path, channels=2)
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
    unfed UDP socket and watchdog-restarts into StartLimitAction=reboot.

    Reached here through the kept park — no eligible capture device at all, not
    a firmware or DAC disposition, which now disclose and keep hearing.
    """
    _write_env(tmp_path, "udp:9876")
    _write_profile_mode(tmp_path, "xvf_chip_aec")
    _write_accessory_mics(
        tmp_path, f"JASPER_MANUAL_MIC_SOURCES=wiim_remote_2={WIIM_REMOTE_2_MIC_DEVICE}\n",
    )

    result = _run_reconcile(
        tmp_path,
        "--reason",
        "test",
        extra_env={"JASPER_MIC_PROFILE_PYTHON": str(tmp_path / "missing-python")},
    )

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


@pytest.mark.parametrize(
    "status, expected",
    [
        ("ready", 0),
        ("disclosed_stale", 0),
        ("checking", 1),
        ("unavailable", 1),
        ("fault", 1),
        ("", 1),
    ],
)
def test_the_bridge_execcondition_admits_every_running_alignment(
    tmp_path: Path, status: str, expected: int
) -> None:
    # ADR-0101: `disclosed_stale` is a box that IS hearing — on the banked chip
    # alignment or on software AEC3 — and the bridge carries its audio either
    # way, so it must start. Only a refusal, a fault, or the mid-bounce
    # `checking` keeps it out.
    _write_env(
        tmp_path,
        "Array",
        extra=f"JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS={status}\n",
    )
    _write_profile_mode(tmp_path, "auto")
    _write_card(tmp_path, channels=6)

    assert _run_reconcile(tmp_path, "--check-aec-ready").returncode == expected


@pytest.mark.parametrize(("channels", "expected"), [(6, 0), (2, 1)])
def test_the_bridge_execcondition_admits_the_managed_aec3_fallback(
    tmp_path: Path, channels: int, expected: int
) -> None:
    # A managed XVF whose chip leg is unavailable still needs the bridge when
    # the mic can carry software AEC3 — that leg IS the wake path there.
    # Below the 6-channel endpoint there is nothing for the bridge to read.
    _write_env(
        tmp_path,
        "Array",
        extra=(
            "JASPER_AUDIO_DAC_ID=mystery_usb_audio\n"
            "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS=disclosed_stale\n"
        ),
    )
    _write_profile_mode(tmp_path, "auto")
    _write_card(tmp_path, channels=channels)

    assert _run_reconcile(tmp_path, "--check-aec-ready").returncode == expected


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
    assert "JASPER_MIC_DEVICE_CHIP_AEC_150=udp:" not in body
    # Disarmed, not deafened: the 6-channel mic still carries software AEC3.
    assert "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS=disclosed_stale" in body
    assert not _marker(tmp_path).exists()


def test_unevaluable_dac_gate_carries_the_last_resolved_verdict(
    tmp_path: Path,
) -> None:
    """ADR-0101: an unmeasured gate is not a "no".

    A resolver that cannot answer must not knock a commissioned box off
    chip-AEC; the verdict it last resolved for this same DAC stands, and the
    disclosure says it is carried.
    """
    env_file = _write_env(tmp_path, "Array", extra="JASPER_AUDIO_DAC_ID=apple_usb_c_dongle\n")
    _write_profile_mode(tmp_path, "auto")
    _write_card(tmp_path, channels=6)

    first = _run_reconcile(tmp_path, "--reason", "test")
    assert first.returncode == 0, first.stderr
    assert "JASPER_AEC_CHIP_AEC_DAC_STATUS=approved" in env_file.read_text()

    # Mic profile still resolves; only the DAC policy query fails.
    broken_gate = _write_synthetic_xvf_resolver(
        tmp_path,
        "Array",
        chip_beam_plan="xvf_square_fixed_150_210",
        chip_aec_supported="1",
        policy_exit=1,
    )
    second = _run_reconcile(
        tmp_path,
        "--reason",
        "test",
        extra_env={"JASPER_MIC_PROFILE_PYTHON": str(broken_gate)},
    )

    assert second.returncode == 0, second.stderr
    body = env_file.read_text()
    assert "JASPER_AEC_CHIP_AEC_DAC_STATUS=approved" in body
    assert "JASPER_AEC_CHIP_AEC_DAC_SOURCE=runtime_env_carried" in body
    assert "carrying last verdict" in body
    # The carried verdict is what keeps the chip leg armed.
    assert "JASPER_AEC_CHIP_AEC_ENABLED=1" in body
    assert "JASPER_MIC_DEVICE=udp:9876" in body
    assert not _marker(tmp_path).exists()


@pytest.mark.parametrize(
    "selection", ["auto", "xvf_chip_aec", "xvf_chip_aec_testing"]
)
def test_a_carried_verdict_answers_whichever_selection_asks(
    tmp_path: Path, selection: str
) -> None:
    """The record holds both selections' answers, so neither is refused.

    The managed path always queries the production gate, even under the
    testing alias — a record keyed to one selection alone would leave the
    other with no verdict to carry and drop the box on a resolver outage.
    """
    env_file = _write_env(
        tmp_path, "Array", extra="JASPER_AUDIO_DAC_ID=apple_usb_c_dongle\n"
    )
    _write_profile_mode(tmp_path, selection)
    _write_card(tmp_path, channels=6)

    assert _run_reconcile(tmp_path, "--reason", "test").returncode == 0
    broken_gate = _write_synthetic_xvf_resolver(
        tmp_path,
        "Array",
        chip_beam_plan="xvf_square_fixed_150_210",
        chip_aec_supported="1",
        policy_exit=1,
    )

    result = _run_reconcile(
        tmp_path,
        "--reason",
        "test",
        extra_env={"JASPER_MIC_PROFILE_PYTHON": str(broken_gate)},
    )

    assert result.returncode == 0, result.stderr
    body = env_file.read_text()
    assert "carrying last verdict" in body
    assert "JASPER_AEC_CHIP_AEC_ENABLED=1" in body
    assert "JASPER_MIC_DEVICE=udp:9876" in body


def test_a_status_only_record_still_carries(
    tmp_path: Path,
) -> None:
    """The status IS the verdict: `approved` is what the automatic profile arms on.

    Reading a carried record as not-permitted would be the exact drop the carry
    exists to prevent, and would then persist that contradiction.
    """
    env_file = _write_env(
        tmp_path,
        "Array",
        extra=(
            "JASPER_AUDIO_DAC_ID=apple_usb_c_dongle\n"
            "JASPER_AEC_CHIP_AEC_DAC_ID=apple_usb_c_dongle\n"
            "JASPER_AEC_CHIP_AEC_DAC_STATUS=approved\n"
            "JASPER_AEC_CHIP_AEC_DAC_SOURCE=static\n"
            "JASPER_AEC_CHIP_AEC_DAC_DETAIL='approved for production chip-AEC'\n"
        ),
    )
    _write_profile_mode(tmp_path, "auto")
    _write_card(tmp_path, channels=6)
    broken_gate = _write_synthetic_xvf_resolver(
        tmp_path,
        "Array",
        chip_beam_plan="xvf_square_fixed_150_210",
        chip_aec_supported="1",
        policy_exit=1,
    )

    result = _run_reconcile(
        tmp_path,
        "--reason",
        "test",
        extra_env={"JASPER_MIC_PROFILE_PYTHON": str(broken_gate)},
    )

    assert result.returncode == 0, result.stderr
    body = env_file.read_text()
    assert "JASPER_AEC_CHIP_AEC_DAC_STATUS=approved" in body
    assert "JASPER_AEC_CHIP_AEC_DAC_SOURCE=runtime_env_carried" in body
    assert "JASPER_AEC_CHIP_AEC_ENABLED=1" in body


@pytest.mark.parametrize(
    ("dac_id", "stderr_phrase"),
    [
        ("hifiberry_dac8x_studio", "HiFiBerry DAC8x Studio needs per-profile"),
        ("mystery_usb_audio", "has no codified chip-AEC calibration"),
    ],
)
def test_auto_profile_discloses_and_runs_aec3_when_output_dac_needs_calibration(
    tmp_path: Path,
    dac_id: str,
    stderr_phrase: str,
) -> None:
    """ADR-0101: the DAC gate is a quality signal, not an admission gate.

    An uncodified output DAC keeps chip-AEC unselected, but the 6-channel mic
    still carries software AEC3 and the box discloses what it lost instead of
    parking the voice stack deaf.
    """
    _write_env(tmp_path, "Array", extra=f"JASPER_AUDIO_DAC_ID={dac_id}\n")
    _write_card(tmp_path, channels=6)

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    assert stderr_phrase in result.stderr
    body = (tmp_path / "jasper.env").read_text()
    # The software-AEC3 leg shape, on the bridge's own UDP carrier.
    assert "JASPER_AEC_CHIP_AEC_ENABLED=0" in body
    assert "JASPER_MIC_DEVICE_RAW=udp:9877" in body
    assert "JASPER_MIC_DEVICE_CHIP_AEC_150=udp:" not in body
    assert "JASPER_MIC_DEVICE=udp:9876" in body
    assert "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS=disclosed_stale" in body
    assert "JASPER_AEC_CHIP_AEC_ALIGNMENT_ACTION='Run sudo jasper-aec-commission'" in body
    assert not _marker(tmp_path).exists()
    assert VOICE_RESTART_CMD in _systemctl_log(tmp_path)


def test_explicit_chip_profile_falls_back_for_uncalibrated_output_dac(
    tmp_path: Path,
) -> None:
    """A managed profile never selects chip-AEC on uncodified output timing.

    The selection is refused; the box is not. It runs software AEC3 and carries
    the DAC's own detail as the disclosure.
    """
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
    assert "JASPER_AEC_CHIP_AEC_ENABLED=0" in body
    assert "JASPER_MIC_DEVICE_RAW=udp:9877" in body
    assert "JASPER_MIC_DEVICE=udp:9876" in body
    assert "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS=disclosed_stale" in body


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
    assert "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS=disclosed_stale" in body


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
    # No production beam plan, so no chip beams — but the mic is 6-channel, so
    # software AEC3 carries the wake path and the reason reaches the doctor.
    assert "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS=disclosed_stale" in body
    assert "future XVF needs a validated beam plan" in body
    assert "JASPER_AEC_CHIP_AEC_ENABLED=0" in body
    assert "JASPER_MIC_DEVICE_RAW=udp:9877" in body
    assert "JASPER_MIC_DEVICE=udp:9876" in body
    assert "future XVF needs a validated beam plan" in result.stderr
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


def test_custom_chip_leg_is_honoured_on_an_unapproved_dac(
    tmp_path: Path,
) -> None:
    """ADR-0101: the operator's explicit leg survives an uncodified DAC.

    The gate is a quality signal here, so the leg stands and the log line is
    the disclosure. The DAC's own verdict still reaches the status surfaces.
    """
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
    assert "JASPER_AEC_CHIP_AEC_ENABLED=1" in body
    assert "JASPER_MIC_DEVICE_RAW=udp:" not in body
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
    assert "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS=disclosed_stale" in body
    assert "JASPER_MIC_DEVICE_CHIP_AEC_150=udp:" not in body


def test_explicit_chip_profile_does_not_arm_for_compensable_verdict(
    tmp_path: Path,
) -> None:
    """A measured but drifting DAC needs the deferred rate-match layer, so
    `compensable` stays unarmed pending a supported product path — on software
    AEC3, not parked."""
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
    assert "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS=disclosed_stale" in body
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
                "ref_source": "outputd_udp",
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
    assert "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS=disclosed_stale" in body
    assert "JASPER_XVF_VARIANT=xvf3800_flex_linear_6ch" in body
    assert "JASPER_XVF_GEOMETRY=linear" in body
    assert "JASPER_XVF_CHIP_AEC_SUPPORTED=0" in body
    assert "JASPER_AEC_CHIP_AEC_ENABLED=0" in body
    assert "JASPER_MIC_DEVICE_RAW=udp:9877" in body
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
    assert "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS=disclosed_stale" in body
    assert "JASPER_XVF_VARIANT=xvf3800_flex_linear_6ch" in body
    assert "JASPER_XVF_CHIP_AEC_SUPPORTED=0" in body
    assert "JASPER_AEC_CHIP_AEC_ENABLED=0" in body
    assert "JASPER_MIC_DEVICE_RAW=udp:9877" in body


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
    assert "JASPER_AEC_REF_SOURCE=outputd_udp" in body
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


# --- measurement-class mic identity + hotplug change-gating (W1) -----------
#
# Two coupled behaviours, both driven by the same udev rule
# (deploy/udev/99-jasper-aec-reconcile.rules fires on EVERY sound-card
# add|remove, deliberately id-agnostic — the policy lives in the reconciler):
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
    suppressed-but-needed restart into a red test. The round-1 review proved
    the cost: the multiroom bond/unbond hole sat green behind exactly this
    (the transition test's fixture wrote no manifest, so the gate its
    scenario would have tripped never engaged). Single-pass tests are
    unaffected (their first pass has no stamp and restarts regardless);
    multi-pass transition tests now run gate-armed by construction. The
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
    _write_env(tmp_path, "Array")
    _write_mode(tmp_path)
    _write_card(tmp_path, channels=6)
    _write_manifest(tmp_path)
    if alignment == "disclosed_stale":
        (tmp_path / "alignment-disclosure").write_text(
            "commissioned alignment was measured on a different unit (xvf_serial)\n"
        )
    first = _run_reconcile(tmp_path, "--reason", "install")
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


def test_a_software_fallback_disclosure_keeps_bouncing_so_the_race_can_heal(
    tmp_path: Path,
) -> None:
    # The other disclosed sub-state: chip NOT armed because aec-init could not
    # apply the alignment. That one must keep re-running the sequence, or the
    # outputd ordering race (exit 3) it came from could never resolve.
    _write_env(
        tmp_path,
        "Array",
        extra=(
            "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS=disclosed_stale\n"
            "JASPER_AEC_CHIP_AEC_ENABLED=0\n"
        ),
    )
    _write_mode(tmp_path)
    _write_card(tmp_path, channels=6)
    _write_manifest(tmp_path)

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
    _write_env(tmp_path, "Array", extra="JASPER_AUDIO_DAC_ID=mystery_usb_audio\n")
    _write_mode(tmp_path)
    _write_card(tmp_path, channels=6)
    _write_manifest(tmp_path)

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
    """The bug this PR exists for: plugging a UMIK-2 in to take a room
    measurement fires the id-agnostic sound-card udev rule, and every
    mic-bearing branch used to restart jasper-voice unconditionally — ~8 s of
    deafness measured on jts3, up to ~55 s worst case."""
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
    _write_env(tmp_path, "Array")
    _write_profile_mode(tmp_path, "custom")
    _write_card(tmp_path, channels=6)
    _write_manifest(tmp_path)
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


def test_a_new_installed_build_still_restarts_voice(tmp_path: Path) -> None:
    """A deploy changes no env value on a settled box, and
    scripts/deploy-to-pi.sh deliberately does not restart jasper-voice itself
    ("Voice is mic-hardware-dependent") — this reconciler is what rolls new
    Python into the daemon. A gate blind to the build would leave voice on the
    previous build indefinitely."""
    _armed_chip_aec_box(tmp_path)
    _write_manifest(tmp_path, sha="def5678")

    result = _run_reconcile(tmp_path, "--reason", "systemd")

    assert result.returncode == 0, result.stderr
    assert VOICE_RESTART_CMD in _systemctl_log(tmp_path)


def test_a_missing_install_manifest_still_restarts_voice(tmp_path: Path) -> None:
    """"I cannot prove the code is unchanged" must resolve to restarting."""
    _armed_chip_aec_box(tmp_path)
    (tmp_path / "build.txt").unlink()

    result = _run_reconcile(tmp_path, "--reason", "systemd")

    assert result.returncode == 0, result.stderr
    assert VOICE_RESTART_CMD in _systemctl_log(tmp_path)


def test_a_stopped_voice_daemon_still_restarts(tmp_path: Path) -> None:
    """Nothing changed, but jasper-voice is not running. Skipping here would
    leave the speaker deaf until the next hardware event — the exact failure
    the gate must never produce."""
    _armed_chip_aec_box(tmp_path)
    # A systemctl double that reports jasper-voice as inactive. Its own name,
    # because _run_reconcile rewrites tmp_path/systemctl on every call.
    inactive_voice = tmp_path / "systemctl-inactive-voice"
    inactive_voice.write_text(
        "#!/usr/bin/env bash\n"
        'printf \'%s\\n\' "$*" >> "$JASPER_SYSTEMCTL_LOG"\n'
        'if [[ "$1" == "is-active" && "$*" == *"jasper-voice.service"* ]]; then\n'
        "  exit 3\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    inactive_voice.chmod(0o755)

    result = _run_reconcile(
        tmp_path,
        "--reason",
        "systemd",
        extra_env={"JASPER_SYSTEMCTL": str(inactive_voice)},
    )

    assert result.returncode == 0, result.stderr
    assert VOICE_RESTART_CMD in _systemctl_log(tmp_path)


def test_a_downed_aec_bridge_forces_the_voice_restart_with_it(
    tmp_path: Path,
) -> None:
    """The stack bounce and the voice restart are gated TOGETHER. When the
    bridge is down, enable_start_aec rebuilds it — and that pass must also
    restart voice even though no env value changed, so the daemon and its UDP
    carrier always come from the same pass (enable_start_aec's own
    VOICE_RESTART_NEEDED=1). Skipping voice while the bridge bounces is the
    split the gate must never produce."""
    _write_env(tmp_path, "Array")
    _write_profile_mode(tmp_path, "custom")
    _write_card(tmp_path, channels=6)
    _write_manifest(tmp_path)
    first = _run_reconcile(tmp_path, "--reason", "install")
    assert first.returncode == 0, first.stderr
    _clear_systemctl_log(tmp_path)
    # A systemctl double where the bridge is down but voice is up: the stack
    # skip cannot engage, so the pass rebuilds the bridge.
    downed_bridge = tmp_path / "systemctl-downed-bridge"
    downed_bridge.write_text(
        "#!/usr/bin/env bash\n"
        'printf \'%s\\n\' "$*" >> "$JASPER_SYSTEMCTL_LOG"\n'
        'if [[ "$1" == "is-active" && "$*" == *"jasper-aec-bridge.service"* ]]; then\n'
        "  exit 3\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    downed_bridge.chmod(0o755)

    result = _run_reconcile(
        tmp_path,
        "--reason",
        "systemd",
        extra_env={"JASPER_SYSTEMCTL": str(downed_bridge)},
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


def test_a_present_park_marker_still_restarts_voice(tmp_path: Path) -> None:
    """A pass that would clear the park marker is UN-parking voice; bringing
    it back is the whole point."""
    _armed_chip_aec_box(tmp_path)
    _marker(tmp_path).write_text("reason=stale\n")

    result = _run_reconcile(tmp_path, "--reason", "systemd")

    assert result.returncode == 0, result.stderr
    assert VOICE_RESTART_CMD in _systemctl_log(tmp_path)
    assert not _marker(tmp_path).exists()


def test_an_invalidated_voice_provider_still_reaches_the_park_branch(
    tmp_path: Path,
) -> None:
    """voice_provider.env is not in jasper.env, so the env change test cannot
    see a provider that went away. restart_voice's park branch has to run."""
    _armed_chip_aec_box(tmp_path)
    (tmp_path / "voice_provider.env").write_text("JASPER_VOICE_PROVIDER=\n")

    result = _run_reconcile(tmp_path, "--reason", "systemd")

    assert result.returncode == 0, result.stderr
    assert "disable --now jasper-voice.service" in _systemctl_log(tmp_path)


def test_a_newly_paired_accessory_mic_still_restarts_voice(tmp_path: Path) -> None:
    """The one caller that hands the restart back to this reconciler WITHOUT
    stopping voice first.

    jasper.accessories.reconcile.refresh_voice_input starts this unit so "a
    live push-to-talk session picks up the new source". The published sources
    live outside jasper.env, so the env change test cannot see them — they are
    part of the stamp instead. Skipping here would leave a freshly-paired
    remote dead until the next hardware event.

    The value is the accessory owner's real publish shape (source_id=device):
    the probe must RESOLVE it, because a malformed value fails the probe and
    restarts through the fail-open branch instead — which would leave the
    sources-in-the-stamp promise untested (the unreadable-probe case below
    owns the fail-open branch).
    """
    _armed_chip_aec_box(tmp_path)
    (tmp_path / "accessory-mics.env").write_text(
        f"JASPER_MANUAL_MIC_SOURCES=wiim_remote_2={WIIM_REMOTE_2_MIC_DEVICE}\n"
    )

    result = _run_reconcile(tmp_path, "--reason", "systemd")

    assert result.returncode == 0, result.stderr
    # The probe must have resolved the value: a parse failure would restart
    # through the fail-open branch and prove nothing about the stamp.
    assert "accessory mic probe failed" not in result.stderr
    assert VOICE_RESTART_CMD in _systemctl_log(tmp_path)


def test_an_unreadable_accessory_probe_still_restarts_voice(
    tmp_path: Path,
) -> None:
    """"I could not tell" is not "nothing is paired". An accessory probe that
    cannot answer must not be allowed to look like an unchanged input."""
    _armed_chip_aec_box(tmp_path)
    broken = tmp_path / "broken-accessory-python"
    broken.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$*" == *"jasper.accessories.mic_env"* ]]; then\n'
        "  exit 1\n"
        "fi\n"
        f'exec "{sys.executable}" "$@"\n',
        encoding="utf-8",
    )
    broken.chmod(0o755)

    result = _run_reconcile(
        tmp_path,
        "--reason",
        "systemd",
        extra_env={"JASPER_MIC_PROFILE_PYTHON": str(broken)},
    )

    assert result.returncode == 0, result.stderr
    assert VOICE_RESTART_CMD in _systemctl_log(tmp_path)


def test_a_bond_and_an_unbond_both_restart_the_leaders_voice(
    tmp_path: Path,
) -> None:
    """The other owner-published fact voice starts from: grouping-voice.env.

    jasper.multiroom.reconcile (step 3b) rewrites it on bond/unbond — the
    leader's TTS socket flip — and kicks this reconciler to do the restart,
    without stopping voice and without touching jasper.env. For a non-parked
    leader the file's CONTENT is the only visible change, so it is part of
    the stamp; a gate blind to it leaves the leader on the wrong TTS route
    until the next unrelated hardware event (round-1 blocker)."""
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


def test_a_check_only_pass_leaves_the_intent_marker(tmp_path: Path) -> None:
    """--check-aec-ready cannot act on intent; consuming it there would eat
    the restart the marker was left to cause."""
    _armed_chip_aec_box(tmp_path)
    marker = tmp_path / "run" / "voice-restart-intent"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("enhanced_aec_v2_activation\n")

    _run_reconcile(tmp_path, "--check-aec-ready")

    assert marker.exists()


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
    """The change test compares against the PASS-START env file, never
    against shell variables the profile resolver's eval already overwrote —
    a resolved value compared with itself can never trip (the round-1
    tautology across every JASPER_XVF_* write). Model: the stored XVF facts
    went stale relative to the hardware; the resolver re-derives the truth
    and that write must count as a voice-relevant change."""
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


def test_a_disabled_voice_unit_still_restarts_and_reenables(
    tmp_path: Path,
) -> None:
    """Enabled-ness is part of "already running as configured": a
    disabled-but-active voice evaporates on the next boot, so the gate
    refuses to skip and restart_voice's enable repairs the unit."""
    _armed_chip_aec_box(tmp_path)
    disabled_voice = tmp_path / "systemctl-disabled-voice"
    disabled_voice.write_text(
        "#!/usr/bin/env bash\n"
        'printf \'%s\\n\' "$*" >> "$JASPER_SYSTEMCTL_LOG"\n'
        'if [[ "$1" == "is-enabled" && "$*" == *"jasper-voice.service"* ]]; then\n'
        "  exit 1\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    disabled_voice.chmod(0o755)

    result = _run_reconcile(
        tmp_path,
        "--reason",
        "systemd",
        extra_env={"JASPER_SYSTEMCTL": str(disabled_voice)},
    )

    assert result.returncode == 0, result.stderr
    commands = _systemctl_log(tmp_path)
    assert VOICE_RESTART_CMD in commands
    assert "enable jasper-voice.service" in commands


def _armed_direct_mic_box(tmp_path: Path) -> None:
    """A settled non-XVF direct-mic speaker (AEC disabled), one pass run so
    the env file and the /run stamp describe the running state."""
    _write_env(tmp_path, "UsbMic", extra="JASPER_MIC_DEVICE_CANDIDATES=UsbMic\n")
    _write_mode(tmp_path, "disabled")
    _write_card(tmp_path, card="UsbMic", channels=2)
    _write_manifest(tmp_path)
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
    downed_bridge = tmp_path / "systemctl-downed-bridge"
    downed_bridge.write_text(
        "#!/usr/bin/env bash\n"
        'printf \'%s\\n\' "$*" >> "$JASPER_SYSTEMCTL_LOG"\n'
        'if [[ "$1" == "is-active" && "$*" == *"jasper-aec-bridge.service"* ]]; then\n'
        "  exit 3\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    downed_bridge.chmod(0o755)

    result = _run_reconcile(
        tmp_path,
        "--reason",
        "systemd",
        extra_env={"JASPER_SYSTEMCTL": str(downed_bridge)},
    )

    assert result.returncode == 0, result.stderr
    assert VOICE_RESTART_CMD not in _systemctl_log(tmp_path)
    assert "event=aec_reconcile.voice_restart_skipped" in result.stderr


def test_a_six_channel_measurement_card_never_arms_the_aec_stack(
    tmp_path: Path,
) -> None:
    """aec_ready gates on channel count; a hypothetical 6-channel
    measurement card must not pass it into the software-AEC stack, and the
    all-measurement fallback name must not hand the instrument to any later
    consumer either (it seeds JASPER_MIC_DEVICE, which an accessory-cleared
    park gate would let jasper-voice open)."""
    _write_env(tmp_path, "udp:9876", extra="JASPER_MIC_DEVICE_CANDIDATES=UMIK2\n")
    _write_mode(tmp_path)
    _write_usb_card(tmp_path, "UMIK2", UMIK2_USB_ID, channels=6)

    result = _run_reconcile(tmp_path, "--reason", "systemd")

    assert result.returncode == 0, result.stderr
    commands = _systemctl_log(tmp_path)
    assert "enable jasper-aec-init.service jasper-aec-bridge.service" not in commands
    body = (tmp_path / "jasper.env").read_text()
    # The fallback is the stock first candidate — a real card name that
    # simply parks while absent — never the instrument, never stale UDP.
    assert "JASPER_MIC_DEVICE=Array" in body
    assert "JASPER_MIC_DEVICE=UMIK2" not in body
    assert "JASPER_MIC_DEVICE=udp:9876" not in body
    assert (tmp_path / "voice-input-absent").exists()


def test_a_stale_aec_mic_seed_naming_the_instrument_never_arms_aec(
    tmp_path: Path,
) -> None:
    """aec_ready's own measurement-class refusal, reached when
    JASPER_AEC_MIC_DEVICE already NAMES the instrument — a stale seed written
    by a build predating the fallback fix, or a hand edit. The fixed fallback
    upstream cannot help here (it never runs when the seed is set), so this
    is the last line before the software-AEC stack opens a measurement mic."""
    _write_env(
        tmp_path,
        "udp:9876",
        extra=(
            "JASPER_MIC_DEVICE_CANDIDATES=UMIK2\n"
            "JASPER_AEC_MIC_DEVICE=UMIK2\n"
        ),
    )
    _write_mode(tmp_path)
    _write_usb_card(tmp_path, "UMIK2", UMIK2_USB_ID, channels=6)

    result = _run_reconcile(tmp_path, "--reason", "systemd")

    assert result.returncode == 0, result.stderr
    commands = _systemctl_log(tmp_path)
    assert "enable jasper-aec-init.service jasper-aec-bridge.service" not in commands
    body = (tmp_path / "jasper.env").read_text()
    assert "JASPER_MIC_DEVICE=udp:9876" not in body
    assert (tmp_path / "voice-input-absent").exists()


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
    """Drift guard on the exclusion list.

    A typo (or a key that stops being written) silently promotes a descriptive
    key back to voice-relevant, re-arming the bounce this PR removes. That
    failure is invisible at runtime, so it is pinned here.
    """
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
    """Defense in depth. DEFAULT_MIC_DEVICE_CANDIDATES is a closed allowlist no
    measurement mic appears in, so this only bites for an operator who widened
    JASPER_MIC_DEVICE_CANDIDATES — and then it must bite: a UMIK-2 carries no
    wake or AEC contract."""
    _write_env(
        tmp_path,
        "udp:9876",
        extra="JASPER_MIC_DEVICE_CANDIDATES=UMIK2,Array\n",
    )
    _write_mode(tmp_path)
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
    _write_env(
        tmp_path,
        "udp:9876",
        extra="JASPER_MIC_DEVICE_CANDIDATES=USBMIC\n",
    )
    _write_mode(tmp_path)
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
    broken = tmp_path / "broken-measurement-python"
    broken.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$*" == *"jasper.cli.measurement_mic"* ]]; then\n'
        "  exit 1\n"
        "fi\n"
        f'exec "{sys.executable}" "$@"\n',
        encoding="utf-8",
    )
    broken.chmod(0o755)
    _write_env(
        tmp_path,
        "udp:9876",
        extra="JASPER_MIC_DEVICE_CANDIDATES=UMIK2\n",
    )
    _write_mode(tmp_path)
    _write_usb_card(tmp_path, "UMIK2", UMIK2_USB_ID, channels=1)

    result = _run_reconcile(
        tmp_path,
        "--reason",
        "systemd",
        extra_env={"JASPER_MIC_PROFILE_PYTHON": str(broken)},
    )

    assert result.returncode == 0, result.stderr
    assert "measurement-mic registry probe failed" in result.stderr
    assert "JASPER_MIC_DEVICE=UMIK2" in (tmp_path / "jasper.env").read_text()


def test_measurement_exclusion_costs_no_interpreter_without_a_usb_card(
    tmp_path: Path,
) -> None:
    """A card with no usbid (absent, I2S, virtual) cannot be a registered USB
    measurement mic, so the resolver is never spawned for it. Proven with an
    interpreter that fails loudly if it is asked for the measurement
    registry."""
    tripwire = tmp_path / "tripwire-python"
    tripwire.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$*" == *"jasper.cli.measurement_mic"* ]]; then\n'
        "  echo 'measurement resolver was spawned' >&2\n"
        "  exit 1\n"
        "fi\n"
        f'exec "{sys.executable}" "$@"\n',
        encoding="utf-8",
    )
    tripwire.chmod(0o755)
    _write_env(tmp_path, "Array")
    _write_mode(tmp_path)
    _write_card(tmp_path, channels=6)  # stream0 only, no usbid

    result = _run_reconcile(
        tmp_path,
        "--reason",
        "systemd",
        extra_env={"JASPER_MIC_PROFILE_PYTHON": str(tripwire)},
    )

    assert result.returncode == 0, result.stderr
    assert "measurement resolver was spawned" not in result.stderr
