# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contracts for the narrow, reproducible first-party ARM64 bundle."""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import _first_party_arm64_release as release  # noqa: E402


CONTRACT = release.load_contract(ROOT)
INSTALL_HELPER = ROOT / "deploy/lib/install/first-party-runtime.sh"
RUST_HELPER = ROOT / "deploy/lib/install/rust-daemons.sh"
RING_HELPER = ROOT / "deploy/lib/install/ring-platform.sh"
WORKFLOW = ROOT / ".github/workflows/first-party-arm64-release.yml"


def _good_elf(artifact: release.ArtifactSpec) -> dict[str, object]:
    return {
        "class": "ELF64",
        "machine": "AArch64",
        "type": "DYN",
        "interpreter": (
            "/lib/ld-linux-aarch64.so.1"
            if artifact.elf_kind == "executable"
            else None
        ),
        "needed": ["libasound.so.2", "libc.so.6"],
        "rpath": None,
        "runpath": None,
        "_exported_symbols": set(artifact.required_symbols),
    }


def _build_info() -> dict[str, object]:
    artifacts = []
    for spec in sorted(CONTRACT.artifacts, key=lambda item: item.id):
        elf = {
            key: value
            for key, value in _good_elf(spec).items()
            if not key.startswith("_")
        }
        elf["required_symbols"] = list(spec.required_symbols)
        artifacts.append(
            {
                "id": spec.id,
                "description": spec.description,
                "path": str(spec.bundle_path),
                "install_path": str(spec.install_path),
                "mode": f"{spec.mode:04o}",
                "size": 1,
                "sha256": "a" * 64,
                "elf": elf,
            }
        )
    graphs = []
    for graph in sorted(CONTRACT.cargo_graphs, key=lambda item: item.root):
        graphs.append(
            {
                "root": graph.root,
                "lockfile": str(graph.lockfile_path),
                "lockfile_sha256": "b" * 64,
                "packages": [
                    {
                        "name": graph.root,
                        "version": "0.1.0",
                        "source": None,
                        "checksum": None,
                    }
                ],
            }
        )
    return {
        "schema_version": 1,
        "artifact_set": "jts-first-party-runtime",
        "bundle_format_version": 1,
        "release_version": "git-0123456789ab",
        "source": {
            "repository": "https://github.com/jaspercurry/JTS",
            "git_commit": "0" * 40,
            "dirty": False,
            "source_date_epoch": 0,
        },
        "target": {
            key: CONTRACT.target[key]
            for key in (
                "triple",
                "architecture",
                "elf_class",
                "elf_machine",
                "os",
                "distribution",
                "distribution_codename",
                "platform",
            )
        },
        "build": {
            "generated_at": "1970-01-01T00:00:00Z",
            "host_machine": "aarch64",
            "toolchain": {
                "python": "3.13.5",
                "rustc": "rustc 1.85.0",
                "cargo": "cargo 1.85.0",
                "cc": "cc 14.2.0",
                "readelf": "GNU readelf 2.44",
            },
            "system_packages": [
                {
                    "name": "libasound2-dev:arm64",
                    "version": "1.2.14-1",
                    "architecture": "arm64",
                }
            ],
            "reproducible_environment": {
                "CARGO_INCREMENTAL": "0",
                "SOURCE_DATE_EPOCH": "0",
            },
        },
        "artifacts": artifacts,
        "cargo_graphs": graphs,
        "notices": {
            "index_path": "THIRD-PARTY-NOTICES.md",
            "cargo_package_count": 1,
            "toolchain_notice_count": 2,
            "license_file_count": 1,
            "scope": "first-party bundle only",
        },
    }


def test_contract_is_the_single_exact_install_layout() -> None:
    assert [
        (
            artifact.id,
            str(artifact.bundle_path),
            str(artifact.install_path),
            f"{artifact.mode:04o}",
        )
        for artifact in CONTRACT.artifacts
    ] == [
        (
            "jasper-fanin",
            "bin/jasper-fanin",
            "/opt/jasper/bin/jasper-fanin",
            "0755",
        ),
        (
            "jasper-outputd",
            "bin/jasper-outputd",
            "/opt/jasper/bin/jasper-outputd",
            "0755",
        ),
        (
            "jts-ring-ioplug",
            "lib/alsa-lib/libasound_module_pcm_jts_ring.so",
            "/usr/lib/aarch64-linux-gnu/alsa-lib/"
            "libasound_module_pcm_jts_ring.so",
            "0644",
        ),
    ]
    assert {graph.root for graph in CONTRACT.cargo_graphs} == {
        "jasper-fanin",
        "jasper-outputd",
    }
    assert {dependency.soname for dependency in CONTRACT.system_dependencies} == {
        "libasound.so.2"
    }
    assert {
        artifact.id: (
            artifact.build_system,
            str(artifact.build_output_path),
        )
        for artifact in CONTRACT.artifacts
    } == {
        "jasper-fanin": (
            "cargo",
            "cargo-target/release/jasper-fanin",
        ),
        "jasper-outputd": (
            "cargo",
            "cargo-target/release/jasper-outputd",
        ),
        "jts-ring-ioplug": (
            "make",
            "c/libasound_module_pcm_jts_ring.so",
        ),
    }
    assert {notice.component for notice in CONTRACT.toolchain_notices} == {
        "Apache License 2.0",
        "Debian rustc copyright inventory",
    }
    assert CONTRACT.allowed_cargo_license_expressions == {
        "(MIT OR Apache-2.0) AND Unicode-3.0",
        "Apache-2.0",
        "Apache-2.0/MIT",
        "MIT",
        "MIT OR Apache-2.0",
        "Unlicense OR MIT",
    }
    assert CONTRACT.derived_notices[0].source_sha256 == (
        "5dd6a467ff661f0e1ed1b59fa2e0b32db270f8c56b4ebbbdce700e47b0ee26ee"
    )
    assert "fd60e04525f3a04d90bf50085222e0cc9139b4a4" in (
        CONTRACT.derived_notices[0].source_url
    )


def test_schema_and_semantic_validator_share_contract_constants() -> None:
    schema = json.loads(
        (ROOT / "release/first-party-arm64/BUILD-INFO.schema.json").read_text()
    )
    properties = schema["properties"]
    assert properties["schema_version"]["const"] == CONTRACT.schema_version
    assert properties["artifact_set"]["const"] == CONTRACT.artifact_set
    assert (
        properties["bundle_format_version"]["const"]
        == CONTRACT.bundle_format_version
    )
    for key in ("triple", "architecture", "elf_class", "elf_machine", "os"):
        assert properties["target"]["properties"][key]["const"] == CONTRACT.target[key]

    release.validate_build_info(_build_info(), CONTRACT)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda info: info["source"].update(dirty=True), "dirty source tree"),
        (
            lambda info: info["target"].update(architecture="x86_64"),
            "target.architecture",
        ),
        (
            lambda info: info["artifacts"][0].update(
                install_path="/tmp/jasper-fanin"
            ),
            "install_path",
        ),
        (
            lambda info: info["cargo_graphs"][0]["packages"].append(
                info["cargo_graphs"][0]["packages"][0].copy()
            ),
            "sorted and unique",
        ),
    ],
)
def test_build_info_validator_fails_closed(mutation, message: str) -> None:
    info = _build_info()
    mutation(info)
    with pytest.raises(release.ReleaseError, match=message):
        release.validate_build_info(info, CONTRACT)


def test_elf_policy_requires_aarch64_dynamic_alsa_and_no_search_path() -> None:
    artifact = CONTRACT.artifacts[0]
    release.verify_elf(artifact, CONTRACT.target, _good_elf(artifact))

    wrong_arch = {**_good_elf(artifact), "machine": "Advanced Micro Devices X86-64"}
    with pytest.raises(release.ReleaseError):
        release.verify_elf(artifact, CONTRACT.target, wrong_arch)

    static_alsa = {**_good_elf(artifact), "needed": ["libc.so.6"]}
    with pytest.raises(release.ReleaseError):
        release.verify_elf(artifact, CONTRACT.target, static_alsa)

    unreviewed = {
        **_good_elf(artifact),
        "needed": ["libasound.so.2", "libc.so.6", "libsurprise.so.1"],
    }
    with pytest.raises(release.ReleaseError):
        release.verify_elf(artifact, CONTRACT.target, unreviewed)

    rpath = {**_good_elf(artifact), "runpath": "/opt/jasper/lib"}
    with pytest.raises(release.ReleaseError):
        release.verify_elf(artifact, CONTRACT.target, rpath)


def test_plugin_policy_requires_alsa_entrypoint() -> None:
    plugin = next(item for item in CONTRACT.artifacts if item.id == "jts-ring-ioplug")
    missing = {**_good_elf(plugin), "_exported_symbols": set()}
    with pytest.raises(release.ReleaseError):
        release.verify_elf(plugin, CONTRACT.target, missing)


def test_inspect_elf_normalizes_readelf_output(monkeypatch, tmp_path: Path) -> None:
    artifact = tmp_path / "artifact"
    artifact.write_bytes(b"\x7fELF")

    def fake_run(argv, **_kwargs):
        option = argv[1]
        if option == "--file-header":
            return """ELF Header:
  Class:                             ELF64
  Type:                              DYN (Position-Independent Executable file)
  Machine:                           AArch64
"""
        if option == "--dynamic":
            return """Dynamic section:
 0x0000000000000001 (NEEDED) Shared library: [libc.so.6]
 0x0000000000000001 (NEEDED) Shared library: [libasound.so.2]
"""
        if option == "--program-headers":
            return "[Requesting program interpreter: /lib/ld-linux-aarch64.so.1]\n"
        if option == "--dyn-syms":
            return "  12: 000000 0 FUNC GLOBAL DEFAULT 12 _snd_pcm_jts_ring_open\n"
        raise AssertionError(option)

    monkeypatch.setattr(release, "_run", fake_run)
    assert release.inspect_elf(artifact) == {
        "class": "ELF64",
        "machine": "AArch64",
        "type": "DYN",
        "interpreter": "/lib/ld-linux-aarch64.so.1",
        "needed": ["libasound.so.2", "libc.so.6"],
        "rpath": None,
        "runpath": None,
        "_exported_symbols": {"_snd_pcm_jts_ring_open"},
    }


def test_target_active_graph_walk_excludes_dev_only_edges() -> None:
    metadata = {
        "packages": [
            {"id": "root", "name": "jasper-fanin", "source": None},
            {"id": "runtime", "name": "alsa", "source": "registry"},
            {"id": "build", "name": "alsa-sys", "source": "registry"},
            {"id": "dev", "name": "serde_json", "source": "registry"},
        ],
        "resolve": {
            "nodes": [
                {
                    "id": "root",
                    "deps": [
                        {
                            "pkg": "runtime",
                            "dep_kinds": [{"kind": None, "target": None}],
                        },
                        {
                            "pkg": "build",
                            "dep_kinds": [{"kind": "build", "target": None}],
                        },
                        {
                            "pkg": "dev",
                            "dep_kinds": [{"kind": "dev", "target": None}],
                        },
                    ],
                },
                {"id": "runtime", "deps": []},
                {"id": "build", "deps": []},
                {"id": "dev", "deps": []},
            ]
        },
    }
    assert release._active_package_ids(metadata, "jasper-fanin") == {
        "root",
        "runtime",
        "build",
    }


def test_build_artifacts_never_consumes_conventional_or_prior_release_outputs(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    stale_release_root = repo / "target/first-party-arm64-release"
    stale_release_root.mkdir(parents=True)
    (stale_release_root / "stale").write_text("must disappear\n")
    conventional = (
        repo / "rust/jasper-fanin/target/release/jasper-fanin"
    )
    conventional.parent.mkdir(parents=True)
    conventional.write_text("stale conventional cargo output\n")
    conventional_plugin = (
        repo / "c/jts-ring-ioplug/libasound_module_pcm_jts_ring.so"
    )
    conventional_plugin.parent.mkdir(parents=True)
    conventional_plugin.write_text("stale conventional make output\n")

    calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

    def fake_run(argv, *, cwd=None, env=None):
        assert cwd == repo
        assert env is not None
        copied_env = dict(env)
        calls.append((tuple(argv), copied_env))
        if argv[0] == "cargo":
            binary = (
                "jasper-fanin"
                if "jasper-fanin/Cargo.toml" in " ".join(argv)
                else "jasper-outputd"
            )
            output = Path(copied_env["CARGO_TARGET_DIR"]) / "release" / binary
        else:
            output = Path(copied_env["PLUGIN_OUTPUT"])
        assert not output.exists()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(f"fresh {' '.join(argv)}\n")
        return ""

    monkeypatch.setattr(release, "_run", fake_run)
    outputs = release.build_artifacts(
        repo,
        CONTRACT,
        source_date_epoch=1_700_000_000,
    )

    build_root = repo / "target/first-party-arm64-release"
    assert not (build_root / "stale").exists()
    assert set(outputs) == {artifact.id for artifact in CONTRACT.artifacts}
    assert all(path.is_relative_to(build_root) for path in outputs.values())
    assert all(path.read_text().startswith("fresh ") for path in outputs.values())
    assert conventional.read_text() == "stale conventional cargo output\n"
    assert conventional_plugin.read_text() == "stale conventional make output\n"
    cargo_envs = [env for argv, env in calls if argv[0] == "cargo"]
    assert len(cargo_envs) == 2
    assert {
        env["CARGO_TARGET_DIR"] for env in cargo_envs
    } == {str(build_root / "cargo-target")}
    make_env = next(env for argv, env in calls if argv[0] == "make")
    assert make_env["PLUGIN_OUTPUT"] == str(
        build_root / "c/libasound_module_pcm_jts_ring.so"
    )
    makefile = (ROOT / "c/jts-ring-ioplug/Makefile").read_text()
    assert "PLUGIN_OUTPUT ?= $(PLUGIN_SO)" in makefile
    assert "-o $(PLUGIN_OUTPUT)" in makefile
    assert "clean:" in makefile
    assert "$(PLUGIN_OUTPUT)" in makefile.split("clean:", 1)[1]


def test_release_environment_ignores_ambient_compiler_overrides(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("RUSTFLAGS", "-C target-cpu=native")
    monkeypatch.setenv("CARGO_ENCODED_RUSTFLAGS", "malicious")
    monkeypatch.setenv("RUSTC_WRAPPER", "/tmp/wrapper")
    monkeypatch.setenv("MAKEFLAGS", "--eval=unexpected")

    env = release.reproducible_environment(tmp_path, 123)

    assert env["RUSTFLAGS"] == f"--remap-path-prefix={tmp_path}=/usr/src/jts"
    assert env["CC"] == "cc"
    assert env["AR"] == "ar"
    assert env["CPPFLAGS"] == ""
    assert env["LDFLAGS"] == ""
    assert "CARGO_ENCODED_RUSTFLAGS" not in env
    assert "RUSTC_WRAPPER" not in env
    assert "MAKEFLAGS" not in env


def test_complete_system_package_inventory_is_sorted_and_strict(monkeypatch) -> None:
    monkeypatch.setattr(
        release,
        "_run",
        lambda *_args, **_kwargs: (
            "rustc\t1.85.0+dfsg3-1\tarm64\n"
            "libasound2-dev:arm64\t1.2.14-1\tarm64\n"
        ),
    )
    assert release.system_package_inventory() == [
        {
            "name": "libasound2-dev:arm64",
            "version": "1.2.14-1",
            "architecture": "arm64",
        },
        {
            "name": "rustc",
            "version": "1.85.0+dfsg3-1",
            "architecture": "arm64",
        },
    ]

    monkeypatch.setattr(
        release,
        "_run",
        lambda *_args, **_kwargs: "malformed row\n",
    )
    with pytest.raises(release.ReleaseError):
        release.system_package_inventory()


def test_notice_writer_preserves_exact_crate_derived_and_system_texts(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    package_root = tmp_path / "cargo/alsa-0.11.0"
    bundle = tmp_path / "bundle"
    (repo / "LICENSES").mkdir(parents=True)
    package_root.mkdir(parents=True)
    bundle.mkdir()
    (repo / "LICENSES/PipeWire.txt").write_text("pipewire exact\n")
    (package_root / "Cargo.toml").write_text("[package]\nname='alsa'\n")
    (package_root / "LICENSE-MIT").write_text("crate exact\n")
    system_license = tmp_path / "LGPL-2.1"
    system_license.write_text("system exact\n")
    rustc_copyright = tmp_path / "rustc-copyright"
    rustc_copyright.write_text("rust runtime exact\n")
    apache_license = tmp_path / "Apache-2.0"
    apache_license.write_text("apache exact\n")

    contract = dataclasses.replace(
        CONTRACT,
        derived_notices=(
            dataclasses.replace(
                CONTRACT.derived_notices[0],
                source_path=release.PurePosixPath("LICENSES/PipeWire.txt"),
            ),
        ),
        system_dependencies=(
            dataclasses.replace(
                CONTRACT.system_dependencies[0],
                license_source_path=system_license,
            ),
        ),
        toolchain_notices=(
            dataclasses.replace(
                CONTRACT.toolchain_notices[0],
                source_path=rustc_copyright,
            ),
            dataclasses.replace(
                CONTRACT.toolchain_notices[1],
                source_path=apache_license,
            ),
        ),
    )
    package = release.CargoPackage(
        name="alsa",
        version="0.11.0",
        source="registry+https://github.com/rust-lang/crates.io-index",
        checksum="1" * 64,
        license="Apache-2.0/MIT",
        manifest_path=package_root / "Cargo.toml",
        used_by=("jasper-fanin", "jasper-outputd"),
    )
    package_count, license_count = release.write_notices(
        repo, bundle, contract, [package]
    )
    assert package_count == 1
    assert license_count == 5
    notice = (bundle / "THIRD-PARTY-NOTICES.md").read_text()
    assert "`alsa` | `0.11.0` | `Apache-2.0/MIT`" in notice
    assert "SBOM or redistribution clearance" in notice
    assert "does not copy" in notice
    assert (
        bundle / "licenses/cargo/alsa-0.11.0-111111111111/LICENSE-MIT"
    ).read_text() == "crate exact\n"
    assert (
        bundle / "licenses/derived/PipeWire-spa-dll-MIT.txt"
    ).read_text() == "pipewire exact\n"
    assert (
        bundle / "licenses/system/LGPL-2.1.txt"
    ).read_text() == "system exact\n"
    assert (
        bundle / "licenses/toolchain/rustc-Debian-copyright.txt"
    ).read_text() == "rust runtime exact\n"
    assert (
        bundle / "licenses/toolchain/Apache-2.0.txt"
    ).read_text() == "apache exact\n"
    assert "Statically incorporated Rust toolchain/runtime code" in notice


def test_notice_writer_rejects_unreviewed_cargo_license_expression(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "cargo/dependency"
    package_root.mkdir(parents=True)
    (package_root / "Cargo.toml").write_text("[package]\nname='dependency'\n")
    (package_root / "LICENSE").write_text("license text\n")
    package = release.CargoPackage(
        name="dependency",
        version="1.0.0",
        source="registry+https://github.com/rust-lang/crates.io-index",
        checksum="2" * 64,
        license="GPL-3.0-only",
        manifest_path=package_root / "Cargo.toml",
        used_by=("jasper-fanin",),
    )

    with pytest.raises(release.ReleaseError):
        release.write_notices(tmp_path, tmp_path / "bundle", CONTRACT, [package])


def test_deterministic_archive_is_byte_identical(tmp_path: Path) -> None:
    bundle = tmp_path / "jts-first-party-runtime-test"
    (bundle / "bin").mkdir(parents=True)
    executable = bundle / "bin/jasper-fanin"
    executable.write_bytes(b"artifact bytes\n")
    executable.chmod(0o755)
    (bundle / "BUILD-INFO.json").write_text('{"stable":true}\n')

    first = release.create_deterministic_archive(
        bundle, source_date_epoch=1_700_000_000
    )
    first_hash = release.sha256_file(first)
    first.unlink()
    Path(f"{first}.sha256").unlink()
    second = release.create_deterministic_archive(
        bundle, source_date_epoch=1_700_000_000
    )
    assert release.sha256_file(second) == first_hash

    with tarfile_open(second) as tar:
        members = tar.getmembers()
        assert [member.name for member in members] == [
            "jts-first-party-runtime-test",
            "jts-first-party-runtime-test/BUILD-INFO.json",
            "jts-first-party-runtime-test/bin",
            "jts-first-party-runtime-test/bin/jasper-fanin",
        ]
        assert all(member.uid == 0 and member.gid == 0 for member in members)
        assert all(member.mtime == 1_700_000_000 for member in members)


def tarfile_open(path: Path):
    # Local helper keeps tarfile out of module import order commentary above.
    import tarfile

    return tarfile.open(path, mode="r:xz")


def test_checksum_manifest_requires_sorted_complete_paths(tmp_path: Path) -> None:
    path = tmp_path / "SHA256SUMS"
    path.write_text(f"{'a' * 64}  z\n{'b' * 64}  a\n")
    with pytest.raises(release.ReleaseError):
        release._parse_checksum_manifest(path)
    path.write_text(f"{'a' * 64}  ../escape\n")
    with pytest.raises(release.ReleaseError):
        release._parse_checksum_manifest(path)
    path.write_text(f"{'a' * 64}  duplicate//slash\n")
    with pytest.raises(release.ReleaseError):
        release._parse_checksum_manifest(path)


def test_bundle_verifier_detects_post_manifest_tampering(
    monkeypatch, tmp_path: Path
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    info = _build_info()
    specs_by_path = {str(spec.bundle_path): spec for spec in CONTRACT.artifacts}
    for item in info["artifacts"]:
        path = bundle / item["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"{item['id']} bytes\n".encode())
        path.chmod(int(item["mode"], 8))
        item["size"] = path.stat().st_size
        item["sha256"] = release.sha256_file(path)
    (bundle / "BUILD-INFO.json").write_text(
        json.dumps(info, sort_keys=True) + "\n"
    )
    (bundle / "THIRD-PARTY-NOTICES.md").write_text("narrow notices\n")
    release._write_checksums(bundle)

    def fake_inspect(path: Path, **_kwargs):
        relative = path.relative_to(bundle).as_posix()
        return _good_elf(specs_by_path[relative])

    monkeypatch.setattr(release, "inspect_elf", fake_inspect)
    source_sha = info["source"]["git_commit"]
    release.verify_bundle(
        bundle,
        CONTRACT,
        expected_source_sha=source_sha,
    )
    with pytest.raises(release.ReleaseError):
        release.verify_bundle(
            bundle,
            CONTRACT,
            expected_source_sha="1" * 40,
        )
    with pytest.raises(release.ReleaseError):
        release.verify_bundle(
            bundle,
            CONTRACT,
            expected_source_sha="unknown",
        )

    fifo = bundle / "unlisted.fifo"
    os.mkfifo(fifo)
    with pytest.raises(release.ReleaseError):
        release.verify_bundle(bundle, CONTRACT)
    fifo.unlink()

    notice = bundle / "THIRD-PARTY-NOTICES.md"
    notice.chmod(0o600)
    with pytest.raises(release.ReleaseError):
        release.verify_bundle(bundle, CONTRACT)
    notice.chmod(0o644)

    extra_dir = bundle / "unlisted-empty-directory"
    extra_dir.mkdir()
    with pytest.raises(release.ReleaseError):
        release.verify_bundle(bundle, CONTRACT)
    extra_dir.rmdir()

    nested_checksum_name = bundle / "licenses/SHA256SUMS"
    nested_checksum_name.parent.mkdir(exist_ok=True)
    nested_checksum_name.write_text("unlisted\n")
    with pytest.raises(release.ReleaseError):
        release.verify_bundle(bundle, CONTRACT)
    nested_checksum_name.unlink()
    nested_checksum_name.parent.rmdir()

    bundle.chmod(0o700)
    with pytest.raises(release.ReleaseError):
        release.verify_bundle(bundle, CONTRACT)
    bundle.chmod(0o755)

    tampered = bundle / info["artifacts"][0]["path"]
    tampered.write_bytes(b"changed after verification input was prepared\n")
    with pytest.raises(release.ReleaseError):
        release.verify_bundle(bundle, CONTRACT)


def test_installer_bundle_seam_is_fail_closed_and_preserves_complete_bundle() -> None:
    helper = INSTALL_HELPER.read_text()
    assert "JASPER_FIRST_PARTY_RUNTIME_BUNDLE" in helper
    assert 'FIRST_PARTY_RUNTIME_PREPARED=-1' in helper
    assert "refusing source-build fallback" in helper
    assert "expected 3" not in helper
    assert '"${input_bundle}/." "${marker_stage}/bundle/"' in helper
    assert helper.count('"${input_bundle}/."') == 1
    assert 'chmod --reference="${input_bundle}" "${marker_stage}/bundle"' in helper
    assert 'local bundle="${marker_stage}/bundle"' in helper
    assert "THIRD-PARTY-NOTICES" not in helper, (
        "the installer should copy the complete bundle, not grow a second "
        "hand-maintained compliance file list"
    )
    marker_write = helper.index(
        "printf '%s\\n' 'bundle/BUILD-INFO.json' >\"${marker_stage}/INSTALLED\""
    )
    bundle_copy = helper.index('"${input_bundle}/." "${marker_stage}/bundle/"')
    identity_gate = helper.index(
        'expected_source_sha="$(_first_party_runtime_expected_source_sha)"'
    )
    verify_snapshot = helper.index('"${verifier}" \\', identity_gate)
    assert '--expected-source-sha "${expected_source_sha}"' in helper
    source_snapshot = helper.index('sources+=("${bundle}/${relative}")')
    first_activation = helper.index(
        'mv -f -- "${staged[$index]}" "${destinations[$index]}"'
    )
    marker_publish = helper.index('mv -f -- "${marker_stage}" "${marker_dir}"')
    assert (
        identity_gate
        < bundle_copy
        < verify_snapshot
        < source_snapshot
        < first_activation
        < marker_write
        < marker_publish
    )
    assert 'sync -f "${journal}"' in helper
    assert 'sync -f "${journal}" 2>/dev/null || true' not in helper
    assert 'cp -a -- "${backups[$index]}" "${restore_path}"' in helper
    assert helper.index('mv -f -- "${journal_tmp}" "${journal}"') < first_activation
    commit_transition = helper.index(
        'mv -f -- "${journal}" "${committed}"',
        marker_publish,
    )
    cleanup = helper.index(
        "_first_party_runtime_finish_committed_cleanup",
        commit_transition,
    )
    assert marker_publish < commit_transition < cleanup, (
        "runtime backups must survive until complete provenance is published"
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="requires bash")
@pytest.mark.parametrize(
    ("source_sha", "message"),
    [
        ("0" * 40 + "-dirty", "dirty source deployment"),
        ("unknown", "must be an exact lowercase"),
        ("ABCDEF" * 6 + "ABCD", "must be an exact lowercase"),
    ],
)
def test_installer_rejects_dirty_or_unknown_deploy_identity(
    tmp_path: Path,
    source_sha: str,
    message: str,
) -> None:
    script = f"""
set -u
source {INSTALL_HELPER!s}
REPO_DIR={tmp_path!s}
JASPER_DEPLOY_SHA_FULL={source_sha}
_first_party_runtime_expected_source_sha
"""
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert message in result.stderr


@pytest.mark.skipif(shutil.which("bash") is None, reason="requires bash")
def test_installer_accepts_exact_deploy_identity_and_fails_unknown_git(
    tmp_path: Path,
) -> None:
    exact_sha = "a" * 40
    explicit = f"""
set -euo pipefail
source {INSTALL_HELPER!s}
REPO_DIR={tmp_path!s}
JASPER_DEPLOY_SHA_FULL={exact_sha}
_first_party_runtime_expected_source_sha
"""
    accepted = subprocess.run(
        ["bash", "-c", explicit],
        capture_output=True,
        text=True,
        check=False,
    )
    assert accepted.returncode == 0, accepted.stderr
    assert accepted.stdout.strip() == exact_sha

    unknown = f"""
set -u
source {INSTALL_HELPER!s}
REPO_DIR={tmp_path!s}
unset JASPER_DEPLOY_SHA_FULL
_first_party_runtime_expected_source_sha
"""
    rejected = subprocess.run(
        ["bash", "-c", unknown],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rejected.returncode != 0
    assert "source revision is unknown" in rejected.stderr


@pytest.mark.skipif(shutil.which("bash") is None, reason="requires bash")
def test_source_build_path_retires_prior_bundle_success_marker(tmp_path: Path) -> None:
    marker_parent = tmp_path / "share"
    marker_dir = marker_parent / "first-party-runtime"
    bundle = marker_dir / "bundle"
    bundle.mkdir(parents=True)
    (marker_dir / "INSTALLED").write_text("bundle/BUILD-INFO.json\n")
    (bundle / "BUILD-INFO.json").write_text('{"historical":true}\n')

    script = f"""
set -euo pipefail
FIRST_PARTY_RUNTIME_MARKER_PARENT={marker_parent!s}
source {INSTALL_HELPER!s}
unset JASPER_FIRST_PARTY_RUNTIME_BUNDLE
prepare_first_party_runtime_bundle
"""
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    retired = marker_parent / "first-party-runtime-superseded"
    assert not marker_dir.exists()
    assert not (retired / "INSTALLED").exists()
    assert (retired / "SUPERSEDED").is_file()
    assert (retired / "bundle/BUILD-INFO.json").is_file()


@pytest.mark.skipif(shutil.which("bash") is None, reason="requires bash")
def test_installer_bundle_failure_sentinel_blocks_later_fallback(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    marker_parent = tmp_path / "share"
    script = f"""
set -u
FIRST_PARTY_RUNTIME_MARKER_PARENT={marker_parent!s}
source {INSTALL_HELPER!s}
JASPER_FIRST_PARTY_RUNTIME_BUNDLE={missing!s}
prepare_first_party_runtime_bundle >/dev/null 2>&1
first=$?
JASPER_FIRST_PARTY_RUNTIME_BUNDLE=
prepare_first_party_runtime_bundle >/dev/null 2>&1
second=$?
printf '%s %s %s\\n' "$first" "$second" "$FIRST_PARTY_RUNTIME_PREPARED"
"""
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1 1 -1"


@pytest.mark.skipif(shutil.which("bash") is None, reason="requires bash")
def test_interrupted_install_journal_rolls_back_files_and_provenance(
    tmp_path: Path,
) -> None:
    marker_parent = tmp_path / "share"
    marker_parent.mkdir()
    marker_dir = marker_parent / "first-party-runtime"
    marker_backup = marker_parent / ".first-party-runtime-backup"
    marker_stage = marker_parent / ".first-party-runtime-stage"
    journal = marker_parent / ".first-party-runtime-transaction"
    marker_dir.mkdir()
    marker_backup.mkdir()
    marker_stage.mkdir()
    (marker_dir / "version").write_text("new\n")
    (marker_backup / "version").write_text("old\n")

    destinations = [tmp_path / f"runtime-{index}" for index in range(3)]
    rows = ["marker\t1"]
    for index, destination in enumerate(destinations):
        destination.write_text(f"new-{index}\n")
        backup = Path(f"{destination}.jts-runtime-backup")
        if index < 2:
            backup.write_text(f"old-{index}\n")
            existed = 1
        else:
            existed = 0
        Path(f"{destination}.jts-runtime-stage").write_text("stale stage\n")
        rows.append(f"file\t{destination}\t{backup}\t{existed}")
    journal.write_text("\n".join(rows) + "\n")

    script = f"""
set -euo pipefail
source {INSTALL_HELPER!s}
_first_party_runtime_recover_transaction {marker_parent!s}
"""
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert destinations[0].read_text() == "old-0\n"
    assert destinations[1].read_text() == "old-1\n"
    assert not destinations[2].exists()
    assert (marker_dir / "version").read_text() == "old\n"
    assert not marker_backup.exists()
    assert not marker_stage.exists()
    assert not journal.exists()
    assert not list(tmp_path.glob("*.jts-runtime-stage"))
    assert not list(tmp_path.glob("*.jts-runtime-backup"))


@pytest.mark.skipif(shutil.which("bash") is None, reason="requires bash")
def test_committed_transaction_cleanup_keeps_new_files_and_removes_backups(
    tmp_path: Path,
) -> None:
    marker_parent = tmp_path / "share"
    marker_parent.mkdir()
    marker_dir = marker_parent / "first-party-runtime"
    marker_backup = marker_parent / ".first-party-runtime-backup"
    marker_stage = marker_parent / ".first-party-runtime-stage"
    committed = marker_parent / ".first-party-runtime-transaction.committed"
    marker_dir.mkdir()
    marker_backup.mkdir()
    marker_stage.mkdir()
    (marker_dir / "version").write_text("new\n")
    (marker_backup / "version").write_text("old\n")

    destinations = [tmp_path / f"runtime-{index}" for index in range(3)]
    rows = ["marker\t1"]
    for index, destination in enumerate(destinations):
        destination.write_text(f"new-{index}\n")
        backup = Path(f"{destination}.jts-runtime-backup")
        backup.write_text(f"old-{index}\n")
        Path(f"{destination}.jts-runtime-stage").write_text("stale stage\n")
        rows.append(f"file\t{destination}\t{backup}\t1")
    committed.write_text("\n".join(rows) + "\n")

    script = f"""
set -euo pipefail
source {INSTALL_HELPER!s}
_first_party_runtime_recover_transaction {marker_parent!s}
"""
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    for index, destination in enumerate(destinations):
        assert destination.read_text() == f"new-{index}\n"
    assert (marker_dir / "version").read_text() == "new\n"
    assert not marker_backup.exists()
    assert not marker_stage.exists()
    assert not committed.exists()
    assert not list(tmp_path.glob("*.jts-runtime-stage"))
    assert not list(tmp_path.glob("*.jts-runtime-backup"))


def test_each_source_build_entrypoint_checks_bundle_first() -> None:
    rust = RUST_HELPER.read_text()
    ring = RING_HELPER.read_text()
    fanin_body = rust.split("build_install_jasper_fanin() {", 1)[1].split(
        "build_install_jasper_outputd() {", 1
    )[0]
    # build_install_jasper_outputd is the LAST function in rust-daemons.sh, so
    # there is no following `<name>() {` to bound its body with. Its own
    # column-0 closing brace is the boundary instead — which keeps the slice
    # correct (and this assertion honest) whether or not a function is ever
    # appended below it. Do not swap this for a split on a sibling function
    # name: deleting that sibling silently widens the slice to the rest of the
    # file instead of failing.
    outputd_body = rust.split("build_install_jasper_outputd() {", 1)[1].split(
        "\n}\n", 1
    )[0]
    ring_body = ring.split("build_install_jts_ring_ioplug() {", 1)[1].split(
        "install_jts_ring_conf_assets() {", 1
    )[0]
    for artifact_id, body in (
        ("jasper-fanin", fanin_body),
        ("jasper-outputd", outputd_body),
        ("jts-ring-ioplug", ring_body),
    ):
        assert "prepare_first_party_runtime_bundle || return 1" in body
        assert f'first_party_runtime_artifact_installed "{artifact_id}"' in body


def test_protocol_crate_and_pipewire_port_have_release_license_metadata() -> None:
    protocol = (ROOT / "rust/jasper-tts-protocol/Cargo.toml").read_text()
    assert 'license = "Apache-2.0"' in protocol
    assert 'repository = "https://github.com/jaspercurry/JTS"' in protocol
    pipewire_license = ROOT / "LICENSES/PipeWire-spa-dll-MIT.txt"
    assert "Copyright © 2019 Wim Taymans" in pipewire_license.read_text()
    inventory = (ROOT / "LICENSE-third-party.md").read_text()
    assert "`spa_dll` clock-tracking math" in inventory
    assert "LICENSES/PipeWire-spa-dll-MIT.txt" in inventory
    assert "Rust standard library / panic runtime / compiler-builtins" in inventory


def test_arm64_workflow_is_manual_native_and_non_publishing() -> None:
    workflow = WORKFLOW.read_text()
    assert "workflow_dispatch:" in workflow
    assert "runs-on: ubuntu-24.04-arm" in workflow
    assert (
        "image: debian:trixie-20260824-slim@sha256:"
        "d7e12182ce18b85b93007c1dedf31f2d29e01ccf3182cc4017c709b6259bc132"
    ) in workflow
    assert "permissions:\n  contents: read" in workflow
    assert "scripts/build-first-party-arm64-release.py" in workflow
    assert "sha256sum --check ./*.tar.xz.sha256" in workflow
    assert (
        "actions/upload-artifact@"
        "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
    ) in workflow
    trigger_block = workflow.split("permissions:", 1)[0]
    for forbidden in ("push:", "pull_request:", "release:", "schedule:"):
        assert forbidden not in trigger_block
    assert "contents: write" not in workflow
    install_tools = workflow.index(
        "- name: Install native build and ELF inspection tools"
    )
    checkout = workflow.index("- uses: actions/checkout@")
    identity = workflow.index("- name: Verify exact source identity is available")
    build = workflow.index("- name: Build, verify, and package")
    assert install_tools < checkout < identity < build
    assert "test -d .git" in workflow
    assert "git rev-parse --verify HEAD" in workflow
    assert "libasound2-dev pkg-config python3" in workflow
    assert "test -f /usr/share/doc/rustc/copyright" in workflow
    assert "args=()" not in workflow


def test_release_builder_cannot_package_stale_outputs() -> None:
    cli = (ROOT / "scripts/build-first-party-arm64-release.py").read_text()
    implementation = (ROOT / "scripts/_first_party_arm64_release.py").read_text()
    assert "--skip-build" not in cli
    assert 'repo_root / "target/first-party-arm64-release"' in implementation
    assert 'env["CARGO_TARGET_DIR"]' in implementation
    assert 'env["PLUGIN_OUTPUT"]' in implementation
    first_identity = implementation.index("commit, commit_epoch, dirty =")
    build = implementation.index("build_artifacts(", first_identity)
    second_identity = implementation.index(
        "post_build_commit, _, post_build_dirty = git_source_info", build
    )
    assemble = implementation.index("assemble_bundle(", second_identity)
    assert first_identity < build < second_identity < assemble
