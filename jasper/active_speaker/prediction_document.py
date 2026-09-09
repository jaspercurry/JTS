# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Project one saved capture prediction into frequency-view curves."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .frequency_reference import band_limited_curve, share_run_reference
from .frequency_view import FrequencyRun, FrequencySeries, frequency_series


def _identity(
    source: Any, candidate_id: Any, take_path: Any, *, prediction: bool = False,
) -> dict[str, Any]:
    source = source if isinstance(source, Mapping) else {}
    if prediction:
        return {
            "candidate_id": candidate_id,
            "identity_scope": "prediction_from_basis",
            "basis_capture_id": source.get("capture_id"),
            "basis_graph_fingerprint": source.get("graph_fingerprint"),
            "basis_take_path": take_path or source.get("record_path"),
        }
    return {
        "capture_id": source.get("capture_id"),
        "candidate_id": candidate_id,
        "graph_fingerprint": source.get("graph_fingerprint"),
        "take_path": take_path or source.get("record_path"),
    }


def _series(
    series_id: str,
    label: str,
    curve: Any,
    magnitude_field: str,
    band_field: str,
    identity: Mapping[str, Any],
    window: Mapping[str, Any],
    *,
    reference_db: float | None = None,
    visible: bool = False,
) -> FrequencySeries | None:
    if not isinstance(curve, Mapping):
        return None
    band = curve.get(band_field)
    freqs_hz, magnitude_db = band_limited_curve({
        "freqs_hz": curve.get("freqs_hz"),
        "magnitude_db": curve.get(magnitude_field),
        "band_hz": band,
    })
    return frequency_series(
        series_id=series_id,
        label=label,
        kind="analysis",
        freqs_hz=freqs_hz,
        magnitude_db=magnitude_db,
        reference_db=reference_db,
        visible_by_default=visible,
        evidence=series_id.split(":", 1)[0],
        role=(
            "difference" if magnitude_field == "delta_db"
            else magnitude_field.removesuffix("_db")
        ),
        band_hz=band,
        window_ms=window.get("window_ms"),
        validity_floor_hz=window.get("validity_floor_hz"),
        trusted_floor_hz=window.get("trusted_floor_hz"),
        level_offset_db=curve.get("level_offset_db"),
        **identity,
    )


def _same_prediction(prediction: Any, comparison: Any) -> bool:
    if not isinstance(prediction, Mapping) or not isinstance(comparison, Mapping):
        return False
    for prediction_field, comparison_field in (
        ("freqs_hz", "freqs_hz"),
        ("predicted_db", "predicted_db"),
        ("sum_band_hz", "compared_band_hz"),
    ):
        left = prediction.get(prediction_field)
        right = comparison.get(comparison_field)
        if (
            not isinstance(left, Sequence)
            or isinstance(left, (str, bytes))
            or not isinstance(right, Sequence)
            or isinstance(right, (str, bytes))
            or list(left) != list(right)
        ):
            return False
    return True


def frequency_run_from_capture_prediction(
    *,
    run_id: str,
    document: Mapping[str, Any],
    started_at: Any = None,
    state: str | None = None,
) -> FrequencyRun:
    summary = document.get("summary")
    summary = summary if isinstance(summary, Mapping) else {}
    basis = summary.get("basis")
    measured = summary.get("measured")
    window = summary.get("window")
    window = window if isinstance(window, Mapping) else {}
    candidate_id = summary.get("candidate_id")
    basis_candidate = basis.get("candidate_id") if isinstance(basis, Mapping) else None
    prediction = document.get("prediction")
    prediction_path = prediction.get("take_path") if isinstance(prediction, Mapping) else None
    comparisons = (
        ("reconstruction", "Reconstruction", document.get("reconstruction")),
        ("comparison", "Prediction comparison", document.get("predicted_minus_measured")),
    )
    duplicate_name = next(
        (name for name, _, comparison in comparisons
         if (name != "reconstruction" or candidate_id == basis_candidate)
         and _same_prediction(prediction, comparison)),
        None,
    )

    responses: list[FrequencySeries] = []
    differences: list[FrequencySeries] = []
    top = _series(
        "prediction:predicted",
        (
            "Forecast predicted response"
            if summary.get("comparison_kind")
            in {"unmeasured_forecast", "changed_candidate"}
            else "Prediction response"
        ),
        prediction,
        "predicted_db",
        "sum_band_hz",
        _identity(basis, candidate_id, prediction_path, prediction=True),
        window,
        visible=True,
    )
    if top is not None and duplicate_name is None:
        responses.append(top)

    for name, title, comparison in comparisons:
        if not isinstance(comparison, Mapping):
            continue
        predicted_identity = _identity(
            basis,
            basis_candidate if name == "reconstruction" else candidate_id,
            comparison.get("take_path"),
            prediction=True,
        )
        measured_source = basis if name == "reconstruction" else measured
        measured_identity = _identity(
            measured_source,
            (
                basis_candidate
                if name == "reconstruction"
                else measured.get("candidate_id") if isinstance(measured, Mapping) else None
            ),
            measured_source.get("record_path")
            if isinstance(measured_source, Mapping) else None,
        )
        for role, identity in (
            ("predicted", predicted_identity),
            ("measured", measured_identity),
        ):
            item = _series(
                f"{name}:{role}", f"{title} {role} response", comparison,
                f"{role}_db", "compared_band_hz", identity, window,
                visible=role == "predicted" and name == duplicate_name,
            )
            if item is not None:
                responses.append(item)
        difference_identity = {
            **predicted_identity,
            "measured_capture_id": measured_identity.get("capture_id"),
            "measured_graph_fingerprint": measured_identity.get("graph_fingerprint"),
            "measured_take_path": measured_identity.get("take_path"),
        }
        difference = _series(
            f"{name}:difference",
            f"{title} level-aligned difference (predicted − measured)",
            comparison,
            "delta_db",
            "compared_band_hz",
            difference_identity,
            window,
            reference_db=0.0,
        )
        if difference is not None:
            differences.append(difference)

    return FrequencyRun(
        id=run_id,
        label="Capture prediction",
        measurement_family="speaker_prediction",
        started_at=started_at,
        state=state,
        series=(*share_run_reference(responses, None), *differences),
        metadata={
            "source": "capture prediction artifact",
            "summary": dict(summary),
            "limitations": document.get("limitations"),
        },
    )


__all__ = ["frequency_run_from_capture_prediction"]
