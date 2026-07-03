# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The jts_ring platform-assets doctor check (audio-graph consolidation P1).

Pins the inert-phase contract: a MISSING asset is `warn` (loopback still
carries audio), but an INSTALLED-but-unusable ioplug is `fail`. The check
never touches a live ring — the open probe is fully mocked here.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

from jasper.cli.doctor import audio


def _stage_assets(monkeypatch, tmp_path, *, so=True, conf=True, shm=True):
    """Point the module constants at tmp paths and create/omit each asset."""
    plugin_dir = tmp_path / "alsa-lib"
    plugin_dir.mkdir()
    so_path = plugin_dir / audio._JTS_RING_IOPLUG_SO
    if so:
        so_path.write_bytes(b"\x7fELF fake so")
    conf_path = tmp_path / "60-jts-ring.conf"
    if conf:
        conf_path.write_text("pcm.jts_ring_capture { type jts_ring }\n")
    shm_dir = tmp_path / "jts-ring"
    if shm:
        shm_dir.mkdir()

    monkeypatch.setattr(audio, "_JTS_RING_ALSA_PLUGIN_DIR", str(plugin_dir))
    monkeypatch.setattr(audio, "_JTS_RING_CONF_D", str(conf_path))
    monkeypatch.setattr(audio, "_JTS_RING_SHM_DIR", str(shm_dir))


def _probes_ok(monkeypatch):
    monkeypatch.setattr(
        audio, "_jts_ring_pcm_resolves", lambda pcm, tool: (True, "resolved")
    )


def _probes_fail(monkeypatch, detail="undefined symbol: snd_dlsym_start"):
    monkeypatch.setattr(
        audio, "_jts_ring_pcm_resolves", lambda pcm, tool: (False, detail)
    )


# --- check_ring_platform_assets ---------------------------------------


def test_ok_when_all_assets_present_and_probes_resolve(monkeypatch, tmp_path):
    _stage_assets(monkeypatch, tmp_path)
    _probes_ok(monkeypatch)
    res = audio.check_ring_platform_assets()
    assert res.status == "ok"
    assert "inert" in res.detail
    assert "jts_ring_capture" in res.detail and "jts_ring_playback" in res.detail


def test_warn_when_so_missing(monkeypatch, tmp_path):
    # Build failed / ring unavailable: inert phase => warn, not fail
    # (loopback still carries audio).
    _stage_assets(monkeypatch, tmp_path, so=False)
    _probes_ok(monkeypatch)
    res = audio.check_ring_platform_assets()
    assert res.status == "warn"
    assert "ioplug .so absent" in res.detail
    assert "loopback still active" in res.detail
    assert "redeploy" in res.detail.lower()


def test_warn_when_conf_missing(monkeypatch, tmp_path):
    _stage_assets(monkeypatch, tmp_path, conf=False)
    _probes_ok(monkeypatch)
    res = audio.check_ring_platform_assets()
    assert res.status == "warn"
    assert "conf.d absent" in res.detail


def test_warn_when_shm_dir_missing(monkeypatch, tmp_path):
    _stage_assets(monkeypatch, tmp_path, shm=False)
    _probes_ok(monkeypatch)
    res = audio.check_ring_platform_assets()
    assert res.status == "warn"
    assert "absent" in res.detail


def test_warn_lists_every_missing_asset(monkeypatch, tmp_path):
    _stage_assets(monkeypatch, tmp_path, so=False, conf=False, shm=False)
    _probes_ok(monkeypatch)
    res = audio.check_ring_platform_assets()
    assert res.status == "warn"
    assert "ioplug .so absent" in res.detail
    assert "conf.d absent" in res.detail


def test_missing_asset_does_not_run_the_open_probe(monkeypatch, tmp_path):
    # Guard: when an asset is missing we must NOT open-probe (there is no
    # working plugin to probe). A probe that raised here would prove it ran.
    _stage_assets(monkeypatch, tmp_path, so=False)

    def _boom(pcm, tool):  # pragma: no cover - must never be called
        raise AssertionError("probe ran despite missing asset")

    monkeypatch.setattr(audio, "_jts_ring_pcm_resolves", _boom)
    res = audio.check_ring_platform_assets()
    assert res.status == "warn"


def test_fail_when_so_present_but_pcm_open_fails(monkeypatch, tmp_path):
    # The .so is installed but ALSA can't use it (bad registration / arch /
    # -DPIC): a genuine defect that would break P2's arm => fail.
    _stage_assets(monkeypatch, tmp_path)
    _probes_fail(monkeypatch)
    res = audio.check_ring_platform_assets()
    assert res.status == "fail"
    assert "PCM open failed" in res.detail
    assert "snd_dlsym_start" in res.detail


def test_fail_names_the_failing_pcm(monkeypatch, tmp_path):
    _stage_assets(monkeypatch, tmp_path)

    def _one_fails(pcm, tool):
        if pcm == "jts_ring_playback":
            return False, "Unknown PCM"
        return True, "resolved"

    monkeypatch.setattr(audio, "_jts_ring_pcm_resolves", _one_fails)
    res = audio.check_ring_platform_assets()
    assert res.status == "fail"
    assert "jts_ring_playback" in res.detail


# --- _jts_ring_pcm_resolves (the open-probe helper) -------------------


def test_probe_ok_on_zero_exit(monkeypatch):
    monkeypatch.setattr(audio.shutil, "which", lambda t: f"/usr/bin/{t}")
    monkeypatch.setattr(
        audio, "_run",
        lambda cmd, timeout=5.0: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    ok, detail = audio._jts_ring_pcm_resolves("jts_ring_capture", "arecord")
    assert ok is True and detail == "resolved"


def test_probe_reports_stderr_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(audio.shutil, "which", lambda t: f"/usr/bin/{t}")
    monkeypatch.setattr(
        audio, "_run",
        lambda cmd, timeout=5.0: SimpleNamespace(
            returncode=1, stdout="", stderr="ALSA lib: Unknown PCM jts_ring_playback"
        ),
    )
    ok, detail = audio._jts_ring_pcm_resolves("jts_ring_playback", "aplay")
    assert ok is False
    assert "Unknown PCM" in detail


def test_probe_fails_closed_when_tool_missing(monkeypatch):
    monkeypatch.setattr(audio.shutil, "which", lambda t: None)
    ok, detail = audio._jts_ring_pcm_resolves("jts_ring_capture", "arecord")
    assert ok is False
    assert "not found" in detail


def test_probe_reports_hang_on_timeout(monkeypatch):
    monkeypatch.setattr(audio.shutil, "which", lambda t: f"/usr/bin/{t}")

    def _timeout(cmd, timeout=5.0):
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(audio, "_run", _timeout)
    ok, detail = audio._jts_ring_pcm_resolves("jts_ring_playback", "aplay")
    assert ok is False
    assert "hung" in detail


def test_probe_uses_devnull_for_capture_and_devzero_for_playback(monkeypatch):
    monkeypatch.setattr(audio.shutil, "which", lambda t: f"/usr/bin/{t}")
    seen = {}

    def _capture_cmd(cmd, timeout=5.0):
        seen["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(audio, "_run", _capture_cmd)

    audio._jts_ring_pcm_resolves("jts_ring_capture", "arecord")
    assert seen["cmd"][0] == "arecord"
    assert seen["cmd"][-1] == "/dev/null"

    audio._jts_ring_pcm_resolves("jts_ring_playback", "aplay")
    assert seen["cmd"][0] == "aplay"
    assert seen["cmd"][-1] == "/dev/zero"
