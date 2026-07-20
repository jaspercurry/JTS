# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Operator-authored ``campaign_manifest`` for the limiter-evidence bench run.

The frozen protocol ("Required bench owner — no hidden authority") requires the
runner to begin from a reviewed ``campaign_manifest`` that records, for every
target and stimulus role, the requested stimulus band, effective peak,
commanded main volume, hold, cooldown, repeat count, and generator identity —
all **operator-authorized inputs, never invented from a default**. A missing
value is a refusal, not a filled blank.

This module is pure. It validates the operator's supplied inputs and produces a
strict :class:`CampaignManifest`; it never reads a clock, device, or default.
The caller (the CLI / bench operator) composes the inputs — for example seeding
the sustain hold from the selected ``MarginPolicy.sustain_duration_s`` is the
operator's authored choice made *before* calling here, not a default applied
inside this module.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

# The three exact stimulus roles the frozen protocol names.
STIMULUS_ROLES: tuple[str, ...] = (
    "digital_transfer_probe",
    "sweep_transparency",
    "sustain_stress",
)


class ManifestRefusal(ValueError):
    """One or more operator manifest inputs are missing or malformed.

    ``missing_paths`` names every absent required input (sorted, unique) so the
    operator sees exactly what to supply. The runner refuses the campaign
    rather than filling any value from a default.
    """

    def __init__(self, missing_paths: Sequence[str]) -> None:
        self.missing_paths: tuple[str, ...] = tuple(sorted(set(missing_paths)))
        super().__init__(
            "campaign manifest is missing operator-authorized inputs: "
            + ", ".join(self.missing_paths)
        )


@dataclass(frozen=True, slots=True)
class StimulusRequest:
    """One target+role's operator-authorized stimulus request."""

    requested_stimulus_band_hz: tuple[float, float]
    requested_stimulus_effective_peak_dbfs: float
    requested_commanded_main_volume_db: float
    requested_hold_duration_s: float
    requested_cooldown_s: float
    requested_repeat_count: int
    stimulus_generator_identity: str

    def to_dict(self) -> dict[str, object]:
        return {
            "requested_stimulus_band_hz": [
                self.requested_stimulus_band_hz[0],
                self.requested_stimulus_band_hz[1],
            ],
            "requested_stimulus_effective_peak_dbfs": (
                self.requested_stimulus_effective_peak_dbfs
            ),
            "requested_commanded_main_volume_db": (
                self.requested_commanded_main_volume_db
            ),
            "requested_hold_duration_s": self.requested_hold_duration_s,
            "requested_cooldown_s": self.requested_cooldown_s,
            "requested_repeat_count": self.requested_repeat_count,
            "stimulus_generator_identity": self.stimulus_generator_identity,
        }


@dataclass(frozen=True, slots=True)
class CampaignManifest:
    """The complete operator-authored campaign manifest.

    ``requests`` is keyed ``target_id -> role -> StimulusRequest`` for every
    target in the sealed family and every stimulus role.
    """

    driver_safety_fingerprint: str
    margin_policy_name: str
    margin_policy_fingerprint: str
    requests: Mapping[str, Mapping[str, StimulusRequest]]

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": "jts_bass_extension_bench_campaign_manifest",
            "schema_version": 1,
            "driver_safety_fingerprint": self.driver_safety_fingerprint,
            "margin_policy_name": self.margin_policy_name,
            "margin_policy_fingerprint": self.margin_policy_fingerprint,
            "requests": {
                target_id: {
                    role: request.to_dict() for role, request in by_role.items()
                }
                for target_id, by_role in self.requests.items()
            },
        }


_REQUEST_FIELDS: tuple[str, ...] = (
    "requested_stimulus_band_hz",
    "requested_stimulus_effective_peak_dbfs",
    "requested_commanded_main_volume_db",
    "requested_hold_duration_s",
    "requested_cooldown_s",
    "requested_repeat_count",
    "stimulus_generator_identity",
)


def _finite_float(value: object) -> float | None:
    if type(value) is int:  # accept an exact int as a float quantity
        value = float(value)
    if type(value) is not float or not math.isfinite(value):
        return None
    return value


def _read_request(
    raw: object,
    *,
    path: str,
    missing: list[str],
) -> StimulusRequest | None:
    if not isinstance(raw, Mapping):
        missing.append(path)
        return None
    for field in _REQUEST_FIELDS:
        if field not in raw or raw[field] is None:
            missing.append(f"{path}.{field}")

    band_raw = raw.get("requested_stimulus_band_hz")
    band: tuple[float, float] | None = None
    if isinstance(band_raw, Sequence) and not isinstance(band_raw, (str, bytes)):
        values = list(band_raw)
        if len(values) == 2:
            low = _finite_float(values[0])
            high = _finite_float(values[1])
            if low is not None and high is not None and 0.0 < low < high:
                band = (low, high)
    if band_raw is not None and band is None:
        missing.append(f"{path}.requested_stimulus_band_hz")

    peak = _finite_float(raw.get("requested_stimulus_effective_peak_dbfs"))
    commanded = _finite_float(raw.get("requested_commanded_main_volume_db"))
    hold = _finite_float(raw.get("requested_hold_duration_s"))
    cooldown = _finite_float(raw.get("requested_cooldown_s"))
    repeats = raw.get("requested_repeat_count")
    generator = raw.get("stimulus_generator_identity")

    valid_scalars = (
        peak is not None
        and commanded is not None
        and hold is not None
        and hold > 0.0
        and cooldown is not None
        and cooldown >= 0.0
        and type(repeats) is int
        and repeats > 0
        and type(generator) is str
        and bool(generator.strip())
        and generator == generator.strip()
    )
    for field, ok in (
        ("requested_stimulus_effective_peak_dbfs", peak is not None),
        ("requested_commanded_main_volume_db", commanded is not None),
        ("requested_hold_duration_s", hold is not None and hold > 0.0),
        ("requested_cooldown_s", cooldown is not None and cooldown >= 0.0),
        ("requested_repeat_count", type(repeats) is int and repeats > 0),
        (
            "stimulus_generator_identity",
            type(generator) is str
            and bool(generator.strip())
            and generator == generator.strip(),
        ),
    ):
        if not ok and raw.get(field) is not None:
            missing.append(f"{path}.{field}")

    if band is None or not valid_scalars:
        return None
    assert peak is not None and commanded is not None
    assert hold is not None and cooldown is not None
    return StimulusRequest(
        requested_stimulus_band_hz=band,
        requested_stimulus_effective_peak_dbfs=peak,
        requested_commanded_main_volume_db=commanded,
        requested_hold_duration_s=hold,
        requested_cooldown_s=cooldown,
        requested_repeat_count=repeats,  # type: ignore[arg-type]
        stimulus_generator_identity=generator,  # type: ignore[arg-type]
    )


def author_campaign_manifest(
    operator_inputs: Mapping[str, object],
    *,
    target_ids: Sequence[str],
    roles: Sequence[str] = STIMULUS_ROLES,
) -> CampaignManifest:
    """Validate operator inputs into a :class:`CampaignManifest` or refuse.

    Every target+role must carry a complete :class:`StimulusRequest`, plus the
    top-level ``driver_safety_fingerprint`` / ``margin_policy_name`` /
    ``margin_policy_fingerprint``. A single missing or malformed value raises
    :class:`ManifestRefusal` naming every absent path — the runner never
    supplies a default.
    """

    missing: list[str] = []

    def _top(field: str) -> str | None:
        value = operator_inputs.get(field)
        if type(value) is str and value.strip() and value == value.strip():
            return value
        missing.append(field)
        return None

    driver_fp = _top("driver_safety_fingerprint")
    margin_name = _top("margin_policy_name")
    margin_fp = _top("margin_policy_fingerprint")

    requests_raw = operator_inputs.get("requests")
    requests: dict[str, dict[str, StimulusRequest]] = {}
    if not isinstance(requests_raw, Mapping):
        missing.append("requests")
    else:
        for target_id in target_ids:
            by_role_raw = requests_raw.get(target_id)
            if not isinstance(by_role_raw, Mapping):
                missing.append(f"requests.{target_id}")
                continue
            by_role: dict[str, StimulusRequest] = {}
            for role in roles:
                request = _read_request(
                    by_role_raw.get(role),
                    path=f"requests.{target_id}.{role}",
                    missing=missing,
                )
                if request is not None:
                    by_role[role] = request
            requests[target_id] = by_role

    if missing or driver_fp is None or margin_name is None or margin_fp is None:
        raise ManifestRefusal(missing)

    return CampaignManifest(
        driver_safety_fingerprint=driver_fp,
        margin_policy_name=margin_name,
        margin_policy_fingerprint=margin_fp,
        requests=requests,
    )
