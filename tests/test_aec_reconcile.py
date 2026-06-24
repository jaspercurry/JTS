# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from jasper.audio_profile_state import profile_env_updates
from jasper.multiroom.tts_route import VOICE_PARK_ENV
from jasper.tts_routing import OUTPUTD_TTS_SOCKET, VOICE_TTS_SOCKET_ENV
from jasper.voice.catalog import VALID_PROVIDER_IDS, provider_ids_manifest_text


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "bin" / "jasper-aec-reconcile"


def _fake_systemctl(tmp_path: Path) -> tuple[Path, Path]:
    log = tmp_path / "systemctl.log"
    fake = tmp_path / "systemctl"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$JASPER_SYSTEMCTL_LOG\"\n"
        "exit 0\n"
    )
    fake.chmod(0o755)
    return fake, log


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
            # Redirect the voice-input-absent marker into tmp so the no-mic
            # paths (mark_voice_input_absent) never touch the real
            # /var/lib/jasper on the test host. Per-test overrides via
            # extra_env still win (the marker cases assert on this path).
            "JASPER_VOICE_INPUT_ABSENT_MARKER": str(
                tmp_path / "voice-input-absent"
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


def _systemctl_log(tmp_path: Path) -> str:
    log = tmp_path / "systemctl.log"
    return log.read_text() if log.exists() else ""


def _outputd_status_payload(
    *,
    verdict: str,
    status: str = "locked",
    observe: bool = True,
    writer_enabled: bool = True,
) -> dict:
    return {
        "reference_outputs": {
            "chip_ref_pcm": "plughw:CARD=Array,DEV=0",
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


@contextmanager
def _fake_outputd_status_socket(payload: dict):
    """Serve one small STATUS JSON fixture over a short-path UDS."""

    with tempfile.TemporaryDirectory(prefix="jts-aec-", dir="/tmp") as root:
        socket_path = str(Path(root) / "outputd.sock")
        ready = threading.Event()
        stop = threading.Event()
        errors: list[BaseException] = []

        def serve() -> None:
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as srv:
                    srv.bind(socket_path)
                    srv.listen()
                    srv.settimeout(0.1)
                    ready.set()
                    while not stop.is_set():
                        try:
                            conn, _ = srv.accept()
                        except socket.timeout:
                            continue
                        with conn:
                            try:
                                conn.recv(1024)
                            except OSError:
                                pass
                            conn.sendall(json.dumps(payload).encode("utf-8"))
            except OSError as exc:
                errors.append(exc)
                ready.set()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        assert ready.wait(2.0), "fake outputd STATUS socket did not start"
        if errors:
            raise errors[0]
        try:
            yield socket_path
        finally:
            stop.set()
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(0.1)
                    client.connect(socket_path)
                    client.sendall(b"STATUS\n")
            except OSError:
                pass
            thread.join(2.0)


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
    assert "restart jasper-voice.service" not in commands
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
    assert "JASPER_MIC_DEVICE=udp:9876" in env_file.read_text()
    commands = _systemctl_log(tmp_path)
    assert "enable jasper-aec-init.service jasper-aec-bridge.service" in commands
    assert "reset-failed jasper-aec-init.service" in commands
    assert "reset-failed jasper-aec-bridge.service" in commands
    assert "is-failed --quiet" not in commands
    assert "start jasper-aec-init.service" in commands
    assert "restart jasper-aec-bridge.service" in commands
    assert "enable jasper-voice.service" in commands
    assert "restart jasper-voice.service" in commands


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
    assert "restart jasper-voice.service" in commands


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
    assert "restart jasper-voice.service" not in commands


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
    assert "restart jasper-voice.service" not in commands


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
    assert "restart jasper-voice.service" not in commands


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
    assert "restart jasper-voice.service" not in commands


def test_reconcile_uses_direct_mic_when_array_is_not_6_channel(tmp_path: Path) -> None:
    env_file = _write_env(tmp_path, "udp:9876")
    _write_mode(tmp_path)
    _write_card(tmp_path, channels=2)

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    assert "JASPER_MIC_DEVICE=Array" in env_file.read_text()
    commands = _systemctl_log(tmp_path)
    assert "disable jasper-aec-bridge.service jasper-aec-init.service" in commands
    assert "restart jasper-voice.service" in commands


def test_reconcile_respects_custom_mic_device(tmp_path: Path) -> None:
    env_file = _write_env(tmp_path, "UMIK-2")
    _write_mode(tmp_path)

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    assert "JASPER_MIC_DEVICE=UMIK-2" in env_file.read_text()
    commands = _systemctl_log(tmp_path)
    assert "disable jasper-aec-bridge.service jasper-aec-init.service" in commands
    assert "stop jasper-voice.service" not in commands
    assert "restart jasper-voice.service" not in commands


def test_check_aec_ready_reflects_mode_and_firmware(tmp_path: Path) -> None:
    _write_env(tmp_path, "Array")
    _write_mode(tmp_path)
    _write_card(tmp_path, channels=6)
    assert _run_reconcile(tmp_path, "--check-aec-ready").returncode == 0

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
    chip_ref_observe: str | None = None,
) -> None:
    body = (
        f"JASPER_AEC_MODE={mode}\n"
        f"JASPER_WAKE_LEG_RAW={raw}\n"
        f"JASPER_WAKE_LEG_DTLN={dtln}\n"
    )
    # When chip_aec is None the key is omitted, so ensure_mode_file
    # appends the default (0) — exercising the pre-chip-AEC upgrade path.
    if chip_aec is not None:
        body += f"JASPER_WAKE_LEG_CHIP_AEC={chip_aec}\n"
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
    assert "JASPER_MIC_DEVICE_CHIP_AEC_150=udp:9887" in body
    assert "JASPER_AEC_CHIP_AEC_ENABLED=1" in body
    assert "JASPER_MIC_DEVICE_RAW=udp:" not in body
    assert "JASPER_MIC_DEVICE_DTLN=udp:" not in body


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
    assert "JASPER_MIC_DEVICE_RAW=udp:9877" in body
    assert "JASPER_MIC_DEVICE_CHIP_AEC_150=udp:" not in body


@pytest.mark.parametrize(
    ("dac_id", "stderr_phrase"),
    [
        ("hifiberry_dac8x_studio", "HiFiBerry DAC8x Studio needs per-profile"),
        ("mystery_usb_audio", "has no codified chip-AEC calibration"),
    ],
)
def test_auto_profile_falls_back_when_output_dac_needs_calibration(
    tmp_path: Path,
    dac_id: str,
    stderr_phrase: str,
) -> None:
    """Chip-AEC is gated by both XVF firmware and output DAC timing support.
    Calibration-required and future DAC profiles stay on the software AEC path
    instead of inheriting the Apple baseline by accident."""
    _write_env(tmp_path, "Array", extra=f"JASPER_AUDIO_DAC_ID={dac_id}\n")
    _write_card(tmp_path, channels=6)

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    assert stderr_phrase in result.stderr
    body = (tmp_path / "jasper.env").read_text()
    assert "JASPER_MIC_DEVICE=udp:9876" in body
    assert "JASPER_MIC_DEVICE_RAW=udp:9877" in body
    assert "JASPER_AEC_CHIP_AEC_ENABLED=0" in body
    assert "JASPER_MIC_DEVICE_CHIP_AEC_150=udp:" not in body
    assert "JASPER_OUTPUTD_CHIP_REF_PCM=''" in body
    assert "JASPER_OUTPUTD_REFERENCE_UDP_TARGET=127.0.0.1:9891" in body


def test_explicit_chip_profile_falls_back_for_uncalibrated_output_dac(
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
    assert "requested xvf_chip_aec" in result.stderr
    assert "measured-sync contract" in result.stderr
    body = (tmp_path / "jasper.env").read_text()
    assert "JASPER_MIC_DEVICE=udp:9876" in body
    assert "JASPER_MIC_DEVICE_RAW=udp:9877" in body
    assert "JASPER_AEC_CHIP_AEC_ENABLED=0" in body
    assert "JASPER_OUTPUTD_CHIP_REF_PCM=''" in body


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
    assert "JASPER_MIC_DEVICE_CHIP_AEC_150=udp:9887" in body
    assert "JASPER_MIC_DEVICE_CHIP_AEC_210=udp:9888" in body
    assert "JASPER_MIC_DEVICE_RAW=udp:" not in body
    assert "JASPER_OUTPUTD_CHIP_REF_PCM=plughw:CARD=Array,DEV=0" in body
    assert "JASPER_OUTPUTD_CHIP_REF_OBSERVE=0" in body


def test_auto_profile_uses_outputd_coherent_verdict_for_uncodified_dac(
    tmp_path: Path,
) -> None:
    """Future DACs can still promote through live outputd calibration evidence."""
    _write_env(
        tmp_path,
        "udp:9876",
        extra="JASPER_AUDIO_DAC_ID=mystery_usb_audio\n",
    )
    _write_card(tmp_path, channels=6)

    with _fake_outputd_status_socket(
        _outputd_status_payload(verdict="coherent", status="locked"),
    ) as socket_path:
        result = _run_reconcile(
            tmp_path,
            "--reason",
            "test",
            extra_env={"JASPER_OUTPUTD_CONTROL_SOCKET": socket_path},
        )

    assert result.returncode == 0, result.stderr
    body = (tmp_path / "jasper.env").read_text()
    assert "JASPER_AEC_CHIP_AEC_ENABLED=1" in body
    assert "JASPER_MIC_DEVICE_CHIP_AEC_150=udp:9887" in body
    assert "JASPER_MIC_DEVICE_RAW=udp:" not in body


def test_explicit_chip_profile_still_falls_back_for_compensable_verdict(
    tmp_path: Path,
) -> None:
    """A measured but drifting DAC needs the deferred rate-match layer, so
    `compensable` remains on the software-AEC3 floor."""
    _write_env(
        tmp_path,
        "udp:9876",
        extra="JASPER_AUDIO_DAC_ID=mystery_usb_audio\n",
    )
    _write_profile_mode(tmp_path, "xvf_chip_aec")
    _write_card(tmp_path, channels=6)

    with _fake_outputd_status_socket(
        _outputd_status_payload(verdict="compensable", status="locked"),
    ) as socket_path:
        result = _run_reconcile(
            tmp_path,
            "--reason",
            "test",
            extra_env={"JASPER_OUTPUTD_CONTROL_SOCKET": socket_path},
        )

    assert result.returncode == 0, result.stderr
    assert "verdict=compensable" in result.stderr
    body = (tmp_path / "jasper.env").read_text()
    assert "JASPER_AEC_CHIP_AEC_ENABLED=0" in body
    assert "JASPER_MIC_DEVICE_RAW=udp:9877" in body
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
                "raw_udp": True,
                "dtln_udp": False,
                "chip_enabled": "0",
                "ref_source": "outputd_udp",
            },
        ),
        (
            "direct_mic", 6,
            {
                "mic": "Array",
                "raw_udp": False,
                "dtln_udp": False,
                "chip_enabled": "0",
                "ref_source": "alsa",
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


# ---------- Chip-AEC beam legs (chip-AEC promotion P2) --------------------
# JASPER_WAKE_LEG_CHIP_AEC (one boolean) maps to BOTH chip-beam mic device
# vars + the JASPER_AEC_CHIP_AEC_ENABLED bridge/init signal, and is
# mutually exclusive with raw/DTLN (single-chip Option-A). Default off, so
# any install that hasn't opted in keeps the same runtime shape. The
# reconciler also owns outputd's chip-reference fanout when chip-AEC is
# active; outputd must restart when those producer vars change.


def test_ensure_mode_file_seeds_chip_aec_default(tmp_path: Path) -> None:
    """Fresh install: the mode file gets JASPER_WAKE_LEG_CHIP_AEC=0
    alongside the existing leg defaults. Must match install.sh's seed."""
    _write_env(tmp_path, "Array")
    _run_reconcile(tmp_path, "--reason", "test")
    body = (tmp_path / "aec_mode.env").read_text()
    assert "JASPER_WAKE_LEG_CHIP_AEC=0" in body


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
    assert "JASPER_AUDIO_INPUT_PROFILE=custom" in body  # raw+DTLN is custom


def test_chip_aec_on_sets_chip_vars_and_clears_raw_dtln(tmp_path: Path) -> None:
    """AEC auto + 6-ch + CHIP_AEC=1 → sets both chip-beam UDP devices +
    JASPER_AEC_CHIP_AEC_ENABLED=1, and CLEARS raw/DTLN even though their
    booleans are on (single-chip mutual exclusion: the bridge can't emit
    the software legs and the chip beams at the same time)."""
    _write_env(tmp_path, "udp:9876", extra="JASPER_AUDIO_DAC_ID=apple_usb_c_dongle\n")
    _write_mode_with_legs(tmp_path, mode="auto", raw="1", dtln="1", chip_aec="1")
    _write_card(tmp_path, channels=6)
    _run_reconcile(tmp_path, "--reason", "test")
    body = (tmp_path / "jasper.env").read_text()
    assert "JASPER_MIC_DEVICE_CHIP_AEC_150=udp:9887" in body
    assert "JASPER_MIC_DEVICE_CHIP_AEC_210=udp:9888" in body
    assert "JASPER_AEC_CHIP_AEC_ENABLED=1" in body
    assert "JASPER_AEC_REF_SOURCE=outputd_udp" in body
    assert "JASPER_AEC_OUTPUTD_REF_UDP_HOST=127.0.0.1" in body
    assert "JASPER_AEC_OUTPUTD_REF_UDP_PORT=9891" in body
    assert "JASPER_OUTPUTD_CHIP_REF_PCM=plughw:CARD=Array,DEV=0" in body
    assert "JASPER_OUTPUTD_REFERENCE_UDP_TARGET=127.0.0.1:9891" in body
    assert "JASPER_OUTPUTD_CHIP_REF_SAMPLE_RATE=16000" in body
    assert "JASPER_OUTPUTD_CHIP_REF_PERIOD_FRAMES=320" in body
    assert "JASPER_OUTPUTD_CHIP_REF_BUFFER_FRAMES=1280" in body
    assert "JASPER_MIC_DEVICE_RAW=udp:" not in body
    assert "JASPER_MIC_DEVICE_DTLN=udp:" not in body
    assert "JASPER_AEC_DTLN_ENABLED=1" not in body
    commands = _systemctl_log(tmp_path)
    assert "restart jasper-outputd.service" in commands


def test_flex_linear_auto_discovers_card_but_does_not_arm_square_chip_beams(
    tmp_path: Path,
) -> None:
    """Flex linear firmware enumerates as L16K6Ch, not Array. With no
    explicit JASPER_AEC_MIC_DEVICE pinned, the reconciler should select
    the present Flex card but refuse the legacy square 150/210 chip plan."""
    _write_env(tmp_path, "udp:9876", extra="JASPER_AUDIO_DAC_ID=apple_usb_c_dongle\n")
    _write_mode_with_legs(tmp_path, mode="auto", raw="0", dtln="0", chip_aec="1")
    _write_card(tmp_path, card="L16K6Ch", channels=6)

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    body = (tmp_path / "jasper.env").read_text()
    assert "JASPER_MIC_DEVICE=udp:9876" in body
    assert "JASPER_XVF_VARIANT=xvf3800_flex_linear_6ch" in body
    assert "JASPER_XVF_GEOMETRY=linear" in body
    assert "JASPER_XVF_CHIP_AEC_SUPPORTED=0" in body
    assert "JASPER_AEC_CHIP_AEC_ENABLED=0" in body
    assert "JASPER_MIC_DEVICE_RAW=udp:9877" in body
    assert "JASPER_OUTPUTD_CHIP_REF_PCM=''" in body
    assert "aec_mic=L16K6Ch" in result.stderr
    assert "no validated production chip beam plan" in result.stderr


def test_chip_aec_comma_values_idempotent_across_runs(tmp_path: Path) -> None:
    """Regression for the `printf %q` comma-corruption bug (PR #534's
    bug class, in this script): bash 5.2 %q-escapes commas, turning
    plughw:CARD=Array,DEV=0 into plughw:CARD=Array\\,DEV=0 — which
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
    assert "JASPER_OUTPUTD_CHIP_REF_PCM=plughw:CARD=Array,DEV=0" in first_body
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
            "JASPER_OUTPUTD_CHIP_REF_PCM=plughw:CARD=Array,DEV=0\n"
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
    assert "JASPER_OUTPUTD_CHIP_REF_PCM=plughw:CARD=Array,DEV=0" in body
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
    assert "restart jasper-voice.service" not in commands
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
    assert "restart jasper-voice.service" in commands
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
    assert "restart jasper-voice.service" in _systemctl_log(tmp_path)


def test_reconcile_clears_marker_when_direct_mic_present(tmp_path: Path) -> None:
    # 2-channel Array -> direct-mic (no AEC) path still (re)starts voice, so
    # the marker must clear here too.
    _write_env(tmp_path, "udp:9876")
    _write_mode(tmp_path)
    _write_card(tmp_path, channels=2)
    _marker(tmp_path).write_text("reason=stale\n")

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    assert not _marker(tmp_path).exists(), result.stderr
    assert "restart jasper-voice.service" in _systemctl_log(tmp_path)


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
