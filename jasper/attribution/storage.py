# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Where findings live: inside the session bundle, for exactly as long as it.

Bundle-lifetime retention (Q-C, #1866), implemented by choosing the container
rather than writing a retention loop: a finding set is one ordinary artifact in
the commissioning evidence bundle, which ``bundles.enforce_retention`` evicts
whole. :func:`read_finding_set` re-resolves and re-hashes every bundle citation
by default and raises rather than returning a finding whose support it could
not confirm. An absent artifact returns ``None`` (a legacy bundle); a present
one with an empty list means attribution ran and found nothing.
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

    Raised when a citation's artifact is gone, unreadable, or no longer hashes
    to what the finding recorded — never swallowed.
    """


def findings_relative_path(relay_session_id: str, phase: str) -> str:
    """The **publish** path for one phase's finding set.

    Per phase, not per session: the pre-apply and post-apply groups close at
    different times and the store is write-once, so a shared path would
    collide. ``publish_json_artifact`` takes this SHORT path and prefixes it
    with its artifact namespace itself; :func:`findings_artifact_path` is the
    full bundle-relative form every reader and citation uses.
    """

    if not relay_session_id or not phase:
        raise FindingStorageError("relay_session_id and phase are required")
    return f"crossover_v2/{relay_session_id}/findings_{phase}.json"


def findings_artifact_path(relay_session_id: str, phase: str) -> str:
    """The full bundle-relative path — what a reader and a citation use.

    The namespace prefix is imported from the store rather than spelled here,
    so a publish that succeeds and a read that reports "no findings" cannot
    drift apart.
    """

    from jasper.active_speaker.commissioning_evidence_store import EVIDENCE_ROOT

    return f"{EVIDENCE_ROOT}/artifacts/{findings_relative_path(relay_session_id, phase)}"


def bundle_evidence_ref(artifact: Any, session: SessionIdentity) -> EvidenceRef:
    """An :class:`EvidenceRef` for one published bundle artifact.

    The locator and digest are the store's own, from the ``ArtifactIdentity``
    its publish returned; re-deriving them here would be a second computation
    of the one fact that makes a citation checkable.
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
    :class:`FindingStorageError` on any store refusal — the fail-soft boundary
    belongs at the caller.
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

    ``None`` is the legacy/absent answer and is not an error.
    ``verify_evidence`` defaults to True: every bundle citation is re-resolved
    and re-hashed, and one that cannot be confirmed raises
    :class:`FindingEvidenceMissing`. Pass False only for a caller that wants
    the record without its support.
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

    Only ``commissioning_bundle`` citations are checked: the bundle is the one
    store whose lifetime is bound to the finding's. A citation into the capture
    ring or the laptop archive is a cross-store join whose absence says nothing
    about support. ``Finding`` guarantees at least one bundle citation.
    """

    # Every finding a group produces cites the SAME cloud artifact, so memoize
    # by locator: a write-once artifact's digest cannot change underneath one
    # verification pass, and the read is what costs.
    digests: dict[str, str] = {}
    for finding in finding_set.findings:
        for cite in finding.cites:
            if cite.store != EVIDENCE_STORE_BUNDLE:
                continue
            digest = digests.get(cite.locator)
            if digest is None:
                try:
                    digest = str(store.identify_artifact(cite.locator).sha256)
                except (OSError, RuntimeError, TypeError, ValueError) as exc:
                    raise FindingEvidenceMissing(
                        f"{finding.mechanism} cites {cite.locator!r}, which this "
                        f"bundle can no longer produce: {exc}"
                    ) from exc
                digests[cite.locator] = digest
            if cite.sha256 and digest != cite.sha256:
                raise FindingEvidenceMissing(
                    f"{finding.mechanism} cites {cite.locator!r} at "
                    f"{cite.sha256[:12]}…, but the bundle now holds "
                    f"{digest[:12]}…"
                )


def _is_missing(exc: BaseException) -> bool:
    """Is this store failure "the artifact is not there" rather than a fault?

    Matched on the store's stable ``MISSING`` error code with a
    ``FileNotFoundError`` fallback for test doubles. An unreadable or oversized
    artifact must not be mistaken for a legacy bundle.
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
