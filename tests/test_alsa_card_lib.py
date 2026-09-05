# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "deploy/lib/jasper-alsa-card.sh"


def test_shared_parser_extracts_first_matching_card(tmp_path: Path):
    hints = tmp_path / "alsa-hints"
    hints.write_text(
        "#!/usr/bin/env bash\n"
        "cat <<'EOF'\n"
        "sysdefault:CARD=Apple\n"
        "    USB Audio Device\n"
        "sysdefault:CARD=DAC8x\n"
        "    DAC8x Studio Output\n"
        "EOF\n",
        encoding="utf-8",
    )
    hints.chmod(0o755)

    result = subprocess.run(
        [
            "bash",
            "-c",
            f'source "{LIB}"; jasper_find_alsa_card "{hints}" "DAC8x Studio"',
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "DAC8x\n"


def test_install_uses_the_shared_parser_and_ships_it():
    """install.sh is the parser's one shell consumer since ADR-0235 R2 moved
    the reconciler's card lookup behind the classifier's env emitter."""
    install = (ROOT / "deploy/install.sh").read_text(encoding="utf-8")
    units = (ROOT / "deploy/lib/install/systemd-units.sh").read_text(
        encoding="utf-8"
    )

    assert 'source "${REPO_DIR}/deploy/lib/jasper-alsa-card.sh"' in install
    assert 'jasper_find_alsa_card "$1" "$2"' in install
    assert "/usr/local/lib/jasper/jasper-alsa-card.sh" in units
