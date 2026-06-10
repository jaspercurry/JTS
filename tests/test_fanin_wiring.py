"""Config-shape tests for the fan-in audio topology.

These read deploy-time files directly and lock down the production
renderer graph:

    renderer/test private lanes -> jasper-fanin -> jasper_capture substream 7

They do not exercise ALSA itself; hardware validation lives on the Pi
through jasper-doctor and the AirPlay/renderer smoke tests.
"""
from __future__ import annotations

import re
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def _non_comment(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines()
        if not line.lstrip().startswith("#")
    )


def _pcm_block(text: str, name: str) -> str:
    start = text.index(f"pcm.{name}")
    tail = text[start:]
    next_def = re.search(r"^(?:pcm|ctl)\.", tail[len(f"pcm.{name}"):], re.MULTILINE)
    if next_def:
        return tail[:len(f"pcm.{name}") + next_def.start()]
    return tail


def test_asoundrc_has_no_legacy_renderer_dmix():
    rc = _non_comment((REPO / "deploy" / "alsa" / "asoundrc.jasper").read_text())
    assert not re.search(r"^pcm\.jasper_renderer_mix\s*\{", rc, re.MULTILINE)
    assert not re.search(r"^pcm\.jasper_renderer_in\s*\{", rc, re.MULTILINE)


def test_asoundrc_declares_private_renderer_lanes():
    rc = _non_comment((REPO / "deploy" / "alsa" / "asoundrc.jasper").read_text())
    aliases = {
        "librespot_substream": "hw:Loopback,0,0",
        "shairport_substream": "hw:Loopback,0,1",
        "bluealsa_substream": "hw:Loopback,0,2",
        "usbsink_substream": "hw:Loopback,0,3",
        "correction_substream": "hw:Loopback,0,4",
    }
    seen: set[str] = set()
    for alias, expected_slave in aliases.items():
        block = _pcm_block(rc, alias)
        assert "type plug" in block
        assert f'pcm "{expected_slave}"' in block
        assert "rate 48000" in block
        assert "channels 2" in block
        assert "format S16_LE" in block
        assert "slave.pcm" not in block
        assert expected_slave not in seen, f"duplicate lane {expected_slave}"
        seen.add(expected_slave)


def test_asoundrc_capture_reads_fanin_summed_output():
    rc = _non_comment((REPO / "deploy" / "alsa" / "asoundrc.jasper").read_text())
    capture = _pcm_block(rc, "jasper_capture")
    assert 'pcm "hw:Loopback,1,7"' in capture
    assert 'pcm "hw:Loopback,1,0"' not in capture
    assert "pcm.jasper_ref" in rc
    assert 'slave.pcm "jasper_capture"' in rc


def test_renderer_units_use_private_lanes():
    librespot = (REPO / "deploy" / "systemd" / "librespot.service").read_text()
    assert "--device librespot_substream" in librespot
    assert "audio_topology.env" not in librespot
    assert "jasper_renderer_in" not in librespot

    bluealsa = (
        REPO / "deploy" / "systemd" / "bluealsa-aplay.service.d"
        / "jts-output.conf"
    ).read_text()
    assert "--pcm=bluealsa_substream" in bluealsa
    assert "audio_topology.env" not in bluealsa
    assert "jasper_renderer_in" not in bluealsa

    usbsink = (REPO / "deploy" / "systemd" / "jasper-usbsink.service").read_text()
    assert "JASPER_USBSINK_PLAYBACK_DEVICE=usbsink_substream" in usbsink
    assert "audio_topology.env" not in usbsink


def test_renderer_units_soft_depend_on_fanin():
    """Renderers should start after fan-in without being hard-coupled to
    its restart policy."""
    unit_paths = [
        REPO / "deploy" / "systemd" / "librespot.service",
        REPO / "deploy" / "systemd" / "shairport-sync.service",
        REPO / "deploy" / "systemd" / "jasper-usbsink.service",
        REPO / "deploy" / "systemd" / "bluealsa-aplay.service.d" / "jts-output.conf",
    ]
    for path in unit_paths:
        text = path.read_text()
        assert "After=" in text
        assert "jasper-fanin.service" in text


def test_shairport_template_keeps_renderer_placeholder():
    conf = (REPO / "deploy" / "shairport-sync.conf.template").read_text()
    assert 'output_device = "__RENDERER_DEVICE__"' in conf
    assert "output_rate = 44100" in conf


def test_install_writes_fanin_asound_conf_and_retires_switcher():
    install = (REPO / "deploy" / "install.sh").read_text()
    assert "jasper_asound_render_template" in install
    assert '"${ENV_DIR}/asoundrc.jasper.template"' in install
    assert "/usr/local/sbin/jasper-render-asound-conf" in install
    assert "ln -sfn /var/lib/jasper-asound/asound.conf /etc/asound.conf" in install
    assert "chmod 0644 /var/lib/jasper-asound/asound.conf" in install
    assert 'grep -q "shairport_substream" /etc/asound.conf' in install
    # install_renderers (which removes the retired switcher binary)
    # lives in the sourced deploy/lib/install/renderers.sh now.
    renderers_lib = (
        REPO / "deploy" / "lib" / "install" / "renderers.sh"
    ).read_text()
    assert "rm -f /usr/local/sbin/jasper-audio-topology" in renderers_lib
    assert "retire_audio_topology_switch" in install
    assert "systemctl enable jasper-camilla.service jasper-fanin.service" in install
    assert "/usr/local/sbin/jasper-audio-topology fanin" not in install
    assert "/usr/local/sbin/jasper-audio-topology fanin" not in renderers_lib


def test_snd_aloop_modprobe_pins_substreams_and_notify():
    conf = (REPO / "deploy" / "modprobe.d" / "snd-aloop.conf").read_text()
    assert "pcm_substreams=8" in conf
    assert "pcm_notify=0" in conf
