# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Bank one live commissioning session into the on-box campaign home.

The same tree ``scripts/bank-crossover-round.sh`` assembles on a laptop, built
on the box itself so a round outlives session retention (#3498, #2882). It is
exactly the tree
:func:`~jasper.active_speaker.crossover_v2.round_views.load_banked_round`
reads::

    <campaign-root>/<round-id>/
      bundle/<session-id>/...    the live session bundle, hard-linked
      state.json                 crossover-v2 flow state (optional)
      design-draft.json          active-speaker design draft (optional)
      applied-profile.json       applied baseline profile SSOT (optional)
      repeat-floor.json          measured repeat floor SSOT (optional)
      declared-geometry.json     declared rig geometry SSOT (optional)
      provenance.json            when it was banked, off which build

``provenance.json``'s key set is owned here: ``banked_at_utc`` is spelled and
formatted as ``scripts/bank-crossover-round.sh`` writes it, and each path adds
only what it alone knows. Nothing here evicts — the campaign store is
operator-pruned.

The banked names and their SSOT paths belong to the reader
(:mod:`~jasper.active_speaker.crossover_v2.round_inputs`) and are imported
inside the function that needs them, so importing this module for
:data:`DEFAULT_CAMPAIGN_ROOT` alone stays cheap.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, NamedTuple

from .bundles import _UNFINISHED_STATES, _detect_build_sha
# Reused rather than a third package-local identifier regex; its first-char
# class excludes ".", so it rejects ".", ".." and any "/"-carrying token.
from .commissioning_run import _IDENTIFIER_RE as _ROUND_ID_RE

__all__ = [
    "DEFAULT_CAMPAIGN_ROOT",
    "REASON_ALREADY_BANKED",
    "REASON_NOT_A_BUNDLE",
    "REASON_SESSION_UNFINISHED",
    "BankedRound",
    "RoundBankError",
    "bank_round",
]

#: The on-box campaign home: banked rounds, one directory each. A sibling of
#: ``bundles.DEFAULT_SESSIONS_DIR`` rather than a child of it, so session
#: retention (``bundles.enforce_retention``) never walks over a banked round.
DEFAULT_CAMPAIGN_ROOT = Path("/var/lib/jasper/active_speaker/campaigns")

REASON_NOT_A_BUNDLE = "not_a_bundle"
REASON_ALREADY_BANKED = "already_banked"
REASON_SESSION_UNFINISHED = "session_unfinished"


class BankedRound(NamedTuple):
    """What :func:`bank_round` assembled: the round directory and the exact
    ``provenance.json`` payload written into it (so a caller never re-reads a
    file it just wrote)."""

    path: Path
    provenance: dict[str, Any]


class RoundBankError(Exception):
    """A session could not be banked; ``reason`` is the machine-readable slug."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


def _ssot_documents(
    state_path: Path | None,
    design_draft_path: Path | None,
    applied_profile_path: Path | None,
    repeat_floor_path: Path | None,
    declared_geometry_path: Path | None,
) -> tuple[tuple[str, Path], ...]:
    """``(banked filename, source path)`` for the five documents beside the
    bundle, defaulting to each document's own on-box SSOT constant.

    Both halves come from ``round_inputs``, the reader that opens them, so
    writer and reader cannot drift apart silently.
    """
    from .crossover_v2 import round_inputs as reader

    return (
        (reader.STATE_FILENAME, state_path or reader.STATE_DEFAULT_PATH),
        (
            reader.DESIGN_DRAFT_FILENAME,
            design_draft_path or reader.DRIVERS_DEFAULT_PATH,
        ),
        (
            reader.APPLIED_PROFILE_FILENAME,
            applied_profile_path or reader.APPLIED_PROFILE_DEFAULT_PATH,
        ),
        (
            reader.REPEAT_FLOOR_FILENAME,
            repeat_floor_path or reader.REPEAT_FLOOR_DEFAULT_PATH,
        ),
        (
            reader.DECLARED_GEOMETRY_FILENAME,
            declared_geometry_path or reader.DECLARED_GEOMETRY_DEFAULT_PATH,
        ),
    )


#: A campaign root on another filesystem (EXDEV), or a non-root operator
#: hitting Raspberry Pi OS's fs.protected_hardlinks=1 (EPERM/EACCES: linking
#: requires being root, the file's owner, or write access to it) — both fall
#: back to a real copy rather than failing the bank.
_LINK_FALLBACK_ERRNOS = (errno.EXDEV, errno.EPERM, errno.EACCES)


def _link_or_copy(source: str, destination: str) -> None:
    """Hard-link the banked bundle instead of copying its bytes.

    The source session bundle is immutable once banked, so a hard link is
    safe and shares bytes with the (later-unlinked) sessions-ring copy; see
    :data:`_LINK_FALLBACK_ERRNOS` for when a real copy is used instead.
    """
    try:
        os.link(source, destination)
    except OSError as exc:
        if exc.errno not in _LINK_FALLBACK_ERRNOS:
            raise
        shutil.copy2(source, destination)


def _round_id(session_dir: Path, session_id: str) -> str:
    """The bundle's own ``round_id`` when it banked a receipt, else its session id.

    Located with the public ``round_artifact_dir``, so the receipt a packet
    would be built from is the one read here. A ``round_id`` that is not a plain
    :data:`_ROUND_ID_RE` token falls back to the session id rather than banking
    outside the store.
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
    if isinstance(candidate, str) and _ROUND_ID_RE.fullmatch(candidate):
        return candidate
    return session_id


def bank_round(
    session_dir: Path,
    *,
    campaign_root: Path = DEFAULT_CAMPAIGN_ROOT,
    state_path: Path | None = None,
    design_draft_path: Path | None = None,
    applied_profile_path: Path | None = None,
    repeat_floor_path: Path | None = None,
    declared_geometry_path: Path | None = None,
) -> BankedRound:
    """Bank one live session bundle and its SSOT documents into the campaign home.

    The bundle is hard-linked in, not copied byte-for-byte (falling back to a
    copy only across a filesystem boundary) — see :func:`_link_or_copy`.

    Returns the banked round directory and the ``provenance.json`` payload
    written beside the bundle: when it was banked, which session it came from,
    and the installed build's SHA (``None`` with ``git_absent`` when the box
    records none).

    Raises :class:`RoundBankError` with ``reason`` :data:`REASON_NOT_A_BUNDLE`,
    :data:`REASON_SESSION_UNFINISHED` (banking an ``open``/``proposal_ready``
    session would claim its round id mid-flight, and an id is never re-banked)
    or :data:`REASON_ALREADY_BANKED` — a banked round is never overwritten. An
    absent SSOT document is skipped and named in ``provenance.json``'s
    ``missing``: a partially banked round is a normal thing to read.

    A filesystem failure is not a refusal: the :class:`OSError` propagates, so
    the CLI exits on its filesystem-failure code rather than telling the
    operator this was not a bundle.
    """
    session_dir = Path(session_dir)
    try:
        info: Any = json.loads(
            (session_dir / "info.json").read_text(encoding="utf-8")
        )
    except (FileNotFoundError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RoundBankError(
            REASON_NOT_A_BUNDLE, f"{session_dir}: no readable info.json ({exc})"
        ) from exc
    if not isinstance(info, Mapping):
        raise RoundBankError(
            REASON_NOT_A_BUNDLE, f"{session_dir}: info.json is not a JSON object"
        )
    if info.get("state") in _UNFINISHED_STATES:
        raise RoundBankError(
            REASON_SESSION_UNFINISHED,
            f"{session_dir}: session state is {info.get('state')!r}; "
            "bank it once the session has finished",
        )
    session_id = str(info.get("session_id") or session_dir.name)
    target = Path(campaign_root) / _round_id(session_dir, session_id)
    if target.exists():
        raise RoundBankError(REASON_ALREADY_BANKED, f"{target} is already banked")

    documents = _ssot_documents(
        state_path,
        design_draft_path,
        applied_profile_path,
        repeat_floor_path,
        declared_geometry_path,
    )
    target.mkdir(parents=True)
    try:
        shutil.copytree(
            session_dir,
            target / "bundle" / session_dir.name,
            copy_function=_link_or_copy,
        )
        missing: list[str] = []
        for name, source in documents:
            if source.is_file():
                shutil.copy2(source, target / name)
            else:
                missing.append(name)
        sha = _detect_build_sha()
        provenance: dict[str, Any] = {
            "banked_at_utc": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "session_id": session_id,
            "source": "on-box",
            "installed_sha": sha,
            "git_absent": sha is None,
            "missing": missing,
        }
        (target / "provenance.json").write_text(
            json.dumps(provenance, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError:
        # Never leave a half-assembled round where a reader would find one.
        shutil.rmtree(target, ignore_errors=True)
        raise
    return BankedRound(target, provenance)
