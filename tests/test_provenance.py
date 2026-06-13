from __future__ import annotations

import copy
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-provenance.py"


def _load_check_module():
    spec = importlib.util.spec_from_file_location("check_provenance", SCRIPT)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_provenance_manifest_covers_known_fetches() -> None:
    check_provenance = _load_check_module()

    assert check_provenance.check_manifest() == []


def test_provenance_check_scans_aec3_build_requirements() -> None:
    check_provenance = _load_check_module()

    discovered = check_provenance.discovered_fetch_urls()

    assert "jasper_aec3/pyproject.toml" in discovered


def test_provenance_check_detects_install_constant_drift() -> None:
    check_provenance = _load_check_module()
    data = copy.deepcopy(check_provenance.load_manifest())
    for artifact in data["artifact"]:
        if artifact["id"] == "raspotify-librespot-deb":
            artifact["sha256"] = "0" * 64
            break

    errors = check_provenance.validate_source_consistency(data)

    assert any(
        "raspotify-librespot-deb" in error
        and "RASPOTIFY_SHA256" in error
        for error in errors
    )


def test_pycamilladsp_provenance_scans_optional_dependencies(
    tmp_path: Path,
) -> None:
    check_provenance = _load_check_module()
    artifact = copy.deepcopy(
        check_provenance.artifacts_by_id(check_provenance.load_manifest())[
            "pycamilladsp"
        ]
    )
    direct_url = artifact["direct_url"]
    (tmp_path / "pyproject.toml").write_text(
        f"""
[project]
name = "probe"
version = "0.0.0"
dependencies = []

[project.optional-dependencies]
full = [
    "camilladsp @ {direct_url}",
]
""".lstrip(),
        encoding="utf-8",
    )

    errors: list[str] = []
    check_provenance._validate_pycamilladsp(
        {"artifact": [artifact]}, tmp_path, errors,
    )

    assert errors == []


def test_binary_artifacts_have_sha256s() -> None:
    check_provenance = _load_check_module()
    data = check_provenance.load_manifest()
    artifacts = check_provenance.iter_artifacts(data)

    checked_kinds = {
        "release-archive",
        "release-deb",
        "source-archive",
        "python-source-archive",
        "onnx-model",
        "platformio-platform-archive",
    }
    missing = [
        artifact["id"]
        for artifact in artifacts
        if artifact.get("kind") in checked_kinds
        and not check_provenance.SHA256_RE.match(str(artifact.get("sha256", "")))
    ]

    assert missing == []


def test_source_artifacts_have_immutable_commits() -> None:
    check_provenance = _load_check_module()
    data = check_provenance.load_manifest()
    artifacts = check_provenance.iter_artifacts(data)

    checked_kinds = {
        "source-archive",
        "python-source-archive",
        "git-source",
        "python-direct-git",
        "platformio-git-library",
    }
    missing = [
        artifact["id"]
        for artifact in artifacts
        if artifact.get("kind") in checked_kinds
        and not check_provenance.HEX40_RE.match(str(artifact.get("commit", "")))
    ]

    assert missing == []


def test_install_source_archive_mirrors_retain_upstream_provenance() -> None:
    check_provenance = _load_check_module()
    data = check_provenance.load_manifest()
    artifacts = check_provenance.artifacts_by_id(data)
    documented = check_provenance.provenance_strings(data)

    for artifact_id in (
        "nqptp",
        "shairport-sync",
        "webrtc-audio-processing-v2",
    ):
        artifact = artifacts[artifact_id]
        assert artifact["url"].startswith(
            "https://github.com/jaspercurry/JTS/releases/download/build-deps-v1/"
        )
        assert artifact["url"] == artifact["resolved_url"]
        assert artifact["upstream_url"].startswith("https://")
        assert artifact["upstream_resolved_url"].startswith("https://")
        assert artifact["commit"] in artifact["upstream_resolved_url"]
        assert artifact["upstream_resolved_url"] in documented

    broken = copy.deepcopy(data)
    for artifact in broken["artifact"]:
        if artifact["id"] == "nqptp":
            del artifact["upstream_url"]
            break

    errors = check_provenance.validate_artifacts(broken)

    assert any(
        "nqptp: mirrored source archive requires upstream_url" in e
        for e in errors
    )


def test_rust_fanin_lock_check_detects_dependency_drift(tmp_path: Path) -> None:
    check_provenance = _load_check_module()
    _write_rust_crate_with_missing_bar(tmp_path, "jasper-fanin")
    data = {
        "surface": [
            {
                "id": "rust-fanin-crates",
                "status": "pinned",
            }
        ]
    }

    errors: list[str] = []
    check_provenance._validate_rust_fanin_lock(data, tmp_path, errors)

    assert any("bar" in error for error in errors)


def test_rust_outputd_lock_check_detects_dependency_drift(tmp_path: Path) -> None:
    check_provenance = _load_check_module()
    _write_rust_crate_with_missing_bar(tmp_path, "jasper-outputd")
    data = {
        "surface": [
            {
                "id": "rust-outputd-crates",
                "status": "pinned",
            }
        ]
    }

    errors: list[str] = []
    check_provenance._validate_rust_outputd_lock(data, tmp_path, errors)

    assert any("bar" in error for error in errors)


def _write_rust_crate_with_missing_bar(tmp_path: Path, crate_name: str) -> None:
    crate_dir = tmp_path / "rust" / crate_name
    crate_dir.mkdir(parents=True)
    (crate_dir / "Cargo.toml").write_text(
        f"""
[package]
name = "{crate_name}"
version = "0.1.0"

[dependencies]
foo = "1"
bar = "1"
""".lstrip(),
        encoding="utf-8",
    )
    (crate_dir / "Cargo.lock").write_text(
        f"""
version = 3

[[package]]
name = "{crate_name}"
version = "0.1.0"
dependencies = [
 "foo",
]
""".lstrip(),
        encoding="utf-8",
    )
