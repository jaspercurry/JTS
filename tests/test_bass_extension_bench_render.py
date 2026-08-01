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
# twice on ONE config, moving each output aside so both survive. The
# comparison logic R8 actually cares about is exercised by patching
# render_config itself (not subprocess.run) to return canned RenderInvocation
# values; the destination-and-move contract underneath it is exercised
# against a faithful fake binary, further down.
# --------------------------------------------------------------------------- #

_BOUNDS = render.RenderBounds(
    timeout_s=5.0, rlimit_as_bytes=1 << 28, rlimit_cpu_s=5, nice=10
)


def _config_text(*, playback_filename: Path) -> str:
    """A minimal derived-shaped config. The destination lives INSIDE it, at
    ``devices.playback.filename`` — exactly as a real derived render config
    carries it."""

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
            "filters": {"owner_gain": {"type": "Gain", "parameters": {"gain": 0.0}}},
            "pipeline": [{"type": "Filter", "channels": [0], "names": ["owner_gain"]}],
        },
        sort_keys=False,
    )


def _write_render_config(tmp_path: Path) -> tuple[Path, Path]:
    """Write one config whose text names its own destination.

    Returns ``(config_path, declared_output_path)``.
    """

    declared_output_path = tmp_path / "output.raw"
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        _config_text(playback_filename=declared_output_path), encoding="utf-8"
    )
    return config_path, declared_output_path


def _stub_faithful_render_binary(
    monkeypatch: pytest.MonkeyPatch,
    *,
    payloads: tuple[bytes, ...] = (b"rendered", b"rendered"),
) -> list[Path]:
    """Stub ``subprocess.run`` with a FAITHFUL fake camilladsp binary.

    The real binary is never told where to write on the command line —
    ``render_config``'s argv is exactly ``[binary, --gain=<db>, <config>]``.
    It writes to ``devices.playback.filename`` inside the config it is
    handed. This fake reproduces that: it parses the config path out of
    argv, reads the destination that config declares, and writes THERE.

    A stub that instead writes to whatever path the test picked cannot catch
    a caller whose idea of the destination has drifted from the config's —
    which is exactly how that defect survived into main. Returns the
    destinations written, in call order.
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


def _stub_render_config_writing(
    monkeypatch: pytest.MonkeyPatch, *, shas: tuple[str, ...]
) -> list[Path]:
    """Patch render_config to return canned invocations AND create the output
    file, so the move-aside step has something real to move.

    Returns the list of ``config_path`` values the calls received, in order —
    which is how a test asserts R8's "one shape, rendered twice".
    """

    config_paths: list[Path] = []

    def _render_config(  # type: ignore[no-untyped-def]
        binary_path, config_path, *, output_path, bounds, fader_db
    ) -> render.RenderInvocation:
        sha = shas[len(config_paths) % len(shas)]
        config_paths.append(config_path)
        output_path.write_bytes(b"x")
        return _fake_render_invocation(output_sha256=sha)

    monkeypatch.setattr(render, "render_config", _render_config)
    return config_paths


def test_determinism_receipt_passes_on_identical_renders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_paths = _stub_render_config_writing(monkeypatch, shas=("same-sha",))
    config_path, declared = _write_render_config(tmp_path)
    receipt = render.render_with_determinism_receipt(
        "/opt/camilladsp/camilladsp",
        config_path,
        declared_output_path=declared,
        first_output_path=tmp_path / "out.first",
        second_output_path=tmp_path / "out.second",
        bounds=_BOUNDS,
        fader_db=0.0,
    )
    assert receipt.deterministic
    # ONE shape, rendered twice — R8's own words. Both renders run the same
    # config path; this is the property whose absence made the previous
    # revision produce two distinct shapes rendered once each.
    assert config_paths == [config_path, config_path]
    # The recorded shape identity is the SHA of the file that was actually
    # rendered, read from disk here rather than passed in alongside it.
    assert receipt.config_sha256 == render.config_shape_sha256(
        config_path.read_text(encoding="utf-8")
    )


def test_determinism_receipt_refuses_on_byte_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_render_config_writing(monkeypatch, shas=("sha-one", "sha-two"))
    config_path, declared = _write_render_config(tmp_path)
    with pytest.raises(render.RenderError, match="non-deterministic"):
        render.render_with_determinism_receipt(
            "/opt/camilladsp/camilladsp",
            config_path,
            declared_output_path=declared,
            first_output_path=tmp_path / "out.first",
            second_output_path=tmp_path / "out.second",
            bounds=_BOUNDS,
            fader_db=0.0,
        )


def test_determinism_receipt_renders_the_same_config_twice_and_preserves_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression test for the render-destination defect.

    Against the faithful fake binary — which writes where the config says,
    not where the caller wished — the declared destination must be written
    twice and each output preserved. The defective revision handed
    ``render_config`` an ``output_path`` the config did not name; the binary
    wrote elsewhere and the render died on its own output assertion with
    "render exited 0 but produced no output file".
    """

    written = _stub_faithful_render_binary(monkeypatch)
    config_path, declared = _write_render_config(tmp_path)
    first_output = tmp_path / "out.first"
    second_output = tmp_path / "out.second"

    receipt = render.render_with_determinism_receipt(
        "/opt/camilladsp/camilladsp",
        config_path,
        declared_output_path=declared,
        first_output_path=first_output,
        second_output_path=second_output,
        bounds=_BOUNDS,
        fader_db=-17.5,
    )

    assert receipt.deterministic
    # The binary wrote the SAME declared destination both times — one shape,
    # rendered twice, rather than two shapes rendered once each.
    assert written == [declared, declared]
    # Both outputs survived, moved aside; the destination itself is empty.
    assert first_output.read_bytes() == second_output.read_bytes() == b"rendered"
    assert not declared.exists()
    assert receipt.first.output_sha256 == receipt.second.output_sha256
    # Both invocations carry the same config and the same one-token gain.
    assert receipt.first.argv == receipt.second.argv
    assert receipt.first.argv[1] == "--gain=-17.5"
    assert receipt.first.argv[2] == str(config_path)


def test_determinism_receipt_second_render_must_recreate_the_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A binary that writes nothing on the second render is caught, not passed.

    The catch belongs to :func:`render_config`, which unlinks its destination
    before every render and asserts a file appeared afterwards — so it holds
    whether or not render 1's output was moved aside. What the move-aside
    contributes is preservation: both outputs survive to be compared. This
    case pins the catch."""

    calls = {"n": 0}

    def _responder(argv, kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            config_path = Path(argv[-1])
            parsed = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            Path(parsed["devices"]["playback"]["filename"]).write_bytes(b"rendered")
        # The second invocation writes nothing at all.
        return _FakeCompleted(returncode=0)

    _stub_subprocess_run(monkeypatch, _responder)
    config_path, declared = _write_render_config(tmp_path)
    with pytest.raises(render.RenderError, match="produced no output file"):
        render.render_with_determinism_receipt(
            "/opt/camilladsp/camilladsp",
            config_path,
            declared_output_path=declared,
            first_output_path=tmp_path / "out.first",
            second_output_path=tmp_path / "out.second",
            bounds=_BOUNDS,
            fader_db=0.0,
        )


def test_determinism_receipt_refuses_when_the_config_names_another_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bug's actual lesson, kept as a guard: a caller whose idea of the
    destination has drifted from the config's would have render_config watch
    a file the binary never writes."""

    written = _stub_faithful_render_binary(monkeypatch)
    config_path, _ = _write_render_config(tmp_path)
    with pytest.raises(render.RenderError, match="devices.playback.filename"):
        render.render_with_determinism_receipt(
            "/opt/camilladsp/camilladsp",
            config_path,
            declared_output_path=tmp_path / "somewhere-else.raw",
            first_output_path=tmp_path / "out.first",
            second_output_path=tmp_path / "out.second",
            bounds=_BOUNDS,
            fader_db=0.0,
        )
    # Refused before the first subprocess started.
    assert written == []


def test_determinism_receipt_refuses_a_config_with_no_playback_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    written = _stub_faithful_render_binary(monkeypatch)
    config_path = tmp_path / "headless.yml"
    config_path.write_text("devices: {}\n", encoding="utf-8")
    with pytest.raises(render.RenderError, match="declares no"):
        render.render_with_determinism_receipt(
            "/opt/camilladsp/camilladsp",
            config_path,
            declared_output_path=tmp_path / "out.raw",
            first_output_path=tmp_path / "out.first",
            second_output_path=tmp_path / "out.second",
            bounds=_BOUNDS,
            fader_db=0.0,
        )
    assert written == []


@pytest.mark.parametrize("collide", ["declared", "each_other"])
def test_determinism_receipt_refuses_non_distinct_output_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, collide: str
) -> None:
    """Reusing one path would destroy a render's output before it could be
    compared: the second move would overwrite the first, or the second render
    would unlink what the first preserved."""

    written = _stub_faithful_render_binary(monkeypatch)
    config_path, declared = _write_render_config(tmp_path)
    first_output = declared if collide == "declared" else tmp_path / "out.shared"
    second_output = tmp_path / "out.second" if collide == "declared" else first_output
    with pytest.raises(render.RenderError, match="three distinct files"):
        render.render_with_determinism_receipt(
            "/opt/camilladsp/camilladsp",
            config_path,
            declared_output_path=declared,
            first_output_path=first_output,
            second_output_path=second_output,
            bounds=_BOUNDS,
            fader_db=0.0,
        )
    assert written == []


def test_determinism_receipt_refuses_a_preserved_output_path_already_in_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins the UP-FRONT slot check — the one that fires in practice.

    Path.rename overwrites silently on POSIX, so a .first left behind by a
    campaign that died partway must not be replaced without a word. The
    ``written == []`` assertion is what pins this to the up-front call site
    rather than the pre-move one: the refusal must cost no render.
    """

    written = _stub_faithful_render_binary(monkeypatch)
    config_path, declared = _write_render_config(tmp_path)
    stale = tmp_path / "out.first"
    stale.write_bytes(b"stale-from-an-earlier-run")
    with pytest.raises(render.RenderError, match="already exists"):
        render.render_with_determinism_receipt(
            "/opt/camilladsp/camilladsp",
            config_path,
            declared_output_path=declared,
            first_output_path=stale,
            second_output_path=tmp_path / "out.second",
            bounds=_BOUNDS,
            fader_db=0.0,
        )
    # Refused before any render, and the stale file is untouched.
    assert written == []
    assert stale.read_bytes() == b"stale-from-an-earlier-run"


def test_determinism_receipt_refuses_a_slot_taken_during_the_render_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins the PRE-MOVE slot check, which the up-front one cannot cover.

    Nothing inside the helper can occupy a preserved slot between the
    up-front check and the move — the three paths are proved distinct and
    the only file created in between is the one at the declared destination.
    So the pre-move check exists for a writer OUTSIDE this function touching
    the bundle directory while a render is running, which is what this
    responder simulates. Without that check, ``Path.rename`` would overwrite
    the intruding file in silence.
    """

    first_output = tmp_path / "out.first"

    def _responder(argv, kwargs):  # type: ignore[no-untyped-def]
        parsed = yaml.safe_load(Path(argv[-1]).read_text(encoding="utf-8"))
        Path(parsed["devices"]["playback"]["filename"]).write_bytes(b"rendered")
        # Something else drops a file into the slot this run intends to move
        # its output into — after the up-front check has already passed.
        first_output.write_bytes(b"not ours")
        return _FakeCompleted(returncode=0)

    _stub_subprocess_run(monkeypatch, _responder)
    config_path, declared = _write_render_config(tmp_path)
    with pytest.raises(render.RenderError, match="already exists"):
        render.render_with_determinism_receipt(
            "/opt/camilladsp/camilladsp",
            config_path,
            declared_output_path=declared,
            first_output_path=first_output,
            second_output_path=tmp_path / "out.second",
            bounds=_BOUNDS,
            fader_db=0.0,
        )
    # The intruding file was refused, not silently replaced.
    assert first_output.read_bytes() == b"not ours"


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
