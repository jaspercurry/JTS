# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The fan-in coupling drift doctor check + its capture-type parser."""

from __future__ import annotations

from jasper.cli.doctor import audio

_RAWFILE_CFG = """\
devices:
  samplerate: 48000
  capture:
    type: RawFile
    channels: 2
    filename: "/run/jasper-fanin/camilla.pipe"
    format: S32_LE
  playback:
    type: File
    channels: 2
    filename: "/run/jasper-outputd/content.pipe"
    format: S16_LE
filters:
"""

_ALSA_CFG = """\
devices:
  capture:
    type: Alsa
    channels: 2
    device: "plug:jasper_capture"
  playback:
    type: File
    channels: 2
    filename: "/run/jasper-snapserver/snapfifo"
filters:
"""

_ALSA_LOCAL_PIPE_CFG = _ALSA_CFG.replace(
    "/run/jasper-snapserver/snapfifo",
    "/run/jasper-outputd/content.pipe",
)

_OUTPUTD_PIPE_ENV = (
    "JASPER_OUTPUTD_LOCAL_CONTENT_PIPE=/run/jasper-outputd/content.pipe\n"
)


def test_capture_parser_reads_rawfile(tmp_path):
    cfg = tmp_path / "c.yml"
    cfg.write_text(_RAWFILE_CFG)
    assert audio._loaded_capture_type(cfg) == "RawFile"
    assert audio._loaded_playback_type(cfg) == "File"
    assert (
        audio._loaded_playback_filename(cfg)
        == "/run/jasper-outputd/content.pipe"
    )


def test_capture_parser_reads_alsa_not_playback_file(tmp_path):
    # The playback File sink must NOT be misread as the capture type.
    cfg = tmp_path / "c.yml"
    cfg.write_text(_ALSA_CFG)
    assert audio._loaded_capture_type(cfg) == "Alsa"


def test_capture_parser_none_when_absent(tmp_path):
    assert audio._loaded_capture_type(tmp_path / "missing.yml") is None
    cfg = tmp_path / "c.yml"
    cfg.write_text("filters:\n  x: 1\n")
    assert audio._loaded_capture_type(cfg) is None


def _run_check(monkeypatch, *, coupling, cfg_text, tmp_path, outputd_env_text=""):
    cfg = tmp_path / "sound_current.yml"
    cfg.write_text(cfg_text)
    outputd_env = tmp_path / "outputd.env"
    outputd_env.write_text(outputd_env_text)
    monkeypatch.setattr(
        "jasper.audio_runtime_plan.DEFAULT_OUTPUTD_ENV_PATH", str(outputd_env)
    )
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.read_persisted_coupling",
        lambda *a, **k: coupling,
    )
    # _active_camilla_config_path returns (statefile, active_config_path|None) —
    # mock the REAL tuple shape (a str-only mock masked a production TypeError).
    monkeypatch.setattr(
        audio, "_active_camilla_config_path", lambda: (cfg.parent, str(cfg))
    )
    return audio.check_fanin_coupling()


def test_check_ok_when_transport_pipe_matches_rawfile(monkeypatch, tmp_path):
    res = _run_check(
        monkeypatch,
        coupling="transport_pipe",
        cfg_text=_RAWFILE_CFG,
        tmp_path=tmp_path,
        outputd_env_text=_OUTPUTD_PIPE_ENV,
    )
    assert res.status == "ok" and "RawFile" in res.detail
    assert "playback=File" in res.detail


def test_check_ok_when_loopback_matches_alsa(monkeypatch, tmp_path):
    res = _run_check(monkeypatch, coupling="loopback", cfg_text=_ALSA_CFG, tmp_path=tmp_path)
    assert res.status == "ok"


def test_check_warns_on_loopback_capture_with_stale_local_file_playback(
    monkeypatch, tmp_path
):
    res = _run_check(
        monkeypatch,
        coupling="loopback",
        cfg_text=_ALSA_LOCAL_PIPE_CFG,
        tmp_path=tmp_path,
    )

    assert res.status == "warn"
    assert "non-Snapcast File sink" in res.detail


def test_check_warns_on_dangerous_drift_loopback_intent_rawfile_loaded(
    monkeypatch, tmp_path
):
    # The crash-loop precursor: env says loopback but the RawFile config is live.
    res = _run_check(monkeypatch, coupling="loopback", cfg_text=_RAWFILE_CFG, tmp_path=tmp_path)
    assert res.status == "warn"
    assert "jasper-fanin-coupling-reconcile loopback" in res.detail


def test_check_warns_on_drift_transport_pipe_intent_alsa_loaded(monkeypatch, tmp_path):
    res = _run_check(
        monkeypatch,
        coupling="transport_pipe",
        cfg_text=_ALSA_CFG,
        tmp_path=tmp_path,
        outputd_env_text=_OUTPUTD_PIPE_ENV,
    )
    assert res.status == "warn" and "expected RawFile" in res.detail
    assert "expected /run/jasper-outputd/content.pipe" in res.detail


def test_check_warns_when_transport_pipe_playback_path_drifted(monkeypatch, tmp_path):
    drifted = _RAWFILE_CFG.replace(
        "/run/jasper-outputd/content.pipe",
        "/run/elsewhere/content.pipe",
    )
    res = _run_check(
        monkeypatch,
        coupling="transport_pipe",
        cfg_text=drifted,
        tmp_path=tmp_path,
        outputd_env_text=_OUTPUTD_PIPE_ENV,
    )
    assert res.status == "warn"
    assert "playback_path=/run/elsewhere/content.pipe" in res.detail


def test_check_warns_when_transport_pipe_outputd_env_missing(monkeypatch, tmp_path):
    res = _run_check(
        monkeypatch,
        coupling="transport_pipe",
        cfg_text=_RAWFILE_CFG,
        tmp_path=tmp_path,
    )

    assert res.status == "warn"
    assert "JASPER_OUTPUTD_LOCAL_CONTENT_PIPE is missing" in res.detail


def test_check_warns_when_loopback_outputd_pipe_env_stale(monkeypatch, tmp_path):
    res = _run_check(
        monkeypatch,
        coupling="loopback",
        cfg_text=_ALSA_CFG,
        tmp_path=tmp_path,
        outputd_env_text=_OUTPUTD_PIPE_ENV,
    )

    assert res.status == "warn"
    assert "stale JASPER_OUTPUTD_LOCAL_CONTENT_PIPE" in res.detail


def test_check_ok_when_no_loaded_capture(monkeypatch, tmp_path):
    res = _run_check(
        monkeypatch, coupling="loopback", cfg_text="filters:\n", tmp_path=tmp_path
    )
    assert res.status == "ok"


# --- shm_ring coherence (Ring A + Ring B, P2) --------------------------------

_RING_CFG = """\
devices:
  samplerate: 48000
  capture:
    type: Alsa
    channels: 2
    device: "jts_ring_capture"
    format: S16_LE
  playback:
    type: Alsa
    channels: 2
    device: "jts_ring_playback"
    format: S16_LE
filters:
"""

_RING_BRIDGE_ENV = "JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring\n"


def test_ring_ok_when_both_ends_ring_and_bridge_matches(monkeypatch, tmp_path):
    res = _run_check(
        monkeypatch,
        coupling="shm_ring",
        cfg_text=_RING_CFG,
        tmp_path=tmp_path,
        outputd_env_text=_RING_BRIDGE_ENV,
    )
    assert res.status == "ok"
    assert "jts_ring_capture" in res.detail and "jts_ring_playback" in res.detail


def test_ring_warns_on_partial_flip_bridge_missing(monkeypatch, tmp_path):
    # shm_ring intent but outputd bridge is direct -> partial flip warning.
    res = _run_check(
        monkeypatch,
        coupling="shm_ring",
        cfg_text=_RING_CFG,
        tmp_path=tmp_path,
        outputd_env_text="",  # bridge defaults to direct
    )
    assert res.status == "warn"
    assert "PARTIAL" in res.detail or "shm_ring" in res.detail


def test_loopback_warns_on_stale_ring_bridge(monkeypatch, tmp_path):
    # loopback intent but a stale shm_ring bridge remains -> partial flip warning.
    res = _run_check(
        monkeypatch,
        coupling="loopback",
        cfg_text=_ALSA_CFG,
        tmp_path=tmp_path,
        outputd_env_text=_RING_BRIDGE_ENV,
    )
    assert res.status == "warn"
    assert "stale" in res.detail.lower() and "shm_ring" in res.detail


def test_ring_warns_when_loaded_graph_reverted_to_loopback(monkeypatch, tmp_path):
    # THE finding-5 revert: env pair is coherent but the loaded config is the
    # loopback graph (a camilla restart re-seeded it) -> warn to re-arm.
    res = _run_check(
        monkeypatch,
        coupling="shm_ring",
        cfg_text=_ALSA_CFG,  # loopback capture device, NOT jts_ring_capture
        tmp_path=tmp_path,
        outputd_env_text=_RING_BRIDGE_ENV,
    )
    assert res.status == "warn"
    assert "ring config" in res.detail or "jts_ring" in res.detail


def test_loopback_warns_on_stale_ring_graph_with_clean_env(monkeypatch, tmp_path):
    # SF5 (the disarm-direction mirror of finding-5): a disarm's camilla step
    # FAILED, so the env pair reads clean (loopback intent, bridge=direct — the
    # earlier stale-bridge check does NOT fire) but the LOADED graph still names the
    # ring ioplug devices. CamillaDSP then captures a writer-dead Ring A (zero-fill
    # silence) while the box reads doctor-GREEN on a type-only capture==Alsa check.
    # The device-name check must catch this.
    res = _run_check(
        monkeypatch,
        coupling="loopback",
        cfg_text=_RING_CFG,  # stale ring devices, but capture.type is Alsa
        tmp_path=tmp_path,
        outputd_env_text="",  # bridge=direct -> env pair coherent with loopback
    )
    assert res.status == "warn"
    assert "ring ioplug device" in res.detail
    assert "jts_ring_capture" in res.detail
    assert "jasper-fanin-coupling-reconcile loopback" in res.detail


def test_loopback_ok_when_loaded_graph_is_plain_alsa(monkeypatch, tmp_path):
    # The guard must not false-positive: a clean loopback box (plug:jasper_capture,
    # snapfifo playback) stays OK.
    res = _run_check(
        monkeypatch,
        coupling="loopback",
        cfg_text=_ALSA_CFG,
        tmp_path=tmp_path,
        outputd_env_text="",
    )
    assert res.status == "ok"
