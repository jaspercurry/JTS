# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The v2 crossover conductor's web host (Wave 5a endpoint binding).

Owns everything between the ``/correction/crossover/v2/*`` POST routes (thin
dispatch branches in :mod:`jasper.web.correction_setup`) and the pure conductor
(:mod:`jasper.active_speaker.crossover_v2_flow`):

* the **durable v2 flow state** (one JSON file) that ``status_payload`` threads
  into the envelope as ``status["crossover_v2"]`` — phase / candidate / verify
  / failure / apply_blocked / needs_recovery / applied;
* the **session volume plan** singleton (one fixed measurement volume per
  session, §5.5) and its open/close/abandon wiring — including the
  walked-away guarantee: every terminal relay outcome drains the restore-once
  path;
* the **production seam bindings** — real ``analyze_program_capture``, real
  evidence-store publication (publish → tamper-checked reopen, §5.6), the real
  CamillaController-backed program playback via
  :func:`jasper.active_speaker.crossover_v2_flow.bind_program_playback_seams`,
  and the apply gate reading the durable applied flag;
* the **plan runner host** (:func:`build_v2_run_and_consume`) that drives the
  REAL :func:`jasper.capture_relay.session.run_capture_plan` on a worker
  thread, mirroring ``build_crossover_relay_plan_run_and_consume``'s thread
  model (``asyncio.to_thread`` + shielded cancel-drain + purge on teardown +
  ``stop_event``/``stop_lock`` stop semantics);
* the **host-owned error mapping**: ``CaptureTimeout`` / relay-session death →
  ``relay_timeout`` failure state + volume abandon; conductor failure codes →
  ``status["crossover_v2"]["failure"]``.

Session binding (§5.6): the durable state is keyed to the relay session id. A
new ``/v2/session`` POST hydrates through
:meth:`CrossoverV2Conductor.hydrate`, which invalidates CHECK/MEASURE evidence
for a different session; ``/v2/verify`` re-arms VERIFY only (a 1-entry plan)
from the persisted post-apply state, per §5.2's re-verify action.

ON-DEVICE: the acoustic playback binding is not exercised hardware-free (same
status as the room/sync relay flows) — W6 validates it end-to-end on JTS3.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import dataclasses
import hashlib
import json
import logging
import math
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping, MutableMapping, Sequence

from jasper.atomic_io import atomic_write_text
from jasper.log_event import log_event

if TYPE_CHECKING:
    from jasper.active_speaker.crossover_v2_flow import AnalyzeCapture
    from jasper.active_speaker.model_error_store import ModelErrorStoreSnapshot

logger = logging.getLogger(__name__)

STATE_SCHEMA_VERSION = 1
STATE_KIND = "jts_crossover_v2_flow_state"
DEFAULT_V2_STATE_PATH = Path(
    "/var/lib/jasper/active_speaker_crossover_v2_state.json"
)

# The wizard-facing relay kind label (mirrors the legacy
# "crossover_sweep:<kind>" labels so /status.relay consumers need no new
# vocabulary beyond the prefix).
V2_RELAY_KIND_SESSION = "crossover_v2:session"
V2_RELAY_KIND_VERIFY = "crossover_v2:verify"

# Where the household-readable projection of a banked finding set rides inside
# the durable state's ``evidence`` refs: a list of ``{household_copy, at}``
# rows, written by :func:`_bank_household_findings` and read by
# :func:`_household_findings_status`. Up here with the other state vocabulary
# rather than beside its writer, because THREE places name it — the writer, the
# reader, and ``persist_conductor_state``'s carry-forward — and a key three
# functions spell is a name, not a local detail.
FINDING_HOUSEHOLD_REFS_KEY = "household_findings"

# Downsample ceiling for the persisted predicted-sum verify prior — enough
# resolution for the ±1.5 dB [Fc/2, 2Fc] comparison at 1/6-octave smoothing
# while keeping the durable state file small. Reduction to this ceiling is a
# block average in linear power (see ``_decimate_sum``), never a raw stride
# — issue #1858 found the prior stride picked one raw bin per output point,
# which aliases below ~600 Hz, where the 46.875 Hz stride spacing leaves
# fewer than 3 samples in a 1/3-octave band (below ~200 Hz the spacing
# exceeds the band's own width outright — zero guaranteed samples), so the
# "1/6-octave smoothing" this comment budgets resolution for was not
# actually happening upstream of it.
MAX_PERSISTED_SUM_POINTS = 512

# --------------------------------------------------------------------------- #
# operator-debug capture retention (Part 2 — off by default, bounded)
# --------------------------------------------------------------------------- #
#
# Productizes a hot-patch that used to live directly in ``_analyze`` and kept
# getting silently wiped by every deploy (runtime Python is copied fresh from
# the rsync checkout — see AGENTS.md "Runtime Python lives in /opt/jasper").
# An operator investigating a hardware failure creates
# ``XOVER_CAPTURE_DUMP_DIR / "ENABLED"``; every subsequent CHECK/MEASURE/
# VERIFY capture then persists its raw WAV + a diagnostic-summary sidecar
# here for offline analysis. ABSENT by default → zero behavior change for
# real households. Delete the marker (or the whole directory) to disable and
# reclaim the disk budget below; this is deliberately removable, not a
# permanent feature.
XOVER_CAPTURE_DUMP_DIR = Path("/var/lib/jasper/xover-capture-dump")
# The operator-created enable marker's filename, INSIDE XOVER_CAPTURE_DUMP_DIR.
# Excluded from ring-buffer pruning below (see `_prune_capture_dump`) — the
# marker is not a capture artifact, and without this exclusion the ring
# buffer could eventually delete its own on/off switch (the marker is
# typically the oldest file in the directory, so a naive oldest-first count/
# byte cap would evict it first), silently re-disabling retention the
# operator explicitly turned on.
XOVER_CAPTURE_DUMP_ENABLED_MARKER = "ENABLED"
# Ring-buffer caps, bounded BOTH ways so an operator who forgets to disable
# this can't fill the 1 GB Pi's SD card. Counts every capture file in the
# directory (a WAV and its JSON sidecar are two entries; the enable marker
# is not counted), oldest-first deletion.
XOVER_CAPTURE_DUMP_MAX_FILES = 90
XOVER_CAPTURE_DUMP_MAX_BYTES = 300 * 1024 * 1024  # 300 MB

_state_lock = threading.Lock()
_state_path_override: Path | None = None

_volume_plan_lock = threading.Lock()
_volume_plan: Any = None


class CrossoverV2Refused(ValueError):
    """A v2 endpoint refusal (maps to HTTP 400 in the dispatch ladder).

    ``code`` is an optional :data:`~jasper.active_speaker.crossover_v2_flow.REASON_REGISTRY`
    code. A refusal raised BEFORE any durable state is written — which the
    session-open pre-flight is, by design (issue #1821: no link minted, no
    session burned) — never reaches the envelope, because the envelope renders
    from a PERSISTED ``failure``. So a pre-flight refusal that carried only a
    sentence left the household reading the fix rather than one click from it:
    the reason's ``next_action`` existed and nothing could render it. Stamping
    the code here lets :func:`refusal_next_action` hand the dispatcher the same
    action the hard-stop screen would have shown, from the same registry entry.
    """

    def __init__(self, *args: Any, code: str = "") -> None:
        super().__init__(*args)
        self.code = code


def refusal_next_action(exc: BaseException) -> dict[str, Any] | None:
    """The action a refusal's own reason declares, for a 400 response body.

    ``None`` when the refusal carries no code or its code declares no action —
    the ordinary case for the many refusals whose only honest answer is prose.
    Copy and destination come from the SAME registry entry the envelope's
    hard-stop screen reads, so the pre-flight 400 and the post-persist screen
    can never offer different buttons for the same refusal.
    """
    from jasper.active_speaker.crossover_v2_flow import REASON_REGISTRY

    code = str(getattr(exc, "code", "") or "")
    spec = REASON_REGISTRY.get(code) if code else None
    if spec is None or not spec.next_action:
        return None
    return dict(spec.next_action)


class CrossoverV2LocalSeamError(RuntimeError):
    """A LOCAL play/analyze seam raised ``OSError`` — not a relay-transport death.

    W6 hardware run 3 finding G: the DSP writer lock's ``os.open`` on a
    read-only ``config_dir`` (finding F) raised a bare ``OSError`` from inside
    ``on_armed`` — the exact same exception TYPE
    ``jasper.capture_relay.session.run_capture_plan``'s transport polling
    loop raises on a genuine relay-connection failure (``client.status``
    reaching an unreachable host). ``build_v2_run_and_consume``'s relay-death
    except arm cannot tell those apart by type alone, so it misclassified a
    local filesystem fault as ``relay_timeout``. ``on_armed``/``consume``
    convert a local ``OSError`` to THIS type at the seam boundary before it
    can reach that arm, so it falls through to the catch-all cleanup arm's
    honest ``internal_error`` classification instead — a genuine transport
    ``OSError`` (never wrapped) still hits the relay-death arm unchanged.
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
    :data:`~jasper.active_speaker.crossover_v2_flow.REASON_PROGRAM_UNPLAYABLE`,
    so a deterministic "the household has not confirmed the safety limits" and
    a genuine level-ceiling failure rendered the same sentence and offered the
    same (for the former, actively harmful) action. Refusal identity survives
    the boundary now: ``PROFILE_NOT_CONFIRMED`` gets its own code and screen,
    and every other refusal keeps ``program_unplayable`` but carries its own
    slugs out for forensics (persisted under ``state["failure"]["refusals"]``
    and logged) instead of being erased.

    This is the ONE classifier. ``build_v2_run_and_consume``'s cleanup arm and
    ``jasper.web.correction_setup._relay_failure_message`` both call it, so the
    phone's failure screen and the operator wizard's relay status line can
    never disagree about which refusal happened — the drift that let a raw
    ``"program re-admission refused: program_profile_not_confirmed"`` reach the
    wizard's DOM while the phone was told something else entirely.
    """
    from jasper.active_speaker.crossover_v2_flow import (
        REASON_PROGRAM_PROFILE_NOT_CONFIRMED,
        REASON_PROGRAM_UNPLAYABLE,
        CrossoverV2FlowError,
    )
    from jasper.active_speaker.program_admission import (
        ProgramAdmissionError,
        ProgramAdmissionRefusal,
    )
    from jasper.active_speaker.program_playback import (
        ProgramPlaybackError,
        ProgramPlaybackRefused,
    )

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
    ("unknown commission tier 'turbo' (expected one of full, express)",
    "cloud_measure_positions must be 6..12, got 14"). Two call sites used to
    rewrap them as ``CrossoverV2Refused(str(exc))``, which the wizard's 400 arm
    echoes into the DOM verbatim — the exact leak
    :func:`classify_program_failure` exists to close, defeated by the rewrap
    happening BEFORE any classification: once it is a ``ValueError`` the
    classifier no longer claims it, and
    ``correction_setup._relay_failure_message`` never sees it either.

    So classify FIRST and carry the code out. The message comes from the same
    :data:`~jasper.active_speaker.crossover_v2_flow.REASON_REGISTRY` entry the
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
    from jasper.active_speaker.crossover_v2_flow import (
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


def profile_refusal_code(evaluation_status: str) -> str:
    """Map a :class:`~jasper.active_speaker.driver_safety.DriverSafetyProfileEvaluation`
    status to the reason code whose copy names the action that ACTUALLY clears it.

    The pre-flight holds evidence the play seam does not — the play seam's
    admission vocabulary carries one ``PROFILE_NOT_CONFIRMED`` slug for every
    un-playable profile state — so this is where the three genuinely different
    household actions separate:

    * ``missing``    → finish the driver details. ``/sound/`` renders no confirm
      control at all in this state, so "confirm the safety limits" would name a
      button that is not on the page.
    * ``incomplete`` → add the missing values first.
      ``build_driver_safety_profile`` refuses a confirm while derived issues
      exist, so "confirm" would 400 even if the household found the control.
    * everything else (``unconfirmed``, ``stale``, ``malformed``) → confirm.
      All three are cleared by the one confirm action: it saves the visible
      values and rebuilds the profile, so a fingerprint rotation, an output
      change, and a corrupt artifact all end the same way.
    """
    from jasper.active_speaker.crossover_v2_flow import (
        REASON_PROGRAM_PROFILE_INCOMPLETE,
        REASON_PROGRAM_PROFILE_MISSING,
        REASON_PROGRAM_PROFILE_NOT_CONFIRMED,
    )

    status = str(evaluation_status or "")
    if status == "missing":
        return REASON_PROGRAM_PROFILE_MISSING
    if status == "incomplete":
        return REASON_PROGRAM_PROFILE_INCOMPLETE
    return REASON_PROGRAM_PROFILE_NOT_CONFIRMED


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


def save_v2_state(state: Mapping[str, Any]) -> None:
    payload = {
        "schema_version": STATE_SCHEMA_VERSION,
        "kind": STATE_KIND,
        "updated_at": time.time(),
        **{k: v for k, v in state.items() if k not in {"schema_version", "kind"}},
    }
    with _state_lock:
        atomic_write_text(
            _state_path(),
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            mode=0o640,
            group_from_parent=True,
        )


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
    """Start-over's v2 clear (W6.10, gate-amended for W6.8's Undo).

    Clears the measurement-JOURNEY fields (session binding, accepted phases,
    candidate, verify, failure, gain plan, priors, evidence) so the envelope
    serves the clean start screen — but when a candidate is APPLIED, preserves
    ``applied`` + ``pre_apply_profile``: those are the ONLY durable pointers the
    v2-aware Undo (:func:`handle_v2_restore`, W6.8) restores from. A full
    :func:`clear_v2_state` here would unlink the sole reference to the retained
    pre-candidate snapshot, leaving the applied graph playing with Undo
    permanently unreachable. Not applied ⇒ full clear, as before.
    """
    state = load_v2_state()
    if state is None:
        return
    if not state.get("applied"):
        clear_v2_state()
        return
    pre_apply_profile = state.get("pre_apply_profile")
    attempts_loop = state.get("attempts_loop")
    save_v2_state({
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
        # its immediate predecessor; Undo below clears them because the graph
        # they describe no longer exists.
        "attempts_loop": (
            dict(attempts_loop) if isinstance(attempts_loop, Mapping) else None
        ),
        "pre_apply_profile": (
            dict(pre_apply_profile)
            if isinstance(pre_apply_profile, Mapping)
            else None
        ),
    })
    log_event(logger, "correction.crossover_v2_journey_reset_kept_applied")


def observe_apply_success(
    candidate_fingerprint: str,
    *,
    pre_apply_profile: Mapping[str, Any] | None = None,
    expected_post_apply_offset_db: float = 0.0,
) -> None:
    """Mark the v2 candidate applied — the apply-complete event that arms the
    soft-held VERIFY (§5.2). Called by the v2 apply endpoint on success.

    ``pre_apply_profile`` is the frozen applied-baseline snapshot the apply
    replaced (``build_baseline_profile_candidate``'s
    ``applied_recomposition_profile`` — see ``handle_v2_apply``), stashed here
    because the apply's own persisted profile pops that field once it becomes
    the new applied SSOT. This is the ONLY durable record of what the Undo
    path (``handle_v2_restore``) restores to; ``None`` when this was the
    speaker's first-ever applied crossover (nothing to undo back to).

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
    # but a terminal session-death code (a phone Stop, a relay timeout) can
    # land WHILE the auto-apply background thread's apply_baseline_profile
    # transaction is still in flight. If that race lands the stop FIRST,
    # clobbering it here would erase the evidence that the household
    # stopped even though the crossover genuinely got applied (this call
    # proves it) — the envelope needs BOTH facts to render an honest
    # "applied, but you stopped it — undo if you want" screen instead of a
    # false "nothing happened" or a false "start over, nothing changed."
    # The reverse race (a stop landing AFTER this call persists) is already
    # handled: persist_conductor_state preserves ``applied`` once it
    # observes it, for the same session.
    state["apply_blocked"] = None
    state["pre_apply_profile"] = (
        dict(pre_apply_profile) if isinstance(pre_apply_profile, Mapping) else None
    )
    offset_db = float(expected_post_apply_offset_db)
    state["expected_post_apply_offset_db"] = (
        round(offset_db, 3) if math.isfinite(offset_db) else 0.0
    )
    save_v2_state(state)
    log_event(
        logger,
        "correction.crossover_v2_applied",
        expected_post_apply_offset_db=state["expected_post_apply_offset_db"],
    )


def observe_restore() -> None:
    """Clear the durable v2 state after a successful Undo (mirrors
    :func:`observe_apply_success`). Resets the whole flow to a clean
    unmeasured state — matching the reset a pre-apply terminal failure
    already gets (:func:`_persist_terminal_failure`) — so the envelope lands
    back on the pre-measurement screen rather than a half-consistent
    "applying" phase pointing at the now-undone candidate.

    Every journey field is cleared EXPLICITLY here because this mutates the
    loaded state in place, unlike :func:`reset_v2_journey_state`, which writes
    a whole fresh dict and so drops unlisted fields for free. That asymmetry is
    a standing trap: a new durable field added anywhere else must be added to
    this list by hand or it survives an Undo. Three already did —
    ``session_phases`` left a re-verify session's ``["verify"]`` behind, which
    ``_phase_from_state`` resolved to the VERIFY screen ("The crossover is
    applied. Put the microphone back where it started…") moments after the
    household removed it; ``cloud`` left a geometry verdict describing an
    undone session for PR-4 to consume as if it were live; and ``evidence``
    (SF-2 review finding, 2026-07-27) left a stale ``cloud_artifacts`` fingerprint
    map behind for ``persist_conductor_state``'s own group-phase-less carry-
    forward (the B1 fix) to resurrect on the very next verify-only re-arm.
    Cleared wholesale, not by surgically deleting ``cloud_artifacts`` alone —
    :func:`reset_v2_journey_state` already treats the WHOLE ``evidence`` key
    as journey-scoped (nulled there unconditionally, applied or not), no other
    reader expects it to survive past the live measurement/apply flow it is
    written during, and a key-by-key deletion would leave this exact hole open
    for the next field someone adds under ``evidence``.

    ``measure`` is the fourth (#2087). It carries G1's ripple reservation — a
    statement about the capture a now-undone tuning was built from — and the
    same PR that added it also added a ``prior["measure"]`` carry-forward in
    :func:`persist_conductor_state`, which is structurally the identical
    resurrection mechanism the ``evidence`` finding above was filed for. It is
    listed here rather than left to the reachability argument (post-Undo the
    envelope resolves ``not_applicable``, and ``prepare_v2_verify`` refuses
    without ``applied``) because that argument makes the field correct by
    accident, and the accident is one screen-routing change away from being a
    stale caveat on a live session."""
    state = load_v2_state()
    if state is None:
        return
    state["applied"] = False
    state["candidate"] = None
    state["verify"] = None
    state["failure"] = None
    state["apply_blocked"] = None
    state["pre_apply_profile"] = None
    state["accepted_phases"] = []
    state["session_phases"] = []
    state["cloud"] = None
    state["gain_plan_db"] = None
    state["evidence"] = None
    state["attempts_loop"] = None
    state["measure"] = None
    save_v2_state(state)


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
    ``jasper.active_speaker.crossover_v2_flow.CrossoverV2Conductor.authorize_begin``,
    which refuses the deferred VERIFY hold outright once this names a code
    rather than holding it toward a dishonest relay_timeout. Retained with
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
    from jasper.active_speaker.crossover_v2_flow import REASON_APPLY_FAILED

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
    from jasper.correction import coordinator

    target = coordinator.MeasurementAbortTarget()
    cm = coordinator.measurement_window(abort_target=target)
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
    from jasper.correction.coordinator import MeasurementWindowError

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


def _session_volume_io(camilla_factory: Any) -> tuple[Callable[[float], Any], Callable[[], Any]]:
    """(set, get) main-volume callables for the plan drains, fail-closed on
    CamillaUnavailable so a wedged DSP cannot silently no-op a recovery."""
    from jasper.camilla import CamillaUnavailable

    async def _set(db: float) -> bool:
        try:
            return await camilla_factory().set_volume_db(db, best_effort=False)
        except CamillaUnavailable as exc:
            raise RuntimeError("CamillaDSP is unavailable") from exc

    async def _get() -> float | None:
        try:
            return await camilla_factory().get_volume_db(best_effort=False)
        except CamillaUnavailable as exc:
            raise RuntimeError("CamillaDSP is unavailable") from exc

    return _set, _get


def _release_pause_best_effort(run_async: Any) -> None:
    """Release the session measurement pause from a drain that runs outside the
    runner (recover / ceiling). Idempotent no-op when nothing is held (already
    released, or a crash-fresh process that never entered it)."""
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
    """
    plan = session_volume_plan()
    try:
        if not plan.stale_active():
            return False
    except (OSError, RuntimeError, ValueError):
        return False
    set_v, get_v = _session_volume_io(camilla_factory)
    try:
        run_async(
            plan.enforce_ceiling(set_v, get_v),
            timeout=_SESSION_VOLUME_DRAIN_TIMEOUT_S,
        )
    except (concurrent.futures.TimeoutError, OSError, RuntimeError, ValueError):
        log_event(
            logger,
            "correction.crossover_v2_ceiling_enforce_failed",
            level=logging.ERROR,
        )
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


def recover_session_volume(
    run_async: Any, camilla_factory: Any
) -> tuple[bool, str]:
    """Drain the v2 plan's unresolved / stale-active state (the volume_recovery
    screen's ``recover_volume`` action). Returns ``(succeeded, result_value)``.

    Routes to ``SessionVolumePlan.recover_unresolved`` — the v2 owner of the
    unresolved state — instead of the legacy lease, and releases any held
    measurement pause on success.
    """
    from jasper.active_speaker.session_volume_plan import (
        SessionVolumeRestoreResult,
    )

    plan = session_volume_plan()
    set_v, get_v = _session_volume_io(camilla_factory)
    try:
        result = run_async(
            plan.recover_unresolved(set_v, get_v),
            timeout=_SESSION_VOLUME_DRAIN_TIMEOUT_S,
        )
    except concurrent.futures.TimeoutError:
        log_event(
            logger,
            "correction.crossover_v2_volume_recovery_timeout",
            level=logging.ERROR,
        )
        result = SessionVolumeRestoreResult.FAILED
    succeeded = result is not SessionVolumeRestoreResult.FAILED
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
    """
    plan = session_volume_plan()
    enforce_session_volume_ceiling_if_stale(run_async, camilla_factory)
    if plan.measurement_volume_db is None or plan.needs_recovery:
        return
    set_v, get_v = _session_volume_io(camilla_factory)
    try:
        run_async(
            plan.abandon(set_v, get_v, reason="stale_session_reset"),
            timeout=_SESSION_VOLUME_DRAIN_TIMEOUT_S,
        )
        log_event(logger, "correction.crossover_v2_stale_session_reset")
    except (concurrent.futures.TimeoutError, OSError, RuntimeError, ValueError):
        log_event(
            logger,
            "correction.crossover_v2_stale_session_reset_failed",
            level=logging.ERROR,
        )
    _release_pause_best_effort(run_async)


# --------------------------------------------------------------------------- #
# status threading (S1b)
# --------------------------------------------------------------------------- #


def _phase_from_state(state: Mapping[str, Any] | None) -> str:
    from jasper.active_speaker.crossover_v2_flow import (
        CAPTURE_PHASES,
        PHASE_APPLYING,
        PHASE_CLOSING,
        PHASE_DONE,
        PHASE_MEASURE,
        PHASE_REVIEW,
        PHASE_VERIFY,
        PRE_CLOUD_CAPTURE_PHASES,
    )

    accepted = set(
        state.get("accepted_phases") or () if isinstance(state, Mapping) else ()
    )
    applied = bool(state and state.get("applied"))
    # A session runs a SUBSET of CAPTURE_PHASES (a verify-only re-arm runs just
    # VERIFY), so walk the subset the conductor recorded. State written before
    # the position groups shipped has no such field, and it came from a session
    # that ran exactly the pre-cloud three — reading it against the longer
    # tuple would report a household mid-cloud in a session that never had one.
    #
    # FILTER FIRST, then decide whether anything survived: a corrupt
    # ``["nonsense"]`` filters to the empty tuple, and treating "recorded but
    # unrecognisable" as "recorded" would walk zero phases and fall straight
    # through to PHASE_DONE — telling a household "Your speaker is tuned" on
    # the strength of a garbled state file. Fail toward the honest fallback.
    recorded = state.get("session_phases") if isinstance(state, Mapping) else None
    known = (
        tuple(str(p) for p in recorded if str(p) in CAPTURE_PHASES)
        if isinstance(recorded, (list, tuple))
        else ()
    )
    phases = known or PRE_CLOUD_CAPTURE_PHASES
    for phase in phases:
        if phase not in accepted:
            if phase == PHASE_VERIFY and PHASE_MEASURE in accepted and not applied:
                return PHASE_APPLYING
            return phase
    # Every phase this session ran is accepted. WHICH terminal state that is
    # depends on whether the session ever intended to verify (two-stage
    # commission D3/PR-T2, work order premise 6 — a verified collision).
    #
    # A MEASURE-ONLY session — stage 1 of the two-stage flow: CHECK, MEASURE,
    # CLOUD_MEASURE, no VERIFY — used to fall straight through to PHASE_DONE,
    # the RESULT screen, whose copy is "Your speaker is tuned." Nothing had
    # been applied; the household had measured a speaker and was told it was
    # tuned. The special case one line up cannot catch it (it keys on
    # PHASE_VERIFY being in the walked phases, which is exactly what a
    # measure-only session lacks), so the honest terminal for that shape is
    # the review interlude: a candidate to look at and a decision to make.
    #
    # Keyed on the WALKED tuple, not on ``known``, so the corrupt-state
    # fallback documented above keeps working unchanged: a garbled
    # ``session_phases`` filters to the empty tuple, walks
    # PRE_CLOUD_CAPTURE_PHASES — which DOES contain PHASE_VERIFY — and so can
    # never reach the review branch on the strength of an unreadable state
    # file. It resolves through the loop above to its first unaccepted phase,
    # exactly as before.
    #
    # ``applied`` still wins over the REVIEW interlude: once something is
    # genuinely on the speaker the decision has been made, and re-offering
    # "apply this?" over a speaker that already has it would be the mirror of
    # the bug being fixed.
    #
    # But an applied measure-only session is not DONE either (two-stage
    # commission D2, PR-T3): stage 1 measured, the household applied from the
    # review screen, and the post-apply check — stage 2 — has not been opened
    # yet. That is exactly PHASE_VERIFY: "the crossover is applied, put the
    # microphone back where it started", whose screen now carries the action
    # that opens stage 2. Once stage 2 IS open its own conductor records VERIFY
    # in ``session_phases``, so this branch stops firing and the ordinary walk
    # above resolves the rest of the journey — including PHASE_DONE's "applied
    # implies graded" ladder for a stage 2 that ran and could not decide.
    if PHASE_VERIFY not in phases:
        if applied:
            return PHASE_VERIFY
        # …and the measuring session's own TAIL is not the review interlude
        # either. Accepting the final cloud position marks every stage-1 phase
        # accepted, so this walk resolves the instant that capture lands —
        # while the household is still holding a phone at the confirm screen
        # (up to the runner's full between-step budget) and again while the
        # combine + fit run. Both used to render the review screen's
        # no-candidate copy: "JTS measured your speaker but has no correction
        # to propose — measure again to try afresh", with a destructive
        # "Measure again" beside it, over a measurement that was still in
        # progress. ``cloud_close`` is what tells those moments apart from a
        # session that genuinely ended with nothing (where it is ``""``, and
        # the review screen's absence copy is the honest answer).
        if str((state or {}).get("cloud_close") or ""):
            return PHASE_CLOSING
        return PHASE_REVIEW
    return PHASE_DONE


def _provenance_note(measured_this_session: bool | None) -> str:
    """PR-7's household-facing provenance caption — one owner of the copy, so
    the chart never has to (or may) phrase this itself.

    A re-armed session's ``persist_conductor_state`` can carry a group's
    ``cloud`` entry forward from an EARLIER session verbatim (see
    ``_cloud_summary``'s own comment and the B1 fix above it) — so
    ``/state.crossover_v2.cloud`` and the envelope can describe a measurement
    that did not happen in the session currently open on the page. Silently
    charting it as fresh would be exactly the kind of measured-narrow-
    stated-wide claim this program exists to avoid.

    ``""`` for both "definitely current" and "unknown" (a durable state
    written before this marker existed, or the whole entry unavailable) —
    mirrors :func:`~jasper.active_speaker.crossover_v2_flow._geometry_guidance_copy`'s
    "empty string when nothing to say" rule rather than asserting freshness
    it cannot prove. Only the one state worth interrupting the household
    for — data that is KNOWN to be stale — gets a sentence.
    """
    if measured_this_session is False:
        return (
            "This chart is from a previous session's measurement — "
            "re-measure to see this session's own result."
        )
    return ""


def _compact_cloud_status(
    cloud_state: Any, *, current_session_id: str | None = None,
) -> dict[str, Any] | None:
    """PR-4's ``/state`` projection of the durable ``cloud`` block — compact:
    per band, only ``passed``; the excluded-interval COUNT, not the
    intervals; the geometry verdict's two household-relevant bits.

    The full per-null τ/r/evidence numbers and the decimated curve live in
    the durable state's own ``pipeline`` sub-key (:func:`_cloud_summary`) and
    the bundle artifact (:func:`bind_cloud_publisher`) — this stays a
    shape-scoped projection, not a third owner of the same data: a consumer
    that reads ``cloud`` alone (the doctor) never has to parse curve-shaped
    data mixed into it. PR-7's chart feed is a fourth, separate KEY —
    :func:`_chart_cloud_status`, riding alongside this one on
    :func:`crossover_v2_status_block`'s own returned dict — for that same
    shape-scoping reason. It is **not** a separate endpoint or a smaller HTTP
    response: see that function's own docstring for the measured byte cost
    and why the actual size mitigation is its own re-decimation ceiling, not
    this key split.

    ``flatness`` (plan PR-5) is the spec-facing gauge, copied VERBATIM from
    the pipeline's own ``flatness`` key — the reduction
    :func:`~jasper.active_speaker.flat_spec.spec_flatness_gauge` made of the
    same ``spec`` report the ``spec_bands`` above project. It rides the
    compact block because the envelope's expert disclosure
    (``crossover_envelope_v2._flatness_details_lines``) renders from THIS
    projection, and copying is what makes the gauge, the ledger line, and the
    persisted report byte-identical rather than merely consistent. ``None``
    when the pipeline never became available — the same "never a fabricated
    clean reading" rule as ``excluded_interval_count`` below.

    ``reference_db`` (PR-7) rides alongside ``flatness`` for the same reason
    ``spec_bands`` carries ``max_deviation_db``: it is the one report-level
    number a chart needs to draw the tolerance corridor
    (``reference_db ± tolerance_db`` per band) and PR-5 already computed it
    once, inside ``spec`` — copied verbatim, never re-derived. ``None`` under
    the same unavailable-pipeline rule as everything else here.

    ``validity_floor_hz`` rides alongside it for one reason: without it a
    live surface cannot tell WHY ``flatness.n_excluded`` is large. The
    interference instruments and the gate-validity clamp both remove
    spec-band bins, and only the honesty instruments' removals are counted
    by ``excluded_interval_count`` — so a reader seeing 4063 excluded bins
    and 5 excluded intervals needs the floor to separate "the room combed
    this speaker" from "one capture's gate collapsed". ``None`` means either
    no position reported a usable floor or the pipeline never ran; it never
    means zero.

    ``spec_bands`` carries each band's own ``max_deviation_db`` (N-3) AND
    ``tolerance_db`` (PR-7 — the corridor half-width a chart draws per band):
    the per-band numbers are what a chart labels, and their absence from the
    only projection a page reads is exactly the pressure that grows a second
    derivation somewhere downstream. Copied from the report like everything
    else here — this stays a projection, never an owner.

    ``carve_outs`` (plan PR-6b) rides the compact block for that same reason,
    and it is the one place this projection is deliberately NOT reduced: the
    τ/r numbers and the copy strings ARE the disclosure owner decision 1
    committed to, so summarising them to a count here would leave the only
    surface a page reads unable to say why a band lost bins — and would grow
    the second copy owner the producer
    (:func:`~jasper.active_speaker.crossover_v2_flow.carve_outs_by_band`)
    exists to prevent. **It is the largest thing on the entry, and that is
    stated rather than glossed:** measured 2026-07-27 on the S0 ten-position
    cloud (the widest real case this program has — three identified nulls plus
    the one screened range that falls inside a graded band, four rows), the
    carve-outs are **3162 of the entry's 4056 JSON bytes**, against 291 for
    ``spec_bands``, 217 for ``flatness`` and 186 for ``geometry_guidance`` — a
    dated snapshot, since any copy edit moves the digits by tens of bytes; what
    the corpus test pins is the structural claim (four rows, and this key
    larger than every other on the entry combined), not the digits. The copy
    strings are the bulk of it. What bounds it is the instruments
    themselves: three bands, one row per carved range that lands in one, and a
    range outside every spec band produces no row at all. Copied verbatim, like
    ``flatness``. ``[]`` when the pipeline never became
    available — an empty LIST, not ``None``, is safe here because the entry it
    sits in already reports ``overall_passed``/``excluded_interval_count`` as
    ``None`` for that state, so an empty carve-out list cannot be read as "we
    looked and found nothing" without contradicting its own neighbours.

    ``excluded_interval_count`` is ``None`` — not ``0`` — when the pipeline
    never successfully became available (SF-1 review finding, 2026-07-27):
    ``0`` reads as "the honest-instrument pipeline looked and found no
    interference", a fabricated-clean claim this program forbids when the
    pipeline simply never ran (a combine or DSP-step failure — see
    :func:`~jasper.active_speaker.crossover_v2_flow.assemble_cloud_group_result`'s
    own ``available: False`` shape). ``geometry_guidance`` is computed
    directly from the ``geometry`` verdict via
    :func:`~jasper.active_speaker.crossover_v2_flow._geometry_guidance_copy`
    rather than read out of the pipeline's own copy of it, because geometry
    locking is decided and RECORDED before the pipeline ever runs (see
    ``_close_cloud_group``) — a locked group's "spread the mic further"
    guidance must survive an unrelated downstream DSP failure, not disappear
    with it.

    ``provenance_note`` (PR-7) is the household-facing half of the same
    marker: ``current_session_id`` is the session the CALLER currently has
    open (``crossover_v2_status_block``'s own ``state["session_id"]``);
    ``_cloud_summary`` now stamps each phase's dict with the session that
    actually produced it. When the two disagree — a group carried forward
    from an earlier session (see that function's own comment) — the note
    says so via :func:`_provenance_note`; a durable state written before the
    stamp existed (no ``session_id`` on the block) reads as unknown, not
    stale, so an upgrade does not manufacture a false "this is old" warning
    for data nobody ever mis-attributed.
    """
    from jasper.active_speaker.crossover_v2_flow import _geometry_guidance_copy

    if not isinstance(cloud_state, Mapping):
        return None
    out: dict[str, Any] = {}
    for phase, block in cloud_state.items():
        if not isinstance(block, Mapping):
            continue
        geometry = block.get("geometry")
        geometry = geometry if isinstance(geometry, Mapping) else {}
        pipeline = block.get("pipeline")
        pipeline = pipeline if isinstance(pipeline, Mapping) else {}
        produced_by = block.get("session_id")
        measured_this_session: bool | None = None
        if isinstance(produced_by, str) and produced_by and current_session_id:
            measured_this_session = produced_by == current_session_id
        entry: dict[str, Any] = {
            "geometry_locked": bool(geometry.get("locked")),
            "thin_evidence": bool(geometry.get("thin_evidence")),
            "geometry_guidance": _geometry_guidance_copy(geometry),
            "spec_bands": [],
            "overall_passed": None,
            "excluded_interval_count": None,
            "flatness": None,
            "reference_db": None,
            "validity_floor_hz": None,
            "carve_outs": [],
            "provenance_note": _provenance_note(measured_this_session),
        }
        if pipeline.get("available") is True:
            spec = pipeline.get("spec")
            spec = spec if isinstance(spec, Mapping) else {}
            bands = spec.get("bands")
            entry["spec_bands"] = [
                {
                    "f_lo_hz": b.get("f_lo_hz"),
                    "f_hi_hz": b.get("f_hi_hz"),
                    "passed": b.get("passed"),
                    "max_deviation_db": b.get("max_deviation_db"),
                    "tolerance_db": b.get("tolerance_db"),
                }
                for b in bands
                if isinstance(b, Mapping)
            ] if isinstance(bands, list) else []
            entry["overall_passed"] = spec.get("overall_passed")
            entry["reference_db"] = _finite(spec.get("reference_db"))
            merged = pipeline.get("merged_excluded_bands_hz")
            entry["excluded_interval_count"] = (
                len(merged) if isinstance(merged, list) else 0
            )
            flatness = pipeline.get("flatness")
            # Copied, never re-derived — see this function's docstring.
            entry["flatness"] = dict(flatness) if isinstance(flatness, Mapping) else None
            floor = pipeline.get("validity_floor_hz")
            entry["validity_floor_hz"] = (
                float(floor) if isinstance(floor, (int, float)) else None
            )
            carve_outs = pipeline.get("carve_outs")
            # Copied, never re-derived — same rule as ``flatness`` above. A
            # durable state written by a build BETWEEN PR-4 and PR-6b has an
            # available pipeline but no ``carve_outs`` key, and keeps the empty
            # default — indistinguishable here from a group that genuinely
            # carved nothing. ``excluded_interval_count`` is the tell for a
            # reader who needs to know: > 0 alongside an empty carve-out list
            # is the pre-PR-6b era, since a group that carved nothing has a
            # count of 0. No repair is attempted from this projection: it is
            # not an owner of the pipeline's data (see the docstring).
            entry["carve_outs"] = (
                [dict(band) for band in carve_outs if isinstance(band, Mapping)]
                if isinstance(carve_outs, list)
                else []
            )
        out[str(phase)] = entry
    return out or None


# PR-7's own re-decimation ceiling for the polled chart feed — HALF of
# crossover_v2_flow.CLOUD_CURVE_MAX_JSON_POINTS (512), the ceiling the
# pipeline's own ``curve`` key (and the persisted bundle artifact it is
# copied from) already uses. Review S-1 (2026-07-27) measured the byte cost
# of NOT re-decimating: 41,161 bytes for both phases' ``cloud_chart`` entries
# at the full 512-point resolution, on the real S0 ten-position cloud —
# roughly 82% of an otherwise-typical envelope response, repeated on every
# ~1.5 s poll while the wizard page is open. Halving to 256 points/phase
# measured 20,653 bytes on the same corpus (a 49.8% reduction) for a chart
# drawn into a ~640 px-wide canvas, where 256 points is already well under
# 1 px/point — visually identical to 512 on the one dimension that matters
# (this projection's own consumer), so nothing is lost by re-decimating
# again here rather than raising the ceiling everywhere `curve` is used.
CHART_CURVE_MAX_JSON_POINTS = 256


def _decimate_curve_for_chart(freqs: Any, mags: Any) -> dict[str, Any] | None:
    """Stride a stored curve down to at most :data:`CHART_CURVE_MAX_JSON_POINTS`.

    THE chart feed's decimation — extracted from :func:`_chart_cloud_status`'s
    body (two-stage commission D4) when the predicted curve became a second
    curve on the same block. D4 asks for the prediction to ride "the existing
    ``CHART_CURVE_MAX_JSON_POINTS`` path so the chart feed keeps one decimation
    owner"; a second inline copy of this stride would be a second owner, and
    two curves drawn in one frame at silently different densities is exactly
    the drift that costs. ``None`` for anything that is not a usable pair, so a
    caller never fabricates an empty curve out of malformed state.

    **Ceiling-division stride, not floor (gate finding on #1858, SF-1).** The
    original shape here was ``step = n // CAP`` — a *soft* ceiling, documented
    (and pinned, before this fix) as capable of overshooting by up to one
    stride: 1031 raw points strode by 4 and yielded 258, not 256. That was
    tolerable while every caller's persisted length always landed at or above
    ``CAP * 2`` (both ``_decimate_sum``'s old raw stride and
    ``_decimate_curve_for_json``'s stride always overshoot to slightly above
    their own 512-point cap). #1858's block-average fix to ``_decimate_sum``
    changed that: block-averaging *undershoots* its cap instead of
    overshooting it (a 32769-bin capture landed at 504, not 512-513), which
    put the predicted curve's persisted length just BELOW ``CAP * 2`` — where
    floor division gives ``step = 1``, i.e. no reduction at all (504 points
    rendered, not ~252), breaking the soft-ceiling promise outright and
    rendering the prediction at roughly double the cloud curves' density in
    the same chart frame. Ceiling division (``-(-n // CAP)``, this module's
    existing integer-ceiling idiom — see
    :func:`~jasper.audio_measurement.spatial_combine._decimate_to_analysis_grid`)
    makes ``len(rendered) <= CAP`` a TRUE hard bound for any input length,
    closing the whole class rather than this one instance: it guarantees
    ``step >= n / CAP`` by construction, so ``ceil(n / step) <= CAP`` always.
    Both curve families now render through the identical formula, so neither
    can silently outrun the other's density regardless of which side of any
    boundary their own persisted length lands on.

    **One deliberate behaviour delta from the inlined version this replaced.**
    A zero-length pair used to yield ``{"freqs_hz": [], "magnitude_db": []}``;
    it now yields ``None``. Reachable only from malformed durable state — a
    pipeline marked ``available: True`` whose stored curve is empty — and the
    new answer is the honest direction: an empty curve renders as "we looked
    and there is nothing there", which is the fabricated-clean-reading shape
    this module forbids, whereas ``None`` says "no curve", which is what an
    empty stored curve actually means.
    """
    if not isinstance(freqs, list) or not isinstance(mags, list):
        return None
    n = min(len(freqs), len(mags))
    if n == 0:
        return None
    step = max(1, -(-n // CHART_CURVE_MAX_JSON_POINTS))
    return {
        "freqs_hz": [_finite(f) for f in freqs[:n:step]],
        "magnitude_db": [_finite(m) for m in mags[:n:step]],
    }


def _chart_cloud_status(cloud_state: Any) -> dict[str, Any] | None:
    """PR-7's chart-feed projection of the durable ``cloud`` block — the ONE
    thing :func:`_compact_cloud_status` deliberately withholds: the decimated
    combined curve a before/after chart draws. Everything else the chart
    needs (the tolerance corridor's ``reference_db``/``tolerance_db``, the
    carve-out disclosure) already rides the compact block, so duplicating it
    here would be a second, driftable copy of the same numbers — this key
    carries only what genuinely has no other home.

    **What the key-level separation from ``_compact_cloud_status`` does and
    does not buy (review S-1 correction, 2026-07-27).** Both projections ride
    the SAME returned dict (:func:`crossover_v2_status_block`) and therefore
    the same HTTP response — ``/correction/crossover/status`` and this
    module's envelope both carry ``cloud`` AND ``cloud_chart`` together, so
    splitting the KEY does **not** shrink that response's byte count (an
    earlier version of this and two sibling comments overclaimed exactly
    that — "never pay" was wrong for this endpoint, which does carry both).
    What the split buys is narrower: a consumer that reads ONLY ``cloud`` —
    the doctor (:func:`~jasper.cli.doctor.correction.check_crossover_v2_cloud_pipeline`),
    and any future reader of the compact projection alone — never has to
    parse or skip over curve-shaped data mixed into that key's own shape.
    See :data:`CHART_CURVE_MAX_JSON_POINTS` above for the actual byte-cost
    mitigation (halving this key's own resolution), which is the fix that
    matters for the endpoint's total size.

    Same per-phase presence and ``None``-means-unavailable rules as
    :func:`_compact_cloud_status` (mirrored rather than shared because the two
    projections serve different consumers and have no other logic in common):
    a phase key is present whenever the durable block has one, ``curve`` is
    ``None`` until the pipeline becomes available, and it is never a
    fabricated empty curve.
    """
    if not isinstance(cloud_state, Mapping):
        return None
    out: dict[str, Any] = {}
    for phase, block in cloud_state.items():
        if not isinstance(block, Mapping):
            continue
        pipeline = block.get("pipeline")
        pipeline = pipeline if isinstance(pipeline, Mapping) else {}
        curve = None
        if pipeline.get("available") is True:
            raw_curve = pipeline.get("curve")
            if isinstance(raw_curve, Mapping):
                curve = _decimate_curve_for_chart(
                    raw_curve.get("freqs_hz"), raw_curve.get("magnitude_db"),
                )
        out[str(phase)] = {"curve": curve}
    return out or None


def _prediction_status(state: Any) -> dict[str, Any] | None:
    """The PREDICTED post-apply response and its stored spec verdict, or
    ``None`` (two-stage commission D4).

    Rides :func:`crossover_v2_status_block`'s returned dict beside ``cloud`` /
    ``cloud_chart``. Both halves were already computed — the curve by
    ``_decimate_sum`` at persist time, the verdict by the conductor's
    accountability veto against the FULL-RESOLUTION tuple — and neither reached
    any surface. This projects; it never grades.

    **Nothing renders it yet.** It is the wire half of the two-stage flow's
    review screen (PR-T2's "what we predict" panel and the chart's third
    curve), landed on its own rung so that screen is built against data already
    proven on the wire rather than against a shape invented alongside it.

    **``curve`` and ``spec`` are independently absent, and all four
    combinations are reachable.** Enumerated because a consumer — PR-T2's
    review screen above all — has to render each one differently:

    1. *Both present* — the ordinary closed session. Draw the curve, state the
       verdict.
    2. *Curve, no report* — a state written before D4, or a prediction the
       evaluator refused (:func:`~jasper.active_speaker.crossover_v2_flow
       .spec_report_for_predicted_sum` returned ``None``). Draw the curve, say
       the verdict is unknown; **do not** infer one from the picture.
    3. *Neither* — no session has closed a candidate. This function returns
       ``None`` outright rather than an empty shell.
    4. *Report, no curve* — **the refusal lane, and the least obvious of the
       four.** The verdict is stashed by ``_assert_accountable`` BEFORE the
       improvement gate runs, while ``_measure_predicted_sum`` is assigned only
       after that gate returns — so a ``correction_not_an_improvement`` refusal
       persists the report with ``predicted_sum`` still ``None`` (the
       ``CaptureBeginRefused`` arm's terminal-failure persist). This is honest,
       not a leak: the spec verdict genuinely evaluated that prediction, and
       what the gate refused on was a *different* question — insufficient
       improvement over the correction's own pre-fit model. A consumer shows
       the verdict and has no curve to draw.

    So ``overall_passed`` is ``None`` — not ``False`` — whenever no report was
    stored, under the same never-fabricate-a-clean-reading rule
    :func:`_compact_cloud_status` states at length. ``None`` here means
    "unknown", and a consumer must not read it as permission. ``False`` is the
    opposite: a real graded verdict that the prediction misses the spec, which
    is exactly what state 4 carries.

    ``spec_bands`` / ``reference_db`` mirror the compact cloud block's own
    vocabulary key-for-key on purpose: the review screen draws the measured
    curve and this one in ONE deviation frame with one tolerance corridor, and
    a second spelling of the same five per-band numbers is how the two frames
    would drift apart.
    """
    priors = (state or {}).get("verify_priors")
    if not isinstance(priors, Mapping):
        return None
    raw_curve = priors.get("predicted_sum")
    curve = (
        _decimate_curve_for_chart(
            raw_curve.get("freqs_hz"), raw_curve.get("magnitude_db"),
        )
        if isinstance(raw_curve, Mapping)
        else None
    )
    spec = priors.get("predicted_spec")
    spec = spec if isinstance(spec, Mapping) else {}
    if curve is None and not spec:
        return None
    bands = spec.get("bands")
    return {
        "curve": curve,
        "spec_bands": [
            {
                "f_lo_hz": b.get("f_lo_hz"),
                "f_hi_hz": b.get("f_hi_hz"),
                "passed": b.get("passed"),
                "max_deviation_db": b.get("max_deviation_db"),
                "tolerance_db": b.get("tolerance_db"),
            }
            for b in bands
            if isinstance(b, Mapping)
        ] if isinstance(bands, list) else [],
        "overall_passed": (
            spec.get("overall_passed")
            if isinstance(spec.get("overall_passed"), bool)
            else None
        ),
        "reference_db": _finite(spec.get("reference_db")),
    }


def crossover_v2_status_block() -> dict[str, Any] | None:
    """The ``status["crossover_v2"]`` block.

    ``needs_recovery`` comes from the SessionVolumePlan (the W2 gate ruling:
    key on ``needs_recovery``, never ``unresolved_volume_safety`` alone — a
    crash-hydrated active plan surfaces no unresolved payload but still needs
    draining before a new session).
    """
    state = load_v2_state()
    session_id = (state or {}).get("session_id")
    attempts = (state or {}).get("attempts_loop")
    attempts = attempts if isinstance(attempts, Mapping) else {}
    last_attempt_decision = attempts.get("last_decision")
    # Count is derived from its persistence owner on every state read. Keeping
    # a second copy in journey state made crash recovery and offline store
    # repair observable as two contradictory counts.
    store_count = _attempt_loop_store_snapshot().model_error_count
    try:
        needs_recovery = bool(session_volume_plan().needs_recovery)
    except (OSError, RuntimeError, ValueError):
        needs_recovery = True  # unreadable volume state fails closed
    block: dict[str, Any] = {
        "phase": _phase_from_state(state),
        # The commission tier behind whatever this block reports, or ``None``
        # when the durable state does not say (pre-tier state, or a session
        # that declared none). Never defaulted to "full" — see
        # ``persist_conductor_state``.
        "tier": (str((state or {}).get("tier") or "") or None),
        # Which sub-moment of the measuring session's tail this is, when
        # ``phase`` is ``closing`` (two-stage D1). ``""`` everywhere else.
        "cloud_close": str((state or {}).get("cloud_close") or ""),
        "candidate": (state or {}).get("candidate"),
        # MEASURE's own verdict-time disclosures — today just G1's ripple
        # reservation (#2087). Copied through unvalidated, exactly like
        # ``candidate`` and ``verify`` beside it: the envelope's own accessor
        # is the validating reader, so a state file written by another build
        # cannot 500 this poll path.
        "measure": (state or {}).get("measure"),
        "verify": (state or {}).get("verify"),
        "failure": (state or {}).get("failure"),
        "apply_blocked": (state or {}).get("apply_blocked"),
        "needs_recovery": needs_recovery,
        "applied": bool(state and state.get("applied")),
        "session_id": session_id,
        # Minimal live-loop observability: no attempt curves/history on the
        # household polling path, only the kernel output the envelope formats
        # and the durable model-error record count.
        "attempts_loop": {
            "last_decision": (
                dict(last_attempt_decision)
                if isinstance(last_attempt_decision, Mapping) else None
            ),
            "store_count": store_count,
        },
        # Flat-linearization plan PR-4: the compact per-group honesty
        # verdict. ``None`` when no group has closed yet — never a fabricated
        # "clean" reading (mirrors every other honesty-instrument field's own
        # "not yet run" rule). ``current_session_id`` lets the compact block
        # tell a fresh group apart from one carried forward from an earlier
        # session (PR-7's provenance marker) — see ``_cloud_summary``'s and
        # ``_compact_cloud_status``'s own comments.
        "cloud": _compact_cloud_status(
            (state or {}).get("cloud"), current_session_id=session_id,
        ),
        # PR-7: the chart-only curve feed, kept off the ``cloud`` key above
        # so the doctor (which reads only ``cloud``) never has to parse
        # curve-shaped data mixed into it. This DOES ride the same returned
        # dict — and so the same HTTP response — as ``cloud``; see
        # _chart_cloud_status's own docstring for the measured byte cost and
        # its own re-decimation ceiling, which is the actual size mitigation.
        "cloud_chart": _chart_cloud_status((state or {}).get("cloud")),
        # Two-stage commission D4: the PREDICTED post-apply response and the
        # spec verdict the accountability veto graded it with. It rides beside
        # ``cloud``/``cloud_chart`` because the review screen (PR-T2 — no
        # consumer yet) will draw all three in one frame — measured, proposed,
        # predicted — and a household
        # deciding whether to apply is comparing exactly those. Kept as ONE key
        # rather than split compact/chart the way ``cloud`` is: that split
        # exists because the doctor reads ``cloud`` alone and should not have
        # to skip curve-shaped data, and nothing reads the prediction that way.
        # ``None`` until a candidate's close has stored a prediction.
        "prediction": _prediction_status(state),
        # WO-1's read half (CC1): what this speaker's measurement LEARNED, in
        # the one register a household may read. ``[]`` means "nothing was
        # banked" — never "nothing was looked for", which is the store's own
        # absent-vs-empty distinction and stays where it belongs, in the
        # bundle artifact.
        "findings": _household_findings_status(state),
    }
    block["post_apply_grade"] = _post_apply_grade(block)
    return block


def _household_findings_status(state: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """The banked findings a household may read, from the durable projection.

    Reads what :func:`_bank_household_findings` wrote — never the bundle, and
    never ``os``-anything: this runs on every wizard poll, and the read that
    costs (reopen + re-hash the artifact and its citation) already happened
    once, at publish.

    **Validated, not trusted.** The state file is JSON written by some build,
    possibly an older or newer one, so every row is checked rather than passed
    through: a row without usable copy is DROPPED (an empty or non-string
    sentence is not a finding a household can read), and an unusable ``at``
    becomes ``None`` — which the envelope renders as "we cannot say when",
    exactly as an undated failure record does. Fabricating neither a sentence
    nor a date is the whole contract here, and it is pinned at THIS layer
    (``tests/test_correction_crossover_v2_endpoints.py``'s projection-contract
    tests) rather than only through the envelope: a weakened copy check here —
    ``str(row.get("household_copy") or "")`` — renders a fabricated ``"42"`` on
    the done screen end to end, and every screen-level assertion stays green
    while it does.

    Never raises. ``float`` on an unbounded JSON integer raises
    ``OverflowError`` rather than returning ``inf``, and this runs on the
    wizard's 1.5 s poll path, so an escaping conversion would be a 500 on a
    plain page load — the same failure ``_record_when_phrase`` catches one
    module over for the same reason.
    """
    evidence = (state or {}).get("evidence")
    rows = (
        evidence.get(FINDING_HOUSEHOLD_REFS_KEY)
        if isinstance(evidence, Mapping)
        else None
    )
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        copy = row.get("household_copy")
        if not isinstance(copy, str) or not copy.strip():
            continue
        at = row.get("at")
        try:
            stamp = (
                float(at)
                if isinstance(at, (int, float)) and not isinstance(at, bool)
                else None
            )
        except (TypeError, ValueError, OverflowError):
            stamp = None
        if stamp is not None and not math.isfinite(stamp):
            stamp = None
        out.append({"household_copy": copy, "at": stamp})
    return out


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
    express session ever run. Undo already exists, is the PRIMARY button on the
    done screen, and is the household's call. What was missing is being told.

    The returned ``state`` is one of the ``GRADE_*`` constants above;
    ``graded`` is the single boolean a caller can key on without knowing the
    vocabulary. Both a passing VERIFY outcome and a graded post-apply cloud
    count — either instrument is a real check, and the tiers differ in which
    one they run.
    """
    from jasper.active_speaker.crossover_v2_flow import PHASE_CLOUD_VERIFY

    if not block.get("applied"):
        return {"state": GRADE_NOT_APPLIED, "graded": True, "verify_outcome": None}
    verify = block.get("verify")
    outcome = str((verify or {}).get("outcome") or "") if isinstance(verify, Mapping) else ""
    cloud = block.get("cloud")
    post_apply = cloud.get(PHASE_CLOUD_VERIFY) if isinstance(cloud, Mapping) else None
    cloud_verdict = (
        post_apply.get("overall_passed") if isinstance(post_apply, Mapping) else None
    )
    if isinstance(cloud_verdict, bool):
        # A walked post-apply position group — the widest claim available.
        state = GRADE_GRADED
    elif outcome == "pass":
        # Verified at the mark only. On express that is the whole grade by
        # design; on full it means VERIFY passed but the post-apply group has
        # not closed yet.
        state = GRADE_MARK_VERIFIED
    elif outcome == "inconclusive":
        state = GRADE_INCONCLUSIVE
    elif outcome == "fail":
        state = GRADE_FAILED
    else:
        state = GRADE_UNVERIFIED
    return {
        "state": state,
        "graded": state in {GRADE_GRADED, GRADE_MARK_VERIFIED},
        "verify_outcome": outcome or None,
        "post_apply_spec_passed": cloud_verdict if isinstance(cloud_verdict, bool) else None,
    }


# --------------------------------------------------------------------------- #
# conductor persistence
# --------------------------------------------------------------------------- #


def _decimate_sum(predicted_sum: Any) -> dict[str, Any] | None:
    """Persist-time reduction of the full-resolution predicted-sum curve to
    at most :data:`MAX_PERSISTED_SUM_POINTS` (the ``verify_priors.
    predicted_sum`` this module writes at :func:`persist_conductor_state`).

    **Issue #1858 — was a raw ``freqs[::step]`` stride, which aliases.**
    Picking one raw bin per output point keeps whichever bin the stride
    happened to land on; below ~600 Hz the 46.875 Hz stride spacing leaves
    fewer than 3 samples in a 1/3-octave band (below ~200 Hz it exceeds the
    band's own width outright — zero guaranteed samples), so the persisted
    LF shape was noise, not signal (point-to-point |Δ| median 0.39 dB vs.
    the properly-decimated cloud curve's 0.06 dB). Fixed by routing through
    the SAME block-average owner
    :func:`~jasper.active_speaker.crossover_v2_flow.spec_report_for_predicted_sum`
    already uses to grade this exact curve —
    :func:`~jasper.audio_measurement.spatial_combine.decimate_curve_to_analysis_grid`
    — at this module's own ceiling rather than the analysis-grid one. That
    function block-averages in linear power (never subsamples), so every
    persisted point is a genuine local mean instead of one raw bin —
    generalizing the existing owner with a ``max_bins`` argument rather than
    adding a second decimation path.

    Era note: a state file persisted by a build before this fix carries
    whatever its own stride wrote; this only changes what a NEW persist
    writes; :func:`_predicted_spec_prior`'s stored verdict is untouched
    either way, since D4 grades the full-resolution tuple, never this
    decimated curve
    (see ``test_the_persisted_prediction_verdict_is_the_veto_s_not_a_re_grade``).
    **One path re-persists an old curve, not just new ones:** a verify-only
    re-arm (:func:`prepare_v2_verify`) rehydrates ``predicted_sum`` from
    whatever is on disk and feeds it straight back through this function at
    its own persist step, so a pre-fix 513-point stride, encountered this
    way, is itself now block-averaged again — 513 → 256 at 93.75 Hz spacing
    (halved, not preserved) — where the old code would have left an
    already-persisted curve untouched on that path. Still honest values,
    just coarser than a fresh full-resolution persist would produce; the
    household's next MEASURE naturally replaces it.
    """
    if predicted_sum is None:
        return None
    freqs, mags = predicted_sum
    n = len(freqs)
    if n == 0:
        return None
    import numpy as np

    from jasper.audio_measurement.spatial_combine import (
        decimate_curve_to_analysis_grid,
    )

    grid, curve_db = decimate_curve_to_analysis_grid(
        np.asarray(freqs, dtype=float), np.asarray(mags, dtype=float),
        max_bins=MAX_PERSISTED_SUM_POINTS,
    )
    return {
        "freqs_hz": [float(f) for f in grid],
        "magnitude_db": [float(m) for m in curve_db],
    }


def _predicted_spec_prior(conductor: Any) -> dict[str, Any] | None:
    """The conductor's stored prediction verdict, in the shape the durable
    state carries it (two-stage commission D4).

    ``getattr`` rather than attribute access for the same reason
    :func:`_cloud_summary` guards its own reads: this persistence helper is
    called from test seams with conductor doubles that carry only the surfaces
    a given test exercises, and a missing property must read as "no verdict",
    not raise mid-persist and lose the whole snapshot.
    """
    report = getattr(conductor, "measure_predicted_spec_report", None)
    return dict(report) if isinstance(report, Mapping) else None


def pilot_transfer_prior_from_state(
    state: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    """The PREVIOUS session's G3 reference, as durable state carries it (#1927).

    The read side of :func:`persist_conductor_state`'s
    ``verify_priors.pilot_transfer_reference`` — the mirror of
    :func:`_predicted_spec_prior` above, and the whole of what
    ``prepare_v2_verify`` seeds a fresh conductor's *history* with.

    Named and module-level so the seeding PATH is drivable in a test without a
    relay: durable state in, the ctor's ``verify_pilot_transfer_prior``
    argument out. Whether that argument can ever become a comparator is the
    conductor's own contract (it cannot — there is no longer an argument that
    seeds one).

    Shape checking beyond "is it a mapping" belongs to the conductor, which
    owns the "values plus a date, or nothing" rule.
    """
    priors = (state or {}).get("verify_priors")
    prior = priors.get("pilot_transfer_reference") if isinstance(priors, Mapping) else None
    return prior if isinstance(prior, Mapping) else None


def _finite(value: Any) -> float | None:
    """Mirrors ``crossover_envelope_v2._finite``'s exact guard (reject
    bool, reject non-numeric, reject NaN/inf) — N1 (2026-07-24 review
    follow-up): this module and that one stay symmetric about what counts
    as a displayable number, rather than one layer trusting a raw
    ``float(v)`` the other layer would refuse."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if number == number and abs(number) != float("inf") else None


def _candidate_headroom_cost_db(linearization: Any) -> float:
    """The applied correction's disclosed max-level cost, dB (PR-L5).

    Thin adapter over the fit module's own reducer — defined there once so this
    browser payload and the conductor's cannot disagree about a
    household-facing number.
    """
    from jasper.active_speaker.linearization_fit import worst_headroom_cost_db

    if not isinstance(linearization, Mapping):
        return 0.0
    return worst_headroom_cost_db(linearization)


def _candidate_octave_summary(linearization: Any) -> dict[str, dict[str, float]]:
    """Gauge fix (2026-07-24): per-role OBSERVE-layer octave deficits
    (``LinearizationFit.observe_octave_summary`` — already computed by the
    fit engine, achieved-minus-target dB at each octave center), read
    straight off the live candidate's own rich ``linearization`` dict. Empty
    for a role whose fit never ran (ineligible/fit_failed/no fit) — nothing
    to disclose.

    A pure projection: this reads the fit's numbers and re-keys them, it has
    never derived a curve of its own. What the flat-linearization plan's PR-5
    changed is the FRAME these travel under — they are per-driver fit
    diagnostics from the design-axis capture, not the spec measurement, and
    ``crossover_envelope_v2._linearization_octave_rows`` (which renders them)
    carries the full reasoning."""
    out: dict[str, dict[str, float]] = {}
    for role, fit in (linearization or {}).items():
        if not isinstance(fit, Mapping):
            continue
        octaves = fit.get("observe_octave_summary")
        if not isinstance(octaves, Mapping) or not octaves:
            continue
        role_octaves: dict[str, float] = {}
        for hz, value in octaves.items():
            db = _finite(value)
            if db is not None:
                role_octaves[str(hz)] = db
        if role_octaves:
            out[str(role)] = role_octaves
    return out


def _candidate_summary(candidate: Any) -> dict[str, Any] | None:
    # Lazy, like ``_candidate_headroom_cost_db``'s own import below it: this
    # module has no module-level numpy and the fit module does, so the
    # socket-activated wizard process only pays for it on a path that
    # genuinely has a candidate.
    from jasper.active_speaker.linearization_fit import (
        HEADROOM_COST_BASIS_REALIZED_PEAK,
    )

    if candidate is None:
        return None
    analysis = candidate.analysis if isinstance(candidate.analysis, Mapping) else {}
    return {
        "fingerprint": candidate.fingerprint,
        "program_id": candidate.program_id,
        "trims_db": dict(candidate.role_attenuations_db),
        "alignment": candidate.alignment.to_dict(),
        # Threaded through for the conductor's own trust gate
        # (crossover_v2_flow.ALIGNMENT_CONFIDENCE_TRUST_FLOOR) and, on the
        # RESULT screen, the collapsed expert disclosure.
        "alignment_confidence": analysis.get("alignment_confidence"),
        # RESULT-screen expert disclosure only (crossover_envelope_v2
        # ._candidate_review_payload's "ripple_db").
        "predicted_ripple_db": analysis.get("predicted_ripple_db"),
        # Gauge fix (2026-07-24): WHY Layer-1a driver linearization did or
        # didn't run this attempt — "" / "fitted" / "trim_rejected" /
        # "ineligible_mic_tier" / "ineligible_repeats" / "fit_failed".
        "linearization_outcome": str(
            getattr(candidate, "linearization_outcome", "") or ""
        ),
        # Gauge fix (2026-07-24): per-role top-octave deficits (the number
        # that says "the top octave is 9 dB down and nothing corrected it").
        "linearization_octaves": _candidate_octave_summary(candidate.linearization),
        # "This correction costs N dB of maximum level" (linearization-integrity
        # PR-L5). The owner's ruling on boost is that headroom spend is
        # DISCLOSED, never silently limited — and a number that only reaches the
        # journal is not disclosed to the household that owns the speaker. This
        # is the DURABLE candidate block (``/state.crossover_v2.candidate``);
        # the envelope's screens read
        # ``crossover_envelope_v2._candidate_review_payload``, which projects
        # this field into ``headroom_cost`` alongside the era stamp below. So
        # persisting it here is the first half of making the ruling true — that
        # projection is the half a household actually sees.
        #
        # The WORST branch's charge, matching the emitter's own worst-branch
        # rule (``camilla_yaml.linearization_headroom_db``): the driver chains
        # run in parallel after the split, so the graph gives up the largest
        # branch's charge, not the sum across branches. (Each branch's charge
        # is its emitted chain's realized peak since #1808.) 0.0 for every cut-only
        # correction, which is every correction before PR-L5 — present and
        # zero rather than absent, so a surface never has to guess whether the
        # field is missing or the cost is nothing.
        "headroom_cost_db": _candidate_headroom_cost_db(candidate.linearization),
        # WHICH derivation the number above was stamped under (#1808 /
        # two-stage commission D3). Written unconditionally here because a
        # candidate reaching this function was built by THIS process, so its
        # per-fit charges are this build's realized-peak rule by construction.
        # A candidate persisted by an older build has no such key, and its
        # absence is the only honest evidence of era there is — see
        # ``linearization_fit.HEADROOM_COST_BASIS_*`` for why it is recorded
        # rather than sniffed, and ``crossover_envelope_v2
        # ._candidate_review_payload`` for what an absent basis renders as.
        "headroom_cost_basis": HEADROOM_COST_BASIS_REALIZED_PEAK,
    }


def _cloud_summary(conductor: Any) -> dict[str, Any] | None:
    """Per-group geometry verdict + position ids, or ``None`` when no group ran.

    Reads the conductor's public group surfaces only, and tolerates a conductor
    double that has none (the persistence helper is called from test seams too).
    """
    from jasper.active_speaker.crossover_v2_flow import GROUP_PHASES

    try:
        session_phases = tuple(conductor.session_phases)
    except (AttributeError, TypeError):
        return None
    out: dict[str, Any] = {}
    for phase in session_phases:
        if phase not in GROUP_PHASES:
            continue
        geometry = conductor.group_geometry(phase)
        if geometry is None:
            continue
        out[phase] = {
            "geometry": geometry,
            # The SURVIVING take per position (id + attempt). A bare id list
            # would be ambiguous after a geometry retake, where two takes share
            # an id and only one is in the cloud; the attempt is what joins
            # this to the per-take evidence artifacts.
            "positions": list(conductor.group_position_takes(phase)),
            # PR-4: the honest-instrument pipeline result for this group —
            # merged mask, null registry, evaluated spec, geometry guidance
            # copy, decimated curve (assemble_cloud_group_result's own JSON
            # shape, so this is verbatim what the bundle artifact carries
            # too). ``None`` only if the conductor double has no such method
            # (a pre-PR-4 test seam) — never "the pipeline was fine".
            "pipeline": (
                conductor.group_cloud_result(phase)
                if hasattr(conductor, "group_cloud_result")
                else None
            ),
            # PR-7: the PRODUCING session's id, stamped once here (not
            # re-derived downstream) — the provenance marker
            # ``_compact_cloud_status`` needs to tell "measured in the
            # currently active session" apart from "carried forward from an
            # earlier one". Carried forward unconditionally by
            # ``persist_conductor_state``'s own carry-forward branch below,
            # which copies this whole per-phase dict verbatim — so a stamp
            # written here survives every re-arm that does not itself close
            # a fresh group, without a second write site. Guarded the same
            # way as ``pipeline`` above (review N-3): a conductor double
            # built only to exercise this function need not carry every
            # attribute a real one does, and ``_compact_cloud_status``
            # already treats a missing/non-string stamp as unknown
            # provenance, never a fabricated one.
            "session_id": (
                str(conductor.session_id) if hasattr(conductor, "session_id") else None
            ),
        }
    return out or None


def _delta_probe_summary(probe: Any) -> dict[str, Any]:
    """The delta probe's verdict, small enough to live in durable state (#1811).

    Everything a reader needs to judge a non-rollback finding — chiefly
    ``level_mismatch``, which by design produces no refusal and would otherwise
    be invisible outside the journal: what the verdict was, why, the level move
    the emitter declared, and the one it could not account for. The full map
    (per-bin errors, exceedance width, gain factor, spatial arm) stays on
    ``event=correction.crossover_v2_delta_probe`` — this is the durable
    summary, not a second copy of the record.
    """
    return {
        "verdict": str(getattr(probe, "verdict", "") or ""),
        "reason": str(getattr(probe, "reason", "") or ""),
        "expected_offset_db": getattr(probe, "expected_offset_db", 0.0),
        "residual_offset_db": getattr(probe, "residual_offset_db", None),
    }


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
    """
    from jasper.active_speaker.crossover_v2_flow import PHASE_MEASURE

    snap = conductor.snapshot()
    verify_outcome = conductor.verify_outcome
    # Read through ``getattr`` with a default because this function accepts
    # DUCK-TYPED conductors, not only the real class — the same reason
    # ``hasattr(snap, "attempt_history")`` reads defensively a few lines down.
    # Measured, not assumed: a direct attribute read fails 7 tests in
    # ``test_correction_crossover_v2_endpoints.py``, all of which persist a
    # stand-in that implements the fields their own assertion is about and
    # nothing else. An absent property is "nothing reserved", which is what the
    # key's own absence already means downstream.
    ripple_reservation = getattr(conductor, "measure_ripple_reservation", None)
    # Same duck-typed read, same reason: a stand-in conductor that predates
    # this field is "no pilot evidence", which is what the key's own absence
    # means downstream. See the ``failure`` block below for what it renders.
    #
    # GATED ON THE CODE BEING PERSISTED, not on the conductor's own. The
    # caller supplies ``failure_code``, and several terminal arms supply one
    # the capture loop never produced — the relay-death arm persists
    # ``relay_timeout`` over whatever the last capture failed on. Ungated,
    # ``_persist_terminal_failure(c, "relay_timeout")`` after a heard-speaker
    # ``locate_failed`` writes ``{"code": "relay_timeout", "pilot_heard":
    # True}``: one failure's code carrying another's evidence, which is the
    # exact mispairing ``_pilot_heard_for`` refuses on the conductor side.
    # No terminal arm renders a wrong sentence from that pair today (only
    # ``locate_failed`` reads the key), but the drift surface is introduced
    # here, so the check belongs here too.
    failure_pilot_heard = (
        getattr(conductor, "last_failure_pilot_heard", None)
        if failure_code == getattr(conductor, "last_failure_code", None)
        else None
    )
    prior = load_v2_state() or {}
    if hasattr(snap, "attempt_history"):
        attempts_loop_state: dict[str, Any] | None = {
            "history": [
                item.to_dict()
                for item in (getattr(snap, "attempt_history", ()) or ())
            ],
            "last_decision": (
                dict(getattr(snap, "last_attempt_decision"))
                if getattr(snap, "last_attempt_decision", None) is not None
                else None
            ),
        }
    else:
        prior_attempts = prior.get("attempts_loop")
        attempts_loop_state = (
            dict(prior_attempts) if isinstance(prior_attempts, Mapping) else None
        )
    state: dict[str, Any] = {
        "session_id": snap.session_id,
        "accepted_phases": list(snap.accepted_phases),
        # The phases THIS session runs — read by ``_phase_from_state`` so a
        # verify-only re-arm reaches "done" instead of waiting forever on a
        # position group it never had.
        "session_phases": list(snap.session_phases),
        # WHICH INSTRUMENT produced this state (flow-simplification §1.2).
        # Empty string means unknown — state written before tiers existed, or
        # a session that never declared one — and every reader must render it
        # as unknown rather than assuming "full": express makes no
        # cross-position post-apply claim at all, so guessing would attach a
        # claim the measurement never made (the same unknown-vs-default rule
        # ``echo_band_provenance`` carries, issue #1763).
        "tier": snap.tier,
        # WHERE the pre-apply cloud's close has got to (two-stage D1). The
        # wizard renders from this file alone, and "every stage-1 phase
        # accepted, no candidate" is true at three different moments — the
        # household holding a phone at the confirm screen, the fit running,
        # and a session that ended having produced nothing. Without this they
        # rendered as the third one, which offered to throw away a
        # measurement that was still in progress.
        "cloud_close": snap.cloud_close,
        "applied": snap.applied,
        "gain_plan_db": dict(snap.gain_plan_db) if snap.gain_plan_db else None,
        # S3 journey state. The conductor is the sole lifecycle owner and the
        # web host serializes its snapshot verbatim; `/state` below projects
        # only the last decision, never the full history. The store count is
        # read fresh from its persistence owner at the `/state` boundary.
        "attempts_loop": attempts_loop_state,
        "candidate": _candidate_summary(conductor.candidate),
        # What MEASURE accepted WITH A RESERVATION (owner ruling 2026-08-03,
        # issue #2087). Absent/`None` means the accepted capture had nothing to
        # reserve about — never "we did not check", because this key is written
        # on every persist by a session that runs MEASURE.
        #
        # Its own block rather than a key on ``candidate`` above: that summary
        # projects the candidate ARTIFACT's own fields, and a reservation is a
        # verdict-time judgement about the capture the artifact was built from.
        # Folding it in there would make one dict have two owners.
        "measure": (
            {"ripple_reservation": dict(ripple_reservation)}
            if ripple_reservation
            else None
        ),
        "verify": (
            {
                "outcome": verify_outcome,
                # WHICH VERDICT produced that outcome (issue #1974). "inconclusive"
                # is reached by two verdicts sharing no mechanism — a VERIFY gate
                # shorter than MEASURE's, and the recording chain moving between
                # attempts — and the done screen must name the right one long
                # after the terminal failure screen has aged out of the render
                # path. It is NOT read from ``failure.code`` below: that is the
                # most recent rejection of ANY phase, and a later persist with
                # ``failure_code=None`` nulls it while this outcome still stands,
                # so the two are only coincidentally equal. Written by the
                # conductor with the outcome in one call (``_set_verify_outcome``),
                # so the pair cannot disagree.
                **(
                    {"code": conductor.verify_code}
                    if conductor.verify_code
                    else {}
                ),
                # WHAT THE GATE DID, on EVERY outcome (issues #1974 / #1966).
                # Same shape and same argument as the frame and the graded band
                # below: the sentence is
                # ``gate_disclosure.describe_gate``'s, composed once at verdict
                # time and rendered verbatim — a record that prints a 7 ms
                # window and nothing else reads as "reflections removed" to
                # every consumer, and across the 2026-07-30 corpus it meant "no
                # reflection found; window capped". ``reflection_measured``
                # beside it is the one fact the household copy branches on.
                **(
                    {"gate": dict(conductor.verify_gate)}
                    if conductor.verify_gate
                    else {}
                ),
                # The verify_fail expert-disclosure numbers (#1605) — persisted
                # only for a NON-pass outcome (the only one that renders a
                # verify_fail screen). A pass shows the candidate_review card,
                # not these tracking numbers, so it keeps its lean shape.
                **(
                    {"evidence": dict(conductor.verify_evidence)}
                    if (verify_outcome != "pass" and conductor.verify_evidence)
                    else {}
                ),
                # WHAT SPAN was graded, on EVERY outcome including a pass
                # (#1868). This rode ``evidence`` above until now, i.e. it
                # reached a surface only once the verdict had already failed —
                # so the one screen that says "Verified." was the one screen
                # that never said over what. The band is not the nominal
                # Fc±1 octave: two clamps move its lower edge up, and on the
                # 2026-07-30 corpus it sat at [2000, 4000] Hz while the
                # crossover defect under investigation sat at 1919 Hz. Same
                # shape as ``delta_probe`` below, and for the same reason.
                **(
                    {"graded_band_hz": list(conductor.verify_graded_band_hz)}
                    if conductor.verify_graded_band_hz
                    else {}
                ),
                # WHAT FRAME the comparison spanned, on EVERY outcome (rung
                # P1). Same shape and same argument as the graded band above:
                # VERIFY differences an on-axis MODEL against an in-room
                # MEASUREMENT, and on the 2026-07-29 corpus a single
                # −0.79 dB/oct tilt between those two frames was 84% of the
                # flow's apparent prediction error. The raw numbers are
                # untouched — this says how much of them was the instrument,
                # and a pass is exactly when nobody would otherwise ask.
                **(
                    {"frame": dict(conductor.verify_frame)}
                    if conductor.verify_frame
                    else {}
                ),
                # The level-reference reset this session performed, when the
                # previous session's reference differed enough to be worth
                # saying (#1927). Same every-outcome shape as the graded band
                # above: a pass is exactly when an unstated reset would let a
                # household read cross-day identity into a same-session claim.
                # Absent means there was nothing to disclose, never "we did not
                # reset" — the reset is now unconditional.
                **(
                    {"level_reference": dict(conductor.verify_level_reference_reset)}
                    if conductor.verify_level_reference_reset
                    else {}
                ),
                # (A "flatness" key lived here until the flat-linearization
                # plan's PR-5, carrying the retired per-VERIFY-capture
                # construction. It is gone rather than repointed: the spec
                # verdict is a property of the CLOUD, so it belongs in the
                # "cloud" block below and nowhere else — a second copy under
                # "verify" is exactly the duplicated-frame shape PR-5 exists
                # to remove. A state file written by an older build may still
                # carry the stale key; nothing reads it.)
                #
                # The delta probe's verdict, on EVERY outcome including a pass
                # (#1811). A non-rollback non-matched verdict — today only
                # ``level_mismatch`` — otherwise reached no surface at all: the
                # refusal path ignores it by design, so the household saw a
                # clean "Verified." over a shape question the probe never got
                # to answer. Four scalars, not the whole map: the verdict, why,
                # and the two level numbers a reader needs to judge it. The
                # full record stays in the journal.
                **(
                    {"delta_probe": _delta_probe_summary(conductor.delta_probe)}
                    if getattr(conductor, "delta_probe", None) is not None
                    else {}
                ),
            }
            if verify_outcome is not None else None
        ),
        "failure": (
            {
                "code": failure_code,
                # WHEN this failure happened (issue #1942). Without it the
                # envelope could not tell a failure the household is looking
                # at right now from one a previous day's session left behind,
                # so it re-rendered the terminal screen — with that session's
                # verify numbers — on every later page load, forever.
                #
                # The file-level ``updated_at`` cannot answer this: it is
                # last-write-of-ANYTHING, so it moves for reasons that have
                # nothing to do with the failure. This is the failure's own
                # clock, and it belongs on the failure's own record.
                #
                # Epoch float, deliberately the same type and clock as
                # ``save_v2_state``'s ``updated_at`` one level up — an age is
                # then a subtraction, with no format to parse and no second
                # time representation in one file.
                #
                # Stamped at write, not carried forward, because every writer
                # is an in-session capture-loop event: a failing session may
                # persist the same code twice (the rejecting capture, then the
                # plan-finished write) seconds apart, and NO read path
                # persists — ``handle_status`` never writes — so nothing can
                # refresh this while a household stares at the screen.
                "at": time.time(),
                **(
                    {"refusals": [str(slug) for slug in failure_refusals]}
                    if failure_refusals else {}
                ),
                # WHAT THE CAPTURE MEASURED about the speaker being audible —
                # ``locate_failed``'s copy branches on it (#2085), so the
                # envelope cannot render this failure's honest sentence
                # without it. Unlike ``refusals`` above this is NOT forensics:
                # it reaches the household, through
                # ``crossover_envelope_v2._reason_message``.
                #
                # Present only when established. Absent and ``False`` mean
                # different things and both are already handled downstream —
                # absent is "no pilot evidence" (every failure that ran no
                # capture, plus every state file written before this shipped),
                # ``False`` is "the pilot was measured and did not clear the
                # room". Writing a bare ``False`` for the unknown case would
                # turn a missing measurement into a claim about the room.
                #
                **(
                    {"pilot_heard": bool(failure_pilot_heard)}
                    if failure_pilot_heard is not None else {}
                ),
            }
            if failure_code else None
        ),
        # Position-group outcome (PR-3b): the closing geometry verdict and the
        # position ids behind it, per group. Present only for groups that have
        # CLOSED — an absent key means "still walking", never "geometry was
        # fine". PR-4 adds the rest of the honest-instrument output (exclusion
        # screen, null registry, spec curve) alongside this block; the geometry
        # verdict is what PR-3b measured and so what PR-3b persists.
        "cloud": _cloud_summary(conductor),
        "verify_priors": {
            "predicted_sum": _decimate_sum(conductor.measure_predicted_sum),
            # Two-stage commission D4: the spec verdict for the curve above,
            # graded ONCE by the conductor's accountability veto against the
            # full-resolution tuple — this is a copy of that one report, never
            # a re-grade of the decimation the line above just wrote.
            # ``None`` means ungradeable, which is not a pass.
            #
            # Threaded through exactly like ``gate_window_ms`` below, and
            # rehydrated by ``prepare_v2_verify`` for the same reason: a
            # verify-only re-arm builds a fresh conductor that never runs a
            # fit, so a verdict that did not travel that route would be
            # dropped on the first "Try again" — the ``cloud`` B1 bug shape,
            # one field over.
            "predicted_spec": _predicted_spec_prior(conductor),
            "gate_window_ms": conductor.measure_gate_window_ms,
            # Measurement-honesty gate G3's reference, DATED — history, not a
            # comparator (#1927). ``prepare_v2_verify`` hands it to the next
            # conductor as ``verify_pilot_transfer_prior``, which may only
            # disclose it; the constructor argument that used to make it that
            # session's live baseline is gone. Carried forward below when this
            # session set no reference of its own, so a re-arm that dies before
            # its first usable VERIFY attempt does not erase the history.
            #
            # The retired flat ``pilot_transfer_baseline`` key is deliberately
            # NOT read anywhere any more: it carried no date, so it can be
            # neither compared against (the ruling) nor shown as history
            # (#1942). No migration is needed — this whole ``verify_priors``
            # dict is rebuilt on every persist, so an older build's key is
            # inert until the first write of the next session drops it.
            "pilot_transfer_reference": conductor.verify_pilot_transfer_reference,
        },
        "evidence": dict(evidence) if evidence else None,
    }
    # A conductor that declares no tier of its own — the verify-only re-arm
    # (``prepare_v2_verify``), which re-runs one tracking capture against an
    # ALREADY-applied result — must not erase which instrument produced that
    # result. Carried forward unconditionally, exactly like ``cloud`` below
    # and for the same reason: the re-arm runs under a brand-new relay session
    # id, so a session-scoped guard would drop it on the first "Try again".
    if not state["tier"] and prior.get("tier"):
        state["tier"] = str(prior["tier"])
    # G3's dated reference (#1927) carries forward across the writes of a
    # VERIFY-ONLY session, and is dropped by any session that MEASURES.
    #
    # Carried, because every capture of a verify session persists and the
    # first writes run BEFORE any usable VERIFY attempt has set this session's
    # own reference: without this the opening write of a re-arm would blank
    # the history the disclosure reads, and a session that died before its
    # first tone would erase it for good. Same shape as ``tier`` above — the
    # re-arm runs under a brand-new relay session id, so a session-id guard is
    # the wrong one (the ``cloud`` B1 bug, one field over).
    #
    # Dropped by a measuring session, because a pilot transfer is captured
    # THROUGH the applied graph: once a new candidate is applied the two
    # numbers answer different questions, and a disclosure computed across
    # that boundary would report a graph change as a level-reference move.
    #
    # State the predicate honestly: this tests "does THIS SESSION'S PLAN
    # contain MEASURE", which is COARSER than "the graph changed". A session
    # that measures and never applies — a refused candidate, an abandoned
    # walk — drops the history even though nothing moved. That is the
    # fail-silent direction and the one to be coarse in: the cost is a
    # disclosure that goes unsaid, against a disclosure that says something
    # untrue. Binding this to the applied candidate's fingerprint instead
    # would be exact, and is deliberately not built for a report-only line.
    if PHASE_MEASURE in snap.session_phases:
        state["verify_priors"]["pilot_transfer_reference"] = None
    elif state["verify_priors"]["pilot_transfer_reference"] is None:
        prior_reference = (prior.get("verify_priors") or {}).get(
            "pilot_transfer_reference"
        )
        if isinstance(prior_reference, Mapping):
            state["verify_priors"]["pilot_transfer_reference"] = dict(prior_reference)
    # The applied flag is host-durable (set by the apply endpoint) — never
    # regressed by a conductor snapshot that predates it.
    if prior.get("applied") is True and prior.get("session_id") == snap.session_id:
        state["applied"] = True
    if state["candidate"] is None and isinstance(prior.get("candidate"), Mapping):
        # A verify-only re-arm mints a new relay session around the already-
        # applied candidate. Its conductor intentionally has no candidate
        # object of its own, but the fingerprint is the attempts loop's stable
        # write identity; erasing it here turns recovery into a second record.
        # A measuring session still keeps the old session-scoped rule so a new
        # journey cannot inherit a stale candidate before it builds its own.
        if (
            prior.get("session_id") == snap.session_id
            or (
                prior.get("applied") is True
                and PHASE_MEASURE not in snap.session_phases
            )
        ):
            state["candidate"] = dict(prior["candidate"])
    if state["evidence"] is None and isinstance(prior.get("evidence"), Mapping):
        if prior.get("session_id") == snap.session_id:
            state["evidence"] = dict(prior["evidence"])
    # B1 fix (flat-linearization plan PR-4 review, 2026-07-26): ``cloud``
    # carries the SAME session-id-gated shape as ``candidate``/``evidence``
    # above, which is the WRONG guard for it — a verify-only re-arm's
    # conductor (``prepare_v2_verify``'s ``index_phase_map={1: PHASE_VERIFY}``)
    # has NO group phase in ITS OWN session, so ``_cloud_summary`` always
    # returns ``None`` for it: not because nothing closed, but because there
    # is nothing to close in this session. A session-id gate would never
    # carry the prior cloud verdict forward — exactly the same shape of bug
    # ``pre_apply_profile``'s own comment below documents ("the deferred
    # VERIFY that auto-arms right after every apply runs under a BRAND-NEW
    # relay session id"), and it hit on the very first tap of "Try again"
    # (the PRIMARY next_action after a failed verify): the cloud verdict a
    # household had just walked a session for went blank on `/state`, the
    # envelope, and the doctor's read, all three at once.
    #
    # Carry ``cloud`` forward UNCONDITIONALLY whenever THIS conductor's own
    # session has no group phase to report on — mirroring
    # ``pre_apply_profile``'s unconditional carry-forward below, not
    # ``candidate``/``evidence``'s session-scoped one above, which is the
    # wrong shape for this path. A conductor that DOES have a group phase in
    # its own session is left alone: ``_cloud_summary``'s own ``None`` there
    # honestly means "this session's group has not closed yet" and must not
    # be papered over with a stale prior verdict.
    from jasper.active_speaker.crossover_v2_flow import GROUP_PHASES, PHASE_MEASURE

    conductor_session_phases = set(getattr(conductor, "session_phases", ()) or ())
    if not (conductor_session_phases & GROUP_PHASES):
        if state["cloud"] is None and isinstance(prior.get("cloud"), Mapping):
            state["cloud"] = dict(prior["cloud"])
        # The cloud bundle-artifact fingerprints ride inside `evidence`
        # (`refs["cloud_artifacts"]`) — a group-phase-less session's own
        # `publish_cloud` seam is never wired (nothing to publish), so its
        # own `evidence` dict never carries this key forward on its own.
        # Restore it from prior rather than losing it to whatever this
        # session's own evidence looks like.
        prior_evidence = prior.get("evidence")
        if (
            isinstance(prior_evidence, Mapping)
            and "cloud_artifacts" in prior_evidence
        ):
            merged_evidence = dict(state["evidence"] or {})
            merged_evidence.setdefault(
                "cloud_artifacts", prior_evidence["cloud_artifacts"]
            )
            state["evidence"] = merged_evidence
    # The household-readable findings projection (CC1), carried forward on its
    # OWN predicate rather than the group-phase one above. A finding is banked
    # by the fit, which runs in MEASURE; a session that does not run MEASURE
    # never had the chance to produce one, so its empty projection is a
    # timestamp rather than a verdict — the same distinction ``cloud``'s carry
    # forward draws one block up, keyed on the phase that actually produces the
    # value. This is what puts the measuring session's finding on the DONE
    # screen: stage 2 is a different session in a different bundle
    # (``prepare_v2_verify`` opens a new one), and without this the result
    # screen would silently lose what the measurement learned.
    #
    # The converse matters just as much: a session that DOES run MEASURE writes
    # its own projection, empty included, so a fresh measurement that banks
    # nothing clears a previous session's finding instead of replaying it.
    if PHASE_MEASURE not in conductor_session_phases:
        prior_evidence = prior.get("evidence")
        if (
            isinstance(prior_evidence, Mapping)
            and FINDING_HOUSEHOLD_REFS_KEY in prior_evidence
        ):
            merged_evidence = dict(state["evidence"] or {})
            merged_evidence.setdefault(
                FINDING_HOUSEHOLD_REFS_KEY,
                prior_evidence[FINDING_HOUSEHOLD_REFS_KEY],
            )
            state["evidence"] = merged_evidence
        # G1's reservation (#2087) carries forward on the SAME predicate and
        # for the same reason as the findings projection above: it is banked
        # by MEASURE, and the DONE screen is rendered by stage 2 — a different
        # session, in a different bundle, whose conductor never ran MEASURE and
        # so would persist ``None`` over it. Without this line the household
        # would be shown the reservation on the screen where they DECIDE and
        # then not on the screen that tells them the speaker is tuned, which is
        # the worse half to lose.
        #
        # The converse holds too, again like findings: a session that DOES run
        # MEASURE writes its own value, ``None`` included, so a fresh clean
        # measurement clears a previous session's reservation rather than
        # replaying a caveat about a capture that has been superseded.
        if isinstance(prior.get("measure"), Mapping) and state["measure"] is None:
            state["measure"] = dict(prior["measure"])
    # ``pre_apply_profile`` (the Undo stash — observe_apply_success /
    # handle_v2_restore), ``expected_post_apply_offset_db`` (the apply's own
    # declared level move — observe_apply_success / the delta probe's
    # ``applied_offset_db`` seam), and ``apply_blocked`` (the
    # auto-apply-failed nudge, layered onto the "applying"-phase
    # fix_and_retry screen — owner ruling, 2026-07-20) are NOT conductor-owned
    # fields: the conductor neither produces nor reads any of them, so they
    # are absent from the ``state`` literal above and every OTHER caller of
    # this function only ever sets the fields it does know about.
    #
    # THREE separate P0s have now been caused by a host-owned key being added
    # to ``observe_apply_success`` without a line here (pre_apply_profile
    # W6.12; cloud B1; this offset, #1811).
    # ``test_every_host_owned_apply_key_survives_persist_conductor_state``
    # derives the host-owned set mechanically — whatever the apply path gives a
    # value that a conductor-only persist cannot regenerate — and fails on the
    # FOURTH. The fix is a carry-forward line here. Only a key that genuinely
    # wants session scoping (like ``apply_blocked``) belongs in that test's
    # exception set instead, and it has to say why.
    #
    # They carry forward with OPPOSITE session-scoping, by design:
    #
    #   * ``pre_apply_profile`` is carried forward UNCONDITIONALLY (not gated
    #     on a matching session_id): the deferred VERIFY that auto-arms right
    #     after every SUCCESSFUL apply runs under a BRAND-NEW relay session id
    #     (``prepare_v2_verify`` mints one and "rebinds" the conductor's
    #     session_id before its own ``persist_conductor_state`` call), so a
    #     session-id-gated carry-forward would lose the stash on that very
    #     first post-apply snapshot. W6.12 P0: without this, the verify phase
    #     that always immediately follows an apply wiped the just-stashed
    #     ``pre_apply_profile`` before a household could ever reach the
    #     verify_fail Undo screen — ``/crossover/v2/restore`` 400'd with "no
    #     previous crossover to restore to" after literally every apply.
    #
    #   * ``apply_blocked`` IS session-scoped (#1605): it is only ever set on
    #     a BLOCKED auto-apply, which — unlike a successful one — refuses the
    #     deferred VERIFY outright (the honest ``apply_failed`` reason, never a
    #     re-arm), so it never has to survive ``prepare_v2_verify``'s
    #     new-session rebind. Gating it drops a stale nudge the moment a fresh
    #     session begins instead of leaking session A's blocker onto session
    #     B's apply step.
    state["pre_apply_profile"] = prior.get("pre_apply_profile")
    # ``expected_post_apply_offset_db`` (#1811) is the THIRD field in this
    # host-owned class and takes ``pre_apply_profile``'s unconditional shape
    # for the identical reason: ``observe_apply_success`` writes it, the
    # conductor neither produces nor reads it, so it is absent from the state
    # literal above and every call to this function would otherwise erase it.
    #
    # It was erased, on every call, until this line existed — and the two
    # readers that lost it are exactly the two the post-apply flow depends on.
    # The CLOUD_VERIFY probe (the one that carries the spatial arm AND rollback
    # authority) re-classifies after the group closes, by which time several
    # captures have persisted: it would have graded the apply's own headroom
    # charge blind and could roll a healthy correction back — the precise
    # failure this key exists to prevent, one phase later. And
    # ``prepare_v2_verify``'s re-arm persists under a brand-new session id, so
    # every "Try again" probe would have been blind too.
    #
    # Session-scoping it would reintroduce that second half: like
    # ``pre_apply_profile``, this must survive the new-session rebind.
    state["expected_post_apply_offset_db"] = prior.get(
        "expected_post_apply_offset_db"
    )
    state["apply_blocked"] = (
        prior.get("apply_blocked")
        if prior.get("session_id") == snap.session_id
        else None
    )
    save_v2_state(state)


def _persist_terminal_failure(
    conductor: Any, code: str, *, refusals: Sequence[str] = (),
) -> None:
    """Session-terminal persistence (§5.6): pre-apply, capture evidence dies
    with the session (restart at CHECK); post-apply, the applied candidate +
    verify priors survive so ``/v2/verify`` can re-arm.

    SF2 (adversarial review, 2026-07-20): ``REASON_APPLY_FAILED`` is exempted
    from the pre-apply evidence reset. The §5.6 rationale for wiping
    ``accepted_phases``/``gain_plan_db`` is that a DEAD session makes the mic
    position unverifiable — but an auto-apply that came back blocked or
    errored says nothing about the mic position; MEASURE's own evidence is
    still exactly as good as it was. Keeping MEASURE accepted here is what
    lets ``_phase_from_state`` resolve to ``PHASE_APPLYING`` (not
    ``PHASE_CHECK``) so the envelope's apply-step failure screen — and the
    specific blocked-issue nudge layered onto it — can actually render;
    before this fix the reset always won, so that nudge was unreachable in
    production (only reachable by injecting the phase directly in a test).
    """
    from jasper.active_speaker.crossover_v2_flow import REASON_APPLY_FAILED

    persist_conductor_state(
        conductor, failure_code=code, failure_refusals=refusals,
    )
    state = load_v2_state()
    if state is None:
        return
    if not state.get("applied") and code != REASON_APPLY_FAILED:
        state["accepted_phases"] = []
        state["gain_plan_db"] = None
    save_v2_state(state)


# --------------------------------------------------------------------------- #
# production seam bindings (S1a/S1e)
# --------------------------------------------------------------------------- #


def _wav_bytes_to_samples(wav_bytes: bytes) -> tuple[Any, int]:
    import io

    import numpy as np
    from scipy.io import wavfile

    rate, data = wavfile.read(io.BytesIO(wav_bytes))
    if data.ndim > 1:
        data = data[:, 0]
    if np.issubdtype(data.dtype, np.integer):
        scale = float(np.iinfo(data.dtype).max)
        samples = data.astype(np.float64) / scale
    else:
        samples = data.astype(np.float64)
    return samples, int(rate)


def resolve_relay_calibration(setup: Any, device: Any) -> Any:
    """The production mic-calibration resolver for a v2 capture.

    Reuses ``correction_setup._relay_calibration_from_setup`` — the ONE point
    the room + legacy crossover relay flows already use to materialize the
    phone wizard's serial/upload/stored calibration choice as a stored
    ``CalibrationRecord`` (and persist it as the household default mic).
    Returns the record or ``None`` (phone mic / no calibration chosen, OR a
    detected mic-identity mismatch — see ``_relay_calibration_from_setup``'s
    ``device`` parameter / ``_stored_calibration_model_mismatch``: a
    ``mode="stored"`` re-confirm silently carrying a DIFFERENT mic's
    calibration than this capture's reported device, the 2026-07-20
    incident). ``device`` is this capture's phone-reported input device
    (``CaptureResult.device``) — threaded through so that mismatch can be
    caught at the same point the calibration is resolved for THIS capture,
    not applied blind to whichever mic actually recorded.
    """
    from .correction_setup import _relay_calibration_from_setup

    return _relay_calibration_from_setup(
        dict(setup) if isinstance(setup, Mapping) else None,
        device=device if isinstance(device, Mapping) else None,
    )


def default_setup_calibration_for_v2() -> Any | None:
    """The v2 session's OPTIONAL household-mic prefill hint (W6.12).

    Every v2 capture logged ``crossover_v2_uncalibrated_capture`` even when
    the household had a resolvable stored mic (a UMIK-2 by serial, ingested
    via ``/correction/calibration/fetch``). Root cause: ``resolve_relay_calibration``
    is only as good as what the phone posts in ``setup.calibration`` — and a
    v2 capture-plan session has no calibration-picker screen of its own. The
    LEGACY per-driver crossover flow gets away with this because its
    calibration choice comes from the ``level_ramp`` level-match page the
    household visits FIRST in the same phone tab (the capture page's
    ``setupState`` module variable survives the in-tab hash navigation from
    that page into the driver sweeps that follow); v2 has no preceding
    level-match page (design: CHECK's own pilot pairs solve gain), so it
    never had a carrier for the same hint.

    Reuses ``correction_setup._default_setup_calibration_for_spec`` — the
    SAME household-mic-hint resolver the legacy level-match handlers already
    pass into ``build_level_ramp_spec``. Threaded into
    ``build_v2_session_spec``/``build_v2_verify_session_spec`` via their
    shared ``**spec_kwargs`` forward to ``build_crossover_sweep_spec``
    (W6.12 added the parameter there); the capture page applies it SILENTLY
    (no extra tap) when nothing has already been chosen for that page load.
    Fail-soft: any resolution miss yields no hint, never blocks session open.
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
    """What the capture's phone-reported setup held, redacted-safe (W6.13).

    Returns ``(mode, calibration_id)`` for the uncalibrated-capture WARN so a
    live journal line settles empirically whether the phone sent NO setup at
    all (``mode="absent"``) or sent one whose calibration didn't resolve
    (e.g. ``mode="none"``, or a stale ``calibration_id``) — the round-5
    ambiguity. Only the mode and the calibration_id (a stored-record id, not
    a secret) are ever extracted; a serial or an uploaded calibration file
    body never reaches the journal.
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


def bind_production_analyze(
    *,
    resolve_calibration: Callable[[Any, Any], Any] | None = resolve_relay_calibration,
    meta: dict[str, Any] | None = None,
) -> "AnalyzeCapture":
    """The real ``analyze`` seam: CaptureResult → ``analyze_program_capture``.

    Design §5.6.4 applies the mic cal to every gated response, so this binding
    resolves the calibration from the capture's phone-reported setup (the same
    ``_relay_calibration_from_setup`` machinery the legacy relay flows use)
    and threads BOTH the resolved curve and the conductor's declared geometry
    into ``analyze_program_capture``. When no calibration resolves, the
    analysis still runs — relative timing/level stay valid per the design —
    but the fact is never silent: a WARN ``event=`` fires and ``meta``
    (persisted with the session's evidence refs) records the per-phase
    ``{"applied": False}`` annotation.

    ``phase`` (required, keyword-only, issue #1855) is the conductor's own
    flow phase — ``crossover_v2_flow.CrossoverV2Conductor.consume_capture``
    always passes it. It is NOT the same value as ``program.phase``: every
    cloud position plays the verify-shaped summed sweep, so
    ``program.phase == "verify"`` even during
    PHASE_CLOUD_MEASURE/PHASE_CLOUD_VERIFY. Retention (below) must label a
    capture with the flow's phase, never the program's, or every cloud
    position gets retained as "verify" (the bug this fixes — 32 of 45
    retained sidecars, per the 2026-07-29 WO-0 retrospective). No default:
    see ``crossover_v2_flow.AnalyzeCapture``'s docstring for why a silent
    fallback to ``program.phase`` is exactly the defect class being fixed.
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
                    phase=program.phase,
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
                phase=program.phase,
                setup_mode=setup_mode,
                setup_calibration_id=setup_calibration_id,
            )
        if meta is not None:
            meta.setdefault("calibration", {})[program.phase] = {
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
        priors = dataclasses.replace(
            priors, mic_tier=mic_tier_for_model(getattr(record, "model", None)),
        )
        analysis = _pa.analyze_program_capture(
            program,
            samples,
            rate,
            calibration=curve,
            geometry=geometry,
            priors=priors,
        )
        # #1855: retention labels the capture from the FLOW's phase (this
        # function's own required ``phase`` argument), never from
        # ``program.phase`` — see the docstring above and
        # ``_maybe_retain_capture``.
        _maybe_retain_capture(
            phase=phase, result=result, wav=wav, analysis=analysis,
            # WO-1: the ring is the store WO-0 found carrying NEITHER the
            # bundle id nor the relay session id — only an epoch-microsecond
            # filename stamp, which is why the SHA-256 of the WAV bytes ended
            # up as the corpus's only reliable join. ``meta`` is the evidence
            # ``refs`` dict, which already holds the bundle session id.
            bundle_session_id=str((meta or {}).get("bundle_session_id") or ""),
        )
        return analysis

    return _analyze


def _prune_capture_dump(
    dump_dir: Path, *, max_files: int, max_bytes: int, phase: str = "unknown",
) -> None:
    """Oldest-first ring-buffer prune, bounded by BOTH file count and bytes.

    Genuinely never raises. An operator is expected to `ls`/`scp`/`rm` this
    directory WHILE captures keep landing — that is the documented usage —
    so a file can legitimately vanish between one step and the next here.
    Every ``.stat()``/``.unlink()`` is individually guarded (a file that
    vanished mid-prune is just skipped, not fatal), and the WHOLE body is
    additionally wrapped so any other OSError this doesn't anticipate still
    degrades to a WARN instead of propagating out of ``_maybe_retain_capture``
    and crashing the measurement (the exact bug class a disappearing-file
    review finding caught: the old version's sort/sum `.stat()` calls sat
    outside any try/except).
    """
    try:
        entries = [
            p for p in dump_dir.iterdir()
            if p.is_file() and p.name != XOVER_CAPTURE_DUMP_ENABLED_MARKER
        ]
        sized: list[tuple[Path, float, int]] = []
        for p in entries:
            try:
                st = p.stat()
            except OSError:
                continue  # vanished between iterdir() and stat() — skip it
            sized.append((p, st.st_mtime, st.st_size))
        sized.sort(key=lambda entry: entry[1])
        total_bytes = sum(size for _p, _mtime, size in sized)
        while sized and (len(sized) > max_files or total_bytes > max_bytes):
            victim, _mtime, victim_bytes = sized.pop(0)
            try:
                victim.unlink()
                total_bytes -= victim_bytes
            except OSError:
                continue
    except OSError:
        log_event(
            logger, "correction.crossover_v2_capture_retain_failed",
            level=logging.WARNING, phase=phase, exc_info=True,
        )


def _maybe_retain_capture(
    *, phase: str, result: Any, wav: bytes, analysis: Any,
    bundle_session_id: str = "",
) -> None:
    """Operator-debug capture retention (Part 2 — off by default, bounded).

    Gated on ``XOVER_CAPTURE_DUMP_DIR / XOVER_CAPTURE_DUMP_ENABLED_MARKER``
    existing; when it does not (the default for every real household), this
    returns after one ``Path.exists()`` check — zero behavior change. When
    present, persists
    the raw captured WAV plus a diagnostic-summary JSON sidecar
    (``program_analysis.analysis_diagnostic_summary`` — the same numbers the
    v2 conductor's per-phase diag ``log_event`` calls surface, see
    ``jasper.active_speaker.crossover_v2_flow``) so a retained clip is
    self-describing without replaying the analysis, then ring-buffer prunes
    the directory.

    ``phase`` is the caller's resolved label (the flow's own phase — see
    ``_analyze``'s docstring), not derived here. Before the #1855 fix this
    function read ``program.phase`` directly, which mislabeled every cloud
    position as "verify" (every cloud position plays the verify-shaped
    summed sweep, so ``program.phase`` can't distinguish them) — 32 of 45
    retained sidecars, per the 2026-07-29 WO-0 retrospective. Existing
    on-disk sidecars from before the fix are NOT rewritten; they are
    historical artifacts of the bug, not corrected in place.

    Fully best-effort: this runs inside the real ``analyze`` seam, so ANY
    failure here must never affect the measurement itself. Every failure
    mode (disk full, permission, a non-``ProgramAnalysis`` double reaching
    this from a test monkeypatch — see
    ``test_production_analyze_threads_geometry_and_resolved_calibration``,
    which stubs ``analyze_program_capture`` to return a bare string) is
    caught and logged at WARN, never raised past this function.
    """
    if not (XOVER_CAPTURE_DUMP_DIR / XOVER_CAPTURE_DUMP_ENABLED_MARKER).exists():
        return
    try:
        from jasper.audio_measurement import program_analysis as _pa

        XOVER_CAPTURE_DUMP_DIR.mkdir(parents=True, exist_ok=True)
        device = getattr(result, "device", None) or {}
        setup = getattr(result, "setup", None) or {}
        device_label = str(device.get("label") or "unknown")
        # Device labels are phone-reported free text (untrusted) — keep the
        # filename filesystem-safe and short.
        safe_device = "".join(
            ch if (ch.isalnum() or ch in "-_") else "_" for ch in device_label
        )[:40] or "unknown"
        stamp = f"{time.time():.6f}".replace(".", "")
        basename = f"{stamp}_{phase}_{safe_device}"
        wav_path = XOVER_CAPTURE_DUMP_DIR / f"{basename}.wav"
        sidecar_path = XOVER_CAPTURE_DUMP_DIR / f"{basename}.json"
        wav_path.write_bytes(wav)
        setup_mode, setup_calibration_id = _setup_calibration_observation(setup)
        digest = hashlib.sha256(wav).hexdigest()
        sidecar = {
            "phase": phase,
            "device_label": device_label,
            "wav_bytes": len(wav),
            "wav_sha256_12": digest[:12],
            # WO-1 (attribution plan §6): the FULL digest, because a 12-char
            # prefix is a browsing aid, not a verifier — and this ring is
            # exactly where WO-0 had to fall back to content hashing for
            # want of an index.
            "wav_sha256": digest,
            "setup_mode": setup_mode,
            "setup_calibration_id": setup_calibration_id,
            "diagnostic": _pa.analysis_diagnostic_summary(analysis),
        }
        if bundle_session_id:
            # The index this store never had. With it, a pulled ring sidecar
            # joins to its bundle by id instead of by directory mtime — which
            # WO-0 measured actively misrouting (the ~08:27 session lives in
            # one bundle while the bundle whose mtime IS 08:27 holds the
            # previous evening's run).
            from jasper.attribution.session_identity import (
                SessionIdentity,
                stamp_session_identity,
            )

            stamp_session_identity(
                sidecar, SessionIdentity(session_id=bundle_session_id)
            )
        sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")
    except (OSError, ValueError, TypeError, AttributeError):
        log_event(
            logger, "correction.crossover_v2_capture_retain_failed",
            level=logging.WARNING, phase=phase, exc_info=True,
        )
        return
    log_event(
        logger, "correction.crossover_v2_capture_retained",
        phase=phase, bytes=len(wav), path=wav_path.name,
    )
    # Belt-and-suspenders: _prune_capture_dump is already internally
    # never-raising, but this call site is guarded too so a future bug in
    # prune (or an exception type it doesn't yet anticipate) still can't
    # escape into the analyze seam and crash the measurement.
    try:
        _prune_capture_dump(
            XOVER_CAPTURE_DUMP_DIR,
            max_files=XOVER_CAPTURE_DUMP_MAX_FILES,
            max_bytes=XOVER_CAPTURE_DUMP_MAX_BYTES,
            phase=phase,
        )
    except (OSError, ValueError, TypeError, AttributeError):
        log_event(
            logger, "correction.crossover_v2_capture_retain_failed",
            level=logging.WARNING, phase=phase, exc_info=True,
        )


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


def bind_evidence_publishers(
    store: Any, relay_session_id: str
) -> tuple[Callable[[Any, Mapping[str, Any]], None], Callable[[Any], None], dict[str, Any]]:
    """Real ``publish_check`` / ``publish_candidate`` seams (§5.6).

    CHECK publishes the ambient report + solved gain plan; MEASURE publishes
    the full candidate dict and re-opens it through
    ``MeasuredCrossoverCandidate.from_mapping`` — the same tamper check the
    apply path runs, so a candidate that cannot survive exact reopen never
    becomes reviewable. Artifact fingerprints land in the returned ``refs``
    mapping (persisted into the durable state for the status surface).
    """
    refs: dict[str, Any] = {"bundle_session_id": store.session_id}

    def publish_check(gain_plan: Any, ambient_report: Mapping[str, Any]) -> None:
        artifact = store.publish_json_artifact(
            f"crossover_v2/{relay_session_id}/check.json",
            {
                "schema_version": 1,
                "kind": "jts_crossover_v2_check_evidence",
                "relay_session_id": relay_session_id,
                "gain_plan_db": dict(gain_plan.gain_db),
                "predicted_peak_dbfs": gain_plan.predicted_peak_dbfs,
                "snr_floor_ok": gain_plan.snr_floor_ok,
                # #1825: the per-role derivation behind ``gain_plan_db`` —
                # which limit chose each driver's MEASURE level and the
                # ambient evidence it rests on. Empty for a legacy plan that
                # carries no solves (never a claim that nothing moved).
                "role_solves": {
                    role: solve.to_dict()
                    for role, solve in (gain_plan.role_solves or {}).items()
                },
                "ambient_report": dict(ambient_report),
            },
        )
        refs["check_artifact"] = artifact.fingerprint

    def publish_candidate(candidate: Any) -> None:
        from jasper.active_speaker.measured_crossover_candidate import (
            MeasuredCrossoverCandidate,
        )

        artifact = store.publish_json_artifact(
            f"crossover_v2/{relay_session_id}/candidate.json",
            candidate.to_dict(),
        )
        reopened_raw = store.reopen_json_artifact(artifact)
        reopened = MeasuredCrossoverCandidate.from_mapping(reopened_raw)
        if reopened.fingerprint != candidate.fingerprint:
            raise RuntimeError(
                "published measured candidate changed on exact readback"
            )
        refs["candidate_artifact"] = artifact.fingerprint
        log_event(
            logger,
            "correction.crossover_v2_candidate_published",
            relay_session_id=relay_session_id,
            candidate_fingerprint=candidate.fingerprint,
            artifact_fingerprint=artifact.fingerprint,
        )

    return publish_check, publish_candidate, refs


def bind_raw_anchor_publisher(
    store: Any,
    relay_session_id: str,
    refs: dict[str, Any],
    *,
    session_graph: Any,
    configured_fc_hz: float,
    safety_profile: Mapping[str, Any],
) -> Callable[[Any, Any, Any, int], None]:
    """Publish the accepted MEASURE WAV and anchor-only v1 record write-once."""

    def _publish(analysis: Any, result: Any, program: Any, attempt: int) -> None:
        from jasper.active_speaker.crossover_raw_evidence import (
            build_raw_evidence_v1,
            validate_raw_evidence_v1,
        )

        wav = getattr(result, "wav", result)
        if not isinstance(wav, (bytes, bytearray)):
            raise TypeError("RAW anchor publisher requires exact capture WAV bytes")
        take_id = f"anchor_00_take_{int(attempt):02d}"
        calibration = (refs.get("calibration") or {}).get("measure") or {
            "applied": False,
            "calibration_id": None,
        }
        payload = build_raw_evidence_v1(
            session_id=relay_session_id,
            analysis=analysis,
            program=program,
            capture_wav=bytes(wav),
            attempt=int(attempt),
            session_graph=session_graph,
            configured_fc_hz=float(configured_fc_hz),
            component_safety_profile=safety_profile,
            calibration_identity=calibration,
        )
        expected_identity = {
            "graph": session_graph.identity["fingerprint"],
            "safety": payload["component_safety_profile_identity"]["fingerprint"],
            "calibration": payload["calibration_identity"]["fingerprint"],
            "stimulus": payload["stimulus_identity"]["fingerprint"],
        }

        def _validate(value: Mapping[str, Any]) -> None:
            validate_raw_evidence_v1(
                value,
                expected_session_id=relay_session_id,
                expected_graph_fingerprint=expected_identity["graph"],
                expected_safety_fingerprint=expected_identity["safety"],
                expected_calibration_fingerprint=expected_identity["calibration"],
                expected_stimulus_fingerprint=expected_identity["stimulus"],
            )

        raw_artifact, artifact = store.publish_raw_json_pair(
            raw_relative_path=f"crossover_v2/{relay_session_id}/{take_id}.wav",
            raw_payload=bytes(wav),
            json_relative_path=(
                f"crossover_v2/{relay_session_id}/raw_evidence_v1.json"
            ),
            json_payload=payload,
            validate_json=_validate,
        )
        refs["raw_anchor_wav_artifact"] = raw_artifact.fingerprint
        refs["raw_evidence_artifact"] = artifact.fingerprint
        refs["raw_evidence_set_fingerprint"] = payload[
            "evidence_set_fingerprint"
        ]
        log_event(
            logger,
            "correction.crossover_v2_raw_anchor_published",
            relay_session_id=relay_session_id,
            evidence_set_fingerprint=payload["evidence_set_fingerprint"],
        )

    return _publish


def bind_position_retention(
    store: Any, relay_session_id: str, refs: dict[str, Any]
) -> Callable[[str, Any, Mapping[str, Any]], None]:
    """The real ``retain_position`` seam — one WAV + one JSON per cloud position.

    The forensic record the position-group choreography owes: the S0 work that
    produced this program's central finding (source-fixed vs room-fixed comb
    attribution) was only possible because every position's RAW capture
    survived, so a household cloud keeps the same thing rather than a derived
    summary that cannot answer a question nobody has asked yet.

    Placement follows the shipped bundle scheme —
    ``bundles.capture_artifact_relpath("summed", group, role)`` with the TAKE
    id (position id + attempt) as ``group``, so a cloud WAV lands beside the
    flow's other summed captures rather than in a private layout — and the
    metadata sidecar
    carries the prompt the operator was given, which is the only durable record
    of WHERE a curve was measured. ``metadata["position_id"]`` is what feeds
    ``spatial_combine.PositionCapture.position_id``, so a flagged or outlying
    position can be named back to the household in PR-4's report.

    This function does NOT swallow failures. The evidence store is deliberately
    strict — ``publish_json_artifact`` raises ``CommissioningEvidenceStoreError``
    (a ``RuntimeError``) rather than silently dropping an artifact, and a WAV
    write raises ``OSError`` — and the fail-soft boundary lives one level up, at
    the conductor's ``_retain_cloud_position`` call site, which logs and
    continues so a full disk cannot turn an acoustically-good position into a
    retake. Keeping the boundary there rather than here means the strictness the
    store was built for is preserved for every OTHER caller.
    """
    from jasper.active_speaker.bundles import capture_artifact_relpath

    def retain_position(
        position_id: str, result: Any, metadata: Mapping[str, Any]
    ) -> None:
        wav = getattr(result, "wav", None)
        record = dict(metadata)
        # A geometry retake re-uses its position id — same prompted spot,
        # measured again from further out — so the id alone does NOT identify a
        # take. The evidence store is write-once (a repeated path is a
        # PATH_CONFLICT refusal), which would have dropped the retake's sidecar
        # and left the REPLACED take as the only record of a curve that is not
        # in the cloud. Qualify by attempt: every take gets its own sidecar,
        # the superseded one stays on disk as the honest walk record, and the
        # conductor's `group_position_takes` names which attempt survived.
        attempt = int(record.get("attempt") or 0)
        take_id = f"{position_id}_a{attempt:02d}"
        wav_rel = ""
        if isinstance(wav, (bytes, bytearray)):
            wav_rel = capture_artifact_relpath("summed", take_id, None)
            wav_path = Path(store.bundle_dir) / wav_rel
            wav_path.parent.mkdir(parents=True, exist_ok=True)
            wav_path.write_bytes(bytes(wav))
            record["wav_path"] = wav_rel
            record["wav_bytes"] = len(wav)
        artifact = store.publish_json_artifact(
            f"crossover_v2/{relay_session_id}/positions/{take_id}.json",
            {
                "schema_version": 1,
                "kind": "jts_crossover_v2_position_evidence",
                "relay_session_id": relay_session_id,
                **record,
            },
        )
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
            "artifact": artifact.fingerprint,
            "wav_path": wav_rel,
            "wav_sha256": str(record.get("wav_sha256") or ""),
        })

    return retain_position


def v2_session_identity(store: Any, relay_session_id: str) -> Any:
    """This v2 session's cross-store identity (attribution plan §6).

    The **bundle** session id is canonical, because Q-C's bundle-lifetime
    ruling makes the bundle the retention unit: identity and lifetime then
    name the same thing, which is what keeps a finding from outliving its
    evidence. The relay capture-session id is real and is minted *after* the
    bundle — it is not derivable from it — so it rides as an alias rather
    than as a second identity. Before this, the only join between the two
    namespaces was one key in the durable state file, and the capture ring
    carried neither.
    """

    from jasper.attribution.session_identity import (
        ALIAS_RELAY_SESSION_ID,
        SessionIdentity,
    )

    return SessionIdentity(
        session_id=str(store.session_id),
        aliases={ALIAS_RELAY_SESSION_ID: str(relay_session_id)},
    )


def _publish_findings(
    store: Any,
    relay_session_id: str,
    phase: str,
    result: Mapping[str, Any],
    cloud_artifact: Any,
    refs: dict[str, Any],
) -> None:
    """Promote this group's excluded-band records to findings and persist them.

    WO-1's write half. The findings cite the cloud artifact **that was just
    published** — the exact bytes the carve-out records were read from — so
    the citation is verifiable and, being a bundle artifact, is bound to the
    same lifetime the finding is (Q-C).

    **Fail-soft, unlike its two sibling seams.** ``publish_cloud`` and
    ``retain_position`` deliberately let the strict store's refusals surface
    so the conductor's own boundary handles them. Findings are different:
    plan §3.4 makes them *optional evidence artifacts* — "a session with no
    findings behaves exactly as it does today" — so a findings failure must
    not turn a successfully-published cloud group into a logged failure. The
    cloud artifact above is already durable by the time this runs.
    """

    from jasper.attribution.findings import FindingSet
    from jasper.attribution.promotion import PRODUCED_BY, promote_carve_outs
    from jasper.attribution.storage import (
        bundle_evidence_ref,
        publish_finding_set,
    )

    try:
        identity = v2_session_identity(store, relay_session_id)
        findings = promote_carve_outs(
            result.get("carve_outs"),
            session=identity,
            cites=(bundle_evidence_ref(cloud_artifact, identity),),
        )
        artifact = publish_finding_set(
            store,
            relay_session_id=relay_session_id,
            phase=phase,
            finding_set=FindingSet(
                session=identity,
                produced_by=PRODUCED_BY,
                findings=findings,
            ),
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        log_event(
            logger,
            "correction.crossover_v2_findings_publish_failed",
            level=logging.WARNING,
            relay_session_id=relay_session_id,
            phase=phase,
            exc_info=True,
        )
        return
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
        relay_session_id=relay_session_id,
        phase=phase,
        findings=len(findings),
    )


def _bank_household_findings(
    store: Any, *, relay_session_id: str, phase: str, refs: dict[str, Any],
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
    relay session id (``prepare_v2_verify`` → ``open_v2_evidence_store``), so
    by the time the household sees the result screen, "this session's bundle"
    no longer holds the set the measuring session banked. Reading once and
    projecting the compact result into the durable state is the same shape
    ``_compact_cloud_status`` already uses for the cloud's numbers: the bundle
    artifact stays the record; the state carries what a screen renders.

    **The read-back is itself the honesty check.** Going out through
    ``publish_finding_set`` and straight back in through ``read_finding_set``
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
            store, relay_session_id=relay_session_id, phase=phase,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        log_event(
            logger,
            "correction.crossover_v2_findings_readback_failed",
            level=logging.WARNING,
            relay_session_id=relay_session_id,
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
        relay_session_id=relay_session_id,
        phase=phase,
        findings=len(finding_set.findings),
    )


def bind_findings_publisher(
    store: Any, relay_session_id: str, refs: dict[str, Any]
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
    frame's own per-role ``level_frame_offset_db`` inside its ``linearization``
    block, and it is guaranteed to exist and to be durable at this point in the
    session — which a citation must be, since
    :func:`~jasper.attribution.storage.read_finding_set` re-hashes it on every
    read and raises when it cannot be confirmed.

    Fail-soft, like :func:`_publish_findings` and for the same §3.4 reason: the
    candidate is already published and the gate has already ruled that this
    session may proceed. A findings failure is a lost diagnosis, never a lost
    tune.
    """

    def publish_findings(record: Mapping[str, Any]) -> None:
        from jasper.active_speaker.commissioning_evidence_store import (
            EVIDENCE_ROOT,
        )
        from jasper.active_speaker.crossover_v2_flow import PHASE_MEASURE
        from jasper.attribution.findings import FindingSet
        from jasper.attribution.promotion import (
            PRODUCED_BY_LEVEL_FRAME,
            promote_level_frame_disagreement,
        )
        from jasper.attribution.storage import (
            bundle_evidence_ref,
            publish_finding_set,
        )

        try:
            identity = v2_session_identity(store, relay_session_id)
            finding = promote_level_frame_disagreement(
                record,
                session=identity,
                cites=(
                    bundle_evidence_ref(
                        store.identify_artifact(
                            f"{EVIDENCE_ROOT}/artifacts/crossover_v2/"
                            f"{relay_session_id}/candidate.json"
                        ),
                        identity,
                    ),
                ),
            )
            # A record the promoter refused is already logged by it, with the
            # reason. Publishing an EMPTY set here would be a lie of a
            # different shape — "attribution ran and found nothing" — about a
            # session whose gate found something and said so in the journal.
            if finding is None:
                return
            artifact = publish_finding_set(
                store,
                relay_session_id=relay_session_id,
                phase=PHASE_MEASURE,
                finding_set=FindingSet(
                    session=identity,
                    produced_by=PRODUCED_BY_LEVEL_FRAME,
                    findings=(finding,),
                ),
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            log_event(
                logger,
                "correction.crossover_v2_findings_publish_failed",
                level=logging.WARNING,
                relay_session_id=relay_session_id,
                phase=PHASE_MEASURE,
                exc_info=True,
            )
            return
        refs.setdefault("finding_artifacts", {})[PHASE_MEASURE] = (
            artifact.fingerprint
        )
        log_event(
            logger,
            "correction.crossover_v2_findings_published",
            relay_session_id=relay_session_id,
            phase=PHASE_MEASURE,
            findings=1,
        )
        # The read half (CC1). Deliberately AFTER the publish log and outside
        # the try above: the set is durable at this point, so a read-back
        # failure must be reported as its own event rather than making a
        # successful publish look like a failed one.
        _bank_household_findings(
            store,
            relay_session_id=relay_session_id,
            phase=PHASE_MEASURE,
            refs=refs,
        )

    return publish_findings


def bind_cloud_publisher(
    store: Any, relay_session_id: str, refs: dict[str, Any]
) -> Callable[[str, Mapping[str, Any]], None]:
    """The real ``publish_cloud`` seam (flat-linearization plan PR-4).

    One JSON artifact PER CLOSED GROUP — ``crossover_v2/<session>/<phase>.json``
    (``cloud_measure.json`` / ``cloud_verify.json``), never a single shared
    ``cloud.json`` across both groups. The evidence store is write-once (a
    repeated path is a ``PATH_CONFLICT`` refusal — see
    :func:`bind_position_retention`'s own docstring), and the pre-apply and
    post-apply groups close at genuinely different times in the SAME
    session, so a single shared path would collide on the second group's
    write. This is a mechanism deviation from the work order's literal
    ``crossover_v2/<session>/cloud.json`` path, recorded here rather than
    silently matched — the per-group content (mask/registry/spec/geometry)
    is exactly what was asked for either way.

    Fail-soft at the CALLER (``CrossoverV2Conductor._run_cloud_pipeline``):
    like :func:`bind_position_retention`, this function does not swallow
    failures itself — a full disk or a write-once conflict must surface as
    an exception here so the conductor's own boundary can log and continue
    rather than every OTHER caller of this store losing its strictness.
    """

    def publish_cloud(phase: str, result: Mapping[str, Any]) -> None:
        from jasper.attribution.session_identity import stamp_session_identity

        payload = stamp_session_identity(
            {
                "schema_version": 1,
                "kind": "jts_crossover_v2_cloud_evidence",
                "relay_session_id": relay_session_id,
                "phase": phase,
                **dict(result),
            },
            v2_session_identity(store, relay_session_id),
        )
        artifact = store.publish_json_artifact(
            f"crossover_v2/{relay_session_id}/{phase}.json", payload
        )
        cloud_artifacts = refs.setdefault("cloud_artifacts", {})
        cloud_artifacts[phase] = artifact.fingerprint
        _publish_findings(store, relay_session_id, phase, result, artifact, refs)

    return publish_cloud


# --------------------------------------------------------------------------- #
# pre-tone phase ladder (#1824 D3/D4)
# --------------------------------------------------------------------------- #

# The phone-visible names for what the speaker is ACTUALLY doing. Only
# ``sweep_started``/``sweep_complete`` existed before, and ``sweep_started`` was
# posted synchronously in ``on_armed`` — ~4.6 s before any sound on a courtesy-
# prelude program (0.6 s of beeps + a 3.0 s settle + the 1.0 s pre-pilot
# ambient window), so the phone said "Playing the measurement tone…" through
# the entire quiet stretch the household is being asked to be quiet in. The two
# names below are that missing middle. ``ambient_started`` in particular had NO
# producer anywhere on the Pi — the capture page's countdown consumer for it
# has been shipped-but-dead since the producer it was written against was
# deleted (see docs/HANDOFF-correction.md).
HOST_PHASE_PRELUDE_STARTED = "prelude_started"
HOST_PHASE_AMBIENT_STARTED = "ambient_started"
HOST_PHASE_SWEEP_STARTED = "sweep_started"
HOST_PHASE_SWEEP_COMPLETE = "sweep_complete"

# NAMED RESIDUAL — ON-DEVICE: the size of this bias is NOT measured. The ladder
# is anchored where the host HANDS a program to the playback path, which leads
# real audio by the verified-source read plus the ALSA/output prefill: tens to a
# few hundred ms, device-dependent, and not one number we can look up. Delaying
# every step is INTENDED to bias late, so the residual lands on the safe side —
# a phase line appearing late is harmless, a phone claiming the tone is playing
# while the room is silent is the failure this ladder exists to remove. It is
# not a guarantee: a prefill longer than this value would still put the sweep
# line marginally early.
#
# Two things push the same way and are worth knowing before tuning it: the
# phone only repaints on its own poll (``progress_poll_ms``, ≤1 s), which adds
# further late bias on the rendered line; and the household reads copy, not
# timestamps. Measure the real anchor-to-audio interval on hardware before
# changing this, and prefer replacing the whole estimate with an observed
# playback start over shrinking the number from reasoning alone.
PHASE_LADDER_START_SKEW_S = 0.35


class PlaybackStartSignal:
    """The seam between the play binding and the runner's phone-phase posts.

    ``bind_production_play`` is built inside ``_open`` — before the runner
    exists — so the play path cannot post to the phone itself. It fires this
    signal at the instant a program's WAV reaches the playback call; the runner
    installs a handler for the duration of ONE armed capture and clears it
    afterwards, so a late fire from a play that outlived its capture is a no-op
    rather than a phase post against the wrong capture.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._handler: Callable[[Any], None] | None = None

    def install(self, handler: Callable[[Any], None]) -> None:
        with self._lock:
            self._handler = handler

    def clear(self) -> None:
        with self._lock:
            self._handler = None

    def fire(self, program: Any) -> None:
        with self._lock:
            handler = self._handler
        if handler is None:
            return
        try:
            handler(program)
        except (
            OSError, RuntimeError, ValueError, AttributeError, KeyError, TypeError,
        ):
            # Progress reporting must never be able to stop a measurement, and
            # this fires from INSIDE the play path — an escape here would abort
            # a sweep over a status line. The handler reads a program object it
            # does not own (AttributeError/TypeError/KeyError on an unexpected
            # shape) and starts a thread (RuntimeError), so the list covers what
            # it can actually raise; pinned by
            # test_playback_signal_handler_failure_never_stops_the_measurement.
            logger.warning("v2 playback-start signal handler failed", exc_info=True)


def program_phase_schedule(
    program: Any,
) -> tuple[tuple[float, str, dict[str, Any]], ...]:
    """When each phone-visible phase of ``program`` actually becomes audible.

    Returns ``((offset_s, phase, extra_fields), …)`` ordered by offset, where
    ``offset_s`` is measured from the start of the program WAV and
    ``extra_fields`` are the wire fields that phase carries (empty for phases
    that carry none).

    Offsets are read off the program's OWN segment table
    (``start_sample``/``sample_rate_hz``), never re-derived from the composer's
    constants: a program that stops carrying a courtesy prelude or an ambient
    window simply stops emitting that phase, and a composer that moves either
    one moves this schedule with it. A program with no segments (or an
    unreadable rate) yields an empty schedule — the caller then posts nothing,
    which is what an older/simpler program should produce.

    ``ambient_started`` carries ``quiet_requested``, and it is a MEASUREMENT
    fact, not a presentation one. The two ambient windows are opposites:

    * MEASURE/VERIFY's 1 s pre-pilot window sits AFTER the courtesy beeps and
      "MUST be measured with the room already quiet" — the phone should ask.
    * CHECK's 12 s window is the SESSION's room-noise measurement, "deliberately
      taken before the household is asked to go quiet" (both quotes:
      :mod:`jasper.audio_measurement.program`'s module docstring). The ambient
      band-floor report and the gain solve read it. A phone that asked for quiet
      during THAT window would change the very floor it is measuring — a copy
      string silently editing the measurement.

    Derived from the composed order (is this window after the beeps?), so a
    composer that moves either window moves the flag with it.
    """
    from jasper.audio_measurement.program import (
        AMBIENT_SEGMENT_ID,
        KIND_COURTESY_TONE,
        STIMULUS_KINDS,
    )

    try:
        rate = float(getattr(program, "sample_rate_hz", 0) or 0)
    except (TypeError, ValueError):
        return ()
    segments = tuple(getattr(program, "segments", ()) or ())
    if rate <= 0 or not segments:
        return ()

    steps: list[tuple[float, str, dict[str, Any]]] = []
    beeps = [s for s in segments if s.kind == KIND_COURTESY_TONE]
    beep_end = (
        max(s.start_sample + s.n_samples for s in beeps) if beeps else None
    )
    if beeps:
        steps.append(
            (
                min(s.start_sample for s in beeps) / rate,
                HOST_PHASE_PRELUDE_STARTED,
                {},
            )
        )
    ambient = [s for s in segments if s.segment_id == AMBIENT_SEGMENT_ID]
    if ambient:
        first = min(ambient, key=lambda s: s.start_sample)
        steps.append(
            (
                first.start_sample / rate,
                HOST_PHASE_AMBIENT_STARTED,
                {
                    "duration_s": first.n_samples / rate,
                    # No prelude at all ⇒ no warning was played ⇒ do not ask.
                    "quiet_requested": (
                        beep_end is not None and first.start_sample >= beep_end
                    ),
                },
            )
        )
    stimulus = [s for s in segments if s.kind in STIMULUS_KINDS]
    if stimulus:
        steps.append(
            (
                min(s.start_sample for s in stimulus) / rate,
                HOST_PHASE_SWEEP_STARTED,
                {},
            )
        )
    steps.sort(key=lambda step: step[0])
    return tuple(steps)


def start_program_phase_ladder(
    post_phase: Callable[..., None],
    program: Any,
    *,
    skew_s: float | None = None,
) -> Callable[[], None]:
    """Post ``program``'s phase ladder on the program's own clock.

    Runs on one short-lived daemon thread (the play call owns the caller's
    thread for the whole program) and returns a cancel callable the caller
    invokes when playback returns — so a program that ends early, fails, or is
    stopped cannot leave a timer behind that posts a phase for a capture that
    is already over.

    ``skew_s`` defaults to :data:`PHASE_LADDER_START_SKEW_S`, read at CALL time
    so the constant stays the single place the bias is expressed (and a test
    can drive the ladder without waiting it out).
    """
    schedule = program_phase_schedule(program)
    if not schedule:
        return lambda: None

    skew = PHASE_LADDER_START_SKEW_S if skew_s is None else float(skew_s)
    done = threading.Event()

    def _run() -> None:
        started = time.monotonic()
        for offset_s, phase, extra in schedule:
            delay = (offset_s + skew) - (time.monotonic() - started)
            if delay > 0 and done.wait(delay):
                return
            if done.is_set():
                return
            post_phase(phase, **extra)

    thread = threading.Thread(
        target=_run, name="crossover-v2-phase-ladder", daemon=True
    )
    thread.start()

    def _cancel() -> None:
        done.set()
        # UNBOUNDED join, deliberately. The relay's host-event slot is
        # last-write-wins, so a ladder post still in flight when this returns
        # would land AFTER whatever the caller posts next — overwriting a
        # `sweep_complete`, or worse a terminal `capture_result`, and putting
        # the phone back to polling a refusal it can no longer see (the
        # expired-link pathology this PR removes elsewhere). A bounded join
        # would leave exactly that race open on a slow post.
        #
        # Safe to wait: the only thing this thread does is
        # ``client.post_host_event``, whose urllib transport carries
        # ``capture_relay.client.DEFAULT_TIMEOUT_S`` (15 s) and whose failures
        # the caller already swallows as OSError — so the wait is bounded by
        # the transport, not by hope.
        thread.join()

    return _cancel


def bind_production_play(
    *,
    run_async: Any,
    camilla_factory: Any,
    evidence_store: Any,
    relay_session_id: str,
    topology: Any,
    preset: Any,
    role_channels: Mapping[str, int],
    session_graph: Any,
    playback_device: str,
    safety_profile: Mapping[str, Any],
    role_targets: Mapping[str, str],
    session_volume_db: float,
    declared_sensitivities: Mapping[str, float] | None = None,
    config_dir: str | None = None,
    on_playback_started: Callable[[Any], None] | None = None,
) -> Callable[[str, Any], None]:
    """The real ``play`` seam: program WAV → admitted playback through the DSP.

    CHECK/MEASURE/CLOUD_MEASURE render + publish the program WAV into the session's evidence
    bundle, emit the channel-routed program graph
    (``emit_active_speaker_program_config``), and ride
    :func:`jasper.active_speaker.program_playback.play_program` with the
    CamillaController seams from ``bind_program_playback_seams``; VERIFY and
    CLOUD_VERIFY play through the APPLIED production graph (verified-aplay only
    — no graph load). All run inside the mux measurement window so the
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

    ``on_playback_started`` (optional) is fired with the program at the instant
    its WAV reaches the playback call — the closest the host gets to "audio
    starts now" without reaching into the shared aplay path. It is what anchors
    the phone's pre-tone phase ladder (:func:`start_program_phase_ladder`);
    omitted, playback is unchanged and the caller keeps whatever progress
    reporting it had.

    ON-DEVICE: not exercised hardware-free; W6 validates acoustically.
    """
    from jasper.active_speaker.crossover_v2_flow import (
        PHASE_CLOUD_VERIFY,
        PHASE_VERIFY,
        bind_program_playback_seams,
    )
    from jasper.active_speaker.web_commissioning import DEFAULT_CAMILLA_CONFIG_DIR
    from jasper.audio_measurement.program import write_program_wav

    resolved_config_dir = (
        config_dir if config_dir is not None else str(DEFAULT_CAMILLA_CONFIG_DIR)
    )
    program_graph_yaml = (
        str(session_graph.yaml_text) if session_graph is not None else ""
    )

    def _play(phase: str, program: Any) -> None:
        if phase not in {PHASE_VERIFY, PHASE_CLOUD_VERIFY}:
            from jasper.active_speaker.protected_neutral_graph import (
                validate_protected_neutral_program_routing,
            )

            if session_graph is None:
                raise RuntimeError(
                    "pre-apply playback requires a protected-neutral session graph"
                )
            validate_protected_neutral_program_routing(
                program, session_graph.identity
            )
        bundle_dir = evidence_store.bundle_dir
        wav_rel = f"crossover_v2/{relay_session_id}/{phase}_program.wav"
        wav_path = Path(bundle_dir) / wav_rel
        wav_path.parent.mkdir(parents=True, exist_ok=True)
        write_program_wav(wav_path, program)
        artifact = evidence_store.identify_artifact(wav_rel)
        if phase in {PHASE_VERIFY, PHASE_CLOUD_VERIFY}:
            # Issue #1976's stable applied-sum alias. R15's pre-apply cloud
            # is deliberately a different protected-stereo artifact for the
            # protected-neutral graph and remains under its exact phase name;
            # mixing that identity into this mono production alias would make
            # offline replay nondeterministic. Best-effort as before.
            try:
                summed_wav_path = (
                    Path(bundle_dir)
                    / f"crossover_v2/{relay_session_id}/summed_program.wav"
                )
                if not summed_wav_path.exists():
                    write_program_wav(summed_wav_path, program)
            except (OSError, ValueError, TypeError, AttributeError):
                log_event(
                    logger, "correction.crossover_v2_summed_program_persist_failed",
                    level=logging.WARNING, phase=phase, exc_info=True,
                )

        async def _play_body() -> None:
            from jasper.active_speaker.program_playback import (
                verified_program_aplay,
            )

            if phase in {PHASE_VERIFY, PHASE_CLOUD_VERIFY}:
                # Post-apply evidence measures the applied production graph.
                if on_playback_started is not None:
                    on_playback_started(program)
                await verified_program_aplay(
                    bundle_dir, artifact, timeout_s=60.0
                )
                return
            from jasper.active_speaker.program_playback import play_program

            seams = bind_program_playback_seams(
                camilla_factory(),
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
            if on_playback_started is not None:
                # Wrap the seam rather than firing before ``play_program``: the
                # readmission, writer-lock acquisition and program-graph load
                # all happen inside it and all precede any sound. Firing here
                # keeps the anchor at the WAV handoff for both playback shapes.
                inner_play_wav = seams["play_wav"]

                async def _play_wav_signalling() -> Any:
                    on_playback_started(program)
                    return await inner_play_wav()

                seams["play_wav"] = _play_wav_signalling
            await play_program(
                program,
                program_graph_yaml=program_graph_yaml,
                session_volume_plan=session_volume_plan(),
                **seams,
            )

        async def _emit() -> None:
            # The session holds ONE measurement window for its whole life (W6.1
            # — see acquire_session_measurement_pause). ``measurement_window``
            # is exclusive/non-nestable, so nest-SKIP it here when the session
            # already holds it — registering this play task as the window's
            # abort target so an isolation-loss abort still stops the sweep.
            # Only fall back to a per-play window if the session pause is
            # somehow not held.
            if session_measurement_pause_held():
                await _play_under_session_pause(_play_body)
                return
            from jasper.correction import coordinator

            async with coordinator.measurement_window():
                await _play_body()

        run_async(_emit())

    return _play


# --------------------------------------------------------------------------- #
# the plan-runner host (S1a/S1c)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class V2VolumeHooks:
    """The session-volume lifecycle the runner drives (§5.5)."""

    open: Callable[[], Any]      # async
    close: Callable[[], Any]     # async
    abandon: Callable[[], Any]   # async


# W6 hardware run 3 finding H: the catch-all cleanup arm below posts a
# terminal host event (§5.10's ``capture_result``) so the phone stops
# recording into silence, then purges the relay session. Purging
# immediately after can race the phone's very next poll — the driver saw a
# bare 404 on the session's own status endpoint ~1 s after the terminal
# event, because the session was already gone. This grace gives the just-
# posted event a bounded window to actually reach the phone (over the
# public relay) before the session disappears; it does not delay the
# household's volume restore, which stays immediate.
TERMINAL_FAILURE_PURGE_GRACE_S = 3.0


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


def build_v2_run_and_consume(
    conductor: Any,
    *,
    volume: V2VolumeHooks,
    stop_event: threading.Event,
    stop_lock: Any,
    evidence_refs: Mapping[str, Any] | None = None,
    poll_interval_s: float | None = None,
    timeout_s: float | None = None,
    first_begin_timeout_s: float | None = None,
    playback_started: PlaybackStartSignal | None = None,
) -> Callable[[Any, Any], Any]:
    """The async ``run_and_consume(client, pi_session)`` for one v2 session.

    Mirrors ``build_crossover_relay_plan_run_and_consume``'s thread model: the
    REAL :func:`jasper.capture_relay.session.run_capture_plan` runs on a
    worker thread (``asyncio.to_thread``), the awaiting task shields it
    through cancellation (Stop drains the runner before purging), and the
    relay session is purged on every exit path.

    **No session applies anything (two-stage commission work order D1).** This
    runner took ``run_async``/``camilla_factory``/``idle_hold`` until PR-T3 for
    one reason: to fire ``handle_v2_apply`` on a background thread the instant
    the pre-apply cloud closed, and to keep the socket-activated wizard alive
    while that thread ran (#1854). Apply is now the household's own POST from
    the review screen — served in-request, so the idle tracker's ordinary
    in-flight-request accounting covers it — and this runner needs none of the
    three. What replaced them is the ``complete_capture_set`` seam below: the
    household's explicit "Continue" closes the group, fits the candidate, and
    persists it, and the journey stops there.

    Host-owned error mapping (S1c):

    * ``CaptureTimeout`` / ``RelayError`` / ``OSError`` / generic
      ``CaptureFailed`` — relay-session death ⇒ ``relay_timeout`` failure
      state + volume ABANDON (the §5.5 walked-away guarantee). The
      ``OSError`` here is genuinely the relay TRANSPORT (e.g.
      ``run_capture_plan``'s poll loop reaching an unreachable host) — a
      LOCAL play/analyze seam OSError never reaches this arm: ``on_armed``/
      ``consume`` below convert it to :class:`CrossoverV2LocalSeamError`
      first (W6 hardware run 3 finding G), so it falls through to the
      catch-all arm's ``internal_error`` instead.
    * ``CaptureAborted`` — a deliberate phone Stop (``reason == "stopped"``)
      gets its own honest ``user_stopped`` failure state; every other abort
      reason (backgrounded / vanished) still reads as ``relay_timeout``. Both
      abandon the volume identically — only the persisted reason differs.
    * ``CaptureBeginRefused`` — the conductor already recorded the phase's own
      failure code (including the PR-L4 accountability veto refusing ON the
      household's group-close confirmation); persist it + abandon.
    * ``CaptureStopped`` / cancellation — expected control flow: abandon the
      volume, no failure code.
    * ANY other ``Exception`` — the W6.1 catch-all cleanup arm: the seams
      raise open-endedly (``CamillaUnavailable`` is a bare Exception), so
      every non-relay failure posts a terminal host event, persists
      ``program_unplayable`` (program/admission/flow classes) or
      ``internal_error`` (everything else — including
      ``CrossoverV2LocalSeamError``), abandons the volume (releasing the
      session measurement pause), waits a bounded grace period (finding H —
      the just-posted terminal host event must reach the phone before the
      relay session is purged out from under its next poll), purges, and
      re-raises.
    * Plan complete with every phase this session ran accepted ⇒ CLOSE (exact
      restore); a completed plan that did not reach done (attempt budget
      exhausted) abandons. Stage 1 takes the CLOSE path like any other complete
      session: its phases are all accepted, the household is walking back to a
      browser rather than to another capture, and an exact restore is the
      honest end for a measurement that finished.
    """

    async def _run_and_consume(client: Any, pi_session: Any) -> None:
        from jasper.capture_relay.client import RelayError
        from jasper.capture_relay.session import (
            HOST_PHASE_CAPTURE_RESULT,
            HOST_PHASE_CAPTURE_SET_EXHAUSTED,
            CaptureAborted,
            CaptureBeginRefused,
            CaptureFailed,
            CaptureStopped,
            TIME_BUDGET_NONE,
            CaptureTimeout,
            expired_time_budget,
            purge,
            run_capture_plan,
        )
        from jasper.active_speaker.crossover_v2_flow import (
            PHASE_APPLYING,
            PHASE_DONE,
            REASON_INTERNAL_ERROR,
            REASON_REGISTRY,
            REASON_RELAY_TIMEOUT,
            REASON_REVIEW_HOLD_TIMEOUT,
            REASON_USER_STOPPED,
            V2_FIRST_BEGIN_TIMEOUT_S,
            TRANSIENT_AUTO_RETRY_CODES,
        )
        from jasper.active_speaker.session_volume_plan import SessionVolumePlanError
        from jasper.correction.coordinator import MeasurementWindowError

        def complete_capture_set() -> None:
            """The household's "all spots measured — Continue" tap (D1).

            **This is the group-close seam at the host boundary**, and the
            only thing that fits a correction in stage 1. It replaces the
            inference this runner used to make — that VERIFY's begin, the one
            index past the walked cloud, WAS the confirmation — which stage 1
            structurally cannot supply because it has no VERIFY entry.

            What it deliberately does NOT do any more is apply. Until PR-T3
            this function's predecessor (``_fire_auto_apply``) started the
            apply transaction on its own background thread the instant the
            group closed: unconditionally, inside the relay session, three
            seconds before VERIFY, with the household holding a phone. The
            2026-07-28 ruling made the review interlude the apply decision
            point, so the candidate this builds is a PROPOSAL and the apply is
            the household's own POST to ``/correction/crossover/v2/apply``.

            The persist is load-bearing and must happen here: ``handle_v2_apply``
            reads the candidate off the DURABLE state, not off the conductor,
            so a confirmation whose fit never reached disk would leave the
            review screen with nothing to review.

            A refusal (the PR-L4 accountability veto raises
            ``CaptureBeginRefused``) propagates to the runner, which publishes
            it to the phone and ends the session exactly as an admission
            refusal does.
            """
            # Persist FIRST, before the close runs. The combine plus the fit
            # are the slowest thing in the session, the wizard renders from
            # durable state, and until this write landed the speaker page kept
            # telling the household to confirm on their phone for the several
            # seconds after they already had.
            conductor.note_group_close_started()
            persist_conductor_state(
                conductor, failure_code=None, evidence=evidence_refs,
            )
            confirmed = conductor.confirm_cloud_measure_group()
            if confirmed is not None:
                persist_conductor_state(
                    conductor, failure_code=None, evidence=evidence_refs,
                )

        def authorize(index: int, attempt: int, entry: Any = None) -> None:
            # Admission, and ONLY admission. The group close used to run here
            # first, inferred from a begin whose index was past the walked
            # cloud, and fired the apply behind it; both moved to
            # ``complete_capture_set`` above, on the household's explicit
            # signal (work order D1). Nothing on this path applies anything.
            with stop_lock:
                if stop_event.is_set():
                    raise CaptureStopped("capture stopped")
            conductor.authorize_begin(index, attempt, entry)

        def completion_signal_required() -> bool:
            """Whether this set is the household's to end (D1).

            True exactly while the pre-apply cloud is walked and unconfirmed —
            the window in which a voluntary retake of the final position still
            means something, and the window the runner must not close by
            arithmetic. Every other session shape (the post-apply session, the
            recovery re-verify) never stashes a pre-apply group, so this is
            always False there and the runner ends those sets exactly as it
            always has.

            **This is decoupled from the candidate, and has to stay that way**
            (eager-fit rider on #1806, shipped 2026-07-30). The predicate used
            to resolve through ``self._candidate is None``, which is ALSO the
            group close's fire-once guard — so the rider, which fits a
            candidate BEFORE the household confirms, would have flipped this to
            False and un-held the runner's set, shutting the retake window in
            the same instant, silently. It now resolves through
            ``_group_confirmed``: the held-set question is "has the household
            confirmed?", never "does a candidate exist?". An eagerly-fitted
            candidate parks in ``_speculative_close`` and is invisible here.

            The two must not be re-merged. If you are tempted, the discriminator
            is ``test_an_eager_fit_failure_surfaces_on_the_confirm_not_before``:
            after a close that RAISED, ``_candidate`` is unset (T3's
            retryability) but the household has confirmed, so this must read
            False. Only the decoupled predicate gets that right. See the
            predicate's own docstring on the conductor.
            """
            return bool(conductor.cloud_measure_group_awaiting_confirm())

        def _post_sweep_phase_best_effort(phase: str, **extra: Any) -> None:
            """Post one phone-visible progress phase (§5.10 progress).

            The capture page's ``waitForSweepComplete``
            (``capture-page/js/main.js``) polls ``host_event.phase`` around its
            own play wait and otherwise sits until ITS OWN timeout elapses —
            the v2 runner posted nothing (W6 run 5), so a real phone could
            never complete a v2 capture.

            The phases this posts, in the order one capture produces them:
            ``prelude_started`` (the courtesy beeps), ``ambient_started``
            (the room-listening window; carries ``duration_s`` and
            ``quiet_requested``), ``sweep_started`` (the tone) — all three from
            :func:`start_program_phase_ladder`, on the program's own clock —
            and finally ``sweep_complete``, the ONLY phase that makes the
            phone's wait return. ``**extra`` carries whatever fields a phase
            declares; the relay relays them verbatim.

            Best-effort: a transient post failure here is a progress-only miss,
            not a capture failure — the existing terminal host event (or the
            phone's own wait timeout) still resolves the phone's wait on any
            real failure.
            """
            armed = conductor.armed_capture
            index, attempt = armed if armed is not None else (None, None)
            try:
                client.post_host_event(
                    pi_session.session_id,
                    pi_session.pull_token,
                    {"phase": phase, "index": index, "attempt": attempt, **extra},
                )
            except (OSError, RuntimeError, ValueError):
                logger.warning(
                    "v2 sweep progress host-event post failed", exc_info=True
                )

        def on_armed(state: Any) -> None:
            if stop_event.is_set():
                raise CaptureStopped("capture stopped")
            # The pre-tone phase ladder (#1824 D4). ``sweep_started`` used to be
            # posted right here, synchronously, BEFORE the play seam had done
            # anything at all — so the phone announced the measurement tone
            # ~4.6 s before the first sound of a courtesy-prelude program and
            # stayed on that line through the beeps, the settle and the room-
            # listening window. The ladder instead posts each phase when it
            # actually becomes audible, anchored at the play path's own WAV
            # handoff (``PlaybackStartSignal``).
            #
            # Backwards-compatible in the one direction that matters: when no
            # play-start signal is wired (a host binding its own play seam, or
            # a test fake), keep the legacy eager post so the phone's wait still
            # resolves rather than sitting until its own timeout.
            cancel_ladder: Callable[[], None] = lambda: None

            def _start_ladder(program: Any) -> None:
                nonlocal cancel_ladder
                cancel_ladder = start_program_phase_ladder(
                    _post_sweep_phase_best_effort, program
                )

            if playback_started is not None:
                playback_started.install(_start_ladder)
            else:
                _post_sweep_phase_best_effort(HOST_PHASE_SWEEP_STARTED)
            # Finding G: on_armed's ``conductor.on_armed`` → ``seams.play`` is a
            # LOCAL seam (the DSP writer lock, CamillaController) — an OSError
            # here (e.g. EROFS opening the lock file) is not a relay-transport
            # death and must not be caught by the relay-death arm below.
            try:
                conductor.on_armed(state)
            except OSError as exc:
                raise CrossoverV2LocalSeamError(str(exc)) from exc
            finally:
                if playback_started is not None:
                    playback_started.clear()
                cancel_ladder()
            _post_sweep_phase_best_effort(HOST_PHASE_SWEEP_COMPLETE)

        def consume(index: int, attempt: int, result: Any, entry: Any = None):
            # Same local-seam boundary as on_armed, for consume_capture's
            # analyze seam.
            try:
                verdict = conductor.consume_capture(index, attempt, result, entry)
            except OSError as exc:
                raise CrossoverV2LocalSeamError(str(exc)) from exc
            code = verdict.get("code") if isinstance(verdict, Mapping) else None
            persist_conductor_state(
                conductor,
                failure_code=code if not verdict.get("accepted") else None,
                evidence=evidence_refs,
            )
            # (An ``auto_apply``-keyed branch lived here until
            # flow-simplification PR-U1, and the flag it read is itself gone
            # since PR-T3 removed auto-apply. It fired the apply off whichever
            # capture verdict carried the flag — MEASURE's accept originally,
            # the CLOUD_MEASURE group close after the 2026-07-27 timing move.
            # §2.6 moved the trigger off a capture verdict entirely and onto
            # the household's confirmation past the walked cloud, so the flag
            # no longer reaches any verdict a production session emits and the
            # branch became unreachable. It is DELETED rather than left as a
            # comment-that-lies: ``authorize`` above is now the only place that
            # fires ``_fire_auto_apply``, which is the whole point of putting
            # the confirm seam at the host boundary.)
            #
            # The EAGER FIT trigger (owner UX direction, 2026-07-30). This
            # verdict flag marks the one accept that leaves a walked, unconfirmed
            # pre-apply cloud — the group close — and a voluntary retake raises
            # it again on ITS accept, which is exactly when a re-fit is wanted:
            # the retake dropped the previous bank when it re-stashed the
            # combine. Started after the persist so the durable state the wizard
            # renders is already the held-window state, and the fit is racing the
            # household's walk rather than the write.
            if verdict.get("awaiting_confirm"):
                _start_speculative_group_close(conductor)
            return verdict

        async def _purge_best_effort() -> None:
            try:
                await asyncio.to_thread(purge, client, pi_session)
            except (OSError, RuntimeError, ValueError):
                logger.warning("v2 relay purge failed", exc_info=True)

        async def _abandon_best_effort() -> None:
            try:
                await volume.abandon()
            except (OSError, RuntimeError, ValueError):
                log_event(
                    logger,
                    "correction.crossover_v2_volume_abandon_failed",
                    level=logging.CRITICAL,
                )

        async def _post_terminal_failure_host_event(code: str) -> None:
            """Tell the phone the session is over so it stops waiting (§5.10).

            A play-seam failure escapes ``run_capture_plan`` WITHOUT posting a
            capture verdict, so the phone records into silence and then polls
            ``capture_result`` forever (W6.1 hardware run 2 froze at
            ``capture_authorized``). Address a terminal ``capture_result``
            (accepted=false, carrying the §5.10 reason so the phone can render
            the failure screen) to the armed capture; fall back to
            ``capture_set_exhausted`` when no capture was armed. Best-effort —
            the operator wizard also shows the persisted failure.
            """
            spec = REASON_REGISTRY.get(code)
            armed = conductor.armed_capture
            if armed is not None and spec is not None:
                index, attempt = armed
                event: dict[str, Any] = {
                    "phase": HOST_PHASE_CAPTURE_RESULT,
                    "index": index,
                    "attempt": attempt,
                    "accepted": False,
                    "code": spec.code,
                    "template": spec.template,
                    "reason": spec.message or spec.banner,
                    "banner": spec.banner,
                    "auto_retry": spec.code in TRANSIENT_AUTO_RETRY_CODES,
                }
            else:
                event = {"phase": HOST_PHASE_CAPTURE_SET_EXHAUSTED}
            try:
                await asyncio.to_thread(
                    client.post_host_event,
                    pi_session.session_id,
                    pi_session.pull_token,
                    event,
                )
            except (OSError, RuntimeError, ValueError):
                logger.warning(
                    "v2 terminal host-event post failed", exc_info=True
                )

        async def _post_session_over_host_event(budget: str = "") -> None:
            """Tell the phone the whole SESSION ended so its deferred-retry loop
            stops waiting (W6.10 blocker #3).

            ``budget`` names WHICH clock ran out (work order D8, issue #1807),
            or ``""`` when the death was not a timeout at all. The wire always
            carries the field: ``""`` is published as ``TIME_BUDGET_NONE``
            rather than omitted (issue #2083). Omitting it made the phone render
            this event as ``renderPlanExhausted`` — "the speaker reached its
            measurement attempt limit" — which is untrue of BOTH the cases that
            reach here. It is untrue of an expiry (no attempt limit was reached,
            a clock ran out) and untrue of a transport death (no clock ran out
            either, the relay went away), and the three want different things
            from the household. An older page treats the explicit ``"none"`` as
            an unnamed budget and behaves exactly as it does today.

            A watchdog collapse during the "waiting for apply" REVIEW hold
            (``CaptureTimeout``) otherwise left the phone re-posting the same
            ``begin_capture`` against a still-200 relay session with NO terminal
            signal — it sat on the hold screen forever (Chrome round 2: "the
            phone saw nothing"). Unlike ``_post_terminal_failure_host_event``
            this is session-level (``capture_set_exhausted``), not addressed to
            the last-armed capture (MEASURE was accepted — a per-index
            ``capture_result`` there would misreport it): the collapse is not a
            per-capture verdict. Best-effort — the purge-driven 404 the phone
            reads as ``deadSession`` is the backstop, and this post fails
            harmlessly when the failure was the relay transport itself.
            """
            try:
                await asyncio.to_thread(
                    client.post_host_event,
                    pi_session.session_id,
                    pi_session.pull_token,
                    {
                        "phase": HOST_PHASE_CAPTURE_SET_EXHAUSTED,
                        "budget": budget or TIME_BUDGET_NONE,
                    },
                )
            except (OSError, RuntimeError, ValueError):
                logger.warning(
                    "v2 session-over host-event post failed", exc_info=True
                )

        try:
            opened = await volume.open()
        except (SessionVolumePlanError, MeasurementWindowError) as exc:
            # volume.open() raised BEFORE the capture loop owns cleanup — the
            # relay session is already minted (run 2's retry leaked one here
            # when the prior session's volume state was still open, firing
            # SessionVolumePlanError). Purge it best-effort before surfacing so
            # it cannot linger to worker TTL; the volume hook already released
            # any measurement pause it took.
            log_event(
                logger,
                "correction.crossover_v2_volume_open_failed",
                level=logging.WARNING,
                reason=type(exc).__name__,
            )
            await _purge_best_effort()
            raise CaptureFailed(
                "the measurement volume could not be opened"
            ) from exc
        opened_value = getattr(opened, "value", opened)
        if opened is not None and str(opened_value) != "opened":
            # The plan drained itself (emergency attenuation / failure); the
            # recovery screen keys on needs_recovery via the status block.
            # The freshly-minted relay session must not linger to worker TTL
            # when no capture will ever run against it.
            await _purge_best_effort()
            raise CaptureFailed(
                "the fixed measurement volume could not be confirmed"
            )
        plan_kwargs: dict[str, Any] = {}
        if poll_interval_s is not None:
            plan_kwargs["poll_interval_s"] = poll_interval_s
        if timeout_s is not None:
            plan_kwargs["timeout_s"] = timeout_s
        # The FIRST begin gets the wider v2 placement-reading budget (fold-in);
        # every later window (arm/upload/between-capture) keeps the tight
        # per-phase backstop. The REVIEW hold's own rescope lives in the runner.
        plan_kwargs["first_begin_timeout_s"] = (
            first_begin_timeout_s if first_begin_timeout_s is not None
            else V2_FIRST_BEGIN_TIMEOUT_S
        )
        capture_task = asyncio.create_task(
            asyncio.to_thread(
                run_capture_plan,
                client,
                pi_session,
                authorize_begin=authorize,
                on_armed=on_armed,
                consume_capture=consume,
                stop_requested=stop_event.is_set,
                # The held-set pair (work order D1): stage 1's final cloud
                # position IS its capture target, so without these the runner
                # would end the set on arithmetic — the fit would never run,
                # the household's retake window would shut at the same moment,
                # and the review screen would have nothing to review.
                completion_signal_required=completion_signal_required,
                on_completion_signal=complete_capture_set,
                **plan_kwargs,
            )
        )
        try:
            try:
                await asyncio.shield(capture_task)
            except asyncio.CancelledError:
                stop_event.set()
                while not capture_task.done():
                    try:
                        await asyncio.shield(capture_task)
                    except asyncio.CancelledError:
                        continue
                    except (OSError, RuntimeError, ValueError):
                        break
                if capture_task.done() and not capture_task.cancelled():
                    capture_task.exception()
                await _abandon_best_effort()
                await _purge_best_effort()
                raise
        except CaptureStopped:
            await _abandon_best_effort()
            await _purge_best_effort()
            raise
        except CaptureBeginRefused:
            # The conductor's own budget refusal — its failure code is already
            # in _last_reason; persist the terminal state.
            code = conductor.last_failure_code or REASON_RELAY_TIMEOUT
            _persist_terminal_failure(conductor, code)
            await _abandon_best_effort()
            await _purge_best_effort()
            raise
        except (CaptureTimeout, CaptureAborted, CaptureFailed, RelayError, OSError) as exc:
            # Relay-session death (§5.10): relay_timeout ⇒ session restart; the
            # walked-away user's volume is always drained. Tell the phone the
            # session is over BEFORE purging (W6.10 blocker #3) — mirror the
            # catch-all arm's terminal-then-grace-then-purge so a watchdog
            # collapse during the apply hold reaches the phone's deferred-retry
            # loop instead of leaving it polling a still-live session forever.
            #
            # A deliberate phone Stop (CaptureAborted, reason == "stopped") is
            # NOT a relay-transport death — it is the household explicitly
            # ending the measurement. Splitting it out gives it its own honest
            # copy instead of the dishonest "the measurement link timed out"
            # claim every other death in this tuple gets.
            code = REASON_RELAY_TIMEOUT
            if isinstance(exc, CaptureAborted) and exc.reason == "stopped":
                code = REASON_USER_STOPPED
            elif (
                isinstance(exc, CaptureTimeout)
                and conductor.current_phase == PHASE_APPLYING
            ):
                # The deferred apply/"review" hold (CaptureBeginDeferred
                # "awaiting_apply") expired: MEASURE was accepted but the
                # conductor's own auto-apply never landed within
                # REVIEW_HOLD_BUDGET_S. RETAINED but unreached since PR-T3
                # (D10): no shipped session parks on that hold any more, so
                # this arm cannot fire in production — kept with the hold it
                # classifies. current_phase is PHASE_APPLYING ONLY in
                # that exact window (MEASURE accepted, VERIFY pending, apply not
                # observed), so it cleanly separates a hold expiry from a
                # generic transport death (#1605) — name the real cause instead
                # of the dishonest "the measurement link timed out". A rare
                # apply-landed-but-phone-bailed race still renders honestly: the
                # envelope's applied-keyed override keys on durable
                # ``applied``, not on this phase.
                code = REASON_REVIEW_HOLD_TIMEOUT
            # WHICH clock ran out, named once and disclosed everywhere (work
            # order D8, issue #1807). Both a step expiry and the relay TTL
            # arrive in this arm and both persist as REASON_RELAY_TIMEOUT, so
            # before this line the only surface that could tell them apart was
            # the exception's own message — and the household saw "the speaker
            # reached its measurement attempt limit", which is neither of them.
            budget = expired_time_budget(exc)
            if budget:
                log_event(
                    logger,
                    "correction.crossover_v2_time_budget_expired",
                    level=logging.WARNING,
                    budget=budget,
                    phase=str(getattr(exc, "phase", "") or ""),
                    conductor_phase=conductor.current_phase,
                    # What the expiry PRESERVED: the phases whose captures were
                    # accepted before the clock ran out. They are on disk as
                    # evidence; they are not a set the next session resumes
                    # from, and the phone's copy says so rather than implying a
                    # resume that does not exist.
                    accepted_phases=",".join(sorted(conductor.accepted_phases)),
                )
            await _post_session_over_host_event(budget)
            _persist_terminal_failure(conductor, code)
            await _abandon_best_effort()
            await asyncio.sleep(TERMINAL_FAILURE_PURGE_GRACE_S)
            await _purge_best_effort()
            raise
        except Exception as exc:  # noqa: BLE001 — cleanup-and-reraise, see below
            # CATCH-ALL cleanup arm (W6.1 gate ruling). The seams raise
            # open-endedly — CamillaUnavailable is a bare Exception (a DSP
            # wedge in load/restore escaped the previously-enumerated arms:
            # volume left active, relay session leaked, phone frozen at
            # capture_authorized), analyze/emit raise ValueError/RuntimeError,
            # the held measurement window raises MeasurementWindowError — so
            # ANY non-relay failure gets the same honest cleanup: tell the
            # phone (still polling capture_result), persist a terminal
            # failure, drain the volume (whose hook also releases the session
            # measurement pause), purge the relay session, then RE-RAISE so
            # the outer relay net still logs and flips /status.relay to
            # failed. Program-side classes keep their own honest code via
            # ``classify_program_failure`` (issue #1820: a not-confirmed safety
            # profile is NOT the same failure as a level ceiling, and the
            # underlying refusal slugs ride out with it); everything else is
            # internal_error.
            classified = classify_program_failure(exc)
            code = classified[0] if classified else REASON_INTERNAL_ERROR
            refusals = classified[1] if classified else ()
            if classified:
                log_event(
                    logger,
                    "correction.crossover_v2_program_failure",
                    level=logging.WARNING,
                    code=code,
                    refusals=",".join(refusals),
                    error_type=type(exc).__name__,
                )
            await _post_terminal_failure_host_event(code)
            _persist_terminal_failure(conductor, code, refusals=refusals)
            await _abandon_best_effort()
            # Finding H: give the just-posted terminal host event a bounded
            # grace window to reach the phone before the session is purged
            # out from under its next poll. Volume restore above stays
            # immediate — only the purge waits.
            await asyncio.sleep(TERMINAL_FAILURE_PURGE_GRACE_S)
            await _purge_best_effort()
            raise
        # Plan finished without a transport failure.
        done = conductor.current_phase == PHASE_DONE
        persist_conductor_state(
            conductor,
            failure_code=None if done else conductor.last_failure_code,
            evidence=evidence_refs,
        )
        if done:
            try:
                await volume.close()
            except (OSError, RuntimeError, ValueError):
                log_event(
                    logger,
                    "correction.crossover_v2_volume_close_failed",
                    level=logging.CRITICAL,
                )
        else:
            await _abandon_best_effort()
        await _purge_best_effort()

    return _run_and_consume


# --------------------------------------------------------------------------- #
# conductor context resolution (production inputs)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class V2ConductorContext:
    """Everything the production conductor needs, resolved from live status."""

    preset: Any
    roles_bands: tuple
    fc_hz: float
    driver_caps_dbfs: dict[str, float]
    role_targets: dict[str, str]
    safety_profile: Mapping[str, Any]
    session_volume_db: float
    driver_spacing_m: float
    topology: Any
    playback_device: str
    role_channels: dict[str, int]
    # Per-role declared EFFECTIVE sensitivities (naked datasheet figure with
    # any declared in-line pad folded in — #1665) from the design draft's
    # declaration (the one owner of that fact — W6.5). Threaded into every cap
    # resolution AND the play-time readmission so the composed levels and the
    # admission gate can never disagree about a derived HF ceiling.
    declared_sensitivities: dict[str, float] = field(default_factory=dict)
    # Per-role declared driver technology class (#1665 component entry), fed
    # to the conductor's Layer-1a linearization fit
    # (linearization_envelope.compose_envelope's class_prior_limit term —
    # see crossover_v2_flow.CrossoverV2Conductor's own driver_class_by_role
    # ctor param, landed by #1668 PR-C ahead of this resolver populating it).
    # A role absent here fits under the conservative "unknown" class default.
    driver_class_by_role: dict[str, str] = field(default_factory=dict)
    # Flat-linearization plan PR-4: the tweeter's confirmed
    # ``measurement_band_hz`` — the contract-derived echo/null analysis band
    # replacing DEFAULT_ECHO_BAND_HZ's flat constant at the cloud-group
    # pipeline's call site (crossover_v2_flow.CrossoverV2Conductor's own
    # tweeter_measurement_band_hz ctor param). ``None`` degrades to that
    # module default, never a refused session — a declared-metadata gap here
    # is not a reason to block a measurement the household is entitled to run.
    tweeter_measurement_band_hz: tuple[float, float] | None = None


def ensure_crossover_preview_ready() -> dict[str, Any]:
    """Ensure a ready crossover preview exists before a v2 session reads one.

    ``/sound/``'s Preview button was the ONLY historical writer of
    ``active_speaker_crossover_preview.json``; the v2 flow never called it, so
    a household that went straight to ``/correction/`` without visiting
    ``/sound/`` first baked its MEASURE candidate's ``source_preset`` against
    the generic bundled-preset fallback (:func:`~jasper.active_speaker.commission_wiring.resolve_capture_preset`'s
    no-preview branch) — which then can NEVER match a preview generated later,
    so Apply refuses ``measured_candidate_preset_mismatch`` forever. This is
    called at the top of :func:`resolve_conductor_context` — the one place
    both session-open (:func:`prepare_v2_session`) and the verify re-arm
    (:func:`prepare_v2_verify`) resolve the design draft/topology — so the
    fallback branch is never reached from a v2 entry point again.

    Reuses the SAME generator ``/sound/`` drives
    (:func:`~jasper.active_speaker.web_commissioning.regenerate_crossover_preview_from_current_draft`,
    itself a thin wrapper around :func:`~jasper.active_speaker.crossover_preview.save_crossover_preview`)
    rather than reimplementing preview generation. Idempotent: an existing
    preview that is already ``ready_for_protected_staging`` for the CURRENT
    design draft (the freshness/fingerprint check already built into
    :func:`~jasper.active_speaker.crossover_preview.load_crossover_preview`)
    is left byte-untouched — reused, not regenerated. Anything else (absent,
    stale, or blocked) is regenerated once; if the fresh attempt still cannot
    reach ``ready_for_protected_staging`` (an unconfirmed safety profile, a
    blocked design draft, etc.), this raises a named :class:`CrossoverV2Refused`
    pointing at ``/sound/`` instead of leaving the surprise for apply time.
    """
    from jasper.active_speaker.crossover_preview import load_crossover_preview
    from jasper.active_speaker.design_draft import load_design_draft
    from jasper.active_speaker.web_commissioning import (
        regenerate_crossover_preview_from_current_draft,
    )

    preview = load_crossover_preview(current_design_draft=load_design_draft())
    outcome = "reused"
    if preview.get("status") != "ready_for_protected_staging":
        preview = regenerate_crossover_preview_from_current_draft()
        outcome = (
            "generated"
            if preview.get("status") == "ready_for_protected_staging"
            else "refused"
        )
    log_event(
        logger,
        "correction.crossover_v2_preview_ensured",
        outcome=outcome,
        preview_status=str(preview.get("status")),
    )
    if outcome == "refused":
        messages = [
            str(issue.get("message") or issue.get("code"))
            for issue in (preview.get("issues") or [])
            if isinstance(issue, Mapping) and issue.get("severity") == "blocker"
        ]
        raise CrossoverV2Refused(
            "the crossover preview is not ready for measurement; finish "
            "speaker setup at /sound/ first"
            + (": " + "; ".join(messages[:2]) if messages else "")
        )
    return preview


def _resolve_driver_class_by_role(draft: Mapping[str, Any]) -> dict[str, str]:
    """Per-role declared driver technology class (#1665 component entry).

    Mirrors :func:`jasper.active_speaker.design_draft.declared_driver_sensitivities`'s
    exact shape — role-keyed, a role with disagreeing declarations drops
    entirely, fails soft on anything malformed rather than raising. This
    resolver runs inside conductor-context resolution: an unexpected value
    should fall back to :func:`~jasper.active_speaker.linearization_envelope.compose_envelope`'s
    own conservative "unknown" default for that one role, never abort the
    whole session.
    """

    from jasper.active_speaker._common import DRIVER_CLASSES

    manual = draft.get("manual_settings") if isinstance(draft, Mapping) else None
    if not isinstance(manual, Mapping):
        return {}
    drivers = manual.get("drivers")
    out: dict[str, str] = {}
    conflicted: set[str] = set()
    for driver in drivers if isinstance(drivers, list) else []:
        if not isinstance(driver, Mapping):
            continue
        role = str(driver.get("role") or "")
        value = driver.get("driver_class")
        if not role or not isinstance(value, str) or value not in DRIVER_CLASSES:
            continue
        if role in out and out[role] != value:
            conflicted.add(role)
            continue
        out[role] = value
    for role in conflicted:
        out.pop(role, None)
    return out


def resolve_conductor_context(status: Mapping[str, Any]) -> V2ConductorContext:
    """Resolve preset/bands/caps/targets/volume from live status + topology.

    Fail-closed: every missing input is a :class:`CrossoverV2Refused` naming
    what to finish first — never a guessed default.

    This runs at SESSION OPEN — ``prepare_v2_session`` calls it before the
    relay session is registered and the phone link minted, and
    ``prepare_v2_verify`` before its re-arm — which is what makes the driver-
    safety-profile gate below a pre-flight rather than a surprise (issue
    #1821). Before that gate existed, this function checked only that a
    profile object was PRESENT while its refusal text claimed confirmation had
    been checked; the real confirmation gate lived four screens later inside
    ``prepare_driver_excitation_plan`` at CHECK-phase program admission. A
    household with an un-confirmed profile therefore burned a link, walked to
    the phone, and hit a deterministic refusal that was knowable before any of
    it — the exact 2026-07-28 JTS3 dead-end.
    """
    from jasper.active_speaker.commission_wiring import resolve_capture_preset
    from jasper.active_speaker.crossover_v2_flow import (
        REASON_REGISTRY,
        derive_session_volume_db,
    )
    from jasper.active_speaker.design_draft import (
        declared_effective_driver_sensitivities,
        load_design_draft,
    )
    from jasper.active_speaker.driver_safety import evaluate_driver_safety_profile
    from jasper.active_speaker.excitation_safety_plan import (
        ExcitationSafetyPlanError,
        resolve_driver_excitation_ceilings,
        resolve_driver_measurement_band_hz,
    )
    from jasper.active_speaker.playback_route import resolve_active_playback_device
    from jasper.audio_measurement.program import RoleBand
    from jasper.output_topology import load_output_topology

    if not status.get("active"):
        raise CrossoverV2Refused(
            "this speaker has no active crossover to measure"
        )
    setup = status.get("setup")
    if not isinstance(setup, Mapping) or setup.get("status") != "ready":
        raise CrossoverV2Refused(
            "protected speaker setup is not ready; finish it before measuring"
        )
    topology = load_output_topology()
    # Ensure a ready crossover preview BEFORE resolving the capture preset —
    # otherwise resolve_capture_preset's no-preview fallback silently bakes
    # the generic bundled preset into every MEASURE candidate (see docstring).
    ensure_crossover_preview_ready()
    preset = resolve_capture_preset(topology)
    if preset.way_count != 2:
        raise CrossoverV2Refused("the v2 conductor flow is scoped to 2-way presets")
    draft = load_design_draft(topology=topology)
    safety_profile = draft.get("driver_safety_profile")
    # The gate the refusal text has always CLAIMED (issue #1821). Evaluated
    # against the live topology, so a stale profile (an output change) and an
    # un-confirmed one (a driver-detail edit rotated the fingerprint) are both
    # caught here rather than at play time. Copy comes from the SAME registry
    # entry the phone's terminal failure screen renders, so the two surfaces
    # cannot say different things about the same missing confirmation.
    safety_evaluation = evaluate_driver_safety_profile(safety_profile, topology)
    if not safety_evaluation.confirmed_and_current or not isinstance(
        safety_profile, Mapping
    ):
        code = profile_refusal_code(safety_evaluation.status)
        log_event(
            logger,
            "correction.crossover_v2_profile_not_confirmed",
            level=logging.WARNING,
            gate="session_open",
            profile_status=safety_evaluation.status,
            code=code,
            reasons=",".join(safety_evaluation.reasons),
        )
        raise CrossoverV2Refused(REASON_REGISTRY[code].message, code=code)
    targets_raw = status.get("targets")
    drivers = (
        targets_raw.get("drivers") if isinstance(targets_raw, Mapping) else None
    ) or []
    role_targets: dict[str, str] = {}
    for target in drivers:
        if isinstance(target, Mapping):
            role = str(target.get("role") or "").lower()
            fingerprint = str(target.get("target_fingerprint") or "")
            if role and fingerprint:
                role_targets[role] = fingerprint
    if set(role_targets) != {"woofer", "tweeter"}:
        raise CrossoverV2Refused(
            "the woofer and tweeter measurement targets are not both active"
        )
    # The declaration's per-role EFFECTIVE datasheet sensitivities -- naked
    # figure with any declared in-line pad folded in (#1665) -- threaded into
    # every cap resolution below. This is the one owner of that fact (W6.5).
    declared_sensitivities = declared_effective_driver_sensitivities(draft)
    # The declaration's per-role driver technology class (#1665), threaded
    # into the conductor construction sites below so the Layer-1a
    # linearization fit (compose_envelope's class_prior_limit term) sees it.
    driver_class_by_role = _resolve_driver_class_by_role(draft)
    roles_bands = []
    caps: dict[str, float] = {}
    for channel, role in enumerate(("woofer", "tweeter")):
        try:
            # program_admission=True: this context exists solely to serve the
            # admission-gated CHECK/MEASURE programs, whose channel routing
            # carries each driver's crossover filter (the tweeter's protective
            # HP included) by construction — the proven-HP path, the same
            # justification as session_volume_plan. Without it the W6.5
            # derived HF ceiling is inert exactly where it matters: these
            # context caps clamp every composed level (CHECK pilot bases,
            # MEASURE back_off_gain, VERIFY min(caps)).
            band, cap = resolve_driver_excitation_ceilings(
                safety_profile,
                role_targets[role],
                program_admission=True,
                declared_sensitivities=declared_sensitivities,
            )
        except (ExcitationSafetyPlanError, ValueError) as exc:
            raise CrossoverV2Refused(
                f"the {role}'s safe excitation limits could not be resolved"
            ) from exc
        roles_bands.append(RoleBand(role, channel, band))
        caps[role] = float(cap)
    # Flat-linearization plan PR-4: the tweeter's confirmed measurement band —
    # by the time this loop above has succeeded for role="tweeter",
    # resolve_driver_excitation_ceilings has ALREADY validated this exact
    # field on the same confirmed record (see its "Band-edge asymmetry"
    # docstring paragraph), so this call is not expected to raise here; it is
    # still wrapped, because a declared-metadata gap on this NEW, optional
    # surface must never turn into a refused measurement session — the
    # conductor's own tweeter_measurement_band_hz ctor param degrades to the
    # module default on None.
    try:
        tweeter_measurement_band_hz = resolve_driver_measurement_band_hz(
            safety_profile, role_targets["tweeter"],
        )
    except (ExcitationSafetyPlanError, ValueError):
        tweeter_measurement_band_hz = None
    region = preset.crossover_regions[0]
    fc_hz = float(region.fc_hz)
    session_volume_db = derive_session_volume_db(
        safety_profile,
        [role_targets["woofer"], role_targets["tweeter"]],
        declared_sensitivities=declared_sensitivities,
    )
    playback_device, _playback_device_source = resolve_active_playback_device(
        topology
    )
    playback_device = str(playback_device or "")
    if not playback_device:
        raise CrossoverV2Refused(
            "the active output device is not declared; finish speaker setup"
        )
    return V2ConductorContext(
        preset=preset,
        roles_bands=tuple(roles_bands),
        fc_hz=fc_hz,
        driver_caps_dbfs=caps,
        role_targets=role_targets,
        safety_profile=safety_profile,
        session_volume_db=session_volume_db,
        # W6 CHECKLIST ITEM: driver_spacing_m stays 0.0 until a declared
        # woofer↔tweeter spacing input exists (topology/preset carry none
        # today), so the §3.2 parallax correction is INERT — the analysis
        # subtracts nothing. Do not assume VERIFY covers this: a missing
        # parallax correction is SELF-CANCELLING at the mic position (the
        # same geometric excess is baked into both MEASURE and VERIFY), so
        # VERIFY passes while the LISTENING POSITION carries the full error
        # (~23° at 2 kHz for 15 cm spacing measured at 1 m).
        # W6 CHECKLIST ITEM (pre-existing): a deliberate household volume
        # action mid-session (remote / voice "louder" / :8780 HTTP) still moves
        # the CamillaDSP main volume — the session measurement pause holds off
        # the idle reconciler, not VolumeCoordinator writes. W6 validation
        # runs hands-off; a session-long volume guard is a follow-up.
        driver_spacing_m=0.0,
        topology=topology,
        playback_device=playback_device,
        role_channels={"woofer": 0, "tweeter": 1},
        declared_sensitivities=declared_sensitivities,
        driver_class_by_role=driver_class_by_role,
        tweeter_measurement_band_hz=tweeter_measurement_band_hz,
    )


# The key :func:`attach_stage2_preflight` writes onto ``status["crossover_v2"]``
# and :func:`~jasper.active_speaker.crossover_envelope_v2._stage2_preflight`
# reads. Spelled once, here, because the writer and the reader live in
# different packages and a literal in each is how they drift.
STAGE2_PREFLIGHT_KEY = "stage2_preflight"


def attach_stage2_preflight(status: MutableMapping[str, Any]) -> None:
    """Run the stage-2 openability predicate for the REVIEW screen (D3).

    Two-stage commission work order D3: *"The review screen runs
    ``resolve_conductor_context``'s predicate — the same fail-closed resolution
    ``prepare_v2_verify`` will run — twice: once at review render, and again
    server-side immediately before the apply commits."* This is the render-time
    half. **The pre-POST half landed with PR-T3** and lives in
    :func:`_assert_stage_2_can_open`, which ``handle_v2_apply`` calls
    immediately before the transaction commits. Neither is redundant: a
    disabled control is not a security boundary, and a stale page, a second
    tab, or a direct POST all reach the endpoint without ever rendering this.

    Without it, the failure mode is exactly the applied-and-ungraded end state
    the whole work order exists to eliminate — premise 5: a box can be applied
    and still be unable to open stage 2, because ``prepare_v2_verify``'s very
    next line is ``resolve_conductor_context(status)`` and that carries seven
    refusal sites of its own plus ``ensure_crossover_preview_ready()``'s. The
    household applies, stage 2 refuses at open, and the speaker sits corrected
    with no verdict.

    **The SAME predicate, not a cheaper lookalike.** A second, narrower copy of
    the rule here would be free to disagree with the one stage 2 actually runs,
    and agreement is the only thing this control's honesty rests on. Most of
    the seven gates ARE reachable more cheaply off the status mapping
    (``active``, ``setup``, ``targets.drivers``, and
    ``driver_safety_profile_evaluation`` — which ``status_payload`` already
    computes), but re-deriving them here would rebuild the predicate minus its
    2-way-preset, excitation-ceiling, playback-device, and preview-readiness
    gates, which is precisely how a screen ends up promising an apply that
    stage 2 then refuses.

    **What it costs, re-derived against what the code now does.** PR-T2 argued
    this cost nothing because ``PHASE_REVIEW`` was unreachable — no
    ``index_phase_map`` could omit VERIFY — and named T3 as the rung that would
    falsify that. It did: stage 1's map omits VERIFY by design, so the review
    phase is real and this predicate runs for it.

    One call is roughly six JSON reads (the topology and design draft are each
    read more than once through different loaders), a canonical-JSON SHA-256
    profile fingerprint, and a preset compile. It is not free of side effects:
    ``ensure_crossover_preview_ready()`` can write BOTH
    ``active_speaker_crossover_preview.json`` and the topology file, and emits
    ``correction.crossover_v2_preview_ensured`` on every call, ready or not.
    The writes are self-limiting rather than per-call, though — that function
    regenerates only a preview that is absent, stale, or blocked, and leaves an
    already-ready one byte-untouched. No subprocess, no network, no
    audio-device probing.

    **T2's second bound was WRONG, and the correction is the gate below.** T2
    reasoned that the review interlude is never polled because stage 1's
    session has ended by the time it renders, and named the one shape the
    design would not survive: a review screen rendering beside a live relay,
    turning those writes into a 1.5 s loop. That shape was real. Accepting the
    final cloud position resolves every stage-1 phase, so the phase flipped the
    instant that capture landed — while the runner was parked in D1's held-set
    window (up to the full between-step budget) and the wizard was polling at
    1.5 s. Measured mid-hold: ~80 calls per hold.

    Two things bound it now, both structural rather than a cache. The
    **candidate gate** below is the real one: with no candidate there is
    nothing to apply, so there is nothing to preflight — which is exactly the
    held-set window, the fit-in-flight window, and the refusal lane, and it
    reduces all three to a dict lookup. (Those first two no longer resolve to
    ``review`` at all since ``PHASE_CLOSING`` exists, so the gate is
    belt-and-braces there; it is load-bearing for the refusal lane, where a
    spec report exists with no candidate behind it.) And it runs ONLY on the
    review phase, so no other screen pays anything.

    **Fail-closed in both directions.** A refusal disables Apply and hands the
    screen the refusal's own sentence; an UNEXPECTED exception does the same
    rather than defaulting to permission, because "we could not check" and "we
    checked and it is fine" must never render as the same screen. Absence of
    the key is likewise not permission — see the envelope's own reader.

    Mutates ``status`` in place (the established shape on this path:
    ``handle_status`` sets ``payload["relay"]`` the same way) so the envelope
    builder stays the pure ``status → envelope`` function it is. It has to be
    computed HERE, in the web layer, because ``resolve_conductor_context`` is a
    ``jasper.web`` function and ``jasper.active_speaker`` never imports from
    ``jasper.web`` — the envelope module reaching for it directly would be the
    first such import in the package.
    """
    from jasper.active_speaker.crossover_v2_flow import PHASE_REVIEW

    v2 = status.get("crossover_v2")
    if not isinstance(v2, MutableMapping) or v2.get("phase") != PHASE_REVIEW:
        return
    # No candidate ⇒ nothing to apply ⇒ nothing to preflight. See the cost
    # paragraph above: this is what keeps the predicate off a polled screen.
    # Absence of the key it would have written is NOT permission — the
    # envelope's own reader treats it as a refusal — and with no candidate the
    # Apply control does not render at all, so nothing is being hidden.
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
        # A user-visible dead end gets a named line nobody has to guess at
        # (AGENTS.md's no-silent-failure rule) — the household sees a disabled
        # Apply, and this is where an operator reads why.
        log_event(
            logger,
            "correction.crossover_v2_stage2_preflight_refused",
            level=logging.WARNING,
            code=str(getattr(exc, "code", "") or ""),
            detail=message,
        )
        return
    except (OSError, RuntimeError, TypeError, ValueError):
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


@dataclass(frozen=True)
class V2PreparedSession:
    """What the correction_setup dispatch needs to host one v2 relay session."""

    label: str
    open: Callable[..., Any]
    run_and_consume: Callable[[Any, Any], Any]
    request_stop: Callable[[], None]


def _volume_hooks(camilla_factory: Any, context: V2ConductorContext) -> V2VolumeHooks:
    plan = session_volume_plan()
    # The shared IO pair converts CamillaUnavailable (a bare Exception) into
    # RuntimeError, which plan.open's internal catches and set_and_confirm's
    # fail-closed contract actually cover — a DSP wedge during open then drains
    # to a non-OPENED result instead of raising an unhandled exception past the
    # runner (the W6.1 gate's escape class).
    _set, _get = _session_volume_io(camilla_factory)

    async def _open() -> Any:
        # Hold voice paused for the WHOLE session BEFORE setting the volume, so
        # the idle reconciler cannot revert the measurement volume in the gap
        # before the first play (W6.1). If the volume does not actually open —
        # a non-OPENED result OR any raise — release the pause in the finally
        # so a failed open never strands voice paused.
        await acquire_session_measurement_pause()
        opened: Any = None
        try:
            opened = await plan.open(context.session_volume_db, _set, _get)
        finally:
            if str(getattr(opened, "value", opened)) != "opened":
                await release_session_measurement_pause()
        return opened

    async def _close() -> Any:
        try:
            return await plan.close(_set, _get)
        finally:
            await release_session_measurement_pause()

    async def _abandon() -> Any:
        try:
            return await plan.abandon(_set, _get)
        finally:
            await release_session_measurement_pause()

    return V2VolumeHooks(open=_open, close=_close, abandon=_abandon)


def prepare_v2_session(
    raw: Mapping[str, Any],
    *,
    status: Mapping[str, Any],
    run_async: Any,
    camilla_factory: Any,
) -> V2PreparedSession:
    """Prepare the ``POST /crossover/v2/session`` relay hosting (S1a).

    Gates (fail-closed, before any relay registration): the volume-recovery
    gate (``needs_recovery`` — the W2 ruling) and the conductor-context
    resolution. Hydration (S1d): the durable state is
    hydrated through :meth:`CrossoverV2Conductor.hydrate` with the NEW relay
    session id — a prior session's CHECK/MEASURE evidence is invalidated per
    §5.6 (and logged); the fresh session starts at CHECK.

    ``raw["tier"]`` selects the commission instrument (flow-simplification
    §3): the wizard posts the household's explicit choice, an absent value
    means the full instrument, and an unrecognised one is refused before any
    relay registration rather than silently measured as something else. It is
    resolved into ONE :class:`V2PlanShape` here, which is then threaded into
    both the emitted spec and the conductor's index→phase map — the two
    surfaces that must agree about the walk and used to reach their defaults
    independently.
    """
    from jasper.active_speaker.crossover_v2_flow import (
        CrossoverV2Conductor,
        CrossoverV2FlowError,
        V2ConductorSnapshot,
        V2FlowSeams,
        attempt_history_from_state,
        build_v2_cloud_index_phase_map,
        build_v2_session_spec,
        resolve_plan_shape,
        session_wall_clock_ceiling_s,
    )
    from jasper.capture_relay import correction_adapter

    try:
        plan_shape = resolve_plan_shape(raw.get("tier") if raw else None)
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
    evidence_store, _bundle_id = open_v2_evidence_store(context.topology)
    acknowledgement_binding = secrets.token_urlsafe(24)
    stop_event = threading.Event()
    stop_lock = threading.Lock()

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

    holder: dict[str, Any] = {}

    def _open(client: Any, base: str, capture_origin: str, return_url: str) -> Any:
        spec = build_v2_session_spec(
            context.roles_bands,
            context.fc_hz,
            acknowledgement_binding=acknowledgement_binding,
            plan_shape=plan_shape,
            default_setup_calibration=default_setup_calibration_for_v2(),
        ).with_return_url(return_url)
        rc = correction_adapter.open_capture(
            client,
            spec,
            relay_base=base,
            capture_origin=capture_origin,
            return_url=return_url,
        )
        # The conductor + publishers bind to the MINTED relay session id.
        relay_session_id = rc.pi_session.session_id
        publish_check, publish_candidate, refs = bind_evidence_publishers(
            evidence_store, relay_session_id
        )
        # A cloud session legitimately outlasts the 3-entry flow's walked-away
        # ceiling; scale it from the plan this session actually emitted (never
        # from the constants, so a caller-configured cloud is covered too) and
        # arm it BEFORE the volume opens.
        session_volume_plan().set_wall_clock_ceiling_s(
            session_wall_clock_ceiling_s(spec.capture_plan)
        )
        # One signal per session, shared by the play seam (which fires it) and
        # the runner (which installs the armed capture's phase ladder on it).
        playback_started = PlaybackStartSignal()
        from jasper.active_speaker.protected_neutral_graph import (
            build_protected_neutral_session_graph,
        )

        session_graph = build_protected_neutral_session_graph(
            preset=context.preset,
            component_safety_profile=context.safety_profile,
            role_channels=context.role_channels,
            playback_device=context.playback_device,
        )
        play = bind_production_play(
            run_async=run_async,
            camilla_factory=camilla_factory,
            evidence_store=evidence_store,
            relay_session_id=relay_session_id,
            topology=context.topology,
            preset=context.preset,
            role_channels=context.role_channels,
            session_graph=session_graph,
            playback_device=context.playback_device,
            safety_profile=context.safety_profile,
            role_targets=context.role_targets,
            session_volume_db=context.session_volume_db,
            declared_sensitivities=context.declared_sensitivities,
            on_playback_started=playback_started.fire,
        )
        conductor = CrossoverV2Conductor.hydrate(
            prior_snapshot,
            session_id=relay_session_id,
            source_preset=context.preset,
            roles_bands=context.roles_bands,
            fc_hz=context.fc_hz,
            driver_caps_dbfs=context.driver_caps_dbfs,
            session_volume_db=context.session_volume_db,
            seams=V2FlowSeams(
                play=play,
                analyze=bind_production_analyze(meta=refs),
                publish_check=publish_check,
                publish_candidate=publish_candidate,
                apply_complete=_applied_gate,
                apply_failed=_apply_failure_gate,
                retain_position=bind_position_retention(
                    evidence_store, relay_session_id, refs
                ),
                publish_cloud=bind_cloud_publisher(
                    evidence_store, relay_session_id, refs
                ),
                # #1866: banked only by the MEASURE candidate's own gate, so it
                # is wired on the measuring preparer alone. The stage-2 /
                # re-verify preparer below never builds a MEASURE candidate —
                # its index map is verify-only — so binding it there would ship
                # a seam nothing can reach.
                publish_findings=bind_findings_publisher(
                    evidence_store, relay_session_id, refs
                ),
                rollback=bind_delta_probe_rollback(run_async, camilla_factory),
                applied_offset_db=_applied_offset_gate,
                record_model_error=_record_live_model_error,
                publish_raw_anchor=bind_raw_anchor_publisher(
                    evidence_store,
                    relay_session_id,
                    refs,
                    session_graph=session_graph,
                    configured_fc_hz=context.fc_hz,
                    safety_profile=context.safety_profile,
                ),
            ),
            tier=plan_shape.tier,
            # The conductor's index→phase map is built from the SAME resolved
            # plan shape the emitted spec used — not merely the same function
            # at its own defaults — so the prompt an entry carries and the
            # phase the conductor runs for that index can never disagree.
            index_phase_map=build_v2_cloud_index_phase_map(plan_shape=plan_shape),
            # The boost-permission evidence gate (work order D2's consequence).
            # This session's own phases carry no VERIFY — the post-apply sweep
            # is stage 2 — so the measuring host declares from the SHAPE that
            # the journey does verify. Derived, not asserted: a tier declaring
            # no post-apply positions would drop boost with them.
            post_apply_verifies=plan_shape.verify_capture_target >= 1,
            driver_spacing_m=context.driver_spacing_m,
            driver_class_by_role=context.driver_class_by_role,
            tweeter_measurement_band_hz=context.tweeter_measurement_band_hz,
            session_graph=session_graph,
            attempt_floor=attempt_store.floor,
            speaker_id=context.topology.topology_id,
        )
        persist_conductor_state(conductor, failure_code=None, evidence=refs)
        holder["run"] = build_v2_run_and_consume(
            conductor,
            volume=_volume_hooks(camilla_factory, context),
            stop_event=stop_event,
            stop_lock=stop_lock,
            evidence_refs=refs,
            playback_started=playback_started,
        )
        return rc

    async def _run(client: Any, pi_session: Any) -> None:
        await holder["run"](client, pi_session)

    def _request_stop() -> None:
        with stop_lock:
            stop_event.set()

    return V2PreparedSession(
        label=V2_RELAY_KIND_SESSION,
        open=_open,
        run_and_consume=_run,
        request_stop=_request_stop,
    )


# The request field that selects which post-apply instrument
# ``prepare_v2_verify`` opens, and its one non-default value.
VERIFY_STAGE_KEY = "stage"
VERIFY_STAGE_POST_APPLY = "post_apply"
VERIFY_STAGE_RECOVERY = "recovery"


def _verify_plan_shape(
    raw: Mapping[str, Any] | None, state: Mapping[str, Any] | None,
) -> Any:
    """Resolve the post-apply plan shape, or ``None`` for the 1-entry recovery.

    Explicit rather than inferred. A shape could be guessed from the durable
    state (has VERIFY been walked? has it been accepted?), but every such
    inference has a case where it silently downgrades Full's six-position
    post-apply walk to one sweep — a household who opened stage 2 and let the
    link expire before the first capture would get the recovery instrument on
    their next tap and lose the spatial "after" evidence with no way to know.
    The caller says which instrument it wants; the tier still comes from the
    durable state, so the household's tier choice governs both stages.
    """
    from jasper.active_speaker.crossover_v2_flow import (
        CrossoverV2FlowError,
        resolve_plan_shape,
    )

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


def prepare_v2_verify(
    raw: Mapping[str, Any],
    *,
    status: Mapping[str, Any],
    run_async: Any,
    camilla_factory: Any,
) -> V2PreparedSession:
    """Prepare ``POST /crossover/v2/verify`` — the post-apply session.

    Requires a durable post-apply state (MEASURE accepted + applied). Opens a
    NEW relay session hosting a post-apply plan; the conductor is rebuilt in
    verify-only mode (CHECK/MEASURE marked accepted, applied, verify priors
    rehydrated from the durable state), with its index→phase map built by
    ``build_v2_verify_index_phase_map``.

    **ONE entry point, two shapes** (two-stage commission work order D2,
    owner-confirmed 2026-07-29) — generalized over the plan shape rather than
    forked into a second builder:

    * ``raw["stage"] == "post_apply"`` — **stage 2**, the tier's own shipped
      verify walk. The tier comes from the durable state the measuring session
      wrote, so the household's choice at the tier chooser governs both stages.
      Express is one position at the mark; Full is the six-position spatial
      walk whose combined curve the after-chart, the post-apply spec verdict,
      and the delta probe read. Running Full's stage 2 as a single position
      would leave its post-apply group with 0 curves and no combine at all.
    * anything else (the default, and every shipped caller) — the **§5.2
      recovery re-verify**: one entry at the mark, byte-identical to what this
      endpoint has always done, and what a FAILED stage 2 offers.

    An unrecognised ``stage`` is refused rather than silently measured as
    something else — the same strictness ``normalize_tier`` applies to a tier.
    """
    import numpy as np

    from jasper.active_speaker.crossover_v2_flow import (
        PHASE_CHECK,
        PHASE_MEASURE,
        CrossoverV2Conductor,
        V2FlowSeams,
        attempt_history_from_state,
        build_v2_verify_index_phase_map,
        build_v2_verify_session_spec,
        session_wall_clock_ceiling_s,
    )
    from jasper.capture_relay import correction_adapter

    if session_volume_plan().needs_recovery:
        raise CrossoverV2Refused(
            "the measurement volume needs recovery; recover it before verifying"
        )
    state = load_v2_state()
    if not state or not state.get("applied"):
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
    evidence_store, _bundle_id = open_v2_evidence_store(context.topology)
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
    # ``CrossoverV2Conductor.__init__`` so the "values plus a date, or
    # nothing" rule has one owner.
    pilot_transfer_prior = pilot_transfer_prior_from_state(state)
    acknowledgement_binding = secrets.token_urlsafe(24)
    stop_event = threading.Event()
    stop_lock = threading.Lock()
    holder: dict[str, Any] = {}

    def _open(client: Any, base: str, capture_origin: str, return_url: str) -> Any:
        spec = build_v2_verify_session_spec(
            context.fc_hz,
            acknowledgement_binding=acknowledgement_binding,
            plan_shape=plan_shape,
            default_setup_calibration=default_setup_calibration_for_v2(),
        ).with_return_url(return_url)
        rc = correction_adapter.open_capture(
            client,
            spec,
            relay_base=base,
            capture_origin=capture_origin,
            return_url=return_url,
        )
        relay_session_id = rc.pi_session.session_id
        # Re-arm the walked-away ceiling from THIS plan (1 entry ⇒ the plain
        # 1800 s baseline; Full's stage-2 walk scales it like any other plan).
        # The volume plan is process-global, so a preceding cloud session would
        # otherwise leave its own longer ceiling in force for a measurement
        # that cannot possibly need it.
        session_volume_plan().set_wall_clock_ceiling_s(
            session_wall_clock_ceiling_s(spec.capture_plan)
        )
        _publish_check, publish_candidate, refs = bind_evidence_publishers(
            evidence_store, relay_session_id
        )
        # One signal per session, shared by the play seam (which fires it) and
        # the runner (which installs the armed capture's phase ladder on it).
        playback_started = PlaybackStartSignal()
        play = bind_production_play(
            run_async=run_async,
            camilla_factory=camilla_factory,
            evidence_store=evidence_store,
            relay_session_id=relay_session_id,
            topology=context.topology,
            preset=context.preset,
            role_channels=context.role_channels,
            session_graph=None,
            playback_device=context.playback_device,
            safety_profile=context.safety_profile,
            role_targets=context.role_targets,
            session_volume_db=context.session_volume_db,
            declared_sensitivities=context.declared_sensitivities,
            on_playback_started=playback_started.fire,
        )
        conductor = CrossoverV2Conductor(
            session_id=relay_session_id,
            source_preset=context.preset,
            roles_bands=context.roles_bands,
            fc_hz=context.fc_hz,
            driver_caps_dbfs=context.driver_caps_dbfs,
            session_volume_db=context.session_volume_db,
            seams=V2FlowSeams(
                play=play,
                analyze=bind_production_analyze(meta=refs),
                publish_check=_publish_check,
                publish_candidate=publish_candidate,
                apply_complete=_applied_gate,
                # Never actually consulted: this conductor is constructed with
                # ``applied=True`` already, so authorize_begin's apply-observed
                # short-circuit never reaches the apply_failed check. Supplied
                # anyway — V2FlowSeams is a frozen dataclass with no defaults.
                apply_failed=_apply_failure_gate,
                # A verify-only re-arm measures the ALREADY-applied graph
                # against the original session's prediction, so it carries the
                # same apply-boundary offset and needs the same correction
                # (#1811). The value survives in durable state across the
                # re-arm, which is exactly what this seam reads.
                applied_offset_db=_applied_offset_gate,
                # The two cloud seams, re-threaded for stage 2 (work order D2).
                # They were deliberately absent while this preparer only ever
                # built ``{1: PHASE_VERIFY}`` — no cloud group of any kind, so
                # ``_close_cloud_group``/``_run_cloud_pipeline`` never ran.
                # Full's stage 2 IS a post-apply group, and without these its
                # positions would never be retained and its combined curve
                # never published: the after-chart, the post-apply spec
                # verdict, and the delta probe all read that publication. A
                # single-entry plan exercises neither, so the recovery
                # re-verify is unchanged by their presence.
                retain_position=bind_position_retention(
                    evidence_store, relay_session_id, refs
                ),
                publish_cloud=bind_cloud_publisher(
                    evidence_store, relay_session_id, refs
                ),
                record_model_error=_record_live_model_error,
            ),
            driver_spacing_m=context.driver_spacing_m,
            driver_class_by_role=context.driver_class_by_role,
            tweeter_measurement_band_hz=context.tweeter_measurement_band_hz,
            accepted_phases=(PHASE_CHECK, PHASE_MEASURE),
            applied=True,
            gain_plan_db=state.get("gain_plan_db"),
            index_phase_map=build_v2_verify_index_phase_map(plan_shape=plan_shape),
            measure_predicted_sum=predicted_sum,
            measure_predicted_spec_report=predicted_spec,
            measure_gate_window_ms=(
                float(gate_ms) if isinstance(gate_ms, (int, float)) else None
            ),
            verify_pilot_transfer_prior=pilot_transfer_prior,
            attempt_history=attempt_history_from_state(state),
            attempt_floor=attempt_store.floor,
            last_attempt_decision=(
                dict(prior_attempt_decision)
                if isinstance(prior_attempt_decision, Mapping) else None
            ),
            speaker_id=context.topology.topology_id,
            tuning_attempt_id=tuning_attempt_id,
        )
        # Keep the durable candidate/applied facts; rebind the session id.
        persist_conductor_state(conductor, failure_code=None, evidence=refs)
        holder["run"] = build_v2_run_and_consume(
            conductor,
            volume=_volume_hooks(camilla_factory, context),
            stop_event=stop_event,
            stop_lock=stop_lock,
            evidence_refs=refs,
            playback_started=playback_started,
        )
        return rc

    async def _run(client: Any, pi_session: Any) -> None:
        await holder["run"](client, pi_session)

    def _request_stop() -> None:
        with stop_lock:
            stop_event.set()

    return V2PreparedSession(
        label=V2_RELAY_KIND_VERIFY,
        open=_open,
        run_and_consume=_run,
        request_stop=_request_stop,
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

    It closes the hole work-order premise 5 was hiding: ``prepare_v2_verify``'s
    very next line is ``resolve_conductor_context(status)``, which is
    fail-closed and carries seven refusal sites of its own plus
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
    PR-T3 the relay runner also called it, automatically, off the pre-apply
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
    from jasper.active_speaker.crossover_preview import load_crossover_preview
    from jasper.active_speaker.design_draft import load_design_draft
    from jasper.active_speaker.measured_crossover_candidate import (
        MeasuredCrossoverCandidate,
        MeasuredCrossoverCandidateError,
    )
    from jasper.active_speaker.measurement import load_measurement_state
    from jasper.output_topology import load_output_topology

    expected = str(raw.get("expected_candidate_fingerprint") or "")
    if not expected:
        raise CrossoverV2Refused("expected_candidate_fingerprint is required")
    state = load_v2_state()
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
    draft = load_design_draft(topology=topology)
    preview = load_crossover_preview(current_design_draft=draft)
    measurements = load_measurement_state(topology)

    # Blocker M translation: compose read-only (the seam's own build_candidate
    # closure re-derives this identically under its writer lock, so this is a
    # deterministic recompose, not a second opinion) and confirm the
    # composition is still bound to the reviewed measured candidate before
    # asking the seam to apply anything.
    reviewed_baseline = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
        write=False,
        tuning_owner="automatic",
        measured_candidate=candidate,
    )
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
    # commits and pops that field off the new applied SSOT — this is the
    # ONLY place it survives to stash for the v2 Undo path
    # (``handle_v2_restore``).
    pre_apply_profile = reviewed_baseline.get("applied_recomposition_profile")
    if not isinstance(pre_apply_profile, Mapping):
        pre_apply_profile = None

    # LAST, immediately before the transaction commits (D3). After the
    # freshness gates, so a stale candidate still gets its own specific
    # refusal rather than this one.
    _assert_stage_2_can_open(status)

    cam = camilla_factory()
    payload = run_async(
        apply_baseline_profile(
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
        )
    )
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
        observe_apply_success(
            expected,
            pre_apply_profile=pre_apply_profile,
            expected_post_apply_offset_db=offset_db,
        )
    issue = None
    if payload.get("status") == "blocked":
        # Finding N: name the blocker compactly (not buried in the full
        # composed profile) and persist it so the failure screen can surface
        # it instead of the household seeing a generic message with no
        # specific cause. This endpoint is the flow's ONE apply path since
        # PR-T3 removed auto-apply, so it is also the only writer of this
        # blocking issue — the SSOT claim survives its original reason.
        issue = _blocking_apply_issue(payload)
        if issue is not None:
            payload["issue"] = issue
        _persist_apply_blocked(issue)
        log_event(
            logger,
            "correction.crossover_v2_apply_blocked",
            level=logging.WARNING,
            issue_id=(issue or {}).get("id", ""),
        )
    log_event(
        logger,
        "correction.crossover_v2_apply",
        status=payload.get("status"),
        candidate_fingerprint=expected,
    )
    return payload


def bind_delta_probe_rollback(run_async: Any, camilla_factory: Any) -> Any:
    """The conductor's ``rollback`` seam (linearization-integrity PR-L5).

    Runs the SAME restore the household's Undo button runs — one restore path,
    not a second one that could drift from it — and returns True when the
    previous profile is back on the speaker. The only difference is who
    pressed it: here the delta probe did, because it measured that the applied
    correction is not doing what its own filters commanded.

    Catches :class:`CrossoverV2Refused`, which is the ordinary outcome for an
    automatic caller — a first-ever apply has nothing stashed to go back to,
    and a changed output topology makes the stash unsafe to reload — and
    reports "not restored" so the conductor's refusal still reaches the
    household with the Undo button on it. A rollback that could not run must
    not swallow the verdict that asked for it.

    It does NOT claim to catch everything: an OSError from the CamillaDSP
    socket or a malformed stash still propagates, and the conductor's own
    ``_delta_probe_refusal`` catches that wider family on the other side of
    the seam (it has to — a conductor with a different binding gets the same
    protection). Two honest halves rather than one dishonest "never raises".
    """

    def _rollback(reason: str) -> bool:
        try:
            payload = handle_v2_restore(run_async, camilla_factory)
        except CrossoverV2Refused as exc:
            log_event(
                logger, "correction.crossover_v2_delta_probe_restore_refused",
                level=logging.WARNING, reason=reason, detail=str(exc),
            )
            return False
        restored = payload.get("status") == "restored"
        log_event(
            logger, "correction.crossover_v2_delta_probe_restore",
            level=logging.WARNING if not restored else logging.INFO,
            reason=reason, status=payload.get("status"),
        )
        return restored

    return _rollback


def handle_v2_restore(
    run_async: Any,
    camilla_factory: Any,
) -> dict[str, Any]:
    """POST /crossover/v2/restore — the v2-aware Undo (§5.2 verify_fail).

    The legacy ``/crossover/restore`` expects a PENDING candidate-apply
    transaction from the per-driver commissioning-run machinery
    (``commissioning_apply.restore_pending_candidate_apply``); a v2 apply
    never creates one — :func:`handle_v2_apply` commits straight through
    :func:`~jasper.active_speaker.baseline_profile.apply_baseline_profile`'s
    atomic transaction, so by the time a household reaches ``verify_fail``
    there is nothing "pending" left for the legacy path to find, and it
    500s (W6 run-8 Blocker Q: ``there is no pending candidate apply to
    restore``). This is the v2-aware replacement: reload the pre-candidate
    applied profile ``handle_v2_apply`` stashed (``pre_apply_profile`` in the
    durable v2 state) through the SAME apply transaction family
    (:func:`~jasper.active_speaker.baseline_profile.restore_applied_baseline_profile`),
    then clear the durable v2 applied/candidate/failure state so the
    envelope returns to a clean measure/review state.

    Never raises a bare exception for an ordinary refusal — every refusal is
    a :class:`CrossoverV2Refused` (maps to 400 at the dispatch ladder,
    exactly like every other v2 endpoint refusal) so a household stuck on a
    bad-sounding candidate always gets a named answer, never a 500.
    """
    from jasper.active_speaker.baseline_profile import (
        restore_applied_baseline_profile,
        topology_config_fingerprint,
    )
    from jasper.output_topology import load_output_topology

    state = load_v2_state()
    if not state or not state.get("applied"):
        raise CrossoverV2Refused(
            "nothing is applied to undo; measure and apply a crossover first"
        )
    pre_apply_profile = state.get("pre_apply_profile")
    if not isinstance(pre_apply_profile, Mapping):
        # Now that persist_conductor_state carries pre_apply_profile forward
        # across every conductor snapshot (W6.12 P0), this branch is reached
        # ONLY on a genuine first-ever apply — there was never an earlier
        # profile to stash. Say so plainly rather than reading like a bug
        # report or a generic "no undo available" error.
        raise CrossoverV2Refused(
            "this is the first measured crossover on this speaker — there's "
            "no earlier one to restore; use Speaker setup to remove it instead"
        )
    # Item 2 (#1605): the stashed Undo target was composed for the output
    # topology live at apply time. If the topology's config-determining
    # fingerprint changed since (a driver swap, a different output device or
    # channel map), reloading that config would realize the WRONG graph —
    # refuse with a clear "re-measure" nudge rather than silently restoring a
    # stale config. A stash predating the fingerprint (no topology_fingerprint
    # in its ``source``) can't be compared, so it falls through to the existing
    # restore path, which still validates the config bytes itself.
    stashed_source = pre_apply_profile.get("source")
    stashed_topology_fp = (
        str(stashed_source.get("topology_fingerprint") or "")
        if isinstance(stashed_source, Mapping)
        else ""
    )
    if stashed_topology_fp:
        current_topology_fp = topology_config_fingerprint(load_output_topology())
        if current_topology_fp != stashed_topology_fp:
            log_event(
                logger,
                "correction.crossover_v2_restore_topology_mismatch",
                level=logging.WARNING,
            )
            raise CrossoverV2Refused(
                "the speaker's output configuration changed since this "
                "crossover was applied, so the previous sound can't be safely "
                "restored — re-measure the crossover instead"
            )
    cam = camilla_factory()
    payload = run_async(
        restore_applied_baseline_profile(
            pre_apply_profile,
            load_config=lambda path: cam.set_config_file_path(
                path, best_effort=False
            ),
            get_current_config_path=lambda: cam.get_config_file_path(
                best_effort=False
            ),
        )
    )
    if payload.get("status") == "restored":
        observe_restore()
    log_event(
        logger,
        "correction.crossover_v2_restored",
        status=payload.get("status"),
    )
    return payload


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


def _persist_apply_blocked(issue: Mapping[str, str] | None) -> None:
    """Record (or clear) the last blocked-apply issue for the fix_and_retry
    screen's nudge (layered onto REASON_APPLY_FAILED at the "applying"
    phase — owner ruling, 2026-07-20)."""
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
