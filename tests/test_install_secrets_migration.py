# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the WS1 Phase 4 secret-compartment migrations in
deploy/lib/install/env-migrations.sh — `migrate_secrets_phase4a` (moves the
Google token tree + rewrites accounts.json) and `migrate_voice_keys_split`
(splits the LLM API keys out of voice_provider.env / jasper.env into
voice_keys.env), plus `migrate_secrets_phase4b` (moves HA + Spotify into the
integration-secret compartment and rewrites Spotify cache paths).

These are the most data-sensitive bash in the PR (they move live OAuth tokens),
so the safety properties get pinned here: the accounts.json token_path rewrite
on move, the dual-source key split, idempotency, and the "never strip a key from
the broad files until it is confirmed in voice_keys.env" guard.

CI has no root and no `jasper-secrets` group, so the privileged ops the
functions call (`getent`, `chgrp`, `chown`, `install -d -g`, `systemd-tmpfiles`)
are stubbed; the file-munging under test (mv / sed / grep / touch / printf /
chmod) runs for real against tmp paths.
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "deploy" / "lib" / "install" / "env-migrations.sh"

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
    "migrate_secrets_phase4a",
    "migrate_secrets_phase4b",
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
    return _STUBS + "\n".join(_extract(n) for n in _FUNCS)


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

def test_split_moves_keys_from_voice_provider_to_keys_file(tmp_path: Path):
    _etc, state, secrets = _prep(tmp_path)
    (state / "voice_provider.env").write_text(
        "JASPER_VOICE_PROVIDER=openai\n"
        "JASPER_OPENAI_MODEL=gpt-realtime-2\n"
        "GEMINI_API_KEY=AIza-secret\n"
        "OPENAI_API_KEY=sk-secret\n"
    )

    proc = _run(tmp_path, "migrate_voice_keys_split")
    assert proc.returncode == 0, proc.stderr

    # keys → voice_keys.env; provider + model stay in voice_provider.env.
    assert _kv(secrets / "voice_keys.env") == {
        "GEMINI_API_KEY": "AIza-secret",
        "OPENAI_API_KEY": "sk-secret",
    }
    pv = _kv(state / "voice_provider.env")
    assert pv == {
        "JASPER_VOICE_PROVIDER": "openai",
        "JASPER_OPENAI_MODEL": "gpt-realtime-2",
    }
    assert "GEMINI_API_KEY" not in pv and "OPENAI_API_KEY" not in pv


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
    _etc, state, secrets = _prep(tmp_path)
    (state / "voice_provider.env").write_text(
        "JASPER_VOICE_PROVIDER=gemini\nGEMINI_API_KEY=AIza-x\n"
    )

    first = _run(tmp_path, "migrate_voice_keys_split")
    assert first.returncode == 0, first.stderr
    second = _run(tmp_path, "migrate_voice_keys_split")
    assert second.returncode == 0, second.stderr

    # Re-run does not duplicate or clobber the key, and the broad file stays clean.
    assert _kv(secrets / "voice_keys.env") == {"GEMINI_API_KEY": "AIza-x"}
    assert secrets.joinpath("voice_keys.env").read_text().count("GEMINI_API_KEY=") == 1
    assert "GEMINI_API_KEY" not in _kv(state / "voice_provider.env")


def test_split_keeps_existing_keys_file_value_and_strips_broad(tmp_path: Path):
    """If voice_keys.env already declares a key (e.g. a post-4a wizard save),
    the split must NOT overwrite it — it only cleans a stale broad copy."""
    _etc, state, secrets = _prep(tmp_path)
    (secrets / "voice_keys.env").write_text("OPENAI_API_KEY=sk-canonical\n")
    # A stale duplicate lingering in the broad file:
    (state / "voice_provider.env").write_text(
        "JASPER_VOICE_PROVIDER=openai\nOPENAI_API_KEY=sk-stale\n"
    )

    proc = _run(tmp_path, "migrate_voice_keys_split")
    assert proc.returncode == 0, proc.stderr

    # keys_env value preserved (canonical wins); broad copy stripped.
    assert _kv(secrets / "voice_keys.env") == {"OPENAI_API_KEY": "sk-canonical"}
    assert "OPENAI_API_KEY" not in _kv(state / "voice_provider.env")


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


def test_google_routes_key_moves_from_transit_env_to_secrets(tmp_path: Path):
    _etc, state, secrets = _prep(tmp_path)
    (state / "transit.env").write_text(
        "JASPER_TRANSIT_LAT=40.758\n"
        "GOOGLE_ROUTES_API_KEY=AIzaSySynthetic-Transit_Key\n"
    )

    proc = _run(tmp_path, "migrate_google_routes_key")
    assert proc.returncode == 0, proc.stderr

    assert _kv(secrets / "google_routes.env") == {
        "GOOGLE_ROUTES_API_KEY": "AIzaSySynthetic-Transit_Key",
    }
    assert _kv(state / "transit.env") == {"JASPER_TRANSIT_LAT": "40.758"}


def test_google_routes_key_preserves_existing_secret_and_strips_stale_broad(
    tmp_path: Path,
):
    etc, state, secrets = _prep(tmp_path)
    (secrets / "google_routes.env").write_text(
        "GOOGLE_ROUTES_API_KEY=AIzaSyCanonical\n",
    )
    (etc / "jasper.env").write_text("GOOGLE_ROUTES_API_KEY=AIzaSyStaleEnv\n")
    (state / "transit.env").write_text("GOOGLE_ROUTES_API_KEY=AIzaSyStaleTransit\n")

    proc = _run(tmp_path, "migrate_google_routes_key")
    assert proc.returncode == 0, proc.stderr

    assert _kv(secrets / "google_routes.env") == {
        "GOOGLE_ROUTES_API_KEY": "AIzaSyCanonical",
    }
    assert "GOOGLE_ROUTES_API_KEY" not in _kv(etc / "jasper.env")
    assert "GOOGLE_ROUTES_API_KEY" not in _kv(state / "transit.env")


# --- migrate_secrets_phase4a (Google tree move + accounts.json rewrite) -------

def test_google_tree_move_rewrites_accounts_json_token_path(tmp_path: Path):
    _etc, state, secrets = _prep(tmp_path)
    google = state / "google"
    (google / "tokens").mkdir(parents=True)
    # accounts.json bakes the ABSOLUTE token_path — the migration must rewrite it
    # to the new location or google_creds.load_credentials can't find the token.
    (google / "accounts.json").write_text(
        '{"version": 1, "default": "Jasper", "accounts": [{"name": "Jasper", '
        f'"token_path": "{google}/tokens/Jasper.json"}}]}}\n'
    )
    (google / "tokens" / "Jasper.json").write_text('{"refresh_token": "rt"}\n')
    (state / "google_credentials.env").write_text(
        "GOOGLE_CLIENT_ID=cid\nGOOGLE_CLIENT_SECRET=csecret\n"
    )

    proc = _run(tmp_path, "migrate_secrets_phase4a")
    assert proc.returncode == 0, proc.stderr

    # Tree moved out of STATE_DIR into the compartment.
    assert not google.exists(), "old /state/google must be gone after the move"
    moved = secrets / "google" / "accounts.json"
    assert moved.exists()
    # token_path rewritten to the new prefix; old prefix gone.
    text = moved.read_text()
    assert f"{secrets}/google/tokens/Jasper.json" in text
    assert f"{state}/google/tokens/Jasper.json" not in text
    assert (secrets / "google" / "tokens" / "Jasper.json").exists()
    # creds env moved too; no .bak left behind by the sed rewrite.
    assert (secrets / "google_credentials.env").exists()
    assert not (state / "google_credentials.env").exists()
    assert not (secrets / "google" / "accounts.json.bak").exists()


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

    proc = _run(tmp_path, "migrate_secrets_phase4a")
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

    proc = _run(tmp_path, "migrate_secrets_phase4a")
    assert proc.returncode == 0, proc.stderr

    assert stat.S_IMODE(keys.stat().st_mode) == 0o640


def test_secrets_migration_is_idempotent(tmp_path: Path):
    _etc, state, secrets = _prep(tmp_path)
    google = state / "google"
    (google / "tokens").mkdir(parents=True)
    (google / "accounts.json").write_text(
        f'{{"accounts": [{{"token_path": "{google}/tokens/J.json"}}]}}\n'
    )
    (google / "tokens" / "J.json").write_text('{"refresh_token": "rt"}\n')

    first = _run(tmp_path, "migrate_secrets_phase4a")
    assert first.returncode == 0, first.stderr
    moved = (secrets / "google" / "accounts.json").read_text()
    second = _run(tmp_path, "migrate_secrets_phase4a")
    assert second.returncode == 0, second.stderr
    # Second run is a no-op: the moved accounts.json is unchanged (the guarded
    # `! -e new` move never re-fires), and the token stays put.
    assert (secrets / "google" / "accounts.json").read_text() == moved
    assert (secrets / "google" / "tokens" / "J.json").exists()


# --- migrate_secrets_phase4b (HA + Spotify move/cache_path rewrite) ----------

def test_phase4b_moves_ha_spotify_and_rewrites_accounts_cache_path(tmp_path: Path):
    _etc, state, _secrets = _prep(tmp_path)
    intsecrets = tmp_path / "intsecrets"

    spotify = state / "spotify"
    (spotify / "caches").mkdir(parents=True)
    (spotify / "accounts.json").write_text(
        '{"version": 1, "default": "jasper", "accounts": [{"name": "jasper", '
        f'"cache_path": "{spotify}/caches/jasper.json"}}]}}\n'
    )
    (spotify / "caches" / "jasper.json").write_text('{"refresh_token": "rt"}\n')
    (state / "home_assistant.env").write_text(
        "JASPER_HA_URL=http://ha.local:8123\nJASPER_HA_TOKEN=ha-token\n"
    )
    (state / "spotify_credentials.env").write_text(
        "SPOTIFY_CLIENT_ID=0123456789abcdef0123456789abcdef\n"
        "SPOTIFY_OAUTH_MODE=bounce\n"
    )
    (state / ".spotify-cache").write_text('{"legacy": true}\n')

    proc = _run(tmp_path, "migrate_secrets_phase4b")
    assert proc.returncode == 0, proc.stderr

    assert not (state / "home_assistant.env").exists()
    assert not (state / "spotify_credentials.env").exists()
    assert not (state / ".spotify-cache").exists()
    assert not spotify.exists(), "old /state/spotify must be gone after the move"

    assert (intsecrets / "home_assistant.env").read_text().startswith(
        "JASPER_HA_URL=http://ha.local:8123\n"
    )
    assert (intsecrets / "spotify_credentials.env").exists()
    assert (intsecrets / ".spotify-cache").read_text() == '{"legacy": true}\n'
    moved_accounts = intsecrets / "spotify" / "accounts.json"
    text = moved_accounts.read_text()
    assert f"{intsecrets}/spotify/caches/jasper.json" in text
    assert f"{state}/spotify/caches/jasper.json" not in text
    assert (intsecrets / "spotify" / "caches" / "jasper.json").exists()
    assert not (intsecrets / "spotify" / "accounts.json.bak").exists()


def test_phase4b_migration_is_idempotent(tmp_path: Path):
    _etc, state, _secrets = _prep(tmp_path)
    intsecrets = tmp_path / "intsecrets"
    spotify = state / "spotify"
    (spotify / "caches").mkdir(parents=True)
    (spotify / "accounts.json").write_text(
        f'{{"accounts": [{{"cache_path": "{spotify}/caches/j.json"}}]}}\n'
    )
    (spotify / "caches" / "j.json").write_text('{"refresh_token": "rt"}\n')

    first = _run(tmp_path, "migrate_secrets_phase4b")
    assert first.returncode == 0, first.stderr
    moved = (intsecrets / "spotify" / "accounts.json").read_text()
    second = _run(tmp_path, "migrate_secrets_phase4b")
    assert second.returncode == 0, second.stderr

    assert (intsecrets / "spotify" / "accounts.json").read_text() == moved
    assert (intsecrets / "spotify" / "caches" / "j.json").exists()
