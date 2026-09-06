# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Narrow shared helpers for the jasper-doctor domain test modules."""

import json
import os

from jasper.cli import doctor
from jasper.config import Config
from jasper.output_hardware import OutputCardFact, OutputHardwareState, write_state


def _make_unit_states_fake(
    overrides: dict[str, dict[str, object]] | None = None, *, unavailable: bool = False,
):
    """Table-driven double for ``_evidence.read_unit_states`` (also
    ``jasper.service_units.read_unit_states``, the same function).

    ``overrides`` maps a FULL unit name (e.g. ``"jasper-camilla.service"``)
    to a partial state dict merged over a healthy default (loaded / active /
    running, MainPID 0). A unit requested but absent from ``overrides`` gets
    the healthy default — pass e.g. ``{"load_state": "not-found"}`` to model
    a unit this profile does not install. ``unavailable=True`` models
    systemctl itself being absent: every call returns None, as the real
    reader does on a dev host.
    """
    overrides = overrides or {}

    def fake(units, *, timeout):
        if unavailable:
            return None
        out = {}
        for unit in units:
            base = {
                "unit": unit,
                "load_state": "loaded",
                "active_state": "active",
                "sub_state": "running",
                "unit_file_state": "enabled",
                "result": "success",
                "n_restarts": 0,
                "main_pid": 0,
                "tasks_current": None,
                "memory_current_bytes": None,
                "cpu_usage_nsec": None,
                "control_group": "",
            }
            base.update(overrides.get(unit, {}))
            out[unit] = base
        return out

    return fake


def _fake_unit_states(
    active: dict[str, str] | None = None,
    *,
    load: dict[str, str] | None = None,
    default_active="inactive",
    default_load="loaded",
):
    """A ``_evidence.read_unit_states`` stand-in: ``active``/``load`` map a
    unit name to its ActiveState/LoadState word; any unit not named gets the
    matching default. Both the batched roster read and a per-unit fallback
    read route through this one fake, so it must answer for any units list."""
    active = active or {}
    load = load or {}

    def fake(units, *, timeout=2.0):
        return {
            u: {
                "unit": u,
                "load_state": load.get(u, default_load),
                "active_state": active.get(u, default_active),
                "sub_state": None,
                "unit_file_state": None,
                "result": None,
                "n_restarts": 0,
                "main_pid": 0,
                "tasks_current": None,
                "memory_current_bytes": None,
                "cpu_usage_nsec": None,
                "control_group": "",
            }
            for u in units
        }

    return fake


def _bootloop_marker(monkeypatch, tmp_path, payload) -> None:
    """Point ``JASPER_BOOTLOOP_MARKER_FILE`` at a per-test path and write
    ``payload`` there. ``None`` skips the write (models a marker that was
    never created); a ``str`` is written verbatim (models malformed JSON,
    e.g. ``"{torn"``); anything else is JSON-serialized."""
    p = tmp_path / "bootloop-state.json"
    monkeypatch.setenv("JASPER_BOOTLOOP_MARKER_FILE", str(p))
    if payload is None:
        return
    p.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")


def _registered_check_names() -> set[str]:
    """Return function names of every check registered via ``run_async``."""
    return {check.func.__name__ for check in doctor.registered_checks()}


def _pretend_group_is_jasper(monkeypatch):
    """CI has no `jasper` group; resolve every gid to it.

    Patches the shared ``grp`` module object, so it takes effect regardless
    of which doctor submodule performs the lookup (``_shared._group_writable_dir``,
    ``_classify_state_group_write``, …) — all of them do a bare ``import grp``
    and read ``grp.getgrgid`` off the same module.
    """
    import grp
    import types

    monkeypatch.setattr(
        grp, "getgrgid", lambda _gid: types.SimpleNamespace(gr_name="jasper")
    )


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


def record_active_dac(
    profile_id: str, *, card_id: str | None = None, status: str = "ready",
) -> None:
    """Write the reconciler's record naming ``profile_id`` at the path conftest
    isolates, so a check resolves the DAC the way a live box does.

    ``selected_card_id`` defaults to ``card_id`` (or ``profile_id`` when no
    card is given) because ``active_profile_id`` requires a non-empty one for
    a single DAC — a bare ``status="ready"`` without it would resolve no
    active DAC and no floor, unlike every real reconciler-written record."""
    children = (
        (OutputCardFact(card_id=card_id, device_id=profile_id),) if card_id else ()
    )
    write_state(
        OutputHardwareState(
            profile_id=profile_id,
            profile_label=profile_id,
            status=status,
            physical_output_count=2,
            selected_card_id=card_id or profile_id,
            child_devices=children,
        ),
        os.environ["JASPER_OUTPUT_HARDWARE_STATE_PATH"],
    )
