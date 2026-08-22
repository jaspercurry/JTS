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
* the **session assembly for the capture provider** (#2662): the preparers
  below gate, build the conductor, and hand the walk to the capture source —
  today the relay provider, :mod:`jasper.web.correction_crossover_v2_relay`,
  which owns the plan-walk hosting (``build_v2_run_and_consume``), the phone
  progress choreography, and the translation of relay-internal deaths into
  the flow's reason vocabulary. This host stays the single writer of the
  persisted failure state those reasons land in
  (``status["crossover_v2"]["failure"]`` — ``relay_timeout``,
  ``user_stopped``, …), and of the walked-away volume guarantee the provider
  drives through ``V2VolumeHooks``.

Session binding (§5.6): the durable state is keyed to the relay session id. A
new ``/v2/session`` POST hydrates through
:meth:`CrossoverV2Session.hydrate`, which invalidates CHECK/MEASURE evidence
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
from typing import (
    TYPE_CHECKING, Any, Callable, Mapping, MutableMapping, Sequence, cast,
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
    STAGE_MEASURE_CAPABILITIES,
    STAGE_VERIFY_CAPABILITIES,
    StageOpening,
    available_stage_priors,
    open_stage,
)
# The round's own rollback-anchor provenance (#2537) — what an apply displaced,
# what it put live, and whether the running graph is still that. A pure leaf
# like ``journey`` above: it owns the record's shape and the divergence rule,
# and this module keeps only the wiring (write it on apply, read it on restore).
from jasper.active_speaker.crossover_v2.round_anchor import (
    ROUND_ANCHOR_STATE_KEY,
    restore_target_diverged,
    round_anchor_record,
    running_config_diverged,
)
# The position gate's two TERMINAL codes, at module level because the constants
# they name are module level. A pure-organ leaf like ``journey`` above, so this
# adds no cycle and no import cost worth deferring — every other flow symbol in
# this module stays lazily imported inside its own function, as before.
from jasper.active_speaker.capture_provenance import (
    CaptureProvenance,
    CaptureProvenanceRecorder,
    record_capture_provenance,
)
from jasper.active_speaker.crossover_v2.capture_source import (
    SOURCE_RELAY,
    SOURCE_WIRED,
)
from jasper.active_speaker.crossover_v2.topology_prescription import (
    candidate_topology,
)
from jasper.active_speaker.crossover_v2.vocabulary import (
    REASON_POSITION_HOLD_EXPIRED,
    REASON_POSITION_TARGET_MISSING,
    REASON_SESSION_CEILING_EXPIRED,
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
# The RELAY capture provider (#2662 strangler slice 1). The plan-walk hosting,
# the phone progress ladder, the purge grace, and the link-TTL policy are the
# relay source's private choreography and moved behind the capture-source seam
# (jasper.active_speaker.crossover_v2.capture_source), into
# jasper.web.correction_crossover_v2_relay. The names stay published HERE —
# same PEP 484 redundant-alias form as the journey block above — because they
# are this host's existing surface: the preparers below call them, and
# correction_setup and the test suite address them at this module. EAGER, and
# safely so: the provider module never imports this one at module scope (its
# host reach-backs are call-time), so the pair cannot deadlock an import.
#
# Patch contract for test doubles: this HOST calls build_v2_run_and_consume,
# relay_link_ttl_s, and PlaybackStartSignal through these bindings, so
# patching them on this module reaches the preparers.
#
# Three further names (program_phase_schedule, start_program_phase_ladder,
# TERMINAL_FAILURE_PURGE_GRACE_S) were re-published here too, for external
# callers the host never invoked. No external caller ever arrived — only tests
# addressed them as ``v2host.<name>`` — so the re-exports are gone and the
# provider module owns them outright. Reach them at
# ``jasper.web.correction_crossover_v2_relay``.
from jasper.web.correction_crossover_v2_relay import (
    PlaybackStartSignal as PlaybackStartSignal,
    build_v2_run_and_consume as build_v2_run_and_consume,
    relay_link_ttl_s as relay_link_ttl_s,
)

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


def capture_dump_enabled() -> bool:
    """Is operator capture retention switched on right now?

    ONE reader of the marker, because two moments of a capture now depend on
    the answer: the play seam decides whether to observe the graph, and
    ``_maybe_retain_capture`` decides whether to write the clip. Read fresh
    every call — the operator creates and deletes the marker on a live
    speaker, with no restart.
    """
    return (XOVER_CAPTURE_DUMP_DIR / XOVER_CAPTURE_DUMP_ENABLED_MARKER).exists()

_state_lock = threading.RLock()
_state_path_override: Path | None = None

_volume_plan_lock = threading.Lock()
_volume_plan: Any = None


class CrossoverV2Refused(ValueError):
    """A v2 endpoint refusal (maps to HTTP 400 in the dispatch ladder).

    ``code`` is an optional
    :data:`~jasper.active_speaker.crossover_v2.vocabulary.REASON_REGISTRY`
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
    :data:`~jasper.active_speaker.crossover_v2.vocabulary.REASON_PROGRAM_UNPLAYABLE`,
    so a deterministic "JTS cannot use the saved safety limits" and
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
        REASON_PROTECTION_NOT_SEPARABLE,
        REASON_PROTECTION_SWEEP_TOO_LOW,
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
    from jasper.audio_measurement.program_analysis import (
        ConfiguredPathConditioningError,
    )

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
    ``correction_setup._relay_failure_message`` never sees it either.

    So classify FIRST and carry the code out. The message comes from the same
    :data:`~jasper.active_speaker.crossover_v2.vocabulary.REASON_REGISTRY` entry the
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

    * ``missing``    → finish the driver details. ``/sound/`` renders no
      callout at all in this state, so "review the safety limits" would name a
      panel that is not on the page.
    * ``incomplete`` → add the missing values first. Saving with values missing
      just rebuilds the same ``incomplete`` profile, so "save again" would send
      the household in a circle.
    * everything else (``stale``, ``malformed``) → review and save. Both are
      cleared by one ordinary save: it rebuilds the profile from the visible
      values, so an output change and an unreadable artifact end the same way.
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


def save_v2_state(state: Mapping[str, Any], *, durable: bool = False) -> None:
    """Write the durable v2 state. ``durable`` decides whether it is fsync'd.

    Atomic is not durable. :func:`~jasper.atomic_io.atomic_write_text` writes a
    tempfile and renames, so a concurrent reader never sees a partial file —
    but without ``durable=True`` nothing has told the kernel to put those bytes
    on the platter, and a power cut can lose the whole write while leaving the
    speaker's DSP graph changed.

    **The rule for choosing (#2291): durable where power loss would lose the
    rollback anchor or falsify a receipt; cheap everywhere else.** Three writes
    qualify — one per half of that rule:

    * the ANCHOR pair, which own ``pre_apply_profile``, the only pointer Undo
      restores from. :func:`observe_apply_success` CREATES it in the same
      moment the new graph goes live, so a lost write leaves a corrected
      speaker with no way back; :func:`observe_restore` clears it and flips
      ``applied``, so a lost write leaves the state claiming a correction that
      is no longer on the speaker.
    * the RECEIPT identity, written by :func:`persist_conductor_state` — but
      **only on a persist that carries a new one**. A receipt lives in the
      write-once evidence bundle, so losing the pointer to it leaves an
      immutable record nothing can find: the "falsifies a receipt" half.

    Everything else stays cheap on purpose, including
    :func:`persist_conductor_state`'s ordinary path — it runs after every
    consumed capture, and an fsync per capture buys nothing that the next
    capture's write does not already redo. :func:`reset_v2_journey_state`
    PRESERVES the anchor, so losing its write leaves the richer previous
    state — the anchor survives either way, which is why it is not on the
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
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            mode=0o640,
            group_from_parent=True,
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
            or "measure" not in (state.get("accepted_phases") or ())
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
    sound_declaration_undo = state.get("sound_declaration_undo")
    round_anchor = state.get(ROUND_ANCHOR_STATE_KEY)
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
        # Preserved on exactly ``pre_apply_profile``'s terms (#2292): the two
        # are the graph half and the declaration half of ONE Undo. Start over
        # keeps the applied graph playing and keeps Undo reachable, so dropping
        # this here would leave that Undo able to restore the sound but not the
        # crossover ``/sound`` declares for it.
        "sound_declaration_undo": (
            dict(sound_declaration_undo)
            if isinstance(sound_declaration_undo, Mapping)
            else None
        ),
        # Preserved on the same terms (#2537), and load-bearing for the same
        # reason: Start over keeps the applied graph playing and keeps Undo
        # reachable, so dropping the round's own record of what that apply
        # displaced would leave the restore unable to check whether the graph
        # it is about to replace is still the one it applied.
        ROUND_ANCHOR_STATE_KEY: (
            dict(round_anchor) if isinstance(round_anchor, Mapping) else None
        ),
    })
    log_event(logger, "correction.crossover_v2_journey_reset_kept_applied")


def observe_apply_success(
    candidate_fingerprint: str,
    *,
    pre_apply_profile: Mapping[str, Any] | None = None,
    sound_declaration_undo: Mapping[str, Any] | None = None,
    expected_post_apply_offset_db: float = 0.0,
    round_anchor: Mapping[str, Any] | None = None,
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

    ``sound_declaration_undo`` (#2292) is the OTHER half of that same Undo: how
    to put ``/sound``'s declared crossover back to what it said before this
    apply's accept wrote it, or ``None`` when this apply did not write Sound
    (every configured-Fc winner). It is a parameter of THIS call, beside
    ``pre_apply_profile`` and written in the SAME state write, because the two
    describe one reversible event and a household undoes both or neither.

    That co-location is the fix for the shape a gate caught in review: while
    this key was written on the alternative-Fc accept instead, an alternative
    apply followed by Start over and an ORDINARY apply left the graph half
    re-stamped here and the declaration half describing the older apply — so
    Undo restored the recent graph and wrote a two-applies-old frequency into
    ``/sound``, reporting success. Passing ``None`` is therefore not a default
    to skip: it is an ordinary apply CLEARING a record that no longer inverts
    anything.

    ``round_anchor`` (#2537) is
    :func:`~jasper.active_speaker.crossover_v2.round_anchor.round_anchor_record`'s
    snapshot of what this apply displaced and what it put live, written in the
    SAME state write and for exactly ``pre_apply_profile``'s reason: it
    describes this one reversible event, so it must be re-stamped by every apply
    and can never outlive the graph its sibling describes. ``None`` clears it,
    which is what an apply that could not name either path should leave behind —
    an absent anchor makes the restore's divergence check say "cannot compare",
    never "matches".

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
    # UNCONDITIONAL, exactly like the line above it — that is the whole
    # invariant. Both halves of the Undo are re-stamped by every successful
    # apply, so neither can outlive the graph the other describes.
    state["sound_declaration_undo"] = (
        dict(sound_declaration_undo)
        if isinstance(sound_declaration_undo, Mapping)
        else None
    )
    # UNCONDITIONAL for the same reason as the two above (#2537): three halves
    # of one reversible event, re-stamped together or the round's own record of
    # what it displaced starts describing a different apply.
    state[ROUND_ANCHOR_STATE_KEY] = (
        dict(round_anchor) if isinstance(round_anchor, Mapping) else None
    )
    offset_db = float(expected_post_apply_offset_db)
    state["expected_post_apply_offset_db"] = (
        round(offset_db, 3) if math.isfinite(offset_db) else 0.0
    )
    # fsync'd (#2291). This write CREATES the rollback anchor, and it happens
    # after the new graph is already live on the speaker: a power cut that
    # loses it leaves a corrected speaker with nothing to restore to.
    save_v2_state(state, durable=True)
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
    stale caveat on a live session.

    ``round_receipt`` is the fifth (#2698), and the one field here cleared IN
    PART rather than wholesale — it is the SERIES' memory, not the session's,
    so the blend instruction is withdrawn while the ordinal that bounds the
    series survives. The argument for that split is at the line itself. A9's
    staged prescription is withdrawn on the same rule, and is the one thing this
    function clears that does not live in ``state`` at all — see its call."""
    from jasper.active_speaker.crossover_v2.prescription_spool import (
        withdraw_staged_prescription,
    )

    # A9's half of the ``round_receipt`` withdrawal below, and it runs FIRST —
    # ahead of the no-state early return — because it is the one thing this
    # function clears that does not live in ``state``. An operator staged that
    # document by reading the evidence of the round this Undo has just reversed,
    # so it prescribes a correction for a graph the household no longer has,
    # exactly as the banked instruction does. Whether durable state survived is
    # a fact about the journey record, not about that document: a lost state
    # file resolves the next round's ordinal back to 1, which is an ordinal a
    # stale document could legitimately match. Withdrawn rather than consumed —
    # nothing ran it — and best-effort in the owner, so a slot that cannot be
    # cleared never costs the household its Undo.
    withdraw_staged_prescription()
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
    state["sound_design_revision"] = None
    state["accepted_sound_revision"] = None
    # Written in the same state write as the revision above and scoped to the
    # same review, so it is cleared with it. Nothing could read a survivor —
    # every reader gates on that revision first — but a record of what an
    # already-reversed accept displaced is exactly the stale-frequency hazard
    # the next comment block was written about.
    state["accepted_sound_declaration_change"] = None
    # The declaration half of the Undo (#2292), cleared for the same reason
    # ``pre_apply_profile`` above is: both describe how to reverse an apply
    # that has just been reversed. :func:`handle_v2_restore` reads the record
    # BEFORE calling this, so the declaration leg has already run (or reported
    # why it did not) by the time the record goes away.
    #
    # Cleared even when that leg reported ``declaration_restore_failed``, which
    # does cost the household an automatic second chance — deliberately, on two
    # grounds. Nothing could consume a surviving record: this call sets
    # ``applied`` False, and ``handle_v2_restore`` refuses outright without it,
    # so the Undo button is gone and no other caller reads the key. And a
    # record that outlives its ``pre_apply_profile`` twin is precisely the
    # asymmetry a gate caught in review — it is what let a stale frequency
    # reach ``/sound``. The recovery is the household sentence, which names the
    # exact frequency to set, plus the ERROR journal line.
    state["sound_declaration_undo"] = None
    # The third half of the same Undo (#2537): what this apply displaced and
    # what it put live. Cleared here for exactly ``pre_apply_profile``'s
    # reason — the apply it describes has just been reversed, so a surviving
    # record would describe a graph that is no longer live and a displacement
    # that has already been undone.
    state[ROUND_ANCHOR_STATE_KEY] = None
    # ``round_receipt`` is the fifth field to meet the trap above (#2698), and
    # the only one cleared IN PART. It is the SERIES' memory rather than the
    # session's — nulling it would send the series back to round 1 and let an
    # Undo→measure cycle repeat past the round cap forever, so the ordinal, the
    # objectives that frame it, and the trusted floor all survive an Undo by
    # design. What cannot is the round's blend INSTRUCTION: since #2698 the
    # measuring stage builds its candidate from it
    # (``CrossoverV2Session._blend_prescription``), so a surviving one would
    # compose a correction onto the graph the household just removed. That is
    # the same ruling ``coordinator._write_round_receipt`` already applies to a
    # round that restores its OWN graph — a prescription describes a speaker
    # measured through a specific incumbent, and this Undo is the other door to
    # the same state. ``blend`` holds the filters and the residual they were
    # decided against as one fact, so one clear withdraws both, and ``None`` is
    # the shape that writer already uses for "this round issues no
    # instruction".
    receipt = state.get("round_receipt")
    if isinstance(receipt, Mapping):
        state["round_receipt"] = {**receipt, "blend": None}
    # fsync'd (#2291), the mirror of ``observe_apply_success``: this write
    # CLEARS the rollback anchor and flips ``applied`` after the previous graph
    # is already back on the speaker, so a power cut that loses it leaves the
    # state claiming a correction the speaker is no longer playing.
    save_v2_state(state, durable=True)


#: The one shape a recorded review decision takes, and its only value.
#:
#: ``decision`` is a word rather than a bool because the fact being recorded is
#: WHICH answer the household gave, and a bool names only one of them.
REVIEW_DECISION_DECLINED = "declined"


def observe_review_decline(candidate_fingerprint: str) -> None:
    """Record that the household chose to keep the current sound (#2641).

    The write half of the review screen's decline, beside its
    :func:`observe_restore` sibling and under the same lock, because that is
    where every durable v2 write lives: a read-modify-write from the HTTP layer
    would be a second writer racing this one on the state file.

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
        PHASE_CHECK,
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
        # …and a household who has ALREADY answered this screen does not get
        # it again (#2641). The decline changed nothing on the speaker and did
        # not delete the candidate, so the honest destination is the journey's
        # own resting screen — which is what ``PHASE_CHECK`` renders. Bound to
        # the candidate the decline answered, so a newer measurement brings
        # the review back rather than inheriting a stale "no".
        if review_declined(state):
            return PHASE_CHECK
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
    4. *Report, no curve* — **the least obvious of the four.**
       ``_assert_accountable`` stashes the verdict BEFORE the improvement gate
       runs and ``_measure_predicted_sum`` only after it returns, so a refusal
       between the two persists a report with ``predicted_sum`` still ``None``
       — honest, not a leak: the spec verdict did evaluate that prediction. The
       refusal that produced this shape is retired (``accountability``'s item
       2); a pre-retirement state still carries it, and a consumer shows the
       verdict with no curve to draw.

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
        "comparison": (
            dict(spec["comparison"])
            if isinstance(spec.get("comparison"), Mapping) else None
        ),
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
        "fc_selection": (state or {}).get("fc_selection"),
        "accepted_sound_revision": (state or {}).get("accepted_sound_revision"),
        # MEASURE's own verdict-time disclosures — today just G1's ripple
        # reservation (#2087). Copied through unvalidated, exactly like
        # ``candidate`` and ``verify`` beside it: the envelope's own accessor
        # is the validating reader, so a state file written by another build
        # cannot 500 this poll path.
        "measure": (state or {}).get("measure"),
        # The last graded round's adoption receipt — what it decided, which row
        # decided it, and where it sat in the series (#2537, #2602).
        #
        # **This projection was missing**, and the envelope has read ``None``
        # here on every real box since #2537: ``persist_conductor_state`` wrote
        # ``state["round_receipt"]`` and this block never forwarded it, so the
        # done screen's round key and its keep-for-iteration caveat existed only
        # in unit tests that hand-built the status dict. #2602 makes that gap
        # load-bearing rather than merely wasteful — a series that cannot tell a
        # household another round is coming has not delivered the ruling — so it
        # is fixed here rather than filed. Copied through unvalidated, exactly
        # like ``candidate`` and ``verify`` beside it: the envelope's own
        # accessor is the validating reader.
        "round_receipt": (state or {}).get("round_receipt"),
        "verify": (state or {}).get("verify"),
        "failure": (state or {}).get("failure"),
        "apply_blocked": (state or {}).get("apply_blocked"),
        "needs_recovery": needs_recovery,
        "applied": bool(state and state.get("applied")),
        # Issue #1863: whether the v2-aware Undo (handle_v2_restore) actually
        # has something to restore, so the envelope layer can stop offering a
        # button the endpoint is guaranteed to refuse. ASKED of the rule's
        # owner rather than transcribed here — see
        # :func:`restore_anchor_static_prefix_refusal` for why this reader
        # takes the static prefix and not the full five-gate resolver.
        "can_undo": restore_anchor_static_prefix_refusal(state) is None,
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

    ``overall_passed`` — projected by :func:`_compact_cloud_status` from the
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
    express session ever run. Undo already exists, is the PRIMARY button on the
    done screen, and is the household's call. What was missing is being told.

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
    from jasper.active_speaker.crossover_v2_flow import (
        CLAIM_FAIL,
        CLAIM_PASS,
        PHASE_CLOUD_VERIFY,
        PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB,
        REASON_VERIFY_CROSSOVER_REGION,
        TIER_EXPRESS,
        TIER_FULL,
    )
    from jasper.active_speaker.crossover_v2.accountability import LEDGER_NOT_AN_IMPROVEMENT
    from jasper.active_speaker.fc_selector import SELECTION_KEEP_CONFIGURED, SELECTION_RECOMMEND

    fc = block.get("fc_selection")
    fc = fc if isinstance(fc, Mapping) else {}
    fc_verdict = str(fc.get("verdict") or "")
    comparison_complete = fc.get("comparison_complete") is True
    # **A comparison that never ran is ABSENT, not incomplete.** Two gates
    # below — ``comparison_complete`` and ``authorized_winner`` — encode "the Fc
    # comparison finished, and the corner on the speaker is the one it
    # authorized". Neither question EXISTS on a round that executes its declared
    # corner rather than hunting one, so ``fc_selection`` is absent on every
    # session; only rounds banked while a corner selector existed carry one.
    # Reading that absence as an unfinished comparison made
    # ``RESULT_VERIFIED_TARGET``/``_BEST_EVALUATED`` structurally unreachable —
    # a successful commission told the household "not enough complete evidence
    # to grade… this report changed nothing automatically" over a tune that IS
    # applied, and dropped the Undo pointer exactly where a dissatisfied
    # household reaches for it.
    #
    # This function's own question is the one in its title: was the applied
    # correction checked AFTERWARDS. VERIFY and the post-apply group answer
    # that by themselves and neither consults the selector, which is why the
    # exemption is sound rather than merely convenient.
    #
    # **Scoped to absence only.** A sweep that RAN and did not finish still
    # fails both gates — that is a real incomplete comparison about a session
    # that really did evaluate alternatives, and this exemption must never
    # reach it. Both directions are pinned in
    # ``tests/test_correction_crossover_v2_endpoints.py``.
    sweep_ran = bool(fc)

    if not block.get("applied"):
        # One cause, since ``accountability``'s item 2 stopped refusing: a
        # forecast can no longer be why a round did not apply. A state carrying
        # the retired failure code reads ``inconclusive`` now, not
        # ``keep_previous`` — pinned in the endpoints suite.
        keep_previous = comparison_complete and fc_verdict == SELECTION_RECOMMEND
        return {
            **({"outcome": RESULT_KEEP_PREVIOUS if keep_previous else RESULT_INCONCLUSIVE} if keep_previous or fc else {}),
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
    configured_hz = _finite(fc.get("configured_hz"))
    scores = fc.get("scores")
    baseline_score = None
    if configured_hz is not None and isinstance(scores, list):
        for row in scores:
            if not isinstance(row, Mapping):
                continue
            hz = _finite(row.get("fc_hz"))
            if hz is not None and math.isclose(hz, configured_hz, abs_tol=0.05):
                baseline_score = _finite(row.get("score"))
                break
    prediction = block.get("prediction")
    prediction = prediction if isinstance(prediction, Mapping) else {}
    comparison = prediction.get("comparison")
    comparison = comparison if isinstance(comparison, Mapping) else {}
    candidate = block.get("candidate")
    candidate = candidate if isinstance(candidate, Mapping) else {}
    improvement_db = _finite(comparison.get("improvement_db"))
    required_db = _finite(comparison.get("required_db"))
    absolute_miss_db, absolute_worst_hz = _finite(absolute.get("max_db")), _finite(absolute.get("worst_hz"))
    result_evidence = bool(fc or comparison or integration or absolute)
    authorized_winner = bool(str(candidate.get("fingerprint") or "")) and (
        # No sweep ran, so there was no alternative for a winner to beat: the
        # published candidate IS the configured corner, which is precisely what
        # a ``keep_configured`` verdict authorizes. The fingerprint stays
        # required — it is the evidence that a candidate was published at all.
        not sweep_ran
        or (baseline_score is not None and fc_verdict == SELECTION_KEEP_CONFIGURED)
    )
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
        or (sweep_ran and not comparison_complete)
    ):
        result_outcome = RESULT_INCONCLUSIVE
    elif fc_verdict == SELECTION_RECOMMEND:
        result_outcome = RESULT_KEEP_PREVIOUS
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
    # rides a clean ``pass`` — and the other way, an absent or non-numeric
    # tracking max is an ``outcome`` fail whose integration claim reads
    # ``not_evaluated``. It reads ``integration`` and ``absolute`` because a
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
    if verify_failed:
        state = GRADE_FAILED
    elif outcome == "inconclusive":
        state = GRADE_INCONCLUSIVE
    elif isinstance(cloud_verdict, bool):
        # A walked post-apply position group — the widest claim available, and
        # on a clean pass it is the wider claim, so it still wins the word.
        state = GRADE_GRADED
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
        "comparison_complete": comparison_complete,
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


def _decimate_delta(commanded_delta: Any) -> dict[str, Any] | None:
    """Persist-time reduction of the COMMANDED delta (#2291 Phase 3).

    The delta probe's commanded axis —
    :func:`~jasper.active_speaker.crossover_v2_flow._commanded_delta`'s
    ``(freqs_hz, delta_db)``, the CHANGE the applied graph asks the speaker for
    relative to the graph it replaces — filters, role gains, polarity and delay
    (#2611). Bounded at the same :data:`MAX_PERSISTED_SUM_POINTS` ceiling,
    over the same fixed-width blocks, so it lands on the SAME grid
    :func:`_decimate_sum` produces for the same input frequencies — the two
    curves cross the stage bridge together and a reader comparing them should
    not have to ask whether their frequencies line up. That agreement is pinned
    (``test_the_commanded_delta_persists_on_the_same_grid_as_the_predicted_sum``)
    rather than asserted, because the block rule lives in
    ``_decimate_to_analysis_grid`` and is restated here.

    **Averaged in dB, not in linear power** — the one place this deliberately
    parts company with :func:`_decimate_sum`.

    The REASON first: over a block, the arithmetic mean is the unbiased
    estimator of a DIFFERENCE of dB curves, where the power mean is biased
    upward by Jensen's inequality. That bias grows with the WITHIN-BLOCK spread
    of the values and vanishes across a block the curve is flat over — so it is
    largest exactly where the commanded delta is steepest, and zero where the
    two estimators would have agreed anyway. ``_decimate_sum``'s power mean is
    the right estimator for its own input, a MAGNITUDE curve; it is the wrong
    one here.

    The measured bound second. Re-derived through the production owner
    (:func:`~jasper.active_speaker.linearization_fit.complex_correction_response`)
    over 200 cascades of realistic shape — a highshelf plus 4–11 peaking
    biquads at Q 0.7–6 plus a trim, on 1,024–8,192-bin grids: worst single
    block disagreement **1.60 dB**, and **5 of 100,762** persisted bins change
    side of the 0.5 dB
    :data:`~jasper.active_speaker.delta_probe.DELTA_PROBE_MIN_COMMANDED_DB`
    floor. So the domain choice does reach band membership and not merely a
    third decimal — rarely, and the bias is largest where the commanded value
    is largest, which is furthest from the floor.

    (The 0.27 dB this docstring used to quote was not reproducible against the
    production response owner and has been replaced by the figures above.)

    What it does NOT move is the graded error. The conductor reconstructs
    ``realized = (measured - predicted) + commanded``, so ``realized -
    commanded`` cancels this curve exactly; the decimation reaches band
    membership near the commanded floor and the reported ``gain_factor``.
    """
    if commanded_delta is None:
        return None
    import numpy as np

    freqs, delta = commanded_delta
    grid = np.asarray(freqs, dtype=float)
    values = np.asarray(delta, dtype=float)
    n = int(grid.size)
    # A length disagreement is not reachable from ``_commanded_delta`` (it
    # builds both arrays on one grid), but this function persists whatever a
    # conductor holds and a raise here would lose the WHOLE snapshot, not just
    # this key. Absent means "nothing commanded", which is already the honest
    # reading downstream.
    if n == 0 or int(values.size) != n:
        return None
    if n > MAX_PERSISTED_SUM_POINTS:
        block = -(-n // MAX_PERSISTED_SUM_POINTS)  # ceil division
        blocks = n // block
        kept = blocks * block
        grid = grid[:kept].reshape(blocks, block).mean(axis=1)
        values = values[:kept].reshape(blocks, block).mean(axis=1)
    return {
        "freqs_hz": [float(f) for f in grid],
        "delta_db": [float(d) for d in values],
    }


def _decimate_verify_measured(tracking_curve: Any) -> dict[str, Any] | None:
    """Persist-time reduction of the VERIFY capture's graded curve pair (#2522).

    ``crossover_v2_flow.CrossoverV2Session.verify_tracking_curve`` —
    ``(freqs_hz, measured_db, predicted_db)``, the very pair the delta probe
    graded. Bounded at the same :data:`MAX_PERSISTED_SUM_POINTS` ceiling over
    the same fixed-width blocks as :func:`_decimate_delta`, so a reader
    comparing the two records is comparing comparably-spaced grids. This curve
    is on the VERIFY capture's grid rather than the MEASURE prediction's, so
    the two are not expected to land on the SAME frequencies; a re-grade
    interpolates the commanded axis onto this one, exactly as the live probe
    does.

    **Averaged in dB, and here that is not merely the unbiased choice — it is
    what makes the record re-gradable.** Block-averaging in dB is linear, so the
    difference of the two decimated curves is exactly the decimated difference,
    and ``measured − predicted`` is precisely the quantity
    :func:`~jasper.active_speaker.delta_probe.classify_delta_probe` grades
    (``realized − commanded`` cancels the commanded curve). A power mean, right
    for :func:`_decimate_sum`'s magnitude curve, would bias each side
    differently by Jensen's inequality and leave a residual that belongs to
    neither the speaker nor the model.

    Why persist at all: the commanded and predicted priors were durable and the
    MEASURED curve was not, so the 2026-08-14 remote ``model_error`` verdict
    could not be re-graded against a corrected instrument without another full
    hardware run (#2522). Same store, same lifecycle, no retention knob of its
    own — this dict is rebuilt on every persist exactly like its neighbours.

    ``None`` for an absent curve, a curve that is not a triple, an empty grid,
    or arrays whose lengths disagree. Absent means "not re-gradable offline",
    which is the honest reading and is what every state file written before this
    key shipped already says.
    """
    if tracking_curve is None:
        return None
    import numpy as np

    try:
        freqs, measured, predicted = tracking_curve
    except (TypeError, ValueError):
        return None
    grid = np.asarray(freqs, dtype=float)
    measured_db = np.asarray(measured, dtype=float)
    predicted_db = np.asarray(predicted, dtype=float)
    n = int(grid.size)
    if n == 0 or int(measured_db.size) != n or int(predicted_db.size) != n:
        return None
    if n > MAX_PERSISTED_SUM_POINTS:
        block = -(-n // MAX_PERSISTED_SUM_POINTS)  # ceil division
        blocks = n // block
        kept = blocks * block

        def _blocks(values):
            return values[:kept].reshape(blocks, block).mean(axis=1)

        grid, measured_db, predicted_db = (
            _blocks(grid), _blocks(measured_db), _blocks(predicted_db)
        )
    return {
        "freqs_hz": [float(f) for f in grid],
        "measured_db": [float(v) for v in measured_db],
        "predicted_db": [float(v) for v in predicted_db],
    }


def verify_measured_curve_from_state(
    state: Mapping[str, Any] | None,
) -> tuple[Any, Any, Any] | None:
    """The persisted VERIFY curve pair, ready to re-grade (#2522).

    The read side of :func:`persist_conductor_state`'s
    ``verify_priors.verify_measured``, and the mirror of
    :func:`commanded_delta_prior_from_state`: durable state in, the
    ``(freqs_hz, measured_db, predicted_db)`` triple
    :meth:`~jasper.active_speaker.crossover_v2_flow.CrossoverV2Session.
    _run_delta_probe` consumes out. With the persisted ``commanded_delta`` and
    the ``delta_probe`` record's own ``requested_band_hz`` /
    ``expected_offset_db``, that is everything a laptop-side re-grade needs.

    ``None`` covers the honest cases that mean one thing to a re-grade — there
    is no measured curve to grade: a state file written before this key shipped,
    a session that never reached VERIFY, and a record that is not the triple
    this build writes. A length disagreement is one of those and is checked
    here, for :func:`commanded_delta_prior_from_state`'s reason: three arrays
    that are not one curve would otherwise reach the classifier as a grid
    mismatch instead of an absence.
    """
    import numpy as np

    priors = (state or {}).get("verify_priors")
    record = priors.get("verify_measured") if isinstance(priors, Mapping) else None
    if not isinstance(record, Mapping):
        return None
    freqs = record.get("freqs_hz")
    measured = record.get("measured_db")
    predicted = record.get("predicted_db")
    if not freqs or not measured or not predicted:
        return None
    if not (len(freqs) == len(measured) == len(predicted)):
        log_event(
            logger, "correction.crossover_v2_verify_measured_malformed",
            level=logging.WARNING,
            n_freqs=len(freqs), n_measured=len(measured), n_predicted=len(predicted),
        )
        return None
    return (
        np.asarray(freqs, dtype=float),
        np.asarray(measured, dtype=float),
        np.asarray(predicted, dtype=float),
    )


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


def _entry_baseline_prior(conductor: Any) -> dict[str, Any] | None:
    """The conductor's entry baseline as durable state carries it (#2291).

    ``getattr`` plus a duck-typed ``to_dict`` for :func:`_predicted_spec_prior`'s
    reason: this persistence helper is called with conductor doubles that carry
    only the surfaces a given test exercises, and a missing property must read
    as "no baseline", not raise mid-persist and lose the whole snapshot.
    """
    baseline = getattr(conductor, "measure_entry_baseline", None)
    to_dict = getattr(baseline, "to_dict", None)
    if not callable(to_dict):
        return None
    record = to_dict()
    return dict(record) if isinstance(record, Mapping) else None


def _round_receipt_identity(conductor: Any) -> dict[str, Any] | None:
    """The conductor's round-receipt identity, or ``None`` (#2291).

    ``getattr`` for :func:`_predicted_spec_prior`'s reason: this persistence
    helper runs against conductor doubles carrying only the surfaces a given
    test exercises, and a missing property must read as "no receipt", never
    raise mid-persist and lose the whole snapshot.
    """
    record = getattr(conductor, "round_receipt_identity", None)
    return dict(record) if isinstance(record, Mapping) else None


def entry_baseline_prior_from_state(state: Mapping[str, Any] | None) -> Any:
    """The stage-1 entry baseline, as the conductor's ctor takes it (#2291).

    The read side of :func:`persist_conductor_state`'s
    ``verify_priors.entry_baseline`` and the exact mirror of
    :func:`commanded_delta_prior_from_state` below: durable state in, the
    ``measure_entry_baseline`` argument out. Named and module-level for the
    same reason — the seeding PATH is then drivable in a test without a relay.

    Returns an
    :class:`~jasper.active_speaker.crossover_v2.round_evidence.EntryBaseline`
    or ``None``. ``None`` is
    :data:`~jasper.active_speaker.crossover_v2.verification.BENEFIT_BASELINE_UNAVAILABLE`,
    which is INDETERMINATE and **not** a pass, and it covers the honest cases
    that mean one thing to the round — there is no comparable before: a state
    file written before this key shipped, a stage 1 whose baseline capture
    never landed, and a truncated or hand-edited record. Which of those it was
    is not recoverable from the file and the round does not branch on it.

    Shape validation belongs to ``EntryBaseline.from_dict``, which owns the
    "length-agreeing curve and mask, or nothing" rule.
    """
    from jasper.active_speaker.crossover_v2.round_evidence import EntryBaseline

    priors = (state or {}).get("verify_priors")
    record = priors.get("entry_baseline") if isinstance(priors, Mapping) else None
    return EntryBaseline.from_dict(record)


def alignment_prescription_prior_from_state(state: Mapping[str, Any] | None) -> Any:
    """The stage-1 delay prescription, as the conductor's ctor takes it (#2662).

    The read side of :func:`persist_conductor_state`'s
    ``verify_priors.alignment_prescription`` and the exact mirror of
    :func:`entry_baseline_prior_from_state`: durable state in, the
    ``alignment_prescription`` argument out. Named and module-level for the
    same reason — the seeding PATH is then drivable in a test without a relay.

    ``None`` is "this round prescribed no delay", and it also covers a state
    file written before this key shipped and a truncated or hand-edited record;
    the reader below says which of the last two in a WARNING, and the round
    does not branch on it. The BOUND is deliberately not re-applied here —
    :mod:`~jasper.active_speaker.crossover_v2.alignment_prescription` states
    why: it has one owner, and it is the request boundary.
    """
    from jasper.active_speaker.crossover_v2.alignment_prescription import (
        alignment_prescription_from_mapping,
    )

    priors = (state or {}).get("verify_priors")
    record = (
        priors.get("alignment_prescription") if isinstance(priors, Mapping) else None
    )
    return alignment_prescription_from_mapping(record)


def topology_prescription_prior_from_state(state: Mapping[str, Any] | None) -> Any:
    """Durable state in, the ``topology_prescription`` argument out.

    The exact mirror of :func:`alignment_prescription_prior_from_state`, module
    level for the same reason and reading the same durable ``verify_priors``
    block.  What it feeds is larger than a receipt field: the grading stage
    re-opens its session AT this topology, so a pin that failed to rehydrate
    would silently grade a pinned round's VERIFY against the crossover the
    speaker used to run.  The read-back is deliberately shape-only — see
    :func:`~jasper.active_speaker.crossover_v2.topology_prescription.topology_prescription_from_mapping`
    for why re-applying the bounds at grading time could only throw away the
    evidence of a round that really ran.
    """
    from jasper.active_speaker.crossover_v2.topology_prescription import (
        topology_prescription_from_mapping,
    )

    priors = (state or {}).get("verify_priors")
    record = (
        priors.get("topology_prescription") if isinstance(priors, Mapping) else None
    )
    return topology_prescription_from_mapping(record)


def blend_prescription_prior_from_state(state: Mapping[str, Any] | None) -> Any:
    """The stage-1 blend prescription, as the conductor's ctor takes it (A9).

    The read side of :func:`persist_conductor_state`'s
    ``verify_priors.blend_prescription``, and the exact mirror of
    :func:`alignment_prescription_prior_from_state` — durable state in, the
    ``blend_prescription`` argument out.

    **Without this arm the feature loses the thing it exists to bank.**
    ``verify_priors`` is rebuilt from the conductor on EVERY persist, and a
    stage-2 conductor holds no prescription — so stage 2 writes ``None`` over
    stage 1's record before the round receipt is ever written, and a round that
    ran a prescribed correction is banked as though its correction had been
    solved. That is the same stops-one-step-short shape as #2698: the value
    reaches the durable state and then nothing carries it the rest of the way.

    ``None`` is "this round prescribed no blend correction" — the automatic
    path — and it also covers a state file written before this key shipped and a
    truncated or hand-edited record. The BOUND is deliberately not re-applied:
    :mod:`~jasper.active_speaker.crossover_v2.blend_prescription` states why —
    it has one owner, and it is the boundary that accepted the document.
    """
    from jasper.active_speaker.crossover_v2.blend_prescription import (
        blend_prescription_from_mapping,
    )

    priors = (state or {}).get("verify_priors")
    record = (
        priors.get("blend_prescription") if isinstance(priors, Mapping) else None
    )
    return blend_prescription_from_mapping(record)


def blend_prescription_sha256_from_state(state: Mapping[str, Any] | None) -> str:
    """The digest beside the record above, or ``""``.

    Read separately because it is banked separately — see the persist's own
    comment for why the digest cannot live inside the record it describes.
    """
    priors = (state or {}).get("verify_priors")
    digest = (
        priors.get("blend_prescription_sha256") if isinstance(priors, Mapping)
        else None
    )
    return str(digest or "") if isinstance(digest, str) else ""


def _take_staged_prescription(round_ordinal: int) -> tuple[Any, Any]:
    """This round's staged prescription as ``(blend, driver)`` (A9).

    The ONE place a staged prescription enters the flow, and the one place its
    refusal is turned into a round that carries on without it. Named and
    module-level for
    :func:`alignment_prescription_prior_from_state`'s reason — the seeding path
    is then drivable in a test without a relay.

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
) -> tuple[tuple[Any, ...], str] | None:
    """This session's staged angle walk as ``(poses, consumer)``, or ``None``.

    :func:`_take_staged_prescription`'s twin: ONE take, at ONE place.
    ``None`` is every ordinary session, and every refused one.

    Guarantees: it NEVER raises, so a staged document can never cost a
    household its session — the three refusal classes are the spool's
    ``AngleRequestRefused``, the seam's ``LateralWalkRefused``, and the bare
    ``CrossoverV2FlowError`` the spool re-raises for a banked stop that no
    longer satisfies the seam's contract (a hand-edited angle), which that
    module deliberately does not re-wrap. Every one is journalled with its
    producing module's own slug. The consumer is always
    ``LATERAL_CONSUMER_FORWARD_MODEL`` — assigned here rather than read from the
    document, because it decides which pose table the walk runs and may have
    exactly one writer.

    ``consumed`` on the journal line is READ BACK from the spool, never
    asserted: its two unreadable arms deliberately do not consume, so a
    permissions mistake refuses every session until it is fixed rather than
    silently destroying the evidence of itself.

    ``lateral_group_present`` and ``plans_cloud_group`` are the session's own
    facts, passed in rather than read, because the composing seam may not read
    session flags.

    A walk does not survive its session. The consumer is not persisted and the
    document is single-use, so a session that lapses mid-walk re-opens in its
    ordinary shape and the operator stages again — which is the safe direction:
    a resumed half-walk would bank poses under a consumer nothing re-declared.
    """
    from jasper.active_speaker.angle_capture import (
        WALK_LATERAL_GROUP_ALREADY_PLANNED,
        WALK_STOP_NO_LONGER_VALID,
        LateralWalkRefused,
        session_lateral_walk,
    )
    from jasper.active_speaker.angle_capture_spool import (
        AngleRequestRefused,
        staged_angle_request_pending,
        take_staged_angle_request,
    )
    from jasper.active_speaker.crossover_v2.journey import (
        LATERAL_CONSUMER_FORWARD_MODEL,
    )
    from jasper.active_speaker.crossover_v2_flow import (
        CrossoverV2FlowError,
        position_angle_deg,
    )

    def refused(reason: str, detail: str) -> None:
        log_event(
            logger, "correction.crossover_v2_angle_walk_refused",
            level=logging.WARNING, reason=reason, detail=detail,
            # READ BACK, never asserted: the spool's two unreadable arms
            # deliberately do not consume.
            consumed=not staged_angle_request_pending(),
            session_continues=True,
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
        refused(exc.reason, exc.detail)
        return None
    except CrossoverV2FlowError as exc:
        # The third class: a banked stop the seam can no longer build. The spool
        # re-raises it un-wrapped because ``_validated_angle``'s own sentence
        # beats anything a second vocabulary could say, so it arrives with no
        # slug — it gets one here and keeps that sentence as the detail.
        refused(WALK_STOP_NO_LONGER_VALID, str(exc))
        return None
    log_event(
        logger, "correction.crossover_v2_angle_walk_taken",
        stops=len(prompts),
        angles=",".join(f"{position_angle_deg(p):+d}" for p in prompts),
        mover=request.mover,
        regimes=",".join(sorted({stop.regime for stop in request.stops})),
        consumer=LATERAL_CONSUMER_FORWARD_MODEL,
    )
    return prompts, LATERAL_CONSUMER_FORWARD_MODEL


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


def commanded_delta_prior_from_state(
    state: Mapping[str, Any] | None,
) -> tuple[Any, Any] | None:
    """The stage-1 commanded delta, as the conductor's ctor takes it (#2291).

    The read side of :func:`persist_conductor_state`'s
    ``verify_priors.commanded_delta`` and the exact mirror of
    :func:`pilot_transfer_prior_from_state` above: durable state in, the
    ``measure_commanded_delta`` argument out. Named and module-level for the
    same reason — the seeding PATH is then drivable in a test without a relay.

    ``None`` is the probe's
    :data:`~jasper.active_speaker.delta_probe.VERDICT_UNAVAILABLE`, which is
    **not** a pass, and it covers three honest cases that mean one thing to the
    probe — there is no commanded axis to grade against: a state file written
    before this key shipped, a trims-only candidate (which commands no shape at
    all), and a record that is not the pair this build writes.

    **A length disagreement is one of those, checked here (#2316 N3).** The two
    arrays are read separately, so a truncated or hand-edited record yields two
    valid arrays that are not a curve. Returning them would leave the two
    surfaces disagreeing in the journal: ``prepare_v2_verify``'s capability line
    would report the commanded delta PRESENT, and the probe's own warning would
    report it unavailable a moment later. Both degrade safely, but one of the
    two lines is false, and an operator reading the capability line has no way
    to know which. Refusing the pair here — rather than documenting the
    disagreement — makes the capability line true by construction, for the cost
    of one comparison.
    """
    return _delta_prior_from_state(state, "commanded_delta")


def declared_transfer_prior_from_state(
    state: Mapping[str, Any] | None,
) -> tuple[Any, Any] | None:
    """The stage-1 STATE axis, as the conductor's ctor takes it (#2614).

    The applied graph's own transfer against the uncorrected crossover — the
    axis the delta probe's two directional safety rules mask on. The exact twin
    of :func:`commanded_delta_prior_from_state` above, through the same reader,
    and it degrades the same way: ``None`` means the probe falls back to the
    CHANGE axis alone for those two rules, which is the pre-#2614 behaviour and
    an identity on a first-ever apply.
    """
    return _delta_prior_from_state(state, "declared_transfer")


def _delta_prior_from_state(
    state: Mapping[str, Any] | None, key: str,
) -> tuple[Any, Any] | None:
    """One ``verify_priors`` curve record, rehydrated — the shared reader.

    Both delta axes persist through :func:`_decimate_delta` and rehydrate
    through here, so the length check the docstring above argues for cannot end
    up applied to one axis and not the other.
    """
    import numpy as np

    priors = (state or {}).get("verify_priors")
    record = priors.get(key) if isinstance(priors, Mapping) else None
    if not isinstance(record, Mapping):
        return None
    freqs, delta = record.get("freqs_hz"), record.get("delta_db")
    if not freqs or not delta:
        return None
    if len(freqs) != len(delta):
        log_event(
            logger, "correction.crossover_v2_commanded_delta_malformed",
            level=logging.WARNING, prior=key,
            n_freqs=len(freqs), n_delta=len(delta),
        )
        return None
    return (
        np.asarray(freqs, dtype=float),
        np.asarray(delta, dtype=float),
    )


def _finite(value: Any) -> float | None:
    """Mirrors ``crossover_envelope_v2._finite``'s guard (reject bool,
    reject non-numeric, reject NaN/inf) — N1 (2026-07-24 review follow-up):
    this module and that one stay symmetric about what counts as a
    displayable number, rather than one layer trusting a raw ``float(v)``
    the other layer would refuse. That symmetry is one-directional until
    #2470 lands: the ``OverflowError`` guard below is this function's
    alone, and the twin is exactly the trusting layer this note warns
    about until it mirrors the guard.

    Never raises. An unbounded JSON integer (``10 ** 400``) makes
    ``float()`` raise ``OverflowError`` rather than returning ``inf`` —
    this runs on every :func:`crossover_v2_status_block` read (the
    wizard's poll path), so an escaping conversion would be a 500 on a
    plain page load, the same hazard :func:`_household_findings_status`
    already guards against, and the same fallback (#2245).
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _fc_hz_label(hz: float) -> str:
    """A crossover frequency as the household reads it: ``2250``, ``1787.5``.

    One formatter, because the apply path's refusal copy and the Undo path's
    declaration copy (#2292) name the same numbers to the same person, and two
    spellings of one frequency in two adjacent sentences is a defect the
    household is the first to notice.
    """
    return f"{hz:.1f}".rstrip("0").rstrip(".")


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


def _candidate_octave_reasons(
    linearization: Any, octaves: Mapping[str, Mapping[str, float]],
) -> dict[str, dict[str, str]]:
    """Per-role octave-band reason codes (``LinearizationFit.reason_summary``
    — the fit engine's own closed
    :class:`~jasper.active_speaker.linearization_envelope.ReasonCode`
    vocabulary), the sibling of :func:`_candidate_octave_summary`'s numbers.
    Same pure projection, same band keying: the fit computes both dicts over
    the same octave centers in the same pass, so they line up band-for-band
    (see ``linearization_fit._observe_octave_summary``'s own docstring).

    Band-for-band, but NOT role-for-role on its own — which is why the
    already-projected ``octaves`` is an argument rather than something this
    recomputes. ``linearization_fit._empty_fit`` (the envelope allowed
    correction nowhere) returns an EMPTY ``observe_octave_summary`` beside a
    fully populated ``reason_summary``, so a role can honestly have verdicts
    and no numbers. Keying off the numbers makes the reason set a subset of
    the octave set by construction: no role is ever handed a verdict for a
    band this candidate has no number in.

    **Why the numbers need this (#2638).** ``observe_octave_summary`` is
    ``working_db - frame_target_db`` across the WHOLE grid. Above a driver's
    own radiating band the crossover target dives at 24 dB/oct while the
    measurement floor stays put, so the difference explodes into a large
    POSITIVE number — stopband arithmetic, not performance. On 2026-08-16 a
    healthy candidate's "+23.0 dB" at 16 kHz read on the review screen as a
    runaway boost and nearly indicted a correction whose largest filter gain
    anywhere was +2.5 dB. The fit engine already labels every one of those
    octaves ``envelope_out_of_band``; this hop is what carries the label to
    the surface that shows the number.

    A SEPARATE key rather than a compound value, deliberately: a candidate
    persisted before #2638 carries the numbers with no reasons, and an absent
    key reads as "this build did not record why" — which renders exactly as
    it did before, instead of making every reader handle two shapes of the
    same key.
    """
    out: dict[str, dict[str, str]] = {}
    for role, fit in (linearization or {}).items():
        if not isinstance(fit, Mapping) or str(role) not in octaves:
            continue
        reasons = fit.get("reason_summary")
        if not isinstance(reasons, Mapping) or not reasons:
            continue
        role_reasons = {
            str(hz): code for hz, code in reasons.items()
            if isinstance(code, str) and code
        }
        if role_reasons:
            out[str(role)] = role_reasons
    return out


def _candidate_summary(
    candidate: Any, *, topology_pinned: bool = False,
    headroom_cost_basis: str | None = None,
) -> dict[str, Any] | None:
    # Lazy, like ``_candidate_headroom_cost_db``'s own import below it: this
    # module has no module-level numpy and the fit module does, so the
    # socket-activated wizard process only pays for it on a path that
    # genuinely has a candidate.
    from jasper.active_speaker.linearization_fit import (
        HEADROOM_COST_BASIS_REALIZED_PEAK_FULL_DOMAIN,
    )

    # WHICH era stamped the per-branch charges below, supplied by the CALLER
    # because only the caller knows. The default is this build's era, true on
    # the minting path; a candidate read OFF DISK records none of its own, so
    # republish passes UNKNOWN rather than letting a pre-#2758 candidate wear a
    # current label over numbers the widened grid now charges more for.
    stamped_basis = headroom_cost_basis or HEADROOM_COST_BASIS_REALIZED_PEAK_FULL_DOMAIN

    if candidate is None:
        return None
    analysis = candidate.analysis if isinstance(candidate.analysis, Mapping) else {}
    octaves = _candidate_octave_summary(candidate.linearization)
    return {
        "fingerprint": candidate.fingerprint,
        "program_id": candidate.program_id,
        "trims_db": dict(candidate.role_attenuations_db),
        # WHERE this candidate crosses, and whether the round was PINNED there.
        # The corner comes off the candidate, read by the module that owns the
        # shape; the bit comes from the session, because a corner cannot say
        # who chose it — and the household copy must never word a pinned corner
        # as a measured result, the same rule ``polarity_pinned`` below carries.
        "crossover": candidate_topology(candidate),
        "crossover_pinned": bool(topology_pinned),
        "alignment": candidate.alignment.to_dict(),
        # Threaded through for the conductor's own trust gate
        # (crossover_v2_flow.ALIGNMENT_CONFIDENCE_TRUST_FLOOR) and, on the
        # RESULT screen, the collapsed expert disclosure.
        "alignment_confidence": analysis.get("alignment_confidence"),
        # RESULT-screen expert disclosure only (crossover_envelope_v2
        # ._candidate_review_payload's "ripple_db").
        "predicted_ripple_db": analysis.get("predicted_ripple_db"),
        # WHICH objective committed this candidate's (polarity, delay) pair, and
        # whether the committed delay left the comb lobe its physical anchor
        # owns (#2598, and the #2607 panel's S2/S3). The objective reaches the
        # review screen, which must not word a declared-design commitment as a
        # measured one; the lobe flag is a receipt line, because that mode is
        # magnitude-flat and an on-axis VERIFY cannot contradict it.
        "alignment_objective": analysis.get("alignment_objective"),
        # …and whether the polarity above was MEASURED or held by the request.
        # Its own key because the objective cannot say: a pinned round commits
        # the same `explicit_prescription_committed` an unpinned one does.
        "polarity_pinned": bool(analysis.get("polarity_pinned")),
        "left_anchor_lobe": analysis.get("left_anchor_lobe"),
        # Gauge fix (2026-07-24): WHY Layer-1a driver linearization did or
        # didn't run this attempt — "" / "fitted" / "trim_rejected" /
        # "ineligible_mic_tier" / "ineligible_repeats" / "fit_failed".
        "linearization_outcome": str(
            getattr(candidate, "linearization_outcome", "") or ""
        ),
        # Gauge fix (2026-07-24): per-role top-octave deficits (the number
        # that says "the top octave is 9 dB down and nothing corrected it").
        "linearization_octaves": octaves,
        # WHY each of those octaves reads the way it does (#2638). The number
        # alone cannot distinguish "the top octave is 9 dB down and nothing
        # corrected it" from "this octave is past the driver's own band, where
        # the difference is the crossover's rolloff rather than anything the
        # driver did." Both are honest; only one is about performance, and the
        # screen was showing the second as if it were the first.
        "linearization_octave_reasons": _candidate_octave_reasons(
            candidate.linearization, octaves
        ),
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
        # two-stage commission D3) — see ``stamped_basis`` above for why it
        # comes from the caller, ``linearization_fit.HEADROOM_COST_BASIS_*``
        # for why an era is recorded rather than sniffed, and
        # ``crossover_envelope_v2._candidate_review_payload`` for what an
        # unknown one renders as.
        "headroom_cost_basis": stamped_basis,
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


def _fc_selection_summary(conductor: Any) -> dict[str, Any] | None:
    """R17's Fc RECOMMENDATION, small enough to live in durable state.

    ``None`` when no candidate sweep ran (an Express/no-walk session, a
    conductor double without the surface) — never a fabricated "keep what you
    have", which would read as a verdict the measurement never reached.

    What rides: the verdict and frequencies; ordered attempted/skipped evidence;
    the single comparison-completeness fact; the derived bounds; and compact
    per-candidate scores. What does NOT ride: the operators and predicted sums
    behind them. Those are working evidence, already released by the time this
    is written, and a durable copy would be a second answer to "what did this
    session measure" that nothing reads back.
    """
    selection = getattr(conductor, "fc_selection", None)
    if selection is None:
        return None
    return {
        "verdict": selection.verdict,
        "configured_hz": round(float(selection.configured_hz), 1),
        "recommended_hz": (
            round(float(selection.recommended_hz), 1)
            if selection.recommended_hz is not None else None
        ),
        "margin_db": (
            round(float(selection.margin_db), 3)
            if selection.margin_db is not None else None
        ),
        # k of N, disclosed rather than hidden: the accept window is bounded by
        # the phone's own result deadline, so a loaded Pi legitimately scores
        # fewer candidates than it proposed.
        "evaluated": int(selection.evaluated),
        "planned": int(selection.planned),
        "candidate_order": [
            round(float(fc), 1) for fc in selection.candidate_order
        ],
        "attempted": [round(float(fc), 1) for fc in selection.attempted],
        "skipped": [
            {"fc_hz": round(float(fc), 1), "reason": str(reason)}
            for fc, reason in selection.skipped
        ],
        "comparison_complete": bool(selection.comparison_complete),
        # WHY the set was the size it was — each bound traceable to a
        # declaration the household can edit.
        "limits": {k: round(float(v), 1) for k, v in selection.limits.items()},
        "refusals": [
            [round(float(fc), 1), str(reason)] for fc, reason in selection.refusals
        ],
        "scores": [
            {
                "fc_hz": round(float(s.fc_hz), 1),
                "score": round(float(s.score), 3),
                "worst_db": round(float(s.anchor.worst_db), 3),
                "worst_hz": round(float(s.anchor.worst_hz), 1),
                "lateral_excess_db": round(float(s.lateral_excess_db), 3),
                "headroom_cost_db": round(float(s.headroom_cost_db), 3),
                "poses": int(s.n_poses),
            }
            for s in selection.scores
        ],
    }


def _delta_probe_summary(probe: Any) -> dict[str, Any]:
    """The delta probe's verdict, small enough to live in durable state (#1811).

    Everything a reader needs to judge a non-rollback finding — the two that by
    design produce no refusal and would otherwise be invisible outside the
    journal: what the verdict was, why, the level move the emitter declared, the
    one it could not account for, and (since #2521) the two terms of the frame
    it removed. The full map (per-bin errors, exceedance width, gain factor,
    spatial arm, both bands) stays on
    ``event=correction.crossover_v2_delta_probe`` — this is the durable summary,
    not a second copy of the record.

    The frame terms are here rather than only on the journal line for exactly
    the reason ``residual_offset_db`` is: ``frame_mismatch`` is a claim ABOUT
    those two numbers, so a durable record naming the verdict without them
    would say a level and a slope explained the finding while withholding what
    they were. ``None`` when no frame was fitted — never 0.0, which would read
    as "measured, and flat".

    ``frame_n_bins`` / ``frame_band_hz`` ride beside them because they are
    ``frame_fit``'s own stated defence against an ill-conditioned fit: two
    scalars fitted over a narrow quiet span can be large and mean nothing, and a
    reader judging this verdict needs to see how much was trusted. They cannot
    change the verdict — the gate only narrows — but they bound how much weight
    the two terms above can carry.

    ``entry_anchor_offset_db`` and the three ``quiet_*`` terms are here for the
    same reason one step further (#2533). ``residual_offset_db`` is now a level
    CHANGE rather than an absolute disagreement, so the record has to say what
    standing offset was subtracted to make it one — and ``None`` there means
    nothing was, which changes how the number should be read. The quiet terms
    bound the claim: ``uncommanded_level_shift_outside_probe_band`` is a verdict
    ABOUT how little of the graded band its evidence covered, so the covered band
    and the coverage travel with it. ``quiet_core_band_hz`` is deliberately not a
    second copy of ``frame_band_hz``: that one is the min/max, and this one is
    the interquartile span, which is the difference the verdict turns on.

    ``getattr`` throughout, and through the nested ``frame`` too: this runs
    against duck-typed probe stand-ins in tests, and an absent field is
    "unknown", never a raise that loses the whole snapshot.
    """
    frame = getattr(probe, "frame", None)
    return {
        "verdict": str(getattr(probe, "verdict", "") or ""),
        "reason": str(getattr(probe, "reason", "") or ""),
        # Whether the realized-energy half of the safety axis ran (series-2 D1).
        # Here for ``residual_offset_db``'s reason one step further: that number
        # needed the record to say what was subtracted to make it a change, and
        # this one needs the record to say whether the subtraction was possible
        # at all — a first-ever round takes the ``state_axis_only`` branch, so
        # its axis reports SAFE with that half unrun.
        #
        # A FORENSIC state key: no renderer reads it today (the done screen's
        # caveat keys on the probe's verdict). It is here because the round
        # receipt is write-once and this record is the LIVE one — the surface
        # ``/state``, the doctor and the done screen would each have to read it
        # from — so a fact only the receipt holds is a fact no live surface can
        # ever show.
        "safety_anchored": bool(getattr(probe, "safety_anchored", False)),
        "expected_offset_db": getattr(probe, "expected_offset_db", 0.0),
        "residual_offset_db": getattr(probe, "residual_offset_db", None),
        "entry_anchor_offset_db": getattr(probe, "entry_anchor_offset_db", None),
        "quiet_n_bins": getattr(probe, "quiet_n_bins", None),
        "quiet_core_band_hz": (
            list(core) if isinstance(
                core := getattr(probe, "quiet_core_band_hz", None), tuple
            ) else None
        ),
        "quiet_probe_coverage": getattr(probe, "quiet_probe_coverage", None),
        "frame_offset_db": getattr(frame, "offset_db", None),
        "frame_tilt_db_per_octave": getattr(frame, "tilt_db_per_octave", None),
        "frame_n_bins": getattr(frame, "n_bins", None),
        "frame_band_hz": (
            list(band) if isinstance(band := getattr(frame, "band_hz", None), tuple)
            else None
        ),
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
    # #2291: which arm of ``correction_rollback_failed`` this is. Gated on the
    # SAME code-agreement check for the same reason — a terminal arm passing a
    # different ``failure_code`` must not inherit this round's anchor fact and
    # render a sentence about a restore that code never attempted.
    failure_rollback_anchor = (
        getattr(conductor, "last_failure_rollback_anchor", None)
        if failure_code == getattr(conductor, "last_failure_code", None)
        else None
    )
    prior = load_v2_state() or {}
    # #2616: let the journey learn about a restore it could not see.
    #
    # ``observe_restore`` clears the durable ``applied`` in place and holds no
    # conductor, so a LIVE session that rolled back — the delta probe's own
    # ``rollback`` seam, or the round's adoption restore — kept ``applied``
    # True in memory. The write below reads that stale True off the snapshot
    # and put it straight back over the clear, which is one fact with two
    # owners and the durable one losing.
    #
    # Resolved in the owner's favour rather than by special-casing the write:
    # the durable state is the authority on whether a restore HAPPENED, the
    # journey is the owner of the flag, so this tells the journey and then
    # writes what it says. Scoped to the SAME session, because a prior
    # session's restore says nothing about this one.
    #
    # This is not the SF1 carry-forward's inverse and does not weaken it. That
    # guard (below) only ever sets True, protecting a stop that lands while an
    # apply is in flight; it reads ``prior`` too, so after this correction it
    # sees ``applied`` already False and correctly declines to fire.
    if (
        prior.get("applied") is False
        and prior.get("session_id") == snap.session_id
        and snap.applied
    ):
        conductor.note_restore_observed()
        snap = conductor.snapshot()
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
        "candidate": _candidate_summary(
            conductor.candidate,
            # ``getattr`` for ``_predicted_spec_prior``'s reason: this snapshot
            # serializes duck-typed conductors too, and a stand-in without the
            # property means "not pinned", which is what an ordinary round is.
            topology_pinned=(
                getattr(conductor, "topology_prescription_record", None) is not None
            ),
        ),
        "sound_design_revision": (
            getattr(conductor, "sound_design_revision", None)
            if getattr(conductor, "sound_design_revision", None) is not None
            else prior.get("sound_design_revision")
        ),
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
                # WHICH OF §7's CLAIMS WERE PROVED, on EVERY outcome including
                # a pass (R18, #1868). Same shape and argument as the graded
                # band and frame above, one step further: those bound how wide
                # and how honest a claim is, this says which claims exist. Two
                # of the four are structurally not-evaluated (VERIFY plays one
                # summed sweep), and "Verified." over an unstated claim set
                # reads as all four.
                **(
                    {"claims": dict(conductor.verify_claims)}
                    if conductor.verify_claims
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
                # (#1811). A non-rollback non-matched verdict — ``level_mismatch``
                # and ``frame_mismatch`` — otherwise reached no surface at all: the
                # refusal path ignores it by design, so the household saw a
                # clean "Verified." over a shape question the probe never got
                # to answer. A summary, not the whole map: the verdict, why, and
                # the numbers each non-rollback verdict is a claim ABOUT — see
                # ``_delta_probe_summary``. The full record stays in the journal.
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
                # #2291, on exactly the key above's terms: absent is "the
                # question does not apply to this code, or predates the
                # record", and the copy owner reads absent as the Undo arm.
                # Writing a bare False for the unknown case would tell a
                # household with a perfectly good anchor that they have none.
                **(
                    {"rollback_anchor_available": bool(failure_rollback_anchor)}
                    if failure_rollback_anchor is not None else {}
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
        # R17's Fc recommendation. Sound remains the declaration writer;
        # Review may accept the exact retained candidate through that boundary.
        "fc_selection": _fc_selection_summary(conductor),
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
            # The delta probe's COMMANDED axis (#2291 Phase 3). Produced by the
            # stage-1 fit and consumed by the stage-2 probe — which runs in a
            # different process, against a conductor that never ran a fit — so
            # exactly like ``predicted_sum`` above, this durable state is the
            # only channel it has. Until it crossed, every shipped stage 2
            # graded its correction with the shortfall-vs-model-error
            # discriminator switched off, reporting ``unavailable``.
            #
            # Read through ``getattr`` for ``_predicted_spec_prior``'s reason:
            # this function persists duck-typed conductors too, and a stand-in
            # without the property means "nothing commanded" — which is what
            # the key's own absence already means downstream.
            "commanded_delta": _decimate_delta(
                getattr(conductor, "measure_commanded_delta", None)
            ),
            # The delta probe's STATE axis beside its CHANGE axis (#2614): what
            # the applied graph declares it does against the uncorrected
            # crossover. It crosses for exactly ``commanded_delta``'s reason and
            # is read the same way — a stand-in without the property means "no
            # state axis", which downstream degrades to the change axis alone
            # for the two directional safety rules.
            "declared_transfer": _decimate_delta(
                getattr(conductor, "measure_declared_transfer", None)
            ),
            # The MEASURED side of the same comparison (#2522). Its two
            # neighbours above are what the correction PREDICTED and COMMANDED;
            # without this one, a disputed probe verdict could only be
            # re-examined by measuring the speaker again, because the evidence
            # that produced it lived in one process's memory and died with it.
            #
            # Read through ``getattr`` for ``_predicted_spec_prior``'s reason:
            # this function persists duck-typed conductors too, and a stand-in
            # without the property means "nothing measured" — which is what the
            # key's own absence already means downstream.
            "verify_measured": _decimate_verify_measured(
                getattr(conductor, "verify_tracking_curve", None)
            ),
            # #2662's provenance. It crosses for exactly ``commanded_delta``'s
            # reason — produced by the stage that MEASURES the arm, banked by
            # the stage that GRADES it, and those are different sessions in
            # different processes — and is read the same way, through
            # ``getattr``, so a duck-typed stand-in without the property means
            # "no prescription", which is what the key's own absence already
            # means downstream.
            "alignment_prescription": getattr(
                conductor, "alignment_prescription_record", None
            ),
            # The crossover pin, crossing on the identical route and read the
            # same way. It carries MORE weight than its neighbour, not less:
            # stage 2 re-opens at the topology this names, so without it the
            # VERIFY of a 4000 Hz round would be graded against the incumbent
            # corner's design target — the applied graph judged for not being
            # the crossover it replaced.
            "topology_prescription": getattr(
                conductor, "topology_prescription_record", None
            ),
            # A9's provenance, and it crosses for the line above's reason, read
            # the same way: the stage that TAKES a blend prescription is stage 1
            # and the stage that banks the round's receipt is stage 2, so
            # durable state is the only channel it has. ``None`` means the
            # round's blend correction came from decision 10's solver, which is
            # what every automatic round banks — so a series read back later can
            # attribute an outcome to the class that produced it, which is the
            # comparison the prescriber loop exists to make possible.
            "blend_prescription": getattr(
                conductor, "blend_prescription_record", None
            ),
            # …and WHICH document asked, the fact that lets a reader six weeks
            # later find the evidence packet and the conversation behind the
            # numbers. Its own key rather than a field inside the record above,
            # on ``alignment_objective``'s rule — and here that rule is
            # load-bearing rather than tidy: the record has to round-trip
            # through ``blend_prescription_from_mapping`` for stage 2 to
            # rehydrate it, and that reader refuses an unknown field instead of
            # ignoring it, so a digest nested inside would make the whole record
            # unreadable.
            "blend_prescription_sha256": str(
                getattr(conductor, "blend_prescription_sha256", "") or ""
            ),
            # …and WHICH commitment the fit reached, the fact that turns the
            # block above from "this arm was asked for" into "this arm ran".
            # Its own key rather than a field inside the prescription: the
            # prescription is the REQUEST and this is the OUTCOME, they are
            # written at different moments by different owners, and nesting one
            # in the other would make a round that prescribed nothing have
            # nowhere to record an objective it still has.
            "alignment_objective": getattr(
                conductor, "measure_alignment_objective", "",
            ),
            # #2291's measured "before": the summed capture stage 1 takes at
            # the mark immediately before apply, which stage 2's benefit
            # verdict differences its own capture against. Produced in one
            # process and graded in another, so — exactly like
            # ``predicted_sum`` and ``commanded_delta`` above — this durable
            # state is the only channel it has.
            #
            # Already bounded: the record's curve is reduced to
            # ``round_evidence.BENEFIT_CURVE_MAX_BINS`` (the same 512
            # ``_decimate_sum`` applies) at capture time, on BOTH sides of the
            # comparison, so no decimation belongs here — re-gridding one side
            # after the fact is how a grid mismatch gets manufactured.
            #
            # Read through ``getattr`` for ``_predicted_spec_prior``'s reason,
            # and ``to_dict`` behind a type check for the same one: a duck-typed
            # conductor without the property, or with something that is not an
            # ``EntryBaseline``, means "no baseline" — which is what the key's
            # own absence already means downstream — never a raise that loses
            # the whole snapshot.
            "entry_baseline": _entry_baseline_prior(conductor),
            # What stage 1 PROPOSED, as an identity (#2392). The round receipt
            # is written by the stage that GRADES, which runs in a different
            # process against a conductor that never planned anything — so,
            # exactly like ``commanded_delta`` and ``entry_baseline`` above,
            # this durable state is the only channel the fingerprint has.
            #
            # The fingerprint travels, never the proposal: reassembling one at
            # VERIFY out of the decimated priors around it would digest to a
            # different value, and a receipt naming a proposal that never
            # existed is worse than one naming the candidate honestly.
            #
            # Read through ``getattr`` for ``_predicted_spec_prior``'s reason:
            # this function persists duck-typed conductors too, and a stand-in
            # without the property means "nothing proposed" — which is what the
            # empty string already means downstream.
            "proposal_fingerprint": str(
                getattr(conductor, "measure_proposal_fingerprint", "") or ""
            ),
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
    # #2291's entry baseline needs NO carry-forward, and that is worth stating
    # because its immediate neighbour above needs one. The difference is where
    # the fact lives. ``pilot_transfer_reference`` is seeded into the conductor
    # as history it may only DISCLOSE (#1927) and is not what
    # ``verify_pilot_transfer_reference`` reports, so a re-arm's own persist
    # really would blank it. The entry baseline is seeded into the SAME field
    # its own capture writes (``measure_entry_baseline``) — exactly like
    # ``predicted_sum`` and ``commanded_delta``, neither of which carries
    # forward either — so a stage-2 persist re-writes the record its conductor
    # was constructed with.
    #
    # Mutation-verified rather than argued (2026-08-11): a carry-forward branch
    # was written here first, and deleting it changed no test outcome, because
    # no path reaches it. Keeping it would have been a branch nothing can
    # distinguish, and it would have weakened the real pin — that a MEASURING
    # session replaces the previous round's "before" instead of letting this
    # round's "after" be differenced against a stale one, which is exactly the
    # false comparison #2291 exists to stop.
    #
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
        # R17's Fc selection, on the SAME predicate and for the same
        # reason: it is produced at the lateral walk's close, which only a
        # MEASURING session runs, and the DONE screen belongs to stage 2 —
        # whose conductor has no ``fc_selection`` and would persist ``None``
        # over it. Losing it there would show the household "your crossover
        # could be 1750 Hz" while they decide and then nothing at all once the
        # tuning is applied, where its exact provenance still matters.
        if (
            isinstance(prior.get("fc_selection"), Mapping)
            and state["fc_selection"] is None
        ):
            state["fc_selection"] = dict(prior["fc_selection"])
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
    state["accepted_sound_revision"] = (prior.get("accepted_sound_revision")
        if PHASE_MEASURE not in snap.session_phases else None)
    # Takes ``accepted_sound_revision``'s session-gated shape, not
    # ``sound_declaration_undo``'s unconditional one, because it is scoped to
    # exactly that token: it is the inverse of a save the apply has not yet
    # committed to a graph, readable only while the review that saved Sound is
    # still the current one. A fresh MEASURE clears the token, and a record
    # that outlived it would be an inverse nothing can apply.
    state["accepted_sound_declaration_change"] = (
        prior.get("accepted_sound_declaration_change")
        if PHASE_MEASURE not in snap.session_phases else None)
    # #2292's declaration-undo record is the FOURTH key in the host-owned class
    # described above — ``observe_apply_success`` writes it beside
    # ``pre_apply_profile``, the conductor neither produces nor reads it — so it
    # takes that key's UNCONDITIONAL shape, and the mechanical guard
    # ``test_every_host_owned_apply_key_survives_persist_conductor_state``
    # covers it automatically.
    #
    # Deliberately NOT the shape of ``accepted_sound_revision`` directly above,
    # even though the alternative-Fc accept is what makes both non-null:
    #
    #   * ``accepted_sound_revision`` is the REVIEW-binding token
    #     ``_update_current_review`` gates the apply on. It is scoped to the
    #     review that saved Sound, so a fresh MEASURE must clear it or the next
    #     accept would skip its own Sound save.
    #   * this record is the inverse of a LIVE applied declaration, which
    #     outlives that review exactly as long as the graph does. Session-
    #     scoping it would reproduce the W6.12 P0 one field over: the deferred
    #     VERIFY that auto-arms after every apply persists under a brand-new
    #     session id, so the very first Undo after every apply would find no
    #     record and leave ``/sound`` declaring the undone crossover.
    state["sound_declaration_undo"] = prior.get("sound_declaration_undo")
    # #2537's round anchor is the FIFTH key in the same host-owned class, and
    # takes the same unconditional shape for the same reason — ``round_anchor``
    # describes the live apply, and the deferred VERIFY that auto-arms after
    # every apply persists under a brand-new session id, so session-scoping it
    # would blank the restore's divergence check on the very first re-arm.
    state[ROUND_ANCHOR_STATE_KEY] = prior.get(ROUND_ANCHOR_STATE_KEY)
    state["apply_blocked"] = (
        prior.get("apply_blocked")
        if prior.get("session_id") == snap.session_id
        else None
    )
    # #2291: WHERE this round's receipt landed — round id plus the bundle
    # artifact's fingerprint, so the next round resolves the previous one by
    # identity instead of scanning bundles. Carried forward like the anchor
    # keys above rather than session-scoped: the receipt describes the graph
    # currently on the speaker, and that outlives the session that wrote it.
    # A conductor that graded no round contributes ``None``, which must not
    # erase the identity a previous one recorded.
    receipt_identity = _round_receipt_identity(conductor)
    state["round_receipt"] = (
        receipt_identity
        if receipt_identity is not None
        else prior.get("round_receipt")
    )
    prior_grade = (crossover_v2_status_block() or {}).get("post_apply_grade")
    prior_outcome = (
        str(prior_grade.get("outcome") or "")
        if isinstance(prior_grade, Mapping)
        and prior.get("session_id") == snap.session_id else ""
    )
    # Durable (#2291): this write records an immutable receipt's identity. A
    # power cut that lost it would leave a receipt in the bundle that nothing
    # points at, which is the "falsifies a receipt" half of save_v2_state's
    # durability rule. Only when there IS a new identity — the ordinary
    # per-capture persist stays cheap.
    save_v2_state(state, durable=receipt_identity is not None)
    from jasper.active_speaker.crossover_v2_flow import PHASE_DONE

    grade = (crossover_v2_status_block() or {}).get("post_apply_grade")
    grade = grade if isinstance(grade, Mapping) else {}
    if (
        _phase_from_state(state) == PHASE_DONE
        and _phase_from_state(prior) != PHASE_DONE
    ) or (
        grade.get("outcome") == RESULT_KEEP_PREVIOUS
        and prior_outcome != RESULT_KEEP_PREVIOUS
    ):
        log_event(
            logger, "correction.crossover_v2_result_classified",
            session_id=snap.session_id, outcome=grade.get("outcome") or RESULT_INCONCLUSIVE,
            comparison_complete=grade.get("comparison_complete"), improvement_db=grade.get("improvement_db"),
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
    lets ``_phase_from_state`` resolve to ``PHASE_APPLYING`` (not
    ``PHASE_CHECK``) so the envelope's apply-step failure screen — and the
    specific blocked-issue nudge layered onto it — can actually render;
    before this fix the reset always won, so that nudge was unreachable in
    production (only reachable by injecting the phase directly in a test).
    """
    from jasper.active_speaker.crossover_v2_flow import REASON_APPLY_FAILED

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
        # relay trouble is a cleanup fault, not a commissioning verdict.
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
    provenance: CaptureProvenanceRecorder | None = None,
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
    flow phase — ``crossover_v2_flow.CrossoverV2Session.consume_capture``
    always passes it. It is NOT the same value as ``program.phase``: every
    cloud position plays the verify-shaped summed sweep, so
    ``program.phase == "verify"`` even during
    PHASE_CLOUD_MEASURE/PHASE_CLOUD_VERIFY. Retention (below) must label a
    capture with the flow's phase, never the program's, or every cloud
    position gets retained as "verify" (the bug this fixes — 32 of 45
    retained sidecars, per the 2026-07-29 WO-0 retrospective). No default:
    see ``crossover_v2_flow.AnalyzeCapture``'s docstring for why a silent
    fallback to ``program.phase`` is exactly the defect class being fixed.

    ``provenance`` (optional) is the session's
    :class:`~jasper.active_speaker.capture_provenance.CaptureProvenanceRecorder`
    — the same object ``bind_production_play`` records into, and the only way a
    retained capture can name the graph it went through. Omitted, retention
    behaves exactly as before.
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
            # #2094: the phone's own frame counters, reconciled against the
            # frames just decoded. This seam is the ONLY place both halves of
            # the ledger exist — the page's account arrives on the relay's
            # event channel, the received count comes out of the WAV — so it is
            # the only place the comparison can be made.
            capture_report=getattr(result, "capture_integrity", None),
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
            # THIS capture's stimulus, consumed once: a second analyze with no
            # play between gets ``None``, never the last capture's context.
            provenance=provenance.take() if provenance is not None else None,
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
    provenance: CaptureProvenance | None = None,
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

    ``provenance`` is what the play seam observed while THIS capture's stimulus
    was emitting (:mod:`jasper.active_speaker.capture_provenance`). Handed in,
    never read here: by then the routing graph is restored and the fader may
    have moved, so every one of those facts would be read after the fact.
    ``None`` leaves the block absent rather than claiming an unmeasured context.

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
    if not capture_dump_enabled():
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
        # What the capture was taken THROUGH — fader, session volume, the graph
        # actually loaded, the stimulus. Why a config path could not answer the
        # graph half: ``capture_provenance``'s module docstring (2026-08-19).
        if provenance is not None:
            sidecar["provenance"] = provenance.to_dict()
        # The phone's own account of the recording conditions (issue #2151):
        # whether the capture page held the foreground, plus its render-graph
        # block counters. Written ONLY when reported, so an older page leaves
        # the key absent rather than claiming a clean take it never measured.
        # This is what lets a ±128-sample splice be attributed to the capture
        # side or the host side without re-analysing the WAV.
        capture_integrity = getattr(result, "capture_integrity", None)
        if isinstance(capture_integrity, dict) and capture_integrity:
            sidecar["capture_integrity"] = capture_integrity
        # The reconciliation of that report against what actually arrived
        # (issue #2094) — the page's claim and the host's count side by side,
        # with the losing hop named. Written whenever the analysis carries one,
        # which is every capture analysed through the live seam; a test double
        # that returns something other than a ProgramAnalysis leaves it absent.
        frame_ledger = getattr(analysis, "frame_ledger", None)
        if frame_ledger is not None:
            sidecar["frame_ledger"] = frame_ledger.to_dict()
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


def bind_round_receipt(
    store: Any, relay_session_id: str, refs: dict[str, Any]
) -> Callable[[Mapping[str, Any]], str]:
    """The conductor's ``publish_round_receipt`` seam (#2291).

    Writes ONE immutable receipt per round as a bundle artifact at
    ``crossover_v2/<relay_session_id>/round_receipt.json``, through
    :meth:`publish_json_artifact` + reopen-and-compare — the R21 accept-receipt
    pattern verbatim (``commissioning_verification._receipt``). That store is
    write-once, canonical-JSON, fsync'd (file **and** parent directory) and
    tamper-checked on the way out, which is what a receipt needs and what a
    ``/var/lib/jasper/*.json`` file is not. It also puts the receipt beside
    ``check.json``, ``candidate.json`` and the retained positions — the
    artifacts its own ``evidence_identities`` name, which is what makes them
    resolvable at all.

    Raises rather than swallowing: the store is deliberately strict, and the
    fail-soft boundary is the round coordinator's own receipt writer
    (:func:`jasper.active_speaker.crossover_v2.coordinator.run_round` catches
    it), exactly as it is for :func:`bind_position_retention`. Keeping the
    boundary there preserves the strictness for every other caller of this
    store.
    """

    def publish_round_receipt(receipt: Mapping[str, Any]) -> str:
        artifact = store.publish_json_artifact(
            f"crossover_v2/{relay_session_id}/round_receipt.json", dict(receipt)
        )
        reopened = store.reopen_json_artifact(artifact)
        if reopened != dict(receipt):
            raise RuntimeError("published round receipt changed on exact readback")
        refs["round_receipt_artifact"] = artifact.fingerprint
        return str(artifact.fingerprint)

    return publish_round_receipt


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

    Fail-soft at the CALLER (``CrossoverV2Session._run_cloud_pipeline``):
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


def bind_production_play(
    *,
    run_async: Any,
    camilla_factory: Any,
    evidence_store: Any,
    relay_session_id: str,
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
    on_playback_started: Callable[[Any], None] | None = None,
    provenance: CaptureProvenanceRecorder | None = None,
) -> Callable[[str, Any], None]:
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

    ``on_playback_started`` (optional) is fired with the program at the instant
    its WAV reaches the playback call — the closest the host gets to "audio
    starts now" without reaching into the shared aplay path. It is what anchors
    the phone's pre-tone phase ladder (:func:`~jasper.web.correction_crossover_v2_relay.start_program_phase_ladder`);
    omitted, playback is unchanged and the caller keeps whatever progress
    reporting it had.

    ``provenance`` (optional) is the session's
    :class:`~jasper.active_speaker.capture_provenance.CaptureProvenanceRecorder`.
    **This function is the one owner of "which graph did the capture go
    through"**: the branch below either loads the transient routing graph or
    deliberately does not, and no downstream reader can recover that from a
    file path — see that module for why. So the branch states it. The
    observation is taken as late as the seam allows (for CHECK/MEASURE, inside
    the writer lock, after the load, immediately before the WAV handoff) and
    only while retention is on — :func:`capture_dump_enabled`.

    ON-DEVICE: not exercised hardware-free; W6 validates acoustically.
    """
    from jasper.active_speaker.crossover_v2_flow import (
        SUMMED_SWEEP_PHASES,
        bind_program_playback_seams,
    )
    from jasper.active_speaker.web_commissioning import DEFAULT_CAMILLA_CONFIG_DIR
    from jasper.audio_measurement.program import write_program_wav

    resolved_config_dir = (
        config_dir if config_dir is not None else str(DEFAULT_CAMILLA_CONFIG_DIR)
    )

    def _play(phase: str, program: Any) -> None:
        bundle_dir = evidence_store.bundle_dir
        wav_rel = f"crossover_v2/{relay_session_id}/{phase}_program.wav"
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
                    / f"crossover_v2/{relay_session_id}/summed_program.wav"
                )
                if not summed_wav_path.exists():
                    write_program_wav(summed_wav_path, program)
            except (OSError, ValueError, TypeError, AttributeError):
                log_event(
                    logger, "correction.crossover_v2_summed_program_persist_failed",
                    level=logging.WARNING, phase=phase, exc_info=True,
                )

        def _observe(open_cam: Callable[[], Any], graph_kind: str) -> Any:
            """Awaitable: record what this stimulus plays THROUGH, fail-soft."""
            return record_capture_provenance(
                provenance, open_cam=open_cam, graph_kind=graph_kind, program=program,
                artifact=artifact, read_volume_plan=session_volume_plan,
            )

        async def _play_body() -> None:
            from jasper.active_speaker.capture_provenance import (
                GRAPH_KIND_APPLIED,
                GRAPH_KIND_PROGRAM_ROUTING,
            )
            from jasper.active_speaker.program_playback import (
                verified_program_aplay,
            )

            # Observing costs CamillaDSP round-trips, so it is bought only when
            # retention is on; absent the marker this path is one Path.exists().
            observing = provenance is not None and capture_dump_enabled()

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
                if observing:
                    await _observe(camilla_factory, GRAPH_KIND_APPLIED)
                if on_playback_started is not None:
                    on_playback_started(program)
                await verified_program_aplay(
                    bundle_dir, artifact, timeout_s=60.0
                )
                return
            from jasper.active_speaker.camilla_yaml import (
                active_emit_devices,
                emit_active_speaker_program_config,
            )
            from jasper.active_speaker.program_playback import play_program

            # BOTH HALVES OF THE DEVICE BLOCK, DERIVED TOGETHER (issue #2450).
            # ``playback_device`` is already marker-aware — on an armed box
            # ``resolve_active_playback_device`` answers the ACTIVE RING — but
            # naming only the sink left the emitter to default its capture lane
            # to the snd-aloop tap, which under ``shm_ring`` fan-in has stopped
            # feeding: Stage 1's per-driver sweeps would excite the ring while
            # CamillaDSP captured a device nobody writes. Digital silence, every
            # daemon healthy, and no gate to catch it — the capture-channel
            # check compares 2 == 2 and the arm's width gate only holds
            # ring-NAMED lanes to the wire. ``active_emit_devices`` is the one
            # place that answers for a ring PCM, so this site asks it rather
            # than learning the ring; on every unarmed box it returns today's
            # literals and this emit is byte-identical.
            #
            # EVERY field it derives is forwarded. A subset is the same defect
            # one level up (#2343/#2359/#2363's family), which is why
            # ``test_every_emit_devices_field_reaches_the_emitter`` walks
            # ``dataclasses.fields`` at this site too.
            devices = active_emit_devices(playback_device, topology=topology)
            program_yaml = emit_active_speaker_program_config(
                preset,
                role_channels=dict(role_channels),
                playback_device=playback_device,
                protection_sections_by_role=protection_sections_by_role,
                capture_device=devices.capture_device,
                capture_format=devices.capture_format,
                playback_format=devices.playback_format,
                chunksize=devices.chunksize,
                target_level=devices.target_level,
                queuelimit=devices.queuelimit,
                enable_rate_adjust=devices.enable_rate_adjust,
            )
            # Hoisted so the observation below rides the SAME controller the
            # graph load went through, not a second one asking separately.
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
            if observing:
                # OUTSIDE the signalling wrapper, so the phase-ladder anchor
                # stays adjacent to the WAV handoff. INSIDE ``play_program``,
                # because that is the only point at which the routing graph is
                # loaded: read one step earlier and ``get_active_config_raw``
                # still answers the applied graph — the exact misreading this
                # block exists to prevent.
                pre_provenance_play_wav = seams["play_wav"]

                async def _play_wav_observed() -> Any:
                    await _observe(lambda: cam, GRAPH_KIND_PROGRAM_ROUTING)
                    return await pre_provenance_play_wav()

                seams["play_wav"] = _play_wav_observed
            await play_program(
                program,
                program_graph_yaml=program_yaml,
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
# what the host hands the capture provider (S1a/S1c)
# --------------------------------------------------------------------------- #
#
# The plan runners themselves are the providers'
# (jasper.web.correction_crossover_v2_relay, re-published above, and
# jasper.web.correction_crossover_v2_wired). These stay HERE because they are
# host policy a provider merely drives: the volume lifecycle it is handed, the
# group-close seam, and the eager-fit starter it calls back into.


@dataclass(frozen=True)
class V2VolumeHooks:
    """The session-volume lifecycle the runner drives (§5.5)."""

    open: Callable[[], Any]      # async
    close: Callable[[], Any]     # async
    abandon: Callable[[], Any]   # async


def drive_group_close(conductor: Any, *, evidence: Mapping[str, Any] | None) -> None:
    """The household's "all spots measured — Continue" group close (D1).

    **The group-close seam at the host boundary, one owner for every
    provider** (#2662 W2b): the relay runner drives it on the phone's
    authenticated completion event, the wired runner on its local completion
    signal, and both must run the identical sequence — persist FIRST (the
    combine + fit are the slowest thing in the session and the wizard renders
    from durable state, so a confirmation whose fit never reached disk would
    leave the review screen with nothing to review), then the conductor's
    confirm (the only thing that fits a correction in stage 1), then persist
    the fitted result. A refusal (the PR-L4 accountability veto raises
    ``CaptureBeginRefused``) propagates to the calling runner, which ends the
    session exactly as an admission refusal does.

    What this deliberately does NOT do is apply — the review interlude is the
    apply decision point (2026-07-28 ruling); the full history lives on the
    relay provider's ``complete_capture_set`` docstring, whose body now
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
    sound_design_revision: int
    # Per-role declared EFFECTIVE sensitivities (naked datasheet figure with
    # any declared in-line pad folded in — #1665) from the design draft's
    # declaration (the one owner of that fact — W6.5). Threaded into every cap
    # resolution AND the play-time readmission so the composed levels and the
    # admission gate can never disagree about a derived HF ceiling.
    declared_sensitivities: dict[str, float] = field(default_factory=dict)
    # Per-role declared driver technology class (#1665 component entry), fed
    # to the conductor's Layer-1a linearization fit
    # (linearization_envelope.compose_envelope's class_prior_limit term —
    # see crossover_v2_flow.CrossoverV2Session's own driver_class_by_role
    # ctor param, landed by #1668 PR-C ahead of this resolver populating it).
    # A role absent here fits under the conservative "unknown" class default.
    driver_class_by_role: dict[str, str] = field(default_factory=dict)
    # Per-role declared effective radiating diameter in mm (#1665 component
    # entry), the ka/beaming prior (#1675 owner ruling), which is DISCLOSURE
    # and never a bound. Collected since #1665; the value reaches the conductor
    # by the SAME draft path ``driver_class_by_role`` takes, which is why wiring
    # it needs no schema or allowlist change. A role absent here simply gets no
    # beaming prior, disclosed as such rather than assuming a diameter.
    radiating_diameter_mm_by_role: dict[str, float] = field(default_factory=dict)
    # Per-role declared ``crossover_search_band_hz`` — the range each driver
    # may be crossed over IN, intersected across the participating roles by
    # ``crossover_v2.fc_sweep.resolve_fc_search_band``. Like the diameter above
    # it has been a REQUIRED declaration since the safety profile shipped
    # (``driver_safety._target_issues`` refuses a target without one); without
    # it a corner below the tweeter's own declaration would read as admissible.
    # A role maps to ``None`` when its declaration is absent or malformed, which
    # the resolver turns into "no admissible corner" with that role named —
    # never "anything goes".
    crossover_search_band_hz_by_role: dict[str, tuple[float, float] | None] = field(
        default_factory=dict
    )
    # Flat-linearization plan PR-4: the tweeter's confirmed
    # ``measurement_band_hz`` — the contract-derived echo/null analysis band
    # replacing DEFAULT_ECHO_BAND_HZ's flat constant at the cloud-group
    # pipeline's call site (crossover_v2_flow.CrossoverV2Session's own
    # tweeter_measurement_band_hz ctor param). ``None`` degrades to that
    # module default, never a refused session — a declared-metadata gap here
    # is not a reason to block a measurement the household is entitled to run.
    tweeter_measurement_band_hz: tuple[float, float] | None = None


def ensure_crossover_preview_ready(*, durable: bool = False) -> dict[str, Any]:
    """Ensure a ready crossover preview exists before a v2 session reads one.

    ``durable`` only matters on the regenerate branch (a reused preview
    writes nothing) and passes straight through to
    :func:`~jasper.active_speaker.web_commissioning.regenerate_crossover_preview_from_current_draft`.
    The default keeps the session-open/verify-re-arm callers cheap;
    :func:`handle_v2_apply`'s crossover-accept branch opts in.

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
    reach ``ready_for_protected_staging`` (a safety profile whose declared
    values are ``incomplete`` or whose outputs moved under it, a blocked design
    draft, etc.), this raises a named :class:`CrossoverV2Refused`
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
        preview = regenerate_crossover_preview_from_current_draft(durable=durable)
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


def _resolve_radiating_diameter_by_role(draft: Mapping[str, Any]) -> dict[str, float]:
    """Per-role declared effective radiating diameter, mm (#1665 / #1675).

    The same shape and the same fail-soft contract as
    :func:`_resolve_driver_class_by_role` — role-keyed, a role with disagreeing
    declarations drops entirely, anything malformed is skipped rather than
    raised. A diameter is a beaming PRIOR, so a bad one must cost that one
    role its prior, never the session.

    Deliberately no default: absent means "not declared", and the receipt says
    so. Substituting a nominal diameter would manufacture a beaming ceiling out
    of nothing, and #1675 is explicit that this is geometry
    guidance derived from a declared dimension.
    """
    manual = draft.get("manual_settings") if isinstance(draft, Mapping) else None
    if not isinstance(manual, Mapping):
        return {}
    drivers = manual.get("drivers")
    out: dict[str, float] = {}
    conflicted: set[str] = set()
    for driver in drivers if isinstance(drivers, list) else []:
        if not isinstance(driver, Mapping):
            continue
        role = str(driver.get("role") or "")
        value = driver.get("radiating_diameter_mm")
        if not role or isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        millimetres = float(value)
        if not math.isfinite(millimetres) or millimetres <= 0.0:
            continue
        if role in out and out[role] != millimetres:
            conflicted.add(role)
            continue
        out[role] = millimetres
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
        resolve_driver_crossover_search_band_hz,
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
    # #1675: the ka/beaming prior, off the SAME draft path, as disclosure.
    radiating_diameter_mm_by_role = _resolve_radiating_diameter_by_role(draft)
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
    # R17: every PARTICIPATING role's declared crossover search band, keyed by
    # role. Both roles are always present as KEYS — a missing declaration is a
    # ``None`` VALUE, not an absent key, because the intersection rule needs to
    # know the role participated in order to fail closed on it.
    crossover_search_band_hz_by_role = {
        role: resolve_driver_crossover_search_band_hz(
            safety_profile, role_targets[role],
        )
        for role in role_targets
    }
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
        sound_design_revision=int(draft.get("revision", 0)),
        declared_sensitivities=declared_sensitivities,
        driver_class_by_role=driver_class_by_role,
        radiating_diameter_mm_by_role=radiating_diameter_mm_by_role,
        crossover_search_band_hz_by_role=crossover_search_band_hz_by_role,
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


# --------------------------------------------------------------------------- #
# the remote tier's position gate
# --------------------------------------------------------------------------- #

#: How long ONE position hold waits for its external driver before the session
#: refuses rather than holding forever.
#:
#: A hold is UNBOUNDED as far as the transport is concerned — the capture page
#: re-posts the same begin every 1.5 s and each re-post rearms the runner's
#: inactivity deadline (``capture_relay.session.run_capture_plan``) — so nothing
#: below this module would ever end a hold whose driver died. That is the right
#: default for the household-facing apply hold it was built for, where a person
#: is standing there; it is the wrong one here, where the other end is a program
#: that can crash silently and leave the speaker holding its measurement volume,
#: its paused voice, and the relay slot indefinitely.
#:
#: Ten minutes because the thing being waited on is a machine move: seconds to
#: swing an arm, plus whatever settle the driver takes, plus room for a driver
#: that is retrying its own transport. It is an order of magnitude past the
#: move and an order of magnitude short of "nobody is coming".
#:
#: **This is a PER-HOLD bound, and it is not the operative total.** The session's
#: own wall-clock ceiling
#: (:func:`~jasper.active_speaker.crossover_v2_flow.session_wall_clock_ceiling_s`,
#: derived per plan — 1800 s for remote's stage 1 and 2040 s for its stage 2 at
#: the shipped shape) covers the WHOLE walk, so a run spending anywhere near
#: this budget on several holds ends on that ceiling long before any individual
#: hold expires: stage 1's ceiling is exactly 3 holds' worth of a 3-capture
#: stage, so a remote stage 1 that spent a FULL hold at every position would
#: land precisely on its ceiling. (Before the 2026-08-18 lateral pause the same
#: sentence read 2520 s, 4.2 holds' worth, and the FIFTH full hold of a
#: nine-capture walk exceeding it — the pause dropped both the ceiling and the
#: captures, and left more hold per position, not less.)
#: A driver that stalls once is caught
#: here by name; a driver that is merely slow at every position is caught by the
#: ceiling, and since issue #2506 that death has its OWN name too —
#: :data:`SESSION_CEILING_EXPIRED_CODE`, raised by :meth:`PositionGate.gate`
#: once :func:`enforce_session_volume_ceiling_if_stale` reports the walk
#: outlived its ceiling. Whether the per-hold budget should instead be DERIVED
#: from the ceiling and the capture count is still open; the two bounds are
#: named separately on purpose, because they describe different drivers.
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

#: The endpoint an external driver POSTs to report the microphone in place.
POSITION_READY_ENDPOINT = "/correction/crossover/v2/position-ready"


class PositionGate:
    """Holds each remote capture's begin until a driver reports the angle reached.

    **Why a gate exists at all.** The hand-walked tiers make every prompted pose
    a TAP: the person who just moved the microphone is the one who says it has
    arrived, so the tone can never play into an arm still in flight. A remote
    session has no hand, so its entries auto-begin
    (:data:`~jasper.active_speaker.crossover_v2_flow.AUTO_ADVANCE_COUNTDOWN`) —
    and this is what replaces the promise the tap was making. The driver moves
    the positioner, waits its own settle, and POSTs; only then is the begin
    admitted.

    **The mechanism is the shipped soft-hold, not a new one.**
    :class:`~jasper.capture_relay.session.CaptureBeginDeferred` is the
    purpose-built non-terminal deferral: the Pi answers ``capture_deferred``,
    the phone parks on a wait screen with no affordance and re-posts the
    IDENTICAL begin every 1.5 s, the attempt budget is not spent, and the
    session does not end. Nothing on the capture page changes to support this —
    which matters, because that page is a separately deployed artifact and a
    release-order coupling is exactly what this tier must not introduce.

    **Every begin is gated, including the 0° ones.** CHECK, MEASURE, the entry
    baseline, and stage 2's anchor are design-axis captures; a driver has to put
    the arm back there as deliberately as it moved away, and a gate that assumed
    "probably still on axis" would measure whatever the last pose left behind.
    Gating is per ``(index, attempt)``, so a retake re-gates — the same uniform
    rule rather than a special case that has to be reasoned about.

    **Two bounds end a hold, and they name different drivers** (issue #2506).
    :data:`REMOTE_POSITION_HOLD_BUDGET_S` is the per-hold one: a driver that
    STOPPED answering. The session's own wall-clock ceiling is the cumulative
    one: a driver answering every position, just too slowly to finish the walk.
    The second is not measured here — this class keeps no session clock, because
    the ceiling already has an owner (``SessionVolumePlan``'s ``opened_at`` plus
    the stage's stamped ceiling) and a second clock for one bound is a second
    definition of it. :meth:`note_session_ceiling_expired` is how the owner's
    finding reaches the hold that is blocking on it.

    Thread-safe: :meth:`gate` runs on the relay worker thread,
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

    # -- the relay worker's side ------------------------------------------- #

    def gate(self, index: int, attempt: int, entry: Any) -> None:
        """Admit this begin, or raise to hold/refuse it.

        Returns cleanly once the driver has released this ``(index, attempt)``;
        raises :class:`CaptureBeginDeferred` while it has not, and
        :class:`CaptureBeginRefused` when the hold outlived
        :data:`REMOTE_POSITION_HOLD_BUDGET_S`, when the whole walk outlived the
        session's wall-clock ceiling, or when the entry carries no target.
        """
        from jasper.active_speaker.crossover_v2_flow import (
            POSITION_DEG_KEY,
            POSITION_ROLE_KEY,
        )
        from jasper.capture_relay.session import (
            CaptureBeginDeferred,
            CaptureBeginRefused,
        )

        screen = getattr(entry, "screen", None) or {}
        raw_degrees = screen.get(POSITION_DEG_KEY)
        if raw_degrees is None:
            # Fail loud rather than measure an unknown position. A remote plan
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
            # Checked SECOND, so a stalled driver keeps the specific diagnosis
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
                    "role": role,
                    "action": {
                        "id": "crossover_v2_position_ready",
                        "label": f"Microphone is at {target:+d}°",
                        "endpoint": POSITION_READY_ENDPOINT,
                        "body": {"index": int(index), "degrees": target},
                    },
                }
                log_event(
                    logger,
                    "correction.crossover_v2_position_pending",
                    index=int(index),
                    attempt=int(attempt),
                    degrees=target,
                    role=role,
                )
        raise CaptureBeginDeferred(
            POSITION_HOLD_CODE,
            f"Waiting for the microphone to reach {target:+d}°.",
        )

    # -- the driver's side -------------------------------------------------- #

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

    def release(self, index: int | None = None) -> dict[str, Any]:
        """Report the microphone in place for the pending capture.

        ``index`` is checked against what is actually pending rather than
        ignored: a driver that retries its POST after the capture already began
        must NOT release the NEXT position, which is the one hazard an
        untargeted latch would introduce. A retry that still matches the pending
        index is idempotent.

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
    open: Callable[..., Any]
    run_and_consume: Callable[[Any, Any], Any]
    request_stop: Callable[[], None]
    #: The remote tier's position gate, or ``None`` for a hand-walked session.
    #: ``correction_setup`` reads :meth:`PositionGate.pending` into the relay
    #: block the envelope renders, and routes the driver's POST to
    #: :meth:`PositionGate.release`.
    position_gate: PositionGate | None = None
    #: Which capture source this session opened on (#2662):
    #: ``capture_source.SOURCE_RELAY`` (the default — the hosting mints a
    #: relay link) or ``SOURCE_WIRED`` (local capture — no relay client, no
    #: link, and the hosting must not require a configured relay).
    capture_source: str = SOURCE_RELAY
    #: The wired session's completion signal (work order D1) — the local
    #: stand-in for the phone's authenticated complete-capture-set event.
    #: ``None`` on a relay session, whose signal rides the relay protocol.
    #: The W3 wizard surface is what will POST this; it exists now because
    #: the held-set walk semantics need a signal source to be complete.
    request_complete: Callable[[], None] | None = None


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


def _rollback_anchor_available() -> bool:
    """Is there a valid anchor for :func:`handle_v2_restore` to restore FROM?

    #2291's ``rollback_available``, anchor half. The seam half — is the
    conductor's ``rollback`` bound at all — is the conductor's own to check,
    and it ANDs the two: the parameter's name asks "can the host actually
    restore", and either half alone answers that wrongly. A stage-2 conductor
    on a speaker whose durable state carries no ``pre_apply_profile`` has the
    seam bound and can restore nothing, so a round trusting seam presence
    alone would issue a ``restore`` instruction that Undo then refuses.

    Reads :func:`rollback_anchor_refusal` — the same predicate
    :func:`handle_v2_restore` refuses on — rather than re-deriving the three
    preconditions, so the answer and the action cannot drift.

    **Fails closed.** Any unexpected error reading the durable state or the
    output topology means "not available": a round that cannot confirm an
    anchor must not be told it has one, because the cost of the wrong answer
    is a restore instruction nothing can carry out, which then surfaces as
    ``recovery_required`` anyway — one round later and less honestly.
    """
    try:
        return rollback_anchor_refusal(load_v2_state()) is None
    except (OSError, RuntimeError, TypeError, ValueError, KeyError):
        log_event(
            logger,
            "correction.crossover_v2_rollback_available_failed",
            level=logging.WARNING,
            exc_info=True,
        )
        return False


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


def bind_v2_stage_seams(
    opening: StageOpening,
    *,
    play: Any,
    evidence_store: Any,
    relay_session_id: str,
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
    ``publish_cloud``/``retain_position`` do nothing for a single-entry recovery
    re-verify — but Full's stage 2 IS a post-apply position group whose combined
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
        stage=capabilities.stage, session_id=relay_session_id,
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
            session_id=relay_session_id, missing=",".join(missing),
        )
    return V2FlowSeams(
        play=play,
        analyze=bind_production_analyze(meta=refs, provenance=provenance),
        publish_check=publish_check,
        publish_candidate=publish_candidate,
        apply_complete=_applied_gate,
        apply_failed=_apply_failure_gate,
        retain_position=bind_position_retention(
            evidence_store, relay_session_id, refs
        ),
        publish_cloud=bind_cloud_publisher(evidence_store, relay_session_id, refs),
        # #2291's round receipt. Bound on both stages rather than gated on a
        # capability: only the stage that GRADES a round ever calls it, and a
        # binding that exists everywhere cannot be the reason a receipt went
        # unwritten on the stage that needed it.
        publish_round_receipt=bind_round_receipt(
            evidence_store, relay_session_id, refs
        ),
        publish_findings=(
            bind_findings_publisher(evidence_store, relay_session_id, refs)
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
        rollback_available=_rollback_anchor_available,
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


def _resolve_prepare_capture_source() -> tuple[str, Any]:
    """Which capture source this prepare opens on (#2662 W2b).

    Resolved ONCE per prepare and threaded into ``_open`` — the mint, the
    runner build, and the returned ``capture_source`` must describe one
    decision, not three reads of a probe that can change between them. The
    wired provider is imported lazily so a relay session keeps today's
    import surface. A wired resolution error (an explicit override with no
    mic present, an unrecognized override value) refuses at the tap, before
    any evidence store or durable state is touched.

    **The relay's own precondition is checked HERE too** (gate fix round
    S3): a session that resolved to the relay needs a configured relay
    origin, and learning that at the dispatch — after the preparer had
    already opened a fresh evidence bundle (abandoning the prior one) and
    hydrated durable state — was a refusal that cost side effects. Every
    source's "can this session open at all?" question is answered at this
    one point, before anything is written. The dispatch's later
    ``_require_relay_base()`` read is then a plain re-read of a value this
    gate just proved present.
    """
    from jasper.audio_measurement.wired_capture import WiredCaptureError
    from jasper.web import correction_crossover_v2_wired as wired

    try:
        source, device = wired.resolve_v2_capture_source()
    except WiredCaptureError as exc:
        raise CrossoverV2Refused(str(exc)) from exc
    if source == SOURCE_RELAY:
        # The one owner of the relay-configured question and its message
        # (correction_setup._require_relay_base), reached lazily exactly as
        # the calibration resolver reaches its correction_setup owner.
        from jasper.web.correction_setup import _require_relay_base

        try:
            _require_relay_base()
        except ValueError as exc:
            raise CrossoverV2Refused(str(exc)) from exc
    return source, device


def _mint_source_session(
    source: str,
    wired_device: Any,
    client: Any,
    spec: Any,
    *,
    base: str,
    capture_origin: str,
    return_url: str,
    plan_shape: Any,
    ceiling_s: float,
) -> Any:
    """Mint one session on the selected source (#2662 W2b).

    Both mints answer the same shape (``pi_session`` + ``tap_link``); only
    the relay's talks to a network. The TTL policy is relay-private — a wired
    session has no link to expire.

    **No Pi-minted per-capture result wait.** The wait #2706 put on the spec was
    the Fc sweep's compute ceiling plus its measured overhead; with no sweep to
    bound, the Pi publishes nothing and ``resultWaitMs`` falls back to the
    page's own 90 s floor — the wall every banked round was measured against,
    and 8.7 s clear of the slowest observed round (81.28 s), which did strictly
    more work than any round runs now.
    """
    if source == SOURCE_WIRED:
        from jasper.web import correction_crossover_v2_wired as wired

        return wired.open_wired_capture(spec, device=wired_device)
    from jasper.capture_relay import correction_adapter

    return correction_adapter.open_capture(
        client,
        spec,
        relay_base=base,
        capture_origin=capture_origin,
        return_url=return_url,
        ttl_s=relay_link_ttl_s(plan_shape, ceiling_s),
    )


def _build_source_run(
    source: str,
    conductor: Any,
    *,
    volume: "V2VolumeHooks",
    stop_event: threading.Event,
    stop_lock: Any,
    position_gate: "PositionGate | None",
    evidence_refs: dict[str, Any],
    playback_started: Any,
    wired_device: Any,
    ceiling_s: float,
    complete_event: threading.Event,
) -> Callable[[Any, Any], Any]:
    """One provider runner per source, driving the same conductor hooks.

    The relay runner takes the phone-phase signal (its progress ladder); the
    wired runner takes the device, the session ceiling (its confirm-wait
    bound) and the local completion signal. Neither takes the other's
    extras — the seam's rule that a source's choreography stays private.
    """
    if source == SOURCE_WIRED:
        from jasper.web import correction_crossover_v2_wired as wired

        return wired.build_v2_wired_run_and_consume(
            conductor,
            volume=volume,
            stop_event=stop_event,
            stop_lock=stop_lock,
            device=wired_device,
            ceiling_s=ceiling_s,
            complete_event=complete_event,
            position_gate=position_gate,
            evidence_refs=evidence_refs,
        )
    return build_v2_run_and_consume(
        conductor,
        volume=volume,
        stop_event=stop_event,
        stop_lock=stop_lock,
        position_gate=position_gate,
        evidence_refs=evidence_refs,
        playback_started=playback_started,
    )


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
    hydrated through :meth:`CrossoverV2Session.hydrate` with the NEW relay
    session id — a prior session's CHECK/MEASURE evidence is invalidated per
    §5.6 (and logged); the fresh session starts at CHECK.

    ``raw["tier"]`` selects the commission instrument (flow-simplification
    §3): the wizard posts the household's explicit choice, and an unrecognised
    one is refused before any relay registration rather than silently measured
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
    line. :func:`prepare_v2_verify` already reads its tier from durable state
    (``_verify_plan_shape``); this reads the same fact from the same place when
    the body does not name one, which makes the two siblings agree rather than
    giving the retry a second rule. An explicit body tier still wins: the tier
    chooser is how a household changes instrument.
    """
    from jasper.active_speaker.branch_chain import confirmed_protection_sections
    from jasper.active_speaker.branch_chain import beaming_onset_hz
    from jasper.active_speaker.crossover_v2.alignment_prescription import (
        ALIGNMENT_PRESCRIPTION_KEY,
        AlignmentPrescriptionRefused,
        read_alignment_prescription,
    )
    from jasper.active_speaker.crossover_v2.fc_sweep import (
        resolve_fc_search_band,
    )
    from jasper.active_speaker.crossover_v2.topology_prescription import (
        TOPOLOGY_PRESCRIPTION_KEY,
        TopologyPrescriptionRefused,
        apply_topology_pin,
        read_topology_prescription,
    )
    from jasper.active_speaker.excitation_safety_plan import (
        resolve_driver_protection_slope_db_per_octave,
    )
    from jasper.active_speaker.crossover_v2_flow import (
        LATERAL_CONSUMER_FC_SELECTOR,
        STAGE1_INCLUDES_CLOUD_MEASURE,
        STAGE1_INCLUDES_ENTRY_BASELINE,
        CrossoverV2Session,
        CrossoverV2FlowError,
        V2ConductorSnapshot,
        alignment_delay_search_bounds_us,
        attempt_history_from_state,
        build_v2_cloud_index_phase_map,
        build_v2_session_spec,
        resolve_plan_shape,
        session_wall_clock_ceiling_s,
    )
    from jasper.active_speaker.crossover_v2.coordinator import (
        series_position_from_state,
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
    # evidence store, relay registration, or capture — an operator walking a
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
    # and a "measure again" that silently re-ran an arm would put that arm's
    # name on a round nobody asked for.
    #
    # The TOPOLOGY pin is read FIRST, and the order is load-bearing rather than
    # alphabetical: it decides the corner this round runs at, and the delay
    # gate below is a half-period AT that corner. Read the other way round, a
    # 4000 Hz round's delay would be bounded by the incumbent 1648.7 Hz lobe
    # (303 us) instead of its own (125 us) — a gate that passes arms the round
    # it is gating cannot support.
    #
    # Every bound it applies is a DECLARATION, asked of the module that owns
    # it: the two role bands a corner is admissible within (read positionally,
    # in the order this context builds them — woofer first), the intersected
    # declared search band, and the upper driver's declared protective
    # high-pass slope. The ka/beaming onset rides along as DISCLOSURE only
    # (#1675 makes it guidance, and no admissibility bound anywhere reads it).
    #
    # The declarations are gathered ONLY for a request that carries a pin, and
    # that branch is deliberate rather than an optimisation. Four of this
    # context's fields are read nowhere else on this path; deriving them
    # unconditionally would make an ORDINARY round's session-open depend on
    # declarations it is not using — including a positional read of
    # ``roles_bands`` — so a context shaped for a decision this round does not
    # take could fail a round that never asked for one.
    raw_topology = (raw or {}).get(TOPOLOGY_PRESCRIPTION_KEY)
    topology_prescription = None
    if raw_topology is not None:
        # Woofer first, tweeter second — the order this context builds them in.
        woofer_band, tweeter_band = (
            context.roles_bands[0].band, context.roles_bands[1].band,
        )
        woofer_diameter_mm = context.radiating_diameter_mm_by_role.get("woofer")
        try:
            topology_prescription = read_topology_prescription(
                raw_topology,
                declared_floor_hz=tweeter_band.lower_hz,
                lower_driver_ceiling_hz=woofer_band.upper_hz,
                search_band_hz=resolve_fc_search_band(
                    context.crossover_search_band_hz_by_role
                ).band_hz,
                minimum_slope_db_per_octave=(
                    resolve_driver_protection_slope_db_per_octave(
                        context.safety_profile, context.role_targets["tweeter"],
                    )
                ),
                beaming_ceiling_hz=(
                    None if woofer_diameter_mm is None
                    else beaming_onset_hz(float(woofer_diameter_mm))
                ),
            )
        except TopologyPrescriptionRefused as exc:
            raise CrossoverV2Refused(
                "the topology prescription was refused "
                f"({exc.reason}): {exc.detail}"
            ) from exc
    # What this round actually runs at — one decision, made by the module that
    # owns the pin and taken identically by the grading stage below.
    session_preset, session_fc_hz = apply_topology_pin(
        topology_prescription, preset=context.preset, fc_hz=context.fc_hz,
    )
    try:
        alignment_prescription = read_alignment_prescription(
            (raw or {}).get(ALIGNMENT_PRESCRIPTION_KEY),
            # THIS round's corner, never ``context.fc_hz`` — see the pin above.
            fc_hz=session_fc_hz,
            # The preset's own declared window, from its single owner. It is
            # the one bound here that does not rest on a number the request
            # supplied — and it already existed as the Fix-3 plausibility
            # screen, ten minutes downstream, wearing household copy that asks
            # the user to move the microphone. Asking it HERE is what stops a
            # prescribed arm being blamed on a mic.
            declared_bounds_us=alignment_delay_search_bounds_us(context.preset),
        )
    except AlignmentPrescriptionRefused as exc:
        raise CrossoverV2Refused(
            f"the alignment prescription was refused ({exc.reason}): {exc.detail}"
        ) from exc
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
    )
    lateral_prompts: tuple[Any, ...] | None = None
    lateral_consumer = LATERAL_CONSUMER_FC_SELECTOR
    if staged_walk is not None:
        lateral_prompts, lateral_consumer = staged_walk
        include_lateral = True
        stage1_index_phase = build_v2_cloud_index_phase_map(
            plan_shape=plan_shape,
            include_cloud_measure=include_cloud_measure,
            include_lateral=include_lateral,
            include_entry_baseline=include_entry_baseline,
            lateral_prompts=lateral_prompts,
        )
    # #2662 W2b: which capture source answers this session's asks. After the
    # speaker-level gates (they are prior questions), before any state is
    # opened (an explicit-override refusal must cost nothing).
    capture_source, wired_device = _resolve_prepare_capture_source()
    evidence_store, _bundle_id = open_v2_evidence_store(context.topology)
    acknowledgement_binding = secrets.token_urlsafe(24)
    stop_event = threading.Event()
    stop_lock = threading.Lock()
    complete_event = threading.Event()
    # The remote tier's position gate — built only for an externally
    # positioned shape, so a hand-walked session carries no gate at all and
    # every begin reaches the conductor exactly as it always has.
    position_gate = PositionGate() if plan_shape.externally_positioned else None
    if position_gate is not None:
        log_event(
            logger,
            "correction.crossover_v2_remote_session_open",
            stage=1,
            tier=plan_shape.tier,
            # The captures this session will ACTUALLY take, off that plan. It
            # logged ``plan_shape.measure_capture_target`` — the cloud-INCLUSIVE
            # shape target, 10 where the shipped stage 1 walks 3. The reader is
            # an external positioner with no screen to check it against, and
            # OVER-reporting is the direction that STALLS a walk: a driver sized
            # at 10 waits for seven captures that are never coming, until the
            # session ceiling expires (#2506). On 2026-08-19 a wired-night
            # driver met exactly that contradiction — its own
            # ``--complete-after 3`` against this line's 10 — and settled it by
            # re-deriving the count by hand on the live box.
            captures=len(stage1_index_phase),
        )

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
            # THIS round's corner, the same one the conductor above was opened
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
        ).with_return_url(return_url)
        # This stage's own wall-clock budget, read ONCE off the plan it just
        # emitted: the relay link is minted against it below and the walked-away
        # volume ceiling is armed from it further down, and those two must
        # describe the same session (issue #2509).
        ceiling_s = session_wall_clock_ceiling_s(spec.capture_plan)
        rc = _mint_source_session(
            capture_source,
            wired_device,
            client,
            spec,
            base=base,
            capture_origin=capture_origin,
            return_url=return_url,
            plan_shape=plan_shape,
            ceiling_s=ceiling_s,
        )
        # The conductor + publishers bind to the MINTED provider session id
        # (the seam's identity rule — the wired id rides the same key).
        relay_session_id = rc.pi_session.session_id
        publish_check, publish_candidate, refs = bind_evidence_publishers(
            evidence_store, relay_session_id
        )
        # A cloud session legitimately outlasts the 3-entry flow's walked-away
        # ceiling; scale it from the plan this session actually emitted (never
        # from the constants, so a caller-configured cloud is covered too) and
        # arm it BEFORE the volume opens.
        session_volume_plan().set_wall_clock_ceiling_s(ceiling_s)
        # One signal per session, shared by the play seam (which fires it) and
        # the runner (which installs the armed capture's phase ladder on it).
        playback_started = PlaybackStartSignal()
        # Same shape, same reason: written by the play seam, read by analyze.
        capture_provenance = CaptureProvenanceRecorder()
        play = bind_production_play(
            run_async=run_async,
            camilla_factory=camilla_factory,
            evidence_store=evidence_store,
            relay_session_id=relay_session_id,
            topology=context.topology,
            preset=context.preset,
            role_channels=context.role_channels,
            playback_device=context.playback_device,
            safety_profile=context.safety_profile,
            role_targets=context.role_targets,
            session_volume_db=context.session_volume_db,
            protection_sections_by_role=protection_sections,
            declared_sensitivities=context.declared_sensitivities,
            on_playback_started=playback_started.fire,
            provenance=capture_provenance,
        )
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
            session_id=relay_session_id,
            # The PINNED topology when this request carried one, else the
            # context's own — resolved once, above, so the preset and the
            # corner can never name two different crossovers.
            source_preset=session_preset,
            roles_bands=context.roles_bands,
            fc_hz=session_fc_hz,
            driver_caps_dbfs=context.driver_caps_dbfs,
            session_volume_db=context.session_volume_db,
            seams=bind_v2_stage_seams(
                opening,
                play=play,
                evidence_store=evidence_store,
                relay_session_id=relay_session_id,
                refs=refs,
                publish_check=publish_check,
                publish_candidate=publish_candidate,
                run_async=run_async,
                camilla_factory=camilla_factory,
                provenance=capture_provenance,
            ),
            tier=plan_shape.tier,
            index_phase_map=opening.plan.index_phase_map,
            post_apply_verifies=opening.plan.post_apply_verifies,
            driver_spacing_m=context.driver_spacing_m,
            driver_class_by_role=context.driver_class_by_role,
            radiating_diameter_mm_by_role=context.radiating_diameter_mm_by_role,
            # R17. Threaded on the MEASURING session only — the verify re-arm
            # below runs no lateral walk (it maps VERIFY alone), so the Fc
            # selector cannot fire there and an argument passed to it would be
            # dead rather than symmetric.
            crossover_search_band_hz_by_role=context.crossover_search_band_hz_by_role,
            # #2732 P2. From the SAME take the map and the spec above read.
            lateral_consumer=lateral_consumer,
            lateral_prompts=lateral_prompts,
            measurement_protection_sections_by_role=protection_sections,
            sound_design_revision=context.sound_design_revision,
            tweeter_measurement_band_hz=context.tweeter_measurement_band_hz,
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
        persist_conductor_state(conductor, failure_code=None, evidence=refs)
        holder["run"] = _build_source_run(
            capture_source,
            conductor,
            volume=_volume_hooks(camilla_factory, context),
            stop_event=stop_event,
            stop_lock=stop_lock,
            position_gate=position_gate,
            evidence_refs=refs,
            playback_started=playback_started,
            wired_device=wired_device,
            ceiling_s=ceiling_s,
            complete_event=complete_event,
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
        position_gate=position_gate,
        capture_source=capture_source,
        request_complete=(
            complete_event.set if capture_source == SOURCE_WIRED else None
        ),
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
    inference has a case where it silently downgrades Full's multi-position
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
    import numpy as np

    from jasper.active_speaker.crossover_v2_flow import (
        PHASE_CHECK,
        PHASE_MEASURE,
        CrossoverV2Session,
        attempt_history_from_state,
        build_v2_verify_index_phase_map,
        build_v2_verify_session_spec,
        session_wall_clock_ceiling_s,
    )
    from jasper.active_speaker.crossover_v2.coordinator import (
        series_position_from_state,
    )
    from jasper.active_speaker.crossover_v2.topology_prescription import (
        apply_topology_pin,
    )

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
    # #2662 W2b: the same one-decision source resolution stage 1 makes, in
    # the same position — after the speaker-level gates, BEFORE the evidence
    # bundle opens, so a refused verify-start (relay-less Pi, unplugged mic
    # under an explicit wired override) costs no side effects (gate S3, both
    # preparers).
    capture_source, wired_device = _resolve_prepare_capture_source()
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
    acknowledgement_binding = secrets.token_urlsafe(24)
    stop_event = threading.Event()
    stop_lock = threading.Lock()
    complete_event = threading.Event()
    # Same rule as stage 1's, with one extra case: ``_verify_plan_shape``
    # returns ``None`` for the recovery re-arm, which has no tier and therefore
    # no gate — it is the one-sweep session a household starts by hand.
    position_gate = (
        PositionGate()
        if plan_shape is not None and plan_shape.externally_positioned
        else None
    )
    if position_gate is not None and plan_shape is not None:
        log_event(
            logger,
            "correction.crossover_v2_remote_session_open",
            stage=2,
            tier=plan_shape.tier,
            captures=plan_shape.verify_capture_target,
        )
    holder: dict[str, Any] = {}

    def _open(client: Any, base: str, capture_origin: str, return_url: str) -> Any:
        spec = build_v2_verify_session_spec(
            # The corner this round was measured and applied at — stage 1's
            # ``session_fc_hz`` rehydrated. Same one-corner-per-round rule as
            # the stage-1 spec above.
            verify_fc_hz,
            acknowledgement_binding=acknowledgement_binding,
            plan_shape=plan_shape,
            default_setup_calibration=default_setup_calibration_for_v2(),
        ).with_return_url(return_url)
        # Stage 2's own budget, read once off its own plan — the link below and
        # the ceiling further down size from the same number, exactly as stage 1
        # does (issue #2509).
        ceiling_s = session_wall_clock_ceiling_s(spec.capture_plan)
        rc = _mint_source_session(
            capture_source,
            wired_device,
            client,
            spec,
            base=base,
            capture_origin=capture_origin,
            return_url=return_url,
            plan_shape=plan_shape,
            ceiling_s=ceiling_s,
        )
        relay_session_id = rc.pi_session.session_id
        # Re-arm the walked-away ceiling from THIS plan (1 entry ⇒ the plain
        # 1800 s baseline; Full's stage-2 walk scales it like any other plan).
        # The volume plan is process-global, so a preceding cloud session would
        # otherwise leave its own longer ceiling in force for a measurement
        # that cannot possibly need it.
        session_volume_plan().set_wall_clock_ceiling_s(ceiling_s)
        _publish_check, publish_candidate, refs = bind_evidence_publishers(
            evidence_store, relay_session_id
        )
        # One signal per session, shared by the play seam (which fires it) and
        # the runner (which installs the armed capture's phase ladder on it).
        playback_started = PlaybackStartSignal()
        # Same shape, same reason: written by the play seam, read by analyze.
        capture_provenance = CaptureProvenanceRecorder()
        play = bind_production_play(
            run_async=run_async,
            camilla_factory=camilla_factory,
            evidence_store=evidence_store,
            relay_session_id=relay_session_id,
            topology=context.topology,
            preset=context.preset,
            role_channels=context.role_channels,
            playback_device=context.playback_device,
            safety_profile=context.safety_profile,
            role_targets=context.role_targets,
            session_volume_db=context.session_volume_db,
            declared_sensitivities=context.declared_sensitivities,
            on_playback_started=playback_started.fire,
            provenance=capture_provenance,
        )
        # This stage's journey (#2291 Phase 4), the same contract stage 1 opens
        # and differing only in its arguments. ``available`` states facts about
        # the rehydration above and nothing more — which slug each fact answers
        # to is the journey's, so the journal's "missing" line cannot drift
        # from what the conductor was really constructed with. No
        # ``verify_capture_target``: this plan really does contain VERIFY, so
        # the phase-derived reading is already the right one.
        opening = open_stage(
            STAGE_VERIFY_CAPABILITIES,
            index_phase_map=build_v2_verify_index_phase_map(plan_shape=plan_shape),
            available=available_stage_priors(
                commanded_delta=commanded_delta is not None,
                predicted_sum=predicted_sum is not None,
                entry_baseline=entry_baseline is not None,
            ),
        )
        conductor = CrossoverV2Session(
            session_id=relay_session_id,
            # The topology the round being graded was MEASURED and APPLIED at
            # — the pin when the durable state carried one, else the context's
            # own. Resolved once, above.
            source_preset=verify_preset,
            roles_bands=context.roles_bands,
            fc_hz=verify_fc_hz,
            driver_caps_dbfs=context.driver_caps_dbfs,
            session_volume_db=context.session_volume_db,
            seams=bind_v2_stage_seams(
                opening,
                play=play,
                evidence_store=evidence_store,
                relay_session_id=relay_session_id,
                refs=refs,
                publish_check=_publish_check,
                publish_candidate=publish_candidate,
                run_async=run_async,
                camilla_factory=camilla_factory,
                provenance=capture_provenance,
            ),
            driver_spacing_m=context.driver_spacing_m,
            driver_class_by_role=context.driver_class_by_role,
            radiating_diameter_mm_by_role=context.radiating_diameter_mm_by_role,
            tweeter_measurement_band_hz=context.tweeter_measurement_band_hz,
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
        # Keep the durable candidate/applied facts; rebind the session id.
        persist_conductor_state(conductor, failure_code=None, evidence=refs)
        holder["run"] = _build_source_run(
            capture_source,
            conductor,
            volume=_volume_hooks(camilla_factory, context),
            stop_event=stop_event,
            stop_lock=stop_lock,
            position_gate=position_gate,
            evidence_refs=refs,
            playback_started=playback_started,
            wired_device=wired_device,
            ceiling_s=ceiling_s,
            complete_event=complete_event,
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
        position_gate=position_gate,
        capture_source=capture_source,
        request_complete=(
            complete_event.set if capture_source == SOURCE_WIRED else None
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
                    # crossover, what it displaced is no longer derivable from
                    # anything live, and a retry still owes Undo that inverse.
                    {"accepted_sound_revision": accepted_revision,
                     "accepted_sound_declaration_change": change_to_record(change)},
                )
            ):
                raise _review_replaced()
        # How to put ``/sound`` back if the household undoes this (#2292).
        # Derived here, where the facts are in scope, and CARRIED to
        # ``observe_apply_success`` rather than persisted here, so it lands in
        # the same state write as ``pre_apply_profile`` — see that function.
        # Rebuilt identically on a retry (whose Sound save already happened and
        # is skipped above) because both attempts read the same ``change``.
        declaration_undo = {
            "sound_revision": accepted_revision, **change_to_record(change)}
        # durable=True: this branch just accepted a measured crossover onto the
        # Sound declaration (apply_measured_crossover_geometry above), so the
        # preview regenerated from it is part of the same crossover-accept
        # seam and gets the same power-loss-durable write.
        preview = _before_dsp(lambda: ensure_crossover_preview_ready(durable=True))
        draft = _before_dsp(lambda: load_design_draft(topology=topology))
    else:
        # An ordinary as-declared apply writes nothing to Sound, so it has
        # nothing to invert — and says so explicitly rather than by omission.
        # ``observe_apply_success`` writes this value, so ``None`` here is what
        # CLEARS a record left by an earlier declaration-changing apply that
        # this one has just superseded.
        declaration_undo = None
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
    # commits and pops that field off the new applied SSOT — this is the
    # ONLY place it survives to stash for the v2 Undo path
    # (``handle_v2_restore``).
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
                    expected, pre_apply_profile=pre_apply_profile,
                    sound_declaration_undo=declaration_undo,
                    expected_post_apply_offset_db=offset_db,
                    # Read from the transaction that just completed, so the
                    # displaced identity is the graph CamillaDSP was actually
                    # playing a moment ago rather than whatever a global record
                    # says later (#2537).
                    round_anchor=round_anchor_record(payload))
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
    socket or a malformed stash still propagates, and
    :func:`~jasper.active_speaker.crossover_v2.coordinator._run_round_restore`
    catches that wider family on the other side of the seam (it has to — a
    conductor with a different binding gets the same protection). Two honest
    halves rather than one dishonest "never raises".

    **Exactly once per binding, and the reason is the copy (#2291).** ONE
    conductor site reaches this closure — the round's adoption path — and the
    guard below is what its history bought. Until the fifth-principle routing
    there were three: the delta probe's own refusal at VERIFY, the same refusal
    when the post-apply cloud closed, and the round. ``handle_v2_restore`` is
    NOT idempotent: a successful restore sets ``applied = False``, so a SECOND
    call refused with "nothing is applied to undo", this closure returned
    ``False``, and the second asker re-labelled its verdict
    :data:`REASON_CORRECTION_ROLLBACK_FAILED` — whose household copy says the
    correction is **still applied**. It is not. That false sentence about their
    own speaker was the defect, not the extra call, so the guard fixed the
    sentence rather than merely the reachability: the restore is attempted
    once, and every later caller is handed the FIRST call's outcome verbatim.

    **Kept now that the callers are down to one**, deliberately. It is a
    property of THIS closure rather than of any caller's discipline, and it is
    what makes "a successful restore never reports failure" hold without the
    binding having to know how many askers exist — which is exactly the
    assumption that broke last time. The one-owner rule is pinned separately,
    at the flow (``test_two_restore_triggers_run_one_undo_and_keep_the_honest_
    sentence`` asserts no second ``self._seams.rollback(`` call site survives).
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

    def _restore_once(reason: str) -> bool:
        try:
            payload = handle_v2_restore(run_async, camilla_factory)
        except CrossoverV2Refused as exc:
            log_event(
                logger, "correction.crossover_v2_delta_probe_restore_refused",
                level=logging.WARNING, reason=reason, detail=str(exc),
            )
            return False
        restored = payload.get("status") == "restored"
        # The declaration outcome (#2292) rides the payload, which this seam
        # reduces to a bool for its conductor caller — so on an AUTOMATIC
        # rollback a refused declaration is journal-only
        # (``event=correction.crossover_v2_restore_sound_declaration``), not
        # household-visible.
        #
        # #2291 Phase 3a bound this seam on the stage that actually reaches the
        # delta probe (``STAGE_VERIFY_CAPABILITIES``), so an automatic rollback
        # now genuinely runs — the two halves that used to sit on different
        # stages have met. The declaration outcome is still journal-only:
        # widening the seam's return shape to carry a household sentence means
        # deciding which screen renders it, and the refusal the probe returns
        # already routes through the verify_fail screen's own copy.
        log_event(
            logger, "correction.crossover_v2_delta_probe_restore",
            level=logging.WARNING if not restored else logging.INFO,
            reason=reason, status=payload.get("status"),
            # The same code the household's own Undo journals (#2519). This
            # caller has NO screen — it reduces the payload to a bool for the
            # conductor — so the refusal's cause exists nowhere else at all.
            code=_restore_refusal_code(payload),
            sound_declaration=str(payload.get("sound_declaration") or ""),
        )
        return restored

    return _rollback


#: The declaration half of Undo (#2292), as four named outcomes. The DSP graph
#: and the ``/sound`` declaration are restored by two different owners through
#: two different writers, so the restore payload reports them separately rather
#: than letting one ``status`` speak for both. An Undo whose graph half
#: SUCCEEDED carries exactly one of these in ``payload["sound_declaration"]``.
#: An Undo whose graph half did NOT succeed omits the key entirely, because the
#: declaration leg never ran — the graph is still the new one, so reverting the
#: declaration would invent the inconsistency this exists to remove.
#:
#: There is deliberately no "restored, probably" — a declaration this could not
#: put back is named and handed to the household, because the alternative is a
#: speaker that plays one crossover while ``/sound`` declares another and says
#: nothing about it.
DECLARATION_RESTORED = "declaration_restored"
DECLARATION_REFUSED_SOUND_MOVED = "declaration_refused_sound_moved"
DECLARATION_NOT_APPLICABLE = "declaration_not_applicable"
DECLARATION_RESTORE_FAILED = "declaration_restore_failed"


def _parse_sound_declaration_undo(record: Any) -> tuple[int, Any] | None:
    """The accept-time undo record, validated, or ``None``.

    ``None`` covers three honest cases that all mean the same thing to the
    restore path — there is no declaration write to reverse: this apply never
    touched Sound (the as-declared winner, which is most applies), the apply
    predates #2292, or the persisted record is not the shape this build writes.

    The crossover half is read by ``crossover_declaration.change_from_record``,
    the SAME reader the apply path uses to resume a retry, so a record one of
    them accepts cannot be one the other rejects. Only the revision — which is
    this module's own review-binding token, not part of the crossover — is
    validated here.
    """
    from jasper.active_speaker.crossover_declaration import change_from_record

    if not isinstance(record, Mapping):
        return None
    revision = record.get("sound_revision")
    change = change_from_record(record)
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 0
        or change is None
    ):
        return None
    return revision, change


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


def _restore_sound_declaration(record: Any) -> tuple[str, str]:
    """Put ``/sound``'s declared crossover back to its pre-accept value.

    The declaration half of Undo (#2292). ``handle_v2_apply``'s
    alternative-Fc path writes the winning measured Fc into the Sound
    declaration before it touches DSP (#2290), so an Undo that only reloads
    the previous DSP graph leaves the speaker playing one crossover while
    ``/sound`` declares another — one fact with two answers, and the wrong one
    is the one the next measurement session reads as its configured Fc.

    Goes back through
    :func:`jasper.web.sound_setup.apply_measured_crossover_geometry`, the
    SAME in-process revision-checked writer the forward path used, with its
    two geometries swapped. Sound stays the only writer of the crossover; this
    is the forward write run backwards, not a second route into the
    declaration — and it covers the slope for the same reason it covers the
    frequency, because one writer wrote both.

    Returns ``(outcome, household_message)``. **Never raises**: the graph is
    already back by the time this runs, so a declaration that could not be
    restored is a fact to report beside a successful restore, never a 500 that
    hides it. The message is empty when nothing needs saying.
    """
    from jasper.active_speaker.design_draft import (
        ActiveSpeakerDesignDraftRevisionConflict,
        load_design_draft,
    )
    from jasper.output_topology import load_output_topology
    from jasper.web.sound_setup import apply_measured_crossover_geometry

    parsed = _parse_sound_declaration_undo(record)
    if parsed is None:
        log_event(logger, "correction.crossover_v2_restore_sound_declaration",
                  outcome=DECLARATION_NOT_APPLICABLE)
        return DECLARATION_NOT_APPLICABLE, ""
    revision, change = parsed
    slope_moved = change.changes_slope
    applied_label = _crossover_label(change.selected, slope_moved)
    previous_label = _crossover_label(change.configured, slope_moved)

    def _refused(current_revision: Any) -> tuple[str, str]:
        log_event(logger, "correction.crossover_v2_restore_sound_declaration",
                  level=logging.WARNING, outcome=DECLARATION_REFUSED_SOUND_MOVED,
                  accepted_revision=revision, current_revision=current_revision)
        return DECLARATION_REFUSED_SOUND_MOVED, (
            f"The previous sound is back, but Sound still declares "
            f"{applied_label}: the speaker design changed after this "
            f"crossover was applied, so JTS left it alone. Set the crossover "
            f"back to {previous_label} in Sound settings if you want it.")

    def _failed(exc: BaseException) -> tuple[str, str]:
        log_event(logger, "correction.crossover_v2_restore_sound_declaration",
                  level=logging.ERROR, outcome=DECLARATION_RESTORE_FAILED,
                  accepted_revision=revision, error_type=type(exc).__name__)
        return DECLARATION_RESTORE_FAILED, (
            f"The previous sound is back, but Sound's declared crossover "
            f"could not be changed back from {applied_label} — set it to "
            f"{previous_label} in Sound settings.")

    # Read the live revision first so the ordinary "someone edited Sound"
    # refusal is named exactly and costs no write attempt. Without it, an edit
    # that moved the crossover ITSELF trips
    # ``apply_measured_crossover_geometry``'s value guard, and a household
    # edit would be reported as a failed write. The writer's own CAS below is
    # still the authority: it closes the window between this read and that
    # write, and it refuses BEFORE composing anything, so a Sound that moved in
    # between is left byte-identical either way.
    try:
        current_revision = load_design_draft(
            topology=load_output_topology()).get("revision")
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return _failed(exc)
    if isinstance(current_revision, bool) or current_revision != revision:
        return _refused(current_revision)
    try:
        apply_measured_crossover_geometry(
            expected_revision=revision,
            between_roles=change.between_roles,
            # Swapped: what the accept PUT there is what must be live now, and
            # what it displaced is what goes back.
            configured=change.selected,
            selected=change.configured,
        )
    except ActiveSpeakerDesignDraftRevisionConflict as exc:
        return _refused(exc.current_draft.get("revision"))
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return _failed(exc)
    log_event(logger, "correction.crossover_v2_restore_sound_declaration",
              outcome=DECLARATION_RESTORED, accepted_revision=revision,
              restored_fc_hz=change.configured.fc_hz,
              restored_slope_db_per_octave=change.configured.slope_db_per_octave)
    return DECLARATION_RESTORED, ""


#: Why a rollback anchor cannot be restored from, as five named codes. On the
#: journal and the round receipt, so "we could not put the old sound back" says
#: WHICH of the five it was without re-deriving it from a sentence.
ANCHOR_NOT_APPLIED = "not_applied"
ANCHOR_NO_PRE_APPLY_PROFILE = "no_pre_apply_profile"
ANCHOR_TOPOLOGY_CHANGED = "topology_changed"
#: The speaker is no longer playing the graph this round applied — something
#: changed the running config out of band since the apply (#2537).
ANCHOR_RUNNING_CONFIG_DIVERGED = "running_config_diverged"
#: The stashed "previous sound" is not the graph this round displaced — the
#: applied-baseline-profile record it was frozen from was already stale when the
#: apply read it, so restoring it would put back a sound this round never
#: replaced (#2559).
ANCHOR_STASH_NOT_DISPLACED = "stash_not_displaced"


@dataclass(frozen=True)
class RollbackAnchorRefusal:
    """One reason Undo cannot run, with the sentence the household reads."""

    code: str
    message: str


def restore_anchor_static_prefix_refusal(
    state: Mapping[str, Any] | None,
) -> RollbackAnchorRefusal | None:
    """The first two of :func:`rollback_anchor_refusal`'s five, or ``None``.

    Split out for the THIRD reader (#1863): the ``can_undo`` flag
    :func:`crossover_v2_status_block` publishes, which decides whether the
    envelope layer offers an Undo button at all. That reader cannot call the
    full resolver — gate 3 loads the live output topology, and this runs on
    every household status poll — but it must not answer a DIFFERENT question
    from the endpoint the button posts to, which is exactly the drift
    :func:`rollback_anchor_refusal`'s own "one owner" argument exists to stop.
    So the prefix a pure state read CAN answer is owned here and asked by both.

    Gates 3-5 are deliberately not in this prefix: two compare stored config
    identity and one needs a live CamillaDSP reading. A household that trips
    one of those gets an actionable sentence from a button that exists, which
    is a legitimate refusal — unlike the first two, which are the guaranteed
    click-to-fail #1863 removes.
    """
    if not state or not state.get("applied"):
        return RollbackAnchorRefusal(
            ANCHOR_NOT_APPLIED,
            "nothing is applied to undo; measure and apply a crossover first",
        )
    if not isinstance(state.get("pre_apply_profile"), Mapping):
        # Now that persist_conductor_state carries pre_apply_profile forward
        # across every conductor snapshot (W6.12 P0), this branch is reached
        # ONLY on a genuine first-ever apply — there was never an earlier
        # profile to stash. Say so plainly rather than reading like a bug
        # report or a generic "no undo available" error.
        return RollbackAnchorRefusal(
            ANCHOR_NO_PRE_APPLY_PROFILE,
            "this is the first measured crossover on this speaker — there's "
            "no earlier one to restore; use Speaker setup to remove it instead",
        )
    return None


def rollback_anchor_refusal(
    state: Mapping[str, Any] | None,
    *,
    running_config_path: str | None = None,
) -> RollbackAnchorRefusal | None:
    """Why this durable state has no restorable anchor, or ``None`` if it has.

    **One owner for a rule with three readers.** :func:`handle_v2_restore`
    raises on this, and #2291's ``rollback_available`` seam asks it before the
    round's adoption decision commits to a ``restore`` instruction. Before this
    function the two would have been separate transcriptions of the same
    preconditions, and a round could have promised a restore that Undo then
    refused — the exact drift #2291 exists to close. #1863 added the third,
    ``crossover_v2_status_block``'s ``can_undo``, which decides whether the
    button is offered at all; it cannot afford gate 3's live topology read on
    every status poll, so it asks
    :func:`restore_anchor_static_prefix_refusal` — the prefix THIS function
    also delegates to, rather than a fourth copy of the same two conditions.

    The five are, in the order Undo needs them:

    * nothing is applied, so there is no apply to reverse;
    * nothing was stashed — a genuine first-ever apply on this speaker;
    * the stash was composed for an output topology that has since changed, so
      reloading it would realize the WRONG graph (item 2, #1605). A stash that
      predates the fingerprint carries none, cannot be compared, and is
      allowed through to the restore path, which validates the config bytes
      itself;
    * the stash does not name the graph this round DISPLACED (#2559);
    * the speaker is no longer playing the graph this round APPLIED (#2537).

    **Why the fourth and fifth exist.** On 2026-08-15 (jts3 cycle 4) an
    operator reconciled the running config out of band at 01:00; at 07:30 a
    round's restore fired, faithfully put back the profile record's "previous
    sound" — run 2's candidate, applied six and a half hours earlier — and
    reported success. Every check that existed then passed, because every one of
    them asks about the ANCHOR and none asks whether the graph being replaced is
    the one this round put there. A restore is a check-then-act on a shared
    resource; the fifth check is its check.

    The FOURTH is the other half of the same shape, and it took a second live
    firing to find (#2559). At 14:47 the same day, ninety-seven seconds after an
    apply, the fifth check passed — correctly, nothing had moved the running
    graph in ninety-seven seconds — and the restore resurrected the same stale
    candidate anyway. The staleness was not downstream of the apply; it was
    already inside the stash, because ``pre_apply_profile`` is frozen from the
    applied-baseline-profile record and that record had been stale since 00:34.
    So the two checks ask about different moments and both are needed: the
    fourth asks whether the stash was right when it was TAKEN, the fifth whether
    it is still right now.

    **The fourth is where a stale record stops being able to hurt a household,
    and it adds no writer.** #2537 named three options for the record; this is
    still option (b) — validate at the READER — extended to the one reader that
    resolves a restore TARGET rather than merely a precondition. The record's
    sole writer is still ``persist_applied_baseline_profile`` on the apply path,
    ``reconcile-current-dsp`` stays ignorant of the active-speaker profile
    system, and no operator repair is required for correctness: a stale record
    now refuses a restore instead of aiming one, and the next apply overwrites
    it as it always did.

    It refuses rather than re-anchors, and that is the honest half: this
    function knows the running graph is not the one the round applied, and it
    does not know what the household would want done about that. Stomping a
    config an operator deliberately reconciled to is the one outcome nobody
    asked for.

    **The fifth check needs a LIVE reading, which is why it is a parameter and
    why ``None`` skips it.** ``running_config_path`` is what CamillaDSP reports
    right now — an async round trip this pure function must not make, and one
    the ``rollback_available`` capability probe deliberately does not make
    either: a camilla hiccup would flip that probe to "no anchor" and route an
    otherwise-fine round to ``recovery_required``. So the probe answers the
    four STATIC preconditions and :func:`handle_v2_restore` adds the live one
    at the moment of action, which is where a check-then-act window belongs.
    The two can therefore disagree, and the disagreement is bounded and loud: a
    divergence found at the endpoint returns "not restored", which re-grades the
    round with ``restore_failed=True`` into ``recovery_required``. That is the
    operator path, not a silent difference of opinion.

    **The fourth is static, so the probe DOES answer it, and that is the point.**
    Stash-versus-displaced needs no live reading — both facts were written into
    the durable state by the same apply — so a round learns before it decides
    that it has no restorable anchor, and routes a would-be restore to
    ``recovery_required`` instead of promising one the seam then refuses. That
    is #2291's own drift argument applied to the new check rather than an
    exception carved out of it.

    A state with no ``round_anchor`` — applied before #2537 shipped, or by an
    apply that could not name either path — cannot be compared by either anchor
    check and is allowed through, exactly as a stash predating the topology
    fingerprint is.

    Pure and side-effect-free by design: it is called on the read path to
    answer a capability question, so it must not log, mutate, or apply
    anything. :func:`handle_v2_restore` owns the journal lines for the
    topology, stale-stash and divergence cases, because those fire when a
    household actually presses the button.
    """
    from jasper.active_speaker.baseline_profile import topology_config_fingerprint
    from jasper.output_topology import load_output_topology

    static_prefix = restore_anchor_static_prefix_refusal(state)
    if static_prefix is not None:
        return static_prefix
    # Both guaranteed by the prefix above: ``state`` is a Mapping and
    # ``pre_apply_profile`` is one too. Bound once, because the prefix call
    # narrows for the reader but not for the type checker — the gates below
    # read ``state`` again and would each need their own ``or {}``.
    resolved = state or {}
    pre_apply_profile = resolved["pre_apply_profile"]
    stashed_source = pre_apply_profile.get("source")
    stashed_topology_fp = (
        str(stashed_source.get("topology_fingerprint") or "")
        if isinstance(stashed_source, Mapping)
        else ""
    )
    if stashed_topology_fp:
        current_topology_fp = topology_config_fingerprint(load_output_topology())
        if current_topology_fp != stashed_topology_fp:
            return RollbackAnchorRefusal(
                ANCHOR_TOPOLOGY_CHANGED,
                "the speaker's output configuration changed since this "
                "crossover was applied, so the previous sound can't be safely "
                "restored — re-measure the crossover instead",
            )
    stashed_config = pre_apply_profile.get("config")
    if restore_target_diverged(
        resolved.get(ROUND_ANCHOR_STATE_KEY),
        str(stashed_config.get("path") or "")
        if isinstance(stashed_config, Mapping) else "",
        str(stashed_config.get("sha256") or "")
        if isinstance(stashed_config, Mapping) else "",
    ):
        return RollbackAnchorRefusal(
            ANCHOR_STASH_NOT_DISPLACED,
            "JTS can't tell which sound to put back: the sound it has on "
            "record as the previous one isn't the one this tuning replaced — "
            "check the speaker's current sound, then re-measure",
        )
    if running_config_diverged(
        resolved.get(ROUND_ANCHOR_STATE_KEY), running_config_path
    ):
        return RollbackAnchorRefusal(
            ANCHOR_RUNNING_CONFIG_DIVERGED,
            "the speaker's sound was changed by something else after this "
            "crossover was applied, so undoing it now would replace that "
            "change instead — check the speaker's current sound first",
        )
    return None


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

    An apply that also WROTE the winning Fc into the Sound declaration (#2290's
    alternative-Fc path) owes a second leg: ``/sound`` must go back to what it
    declared before that accept, or say why it did not (#2292). The two legs
    are reported separately — ``status`` stays the DSP graph's answer and
    ``sound_declaration`` carries the declaration's — because they are restored
    by different owners through different writers and can genuinely disagree.
    The declaration leg runs only after the graph is back, never before: a
    declaration reverted ahead of a restore that then failed would invent the
    same inconsistency in the other direction.

    Never raises a bare exception for an ordinary refusal — every refusal is
    a :class:`CrossoverV2Refused` (maps to 400 at the dispatch ladder,
    exactly like every other v2 endpoint refusal) so a household stuck on a
    bad-sounding candidate always gets a named answer, never a 500.
    """
    from jasper.active_speaker.baseline_profile import (
        restore_applied_baseline_profile,
    )

    state = load_v2_state()
    cam = camilla_factory()
    # The LIVE half of the fifth precondition (#2537), read here and only
    # here: what is CamillaDSP actually playing right now? Guarded to ``None``
    # — "could not compare" — because a camilla that cannot answer is a camilla
    # the restore below is about to fail against anyway, and refusing on its
    # silence would trade a real remedy for a non-finding.
    try:
        running_config_path = run_async(cam.get_config_file_path(best_effort=True))
    except (OSError, RuntimeError, TypeError, ValueError, AttributeError):
        running_config_path = None
    # The five preconditions live in ``rollback_anchor_refusal`` (#2291, #2537,
    # #2559), so this endpoint and the round's ``rollback_available`` seam
    # cannot come to different conclusions about the same state. Item 2
    # (#1605)'s topology check is one of them, and its journal line stays HERE:
    # it fires when a household actually pressed Undo, not on every capability
    # probe. So does the stale-stash line, for that same reason alone — the
    # seam DOES answer that precondition, it just does not journal it. And so
    # does the divergence line, for that reason and one more: the seam does not
    # take the live reading at all.
    refusal = rollback_anchor_refusal(
        state, running_config_path=str(running_config_path or "") or None,
    )
    if refusal is not None:
        if refusal.code == ANCHOR_TOPOLOGY_CHANGED:
            log_event(
                logger,
                "correction.crossover_v2_restore_topology_mismatch",
                level=logging.WARNING,
            )
        elif refusal.code == ANCHOR_RUNNING_CONFIG_DIVERGED:
            log_event(
                logger,
                "correction.crossover_v2_restore_running_config_diverged",
                level=logging.ERROR,
                running_config_path=str(running_config_path or ""),
                applied_config_path=str(
                    ((state or {}).get(ROUND_ANCHOR_STATE_KEY) or {})
                    .get("applied", {})
                    .get("config_path") or ""
                ),
            )
        elif refusal.code == ANCHOR_STASH_NOT_DISPLACED:
            # Both paths, because the whole finding is that they disagree — and
            # a reader diagnosing this needs to see WHICH stale record the
            # stash was frozen from, not just that one was (#2559).
            log_event(
                logger,
                "correction.crossover_v2_restore_stash_not_displaced",
                level=logging.ERROR,
                stash_config_path=str(
                    ((state or {}).get("pre_apply_profile") or {})
                    .get("config", {})
                    .get("path") or ""
                ),
                displaced_config_path=str(
                    ((state or {}).get(ROUND_ANCHOR_STATE_KEY) or {})
                    .get("displaced", {})
                    .get("config_path") or ""
                ),
            )
        raise CrossoverV2Refused(refusal.message)
    # No refusal means the anchor cleared all five checks, so the state is
    # present and ``pre_apply_profile`` is a Mapping — indexed rather than
    # re-tested, because a second test here would be a fifth transcription of
    # the rule this function was extracted to own.
    pre_apply_profile = (state or {})["pre_apply_profile"]
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
        # Read off the state loaded above, before ``observe_restore`` clears
        # the record — the graph is back, so this is the moment the
        # declaration owes the same reversal.
        outcome, message = _restore_sound_declaration(
            # ``(state or {})`` for the same reason as ``pre_apply_profile``
            # above: ``rollback_anchor_refusal`` is what proves ``state`` is
            # present now, and it does that by returning ``None`` rather than
            # by narrowing the local the way the inline guard it replaced did.
            (state or {}).get("sound_declaration_undo")
        )
        payload["sound_declaration"] = outcome
        if message:
            payload["sound_declaration_message"] = message
        observe_restore()
    log_event(
        logger,
        "correction.crossover_v2_restored",
        status=payload.get("status"),
        code=_restore_refusal_code(payload),
        sound_declaration=str(payload.get("sound_declaration") or ""),
    )
    return payload


def _restore_refusal_code(payload: Mapping[str, Any]) -> str:
    """WHY a restore did not restore, as its issue code (``""`` on success).

    The journal is the ONLY record of a refused restore (#2519). ``status``
    alone cannot carry the reason: the two anchor-integrity refusals return
    ``blocked``, the same word ``restore_target_invalid`` and
    ``restore_target_missing`` already return, and — because all four refuse
    before ``apply_dsp_config`` is ever entered — nothing lands in
    ``dsp_apply_state.json`` either. The delta probe's automatic rollback has
    no household screen at all, so without this a jts3-shaped failure is again
    a status with no cause behind it. Shared by both log sites so the manual
    Undo and the automatic rollback cannot name the same refusal differently.
    """
    return str((_blocking_apply_issue(payload) or {}).get("id") or "")


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
