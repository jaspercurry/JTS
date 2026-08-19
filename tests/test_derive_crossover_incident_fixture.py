# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the incident-fixture derivation script's hand-banked field.

``scripts/derive-crossover-incident-fixture.py`` can no longer derive
``anchor_replay``: the give-back it encodes is measured over
``branch_level_bands_hz`` and needs the per-driver complex responses the
2026-08-10 bank never retained. The value is hand-banked in the committed
fixture instead, and the script guards it two ways — ``_carry_hand_banked``
splices the committed block into a re-derivation so a plain re-run cannot
delete it, and ``--check`` FAILS when it is gone rather than reporting a pass
it did not earn (the diff loop cannot see the deletion: absent from both sides
compares equal).

The script is a script, not a package module, and its filename is not an
identifier, so it is loaded by path.

Nothing here reads the banked bundle — it is 93 MB of gitignored capture data.
The end-to-end ``--check`` tests stub ``derive`` (the one function that reads
the bank) and drive ``main`` for real over a tmp_path fixture directory, so the
exit code, the carry, and the message all come from production code.
"""

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "derive-crossover-incident-fixture.py"
)
_spec = importlib.util.spec_from_file_location(
    "derive_crossover_incident_fixture", _SCRIPT
)
derive_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(derive_fixture)


# A value shaped like the real one: a dict carrying its own provenance note.
# Distinct per key so a carry that returns the wrong one is visible.
def _banked(key: str) -> dict[str, object]:
    return {"note": f"hand-banked {key}", "value_db": -6.400575020419777}


def _derived_payload() -> dict[str, dict[str, object]]:
    """What ``derive`` returns: no hand-banked key anywhere in it."""
    return {
        "expected_outcome.json": {
            "_provenance": {"bundle_dir_name": "bundle", "note": "synthetic"},
            "committed_attenuations_db": {"tweeter": -6.4, "woofer": 0.0},
            "fingerprint": "3df7a4da",
        }
    }


def _commit(out: Path, payload: dict[str, object]) -> None:
    """Write ``payload`` as the committed expected_outcome.json.

    Rendered through the script's own ``_render`` so the bytes match what a
    re-derivation would produce, and ``--check`` compares content rather than
    formatting.
    """
    out.mkdir(parents=True, exist_ok=True)
    (out / "expected_outcome.json").write_text(
        derive_fixture._render(payload), encoding="utf-8"
    )


def test_hand_banked_keys_names_anchor_replay():
    """The guarded set is the give-back field, and it is not empty.

    An empty tuple would make every guard below vacuously pass.
    """
    assert "anchor_replay" in derive_fixture.HAND_BANKED_KEYS


# (a) committed fixture present WITH the key
def test_carries_the_committed_value_and_splices_it_into_derived(tmp_path):
    keys = derive_fixture.HAND_BANKED_KEYS
    committed = {**_derived_payload()["expected_outcome.json"]}
    committed.update({key: _banked(key) for key in keys})
    _commit(tmp_path, committed)

    derived = _derived_payload()
    carried = derive_fixture._carry_hand_banked(tmp_path, derived)

    assert carried == {key: _banked(key) for key in keys}
    # Spliced into the payload in place, alongside — not instead of — what the
    # bundle derived.
    got = derived["expected_outcome.json"]
    for key in keys:
        assert got[key] == _banked(key)
    assert got["fingerprint"] == "3df7a4da"
    assert got["committed_attenuations_db"] == {"tweeter": -6.4, "woofer": 0.0}


# (b) committed fixture present WITHOUT the key
def test_returns_none_when_the_committed_fixture_lacks_the_key(tmp_path):
    _commit(tmp_path, _derived_payload()["expected_outcome.json"])

    derived = _derived_payload()
    assert derive_fixture._carry_hand_banked(tmp_path, derived) is None
    for key in derive_fixture.HAND_BANKED_KEYS:
        assert key not in derived["expected_outcome.json"]


# (c) no committed fixture file at all
def test_returns_none_when_there_is_no_committed_fixture(tmp_path):
    derived = _derived_payload()
    before = {name: dict(payload) for name, payload in derived.items()}

    assert derive_fixture._carry_hand_banked(tmp_path, derived) is None
    assert derived == before


# (d) the derived payload has no expected_outcome.json entry
def test_returns_none_when_the_derived_payload_has_no_entry_to_splice_into(tmp_path):
    committed = {**_derived_payload()["expected_outcome.json"]}
    committed.update(
        {key: _banked(key) for key in derive_fixture.HAND_BANKED_KEYS}
    )
    _commit(tmp_path, committed)

    derived = {"session_context.json": {"tier": "measured"}}
    assert derive_fixture._carry_hand_banked(tmp_path, derived) is None
    # No entry invented for a file this derivation did not produce.
    assert derived == {"session_context.json": {"tier": "measured"}}


@pytest.fixture
def stubbed_bundle(tmp_path, monkeypatch):
    """A bundle directory ``main`` accepts, with ``derive`` stubbed.

    ``derive`` is the only function that reads the gitignored 93 MB bank;
    everything else under test — the carry, the diff loop, the exit code, the
    messages — runs for real.
    """
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    monkeypatch.setattr(derive_fixture, "derive", lambda _bundle: _derived_payload())
    return bundle


def test_check_fails_when_a_hand_banked_field_is_missing(
    tmp_path, stubbed_bundle, capsys
):
    """A guarded field going missing is a FAILED check, not a passed one.

    The committed fixture here is byte-identical to what this run re-derives,
    so the diff loop finds nothing: without the presence guard this exits 0
    and the deleted value is never reported.
    """
    out = tmp_path / "fixture"
    _commit(out, _derived_payload()["expected_outcome.json"])

    rc = derive_fixture.main(
        ["--check", "--bundle", str(stubbed_bundle), "--out", str(out)]
    )

    assert rc != 0
    err = capsys.readouterr().err
    assert "anchor_replay" in err
    # Names the file, says a re-run is not the repair, and asks for a hand
    # restore with the provenance note.
    assert "expected_outcome.json" in err
    assert "cannot" in err and "regenerate" in err
    assert "by hand" in err
    # The write-mode wording must not leak into a mode that writes nothing.
    assert "writ" not in err.lower()


def test_check_passes_when_the_hand_banked_field_is_present(
    tmp_path, stubbed_bundle, capsys
):
    """Positive control: the same run, with the field restored, exits 0.

    Without this the failure above could be an unconditional failure rather
    than a guard, and it also pins that the carry makes the hand-banked field
    match by construction instead of registering as drift.
    """
    out = tmp_path / "fixture"
    committed = {**_derived_payload()["expected_outcome.json"]}
    committed.update(
        {key: _banked(key) for key in derive_fixture.HAND_BANKED_KEYS}
    )
    _commit(out, committed)

    rc = derive_fixture.main(
        ["--check", "--bundle", str(stubbed_bundle), "--out", str(out)]
    )

    captured = capsys.readouterr()
    assert rc == 0, captured.err
    assert "fixture matches the banked bundle" in captured.out
    assert captured.err == ""


def test_write_mode_warns_but_still_writes_when_the_field_cannot_be_carried(
    tmp_path, stubbed_bundle, capsys
):
    """Write mode stays recoverable: loud, not fatal.

    A first-ever derivation legitimately has nothing to carry from, and a
    fixture written without the field is repaired by restoring the field — so
    this must not block. It must not be silent either.
    """
    out = tmp_path / "fixture"

    rc = derive_fixture.main(["--bundle", str(stubbed_bundle), "--out", str(out)])

    captured = capsys.readouterr()
    assert rc == 0
    assert (out / "expected_outcome.json").exists()
    assert "anchor_replay" in captured.err
    assert "writ" in captured.err.lower()
    assert "by hand" in captured.err
