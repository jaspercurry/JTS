# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import importlib.util
import io
import json
import stat
import struct
import tarfile
from pathlib import Path
from types import ModuleType, SimpleNamespace

import numpy as np
import pytest
from scipy import signal


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "airplay-receiver-timing-proof.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("airplay_receiver_timing_proof", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PROOF = _load_script()


def test_operator_proof_stays_passive_and_private() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert "systemctl" not in text
    assert "aplay" not in text
    assert "socket.AF_PACKET" in PROOF.REMOTE_CAPTURE
    assert "socket.SOCK_DGRAM" not in PROOF.REMOTE_CAPTURE
    assert "workdir.mkdir(mode=0o700" in PROOF.REMOTE_CAPTURE
    assert "path.chmod(0o600)" in PROOF.REMOTE_CAPTURE
    assert "time.sleep(0.25)" not in PROOF.REMOTE_CAPTURE
    assert "first_monotonic_ns" not in PROOF.REMOTE_CAPTURE
    assert PROOF.REMOTE_CAPTURE.count("outputd_capture_errors(") == 3


@pytest.fixture(scope="module")
def remote() -> dict[str, object]:
    namespace: dict[str, object] = {"__name__": "airplay_remote_capture_test"}
    exec(PROOF.REMOTE_CAPTURE, namespace)
    return namespace


def test_estimate_lag_recovers_file_relative_digital_delay() -> None:
    rng = np.random.default_rng(74)
    marker = rng.normal(0.0, 4000.0, size=4000)
    pre = np.zeros(48_000)
    pre[8000 : 8000 + marker.size] = marker
    delayed = np.zeros_like(pre)
    lag_samples = 3840
    delayed[8000 + lag_samples : 8000 + lag_samples + marker.size] = marker

    result = PROOF.estimate_lag(
        pre,
        delayed,
        np=np,
        signal=signal,
        min_latency_ms=20.0,
        max_latency_ms=150.0,
    )
    zero_lag = PROOF.estimate_lag(
        pre,
        pre.copy(),
        np=np,
        signal=signal,
        min_latency_ms=0.0,
        max_latency_ms=20.0,
    )

    assert result["lag_samples_ref_minus_pre"] == lag_samples
    assert result["measured_latency_ms"] == pytest.approx(80.0)
    assert result["confidence"] == "high"
    assert zero_lag["lag_samples_ref_minus_pre"] == 0
    assert zero_lag["measured_latency_ms"] == pytest.approx(0.0)


def test_estimate_lag_downgrades_ambiguous_periodic_content() -> None:
    samples = np.arange(48_000, dtype=np.float64)
    periodic = np.sin(2.0 * np.pi * 1000.0 * samples / PROOF.SAMPLE_RATE)

    result = PROOF.estimate_lag(
        periodic,
        periodic.copy(),
        np=np,
        signal=signal,
        min_latency_ms=-20.0,
        max_latency_ms=50.0,
    )

    assert result["ambiguity_ratio"] < 1.05
    assert result["confidence"] == "low"
    with pytest.raises(ValueError, match="low-confidence correlation"):
        PROOF.require_accepted_correlation(result)


def test_aggregate_defensively_rejects_low_confidence() -> None:
    reports = [
        {
            "lag": {"confidence": "high"},
            "derived": {
                "measured_receiver_hidden_delay_ms": 80.0,
                "measurement_minus_live_model_ms": 2.0,
                "measurement_minus_configured_offset_abs_ms": None,
            },
        },
        {
            "lag": {"confidence": "low"},
            "derived": {
                "measured_receiver_hidden_delay_ms": 400.0,
                "measurement_minus_live_model_ms": 322.0,
                "measurement_minus_configured_offset_abs_ms": None,
            },
        },
    ]

    with pytest.raises(ValueError, match="refusing to aggregate"):
        PROOF.print_aggregate(reports)


def _udp_packet(
    *,
    dst_port: int,
    payload: bytes,
    protocol: int = 17,
    destination: bytes = b"\x7f\x00\x00\x01",
    fragmented: bool = False,
) -> bytes:
    ethernet = b"\x00" * 12 + struct.pack("!H", 0x0800)
    udp = struct.pack("!HHHH", 12345, dst_port, 8 + len(payload), 0) + payload
    ip = bytearray(20)
    ip[0] = 0x45
    ip[2:4] = struct.pack("!H", len(ip) + len(udp))
    if fragmented:
        ip[6:8] = struct.pack("!H", 0x2000)
    ip[9] = protocol
    ip[12:16] = b"\x7f\x00\x00\x01"
    ip[16:20] = destination
    return ethernet + bytes(ip) + udp


def test_remote_packet_filter_accepts_only_outgoing_reference_udp(remote) -> None:
    parser = remote["udp_payload_if_reference"]
    packet_outgoing = remote["PACKET_OUTGOING"]
    payload = b"reference-audio"
    packet = _udp_packet(dst_port=9891, payload=payload)

    accepted = [
        packet_type
        for packet_type in range(5)
        if parser(packet, 9891, packet_type) == payload
    ]
    assert accepted == [packet_outgoing]
    assert parser(packet, 9892, packet_outgoing) is None
    assert parser(
        _udp_packet(dst_port=9891, payload=payload, protocol=6),
        9891,
        packet_outgoing,
    ) is None
    assert parser(
        _udp_packet(dst_port=9891, payload=payload, destination=b"\x7f\x00\x00\x02"),
        9891,
        packet_outgoing,
    ) is None
    assert parser(
        _udp_packet(dst_port=9891, payload=payload, fragmented=True),
        9891,
        packet_outgoing,
    ) is None
    assert parser(packet[:30], 9891, packet_outgoing) is None


def test_remote_outputd_capture_gate_requires_loopback_and_live_reference(remote) -> None:
    validate = remote["outputd_capture_errors"]
    healthy = {
        "content": {"source": "alsa"},
        "udp_target": "127.0.0.1:9891",
        "udp_active": True,
    }

    assert validate(healthy, 9891, "before capture") == []
    shm_ring = {**healthy, "content": {"source": "shm_ring"}}
    assert any("lossy lane-7 diagnostic mirror" in error for error in validate(
        shm_ring, 9891, "before capture"
    ))
    inactive = {**healthy, "udp_active": False}
    assert any("udp_active" in error for error in validate(
        inactive, 9891, "after capture"
    ))
    wrong_target = {**healthy, "udp_target": "127.0.0.1:9999"}
    assert any("udp_target" in error for error in validate(
        wrong_target, 9891, "after capture"
    ))
    assert validate([], 9891, "before capture") == [
        "outputd before capture STATUS is not a JSON object"
    ]


def test_remote_env_snapshot_is_allowlisted_and_excludes_secrets(tmp_path, remote) -> None:
    env_file = tmp_path / "jasper.env"
    env_file.write_text(
        "JASPER_CAMILLA_CHUNKSIZE=256\n"
        "JASPER_FANIN_OUTPUT_BUFFER_FRAMES=1024\n"
        "OPENAI_API_KEY=should-not-leave-the-pi\n"
        "JASPER_CONTROL_TOKEN=also-private\n",
        encoding="utf-8",
    )

    values = remote["parse_env_file"](env_file, remote["MODEL_ENV_KEYS"])

    assert values == {
        "JASPER_CAMILLA_CHUNKSIZE": "256",
        "JASPER_FANIN_OUTPUT_BUFFER_FRAMES": "1024",
    }
    assert "should-not-leave-the-pi" not in json.dumps(values)


def test_airplay_source_identity_fails_closed() -> None:
    PROOF.validate_airplay_source_identity({
        "active_source_before": "airplay",
        "active_source_after": "airplay",
    })

    with pytest.raises(ValueError, match="stable AirPlay proof"):
        PROOF.validate_airplay_source_identity({
            "active_source_before": "airplay",
            "active_source_after": "spotify",
        })
    with pytest.raises(ValueError, match="stable AirPlay proof"):
        PROOF.validate_airplay_source_identity({})


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"duration": 1.0}, "--duration"),
        ({"duration": 61.0}, "--duration"),
        ({"runs": 0}, "--runs"),
        ({"runs": 11}, "--runs"),
        ({"pause_s": -1.0}, "--pause-s"),
        ({"pause_s": float("nan")}, "--pause-s"),
        ({"ref_port": 0}, "--ref-port"),
        ({"ref_port": 9892}, "--ref-port"),
        ({"pre_device": "hw:Loopback,1,6"}, "--pre-device"),
        ({"min_latency_ms": 10.0, "max_latency_ms": 10.0}, "--min-latency-ms"),
        ({"host": "../../tmp"}, "--host"),
        ({"user": "pi;id"}, "--user"),
    ],
)
def test_cli_bounds_fail_closed(overrides: dict[str, object], message: str) -> None:
    values = {
        "duration": 15.0,
        "runs": 1,
        "pause_s": 1.0,
        "ref_port": 9891,
        "pre_device": "plug:jasper_capture",
        "min_latency_ms": -20.0,
        "max_latency_ms": 250.0,
        "host": "jts2.local",
        "user": "pi",
    }
    values.update(overrides)

    with pytest.raises(SystemExit, match=message):
        PROOF.validate_args(SimpleNamespace(**values))


@pytest.mark.parametrize("short_endpoint", ["pre", "reference"])
def test_capture_coverage_rejects_truncated_endpoint(short_endpoint: str) -> None:
    requested = np.zeros(2 * PROOF.SAMPLE_RATE)
    truncated = np.zeros(PROOF.SAMPLE_RATE // 10)

    accepted = PROOF.require_capture_coverage(requested, requested, duration=2.0)
    assert accepted["pre_ratio"] == pytest.approx(1.0)
    assert accepted["reference_ratio"] == pytest.approx(1.0)
    with pytest.raises(ValueError, match="at least 95%"):
        PROOF.require_capture_coverage(
            truncated if short_endpoint == "pre" else requested,
            truncated if short_endpoint == "reference" else requested,
            duration=2.0,
        )


def test_model_prefers_live_status_geometry_and_labels_camilla_inference() -> None:
    model = PROOF.build_model({
        "outputd_env": {
            "JASPER_CAMILLA_CHUNKSIZE": "256",
            "JASPER_CAMILLA_TARGET_LEVEL": "1536",
            "JASPER_OUTPUTD_DAC_BUFFER_FRAMES": "222",
            "JASPER_OUTPUTD_PERIOD_FRAMES": "333",
        },
        "fanin_env": {"JASPER_FANIN_OUTPUT_BUFFER_FRAMES": "111"},
        "fanin_after": {"output": {"buffer_frames": 1536}},
        "outputd_after": {
            "dac": {
                "buffer_frames": 4096,
                "period_frames": 512,
                "snd_pcm_delay_frames": 1000,
            }
        },
    })

    assert model["fanin_output_buffer_frames"] == 1536
    assert model["outputd_configured_dac_buffer_frames"] == 4096
    assert model["outputd_period_frames"] == 512
    assert model["outputd_live_dac_delay_frames"] == 1000
    assert model["camilla_extra_frames"] == 1280
    assert model["camilla_values_kind"].startswith("inferred_")
    assert model["camilla_chunk_source"] == (
        "inferred from outputd env JASPER_CAMILLA_CHUNKSIZE"
    )
    assert model["camilla_target_level_source"] == (
        "inferred from outputd env JASPER_CAMILLA_TARGET_LEVEL"
    )
    assert model["fanin_output_buffer_source"].startswith("fanin STATUS")
    assert model["outputd_dac_buffer_source"].startswith("outputd STATUS")
    assert model["outputd_period_source"].startswith("outputd STATUS")


def _healthy_counter_snapshots() -> tuple[dict, dict, dict, dict]:
    outputd_before = {
        "content": {"xrun_count": 2, "empty_periods": 4},
        "dac": {"xrun_count": 3},
    }
    outputd_after = {
        "content": {"xrun_count": 2, "empty_periods": 5},
        "dac": {"xrun_count": 3},
    }
    fanin_before = {"output": {"xrun_count": 1}}
    fanin_after = {"output": {"xrun_count": 1}}
    return outputd_before, outputd_after, fanin_before, fanin_after


def test_capture_health_requires_known_monotonic_zero_xrun_deltas() -> None:
    snapshots = _healthy_counter_snapshots()
    evidence = PROOF.require_healthy_counters(*snapshots)

    assert evidence["outputd_content_xruns"]["status"] == "unchanged"
    assert evidence["outputd_content_empty_periods"] == {
        "before": 4,
        "after": 5,
        "delta": 1,
        "status": "increased",
    }



@pytest.mark.parametrize(
    "counter_name",
    ["outputd_content_xruns", "outputd_dac_xruns", "fanin_output_xruns"],
)
def test_capture_health_rejects_each_positive_xrun_delta(counter_name: str) -> None:
    outputd_before, outputd_after, fanin_before, fanin_after = (
        _healthy_counter_snapshots()
    )
    counters = {
        "outputd_content_xruns": outputd_after["content"],
        "outputd_dac_xruns": outputd_after["dac"],
        "fanin_output_xruns": fanin_after["output"],
    }
    counters[counter_name]["xrun_count"] += 1

    with pytest.raises(ValueError, match=rf"{counter_name}=\+1"):
        PROOF.require_healthy_counters(
            outputd_before, outputd_after, fanin_before, fanin_after
        )


@pytest.mark.parametrize(("replacement", "message"), [(None, "unknown"), (0, "reset")])
def test_capture_health_rejects_unknown_and_reset_counters(
    replacement: int | None,
    message: str,
) -> None:
    outputd_before, outputd_after, fanin_before, fanin_after = (
        _healthy_counter_snapshots()
    )
    outputd_after["content"]["xrun_count"] = replacement

    with pytest.raises(ValueError, match=message):
        PROOF.require_healthy_counters(
            outputd_before, outputd_after, fanin_before, fanin_after
        )


def test_remote_path_validation_and_cleanup_reject_untrusted_path(monkeypatch) -> None:
    stamp = "20260712T120000Z"
    token = "abcdefghijklmnopqrstuvwx"
    valid = f"{PROOF.REMOTE_DIR_PREFIX}{stamp}-{token}"
    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(PROOF, "run", fake_run)

    assert PROOF.validate_remote_dir(
        valid,
        expected_stamp=stamp,
        expected_token=token,
    ) == Path(valid)
    assert PROOF.cleanup_remote(
        "pi@jts2.local",
        valid,
        expected_stamp=stamp,
        expected_token=token,
    ) is True
    assert calls and "sudo rm -rf" in calls[0][2]
    with pytest.raises(ValueError, match="unsafe remote"):
        PROOF.cleanup_remote(
            "pi@jts2.local",
            "/",
            expected_stamp=stamp,
            expected_token=token,
        )
    with pytest.raises(ValueError, match="stamp mismatch"):
        PROOF.validate_remote_dir(
            valid,
            expected_stamp="20260712T120001Z",
            expected_token=token,
        )
    with pytest.raises(ValueError, match="token mismatch"):
        PROOF.validate_remote_dir(
            valid,
            expected_stamp=stamp,
            expected_token="zyxwvutsrqponmlkjihgfedc",
        )
    assert len(calls) == 1


def test_remote_cleanup_requires_matching_unguessable_ownership_token() -> None:
    stamp = "20260712T120000Z"
    token = "abcdefghijklmnopqrstuvwx"
    remote_dir = f"{PROOF.REMOTE_DIR_PREFIX}{stamp}-{token}"
    result = {"workdir": remote_dir, "run_token": token, "errors": []}

    assert PROOF.confirm_remote_ownership(
        result,
        expected_remote_dir=remote_dir,
        expected_stamp=stamp,
        expected_token=token,
    ) == remote_dir
    with pytest.raises(RuntimeError, match="did not prove run ownership"):
        PROOF.confirm_remote_ownership(
            {**result, "run_token": "zyxwvutsrqponmlkjihgfedc"},
            expected_remote_dir=remote_dir,
            expected_stamp=stamp,
            expected_token=token,
        )
    with pytest.raises(RuntimeError, match="not a JSON object"):
        PROOF.confirm_remote_ownership(
            None,
            expected_remote_dir=remote_dir,
            expected_stamp=stamp,
            expected_token=token,
        )


def test_run_tokens_are_unique_and_remote_path_safe() -> None:
    stamp = "20260712T120000Z"
    tokens = {PROOF.new_run_token() for _ in range(20)}

    assert len(tokens) == 20
    for token in tokens:
        path = f"{PROOF.REMOTE_DIR_PREFIX}{stamp}-{token}"
        assert PROOF.validate_remote_dir(
            path,
            expected_stamp=stamp,
            expected_token=token,
        ) == Path(path)


def _tar_bytes(root: str, *, member_name: str = "capture.raw", kind: str = "file") -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        root_info = tarfile.TarInfo(root)
        root_info.type = tarfile.DIRTYPE
        archive.addfile(root_info)
        info = tarfile.TarInfo(f"{root}/{member_name}")
        if kind == "symlink":
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            archive.addfile(info)
        else:
            payload = b"private program audio"
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return output.getvalue()


def test_safe_extract_keeps_artifacts_private_and_rejects_links(tmp_path) -> None:
    root = "jasper-airplay-receiver-proof-20260712T120000Z-abcdefghijklmnopqrstuvwx"
    extracted = PROOF._safe_extract_tar(
        _tar_bytes(root),
        tmp_path / "captures",
        expected_root=root,
    )

    assert stat.S_IMODE(extracted.stat().st_mode) == 0o700
    assert stat.S_IMODE((extracted / "capture.raw").stat().st_mode) == 0o600

    with pytest.raises(RuntimeError, match="existing artifact path"):
        PROOF._safe_extract_tar(
            _tar_bytes(root),
            tmp_path / "captures",
            expected_root=root,
        )

    with pytest.raises(RuntimeError, match="non-file tar member"):
        PROOF._safe_extract_tar(
            _tar_bytes(root + "2", kind="symlink"),
            tmp_path / "captures",
            expected_root=root + "2",
        )

    with pytest.raises(RuntimeError, match="unsafe tar member"):
        PROOF._safe_extract_tar(
            _tar_bytes(root + "3", member_name="../escaped.raw"),
            tmp_path / "captures",
            expected_root=root + "3",
        )


def test_remote_size_limit_is_enforced(monkeypatch) -> None:
    monkeypatch.setattr(
        PROOF,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            stdout=f"{PROOF.MAX_REMOTE_DIR_BYTES + 1}\t/tmp/proof\n",
        ),
    )

    with pytest.raises(RuntimeError, match="limit"):
        PROOF._remote_dir_size("pi@jts2.local", Path("/tmp/proof"))


def test_fetch_uses_bounded_privileged_tar_not_world_readable_scp(
    monkeypatch,
    tmp_path,
) -> None:
    stamp = "20260712T120000Z"
    token = "abcdefghijklmnopqrstuvwx"
    root = f"jasper-airplay-receiver-proof-{stamp}-{token}"
    remote_dir = f"/tmp/{root}"
    calls: list[tuple[list[str], dict[str, object]]] = []

    monkeypatch.setattr(PROOF, "_remote_dir_size", lambda *_args: 1024)

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(stdout=_tar_bytes(root), stderr=b"", returncode=0)

    monkeypatch.setattr(PROOF.subprocess, "run", fake_run)

    extracted = PROOF.fetch_remote_dir(
        "pi@jts2.local",
        remote_dir,
        tmp_path,
        expected_stamp=stamp,
        expected_token=token,
    )

    assert extracted == tmp_path / root
    assert calls[0][0][:2] == ["ssh", "pi@jts2.local"]
    assert "sudo tar" in calls[0][0][2]
    assert calls[0][1]["timeout"] == PROOF.TRANSFER_TIMEOUT_SEC
    assert "scp" not in calls[0][0]
