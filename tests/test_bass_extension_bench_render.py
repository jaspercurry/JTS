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
import yaml

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
    """B1/B-A: the render argv MUST carry ``--gain=<fader_db>`` as ONE
    ``=``-joined token — R4(c) always resolves to "reproduce the recorded
    fader gain" for the pinned build, and a render that omits it is
    systematically off by the fader's dB (silently permissive on the
    Discovery path, since finish_discovery never cross-checks against a
    live capture).

    Uses a NEGATIVE ``fader_db`` deliberately: JTS fader values are always
    ``<= 0`` dB, and the pinned v4.1.3 README's "Initial volume" section
    documents that clap (no ``allow_hyphen_values`` on this ``Arg``) parses
    a SPACE-separated ``--gain -6.5`` (two argv tokens) as broken —
    "have a space before the minus sign and do **NOT** work" — while
    ``--gain=-6.5`` (one token) works. A test asserting only the two-token
    shape would pass regardless of which form the code actually emits
    (this mock never invokes real clap), so the assertion below pins the
    single-token ``=`` form specifically."""

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
    expected = ("/opt/camilladsp/camilladsp", "--gain=-6.5", str(config_path))
    assert result.argv == expected
    assert seen_argv == [expected]
    # Never two separate tokens ("--gain", "-6.5") — the documented-broken form.
    assert "--gain" not in result.argv


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


@pytest.mark.parametrize("forbidden", ["--statefile", "-p", "--port", "-a", "--address"])
def test_render_config_refuses_forbidden_token_embedded_via_equals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forbidden: str
) -> None:
    """N-1: the forbidden-token check must split each argv token on "=" — an
    exact-string membership check alone would miss a hypothetical
    "--port=1234"-shaped single token, now that --gain=<value> establishes
    "=" as a valid argv-token shape in this exact construction path."""

    called = False

    def _responder(argv, kwargs):  # type: ignore[no-untyped-def]
        nonlocal called
        called = True
        return _FakeCompleted(returncode=0)

    _stub_subprocess_run(monkeypatch, _responder)
    bounds = render.RenderBounds(timeout_s=5.0, rlimit_as_bytes=1 << 28, rlimit_cpu_s=5, nice=10)
    with pytest.raises(render.RenderError, match="statefile or websocket"):
        render.render_config(
            f"{forbidden}=evil",
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
# once per render pass. The comparison logic R8 actually cares about is
# exercised by patching render_config itself (not subprocess.run) to return
# canned RenderInvocation values; the destination contract underneath it is
# exercised against a faithful fake binary, further down.
# --------------------------------------------------------------------------- #

_BOUNDS = render.RenderBounds(
    timeout_s=5.0, rlimit_as_bytes=1 << 28, rlimit_cpu_s=5, nice=10
)


def _config_text(*, playback_filename: Path, gain_db: float = 0.0) -> str:
    """A minimal derived-shaped config. The destination lives INSIDE it, at
    ``devices.playback.filename`` — exactly as a real derived render config
    carries it, and as `PlaybackDevice::File` requires. ``gain_db`` is the
    knob these tests turn to make two configs differ somewhere OTHER than
    their destination."""

    return yaml.safe_dump(
        {
            "devices": {
                "samplerate": 48000,
                "enable_rate_adjust": False,
                "capture": {"type": "WavFile", "filename": "/tmp/stimulus.wav"},
                "playback": {
                    "type": "File",
                    "channels": 2,
                    "filename": str(playback_filename),
                    "format": render.DEPLOYED_PROCESSING_PRECISION,
                },
            },
            "filters": {"owner_gain": {"type": "Gain", "parameters": {"gain": gain_db}}},
            "pipeline": [{"type": "Filter", "channels": [0], "names": ["owner_gain"]}],
        },
        sort_keys=False,
    )


def _render_pass(tmp_path: Path, name: str, *, gain_db: float = 0.0) -> render.RenderPass:
    """A complete pass: a config on disk whose text names its own destination."""

    output_path = tmp_path / f"{name}.raw"
    yaml_text = _config_text(playback_filename=output_path, gain_db=gain_db)
    config_path = tmp_path / f"{name}.yml"
    config_path.write_text(yaml_text, encoding="utf-8")
    return render.RenderPass(
        config_path=config_path, yaml_text=yaml_text, output_path=output_path
    )


def _stub_faithful_render_binary(
    monkeypatch: pytest.MonkeyPatch, *, payloads: tuple[bytes, ...] = (b"rendered", b"rendered")
) -> list[Path]:
    """Stub ``subprocess.run`` with a FAITHFUL fake camilladsp binary.

    The real binary is never told where to write on the command line —
    ``render_config``'s argv is exactly ``[binary, --gain=<db>, <config>]``.
    It writes to ``devices.playback.filename`` inside the config it is
    handed. This fake reproduces that: it parses the config path out of
    argv, reads the destination that config declares, and writes THERE.

    A stub that instead writes to whatever path the test picked cannot
    catch a caller that hands one config two destinations — which is
    exactly how that defect survived into main. Returns the destinations
    written, in call order.
    """

    written: list[Path] = []

    def _responder(argv, kwargs):  # type: ignore[no-untyped-def]
        config_path = Path(argv[-1])
        parsed = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        destination = Path(parsed["devices"]["playback"]["filename"])
        destination.write_bytes(payloads[len(written) % len(payloads)])
        written.append(destination)
        return _FakeCompleted(returncode=0)

    _stub_subprocess_run(monkeypatch, _responder)
    return written


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
    canonical = _render_pass(tmp_path, "canonical")
    repeat = _render_pass(tmp_path, "repeat")
    receipt = render.render_with_determinism_receipt(
        "/opt/camilladsp/camilladsp",
        canonical=canonical,
        repeat=repeat,
        bounds=_BOUNDS,
        fader_db=0.0,
    )
    assert receipt.deterministic
    # The canonical shape identity is the one callers key their receipt cache
    # by, and the one whose output is measured downstream — it must stay the
    # SHA of the config that produced the kept artifact.
    assert receipt.config_sha256 == render.config_shape_sha256(canonical.yaml_text)
    # The repeat is a genuinely distinct shape under R8's byte-content
    # definition, and the receipt says so rather than implying one text ran
    # twice.
    assert receipt.repeat_config_sha256 == render.config_shape_sha256(repeat.yaml_text)
    assert receipt.repeat_config_sha256 != receipt.config_sha256


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
    with pytest.raises(render.RenderError, match="non-deterministic"):
        render.render_with_determinism_receipt(
            "/opt/camilladsp/camilladsp",
            canonical=_render_pass(tmp_path, "canonical"),
            repeat=_render_pass(tmp_path, "repeat"),
            bounds=_BOUNDS,
            fader_db=0.0,
        )


def test_determinism_receipt_renders_to_the_destinations_the_configs_declare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression test for the one-config-two-destinations defect.

    Against the faithful fake binary — which writes where the config says,
    not where the caller wished — both destinations must exist and be
    byte-identical afterwards. The previous API took ONE config and two
    output paths; a real binary wrote both renders to the single destination
    that one config named, and the first render's own output assertion blew
    up with "render exited 0 but produced no output file".
    """

    written = _stub_faithful_render_binary(monkeypatch)
    canonical = _render_pass(tmp_path, "canonical")
    repeat = _render_pass(tmp_path, "repeat")

    receipt = render.render_with_determinism_receipt(
        "/opt/camilladsp/camilladsp",
        canonical=canonical,
        repeat=repeat,
        bounds=_BOUNDS,
        fader_db=-17.5,
    )

    assert receipt.deterministic
    # Two renders, two distinct destinations, each written by the config that
    # named it — not one destination written twice.
    assert written == [canonical.output_path, repeat.output_path]
    assert canonical.output_path.read_bytes() == repeat.output_path.read_bytes()
    assert receipt.first.output_sha256 == receipt.second.output_sha256
    # The bracketed fader gain still rides both invocations as one argv token.
    assert receipt.first.argv[1] == "--gain=-17.5"
    assert receipt.second.argv[1] == "--gain=-17.5"
    assert receipt.first.argv[2] == str(canonical.config_path)
    assert receipt.second.argv[2] == str(repeat.config_path)


def test_determinism_receipt_refuses_a_pass_that_does_not_name_its_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pass whose output_path is not what its config writes to is the
    original defect in its most direct form: render_config would watch a
    file the binary never creates."""

    written = _stub_faithful_render_binary(monkeypatch)
    canonical = _render_pass(tmp_path, "canonical")
    elsewhere = render.RenderPass(
        config_path=canonical.config_path,
        yaml_text=canonical.yaml_text,
        output_path=tmp_path / "somewhere-else.raw",
    )
    with pytest.raises(render.RenderError, match="devices.playback.filename"):
        render.render_with_determinism_receipt(
            "/opt/camilladsp/camilladsp",
            canonical=canonical,
            repeat=elsewhere,
            bounds=_BOUNDS,
            fader_db=0.0,
        )
    # Refused before the first subprocess started.
    assert written == []


def test_determinism_receipt_refuses_configs_differing_beyond_the_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two genuinely different graphs would make the receipt prove that two
    DIFFERENT configs happened to agree, not that one config repeats."""

    written = _stub_faithful_render_binary(monkeypatch)
    with pytest.raises(render.RenderError, match="not the same config shape"):
        render.render_with_determinism_receipt(
            "/opt/camilladsp/camilladsp",
            canonical=_render_pass(tmp_path, "canonical", gain_db=0.0),
            repeat=_render_pass(tmp_path, "repeat", gain_db=-6.0),
            bounds=_BOUNDS,
            fader_db=0.0,
        )
    assert written == []


def test_determinism_receipt_refuses_two_passes_sharing_one_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rendering both passes into one file leaves nothing to compare: the
    second render unlinks the first render's output before it starts."""

    written = _stub_faithful_render_binary(monkeypatch)
    canonical = _render_pass(tmp_path, "canonical")
    with pytest.raises(render.RenderError, match="same destination"):
        render.render_with_determinism_receipt(
            "/opt/camilladsp/camilladsp",
            canonical=canonical,
            repeat=canonical,
            bounds=_BOUNDS,
            fader_db=0.0,
        )
    assert written == []


def test_determinism_receipt_refuses_a_config_with_no_playback_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    written = _stub_faithful_render_binary(monkeypatch)
    headless = render.RenderPass(
        config_path=tmp_path / "headless.yml",
        yaml_text="devices: {}\n",
        output_path=tmp_path / "headless.raw",
    )
    with pytest.raises(render.RenderError, match="declares no"):
        render.render_with_determinism_receipt(
            "/opt/camilladsp/camilladsp",
            canonical=headless,
            repeat=_render_pass(tmp_path, "repeat"),
            bounds=_BOUNDS,
            fader_db=0.0,
        )
    assert written == []


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


def test_check_free_space_floors_renders_outstanding_at_one_never_a_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S-4: ``renders_outstanding <= 0`` must NOT disable the guard — it
    floors at 1 render's worth, so the guard stays active for the rest of a
    campaign whose seeded estimate ran out early (e.g. a target with more
    than the one-candidate-per-target assumption
    ``estimate_campaign_render_count`` bakes in). A pre-S-4 early-out would
    have made this a silent no-op regardless of free space."""

    import shutil as shutil_module

    monkeypatch.setattr(
        render.shutil,
        "disk_usage",
        lambda path: shutil_module._ntuple_diskusage(total=1000, used=900, free=100),  # type: ignore[attr-defined]
    )
    with pytest.raises(render.RenderError, match="free space"):
        render.check_free_space(
            tmp_path, per_render_estimate_bytes=1000, renders_outstanding=0
        )


def test_check_free_space_passes_at_zero_outstanding_when_one_renders_worth_fits(
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
    render.check_free_space(tmp_path, per_render_estimate_bytes=1000, renders_outstanding=0)


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
