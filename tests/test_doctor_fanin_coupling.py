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
    type: Alsa
    channels: 2
    device: "outputd_content_playback"
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


def test_capture_parser_reads_rawfile(tmp_path):
    cfg = tmp_path / "c.yml"
    cfg.write_text(_RAWFILE_CFG)
    assert audio._loaded_capture_type(cfg) == "RawFile"


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


def _run_check(monkeypatch, *, coupling, cfg_text, tmp_path):
    cfg = tmp_path / "sound_current.yml"
    cfg.write_text(cfg_text)
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


def test_check_ok_when_fifo_matches_rawfile(monkeypatch, tmp_path):
    res = _run_check(monkeypatch, coupling="fifo", cfg_text=_RAWFILE_CFG, tmp_path=tmp_path)
    assert res.status == "ok" and "RawFile" in res.detail


def test_check_ok_when_loopback_matches_alsa(monkeypatch, tmp_path):
    res = _run_check(monkeypatch, coupling="loopback", cfg_text=_ALSA_CFG, tmp_path=tmp_path)
    assert res.status == "ok"


def test_check_warns_on_dangerous_drift_loopback_intent_rawfile_loaded(
    monkeypatch, tmp_path
):
    # The crash-loop precursor: env says loopback but the RawFile config is live.
    res = _run_check(monkeypatch, coupling="loopback", cfg_text=_RAWFILE_CFG, tmp_path=tmp_path)
    assert res.status == "warn"
    assert "jasper-fanin-coupling-reconcile loopback" in res.detail


def test_check_warns_on_drift_fifo_intent_alsa_loaded(monkeypatch, tmp_path):
    res = _run_check(monkeypatch, coupling="fifo", cfg_text=_ALSA_CFG, tmp_path=tmp_path)
    assert res.status == "warn" and "expected RawFile" in res.detail


def test_check_ok_when_no_loaded_capture(monkeypatch, tmp_path):
    res = _run_check(
        monkeypatch, coupling="loopback", cfg_text="filters:\n", tmp_path=tmp_path
    )
    assert res.status == "ok"
