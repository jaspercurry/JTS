# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Durable, hardware-bound Bass Extension Profile facts."""

from __future__ import annotations

import json
import math
import os
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Mapping

from jasper.atomic_io import atomic_write_text
from jasper.audio_measurement.evidence_identity import (
    ArtifactIdentity,
    json_fingerprint,
)

if TYPE_CHECKING:
    from jasper.bass_extension.adapters.base import TargetSpec
    from jasper.bass_extension.targets import AnchorPoint

BASS_EXTENSION_PROFILE_KIND = "jts_bass_extension_profile"
BASS_EXTENSION_SCHEMA_VERSION = 1
BASS_EXTENSION_ALGORITHM_VERSION = "bass_extension_v1"
DEFAULT_PROFILE_PATH = Path("/var/lib/jasper/bass_extension_profile.json")
PROFILE_PATH_ENV = "JASPER_BASS_EXTENSION_PROFILE_STATE"


class BassExtensionRefusal(StrEnum):
    BASELINE_NOT_APPLIED = "bass_extension_baseline_not_applied"
    TOPOLOGY_MISMATCH = "bass_extension_topology_mismatch"
    BASS_OWNER_AMBIGUOUS = "bass_extension_bass_owner_ambiguous"
    BONDED_BASS_OWNER_REMOTE = "bass_extension_bonded_bass_owner_remote"
    ENCLOSURE_UNKNOWN = "bass_extension_enclosure_unknown"
    ENCLOSURE_UNSUPPORTED = "bass_extension_enclosure_unsupported"
    TUNING_NOT_LOCATED = "bass_extension_tuning_not_located"
    PR_NOTCH_NOT_LOCATED = "bass_extension_pr_notch_not_located"
    FIT_QUALITY_INSUFFICIENT = "bass_extension_fit_quality_insufficient"
    CAPTURE_QUALITY_REFUSED = "bass_extension_capture_quality_refused"
    CAPTURE_SNR_INSUFFICIENT = "bass_extension_capture_snr_insufficient"
    MIC_MOVED_BETWEEN_RUNGS = "bass_extension_mic_moved_between_rungs"
    LADDER_INCOMPLETE = "bass_extension_ladder_incomplete"
    BOOST_LIMIT_EXCEEDED = "bass_extension_boost_limit_exceeded"
    PROFILE_STALE = "bass_extension_profile_stale"


_PROFILE_FIELDS = {
    "kind", "schema_version", "profile_id", "created_at", "algorithm_version",
    "baseline_fingerprint", "topology_id", "topology_fingerprint", "bass_owner",
    "enclosure", "mic_calibration_id", "measurement_ids", "natural", "targets", "anchors",
    "margin", "digital_margin_db", "clean_ceiling", "sustain_test", "impedance_import", "status",
}


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} must be non-empty trimmed text")
    return value


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be finite numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite numeric")
    return number


def _object(value: Any, name: str, keys: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ValueError(f"{name} has unknown or missing fields")
    return value


def _json_value(value: Any, name: str) -> Any:
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{name} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise ValueError(f"{name} contains a non-string key")
        return {key: _json_value(item, name) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item, name) for item in value]
    raise ValueError(f"{name} contains a non-JSON value")


def _frozen_json(value: Any, name: str) -> Any:
    value = _json_value(value, name)
    if isinstance(value, dict):
        return MappingProxyType({key: _frozen_json(item, name) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_frozen_json(item, name) for item in value)
    return value


def _target_to_dict(target: TargetSpec) -> dict[str, Any]:
    return {
        "target_id": target.target_id,
        "fp_hz": target.fp_hz,
        "qp": target.qp,
        "filters": [_json_value(item, "target filters") for item in target.filters],
        "boost_headroom_db": target.boost_headroom_db,
        "limiter_threshold_dbfs": target.limiter_threshold_dbfs,
        "subsonic": _json_value(target.subsonic, "target subsonic"),
    }


def _target_from_dict(value: Any, index: int) -> TargetSpec:
    from jasper.bass_extension.adapters.base import TargetSpec

    name = f"targets[{index}]"
    raw = _object(value, name, {
        "target_id", "fp_hz", "qp", "filters", "boost_headroom_db",
        "limiter_threshold_dbfs", "subsonic",
    })
    filters = raw["filters"]
    if type(filters) is not list or any(not isinstance(item, Mapping) for item in filters):
        raise ValueError(f"{name}.filters must be a list of objects")
    subsonic = raw["subsonic"]
    if subsonic is not None and not isinstance(subsonic, Mapping):
        raise ValueError(f"{name}.subsonic must be an object or null")
    qp = raw["qp"]
    limiter = raw["limiter_threshold_dbfs"]
    return TargetSpec(
        target_id=_text(raw["target_id"], f"{name}.target_id"),
        fp_hz=_number(raw["fp_hz"], f"{name}.fp_hz"),
        qp=None if qp is None else _number(qp, f"{name}.qp"),
        filters=tuple(_frozen_json(item, f"{name}.filters") for item in filters),
        boost_headroom_db=_number(
            raw["boost_headroom_db"], f"{name}.boost_headroom_db"
        ),
        limiter_threshold_dbfs=(
            None if limiter is None else _number(limiter, f"{name}.limiter_threshold_dbfs")
        ),
        subsonic=None if subsonic is None else _frozen_json(subsonic, f"{name}.subsonic"),
    )


def _anchor_to_dict(anchor: AnchorPoint) -> dict[str, Any]:
    return {
        "target_id": anchor.target_id,
        "max_listening_level": anchor.max_listening_level,
        "evidence": anchor.evidence,
    }


def _anchor_from_dict(value: Any, index: int) -> AnchorPoint:
    from jasper.bass_extension.targets import AnchorPoint

    name = f"anchors[{index}]"
    raw = _object(value, name, {"target_id", "max_listening_level", "evidence"})
    level = raw["max_listening_level"]
    if type(level) is not int or not 0 <= level <= 100:
        raise ValueError(f"{name}.max_listening_level must be an integer from 0 to 100")
    evidence = _text(raw["evidence"], f"{name}.evidence")
    if evidence not in {"measured", "derived", "spot_verified"}:
        raise ValueError(f"{name}.evidence is unsupported")
    return AnchorPoint(_text(raw["target_id"], f"{name}.target_id"), level, evidence)


def _normalise_natural(adapter_id: str, value: Any) -> dict[str, Any]:
    from jasper.bass_extension.adapters.base import (
        ADAPTERS,
        PassiveRadiatorPlantFit,
        PortedPlantFit,
        SealedPlantFit,
    )

    if adapter_id not in ADAPTERS:
        raise ValueError("enclosure.adapter_id is unsupported")
    if adapter_id == "sealed_v1":
        return SealedPlantFit.from_dict(value).to_dict()
    if adapter_id == "ported_v1":
        return PortedPlantFit.from_dict(value).to_dict()
    return PassiveRadiatorPlantFit.from_dict(value).to_dict()


@dataclass(frozen=True)
class BassExtensionProfile:
    created_at: str
    algorithm_version: str
    baseline_fingerprint: str
    topology_id: str
    topology_fingerprint: str
    bass_owner: Mapping[str, Any]
    enclosure: Mapping[str, Any]
    mic_calibration_id: str | None
    measurement_ids: tuple[ArtifactIdentity, ...]
    natural: Mapping[str, Any]
    targets: tuple[TargetSpec, ...]
    anchors: tuple[AnchorPoint, ...]
    margin: str
    digital_margin_db: float
    clean_ceiling: Mapping[str, Any]
    sustain_test: Mapping[str, Any] | None
    impedance_import: Mapping[str, Any] | None
    status: str
    profile_id: str = field(init=False)

    def __post_init__(self) -> None:
        _text(self.created_at, "created_at")
        _text(self.algorithm_version, "algorithm_version")
        _text(self.baseline_fingerprint, "baseline_fingerprint")
        _text(self.topology_id, "topology_id")
        _text(self.topology_fingerprint, "topology_fingerprint")
        owner = _object(
            _json_value(self.bass_owner, "bass_owner"),
            "bass_owner",
            {"kind", "roles", "channels"},
        )
        if owner["kind"] not in {"woofer_way", "local_sub"}:
            raise ValueError("bass_owner.kind is unsupported")
        if type(owner["roles"]) is not list or not all(
            isinstance(item, str) and item for item in owner["roles"]
        ):
            raise ValueError("bass_owner.roles must be a list of role names")
        if type(owner["channels"]) is not list or not all(
            type(item) is int and item >= 0 for item in owner["channels"]
        ):
            raise ValueError("bass_owner.channels must be non-negative integers")
        object.__setattr__(self, "bass_owner", _frozen_json(owner, "bass_owner"))

        enclosure = _object(
            self.enclosure,
            "enclosure",
            {"adapter_id", "adapter_version", "cabinet_fingerprint"},
        )
        adapter_id = _text(enclosure["adapter_id"], "enclosure.adapter_id")
        if type(enclosure["adapter_version"]) is not int:
            raise ValueError("enclosure.adapter_version must be an integer")
        _text(enclosure["cabinet_fingerprint"], "enclosure.cabinet_fingerprint")
        object.__setattr__(self, "enclosure", _frozen_json(enclosure, "enclosure"))
        object.__setattr__(
            self,
            "natural",
            _frozen_json(
                _normalise_natural(adapter_id, _json_value(self.natural, "natural")),
                "natural",
            ),
        )

        if self.mic_calibration_id is not None:
            _text(self.mic_calibration_id, "mic_calibration_id")
        if type(self.measurement_ids) is not tuple or any(
            not isinstance(item, ArtifactIdentity) for item in self.measurement_ids
        ):
            raise ValueError("measurement_ids must contain ArtifactIdentity values")
        if type(self.targets) is not tuple or not self.targets:
            raise ValueError("targets must be a non-empty tuple")
        targets = tuple(
            _target_from_dict(_target_to_dict(target), index)
            for index, target in enumerate(self.targets)
        )
        if len({target.target_id for target in targets}) != len(targets):
            raise ValueError("targets must have unique target_id values")
        if any(deeper.fp_hz > shallower.fp_hz for deeper, shallower in zip(targets, targets[1:])):
            raise ValueError("targets must be ordered deepest first")
        natural = targets[-1]
        if natural.target_id != "natural" or natural.filters or natural.boost_headroom_db != 0.0:
            raise ValueError("last target must be natural with empty filters and 0.0 boost")
        object.__setattr__(self, "targets", targets)

        if type(self.anchors) is not tuple:
            raise ValueError("anchors must be a tuple")
        anchors = tuple(
            _anchor_from_dict(_anchor_to_dict(anchor), index)
            for index, anchor in enumerate(self.anchors)
        )
        expected_anchor_ids = {target.target_id for target in targets[:-1]}
        if {anchor.target_id for anchor in anchors} != expected_anchor_ids or len(
            {anchor.target_id for anchor in anchors}
        ) != len(anchors):
            raise ValueError("anchors must identify every non-natural target exactly once")
        object.__setattr__(self, "anchors", anchors)

        from jasper.bass_extension.targets import MARGINS

        if self.margin not in MARGINS:
            raise ValueError("margin is unsupported")
        object.__setattr__(
            self, "digital_margin_db", _number(self.digital_margin_db, "digital_margin_db")
        )
        self._validate_evidence_summaries()
        for name in ("clean_ceiling", "sustain_test", "impedance_import"):
            object.__setattr__(self, name, _frozen_json(getattr(self, name), name))
        if self.status not in {"accepted", "bypassed"}:
            raise ValueError("status is unsupported")
        object.__setattr__(
            self,
            "profile_id",
            "bex-" + json_fingerprint(self._content_dict(), field_name="profile")[:12],
        )

    def _validate_evidence_summaries(self) -> None:
        ceiling = _object(
            self.clean_ceiling, "clean_ceiling", {"listening_level", "limited_by"}
        )
        if type(ceiling["listening_level"]) is not int or not 0 <= ceiling["listening_level"] <= 100:
            raise ValueError("clean_ceiling.listening_level must be an integer from 0 to 100")
        if ceiling["limited_by"] not in {
            "compression", "thd", "mic_clip", "digital", "operator_stop",
            "sustain_sag", "sustain_fc_shift",
        }:
            raise ValueError("clean_ceiling.limited_by is unsupported")
        if self.sustain_test is not None:
            sustain = _object(self.sustain_test, "sustain_test", {
                "duration_s", "fundamental_sag_db", "fc_shift_pct", "verdict",
            })
            for name in ("duration_s", "fundamental_sag_db", "fc_shift_pct"):
                _number(sustain[name], f"sustain_test.{name}")
            if sustain["verdict"] != "passed":
                raise ValueError("sustain_test.verdict is unsupported")
        if self.impedance_import is not None:
            impedance = _object(self.impedance_import, "impedance_import", {
                "source", "fc_hz", "qtc", "agreement_pct",
            })
            _text(impedance["source"], "impedance_import.source")
            for name in ("fc_hz", "qtc", "agreement_pct"):
                _number(impedance[name], f"impedance_import.{name}")

    def _content_dict(self) -> dict[str, Any]:
        return {
            "kind": BASS_EXTENSION_PROFILE_KIND,
            "schema_version": BASS_EXTENSION_SCHEMA_VERSION,
            "algorithm_version": self.algorithm_version,
            "baseline_fingerprint": self.baseline_fingerprint,
            "topology_id": self.topology_id,
            "topology_fingerprint": self.topology_fingerprint,
            "bass_owner": _json_value(self.bass_owner, "bass_owner"),
            "enclosure": _json_value(self.enclosure, "enclosure"),
            "mic_calibration_id": self.mic_calibration_id,
            "measurement_ids": [item.to_dict() for item in self.measurement_ids],
            "natural": _json_value(self.natural, "natural"),
            "targets": [_target_to_dict(item) for item in self.targets],
            "anchors": [_anchor_to_dict(item) for item in self.anchors],
            "margin": self.margin,
            "digital_margin_db": self.digital_margin_db,
            "clean_ceiling": _json_value(self.clean_ceiling, "clean_ceiling"),
            "sustain_test": _json_value(self.sustain_test, "sustain_test"),
            "impedance_import": _json_value(self.impedance_import, "impedance_import"),
            "status": self.status,
        }

    def to_dict(self) -> dict[str, Any]:
        content = self._content_dict()
        return {
            "kind": content.pop("kind"),
            "schema_version": content.pop("schema_version"),
            "profile_id": self.profile_id,
            "created_at": self.created_at,
            **content,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "BassExtensionProfile":
        raw = _object(value, "bass extension profile", _PROFILE_FIELDS)
        if raw["kind"] != BASS_EXTENSION_PROFILE_KIND:
            raise ValueError("bass extension profile kind is unsupported")
        if type(raw["schema_version"]) is not int or raw["schema_version"] != BASS_EXTENSION_SCHEMA_VERSION:
            raise ValueError("bass extension profile schema_version is unsupported")
        measurements = raw["measurement_ids"]
        targets = raw["targets"]
        anchors = raw["anchors"]
        if type(measurements) is not list:
            raise ValueError("measurement_ids must be a list")
        if type(targets) is not list:
            raise ValueError("targets must be a list")
        if type(anchors) is not list:
            raise ValueError("anchors must be a list")
        profile = cls(
            created_at=raw["created_at"],
            algorithm_version=raw["algorithm_version"],
            baseline_fingerprint=raw["baseline_fingerprint"],
            topology_id=raw["topology_id"],
            topology_fingerprint=raw["topology_fingerprint"],
            bass_owner=raw["bass_owner"],
            enclosure=raw["enclosure"],
            mic_calibration_id=raw["mic_calibration_id"],
            measurement_ids=tuple(ArtifactIdentity.from_mapping(item) for item in measurements),
            natural=raw["natural"],
            targets=tuple(_target_from_dict(item, index) for index, item in enumerate(targets)),
            anchors=tuple(_anchor_from_dict(item, index) for index, item in enumerate(anchors)),
            margin=raw["margin"],
            digital_margin_db=raw["digital_margin_db"],
            clean_ceiling=raw["clean_ceiling"],
            sustain_test=raw["sustain_test"],
            impedance_import=raw["impedance_import"],
            status=raw["status"],
        )
        if raw["profile_id"] != profile.profile_id:
            raise ValueError("bass extension profile_id does not match content")
        return profile


@dataclass(frozen=True)
class BassExtensionEvaluation:
    status: str
    refusals: tuple[BassExtensionRefusal, ...]
    profile: BassExtensionProfile | None
    detail: str


def _profile_path(path: str | Path | None) -> Path:
    return Path(path or os.environ.get(PROFILE_PATH_ENV) or DEFAULT_PROFILE_PATH)


def _read_profile(path: str | Path | None) -> BassExtensionEvaluation:
    target = _profile_path(path)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        profile = BassExtensionProfile.from_dict(raw)
    except FileNotFoundError:
        return BassExtensionEvaluation("missing", (), None, "profile is absent")
    except (
        OSError,
        UnicodeError,
        ValueError,
        TypeError,
        AttributeError,
        KeyError,
        IndexError,
    ) as exc:
        return BassExtensionEvaluation("malformed", (), None, str(exc))
    return BassExtensionEvaluation(profile.status, (), profile, "profile parsed")


def save_bass_extension_profile(
    profile: BassExtensionProfile,
    path: str | Path | None = None,
) -> None:
    if not isinstance(profile, BassExtensionProfile):
        raise ValueError("profile must be a BassExtensionProfile")
    atomic_write_text(
        _profile_path(path),
        json.dumps(profile.to_dict(), indent=2, sort_keys=True) + "\n",
        mode=0o640,
        group_from_parent=True,
        durable=True,
    )


def load_bass_extension_profile(
    path: str | Path | None = None,
) -> BassExtensionProfile | None:
    return _read_profile(path).profile


def evaluate_bass_extension_profile(
    *,
    path: str | Path | None = None,
    topology: Any,
    applied_baseline_state: Mapping[str, Any] | None,
) -> BassExtensionEvaluation:
    parsed = _read_profile(path)
    if parsed.profile is None:
        return parsed

    return evaluate_loaded_bass_extension_profile(
        parsed.profile,
        topology=topology,
        applied_baseline_state=applied_baseline_state,
    )


def evaluate_loaded_bass_extension_profile(
    profile: BassExtensionProfile,
    *,
    topology: Any,
    applied_baseline_state: Mapping[str, Any] | None,
) -> BassExtensionEvaluation:
    """Evaluate one already-parsed immutable profile without disk I/O."""

    from jasper.active_speaker.baseline_profile import (
        baseline_candidate_fingerprint,
        topology_config_fingerprint,
    )
    from jasper.bass_extension.adapters.base import ADAPTERS

    refusals: list[BassExtensionRefusal] = []
    mismatches: list[str] = []

    baseline_fingerprint = (
        baseline_candidate_fingerprint(applied_baseline_state)
        if isinstance(applied_baseline_state, Mapping)
        else None
    )
    if baseline_fingerprint != profile.baseline_fingerprint:
        refusals.append(BassExtensionRefusal.BASELINE_NOT_APPLIED)
        mismatches.append("baseline fingerprint mismatch")
    if (
        getattr(topology, "topology_id", None) != profile.topology_id
        or topology_config_fingerprint(topology) != profile.topology_fingerprint
    ):
        refusals.append(BassExtensionRefusal.TOPOLOGY_MISMATCH)
        mismatches.append("topology id/fingerprint mismatch")
    adapter = ADAPTERS.get(str(profile.enclosure["adapter_id"]))
    if adapter is None or profile.enclosure["adapter_version"] != adapter.adapter_version:
        refusals.append(BassExtensionRefusal.ENCLOSURE_UNSUPPORTED)
        mismatches.append("enclosure adapter version mismatch")
    if profile.algorithm_version != BASS_EXTENSION_ALGORITHM_VERSION:
        refusals.append(BassExtensionRefusal.PROFILE_STALE)
        mismatches.append("algorithm version mismatch")
    if refusals:
        return BassExtensionEvaluation(
            "stale", tuple(refusals), profile, "; ".join(mismatches)
        )
    if profile.status == "bypassed":
        return BassExtensionEvaluation("bypassed", (), profile, "profile is bypassed")
    return BassExtensionEvaluation("accepted", (), profile, "profile is accepted")


def bass_extension_state_summary(
    path: str | Path | None = None,
    *,
    intent_path: str | Path | None = None,
) -> dict[str, Any] | None:
    from jasper.bass_extension import (
        BASS_EXTENSION_APPLY_INTENT_PATH,
        BASS_EXTENSION_RUNTIME_ADAPTER_IDS,
    )

    recovery_required = Path(
        intent_path or BASS_EXTENSION_APPLY_INTENT_PATH
    ).exists()
    with suppress(Exception):
        profile = load_bass_extension_profile(path)
        if profile is None:
            if not recovery_required:
                return None
            return {
                "commissioned": False,
                "status": None,
                "profile_id": None,
                "adapter_id": None,
                "runtime_eligible": False,
                "runtime_deferred_reason": None,
                "apply_recovery_required": True,
            }
        adapter_id = str(profile.enclosure["adapter_id"])
        runtime_eligible = adapter_id in BASS_EXTENSION_RUNTIME_ADAPTER_IDS
        return {
            "commissioned": True,
            "status": profile.status,
            "profile_id": profile.profile_id,
            "adapter_id": adapter_id,
            "runtime_eligible": runtime_eligible,
            "runtime_deferred_reason": (
                None if runtime_eligible else "fixed_graph_not_defined"
            ),
            "apply_recovery_required": recovery_required,
            "deepest_hz": profile.targets[0].fp_hz,
            "natural_hz": profile.targets[-1].fp_hz,
            "margin": profile.margin,
            "anchors": [_anchor_to_dict(anchor) for anchor in profile.anchors],
        }
    return None
