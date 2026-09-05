# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import shutil
import subprocess
from collections import Counter
from pathlib import Path
from typing import NamedTuple

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-rust.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"

RUST_CRATES = (
    "rust/jasper-env",
    "rust/jasper-daemon",
    "rust/jasper-clock",
    "rust/jasper-resampler",
    "rust/jasper-ring",
    "rust/jasper-host-clock",
    "rust/jasper-tts-protocol",
    "rust/jasper-fanin",
    "rust/jasper-outputd",
)


class CargoCall(NamedTuple):
    crate: str
    args: str
    generic_allow_cross: str
    generic_path: str
    generic_libdir: str
    target_allow_cross: str
    target_path: str
    target_libdir: str


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _scratch_repo(tmp_path: Path, *, workflow_body: str | None = None) -> Path:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / ".github" / "workflows").mkdir(parents=True)
    shutil.copy2(SCRIPT, repo / "scripts" / "check-rust.sh")
    (repo / ".github" / "workflows" / "tests.yml").write_text(
        workflow_body
        or 'jobs:\n  rust:\n    env:\n      RUST_TOOLCHAIN: "9.9.9"\n',
        encoding="utf-8",
    )
    for crate in RUST_CRATES:
        crate_dir = repo / crate
        crate_dir.mkdir(parents=True)
        (crate_dir / "Cargo.toml").write_text(
            f'[package]\nname = "{crate_dir.name}"\nversion = "0.0.0"\n',
            encoding="utf-8",
        )
    return repo


def _fake_tools(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    for command in (
        "awk",
        "bash",
        "cat",
        "cp",
        "dirname",
        "env",
        "grep",
        "mktemp",
        "printenv",
        "rm",
        "tr",
    ):
        source = shutil.which(command)
        assert source is not None, f"test host is missing {command}"
        (bin_dir / command).symlink_to(source)
    _write_executable(
        bin_dir / "uname",
        """#!/usr/bin/env bash
set -eu
case "${1:-}" in
  -s) printf '%s\n' "${FAKE_HOST_OS}" ;;
  -m) printf '%s\n' "${FAKE_HOST_ARCH}" ;;
  *) exit 2 ;;
esac
""",
    )
    _write_executable(
        bin_dir / "rustup",
        """#!/usr/bin/env bash
set -eu
printf '%s\n' "$*" >> "${FAKE_RUSTUP_LOG}"
case "${1:-}" in
  which)
    if [[ "${FAKE_TOOLCHAIN_MISSING:-0}" == "1" ]]; then
      exit 1
    fi
    if [[ "${FAKE_MISSING_COMPONENT:-}" == "${4:-}" ]]; then
      exit 1
    fi
    printf '/fake/%s\n' "${4:-tool}"
    ;;
  target)
    if [[ "${2:-}" == "list" && "${FAKE_TARGET_INSTALLED:-1}" == "1" ]]; then
      printf '%s\n' "${FAKE_EXPECTED_TARGET:-aarch64-unknown-linux-gnu}"
    fi
    ;;
  run)
    if [[ "${2:-}" != "${FAKE_EXPECTED_TOOLCHAIN}" || "${3:-}" != "cargo" ]]; then
      exit 95
    fi
    shift 3
    exec "${FAKE_CARGO_DRIVER}" "$@"
    ;;
  *) exit 2 ;;
esac
""",
    )
    _write_executable(
        bin_dir / "pkg-config",
        """#!/usr/bin/env bash
set -eu
printf '%s\n' "$*" >> "${FAKE_PKG_CONFIG_LOG}"
if [[ "${FAKE_ALSA_PRESENT:-1}" == "1" ]]; then
  exit 0
fi
exit 1
""",
    )
    _write_executable(
        bin_dir / "cargo",
        """#!/usr/bin/env bash
set -eu
printf '%s\n' "$*" >> "${FAKE_BARE_CARGO_LOG}"
exit 94
""",
    )
    _write_executable(
        bin_dir / "cargo-driver",
        """#!/usr/bin/env bash
set -eu
target_allow="$(printenv "PKG_CONFIG_ALLOW_CROSS_${FAKE_EXPECTED_TARGET}" 2>/dev/null || true)"
target_path="$(printenv "PKG_CONFIG_PATH_${FAKE_EXPECTED_TARGET}" 2>/dev/null || true)"
target_libdir="$(printenv "PKG_CONFIG_LIBDIR_${FAKE_EXPECTED_TARGET}" 2>/dev/null || true)"
printf '%s|%s|%s|%s|%s|%s|%s|%s\n' \
  "${PWD##*/}" "$*" "${PKG_CONFIG_ALLOW_CROSS:-}" \
  "${PKG_CONFIG_PATH:-}" "${PKG_CONFIG_LIBDIR:-}" \
  "${target_allow}" "${target_path}" "${target_libdir}" \
  >> "${FAKE_CARGO_LOG}"
if [[ " $* " == *" clippy "* && "${FAKE_HOST_OS}" == "Darwin" ]]; then
  [[ -f "${target_path}/alsa.pc" ]] || exit 97
  grep -Fq 'Description: stub for Cargo check and Clippy only (no link)' \
    "${target_path}/alsa.pc" || exit 98
  cp "${target_path}/alsa.pc" "${FAKE_STUB_CAPTURE}"
fi
if [[ " $* " == *" clippy "* && \
      "${FAKE_CARGO_FAIL_CRATE:-}" == "${PWD##*/}" ]]; then
  exit "${FAKE_CARGO_FAIL_STATUS:-42}"
fi
""",
    )
    return bin_dir


def _run_script(
    repo: Path,
    tmp_path: Path,
    *,
    host_os: str = "Darwin",
    host_arch: str = "arm64",
    extra_env: dict[str, str] | None = None,
    missing_commands: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    bin_dir = _fake_tools(tmp_path)
    for command in missing_commands:
        (bin_dir / command).unlink()
    temp_dir = tmp_path / "tmp"
    temp_dir.mkdir()
    expected_target = (
        "x86_64-unknown-linux-gnu"
        if host_arch in {"x86_64", "amd64"}
        else "aarch64-unknown-linux-gnu"
    )
    env = dict(os.environ)
    for key in list(env):
        if "PKG_CONFIG" in key:
            env.pop(key)
    env.update(
        {
            "PATH": str(bin_dir),
            "TMPDIR": str(temp_dir),
            "FAKE_HOST_OS": host_os,
            "FAKE_HOST_ARCH": host_arch,
            "FAKE_EXPECTED_TARGET": expected_target,
            "FAKE_EXPECTED_TOOLCHAIN": "9.9.9",
            "FAKE_CARGO_DRIVER": str(bin_dir / "cargo-driver"),
            "FAKE_CARGO_LOG": str(tmp_path / "cargo.log"),
            "FAKE_BARE_CARGO_LOG": str(tmp_path / "bare-cargo.log"),
            "FAKE_RUSTUP_LOG": str(tmp_path / "rustup.log"),
            "FAKE_PKG_CONFIG_LOG": str(tmp_path / "pkg-config.log"),
            "FAKE_STUB_CAPTURE": str(tmp_path / "alsa.pc.capture"),
        }
    )
    if host_os == "Darwin":
        target_suffix = expected_target.replace("-", "_")
        env.update(
            {
                "PKG_CONFIG_ALLOW_CROSS": "0",
                "PKG_CONFIG_PATH": "/inherited/generic/path",
                "PKG_CONFIG_LIBDIR": "/inherited/generic/libdir",
                "TARGET_PKG_CONFIG_ALLOW_CROSS": "0",
                "TARGET_PKG_CONFIG_PATH": "/inherited/target/path",
                "TARGET_PKG_CONFIG_LIBDIR": "/inherited/target/libdir",
                f"PKG_CONFIG_ALLOW_CROSS_{expected_target}": "0",
                f"PKG_CONFIG_PATH_{expected_target}": "/inherited/exact/path",
                f"PKG_CONFIG_LIBDIR_{expected_target}": (
                    "/inherited/exact/libdir"
                ),
                f"PKG_CONFIG_ALLOW_CROSS_{target_suffix}": "0",
                f"PKG_CONFIG_PATH_{target_suffix}": "/inherited/underscore/path",
                f"PKG_CONFIG_LIBDIR_{target_suffix}": (
                    "/inherited/underscore/libdir"
                ),
            }
        )
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [str(repo / "scripts" / "check-rust.sh")],
        cwd=tmp_path,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _cargo_calls(tmp_path: Path) -> list[CargoCall]:
    log = tmp_path / "cargo.log"
    if not log.exists():
        return []
    return [CargoCall(*line.split("|", 7)) for line in log.read_text().splitlines()]


def test_script_is_executable_and_is_the_ci_format_clippy_owner() -> None:
    import yaml

    workflow = WORKFLOW.read_text(encoding="utf-8")
    rust_steps = yaml.safe_load(workflow)["jobs"]["rust"]["steps"]
    router = (ROOT / "scripts" / "rust-ci-needed").read_text(encoding="utf-8")
    test_fast = (ROOT / "scripts" / "test-fast").read_text(encoding="utf-8")
    doc_map = (ROOT / "docs" / "doc-map.toml").read_text(encoding="utf-8")

    assert SCRIPT.is_file()
    assert SCRIPT.stat().st_mode & 0o111
    assert workflow.count("RUST_TOOLCHAIN:") == 1
    assert "run: scripts/check-rust.sh" in workflow
    assert "run: cargo +\"$RUST_TOOLCHAIN\" clippy" not in workflow
    rust_test_crates = [
        step["working-directory"]
        for step in rust_steps
        if step.get("run", "").strip()
        == 'cargo +"$RUST_TOOLCHAIN" test --release --locked'
    ]
    cache_steps = [
        step
        for step in rust_steps
        if step.get("uses", "").startswith("Swatinem/rust-cache@")
    ]
    assert len(cache_steps) == 1
    cache_crates = [
        line.strip()
        for line in cache_steps[0]["with"]["workspaces"].splitlines()
        if line.strip()
    ]
    assert len(RUST_CRATES) == len(set(RUST_CRATES)) == 9
    assert Counter(rust_test_crates) == Counter(RUST_CRATES)
    assert Counter(cache_crates) == Counter(RUST_CRATES)
    assert "scripts/check-rust.sh" in router
    assert "scripts/check-rust.sh" in test_fast
    assert 'add_test_if_present "tests/test_check_rust_script.py"' in test_fast
    assert '"scripts/check-rust.sh"' in doc_map
    assert '"docs/testing-tooling.md"' in doc_map


def test_darwin_lane_uses_workflow_pin_and_exact_ci_crate_contract(
    tmp_path: Path,
) -> None:
    repo = _scratch_repo(tmp_path)

    result = _run_script(repo, tmp_path)

    assert result.returncode == 0, result.stderr
    calls = _cargo_calls(tmp_path)
    assert len(calls) == 18
    assert [call.crate for call in calls if call.args.startswith("fmt ")] == [
        Path(crate).name for crate in RUST_CRATES
    ]
    clippy_calls = [call for call in calls if call.args.startswith("clippy ")]
    assert [call.crate for call in clippy_calls] == [
        Path(crate).name for crate in RUST_CRATES
    ]
    rustup_run_calls = [
        line
        for line in (tmp_path / "rustup.log").read_text(encoding="utf-8").splitlines()
        if line.startswith("run ")
    ]
    assert len(rustup_run_calls) == 18
    assert all(line.startswith("run 9.9.9 cargo ") for line in rustup_run_calls)
    assert not (tmp_path / "bare-cargo.log").exists()

    for call in calls:
        if not call.args.startswith("clippy "):
            assert call.args == "fmt --all -- --check"
            continue
        assert "clippy --release --locked --all-targets" in call.args
        assert "--target aarch64-unknown-linux-gnu" in call.args
        assert call.args.endswith("-- --no-deps -D warnings")
        assert ("--all-features" in call.args) == (
            call.crate == "jasper-host-clock"
        )
        assert call.generic_allow_cross == "1"
        assert call.generic_path == "/inherited/generic/path"
        assert call.generic_libdir == "/inherited/generic/libdir"
        assert call.target_allow_cross == "1"
        assert call.target_path == call.target_libdir
        assert call.target_path != "/inherited/exact/path"
        assert Path(call.target_path).name.startswith("jts-rust-check.")

    stub = (tmp_path / "alsa.pc.capture").read_text(encoding="utf-8")
    assert "Name: alsa" in stub
    assert "Libs: -L${libdir} -lasound" in stub
    assert "Rust formatting and Clippy passed." in result.stdout
    assert not list((tmp_path / "tmp").glob("jts-rust-check.*"))


@pytest.mark.parametrize(
    ("host_arch", "expected_target"),
    (
        ("arm64", "aarch64-unknown-linux-gnu"),
        ("aarch64", "aarch64-unknown-linux-gnu"),
        ("x86_64", "x86_64-unknown-linux-gnu"),
    ),
)
def test_darwin_host_arch_maps_to_matching_linux_target(
    tmp_path: Path, host_arch: str, expected_target: str
) -> None:
    repo = _scratch_repo(tmp_path)

    result = _run_script(repo, tmp_path, host_arch=host_arch)

    assert result.returncode == 0, result.stderr
    clippy_args = [
        call.args
        for call in _cargo_calls(tmp_path)
        if call.args.startswith("clippy ")
    ]
    assert clippy_args
    assert all(f"--target {expected_target}" in args for args in clippy_args)


def test_missing_cross_target_fails_before_cargo_with_install_command(
    tmp_path: Path,
) -> None:
    repo = _scratch_repo(tmp_path)

    result = _run_script(
        repo, tmp_path, extra_env={"FAKE_TARGET_INSTALLED": "0"}
    )

    assert result.returncode == 1
    assert (
        "rustup target add --toolchain 9.9.9 aarch64-unknown-linux-gnu"
        in result.stderr
    )
    assert not _cargo_calls(tmp_path)


def test_missing_toolchain_fails_closed_with_install_command(tmp_path: Path) -> None:
    repo = _scratch_repo(tmp_path)

    result = _run_script(
        repo, tmp_path, extra_env={"FAKE_TOOLCHAIN_MISSING": "1"}
    )

    assert result.returncode == 1
    assert (
        "rustup toolchain install 9.9.9 --profile minimal --component rustfmt "
        "--component clippy" in result.stderr
    )
    assert not _cargo_calls(tmp_path)


@pytest.mark.parametrize(
    ("command", "expected_message"),
    (
        ("rustup", "Install Rust via https://rustup.rs/"),
        ("pkg-config", "brew install pkg-config"),
    ),
)
def test_missing_prerequisite_command_has_actionable_message(
    tmp_path: Path, command: str, expected_message: str
) -> None:
    repo = _scratch_repo(tmp_path)

    result = _run_script(repo, tmp_path, missing_commands=(command,))

    assert result.returncode == 1
    assert expected_message in result.stderr
    if command == "pkg-config":
        assert "apt-get install pkg-config" in result.stderr
    assert not _cargo_calls(tmp_path)


@pytest.mark.parametrize("component", ("rustfmt", "cargo-clippy"))
def test_missing_component_names_actionable_rustup_command(
    tmp_path: Path, component: str
) -> None:
    repo = _scratch_repo(tmp_path)

    result = _run_script(
        repo, tmp_path, extra_env={"FAKE_MISSING_COMPONENT": component}
    )

    assert result.returncode == 1
    assert "rustup component add --toolchain 9.9.9 rustfmt clippy" in result.stderr
    assert not _cargo_calls(tmp_path)


def test_linux_lane_uses_native_alsa_without_cross_target_or_stub(
    tmp_path: Path,
) -> None:
    repo = _scratch_repo(tmp_path)

    result = _run_script(repo, tmp_path, host_os="Linux", host_arch="x86_64")

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "pkg-config.log").read_text(encoding="utf-8") == "--exists alsa\n"
    for call in _cargo_calls(tmp_path):
        assert "--target" not in call.args
        assert not call.generic_allow_cross
        assert not call.generic_path
        assert not call.generic_libdir
        assert not call.target_allow_cross
        assert not call.target_path
        assert not call.target_libdir
    assert not (tmp_path / "alsa.pc.capture").exists()


def test_linux_lane_requires_real_alsa_metadata(tmp_path: Path) -> None:
    repo = _scratch_repo(tmp_path)

    result = _run_script(
        repo,
        tmp_path,
        host_os="Linux",
        host_arch="x86_64",
        extra_env={"FAKE_ALSA_PRESENT": "0"},
    )

    assert result.returncode == 1
    assert "libasound2-dev" in result.stderr
    assert "alsa-lib-devel" in result.stderr
    assert not _cargo_calls(tmp_path)


def test_cross_stub_is_cleaned_and_cargo_failure_status_is_preserved(
    tmp_path: Path,
) -> None:
    repo = _scratch_repo(tmp_path)

    result = _run_script(
        repo,
        tmp_path,
        extra_env={
            "FAKE_CARGO_FAIL_CRATE": "jasper-outputd",
            "FAKE_CARGO_FAIL_STATUS": "42",
        },
    )

    assert result.returncode == 42
    assert not list((tmp_path / "tmp").glob("jts-rust-check.*"))
    assert "Rust formatting and Clippy passed." not in result.stdout


@pytest.mark.parametrize(
    ("workflow_body", "expected_count"),
    (
        ("jobs: {}\n", "0"),
        (
            'env:\n  RUST_TOOLCHAIN: "1.85.0"\n  RUST_TOOLCHAIN: "1.86.0"\n',
            "2",
        ),
    ),
)
def test_toolchain_pin_must_be_present_exactly_once(
    tmp_path: Path, workflow_body: str, expected_count: str
) -> None:
    repo = _scratch_repo(tmp_path, workflow_body=workflow_body)

    result = _run_script(repo, tmp_path)

    assert result.returncode == 1
    assert "expected exactly one RUST_TOOLCHAIN pin" in result.stderr
    assert f"found {expected_count}" in result.stderr
    assert not _cargo_calls(tmp_path)
