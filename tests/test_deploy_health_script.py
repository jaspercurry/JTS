# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Behavior pins for the stdlib-only low-memory deploy health gate."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
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


def test_source_intent_constants_match_canonical_owner_and_fixed_contract() -> None:
    from jasper import source_intent

    assert health.SOURCE_INTENT_FILE == Path(source_intent.SOURCE_INTENT_ENV)
    assert health.AIRPLAY_INTENT_KEY == source_intent.intent_env_key(
        "shairport-sync.service"
    )
    assert health.SPOTIFY_INTENT_KEY == source_intent.intent_env_key(
        "librespot.service"
    )
    # Bluetooth has no systemd intent unit, so the source-level key is the
    # fixed compatibility contract shared with the lifecycle coordinator.
    assert health.BLUETOOTH_INTENT_KEY == "JASPER_BLUETOOTH_SOURCE_INTENT"
    assert health.USBSINK_INTENT_KEY == source_intent.intent_env_key(
        "jasper-usbsink.service"
    )
    assert health.SOURCE_INTENT_KEYS == {
        "airplay": health.AIRPLAY_INTENT_KEY,
        "spotify": health.SPOTIFY_INTENT_KEY,
        "bluetooth": health.BLUETOOTH_INTENT_KEY,
        "usbsink": health.USBSINK_INTENT_KEY,
    }
    assert health.SOURCE_INTENT_DEFAULTS == {
        "airplay": True,
        "spotify": True,
        "bluetooth": True,
        "usbsink": False,
    }
    assert health.SOURCE_REQUIRED_UNITS == {
        "airplay": ("shairport-sync.service", "nqptp.service"),
        "spotify": ("librespot.service",),
        "bluetooth": (
            "bluealsa.service",
            "bluealsa-aplay.service",
            "bt-agent.service",
        ),
        "usbsink": ("jasper-usbsink.service",),
    }
    assert health.MAX_SOURCE_INTENT_BYTES == source_intent._MAX_INTENT_BYTES
    assert health.SOURCE_RECONCILE_STATUS_FILE == Path(source_intent.SOURCE_STATUS_PATH)


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


def _set_inactive_units(
    monkeypatch: pytest.MonkeyPatch,
    *units: str,
) -> None:
    monkeypatch.setenv("FAKE_INACTIVE_UNITS", ",".join(sorted(set(units))))


def _fanin_status(
    *,
    output_xruns: Any = 0,
    input_xruns: tuple[Any, ...] = (0,),
    progress_age_ms: Any = 100,
    usb_direct: bool = False,
    usb_present: Any = True,
    usb_health: Any = "idle",
) -> dict[str, Any]:
    inputs = [{"xrun_count": value} for value in input_xruns]
    if usb_direct:
        inputs[0].update({
            "label": "usbsink",
            "source": "direct",
            "direct": {"present": usb_present, "health": usb_health},
        })
    return {
        "output": {"xrun_count": output_xruns},
        "inputs": inputs,
        "watchdog": {"last_progress_age_ms": progress_age_ms},
    }


def _outputd_status(
    *,
    uptime_seconds: Any = 100.0,
    backend: Any = "alsa",
    content_xruns: Any = 0,
    dac_xruns: Any = 0,
    empty_periods: Any = 0,
    eagain_count: Any = 0,
    progress_age_ms: Any = 100,
) -> dict[str, Any]:
    return {
        "uptime_seconds": uptime_seconds,
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
    grouping: str | None = None,
    radio_issues: list[str] | None = None,
    fanin_payloads: list[Any] | None = None,
    outputd_payload: Any | None = None,
    outputd_payloads: list[Any] | None = None,
    usb_runtime_matches_intent: bool = True,
    usb_card_present: bool | None = None,
    usb_effective: str | None = None,
    usb_effective_reason: str = "",
    usb_status_text: str | None = None,
    in_process_status: bool = True,
) -> int:
    marker = short_socket_dir / "install_profile"
    if profile is not None:
        marker.write_text(profile + "\n", encoding="utf-8")
    monkeypatch.setattr(health, "INSTALL_PROFILE_FILE", marker)
    source_intent_file = short_socket_dir / "source_intent.env"
    if source_intent is not None:
        source_intent_file.write_text(source_intent, encoding="utf-8")
    monkeypatch.setattr(health, "SOURCE_INTENT_FILE", source_intent_file)
    grouping_file = short_socket_dir / "grouping.env"
    if grouping is not None:
        grouping_file.write_text(grouping, encoding="utf-8")
    monkeypatch.setattr(health, "GROUPING_ENV_FILE", grouping_file)
    monkeypatch.setattr(
        health,
        "_bluetooth_radio_issues",
        lambda _desired: [] if radio_issues is None else radio_issues,
    )
    monkeypatch.setattr(health.time, "sleep", lambda _seconds: None)

    try:
        saved_usb_desired = health._source_expectations_with_fingerprint(
            source_intent_file
        )[0]["usbsink"]
    except RuntimeError:
        saved_usb_desired = False
    if usb_effective is None:
        if grouping and "JASPER_GROUPING_ROLE=follower" in grouping and saved_usb_desired:
            usb_effective = "parked"
        else:
            usb_effective = "on" if saved_usb_desired else "off"
    usb_runtime_on = usb_effective == "on"
    source_status_file = short_socket_dir / "source-status.json"
    if usb_status_text is None:
        intent_bytes = (
            source_intent_file.read_bytes() if source_intent_file.exists() else b""
        )
        intent_fingerprint = health.hashlib.sha256(intent_bytes).hexdigest()
        usb_status_text = json.dumps(
            {
                "completed_monotonic_ns": time.monotonic_ns(),
                "intent_fingerprint": intent_fingerprint,
                "sources": {
                    "usbsink": {
                        "desired": "enabled" if saved_usb_desired else "disabled",
                        "effective": usb_effective,
                        "result": "ok",
                        "reason": usb_effective_reason,
                    }
                }
            }
        )
    source_status_file.write_text(usb_status_text, encoding="utf-8")
    monkeypatch.setattr(
        health,
        "SOURCE_RECONCILE_STATUS_FILE",
        source_status_file,
    )
    inactive = {
        unit for unit in os.environ.get("FAKE_INACTIVE_UNITS", "").split(",")
        if unit
    }
    if usb_runtime_matches_intent:
        if usb_runtime_on:
            inactive.discard("jasper-usbsink.service")
        else:
            inactive.add("jasper-usbsink.service")
        monkeypatch.setenv("FAKE_INACTIVE_UNITS", ",".join(sorted(inactive)))

    card = short_socket_dir / "UAC2Gadget"
    card_should_exist = (
        usb_runtime_on if usb_card_present is None else usb_card_present
    )
    if card_should_exist:
        card.mkdir(exist_ok=True)
    monkeypatch.setattr(health, "UAC2_CARD_PATH", card)

    fanin_path = short_socket_dir / "fanin.sock"
    outputd_path = short_socket_dir / "outputd.sock"
    monkeypatch.setattr(health, "FANIN_SOCKET", str(fanin_path))
    monkeypatch.setattr(health, "OUTPUTD_SOCKET", str(outputd_path))
    fanin_payloads = fanin_payloads or [
        _fanin_status(usb_direct=usb_runtime_on),
        _fanin_status(usb_direct=usb_runtime_on),
    ]
    if outputd_payloads is None:
        outputd_payload = (
            _outputd_status() if outputd_payload is None else outputd_payload
        )
        outputd_later = dict(outputd_payload)
        first_uptime = outputd_payload.get("uptime_seconds")
        if (
            not isinstance(first_uptime, bool)
            and isinstance(first_uptime, (int, float))
        ):
            outputd_later["uptime_seconds"] = first_uptime + 1
        outputd_payloads = [outputd_payload, outputd_later]

    if in_process_status:
        payloads = iter([*fanin_payloads, *outputd_payloads])
        monkeypatch.setattr(health, "_status_json", lambda _path: next(payloads))
        return health.main()

    with (
        _SplitJsonStatusServer(fanin_path, fanin_payloads),
        _SplitJsonStatusServer(outputd_path, outputd_payloads),
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


def test_source_intent_reader_uses_each_fixed_source_default_when_absent(
    tmp_path: Path,
) -> None:
    expectations, _ = health._source_expectations_with_fingerprint(
        tmp_path / "missing.env"
    )
    assert expectations == {
        "airplay": True,
        "spotify": True,
        "bluetooth": True,
        "usbsink": False,
    }


def test_source_intent_fingerprint_matches_coordinator_owner(tmp_path: Path) -> None:
    from jasper.source_intent import _intent_fingerprint

    text = (
        f"{health.AIRPLAY_INTENT_KEY}=disabled\n"
        f"{health.USBSINK_INTENT_KEY}=enabled\n"
    )
    intent = tmp_path / "source_intent.env"
    intent.write_text(text, encoding="utf-8")

    _, fingerprint = health._source_expectations_with_fingerprint(intent)
    assert fingerprint == _intent_fingerprint(text)


def test_usb_effective_status_accepts_unavailable_and_rejects_malformed(
    tmp_path: Path,
) -> None:
    status = tmp_path / "status.json"
    status.write_text(
        json.dumps(
            {
                "completed_monotonic_ns": 1,
                "intent_fingerprint": "0" * 64,
                "sources": {
                    "usbsink": {
                        "desired": "enabled",
                        "effective": "unavailable",
                        "result": "ok",
                        "reason": "shared_otg_usb_output_requires_host",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    assert health._usb_effective_status(status) == (
        False,
        "unavailable",
        "shared_otg_usb_output_requires_host",
    )

    status.write_text("{not-json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="unreadable"):
        health._usb_effective_status(status)


@pytest.mark.parametrize(
    ("desired", "effective", "expected_desired", "message"),
    [
        ("disabled", "unavailable", None, "inconsistent"),
        ("enabled", "off", None, "inconsistent"),
        ("disabled", "off", True, "stale"),
    ],
)
def test_usb_effective_status_rejects_impossible_or_stale_intent(
    tmp_path: Path,
    desired: str,
    effective: str,
    expected_desired: bool | None,
    message: str,
) -> None:
    status = tmp_path / "status.json"
    status.write_text(
        json.dumps(
            {
                "completed_monotonic_ns": 1,
                "intent_fingerprint": "0" * 64,
                "sources": {
                    "usbsink": {
                        "desired": desired,
                        "effective": effective,
                        "result": "ok",
                        "reason": "",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match=message):
        health._usb_effective_status(
            status,
            expected_desired=expected_desired,
        )


def test_source_intent_reader_matches_last_wins_quoted_env_format(
    tmp_path: Path,
) -> None:
    intent = tmp_path / "source_intent.env"
    intent.write_text(
        "# household source choices\n"
        f"{health.AIRPLAY_INTENT_KEY}=disabled\n"
        f"{health.SPOTIFY_INTENT_KEY}='disabled'\n"
        f"{health.BLUETOOTH_INTENT_KEY}=disabled\n"
        f"{health.USBSINK_INTENT_KEY}=enabled\n"
        "UNRELATED=malformed-but-not-ours\n"
        f"  {health.AIRPLAY_INTENT_KEY} = 'enabled'  \n",
        encoding="utf-8",
    )

    expectations, _ = health._source_expectations_with_fingerprint(intent)
    assert expectations == {
        "airplay": True,
        "spotify": False,
        "bluetooth": False,
        "usbsink": True,
    }


def test_source_intent_reader_enforces_byte_cap_and_strict_utf8(
    tmp_path: Path,
) -> None:
    intent = tmp_path / "source_intent.env"
    # The cap is bytes, not decoded characters. This payload contains fewer
    # than MAX characters but exceeds MAX UTF-8 bytes.
    intent.write_bytes(
        "é".encode("utf-8") * (health.MAX_SOURCE_INTENT_BYTES // 2 + 1)
    )
    with pytest.raises(RuntimeError, match="exceeds.*byte cap"):
        health._source_expectations_with_fingerprint(intent)

    intent.write_bytes(b"\xff")
    with pytest.raises(RuntimeError, match="cannot decode.*UTF-8"):
        health._source_expectations_with_fingerprint(intent)


def test_source_intent_reader_refuses_symlink_and_fifo_without_blocking(
    tmp_path: Path,
) -> None:
    target = tmp_path / "sensitive.env"
    target.write_text(f"{health.AIRPLAY_INTENT_KEY}=enabled\n", encoding="utf-8")
    intent = tmp_path / "source_intent.env"
    intent.symlink_to(target)
    with pytest.raises(RuntimeError, match="cannot read"):
        health._source_expectations_with_fingerprint(intent)
    assert target.read_text(encoding="utf-8").endswith("=enabled\n")

    intent.unlink()
    os.mkfifo(intent)
    with pytest.raises(RuntimeError, match="not a regular file"):
        health._source_expectations_with_fingerprint(intent)


@pytest.mark.parametrize(
    "key",
    [
        health.AIRPLAY_INTENT_KEY,
        health.SPOTIFY_INTENT_KEY,
        health.BLUETOOTH_INTENT_KEY,
        health.USBSINK_INTENT_KEY,
    ],
)
def test_source_intent_reader_rejects_malformed_recognized_values(
    tmp_path: Path,
    key: str,
) -> None:
    intent = tmp_path / "source_intent.env"
    intent.write_text(f"{key}=maybe\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="expected enabled or disabled"):
        health._source_expectations_with_fingerprint(intent)


def test_source_intent_reader_rejects_unknown_owned_key(tmp_path: Path) -> None:
    intent = tmp_path / "source_intent.env"
    intent.write_text(
        "JASPER_SOURCE_INTENT_FUTURE_SOURCE=enabled\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="unrecognized source intent key"):
        health._source_expectations_with_fingerprint(intent)


def test_usb_source_health_matches_card_and_direct_lane(tmp_path: Path) -> None:
    card = tmp_path / "UAC2Gadget"
    card.mkdir()
    healthy = _fanin_status(usb_direct=True, usb_present=True, usb_health="idle")

    assert health._usb_source_issues(True, healthy, card_path=card) == []
    assert health._usb_source_issues(False, _fanin_status(), card_path=tmp_path / "missing") == []


def test_usb_source_health_rejects_unconsumed_or_stale_advertisement(
    tmp_path: Path,
) -> None:
    card = tmp_path / "UAC2Gadget"
    card.mkdir()

    assert health._usb_source_issues(True, _fanin_status(), card_path=card) == [
        "fan-in direct USB lane is absent",
    ]
    issues = health._usb_source_issues(
        True,
        _fanin_status(usb_direct=True, usb_present=False, usb_health="broken"),
        card_path=card,
    )
    assert issues == [
        "fan-in direct USB device is not present",
        "fan-in direct USB health is 'broken'",
    ]
    assert health._usb_source_issues(
        False,
        _fanin_status(usb_direct=True),
        card_path=card,
    ) == [
        "UAC2 card is still advertised",
        "fan-in direct USB lane is still armed",
    ]


def _write_rfkill(
    root: Path,
    *,
    kind: str = "bluetooth",
    soft: str = "0",
    hard: str = "0",
    index: int = 0,
) -> None:
    entry = root / f"rfkill{index}"
    entry.mkdir()
    (entry / "type").write_text(kind, encoding="utf-8")
    (entry / "soft").write_text(soft, encoding="utf-8")
    (entry / "hard").write_text(hard, encoding="utf-8")


def test_bluetooth_radio_validation_covers_on_and_off_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_rfkill(tmp_path, soft="1", hard="0")
    monkeypatch.setattr(health, "RFKILL_CLASS_PATH", tmp_path)
    monkeypatch.setattr(health, "_bluez_powered", lambda: False)

    on_issues = health._bluetooth_radio_issues(True)
    assert "Bluetooth radio is soft blocked" in on_issues
    assert "BlueZ Powered is not yes" in on_issues

    monkeypatch.setattr(health, "_bluez_powered", lambda: True)
    off_issues = health._bluetooth_radio_issues(False)
    assert "BlueZ Powered is still yes" in off_issues

    (tmp_path / "rfkill0" / "soft").write_text("0", encoding="utf-8")
    monkeypatch.setattr(health, "_bluez_powered", lambda: False)
    off_issues = health._bluetooth_radio_issues(False)
    assert "Bluetooth radio is not soft blocked" in off_issues


def test_bluetooth_rfkill_fields_and_bluez_probe_are_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_rfkill(tmp_path, soft="0" * (health.MAX_RFKILL_FIELD_BYTES + 1))
    with pytest.raises(RuntimeError, match="exceeds.*bytes"):
        health._bluetooth_rfkill_state(tmp_path)

    seen: dict[str, object] = {}

    def timeout_run(argv, **kwargs):
        seen["argv"] = argv
        seen["timeout"] = kwargs["timeout"]
        raise health.subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(health.subprocess, "run", timeout_run)
    assert health._bluez_powered() is None
    assert seen == {"argv": ["bluetoothctl", "show"], "timeout": 3}


def test_bluetooth_off_requires_every_rfkill_radio_soft_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_rfkill(tmp_path, soft="1", index=0)
    _write_rfkill(tmp_path, soft="0", index=1)
    monkeypatch.setattr(health, "RFKILL_CLASS_PATH", tmp_path)
    monkeypatch.setattr(health, "_bluez_powered", lambda: False)

    assert "Bluetooth radio is not soft blocked" in (
        health._bluetooth_radio_issues(False)
    )
    assert "Bluetooth radio is soft blocked" in (
        health._bluetooth_radio_issues(True)
    )


def test_bonded_follower_requires_config_and_runtime_agreement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    grouping = tmp_path / "grouping.env"
    grouping.write_text(
        "JASPER_GROUPING=on\n"
        "JASPER_GROUPING_ROLE=follower\n"
        "JASPER_GROUPING_BOND_ID=pair-1\n"
        "JASPER_GROUPING_LEADER_ADDR=jts.local\n",
        encoding="utf-8",
    )
    states = {
        "jasper-snapclient.service": "active",
        "jasper-snapserver.service": "inactive",
    }
    monkeypatch.setattr(
        health, "_systemctl_is_active", lambda unit: states[unit],
    )

    assert health._bonded_follower_active(grouping) is True
    states["jasper-snapclient.service"] = "inactive"
    assert health._bonded_follower_active(grouping) is False


def test_grouping_reader_refuses_symlink_and_fifo_without_blocking(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.env"
    target.write_text(
        "JASPER_GROUPING=on\nJASPER_GROUPING_ROLE=follower\n",
        encoding="utf-8",
    )
    grouping = tmp_path / "grouping.env"
    grouping.symlink_to(target)
    assert health._read_bounded_env(
        grouping,
        health.MAX_GROUPING_ENV_BYTES,
    ) == {}

    grouping.unlink()
    os.mkfifo(grouping)
    assert health._read_bounded_env(
        grouping,
        health.MAX_GROUPING_ENV_BYTES,
    ) == {}


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


def test_saved_usb_on_with_hardware_unavailable_is_healthy(
    monkeypatch: pytest.MonkeyPatch,
    short_socket_dir: Path,
    stub_systemctl: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _run_main(
        monkeypatch,
        short_socket_dir,
        source_intent=f"{health.USBSINK_INTENT_KEY}=enabled\n",
        usb_effective="unavailable",
        usb_effective_reason="shared_otg_usb_output_requires_host",
        in_process_status=True,
    ) == 0

    output = capsys.readouterr().out
    assert "effective unavailable" in output
    assert "shared_otg_usb_output_requires_host" in output
    assert "deploy health passed" in output


def test_malformed_source_reconcile_status_fails_deploy_health(
    monkeypatch: pytest.MonkeyPatch,
    short_socket_dir: Path,
    stub_systemctl: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _run_main(
        monkeypatch,
        short_socket_dir,
        usb_status_text="{not-json",
        in_process_status=True,
    ) == 1

    assert "FAIL USB Audio Input status" in capsys.readouterr().out


def test_stale_usb_reconcile_desired_fails_deploy_health(
    monkeypatch: pytest.MonkeyPatch,
    short_socket_dir: Path,
    stub_systemctl: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_intent = f"{health.USBSINK_INTENT_KEY}=disabled\n"
    status = json.dumps(
        {
            "completed_monotonic_ns": 1,
            "intent_fingerprint": health.hashlib.sha256(
                source_intent.encode("utf-8")
            ).hexdigest(),
            "sources": {
                "usbsink": {
                    "desired": "enabled",
                    "effective": "unavailable",
                    "result": "ok",
                    "reason": "shared_otg_defaults_host_without_i2s",
                }
            }
        }
    )

    assert _run_main(
        monkeypatch,
        short_socket_dir,
        source_intent=source_intent,
        usb_status_text=status,
    ) == 1

    output = capsys.readouterr().out
    assert "FAIL USB Audio Input status" in output
    assert "status is stale" in output


def test_stale_same_desired_usb_reconcile_fingerprint_fails_deploy_health(
    monkeypatch: pytest.MonkeyPatch,
    short_socket_dir: Path,
    stub_systemctl: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    status = json.dumps(
        {
            "completed_monotonic_ns": 1,
            # This is a valid acknowledgement for the prior empty intent file.
            "intent_fingerprint": health.hashlib.sha256(b"").hexdigest(),
            "sources": {
                "usbsink": {
                    "desired": "disabled",
                    "effective": "off",
                    "result": "ok",
                    "reason": "",
                }
            },
        }
    )

    assert _run_main(
        monkeypatch,
        short_socket_dir,
        # USB remains disabled, but another persisted source changed.
        source_intent=f"{health.AIRPLAY_INTENT_KEY}=disabled\n",
        usb_status_text=status,
    ) == 1

    output = capsys.readouterr().out
    assert "FAIL USB Audio Input status" in output
    assert "status is stale: intent does not match" in output


def test_source_intent_change_during_health_probe_fails_final_stability_check(
    monkeypatch: pytest.MonkeyPatch,
    short_socket_dir: Path,
    stub_systemctl: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original = health._source_expectations_with_fingerprint
    calls = 0

    def changing_fingerprint(path=None):
        nonlocal calls
        expectations, fingerprint = original(path)
        calls += 1
        if calls == 3:
            return expectations, "f" * 64
        return expectations, fingerprint

    monkeypatch.setattr(
        health,
        "_source_expectations_with_fingerprint",
        changing_fingerprint,
    )

    assert _run_main(monkeypatch, short_socket_dir) == 1

    output = capsys.readouterr().out
    assert "FAIL source intent stability" in output
    assert "changed during health verification" in output


def test_invalid_source_intent_on_final_reread_fails_stability_check(
    monkeypatch: pytest.MonkeyPatch,
    short_socket_dir: Path,
    stub_systemctl: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original = health._source_expectations_with_fingerprint
    calls = 0

    def invalid_final_read(path=None):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("source intent became unreadable")
        return original(path)

    monkeypatch.setattr(
        health,
        "_source_expectations_with_fingerprint",
        invalid_final_read,
    )

    assert _run_main(monkeypatch, short_socket_dir) == 1

    output = capsys.readouterr().out
    assert "FAIL source intent stability" in output
    assert "source intent became unreadable" in output


def test_install_replay_invalidates_previous_source_acknowledgement() -> None:
    installer = (
        ROOT / "deploy" / "lib" / "install" / "systemd-units.sh"
    ).read_text(encoding="utf-8")
    function = installer.split("reapply_source_intent() {", maxsplit=1)[1].split(
        "\n}\n", maxsplit=1
    )[0]

    invalidation = "rm -f /run/jasper-source-intent/status.json"
    assert function.count(invalidation) == 2
    assert "--reason install --invalidate-status-before" in function
    assert "if ! /usr/bin/timeout --foreground --kill-after=5s 793s" in function
    first_unlink = function.index(invalidation)
    reconcile = function.index("jasper-source-intent-reconcile")
    second_unlink = function.index(invalidation, first_unlink + 1)
    assert first_unlink < reconcile < second_unlink


@pytest.mark.parametrize("profile", ["streambox", "endpoint", "satellite"])
def test_streambox_profiles_skip_parked_brain_and_input_units(
    monkeypatch: pytest.MonkeyPatch,
    short_socket_dir: Path,
    stub_systemctl: Path,
    capsys: pytest.CaptureFixture[str],
    profile: str,
) -> None:
    _set_inactive_units(
        monkeypatch,
        "jasper-input.service",
        "jasper-voice.service",
        "jasper-aec-bridge.service",
    )

    assert _run_main(monkeypatch, short_socket_dir, profile=profile) == 0

    output = capsys.readouterr().out
    queried = stub_systemctl.read_text(encoding="utf-8").splitlines()
    assert "profile=streambox" in output
    assert "jasper-input.service" not in queried
    assert "jasper-voice.service" not in queried
    assert "jasper-aec-bridge.service" not in queried
    assert "deploy health passed: 0 failure(s), 0 warning(s)" in output


def test_bonded_follower_expects_local_sources_and_mux_parked(
    monkeypatch: pytest.MonkeyPatch,
    short_socket_dir: Path,
    stub_systemctl: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    parked_units = {
        "jasper-snapserver.service",
        "jasper-mux.service",
        "shairport-sync.service",
        "nqptp.service",
        "librespot.service",
        "bluealsa.service",
        "bluealsa-aplay.service",
        "bt-agent.service",
    }
    _set_inactive_units(monkeypatch, *parked_units)
    grouping = (
        "JASPER_GROUPING=on\n"
        "JASPER_GROUPING_ROLE=follower\n"
        "JASPER_GROUPING_BOND_ID=pair-1\n"
        "JASPER_GROUPING_LEADER_ADDR=jts.local\n"
    )

    assert _run_main(monkeypatch, short_socket_dir, grouping=grouping) == 0

    output = capsys.readouterr().out
    queried = stub_systemctl.read_text(encoding="utf-8").splitlines()
    assert "parked (bonded follower)" in output
    assert "inactive (parked on bonded follower)" in output
    assert "Bluetooth radio" not in output
    # jasper-mux is absent from the core-required loop; its configured
    # inactivity is therefore healthy and it is never queried.
    assert "jasper-mux.service" not in queried
    assert "deploy health passed" in output


def test_main_fails_when_bluetooth_radio_disagrees_with_intent(
    monkeypatch: pytest.MonkeyPatch,
    short_socket_dir: Path,
    stub_systemctl: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        _run_main(
            monkeypatch,
            short_socket_dir,
            radio_issues=["Bluetooth radio is soft blocked"],
        )
        == 1
    )

    output = capsys.readouterr().out
    assert "FAIL Bluetooth radio" in output
    assert "Bluetooth radio is soft blocked" in output


@pytest.mark.parametrize("profile", ["full", "streambox"])
@pytest.mark.parametrize(
    ("source", "key", "units"),
    [
        (source, health.SOURCE_INTENT_KEYS[source], units)
        for source, units in health.SOURCE_REQUIRED_UNITS.items()
    ],
)
@pytest.mark.parametrize(
    ("intent", "expected_detail"),
    [
        ("enabled", "active"),
        ("disabled", "inactive (disabled by persisted source intent)"),
    ],
)
def test_main_honors_fixed_source_intents_for_both_profiles(
    monkeypatch: pytest.MonkeyPatch,
    short_socket_dir: Path,
    stub_systemctl: Path,
    capsys: pytest.CaptureFixture[str],
    profile: str,
    source: str,
    key: str,
    units: tuple[str, ...],
    intent: str,
    expected_detail: str,
) -> None:
    inactive: set[str] = set()
    if intent == "disabled":
        inactive.update(units)
    _set_inactive_units(monkeypatch, *inactive)
    text = f"{key}={intent}\n"

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
    for unit in units:
        assert unit in queried
    if source == "usbsink" and intent == "disabled":
        assert "inactive (effective off; saved intent preserved)" in output
    else:
        assert expected_detail in output
    if source == "bluetooth" and intent == "disabled":
        assert "bt-agent.service" in queried
        assert "0 warning(s)" in output


@pytest.mark.parametrize(
    ("key", "units"),
    [
        (health.AIRPLAY_INTENT_KEY, ("shairport-sync.service",)),
        (health.SPOTIFY_INTENT_KEY, ("librespot.service",)),
        (
            health.BLUETOOTH_INTENT_KEY,
            (
                "bluealsa.service",
                "bluealsa-aplay.service",
                "bt-agent.service",
            ),
        ),
        (health.USBSINK_INTENT_KEY, ("jasper-usbsink.service",)),
    ],
)
def test_main_fails_when_source_runs_despite_disabled_intent(
    monkeypatch: pytest.MonkeyPatch,
    short_socket_dir: Path,
    stub_systemctl: Path,
    capsys: pytest.CaptureFixture[str],
    key: str,
    units: tuple[str, ...],
) -> None:
    # Leave the first unit active while making any siblings inactive. One unit
    # drifting on is enough to fail the source contract.
    _set_inactive_units(monkeypatch, *units[1:])
    text = f"{key}=disabled\n"

    assert _run_main(
        monkeypatch,
        short_socket_dir,
        source_intent=text,
        usb_runtime_matches_intent=key != health.USBSINK_INTENT_KEY,
    ) == 1

    output = capsys.readouterr().out
    assert units[0] in stub_systemctl.read_text(encoding="utf-8").splitlines()
    assert f"FAIL {units[0]}" in output
    assert "active; expected inactive from persisted source intent" in output


@pytest.mark.parametrize(
    ("key", "inactive_unit"),
    [
        (health.AIRPLAY_INTENT_KEY, "shairport-sync.service"),
        (health.SPOTIFY_INTENT_KEY, "librespot.service"),
        (health.BLUETOOTH_INTENT_KEY, "bluealsa.service"),
        (health.USBSINK_INTENT_KEY, "jasper-usbsink.service"),
    ],
)
def test_main_fails_when_enabled_source_unit_is_inactive(
    monkeypatch: pytest.MonkeyPatch,
    short_socket_dir: Path,
    stub_systemctl: Path,
    capsys: pytest.CaptureFixture[str],
    key: str,
    inactive_unit: str,
) -> None:
    _set_inactive_units(monkeypatch, inactive_unit)

    assert (
        _run_main(
            monkeypatch,
            short_socket_dir,
            source_intent=f"{key}=enabled\n",
            usb_runtime_matches_intent=key != health.USBSINK_INTENT_KEY,
        )
        == 1
    )

    output = capsys.readouterr().out
    assert f"FAIL {inactive_unit}" in output
    assert "inactive; expected active from persisted source intent" in output


@pytest.mark.parametrize(
    "key",
    [
        health.AIRPLAY_INTENT_KEY,
        health.SPOTIFY_INTENT_KEY,
        health.BLUETOOTH_INTENT_KEY,
        health.USBSINK_INTENT_KEY,
    ],
)
def test_main_fails_closed_on_malformed_recognized_source_intent(
    monkeypatch: pytest.MonkeyPatch,
    short_socket_dir: Path,
    stub_systemctl: Path,
    capsys: pytest.CaptureFixture[str],
    key: str,
) -> None:
    text = f"{key}=maybe\n"

    assert _run_main(monkeypatch, short_socket_dir, source_intent=text) == 1

    output = capsys.readouterr().out
    queried = stub_systemctl.read_text(encoding="utf-8").splitlines()
    assert "FAIL source intent" in output
    assert "expected enabled or disabled" in output
    assert "disabled by persisted source intent" not in output
    assert not set(queried).intersection(
        unit
        for units in health.SOURCE_REQUIRED_UNITS.values()
        for unit in units
    )


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
    _set_inactive_units(monkeypatch, "jasper-control.service")

    assert _run_main(monkeypatch, short_socket_dir) == 1

    output = capsys.readouterr().out
    assert "FAIL jasper-control.service" in output
    assert "deploy health failed: 1 failure(s), 0 warning(s)" in output


def test_inactive_bluetooth_agent_fails_when_bluetooth_is_desired_on(
    monkeypatch: pytest.MonkeyPatch,
    short_socket_dir: Path,
    stub_systemctl: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_inactive_units(monkeypatch, "bt-agent.service")

    assert _run_main(monkeypatch, short_socket_dir) == 1

    output = capsys.readouterr().out
    assert "FAIL bt-agent.service" in output
    assert "deploy health failed: 1 failure(s), 0 warning(s)" in output


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


def test_outputd_stable_startup_starvation_counters_are_healthy(
    monkeypatch: pytest.MonkeyPatch,
    short_socket_dir: Path,
    stub_systemctl: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = _outputd_status(
        uptime_seconds=100.0,
        empty_periods=2,
        eagain_count=2,
    )
    later = _outputd_status(
        uptime_seconds=101.0,
        empty_periods=2,
        eagain_count=2,
    )

    assert _run_main(
        monkeypatch,
        short_socket_dir,
        outputd_payloads=[first, later],
    ) == 0

    output = capsys.readouterr().out
    assert "empty=2 eagain=2 empty_delta=0 eagain_delta=0" in output
    assert "deploy health passed" in output


@pytest.mark.parametrize(
    "first",
    [
        _outputd_status(uptime_seconds=100.0, backend="pipewire"),
        _outputd_status(uptime_seconds=100.0, content_xruns=1),
        _outputd_status(uptime_seconds=100.0, progress_age_ms=2001),
    ],
)
def test_outputd_first_bad_snapshot_cannot_be_erased_by_clean_restart(
    monkeypatch: pytest.MonkeyPatch,
    short_socket_dir: Path,
    stub_systemctl: Path,
    capsys: pytest.CaptureFixture[str],
    first: dict[str, Any],
) -> None:
    later = _outputd_status(uptime_seconds=101.0)

    assert _run_main(
        monkeypatch,
        short_socket_dir,
        outputd_payloads=[first, later],
    ) == 1

    assert "FAIL jasper-outputd STATUS" in capsys.readouterr().out


def test_outputd_uptime_reset_fails_even_when_both_snapshots_are_clean(
    monkeypatch: pytest.MonkeyPatch,
    short_socket_dir: Path,
    stub_systemctl: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _run_main(
        monkeypatch,
        short_socket_dir,
        outputd_payloads=[
            _outputd_status(uptime_seconds=100.0),
            _outputd_status(uptime_seconds=1.0),
        ],
    ) == 1

    output = capsys.readouterr().out
    assert "FAIL jasper-outputd STATUS" in output
    assert "uptime_seconds=100->1" in output


@pytest.mark.parametrize(
    ("first", "later", "detail"),
    [
        (
            _outputd_status(empty_periods=2, eagain_count=2),
            _outputd_status(empty_periods=3, eagain_count=2),
            "empty_delta=1",
        ),
        (
            _outputd_status(empty_periods=2, eagain_count=2),
            _outputd_status(empty_periods=2, eagain_count=3),
            "eagain_delta=1",
        ),
    ],
)
def test_outputd_growing_starvation_counters_fail(
    monkeypatch: pytest.MonkeyPatch,
    short_socket_dir: Path,
    stub_systemctl: Path,
    capsys: pytest.CaptureFixture[str],
    first: dict[str, Any],
    later: dict[str, Any],
    detail: str,
) -> None:
    assert _run_main(
        monkeypatch,
        short_socket_dir,
        outputd_payloads=[first, later],
    ) == 1

    output = capsys.readouterr().out
    assert "FAIL jasper-outputd STATUS" in output
    assert detail in output


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
    "value",
    [None, True, False, "1", -1, float("nan"), float("inf")],
)
def test_outputd_uptime_requires_finite_nonnegative_number(value: Any) -> None:
    with pytest.raises(RuntimeError, match="finite nonnegative number"):
        health._uptime_seconds({"uptime_seconds": value})

    assert health._uptime_seconds({"uptime_seconds": 0}) == 0.0
    assert health._uptime_seconds({"uptime_seconds": 1.25}) == 1.25


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
