# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Compare declared topology with observed hardware and match runtime children."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Mapping

from .audio_hardware.dac import (
    by_id as _dac_profile_by_id,
    clock_domain_contract_for as _dac_clock_domain_contract_for,
    kind_for,
    percent_pinned_control_for,
)
from .json_fields import issue as _issue
from .output_hardware import (
    ObservedOutput,
    OutputCardFact,
    OutputHardwareState,
    _text,
    apple_output_card_ids,
    detected_hardware_adoption_precondition,
    normalize_output_device_id,
)
from .output_topology import (
    APPLE_USB_C_DONGLE_DEVICE_ID,
    DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID,
    SCHEMA_VERSION,
    OutputChildDevice,
    OutputHardware,
    OutputTopology,
    OutputTopologyError,
    _dual_apple_clock_issues,
)
from .output_topology_store import load_output_topology, topology_path

CLOCK_DOMAIN_REPORT_KIND = "jts_output_clock_domain_report"


def _observed_dual_apple_hardware_issues(
    hardware: OutputHardware,
    observed: OutputHardwareState | None,
) -> list[dict[str, str]]:
    """Return blockers/warnings from current runtime hardware observation."""

    if observed is None:
        return [
            _issue(
                "blocker",
                "dual_apple_observation_missing",
                "current dual-Apple output hardware state has not been observed",
            )
        ]
    if observed.profile_id != DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID:
        return [
            _issue(
                "blocker",
                "dual_apple_observed_profile_mismatch",
                f"current output hardware is {observed.profile_id}, not "
                f"{DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID}",
            )
        ]

    issues: list[dict[str, str]] = []
    for raw_issue in observed.issues:
        severity = str(raw_issue.get("severity") or "warning")
        if severity not in {"blocker", "warning"}:
            severity = "warning"
        code = str(raw_issue.get("code") or "dual_apple_observed_issue")
        message = str(raw_issue.get("message") or "observed dual-Apple hardware issue")
        issues.append(_issue(severity, code, message))

    if observed.status != "ready" and not any(
        issue.get("severity") == "blocker" for issue in issues
    ):
        issues.append(_issue(
            "blocker",
            "dual_apple_observed_hardware_not_ready",
            f"current dual-Apple output hardware state is {observed.status}",
        ))

    topology_serials = {
        child.serial for child in hardware.child_devices if child.serial
    }
    observed_serials = {
        child.serial for child in observed.child_devices
        if child.device_id == APPLE_USB_C_DONGLE_DEVICE_ID and child.serial
    }
    if topology_serials:
        if len(observed_serials) != 2:
            issues.append(_issue(
                "blocker",
                "dual_apple_observed_serials_missing",
                "current dual-Apple hardware observation lacks two DAC serials",
            ))
        elif observed_serials != topology_serials:
            issues.append(_issue(
                "blocker",
                "dual_apple_observed_serial_mismatch",
                "current dual-Apple DAC serials do not match the saved topology",
            ))

    return issues


def clock_domain_report(
    topology: OutputTopology, observed: OutputHardwareState | None,
) -> dict[str, Any]:
    """Return clocking evidence for declared intent and one observed snapshot."""

    hardware = topology.hardware
    issues: list[dict[str, str]] = []
    notes: list[str]
    clock_contract = _dac_clock_domain_contract_for(hardware.device_id)
    if (
        clock_contract == "measured_sync_required"
        and hardware.device_id == DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID
    ):
        issues = _dual_apple_clock_issues(hardware)
        issues.extend(_observed_dual_apple_hardware_issues(hardware, observed))
        passed = not any(issue.get("severity") == "blocker" for issue in issues)
        status = (
            "dual_apple_composite_clock"
            if passed
            else "dual_apple_composite_clock_blocked"
        )
        notes = [
            "This is a constrained dual-DAC output profile, not generic ALSA aggregation.",
            "Each Apple DAC remains one speaker-local stereo device; JTS must own both sinks in one process.",
        ]
        return {
            "artifact_schema_version": SCHEMA_VERSION,
            "kind": CLOCK_DOMAIN_REPORT_KIND,
            "status": status,
            "clock_domain_id": hardware.clock_domain_id,
            "clock_domain_label": hardware.clock_domain_label,
            "clock_domain_count": len(hardware.child_devices) or 2,
            "coherent_physical_output_count": (
                hardware.physical_output_count if passed else 0
            ),
            "multi_device_aggregate_supported": False,
            "composite_clock_supported": passed,
            "future_multi_device_lab_path": not passed,
            "sound_tests_allowed": False,
            "issues": issues,
            "notes": notes,
            "child_devices": [
                child.to_dict() for child in hardware.child_devices
            ],
            "observed_hardware": (
                observed.to_dict()
                if observed is not None else None
            ),
            "recommendation": (
                "Proceed only through the measured dual-Apple active-output "
                "owner: one process opens both serial-pinned DACs, writes "
                "silence first, monitors xruns/delay/frame counts, and aborts "
                "both sinks on mismatch."
            ),
        }

    notes = [
        "All current physical outputs are assumed to belong to one output device clock domain.",
        "Multiple independent USB DACs are not aggregated by this topology contract.",
    ]
    if hardware.physical_output_count <= 0:
        status = "missing_hardware"
        issues.append(
            _issue(
                "blocker",
                "no_output_hardware",
                "no recognized output hardware is available",
            )
        )
    elif clock_contract is None:
        status = "unknown_device_clock"
        issues.append(
            _issue(
                "warning",
                "unknown_clock_domain",
                "output hardware clocking is not recognized by JTS",
            )
        )
    elif clock_contract == "single_device":
        status = "single_device_clock"
    elif clock_contract in {"independent", "measured_sync_required"}:
        status = "unsupported_clock_contract"
        issues.append(
            _issue(
                "warning",
                "unsupported_clock_contract",
                f"output hardware clock contract {clock_contract} is not supported",
            )
        )

    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": CLOCK_DOMAIN_REPORT_KIND,
        "status": status,
        "clock_domain_id": hardware.clock_domain_id,
        "clock_domain_label": hardware.clock_domain_label,
        "clock_domain_count": 1 if hardware.physical_output_count > 0 else 0,
        "coherent_physical_output_count": hardware.physical_output_count
        if status == "single_device_clock"
        else 0,
        "multi_device_aggregate_supported": False,
        "future_multi_device_lab_path": True,
        "sound_tests_allowed": False,
        "issues": issues,
        "notes": notes,
        "recommendation": (
            "Use one coherent multi-output DAC/interface for active crossover. "
            "Treat multiple USB DACs as future lab work until JTS can measure "
            "and compensate inter-device skew and drift."
        ),
    }


@dataclass(frozen=True)
class CompositeRepinPlan:
    """The number of composite children replaced by a same-shape re-pin."""

    child_count: int
    replaced_child_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "child_count": self.child_count,
            "replaced_child_count": self.replaced_child_count,
        }


def _composite_repin_pairs(
    topology: OutputTopology,
    observed: OutputHardwareState | None,
) -> tuple[tuple[str, OutputChildDevice, OutputCardFact], ...] | None:
    """Pair each saved composite child with the DAC now in its USB port.

    Returns ``None`` unless the attached hardware is the SAME SHAPE as the
    saved declaration — same profile, same physical output count, same child
    count, one child of the same kind per saved USB port — so the only thing
    that can differ is WHICH physical unit is plugged into each port. Whether
    the attached hardware is usable at all is
    ``detected_hardware_adoption_precondition``'s verdict, not this one's.

    The pairing anchor is ``usb_path`` — the sysfs port topology path, which
    survives a unit swap in the same port and is the first identity token the
    runtime child-order matcher tries
    (``dual_apple_runtime_mapping``). Serial cannot be
    the anchor: it is the thing that changed. A DAC moved to a DIFFERENT port
    is therefore not a re-pin — nothing then says which physical unit landed on
    which lanes, and the saved speaker/role assignment has no anchor to keep.

    ``physical_output_indexes`` are deliberately NOT compared against the
    observed projection: observed lane order follows ALSA enumeration, which is
    precisely what a saved topology exists to override. Those indexes are
    declaration, and a re-pin preserves them.
    """

    if observed is None:
        return None
    if not detected_hardware_adoption_precondition(observed)["allowed"]:
        return None
    hardware = topology.hardware
    # Two or more child DACs is what "composite" means in a saved topology
    # (mirrors ``active_speaker.output_contract.topology_sink_is_composite``);
    # a single-child DAC has no serial-keyed pairing contract to repair.
    if len(hardware.child_devices) < 2:
        return None
    if normalize_output_device_id(observed.profile_id) != hardware.device_id:
        return None
    if observed.physical_output_count != hardware.physical_output_count:
        return None
    if len(observed.child_devices) != len(hardware.child_devices):
        return None

    saved_by_port: dict[str, OutputChildDevice] = {}
    for child in hardware.child_devices:
        if not child.usb_path or child.usb_path in saved_by_port:
            return None
        saved_by_port[child.usb_path] = child
    attached_by_port: dict[str, OutputCardFact] = {}
    for card in observed.child_devices:
        if not card.usb_path:
            return None
        attached_by_port[card.usb_path] = card
    if set(saved_by_port) != set(attached_by_port):
        return None

    pairs: list[tuple[str, OutputChildDevice, OutputCardFact]] = []
    # Walks the saved child order (dict insertion order).
    for port, child in saved_by_port.items():
        card = attached_by_port[port]
        if card.device_id != child.device_id or not card.serial:
            return None
        pairs.append((port, child, card))
    return tuple(pairs)


def _composite_repin_plan(
    pairs: tuple[tuple[str, OutputChildDevice, OutputCardFact], ...],
) -> CompositeRepinPlan | None:
    """Project paired children into a plan, or ``None`` when nothing changed."""

    replaced = [
        child for _port, child, card in pairs if child.serial != card.serial
    ]
    if not replaced:
        return None
    return CompositeRepinPlan(
        child_count=len(pairs),
        replaced_child_count=len(replaced),
    )


def composite_serial_repin_plan(
    topology: OutputTopology,
    observed: OutputHardwareState | None,
) -> CompositeRepinPlan | None:
    """Return the same-shape re-pin available for ``topology``, if any.

    ``None`` means "not offerable": the attached hardware is a different shape,
    is not usable, or is the very same pair of units already pinned.
    """

    pairs = _composite_repin_pairs(topology, observed)
    if pairs is None:
        return None
    return _composite_repin_plan(pairs)


_OBSERVED_HARDWARE_CLOCK_ISSUE_CODES = frozenset({
    "dual_apple_observation_missing",
    "dual_apple_usb_topology_mismatch",
    "dual_apple_usb_topology_unknown",
    "dual_apple_stable_identity_missing",
    "dual_apple_endpoint_not_synchronous",
})


def _is_observed_hardware_clock_issue(code: str) -> bool:
    return (
        code.startswith("dual_apple_observed_")
        or code in _OBSERVED_HARDWARE_CLOCK_ISSUE_CODES
    )


def declared_hardware_mismatch(
    topology: OutputTopology,
    observed: OutputHardwareState | None,
) -> dict[str, Any] | None:
    """Compare declared hardware against the supplied observation snapshot.

    Adoption being allowed only proves the observed hardware is usable; this
    comparison says whether it differs from the declaration. A missing saved
    topology auto-seeds a draft from observation, so it can match without ever
    being saved. Callers needing that distinction must check the store's
    snapshot revision for "missing".
    """
    clock_blockers = [
        issue
        for issue in clock_domain_report(topology, observed).get("issues", [])
        if issue.get("severity") == "blocker"
        and _is_observed_hardware_clock_issue(str(issue.get("code") or ""))
    ]
    if observed is None and not clock_blockers:
        return None
    saved = topology.hardware
    saved_id = saved.device_id
    current_id = observed.profile_id if observed is not None else ""
    saved_count = saved.physical_output_count
    current_count = observed.physical_output_count if observed is not None else 0
    id_mismatch = bool(saved_id and current_id and saved_id != current_id)
    count_mismatch = saved_count != current_count
    if not id_mismatch and not count_mismatch and not clock_blockers:
        return None
    saved_label = saved.device_label or saved.device_id or "Saved hardware"
    current_label = (
        (observed.profile_label or observed.profile_id)
        if observed is not None
        else "Attached hardware"
    )
    current_summary = (
        f"currently attached hardware is {current_label} "
        f"({current_count} physical output{'' if current_count == 1 else 's'})"
        if observed is not None
        else "current output hardware has not been observed"
    )
    blocker_messages = [
        str(issue.get("message") or "")
        for issue in clock_blockers
        if issue.get("message")
    ]
    message = (
        f"Saved topology expects {saved_label} "
        f"({saved_count} physical output{'' if saved_count == 1 else 's'}), "
        f"but {current_summary}."
    )
    if blocker_messages:
        message = f"{message} {' '.join(blocker_messages)}"
    return {
        "saved_label": saved_label,
        "current_label": current_label,
        "saved_count": saved_count,
        "current_count": current_count,
        "clock_blockers": clock_blockers,
        "message": message,
    }


def repin_composite_child_serials(
    topology: OutputTopology,
    observed: OutputHardwareState | None,
) -> OutputTopology:
    """Return a copy pinned to the units now attached, keeping the design.

    The NARROW counterpart to :func:`new_topology_draft`'s wipe: the design a
    swapped dongle cannot invalidate survives, and only each child's observed
    physical identity is rewritten.

    Raises ``OutputTopologyError`` when the attached hardware is not a
    same-shape re-pin — callers offer this only after
    :func:`composite_serial_repin_plan` returns a plan.
    """

    pairs = _composite_repin_pairs(topology, observed)
    plan = _composite_repin_plan(pairs) if pairs is not None else None
    if pairs is None or plan is None:
        raise OutputTopologyError(
            "attached output hardware is not a same-shape re-pin of the saved "
            "speaker setup"
        )
    composed = replace(
        topology.hardware,
        child_devices=tuple(
            replace(
                child,
                serial=card.serial,
                card_id=card.card_id,
                stable_path=card.stable_path,
                controller=card.controller,
            )
            for _port, child, card in pairs
        ),
    )
    # Round-trip through the artifact contract before it can be persisted: the
    # rewritten fields are observed strings from the reconciler, and `replace`
    # bypasses every check `from_mapping` owns (id shape, length caps, lane
    # coverage).
    hardware = OutputHardware.from_mapping(composed.to_dict())
    return replace(topology, hardware=hardware)


@dataclass(frozen=True)
class DualAppleRuntimeMapping:
    """Runtime child-device order for the dual-Apple outputd sink."""

    ok: bool
    reason: str
    order_source: str = ""
    child_devices: tuple[OutputCardFact, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "ok": self.ok,
            "reason": self.reason,
            "order_source": self.order_source,
            "pcms": [
                child.pcm or f"hw:CARD={child.card_id},DEV=0"
                for child in self.child_devices
            ],
            "child_devices": [child.to_dict() for child in self.child_devices],
        }
        return out


def observed_output(
    state: OutputHardwareState,
    cards: tuple[OutputCardFact, ...],
    *,
    record_changed: bool = False,
) -> ObservedOutput:
    """The classifier's verdict as the one typed observation both readers share."""
    usb = state.usb_data_role
    mapping = dual_apple_runtime_mapping(state)
    # Padded so an absent or partial composite still answers both PCM keys.
    pcms = [child.pcm or "" for child in mapping.child_devices] + ["", ""]
    return ObservedOutput(
        profile_id=state.profile_id,
        status=state.status,
        kind=kind_for(state.profile_id) or "",
        headphone_control=(
            percent_pinned_control_for(state.active_profile_id or "") or ""
        ),
        selected_card_id=state.selected_card_id or "",
        child_device_ids=tuple(child.device_id for child in state.child_devices),
        apple_card_ids=tuple(apple_output_card_ids(cards)),
        blocker_codes=tuple(
            str(issue.get("code") or "unnamed")
            for issue in state.issues
            if issue.get("severity") == "blocker"
        ),
        record_changed=record_changed,
        management_transport_available=(
            usb.management_transport_available if usb else None
        ),
        dual_mapping_ok=mapping.ok,
        dual_mapping_reason=mapping.reason,
        dual_order_source=mapping.order_source,
        dual_dac_a_pcm=pcms[0],
        dual_dac_b_pcm=pcms[1],
    )


def _identity_tokens(raw: Any) -> tuple[tuple[str, str], ...]:
    if isinstance(raw, OutputCardFact):
        values = {
            "serial": raw.serial,
            "stable_path": raw.stable_path,
            "usb_path": raw.usb_path,
        }
    elif isinstance(raw, Mapping):
        values = {
            "serial": raw.get("serial"),
            "stable_path": raw.get("stable_path"),
            "usb_path": raw.get("usb_path"),
        }
    else:
        values = {}
    tokens: list[tuple[str, str]] = []
    for key in ("serial", "stable_path", "usb_path"):
        value = _text(values.get(key))
        if value:
            tokens.append((key, value))
    return tuple(tokens)


def _runtime_identity_candidates(raw: Any) -> tuple[tuple[str, str], ...]:
    tokens = dict(_identity_tokens(raw))
    return tuple(
        (key, value)
        for key in ("usb_path", "stable_path", "serial")
        if (value := tokens.get(key))
    )


def _read_topology_hardware(
    path: str | Path | None = None,
) -> tuple[bool, Mapping[str, Any] | None]:
    """Read saved hardware, distinguishing absent intent from corrupt intent.

    Missing intent permits observed child order; corrupt intent refuses dual
    runtime mapping. The saved-topology parking policy deliberately leaves
    both alone instead of silencing every box with malformed intent.
    """

    target = topology_path(path)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return (False, None)
    except (OSError, json.JSONDecodeError):
        return (True, None)
    if not isinstance(raw, Mapping):
        return (True, None)
    hardware = raw.get("hardware")
    if not isinstance(hardware, Mapping):
        return (True, None)
    return (True, hardware)


def _load_dual_apple_topology_children(
    path: str | Path | None = None,
) -> tuple[Mapping[str, Any], ...] | None:
    exists, hardware = _read_topology_hardware(path)
    if not exists:
        return None
    if hardware is None:
        return ()
    device_id = normalize_output_device_id(_text(hardware.get("device_id")))
    if device_id != DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID:
        return None
    children = hardware.get("child_devices") or []
    if not isinstance(children, list):
        return ()
    out = [item for item in children if isinstance(item, Mapping)]
    return tuple(sorted(out, key=_child_topology_order))


def _child_topology_order(child: Mapping[str, Any]) -> int:
    indexes = child.get("physical_output_indexes") or []
    values: list[int] = []
    if isinstance(indexes, list):
        for item in indexes:
            try:
                values.append(int(item))
            except (TypeError, ValueError):
                pass
    return min(values) if values else 999


def _declared_child_devices(
    hardware: Mapping[str, Any],
) -> tuple[Mapping[str, Any], ...]:
    """The child devices the saved topology declares, if it declares any."""

    children = hardware.get("child_devices") or []
    if not isinstance(children, list):
        return ()
    return tuple(item for item in children if isinstance(item, Mapping))


def _missing_topology_child_labels(
    hardware: Mapping[str, Any],
    observed: Iterable[OutputCardFact],
) -> tuple[str, ...]:
    """Name the saved child devices that no observed card accounts for."""

    observed_tokens: set[tuple[str, str]] = set()
    for card in observed:
        observed_tokens.update(_identity_tokens(card))
    missing: list[str] = []
    for child in _declared_child_devices(hardware):
        tokens = _identity_tokens(child)
        if tokens and observed_tokens.intersection(tokens):
            continue
        missing.append(
            _text(child.get("serial"))
            or _text(child.get("child_id"))
            or _text(child.get("card_id"))
            or "unidentified child"
        )
    return tuple(missing)


def _saved_topology_requires_roleful_graph(
    path: str | Path | None = None,
) -> bool:
    """Ask the runtime owner whether saved intent needs per-driver DSP.

    Malformed intent deliberately does not park: the store returns an empty
    draft, which is not roleful. Broadening this would silence boxes beyond
    the partly present composite this policy covers.
    """

    from .active_speaker.output_contract import active_topology_requires_roleful_graph  # lazy: import cost only on a composite mismatch

    return active_topology_requires_roleful_graph(load_output_topology(path))


def apply_saved_topology_policy(
    state: OutputHardwareState,
    observed_cards: Iterable[OutputCardFact],
    *,
    topology_path: str | Path | None = None,
) -> OutputHardwareState:
    """Fail closed when a saved ROLEFUL composite is only partly present.

    A survivor can classify as ordinary stereo, but saved driver assignments
    still require the roleful graph. A passive composite can put all speakers
    on one child, so compositeness alone must not park it. Malformed intent
    also leaves classification alone. Unreadable saved hardware separately
    refuses dual runtime mapping.
    """

    exists, hardware = _read_topology_hardware(topology_path)
    if not exists or hardware is None:
        return state
    declared_id = normalize_output_device_id(_text(hardware.get("device_id")))
    declared = _dac_profile_by_id(declared_id)
    if declared is None or declared.kind != "composite":
        return state
    if state.profile_id == declared_id:
        return state
    if not _saved_topology_requires_roleful_graph(topology_path):
        return state

    # Diffed against every observed CARD, not against the classified record's
    # children: a record can carry children that are not the Apple pair at all
    # (attach a registered single DAC beside both dongles and the record's
    # children are that DAC), which would report a plugged-in child missing.
    # The one surface this policy exists to produce must not misname hardware.
    missing = _missing_topology_child_labels(hardware, observed_cards)
    if missing:
        detail = f"missing child devices: {', '.join(missing)}"
    elif not _declared_child_devices(hardware):
        detail = "the saved topology declares no child devices to look for"
    else:
        # Every declared child IS attached. Reaching here at all means
        # classification went elsewhere, and it can only do that when other
        # output hardware is also attached — two Apple children alone classify
        # as the composite and return above. So the remediation is to remove
        # the interloper, not to reconnect anything.
        detail = (
            "every declared child device is attached; other output hardware is "
            "also present and was classified first — detach it so the declared "
            "children are recognized as one device"
        )
    return replace(
        state,
        # An already-partial/missing observation keeps its own status; only a
        # "ready" one is downgraded, because "ready" is the single word every
        # consumer reads as "safe to drive this speaker with".
        status="partial" if state.status == "ready" else state.status,
        issues=state.issues + (
            _issue(
                "blocker",
                "saved_composite_partially_present",
                f"saved topology declares the composite {declared.label} "
                f"({declared.physical_output_count} outputs) but observed output "
                f"hardware is {state.profile_id}; {detail}",
            ),
        ),
    )


def dual_apple_runtime_mapping(
    state: OutputHardwareState,
    *,
    topology_path: str | Path | None = None,
) -> DualAppleRuntimeMapping:
    """Return the child order outputd should use for the dual-Apple sink.

    A saved speaker topology pins which physical dongle owns lanes 0/1 and
    2/3. ALSA card order is only safe before that topology exists.
    """

    if state.profile_id != DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID:
        return DualAppleRuntimeMapping(False, "not_dual_apple_profile")
    if state.status != "ready":
        return DualAppleRuntimeMapping(False, f"profile_status_{state.status}")
    if len(state.child_devices) != 2:
        return DualAppleRuntimeMapping(False, "expected_two_child_devices")
    if any(not child.pcm for child in state.child_devices):
        return DualAppleRuntimeMapping(False, "missing_child_pcm")

    topology_children = _load_dual_apple_topology_children(topology_path)
    if topology_children is None:
        return DualAppleRuntimeMapping(
            True,
            "ok",
            "observed_hardware",
            state.child_devices,
        )
    if not topology_children:
        return DualAppleRuntimeMapping(False, "saved_topology_unreadable")
    if len(topology_children) != 2:
        return DualAppleRuntimeMapping(False, "saved_topology_expected_two_children")

    remaining = list(state.child_devices)
    ordered: list[OutputCardFact] = []
    for topology_child in topology_children:
        candidates = _runtime_identity_candidates(topology_child)
        if not candidates:
            return DualAppleRuntimeMapping(
                False,
                "saved_topology_child_identity_missing",
            )
        match = None
        for token in candidates:
            matches = [
                child
                for child in remaining
                if token in _identity_tokens(child)
            ]
            if len(matches) == 1:
                match = matches[0]
                break
        if match is None:
            return DualAppleRuntimeMapping(
                False,
                "saved_topology_child_identity_mismatch",
            )
        ordered.append(match)
        remaining.remove(match)

    if ordered[0].pcm == ordered[1].pcm:
        return DualAppleRuntimeMapping(False, "duplicate_child_pcm")
    return DualAppleRuntimeMapping(True, "ok", "saved_topology", tuple(ordered))
