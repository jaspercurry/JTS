# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the jasper-doctor renderers domain."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from jasper.cli.doctor import _evidence, _shared, renderers
from jasper.cli.doctor.renderers import _classify_mux_mode
from jasper.music_sources import MUSIC_SOURCES

from .doctor_test_support import _grouping_cfg


def _seed_unit_states(**by_unit):
    """Seed the batched-roster evidence read so `evidence.unit_state(unit)`
    (and `unit_active`) answer from these fields without spawning
    `systemctl`. Missing fields default to falsy/None, matching a real
    `systemctl show` reply's absent keys."""
    fields = ("unit", "load_state", "active_state", "sub_state",
              "unit_file_state", "result", "n_restarts", "main_pid")
    states = {
        unit: {f: overrides.get(f) for f in fields} | {"unit": unit}
        for unit, overrides in by_unit.items()
    }
    _evidence.evidence.seed("units", states)


def _patch_shairport_conf(monkeypatch, conf_text: str, tmp_path: Path):
    """Read a synthetic shairport-sync.conf instead of the host file."""
    target = tmp_path / "shairport-sync.conf"
    target.write_text(conf_text)
    real_path_cls = Path

    def fake_path(arg):
        if arg == "/etc/shairport-sync.conf":
            return target
        return real_path_cls(arg)

    monkeypatch.setattr(renderers, "Path", fake_path)


def _seed_bt_agent_running():
    _seed_unit_states(
        **{"bt-agent.service": {"load_state": "loaded", "active_state": "active",
                                 "sub_state": "running"}},
    )


def test_check_bluetooth_pairing_policy_ok(monkeypatch):
    _seed_bt_agent_running()

    def fake_run(cmd, *args, **kwargs):
        if cmd[:4] == ["systemctl", "show", "bt-agent.service", "-p"]:
            return SimpleNamespace(
                returncode=0,
                stdout="ExecStart={ path=/opt/jasper/.venv/bin/jasper-bluetooth-agent ; }\n",
                stderr="",
            )
        if cmd == ["bluetoothctl", "show"]:
            return SimpleNamespace(
                returncode=0,
                stdout=("\tPowered: yes\n\tDiscoverable: no\n\tPairable: no\n"),
                stderr="",
            )
        raise AssertionError(cmd)

    monkeypatch.setattr(renderers, "_run", fake_run)

    r = renderers.check_bluetooth_pairing_policy()

    assert r.status == "ok"
    assert r.reason == ""


def test_check_bluetooth_pairing_policy_fails_old_agent(monkeypatch):
    _seed_bt_agent_running()

    def fake_run(cmd, *args, **kwargs):
        assert cmd[:4] == ["systemctl", "show", "bt-agent.service", "-p"]
        return SimpleNamespace(
            returncode=0,
            stdout="ExecStart={ path=/usr/bin/bt-agent ; }\n",
            stderr="",
        )

    monkeypatch.setattr(renderers, "_run", fake_run)

    r = renderers.check_bluetooth_pairing_policy()

    assert r.status == "fail"
    assert r.reason == renderers.REASON_BT_PAIRING_WRONG_AGENT


def test_check_bluetooth_pairing_policy_warns_pairable_outside_window(monkeypatch):
    _seed_bt_agent_running()

    def fake_run(cmd, *args, **kwargs):
        if cmd[:4] == ["systemctl", "show", "bt-agent.service", "-p"]:
            return SimpleNamespace(
                returncode=0,
                stdout="ExecStart={ path=/opt/jasper/.venv/bin/jasper-bluetooth-agent ; }\n",
                stderr="",
            )
        if cmd == ["bluetoothctl", "show"]:
            return SimpleNamespace(
                returncode=0,
                stdout=("\tPowered: yes\n\tDiscoverable: no\n\tPairable: yes\n"),
                stderr="",
            )
        raise AssertionError(cmd)

    monkeypatch.setattr(renderers, "_run", fake_run)

    r = renderers.check_bluetooth_pairing_policy()

    assert r.status == "warn"
    assert r.reason == renderers.REASON_BT_PAIRING_PAIRABLE_WITHOUT_DISCOVERABLE


def test_check_bluetooth_pairing_policy_warns_when_pairing_window_open(monkeypatch):
    _seed_bt_agent_running()

    def fake_run(cmd, *args, **kwargs):
        if cmd[:4] == ["systemctl", "show", "bt-agent.service", "-p"]:
            return SimpleNamespace(
                returncode=0,
                stdout="ExecStart={ path=/opt/jasper/.venv/bin/jasper-bluetooth-agent ; }\n",
                stderr="",
            )
        if cmd == ["bluetoothctl", "show"]:
            return SimpleNamespace(
                returncode=0,
                stdout=("\tPowered: yes\n\tDiscoverable: yes\n\tPairable: yes\n"),
                stderr="",
            )
        raise AssertionError(cmd)

    monkeypatch.setattr(renderers, "_run", fake_run)

    r = renderers.check_bluetooth_pairing_policy()

    assert r.status == "warn"
    assert r.reason == renderers.REASON_BT_PAIRING_WINDOW_OPEN


def _patch_lane_map(monkeypatch, tmp_path: Path, armed):
    """Pin the renderer-lane map the shairport conf check consults.

    ``armed=None`` means NO map file — the shipped fleet state. Anything
    else is rendered through the map's real writer-side text so the test
    reads exactly what production would. Without this pin the check would
    read the HOST's /var/lib/jasper map, making these tests answer
    differently on an armed box.
    """
    import jasper.renderer_lanes as rl

    map_path = tmp_path / "renderer_lanes.env"
    monkeypatch.setattr(rl, "RENDERER_LANES_ENV", str(map_path))
    if armed is not None:
        map_path.write_text(rl.render_env_text(tuple(armed)))


def test_shairport_check_substream_is_ok(monkeypatch, tmp_path):
    """Canonical fan-in wiring: AirPlay targets its private aloop lane —
    the unarmed/fleet default (no lane map exists)."""
    _patch_lane_map(monkeypatch, tmp_path, None)
    _patch_shairport_conf(
        monkeypatch,
        'alsa = {\n    output_device = "shairport_substream";\n};\n',
        tmp_path,
    )
    r = renderers.check_shairport_sync_loopback_plughw()
    assert r.status == "ok"
    assert r.reason == renderers.REASON_SHAIRPORT_ALOOP_UNARMED_OK


def test_shairport_check_ring_device_ok_when_armed(monkeypatch, tmp_path):
    """Armed lane + conf rendering the ring device = the coherent armed
    state (U3/P6d)."""
    _patch_lane_map(monkeypatch, tmp_path, ["airplay"])
    _patch_shairport_conf(
        monkeypatch,
        'alsa = {\n    output_device = "shairport_ring_lane";\n};\n',
        tmp_path,
    )
    r = renderers.check_shairport_sync_loopback_plughw()
    assert r.status == "ok"
    assert r.reason == renderers.REASON_SHAIRPORT_RING_ARMED_OK


def test_shairport_check_armed_but_conf_aloop_warns_restart(
    monkeypatch, tmp_path
):
    """ARMED lane but the conf still renders the aloop device: the unit has
    not restarted since the arm — the half-flip window the arm CLI's
    restart_required instruction exists to close. The conf is re-rendered
    at every unit start, so this disagreement means exactly that, and the
    detail must say so with the unit named."""
    _patch_lane_map(monkeypatch, tmp_path, ["airplay"])
    _patch_shairport_conf(
        monkeypatch,
        'alsa = {\n    output_device = "shairport_substream";\n};\n',
        tmp_path,
    )
    r = renderers.check_shairport_sync_loopback_plughw()
    assert r.status == "warn"
    assert r.reason == renderers.REASON_SHAIRPORT_ALOOP_ARMED_STALE


def test_shairport_check_ring_conf_but_disarmed_warns(monkeypatch, tmp_path):
    """Conf renders the ring device while the lane is NOT armed: the
    rollback half of the same window (disarm without restart). shairport
    would write a ring fan-in no longer reads — silence — so this warns
    with the restart remedy rather than counting as 'not recognized'."""
    _patch_lane_map(monkeypatch, tmp_path, [])
    _patch_shairport_conf(
        monkeypatch,
        'alsa = {\n    output_device = "shairport_ring_lane";\n};\n',
        tmp_path,
    )
    r = renderers.check_shairport_sync_loopback_plughw()
    assert r.status == "warn"
    assert r.reason == renderers.REASON_SHAIRPORT_RING_DISARMED_STALE


def test_shairport_check_jasper_renderer_in_fails(monkeypatch, tmp_path):
    """The retired renderer-dmix device is now a hard drift signal."""
    _patch_lane_map(monkeypatch, tmp_path, None)
    _patch_shairport_conf(
        monkeypatch,
        'alsa = {\n    output_device = "jasper_renderer_in";\n};\n',
        tmp_path,
    )
    r = renderers.check_shairport_sync_loopback_plughw()
    assert r.status == "fail"
    assert r.reason == renderers.REASON_SHAIRPORT_LEGACY_DMIX


def test_shairport_check_legacy_plughw_warns_with_redeploy_hint(
    monkeypatch,
    tmp_path,
):
    """Pre-PR-#214 wiring: output_device still points at the bare
    loopback. Doctor warns and tells the user to redeploy. This is
    the legacy-but-functional path, not a hard failure."""
    _patch_lane_map(monkeypatch, tmp_path, None)
    _patch_shairport_conf(
        monkeypatch,
        'alsa = {\n    output_device = "plughw:Loopback,0,0";\n};\n',
        tmp_path,
    )
    r = renderers.check_shairport_sync_loopback_plughw()
    assert r.status == "warn"
    assert r.reason == renderers.REASON_SHAIRPORT_LEGACY_PLUGHW


def test_shairport_check_raw_hw_loopback_fails(monkeypatch, tmp_path):
    """Raw `hw:Loopback,0,0` bypasses plug entirely. shairport requests
    44.1 kHz and snd-aloop is locked at 48 kHz → silent rejection.
    This is the hard-fail case."""
    _patch_lane_map(monkeypatch, tmp_path, None)
    _patch_shairport_conf(
        monkeypatch,
        'alsa = {\n    output_device = "hw:Loopback,0,0";\n};\n',
        tmp_path,
    )
    r = renderers.check_shairport_sync_loopback_plughw()
    assert r.status == "fail"
    assert r.reason == renderers.REASON_SHAIRPORT_RAW_HW_LOOPBACK


@pytest.mark.parametrize(
    "stale_device",
    ["jasper_renderer_in", "plughw:Loopback,0,0", "hw:Loopback,0,0"],
)
@pytest.mark.parametrize(
    ("armed", "expected_device", "forbidden_device"),
    [
        ([], "shairport_substream", "shairport_ring_lane"),
        (["airplay"], "shairport_ring_lane", "shairport_substream"),
    ],
    ids=["unarmed", "armed"],
)
def test_shairport_legacy_remediations_name_this_box_s_device(
    monkeypatch,
    tmp_path,
    stale_device,
    armed,
    expected_device,
    forbidden_device,
):
    """Every legacy branch must name the device the lane map resolves HERE.

    All three stale values predate the lane map, so they say nothing about
    whether the airplay lane is armed — the remediation has to consult the
    map like the coherent branches do. Hardcoding `shairport_substream`
    (as all three did before U4/P7-5) sends an armed box's operator to a
    device their box does not render, and a redeploy to that target would
    not converge anything.
    """
    _patch_lane_map(monkeypatch, tmp_path, armed)
    _patch_shairport_conf(
        monkeypatch,
        f'alsa = {{\n    output_device = "{stale_device}";\n}};\n',
        tmp_path,
    )

    r = renderers.check_shairport_sync_loopback_plughw()

    expected_reason = {
        "jasper_renderer_in": renderers.REASON_SHAIRPORT_LEGACY_DMIX,
        "plughw:Loopback,0,0": renderers.REASON_SHAIRPORT_LEGACY_PLUGHW,
        "hw:Loopback,0,0": renderers.REASON_SHAIRPORT_RAW_HW_LOOPBACK,
    }[stale_device]
    assert r.status in {"warn", "fail"}
    assert r.reason == expected_reason
    # The reason code is the same regardless of `armed` — the fact under
    # test here is that the remediation's NAMED DEVICE still consults the
    # lane map, which only `.detail` can pin.
    assert expected_device in r.detail
    assert forbidden_device not in r.detail


def test_shairport_check_missing_output_device_warns(monkeypatch, tmp_path):
    """A conf without an output_device line at all means shairport is
    using its own default — almost certainly wrong on this host."""
    _patch_shairport_conf(
        monkeypatch,
        "alsa = {\n    output_rate = 44100;\n};\n",
        tmp_path,
    )
    r = renderers.check_shairport_sync_loopback_plughw()
    assert r.status == "warn"
    assert r.reason == renderers.REASON_SHAIRPORT_NO_OUTPUT_DEVICE


def test_shairport_check_comments_ignored(monkeypatch, tmp_path):
    """// comments referencing plughw:Loopback (e.g. PR-history notes
    in the template) must not bait the check into reporting `ok` when
    the active line says something else."""
    conf = (
        "alsa = {\n"
        "    // Pre-2026-05-22 this was plughw:Loopback,0,0 directly\n"
        '    output_device = "shairport_substream";\n'
        "};\n"
    )
    _patch_shairport_conf(monkeypatch, conf, tmp_path)
    r = renderers.check_shairport_sync_loopback_plughw()
    assert r.status == "ok"


# ---- renderer ALSA device resolvable (PR #223 — the bug-class catch) ---

# These tests mock the parse helpers + the systemd-user lookup + the
# probe subprocess. They don't actually shell out — we're testing the
# orchestration, not aplay. The integration angle (does aplay actually
# open the device?) only meaningfully runs on the Pi via `jasper-doctor`.


def test_renderer_resolvable_all_ok(monkeypatch):
    """Happy path: every renderer has a discoverable device and the
    probe succeeds for each."""
    monkeypatch.setattr(
        renderers, "_renderer_device_shairport", lambda: "shairport_substream"
    )
    monkeypatch.setattr(
        renderers, "_renderer_device_librespot", lambda: "librespot_substream"
    )
    monkeypatch.setattr(
        renderers, "_renderer_device_bluealsa", lambda: "bluealsa_substream"
    )
    monkeypatch.setattr(
        renderers,
        "_systemd_unit_user",
        lambda unit: ({
            "shairport-sync.service": "shairport-sync",
            "librespot.service": "pi",
            "bluealsa-aplay.service": None,  # root
        }[unit], "loaded"),
    )
    monkeypatch.setattr(
        renderers,
        "_probe_open_as_user",
        lambda dev, user: (renderers.ProbeOutcome.OPENED, ""),
    )
    r = renderers.check_renderer_device_resolvable()
    assert r.status == "ok"
    assert r.reason == ""
    # This check's entire contract is disclosing WHICH renderer resolved to
    # WHICH device as WHICH user — a per-renderer summary the reason
    # vocabulary (one code for the whole check) cannot carry, so `.detail`
    # stays the pure-formatting-helper exception throughout this section.
    assert "shairport-sync(shairport-sync)→shairport_substream" in r.detail
    assert "librespot(pi)→librespot_substream" in r.detail
    assert "bluealsa-aplay(root)→bluealsa_substream" in r.detail


# Captured on jts3 (#3515) from the non-negotiable-#5 probe run against
# `librespot_ring_lane` while a live writer held it. The busy marker is the
# SECOND line: the ioplug refuses from `prepare`, which alsa-lib runs inside
# `snd_pcm_hw_params`, so aplay's hw_params dump lands AFTER it.
_BUSY_RING_LANE_STDERR = """\
event=jts_ring.writer.busy path=/dev/shm/jts-ring/lane-spotify.ring \
reason=writer_lock_held
ALSA lib pcm_jts_ring.c:383:(jts_ring_prepare) jts_ring: \
writer_open(/dev/shm/jts-ring/lane-spotify.ring) failed rc=-16 \
(ring already has a live writer — EBUSY)
aplay: set_params:1456: Unable to install hw params:
ACCESS:  RW_INTERLEAVED
FORMAT:  S16_LE
SUBFORMAT:  STD
SAMPLE_BITS: 16
FRAME_BITS: 32
CHANNELS: 2
RATE: 48000
PERIOD_TIME: (5333 5334)
PERIOD_SIZE: 256
PERIOD_BYTES: 1024
PERIODS: 16
BUFFER_TIME: (85333 85334)
BUFFER_SIZE: 4096
BUFFER_BYTES: 16384
TICK_TIME: 0
"""

# Same box, same command, a PCM name that resolves nowhere.
_UNKNOWN_PCM_STDERR = (
    "ALSA lib pcm.c:2722:(snd_pcm_open_noupdate) Unknown PCM jts_no_such_pcm\n"
    "aplay: main:850: audio open error: No such file or directory\n"
)

# Same box, same command, nothing holding the lane: the burst completed and
# aplay exited 0 on its own. 19 slots x 256 frames covers the 4800 written.
_IDLE_RING_LANE_STDERR = (
    "ALSA lib pcm_jts_ring.c:644:(jts_ring_close) jts_ring: closing "
    "published_slots=19 drop_no_reader=0 full_waits=0\n"
)

# The REAL shape on a JTS box: renderer units declare `Slice=jts-audio.slice`,
# so an owner's cgroup is NOT under /system.slice/ and nothing may assume it is.
_OWNED_CGROUP = "0::/jts.slice/jts-audio.slice/librespot.service\n"
_STRAY_CGROUP = "0::/user.slice/user-1000.slice/session-1461.scope\n"
# A same-named unit run by a per-user manager must not satisfy the gate.
_USER_MANAGER_CGROUP = (
    "0::/user.slice/user-1000.slice/user@1000.service/app.slice/"
    "librespot.service\n"
)


# Captured on jts3: aplay's own refusal for an snd-aloop substream that
# already has a writer. Aloop lanes refuse at `snd_pcm_open`, so this is the
# ONLY marker they produce — there is no ioplug line and no `rc=-16`.
_BUSY_ALOOP_STDERR = "aplay: main:850: audio open error: Device or resource busy\n"

# An ordinary alsa-lib warning, used to push the busy marker off both ends of
# the stderr so no fixed slice — head or tail — can classify.
_ALSA_CONFIG_WARNING = "ALSA lib conf.c:5205:(parse_args) Unknown parameter CARD\n"

_ALOOP_OWNER_PID = 4242


class _FakeProcPath:
    """Serves the two /proc reads the ownership gate makes: the aloop
    substream status (which names an owner pid) and that pid's cgroup."""

    def __init__(self, text):
        self._text = text

    def read_text(self):
        if isinstance(self._text, BaseException):
            raise self._text
        return self._text


_OUTCOME = renderers.ProbeOutcome
#: librespot's ARMED device (ring ingress) and its UNARMED one (snd-aloop).
#: An un-armed box — the fleet default, and jts3 today — resolves to the
#: latter, so both halves of the ownership gate are live and both are driven.
_RING = "librespot_ring_lane"
_ALOOP = "librespot_substream"

# Each row: the device the renderer resolves to; what the probe subprocess does
# — an (exit code, stderr) pair, or an exception it raises instead of running —
# the cgroup the lane's published owner pid resolves to; and the verdict and
# check status that must follow.
_PROBE_ROWS = [
    # A ring lane refused by its OWN renderer's writer lock: the probe
    # reached the lock, so the PCM resolved and opened.
    ("busy-owned", _RING, (1, _BUSY_RING_LANE_STDERR),
     _OWNED_CGROUP, _OUTCOME.BUSY, "ok"),
    # The ioplug's errno alone must carry the verdict: the `— EBUSY` gloss on
    # that line is a conditional suffix in the C, and nothing else prints it.
    (
        "ring-marker-without-the-ebusy-gloss", _RING,
        (
            1,
            "ALSA lib pcm_jts_ring.c:383:(jts_ring_prepare) jts_ring: "
            "writer_open(/dev/shm/jts-ring/lane-spotify.ring) failed rc=-16\n",
        ),
        _OWNED_CGROUP, _OUTCOME.BUSY, "ok",
    ),
    # Split across two lines, neither half complete: the two markers must be
    # required TOGETHER on ONE line, not merely both present somewhere.
    (
        "ring-marker-split-across-lines", _RING,
        (
            1,
            "ALSA lib pcm_jts_ring.c:383:(jts_ring_prepare) jts_ring: "
            "writer_open(\n/dev/shm/jts-ring/lane-spotify.ring) failed rc=-16\n",
        ),
        _OWNED_CGROUP, _OUTCOME.FAILED, "fail",
    ),
    # The ring's CAPTURE half refuses with the same errno and the same gloss
    # (`pcm_jts_ring.c:758`). It is not this playback probe bouncing off the
    # writer lock, so `rc=-16` alone must not carry a busy verdict.
    (
        "reader-open-refusal-is-not-our-busy", _RING,
        (
            1,
            "ALSA lib pcm_jts_ring.c:759:(jts_ring_capture_prepare) jts_ring: "
            "reader_open(/dev/shm/jts-ring/lane-spotify.ring) failed rc=-16 "
            "(ring already has a live reader — EBUSY)\n",
        ),
        _OWNED_CGROUP, _OUTCOME.FAILED, "fail",
    ),
    # The ownership gate is fail-closed: an unreadable cgroup proves nothing.
    (
        "cgroup-unreadable", _RING, (1, _BUSY_RING_LANE_STDERR),
        PermissionError(13, "Permission denied"), _OUTCOME.BUSY, "fail",
    ),
    # A same-named unit under a per-user manager is not the system unit.
    (
        "user-manager-impostor", _RING, (1, _BUSY_RING_LANE_STDERR),
        _USER_MANAGER_CGROUP, _OUTCOME.BUSY, "fail",
    ),
    # The SAME ioplug line with a different errno is NOT busy: `writer_open`
    # prints it for every refusal and only glosses -EBUSY, so a geometry shear
    # (-22) must stay a failure rather than borrow the ownership proof.
    (
        "ring-writer-open-failed-not-busy", _RING,
        (
            1,
            "ALSA lib pcm_jts_ring.c:383:(jts_ring_prepare) jts_ring: "
            "writer_open(/dev/shm/jts-ring/lane-spotify.ring) failed rc=-22\n",
        ),
        _OWNED_CGROUP, _OUTCOME.FAILED, "fail",
    ),
    # An UNARMED lane: aplay refuses at snd_pcm_open, so its own wording is the
    # only marker there is — no ioplug line, no rc=-16 — and the owner pid
    # comes from /proc/asound rather than the ring header.
    ("aloop-busy-owned", _ALOOP, (1, _BUSY_ALOOP_STDERR),
     _OWNED_CGROUP, _OUTCOME.BUSY, "ok"),
    # The PR #223 bug class stays a hard failure.
    ("unknown-pcm", _RING, (1, _UNKNOWN_PCM_STDERR),
     _OWNED_CGROUP, _OUTCOME.FAILED, "fail"),
    # Busy, but a stray process holds the ring — not the renderer.
    ("busy-stray", _RING, (1, _BUSY_RING_LANE_STDERR),
     _STRAY_CGROUP, _OUTCOME.BUSY, "fail"),
    # Busy on a device in NEITHER lane map: nothing can prove who holds it.
    ("busy-unknown-device", "some_other_pcm", (1, _BUSY_RING_LANE_STDERR),
     _OWNED_CGROUP, _OUTCOME.BUSY, "fail"),
    # The probe never executed. A live ring writer must not excuse that: the
    # header proves the RENDERER opened the lane, never that the probe
    # resolved it as the unit's User=.
    (
        "sudo-refused", _RING, (1, "sudo: a password is required\n"),
        _OWNED_CGROUP, _OUTCOME.FAILED, "fail",
    ),
    # One extra alsa-lib line must not defeat the Unknown-PCM failure:
    # nothing here may key on a fixed slice of stderr.
    (
        "unknown-pcm-plus-a-line", _RING,
        (1, _UNKNOWN_PCM_STDERR + _ALSA_CONFIG_WARNING),
        _OWNED_CGROUP, _OUTCOME.FAILED, "fail",
    ),
    # The same from the other end: a warning ABOVE the marker, a hw_params dump
    # below it. Neither a head slice nor a tail slice can classify this.
    (
        "busy-between-a-warning-and-the-dump", _RING,
        (1, _ALSA_CONFIG_WARNING + _BUSY_RING_LANE_STDERR),
        _OWNED_CGROUP, _OUTCOME.BUSY, "ok",
    ),
    # A slave open deeper in the chain reports its own -16 and busy text; the
    # PCM the probe asked for still did not resolve. Neither marker may match
    # on a bare substring, or this reads as a healthy busy lane.
    (
        "slave-busy-then-unknown-pcm", _RING,
        (
            1,
            "ALSA lib pcm_hw.c:1829:(snd_pcm_hw_open) open '/dev/snd/pcmC0D0p'"
            " failed (-16): Device or resource busy\n" + _UNKNOWN_PCM_STDERR,
        ),
        _OWNED_CGROUP, _OUTCOME.FAILED, "fail",
    ),
    # Nothing was contending: the burst completed and aplay exited 0 itself.
    # This is the ONLY success shape.
    (
        "idle-burst-completed", _RING, (0, _IDLE_RING_LANE_STDERR),
        _OWNED_CGROUP, _OUTCOME.OPENED, "ok",
    ),
    # Killed with the burst unfinished. Nothing completed, so nothing is
    # proven — a kill is a failure, not a success reached by outlasting a
    # timer.
    (
        "timeout-kill-no-marker", _RING, (124, ""),
        _OWNED_CGROUP, _OUTCOME.FAILED, "fail",
    ),
    # The marker outranks the exit code: the ioplug's SNDERR fires the instant
    # the lock wait expires, and the outer `timeout` can land anywhere in the
    # hw_params dump that follows, so a contended probe can carry BOTH.
    (
        "busy-and-timeout-killed", _RING, (124, _BUSY_RING_LANE_STDERR),
        _STRAY_CGROUP, _OUTCOME.BUSY, "fail",
    ),
    # `timeout`/sudo could not exec what it was given.
    (
        "exit-126", _RING, (126, "sudo: unable to execute /usr/bin/aplay\n"),
        _OWNED_CGROUP, _OUTCOME.FAILED, "fail",
    ),
    ("exit-127", _RING, (127, "aplay: command not found\n"),
     _OWNED_CGROUP, _OUTCOME.FAILED, "fail"),
    # The probe binary is missing, or the outer guard fired: nothing ran, so
    # nothing is proven, however healthy the lane looks.
    (
        "exec-missing", _RING, FileNotFoundError("timeout"),
        _OWNED_CGROUP, _OUTCOME.FAILED, "fail",
    ),
    (
        "outer-guard-fired", _RING,
        subprocess.TimeoutExpired(cmd=["timeout", "aplay"], timeout=4.0),
        _OWNED_CGROUP, _OUTCOME.FAILED, "fail",
    ),
    # A fork that cannot allocate on a 1 GB Pi must fail this renderer, not
    # escape as an exception and crash the whole check.
    (
        "fork-enomem", _RING, OSError(12, "Cannot allocate memory"),
        _OWNED_CGROUP, _OUTCOME.FAILED, "fail",
    ),
]


@pytest.mark.parametrize(
    ("device", "probe", "cgroup", "expect_outcome", "expect_status"),
    [row[1:] for row in _PROBE_ROWS],
    ids=[row[0] for row in _PROBE_ROWS],
)
def test_probe_verdict_and_check_status(
    monkeypatch, device, probe, cgroup, expect_outcome, expect_status
):
    """(probe argv, probe result, lane owner) -> verdict -> check status.

    Real aplay output captured on jts3, run through the real classifier and
    the real ownership gate; only the probe subprocess and the two /proc reads
    are stubbed. A busy verdict is what licenses the ownership check — never a
    substitute for a probe that did not open."""
    monkeypatch.setattr(renderers, "_renderer_device_shairport", lambda: None)
    monkeypatch.setattr(renderers, "_renderer_device_bluealsa", lambda: None)
    monkeypatch.setattr(
        renderers, "_renderer_device_librespot", lambda: device
    )
    monkeypatch.setattr(
        renderers, "_systemd_unit_user", lambda unit: ("pi", "loaded")
    )
    monkeypatch.setattr(
        renderers, "_resolve_systemd_env_vars", lambda dev, unit: dev
    )

    argv: list[list[str]] = []

    def fake_run(cmd, timeout=None):
        argv.append(cmd)
        if isinstance(probe, BaseException):
            raise probe
        returncode, stderr = probe
        return SimpleNamespace(returncode=returncode, stdout="", stderr=stderr)

    monkeypatch.setattr(renderers, "_run", fake_run)
    monkeypatch.setattr(
        "jasper.renderer_lanes.ring_writer_pid", lambda label: _ALOOP_OWNER_PID
    )
    # Aloop ownership reads through the evidence cache now (not `Path`); seed
    # every private-lane substream so whichever aloop device a row uses (only
    # `_ALOOP` today) finds its owner_pid.
    _evidence.evidence.seed(
        "loopback_substreams",
        {i: f"owner_pid : {_ALOOP_OWNER_PID}\n" for i in range(3)},
    )
    real_path = renderers.Path

    def fake_path(arg):
        if str(arg).startswith("/proc/"):
            return _FakeProcPath(cgroup)
        return real_path(arg)

    monkeypatch.setattr(renderers, "Path", fake_path)

    outcome, _ = renderers._probe_open_as_user(device, "pi")
    assert outcome is expect_outcome
    # Non-negotiable #5's command, pinned as a list rather than as source text.
    assert argv[0] == [
        "sudo", "-n", "-u", "pi",
        "env", "LC_ALL=C",
        "timeout", renderers._PROBE_TIMEOUT_SEC,
        "aplay", "-q",
        "-s", renderers._PROBE_FRAMES,
        "-D", device,
        "-c", "2", "-r", "48000", "-f", "S16_LE",
        "/dev/zero",
    ]
    assert renderers.check_renderer_device_resolvable().status == expect_status


def test_ownership_gate_refuses_an_empty_unit(monkeypatch):
    """`"/" in cgroup` matches everything, so an empty unit must fail closed.

    Unreachable through the check today (the unit names are module constants),
    which is why the guard needs its own pin. The cgroup read is stubbed with
    a cgroup that WOULD match: reading the host's real /proc would pass on a
    machine that has none, pinning nothing."""
    monkeypatch.setattr(
        renderers, "Path", lambda arg: _FakeProcPath(_OWNED_CGROUP)
    )
    owned, _ = renderers._cgroup_owner_is_unit(1, "")
    assert owned is False


@pytest.mark.parametrize(
    ("load_state", "expect_status"),
    [("not-found", "fail"), ("masked", "fail"), ("loaded", "ok")],
)
def test_absent_unit_is_not_a_root_probe(monkeypatch, load_state, expect_status):
    """An unloaded unit must fail, not silently degrade to a root probe.

    `systemctl show <unit> -p User` exits 0 with an EMPTY User= for a unit that
    does not exist, which is indistinguishable from the genuine root case
    (bluealsa-aplay really runs as root). Only LoadState separates them, so a
    renamed or uninstalled renderer would otherwise be probed as root and pass
    — the check claiming "as the unit's real User=" with no unit behind it."""
    monkeypatch.setattr(renderers, "_renderer_device_shairport", lambda: None)
    monkeypatch.setattr(renderers, "_renderer_device_bluealsa", lambda: None)
    monkeypatch.setattr(
        renderers, "_renderer_device_librespot", lambda: "librespot_ring_lane"
    )
    monkeypatch.setattr(
        renderers, "_resolve_systemd_env_vars", lambda dev, unit: dev
    )
    monkeypatch.setattr(
        renderers, "_systemd_unit_user", lambda unit: (None, load_state)
    )
    probed: list[list[str]] = []

    def fake_run(cmd, timeout=None):
        probed.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(renderers, "_run", fake_run)
    assert renderers.check_renderer_device_resolvable().status == expect_status
    # An unloaded unit must not even be probed.
    assert bool(probed) is (load_state == "loaded")


def test_renderer_resolvable_catches_pr214_regression(monkeypatch):
    """The exact bug PR #223 fixes: configs look right, services look
    active, but shairport-sync's runtime user can't open the device.
    Pre-#223 the doctor missed this entirely. This test pins that the
    new check would have caught it."""
    monkeypatch.setattr(
        renderers, "_renderer_device_shairport", lambda: "shairport_substream"
    )
    monkeypatch.setattr(
        renderers, "_renderer_device_librespot", lambda: "librespot_substream"
    )
    monkeypatch.setattr(
        renderers, "_renderer_device_bluealsa", lambda: "bluealsa_substream"
    )
    monkeypatch.setattr(
        renderers,
        "_systemd_unit_user",
        lambda unit: ({
            "shairport-sync.service": "shairport-sync",
            "librespot.service": "pi",
            "bluealsa-aplay.service": None,
        }[unit], "loaded"),
    )

    # Simulate the bug: as shairport-sync user, the open fails with
    # the canonical "Unknown PCM" pattern. Root + pi (somehow) succeed
    # — only shairport-sync fails. Doctor must still fail-the-check.
    def fake_probe(dev, user):
        if user == "shairport-sync":
            return (
                renderers.ProbeOutcome.FAILED,
                "ALSA lib pcm.c:2722: Unknown PCM shairport_substream",
            )
        return (renderers.ProbeOutcome.OPENED, "")

    monkeypatch.setattr(renderers, "_probe_open_as_user", fake_probe)

    r = renderers.check_renderer_device_resolvable()
    assert r.status == "fail"
    assert r.reason == renderers.REASON_RENDERER_DEVICE_UNRESOLVABLE
    assert "shairport-sync" in r.detail
    assert "Unknown PCM" in r.detail
    # The actionable hint should mention the fix path.
    assert "/etc/asound.conf" in r.detail


def test_renderer_resolvable_fail_includes_user_in_detail(monkeypatch):
    """Failure details must name the failing user — that's the key
    diagnostic for any "device works as root, fails as non-root" bug
    of which the PR #214 regression is the canonical example."""
    monkeypatch.setattr(
        renderers, "_renderer_device_shairport", lambda: "weird-device"
    )
    monkeypatch.setattr(renderers, "_renderer_device_librespot", lambda: None)
    monkeypatch.setattr(renderers, "_renderer_device_bluealsa", lambda: None)
    monkeypatch.setattr(
        renderers,
        "_systemd_unit_user",
        lambda unit: ("shairport-sync", "loaded"),
    )
    monkeypatch.setattr(
        renderers,
        "_probe_open_as_user",
        lambda d, u: (renderers.ProbeOutcome.FAILED, "open failed"),
    )
    r = renderers.check_renderer_device_resolvable()
    assert r.status == "fail"
    assert r.reason == renderers.REASON_RENDERER_DEVICE_UNRESOLVABLE
    assert "(shairport-sync)" in r.detail


def test_renderer_resolvable_skips_missing_renderers(monkeypatch):
    """A stripped image without all renderers installed should
    `ok` for what works, `warn` only if nothing was probeable."""
    monkeypatch.setattr(
        renderers, "_renderer_device_shairport", lambda: "shairport_substream"
    )
    monkeypatch.setattr(renderers, "_renderer_device_librespot", lambda: None)
    monkeypatch.setattr(renderers, "_renderer_device_bluealsa", lambda: None)
    monkeypatch.setattr(
        renderers,
        "_systemd_unit_user",
        lambda unit: ("shairport-sync", "loaded"),
    )
    monkeypatch.setattr(
        renderers,
        "_probe_open_as_user",
        lambda d, u: (renderers.ProbeOutcome.OPENED, ""),
    )
    r = renderers.check_renderer_device_resolvable()
    assert r.status == "ok"
    assert r.reason == ""
    assert "shairport-sync" in r.detail
    # Skipped renderers should be mentioned (informational).
    assert "skipped" in r.detail.lower()


def test_renderer_resolvable_no_renderers_at_all_is_warn(monkeypatch):
    """If literally nothing is configured, no audio path exists —
    surface as warn, not fail (could be a doctor-only image)."""
    monkeypatch.setattr(renderers, "_renderer_device_shairport", lambda: None)
    monkeypatch.setattr(renderers, "_renderer_device_librespot", lambda: None)
    monkeypatch.setattr(renderers, "_renderer_device_bluealsa", lambda: None)
    r = renderers.check_renderer_device_resolvable()
    assert r.status == "warn"
    assert r.reason == renderers.REASON_RENDERER_NONE_CONFIGURED


def test_renderer_resolvable_expands_systemd_env_vars(monkeypatch):
    """Operator overrides can still use `${VAR}` device indirection.
    The doctor's check must resolve those env vars via `systemctl show
    -p Environment` before probing, otherwise it false-positives with
    'Unknown PCM ${JASPER_LIBRESPOT_DEVICE}'."""
    monkeypatch.setattr(
        renderers, "_renderer_device_shairport", lambda: "shairport_substream"
    )  # already literal
    monkeypatch.setattr(
        renderers,
        "_renderer_device_librespot",
        lambda: "${JASPER_LIBRESPOT_DEVICE}",
    )
    monkeypatch.setattr(
        renderers,
        "_renderer_device_bluealsa",
        lambda: "${JASPER_BLUEALSA_DEVICE}",
    )
    monkeypatch.setattr(
        renderers,
        "_systemd_unit_user",
        lambda unit: ({
            "shairport-sync.service": "shairport-sync",
            "librespot.service": "pi",
            "bluealsa-aplay.service": None,
        }[unit], "loaded"),
    )

    # Mock _resolve_systemd_env_vars to simulate systemd returning
    # operator-supplied fan-in lane names.
    def fake_resolve(device, unit):
        env = {
            "librespot.service": {
                "JASPER_LIBRESPOT_DEVICE": "librespot_substream",
            },
            "bluealsa-aplay.service": {
                "JASPER_BLUEALSA_DEVICE": "bluealsa_substream",
            },
        }.get(unit, {})
        import re

        return re.sub(
            r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}",
            lambda m: env.get(m.group(1), m.group(0)),
            device,
        )

    monkeypatch.setattr(renderers, "_resolve_systemd_env_vars", fake_resolve)

    # Probe sees the RESOLVED device — record what it gets called with.
    received: list[str] = []

    def fake_probe(device, user):
        received.append(device)
        return (renderers.ProbeOutcome.OPENED, "")

    monkeypatch.setattr(renderers, "_probe_open_as_user", fake_probe)

    r = renderers.check_renderer_device_resolvable()
    assert r.status == "ok"
    assert r.reason == ""
    # Probe must have been called with the RESOLVED value, not the
    # literal ${VAR} string.
    assert "librespot_substream" in received
    assert "bluealsa_substream" in received
    assert "${JASPER_LIBRESPOT_DEVICE}" not in received
    assert "${JASPER_BLUEALSA_DEVICE}" not in received
    # Detail should show both literal and resolved when they differ,
    # so the operator can see env-var resolution at a glance.
    assert "from ${JASPER_LIBRESPOT_DEVICE}" in r.detail
    assert "from ${JASPER_BLUEALSA_DEVICE}" in r.detail
    # And the shairport literal (no `${`) is shown unchanged.
    assert "(shairport-sync)→shairport_substream" in r.detail
    assert "(from " not in r.detail.split("shairport-sync(")[1].split(";")[0]


def test_resolve_systemd_env_vars_no_op_when_no_placeholder():
    """Strings without ${VAR} pass through unchanged — avoids the
    subprocess call entirely."""
    assert (
        renderers._resolve_systemd_env_vars("librespot_substream", "librespot.service")
        == "librespot_substream"
    )
    assert (
        renderers._resolve_systemd_env_vars("hw:Loopback,0,0", "any.service")
        == "hw:Loopback,0,0"
    )


def test_resolve_systemd_env_vars_returns_original_on_failure(monkeypatch):
    """If systemctl is unavailable / errors, return the original
    string unchanged. The caller's aplay probe will then fail with
    a clear 'Unknown PCM ${VAR}' message — explicit failure beats
    silent wrong-value substitution."""
    import subprocess as sp

    def fake_run(*args, **kwargs):
        raise FileNotFoundError("systemctl missing")

    monkeypatch.setattr(sp, "run", fake_run)
    # The function should swallow the error and return the input.
    assert (
        renderers._resolve_systemd_env_vars(
            "${JASPER_LIBRESPOT_DEVICE}", "librespot.service"
        )
        == "${JASPER_LIBRESPOT_DEVICE}"
    )


# ---- renderer device parsers ----------------------------------------


def test_parse_shairport_device_from_conf(tmp_path, monkeypatch):
    """shairport-sync.conf uses libconfig syntax. Parser must handle
    double quotes, leading whitespace, and ignore // comments."""
    conf = tmp_path / "shairport-sync.conf"
    conf.write_text(
        "alsa = {\n"
        "    // Pre-2026-05-23 this was plughw:Loopback,0,0\n"
        '    output_device = "shairport_substream";\n'
        "};\n"
    )
    real_path_cls = Path

    def fake_path(arg):
        if arg == "/etc/shairport-sync.conf":
            return conf
        return real_path_cls(arg)

    monkeypatch.setattr(renderers, "Path", fake_path)
    assert renderers._renderer_device_shairport() == "shairport_substream"


def test_parse_librespot_device_from_systemd_unit(tmp_path, monkeypatch):
    """librespot.service has a multi-line ExecStart= with backslash
    continuations. Parser must handle line joining and grab --device."""
    unit = tmp_path / "librespot.service"
    unit.write_text(
        "[Service]\n"
        "ExecStart=/usr/bin/librespot \\\n"
        "    --name JTS \\\n"
        "    --backend alsa \\\n"
        "    --device librespot_substream \\\n"
        "    --format S24_3\n"
    )
    real_path_cls = Path

    def fake_path(arg):
        if arg == "/etc/systemd/system/librespot.service":
            return unit
        return real_path_cls(arg)

    monkeypatch.setattr(renderers, "Path", fake_path)
    assert renderers._renderer_device_librespot() == "librespot_substream"


def test_parse_bluealsa_device_from_dropin(tmp_path, monkeypatch):
    """bluealsa-aplay's device is configured via a drop-in's --pcm= flag."""
    dropin_dir = tmp_path / "bluealsa-aplay.service.d"
    dropin_dir.mkdir()
    dropin = dropin_dir / "jts-output.conf"
    dropin.write_text(
        "[Service]\n"
        "ExecStart=\n"
        "ExecStart=/usr/bin/bluealsa-aplay -S --pcm=bluealsa_substream\n"
    )
    real_path_cls = Path

    def fake_path(arg):
        if arg == "/etc/systemd/system/bluealsa-aplay.service.d/jts-output.conf":
            return dropin
        # The other candidate (override.conf) should not exist for this test.
        if arg == "/etc/systemd/system/bluealsa-aplay.service.d/override.conf":
            return tmp_path / "does-not-exist"
        return real_path_cls(arg)

    monkeypatch.setattr(renderers, "Path", fake_path)
    assert renderers._renderer_device_bluealsa() == "bluealsa_substream"


def test_renderer_checks_read_parked_on_bonded_follower(monkeypatch):
    """The dumb-follower profile deliberately stops the renderer stack —
    every liveness check for a parked unit must read ok/'parked', never
    fail against intended state. Driven through the real shared
    predicate with only the grouping config patched."""
    import jasper.multiroom.config as mr_config
    monkeypatch.setattr(
        mr_config,
        "load_config",
        lambda *a, **k: _grouping_cfg(
            enabled=True,
            role="follower",
            channel="right",
            bond_id="b",
            leader_addr="jts.local",
        ),
    )
    checks = [
        lambda: renderers.check_librespot_running(None),
        renderers.check_shairport_sync_ap2,
        renderers.check_nqptp_running,
        renderers.check_jasper_mux,
        renderers.check_bluealsa,
    ]
    for check in checks:
        r = check()
        assert r.status == "ok", r
        assert r.reason == _shared.REASON_PARKED_BONDED_FOLLOWER


def test_renderer_checks_probe_normally_when_solo(monkeypatch):
    """The parked skip must vanish on a solo speaker — a dead librespot
    is a real failure there. (Fail-open contract of the predicate.)"""
    import jasper.multiroom.config as mr_config
    monkeypatch.setattr(
        mr_config,
        "load_config",
        lambda *a, **k: _grouping_cfg(enabled=False),
    )
    monkeypatch.setattr(renderers.os.path, "isfile", lambda p: False)
    r = renderers.check_librespot_running(None)
    assert r.status == "fail"  # binary missing probes through, no skip


def test_renderer_checks_treat_household_source_off_as_healthy(monkeypatch):
    """Intentional Off is desired state, not a dead-renderer incident."""
    from jasper.source_intent import BluetoothRfkillState

    monkeypatch.setattr(renderers, "_parked_follower_result", lambda _label: None)
    monkeypatch.setattr(renderers, "source_intent_enabled", lambda source: False)
    _seed_unit_states(**{
        "librespot.service": {"active_state": "inactive"},
        "shairport-sync.service": {"active_state": "inactive"},
        "nqptp.service": {"active_state": "inactive"},
        "bluealsa.service": {"active_state": "inactive"},
        "bluealsa-aplay.service": {"active_state": "inactive"},
        "bt-agent.service": {"active_state": "inactive"},
    })
    monkeypatch.setattr(
        renderers,
        "_run",
        lambda cmd: SimpleNamespace(returncode=3, stdout="Powered: no\n", stderr=""),
    )
    monkeypatch.setattr(
        renderers,
        "read_bluetooth_rfkill_state",
        lambda: BluetoothRfkillState(True, True, False),
    )

    checks = (
        lambda: renderers.check_librespot_running(None),
        renderers.check_shairport_sync_ap2,
        renderers.check_bluealsa,
        renderers.check_bluetooth_pairing_policy,
    )
    for check in checks:
        result = check()
        assert result.status == "ok", result
        assert result.reason == renderers.REASON_SOURCE_OFF


def test_renderer_check_fails_when_household_off_runtime_is_active(monkeypatch):
    monkeypatch.setattr(renderers, "_parked_follower_result", lambda _label: None)
    monkeypatch.setattr(renderers, "source_intent_enabled", lambda source: False)
    _seed_unit_states(**{"librespot.service": {"active_state": "active"}})
    result = renderers.check_librespot_running(None)

    assert result.status == "fail"
    assert result.reason == renderers.REASON_SOURCE_OFF_DRIFT


def test_renderer_check_fails_loud_on_invalid_source_intent(monkeypatch):
    monkeypatch.setattr(renderers, "_parked_follower_result", lambda _label: None)

    def invalid(_source):
        raise RuntimeError("bad source intent")

    monkeypatch.setattr(renderers, "source_intent_enabled", invalid)
    result = renderers.check_bluealsa()

    assert result.status == "fail"
    assert result.reason == renderers.REASON_SOURCE_INTENT_INVALID


def test_bluealsa_desired_on_fails_when_radio_is_blocked_or_powered_off(
    monkeypatch,
):
    from jasper.source_intent import BluetoothRfkillState

    monkeypatch.setattr(renderers, "_parked_follower_result", lambda _label: None)
    monkeypatch.setattr(renderers, "source_intent_enabled", lambda _source: True)
    monkeypatch.setattr(
        renderers,
        "read_bluetooth_rfkill_state",
        lambda: BluetoothRfkillState(True, True, False),
    )
    monkeypatch.setattr(
        renderers,
        "_run",
        lambda cmd: SimpleNamespace(
            returncode=0,
            stdout="Powered: no\n",
            stderr="",
        ),
    )

    result = renderers.check_bluealsa()

    assert result.status == "fail"
    assert result.reason == renderers.REASON_BLUETOOTH_RADIO_NOT_READY


def test_bluealsa_desired_on_proves_radio_and_units(monkeypatch):
    from jasper.source_intent import BluetoothRfkillState

    monkeypatch.setattr(renderers, "_parked_follower_result", lambda _label: None)
    monkeypatch.setattr(renderers, "source_intent_enabled", lambda _source: True)
    monkeypatch.setattr(
        renderers,
        "read_bluetooth_rfkill_state",
        lambda: BluetoothRfkillState(True, False, False),
    )
    _seed_unit_states(**{
        "bluealsa.service": {"active_state": "active"},
        "bluealsa-aplay.service": {"active_state": "active"},
    })
    monkeypatch.setattr(
        renderers, "_run",
        lambda cmd: SimpleNamespace(returncode=0, stdout="Powered: yes\n", stderr=""),
    )

    result = renderers.check_bluealsa()

    assert result.status == "ok"
    assert result.reason == ""


def test_bluealsa_desired_on_fails_when_rfkill_is_unreadable(monkeypatch):
    monkeypatch.setattr(renderers, "_parked_follower_result", lambda _label: None)
    monkeypatch.setattr(renderers, "source_intent_enabled", lambda _source: True)

    def unreadable_rfkill():
        raise RuntimeError("rfkill unavailable")

    monkeypatch.setattr(renderers, "read_bluetooth_rfkill_state", unreadable_rfkill)

    result = renderers.check_bluealsa()

    assert result.status == "fail"
    assert result.reason == renderers.REASON_BLUETOOTH_RADIO_UNVERIFIABLE


def test_voice_aec_checks_read_parked_on_bonded_follower(monkeypatch):
    """PR-B: voice + the AEC stack park on a bonded follower — the
    bridge/mic liveness checks must read parked, never fail against
    intended state."""
    import jasper.multiroom.config as mr_config
    from jasper.cli.doctor import aec as adoc
    from jasper.cli.doctor import audio as audoc

    monkeypatch.setattr(
        mr_config,
        "load_config",
        lambda *a, **k: _grouping_cfg(
            enabled=True,
            role="follower",
            channel="right",
            bond_id="b",
            leader_addr="jts.local",
        ),
    )
    checks = [
        adoc.check_aec_bridge_running,
        adoc.check_aec_bridge_output_health,
        adoc.check_aec_bridge_dtln_engine,
        adoc.check_chip_aec_alignment,
        adoc.check_audio_profile_runtime,
        lambda: audoc.check_mic_card_matches_config(None),
        lambda: audoc.check_mic_capture(None),
        # Caught LIVE by the first on-pair doctor run after PR-B
        # deployed: these three probed parked units and read fail/warn
        # against intended state.
        renderers.check_bluetooth_pairing_policy,
        lambda: renderers.check_spotify_connect_device(None),
    ]
    for check in checks:
        r = check()
        assert r.status == "ok", r
        # Each domain module owns its own closed reason vocabulary, but all
        # converged on the identical value for this shared fact.
        assert r.reason == "parked_bonded_follower"


# ---------------------------------------------------------------------------
# U3 / P6a — the renderer device is a ${VAR}, and `systemctl show` cannot
# resolve it. These pin the surfaces that CAN.
# ---------------------------------------------------------------------------


def test_resolve_device_prefers_the_lane_map_over_systemctl_show(monkeypatch, tmp_path):
    """`systemctl show -p Environment` returns ONLY `Environment=` directives —
    never `EnvironmentFile=` layers, which is where every JTS runtime override
    lives (established empirically on an armed box 2026-07-02; re-confirmed on
    jts.local during the P6a review).

    So on an ARMED box `systemctl show` reports the in-unit aloop DEFAULT while
    the renderer is really writing its ring device. Trusting it would make the
    doctor probe the wrong PCM and call an unprobed ring lane healthy. The lane
    map — the SSOT that wrote the override — must win.
    """
    from jasper import renderer_lanes as rl
    lanes = str(tmp_path / "renderer_lanes.env")
    rl.render_renderer_lanes_env(("spotify",), path=lanes)
    monkeypatch.setattr(rl, "RENDERER_LANES_ENV", lanes)

    # Exactly what a real armed box reports: the stale in-unit default.
    def fake_run(cmd, **kwargs):
        class R:
            returncode = 0
            stdout = "JASPER_LIBRESPOT_DEVICE=librespot_substream"

        return R()

    monkeypatch.setattr(renderers.subprocess, "run", fake_run)
    monkeypatch.setattr(renderers, "_unit_runtime_environ", lambda unit: {})

    resolved = renderers._resolve_systemd_env_vars(
        "${JASPER_LIBRESPOT_DEVICE}", "librespot.service"
    )
    assert resolved == "librespot_ring_lane", (
        "an armed box must resolve to its RING device; resolving to the "
        "systemctl-visible aloop default would probe the wrong PCM"
    )


def test_resolve_device_falls_back_to_proc_environ(monkeypatch, tmp_path):
    """With no lane map (an operator override, or a box predating it), the
    running daemon's own `/proc/<MainPID>/environ` is the next-best surface —
    the arm.sh precedent — and it still beats `systemctl show`."""
    from jasper import renderer_lanes as rl
    monkeypatch.setattr(rl, "RENDERER_LANES_ENV", str(tmp_path / "absent.env"))

    def fake_run(cmd, **kwargs):
        class R:
            returncode = 0
            stdout = "JASPER_LIBRESPOT_DEVICE=librespot_substream"

        return R()

    monkeypatch.setattr(renderers.subprocess, "run", fake_run)
    monkeypatch.setattr(
        renderers,
        "_unit_runtime_environ",
        lambda unit: {"JASPER_LIBRESPOT_DEVICE": "operator_override_pcm"},
    )

    assert (
        renderers._resolve_systemd_env_vars(
            "${JASPER_LIBRESPOT_DEVICE}", "librespot.service"
        )
        == "operator_override_pcm"
    )


def test_unarmed_box_resolves_to_the_shipped_aloop_device(monkeypatch, tmp_path):
    """The shipped fleet state: no lane map, no override — the in-unit default
    is what the renderer writes and what the probe must open."""
    from jasper import renderer_lanes as rl
    monkeypatch.setattr(rl, "RENDERER_LANES_ENV", str(tmp_path / "absent.env"))

    def fake_run(cmd, **kwargs):
        class R:
            returncode = 0
            stdout = "JASPER_LIBRESPOT_DEVICE=librespot_substream"

        return R()

    monkeypatch.setattr(renderers.subprocess, "run", fake_run)
    monkeypatch.setattr(renderers, "_unit_runtime_environ", lambda unit: {})

    assert (
        renderers._resolve_systemd_env_vars(
            "${JASPER_LIBRESPOT_DEVICE}", "librespot.service"
        )
        == "librespot_substream"
    )


def test_ring_lane_ebusy_owner_is_read_from_the_ring_header(monkeypatch, tmp_path):
    """An EBUSY on a ring lane proves the PCM resolved AND that someone holds
    it; this proves it is the RIGHT someone, from the pid the ring itself
    published. Reachable only because the resolver above now returns the ring
    device on an armed box.
    """
    from jasper import renderer_lanes as rl
    # No monkeypatch of the device map: it is DERIVED from RENDERER_LANES now,
    # so the real registry is the right input and each new lane is covered here
    # for free. (P6a hand-listed it, which meant a second lane would silently
    # miss the EBUSY-owner path and fail a healthy box.)
    assert renderers._ring_renderer_devices()["librespot_ring_lane"] == "spotify"
    monkeypatch.setattr(rl, "ring_writer_pid", lambda label: 4242)

    cgroup = tmp_path / "cgroup"
    cgroup.write_text("0::/system.slice/librespot.service\n")
    real_path = renderers.Path

    def fake_path(p):
        if str(p) == "/proc/4242/cgroup":
            return cgroup
        return real_path(p)

    monkeypatch.setattr(renderers, "Path", fake_path)

    owned, detail = renderers._fanin_lane_busy_owner_matches(
        "librespot_ring_lane", "librespot.service"
    )
    assert owned, detail
    assert "4242" in detail and "ring writer" in detail

    # A DIFFERENT unit holding the ring is NOT accepted — that is a stray
    # writer in the music path, which is the whole reason the guard exists.
    other = tmp_path / "other"
    other.write_text("0::/system.slice/some-other.service\n")

    def fake_path_other(p):
        if str(p) == "/proc/4242/cgroup":
            return other
        return real_path(p)

    monkeypatch.setattr(renderers, "Path", fake_path_other)
    owned, detail = renderers._fanin_lane_busy_owner_matches(
        "librespot_ring_lane", "librespot.service"
    )
    assert not owned
    assert "4242" in detail


def test_unit_runtime_environ_parses_real_nul_delimited_bytes(monkeypatch, tmp_path):
    """Execute the /proc tier end to end on real bytes.

    This is the tier the B1 fix EXISTS for — `systemctl show` cannot see
    `EnvironmentFile=` and the lane map only knows lanes it wrote — and it was
    the one mutation survivor: every other path was covered by a monkeypatched
    stand-in. Feed it a genuine NUL-delimited environ blob.
    """
    environ = tmp_path / "environ"
    environ.write_bytes(
        b"PATH=/usr/bin\x00JASPER_LIBRESPOT_DEVICE=librespot_ring_lane\x00"
        b"EMPTY=\x00NOEQUALS\x00LANG=C.UTF-8\x00"
    )

    def fake_run(cmd, **kwargs):
        class R:
            returncode = 0
            stdout = "4242\n"
        return R()

    monkeypatch.setattr(renderers.subprocess, "run", fake_run)
    real_path = renderers.Path
    monkeypatch.setattr(
        renderers, "Path",
        lambda p: environ if str(p) == "/proc/4242/environ" else real_path(p),
    )

    env = renderers._unit_runtime_environ("librespot.service")
    assert env["JASPER_LIBRESPOT_DEVICE"] == "librespot_ring_lane"
    assert env["PATH"] == "/usr/bin"
    assert env["EMPTY"] == "", "an empty value is a value, not an absence"
    assert "NOEQUALS" not in env, "a malformed entry is dropped, not crashed on"


def test_unit_runtime_environ_returns_empty_for_a_parked_unit(monkeypatch):
    """MainPID 0 means stopped/parked/failed. There is no environ to read, and
    guessing one would be worse than deferring to the next tier."""
    for mainpid in ("0", "", "not-a-pid"):
        def fake_run(cmd, _v=mainpid, **kwargs):
            class R:
                returncode = 0
                stdout = _v
            return R()

        monkeypatch.setattr(renderers.subprocess, "run", fake_run)
        assert renderers._unit_runtime_environ("librespot.service") == {}, mainpid


def test_resolver_surfaces_a_lanemap_vs_proc_disagreement(monkeypatch, tmp_path, caplog):
    """The docstring promises `/proc` is worth naming as ground truth. When the
    map's implied device differs from what the daemon is ACTUALLY running, that
    disagreement is the single most diagnostic fact available — the map wins
    (it is what the next restart will apply) but the divergence must not be
    silent."""
    import logging

    from jasper import renderer_lanes as rl
    lanes = str(tmp_path / "renderer_lanes.env")
    rl.render_renderer_lanes_env(("spotify",), path=lanes)
    monkeypatch.setattr(rl, "RENDERER_LANES_ENV", lanes)

    def fake_run(cmd, **kwargs):
        class R:
            returncode = 0
            stdout = ""
        return R()

    monkeypatch.setattr(renderers.subprocess, "run", fake_run)
    monkeypatch.setattr(
        renderers, "_unit_runtime_environ",
        lambda unit: {"JASPER_LIBRESPOT_DEVICE": "librespot_substream"},
    )

    with caplog.at_level(logging.WARNING):
        resolved = renderers._resolve_systemd_env_vars(
            "${JASPER_LIBRESPOT_DEVICE}", "librespot.service"
        )
    assert resolved == "librespot_ring_lane", "the map wins — it is what restarts apply"
    assert any(
        "renderer_lane.device_disagreement" in r.getMessage()
        for r in caplog.records
    ), "the map-vs-running disagreement must be surfaced, not swallowed"


# ---------------------------------------------------------------- mux mode
#
# The runtime reader is fail-open: missing/corrupt reads as "auto", which is
# right at runtime but hides a dropped manual pin without this doctor line.


@pytest.mark.parametrize(
    "payload, status, reason",
    [
        (None, "ok", ""),
        ("{ not json", "warn", renderers.REASON_MUX_MODE_CORRUPT),
        (json.dumps({"mode": "auto"}), "ok", ""),
        (
            json.dumps({"mode": "manual", "selected_source": "betamax"}),
            "warn",
            renderers.REASON_MUX_MODE_UNKNOWN_SOURCE,
        ),
    ],
    ids=["absent", "corrupt", "auto", "unknown-source"],
)
def test_classify_mux_mode_verdicts(tmp_path, payload, status, reason):
    p = tmp_path / "mux_mode.json"
    if payload is not None:
        p.write_text(payload, encoding="utf-8")

    res = _classify_mux_mode(p)

    assert res.status == status
    assert res.reason == reason


def test_classify_mux_mode_reports_a_valid_manual_pin(tmp_path):
    source = next(iter(MUSIC_SOURCES))
    p = tmp_path / "mux_mode.json"
    p.write_text(
        json.dumps({"mode": "manual", "selected_source": source.value}),
        encoding="utf-8",
    )

    res = _classify_mux_mode(p)

    assert res.status == "ok"
    assert res.reason == renderers.REASON_MUX_MODE_PINNED
