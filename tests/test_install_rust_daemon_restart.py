# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Run the shipped Rust build path with local build and install boundaries."""

from __future__ import annotations

import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUST_HELPERS = ROOT / "deploy/lib/install/rust-daemons.sh"


@pytest.mark.parametrize("failure", ["", "cargo", "missing-output"])
def test_workspace_build_stages_once_per_daemon_and_installs_selected_output(
    tmp_path: Path, failure: str,
) -> None:
    if shutil.which("rsync") is None:
        pytest.skip("requires rsync")
    cache = tmp_path / "cache/jasper-rust-build"
    repo = tmp_path / "repo"
    shutil.copytree(ROOT / "rust", repo / "rust", ignore=shutil.ignore_patterns("target"))
    cache.mkdir(parents=True)
    (cache / ".jts-build-cache-format").write_text("1\n")
    for name in ("jasper-rust-build", "jasper-fanin-build", "jasper-outputd-build", "unrelated"):
        target = cache.parent / name / "target"
        target.mkdir(exist_ok=True, parents=True)
        (target / "old").write_text("old\n")
    script = f"""
set -euo pipefail
source {shlex.quote(str(RUST_HELPERS))}
REPO_DIR={shlex.quote(str(repo))}
BUILD_USER=test-build-user
FANIN_BIN={shlex.quote(str(tmp_path / 'installed/jasper-fanin'))}
OUTPUTD_BIN={shlex.quote(str(tmp_path / 'installed/jasper-outputd'))}
export CARGO_TEST_LOG={shlex.quote(str(tmp_path / 'cargo.log'))}
export CARGO_TEST_FAILURE={shlex.quote(failure)}
JASPER_RUST_LOW_MEMORY_BUILD=1
chown() {{ :; }}
sudo() {{ [[ "$1 $2 $3" == '-u test-build-user -H' ]]; shift 3; "$@"; }}
install() {{ shift 6; command install -m 0755 "$@"; }}
run_contained_build() {{ shift 2; "$@"; }}
cargo() {{
    printf '%s|%s|%s|%s|%s|%s\\n' "$PWD" "$*" "$CARGO_BUILD_JOBS" \\
        "$CARGO_PROFILE_RELEASE_LTO" "$CARGO_PROFILE_RELEASE_CODEGEN_UNITS" \\
        "$CARGO_PROFILE_RELEASE_OPT_LEVEL" >> "$CARGO_TEST_LOG"
    [[ ! -e target/old ]] || return 91
    [[ "$CARGO_TEST_FAILURE" != cargo ]] || return 42
    [[ "$CARGO_TEST_FAILURE" != missing-output ]] || return 0
    mkdir -p target/release
    cp "$3/src/main.rs" "target/release/$3"
    chmod +x "target/release/$3"
}}
export -f cargo
build_install_rust_daemon jasper-fanin 0 {shlex.quote(str(cache))}
build_install_rust_daemon jasper-outputd 1 {shlex.quote(str(cache))}
"""
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    calls = [line.split("|") for line in (tmp_path / "cargo.log").read_text().splitlines()]
    assert result.returncode == (1 if failure else 0), result.stderr
    assert len(calls) == (1 if failure else 2)
    for call, name in zip(calls, ("jasper-fanin", "jasper-outputd")):
        assert call == [str(cache), f"build --package {name} --release --locked --quiet", "1", "false", "16", "2"]
    for source in (repo / "rust").rglob("*"):
        if source.is_file():
            assert (cache / source.relative_to(repo / "rust")).read_bytes() == source.read_bytes()
    assert (cache / ".jts-build-cache-format").read_text().strip() == "2"
    for name in ("jasper-fanin-build", "jasper-outputd-build"):
        assert not (cache.parent / name / "target").exists()
    assert (cache.parent / "unrelated/target/old").is_file()
    for name in ("jasper-fanin", "jasper-outputd"):
        installed = tmp_path / "installed" / name
        if failure:
            assert not installed.exists()
        else:
            assert installed.read_bytes() == (repo / "rust" / name / "src/main.rs").read_bytes()
            assert installed.stat().st_mode & 0o777 == 0o755


def test_each_workspace_binary_has_one_systemd_owner() -> None:
    for main in sorted((ROOT / "rust").glob("*/src/main.rs")):
        daemon = main.parents[1].name
        needle = f"ExecStart=/opt/jasper/bin/{daemon}"
        owners = [
            unit.name for unit in (ROOT / "deploy/systemd").glob("*.service")
            if needle in unit.read_text()
        ]
        assert owners == [f"{daemon}.service"], (daemon, owners)
