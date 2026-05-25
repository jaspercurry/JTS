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
    """librespot's systemd unit must default-target jasper_renderer_in
    (the dmix-mode value). In Tier 2A's fanin mode the topology switch
    flips ${JASPER_LIBRESPOT_DEVICE} to librespot_substream via
    /var/lib/jasper/audio_topology.env — but the unit's Environment=
    default has to be jasper_renderer_in so the absence of that
    file (= fresh install) lands on dmix mode.

    Direct-loopback write was the EBUSY-crash-loop pattern fixed
    2026-05-22 (PR #214); the env-var indirection ships in the
    Tier 2A topology-switch work."""
    unit = (REPO / "deploy" / "systemd" / "librespot.service").read_text()
    # ExecStart must reference the env var, not the literal device.
    assert "--device ${JASPER_LIBRESPOT_DEVICE}" in unit, (
        "librespot ExecStart must use ${JASPER_LIBRESPOT_DEVICE} "
        "(the env var the topology switch flips) — not a literal "
        "device name. See deploy/bin/jasper-audio-topology."
    )
    # And the Environment= default must be jasper_renderer_in so that
    # the absence of /var/lib/jasper/audio_topology.env = dmix mode.
    assert 'Environment="JASPER_LIBRESPOT_DEVICE=jasper_renderer_in"' in unit, (
        "librespot.service must declare a default of "
        "JASPER_LIBRESPOT_DEVICE=jasper_renderer_in so dmix mode is "
        "the default behavior when audio_topology.env is absent."
    )
    # Defensive: the old direct-loopback path should NOT appear in an
    # active ExecStart. (Historical comments are fine; ExecStart lines
    # are checked explicitly.)
    for line in unit.splitlines():
        if line.strip().startswith("ExecStart=") and "/usr/bin/librespot" in line:
            assert "plughw:Loopback,0,0" not in line, (
                f"librespot ExecStart still references plughw:Loopback,0,0: {line}"
            )


def test_shairport_writes_to_renderer_mix():
    """shairport-sync's conf template uses the __RENDERER_DEVICE__
    placeholder, which jasper-apply-airplay-mode substitutes based
    on JASPER_AUDIO_TOPOLOGY. Default (dmix) substitutes
    `jasper_renderer_in`; fanin substitutes `shairport_substream`.

    Validating the *substitution* lives in tests/test_airplay_render.py
    (which actually invokes the render script). Here we just lock
    the template into the placeholder-based shape — a future edit
    that hard-codes the device name would break the topology switch."""
    conf = (REPO / "deploy" / "shairport-sync.conf.template").read_text()
    assert 'output_device = "__RENDERER_DEVICE__"' in conf, (
        "shairport conf template must use __RENDERER_DEVICE__ "
        "(substituted by jasper-apply-airplay-mode from "
        "JASPER_AUDIO_TOPOLOGY) — not a hard-coded device name."
    )
    # output_rate must stay 44100 — shairport rejects non-multiples
    # of 44100. The resampling happens in the plug layer above the
    # output device.
    assert "output_rate = 44100" in conf


def test_bluealsa_aplay_writes_to_renderer_mix():
    """bluealsa-aplay's drop-in unit must default-target
    jasper_renderer_in via ${JASPER_BLUEALSA_DEVICE} (same env-var
    indirection as librespot, for the topology switch). The drop-in
    clears the default ExecStart and re-sets it."""
    unit = (
        REPO / "deploy" / "systemd" / "bluealsa-aplay.service.d"
        / "jts-output.conf"
    ).read_text()
    assert "--pcm=${JASPER_BLUEALSA_DEVICE}" in unit, (
        "bluealsa-aplay drop-in must use ${JASPER_BLUEALSA_DEVICE} "
        "(env var the topology switch flips)"
    )
    assert 'Environment="JASPER_BLUEALSA_DEVICE=jasper_renderer_in"' in unit, (
        "bluealsa-aplay drop-in must declare a default of "
        "JASPER_BLUEALSA_DEVICE=jasper_renderer_in (= dmix mode)."
    )
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


def test_install_writes_asound_conf_to_system_wide_location():
    """PR #223 — the renderer ALSA PCM defs MUST land in a system-wide
    file (/etc/asound.conf, mode 0644) so non-root renderer users
    (shairport-sync, pi) can resolve user-space PCM names. The
    previous location (/root/.asoundrc, mode 0600) was visible only
    to root and broke AirPlay + Spotify Connect after PR #214. This
    test locks down the install destination so a future edit can't
    silently regress us back to a per-user file."""
    install = (REPO / "deploy" / "install.sh").read_text()
    # The destination must be /etc/asound.conf with mode 0644.
    assert "> /etc/asound.conf" in install, (
        "install.sh must redirect the rendered asoundrc to "
        "/etc/asound.conf (system-wide, readable by non-root "
        "renderer users)."
    )
    assert "chmod 0644 /etc/asound.conf" in install, (
        "install.sh must chmod 0644 /etc/asound.conf — non-root "
        "renderers need read access."
    )
    # And the pre-#223 destination must NOT be the active install
    # path (a backup/migration line is fine; that's how upgrades
    # work). Specifically, no `> /root/.asoundrc` redirect.
    for lineno, line in enumerate(install.splitlines(), 1):
        if line.strip().startswith("#"):
            continue
        assert "> /root/.asoundrc" not in line, (
            f"install.sh:{lineno} writes to /root/.asoundrc — that "
            "broke non-root renderer ALSA resolution (PR #223). "
            "Write to /etc/asound.conf instead."
        )


def test_asoundrc_declares_per_renderer_substream_aliases():
    """Phase 1 of Tier 2A (docs/HANDOFF-fan-in-daemon.md) — each
    renderer eventually gets its own snd-aloop substream pair, replacing
    the shared jasper_renderer_mix dmix.

    The aliases are defined NOW (additive) so the renderer service files
    can switch to them in a later phase. Today they're defined but
    nothing references them; tomorrow they replace the dmix front-end.
    """
    rc = (REPO / "deploy" / "alsa" / "asoundrc.jasper").read_text()
    aliases = {
        "librespot_substream": "hw:Loopback,0,0",
        "shairport_substream": "hw:Loopback,0,1",
        "bluealsa_substream": "hw:Loopback,0,2",
        "usbsink_substream": "hw:Loopback,0,3",
    }
    for alias_name, expected_slave in aliases.items():
        assert f"pcm.{alias_name}" in rc, (
            f"asoundrc.jasper missing pcm.{alias_name} alias "
            f"(Phase 1 of Tier 2A; see docs/HANDOFF-fan-in-daemon.md)"
        )
        # Locate the block and verify the slave is the expected substream.
        block_start = rc.index(f"pcm.{alias_name}")
        block_end = rc.find("}", block_start)
        block = rc[block_start:block_end]
        assert f'slave.pcm "{expected_slave}"' in block, (
            f"pcm.{alias_name} should slave to {expected_slave} per the "
            f"Tier 2A substream allocation in docs/HANDOFF-fan-in-daemon.md"
        )
        # Each alias is a `plug:` wrapper so each renderer's native
        # rate/format gets converted to the substream's 48 kHz S16_LE.
        assert "type plug" in block, (
            f"pcm.{alias_name} must be a `type plug` wrapper for "
            f"rate/format conversion (per Tier 2A design)"
        )


def test_per_renderer_substream_aliases_have_unique_substreams():
    """Each renderer alias must point at a distinct substream pair.
    Sharing a substream between renderers re-introduces the EBUSY
    contention that PR #214 fixed — defeats the whole point of
    Tier 2A's per-renderer assignment.
    """
    rc = (REPO / "deploy" / "alsa" / "asoundrc.jasper").read_text()
    aliases = [
        "librespot_substream",
        "shairport_substream",
        "bluealsa_substream",
        "usbsink_substream",
    ]
    slaves: dict[str, str] = {}
    for alias in aliases:
        block_start = rc.index(f"pcm.{alias}")
        block_end = rc.find("}", block_start)
        block = rc[block_start:block_end]
        # Pull the slave.pcm value.
        for line in block.splitlines():
            line = line.strip()
            if line.startswith("slave.pcm "):
                slaves[alias] = line.split('"')[1]
                break
    # All four must be defined and unique.
    assert len(slaves) == 4, f"missing slave declarations: {slaves}"
    seen: set[str] = set()
    for alias, slave in slaves.items():
        assert slave not in seen, (
            f"substream collision: {alias} shares {slave} with another "
            f"renderer alias — re-introduces EBUSY contention"
        )
        seen.add(slave)


def test_snd_aloop_modprobe_pins_pcm_notify_zero():
    """Phase 1 of Tier 2A pins pcm_notify=0 in the snd-aloop module
    options. With each renderer eventually owning its own substream
    pair, we don't want the capture side torn down when any single
    substream changes parameters.

    The default kernel value is already 0; pinning it is insurance
    against future default flips. See docs/HANDOFF-fan-in-daemon.md
    "snd-aloop module parameters" section.
    """
    conf = (
        REPO / "deploy" / "modprobe.d" / "snd-aloop.conf"
    ).read_text()
    assert "pcm_notify=0" in conf, (
        "deploy/modprobe.d/snd-aloop.conf must pin pcm_notify=0 "
        "per Tier 2A design (Phase 1)"
    )


def test_install_backs_up_pre_existing_etc_asound_conf():
    """Symmetric with the /root/.asoundrc migration: install.sh must
    back up any hand-edited or apt-installed `/etc/asound.conf` before
    overwriting. The grep guard makes the backup idempotent on
    re-deploys (skipped when our content is already in place).

    Without this, an operator's customised file would be silently
    replaced on first deploy of PR #223. Documented in CLAUDE.md's
    "Surgical changes — file ownership" rule.
    """
    install = (REPO / "deploy" / "install.sh").read_text()
    # The grep guard sentinel must reference jasper_renderer_in
    # specifically — that string is unique to our PR-#214+ wiring and
    # won't false-positive on any pre-JTS /etc/asound.conf.
    assert 'grep -q "jasper_renderer_in" /etc/asound.conf' in install, (
        "install.sh must grep for `jasper_renderer_in` as the "
        "sentinel for 'is this our /etc/asound.conf already?' to "
        "avoid backup spam on re-deploys."
    )
    # The backup itself must use the .pre-jasper.<unix-ts> naming
    # convention used elsewhere in install.sh so operators have one
    # rule to remember when cleaning up.
    assert '"/etc/asound.conf.pre-jasper.$(date +%s)"' in install, (
        "install.sh must back up /etc/asound.conf to "
        "/etc/asound.conf.pre-jasper.<unix-ts> matching the "
        "/root/.asoundrc migration pattern."
    )
    # Symlinks must be skipped — backing up a symlinked file and
    # then overwriting still mutates the target the symlink points
    # at. The /root/.asoundrc block uses `! -L` for the same reason;
    # we mirror it here.
    assert "! -L /etc/asound.conf" in install, (
        "install.sh must skip symlinked /etc/asound.conf in the "
        "backup branch (matches the /root/.asoundrc `! -L` guard)."
    )
