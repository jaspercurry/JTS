# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The shared order-preserving env-file upsert helper (jasper.env_file)."""

from __future__ import annotations

from jasper import env_file


def test_upsert_replaces_in_place_preserving_other_lines():
    text = "# header\nA=1\nB=2\n\nC=3\n"
    new, changed = env_file.upsert(text, "B", "9")
    assert changed is True
    # Other keys, the comment, AND the blank line survive verbatim and in order.
    assert new == "# header\nA=1\nB=9\n\nC=3\n"


def test_upsert_appends_when_absent():
    new, changed = env_file.upsert("A=1\n", "B", "2")
    assert changed is True
    assert new == "A=1\nB=2\n"


def test_upsert_unchanged_when_value_identical():
    new, changed = env_file.upsert("A=1\nB=2\n", "B", "2")
    assert changed is False
    assert new == "A=1\nB=2\n"


def test_upsert_dedupes_later_duplicate_assignments():
    # systemd is last-wins; a clean upsert collapses to ONE canonical line so
    # the file does not accumulate stale duplicates across reconciles.
    new, changed = env_file.upsert("B=old\nA=1\nB=stale\n", "B", "new")
    assert changed is True
    assert new == "B=new\nA=1\n"


def test_upsert_quoted_value_compares_unquoted():
    new, changed = env_file.upsert('B="2"\n', "B", "2")
    # The stored value already resolves to 2, so no rewrite.
    assert changed is False
    assert new == 'B="2"\n'


def test_remove_strips_key_preserving_others():
    new, changed = env_file.remove("A=1\n# c\nB=2\n", "A")
    assert changed is True
    assert new == "# c\nB=2\n"


def test_remove_to_empty_returns_empty_string():
    new, changed = env_file.remove("A=1\n", "A")
    assert changed is True
    assert new == ""


def test_remove_absent_key_is_noop():
    new, changed = env_file.remove("A=1\n", "Z")
    assert changed is False
    assert new == "A=1\n"


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
