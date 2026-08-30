# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Per-band CONTROLLABILITY, aggregated over a speaker's banked round receipts.

Read-only aggregation. Every number here was banked by a round that already
ran; this module opens no capture, writes no state, and gates nothing. It
answers the question one receipt structurally cannot, because each receipt
describes one round: *in band X, do commands realize as commanded, and do they
do it the same way every time?* A receipt says "this round realized 0.62 of
what it commanded below 10 kHz"; the ledger says "over four rounds this band
realized 0.61 +/- 0.03".

Two blocks of ``round_receipt.json`` are read and nothing else:

* ``round_measurements.realization.bands`` — the delta probe's per-band
  realized/commanded slope, banked by
  :func:`~jasper.active_speaker.crossover_v2.coordinator._round_measurements`.
* ``round_axes.quality.evidence.spec_bands`` beside ``verification.spec`` —
  the grader's misses, and the round verdict that says how to read a band's
  absence from them (:func:`_spec_outcome`).

The cloud report's fuller per-band table is deliberately not read: it carries
curves, and this ledger is served from a polled status endpoint on a 1 GB Pi.
What that costs is named in :data:`OUTCOME_UNDISCLOSED` rather than hidden.

The band axis is :data:`~jasper.active_speaker.flat_spec.SPEC_BANDS`, and every
band in it appears in every ledger. Absence is disclosed, never interpolated: a
band with no observation reports :data:`CONFIDENCE_UNOBSERVED` and ``None``
statistics rather than a number borrowed from a neighbour or pooled across a
span no instrument measured as a unit (:func:`_realization_ratio`). Receipts
written before the realization block shipped read as absent, never as a raise.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jasper.active_speaker.flat_spec import SPEC_BANDS

__all__ = [
    "CONFIDENCE_CONSISTENT",
    "CONFIDENCE_INCONSISTENT",
    "CONFIDENCE_LABELS",
    "CONFIDENCE_LOW_N",
    "CONFIDENCE_UNOBSERVED",
    "CONSISTENT_RATIO_SIGMA",
    "MAX_RECEIPTS_SCANNED",
    "MIN_ROUNDS_FOR_SPREAD",
    "OUTCOME_FAILED",
    "OUTCOME_PASSED",
    "OUTCOME_UNDISCLOSED",
    "RATIO_DECIMALS",
    "ROUND_RECEIPT_GLOB",
    "BandControllability",
    "ControllabilityLedger",
    "ledger_from_receipts",
    "read_controllability_ledger",
    "receipt_paths",
]

#: ``*`` one is the bundle id, ``*`` two the minting relay session id — two
#: distinct namespaces, as
#: :data:`~jasper.active_speaker.candidate_bank.CANDIDATE_ARTIFACT_GLOB` states
#: for the sibling artifact in the same directory.
ROUND_RECEIPT_GLOB = "*/evidence/v1/artifacts/crossover_v2/*/round_receipt.json"

#: Receipts PARSED before giving up, matching
#: :data:`~jasper.active_speaker.candidate_bank.MAX_CANDIDATE_ARTIFACTS_SCANNED`
#: and bounding the same risk: the bundle root is retention-capped
#: (``DEFAULT_SESSIONS_MAX_BUNDLES = 12``), so a healthy box is far under this
#: and a pathological directory cannot turn one poll into unbounded work.
MAX_RECEIPTS_SCANNED = 64

#: A receipt banks identities and verdicts, never curves, so the writer's own
#: artifact is orders of magnitude under this. Defensive ceiling, not a
#: contract.
MAX_RECEIPT_BYTES = 1024 * 1024

OUTCOME_FAILED = "failed"
OUTCOME_PASSED = "passed"
#: The receipt does not say — see :func:`_spec_outcome`.
OUTCOME_UNDISCLOSED = "undisclosed"

CONFIDENCE_UNOBSERVED = "unobserved"
CONFIDENCE_LOW_N = "low_n"
CONFIDENCE_CONSISTENT = "consistent"
CONFIDENCE_INCONSISTENT = "inconsistent"

#: Every label, in increasing-evidence order. DESCRIPTIVE, all four: no
#: adoption row, refusal, prescription or gate reads one back, and none of them
#: says what anybody should do next.
CONFIDENCE_LABELS: tuple[str, ...] = (
    CONFIDENCE_UNOBSERVED,
    CONFIDENCE_LOW_N,
    CONFIDENCE_CONSISTENT,
    CONFIDENCE_INCONSISTENT,
)

#: Rounds a band needs before its spread is called anything: a spread over two
#: observations is the gap between two points, not a distribution.
MIN_ROUNDS_FOR_SPREAD = 3

#: Population sigma of the realized/commanded ratio at or below which a band's
#: rounds are called :data:`CONFIDENCE_CONSISTENT`. The ratio is dimensionless
#: and 1.0 is full commanded depth, so this is 15 percentage points of
#: delivered depth: wide enough to absorb the round-to-round scatter the
#: probe's own per-bin tolerances already assume, narrow enough that a band
#: swinging between half and full delivery is never called steady.
CONSISTENT_RATIO_SIGMA = 0.15

#: Decimal places the ratio and its spread are published to. Three, because the
#: third is already finer than the probe's per-bin tolerances resolve and
#: everything past it is float noise printed as measurement. Rounded once, here,
#: so every reader renders one number instead of deriving its own.
RATIO_DECIMALS = 3


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> Sequence[Any]:
    return value if isinstance(value, (list, tuple)) else ()


def _span(value: Any) -> tuple[float, float] | None:
    pair = _sequence(value)
    if len(pair) != 2:
        return None
    lo, hi = _finite(pair[0]), _finite(pair[1])
    if lo is None or hi is None or hi <= lo:
        return None
    return (lo, hi)


def _band_key(f_lo_hz: Any, f_hi_hz: Any) -> tuple[float, float] | None:
    """A spec row's NOMINAL edges as the axis key, or ``None``.

    Nominal rather than graded: the graded edges move with the session's
    trusted floor and ceiling (issue #2551), so keying on them would file one
    band's rounds under several keys the moment a microphone's trust changed
    mid-series — which is the history this ledger exists to pool.
    """

    lo, hi = _finite(f_lo_hz), _finite(f_hi_hz)
    if lo is None or hi is None or hi <= lo:
        return None
    return (lo, hi)


@dataclass(frozen=True)
class BandControllability:
    """One :data:`~jasper.active_speaker.flat_spec.SPEC_BANDS` row's history.

    ``ratio_sigma`` is the POPULATION spread and is ``None`` below two
    observations — the spread of one point is not a small spread, it is no
    spread. The three spec counters are disjoint and sum to the number of
    readable receipts, so a reader can tell "0 of 0" from "0 of 6".
    """

    f_lo_hz: float
    f_hi_hz: float
    tolerance_db: float
    n_realization: int
    ratio_mean: float | None
    ratio_sigma: float | None
    #: Octave fraction of the band the ratio was measured over — see
    #: :func:`_realization_ratio`. ``None`` exactly when ``ratio_mean`` is.
    coverage: float | None
    #: The span the pooled fits actually ran over, as the envelope across the
    #: contributing rounds. Routinely WIDER than the band it is filed under,
    #: which is the fact ``coverage`` alone cannot state. ``None`` exactly when
    #: ``ratio_mean`` is.
    fitted_band_hz: tuple[float, float] | None
    spec_passed: int
    spec_failed: int
    spec_undisclosed: int
    confidence: str

    @property
    def n_spec(self) -> int:
        return self.spec_passed + self.spec_failed + self.spec_undisclosed

    def to_dict(self) -> dict[str, Any]:
        return {
            "f_lo_hz": self.f_lo_hz,
            "f_hi_hz": self.f_hi_hz,
            "tolerance_db": self.tolerance_db,
            "confidence": self.confidence,
            "realization": {
                # The count rides with the summary: a ratio without its N is
                # unreadable, and the claim here is about how many agreed.
                "n_rounds": self.n_realization,
                "ratio_mean": self.ratio_mean,
                "ratio_sigma": self.ratio_sigma,
                "coverage": self.coverage,
                "fitted_band_hz": (
                    None if self.fitted_band_hz is None
                    else list(self.fitted_band_hz)
                ),
            },
            # Three counters rather than a pass fraction, which would hide its
            # denominator — the honest half.
            "spec": {
                "n_rounds": self.n_spec,
                OUTCOME_PASSED: self.spec_passed,
                OUTCOME_FAILED: self.spec_failed,
                OUTCOME_UNDISCLOSED: self.spec_undisclosed,
            },
        }


@dataclass(frozen=True)
class ControllabilityLedger:
    """Every spec band's controllability over the receipts that were read.

    ``n_rounds`` counts receipts READ, not receipts that contributed: six
    rounds that ran no probe is a different thing from no rounds, and both
    produce a full band axis.
    """

    bands: tuple[BandControllability, ...]
    n_rounds: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_rounds": self.n_rounds,
            "bands": [band.to_dict() for band in self.bands],
        }


def _realization_ratio(
    realization: Mapping[str, Any], span: tuple[float, float],
) -> tuple[float, float, tuple[float, float]] | None:
    """The one probe band that speaks for ``span``: ratio, coverage, fitted span.

    The probe cuts its bands at
    :data:`~jasper.active_speaker.delta_probe.DELTA_PROBE_HF_SPLIT_HZ` (10 kHz)
    and the spec table cuts at 2 kHz and 8 kHz, so the two partitions do not
    nest. Where exactly one GRADED probe band overlaps a spec band, its pooled
    slope is the only measurement anyone has of that span — which is why the
    250-2000 Hz and 2-8 kHz rows can legitimately carry the same number, at the
    probe's coarser resolution.

    Where TWO overlap — the 8-16 kHz row, cut in half by the 10 kHz split —
    there is no single measured ratio, and this returns ``None``. Picking one
    or averaging them would publish a number no instrument produced.

    **The overlap count is taken over every graded band that reaches the span,
    including one whose own fit failed.** A probe band with too few bins to fit
    a slope reports ``ratio=None`` (``DELTA_PROBE_MIN_BINS``), which is routine
    for ``trusted_hf`` on a round that commanded little above 10 kHz — and it
    still means the span is split. Skipping it before the count would leave the
    8-16 kHz row taking the crossover band's 953-10000 Hz slope as its own:
    a confidently-labelled number produced almost entirely outside the band it
    is filed under, which is the exact failure this refusal exists to prevent.

    ``coverage`` is the OCTAVE fraction of the spec band the overlap spanned,
    and ``fitted`` is the span the slope was actually fitted over. BOTH travel,
    because coverage alone is misreadable in the direction that matters: the
    low-mid row's 0.36 says the overlap covers a third of that row, and says
    nothing about the two thirds of the FIT that sit above it. Disclosed for
    the reason
    :attr:`~jasper.active_speaker.delta_probe.DeltaProbeMap.quiet_probe_coverage`
    is, and never used to weight or correct the ratio, which is the probe's.

    ``above_ceiling`` never counts: the probe marks it ``graded=False`` by
    construction and it enters no verdict.
    """

    # Positive by construction: ``_band_key`` drops any row whose upper edge
    # does not exceed its lower one, so no degenerate span reaches the axis.
    width = math.log2(span[1] / span[0])
    overlapping: list[tuple[float | None, float, tuple[float, float]]] = []
    for entry in _mapping(realization.get("bands")).values():
        row = _mapping(entry)
        if row.get("graded") is not True:
            continue
        band = _span(row.get("band_hz"))
        if band is None:
            continue
        lo, hi = max(band[0], span[0]), min(band[1], span[1])
        if hi <= lo:
            continue
        overlapping.append(
            (_finite(row.get("ratio")), math.log2(hi / lo) / width, band)
        )
    if len(overlapping) != 1:
        return None
    ratio, coverage, fitted = overlapping[0]
    return None if ratio is None else (ratio, coverage, fitted)


def _spec_outcome(
    key: tuple[float, float], failing: frozenset[tuple[float, float]], verdict: str,
) -> str:
    """This band's spec outcome in one round, from the receipt alone.

    Named among the round's misses -> :data:`OUTCOME_FAILED`, the only one of
    the three the receipt states directly. Not named, on a round whose verdict
    is a clean pass -> :data:`OUTCOME_PASSED`, sound because
    :func:`~jasper.active_speaker.crossover_v2.verification.evaluate_spec`
    issues ``PASSED`` only when every band was evaluable and inside tolerance,
    so no unevaluable band can hide behind it. Anything else ->
    :data:`OUTCOME_UNDISCLOSED`: the receipt banks only the misses, so on a
    round that failed or could not be graded, a band's absence from that list
    means "passed" or "was never evaluable" and nothing separates them.
    """

    if key in failing:
        return OUTCOME_FAILED
    return OUTCOME_PASSED if verdict == "passed" else OUTCOME_UNDISCLOSED


def _failing_keys(receipt: Mapping[str, Any]) -> frozenset[tuple[float, float]]:
    evidence = _mapping(
        _mapping(_mapping(receipt.get("round_axes")).get("quality")).get("evidence")
    )
    keys = set()
    for raw in _sequence(evidence.get("spec_bands")):
        key = _band_key(_mapping(raw).get("f_lo_hz"), _mapping(raw).get("f_hi_hz"))
        if key is not None:
            keys.add(key)
    return frozenset(keys)


def _confidence(ratios: Sequence[float], sigma: float | None) -> str:
    """The label, from the count and the spread and nothing else.

    Blind to the ratios' own values: a band that consistently delivers 40% of
    what it is asked for is :data:`CONFIDENCE_CONSISTENT`, because that is a
    statement about how well the band is UNDERSTOOD, not about whether the
    speaker is any good. Judging the depth is the grader's job.
    """

    if not ratios:
        return CONFIDENCE_UNOBSERVED
    if len(ratios) < MIN_ROUNDS_FOR_SPREAD or sigma is None:
        return CONFIDENCE_LOW_N
    return (
        CONFIDENCE_CONSISTENT if sigma <= CONSISTENT_RATIO_SIGMA
        else CONFIDENCE_INCONSISTENT
    )


def ledger_from_receipts(
    receipts: Iterable[Mapping[str, Any]],
) -> ControllabilityLedger:
    """Fold banked round receipts into one ledger, in any order.

    A mean and a spread do not care which round came first. An empty or
    unrecognisable receipt is still counted as a round and contributes
    :data:`OUTCOME_UNDISCLOSED` to every band, which is what it honestly says.
    """

    axis: list[tuple[tuple[float, float], float]] = []
    for f_lo_hz, f_hi_hz, tolerance_db in SPEC_BANDS:
        key = _band_key(f_lo_hz, f_hi_hz)
        if key is not None:
            axis.append((key, float(tolerance_db)))

    ratios: dict[tuple[float, float], list[float]] = {key: [] for key, _ in axis}
    coverages: dict[tuple[float, float], list[float]] = {key: [] for key, _ in axis}
    fitted: dict[tuple[float, float], list[tuple[float, float]]] = {
        key: [] for key, _ in axis
    }
    outcomes: dict[tuple[float, float], dict[str, int]] = {
        key: {OUTCOME_PASSED: 0, OUTCOME_FAILED: 0, OUTCOME_UNDISCLOSED: 0}
        for key, _ in axis
    }
    n_rounds = 0

    for raw in receipts:
        n_rounds += 1
        receipt = _mapping(raw)
        realization = _mapping(
            _mapping(receipt.get("round_measurements")).get("realization")
        )
        failing = _failing_keys(receipt)
        verdict = str(_mapping(receipt.get("verification")).get("spec") or "")
        for key, _tolerance in axis:
            outcomes[key][_spec_outcome(key, failing, verdict)] += 1
            observed = _realization_ratio(realization, key)
            if observed is not None:
                ratios[key].append(observed[0])
                coverages[key].append(observed[1])
                fitted[key].append(observed[2])

    bands: list[BandControllability] = []
    for key, tolerance in axis:
        seen = ratios[key]
        mean = sum(seen) / len(seen) if seen else None
        sigma = (
            math.sqrt(sum((value - mean) ** 2 for value in seen) / len(seen))
            if mean is not None and len(seen) > 1
            else None
        )
        # Rounded AFTER the spread is taken from the unrounded values, so the
        # published sigma is the spread of the observations rather than of
        # their roundings.
        confidence = _confidence(seen, sigma)
        spanned = coverages[key]
        coverage = sum(spanned) / len(spanned) if spanned else None
        # The envelope across contributing rounds, not one round's span: the
        # trusted floor can move mid-series, and a single round's edges would
        # under-state what the pooled mean was actually taken over.
        runs = fitted[key]
        envelope = (
            (min(lo for lo, _ in runs), max(hi for _, hi in runs)) if runs else None
        )
        counts = outcomes[key]
        bands.append(BandControllability(
            f_lo_hz=key[0],
            f_hi_hz=key[1],
            tolerance_db=tolerance,
            n_realization=len(seen),
            ratio_mean=None if mean is None else round(mean, RATIO_DECIMALS),
            ratio_sigma=None if sigma is None else round(sigma, RATIO_DECIMALS),
            coverage=None if coverage is None else round(coverage, RATIO_DECIMALS),
            fitted_band_hz=envelope,
            spec_passed=counts[OUTCOME_PASSED],
            spec_failed=counts[OUTCOME_FAILED],
            spec_undisclosed=counts[OUTCOME_UNDISCLOSED],
            confidence=confidence,
        ))
    return ControllabilityLedger(bands=tuple(bands), n_rounds=n_rounds)


def receipt_paths(root: Path) -> list[Path]:
    """Every banked round receipt under the bundle root, in a stable order.

    Sorted for determinism only. **Not chronological**, and deliberately not
    claimed to be: a bundle directory is named ``uuid4().hex[:12]``
    (:func:`~jasper.active_speaker.bundles.open_bundle`), so path order carries
    no time information — ``started_at`` inside each ``info.json`` is the only
    thing that does, and reading a dozen of those to order a set this module
    then pools order-independently would buy nothing.

    :data:`MAX_RECEIPTS_SCANNED` therefore BOUNDS work rather than selecting
    recent history, and which end it truncates is arbitrary because the order
    is. The bundle root's own retention cap (12) keeps a healthy box far under
    it; a directory that reached this cap would already have lost the property
    that makes any subset of it meaningful.
    """

    try:
        found = sorted(Path(root).glob(ROUND_RECEIPT_GLOB))
    except OSError:
        return []
    return found[-MAX_RECEIPTS_SCANNED:]


def _load_receipt(path: Path) -> Mapping[str, Any] | None:
    """Parse one receipt, or ``None`` when it is not usable.

    Never raises. An unreadable receipt costs the ledger that round, as a round
    that ran no probe does; losing the whole ledger to one bad file would be
    the trade this module refuses everywhere else.
    """

    try:
        if path.stat().st_size > MAX_RECEIPT_BYTES:
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, Mapping) else None


def read_controllability_ledger(root: Path | None = None) -> ControllabilityLedger:
    """The ledger over a speaker's banked rounds.

    ``root`` defaults to :func:`~jasper.active_speaker.bundles.sessions_dir`,
    the bundle root the sibling banked-artifact readers walk. A box with no
    banked rounds yields a full axis of :data:`CONFIDENCE_UNOBSERVED` rows
    rather than an empty document: the absence is the answer, and it has the
    same shape as every other answer.
    """

    from jasper.active_speaker.bundles import sessions_dir

    bundle_root = Path(root) if root is not None else sessions_dir()
    receipts = [
        receipt
        for receipt in (_load_receipt(path) for path in receipt_paths(bundle_root))
        if receipt is not None
    ]
    return ledger_from_receipts(receipts)
