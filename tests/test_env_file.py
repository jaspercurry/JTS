# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The shared systemd EnvironmentFile helper (jasper.env_file)."""

from __future__ import annotations

import pytest

from jasper import atomic_io, env_file


@pytest.mark.parametrize(
    ("text", "key", "value", "expected_changed", "expected_new"),
    [
        pytest.param(
            "# header\nA=1\nB=2\n\nC=3\n",
            "B",
            "9",
            True,
            "# header\nA=1\nB=9\n\nC=3\n",
            id="replaces_in_place_preserving_other_lines",
        ),
        pytest.param("A=1\n", "B", "2", True, "A=1\nB=2\n", id="appends_when_absent"),
        pytest.param(
            "A=1\nB=2\n", "B", "2", False, "A=1\nB=2\n", id="unchanged_when_value_identical"
        ),
        pytest.param(
            # systemd is last-wins; a clean upsert collapses to ONE canonical
            # line so the file does not accumulate stale duplicates across
            # reconciles.
            "B=old\nA=1\nB=stale\n",
            "B",
            "new",
            True,
            "B=new\nA=1\n",
            id="dedupes_later_duplicate_assignments",
        ),
    ],
)
def test_upsert_basic_value_changes(text, key, value, expected_changed, expected_new):
    new, changed = env_file.upsert(text, key, value)
    assert changed is expected_changed
    assert new == expected_new


def test_upsert_quoted_value_compares_unquoted():
    new, changed = env_file.upsert('B="2"\n', "B", "2")
    # The stored value already resolves to 2, so no rewrite.
    assert changed is False
    assert new == 'B="2"\n'


def test_upsert_spaced_input_is_changed_false_value_resolves():
    # Documented limitation: a hand-written `KEY = value` already resolving to
    # the desired value yields changed=False, so the caller SKIPS the write and
    # discards new_text. We pin the contract that matters (no spurious rewrite)
    # rather than a byte-exact string.
    new, changed = env_file.upsert("B = 2\n", "B", "2")
    assert changed is False
    assert env_file.read_value(new, "B") == "2"


def test_upsert_rewrite_canonicalizes_assignments_but_keeps_comments():
    # When a rewrite IS triggered, assignment lines canonicalize to KEY=value
    # (key-side spacing dropped) but comments + blanks survive verbatim.
    new, changed = env_file.upsert("# note\nA = 1\n\nB=2\n", "B", "9")
    assert changed is True
    assert "# note" in new and "\n\n" in new  # comment + blank verbatim
    # Other assignments still resolve correctly (key-side spacing canonicalized).
    assert env_file.read_value(new, "A") == "1"
    assert env_file.read_value(new, "B") == "9"


@pytest.mark.parametrize(
    ("text", "key", "expected_changed", "expected_new"),
    [
        pytest.param(
            "A=1\n# c\nB=2\n", "A", True, "# c\nB=2\n", id="strips_key_preserving_others"
        ),
        pytest.param("A=1\n", "A", True, "", id="to_empty_returns_empty_string"),
        pytest.param("A=1\n", "Z", False, "A=1\n", id="absent_key_is_noop"),
    ],
)
def test_remove(text, key, expected_changed, expected_new):
    new, changed = env_file.remove(text, key)
    assert changed is expected_changed
    assert new == expected_new


def test_read_value_last_wins_and_strips_quotes():
    assert env_file.read_value("A=1\nA='2'\n", "A") == "2"
    assert env_file.read_value("# c\nB = 3 \n", "B") == "3"
    assert env_file.read_value("A=1\n", "Z") is None


def test_malformed_and_comment_lines_round_trip():
    text = "not-an-assignment\n#comment\nA=1\n"
    parsed = env_file.parse_env_lines(text)
    assert parsed == [("not-an-assignment", None), ("#comment", None), ("A", "1")]
    # Upserting a new key leaves the verbatim lines untouched.
    new, _ = env_file.upsert(text, "A", "2")
    assert new == "not-an-assignment\n#comment\nA=2\n"


def test_read_env_file_is_empty_for_a_missing_file(tmp_path):
    assert env_file.read_env_file(str(tmp_path / "nope.env")) == {}


def test_read_env_file_resolves_quotes_and_skips_malformed_lines(tmp_path):
    path = tmp_path / "wizard.env"
    path.write_text(
        'JASPER_PROVIDER="acme"\n'
        "JASPER_MODEL='small'\n"
        "MALFORMED\n",
    )

    assert env_file.read_env_file(str(path)) == {
        "JASPER_PROVIDER": "acme",
        "JASPER_MODEL": "small",
    }


def test_delete_env_file_is_idempotent(tmp_path):
    path = tmp_path / "gone.env"
    atomic_io.write_env_file(str(path), {"A": "1"})
    env_file.delete_env_file(str(path))
    assert not path.exists()
    env_file.delete_env_file(str(path))
    assert not path.exists()
