# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shape pin for the USB resampler warm/cold verdict logic.

Feeds `warm_verdict` fake fan-in STATUS dicts (never a real socket) so the
verdict's three conditions -- locked, held at the 576-frame floor, decay
frozen at "at_floor" -- and the lane-matching rule are pinned independent of
any running daemon.
"""
from __future__ import annotations

import pytest

from jasper.route_latency import warm_check


def _status(*, label="usbsink", lane="usbsink", locked=True, held=576, frozen_reason="at_floor"):
    return {
        "inputs": [
            {
                "label": label,
                "lane": lane,
                "resampler": {
                    "locked": locked,
                    "held_target_frames": held,
                    "decay": {"frozen_reason": frozen_reason},
                },
            }
        ]
    }


def test_warm_verdict_true_at_the_churn_safe_floor():
    verdict = warm_check.warm_verdict(_status())

    assert verdict.warm is True
    assert verdict.to_status_dict() == {
        "locked": True,
        "held": 576,
        "frozen_reason": "at_floor",
        "warm": True,
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"locked": False},
        {"held": 2048},  # still descending from the cold-start ceiling
        {"held": None},  # resampler section missing the field entirely
        {"frozen_reason": "prime_hold"},  # a compliance proof is live but decay hasn't settled
        {"frozen_reason": ""},  # actively decaying, not frozen at all
    ],
    ids=["unlocked", "wrong_floor", "missing_floor", "prime_hold", "actively_decaying"],
)
def test_warm_verdict_false_when_any_condition_fails(overrides):
    verdict = warm_check.warm_verdict(_status(**overrides))

    assert verdict.warm is False


def test_find_usb_lane_matches_by_label():
    status = _status(label="usbsink", lane="something_else")

    assert warm_check.find_usb_lane(status)["label"] == "usbsink"


def test_find_usb_lane_matches_by_lane_substring():
    status = _status(label="not_usbsink", lane="usb_direct")

    assert warm_check.find_usb_lane(status)["lane"] == "usb_direct"


def test_find_usb_lane_raises_when_no_lane_matches():
    status = {"inputs": [{"label": "spotify", "lane": "spotify"}]}

    with pytest.raises(LookupError):
        warm_check.find_usb_lane(status)


def test_find_usb_lane_raises_when_inputs_missing():
    with pytest.raises(LookupError):
        warm_check.find_usb_lane({})
