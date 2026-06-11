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


ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "scripts" / "_lib.sh"


def _run(remote_id: str, env_file: Path, accept_new: str = "") -> subprocess.CompletedProcess:
    # _lib.sh sources REPO_ROOT/.env.local at load time; that's harmless
    # here (we pass the env file under test explicitly as an argument).
    script = (
        f'source "{LIB}"; '
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


def test_skips_without_state_file(tmp_path):
    proc = _run("uuid-1", tmp_path / "absent.env")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "no_state_file"


def test_first_contact_records_tofu(tmp_path):
    env = tmp_path / ".env.local"
    env.write_text("PI_HOST=jts.local\nPI_USER=pi\n")
    proc = _run("uuid-1", env)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "recorded"
    assert "PI_PEER_ID=uuid-1\n" in env.read_text()
    # Existing lines survive.
    assert "PI_HOST=jts.local\n" in env.read_text()


def test_match_on_same_identity(tmp_path):
    env = tmp_path / ".env.local"
    env.write_text("PI_HOST=jts.local\nPI_PEER_ID=uuid-1\n")
    proc = _run("uuid-1", env)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "match"


def test_whitespace_in_remote_id_is_normalized(tmp_path):
    # `ssh ... cat` output carries a trailing newline; the guard must not
    # treat it as a different identity.
    env = tmp_path / ".env.local"
    env.write_text("PI_PEER_ID=uuid-1\n")
    proc = _run("uuid-1\n", env)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "match"


def test_mismatch_returns_1(tmp_path):
    env = tmp_path / ".env.local"
    env.write_text("PI_PEER_ID=uuid-1\n")
    proc = _run("uuid-OTHER", env)
    assert proc.returncode == 1
    assert proc.stdout.strip().startswith("mismatch")
    assert "uuid-1" in proc.stdout and "uuid-OTHER" in proc.stdout
    # The recorded identity is NOT silently replaced.
    assert "PI_PEER_ID=uuid-1\n" in env.read_text()


def test_accept_new_rerecords(tmp_path):
    env = tmp_path / ".env.local"
    env.write_text("PI_HOST=jts.local\nPI_PEER_ID=uuid-1\n")
    proc = _run("uuid-NEW", env, accept_new="1")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "rerecorded"
    content = env.read_text()
    assert "PI_PEER_ID=uuid-NEW\n" in content
    assert "uuid-1" not in content
    assert "PI_HOST=jts.local\n" in content
