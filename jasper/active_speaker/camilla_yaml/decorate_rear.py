# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import re
from typing import Any, Collection, Mapping

from jasper.camilla_emit import emit_gain_filter
from jasper.speaker_layout import cardioid_cabinet_channels, measurement_target_id

from ..profile import ActiveSpeakerConfigError, ActiveSpeakerPreset
from ..rear_calibration import RearCalibrationError, compile_rear_stage, read_rear_calibration
from ..camilla_names import STARTUP_MUTE_GAIN_DB
from .topology import _output_count


def _rear_stage_channels(preset: ActiveSpeakerPreset) -> tuple[int, int, int] | None:
    """``(front woofer, rear woofer, tweeter)`` of the one cabinet a rear
    calibration document describes, or ``None`` when this preset is not one.

    ADR-0318: one document, one mono cabinet of exactly three declared outputs.
    """
    outputs = preset.channel_map.outputs
    if (
        preset.channel_map.layout != "mono"
        or preset.local_subwoofer is not None
        or len(outputs) != 3
    ):
        return None
    return cardioid_cabinet_channels(
        (output.driver_role, output.output_variant, output.index) for output in outputs
    )


def _validated_rear_calibration(
    document: Mapping[str, Any] | None, *, sample_rate: int
) -> dict[str, Any] | None:
    if not document:
        return None
    try:
        return read_rear_calibration(document, sample_rate=sample_rate)
    except RearCalibrationError as exc:
        raise ActiveSpeakerConfigError(f"rear calibration is invalid: {exc}") from exc


def _rear_calibration_graph(
    base: dict[str, Any], preset: ActiveSpeakerPreset, document: Mapping[str, Any]
) -> dict[str, Any]:
    """Splice the compiled cardioid stage in after the split, before every role chain."""
    channels = _rear_stage_channels(preset)
    if channels is None:
        raise ActiveSpeakerConfigError(
            "rear calibration requires a mono cabinet of one front woofer, "
            "one rear woofer and one tweeter"
        )
    front_channel, rear_channel, tweeter_channel = channels
    try:
        stage = compile_rear_stage(
            document,
            front_channel=front_channel,
            rear_channel=rear_channel,
            tweeter_channel=tweeter_channel,
            channel_count=_output_count(preset),
        )
    except RearCalibrationError as exc:
        raise ActiveSpeakerConfigError(f"rear calibration is invalid: {exc}") from exc
    for section, additions in (("filters", stage["filters"]), ("mixers", stage["mixers"])):
        if set(base[section]) & set(additions):
            raise ActiveSpeakerConfigError("rear calibration conflicts with the static speaker tune")
        base[section].update(additions)
    split = [
        index
        for index, step in enumerate(base["pipeline"])
        if step.get("type") == "Mixer" and str(step.get("name", "")).startswith("split_active_")
    ]
    if len(split) != 1:
        raise ActiveSpeakerConfigError("rear calibration needs exactly one active split mixer to follow")
    base["pipeline"][split[0] + 1 : split[0] + 1] = stage["pipeline"]
    return base


def _mute_unfitted_rear_outputs(
    text: str,
    preset: ActiveSpeakerPreset,
    *,
    excited_target_ids: Collection[str] = (),
) -> str:
    # Remove when the typed branch-transfer section owns rear protection and
    # routing/polarity qualification (issue #5161). Model seeds are not a tune.
    #
    # ``excited_target_ids`` names the physical targets a measurement take
    # drives on their own program channel: muting one would record silence
    # where the take needs its rear sweep. Measurement graphs only.
    rear = [output.index for output in preset.channel_map.outputs
            if output.output_variant == "rear"
            and measurement_target_id(output.driver_role, output.output_variant)
            not in excited_target_ids]
    if not rear:
        return text
    head, pipeline = text.split("\npipeline:\n", 1)
    parts = re.split(r"\n(?=[A-Za-z_][A-Za-z_0-9]*:)", pipeline, maxsplit=1)
    first_line = parts[0].splitlines()[0]
    indent = " " * (len(first_line) - len(first_line.lstrip(" ")))
    tail: list[str] = []
    for index in rear:
        name = f"as_out{index}_rear_pending_mute"
        head = head.replace("\nfilters:\n", "\nfilters:\n" + "\n".join(
            emit_gain_filter(name, STARTUP_MUTE_GAIN_DB, mute=True)
        ) + "\n", 1)
        tail.extend((f"{indent}- type: Filter", f"{indent}  channels: [{index}]", f"{indent}  names: [{name}]"))
    parts[0] = parts[0].rstrip() + "\n" + "\n".join(tail) + "\n"
    return head + "\npipeline:\n" + "\n".join(parts)
