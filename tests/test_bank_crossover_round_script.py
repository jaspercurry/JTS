# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Laptop banking through fake SSH, retaining valid captures and matching state."""

from __future__ import annotations

import os
import json
import sys
import tarfile
import pytest
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "bank-crossover-round.sh"


def _run(
    *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    # The script targets a speaker, so _lib.sh requires one to be named
    # (#3498) — nothing here reaches the network, so an unroutable name
    # keeps the refusal under test the dest-dir one.
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=30,
        env=env if env is not None else {**os.environ, "PI_HOST": "jts9.invalid"},
    )


def test_syntax_is_valid():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True, timeout=10)


def test_refuses_a_non_empty_dest_dir_with_the_literal_exit_code_four(tmp_path):
    dest = tmp_path / "already-banked"
    dest.mkdir()
    (dest / "state.json").write_text("{}")

    proc = _run(str(dest))

    # Decoupled from any symbol on purpose -- see module docstring. `4`,
    # not `!= 0`, is the actual promise.
    assert proc.returncode == 4
    assert "already exists and is not empty" in proc.stderr

    # A refused run must not touch what was already in the directory.
    assert (dest / "state.json").read_text() == "{}"


def test_sourcing_the_shared_lib_survives_a_pre_set_pi_host(tmp_path):
    """scripts/_lib.sh is sourced INTO this script's own namespace.

    This script runs under `set -u`, so any name the library leaves unset
    out from under it is an unbound-variable exit 1 -- which callers used to
    read as the capture-integrity checker's benign "nothing to check"
    verdict, banking nothing and calling it success. That overload is gone
    (`run-crossover-round.py` now aborts on any non-zero bank rc), but the
    silent-source failure this pin catches is not: reaching the exit-4
    refusal above proves the source completed.
    """
    dest = tmp_path / "already-banked"
    dest.mkdir()
    (dest / "state.json").write_text("{}")

    proc = _run(str(dest), env={**os.environ, "PI_HOST": "jts9.local"})

    assert proc.returncode == 4


def test_non_empty_dest_dir_refusal_is_distinct_from_a_missing_argument():
    # Bash's own `${1:?…}` exit code for a missing required argument is 1
    # -- unchanged, and deliberately not moved onto 4 alongside the
    # non-empty-dest-dir refusal (a one-time invocation mistake is not the
    # retry-loop collision exit 4 exists to resolve). The two refusals
    # must stay numerically distinct from each other.
    missing_arg = _run()
    assert missing_arg.returncode == 1


@pytest.mark.parametrize("snapshot", [True, False])
def test_named_bundle_keeps_its_state_after_a_later_round(tmp_path, snapshot):
    from jasper.active_speaker.crossover_v2.round_inputs import CAPTURE_STATE_FILENAME
    from jasper.active_speaker.crossover_v2.round_views import load_banked_round
    from tests.crossover_v2_banked_round import bank_measure_round

    source = bank_measure_round(tmp_path / "source")
    bundle = next((source / "bundle").iterdir())
    if snapshot:
        (bundle / CAPTURE_STATE_FILENAME).write_text((source / "state.json").read_text())
    archive = tmp_path / "bundle.tar"
    with tarfile.open(archive, "w") as writer:
        writer.add(bundle, arcname=bundle.name)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ssh = bin_dir / "ssh"
    ssh.write_text(f"""#!/bin/sh
case "$*" in
  *'tar -C'*) cat '{archive}' ;;
  *'active_speaker_crossover_v2_state.json'*) echo '{{"session_id":"capture-B","verify":{{"outcome":"pass"}}}}' ;;
  *) exit 0 ;;
esac
""")
    ssh.chmod(0o755)
    destination = tmp_path / "banked"
    proc = _run(str(destination), bundle.name, env={**os.environ,
        "PI_HOST": "jts9.invalid", "PYTHON": sys.executable,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
    })
    assert proc.returncode == 0, proc.stderr
    packet = load_banked_round(destination).packet
    assert packet["session"]["capture_session_id"] == "capture-1"
    assert packet["entry_baseline"]["available"] is True
    assert packet["verify"]["available"] is False
    assert (destination / "state.json").is_file() is snapshot
    provenance = json.loads((destination / "provenance.json").read_text())
    assert provenance["missing"] == ([] if snapshot else ["state.json"])
