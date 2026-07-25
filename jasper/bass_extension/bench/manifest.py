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

The tap-realization amendment's "Receipts are bundle files, not schema fields"
section authorizes exactly one mechanism for R9's render bounds and R10's live
cross-check parameters to ride the campaign manifest: extending this module's
request-field set (never a new field on any closed evidence schema object).
``render_timeout_s`` / ``render_rlimit_as_bytes`` / ``render_rlimit_cpu_s`` /
``render_nice`` (R9's process-local bounds for every offline render this
stimulus role's pass produces — including ``digital_transfer_probe``'s) and
``cross_check_poll_interval_s`` / ``cross_check_read_count`` /
``cross_check_tolerance_db`` (R10(c)'s poll interval, read count, and
manifest tolerance — meaningful only for ``sweep_transparency`` and
``sustain_stress``, since ``digital_transfer_probe`` never touches hardware
and so has no live peak to cross-check) are therefore required on every
request, uniformly across all three roles, exactly like every other request
field — never invented from a default.
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
    """One target+role's operator-authorized stimulus request.

    ``render_*`` and ``cross_check_*`` are the tap-realization amendment's
    additive fields (see the module docstring) — R9's render bounds and
    R10(c)'s live cross-check parameters, riding the manifest.
    """

    requested_stimulus_band_hz: tuple[float, float]
    requested_stimulus_effective_peak_dbfs: float
    requested_commanded_main_volume_db: float
    requested_hold_duration_s: float
    requested_cooldown_s: float
    requested_repeat_count: int
    stimulus_generator_identity: str
    render_timeout_s: float
    render_rlimit_as_bytes: int
    render_rlimit_cpu_s: int
    render_nice: int
    cross_check_poll_interval_s: float
    cross_check_read_count: int
    cross_check_tolerance_db: float

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
            "render_timeout_s": self.render_timeout_s,
            "render_rlimit_as_bytes": self.render_rlimit_as_bytes,
            "render_rlimit_cpu_s": self.render_rlimit_cpu_s,
            "render_nice": self.render_nice,
            "cross_check_poll_interval_s": self.cross_check_poll_interval_s,
            "cross_check_read_count": self.cross_check_read_count,
            "cross_check_tolerance_db": self.cross_check_tolerance_db,
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
    "render_timeout_s",
    "render_rlimit_as_bytes",
    "render_rlimit_cpu_s",
    "render_nice",
    "cross_check_poll_interval_s",
    "cross_check_read_count",
    "cross_check_tolerance_db",
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

    # R9's render bounds and R10(c)'s cross-check parameters — see the module
    # docstring. Required uniformly on every request, exactly like every
    # other field above.
    render_timeout = _finite_float(raw.get("render_timeout_s"))
    render_rlimit_as = raw.get("render_rlimit_as_bytes")
    render_rlimit_cpu = raw.get("render_rlimit_cpu_s")
    render_nice = raw.get("render_nice")
    poll_interval = _finite_float(raw.get("cross_check_poll_interval_s"))
    read_count = raw.get("cross_check_read_count")
    tolerance = _finite_float(raw.get("cross_check_tolerance_db"))

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
        and render_timeout is not None
        and render_timeout > 0.0
        and type(render_rlimit_as) is int
        and render_rlimit_as > 0
        and type(render_rlimit_cpu) is int
        and render_rlimit_cpu > 0
        and type(render_nice) is int
        and 0 <= render_nice <= 19
        and poll_interval is not None
        and poll_interval > 0.0
        and type(read_count) is int
        and read_count > 0
        and tolerance is not None
        and tolerance > 0.0
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
        ("render_timeout_s", render_timeout is not None and render_timeout > 0.0),
        (
            "render_rlimit_as_bytes",
            type(render_rlimit_as) is int and render_rlimit_as > 0,
        ),
        (
            "render_rlimit_cpu_s",
            type(render_rlimit_cpu) is int and render_rlimit_cpu > 0,
        ),
        ("render_nice", type(render_nice) is int and 0 <= render_nice <= 19),
        (
            "cross_check_poll_interval_s",
            poll_interval is not None and poll_interval > 0.0,
        ),
        ("cross_check_read_count", type(read_count) is int and read_count > 0),
        ("cross_check_tolerance_db", tolerance is not None and tolerance > 0.0),
    ):
        if not ok and raw.get(field) is not None:
            missing.append(f"{path}.{field}")

    if band is None or not valid_scalars:
        return None
    assert peak is not None and commanded is not None
    assert hold is not None and cooldown is not None
    assert render_timeout is not None and poll_interval is not None
    assert tolerance is not None
    return StimulusRequest(
        requested_stimulus_band_hz=band,
        requested_stimulus_effective_peak_dbfs=peak,
        requested_commanded_main_volume_db=commanded,
        requested_hold_duration_s=hold,
        requested_cooldown_s=cooldown,
        requested_repeat_count=repeats,  # type: ignore[arg-type]
        stimulus_generator_identity=generator,  # type: ignore[arg-type]
        render_timeout_s=render_timeout,
        render_rlimit_as_bytes=render_rlimit_as,  # type: ignore[arg-type]
        render_rlimit_cpu_s=render_rlimit_cpu,  # type: ignore[arg-type]
        render_nice=render_nice,  # type: ignore[arg-type]
        cross_check_poll_interval_s=poll_interval,
        cross_check_read_count=read_count,  # type: ignore[arg-type]
        cross_check_tolerance_db=tolerance,
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
