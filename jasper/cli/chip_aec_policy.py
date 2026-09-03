# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""CLI shim for chip-AEC policy decisions used by shell reconcilers.

`--alignment` answers the second question `deploy/bin/jasper-aec-reconcile`
asks: what the alignment record says for a disposition only the shell can see.
`jasper-aec-init` publishes its own pass's record directly; every other
disposition comes through here, so no alignment string is typed in shell.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from ..chip_aec.health import DISPOSITIONS, alignment_health, render_shell_assignments
from ..chip_aec.policy import resolve_chip_aec_dac_gate
from ..route_latency.status_socket import DEFAULT_STATUS_TIMEOUT_SECONDS, read_status_socket


def _query_outputd_status(
    path: str, *, timeout: float = DEFAULT_STATUS_TIMEOUT_SECONDS
) -> tuple[dict[str, Any] | None, str]:
    if not path:
        return None, "STATUS socket path is empty"
    try:
        return read_status_socket(path, timeout=timeout), ""
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return None, str(exc)


def _shell_assignments(gate, *, testing_requested: bool) -> str:
    # The shell consumes one boolean per query: may THIS selection be armed?
    permitted = gate.permits(testing_requested=testing_requested)
    values = {
        "JASPER_CHIP_AEC_DAC_GATE_DAC": gate.dac_id,
        "JASPER_CHIP_AEC_DAC_GATE_STATUS": gate.status,
        "JASPER_CHIP_AEC_DAC_GATE_PERMITTED": "1" if permitted else "0",
        "JASPER_CHIP_AEC_DAC_GATE_SOURCE": gate.source,
        "JASPER_CHIP_AEC_DAC_GATE_DETAIL": gate.detail,
    }
    return render_shell_assignments(values)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    # The two questions this shim answers, one per call. Both callers are
    # `|| true` shell, so an unanswerable call must fail at the parser where CI
    # sees it, not print an empty record the reconciler then treats as a
    # missing interpreter.
    question = parser.add_mutually_exclusive_group(required=True)
    question.add_argument("--dac-id")
    question.add_argument("--alignment", choices=DISPOSITIONS)
    parser.add_argument("--outputd-socket", default="")
    parser.add_argument("--testing-requested", action="store_true")
    parser.add_argument("--shell-env", action="store_true")
    parser.add_argument("--selection", default="")
    parser.add_argument("--reason", default="")
    parser.add_argument("--action", default="")
    args = parser.parse_args(argv)

    if args.alignment:
        health = alignment_health(
            args.alignment,
            selection=args.selection,
            reason=args.reason,
            action=args.action,
        )
        print(health.to_shell(), end="")
        return 0

    outputd_status = None
    outputd_error = ""
    if args.outputd_socket:
        outputd_status, outputd_error = _query_outputd_status(args.outputd_socket)
    gate = resolve_chip_aec_dac_gate(
        args.dac_id,
        testing_requested=args.testing_requested,
        outputd_status=outputd_status,
        outputd_error=outputd_error,
    )
    if args.shell_env:
        print(_shell_assignments(gate, testing_requested=args.testing_requested), end="")
    else:
        print(json.dumps(gate.to_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
