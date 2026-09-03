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
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from jasper.chip_aec import health as chip_aec_health
from jasper.chip_aec.policy import ChipAecGate
from jasper.cli import chip_aec_policy

ROOT = Path(__file__).resolve().parents[1]
# Everything an operator-supplied park reason can carry that shell would
# otherwise act on: an apostrophe, a command substitution, a backquote, a
# double quote, and the line break that would split the assignment in two.
HOSTILE_REASON = "it's a $(rm -rf /) `whoami` \"6-ch\" mic\nsecond line"


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


def _fold_dac_id(value: str) -> str:
    # normalize_dac_id's transform: lowercased, hyphens folded to underscores.
    return value.lower().replace("-", "_")


_XVF_UNUSABLE = ["--alignment", chip_aec_health.XVF_UNUSABLE, "--reason", HOSTILE_REASON]
_DAC_UNCALIBRATED = [
    "--alignment", chip_aec_health.DAC_UNCALIBRATED, "--reason", HOSTILE_REASON,
]
_DAC_GATE_KEY = "JASPER_CHIP_AEC_DAC_GATE_DAC"


@pytest.mark.parametrize(
    ("cli_args", "key", "transform"),
    [
        (_XVF_UNUSABLE, chip_aec_health.REASON_KEY, str),
        (_DAC_UNCALIBRATED, chip_aec_health.REASON_KEY, str),
        (["--dac-id", HOSTILE_REASON, "--shell-env"], _DAC_GATE_KEY, _fold_dac_id),
    ],
    ids=["xvf_unusable", "dac_uncalibrated", "dac_gate_shell_env"],
)
def test_shell_measured_free_text_survives_the_reconcilers_eval(
    cli_args: list[str], key: str, transform,
) -> None:
    """Both shell-facing emitters share one quoting helper, driven the way
    `deploy/bin/jasper-aec-reconcile` drives them: this shim's stdout is
    eval'd by bash.  Free text reaches the record as one collapsed line and
    nothing in it is executed.
    """
    emit = shlex.join(
        [sys.executable, "-m", "jasper.cli.chip_aec_policy", *cli_args]
    )
    result = subprocess.run(
        ["bash", "-c", f'eval "$({emit})"; printf %s "${key}"'],
        capture_output=True,
        text=True,
        check=True,
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )

    assert result.stdout == transform(" ".join(HOSTILE_REASON.split()))


def test_an_unknown_alignment_token_fails_at_the_parser() -> None:
    """The shell call sites are `|| true`, so a typo that only raised inside
    `alignment_health` would print nothing and silently leave the last record
    standing.  argparse's closed vocabulary makes it a CI-visible exit 2.
    """
    with pytest.raises(SystemExit) as exit_info:
        chip_aec_policy.main(["--alignment", "half_applied"])

    assert exit_info.value.code == 2
