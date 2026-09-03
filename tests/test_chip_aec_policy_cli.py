# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Direct contracts for the shell-facing chip-AEC policy shim.

The socket wire protocol itself (missing socket, timeout, malformed JSON,
non-object reply) is `jasper.route_latency.status_socket.read_status_socket`'s
contract, pinned in `tests/test_route_latency_status_socket.py`; this shim
only wraps that reader, so its own test covers the empty-path short-circuit
and that a read failure is turned into a `(None, str)` pair rather than
raising.
"""

from __future__ import annotations

import json

import pytest

from jasper.chip_aec_policy import ChipAecGate
from jasper.cli import chip_aec_policy


def test_query_outputd_status_empty_path_short_circuits():
    assert chip_aec_policy._query_outputd_status("") == (
        None, "STATUS socket path is empty"
    )


def test_query_outputd_status_returns_none_and_error_on_read_failure(monkeypatch):
    def _raise(*_args, **_kwargs):
        raise OSError("no such socket")

    monkeypatch.setattr(chip_aec_policy, "read_status_socket", _raise)

    payload, error = chip_aec_policy._query_outputd_status("/run/outputd.sock")

    assert payload is None
    assert "no such socket" in error


def test_shell_assignments_quote_values():
    gate = ChipAecGate(
        dac_id="a dac",
        status="approved",
        source="registry",
        detail="operator's choice",
        auto_allowed=True,
    )

    output = chip_aec_policy._shell_assignments(gate, testing_requested=False)

    assert "JASPER_CHIP_AEC_DAC_GATE_DAC='a dac'" in output
    assert "JASPER_CHIP_AEC_DAC_GATE_DETAIL='operator'\"'\"'s choice'" in output
    assert "JASPER_CHIP_AEC_DAC_GATE_PERMITTED=1" in output


@pytest.mark.parametrize(
    ("testing_requested", "expected"),
    [(False, "0"), (True, "1")],
)
def test_shell_permitted_answers_the_requested_selection(
    testing_requested: bool,
    expected: str,
) -> None:
    """The shell gets one boolean: may THIS selection arm chip-AEC?

    ADR-0101 leaves an uncodified DAC armable, so the answer must follow the
    selection or the automatic profile would silently arm uncommissioned
    hardware.
    """
    gate = ChipAecGate(
        dac_id="mystery_usb_audio",
        status="needs_calibration",
        source="static",
        detail="no codified timing",
        auto_allowed=False,
    )

    output = chip_aec_policy._shell_assignments(
        gate, testing_requested=testing_requested
    )

    assert f"JASPER_CHIP_AEC_DAC_GATE_PERMITTED={expected}" in output


def test_main_forwards_status_and_emits_shell_or_json(monkeypatch, capsys):
    seen = []
    gate = ChipAecGate(
        dac_id="apple_usb_c_dongle",
        status="approved",
        source="registry",
        detail="ok",
        auto_allowed=True,
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
    assert json.loads(capsys.readouterr().out) == gate.to_dict()
    assert seen[0][1]["outputd_status"] == {"reference_outputs": {}}

    assert chip_aec_policy.main([
        "--dac-id", "apple_usb_c_dongle", "--shell-env",
    ]) == 0
    assert "JASPER_CHIP_AEC_DAC_GATE_STATUS=approved" in capsys.readouterr().out
