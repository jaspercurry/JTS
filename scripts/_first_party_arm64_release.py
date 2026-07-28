# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Build and verify the narrow JTS first-party ARM64 runtime bundle.

The declarative contract in ``release/first-party-arm64/artifacts.toml`` is
the one source of truth for build outputs, bundle locations, install
destinations, file modes, and ELF requirements.  This module deliberately
uses only the Python standard library so the same verifier can run during a
fresh Pi install before the JTS virtual environment exists.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import lzma
import os
import platform
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import tomllib
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence


BUILD_INFO_NAME = "BUILD-INFO.json"
BUILD_INFO_SCHEMA_NAME = "BUILD-INFO.schema.json"
CHECKSUMS_NAME = "SHA256SUMS"
NOTICES_NAME = "THIRD-PARTY-NOTICES.md"
DEFAULT_CONTRACT = Path("release/first-party-arm64/artifacts.toml")
DEFAULT_SCHEMA = Path("release/first-party-arm64/BUILD-INFO.schema.json")
REPOSITORY_URL = "https://github.com/jaspercurry/JTS"
RELEASE_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
NOTICE_FILE_PREFIXES = ("license", "copying", "notice", "copyright", "unlicense")


class ReleaseError(RuntimeError):
    """A fail-closed release-contract or verification error."""


@dataclasses.dataclass(frozen=True)
class ArtifactSpec:
    id: str
    description: str
    build_system: str
    build_output_path: PurePosixPath
    bundle_path: PurePosixPath
    install_path: PurePosixPath
    mode: int
    elf_kind: str
    required_needed: tuple[str, ...]
    required_symbols: tuple[str, ...]
    build: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class CargoGraphSpec:
    root: str
    manifest_path: PurePosixPath
    lockfile_path: PurePosixPath


@dataclasses.dataclass(frozen=True)
class DerivedNoticeSpec:
    component: str
    description: str
    source_url: str
    source_sha256: str
    source_path: PurePosixPath
    bundle_path: PurePosixPath
    license: str


@dataclasses.dataclass(frozen=True)
class SystemDependencySpec:
    soname: str
    component: str
    description: str
    source_url: str
    license: str
    license_source_path: Path
    bundle_path: PurePosixPath


@dataclasses.dataclass(frozen=True)
class ToolchainNoticeSpec:
    component: str
    description: str
    source_url: str
    source_path: Path
    bundle_path: PurePosixPath
    license: str


@dataclasses.dataclass(frozen=True)
class Contract:
    schema_version: int
    artifact_set: str
    bundle_format_version: int
    archive_prefix: str
    allowed_cargo_license_expressions: frozenset[str]
    target: Mapping[str, Any]
    artifacts: tuple[ArtifactSpec, ...]
    cargo_graphs: tuple[CargoGraphSpec, ...]
    derived_notices: tuple[DerivedNoticeSpec, ...]
    toolchain_notices: tuple[ToolchainNoticeSpec, ...]
    system_dependencies: tuple[SystemDependencySpec, ...]


@dataclasses.dataclass(frozen=True)
class CargoPackage:
    name: str
    version: str
    source: str | None
    checksum: str | None
    license: str | None
    manifest_path: Path
    used_by: tuple[str, ...]

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.name, self.version, self.source or "")


def _safe_relative(value: str, *, field: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or str(path) != value
        or path.is_absolute()
        or ".." in path.parts
        or "." in path.parts
        or "\\" in value
    ):
        raise ReleaseError(f"{field} must be a normalized relative POSIX path: {value!r}")
    return path


def _absolute(value: str, *, field: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        str(path) != value
        or not path.is_absolute()
        or ".." in path.parts
        or "\\" in value
    ):
        raise ReleaseError(f"{field} must be a normalized absolute POSIX path: {value!r}")
    return path


def _mode(value: str) -> int:
    if not re.fullmatch(r"0[0-7]{3}", value):
        raise ReleaseError(f"artifact mode must be a four-digit octal string: {value!r}")
    return int(value, 8)


def load_contract(repo_root: Path, contract_path: Path | None = None) -> Contract:
    """Load and structurally validate the declarative release contract."""

    path = contract_path or repo_root / DEFAULT_CONTRACT
    if not path.is_absolute():
        path = repo_root / path
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ReleaseError(f"cannot load release contract {path}: {exc}") from exc

    expected_top = {
        "schema_version",
        "artifact_set",
        "bundle_format_version",
        "archive_prefix",
        "licensing",
        "target",
        "artifact",
        "cargo_graph",
        "derived_notice",
        "toolchain_notice",
        "system_dependency",
    }
    extra = set(raw) - expected_top
    if extra:
        raise ReleaseError(f"unknown release-contract keys: {sorted(extra)}")
    if raw.get("schema_version") != 1:
        raise ReleaseError("release contract schema_version must be 1")

    licensing = raw.get("licensing")
    if not isinstance(licensing, dict) or set(licensing) != {
        "allowed_cargo_license_expressions"
    }:
        raise ReleaseError(
            "release contract licensing must contain exactly "
            "allowed_cargo_license_expressions"
        )
    allowed_license_expressions = licensing["allowed_cargo_license_expressions"]
    if (
        not isinstance(allowed_license_expressions, list)
        or not allowed_license_expressions
        or not all(
            isinstance(value, str) and value.strip() == value and value
            for value in allowed_license_expressions
        )
        or allowed_license_expressions != sorted(set(allowed_license_expressions))
    ):
        raise ReleaseError(
            "allowed_cargo_license_expressions must be a sorted, unique, "
            "non-empty string list"
        )

    target = raw.get("target")
    if not isinstance(target, dict):
        raise ReleaseError("release contract target must be a table")
    required_target = {
        "triple",
        "architecture",
        "elf_class",
        "elf_machine",
        "os",
        "distribution",
        "distribution_codename",
        "platform",
        "accepted_host_machines",
        "allowed_interpreters",
        "allowed_needed",
    }
    if set(target) != required_target:
        raise ReleaseError(
            "release contract target keys drifted: "
            f"expected {sorted(required_target)}, got {sorted(target)}"
        )

    artifacts: list[ArtifactSpec] = []
    for item in raw.get("artifact", []):
        expected = {
            "id",
            "description",
            "build_system",
            "build_output_path",
            "bundle_path",
            "install_path",
            "mode",
            "elf_kind",
            "required_needed",
            "required_symbols",
            "build",
        }
        if set(item) != expected:
            raise ReleaseError(
                f"artifact {item.get('id', '<unknown>')} keys drifted: "
                f"expected {sorted(expected)}, got {sorted(item)}"
            )
        if item["elf_kind"] not in {"executable", "shared-object"}:
            raise ReleaseError(f"unknown elf_kind for {item['id']}: {item['elf_kind']}")
        if item["build_system"] not in {"cargo", "make"}:
            raise ReleaseError(
                f"unknown build_system for {item['id']}: {item['build_system']}"
            )
        build = item["build"]
        if not isinstance(build, list) or not build or not all(
            isinstance(part, str) and part for part in build
        ):
            raise ReleaseError(f"artifact {item['id']} build must be a non-empty argv")
        artifacts.append(
            ArtifactSpec(
                id=item["id"],
                description=item["description"],
                build_system=item["build_system"],
                build_output_path=_safe_relative(
                    item["build_output_path"],
                    field=f"{item['id']}.build_output_path",
                ),
                bundle_path=_safe_relative(
                    item["bundle_path"], field=f"{item['id']}.bundle_path"
                ),
                install_path=_absolute(
                    item["install_path"], field=f"{item['id']}.install_path"
                ),
                mode=_mode(item["mode"]),
                elf_kind=item["elf_kind"],
                required_needed=tuple(sorted(set(item["required_needed"]))),
                required_symbols=tuple(sorted(set(item["required_symbols"]))),
                build=tuple(build),
            )
        )
    if len(artifacts) != 3:
        raise ReleaseError("first-party contract must contain exactly 3 artifacts")
    for label, values in (
        ("artifact id", [item.id for item in artifacts]),
        ("bundle path", [str(item.bundle_path) for item in artifacts]),
        ("install path", [str(item.install_path) for item in artifacts]),
    ):
        if len(values) != len(set(values)):
            raise ReleaseError(f"duplicate {label} in release contract")

    cargo_graphs = tuple(
        CargoGraphSpec(
            root=item["root"],
            manifest_path=_safe_relative(
                item["manifest_path"], field=f"{item['root']}.manifest_path"
            ),
            lockfile_path=_safe_relative(
                item["lockfile_path"], field=f"{item['root']}.lockfile_path"
            ),
        )
        for item in raw.get("cargo_graph", [])
    )
    if {graph.root for graph in cargo_graphs} != {
        "jasper-fanin",
        "jasper-outputd",
    }:
        raise ReleaseError("cargo_graph must cover jasper-fanin and jasper-outputd")

    derived = tuple(
        DerivedNoticeSpec(
            component=item["component"],
            description=item["description"],
            source_url=item["source_url"],
            source_sha256=item["source_sha256"],
            source_path=_safe_relative(
                item["source_path"], field=f"{item['component']}.source_path"
            ),
            bundle_path=_safe_relative(
                item["bundle_path"], field=f"{item['component']}.bundle_path"
            ),
            license=item["license"],
        )
        for item in raw.get("derived_notice", [])
    )
    if not all(SHA256_RE.fullmatch(item.source_sha256) for item in derived):
        raise ReleaseError("derived_notice source_sha256 must be lowercase SHA-256")
    toolchain = tuple(
        ToolchainNoticeSpec(
            component=item["component"],
            description=item["description"],
            source_url=item["source_url"],
            source_path=Path(item["source_path"]),
            bundle_path=_safe_relative(
                item["bundle_path"], field=f"{item['component']}.bundle_path"
            ),
            license=item["license"],
        )
        for item in raw.get("toolchain_notice", [])
    )
    if {item.component for item in toolchain} != {
        "Apache License 2.0",
        "Debian rustc copyright inventory",
    }:
        raise ReleaseError(
            "toolchain_notice must cover Debian rustc copyright and Apache-2.0"
        )
    systems = tuple(
        SystemDependencySpec(
            soname=item["soname"],
            component=item["component"],
            description=item["description"],
            source_url=item["source_url"],
            license=item["license"],
            license_source_path=Path(item["license_source_path"]),
            bundle_path=_safe_relative(
                item["bundle_path"], field=f"{item['component']}.bundle_path"
            ),
        )
        for item in raw.get("system_dependency", [])
    )
    if {dependency.soname for dependency in systems} != {"libasound.so.2"}:
        raise ReleaseError("system_dependency must explicitly cover libasound.so.2")

    return Contract(
        schema_version=raw["schema_version"],
        artifact_set=raw["artifact_set"],
        bundle_format_version=raw["bundle_format_version"],
        archive_prefix=raw["archive_prefix"],
        allowed_cargo_license_expressions=frozenset(
            allowed_license_expressions
        ),
        target=target,
        artifacts=tuple(artifacts),
        cargo_graphs=cargo_graphs,
        derived_notices=derived,
        toolchain_notices=toolchain,
        system_dependencies=systems,
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    try:
        result = subprocess.run(
            list(argv),
            cwd=cwd,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise ReleaseError(f"cannot execute {argv[0]}: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no output"
        raise ReleaseError(f"{' '.join(argv)} failed ({result.returncode}): {detail}")
    return result.stdout


def _command_version(argv: Sequence[str]) -> str:
    output = _run(argv)
    for line in output.splitlines():
        if line.strip():
            return line.strip()
    raise ReleaseError(f"{' '.join(argv)} returned no version text")


def git_source_info(repo_root: Path) -> tuple[str, int, bool]:
    commit = _run(["git", "rev-parse", "HEAD"], cwd=repo_root).strip()
    if not GIT_SHA_RE.fullmatch(commit):
        raise ReleaseError(f"git returned invalid commit id: {commit!r}")
    epoch_text = _run(["git", "show", "-s", "--format=%ct", "HEAD"], cwd=repo_root).strip()
    try:
        epoch = int(epoch_text)
    except ValueError as exc:
        raise ReleaseError(f"git returned invalid commit timestamp: {epoch_text!r}") from exc
    dirty = bool(_run(["git", "status", "--porcelain"], cwd=repo_root).strip())
    return commit, epoch, dirty


def _os_release(path: Path = Path("/etc/os-release")) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ReleaseError(f"cannot read build host identity {path}: {exc}") from exc
    values: dict[str, str] = {}
    for line in lines:
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value.strip().strip('"')
    return values


def verify_build_host(contract: Contract) -> str:
    if platform.system().lower() != contract.target["os"]:
        raise ReleaseError(
            f"release builds require Linux, got {platform.system() or '<unknown>'}"
        )
    machine = platform.machine().lower()
    if machine not in contract.target["accepted_host_machines"]:
        raise ReleaseError(
            "release builds must run natively on ARM64; "
            f"got machine {machine or '<unknown>'}"
        )
    os_release = _os_release()
    if os_release.get("ID") != contract.target["distribution"]:
        raise ReleaseError(
            f"release builds require Debian, got ID={os_release.get('ID', '<missing>')}"
        )
    if os_release.get("VERSION_CODENAME") != contract.target["distribution_codename"]:
        raise ReleaseError(
            "release builds require Debian Trixie, got "
            f"VERSION_CODENAME={os_release.get('VERSION_CODENAME', '<missing>')}"
        )
    return machine


def reproducible_environment(repo_root: Path, source_date_epoch: int) -> dict[str, str]:
    env = os.environ.copy()
    # Do not let an operator's interactive compiler wrappers/flags silently
    # change release bytes while BUILD-INFO reports the canonical toolchain.
    for key in (
        "CARGO_BUILD_TARGET",
        "CARGO_ENCODED_RUSTFLAGS",
        "MAKEFLAGS",
        "MFLAGS",
        "RUSTC",
        "RUSTC_WRAPPER",
        "RUSTC_WORKSPACE_WRAPPER",
    ):
        env.pop(key, None)
    stable = {
        "AR": "ar",
        "CARGO_INCREMENTAL": "0",
        "CC": "cc",
        "CPPFLAGS": "",
        "LDFLAGS": "",
        "LC_ALL": "C.UTF-8",
        "SOURCE_DATE_EPOCH": str(source_date_epoch),
        "TZ": "UTC",
    }
    env.update(stable)
    remap = f"--remap-path-prefix={repo_root}=/usr/src/jts"
    env["RUSTFLAGS"] = remap
    base_cflags = "-std=c11 -D_GNU_SOURCE -O2 -Wall -Wextra -Werror -fPIC"
    env["CFLAGS"] = (
        f"{base_cflags} -ffile-prefix-map={repo_root}=/usr/src/jts "
        f"-fdebug-prefix-map={repo_root}=/usr/src/jts"
    )
    return env


def build_artifacts(
    repo_root: Path,
    contract: Contract,
    *,
    source_date_epoch: int,
) -> dict[str, Path]:
    """Build every artifact into a fresh release-owned output root.

    Cargo's normal ignored ``target/`` trees are never read. This is
    correctness-critical in JTS because restored/rounded mtimes have previously
    caused Cargo to call a stale output ``Fresh`` under a newer source commit.
    """

    build_root = repo_root / "target/first-party-arm64-release"
    try:
        if build_root.exists():
            shutil.rmtree(build_root)
        build_root.mkdir(parents=True)
    except OSError as exc:
        raise ReleaseError(
            f"cannot create a fresh isolated release build root {build_root}: {exc}"
        ) from exc
    stable_env = reproducible_environment(repo_root, source_date_epoch)
    outputs: dict[str, Path] = {}
    for artifact in contract.artifacts:
        print(f"==> building {artifact.id}", flush=True)
        env = stable_env.copy()
        output = build_root / artifact.build_output_path
        output.parent.mkdir(parents=True, exist_ok=True)
        if artifact.build_system == "cargo":
            env["CARGO_TARGET_DIR"] = str(build_root / "cargo-target")
        elif artifact.build_system == "make":
            env["PLUGIN_OUTPUT"] = str(output)
        output.unlink(missing_ok=True)
        _run(artifact.build, cwd=repo_root, env=env)
        if not output.is_file():
            raise ReleaseError(
                f"{artifact.id} build completed but output is missing: {output}"
            )
        outputs[artifact.id] = output
    if set(outputs) != {artifact.id for artifact in contract.artifacts}:
        raise ReleaseError("isolated build output set does not match artifact contract")
    return outputs


def inspect_elf(path: Path, *, readelf: str = "readelf") -> dict[str, Any]:
    """Return normalized ELF identity, linkage, and exported-symbol metadata."""

    header = _run([readelf, "--file-header", "--wide", str(path)])
    dynamic = _run([readelf, "--dynamic", "--wide", str(path)])
    program_headers = _run([readelf, "--program-headers", "--wide", str(path)])
    symbols = _run([readelf, "--dyn-syms", "--wide", str(path)])

    def field(name: str) -> str:
        match = re.search(rf"^\s*{re.escape(name)}:\s+(.+?)\s*$", header, re.MULTILINE)
        if not match:
            raise ReleaseError(f"{path}: readelf did not report ELF {name}")
        return match.group(1)

    elf_type = field("Type").split()[0]
    needed = sorted(
        set(
            re.findall(
                r"\(NEEDED\)\s+Shared library: \[([^\]]+)\]",
                dynamic,
            )
        )
    )

    def dynamic_path(tag: str) -> str | None:
        match = re.search(
            rf"\({tag}\)\s+Library (?:rpath|runpath): \[([^\]]*)\]",
            dynamic,
        )
        return match.group(1) if match else None

    interpreter_match = re.search(
        r"Requesting program interpreter:\s*([^\]]+)\]",
        program_headers,
    )
    exported: set[str] = set()
    for line in symbols.splitlines():
        columns = line.split()
        if len(columns) >= 8 and columns[0].rstrip(":").isdigit():
            exported.add(columns[-1].split("@", 1)[0])

    return {
        "class": field("Class"),
        "machine": field("Machine"),
        "type": elf_type,
        "interpreter": interpreter_match.group(1) if interpreter_match else None,
        "needed": needed,
        "rpath": dynamic_path("RPATH"),
        "runpath": dynamic_path("RUNPATH"),
        "_exported_symbols": exported,
    }


def verify_elf(
    artifact: ArtifactSpec,
    target: Mapping[str, Any],
    info: Mapping[str, Any],
) -> None:
    label = artifact.id
    if info["class"] != target["elf_class"]:
        raise ReleaseError(
            f"{label}: expected {target['elf_class']}, got {info['class']}"
        )
    if info["machine"] != target["elf_machine"]:
        raise ReleaseError(
            f"{label}: expected ELF machine {target['elf_machine']}, got {info['machine']}"
        )
    if info["type"] != "DYN":
        raise ReleaseError(f"{label}: expected PIE/shared ELF type DYN, got {info['type']}")
    if info["rpath"] is not None or info["runpath"] is not None:
        raise ReleaseError(
            f"{label}: release artifacts must not carry RPATH/RUNPATH "
            f"(rpath={info['rpath']!r}, runpath={info['runpath']!r})"
        )

    interpreter = info["interpreter"]
    if artifact.elf_kind == "executable":
        if interpreter not in target["allowed_interpreters"]:
            raise ReleaseError(
                f"{label}: unexpected ELF interpreter {interpreter!r}; "
                f"allowed={target['allowed_interpreters']}"
            )
    elif interpreter is not None:
        raise ReleaseError(f"{label}: shared object unexpectedly has interpreter {interpreter}")

    needed = set(info["needed"])
    missing_needed = set(artifact.required_needed) - needed
    if missing_needed:
        raise ReleaseError(
            f"{label}: missing required dynamic libraries {sorted(missing_needed)}"
        )
    unexpected_needed = needed - set(target["allowed_needed"])
    if unexpected_needed:
        raise ReleaseError(
            f"{label}: unreviewed dynamic libraries {sorted(unexpected_needed)}"
        )

    exported = set(info.get("_exported_symbols", set()))
    missing_symbols = set(artifact.required_symbols) - exported
    if missing_symbols:
        raise ReleaseError(
            f"{label}: missing required exported symbols {sorted(missing_symbols)}"
        )


def _lock_packages(lockfile: Path) -> dict[tuple[str, str, str], dict[str, Any]]:
    try:
        data = tomllib.loads(lockfile.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ReleaseError(f"cannot read Cargo lockfile {lockfile}: {exc}") from exc
    packages: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in data.get("package", []):
        key = (item["name"], item["version"], item.get("source", ""))
        if key in packages:
            raise ReleaseError(f"{lockfile}: duplicate locked package {key}")
        packages[key] = item
    return packages


def _active_package_ids(metadata: Mapping[str, Any], root_name: str) -> set[str]:
    packages = {package["id"]: package for package in metadata["packages"]}
    roots = [
        package["id"]
        for package in metadata["packages"]
        if package["name"] == root_name and package.get("source") is None
    ]
    if len(roots) != 1:
        raise ReleaseError(
            f"cargo metadata expected one local root {root_name}, found {len(roots)}"
        )
    nodes = {node["id"]: node for node in metadata["resolve"]["nodes"]}
    active: set[str] = set()
    pending = roots[:]
    while pending:
        package_id = pending.pop()
        if package_id in active:
            continue
        if package_id not in nodes or package_id not in packages:
            raise ReleaseError(f"cargo metadata graph references unknown package {package_id}")
        active.add(package_id)
        for dependency in nodes[package_id]["deps"]:
            kinds = dependency.get("dep_kinds") or [{"kind": None}]
            if not any(kind.get("kind") != "dev" for kind in kinds):
                continue
            pending.append(dependency["pkg"])
    return active


def collect_cargo_graphs(
    repo_root: Path,
    contract: Contract,
) -> tuple[list[dict[str, Any]], list[CargoPackage]]:
    """Resolve exact target-active locked graphs and their notice metadata."""

    graph_records: list[dict[str, Any]] = []
    union: dict[tuple[str, str, str], CargoPackage] = {}
    for graph in contract.cargo_graphs:
        manifest = repo_root / graph.manifest_path
        lockfile = repo_root / graph.lockfile_path
        metadata_text = _run(
            [
                "cargo",
                "metadata",
                "--manifest-path",
                str(manifest),
                "--locked",
                "--format-version",
                "1",
                "--filter-platform",
                contract.target["triple"],
            ],
            cwd=repo_root,
        )
        try:
            metadata = json.loads(metadata_text)
        except json.JSONDecodeError as exc:
            raise ReleaseError(f"cargo metadata for {graph.root} returned invalid JSON") from exc

        active_ids = _active_package_ids(metadata, graph.root)
        packages_by_id = {package["id"]: package for package in metadata["packages"]}
        locked = _lock_packages(lockfile)
        graph_packages: list[dict[str, Any]] = []
        for package_id in sorted(
            active_ids,
            key=lambda value: (
                packages_by_id[value]["name"],
                packages_by_id[value]["version"],
                packages_by_id[value].get("source") or "",
            ),
        ):
            package = packages_by_id[package_id]
            source = package.get("source")
            key = (package["name"], package["version"], source or "")
            lock_entry = locked.get(key)
            if lock_entry is None:
                raise ReleaseError(
                    f"{graph.root}: active package missing from {lockfile}: {key}"
                )
            checksum = lock_entry.get("checksum")
            graph_packages.append(
                {
                    "name": package["name"],
                    "version": package["version"],
                    "source": source,
                    "checksum": checksum,
                }
            )
            current = CargoPackage(
                name=package["name"],
                version=package["version"],
                source=source,
                checksum=checksum,
                license=package.get("license"),
                manifest_path=Path(package["manifest_path"]),
                used_by=(graph.root,),
            )
            prior = union.get(current.key)
            if prior is None:
                union[current.key] = current
            else:
                if (
                    prior.checksum != current.checksum
                    or prior.license != current.license
                    or prior.manifest_path != current.manifest_path
                ):
                    raise ReleaseError(
                        f"cargo package metadata disagrees across locked graphs: {current.key}"
                    )
                union[current.key] = dataclasses.replace(
                    prior,
                    used_by=tuple(sorted(set(prior.used_by) | {graph.root})),
                )
        graph_records.append(
            {
                "root": graph.root,
                "lockfile": str(graph.lockfile_path),
                "lockfile_sha256": sha256_file(lockfile),
                "packages": graph_packages,
            }
        )
    return graph_records, sorted(union.values(), key=lambda package: package.key)


def _package_notice_files(package: CargoPackage) -> list[Path]:
    root = package.manifest_path.parent
    try:
        children = list(root.iterdir())
    except OSError as exc:
        raise ReleaseError(f"cannot inspect cargo package {package.name}: {exc}") from exc
    files = [
        child
        for child in children
        if child.is_file()
        and child.name.lower().startswith(NOTICE_FILE_PREFIXES)
    ]
    return sorted(files, key=lambda path: path.name)


def _copy_normalized(source: Path, destination: Path, mode: int = 0o644) -> None:
    if not source.is_file():
        raise ReleaseError(f"required release input is missing: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    destination.chmod(mode)


def _package_slug(package: CargoPackage) -> str:
    identity = package.checksum or hashlib.sha256(
        (package.source or "local").encode("utf-8")
    ).hexdigest()
    raw = f"{package.name}-{package.version}-{identity[:12]}"
    return re.sub(r"[^A-Za-z0-9._+-]", "_", raw)


def write_notices(
    repo_root: Path,
    bundle_root: Path,
    contract: Contract,
    cargo_packages: Sequence[CargoPackage],
) -> tuple[int, int]:
    """Copy exact license texts and write the deterministic notice index."""

    for package in cargo_packages:
        if not package.license:
            raise ReleaseError(
                "target-active cargo package has no declared license: "
                f"{package.name} {package.version}"
            )
        if package.license not in contract.allowed_cargo_license_expressions:
            raise ReleaseError(
                "target-active cargo package has an unreviewed license "
                f"expression: {package.name} {package.version} "
                f"({package.license})"
            )

    registry_packages = [package for package in cargo_packages if package.source]
    package_rows: list[tuple[CargoPackage, list[str]]] = []
    license_file_count = 0
    for package in registry_packages:
        notice_files = _package_notice_files(package)
        if not notice_files:
            raise ReleaseError(
                f"target-active cargo package has no packaged license/notice file: "
                f"{package.name} {package.version}"
            )
        destinations: list[str] = []
        slug = _package_slug(package)
        for source in notice_files:
            relative = PurePosixPath("licenses", "cargo", slug, source.name)
            _copy_normalized(source, bundle_root / relative)
            destinations.append(str(relative))
            license_file_count += 1
        package_rows.append((package, destinations))

    for notice in contract.derived_notices:
        _copy_normalized(repo_root / notice.source_path, bundle_root / notice.bundle_path)
        license_file_count += 1
    for notice in contract.toolchain_notices:
        _copy_normalized(notice.source_path, bundle_root / notice.bundle_path)
        license_file_count += 1
    for dependency in contract.system_dependencies:
        _copy_normalized(
            dependency.license_source_path,
            bundle_root / dependency.bundle_path,
        )
        license_file_count += 1

    lines = [
        "# Third-Party Notices — JTS First-Party ARM64 Runtime",
        "",
        "This notice bundle covers only the three files named in `BUILD-INFO.json`:",
        "`jasper-fanin`, `jasper-outputd`, and the JTS ALSA ring ioplug. It is",
        "generated from their ARM64 target-active, locked Rust dependency graphs,",
        "the explicitly recorded PipeWire-derived control-law code, and their",
        "dynamic ALSA linkage. It is **not** an SBOM or redistribution clearance",
        "for Raspberry Pi OS, a complete JTS checkout, models, firmware, renderers,",
        "Python packages, or a finished SD-card image.",
        "",
        "## Derived code carried by JTS",
        "",
    ]
    for notice in contract.derived_notices:
        lines.extend(
            [
                f"### {notice.component}",
                "",
                f"- License: `{notice.license}`",
                f"- Source: <{notice.source_url}>",
                f"- Verified source SHA-256: `{notice.source_sha256}`",
                f"- Notice text: `{notice.bundle_path}`",
                f"- Use: {notice.description}.",
                "",
            ]
        )

    lines.extend(
        [
            "## Exact locked Rust packages",
            "",
            "| Package | Version | License expression | Used by | Preserved files |",
            "|---|---:|---|---|---|",
        ]
    )
    for package, destinations in package_rows:
        links = "<br>".join(f"`{destination}`" for destination in destinations)
        lines.append(
            f"| `{package.name}` | `{package.version}` | `{package.license}` | "
            f"{', '.join(f'`{root}`' for root in package.used_by)} | {links} |"
        )

    lines.extend(
        [
            "## Statically incorporated Rust toolchain/runtime code",
            "",
            "The Rust executables statically incorporate Rust standard-library,",
            "panic-runtime, and compiler-builtins code from the recorded Debian",
            "rustc build package. The exact Debian copyright/license inventory",
            "and its referenced Apache-2.0 text are preserved below.",
            "",
        ]
    )
    for notice in contract.toolchain_notices:
        lines.extend(
            [
                f"### {notice.component}",
                "",
                f"- License/status: `{notice.license}`",
                f"- Source: <{notice.source_url}>",
                f"- Preserved file: `{notice.bundle_path}`",
                f"- Boundary: {notice.description}.",
                "",
            ]
        )

    lines.extend(["", "## Dynamically linked system libraries", ""])
    for dependency in contract.system_dependencies:
        lines.extend(
            [
                f"### {dependency.component} (`{dependency.soname}`)",
                "",
                f"- License: `{dependency.license}`",
                f"- Source: <{dependency.source_url}>",
                f"- License text: `{dependency.bundle_path}`",
                f"- Boundary: {dependency.description}.",
                "",
            ]
        )
    lines.extend(
        [
            "The ELF verifier requires each artifact to retain a dynamic",
            "`DT_NEEDED` entry for `libasound.so.2`; this bundle does not copy,",
            "statically link, or otherwise contain alsa-lib. An image containing",
            "the Debian runtime package has a broader OS-package compliance scope.",
            "",
            "## First-party license",
            "",
            "JTS first-party code is Apache-2.0. See `LICENSE` and `NOTICE` in this",
            "bundle.",
            "",
        ]
    )
    notice_path = bundle_root / NOTICES_NAME
    notice_path.write_text("\n".join(lines), encoding="utf-8")
    notice_path.chmod(0o644)
    return len(registry_packages), license_file_count


def _artifact_manifest_entry(
    artifact: ArtifactSpec,
    bundle_file: Path,
    elf_info: Mapping[str, Any],
) -> dict[str, Any]:
    clean_elf = {
        key: value for key, value in elf_info.items() if not key.startswith("_")
    }
    clean_elf["required_symbols"] = list(artifact.required_symbols)
    return {
        "id": artifact.id,
        "description": artifact.description,
        "path": str(artifact.bundle_path),
        "install_path": str(artifact.install_path),
        "mode": f"{artifact.mode:04o}",
        "size": bundle_file.stat().st_size,
        "sha256": sha256_file(bundle_file),
        "elf": clean_elf,
    }


def _generated_at(epoch: int) -> str:
    return (
        dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def toolchain_info() -> dict[str, str]:
    return {
        "python": platform.python_version(),
        "rustc": _command_version(["rustc", "--version"]),
        "cargo": _command_version(["cargo", "--version"]),
        "cc": _command_version(["cc", "--version"]),
        "readelf": _command_version(["readelf", "--version"]),
    }


def system_package_inventory() -> list[dict[str, str]]:
    """Record the complete Debian build-root package set.

    The workflow pins its container image, but apt still resolves against the
    signed moving Trixie repositories. Recording every installed package makes
    that non-hermetic input reviewable without overstating bit reproducibility.
    """

    output = _run(
        [
            "dpkg-query",
            "-W",
            "-f=${binary:Package}\t${Version}\t${Architecture}\n",
        ]
    )
    packages: list[dict[str, str]] = []
    for number, line in enumerate(output.splitlines(), start=1):
        fields = line.split("\t")
        if len(fields) != 3 or not all(fields):
            raise ReleaseError(
                f"dpkg-query returned malformed package row {number}: {line!r}"
            )
        packages.append(
            {
                "name": fields[0],
                "version": fields[1],
                "architecture": fields[2],
            }
        )
    packages.sort(key=lambda item: (item["name"], item["version"], item["architecture"]))
    keys = [
        (item["name"], item["version"], item["architecture"]) for item in packages
    ]
    if not packages or len(keys) != len(set(keys)):
        raise ReleaseError("Debian build-root package inventory is empty or duplicated")
    return packages


def _write_checksums(bundle_root: Path) -> None:
    files = sorted(
        (
            path
            for path in bundle_root.rglob("*")
            if path.is_file()
            and path.relative_to(bundle_root).as_posix() != CHECKSUMS_NAME
        ),
        key=lambda path: path.relative_to(bundle_root).as_posix(),
    )
    lines = [
        f"{sha256_file(path)}  {path.relative_to(bundle_root).as_posix()}"
        for path in files
    ]
    checksum_path = bundle_root / CHECKSUMS_NAME
    checksum_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    checksum_path.chmod(0o644)


def _normalize_tree_metadata(bundle_root: Path, epoch: int) -> None:
    for path in sorted(bundle_root.rglob("*"), reverse=True):
        if path.is_symlink():
            raise ReleaseError(f"bundle may not contain symlinks: {path}")
        if path.is_dir():
            path.chmod(0o755)
        os.utime(path, (epoch, epoch), follow_symlinks=False)
    bundle_root.chmod(0o755)
    os.utime(bundle_root, (epoch, epoch), follow_symlinks=False)


def assemble_bundle(
    repo_root: Path,
    contract: Contract,
    *,
    output_dir: Path,
    release_version: str,
    git_commit: str,
    source_date_epoch: int,
    host_machine: str,
    artifact_sources: Mapping[str, Path],
    force: bool = False,
) -> Path:
    if not RELEASE_VERSION_RE.fullmatch(release_version):
        raise ReleaseError(
            "release version must match "
            f"{RELEASE_VERSION_RE.pattern}: {release_version!r}"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    bundle_name = f"{contract.archive_prefix}-{release_version}"
    final_bundle = output_dir / bundle_name
    if final_bundle.exists():
        if not force:
            raise ReleaseError(f"bundle already exists (use --force): {final_bundle}")
        shutil.rmtree(final_bundle)

    graph_records, cargo_packages = collect_cargo_graphs(repo_root, contract)
    with tempfile.TemporaryDirectory(prefix=f".{bundle_name}.", dir=output_dir) as tmp:
        bundle_root = Path(tmp) / bundle_name
        bundle_root.mkdir()

        artifact_entries: list[dict[str, Any]] = []
        expected_ids = {artifact.id for artifact in contract.artifacts}
        if set(artifact_sources) != expected_ids:
            raise ReleaseError(
                "artifact source set does not match release contract: "
                f"expected={sorted(expected_ids)}, got={sorted(artifact_sources)}"
            )
        for artifact in contract.artifacts:
            source = artifact_sources[artifact.id]
            destination = bundle_root / artifact.bundle_path
            _copy_normalized(source, destination, artifact.mode)
            elf_info = inspect_elf(destination)
            verify_elf(artifact, contract.target, elf_info)
            artifact_entries.append(
                _artifact_manifest_entry(artifact, destination, elf_info)
            )

        _copy_normalized(repo_root / "LICENSE", bundle_root / "LICENSE")
        _copy_normalized(repo_root / "NOTICE", bundle_root / "NOTICE")
        _copy_normalized(repo_root / DEFAULT_SCHEMA, bundle_root / BUILD_INFO_SCHEMA_NAME)
        cargo_count, license_count = write_notices(
            repo_root,
            bundle_root,
            contract,
            cargo_packages,
        )

        stable_env = reproducible_environment(repo_root, source_date_epoch)
        build_info = {
            "schema_version": contract.schema_version,
            "artifact_set": contract.artifact_set,
            "bundle_format_version": contract.bundle_format_version,
            "release_version": release_version,
            "source": {
                "repository": REPOSITORY_URL,
                "git_commit": git_commit,
                "dirty": False,
                "source_date_epoch": source_date_epoch,
            },
            "target": {
                key: contract.target[key]
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
                "generated_at": _generated_at(source_date_epoch),
                "host_machine": host_machine,
                "toolchain": toolchain_info(),
                "system_packages": system_package_inventory(),
                "reproducible_environment": {
                    key: stable_env[key]
                    for key in (
                        "AR",
                        "CARGO_INCREMENTAL",
                        "CC",
                        "CFLAGS",
                        "CPPFLAGS",
                        "LC_ALL",
                        "LDFLAGS",
                        "RUSTFLAGS",
                        "SOURCE_DATE_EPOCH",
                        "TZ",
                    )
                },
            },
            "artifacts": sorted(artifact_entries, key=lambda item: item["id"]),
            "cargo_graphs": sorted(graph_records, key=lambda item: item["root"]),
            "notices": {
                "index_path": NOTICES_NAME,
                "cargo_package_count": cargo_count,
                "toolchain_notice_count": len(contract.toolchain_notices),
                "license_file_count": license_count,
                "scope": (
                    "First-party ARM64 runtime bundle only; excludes the OS, "
                    "images, firmware, models, renderers, and Python runtime."
                ),
            },
        }
        build_info_path = bundle_root / BUILD_INFO_NAME
        build_info_path.write_text(
            json.dumps(build_info, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        build_info_path.chmod(0o644)
        _write_checksums(bundle_root)
        _normalize_tree_metadata(bundle_root, source_date_epoch)
        bundle_root.rename(final_bundle)

    verify_bundle(final_bundle, contract)
    return final_bundle


def _tar_info(path: Path, arcname: str, epoch: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(arcname)
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    info.mtime = epoch
    if path.is_dir():
        info.type = tarfile.DIRTYPE
        info.mode = 0o755
        info.size = 0
    elif path.is_file():
        info.type = tarfile.REGTYPE
        info.mode = stat.S_IMODE(path.stat().st_mode)
        info.size = path.stat().st_size
    else:
        raise ReleaseError(f"archive input is not a regular file/directory: {path}")
    return info


def create_deterministic_archive(
    bundle_root: Path,
    *,
    source_date_epoch: int,
    force: bool = False,
) -> Path:
    archive = bundle_root.parent / f"{bundle_root.name}_linux_arm64.tar.xz"
    sidecar = archive.with_suffix(archive.suffix + ".sha256")
    if (archive.exists() or sidecar.exists()) and not force:
        raise ReleaseError(f"archive already exists (use --force): {archive}")
    archive.unlink(missing_ok=True)
    sidecar.unlink(missing_ok=True)

    paths = [bundle_root, *sorted(bundle_root.rglob("*"))]
    with lzma.LZMAFile(
        archive,
        mode="wb",
        format=lzma.FORMAT_XZ,
        check=lzma.CHECK_CRC64,
        preset=9,
    ) as compressed:
        with tarfile.open(
            fileobj=compressed,
            mode="w",
            format=tarfile.GNU_FORMAT,
        ) as tar:
            for path in paths:
                arcname = path.relative_to(bundle_root.parent).as_posix()
                info = _tar_info(path, arcname, source_date_epoch)
                if path.is_file():
                    with path.open("rb") as source:
                        tar.addfile(info, source)
                else:
                    tar.addfile(info)
    sidecar.write_text(f"{sha256_file(archive)}  {archive.name}\n", encoding="utf-8")
    sidecar.chmod(0o644)
    os.utime(archive, (source_date_epoch, source_date_epoch))
    os.utime(sidecar, (source_date_epoch, source_date_epoch))
    return archive


def _load_build_info(bundle_root: Path) -> dict[str, Any]:
    try:
        info = json.loads((bundle_root / BUILD_INFO_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"cannot read {BUILD_INFO_NAME}: {exc}") from exc
    if not isinstance(info, dict):
        raise ReleaseError(f"{BUILD_INFO_NAME} must contain a JSON object")
    return info


def _require_exact_keys(
    value: Mapping[str, Any],
    expected: set[str],
    *,
    label: str,
) -> None:
    if set(value) != expected:
        raise ReleaseError(
            f"{label} keys drifted: expected {sorted(expected)}, got {sorted(value)}"
        )


def validate_build_info(info: Mapping[str, Any], contract: Contract) -> None:
    """Stdlib semantic validator mirroring the shipped JSON Schema.

    The schema remains the external machine-readable contract; this verifier
    intentionally does not depend on the third-party ``jsonschema`` package.
    """

    _require_exact_keys(
        info,
        {
            "schema_version",
            "artifact_set",
            "bundle_format_version",
            "release_version",
            "source",
            "target",
            "build",
            "artifacts",
            "cargo_graphs",
            "notices",
        },
        label=BUILD_INFO_NAME,
    )
    if info["schema_version"] != contract.schema_version:
        raise ReleaseError("unsupported BUILD-INFO schema_version")
    if info["artifact_set"] != contract.artifact_set:
        raise ReleaseError("unexpected BUILD-INFO artifact_set")
    if info["bundle_format_version"] != contract.bundle_format_version:
        raise ReleaseError("unsupported BUILD-INFO bundle_format_version")
    if not isinstance(info["release_version"], str) or not RELEASE_VERSION_RE.fullmatch(
        info["release_version"]
    ):
        raise ReleaseError("invalid BUILD-INFO release_version")

    source = info["source"]
    _require_exact_keys(
        source,
        {"repository", "git_commit", "dirty", "source_date_epoch"},
        label="BUILD-INFO source",
    )
    if source["repository"] != REPOSITORY_URL:
        raise ReleaseError("BUILD-INFO source.repository is not the JTS repository")
    if not isinstance(source["git_commit"], str) or not GIT_SHA_RE.fullmatch(
        source["git_commit"]
    ):
        raise ReleaseError("BUILD-INFO source.git_commit is invalid")
    if source["dirty"] is not False:
        raise ReleaseError("release bundles may not claim a dirty source tree")
    if not isinstance(source["source_date_epoch"], int) or source["source_date_epoch"] < 0:
        raise ReleaseError("BUILD-INFO source_date_epoch is invalid")

    target_keys = {
        "triple",
        "architecture",
        "elf_class",
        "elf_machine",
        "os",
        "distribution",
        "distribution_codename",
        "platform",
    }
    target = info["target"]
    _require_exact_keys(target, target_keys, label="BUILD-INFO target")
    for key in target_keys:
        if target[key] != contract.target[key]:
            raise ReleaseError(
                f"BUILD-INFO target.{key}={target[key]!r}, expected {contract.target[key]!r}"
            )

    build = info["build"]
    _require_exact_keys(
        build,
        {
            "generated_at",
            "host_machine",
            "toolchain",
            "system_packages",
            "reproducible_environment",
        },
        label="BUILD-INFO build",
    )
    if build["host_machine"] not in contract.target["accepted_host_machines"]:
        raise ReleaseError("BUILD-INFO build.host_machine is not ARM64")
    expected_generated = _generated_at(source["source_date_epoch"])
    if build["generated_at"] != expected_generated:
        raise ReleaseError(
            "BUILD-INFO generated_at must derive from source_date_epoch "
            f"({expected_generated})"
        )
    _require_exact_keys(
        build["toolchain"],
        {"python", "rustc", "cargo", "cc", "readelf"},
        label="BUILD-INFO build.toolchain",
    )
    if not all(
        isinstance(value, str) and value for value in build["toolchain"].values()
    ):
        raise ReleaseError("BUILD-INFO toolchain values must be non-empty strings")
    system_packages = build["system_packages"]
    if not isinstance(system_packages, list) or not system_packages:
        raise ReleaseError("BUILD-INFO system_packages must be a non-empty array")
    system_package_keys: list[tuple[str, str, str]] = []
    for package in system_packages:
        _require_exact_keys(
            package,
            {"name", "version", "architecture"},
            label="BUILD-INFO system package",
        )
        if not all(isinstance(value, str) and value for value in package.values()):
            raise ReleaseError("BUILD-INFO system package values must be non-empty")
        system_package_keys.append(
            (package["name"], package["version"], package["architecture"])
        )
    if system_package_keys != sorted(system_package_keys) or len(
        system_package_keys
    ) != len(set(system_package_keys)):
        raise ReleaseError(
            "BUILD-INFO system_packages must be sorted and unique"
        )
    if not isinstance(build["reproducible_environment"], dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in build["reproducible_environment"].items()
    ):
        raise ReleaseError("BUILD-INFO reproducible_environment is invalid")

    artifacts = info["artifacts"]
    if not isinstance(artifacts, list):
        raise ReleaseError("BUILD-INFO artifacts must be an array")
    specs = {artifact.id: artifact for artifact in contract.artifacts}
    if {item.get("id") for item in artifacts if isinstance(item, dict)} != set(specs):
        raise ReleaseError("BUILD-INFO artifact ids do not match the release contract")
    if [item["id"] for item in artifacts] != sorted(specs):
        raise ReleaseError("BUILD-INFO artifacts must be sorted by id")
    for item in artifacts:
        spec = specs[item["id"]]
        _require_exact_keys(
            item,
            {
                "id",
                "description",
                "path",
                "install_path",
                "mode",
                "size",
                "sha256",
                "elf",
            },
            label=f"BUILD-INFO artifact {spec.id}",
        )
        expected = {
            "description": spec.description,
            "path": str(spec.bundle_path),
            "install_path": str(spec.install_path),
            "mode": f"{spec.mode:04o}",
        }
        for key, value in expected.items():
            if item[key] != value:
                raise ReleaseError(
                    f"BUILD-INFO {spec.id}.{key}={item[key]!r}, expected {value!r}"
                )
        if not isinstance(item["size"], int) or item["size"] <= 0:
            raise ReleaseError(f"BUILD-INFO {spec.id}.size is invalid")
        if not isinstance(item["sha256"], str) or not SHA256_RE.fullmatch(
            item["sha256"]
        ):
            raise ReleaseError(f"BUILD-INFO {spec.id}.sha256 is invalid")
        elf = item["elf"]
        _require_exact_keys(
            elf,
            {
                "class",
                "machine",
                "type",
                "interpreter",
                "needed",
                "rpath",
                "runpath",
                "required_symbols",
            },
            label=f"BUILD-INFO artifact {spec.id}.elf",
        )
        if elf["required_symbols"] != list(spec.required_symbols):
            raise ReleaseError(f"BUILD-INFO {spec.id} required_symbols drifted")

    graphs = info["cargo_graphs"]
    if not isinstance(graphs, list) or [item.get("root") for item in graphs] != sorted(
        graph.root for graph in contract.cargo_graphs
    ):
        raise ReleaseError("BUILD-INFO cargo_graphs are missing or unsorted")
    graph_specs = {graph.root: graph for graph in contract.cargo_graphs}
    for graph in graphs:
        _require_exact_keys(
            graph,
            {"root", "lockfile", "lockfile_sha256", "packages"},
            label=f"BUILD-INFO cargo graph {graph.get('root')}",
        )
        spec = graph_specs[graph["root"]]
        if graph["lockfile"] != str(spec.lockfile_path):
            raise ReleaseError(f"BUILD-INFO {graph['root']} lockfile path drifted")
        if not SHA256_RE.fullmatch(graph["lockfile_sha256"]):
            raise ReleaseError(f"BUILD-INFO {graph['root']} lockfile hash is invalid")
        if not isinstance(graph["packages"], list) or not graph["packages"]:
            raise ReleaseError(f"BUILD-INFO {graph['root']} package graph is empty")
        package_keys: list[tuple[str, str, str]] = []
        for package in graph["packages"]:
            _require_exact_keys(
                package,
                {"name", "version", "source", "checksum"},
                label=f"BUILD-INFO {graph['root']} cargo package",
            )
            if not isinstance(package["name"], str) or not package["name"]:
                raise ReleaseError("BUILD-INFO cargo package name is invalid")
            if not isinstance(package["version"], str) or not package["version"]:
                raise ReleaseError("BUILD-INFO cargo package version is invalid")
            if package["source"] is not None and not isinstance(package["source"], str):
                raise ReleaseError("BUILD-INFO cargo package source is invalid")
            if package["checksum"] is not None and not SHA256_RE.fullmatch(
                package["checksum"]
            ):
                raise ReleaseError("BUILD-INFO cargo package checksum is invalid")
            package_keys.append(
                (
                    package["name"],
                    package["version"],
                    package["source"] or "",
                )
            )
        if package_keys != sorted(package_keys) or len(package_keys) != len(
            set(package_keys)
        ):
            raise ReleaseError(
                f"BUILD-INFO {graph['root']} packages must be sorted and unique"
            )

    notices = info["notices"]
    _require_exact_keys(
        notices,
        {
            "index_path",
            "cargo_package_count",
            "toolchain_notice_count",
            "license_file_count",
            "scope",
        },
        label="BUILD-INFO notices",
    )
    if notices["index_path"] != NOTICES_NAME:
        raise ReleaseError("BUILD-INFO notice index path drifted")
    if (
        not isinstance(notices["cargo_package_count"], int)
        or notices["cargo_package_count"] <= 0
        or notices["toolchain_notice_count"] != len(contract.toolchain_notices)
        or not isinstance(notices["license_file_count"], int)
        or notices["license_file_count"] <= 0
    ):
        raise ReleaseError("BUILD-INFO notice counts are invalid")
    if not isinstance(notices["scope"], str) or not notices["scope"]:
        raise ReleaseError("BUILD-INFO notice scope is missing")


def _parse_checksum_manifest(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ReleaseError(f"cannot read {CHECKSUMS_NAME}: {exc}") from exc
    entries: dict[str, str] = {}
    prior = ""
    for number, line in enumerate(lines, start=1):
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if not match:
            raise ReleaseError(f"{CHECKSUMS_NAME}:{number}: invalid checksum line")
        digest, relative_text = match.groups()
        relative = _safe_relative(relative_text, field=f"{CHECKSUMS_NAME}:{number}")
        normalized = str(relative)
        if normalized == CHECKSUMS_NAME:
            raise ReleaseError(f"{CHECKSUMS_NAME} must not hash itself")
        if normalized in entries:
            raise ReleaseError(f"{CHECKSUMS_NAME}: duplicate path {normalized}")
        if prior and normalized <= prior:
            raise ReleaseError(f"{CHECKSUMS_NAME} paths must be strictly sorted")
        prior = normalized
        entries[normalized] = digest
    if not entries:
        raise ReleaseError(f"{CHECKSUMS_NAME} is empty")
    return entries


def verify_bundle(
    bundle_root: Path,
    contract: Contract,
    *,
    readelf: str = "readelf",
    expected_source_sha: str | None = None,
) -> dict[str, Any]:
    """Fail closed unless the bundle matches hashes, contract, and ELF policy."""

    if not bundle_root.is_dir():
        raise ReleaseError(f"bundle is not a directory: {bundle_root}")
    info = _load_build_info(bundle_root)
    validate_build_info(info, contract)
    if expected_source_sha is not None:
        if not GIT_SHA_RE.fullmatch(expected_source_sha):
            raise ReleaseError(
                "expected source commit must be an exact lowercase 40-character "
                "Git SHA"
            )
        actual_source_sha = info["source"]["git_commit"]
        if actual_source_sha != expected_source_sha:
            raise ReleaseError(
                "bundle source commit does not match the source being installed: "
                f"bundle={actual_source_sha}, expected={expected_source_sha}"
            )
    checksums = _parse_checksum_manifest(bundle_root / CHECKSUMS_NAME)

    actual_files: set[str] = set()
    actual_dirs: set[str] = set()
    root_mode = stat.S_IMODE(bundle_root.stat().st_mode)
    if root_mode != 0o755:
        raise ReleaseError(
            f"bundle root mode {root_mode:04o}, expected 0755"
        )
    artifact_modes = {
        str(artifact.bundle_path): artifact.mode for artifact in contract.artifacts
    }
    for path in bundle_root.rglob("*"):
        relative = path.relative_to(bundle_root).as_posix()
        metadata = path.lstat()
        mode = stat.S_IMODE(metadata.st_mode)
        if stat.S_ISLNK(metadata.st_mode):
            raise ReleaseError(f"bundle contains forbidden symlink: {path}")
        if stat.S_ISDIR(metadata.st_mode):
            if mode != 0o755:
                raise ReleaseError(
                    f"bundle directory {relative} mode {mode:04o}, expected 0755"
                )
            actual_dirs.add(relative)
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise ReleaseError(
                f"bundle contains forbidden special file: {relative}"
            )
        expected_mode = artifact_modes.get(relative, 0o644)
        if mode != expected_mode:
            raise ReleaseError(
                f"bundle file {relative} mode {mode:04o}, "
                f"expected {expected_mode:04o}"
            )
        if relative != CHECKSUMS_NAME:
            actual_files.add(relative)
    if actual_files != set(checksums):
        missing = sorted(set(checksums) - actual_files)
        unlisted = sorted(actual_files - set(checksums))
        raise ReleaseError(
            f"bundle/checksum file set differs: missing={missing}, unlisted={unlisted}"
        )
    expected_dirs: set[str] = set()
    for relative in checksums:
        parent = PurePosixPath(relative).parent
        while str(parent) != ".":
            expected_dirs.add(str(parent))
            parent = parent.parent
    if actual_dirs != expected_dirs:
        missing = sorted(expected_dirs - actual_dirs)
        unlisted = sorted(actual_dirs - expected_dirs)
        raise ReleaseError(
            f"bundle directory set differs: missing={missing}, unlisted={unlisted}"
        )
    for relative, expected in checksums.items():
        actual = sha256_file(bundle_root / relative)
        if actual != expected:
            raise ReleaseError(
                f"checksum mismatch for {relative}: expected {expected}, got {actual}"
            )

    specs = {artifact.id: artifact for artifact in contract.artifacts}
    for item in info["artifacts"]:
        spec = specs[item["id"]]
        path = bundle_root / item["path"]
        if path.stat().st_size != item["size"]:
            raise ReleaseError(f"{spec.id}: size does not match BUILD-INFO")
        if sha256_file(path) != item["sha256"]:
            raise ReleaseError(f"{spec.id}: hash does not match BUILD-INFO")
        actual_mode = stat.S_IMODE(path.stat().st_mode)
        if actual_mode != spec.mode:
            raise ReleaseError(
                f"{spec.id}: mode {actual_mode:04o}, expected {spec.mode:04o}"
            )
        elf_info = inspect_elf(path, readelf=readelf)
        verify_elf(spec, contract.target, elf_info)
        normalized = {
            key: value for key, value in elf_info.items() if not key.startswith("_")
        }
        recorded = {
            key: value for key, value in item["elf"].items() if key != "required_symbols"
        }
        if normalized != recorded:
            raise ReleaseError(
                f"{spec.id}: live ELF metadata differs from BUILD-INFO "
                f"(recorded={recorded}, live={normalized})"
            )
    return info


def install_plan_lines(info: Mapping[str, Any]) -> Iterable[str]:
    """Yield validated tab-separated source, destination, mode, and id rows."""

    for artifact in info["artifacts"]:
        yield "\t".join(
            (
                artifact["path"],
                artifact["install_path"],
                artifact["mode"],
                artifact["id"],
            )
        )


def find_repo_root(script_path: Path) -> Path:
    root = script_path.resolve().parents[1]
    if not (root / "AGENTS.md").is_file() or not (root / DEFAULT_CONTRACT).is_file():
        raise ReleaseError(f"cannot locate JTS repository root from {script_path}")
    return root


def default_release_version(commit: str) -> str:
    return f"git-{commit[:12]}"


def build_release(
    repo_root: Path,
    *,
    output_dir: Path,
    release_version: str | None,
    force: bool,
    no_archive: bool,
) -> tuple[Path, Path | None]:
    contract = load_contract(repo_root)
    host_machine = verify_build_host(contract)
    commit, commit_epoch, dirty = git_source_info(repo_root)
    if dirty:
        raise ReleaseError(
            "release builds require a clean checkout; commit or stash changes first"
        )
    epoch_text = os.environ.get("SOURCE_DATE_EPOCH")
    if epoch_text is None:
        source_date_epoch = commit_epoch
    else:
        try:
            source_date_epoch = int(epoch_text)
        except ValueError as exc:
            raise ReleaseError("SOURCE_DATE_EPOCH must be a non-negative integer") from exc
        if source_date_epoch < 0:
            raise ReleaseError("SOURCE_DATE_EPOCH must be a non-negative integer")

    version = release_version or default_release_version(commit)
    build_root = repo_root / "target/first-party-arm64-release"
    try:
        artifact_sources = build_artifacts(
            repo_root,
            contract,
            source_date_epoch=source_date_epoch,
        )
        post_build_commit, _, post_build_dirty = git_source_info(repo_root)
        if post_build_commit != commit or post_build_dirty:
            raise ReleaseError(
                "source identity changed during the artifact build; refusing to "
                "label outputs with stale provenance"
            )
        bundle = assemble_bundle(
            repo_root,
            contract,
            output_dir=output_dir,
            release_version=version,
            git_commit=commit,
            source_date_epoch=source_date_epoch,
            host_machine=host_machine,
            artifact_sources=artifact_sources,
            force=force,
        )
        archive = None
        if not no_archive:
            archive = create_deterministic_archive(
                bundle,
                source_date_epoch=source_date_epoch,
                force=force,
            )
    finally:
        shutil.rmtree(build_root, ignore_errors=True)
    return bundle, archive
