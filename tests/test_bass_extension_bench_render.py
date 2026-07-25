# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""R5/R8/R9 — binary resolution, the argv contract, and determinism receipts.

Every subprocess call is mocked (``subprocess.run``) — no real camilladsp
binary, no real systemctl. Deterministic, no sleeps, no network.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from jasper.bass_extension.bench import render


class _FakeCompleted:
    def __init__(self, *, returncode: int = 0, stdout: object = b"", stderr: object = b"") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _stub_subprocess_run(
    monkeypatch: pytest.MonkeyPatch, responder
) -> None:
    def _run(argv, **kwargs):  # type: ignore[no-untyped-def]
        return responder(argv, kwargs)

    monkeypatch.setattr(subprocess, "run", _run)


# --------------------------------------------------------------------------- #
# R5: binary resolution
# --------------------------------------------------------------------------- #


def test_resolve_render_binary_reads_exec_start_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary_path = tmp_path / "camilladsp"
    binary_path.write_bytes(b"fake-elf-binary")
    binary_path.chmod(0o755)

    def _responder(argv, kwargs):  # type: ignore[no-untyped-def]
        if argv[:2] == ["systemctl", "show"]:
            return _FakeCompleted(
                returncode=0, stdout=f"path={binary_path} ; argv[]={binary_path}\n"
            )
        if str(binary_path) in argv and "--version" in argv:
            return _FakeCompleted(returncode=0, stdout="CamillaDSP 4.1.3\n")
        raise AssertionError(f"unexpected subprocess call: {argv}")

    _stub_subprocess_run(monkeypatch, _responder)
    identity = render.resolve_render_binary(env={})
    assert identity.path == str(binary_path)
    assert "4.1.3" in identity.version_output
    assert len(identity.sha256) == 64
    assert identity.camilladsp_build_id == f"camilladsp-v4.1.3-{identity.sha256[:12]}"


def test_resolve_render_binary_refuses_on_set_but_different_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary_path = tmp_path / "camilladsp"
    binary_path.write_bytes(b"fake-elf-binary")
    binary_path.chmod(0o755)

    def _responder(argv, kwargs):  # type: ignore[no-untyped-def]
        return _FakeCompleted(returncode=0, stdout=f"path={binary_path} ;\n")

    _stub_subprocess_run(monkeypatch, _responder)
    with pytest.raises(render.RenderError, match="JASPER_CAMILLADSP_BIN"):
        render.resolve_render_binary(env={"JASPER_CAMILLADSP_BIN": "/some/other/binary"})


def test_resolve_render_binary_accepts_matching_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary_path = tmp_path / "camilladsp"
    binary_path.write_bytes(b"fake-elf-binary")
    binary_path.chmod(0o755)

    def _responder(argv, kwargs):  # type: ignore[no-untyped-def]
        if argv[:2] == ["systemctl", "show"]:
            return _FakeCompleted(returncode=0, stdout=f"path={binary_path} ;\n")
        return _FakeCompleted(returncode=0, stdout="CamillaDSP 4.1.3\n")

    _stub_subprocess_run(monkeypatch, _responder)
    identity = render.resolve_render_binary(env={"JASPER_CAMILLADSP_BIN": str(binary_path)})
    assert identity.path == str(binary_path)


def test_resolve_render_binary_refuses_wrong_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary_path = tmp_path / "camilladsp"
    binary_path.write_bytes(b"fake-elf-binary")
    binary_path.chmod(0o755)

    def _responder(argv, kwargs):  # type: ignore[no-untyped-def]
        if argv[:2] == ["systemctl", "show"]:
            return _FakeCompleted(returncode=0, stdout=f"path={binary_path} ;\n")
        return _FakeCompleted(returncode=0, stdout="CamillaDSP 4.2.0\n")

    _stub_subprocess_run(monkeypatch, _responder)
    with pytest.raises(render.RenderError, match="4.1.3"):
        render.resolve_render_binary(env={})


def test_resolve_render_binary_refuses_when_systemctl_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _responder(argv, kwargs):  # type: ignore[no-untyped-def]
        return _FakeCompleted(returncode=1, stdout="", stderr="unit not found")

    _stub_subprocess_run(monkeypatch, _responder)
    with pytest.raises(render.RenderError):
        render.resolve_render_binary(env={})


def test_resolve_render_binary_refuses_when_resolved_path_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "nope"

    def _responder(argv, kwargs):  # type: ignore[no-untyped-def]
        return _FakeCompleted(returncode=0, stdout=f"path={missing} ;\n")

    _stub_subprocess_run(monkeypatch, _responder)
    with pytest.raises(render.RenderError):
        render.resolve_render_binary(env={})


# --------------------------------------------------------------------------- #
# R9: the argv contract
# --------------------------------------------------------------------------- #


def test_render_config_argv_carries_the_bracketed_fader_gain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B1: the render argv MUST carry ``--gain <fader_db>`` — R4(c) always
    resolves to "reproduce the recorded fader gain" for the pinned build, and
    a render that omits it is systematically off by the fader's dB (silently
    permissive on the Discovery path, since finish_discovery never
    cross-checks against a live capture)."""

    output_path = tmp_path / "out.raw"
    seen_argv: list[Any] = []

    def _responder(argv, kwargs):  # type: ignore[no-untyped-def]
        seen_argv.append(argv)
        output_path.write_bytes(b"\x00" * 16)
        return _FakeCompleted(returncode=0)

    _stub_subprocess_run(monkeypatch, _responder)
    bounds = render.RenderBounds(
        timeout_s=5.0, rlimit_as_bytes=1 << 28, rlimit_cpu_s=5, nice=10
    )
    config_path = tmp_path / "cfg.yml"
    config_path.write_text("devices: {}\n")
    result = render.render_config(
        "/opt/camilladsp/camilladsp",
        config_path,
        output_path=output_path,
        bounds=bounds,
        fader_db=-6.5,
    )
    expected = ("/opt/camilladsp/camilladsp", "--gain", "-6.5", str(config_path))
    assert result.argv == expected
    assert seen_argv == [expected]


@pytest.mark.parametrize("forbidden", ["--statefile", "-p", "--port", "-a", "--address"])
def test_render_config_refuses_forbidden_argv_tokens_before_starting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forbidden: str
) -> None:
    called = False

    def _responder(argv, kwargs):  # type: ignore[no-untyped-def]
        nonlocal called
        called = True
        return _FakeCompleted(returncode=0)

    _stub_subprocess_run(monkeypatch, _responder)
    bounds = render.RenderBounds(timeout_s=5.0, rlimit_as_bytes=1 << 28, rlimit_cpu_s=5, nice=10)
    # Simulate a hypothetical caller bug by invoking render_config with a
    # binary "path" that IS the forbidden token, proving the guard fires
    # before any subprocess starts.
    with pytest.raises(render.RenderError, match="statefile or websocket"):
        render.render_config(
            forbidden,
            tmp_path / "cfg.yml",
            output_path=tmp_path / "out.raw",
            bounds=bounds,
            fader_db=0.0,
        )
    assert not called


def test_render_config_refuses_on_nonzero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _responder(argv, kwargs):  # type: ignore[no-untyped-def]
        return _FakeCompleted(returncode=1, stdout=b"", stderr=b"bad config")

    _stub_subprocess_run(monkeypatch, _responder)
    bounds = render.RenderBounds(timeout_s=5.0, rlimit_as_bytes=1 << 28, rlimit_cpu_s=5, nice=10)
    with pytest.raises(render.RenderError, match="exited 1"):
        render.render_config(
            "/opt/camilladsp/camilladsp",
            tmp_path / "cfg.yml",
            output_path=tmp_path / "out.raw",
            bounds=bounds,
            fader_db=0.0,
        )


def test_render_config_refuses_on_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _responder(argv, kwargs):  # type: ignore[no-untyped-def]
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 0))

    _stub_subprocess_run(monkeypatch, _responder)
    bounds = render.RenderBounds(timeout_s=5.0, rlimit_as_bytes=1 << 28, rlimit_cpu_s=5, nice=10)
    with pytest.raises(render.RenderError, match="timed out"):
        render.render_config(
            "/opt/camilladsp/camilladsp",
            tmp_path / "cfg.yml",
            output_path=tmp_path / "out.raw",
            bounds=bounds,
            fader_db=0.0,
        )


def test_render_config_refuses_when_no_output_produced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _responder(argv, kwargs):  # type: ignore[no-untyped-def]
        return _FakeCompleted(returncode=0)  # exits 0 but writes nothing

    _stub_subprocess_run(monkeypatch, _responder)
    bounds = render.RenderBounds(timeout_s=5.0, rlimit_as_bytes=1 << 28, rlimit_cpu_s=5, nice=10)
    with pytest.raises(render.RenderError, match="no output file"):
        render.render_config(
            "/opt/camilladsp/camilladsp",
            tmp_path / "cfg.yml",
            output_path=tmp_path / "out.raw",
            bounds=bounds,
            fader_db=0.0,
        )


# --------------------------------------------------------------------------- #
# R8: determinism receipts
#
# render_with_determinism_receipt calls render_config (already pinned above)
# twice; that logic is exercised directly here by patching render_config
# itself (not subprocess.run) to return canned RenderInvocation values,
# isolating the comparison logic R8 actually cares about.
# --------------------------------------------------------------------------- #


def _fake_render_invocation(*, output_sha256: str) -> render.RenderInvocation:
    return render.RenderInvocation(
        argv=("/opt/camilladsp/camilladsp", "/tmp/cfg.yml"),
        returncode=0,
        duration_s=0.01,
        stdout_tail="",
        stderr_tail="",
        output_sha256=output_sha256,
        output_byte_size=4,
    )


def test_determinism_receipt_passes_on_identical_renders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        render,
        "render_config",
        lambda *a, **k: _fake_render_invocation(output_sha256="same-sha"),
    )
    bounds = render.RenderBounds(timeout_s=5.0, rlimit_as_bytes=1 << 28, rlimit_cpu_s=5, nice=10)
    receipt = render.render_with_determinism_receipt(
        "/opt/camilladsp/camilladsp",
        tmp_path / "cfg.yml",
        yaml_text="devices: {}\n",
        first_output_path=tmp_path / "out.first",
        second_output_path=tmp_path / "out.second",
        bounds=bounds,
        fader_db=0.0,
    )
    assert receipt.deterministic
    assert receipt.config_sha256 == render.config_shape_sha256("devices: {}\n")


def test_determinism_receipt_refuses_on_byte_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}

    def _render_config(*a: object, **k: object) -> render.RenderInvocation:
        calls["n"] += 1
        return _fake_render_invocation(
            output_sha256="sha-one" if calls["n"] == 1 else "sha-two"
        )

    monkeypatch.setattr(render, "render_config", _render_config)
    bounds = render.RenderBounds(timeout_s=5.0, rlimit_as_bytes=1 << 28, rlimit_cpu_s=5, nice=10)
    with pytest.raises(render.RenderError, match="non-deterministic"):
        render.render_with_determinism_receipt(
            "/opt/camilladsp/camilladsp",
            tmp_path / "cfg.yml",
            yaml_text="devices: {}\n",
            first_output_path=tmp_path / "out.first",
            second_output_path=tmp_path / "out.second",
            bounds=bounds,
            fader_db=0.0,
        )


# --------------------------------------------------------------------------- #
# R9: free-space guard
# --------------------------------------------------------------------------- #


def test_check_free_space_refuses_when_below_the_campaign_estimate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil as shutil_module

    monkeypatch.setattr(
        render.shutil,
        "disk_usage",
        lambda path: shutil_module._ntuple_diskusage(total=1000, used=900, free=100),  # type: ignore[attr-defined]
    )
    with pytest.raises(render.RenderError, match="free space"):
        render.check_free_space(
            tmp_path, per_render_estimate_bytes=1000, renders_outstanding=1
        )


def test_check_free_space_passes_when_sufficient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil as shutil_module

    monkeypatch.setattr(
        render.shutil,
        "disk_usage",
        lambda path: shutil_module._ntuple_diskusage(
            total=10**9, used=0, free=10**9
        ),  # type: ignore[attr-defined]
    )
    render.check_free_space(tmp_path, per_render_estimate_bytes=1000, renders_outstanding=5)


def test_check_free_space_is_a_noop_with_nothing_outstanding(tmp_path: Path) -> None:
    render.check_free_space(tmp_path, per_render_estimate_bytes=10**12, renders_outstanding=0)


def test_estimate_render_bytes_scales_with_geometry() -> None:
    small = render.estimate_render_bytes(48000, 2, 1.0)
    large = render.estimate_render_bytes(48000, 4, 2.0)
    assert large > small > 0


# --------------------------------------------------------------------------- #
# extract_channel
# --------------------------------------------------------------------------- #


def test_extract_channel_pulls_the_right_interleaved_samples(tmp_path: Path) -> None:
    # 2 channels, 3 frames, 2 bytes/sample: ch0=[1,3,5], ch1=[2,4,6]
    data = bytes([1, 0, 2, 0, 3, 0, 4, 0, 5, 0, 6, 0])
    path = tmp_path / "raw.bin"
    path.write_bytes(data)
    ch0 = render.extract_channel(path, channel_index=0, channel_count=2, bytes_per_sample=2)
    ch1 = render.extract_channel(path, channel_index=1, channel_count=2, bytes_per_sample=2)
    assert ch0 == bytes([1, 0, 3, 0, 5, 0])
    assert ch1 == bytes([2, 0, 4, 0, 6, 0])


def test_extract_channel_refuses_out_of_range_index(tmp_path: Path) -> None:
    path = tmp_path / "raw.bin"
    path.write_bytes(bytes(8))
    with pytest.raises(render.RenderError):
        render.extract_channel(path, channel_index=2, channel_count=2, bytes_per_sample=4)


def test_extract_channel_refuses_partial_frame(tmp_path: Path) -> None:
    path = tmp_path / "raw.bin"
    path.write_bytes(bytes(7))  # not a multiple of 2 channels * 4 bytes
    with pytest.raises(render.RenderError):
        render.extract_channel(path, channel_index=0, channel_count=2, bytes_per_sample=4)


# --------------------------------------------------------------------------- #
# reference_soft_clip — the digital_transfer_probe's pinned reference
# --------------------------------------------------------------------------- #


def test_reference_soft_clip_is_identity_far_below_the_clip_limit() -> None:
    samples = np.array([0.001, -0.001, 0.0005], dtype=np.float64)
    out = render.reference_soft_clip(samples, clip_limit_dbfs=-1.0)
    np.testing.assert_allclose(out, samples, atol=1e-6)


def test_reference_soft_clip_compresses_above_the_clip_limit() -> None:
    clip_limit_linear = 10.0 ** (-1.0 / 20.0)
    samples = np.array([clip_limit_linear * 1.4], dtype=np.float64)
    out = render.reference_soft_clip(samples, clip_limit_dbfs=-1.0)
    assert 0.0 < out[0] < samples[0]


def test_reference_soft_clip_never_exceeds_1_5x_clip_limit() -> None:
    clip_limit_linear = 10.0 ** (-1.0 / 20.0)
    samples = np.array([clip_limit_linear * 100.0], dtype=np.float64)
    out = render.reference_soft_clip(samples, clip_limit_dbfs=-1.0)
    # scaled clamps to 1.5, then: 1.5 - CUBEFACTOR*1.5**3 = 1.5 - (1/6.75)*3.375 = 1.0
    expected_scaled = 1.5 - (1.0 / 6.75) * (1.5**3)
    np.testing.assert_allclose(out[0], expected_scaled * clip_limit_linear, rtol=1e-9)


def test_reference_soft_clip_is_odd_symmetric() -> None:
    samples = np.array([0.3, -0.3], dtype=np.float64)
    out = render.reference_soft_clip(samples, clip_limit_dbfs=-3.0)
    np.testing.assert_allclose(out[0], -out[1], atol=1e-12)


def test_config_shape_sha256_is_stable_and_content_addressed() -> None:
    a = render.config_shape_sha256("devices: {}\n")
    b = render.config_shape_sha256("devices: {}\n")
    c = render.config_shape_sha256("devices: {samplerate: 48000}\n")
    assert a == b
    assert a != c
    assert len(a) == 64


def test_binary_identity_tap_implementation_id_excludes_binary_path() -> None:
    a = render.BinaryIdentity(
        path="/path/one", version_output="v", sha256="a" * 64, camilladsp_build_id="b"
    )
    b = render.BinaryIdentity(
        path="/path/two", version_output="v", sha256="a" * 64, camilladsp_build_id="b"
    )
    assert a.tap_implementation_id == b.tap_implementation_id
    artifact = a.identity_artifact()
    assert artifact["binary_path"] == "/path/one"
    assert "camilladsp_version" in artifact
    assert "binary_sha256" in artifact
