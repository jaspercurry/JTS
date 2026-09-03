# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The raw controllability read over banked round receipts.

The reader derives nothing (ADR-0198), so what is pinned here is the READ: it
finds the receipts, survives the ones it cannot parse, and hands back the two
blocks each carries in the shape they were banked. Pooling is the reader's, and
there is nothing here to pin about it.
"""

from __future__ import annotations

import json
from pathlib import Path

from jasper.active_speaker.controllability_ledger import (
    read_controllability_ledger,
)


def _receipt(
    *,
    ratio: float | None = None,
    hf_ratio: float | None = 0.95,
    spec: str = "passed",
    failing: tuple[tuple[float, float], ...] = (),
    captured_at: float | None = None,
) -> dict:
    """One banked receipt carrying the two blocks the reader reads."""
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
    receipt = {
        "verification": {"spec": spec},
        "round_axes": {"quality": {"evidence": {"spec_bands": [
            {"f_lo_hz": lo, "f_hi_hz": hi, "passed": False,
             "tolerance_db": 2.0, "max_deviation_db": -3.1}
            for lo, hi in failing
        ]}}},
        "round_measurements": {"realization": {"bands": bands}},
    }
    if captured_at is not None:
        receipt["captured_at"] = captured_at
    return receipt


def _bank(root: Path, bundle: str, capture: str, receipt: dict | str) -> None:
    """File one receipt where the store puts it."""
    round_dir = root / bundle / "evidence/v1/artifacts/crossover_v2" / capture
    round_dir.mkdir(parents=True, exist_ok=True)
    path = round_dir / "round_receipt.json"
    path.write_text(
        receipt if isinstance(receipt, str) else json.dumps(receipt),
        encoding="utf-8",
    )


def test_the_reader_hands_back_each_rounds_blocks_exactly_as_banked(tmp_path):
    """What the probe and the grader wrote is what comes out — unpooled.

    The acceptance pin: no mean, no spread, no label, and the per-band rows are
    the probe's own dicts rather than a reshaped copy of them.
    """
    _bank(tmp_path, "bundle-a", "relay-1", _receipt(
        ratio=0.70, spec="failed", failing=((250.0, 2000.0),),
    ))

    payload = read_controllability_ledger(tmp_path)
    assert payload["n_rounds"] == 1
    (entry,) = payload["rounds"]
    assert entry["spec"] == "failed"
    assert entry["bands"]["crossover"] == {
        "band_hz": [953.5, 9999.98], "n_bins": 300,
        "ratio": 0.70, "graded": True,
    }
    assert entry["bands"]["above_ceiling"]["graded"] is False
    assert [
        (row["f_lo_hz"], row["f_hi_hz"]) for row in entry["spec_misses"]
    ] == [(250.0, 2000.0)]


def test_every_banked_round_is_its_own_entry(tmp_path):
    """Three rounds are three entries; nothing is folded across them."""
    _bank(tmp_path, "bundle-a", "relay-1", _receipt(ratio=0.50))
    _bank(tmp_path, "bundle-b", "relay-2", _receipt(ratio=0.52))
    _bank(tmp_path, "bundle-c", "relay-3", _receipt(ratio=0.51))

    payload = read_controllability_ledger(tmp_path)
    assert payload["n_rounds"] == 3
    assert sorted(
        entry["bands"]["crossover"]["ratio"] for entry in payload["rounds"]
    ) == [0.50, 0.51, 0.52]


def test_a_receipt_missing_the_newer_blocks_reads_as_absent_not_as_zero(
    tmp_path,
):
    """A round from before the probe shipped is still a round, with no bands.

    Empty rather than a fabricated row: the receipt does not say, and inventing
    a zero would be a number no instrument produced.
    """
    _bank(tmp_path, "bundle-a", "relay-1", {"verification": {"spec": "passed"}})

    payload = read_controllability_ledger(tmp_path)
    assert payload["n_rounds"] == 1
    (entry,) = payload["rounds"]
    assert entry == {"bands": {}, "spec": "passed", "spec_misses": []}


def test_no_banked_round_reads_as_no_rounds(tmp_path):
    """A box that has never tuned answers with the same shape, empty."""
    assert read_controllability_ledger(tmp_path) == {
        "n_rounds": 0, "rounds": [],
    }


def test_the_reader_walks_the_bundle_root_and_survives_a_corrupt_receipt(
    tmp_path, monkeypatch,
):
    """Two good rounds and one unreadable file: the reader keeps the two.

    Losing the whole history to one bad file is the trade this reader refuses;
    the corrupt round simply never becomes an entry. Also pins the default
    root — no argument means the speaker's own bundle directory.
    """
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_SESSIONS_DIR", str(tmp_path))
    _bank(tmp_path, "bundle-a", "relay-1", _receipt(ratio=0.70))
    _bank(tmp_path, "bundle-b", "relay-2", _receipt(ratio=0.74))
    _bank(tmp_path, "bundle-c", "relay-3", "{not json at all")

    payload = read_controllability_ledger()
    assert payload["n_rounds"] == 2
    assert sorted(
        entry["bands"]["crossover"]["ratio"] for entry in payload["rounds"]
    ) == [0.70, 0.74]


def test_the_state_block_carries_the_rounds_with_their_numbers(
    tmp_path, monkeypatch,
):
    """``/state``'s session disclosure gains the raw rows, values intact."""
    from jasper.web import correction_crossover_v2_status as v2status

    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_SESSIONS_DIR", str(tmp_path))
    _bank(tmp_path, "bundle-a", "relay-1", _receipt(ratio=0.50, spec="passed"))
    _bank(tmp_path, "bundle-b", "relay-2", _receipt(ratio=0.52, spec="passed"))

    ledger = v2status.crossover_v2_status_block()["controllability"]
    assert ledger["n_rounds"] == 2
    assert {entry["spec"] for entry in ledger["rounds"]} == {"passed"}
    assert sorted(
        entry["bands"]["crossover"]["ratio"] for entry in ledger["rounds"]
    ) == [0.50, 0.52]


def test_an_unreadable_bundle_root_costs_the_key_and_not_the_whole_block(
    monkeypatch,
):
    """The ledger is disclosure; it may never take the poll path down with it.

    ``None`` rather than an empty document, because a box with rounds that
    measured nothing already publishes those rounds with empty band rows — so
    ``None`` can only mean the read itself was unavailable.
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
