# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path
import subprocess


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "switch-voice-provider.sh"


def test_switch_voice_provider_script_is_valid_bash():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)
