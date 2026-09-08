# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Installer compartment moves, permissions, and source cleanup."""
from __future__ import annotations

import os
import shlex
import stat
import subprocess
from pathlib import Path

import pytest

from jasper.env_file import read_env_file
from tests._lock_holder import spawn_lock_holder

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "deploy" / "lib" / "install" / "env-migrations.sh"
ENV_LIB = ROOT / "deploy" / "lib" / "jasper-env-file.sh"

# `getent` stubbed to succeed so the `getent group jasper-secrets` guard passes;
# the chgrp/chown/systemd-tmpfiles become no-ops; `install` emulates just enough
# of `install -d ... DIR` (mkdir -p) and skips the file-copy form.
_STUBS = r"""
getent() { return 0; }
chgrp() { :; }
chown() { :; }
systemd-tmpfiles() { :; }
install() {
  local d=0 dirs=()
  while [ $# -gt 0 ]; do
    case "$1" in
      -d) d=1; shift ;;
      -m|-g|-o) shift 2 ;;
      *) dirs+=("$1"); shift ;;
    esac
  done
  [ "$d" = 1 ] && mkdir -p "${dirs[@]}"
  return 0
}
"""

def _prep(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create + return the (etc, state, secrets) dirs. Tests must call this
    before writing fixture files (the migration treats these as pre-existing)."""
    dirs = tuple(tmp_path / d for d in ("etc", "state", "secrets"))
    for d in dirs:
        d.mkdir(exist_ok=True)
    (tmp_path / "intsecrets").mkdir(exist_ok=True)
    return dirs  # type: ignore[return-value]


def _run(tmp_path: Path, fn: str) -> subprocess.CompletedProcess[str]:
    _prep(tmp_path)
    env = {
        "PATH": os.environ["PATH"],
        "REPO_DIR": str(ROOT),
        "ENV_DIR": str(tmp_path / "etc"),
        "STATE_DIR": str(tmp_path / "state"),
        "SECRETS_DIR": str(tmp_path / "secrets"),
        "INTSECRETS_DIR": str(tmp_path / "intsecrets"),
    }
    return subprocess.run(
        ["/bin/bash", "-euc",
         f". {shlex.quote(str(ENV_LIB))}\n. {shlex.quote(str(LIB))}\n{_STUBS}\n{fn}"],
        env=env,
        capture_output=True,
        text=True,
    )


_MIGRATIONS = [
    ("migrate_voice_keys_split", "voice_keys.env", key)
    for key in ("GEMINI_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY")
] + [("migrate_google_routes_key", "google_routes.env", "GOOGLE_ROUTES_API_KEY")]


@pytest.mark.parametrize("fn,filename,key", _MIGRATIONS)
@pytest.mark.parametrize("value", ["operator-seed", ' space "quote" \\path ', "it's a seed", ""])
@pytest.mark.parametrize("canonical", [None, "wizard-key", ""])
def test_secret_move_preserves_values_and_removes_broad_copies(
    tmp_path: Path, fn: str, filename: str, key: str, value: str, canonical: str | None,
):
    etc, _state, secrets = _prep(tmp_path)
    broad, target = etc / "jasper.env", secrets / filename
    encoded = value.replace("\\", "\\\\").replace('"', '\\"')
    broad.write_text(f'JASPER_HOSTNAME=jts.local\n{key}=stale\n  {key} = "{encoded}"\r\n')
    broad.chmod(0o640)
    if canonical is not None:
        target.write_text(f"{key}={canonical}\n" if canonical else f"{key}=''\n")  # jasper_env_quote_value's empty
        target.chmod(0o640)
    owner = (broad.stat().st_uid, broad.stat().st_gid)
    assigned = canonical or value
    for _ in range(2):
        proc = _run(tmp_path, fn)
        assert proc.returncode == 0, proc.stderr
        assert target.exists() is (canonical is not None or bool(value))
        assert read_env_file(target) == ({key: assigned} if target.exists() else {})
        assert read_env_file(broad) == {"JASPER_HOSTNAME": "jts.local", **({} if assigned else {key: ""})}
        for path in filter(Path.exists, (broad, target)):
            assert stat.S_IMODE(path.stat().st_mode) == 0o640
            assert (path.stat().st_uid, path.stat().st_gid) == owner
        assert not list(tmp_path.rglob("*.bak"))
        assert "operator-seed" not in proc.stdout + proc.stderr
        assert "wizard-key" not in proc.stdout + proc.stderr


@pytest.mark.parametrize("fn,filename,key", _MIGRATIONS)
@pytest.mark.parametrize("locked_file", ["source", "target"])
def test_secret_move_waits_for_other_writers(
    tmp_path: Path, fn: str, filename: str, key: str, locked_file: str,
):
    etc, _state, secrets = _prep(tmp_path)
    broad, target = etc / "jasper.env", secrets / filename
    broad.write_text(f"{key}=operator-seed\n")
    path = broad if locked_file == "source" else target
    addition = "KEEP=concurrent\n" if locked_file == "source" else f"{key}=wizard-key\n"
    with spawn_lock_holder(path, hold_seconds=0.5, write_back=addition):
        proc = _run(tmp_path, fn)
    assert proc.returncode == 0, proc.stderr
    assert read_env_file(target) == {key: "operator-seed" if locked_file == "source" else "wizard-key"}
    assert read_env_file(broad) == ({"KEEP": "concurrent"} if locked_file == "source" else {})


@pytest.mark.parametrize("fn,filename,key", _MIGRATIONS)
def test_secret_move_leaves_source_when_publish_fails(
    tmp_path: Path, fn: str, filename: str, key: str,
):
    etc, _state, secrets = _prep(tmp_path)
    broad = etc / "jasper.env"
    broad.write_text(f"{key}=operator-seed\n")
    proc = _run(tmp_path, f"mv() {{ return 1; }}\n{fn}")
    assert proc.returncode != 0
    assert read_env_file(broad) == {key: "operator-seed"}
    assert not (secrets / filename).exists()
    assert "operator-seed" not in proc.stdout + proc.stderr


# --- reassert_secrets_compartment_perms (Phase 4a mode re-narrow) ------------

def test_phase4a_retightens_over_exposed_voice_keys_mode(tmp_path: Path):
    """A pre-existing voice_keys.env manually widened to o+r (0644) must be
    re-narrowed to 0640 on the next deploy. migrate_voice_keys_split only chmods
    when it WRITES the file; the key here is already split, so without the
    re-assert re-tighten the 0644 would survive — a silent confidentiality
    regression. The key value must be preserved (mode-only change)."""
    _etc, _state, secrets = _prep(tmp_path)
    keys = secrets / "voice_keys.env"
    keys.write_text("GEMINI_API_KEY=AIza-x\n")
    os.chmod(keys, 0o644)

    proc = _run(tmp_path, "reassert_secrets_compartment_perms")
    assert proc.returncode == 0, proc.stderr

    assert stat.S_IMODE(keys.stat().st_mode) == 0o640, "voice_keys.env must re-narrow to 0640"
    assert read_env_file(keys) == {"GEMINI_API_KEY": "AIza-x"}, "the key value must be preserved"


def test_phase4a_retighten_is_idempotent_for_correct_voice_keys(tmp_path: Path):
    """An already-0640 voice_keys.env stays 0640 (the re-tighten is a no-op when
    the mode is already correct)."""
    _etc, _state, secrets = _prep(tmp_path)
    keys = secrets / "voice_keys.env"
    keys.write_text("OPENAI_API_KEY=sk-x\n")
    os.chmod(keys, 0o640)

    proc = _run(tmp_path, "reassert_secrets_compartment_perms")
    assert proc.returncode == 0, proc.stderr

    assert stat.S_IMODE(keys.stat().st_mode) == 0o640


def test_secrets_compartment_reassert_is_idempotent(tmp_path: Path):
    """The Google tree's registry + per-account tokens re-narrow to 0640 and
    their contents survive, and a second run changes nothing further."""
    _etc, _state, secrets = _prep(tmp_path)
    google = secrets / "google"
    (google / "tokens").mkdir(parents=True)
    accounts = google / "accounts.json"
    accounts.write_text(f'{{"accounts": [{{"token_path": "{google}/tokens/J.json"}}]}}\n')
    token = google / "tokens" / "J.json"
    token.write_text('{"refresh_token": "rt"}\n')
    os.chmod(accounts, 0o644)
    os.chmod(token, 0o644)

    first = _run(tmp_path, "reassert_secrets_compartment_perms")
    assert first.returncode == 0, first.stderr
    assert stat.S_IMODE(accounts.stat().st_mode) == 0o640
    assert stat.S_IMODE(token.stat().st_mode) == 0o640
    settled = accounts.read_text()

    second = _run(tmp_path, "reassert_secrets_compartment_perms")
    assert second.returncode == 0, second.stderr
    assert stat.S_IMODE(accounts.stat().st_mode) == 0o640
    assert stat.S_IMODE(token.stat().st_mode) == 0o640
    assert accounts.read_text() == settled
    assert token.read_text() == '{"refresh_token": "rt"}\n'


# --- reassert_intsecrets_compartment_perms (Phase 4b mode re-narrow) ---------

def test_intsecrets_compartment_reassert_is_idempotent(tmp_path: Path):
    """Same contract for the integration-secret compartment: the HA token, the
    Spotify registry, and each per-account cache re-narrow to 0640 and keep
    their contents across repeated deploys."""
    _etc, _state, _secrets = _prep(tmp_path)
    intsecrets = tmp_path / "intsecrets"
    spotify = intsecrets / "spotify"
    (spotify / "caches").mkdir(parents=True)
    accounts = spotify / "accounts.json"
    accounts.write_text(f'{{"accounts": [{{"cache_path": "{spotify}/caches/j.json"}}]}}\n')
    cache = spotify / "caches" / "j.json"
    cache.write_text('{"refresh_token": "rt"}\n')
    ha = intsecrets / "home_assistant.env"
    ha.write_text("JASPER_HA_TOKEN=ha-token\n")
    for path in (accounts, cache, ha):
        os.chmod(path, 0o644)

    first = _run(tmp_path, "reassert_intsecrets_compartment_perms")
    assert first.returncode == 0, first.stderr
    for path in (accounts, cache, ha):
        assert stat.S_IMODE(path.stat().st_mode) == 0o640, path
    settled = accounts.read_text()

    second = _run(tmp_path, "reassert_intsecrets_compartment_perms")
    assert second.returncode == 0, second.stderr
    for path in (accounts, cache, ha):
        assert stat.S_IMODE(path.stat().st_mode) == 0o640, path
    assert accounts.read_text() == settled
    assert cache.read_text() == '{"refresh_token": "rt"}\n'
    assert read_env_file(ha) == {"JASPER_HA_TOKEN": "ha-token"}
