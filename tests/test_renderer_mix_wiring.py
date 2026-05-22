"""Tests that lock down the renderer-side dmix wiring added 2026-05-22.

The dmix `pcm.jasper_renderer_mix` (fronted by `pcm.jasper_renderer_in`)
sits between the renderers and `hw:Loopback,0,0` so librespot,
shairport-sync, and bluealsa-aplay can hold the device simultaneously.
Without these tests, a future config edit could silently revert one
of the renderer device strings to `plughw:Loopback,0,0`, re-introducing
the EBUSY contention class.

These are config-shape tests — they read the deploy/ files directly
and assert the expected substrings are present. They don't exercise
ALSA itself (that's hardware-only).
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_asoundrc_declares_renderer_dmix():
    """The renderer-side dmix and its plug front-end must be defined
    in deploy/alsa/asoundrc.jasper. install.sh sed-substitutes the
    __DONGLE_CARD__ placeholder and copies this file to /root/.asoundrc."""
    rc = (REPO / "deploy" / "alsa" / "asoundrc.jasper").read_text()
    # The dmix on hw:Loopback,0,0 — the actual multi-writer
    # convergence point.
    assert "pcm.jasper_renderer_mix" in rc
    assert "type dmix" in rc
    assert 'pcm "hw:Loopback,0,0"' in rc
    # The plug-wrapped front-end — what renderers actually reference.
    # Required so each renderer can write its native format/rate; the
    # plug layer downconverts to the dmix's fixed 48k S16_LE.
    assert "pcm.jasper_renderer_in" in rc
    assert 'slave.pcm "jasper_renderer_mix"' in rc


def test_asoundrc_renderer_dmix_uses_unique_ipc_key():
    """Each dmix on the system needs a unique ipc_key (shared-memory
    segment id). Reusing one across dmix definitions silently fails.
    Existing keys: 7777 (jasper_out), 7778 (jasper_capture's dsnoop).
    The renderer dmix uses 7779."""
    rc = (REPO / "deploy" / "alsa" / "asoundrc.jasper").read_text()
    # Walk the file: each occurrence of `ipc_key` should be one of the
    # three values, with no duplicates.
    keys = [
        line.strip().split()[-1]
        for line in rc.splitlines()
        if line.strip().startswith("ipc_key")
    ]
    assert sorted(keys) == ["7777", "7778", "7779"], (
        f"unexpected ipc_key set in asoundrc.jasper: {keys}"
    )


def test_librespot_writes_to_renderer_mix():
    """librespot's systemd unit must target jasper_renderer_in, not
    plughw:Loopback,0,0 directly. Direct-loopback write was the
    EBUSY-crash-loop pattern fixed 2026-05-22."""
    unit = (REPO / "deploy" / "systemd" / "librespot.service").read_text()
    assert "--device jasper_renderer_in" in unit
    # Defensive: the old path should NOT appear in an active ExecStart.
    # (Historical comments are fine; ExecStart lines are checked
    # explicitly.)
    exec_lines = [
        line for line in unit.splitlines()
        if line.strip().startswith("ExecStart=")
        and not line.strip().startswith("ExecStart=")  # noqa: E501
        is False
    ]
    for line in exec_lines:
        assert "plughw:Loopback,0,0" not in line, (
            f"librespot ExecStart still points at plughw:Loopback,0,0: {line}"
        )


def test_shairport_writes_to_renderer_mix():
    """shairport-sync's config template must target jasper_renderer_in.
    output_rate stays at 44100 (shairport-only constraint); the plug
    layer above the dmix handles the 44.1 -> 48 conversion."""
    conf = (REPO / "deploy" / "shairport-sync.conf.template").read_text()
    assert 'output_device = "jasper_renderer_in"' in conf
    # output_rate must stay 44100 — shairport rejects non-multiples
    # of 44100. The resampling happens in the plug layer.
    assert "output_rate = 44100" in conf


def test_bluealsa_aplay_writes_to_renderer_mix():
    """bluealsa-aplay's drop-in unit must target jasper_renderer_in.
    The drop-in clears the default ExecStart and re-sets it."""
    unit = (
        REPO / "deploy" / "systemd" / "bluealsa-aplay.service.d"
        / "jts-output.conf"
    ).read_text()
    assert "--pcm=jasper_renderer_in" in unit
    # Same defensive check: the active ExecStart should NOT point
    # at the bare loopback device.
    for line in unit.splitlines():
        if line.strip().startswith("ExecStart=") and "/usr/bin/bluealsa-aplay" in line:
            assert "plughw:Loopback,0,0" not in line, (
                f"bluealsa-aplay ExecStart still uses plughw:Loopback,0,0: {line}"
            )


def test_no_renderer_writes_directly_to_loopback():
    """Sanity rollup: across all three renderer config sources, the
    bare plughw:Loopback,0,0 should not appear in any ExecStart /
    output_device line. (It may appear in comments referencing the
    historical config — comments are excluded.)"""
    targets = [
        REPO / "deploy" / "systemd" / "librespot.service",
        REPO / "deploy" / "shairport-sync.conf.template",
        REPO / "deploy" / "systemd" / "bluealsa-aplay.service.d"
            / "jts-output.conf",
    ]
    for path in targets:
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.strip()
            # Skip comments. systemd unit comments start with `#`;
            # shairport-sync.conf comments start with `//`.
            if stripped.startswith("#") or stripped.startswith("//"):
                continue
            assert "plughw:Loopback,0,0" not in line, (
                f"{path.name}:{lineno} references plughw:Loopback,0,0 "
                f"outside a comment: {line!r}"
            )
