# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Config-shape tests for the fan-in audio topology.

These read deploy-time files directly and lock down the production
renderer graph:

    renderer/test private lanes -> jasper-fanin -> SHM ring

They do not exercise ALSA itself; hardware validation lives on the Pi
through jasper-doctor and the AirPlay/renderer smoke tests.
"""
from __future__ import annotations

import re
from pathlib import Path

from tests.install_surface import installer_text


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


def _line_value(text: str, key: str) -> str:
    prefix = f"{key}="
    for line in text.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):]
    return ""


def test_asoundrc_has_no_legacy_renderer_dmix():
    rc = _non_comment((REPO / "deploy" / "alsa" / "asoundrc.jasper").read_text())
    assert not re.search(r"^pcm\.jasper_renderer_mix\s*\{", rc, re.MULTILINE)
    assert not re.search(r"^pcm\.jasper_renderer_in\s*\{", rc, re.MULTILINE)


def test_asoundrc_declares_private_renderer_lanes():
    rc = _non_comment((REPO / "deploy" / "alsa" / "asoundrc.jasper").read_text())
    # No usbsink_substream: USB audio is DIRECT-captured by jasper-fanin from
    # hw:UAC2Gadget (the aloop solo write lane hw:Loopback,0,3 was removed
    # 2026-07-10). Pair 3's capture side is still read by fan-in as the usbsink
    # lane's idle fallback, but nothing writes the alias.
    aliases = {
        "librespot_substream": "hw:Loopback,0,0",
        "shairport_substream": "hw:Loopback,0,1",
        "bluealsa_substream": "hw:Loopback,0,2",
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


def test_asoundrc_declares_no_snd_aloop_central_hop():
    """ADR-0100: the SHM ring is fan-in's ONLY publish path.

    The summed program never reaches an snd-aloop substream, so the dsnoop tap
    CamillaDSP used to capture (``jasper_capture``) and its plug alias
    (``jasper_ref``) are DELETED, not merely unread. Re-declaring either would
    offer a second central transport to a graph that must have exactly one.
    """
    rc = _non_comment((REPO / "deploy" / "alsa" / "asoundrc.jasper").read_text())
    # Positive control FIRST: the assertions below are ABSENCES, so an empty or
    # comment-only read would satisfy all of them vacuously. Proving the reader
    # found the SURVIVING renderer ingress is what rules that out.
    assert 'pcm "hw:Loopback,0,0"' in _pcm_block(rc, "librespot_substream")
    for name in ("jasper_capture", "jasper_ref"):
        assert name not in rc, f"{name} was re-declared in asoundrc.jasper"
    # Nothing may claim substream 7 under any alias — the pair stays free.
    assert "Loopback,0,7" not in rc
    assert "Loopback,1,7" not in rc


def test_renderer_units_use_private_lanes():
    librespot = (REPO / "deploy" / "systemd" / "librespot.service").read_text()
    # Since U3 / P6a the device is an indirection so a per-box ring flip is one
    # env write rather than a unit edit — but the in-unit DEFAULT is still the
    # private aloop lane, which is what this test is actually about: a box with
    # no lane map writes librespot_substream, byte-identically to before.
    assert "--device ${JASPER_LIBRESPOT_DEVICE}" in librespot
    assert 'Environment="JASPER_LIBRESPOT_DEVICE=librespot_substream"' in librespot
    assert "audio_topology.env" not in librespot
    assert "jasper_renderer_in" not in librespot

    bluealsa = (
        REPO / "deploy" / "systemd" / "bluealsa-aplay.service.d"
        / "jts-output.conf"
    ).read_text()
    # Since U3/P6b the device is an indirection so a per-box ring flip is one
    # env write rather than a unit edit — but the in-unit DEFAULT is still the
    # private aloop lane, which is what this test is about: a box with no lane
    # map writes bluealsa_substream, byte-identically to before.
    assert "--pcm=${JASPER_BLUEALSA_DEVICE}" in bluealsa
    assert 'Environment="JASPER_BLUEALSA_DEVICE=bluealsa_substream"' in bluealsa
    assert "audio_topology.env" not in bluealsa
    assert "jasper_renderer_in" not in bluealsa

    # USB is NOT a loopback-writing renderer: the usbsink unit is a process-free
    # readiness marker and fan-in DIRECT-captures the gadget. Pin the deletion.
    usbsink = (REPO / "deploy" / "systemd" / "jasper-usbsink.service").read_text()
    assert "usbsink_substream" not in usbsink
    assert "JASPER_USBSINK_PLAYBACK_DEVICE" not in usbsink
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


def test_shairport_orders_after_outputd_for_live_latency_offset():
    """AirPlay's rendered offset should usually see outputd STATUS at boot.

    The renderer still falls back if outputd parks, but ordering shairport
    after outputd lets jasper-apply-airplay-mode use outputd's live
    snd_pcm_delay in the normal boot path.
    """
    text = (REPO / "deploy" / "systemd" / "shairport-sync.service").read_text()
    assert "jasper-outputd.service" in _line_value(text, "After")
    assert "jasper-outputd.service" in _line_value(text, "Wants")


def test_shairport_template_keeps_renderer_placeholder():
    conf = (REPO / "deploy" / "shairport-sync.conf.template").read_text()
    assert 'output_device = "__RENDERER_DEVICE__"' in conf
    assert "output_rate = 44100" in conf


def test_shairport_template_ships_two_ms_drift_tolerance():
    conf = (REPO / "deploy" / "shairport-sync.conf.template").read_text()
    assignments = re.findall(
        r"^\s*drift_tolerance_in_seconds\s*=\s*([0-9]+(?:\.[0-9]+)?);\s*$",
        conf,
        re.MULTILINE,
    )
    assert assignments == ["0.002"]


def test_install_writes_fanin_asound_conf_and_ships_no_switcher():
    install = installer_text()
    assert "jasper_asound_render_template" in install
    assert '"${ENV_DIR}/asoundrc.jasper.template"' in install
    assert "/usr/local/sbin/jasper-render-asound-conf" in install
    assert "ln -sfn /var/lib/jasper-asound/asound.conf /etc/asound.conf" in install
    assert "chmod 0644 /var/lib/jasper-asound/asound.conf" in install
    assert 'grep -q "shairport_substream" /etc/asound.conf' in install
    assert "rm -f /usr/local/sbin/jasper-audio-topology" in install
    assert "systemctl enable jasper-camilla.service jasper-fanin.service" in install
    assert "/usr/local/sbin/jasper-audio-topology fanin" not in install


def test_snd_aloop_modprobe_pins_substreams_and_notify():
    conf = (REPO / "deploy" / "modprobe.d" / "snd-aloop.conf").read_text()
    # snd_aloop caps pcm_substreams at 8 (pairs 0-7); a 9th is silently clamped.
    assert "pcm_substreams=8" in conf
    assert "pcm_notify=0" in conf
