# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Narrow shared helpers for the jasper-doctor domain test modules."""

from jasper.cli import doctor
from jasper.config import Config


def _registered_check_names() -> set[str]:
    """Return function names of every check registered via ``run_async``."""
    return {check.func.__name__ for check in doctor.registered_checks()}


def _fresh_cfg(monkeypatch, **vars_) -> Config:
    """Build a ``Config`` with only the requested provider vars set."""
    drop = [
        "GEMINI_API_KEY",
        "OPENAI_API_KEY",
        "XAI_API_KEY",
        "JASPER_VOICE_PROVIDER",
        "JASPER_GEMINI_MODEL",
        "SPOTIFY_CLIENT_ID",
    ]
    for variable in drop:
        monkeypatch.delenv(variable, raising=False)
    defaults = {"JASPER_VOICE_PROVIDER": "gemini"}
    for key, value in {**defaults, **vars_}.items():
        monkeypatch.setenv(key, value)
    return Config.from_env()


def _write_identity_env(
    tmp_path, monkeypatch, *, collision="0", drift="0", checked_at=None,
    avahi="jts3.local",
):
    """Lay down a reconciler-shaped identity.env and point the doctor at it."""
    from datetime import datetime, timezone

    if checked_at is None:
        checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    path = tmp_path / "identity.env"
    path.write_text(
        "JASPER_IDENTITY_OS_HOSTNAME=jts3\n"
        f"JASPER_IDENTITY_AVAHI_HOSTNAME={avahi}\n"
        "JASPER_IDENTITY_CONFIGURED_HOSTNAME=jts3.local\n"
        "JASPER_IDENTITY_AVAHI_AVAILABLE=1\n"
        f"JASPER_IDENTITY_COLLISION={collision}\n"
        f"JASPER_IDENTITY_DRIFT={drift}\n"
        f"JASPER_IDENTITY_CHECKED_AT={checked_at}\n"
    )
    monkeypatch.setenv("JASPER_IDENTITY_FILE", str(path))
    return path


def _grouping_cfg(**overrides):
    from jasper.multiroom.config import (
        DEFAULT_BUFFER_MS,
        DEFAULT_CODEC,
        GroupingConfig,
    )

    values = {
        "enabled": False,
        "role": "",
        "channel": "stereo",
        "bond_id": "",
        "leader_addr": "",
        "buffer_ms": DEFAULT_BUFFER_MS,
        "codec": DEFAULT_CODEC,
        "error": None,
    }
    values.update(overrides)
    return GroupingConfig(**values)
