# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for deploy/bin/jasper-camilla-crossover-guard.

The ExecStartPre chain-breaker for camilla#2 (the endpoint-crossover
instance, :1235). It mirrors jasper-camilla-pipe-guard's shape but:
  - reads its OWN statefile var (JASPER_CAMILLA2_STATEFILE) so the two
    guards can never touch each other's statefile, and
  - delegates repair to the active-speaker runtime contract, whose
    contract is that camilla#2's roleful topology NEVER selects the flat
    fallback — it returns the driver-domain (Layer-A-intact) baseline or
    leaves the statefile untouched. ("never flat" is proven at the
    contract level in tests/test_active_speaker_runtime_contract.py; here
    we prove the guard delegates and is fail-open.)

Pure-bash policy script, tested via subprocess.run with env-overridden
paths — the jasper-camilla-pipe-guard harness pattern. Every test asserts
exit 0 (FAIL-OPEN: never block the start path) plus the structured
`event=camilla_crossover_guard.<outcome>` line and the statefile content.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "bin" / "jasper-camilla-crossover-guard"

SNAPFIFO_NAME = "snapfifo"


def _runtime_safe_graph_script(tmp_path: Path) -> Path:
    """A stand-in for `jasper-active-speaker runtime-safe-graph`: rewrites
    the statefile's config_path to --flat-config and reports success, unless
    JASPER_FAKE_RUNTIME_BLOCK=1 (blocked → leave statefile untouched)."""
    script = tmp_path / "runtime-safe-graph"
    script.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
statefile=""
flat=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --statefile) statefile="$2"; shift 2 ;;
        --flat-config) flat="$2"; shift 2 ;;
        runtime-safe-graph|--write-statefile|--json) shift ;;
        *) shift ;;
    esac
done
if [[ "${JASPER_FAKE_RUNTIME_BLOCK:-0}" == "1" || ! -r "$flat" ]]; then
    printf '{"ok":false,"status":"blocked"}\\n'
    exit 1
fi
tmp="${statefile}.fake.$$"
if [[ -f "$statefile" ]]; then
    sed "s|^\\([[:space:]]*config_path:\\).*|\\1 ${flat}|" "$statefile" > "$tmp"
else
    printf 'config_path: %s\\nvolume:\\n- 0.0\\n' "$flat" > "$tmp"
fi
mv "$tmp" "$statefile"
printf '{"ok":true,"status":"select_active_baseline"}\\n'
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _run(
    tmp_path: Path,
    *,
    statefile=None,
    base=None,
    fifo=None,
    timeout="0.3",
    runtime_helper=True,
    runtime_block=False,
):
    env = dict(os.environ)
    # The DISTINCT statefile var — this is the camilla#2 guard.
    env["JASPER_CAMILLA2_STATEFILE"] = str(statefile or tmp_path / "statefile.yml")
    env["JASPER_CAMILLA_BASE_CONFIG"] = str(base or tmp_path / "base.yml")
    env["JASPER_GROUPING_SNAPFIFO"] = str(fifo or tmp_path / SNAPFIFO_NAME)
    env["JASPER_PIPE_GUARD_PROBE_TIMEOUT"] = timeout
    if runtime_helper:
        env["JASPER_RUNTIME_SAFE_GRAPH"] = str(_runtime_safe_graph_script(tmp_path))
    else:
        env["JASPER_RUNTIME_SAFE_GRAPH"] = str(tmp_path / "missing-runtime-helper")
    if runtime_block:
        env["JASPER_FAKE_RUNTIME_BLOCK"] = "1"
    return subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True, text=True, env=env, timeout=20,
    )


def _write_statefile(tmp_path: Path, config_path: Path) -> Path:
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f'config_path: "{config_path}"\nvolume: -20.0\n')
    return statefile


def _pipe_config(tmp_path: Path, fifo: Path) -> Path:
    cfg = tmp_path / "grouping_leader.yml"
    cfg.write_text(
        "devices:\n  playback:\n    type: File\n    channels: 2\n"
        f'    filename: "{fifo}"\n    format: S16_LE\n'
    )
    return cfg


def _driver_domain_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "active_speaker_runtime.yml"
    cfg.write_text(
        "devices:\n  playback:\n    type: Alsa\n    channels: 2\n"
        '    device: "outputd_content_playback"\n    format: S16_LE\n'
    )
    return cfg


def test_uses_distinct_camilla2_statefile_var(tmp_path):
    """The camilla#2 guard reads JASPER_CAMILLA2_STATEFILE, NOT the
    camilla#1 guard's JASPER_CAMILLA_STATEFILE. Set ONLY the camilla#2 var
    and confirm the guard acted on that file."""
    cfg = _driver_domain_config(tmp_path)
    statefile = _write_statefile(tmp_path, cfg)
    before = statefile.read_text()
    # Pass via the camilla#2 var only (the _run helper sets CAMILLA2).
    r = _run(tmp_path, statefile=statefile)
    assert r.returncode == 0
    # A non-pipe (driver-domain) statefile is a no-op for the guard.
    assert "event=camilla_crossover_guard.ok reason=solo_config" in r.stderr
    assert statefile.read_text() == before


def test_dead_pipe_repairs_via_runtime_contract(tmp_path):
    """Bonded-pipe statefile + absent FIFO would clean-exit-loop camilla#2.
    The guard repairs first, delegating to the runtime contract (which on a
    roleful topology returns the driver-domain baseline, never flat)."""
    fifo = tmp_path / SNAPFIFO_NAME  # never created
    cfg = _pipe_config(tmp_path, fifo)
    base = tmp_path / "base.yml"
    base.write_text("devices: {}\n")
    statefile = _write_statefile(tmp_path, cfg)
    r = _run(tmp_path, statefile=statefile, base=base, fifo=fifo)
    assert r.returncode == 0
    assert "event=camilla_crossover_guard.repaired reason=fifo_absent" in r.stderr
    assert "driver-domain" in r.stderr  # the repair detail names the safe target
    assert "volume: -20.0" in statefile.read_text()  # other keys preserved


def test_missing_statefile_fails_open(tmp_path):
    r = _run(tmp_path, statefile=tmp_path / "absent.yml")
    assert r.returncode == 0
    assert "event=camilla_crossover_guard.skip reason=no_statefile" in r.stderr


def test_runtime_contract_blocked_leaves_statefile_untouched(tmp_path):
    """A blocked decision must fail CLOSED to silence — never half-act, never
    fall back to flat. The statefile is left exactly as-is."""
    fifo = tmp_path / SNAPFIFO_NAME  # absent
    cfg = _pipe_config(tmp_path, fifo)
    base = tmp_path / "base.yml"
    base.write_text("devices: {}\n")
    statefile = _write_statefile(tmp_path, cfg)
    before = statefile.read_text()
    r = _run(
        tmp_path, statefile=statefile, base=base, fifo=fifo, runtime_block=True,
    )
    assert r.returncode == 0
    assert "event=camilla_crossover_guard.skip reason=runtime_contract_blocked" in r.stderr
    assert statefile.read_text() == before


def test_runtime_contract_unavailable_fails_open(tmp_path):
    fifo = tmp_path / SNAPFIFO_NAME
    cfg = _pipe_config(tmp_path, fifo)
    statefile = _write_statefile(tmp_path, cfg)
    before = statefile.read_text()
    r = _run(tmp_path, statefile=statefile, fifo=fifo, runtime_helper=False)
    assert r.returncode == 0
    assert "event=camilla_crossover_guard.skip reason=runtime_contract_unavailable" in r.stderr
    assert statefile.read_text() == before
