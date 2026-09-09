# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The campaign's dependency wiring: the two facts it proves before a device.

The composition itself is on-device (a fader owner, a wired mic, a held
session); what is provable hardware-free is what it refuses and how it routes.
"""

from __future__ import annotations

from typing import Any

import pytest

from jasper.bass_extension.bench.compose import (
    REFUSE_LIVE_PASS,
    _CampaignExecutor,
    commanded_level_db,
)
from jasper.bass_extension.bench.runner import BenchRefused, TargetPlan
from tests.test_bass_extension_bench_cli import _inputs


def _manifest(**overrides: Any):
    from jasper.bass_extension.bench.manifest import author_campaign_manifest

    inputs = _inputs("natural")
    inputs["requests"]["natural"]["sustain_stress"].update(overrides)
    return author_campaign_manifest(inputs, target_ids=("natural",))


def _plan(target_id: str) -> TargetPlan:
    return TargetPlan(
        target_id=target_id,
        target_fingerprint="f" * 64,
        graph_raw_text="",
        limiter_name="l",
        owner_channels=(0,),
        profile_summary={},
        baseline_clip_limit_dbfs=-12.0,
        boost_headroom_db=0.0,
    )


@pytest.mark.parametrize(
    ("overrides", "session_volume_db"),
    [
        # Two commanded levels: no campaign to open one session volume for.
        ({"requested_commanded_main_volume_db": -20.0}, -35.0),
        # One level, but not the one this speaker's session resolves at.
        ({}, -20.0),
    ],
)
def test_a_level_this_speaker_is_not_open_at_is_refused(
    overrides: dict[str, Any], session_volume_db: float
) -> None:
    """The play seam proves every stimulus against BOTH agreements; a campaign
    that cannot satisfy them is refused before anything is claimed."""

    from types import SimpleNamespace

    with pytest.raises(BenchRefused) as raised:
        commanded_level_db(
            _manifest(**overrides),
            SimpleNamespace(session_volume_db=session_volume_db),
        )

    assert raised.value.reason == "bench_commanded_volume_mismatch"


def test_the_commanded_level_is_the_one_every_request_agrees_on() -> None:
    from types import SimpleNamespace

    assert commanded_level_db(
        _manifest(), SimpleNamespace(session_volume_db=-35.0)
    ) == pytest.approx(-35.0)


async def test_a_live_pass_failure_ends_that_target_as_the_benchs_refusal() -> None:
    """A proof / derivation / render failure is the runner's ``refused`` arm —
    the campaign keeps the target's partials and runs the next one — never a
    traceback out of the whole campaign."""

    from jasper.bass_extension.bench.derivation import DerivationError

    class _Executor:
        def __init__(self, raises: Exception | None) -> None:
            self.raises = raises
            self.seen: list[str] = []

        async def run_discovery(self, *, target: TargetPlan, **kwargs: Any) -> str:
            self.seen.append(target.target_id)
            if self.raises is not None:
                raise self.raises
            return "ran"

    good, bad = _Executor(None), _Executor(DerivationError("no owner step"))
    dispatch = _CampaignExecutor({"natural": good, "t31.86": bad})

    assert await dispatch.run_discovery(target=_plan("natural"), sink=None) == "ran"
    assert good.seen == ["natural"] and bad.seen == []

    with pytest.raises(BenchRefused) as raised:
        await dispatch.run_discovery(target=_plan("t31.86"), sink=None)

    assert raised.value.reason == REFUSE_LIVE_PASS
    assert "DerivationError" in raised.value.detail
