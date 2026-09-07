# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The shared systemd EnvironmentFile helper (jasper.env_file)."""

from __future__ import annotations

import os
import threading

import pytest

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


def test_write_env_file_round_trips_at_the_default_secret_mode(tmp_path):
    # API keys live in these files; a wider default would leak them under a
    # daemon-readable path.
    path = tmp_path / "v.env"
    env_file.write_env_file(str(path), {"A_KEY": "abc", "PROVIDER": "acme"})
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert env_file.read_env_file(str(path)) == {"A_KEY": "abc", "PROVIDER": "acme"}


def test_write_env_file_rejects_a_newline_value_leaving_the_file_intact(tmp_path):
    # systemd's parser neither quotes nor escapes, so a newline would land a
    # bogus second assignment. Rejecting mid-write must publish nothing.
    path = tmp_path / "v.env"
    env_file.write_env_file(str(path), {"OK": "first"})
    with pytest.raises(ValueError):
        env_file.write_env_file(str(path), {"OK": "second", "BAD": "no\nline"})
    assert env_file.read_env_file(str(path)) == {"OK": "first"}
    assert [f for f in os.listdir(tmp_path) if f.endswith(".tmp")] == []


def test_write_env_file_never_publishes_a_mixed_file_under_concurrent_writers(
    tmp_path,
):
    # The threaded wizard server runs several /save handlers against one file.
    # Each publish must land whole -- never byte-mixed -- and leak no temp.
    path = str(tmp_path / "race.env")
    values = [f"value_{i}_" + "x" * 200 for i in range(8)]
    errors: list[Exception] = []

    def writer(v):
        try:
            for _ in range(50):
                env_file.write_env_file(path, {"V": v})
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(v,)) for v in values]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert (tmp_path / "race.env").read_text() in {f"V={v}\n" for v in values}
    assert [f for f in os.listdir(tmp_path) if f.endswith(".tmp")] == []


def test_delete_env_file_is_idempotent(tmp_path):
    path = tmp_path / "gone.env"
    env_file.write_env_file(str(path), {"A": "1"})
    env_file.delete_env_file(str(path))
    assert not path.exists()
    env_file.delete_env_file(str(path))
    assert not path.exists()
