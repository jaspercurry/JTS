# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The per-band controllability ledger over banked round receipts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jasper.active_speaker.controllability_ledger import (
    CONFIDENCE_CONSISTENT,
    CONFIDENCE_INCONSISTENT,
    CONFIDENCE_LOW_N,
    CONFIDENCE_UNOBSERVED,
    ledger_from_receipts,
    read_controllability_ledger,
)

#: The three spec rows, in ledger order.
LOW_MID = (250.0, 2000.0)
PRESENCE = (2000.0, 8000.0)
TOP = (8000.0, 16000.0)


def _receipt(
    *,
    ratio: float | None = None,
    hf_ratio: float | None = 0.95,
    spec: str = "passed",
    failing: tuple[tuple[float, float], ...] = (),
) -> dict:
    """One banked receipt carrying the two blocks the ledger reads.

    ``ratio`` is the probe's pooled slope below the 10 kHz split, which is the
    only graded band overlapping the two lower spec rows; ``hf_ratio`` is the
    band above it. Both bands overlap the 8-16 kHz row, which is why that row
    never takes a realization number from a receipt of this shape.
    """
    bands: dict[str, dict] = {
        "above_ceiling": {
            "band_hz": [16000.0, 20000.0], "n_bins": 40,
            "ratio": 3.0, "graded": False,
        },
    }
    if ratio is not None:
        bands["crossover"] = {
            "band_hz": [953.5, 9999.98], "n_bins": 300,
            "ratio": ratio, "graded": True,
        }
    if hf_ratio is not None:
        bands["trusted_hf"] = {
            "band_hz": [10000.0, 16000.0], "n_bins": 90,
            "ratio": hf_ratio, "graded": True,
        }
    return {
        "verification": {"spec": spec},
        "round_axes": {"quality": {"evidence": {"spec_bands": [
            {"f_lo_hz": lo, "f_hi_hz": hi, "passed": False,
             "tolerance_db": 2.0, "max_deviation_db": -3.1}
            for lo, hi in failing
        ]}}},
        "round_measurements": {"realization": {"bands": bands}},
    }


def _band(ledger, key: tuple[float, float]) -> dict:
    rows = [
        row for row in ledger.to_dict()["bands"]
        if (row["f_lo_hz"], row["f_hi_hz"]) == key
    ]
    assert len(rows) == 1, f"expected exactly one {key} row, got {len(rows)}"
    return rows[0]


def test_the_ledger_pools_realized_depth_and_spec_outcomes_across_rounds():
    """Three rounds of known numbers land as one per-band summary.

    The mean and the spread are the claim no single receipt carries, and the
    three spec counters are asserted together because their SUM is the honest
    half: a reader has to be able to tell "0 of 0" from "0 of 3".
    """
    ledger = ledger_from_receipts([
        _receipt(ratio=0.60, spec="failed", failing=(PRESENCE,)),
        _receipt(ratio=0.62, spec="failed", failing=(PRESENCE,)),
        _receipt(ratio=0.58, spec="passed"),
    ])
    assert ledger.to_dict()["n_rounds"] == 3

    low_mid = _band(ledger, LOW_MID)
    assert low_mid["realization"]["n_rounds"] == 3
    assert low_mid["realization"]["ratio_mean"] == 0.6
    # The spread of the observations, taken before the rounding and published
    # at the same three places the mean is.
    assert low_mid["realization"]["ratio_sigma"] == 0.016
    assert low_mid["confidence"] == CONFIDENCE_CONSISTENT
    # Two rounds failed elsewhere, so this band's pass is unknowable from the
    # receipt; the third round passed outright, which names every band.
    assert low_mid["spec"] == {
        "n_rounds": 3, "passed": 1, "failed": 0, "undisclosed": 2,
    }

    presence = _band(ledger, PRESENCE)
    # The same probe band answers for both lower rows — one pooled measurement
    # at a coarser resolution, reported at both rather than invented at either.
    assert presence["realization"]["ratio_mean"] == pytest.approx(0.60)
    assert presence["spec"] == {
        "n_rounds": 3, "passed": 1, "failed": 2, "undisclosed": 0,
    }

    # …and what separates the two rows is how much of each the probe reached.
    # The probe's graded floor is 953.5 Hz, so it spans one octave of the
    # low-mid row's three and all of the presence row: same number, very
    # different standing, and the ledger says which is which.
    assert low_mid["realization"]["coverage"] == 0.356
    assert presence["realization"]["coverage"] == 1.0


def test_a_band_two_probe_bands_split_takes_no_ratio_from_either():
    """8-16 kHz straddles the 10 kHz probe split, so no ratio is published.

    Averaging the two overlapping bands, or picking one, would publish a
    number no instrument produced. The spec history for the row still lands —
    the absence is confined to the quantity that was not measured.
    """
    ledger = ledger_from_receipts([_receipt(ratio=0.6) for _ in range(4)])
    top = _band(ledger, TOP)
    assert top["realization"] == {
        "n_rounds": 0, "ratio_mean": None, "ratio_sigma": None,
        "coverage": None,
    }
    assert top["confidence"] == CONFIDENCE_UNOBSERVED
    assert top["spec"]["passed"] == 4


@pytest.mark.parametrize(
    ("ratios", "confidence"),
    [
        ((), CONFIDENCE_UNOBSERVED),
        ((0.6,), CONFIDENCE_LOW_N),
        ((0.6, 0.9), CONFIDENCE_LOW_N),
        ((0.60, 0.61, 0.62), CONFIDENCE_CONSISTENT),
        ((0.2, 0.6, 1.0), CONFIDENCE_INCONSISTENT),
    ],
)
def test_confidence_is_read_off_the_count_and_the_spread_alone(
    ratios, confidence,
):
    """The label describes the evidence, never the speaker.

    A band delivering a steady 20% of what it is asked for is CONSISTENT: how
    well the band is understood and how good it sounds are different
    questions, and only the first one is this module's.
    """
    ledger = ledger_from_receipts([_receipt(ratio=r) for r in ratios])
    assert _band(ledger, LOW_MID)["confidence"] == confidence


@pytest.mark.parametrize(
    "receipt",
    [
        pytest.param({}, id="empty"),
        pytest.param({"round_id": "r1"}, id="identity_only"),
        pytest.param(
            {"round_measurements": {}, "round_axes": {}, "verification": {}},
            id="blocks_present_but_empty",
        ),
        pytest.param(
            {"round_measurements": {"realization": {"bands": {}}}},
            id="probe_ran_but_banked_no_bands",
        ),
        pytest.param(
            {"round_measurements": {"realization": {"bands": {
                "crossover": {"band_hz": None, "n_bins": 0,
                              "ratio": None, "graded": True},
            }}}},
            id="band_too_thin_to_fit",
        ),
    ],
)
def test_a_receipt_missing_the_newer_blocks_reads_as_absent_not_as_zero(receipt):
    """Old bundles cost the ledger observations, never rows and never a raise.

    Every band still appears, reporting ``unobserved`` with ``None``
    statistics — not 0.0, which would claim the band was measured and
    delivered nothing.
    """
    ledger = ledger_from_receipts([receipt])
    payload = ledger.to_dict()
    assert payload["n_rounds"] == 1
    assert len(payload["bands"]) == 3
    for row in payload["bands"]:
        assert row["confidence"] == CONFIDENCE_UNOBSERVED
        assert row["realization"] == {
            "n_rounds": 0, "ratio_mean": None, "ratio_sigma": None,
            "coverage": None,
        }
        # The round is still counted, and its outcome is still undisclosed
        # rather than absorbed into a pass.
        assert row["spec"] == {
            "n_rounds": 1, "passed": 0, "failed": 0, "undisclosed": 1,
        }


def test_no_banked_round_still_publishes_every_band(tmp_path, monkeypatch):
    """An empty bundle root is an answer with the same shape as every other."""
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_SESSIONS_DIR", str(tmp_path))
    payload = read_controllability_ledger().to_dict()
    assert payload["n_rounds"] == 0
    assert [(b["f_lo_hz"], b["f_hi_hz"]) for b in payload["bands"]] == [
        LOW_MID, PRESENCE, TOP,
    ]
    assert {b["confidence"] for b in payload["bands"]} == {CONFIDENCE_UNOBSERVED}


def _bank(root: Path, bundle: str, relay: str, receipt: dict | str) -> None:
    """File one receipt where the store puts it."""
    round_dir = root / bundle / "evidence/v1/artifacts/crossover_v2" / relay
    round_dir.mkdir(parents=True, exist_ok=True)
    path = round_dir / "round_receipt.json"
    path.write_text(
        receipt if isinstance(receipt, str) else json.dumps(receipt),
        encoding="utf-8",
    )


def test_the_reader_walks_the_bundle_root_and_survives_a_corrupt_receipt(
    tmp_path, monkeypatch,
):
    """Two good rounds and one unreadable file: the ledger keeps the two.

    Losing the whole history to one bad file is the trade this reader refuses;
    the corrupt round simply never becomes an observation.
    """
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_SESSIONS_DIR", str(tmp_path))
    _bank(tmp_path, "bundle-a", "relay-1", _receipt(ratio=0.70))
    _bank(tmp_path, "bundle-b", "relay-2", _receipt(ratio=0.74))
    _bank(tmp_path, "bundle-c", "relay-3", "{not json at all")

    payload = read_controllability_ledger().to_dict()
    assert payload["n_rounds"] == 2
    low_mid = next(
        row for row in payload["bands"]
        if (row["f_lo_hz"], row["f_hi_hz"]) == LOW_MID
    )
    assert low_mid["realization"]["n_rounds"] == 2
    assert low_mid["realization"]["ratio_mean"] == pytest.approx(0.72)


def test_the_state_block_carries_the_ledger_with_its_numbers(
    tmp_path, monkeypatch,
):
    """``/state``'s session disclosure gains the object, values intact."""
    from jasper.web import correction_crossover_v2_status as v2status

    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_SESSIONS_DIR", str(tmp_path))
    _bank(tmp_path, "bundle-a", "relay-1", _receipt(ratio=0.50, spec="passed"))
    _bank(tmp_path, "bundle-b", "relay-2", _receipt(ratio=0.52, spec="passed"))
    _bank(tmp_path, "bundle-c", "relay-3", _receipt(ratio=0.51, spec="passed"))

    ledger = v2status.crossover_v2_status_block()["controllability"]
    assert ledger["n_rounds"] == 3
    low_mid = next(
        row for row in ledger["bands"]
        if (row["f_lo_hz"], row["f_hi_hz"]) == LOW_MID
    )
    assert low_mid["realization"]["ratio_mean"] == pytest.approx(0.51)
    assert low_mid["confidence"] == CONFIDENCE_CONSISTENT
    assert low_mid["spec"] == {
        "n_rounds": 3, "passed": 3, "failed": 0, "undisclosed": 0,
    }


def test_an_unreadable_bundle_root_costs_the_key_and_not_the_whole_block(
    monkeypatch,
):
    """The ledger is disclosure; it may never take the poll path down with it.

    ``None`` rather than an empty document, because a box with rounds that
    measured nothing already publishes a full axis of ``unobserved`` rows —
    so ``None`` can only mean the ledger itself was unavailable.
    """
    from jasper.active_speaker import controllability_ledger
    from jasper.web import correction_crossover_v2_status as v2status

    def _explode(*_args, **_kwargs):
        raise OSError("bundle root is gone")

    monkeypatch.setattr(
        controllability_ledger, "read_controllability_ledger", _explode,
    )
    block = v2status.crossover_v2_status_block()
    assert block is not None
    assert block["controllability"] is None
    # …and the rest of the disclosure is untouched.
    assert "phase" in block and "round_receipt" in block
