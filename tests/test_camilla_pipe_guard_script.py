# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for deploy/bin/jasper-camilla-pipe-guard.

The ExecStartPre chain-breaker for the bonded reboot loop
(docs/HANDOFF-multiroom.md §2): camilladsp 4.1.3 exits CLEAN on a dead
File sink (measured on jts3, 2026-06-11), which jasper-camilla's
Restart=always + StartLimitAction=reboot would turn into a Pi reboot
loop. The guard repairs the statefile to the base config BEFORE camilla
launches when the bonded pipe is dead.

Pure-bash policy script, tested via subprocess.run with env-overridden
paths into tmp dirs — the jasper-wifi-guardian harness pattern. Every
test asserts exit 0 (the guard is FAIL-OPEN: it must never block the
music chain's start path), plus the structured
`event=camilla_pipe_guard.<outcome>` line and the statefile's final
content.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "bin" / "jasper-camilla-pipe-guard"

SNAPFIFO_NAME = "snapfifo"


def _runtime_safe_graph_script(tmp_path: Path) -> Path:
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
    if ! grep -q '^[[:space:]]*config_path:' "$tmp"; then
        { printf 'config_path: %s\\n' "$flat"; cat "$tmp"; } > "${tmp}.with-path"
        mv "${tmp}.with-path" "$tmp"
    fi
else
    printf 'config_path: %s\\nvolume:\\n- 0.0\\n' "$flat" > "$tmp"
fi
mv "$tmp" "$statefile"
printf '{"ok":true,"status":"select_flat"}\\n'
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
    env["JASPER_CAMILLA_STATEFILE"] = str(statefile or tmp_path / "statefile.yml")
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


def _solo_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "sound_current.yml"
    cfg.write_text(
        "devices:\n  playback:\n    type: Alsa\n    channels: 2\n"
        '    device: "outputd_content_playback"\n    format: S16_LE\n'
    )
    return cfg


def test_solo_config_is_a_noop(tmp_path):
    cfg = _solo_config(tmp_path)
    statefile = _write_statefile(tmp_path, cfg)
    before = statefile.read_text()
    r = _run(tmp_path, statefile=statefile)
    assert r.returncode == 0
    assert "event=camilla_pipe_guard.ok reason=solo_config" in r.stderr
    assert statefile.read_text() == before  # untouched


def test_pipe_config_with_dead_fifo_repairs_to_base(tmp_path):
    """THE chain-breaker: bonded config + absent FIFO would clean-exit-loop
    camilla into StartLimitAction=reboot. The guard repairs first."""
    fifo = tmp_path / SNAPFIFO_NAME  # never created
    cfg = _pipe_config(tmp_path, fifo)
    base = tmp_path / "base.yml"
    base.write_text("devices: {}\n")
    statefile = _write_statefile(tmp_path, cfg)
    r = _run(tmp_path, statefile=statefile, base=base, fifo=fifo)
    assert r.returncode == 0
    assert "event=camilla_pipe_guard.repaired reason=fifo_absent" in r.stderr
    assert f"config_path: {base}" in statefile.read_text()
    assert "volume: -20.0" in statefile.read_text()  # other keys preserved


def test_pipe_config_with_reader_is_healthy_noop(tmp_path):
    fifo = tmp_path / SNAPFIFO_NAME
    os.mkfifo(fifo)
    cfg = _pipe_config(tmp_path, fifo)
    statefile = _write_statefile(tmp_path, cfg)
    before = statefile.read_text()
    # Hold a real read end open (what a live snapserver does).
    reader_fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    try:
        r = _run(tmp_path, statefile=statefile, fifo=fifo)
    finally:
        os.close(reader_fd)
    assert r.returncode == 0
    # With GNU timeout present the probe confirms the reader
    # (pipe_healthy); without it (stock macOS) the guard fails open on
    # the existing FIFO (probe_unavailable). Both are ok-and-untouched.
    assert "event=camilla_pipe_guard.ok reason=pipe_" in r.stderr
    assert statefile.read_text() == before


def test_pipe_config_with_readerless_fifo_repairs(tmp_path):
    """The measured alive-but-silent variant: FIFO exists, no reader —
    camilladsp would block in open(2) (uninterruptible by SIGTERM). The
    write-open probe times out and the guard repairs."""
    import shutil

    if shutil.which("timeout") is None:
        import pytest

        pytest.skip("GNU timeout unavailable (the guard fails open here)")
    fifo = tmp_path / SNAPFIFO_NAME
    os.mkfifo(fifo)
    cfg = _pipe_config(tmp_path, fifo)
    base = tmp_path / "base.yml"
    base.write_text("devices: {}\n")
    statefile = _write_statefile(tmp_path, cfg)
    r = _run(tmp_path, statefile=statefile, base=base, fifo=fifo)
    assert r.returncode == 0
    assert "event=camilla_pipe_guard.repaired reason=no_reader" in r.stderr
    assert f"config_path: {base}" in statefile.read_text()


def test_missing_statefile_fails_open(tmp_path):
    r = _run(tmp_path, statefile=tmp_path / "absent.yml")
    assert r.returncode == 0
    assert "event=camilla_pipe_guard.skip reason=no_statefile" in r.stderr


def test_missing_base_config_fails_open_and_leaves_statefile(tmp_path):
    """A blocked runtime-contract decision must NOT half-act."""
    fifo = tmp_path / SNAPFIFO_NAME  # absent
    cfg = _pipe_config(tmp_path, fifo)
    statefile = _write_statefile(tmp_path, cfg)
    before = statefile.read_text()
    r = _run(tmp_path, statefile=statefile, base=tmp_path / "no-base.yml", fifo=fifo)
    assert r.returncode == 0
    assert "event=camilla_pipe_guard.skip reason=runtime_contract_blocked" in r.stderr
    assert statefile.read_text() == before


def test_runtime_contract_unavailable_fails_open_and_leaves_statefile(tmp_path):
    fifo = tmp_path / SNAPFIFO_NAME
    cfg = _pipe_config(tmp_path, fifo)
    statefile = _write_statefile(tmp_path, cfg)
    before = statefile.read_text()
    r = _run(tmp_path, statefile=statefile, fifo=fifo, runtime_helper=False)
    assert r.returncode == 0
    assert "event=camilla_pipe_guard.skip reason=runtime_contract_unavailable" in r.stderr
    assert statefile.read_text() == before


def test_runtime_contract_blocked_fails_closed_and_leaves_statefile(tmp_path):
    fifo = tmp_path / SNAPFIFO_NAME
    cfg = _pipe_config(tmp_path, fifo)
    base = tmp_path / "base.yml"
    base.write_text("devices: {}\n")
    statefile = _write_statefile(tmp_path, cfg)
    before = statefile.read_text()
    r = _run(
        tmp_path,
        statefile=statefile,
        base=base,
        fifo=fifo,
        runtime_block=True,
    )
    assert r.returncode == 0
    assert "event=camilla_pipe_guard.skip reason=runtime_contract_blocked" in r.stderr
    assert statefile.read_text() == before


def test_unquoted_and_quoted_config_paths_both_parse(tmp_path):
    fifo = tmp_path / SNAPFIFO_NAME
    cfg = _pipe_config(tmp_path, fifo)
    base = tmp_path / "base.yml"
    base.write_text("devices: {}\n")
    for raw in (f"config_path: {cfg}", f"config_path: '{cfg}'"):
        statefile = tmp_path / "statefile.yml"
        statefile.write_text(raw + "\n")
        r = _run(tmp_path, statefile=statefile, base=base, fifo=fifo)
        assert r.returncode == 0
        assert "repaired reason=fifo_absent" in r.stderr


def test_statefile_with_no_config_path_fails_open(tmp_path):
    statefile = tmp_path / "statefile.yml"
    statefile.write_text("volume: -20.0\n")
    r = _run(tmp_path, statefile=statefile)
    assert r.returncode == 0
    assert "event=camilla_pipe_guard.skip reason=no_config_path" in r.stderr
