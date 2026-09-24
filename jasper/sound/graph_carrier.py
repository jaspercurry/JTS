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

Layering: this module is the one place allowed to bridge the sound and
active-speaker subsystems. It depends on :mod:`jasper.sound.camilla_yaml` and
(lazily) :mod:`jasper.active_speaker.environment` (the safety classifier);
neither depends back.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, cast

from jasper.active_speaker.state_paths import baseline_candidate_config_path, baseline_config_path
from jasper.atomic_io import CONFIG_FILE_MODE, atomic_write_text
from jasper.audio_runtime_plan import apply_capture_precedence
from jasper.audio_runtime_settings import EmitSoundConfigKwargs
from jasper.multiroom.snapfifo import SNAPFIFO
from jasper.sound.camilla_yaml import (
    FLAT_GRAPH_WIDTH,
    FlatChannelPlan,
    emit_sound_config,
    extract_room_peqs_from_config,
    extract_room_peqs_from_config_text,
    flat_graph_channel_plan,
    is_base_config,
    is_jts_generated_config,
    sound_audition_config_path, sound_config_path,
)

if TYPE_CHECKING:
    from jasper.active_speaker.applied_tune import AppliedTune

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

    def __init__(
        self,
        reason_code: str,
        message: str,
        *,
        carrier_kind: str | None = None,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message
        self.carrier_kind = carrier_kind

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
    the carrier emitted. For ``/sound`` this is the preserved count.
    """

    yaml: str
    room_peq_count: int
    applied_profile: dict[str, Any] = field(default_factory=dict)


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
        # L0 safety: a stereo-host graph
        # is a 2-channel passthrough with no per-driver crossover/protection, so
        # it cannot host EQ for a topology that assigns a protected tweeter role —
        # full-range program would reach a compression driver. The runtime
        # contract owns that judgement; read it once here so the existing
        # `can_host_eq` pre-check refuses early (no spurious prepare_failed), and
        # re-assert in reemit() for the live-draft path that skips the pre-check.
        # Lazy import keeps the base wizard path light (the one allowed
        # sound->active_speaker bridge, like _classify_loaded_config below).
        from jasper.active_speaker.runtime_contract import flat_program_graph_block

        self._eq_block = flat_program_graph_block() if guard_flat_topology else None
        self.can_host_eq = self._eq_block is None
        # Width match (issue #2179): a stereo-host graph carries FLAT_GRAPH_WIDTH
        # channels, but the saved topology may claim fewer physical outputs. The
        # unclaimed ones must be hard muted or this re-emit lands full-range
        # program on an output the household never declared — and, on the deploy
        # path, silently REPLACES the width-matched cutover the statefile guard
        # just approved (install runs reconcile_sound_dsp_state, then restarts
        # Camilla onto whatever this wrote). Same single owner the cutover render
        # reads, so the two cannot disagree about which channels are unclaimed
        # or about where a mono box folds its program; this carrier restates no
        # topology rules of its own. Without the fold half, saving preference EQ
        # on a mono box would silently un-fold the graph the cutover seeded.
        self._flat_channel_plan = (
            flat_graph_channel_plan(width=FLAT_GRAPH_WIDTH)
            if guard_flat_topology
            else FlatChannelPlan()
        )

    def destination(self, result: ReemitResult, config_dir: str | Path, *, audition: bool = False) -> Path:
        return sound_audition_config_path(config_dir) if audition else sound_config_path(config_dir)

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

    def _channel_plan_for(
        self, emit_kwargs: Mapping[str, object]
    ) -> FlatChannelPlan:
        """The hard mutes and the mono fold to apply to THIS re-emit.

        Withheld — the empty plan — whenever the resolved sink is not this
        speaker's own DAC, because then there is no physical output to decline
        and no local cabinet to fold for:

        * ``playback_pipe_path`` (bonded-leader Snapcast FIFO). The pipe carries
          the SHARED stereo program to every follower; muting a channel there
          would strip it out of the group's stream, and folding would collapse
          the whole GROUP to mono to suit this one leader's cabinet. A bonded
          member's own fold lives receiver-side instead (``jasper-outputd``'s
          ``ChannelPick``). Same load-bearing "no DAC attached" key the runtime
          contract's program-bake exemption rests on.

        A grouped topology that genuinely needs per-output muting on a pipe
        sink is the Distributed-Active track's problem; withholding here is
        not fail-open, because the statefile guard still refuses an
        over-wide graph out loud.
        """

        if emit_kwargs.get("playback_pipe_path"):
            return FlatChannelPlan()
        return self._flat_channel_plan

    def prepare_eq(self, *, member_kwargs: dict | None = None) -> dict:
        if self._eq_block is not None:
            from jasper.active_speaker.runtime_contract import (
                FLAT_PROGRAM_GRAPH_NOT_AUTHORIZED,
                FLAT_PROGRAM_GRAPH_PROTECTED_TWEETER,
                FLAT_PROGRAM_GRAPH_UNCONFIGURED,
            )

            block_code, block_detail = self._eq_block
            if block_code == FLAT_PROGRAM_GRAPH_UNCONFIGURED:
                raise CarrierCannotHostEq(
                    block_code,
                    "No speaker layout is configured, so sound EQ cannot be "
                    "applied. Save an explicit passive mono or stereo layout, "
                    "or finish the protected active-speaker setup first. Audio "
                    "remains parked.",
                )
            if block_code == FLAT_PROGRAM_GRAPH_PROTECTED_TWEETER:
                raise CarrierCannotHostEq(
                    block_code,
                    "This speaker is running a flat full-range setup with no "
                    f"crossover, so it can't safely host sound EQ: "
                    f"{block_detail}. Adjusting EQ would send full-range "
                    "audio to a protected tweeter. Save an explicit passive "
                    "layout only if the speaker has a built-in passive crossover, "
                    "or finish the protected active-speaker setup. Your driver "
                    "protection is unchanged.",
                )
            raise CarrierCannotHostEq(
                FLAT_PROGRAM_GRAPH_NOT_AUTHORIZED,
                "The saved speaker layout does not authorize a flat sound "
                f"graph: {block_detail}. Save an explicit passive mono or stereo "
                "layout, or finish the protected active-speaker setup first.",
            )
        member_kwargs = self._resolve_member_kwargs(member_kwargs)
        self._validate_member_kwargs(member_kwargs)
        return member_kwargs

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
        member_kwargs = self.prepare_eq(member_kwargs=member_kwargs)

        emit_kwargs = cast(EmitSoundConfigKwargs, dict(member_kwargs))
        # fanin_coupling_capture_kwargs (JASPER_FANIN_CAMILLA_COUPLING=shm_ring)
        # names the shared fan-in -> Camilla -> outputd SHM-ring capture/playback
        # devices: source-agnostic, and byte-identical when absent (loopback ->
        # {}). The carrier-preserved room PEQs, preference filters, trim, and
        # member policy all fold in unchanged. PRECEDENCE, and why a pipe sink
        # still takes the capture half: apply_capture_precedence.
        emit_kwargs = apply_capture_precedence(
            emit_kwargs,
            fanin_coupling_capture_kwargs,
            member_kwargs=member_kwargs,
        )
        room_peqs = self._compute_room_peqs() if room_peqs is None else list(room_peqs)
        plan = self._channel_plan_for(emit_kwargs)
        yaml = emit_sound_config(
            profile,
            room_peqs=room_peqs,
            out_path=out_path,
            profile_id=profile_id,
            output_trim_db=output_trim_db,
            muted_outputs=plan.muted_outputs,
            mono_fold_output=plan.mono_fold_output,
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
    preference EQ, but only while re-emission keeps the File -> Snap FIFO sink.
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
        if member_kwargs.get("playback_pipe_path"):
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
        result = super().reemit(
            profile,
            room_peqs=room_peqs,
            profile_id=profile_id,
            output_trim_db=output_trim_db,
            member_kwargs=member_kwargs,
            fanin_coupling_capture_kwargs=fanin_coupling_capture_kwargs,
        )
        yaml = _restamp_program_bake_source(result.yaml)
        if out_path is not None:
            out_path = Path(out_path)
            if not out_path.parent.exists():
                raise FileNotFoundError(
                    f"parent directory does not exist: {out_path.parent}"
                )
            atomic_write_text(out_path, yaml, mode=CONFIG_FILE_MODE)
        return ReemitResult(yaml=yaml, room_peq_count=result.room_peq_count)


class _ActiveGraphCarrier:
    """The applied speaker candidate with household preference EQ."""

    kind = "active"

    def __init__(self, current_path: str | Path | None, *, is_baseline: bool) -> None:
        self._current_path = current_path
        self._is_baseline = is_baseline
        # Host EQ only on a SOLO baseline. A bonded member refuses (invariant 7);
        # the /sound follower-block (HTTP 409) usually short-circuits first, so
        # this is a backstop. The bonded read is fresh (grouping.env).
        self.can_host_eq = is_baseline and not _bonded_active_member()

    def prepare_eq(self, *, member_kwargs: dict | None = None, tune: AppliedTune | None = None) -> AppliedTune:
        if not self._is_baseline:
            raise CarrierCannotHostEq(
                "eq_on_active_not_wired",
                "This speaker is running an active-crossover setup that isn't a "
                "saved baseline yet (it's still in bring-up). Adjusting sound EQ "
                "on top of it isn't available — your crossover and driver "
                "protection are unchanged.",
            )
        # Distributed active graphs cannot host household EQ on the driver instance.
        if member_kwargs is not None or _bonded_active_member():
            raise CarrierCannotHostEq(
                "eq_on_active_bonded_member",
                "This active speaker is part of (or joining) a speaker group right "
                "now. Adjusting its sound EQ while grouped isn't available yet — "
                "ungroup it first. Your crossover and driver protection are "
                "unchanged.",
            )
        return tune if tune is not None else _load_active_tune_for_eq()

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
        tune: AppliedTune | None = None,
    ) -> ReemitResult:
        tune = self.prepare_eq(member_kwargs=member_kwargs, tune=tune)
        del fanin_coupling_capture_kwargs, room_peqs
        result = _compile_active_baseline_with_eq(profile, output_trim_db=output_trim_db, tune=tune)
        if out_path is not None:
            target = self.destination(result, Path(out_path).parent)
            atomic_write_text(target, result.yaml, mode=CONFIG_FILE_MODE)
        return result

    def destination(self, result: ReemitResult, config_dir: str | Path, *, audition: bool = False) -> Path:
        if audition:
            return sound_audition_config_path(config_dir)
        return baseline_candidate_config_path(
            result.yaml,
            Path(config_dir) / baseline_config_path().name,
        )


class _UnknownCarrier:
    """A config JTS did not generate. Fail closed — never re-emit over it."""

    kind = "unknown"
    can_host_eq = False

    def __init__(self, current_path: str | Path | None) -> None:
        self._current_path = current_path

    def reemit(self, profile, **kwargs) -> NoReturn:
        self.prepare_eq()

    def prepare_eq(self) -> NoReturn:
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


def _load_active_tune_for_eq() -> AppliedTune:
    from jasper.active_speaker.applied_tune import load_applied_tune  # lazy: active graph owner
    from jasper.active_speaker.candidate_bank import CandidateBankRefusal  # lazy: candidate lookup boundary

    try:
        return load_applied_tune()
    except (CandidateBankRefusal, OSError, ValueError) as exc:
        raise CarrierCannotHostEq("active_baseline_compile_unavailable", f"Could not load the saved speaker tune: {exc}") from exc


def _compile_active_baseline_with_eq(profile, *, output_trim_db: float = 0.0, tune: AppliedTune | None = None) -> ReemitResult:
    from jasper.active_speaker.applied_tune import compile_applied_tune  # lazy: active graph owner
    from jasper.sound.profile import build_sound_filter_slots  # lazy: profile DSP imports NumPy

    tune = tune if tune is not None else _load_active_tune_for_eq()
    try:
        text, prepared = compile_applied_tune(tune,
            preference_filters=build_sound_filter_slots(profile), output_trim_db=output_trim_db)
    except (OSError, ValueError) as exc:
        raise CarrierCannotHostEq("active_baseline_compile_unavailable", f"Could not compile the saved speaker tune: {exc}") from exc
    prepared["config"]["sound_layer"] = {"profile": profile.to_dict(), "output_trim_db": output_trim_db}
    return ReemitResult(text, len(extract_room_peqs_from_config_text(text)), prepared)


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
    from jasper.camilla_config_contract import (
        devices_playback_is_pipe,
        read_camilla_devices_config,
    )
    devices = read_camilla_devices_config(current_path) or {}
    return devices_playback_is_pipe(devices, SNAPFIFO)


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
    from jasper.output_topology import OutputTopologyError  # lazy: keep topology off the base emitter path
    from jasper.output_topology_store import load_output_topology_strict  # lazy: keep topology off the base emitter path

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
        from jasper.active_speaker.output_contract import ACTIVE_BASELINE_SOURCE

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


def eq_block_for_loaded_config(*, current_path, config_dir) -> CarrierCannotHostEq | None:
    """Check the shared EQ inputs; preview and save validate the resulting graph."""
    carrier = carrier_for_loaded_config(current_path, config_dir=config_dir)
    try:
        carrier.prepare_eq()
    except CarrierCannotHostEq as refusal:
        return refusal
    return None
