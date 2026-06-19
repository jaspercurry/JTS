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
(lazily) :mod:`jasper.active_speaker.runtime_contract`; neither depends back.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from jasper.sound.camilla_yaml import (
    emit_sound_config,
    extract_room_peqs_from_config,
    is_base_config,
    is_jts_generated_config,
)

logger = logging.getLogger(__name__)


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
    the carrier preserved (0 for the flat baseline), surfaced for telemetry.
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

    def __init__(self, kind: str, current_path: str | Path | None) -> None:
        self.kind = kind
        self._current_path = current_path
        self._room_peqs: list | None = None

    def _compute_room_peqs(self) -> list:
        raise NotImplementedError

    def _resolved_room_peqs(self) -> list:
        if self._room_peqs is None:
            self._room_peqs = self._compute_room_peqs()
        return self._room_peqs

    def reemit(
        self,
        profile,
        *,
        out_path: str | Path | None = None,
        profile_id: str | None = None,
        output_trim_db: float = 0.0,
        member_kwargs: dict | None = None,
    ) -> ReemitResult:
        # Grouping member-config policy is owned by member_config and applied
        # identically on every config path (see its module docstring). The
        # wizard paths let the carrier read it from grouping state
        # (member_kwargs=None → member_camilla_kwargs() disk read); the
        # bonded-leader bake passes its already-resolved cfg kwargs explicitly.
        # The lazy import keeps the socket-activated wizard process light.
        if member_kwargs is None:
            from jasper.multiroom.member_config import member_camilla_kwargs

            member_kwargs = member_camilla_kwargs()

        room_peqs = self._resolved_room_peqs()
        yaml = emit_sound_config(
            profile,
            room_peqs=room_peqs,
            out_path=out_path,
            profile_id=profile_id,
            output_trim_db=output_trim_db,
            **member_kwargs,
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


class _ActiveGraphCarrier:
    """Any active-crossover (roleful) graph — baseline, startup, or commissioning.

    All three are roleful (per-driver split + crossover + limiter + tweeter
    high-pass) and must never be re-emitted through the stereo template.

    PR-1: refuses — preference EQ on top of an active crossover is not wired
    yet. PR-3 will fold preference EQ pre-split (upstream of the per-driver
    split mixer) into the active *baseline*; the transient startup/
    commissioning graphs keep refusing. It NEVER re-emits through the stereo
    template, which would drop the crossover/limiter/protective high-pass.
    """

    kind = "active"

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
    ) -> ReemitResult:
        raise CarrierCannotHostEq(
            "eq_on_active_not_wired",
            "This speaker is running an active-crossover setup. Adjusting "
            "sound EQ on top of an active crossover isn't available yet — "
            "your crossover and driver protection are unchanged.",
        )


class _UnknownCarrier:
    """A config JTS did not generate. Fail closed — never re-emit over it."""

    kind = "unknown"

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
    ) -> ReemitResult:
        raise CarrierCannotHostEq(
            "unknown_config",
            "CamillaDSP is running a configuration JTS didn't generate, so "
            "JTS can't safely add sound EQ on top of it. Reset to the JTS "
            "baseline or apply room correction first.",
        )


def _loaded_config_is_active_speaker_graph(current_path: str | Path) -> bool:
    """True when the loaded config is any active-speaker (roleful) graph.

    Baseline (steady-state runtime), startup, and commissioning configs are ALL
    roleful and must never be re-emitted through the stereo template. Detection
    keys on the active-speaker emitter MODULE in the config's ``# Source:``
    header, derived from ``ACTIVE_BASELINE_SOURCE`` so it tracks every
    active-speaker emitter (not just the baseline) and stays single-sourced
    with the classifier's vocabulary. Content beats name: this fences a roleful
    graph even if it is misnamed like a sound/correction config. An unreadable
    config returns False and falls through to the fail-closed unknown carrier.
    The constant is imported lazily to keep ``runtime_contract``'s heavy
    transitive imports out of the wizard process.
    """
    from jasper.active_speaker.runtime_contract import ACTIVE_BASELINE_SOURCE

    # ACTIVE_BASELINE_SOURCE is "<module>.emit_active_speaker_baseline_config";
    # the module prefix is shared by every active-speaker emitter's header
    # (baseline / startup / commissioning), and never appears in a sound or
    # correction config (those carry the jasper.sound.camilla_yaml source).
    module_prefix = ACTIVE_BASELINE_SOURCE.rsplit(".", 1)[0] + "."
    try:
        text = Path(current_path).read_text()
    except OSError:
        return False
    return f"Source: {module_prefix}" in text


def carrier_for_loaded_config(current_path, *, config_dir):
    """Resolve the loaded CamillaDSP config to the carrier that can re-emit it.

    Resolution is by path + the ``# Source:`` header — it never guesses, and it
    fails closed (a missing/unreadable/foreign config → unknown).

    Order is safety-critical, not cosmetic. The base config is an exact path
    match and is never a roleful graph, so it short-circuits without a read.
    Then **content beats name**: a graph whose source header marks it an active
    baseline resolves to the active carrier *even if it is named like a
    sound/correction config* — otherwise a roleful graph could be re-emitted
    through the stereo template and lose its crossover/limiter/protective HP.
    This ordering is what keeps the carrier in agreement with the runtime
    safety classifier (invariant 1).
    """
    if not current_path:
        return _UnknownCarrier(current_path)
    if is_base_config(current_path):
        return _BaseFlatCarrier(current_path)
    if _loaded_config_is_active_speaker_graph(current_path):
        return _ActiveGraphCarrier(current_path)
    if is_jts_generated_config(current_path, config_dir=config_dir):
        return _SoundOrCorrectionCarrier(current_path)
    return _UnknownCarrier(current_path)
