#!/usr/bin/env python3
# Owns the generated blocks of the installed tuning docs.

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Render the installed tuning docs' generated blocks from their owners.

See ADR-0204 for the CLI menu and ADR-0181 for one owner per fact.

Usage::

    PYTHONPATH=. .venv/bin/python scripts/generate-tuning-tool-menu.py          # write
    PYTHONPATH=. .venv/bin/python scripts/generate-tuning-tool-menu.py --check  # verify; exit 1 on drift
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = ROOT / "docs" / "tuning-operator-runbook.md"
PLAYBOOK = ROOT / "docs" / "tuning-playbook.md"
BOUNDS_BEGIN = "<!-- BOUNDS_BEGIN -->"
BOUNDS_END = "<!-- BOUNDS_END -->"

BOUND_OWNERS = {
    "contract": "jasper.active_speaker.crossover_v2.prescription_contract:prescription_contracts()",
    "driver": "jasper.active_speaker.crossover_v2.driver_prescription:driver_prescription_response_format()",
    "blend": "jasper.active_speaker.crossover_v2.blend_prescription:prescription_response_format()",
    "room": "jasper.audio_measurement.room_limits",
    "alignment": "jasper.audio_measurement.program_analysis.model",
    "timing": "jasper.audio_measurement.program_analysis.response",
    "quality": "jasper.audio_measurement.quality_model",
    "safety": "jasper.active_speaker.profile",
    "gating": "jasper.audio_measurement.gating",
}

BOUND_SOURCES = {
    "Speaker": (
        ("driver.passband", "Hz", "contract.speaker.driver.bounds.passbands_hz"),
        ("driver.chain_scope", "rule", "contract.speaker.driver.bounds.chain_scope"),
        ("driver.trim_pin_scope", "rule", "contract.speaker.driver.bounds.trim_pin_scope"),
        ("driver.cut_Q", "Q", "contract.speaker.driver.bounds.q_range_cut"),
        ("driver.boost_Q_max", "Q", "contract.speaker.driver.bounds.q_max_boost"),
        ("driver.boost_headroom_rule", "dB", "contract.speaker.driver.bounds.boost_headroom_rule"),
        ("driver.cut_rule", "rule", "driver.bounds.cuts_are_free"),
        ("driver.filters_per_role", "count", "contract.speaker.driver.bounds.max_filters_per_role"),
        ("driver.shelf_rule", "rule", "contract.speaker.driver.bounds.shelf_rule"),
        ("driver.shelf_Q", "Q", "contract.speaker.driver.bounds.shelf_q"),
        ("driver.subaudible_below", "dB", "contract.speaker.driver.disclosures.subaudible_below_db"),
        ("driver.declared_tilt", "dB/octave", "contract.speaker.driver.schema.properties.declared_tilt_db_per_octave"),
        ("blend.passband", "Hz", "contract.speaker.blend.bounds.band_hz"),
        ("blend.cut_Q", "Q", "contract.speaker.blend.bounds.q_range_cut"),
        ("blend.boost_Q_max", "Q", "contract.speaker.blend.bounds.q_max_boost"),
        ("blend.filter_boost_max", "dB", "contract.speaker.blend.bounds.max_filter_boost_db"),
        ("blend.composed_boost_max", "dB", "contract.speaker.blend.bounds.max_composed_boost_db"),
        ("blend.cut_rule", "rule", "blend.bounds.cuts_are_free"),
        ("blend.filters", "count", "contract.speaker.blend.bounds.max_filters"),
        ("blend.filter_type", "type", "contract.speaker.blend.schema.properties.filters.items.properties.biquad_type.const"),
        ("blend.boost_route", "rule", "contract.speaker.blend.bounds.boost_route"),
        ("alignment.lobe", "us", "timing.half_period_us"),
        ("alignment.lobe_applies_to", "us", "contract.speaker.alignment.bounds.lobe_applies_to"),
        ("alignment.SNR_floor", "dB", "quality.DRIVER.alignment_snr_ok_db"),
        ("alignment.SPL_raise_margin", "dB", "safety.SPL_RAISE_MARGIN_DB"),
        ("gate.trusted_floor_multiplier", "cycles", "gating.TRUSTED_FLOOR_MULTIPLIER"),
    ),
    "Room": (
        ("passband", "Hz", "contract.room.bounds.band_hz"),
        ("floor", "Hz", "room.ROOM_FLOOR_HZ"),
        ("ceiling", "Hz", "contract.room.bounds.ceiling_hz"),
        ("cut_Q", "Q", "contract.room.bounds.q_range"),
        ("filter_boost_max", "dB", "contract.room.bounds.max_filter_boost_db"),
        ("total_boost_max", "dB", "contract.room.bounds.max_total_boost_db"),
        ("cut_floor_before_spread_and_taper", "dB", "room.ROOM_MAX_CUT_DB"),
        ("spread_tolerance", "dB", "room.TOLERABLE_STD_DB"),
        ("filters_per_side", "count", "contract.room.bounds.max_filters_per_side"),
        ("filter_type", "type", "contract.room.schema.properties.sides.additionalProperties.items.properties.biquad_type.const"),
        ("composed_tolerance", "dB", "contract.room.bounds.composed_tolerance_db"),
        ("boost_dip_max", "dB", "room.ROOM_BOOST_MAX_DIP_DB"),
        ("boost_dip_min", "dB", "room.ROOM_BOOST_MIN_DIP_DB"),
        ("boost_positions_min", "count", "room.ROOM_BOOST_MIN_POSITIONS"),
        ("boost_presence_min", "fraction", "room.ROOM_BOOST_PRESENCE_MIN_FRACTION"),
        ("boost_depth_agreement", "dB", "room.ROOM_BOOST_DEPTH_AGREEMENT_DB"),
        ("boost_width_min", "octaves", "room.ROOM_BOOST_MIN_WIDTH_OCTAVES"),
        ("taper", "octaves", "room.ROOM_TAPER_OCTAVES"),
    ),
    "Bass": (
        ("low_boost", "dB", "contract.bass.schema.properties.low_boost_db"),
        ("reference_level", "dB", "contract.bass.schema.properties.reference_level_db"),
        ("detector_lowpass", "Hz", "contract.bass.schema.properties.detector_lowpass_hz"),
        ("compressor_threshold", "dBFS", "contract.bass.schema.properties.compressor_threshold_dbfs"),
        ("compressor_factor", "ratio", "contract.bass.schema.properties.compressor_factor"),
        ("compressor_attack", "s", "contract.bass.schema.properties.compressor_attack_s"),
        ("compressor_release", "s", "contract.bass.schema.properties.compressor_release_s"),
        ("delta_highpass", "Hz", "contract.bass.schema.properties.delta_highpass_hz"),
        ("delta_highpass_exclusive_upper", "field", "contract.bass.bounds.delta_highpass_hz_exclusive_upper_field"),
        ("shared_headroom_layers", "layers", "contract.bass.shared_headroom.layers"),
    ),
    "Rear": (
        ("document_section", "field", "contract.rear.document_section"),
        ("case", "field", "contract.rear.case"),
        ("mode", "field", "contract.rear.mode"),
        ("max_filters_per_chain", "count", "contract.rear.bounds.max_filters_per_chain"),
        ("chain_gain_db", "dB", "contract.rear.bounds.chain_gain_db"),
        ("chain_gain_rule", "rule", "contract.rear.bounds.chain_gain_rule"),
        ("resonant_Q_max", "Q", "contract.rear.bounds.resonant_q_max"),
        ("allpass_Q_max", "Q", "contract.rear.bounds.allpass_q_max"),
        ("combo_order_max", "count", "contract.rear.bounds.combo_order_max"),
        ("biquad_kinds", "type", "contract.rear.bounds.biquad_kinds"),
        ("combo_kinds", "type", "contract.rear.bounds.combo_kinds"),
        ("cut_only_kinds", "type", "contract.rear.bounds.cut_only_kinds"),
        ("cut_only_rule", "rule", "contract.rear.bounds.cut_only_rule"),
        ("emitted_delay_rule", "rule", "contract.rear.bounds.emitted_delay_rule"),
        ("branch_delay_is_not_acoustic_delay", "rule", "contract.rear.bounds.branch_delay_is_not_acoustic_delay"),
        ("boundary_correction_rule", "rule", "contract.rear.bounds.boundary_correction_rule"),
        ("comparison_scope", "rule", "contract.rear.bounds.comparison_scope"),
        ("rear_muted_reference", "rule", "contract.rear.bounds.rear_muted_reference"),
        ("inheritance_rule", "rule", "contract.rear.bounds.inheritance_rule"),
    ),
}


def _source_value(owner: str) -> Any:
    module, _, path = owner.partition(":")
    value = importlib.import_module(module)
    for name in path.split(".") if path else ():
        call = name.endswith("()")
        value = getattr(value, name.removesuffix("()"))
        if call:
            value = value()
    return value


def render_bounds() -> str:
    owners = {name: _source_value(source) for name, source in BOUND_OWNERS.items()}
    lines = [BOUNDS_BEGIN, "```text"]
    lines.extend(f"{name} = {source}" for name, source in BOUND_OWNERS.items())
    for program, entries in BOUND_SOURCES.items():
        lines += ["", program, "| Name | Value | Unit | Constant or function field |", "|---|---|---|---|"]
        for name, unit, source in entries:
            owner, *keys = source.split(".")
            value = owners[owner]
            for key in keys:
                value = value[key] if isinstance(value, Mapping) else getattr(value, key)
            rendered = (f"{value.__name__}(fc_hz)" if callable(value) else
                        json.dumps(value, ensure_ascii=False, separators=(",", ":")))
            rendered = rendered.replace("—", "--").replace("|", "\\|")
            lines.append(f"| {name} | {rendered} | {unit} | {source} |")
    return "\n".join([*lines, "```", BOUNDS_END])

BEGIN_MARKER = (
    "<!-- BEGIN GENERATED TOOL MENU "
    "(scripts/generate-tuning-tool-menu.py -- do not hand-edit) -->"
)
END_MARKER = "<!-- END GENERATED TOOL MENU -->"

# The tuning tools this table covers: the [project.scripts] entries from
# pyproject.toml that docs/tuning-operator-runbook.md's tool menu names, in
# the happy path's own order. Widening this list is a deliberate edit, not
# something the generator infers -- see the module docstring for who is
# excluded and why.
TUNING_TOOL_MODULES: tuple[str, ...] = (
    "jasper.cli.basic_profile",
    "jasper.cli.mic_calibration",
    "jasper.cli.seat_level",
    "jasper.cli.angle_capture",
    "jasper.cli.measure",
    "jasper.cli.crossover_prescriber",
    "jasper.cli.round",
    "jasper.cli.round_views",
    "jasper.cli.null_door",
    "jasper.cli.audition",
    "jasper.cli.declare_geometry",
)


def _subcommand_names(parser: argparse.ArgumentParser) -> tuple[str, ...]:
    """Subcommand names, in the order ``add_parser`` added them.

    No public argparse API names this; ``format_usage`` walks the same
    private ``_subparsers`` action to build the ``{a,b,c}`` usage group this
    reads instead of re-parsing.
    """
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return tuple(action.choices)
    return ()


def _subcommand_labels(parser: argparse.ArgumentParser) -> tuple[str, ...]:
    names = _subcommand_names(parser)
    action = next((item for item in parser._actions
                   if isinstance(item, argparse._SubParsersAction)), None)
    help_by_name = {} if action is None else {
        choice.dest: choice.help or "" for choice in action._choices_actions
    }
    return tuple(
        f"{name} {help_by_name[name].split()[0]}"
        if help_by_name.get(name, "").startswith("[") else name
        for name in names
    )


def _tool_row(module_name: str) -> str:
    module = importlib.import_module(module_name)
    parser = module.build_parser()
    subcommands = _subcommand_labels(parser)
    tool = parser.prog + (" " + "\\|".join(subcommands) if subcommands else "")
    description = " ".join((parser.description or "").split())
    where = Path(cast(str, module.__file__)).resolve().relative_to(ROOT)
    return f"| `{tool}` | {description} | {module.AUTHORITY_TIER} | `{where}` |"


def render_table() -> str:
    header = "| Tool | Does | Authority | Where |\n|---|---|---|---|"
    rows = "\n".join(_tool_row(name) for name in TUNING_TOOL_MODULES)
    return f"{BEGIN_MARKER}\n{header}\n{rows}\n{END_MARKER}"


def _spliced(text: str, begin: str, end_marker: str, generated: str) -> str:
    """``text`` with the region between the markers replaced by ``generated``.

    Raises ``ValueError`` (uncaught, by design) if either marker is missing
    or out of order -- a generator that silently no-ops on a moved/deleted
    marker would let the runbook's committed table drift unnoticed, which is
    the exact failure this generator exists to close.
    """
    start = text.index(begin)
    end = text.index(end_marker, start) + len(end_marker)
    return text[:start] + generated + text[end:]


def spliced(text: str, generated: str) -> str:
    return _spliced(text, BEGIN_MARKER, END_MARKER, generated)


def render_document(text: str) -> str:
    return spliced(text, render_table())


def render_playbook(text: str) -> str:
    return _spliced(text, BOUNDS_BEGIN, BOUNDS_END, render_bounds())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check", action="store_true",
        help="verify both installed docs match their regenerated blocks; "
             "write nothing, exit 1 on drift",
    )
    args = parser.parse_args(argv)

    stale = False
    for path, render in ((RUNBOOK, render_document), (PLAYBOOK, render_playbook)):
        current = path.read_text(encoding="utf-8")
        updated = render(current)
        if args.check and updated != current:
            stale = True
            print(
                f"error: {path} generated content is stale -- re-run "
                "scripts/generate-tuning-tool-menu.py without --check",
                file=sys.stderr,
            )
        elif not args.check and updated != current:
            path.write_text(updated, encoding="utf-8")
    return int(stale)


if __name__ == "__main__":
    raise SystemExit(main())
