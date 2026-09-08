# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pure decoding of the graph/profile records shared by recovery and evidence."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping

import yaml

from jasper.audio_measurement.evidence_identity import (
    ExactDspStateIdentity,
    NormalizedActiveRawIdentity,
)


@dataclass(frozen=True)
class ApplyIntent:
    config_path: str
    mode: int
    predecessor_graph_bytes: bytes
    predecessor_graph_fingerprint: str
    desired_graph_fingerprint: str
    predecessor_profile_bytes: bytes | None
    desired_profile_bytes: bytes


def _profile_bytes(value: Any) -> bytes | None:
    if not isinstance(value, Mapping) or type(value.get("present")) is not bool:
        raise ValueError
    text = value.get("bytes")
    digest = value.get("sha256")
    if value["present"] is False:
        if text is not None or digest is not None:
            raise ValueError
        return None
    if not isinstance(text, str):
        raise ValueError
    raw = text.encode("utf-8")
    if digest != hashlib.sha256(raw).hexdigest():
        raise ValueError
    return raw


def decode_apply_intent(value: Any) -> ApplyIntent:
    """Validate recorded content, raising ValueError for malformed intent."""
    try:
        if (
            not isinstance(value, Mapping)
            or value.get("kind") != "jts_bass_extension_apply_intent"
            or type(value.get("schema_version")) is not int
            or value.get("schema_version") != 1
        ):
            raise ValueError
        config, profiles, graphs = (value[key] for key in ("config", "profiles", "graphs"))
        operation_id = value.get("operation_id")
        if (
            not all(isinstance(part, Mapping) for part in (config, profiles, graphs))
            or not isinstance(operation_id, str)
            or len(operation_id) != 32
            or any(ch not in "0123456789abcdef" for ch in operation_id)
        ):
            raise ValueError
        ExactDspStateIdentity.from_mapping(value.get("predecessor_identity"))
        config_path, mode = config["path"], config["mode"]
        if (
            not isinstance(config_path, str)
            or not config_path
            or config_path.strip() != config_path
            or type(mode) is not int
            or not 0 <= mode <= 0o7777
            or value.get("boot_selector_target") != config_path
        ):
            raise ValueError
        graph_bytes, fingerprints = {}, {}
        for role in ("predecessor", "desired"):
            text = config[f"{role}_bytes"]
            if not isinstance(text, str):
                raise ValueError
            raw = text.encode("utf-8")
            parsed = yaml.safe_load(text)
            if not isinstance(parsed, dict) or not parsed:
                raise ValueError
            fingerprint = NormalizedActiveRawIdentity(parsed).active_raw_fingerprint
            if (
                config.get(f"{role}_sha256") != hashlib.sha256(raw).hexdigest()
                or graphs.get(role) != fingerprint
            ):
                raise ValueError
            graph_bytes[role], fingerprints[role] = raw, fingerprint
        predecessor = _profile_bytes(profiles.get("predecessor"))
        desired = _profile_bytes(profiles.get("desired"))
        if desired is None:
            raise ValueError
    except (KeyError, TypeError, ValueError, RecursionError, yaml.YAMLError) as exc:
        raise ValueError("pending bass-extension intent content is invalid") from exc
    return ApplyIntent(
        config_path=config_path,
        mode=mode,
        predecessor_graph_bytes=graph_bytes["predecessor"],
        predecessor_graph_fingerprint=fingerprints["predecessor"],
        desired_graph_fingerprint=fingerprints["desired"],
        predecessor_profile_bytes=predecessor,
        desired_profile_bytes=desired,
    )
