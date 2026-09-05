# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""One measurement graph per session — the real filler for the graph seam.

The graph routed stimuli play through is already a session constant, so it is
installed once. :meth:`install` is idempotent and that IS the whole health
check: it proves the running graph is still the one it submitted and reloads
only when it is not (rulings S6 and S10 — a graph that can be put back is put
back and disclosed, never a refusal to play). A summed sweep steps it aside
rather than sharing it. Neither swap ducks the fader (wave 6d): the session
already holds the fader at its declared measurement level inside the
measurement window and nothing is playing, so there is no household programme
for a gain step to be loud against. Async for the reason the seam is: CamillaDSP
over a websocket (ADR-0179).
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
#: three axes of the measurement VARIANT: each makes a different graph with a
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

    Taken from the text this class submitted, never from a readback: a
    normalized readback is a default-filled superset and would name a different
    thing on every CamillaDSP version.
    """
    return hashlib.sha256(yaml_text.encode("utf-8")).hexdigest()[:16]


class MeasurementSessionGraph:
    """The measure stage's graph: installed once, proven per stimulus, put back.

    Every side effect is injected, so the orchestration is exercised without
    CamillaDSP or ALSA.
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

        Banked once, at the first entry graph this session took, and kept across
        every restore/re-install afterwards. ``""`` when it could not be named.
        """
        return self._entry_scope_fingerprint or ""

    @property
    def comparability_boundary(self) -> bool:
        """Has the graph under this session's captures changed since entry?

        Latched, never cleared: two captures that went through different tuning
        layers are not comparable, and a later re-entry that happens to match
        again does not repair the pair already banked. Provenance for the round
        to disclose, never a gate.
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
        (``_assert_program_graph_proven``), so caching the text is what makes
        *"once, before the first stimulus"* a structural fact. R-1 makes that
        once per VARIANT: a sign-flipped branch, a candidate delay and
        a per-driver level match are three different graphs with three
        fingerprints, and every variant pays the same proofs.
        """
        delays = dict(measurement_delays_us or {})
        trims = dict(level_trims_db or {})
        # The delay and the trims are part of the variant KEY, not just the
        # payload: a level-matched capture and its unmatched twin differ ONLY in
        # these gains, so a cache that did not key on them would serve the
        # untrimmed graph and bank a record claiming a level match.
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

        ``inverted_roles`` picks the polarity VARIANT (R-1). A swap this session
        asked for and a stomp by a concurrent DSP writer are independent facts
        and are logged apart, so a walk alternating normal and inverted captures
        does not report a concurrent writer on every stimulus. A swap therefore
        still asks the liveness question rather than assuming the answer.

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
                # than silently measured through (ruling S10).
                level=logging.WARNING if stomped else logging.INFO,
                fingerprint=_fingerprint(yaml_text),
                inverted_roles=",".join(inverted_roles),
                measurement_delays_us=",".join(
                    f"{role}:{us:g}"
                    for role, us in sorted((measurement_delays_us or {}).items())
                ),
                # Named on the line that says which graph went in, because a
                # level match is otherwise invisible in a fingerprint.
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

        ``stomped`` is the ONE input to the journal line's level: a reinstall is
        always a stomp (the fast path already read liveness and got ``False``), a
        swap is one only when the previous variant is gone, and a first install
        displaced nothing of ours.
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

        Runs at every ENTRY-graph take: the first install, and each install after
        a summed sweep put the household's graph back (:meth:`restore` clears the
        path, so the next install re-reads it). The first take is the anchor;
        every later one is the comparison, and a mismatch is
        :data:`~.tuning_scope.COMPARABILITY_BOUNDARY`.

        SCOPED, which is what keeps it quiet (#3489): a household ``/sound/``
        save rewrites this file and moves its whole-graph content hash, but
        preference EQ sits above everything a round measures through and is
        excluded.

        Never raises, and the install never depends on it. An unreadable or
        unparseable entry graph costs the fingerprint, not the capture: the
        session anchors on the first entry graph it CAN name. The hash is taken
        from the entry config FILE — the text :meth:`restore` will put back — so
        a live-only graph change that left the statefile alone is invisible here.
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

        Refuses before there is a graph to patch rather than patching whatever
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
        without asking first, and a second call after one that raised does not
        double-restore.
        """
        # The one restore verdict, shared with the commissioning swap paths.
        # Its catch set is what keeps ``CamillaUnavailable`` — a bare
        # ``Exception`` subclass — from escaping as an unlogged raise.
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
        subclass and is exactly what ``confirm_graph_is_live``'s strict reads
        raise when the websocket is gone. Treating unreadable as "still live"
        would measure the next stimulus through a graph nobody proved.
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
