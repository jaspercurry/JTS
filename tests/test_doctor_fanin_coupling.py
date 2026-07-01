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
