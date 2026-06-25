# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""CLI shim for chip-AEC policy decisions used by shell reconcilers."""
from __future__ import annotations

import argparse
import json
import shlex
import socket
import sys
from typing import Any

from ..chip_aec_policy import resolve_chip_aec_dac_gate


def _query_outputd_status(path: str, *, timeout: float = 1.0) -> tuple[dict[str, Any] | None, str]:
    if not path:
        return None, "STATUS socket path is empty"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(path)
            sock.sendall(b"STATUS\n")
            chunks: list[bytes] = []
            total = 0
            while total < 65536:
                chunk = sock.recv(8192)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
    except OSError as exc:
        return None, f"STATUS socket {path}: {exc}"
    try:
        payload = json.loads(b"".join(chunks).decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        return None, f"invalid STATUS JSON: {exc}"
    if not isinstance(payload, dict):
        return None, "STATUS payload is not an object"
    return payload, ""


def _shell_assignments(gate) -> str:
    values = {
        "JASPER_CHIP_AEC_DAC_GATE_DAC": gate.dac_id,
        "JASPER_CHIP_AEC_DAC_GATE_STATUS": gate.status,
        "JASPER_CHIP_AEC_DAC_GATE_PERMITTED": "1" if gate.permitted else "0",
        "JASPER_CHIP_AEC_DAC_GATE_AUTO_ALLOWED": "1" if gate.auto_allowed else "0",
        "JASPER_CHIP_AEC_DAC_GATE_PRODUCTION_ALLOWED": (
            "1" if gate.production_allowed else "0"
        ),
        "JASPER_CHIP_AEC_DAC_GATE_TESTING_ALLOWED": (
            "1" if gate.testing_allowed else "0"
        ),
        "JASPER_CHIP_AEC_DAC_GATE_SOURCE": gate.source,
        "JASPER_CHIP_AEC_DAC_GATE_DETAIL": gate.detail,
        "JASPER_CHIP_AEC_DAC_GATE_ACTION": gate.recommended_action,
    }
    return "\n".join(f"{key}={shlex.quote(value)}" for key, value in values.items())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dac-id", required=True)
    parser.add_argument("--outputd-socket", default="")
    parser.add_argument("--testing-requested", action="store_true")
    parser.add_argument("--shell-env", action="store_true")
    args = parser.parse_args(argv)

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
        print(_shell_assignments(gate))
    else:
        print(json.dumps(gate.to_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
