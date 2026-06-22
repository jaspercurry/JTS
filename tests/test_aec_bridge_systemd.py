# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Lock down jasper-aec-bridge.service's corpus env-file chain."""
from __future__ import annotations

from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
UNIT_PATH = REPO / "deploy" / "systemd" / "jasper-aec-bridge.service"


def test_bridge_sources_wake_corpus_env_after_system_env() -> None:
    """The recorder UI writes optional corpus-output flags under
    /var/lib/jasper because jasper-web cannot write /etc. The bridge
    must source that file after /etc/jasper/jasper.env so UI-enabled
    corpus outputs take effect on restart.
    """
    unit = UNIT_PATH.read_text()
    env_files = [
        line.strip().split("=", 1)[1]
        for line in unit.splitlines()
        if line.strip().startswith("EnvironmentFile=")
    ]

    assert "-/etc/jasper/jasper.env" in env_files
    assert "-/var/lib/jasper/wake_corpus_bridge.env" in env_files
    assert (
        env_files.index("-/etc/jasper/jasper.env")
        < env_files.index("-/var/lib/jasper/wake_corpus_bridge.env")
    )
