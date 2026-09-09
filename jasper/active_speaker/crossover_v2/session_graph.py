# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Own graph selection, liveness and entry restoration for one measurement session."""

from __future__ import annotations

import hashlib
import json
import logging
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

import yaml

from jasper.active_speaker.commissioning_admission import parse_running_graph
from jasper.audio_measurement.evidence_identity import json_fingerprint
from jasper.camilla import CamillaUnavailable
from jasper.log_event import log_event
from .measure_spec import (
    CANDIDATE_SCOPES,
    GRAPH_SCOPE_BASS_CANDIDATE,
    GRAPH_SCOPE_DRIVERS,
    GRAPH_SCOPES,
)

logger = logging.getLogger(__name__)
_TEMPORARY_GRAPH_DESCRIPTION = "jts-temporary-measurement:"

__all__ = ["MeasurementSessionGraph", "SessionGraphError", "temporary_graph_anchor"]

#: ``(inverted_roles, measurement_delays_us, level_trims_db) -> yaml``. The
#: three axes of the measurement VARIANT: each makes a different graph with a
#: different fingerprint.
EmitYaml = Callable[
    [tuple[str, ...], Mapping[str, float], Mapping[str, float]], str
]
EmitScopedYaml = Callable[[str, str, str], str]
#: ``(inverted_roles, delays, level trims)`` — what makes one graph variant
#: distinct from another, and therefore what the emit cache is keyed by.
_VariantKey = tuple[
    str, str, str, tuple[str, ...], tuple[tuple[str, float], ...],
    tuple[tuple[str, float], ...],
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


def _graph_body_fingerprint(graph: Mapping[str, Any]) -> str:
    return json_fingerprint({
        key: value for key, value in graph.items() if key != "description"
    })


async def temporary_graph_anchor(cam: Any, live_yaml: str | None) -> Path | None:
    """Recognize a scoped measurement only with its unchanged graph and anchor."""
    if not live_yaml or _TEMPORARY_GRAPH_DESCRIPTION not in live_yaml:
        return None
    graph = parse_running_graph(live_yaml)
    description = graph.get("description")
    if not isinstance(description, str) or not description.startswith(_TEMPORARY_GRAPH_DESCRIPTION):
        return None
    try:
        held = json.loads(description.removeprefix(_TEMPORARY_GRAPH_DESCRIPTION))
    except ValueError:
        return None
    if (
        not isinstance(held, dict)
        or held.get("scope") not in GRAPH_SCOPES
        or held.get("scope") == GRAPH_SCOPE_DRIVERS
        or not isinstance(held.get("anchor_path"), str)
        or not held["anchor_path"]
        or held.get("graph_sha256") != _graph_body_fingerprint(graph)
    ):
        return None
    if await cam.get_config_file_path(best_effort=False) != held["anchor_path"]:
        return None
    anchor = Path(held["anchor_path"])
    if hashlib.sha256(anchor.read_bytes()).hexdigest() != held.get("anchor_sha256"):
        return None
    return anchor


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
        emit_scoped: EmitScopedYaml | None = None,
    ) -> None:
        self._emit = emit
        self._emit_scoped = emit_scoped
        self._scope = GRAPH_SCOPE_DRIVERS
        self._candidate_id = ""
        self._bass_target_id = ""
        self._cam_factory = cam_factory
        self._writer_lock = writer_lock
        self._confirm_live = confirm_live
        self._yaml: dict[_VariantKey, str] = {}
        self._installed_yaml: str | None = None
        self._submitted_yaml: dict[str, str] = {}
        self._entry_config_path: str | None = None
        self._entry_yaml: str | None = None
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

    def select_scope(
        self, scope: str, candidate_id: str = "", bass_target_id: str = "",
    ) -> None:
        if scope not in GRAPH_SCOPES:
            raise SessionGraphError(f"unknown graph scope: {scope}")
        if scope in CANDIDATE_SCOPES and not candidate_id.strip():
            raise SessionGraphError(f"{scope} scope requires candidate_id")
        if scope == GRAPH_SCOPE_BASS_CANDIDATE and not bass_target_id.strip():
            raise SessionGraphError(f"{scope} scope requires bass_target_id")
        if scope != GRAPH_SCOPE_DRIVERS and self._emit_scoped is None:
            raise SessionGraphError("no scoped graph emitter is bound")
        self._scope = scope
        self._candidate_id = candidate_id if scope in CANDIDATE_SCOPES else ""
        self._bass_target_id = (
            bass_target_id if scope == GRAPH_SCOPE_BASS_CANDIDATE else ""
        )

    def graph_yaml(
        self,
        inverted_roles: tuple[str, ...] = (),
        measurement_delays_us: Mapping[str, float] | None = None,
        level_trims_db: Mapping[str, float] | None = None,
    ) -> str:
        """Emit and prove each selected graph once; cache its submitted text."""
        delays = dict(measurement_delays_us or {})
        trims = dict(level_trims_db or {})
        if self._scope != GRAPH_SCOPE_DRIVERS and (inverted_roles or delays or trims):
            raise SessionGraphError("graph overlays require drivers scope")
        key = (
            self._scope, self._candidate_id, self._bass_target_id,
            inverted_roles,
            tuple(sorted(delays.items())),
            tuple(sorted(trims.items())),
        )
        cached = self._yaml.get(key)
        if cached is None:
            if self._scope == GRAPH_SCOPE_DRIVERS:
                cached = self._emit(inverted_roles, delays, trims)
            else:
                assert self._emit_scoped is not None
                cached = self._emit_scoped(
                    self._scope, self._candidate_id, self._bass_target_id,
                )
            self._yaml[key] = cached
        return cached

    def installed_graph_yaml(self) -> str:
        if self._installed_yaml is None:
            raise SessionGraphError("no measurement graph is installed")
        return self._submitted_yaml.get(self._installed_yaml, self._installed_yaml)

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
                self._entry_yaml = Path(entry).read_text(encoding="utf-8")
                self._entry_config_path = str(entry)
                self._observe_entry_graph(self._entry_yaml)
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
                graph_scope=self._scope,
                candidate_id=self._candidate_id,
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

    def _observe_entry_graph(self, yaml_text: str) -> None:
        """Disclose tuning changes between restore/install brackets.

        Preference EQ is outside tuning scope. An unparseable entry loses this
        comparison, but its saved text remains available for restoration.
        """
        from .tuning_scope import COMPARABILITY_BOUNDARY, tuning_scope_fingerprint

        try:
            current = tuning_scope_fingerprint(yaml_text)
        except (OSError, RuntimeError, ValueError):
            log_event(
                logger,
                "active_speaker.session_graph",
                action="entry_graph",
                result="unnameable",
                entry_config_path=self._entry_config_path,
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
        """Restore the saved entry text; retain it until confirmed live."""
        # The one restore verdict, shared with the commissioning swap paths.
        # Its catch set is what keeps ``CamillaUnavailable`` — a bare
        # ``Exception`` subclass — from escaping as an unlogged raise.
        from jasper.active_speaker.web_commissioning import attempt_graph_restore

        entry = self._entry_config_path
        if entry is None:
            return
        assert self._entry_yaml is not None
        entry_yaml = self._entry_yaml
        cam = self._cam_factory()

        async def _put_back() -> bool:
            async with self._writer_lock():
                if not await cam.set_active_config_raw(
                    entry_yaml, best_effort=False, duck=False,
                ):
                    return False
                await self._confirm_live(cam, entry_yaml)
                return True

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
        self._entry_config_path = None
        self._entry_yaml = None
        self._installed_yaml = None
        self._submitted_yaml.clear()
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
            await self._confirm_live(cam, self._submitted_yaml.get(yaml_text, yaml_text))
        except (CamillaUnavailable, OSError, RuntimeError, TimeoutError, ValueError):
            return False
        return True

    async def _load(self, cam: Any, yaml_text: str) -> None:
        submitted = self._submitted_yaml.get(yaml_text)
        if submitted is None:
            submitted = yaml_text
            if self._scope != GRAPH_SCOPE_DRIVERS:
                normalized = parse_running_graph(await cam.normalize_config_raw(
                    yaml_text, best_effort=False,
                ))
                graph = parse_running_graph(yaml_text)
                assert self._entry_yaml is not None
                graph["description"] = _TEMPORARY_GRAPH_DESCRIPTION + json.dumps({
                    "scope": self._scope,
                    "anchor_path": self._entry_config_path,
                    "anchor_sha256": hashlib.sha256(self._entry_yaml.encode("utf-8")).hexdigest(),
                    "graph_sha256": _graph_body_fingerprint(normalized),
                }, sort_keys=True)
                submitted = yaml.safe_dump(graph, sort_keys=False)
            self._submitted_yaml[yaml_text] = submitted
        loaded = await cam.set_active_config_raw(
            submitted, best_effort=False, duck=False,
        )
        if not loaded:
            raise SessionGraphError("the measurement graph load was not confirmed")
        await self._confirm_live(cam, submitted)
