# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Direct contracts for the shell-facing chip-AEC policy shim."""

from __future__ import annotations

import json
from types import SimpleNamespace

from jasper.cli import chip_aec_policy


class _Socket:
    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.sent = []
        self.timeout = None
        self.path = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def settimeout(self, timeout):
        self.timeout = timeout

    def connect(self, path):
        self.path = path

    def sendall(self, payload):
        self.sent.append(payload)

    def recv(self, _size):
        return next(self.chunks, b"")


def test_query_outputd_status_owns_socket_protocol(monkeypatch):
    sock = _Socket([b'{"reference_', b'outputs": {}}', b""])
    monkeypatch.setattr(
        chip_aec_policy.socket,
        "socket",
        lambda *_args: sock,
    )

    payload, error = chip_aec_policy._query_outputd_status(
        "/run/outputd.sock", timeout=0.25
    )

    assert payload == {"reference_outputs": {}}
    assert error == ""
    assert sock.timeout == 0.25
    assert sock.path == "/run/outputd.sock"
    assert sock.sent == [b"STATUS\n"]


def test_query_outputd_status_classifies_empty_invalid_and_non_object(monkeypatch):
    assert chip_aec_policy._query_outputd_status("") == (
        None, "STATUS socket path is empty"
    )
    for body, expected in ((b"{", "invalid STATUS JSON"), (b"[]", "not an object")):
        monkeypatch.setattr(
            chip_aec_policy.socket,
            "socket",
            lambda *_args, body=body: _Socket([body, b""]),
        )
        payload, error = chip_aec_policy._query_outputd_status("/run/test.sock")
        assert payload is None
        assert expected in error


def test_shell_assignments_quote_values():
    gate = SimpleNamespace(
        dac_id="a dac",
        status="approved",
        permitted=True,
        auto_allowed=False,
        production_allowed=True,
        testing_allowed=False,
        source="registry",
        detail="operator's choice",
        recommended_action="keep current",
    )

    output = chip_aec_policy._shell_assignments(gate)

    assert "JASPER_CHIP_AEC_DAC_GATE_DAC='a dac'" in output
    assert "JASPER_CHIP_AEC_DAC_GATE_DETAIL='operator'\"'\"'s choice'" in output
    assert "JASPER_CHIP_AEC_DAC_GATE_PERMITTED=1" in output


def test_main_forwards_status_and_emits_shell_or_json(monkeypatch, capsys):
    seen = []
    gate = SimpleNamespace(
        dac_id="apple_usb_c_dongle",
        status="approved",
        permitted=True,
        auto_allowed=True,
        production_allowed=True,
        testing_allowed=True,
        source="registry",
        detail="ok",
        recommended_action="none",
        to_dict=lambda: {"status": "approved"},
    )
    monkeypatch.setattr(
        chip_aec_policy,
        "_query_outputd_status",
        lambda _path: ({"reference_outputs": {}}, ""),
    )
    monkeypatch.setattr(
        chip_aec_policy,
        "resolve_chip_aec_dac_gate",
        lambda dac_id, **kwargs: seen.append((dac_id, kwargs)) or gate,
    )

    assert chip_aec_policy.main([
        "--dac-id", "apple_usb_c_dongle", "--outputd-socket", "/run/o.sock",
    ]) == 0
    assert json.loads(capsys.readouterr().out) == {"status": "approved"}
    assert seen[0][1]["outputd_status"] == {"reference_outputs": {}}

    assert chip_aec_policy.main([
        "--dac-id", "apple_usb_c_dongle", "--shell-env",
    ]) == 0
    assert "JASPER_CHIP_AEC_DAC_GATE_STATUS=approved" in capsys.readouterr().out
