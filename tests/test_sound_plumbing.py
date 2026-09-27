# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_sound_setup_import_keeps_numpy_out_of_cold_start():
    code = (
        "import sys; "
        "import jasper.web.sound_setup; "
        "raise SystemExit(1 if 'numpy' in sys.modules or 'scipy' in sys.modules else 0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        check=False,
        timeout=10,
    )

    assert result.returncode == 0
