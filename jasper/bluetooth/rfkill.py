# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Linux RF-kill state for Bluetooth radios."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BluetoothRfkillState:
    """Observed Linux RF-kill state for Bluetooth radios."""

    present: bool
    # ``soft_blocked`` means any Bluetooth RF-kill entry is blocked, which is
    # the correct On-path warning. ``all_soft_blocked`` proves the stronger Off
    # invariant when more than one adapter exists; ``None`` preserves the
    # single-adapter/test construction contract.
    soft_blocked: bool
    hard_blocked: bool
    all_soft_blocked: bool | None = None

    @property
    def fully_soft_blocked(self) -> bool:
        if self.all_soft_blocked is None:
            return self.soft_blocked
        return self.all_soft_blocked


def read_bluetooth_rfkill_state() -> BluetoothRfkillState:
    present = False
    soft_states: list[bool] = []
    hard_blocked = False
    for entry in Path("/sys/class/rfkill").glob("rfkill*"):
        try:
            if (entry / "type").read_text(encoding="utf-8").strip() != "bluetooth":
                continue
            present = True
            soft_states.append(
                (entry / "soft").read_text(encoding="utf-8").strip() == "1"
            )
            hard_blocked = hard_blocked or (
                (entry / "hard").read_text(encoding="utf-8").strip() == "1"
            )
        except OSError as exc:
            raise RuntimeError(f"cannot read Bluetooth RF-kill state: {exc}") from exc
    return BluetoothRfkillState(
        present,
        any(soft_states),
        hard_blocked,
        all(soft_states) if soft_states else False,
    )
