# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-doctor checks for the loaded CamillaDSP graph and the runtime plan.

Import direction across the audio-runtime check modules runs one way —
``audio_runtime`` -> ``_fanin`` -> ``_outputd`` -> ``_ring`` — so this
module may not import from any of the three.
"""
from __future__ import annotations

from pathlib import Path

from ...camilla_config_contract import read_camilla_device_field
from ._registry import doctor_check
from ._shared import CheckResult, _run
from .correction import _active_camilla_config_path


@doctor_check(order=51.1, group="audio")
def check_camilla_service() -> CheckResult:
    """The jasper-camilla systemd unit must never stay stopped.

    Owns the CLEAN-stop state its peers miss (#2163): `check_service_runtime_state`
    flags only `failed`, and `check_camilla_websocket` reports it as an
    unreachable 127.0.0.1:1234. "Enabled but not active" is unambiguous here
    because CamillaDSP has no gate that makes `inactive` legitimate, unlike
    jasper-outputd (missing-DAC `ExecCondition`) or jasper-voice
    (`voice-input-absent` marker).

    Returns:
      - ok when enabled and active.
      - fail when the unit is missing, disabled, or enabled and not active.
    """
    label = "jasper-camilla service"
    unit = "jasper-camilla.service"
    enabled = _run(["systemctl", "is-enabled", unit]).stdout.strip()
    active = _run(["systemctl", "is-active", unit]).stdout.strip()

    if enabled == "not-found":
        return CheckResult(
            label, "fail", "systemd unit not installed. Re-run install.sh."
        )
    if enabled in ("disabled", "static", "indirect"):
        return CheckResult(
            label,
            "fail",
            f"state={enabled}. CamillaDSP is mandatory; run: "
            f"sudo systemctl enable --now {unit}",
        )
    if active != "active":
        return CheckResult(
            label,
            "fail",
            f"enabled but state={active}. Every source's audio runs through "
            f"CamillaDSP, so nothing will play until it starts. Check: "
            f"journalctl -u jasper-camilla -u jasper-camilla-recover",
        )
    return CheckResult(label, "ok", "enabled and active")


def _loaded_device_field(config_path: Path, block: str, field: str) -> str | None:
    """A field from ``devices.<block>`` in a CamillaDSP config, or None."""
    return read_camilla_device_field(config_path, block, field)


def _loaded_capture_type(config_path: Path) -> str | None:
    """The ``devices.capture.type`` of a CamillaDSP config, or None."""
    return _loaded_device_field(config_path, "capture", "type")


def _loaded_playback_type(config_path: Path) -> str | None:
    """The ``devices.playback.type`` of a CamillaDSP config, or None."""
    return _loaded_device_field(config_path, "playback", "type")


def _loaded_playback_filename(config_path: Path) -> str | None:
    """The ``devices.playback.filename`` of a CamillaDSP config, or None."""
    return _loaded_device_field(config_path, "playback", "filename")


def _graph_feeds_the_bond(config_path: Path) -> bool:
    """Is this graph's post-DSP endpoint the bond rather than a local ring?

    A bonded LEADER's camilla#1 plays into the Snapcast pipe and never touches a
    ring device. The FILENAME is compared, not just the type: any other ``File``
    sink is a stale local pipe and must keep failing the playback axis.
    """
    from ...multiroom.reconcile import SNAPFIFO

    return (
        _loaded_playback_type(config_path) == "File"
        and _loaded_playback_filename(config_path) == SNAPFIFO
    )


def _loaded_playback_format(config_path: Path) -> str | None:
    """The ``devices.playback.format`` of a CamillaDSP config, or None."""
    return _loaded_device_field(config_path, "playback", "format")


def _loaded_playback_device(config_path: Path) -> str | None:
    """The ``devices.playback.device`` of a CamillaDSP config, or None."""
    return _loaded_device_field(config_path, "playback", "device")


def _expected_playback_format(
    playback_type: str | None, playback_device: str | None
) -> tuple[str, str]:
    """``(expected_format, constant_name)`` for a loaded config's playback lane.

    Three lanes, three owners of the width — see
    :func:`check_camilla_playback_format`. The first two predicates are DISJOINT
    in every reachable config (a ``File`` sink carries no ``device`` key), so
    their order is not load-bearing.
    """
    from jasper.camilla_config_contract import (
        DEFAULT_PIPE_SINK_FORMAT,
        DEFAULT_PLAYBACK_FORMAT,
    )
    from jasper.fanin_coupling import (
        RING_ACTIVE_PLAYBACK_DEVICE,
        RING_PLAYBACK_DEVICE,
        resolve_ring_wire,
    )

    if playback_type == "File":
        return DEFAULT_PIPE_SINK_FORMAT, "DEFAULT_PIPE_SINK_FORMAT"
    # MEMBERSHIP over every ring device, not one `==`: the resolved ring WIRE
    # format is one axis per box, shared by all three ring ends.
    if playback_device in (RING_PLAYBACK_DEVICE, RING_ACTIVE_PLAYBACK_DEVICE):
        return resolve_ring_wire().sample_format, "resolve_ring_wire"
    return DEFAULT_PLAYBACK_FORMAT, "DEFAULT_PLAYBACK_FORMAT"


@doctor_check(order=51.75, group="audio")
def check_camilla_playback_format() -> CheckResult:
    """The loaded CamillaDSP config's declared playback format must match its
    LANE's expected format.

    LANE-AWARE, and there are THREE lanes (:func:`_expected_playback_format`):

    - a ``File`` sink (the bonded-leader pipe, or the active-speaker parked
      graph's ``/dev/null``) expects ``DEFAULT_PIPE_SINK_FORMAT``, pinned narrow
      by the snapserver wire contract;
    - a ring playback device expects the wire ``resolve_ring_wire`` resolves —
      an armed ring is ``type: Alsa``, so the File split alone does not cover
      it, and the ring LAYOUT accepts both S16LE and S32LE, so nothing but this
      catches a ring config that drifted to the other one;
    - every other sink expects ``DEFAULT_PLAYBACK_FORMAT``.

    Keyed on the LOADED CONFIG's own ``device``/``type``, never on the persisted
    coupling, so a box mid-arm reads as whatever the config in front of it says.

    THIS CHECK FAILS OPEN ON A CONFIG IT CANNOT READ (it is cited elsewhere as
    the detector for a suppressed DSP reconcile): an unreadable/absent
    statefile, an unresolvable ``config_path``, or a missing
    ``devices.playback.format`` all return ``ok``. The unreadable half is owned
    by ``check_correction_current_config`` (``jasper/cli/doctor/correction.py``).
    """
    label = "camilla playback format"
    _, config_path = _active_camilla_config_path()
    if config_path is None:
        return CheckResult(label, "ok", "no loaded config to compare")
    path = Path(config_path)
    loaded_format = _loaded_playback_format(path)
    if loaded_format is None:
        return CheckResult(
            label, "ok", f"{path} has no devices.playback.format field"
        )
    playback_type = _loaded_playback_type(path)
    playback_device = _loaded_playback_device(path)
    expected_format, expected_name = _expected_playback_format(
        playback_type, playback_device
    )
    if loaded_format == expected_format:
        return CheckResult(
            label,
            "ok",
            f"playback format={loaded_format} "
            f"(type={playback_type}, device={playback_device}, "
            f"expected {expected_name})",
        )
    return CheckResult(
        label,
        "fail",
        f"loaded CamillaDSP playback format={loaded_format!r} for playback "
        f"type={playback_type!r} device={playback_device!r}, expected "
        f"{expected_format!r} "
        f"({expected_name}) — a half-flipped box: {path} was generated "
        f"against a different {expected_name} than the one currently in "
        "force. Regenerate the config (sudo /opt/jasper/.venv/bin/jasper-sound "
        f"reconcile-current-dsp) or investigate why {expected_name} and the "
        "loaded config disagree.",
    )


@doctor_check(order=51.6, group="audio")
def check_audio_runtime_plan() -> CheckResult:
    """Explainable SSOT check for audio latency/coupling knobs."""

    from jasper.audio_runtime_plan import build_audio_runtime_plan_from_system

    plan = build_audio_runtime_plan_from_system()
    # Policy vs observation: see AudioRuntimePlan.camilla_emitted. Reported, not
    # judged — the `camilla ring chunk` check owns the over-capacity failure.
    emitted = plan.camilla_emitted
    summary = (
        f"profile={plan.profile_id}, route={plan.route_mode}, "
        f"route_profile={plan.route_profile.route_id}, "
        f"route_hash={plan.route_config_hash}, "
        f"coupling={plan.setting('JASPER_FANIN_CAMILLA_COUPLING').value or '(unset)'}, "
        f"camilla_policy={plan.setting('JASPER_CAMILLA_CHUNKSIZE').value}/"
        f"{plan.setting('JASPER_CAMILLA_TARGET_LEVEL').value}, "
        + (
            f"camilla_emitted={emitted.chunksize}/{emitted.target_level}, "
            if emitted is not None
            else "camilla_emitted=unread, "
        )
        + f"outputd={plan.setting('JASPER_OUTPUTD_PERIOD_FRAMES').value}/"
        f"{plan.setting('JASPER_OUTPUTD_DAC_BUFFER_FRAMES').value}, "
        f"fanin={plan.setting('JASPER_FANIN_INPUT_BUFFER_FRAMES').value}"
    )
    if plan.errors:
        return CheckResult(
            "audio runtime plan",
            "fail",
            summary + "; " + "; ".join(plan.errors),
        )
    if plan.warnings:
        return CheckResult(
            "audio runtime plan",
            "warn",
            summary + "; " + "; ".join(plan.warnings[:3]),
        )
    return CheckResult("audio runtime plan", "ok", summary)


@doctor_check(order=52.67, group="audio")
def check_camilla_recover_park() -> CheckResult:
    """The core DSP graph is not parked by jasper-camilla-recover.

    ``deploy/bin/jasper-camilla-recover`` parks the graph when its one bounded
    recovery pass cannot bring it back (ADR-0175), and that park is TERMINAL for
    the boot by design. Severity is ``fail``: the speaker emits NOTHING and no
    automatic path recovers it. The record's own ``action=``/``re_arm=`` text is
    surfaced verbatim rather than restated here.
    """
    label = "camilla recovery park"

    from ...control import camilla_recover_state

    state = camilla_recover_state.snapshot()
    status = state.get("status")

    if status == "absent":
        return CheckResult(
            label, "ok", "no core-graph recovery park this boot"
        )

    if status == "unreadable":
        return CheckResult(
            label,
            "warn",
            f"recovery park record at {state.get('path')} exists but could "
            f"not be read ({state.get('error')}) — a park cannot be ruled "
            "out. Check journalctl -u jasper-camilla-recover.",
        )

    if status == "unintelligible":
        return CheckResult(
            label,
            "warn",
            f"recovery park record at {state.get('path')} is present but "
            "carries no reason (a truncated write) — a park cannot be ruled "
            "out from it. Check journalctl -u jasper-camilla-recover.",
        )

    parts = [
        f"PARKED — the core DSP graph was stopped after a failed recovery "
        f"({state.get('reason')})",
    ]
    parked_utc = state.get("parked_utc")
    if parked_utc:
        parts.append(f"at {parked_utc}")
    for field, prefix in (
        ("detail", ""),
        ("action", "ACTION: "),
        ("re_arm", "RE-ARM: "),
    ):
        value = state.get(field)
        if value:
            parts.append(f"{prefix}{value}")
    return CheckResult(label, "fail", ". ".join(parts))
