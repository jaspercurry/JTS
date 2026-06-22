# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import importlib.util
import stat
import sys
import types
from pathlib import Path

import numpy as np
import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "aec-probe-timing.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("aec_probe_timing", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


aec = _load_module()


def test_reference_sources_are_explicit_and_distinct() -> None:
    assert set(aec.REFERENCE_SOURCES) == {
        "outputd_udp",
        "chip_ref_tee",
        "jasper_capture",
    }
    assert "final speaker-reference" in aec.REFERENCE_SOURCES["outputd_udp"].label
    assert "chip-ref writer" in aec.REFERENCE_SOURCES["chip_ref_tee"].label
    assert "pre-DSP" in aec.REFERENCE_SOURCES["jasper_capture"].label

    udp_warnings = aec.source_warnings("outputd_udp", 2)
    assert any("not the actual XVF USB-IN" in warning for warning in udp_warnings)
    old_warnings = aec.source_warnings("jasper_capture", 2)
    assert any("must not be confused with production outputd" in warning for warning in old_warnings)
    tee_warnings = aec.source_warnings("chip_ref_tee", 0)
    assert any("does not prove when the XVF3800 internally consumes" in warning for warning in tee_warnings)
    assert any("processed chip beam" in warning for warning in tee_warnings)


def test_mic_channel_labels_match_chip_aec_contract() -> None:
    assert aec.mic_channel_label(0) == "ch0 = conference/beam in chip-AEC mode"
    assert aec.mic_channel_label(1) == "ch1 = ASR beam in chip-AEC mode"
    assert aec.mic_channel_label(2) == "ch2 = raw mic0, preferred for acoustic timing"
    assert aec.mic_channel_label(5) == "ch5 = unlabeled XVF capture channel"


def test_parse_profiles_supports_default_all_and_custom() -> None:
    assert [(p.name, p.period_frames, p.dac_buffer_frames) for p in aec.parse_profiles("default")] == [
        ("default", 1024, 3072)
    ]
    assert [(p.name, p.period_frames, p.dac_buffer_frames) for p in aec.parse_profiles("all")] == [
        ("default", 1024, 3072),
        ("1024/2048", 1024, 2048),
        ("512/1024", 512, 1024),
    ]
    assert [(p.name, p.period_frames, p.dac_buffer_frames) for p in aec.parse_profiles("256/768")] == [
        ("256/768", 256, 768)
    ]
    assert [(p.name, p.period_frames, p.dac_buffer_frames) for p in aec.parse_profiles("2048/4096")] == [
        ("2048/4096", 2048, 4096)
    ]


@pytest.mark.parametrize(
    "value",
    ["", "fast", "1024/512", "1024/1024", "1024/1536", "4096/8192", "0/1024"],
)
def test_parse_profiles_rejects_invalid_values(value: str) -> None:
    with pytest.raises(Exception):
        aec.parse_profiles(value)


def test_custom_profile_error_names_outputd_buffer_contract() -> None:
    with pytest.raises(Exception, match="2 x period"):
        aec.parse_profiles("1024/1536")

    with pytest.raises(Exception, match="pins content buffer"):
        aec.parse_profiles("4096/8192")


def test_outputd_dropin_uses_root_only_env_file_and_cleans_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dropin_dir = tmp_path / "systemd" / "jasper-outputd.service.d"
    dropin_path = dropin_dir / "aec-timing-probe.conf"
    env_path = tmp_path / "aec-timing-probe.env"
    monkeypatch.setattr(aec, "DROPIN_DIR", str(dropin_dir))
    monkeypatch.setattr(aec, "DROPIN_PATH", str(dropin_path))
    monkeypatch.setattr(aec, "DROPIN_ENV_PATH", str(env_path))

    aec.write_outputd_dropin(aec.OutputProfile("1024/2048", 1024, 2048), tee_path=None)

    assert dropin_path.read_text(encoding="utf-8") == (
        "[Service]\n"
        f"EnvironmentFile={env_path}\n"
    )
    assert env_path.read_text(encoding="utf-8") == (
        "JASPER_OUTPUTD_PERIOD_FRAMES=1024\n"
        "JASPER_OUTPUTD_CONTENT_BUFFER_FRAMES=4096\n"
        "JASPER_OUTPUTD_DAC_BUFFER_FRAMES=2048\n"
        "JASPER_OUTPUTD_CHIP_REF_TEE_PATH=\n"
    )
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600

    aec.remove_outputd_dropin()

    assert not dropin_path.exists()
    assert not env_path.exists()
    assert not dropin_dir.exists()


def test_outputd_dropin_records_chip_ref_tee_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dropin_dir = tmp_path / "systemd" / "jasper-outputd.service.d"
    dropin_path = dropin_dir / "aec-timing-probe.conf"
    env_path = tmp_path / "aec-timing-probe.env"
    monkeypatch.setattr(aec, "DROPIN_DIR", str(dropin_dir))
    monkeypatch.setattr(aec, "DROPIN_PATH", str(dropin_path))
    monkeypatch.setattr(aec, "DROPIN_ENV_PATH", str(env_path))

    aec.write_outputd_dropin(
        aec.OutputProfile("512/1024", 512, 1024),
        tee_path="/run/jasper-outputd/aec-timing-probe-chip-ref.s16le",
    )

    assert "JASPER_OUTPUTD_CHIP_REF_TEE_PATH=/run/jasper-outputd/aec-timing-probe-chip-ref.s16le" in env_path.read_text(
        encoding="utf-8"
    )


def test_remote_artifacts_are_not_reopened_world_readable() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    assert "os.umask(0o077)" in script
    assert "out_dir.chmod(0o700)" in script
    assert '["chmod", "-R"' not in script
    assert "a+rX" not in script


def test_signal_handlers_raise_interrupt_then_restore(monkeypatch: pytest.MonkeyPatch) -> None:
    installed: dict[int, object] = {2: "old-int", 15: "old-term", 1: "old-hup"}

    def getsignal(signum: int) -> object:
        return installed[signum]

    def setsignal(signum: int, handler: object) -> None:
        installed[signum] = handler

    fake_signal = types.SimpleNamespace(
        SIGINT=2,
        SIGTERM=15,
        SIGHUP=1,
        SIG_IGN="ignore",
        getsignal=getsignal,
        signal=setsignal,
    )
    monkeypatch.setattr(aec, "signal", fake_signal)

    previous = aec.install_termination_handlers()

    assert previous == {2: "old-int", 15: "old-term", 1: "old-hup"}
    with pytest.raises(aec.ProbeInterrupted) as excinfo:
        installed[15](15, None)  # type: ignore[operator]
    assert excinfo.value.signum == 15

    aec.ignore_termination_handlers(previous)
    assert installed == {2: "ignore", 15: "ignore", 1: "ignore"}

    aec.restore_signal_handlers(previous)
    assert installed == {2: "old-int", 15: "old-term", 1: "old-hup"}


def test_wait_for_chip_ref_tee_ready_reports_missing_outputd_support(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(aec, "CHIP_REF_TEE_PATH", str(tmp_path / "missing.s16le"))
    monkeypatch.setattr(aec, "outputd_status", lambda: {"schema": "old"})

    with pytest.raises(RuntimeError, match="JASPER_OUTPUTD_CHIP_REF_TEE_PATH"):
        aec.wait_for_chip_ref_tee_ready(timeout_s=0.0)


def test_run_on_pi_entrypoint_reports_expected_errors_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(_args: object) -> int:
        raise RuntimeError("chip_ref_tee was requested, but tee was not created")

    monkeypatch.setattr(aec, "run_on_pi", fail)

    rc = aec.main(["--run-on-pi"])

    captured = capsys.readouterr()
    assert rc == 1
    assert "ERROR: RuntimeError: chip_ref_tee was requested" in captured.err
    assert "Traceback" not in captured.err


def test_decode_outputd_udp_downmixes_stereo_and_decimates_to_16k() -> None:
    stereo48 = np.array(
        [
            30, 90,    # mono 60
            60, 120,   # mono 90
            90, 150,   # mono 120 -> avg 90
            300, 600,  # mono 450
            330, 630,  # mono 480
            360, 660,  # mono 510 -> avg 480
        ],
        dtype=np.int16,
    )

    decoded = aec.decode_ref_bytes(stereo48.tobytes(), source="outputd_udp")

    assert decoded.dtype == np.int16
    assert decoded.tolist() == [90, 480]


def test_decode_chip_ref_tee_downmixes_16k_dual_mono_without_decimation() -> None:
    stereo16 = np.array([100, 100, 200, 202, -50, -54], dtype=np.int16)

    decoded = aec.decode_ref_bytes(stereo16.tobytes(), source="chip_ref_tee")

    assert decoded.tolist() == [100, 201, -52]


def test_estimate_lag_reports_mic_delay_as_positive_lag() -> None:
    rng = np.random.default_rng(1234)
    marker = rng.normal(0, 4000, size=900)
    ref = np.zeros(8000)
    ref[1800 : 1800 + marker.size] = marker
    lag = 137
    mic = np.zeros_like(ref)
    mic[1800 + lag : 1800 + lag + marker.size] = marker

    result = aec.estimate_lag(ref, mic, sample_rate_hz=16_000, search_ms=30)

    assert result["lag_samples"] == lag
    assert result["lag_ms"] == pytest.approx(lag / 16_000 * 1000)
    assert result["confidence"] == "high"
    assert result["normalized_peak"] > 0.8


def test_audio_metrics_reports_levels_duration_and_clipping() -> None:
    metrics = aec.audio_metrics(np.array([0, 32767, -32768, 1000], dtype=np.int16), 16_000)

    assert metrics["sample_rate_hz"] == 16_000
    assert metrics["samples"] == 4
    assert metrics["duration_s"] == pytest.approx(4 / 16_000)
    assert metrics["clipping_samples"] == 2
    assert metrics["clipping_percent"] == pytest.approx(50.0)
    assert metrics["rms"] > 20_000
