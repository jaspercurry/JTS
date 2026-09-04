# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-doctor checks for the loaded CamillaDSP graph and the runtime plan.

Import direction across the audio-runtime check modules runs one way —
``audio_runtime_camilla`` -> ``_fanin`` -> ``_outputd`` -> ``_ring``, so this
module may not import from any of the three.

Closed vocabulary for this module's `CheckResult.reason`: one snake_case
constant per distinct decision branch of the checks below, its value unique
across the doctor and prefixed by the check that emits it. `detail` stays the
human sentence (free to reword); `reason` is what tests and self-healing
consumers pin instead (ADR-0233 rule 3).

A branch that formed NO verdict — subsystem not installed, not applicable to
this box, or the evidence source unreachable so nothing was observed — is
`skipped` with a reason, never `ok`. An `ok` reason means an actual verdict a
consumer would branch on (a feature the box turned off, a floor that is
deliberately not renderable).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ...camilla_config_contract import (
    DEFAULT_PIPE_SINK_FORMAT,
    DEFAULT_PLAYBACK_FORMAT,
    parse_camilla_devices_config,
    read_camilla_devices_config,
)
from ._evidence import evidence
from ._registry import doctor_check
from ._shared import CheckResult, _service_state_failure
from .correction import _active_camilla_config_path

REASON_CAMILLA_UNIT_MISSING = "camilla_unit_missing"
REASON_CAMILLA_UNIT_NOT_ENABLED = "camilla_unit_not_enabled"
REASON_CAMILLA_INACTIVE = "camilla_inactive"

REASON_PLAYBACK_FORMAT_NO_CONFIG = "playback_format_no_config"
REASON_PLAYBACK_FORMAT_FIELD_ABSENT = "playback_format_field_absent"
REASON_PLAYBACK_FORMAT_MISMATCH = "playback_format_mismatch"

REASON_AUDIO_PLAN_ERRORS = "audio_plan_errors"
REASON_AUDIO_PLAN_WARNINGS = "audio_plan_warnings"

REASON_CAMILLA_PARK_RECORD_UNREADABLE = "camilla_park_record_unreadable"
REASON_CAMILLA_PARK_RECORD_UNINTELLIGIBLE = "camilla_park_record_unintelligible"
REASON_CAMILLA_GRAPH_PARKED = "camilla_graph_parked"


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
    service_failure = _service_state_failure(
        label,
        "jasper-camilla.service",
        missing=REASON_CAMILLA_UNIT_MISSING,
        not_enabled=REASON_CAMILLA_UNIT_NOT_ENABLED,
        inactive=REASON_CAMILLA_INACTIVE,
    )
    if service_failure is not None:
        return service_failure
    return CheckResult(label, "ok", "enabled and active")


def _camilla_statefile() -> Path:
    """The statefile behind :meth:`Evidence.camilla_config_path`, from the same
    single read (same memo key)."""
    statefile, _config_path = evidence.get(
        "camilla_config", _active_camilla_config_path
    )
    return statefile


def _loaded_device_fields(config_path: Path | str | None) -> dict[str, Any]:
    """Every ``devices.*`` field the audio-runtime checks compare, keyed
    ``<block>_<field>``, from ONE read of ``config_path`` per doctor run.

    The loaded graph's fields must all come from the SAME revision of the file:
    reading them one at a time re-opened it per field and could answer from two
    revisions. ``config_path`` is a parameter rather than always
    :meth:`Evidence.camilla_config_path` because ``check_fanin_coupling`` falls
    back to the shipped config path when the statefile names nothing.
    """
    if not config_path:
        return {}
    # Normalized, so the two callers' spellings of one path share one memo
    # entry and one read.
    path = str(Path(config_path))

    def read() -> dict[str, Any]:
        active = evidence.camilla_config_path()
        if active and path == str(Path(active)):
            text = evidence.camilla_config_text()
            return dict(parse_camilla_devices_config(text)) if text else {}
        return dict(read_camilla_devices_config(path) or {})

    return evidence.get(f"camilla_devices:{path}", read)


def _expected_playback_format(
    playback_type: str | None, playback_device: str | None
) -> tuple[str, str]:
    """``(expected_format, constant_name)`` for a loaded config's playback lane.

    Three lanes, three owners of the width — see
    :func:`check_camilla_playback_format`. The first two predicates are DISJOINT
    in every reachable config (a ``File`` sink carries no ``device`` key), so
    their order is not load-bearing.
    """
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

    THIS CHECK FORMS NO VERDICT ON A CONFIG IT CANNOT READ: an
    unreadable/absent statefile, an unresolvable ``config_path``, or a missing
    ``devices.playback.format`` all return ``skipped``. The unreadable half is owned
    by ``check_correction_current_config`` (``jasper/cli/doctor/correction.py``).
    """
    label = "camilla playback format"
    config_path = evidence.camilla_config_path()
    if config_path is None:
        return CheckResult(
            label,
            "skipped",
            "no loaded config to compare",
            reason=REASON_PLAYBACK_FORMAT_NO_CONFIG,
        )
    devices = _loaded_device_fields(config_path)
    loaded_format = devices.get("playback_format")
    if loaded_format is None:
        return CheckResult(
            label, "skipped",
            f"{config_path} has no devices.playback.format field",
            reason=REASON_PLAYBACK_FORMAT_FIELD_ABSENT,
        )
    playback_type = devices.get("playback_type")
    playback_device = devices.get("playback_device")
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
        f"({expected_name}) — a half-flipped box: {config_path} was generated "
        f"against a different {expected_name} than the one currently in "
        "force. Regenerate the config (sudo /opt/jasper/.venv/bin/jasper-sound "
        f"reconcile-current-dsp) or investigate why {expected_name} and the "
        "loaded config disagree.",
        reason=REASON_PLAYBACK_FORMAT_MISMATCH,
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
            reason=REASON_AUDIO_PLAN_ERRORS,
        )
    if plan.warnings:
        return CheckResult(
            "audio runtime plan",
            "warn",
            summary + "; " + "; ".join(plan.warnings[:3]),
            reason=REASON_AUDIO_PLAN_WARNINGS,
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
            reason=REASON_CAMILLA_PARK_RECORD_UNREADABLE,
        )

    if status == "unintelligible":
        return CheckResult(
            label,
            "warn",
            f"recovery park record at {state.get('path')} is present but "
            "carries no reason (a truncated write) — a park cannot be ruled "
            "out from it. Check journalctl -u jasper-camilla-recover.",
            reason=REASON_CAMILLA_PARK_RECORD_UNINTELLIGIBLE,
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
    return CheckResult(
        label,
        "fail",
        ". ".join(parts),
        speaker_silent=True,
        reason=REASON_CAMILLA_GRAPH_PARKED,
    )
