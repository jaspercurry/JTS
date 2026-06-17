"""Unit tests for the household credential (device-to-device control auth).

The household secret is the M2M counterpart to the per-device control token:
ONE secret per household, minted at the /rooms/ bond, distributed to each
member, and presented as X-JTS-Household on the cross-device /grouping/set. A
near-line-for-line clone of control_token.py, so these mirror
test_control_token.py — but they pin the load-bearing DIFFERENCES the design
turns on (docs/HANDOFF-control-plane-auth.md §6):

- FAIL-SAFE direction (absent/empty/unreadable ⇒ accept) — a refactor flipping
  it to fail-closed would deadlock first-bond bootstrap and brick re-bonding.
- adopt() is trust-on-first-use and REFUSES to overwrite an existing secret.
- clear() drops the secret (unbond) so a speaker can re-pair.

The route-level gate + bootstrap/recovery regressions live in
test_control_server.py against the real ThreadingHTTPServer.
"""
from __future__ import annotations

import inspect
import os
import stat

from jasper.control import household_credential as hc


# --- verify: FAIL-SAFE direction (the invariant the self-heal path needs) ---


def test_verify_accepts_anything_when_absent(monkeypatch, tmp_path):
    """Not-yet-paired (no file) ⇒ accept any input incl. None. This is what
    keeps the first bond — which DISTRIBUTES the secret over the gated route —
    from being rejected by the gate it installs, and lets a secret-lost follower
    re-bond. The OPPOSITE of fail-closed, and deliberate."""
    monkeypatch.setattr(hc, "SECRET_FILE", str(tmp_path / "nope"))
    assert hc.verify(None) is True
    assert hc.verify("") is True
    assert hc.verify("anything") is True


def test_verify_accepts_when_empty_or_whitespace(monkeypatch, tmp_path):
    path = tmp_path / "household_secret"
    path.write_text("   \n\t\n")  # whitespace-only strips to ""
    monkeypatch.setattr(hc, "SECRET_FILE", str(path))
    assert hc.verify(None) is True
    assert hc.verify("anything") is True


def test_verify_accepts_when_unreadable(monkeypatch, tmp_path):
    """An unreadable secret file (here: a directory in its place → OSError on
    open) resolves to "" and accepts — a read error must never 500 the grouping
    request, and must not brick a follower whose file is transiently broken."""
    a_dir = tmp_path / "household_secret"
    a_dir.mkdir()
    monkeypatch.setattr(hc, "SECRET_FILE", str(a_dir))
    assert hc._stored_secret() == ""
    assert hc.verify("anything") is True


def test_verify_requires_exact_match_when_present(monkeypatch, tmp_path):
    path = tmp_path / "household_secret"
    path.write_text("the-secret-value\n")  # trailing newline must not break compare
    monkeypatch.setattr(hc, "SECRET_FILE", str(path))
    assert hc.verify("the-secret-value") is True
    assert hc.verify("wrong") is False
    assert hc.verify(None) is False
    assert hc.verify("") is False


def test_verify_uses_constant_time_compare():
    """compare_digest, never ==, so the secret's length/prefix doesn't leak via
    timing (mirrors control_token.verify)."""
    src = inspect.getsource(hc.verify)
    assert "compare_digest" in src
    assert "==" not in src.replace("!=", "")


# --- is_paired / current ---------------------------------------------------


def test_is_paired_reflects_file(monkeypatch, tmp_path):
    path = tmp_path / "household_secret"
    monkeypatch.setattr(hc, "SECRET_FILE", str(path))
    assert hc.is_paired() is False
    path.write_text("s\n")
    assert hc.is_paired() is True


def test_current_matches_verify_path(monkeypatch, tmp_path):
    path = tmp_path / "household_secret"
    monkeypatch.setattr(hc, "SECRET_FILE", str(path))
    assert hc.current() == ""  # absent → "", no raise
    secret = hc.ensure()
    assert hc.current() == secret


# --- ensure: mint at /bond (NOT install/startup) ---------------------------


def test_ensure_generates_when_absent(monkeypatch, tmp_path):
    path = tmp_path / "household_secret"
    monkeypatch.setattr(hc, "SECRET_FILE", str(path))
    assert hc.is_paired() is False
    secret = hc.ensure()
    assert secret and len(secret) >= 16
    assert path.read_text().strip() == secret
    assert hc.is_paired() is True
    assert hc.verify(secret) is True
    assert hc.verify("nope") is False


def test_ensure_is_0600(monkeypatch, tmp_path):
    path = tmp_path / "household_secret"
    monkeypatch.setattr(hc, "SECRET_FILE", str(path))
    hc.ensure()
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600, f"secret file is {oct(mode)}, expected 0o600"


def test_ensure_is_idempotent_and_never_rotates(monkeypatch, tmp_path):
    path = tmp_path / "household_secret"
    path.write_text("existing-household-secret\n")
    monkeypatch.setattr(hc, "SECRET_FILE", str(path))
    assert hc.ensure() == "existing-household-secret"
    assert hc.ensure() == "existing-household-secret"
    assert path.read_text().strip() == "existing-household-secret"


# --- adopt: trust-on-first-use distribution, refuse overwrite --------------


def test_adopt_writes_when_absent(monkeypatch, tmp_path):
    path = tmp_path / "household_secret"
    monkeypatch.setattr(hc, "SECRET_FILE", str(path))
    assert hc.adopt("leader-secret") is True
    assert path.read_text().strip() == "leader-secret"
    assert hc.verify("leader-secret") is True
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600


def test_adopt_refuses_to_overwrite_existing(monkeypatch, tmp_path):
    """A member already paired must NOT be silently re-keyed by a different
    X-JTS-Household (the residual a shared secret can't close). Overwrite needs
    an explicit unbond-then-rebond."""
    path = tmp_path / "household_secret"
    path.write_text("ours\n")
    monkeypatch.setattr(hc, "SECRET_FILE", str(path))
    assert hc.adopt("someone-elses") is False
    assert path.read_text().strip() == "ours"  # unchanged


def test_adopt_empty_or_none_is_noop(monkeypatch, tmp_path):
    path = tmp_path / "household_secret"
    monkeypatch.setattr(hc, "SECRET_FILE", str(path))
    assert hc.adopt("") is False
    assert hc.adopt(None) is False
    assert not path.exists()


# --- clear: drop on unbond, idempotent -------------------------------------


def test_clear_removes_file(monkeypatch, tmp_path):
    path = tmp_path / "household_secret"
    path.write_text("s\n")
    monkeypatch.setattr(hc, "SECRET_FILE", str(path))
    hc.clear()
    assert not path.exists()
    assert hc.is_paired() is False
    # After clearing, verify() is fail-safe-open again → the speaker can re-pair.
    assert hc.verify("any-new-household-secret") is True


def test_clear_is_idempotent_when_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(hc, "SECRET_FILE", str(tmp_path / "nope"))
    hc.clear()  # must not raise
    hc.clear()


def test_clear_then_adopt_round_trips_a_rekey(monkeypatch, tmp_path):
    """unbond (clear) → re-bond with a NEW secret (adopt) is the rotation path:
    the only way to replace a stored secret, by design."""
    path = tmp_path / "household_secret"
    path.write_text("old\n")
    monkeypatch.setattr(hc, "SECRET_FILE", str(path))
    assert hc.adopt("new") is False  # refused while "old" present
    hc.clear()
    assert hc.adopt("new") is True
    assert hc.verify("new") is True
    assert hc.verify("old") is False


# --- distinct from control_token (must never blur the two trust domains) ---


def test_distinct_file_and_env_from_control_token():
    from jasper.control import control_token as ct

    assert hc.SECRET_FILE != ct.TOKEN_FILE
    assert "household_secret" in hc.SECRET_FILE
