# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Behavior pins for the stdlib-only low-memory deploy health gate."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import shutil
import socket
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "bin" / "jasper-deploy-health"


def _load_script() -> ModuleType:
    loader = importlib.machinery.SourceFileLoader("jasper_deploy_health", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


health = _load_script()


def test_profile_constants_match_canonical_install_profile_owner() -> None:
    from jasper.install_profile import INSTALL_PROFILE_FILE, normalize_install_profile

    assert health.INSTALL_PROFILE_FILE == INSTALL_PROFILE_FILE
    for token in ("full", "streambox", "endpoint", "satellite"):
        marker_profile = (
            "streambox" if token in health.LEGACY_STREAMBOX_INSTALL_PROFILES else token
        )
        assert marker_profile == normalize_install_profile(token)


def test_airplay_intent_constants_match_canonical_source_intent_owner() -> None:
    from jasper import source_intent

    assert health.SOURCE_INTENT_FILE == Path(source_intent.SOURCE_INTENT_ENV)
    assert health.AIRPLAY_INTENT_KEY == source_intent.intent_env_key(
        "shairport-sync.service"
    )
    assert health.MAX_SOURCE_INTENT_BYTES == source_intent._MAX_INTENT_BYTES


@pytest.fixture
def short_socket_dir() -> Iterator[Path]:
    directory = Path(tempfile.mkdtemp(prefix="jdh-", dir="/tmp"))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


class _SplitJsonStatusServer:
    """Tiny real control-socket peer that verifies the STATUS wire request."""

    def __init__(
        self,
        path: Path,
        payloads: list[Any],
        *,
        allow_disconnect: bool = False,
    ) -> None:
        self.path = path
        self.payloads = payloads
        self.allow_disconnect = allow_disconnect
        self.errors: list[BaseException] = []
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(path))
        self._server.listen(len(payloads))
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        try:
            for payload in self.payloads:
                conn, _ = self._server.accept()
                with conn:
                    request = bytearray()
                    while not request.endswith(b"\n"):
                        chunk = conn.recv(64)
                        if not chunk:
                            break
                        request.extend(chunk)
                    assert bytes(request) == b"STATUS\n"
                    encoded = json.dumps(payload, separators=(",", ":")).encode()
                    split = max(1, len(encoded) // 2)
                    conn.sendall(encoded[:split])
                    conn.sendall(encoded[split:])
        except (BrokenPipeError, ConnectionResetError) as exc:
            if not self.allow_disconnect:
                self.errors.append(exc)
        except (AssertionError, OSError, TypeError, ValueError) as exc:
            self.errors.append(exc)
        finally:
            self._server.close()

    def __enter__(self) -> _SplitJsonStatusServer:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._thread.join(timeout=3)
        if self._thread.is_alive():
            self._server.close()
            raise AssertionError(f"status server at {self.path} did not finish")
        if self.errors:
            raise AssertionError(f"status server failed: {self.errors[0]}")


class _ContinuousStatusServer:
    """STATUS peer that never closes while continuously yielding JSON whitespace."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.errors: list[BaseException] = []
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(path))
        self._server.listen(1)
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        try:
            conn, _ = self._server.accept()
            with conn:
                request = bytearray()
                while not request.endswith(b"\n"):
                    request.extend(conn.recv(64))
                assert bytes(request) == b"STATUS\n"
                while True:
                    conn.sendall(b" ")
                    time.sleep(0.001)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (AssertionError, OSError) as exc:
            self.errors.append(exc)
        finally:
            self._server.close()

    def __enter__(self) -> _ContinuousStatusServer:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._thread.join(timeout=3)
        if self._thread.is_alive():
            self._server.close()
            raise AssertionError(f"continuous server at {self.path} did not finish")
        if self.errors:
            raise AssertionError(f"continuous server failed: {self.errors[0]}")


@pytest.fixture
def stub_systemctl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    script = tmp_path / "systemctl"
    script.write_text(
        """#!/bin/sh
set -eu
test "$1" = "is-active"
printf '%s\n' "$2" >> "$SYSTEMCTL_LOG"
case ",${FAKE_INACTIVE_UNITS:-}," in
    *,"$2",*) printf '%s\n' inactive; exit 3 ;;
    *) printf '%s\n' active ;;
esac
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    log = tmp_path / "systemctl.log"
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("SYSTEMCTL_LOG", str(log))
    monkeypatch.setenv("FAKE_INACTIVE_UNITS", "")
    return log


def _fanin_status(
    *,
    output_xruns: Any = 0,
    input_xruns: tuple[Any, ...] = (0,),
    progress_age_ms: Any = 100,
) -> dict[str, Any]:
    return {
        "output": {"xrun_count": output_xruns},
        "inputs": [{"xrun_count": value} for value in input_xruns],
        "watchdog": {"last_progress_age_ms": progress_age_ms},
    }


def _outputd_status(
    *,
    backend: Any = "alsa",
    content_xruns: Any = 0,
    dac_xruns: Any = 0,
    empty_periods: Any = 0,
    eagain_count: Any = 0,
    progress_age_ms: Any = 100,
) -> dict[str, Any]:
    return {
        "backend": backend,
        "content": {
            "xrun_count": content_xruns,
            "empty_periods": empty_periods,
            "eagain_count": eagain_count,
        },
        "dac": {"xrun_count": dac_xruns},
        "watchdog": {"last_progress_age_ms": progress_age_ms},
    }


def _run_main(
    monkeypatch: pytest.MonkeyPatch,
    short_socket_dir: Path,
    *,
    profile: str | None = "full",
    source_intent: str | None = None,
    fanin_payloads: list[Any] | None = None,
    outputd_payload: Any | None = None,
) -> int:
    marker = short_socket_dir / "install_profile"
    if profile is not None:
        marker.write_text(profile + "\n", encoding="utf-8")
    monkeypatch.setattr(health, "INSTALL_PROFILE_FILE", marker)
    source_intent_file = short_socket_dir / "source_intent.env"
    if source_intent is not None:
        source_intent_file.write_text(source_intent, encoding="utf-8")
    monkeypatch.setattr(health, "SOURCE_INTENT_FILE", source_intent_file)
    monkeypatch.setattr(health.time, "sleep", lambda _seconds: None)

    fanin_path = short_socket_dir / "fanin.sock"
    outputd_path = short_socket_dir / "outputd.sock"
    monkeypatch.setattr(health, "FANIN_SOCKET", str(fanin_path))
    monkeypatch.setattr(health, "OUTPUTD_SOCKET", str(outputd_path))
    fanin_payloads = fanin_payloads or [_fanin_status(), _fanin_status()]
    outputd_payload = _outputd_status() if outputd_payload is None else outputd_payload

    with (
        _SplitJsonStatusServer(fanin_path, fanin_payloads),
        _SplitJsonStatusServer(outputd_path, [outputd_payload]),
    ):
        return health.main()


@pytest.mark.parametrize(
    ("marker_content", "expected"),
    [
        (None, "full"),
        ("", "full"),
        ("   \n", "full"),
        ("full\n", "full"),
        ("streambox\n", "streambox"),
        ("endpoint\n", "streambox"),
        ("satellite\n", "streambox"),
    ],
)
def test_read_install_profile_uses_canonical_fallbacks_and_legacy_aliases(
    tmp_path: Path,
    marker_content: str | None,
    expected: str,
) -> None:
    marker = tmp_path / "install_profile"
    if marker_content is not None:
        marker.write_text(marker_content, encoding="utf-8")

    assert health._read_install_profile(marker) == expected


def test_airplay_intent_reader_matches_last_wins_quoted_env_format(
    tmp_path: Path,
) -> None:
    intent = tmp_path / "source_intent.env"
    intent.write_text(
        "# household source choices\n"
        f"{health.AIRPLAY_INTENT_KEY}=disabled\n"
        "UNRELATED=value\n"
        f"  {health.AIRPLAY_INTENT_KEY} = 'enabled'  \n",
        encoding="utf-8",
    )

    assert health._airplay_expected_active(intent) is True


def test_airplay_intent_reader_rejects_oversized_or_invalid_utf8(
    tmp_path: Path,
) -> None:
    intent = tmp_path / "source_intent.env"
    intent.write_text("x" * (health.MAX_SOURCE_INTENT_BYTES + 1), encoding="utf-8")
    with pytest.raises(RuntimeError, match="exceeds.*byte cap"):
        health._airplay_expected_active(intent)

    intent.write_bytes(b"\xff")
    with pytest.raises(RuntimeError, match="cannot decode.*UTF-8"):
        health._airplay_expected_active(intent)


def test_status_json_reads_split_object_from_real_unix_socket(
    short_socket_dir: Path,
) -> None:
    path = short_socket_dir / "one.sock"
    payload = {"watchdog": {"last_progress_age_ms": 7}, "inputs": [1, 2]}

    with _SplitJsonStatusServer(path, [payload]):
        assert health._status_json(str(path)) == payload


def test_status_json_rejects_non_object_json(short_socket_dir: Path) -> None:
    path = short_socket_dir / "list.sock"
    with _SplitJsonStatusServer(path, [["not", "an", "object"]]):
        with pytest.raises(RuntimeError, match="returned non-object JSON"):
            health._status_json(str(path))


def test_status_json_rejects_oversized_response(short_socket_dir: Path) -> None:
    path = short_socket_dir / "large.sock"
    payload = {"padding": "x" * health.MAX_STATUS_RESPONSE_BYTES}
    with _SplitJsonStatusServer(path, [payload], allow_disconnect=True):
        with pytest.raises(RuntimeError, match="response exceeds.*byte cap"):
            health._status_json(str(path))


def test_status_json_deadline_bounds_continuous_stream(
    short_socket_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = short_socket_dir / "continuous.sock"
    monkeypatch.setattr(health, "STATUS_RESPONSE_DEADLINE_SECONDS", 0.05)

    started = health.time.monotonic()
    with _ContinuousStatusServer(path):
        with pytest.raises(RuntimeError, match="response exceeded.*deadline"):
            health._status_json(str(path))
    assert health.time.monotonic() - started < 1.0


def test_full_profile_main_requires_input_and_observes_voice_aec(
    monkeypatch: pytest.MonkeyPatch,
    short_socket_dir: Path,
    stub_systemctl: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _run_main(monkeypatch, short_socket_dir, profile="full") == 0

    output = capsys.readouterr().out
    queried = stub_systemctl.read_text(encoding="utf-8").splitlines()
    assert "profile=full" in output
    assert "jasper-input.service" in queried
    assert "jasper-voice.service" in queried
    assert "jasper-aec-bridge.service" in queried
    assert "deploy health passed" in output


@pytest.mark.parametrize("profile", ["streambox", "endpoint", "satellite"])
def test_streambox_profiles_skip_parked_brain_and_input_units(
    monkeypatch: pytest.MonkeyPatch,
    short_socket_dir: Path,
    stub_systemctl: Path,
    capsys: pytest.CaptureFixture[str],
    profile: str,
) -> None:
    monkeypatch.setenv(
        "FAKE_INACTIVE_UNITS",
        "jasper-input.service,jasper-voice.service,jasper-aec-bridge.service",
    )

    assert _run_main(monkeypatch, short_socket_dir, profile=profile) == 0

    output = capsys.readouterr().out
    queried = stub_systemctl.read_text(encoding="utf-8").splitlines()
    assert "profile=streambox" in output
    assert "jasper-input.service" not in queried
    assert "jasper-voice.service" not in queried
    assert "jasper-aec-bridge.service" not in queried
    assert "deploy health passed: 0 failure(s), 0 warning(s)" in output


@pytest.mark.parametrize("profile", ["full", "streambox"])
@pytest.mark.parametrize(
    ("intent", "expected_detail"),
    [
        ("enabled", "active"),
        ("disabled", "inactive (disabled by persisted source intent)"),
    ],
)
def test_main_honors_airplay_source_intent_for_both_profiles(
    monkeypatch: pytest.MonkeyPatch,
    short_socket_dir: Path,
    stub_systemctl: Path,
    capsys: pytest.CaptureFixture[str],
    profile: str,
    intent: str,
    expected_detail: str,
) -> None:
    if intent == "disabled":
        monkeypatch.setenv("FAKE_INACTIVE_UNITS", "shairport-sync.service")
    text = f"{health.AIRPLAY_INTENT_KEY}={intent}\n"

    assert (
        _run_main(
            monkeypatch,
            short_socket_dir,
            profile=profile,
            source_intent=text,
        )
        == 0
    )

    output = capsys.readouterr().out
    queried = stub_systemctl.read_text(encoding="utf-8").splitlines()
    assert "shairport-sync.service" in queried
    assert expected_detail in output


def test_main_fails_when_airplay_runs_despite_disabled_source_intent(
    monkeypatch: pytest.MonkeyPatch,
    short_socket_dir: Path,
    stub_systemctl: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    text = f"{health.AIRPLAY_INTENT_KEY}=disabled\n"

    assert _run_main(monkeypatch, short_socket_dir, source_intent=text) == 1

    output = capsys.readouterr().out
    assert (
        "shairport-sync.service"
        in stub_systemctl.read_text(encoding="utf-8").splitlines()
    )
    assert "FAIL shairport-sync.service" in output
    assert "active; expected inactive from persisted source intent" in output


def test_main_fails_closed_on_malformed_airplay_source_intent(
    monkeypatch: pytest.MonkeyPatch,
    short_socket_dir: Path,
    stub_systemctl: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    text = f"{health.AIRPLAY_INTENT_KEY}=maybe\n"

    assert _run_main(monkeypatch, short_socket_dir, source_intent=text) == 1

    output = capsys.readouterr().out
    queried = stub_systemctl.read_text(encoding="utf-8").splitlines()
    assert "FAIL source intent" in output
    assert "expected enabled or disabled" in output
    assert "disabled by persisted source intent" not in output
    assert "shairport-sync.service" not in queried


def test_invalid_profile_fails_closed_before_runtime_probes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stub_systemctl: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    marker = tmp_path / "install_profile"
    marker.write_text("bogus\n", encoding="utf-8")
    monkeypatch.setattr(health, "INSTALL_PROFILE_FILE", marker)

    assert health.main() == 1

    output = capsys.readouterr().out
    assert "FAIL install profile" in output
    assert "invalid install profile 'bogus'" in output
    assert not stub_systemctl.exists()


def test_inactive_required_unit_fails_main(
    monkeypatch: pytest.MonkeyPatch,
    short_socket_dir: Path,
    stub_systemctl: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("FAKE_INACTIVE_UNITS", "jasper-control.service")

    assert _run_main(monkeypatch, short_socket_dir) == 1

    output = capsys.readouterr().out
    assert "FAIL jasper-control.service" in output
    assert "deploy health failed: 1 failure(s), 0 warning(s)" in output


def test_inactive_optional_unit_warns_without_failing(
    monkeypatch: pytest.MonkeyPatch,
    short_socket_dir: Path,
    stub_systemctl: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("FAKE_INACTIVE_UNITS", "bt-agent.service")

    assert _run_main(monkeypatch, short_socket_dir) == 0

    output = capsys.readouterr().out
    assert "WARN bt-agent.service" in output
    assert "deploy health passed: 0 failure(s), 1 warning(s)" in output


@pytest.mark.parametrize(
    ("fanin_payloads", "outputd_payload", "detail"),
    [
        (
            [_fanin_status(), _fanin_status(output_xruns=1)],
            _outputd_status(),
            "output_xrun_delta=1",
        ),
        (
            [_fanin_status(), _fanin_status(input_xruns=(1,))],
            _outputd_status(),
            "input_xrun_delta=1",
        ),
        (
            [_fanin_status(), _fanin_status(progress_age_ms=2001)],
            _outputd_status(),
            "progress_age_ms=2001",
        ),
        (None, _outputd_status(backend="pipewire"), "backend='pipewire'"),
        (None, _outputd_status(content_xruns=1), "xruns=1/0"),
        (None, _outputd_status(dac_xruns=1), "xruns=0/1"),
        (None, _outputd_status(empty_periods=1), "empty=1"),
        (None, _outputd_status(eagain_count=1), "eagain=1"),
        (None, _outputd_status(progress_age_ms=2001), "progress_age_ms=2001"),
    ],
)
def test_main_fails_on_xrun_progress_or_outputd_health_regression(
    monkeypatch: pytest.MonkeyPatch,
    short_socket_dir: Path,
    stub_systemctl: Path,
    capsys: pytest.CaptureFixture[str],
    fanin_payloads: list[Any] | None,
    outputd_payload: Any,
    detail: str,
) -> None:
    assert (
        _run_main(
            monkeypatch,
            short_socket_dir,
            fanin_payloads=fanin_payloads,
            outputd_payload=outputd_payload,
        )
        == 1
    )

    assert detail in capsys.readouterr().out


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"output": {"xrun_count": 0}}, "inputs is missing"),
        ({"output": {"xrun_count": 0}, "inputs": []}, "inputs is missing"),
        ({"output": {"xrun_count": 0}, "inputs": [None]}, "inputs.0 is not an object"),
        ({"output": {"xrun_count": 0}, "inputs": [{}]}, "xrun_count is None"),
        (_fanin_status(output_xruns=True), "xrun_count is True"),
        (_fanin_status(input_xruns=(False,)), "xrun_count is False"),
        (_fanin_status(input_xruns=("0",)), "xrun_count is '0'"),
        (_fanin_status(input_xruns=(-1,)), "xrun_count is -1"),
    ],
)
def test_fanin_schema_requires_nonempty_inputs_and_strict_counters(
    payload: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(RuntimeError, match=message):
        health._fanin_xrun_counts(payload)


@pytest.mark.parametrize("value", [None, True, False, "1", -1])
def test_nested_progress_requires_nonnegative_integer(value: Any) -> None:
    payload = {"watchdog": {"last_progress_age_ms": value}}
    with pytest.raises(RuntimeError, match="not a nonnegative int"):
        health._progress_age_ms(payload)


@pytest.mark.parametrize(
    ("fanin_payloads", "outputd_payload", "detail"),
    [
        (
            [_fanin_status(input_xruns=(True,))],
            _outputd_status(),
            "not a nonnegative int",
        ),
        (
            [_fanin_status(), _fanin_status(progress_age_ms=False)],
            _outputd_status(),
            "not a nonnegative int",
        ),
        (None, _outputd_status(content_xruns=True), "not a nonnegative int"),
        (None, {"backend": "alsa"}, "content.xrun_count is not present"),
    ],
)
def test_main_fails_closed_on_malformed_status_schema(
    monkeypatch: pytest.MonkeyPatch,
    short_socket_dir: Path,
    stub_systemctl: Path,
    capsys: pytest.CaptureFixture[str],
    fanin_payloads: list[Any] | None,
    outputd_payload: Any,
    detail: str,
) -> None:
    assert (
        _run_main(
            monkeypatch,
            short_socket_dir,
            fanin_payloads=fanin_payloads,
            outputd_payload=outputd_payload,
        )
        == 1
    )
    output = capsys.readouterr().out
    assert "FAIL" in output
    assert detail in output
