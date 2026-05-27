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


def test_binary_artifacts_have_sha256s() -> None:
    check_provenance = _load_check_module()
    data = check_provenance.load_manifest()
    artifacts = check_provenance.iter_artifacts(data)

    checked_kinds = {
        "release-archive",
        "release-deb",
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


def test_git_artifacts_have_immutable_commits() -> None:
    check_provenance = _load_check_module()
    data = check_provenance.load_manifest()
    artifacts = check_provenance.iter_artifacts(data)

    checked_kinds = {"git-source", "python-direct-git", "platformio-git-library"}
    missing = [
        artifact["id"]
        for artifact in artifacts
        if artifact.get("kind") in checked_kinds
        and not check_provenance.HEX40_RE.match(str(artifact.get("commit", "")))
    ]

    assert missing == []
