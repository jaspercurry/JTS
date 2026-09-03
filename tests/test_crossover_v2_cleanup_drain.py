# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""A raising persist still gives the fader back — BOTH capture providers.

Every terminal arm of both runners records the failure to durable state and
THEN drains the measurement volume. The persist ends in ``save_v2_state`` ->
``atomic_write_text``, so disk pressure (ENOSPC, EROFS) raises ``OSError`` out
of it, and an unprotected raise skipped the drain entirely. The stranded
``SESSION_MEASUREMENT`` claim has NO TTL and all three out-of-runner drains
gate on ``VolumeOwner.holds_kind``, so the leak wedges the measurement pause
until the process restarts.

Pinned at the PROVIDER altitude with the OWNER's own answer as the
observable: a real :class:`~jasper.volume_owner.VolumeOwner` holds a real
measurement claim, the hooks release it the way production's
``_abandon``/``_close`` do, and after the arm the owner must report it gone.
One row per protected call site — the post-walk rows split by ``done``
because that site's ``finally`` drains through ``close`` on one branch and
``abandon`` on the other.

Cross-provider by construction: the guarantee is one rule stated at seven
sites across two files, and a per-provider example cluster would let one file
drift from the other silently.
"""
from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from jasper.active_speaker.crossover_v2.capture_source import (
    CaptureBeginRefused,
    CaptureFailed,
)
from jasper.active_speaker.crossover_v2.journey import PHASE_DONE
from jasper.active_speaker.crossover_v2.refusal_copy import (
    REASON_SESSION_CEILING_EXPIRED,
)
from jasper.audio_measurement.wired_capture import WiredMicDevice
from jasper.capture_protocol import CapturePlan
from jasper.capture_relay import session as relay_session
from jasper.volume_owner import ClaimKind, VolumeOwner
from jasper.web import correction_crossover_v2 as v2host
from jasper.web import correction_crossover_v2_relay as v2relay
from jasper.web import correction_crossover_v2_wired as v2wired

MEASUREMENT_DB = -12.5

#: What a full disk raises out of ``atomic_write_text``. Identity-checked in
#: the assertion, so a row dying of some OTHER OSError cannot pass as this one.
DISK_FULL = OSError(28, "No space left on device")


class _Fader:
    """A compliant fader: every write reads back exactly."""

    def __init__(self, db: float = -30.0) -> None:
        self.db = db

    async def set(self, db: float) -> bool:
        self.db = float(db)
        return True

    async def get(self) -> float:
        return self.db


class _Conductor:
    """The least conductor either runner asks questions of."""

    def __init__(self, *, done: bool = False, awaiting: Any = False) -> None:
        self.session_id = "drain-test"
        self.last_failure_code = None
        self.relay_published_refusal = False
        self.armed_capture = None
        self.accepted_phases: set[str] = set()
        self._done = done
        self._awaiting = awaiting

    @property
    def current_phase(self) -> str:
        return PHASE_DONE if self._done else "check"

    def cloud_measure_group_awaiting_confirm(self) -> bool:
        if isinstance(self._awaiting, Exception):
            raise self._awaiting
        return bool(self._awaiting)


def _session() -> SimpleNamespace:
    """A no-capture plan: every row reaches its arm without a take."""
    return SimpleNamespace(
        session_id="drain-test",
        pull_token="tok",
        spec=SimpleNamespace(
            capture_plan=CapturePlan(
                capture_target=0, max_attempts=1,
                schema_version=2, entries=(),
            ),
            sample_rate_hz=48000,
        ),
    )


@dataclass(frozen=True)
class _Row:
    """One arm of one provider, and how to steer a runner into it."""

    id: str
    #: Which host persist the arm calls — the one this row makes raise.
    persist: str
    drive: Callable[[Any, Any], Any]


def _relay_row(id: str, persist: str, *, raises=None, done: bool = False) -> _Row:
    async def _drive(hooks: Any, monkeypatch: Any) -> None:
        def _plan(*a: Any, **k: Any) -> None:
            if raises is not None:
                raise raises

        # The runner late-imports run_capture_plan from this module, so the
        # patch lands however early the builder ran.
        monkeypatch.setattr(relay_session, "run_capture_plan", _plan)
        runner = v2relay.build_v2_run_and_consume(
            _Conductor(done=done),
            volume=hooks,
            stop_event=threading.Event(),
            stop_lock=threading.Lock(),
            poll_interval_s=0.01,
            timeout_s=1.0,
        )
        await runner(SimpleNamespace(post_host_event=lambda *a, **k: None), _session())

    return _Row(id=id, persist=persist, drive=_drive)


def _wired_row(
    id: str, persist: str, *, awaiting: Any = False, ceiling_s: float = 30.0,
    done: bool = False,
) -> _Row:
    async def _drive(hooks: Any, monkeypatch: Any) -> None:
        runner = v2wired.build_v2_wired_run_and_consume(
            _Conductor(done=done, awaiting=awaiting),
            volume=hooks,
            stop_event=threading.Event(),
            stop_lock=threading.Lock(),
            device=WiredMicDevice(
                card_id="UMIK2", card_index=0, usb_id="2752:0072",
                model_key="minidsp_umik2", model_label="miniDSP UMIK-2",
            ),
            ceiling_s=ceiling_s,
            complete_event=threading.Event(),
            poll_interval_s=0.01,
        )
        await runner(_session())

    return _Row(id=id, persist=persist, drive=_drive)


ROWS = (
    _relay_row(
        "relay-refused", "_persist_terminal_failure",
        raises=CaptureBeginRefused(
            REASON_SESSION_CEILING_EXPIRED, "out of time",
        ),
    ),
    _relay_row(
        "relay-transport", "_persist_terminal_failure",
        raises=CaptureFailed("the link died"),
    ),
    _relay_row(
        "relay-catch-all", "_persist_terminal_failure",
        raises=RuntimeError("a DSP wedge"),
    ),
    _relay_row("relay-complete-abandon", "persist_conductor_state", done=False),
    _relay_row("relay-complete-close", "persist_conductor_state", done=True),
    # The wired walk has no transport to die of, so it has one fewer arm; the
    # ceiling expiry is what raises its CaptureBeginRefused with no capture.
    _wired_row(
        "wired-refused", "_persist_terminal_failure",
        awaiting=True, ceiling_s=0.0,
    ),
    _wired_row(
        "wired-catch-all", "_persist_terminal_failure",
        awaiting=RuntimeError("a capture-chain fault"),
    ),
    _wired_row("wired-complete-abandon", "persist_conductor_state", done=False),
    _wired_row("wired-complete-close", "persist_conductor_state", done=True),
)


async def _opened() -> str:
    return "opened"


@pytest.mark.parametrize("row", ROWS, ids=lambda row: row.id)
def test_a_raising_persist_still_gives_the_fader_back(row, monkeypatch):
    def _raise(*a: Any, **k: Any) -> None:
        raise DISK_FULL

    monkeypatch.setattr(v2host, row.persist, _raise)

    async def _drive() -> list[str]:
        fader = _Fader()
        owner = VolumeOwner(
            set_fader_db=lambda db: fader.set(db),
            get_fader_db=lambda: fader.get(),
        )
        handle = await owner.acquire_level(
            ClaimKind.SESSION_MEASUREMENT, MEASUREMENT_DB,
        )
        assert owner.holds_kind(ClaimKind.SESSION_MEASUREMENT)
        drained: list[str] = []

        async def _give_back(how: str) -> None:
            drained.append(how)
            await owner.release(handle)

        with pytest.raises(OSError) as caught:
            await row.drive(
                v2host.V2VolumeHooks(
                    open=_opened,
                    close=lambda: _give_back("close"),
                    abandon=lambda: _give_back("abandon"),
                ),
                monkeypatch,
            )
        assert caught.value is DISK_FULL, "the row died of the wrong failure"
        # THE PIN: the owner's own answer, not a spy, and not the drain's
        # outcome — an outcome cannot tell a released claim from a deferred one.
        assert not owner.holds_kind(ClaimKind.SESSION_MEASUREMENT)
        return drained

    assert asyncio.run(_drive()) == [
        "close" if row.id.endswith("-close") else "abandon"
    ]


#: The two post-walk sites, whose ``finally`` holds a whole ``if done/else``.
POST_WALK = tuple(row for row in ROWS if "-complete-" in row.id)


@pytest.mark.parametrize("row", POST_WALK, ids=lambda row: row.id)
def test_a_failing_drain_does_not_replace_the_persists_own_error(row, monkeypatch):
    """A drain raising inside the ``finally`` would REPLACE the in-flight
    persist error and change the failure identity the outer net logs and
    flips ``/status.relay`` on.

    What stops it is that BOTH branches are best-effort over the same
    ``(OSError, RuntimeError, ValueError)`` tuple — ``_abandon_best_effort``
    for one, the inline close guard for the other. This pins that symmetry:
    strip either guard and the persist's own error stops being the one that
    arrives.
    """
    def _raise(*a: Any, **k: Any) -> None:
        raise DISK_FULL

    monkeypatch.setattr(v2host, row.persist, _raise)

    async def _drain_fails() -> None:
        raise ValueError("the fader would not answer")

    async def _drive() -> None:
        with pytest.raises(OSError) as caught:
            await row.drive(
                v2host.V2VolumeHooks(
                    open=_opened, close=_drain_fails, abandon=_drain_fails,
                ),
                monkeypatch,
            )
        assert caught.value is DISK_FULL, "the drain's error replaced the persist's"

    asyncio.run(_drive())
