# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the jasper-doctor aec domain."""

import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from jasper.chip_aec import health as chip_aec_health
from jasper.audio_profile_state import MicProbe, RuntimeAecEnv
from jasper.cli import doctor
from jasper.control import aec_endpoints
from tests._aec_bridge_helpers import _rms_log_line


# --------------------------------------------- AEC bridge output assessment



def _chip_rms_log_line(
    ref: int, near: int, primary: int, level_delta_db: float,
    raw0: int | None = None,
) -> str:
    """Synthesize one `chip_aec rms over` line — the shape the bridge emits
    instead of the AEC3 one when production chip AEC is armed. Legs are the
    fixed 150/210 ASR beams (`jasper/cli/aec_bridge.py`). `raw0=None`
    reproduces a journal from a build that predates the raw-mic-0 token."""
    raw0_token = "" if raw0 is None else f" raw0={raw0}"
    return (
        f"2026-09-02 17:00:00,000 aec-bridge INFO "
        f"chip_aec rms over 5.0s: ref={ref} near=chip_aec_210:{near} "
        f"primary=chip_aec_150:{primary} "
        f"level_delta={level_delta_db:.1f} dB{raw0_token} "
        f"(frames=1 ref_q=0 mic_q=0 "
        f"ref_starve=0 ref_clip=0.00% out_clip=0.00%)"
    )


@pytest.mark.parametrize(
    "raw0, expected_mic",
    [(2_900, 2_900), (None, 2_400)],
    ids=["raw0-present", "raw0-absent-falls-back-to-near"],
)
def test_chip_window_mic_is_the_raw_capture_channel(raw0, expected_mic):
    """The chip `near` beam is already cancelled, so it understates the
    near-end level the music gate was calibrated on. `raw0` carries the
    uncancelled capture channel and must win when the bridge emits it."""
    w = doctor._parse_rms_window(
        _chip_rms_log_line(
            ref=1_200, near=2_400, primary=1_900, level_delta_db=-2.1,
            raw0=raw0,
        )
    )

    assert w is not None
    assert (w.ref, w.mic, w.level_db, w.chip) == (
        1_200, expected_mic, None, True,
    )


def _bridge_reference_stats(
    source: str,
    *,
    now: float = 1_000.0,
    age_sec: float = 1.0,
    endpoint: str = "127.0.0.1:9891",
) -> dict:
    identity = {"ref_source": source}
    if source == "outputd_udp":
        identity["outputd_ref_udp"] = endpoint
    return {
        "updated_epoch_sec": now - age_sec,
        "active_capture_plan": {"mic_reference_identity": identity},
    }


def _outputd_reference_status(
    *,
    target: object = "127.0.0.1:9891",
    active: bool = True,
    error_count: int = 0,
) -> dict:
    return {
        "reference_outputs": {
            "udp_target": target,
            "udp_active": active,
            "udp_error_count": error_count,
        },
    }

# The reference-failure remediation must never send an operator to a hop
# nothing reads any more: `pcm.jasper_ref`/`pcm.jasper_capture` in
# /etc/asound.conf was retired by U4/P7-1, and the `pcm.jasper_out` pre-outputd
# TTS dmix bypass by #2240. Assistant TTS now rides fan-in -> CamillaDSP ->
# outputd and lands in the reference like any other program audio.
_RETIRED_HOPS = ("pcm.jasper_capture", "/etc/asound.conf", "jasper_ref")


def _silent_ref_journal(windows: int = 8) -> str:
    """Mic loud acoustically throughout, ref silent throughout: the PR #75
    dsnoop rate-lock signature, and the regression this check exists for."""
    return "\n".join(
        _rms_log_line(ref=0, mic=2500, aec=2400, attn_db=-0.4)
        for _ in range(windows)
    )


def _healthy_journal(windows: int = 8) -> str:
    return "\n".join(
        _rms_log_line(ref=1200, mic=2400, aec=150, attn_db=-24.1)
        for _ in range(windows)
    )


_REASON_BRIDGE_OUTPUT_NO_WINDOWS = doctor.aec.REASON_BRIDGE_OUTPUT_NO_WINDOWS
_REASON_BRIDGE_OUTPUT_IDLE = doctor.aec.REASON_BRIDGE_OUTPUT_IDLE
_REASON_BRIDGE_OUTPUT_HEALTHY_WORK = doctor.aec.REASON_BRIDGE_OUTPUT_HEALTHY_WORK
_REASON_BRIDGE_OUTPUT_REF_SILENT = doctor.aec.REASON_BRIDGE_OUTPUT_REF_SILENT
_REASON_BRIDGE_OUTPUT_REF_PROVEN_HEALTHY = (
    doctor.aec.REASON_BRIDGE_OUTPUT_REF_PROVEN_HEALTHY
)


@pytest.mark.parametrize(
    "journal, status, reason",
    [
        # An active bridge logs a window every 5 s: none is missing evidence.
        ("", "warn", _REASON_BRIDGE_OUTPUT_NO_WINDOWS),
        # Mic and ref both quiet — the speaker has been idle.
        (
            "\n".join(
                _rms_log_line(ref=0, mic=200, aec=30, attn_db=-16.5)
                for _ in range(10)
            ),
            "ok",
            _REASON_BRIDGE_OUTPUT_IDLE,
        ),
        (_healthy_journal(), "ok", _REASON_BRIDGE_OUTPUT_HEALTHY_WORK),
        (_silent_ref_journal(), "fail", _REASON_BRIDGE_OUTPUT_REF_SILENT),
        # Exactly one healthy_ref window flips the silent-ref pattern from fail
        # to ok: if the ref chain proved itself once, it is trusted.
        (
            _silent_ref_journal(7)
            + "\n"
            + _rms_log_line(ref=300, mic=400, aec=80, attn_db=-14.0),
            "ok",
            _REASON_BRIDGE_OUTPUT_REF_PROVEN_HEALTHY,
        ),
        # 1-4 silent-ref windows are below the 5-count alarm but still
        # surfaced, so an intermittent glitch is visible before it tips over.
        (
            _healthy_journal(6)
            + "\n"
            + "\n".join(
                _rms_log_line(ref=0, mic=2200, aec=2100, attn_db=-0.4)
                for _ in range(3)
            ),
            "ok",
            _REASON_BRIDGE_OUTPUT_HEALTHY_WORK,
        ),
    ],
    ids=["empty", "idle", "healthy", "silent-ref", "one-healthy-window",
         "below-alarm"],
)
def test_assess_aec_bridge_output_verdicts(journal, status, reason):
    r = doctor._assess_aec_bridge_output(journal)

    assert r.status == status
    assert r.reason == reason


def test_assess_aec_bridge_output_below_alarm_still_names_the_count():
    """The reason code says WHICH branch fired; the below-alarm count itself
    is data the reason can't carry (it varies per run), so it stays a detail
    assertion."""
    journal = (
        _healthy_journal(6)
        + "\n"
        + "\n".join(
            _rms_log_line(ref=0, mic=2200, aec=2100, attn_db=-0.4)
            for _ in range(3)
        )
    )

    r = doctor._assess_aec_bridge_output(journal)

    assert r.reason == doctor.aec.REASON_BRIDGE_OUTPUT_HEALTHY_WORK
    assert "silent-ref=3" in r.detail


@pytest.mark.parametrize(
    "journal",
    [
        "\n".join(
            _rms_log_line(ref=1_200, mic=2_400, aec=150, attn_db=-24.1)
            for _ in range(8)
        ),
        "\n".join(
            _chip_rms_log_line(
                ref=1_200, near=2_400, primary=1_900, level_delta_db=-2.1,
            )
            for _ in range(8)
        ),
    ],
    ids=["aec3", "chip"],
)
def test_assess_aec_bridge_output_counts_windows_in_both_log_shapes(journal):
    """The bridge emits a different RMS line under chip AEC. Both shapes must
    reach the assessment as counted windows: a shape the parser drops makes
    the check report on zero evidence."""
    lines = journal.split("\n")

    total_windows = [
        w for w in map(doctor._parse_rms_window, lines) if w is not None
    ]

    assert len(total_windows) == len(lines) > 0
    assert doctor._assess_aec_bridge_output(journal).status == "ok"


def test_chip_windows_never_count_as_attenuation_evidence():
    """A chip `level_delta` of -24 dB reads like deep AEC3 attenuation but is
    the delta between two beams the chip already cancelled, so it must never
    be counted as proof the canceller did work."""
    journal = "\n".join(
        _chip_rms_log_line(
            ref=1_200, near=2_400, primary=150, level_delta_db=-24.1,
        )
        for _ in range(8)
    )

    windows = [doctor._parse_rms_window(line) for line in journal.split("\n")]

    assert all(
        w is not None
        and w.chip
        and w.level_db is None
        and w.ref == 1_200
        and w.mic == 2_400
        for w in windows
    )
    assert doctor._assess_aec_bridge_output(journal).status == "ok"


def test_one_chip_window_does_not_displace_the_aec3_assessment():
    """A restart across a profile change leaves both shapes in one journal.
    The chip summary is for an all-chip journal; a mixed one still owes the
    AEC3 verdict over its AEC3 windows."""
    journal = "\n".join(
        [_rms_log_line(ref=1_200, mic=2_400, aec=150, attn_db=-24.1)] * 17
        + [
            _chip_rms_log_line(
                ref=1_200, near=2_400, primary=1_900, level_delta_db=-2.1,
            )
        ]
    )

    result = doctor._assess_aec_bridge_output(journal)

    assert result.status == "ok"
    assert result.reason == doctor.aec.REASON_BRIDGE_OUTPUT_HEALTHY_WORK
    # Both counts reported: 17 AEC3 windows assessed, 1 chip window disclosed.
    # This is computed data the reason code can't carry, so it stays a
    # detail assertion.
    assert "17/18" in result.detail and "1/18" in result.detail


def test_assess_aec_output_silent_ref_with_a_healthy_window_names_the_cause():
    """The 2026-05-16 false positive: loud room voice pushes silent_ref over
    threshold while at least one window proves the ref chain alive."""
    lines = [
        _rms_log_line(ref=0, mic=2200, aec=2100, attn_db=-0.4),
        _rms_log_line(ref=0, mic=2400, aec=2300, attn_db=-0.4),
        _rms_log_line(ref=0, mic=2600, aec=2500, attn_db=-0.3),
        _rms_log_line(ref=0, mic=2100, aec=2050, attn_db=-0.2),
        _rms_log_line(ref=0, mic=2300, aec=2250, attn_db=-0.2),
        _rms_log_line(ref=800, mic=2400, aec=200, attn_db=-21.6),
        _rms_log_line(ref=1100, mic=2800, aec=180, attn_db=-23.8),
    ]

    r = doctor._assess_aec_bridge_output("\n".join(lines))

    assert r.status == "ok"
    assert r.reason == doctor.aec.REASON_BRIDGE_OUTPUT_REF_PROVEN_HEALTHY


@pytest.mark.parametrize(
    "music_chain_active, status",
    [(False, "ok"), (True, "fail")],
    ids=["loopback-closed", "loopback-open"],
)
def test_assess_aec_output_relaxes_only_on_positive_idle_evidence(
    music_chain_active, status
):
    """A silent ref is expected, not suspicious, when no renderer is writing
    the loopback — so a pure-voice session does not read as a degraded bridge.
    The guard relaxes only on positive evidence of an idle loopback, never on
    uncertainty."""
    r = doctor._assess_aec_bridge_output(
        _silent_ref_journal(), music_chain_active=music_chain_active
    )

    assert r.status == status
    if status == "ok":
        assert r.reason == doctor.aec.REASON_BRIDGE_OUTPUT_REF_SILENT_NO_MUSIC
        # The gate sees only the snd-aloop renderer lanes, so the message must
        # disclose its coverage limit in FULL: a USB Audio Input stream and any
        # ring-armed renderer lane (U3/P6) are both invisible to it.
        assert (
            "USB Audio Input and any ring-armed renderer lane are invisible"
            in r.detail
        )
    else:
        assert r.reason == doctor.aec.REASON_BRIDGE_OUTPUT_REF_SILENT


@pytest.mark.parametrize(
    "bridge_stats, outputd_status, must_name, must_not_name",
    [
        # No stats at all: nothing can be named, so nothing is.
        (None, None, "Runtime reference provenance is unavailable", ""),
        (
            _bridge_reference_stats("outputd_udp"),
            _outputd_reference_status(error_count=3),
            "reference_outputs.udp_target='127.0.0.1:9891'",
            "",
        ),
        # Publisher and receiver disagree: both hops need reconciling.
        (
            _bridge_reference_stats("outputd_udp", endpoint="127.0.0.1:9891"),
            _outputd_reference_status(
                target="127.0.0.1:9999", active=False, error_count=4
            ),
            "publisher target and bridge receiver do not match",
            "",
        ),
        # A stats file still claiming source=alsa names a producer that no
        # longer exists (U4/P7-1), so the generic remediation must win.
        (
            _bridge_reference_stats("alsa"),
            None,
            "cannot safely name the failed hop",
            "source=alsa",
        ),
        (
            _bridge_reference_stats("future_transport"),
            _outputd_reference_status(),
            "cannot safely name the failed hop",
            "source=outputd_udp",
        ),
        (
            {
                "updated_epoch_sec": 999.0,
                "active_capture_plan": {"mic_reference_identity": "malformed"},
            },
            _outputd_reference_status(),
            "cannot safely name the failed hop",
            "source=outputd_udp",
        ),
        (
            _bridge_reference_stats("outputd_udp", age_sec=31.0),
            _outputd_reference_status(),
            "unavailable, malformed, stale, or unknown",
            "source=outputd_udp",
        ),
    ],
    ids=["no-stats", "outputd-udp", "endpoint-mismatch", "retired-alsa",
         "unknown-source", "malformed-identity", "stale-stats"],
)
def test_assess_aec_output_remediation_names_only_a_hop_it_can_prove(
    bridge_stats, outputd_status, must_name, must_not_name
):
    """`.reason` says the branch is the ref-silent FAIL; the hop the
    remediation names is a computed diagnostic value with no other
    structured home (see `_aec_reference_failure_remediation`), so it stays
    a `.detail` content assertion, including the never-mention-a-retired-hop
    guard below."""
    result = doctor._assess_aec_bridge_output(
        _silent_ref_journal(),
        bridge_stats=bridge_stats,
        outputd_status=outputd_status,
        now=1_000.0,
    )

    assert result.status == "fail"
    assert result.reason == doctor.aec.REASON_BRIDGE_OUTPUT_REF_SILENT
    assert must_name in result.detail
    if must_not_name:
        assert must_not_name not in result.detail
    for retired in _RETIRED_HOPS:
        assert retired not in result.detail


@pytest.mark.parametrize(
    "outputd_status",
    [
        {"reference_outputs": {"udp_active": True, "udp_error_count": 0}},
        _outputd_reference_status(target=9891),
        _outputd_reference_status(target="not-an-endpoint"),
    ],
    ids=["missing", "non-string", "malformed"],
)
def test_assess_aec_output_unusable_outputd_target_is_comparison_neutral(
    outputd_status,
):
    result = doctor._assess_aec_bridge_output(
        _silent_ref_journal(),
        bridge_stats=_bridge_reference_stats("outputd_udp"),
        outputd_status=outputd_status,
        now=1_000.0,
    )

    assert result.reason == doctor.aec.REASON_BRIDGE_OUTPUT_REF_SILENT

    assert result.status == "fail"
    assert "no comparable UDP target" in result.detail
    assert "publisher target and bridge receiver do not match" not in result.detail


@pytest.mark.parametrize(
    "stats, expected",
    [
        # A JSON-valid but absurd integer must not be trusted as a timestamp.
        (
            json.loads(
                '{"updated_epoch_sec":'
                + "1" + ("0" * 4_000)
                + ',"active_capture_plan":{"mic_reference_identity":'
                '{"ref_source":"outputd_udp",'
                '"outputd_ref_udp":"127.0.0.1:9891"}}}'
            ),
            None,
        ),
        (_bridge_reference_stats("outputd_udp", now=1_000.0, age_sec=-1.0), None),
    ],
    ids=["oversized-timestamp", "future-timestamp"],
)
def test_bridge_reference_provenance_rejects_untrustworthy_timestamps(
    stats, expected
):
    assert doctor.aec._bridge_reference_provenance(stats, 1_000.0) is expected


def test_check_aec_output_health_uses_live_outputd_status_on_failure(monkeypatch):
    _stage_bridge_journal(monkeypatch, _silent_ref_journal())
    monkeypatch.setattr(
        doctor.aec,
        "_read_outputd_status_for_aec_reference",
        lambda: _outputd_reference_status(error_count=7),
    )

    result = doctor.check_aec_bridge_output_health()

    assert result.status == "fail"
    assert result.reason == doctor.aec.REASON_BRIDGE_OUTPUT_REF_SILENT
    assert "reference_outputs.udp_target='127.0.0.1:9891'" in result.detail
    assert "udp_error_count=7" in result.detail


def test_check_aec_output_health_skips_outputd_status_when_reference_is_healthy(
    monkeypatch,
):
    _stage_bridge_journal(monkeypatch, _healthy_journal())
    monkeypatch.setattr(
        doctor.aec,
        "_read_outputd_status_for_aec_reference",
        lambda: pytest.fail("healthy passive check must not probe outputd STATUS"),
    )

    result = doctor.check_aec_bridge_output_health()

    assert result.status == "ok"
    assert result.reason == doctor.aec.REASON_BRIDGE_OUTPUT_HEALTHY_WORK


def _stage_bridge_journal(monkeypatch, journal: str) -> None:
    def fake_run(command, **_kwargs):
        if command[:2] == ["systemctl", "is-active"]:
            return SimpleNamespace(stdout="active\n", stderr="", returncode=0)
        return SimpleNamespace(stdout=journal, stderr="", returncode=0)

    monkeypatch.setattr(doctor.aec, "_parked_as_bonded_follower", lambda: False)
    monkeypatch.setattr(doctor.aec, "_run", fake_run)
    monkeypatch.setattr(doctor.aec, "_loopback_playback_active", lambda: True)
    monkeypatch.setattr(
        doctor.aec,
        "_read_bridge_stats_snapshot",
        lambda: _bridge_reference_stats("outputd_udp", now=1_000.0),
    )
    monkeypatch.setattr(doctor.aec.time, "time", lambda: 1_000.0)


def test_loopback_playback_active_reads_proc_status(tmp_path):
    """Helper must report True for any non-closed subdev and False when
    every subdev is closed. Verifies the first-line strip-and-compare
    against the actual /proc/asound status file format (single word
    `closed` vs `state: RUNNING\\n…`)."""
    fake_root = tmp_path / "asound" / "Loopback" / "pcm0p"
    fake_root.mkdir(parents=True)
    sub_paths = []
    for sub in range(4):
        d = fake_root / f"sub{sub}"
        d.mkdir()
        status = d / "status"
        status.write_text("closed\n")
        sub_paths.append(str(status))

    with patch("glob.glob", return_value=sub_paths):
        # All closed → inactive.
        assert doctor._loopback_playback_active() is False
        # Flip sub2 to RUNNING → active.
        (fake_root / "sub2" / "status").write_text(
            "state: RUNNING\nowner_pid   : 12345\n"
        )
        assert doctor._loopback_playback_active() is True

    # No status files at all (e.g., snd-aloop not loaded) → inactive,
    # never raises.
    with patch("glob.glob", return_value=[]):
        assert doctor._loopback_playback_active() is False


def _reference_input_stats(
    *,
    now_monotonic: float = 1_000.0,
    schema_version: int = 4,
    snapshot_age_sec: float = 0.5,
    process_age_sec: float = 60.0,
    updated_epoch_sec: float = 50_000.0,
    started_epoch_sec: float = 40_000.0,
    source: str = "outputd_udp",
    endpoint: str = "127.0.0.1:9891",
    frames_enqueued: int = 100,
    last_frame_age_ms: float | None = 100.0,
    ref_starved_frames: int = 0,
) -> dict:
    return {
        "schema_version": schema_version,
        "updated_epoch_sec": updated_epoch_sec,
        "started_epoch_sec": started_epoch_sec,
        "active_capture_plan": {
            "mic_reference_identity": {
                "ref_source": source,
                "outputd_ref_udp": endpoint,
            }
        },
        "reference_input": {
            "source": source,
            "endpoint": endpoint,
            "frames_enqueued": frames_enqueued,
            "last_frame_age_ms": last_frame_age_ms,
            "snapshot_monotonic_ms": (
                now_monotonic - snapshot_age_sec
            ) * 1000,
            "process_age_ms": max(
                0.0,
                process_age_sec - snapshot_age_sec,
            ) * 1000,
        },
        "counters": {"ref_starved_frames": ref_starved_frames},
    }


def _active_outputd_reference_status(
    *,
    target: str = "127.0.0.1:9891",
    active: bool = True,
    errors: int = 0,
) -> dict:
    return {
        "reference_outputs": {
            "udp_target": target,
            "udp_active": active,
            "udp_error_count": errors,
        }
    }


def _assess_reference_stats(stats: dict, *, now_monotonic: float = 1_000.0):
    return doctor.aec._assess_aec_reference_input_from_stats(
        stats,
        now_monotonic,
        configured_source="outputd_udp",
        expected_endpoint="127.0.0.1:9891",
        outputd_status=_active_outputd_reference_status(),
    )


def test_assess_reference_input_recent_receiver_is_ok():
    assessed = _assess_reference_stats(_reference_input_stats())

    assert assessed is not None
    result, startup_grace = assessed
    assert result.status == "ok"
    assert startup_grace is False
    assert result.reason == doctor.aec.REASON_REF_RECEIVER_CURRENT


def test_assess_reference_input_no_frame_after_startup_grace_fails():
    assessed = _assess_reference_stats(
        _reference_input_stats(
            frames_enqueued=0,
            last_frame_age_ms=None,
        )
    )

    assert assessed is not None
    result, startup_grace = assessed
    assert result.status == "fail"
    assert startup_grace is False
    assert result.reason == doctor.aec.REASON_REF_ZERO_FRAMES_AFTER_GRACE


@pytest.mark.parametrize(
    ("source", "endpoint"),
    [
        ("alsa", "jasper_ref"),
        ("outputd_udp", "127.0.0.1:9999"),
    ],
)
def test_assess_reference_input_runtime_identity_mismatch_fails(
    source,
    endpoint,
):
    assessed = _assess_reference_stats(
        _reference_input_stats(source=source, endpoint=endpoint)
    )

    assert assessed is not None
    result, _startup_grace = assessed
    assert result.status == "fail"
    assert result.reason == doctor.aec.REASON_REF_ROUTE_MISMATCH


def test_assess_reference_input_formerly_nonzero_but_frozen_fails():
    assessed = _assess_reference_stats(
        _reference_input_stats(
            frames_enqueued=55_000,
            last_frame_age_ms=5_100,
        )
    )

    assert assessed is not None
    result, _startup_grace = assessed
    assert result.status == "fail"
    assert result.reason == doctor.aec.REASON_REF_RECEIVER_STALE


@pytest.mark.parametrize(
    "stats",
    [
        {},
        _reference_input_stats(schema_version=3),
        _reference_input_stats(schema_version=5),
    ],
    ids=["missing-schema", "old-schema", "future-schema"],
)
def test_assess_reference_input_undeclared_schema_preserves_fallback(stats):
    assert _assess_reference_stats(stats) is None


@pytest.mark.parametrize(
    ("stats", "reason"),
    [
        (
            {
                **_reference_input_stats(),
                "reference_input": {"source": "outputd_udp"},
            },
            "REASON_REF_CONTRACT_MISSING_FIELD",
        ),
        (
            _reference_input_stats(snapshot_age_sec=31.0),
            "REASON_REF_STATS_WRITER_STALE",
        ),
        (
            _reference_input_stats(snapshot_age_sec=-0.1),
            "REASON_REF_CONTRACT_SNAPSHOT_IN_FUTURE",
        ),
    ],
    ids=["malformed", "writer-stale", "future-monotonic"],
)
def test_assess_reference_input_declared_v4_fails_closed(stats, reason):
    assessed = _assess_reference_stats(stats)

    assert assessed is not None
    result, startup_grace = assessed
    assert result.status == "fail"
    assert startup_grace is False
    assert result.reason == getattr(doctor.aec, reason)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("snapshot_monotonic_ms", float("nan")),
        ("snapshot_monotonic_ms", float("inf")),
        ("snapshot_monotonic_ms", -1),
        ("snapshot_monotonic_ms", 10**1000),
        ("process_age_ms", float("nan")),
        ("process_age_ms", -1),
        ("last_frame_age_ms", float("inf")),
        ("last_frame_age_ms", -1),
        ("last_frame_age_ms", 10**1000),
        ("frames_enqueued", 1 << 64),
    ],
)
def test_assess_reference_input_rejects_untrusted_numeric_fields(field, value):
    stats = _reference_input_stats()
    stats["reference_input"][field] = value

    assessed = _assess_reference_stats(stats)

    assert assessed is not None
    result, _startup_grace = assessed
    assert result.status == "fail"
    # frames_enqueued has its own dedicated uint64 bounds check (a distinct
    # branch/reason from the shared nonnegative-float validator every other
    # field here goes through).
    assert result.reason == (
        doctor.aec.REASON_REF_CONTRACT_INVALID_FRAMES_ENQUEUED
        if field == "frames_enqueued"
        else doctor.aec.REASON_REF_CONTRACT_INVALID_NUMERIC
    )


@pytest.mark.parametrize(
    ("updated_epoch_sec", "started_epoch_sec"),
    [
        (10**12, 10**12 - 60),
        (-10**12, -10**12 - 60),
    ],
    ids=["wall-clock-forward", "wall-clock-backward"],
)
def test_assess_reference_input_ignores_wall_clock_jumps(
    updated_epoch_sec,
    started_epoch_sec,
):
    fresh = _assess_reference_stats(
        _reference_input_stats(
            updated_epoch_sec=updated_epoch_sec,
            started_epoch_sec=started_epoch_sec,
            last_frame_age_ms=100,
        )
    )
    startup = _assess_reference_stats(
        _reference_input_stats(
            updated_epoch_sec=updated_epoch_sec,
            started_epoch_sec=started_epoch_sec,
            process_age_sec=9.0,
            frames_enqueued=0,
            last_frame_age_ms=None,
        )
    )
    stale = _assess_reference_stats(
        _reference_input_stats(
            updated_epoch_sec=updated_epoch_sec,
            started_epoch_sec=started_epoch_sec,
            last_frame_age_ms=9_000,
        )
    )

    assert fresh is not None and fresh[0].status == "ok" and fresh[1] is False
    assert startup is not None and startup[0].status == "ok" and startup[1] is True
    assert stale is not None and stale[0].status == "fail"
    assert fresh[0].reason == doctor.aec.REASON_REF_RECEIVER_CURRENT
    assert startup[0].reason == doctor.aec.REASON_REF_STARTUP_GRACE
    assert stale[0].reason == doctor.aec.REASON_REF_RECEIVER_STALE


def test_assess_reference_input_young_process_gets_explicit_grace():
    assessed = _assess_reference_stats(
        _reference_input_stats(
            process_age_sec=9.9,
            frames_enqueued=0,
            last_frame_age_ms=None,
        )
    )

    assert assessed is not None
    result, startup_grace = assessed
    assert result.status == "ok"
    assert startup_grace is True
    assert result.reason == doctor.aec.REASON_REF_STARTUP_GRACE


def test_assess_reference_input_sender_active_is_not_receiver_proof():
    """`.reason` pins the stale-receiver branch; the localization text
    (what outputd's own STATUS says about the sender) is a computed
    diagnostic value from a separate helper (`_outputd_reference_
    localization`) with no other structured home, so it stays a `.detail`
    content assertion — same call this module made for the remediation-hop
    tests above."""
    assessed = _assess_reference_stats(
        _reference_input_stats(last_frame_age_ms=9_000)
    )

    assert assessed is not None
    result, _startup_grace = assessed
    assert result.status == "fail"
    assert result.reason == doctor.aec.REASON_REF_RECEIVER_STALE
    assert "sender active" in result.detail
    assert "send success is not receiver proof" in result.detail


@pytest.mark.parametrize(
    ("status", "error", "expected_detail"),
    [
        ({}, None, "missing reference_outputs"),
        (
            _active_outputd_reference_status(target="127.0.0.1:9999"),
            None,
            "udp_target='127.0.0.1:9999'",
        ),
        (
            _active_outputd_reference_status(active=False, errors=2),
            None,
            "reports UDP inactive",
        ),
        (None, "socket unavailable", "STATUS unavailable"),
    ],
)
def test_reference_input_failure_localizes_outputd_without_using_it_as_proof(
    status,
    error,
    expected_detail,
):
    assessed = doctor.aec._assess_aec_reference_input_from_stats(
        _reference_input_stats(last_frame_age_ms=9_000),
        1_000.0,
        configured_source="outputd_udp",
        expected_endpoint="127.0.0.1:9891",
        outputd_status=status,
        outputd_status_error=error,
    )

    assert assessed is not None
    result, _startup_grace = assessed
    assert result.status == "fail"
    assert result.reason == doctor.aec.REASON_REF_RECEIVER_STALE
    assert expected_detail in result.detail


def test_assess_reference_input_historical_starvation_does_not_fail_recent_ref():
    assessed = _assess_reference_stats(
        _reference_input_stats(
            last_frame_age_ms=50,
            ref_starved_frames=1_000_000,
        )
    )

    assert assessed is not None
    result, _startup_grace = assessed
    assert result.status == "ok"


@pytest.mark.parametrize("configured_source", ["alsa", "custom"])
def test_assess_reference_input_non_udp_sources_keep_journal_policy(
    configured_source,
):
    assert (
        doctor.aec._assess_aec_reference_input_from_stats(
            _reference_input_stats(),
            1_000.0,
            configured_source=configured_source,
            expected_endpoint="jasper_ref",
        )
        is None
    )


def _install_reference_health_check_fakes(
    monkeypatch,
    tmp_path: Path,
    *,
    stats: dict | str,
    journal: str,
) -> list[list[str]]:
    stats_path = tmp_path / "aec_bridge_stats.json"
    stats_path.write_text(
        stats if isinstance(stats, str) else json.dumps(stats),
        encoding="utf-8",
    )
    monkeypatch.setenv("JASPER_AEC_REF_SOURCE", "outputd_udp")
    monkeypatch.setenv("JASPER_AEC_OUTPUTD_REF_UDP_HOST", "127.0.0.1")
    monkeypatch.setenv("JASPER_AEC_OUTPUTD_REF_UDP_PORT", "9891")
    monkeypatch.setenv("JASPER_AEC_BRIDGE_STATS_PATH", str(stats_path))
    monkeypatch.setattr(doctor.aec.time, "monotonic", lambda: 1_000.0)
    monkeypatch.setattr(doctor.aec.time, "time", lambda: 50_000.0)
    monkeypatch.setattr(doctor.aec, "_parked_as_bonded_follower", lambda: False)
    calls: list[list[str]] = []

    def fake_outputd_status():
        calls.append(["outputd-status"])
        return _active_outputd_reference_status()

    monkeypatch.setattr(
        doctor.aec,
        "_read_outputd_status_for_aec_reference",
        fake_outputd_status,
    )

    def fake_run(command, **_kwargs):
        calls.append(command)
        if command == ["systemctl", "is-active", "jasper-aec-bridge.service"]:
            return SimpleNamespace(returncode=0, stdout="active\n", stderr="")
        if command[:3] == ["journalctl", "-u", "jasper-aec-bridge.service"]:
            return SimpleNamespace(returncode=0, stdout=journal, stderr="")
        raise AssertionError(f"unexpected command: {command!r}")

    monkeypatch.setattr(doctor.aec, "_run", fake_run)
    return calls


def test_check_reference_freshness_fails_with_usb_invisible_to_loopback(
    monkeypatch,
    tmp_path: Path,
):
    calls = _install_reference_health_check_fakes(
        monkeypatch,
        tmp_path,
        stats=_reference_input_stats(last_frame_age_ms=8_000),
        journal="",
    )
    monkeypatch.setattr(doctor.aec, "_loopback_playback_active", lambda: False)

    result = doctor.aec.check_aec_bridge_output_health()

    assert result.status == "fail"
    assert result.reason == doctor.aec.REASON_REF_RECEIVER_STALE
    assert not any(command[0] == "journalctl" for command in calls)
    assert sum(command[0] == "outputd-status" for command in calls) == 1


def test_check_reference_freshness_failure_cannot_be_overridden_by_rms(
    monkeypatch,
    tmp_path: Path,
):
    healthy_rms = "\n".join(
        _rms_log_line(ref=1_200, mic=2_400, aec=150, attn_db=-24.1)
        for _ in range(8)
    )
    calls = _install_reference_health_check_fakes(
        monkeypatch,
        tmp_path,
        stats=_reference_input_stats(last_frame_age_ms=8_000),
        journal=healthy_rms,
    )

    result = doctor.aec.check_aec_bridge_output_health()

    assert result.status == "fail"
    assert result.reason == doctor.aec.REASON_REF_RECEIVER_STALE
    assert "historical RMS cannot prove current receiver progress" in result.detail
    assert not any(command[0] == "journalctl" for command in calls)
    assert sum(command[0] == "outputd-status" for command in calls) == 1


@pytest.mark.parametrize("schema_version", [3, 5])
def test_check_undeclared_reference_stats_fall_back_to_journal(
    monkeypatch,
    tmp_path: Path,
    schema_version,
):
    calls = _install_reference_health_check_fakes(
        monkeypatch,
        tmp_path,
        stats=_reference_input_stats(schema_version=schema_version),
        journal="",
    )
    monkeypatch.setattr(doctor.aec, "_loopback_playback_active", lambda: False)

    result = doctor.aec.check_aec_bridge_output_health()

    assert result.status == "warn"
    assert result.reason == doctor.aec.REASON_BRIDGE_OUTPUT_NO_WINDOWS
    assert any(command[0] == "journalctl" for command in calls)
    assert not any(command[0] == "outputd-status" for command in calls)


@pytest.mark.parametrize(
    ("snapshot_age_sec", "reason"),
    [
        # Under the 30s writer-freshness limit: the snapshot itself is
        # trusted, so this is a genuinely stale RECEIVER, not a malformed
        # writer.
        (29.0, "REASON_REF_RECEIVER_STALE"),
        # Past the limit: the snapshot itself can no longer be trusted.
        (31.0, "REASON_REF_STATS_WRITER_STALE"),
    ],
    ids=["receiver-stale", "writer-stale"],
)
def test_check_declared_v4_never_ages_from_fail_into_journal_fallback(
    monkeypatch,
    tmp_path: Path,
    snapshot_age_sec,
    reason,
):
    calls = _install_reference_health_check_fakes(
        monkeypatch,
        tmp_path,
        stats=_reference_input_stats(
            snapshot_age_sec=snapshot_age_sec,
            last_frame_age_ms=100,
        ),
        journal="",
    )

    result = doctor.aec.check_aec_bridge_output_health()

    assert result.status == "fail"
    assert result.reason == getattr(doctor.aec, reason)
    assert not any(command[0] == "journalctl" for command in calls)
    assert sum(command[0] == "outputd-status" for command in calls) == 1


def test_check_malformed_declared_v4_fails_without_row_traceback(
    monkeypatch,
    tmp_path: Path,
):
    stats = _reference_input_stats()
    del stats["reference_input"]["process_age_ms"]
    calls = _install_reference_health_check_fakes(
        monkeypatch,
        tmp_path,
        stats=stats,
        journal="",
    )

    result = doctor.aec.check_aec_bridge_output_health()

    assert result.status == "fail"
    assert result.reason == doctor.aec.REASON_REF_CONTRACT_MISSING_FIELD
    assert "missing required field 'process_age_ms'" in result.detail
    assert not any(command[0] == "journalctl" for command in calls)
    assert sum(command[0] == "outputd-status" for command in calls) == 1


def test_check_oversized_json_integer_preserves_fallback_without_traceback(
    monkeypatch,
    tmp_path: Path,
):
    oversized = (
        '{"schema_version":4,"reference_input":'
        '{"snapshot_monotonic_ms":' + ("9" * 5_000) + "}}"
    )
    calls = _install_reference_health_check_fakes(
        monkeypatch,
        tmp_path,
        stats=oversized,
        journal="",
    )
    monkeypatch.setattr(doctor.aec, "_loopback_playback_active", lambda: False)

    result = doctor.aec.check_aec_bridge_output_health()

    assert result.status == "warn"
    assert result.reason == doctor.aec.REASON_BRIDGE_OUTPUT_NO_WINDOWS
    assert any(command[0] == "journalctl" for command in calls)
    assert not any(command[0] == "outputd-status" for command in calls)


def test_applied_reference_source_reads_the_v4_receiver_not_the_legacy_plan():
    """The applied source is `reference_input.source`, the v4 receiver field.

    Where the v4 block and the epoch-based `active_capture_plan` disagree,
    this module's shipped ruling is that v4 wins (see
    `test_check_fresh_v4_identity_overrides_contradictory_legacy_plan`), so
    the two are given contradictory values here and v4 must be the answer.
    """
    stats = _reference_input_stats()
    stats["active_capture_plan"]["mic_reference_identity"] = {
        "ref_source": "alsa",
    }

    assert doctor.aec._applied_reference_source(stats) == "outputd_udp"


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda s: s.pop("reference_input"), id="block-absent"),
        pytest.param(
            lambda s: s.update(reference_input="outputd_udp"),
            id="block-not-an-object",
        ),
        pytest.param(
            lambda s: s["reference_input"].pop("source"), id="source-absent",
        ),
        pytest.param(
            lambda s: s["reference_input"].update(source="  "),
            id="source-blank",
        ),
        pytest.param(
            lambda s: s["reference_input"].update(source=4),
            id="source-not-a-string",
        ),
    ],
)
def test_applied_reference_source_is_fail_soft(mutate):
    """Anything unreadable returns None so the caller falls back to env."""
    stats = _reference_input_stats()
    mutate(stats)

    assert doctor.aec._applied_reference_source(stats) is None


@pytest.mark.parametrize("stats", [None, {}, "not-a-dict", 4])
def test_applied_reference_source_tolerates_a_missing_snapshot(stats):
    """No snapshot (or a non-object one) is the rolling-deploy path."""
    assert doctor.aec._applied_reference_source(stats) is None


def test_stale_env_ref_source_still_runs_the_authoritative_check(
    monkeypatch,
    tmp_path: Path,
):
    """HS-N2: a stale `alsa` env must not skip the v4 freshness assessment.

    A box parked by a pre-P7-1 reconciler keeps `JASPER_AEC_REF_SOURCE=alsa`
    in /etc/jasper/jasper.env while the bridge converges to `outputd_udp`
    and publishes that in its snapshot. Gating on the env dropped exactly
    that box to the music-conditional journal fallback, which returns OK for
    a dead reference whenever no snd-aloop renderer lane is open.
    """
    calls = _install_reference_health_check_fakes(
        monkeypatch,
        tmp_path,
        stats=_reference_input_stats(last_frame_age_ms=8_000),
        journal="",
    )
    monkeypatch.setenv("JASPER_AEC_REF_SOURCE", "alsa")
    monkeypatch.setattr(doctor.aec, "_loopback_playback_active", lambda: False)

    result = doctor.aec.check_aec_bridge_output_health()

    assert result.status == "fail"
    assert result.reason == doctor.aec.REASON_REF_RECEIVER_STALE
    assert not any(command[0] == "journalctl" for command in calls)
    assert sum(command[0] == "outputd-status" for command in calls) == 1


def test_env_route_still_reaches_the_receiver_identity_fail(
    monkeypatch,
    tmp_path: Path,
):
    """The mirror case must keep its loud FAIL rather than being skipped.

    Env says `outputd_udp`, the receiver reports `alsa`. Resolving the route
    from the snapshot ALONE would close the gate and silently degrade to the
    journal; the env half of the OR keeps this reaching the assessor's
    runtime-identity FAIL.
    """
    stats = _reference_input_stats()
    stats["reference_input"]["source"] = "alsa"
    calls = _install_reference_health_check_fakes(
        monkeypatch,
        tmp_path,
        stats=stats,
        journal="",
    )

    result = doctor.aec.check_aec_bridge_output_health()

    assert result.status == "fail"
    assert result.reason == doctor.aec.REASON_REF_ROUTE_MISMATCH
    assert not any(command[0] == "journalctl" for command in calls)


def test_stale_env_inside_startup_grace_converges_to_ok(
    monkeypatch,
    tmp_path: Path,
):
    """The ONE case where opening the gate turns a journal FAIL into an OK.

    A bridge restarted seconds ago, a stale `alsa` env, and a journal
    window still holding the PREDECESSOR's silent-ref windows: the
    env-gated path FAILed on those windows, the OR path returns the
    assessor's <=10 s startup grace OK *before* the journal is read.

    That is convergence, not masking, and this pins it as intended: the
    grace exists precisely so a previous process's windows cannot indict
    this one, and an env-says-`outputd_udp` box has always taken this
    same path. Its sibling below asserts the self-correction.
    """
    predecessor_silent_ref = "\n".join(
        _rms_log_line(ref=0, mic=2_500, aec=2_400, attn_db=-0.4)
        for _ in range(8)
    )
    calls = _install_reference_health_check_fakes(
        monkeypatch,
        tmp_path,
        stats=_reference_input_stats(process_age_sec=3.0),
        journal=predecessor_silent_ref,
    )
    monkeypatch.setenv("JASPER_AEC_REF_SOURCE", "alsa")
    monkeypatch.setattr(doctor.aec, "_loopback_playback_active", lambda: True)

    result = doctor.aec.check_aec_bridge_output_health()

    assert result.status == "ok"
    assert result.reason == doctor.aec.REASON_REF_STARTUP_GRACE
    assert not any(command[0] == "journalctl" for command in calls)


def test_the_same_box_past_the_startup_grace_fails(
    monkeypatch,
    tmp_path: Path,
):
    """The sibling: identical inputs, only `process_age` clears the grace.

    Proves the grace OK above is bounded and self-correcting rather than
    a permanent downgrade — the same silent-ref journal now reaches the
    FAIL branch.
    """
    predecessor_silent_ref = "\n".join(
        _rms_log_line(ref=0, mic=2_500, aec=2_400, attn_db=-0.4)
        for _ in range(8)
    )
    calls = _install_reference_health_check_fakes(
        monkeypatch,
        tmp_path,
        stats=_reference_input_stats(process_age_sec=60.0),
        journal=predecessor_silent_ref,
    )
    monkeypatch.setenv("JASPER_AEC_REF_SOURCE", "alsa")
    monkeypatch.setattr(doctor.aec, "_loopback_playback_active", lambda: True)

    result = doctor.aec.check_aec_bridge_output_health()

    assert result.status == "fail"
    assert result.reason == doctor.aec.REASON_BRIDGE_OUTPUT_REF_SILENT
    assert any(command[0] == "journalctl" for command in calls)


def test_neither_route_outputd_keeps_the_journal_fallback(
    monkeypatch,
    tmp_path: Path,
):
    """Both ends off the outputd route → unchanged legacy journal policy.

    This is what stops the OR from becoming "always enforce": a box neither
    configured for nor running the outputd reference keeps the pre-existing
    fallback and never pays for an outputd STATUS read.
    """
    stats = _reference_input_stats()
    stats["reference_input"]["source"] = "alsa"
    calls = _install_reference_health_check_fakes(
        monkeypatch,
        tmp_path,
        stats=stats,
        journal="",
    )
    monkeypatch.setenv("JASPER_AEC_REF_SOURCE", "alsa")
    monkeypatch.setattr(doctor.aec, "_loopback_playback_active", lambda: False)

    result = doctor.aec.check_aec_bridge_output_health()

    assert result.status == "warn"
    assert result.reason == doctor.aec.REASON_BRIDGE_OUTPUT_NO_WINDOWS
    assert any(command[0] == "journalctl" for command in calls)
    assert not any(command[0] == "outputd-status" for command in calls)


def test_check_fresh_receiver_still_fails_silent_reference_content(
    monkeypatch,
    tmp_path: Path,
):
    silent_ref = "\n".join(
        _rms_log_line(ref=0, mic=2_500, aec=2_400, attn_db=-0.4)
        for _ in range(5)
    )
    calls = _install_reference_health_check_fakes(
        monkeypatch,
        tmp_path,
        stats=_reference_input_stats(last_frame_age_ms=100),
        journal=silent_ref,
    )
    monkeypatch.setattr(doctor.aec, "_loopback_playback_active", lambda: True)

    result = doctor.aec.check_aec_bridge_output_health()

    assert result.status == "fail"
    assert result.reason == doctor.aec.REASON_BRIDGE_OUTPUT_REF_SILENT
    assert any(command[0] == "journalctl" for command in calls)
    assert sum(command[0] == "outputd-status" for command in calls) == 1


@pytest.mark.parametrize(
    "updated_epoch_sec",
    [10**12, -(10**12)],
    ids=["future-wall-clock", "backward-wall-clock"],
)
def test_check_fresh_v4_journal_failure_uses_monotonic_identity(
    monkeypatch,
    tmp_path: Path,
    updated_epoch_sec,
):
    silent_ref = "\n".join(
        _rms_log_line(ref=0, mic=2_500, aec=2_400, attn_db=-0.4)
        for _ in range(5)
    )
    calls = _install_reference_health_check_fakes(
        monkeypatch,
        tmp_path,
        stats=_reference_input_stats(
            updated_epoch_sec=updated_epoch_sec,
            last_frame_age_ms=100,
        ),
        journal=silent_ref,
    )
    monkeypatch.setattr(doctor.aec, "_loopback_playback_active", lambda: True)

    result = doctor.aec.check_aec_bridge_output_health()

    assert result.status == "fail"
    assert result.reason == doctor.aec.REASON_BRIDGE_OUTPUT_REF_SILENT
    assert "source=outputd_udp at 127.0.0.1:9891" in result.detail
    assert "reference_outputs.udp_target='127.0.0.1:9891'" in result.detail
    assert "provenance is unavailable" not in result.detail
    assert sum(command[0] == "outputd-status" for command in calls) == 1


def test_check_fresh_v4_identity_overrides_contradictory_legacy_plan(
    monkeypatch,
    tmp_path: Path,
):
    stats = _reference_input_stats(last_frame_age_ms=100)
    stats["active_capture_plan"]["mic_reference_identity"] = {
        "ref_source": "alsa",
    }
    silent_ref = "\n".join(
        _rms_log_line(ref=0, mic=2_500, aec=2_400, attn_db=-0.4)
        for _ in range(5)
    )
    calls = _install_reference_health_check_fakes(
        monkeypatch,
        tmp_path,
        stats=stats,
        journal=silent_ref,
    )
    monkeypatch.setattr(doctor.aec, "_loopback_playback_active", lambda: True)

    result = doctor.aec.check_aec_bridge_output_health()

    assert result.status == "fail"
    assert result.reason == doctor.aec.REASON_BRIDGE_OUTPUT_REF_SILENT
    assert "source=outputd_udp at 127.0.0.1:9891" in result.detail
    assert "source=alsa" not in result.detail
    assert "pcm.jasper_capture" not in result.detail
    assert sum(command[0] == "outputd-status" for command in calls) == 1


@pytest.mark.parametrize(
    "legacy_source",
    # `alsa` is the retired source (U4 / P7-1) and gets no special
    # treatment: it reads exactly like a source doctor has never heard of.
    ["alsa", "future_transport"],
    ids=["retired-alsa", "unknown"],
)
def test_check_legacy_non_outputd_fallback_skips_status(
    monkeypatch,
    tmp_path: Path,
    legacy_source,
):
    stats = _reference_input_stats(schema_version=3)
    stats["active_capture_plan"]["mic_reference_identity"] = {
        "ref_source": legacy_source,
    }
    silent_ref = "\n".join(
        _rms_log_line(ref=0, mic=2_500, aec=2_400, attn_db=-0.4)
        for _ in range(5)
    )
    calls = _install_reference_health_check_fakes(
        monkeypatch,
        tmp_path,
        stats=stats,
        journal=silent_ref,
    )
    monkeypatch.setattr(doctor.aec, "_loopback_playback_active", lambda: True)

    result = doctor.aec.check_aec_bridge_output_health()

    assert result.status == "fail"
    assert result.reason == doctor.aec.REASON_BRIDGE_OUTPUT_REF_SILENT
    assert "cannot safely name the failed hop" in result.detail
    assert not any(command[0] == "outputd-status" for command in calls)


def test_check_fresh_receiver_and_one_healthy_rms_window_is_ok(
    monkeypatch,
    tmp_path: Path,
):
    journal = "\n".join(
        [
            *(
                _rms_log_line(ref=0, mic=2_500, aec=2_400, attn_db=-0.4)
                for _ in range(5)
            ),
            _rms_log_line(ref=900, mic=2_500, aec=180, attn_db=-22.8),
        ]
    )
    calls = _install_reference_health_check_fakes(
        monkeypatch,
        tmp_path,
        stats=_reference_input_stats(last_frame_age_ms=100),
        journal=journal,
    )
    monkeypatch.setattr(doctor.aec, "_loopback_playback_active", lambda: True)

    result = doctor.aec.check_aec_bridge_output_health()

    assert result.status == "ok"
    assert result.reason == doctor.aec.REASON_BRIDGE_OUTPUT_REF_PROVEN_HEALTHY
    assert not any(command[0] == "outputd-status" for command in calls)


# ----------------------------------------- DTLN-aec engine health assessment


def _dtln_loaded_line(size: int = 256) -> str:
    """Synthesize the bridge's successful-load log line in journal
    `--output=cat` format. Matches jasper/cli/aec_bridge.py:~675."""
    return (
        f"2026-05-23 12:47:29,197 aec-bridge INFO "
        f"DTLN-aec engine enabled: size={size}, udp out=127.0.0.1:9878"
    )


def _dtln_failed_line(reason: str = "No such file or directory") -> str:
    """Synthesize the bridge's failed-load log line."""
    return (
        f"2026-05-23 12:47:29,197 aec-bridge WARNING "
        f"JASPER_AEC_DTLN_ENABLED set but DTLN couldn't load: {reason}. "
        f"Continuing with AEC3 only."
    )


def test_assess_dtln_engine_loaded_returns_ok():
    """Happy path: bridge logged a successful engine-init line.
    Doctor reports the engine size for the operator to confirm — a value
    with no other structured home, so it stays a `.detail` assertion."""
    r = doctor._assess_dtln_engine(_dtln_loaded_line(size=256))
    assert r.status == "ok"
    assert r.reason == doctor.aec.REASON_DTLN_LOADED_FROM_JOURNAL
    assert "size=256" in r.detail


def test_assess_dtln_engine_load_failed_returns_fail():
    """The regression we exist to catch: JASPER_AEC_DTLN_ENABLED=1
    but the engine couldn't load (e.g. /var/lib/jasper/dtln/*.onnx
    missing because install.sh's download failed and the manual SCP
    step didn't happen). Without this check, the operator would
    spend a week analyzing 'DTLN never fires' data without realizing
    the engine never ran."""
    r = doctor._assess_dtln_engine(
        _dtln_failed_line(reason="DTLN ONNX models missing in /var/lib/jasper/dtln")
    )
    assert r.status == "fail"
    assert r.reason == doctor.aec.REASON_DTLN_LOAD_FAILED
    assert "/var/lib/jasper/dtln" in r.detail  # actionable path, the failed reason


def test_assess_dtln_engine_no_marker_warns():
    """Bridge running but no engine-init marker in the journal
    window — probably means the bridge hasn't restarted since the
    env var was set."""
    r = doctor._assess_dtln_engine("some unrelated log lines\nbridge boot\n")
    assert r.status == "warn"
    assert r.reason == doctor.aec.REASON_DTLN_NO_INIT_LINE


def test_assess_dtln_engine_picks_most_recent_marker():
    """If the journal window straddles a bridge restart that fixed
    an earlier failure, the LATER successful-load line wins. Reverse
    iteration in _assess_dtln_engine ensures we evaluate newest-first."""
    journal = "\n".join(
        [
            _dtln_failed_line(reason="onnxruntime import failed"),
            "(... operator fixed the venv ...)",
            _dtln_loaded_line(size=256),
        ]
    )
    r = doctor._assess_dtln_engine(journal)
    assert r.status == "ok"
    assert r.reason == doctor.aec.REASON_DTLN_LOADED_FROM_JOURNAL


def test_check_dtln_skips_when_env_disabled(monkeypatch):
    """When JASPER_AEC_DTLN_ENABLED is unset (legacy dual-stream
    config), the whole check should skip cleanly without running
    journalctl. This is the common case for non-triple-stream
    installs and must not flap."""
    monkeypatch.delenv("JASPER_AEC_DTLN_ENABLED", raising=False)
    r = doctor.check_aec_bridge_dtln_engine()
    assert r.status == "ok"
    assert r.reason == doctor.aec.REASON_DTLN_DISABLED


def _install_fake_dtln_registry(monkeypatch, tmp_path: Path):
    from jasper.aec_engines import dtln_models

    expected = hashlib.sha256(b"model").hexdigest()

    class _FakeEntry:
        def __init__(self, size: int):
            self.size = size

        def files(self, base_dir=tmp_path):
            base = Path(base_dir)
            return [
                (
                    base / f"dtln_aec_{self.size}_1.onnx",
                    "https://example.invalid/1",
                    expected,
                ),
                (
                    base / f"dtln_aec_{self.size}_2.onnx",
                    "https://example.invalid/2",
                    expected,
                ),
            ]

    entries = {
        128: _FakeEntry(128),
        256: _FakeEntry(256),
    }
    monkeypatch.setattr(dtln_models, "DEFAULT_SIZE", 256)
    monkeypatch.setattr(dtln_models, "REGISTRY", tuple(entries.values()))
    monkeypatch.setattr(dtln_models, "by_size", lambda size: entries.get(size))
    monkeypatch.setenv("JASPER_DTLN_MODEL_DIR", str(tmp_path))


def test_check_dtln_fails_when_enabled_model_file_missing(monkeypatch, tmp_path: Path):
    _install_fake_dtln_registry(monkeypatch, tmp_path)
    monkeypatch.setenv("JASPER_AEC_DTLN_ENABLED", "1")
    (tmp_path / "dtln_aec_256_1.onnx").write_bytes(b"model")

    r = doctor.check_aec_bridge_dtln_engine()

    assert r.status == "fail"
    assert r.reason == doctor.aec.REASON_DTLN_MODEL_FILES_MISSING
    assert "dtln_aec_256_2.onnx" in r.detail
    assert "deploy/install.sh" in r.detail


def test_check_dtln_fails_when_enabled_model_hash_mismatches(
    monkeypatch,
    tmp_path: Path,
):
    _install_fake_dtln_registry(monkeypatch, tmp_path)
    monkeypatch.setenv("JASPER_AEC_DTLN_ENABLED", "1")
    (tmp_path / "dtln_aec_256_1.onnx").write_bytes(b"model")
    (tmp_path / "dtln_aec_256_2.onnx").write_bytes(b"wrong-model")

    r = doctor.check_aec_bridge_dtln_engine()

    assert r.status == "fail"
    assert r.reason == doctor.aec.REASON_DTLN_MODEL_HASH_MISMATCH
    assert "dtln_aec_256_2.onnx" in r.detail
    assert "deploy/install.sh" in r.detail


def test_check_dtln_uses_configured_model_size(monkeypatch, tmp_path: Path):
    _install_fake_dtln_registry(monkeypatch, tmp_path)
    monkeypatch.setenv("JASPER_AEC_DTLN_ENABLED", "1")
    monkeypatch.setenv("JASPER_AEC_DTLN_SIZE", "128")
    monkeypatch.setattr(
        doctor.aec,
        "_run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout="inactive"),
    )
    (tmp_path / "dtln_aec_128_1.onnx").write_bytes(b"model")
    (tmp_path / "dtln_aec_128_2.onnx").write_bytes(b"model")

    r = doctor.check_aec_bridge_dtln_engine()

    assert r.status == "ok"
    assert r.reason == doctor.aec.REASON_DTLN_BRIDGE_NOT_RUNNING


def test_check_dtln_fails_when_configured_model_size_is_invalid(
    monkeypatch,
    tmp_path: Path,
):
    _install_fake_dtln_registry(monkeypatch, tmp_path)
    monkeypatch.setenv("JASPER_AEC_DTLN_ENABLED", "1")
    monkeypatch.setenv("JASPER_AEC_DTLN_SIZE", "large")

    r = doctor.check_aec_bridge_dtln_engine()

    assert r.status == "fail"
    assert r.reason == doctor.aec.REASON_DTLN_SIZE_NOT_INTEGER


def test_check_dtln_fails_when_configured_model_size_is_not_registered(
    monkeypatch,
    tmp_path: Path,
):
    _install_fake_dtln_registry(monkeypatch, tmp_path)
    monkeypatch.setenv("JASPER_AEC_DTLN_ENABLED", "1")
    monkeypatch.setenv("JASPER_AEC_DTLN_SIZE", "512")

    r = doctor.check_aec_bridge_dtln_engine()

    assert r.status == "fail"
    assert r.reason == doctor.aec.REASON_DTLN_SIZE_NOT_REGISTERED
    assert "128" in r.detail
    assert "256" in r.detail

# --------------------------------- Chip-AEC alignment: the record, unaltered
# ---------------------------------------------------------------------------


def _alignment_env(status: str, selection: str = "xvf_chip_aec") -> RuntimeAecEnv:
    return RuntimeAecEnv(
        chip_enabled=True,
        chip_aec_150_device="udp:9887",
        chip_aec_210_device="udp:9888",
        chip_aec_alignment=chip_aec_health.AlignmentHealth(
            status=status,
            reason="published reason",
            action=chip_aec_health.ACTION_RECOMMISSION,
            selection=selection,
        ),
    )


@pytest.mark.parametrize(
    "status, expected_result, expected_reason, expects_action",
    [
        (
            chip_aec_health.STATUS_READY, "ok",
            "REASON_ALIGNMENT_READY", False,
        ),
        (
            chip_aec_health.STATUS_DISCLOSED_STALE, "warn",
            "REASON_ALIGNMENT_NOT_READY", True,
        ),
        (
            chip_aec_health.STATUS_CHECKING, "warn",
            "REASON_ALIGNMENT_NOT_READY", True,
        ),
        (
            chip_aec_health.STATUS_FAULT, "warn",
            "REASON_ALIGNMENT_NOT_READY", True,
        ),
        (
            chip_aec_health.STATUS_UNAVAILABLE, "warn",
            "REASON_ALIGNMENT_NOT_READY", True,
        ),
        ("", "ok", "REASON_ALIGNMENT_NO_VERDICT", False),
    ],
)
def test_chip_aec_alignment_row_follows_the_published_record(
    status, expected_result, expected_reason, expects_action,
):
    """One row per published verdict: the record's status decides the doctor
    result, and the operator only gets an action when it is not ready. The
    action hint itself (`chip_aec_health.ACTION_RECOMMISSION`) is a known
    cross-module constant, not free prose, so checking its presence stays a
    content assertion alongside the reason code."""

    result = doctor.aec._assess_chip_aec_alignment(
        _alignment_env(status), "xvf_chip_aec",
    )

    assert result.name == "Chip-AEC alignment"
    assert result.status == expected_result
    assert result.reason == getattr(doctor.aec, expected_reason)
    assert (chip_aec_health.ACTION_RECOMMISSION in result.detail) is expects_action


@pytest.mark.parametrize(
    "stamp, reading_selection, owned",
    [
        ("xvf_chip_aec", "xvf_chip_aec", True),
        ("xvf_chip_aec", "custom", False),
        ("xvf_chip_aec", "xvf_software_aec3", False),
        ("xvf_chip_aec_testing", "xvf_chip_aec", False),
        ("", "xvf_chip_aec", True),
        ("", "custom", False),
    ],
)
def test_chip_aec_alignment_row_serves_only_the_stamped_selection(
    stamp, reading_selection, owned,
):
    """A leftover record from a path the box has left is not a verdict here:
    an operator who toggles a leg lands on `custom` and must not keep getting
    a warning with a commission command behind it."""

    result = doctor.aec._assess_chip_aec_alignment(
        _alignment_env(chip_aec_health.STATUS_FAULT, selection=stamp),
        reading_selection,
    )

    assert result.status == ("warn" if owned else "ok")
    assert result.reason == (
        doctor.aec.REASON_ALIGNMENT_NOT_READY
        if owned
        else doctor.aec.REASON_ALIGNMENT_NO_VERDICT
    )
    assert (chip_aec_health.ACTION_RECOMMISSION in result.detail) is owned


# ---------------------------------------------------------------------------
# aec_mode.env — one parser (env_load.parse_env_file) behind the doctor
# helpers and /aec's _read_aec_state
# ---------------------------------------------------------------------------


# (file body or None for missing, (mode, profile, JASPER_WAKE_LEG_RAW))
_MODE_FILE_CASES = {
    "missing_file": (None, ("auto", "", True)),
    "empty_file": ("", ("auto", "", True)),
    "quoted_values": (
        "JASPER_AEC_MODE=\"disabled\"\nJASPER_WAKE_LEG_RAW='0'\n"
        "JASPER_AUDIO_INPUT_PROFILE=\"xvf_chip_aec\"\n",
        ("disabled", "xvf_chip_aec", False),
    ),
    "export_lines_ignored": (
        "export JASPER_AEC_MODE=disabled\nexport JASPER_WAKE_LEG_RAW=0\n",
        ("auto", "", True),
    ),
    "trailing_comment_kept_malformed_bool_defaults": (
        "JASPER_AEC_MODE=disabled # note\nJASPER_WAKE_LEG_RAW=0 # note\n",
        ("disabled # note", "", True),
    ),
    "duplicate_key_last_wins": (
        "JASPER_AEC_MODE=disabled\nJASPER_WAKE_LEG_RAW=0\n"
        "JASPER_AEC_MODE=auto\nJASPER_WAKE_LEG_RAW=1\n",
        ("auto", "", True),
    ),
    "empty_value": ("JASPER_AEC_MODE=\nJASPER_WAKE_LEG_RAW=\n", ("auto", "", False)),
    "space_before_equals_matched": ("JASPER_AEC_MODE = disabled\n", ("disabled", "", True)),
    "unbalanced_quote_kept_verbatim": ("JASPER_AEC_MODE=\"disabled\n", ('"disabled', "", True)),
}


@pytest.mark.parametrize(
    ("body", "expected"), _MODE_FILE_CASES.values(), ids=_MODE_FILE_CASES.keys(),
)
def test_aec_mode_file_readers_share_one_parser(
    tmp_path, monkeypatch, body, expected,
):
    path = tmp_path / "aec_mode.env"
    if body is not None:
        path.write_text(body)
    monkeypatch.setattr(doctor.aec, "DEFAULT_AEC_MODE_PATH", path)
    monkeypatch.setattr(aec_endpoints, "_AEC_MODE_FILE", str(path))

    assert (
        doctor.aec._aec_mode_setting(),
        doctor.aec._aec_profile_setting(),
        doctor.aec._wake_leg_setting("JASPER_WAKE_LEG_RAW", True),
    ) == expected
    state = aec_endpoints._read_aec_state()
    assert (state["mode"], state["leg_raw"]) == (expected[0], expected[2])


# ---------------------------------------------------------------------------
# Audio profile runtime truth — shared classifier used by /aec and doctor
# ---------------------------------------------------------------------------


def test_audio_profile_doctor_check_reports_active_chip_profile(monkeypatch):
    monkeypatch.setattr(doctor.aec, "_aec_mode_setting", lambda: "auto")
    settings = {
        "JASPER_WAKE_LEG_RAW": True,
        "JASPER_WAKE_LEG_DTLN": False,
        "JASPER_WAKE_LEG_CHIP_AEC": True,
    }
    monkeypatch.setattr(
        doctor.aec,
        "_wake_leg_setting",
        lambda key, default: settings.get(key, default),
    )

    status = doctor._audio_profile_status_for_doctor(
        bridge_active=True,
        env={
            "JASPER_AUDIO_DAC_ID": "hifiberry_dac8x",
            "JASPER_MIC_DEVICE": "udp:9876",
            "JASPER_AEC_MIC_DEVICE": "Array",
            "JASPER_AEC_CHIP_AEC_ENABLED": "1",
            "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS": "ready",
            "JASPER_MIC_DEVICE_CHIP_AEC_150": "udp:9887",
            "JASPER_MIC_DEVICE_CHIP_AEC_210": "udp:9888",
        },
        mic_probe=MicProbe(
            xvf_present=True,
            capture_channels=6,
            recommended_channels=6,
            variant_id="xvf3800_legacy_square_6ch",
            geometry="square",
            chip_beam_plan="xvf_square_fixed_150_210",
            chip_aec_supported=True,
        ),
    )
    result = doctor._assess_audio_profile(status)

    assert result.status == "ok"
    assert result.reason == doctor.aec.REASON_AUDIO_PROFILE_OK
    assert "requested=xvf_chip_aec" in result.detail
    assert "active=xvf_chip_aec" in result.detail
    assert "Chip AEC 150 beam via :9876" in result.detail


def test_aec_bridge_running_does_not_re_render_the_audio_profile(monkeypatch):
    """A running bridge is one fact; which AEC it carries belongs to the
    Audio profile / Chip-AEC alignment rows, so this check must not rebuild
    the profile status (a mic-firmware probe) to say it."""

    def fake_run(cmd, **kwargs):
        if cmd == ["systemctl", "is-active", "jasper-aec-bridge.service"]:
            return SimpleNamespace(returncode=0, stdout="active\n", stderr="")
        if cmd == ["systemctl", "is-enabled", "jasper-aec-bridge.service"]:
            return SimpleNamespace(returncode=0, stdout="enabled\n", stderr="")
        raise AssertionError(f"unexpected command: {cmd!r}")

    def unexpected_status(**_kwargs):
        raise AssertionError("the running branch must not re-render /aec")

    monkeypatch.setattr(doctor.aec, "_parked_as_bonded_follower", lambda: False)
    monkeypatch.setattr(doctor.aec, "_run", fake_run)
    monkeypatch.setattr(
        doctor.aec, "_audio_profile_status_for_doctor", unexpected_status,
    )

    result = doctor.aec.check_aec_bridge_running()

    assert result.status == "ok"
    assert result.reason == doctor.aec.REASON_BRIDGE_RUNNING


def test_aec_bridge_down_during_commissioning_is_intentional_not_a_failure(
    monkeypatch,
):
    """The commissioner stops the AEC stack for its audible run; the doctor
    must report that as the intended state, not a red bridge failure with a
    restart remedy."""

    def fake_run(cmd, **kwargs):
        if cmd == ["systemctl", "is-active", "jasper-aec-bridge.service"]:
            return SimpleNamespace(returncode=3, stdout="inactive\n", stderr="")
        if cmd == ["systemctl", "is-enabled", "jasper-aec-bridge.service"]:
            return SimpleNamespace(returncode=0, stdout="enabled\n", stderr="")
        if cmd == ["systemctl", "is-active", "jasper-aec-commission.service"]:
            return SimpleNamespace(
                returncode=0, stdout="activating\n", stderr="",
            )
        raise AssertionError(f"unexpected command: {cmd!r}")

    monkeypatch.setattr(doctor.aec, "_parked_as_bonded_follower", lambda: False)
    monkeypatch.setattr(doctor.aec, "_run", fake_run)

    result = doctor.aec.check_aec_bridge_running()

    assert result.status == "ok"
    assert result.reason == doctor.aec.REASON_BRIDGE_COMMISSIONING


def test_aec_bridge_down_separates_a_withheld_verdict_from_a_dead_bridge(
    monkeypatch, tmp_path
):
    """ADR-0224. Without the ready marker systemd never even runs the start
    job, so a down bridge is a reconciler problem; with it, the bridge itself
    failed. Same severity, different diagnosis and different remedy — the row
    has to name which."""
    from jasper.mics import xvf3800

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["systemctl", "is-active"]:
            return SimpleNamespace(returncode=3, stdout="inactive\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="enabled\n", stderr="")

    marker = tmp_path / "aec-bridge-ready"
    monkeypatch.setenv("JASPER_AEC_BRIDGE_READY_MARKER", str(marker))
    monkeypatch.setattr(doctor.aec, "_parked_as_bonded_follower", lambda: False)
    monkeypatch.setattr(doctor.aec, "_run", fake_run)
    monkeypatch.setattr(doctor.aec, "_aec_mode_setting", lambda: "auto")
    monkeypatch.setattr(
        xvf3800,
        "capture_channels",
        lambda: xvf3800.RECOMMENDED_FIRMWARE.capture_channels,
    )

    withheld = doctor.aec.check_aec_bridge_running()
    marker.write_text("reason=systemd\n")
    admitted = doctor.aec.check_aec_bridge_running()

    assert withheld.status == "fail"
    assert admitted.status == "fail"
    assert withheld.reason == doctor.aec.REASON_BRIDGE_DOWN_READY_ABSENT
    assert admitted.reason == doctor.aec.REASON_BRIDGE_DOWN_READY_PRESENT
    # The marker path itself is a computed value with no other structured
    # home (it comes from JASPER_AEC_BRIDGE_READY_MARKER), so it stays a
    # `.detail` content assertion.
    assert str(marker) in withheld.detail


def test_audio_profile_doctor_check_warns_when_runtime_env_pending(monkeypatch):
    monkeypatch.setattr(doctor.aec, "_aec_mode_setting", lambda: "auto")
    settings = {
        "JASPER_WAKE_LEG_RAW": True,
        "JASPER_WAKE_LEG_DTLN": False,
        "JASPER_WAKE_LEG_CHIP_AEC": True,
    }
    monkeypatch.setattr(
        doctor.aec,
        "_wake_leg_setting",
        lambda key, default: settings.get(key, default),
    )

    status = doctor._audio_profile_status_for_doctor(
        bridge_active=True,
        env={
            "JASPER_AUDIO_DAC_ID": "hifiberry_dac8x",
            "JASPER_MIC_DEVICE": "udp:9876",
            "JASPER_AEC_MIC_DEVICE": "Array",
            "JASPER_AEC_CHIP_AEC_ENABLED": "0",
            "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS": "fault",
            "JASPER_AEC_CHIP_AEC_ALIGNMENT_REASON": (
                "chip-AEC bridge failed after alignment reapply"
            ),
            "JASPER_AEC_CHIP_AEC_ALIGNMENT_ACTION": (
                "Inspect jasper-aec-bridge, then run the reconciler"
            ),
            "JASPER_MIC_DEVICE_CHIP_AEC_150": "",
            "JASPER_MIC_DEVICE_CHIP_AEC_210": "",
        },
        mic_probe=MicProbe(
            xvf_present=True,
            capture_channels=6,
            recommended_channels=6,
            variant_id="xvf3800_legacy_square_6ch",
            geometry="square",
            chip_beam_plan="xvf_square_fixed_150_210",
        ),
    )
    result = doctor._assess_audio_profile(status)

    assert result.status == "warn"
    assert result.reason == doctor.aec.REASON_AUDIO_PROFILE_NEEDS_ATTENTION
    assert "active=none" in result.detail
    # The reconciler's own reason/action belong to the Chip-AEC alignment row.
    assert "chip-AEC bridge failed after alignment reapply" not in result.detail
    assert "Inspect jasper-aec-bridge" not in result.detail


def test_audio_profile_doctor_check_names_stale_saved_aec_card(monkeypatch):
    monkeypatch.setattr(doctor.aec, "_aec_mode_setting", lambda: "auto")
    settings = {
        "JASPER_WAKE_LEG_RAW": False,
        "JASPER_WAKE_LEG_DTLN": False,
        "JASPER_WAKE_LEG_CHIP_AEC": True,
    }
    monkeypatch.setattr(
        doctor.aec,
        "_wake_leg_setting",
        lambda key, default: settings.get(key, default),
    )

    status = doctor._audio_profile_status_for_doctor(
        bridge_active=False,
        env={
            "JASPER_AUDIO_DAC_ID": "hifiberry_dac8x",
            "JASPER_MIC_DEVICE": "udp:9876",
            "JASPER_AEC_MIC_DEVICE": "L16K6Ch",
            "JASPER_AEC_CHIP_AEC_ENABLED": "0",
        },
        mic_probe=MicProbe(
            xvf_present=True,
            capture_channels=6,
            recommended_channels=6,
            alsa_card_name="Array",
            variant_id="xvf3800_legacy_square_6ch",
            geometry="square",
            chip_beam_plan="xvf_square_fixed_150_210",
        ),
    )
    result = doctor._assess_audio_profile(status)

    assert result.status == "warn"
    assert result.reason == doctor.aec.REASON_AUDIO_PROFILE_NEEDS_ATTENTION
    assert "Configured AEC mic L16K6Ch" in result.detail
    assert "detected XVF card Array" in result.detail


def test_audio_validation_advisory_ok_when_chip_aec_not_requested():
    result = doctor._assess_audio_validation_summary(
        {
            "state": "missing",
            "status": "unknown",
            "artifact_path": "/var/lib/jasper/audio-validation",
            "reason": "artifact not found",
        },
        requested_profile="xvf_software_aec3",
    )

    assert result.status == "ok"
    assert result.reason == doctor.aec.REASON_VALIDATION_NOT_CHIP_PROFILE


def test_audio_validation_warns_when_chip_aec_requested_and_missing():
    result = doctor._assess_audio_validation_summary(
        {
            "state": "missing",
            "status": "unknown",
            "artifact_path": "/var/lib/jasper/audio-validation",
            "reason": "artifact not found",
        },
        requested_profile="xvf_chip_aec",
    )

    assert result.status == "warn"
    assert result.reason == doctor.aec.REASON_VALIDATION_ADVISORY
    assert "sudo jasper-audio-validate --stdout" in result.detail


def test_audio_validation_suggests_hardware_runner_when_ready_for_passive_evidence():
    result = doctor._assess_audio_validation_summary(
        {
            "state": "current",
            "status": "warn",
            "recommendation": "run_hardware_validation",
            "artifact_path": "/var/lib/jasper/audio-validation/latest.json",
        },
        requested_profile="xvf_chip_aec",
    )

    assert result.status == "warn"
    assert result.reason == doctor.aec.REASON_VALIDATION_ADVISORY
    assert (
        "sudo jasper-audio-hw-validate --duration-seconds 10 --stdout" in result.detail
    )


def test_audio_validation_suggests_hardware_runner_for_drift_delay_recommendation():
    result = doctor._assess_audio_validation_summary(
        {
            "state": "current",
            "status": "warn",
            "recommendation": "run_drift_delay_validation",
            "artifact_path": "/var/lib/jasper/audio-validation/latest.json",
        },
        requested_profile="xvf_chip_aec",
    )

    assert result.status == "warn"
    assert result.reason == doctor.aec.REASON_VALIDATION_ADVISORY
    assert (
        "sudo jasper-audio-hw-validate --duration-seconds 10 --stdout" in result.detail
    )


@pytest.mark.parametrize(
    ("dac_id", "check_overrides", "expected_status"),
    [
        ("hifiberry_dac8x", {}, "ok"),
        ("apple_usb_c_dongle", {}, "ok"),
        ("innomaker_hifi_amp_pro", {}, "warn"),
        ("unknown", {}, "warn"),
        ("apple_usb_c_dongle", {"chip_convergence": "fail"}, "warn"),
    ],
)
def test_audio_validation_passive_evidence_follows_dac_approval(
    dac_id, check_overrides, expected_status
):
    statuses = {
        name: "pass" for name in doctor._CHIP_AEC_PASSIVE_REQUIRED_CHECKS
    }
    statuses.update({"bridge_counters": "warn", "measured_drift_delay": "not_run"})
    statuses.update(check_overrides)
    result = doctor._assess_audio_validation_summary(
        {
            "state": "current",
            "status": "warn",
            "recommendation": "run_drift_delay_validation",
            "artifact_path": "/var/lib/jasper/audio-validation/latest.json",
            "hardware": {"mic_id": "xvf3800", "dac_id": dac_id},
            "check_statuses": statuses,
        },
        requested_profile="xvf_chip_aec",
    )

    assert result.status == expected_status
    assert result.reason == (
        doctor.aec.REASON_VALIDATION_PASSIVE_EVIDENCE
        if expected_status == "ok"
        else doctor.aec.REASON_VALIDATION_ADVISORY
    )
    if expected_status == "ok":
        assert dac_id in result.detail
        assert "xvf3800" in result.detail


def test_audio_validation_readiness_filters_current_hardware(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        doctor.aec,
        "_audio_profile_status_for_doctor",
        lambda: {"audio_profile": {"requested": "xvf_chip_aec"}},
    )
    monkeypatch.setattr(
        doctor.aec,
        "_shared_parse_env_file",
        lambda _path: {"JASPER_AUDIO_DAC_ID": "apple_usb_c_dongle"},
    )
    monkeypatch.setattr(
        doctor.aec,
        "_audio_validation_filter_kwargs",
        lambda **kwargs: {
            "requested_profile": kwargs["requested_profile"],
            "mic_id": "xvf3800",
            "dac_id": "apple_usb_c_dongle",
        },
    )

    def fake_summary(**kwargs):
        captured.update(kwargs)
        return {
            "state": "current",
            "status": "pass",
            "artifact_path": "/var/lib/jasper/audio-validation/latest.json",
        }

    monkeypatch.setattr(doctor.aec, "_audio_validation_summary", fake_summary)

    result = doctor.check_audio_validation_readiness()

    assert result.status == "ok"
    assert result.reason == doctor.aec.REASON_VALIDATION_CURRENT_PASS
    assert captured == {
        "requested_profile": "xvf_chip_aec",
        "mic_id": "xvf3800",
        "dac_id": "apple_usb_c_dongle",
    }


# DTLN engine — bridge stats snapshot surface (journal-independent)
# ---------------------------------------------------------------------------


def _dtln_stats(enabled: bool, loaded: bool, error=None, age_sec: float = 1.0):
    import time as _time

    return {
        "schema_version": 1,
        "updated_epoch_sec": _time.time() - age_sec,
        "leg_engines": {
            "dtln": {"enabled": enabled, "loaded": loaded, "error": error},
        },
    }


def test_assess_dtln_stats_loaded_returns_ok():
    import time as _time

    r = doctor.aec._assess_dtln_engine_from_stats(
        _dtln_stats(enabled=True, loaded=True),
        _time.time(),
    )
    assert r is not None and r.status == "ok"
    assert r.reason == doctor.aec.REASON_DTLN_LOADED_FROM_STATS


def test_assess_dtln_stats_load_failure_returns_fail_with_detail():
    import time as _time

    r = doctor.aec._assess_dtln_engine_from_stats(
        _dtln_stats(enabled=True, loaded=False, error="onnx missing"),
        _time.time(),
    )
    assert r is not None and r.status == "fail"
    assert r.reason == doctor.aec.REASON_DTLN_ENGINE_UNAVAILABLE
    assert "onnx missing" in r.detail
    assert ":9878" in r.detail  # names the unfed leg voice listens on


def test_assess_dtln_stats_bridge_started_without_leg_warns():
    import time as _time

    r = doctor.aec._assess_dtln_engine_from_stats(
        _dtln_stats(enabled=False, loaded=False),
        _time.time(),
    )
    assert r is not None and r.status == "warn"
    assert r.reason == doctor.aec.REASON_DTLN_NOT_STARTED_WITH_LEG
    # A hand-set JASPER_AEC_DTLN_ENABLED=1 under the chip-AEC profile is
    # NOT a stale-restart problem — the chip profile never loads DTLN. The
    # message must point at checking the active input profile, not only at
    # restarting the bridge — a value with no other structured home.
    assert "input profile" in r.detail
    assert "xvf_chip_aec" in r.detail


def test_assess_dtln_stats_stale_or_legacy_falls_back():
    import time as _time

    now = _time.time()
    # Stale snapshot (dead/old bridge process) → journal fallback.
    assert (
        doctor.aec._assess_dtln_engine_from_stats(
            _dtln_stats(enabled=True, loaded=True, age_sec=120.0),
            now,
        )
        is None
    )
    # Pre-leg_engines bridge build → journal fallback.
    assert (
        doctor.aec._assess_dtln_engine_from_stats(
            {"updated_epoch_sec": now},
            now,
        )
        is None
    )


def test_check_dtln_prefers_stats_snapshot_over_journal(
    monkeypatch,
    tmp_path: Path,
):
    """End-to-end: with a fresh stats snapshot reporting a load
    failure, the check fails from the snapshot and never shells out
    to journalctl (whose 10-min window would miss an old failure)."""
    _install_fake_dtln_registry(monkeypatch, tmp_path)
    monkeypatch.setenv("JASPER_AEC_DTLN_ENABLED", "1")
    (tmp_path / "dtln_aec_256_1.onnx").write_bytes(b"model")
    (tmp_path / "dtln_aec_256_2.onnx").write_bytes(b"model")
    stats_path = tmp_path / "aec_bridge_stats.json"
    stats_path.write_text(
        json.dumps(
            _dtln_stats(enabled=True, loaded=False, error="no onnxruntime"),
        )
    )
    monkeypatch.setenv("JASPER_AEC_BRIDGE_STATS_PATH", str(stats_path))

    def _fake_run(cmd, **kwargs):
        if cmd[0] == "systemctl":
            return SimpleNamespace(stdout="active", stderr="", returncode=0)
        raise AssertionError(f"unexpected subprocess: {cmd}")

    monkeypatch.setattr(doctor.aec, "_run", _fake_run)

    r = doctor.check_aec_bridge_dtln_engine()

    assert r.status == "fail"
    assert r.reason == doctor.aec.REASON_DTLN_ENGINE_UNAVAILABLE
    assert "no onnxruntime" in r.detail

# --- Optional enhanced AEC: requested-only advisory -----------------


def test_enhanced_aec_doctor_is_quiet_and_cheap_when_not_requested(monkeypatch):
    monkeypatch.setattr(
        doctor.aec.enhanced_aec,
        "read_intent",
        lambda: {"requested": False},
    )

    def unexpected_status(**_kwargs):
        raise AssertionError("status/fingerprint work must stay requested-only")

    monkeypatch.setattr(doctor.aec.enhanced_aec, "status", unexpected_status)

    result = doctor.check_enhanced_aec()

    assert result.status == "ok"
    assert result.reason == doctor.aec.REASON_ENHANCED_AEC_NOT_REQUESTED


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        ("installed", "REASON_ENHANCED_AEC_INSTALLED"),
        ("not_needed", "REASON_ENHANCED_AEC_INSTALLED"),
        ("installing", "REASON_ENHANCED_AEC_INSTALLING"),
    ],
)
def test_enhanced_aec_doctor_accepts_non_actionable_states(
    monkeypatch,
    state,
    reason,
):
    monkeypatch.setattr(
        doctor.aec.enhanced_aec,
        "read_intent",
        lambda: {"requested": True},
    )
    monkeypatch.setattr(
        doctor.aec,
        "_audio_profile_status_for_doctor",
        lambda: {"audio_profile": {"active": "xvf_software_aec3"}},
    )
    monkeypatch.setattr(
        doctor.aec,
        "_run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [],
            0,
            stdout="inactive\n",
            stderr="",
        ),
    )
    monkeypatch.setattr(
        doctor.aec.enhanced_aec,
        "status",
        lambda **_kwargs: {"state": state, "detail": f"state={state}"},
    )

    result = doctor.check_enhanced_aec()

    assert result.status == "ok"
    assert result.reason == getattr(doctor.aec, reason)
    assert result.detail == f"state={state}"


@pytest.mark.parametrize(
    "state",
    ["not_installed", "failed", "stale", "unavailable"],
)
def test_enhanced_aec_doctor_warns_only_after_request(monkeypatch, state):
    monkeypatch.setattr(
        doctor.aec.enhanced_aec,
        "read_intent",
        lambda: {"requested": True},
    )
    monkeypatch.setattr(
        doctor.aec,
        "_audio_profile_status_for_doctor",
        lambda: {"audio_profile": {"active": "xvf_software_aec3"}},
    )
    monkeypatch.setattr(
        doctor.aec,
        "_run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [],
            0,
            stdout="inactive\n",
            stderr="",
        ),
    )
    monkeypatch.setattr(
        doctor.aec.enhanced_aec,
        "status",
        lambda **_kwargs: {"state": state, "detail": f"state={state}"},
    )

    result = doctor.check_enhanced_aec()

    assert result.status == "warn"
    assert result.reason == doctor.aec.REASON_ENHANCED_AEC_UNAVAILABLE
