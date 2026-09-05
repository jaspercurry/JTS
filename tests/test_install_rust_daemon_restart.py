# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Dependency and ownership checks for installed Rust audio daemons."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUST_ROOT = (ROOT / "rust").resolve()
RUST_HELPERS = ROOT / "deploy/lib/install/rust-daemons.sh"
UNIT_DIR = ROOT / "deploy/systemd"


def _function_body(text: str, name: str) -> str:
    match = re.search(rf"{re.escape(name)}\(\) \{{\n(.*?)\n\}}", text, re.DOTALL)
    assert match is not None, f"missing {name}()"
    return match.group(1)


def _built_daemons() -> set[str]:
    return set(
        re.findall(
            r'^\s*build_install_rust_daemon "([a-z0-9-]+)" "',
            RUST_HELPERS.read_text(),
            re.MULTILINE,
        )
    )


def _local_cargo_path_dependencies(crate_dir: Path) -> set[Path]:
    manifest = tomllib.loads((crate_dir / "Cargo.toml").read_text())
    dependency_tables = [
        manifest.get("dependencies", {}),
        manifest.get("build-dependencies", {}),
        manifest.get("dev-dependencies", {}),
    ]
    for target in manifest.get("target", {}).values():
        dependency_tables.extend(
            (
                target.get("dependencies", {}),
                target.get("build-dependencies", {}),
                target.get("dev-dependencies", {}),
            )
        )

    dependencies: set[Path] = set()
    for table in dependency_tables:
        for declaration in table.values():
            if not isinstance(declaration, dict) or "path" not in declaration:
                continue
            dependency = (crate_dir / declaration["path"]).resolve()
            assert dependency.parent == RUST_ROOT, (
                f"installer source builds support sibling crates under rust/: "
                f"{crate_dir.name} -> {dependency}"
            )
            assert (dependency / "Cargo.toml").is_file(), (
                crate_dir.name,
                declaration["path"],
            )
            dependencies.add(dependency)
    return dependencies


def _reachable_local_cargo_path_dependencies(crate_names: set[str]) -> set[str]:
    pending = [(RUST_ROOT / name).resolve() for name in crate_names]
    visited: set[Path] = set()
    while pending:
        crate_dir = pending.pop()
        if crate_dir in visited:
            continue
        visited.add(crate_dir)
        pending.extend(_local_cargo_path_dependencies(crate_dir) - visited)
    return {path.name for path in visited} - crate_names


def _staged_shared_rust_crates() -> set[str]:
    build_body = _function_body(RUST_HELPERS.read_text(), "build_install_rust_daemon")
    listed = re.search(r"sibling_crates=\(\n(.*?)\n\s*\)", build_body, re.DOTALL)
    assert listed is not None, "missing sibling_crates staging list"
    return set(re.findall(r"jasper-[a-z0-9-]+", listed.group(1)))


def test_source_build_stages_every_reachable_local_cargo_path_dependency():
    required = _reachable_local_cargo_path_dependencies(_built_daemons())
    staged = _staged_shared_rust_crates()

    assert staged == required, (
        f"unstaged local Cargo path dependencies: {sorted(required - staged)}; "
        f"staged crates no production daemon reaches: {sorted(staged - required)}"
    )


def test_each_built_daemon_has_one_systemd_owner():
    for daemon in _built_daemons():
        needle = f"ExecStart=/opt/jasper/bin/{daemon}"
        owners = [path.name for path in UNIT_DIR.glob("*.service") if needle in path.read_text()]
        assert len(owners) == 1, (daemon, owners)
