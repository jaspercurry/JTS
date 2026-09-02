# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

from jasper.local_sources.markers import marker_path
from jasper.music_sources import Source


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "claim-librespot.sh"


def test_claim_restore_rechecks_current_policy_instead_of_entry_snapshot():
    """OAuth restore is a request; the unit's final gate is authority."""

    text = SCRIPT.read_text(encoding="utf-8")

    assert "LIBRESPOT_WAS_ACTIVE" not in text
    assert "systemctl is-active --quiet librespot" not in text
    assert "sudo systemctl start librespot" in text  # early-exit cleanup
    assert "sudo systemctl restart librespot" in text  # credential reload
    assert marker_path(Source.SPOTIFY.value) in text
    assert "RESTORE_COMPLETED" in text
    assert "CLAIM_STARTED" not in text
    assert "/tmp/.last-claim-pid" not in text
    assert "pkill -F ${CLAIM_PID_FILE}" in text
    assert "pkill -f 'librespot --enable-oauth'" not in text
