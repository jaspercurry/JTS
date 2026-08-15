# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What an apply displaced, what it put live, and whether it still is (#2537).

**One fact, one owner, one round.** "The previous sound" had two owners that
could silently diverge — the global applied-baseline-profile record, which the
crossover-v2 apply/restore path reads, and the saved sound intent, which
``jasper-sound reconcile-current-dsp`` renders. Any out-of-band restore updates
one and not the other, and the next in-band restore then resurrects a stale
sound with full confidence.

It did, on 2026-08-15 (jts3 cycle 4):

1. 00:34 — run 2's candidate applied; the profile record names it as the
   reigning sound.
2. 01:00 — an operator reconciles the RUNNING config to ``sound_current.yml``.
   The record does not move, because reconcile re-renders from sound intent and
   is (correctly) ignorant of the active-speaker profile system.
3. 07:30 — cycle 4's round restores "the previous sound" per the record, and
   faithfully puts back run 2's candidate: a graph the speaker had not played
   for six and a half hours. The restore MECHANICS were proven good that night;
   the TARGET was wrong.

This module is the fix's shape: **the round snapshots the identity of the graph
it displaced, at the moment it displaced it**, and keeps that snapshot in its
own durable state. Round N+1's apply then snapshots round N's applied state
automatically, which is exactly the chain the owner's iterate-and-learn series
needs — "round N+1's previous is exactly round N's result" — and which a single
global record structurally cannot promise, because it has no idea a round is
happening.

**It adds a reader, never a second writer.** ``reconcile-current-dsp`` and every
other out-of-band path stay entirely ignorant of the active-speaker profile
system; that ignorance is the separation of concerns that makes them safe to
run, and #2537's option (a) — teaching reconcile to retire the profile record —
would have traded it away. The record's only writer remains the apply path. What
changes is that the restore stops assuming it won a race it never entered.

Pure of the host: no ``jasper.web`` import, nothing from
:mod:`jasper.active_speaker.crossover_v2_flow`, no clock, no logging, and no
CamillaDSP handle — the live "what is playing right now" reading is a
*parameter*, because taking it is an async round trip and deciding when to take
it is the host's call. Two file digests are read, which is the one impurity and
an unavoidable one: "the same filename" and "the same bytes" are different
questions, and only the second is safe to act on.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from jasper.dsp_apply import config_file_sha256, same_config_file

__all__ = [
    "ROUND_ANCHOR_STATE_KEY",
    "round_anchor_record",
    "running_config_diverged",
]

#: The durable v2-state key this module's record lives under. Named here
#: because this module owns the record's shape; the host owns writing it.
ROUND_ANCHOR_STATE_KEY = "round_anchor"


def round_anchor_record(payload: Mapping[str, Any]) -> dict[str, Any]:
    """What one apply DISPLACED and what it PUT LIVE, from its own transaction.

    ``payload`` is
    :func:`~jasper.active_speaker.baseline_profile.apply_baseline_profile`'s
    result. Both halves are read straight off it rather than re-derived:

    * ``displaced`` — ``DspApplyState.prior_config_path``, what CamillaDSP
      reported as the running config immediately before the load. That is the
      RUNNING graph at apply moment *by construction*, and it is the only
      reading nothing out-of-band can have moved yet.
    * ``applied`` — the profile's own ``config.path``/``sha256``, the file this
      apply put live.

    Each carries a digest so a later reader can tell "the same file" from "the
    same filename": ``build_baseline_profile_candidate`` names candidates from a
    SOURCE fingerprint that deliberately does not cover every input reaching the
    emitted bytes, so a same-fingerprint recompile can rewrite a file under its
    own name — the live condition
    ``restore_applied_baseline_profile``'s ``restore_target_changed`` exists
    for. ``""`` for a digest that could not be read: an absence, which
    :func:`running_config_diverged` treats as "cannot compare" and never as a
    mismatch.

    Returns both halves unconditionally, including as empty identities. An
    absent key would be a second way of saying "unknown" alongside the empty
    string, on a record a human reads off durable state.
    """

    apply_state = payload.get("apply")
    profile = payload.get("profile")
    config = profile.get("config") if isinstance(profile, Mapping) else None
    return {
        "displaced": _identity(
            apply_state.get("prior_config_path")
            if isinstance(apply_state, Mapping) else None
        ),
        "applied": _identity(
            config.get("path") if isinstance(config, Mapping) else None,
            config.get("sha256") if isinstance(config, Mapping) else None,
        ),
    }


def _identity(path: Any, sha256: Any = None) -> dict[str, str]:
    """One config file as ``{"config_path", "sha256"}``, digest read if absent."""

    text = str(path or "")
    if not text:
        return {"config_path": "", "sha256": ""}
    digest = str(sha256 or "") or (config_file_sha256(text) or "")
    return {"config_path": text, "sha256": digest}


def running_config_diverged(
    anchor: Mapping[str, Any] | None, running_config_path: str | None
) -> bool:
    """Is the RUNNING graph something other than the one this round applied?

    The check half of the check-then-act a restore is. Every existing rollback
    precondition asks about the ANCHOR — is one stashed, is its topology still
    current, do its bytes still match — and none of them asks whether the graph
    about to be REPLACED is the one this round put there. That gap is the whole
    of the cycle-4 defect above.

    ``False`` for every way the question cannot be answered — no live reading, a
    live reading naming no file on disk, no anchor, an anchor with no applied
    path — because "we could not compare" must never be reported as "it moved".
    Only a positive path-or-digest disagreement is a divergence.

    The is-a-file screen is not belt-and-braces. ``CamillaClient``'s
    ``get_config_file_path`` wraps its websocket answer in ``str()``, so a
    ``None`` from the socket arrives as the literal string ``"None"``, and
    comparing that against a real path would refuse a perfectly good Undo. A
    path CamillaDSP is genuinely playing exists.

    Both facts are compared and both are needed. The PATH catches the shape that
    produced this check — a reconcile pointing CamillaDSP at a different file.
    The DIGEST catches its sibling: the same filename carrying different bytes
    after a same-fingerprint recompile, asked here about the graph being
    REPLACED rather than the one being reloaded. A digest missing on either side
    is an absence, so the path comparison stands alone.

    Reports; it does not decide. What a divergence MEANS — refuse, re-anchor,
    warn — is the caller's, and the caller that acts on it
    (``rollback_anchor_refusal``) refuses, because this module knows the running
    graph is not the round's and cannot know what a household would want done
    about that. Stomping a config an operator deliberately reconciled to is the
    one outcome nobody asked for.
    """

    if not running_config_path:
        return False
    try:
        if not Path(running_config_path).is_file():
            return False
    except (OSError, ValueError):
        return False
    applied = anchor.get("applied") if isinstance(anchor, Mapping) else None
    if not isinstance(applied, Mapping):
        return False
    recorded_path = str(applied.get("config_path") or "")
    if not recorded_path:
        return False
    if not same_config_file(running_config_path, recorded_path):
        return True
    recorded_sha = str(applied.get("sha256") or "")
    live_sha = config_file_sha256(running_config_path) or ""
    return bool(recorded_sha and live_sha and recorded_sha != live_sha)
