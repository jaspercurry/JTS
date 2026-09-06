# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The v2 crossover conductor's web host (Wave 5a endpoint binding).

Owns everything between the ``/sound/speaker/crossover/v2/*`` POST routes (thin
dispatch branches in :mod:`jasper.web.correction_setup`) and the pure conductor
(:mod:`jasper.active_speaker.crossover_v2_flow`):

* the **durable v2 flow state** (one JSON file) that ``status_payload`` threads
  into the envelope as ``status["crossover_v2"]`` — phase / candidate / verify
  / failure / apply_blocked / needs_recovery / applied;
* the **session volume plan** singleton (one fixed measurement volume per
  session, §5.5) and its open/close/abandon wiring — including the
  walked-away guarantee: every terminal capture outcome drains the restore-once
  path;
* the **production seam bindings** — real ``analyze_program_capture``, real
  evidence-store publication (publish → tamper-checked reopen, §5.6), the real
  CamillaController-backed program playback via
  :func:`jasper.active_speaker.crossover_v2.composition.bind_program_playback_seams`,
  and the apply gate reading the durable applied flag;
* the **session assembly for the capture provider** (#2662): the preparers
  below gate, build the conductor, and hand the walk to
  :mod:`jasper.web.correction_crossover_v2_wired`, which owns the plan-walk
  hosting, the capture choreography, and the translation of its internal
  deaths into the flow's reason vocabulary. It is reached LAZILY. This host
  stays the single writer of the persisted failure state those reasons land in
  (``status["crossover_v2"]["failure"]`` — ``capture_timeout``,
  ``user_stopped``, …), and of the walked-away volume guarantee the provider
  drives through ``V2VolumeHooks``.

Session binding (§5.6): the durable state is keyed to the capture session id. A
new ``/v2/session`` POST hydrates through
:meth:`CrossoverV2Session.hydrate`, which invalidates CHECK/MEASURE evidence
for a different session; ``/v2/verify`` re-arms VERIFY only (a 1-entry plan)
from the persisted post-apply state, per §5.2's re-verify action.

ON-DEVICE: the acoustic playback binding is not exercised hardware-free (same
status as the room flow) — W6 validates it end-to-end on JTS3.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import dataclasses
import functools
import json
import logging
import math
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import (
    TYPE_CHECKING, Any, Callable, Mapping, MutableMapping, NoReturn, Sequence,
    TypeVar, cast,
)

from jasper.atomic_io import atomic_write_text
# The stage-capability vocabulary this module publishes and binds (#2291 Phase
# 4). EAGER, unlike every other ``jasper.active_speaker`` import here, because
# these are module-level NAMES rather than call-time dependencies — a lazy
# import cannot bind them. The cost is real and worth stating: the
# ``crossover_v2`` package's convenience re-exports pull ``branch_chain`` and
# with it numpy, so importing THIS module went from ~0.05 s to ~0.34 s. It is
# paid by nobody new: every shipped consumer imports this module in order to
# call into it, and its lightest entry point (``crossover_v2_status_block``)
# already loads numpy on the way to an answer.
#
# The three ``X as X`` lines are PEP 484's redundant-alias form: they are
# re-exports this module names but never calls, and the alias is what says so
# without spending suppression debt the tree is actively paying down.
from jasper.active_speaker.crossover_v2.journey import (
    CAPABILITY_COMMANDED_DELTA as CAPABILITY_COMMANDED_DELTA,
    CAPABILITY_ENTRY_BASELINE as CAPABILITY_ENTRY_BASELINE,
    CAPABILITY_FINDINGS,
    CAPABILITY_PREDICTED_SUM as CAPABILITY_PREDICTED_SUM,
    CAPABILITY_ROLLBACK,
    PHASE_MEASURE,
    STAGE_MEASURE_CAPABILITIES,
    STAGE_VERIFY_CAPABILITIES,
    StageOpening,
    available_stage_priors,
    open_stage,
)
# The position gate's two TERMINAL codes, at module level because the constants
# they name are module level. A pure-organ leaf like ``journey`` above, so this
# adds no cycle and no import cost worth deferring — every other flow symbol in
# this module stays lazily imported inside its own function, as before.
from jasper.active_speaker.capture_provenance import (
    CaptureProvenanceRecorder,
    record_capture_provenance,
)
from jasper.active_speaker.crossover_v2.conductor_context import (
    V2ConductorContext,
    ensure_crossover_preview_ready,
    resolve_conductor_context,
)
from jasper.active_speaker.crossover_v2 import durable_state as _durable
from jasper.active_speaker.crossover_v2.durable_state import (
    build_conductor_state,
)
from jasper.active_speaker.crossover_v2.refusal_copy import (
    REASON_POSITION_HOLD_EXPIRED,
    REASON_POSITION_TARGET_MISSING,
    REASON_SESSION_CEILING_EXPIRED,
    CrossoverV2Refused,
)
# The round-outcome vocabulary, which the domain owns (#2662). This module
# still DECIDES which of the four a graded session came to — see
# ``_post_apply_grade`` — it just no longer declares their names, because the
# domain renderer that speaks them cannot import this module to get them.
from jasper.active_speaker.crossover_v2.verification import (
    RESULT_INCONCLUSIVE,
    RESULT_KEEP_PREVIOUS,
    RESULT_VERIFIED_BEST_EVALUATED,
    RESULT_VERIFIED_TARGET,
)
from jasper.dsp_apply import DSP_PROOF_INACTIVE_RESULTS
from jasper.log_event import log_event

if TYPE_CHECKING:
    from jasper.active_speaker.crossover_v2_flow import AnalyzeCapture
    from jasper.active_speaker.model_error_store import ModelErrorStoreSnapshot
    from jasper.active_speaker.session_volume_plan import VolumeDoor

logger = logging.getLogger(__name__)

STATE_SCHEMA_VERSION = 1
STATE_KIND = "jts_crossover_v2_flow_state"

# The wizard-facing capture kind label (mirrors the legacy
# "crossover_sweep:<kind>" labels so /status.capture consumers need no new
# vocabulary beyond the prefix).
V2_CAPTURE_KIND_SESSION = "crossover_v2:session"
V2_CAPTURE_KIND_VERIFY = "crossover_v2:verify"

# The durable v2 state document's own vocabulary, its schema, and its one
# on-Pi path live in :mod:`jasper.active_speaker.crossover_v2.durable_state`,
# which owns what the file CONTAINS in both directions. This module owns the
# WRITE — when it happens and how durably. Re-bound here under their historical
# names because that is where the endpoints suite and the status projection
# name them.
FINDING_HOUSEHOLD_REFS_KEY = _durable.FINDING_HOUSEHOLD_REFS_KEY
MAX_PERSISTED_SUM_POINTS = _durable.MAX_PERSISTED_SUM_POINTS
DEFAULT_V2_STATE_PATH = _durable.DEFAULT_V2_STATE_PATH

_state_lock = threading.RLock()
_state_path_override: Path | None = None

_volume_plan_lock = threading.Lock()
_volume_plan: Any = None


def refusal_next_action(exc: BaseException) -> dict[str, Any] | None:
    """The action a refusal's own reason declares, for a 400 response body.

    ``None`` when the refusal carries no code or its code declares no action —
    the ordinary case for the many refusals whose only honest answer is prose.
    Copy and destination come from the SAME registry entry the envelope's
    hard-stop screen reads, so the pre-flight 400 and the post-persist screen
    can never offer different buttons for the same refusal.
    """
    from jasper.active_speaker.crossover_v2.refusal_copy import REASON_REGISTRY

    code = str(getattr(exc, "code", "") or "")
    spec = REASON_REGISTRY.get(code) if code else None
    if spec is None or not spec.next_action:
        return None
    return dict(spec.next_action)


class CrossoverV2LocalSeamError(RuntimeError):
    """A LOCAL play/analyze seam raised ``OSError`` — not a program-family failure.

    W6 hardware run 3 finding G: the DSP writer lock's ``os.open`` on a
    read-only ``config_dir`` (finding F) raised a bare ``OSError`` from
    inside ``on_armed``, which the catch-all arm would otherwise misclassify
    identically to a genuine capture-chain fault. ``on_armed``/``consume``
    convert a local ``OSError`` to THIS type at the seam boundary, so it
    reaches the catch-all cleanup arm's honest ``internal_error``
    classification instead of any program-family code.
    """


def classify_program_failure(
    exc: BaseException,
) -> tuple[str, tuple[str, ...]] | None:
    """Map a program-seam exception to its §5.10 reason code + refusal slugs.

    Returns ``None`` for anything outside the program family, so a caller can
    tell "not mine" from "mine, and here is the honest code".

    Issue #1820 defect 4: the session runner's catch-all arm used to fold the
    WHOLE family — ``ProgramPlaybackError``, ``ProgramAdmissionError``,
    ``CrossoverV2FlowError`` — into a single
    :data:`~jasper.active_speaker.crossover_v2.refusal_copy.REASON_PROGRAM_UNPLAYABLE`,
    so a deterministic "JTS cannot use the saved safety limits" and
    a genuine level-ceiling failure rendered the same sentence and offered the
    same (for the former, actively harmful) action. Refusal identity survives
    the boundary now: ``PROFILE_NOT_CONFIRMED`` gets its own code and screen,
    and every other refusal keeps ``program_unplayable`` but carries its own
    slugs out for forensics (persisted under ``state["failure"]["refusals"]``
    and logged) instead of being erased.

    This is the ONE classifier. ``build_v2_run_and_consume``'s cleanup arm and
    ``jasper.web.correction_setup._capture_failure_message`` both call it, so the
    wizard's capture status line and the failure screen can
    never disagree about which refusal happened — the drift that let a raw
    ``"program re-admission refused: program_profile_not_confirmed"`` reach the
    wizard's DOM while the phone was told something else entirely.
    """
    from jasper.active_speaker.crossover_v2.contracts import CrossoverV2FlowError
    from jasper.active_speaker.crossover_v2.refusal_copy import (
        REASON_MEASUREMENT_VOLUME_DRIFT,
        REASON_PROGRAM_PROFILE_NOT_CONFIRMED,
        REASON_PROGRAM_UNPLAYABLE,
        REASON_PROTECTION_NOT_SEPARABLE,
        REASON_PROTECTION_SWEEP_TOO_LOW,
    )
    from jasper.active_speaker.program_admission import (
        ProgramAdmissionError,
        ProgramAdmissionRefusal,
    )
    from jasper.active_speaker.program_playback import (
        ProgramPlaybackError,
        ProgramPlaybackRefused,
    )
    from jasper.active_speaker.volume_latch import MeasurementFaderDrift
    from jasper.audio_measurement.program_analysis import (
        ConfiguredPathConditioningError,
    )

    if isinstance(exc, MeasurementFaderDrift):
        # #2925. Its own code because it says the OPPOSITE of
        # ``program_unplayable``: the program was admissible and the SPEAKER's
        # level was not the one it was admitted against. No refusal slugs — the
        # observed/expected pair rides the
        # ``active_speaker.measurement_fader_drift`` line that refused, not an
        # admission's refusal set.
        return REASON_MEASUREMENT_VOLUME_DRIFT, ()
    if isinstance(exc, ConfiguredPathConditioningError):
        return (
            REASON_PROTECTION_SWEEP_TOO_LOW if exc.protection_floor
            else REASON_PROTECTION_NOT_SEPARABLE
        ), (exc.slug,)
    if not isinstance(
        exc, (ProgramPlaybackError, ProgramAdmissionError, CrossoverV2FlowError)
    ):
        return None
    refusals: tuple[str, ...] = ()
    if isinstance(exc, ProgramPlaybackRefused):
        refusals = tuple(reason.value for reason in exc.admission.refusals)
    code = (
        REASON_PROGRAM_PROFILE_NOT_CONFIRMED
        if ProgramAdmissionRefusal.PROFILE_NOT_CONFIRMED.value in refusals
        else REASON_PROGRAM_UNPLAYABLE
    )
    return code, refusals


def refused_from_flow_error(exc: BaseException) -> "CrossoverV2Refused":
    """Turn a :class:`CrossoverV2FlowError` into a refusal the household reads.

    Issue #1833. ``resolve_plan_shape``'s failures are programmer strings
    ("unknown commission tier 'turbo' (expected one of full, express,
    remote)",
    "cloud_measure_positions must be 6..12, got 14"). Two call sites used to
    rewrap them as ``CrossoverV2Refused(str(exc))``, which the wizard's 400 arm
    echoes into the DOM verbatim — the exact leak
    :func:`classify_program_failure` exists to close, defeated by the rewrap
    happening BEFORE any classification: once it is a ``ValueError`` the
    classifier no longer claims it, and
    ``correction_setup._capture_failure_message`` never sees it either.

    So classify FIRST and carry the code out. The message comes from the same
    :data:`~jasper.active_speaker.crossover_v2.refusal_copy.REASON_REGISTRY` entry the
    phone's failure screen renders, so the two surfaces cannot disagree, and
    the ``code=`` lets the 400 body pick up that reason's ``next_action`` when
    it declares one. ``program_unplayable`` — the only code this function can
    produce today, since that is where the classifier puts the whole
    ``CrossoverV2FlowError`` family — declares none, so today's 400 carries the
    sentence alone.

    The raw text is logged here rather than dropped: this is the one site that
    discards it, and it is the only place the failed constraint is named. The
    boundary's own ``correction.crossover_v2_refused`` log records what was
    SENT, which from here on is household copy.
    """
    from jasper.active_speaker.crossover_v2.refusal_copy import (
        REASON_INTERNAL_ERROR,
        REASON_REGISTRY,
    )

    classified = classify_program_failure(exc)
    code = classified[0] if classified else REASON_INTERNAL_ERROR
    log_event(
        logger,
        "correction.crossover_v2_plan_shape_refused",
        level=logging.WARNING,
        code=code,
        error_type=type(exc).__name__,
        detail=str(exc),
    )
    return CrossoverV2Refused(REASON_REGISTRY[code].message, code=code)


# --------------------------------------------------------------------------- #
# durable state
# --------------------------------------------------------------------------- #


def _state_path() -> Path:
    return _state_path_override or DEFAULT_V2_STATE_PATH


def set_state_path_for_tests(path: str | Path | None) -> None:
    """Test seam: point the durable v2 state at a temp file (None resets)."""
    global _state_path_override
    with _state_lock:
        _state_path_override = Path(path) if path is not None else None


def load_v2_state() -> dict[str, Any] | None:
    """Read the durable v2 flow state; malformed/missing reads as ``None``."""
    with _state_lock:
        try:
            raw = json.loads(_state_path().read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError):
            log_event(
                logger,
                "correction.crossover_v2_state_unreadable",
                level=logging.WARNING,
            )
            return None
    if (
        not isinstance(raw, Mapping)
        or raw.get("kind") != STATE_KIND
        or raw.get("schema_version") != STATE_SCHEMA_VERSION
    ):
        return None
    return dict(raw)


def save_v2_state(state: Mapping[str, Any], *, durable: bool = False) -> None:
    """Write the durable v2 state. ``durable`` decides whether it is fsync'd.

    Atomic is not durable. :func:`~jasper.atomic_io.atomic_write_text` writes a
    tempfile and renames, so a concurrent reader never sees a partial file —
    but without ``durable=True`` nothing has told the kernel to put those bytes
    on the platter, and a power cut can lose the whole write while leaving the
    speaker's DSP graph changed.

    **The rule for choosing (#2291): durable where power loss would lose the
    way-back pointer or falsify a receipt; cheap everywhere else.** Two writes
    qualify — one per half of that rule:

    * :func:`observe_apply_success`, which owns
      ``previous_candidate_fingerprint``, the only pointer the way back
      (republish-then-apply) resolves its target from. It is created in the
      same moment the new graph goes live, so a lost write leaves a corrected
      speaker with no recorded way back.
    * the RECEIPT identity, written by :func:`persist_conductor_state` — but
      **only on a persist that carries a new one**. A receipt lives in the
      write-once evidence bundle, so losing the pointer to it leaves an
      immutable record nothing can find: the "falsifies a receipt" half.

    Everything else stays cheap on purpose, including
    :func:`persist_conductor_state`'s ordinary path — it runs after every
    consumed capture, and an fsync per capture buys nothing that the next
    capture's write does not already redo. :func:`reset_v2_journey_state`
    PRESERVES the pointer, so losing its write leaves the richer previous
    state — the pointer survives either way, which is why it is not on the
    durable list.
    """
    payload = {
        "schema_version": STATE_SCHEMA_VERSION,
        "kind": STATE_KIND,
        "updated_at": time.time(),
        **{k: v for k, v in state.items() if k not in {"schema_version", "kind"}},
    }
    with _state_lock:
        atomic_write_text(
            _state_path(),
            # allow_nan=False: fail at the writer that produced the non-finite
            # value, not at the evidence packet hours later (#2839).
            json.dumps(payload, allow_nan=False, indent=2, sort_keys=True) + "\n",
            mode=0o640,
            durable=durable,
        )


def _update_current_review(
    session_id: str, candidate_fingerprint: str, sound_revision: int | None,
    updates: Mapping[str, Any], *, allow_applied: bool = False,
) -> bool:
    with _state_lock:
        state = load_v2_state()
        candidate = (state or {}).get("candidate")
        if (
            state is None
            or str(state.get("session_id") or "") != session_id
            or not isinstance(candidate, Mapping)
            or str(candidate.get("fingerprint") or "") != candidate_fingerprint
            # The JOURNEY phase, not program.PROGRAM_PHASE_MEASURE — both are
            # the string "measure", so the wrong one reads correct today.
            or PHASE_MEASURE not in (state.get("accepted_phases") or ())
            or state.get("accepted_sound_revision") != sound_revision
            or (state.get("applied") is True and not allow_applied)
        ):
            log_event(logger, "correction.crossover_v2_apply_outcome_superseded",
                      level=logging.WARNING,
                      candidate_fingerprint=candidate_fingerprint)
            return False
        state.update(updates)
        save_v2_state(state)
        return True


def clear_v2_state() -> None:
    with _state_lock:
        try:
            _state_path().unlink()
        except FileNotFoundError:
            pass
        except OSError:
            log_event(
                logger,
                "correction.crossover_v2_state_clear_failed",
                level=logging.WARNING,
            )


def _attempt_loop_store_snapshot() -> ModelErrorStoreSnapshot:
    """The store-owned floor and current model-error count for one conductor.

    The host performs the I/O at conductor construction; the conductor
    receives values and a writer seam, and the attempts kernel remains pure.
    """
    from jasper.active_speaker.model_error_store import store_snapshot

    return store_snapshot()


def _record_live_model_error(**observation: Any) -> bool:
    """Claim one durable identity for the conductor's persistence seam."""
    from jasper.active_speaker.model_error_store import (
        ModelErrorConflictError,
        record_model_error,
    )

    try:
        record_model_error(**observation)
    except ModelErrorConflictError:
        return False
    return True


def reset_v2_journey_state() -> None:
    """Start-over's v2 clear (W6.10, gate-amended for the way back).

    Clears the measurement-JOURNEY fields (session binding, accepted phases,
    candidate, verify, failure, gain plan, priors, evidence) so the envelope
    serves the clean start screen — but when a candidate is APPLIED, preserves
    ``applied`` + ``previous_candidate_fingerprint``: the ONLY durable pointer
    the way back (republish-then-apply) resolves its target from. A full
    :func:`clear_v2_state` here would drop it, leaving the applied graph
    playing with no way back. Not applied ⇒ full clear, as before — except that
    the clear carries the ordinal-sequence epoch when one has been set, because
    ``applied`` is not the same question as "is a measured graph playing" and a
    reset marker any later path can erase is not a disclosure. See the two
    branches for the whole argument.
    """
    from jasper.active_speaker.crossover_v2.coordinator import (
        ROUND_ORDINAL_EPOCH_STATE_KEY,
        round_ordinal_epoch_from_state,
    )

    state = load_v2_state()
    if state is None:
        return
    epoch = round_ordinal_epoch_from_state(state)
    if not state.get("applied"):
        # **The clear CARRIES the epoch.** ``applied`` is not "is a measured
        # graph playing" — the republish door sets it ``False`` while the graph
        # it published keeps playing, and says so — so this branch is reachable
        # on a speaker that has already been tuned AND already had its ordinal
        # sequence reset. Unlinking the file there would destroy the marker and
        # the next round would read "round 1, epoch 0": a fresh box, on a
        # speaker that is anything but.
        #
        # A reset marker that any later path can erase is not a disclosure, so
        # once set it survives every clear. ``epoch == 0`` still unlinks
        # outright — there is nothing to carry, and a box that has never been
        # reset must keep leaving no file at all.
        #
        # NOT routed through the receipt-dropping branch below instead: that
        # branch writes ``applied: True``, which on this path would claim an
        # apply this session never made — trading a lost disclosure for a false
        # one. The clean-start shape is written here verbatim so every reader
        # sees the same falsy journey fields a full clear leaves.
        if not epoch:
            clear_v2_state()
            return
        save_v2_state({
            ROUND_ORDINAL_EPOCH_STATE_KEY: epoch,
            "session_id": None,
            "accepted_phases": [],
            "applied": False,
            "gain_plan_db": None,
            "candidate": None,
            "verify": None,
            "failure": None,
            "apply_blocked": None,
            "verify_priors": None,
            "evidence": None,
        })
        log_event(
            logger,
            "correction.crossover_v2_journey_reset_kept_epoch",
            round_ordinal_epoch=epoch,
        )
        return
    previous_candidate = state.get("previous_candidate_fingerprint")
    displaced_by = state.get("previous_candidate_displaced_by")
    attempts_loop = state.get("attempts_loop")
    # This branch drops ``round_receipt`` — the ordinal sequence's only memory
    # — while the applied graph keeps playing, so the next round is round 1
    # again on a speaker that has already been tuned. That is the SAME reset
    # the republish door performs, so it takes the same epoch marker: one that
    # counted only one of the two doors would make "epoch 0" mean "never reset"
    # on one path and "reset by the other door" on the other.
    #
    # **Incremented only when there was a receipt to drop.** ``applied`` can be
    # ``True`` with no round ever graded (an apply whose VERIFY never landed),
    # and repeated Start-Over taps there would inflate the epoch — disclosing
    # resets that never happened, which is the same dishonesty as hiding one.
    dropped_receipt = state.get("round_receipt")
    reset_from = (
        dropped_receipt.get("round_ordinal")
        if isinstance(dropped_receipt, Mapping)
        else None
    )
    reset_round_ordinal_from = (
        reset_from
        if isinstance(reset_from, int) and not isinstance(reset_from, bool)
        else None
    )
    if dropped_receipt is not None:
        epoch += 1
        log_event(
            logger,
            "correction.crossover_v2_journey_reset_advanced_epoch",
            round_ordinal_epoch=epoch,
            reset_round_ordinal_from=reset_round_ordinal_from,
        )

    save_v2_state({
        ROUND_ORDINAL_EPOCH_STATE_KEY: epoch,
        "session_id": None,
        "accepted_phases": [],
        "applied": True,
        "gain_plan_db": None,
        "candidate": None,
        "verify": None,
        "failure": None,
        "apply_blocked": None,
        "verify_priors": None,
        "evidence": None,
        # Start over is how the household initiates another tune. Preserve the
        # prior applied-candidate attempts so the next VERIFY is compared with
        # its immediate predecessor.
        "attempts_loop": (
            dict(attempts_loop) if isinstance(attempts_loop, Mapping) else None
        ),
        # Start over keeps the applied graph playing, so the way back's
        # pointer survives with it.
        "previous_candidate_fingerprint": (
            previous_candidate
            if isinstance(previous_candidate, str) and previous_candidate
            else None
        ),
        # The pointer's pairing survives on the same terms: Start over
        # keeps the applied graph playing, so the apply that recorded the
        # pointer is still the one under grade.
        "previous_candidate_displaced_by": (
            displaced_by
            if isinstance(displaced_by, str) and displaced_by
            else None
        ),
    })
    log_event(logger, "correction.crossover_v2_journey_reset_kept_applied")


def observe_apply_success(
    candidate_fingerprint: str,
    *,
    previous_candidate_fingerprint: str | None = None,
    expected_post_apply_offset_db: float = 0.0,
) -> None:
    """Mark the v2 candidate applied — the apply-complete event that arms the
    soft-held VERIFY (§5.2). Called by the v2 apply endpoint on success.

    ``previous_candidate_fingerprint`` is the measured candidate the applied
    graph DISPLACED — read off the frozen ``applied_recomposition_profile``'s
    own ``source.measured_candidate_fingerprint`` by ``handle_v2_apply``. It
    is the ONLY durable pointer the way back (republish-then-apply) resolves
    its target from: the status block publishes it, the wizard mints its
    way-back action from it, and the round's auto-revert republishes it.
    ``None`` — a first-ever apply, or a displaced profile that was not a
    measured-candidate apply — is written as such, re-stamped by every
    successful apply so the pointer can never outlive the apply it describes.
    A state written before this field existed simply has no way back until
    the next apply records one (the same no-schema-bump posture
    ``_record_is_fresh`` takes).

    ``expected_post_apply_offset_db`` (#1811) is the whole-band level move the
    emitted graph made and did NOT command as part of the correction's shape —
    the pre-split headroom charged for the correction's own boost. Persisted
    alongside ``applied`` because the delta probe runs one capture later, in a
    different thread, and reads it back off this state; ``0.0`` means "nothing
    known", never "nothing moved" (the probe's ``residual_offset_db`` is what
    tells those two apart).
    """
    state = load_v2_state()
    if state is None:
        return
    candidate = state.get("candidate")
    stored = (
        str(candidate.get("fingerprint") or "")
        if isinstance(candidate, Mapping)
        else ""
    )
    if stored and candidate_fingerprint and stored != candidate_fingerprint:
        log_event(
            logger,
            "correction.crossover_v2_apply_fingerprint_mismatch",
            level=logging.WARNING,
        )
        return
    state["applied"] = True
    # SF1 (adversarial review, 2026-07-20): do NOT blindly clear an existing
    # failure code. In the ordinary happy path it is already None (MEASURE's
    # own accept clears it before the conductor ever triggers auto-apply) —
    # but a terminal session-death code (a Stop, a capture timeout) can
    # land WHILE the auto-apply background thread's apply_baseline_profile
    # transaction is still in flight. If that race lands the stop FIRST,
    # clobbering it here would erase the evidence that the household
    # stopped even though the crossover genuinely got applied (this call
    # proves it) — the envelope needs BOTH facts to render an honest
    # "applied, but you stopped it" screen instead of a false "nothing
    # happened" or a false "start over, nothing changed."
    # The reverse race (a stop landing AFTER this call persists) is already
    # handled: persist_conductor_state preserves ``applied`` once it
    # observes it, for the same session.
    state["apply_blocked"] = None
    state["previous_candidate_fingerprint"] = (
        previous_candidate_fingerprint
        if isinstance(previous_candidate_fingerprint, str)
        and previous_candidate_fingerprint
        else None
    )
    # The pointer's PAIRING: the identity of the apply that recorded it —
    # this one, named by the candidate it installed. The automatic revert
    # fires only when this equals the candidate the round displaced the prior
    # with (one equality, checked at the seam), which is what refuses a
    # pointer inherited from an OLDER apply (#2559's staleness class) and —
    # because the revert's own success re-stamps this to ``None`` — a second
    # automatic revert inside the [revert…next-apply] window (the ping-pong).
    # Re-stamped by every successful apply, exactly like the pointer above.
    state["previous_candidate_displaced_by"] = (
        str(candidate_fingerprint) if candidate_fingerprint else None
    )
    offset_db = float(expected_post_apply_offset_db)
    state["expected_post_apply_offset_db"] = (
        round(offset_db, 3) if math.isfinite(offset_db) else 0.0
    )
    # fsync'd (#2291). This write CREATES the way-back pointer, and it happens
    # after the new graph is already live on the speaker: a power cut that
    # loses it leaves a corrected speaker with no recorded way back.
    save_v2_state(state, durable=True)
    log_event(
        logger,
        "correction.crossover_v2_applied",
        expected_post_apply_offset_db=state["expected_post_apply_offset_db"],
    )


#: The one shape a recorded review decision takes, and its only value.
#:
#: ``decision`` is a word rather than a bool because the fact being recorded is
#: WHICH answer the household gave, and a bool names only one of them.
REVIEW_DECISION_DECLINED = "declined"


def observe_review_decline(candidate_fingerprint: str) -> None:
    """Record that the household chose to keep the current sound (#2641).

    The write half of the review screen's decline, beside its
    :func:`observe_apply_success` sibling and under the same lock, because
    that is where every durable v2 write lives: a read-modify-write from the
    HTTP layer would be a second writer racing this one on the state file.

    **It clears nothing.** Declining changes nothing on the speaker and does
    not delete the candidate — the review screen's own contract, and the reason
    it holds is that an accidental tap would otherwise cost ten captures to
    undo. The proposal stays reviewable until a newer measurement replaces it;
    what this records is the DECISION.

    ``candidate_fingerprint`` is stamped so the decline binds to the proposal
    it answered. A later measurement mints a different candidate, and the
    phase resolver compares the two — so a stale decline cannot close a review
    the household has never seen. Durable, because the whole point is that a
    household who has decided is not asked again after a power cut.
    """
    with _state_lock:
        state = load_v2_state()
        if state is None:
            return
        state["review_decision"] = {
            "decision": REVIEW_DECISION_DECLINED,
            "candidate_fingerprint": str(candidate_fingerprint or ""),
        }
        save_v2_state(state, durable=True)
    log_event(
        logger, "correction.crossover_v2_review_declined",
        candidate_fingerprint=str(candidate_fingerprint or ""),
    )


def review_declined(state: Mapping[str, Any] | None) -> bool:
    """Has the household declined the candidate this state currently holds?

    The READER for the key :func:`observe_review_decline` writes, beside that
    writer for the reason every other reader/writer pair in this module is:
    the two must be impossible to drift apart on a shape.

    The fingerprint comparison is the whole check. A decline names the proposal
    it answered, so a newer measurement — which mints a new candidate — is not
    covered by it and the review screen comes back. A decline recorded when
    there was nothing to propose matches a state with no candidate, because
    "there is nothing to offer, keep what you have" is a real answer to a real
    screen.
    """
    if not isinstance(state, Mapping):
        return False
    decision = state.get("review_decision")
    if not isinstance(decision, Mapping):
        return False
    if str(decision.get("decision") or "") != REVIEW_DECISION_DECLINED:
        return False
    candidate = state.get("candidate")
    current = (
        str(candidate.get("fingerprint") or "")
        if isinstance(candidate, Mapping) else ""
    )
    return str(decision.get("candidate_fingerprint") or "") == current


def _applied_gate() -> bool:
    """The conductor's ``apply_complete`` seam: reads the durable applied flag."""
    state = load_v2_state()
    return bool(state and state.get("applied") is True)


def _applied_offset_gate() -> float:
    """The conductor's ``applied_offset_db`` seam: the whole-band level move
    the apply declared (#1811), read fresh off durable state.

    Written by :func:`observe_apply_success` on the apply's own request
    thread (the auto-apply worker's, before the two-stage split removed it);
    read by the delta probe on a LATER session's runner thread. Durable state
    is the only thing the two share — since the split they are not even the
    same session — which is why this is a seam rather than a constructor
    argument, the same reason ``apply_complete`` and ``apply_failed`` are.

    ``0.0`` for an absent, malformed, or non-finite value: "nothing known".
    The probe treats that honestly (the whole shift stays visible in
    ``residual_offset_db``), so a missing value degrades to today's behaviour
    rather than to a false claim that the level was accounted for.
    """
    state = load_v2_state()
    raw = (state or {}).get("expected_post_apply_offset_db")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return 0.0
    value = float(raw)
    return value if math.isfinite(value) else 0.0


def _apply_failure_gate() -> str:
    """The conductor's ``apply_failed`` seam: reads a durable apply failure
    code (empty when none), persisted through the SAME
    ``persist_conductor_state`` path every other capture failure uses — see
    ``jasper.active_speaker.crossover_v2_flow.CrossoverV2Session.authorize_begin``,
    which refuses the deferred VERIFY hold outright once this names a code
    rather than holding it toward a dishonest capture_timeout. Retained with
    that hold and, like it, unreached by any shipped session since the
    two-stage split (D10) — the writer it was built for was the auto-apply
    worker thread, which is gone.

    N1 (adversarial review, 2026-07-20): the contract is LITERAL — this seam
    answers "did the apply itself fail?", never "is SOME failure code sitting
    in durable state for any reason." Any other code that happens to be
    persisted (e.g. a stale value from an unrelated path) must not be misread
    by authorize_begin as an apply failure; only ``REASON_APPLY_FAILED``
    qualifies.
    """
    from jasper.active_speaker.crossover_v2.refusal_copy import REASON_APPLY_FAILED

    state = load_v2_state()
    failure = (state or {}).get("failure")
    if isinstance(failure, Mapping):
        code = str(failure.get("code") or "")
        if code == REASON_APPLY_FAILED:
            return code
    return ""


# --------------------------------------------------------------------------- #
# session volume plan singleton (§5.5)
# --------------------------------------------------------------------------- #


def session_volume_plan() -> Any:
    """The one durable-state-backed SessionVolumePlan this process owns."""
    global _volume_plan
    from jasper.active_speaker.session_volume_plan import (
        DEFAULT_SESSION_VOLUME_STATE_PATH,
        SessionVolumePlan,
    )

    with _volume_plan_lock:
        if _volume_plan is None:
            _volume_plan = SessionVolumePlan(
                state_path=DEFAULT_SESSION_VOLUME_STATE_PATH
            )
        return _volume_plan


def set_volume_plan_for_tests(plan: Any) -> None:
    global _volume_plan
    with _volume_plan_lock:
        _volume_plan = plan


# --------------------------------------------------------------------------- #
# session-scoped measurement pause (§5.5 + W6.1 — hold voice OFF for the whole
# session, not just per-play)
# --------------------------------------------------------------------------- #
#
# The per-play ``measurement_window()`` (bind_production_play._emit) protected
# each stimulus, but between opening the fixed measurement volume and the first
# play — and in the gaps between plays — nothing held voice paused, so
# jasper-voice's idle reconciler reverted the -20 dB session volume back toward
# the household level within ~200 ms of ``session_volume_opened`` (W6.1 hardware
# run 2). Cap enforcement then silently understates: programs would play hotter
# than admission assumed. Fix: like the room / balance / sync flows, HOLD one
# ``measurement_window`` for the whole session (its MEASURE_PAUSE keeps the idle
# reconciler off), acquired when the volume opens and released on every drain.
#
# ``measurement_window`` is EXCLUSIVE (a single ``_window_active`` mutex — a
# second concurrent window raises), not nestable, so the per-play window is
# nest-SKIPPED while the session holds one (see ``bind_production_play``). The
# held context manager lives here as a process-global entered on jasper-web's
# single background loop; acquire/release are idempotent so a drain that runs
# after the session already released (recover / ceiling / a crash-fresh process)
# is a safe no-op. The paired ``MeasurementAbortTarget`` keeps the coordinator's
# isolation-loss abort effective under a held window: the per-play path
# registers the actual play task, so a mux gate-lease renew failure cancels the
# in-flight sweep (not the long-lived session task) and latches ``failed`` so
# the next play refuses honestly.
_session_pause_cm: Any = None
_session_abort_target: Any = None


async def acquire_session_measurement_pause() -> None:
    """Enter (once) the coordinator measurement window for the whole session.

    Idempotent: if the session already holds it, this is a no-op so a spurious
    second acquire cannot open a second exclusive window. Raises
    ``MeasurementWindowError`` if the window cannot be opened (e.g. a live voice
    session) — the caller surfaces that as a session-open failure.
    """
    global _session_pause_cm, _session_abort_target
    if _session_pause_cm is not None:
        return
    from jasper.measurement_window import (
        MeasurementAbortTarget,
        measurement_window,
    )

    target = MeasurementAbortTarget()
    cm = measurement_window(abort_target=target)
    await cm.__aenter__()
    _session_pause_cm = cm
    _session_abort_target = target
    log_event(logger, "correction.crossover_v2_measurement_pause", action="acquire")


async def release_session_measurement_pause() -> None:
    """Exit the held session measurement window (idempotent).

    Every drain path (close / abandon / ceiling / unresolved-recover) calls
    this; a drain that runs when nothing is held (already released, or a
    crash-fresh process that never entered it) is a safe no-op — never a
    double-release.
    """
    global _session_pause_cm, _session_abort_target
    cm = _session_pause_cm
    if cm is None:
        return
    _session_pause_cm = None
    _session_abort_target = None
    await cm.__aexit__(None, None, None)
    log_event(logger, "correction.crossover_v2_measurement_pause", action="release")


def session_measurement_pause_held() -> bool:
    """True while the session holds the one measurement window (per-play skip)."""
    return _session_pause_cm is not None


def reset_session_measurement_pause_for_tests() -> None:
    """Test seam: drop the held-window reference without an ``__aexit__``."""
    global _session_pause_cm, _session_abort_target
    _session_pause_cm = None
    _session_abort_target = None


async def _under_measurement_isolation(play_body: Callable[[], Any]) -> None:
    """One play under the session's isolation, whichever shape holds it.

    The selector both playback legs use — the flow's ``_emit`` and the engine
    measure leg — spelled once so the two cannot drift: with the session-held
    window, the body runs abort-target-registered under it
    (:func:`_play_under_session_pause` — a latched isolation-loss abort
    REFUSES the play, and a mid-play abort surfaces as the named
    ``MeasurementWindowError``); without it, a per-play window is taken.
    """
    if session_measurement_pause_held():
        await _play_under_session_pause(play_body)
        return
    from jasper.measurement_window import measurement_window

    async with measurement_window():
        await play_body()


async def _play_under_session_pause(play_body: Callable[[], Any]) -> None:
    """Run one play under the session-held window, abort-target registered.

    Before Finding C the per-play window's ENTERING task was the play task, so
    the coordinator's isolation-loss abort (cancel the entering task) stopped
    the sweep. The held window's entering task is the session runner, whose
    cancel would not stop an in-flight play — so the play task registers
    itself as the abort target while playing. A latched abort (gate-lease
    renew failure between plays) refuses the next play with a NAMED error, and
    a cancel that lands mid-play surfaces as the same named error, so the
    runner's cleanup arm persists an honest failure either way.
    """
    from jasper.measurement_window import MeasurementWindowError

    target = _session_abort_target
    if target is not None and target.failed:
        raise MeasurementWindowError(
            "measurement isolation was lost (the music-isolation gate lease "
            "could not be renewed); restart the measurement session"
        )
    task = asyncio.current_task()
    if target is not None and task is not None:
        target.register(task)
    try:
        await play_body()
    except asyncio.CancelledError:
        if target is not None and target.failed:
            # The coordinator aborted THIS play on isolation loss — surface a
            # named terminal error (not a bare cancellation) so the session
            # runner's cleanup arm persists it and tells the phone.
            raise MeasurementWindowError(
                "measurement isolation was lost mid-play; playback was "
                "stopped before household music could re-enter the mix"
            ) from None
        raise
    finally:
        if target is not None:
            target.clear()


# --------------------------------------------------------------------------- #
# session-volume recovery + ceiling (§5.5 + W6.1 — recover routing, lazy ceiling)
# --------------------------------------------------------------------------- #

# Bound each session-volume drain (recover / ceiling) — CamillaDSP set+confirm
# is a few RPCs; longer than that means CamillaDSP is wedged, and the drain
# should surface a failure rather than hang the request thread.
_SESSION_VOLUME_DRAIN_TIMEOUT_S = 15.0


def _session_volume_read(camilla_factory: Any) -> Callable[[], Any]:
    """The main-volume READER, fail-closed on CamillaUnavailable.

    **The write half is gone, and its absence is the point of W5-c1.** This
    factory returned a ``(set, get)`` pair, and that ``_set`` was the one named
    exception to ``VolumeOwner`` owning this fader — it called
    ``CamillaController.set_volume_db`` directly, with no coordinator and no
    arbitration. Every writer that consumed it now goes through the owner, so
    there is nothing left for it to serve and it is deleted rather than left
    for the next caller to find.

    Reads never were the exception, and both survivors are reads: the
    capture-time hold, and :func:`_volume_door`'s physical snapshot.
    """
    from jasper.camilla import CamillaUnavailable

    async def _get() -> float | None:
        try:
            return await camilla_factory().get_volume_db(best_effort=False)
        except CamillaUnavailable as exc:
            raise RuntimeError("CamillaDSP is unavailable") from exc

    return _get


def _refuse_without_a_volume_owner(where: str) -> "CrossoverV2Refused":
    """The one refusal for a process with no fader owner, household copy and all.

    ``jasper.web.__main__`` installs an owner before serving, so a process
    without one is a REGISTRATION defect rather than a shape this module
    supports — there is no second authority to fall back to, and minting one
    would be the arbitration failure the owner exists to delete.

    Raised rather than answered with a door that quietly fails every verb: a
    door like that is a guard against a hypothetical, and the paths that would
    reach it already treat a raise as a failed drain. The household gets
    registry copy from here, because the wizard's 500 arm renders an
    unmapped exception's own string and internals are not household copy.
    """
    from jasper.active_speaker.crossover_v2.refusal_copy import (
        REASON_INTERNAL_ERROR,
        REASON_REGISTRY,
    )

    log_event(
        logger,
        "correction.crossover_v2_volume_owner_absent",
        level=logging.CRITICAL,
        where=where,
    )
    return CrossoverV2Refused(
        REASON_REGISTRY[REASON_INTERNAL_ERROR].message,
        code=REASON_INTERNAL_ERROR,
    )


def _session_volume_claim() -> Any:
    """This session's ONE fader claim, or ``None`` when no owner is installed.

    Minted at the composition root and injected into the two things that hold
    it — the engine's volume seam and the plan's door — because they are one
    claim, not two. ``None`` is the no-owner registration defect, and the
    binder turns it into the household refusal above.
    """
    from jasper.active_speaker.crossover_v2.volume_claim import (
        MeasurementVolumeClaim,
    )
    from jasper.volume_owner import volume_owner

    owner = volume_owner()
    return None if owner is None else MeasurementVolumeClaim(owner)


def _volume_door(
    camilla_factory: Any, *, claim: Any = None, reason: str = "drain",
) -> "VolumeDoor":
    """The plan's one door for every path in this module that drains or opens.

    ONE builder, which is the point: this module's fader authority is
    :class:`~jasper.volume_owner.VolumeOwner`, and every caller asks for its
    door here rather than assembling one, so the binding is a single fact.

    ``claim`` is the session's :class:`~jasper.active_speaker.crossover_v2.
    volume_claim.MeasurementVolumeClaim` when a session is opening, and
    ``None`` for the three OUT-OF-RUNNER drains — ceiling enforcement,
    unresolved recovery and the new-session reconcile — which run when no
    session exists and therefore have no claim to establish through. They need
    the restore leg only, and a door that cannot establish says so rather than
    pretending.

    **The capture-time hold keeps a RAW getter and must not be moved onto this
    door.** Not because it happens not to write: because
    ``hold_measurement_volume`` is the #2925 tripwire, and a tripwire has to
    read the PHYSICAL fader. This door's own read is physical for exactly that
    reason — see ``OwnerVolumeDoor.read_household_level_db`` — but routing the
    hold through the plan's door would still be wrong, because the hold asks
    its question per stimulus against the level the PLAN declares, not against
    a household level.
    """
    from jasper.active_speaker.crossover_v2.volume_claim import OwnerVolumeDoor
    from jasper.volume_owner import volume_owner

    owner = volume_owner()
    if owner is None:
        raise _refuse_without_a_volume_owner(reason)
    return OwnerVolumeDoor(
        owner, read_fader=_session_volume_read(camilla_factory), claim=claim,
    )


def _session_measurement_claim_held() -> bool:
    """Does a measurement session still own the fader, in this process?

    The ``VolumeOwner`` is process-global and already knows, so nothing here
    needs a handle, a session object, or any knowledge of the graph. No owner
    means no claim and therefore no session: releasing is correct, not a
    fail-open, because a measurement session cannot have run without one.
    """
    from jasper.volume_owner import ClaimKind, volume_owner

    owner = volume_owner()
    if owner is None:
        return False
    return owner.holds_kind(ClaimKind.SESSION_MEASUREMENT)


def _release_pause_best_effort(run_async: Any) -> None:
    """Release the measurement pause for a drain that runs OUTSIDE the runner
    (recover / ceiling / new-session reconcile).

    **THE contract for all three drains, stated once, here.** Voice/mux
    isolation is freed only when no ``SESSION_MEASUREMENT`` claim is held —
    the owner's knowledge, never a restore OUTCOME. An outcome cannot tell
    "the session is finished with its isolation" from "deferred", "the drain
    raised", or "landed by coincidence": a household level that happens to
    equal the measurement level answers ``LANDED`` under a live claim (both
    default to ``MEASUREMENT_REFERENCE_VOLUME_DB`` on a box that never ran
    seat-SPL), and a raising drain answers nothing at all. Gating on the claim
    makes every one of those hold the pause.

    Why the claim and not the graph: the graph is the session's, and the web
    layer having its own handle on it is the process global this wave deleted.
    The claim is the owner's, the owner is already process-wide, and a live
    claim is the honest proxy for "a session is still measuring".

    Gating on a claim cannot strand the pause: ``TuningSession._give_back_held``
    releases the claim in the ``finally`` around the graph restore, so even a
    graph that will not come back still gives the claim up, and the next drain
    frees the isolation.

    Idempotent: a drain that runs when nothing is held — a session that never
    opened, a crash-fresh process, a second drain — is a safe no-op.
    """
    if _session_measurement_claim_held():
        log_event(
            logger,
            "correction.crossover_v2_pause_release_withheld",
            level=logging.INFO,
        )
        return
    try:
        run_async(
            release_session_measurement_pause(),
            timeout=_SESSION_VOLUME_DRAIN_TIMEOUT_S,
        )
    except (concurrent.futures.TimeoutError, OSError, RuntimeError, ValueError):
        logger.warning("v2 session measurement-pause release failed", exc_info=True)


def enforce_session_volume_ceiling_if_stale(
    run_async: Any, camilla_factory: Any
) -> bool:
    """Lazy wall-clock-ceiling enforcement (W6.1 — ``enforce_ceiling`` had zero
    callers, so the 1800 s ceiling never existed at runtime).

    Invoked on envelope build (on read) and at session open. Cheap on the happy
    path: ``stale_active`` is an in-memory check, so a healthy session pays
    nothing; only a session that has outlived
    ``DEFAULT_WALL_CLOCK_CEILING_S`` is force-drained here, restoring the
    household volume and releasing any held measurement pause. v2-only. Returns
    True iff a stale session was drained.

    **A LIVE session's claim outranks this drain, and that is not a failure.**
    This runs on the request thread while a ``TuningSession`` may still hold
    the fader — the slow-but-alive positioner this exists for. The owner then
    RECORDS the household level behind that claim and lands it on release, so
    the drain answers ``DEFERRED``: nothing is latched and no recovery is
    offered. The caller's gate still hears that the ceiling expired.

    The measurement pause is NOT this arm's to reason about — a raising drain
    reaches the release with no outcome at all. :func:`_release_pause_best_effort`
    owns that contract for all three drains.
    """
    from jasper.active_speaker.session_volume_plan import (
        SessionVolumeRestoreResult,
    )

    plan = session_volume_plan()
    try:
        if not plan.stale_active():
            return False
    except (OSError, RuntimeError, ValueError):
        return False
    result: Any = None
    try:
        result = run_async(
            plan.enforce_ceiling(_volume_door(camilla_factory)),
            timeout=_SESSION_VOLUME_DRAIN_TIMEOUT_S,
        )
    except (concurrent.futures.TimeoutError, OSError, RuntimeError, ValueError):
        log_event(
            logger,
            "correction.crossover_v2_ceiling_enforce_failed",
            level=logging.ERROR,
        )
    if result is SessionVolumeRestoreResult.DEFERRED:
        log_event(
            logger,
            "correction.crossover_v2_ceiling_enforce_deferred",
            level=logging.INFO,
        )
        return True
    _release_pause_best_effort(run_async)
    return True


def v2_volume_recovery_active() -> bool:
    """True when the v2 session-volume plan holds a state the recover-volume
    endpoint must drain (unresolved, or a crash-hydrated active plan). The
    legacy-lease path 409s these because they live on the v2 plan, not the
    lease — the observed ``crossover_volume_recovery_not_required`` bug."""
    try:
        return bool(session_volume_plan().needs_recovery)
    except (OSError, RuntimeError, ValueError):
        return True  # fail-closed: an unreadable state still offers recovery


#: The ``recovery`` value :func:`recover_session_volume` reports for a
#: deferral, so its caller can say something true without importing the enum.
#: Mirrors ``SessionVolumeRestoreResult.DEFERRED.value`` — this module keeps
#: ``session_volume_plan`` out of its runtime import graph (the import above is
#: ``TYPE_CHECKING``-only), and a test pins the two equal so they cannot drift.
RECOVERY_DEFERRED = "deferred"


def recover_session_volume(
    run_async: Any, camilla_factory: Any
) -> tuple[bool, str]:
    """Drain the v2 plan's unresolved / stale-active state (the volume_recovery
    screen's ``recover_volume`` action). Returns ``(succeeded, result_value)``.

    Routes to ``SessionVolumePlan.recover_unresolved`` — the v2 owner of the
    unresolved state — instead of the legacy lease, and releases any held
    measurement pause on success.

    **A deferral is not a recovery.** A live session's claim outranks this
    drain, so the household level is recorded rather than restored; telling
    the household "recovered" would name an event that has not happened yet.
    ``DEFERRED`` therefore reports failure here, and the caller's copy says
    what is actually true — the restore lands when that session finishes.
    """
    from jasper.active_speaker.session_volume_plan import (
        SessionVolumeRestoreResult,
    )

    plan = session_volume_plan()
    try:
        result = run_async(
            plan.recover_unresolved(_volume_door(camilla_factory)),
            timeout=_SESSION_VOLUME_DRAIN_TIMEOUT_S,
        )
    except concurrent.futures.TimeoutError:
        log_event(
            logger,
            "correction.crossover_v2_volume_recovery_timeout",
            level=logging.ERROR,
        )
        result = SessionVolumeRestoreResult.FAILED
    succeeded = result not in (
        SessionVolumeRestoreResult.FAILED,
        SessionVolumeRestoreResult.DEFERRED,
    )
    if succeeded:
        _release_pause_best_effort(run_async)
    return succeeded, getattr(result, "value", str(result))


def reconcile_session_volume_for_new_session(
    run_async: Any, camilla_factory: Any
) -> None:
    """Drain any residual session volume before a fresh session opens (W6.1 E1).

    A stale-active is force-drained by the ceiling; a residual owned-active
    leftover from a prior failed session in THIS process (``open`` refuses over
    any non-``None`` state) is drained too, so ``plan.open`` starts clean rather
    than raising ``SessionVolumePlanError`` into the silent
    200→adapter_failed loop observed live (run 2's retry). A latched
    ``unresolved`` / crash-hydrated ``needs_recovery`` state is NOT drained here
    — the caller's ``needs_recovery`` gate refuses it toward the recover-volume
    screen.

    **A live session's claim defers this drain rather than failing it.** The
    household level is recorded behind that claim and lands on release, so
    nothing latches and the caller's ``needs_recovery`` gate does not send a
    household that simply opened a second session toward the recovery screen.
    The pause is :func:`_release_pause_best_effort`'s call, not this one's:
    this arm reaches it with no outcome whenever ``abandon`` raises.
    """
    from jasper.active_speaker.session_volume_plan import (
        SessionVolumeRestoreResult,
    )

    plan = session_volume_plan()
    enforce_session_volume_ceiling_if_stale(run_async, camilla_factory)
    if plan.measurement_volume_db is None or plan.needs_recovery:
        return
    try:
        reconciled = run_async(
            plan.abandon(
                _volume_door(camilla_factory), reason="stale_session_reset",
            ),
            timeout=_SESSION_VOLUME_DRAIN_TIMEOUT_S,
        )
        if reconciled is SessionVolumeRestoreResult.DEFERRED:
            # A live session still holds the fader. Its level is recorded and
            # lands on release; nothing is latched, so the caller's
            # ``needs_recovery`` gate does not send a household that simply
            # opened a second session toward the recovery screen.
            log_event(
                logger, "correction.crossover_v2_stale_session_reset_deferred",
            )
            return
        log_event(logger, "correction.crossover_v2_stale_session_reset")
    except (concurrent.futures.TimeoutError, OSError, RuntimeError, ValueError):
        log_event(
            logger,
            "correction.crossover_v2_stale_session_reset_failed",
            level=logging.ERROR,
        )
    _release_pause_best_effort(run_async)


# The status projection — ``crossover_v2_status_block`` and the per-key
# readers it composes — moved whole to
# :mod:`jasper.web.correction_crossover_v2_status` (rows Q/R/S of this
# file's dissolution map). It reaches back here for the durable-state
# owner, the volume plan and the grade; the only reach the other way is
# ``persist_conductor_state``'s, from inside the function.


# The vocabulary of ``crossover_v2.post_apply_grade.state`` (PR-L4 item 4).
# Readers should `.get` against these rather than exhaustively match: a durable
# state written by a later build can carry a name this one has never seen, and
# an unknown state must degrade to "not graded" rather than to a crash.
GRADE_NOT_APPLIED = "not_applied"
GRADE_GRADED = "graded"
# Express's passing grade. Distinct from GRADE_GRADED so a `/state` reader can
# tell the two claims apart WITHOUT cross-referencing `tier` (PR-L4 review):
# express verifies at the mark only — it never walks a post-apply position
# group — so "graded" and "confirmed at one spot" are materially different
# promises and were rendering as the same word.
GRADE_MARK_VERIFIED = "mark_verified"
GRADE_INCONCLUSIVE = "inconclusive"
GRADE_FAILED = "failed"
GRADE_UNVERIFIED = "unverified"

# The four ``RESULT_*`` codes this module's ``_post_apply_grade`` selects from
# are imported at the top of the file rather than declared here: the domain
# owns the vocabulary, this module owns the choice.

# --------------------------------------------------------------------------- #
# #2098's scope/completeness fact, and #2160's failed-gauge consumption.
#
# ``state`` above answers "was it checked". These answer the two questions a
# surface needed and had to guess at: how WIDE is the evidence behind that
# answer, and does that width meet what the commission tier promised.
# --------------------------------------------------------------------------- #

#: How far the evidence behind ``state`` reaches. Never a tier — a tier is what
#: was PROMISED, this is what was DELIVERED, and conflating them is the #2098
#: defect (a Full session's mark-only pass rendered as the full claim).
GRADE_SCOPE_NONE = "none"
GRADE_SCOPE_MARK = "mark"
GRADE_SCOPE_SPATIAL = "spatial"

#: The post-apply SPATIAL grade's own state (#2160). ``overall_passed`` is a
#: bool and therefore cannot distinguish "graded and failed" from "could not be
#: graded at all" — :attr:`~jasper.active_speaker.flat_spec.SpecFlatness.passed`
#: is ``False`` for an unmeasurable spectrum too, by its own "will not report a
#: clean bill of health for a spectrum it could not fully measure" rule. This
#: field carries the distinction the verdict key structurally cannot.
GRADE_SPATIAL_ABSENT = "absent"
GRADE_SPATIAL_PASSED = "passed"
GRADE_SPATIAL_FAILED = "failed"
GRADE_SPATIAL_UNMEASURABLE = "unmeasurable"


def _spatial_grade(post_apply: Any) -> str:
    """One post-apply cloud entry reduced to its SPATIAL grade state.

    ``overall_passed`` — projected by
    :func:`~jasper.active_speaker.crossover_envelope_v2.compact_cloud_status`
    from the
    spec report — stays THE consumed verdict key: every existing verdict path
    reads it, and ``flatness.passed`` is the same value under another name, so
    this deliberately does not become a second reader of it.
    ``flatness.evaluable`` is consulted for exactly one thing, the distinction
    ``overall_passed`` cannot carry: a spectrum where no band survived to be
    measured reports ``passed=False`` and is NOT a failure.

    Unmeasurable is claimed only on POSITIVE evidence (``evaluable`` present
    and ``False``). A durable state whose entry carries no ``flatness`` at all
    — an available pipeline written before the gauge shipped — leaves the only
    verdict that exists standing, because downgrading a recorded failure to
    "could not be measured" on the ABSENCE of a gauge would be the fabricated
    reading this program forbids, pointed the other way.
    """
    if not isinstance(post_apply, Mapping):
        return GRADE_SPATIAL_ABSENT
    passed = post_apply.get("overall_passed")
    if not isinstance(passed, bool):
        # No verdict — the group never closed, or its pipeline never became
        # available. Never a failing grade; see ``_spec_verdict``'s own
        # "absence of a verdict is not a failing one" rule.
        return GRADE_SPATIAL_ABSENT
    if passed:
        return GRADE_SPATIAL_PASSED
    flatness = post_apply.get("flatness")
    if isinstance(flatness, Mapping) and flatness.get("evaluable") is False:
        return GRADE_SPATIAL_UNMEASURABLE
    return GRADE_SPATIAL_FAILED


def _post_apply_grade(block: Mapping[str, Any]) -> dict[str, Any]:
    """Was the correction now ON the speaker ever checked after it landed?

    **Applied implies graded** (linearization-integrity PR-L4 item 4). A
    session can end ``applied: true`` with no passing post-apply grade — VERIFY
    inconclusive, VERIFY failed and never retried, or a session that simply
    stopped after the apply — and before this the only trace was a phase name
    and an empty ``verify`` block that every surface read as "nothing to
    report". That is how a 10 dB-dark profile sat on JTS3 with a green tick
    over it.

    **Surface, not auto-restore.** The work order allowed either; this is the
    deliberate choice and the reason is that the two failure modes are not
    distinguishable at this seam. A missing grade means "we do not know", and
    the commonest way to reach it is a household that closed the phone after
    the apply — auto-restoring would silently undo a correction that is very
    probably fine, on evidence that says nothing about the correction at all.
    Worse, express-tier sessions omit the post-apply position group by design,
    so an auto-restore keyed on a missing cloud grade would revert every
    express session ever run. The way back already exists on the done screen,
    and it is the household's call. What was missing is being told.

    The returned ``state`` is one of the ``GRADE_*`` constants above;
    ``graded`` answers only "was it checked" — since R19 it is no longer a
    boolean a caller may key "all clear" on by itself; ``scope``/``spatial``/
    ``complete`` below carry the verdict it cannot. Both a passing VERIFY
    outcome and a graded post-apply cloud count — either instrument is a real
    check, and the tiers differ in which one they run. A mark-VERIFY that
    FAILED caps ``state`` whatever the cloud group says (#2464); the
    derivation below owns that rule and states why.

    **``state`` answers "was it checked"; ``scope``/``spatial``/``complete``
    answer "how widely, and was that enough" (R19, #2098 + #2160).** Those
    three are why this returns more than a state name. ``state`` alone cannot
    carry either fact, and both were being guessed at downstream:

    * a Full session whose post-apply group never closed reaches
      ``mark_verified`` — a true local result, and NOT the claim Full
      promised. It rendered as "applied and graded".
    * a post-apply group that closed with ``overall_passed=False`` reaches
      ``GRADE_GRADED``, because a graded-and-failed group IS graded. It also
      rendered as "applied and graded" — measured on jts3 2026-08-07, a
      −4.63 dB spatial miss under a green tick.

    ``scope`` is what the evidence DELIVERED; ``tier`` is what the session
    PROMISED; ``complete`` is the producer's own comparison of the two, so no
    consumer re-derives it (plan §7: one producer owns the scope/completeness
    fact for the wizard, ``/state``, and doctor). The ``else`` tier branch
    catches two different inputs and judges BOTH on delivery alone — but NOT
    because "the promise cannot be known", which is false of the first input
    and misdescribes the second:

    * **No ``tier`` line at all.** ``normalize_tier`` documents an absent tier
      as Full, so this promise IS knowable and the branch declines it BY
      CHOICE. A state file with no tier came from a build that had no tier
      concept and therefore never MADE Full's promise; judging it against
      Full's stricter spatial-scope bar would false-warn a correctly
      commissioned legacy speaker about delivery it was never asked to
      produce. Wiring ``normalize_tier`` in here IS that regression.
    * **A tier word from a later build.** ``normalize_tier`` does not default
      this one — it RAISES, by its own "fail loudly rather than silently
      measure something else" rule. That rule is right for a caller opening a
      session and wrong here: ``_post_apply_grade`` runs unguarded inside
      ``crossover_v2_status_block`` on every ``/state`` read, wizard poll, and
      doctor run, so raising would take the whole status block down over a
      word this build only needed to not grade.

    ``spatial_worst_db``/``_hz`` are copied from the same ``flatness`` gauge
    the doctor's cloud-pipeline line prints, never re-derived, so "the grade
    failed" and "by how much" cannot drift apart. ``None`` whenever the gauge
    reports no number, including a failed grade whose gauge is absent.

    **Grades and discloses; never gates** (#2160 ruling). A failed spatial
    grade is a COMPLETED grade: the session completes, the applied tune stays,
    the failure is loud. Nothing here reverts anything — see the
    surface-not-auto-restore paragraph above, which this extends rather than
    revisits.
    """
    from jasper.active_speaker.crossover_v2.capture_plan import (
        TIER_EXPRESS,
        TIER_FULL,
    )
    from jasper.active_speaker.crossover_v2.journey import PHASE_CLOUD_VERIFY
    from jasper.active_speaker.crossover_v2.refusal_copy import (
        REASON_VERIFY_CROSSOVER_REGION,
    )
    from jasper.active_speaker.crossover_v2_flow import (
        CLAIM_FAIL,
        CLAIM_PASS,
        PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB,
    )
    from jasper.active_speaker.crossover_v2.accountability import LEDGER_NOT_AN_IMPROVEMENT

    # **This grade reads no ``fc_selection``, on any round.** It once gated its
    # success verdicts on a corner selector's verdict and completeness — "the Fc
    # comparison finished, and the corner on the speaker is the one it
    # authorized". That selector is retired (``docs/tuning-master-plan.md``
    # ticket 2.4) along with the corner hunt that fed it, so no round publishes
    # one and this build cannot restate the adjudication of a round that did.
    #
    # This function's own question is the one in its title: was the applied
    # correction checked AFTERWARDS. VERIFY and the post-apply group answer that
    # by themselves — that is why dropping the selector consultation is sound
    # rather than merely convenient, and it is the same reasoning that already
    # exempted the absent case when the selector was merely unfed.
    #
    # **Read-back tolerance is by non-consumption.** A round banked while a
    # selector existed still carries the payload in durable state; no product
    # read path parses it — not this grade, the status block, the household
    # envelope or the evidence packet — so no legacy shape, well-formed, partial
    # or malformed, can refuse or raise. (Offline archaeology tooling still
    # reads it on purpose: ``scripts/derive-crossover-incident-fixture.py`` mints
    # the #2291 fixture from it, and ``scripts/bank-crossover-round.sh``
    # snapshots it when a bank carries one.)
    # Such a round grades on its OWN verification evidence,
    # which is measured fact about the applied tune rather than a retired
    # comparator's opinion of an alternative. Pinned in
    # ``tests/test_correction_crossover_v2_endpoints.py``.
    if not block.get("applied"):
        # **No cause left, so no claim.** Two instruments could once say that an
        # un-applied round had DELIBERATELY kept the previous tune: a
        # not-an-improvement refusal, which stopped refusing when
        # ``accountability``'s item 2 became a grade (#2854), and the corner
        # selector's ``recommend_alternative``, retired here. Neither exists,
        # so this arm publishes no ``outcome`` at all rather than inventing one
        # — nothing was applied, and nothing measured why.
        return {
            "state": GRADE_NOT_APPLIED,
            "graded": True,
            "verify_outcome": None,
            "scope": GRADE_SCOPE_NONE,
            "spatial": GRADE_SPATIAL_ABSENT,
            "spatial_worst_db": None,
            "spatial_worst_hz": None,
            # Nothing was promised, so nothing is outstanding. `False` here
            # would warn every speaker that has never been commissioned.
            "complete": True,
        }
    verify = block.get("verify")
    outcome = str((verify or {}).get("outcome") or "") if isinstance(verify, Mapping) else ""
    claims = verify.get("claims") if isinstance(verify, Mapping) else None
    claims = claims if isinstance(claims, Mapping) else {}
    integration = claims.get("integration")
    integration = integration if isinstance(integration, Mapping) else {}
    absolute = claims.get("absolute")
    absolute = absolute if isinstance(absolute, Mapping) else {}
    tracking_status = str(integration.get("status") or "")
    absolute_status = str(absolute.get("status") or "")
    prediction = block.get("prediction")
    prediction = prediction if isinstance(prediction, Mapping) else {}
    comparison = prediction.get("comparison")
    comparison = comparison if isinstance(comparison, Mapping) else {}
    candidate = block.get("candidate")
    candidate = candidate if isinstance(candidate, Mapping) else {}
    improvement_db = _finite(comparison.get("improvement_db"))
    required_db = _finite(comparison.get("required_db"))
    absolute_miss_db, absolute_worst_hz = _finite(absolute.get("max_db")), _finite(absolute.get("worst_hz"))
    result_evidence = bool(comparison or integration or absolute)
    # The published candidate IS the corner the round executed, so there is no
    # alternative for a winner to have beaten. The fingerprint stays required —
    # it is the evidence that a candidate was published at all.
    authorized_winner = bool(str(candidate.get("fingerprint") or ""))
    material_improvement = (
        str(comparison.get("reason") or "") == "improved"
        and improvement_db is not None
        and required_db is not None
        and math.isclose(
            required_db, PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB, abs_tol=1e-9,
        )
        and improvement_db >= PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB
    )
    verify_regressed = (
        outcome == "fail"
        and str(verify.get("code") or "") != REASON_VERIFY_CROSSOVER_REGION
        if isinstance(verify, Mapping) else False
    )
    # ``accountability``'s "the forecast said worse" ledger value. Item 2 dropping
    # its refusal is what makes this reachable; it GRADES here, it does not gate.
    no_material_improvement = (
        str(comparison.get("reason") or "") == LEDGER_NOT_AN_IMPROVEMENT
        or improvement_db is not None
        and improvement_db < PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB
    )
    if tracking_status == CLAIM_FAIL or verify_regressed or no_material_improvement:
        result_outcome = RESULT_KEEP_PREVIOUS
    elif (
        outcome == "inconclusive"
        or tracking_status not in {CLAIM_PASS, CLAIM_FAIL}
    ):
        result_outcome = RESULT_INCONCLUSIVE
    elif (
        not authorized_winner or outcome != "pass"
        or absolute_status not in {CLAIM_PASS, CLAIM_FAIL}
    ):
        result_outcome = RESULT_INCONCLUSIVE
    elif absolute_status == CLAIM_PASS:
        result_outcome = RESULT_VERIFIED_TARGET
    elif material_improvement and None not in (absolute_miss_db, absolute_worst_hz):
        result_outcome = RESULT_VERIFIED_BEST_EVALUATED
    else:
        result_outcome = RESULT_INCONCLUSIVE
    cloud = block.get("cloud")
    post_apply = cloud.get(PHASE_CLOUD_VERIFY) if isinstance(cloud, Mapping) else None
    cloud_verdict = (
        post_apply.get("overall_passed") if isinstance(post_apply, Mapping) else None
    )
    # **A failed mark-VERIFY caps this badge whatever the group says** (#2464,
    # ruled 2026-08-19). ``cloud_verdict`` was tested FIRST, so a closed group
    # made the fail and inconclusive arms unreachable: a re-verify that failed
    # against a carried-forward passing group reached ``GRADE_GRADED`` with
    # ``graded=True``, and every surface keying on those read it as all clear.
    #
    # ``verify_failed`` is a UNION of the two instruments, not a fallback
    # between them, because neither can see the other's failure. ``outcome``
    # grades CAPTURE and tracking health only (``crossover_v2_flow.
    # _set_verify_outcome``; its pass call site says "Absolute remains
    # independent"), so a crossover-region claim that missed its tolerance
    # rides a clean ``pass`` — and the other way, an absent tracking max is an
    # ``outcome`` pass whose integration claim reads ``not_evaluated``, which
    # this union must not read as proved (#3487: a republished candidate has no
    # measure round to track against, and ``tracking_passed`` stays ``None``).
    # It reads ``integration`` and ``absolute`` because a
    # VERIFY grades no others: its one summed sweep leaves both per-branch
    # claims structurally ``not_evaluated`` (``CLAIM_NO_PER_BRANCH_CAPTURE``).
    # A state file with no claims block is a pre-R18 build and leaves
    # ``outcome`` standing alone: absence is never a fail, and never a
    # pass-of-claims either.
    #
    # #2160's rider (ratified 2026-08-17): geometry and k-of-N facts stay
    # un-co-located — each instrument's facts render on its own surface, and
    # capping this badge gathers none of them. ``spatial``,
    # ``post_apply_spec_passed`` and ``verify_outcome`` below are untouched.
    verify_failed = outcome == "fail" or CLAIM_FAIL in {
        tracking_status, absolute_status,
    }
    # **A capture that graded NO claim proved nothing at the mark** — which the
    # union above cannot say, because it looks for a failure and finds none.
    # Reachable since the gate stopped refusing an ungradeable tracking claim
    # (#3487): a republished candidate has no measure round to track against,
    # and a capture that also finds no trusted crossover region leaves the
    # absolute claim ``not_evaluated`` too. INDETERMINATE is the republish
    # door's own declared outcome for that shape and the mark badge is its
    # opposite. Only a claims RECORD says this: an absent block is a pre-R18
    # build and keeps the standing-alone rule stated above.
    no_claim_graded = bool(claims) and not {tracking_status, absolute_status} & {
        CLAIM_PASS, CLAIM_FAIL,
    }
    if verify_failed:
        state = GRADE_FAILED
    elif outcome == "inconclusive":
        state = GRADE_INCONCLUSIVE
    elif isinstance(cloud_verdict, bool):
        # A walked post-apply position group — the widest claim available, and
        # on a clean pass it is the wider claim, so it still wins the word. It
        # is a graded instrument in its own right, so it outranks the
        # ungraded-mark arm below rather than being capped by it.
        state = GRADE_GRADED
    elif no_claim_graded:
        state = GRADE_INCONCLUSIVE
    elif outcome == "pass":
        # Verified at the mark only. On express that is the whole grade by
        # design; on full it means VERIFY passed but the post-apply group has
        # not closed yet.
        state = GRADE_MARK_VERIFIED
    else:
        state = GRADE_UNVERIFIED
    spatial = _spatial_grade(post_apply)
    # Delivered width, derived from the evidence rather than from ``state``:
    # only a real spatial VERDICT is a spatial claim, so a group that closed
    # and could not grade anything reaches back to whatever the mark proved.
    if spatial in {GRADE_SPATIAL_PASSED, GRADE_SPATIAL_FAILED}:
        scope = GRADE_SCOPE_SPATIAL
    elif outcome == "pass":
        scope = GRADE_SCOPE_MARK
    else:
        scope = GRADE_SCOPE_NONE
    tier = str(block.get("tier") or "")
    if tier == TIER_FULL:
        complete = scope == GRADE_SCOPE_SPATIAL
    elif tier == TIER_EXPRESS:
        # Express promises the mark and structurally never walks a post-apply
        # group, so the mark IS its whole grade — complete, and explicitly
        # scoped. A spatial verdict would exceed the promise, never miss it.
        complete = scope in {GRADE_SCOPE_MARK, GRADE_SCOPE_SPATIAL}
    else:
        complete = scope != GRADE_SCOPE_NONE
    flatness = post_apply.get("flatness") if isinstance(post_apply, Mapping) else None
    flatness = flatness if isinstance(flatness, Mapping) else {}
    return {
        **({"outcome": result_outcome} if result_evidence else {}),
        "state": state,
        "graded": state in {GRADE_GRADED, GRADE_MARK_VERIFIED},
        "verify_outcome": outcome or None,
        "post_apply_spec_passed": cloud_verdict if isinstance(cloud_verdict, bool) else None,
        "scope": scope,
        "spatial": spatial,
        # Only alongside a real failing grade: a number without a verdict to
        # attach it to is the fabricated reading this module forbids.
        "spatial_worst_db": (
            _finite(flatness.get("max_db"))
            if spatial == GRADE_SPATIAL_FAILED else None
        ),
        "spatial_worst_hz": (
            _finite(flatness.get("max_hz"))
            if spatial == GRADE_SPATIAL_FAILED else None
        ),
        "complete": complete,
        "improvement_db": improvement_db,
        "tracking_passed": True if tracking_status == CLAIM_PASS else False if tracking_status == CLAIM_FAIL else None,
        "absolute_passed": True if absolute_status == CLAIM_PASS else False if absolute_status == CLAIM_FAIL else None,
        "absolute_miss_db": absolute_miss_db,
        "absolute_worst_hz": absolute_worst_hz,
        "candidate_fingerprint": str(candidate.get("fingerprint") or "") or None,
    }


# --------------------------------------------------------------------------- #
# conductor persistence
# --------------------------------------------------------------------------- #
#
# The document itself is :mod:`jasper.active_speaker.crossover_v2.durable_state`
# — every block builder, every payload shaper, every carry-forward rule and
# every reader that takes the document apart again. What stays here is the
# FILE: the path, the schema envelope, the atomic write, and the one decision
# that needs a live read of the state directory (the durability verdict, which
# the builder hands back rather than taking itself).
#
# Re-bound under their historical names because ``prepare_v2_session``'s
# verify-only stage reaches them as module globals and the stage-bridge suite
# names them off this module.
_finite = _durable._finite
_decimate_sum = _durable._decimate_sum
_decimate_delta = _durable._decimate_delta
_decimate_verify_measured = _durable._decimate_verify_measured
_candidate_summary = _durable._candidate_summary
_cloud_summary = _durable._cloud_summary
_delta_probe_summary = _durable._delta_probe_summary
verify_measured_curve_from_state = _durable.verify_measured_curve_from_state
entry_baseline_prior_from_state = _durable.entry_baseline_prior_from_state
alignment_prescription_prior_from_state = (
    _durable.alignment_prescription_prior_from_state
)
topology_prescription_prior_from_state = (
    _durable.topology_prescription_prior_from_state
)
blend_prescription_prior_from_state = _durable.blend_prescription_prior_from_state
blend_prescription_sha256_from_state = _durable.blend_prescription_sha256_from_state
pilot_transfer_prior_from_state = _durable.pilot_transfer_prior_from_state
commanded_delta_prior_from_state = _durable.commanded_delta_prior_from_state
declared_transfer_prior_from_state = _durable.declared_transfer_prior_from_state




def _take_staged_prescription(round_ordinal: int) -> tuple[Any, Any]:
    """This round's staged prescription as ``(blend, driver)`` (A9).

    The ONE place a staged prescription enters the flow, and the one place its
    refusal is turned into a round that carries on without it. Named and
    module-level for
    :func:`alignment_prescription_prior_from_state`'s reason — the seeding path
    is then drivable in a test without a capture.

    **Both classes, one door (PR-B).** ``accepts`` is
    :data:`~jasper.active_speaker.crossover_v2.prescription_spool.STAGEABLE_KINDS`
    because this round now routes both: a blend document becomes the candidate's
    ``blend_correction``, a per-driver one is merged by role onto the fit in its
    ``linearization``. This function and its two journal slugs dropped ``blend``
    from their names in the same edit — a name that is wrong for half the
    documents it describes is worse than a churned one, and the class-neutral
    spelling is the spool's own (``take_staged_prescription``,
    ``StagedPrescription``), so the door has one vocabulary rather than two.

    **The pair is returned already split, and the split is made HERE**, on the
    envelope's own class field rather than by ``isinstance`` —
    :class:`~jasper.active_speaker.crossover_v2.prescription_spool.StagedPrescription`
    states that rule for a caller that accepted both classes. Here rather than at
    the call site so the class vocabulary has ONE reader: a caller comparing
    kinds itself would be a second place to keep in step the day a third class
    exists. At most one arm is ever non-``None``; ``(None, None)`` is every
    ordinary round and every refused one.

    **Fail-open on the transport, fail-closed on the content.** A document that
    is stale, corrupt, oversized, tampered, or aimed where its class may not
    correct is REFUSED by name and never reaches the candidate; the round then
    runs the deterministic answer for that class exactly as it would have with
    no document at all — decision 10's banked instruction for the blend region,
    the Layer-1a fit's own output per driver. Those two directions are not in
    tension: the content gate protects the speaker from a correction nobody
    vouched for, and the round proceeding protects the household from losing a
    measurement session over an optional instruction it never asked for. The
    refusal is journal-visible rather than silent, because an operator who
    staged a prescription and got a deterministic round needs to be told which
    of the two happened.

    The take CONSUMES the document whether or not it is accepted — see
    :func:`~jasper.active_speaker.crossover_v2.prescription_spool.take_staged_prescription`
    — so a refusal here cannot repeat itself on the next round.
    """
    from jasper.active_speaker.crossover_v2.blend_prescription import (
        BlendPrescriptionRefused,
    )
    from jasper.active_speaker.crossover_v2.driver_prescription import (
        DRIVER_PRESCRIPTION_KIND,
        DriverPrescription,
    )
    from jasper.active_speaker.crossover_v2.prescription_spool import (
        STAGEABLE_KINDS,
        take_staged_prescription,
    )

    try:
        staged = take_staged_prescription(
            round_ordinal=round_ordinal, accepts=STAGEABLE_KINDS,
        )
    except BlendPrescriptionRefused as exc:
        log_event(
            logger, "correction.crossover_v2_prescription_refused",
            level=logging.WARNING, reason=exc.reason,
            round_ordinal=round_ordinal, detail=exc.detail,
        )
        return None, None
    if staged is None:
        return None, None
    is_driver = staged.prescription_kind == DRIVER_PRESCRIPTION_KIND
    log_event(
        logger, "correction.crossover_v2_prescription_taken",
        round_ordinal=round_ordinal,
        # WHICH CLASS was taken, under the spool's own field name.
        # ``prescription_class`` beside it is cut-vs-boost and has meant that
        # since the field shipped; naming the document's class with it too
        # would put two facts on one key.
        prescription_kind=staged.prescription_kind,
        prescription_class=staged.prescription.prescription_class,
        filters=len(staged.prescription.filters),
        # The per-driver class's deciding number: WHICH branches this document
        # replaces, and therefore which fitted ones it does not. Empty for the
        # blend class, whose correction is one region rather than a set of
        # roles — one event, one shape. The ``cast`` narrows, it does not
        # decide: the envelope's class field above already settled which type
        # this is, so an ``isinstance`` here would be a second opinion.
        roles=",".join(
            cast(DriverPrescription, staged.prescription).roles
            if is_driver else ()
        ),
        prescription_sha256=staged.prescription_sha256,
    )
    return (None, staged) if is_driver else (staged, None)


def _take_staged_angle_walk(
    plan_shape: Any,
    *,
    base_entries: int,
    lateral_group_present: bool,
    plans_cloud_group: bool,
    preset: Any,
    topology: Any,
) -> tuple[
    tuple[Any, ...], str, dict[int, Any], dict[str, float], tuple[Any, ...]
] | None:
    """This session's staged angle walk as
    ``(poses, consumer, specs, trims, claims)``, or ``None``.

    :func:`_take_staged_prescription`'s twin: ONE take, at ONE place. ``None``
    means NOTHING WAS STAGED — an ordinary session — and nothing else.

    A staged walk this session cannot honour REFUSES THE OPEN
    (:class:`CrossoverV2Refused`) with the producing module's own slug, rather
    than opening in the ordinary 3-capture shape and silently answering a
    different question. This runs before any state is opened, so the loud
    direction is also the cheap one; the document is single-use either way, so
    the operator restages after fixing what was named.

    ``specs`` is capture index -> the ``MeasureSpec`` that index plays, BUILT
    here rather than checked: the same objects the engine leg plays, so no
    second construction can disagree with the validated one. The design-axis
    MEASURE index always carries the walk-level spec; a STOP is in the map only
    when it names a candidate, which only the engine leg can install.
    ``claims`` is what each stop's graph CARRIED, for the pose records the flow
    banks.

    ``trims`` is the per-role attenuation a ``--level-matched`` walk carries,
    resolved HERE and empty for a walk that asked for none. Adoption is the one
    place that can both ask the evidence question and still refuse, so it is
    asked exactly once. A box with no measured evidence
    (:func:`~jasper.active_speaker.baseline_profile.measured_level_trims`)
    refuses with
    :data:`~jasper.active_speaker.angle_capture.WALK_LEVEL_MATCH_NO_EVIDENCE`.
    A datasheet estimate is deliberately not a fallback: it is physics about
    the driver model, not a measurement of this cabinet.

    ``consumed`` on the journal line is READ BACK from the spool, never
    asserted: its two unreadable arms deliberately do not consume, so a
    permissions mistake refuses every session rather than silently destroying
    the evidence of itself.

    ``lateral_group_present`` and ``plans_cloud_group`` are the session's own
    facts, passed in because the composing seam may not read session flags.

    A walk does not survive its session: the consumer is not persisted and the
    document is single-use, so a session that lapses mid-walk re-opens in its
    ordinary shape and the operator stages again.
    """
    from jasper.active_speaker.angle_capture import (
        WALK_CANDIDATE_NOT_MEASURABLE,
        WALK_LATERAL_GROUP_ALREADY_PLANNED,
        WALK_LEVEL_MATCH_NO_EVIDENCE,
        WALK_DELAY_NOT_ACCEPTED,
        WALK_POLARITY_NOT_ACCEPTED,
        WALK_STOP_NO_LONGER_VALID,
        LateralWalkRefused,
        candidate_measure_axes,
        session_lateral_walk,
    )
    from jasper.active_speaker.candidate_bank import (
        CandidateBankRefusal,
        find_banked_candidate,
    )
    from jasper.active_speaker.angle_capture_spool import (
        AngleRequestRefused,
        staged_angle_request_pending,
        take_staged_angle_request,
    )
    from jasper.active_speaker.crossover_v2.capture_plan import (
        build_v2_cloud_index_phase_map,
        position_angle_deg,
        position_elevation_deg,
    )
    from jasper.active_speaker.crossover_v2.contracts import (
        MEASURE_KIND_CANDIDATE,
        CrossoverV2FlowError,
    )
    from jasper.active_speaker.crossover_v2.journey import (
        LATERAL_CONSUMER_FORWARD_MODEL,
        PHASE_LATERAL,
        PHASE_MEASURE,
    )
    from jasper.active_speaker.crossover_v2.measure_spec import MeasureSpec
    from jasper.active_speaker.crossover_v2.spatial import TakeClaim

    def refused(reason: str, detail: str) -> CrossoverV2Refused:
        log_event(
            logger, "correction.crossover_v2_angle_walk_refused",
            level=logging.WARNING, reason=reason, detail=detail,
            # READ BACK, never asserted: the spool's two unreadable arms
            # deliberately do not consume.
            consumed=not staged_angle_request_pending(),
            session_continues=False,
        )
        # RETURNED, not raised, so every arm below reads ``raise refused(...)``
        # — the journal line and the refusal are one statement, and no arm can
        # log without refusing or refuse without logging.
        return CrossoverV2Refused(
            f"the staged angle walk was refused ({reason}): {detail}"
        )

    try:
        request = take_staged_angle_request()
        if request is None:
            return None
        if lateral_group_present:
            raise LateralWalkRefused(
                WALK_LATERAL_GROUP_ALREADY_PLANNED,
                "this session already walks a lateral group; a staged walk "
                "cannot be a second one",
            )
        prompts = session_lateral_walk(
            request,
            externally_positioned=plan_shape.externally_positioned,
            base_entries=base_entries,
            plans_cloud_group=plans_cloud_group,
        )
    except (AngleRequestRefused, LateralWalkRefused) as exc:
        raise refused(exc.reason, exc.detail) from exc
    except CrossoverV2FlowError as exc:
        # The third class: a banked stop the seam can no longer build. The spool
        # re-raises it un-wrapped because ``_validated_angle``'s own sentence
        # beats anything a second vocabulary could say, so it arrives with no
        # slug — it gets one here and keeps that sentence as the detail.
        raise refused(WALK_STOP_NO_LONGER_VALID, str(exc)) from exc
    try:
        # Its own arm rather than a fourth clause on the block above: only THIS
        # construction may be read as a polarity refusal, and a ``ValueError``
        # from anything else up there must not be relabelled as one.
        measure_spec = MeasureSpec(
            kind=MEASURE_KIND_CANDIDATE,
            polarity=request.polarity,
            inverted_role=request.inverted_role,
            delayed_role=request.delayed_role,
            delay_us=request.delay_us,
            level_matched=request.level_matched,
        )
    except ValueError as exc:
        # Attributed by which half the request STATED, never by re-judging
        # validity here — that rule has one owner and a second copy drifts. A
        # request stating a delay reads as a delay refusal; the detail is the
        # spec's own sentence either way, so it always names the real field.
        stated_delay = bool(request.delayed_role or request.delay_us)
        raise refused(
            WALK_DELAY_NOT_ACCEPTED if stated_delay else WALK_POLARITY_NOT_ACCEPTED,
            str(exc),
        ) from exc
    level_trims, trim_source = _resolve_measurement_level_trims(
        measure_spec, preset=preset, topology=topology,
    )
    if measure_spec.level_matched and not level_trims:
        raise refused(
            WALK_LEVEL_MATCH_NO_EVIDENCE,
            "this walk asks the measurement graph to level-match the driver "
            "branches, and this speaker has no measured per-driver level "
            "evidence to match them by; run the driver trim step, or stage "
            "the walk without --level-matched",
        )
    candidate_ids = tuple(stop.candidate_id for stop in request.stops)
    try:
        axes_by_candidate = {
            candidate_id: candidate_measure_axes(
                find_banked_candidate(candidate_id).candidate
            )
            for candidate_id in sorted(set(candidate_ids) - {""})
        }
    except LateralWalkRefused as exc:
        raise refused(exc.reason, exc.detail) from exc
    except CandidateBankRefusal as exc:
        # The bank's own vocabulary, unwrapped: a second slug for "no such
        # candidate" would send an operator looking in the wrong place.
        raise refused(exc.code, exc.detail) from exc
    # Asked of the ONE owner of this session's index space rather than counted
    # here, so the specs cannot be keyed to captures the plan never runs.
    walk_index_phase = build_v2_cloud_index_phase_map(
        plan_shape=plan_shape,
        include_cloud_measure=plans_cloud_group,
        include_lateral=True,
        lateral_prompts=prompts,
    )
    specs_by_index: dict[int, Any] = {
        index: measure_spec
        for index, phase in walk_index_phase.items()
        if phase == PHASE_MEASURE
    }
    try:
        for index, prompt, stop in zip(
            sorted(
                i for i, phase in walk_index_phase.items()
                if phase == PHASE_LATERAL
            ),
            prompts,
            request.stops,
        ):
            if not stop.candidate_id:
                continue
            specs_by_index[index] = MeasureSpec(
                kind=MEASURE_KIND_CANDIDATE,
                positions=(stop.angle_deg,),
                vertical_deg=stop.elevation_deg,
                pose_prompts=(prompt.text,),
                candidate_id=stop.candidate_id,
                # The level match stays the WALK's: the trims are the
                # speaker's own and are resolved once above, never per
                # candidate.
                level_matched=request.level_matched,
                **axes_by_candidate[stop.candidate_id],
            )
    except ValueError as exc:
        # The BACKSTOP behind ``candidate_measure_axes``'s normalisation: the
        # axes here come from the candidate rather than the operator, so a pair
        # this spec refuses is a candidate this walk cannot play — refused in
        # the spec's own sentence rather than escaping the open as a 500 with
        # the document already consumed.
        raise refused(WALK_CANDIDATE_NOT_MEASURABLE, str(exc)) from exc
    lateral_claims = tuple(
        TakeClaim(
            candidate_id=stop.candidate_id,
            # WHAT THE STOP'S GRAPH CARRIED, never the walk's default: a stop
            # that played a candidate's flipped branch under the speaker's own
            # level match must not bank as an ordinary pose.
            polarity=axes_by_candidate[stop.candidate_id]["polarity"],
            level_matched=measure_spec.level_matched,
            level_match_trims_db=dict(level_trims) or None,
        ) if stop.candidate_id else TakeClaim()
        for stop in request.stops
    )
    log_event(
        logger, "correction.crossover_v2_angle_walk_taken",
        stops=len(prompts),
        angles=",".join(f"{position_angle_deg(p):+d}" for p in prompts),
        elevations=",".join(f"{position_elevation_deg(p):+d}" for p in prompts),
        mover=request.mover,
        regimes=",".join(sorted({stop.regime for stop in request.stops})),
        polarity=request.polarity,
        inverted_role=request.inverted_role,
        delayed_role=request.delayed_role,
        delay_us=request.delay_us,
        level_matched=request.level_matched,
        # WHICH evidence answered, so a take's receipts name the source of the
        # gains its graph carries instead of leaving a reader to guess between
        # the banked trim and the guided captures. Empty on an unmatched walk.
        level_match_source=trim_source,
        level_match_trims_db=",".join(
            f"{role}:{db:g}" for role, db in sorted(level_trims.items())
        ),
        candidates=",".join(sorted(set(candidate_ids) - {""})),
        consumer=LATERAL_CONSUMER_FORWARD_MODEL,
    )
    return (
        prompts,
        LATERAL_CONSUMER_FORWARD_MODEL,
        specs_by_index,
        level_trims,
        lateral_claims,
    )


def _resolve_measurement_level_trims(
    spec: Any, *, preset: Any, topology: Any,
) -> tuple[dict[str, float], str]:
    """This box's own per-driver level match, and which evidence answered.

    ``({}, "")`` for a spec that asks for none — the ordinary walk, which pays
    nothing: no statefile is read and no preview is loaded.

    The precedence is NOT decided here.
    :func:`~jasper.active_speaker.baseline_profile.measured_level_trims` is the
    one owner of which evidence source wins, and this function
    hands it the same two inputs the applied profile's own build hands it, so
    the graph a measurement plays through is levelled by the same evidence the
    speaker would be levelled by.

    **No/unreadable evidence answers empty WITHOUT raising, and there is no
    catch to dress a genuine fault up as no-evidence.** Both loaders fail soft
    — an absent, unreadable or corrupt-but-readable document returns a status
    dict, never a raise (``measurement._normalise_state`` and the preview
    loader both narrow a non-mapping back to a base document) — and the
    estimator is fail-closed, answering empty trims for every unusable-evidence
    case. So a box with nothing to level by reaches the caller's
    ``WALK_LEVEL_MATCH_NO_EVIDENCE`` refusal through the empty return, and NO
    exception is expected here at all. There is therefore nothing to catch: an
    exception that does arise is a real fault in the derivation, and it
    propagates with its traceback pointing straight at this function rather
    than being swallowed and misread as "this box has not measured its trims".
    """
    if not spec.level_matched:
        return {}, ""
    from jasper.active_speaker.baseline_profile import measured_level_trims
    from jasper.active_speaker.crossover_preview import load_crossover_preview
    from jasper.active_speaker.measurement import load_measurement_state

    trims, meta = measured_level_trims(
        preset,
        load_measurement_state(topology) or {},
        load_crossover_preview() or {},
    )
    return (
        {str(role): float(db) for role, db in trims.items()},
        str(meta.get("source") or ""),
    )




def _fc_hz_label(hz: float) -> str:
    """A crossover frequency as the household reads it: ``2250``, ``1787.5``.

    One formatter, because the apply path's refusal copy and the republish
    door's name the same numbers to the same person, and two spellings of one
    frequency in two adjacent sentences is a defect the household is the
    first to notice.
    """
    return f"{hz:.1f}".rstrip("0").rstrip(".")



def persist_conductor_state(
    conductor: Any,
    *,
    failure_code: str | None,
    evidence: Mapping[str, Any] | None = None,
    failure_refusals: Sequence[str] = (),
) -> None:
    """Write the conductor's durable snapshot + host-observed failure state.

    ``failure_refusals`` are the underlying admission-refusal slugs behind a
    program failure (issue #1820). They are FORENSICS, never household copy:
    the envelope renders ``failure["code"]`` through the reason registry and
    ignores this key. It exists so a support read of the state file can tell
    which of ``program_unplayable``'s several causes actually fired, which the
    old single-code collapse erased.

    The DOCUMENT — every key, every carry-forward rule, and the reason each one
    is scoped the way it is — belongs to
    :func:`~jasper.active_speaker.crossover_v2.durable_state.build_conductor_state`.
    What is left here is the write: read the state being replaced, hand it over,
    put the answer back, and journal the one transition a household would
    notice.
    """
    from jasper.active_speaker.crossover_envelope_v2 import crossover_v2_phase

    from .correction_crossover_v2_status import crossover_v2_status_block

    prior = load_v2_state() or {}
    built = build_conductor_state(
        conductor, prior,
        failure_code=failure_code,
        evidence=evidence,
        failure_refusals=failure_refusals,
    )
    session_id = built.state["session_id"]
    # Read BEFORE the write: this is the grade the household is currently
    # looking at, and ``crossover_v2_status_block`` reads the state file.
    prior_grade = (crossover_v2_status_block() or {}).get("post_apply_grade")
    prior_outcome = (
        str(prior_grade.get("outcome") or "")
        if isinstance(prior_grade, Mapping)
        and prior.get("session_id") == session_id else ""
    )
    save_v2_state(built.state, durable=built.durable)
    from jasper.active_speaker.crossover_v2.journey import PHASE_DONE

    grade = (crossover_v2_status_block() or {}).get("post_apply_grade")
    grade = grade if isinstance(grade, Mapping) else {}
    was_done = crossover_v2_phase(
        prior, review_declined=review_declined(prior),
    ) == PHASE_DONE
    now_done = crossover_v2_phase(
        built.state, review_declined=review_declined(built.state),
    ) == PHASE_DONE
    if (now_done and not was_done) or (
        grade.get("outcome") == RESULT_KEEP_PREVIOUS
        and prior_outcome != RESULT_KEEP_PREVIOUS
    ):
        log_event(
            logger, "correction.crossover_v2_result_classified",
            session_id=session_id, outcome=grade.get("outcome") or RESULT_INCONCLUSIVE,
            improvement_db=grade.get("improvement_db"),
            tracking_passed=grade.get("tracking_passed"), absolute_passed=grade.get("absolute_passed"),
            absolute_miss_db=grade.get("absolute_miss_db"), absolute_worst_hz=grade.get("absolute_worst_hz"),
            candidate_fingerprint=grade.get("candidate_fingerprint"),
        )


def _persist_terminal_failure(
    conductor: Any, code: str, *, refusals: Sequence[str] = (),
) -> bool:
    """Session-terminal persistence (§5.6): pre-apply, capture evidence dies
    with the session (restart at CHECK); post-apply, the applied candidate +
    verify priors survive so ``/v2/verify`` can re-arm.

    SF2 (adversarial review, 2026-07-20): ``REASON_APPLY_FAILED`` is exempted
    from the pre-apply evidence reset. The §5.6 rationale for wiping
    ``accepted_phases``/``gain_plan_db`` is that a DEAD session makes the mic
    position unverifiable — but an auto-apply that came back blocked or
    errored says nothing about the mic position; MEASURE's own evidence is
    still exactly as good as it was. Keeping MEASURE accepted here is what
    lets ``crossover_v2_phase`` resolve to ``PHASE_APPLYING`` (not
    ``PHASE_CHECK``) so the envelope's apply-step failure screen — and the
    specific blocked-issue nudge layered onto it — can actually render;
    before this fix the reset always won, so that nudge was unreachable in
    production (only reachable by injecting the phase directly in a test).
    """
    from jasper.active_speaker.crossover_v2.refusal_copy import REASON_APPLY_FAILED

    prior = load_v2_state()
    session_id = str(getattr(conductor, "session_id", ""))
    prior_verify = (prior or {}).get("verify")
    prior_outcome = str(
        (prior_verify or {}).get("outcome")
        if isinstance(prior_verify, Mapping)
        else ""
    )
    if (
        isinstance(prior_verify, Mapping)
        and prior_outcome in {"pass", "fail", "inconclusive"}
        and (prior or {}).get("session_id") == session_id
    ):
        # consume() persists VERIFY before publishing capture_result. Later
        # trouble is a cleanup fault, not a commissioning verdict.
        log_event(
            logger,
            "correction.crossover_v2_terminal_verdict_preserved",
            level=logging.WARNING,
            session_id=session_id,
            outcome=prior_outcome,
            verdict_code=prior_verify.get("code") or "",
            cleanup_fault_code=code,
        )
        return True

    persist_conductor_state(
        conductor, failure_code=code, failure_refusals=refusals,
    )
    state = load_v2_state()
    if state is None:
        return False
    if not state.get("applied") and code != REASON_APPLY_FAILED:
        state["accepted_phases"] = []
        state["gain_plan_db"] = None
    save_v2_state(state)
    return False


# --------------------------------------------------------------------------- #
# production seam bindings (S1a/S1e)
# --------------------------------------------------------------------------- #


def _wav_bytes_to_samples(wav_bytes: bytes) -> tuple[Any, int]:
    """This binding's decode, now owned beside its encoder.

    Lifted to :func:`~jasper.audio_measurement.wired_capture.decode_wav_to_mono`
    so the engine's offline ``analyze`` can decode a banked capture without
    reaching into ``jasper.web`` — the dependency runs the other way, and this
    was the one piece of the analyze-seam assembly the truth layer needed.
    """
    from jasper.audio_measurement.wired_capture import decode_wav_to_mono

    return decode_wav_to_mono(wav_bytes)


def resolve_setup_calibration(setup: Any, device: Any) -> Any:
    """The production mic-calibration resolver for a v2 capture.

    Consumes ``household_mic.resolve_setup_calibration`` — the ONE point the
    capture's ``setup.calibration`` reference becomes a stored
    ``CalibrationRecord``. Returns the record, or ``None`` when the capture
    declared no calibration or its reference names a DIFFERENT mic than the
    one this capture reports (the 2026-07-20 incident). ``device`` is this
    capture's realized input device (``CaptureAnswer.device``) — threaded
    through so that mismatch is caught where the calibration is resolved for
    THIS capture, not applied blind to whichever mic actually recorded.
    """
    from jasper.correction.household_mic import resolve_setup_calibration as resolve

    from .correction_setup import _calibration_root, _household_mic_path

    return resolve(
        setup if isinstance(setup, Mapping) else None,
        device=device if isinstance(device, Mapping) else None,
        root=_calibration_root(),
        path=_household_mic_path(),
    )


def default_setup_calibration_for_v2() -> Any | None:
    """The v2 session's OPTIONAL household-mic prefill hint (W6.12).

    Every v2 capture logged ``crossover_v2_uncalibrated_capture`` even when
    the household had a resolvable stored mic (a UMIK-2 by serial, ingested
    via ``/sound/room/calibration/fetch``). Root cause:
    ``resolve_setup_calibration`` is only as good as the reference the capture
    carries in ``setup.calibration``, and a v2 session has no
    calibration-picker screen of its own (design: CHECK's own pilot pairs
    solve gain), so nothing carried the household's remembered mic into it.

    Reuses ``correction_setup._default_setup_calibration_for_spec`` — the ONE
    household-mic-hint resolver. Threaded into
    ``build_v2_session_spec``/``build_v2_verify_session_spec`` via their
    shared ``**spec_kwargs`` forward to ``build_crossover_sweep_spec``, and
    the measurement source mints the capture's own reference from it
    (``wired_capture.mint_wired_answer``). Fail-soft: any
    resolution miss yields no hint, never blocks session open.
    """
    from .correction_setup import _default_setup_calibration_for_spec

    try:
        return _default_setup_calibration_for_spec()
    except (OSError, RuntimeError, ValueError):
        log_event(
            logger,
            "correction.crossover_v2_default_calibration_hint_failed",
            level=logging.WARNING,
        )
        return None


def _setup_calibration_observation(setup: Any) -> tuple[str, str]:
    """What the capture's own setup reference held, redacted-safe (W6.13).

    Returns ``(mode, calibration_id)`` for the uncalibrated-capture WARN so a
    live journal line settles empirically whether the capture carried NO setup
    at all (``mode="absent"``) or one whose calibration didn't resolve (e.g.
    ``mode="none"``, or a stale ``calibration_id``). Only the mode and the
    calibration_id (a stored-record id, not a secret) are ever extracted.
    """
    if not isinstance(setup, Mapping):
        return "absent", ""
    calibration = setup.get("calibration")
    if not isinstance(calibration, Mapping):
        return "absent", ""
    return (
        str(calibration.get("mode") or ""),
        str(calibration.get("calibration_id") or ""),
    )


class CaptureEvidenceCarry:
    """The analyze seam's one-capture handoff of the blocks a take banks.

    Same single-shot discipline, and for the same reason, as
    :class:`~jasper.active_speaker.capture_provenance.CaptureProvenanceRecorder`
    — read that class for why ``take`` consumes. A separate slot rather than a
    second field on that one because the two hops answer different questions
    and are fed by different seams: the play seam observes the graph and the
    fader, and only the analyze seam has ever held the analysis.

    ``record`` overwrites unconditionally, so nothing has to be drained first:
    every analyze produces a block set (``diagnostic`` at minimum), so a
    refused capture's blocks are always replaced by the next analyze rather
    than stranded for the next accepted take to pick up.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: Mapping[str, Any] | None = None

    def record(self, blocks: Mapping[str, Any]) -> None:
        with self._lock:
            self._pending = blocks

    def take(self) -> Mapping[str, Any] | None:
        with self._lock:
            pending, self._pending = self._pending, None
        return pending


def _bankable(value: Any) -> Any:
    """One JSON document with unbankable floats nulled, recursively.

    NOT decoration. ``CommissioningEvidenceStore`` canonicalises with
    ``allow_nan=False``, so a single ``NaN`` anywhere in a banked record is a
    ``MALFORMED`` refusal — and the retention seam fail-softs, which would
    lose the WHOLE take record over one unmeasurable diagnostic. Since the
    point of carrying these blocks is to stop losing data, an unmeasurable
    number becomes ``null``.

    **Keys are never dropped, only their values nulled**, and that is the whole
    difference between a scrub and a lie. ``analysis_diagnostic_summary``
    spends tri-states deliberately — ``polarity_agrees_with_sum`` is ``None``
    for "nobody cross-checked" against an absent key for "no alignment at all",
    and the ``frame_*`` block is "present with ``None`` terms when the
    comparison ran but no frame could be fitted; absent only when no
    comparison happened" — so a pass that removed empty keys would flatten
    those two answers into one, permanently, on a write-once record.

    Floats only. An unbounded JSON integer serializes exactly, so ``int`` is
    left alone and only the type that can BE ``NaN``/``inf`` is screened —
    through :func:`_finite`, this module's one "is that a usable number?"
    test, rather than a second spelling of it. A non-native number (a
    ``numpy`` scalar, an array) is NOT screened here and would cost the record
    at the store's own ``TypeError``; no field on today's three blocks is one,
    and :func:`_capture_evidence_blocks` names that contract.
    """
    if isinstance(value, float):
        return _finite(value)
    if isinstance(value, Mapping):
        return {key: _bankable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_bankable(item) for item in value]
    return value


def _add_capture_block(
    blocks: dict[str, Any], name: str, build: Callable[[], Any],
) -> None:
    """Add one evidence block, or lose that block and nothing else.

    The belt the deleted ring writer carried in as many words — *"ANY failure
    here must never affect the measurement itself"* — kept rather than dropped
    with it. This runs inside the analyze seam, so a raise costs the CAPTURE:
    the sweep played, the operator is standing at the mark, and a diagnostic
    that could not be summarised would take the measurement with it.

    Per block, not around all three, so a raise while summarising the analysis
    still leaves the frame ledger banked. The caught tuple is concrete rather
    than blind for the reason the shapes below are real: ``AttributeError`` and
    ``TypeError`` are what a half-populated or foreign analysis produces, and
    ``ValueError`` is what a hostile mapping produces. A genuinely unexpected
    type still propagates to the analyze seam's own callers.
    """
    try:
        blocks[name] = _bankable(build())
    except (AttributeError, TypeError, ValueError):
        log_event(
            logger, "correction.crossover_v2_capture_evidence_block_failed",
            level=logging.WARNING, block=name, exc_info=True,
        )


def _capture_evidence_blocks(result: Any, analysis: Any) -> dict[str, Any]:
    """The three blocks a banked take carries about the capture it IS.

    ``diagnostic`` is
    :func:`~jasper.audio_measurement.program_analysis.analysis_diagnostic_summary`
    — the flat numeric account that makes a banked take self-describing
    without replaying the analysis. ``capture_integrity`` is the RECORDER's
    own per-take counters (the wired chain's
    ``build_capture_integrity_report``), written
    verbatim so a reader sees the counts as the recorder reported them.
    ``frame_ledger`` is the reconciliation of those counters against the
    frames that actually arrived — the one thing that names WHICH hop lost a
    quantum.

    **``capture_integrity`` names two different things in one record, and they
    are not the same fact.** This block is the recorder's RAW counters, off the
    capture result. The ``integrity_*`` fields inside ``diagnostic`` are the
    analysis's own evaluated ``CaptureIntegrity`` verdict — computed during the
    capture, on the signal. A reader comparing the two is comparing a report
    against a judgement, not one number against itself.

    Written only when there is something to write: an absent block is a
    capture that reported none, never a clean take claimed on its behalf.

    **NOT total, and guarded because of it.** The summary is defensive at its
    top level but its nested reads are bare once a sub-object exists
    (``drift.epsilon_ppm``, ``alignment.confidence``,
    ``candidate.predicted_ripple_db``, and a ``math.isfinite`` over a pilot's
    ``snr_db`` that raises ``TypeError`` on a non-number), and
    ``ledger.to_dict()`` is a method call rather than a ``getattr`` default.
    A half-populated analysis reaches every one of those. So each block is
    built through :func:`_add_capture_block`, which trades the block for the
    capture and never the other way round.

    One hazard this does NOT cover, stated so a future analysis change knows
    the contract: a non-native number (a ``numpy`` scalar, an array) raises
    inside the STORE's canonical JSON, past this guard, and the retention
    seam's fail-soft would drop the whole record. Every field on today's three
    blocks is Python-native — the summary ``round(float(...))``s its own, the
    ledger's are ``int``, and the page's arrive through ``json.loads`` — so
    this is a contract to keep, not a live defect.
    """
    from jasper.audio_measurement import program_analysis as _pa

    blocks: dict[str, Any] = {}
    _add_capture_block(
        blocks, "diagnostic", lambda: _pa.analysis_diagnostic_summary(analysis),
    )
    report = getattr(result, "capture_integrity", None)
    if isinstance(report, Mapping) and report:
        _add_capture_block(blocks, "capture_integrity", lambda: dict(report))
    ledger = getattr(analysis, "frame_ledger", None)
    if ledger is not None:
        # A lambda and not ``ledger.to_dict``: the bound-method LOOKUP is
        # itself an attribute read, and passing it would raise while building
        # the argument — outside the guard that exists to catch exactly that.
        _add_capture_block(blocks, "frame_ledger", lambda: ledger.to_dict())
    return blocks


def bind_production_analyze(
    *,
    resolve_calibration: Callable[[Any, Any], Any] | None = resolve_setup_calibration,
    meta: dict[str, Any] | None = None,
    provenance: CaptureProvenanceRecorder | None = None,
    carry: CaptureProvenanceRecorder | None = None,
    evidence: CaptureEvidenceCarry | None = None,
) -> "AnalyzeCapture":
    """The real ``analyze`` seam: CaptureResult → ``analyze_program_capture``.

    Design §5.6.4 applies the mic cal to every gated response, so this binding
    resolves the calibration from the capture's phone-reported setup (the same
    machinery the legacy flows use)
    and threads BOTH the resolved curve and the conductor's declared geometry
    into ``analyze_program_capture``. When no calibration resolves, the
    analysis still runs — relative timing/level stay valid per the design —
    but the fact is never silent: a WARN ``event=`` fires and ``meta``
    (persisted with the session's evidence refs) records the per-phase
    ``{"applied": False}`` annotation.

    ``phase`` (required, keyword-only) is the conductor's own flow phase —
    ``crossover_v2_flow.CrossoverV2Session.consume_capture`` always passes it,
    and ``crossover_v2_flow.AnalyzeCapture`` declares it. It is NOT the same
    value as ``program.phase``: every cloud position plays the verify-shaped
    summed sweep, so ``program.phase == "verify"`` even during
    PHASE_CLOUD_MEASURE/PHASE_CLOUD_VERIFY. It keys the per-phase calibration
    annotation and labels this binding's log lines, so those name the capture
    rather than the shared program object.

    ``provenance`` (optional) is the session's
    :class:`~jasper.active_speaker.capture_provenance.CaptureProvenanceRecorder`
    — the same object ``bind_production_play`` records into, and the only way a
    banked take can name the graph it went through.

    ``carry`` (optional) is the SECOND recorder, the one the banking seam
    drains. The shot stays single and stays here, because this is the only
    place in a capture's life that runs exactly once between the play that
    observed the graph and the arm that decides whether to bank. Re-recorded
    rather than re-observed: ``CaptureProvenance`` is a snapshot the play seam
    already took, so the second hop moves bytes, never readings. See
    ``bind_position_retention`` for the drain.

    ``evidence`` (optional) is the analyze seam's OWN handoff to that same
    banking seam: the ``diagnostic``/``capture_integrity``/``frame_ledger``
    blocks, which exist nowhere else in a capture's life. This is the only
    moment they can be taken — the analysis is rewritten inside the round and
    the capture bytes are gone by the time anything reads the bundle — so a
    binding without one computes them and drops them, which is the data-loss
    window the dump ring's death opened. See :func:`_capture_evidence_blocks`.
    """

    def _analyze(
        program: Any, result: Any, priors: Any, geometry: Any, *, phase: str,
    ) -> Any:
        from jasper.audio_measurement import program_analysis as _pa
        from jasper.audio_measurement.calibration import mic_tier_for_model

        wav = getattr(result, "wav", result)
        samples, rate = _wav_bytes_to_samples(wav)
        setup = getattr(result, "setup", None)
        record = None
        if resolve_calibration is not None:
            try:
                record = resolve_calibration(
                    setup, getattr(result, "device", None)
                )
            except (OSError, RuntimeError, TypeError, ValueError):
                # A resolver failure downgrades to an annotated-uncalibrated
                # analysis, never a crashed capture — but it is logged.
                log_event(
                    logger,
                    "correction.crossover_v2_calibration_resolve_failed",
                    level=logging.WARNING,
                    phase=phase,
                )
                record = None
        curve = getattr(record, "curve", None)
        if record is not None and curve is None:
            # A bare CalibrationCurve (tests / future callers) is accepted too;
            # anything else stays None (annotated uncalibrated, never a crash).
            from jasper.audio_measurement.calibration import CalibrationCurve

            if isinstance(record, CalibrationCurve):
                curve = record
        if curve is None:
            # W6.13 round-5 diagnostic: name what the phone-reported setup
            # actually held at resolve time so a live journal line
            # distinguishes "the phone sent nothing" (setup_mode=absent)
            # from "the phone sent a choice that didn't resolve"
            # (setup_mode=none/stored/..., with its id). Redacted-safe —
            # see _setup_calibration_observation.
            setup_mode, setup_calibration_id = _setup_calibration_observation(
                setup
            )
            log_event(
                logger,
                "correction.crossover_v2_uncalibrated_capture",
                level=logging.WARNING,
                phase=phase,
                setup_mode=setup_mode,
                setup_calibration_id=setup_calibration_id,
            )
        if meta is not None:
            meta.setdefault("calibration", {})[phase] = {
                "applied": curve is not None,
                "calibration_id": getattr(record, "calibration_id", None),
            }
        # Layer-1a linearization gate input (#1668 PR-C): resolve the
        # measurement mic's correction-envelope trust tier from the SAME
        # resolved calibration record this binding already computed above —
        # no second resolve, no new failure mode. `record` is `None` (no
        # calibration resolved) or lacks a `model` attribute (a bare
        # CalibrationCurve test double) exactly as often as `curve` above,
        # and `mic_tier_for_model(None)` already resolves to the
        # conservative "phone" tier for that case — never a guess at
        # "reference". Threaded onto every phase's priors (not just
        # MEASURE); only `ProgramAnalysis` from a MEASURE analysis actually
        # surfaces it (see program_analysis.ProgramAnalysis.mic_tier).
        #
        # `mic_calibrated` rides the SAME replace call, from the SAME `curve`
        # this function already resolved above — the household-facing sibling
        # of the `meta["calibration"]` annotation a few lines up, which
        # nothing reads back for a screen (audit gauntlet 5a). Threaded onto
        # every phase's priors for the same reason `mic_tier` is; only a
        # MEASURE analysis has a consumer today
        # (CrossoverV2Session._measure_verdict).
        priors = dataclasses.replace(
            priors,
            mic_tier=mic_tier_for_model(getattr(record, "model", None)),
            mic_calibrated=curve is not None,
        )
        analysis = _pa.analyze_program_capture(
            program,
            samples,
            rate,
            calibration=curve,
            geometry=geometry,
            priors=priors,
            # #2094: the phone's own frame counters, reconciled against the
            # frames just decoded. This seam is the ONLY place both halves of
            # the ledger exist — the page's account arrives on the status
            # event channel, the received count comes out of the WAV — so it is
            # the only place the comparison can be made.
            capture_report=getattr(result, "capture_integrity", None),
        )
        # THIS capture's stimulus, consumed ONCE: a second analyze with no
        # play between gets ``None``, never the last capture's context. The
        # banking seam is its one consumer, reached through ``carry``.
        taken = provenance.take() if provenance is not None else None
        if carry is not None:
            # DRAINED FIRST, unconditionally, and that is not tidiness. Banking
            # is accepted-only, so a REFUSED capture leaves whatever this
            # analyze put in the carry with nobody to take it out. The next
            # accepted capture whose own observation missed would then drain a
            # value belonging to a capture that never became evidence, and
            # write it into a write-once forensic record naming the wrong
            # graph and the wrong fader. ``record`` cannot clear that by
            # itself: the case that strands a value is exactly the case where
            # there is no new value to overwrite it with.
            carry.take()
            if taken is not None:
                carry.record(taken)
        if evidence is not None:
            # No drain-first, unlike the carry above: this block set is never
            # empty, so a refused capture's blocks are overwritten here rather
            # than stranded for the next accepted take to drain.
            evidence.record(_capture_evidence_blocks(result, analysis))
        return analysis

    return _analyze


def open_v2_evidence_store(topology: Any) -> tuple[Any, str]:
    """Open a fresh v2 commissioning bundle + its exact evidence store (§5.6).

    Every v2 measurement session gets its own retention-bounded bundle under
    ``sessions_dir()`` (the same SC-4 bundle machinery the legacy flow uses),
    and every phase artifact is published through the store's write-once +
    tamper-checked-reopen path. Returns ``(store, bundle_session_id)``.
    """
    from jasper.active_speaker.bundles import open_bundle
    from jasper.active_speaker.commissioning_evidence_store import (
        CommissioningEvidenceStore,
    )

    info = open_bundle(topology, calibration_id="")
    if not isinstance(info, Mapping) or not info.get("session_id"):
        raise CrossoverV2Refused(
            "could not open a commissioning evidence bundle for this session"
        )
    session_id = str(info["session_id"])
    store = CommissioningEvidenceStore.open(
        Path(str(info["bundle_dir"])), expected_session_id=session_id
    )
    return store, session_id


_T = TypeVar("_T")


def _record_store(store: Any, capture_session_id: str) -> Any:
    """THE durable-write seam for this session's evidence (ADR-0227 §12).

    A frozen dataclass over the same bundle, so the binders that each build one
    are one writer constructed several times and never several authorities.
    """
    from jasper.active_speaker.crossover_v2.record_store import BankedRecordStore

    return BankedRecordStore(evidence=store, capture_session_id=capture_session_id)


def _bank(
    records: Any, run_async: Any, record: Mapping[str, Any],
) -> tuple[str, Any]:
    """Bank one record; answer its store id and the artifact it wrote.

    The store owns the path, the envelope, the discriminator and the
    reopen-and-compare, and answers with the id that finds the record again;
    the identity every ``refs`` column and every citation needs is re-read from
    it. Driven through ``run_async`` because the publishing seams are
    synchronous all the way up from ``consume_capture``, on a worker thread.
    """
    from jasper.active_speaker.commissioning_evidence_store import EVIDENCE_ROOT

    record_id = str(run_async(records.bank(record)))
    return record_id, records.evidence.identify_artifact(
        f"{EVIDENCE_ROOT}/artifacts/{record_id}"
    )


def _bank_findings(
    records: Any, run_async: Any, *, phase: str, finding_set: Any,
) -> Any:
    """Bank one phase's finding set; answer the artifact it wrote.

    ``phase`` rides the record to ROUTE it — per phase and not per session,
    because the two groups close at different times and the store is write-once
    — and the route takes it back off: the file is ``FindingSet.to_dict()``.
    """
    _, artifact = _bank(
        records, run_async, {**finding_set.to_dict(), "phase": phase},
    )
    return artifact


def _fail_soft(work: Callable[[], _T], *, event: str, **fields: Any) -> _T | None:
    """Run one durable write; log ``event`` and answer ``None`` if it refused.

    The fail-soft boundary, at the caller and never in the store (ADR-0227
    §12): the store stays strict — ``publish_json_artifact`` raises rather than
    dropping an artifact — so every OTHER caller keeps the strictness it was
    built for. Each caller passes its own shipped event name and fields.
    """
    try:
        return work()
    except (OSError, RuntimeError, TypeError, ValueError):
        log_event(logger, event, level=logging.WARNING, exc_info=True, **fields)
        return None


def bind_evidence_publishers(
    store: Any, capture_session_id: str, run_async: Any
) -> tuple[Callable[[Any, Mapping[str, Any]], None], Callable[[Any], None], dict[str, Any]]:
    """Real ``publish_check`` / ``publish_candidate`` seams (§5.6).

    CHECK banks the ambient report + solved gain plan; MEASURE banks the full
    candidate dict, which the store re-opens through
    ``MeasuredCrossoverCandidate.from_mapping`` — the same tamper check the
    apply path runs, so a candidate that cannot survive exact reopen never
    becomes reviewable. Artifact fingerprints land in the returned ``refs``
    mapping (persisted into the durable state for the status surface).

    Neither is fail-soft, and that is the shipped behaviour: a CHECK or MEASURE
    whose evidence did not land has nothing for the household to review.
    """
    from jasper.active_speaker.crossover_v2.record_store import CHECK_EVIDENCE_KIND

    records = _record_store(store, capture_session_id)
    refs: dict[str, Any] = {"bundle_session_id": store.session_id}

    def publish_check(gain_plan: Any, ambient_report: Mapping[str, Any]) -> None:
        _, artifact = _bank(records, run_async, {
            "kind": CHECK_EVIDENCE_KIND,
            "gain_plan_db": dict(gain_plan.gain_db),
            "predicted_peak_dbfs": gain_plan.predicted_peak_dbfs,
            "snr_floor_ok": gain_plan.snr_floor_ok,
            # #1825: the per-role derivation behind ``gain_plan_db`` — which
            # limit chose each driver's MEASURE level and the ambient evidence
            # it rests on. Empty for a legacy plan that carries no solves
            # (never a claim that nothing moved).
            "role_solves": {
                role: solve.to_dict()
                for role, solve in (gain_plan.role_solves or {}).items()
            },
            "ambient_report": dict(ambient_report),
        })
        refs["check_artifact"] = artifact.fingerprint

    def publish_candidate(candidate: Any) -> None:
        _, artifact = _bank(records, run_async, candidate.to_dict())
        refs["candidate_artifact"] = artifact.fingerprint
        log_event(
            logger,
            "correction.crossover_v2_candidate_published",
            capture_session_id=capture_session_id,
            candidate_fingerprint=candidate.fingerprint,
            artifact_fingerprint=artifact.fingerprint,
        )

    return publish_check, publish_candidate, refs


def bind_round_receipt(
    store: Any, capture_session_id: str, refs: dict[str, Any], run_async: Any
) -> Callable[[Mapping[str, Any]], str]:
    """The conductor's ``publish_round_receipt`` seam (#2291).

    Banks ONE immutable receipt per round, which puts it beside ``check.json``,
    ``candidate.json`` and the retained positions — the artifacts its own
    ``evidence_identities`` name, which is what makes them resolvable at all.
    The store runs the R21 accept-receipt reopen-and-compare at the write
    (``record_store._verify_receipt``).

    Raises rather than swallowing: the fail-soft boundary is the round
    coordinator's own receipt writer
    (:func:`jasper.active_speaker.crossover_v2.coordinator.run_round` catches
    it), the same shape :func:`bind_cloud_publisher` takes.
    """
    records = _record_store(store, capture_session_id)

    def publish_round_receipt(receipt: Mapping[str, Any]) -> str:
        _, artifact = _bank(records, run_async, dict(receipt))
        refs["round_receipt_artifact"] = artifact.fingerprint
        return str(artifact.fingerprint)

    return publish_round_receipt


def bind_position_retention(
    store: Any,
    capture_session_id: str,
    refs: dict[str, Any],
    run_async: Any,
    *,
    provenance: CaptureProvenanceRecorder | None = None,
    evidence: CaptureEvidenceCarry | None = None,
) -> Callable[[Any, Mapping[str, Any]], str]:
    """The real ``bank_take`` seam — one WAV + one banked record per take.

    The forensic record the position-group choreography owes: the S0 work that
    produced this program's central finding (source-fixed vs room-fixed comb
    attribution) was only possible because every position's RAW capture
    survived, so a household cloud keeps the same thing rather than a derived
    summary that cannot answer a question nobody has asked yet.

    Placement follows the shipped bundle scheme —
    ``bundles.capture_artifact_relpath("summed", group, role)`` with the TAKE
    id as ``group``, so a cloud WAV lands beside the flow's other summed
    captures rather than in a private layout — and the banked record carries
    the prompt the operator was given, which is the only durable record of
    WHERE a curve was measured. ``metadata["position_id"]`` is what feeds
    ``spatial_combine.PositionCapture.position_id``, so a flagged or outlying
    position can be named back to the household in PR-4's report.

    The JSON half goes through the record store (ADR-0227 §12), which names the
    artifact from ``record["take_id"]`` rather than re-minting one from the
    position id: that record's ``position_id`` IS its take id, so a second mint
    appended a second ``_aNN``.

    **Fail-soft through :func:`_fail_soft`**, at the binding rather than in the
    conductor: a WAV write raises ``OSError``, and a full disk here must not
    turn an acoustically-good capture into a retake.
    ``bundles.register_capture`` is the one write this does not have to guard:
    every public write in that module already fail-softs.

    ``provenance`` (optional, keyword-only) is the recorder the ANALYZE seam
    hands this one forward through — not the play seam's own. It is drained
    once per banked take: what the play seam observed while this capture's
    stimulus was emitting, carried rather than re-read, because by now the
    routing graph is restored and the fader may have moved. Unbound, a take
    simply names no provenance.

    ``evidence`` (optional, keyword-only) is the analyze seam's own carry, on
    the same terms: the ``diagnostic``/``capture_integrity``/``frame_ledger``
    blocks, drained once and written onto the record UNCONDITIONALLY. There is
    no marker and no opt-in — the store is the only retention path there is,
    so a block computed at analyze and not banked here is a number that lands
    in no file at all. Unbound, a take simply carries no blocks, and every
    reader tolerates their absence: records banked before this change stay
    exactly as readable as they were.

    Returns the store id that finds the record again, or ``""`` when nothing
    was banked — which is the whole vocabulary
    :meth:`~jasper.active_speaker.crossover_v2_flow.CrossoverV2Session._retain_entry_baseline`
    reads.
    """
    from jasper.active_speaker.bundles import (
        CAPTURE_KIND_SEQUENTIAL,
        capture_artifact_relpath,
        register_capture,
    )
    from jasper.active_speaker.crossover_v2.journey import PHASE_CHECK, PHASE_LATERAL

    records = _record_store(store, capture_session_id)

    def bank_take(result: Any, metadata: Mapping[str, Any]) -> str:
        return _fail_soft(
            lambda: _bank_one_take(result, metadata),
            event="correction.crossover_v2_position_retain_failed",
            session_id=str(metadata.get("session_id") or ""),
            phase=str(metadata.get("phase") or ""),
            position_id=str(metadata.get("take_id") or ""),
        ) or ""

    def _bank_one_take(result: Any, metadata: Mapping[str, Any]) -> str:
        wav = getattr(result, "wav", None)
        record = dict(metadata)
        # What the play seam saw while THIS capture's stimulus was emitting —
        # the graph it went through and the fader it went out at. Observed at
        # play time and carried here, never re-read: by now the routing graph
        # is restored and the fader may have moved, so a reading taken at this
        # point would describe a speaker the capture never used. Drained, so a
        # take banked with no analyze behind it names no provenance rather than
        # the previous capture's.
        #
        # An ordinary household session fills this: the play seam observes
        # whenever it holds a recorder (``observing = provenance is not None``),
        # with no marker and nothing else to switch on. "none" now means the
        # take really was banked with no analyze behind it.
        carried = provenance.take() if provenance is not None else None
        if carried is not None:
            record["provenance"] = carried.to_dict()
            # The PLAYED program's digest, beside the record's ``wav_sha256``
            # and never merged into it: that key is the CAPTURED audio's
            # (``spatial._take_identity``), and the two answer different
            # questions — what was emitted versus what came back.
            record["stimulus_wav_sha256"] = carried.stimulus_wav_sha256
        # A geometry retake re-uses its position id — same prompted spot,
        # measured again from further out — so the id alone does NOT identify a
        # take. The evidence store is write-once (a repeated path is a
        # PATH_CONFLICT refusal), which would have dropped the retake's record
        # and left the REPLACED take as the only account of a curve that is not
        # in the cloud. The builders qualify by attempt: every take gets its own
        # file, the superseded one stays on disk as the honest walk record, and
        # the conductor's `group_position_takes` names which attempt survived.
        #
        # READ off the record, never re-minted here. The record's own take id is
        # what the store names the artifact by, so a second mint at this seam
        # could only ever disagree with it — which is exactly what it did for
        # the entry baseline, whose ``position_id`` IS a take id already.
        attempt = int(record.get("attempt") or 0)
        take_id = str(record.get("take_id") or "")
        # A cloud position and an entry baseline call it ``position_id``, a
        # lateral pose calls it ``pose_id`` — the two vocabularies
        # ``spatial._take_identity`` deliberately keeps apart, joined here
        # because ``refs`` has one column for the prompted spot.
        position_id = str(record.get("position_id") or record.get("pose_id") or "")
        wav_rel = ""
        if isinstance(wav, (bytes, bytearray)):
            bundle_dir = Path(store.bundle_dir)
            wav_rel = capture_artifact_relpath("summed", take_id, None)
            wav_path = bundle_dir / wav_rel
            wav_path.parent.mkdir(parents=True, exist_ok=True)
            wav_path.write_bytes(bytes(wav))
            record["wav_path"] = wav_rel
            record["wav_bytes"] = len(wav)
            # Placing the bytes is not recording them: without this the WAV is
            # in the bundle but in neither `info.json`'s `summed_captures` nor
            # `artifact_manifest.json`, so the bundle does not describe the
            # audio it carries. The take id is the group — it is already what
            # `capture_artifact_relpath` above named the file by.
            # CHECK, MEASURE and LATERAL play one recording that steps through
            # every driver in turn, so none of the three is a summed capture.
            # CHECK and MEASURE are outside ``SUMMED_SWEEP_PHASES`` for that
            # same reason; a lateral pose replays MEASURE's program OBJECT
            # verbatim (``programs.program_for_phase``), so it is the same
            # stimulus under a third name. The rest — VERIFY, the two cloud
            # position groups, the entry baseline — really is one summed sweep.
            # The WAV placement above stays on the summed scheme either way;
            # only the recorded kind splits.
            register_capture(
                bundle_dir,
                kind=(
                    CAPTURE_KIND_SEQUENTIAL
                    if record.get("phase")
                    in (PHASE_CHECK, PHASE_MEASURE, PHASE_LATERAL)
                    else "summed"
                ),
                relative_path=wav_rel,
                payload={**record, "speaker_group_id": take_id},
            )
        # AFTER ``register_capture``, deliberately: these blocks belong to the
        # write-once take record, not to the bundle's own capture manifest,
        # which is re-read on a 1 GB Pi and describes where the audio IS rather
        # than what an analysis made of it.
        blocks = evidence.take() if evidence is not None else None
        if blocks:
            record.update(blocks)
        # Site B holds the conductor's ``_close_lock`` across this call exactly
        # as it held the direct write this replaces.
        record_id, artifact = _bank(records, run_async, record)
        positions = refs.setdefault("position_artifacts", [])
        # WO-1 (attribution plan §6): the per-position WAV path AND its
        # SHA-256 ride the durable state, not only the bundle sidecar, "so
        # the state alone is replayable". ``take_id`` rides with them because
        # a geometry retake reuses the position id — without it the
        # accepted-attempt <-> position mapping is recoverable only from
        # skipped attempt indices in filenames, which is exactly the gap WO-0
        # hit. The digest is the VERIFIER for those bytes; the session
        # identity plus the take id is the index.
        positions.append({
            "position_id": position_id,
            "attempt": attempt,
            "take_id": take_id,
            # Still the artifact FINGERPRINT and not ``bank``'s store id. F9
            # (the join, #3193) left the two un-bridged because nothing reads
            # this column yet; W1-d's index is what will want the path, and it
            # can have it then.
            "artifact": artifact.fingerprint,
            "wav_path": wav_rel,
            "wav_sha256": str(record.get("wav_sha256") or ""),
        })
        return record_id

    return bank_take


def v2_session_identity(store: Any, capture_session_id: str) -> Any:
    """This v2 session's cross-store identity (attribution plan §6).

    The **bundle** session id is canonical, because Q-C's bundle-lifetime
    ruling makes the bundle the retention unit: identity and lifetime then
    name the same thing, which is what keeps a finding from outliving its
    evidence. The capture-session id is real and is minted *after* the
    bundle — it is not derivable from it — so it rides as an alias rather
    than as a second identity. Before this, the only join between the two
    namespaces was one key in the durable state file, and the capture ring
    carried neither.
    """

    from jasper.attribution.session_identity import (
        ALIAS_CAPTURE_SESSION_ID,
        SessionIdentity,
    )

    return SessionIdentity(
        session_id=str(store.session_id),
        aliases={ALIAS_CAPTURE_SESSION_ID: str(capture_session_id)},
    )


def _publish_findings(
    records: Any,
    run_async: Any,
    phase: str,
    result: Mapping[str, Any],
    cloud_artifact: Any,
    refs: dict[str, Any],
) -> None:
    """Promote this group's excluded-band records to findings and persist them.

    WO-1's write half. The findings cite the cloud artifact **that was just
    banked** — the exact bytes the carve-out records were read from — so
    the citation is verifiable and, being a bundle artifact, is bound to the
    same lifetime the finding is (Q-C).

    **Fail-soft, like ``bank_take`` and unlike ``publish_cloud``**, which
    deliberately lets the strict store's refusals surface so the conductor's
    own boundary handles them. Findings are different:
    plan §3.4 makes them *optional evidence artifacts* — "a session with no
    findings behaves exactly as it does today" — so a findings failure must
    not turn a successfully-banked cloud group into a logged failure. The
    cloud artifact above is already durable by the time this runs.
    """

    from jasper.attribution.findings import FindingSet
    from jasper.attribution.promotion import PRODUCED_BY, promote_carve_outs
    from jasper.attribution.storage import bundle_evidence_ref

    capture_session_id = records.capture_session_id

    def _publish() -> tuple[Any, int]:
        identity = v2_session_identity(records.evidence, capture_session_id)
        findings = promote_carve_outs(
            result.get("carve_outs"),
            session=identity,
            cites=(bundle_evidence_ref(cloud_artifact, identity),),
        )
        return _bank_findings(
            records, run_async, phase=phase,
            finding_set=FindingSet(
                session=identity,
                produced_by=PRODUCED_BY,
                findings=findings,
            ),
        ), len(findings)

    published = _fail_soft(
        _publish,
        event="correction.crossover_v2_findings_publish_failed",
        capture_session_id=capture_session_id,
        phase=phase,
    )
    if published is None:
        return
    artifact, findings_banked = published
    refs.setdefault("finding_artifacts", {})[phase] = artifact.fingerprint
    # No household projection here, deliberately — see
    # :func:`_bank_household_findings`. A carve-out finding's ``household_copy``
    # is COPIED from the carve-out record (``promote_carve_outs`` rule 3) rather
    # than minted, so the copy has an owner already: ``carve_outs_by_band``,
    # whose ``disclosure`` register is the chart callout's plain-language
    # headline (``cloud.js``'s ``buildCallout``) and whose ``expert`` register is
    # the τ/r line ``_carve_out_expert_lines`` folds into ``expert_details``.
    # Both render on both screens this would reach. The store keeps the full
    # record either way.
    log_event(
        logger,
        "correction.crossover_v2_findings_published",
        capture_session_id=capture_session_id,
        phase=phase,
        findings=findings_banked,
    )


def _bank_household_findings(
    store: Any, *, capture_session_id: str, phase: str, refs: dict[str, Any],
) -> None:
    """Reopen the finding set just published and project what a household reads.

    WO-1's **read** half (first-principles panel lens C, CC1): the flow banks a
    finding with validated household copy and, until this, nothing ever read one
    back — ``read_finding_set`` had zero non-test callers, so #1949's "bank a
    finding and proceed" was, in the household's experience, "proceed".

    **The read happens HERE, at publish, not at render, and that is a
    saturation decision.** The screens that show a finding are polled every
    1.5 s (``crossover/main.js``'s ``POLL_MS``), and a render-time read would
    re-open and re-hash the finding artifact AND its cited ``candidate.json``
    on every one of those polls, forever, on a Pi. It would also fail to reach
    the DONE screen at all: stage 2 opens a **new** bundle under a **new**
    capture session id (a verify-only prepare → ``open_v2_evidence_store``), so
    by the time the household sees the result screen, "this session's bundle"
    no longer holds the set the measuring session banked. Reading once and
    projecting the compact result into the durable state is the same shape
    ``compact_cloud_status`` already uses for the cloud's numbers: the bundle
    artifact stays the record; the state carries what a screen renders.

    **The read-back is itself the honesty check.** Going out through
    the record store and straight back in through ``read_finding_set``
    means only a set that survives the strict reopen — schema, session binding,
    and (``verify_evidence`` defaults True) a re-hash of every bundle citation
    — reaches a household. A finding whose support could not be confirmed
    raises ``FindingEvidenceMissing`` and is logged rather than rendered.

    **Order is the producer's, and nothing is de-duplicated.** The set's own
    order is preserved as persisted (``promote_carve_outs`` sorts by band;
    the level-frame path yields one), because re-ordering here would make this
    a second owner of a decision the producer already made. Two findings whose
    copy happens to read identically both render: dropping one would be this
    function silently deciding a banked finding does not exist, and "must not
    drop a finding" outranks a repeated sentence — a producer emitting the same
    sentence twice is a bug to fix at the producer.

    **Called from the level-frame path only.** ``_publish_findings``' carve-out
    sets are not projected: their ``household_copy`` is the carve-out record's
    own ``reason`` (``promote_carve_outs`` rule 3 copies it rather than minting
    it), so ``carve_outs_by_band`` is already that copy's owner and already
    renders the fact on both these screens — its ``disclosure`` register as the
    chart callout's plain-language headline (``cloud.js``'s ``buildCallout``),
    its ``expert`` register as the τ/r line ``_carve_out_expert_lines`` folds
    into ``expert_details``. Projecting it again would put one fact on one
    screen twice from two owners. When a producer mints copy that no other
    surface owns — as the level-frame path does, and as WO-4's detectors will —
    it calls this.

    Fail-soft, like every other findings path (plan §3.4: "a session with no
    findings behaves exactly as it does today"). A lost projection is a lost
    disclosure, never a lost tune.
    """

    from jasper.attribution.storage import read_finding_set

    try:
        finding_set = read_finding_set(
            store, capture_session_id=capture_session_id, phase=phase,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        log_event(
            logger,
            "correction.crossover_v2_findings_readback_failed",
            level=logging.WARNING,
            capture_session_id=capture_session_id,
            phase=phase,
            exc_info=True,
        )
        return
    if finding_set is None:
        return
    # ONE stamp for the whole set: every finding in it was banked by the same
    # publish, and the household-facing rendering of it is a date (see
    # ``crossover_envelope_v2._record_when_phrase``), so a per-finding clock
    # would be a precision the copy never spends and a second number to keep
    # honest. The store carries no timestamp of its own — this is the
    # finding's own clock, on the same epoch-float footing as
    # ``failure["at"]`` one level up.
    banked_at = time.time()
    projected = refs.setdefault(FINDING_HOUSEHOLD_REFS_KEY, [])
    for finding in finding_set.findings:
        projected.append({
            # ``household_copy`` and nothing else. The mechanism id, the
            # evidence scalars, the confidence tier, and the probe lists are
            # INTERNAL taxonomy by ``findings.py``'s own two-vocabularies rule;
            # they stay in the bundle artifact and the journal, where an
            # operator reads them, and never cross onto a household wire.
            "household_copy": finding.household_copy,
            "at": banked_at,
        })
    log_event(
        logger,
        "correction.crossover_v2_findings_readback",
        capture_session_id=capture_session_id,
        phase=phase,
        findings=len(finding_set.findings),
    )


def bind_findings_publisher(
    store: Any, capture_session_id: str, refs: dict[str, Any], run_async: Any
) -> Callable[[Mapping[str, Any]], None]:
    """The real ``publish_findings`` seam — the #1866 frame-gate finding.

    The owner's 2026-07-30 ruling: a level-frame disagreement is BANKED as an
    M7 finding, and the session proceeds, when the realized-level check passes
    on the pair about to ship — a closed-loop read of the OUTCOME, not a
    referee between the two frames (see the flow's gate comment for why the
    distinction matters). The conductor decides and hands over an evidence
    record; this binder is the only thing that knows there is a store.

    **Its own phase, and that is forced rather than chosen.** The finding set
    lands at ``findings_measure.json``, beside the cloud groups'
    ``findings_cloud_measure.json`` / ``findings_cloud_verify.json``. The store
    is write-once and the cloud group's set is published at group CLOSE —
    several seconds and one household tap before the fit this finding comes out
    of even runs — so reusing that phase would be a PATH_CONFLICT, not a merge.
    The per-phase path already exists for exactly this reason (see
    :func:`~jasper.attribution.storage.findings_relative_path`), and the phase
    it takes is the flow phase the finding belongs to.

    **It cites ``candidate.json``**, the artifact
    :func:`bind_evidence_publishers`' ``publish_candidate`` wrote moments
    earlier, for three reasons: it is the thing the finding is ABOUT (the
    trims committed under a frame whose estimators disagreed), it carries the
    candidate's own per-role ``correction_giveback_db`` inside its ``linearization``
    block, and it is guaranteed to exist and to be durable at this point in the
    session — which a citation must be, since
    :func:`~jasper.attribution.storage.read_finding_set` re-hashes it on every
    read and raises when it cannot be confirmed.

    Fail-soft, like :func:`_publish_findings` and for the same §3.4 reason: the
    candidate is already published and the gate has already ruled that this
    session may proceed. A findings failure is a lost diagnosis, never a lost
    tune.
    """

    records = _record_store(store, capture_session_id)

    def publish_findings(record: Mapping[str, Any]) -> None:
        from jasper.active_speaker.commissioning_evidence_store import (
            EVIDENCE_ROOT,
        )
        from jasper.attribution.findings import FindingSet
        from jasper.attribution.promotion import (
            PRODUCED_BY_LEVEL_FRAME,
            promote_level_frame_disagreement,
        )
        from jasper.attribution.storage import bundle_evidence_ref

        def _publish() -> Any:
            identity = v2_session_identity(store, capture_session_id)
            finding = promote_level_frame_disagreement(
                record,
                session=identity,
                cites=(
                    bundle_evidence_ref(
                        store.identify_artifact(
                            f"{EVIDENCE_ROOT}/artifacts/crossover_v2/"
                            f"{capture_session_id}/candidate.json"
                        ),
                        identity,
                    ),
                ),
            )
            # A record the promoter refused is already logged by it, with the
            # reason. Banking an EMPTY set here would be a lie of a different
            # shape — "attribution ran and found nothing" — about a session
            # whose gate found something and said so in the journal.
            if finding is None:
                return None
            return _bank_findings(
                records, run_async, phase=PHASE_MEASURE,
                finding_set=FindingSet(
                    session=identity,
                    produced_by=PRODUCED_BY_LEVEL_FRAME,
                    findings=(finding,),
                ),
            )

        artifact = _fail_soft(
            _publish,
            event="correction.crossover_v2_findings_publish_failed",
            capture_session_id=capture_session_id,
            phase=PHASE_MEASURE,
        )
        if artifact is None:
            return
        refs.setdefault("finding_artifacts", {})[PHASE_MEASURE] = (
            artifact.fingerprint
        )
        log_event(
            logger,
            "correction.crossover_v2_findings_published",
            capture_session_id=capture_session_id,
            phase=PHASE_MEASURE,
            findings=1,
        )
        # The read half (CC1). Deliberately AFTER the publish log and outside
        # the try above: the set is durable at this point, so a read-back
        # failure must be reported as its own event rather than making a
        # successful publish look like a failed one.
        _bank_household_findings(
            store,
            capture_session_id=capture_session_id,
            phase=PHASE_MEASURE,
            refs=refs,
        )

    return publish_findings


def bind_cloud_publisher(
    store: Any, capture_session_id: str, refs: dict[str, Any], run_async: Any
) -> Callable[[str, Mapping[str, Any]], None]:
    """The real ``publish_cloud`` seam (flat-linearization plan PR-4).

    One JSON artifact PER CLOSED GROUP — ``crossover_v2/<session>/<phase>.json``
    (``cloud_measure.json`` / ``cloud_verify.json``), never a single shared
    ``cloud.json`` across both groups: the store is write-once and the
    pre-apply and post-apply groups close at genuinely different times in the
    SAME session, so a shared path would collide on the second group's write.
    This is a mechanism deviation from the work order's literal
    ``crossover_v2/<session>/cloud.json`` path, recorded here rather than
    silently matched — the per-group content (mask/registry/spec/geometry) is
    exactly what was asked for either way.

    Fail-soft at the CALLER (``CrossoverV2Session._run_cloud_pipeline``): a
    full disk or a write-once conflict must surface as an exception here so the
    conductor's own boundary can log and continue.
    :func:`bind_position_retention` is the opposite, deliberately: its boundary
    is at the binding, so it catches where this one raises.
    """
    from jasper.active_speaker.crossover_v2.record_store import CLOUD_EVIDENCE_KIND

    records = _record_store(store, capture_session_id)

    def publish_cloud(phase: str, result: Mapping[str, Any]) -> None:
        _, artifact = _bank(records, run_async, {
            "kind": CLOUD_EVIDENCE_KIND, "phase": phase, **dict(result),
        })
        cloud_artifacts = refs.setdefault("cloud_artifacts", {})
        cloud_artifacts[phase] = artifact.fingerprint
        _publish_findings(records, run_async, phase, result, artifact, refs)

    return publish_cloud


@dataclass(frozen=True)
class _HeldSession:
    """What one prepared capture hosting holds between ``open`` and the run.

    A named pair rather than the untyped ``holder`` dict this replaces: the
    engine's session and the source walk are two different lifetimes that
    happen to be handed across the same closure boundary, and ``holder["run"]``
    could not say which of them a reader was looking at.
    """

    tuning: Any
    run: Any


@dataclass(frozen=True)
class ProductionPlay:
    """The play seam, and the measurement graph it was bound around.

    The graph is RETURNED, not stashed. It is one session's graph and its
    lifetime is that session's, so the session holds it — as
    ``EngineSeams.graph`` — and it is reachable nowhere else.
    """

    play: Callable[[str, Any], None]
    graph: Any
    #: The engine's ``compose`` seam, built here because every binding it needs
    #: — the bundle, the artifact minting, the topology, the safety profile,
    #: the role targets and the declared level — is already resolved in this
    #: function. Threading ten of them back out to build it elsewhere would
    #: make a second site free to disagree with this one about any of them.
    compose: Any = None

    def __call__(self, phase: str, program: Any) -> None:
        """Still callable as the play seam, for the callers that treat it so.

        Production unwraps it — the preparer binds ``.play`` and reads
        ``.graph`` — so this is not what the flow relies on. It is here because
        ``bind_production_play``'s shipped contract was *"returns the thing you
        call with (phase, program)"*, and the suites that call the binder
        directly still hold that contract. Delete it when they stop.
        """
        return self.play(phase, program)


def bind_production_play(
    *,
    run_async: Any,
    camilla_factory: Any,
    evidence_store: Any,
    capture_session_id: str,
    topology: Any,
    preset: Any,
    role_channels: Mapping[str, int],
    playback_device: str,
    safety_profile: Mapping[str, Any],
    role_targets: Mapping[str, str],
    session_volume_db: float,
    protection_sections_by_role: Mapping[str, Sequence[Any]] | None = None,
    declared_sensitivities: Mapping[str, float] | None = None,
    config_dir: str | None = None,
    provenance: CaptureProvenanceRecorder | None = None,
) -> "ProductionPlay":
    """The real ``play`` seam: program WAV → admitted playback through the DSP.

    CHECK/MEASURE render + publish the program WAV into the session's evidence
    bundle, emit the channel-routed program graph
    (``emit_active_speaker_program_config``), and ride
    :func:`jasper.active_speaker.program_playback.play_program` with the
    CamillaController seams from ``bind_program_playback_seams``; VERIFY plays
    its mono WAV through the APPLIED production graph (verified-aplay only —
    no graph load). Both run inside the mux measurement window so the
    correction lane actually reaches the speaker.

    ``config_dir`` defaults to the SAME
    :data:`jasper.active_speaker.staging.DEFAULT_CAMILLA_CONFIG_DIR` every
    sibling DSP writer (commissioning apply/verify, ``web_commissioning``,
    ``correction_setup``) locks against — W6 hardware run 3 finding F caught
    this binding still defaulting to the stale literal ``"/etc/camilladsp"``:
    two defects at once. (a) ``jasper-correction-web`` runs
    ``ProtectSystem=full`` with ``ReadWritePaths=/var/lib/jasper
    /var/lib/camilladsp`` only (see ``deploy/jasper-correction-web.service``),
    so opening ``/etc/camilladsp/.dsp_apply.lock`` raised EROFS 70 ms into the
    first play. (b) even under a writable ``/etc``, it would have been the
    WRONG lock identity — every other writer locks
    ``/var/lib/camilladsp/configs/.dsp_apply.lock``, so a real ``/etc``
    lock would not have serialized against them at all.

    ``provenance`` (optional) is the session's
    :class:`~jasper.active_speaker.capture_provenance.CaptureProvenanceRecorder`.
    **This function is the one owner of "which graph did the capture go
    through"**: the branch below either loads the transient routing graph or
    deliberately does not, and no downstream reader can recover that from a
    file path — see that module for why. So the branch states it. The
    observation is taken as late as the seam allows (for CHECK/MEASURE, inside
    the writer lock, after the load, immediately before the WAV handoff), and
    on every capture this binding holds a recorder for.

    **It is also the one owner of "at what level"** (#2925). Both branches call
    ``_hold_fader`` at that same instant — one seam, because the defect it
    closes lived in the gap between the two branches and a per-branch copy
    would rebuild it. Unlike the provenance observation it is UNCONDITIONAL,
    and a drift it cannot repair refuses the capture before any audio rather
    than banking a measurement taken at an unknown level.

    ON-DEVICE: not exercised hardware-free; W6 validates acoustically.
    """
    from jasper.active_speaker.crossover_v2.programs import SUMMED_SWEEP_PHASES
    from jasper.active_speaker.crossover_v2.session_graph import (
        MeasurementSessionGraph,
    )
    from jasper.active_speaker.crossover_v2.composition import (
        bind_program_playback_seams,
        confirm_graph_is_live,
    )
    from jasper.active_speaker.measurement_emit import (
        MeasurementGraphProfile,
        emit_measurement_graph,
    )
    from jasper.active_speaker.web_commissioning import DEFAULT_CAMILLA_CONFIG_DIR
    from jasper.audio_measurement.program import write_program_wav
    from jasper.dsp_apply import dsp_writer_lock

    resolved_config_dir = (
        config_dir if config_dir is not None else str(DEFAULT_CAMILLA_CONFIG_DIR)
    )

    # The emit is NOT web vocabulary — a preset, a topology, a role→channel map,
    # a sink and the confirmed protection — so it lives in
    # ``active_speaker.measurement_emit``, where an operator door reaches the
    # same one without importing this host. The five closure variables became
    # its snapshot; the three VARIANT axes stay the call's.
    session_graph = MeasurementSessionGraph(
        emit=functools.partial(
            emit_measurement_graph,
            MeasurementGraphProfile(
                preset=preset,
                topology=topology,
                role_channels=role_channels,
                playback_device=playback_device,
                protection_sections_by_role=protection_sections_by_role,
            ),
        ),
        cam_factory=camilla_factory,
        writer_lock=lambda: dsp_writer_lock(
            resolved_config_dir, source="crossover_v2_session_graph"
        ),
        confirm_live=confirm_graph_is_live,
    )

    def _observe_stimulus(
        open_cam: Callable[[], Any], graph_kind: str, program: Any,
        artifact: Any, phase: str,
    ) -> Any:
        """Awaitable: record what this stimulus plays THROUGH, fail-soft.

        Reads ``main_volume_db`` itself rather than reusing the fader hold's
        proven read — deliberately, a second physical RPC and not a redundancy
        to fold away: this read sits closest in time to the stimulus, which is
        what a forensic record must describe, and one extra best-effort
        round-trip per capture is the accepted price.

        One implementation for both playback legs — the flow's ``_play`` and
        the engine's compose — so the two cannot drift about what a
        provenance block records.
        """
        return record_capture_provenance(
            provenance, open_cam=open_cam, graph_kind=graph_kind,
            program=program, phase=phase, artifact=artifact,
            read_volume_plan=session_volume_plan,
        )

    async def _hold_fader_for(open_cam: Callable[[], Any], phase: str) -> None:
        """Re-prove the session's measurement volume for THIS stimulus.

        The ONE volume discipline every playback leg uses (#2925), owned by
        the plan (``SessionVolumePlan.hold_measurement_volume``) because only
        the plan can serialize it against its own drains. NOT gated on
        ``observing`` the way provenance is: that record is forensics, this is
        the safety ledger's own integrity — ``readmit_program_from_wav``
        admitted this program against the DECLARED volume, so a stimulus
        emitted at any other level was never the one that was admitted.

        ``None`` means the plan holds no volume to prove; that is its question
        to answer, so this discloses and plays on.
        """
        get_v = _session_volume_read(open_cam)
        held = await session_volume_plan().hold_measurement_volume(
            get_v, context=f"capture:{phase}",
        )
        if held is None:
            log_event(
                logger,
                "correction.crossover_v2_capture_volume_unheld",
                level=logging.WARNING,
                phase=phase,
            )

    def _play(phase: str, program: Any) -> None:
        bundle_dir = evidence_store.bundle_dir
        wav_rel = f"crossover_v2/{capture_session_id}/{phase}_program.wav"
        wav_path = Path(bundle_dir) / wav_rel
        wav_path.parent.mkdir(parents=True, exist_ok=True)
        write_program_wav(wav_path, program)
        artifact = evidence_store.identify_artifact(wav_rel)
        if phase in SUMMED_SWEEP_PHASES:
            # Issue #1976. Every SUMMED_SWEEP_PHASES phase plays the SAME
            # excitation object — ``program_for_phase`` in
            # crossover_v2_flow.py returns ``self._verify_program`` for
            # VERIFY, CLOUD_MEASURE, and CLOUD_VERIFY alike — so a
            # measure-stage session that walks a pre-apply cloud group
            # WITHOUT ever arming a literal VERIFY capture (the common
            # shape: 12 real bundles surveyed 2026-07-29, only 4 ever
            # reached VERIFY) left this reusable stimulus persisted only
            # under its cloud-phase name, discoverable by nobody who
            # didn't already know which phase happened to run. A
            # 2026-07-31 offline-replay attempt against a real bench
            # bundle confirmed the gap: ``cloud_measure_program.wav`` was
            # on disk, no summed-sweep stimulus was recoverable under a
            # predictable name.
            #
            # This does NOT reuse the ``verify_program.wav`` name: the
            # corpus-index tooling and its mapped research docs derive
            # "which phases this bundle reached" from which
            # ``{phase}_program.wav`` files exist on disk, so writing to
            # that path for a CLOUD_MEASURE-only session would make a
            # cloud-only bundle false-report having reached VERIFY —
            # corrupting the exact presence-heuristic this fix's own
            # replay use case depends on. ``summed_program.wav`` is a
            # NEW, dedicated name: present whenever this session played
            # ANY summed-sweep-shaped capture, additive to (never a
            # substitute for) the phase-named files above.
            #
            # Fill-if-absent: the first summed-sweep phase this session
            # plays wins, and an existing file is left alone. Same sweep at
            # the same clamp whichever phase wrote it; the one difference is
            # the courtesy prelude, which the compared pair carries and a
            # prompted position does not
            # (``crossover_v2.programs.courtesy_prelude_for_phase``) — and
            # which is analysis-invisible by construction, so a replay reads
            # the same either way. Best-effort: this is a diagnostic
            # convenience copy, not the measurement itself, so a full disk or
            # a permissions fault here must not abort an operator's capture.
            try:
                summed_wav_path = (
                    Path(bundle_dir)
                    / f"crossover_v2/{capture_session_id}/summed_program.wav"
                )
                if not summed_wav_path.exists():
                    write_program_wav(summed_wav_path, program)
            except (OSError, ValueError, TypeError, AttributeError):
                log_event(
                    logger, "correction.crossover_v2_summed_program_persist_failed",
                    level=logging.WARNING, phase=phase, exc_info=True,
                )

        def _observe(open_cam: Callable[[], Any], graph_kind: str) -> Any:
            return _observe_stimulus(
                open_cam, graph_kind, program, artifact, phase,
            )

        async def _hold_fader(open_cam: Callable[[], Any]) -> None:
            await _hold_fader_for(open_cam, phase)

        async def _play_body() -> None:
            from jasper.active_speaker.capture_provenance import (
                GRAPH_KIND_APPLIED,
                GRAPH_KIND_PROGRAM_ROUTING,
            )
            from jasper.active_speaker.program_playback import (
                verified_program_aplay,
            )

            # Observing costs CamillaDSP round-trips, and every capture now
            # buys them: the banking seam drains this observation into the
            # write-once record, so a session that skipped it would bank takes
            # that name no graph and no fader. Expressing this on the recorder
            # alone — rather than gating on a separate marker — is what keeps
            # the carry fed.
            # The FADER HOLD below is not covered by this gate and never was
            # meant to be (#2925): it answers for the safety ledger rather than
            # for the forensic record, so it runs even without a recorder.
            observing = provenance is not None

            if phase in SUMMED_SWEEP_PHASES:
                # The LIVE production graph IS the system under test — no graph
                # load, just the verified WAV into the lane. True for VERIFY
                # (the applied graph) and for both position groups: a spatial
                # cloud measures the summed system as it stands, which is the
                # pre-apply graph for CLOUD_MEASURE and the applied one for
                # CLOUD_VERIFY. Level safety for all three is the compose-time
                # min-cap clamp in ``crossover_v2.programs``'s
                # ``SessionExcitation.verify_program``.
                #
                # No load means the standing graph IS what this capture goes
                # through — stated by the branch that skipped the load.
                #
                # A summed sweep measures the STANDING graph, so the session's
                # measurement graph has to step aside rather than be shared.
                # Restoring here (not per stimulus) is what keeps an all-routed
                # walk at two swaps for the whole session and bounds a mixed one
                # by its routed/summed transitions.
                await session_graph.restore()
                # The hold runs FIRST and unconditionally, so the provenance
                # record below observes a fader that was just proven.
                await _hold_fader(camilla_factory)
                if observing:
                    await _observe(camilla_factory, GRAPH_KIND_APPLIED)
                await verified_program_aplay(
                    bundle_dir, artifact, timeout_s=60.0
                )
                return
            from jasper.active_speaker.program_playback import play_program

            # Install-or-prove. One call covers the first routed stimulus and a
            # graph some other DSP writer replaced underneath us: both mean "the
            # running graph is not the one this session measures through", and
            # the answer to both is to put it back (ruling S6's pipeline-health
            # check, ruling S10's shape — repair and disclose, never refuse to
            # play). A summed sweep between two routed stimuli lands here too,
            # because it released the graph on its way past.
            await session_graph.install()
            # Hoisted so the observation below rides the SAME controller, not a
            # second one asking separately.
            cam = camilla_factory()
            seams = bind_program_playback_seams(
                cam,
                bundle_dir=str(bundle_dir),
                artifact=artifact,
                config_dir=resolved_config_dir,
                program=program,
                wav_path=str(wav_path),
                topology=topology,
                safety_profile=safety_profile,
                role_targets=role_targets,
                session_volume_db=session_volume_db,
                declared_sensitivities=declared_sensitivities,
            )
            if observing:
                # INSIDE ``play_program`` even though the graph is installed
                # earlier now: what this records is what the capture PLAYED
                # through, so it belongs next to the WAV handoff rather than
                # next to the install — and ``get_active_config_raw`` answers
                # the measurement graph at both points.
                pre_provenance_play_wav = seams["play_wav"]

                async def _play_wav_observed() -> Any:
                    await _observe(lambda: cam, GRAPH_KIND_PROGRAM_ROUTING)
                    return await pre_provenance_play_wav()

                seams["play_wav"] = _play_wav_observed
            # OUTERMOST, and unconditional (#2925 — ``hold_measurement_volume``
            # states the mechanism). Wrapping ``play_wav`` is what puts the hold
            # where the fix has to be: INSIDE the writer lock, AFTER the graph
            # is proven installed, BEFORE any audio. Outside the provenance
            # wrapper so the recorded fader is one that was proven.
            pre_hold_play_wav = seams["play_wav"]

            async def _play_wav_volume_held() -> Any:
                await _hold_fader(lambda: cam)
                return await pre_hold_play_wav()

            seams["play_wav"] = _play_wav_volume_held
            await play_program(
                program,
                session_volume_plan=session_volume_plan(),
                **seams,
            )

        # The session holds ONE measurement window for its whole life (W6.1 —
        # see acquire_session_measurement_pause). ``measurement_window`` is
        # exclusive/non-nestable, so the selector nest-SKIPs it when the
        # session already holds it — registering this play task as the
        # window's abort target so an isolation-loss abort still stops the
        # sweep — and takes a per-play window only if the session pause is
        # somehow not held.
        run_async(_under_measurement_isolation(_play_body))

    async def _compose_stimulus(
        *,
        spec: Any,
        position_deg: Any = None,
        prompt: str = "",
        level_db: float = 0.0,
        stimulus_dbfs: Any = None,
        program_for_phase: Any = None,
    ) -> Any:
        """One stimulus, as the program plus the seams ``play_program`` takes.

        The engine's five facts in, a ``ProgramForStimulus`` out. Only two of
        them reach the flow: ``spec.kind`` picks the phase, and the phase picks
        the program BY IDENTITY through the session's own
        ``program_for_phase``. The other three describe the pose and the rung,
        which the transaction reports and the record carries — they do not
        choose a stimulus, and a compose that consulted them would be inventing
        a second program vocabulary beside the flow's.

        **ASYNC, and the render goes to a thread.** ``measure`` awaits this on
        the correction loop — the one background loop every short endpoint
        bridges into — so rendering a sweep inline would freeze status, apply,
        restore and every other handler for the length of the write. That is
        the blocking-the-one-loop shape ADR-0179's ``to_thread`` guidance
        names, and it is why the seam's ``Compose`` contract allows either
        colour rather than requiring sync.

        **NOT memoised per phase, deliberately.** The program IS
        identity-constant per phase, so a walk re-renders identical bytes — but
        the shipped ``_play`` re-renders the same path the same way, and a memo
        HERE would make two writers of one artifact disagree about when it is
        rewritten. Worth doing once, for both, when something drives enough
        stimuli to measure it; not worth a cache-invalidation question on a
        path nothing drives yet.

        **The two ``_play_body`` wrappers ride here too, same order, same
        gates.** The #2925 fader hold (outermost, unconditional, inside the
        writer lock and before any audio) and the provenance observation
        (``observing = provenance is not None`` — the same gate ``_play_body``
        uses) wrap ``play_wav`` exactly as the flow leg wraps it, so a MEASURE
        capture banked off the engine leg carries the same provenance block
        and the same mid-lock fader proof a flow-leg capture does. No
        phase-ladder signal: the wired walk has no phone to pace.
        """
        from jasper.active_speaker.capture_provenance import (
            GRAPH_KIND_PROGRAM_ROUTING,
        )
        from jasper.active_speaker.crossover_v2.measurement_phase import (
            phase_for_measurement,
        )
        from jasper.active_speaker.crossover_v2.program_transaction import (
            ProgramForStimulus,
        )

        phase = phase_for_measurement(getattr(spec, "kind", ""))
        program = program_for_phase(phase)
        bundle_dir = evidence_store.bundle_dir
        wav_rel = f"crossover_v2/{capture_session_id}/{phase}_program.wav"
        wav_path = Path(bundle_dir) / wav_rel

        def _render() -> Any:
            """The blocking span: one mkdir, one WAV write, one fingerprint."""
            wav_path.parent.mkdir(parents=True, exist_ok=True)
            write_program_wav(wav_path, program)
            return evidence_store.identify_artifact(wav_rel)

        artifact = await asyncio.to_thread(_render)
        cam = camilla_factory()
        seams = bind_program_playback_seams(
            cam,
            bundle_dir=str(bundle_dir),
            artifact=artifact,
            config_dir=resolved_config_dir,
            program=program,
            wav_path=str(wav_path),
            topology=topology,
            safety_profile=safety_profile,
            role_targets=role_targets,
            session_volume_db=session_volume_db,
            declared_sensitivities=declared_sensitivities,
        )
        if provenance is not None:
            pre_provenance_play_wav = seams["play_wav"]

            async def _play_wav_observed() -> Any:
                await _observe_stimulus(
                    lambda: cam, GRAPH_KIND_PROGRAM_ROUTING, program, artifact,
                    phase,
                )
                return await pre_provenance_play_wav()

            seams["play_wav"] = _play_wav_observed
        pre_hold_play_wav = seams["play_wav"]

        async def _play_wav_volume_held() -> Any:
            await _hold_fader_for(lambda: cam, phase)
            return await pre_hold_play_wav()

        seams["play_wav"] = _play_wav_volume_held
        return ProgramForStimulus(program=program, seams=seams)

    return ProductionPlay(
        play=_play, graph=session_graph, compose=_compose_stimulus,
    )


# --------------------------------------------------------------------------- #
# what the host hands the capture provider (S1a/S1c)
# --------------------------------------------------------------------------- #
#
# The plan runner itself is jasper.web.correction_crossover_v2_wired's own
# (reached where it lives). These stay HERE because they are host policy the
# provider merely drives: the volume lifecycle it is handed, the group-close
# seam, and the eager-fit starter it calls back into.


@dataclass(frozen=True)
class V2VolumeHooks:
    """The session-volume lifecycle the runner drives (§5.5)."""

    open: Callable[[], Any]      # async
    close: Callable[[], Any]     # async
    abandon: Callable[[], Any]   # async


def drive_group_close(conductor: Any, *, evidence: Mapping[str, Any] | None) -> None:
    """The household's "all spots measured — Continue" group close (D1).

    **The group-close seam at the host boundary, one owner for every
    provider** (#2662 W2b): the wired runner drives it on the local
    authenticated completion event, the wired runner on its local completion
    signal, and both must run the identical sequence — persist FIRST (the
    combine + fit are the slowest thing in the session and the wizard renders
    from durable state, so a confirmation whose fit never reached disk would
    leave the review screen with nothing to review), then the conductor's
    confirm (the only thing that fits a correction in stage 1), then persist
    the fitted result. A ``CaptureBeginRefused`` raised under the close
    propagates to the calling runner, which ends the session as an admission
    refusal does; PR-L4's seam has refused nothing since deviations (c)/(i).

    What this deliberately does NOT do is apply — the review interlude is the
    apply decision point (2026-07-28 ruling); the full history lives on the
    provider's ``complete_capture_set`` docstring, whose body now
    delegates here.
    """
    conductor.note_group_close_started()
    persist_conductor_state(conductor, failure_code=None, evidence=evidence)
    confirmed = conductor.confirm_cloud_measure_group()
    if confirmed is not None:
        persist_conductor_state(conductor, failure_code=None, evidence=evidence)


def _start_speculative_group_close(conductor: Any) -> threading.Thread | None:
    """Start the eager fit for a just-walked pre-apply cloud (owner, 2026-07-30).

    Called from ``consume`` the moment the final stage-1 position is ACCEPTED,
    which is the moment the phone starts showing the group-close confirm. The
    household is now walking back to a browser; the combine + fit are the
    slowest thing in the session, and until this rider they did not start until
    that walk finished and they tapped Continue — several seconds of dead air on
    a screen that looked stalled.

    **This is the ONLY background thread stage 1 starts, and it cannot apply.**
    Its target computes a candidate and banks it on the conductor; making that
    candidate real still needs the household's own confirmation, and applying it
    still needs their separate POST from the review screen (work order D1). The
    ``handle_v2_apply``-on-a-thread this file used to run is gone and is not
    coming back through here — pinned by
    ``test_no_session_path_applies_anything``, which reads this function's
    source as well as the runner's.

    Fire-and-forget by design: every reason not to run is checked inside
    ``run_speculative_group_close`` under the conductor's own lock, and the
    result is an optimisation the confirm path never depends on. A thread that
    cannot be started is therefore a logged non-event — the confirm fits
    exactly as it did before this rider — and never a capture failure.

    Daemonised so a session torn down mid-fit (Stop, a wizard restart) cannot
    hold the process open waiting for work whose answer nobody will read.
    """
    try:
        thread = threading.Thread(
            target=conductor.run_speculative_group_close,
            name="jts-v2-eager-fit",
            daemon=True,
        )
        thread.start()
    except RuntimeError:
        # Thread exhaustion on a loaded Pi. The fit still happens, just on the
        # confirm where it always used to.
        log_event(
            logger,
            "correction.crossover_v2_speculative_close_unstarted",
            level=logging.WARNING,
            session_id=getattr(conductor, "session_id", ""),
        )
        return None
    return thread


# The key :func:`attach_stage2_preflight` writes onto ``status["crossover_v2"]``
# and :func:`~jasper.active_speaker.crossover_envelope_v2._stage2_preflight`
# reads. Spelled once, here, because the writer and the reader live in
# different packages and a literal in each is how they drift.
STAGE2_PREFLIGHT_KEY = "stage2_preflight"


def attach_stage2_preflight(status: MutableMapping[str, Any]) -> None:
    """Compute the stage-2 openability DISCLOSURE for the REVIEW screen (D3).

    Runs ``resolve_conductor_context`` — the SAME fail-closed predicate the
    apply transaction re-runs in :func:`_assert_stage_2_can_open` and stage 2
    itself will run, never a cheaper lookalike free to disagree with either —
    and stamps the refusal's own sentence under ``STAGE2_PREFLIGHT_KEY`` for
    the envelope to render as a warning. It does NOT gate the Apply control;
    the apply transaction is the boundary that refuses a truly un-openable
    stage 2. The disclosure stays at render time because the refusal is
    knowable NOW — #1828 moved this predicate early so a household would not
    burn a link and walk to the phone to hit a deterministic refusal that was
    knowable before any of it.

    Not free: one call is roughly six JSON reads, a canonical-JSON SHA-256
    profile fingerprint, and a preset compile, and
    ``ensure_crossover_preview_ready()`` can WRITE the preview and topology
    files (self-limiting — an already-ready preview is left byte-untouched).
    The two gates below keep that off polled screens: review phase only, and
    no candidate ⇒ nothing to apply ⇒ nothing to disclose (measured before
    the candidate gate existed: ~80 calls per held-set window at the wizard's
    1.5 s poll).

    Mutates ``status`` in place (the established shape on this path:
    ``handle_status`` sets ``payload["capture"]`` the same way) so the envelope
    builder stays the pure ``status → envelope`` function it is.
    """
    from jasper.active_speaker.crossover_v2.journey import PHASE_REVIEW

    v2 = status.get("crossover_v2")
    if not isinstance(v2, MutableMapping) or v2.get("phase") != PHASE_REVIEW:
        return
    candidate = v2.get("candidate")
    if not isinstance(candidate, Mapping) or not candidate.get("fingerprint"):
        return
    try:
        resolve_conductor_context(status)
    except CrossoverV2Refused as exc:
        message = str(exc)
        v2[STAGE2_PREFLIGHT_KEY] = {
            "ok": False,
            "message": message,
            "next_action": refusal_next_action(exc),
        }
        # A user-visible dead end gets a named line nobody has to guess at —
        # this is where an operator reads why the review screen warned.
        log_event(
            logger,
            "correction.crossover_v2_stage2_preflight_refused",
            level=logging.WARNING,
            code=str(getattr(exc, "code", "") or ""),
            detail=message,
        )
        return
    except (OSError, RuntimeError, TypeError, ValueError):
        # "We could not check" must not render quiet: the disclosure fails
        # closed even though the Apply control no longer keys on it.
        v2[STAGE2_PREFLIGHT_KEY] = {
            "ok": False,
            "message": (
                "JTS could not check whether it can run the confirming "
                "measurement after applying this. Measure again to try afresh."
            ),
            "next_action": None,
        }
        log_event(
            logger,
            "correction.crossover_v2_stage2_preflight_refused",
            level=logging.WARNING,
            code="preflight_unavailable",
            detail="the stage-2 openability predicate raised",
        )
        return
    v2[STAGE2_PREFLIGHT_KEY] = {"ok": True, "message": "", "next_action": None}


# --------------------------------------------------------------------------- #
# endpoint preparation (S1a/S1d)
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# the position gate — the arm's, and the hand-released round's
# --------------------------------------------------------------------------- #

#: How long ONE position hold waits for whoever is moving the microphone
#: before the session refuses rather than holding forever. Ten minutes covers
#: the slower mover — a person walking a tape to the next bearing and posting
#: the release.
#:
#: A hold is unbounded as far as the transport is
#: concerned — the capture page re-posts the same begin every 1.5 s and each
#: re-post rearms the runner's inactivity deadline — so without this budget
#: nothing below this module would end a hold nobody answers, leaving the
#: speaker holding its measurement volume, its paused voice and the
#: position slot indefinitely.
#:
#: **This is a PER-HOLD bound, and it is not the operative total.** The session's
#: own wall-clock ceiling
#: (:func:`~jasper.active_speaker.crossover_v2_flow.session_wall_clock_ceiling_s`,
#: derived per plan) covers the WHOLE walk, so a run spending anywhere near this
#: budget on several holds ends on that ceiling long before any individual hold
#: expires. A mover that stalls once is caught here by name; one that is merely
#: slow at every position is caught by the ceiling, and since issue #2506 that
#: death has its OWN name — :data:`SESSION_CEILING_EXPIRED_CODE`, raised by
#: :meth:`PositionGate.gate` once
#: :func:`enforce_session_volume_ceiling_if_stale` reports the walk outlived its
#: ceiling.
REMOTE_POSITION_HOLD_BUDGET_S = 600.0

#: Machine reasons the gate answers a begin with. Stable strings: a driver
#: branches on these, and the phone renders the message beside them.
#:
#: The three TERMINAL ones are the registry's own codes, aliased rather than
#: re-spelled: they are persisted as this session's failure and rendered to a
#: household through ``REASON_REGISTRY``, so a second literal here would be a
#: second definition of the same verdict. ``POSITION_HOLD_CODE`` has no registry
#: entry on purpose — a deferral is not a terminal verdict and never reaches a
#: failure screen.
POSITION_HOLD_CODE = "awaiting_position"
POSITION_HOLD_EXPIRED_CODE = REASON_POSITION_HOLD_EXPIRED
POSITION_TARGET_MISSING_CODE = REASON_POSITION_TARGET_MISSING
SESSION_CEILING_EXPIRED_CODE = REASON_SESSION_CEILING_EXPIRED

#: The gate's terminal codes, as a set — what the teardown arm below tests a
#: refusal against to know the runner already published an honest
#: ``capture_refused`` for it.
POSITION_GATE_TERMINAL_CODES = frozenset(
    {
        REASON_POSITION_HOLD_EXPIRED,
        REASON_POSITION_TARGET_MISSING,
        REASON_SESSION_CEILING_EXPIRED,
    }
)

#: The endpoint whoever moved the microphone POSTs to report it in place — the
#: arm's driver, or the person on a hand-released round (#2879). One for both.
POSITION_READY_ENDPOINT = "/sound/speaker/crossover/v2/position-ready"


class PositionGate:
    """Holds a gated session's begin until the angle reached is reported.

    **Why a gate exists at all.** A prompted pose is a promise that the
    microphone has arrived, and the tone must not play before something makes
    it. Two shapes need THIS to make it
    (:attr:`~jasper.active_speaker.crossover_v2_flow.V2PlanShape.positions_gated`):

    * the REMOTE tier has no hand, so its entries auto-begin behind a countdown
      (:data:`~jasper.active_speaker.crossover_v2_flow.AUTO_ADVANCE_COUNTDOWN`)
      and the gate REPLACES the promise the tap was making — the driver moves
      the arm, waits its own settle, and POSTs;
    * a HAND-WALKED round on the WIRED source keeps its tap, but that tap is on
      a capture page that does not exist here, so without the hold the local
      walk fires every capture back to back while the household is still
      walking. The person releases each begin instead.

    A begin is ADMITTED only on a release naming its ``(index, attempt)``,
    whoever sent it; the hold's other two exits REFUSE (the bounds below) or
    ABANDON (:meth:`abandon_hold`).

    **The mechanism is the shipped soft-hold, not a new one.**
    :class:`~jasper.active_speaker.crossover_v2.capture_source.CaptureBeginDeferred` is the
    purpose-built non-terminal deferral: the Pi answers ``capture_deferred``,
    the phone parks on a wait screen with no affordance and re-posts the
    IDENTICAL begin every 1.5 s, the attempt budget is not spent, and the
    session does not end. Nothing on the capture page changes to support this —
    which matters, because that page is a separately deployed artifact and a
    release-order coupling is exactly what a gated shape must not introduce.

    **Every begin is gated, including the 0° ones.** CHECK, MEASURE, the entry
    baseline, and stage 2's anchor are design-axis captures; the microphone has
    to be put back there as deliberately as it was moved away, and a gate that
    assumed "probably still on axis" would measure whatever the last pose left
    behind. Gating is per ``(index, attempt)``, so a retake re-gates — the same
    uniform rule rather than a special case that has to be reasoned about.

    **Two bounds REFUSE a hold, and they name different failures** (#2506).
    :data:`REMOTE_POSITION_HOLD_BUDGET_S` is the per-hold one: a mover that
    STOPPED answering. The session's own wall-clock ceiling is the cumulative
    one: a mover answering every position, just too slowly to finish the walk.
    The second is not measured here — this class keeps no session clock, because
    the ceiling already has an owner (``SessionVolumePlan``'s ``opened_at`` plus
    the stage's stamped ceiling) and a second clock for one bound is a second
    definition of it. :meth:`note_session_ceiling_expired` is how the owner's
    finding reaches the hold that is blocking on it.

    Thread-safe: :meth:`gate` runs on the capture worker thread,
    :meth:`release` and :meth:`note_session_ceiling_expired` on HTTP handler
    threads.
    """

    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        self._lock = threading.Lock()
        self._clock = clock or time.monotonic
        self._pending: dict[str, Any] | None = None
        self._released: set[tuple[int, int]] = set()
        self._opened_at: float | None = None
        self._session_ceiling_expired = False

    # -- the capture worker's side ----------------------------------------- #

    def gate(self, index: int, attempt: int, entry: Any) -> None:
        """Admit this begin, or raise to hold/refuse it.

        Returns cleanly once this ``(index, attempt)`` has been released;
        raises :class:`CaptureBeginDeferred` while it has not, and
        :class:`CaptureBeginRefused` when the hold outlived
        :data:`REMOTE_POSITION_HOLD_BUDGET_S`, when the whole walk outlived the
        session's wall-clock ceiling, or when the entry carries no target.
        """
        from jasper.active_speaker.crossover_v2.capture_plan import (
            AUTO_ADVANCE_TAP,
            POSITION_DEG_KEY,
            POSITION_ROLE_KEY,
            POSITION_VERTICAL_DEG_KEY,
            elevation_clause,
        )
        from jasper.active_speaker.crossover_v2.capture_source import (
            CaptureBeginDeferred,
            CaptureBeginRefused,
        )

        screen = getattr(entry, "screen", None) or {}
        raw_degrees = screen.get(POSITION_DEG_KEY)
        if raw_degrees is None:
            # Fail loud rather than measure an unknown position. A GATED plan
            # emits the key on EVERY entry, so reaching this means the plan and
            # the gate disagree about the session's shape.
            with self._lock:
                self._pending = None
                self._opened_at = None
            raise CaptureBeginRefused(
                POSITION_TARGET_MISSING_CODE,
                "This measurement did not say where the microphone should be.",
            )
        target = int(raw_degrees)
        # Absent reads as mark height, which is what an entry that states no
        # elevation means; the bearing above has no such default because a
        # missing target is the plan/gate disagreement refused just above.
        vertical = int(screen.get(POSITION_VERTICAL_DEG_KEY) or 0)
        # Every sentence this gate publishes about the pose carries it, so the
        # raised stop and the design-axis anchor it is measured against can
        # never render as the same hold.
        rise = f", {elevation_clause(vertical)}" if vertical else ""
        role = str(screen.get(POSITION_ROLE_KEY) or "")
        key = (int(index), int(attempt))
        now = self._clock()
        with self._lock:
            if key in self._released:
                self._pending = None
                self._opened_at = None
                return
            # BOTH refusals are decided before a NEW hold is published, so the
            # modal ceiling death (release N, then the page's begin for N+1)
            # emits ONE event instead of announcing a hold and refusing it in
            # the same breath. This does not reorder the two bounds: a hold
            # that has not opened has waited 0.0 s, which can never exceed the
            # per-hold budget, so that check is vacuous here and the ordering
            # below is exactly the ordering an OPEN hold sees.
            opened = self._opened_at
            waited = 0.0 if opened is None else now - opened
            if waited > REMOTE_POSITION_HOLD_BUDGET_S:
                self._pending = None
                self._opened_at = None
                log_event(
                    logger,
                    "correction.crossover_v2_position_hold_expired",
                    level=logging.WARNING,
                    index=int(index),
                    attempt=int(attempt),
                    degrees=target,
                    waited_s=round(waited, 1),
                )
                raise CaptureBeginRefused(
                    POSITION_HOLD_EXPIRED_CODE,
                    "Nothing reported the microphone in place, so the "
                    "measurement stopped waiting.",
                )
            # Checked SECOND, so a stalled mover keeps the specific diagnosis
            # even when both bounds are past: "nothing answered this position"
            # is the more actionable of the two, and the cumulative name would
            # otherwise absorb it on any walk long enough to reach the ceiling.
            # ``waited_s`` on this line is the CURRENT hold's own wait, and a
            # small value is the point — it is how the journal says no
            # individual hold expired.
            if self._session_ceiling_expired:
                self._pending = None
                self._opened_at = None
                log_event(
                    logger,
                    "correction.crossover_v2_session_ceiling_expired",
                    level=logging.WARNING,
                    index=int(index),
                    attempt=int(attempt),
                    degrees=target,
                    waited_s=round(waited, 1),
                )
                raise CaptureBeginRefused(
                    SESSION_CEILING_EXPIRED_CODE,
                    "The measurement ran out of time before the microphone "
                    "reached every position.",
                )
            if opened is None:
                # A NEW hold: publish what is being waited on and start its clock.
                self._opened_at = now
                self._pending = {
                    "index": int(index),
                    "attempt": int(attempt),
                    "degrees": target,
                    "vertical_deg": vertical,
                    "role": role,
                    # The words a PERSON acts on, lifted verbatim off the entry
                    # the runner already handed us — the same
                    # progress/title/body the capture page renders, composed
                    # once in ``capture_plan`` and shared by every transport
                    # (#2881). ``degrees`` above is the MOVER's number and says
                    # nothing a household can follow; a browser walking the
                    # round needs the sentence, and re-deriving it here from
                    # the index would be a second copy of the plan's own copy.
                    # Absent keys collapse to "" rather than a partial dict, so
                    # a renderer can test one shape.
                    "prompt": {
                        key: str(screen.get(key) or "")
                        for key in ("progress", "title", "body")
                    },
                    # Whether a PERSON is expected to release this hold, which
                    # is the question a surface offering a release control has
                    # to answer and is NOT the same question as the transport.
                    # Both gated shapes reach here, and only one of them has a
                    # hand: an externally positioned walk auto-begins behind a
                    # countdown because its arm's driver POSTs the release
                    # (ADR-0188 §4 keeps that rig off the household's screen),
                    # while a hand-released round keeps the tap policy exactly
                    # because a person is standing there. The plan already
                    # states which, per entry, so this reads its answer rather
                    # than minting a second one.
                    "hand_released": (
                        str(screen.get("auto_advance") or "") == AUTO_ADVANCE_TAP
                    ),
                    "action": {
                        "id": "crossover_v2_position_ready",
                        # The SIGN tells the two off-axis sides apart, so it
                        # stays where it distinguishes something — but "+0°"
                        # distinguishes nothing and reads as a typo on a button
                        # a household presses, and "on the design axis (0°)" is
                        # how the pose's own prompt names that spot. The
                        # elevation rides along because a walk states its
                        # vertical stops at 0° BEARING: without the clause two
                        # different spots share one button.
                        "label": (
                            "Microphone is on the design axis (0°)"
                            if target == 0
                            else f"Microphone is at {target:+d}°"
                        ) + rise,
                        "endpoint": POSITION_READY_ENDPOINT,
                        "body": {
                            "index": int(index),
                            "degrees": target,
                            "vertical_deg": vertical,
                        },
                    },
                }
                log_event(
                    logger,
                    "correction.crossover_v2_position_pending",
                    index=int(index),
                    attempt=int(attempt),
                    degrees=target,
                    vertical_deg=vertical,
                    role=role,
                )
        raise CaptureBeginDeferred(
            POSITION_HOLD_CODE,
            f"Waiting for the microphone to reach {target:+d}°{rise}.",
        )

    # -- the releasing side ------------------------------------------------- #

    def pending(self) -> dict[str, Any] | None:
        """What this session is waiting for, or ``None`` — the envelope's read."""
        with self._lock:
            return dict(self._pending) if self._pending else None

    def note_session_ceiling_expired(self) -> None:
        """Record that the walk outlived the session's wall-clock ceiling.

        Called by the one component that DETECTS it —
        :func:`enforce_session_volume_ceiling_if_stale`, the lazy enforcement
        the wizard/driver poll already runs — rather than sampled here, for two
        reasons. The ceiling belongs to ``SessionVolumePlan``, which stamps the
        ``opened_at`` and the stage's own ceiling it is measured against; and
        that enforcement DRAINS the volume it finds stale, so the plan stops
        reporting ``stale_active`` a poll later. A gate that sampled the plan
        would therefore race the drain and lose the fact it was looking for.
        This is a LATCH for exactly that reason: once the walk has outlived its
        ceiling, no later state can un-say it.

        Idempotent, and safe to call with no hold pending — the next held begin
        is the one that reports it, and a session whose captures are all done
        never asks again.
        """
        with self._lock:
            self._session_ceiling_expired = True

    def abandon_hold(self) -> None:
        """Forget the hold that is open — nothing is running it any more.

        A hold's identity is the ``(index, attempt)`` its begin named, and
        :meth:`gate` publishes a NEW ``pending`` only when no hold is open
        (``_opened_at is None``). That is what makes a re-posted begin
        idempotent: the same begin re-entering an open hold must not restart
        its clock or re-announce it.

        The one caller that walks AWAY from a held begin — the wired runner
        abandoning it to re-open the previous slot as a retake — has to say so,
        or the next begin is treated as a continuation of the abandoned one:
        the envelope keeps naming the position nobody is measuring any more,
        the operator is asked to walk to the wrong spot, and a release for the
        begin that IS running is refused as a stale index. The hold then spends
        its whole :data:`REMOTE_POSITION_HOLD_BUDGET_S` on a target nothing is
        waiting for.

        Idempotent, and safe with no hold open. It deliberately does NOT touch
        ``_released`` (a release already given stays given) or the
        ceiling latch (a walk that ran out of time has still run out of time).
        """
        with self._lock:
            if self._pending is None and self._opened_at is None:
                return
            abandoned = self._pending
            self._pending = None
            self._opened_at = None
        if abandoned:
            log_event(
                logger,
                "correction.crossover_v2_position_hold_abandoned",
                index=int(abandoned["index"]),
                attempt=int(abandoned["attempt"]),
                degrees=int(abandoned["degrees"]),
            )

    def release(self, index: int | None = None) -> dict[str, Any]:
        """Report the microphone in place for the pending capture.

        ``index`` is checked against what is actually pending rather than
        ignored: a release re-POSTed after the capture already began must NOT
        release the NEXT position, which is the one hazard an untargeted latch
        would introduce. A retry that still matches the pending index is
        idempotent.

        Raises :class:`ValueError` when nothing is pending or the index names a
        different capture — the caller maps that to a 409.
        """
        with self._lock:
            pending = self._pending
            if not pending:
                raise ValueError(
                    "no measurement is waiting for the microphone right now"
                )
            wanted = int(pending["index"])
            if index is not None and int(index) != wanted:
                raise ValueError(
                    f"measurement {wanted} is waiting, not {int(index)}"
                )
            self._released.add((wanted, int(pending["attempt"])))
            released = dict(pending)
            self._pending = None
            self._opened_at = None
        log_event(
            logger,
            "correction.crossover_v2_position_released",
            index=int(released["index"]),
            attempt=int(released["attempt"]),
            degrees=int(released["degrees"]),
        )
        return released


@dataclass(frozen=True)
class V2PreparedSession:
    """What the correction_setup dispatch needs to host one v2 session."""

    label: str
    open: Callable[[], Any]
    run_and_consume: Callable[[Any], Any]
    request_stop: Callable[[], None]
    #: This session's position gate, or ``None`` for an ungated one. Built for
    #: either GATED shape (``V2PlanShape.positions_gated``): the remote tier,
    #: and a hand-walked round. ``correction_setup`` reads
    #: :meth:`PositionGate.pending` into the status block the envelope renders,
    #: and routes the release POST to :meth:`PositionGate.release`.
    position_gate: PositionGate | None = None
    #: The session's completion signal for a held set (work order D1). The W3
    #: wizard surface is what will POST this; it exists now because the
    #: held-set walk semantics need a signal source to be complete.
    request_complete: Callable[[], None] | None = None
    #: The session's per-take RETAKE signal. ``None`` on a GATELESS session,
    #: which never waits for a person to serve it in. Routed from
    #: ``POST /crossover/v2/retake`` through the same slot as
    #: :attr:`request_complete`, and honoured in either window where the walk
    #: is waiting on a person: a HELD BEGIN, or the held-set window.
    request_retake: Callable[[], None] | None = None


def _volume_hooks(
    camilla_factory: Any,
    context: V2ConductorContext,
    *,
    tuning: Any,
    volume_claim: Any = None,
) -> V2VolumeHooks:
    plan = session_volume_plan()
    # THE SAME CLAIM the engine session holds, not a second one. The plan's
    # door establishes through it and ``TuningSession.open`` takes the session
    # slot through it; two ``MeasurementVolumeClaim`` objects over one owner
    # would collide on the owner's same-kind rule, and that rule is there for
    # OTHER holders — level-match, autolevel, the balance guard — not for a
    # session arguing with itself.
    door = _volume_door(camilla_factory, claim=volume_claim, reason="session")

    async def _open() -> Any:
        # Hold voice paused for the WHOLE session BEFORE setting the volume, so
        # the idle reconciler cannot revert the measurement volume in the gap
        # before the first play (W6.1). If the volume does not actually open —
        # a non-OPENED result OR any raise — release the pause in the finally
        # so a failed open never strands voice paused.
        await acquire_session_measurement_pause()
        opened: Any = None
        try:
            opened = await plan.open(context.session_volume_db, door)
        finally:
            if str(getattr(opened, "value", opened)) != "opened":
                await release_session_measurement_pause()
        if str(getattr(opened, "value", opened)) != "opened":
            # RETURN, rather than falling through. A volume that did not
            # confirm is not a volume anything may be admitted against, so
            # installing the measurement graph here would buy two CamillaDSP
            # swaps — install and restore — for a session that never plays.
            # The runners' own non-OPENED branch is what tells the household
            # why, and it is only reachable if this result gets back to it.
            #
            # A refused CLAIM lands here too, and that is the NB1 property one
            # frame down: ``plan.open`` establishes through the claim, so a
            # session another holder's level-match still owns is refused before
            # ``tuning.open()`` is ever called, and costs zero graph swaps.
            return opened
        # AFTER the plan confirms, and that order is the whole of it. The plan
        # writes its durable intent BEFORE the claim is taken, so a crash in
        # the gap hydrates as a recoverable state rather than a forgotten one;
        # then ``tuning.open()`` finds the session's claim already held at this
        # level and takes the graph. The two are symmetric: the session opens
        # where the plan opened, and closes where the graph went back.

        async def _give_back_the_level() -> None:
            """Drain the level this open took, then let voice go.

            Inner order is load-bearing: the drain runs even if the pause
            release fails, because a fader left at measurement level is worse
            than voice left paused.
            """
            try:
                await plan.abandon(door, reason="session_open_failed")
            finally:
                await release_session_measurement_pause()

        # A ``finally`` on a success flag rather than a broad ``except``: this
        # has to cover a CANCELLED open as well as a raising one, and a
        # ``CancelledError`` is not an ``Exception``. An open that got the
        # level but not the graph must keep NEITHER.
        session_open = False
        try:
            await tuning.open()
            session_open = True
        finally:
            if not session_open:
                # SHIELDED, the same shape ``TuningSession._release_slots``
                # uses: a cancel landing inside the drain aborts it and leaves
                # the fader at measurement level with nobody holding it — and
                # because the plan never latched, ``needs_recovery`` answers
                # False, so the recovery screen never offers. Only the
                # wall-clock ceiling would ever notice.
                giving_back = asyncio.ensure_future(_give_back_the_level())
                try:
                    await asyncio.shield(giving_back)
                except asyncio.CancelledError:
                    while not giving_back.done():
                        try:
                            await asyncio.shield(giving_back)
                        except asyncio.CancelledError:
                            continue
                        except (OSError, RuntimeError, TimeoutError, ValueError):
                            # The same enumerated set ``_put_the_graph_back``
                            # catches below, and for its reason: a drain that
                            # will not complete must not replace the caller's
                            # cancellation with its own symptom.
                            break
                    raise
        return opened

    async def _put_the_graph_back() -> None:
        """Restore the entry graph before the volume closes, never at its cost.

        BEFORE, and the reason is isolation rather than the fader. The restore
        no longer ducks (wave 6d), so there is no release reference to land in
        the right place — but the ``finally`` below releases the measurement
        pause, and once that goes the household programme can resume. A graph
        swapped after it lands under live audio, which is exactly the condition
        an un-ducked swap is only safe in the absence of. The graph goes back
        through the SESSION and nowhere else — this arm's ``close``, or the
        session's own failed-open teardown; no out-of-runner drain touches it
        (see :func:`_release_pause_best_effort`).

        NEVER AT ITS COST, because a graph that will not come back must not
        stop the fader coming down: that would strand the speaker in the
        measurement graph AND at measurement volume, which is the worse of the
        two failures by a wide margin.
        """
        from jasper.active_speaker.crossover_v2.session_graph import SessionGraphError

        try:
            # The session gives back what it took, and BOTH halves are real
            # now: the graph goes back first, then the claim is released and
            # the owner lands the standing household level. The plan's drain
            # below then re-asserts its own durable snapshot on top, which is
            # what makes one authority out of two definitions of "where the
            # fader belongs" — the snapshot is last and wins. Called HERE and
            # not around the run, so the order this docstring promises is the
            # order that happens. Graph first is THIS path's order
            # (``_give_back_held``); the failed-open teardown deliberately
            # releases volume first, which is why no reader should take the
            # ordering as universal.
            #
            # It restores here or not at all — nothing downstream will do it
            # later, which is what makes the failure below CRITICAL rather
            # than a retryable warning.
            await tuning.close()
        except (SessionGraphError, OSError, RuntimeError, TimeoutError, ValueError):
            log_event(
                logger,
                "correction.crossover_v2_session_graph_restore_failed",
                level=logging.CRITICAL,
                exc_info=True,
            )

    async def _close() -> Any:
        try:
            await _put_the_graph_back()
            return await plan.close(door)
        finally:
            await release_session_measurement_pause()

    async def _abandon() -> Any:
        try:
            await _put_the_graph_back()
            return await plan.abandon(door)
        finally:
            await release_session_measurement_pause()

    return V2VolumeHooks(open=_open, close=_close, abandon=_abandon)


# --------------------------------------------------------------------------- #
# stage capabilities — declared by the journey, bound here
# --------------------------------------------------------------------------- #
#
# The declarations moved to
# :mod:`jasper.active_speaker.crossover_v2.journey` in #2291 Phase 4: which
# seams a stage provides and which priors it needs are facts about the
# COMMISSION, and a host that owns them is a host owning domain semantics.
# Re-exported here — the same objects, not copies — because these names are
# this module's published surface and every reader of "which stage binds
# rollback" should keep finding the one answer.
#
# What stays here is the binding itself: which callable implements each seam,
# and the journal line that says what a stage opened with. Both are the host's,
# and :func:`bind_v2_stage_seams` remains their single owner.

def _active_graph_fingerprint() -> str:
    """Identity of the Layer-A profile currently on the speaker, or ``""``.

    The conductor's ``entry_graph_fingerprint`` seam (#2291): which DSP graph
    the entry baseline was measured through, so a receipt can say what the
    "before" was a before OF.

    **Reuses the existing owner rather than hashing anything here.**
    :func:`~jasper.active_speaker.baseline_profile.load_applied_baseline_profile_state`
    returns the frozen applied SSOT with its ``candidate_fingerprint``
    *recomputed* from the immutable source + snapshot by
    :func:`~jasper.active_speaker.baseline_profile.baseline_candidate_fingerprint`
    — that repair is why this reads the stored field instead of re-deriving it:
    the loader has already refused to trust a stale or absent stamp. One hash
    function, one definition of "which graph".

    ``""`` for a speaker with no applied profile — its first-ever round, where
    the entry graph genuinely has no identity to name. The conductor turns that
    into its own ``unknown`` word; this function does not invent one, because
    "the loader found nothing" and "the conductor has no seam" are the same
    answer to the round and should not become two vocabularies.
    """
    from jasper.active_speaker.baseline_profile import (
        APPLIED_PROFILE_DISPLACED,
        applied_profile_displacement,
        load_applied_baseline_profile_state,
    )

    applied = load_applied_baseline_profile_state()
    if not isinstance(applied, Mapping):
        return ""
    # The record is only an answer to "which graph is on the speaker" while it
    # is still the graph on the speaker (#2537). An out-of-band reconcile
    # changes the RUNNING config without touching this record, and a receipt
    # that then named the record's fingerprint would assert a graph the speaker
    # had not played for hours — which is exactly the 2026-08-15 cycle-4 shape,
    # one layer up from the restore it misdirected. A displaced record answers
    # ``""``, which the coordinator turns into its own ``unknown`` word: the
    # honest "we cannot name it", not a wrong name.
    #
    # ONLY on a positive displacement. The other two codes mean the comparison
    # could not be made — no statefile to read, no path on the record — and
    # this module's standing rule is that an absent measurement is not evidence
    # of a defect. Dropping a fingerprint because a statefile was unreadable
    # would make every box without one report ``unknown`` forever.
    if applied_profile_displacement(applied) == APPLIED_PROFILE_DISPLACED:
        log_event(
            logger,
            "correction.crossover_v2_applied_profile_displaced",
            level=logging.WARNING,
            surface="entry_graph_fingerprint",
        )
        return ""
    return str(applied.get("candidate_fingerprint") or "")


def _previous_candidate_known() -> bool:
    """Can the automatic revert actually go back? (#2291's ``rollback_available``)

    The state half; the seam half — is the conductor's ``rollback`` bound at
    all — is the coordinator's own to check, and it ANDs the two. Three facts,
    each the answer half of a question the ACTION then re-asks, so the round's
    promise cannot drift from what the doors do:

    * a prior candidate fingerprint is recorded — the SAME field the
      auto-revert resolves its target from
      (:func:`~jasper.web.correction_crossover_v2_status._previous_candidate_fingerprint`);
    * the pointer is PAIRED to the apply this round is grading
      (``previous_candidate_displaced_by`` equals the published candidate's
      fingerprint) — a pointer stamped by an older apply, or consumed by the
      automatic revert itself, routes to ``recovery_required`` instead of
      promising a restore the seam then refuses;
    * the republish door would admit that fingerprint
      (:func:`~jasper.web.correction_crossover_v2_republish.republish_preflight`
      — the bank still holds a verifiable artifact and the declaration gate
      passes).

    The APPLY door's own gates are deliberately not re-asked here — they read
    live SSOT that a probe cannot pre-answer — and the live displacement check
    runs at the moment of action (:func:`bind_delta_probe_rollback`), exactly
    the static/live split the old five-gate probe kept. A raising preflight is
    caught by the coordinator's seam reader, which fails closed.
    """
    from jasper.web import correction_crossover_v2_republish as republish_door
    from jasper.web import correction_crossover_v2_status as v2_status

    state = load_v2_state()
    fingerprint = v2_status._previous_candidate_fingerprint(state)
    if fingerprint is None:
        return False
    if not _previous_candidate_paired(state):
        return False
    return republish_door.republish_preflight(fingerprint) is None


def _previous_candidate_paired(state: Mapping[str, Any] | None) -> bool:
    """Was the way-back pointer recorded by the apply now under grade?

    The one equality of the pairing rule: ``previous_candidate_displaced_by``
    — stamped by :func:`observe_apply_success` with the candidate its apply
    installed, and re-stamped to ``None`` by the automatic revert's own
    success — must equal the published candidate's fingerprint. ``False``
    covers a pointer stamped by an older apply (#2559's staleness class), the
    [revert…next-apply] window (no automatic ping-pong; the household's
    way-back button is deliberately NOT gated on this), and a state written
    before the pairing existed (no auto-follow until the next apply records
    one — the same no-schema-bump posture the pointer itself takes).
    """
    resolved = state or {}
    displaced_by = resolved.get("previous_candidate_displaced_by")
    candidate = resolved.get("candidate")
    published = (
        str(candidate.get("fingerprint") or "")
        if isinstance(candidate, Mapping)
        else ""
    )
    return (
        isinstance(displaced_by, str)
        and bool(displaced_by)
        and bool(published)
        and displaced_by == published
    )


def _applied_graph_boosts() -> bool:
    """Does the graph currently on the speaker put energy IN? (#2291)

    #2318's fail-closed cell asks this of the APPLIED intervention, and the
    grading conductor cannot answer it from its own state: stage 2 builds a
    fresh conductor whose ``_candidate`` is never set (only stage 1's commit
    assigns one), so the predicate read ``None`` on every shipped round and
    the cell was unreachable — a boosted round with unprovable benefit ended
    accepted, which is exactly the state that rule exists to prevent.

    **One owner for "what did we apply": the applied profile SSOT.** Not the
    durable ``state["candidate"]``, which is a display summary carrying
    ``linearization_outcome`` and per-octave figures but not the filters; and
    not a re-derivation, because
    :func:`~jasper.active_speaker.baseline_profile.profile_linearization`
    already owns which copy of a profile's linearization is authoritative.
    That mapping is ALREADY reduced, so it goes straight to the shipped
    predicate with no ``linearization_filters_by_role`` in between — that
    reducer returns ``{}`` for an already-reduced mapping, which would read as
    "this graph boosts nothing" and quietly restore the bug.

    **Fails closed.** An unreadable profile answers "boosted", so an
    intervention nobody can inspect comes off rather than staying on evidence
    nobody has — the same direction the conductor takes when this seam is
    absent entirely.
    """
    from jasper.active_speaker.baseline_profile import (
        load_applied_baseline_profile_state,
        profile_linearization,
    )
    from jasper.active_speaker.camilla_yaml import linearization_has_boost

    try:
        return linearization_has_boost(
            profile_linearization(load_applied_baseline_profile_state())
        )
    except (OSError, RuntimeError, TypeError, ValueError, KeyError):
        log_event(
            logger,
            "correction.crossover_v2_applied_boost_unreadable",
            level=logging.WARNING,
            exc_info=True,
        )
        return True


def _applied_profile_now() -> Mapping[str, Any] | None:
    """The Layer-A profile the speaker is playing right now, or ``None`` (#2611).

    The conductor's PREVIOUS-graph seam. Read at MEASURE time, when the apply
    has not happened yet, so "currently applied" and "the graph this apply would
    replace" are the same profile — and read through the same
    :func:`~jasper.active_speaker.baseline_profile.load_applied_baseline_profile_state`
    record ``_applied_graph_boosts`` and :func:`_active_graph_fingerprint` read,
    so the commanded axis, the boost predicate and the entry identity all
    describe one graph.

    **``_applied_offset_gate`` is a DIFFERENT source, and saying otherwise was
    wrong.** That seam reads ``expected_post_apply_offset_db`` off the v2
    durable state (``load_v2_state``), written at Apply time by
    :func:`observe_apply_success` from the two profiles' program headrooms. It
    is a number about an apply, banked once; this is a record about a graph,
    re-read live. They are two accounts of one apply that are meant to be
    disjoint (per-role gains on the commanded axis, the common pre-split gain in
    the offset) and they are not one SSOT with two readers.

    **The displaced-record guard, the same one
    :func:`_active_graph_fingerprint` applies** (#2537's 2026-08-15 cycle-4
    shape). An out-of-band reconcile changes the RUNNING config without
    touching this record, so a displaced record no longer answers "which graph
    is on the speaker" — and this PR makes that record rollback-DECIDING, which
    is a stronger claim than the fingerprint it was first refused for. Only a
    POSITIVE displacement refuses: the other two codes mean the comparison could
    not be made, and an absent measurement is not evidence of a defect.

    **Fails to ``None``, and that is not the fail-closed direction here — it is
    the honest one.** ``None`` makes the commanded axis unavailable and the delta
    probe ``unavailable``: no rollback, and no pass either. There is deliberately
    no fabricated substitute, because grading against a graph nobody ran is the
    defect #2611 records.
    """
    from jasper.active_speaker.baseline_profile import (
        APPLIED_PROFILE_DISPLACED,
        applied_profile_displacement,
        load_applied_baseline_profile_state,
    )

    try:
        applied = load_applied_baseline_profile_state()
        if applied is not None and (
            applied_profile_displacement(applied) == APPLIED_PROFILE_DISPLACED
        ):
            log_event(
                logger,
                "correction.crossover_v2_applied_profile_displaced",
                level=logging.WARNING,
                surface="commanded_axis",
            )
            return None
        return applied
    except (OSError, RuntimeError, TypeError, ValueError, KeyError):
        log_event(
            logger,
            "correction.crossover_v2_applied_profile_unreadable",
            level=logging.WARNING,
            exc_info=True,
        )
        return None


def bind_v2_engine_seams(
    *,
    session_graph: Any,
    evidence_store: Any,
    capture_session_id: str,
    compose_stimulus: Any,
    capture_stimulus: Any = None,
    volume_claim: Any = None,
    routed_phases: bool = True,
) -> Any:
    """This host's parts for the engine binder — web policy only.

    The mechanics live in
    :func:`jasper.active_speaker.crossover_v2.composition.bind_engine_seams`
    (one binder, any front end); what stays here is exactly what only THIS
    host can decide, which is why the wrapper survives the lift:

    * **The claim, and its refusal copy.** ``SessionVolumePlan`` no longer
      writes the fader at all — it keeps the durable half and reaches the same
      :class:`~jasper.volume_owner.VolumeOwner` through ``OwnerVolumeDoor``,
      and the engine's volume seam is the REAL ranked claim, this session its
      first production path. A missing owner is a registration defect refused
      as household copy, because the route's 500 arm renders an unmapped
      exception's string on the wizard's status line.
    * **The record store.** ``BankedRecordStore`` over this session's evidence
      bundle. F9, answered by W1-c and left un-bridged on purpose: ``bank``
      returns a store-relative PATH while the shipped flow publishers write
      artifact FINGERPRINTS into ``refs`` — and they still do. W1-d's index is
      the first reader that will want the path.

    ``capture_stimulus`` records what each stimulus does to the room; the
    transaction names it in the outcome's incident.
    """
    from jasper.active_speaker.crossover_v2.composition import bind_engine_seams
    from jasper.active_speaker.crossover_v2.record_store import BankedRecordStore

    claim = volume_claim if volume_claim is not None else _session_volume_claim()
    if claim is None:
        raise _refuse_without_a_volume_owner("session")
    return bind_engine_seams(
        session_graph=session_graph,
        records=BankedRecordStore(
            evidence=evidence_store,
            capture_session_id=capture_session_id,
        ),
        volume_claim=claim,
        session_volume_plan=session_volume_plan(),
        compose_stimulus=compose_stimulus,
        capture_stimulus=capture_stimulus,
        routed_phases=routed_phases,
    )


def bind_v2_stage_seams(
    opening: StageOpening,
    *,
    play: Any,
    evidence_store: Any,
    capture_session_id: str,
    # ``dict``, not ``Mapping``: the four evidence binders below take the
    # MUTABLE refs dict ``bind_evidence_publishers`` returns and write artifact
    # fingerprints into it. Widening this to ``Mapping`` would type-check here
    # and lie about that.
    refs: dict[str, Any],
    publish_check: Any,
    publish_candidate: Any,
    run_async: Any,
    camilla_factory: Any,
    provenance: CaptureProvenanceRecorder | None = None,
) -> Any:
    """Build one stage's :class:`V2FlowSeams`, and declare what it opened with.

    The single source of truth for the two stage shapes. Both preparers call
    it; neither assembles a ``V2FlowSeams`` of its own, so "which stage binds
    rollback" is answered in exactly one place — the capability declarations in
    :mod:`~jasper.active_speaker.crossover_v2.journey` — instead of being
    re-derived at two call sites that were free to disagree.

    The unconditional seams are unconditional on purpose. ``apply_failed`` is
    never consulted by stage 2 (its conductor is constructed ``applied=True``,
    so ``authorize_begin``'s apply-observed short-circuit runs first), and
    ``publish_cloud`` does nothing for a single-entry recovery re-verify.
    ``bank_take`` is no longer in that company: a recovery re-verify IS an
    accepted VERIFY capture, so it banks a take like any other, and that is
    the wanted behaviour rather than an accident of binding — the round whose
    evidence a household is recovering is exactly the one whose capture should
    survive it. Full's stage 2 is also a post-apply position group whose combined
    curve the after-chart, the post-apply spec verdict, and the delta probe all
    read, and ``V2FlowSeams`` requires the two apply gates outright. Binding a
    seam a plan never exercises costs nothing; omitting one a plan does
    exercise is a silently missing publication.

    ``provenance`` is threaded here only to reach the analyze seam: the SAME
    recorder the caller handed ``bind_production_play``, which is the pairing.

    The shortfall is :attr:`~...journey.StageOpening.missing`, derived where the
    declaration lives so the two cannot disagree. Logging it lives HERE rather
    than in the journey or at the call sites: the journey is a pure aggregate
    with no journal of its own, and a third caller that bound seams without
    declaring them would put the journal's account of stage shape back out of
    one owner's hands.
    """
    from jasper.active_speaker.crossover_v2_flow import V2FlowSeams

    capabilities = opening.capabilities
    missing = opening.missing
    log_event(
        logger, "correction.crossover_v2_stage_capabilities",
        stage=capabilities.stage, session_id=capture_session_id,
        provides=",".join(sorted(capabilities.provides)),
        requires=",".join(sorted(capabilities.requires)),
        missing=",".join(missing),
    )
    if missing:
        # WARNING, and its own event: a required prior that did not cross the
        # bridge does not stop this stage, so nothing else would ever say the
        # verdict it is about to produce was reached with an input absent.
        log_event(
            logger, "correction.crossover_v2_stage_capability_unavailable",
            level=logging.WARNING, stage=capabilities.stage,
            session_id=capture_session_id, missing=",".join(missing),
        )
    # The one-capture handoff from the analyze seam to the banking seam, in
    # the same type the play seam already uses for its own hop. A second
    # recorder rather than a shared slot because the single-shot ``take`` is
    # exactly the semantics this hop needs too: a take banked with no analyze
    # behind it must name no provenance, never the previous capture's.
    banked_provenance = CaptureProvenanceRecorder()
    # The analyze seam's own hop over the same gap, for the blocks only it
    # holds. Bound unconditionally: with the capture-dump ring gone the banked
    # record is the only file these numbers can land in.
    banked_evidence = CaptureEvidenceCarry()
    return V2FlowSeams(
        play=play,
        analyze=bind_production_analyze(
            meta=refs, provenance=provenance, carry=banked_provenance,
            evidence=banked_evidence,
        ),
        publish_check=publish_check,
        publish_candidate=publish_candidate,
        apply_complete=_applied_gate,
        apply_failed=_apply_failure_gate,
        bank_take=bind_position_retention(
            evidence_store, capture_session_id, refs, run_async,
            provenance=banked_provenance, evidence=banked_evidence,
        ),
        publish_cloud=bind_cloud_publisher(
            evidence_store, capture_session_id, refs, run_async
        ),
        # #2291's round receipt. Bound on both stages rather than gated on a
        # capability: only the stage that GRADES a round ever calls it, and a
        # binding that exists everywhere cannot be the reason a receipt went
        # unwritten on the stage that needed it.
        publish_round_receipt=bind_round_receipt(
            evidence_store, capture_session_id, refs, run_async
        ),
        publish_findings=(
            bind_findings_publisher(
                evidence_store, capture_session_id, refs, run_async
            )
            if CAPABILITY_FINDINGS in capabilities.provides else None
        ),
        rollback=(
            bind_delta_probe_rollback(run_async, camilla_factory)
            if CAPABILITY_ROLLBACK in capabilities.provides else None
        ),
        applied_offset_db=_applied_offset_gate,
        # #2611: the graph an apply replaces, for the commanded axis. Bound on
        # both stages for ``entry_graph_fingerprint``'s reason — "what is live
        # right now" is not a stage asymmetry — though only stage 1 commits a
        # candidate and therefore only stage 1 reads it today.
        applied_profile=_applied_profile_now,
        record_model_error=_record_live_model_error,
        rollback_available=_previous_candidate_known,
        # #2291/#2318: "does the APPLIED graph boost". Bound on both stages for
        # ``entry_graph_fingerprint``'s reason — what is live right now is not
        # a stage asymmetry — and it is the only way the grading stage can
        # answer at all, since its conductor never holds the candidate.
        applied_boosts=_applied_graph_boosts,
        # Unconditional on both stages: "which graph is live right now" is not
        # a stage asymmetry, and #2291's receipt is what lets a LATER round
        # bind the currently-active profile as its own entry graph.
        entry_graph_fingerprint=_active_graph_fingerprint,
    )


def _resolve_prepare_wired_mic() -> Any:
    """The measurement mic this prepare opens on (#2662 W2b).

    Resolved ONCE per prepare and threaded into ``_open`` — the mint and the
    runner build must describe one decision, not two reads of a probe that can
    change between them. The provider is imported lazily. A resolution error
    (``WiredMicMissing``) refuses at the tap, before any evidence store or
    durable state is touched, carrying the provider's own code so the journal
    names the disclosure rather than quoting it.
    """
    from jasper.audio_measurement.wired_capture import WiredCaptureError
    from jasper.web import correction_crossover_v2_wired as wired

    try:
        return wired.resolve_v2_wired_mic()
    except WiredCaptureError as exc:
        raise CrossoverV2Refused(
            str(exc), code=str(getattr(exc, "code", "") or ""),
        ) from exc


def _hand_released_plan_shape(plan_shape: Any) -> Any:
    """The same shape, told whether a PERSON releases each of its begins.

    A hand-walked round is the shape that needs saying: nothing paces it, and
    without a hold the local runner fires every capture back to back while the
    household is still walking to the next spot. Its begins are therefore held
    and released by hand (``V2PlanShape.hand_released_positions``), the same
    ``POST /crossover/v2/position-ready`` an external driver uses.

    Every other shape is returned untouched: the arm already holds behind its
    driver's report, and the tier-less recovery re-arm (``plan_shape is
    None``) is one sweep at the mark with no walk to pace at all.
    """
    if plan_shape is None:
        return plan_shape
    if plan_shape.externally_positioned:
        return plan_shape
    return dataclasses.replace(plan_shape, hand_released_positions=True)


def _mint_wired_session(wired_device: Any, spec: Any) -> Any:
    """Mint one session on the measurement mic (#2662 W2b).

    **No Pi-minted per-capture result wait.** The wait #2706 put on the spec was
    the Fc sweep's compute ceiling plus its measured overhead; with no sweep to
    bound, the Pi publishes nothing and ``resultWaitMs`` falls back to the
    page's own 90 s floor — the wall every banked round was measured against,
    and 8.7 s clear of the slowest observed round (81.28 s), which did strictly
    more work than any round runs now.
    """
    from jasper.web import correction_crossover_v2_wired as wired

    return wired.open_wired_capture(spec, device=wired_device)


def _wired_stimulus_capture(wired_device: Any, evidence_store: Any) -> Any:
    """The play seam's capture half: the Pi's own microphone, on the box the
    stimulus comes out of."""
    from jasper.web import correction_crossover_v2_wired as wired

    return wired.WiredStimulusCapture(
        device=wired_device, bundle_dir=Path(evidence_store.bundle_dir),
    )


def _bind_engine_measure_leg(
    *,
    tuning: Any,
    stimulus_capture: Any,
    index_phase_map: Mapping[int, str],
    run_async: Any,
    specs_by_index: Mapping[int, Any] | None = None,
) -> Callable[[int, int, Any], Any] | None:
    """The wired walk's engine leg: MEASURE captures through ``measure()``.

    Returns the ``capture_stimulus(index, attempt, entry)`` callable the wired
    runner drives in place of its own recorder + ``on_armed`` for the indices
    this closure claims, or ``None`` when there is nothing to bind (no local
    capture half). The closure answers ``None`` for every index it does NOT
    claim, and the walk's own path is unchanged for those.

    **It claims every index ``specs_by_index`` names, and the MEASURE ones
    besides.** MEASURE is the one phase the
    engine can drive end-to-end today: its kind exists
    (``MEASURE_KIND_CANDIDATE``), its program is routed (a verify-class summed
    sweep is structurally not re-admittable, so the transaction's readmit gate
    cannot pass one), and its graph discipline matches the session's
    prove-per-stimulus model. CHECK
    and the prompted walks stay on the flow callbacks until their kinds and
    verdicts move engine-side.

    **The whole ``measure()`` runs under the session's measurement isolation**
    — the same :func:`_under_measurement_isolation` selector the flow leg's
    ``_emit`` uses, so a latched isolation-loss abort REFUSES the play before
    any audio and a mid-sweep abort cancels it and surfaces as the named
    ``MeasurementWindowError``, identically on both legs. The engine leg's
    span under the window is wider than the flow leg's (it also covers the
    render, the recording tail and the banking, where ``_play_body`` covers
    graph-prove + hold + play), which errs toward MORE abortability, not less.

    **Failure identity is preserved by re-raising the exception TYPE today's
    path emits** for each engine incident, so ``classify_program_failure`` and
    the frozen persisted codes read identically whichever leg played:

    * ``program_admission_refused`` → ``ProgramAdmissionError`` →
      ``program_unplayable`` (disclosed downgrade: the refusal SLUGS are not
      reconstructable from an incident code, so
      ``state["failure"]["refusals"]`` is empty for a mid-walk admission
      refusal — and with them the ``PROGRAM_PROFILE_NOT_CONFIRMED`` dedicated
      code/screen is unreachable from a mid-walk MEASURE refusal. The slugs
      are still journaled at the admission site itself, and a profile-level
      refusal dies at CHECK — which runs first, on the flow leg, with full
      fidelity.)
    * ``program_play_failed`` (``play_program``'s OWN family) →
      ``ProgramPlaybackError`` → ``program_unplayable``, as today.
    * ``stimulus_emission_failed`` (aplay/device/I-O death —
      ``PlaybackError``/``OSError``, outside the program family) →
      ``CrossoverV2LocalSeamError`` → ``internal_error`` (fix-and-retry), as
      today. A dead aplay must never render the program-unplayable safety
      copy.
    * ``session_level_not_ready`` → ``SessionVolumePlanError`` and
      ``stimulus_not_captured``/``program_not_composed`` → the local-seam
      family → ``internal_error``, as today.
    * **Any incident this table does not name** → ``internal_error``: an
      unknown failure gets the fix-and-retry copy, never safety copy.

    **An UNPROVEN level is not a walk event.** MS-14 is engine-internal
    honesty: the claim's single pre-transaction ``prove()`` read is STRICTER
    than the #2925 hold (no independent re-read; ``None`` on preemption or
    one unreadable round-trip), so treating its refusal as terminal would
    invent a new walk-death today's path does not have — contradicting both
    ``prove()``'s contract ("refuses to BANK … never to play the stimulus or
    to try again") and ``run()``'s "never raises for a measurement problem".
    The #2925 hold still runs INSIDE the play (the compose wrapper), and a
    genuine drift still raises ``MeasurementFaderDrift`` through the leg to
    the same terminal end as the flow leg. So: played + hold held + answer
    minted + ``UNPROVEN_LEVEL`` ⇒ the answer is graded normally (the walk
    sees exactly today's behavior); the engine banks nothing, and the gap is
    a WARNING event plus the outcome's own disclosure.
    """
    if stimulus_capture is None:
        return None
    from jasper.active_speaker.crossover_v2.contracts import (
        MEASURE_KIND_CANDIDATE,
    )
    from jasper.active_speaker.crossover_v2.journey import PHASE_MEASURE
    from jasper.active_speaker.crossover_v2.measure_spec import MeasureSpec
    from jasper.active_speaker.crossover_v2.program_transaction import (
        STIMULUS_ADMISSION_REFUSED,
        STIMULUS_EMISSION_FAILED,
        STIMULUS_LEVEL_NOT_READY,
        STIMULUS_NOT_CAPTURED,
        STIMULUS_PLAY_FAILED,
    )
    from jasper.active_speaker.crossover_v2.session import UNPROVEN_LEVEL
    from jasper.active_speaker.program_admission import ProgramAdmissionError
    from jasper.active_speaker.program_playback import ProgramPlaybackError
    from jasper.active_speaker.session_volume_plan import SessionVolumePlanError
    from jasper.audio_measurement.wired_capture import WiredCaptureError

    # What each claimed capture asks for. The bare candidate spec is the
    # ordinary session's, at every MEASURE index; a staged walk hands over the
    # specs ADOPTION already built and validated, so what reaches the graph is
    # what the refusal gate passed rather than a second construction from the
    # same words. A walk's own stop appears here only when it names a
    # candidate, so a candidate-free walk keeps the leg it already ran on.
    specs: dict[int, Any] = {
        index: MeasureSpec(kind=MEASURE_KIND_CANDIDATE)
        for index, phase in index_phase_map.items()
        if phase == PHASE_MEASURE
    }
    specs.update(specs_by_index or {})
    if not specs:
        return None

    def _raise_as_todays_failure(incident: str) -> NoReturn:
        """The incident, as the exception type the classifier already maps.

        The default arm is ``internal_error`` on purpose: only the incidents
        this table NAMES as the program family may reach the
        program-unplayable safety copy, so an incident a later engine adds
        gets fix-and-retry until somebody classifies it deliberately.
        """
        if incident == STIMULUS_ADMISSION_REFUSED:
            raise ProgramAdmissionError(
                "program re-admission refused (engine measure leg)"
            )
        if incident == STIMULUS_PLAY_FAILED:
            raise ProgramPlaybackError(f"stimulus failed: {incident}")
        if incident == STIMULUS_LEVEL_NOT_READY:
            raise SessionVolumePlanError("no measurement volume is open")
        if incident == STIMULUS_NOT_CAPTURED:
            raise WiredCaptureError(
                "the wired capture half minted no evidence for the stimulus"
            )
        if incident == STIMULUS_EMISSION_FAILED:
            raise CrossoverV2LocalSeamError(
                "the emission mechanism failed (aplay/device/I-O)"
            )
        raise CrossoverV2LocalSeamError(
            f"engine measure leg failed: {incident or 'no incident named'}"
        )

    def _measure_capture(index: int, attempt: int, entry: Any) -> Any:
        # The pose an index was prompted at rides its own spec, so the entry
        # states nothing this leg reads.
        del entry
        spec = specs.get(index)
        if spec is None:
            return None
        async def _measured() -> Any:
            # The selector runs a body and returns nothing (its flow-leg body
            # is side-effecting), so the outcome rides a holder.
            measured: list[Any] = []

            async def _body() -> None:
                measured.append(await tuning.measure(spec))

            await _under_measurement_isolation(_body)
            return measured[0]

        outcome = run_async(_measured())
        if len(outcome.stimuli) != 1:
            # The leg mints a single-stimulus spec and grades THE stimulus
            # against THE drained answer; a spec that produced any other count
            # would pair takes wrongly. Load-bearing assumption, checked.
            raise CrossoverV2LocalSeamError(
                "engine measure leg expects exactly one stimulus, got "
                f"{len(outcome.stimuli)}"
            )
        stimulus = outcome.stimuli[0]
        answer = stimulus_capture.take_answer()
        if stimulus.banked and answer is not None:
            log_event(
                logger,
                "correction.crossover_v2_engine_measure",
                index=index,
                attempt=attempt,
                record_id=stimulus.record_id,
                level_db=stimulus.level_db,
            )
            return answer
        if stimulus.incident == UNPROVEN_LEVEL and answer is not None:
            # Played, recorded, #2925 hold held (a drift would have raised out
            # of the play) — only the claim's stricter pre-play read failed.
            # MS-14 already did its whole job: the ENGINE record is not
            # banked. The walk grades the answer exactly as today; inventing
            # a rejection or a death here would be a new trigger with no
            # flow-leg analog.
            log_event(
                logger,
                "correction.crossover_v2_engine_measure_unproven",
                level=logging.WARNING,
                index=index,
                attempt=attempt,
            )
            return answer
        log_event(
            logger,
            "correction.crossover_v2_engine_measure_failed",
            level=logging.WARNING,
            index=index,
            attempt=attempt,
            incident=stimulus.incident,
            banked=stimulus.banked,
        )
        _raise_as_todays_failure(stimulus.incident or STIMULUS_NOT_CAPTURED)

    return _measure_capture


def _build_wired_run(
    conductor: Any,
    *,
    volume: "V2VolumeHooks",
    stop_event: threading.Event,
    stop_lock: Any,
    position_gate: "PositionGate | None",
    evidence_refs: dict[str, Any],
    wired_device: Any,
    ceiling_s: float,
    complete_event: threading.Event,
    retake_event: threading.Event,
    capture_stimulus: Any = None,
) -> Callable[[Any], Any]:
    """The provider runner, driving the conductor hooks.

    It takes the device, the session ceiling (its confirm-wait bound), the
    local completion and retake signals, and the engine measure leg.
    """
    from jasper.web import correction_crossover_v2_wired as wired

    return wired.build_v2_wired_run_and_consume(
        conductor,
        volume=volume,
        stop_event=stop_event,
        stop_lock=stop_lock,
        device=wired_device,
        ceiling_s=ceiling_s,
        complete_event=complete_event,
        retake_event=retake_event,
        position_gate=position_gate,
        evidence_refs=evidence_refs,
        capture_stimulus=capture_stimulus,
    )


# The request field that selects which post-apply instrument a verify-only
# prepare opens, and its one non-default value.
VERIFY_STAGE_KEY = "stage"
VERIFY_STAGE_POST_APPLY = "post_apply"
VERIFY_STAGE_RECOVERY = "recovery"


def _verify_plan_shape(
    raw: Mapping[str, Any] | None, state: Mapping[str, Any] | None,
) -> Any:
    """Resolve the post-apply plan shape, or ``None`` for the 1-entry recovery.

    Explicit rather than inferred. A shape could be guessed from the durable
    state (has VERIFY been walked? has it been accepted?), but every such
    inference has a case where it silently downgrades Full's multi-position
    post-apply walk to one sweep — a household who opened stage 2 and let the
    link expire before the first capture would get the recovery instrument on
    their next tap and lose the spatial "after" evidence with no way to know.
    The caller says which instrument it wants; the tier still comes from the
    durable state, so the household's tier choice governs both stages.
    """
    from jasper.active_speaker.crossover_v2.capture_plan import resolve_plan_shape
    from jasper.active_speaker.crossover_v2.contracts import CrossoverV2FlowError

    stage = str((raw or {}).get(VERIFY_STAGE_KEY) or VERIFY_STAGE_RECOVERY).strip()
    if stage == VERIFY_STAGE_RECOVERY:
        return None
    if stage != VERIFY_STAGE_POST_APPLY:
        raise CrossoverV2Refused(
            f"unknown verify stage {stage!r} (expected "
            f"{VERIFY_STAGE_POST_APPLY!r} or {VERIFY_STAGE_RECOVERY!r})"
        )
    try:
        return resolve_plan_shape((state or {}).get("tier"))
    except CrossoverV2FlowError as exc:
        raise refused_from_flow_error(exc) from exc


def prepare_v2_session(
    raw: Mapping[str, Any],
    *,
    status: Mapping[str, Any],
    run_async: Any,
    camilla_factory: Any,
    verify_only: bool = False,
) -> V2PreparedSession:
    """Prepare one v2 capture hosting — ``verify_only`` picks which stage.

    ``POST /crossover/v2/session`` (S1a) when false, ``POST
    /crossover/v2/verify`` when true. The flag is the dispatch's own
    discriminator handed one frame down, never re-derived here
    (``correction_setup._handle_crossover_v2_capture``).

    **One sequence, and the nine steps along it that fork.** From the
    capture-source resolution onward both stages run the same steps in the same
    order: resolve the source, rebind the plan shape, open the evidence bundle,
    build the position gate, mint the capture session, re-arm the walked-away
    ceiling, bind the evidence publishers, bind the play seam, open the journey,
    bind the seams, construct the conductor, persist, build the runner, return a
    :class:`V2PreparedSession`. That shared SEQUENCE is why they are one
    function rather than two free to drift apart; it is not a claim that every
    step is identical. Exactly nine of them read ``verify_only`` — the prologue
    (which gates run, and whether the round is RESOLVED from the request or
    REHYDRATED from durable state), the stage-1-only plan flags and staged angle
    walk, the prior-snapshot read against the durable rehydration, the position
    gate's journal line (``stage`` and ``captures``, never the gate predicate,
    which is shared), the session spec builder, the play seam's
    ``protection_sections_by_role`` (stage 2 takes no MEASURE capture, so it
    leaves the seam's own ``None``), the journey's capability set, the
    conductor's constructor and its kwargs, and the returned ``label``.

    ``bind_v2_stage_seams`` is deliberately not among the nine: one call serves
    both stages, which is what keeps "which callable implements each seam"
    answerable in one place instead of at two sites free to disagree.

    **Stage 1** (``verify_only=False``). Gates (fail-closed, before any capture
    registration): the volume-recovery gate (``needs_recovery`` — the W2
    ruling) and the conductor-context resolution. Hydration (S1d): the durable
    state is hydrated through :meth:`CrossoverV2Session.hydrate` with the NEW
    capture session id — a prior session's CHECK/MEASURE evidence is invalidated
    per §5.6 (and logged); the fresh session starts at CHECK.

    ``raw["tier"]`` selects the commission instrument (flow-simplification
    §3): the wizard posts the household's explicit choice, and an unrecognised
    one is refused before any capture registration rather than silently measured
    as something else. It is resolved into ONE :class:`V2PlanShape` here, which
    is then threaded into both the emitted spec and the conductor's index→phase
    map — the two surfaces that must agree about the walk and used to reach
    their defaults independently.

    **An ABSENT tier inherits the lapsed session's, and #2639 is the
    asymmetry that fixed.** ``resolve_plan_shape`` is strict about unknown
    names and lenient about absence, resolving nothing to ``TIER_FULL``; every
    re-measure action the envelope mints posts an empty body, because the
    action does not know what the session was. So a REMOTE session's own retry
    silently minted ``full`` — a tier the turntable rig cannot walk, since full
    is not externally positioned and its verify plan raises in
    ``position_angle_deg`` — and an Express household was demoted by the same
    line. The verify-only stage reads its tier from durable state
    (``_verify_plan_shape``); this reads the same fact from the same place when
    the body does not name one, which makes the two stages agree rather than
    giving the retry a second rule. An explicit body tier still wins: the tier
    chooser is how a household changes instrument.

    **Stage 2** (``verify_only=True``). Requires a durable post-apply state
    (MEASURE accepted + applied). Opens a NEW capture session hosting a
    post-apply plan; the conductor is rebuilt in verify-only mode (CHECK/MEASURE
    marked accepted, applied, verify priors rehydrated from the durable state),
    with its index→phase map built by ``build_v2_verify_index_phase_map``.

    **ONE entry point, two shapes** (two-stage commission work order D2,
    owner-confirmed 2026-07-29) — generalized over the plan shape rather than
    forked into a second builder:

    * ``raw["stage"] == "post_apply"`` — **stage 2**, the tier's own shipped
      verify walk. The tier comes from the durable state the measuring session
      wrote, so the household's choice at the tier chooser governs both stages.
      Express is one position at the mark; Full is the multi-position spatial
      walk whose combined curve the after-chart, the post-apply spec verdict,
      and the delta probe read. Running Full's stage 2 as a single position
      would leave its post-apply group with 0 curves and no combine at all.
    * anything else (the default, and every shipped caller) — the **§5.2
      recovery re-verify**: one entry at the mark, byte-identical to what this
      endpoint has always done, and what a FAILED stage 2 offers.

    An unrecognised ``stage`` is refused rather than silently measured as
    something else — the same strictness ``normalize_tier`` applies to a tier.
    """
    from jasper.active_speaker.crossover_v2.capture_plan import (
        session_wall_clock_ceiling_s,
    )
    from jasper.active_speaker.crossover_v2.coordinator import (
        series_position_from_state,
    )
    from jasper.active_speaker.crossover_v2.programs import measurement_band_hz
    from jasper.active_speaker.crossover_v2.topology_prescription import (
        apply_topology_pin,
    )
    from jasper.active_speaker.crossover_v2_flow import (
        CrossoverV2Session,
        attempt_history_from_state,
    )

    if verify_only:
        from jasper.active_speaker.crossover_v2.capture_plan import (
            build_v2_verify_index_phase_map,
            build_v2_verify_session_spec,
        )
        from jasper.active_speaker.crossover_v2.journey import (
            PHASE_CHECK,
            PHASE_MEASURE,
        )

        if session_volume_plan().needs_recovery:
            raise CrossoverV2Refused(
                "the measurement volume needs recovery; recover it before verifying"
            )
        # ``or {}`` rather than a None check, so the narrowing survives into
        # ``_open`` below: an absent state and an unapplied one are the same
        # refusal, and the empty mapping never reaches past this gate.
        state = load_v2_state() or {}
        if not state.get("applied"):
            raise CrossoverV2Refused(
                "verification needs an applied measured crossover; measure and "
                "apply first"
            )
        attempt_store = _attempt_loop_store_snapshot()
        attempts_loop = state.get("attempts_loop")
        attempts_loop = attempts_loop if isinstance(attempts_loop, Mapping) else {}
        prior_attempt_decision = attempts_loop.get("last_decision")
        candidate_state = state.get("candidate")
        tuning_attempt_id = (
            str(candidate_state.get("fingerprint") or "")
            if isinstance(candidate_state, Mapping) else ""
        )
        plan_shape = _verify_plan_shape(raw, state)
        context = resolve_conductor_context(status)
    else:
        from jasper.active_speaker.branch_chain import confirmed_protection_sections
        from jasper.active_speaker.branch_chain import beaming_onset_hz
        from jasper.active_speaker.crossover_v2.alignment_prescription import (
            ALIGNMENT_PRESCRIPTION_KEY,
            AlignmentPrescriptionRefused,
            read_alignment_prescription,
        )
        from jasper.active_speaker.crossover_v2.topology_prescription import (
            TOPOLOGY_PRESCRIPTION_KEY,
            TopologyPrescriptionRefused,
            read_topology_prescription,
        )
        from jasper.active_speaker.excitation_safety_plan import (
            resolve_driver_protection_slope_db_per_octave,
        )
        from jasper.active_speaker.crossover_v2.capture_plan import (
            STAGE1_INCLUDES_CLOUD_MEASURE,
            STAGE1_INCLUDES_ENTRY_BASELINE,
            build_v2_cloud_index_phase_map,
            build_v2_session_spec,
            resolve_plan_shape,
        )
        from jasper.active_speaker.crossover_v2.contracts import CrossoverV2FlowError
        from jasper.active_speaker.crossover_v2.journey import (
            LATERAL_CONSUMER_FC_SELECTOR,
        )
        from jasper.active_speaker.crossover_v2_flow import (
            V2ConductorSnapshot,
            alignment_delay_search_bounds_us,
        )

        requested_tier = (raw.get("tier") if raw else None) or None
        if requested_tier is None:
            # #2639. Read fresh rather than from any snapshot: this runs before
            # hydration, and the lapsed session's tier is exactly what the durable
            # state's ``tier`` key holds. ``None`` here is still ``None`` to
            # ``resolve_plan_shape`` — a first session, with no lapsed tier to
            # inherit, keeps the shipped default.
            requested_tier = ((load_v2_state() or {}).get("tier")) or None
        try:
            plan_shape = resolve_plan_shape(requested_tier)
        except CrossoverV2FlowError as exc:
            raise refused_from_flow_error(exc) from exc
        if session_volume_plan().needs_recovery:
            raise CrossoverV2Refused(
                "the measurement volume needs recovery; recover it before starting "
                "a new session"
            )
        # W6.1 E1/E3-at-open: force-drain a stale-active (ceiling) and reset a
        # residual owned-active leftover so plan.open() starts clean — the silent
        # 200→adapter_failed loop happened when a prior session left the volume
        # active and open() then refused. Runs AFTER the needs_recovery gate (which
        # refused a crash-active / unresolved state toward the recover screen), then
        # re-gates in case a drain here could not confirm and latched unresolved.
        reconcile_session_volume_for_new_session(run_async, camilla_factory)
        if session_volume_plan().needs_recovery:
            raise CrossoverV2Refused(
                "the measurement volume needs recovery; recover it before starting "
                "a new session"
            )
        context = resolve_conductor_context(status)
        try:
            protection_sections = confirmed_protection_sections(
                context.safety_profile, context.role_targets
            )
        except ValueError as exc:
            raise CrossoverV2Refused(
                "The confirmed driver protection cannot be used for this measurement."
            ) from exc
        # #2662, and it happens HERE for three reasons. It is the untrusted-input
        # boundary, so a malformed or out-of-lobe prescription is refused before any
        # evidence store, capture registration, or capture — an operator walking a
        # delay sweep learns at the tap, not after a ten-minute measurement. It is
        # the first point holding the crossover corner the bound is a half-period
        # of. And it sits AFTER the two speaker-level gates above rather than before
        # them: whether this speaker can be measured at all is a prior question to
        # whether this request's prescription is good, and answering them in the
        # other order would hand a household a prescription error for a speaker
        # whose protection cannot be used either way.
        #
        # Never inherited from the lapsed session's durable state the way ``tier``
        # above deliberately is: a prescription is one round's explicit instruction,
        # and a "measure again" that silently re-ran a candidate would put that
        # candidate's name on a round nobody asked for.
        #
        # The TOPOLOGY pin is read FIRST, and the order is load-bearing rather than
        # alphabetical: it decides the corner this round runs at, and the delay
        # gate below is a half-period AT that corner. Read the other way round, a
        # 4000 Hz round's delay would be bounded by the incumbent 1648.7 Hz lobe
        # (303 us) instead of its own (125 us) — a gate that passes candidates
        # the round it is gating cannot support.
        #
        # Every bound it applies is a DECLARATION, asked of the module that owns
        # it: the two role bands a corner is admissible within (each looked up by
        # role) and the upper driver's declared protective high-pass slope. The
        # ka/beaming onset rides along as DISCLOSURE only (#1675 makes it
        # guidance, and no admissibility bound anywhere reads it).
        #
        # The declarations are gathered ONLY for a request that carries a pin, and
        # that branch is deliberate rather than an optimisation. Several of this
        # context's fields are read nowhere else on this path; deriving them
        # unconditionally would make an ORDINARY round's session-open depend on
        # declarations it is not using, so a context shaped for a decision this
        # round does not take could fail a round that never asked for one.
        raw_topology = (raw or {}).get(TOPOLOGY_PRESCRIPTION_KEY)
        topology_prescription = None
        if raw_topology is not None:
            # The declarations below are a TWO-role reading: a 1-way main passes
            # ``None`` and the reader refuses on the way count instead. Which
            # band is the floor and which the ceiling is a question about ROLES,
            # so both are fetched by name rather than by tuple position. The WAY
            # COUNT is what keeps a 3-way out — it declares both roles too.
            two_way = (
                len(context.roles_bands) == 2
                and "tweeter" in context.role_targets
            )
            tweeter_band = context.declared_band("tweeter") if two_way else None
            woofer_band = context.declared_band("woofer") if two_way else None
            woofer_diameter_mm = context.radiating_diameter_mm_by_role.get("woofer")
            try:
                topology_prescription = read_topology_prescription(
                    raw_topology,
                    declared_floor_hz=(
                        None if tweeter_band is None else tweeter_band.lower_hz
                    ),
                    lower_driver_ceiling_hz=(
                        None if woofer_band is None else woofer_band.upper_hz
                    ),
                    minimum_slope_db_per_octave=(
                        resolve_driver_protection_slope_db_per_octave(
                            context.safety_profile, context.role_targets["tweeter"],
                        ) if two_way else None
                    ),
                    beaming_ceiling_hz=(
                        None if woofer_diameter_mm is None
                        else beaming_onset_hz(float(woofer_diameter_mm))
                    ),
                    # ``None`` is "not stated": the gate stays silent, never guesses.
                    way_count=getattr(context.preset, "way_count", None),
                )
            except TopologyPrescriptionRefused as exc:
                raise CrossoverV2Refused(
                    "the topology prescription was refused "
                    f"({exc.reason}): {exc.detail}"
                ) from exc
        # What this round actually runs at — one decision, made by the module that
        # owns the pin and taken identically by the grading stage.
        session_preset, session_fc_hz = apply_topology_pin(
            topology_prescription, preset=context.preset, fc_hz=context.fc_hz,
        )
        try:
            alignment_prescription = read_alignment_prescription(
                (raw or {}).get(ALIGNMENT_PRESCRIPTION_KEY),
                # THIS round's corner, never ``context.fc_hz`` — see the pin
                # above. ``None`` on a speaker with no crossover region, which
                # the reader refuses on by way count rather than here.
                fc_hz=session_fc_hz,
                # THIS round's preset — the pinned one when a pin ran.
                way_count=getattr(session_preset, "way_count", None),
                # The preset's own declared window, from its single owner. It is
                # the one bound here that does not rest on a number the request
                # supplied — and it already existed as the Fix-3 plausibility
                # screen, ten minutes downstream, wearing household copy that asks
                # the user to move the microphone. Asking it HERE is what stops a
                # prescribed candidate being blamed on a mic.
                declared_bounds_us=alignment_delay_search_bounds_us(context.preset),
            )
        except AlignmentPrescriptionRefused as exc:
            raise CrossoverV2Refused(
                f"the alignment prescription was refused ({exc.reason}): {exc.detail}"
            ) from exc

    # #2662 W2b: the microphone that answers this session's asks. After the
    # speaker-level gates (they are prior questions) and before any state is
    # opened, so a refused start (unplugged mic) costs no side effects (gate
    # S3, both stages).
    wired_device = _resolve_prepare_wired_mic()
    # Rebound ONCE, here, so every surface downstream — the index map, the
    # spec, the gate — reads one shape rather than a half-updated one.
    plan_shape = _hand_released_plan_shape(plan_shape)
    # Capture index -> the MEASURE spec a staged walk states there (R-1's
    # reverse polarity, and #3498's per-pose candidate). Bound here rather than
    # inside the branch below because ``_open`` reads it on both stages, and
    # stage 2 takes no MEASURE capture at all: empty is the engine leg's own
    # default spec at every MEASURE index, which is every ordinary session.
    engine_measure_specs: dict[int, Any] = {}
    # The per-role attenuation that spec's graph carries, resolved once at
    # adoption beside it. Empty on every session that stages no level-matched
    # walk, which is every ordinary one.
    engine_level_trims: dict[str, float] = {}
    # What each stop of a staged walk was measured UNDER, in stop order, for
    # the pose records the flow banks. Empty on every session that stages none.
    lateral_claims: tuple[Any, ...] = ()
    if not verify_only:
        include_cloud_measure = STAGE1_INCLUDES_CLOUD_MEASURE
        # R16's lateral walk (plan §4.4) is not a stage-1 group. Spelled here beside
        # the cloud flag, rather than passed as a literal below, because an
        # operator's staged angle walk flips it and the spec and the conductor's
        # index map must be built from ONE decision either way.
        include_lateral = False
        # #2291's entry baseline. Same reason, same place: the emitted plan and the
        # conductor's index→phase map are two surfaces that must agree about which
        # captures this session runs, and reading the flag twice is how they get to
        # disagree.
        include_entry_baseline = STAGE1_INCLUDES_ENTRY_BASELINE
        # ...and the map those three flags decide, built ONCE: the journal line
        # below and the conductor's walk read one plan, not two readings of it.
        stage1_index_phase = build_v2_cloud_index_phase_map(
            plan_shape=plan_shape,
            include_cloud_measure=include_cloud_measure,
            include_lateral=include_lateral,
            include_entry_baseline=include_entry_baseline,
        )
        # P2's staged angle walk (#2732). ONE take, feeding the map, the spec and
        # the conductor's two lateral kwargs. Before any state is opened, so a
        # refusal costs nothing. The map above is this walk's ``base_entries`` —
        # the captures that are NOT the walk — and is rebuilt below when one is
        # taken; that rebuild is still ONE decision, because the take is not redone.
        staged_walk = _take_staged_angle_walk(
            plan_shape,
            base_entries=len(stage1_index_phase),
            lateral_group_present=include_lateral,
            plans_cloud_group=include_cloud_measure,
            preset=context.preset,
            topology=context.topology,
        )
        lateral_prompts: tuple[Any, ...] | None = None
        lateral_consumer = LATERAL_CONSUMER_FC_SELECTOR
        if staged_walk is not None:
            (
                lateral_prompts,
                lateral_consumer,
                engine_measure_specs,
                engine_level_trims,
                lateral_claims,
            ) = staged_walk
            include_lateral = True
            stage1_index_phase = build_v2_cloud_index_phase_map(
                plan_shape=plan_shape,
                include_cloud_measure=include_cloud_measure,
                include_lateral=include_lateral,
                include_entry_baseline=include_entry_baseline,
                lateral_prompts=lateral_prompts,
            )
    evidence_store, _bundle_id = open_v2_evidence_store(context.topology)
    if verify_only:
        import numpy as np

        priors_raw = state.get("verify_priors") or {}
        sum_raw = priors_raw.get("predicted_sum") if isinstance(priors_raw, Mapping) else None
        predicted_sum = None
        if isinstance(sum_raw, Mapping) and sum_raw.get("freqs_hz"):
            predicted_sum = (
                np.asarray(sum_raw["freqs_hz"], dtype=float),
                np.asarray(sum_raw["magnitude_db"], dtype=float),
            )
        # Two-stage commission D4: the prediction's stored spec verdict, rehydrated
        # exactly like ``gate_ms`` below so this re-arm's own persist carries it
        # forward instead of blanking it. Absent (a state written before D4, or an
        # ungradeable prediction) stays None — unknown, never a pass.
        predicted_spec = (
            priors_raw.get("predicted_spec") if isinstance(priors_raw, Mapping) else None
        )
        predicted_spec = predicted_spec if isinstance(predicted_spec, Mapping) else None
        # The delta probe's commanded axis, rehydrated (#2291 Phase 3). This is the
        # stage the probe RUNS in, and stage 1 is the only stage that can produce
        # the curve, so without this every shipped stage 2 graded its correction
        # with the shortfall-vs-model-error discriminator switched off. ``None``
        # stays ``VERDICT_UNAVAILABLE`` — no evidence to refuse on, and no
        # permission granted either.
        commanded_delta = commanded_delta_prior_from_state(state)
        # The probe's STATE axis, rehydrated on exactly the same route and for the
        # same reason (#2614). ``None`` narrows the two directional safety rules to
        # the change axis alone, which the probe's own caller names on the journal
        # rather than letting a hearing-safety mask quietly shrink.
        declared_transfer = declared_transfer_prior_from_state(state)
        # What stage 1 proposed, rehydrated (#2392) — the identity the round
        # receipt names. Stage 2 plans nothing, so this is the only channel, and an
        # absent key (a state written before #2392, or a stage 1 whose proposal
        # assembly was refused) stays ``""``: the receipt then records the
        # candidate fingerprint under an explicit ``proposal_fingerprint_kind``
        # rather than claiming a proposal identity it never had.
        proposal_fingerprint = (
            str(priors_raw.get("proposal_fingerprint") or "")
            if isinstance(priors_raw, Mapping) else ""
        )
        # #2291's measured "before", rehydrated. Stage 2 never captures one — its
        # plan has no ``PHASE_ENTRY_BASELINE`` entry, by construction, because the
        # baseline has to precede the apply this stage is verifying — so this is
        # the only way the benefit verdict gets a side to compare against. ``None``
        # stays ``entry_baseline_unavailable``: INDETERMINATE, never a pass, and
        # declared to the capability journal below so the verdict's reason is not
        # the first place anyone learns it was missing.
        entry_baseline = entry_baseline_prior_from_state(state)
        # #2662, rehydrated on entry_baseline's route and for its reason: stage 2
        # never opened a session with a prescription (it takes no MEASURE capture),
        # so this durable record is the only way the round it grades can name what
        # its delay was derived from.
        alignment_prescription = alignment_prescription_prior_from_state(state)
        # The topology pin, on the line above's route and carrying more than a
        # receipt field: this stage GRADES the applied graph, and a pinned round
        # applied a crossover the saved declaration does not name. Re-opening at
        # the incumbent corner would hand VERIFY the wrong design target (R18's
        # absolute claim) and the wrong overlap band, so the round would be graded
        # for not being the crossover it deliberately replaced. Resolved before the
        # session below for the same one-decision reason stage 1 resolves it.
        topology_prescription = topology_prescription_prior_from_state(state)
        verify_preset, verify_fc_hz = apply_topology_pin(
            topology_prescription, preset=context.preset, fc_hz=context.fc_hz,
        )
        # A9, on the line above's route and for its reason, sharpened by one fact
        # that arm did not have to face: ``verify_priors`` is REBUILT from the
        # conductor on every persist, so this is not merely how stage 2 learns what
        # the round was prescribed — it is the only thing that stops stage 2's own
        # persist erasing it before the receipt is written.
        blend_prescription = blend_prescription_prior_from_state(state)
        blend_prescription_sha256 = blend_prescription_sha256_from_state(state)
        alignment_objective = str(
            (priors_raw.get("alignment_objective") if isinstance(priors_raw, Mapping)
             else "") or ""
        )
        gate_ms = (
            priors_raw.get("gate_window_ms") if isinstance(priors_raw, Mapping) else None
        )
        # Measurement-honesty gate G3's PREVIOUS reference — read as dated
        # history, never rehydrated as this session's comparator (#1927). This
        # re-arm always establishes its own baseline from its own first usable
        # VERIFY attempt; the only thing this record can do is make the session
        # SAY it reset the reference, and by how much. Absent (a state written
        # before this key, or a session that never reached a usable VERIFY pilot)
        # simply leaves the disclosure silent. Shape validation lives in
        # ``CrossoverV2Session.__init__`` so the "values plus a date, or
        # nothing" rule has one owner.
        pilot_transfer_prior = pilot_transfer_prior_from_state(state)
    else:
        prior_raw = load_v2_state()
        attempt_store = _attempt_loop_store_snapshot()
        prior_loop = (
            prior_raw.get("attempts_loop")
            if isinstance(prior_raw, Mapping) else None
        )
        prior_decision = (
            prior_loop.get("last_decision")
            if isinstance(prior_loop, Mapping) else None
        )
        prior_snapshot = (
            V2ConductorSnapshot(
                session_id=str(prior_raw.get("session_id") or ""),
                accepted_phases=tuple(prior_raw.get("accepted_phases") or ()),
                applied=bool(prior_raw.get("applied")),
                gain_plan_db=prior_raw.get("gain_plan_db"),
                attempt_history=attempt_history_from_state(prior_raw),
                last_attempt_decision=(
                    dict(prior_decision)
                    if isinstance(prior_decision, Mapping)
                    else None
                ),
            )
            if isinstance(prior_raw, Mapping)
            else None
        )

    acknowledgement_binding = secrets.token_urlsafe(24)
    stop_event = threading.Event()
    stop_lock = threading.Lock()
    complete_event = threading.Event()
    retake_event = threading.Event()
    # The position gate — built for any GATED shape, so a tap-paced session
    # (every ungated round) still carries no gate at all and every begin reaches
    # the conductor exactly as it always has. Two shapes are gated: the arm's
    # remote tier, whose driver releases each hold, and a hand-walked round on
    # the wired source, where a person does. ``plan_shape`` is ``None`` for the
    # verify-only recovery re-arm, which has no tier and therefore no gate — it
    # is the one-sweep session a household starts by hand, at the mark, with
    # nowhere to walk to.
    position_gate = (
        PositionGate()
        if plan_shape is not None and plan_shape.positions_gated
        else None
    )
    if position_gate is not None and plan_shape is not None:
        log_event(
            logger,
            "correction.crossover_v2_remote_session_open",
            stage=2 if verify_only else 1,
            tier=plan_shape.tier,
            # WHICH mover releases this session's holds. The event name is the
            # arm's and stays it — a driver greps it, and the doc names it —
            # so the second gated shape says so in a field instead of a second
            # event nothing is watching for. The shape's own field name, not a
            # second word for it.
            hand_released=plan_shape.hand_released_positions,
            # The captures this session will ACTUALLY take, off that plan. Stage
            # 1 logged ``plan_shape.measure_capture_target`` — the
            # cloud-INCLUSIVE shape target, 10 where the shipped stage 1 walks 3.
            # The reader is an external positioner with no screen to check it
            # against, and OVER-reporting is the direction that STALLS a walk: a
            # driver sized at 10 waits for seven captures that are never coming,
            # until the session ceiling expires (#2506). On 2026-08-19 a
            # wired-night driver met exactly that contradiction — its own
            # ``--complete-after 3`` against this line's 10 — and settled it by
            # re-deriving the count by hand on the live box.
            captures=(
                plan_shape.verify_capture_target
                if verify_only else len(stage1_index_phase)
            ),
        )

    held: _HeldSession | None = None

    def _open() -> Any:
        if verify_only:
            spec = build_v2_verify_session_spec(
                # The corner this round was measured and applied at — stage 1's
                # ``session_fc_hz`` rehydrated. Same one-corner-per-round rule
                # as the stage-1 spec below; ``None`` on a speaker with none.
                verify_fc_hz,
                measurement_band_hz=measurement_band_hz(context.roles_bands),
                acknowledgement_binding=acknowledgement_binding,
                plan_shape=plan_shape,
                default_setup_calibration=default_setup_calibration_for_v2(),
            )
        else:
            spec = build_v2_session_spec(
                context.roles_bands,
                # THIS round's corner, the same one the conductor below was opened
                # at. The entry-baseline program this plan announces is stage 2's
                # own anchor, and ``build_verify_program`` is fc-dependent TWICE:
                # the sweep's low bound is ``min(VERIFY_F_LO_HZ=150, fc/2)`` (live
                # below fc = 300) and the leading pilot's high bound is
                # ``min(VERIFY_PILOT_F_HI_HZ=800, fc/VERIFY_PILOT_FC_CLEARANCE_RATIO
                # =2.5)`` (live below fc = 2000). So a plan built at the incumbent
                # corner while the session solves at a pinned one announces a
                # DIFFERENT program at every corner under 2000 Hz — which is most
                # of a two-way's legal pin band, jts3's 1600-2500 included, not the
                # sub-300 Hz curiosity an earlier version of this comment claimed.
                session_fc_hz,
                acknowledgement_binding=acknowledgement_binding,
                plan_shape=plan_shape,
                include_cloud_measure=include_cloud_measure,
                include_lateral=include_lateral,
                include_entry_baseline=include_entry_baseline,
                lateral_prompts=lateral_prompts,
                default_setup_calibration=default_setup_calibration_for_v2(),
            )
        # This stage's own wall-clock budget, read ONCE off the plan it just
        # emitted: the runner's confirm-wait is bounded by it and the
        # walked-away volume ceiling is armed from it further down, and those
        # two must describe the same session (issue #2509).
        ceiling_s = session_wall_clock_ceiling_s(spec.capture_plan)
        rc = _mint_wired_session(wired_device, spec)
        # The conductor + publishers bind to the MINTED provider session id
        # (the seam's identity rule — the wired id rides the same key).
        capture_session_id = rc.pi_session.session_id
        # Re-arm the walked-away ceiling from THIS stage's own plan, BEFORE the
        # volume opens. The volume plan is process-global, so a preceding
        # session would otherwise leave its ceiling in force in either
        # direction: a cloud stage 1 legitimately outlasts the 3-entry flow's
        # window, and a 1-entry stage 2 cannot possibly need a cloud's. Scaled
        # from the plan this session actually emitted, never from the constants,
        # so a caller-configured cloud is covered too.
        session_volume_plan().set_wall_clock_ceiling_s(ceiling_s)
        publish_check, publish_candidate, refs = bind_evidence_publishers(
            evidence_store, capture_session_id, run_async
        )
        # Written by the play seam, read by analyze.
        capture_provenance = CaptureProvenanceRecorder()
        production_play = bind_production_play(
            run_async=run_async,
            camilla_factory=camilla_factory,
            evidence_store=evidence_store,
            capture_session_id=capture_session_id,
            topology=context.topology,
            preset=context.preset,
            role_channels=context.role_channels,
            playback_device=context.playback_device,
            safety_profile=context.safety_profile,
            role_targets=context.role_targets,
            session_volume_db=context.session_volume_db,
            # Stage 2 takes no MEASURE capture, so it declares no measurement
            # protection sections and rides the seam's own "none" default.
            protection_sections_by_role=(
                None if verify_only else protection_sections
            ),
            declared_sensitivities=context.declared_sensitivities,
            provenance=capture_provenance,
        )
        play = production_play.play
        session_graph = production_play.graph
        if verify_only:
            # This stage's journey (#2291 Phase 4), the same contract stage 1
            # opens and differing only in its arguments. ``available`` states
            # facts about the rehydration above and nothing more — which slug
            # each fact answers to is the journey's, so the journal's "missing"
            # line cannot drift from what the conductor was really constructed
            # with. No ``verify_capture_target``: this plan really does contain
            # VERIFY, so the phase-derived reading is already the right one.
            opening = open_stage(
                STAGE_VERIFY_CAPABILITIES,
                index_phase_map=build_v2_verify_index_phase_map(plan_shape=plan_shape),
                available=available_stage_priors(
                    commanded_delta=commanded_delta is not None,
                    predicted_sum=predicted_sum is not None,
                    entry_baseline=entry_baseline is not None,
                ),
            )
        else:
            # This stage's journey, resolved once (#2291 Phase 4). The index→phase
            # map is the one built above from the SAME resolved plan shape the
            # emitted spec used — not merely the same function at its own defaults
            # — so the prompt an entry carries, the phase the conductor runs for
            # that index, and the count the journal announced can never disagree.
            # ``verify_capture_target`` is the tier's own number, handed over as a
            # fact: the boost-permission rule that reads it (work
            # order D2's consequence — this session's phases carry no VERIFY,
            # because the post-apply sweep is stage 2) belongs to the journey.
            opening = open_stage(
                STAGE_MEASURE_CAPABILITIES,
                index_phase_map=stage1_index_phase,
                verify_capture_target=plan_shape.verify_capture_target,
            )
        # ONE binding for both stages. ``bind_v2_stage_seams`` is already the
        # single owner of "which callable implements each seam"; taking it at
        # one call site is what stops the two stages disagreeing about one —
        # which they had begun to, stage 2 reaching its check publisher through
        # a second, underscore-prefixed name for the same object.
        seams = bind_v2_stage_seams(
            opening,
            play=play,
            evidence_store=evidence_store,
            capture_session_id=capture_session_id,
            refs=refs,
            publish_check=publish_check,
            publish_candidate=publish_candidate,
            run_async=run_async,
            camilla_factory=camilla_factory,
            provenance=capture_provenance,
        )
        if verify_only:
            conductor = CrossoverV2Session(
                session_id=capture_session_id,
                # The topology the round being graded was MEASURED and APPLIED at
                # — the pin when the durable state carried one, else the context's
                # own. Resolved once, above.
                source_preset=verify_preset,
                # Stage 1's twin, and the ONLY statement of the fact this session
                # gets: this ctor is handed no ``tier`` at all, so before #2879 a
                # geometry-locked stage-2 group prompted for the 75 cm rung even on
                # the arm. The gate above and this now read one shape.
                positions_gated=bool(plan_shape and plan_shape.positions_gated),
                roles_bands=context.roles_bands,
                fc_hz=verify_fc_hz,
                driver_caps_dbfs=context.driver_caps_dbfs,
                driver_sweep_duration_limits_s=context.driver_sweep_duration_limits_s,
                session_volume_db=context.session_volume_db,
                seams=seams,
                driver_spacing_m=context.driver_spacing_m,
                driver_class_by_role=context.driver_class_by_role,
                radiating_diameter_mm_by_role=context.radiating_diameter_mm_by_role,
                tweeter_measurement_band_hz=context.measurement_band_hz_by_role.get("tweeter"),
                accepted_phases=(PHASE_CHECK, PHASE_MEASURE),
                applied=True,
                gain_plan_db=state.get("gain_plan_db"),
                index_phase_map=opening.plan.index_phase_map,
                measure_predicted_sum=predicted_sum,
                measure_predicted_spec_report=predicted_spec,
                measure_commanded_delta=commanded_delta,
                measure_declared_transfer=declared_transfer,
                measure_proposal_fingerprint=proposal_fingerprint,
                measure_entry_baseline=entry_baseline,
                alignment_prescription=alignment_prescription,
                topology_prescription=topology_prescription,
                blend_prescription=blend_prescription,
                blend_prescription_sha256=blend_prescription_sha256,
                measure_alignment_objective=alignment_objective,
                measure_gate_window_ms=(
                    float(gate_ms) if isinstance(gate_ms, (int, float)) else None
                ),
                verify_pilot_transfer_prior=pilot_transfer_prior,
                attempt_history=attempt_history_from_state(state),
                # #2602: where the next round sits in the flattening series, read
                # off the receipt the previous round banked and carried forward
                # across sessions — the series outlives the session that started
                # it. This stage reads it in ``_grade_round_once``, the stage that
                # grades a round. It is wired on BOTH stages since #2698:
                # #2687 gave stage 1 its own reader (``_blend_prescription``, at
                # candidate-build time), which falsified the argument this line
                # once carried for being the series' only wiring site.
                series_position=series_position_from_state(state),
                attempt_floor=attempt_store.floor,
                last_attempt_decision=(
                    dict(prior_attempt_decision)
                    if isinstance(prior_attempt_decision, Mapping) else None
                ),
                speaker_id=context.topology.topology_id,
                tuning_attempt_id=tuning_attempt_id,
            )
        else:
            # A9. Resolved ONCE and used twice, because the two uses have to agree:
            # the ordinal that decides which round this is is the ordinal a staged
            # prescription must have been staged for, and re-reading it would let a
            # state write between the two hand this round an instruction written for
            # another one.
            series_position = series_position_from_state(prior_raw)
            # ONE take, already split by class — see the take's own contract for why
            # the split is made there and not here.
            staged_blend, staged_driver = _take_staged_prescription(
                series_position.ordinal
            )
            conductor = CrossoverV2Session.hydrate(
                prior_snapshot,
                session_id=capture_session_id,
                # The PINNED topology when this request carried one, else the
                # context's own — resolved once, above, so the preset and the
                # corner can never name two different crossovers.
                source_preset=session_preset,
                roles_bands=context.roles_bands,
                fc_hz=session_fc_hz,
                driver_caps_dbfs=context.driver_caps_dbfs,
                driver_sweep_duration_limits_s=context.driver_sweep_duration_limits_s,
                session_volume_db=context.session_volume_db,
                seams=seams,
                tier=plan_shape.tier,
                # The gate's own fact, handed down rather than re-derived from the
                # tier: a geometry-locked retake asks for a pose no HELD walk can
                # state, so the group refuses it for either gated shape.
                positions_gated=plan_shape.positions_gated,
                index_phase_map=opening.plan.index_phase_map,
                post_apply_verifies=opening.plan.post_apply_verifies,
                driver_spacing_m=context.driver_spacing_m,
                driver_class_by_role=context.driver_class_by_role,
                radiating_diameter_mm_by_role=context.radiating_diameter_mm_by_role,
                # #2732 P2. From the SAME take the map and the spec above read.
                lateral_consumer=lateral_consumer,
                lateral_prompts=lateral_prompts,
                lateral_claims=lateral_claims,
                measurement_protection_sections_by_role=protection_sections,
                sound_design_revision=context.sound_design_revision,
                tweeter_measurement_band_hz=context.measurement_band_hz_by_role.get("tweeter"),
                attempt_floor=attempt_store.floor,
                speaker_id=context.topology.topology_id,
                alignment_prescription=alignment_prescription,
                # The pin itself, held so the session knows it IS pinned: it closes
                # the Fc search and suppresses the selector, and it banks the
                # provenance on the round's receipt. The topology it names has
                # already taken effect in the two arguments above.
                topology_prescription=topology_prescription,
                # #2698. The same series fact the grading stage reads, from the
                # same reader and off the same durable state this snapshot is
                # built from — because stage 1 reads it too, and reads it FIRST.
                # ``_blend_prescription`` runs at candidate-build time, here in
                # MEASURE, and an absent position there is not a no-op: it falls
                # back to the incumbent, so a measuring session with no position
                # silently discards every blend instruction the previous round
                # banked and the series can never converge.
                series_position=series_position,
                # A9, and it rides the same stage for the same reason: the door it
                # enters is ``_blend_prescription``, which runs at candidate-build
                # time, so a prescription handed to any other stage would be held by
                # a session that never builds a candidate.
                blend_prescription=(
                    None if staged_blend is None else staged_blend.prescription
                ),
                blend_prescription_sha256=(
                    "" if staged_blend is None else staged_blend.prescription_sha256
                ),
                # PR-B, on this stage for the line above's reason with the same
                # deadline and a different door: the per-driver class is merged onto
                # the Layer-1a fit inside the candidate build, which is MEASURE's.
                driver_prescription=(
                    None if staged_driver is None else staged_driver.prescription
                ),
            )
        # Stage 2 keeps the durable candidate/applied facts and rebinds the
        # session id; stage 1 writes its fresh one.
        persist_conductor_state(conductor, failure_code=None, evidence=refs)
        # The engine's session, constructed HERE and held for the run's
        # lifetime. Its graph slot is the one this stage just bound, so the
        # measurement graph is a field on an object that dies with the run.
        from jasper.active_speaker.crossover_v2.session import TuningSession

        # ONE claim for this session, minted HERE and injected into both the
        # things that hold it: the engine's volume seam, and the plan's door.
        # Made at the composition root rather than reached for afterwards —
        # ``EngineSeams`` is engine-internal, and a front end that read
        # ``tuning.seams.volume`` back out would be doing engine work outside
        # the engine (``session_seams.EngineSeams``, wave 2's enforcement pin).
        volume_claim = _session_volume_claim()
        # The half that records what a stimulus does to the room — minted ONCE,
        # because two parties hold the same object: the play transaction (which
        # records across the play and banks the path) and the walk (which
        # drains the minted answer so `consume_capture` grades the very take
        # the engine banked).
        stimulus_capture = _wired_stimulus_capture(wired_device, evidence_store)
        tuning = TuningSession(
            session_id=capture_session_id,
            seams=bind_v2_engine_seams(
                session_graph=session_graph,
                evidence_store=evidence_store,
                capture_session_id=capture_session_id,
                # The session's own program door, so a stimulus is picked BY
                # IDENTITY from what this conductor composed rather than
                # recomposed at a guessed level.
                compose_stimulus=functools.partial(
                    production_play.compose,
                    program_for_phase=conductor.program_for_phase,
                ),
                capture_stimulus=stimulus_capture,
                volume_claim=volume_claim,
                # Stage 2 is verify-class on every tier — it grades through the
                # APPLIED graph and takes no routed capture — so it must not
                # swap the measurement graph in and straight back out.
                routed_phases=not verify_only,
            ),
            measurement_level_db=context.session_volume_db,
            # Resolved at adoption, applied here: the session installs the
            # trims a ``level_matched`` spec asks for and derives none of its
            # own. Empty on every session whose walk asked for no level match.
            level_match_trims_db=engine_level_trims,
        )
        nonlocal held
        source_run = _build_wired_run(
            conductor,
            volume=_volume_hooks(
                camilla_factory, context, tuning=tuning,
                volume_claim=volume_claim,
            ),
            stop_event=stop_event,
            stop_lock=stop_lock,
            position_gate=position_gate,
            evidence_refs=refs,
            wired_device=wired_device,
            ceiling_s=ceiling_s,
            complete_event=complete_event,
            retake_event=retake_event,
            capture_stimulus=_bind_engine_measure_leg(
                tuning=tuning,
                stimulus_capture=stimulus_capture,
                index_phase_map=opening.plan.index_phase_map,
                run_async=run_async,
                specs_by_index=engine_measure_specs,
            ),
        )
        held = _HeldSession(tuning=tuning, run=source_run)
        return rc

    async def _run(pi_session: Any) -> None:
        """Drive the walk the preparer built.

        The session's own lifetime is NOT driven here. Both halves live in the
        volume hooks: ``open`` after the plan has written its durable intent
        (the claim is the first fader mutation, and the intent must be on disk
        before it — that ordering is what a crash in the gap survives), and
        ``close`` where the graph goes back, before the plan's fader drain.
        The runner calls those hooks, so the session opens and closes inside
        the same span this coroutine owns — without this body being able to
        reorder either.
        """
        if held is None:
            raise RuntimeError(
                "the v2 measurement session was run before it was opened"
            )
        await held.run(pi_session)

    def _request_stop() -> None:
        with stop_lock:
            stop_event.set()

    return V2PreparedSession(
        label=V2_CAPTURE_KIND_VERIFY if verify_only else V2_CAPTURE_KIND_SESSION,
        open=_open,
        run_and_consume=_run,
        request_stop=_request_stop,
        position_gate=position_gate,
        request_complete=complete_event.set,
        request_retake=(
            # Carried only by a walk that can ever WAIT for a person, which is
            # the same question the gate answers: the retake is served at a
            # HELD BEGIN or in the held-set window, and a gateless walk reaches
            # neither — the recovery re-arm is one sweep at the mark with no
            # group to hold open. Carrying it there would answer a household's
            # POST with ok:true and then do nothing at all, which is the one
            # shape a signal seam must never have.
            retake_event.set if position_gate is not None else None
        ),
    )


# --------------------------------------------------------------------------- #
# apply (the existing baseline transaction, W4 seam)
# --------------------------------------------------------------------------- #


def _assert_stage_2_can_open(status: Mapping[str, Any]) -> None:
    """Refuse an apply this speaker could not then verify (D3, PR-T3's half).

    The pre-POST half of the stage-2 openability preflight. PR-T2 shipped the
    render-time half — the review screen disables Apply and shows the refusal's
    own sentence — and deliberately left this one to T3, because until T3
    removed auto-apply ``handle_v2_apply`` was ALSO the automatic path and a
    refusal here would have newly refused a shipped automatic flow.

    It closes the hole work-order premise 5 was hiding: a verify-only prepare
    reaches ``resolve_conductor_context(status)``, which is fail-closed and
    carries seven refusal sites of its own plus
    ``ensure_crossover_preview_ready()``'s. So a box can be applied and still
    be unable to open stage 2 — the household applies, stage 2 refuses at open,
    and the speaker sits corrected with no verdict. That applied-and-ungraded
    end state is the one this whole work order exists to eliminate.

    **The SAME predicate the render-time half runs and stage 2 itself will
    run**, not a cheaper lookalike free to disagree with either. A disabled
    control is not a security boundary — a stale page, a second tab, or a
    direct POST all reach this endpoint — so the screen's honesty layer and
    this refusal are two halves of one guarantee, and neither is redundant.

    Fail-closed in both directions: an unexpected exception refuses too,
    because "we could not check" and "we checked and it is fine" must never
    produce the same outcome on the one action that touches the speaker.
    """
    try:
        resolve_conductor_context(status)
    except CrossoverV2Refused as exc:
        log_event(
            logger,
            "correction.crossover_v2_apply_stage2_preflight_refused",
            level=logging.WARNING,
            reason=str(exc),
        )
        raise CrossoverV2Refused(
            f"{exc} Applying now would leave this speaker corrected but "
            "unchecked, so the apply was not run."
        ) from exc
    except Exception as exc:  # noqa: BLE001 — fail closed, see the docstring
        log_event(
            logger,
            "correction.crossover_v2_apply_stage2_preflight_failed",
            level=logging.ERROR,
            error_type=type(exc).__name__,
        )
        raise CrossoverV2Refused(
            "the speaker could not confirm it is ready to check this "
            "correction afterwards, so the apply was not run"
        ) from exc


def handle_v2_apply(
    raw: Mapping[str, Any],
    run_async: Any,
    camilla_factory: Any,
    *,
    status: Mapping[str, Any],
) -> dict[str, Any]:
    """POST /crossover/v2/apply — apply the reviewed measured candidate.

    **This is the ONLY path that applies a measured crossover, and it runs only
    on an explicit household POST** (two-stage commission work order D1). Until
    PR-T3 the capture runner also called it, automatically, off the pre-apply
    cloud's close.

    Reopens the published candidate artifact through
    ``MeasuredCrossoverCandidate.from_mapping`` (the tamper check), gates on
    the reviewed ``expected_candidate_fingerprint``, runs the stage-2
    openability preflight (:func:`_assert_stage_2_can_open` — ``status`` is
    keyword-only and REQUIRED so no caller can quietly skip it), and rides the
    EXISTING atomic apply-with-rollback transaction —
    ``apply_baseline_profile(measured_candidate=...)`` (the W4 seam) — then
    marks the durable v2 state applied.

    W6 run-6 Blocker M: the seam's own freshness guard
    (``apply_baseline_profile``'s ``expected_candidate_fingerprint``) compares
    against ``baseline_candidate_fingerprint`` — the COMPOSED baseline
    candidate's own identity — never the MEASURED candidate's fingerprint
    this endpoint reviews with the household. Forwarding the measured
    fingerprint straight through made every apply refuse
    ``baseline_candidate_fingerprint_mismatch``, unconditionally. This host
    translates between the two vocabularies: it composes the baseline
    candidate read-only first (``build_baseline_profile_candidate(...,
    write=False)`` — the exact builder the seam itself uses), asserts that
    composition is still bound to the reviewed MEASURED candidate (preserving
    the review-freshness guarantee at the measured-candidate level), then
    passes the COMPOSED candidate's own ``candidate_fingerprint`` through to
    the seam.
    """
    from jasper.active_speaker.baseline_profile import (
        applied_program_level_delta_db,
        apply_baseline_profile,
        build_baseline_profile_candidate,
    )
    from jasper.active_speaker.crossover_declaration import (
        CrossoverBelowDeclaredFloor,
        assert_crossover_honours_declared_floor,
        change_from_record,
        change_to_record,
        declaration_change_for_candidate,
    )
    from jasper.active_speaker.crossover_preview import load_crossover_preview
    from jasper.active_speaker.design_draft import load_design_draft
    from jasper.active_speaker.measured_crossover_candidate import (
        MeasuredCrossoverCandidate,
        MeasuredCrossoverCandidateError,
    )
    from jasper.active_speaker.measurement import load_measurement_state
    from jasper.output_topology import load_output_topology
    from jasper.web.sound_setup import apply_measured_crossover_geometry

    expected = str(raw.get("expected_candidate_fingerprint") or "")
    if not expected:
        raise CrossoverV2Refused("expected_candidate_fingerprint is required")
    state = load_v2_state()
    review_session_id = str((state or {}).get("session_id") or "")
    candidate_ref = (state or {}).get("candidate")
    evidence = (state or {}).get("evidence") or {}
    if not isinstance(candidate_ref, Mapping) or not candidate_ref.get("fingerprint"):
        raise CrossoverV2Refused(
            "no measured crossover candidate is ready to apply; measure first"
        )
    if str(candidate_ref["fingerprint"]) != expected:
        raise CrossoverV2Refused(
            "the reviewed crossover is no longer current; review the newest "
            "measurement before applying"
        )
    candidate_payload = raw.get("candidate")
    if not isinstance(candidate_payload, Mapping):
        # Endpoint contract: the wizard posts only the fingerprint; the host
        # reopens the artifact from the evidence bundle recorded at publish.
        candidate_payload = _reopen_candidate_artifact(state, evidence)
    try:
        candidate = MeasuredCrossoverCandidate.from_mapping(candidate_payload)
    except MeasuredCrossoverCandidateError as exc:
        raise CrossoverV2Refused(str(exc)) from exc
    if candidate.fingerprint != expected:
        raise CrossoverV2Refused(
            "the persisted candidate does not match the reviewed fingerprint"
        )
    topology = load_output_topology()
    # WHAT THIS APPLY ASKS ``/sound`` TO DECLARE — derived from the candidate
    # that is about to be applied, never from a persisted record that merely
    # claims something about it. ``crossover_declaration`` carries the full
    # argument; the short version is that the candidate is the artifact the
    # graph is emitted from, so keying the declaration write on it is what makes
    # the two agree by construction rather than by cross-check. ``None`` is the
    # ordinary answer — the candidate was measured at exactly the crossover
    # Sound declares — and it keeps this path byte-identical to what it did
    # before this seam existed.
    pre_draft = load_design_draft(topology=topology)
    accepted_revision = (state or {}).get("accepted_sound_revision")
    saved_already = (
        isinstance(accepted_revision, int) and not isinstance(accepted_revision, bool)
    )
    # On a retry the declaration ALREADY carries the candidate's crossover, so
    # the live draft can no longer say what that accept displaced: the record
    # written beside ``accepted_sound_revision`` is the only surviving inverse.
    change = (
        change_from_record((state or {}).get("accepted_sound_declaration_change"))
        if saved_already
        else declaration_change_for_candidate(
            source_preset=candidate.source_preset, design_draft=pre_draft)
    )
    if accepted_revision is not None and (not saved_already or change is None):
        raise CrossoverV2Refused(
            "the saved Sound revision is invalid; review a fresh measurement")
    # Names the slope only when the slope is what moved (``_crossover_label``):
    # every refusal below tells the household what is now sitting in Sound, and
    # on a slope-only accept a sentence naming only the frequency would name the
    # one number that did NOT change.
    selected_label = (
        _crossover_label(change.selected, change.changes_slope) if change else "")
    selected_fc_hz = change.selected.fc_hz if change else None
    # Same predicate this path has always branched on — "did this apply write
    # the Sound declaration" — now read off the change itself rather than off a
    # persisted recommendation. Every downstream use is unchanged.
    alternative = change is not None

    def _saved_not_applied(exc: BaseException) -> CrossoverV2Refused:
        log_event(logger, "correction.crossover_v2_sound_saved_not_applied",
                  level=logging.ERROR, selected_fc_hz=selected_fc_hz,
                  error_type=type(exc).__name__)
        return CrossoverV2Refused(
            f"{selected_label} is saved in Sound but was not "
            "applied to the speaker; retry this same action")

    def _before_dsp(call: Callable[[], Any]) -> Any:
        try:
            return call()
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            if change is not None:
                raise _saved_not_applied(exc) from exc
            raise

    def _review_replaced() -> CrossoverV2Refused:
        return CrossoverV2Refused(
            f"{selected_label} is saved in Sound, but this review was "
            "replaced before DSP apply; open the fresh Review")

    if change is not None:
        # HEARING-SAFETY BOUNDARY, and it runs BEFORE the durable declaration
        # write on purpose. The L0 emit gate refuses this same condition, but it
        # can only refuse once the declaration already carries the crossover
        # (``baseline_profile``'s staleness guard requires that ordering) — so an
        # emit-time refusal alone would leave ``/sound`` declaring a corner the
        # speaker is not playing and cannot be made to play. Refusing here means
        # a refused apply displaces nothing at all. See
        # ``crossover_declaration.assert_crossover_honours_declared_floor``.
        #
        # Scoped to THIS arm on purpose, and the resulting asymmetry is
        # disclosed rather than closed. An as-declared apply (``change is
        # None``) on a speaker whose declaration is ALREADY below the floor has
        # no write to run ahead of; it falls through to the L0 emit gate, whose
        # ``ActiveSpeakerConfigError`` the compose below re-raises RAW. Both
        # refuse — but only one names itself. Converting that re-raise would
        # make this function the owner of how EVERY L0 gate reads here (the
        # unprotected-tweeter gate included), which is #2736's residual to
        # widen with tests per gate, not this path's to take in passing. The
        # two are also different situations: this arm refuses a change the
        # household can still decline, that one describes a graph the speaker
        # is already playing, whose remedy is the fleet check rather than
        # "do not do this apply".
        try:
            assert_crossover_honours_declared_floor(candidate.source_preset)
        except CrossoverBelowDeclaredFloor as exc:
            log_event(logger, "correction.crossover_v2_apply_refused",
                      level=logging.ERROR, reason=exc.reason,
                      selected_fc_hz=selected_fc_hz)
            raise CrossoverV2Refused(str(exc)) from exc
        if not saved_already:
            # Ordered BEFORE the durable declaration write for the reason the
            # hearing-safety gate above orders itself the same way: a refused
            # apply must displace nothing, or ``/sound`` declares a crossover
            # the speaker is not playing and every retry re-refuses. The D3
            # assert below stays at commit position (freshness); a retry
            # (Sound already saved) skips this arm and keeps its "saved in
            # Sound but was not applied" framing there. Raw on purpose:
            # ``_before_dsp`` would relabel a pre-write refusal as
            # saved-not-applied, which is false on this side of the write.
            _assert_stage_2_can_open(status)
            measured_revision = (state or {}).get("sound_design_revision")
            if (isinstance(measured_revision, bool)
                    or not isinstance(measured_revision, int)):
                raise CrossoverV2Refused(
                    "the Sound revision measured for this review is missing; "
                    "review a fresh measurement")
            try:
                saved = apply_measured_crossover_geometry(
                    expected_revision=measured_revision,
                    between_roles=change.between_roles,
                    configured=change.configured,
                    selected=change.selected,
                )
            except ValueError as exc:
                raise CrossoverV2Refused(
                    "Sound changed since this review; review a fresh measurement") from exc
            accepted_revision = saved.get("revision")
            if (
                isinstance(accepted_revision, bool)
                or not isinstance(accepted_revision, int)
                or not _update_current_review(
                    review_session_id, expected, None,
                    # The change lands in the SAME state write as the revision
                    # that save produced, for the reason the retry read above
                    # gives: once the declaration carries the candidate's
                    # crossover, what it displaced is no longer derivable
                    # from anything live, and a retry still needs that inverse
                    # to rebuild its change.
                    {"accepted_sound_revision": accepted_revision,
                     "accepted_sound_declaration_change": change_to_record(change)},
                )
            ):
                raise _review_replaced()
        # durable=True: this branch just accepted a measured crossover onto the
        # Sound declaration (apply_measured_crossover_geometry above), so the
        # preview regenerated from it is part of the same crossover-accept
        # seam and gets the same power-loss-durable write.
        preview = _before_dsp(lambda: ensure_crossover_preview_ready(durable=True))
        draft = _before_dsp(lambda: load_design_draft(topology=topology))
    else:
        draft = pre_draft
        preview = load_crossover_preview(current_design_draft=draft)

    # Blocker M translation: compose read-only (the seam's own build_candidate
    # closure re-derives this identically under its writer lock, so this is a
    # deterministic recompose, not a second opinion) and confirm the
    # composition is still bound to the reviewed measured candidate before
    # asking the seam to apply anything.
    try:
        measurements = load_measurement_state(topology)
        reviewed_baseline = build_baseline_profile_candidate(
            topology,
            design_draft=draft,
            crossover_preview=preview,
            measurements=measurements,
            write=False,
            tuning_owner="automatic",
            measured_candidate=candidate,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        if alternative:
            raise _saved_not_applied(exc) from exc
        raise
    reviewed_measured_fingerprint = str(
        (reviewed_baseline.get("source") or {}).get("measured_candidate_fingerprint")
        or ""
    )
    if reviewed_measured_fingerprint != expected:
        raise CrossoverV2Refused(
            "the reviewed crossover is no longer current; review the newest "
            "measurement before applying"
        )
    baseline_expected_fingerprint = str(
        reviewed_baseline.get("candidate_fingerprint") or ""
    )
    # The pre-candidate applied profile, if any (``None`` on the speaker's
    # first-ever apply). ``build_baseline_profile_candidate`` freezes it here
    # as ``applied_recomposition_profile`` before the actual apply below
    # commits and pops that field off the new applied SSOT — this is the ONLY
    # moment it survives to read the way back's pointer and the #1811 offset
    # from.
    pre_apply_profile = reviewed_baseline.get("applied_recomposition_profile")
    if not isinstance(pre_apply_profile, Mapping):
        pre_apply_profile = None

    # LAST, immediately before the transaction commits (D3). After the
    # freshness gates, so a stale candidate still gets its own specific
    # refusal rather than this one.
    _before_dsp(lambda: _assert_stage_2_can_open(status))

    if alternative and not _update_current_review(
        review_session_id, expected, accepted_revision, {},
    ):
        raise _review_replaced()
    if alternative:
        draft = _before_dsp(lambda: load_design_draft(topology=topology))
        if draft.get("revision") != accepted_revision:
            raise CrossoverV2Refused(
                "Sound changed after this crossover was saved; review a fresh "
                "measurement before applying")

    review_identity = (
        (review_session_id, expected, accepted_revision) if alternative else None
    )

    def _unknown_result(error_type: str) -> CrossoverV2Refused:
        message = (
            f"{selected_label} is saved in Sound, but JTS could not "
            "confirm whether DSP apply finished; review the current speaker "
            "state before retrying")
        _persist_apply_blocked({"id": "apply_result_unknown", "message": message},
                               review_identity)
        log_event(logger, "correction.crossover_v2_apply_result_unknown",
                  level=logging.ERROR, selected_fc_hz=selected_fc_hz,
                  error_type=error_type)
        return CrossoverV2Refused(message)

    cam = _before_dsp(camilla_factory)
    try:
        payload = run_async(apply_baseline_profile(
            topology,
            design_draft=draft,
            crossover_preview=preview,
            measurements=measurements,
            load_config=lambda path: cam.set_config_file_path(
                path, best_effort=False
            ),
            get_current_config_path=lambda: cam.get_config_file_path(
                best_effort=False
            ),
            tuning_owner="automatic",
            expected_candidate_fingerprint=baseline_expected_fingerprint,
            measured_candidate=candidate,
        ))
    except Exception as exc:  # noqa: BLE001 - DSP result may be ambiguous
        if alternative:
            raise _unknown_result(type(exc).__name__) from exc
        raise
    if payload.get("status") == "applied":
        # #1811 — the apply boundary. The graph that just went live absorbs its
        # correction's boost as a pre-split common attenuation, so the same
        # commanded volume now drives the speaker measurably quieter (−7.9 dB
        # broadband on the session that surfaced this). That attenuation is the
        # excitation-safety property — it is what keeps a boosted band at or
        # under unity — so it is NOT compensated at the main volume. What it
        # needs is to be DECLARED, because the post-apply analysis compares the
        # capture against a prediction that carries no such term.
        #
        # Handed to ``observe_apply_success``, which persists it in the SAME
        # state write as the ``applied`` flag. That is the ordering guarantee
        # this needs: ``applied`` is what releases VERIFY's deferred hold, so
        # the flag can never become visible without the offset beside it, and
        # the conductor's probe seam reads a complete record the moment VERIFY
        # lands one capture later.
        offset_db = applied_program_level_delta_db(
            pre_apply_profile, payload.get("profile"),
        )
        payload["expected_post_apply_offset_db"] = round(offset_db, 3)
        with _state_lock:
            if _update_current_review(
                review_session_id, expected,
                accepted_revision if alternative else None, {},
                allow_applied=True,
            ):
                observe_apply_success(
                    expected,
                    # The identity of the measured candidate this apply
                    # DISPLACED — the way back's pointer, read off the frozen
                    # profile's own source record.
                    previous_candidate_fingerprint=str(
                        ((pre_apply_profile or {}).get("source") or {}).get(
                            "measured_candidate_fingerprint"
                        )
                        or ""
                    )
                    or None,
                    expected_post_apply_offset_db=offset_db,
                )
    issue = None
    if payload.get("status") in {"blocked", "apply_failed"}:
        # Finding N: name the blocker compactly (not buried in the full
        # composed profile) and persist it so the failure screen can surface
        # it instead of the household seeing a generic message with no
        # specific cause. This endpoint is the flow's ONE apply path since
        # PR-T3 removed auto-apply, so it is also the only writer of this
        # blocking issue — the SSOT claim survives its original reason.
        issue = _blocking_apply_issue(payload)
        if (alternative and payload.get("status") == "apply_failed"
                and not _dsp_apply_is_known_inactive(payload)):
            raise _unknown_result("returned_apply_failed")
        if alternative:
            payload["error"] = (
                f"{selected_label} is saved in Sound but was not "
                "applied to the speaker; retry this same action"
            )
            issue = {"id": str((issue or {}).get("id") or "apply_blocked"),
                     "message": payload["error"]}
        if issue is not None:
            payload["issue"] = issue
        _persist_apply_blocked(issue, review_identity)
        log_event(logger, "correction.crossover_v2_apply_blocked",
                  level=logging.WARNING, issue_id=(issue or {}).get("id", ""))
        if alternative and payload.get("status") == "apply_failed":
            raise CrossoverV2Refused(str(payload.get("error") or "DSP apply failed"))
    log_event(
        logger,
        "correction.crossover_v2_apply",
        status=payload.get("status"),
        candidate_fingerprint=expected,
    )
    return payload


def bind_delta_probe_rollback(run_async: Any, camilla_factory: Any) -> Any:
    """The conductor's ``rollback`` seam (linearization-integrity PR-L5).

    Puts the previous tuning back THROUGH THE NORMAL PATH — republish the
    prior candidate by fingerprint (:func:`handle_v2_republish`, the same
    handler POST /crossover/v2/republish runs), then apply it
    (:func:`handle_v2_apply`, with every admission gate it always runs) — and
    returns True when that candidate is live again. Not a second restore
    mechanism that could drift from the operator's own way back: the target is
    resolved from the SAME field the wizard's "Go back to the previous tuning"
    action reads (``previous_candidate_fingerprint``),
    and the two doors are the two the operator would press. The only
    difference is who pressed them: here the round's adoption table did,
    because it measured that the applied correction made the speaker worse.

    Catches :class:`CrossoverV2Refused`, which is the ordinary outcome for an
    automatic caller — a first-ever apply records no prior candidate, a pruned
    bank cannot republish one, and the apply door carries its own refusals —
    and reports "not restored" so the conductor's refusal still reaches the
    household. A rollback that could not run must not swallow the verdict that
    asked for it.

    It does NOT claim to catch everything: an OSError from the CamillaDSP
    socket still propagates, and
    :func:`~jasper.active_speaker.crossover_v2.coordinator._run_round_restore`
    catches that wider family on the other side of the seam (it has to — a
    conductor with a different binding gets the same protection). Two honest
    halves rather than one dishonest "never raises".

    **Three action-time gates run before the doors**, each the ACT half of
    an answer the adoption table already gave: the pointer must exist, it
    must be PAIRED to the apply this round displaced
    (:func:`_previous_candidate_paired` — the stale-stash class, #2559, and
    the automatic revert's own window both read as unpaired), and the running
    graph must still be the one this round applied
    (``applied_profile_displacement`` — the 2026-08-15 out-of-band class). A
    successful revert then consumes its own pairing
    (:func:`_consume_auto_revert_pairing`), which is what keeps the graph a
    round measured WORSE from being automatically re-applied in the
    [revert…next-apply] window.

    **Exactly once per binding, and the stakes went UP with the normal path
    (#2291).** ONE conductor site reaches this closure — the round's adoption
    path — and the guard below is what its history bought (three callers once
    raced it into a false "still applied" sentence). The restore is attempted
    once, and every later caller is handed the FIRST call's outcome verbatim.

    **Kept as a property of THIS closure** rather than of any caller's
    discipline — which is exactly the assumption that broke last time. The
    one-owner rule is pinned separately, at the flow
    (``test_two_restore_triggers_run_one_undo_and_keep_the_honest_sentence``
    asserts no second ``self._seams.rollback(`` call site survives).
    """
    lock = threading.Lock()
    outcome: dict[str, bool] = {}

    def _rollback(reason: str) -> bool:
        with lock:
            if "restored" in outcome:
                remembered = outcome["restored"]
                log_event(
                    logger,
                    "correction.crossover_v2_delta_probe_restore_repeat",
                    level=logging.INFO,
                    reason=reason, restored=remembered,
                )
                return remembered
            restored = _restore_once(reason)
            outcome["restored"] = restored
            return restored

    def _refused_before_the_doors(reason: str, detail: str) -> bool:
        # The answer ``_previous_candidate_known`` gave the adoption table,
        # re-read at the moment of action: reaching one of these arms means
        # the state (or the speaker) moved between the decision and the act,
        # and refusing is the honest end — the round re-grades into
        # recovery_required.
        log_event(
            logger, "correction.crossover_v2_delta_probe_restore_refused",
            level=logging.WARNING, reason=reason, detail=detail,
        )
        return False

    def _restore_once(reason: str) -> bool:
        from jasper.active_speaker.baseline_profile import (
            APPLIED_PROFILE_DISPLACED,
            applied_profile_displacement,
            load_applied_baseline_profile_state,
        )
        from jasper.web import correction_crossover_backend
        from jasper.web import correction_crossover_v2_republish as republish_door
        from jasper.web import correction_crossover_v2_status as v2_status

        state = load_v2_state()
        fingerprint = v2_status._previous_candidate_fingerprint(state)
        if fingerprint is None:
            return _refused_before_the_doors(
                reason, "no previous candidate fingerprint is recorded",
            )
        # The pairing rule's one equality, re-checked where the action is: the
        # pointer must have been recorded by the apply this round displaced.
        # An unpaired pointer is every stale-stash shape (#2559) plus the
        # automatic revert's own [revert…next-apply] window — the AUTO path
        # refuses; the household's way-back button is deliberately not gated
        # on this and still offers the pointer through review + Apply.
        if not _previous_candidate_paired(state):
            return _refused_before_the_doors(
                reason,
                "the recorded previous candidate is not paired to the apply "
                "this round displaced",
            )
        # The live half (#2537's out-of-band class): is the speaker still
        # playing the graph this round's apply installed? A reconcile moved
        # the running config on 2026-08-15 and the old restore faithfully
        # replaced an operator's deliberate graph; the detector already
        # existed — this path just never asked. Only a POSITIVE displacement
        # refuses: the other codes mean the comparison could not be made, and
        # an absent measurement is not evidence of a defect.
        applied_record = load_applied_baseline_profile_state()
        if applied_record is not None and (
            applied_profile_displacement(applied_record)
            == APPLIED_PROFILE_DISPLACED
        ):
            log_event(
                logger,
                "correction.crossover_v2_applied_profile_displaced",
                level=logging.WARNING,
                surface="auto_revert",
            )
            return _refused_before_the_doors(
                reason,
                "the running graph is not the one this round applied",
            )
        try:
            republish_door.handle_v2_republish({"fingerprint": fingerprint})
            payload = handle_v2_apply(
                {"expected_candidate_fingerprint": fingerprint},
                run_async,
                camilla_factory,
                # Read fresh at the moment of action, exactly as the manual
                # dispatch does: the stage-2 openability preflight must see
                # the speaker as it is NOW, not as it was at session prepare.
                status=correction_crossover_backend.status_payload(),
            )
        except CrossoverV2Refused as exc:
            log_event(
                logger, "correction.crossover_v2_delta_probe_restore_refused",
                level=logging.WARNING, reason=reason, detail=str(exc),
                candidate_fingerprint=fingerprint,
            )
            return False
        restored = payload.get("status") == "applied"
        if restored:
            _consume_auto_revert_pairing()
        # This caller has NO screen — it reduces the payload to a bool for the
        # conductor — so the blocked apply's cause exists nowhere else at all
        # (#2519's reasoning, carried to the new mechanism).
        log_event(
            logger, "correction.crossover_v2_delta_probe_restore",
            level=logging.WARNING if not restored else logging.INFO,
            reason=reason, status=payload.get("status"),
            candidate_fingerprint=fingerprint,
            code=str((_blocking_apply_issue(payload) or {}).get("id") or ""),
        )
        return restored

    return _rollback


def _consume_auto_revert_pairing() -> None:
    """Mark the way-back pointer as recorded by the automatic revert.

    The revert's own apply just re-stamped the pointer at the candidate it
    displaced — which is the graph a round measured WORSE. Left paired, the
    very next graded round of the reverted-to candidate could automatically
    re-apply that graph (the ping-pong the pairing rule exists to close), so
    the revert consumes its own pairing: ``previous_candidate_displaced_by``
    becomes ``None`` until the next ORDINARY apply records a fresh one. The
    pointer itself stays — the household's way-back button still offers the
    displaced candidate, through review + Apply, which is their call.

    A cheap write on ``save_v2_state``'s own rule: losing it to a power cut
    leaves a paired pointer, which re-opens the window for one round on one
    boot — narrow, and the round it would mislead still runs every apply-door
    gate.
    """
    with _state_lock:
        state = load_v2_state()
        if state is None:
            return
        state["previous_candidate_displaced_by"] = None
        save_v2_state(state)


def _crossover_label(geometry: Any, with_slope: bool) -> str:
    """One declared crossover as the household reads it.

    ``"2500 Hz"``, or ``"2500 Hz at 24 dB/octave"`` when the slope is part of
    what moved — named only when it moved, because a slope in a sentence about
    a frequency change is one more number to hold and nothing to do with.
    """
    label = f"{_fc_hz_label(geometry.fc_hz)} Hz"
    if not with_slope:
        return label
    return f"{label} at {geometry.slope_db_per_octave:g} dB/octave"


def _blocking_apply_issue(payload: Mapping[str, Any]) -> dict[str, str] | None:
    """The single most relevant blocker from a blocked apply payload.

    ``payload["issues"]`` already carries the full severity-tagged list; this
    picks the first blocker (the seam always orders the real cause before any
    generic trailer issue) so a compact ``{id, message}`` pointer reaches the
    browser without digging through the composed profile.
    """
    issues = payload.get("issues")
    if not isinstance(issues, list):
        return None
    candidates = [issue for issue in issues if isinstance(issue, Mapping)]
    for issue in candidates:
        if issue.get("severity") == "blocker":
            return {
                "id": str(issue.get("code") or ""),
                "message": str(issue.get("message") or ""),
            }
    if candidates:
        first = candidates[0]
        return {
            "id": str(first.get("code") or ""),
            "message": str(first.get("message") or ""),
        }
    return None


def _dsp_apply_is_known_inactive(payload: Mapping[str, Any]) -> bool:
    apply = payload.get("apply")
    if not isinstance(apply, Mapping):
        return False
    phase, result = str(apply.get("phase") or ""), str(apply.get("result") or "")
    # The proof-phase set is imported, not transcribed (#2519). Every proof
    # failure refuses before ``load_config`` runs, so all of them are known
    # inactive — and a transcribed member list is how the two results that
    # split out of ``candidate_changed`` would have silently become "we cannot
    # tell whether the speaker changed", which raises the far scarier
    # ``apply_result_unknown`` refusal at the household.
    return bool(apply.get("finished_at")) and (
        (phase, result) == ("prepare", "prepare_failed")
        or (phase == "proof" and result in DSP_PROOF_INACTIVE_RESULTS)
        or (phase == "validate"
            and result in {"invalid_config", "runner_error", "timeout"})
        or (apply.get("rollback_attempted") is True
            and apply.get("rollback_succeeded") is True))


def _persist_apply_blocked(
    issue: Mapping[str, str] | None,
    current_review: tuple[str, str, Any] | None = None,
) -> None:
    """Record (or clear) the last blocked-apply issue for the fix_and_retry
    screen's nudge (layered onto REASON_APPLY_FAILED at the "applying"
    phase — owner ruling, 2026-07-20)."""
    if current_review is not None:
        session_id, fingerprint, revision = current_review
        _update_current_review(session_id, fingerprint, revision,
            {"apply_blocked": dict(issue) if issue else None})
        return
    state = load_v2_state()
    if state is None:
        return
    state["apply_blocked"] = dict(issue) if issue else None
    save_v2_state(state)


def _reopen_candidate_artifact(
    state: Mapping[str, Any] | None, evidence: Mapping[str, Any]
) -> Mapping[str, Any]:
    """Reopen the published candidate JSON from the session's evidence bundle."""
    from jasper.active_speaker.bundles import sessions_dir

    session_id = str((state or {}).get("session_id") or "")
    bundle_session = str(evidence.get("bundle_session_id") or "")
    candidates = []
    root = sessions_dir()
    try:
        bundle_dirs = (
            [root / bundle_session] if bundle_session else sorted(root.iterdir())
        )
    except OSError:
        bundle_dirs = []
    for bundle in bundle_dirs:
        path = (
            bundle / "evidence" / "v1" / "artifacts" / "crossover_v2"
            / session_id / "candidate.json"
        )
        if path.is_file():
            candidates.append(path)
    if not candidates:
        raise CrossoverV2Refused(
            "the published measured candidate could not be found; measure again"
        )
    try:
        return json.loads(candidates[-1].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CrossoverV2Refused(
            "the published measured candidate could not be reopened"
        ) from exc
