# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Test scripts/_lib.sh `verify_or_record_peer_id` — the deploy-target
identity guard.

mDNS names are transport, not identity: after an Avahi collision rename
or a re-image, PI_HOST can resolve to a different speaker than the
checkout means. deploy-to-pi.sh records the target's stable peer_id
into .env.local on first contact (TOFU) and aborts BEFORE rsync on a
later mismatch. This pins the helper's outcome tokens and return codes,
sourced under bash with the paths pointed at tmp_path.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "scripts" / "_lib.sh"

# install.sh writes peer_id as `python3 -c 'import uuid; print(uuid.uuid4())'`,
# and only that shape is an identity (see verify_or_record_peer_id).
ONE = "6f1a2b3c-4d5e-4f60-8a91-0123456789ab"
OTHER = "0badc0de-1111-4222-8333-444455556666"
NEW = "11112222-3333-4444-8555-666677778888"


def _run(remote_id: str, env_file: Path, accept_new: str = "") -> subprocess.CompletedProcess:
    # _lib.sh sources REPO_ROOT/.env.local at load time; that's harmless
    # here (we pass the env file under test explicitly as an argument).
    script = (
        f'JTS_LIB_TARGET_OPTIONAL=1 source "{LIB}"; '
        f'verify_or_record_peer_id "$1" "$2" "$3"'
    )
    return subprocess.run(
        ["bash", "-c", script, "bash", remote_id, str(env_file), accept_new],
        capture_output=True, text=True, timeout=30,
    )


def test_unavailable_when_remote_has_no_peer_id(tmp_path):
    env = tmp_path / ".env.local"
    env.write_text("PI_HOST=jts.local\n")
    proc = _run("", env)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "unavailable"
    assert "PI_PEER_ID" not in env.read_text()


@pytest.mark.parametrize(
    "remote_id",
    ["[sudo] password for pi:", "cat: /var/lib/jasper/peer_id: No such file",
     "6f1a2b3c4d5e4f608a910123456789ab", "6F1A2B3C-4D5E-4F60-8A91-0123456789AB"],
    ids=["sudo-prompt", "error-text", "no-hyphens", "uppercase"],
)
def test_a_read_that_is_not_a_uuid_is_never_an_identity(tmp_path, remote_id):
    # A capture can pick up prompt or error text where a peer_id should
    # be; recording that would bless the wrong Pi on every later deploy.
    env = tmp_path / ".env.local"
    env.write_text("PI_HOST=jts.local\n")
    proc = _run(remote_id, env)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "unavailable"
    assert "PI_PEER_ID" not in env.read_text()


def test_skips_without_state_file(tmp_path):
    proc = _run(ONE, tmp_path / "absent.env")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "no_state_file"


def test_first_contact_records_tofu(tmp_path):
    env = tmp_path / ".env.local"
    env.write_text("PI_HOST=jts.local\nPI_USER=pi\n")
    proc = _run(ONE, env)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "recorded"
    assert f"PI_PEER_ID={ONE}\n" in env.read_text()
    # Existing lines survive.
    assert "PI_HOST=jts.local\n" in env.read_text()


def test_match_on_same_identity(tmp_path):
    env = tmp_path / ".env.local"
    env.write_text(f"PI_HOST=jts.local\nPI_PEER_ID={ONE}\n")
    proc = _run(ONE, env)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "match"


def test_whitespace_in_remote_id_is_normalized(tmp_path):
    # `ssh ... cat` output carries a trailing newline; the guard must not
    # treat it as a different identity.
    env = tmp_path / ".env.local"
    env.write_text(f"PI_PEER_ID={ONE}\n")
    proc = _run(f"{ONE}\n", env)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "match"


def test_mismatch_returns_1(tmp_path):
    env = tmp_path / ".env.local"
    env.write_text(f"PI_PEER_ID={ONE}\n")
    proc = _run(OTHER, env)
    assert proc.returncode == 1
    assert proc.stdout.strip().startswith("mismatch")
    assert ONE in proc.stdout and OTHER in proc.stdout
    # The recorded identity is NOT silently replaced.
    assert f"PI_PEER_ID={ONE}\n" in env.read_text()


def test_accept_new_rerecords(tmp_path):
    env = tmp_path / ".env.local"
    env.write_text(f"PI_HOST=jts.local\nPI_PEER_ID={ONE}\n")
    proc = _run(NEW, env, accept_new="1")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "rerecorded"
    content = env.read_text()
    assert f"PI_PEER_ID={NEW}\n" in content
    assert ONE not in content
    assert "PI_HOST=jts.local\n" in content


def test_append_survives_missing_trailing_newline(tmp_path):
    """A hand-edited .env.local without a trailing newline must not get
    PI_PEER_ID glued onto its last line (silently corrupting it)."""
    env = tmp_path / ".env.local"
    env.write_text("PI_HOST=jts.local\nPI_USER=pi")  # no trailing \n
    proc = _run(ONE, env)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "recorded"
    content = env.read_text()
    assert "PI_USER=pi\n" in content
    assert f"PI_PEER_ID={ONE}\n" in content
    assert "piPI_PEER_ID" not in content
