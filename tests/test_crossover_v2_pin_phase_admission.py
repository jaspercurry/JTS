# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pin each phase's linearity refusal and each owner's admission site."""

from jasper.active_speaker.crossover_v2 import refusal_copy
from jasper.active_speaker.crossover_v2.refusal_copy import REASON_AGC_BEHAVIORAL_FAIL


def test_checks_own_linearity_rule_is_deliberately_not_the_plain_one():
    assert refusal_copy.REASON_NOISY_ROOM_LINEARITY != REASON_AGC_BEHAVIORAL_FAIL
    assert refusal_copy.REASON_NOISY_ROOM_LINEARITY in refusal_copy.REASON_REGISTRY
