# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Bank one live commissioning session into the on-box campaign home.

``scripts/bank-crossover-round.sh`` banks a round onto a LAPTOP: every artifact
it assembles is pulled over ssh. This is the same tree, assembled on the box
itself, so a round outlives session retention with no laptop in the loop
(#3498, #2882).

The tree is exactly the one
:func:`~jasper.active_speaker.crossover_v2.round_views.load_banked_round`
reads::

    <campaign-root>/<round-id>/
      bundle/<session-id>/...    the live session bundle, copied
      state.json                 crossover-v2 flow state (optional)
      design-draft.json          active-speaker design draft (optional)
      applied-profile.json       applied baseline profile SSOT (optional)
      provenance.json            when it was banked, off which build

**Nothing here evicts.** The campaign store is operator-pruned: no enforced
budget, size disclosed by ``jasper-doctor``'s active-speaker storage check — so
the tuning plan's retention rule (eviction never crosses an active round's
boundary, docs/tuning-master-plan.md) holds trivially.

The flow-state SSOT constant and the round-artifact reader are imported inside
the two functions that need them: ``jasper.cli.doctor`` imports this module for
:data:`DEFAULT_CAMPAIGN_ROOT` alone, and a directory-size disclosure must not
pay the wizard stack's (or numpy's) import cost.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .baseline_profile import DEFAULT_STATE_PATH as _APPLIED_PROFILE_PATH
from .bundles import _detect_build_sha
from .design_draft import DEFAULT_DESIGN_DRAFT_PATH

__all__ = [
    "DEFAULT_CAMPAIGN_ROOT",
    "REASON_ALREADY_BANKED",
    "REASON_NOT_A_BUNDLE",
    "RoundBankError",
    "bank_round",
]

#: The on-box campaign home: banked rounds, one directory each. A sibling of
#: ``bundles.DEFAULT_SESSIONS_DIR`` rather than a child of it, so session
#: retention (``bundles.enforce_retention``) never walks over a banked round.
DEFAULT_CAMPAIGN_ROOT = Path("/var/lib/jasper/active_speaker/campaigns")

REASON_NOT_A_BUNDLE = "not_a_bundle"
REASON_ALREADY_BANKED = "already_banked"


class RoundBankError(Exception):
    """A session could not be banked; ``reason`` is the machine-readable slug."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


def _ssot_documents(
    state_path: Path | None,
    design_draft_path: Path | None,
    applied_profile_path: Path | None,
) -> tuple[tuple[str, Path], ...]:
    """``(banked filename, source path)`` for the three documents beside the
    bundle, defaulting to each document's own on-box SSOT constant.

    The filenames are the ones ``load_banked_round`` opens;
    ``tests/test_round_bank.py`` round-trips an assembled tree through that
    reader, so writer and reader cannot drift apart silently.
    """
    from jasper.web.correction_crossover_v2 import DEFAULT_V2_STATE_PATH

    return (
        ("state.json", state_path or DEFAULT_V2_STATE_PATH),
        ("design-draft.json", design_draft_path or DEFAULT_DESIGN_DRAFT_PATH),
        ("applied-profile.json", applied_profile_path or _APPLIED_PROFILE_PATH),
    )


def _round_id(session_dir: Path, session_id: str) -> str:
    """The bundle's own ``round_id`` when it banked a receipt, else its session id.

    The receipt is located with ``round_artifact_dir`` — the same public reader
    every other producer and consumer of that directory uses — so the receipt a
    packet would be built from is the one read here. A ``round_id`` names a
    directory under the campaign root, so one that is not a single path segment
    falls back to the session id rather than banking outside the store.
    """
    from .crossover_v2.evidence_packet import round_artifact_dir

    round_dir, _why = round_artifact_dir(session_dir)
    if round_dir is None:
        return session_id
    try:
        receipt = json.loads(
            (round_dir / "round_receipt.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return session_id
    candidate = receipt.get("round_id") if isinstance(receipt, Mapping) else None
    if isinstance(candidate, str) and candidate == Path(candidate).name != "":
        return candidate
    return session_id


def bank_round(
    session_dir: Path,
    *,
    campaign_root: Path = DEFAULT_CAMPAIGN_ROOT,
    state_path: Path | None = None,
    design_draft_path: Path | None = None,
    applied_profile_path: Path | None = None,
) -> Path:
    """Copy one live session bundle and its SSOT documents into the campaign home.

    Returns the banked round directory, and writes ``provenance.json`` beside
    the bundle naming when it was banked, which session it came from, and the
    installed build's SHA (``None`` with ``git_absent`` when the box records
    none).

    Raises :class:`RoundBankError` with ``reason`` :data:`REASON_NOT_A_BUNDLE`
    (no readable ``info.json``) or :data:`REASON_ALREADY_BANKED` — a banked
    round is never overwritten. An absent SSOT document is skipped and named in
    ``provenance.json``'s ``missing``, because a partially banked round is a
    normal thing to want to read (``build_crossover_evidence_packet``'s own
    posture).
    """
    session_dir = Path(session_dir)
    try:
        info: Any = json.loads(
            (session_dir / "info.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise RoundBankError(
            REASON_NOT_A_BUNDLE, f"{session_dir}: no readable info.json ({exc})"
        ) from exc
    if not isinstance(info, Mapping):
        raise RoundBankError(
            REASON_NOT_A_BUNDLE, f"{session_dir}: info.json is not a JSON object"
        )
    session_id = str(info.get("session_id") or session_dir.name)
    target = Path(campaign_root) / _round_id(session_dir, session_id)
    if target.exists():
        raise RoundBankError(REASON_ALREADY_BANKED, f"{target} is already banked")

    documents = _ssot_documents(state_path, design_draft_path, applied_profile_path)
    target.mkdir(parents=True)
    try:
        shutil.copytree(session_dir, target / "bundle" / session_dir.name)
        missing: list[str] = []
        for name, source in documents:
            if source.is_file():
                shutil.copy2(source, target / name)
            else:
                missing.append(name)
        sha = _detect_build_sha()
        (target / "provenance.json").write_text(
            json.dumps(
                {
                    "banked_at": datetime.now(timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                    "session_id": session_id,
                    "source": "on-box",
                    "installed_sha": sha,
                    "git_absent": sha is None,
                    "missing": missing,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError:
        # Never leave a half-assembled round where a reader would find one.
        shutil.rmtree(target, ignore_errors=True)
        raise
    return target
