# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contracts for the optional enhanced-AEC lifecycle."""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from jasper import enhanced_aec
from jasper.cli import aec_bridge_engines
from jasper.cli import enhanced_aec_install


def _write_source(root: Path, *, value: str = "one") -> None:
    (root / "jasper_aec3").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "enhanced-aec-source.env").write_text(
        "WEBRTC_AEC3_VERSION=v2.1\n"
        "WEBRTC_AEC3_COMMIT=846fe90\n"
        "WEBRTC_AEC3_ARCHIVE_URL=https://example.test/source.tar.gz\n"
        "WEBRTC_AEC3_SHA256=" + "a" * 64 + "\n",
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text("[project]\nname='test'\n")
    (root / "setup.py").write_text(f"VALUE = {value!r}\n")
    (root / "jasper_aec3" / "__init__.py").write_text("")
    (root / "src" / "aec3_binding_v2.cpp").write_text(f"// {value}\n")


@pytest.fixture
def capability(tmp_path: Path):
    source = tmp_path / "source"
    venv = tmp_path / "venv"
    state = tmp_path / "state"
    _write_source(source)
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").symlink_to(sys.executable)
    package = venv / "lib" / "python3.13" / "site-packages" / "jasper_aec3"
    package.mkdir(parents=True)
    state.mkdir()
    return SimpleNamespace(
        source=source,
        venv=venv,
        package=package,
        intent=state / "intent.json",
        job=state / "job.json",
        marker=state / "installed.json",
        state_lock=state / ".state.lock",
        install_lock=state / ".install.lock",
    )


def _status(capability, **kwargs):
    return enhanced_aec.status(
        source_root=capability.source,
        venv_root=capability.venv,
        intent_path=capability.intent,
        job_state_path=capability.job,
        installed_marker_path=capability.marker,
        machine="aarch64",
        **kwargs,
    )


def test_default_is_v1_ready_optional_not_installed(capability):
    result = _status(capability)
    assert result["state"] == "not_installed"
    assert result["requested"] is False
    assert result["installed"] is False
    assert result["current"] is False
    assert result["engine"] == "v1"
    assert result["action"]["enabled"] is True


def test_request_keeps_durable_intent_separate_from_installed_proof(capability):
    enhanced_aec.request_install(
        intent_path=capability.intent,
        job_state_path=capability.job,
        state_lock_path=capability.state_lock,
    )
    assert json.loads(capability.intent.read_text())["requested"] is True
    assert json.loads(capability.job.read_text())["phase"] == "queued"
    assert not capability.marker.exists()
    result = _status(capability)
    assert result["state"] == "installing"
    assert result["requested"] is True
    assert result["installed"] is False


def test_inactive_old_build_phase_is_interrupted_not_installing_forever(capability):
    capability.intent.write_text('{"schema_version":1,"requested":true}\n')
    capability.job.write_text(
        json.dumps({
            "schema_version": 1,
            "phase": "building",
            "detail": "Building.",
            "updated_at": "2000-01-01T00:00:00Z",
        })
    )
    result = _status(capability, service_active=False)
    assert result["state"] == "failed"
    assert "interrupted" in result["detail"].lower()
    assert result["action"]["enabled"] is True


def test_live_service_is_authoritative_for_installing(capability):
    capability.intent.write_text('{"schema_version":1,"requested":true}\n')
    capability.job.write_text(
        '{"schema_version":1,"phase":"building",'
        '"updated_at":"2000-01-01T00:00:00Z"}\n'
    )
    assert _status(capability, service_active=True)["state"] == "installing"


def test_chip_aec_overlay_is_not_needed_without_losing_underlying_truth(capability):
    result = _status(capability, chip_aec_active=True)
    assert result["state"] == "not_needed"
    assert result["engine"] == "chip"
    assert result["action"]["enabled"] is False


def test_streambox_never_offers_or_runs_voice_brain_enhancement(
    capability,
    monkeypatch,
):
    monkeypatch.setattr(enhanced_aec, "read_install_profile", lambda: "streambox")

    result = _status(capability)

    assert result["state"] == "not_needed"
    assert result["unavailable_reason"] == "voice_brain_not_installed"
    assert result["action"]["enabled"] is False
    assert enhanced_aec_install.install(
        source_root=capability.source,
        venv_root=capability.venv,
        cache_root=capability.source.parent / "cache",
    ) == {
        "changed": False,
        "reason": "voice_brain_not_installed",
    }


def test_job_transitions_emit_stable_low_volume_events(capability, monkeypatch):
    events: list[tuple[str, dict[str, str]]] = []
    monkeypatch.setattr(
        enhanced_aec,
        "log_event",
        lambda _logger, event, **fields: events.append((event, fields)),
    )

    enhanced_aec.request_install(
        intent_path=capability.intent,
        job_state_path=capability.job,
        state_lock_path=capability.state_lock,
    )
    enhanced_aec.write_job_state(
        "building",
        "compiler details stay out of event fields",
        path=capability.job,
        state_lock_path=capability.state_lock,
    )
    enhanced_aec.write_job_state(
        "failed",
        "safe product summary",
        error="raw compiler output",
        path=capability.job,
        state_lock_path=capability.state_lock,
    )

    assert events == [
        ("enhanced_aec.install_phase", {"phase": "queued"}),
        ("enhanced_aec.install_phase", {"phase": "building"}),
        ("enhanced_aec.install_terminal", {"result": "failed"}),
    ]


def _publish_marker(capability, extension: Path) -> str:
    fingerprint = enhanced_aec.desired_fingerprint(capability.source)
    capability.marker.write_text(
        json.dumps({
            "schema_version": 1,
            "fingerprint": fingerprint,
            "extension_sha256": enhanced_aec.extension_sha256(extension),
            "extension_name": extension.name,
        })
    )
    return fingerprint


def test_verified_marker_and_digest_make_v2_current(capability):
    extension = capability.package / "_aec3_v2.cpython-313-aarch64-linux-gnu.so"
    extension.write_bytes(b"verified native module")
    fingerprint = _publish_marker(capability, extension)
    result = _status(capability)
    assert result["state"] == "installed"
    assert result["installed_fingerprint"] == fingerprint
    assert result["current"] is True
    assert result["engine"] == "v2"
    assert enhanced_aec.runtime_v2_verified(
        source_root=capability.source,
        venv_root=capability.venv,
        installed_marker_path=capability.marker,
    )


def test_source_change_marks_installed_extension_stale(capability):
    extension = capability.package / "_aec3_v2.cpython-313-aarch64-linux-gnu.so"
    extension.write_bytes(b"old native module")
    _publish_marker(capability, extension)
    (capability.source / "src" / "aec3_binding_v2.cpp").write_text("// changed\n")
    result = _status(capability)
    assert result["state"] == "stale"
    assert result["installed"] is True
    assert result["current"] is False
    assert result["engine"] == "v1"


def test_crash_window_orphan_so_is_never_runtime_authorized(capability):
    """Power loss after SO replace but before marker publish stays on v1."""

    extension = capability.package / "_aec3_v2.cpython-313-aarch64-linux-gnu.so"
    extension.write_bytes(b"uncommitted native module")
    assert not capability.marker.exists()
    assert not enhanced_aec.runtime_v2_verified(
        source_root=capability.source,
        venv_root=capability.venv,
        installed_marker_path=capability.marker,
    )
    result = _status(capability)
    assert result["installed"] is False
    assert result["current"] is False
    assert result["engine"] == "v1"


def test_unknown_or_missing_persisted_schema_fails_closed(capability):
    capability.intent.write_text('{"requested":true}\n')
    capability.job.write_text(
        '{"schema_version":2,"phase":"building",'
        '"updated_at":"2999-01-01T00:00:00Z"}\n'
    )
    extension = capability.package / "_aec3_v2.cpython-313-aarch64-linux-gnu.so"
    extension.write_bytes(b"native module")
    capability.marker.write_text(
        json.dumps({
            "schema_version": 2,
            "fingerprint": enhanced_aec.desired_fingerprint(capability.source),
            "extension_sha256": enhanced_aec.extension_sha256(extension),
            "extension_name": extension.name,
        })
    )

    assert enhanced_aec.read_intent(path=capability.intent)["requested"] is False
    assert enhanced_aec.read_job_state(path=capability.job) == {}
    assert enhanced_aec.read_installed_marker(path=capability.marker) == {}
    assert not enhanced_aec.runtime_v2_verified(
        source_root=capability.source,
        venv_root=capability.venv,
        installed_marker_path=capability.marker,
    )
    result = _status(capability, service_active=False)
    assert result["state"] == "not_installed"
    assert result["requested"] is False
    assert result["installed"] is False


def test_installed_marker_uses_root_proof_publish_contract(
    tmp_path: Path, monkeypatch,
):
    calls: list[tuple[Path, str, dict[str, object]]] = []

    def fake_atomic_write(path, text, **kwargs):
        calls.append((Path(path), text, kwargs))

    monkeypatch.setattr(enhanced_aec, "atomic_write_text", fake_atomic_write)
    marker = tmp_path / "proof" / "installed.json"
    enhanced_aec.write_installed_marker(
        fingerprint="enhanced-aec-v1:fingerprint",
        extension_sha256="a" * 64,
        extension_name="_aec3_v2.test.so",
        path=marker,
    )

    assert len(calls) == 1
    path, text, options = calls[0]
    assert path == marker
    assert options == {
        "mode": 0o644,
        "group_from_parent": False,
        "durable": True,
    }
    assert json.loads(text)["fingerprint"] == "enhanced-aec-v1:fingerprint"
    assert enhanced_aec.INSTALLED_MARKER_PATH == (
        Path("/var/lib/jasper-enhanced-aec") / "installed.json"
    )


def test_select_engine_does_not_import_unverified_v2(monkeypatch):
    selected: list[str] = []

    monkeypatch.setattr(enhanced_aec, "runtime_v2_verified", lambda: False)
    monkeypatch.setattr(
        aec_bridge_engines,
        "Aec3V1Engine",
        lambda **_kwargs: selected.append("v1") or "v1",
    )
    monkeypatch.setattr(
        aec_bridge_engines,
        "Aec3V2Engine",
        lambda **_kwargs: selected.append("v2") or "v2",
    )
    monkeypatch.setenv("JASPER_AEC_BINDING", "auto")
    assert aec_bridge_engines.select_engine() == "v1"
    assert selected == ["v1"]


def test_jasper_aec3_v2_native_module_is_genuinely_lazy(tmp_path: Path):
    """Importing and constructing v1 must never execute an orphan v2 module."""

    package = tmp_path / "jasper_aec3"
    package.mkdir()
    source = Path("jasper_aec3/jasper_aec3/__init__.py")
    (package / "__init__.py").write_text(source.read_text(encoding="utf-8"))
    (package / "_aec3.py").write_text(
        "class Aec3:\n    pass\n",
        encoding="utf-8",
    )
    (package / "_aec3_v2.py").write_text(
        "import builtins\n"
        "builtins._JASPER_TEST_V2_IMPORTS = "
        "getattr(builtins, '_JASPER_TEST_V2_IMPORTS', 0) + 1\n"
        "class Aec3V2:\n    pass\n",
        encoding="utf-8",
    )
    script = textwrap.dedent(
        """
        import builtins
        import sys

        import jasper_aec3

        assert jasper_aec3.HAS_V2 is True
        assert "jasper_aec3._aec3_v2" not in sys.modules
        assert not hasattr(builtins, "_JASPER_TEST_V2_IMPORTS")
        jasper_aec3.Aec3()
        assert "jasper_aec3._aec3_v2" not in sys.modules
        assert not hasattr(builtins, "_JASPER_TEST_V2_IMPORTS")
        jasper_aec3.Aec3V2()
        assert "jasper_aec3._aec3_v2" in sys.modules
        assert builtins._JASPER_TEST_V2_IMPORTS == 1
        """
    )
    subprocess.run(
        [sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(tmp_path)!r})\n{script}"],
        check=True,
        capture_output=True,
        text=True,
    )


def test_select_engine_uses_v2_only_after_verification(monkeypatch):
    selected: list[str] = []

    monkeypatch.setattr(enhanced_aec, "runtime_v2_verified", lambda: True)
    monkeypatch.setattr(
        aec_bridge_engines,
        "Aec3V2Engine",
        lambda **_kwargs: selected.append("v2") or "v2",
    )
    fake_package = SimpleNamespace(HAS_V2=True)
    monkeypatch.setitem(sys.modules, "jasper_aec3", fake_package)
    monkeypatch.setenv("JASPER_AEC_BINDING", "auto")
    assert aec_bridge_engines.select_engine() == "v2"
    assert selected == ["v2"]


def test_activation_refuses_source_changed_during_build(capability, monkeypatch):
    staged = capability.source.parent / "_aec3_v2.so"
    staged.write_bytes(b"new")
    monkeypatch.setattr(
        enhanced_aec_install,
        "INSTALL_LOCK_PATH",
        capability.install_lock,
    )
    monkeypatch.setattr(
        enhanced_aec_install,
        "desired_fingerprint",
        lambda _root: "enhanced-aec-v1:newer",
    )
    with pytest.raises(enhanced_aec_install.SourceChanged):
        enhanced_aec_install._activate(
            staged,
            "enhanced-aec-v1:snapshot",
            source_root=capability.source,
            venv_root=capability.venv,
        )
    assert not list(capability.package.glob("_aec3_v2*.so"))


def test_activation_publishes_proof_only_after_active_frame_probe(
    capability, tmp_path: Path, monkeypatch,
):
    staged = tmp_path / "_aec3_v2.test.so"
    staged.write_bytes(b"new extension")
    events: list[str] = []
    monkeypatch.setattr(
        enhanced_aec_install,
        "INSTALL_LOCK_PATH",
        capability.install_lock,
    )
    monkeypatch.setattr(
        enhanced_aec_install,
        "desired_fingerprint",
        lambda _root: "enhanced-aec-v1:current",
    )
    monkeypatch.setattr(
        enhanced_aec_install,
        "_v1_package_dir",
        lambda _root: capability.package,
    )
    monkeypatch.setattr(
        enhanced_aec_install,
        "_probe_active",
        lambda **_kwargs: events.append("probe"),
    )
    monkeypatch.setattr(
        enhanced_aec_install,
        "write_installed_marker",
        lambda **_kwargs: events.append("marker"),
    )

    destination = enhanced_aec_install._activate(
        staged,
        "enhanced-aec-v1:current",
        source_root=capability.source,
        venv_root=capability.venv,
    )
    assert destination.read_bytes() == b"new extension"
    assert events == ["probe", "marker"]


def test_failed_active_probe_rolls_back_without_publishing_marker(
    capability, tmp_path: Path, monkeypatch,
):
    staged = tmp_path / "_aec3_v2.test.so"
    staged.write_bytes(b"new extension")
    destination = capability.package / staged.name
    destination.write_bytes(b"previous extension")
    marker_calls: list[dict] = []
    monkeypatch.setattr(
        enhanced_aec_install,
        "INSTALL_LOCK_PATH",
        capability.install_lock,
    )
    monkeypatch.setattr(
        enhanced_aec_install,
        "desired_fingerprint",
        lambda _root: "enhanced-aec-v1:current",
    )
    monkeypatch.setattr(
        enhanced_aec_install,
        "_v1_package_dir",
        lambda _root: capability.package,
    )
    monkeypatch.setattr(
        enhanced_aec_install,
        "_probe_active",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("bad frame")),
    )
    monkeypatch.setattr(
        enhanced_aec_install,
        "write_installed_marker",
        lambda **kwargs: marker_calls.append(kwargs),
    )

    with pytest.raises(RuntimeError, match="bad frame"):
        enhanced_aec_install._activate(
            staged,
            "enhanced-aec-v1:current",
            source_root=capability.source,
            venv_root=capability.venv,
        )
    assert destination.read_bytes() == b"previous extension"
    assert marker_calls == []


def test_setup_has_fail_closed_v1_only_and_v2_only_modes():
    text = Path("jasper_aec3/setup.py").read_text(encoding="utf-8")
    assert '"v1-only", "v2-only"' in text
    assert 'build_mode == "v2-only"' in text
    assert "v2-only requires" in text


def test_native_build_backend_is_exact_pinned_and_fingerprinted(capability):
    metadata = capability.source / "pyproject.toml"
    metadata.write_text(
        "[build-system]\n"
        "requires=['setuptools==82.0.1', 'pybind11==2.13.6']\n"
    )
    before = enhanced_aec.desired_fingerprint(capability.source)
    metadata.write_text(metadata.read_text().replace("2.13.6", "2.13.5"))
    assert enhanced_aec.desired_fingerprint(capability.source) != before

    shipped = Path("jasper_aec3/pyproject.toml").read_text(encoding="utf-8")
    assert "setuptools==83.0.0" in shipped
    assert "pybind11==2.13.6" in shipped
    assert "setuptools>=" not in shipped
    assert "pybind11>=" not in shipped


def test_v2_wheel_uses_isolated_pep517_build(
    capability, tmp_path: Path, monkeypatch,
):
    calls: list[list[str]] = []

    def fake_contained(_label, argv, **_kwargs):
        calls.append(argv)
        destination = Path(argv[argv.index("--wheel-dir") + 1])
        (destination / "jasper_aec3-0.1.0-test.whl").write_bytes(b"wheel")
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(enhanced_aec_install, "_run_contained", fake_contained)
    wheel = enhanced_aec_install._build_v2_wheel(
        capability.source,
        tmp_path / "webrtc",
        tmp_path / "wheels",
        venv_root=capability.venv,
    )
    assert wheel.is_file()
    argv = calls[0]
    assert argv[:4] == [
        str(capability.venv / "bin" / "python"),
        "-m",
        "pip",
        "wheel",
    ]
    assert "--no-build-isolation" not in argv


def test_command_failure_keeps_bounded_redacted_diagnostic_tail(monkeypatch):
    secret = "do-not-persist-this-token"
    stderr = "old context\n" + ("x" * 6000) + f"\ntoken={secret}\nreal compiler error"

    def fail(*_args, **kwargs):
        assert kwargs.get("capture_output") is None
        assert kwargs["stderr"] is subprocess.STDOUT
        kwargs["stdout"].write(stderr.encode())
        kwargs["stdout"].flush()
        raise subprocess.CalledProcessError(
            7,
            ["compiler", "--private-argument"],
        )

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(enhanced_aec.EnhancedAecError) as caught:
        enhanced_aec_install._run(
            ["compiler", "--private-argument"],
            timeout=12.0,
        )
    message = str(caught.value)
    assert message.startswith("compiler exited with status 7; output tail:")
    assert "real compiler error" in message
    assert secret not in message
    assert "token=[redacted]" in message
    assert "--private-argument" not in message
    assert len(message) <= enhanced_aec_install.MAX_FAILURE_OUTPUT_CHARS + 100


def test_command_timeout_is_bounded_and_does_not_dump_argv(monkeypatch):
    def timeout(*_args, **kwargs):
        kwargs["stdout"].write(
            b"authorization=very-secret\nnetwork stalled"
        )
        kwargs["stdout"].flush()
        raise subprocess.TimeoutExpired(
            ["curl", "https://user:password@example.test/archive"],
            3.0,
        )

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(enhanced_aec.EnhancedAecError) as caught:
        enhanced_aec_install._run(
            ["curl", "https://user:password@example.test/archive"],
            timeout=3.0,
        )
    message = str(caught.value)
    assert message.startswith("curl timed out after 3 seconds")
    assert "network stalled" in message
    assert "very-secret" not in message
    assert "user:password" not in message


def test_runtime_refresh_failure_is_nonfatal_and_defers_to_next_restart(
    capability, monkeypatch,
):
    monkeypatch.setattr(
        enhanced_aec_install,
        "_run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("systemd unavailable")
        ),
    )
    assert "systemd unavailable" in enhanced_aec_install._refresh_aec_runtime()
