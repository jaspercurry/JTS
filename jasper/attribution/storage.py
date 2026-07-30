# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Where findings live: inside the session bundle, for exactly as long as it.

**Q-C, ruled by the owner 2026-07-29 night (issue #1866): bundle-lifetime
retention.** "A finding lives inside the session bundle whose evidence it
cites and dies with it — structurally satisfying the plan's pinned constraint
(a finding must not outlive, or silently predecease, the evidence it cites).
Longitudinal analysis reads across bundles via WO-1's stable cross-store
session identity. A summary ledger for long-horizon retrospectives is
explicitly deferred to a later WO."

The ruling is implemented by *choosing the right container*, not by writing a
retention loop. A finding set is published as one ordinary artifact in the
commissioning evidence bundle, so:

* **It cannot outlive its evidence.** ``bundles.enforce_retention`` evicts a
  bundle with ``shutil.rmtree`` — the whole directory, JSON and WAVs
  together. There is no per-file tier that could take the evidence and leave
  the finding. This half needs no code at all, which is the point: a
  retention rule that is a consequence of the storage shape cannot drift from
  it. It is pinned by test anyway, because "no code" is exactly the kind of
  guarantee that quietly stops being true when someone adds a pruner.
* **It cannot silently predecease its evidence.** Nothing prunes *inside* a
  living bundle today, but a finding that cited a vanished or altered
  artifact would still be the more dangerous failure — a diagnosis that reads
  fine and rests on nothing. So :func:`read_finding_set` re-resolves and
  re-hashes every bundle citation by default and raises
  :class:`FindingEvidenceMissing` rather than returning a finding whose
  support it could not confirm.

**Provenance marker discipline** (§7's acceptance: "old bundles without
findings readable"). Absence of the artifact is a *state*, not an error:
:func:`read_finding_set` returns ``None`` for every bundle written before
attribution existed. An artifact that is present but carries an empty
``findings`` list is the different, meaningful statement that attribution ran
and found nothing. The two are never conflated, which is what lets a reader
distinguish "this speaker is clean" from "nobody looked".

**Write-once is inherited, deliberately.** ``CommissioningEvidenceStore``
publishes canonical JSON at a path that may never be rewritten with different
bytes. A finding set is produced once, at group close, from evidence that is
itself already write-once — so the constraint costs nothing and buys
tamper-evidence for free.
"""

from __future__ import annotations

from typing import Any

from .findings import (
    EVIDENCE_STORE_BUNDLE,
    EvidenceRef,
    FindingError,
    FindingSet,
)
from .session_identity import SessionIdentity


class FindingStorageError(RuntimeError):
    """A finding set could not be published or reopened."""


class FindingEvidenceMissing(FindingStorageError):
    """A persisted finding cites evidence the bundle can no longer produce.

    The loud half of the bundle-lifetime rule. Raised when a citation's
    artifact is gone, unreadable, or no longer hashes to what the finding
    recorded — never swallowed, because the failure mode this prevents is a
    diagnosis that still reads convincingly after its support disappeared.
    """


def findings_relative_path(relay_session_id: str, phase: str) -> str:
    """The **publish** path for one phase's finding set.

    Per phase, not per session, for the same reason
    ``bind_cloud_publisher`` writes ``cloud_measure.json`` and
    ``cloud_verify.json`` separately: the pre-apply and post-apply groups
    close at genuinely different times in one session, and the store is
    write-once, so a shared path would collide on the second close.

    Note the store's own path asymmetry, which is easy to get wrong:
    ``publish_json_artifact`` takes this *short* path and prefixes it with
    its artifact namespace itself, while ``identify_artifact`` and every
    ``ArtifactIdentity.relative_path`` use the *full* bundle-relative one.
    :func:`findings_artifact_path` is the full form.
    """

    if not relay_session_id or not phase:
        raise FindingStorageError("relay_session_id and phase are required")
    return f"crossover_v2/{relay_session_id}/findings_{phase}.json"


def findings_artifact_path(relay_session_id: str, phase: str) -> str:
    """The full bundle-relative path — what a reader and a citation use.

    The namespace prefix is imported from the store rather than spelled here,
    so the two cannot drift apart into a publish that succeeds and a read
    that silently reports "no findings" — which is exactly the failure this
    asymmetry produces if the prefix is duplicated.
    """

    from jasper.active_speaker.commissioning_evidence_store import EVIDENCE_ROOT

    return f"{EVIDENCE_ROOT}/artifacts/{findings_relative_path(relay_session_id, phase)}"


def bundle_evidence_ref(artifact: Any, session: SessionIdentity) -> EvidenceRef:
    """An :class:`EvidenceRef` for one published bundle artifact.

    Takes the ``ArtifactIdentity`` the evidence store already returned from
    its publish, so the locator and the digest are the store's own — never
    re-derived here, which would be a second computation of the one fact that
    makes a citation checkable.
    """

    try:
        return EvidenceRef(
            session=session,
            store=EVIDENCE_STORE_BUNDLE,
            locator=str(artifact.relative_path),
            sha256=str(artifact.sha256),
        )
    except (AttributeError, FindingError) as exc:
        raise FindingStorageError(
            f"cannot cite this artifact: {exc}"
        ) from exc


def publish_finding_set(
    store: Any, *, relay_session_id: str, phase: str, finding_set: FindingSet
) -> Any:
    """Publish one phase's findings into the session bundle.

    Returns the store's ``ArtifactIdentity``. Raises
    :class:`FindingStorageError` on any store refusal — this function does
    **not** swallow failures, matching ``bind_cloud_publisher`` and
    ``bind_position_retention``: the fail-soft boundary belongs at the flow
    call site, so every other caller keeps the strictness the store was built
    for.
    """

    if not isinstance(finding_set, FindingSet):
        raise FindingStorageError("finding_set must be a FindingSet")
    path = findings_relative_path(relay_session_id, phase)
    try:
        return store.publish_json_artifact(path, finding_set.to_dict())
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise FindingStorageError(f"could not publish findings: {exc}") from exc


def read_finding_set(
    store: Any,
    *,
    relay_session_id: str,
    phase: str,
    verify_evidence: bool = True,
) -> FindingSet | None:
    """Reopen one phase's findings, or ``None`` if this bundle has none.

    ``None`` is the legacy/absent answer and is not an error — see the
    provenance-marker discipline in this module's docstring.

    ``verify_evidence`` defaults to **True**: every bundle citation is
    re-resolved and re-hashed, and a citation that cannot be confirmed raises
    :class:`FindingEvidenceMissing`. Pass ``False`` only for a caller that
    genuinely wants the record without its support — a listing, an audit of
    what *was* written — and that is prepared to say so.
    """

    path = findings_artifact_path(relay_session_id, phase)
    try:
        artifact = store.identify_artifact(path)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        # The store's own failure type is a RuntimeError subclass carrying a
        # stable code; FileNotFoundError covers a plain test double.
        if _is_missing(exc):
            return None
        raise FindingStorageError(f"could not read findings: {exc}") from exc
    try:
        raw = store.reopen_json_artifact(artifact)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise FindingStorageError(f"could not reopen findings: {exc}") from exc
    try:
        finding_set = FindingSet.from_mapping(raw)
    except FindingError as exc:
        raise FindingStorageError(f"persisted findings are malformed: {exc}") from exc
    if verify_evidence:
        verify_finding_evidence(store, finding_set)
    return finding_set


def verify_finding_evidence(store: Any, finding_set: FindingSet) -> None:
    """Confirm every bundle citation still resolves to the exact cited bytes.

    Only ``commissioning_bundle`` citations are checked, and that asymmetry
    is the design rather than a shortcut: the bundle is the one store whose
    lifetime is bound to the finding's. The capture ring prunes oldest-first
    on its own schedule and the laptop archive is off-device, so a citation
    into either is a cross-store *join*, recorded so the join exists at all,
    and its absence says nothing about whether the finding is still
    supported. ``Finding`` guarantees at least one bundle citation, so every
    finding is covered by this check.
    """

    for finding in finding_set.findings:
        for cite in finding.cites:
            if cite.store != EVIDENCE_STORE_BUNDLE:
                continue
            try:
                identity = store.identify_artifact(cite.locator)
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                raise FindingEvidenceMissing(
                    f"{finding.mechanism} cites {cite.locator!r}, which this "
                    f"bundle can no longer produce: {exc}"
                ) from exc
            if cite.sha256 and str(identity.sha256) != cite.sha256:
                raise FindingEvidenceMissing(
                    f"{finding.mechanism} cites {cite.locator!r} at "
                    f"{cite.sha256[:12]}…, but the bundle now holds "
                    f"{str(identity.sha256)[:12]}…"
                )


def _is_missing(exc: BaseException) -> bool:
    """Is this store failure "the artifact is not there" rather than a fault?

    Matched on the store's own stable error code
    (``CommissioningEvidenceStoreErrorCode.MISSING``) with a
    ``FileNotFoundError`` fallback, so a test double that raises the ordinary
    OS error reads the same way. Anything else is a real failure and stays
    one — an unreadable or oversized artifact must not be mistaken for a
    legacy bundle.
    """

    if isinstance(exc, FileNotFoundError):
        return True
    return str(getattr(exc, "code", "")) == "commissioning_evidence_missing"


__all__ = [
    "FindingEvidenceMissing",
    "FindingStorageError",
    "bundle_evidence_ref",
    "findings_artifact_path",
    "findings_relative_path",
    "publish_finding_set",
    "read_finding_set",
    "verify_finding_evidence",
]
