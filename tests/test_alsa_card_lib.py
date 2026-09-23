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
