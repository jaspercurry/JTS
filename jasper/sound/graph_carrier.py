# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Graph-carrier dispatch for preference-EQ apply over any loaded CamillaDSP graph.

The ``/sound`` preference-EQ apply path must re-emit the running CamillaDSP
config with the user's preference (and preserved room-correction) filters
folded in. Different graph *kinds* preserve themselves differently, and some
cannot host program-domain EQ at all without dropping driver protection.

Rather than hard-coding "the loaded graph is a stereo ``emit_sound_config``"
at the call site, resolve the loaded graph to a *carrier* that knows how to
re-emit itself — or fail CLOSED with a typed, honest reason. Graph kinds that
can safely host EQ do so; the rest raise :class:`CarrierCannotHostEq`.

Design-of-record: ``docs/HANDOFF-dsp-graph-carrier.md``.

Layering: this module is the one place allowed to bridge the sound and
active-speaker subsystems. It depends on :mod:`jasper.sound.camilla_yaml` and
(lazily) :mod:`jasper.active_speaker.environment` (the safety classifier);
neither depends back.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from jasper.atomic_io import atomic_write_text
from jasper.audio_runtime_plan import EmitSoundConfigKwargs, apply_capture_precedence
from jasper.sound.camilla_yaml import (
    emit_sound_config,
    extract_room_peqs_from_config,
    is_base_config,
    is_jts_generated_config,
)

logger = logging.getLogger(__name__)

_SOUND_SOURCE_LINE = "# Source: jasper.sound.camilla_yaml.emit_sound_config"
_CURRENT_SOUND_CONFIG = "sound_current.yml"


class CarrierCannotHostEq(RuntimeError):
    """The loaded CamillaDSP graph cannot safely host preference EQ.

    This is a fail-CLOSED signal, NOT a server error. Re-emitting an
    unhostable graph through the stereo ``emit_sound_config`` template would
    collapse N driver outputs to 2 and drop every crossover, limiter, and
    protective high-pass — the exact unprotected-driver hazard the active
    runtime contract exists to block. ``reason_code`` is stable (the UI
    branches on it); ``message`` is household-readable.
    """

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message

    def to_payload(self) -> dict[str, str]:
        """Typed body for an HTTP 200 response (no silent failure, no 502)."""
        return {
            "status": "blocked",
            "reason_code": self.reason_code,
            "message": self.message,
        }


@dataclass(frozen=True)
class ReemitResult:
    """Result of a successful re-emit.

    ``yaml`` is always the emitted config text (the durable path also writes
    it to ``out_path``); ``room_peq_count`` is how many room-correction PEQs
    the carrier emitted. For ``/sound`` this is the preserved count; for
    ``/correction`` it is the explicitly replaced count.
    """

    yaml: str
    room_peq_count: int


class _StereoHostCarrier:
    """Re-emit for graphs the stereo emitter owns (flat baseline + JTS sound/correction).

    Only the preserved room-PEQ set differs between the two; everything else —
    including the grouping member-config kwargs applied identically on every
    config path — is shared. This is the verbatim relocation of the two safe
    arms of the former ``/sound`` 3-arm branch.
    """

    def __init__(
        self,
        kind: str,
        current_path: str | Path | None,
        *,
        guard_flat_topology: bool = True,
    ) -> None:
        self.kind = kind
        self._current_path = current_path
        # L0 safety (docs/HANDOFF-audio-measurement-core.md): a stereo-host graph
        # is a 2-channel passthrough with no per-driver crossover/protection, so
        # it cannot host EQ for a topology that assigns a protected tweeter role —
        # full-range program would reach a compression driver. The runtime
        # contract owns that judgement; read it once here so the existing
        # `can_host_eq` pre-check refuses early (no spurious prepare_failed), and
        # re-assert in reemit() for the live-draft path that skips the pre-check.
        # Lazy import keeps the base wizard path light (the one allowed
        # sound->active_speaker bridge, like _classify_loaded_config below).
        from jasper.active_speaker.runtime_contract import (
            flat_program_graph_blocked_reason,
        )

        self._eq_block_reason = (
            flat_program_graph_blocked_reason()
            if guard_flat_topology
            else None
        )
        self.can_host_eq = self._eq_block_reason is None

    def _compute_room_peqs(self) -> list:
        raise NotImplementedError

    def _resolve_member_kwargs(self, member_kwargs: dict | None) -> dict:
        # Grouping member-config policy is owned by member_config and applied
        # identically on every config path (see its module docstring). The
        # wizard paths let the carrier read it from grouping state
        # (member_kwargs=None -> member_camilla_kwargs() disk read); the
        # bonded-leader bake passes its already-resolved cfg kwargs explicitly.
        if member_kwargs is None:
            from jasper.multiroom.member_config import member_camilla_kwargs

            member_kwargs = member_camilla_kwargs()
        return member_kwargs

    def _validate_member_kwargs(self, member_kwargs: dict) -> None:
        """Carrier-specific guard after grouping policy is resolved."""

    def reemit(
        self,
        profile,
        *,
        out_path: str | Path | None = None,
        profile_id: str | None = None,
        output_trim_db: float = 0.0,
        member_kwargs: dict | None = None,
        room_peqs: list | None = None,
        fanin_coupling_capture_kwargs: dict | None = None,
    ) -> ReemitResult:
        # Refuse (typed, honest) before emitting/loading a flat program graph
        # when the saved topology assigns a protected tweeter. This is the
        # authoritative gate for the live-draft SetConfig path (which bypasses
        # the pre-check), and a backstop for the durable path. Covers BOTH the
        # in-memory live preview and the on-disk write, so a flat graph can never
        # reach the DAC under a protected-tweeter topology.
        if self._eq_block_reason is not None:
            raise CarrierCannotHostEq(
                "flat_graph_protected_tweeter",
                "This speaker is running a flat full-range setup with no "
                f"crossover, so it can't safely host sound EQ: "
                f"{self._eq_block_reason}. Adjusting EQ would send full-range "
                "audio to a protected tweeter, so it's blocked until the active "
                "crossover is applied (or the speaker layout is cleared). Your "
                "driver protection is unchanged.",
            )
        member_kwargs = self._resolve_member_kwargs(member_kwargs)
        self._validate_member_kwargs(member_kwargs)

        emit_kwargs = cast(EmitSoundConfigKwargs, dict(member_kwargs))
        # fanin_coupling_capture_kwargs (JASPER_FANIN_CAMILLA_COUPLING=shm_ring)
        # names the shared fan-in -> Camilla -> outputd SHM-ring capture/playback
        # devices: source-agnostic, and byte-identical when absent (loopback ->
        # {}). The carrier-preserved room PEQs, preference filters, trim, and
        # member policy all fold in unchanged. PRECEDENCE: a grouped pipe-SINK
        # member (enable_rate_adjust=False + SnapFIFO playback) is mutually
        # exclusive with the local coupling, so the coupling is a no-op there too.
        emit_kwargs = apply_capture_precedence(
            emit_kwargs,
            fanin_coupling_capture_kwargs,
            member_kwargs=member_kwargs,
        )
        room_peqs = self._compute_room_peqs() if room_peqs is None else list(room_peqs)
        yaml = emit_sound_config(
            profile,
            room_peqs=room_peqs,
            out_path=out_path,
            profile_id=profile_id,
            output_trim_db=output_trim_db,
            **emit_kwargs,
        )
        return ReemitResult(yaml=yaml, room_peq_count=len(room_peqs))


class _BaseFlatCarrier(_StereoHostCarrier):
    """The JTS flat baseline (outputd-cutover). No room PEQs to preserve."""

    def __init__(self, current_path: str | Path | None) -> None:
        super().__init__("base_flat", current_path)

    def _compute_room_peqs(self) -> list:
        return []


class _SoundOrCorrectionCarrier(_StereoHostCarrier):
    """A JTS-generated sound/correction config. Preserve its room PEQs."""

    def __init__(self, current_path: str | Path | None) -> None:
        super().__init__("sound_or_correction", current_path)

    def _compute_room_peqs(self) -> list:
        return extract_room_peqs_from_config(self._current_path)


class _ProgramBakeCarrier(_SoundOrCorrectionCarrier):
    """Active-leader camilla#1 program bake, safe only as a pipe sink.

    The active leader's first CamillaDSP instance owns only the 2-channel program
    domain and writes it to Snapcast's FIFO; the second instance owns Layer A
    driver protection. It is therefore a valid host for program-domain room /
    preference EQ, but only while re-emission keeps the File -> Snap FIFO sink
    and rate-adjust remains off.
    """

    def __init__(self, current_path: str | Path | None) -> None:
        # A program bake is a flat program graph, but not a DAC-bound flat graph.
        # The protected-tweeter guard remains correct for base/sound/correction
        # ALSA hosts; this carrier proves the safer predicate below instead.
        _StereoHostCarrier.__init__(
            self,
            "active_leader_program_bake",
            current_path,
            guard_flat_topology=False,
        )

    def _validate_member_kwargs(self, member_kwargs: dict) -> None:
        pipe = member_kwargs.get("playback_pipe_path")
        rate_adjust = member_kwargs.get("enable_rate_adjust")
        if pipe and rate_adjust is False:
            return
        raise CarrierCannotHostEq(
            "program_bake_pipe_unavailable",
            "CamillaDSP is running the active-leader program bake, but the "
            "current grouping state does not resolve to the Snapcast pipe sink. "
            "JTS cannot safely rewrite this grouped graph until the speaker is "
            "reconciled or ungrouped.",
        )

    def reemit(
        self,
        profile,
        *,
        out_path: str | Path | None = None,
        profile_id: str | None = None,
        output_trim_db: float = 0.0,
        member_kwargs: dict | None = None,
        room_peqs: list | None = None,
        fanin_coupling_capture_kwargs: dict | None = None,
    ) -> ReemitResult:
        # fanin_coupling_capture_kwargs is intentionally a NO-OP here: a program
        # bake is a bonded pipe SINK on the synced chain (snapclient owns the
        # rate, enable_rate_adjust=False), which is mutually exclusive with the
        # local (shm_ring) coupling. The grouped transport topology is the
        # Distributed-Active track's concern, not this solo hop; accept the keyword
        # so every call site can pass it uniformly, but never apply it. (The plan's
        # apply_capture_precedence helper makes the same grouped-sink choice for
        # stereo-host carriers.)
        del fanin_coupling_capture_kwargs
        member_kwargs = self._resolve_member_kwargs(member_kwargs)
        self._validate_member_kwargs(member_kwargs)
        room_peqs = self._compute_room_peqs() if room_peqs is None else list(room_peqs)
        yaml = emit_sound_config(
            profile,
            room_peqs=room_peqs,
            profile_id=profile_id,
            output_trim_db=output_trim_db,
            **member_kwargs,
        )
        yaml = _restamp_program_bake_source(yaml)
        if out_path is not None:
            out_path = Path(out_path)
            if not out_path.parent.exists():
                raise FileNotFoundError(
                    f"parent directory does not exist: {out_path.parent}"
                )
            atomic_write_text(out_path, yaml, mode=0o640)
        return ReemitResult(yaml=yaml, room_peq_count=len(room_peqs))


class _ActiveGraphCarrier:
    """Any active-crossover (roleful) graph — baseline, startup, or commissioning.

    All three are roleful (per-driver split + crossover + limiter + tweeter
    high-pass) and must never be re-emitted through the stereo template.

    PR-3: the SOLO active *baseline* now hosts preference EQ. It is recomposed
    from the immutable applied-profile snapshot via the active-speaker emitter —
    the preference
    filters fold in PRE-SPLIT, with their worst-case boost rolled into the
    single ``active_baseline_headroom`` gain (see
    :func:`jasper.active_speaker.baseline_profile.recompose_applied_baseline_yaml` and
    ``docs/HANDOFF-dsp-graph-carrier.md``). It NEVER re-emits through the stereo
    ``emit_sound_config`` template, so the crossover, per-driver limiters, and
    protective high-pass are preserved by construction (invariant 3). The
    transient startup/commissioning graphs keep refusing
    (``eq_on_active_not_wired``); a bonded member refuses
    (``eq_on_active_bonded_member``) — the deferred active×grouping decision
    belongs to the Distributed-Active track.

    The baseline-vs-transient distinction is the ``# Source:`` header the runtime
    verifier keys on (``runtime_contract.ACTIVE_BASELINE_SOURCE``), so the carrier
    and the verifier cannot disagree about what is a baseline (invariant 1). A
    header-stripped graph (a CamillaDSP round-trip drops comments) reads as
    non-baseline and refuses — the safe default.
    """

    kind = "active"

    def __init__(self, current_path: str | Path | None, *, is_baseline: bool) -> None:
        self._current_path = current_path
        self._is_baseline = is_baseline
        # Host EQ only on a SOLO baseline. A bonded member refuses (invariant 7);
        # the /sound follower-block (HTTP 409) usually short-circuits first, so
        # this is a backstop. The bonded read is fresh (grouping.env).
        self.can_host_eq = is_baseline and not _bonded_active_member()

    def reemit(
        self,
        profile,
        *,
        out_path: str | Path | None = None,
        profile_id: str | None = None,
        output_trim_db: float = 0.0,
        member_kwargs: dict | None = None,
        room_peqs: list | None = None,
        fanin_coupling_capture_kwargs: dict | None = None,
    ) -> ReemitResult:
        if not self._is_baseline:
            raise CarrierCannotHostEq(
                "eq_on_active_not_wired",
                "This speaker is running an active-crossover setup that isn't a "
                "saved baseline yet (it's still in bring-up). Adjusting sound EQ "
                "on top of it isn't available — your crossover and driver "
                "protection are unchanged.",
            )
        # Invariant 7: an active baseline that is grouped (already a bonded member,
        # OR forming a bond right now — the bonded-leader bake is the one caller
        # that passes member_kwargs) refuses. The active×grouping composition is
        # deferred to the Distributed-Active track. member_kwargs is the
        # bake-context signal because grouping.env may not be active yet mid-bake.
        if member_kwargs is not None or _bonded_active_member():
            raise CarrierCannotHostEq(
                "eq_on_active_bonded_member",
                "This active speaker is part of (or joining) a speaker group right "
                "now. Adjusting its sound EQ while grouped isn't available yet — "
                "ungroup it first. Your crossover and driver protection are "
                "unchanged.",
            )
        room_peqs = (
            extract_room_peqs_from_config(self._current_path)
            if room_peqs is None
            else list(room_peqs)
        )
        # shm_ring is solo-stereo-only; active baselines keep their roleful ALSA
        # capture/playback graph. Accept the shared carrier keyword for interface
        # uniformity, but do not thread it into active recomposition.
        del fanin_coupling_capture_kwargs
        # By here the carrier has proven this is a SOLO active baseline (bonded
        # members refused above).
        yaml = _recompose_active_baseline_with_eq(
            profile,
            room_peqs=room_peqs,
            output_trim_db=output_trim_db,
            out_path=out_path,
        )
        return ReemitResult(yaml=yaml, room_peq_count=len(room_peqs))


class _UnknownCarrier:
    """A config JTS did not generate. Fail closed — never re-emit over it."""

    kind = "unknown"
    can_host_eq = False

    def __init__(self, current_path: str | Path | None) -> None:
        self._current_path = current_path

    def reemit(
        self,
        profile,
        *,
        out_path: str | Path | None = None,
        profile_id: str | None = None,
        output_trim_db: float = 0.0,
        member_kwargs: dict | None = None,
        room_peqs: list | None = None,
        fanin_coupling_capture_kwargs: dict | None = None,
    ) -> ReemitResult:
        raise CarrierCannotHostEq(
            "unknown_config",
            "CamillaDSP is running a configuration JTS didn't generate, so "
            "JTS can't safely add sound EQ on top of it. Reset to the JTS "
            "baseline or apply room correction first.",
        )


def _bonded_active_member() -> bool:
    """True when this speaker is an ACTIVE member of a running bond.

    Lazy import keeps grouping state out of the socket-activated wizard's base
    path; ``is_active_member`` is a pure read of the fresh grouping config.
    """
    from jasper.multiroom.config import is_active_member, load_config

    return is_active_member(load_config())


def _recompose_active_baseline_with_eq(
    profile,
    *,
    room_peqs: list | None = None,
    output_trim_db: float = 0.0,
    out_path: str | Path | None = None,
):
    """Recompose the SOLO active baseline with ``profile``'s preference EQ
    inserted pre-split, returning the emitted YAML (written to ``out_path`` when
    given).

    Rebuilds the structural baseline from the immutable applied-profile snapshot
    via ``recompose_applied_baseline_yaml`` rather than parsing the running config
    — so the crossover/limiter/protective-HP come from the canonical builder,
    not a lossy round-trip — and raises :class:`CarrierCannotHostEq` if that
    snapshot can no longer produce a baseline. ``output_trim_db`` (the
    household's manual headroom + loudness-match attenuation) is folded into the
    active headroom so the active path honours it like the stereo path. All
    imports are lazy: this only runs for a speaker that already IS an active
    baseline, so the active-speaker + sound-profile deps stay out of the base
    wizard path.
    """
    from jasper.active_speaker.baseline_profile import (
        load_applied_baseline_profile_state,
        recompose_applied_baseline_yaml,
    )
    from jasper.active_speaker.runtime_contract import (
        GRAPH_APPROVED_ACTIVE_RUNTIME,
        classify_camilla_graph,
    )
    from jasper.output_topology import load_output_topology
    from jasper.sound.profile import build_sound_filters

    topology = load_output_topology()
    applied_profile = load_applied_baseline_profile_state()
    preference_filters = build_sound_filters(profile)
    yaml, issues = recompose_applied_baseline_yaml(
        topology,
        applied_profile=applied_profile or {},
        room_peqs=room_peqs or [],
        preference_filters=preference_filters,
        output_trim_db=output_trim_db,
        out_path=out_path,
    )
    if yaml is None:
        detail = (issues[0].get("message") if issues else None) or (
            "the saved active-speaker measurement/crossover evidence is unavailable"
        )
        raise CarrierCannotHostEq(
            "active_baseline_recompose_unavailable",
            "JTS couldn't rebuild this speaker's active baseline to add sound EQ: "
            f"{detail}. Your crossover and driver protection are unchanged.",
        )
    graph = classify_camilla_graph(topology=topology, text=yaml)
    if not graph.allowed or graph.classification != GRAPH_APPROVED_ACTIVE_RUNTIME:
        detail = (
            graph.issues[0].get("message") if graph.issues else None
        ) or "the recomposed active baseline did not pass the runtime contract"
        raise CarrierCannotHostEq(
            "active_baseline_recompose_unsafe",
            "JTS rebuilt this speaker's active baseline, but the safety "
            f"contract rejected it: {detail}. Your crossover and driver "
            "protection are unchanged.",
        )
    return yaml


def _classify_loaded_config(current_path: str | Path) -> dict | None:
    """Classify the loaded config text with the active-speaker safety classifier.

    Reuses the STRUCTURAL signal — the same ``classify_camilla_config_text`` that
    ``runtime_contract.classify_camilla_graph`` keys on — so the carrier and the
    verifier cannot drift (invariant 1). A roleful graph is recognised by its
    per-driver split mixer, not by a ``# Source:`` comment a CamillaDSP
    round-trip could strip; **content beats name**, so this fences a roleful
    graph even when it is misnamed like a sound/correction config. Returns the
    full summary (so the resolver can read both ``classification`` and
    ``source``), or ``None`` for an unreadable config (falls through to the
    fail-closed unknown carrier). The import is lazy to keep the classifier's
    transitive deps out of the socket-activated wizard process;
    ``classify_camilla_config_text`` is dependency-free text parsing and never
    raises on arbitrary input.
    """
    from jasper.active_speaker.environment import classify_camilla_config_text

    try:
        text = Path(current_path).read_text()
    except OSError:
        return None
    return classify_camilla_config_text(text)


def _loaded_config_is_program_bake_pipe(current_path: str | Path) -> bool:
    from jasper.multiroom.leader_config import playback_is_pipe
    from jasper.multiroom.reconcile import SNAPFIFO

    try:
        text = Path(current_path).read_text()
    except OSError:
        return False
    return playback_is_pipe(text, SNAPFIFO)


def _loaded_config_is_stale_program_bake_pipe(current_path: str | Path) -> bool:
    """True for the one-time recovery shape left by the old program-bake reemit.

    PR #1009 briefly produced ``sound_current.yml`` with the generic sound
    source marker even though the graph still wrote to the active leader's
    SnapFIFO program lane. The fallback is intentionally narrower than
    "any JTS pipe config": ordinary passive grouping leaders also write to
    SnapFIFO and must not be reclassified or re-stamped as active program bakes.
    """
    if Path(current_path).name != _CURRENT_SOUND_CONFIG:
        return False
    from jasper.active_speaker.runtime_contract import flat_program_graph_blocked_reason
    from jasper.output_topology import OutputTopologyError, load_output_topology_strict

    try:
        topology = load_output_topology_strict()
    except OutputTopologyError:
        return False
    return (
        _loaded_config_is_program_bake_pipe(current_path)
        and flat_program_graph_blocked_reason(topology) is not None
    )


def _restamp_program_bake_source(yaml: str) -> str:
    from jasper.active_speaker.camilla_yaml import ACTIVE_PROGRAM_BAKE_SOURCE

    program_source_line = f"# Source: {ACTIVE_PROGRAM_BAKE_SOURCE}"
    if program_source_line in yaml:
        return yaml
    if _SOUND_SOURCE_LINE not in yaml:
        raise CarrierCannotHostEq(
            "program_bake_source_marker_missing",
            "JTS rebuilt the active-leader program bake, but could not preserve "
            "its source marker. The graph was not loaded; your driver protection "
            "is unchanged.",
        )
    return yaml.replace(_SOUND_SOURCE_LINE, program_source_line, 1)


def carrier_for_loaded_config(current_path, *, config_dir):
    """Resolve the loaded CamillaDSP config to the carrier that can re-emit it.

    Resolution is by path + config *content* — it never guesses, and it fails
    closed (a missing/unreadable/foreign config → unknown).

    Order is safety-critical, not cosmetic. The base config is an exact path
    match and is never a roleful graph, so it short-circuits without a read.
    Then **content beats name**: an active-speaker graph is recognised by the
    runtime safety classifier's structural signal (its per-driver split mixer —
    see ``_classify_loaded_config``) and routed to the active carrier *even if
    it is named like a sound/correction config*, so a roleful graph can never be
    re-emitted through the stereo template and lose its crossover/limiter/
    protective HP. Within the active branch, the ``# Source:`` header decides
    whether it is the EQ-hostable *baseline* (keyed on the same
    ``ACTIVE_BASELINE_SOURCE`` the verifier's ``is_baseline`` branch uses, so
    they cannot disagree) or a transient startup/commissioning graph that
    refuses. The ``is_jts_generated_config`` name match runs only after the
    content check.
    """
    if not current_path:
        return _UnknownCarrier(current_path)
    if is_base_config(current_path):
        return _BaseFlatCarrier(current_path)
    summary = _classify_loaded_config(current_path)
    if summary and summary.get("classification") == "active_startup_candidate":
        from jasper.active_speaker.runtime_contract import ACTIVE_BASELINE_SOURCE

        is_baseline = summary.get("source") == ACTIVE_BASELINE_SOURCE
        return _ActiveGraphCarrier(current_path, is_baseline=is_baseline)
    if summary:
        from jasper.active_speaker.environment import CAMILLA_CLASS_PROGRAM_BAKE

        if summary.get("classification") == CAMILLA_CLASS_PROGRAM_BAKE:
            return _ProgramBakeCarrier(current_path)
        if (
            summary.get("classification") == "jts_generated_stereo"
            and _loaded_config_is_stale_program_bake_pipe(current_path)
        ):
            return _ProgramBakeCarrier(current_path)
    if is_jts_generated_config(current_path, config_dir=config_dir):
        return _SoundOrCorrectionCarrier(current_path)
    return _UnknownCarrier(current_path)
