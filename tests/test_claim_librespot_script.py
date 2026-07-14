# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

from jasper.local_sources import guard
from jasper.multiroom.config import (
    DEFAULT_BUFFER_MS,
    DEFAULT_CODEC,
    GroupingConfig,
)
from jasper.music_sources import Source


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "claim-librespot.sh"
LIBRESPOT_UNIT = ROOT / "deploy" / "systemd" / "librespot.service"


def _leader_config() -> GroupingConfig:
    return GroupingConfig(
        enabled=True,
        role="leader",
        channel="left",
        bond_id="bond-1",
        leader_addr="jts.local",
        buffer_ms=DEFAULT_BUFFER_MS,
        codec=DEFAULT_CODEC,
        error=None,
    )


def test_claim_restore_rechecks_current_policy_instead_of_entry_snapshot():
    """OAuth restore is a request; the unit's final gate is authority."""

    text = SCRIPT.read_text(encoding="utf-8")
    unit = LIBRESPOT_UNIT.read_text(encoding="utf-8")

    assert "LIBRESPOT_WAS_ACTIVE" not in text
    assert "systemctl is-active --quiet librespot" not in text
    assert "sudo systemctl start librespot" in text  # early-exit cleanup
    assert "sudo systemctl restart librespot" in text  # credential reload
    assert (
        "sudo /opt/jasper/.venv/bin/jasper-local-source-allowed "
        "--source spotify"
    ) in text
    assert "RESTORE_COMPLETED" in text
    assert (
        "ExecCondition=+/usr/bin/env -i PATH=/opt/jasper/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin /opt/jasper/.venv/bin/jasper-local-source-allowed "
        "--source spotify"
    ) in unit


def test_spotify_off_landing_mid_claim_wins_final_restart(monkeypatch):
    """The exact start boundary observes Off written during the OAuth wait."""

    intent = {Source.SPOTIFY: True}
    monkeypatch.setattr(guard, "load_config", _leader_config)
    monkeypatch.setattr(
        guard,
        "source_intent_enabled",
        lambda source: intent[source],
    )

    # Claim entered while Spotify was allowed and then waited for the human.
    assert guard.local_source_allowed(Source.SPOTIFY) == (True, None)

    # Household Off lands before the script's final systemctl restart. The
    # service ExecCondition executes here, immediately before ExecStart.
    intent[Source.SPOTIFY] = False
    assert guard.local_source_allowed(Source.SPOTIFY) == (
        False,
        "source_intent_disabled",
    )
