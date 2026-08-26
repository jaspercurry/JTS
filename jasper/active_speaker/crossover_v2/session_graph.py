# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""One measurement graph per session — the real filler for the graph seam.

:class:`~.session_seams.SessionGraph` declares what a session's graph owes;
this is the implementation the crossover-v2 measure stage runs. It exists
because the graph the routed stimuli play through is **already a session
constant**: every argument
``camilla_yaml.emit_active_speaker_program_config`` takes at the production
call site is a bind-time closure variable, so the per-stimulus path was
emitting, loading and restoring identical bytes for every capture — two config
swaps, two ducks and at least five CamillaDSP round-trips each
(``08 §Test 2``: ``Δ1 ≈ 489 ms + Δ2 ≈ 454 ms ≈ 0.94 s`` of pure duck ramp per
swapping stimulus).

**Install is idempotent, and that is the whole health check.** The one entry is
:meth:`install`: it emits once, then on every call proves the running graph is
still the one it submitted and reloads only when it is not. First stimulus and
stomped-by-a-concurrent-writer are therefore the same code path, not two. Ruling
S6 pre-authorises exactly this — *"a simple pipeline-health check may remain"* —
and ruling S10 fixes its shape: a graph that can be put back is put back and
disclosed, never a refusal to play.

**The graph itself does not change, so neither do its proofs.** This installs
the same emitter's output the per-stimulus path installed, so MS-1 (every
``ActiveEmitDevices`` field derived and forwarded), MS-2, MS-3, MS-5 and MS-13's
``_assert_program_graph_proven`` return contract are satisfied by the emit this
class is handed, unchanged — and MS-13's *"once, before the first stimulus"*
clause is satisfied by construction, because there is now exactly one emit per
session. MS-4 holds for the same reason: the stimulus still enters on the
renderer-lane ring the emitter was already given.

**A summed sweep steps it aside rather than sharing it.** ``SUMMED_SWEEP_PHASES``
measure the standing production graph deliberately — the applied system is the
thing under test — so the caller restores before one and installs again after.
That is what keeps the swap count at two for an all-routed walk and bounded by
the routed/summed transitions otherwise, instead of two per stimulus.

**Async, and the seam is not.** :class:`~.session_seams.SessionGraph` declares
its three verbs synchronous; the transport underneath is CamillaDSP over a
websocket and every production caller is already on the event loop, so these are
``async``. The verbs, their contract and their idempotence match the seam
exactly; only the colour differs. Reconciling that belongs with the seam (all
four of them are synchronous today), not with the first implementation to
discover it.
"""

from __future__ import annotations

import hashlib
import logging
from contextlib import AbstractAsyncContextManager
from typing import Any, Awaitable, Callable, Mapping

from jasper.camilla import CamillaUnavailable
from jasper.log_event import log_event

logger = logging.getLogger(__name__)

__all__ = ["MeasurementSessionGraph", "SessionGraphError"]

EmitYaml = Callable[[], str]
CamFactory = Callable[[], Any]
WriterLock = Callable[[], AbstractAsyncContextManager]
ConfirmLive = Callable[[Any, str], Awaitable[None]]
HeldTargetDb = Callable[[], float | None]


class SessionGraphError(RuntimeError):
    """The measurement graph could not be installed or put back."""


def _fingerprint(yaml_text: str) -> str:
    """Name the SUBMITTED graph.

    Provenance a record carries — which graph the evidence was measured
    through — never a gate, so it is taken from the text this class submitted
    rather than from a readback: a normalized readback is a default-filled
    superset and would name a different thing on every CamillaDSP version.
    """
    return hashlib.sha256(yaml_text.encode("utf-8")).hexdigest()[:16]


class MeasurementSessionGraph:
    """The measure stage's graph: installed once, proven per stimulus, put back.

    Every side effect is injected, so the orchestration is exercised without
    CamillaDSP or ALSA — the same shape ``bind_program_playback_seams`` uses for
    the play transaction.
    """

    def __init__(
        self,
        *,
        emit: EmitYaml,
        cam_factory: CamFactory,
        writer_lock: WriterLock,
        confirm_live: ConfirmLive,
        held_target_db: HeldTargetDb | None = None,
    ) -> None:
        self._emit = emit
        self._cam_factory = cam_factory
        self._writer_lock = writer_lock
        self._confirm_live = confirm_live
        self._held_target_db = held_target_db
        self._yaml: str | None = None
        self._entry_config_path: str | None = None

    @property
    def installed(self) -> bool:
        """True while this session holds an entry graph to put back."""
        return self._entry_config_path is not None

    def graph_yaml(self) -> str:
        """The emitted graph, emitted at most once per session.

        The emitter runs its fail-closed proofs on every call
        (``_assert_program_graph_proven``), so caching the text is what turns
        MS-13's *"once, before the first stimulus"* from a scheduling promise
        into a structural fact.
        """
        if self._yaml is None:
            self._yaml = self._emit()
        return self._yaml

    async def install(self) -> str:
        """Install the measurement graph, or prove the installed one is still it.

        Returns the fingerprint of the graph the next stimulus will play
        through. Idempotent: called before every routed stimulus, it costs a
        liveness proof when nothing moved and a reload when something did.

        **May raise** :class:`SessionGraphError`, and the caller treats that as
        "nothing new was installed" — :meth:`restore` stays able to put back
        whatever an earlier install displaced.
        """
        yaml_text = self.graph_yaml()
        cam = self._cam_factory()

        if self._entry_config_path is not None and await self._is_live(cam, yaml_text):
            return _fingerprint(yaml_text)

        async with self._writer_lock():
            reinstall = self._entry_config_path is not None
            if not reinstall:
                entry = await cam.get_config_file_path(best_effort=False)
                if not entry:
                    raise SessionGraphError(
                        "no current DSP config to restore after the session; "
                        "refusing to install the measurement graph"
                    )
                self._entry_config_path = str(entry)
            log_event(
                logger,
                "active_speaker.session_graph",
                action="install",
                result="reinstall" if reinstall else "install",
                # A reinstall means the running graph stopped being the one this
                # session submitted — a concurrent DSP writer, disclosed rather
                # than silently measured through (ruling S10).
                level=logging.WARNING if reinstall else logging.INFO,
                fingerprint=_fingerprint(yaml_text),
            )
            await self._load(cam, yaml_text)
        return _fingerprint(yaml_text)

    async def patch(self, changes: Mapping[str, Any]) -> None:
        """Change what one candidate needs, without re-installing.

        The cheap half of *"structural swap once, patch per candidate"*. It
        refuses before there is a graph to patch rather than patching whatever
        the box happens to be running.
        """
        if self._entry_config_path is None:
            raise SessionGraphError("no measurement graph is installed to patch")
        cam = self._cam_factory()
        async with self._writer_lock():
            if not await cam.patch_config(dict(changes), best_effort=False):
                raise SessionGraphError("CamillaDSP rejected the candidate patch")

    async def restore(self) -> None:
        """Put the entry graph back. Idempotent, and safe after a failed install.

        A no-op when nothing is installed, so every drain path can call it
        without asking first, and so a second call after a first that raised
        does not double-restore.
        """
        # The one restore verdict, shared with the commissioning swap paths
        # (wave 6a). Its catch set is what keeps ``CamillaUnavailable`` — a bare
        # ``Exception`` subclass, and the likeliest failure here — from escaping
        # as an unlogged raise.
        from jasper.active_speaker.web_commissioning import attempt_graph_restore

        entry = self._entry_config_path
        if entry is None:
            return
        # Cleared FIRST: a restore that raises must not leave this session
        # believing it still owns a graph, or the next drain re-enters the same
        # failing path and the caller's error is replaced by a later one.
        self._entry_config_path = None
        cam = self._cam_factory()

        async def _put_back() -> bool:
            async with self._writer_lock():
                return await cam.set_active_config_raw(
                    _read_text(entry),
                    best_effort=False,
                    held_target_db=self._held_target_db,
                )

        took_effect, raise_message = await attempt_graph_restore(_put_back)
        if not took_effect:
            fields: dict[str, Any] = {
                "action": "restore",
                "result": "rejected" if raise_message is None else "failed",
                "entry_config_path": entry,
            }
            if raise_message is not None:
                fields["error"] = raise_message
            log_event(
                logger,
                "active_speaker.session_graph",
                level=logging.CRITICAL,
                fields=fields,
            )
            raise SessionGraphError(
                raise_message
                or (
                    "the measurement graph was played but the entry graph could "
                    "not be restored; reapply the speaker profile before playing "
                    "audio"
                )
            )
        log_event(
            logger,
            "active_speaker.session_graph",
            action="restore",
            result="restored",
            entry_config_path=entry,
        )

    async def _is_live(self, cam: Any, yaml_text: str) -> bool:
        """Fail-closed: an unanswerable question is never a yes.

        ``CamillaUnavailable`` is named because it is a bare ``Exception``
        subclass and is exactly what ``confirm_graph_is_live``'s two strict
        reads raise when the websocket is gone. Treating unreadable as "still
        live" would measure the next stimulus through a graph nobody proved;
        answering ``False`` re-installs, and if that fails too the load raises
        where the caller can see it.
        """
        try:
            await self._confirm_live(cam, yaml_text)
        except (CamillaUnavailable, OSError, RuntimeError, TimeoutError, ValueError):
            return False
        return True

    async def _load(self, cam: Any, yaml_text: str) -> None:
        loaded = await cam.set_active_config_raw(
            yaml_text, best_effort=False, held_target_db=self._held_target_db,
        )
        if not loaded:
            raise SessionGraphError("the measurement graph load was not confirmed")
        await self._confirm_live(cam, yaml_text)


def _read_text(path: str) -> str:
    from pathlib import Path

    return Path(path).read_text(encoding="utf-8")
