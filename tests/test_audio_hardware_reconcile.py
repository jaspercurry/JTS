from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "bin" / "jasper-audio-hardware-reconcile"


def _fake_systemctl(tmp_path: Path) -> tuple[Path, Path]:
    log = tmp_path / "systemctl.log"
    fake = tmp_path / "systemctl"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$JASPER_SYSTEMCTL_LOG\"\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return fake, log


def _fake_aplay(tmp_path: Path, listing: str) -> Path:
    fake = tmp_path / "aplay"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        "cat \"$JASPER_FAKE_APLAY_LISTING\"\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    (tmp_path / "aplay-L.txt").write_text(listing, encoding="utf-8")
    return fake


def _fake_renderer(tmp_path: Path) -> tuple[Path, Path]:
    log = tmp_path / "render.log"
    fake = tmp_path / "jasper-render-asound-conf"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        "printf 'render\\n' >> \"$JASPER_RENDER_LOG\"\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return fake, log


def _run_reconcile(
    tmp_path: Path,
    listing: str,
    *args: str,
    initial_env: str | None = None,
    initial_template: str | None = None,
    create_source_template: bool = True,
) -> subprocess.CompletedProcess[str]:
    fake_systemctl, systemctl_log = _fake_systemctl(tmp_path)
    fake_aplay = _fake_aplay(tmp_path, listing)
    fake_renderer, render_log = _fake_renderer(tmp_path)
    source_template = tmp_path / "asoundrc.jasper.source"
    if create_source_template:
        source_template.write_text(
            "__OUTPUTD_DAC_PCM_BLOCK__\n"
            "ctl.outputd_dac { card __OUTPUT_DAC_CARD__ }\n"
            "pcm.jasper_out { card __DONGLE_CARD__ }\n"
            "defaults.pcm.rate_converter \"__RATE_CONVERTER__\"\n",
            encoding="utf-8",
        )
    audio_quality = tmp_path / "audio_quality.env"
    audio_quality.write_text(
        "JASPER_ALSA_RATE_CONVERTER=samplerate_medium\n",
        encoding="utf-8",
    )
    if initial_env is not None:
        (tmp_path / "jasper.env").write_text(initial_env, encoding="utf-8")
    if initial_template is not None:
        (tmp_path / "asoundrc.jasper.template").write_text(
            initial_template,
            encoding="utf-8",
        )
    sys_sound = tmp_path / "sys" / "class" / "sound"
    proc_asound = tmp_path / "proc" / "asound"
    sys_sound.mkdir(parents=True)
    proc_asound.mkdir(parents=True)

    env = os.environ.copy()
    env.update(
        {
            "JASPER_ENV_FILE": str(tmp_path / "jasper.env"),
            "JASPER_ASOUND_SOURCE_TEMPLATE": str(source_template),
            "JASPER_ASOUND_TEMPLATE": str(tmp_path / "asoundrc.jasper.template"),
            "JASPER_ASOUND_CONF": str(tmp_path / "asound.conf"),
            "JASPER_AUDIO_QUALITY_FILE": str(audio_quality),
            "JASPER_RENDER_ASOUND_CONF": str(fake_renderer),
            "JASPER_RENDER_LOG": str(render_log),
            "JASPER_SYSTEMCTL": str(fake_systemctl),
            "JASPER_SYSTEMCTL_LOG": str(systemctl_log),
            "JASPER_APLAY": str(fake_aplay),
            "JASPER_FAKE_APLAY_LISTING": str(tmp_path / "aplay-L.txt"),
            "JASPER_OUTPUT_HARDWARE_APLAY_LISTING": str(tmp_path / "aplay-L.txt"),
            "JASPER_OUTPUT_HARDWARE_STATE_PATH": str(
                tmp_path / "output_hardware.json"
            ),
            "JASPER_SYS_CLASS_SOUND": str(sys_sound),
            "JASPER_PROC_ASOUND": str(proc_asound),
        }
    )
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        check=False,
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )


def _systemctl_log(tmp_path: Path) -> str:
    log = tmp_path / "systemctl.log"
    return log.read_text(encoding="utf-8") if log.exists() else ""


def _render_log(tmp_path: Path) -> str:
    log = tmp_path / "render.log"
    return log.read_text(encoding="utf-8") if log.exists() else ""


APPLE_LISTING = """
hw:CARD=A,DEV=0
    Apple USB-C to 3.5mm Headphone Jack, USB Audio
"""


DUAL_APPLE_LISTING = """
hw:CARD=A,DEV=0
    Apple USB-C to 3.5mm Headphone Jack, USB Audio
hw:CARD=B,DEV=0
    Apple USB-C to 3.5mm Headphone Jack, USB Audio
"""


DAC8X_AND_APPLE_LISTING = """
hw:CARD=A,DEV=0
    Apple USB-C to 3.5mm Headphone Jack, USB Audio
hw:CARD=sndrpihifiberry,DEV=0
    snd_rpi_hifiberry_dac8x, HiFiBerry DAC8x
"""


def test_print_env_prefers_dac8x_but_keeps_apple_control_role(tmp_path: Path):
    result = _run_reconcile(
        tmp_path,
        DAC8X_AND_APPLE_LISTING,
        "--print-env",
    )

    assert result.returncode == 0, result.stderr
    assert "DONGLE_CARD=A" in result.stdout
    assert "APPLE_DONGLE_PRESENT=1" in result.stdout
    assert "APPLE_DONGLE_COUNT=1" in result.stdout
    assert "APPLE_DONGLE_SERVICE_CARD=A" in result.stdout
    assert "OUTPUT_DAC_CARD=sndrpihifiberry" in result.stdout
    assert "OUTPUT_DAC_ID=hifiberry_dac8x" in result.stdout
    assert "OUTPUT_DAC_RECOGNIZED=1" in result.stdout
    assert "OUTPUT_DAC_ROUTE=''" in result.stdout
    assert not (tmp_path / "jasper.env").exists()


def test_reconcile_apple_role_enables_apple_helpers_and_renders(tmp_path: Path):
    result = _run_reconcile(tmp_path, APPLE_LISTING, "--reason", "test")

    assert result.returncode == 0, result.stderr
    env_text = (tmp_path / "jasper.env").read_text(encoding="utf-8")
    assert "JASPER_AUDIO_DAC_ID=apple_usb_c_dongle" in env_text
    assert "JASPER_AUDIO_DAC_CARD=A" in env_text
    state = json.loads((tmp_path / "output_hardware.json").read_text(encoding="utf-8"))
    assert state["kind"] == "jts_output_hardware_state"
    assert state["active"]["profile_id"] == "apple_usb_c_dongle"
    assert state["active"]["runtime_ready"] is True
    assert state["observed"]["profile_id"] == "apple_usb_c_dongle"
    assert state["observed"]["physical_output_count"] == 2
    template = (tmp_path / "asoundrc.jasper.template").read_text(encoding="utf-8")
    assert "pcm.outputd_dac" in template
    assert "type hw" in template
    assert "card A" in template
    assert _render_log(tmp_path) == "render\n"
    commands = _systemctl_log(tmp_path)
    assert "enable jasper-dac-init.service jasper-headphone-monitor.service" in commands
    assert "start jasper-dac-init.service" in commands
    assert "restart jasper-headphone-monitor.service" in commands
    assert "stop jasper-voice.service" in commands
    assert "reset-failed jasper-outputd.service" in commands
    assert "--no-block restart jasper-outputd.service" in commands
    assert "--no-block restart jasper-aec-reconcile.service" in commands


def test_reconcile_dac8x_role_disables_apple_helpers(tmp_path: Path):
    result = _run_reconcile(tmp_path, DAC8X_AND_APPLE_LISTING, "--reason", "test")

    assert result.returncode == 0, result.stderr
    env_text = (tmp_path / "jasper.env").read_text(encoding="utf-8")
    assert "JASPER_AUDIO_DAC_ID=hifiberry_dac8x" in env_text
    assert "JASPER_AUDIO_DAC_CARD=sndrpihifiberry" in env_text
    state = json.loads((tmp_path / "output_hardware.json").read_text(encoding="utf-8"))
    assert state["active"]["profile_id"] == "hifiberry_dac8x"
    assert state["observed"]["profile_id"] == "hifiberry_dac8x"
    assert state["observed"]["apple_dac_count"] == 1
    template = (tmp_path / "asoundrc.jasper.template").read_text(encoding="utf-8")
    assert "pcm.outputd_dac" in template
    assert "type hw" in template
    assert "card sndrpihifiberry" in template
    assert "pcm.jasper_out { card A }" in template
    commands = _systemctl_log(tmp_path)
    assert "disable --now jasper-dac-init.service jasper-headphone-monitor.service" in commands
    assert "reset-failed jasper-dac-init.service jasper-headphone-monitor.service" in commands
    assert "enable jasper-dac-init.service" not in commands
    assert "stop jasper-voice.service" in commands
    assert "--no-block restart jasper-outputd.service" in commands
    assert "--no-block restart jasper-aec-reconcile.service" in commands


def test_reconcile_unknown_role_parks_output_without_rerender(tmp_path: Path):
    result = _run_reconcile(tmp_path, "", "--reason", "test")

    assert result.returncode == 0, result.stderr
    env_text = (tmp_path / "jasper.env").read_text(encoding="utf-8")
    assert "JASPER_AUDIO_DAC_ID=A" in env_text
    assert "JASPER_AUDIO_DAC_CARD=A" in env_text
    assert not (tmp_path / "asoundrc.jasper.template").exists()
    assert _render_log(tmp_path) == ""
    commands = _systemctl_log(tmp_path)
    assert "disable --now jasper-dac-init.service jasper-headphone-monitor.service" in commands
    assert "--no-block stop jasper-voice.service jasper-outputd.service" in commands
    assert "reset-failed jasper-voice.service jasper-outputd.service" in commands
    assert "restart jasper-outputd.service" not in commands
    assert "restart jasper-aec-reconcile.service" not in commands
    assert "event=audio_hardware_reconcile.output_parked" in result.stderr
    state = json.loads((tmp_path / "output_hardware.json").read_text(encoding="utf-8"))
    assert state["active"]["profile_id"] == "unknown"
    assert state["active"]["status"] == "parked"
    assert state["observed"]["status"] == "missing"


def test_reconcile_dual_apple_observes_composite_without_runtime_handoff(
    tmp_path: Path,
):
    result = _run_reconcile(tmp_path, DUAL_APPLE_LISTING, "--reason", "test")

    assert result.returncode == 0, result.stderr
    env_text = (tmp_path / "jasper.env").read_text(encoding="utf-8")
    assert "JASPER_AUDIO_DAC_ID=apple_usb_c_dongle" in env_text
    assert "JASPER_AUDIO_DAC_CARD=A" in env_text
    state = json.loads((tmp_path / "output_hardware.json").read_text(encoding="utf-8"))
    assert state["active"]["profile_id"] == "apple_usb_c_dongle"
    assert state["active"]["runtime_ready"] is True
    assert state["observed"]["profile_id"] == "dual_apple_usb_c_dac_4ch"
    assert state["observed"]["physical_output_count"] == 4
    assert state["observed"]["status"] == "blocked"
    assert [child["card_id"] for child in state["child_devices"]] == ["A", "B"]
    assert {
        issue["code"] for issue in state["issues"]
    } >= {
        "dual_apple_usb_topology_unknown",
        "dual_apple_stable_identity_missing",
        "observed_active_profile_mismatch",
        "dual_apple_runtime_handoff_pending",
    }


def test_reconcile_does_not_publish_active_env_when_render_template_missing(
    tmp_path: Path,
):
    result = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "test",
        create_source_template=False,
    )

    assert result.returncode == 66
    assert not (tmp_path / "jasper.env").exists()
    assert not (tmp_path / "output_hardware.json").exists()
    assert "source_template=" in result.stderr


def test_reconcile_recognized_role_restarts_outputd_after_unknown_state(
    tmp_path: Path,
):
    result = _run_reconcile(
        tmp_path,
        DAC8X_AND_APPLE_LISTING,
        "--reason",
        "test",
        initial_env="JASPER_AUDIO_DAC_ID=A\nJASPER_AUDIO_DAC_CARD=A\n",
        initial_template=(
            "pcm.outputd_dac {\n"
            "    type hw\n"
            "    card sndrpihifiberry\n"
            "    device 0\n"
            "}\n"
            "ctl.outputd_dac { card sndrpihifiberry }\n"
            "pcm.jasper_out { card A }\n"
            "defaults.pcm.rate_converter \"__RATE_CONVERTER__\"\n"
        ),
    )

    assert result.returncode == 0, result.stderr
    assert _render_log(tmp_path) == ""
    env_text = (tmp_path / "jasper.env").read_text(encoding="utf-8")
    assert "JASPER_AUDIO_DAC_ID=hifiberry_dac8x" in env_text
    assert "JASPER_AUDIO_DAC_CARD=sndrpihifiberry" in env_text
    commands = _systemctl_log(tmp_path)
    assert "stop jasper-voice.service" in commands
    assert "reset-failed jasper-outputd.service" in commands
    assert "--no-block restart jasper-outputd.service" in commands
    assert "--no-block restart jasper-aec-reconcile.service" in commands


def test_reconcile_dac8x_mono_route_renders_channel_five_sum(tmp_path: Path):
    result = _run_reconcile(
        tmp_path,
        DAC8X_AND_APPLE_LISTING,
        "--reason",
        "test",
        initial_env="JASPER_OUTPUT_DAC_ROUTE=mono:5\n",
    )

    assert result.returncode == 0, result.stderr
    template = (tmp_path / "asoundrc.jasper.template").read_text(encoding="utf-8")
    assert "type route" in template
    assert 'pcm "hw:CARD=sndrpihifiberry,DEV=0"' in template
    assert "channels 8" in template
    assert "0.4 0.5" in template
    assert "1.4 0.5" in template
    assert "output_dac_route=mono:5" in result.stderr


def test_reconcile_dac8x_stereo_route_renders_distinct_outputs(tmp_path: Path):
    result = _run_reconcile(
        tmp_path,
        DAC8X_AND_APPLE_LISTING,
        "--reason",
        "test",
        initial_env="JASPER_OUTPUT_DAC_ROUTE=stereo:5,6\n",
    )

    assert result.returncode == 0, result.stderr
    template = (tmp_path / "asoundrc.jasper.template").read_text(encoding="utf-8")
    assert "type route" in template
    assert "0.4 1.0" in template
    assert "1.5 1.0" in template


def test_reconcile_ignores_invalid_dac8x_route_without_rerendering_route(
    tmp_path: Path,
):
    result = _run_reconcile(
        tmp_path,
        DAC8X_AND_APPLE_LISTING,
        "--reason",
        "test",
        initial_env="JASPER_OUTPUT_DAC_ROUTE=stereo:5,5\n",
    )

    assert result.returncode == 0, result.stderr
    template = (tmp_path / "asoundrc.jasper.template").read_text(encoding="utf-8")
    assert "type hw" in template
    assert "type route" not in template
    assert "reason=duplicate_stereo_channel" in result.stderr


def test_reconcile_ignores_route_for_apple_output_role(tmp_path: Path):
    result = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "test",
        initial_env="JASPER_OUTPUT_DAC_ROUTE=mono:5\n",
    )

    assert result.returncode == 0, result.stderr
    template = (tmp_path / "asoundrc.jasper.template").read_text(encoding="utf-8")
    assert "type hw" in template
    assert "card A" in template
    assert "type route" not in template
    assert "reason=unsupported_dac" in result.stderr
