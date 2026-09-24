# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Load a composed baseline graph through DSP apply, record what was applied,
and publish its bytes under the canonical config name."""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Literal, Mapping

from jasper.atomic_io import CONFIG_FILE_MODE, atomic_write_text
from jasper.dsp_apply import DspApplyError, DspApplyState, apply_dsp_config, dsp_writer_lock
from jasper.json_fields import utc_now_iso as _utc_now
from jasper.log_event import log_event
from jasper.output_topology import OutputTopology, canonical_fingerprint as _fingerprint

from ._common import issue as _issue
from .baseline_profile import applied_profile_anchor, baseline_candidate_fingerprint, load_baseline_profile_state
from .baseline_record import protection_projection
from .driver_base_trim import bank_applied_base_trim
from .state_paths import (
    baseline_candidate_config_path, baseline_config_path, baseline_profile_state_path, config_text_sha256,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def load_composed_graph(
    text: str | Callable[[], Awaitable[str]], *, source: str,
    profile: Mapping[str, Any],
    load_config: Callable[[str], Awaitable[bool]],
    get_current_config_path: Callable[[], Awaitable[str | None]],
    persist: Callable[[], Any] | None = None,
    config_dir: str | Path | None = None,
    audition: bool = False,
    record: Literal["apply", "sound"] | None = "apply",
    sound_filter_count: int | None = None,
) -> AsyncIterator[tuple[DspApplyState, dict[str, Any]]]:
    from jasper.sound.camilla_yaml import extract_room_peqs_from_config_text, sound_audition_config_path  # lazy: sound graph metadata

    directory = Path(config_dir) if config_dir is not None else baseline_config_path().parent
    target = sound_audition_config_path(directory) if audition else directory / baseline_config_path().name
    applied = dict(profile)
    async with dsp_writer_lock(directory, source=source):
        async def write_graph() -> dict[str, Any]:
            nonlocal target, applied
            rendered = await text() if callable(text) else text
            applied = dict(profile)
            if record == "sound":
                saved = load_baseline_profile_state() or {}
                anchor = applied_profile_anchor(saved) or {}
                if not anchor.get("recomposition_snapshot") or not anchor.get("source"):
                    raise ValueError("Applied speaker record is incomplete")
                protection = protection_projection((profile.get("recomposition_snapshot") or {}).get("driver_protection"))
                applied = {**anchor, "source": {**anchor.get("source", {}),
                           "driver_protection_fingerprint": _fingerprint(protection)},
                           "recomposition_snapshot": {**anchor.get("recomposition_snapshot", {}), "driver_protection": protection}}
                applied["source"]["fingerprint"] = _fingerprint({key: value for key, value in applied["source"].items() if key != "fingerprint"})
            if not audition:
                target = baseline_candidate_config_path(rendered, directory / baseline_config_path().name)
            sha256 = config_text_sha256(rendered)
            applied["config"] = {**profile.get("config", {}), "path": str(target), "basename": target.name,
                                 "sha256": sha256, "exists": True}
            atomic_write_text(target, rendered, mode=CONFIG_FILE_MODE)
            return {"candidate_path": target, "expected_candidate_sha256": sha256,
                    "room_peq_count": len(extract_room_peqs_from_config_text(rendered))}

        state = await apply_dsp_config(
            source=source, candidate_path=target,
            prepare=write_graph,
            load_config=load_config, get_current_config_path=get_current_config_path,
            persist=persist, sound_filter_count=sound_filter_count,
        )
        if record == "apply":
            applied = persist_applied_baseline_profile(applied, apply_state=state.to_dict())
        elif record == "sound":
            saved = load_baseline_profile_state() or {}
            if saved.get("status") == "applied":
                saved = applied
            else:
                saved["applied_recomposition_profile"] = applied
            atomic_write_text(baseline_profile_state_path(), json.dumps(saved, indent=2, sort_keys=True) + "\n",
                              mode=CONFIG_FILE_MODE, durable=True)
        if record:
            promote_applied_baseline_candidate(applied)
        yield state, applied


def apply_started(topology: OutputTopology, candidate: Mapping[str, Any]) -> None:
    log_event(
        logger, "correction.crossover_apply_started",
        config_path=str((candidate.get("config") or {}).get("path") or ""),
        tuning_owner=candidate.get("tuning_owner"), topology_id=topology.topology_id,
        graph_fingerprint=(candidate.get("source") or {}).get("fingerprint"),
        candidate_fingerprint=candidate.get("candidate_fingerprint"),
    )


async def apply_result(
    topology: OutputTopology, profile: Mapping[str, Any],
    *, apply_state: DspApplyState, error: DspApplyError | None = None,
) -> dict[str, Any]:
    state = apply_state.to_dict()
    graph_fingerprint = (profile.get("source") or {}).get("fingerprint")
    if error is not None:
        target = baseline_profile_state_path()
        previous = applied_profile_anchor(load_baseline_profile_state())
        profile = {**profile, "status": "apply_failed", "apply": state, "updated_at": _utc_now(),
                   "permissions": {"may_apply": False},
                   "issues": [*profile.get("issues", []), _issue("blocker", "baseline_profile_apply_failed", str(error))]}
        if previous is not None:
            profile["applied_recomposition_profile"] = previous
        atomic_write_text(target, json.dumps(profile, indent=2, sort_keys=True) + "\n", mode=0o640, durable=True)
        # See docs/active-crossover-information-design.md: this event includes failed rollbacks.
        log_event(
            logger, "correction.crossover_apply_rolled_back", topology_id=topology.topology_id,
            graph_fingerprint=graph_fingerprint, reason=str(error),
            rollback_attempted=apply_state.rollback_attempted, rollback_succeeded=apply_state.rollback_succeeded,
            rollback_error=apply_state.rollback_error,
        )
    else:
        log_event(
            logger, "correction.crossover_apply_succeeded", topology_id=topology.topology_id,
            tuning_owner=profile.get("tuning_owner"), graph_fingerprint=graph_fingerprint,
            candidate_fingerprint=profile["candidate_fingerprint"], applied_fingerprint=profile["candidate_fingerprint"],
            applied_at=profile["applied_at"],
        )
        linearization = profile.get("linearization") or {}
        log_event(logger, "dsp.baseline_linearization", topology_id=topology.topology_id,
                  **({role: len(filters) for role, filters in linearization.items()} if linearization else {"none": True}))
    return {"status": profile["status"], "profile": profile, "apply": state, "issues": profile.get("issues", [])}


def persist_applied_baseline_profile(
    candidate: Mapping[str, Any], *, apply_state: Mapping[str, Any],
    state_path: str | Path | None = None, applied_at: str | None = None,
) -> dict[str, Any]:
    if apply_state.get("result") != "success":
        raise ValueError("successful apply proof is required")
    bank_applied_base_trim(candidate)
    target = baseline_profile_state_path(state_path)
    existing = load_baseline_profile_state(target)
    identity = baseline_candidate_fingerprint(candidate)
    if (existing and existing.get("status") == "applied"
            and baseline_candidate_fingerprint(existing) == identity
            and existing.get("config") == candidate.get("config")
            and existing.get("timing") == candidate.get("timing")):
        return existing
    now = applied_at or _utc_now()
    applied = {**candidate, "status": "applied", "applied_at": now, "updated_at": now,
               "apply": dict(apply_state), "candidate_fingerprint": identity,
               "permissions": {"may_apply": False}}
    applied.pop("applied_recomposition_profile", None)
    atomic_write_text(target, json.dumps(applied, indent=2, sort_keys=True) + "\n", mode=0o640, durable=True)
    return applied


# Newest-by-mtime content-addressed candidate siblings to keep around a
# canonical baseline config on every successful promote. Orphaned candidates
# accumulate forever now that promotion is a byte COPY, never a move/rename
# (a fleet Pi was observed carrying 38 of them); this is a bounded-I/O
# resilience floor, not a tunable, so it is a plain constant rather than an
# env override.
_MAX_BASELINE_CANDIDATE_FILES = 20


def promote_applied_baseline_candidate(applied: Mapping[str, Any]) -> None:
    """Publish a just-applied candidate's bytes as the canonical config file.

    Reviewed candidates use content-addressed siblings of ``baseline_config_path()``.
    Each candidate lands on its own
    content-addressed sibling, so a candidate that fails validation or
    activation can never appear at the canonical name. This is the ONLY
    place that publishes to that name, and every caller runs it AFTER its own
    ``apply_dsp_config`` + ``persist_applied_baseline_profile`` have already
    proven ``applied`` is the new applied truth -- the copy is durability
    convenience for readers of the canonical name (CamillaDSP's own
    statefile already self-persists the running path independently; the
    multiroom follower fallback in ``jasper.multiroom.follower_config``,
    jasper-doctor, and a human inspecting the box are what this serves), not
    part of the apply decision.

    Fail-soft by design: the running CamillaDSP graph and the JSON SSOT
    (``config.path``, which keeps the truthful applied sibling path -- this
    promotes a COPY, it never rewrites the SSOT) are already correct by the
    time this runs, so a copy failure must never fail an otherwise-successful
    apply. jasper-doctor's baseline-canonical check discloses a stale or
    missing canonical file as an `ok` row, never a service disruption.
    """

    applied_path_raw = (applied.get("config") or {}).get("path")
    if not applied_path_raw:
        return
    applied_path = Path(str(applied_path_raw))
    canonical = baseline_config_path()
    if applied_path == canonical:
        return
    try:
        text = applied_path.read_text(encoding="utf-8")
        atomic_write_text(canonical, text, mode=0o640)
    except (OSError, UnicodeError) as exc:
        # UnicodeError (read_text can raise UnicodeDecodeError on a
        # corrupted-but-present sibling) is a ValueError, not an OSError --
        # it must be caught here too, or a copy failure could raise out of
        # this "must never fail an otherwise-successful apply" boundary.
        log_event(
            logger,
            "dsp.baseline_promote",
            level=logging.WARNING,
            result="failed",
            reason=str(exc),
            candidate_path=applied_path,
            canonical_path=canonical,
        )
        return
    _prune_baseline_candidate_siblings(canonical, protect=applied_path)


def _prune_baseline_candidate_siblings(
    canonical: Path, *, protect: Path,
) -> None:
    """Bound unconditional candidate-sibling growth to the newest K by mtime.

    Deletes ``<stem>_candidate_*<suffix>`` files beside ``canonical`` beyond
    the newest :data:`_MAX_BASELINE_CANDIDATE_FILES` by mtime. Never deletes
    the PROTECTED sibling — ``protect``, the candidate
    :func:`promote_applied_baseline_candidate` just promoted, always the new
    applied anchor — or the canonical file itself (which never matches the
    glob below; it carries no ``_candidate_`` suffix). A displaced sibling
    needs no protection: apply re-emits the banked candidate
    artifact, never a pruned file.

    BLAST RADIUS, stated because the code cannot show it: since #2572 the
    CamillaDSP statefile may durably name a candidate sibling. A deploy's
    reconcile no longer rewrites a content-identical graph to
    ``sound_current.yml``, so a kept correction stays the running config under
    its own candidate name — and pruning that file would leave the statefile
    pointing at nothing, i.e. CamillaDSP failing to start. Safe today only
    because the one caller is the promote path, which always passes the live
    anchor as ``protect``. A future caller, or a prune that stops honouring
    ``protect``, inherits "CamillaDSP starts" as a consequence.
    """

    pattern = f"{canonical.stem}_candidate_*{canonical.suffix}"
    try:
        siblings = list(canonical.parent.glob(pattern))
        prunable = sorted(
            (p for p in siblings if p != protect),
            key=lambda p: p.stat().st_mtime_ns,
            reverse=True,
        )
        # The protected sibling always survives and costs one of the K slots,
        # so the on-disk total stays at K.
        protected_present = sum(1 for p in siblings if p == protect)
        keep = max(0, _MAX_BASELINE_CANDIDATE_FILES - protected_present)
        for stale in prunable[keep:]:
            stale.unlink()
    except OSError as exc:
        log_event(
            logger,
            "dsp.baseline_candidate_prune",
            level=logging.WARNING,
            result="failed",
            reason=str(exc),
            canonical_path=canonical,
        )
