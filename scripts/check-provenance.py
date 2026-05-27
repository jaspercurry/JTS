#!/usr/bin/env python3
"""Check that install/build fetches are represented in provenance.

This is intentionally lightweight. It does not try to solve SBOMs,
apt snapshots, or Python hash installs in one jump; it guards the
surfaces JTS already owns directly: release archives, git clones,
PlatformIO top-level inputs, and curated model downloads.
"""
from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "deploy" / "provenance.toml"

HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
URL_RE = re.compile(r"https?://[^\s\"'<>),]+")

SHELL_URL_ASSIGN_RE = re.compile(
    r"^(?:local\s+)?(?:[A-Za-z_][A-Za-z0-9_]*(?:URL|REPO)|url)="
)
SHELL_ASSIGN_RE = re.compile(
    r"^(?:local\s+)?([A-Za-z_][A-Za-z0-9_]*)="
    r"(?:\"([^\"]*)\"|'([^']*)'|([^#\s]+))"
)


def load_manifest(path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    with path.open("rb") as f:
        data = tomllib.load(f)
    if data.get("version") != 1:
        raise ValueError(f"{path}: expected version = 1")
    return data


def iter_artifacts(data: dict[str, Any]) -> list[dict[str, Any]]:
    artifacts = data.get("artifact", [])
    if not isinstance(artifacts, list):
        raise ValueError("deploy/provenance.toml: [[artifact]] must be a list")
    return artifacts


def artifacts_by_id(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(artifact["id"]): artifact
        for artifact in iter_artifacts(data)
        if isinstance(artifact.get("id"), str)
    }


def provenance_strings(data: dict[str, Any]) -> set[str]:
    keys = {
        "url",
        "resolved_url",
        "repository",
        "direct_url",
        "download_url",
        "source",
    }
    out: set[str] = set()
    records = list(iter_artifacts(data))
    surfaces = data.get("surface", [])
    if isinstance(surfaces, list):
        records.extend(surfaces)
    for record in records:
        for key in keys:
            value = record.get(key)
            if isinstance(value, str):
                out.add(value)
                if value.startswith("git+"):
                    out.add(value.removeprefix("git+"))
    return out


def validate_artifacts(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    for artifact in iter_artifacts(data):
        artifact_id = artifact.get("id")
        if not isinstance(artifact_id, str) or not artifact_id:
            errors.append("artifact missing string id")
            continue
        if artifact_id in seen:
            errors.append(f"duplicate artifact id: {artifact_id}")
        seen.add(artifact_id)

        kind = str(artifact.get("kind", ""))
        sha = artifact.get("sha256")
        commit = artifact.get("commit")
        if sha is not None and not (isinstance(sha, str) and SHA256_RE.match(sha)):
            errors.append(f"{artifact_id}: invalid sha256 {sha!r}")
        if commit is not None and not (
            isinstance(commit, str) and HEX40_RE.match(commit)
        ):
            errors.append(f"{artifact_id}: invalid commit {commit!r}")

        if kind in {
            "release-archive",
            "release-deb",
            "onnx-model",
            "platformio-platform-archive",
        } and not sha:
            errors.append(f"{artifact_id}: {kind} requires sha256")

        if kind in {"git-source", "python-direct-git", "platformio-git-library"}:
            if not commit:
                errors.append(f"{artifact_id}: {kind} requires commit")
            if not (artifact.get("repository") or artifact.get("direct_url")):
                errors.append(f"{artifact_id}: {kind} requires repository or direct_url")
    return errors


def _strip_comment(line: str, marker: str) -> str:
    before, _sep, _after = line.partition(marker)
    return before


def shell_assignments(path: Path) -> dict[str, list[str]]:
    assignments: dict[str, list[str]] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = _strip_comment(line, "#").strip()
        match = SHELL_ASSIGN_RE.match(line)
        if not match:
            continue
        value = next(group for group in match.groups()[1:] if group is not None)
        assignments.setdefault(match.group(1), []).append(value)
    return assignments


def shell_fetch_urls(path: Path) -> set[str]:
    urls: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = _strip_comment(line, "#").strip()
        if not SHELL_URL_ASSIGN_RE.match(line):
            continue
        urls.update(URL_RE.findall(line))
    return urls


def pyproject_requirement_urls(path: Path) -> set[str]:
    urls: set[str] = set()
    data = tomllib.loads(path.read_text(encoding="utf-8"))

    def collect(value: object) -> None:
        if isinstance(value, str):
            urls.update(URL_RE.findall(value))
        elif isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, dict):
            for item in value.values():
                collect(item)

    collect(data.get("build-system", {}).get("requires", []))
    collect(data.get("project", {}).get("dependencies", []))
    collect(data.get("project", {}).get("optional-dependencies", {}))
    return urls


def platformio_urls(path: Path) -> set[str]:
    urls: set[str] = set()
    in_lib_deps = False
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith(";"):
            continue
        line = _strip_comment(stripped, ";").strip()
        if line.startswith("[") and line.endswith("]"):
            in_lib_deps = False
            continue
        if line.startswith("platform ="):
            urls.update(URL_RE.findall(line))
            continue
        if line.startswith("lib_deps"):
            in_lib_deps = True
            continue
        if in_lib_deps:
            urls.update(URL_RE.findall(line))
    return urls


def registry_urls() -> set[str]:
    sys.path.insert(0, str(ROOT))
    from jasper.aec_engines.dtln_models import REGISTRY as DTLN_REGISTRY
    from jasper.wake_models import REGISTRY as WAKE_REGISTRY

    urls: set[str] = set()
    for entry in WAKE_REGISTRY:
        if entry.source_url:
            urls.add(entry.source_url)
        if entry.download_url:
            urls.add(entry.download_url)
    for entry in DTLN_REGISTRY:
        urls.add(entry.stage1_url)
        urls.add(entry.stage2_url)
    return urls


def discovered_fetch_urls(root: Path = ROOT) -> dict[str, set[str]]:
    return {
        "deploy/install.sh": shell_fetch_urls(root / "deploy" / "install.sh"),
        "pyproject.toml": pyproject_requirement_urls(root / "pyproject.toml"),
        "jasper_aec3/pyproject.toml": pyproject_requirement_urls(
            root / "jasper_aec3" / "pyproject.toml"
        ),
        "firmware/dial/platformio.ini": platformio_urls(
            root / "firmware" / "dial" / "platformio.ini"
        ),
        "firmware/satellite-amoled/platformio.ini": platformio_urls(
            root / "firmware" / "satellite-amoled" / "platformio.ini"
        ),
        "model registries": registry_urls(),
    }


def _expect(
    errors: list[str],
    artifact: dict[str, Any],
    field: str,
    expected: str,
    source: str,
) -> None:
    actual = artifact.get(field)
    if actual != expected:
        errors.append(
            f"{artifact.get('id', '<unknown>')}: {field} is {actual!r}, "
            f"but {source} has {expected!r}"
        )


def _find_artifact(
    artifacts: list[dict[str, Any]],
    field: str,
    expected: str,
) -> dict[str, Any] | None:
    for artifact in artifacts:
        if artifact.get(field) == expected:
            return artifact
    return None


def validate_source_consistency(
    data: dict[str, Any],
    root: Path = ROOT,
) -> list[str]:
    errors: list[str] = []
    artifacts = artifacts_by_id(data)
    install_vars = shell_assignments(root / "deploy" / "install.sh")
    install_text = (root / "deploy" / "install.sh").read_text(encoding="utf-8")

    install_expectations = {
        "CAMILLA_SHA256": ("camilladsp", "sha256"),
        "CAMILLA_URL": ("camilladsp", "url"),
        "CAMILLA_VERSION": ("camilladsp", "version"),
        "RASPOTIFY_SHA256": ("raspotify-librespot-deb", "sha256"),
        "RASPOTIFY_URL": ("raspotify-librespot-deb", "url"),
        "RASPOTIFY_VERSION": ("raspotify-librespot-deb", "version"),
        "NQPTP_COMMIT": ("nqptp", "commit"),
        "NQPTP_REPO": ("nqptp", "repository"),
        "SHAIRPORT_SYNC_COMMIT": ("shairport-sync", "commit"),
        "SHAIRPORT_SYNC_REPO": ("shairport-sync", "repository"),
        "SHAIRPORT_SYNC_VERSION": ("shairport-sync", "ref"),
        "WEBRTC_AEC3_COMMIT": ("webrtc-audio-processing-v2", "commit"),
        "WEBRTC_AEC3_REPO": ("webrtc-audio-processing-v2", "repository"),
        "WEBRTC_AEC3_VERSION": ("webrtc-audio-processing-v2", "ref"),
    }
    for var_name, (artifact_id, field) in install_expectations.items():
        values = install_vars.get(var_name, [])
        if not values:
            errors.append(f"deploy/install.sh: missing {var_name}")
            continue
        artifact = artifacts.get(artifact_id)
        if artifact is None:
            errors.append(f"deploy/provenance.toml: missing artifact {artifact_id}")
            continue
        _expect(errors, artifact, field, values[-1], f"deploy/install.sh {var_name}")

    for artifact_id in (
        "camillagui-aarch64",
        "camillagui-amd64",
        "camillagui-armv7",
    ):
        artifact = artifacts.get(artifact_id)
        if artifact is None:
            errors.append(f"deploy/provenance.toml: missing artifact {artifact_id}")
            continue
        sha = str(artifact.get("sha256", ""))
        if sha not in install_text:
            errors.append(
                f"{artifact_id}: sha256 {sha!r} is not present in deploy/install.sh"
            )

    _validate_pycamilladsp(data, root, errors)
    _validate_model_registries(data, root, errors)
    _validate_platformio_git_artifacts(data, root, errors)
    return errors


def _validate_pycamilladsp(
    data: dict[str, Any],
    root: Path,
    errors: list[str],
) -> None:
    artifact = artifacts_by_id(data).get("pycamilladsp")
    if artifact is None:
        errors.append("deploy/provenance.toml: missing artifact pycamilladsp")
        return
    deps = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    dep_strings = deps.get("project", {}).get("dependencies", [])
    direct_url = str(artifact.get("direct_url", ""))
    if not any(direct_url in dep for dep in dep_strings if isinstance(dep, str)):
        errors.append(
            "pycamilladsp: direct_url does not match pyproject.toml dependency"
        )
    commit = str(artifact.get("commit", ""))
    if commit and commit not in direct_url:
        errors.append("pycamilladsp: commit is not embedded in direct_url")


def _validate_model_registries(
    data: dict[str, Any],
    root: Path,
    errors: list[str],
) -> None:
    sys.path.insert(0, str(root))
    from jasper.aec_engines.dtln_models import REGISTRY as DTLN_REGISTRY
    from jasper.wake_models import REGISTRY as WAKE_REGISTRY

    artifacts = iter_artifacts(data)
    for entry in WAKE_REGISTRY:
        if not entry.download_url:
            continue
        artifact = _find_artifact(artifacts, "download_url", entry.download_url)
        if artifact is None:
            errors.append(
                f"wake model {entry.key}: {entry.download_url} missing artifact"
            )
            continue
        if entry.download_sha256:
            _expect(
                errors,
                artifact,
                "sha256",
                entry.download_sha256,
                f"jasper.wake_models {entry.key}",
            )

    for entry in DTLN_REGISTRY:
        for path, url, expected_sha in entry.files():
            artifact = _find_artifact(artifacts, "download_url", url)
            if artifact is None:
                errors.append(f"DTLN model {path.name}: {url} missing artifact")
                continue
            _expect(
                errors,
                artifact,
                "sha256",
                expected_sha,
                f"jasper.aec_engines.dtln_models {path.name}",
            )


def _validate_platformio_git_artifacts(
    data: dict[str, Any],
    root: Path,
    errors: list[str],
) -> None:
    artifact = artifacts_by_id(data).get("improv-wifi-library")
    if artifact is None:
        errors.append("deploy/provenance.toml: missing artifact improv-wifi-library")
        return
    expected = str(artifact.get("direct_url", ""))
    for relpath in (
        "firmware/dial/platformio.ini",
        "firmware/satellite-amoled/platformio.ini",
    ):
        text = (root / relpath).read_text(encoding="utf-8")
        if expected not in text:
            errors.append(f"improv-wifi-library: direct_url missing from {relpath}")


def _validate_rust_fanin_lock(
    data: dict[str, Any],
    root: Path,
    errors: list[str],
) -> None:
    surfaces = data.get("surface", [])
    surface = None
    if isinstance(surfaces, list):
        for candidate in surfaces:
            if candidate.get("id") == "rust-fanin-crates":
                surface = candidate
                break
    if surface is None:
        errors.append("deploy/provenance.toml: missing surface rust-fanin-crates")
        return
    if surface.get("status") != "pinned":
        errors.append("rust-fanin-crates: status must be pinned")

    manifest_path = root / "rust" / "jasper-fanin" / "Cargo.toml"
    lock_path = root / "rust" / "jasper-fanin" / "Cargo.lock"
    if not lock_path.exists():
        errors.append("rust/jasper-fanin/Cargo.lock is missing")
        return

    manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    package_name = manifest.get("package", {}).get("name")
    direct_deps = set(manifest.get("dependencies", {}).keys())

    packages = lock.get("package", [])
    if not isinstance(packages, list):
        errors.append("rust/jasper-fanin/Cargo.lock: missing [[package]] records")
        return

    root_package = None
    for package in packages:
        if package.get("name") == package_name:
            root_package = package
            break
    if root_package is None:
        errors.append(
            f"rust/jasper-fanin/Cargo.lock: missing root package {package_name!r}"
        )
        return

    locked_deps = {
        str(dep).split()[0]
        for dep in root_package.get("dependencies", [])
        if isinstance(dep, str)
    }
    missing = sorted(direct_deps - locked_deps)
    if missing:
        errors.append(
            "rust/jasper-fanin/Cargo.lock: root package missing direct deps "
            + ", ".join(missing)
        )


def check_manifest(path: Path = DEFAULT_MANIFEST) -> list[str]:
    errors: list[str] = []
    data = load_manifest(path)
    errors.extend(validate_artifacts(data))
    errors.extend(validate_source_consistency(data))
    _validate_rust_fanin_lock(data, ROOT, errors)
    documented = provenance_strings(data)
    for source, urls in discovered_fetch_urls().items():
        for url in sorted(urls):
            if url not in documented:
                errors.append(f"{source}: {url} missing from {path.relative_to(ROOT)}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help="provenance TOML file to validate",
    )
    args = parser.parse_args(argv)

    errors = check_manifest(args.manifest)
    if errors:
        for error in errors:
            print(f"provenance: {error}", file=sys.stderr)
        return 1
    print("provenance: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
