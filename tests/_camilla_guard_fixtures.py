# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared filesystem fixtures for the two Camilla start guards."""

from __future__ import annotations

from pathlib import Path


def write_runtime_safe_graph_script(
    tmp_path: Path,
    *,
    success_status: str,
    ensure_config_path: bool = False,
) -> Path:
    """Write the guard tests' small runtime-contract stand-in.

    Appends one line per invocation to ``$JASPER_FAKE_RUNTIME_CALLS`` when that
    is set, so a test can count spawns.
    """

    ensure_path = """
    if ! grep -q '^[[:space:]]*config_path:' "$tmp"; then
        { printf 'config_path: %s\\n' "$flat"; cat "$tmp"; } > "${tmp}.with-path"
        mv "${tmp}.with-path" "$tmp"
    fi""" if ensure_config_path else ""
    script = tmp_path / "runtime-safe-graph"
    script.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
if [[ -n "${{JASPER_FAKE_RUNTIME_CALLS:-}}" ]]; then
    printf 'call\\n' >> "$JASPER_FAKE_RUNTIME_CALLS"
fi
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
if [[ "${{JASPER_FAKE_RUNTIME_BLOCK:-0}}" == "1" || ! -r "$flat" ]]; then
    printf '{{"ok":false,"status":"blocked"}}\\n'
    exit 1
fi
tmp="${{statefile}}.fake.$$"
if [[ -f "$statefile" ]]; then
    sed "s|^\\([[:space:]]*config_path:\\).*|\\1 ${{flat}}|" "$statefile" > "$tmp"{ensure_path}
else
    printf 'config_path: %s\\nvolume:\\n- 0.0\\n' "$flat" > "$tmp"
fi
mv "$tmp" "$statefile"
printf '{{"ok":true,"status":"{success_status}"}}\\n'
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def write_statefile(tmp_path: Path, config_path: Path) -> Path:
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f'config_path: "{config_path}"\nvolume: -20.0\n')
    return statefile


def write_pipe_config(tmp_path: Path, fifo: Path) -> Path:
    config = tmp_path / "grouping_leader.yml"
    config.write_text(
        "devices:\n  playback:\n    type: File\n    channels: 2\n"
        f'    filename: "{fifo}"\n    format: S16_LE\n'
    )
    return config
