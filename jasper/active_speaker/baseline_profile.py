# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Compile and apply accepted active-speaker baseline profiles."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Mapping, Sequence

from jasper.bass_extension.dynamic import validate_dynamic_bass_descriptor
from jasper.dsp_apply import (
    same_config_file,
)
from jasper.log_event import log_event
from jasper.output_topology import (
    canonical_fingerprint as _fingerprint,
)

from ._common import coerce_finite_float
from .camilla_yaml import (
    _branch_context,
    linearization_headroom_db,
)
from .crossover_contract import (
    measured_level_match_applied,
)
from .crossover_preview import crossover_preview_fingerprint
from .driver_base_trim import (
    BANK_CLEAR_FAILED,
    BANK_CORRECTION_ENTRY_UNREADABLE,
    BANK_CORRECTIONS_UNREADABLE,
    BANK_PARTLY_MEASURED,
    BANK_READINESS_UNREADABLE,
    BANK_UNMEASURED,
    BANK_WRITE_FAILED,
    BANK_WRITE_REFUSED,
    REFUSE_NO_FRAME,
    DriverBaseTrimError,
    banked_base_trims,
    clear_base_trim,
    load_base_trim,
    write_base_trim,
)
from .measurement_programs import PROGRAM_DOCUMENT_ORDER, PURPOSE_SPEAKER
from .profile import ActiveSpeakerConfigError, ActiveSpeakerPreset, required_driver_roles
from .profile import snapshot_declares_single_branch
from . import passive_profile as _passive
from .state_paths import (
    baseline_profile_state_path,
)

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
BASELINE_PROFILE_KIND = "jts_active_speaker_baseline_profile_candidate"

# Canonical per-parameter provenance vocabulary (SC-3).
PROVENANCE_MANUAL = "manual"
PROVENANCE_MEASURED = "measured"
PROVENANCE_AUTHORED_BY_MODEL = "authored_by_model"
PROVENANCE_SET_BY_USER = "set_by_user"

#: ``corrections_source`` values an operator set by hand. Every other source
#: that is not ``measured`` fell back to weaker evidence (the datasheet, an
#: estimate, a preserved manual crossover).
_PINNED_GAIN_SOURCES = frozenset({"operator_pinned", "explicit"})


def applied_bass_extension(profile: Mapping[str, Any] | None = None) -> dict[str, Any]:
    source = load_applied_baseline_profile_state() if profile is None else profile
    snapshot = (source or {}).get("recomposition_snapshot") or {}
    raw = snapshot.get("bass_extension") or {}
    return validate_dynamic_bass_descriptor(raw) if raw else {}


def baseline_candidate_fingerprint(candidate: Mapping[str, Any]) -> str:
    """Identify the exact immutable Layer-A candidate, not its cache source."""

    source = candidate.get("source")
    snapshot = candidate.get("recomposition_snapshot")
    hashed_snapshot = dict(snapshot) if isinstance(snapshot, Mapping) else None
    if hashed_snapshot is not None and isinstance(
        hashed_snapshot.get("level_match"), Mapping
    ):
        # Capture recency changes on recompose; graph identity must not.
        # Candidates without this field must keep their stored fingerprint.
        hashed_snapshot["level_match"] = {
            key: value
            for key, value in hashed_snapshot["level_match"].items()
            if key != "newest_capture_at"
        }
    return _fingerprint({
        "artifact_schema_version": candidate.get("artifact_schema_version"),
        "kind": candidate.get("kind"),
        "source_fingerprint": (
            source.get("fingerprint") if isinstance(source, Mapping) else None
        ),
        "recomposition_snapshot": hashed_snapshot,
    })


def measured_level_trims(
    preset: ActiveSpeakerPreset,
    crossover_preview: Mapping[str, Any] | None = None,
    *,
    design_draft: Mapping[str, Any] | None = None,
) -> tuple[dict[str, float], dict[str, Any]]:
    """This box's banked per-driver level offsets for the declaration in hand.

    The one owner of *"what does this box's own evidence say the per-driver
    level offsets are?"*, which the crossover-v2 MEASUREMENT graph levels by.
    ``meta['source']`` names the evidence that answered, which is what a caller
    discloses beside the trims; empty trims mean none did (``meta['base_trim']``
    says why), and no caller may substitute an estimate for them.
    """
    roles = required_driver_roles(preset.way_count)
    declaration_fingerprint = (
        crossover_preview_fingerprint(crossover_preview, design_draft)
        if isinstance(crossover_preview, Mapping) and crossover_preview
        else None
    )
    base_trims, base_trim_meta = banked_base_trims(declaration_fingerprint, roles)
    if not base_trims:
        return {}, {"base_trim": base_trim_meta}
    banked_group_ids = base_trim_meta.get("speaker_group_ids") or []
    return base_trims, {
        "source": "banked_base_trim",
        "base_trim": base_trim_meta,
        "newest_capture_at": base_trim_meta.get("measured_at"),
        # The record's own trim source, not a second word for it: the
        # apply that banked it stamped WHICH evidence levelled the
        # graph, and this ledger repeats that rather than minting a
        # comparison of its own.
        "comparison": base_trim_meta.get("trim_source"),
        "groups_total": len(banked_group_ids),
        "groups_measured": len(banked_group_ids),
        "measured_group_ids": list(banked_group_ids),
        # Empty because the record banks an ALREADY-APPLIED level
        # match: the per-crossover evidence behind it lives with the
        # profile that was applied, which ``base_trim.trim_source``
        # names.
        "deltas": [],
        "incomparable_groups": [],
        "trims": dict(base_trims),
    }


def _load_saved_state(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    if (
        raw.get("artifact_schema_version") != SCHEMA_VERSION
        or raw.get("kind") != BASELINE_PROFILE_KIND
    ):
        return None
    return raw


def applied_profile_anchor(
    saved: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    """Current or retained applied profile behind a mutable candidate state."""
    if not isinstance(saved, Mapping):
        return None
    if saved.get("status") == "applied":
        return saved
    prior = saved.get("applied_recomposition_profile")
    if isinstance(prior, Mapping) and prior.get("status") == "applied":
        return prior
    return None


def _frozen_applied_profile(
    saved: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Return the applied record fields consumed by runtime readers."""
    applied = applied_profile_anchor(saved)
    if applied is None:
        return None
    snapshot = applied.get("recomposition_snapshot")
    # candidate_fingerprint is derived data, not an authority. Older saved
    # profiles may omit it and a partially written/corrupt profile may carry a
    # value that no longer identifies its immutable snapshot. Always migrate
    # or repair it from the exact source + snapshot content consumers trust.
    candidate_fingerprint = (
        baseline_candidate_fingerprint(applied)
        if isinstance(snapshot, Mapping)
        else None
    )
    return {
        "artifact_schema_version": applied.get("artifact_schema_version"),
        "kind": applied.get("kind"),
        "status": "applied",
        "applied_at": applied.get("applied_at"),
        "candidate_fingerprint": candidate_fingerprint,
        "candidate_artifact_path": applied.get("candidate_artifact_path"),
        "source": dict(applied.get("source") or {}),
        "config": dict(applied.get("config") or {}),
        "corrections": dict(applied.get("corrections") or {}),
        "corrections_source": dict(applied.get("corrections_source") or {}),
        **({"timing": applied["timing"]} if "timing" in applied else {}),
        "gain_provenance": dict(applied.get("gain_provenance") or {}),
        "corrections_provenance": dict(applied.get("corrections_provenance") or {}),
        "level_match": dict(applied.get("level_match") or {}),
        "automatic_candidate": dict(applied.get("automatic_candidate") or {}),
        "linearization": dict(applied.get("linearization") or {}),
        "linearization_outcome": str(applied.get("linearization_outcome") or ""),
        "trim_decision": dict(applied.get("trim_decision") or {}),
        "blend_correction": list(applied.get("blend_correction") or []),
        "room_correction": dict(applied.get("room_correction") or {}),
        "tuning_owner": str(applied.get("tuning_owner") or ""),
        # Quality state belongs to the immutable applied anchor too.  Dropping
        # it here lets an older sensitivity-only profile masquerade as a
        # measured profile on every consumer of the frozen view.
        "provisional": bool(applied.get("provisional")),
        "recomposition_snapshot": dict(snapshot) if isinstance(snapshot, Mapping) else None,
    }


def _profile_branch_context(
    profile: Mapping[str, Any],
) -> dict[str, tuple[Any, float]]:
    """The ``(crossover sections, trim_db)`` context for one profile's charge.

    Rebuilt from the SAME two snapshot fields the graph was emitted from — the
    preset's crossover regions and the per-driver ``corrections`` gains — via
    the emitter's own :func:`~jasper.active_speaker.camilla_yaml._branch_context`,
    so a headroom read back off a profile is evaluated over the chain that
    profile actually carries.

    ``{}`` when either field is missing or unparseable. That is the same
    over-estimating fallback ``linearization_headroom_db`` applies to a role
    absent from a supplied context ("no crossover, no trim"), and it is the
    right direction here: an over-estimated headroom read makes the DECLARED
    apply-boundary offset larger than the graph's, which under-corrects the
    delta probe and leaves the difference visible as ``residual_offset_db``
    rather than hiding it.
    """
    snapshot = profile.get("recomposition_snapshot")
    if not isinstance(snapshot, Mapping):
        return {}
    corrections = snapshot.get("corrections")
    if not isinstance(corrections, Mapping):
        return {}
    try:
        preset = ActiveSpeakerPreset.from_mapping(dict(snapshot.get("preset") or {}))
    except (ActiveSpeakerConfigError, TypeError, ValueError):
        return {}
    return _branch_context(preset, corrections)


def profile_program_headroom_db(profile: Mapping[str, Any] | None) -> float:
    """The pre-split common attenuation one profile's linearization costs, dB."""
    linearization = profile_linearization(profile)
    if not linearization:
        return 0.0
    return linearization_headroom_db(
        linearization, branch_context=_profile_branch_context(profile or {}),
    )


def profile_blend_correction(
    profile: Mapping[str, Any] | None,
) -> tuple[Any, ...] | None:
    """Read the snapshot first, then the legacy top-level blend correction.

    None means unknown; () means no correction. Never turn unknown into zero:
    that can double-count correction already present in the measured graph.
    Preserve every entry for blend_filters_from_mapping to validate.
    """

    if not isinstance(profile, Mapping):
        return None
    snapshot = profile.get("recomposition_snapshot")
    raw = (
        snapshot.get("blend_correction") if isinstance(snapshot, Mapping) else None
    )
    if raw is None:
        raw = profile.get("blend_correction")
    if raw is None:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, Mapping)):
        return None
    return tuple(raw)


def profile_linearization(profile: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """One profile's AUTHORITATIVE reduced linearization, or ``{}``."""
    if not isinstance(profile, Mapping):
        return {}
    snapshot = profile.get("recomposition_snapshot")
    linearization = (
        snapshot.get("linearization") if isinstance(snapshot, Mapping) else None
    )
    if not isinstance(linearization, Mapping):
        linearization = profile.get("linearization")
    return linearization if isinstance(linearization, Mapping) else {}


def applied_layers(profile: Mapping[str, Any] | None) -> dict[str, bool]:
    profile = profile or {}
    snapshot = profile.get("recomposition_snapshot") or {}
    return {row.purpose: bool(profile_linearization(profile) if row.purpose == PURPOSE_SPEAKER else
                              snapshot.get(row.candidate_fields[0].name,
                                           profile.get(row.candidate_fields[0].name) if row.profile_fallback else None))
            for row in PROGRAM_DOCUMENT_ORDER}


def applied_layer_names(profile: Mapping[str, Any] | None) -> dict[str, bool]:
    """``applied_layers``, keyed by its packet-facing layer name instead of purpose."""
    layers = applied_layers(profile)
    return {row.applied_name: layers[row.purpose] for row in PROGRAM_DOCUMENT_ORDER}


def profile_driver_corrections(profile: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """One profile's AUTHORITATIVE ``{role: {gain_db, delay_ms, inverted}}``, or ``{}``."""
    if not isinstance(profile, Mapping):
        return {}
    snapshot = profile.get("recomposition_snapshot")
    corrections = (
        snapshot.get("corrections") if isinstance(snapshot, Mapping) else None
    )
    if not isinstance(corrections, Mapping):
        corrections = profile.get("corrections")
    if not isinstance(corrections, Mapping):
        return {}
    if any(
        isinstance(values, Mapping)
        and isinstance(values.get("delay_ms"), bool)
        for values in corrections.values()
    ):
        return {}
    return corrections


def applied_program_level_delta_db(
    previous_profile: Mapping[str, Any] | None,
    applied_profile: Mapping[str, Any] | None,
) -> float:
    """dB the emitted graph's BROADBAND level MOVED across one apply (#1811).

    Negative when the apply made the speaker quieter — the ordinary case, and
    the whole reason this exists: the applied correction's boost is absorbed as
    a pre-split common attenuation, so the same commanded volume produces a
    materially quieter speaker the instant the config swaps.

    **This is an input to ANALYSIS, never to the speaker's level.** The
    absorption keeps the boosted branch at or below unity (see
    ``camilla_yaml.program_headroom_db``). Compensating at main volume would
    undo that attenuation. The consumer is
    :func:`~jasper.active_speaker.delta_probe.classify_delta_probe`, whose
    realized-vs-commanded comparison is not mean-centered and would otherwise
    read this move as a defect.

    Read from the profiles, never assumed: the magnitude is whatever the fit
    charged (22.5 dB on the session that surfaced this; a few dB once the
    loudness-doctrine work shrinks the charge), and a correction that hands
    headroom BACK yields a positive number.

    **This is the pre-split term, and it is the ONLY level term the commanded
    axis cannot carry** (#2611). The absorption is applied BEFORE the branch
    split, so ``predicted_branch_sum`` — a model of the two branches — has no
    place to put it, which is why it travels as a scalar beside the commanded
    curve rather than inside it. Per-branch trims are excluded here for the
    complementary reason: since #2611 they ARE on the commanded axis, which is
    the applied graph's predicted sum minus the PREVIOUS graph's
    (:mod:`jasper.active_speaker.crossover_v2.commanded`), so counting them here
    too would remove them twice. Before that fix they were in neither account —
    both sides of the commanded delta carried the applied candidate's own
    trims, so a per-role gain step cancelled out of the model entirely and
    landed in ``residual_offset_db`` as a surprise (+3.2198 dB on the
    2026-08-16 jts3 round). The two accounts are disjoint and, between them,
    complete for everything the apply commands.

    **One known incompleteness, deliberate, caught downstream.**
    Room-PEQ and preference-EQ headroom are excluded. The candidate's own room
    set is emitted, including its boost headroom, and the preference layer is not
    emitted at all; neither term is read here, so a round that changes either
    can see a real level move this reader cannot see. That remainder is
    exactly what the probe's ``residual_offset_db`` measures and what
    ``delta_probe.VERDICT_LEVEL_MISMATCH`` names — which is why this function
    is allowed to be an honest partial account rather than having to be a
    complete one.
    """
    return profile_program_headroom_db(previous_profile) - (
        profile_program_headroom_db(applied_profile)
    )


def load_baseline_profile_state(
    path: str | Path | None = None,
) -> dict[str, Any] | None:
    """Read one validated baseline artifact without deriving fresh evidence."""
    return _load_saved_state(baseline_profile_state_path(path))


def load_applied_baseline_profile_state(
    path: str | Path | None = None,
) -> dict[str, Any] | None:
    """Read the applied Layer-A SSOT, even while a new candidate is staged."""
    return _frozen_applied_profile(load_baseline_profile_state(path))


#: :func:`applied_profile_displacement`'s answers. ``""`` is the fourth and it
#: means the record is authoritative — an empty string rather than a word so
#: the check reads as a guard at every call site.
APPLIED_PROFILE_DISPLACED = "applied_profile_displaced"
APPLIED_PROFILE_PATH_UNKNOWN = "applied_profile_path_unknown"
APPLIED_PROFILE_RUNNING_UNKNOWN = "running_config_path_unknown"
#: The record names a config file that is no longer on disk. A fifth answer to
#: the same question, and NOT one of the four above: the record's own
#: ``config.exists`` is frozen at apply time, so only a fresh stat can say it.
APPLIED_PROFILE_CONFIG_MISSING = "applied_profile_config_missing"


def applied_profile_displacement(
    applied: Mapping[str, Any] | None,
    *,
    statefile_path: str | Path | None = None,
) -> str:
    """Is this applied-profile record still what the speaker is PLAYING? (#2537)

    ``""`` when the record's own ``config.path`` is the path CamillaDSP's
    durable statefile selects — the record is authoritative. Otherwise one of
    the three codes above, naming why it cannot be trusted as "the current
    sound".

    **The defect this exists for.** On 2026-08-15 (jts3 cycle 4) the applied
    record still named run 2's candidate while the speaker had been reconciled
    out of band to ``sound_current.yml``. Nothing compared the two, so a restore
    faithfully put back "the previous sound" per the record — a graph the
    speaker had not played for six and a half hours. The record had silently
    stopped being the truth, and the statefile is the one place that says so.

    **This adds a READER, never a second writer.** ``reconcile-current-dsp``
    and every other out-of-band path stay entirely ignorant of the
    active-speaker profile system, which is the separation of concerns that
    makes them safe to run; the record's only writer remains
    :func:`persist_applied_baseline_profile` on the apply path. What changes is
    that consumers stop assuming the record won a race it never entered.

    Fail-soft and *reporting*, never gating on its own: a missing statefile or a
    record with no path yields a code, and each caller decides what an unknown
    provenance means for its own question. An unreadable statefile is
    :data:`APPLIED_PROFILE_RUNNING_UNKNOWN` — "we could not check" — which is a
    different answer from "we checked and it moved", and conflating them is the
    class of mistake this whole issue is about.
    """

    from .environment import read_camilla_statefile_config_path

    config = (applied or {}).get("config")
    recorded = str(config.get("path") or "") if isinstance(config, Mapping) else ""
    if not recorded:
        return APPLIED_PROFILE_PATH_UNKNOWN
    running = read_camilla_statefile_config_path(statefile_path)
    if not running:
        return APPLIED_PROFILE_RUNNING_UNKNOWN
    # ``same_config_file`` rather than a comparison here: "do two paths name
    # one config file" is one rule, and it shipped as two near-verbatim copies
    # until an adversarial gate caught them. Its own docstring carries the
    # resolve-vs-string-compare reasoning.
    return "" if same_config_file(running, recorded) else APPLIED_PROFILE_DISPLACED


def _bank_applied_base_trim(candidate: Mapping[str, Any]) -> None:
    """Bank (or clear) the base trim the applied profile is actually playing.

    The single writer of
    :mod:`jasper.active_speaker.driver_base_trim`'s record, and the fix for the
    blind run's F-1: a box could apply a measured level match, report
    ``corrections_source: measured``, and still refuse a ``--level-matched``
    walk ``walk_level_match_no_evidence``, because the resolver
    (:func:`measured_level_trims`) reads only the banked record — never a
    candidate's applied corrections. Banking here makes the applied trim the
    very thing the resolver already looks for, so the walk door and the
    acoustic confirm unblock with no change of their own.

    Three answers, not two, and the middle one is the whole point:

    * **every** role sourced ``measured`` (beside ``level_match.applied``) —
      BANK. The profile is levelled by measurement end to end.
    * **some measured, and every other role OPERATOR-PINNED** — leave the bank
      ALONE, neither banking nor clearing. Pinning one driver by hand does not
      un-measure the speaker, so the prior full measurement is still the best
      evidence anyone has and destroying it on the strength of a pin loses
      real information.
    * **anything else** — CLEAR. No measured role at all, or a role that fell
      back to the datasheet (``sensitivity``/``estimate``) or to a preserved
      manual crossover. That is weaker evidence, not a pin, and a banked trim
      the box is not playing is the same lie pointing the other way.

    The pin/fallback line is drawn by :data:`_PINNED_GAIN_SOURCES` rather than
    by the ANY predicate alone, because ``level_match.applied`` is ITSELF only
    "some role was measured" — see the ANY-arm comment below.

    Fail-soft by contract, exactly as :func:`promote_applied_baseline_candidate`
    is: the graph is applied and read back by the time this runs, so a
    statefile that cannot be written must never turn a successful apply into a
    failure. The speaker then behaves as it did before this seam existed.
    """
    def emit(result: str, reason: str, detail: str, *, level: int) -> None:
        log_event(
            logger,
            "dsp.baseline_base_trim_banked",
            level=level,
            result=result,
            reason=reason,
            detail=detail,
        )

    def refused(reason: str, detail: str) -> None:
        emit("failed", reason, detail, level=logging.WARNING)

    def left_standing(reason: str, detail: str) -> None:
        emit("left_standing", reason, detail, level=logging.INFO)

    def cleared(reason: str, detail: str) -> None:
        if clear_base_trim():
            # A successful clear is a state change an operator must be able to
            # see: without this, the only evidence that a bank was dropped was
            # the absence of the file.
            emit("cleared", reason, detail, level=logging.INFO)
            return
        # The clear could not HAPPEN (EACCES, a read-only /var/lib), which is
        # the opposite of nothing-to-clear: a banked trim survives an apply
        # that is not playing it, and a --level-matched walk would level its
        # graph by numbers nothing applies.
        refused(BANK_CLEAR_FAILED, "a banked trim survived an apply it does not match")

    # Grouping artifacts must not replace the solo trim record.
    snapshot = candidate.get("recomposition_snapshot")
    if isinstance(snapshot, Mapping) and snapshot.get("domain") == "driver":
        return

    # A base trim is a FRAME — one role's level relative to the others.
    if snapshot_declares_single_branch(snapshot):
        left_standing(REFUSE_NO_FRAME, "one driver declared, so no roles to level")
        return

    corrections = candidate.get("corrections")
    sources = candidate.get("corrections_source")
    level_match = candidate.get("level_match")
    if not isinstance(corrections, Mapping) or not isinstance(sources, Mapping):
        refused(
            BANK_CORRECTIONS_UNREADABLE,
            "the applied profile names no corrections",
        )
        return
    measured = (
        isinstance(level_match, Mapping)
        and level_match.get("applied") is True
        and bool(corrections)
        and all(sources.get(role) == "measured" for role in corrections)
    )
    if not measured:
        # The middle arm, and it is narrower than "not every role measured".
        # `level_match.applied` is ALREADY only "some role was measured" (see
        # this module's own `level_match["applied"] = bool(measured_notes)`),
        # so the contract's ANY predicate alone cannot tell an operator PIN
        # from a measurement that was REFUSED and fell back to the datasheet.
        # Those are opposites: a pin leaves the speaker measured, while a
        # `sensitivity`/`estimate` fallback IS the weaker evidence the clear
        # exists for.
        if (
            measured_level_match_applied(candidate)
            and all(str(sources.get(role) or "") in _PINNED_GAIN_SOURCES
                    for role in corrections if sources.get(role) != "measured")
        ):
            left_standing(
                BANK_PARTLY_MEASURED,
                "some roles are operator-pinned; the prior banked trim "
                "remains the best measurement of this speaker",
            )
            return
        cleared(
            BANK_UNMEASURED,
            "the applied profile is not level-matched by measurement",
        )
        return
    assert isinstance(level_match, Mapping)  # narrowed by `measured` above
    readiness = candidate.get("automatic_candidate")
    source = candidate.get("source")
    if (
        not isinstance(readiness, Mapping)
        or not readiness.get("measured_group_ids")
        or not isinstance(source, Mapping)
    ):
        # Leave the bank standing rather than clearing it. A frozen applied
        # profile persisted before `_frozen_applied_profile` carried
        # `automatic_candidate` reaches the restore leg in exactly this shape,
        # and it is a MEASURED profile whose readiness block simply was not
        # kept — not evidence that the speaker was never measured.
        left_standing(
            BANK_READINESS_UNREADABLE,
            "the applied profile names no readiness or source block",
        )
        return
    trims_db: dict[str, float] = {}
    for role, entry in corrections.items():
        gain = (
            coerce_finite_float(entry.get("gain_db"))
            if isinstance(entry, Mapping)
            else None
        )
        if gain is None:
            # Typed refusal, never an exception: `float((entry or {}).get(...))`
            # raised AttributeError on a non-Mapping entry, and AttributeError
            # is not something a fail-soft seam catches — it escaped past
            # `persist_applied_baseline_profile` and failed a successful apply.
            left_standing(
                BANK_CORRECTION_ENTRY_UNREADABLE,
                f"correction {str(role)!r} names no finite gain_db",
            )
            return
        trims_db[str(role)] = gain
    # The record's ``measured_at`` is the EVIDENCE time, never this persist's
    # wall clock: this seam re-runs on frozen candidates (the apply retry
    # before the idempotent early-return, any re-apply of an older candidate),
    # and stamping now would re-date old evidence. Candidates carry their own
    # recency in the ledger; a frozen candidate from before that field existed
    # inherits the standing record's time (never re-dated forward), and only a
    # box with no dated evidence and no record lets the writer mint now.
    evidence_at = str(level_match.get("newest_capture_at") or "") or None
    if evidence_at is None:
        existing = load_base_trim()
        if existing is not None:
            evidence_at = str(existing.get("measured_at") or "") or None
    try:
        record = write_base_trim(
            trims_db=trims_db,
            roles=sorted(trims_db),
            speaker_group_ids=readiness.get("measured_group_ids") or [],
            declaration_fingerprint=str(
                source.get("crossover_preview_fingerprint") or ""
            ),
            trim_source=str(level_match.get("comparison") or ""),
            # WHICH CHAIN this trim was co-fitted with (#3479). The resolving
            # candidate's own fingerprint, already on the profile's source
            # block — passed through rather than derived, because the frame
            # exists at fit time and no later reader can reconstruct it. A
            # profile that names none banks as "frame unknown" rather than as
            # the bare frame.
            chain_fingerprint=_passive.measured_candidate_fingerprint(source) or None,
            measured_at=evidence_at,
        )
    except (OSError, DriverBaseTrimError) as exc:
        refused(
            exc.reason if isinstance(exc, DriverBaseTrimError) else BANK_WRITE_FAILED,
            str(exc),
        )
        # A measured graph is now playing and could not be banked, so whatever
        # was banked before describes some OTHER apply. Absent beats wrong:
        # the resolver's empty answer is conservative, while a stale record
        # levels the graph by numbers nothing is playing.
        cleared(BANK_WRITE_REFUSED, "the measured trim could not be banked")
        return
    log_event(
        logger,
        "dsp.baseline_base_trim_banked",
        result="ok",
        trims=" ".join(
            f"{role}={value:.1f}"
            for role, value in sorted(record["trims_db"].items())
        ),
        trim_source=record["trim_source"],
        declaration=record["declaration_fingerprint"][:12],
    )
