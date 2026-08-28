# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shipped chip-AEC alignment proofs, keyed by hardware class.

Static data, IO-free like ``jasper.audio_hardware.dac``: each row banks the
``K`` and ``SYS_DELAY`` one commissioned box measured for a hardware class, so
a fresh install on recognized hardware starts from that proof and discloses
instead of parking on an absent artifact (ADR-0101, #2984).

Rows are harvested off commissioned hardware with
``jasper-aec-commission --emit-class-entry``, which prints one ready to paste.
A per-unit artifact, once commissioned, always wins over a row here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from jasper.chip_aec_alignment import (
    HARDWARE_CLASS_IDENTITY_FIELDS,
    AlignmentIdentity,
    hardware_class_identity,
    hardware_class_key,
    identity_divergence,
    validate_banked_delays,
)


@dataclass(frozen=True)
class ShippedAlignment:
    """One hardware class's alignment, as measured on a commissioned box."""

    label: str
    # Exactly HARDWARE_CLASS_IDENTITY_FIELDS: the alignment identity minus
    # the per-unit fields and the recorded-only forensics fields — just the
    # 8 that name the hardware class. K is a property of the class, so a row
    # that carried a serial would claim a proof it cannot transfer.
    identity: Mapping[str, Any]
    k_samples: int
    sys_delay: int
    # Derived from `identity` where the row is validated, so a lookup over the
    # registry does not rebuild them per row. Out of == and repr: they say
    # nothing `identity` does not.
    class_identity: AlignmentIdentity = field(init=False, repr=False, compare=False)
    class_key: tuple[Any, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.label.strip():
            raise ValueError("shipped alignment label is required")
        validate_banked_delays(self.k_samples, self.sys_delay)
        object.__setattr__(
            self, "class_identity", hardware_class_identity(self.identity)
        )
        object.__setattr__(
            self, "class_key", hardware_class_key(self.class_identity)
        )

    def divergence(self, identity: AlignmentIdentity) -> tuple[str, ...]:
        """Return the class fields this row disagrees with a live box on."""

        return identity_divergence(
            self.class_identity, identity, fields=HARDWARE_CLASS_IDENTITY_FIELDS
        )


# Harvested rows are pasted here.
REGISTRY: tuple[ShippedAlignment, ...] = ()


def _refuse_duplicate_classes(entries: Sequence[ShippedAlignment]) -> None:
    """Refuse two rows for one class; the second would shadow the first.

    Checked at import, where the DAC profile registry refuses a duplicate id.
    """

    if len({entry.class_key for entry in entries}) != len(entries):
        raise ValueError("two shipped alignment rows share one hardware class")


_refuse_duplicate_classes(REGISTRY)


def for_identity(identity: AlignmentIdentity) -> ShippedAlignment | None:
    """Return what this box's hardware class ships, or None if unrecognized."""

    key = hardware_class_key(identity)
    return next((entry for entry in REGISTRY if entry.class_key == key), None)


def render_entry(identity: AlignmentIdentity, k_samples: int, sys_delay: int) -> str:
    """Render one commissioned alignment as a REGISTRY entry, ready to paste.

    The per-unit and recorded-only fields are dropped here — that stripping,
    down to exactly the 8 HARDWARE_CLASS_IDENTITY_FIELDS, is what makes the
    proof transferable to a sibling box.  The label is a starting point for
    whoever pastes it, not derived data anything reads back.
    """

    fields = "".join(
        f"        {name!r}: {getattr(identity, name)!r},\n"
        for name in HARDWARE_CLASS_IDENTITY_FIELDS
    )
    label = f"{identity.xvf_variant} on {identity.output_id}"
    return (
        "ShippedAlignment(\n"
        f"    label={label!r},\n"
        "    identity={\n"
        f"{fields}"
        "    },\n"
        f"    k_samples={k_samples},\n"
        f"    sys_delay={sys_delay},\n"
        ")"
    )
