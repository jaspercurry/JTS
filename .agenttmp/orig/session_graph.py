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

**It banks what it entered on, and says when that moved.** Every entry-graph
take is also a content fingerprint of the layers a round measures through
(:mod:`.tuning_scope`), so a round anchored to one candidate NAME can still
tell that the bytes underneath it changed between two of its own captures. It
is a disclosure, never a gate, and it is scoped: a household preference-EQ
save is above everything under tune and deliberately does not trip it (#3489).

**Neither swap ducks the fader** (wave 6d). The ``GRAPH_SWAP_DUCK_DB`` /
``MAIN_VOLUME_RAMP_SETTLE_S`` bracket exists because replacing the pipeline
under live household audio can step the graph's own gain by tens of dB at an
unchanged volume. Neither condition holds here: the session has already
claimed the fader at its declared measurement level and holds the measurement
window, so there is no household programme for a step to be loud against,
and the install happens once with nothing playing. Both swaps keep the
writer lock. Every other ``set_active_config_raw`` caller replaces the
pipeline under live audio and still ducks.

**Async, like the seam.** :class:`~.session_seams.SessionGraph` declares its
three verbs ``async`` for the reason this implementation is: the transport
underneath is CamillaDSP over a websocket and every production caller is already
on the event loop. See ADR-0179.
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

#: ``(inverted_roles, measurement_delays_us, level_trims_db) -> yaml``. The
#: three axes of the measurement VARIANT: a sign-flipped branch, a candidate
#: delay and a per-driver level match each make a different graph with a
#: different fingerprint.
EmitYaml = Callable[
    [tuple[str, ...], Mapping[str, float], Mapping[str, float]], str
]
#: ``(inverted_roles, delays, level trims)`` — what makes one graph variant
#: distinct from another, and therefore what the emit cache is keyed by.
_VariantKey = tuple[
    tuple[str, ...], tuple[tuple[str, float], ...], tuple[tuple[str, float], ...]
]
CamFactory = Callable[[], Any]
WriterLock = Callable[[], AbstractAsyncContextManager]
ConfirmLive = Callable[[Any, str], Awaitable[None]]


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
    ) -> None:
        self._emit = emit
        self._cam_factory = cam_factory
        self._writer_lock = writer_lock
        self._confirm_live = confirm_live
        self._yaml: dict[_VariantKey, str] = {}
        self._installed_yaml: str | None = None
        self._entry_config_path: str | None = None
        self._entry_scope_fingerprint: str | None = None
        self._comparability_boundary = False

    @property
    def installed(self) -> bool:
        """True while this session holds an entry graph to put back."""
        return self._entry_config_path is not None

    @property
    def entry_scope_fingerprint(self) -> str:
        """The tuning-scope hash of the graph this session ENTERED on (#3489).

        Banked once, at the first entry graph this session took, and kept
        across every restore/re-install afterwards — a round keeps its entry
        graph. ``""`` when the entry graph could not be named.
        """
        return self._entry_scope_fingerprint or ""

    @property
    def comparability_boundary(self) -> bool:
        """Has the graph under this session's captures changed since entry?

        Latched, never cleared: once two captures in one round went through
        different tuning layers they are not comparable, and a later re-entry
        that happens to match again does not repair the pair already banked.
        Provenance for the round to disclose, never a gate.

        No production reader today — the WARNING this property is set beside is
        the whole disclosure surface until a round banks it.
        """
        return self._comparability_boundary

    def graph_yaml(
        self,
        inverted_roles: tuple[str, ...] = (),
        measurement_delays_us: Mapping[str, float] | None = None,
        level_trims_db: Mapping[str, float] | None = None,
    ) -> str:
        """The emitted graph, emitted at most once per MEASUREMENT VARIANT.

        The emitter runs its fail-closed proofs on every call
        (``_assert_program_graph_proven``), so caching the text is what turns
        MS-13's *"once, before the first stimulus"* from a scheduling promise
        into a structural fact.

        R-1 makes that *once per variant* rather than once outright: a
        reverse-null walk asks for a graph whose named driver branch is
        sign-flipped, and that is a different graph with a different
        fingerprint. A candidate delay and a per-driver level match are the
        other two axes of the same thing. It is still one emit per variant,
        and every variant pays the same proofs — a flipped, delayed or
        levelled branch is not a way past them.
        """
        delays = dict(measurement_delays_us or {})
        trims = dict(level_trims_db or {})
        # The delay is part of the variant KEY, not just the payload: a walk
        # stepping coordinates asks for a different graph at every step, and a
        # cache keyed on polarity alone would hand back the previous
        # coordinate's graph and measure the wrong delay. The level match joins
        # it for the same reason and one sharper: a level-matched capture and
        # its unmatched twin differ ONLY in these gains, so a cache that did
        # not key on them would serve the untrimmed graph and bank a record
        # claiming a level match that never played.
        key = (
            inverted_roles,
            tuple(sorted(delays.items())),
            tuple(sorted(trims.items())),
        )
        cached = self._yaml.get(key)
        if cached is None:
            cached = self._emit(inverted_roles, delays, trims)
            self._yaml[key] = cached
        return cached

    async def install(
        self,
        inverted_roles: tuple[str, ...] = (),
        measurement_delays_us: Mapping[str, float] | None = None,
        level_trims_db: Mapping[str, float] | None = None,
    ) -> str:
        """Install the measurement graph, or prove the installed one is still it.

        Returns the fingerprint of the graph the next stimulus will play
        through. Idempotent: called before every routed stimulus, it costs a
        liveness proof when nothing moved and a reload when something did.

        ``inverted_roles`` picks the polarity VARIANT (R-1). Asking for one the
        box is not running is a deliberate swap, not a stomp, and the two are
        logged apart: a walk alternating normal and inverted captures would
        otherwise report a concurrent DSP writer on every stimulus.

        **A swap still asks whether we were stomped.** The two facts are
        independent — a concurrent writer can replace the graph between two
        stimuli of an alternating walk — and on that walk EVERY install is a
        variant change, so deciding the level from "the text differs" alone
        would repair a stomp silently for the whole walk and lose ruling S10's
        disclosure. The liveness read below is the symmetric counterpart of the
        fast path's: same question, asked about the graph this session last
        submitted rather than about the one it is about to.

        **May raise** :class:`SessionGraphError`, and the caller treats that as
        "nothing new was installed" — :meth:`restore` stays able to put back
        whatever an earlier install displaced.
        """
        yaml_text = self.graph_yaml(
            inverted_roles, measurement_delays_us, level_trims_db,
        )
        cam = self._cam_factory()

        if self._installed_yaml == yaml_text and await self._is_live(cam, yaml_text):
            return _fingerprint(yaml_text)

        async with self._writer_lock():
            # The ENTRY config is captured once and never re-read: after the
            # first install the box is running OUR graph, so a second read
            # would file the measurement graph as the thing to restore to.
            if self._entry_config_path is None:
                entry = await cam.get_config_file_path(best_effort=False)
                if not entry:
                    raise SessionGraphError(
                        "no current DSP config to restore after the session; "
                        "refusing to install the measurement graph"
                    )
                self._entry_config_path = str(entry)
                self._observe_entry_graph(self._entry_config_path)
            result, stomped = await self._reason_for_loading(cam, yaml_text)
            log_event(
                logger,
                "active_speaker.session_graph",
                action="install",
                result=result,
                # A stomp means the running graph stopped being the one this
                # session submitted — a concurrent DSP writer, disclosed rather
                # than silently measured through (ruling S10). A variant swap
                # this session asked for is not that, and only the stomp is
                # loud.
                level=logging.WARNING if stomped else logging.INFO,
                fingerprint=_fingerprint(yaml_text),
                inverted_roles=",".join(inverted_roles),
                measurement_delays_us=",".join(
                    f"{role}:{us:g}"
                    for role, us in sorted((measurement_delays_us or {}).items())
                ),
                # Named on the line that says which graph went in, because a
                # level match is otherwise invisible in a fingerprint: two
                # takes of one walk differing only in these numbers would
                # read as the same install to anyone reading the journal.
                measurement_level_trims_db=",".join(
                    f"{role}:{db:g}"
                    for role, db in sorted((level_trims_db or {}).items())
                ),
            )
            await self._load(cam, yaml_text)
            self._installed_yaml = yaml_text
        return _fingerprint(yaml_text)

    async def _reason_for_loading(
        self, cam: Any, yaml_text: str,
    ) -> tuple[str, bool]:
        """Why this load is happening, and whether it is somebody else's doing.

        Three arms, and the middle one is R-1's: the graph this session last
        submitted may have been replaced by a concurrent DSP writer whether or
        not the next stimulus wants a different polarity variant, so a swap
        asks the liveness question rather than assuming the answer.

        ``stomped`` is what makes the journal line loud, and it is the ONE
        input to that: a reinstall is always a stomp (the fast path already
        read liveness and got ``False``), a swap is one only when the previous
        variant is gone, and a first install displaced nothing of ours.
        """
        previous = self._installed_yaml
        if previous is None:
            return "install", False
        if previous == yaml_text:
            return "reinstall", True
        if not await self._is_live(cam, previous):
            return "reinstall", True
        return "variant", False

    def _observe_entry_graph(self, path: str) -> None:
        """Bank this session's entry tuning scope, or disclose that it moved.

        Runs at every ENTRY-graph take: the first install, and each install
        after a summed sweep put the household's graph back — :meth:`restore`
        clears the path, so the next install re-reads it, and that is the one
        moment in a session where the graph a round is anchored to is standing
        in front of us again. The first take is the anchor; every later one is
        the comparison, and a mismatch is
        :data:`~.tuning_scope.COMPARABILITY_BOUNDARY`: the round's captures
        either side of it went through different tuning layers.

        **SCOPED, which is what keeps it quiet** (#3489). A household
        ``/sound/`` save rewrites this file and moves its whole-graph content
        hash, but preference EQ sits above everything a round measures through
        and is excluded — so an EQ save is not a boundary, and a change to any
        tuning layer is.

        Never raises, and the install never depends on it. An unreadable or
        unparseable entry graph costs the fingerprint, not the capture: the
        session then makes no comparison at all rather than a false one, and
        anchors on the first entry graph it CAN name. A late anchor is still a
        real one; refusing to anchor at all would cost the round every later
        disclosure as well as the one it could not make.

        The hash is taken from the entry config FILE — the text :meth:`restore`
        will put back — not from a readback, so a live-only graph change that
        left the statefile alone is invisible here. That is the same document
        this class already treats as the thing it entered on.

        A boundary re-disclosed on every later re-entry is deliberate: each one
        brackets different captures, and a reader has to be able to tell which
        of them fell after it.
        """
        from .tuning_scope import COMPARABILITY_BOUNDARY, tuning_scope_fingerprint

        try:
            current = tuning_scope_fingerprint(_read_text(path))
        except (OSError, RuntimeError, ValueError):
            log_event(
                logger,
                "active_speaker.session_graph",
                action="entry_graph",
                result="unnameable",
                entry_config_path=path,
                exc_info=True,
            )
            return
        if self._entry_scope_fingerprint is None:
            self._entry_scope_fingerprint = current
            log_event(
                logger,
                "active_speaker.session_graph",
                action="entry_graph",
                result="banked",
                entry_scope_fingerprint=current,
            )
            return
        if current == self._entry_scope_fingerprint:
            return
        self._comparability_boundary = True
        log_event(
            logger,
            "active_speaker.session_graph",
            level=logging.WARNING,
            action="entry_graph",
            result=COMPARABILITY_BOUNDARY,
            entry_scope_fingerprint=self._entry_scope_fingerprint,
            current_scope_fingerprint=current,
        )

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
        self._installed_yaml = None
        cam = self._cam_factory()

        async def _put_back() -> bool:
            async with self._writer_lock():
                return await cam.set_active_config_raw(
                    _read_text(entry), best_effort=False, duck=False,
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
            yaml_text, best_effort=False, duck=False,
        )
        if not loaded:
            raise SessionGraphError("the measurement graph load was not confirmed")
        await self._confirm_live(cam, yaml_text)


def _read_text(path: str) -> str:
    from pathlib import Path

    return Path(path).read_text(encoding="utf-8")
