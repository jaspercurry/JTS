# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Test install.sh's migrate_grouping key list against the real reader.

migrate_grouping moves operator-set JASPER_GROUPING_* keys out of the legacy
/etc/jasper/jasper.env into the wizard-owned grouping.env that
jasper.multiroom.config.load_config actually reads. From the very first
commit that introduced grouping (3e85f3558, "multi-room: off-by-default
grouping plumbing + P0 sync spike"), the shell keys array carried
JASPER_GROUPING_ENABLED for the enable flag while load_config has always
read plain JASPER_GROUPING — and jasper.control.server._write_grouping, the
sole control-plane writer of grouping.env, has always written plain
JASPER_GROUPING too. JASPER_GROUPING_ENABLED appears nowhere else in the
repo. Net effect: an operator-set legacy enable value never migrated (the
real key had no migration path), while a dead key literal sat in the
migration list moving nothing (the migrated key was never read).

test_shell_migration_keys_are_read_by_config pins the fix the way
test_shell_migration_keys_cover_provider_registry_exactly pins the transit
migration (tests/test_install_transit_citypack_migration.py): derive truth
from the actual reading/writing source rather than a hand-maintained shadow
list, so the test can't independently drift the way the shell array drifted
out of sync with the reader it was supposed to mirror. Unlike transit,
jasper.multiroom.config has no all_env_keys() registry — and shouldn't grow
one just to serve this test, since migrate_grouping's own docstring already
documents, deliberately, that it covers only the bootstrap-era key subset:
load_config also parses ROSTER, PEER_ADDR/NAME, TRIM_DB,
CLIENT_LATENCY_MS, LEFT/RIGHT_DELAY_MS, CROSSOVER_HZ, MAINS_HIGHPASS, and
SUBWOOFER_PRESENT, none of which were ever plausibly hand-set in jasper.env
pre-wizard (they are wizard/control-plane-only fields). So the assertion
direction is shell_keys <= config_keys (every migrated key is one the
reader actually reads), not exact-coverage equality.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENV_MIGRATIONS_LIB = ROOT / "deploy" / "lib" / "install" / "env-migrations.sh"
MULTIROOM_CONFIG_PY = ROOT / "jasper" / "multiroom" / "config.py"


def _shell_migration_keys() -> set[str]:
    text = ENV_MIGRATIONS_LIB.read_text(encoding="utf-8")
    function = text.split("migrate_grouping() {", 1)[1].split("\n}", 1)[0]
    match = re.search(r"local keys=\(\n(?P<body>.*?)\n    \)", function, re.DOTALL)
    assert match is not None
    return {line.strip() for line in match.group("body").splitlines()}


def _load_config_read_keys() -> tuple[set[str], str]:
    """Return (every JASPER_GROUPING_* key load_config reads, the enable key).

    Both are derived from load_config's actual source text — the literal
    string arguments to src.get(...) — not a hand-maintained duplicate
    list, so this test cannot independently drift out of sync with the
    reader the way the shell array drifted out of sync with it.
    """
    text = MULTIROOM_CONFIG_PY.read_text(encoding="utf-8")
    after = text.split("\ndef load_config(", 1)[1]
    body = after.split("\ndef ", 1)[0]
    config_keys = set(re.findall(r'src\.get\("(JASPER_GROUPING[A-Z0-9_]*)"', body))
    enable_match = re.search(r'_parse_enabled\(src\.get\("(?P<key>[^"]+)"', body)
    assert enable_match is not None
    return config_keys, enable_match.group("key")


def test_shell_migration_keys_are_read_by_config() -> None:
    shell_keys = _shell_migration_keys()
    config_keys, enable_key = _load_config_read_keys()

    # Sanity floor so a broken extraction (e.g. an accidental rename of
    # load_config or the keys array) can't silently pass on empty sets.
    assert len(shell_keys) >= 7
    assert len(config_keys) >= 7

    # The real enable-flag key load_config reads — confirmed at HEAD to
    # also be the only key jasper.control.server._write_grouping writes
    # for enablement — must have a migration path out of jasper.env.
    assert enable_key == "JASPER_GROUPING"
    assert enable_key in shell_keys

    # No key in the migration list may be one load_config never reads: a
    # dead literal there (JASPER_GROUPING_ENABLED, pre-fix) moves an
    # operator's legacy value into grouping.env where nothing ever reads
    # it again.
    assert shell_keys <= config_keys
    assert "JASPER_GROUPING_ENABLED" not in shell_keys


def _run_migrate(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    env_dir = tmp_path / "etc"
    state_dir = tmp_path / "state"
    env_dir.mkdir(exist_ok=True)
    state_dir.mkdir(exist_ok=True)

    helper = subprocess.run(
        [
            "bash",
            "-c",
            rf"sed -n '/^migrate_grouping()/,/^}}/p' '{ENV_MIGRATIONS_LIB}'",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "migrate_grouping()" in helper
    helper = 'ensure_state_dir() { install -d -m 0750 "${STATE_DIR}"; }\n' + helper

    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "ENV_DIR": str(env_dir),
        "STATE_DIR": str(state_dir),
    }
    return subprocess.run(
        ["/bin/bash", "-c", f"{helper}\nmigrate_grouping"],
        env=env,
        capture_output=True,
        text=True,
    )


def _read_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text().splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            out[key] = value
    return out


def test_migrates_legacy_enable_flag_from_jasper_env(tmp_path: Path) -> None:
    """Reproduces the reported bug directly: an operator-pasted
    JASPER_GROUPING=on in jasper.env (CI bootstrap / headless imaging /
    hand-edited SSH setup) must land in grouping.env under the same key,
    because that literal is what jasper.multiroom.config.load_config reads.
    Pre-fix, the shell array only recognized JASPER_GROUPING_ENABLED, so
    this exact input was silently left in jasper.env and never reached
    grouping.env at all.
    """
    env_dir = tmp_path / "etc"
    state_dir = tmp_path / "state"
    env_dir.mkdir()
    state_dir.mkdir()
    (env_dir / "jasper.env").write_text(
        "JASPER_GROUPING=on\nJASPER_GROUPING_BOND_ID=abc123\n"
    )

    proc = _run_migrate(tmp_path)
    assert proc.returncode == 0, proc.stderr

    grouping = _read_env(state_dir / "grouping.env")
    assert grouping["JASPER_GROUPING"] == "on"
    assert grouping["JASPER_GROUPING_BOND_ID"] == "abc123"
    # The legacy line must be cleaned out of jasper.env, not left duplicated.
    assert "JASPER_GROUPING=" not in (env_dir / "jasper.env").read_text()
