# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The finding — attribution's unit of diagnosis, as a persisted artifact.

``docs/historical/attribution-stage-plan.md`` §3.1 defines it: ``{mechanism, band,
evidence, confidence, fix_class, household_copy, probes_run,
probes_recommended}``. This module is that artifact, its validation, and its
self-describing serialization. It computes nothing — detectors (WO-4) and the
promotion path (:mod:`jasper.attribution.promotion`) produce findings; this
module only decides whether a produced finding is well-formed.

**Two vocabularies, one artifact** (§3.1). ``mechanism`` is *internal*
taxonomy: it names physics, it may name hardware, and it appears on
ops/forensic surfaces. ``household_copy`` stays **phenomenon-level and
hardware-noun-free**, because the prohibition it inherits is explicit — the
shipped ``_null_classification_copy`` docstring says of its own two branches
that "No hardware noun appears here … naming one would be the device-taxonomy
guess this program forbids in shipped copy". Attribution does not overturn
that, so this module refuses a household string that names one. Whether
household copy may ever become more specific is **Q-F**, gated on P4-class
adjudication, not on this schema.

**Findings are optional evidence artifacts** (§3.4). Nothing is gated on a
structured attribution verdict existing; a session with no findings behaves
exactly as it does today. That is why :class:`FindingSet` may legitimately be
empty and why its *absence* is a first-class readable state rather than an
error — see :mod:`jasper.attribution.storage`.

**Two additions to §3.1's eight fields, both required by §7's acceptance.**
``cites`` carries the id-indexed, hash-verified pointers to the evidence a
finding rests on ("a finding cites its evidence by an id that survives every
hop, not by content hash or directory mtime — WO-0 proved both of those
fail"), and the enclosing :class:`FindingSet` carries the session identity and
a provenance marker. Neither is a diagnosis field; both exist so a finding can
be checked rather than believed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from jasper.json_fields import finite_float

from .closed_sets import CONFIDENCE_TIERS, CONFIDENCE_UNSURE, PROBES
from .mechanisms import MechanismError, mechanism_spec
from .session_identity import SessionIdentity, SessionIdentityError

#: Schema tags. Present in every serialized payload so a reader never has to
#: infer the shape, and so a future incompatible change is detectable.
FINDING_SCHEMA = "jts_attribution_finding/1"
FINDING_SET_SCHEMA = "jts_attribution_finding_set/1"

#: Stores a finding may cite. ``commissioning_bundle`` is the only
#: **lifetime-bound and verifiable** one: Q-C's bundle-lifetime ruling means a
#: bundle citation dies exactly when the finding does, and the evidence store
#: can re-hash it on read. The other two are recorded so a cross-store join
#: exists at all — the capture ring prunes on its own schedule and the laptop
#: archive is off-device, so neither can be verified from here and neither may
#: be a finding's only citation.
EVIDENCE_STORE_BUNDLE = "commissioning_bundle"
EVIDENCE_STORE_CAPTURE_RING = "capture_ring"
EVIDENCE_STORE_LAPTOP_ARCHIVE = "laptop_archive"
EVIDENCE_STORES: tuple[str, ...] = (
    EVIDENCE_STORE_BUNDLE,
    EVIDENCE_STORE_CAPTURE_RING,
    EVIDENCE_STORE_LAPTOP_ARCHIVE,
)

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SNAKE_CASE_RE = re.compile(r"\b[a-z0-9]+(?:_[a-z0-9]+)+\b")
_MECHANISM_ID_RE = re.compile(r"\bM[0-9]{1,2}\b")

# Word-boundary matched, so "important" does not trip "port" and "concern"
# does not trip "cone". Seeded from the device-taxonomy nouns §3.1's cited
# prohibition is about; WO-4 extends the list as its detectors add copy.
#
# Two entries need their scope stated, because neither is obvious from the
# name "hardware noun":
#
# * "subwoofer" is listed SEPARATELY from "woofer" and is not redundant: the
#   word boundary that stops "important" tripping "port" also stops
#   "\bwoofer\b" matching inside "subwoofer". It is a live noun — JTS ships a
#   local-DAC subwoofer output group — so the gap was reachable, not
#   theoretical.
# * "desk" is not hardware at all; it is a *reflector*. It belongs here
#   because the rule §3.1 states is about naming WHAT PRODUCED a feature, not
#   about a parts list: "the desk bounce" is the same device-taxonomy guess as
#   "the horn's rim wave", made about the room instead of the speaker, and a
#   single session licenses neither. Read this list as "nouns that name a
#   cause", which is what the prohibition is actually for.
_HARDWARE_NOUNS: tuple[str, ...] = (
    "horn",
    "tweeter",
    "subwoofer",
    "woofer",
    "midrange",
    "driver",
    "crossover",
    "baffle",
    "cabinet",
    "enclosure",
    "port",
    "cone",
    "dome",
    "diaphragm",
    "waveguide",
    "desk",
    "amplifier",
    "dac",
)
_HARDWARE_NOUN_RE = re.compile(
    r"\b(?:" + "|".join(_HARDWARE_NOUNS) + r")s?\b", re.IGNORECASE
)

_MAX_EVIDENCE_KEYS = 64
_MAX_CITES = 32
_MAX_FINDINGS = 256

#: Inline field descriptions, emitted with every serialized set. §6's
#: requirement: "no code-reading needed to interpret — the v2-state friction".
FINDING_FIELD_DESCRIPTIONS: Mapping[str, str] = MappingProxyType(
    {
        "mechanism": (
            "Internal taxonomy id from the attribution mechanism registry "
            "(plan §4 seed ids, e.g. 'M2'). Names physics; may name hardware. "
            "Ops/forensic surfaces only — never shown to a household."
        ),
        "band_hz": (
            "[f_lo_hz, f_hi_hz] — the frequency range this finding is about, "
            "in hertz, lower edge first."
        ),
        "evidence": (
            "The measured numbers this finding rests on, as a flat object of "
            "finite scalars. Keys are producer-defined and self-describing; "
            "units are named in the key (e.g. 'tau_us', 'depth_db')."
        ),
        "confidence": (
            "One of 'confident' / 'likely' / 'unsure'. A word, never a score "
            "(plan §10: 'No confidence scores — tiers only'). 'unsure' is the "
            "refuse-and-recommend-the-probe outcome and always carries at "
            "least one entry in probes_recommended."
        ),
        "fix_class": (
            "Internal routing class from the closed §3.3 set: delay, "
            "polarity, eq, refit, carve, physical, document_as_physics, "
            "measure_differently. Not a copy vocabulary; where a finding's "
            "consequence is a per-bin correction limit it resolves through "
            "the shipped linearization_envelope.ReasonCode enum."
        ),
        "household_copy": (
            "The sentence a household may be shown. Phenomenon-level and "
            "hardware-noun-free by contract: it describes what was observed, "
            "never which part produced it."
        ),
        "probes_run": (
            "Probe ids (plan §5, P1-P7) whose evidence this finding actually "
            "rests on."
        ),
        "probes_recommended": (
            "Probe ids that would raise this finding's confidence. Advisory "
            "and per-mechanism — not a fixed global discriminator chain."
        ),
        "cites": (
            "Pointers to the evidence, each {session, store, locator, "
            "sha256}. The session identity plus the locator is the INDEX; the "
            "sha256 is only ever the VERIFIER. At least one citation is in "
            "the commissioning bundle, which is what binds a finding's "
            "lifetime to its evidence."
        ),
    }
)

FINDING_SET_FIELD_DESCRIPTIONS: Mapping[str, str] = MappingProxyType(
    {
        "schema": "This payload's schema tag and version.",
        "session": (
            "The measurement session's identity — one identifier that "
            "survives every store hop. See the jts_session_identity key."
        ),
        "produced_by": (
            "Provenance marker: which producer wrote this set. The PRESENCE "
            "of this record is what distinguishes 'attribution ran and found "
            "nothing' (an empty findings list) from 'this bundle predates "
            "attribution' (no record at all)."
        ),
        "findings": "The findings themselves; may legitimately be empty.",
        "field_descriptions": (
            "This object. Inline so the artifact is interpretable without "
            "reading any code."
        ),
    }
)


class FindingError(ValueError):
    """A finding or finding set is malformed."""


def _finite(value: Any, *, field_name: str) -> float:
    number = finite_float(value)
    if number is None:
        raise FindingError(f"{field_name} must be a finite number")
    return number


def _prose(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FindingError(f"{field_name} must be non-empty text")
    return value


@dataclass(frozen=True)
class EvidenceRef:
    """One id-indexed, hash-verified pointer to a finding's evidence.

    The whole point, from §6: **content hashing stays the verifier; it must
    stop being the index.** ``session`` + ``locator`` is how the evidence is
    *found*; ``sha256`` is how the bytes are *checked* once found. Directory
    mtime appears nowhere, because WO-0 measured it actively misrouting.

    Args:
      session: the session identity, carried per-citation so a reference read
        on its own is still self-describing.
      store: one of :data:`EVIDENCE_STORES`.
      locator: the path within that store, store-relative. For
        ``commissioning_bundle`` this is the evidence store's own
        bundle-relative artifact path.
      sha256: lowercase hex digest, or ``""`` when the producer did not hash
        the bytes. Empty means "presence is checkable, content is not" — an
        honest gap, never a silent pass.
    """

    session: SessionIdentity
    store: str
    locator: str
    sha256: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.session, SessionIdentity):
            raise FindingError("cite session must be a SessionIdentity")
        if self.store not in EVIDENCE_STORES:
            raise FindingError(
                f"cite store must be one of {EVIDENCE_STORES}, got {self.store!r}"
            )
        locator = _prose(self.locator, field_name="cite locator")
        if locator != locator.strip() or locator.startswith("/") or ".." in locator:
            raise FindingError(
                "cite locator must be a normalized store-relative path"
            )
        if self.sha256 and _SHA256_RE.fullmatch(self.sha256) is None:
            raise FindingError("cite sha256 must be a lowercase SHA-256 or empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "session": self.session.to_dict(),
            "store": self.store,
            "locator": self.locator,
            "sha256": self.sha256,
        }

    @classmethod
    def from_mapping(cls, raw: Any) -> "EvidenceRef":
        if not isinstance(raw, Mapping):
            raise FindingError("cite must be an object")
        unknown = set(raw) - {"session", "store", "locator", "sha256"}
        if unknown:
            raise FindingError(f"cite has unknown fields: {sorted(unknown)}")
        try:
            session = SessionIdentity.from_mapping(raw.get("session"))
        except SessionIdentityError as exc:
            raise FindingError(f"cite session is malformed: {exc}") from exc
        return cls(
            session=session,
            store=str(raw.get("store") or ""),
            locator=str(raw.get("locator") or ""),
            sha256=str(raw.get("sha256") or ""),
        )


@dataclass(frozen=True)
class Finding:
    """§3.1's artifact: the unit of diagnosis."""

    mechanism: str
    band_hz: tuple[float, float]
    evidence: Mapping[str, Any]
    confidence: str
    fix_class: str
    household_copy: str
    probes_run: tuple[str, ...] = ()
    probes_recommended: tuple[str, ...] = ()
    cites: tuple[EvidenceRef, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        try:
            spec = mechanism_spec(self.mechanism)
        except MechanismError as exc:
            raise FindingError(str(exc)) from exc

        if self.confidence not in CONFIDENCE_TIERS:
            raise FindingError(
                f"confidence must be one of {CONFIDENCE_TIERS}, got "
                f"{self.confidence!r}"
            )
        if self.fix_class not in spec.fix_classes:
            raise FindingError(
                f"{self.mechanism} may not route {self.fix_class!r}; its "
                f"declared classes are {spec.fix_classes}"
            )

        if not isinstance(self.band_hz, tuple) or len(self.band_hz) != 2:
            raise FindingError("band_hz must be a (f_lo_hz, f_hi_hz) pair")
        lo = _finite(self.band_hz[0], field_name="band_hz lower edge")
        hi = _finite(self.band_hz[1], field_name="band_hz upper edge")
        if lo < 0.0 or hi <= lo:
            raise FindingError("band_hz must be 0 <= f_lo_hz < f_hi_hz")
        object.__setattr__(self, "band_hz", (lo, hi))

        for name in ("probes_run", "probes_recommended"):
            probes = getattr(self, name)
            if not isinstance(probes, tuple):
                raise FindingError(f"{name} must be a tuple")
            unknown = set(probes) - set(PROBES)
            if unknown:
                raise FindingError(
                    f"{name} names probes outside the §5 table: {sorted(unknown)}"
                )
            if len(set(probes)) != len(probes):
                raise FindingError(f"{name} must not repeat a probe")
        # §3.2: "'unsure' carries the single recommended disambiguating
        # probe". An unsure finding with nothing to recommend is a dead end
        # dressed as a diagnosis — the flow could not offer the
        # refuse-and-recommend outcome §3.4 makes a first-class session
        # result.
        if self.confidence == CONFIDENCE_UNSURE and not self.probes_recommended:
            raise FindingError(
                "an 'unsure' finding must recommend at least one probe"
            )

        object.__setattr__(
            self, "household_copy", _validated_household_copy(self.household_copy)
        )

        if not isinstance(self.evidence, Mapping):
            raise FindingError("evidence must be a mapping")
        if len(self.evidence) > _MAX_EVIDENCE_KEYS:
            raise FindingError(
                f"evidence carries at most {_MAX_EVIDENCE_KEYS} keys"
            )
        frozen: dict[str, Any] = {}
        for key, value in self.evidence.items():
            if not isinstance(key, str) or not key.strip():
                raise FindingError("evidence keys must be non-empty strings")
            if isinstance(value, bool) or isinstance(value, str) or value is None:
                frozen[key] = value
            else:
                frozen[key] = _finite(value, field_name=f"evidence[{key}]")
        object.__setattr__(self, "evidence", MappingProxyType(frozen))

        if not isinstance(self.cites, tuple) or not self.cites:
            raise FindingError("a finding must cite at least one piece of evidence")
        if len(self.cites) > _MAX_CITES:
            raise FindingError(f"a finding may carry at most {_MAX_CITES} citations")
        if any(not isinstance(cite, EvidenceRef) for cite in self.cites):
            raise FindingError("cites must contain EvidenceRef values")
        # The retention invariant, made structural (Q-C, owner ruling
        # 2026-07-29 night): a finding lives inside the session bundle whose
        # evidence it cites and dies with it. A finding whose only citations
        # were the capture ring or a laptop archive would have no such
        # binding — both prune on their own schedules — so it could silently
        # outlive everything that justified it.
        if not any(cite.store == EVIDENCE_STORE_BUNDLE for cite in self.cites):
            raise FindingError(
                "a finding must cite at least one commissioning_bundle "
                "artifact — that citation is what binds its lifetime to its "
                "evidence"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": FINDING_SCHEMA,
            "mechanism": self.mechanism,
            "band_hz": [self.band_hz[0], self.band_hz[1]],
            "evidence": dict(self.evidence),
            "confidence": self.confidence,
            "fix_class": self.fix_class,
            "household_copy": self.household_copy,
            "probes_run": list(self.probes_run),
            "probes_recommended": list(self.probes_recommended),
            "cites": [cite.to_dict() for cite in self.cites],
        }

    @classmethod
    def from_mapping(cls, raw: Any) -> "Finding":
        if not isinstance(raw, Mapping):
            raise FindingError("finding must be an object")
        expected = set(FINDING_FIELD_DESCRIPTIONS) | {"schema"}
        unknown = set(raw) - expected
        if unknown:
            raise FindingError(f"finding has unknown fields: {sorted(unknown)}")
        missing = expected - set(raw)
        if missing:
            raise FindingError(f"finding is missing fields: {sorted(missing)}")
        if raw.get("schema") != FINDING_SCHEMA:
            raise FindingError(
                f"unsupported finding schema: {raw.get('schema')!r}"
            )
        band = raw.get("band_hz")
        if not isinstance(band, (list, tuple)) or len(band) != 2:
            raise FindingError("band_hz must be a two-element list")
        for name in ("probes_run", "probes_recommended", "cites"):
            if not isinstance(raw.get(name), (list, tuple)):
                raise FindingError(f"{name} must be a list")
        return cls(
            mechanism=str(raw.get("mechanism") or ""),
            band_hz=(band[0], band[1]),
            evidence=raw.get("evidence") or {},
            confidence=str(raw.get("confidence") or ""),
            fix_class=str(raw.get("fix_class") or ""),
            household_copy=str(raw.get("household_copy") or ""),
            probes_run=tuple(str(p) for p in raw["probes_run"]),
            probes_recommended=tuple(str(p) for p in raw["probes_recommended"]),
            cites=tuple(EvidenceRef.from_mapping(c) for c in raw["cites"]),
        )


def _validated_household_copy(value: Any) -> str:
    """Enforce §3.1's household-copy contract, or raise.

    Three checks, each a rule stated elsewhere in the plan rather than a
    style preference:

    * **No hardware noun.** §3.1, inheriting the shipped
      ``_null_classification_copy`` prohibition verbatim.
    * **No snake_case token.** Internal slugs (``position_invariant``,
      ``document_as_physics``) leaking into a household sentence is the same
      two-vocabularies failure one layer down.
    * **No mechanism id.** ``M2`` is internal taxonomy by §3.1's own
      definition; a household sentence naming it has crossed the line the
      artifact exists to hold.
    """

    text = _prose(value, field_name="household_copy")
    noun = _HARDWARE_NOUN_RE.search(text)
    if noun is not None:
        raise FindingError(
            f"household_copy must be hardware-noun-free (§3.1); found "
            f"{noun.group(0)!r}"
        )
    slug = _SNAKE_CASE_RE.search(text)
    if slug is not None:
        raise FindingError(
            f"household_copy must be prose, not an internal slug; found "
            f"{slug.group(0)!r}"
        )
    mechanism = _MECHANISM_ID_RE.search(text)
    if mechanism is not None:
        raise FindingError(
            f"household_copy must not name a mechanism id (§3.1 internal "
            f"taxonomy); found {mechanism.group(0)!r}"
        )
    return text


@dataclass(frozen=True)
class FindingSet:
    """Every finding one session produced, plus its provenance marker.

    An **empty** set is meaningful and legal: attribution ran and found
    nothing. That is a different statement from the set being *absent*, which
    means the bundle predates attribution entirely — see
    :func:`jasper.attribution.storage.read_finding_set`.
    """

    session: SessionIdentity
    produced_by: str
    findings: tuple[Finding, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.session, SessionIdentity):
            raise FindingError("session must be a SessionIdentity")
        object.__setattr__(
            self, "produced_by", _prose(self.produced_by, field_name="produced_by")
        )
        if not isinstance(self.findings, tuple):
            raise FindingError("findings must be a tuple")
        if len(self.findings) > _MAX_FINDINGS:
            raise FindingError(f"at most {_MAX_FINDINGS} findings per set")
        if any(not isinstance(item, Finding) for item in self.findings):
            raise FindingError("findings must contain Finding values")
        # Bundle-lifetime again: a set whose members cite a *different*
        # session's bundle would survive that session's eviction.
        for finding in self.findings:
            for cite in finding.cites:
                if cite.store != EVIDENCE_STORE_BUNDLE:
                    continue
                if cite.session.session_id != self.session.session_id:
                    raise FindingError(
                        "a finding's bundle citation must belong to this set's "
                        "own session"
                    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": FINDING_SET_SCHEMA,
            "session": self.session.to_dict(),
            "produced_by": self.produced_by,
            "findings": [finding.to_dict() for finding in self.findings],
            "field_descriptions": {
                "set": dict(FINDING_SET_FIELD_DESCRIPTIONS),
                "finding": dict(FINDING_FIELD_DESCRIPTIONS),
            },
        }

    @classmethod
    def from_mapping(cls, raw: Any) -> "FindingSet":
        if not isinstance(raw, Mapping):
            raise FindingError("finding set must be an object")
        if raw.get("schema") != FINDING_SET_SCHEMA:
            raise FindingError(
                f"unsupported finding set schema: {raw.get('schema')!r}"
            )
        findings = raw.get("findings")
        if not isinstance(findings, (list, tuple)):
            raise FindingError("findings must be a list")
        try:
            session = SessionIdentity.from_mapping(raw.get("session"))
        except SessionIdentityError as exc:
            raise FindingError(f"finding set session is malformed: {exc}") from exc
        return cls(
            session=session,
            produced_by=str(raw.get("produced_by") or ""),
            findings=tuple(Finding.from_mapping(item) for item in findings),
        )


__all__ = [
    "EVIDENCE_STORES",
    "EVIDENCE_STORE_BUNDLE",
    "EVIDENCE_STORE_CAPTURE_RING",
    "EVIDENCE_STORE_LAPTOP_ARCHIVE",
    "FINDING_FIELD_DESCRIPTIONS",
    "FINDING_SCHEMA",
    "FINDING_SET_FIELD_DESCRIPTIONS",
    "FINDING_SET_SCHEMA",
    "EvidenceRef",
    "Finding",
    "FindingError",
    "FindingSet",
]
