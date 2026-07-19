# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "docs" / "HANDOFF-bass-extension-plan.md"


def test_bass_extension_plan_pins_merged_waves_and_remaining_gate() -> None:
    text = PLAN.read_text(encoding="utf-8")

    for merge_sha in (
        "0670540654a6684f8ac98fb2e70b2e643d65d82f",
        "9f39c70e418cf64316c23de535f322d21f825c8e",
        "bb2919383b408d630f9d70ef24c14fe38ca98be0",
    ):
        assert merge_sha in text

    assert "The transaction has\n> no production caller" in text
    assert "crossover-program hardware burn-in prerequisite is **met**" in text
    assert "contract rev 7 freezes the limiter bench protocol" in text
    assert "reviewed bench runner/temporary activation owner" in text
    assert "runtime/audio emission has not shipped" not in text
    assert "hardware-unvalidated" not in text
