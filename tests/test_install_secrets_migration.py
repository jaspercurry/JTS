# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the secret compartments in deploy/lib/install/env-migrations.sh —
`reassert_secrets_compartment_perms` and `reassert_intsecrets_compartment_perms` (which
re-narrow each compartment's ownership and modes on every deploy), plus the two key
relocations that still have a producer: `migrate_voice_keys_split` and
`migrate_google_routes_key`, which move an operator-seeded key out of /etc/jasper/jasper.env
into the jasper-secrets compartment.

These are the most confidentiality-sensitive bash in the installer, so the
safety properties get pinned here: the mode re-narrow, idempotency, and the
"never strip a key from jasper.env until it is confirmed in voice_keys.env"
guard.

CI has no root and no `jasper-secrets` group, so the privileged ops the
functions call (`getent`, `chgrp`, `chown`, `install -d -g`, `systemd-tmpfiles`)
are stubbed; the file-munging under test (mv / sed / grep / touch / printf /
chmod) runs for real against tmp paths.
"""
from __future__ import annotations

import os
import shlex
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "deploy" / "lib" / "install" / "env-migrations.sh"
SED_LIB = ROOT / "deploy" / "lib" / "jasper-sed-inplace.sh"

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

_FUNCS = (
    "ensure_secrets_dir",
    "ensure_intsecrets_dir",
    "reassert_secrets_compartment_perms",
    "reassert_intsecrets_compartment_perms",
    "migrate_voice_keys_split",
    "migrate_google_routes_key",
    "_strip_key_from_broad",
)


def _extract(name: str) -> str:
    out = subprocess.run(
        ["bash", "-c", rf"sed -n '/^{name}()/,/^}}/p' '{LIB}'"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert f"{name}()" in out, f"could not extract {name} from {LIB}"
    return out


def _helpers() -> str:
    # install.sh sources the shared in-place-sed helper for the whole
    # install lib; extracted functions need it too.
    return (
        f". {shlex.quote(str(SED_LIB))}\n"
        + _STUBS
        + "\n".join(_extract(n) for n in _FUNCS)
    )


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
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "REPO_DIR": str(ROOT),
        "ENV_DIR": str(tmp_path / "etc"),
        "STATE_DIR": str(tmp_path / "state"),
        "SECRETS_DIR": str(tmp_path / "secrets"),
        "INTSECRETS_DIR": str(tmp_path / "intsecrets"),
    }
    return subprocess.run(
        ["/bin/bash", "-c", f"{_helpers()}\n{fn}"],
        env=env,
        capture_output=True,
        text=True,
    )


def _kv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                out[key] = value
    return out


# --- migrate_voice_keys_split -------------------------------------------------

def test_split_moves_operator_seeded_key_from_jasper_env(tmp_path: Path):
    etc, _state, secrets = _prep(tmp_path)
    (etc / "jasper.env").write_text(
        "JASPER_HOSTNAME=jts.local\n"
        "XAI_API_KEY=xai-operator-seed\n"
    )

    proc = _run(tmp_path, "migrate_voice_keys_split")
    assert proc.returncode == 0, proc.stderr

    assert _kv(secrets / "voice_keys.env") == {"XAI_API_KEY": "xai-operator-seed"}
    # The non-secret hostname stays; the key is stripped from jasper.env.
    je = _kv(etc / "jasper.env")
    assert je == {"JASPER_HOSTNAME": "jts.local"}


def test_split_is_idempotent(tmp_path: Path):
    etc, _state, secrets = _prep(tmp_path)
    (etc / "jasper.env").write_text(
        "JASPER_HOSTNAME=jts.local\nGEMINI_API_KEY=AIza-x\n"
    )

    first = _run(tmp_path, "migrate_voice_keys_split")
    assert first.returncode == 0, first.stderr
    second = _run(tmp_path, "migrate_voice_keys_split")
    assert second.returncode == 0, second.stderr

    # Re-run does not duplicate or clobber the key, and the broad file stays clean.
    assert _kv(secrets / "voice_keys.env") == {"GEMINI_API_KEY": "AIza-x"}
    assert secrets.joinpath("voice_keys.env").read_text().count("GEMINI_API_KEY=") == 1
    assert "GEMINI_API_KEY" not in _kv(etc / "jasper.env")


def test_split_keeps_existing_keys_file_value_and_strips_broad(tmp_path: Path):
    """If voice_keys.env already declares a key (the wizard's normal path),
    the split must NOT overwrite it — it only cleans a stale jasper.env seed."""
    etc, _state, secrets = _prep(tmp_path)
    (secrets / "voice_keys.env").write_text("OPENAI_API_KEY=sk-canonical\n")
    # A stale operator seed lingering in the broad file:
    (etc / "jasper.env").write_text(
        "JASPER_HOSTNAME=jts.local\nOPENAI_API_KEY=sk-stale\n"
    )

    proc = _run(tmp_path, "migrate_voice_keys_split")
    assert proc.returncode == 0, proc.stderr

    # keys_env value preserved (canonical wins); broad copy stripped.
    assert _kv(secrets / "voice_keys.env") == {"OPENAI_API_KEY": "sk-canonical"}
    assert _kv(etc / "jasper.env") == {"JASPER_HOSTNAME": "jts.local"}


# --- migrate_google_routes_key ----------------------------------------------

def test_google_routes_key_moves_from_jasper_env_to_secrets(tmp_path: Path):
    etc, _state, secrets = _prep(tmp_path)
    (etc / "jasper.env").write_text(
        "JASPER_HOSTNAME=jts.local\n"
        "GOOGLE_ROUTES_API_KEY=AIzaSySynthetic-Test_Key\n"
    )

    proc = _run(tmp_path, "migrate_google_routes_key")
    assert proc.returncode == 0, proc.stderr

    assert _kv(secrets / "google_routes.env") == {
        "GOOGLE_ROUTES_API_KEY": "AIzaSySynthetic-Test_Key",
    }
    assert _kv(etc / "jasper.env") == {"JASPER_HOSTNAME": "jts.local"}
    assert stat.S_IMODE((secrets / "google_routes.env").stat().st_mode) == 0o640


def test_google_routes_key_preserves_existing_secret_and_strips_stale_broad(
    tmp_path: Path,
):
    etc, _state, secrets = _prep(tmp_path)
    (secrets / "google_routes.env").write_text(
        "GOOGLE_ROUTES_API_KEY=AIzaSyCanonical\n",
    )
    (etc / "jasper.env").write_text("GOOGLE_ROUTES_API_KEY=AIzaSyStaleEnv\n")

    proc = _run(tmp_path, "migrate_google_routes_key")
    assert proc.returncode == 0, proc.stderr

    assert _kv(secrets / "google_routes.env") == {
        "GOOGLE_ROUTES_API_KEY": "AIzaSyCanonical",
    }
    assert "GOOGLE_ROUTES_API_KEY" not in _kv(etc / "jasper.env")


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
    assert _kv(keys) == {"GEMINI_API_KEY": "AIza-x"}, "the key value must be preserved"


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
    assert _kv(ha) == {"JASPER_HA_TOKEN": "ha-token"}
