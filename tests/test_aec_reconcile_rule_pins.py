# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pin that deploy/bin/jasper-aec-reconcile's bash re-implementations agree
with the Python rules they exist to mirror when the interpreter is
unavailable (see the script's own ADR-0101 comments on normalize_bool,
normalize_output_dac_id and carry_chip_aec_dac_gate).

Each bash function is sourced verbatim out of the real script (never
copy-pasted) via a tiny extraction regex, so a future edit to the script is
what these tests exercise — not a frozen duplicate of today's text.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from jasper.audio_profile_state import parse_env_bool
from jasper.chip_aec.policy import normalize_dac_id, permits_selection

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "bin" / "jasper-aec-reconcile"
_SOURCE = SCRIPT.read_text(encoding="utf-8")


def _shell_function_body(source: str, name: str) -> str:
    match = re.search(
        rf"^{re.escape(name)}\(\)\s*\{{\n(.*?)^\}}$",
        source,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match is not None, f"could not locate shell function {name}"
    return match.group(1)


def _shell_global(source: str, name: str) -> str:
    match = re.search(rf"^{re.escape(name)}=.*$", source, flags=re.MULTILINE)
    assert match is not None, f"could not locate shell global {name}"
    return match.group(0)


def _run_bash(snippet: str, *args: str) -> subprocess.CompletedProcess[str]:
    # Positional args ride argv, never string interpolation, so a value
    # containing quotes or spaces can never break out of the snippet.
    result = subprocess.run(
        ["bash", "-c", snippet, "bash", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode in (0, 1), result.stderr
    return result


def _bash_normalize_bool(value: str) -> bool:
    """Invoke the real script's normalize_bool() in isolation."""
    body = _shell_function_body(_SOURCE, "normalize_bool")
    snippet = (
        "set -euo pipefail\n"
        "log() { :; }\n"
        f"normalize_bool() {{\n{body}}}\n"
        'normalize_bool "$1"\n'
    )
    result = _run_bash(snippet, value)
    assert result.returncode == 0, result.stderr
    # Trim only the trailing newline `echo` adds — the value under test may
    # itself carry leading/trailing whitespace that must NOT be discarded.
    return result.stdout.rstrip("\n") == "1"


def _bash_normalize_output_dac_id(value: str) -> str:
    """Invoke the real script's normalize_output_dac_id() in isolation."""
    body = _shell_function_body(_SOURCE, "normalize_output_dac_id")
    snippet = (
        "set -euo pipefail\n"
        "log() { :; }\n"
        f"normalize_output_dac_id() {{\n{body}}}\n"
        'normalize_output_dac_id "$1"\n'
    )
    result = _run_bash(snippet, value)
    assert result.returncode == 0, result.stderr
    return result.stdout.rstrip("\n")


def _bash_carry_chip_aec_dac_gate(
    dac_id_arg: str,
    testing_arg: str,
    *,
    env_dac_id: str,
    env_status: str,
) -> tuple[int, str]:
    """Invoke the real script's carry_chip_aec_dac_gate() in isolation.

    Returns (exit_code, CHIP_AEC_DAC_GATE_OK) after seeding the same globals
    the real script initializes before ever calling it.
    """
    note_line = _shell_global(_SOURCE, "CHIP_AEC_GATE_CARRY_NOTE")
    normalize_body = _shell_function_body(_SOURCE, "normalize_output_dac_id")
    carry_body = _shell_function_body(_SOURCE, "carry_chip_aec_dac_gate")
    snippet = "\n".join(
        [
            "set -euo pipefail",
            "log() { :; }",
            note_line,
            "CHIP_AEC_DAC_GATE_OK=0",
            f"normalize_output_dac_id() {{\n{normalize_body}}}",
            f"carry_chip_aec_dac_gate() {{\n{carry_body}}}",
            "rc=0",
            'carry_chip_aec_dac_gate "$1" "$2" || rc=$?',
            'printf "rc=%s\\nok=%s\\n" "$rc" "$CHIP_AEC_DAC_GATE_OK"',
            "",
        ]
    )
    env = {
        "PATH": "/usr/bin:/bin",
        "JASPER_AEC_CHIP_AEC_DAC_ID": env_dac_id,
        "JASPER_AEC_CHIP_AEC_DAC_STATUS": env_status,
    }
    result = subprocess.run(
        ["bash", "-c", snippet, "bash", dac_id_arg, testing_arg],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    fields = dict(
        line.split("=", 1) for line in result.stdout.strip().splitlines()
    )
    return int(fields["rc"]), fields["ok"]


# The wizard only ever writes "1"/"0" (see normalize_bool's own comment), but
# an operator hand-editing aec_mode.env may use any of these. normalize_bool()
# trims surrounding whitespace before matching, same as parse_env_bool()'s
# leading .strip(), so a padded value normalizes the same in both.
_BOOL_VECTORS = [
    "yes",
    "no",
    "on",
    "off",
    "true",
    "false",
    "1",
    "0",
    "enabled",
    "disabled",
    "",
    "garbage",
    " YES ",
]


@pytest.mark.parametrize("value", _BOOL_VECTORS)
def test_normalize_bool_matches_parse_env_bool(value: str) -> None:
    assert _bash_normalize_bool(value) == parse_env_bool(value, default=False)


# Mixed case, quoting, dashes, empty and whitespace padding all normalize
# the same way in both languages: normalize_output_dac_id() trims
# surrounding whitespace before matching, same as normalize_dac_id()'s
# leading .strip().
_DAC_ID_VECTORS = [
    "HiFiBerry-DAC8x",
    "'hifiberry_dac8x'",
    '"HiFiBerry-DAC8X"',
    "",
    "-",
    "UNKNOWN",
    "Hi--Fi_Berry",
    "  hifiberry_dac8x  ",
]


@pytest.mark.parametrize("value", _DAC_ID_VECTORS)
def test_normalize_output_dac_id_matches_normalize_dac_id(value: str) -> None:
    assert _bash_normalize_output_dac_id(value) == normalize_dac_id(value)


# jasper.chip_aec.policy.gate_from_runtime_env's own docstring names this
# exact mapping ("the mapping the reconciler's carry_chip_aec_dac_gate
# applies to this same record"): auto_allowed is status == "approved".
_GATE_STATUSES = ["approved", "needs_calibration", "testing", "garbage"]


@pytest.mark.parametrize("status", _GATE_STATUSES)
@pytest.mark.parametrize("testing", ["0", "1"])
def test_carry_chip_aec_dac_gate_matches_permits_selection(
    status: str, testing: str
) -> None:
    dac_id = "hifiberry_dac8x"  # already normalized: isolate the gate rule
    # from normalize_output_dac_id's own identity-match, pinned separately.
    rc, ok = _bash_carry_chip_aec_dac_gate(
        dac_id, testing, env_dac_id=dac_id, env_status=status
    )

    assert rc == 0
    assert (ok == "1") == permits_selection(
        auto_allowed=status == "approved", testing_requested=testing == "1"
    )


def test_carry_chip_aec_dac_gate_withholds_without_a_status() -> None:
    """A missing status is "no record to carry", never a false verdict.

    carry_chip_aec_dac_gate()'s presence guard (`[[ -n ... ]] || return 1`)
    sits in front of the approved||testing rule pinned above; an empty
    status short-circuits before that rule ever runs, so this is a distinct
    code path from a status the rule would evaluate as non-approved.
    """
    dac_id = "hifiberry_dac8x"

    rc, ok = _bash_carry_chip_aec_dac_gate(
        dac_id, "1", env_dac_id=dac_id, env_status=""
    )

    assert rc == 1
    assert ok == "0"  # untouched: still the function's own zero-init
