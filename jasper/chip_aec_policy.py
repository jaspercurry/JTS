# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared chip-AEC capability policy.

This module is intentionally side-effect-free. It does not probe ALSA,
talk to outputd, read env files, or write service config. Callers pass in
the hardware facts they already observed; this module classifies whether
the XVF3800 chip-AEC path may be armed automatically, explicitly, or only
as an operator testing run.
"""
from __future__ import annotations

import argparse
import shlex
from dataclasses import dataclass
from typing import Any, Literal, Mapping

from .audio_hardware import dac as dac_profiles


DacChipAecStatus = Literal["approved", "needs_calibration"]
ChipAecGateStatus = Literal["approved", "testing", "needs_calibration"]

STATUS_APPROVED: Literal["approved"] = "approved"
STATUS_TESTING: Literal["testing"] = "testing"
STATUS_TRIAL: Literal["testing"] = STATUS_TESTING
STATUS_NEEDS_CALIBRATION: Literal["needs_calibration"] = "needs_calibration"

ACTION_USE_CHIP_AEC = "use_chip_aec"
ACTION_RUN_TESTING_AND_VALIDATE = "run_chip_aec_testing_and_validate"
ACTION_USE_SOFTWARE_OR_TEST = "use_software_aec3_or_enable_testing"
ACTION_CALIBRATE_BEFORE_CHIP_AEC = "calibrate_output_dac_before_chip_aec"
ACTION_FIX_MIC_PROFILE = "fix_mic_profile_before_chip_aec"

SOURCE_MIC = "mic"
SOURCE_STATIC = "static"
SOURCE_OUTPUTD_AEC_CLOCK = "outputd_aec_clock"
SOURCE_OPERATOR_TESTING = "explicit_testing"
SOURCE_RUNTIME_ENV = "runtime_env"

HIFIBERRY_DAC8X_DAC_ID = dac_profiles.HIFIBERRY_DAC8X_ID


@dataclass(frozen=True)
class OutputdAecClockEvidence:
    """Already-observed outputd/XVF timing evidence.

    The policy module does not fetch this. The reconciler or a status
    surface that already read outputd passes the result in.
    """

    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class DacChipAecQualification:
    """Static DAC registry qualification for chip-AEC."""

    dac_id: str
    status: DacChipAecStatus
    source: str
    detail: str


@dataclass(frozen=True)
class ChipAecGate:
    """Combined mic + DAC gate for the production chip-AEC path."""

    dac_id: str
    status: ChipAecGateStatus
    source: str
    detail: str
    auto_allowed: bool
    arm_allowed: bool
    trial_allowed: bool
    blockers: tuple[str, ...] = ()

    @property
    def permitted(self) -> bool:
        return self.arm_allowed

    @property
    def production_allowed(self) -> bool:
        return self.auto_allowed

    @property
    def testing_allowed(self) -> bool:
        return self.trial_allowed

    @property
    def recommended_action(self) -> str:
        if self.status == STATUS_APPROVED:
            return ACTION_USE_CHIP_AEC
        if self.status == STATUS_TESTING:
            return ACTION_RUN_TESTING_AND_VALIDATE
        if "mic" in self.blockers:
            return ACTION_FIX_MIC_PROFILE
        return ACTION_USE_SOFTWARE_OR_TEST

    def to_dict(self) -> dict[str, object]:
        return {
            "dac_id": self.dac_id,
            "status": self.status,
            "source": self.source,
            "detail": self.detail,
            "permitted": self.permitted,
            "auto_allowed": self.auto_allowed,
            "arm_allowed": self.arm_allowed,
            "trial_allowed": self.trial_allowed,
            "production_allowed": self.production_allowed,
            "testing_allowed": self.testing_allowed,
            "recommended_action": self.recommended_action,
            "blockers": list(self.blockers),
        }


def normalize_dac_id(value: object) -> str:
    normalized = (
        str(value or "unknown")
        .strip()
        .strip("'\"")
        .lower()
        .replace("-", "_")
    )
    return normalized or "unknown"


def approved_chip_aec_dac_ids() -> tuple[str, ...]:
    return tuple(
        profile.id
        for profile in dac_profiles.all_profiles()
        if profile.chip_aec_qualification == STATUS_APPROVED
    )


APPROVED_DAC_IDS = frozenset(approved_chip_aec_dac_ids())
KNOWN_CALIBRATION_REQUIRED_DAC_IDS = frozenset(
    profile.id
    for profile in dac_profiles.all_profiles()
    if profile.chip_aec_qualification != STATUS_APPROVED
)


def static_dac_qualification(dac_id: object) -> DacChipAecQualification:
    normalized = normalize_dac_id(dac_id)
    profile = dac_profiles.by_id(normalized)
    if profile is None:
        if normalized == "unknown":
            detail = (
                "output DAC profile is unknown; run audio-hardware reconcile "
                "and calibrate chip-AEC timing before arming production chip AEC"
            )
        else:
            detail = (
                f"output DAC profile {normalized} has no codified chip-AEC "
                "calibration"
            )
        return DacChipAecQualification(
            dac_id=normalized,
            status=STATUS_NEEDS_CALIBRATION,
            source=SOURCE_STATIC,
            detail=detail,
        )

    if profile.chip_aec_qualification == STATUS_APPROVED:
        detail = profile.chip_aec_detail or (
            f"{profile.label} is approved for production chip-AEC"
        )
        return DacChipAecQualification(
            dac_id=normalized,
            status=STATUS_APPROVED,
            source=SOURCE_STATIC,
            detail=detail,
        )

    detail = profile.chip_aec_detail or (
        f"{profile.label} needs chip-AEC timing calibration before arming "
        "production chip AEC"
    )
    return DacChipAecQualification(
        dac_id=normalized,
        status=STATUS_NEEDS_CALIBRATION,
        source=SOURCE_STATIC,
        detail=detail,
    )


def _outputd_clock_evidence(
    outputd_status: Mapping[str, Any] | None,
    outputd_error: str = "",
) -> OutputdAecClockEvidence | None:
    if outputd_status is None:
        detail = (
            f"outputd aec_clock unavailable: {outputd_error}"
            if outputd_error else "outputd aec_clock unavailable"
        )
        return OutputdAecClockEvidence(ok=False, detail=detail)
    refs = outputd_status.get("reference_outputs")
    if not isinstance(refs, Mapping):
        return OutputdAecClockEvidence(
            ok=False,
            detail="outputd aec_clock unavailable: STATUS missing reference_outputs",
        )
    clock = refs.get("aec_clock")
    if not isinstance(clock, Mapping):
        return OutputdAecClockEvidence(
            ok=False,
            detail=(
                "outputd aec_clock unavailable: STATUS missing "
                "reference_outputs.aec_clock"
            ),
        )
    writer = refs.get("chip_ref_writer")
    if not isinstance(writer, Mapping) or not writer.get("enabled"):
        return OutputdAecClockEvidence(
            ok=False,
            detail="outputd aec_clock unavailable: chip-ref writer is not active",
        )
    verdict = str(clock.get("verdict") or "")
    status = str(clock.get("sro_estimator_status") or "")
    observe = clock.get("observe")
    ppm = clock.get("chip_ref_sro_ppm")
    reason = str(clock.get("verdict_reason") or "").strip()
    detail = (
        f"outputd aec_clock verdict={verdict or 'missing'} "
        f"status={status or 'missing'} observe={observe} "
        f"chip_ref_sro_ppm={ppm} reason={reason}"
    )
    return OutputdAecClockEvidence(
        ok=verdict == "coherent" and status == "locked",
        detail=detail,
    )


def resolve_chip_aec_dac_gate(
    dac_id: object,
    *,
    testing_requested: bool = False,
    outputd_status: Mapping[str, Any] | None = None,
    outputd_error: str = "",
) -> ChipAecGate:
    """Resolve the DAC side of the chip-AEC gate.

    Mic capability is intentionally separate: the mic profile registry owns
    beam-plan support, while this function owns output-DAC qualification.
    """

    static = static_dac_qualification(dac_id)
    if static.status == STATUS_APPROVED:
        return ChipAecGate(
            dac_id=static.dac_id,
            status=STATUS_APPROVED,
            source=static.source,
            detail=static.detail,
            auto_allowed=True,
            arm_allowed=True,
            trial_allowed=True,
        )

    outputd_clock = _outputd_clock_evidence(outputd_status, outputd_error)
    if outputd_clock is not None and outputd_clock.ok:
        return ChipAecGate(
            dac_id=static.dac_id,
            status=STATUS_APPROVED,
            source=SOURCE_OUTPUTD_AEC_CLOCK,
            detail=outputd_clock.detail,
            auto_allowed=True,
            arm_allowed=True,
            trial_allowed=True,
        )

    outputd_detail = ""
    if outputd_clock is not None and outputd_clock.detail:
        outputd_detail = f"; {outputd_clock.detail}"
    if testing_requested:
        return ChipAecGate(
            dac_id=static.dac_id,
            status=STATUS_TESTING,
            source=SOURCE_OPERATOR_TESTING,
            detail=(
                f"operator testing profile permits chip-AEC trial for "
                f"output DAC {static.dac_id}; {static.detail}{outputd_detail}"
            ),
            auto_allowed=False,
            arm_allowed=True,
            trial_allowed=True,
        )

    return ChipAecGate(
        dac_id=static.dac_id,
        status=STATUS_NEEDS_CALIBRATION,
        source=static.source,
        detail=f"{static.detail}{outputd_detail}",
        auto_allowed=False,
        arm_allowed=False,
        trial_allowed=True,
        blockers=("dac",),
    )


def gate_from_runtime_env(env: Mapping[str, str]) -> ChipAecGate | None:
    """Reconstruct the reconciler-applied DAC gate from jasper.env."""

    status = str(
        env.get("JASPER_AEC_CHIP_AEC_DAC_STATUS")
        or ""
    )
    if not status:
        return None
    dac_id = env.get("JASPER_AUDIO_DAC_ID", "unknown")
    source = str(
        env.get("JASPER_AEC_CHIP_AEC_DAC_SOURCE")
        or SOURCE_RUNTIME_ENV
    )
    detail = str(
        env.get("JASPER_AEC_CHIP_AEC_DAC_DETAIL")
        or ""
    )
    testing_requested = str(
        env.get("JASPER_AEC_CHIP_AEC_TESTING_REQUESTED") or "0"
    ).strip().lower() in {"1", "true", "yes", "on"}
    if status == STATUS_APPROVED:
        gate_status: ChipAecGateStatus = STATUS_APPROVED
        auto_allowed = True
        arm_allowed = True
        trial_allowed = True
        blockers: tuple[str, ...] = ()
    elif status in {STATUS_TESTING, "trial"}:
        gate_status = STATUS_TESTING
        auto_allowed = False
        arm_allowed = True
        trial_allowed = True
        blockers = ()
    else:
        gate_status = STATUS_NEEDS_CALIBRATION
        auto_allowed = False
        arm_allowed = False
        trial_allowed = True
        blockers = ("dac",)
    if testing_requested and gate_status == STATUS_NEEDS_CALIBRATION:
        trial_allowed = True
    return ChipAecGate(
        dac_id=normalize_dac_id(dac_id),
        status=gate_status,
        source=source,
        detail=detail,
        auto_allowed=auto_allowed,
        arm_allowed=arm_allowed,
        trial_allowed=trial_allowed,
        blockers=blockers,
    )


def resolve_chip_aec_gate(
    dac_id: object,
    *,
    mic_supported: bool,
    mic_detail: str = "",
    outputd_clock: OutputdAecClockEvidence | None = None,
    allow_trial: bool = False,
) -> ChipAecGate:
    """Return the shared chip-AEC gate for one requested runtime shape."""

    normalized = normalize_dac_id(dac_id)
    if not mic_supported:
        return ChipAecGate(
            dac_id=normalized,
            status=STATUS_NEEDS_CALIBRATION,
            source=SOURCE_MIC,
            detail=mic_detail or "mic has no validated production chip-AEC beam plan",
            auto_allowed=False,
            arm_allowed=False,
            trial_allowed=False,
            blockers=("mic",),
        )

    static = static_dac_qualification(normalized)
    if static.status == STATUS_APPROVED:
        return ChipAecGate(
            dac_id=normalized,
            status=STATUS_APPROVED,
            source=static.source,
            detail=static.detail,
            auto_allowed=True,
            arm_allowed=True,
            trial_allowed=True,
        )

    if outputd_clock is not None and outputd_clock.ok:
        return ChipAecGate(
            dac_id=normalized,
            status=STATUS_APPROVED,
            source=SOURCE_OUTPUTD_AEC_CLOCK,
            detail=outputd_clock.detail,
            auto_allowed=True,
            arm_allowed=True,
            trial_allowed=True,
        )

    outputd_detail = ""
    if outputd_clock is not None and outputd_clock.detail:
        outputd_detail = f"; {outputd_clock.detail}"
    if allow_trial:
        return ChipAecGate(
            dac_id=normalized,
            status=STATUS_TESTING,
            source=SOURCE_OPERATOR_TESTING,
            detail=(
                f"operator testing profile permits chip-AEC trial for "
                f"output DAC {normalized}; {static.detail}{outputd_detail}"
            ),
            auto_allowed=False,
            arm_allowed=True,
            trial_allowed=True,
        )

    return ChipAecGate(
        dac_id=normalized,
        status=STATUS_NEEDS_CALIBRATION,
        source=static.source,
        detail=f"{static.detail}{outputd_detail}",
        auto_allowed=False,
        arm_allowed=False,
        trial_allowed=True,
        blockers=("dac",),
    )


def _shell_bool(value: bool) -> str:
    return "1" if value else "0"


def _shell_assignments(gate: ChipAecGate) -> str:
    values = {
        "CHIP_AEC_GATE_DAC_ID": gate.dac_id,
        "CHIP_AEC_GATE_STATUS": gate.status,
        "CHIP_AEC_GATE_SOURCE": gate.source,
        "CHIP_AEC_GATE_DETAIL": gate.detail,
        "CHIP_AEC_GATE_AUTO_ALLOWED": _shell_bool(gate.auto_allowed),
        "CHIP_AEC_GATE_ARM_ALLOWED": _shell_bool(gate.arm_allowed),
        "CHIP_AEC_GATE_TRIAL_ALLOWED": _shell_bool(gate.trial_allowed),
        "CHIP_AEC_GATE_BLOCKERS": ",".join(gate.blockers),
    }
    return "\n".join(
        f"{key}={shlex.quote(str(value))}"
        for key, value in values.items()
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dac-id", default="unknown")
    parser.add_argument("--mic-supported", action="store_true")
    parser.add_argument("--mic-detail", default="")
    parser.add_argument("--allow-trial", action="store_true")
    parser.add_argument("--outputd-clock-ok", action="store_true")
    parser.add_argument("--outputd-clock-detail", default="")
    parser.add_argument("--shell", action="store_true")
    args = parser.parse_args(argv)

    evidence = None
    if args.outputd_clock_ok or args.outputd_clock_detail:
        evidence = OutputdAecClockEvidence(
            ok=bool(args.outputd_clock_ok),
            detail=args.outputd_clock_detail,
        )
    gate = resolve_chip_aec_gate(
        args.dac_id,
        mic_supported=bool(args.mic_supported),
        mic_detail=args.mic_detail,
        outputd_clock=evidence,
        allow_trial=bool(args.allow_trial),
    )
    if args.shell:
        print(_shell_assignments(gate))
    else:
        import json

        print(json.dumps(gate.to_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
