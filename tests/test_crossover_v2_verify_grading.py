# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""How a VERIFY refusal says what it knows."""

from __future__ import annotations

from jasper.active_speaker.crossover_v2.refusal_copy import REASON_REGISTRY


def test_verify_level_shift_copy_is_true_on_both_surfaces():
    """#1924's routing half. One string renders on the measurement page's
    in-session retry (which re-compares the same reference and CAN repeat)
    and on the wizard's fresh-session retry (which since #1927 settles it in
    one capture). So it must command neither and discredit neither: state the
    fact, contextualize the retry, name the escalation conditionally."""
    message = REASON_REGISTRY["verify_level_shift"].message
    assert message == (
        "The microphone's levels changed between measurements, so this check "
        "couldn't settle. Try again — if it repeats, re-measure."
    )
    # The retired routing: it commanded the retry the phone cannot win.
    assert "re-verify" not in message.lower()
    # The visible primary is named, not undermined.
    assert "Try again" in message
    # …and the escalation is conditional on the retry repeating, never
    # presented as the only way forward.
    assert "if it repeats, re-measure" in message
