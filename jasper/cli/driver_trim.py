# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Operator entry point for the measured per-driver BASE TRIM.

Answers one question: *how much quieter must each driver be so the acoustic
sum is level across every declared crossover?* — and banks the answer as the
speaker's base trim (:mod:`jasper.active_speaker.driver_base_trim`), replacing
the trim the profile otherwise derives from the drivers' declared datasheet
sensitivities.

Where it sits in commissioning::

    rough config at /sound -> MEASURED AUTO-TRIM (this verb) -> seat-level
        -> crossover candidates -> driver linearization -> room correction

Before seat-level, not after: seat-level's own ceiling is derived from the
DECLARED sensitivities, and the declaration is what this step measures against.

This module is wiring only. Every decision it makes belongs to someone else:

* the per-driver level — ``driver_acoustics.analyze_driver_capture``'s
  overlap-band read, the same estimator the guided level match uses;
* the delta-to-attenuation chain and its normalize-up —
  ``level_trim.attenuation_from_group_deltas``;
* which band a role is read in — ``commissioning_capture.driver_crossover_fcs``
  and ``driver_passband_hz``, derived from the preset's declared crossovers;
* the excitation ledger that makes two captures comparable —
  ``crossover_contract.verified_driver_excitation``;
* the mic's calibration — ``audio_measurement.calibration``;
* the record and its envelope — ``active_speaker.driver_base_trim``.

**It captures nothing yet.** This slice reads captures that already exist and
computes the trim from them; driving the sweeps itself (solo graph, admitted
excitation, play-and-capture, rollback) is the next change. The manifest is
therefore the input contract::

    {
      "artifact_schema_version": 1,
      "kind": "jts_active_speaker_driver_trim_captures",
      "captures": [
        {"speaker_group_id": "mono", "role": "woofer",
         "wav": "woofer.wav", "capture_geometry": "near_field",
         "sweep_meta": {...},
         "excitation": {"schema_version": 1, "scope": "...",
                        "sweep_peak_dbfs": -12.0,
                        "commissioning_gain_db": -40.0,
                        "effective_peak_dbfs": -52.0}}
      ]
    }

``wav`` is resolved relative to the manifest. ``sweep_meta`` is what
``driver_acoustics.write_driver_sweep_wav`` returned for that capture, because
the analysis regenerates the reference sweep rather than reloading the WAV.

**Why a calibration is required for a RELATIVE answer.** Both drivers are read
over the same one-octave band about the shared Fc at one mic position, so most
of the mic's own response is common to both reads and would cancel in the
delta. Only MOST of it: each capture's magnitude sits on the FFT grid its own
sweep sets, and gating can restrict one driver's band and not the other's, so
the two means are not taken over identical bins and the curve does not cancel
exactly. It is required for a second reason regardless — the banked record
names the microphone that produced the evidence, and evidence whose instrument
is unnamed cannot be compared with the next sitting's.

Usage::

    jasper-driver-trim --captures-dir /var/lib/jasper/driver-trim-2026-08-23 \\
        --mic-serial 810-8494

Exit 0 only on a banked base trim; 1 on any refusal, with the ``REFUSE_*``
reason on stderr — on stdout under ``--json`` — and in the ``event=`` line.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Mapping

from jasper.cli._logging import CLI_LOG_FORMAT
from jasper.log_event import log_event
from jasper.active_speaker.driver_base_trim import (
    DriverBaseTrimError,
    solve_base_trims,
    write_base_trim,
)
from jasper.audio_measurement.calibration import (
    MIC_CALIBRATION_UNAVAILABLE_DETAIL,
    REFUSE_MIC_CALIBRATION_UNAVAILABLE,
    resolve_mic_curve,
    resolve_mic_sensitivity,
)

logger = logging.getLogger(__name__)

CAPTURES_MANIFEST_KIND = "jts_active_speaker_driver_trim_captures"
CAPTURES_MANIFEST_NAME = "captures.json"

REFUSE_CAPTURES_MISSING = "driver_captures_missing"
REFUSE_CAPTURES_INVALID = "driver_captures_invalid"
REFUSE_EXCITATION_LEDGER_INVALID = "excitation_ledger_invalid"
REFUSE_DECLARATION_UNAVAILABLE = "driver_declaration_unavailable"
REFUSE_CAPTURE_UNUSABLE = "capture_unusable"
REFUSE_LEVEL_SOLVE_INCOMPLETE = "level_solve_incomplete"


class TrimRefusal(Exception):
    """One refusal, its reason slug, and the sentence an operator acts on."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def _load_captures_manifest(captures_dir: Path) -> list[Mapping[str, Any]]:
    """The per-role capture entries, or a refusal naming what is wrong."""
    path = captures_dir / CAPTURES_MANIFEST_NAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise TrimRefusal(
            REFUSE_CAPTURES_MISSING, f"cannot read {path}: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise TrimRefusal(REFUSE_CAPTURES_INVALID, f"{path} is not JSON: {exc}") from exc
    if not isinstance(raw, Mapping) or raw.get("kind") != CAPTURES_MANIFEST_KIND:
        raise TrimRefusal(
            REFUSE_CAPTURES_INVALID,
            f"{path} is not a {CAPTURES_MANIFEST_KIND} document",
        )
    captures = raw.get("captures")
    if not isinstance(captures, list) or not captures:
        raise TrimRefusal(REFUSE_CAPTURES_INVALID, f"{path} lists no captures")
    entries: list[Mapping[str, Any]] = []
    for entry in captures:
        if not isinstance(entry, Mapping):
            raise TrimRefusal(REFUSE_CAPTURES_INVALID, f"{path} has a non-object capture")
        entries.append(entry)
    return entries


def _resolve_declaration() -> tuple[Any, Mapping[str, Any], str]:
    """``(preset, crossover preview, its fingerprint)`` — what the trim is keyed to.

    The preview IS the declaration this speaker is being commissioned against,
    and its own fingerprint is what the profile's reader compares a banked trim
    to. A box with no staging-ready preview has nothing to key against, so this
    refuses rather than banking a trim nobody can match later.
    """
    from jasper.active_speaker.commission_wiring import (
        resolve_commission_inputs,
        resolve_commission_preset,
    )
    from jasper.active_speaker.crossover_preview import crossover_preview_fingerprint
    from jasper.output_topology import load_output_topology_strict

    topology = load_output_topology_strict(None)
    explicit_preset, preview = resolve_commission_inputs()
    if not isinstance(preview, Mapping) or not preview:
        raise TrimRefusal(
            REFUSE_DECLARATION_UNAVAILABLE,
            "no staging-ready crossover preview to measure against — save the "
            "base config at /sound first",
        )
    preset = resolve_commission_preset(
        topology, preset=explicit_preset, crossover_preview=dict(preview)
    )
    return preset, preview, crossover_preview_fingerprint(preview)


def _capture_levels(
    entries: list[Mapping[str, Any]],
    preset: Any,
    captures_dir: Path,
    curve: Any,
) -> tuple[
    dict[str, dict[str, dict[float, float]]], dict[str, dict[str, str]]
]:
    """``(group -> role -> declared Fc -> ledger-normalized level, group -> role
    -> capture geometry)``. The second map is banked with the record so a reader
    can see under which geometry each driver was read."""
    from jasper.active_speaker.commissioning_capture import (
        driver_crossover_fcs,
        driver_passband_hz,
    )
    from jasper.active_speaker.crossover_contract import verified_driver_excitation
    from jasper.active_speaker.driver_acoustics import (
        CAPTURE_GEOMETRIES,
        VERDICT_PRESENT,
        analyze_driver_capture,
        usable_overlap_level_db,
    )
    from jasper.active_speaker.profile import required_driver_roles

    roles = set(required_driver_roles(preset.way_count))
    # Two passes on purpose: the manifest is checked whole BEFORE any capture
    # is deconvolved, so a speaker whose third driver carries a broken ledger
    # is told so immediately instead of after two analyses it will discard.
    checked: list[tuple[str, str, Path, Mapping[str, Any], str, float]] = []
    seen: set[tuple[str, str]] = set()
    for entry in entries:
        role = str(entry.get("role") or "")
        group_id = str(entry.get("speaker_group_id") or "")
        if (group_id, role) in seen:
            # Last-wins would silently pick one of two captures of the same
            # driver and never say which, so the ambiguity is refused instead.
            raise TrimRefusal(
                REFUSE_CAPTURES_INVALID,
                f"{group_id}:{role} is captured twice; one manifest names one "
                "capture per driver per speaker group",
            )
        seen.add((group_id, role))
        if role not in roles or not group_id:
            raise TrimRefusal(
                REFUSE_CAPTURES_INVALID,
                f"capture {group_id or '?'}:{role or '?'} names no declared role "
                f"of this {preset.way_count}-way speaker",
            )
        excitation = verified_driver_excitation(entry.get("excitation"))
        if excitation is None:
            raise TrimRefusal(
                REFUSE_EXCITATION_LEDGER_INVALID,
                f"{group_id}:{role} carries no auditable excitation ledger; its "
                "level cannot be normalized onto a common reference",
            )
        wav = captures_dir / str(entry.get("wav") or "")
        sweep_meta = entry.get("sweep_meta")
        if not wav.is_file() or not isinstance(sweep_meta, Mapping):
            raise TrimRefusal(
                REFUSE_CAPTURES_INVALID,
                f"{group_id}:{role} needs an existing wav and its sweep_meta",
            )
        geometry = str(entry.get("capture_geometry") or "near_field")
        if geometry not in CAPTURE_GEOMETRIES:
            # Checked here so an out-of-vocabulary geometry is a refusal in this
            # verb's own words rather than a DriverAcousticsError traceback out
            # of the analysis, which is what "the manifest is checked whole
            # before any capture is deconvolved" promises.
            raise TrimRefusal(
                REFUSE_CAPTURES_INVALID,
                f"{group_id}:{role} names capture_geometry {geometry!r}; the "
                f"analysis reads {' or '.join(sorted(CAPTURE_GEOMETRIES))}",
            )
        checked.append((
            group_id,
            role,
            wav,
            sweep_meta,
            geometry,
            float(excitation["effective_peak_dbfs"]),
        ))

    levels: dict[str, dict[str, dict[float, float]]] = {}
    geometries: dict[str, dict[str, str]] = {}
    for group_id, role, wav, sweep_meta, geometry, effective_peak in checked:
        fcs = driver_crossover_fcs(preset, role)
        result = analyze_driver_capture(
            wav,
            sweep_meta,
            passband_hz=driver_passband_hz(preset, role),
            overlap_fcs=fcs,
            has_mic_calibration=curve is not None,
            calibration=curve,
            capture_geometry=geometry,
        )
        if result.verdict != VERDICT_PRESENT:
            raise TrimRefusal(
                REFUSE_CAPTURE_UNUSABLE,
                f"{group_id}:{role} analysed as {result.verdict}, not a driver "
                "producing sound in its declared band — capture it again",
            )
        for fc in fcs:
            level = usable_overlap_level_db(result.overlap_levels, fc)
            if level is None:
                raise TrimRefusal(
                    REFUSE_CAPTURE_UNUSABLE,
                    f"{group_id}:{role} has no usable overlap-band level at "
                    f"{fc:g} Hz — capture it again",
                )
            # The excitation-ledger normalize: captures taken at different
            # protected drive levels are only comparable once each is expressed
            # relative to the drive that produced it.
            levels.setdefault(group_id, {}).setdefault(role, {})[fc] = (
                level - effective_peak
            )
        geometries.setdefault(group_id, {})[role] = geometry
    return levels, geometries


def _run(args: argparse.Namespace) -> dict[str, Any]:
    """Resolve, analyse, solve, bank. Raises :class:`TrimRefusal` on any stop."""
    from jasper.active_speaker.profile import required_driver_roles

    captures_dir = Path(args.captures_dir)
    entries = _load_captures_manifest(captures_dir)

    mic = dict(
        calibration_file=args.calibration_file,
        mic_serial=args.mic_serial,
        mic_provider=args.mic_provider,
        mic_model=args.mic_model,
    )
    sensitivity = resolve_mic_sensitivity(**mic)
    resolved_curve = resolve_mic_curve(**mic)
    if sensitivity is None or resolved_curve is None:
        raise TrimRefusal(
            REFUSE_MIC_CALIBRATION_UNAVAILABLE, MIC_CALIBRATION_UNAVAILABLE_DETAIL
        )
    curve, sign_convention = resolved_curve

    preset, _preview, declaration_fingerprint = _resolve_declaration()
    roles = required_driver_roles(preset.way_count)
    regions = [
        (region.lower_driver, region.upper_driver, float(region.fc_hz))
        for region in sorted(preset.crossover_regions, key=lambda r: r.fc_hz)
    ]
    if not regions:
        raise TrimRefusal(
            REFUSE_DECLARATION_UNAVAILABLE,
            "this speaker declares no crossover, so there is no handoff band in "
            "which to level one driver against another",
        )

    levels, geometries = _capture_levels(entries, preset, captures_dir, curve)
    trims = solve_base_trims(levels, roles, regions)
    if not trims:
        raise TrimRefusal(
            REFUSE_LEVEL_SOLVE_INCOMPLETE,
            "no speaker group carries a usable level for both drivers of every "
            f"declared crossover ({', '.join(sorted(roles))})",
        )
    try:
        return write_base_trim(
            trims_db=trims,
            levels_db=levels,
            capture_geometries=geometries,
            roles=roles,
            regions=regions,
            declaration_fingerprint=declaration_fingerprint,
            microphone={
                "provider": args.mic_provider,
                "model": args.mic_model,
                "calibration_sign_convention": sign_convention,
                **sensitivity.to_dict(),
            },
            state_path=args.state,
        )
    except DriverBaseTrimError as exc:
        raise TrimRefusal(REFUSE_LEVEL_SOLVE_INCOMPLETE, str(exc)) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jasper-driver-trim",
        description=(
            "Compute the measured per-driver base trim from per-driver captures "
            "and bank it as the speaker's new starting point. Runs after the "
            "rough config at /sound and before seat-level, crossover "
            "candidates, and driver linearization."
        ),
    )
    parser.add_argument(
        "--captures-dir",
        required=True,
        help=(
            f"directory holding {CAPTURES_MANIFEST_NAME} and the per-driver WAVs "
            "it names"
        ),
    )
    parser.add_argument(
        "--calibration-file",
        help="explicit vendor calibration .txt carrying the 'Sens Factor' line",
    )
    parser.add_argument(
        "--mic-serial",
        help="look the stored calibration up by microphone serial instead",
    )
    parser.add_argument("--mic-provider", default="minidsp")
    parser.add_argument("--mic-model", default="minidsp_umik2")
    parser.add_argument(
        "--state",
        default=None,
        help="where to bank the record (default: the base-trim statefile)",
    )
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    # Without a handler ``logging.lastResort`` floors at WARNING, which discards
    # ``event=active_speaker.driver_trim_banked`` — the one line attributing a
    # banked trim to the run that measured it. ``basicConfig`` at INFO in
    # ``main`` is what the sibling ``event=``-emitting CLIs do, and NOT
    # ``_logging.configure_verbose_logging``, whose no-``--verbose`` floor is
    # the level that hides this; ``crossover_prescriber.main`` carries the full
    # rationale for both choices.
    logging.basicConfig(level=logging.INFO, format=CLI_LOG_FORMAT)
    args = build_parser().parse_args(argv)
    if not args.calibration_file and not args.mic_serial:
        build_parser().error("pass --calibration-file or --mic-serial")
    try:
        payload = _run(args)
    except TrimRefusal as refusal:
        log_event(
            logger,
            "active_speaker.driver_trim_refused",
            level=logging.WARNING,
            reason=refusal.reason,
            detail=refusal.detail,
        )
        if args.json:
            print(
                json.dumps(
                    {"status": "refused", "reason": refusal.reason,
                     "detail": refusal.detail},
                    indent=2, sort_keys=True,
                )
            )
        else:
            print(f"refused ({refusal.reason}): {refusal.detail}", file=sys.stderr)
        return 1
    log_event(
        logger,
        "active_speaker.driver_trim_banked",
        trims=" ".join(
            f"{role}={value:.1f}" for role, value in sorted(payload["trims_db"].items())
        ),
        declaration=payload["declaration_fingerprint"][:12],
        state_path=payload["state_path"],
    )
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            "banked base trim: "
            + ", ".join(
                f"{role} {value:.1f} dB"
                for role, value in sorted(payload["trims_db"].items())
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
