# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The fan-in coupling drift doctor check + its capture-type parser."""

from __future__ import annotations

from jasper.cli.doctor import audio_runtime as audio

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


def test_check_ok_when_no_loaded_capture(monkeypatch, tmp_path):
    res = _run_check(
        monkeypatch, coupling="loopback", cfg_text="filters:\n", tmp_path=tmp_path
    )
    assert res.status == "ok"


# --- check_fanin_coupling_value: persisted coupling value must be recognized --


def test_check_fanin_coupling_value_warns_on_removed_transport_pipe(monkeypatch, tmp_path):
    # A migrating box carrying the REMOVED transport_pipe coupling (or a typo) must
    # surface a warn until the reconciler converges it to loopback.
    fanin_env = tmp_path / "fanin.env"
    fanin_env.write_text("JASPER_FANIN_CAMILLA_COUPLING=transport_pipe\n")
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.FANIN_ENV_PATH", str(fanin_env)
    )
    res = audio.check_fanin_coupling_value()
    assert res.status == "warn"
    assert "transport_pipe" in res.detail
    assert "removed" in res.detail


def test_check_fanin_coupling_value_ok_on_recognized_coupling(monkeypatch, tmp_path):
    fanin_env = tmp_path / "fanin.env"
    fanin_env.write_text("JASPER_FANIN_CAMILLA_COUPLING=shm_ring\n")
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.FANIN_ENV_PATH", str(fanin_env)
    )
    res = audio.check_fanin_coupling_value()
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


# --- D-list survey finding 1 / wide-output-path PR-1: playback format check --
# Before this check, nothing read the CamillaDSP playback format back off a
# live config — a half-flip (emitter regenerated against one
# DEFAULT_PLAYBACK_FORMAT while the loaded file reflects another) was silent.

_S16_PLAYBACK_CFG = """\
devices:
  samplerate: 48000
  capture:
    type: Alsa
    channels: 2
    device: "plug:jasper_capture"
    format: S32_LE
  playback:
    type: Alsa
    channels: 2
    device: "outputd_content_playback"
    format: S16_LE
filters:
"""

_S32_PLAYBACK_CFG = """\
devices:
  samplerate: 48000
  capture:
    type: Alsa
    channels: 2
    device: "plug:jasper_capture"
    format: S32_LE
  playback:
    type: Alsa
    channels: 2
    device: "outputd_content_playback"
    format: S32_LE
filters:
"""


def _run_format_check(monkeypatch, tmp_path, cfg_text):
    cfg = tmp_path / "sound_current.yml"
    cfg.write_text(cfg_text)
    monkeypatch.setattr(
        audio, "_active_camilla_config_path", lambda: (cfg.parent, str(cfg))
    )
    return audio.check_camilla_playback_format()


def test_playback_format_ok_when_alsa_lane_matches_the_wide_default(
    monkeypatch, tmp_path
):
    # Green on a flipped box: the ALSA content lane carries
    # DEFAULT_PLAYBACK_FORMAT, S32_LE since PR-6.
    res = _run_format_check(monkeypatch, tmp_path, _S32_PLAYBACK_CFG)
    assert res.status == "ok"
    assert "S32_LE" in res.detail


def test_playback_format_fails_on_a_half_flipped_narrow_alsa_lane(
    monkeypatch, tmp_path
):
    # Prove the check CAN STILL FAIL in the post-flip world (mutation rule): an
    # ALSA lane config left at S16_LE after the flip is a half-flipped box — a
    # stale generated file, or an emitter that regenerated against a different
    # constant — and on the raw active lane it is what makes outputd's open fail
    # rather than convert. Red doctor line instead of silence.
    res = _run_format_check(monkeypatch, tmp_path, _S16_PLAYBACK_CFG)
    assert res.status == "fail"
    assert "S16_LE" in res.detail
    assert "S32_LE" in res.detail
    assert "DEFAULT_PLAYBACK_FORMAT" in res.detail


def test_playback_format_ok_when_no_config_loaded(monkeypatch, tmp_path):
    monkeypatch.setattr(
        audio, "_active_camilla_config_path", lambda: (tmp_path, None)
    )
    res = audio.check_camilla_playback_format()
    assert res.status == "ok"


def test_playback_format_ok_when_config_has_no_format_field(monkeypatch, tmp_path):
    res = _run_format_check(monkeypatch, tmp_path, "filters:\n")
    assert res.status == "ok"


# --- NIT1 (PR-1 gate review): the check is lane-aware, keyed on playback type -

_S16_FILE_PLAYBACK_CFG = """\
devices:
  samplerate: 48000
  capture:
    type: Alsa
    channels: 2
    device: "plug:jasper_capture"
    format: S32_LE
  playback:
    type: File
    channels: 2
    filename: "/run/jasper-snapserver/snapfifo"
    format: S16_LE
filters:
"""

_S32_FILE_PLAYBACK_CFG = """\
devices:
  samplerate: 48000
  capture:
    type: Alsa
    channels: 2
    device: "plug:jasper_capture"
    format: S32_LE
  playback:
    type: File
    channels: 2
    filename: "/run/jasper-snapserver/snapfifo"
    format: S32_LE
filters:
"""


_S16_RING_PLAYBACK_CFG = """\
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

_S32_RING_PLAYBACK_CFG = _S16_RING_PLAYBACK_CFG.replace(
    '    device: "jts_ring_playback"\n    format: S16_LE',
    '    device: "jts_ring_playback"\n    format: S32_LE',
)


def test_playback_format_ok_for_an_armed_ring_on_a_wide_box(monkeypatch, tmp_path):
    """AN ARMED RING IS ``type: Alsa`` — the File split alone does NOT cover it.

    Its width comes from resolve_ring_wire through the coupling's own kwargs (the
    PR-6 ring ruling), so an S16 ring config on a box whose general default is
    S32 is HEALTHY. Keyed
    on the ring's playback device, this must be green; keyed only on the File
    type, it red-lines every armed-ring box — including the certified-latency USB
    box, whose canary criterion is literally "doctor green" — with a remediation
    that regenerates the identical S16 config.
    """
    from jasper.camilla_config_contract import DEFAULT_PLAYBACK_FORMAT
    from jasper.fanin_coupling import RING_PLAYBACK_DEVICE, resolve_ring_wire

    assert resolve_ring_wire().sample_format != DEFAULT_PLAYBACK_FORMAT
    assert RING_PLAYBACK_DEVICE in _S16_RING_PLAYBACK_CFG
    res = _run_format_check(monkeypatch, tmp_path, _S16_RING_PLAYBACK_CFG)
    assert res.status == "ok"
    assert "S16_LE" in res.detail
    assert "resolve_ring_wire" in res.detail


def test_playback_format_fails_on_a_ring_config_that_drifted_wide(
    monkeypatch, tmp_path
):
    """The ring split must not become "any ring device auto-passes": a config
    declaring a width the box's resolved ring wire does not carry is a genuinely
    broken box and stays red — even though S32 is what the loopback lane wants,
    which is exactly the confusion the three-way split has to get right.

    This is the check that has to catch it, because the ring LAYOUT accepts both
    S16LE and S32LE: a config drifted to the other one is inside the accept-set,
    so the attach would not refuse it — the ends would simply be built to
    different widths."""
    res = _run_format_check(monkeypatch, tmp_path, _S32_RING_PLAYBACK_CFG)
    assert res.status == "fail"
    assert "S32_LE" in res.detail
    assert "S16_LE" in res.detail
    assert "resolve_ring_wire" in res.detail


def test_playback_format_ok_for_file_sink_pinned_narrow_while_the_lane_is_wide(
    monkeypatch, tmp_path
):
    # The bonded-leader pipe sink (and the active-speaker parked graph's
    # /dev/null sink) are pinned to DEFAULT_PIPE_SINK_FORMAT independently of
    # the general program lane (D4), so a File-type S16 config stays green while
    # the ALSA lane is S32 — the two constants now genuinely differ, no
    # monkeypatch needed. Without the lane split this would red-line every
    # healthy pipe-sink leader and parked box.
    from jasper.camilla_config_contract import (
        DEFAULT_PIPE_SINK_FORMAT,
        DEFAULT_PLAYBACK_FORMAT,
    )

    assert DEFAULT_PIPE_SINK_FORMAT != DEFAULT_PLAYBACK_FORMAT
    res = _run_format_check(monkeypatch, tmp_path, _S16_FILE_PLAYBACK_CFG)
    assert res.status == "ok"
    assert "S16_LE" in res.detail


def test_playback_format_fails_on_a_deliberately_wide_file_sink_config(
    monkeypatch, tmp_path
):
    # The lane split must not become "any File type auto-passes": a File
    # sink whose format has genuinely drifted off DEFAULT_PIPE_SINK_FORMAT
    # (S32 here) still fails — even though S32 is what the ALSA lane wants,
    # which is exactly the confusion the lane split has to get right.
    res = _run_format_check(monkeypatch, tmp_path, _S32_FILE_PLAYBACK_CFG)
    assert res.status == "fail"
    assert "S32_LE" in res.detail
    assert "S16_LE" in res.detail
    assert "DEFAULT_PIPE_SINK_FORMAT" in res.detail


_STALE_RING_CFG = """\
devices:
  capture:
    type: Alsa
    channels: 2
    device: "jts_ring_capture"
  playback:
    type: Alsa
    channels: 2
    device: "jts_ring_active_playback"
filters:
"""


def test_stale_ring_devices_under_loopback_send_a_roleful_box_up_the_ladder(
    monkeypatch, tmp_path
):
    """A ROLEFUL box gets the rollback LADDER, not a reconcile that converges nothing.

    This is the state PR #2514's residual describes: a ring-endpoint graph loaded
    while the coupling has fallen back to loopback. The check's remedy used to be
    `jasper-fanin-coupling-reconcile loopback` unconditionally — which on a
    roleful box moves nothing and reports SUCCESS. `reconcile_current_dsp`
    declines to host a transient active graph (`eq_on_active_not_wired` ->
    status `skipped`), and `_reconcile_camilla` turns a `skipped` under a
    non-shm_ring coupling into `True`. So the operator ran a command, was told it
    worked, and the warn stayed.

    The graph has to be moved by step 1 of the rollback ladder. Asserted through
    the classification-free half of the message — the command spelling — because
    that is what an operator copies.
    """
    monkeypatch.setattr(audio, "_requires_roleful_graph", lambda: True)
    res = _run_check(
        monkeypatch,
        coupling="loopback",
        cfg_text=_STALE_RING_CFG,
        tmp_path=tmp_path,
    )
    assert res.status == "warn"
    assert "jts_ring_active_playback" in res.detail
    assert "baseline-reemit --endpoint aloop" in res.detail, res.detail
    assert "jasper-audio-hardware-reconcile" in res.detail, res.detail
    assert "jasper-fanin-coupling-reconcile loopback" in res.detail, res.detail


def test_stale_ring_devices_under_loopback_keep_the_plain_remedy_when_passive(
    monkeypatch, tmp_path
):
    """CONTROL: a PASSIVE box keeps exactly the one-command remedy.

    Without this the assertion above would also pass if the ladder text had been
    appended unconditionally — and on a passive box the coupling reconciler
    genuinely is the whole fix, so sending one up a three-rung active-speaker
    ladder would be worse advice, not more of it.
    """
    monkeypatch.setattr(audio, "_requires_roleful_graph", lambda: False)
    res = _run_check(
        monkeypatch,
        coupling="loopback",
        cfg_text=_STALE_RING_CFG,
        tmp_path=tmp_path,
    )
    assert res.status == "warn"
    assert "jasper-fanin-coupling-reconcile loopback" in res.detail
    assert "baseline-reemit" not in res.detail, res.detail
