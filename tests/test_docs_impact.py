from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_docs_impact():
    path = ROOT / "scripts" / "docs-impact.py"
    spec = importlib.util.spec_from_file_location("docs_impact", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_doc_map_valid():
    docs_impact = load_docs_impact()

    subsystems = docs_impact.load_map(ROOT / "docs" / "doc-map.toml")

    assert not docs_impact.validate_map(subsystems)
    assert any(subsystem.id == "docs-governance" for subsystem in subsystems)


def test_root_and_top_level_docs_are_intentionally_mapped():
    docs_impact = load_docs_impact()
    subsystems = docs_impact.load_map(ROOT / "docs" / "doc-map.toml")
    mapped_docs = {doc for subsystem in subsystems for doc in subsystem.docs}
    root_docs = {str(path.relative_to(ROOT)) for path in ROOT.glob("*.md")}
    docs_top = {str(path.relative_to(ROOT)) for path in (ROOT / "docs").glob("*.md")}

    assert sorted((root_docs | docs_top) - mapped_docs) == []


def test_voice_file_routes_to_voice_docs():
    docs_impact = load_docs_impact()
    subsystems = docs_impact.load_map(ROOT / "docs" / "doc-map.toml")

    report = docs_impact.impact_report(
        subsystems, ("jasper/voice/openai_session.py",)
    )

    assert [item["id"] for item in report] == ["voice-runtime-and-providers"]
    assert "docs/HANDOFF-voice-providers.md" in report[0]["docs"]
    assert "docs/HANDOFF-prompting.md" in report[0]["docs"]


def test_vad_file_routes_to_voice_and_vad_docs():
    docs_impact = load_docs_impact()
    subsystems = docs_impact.load_map(ROOT / "docs" / "doc-map.toml")

    report = docs_impact.impact_report(subsystems, ("jasper/vad.py",))

    assert [item["id"] for item in report] == ["voice-runtime-and-providers"]
    assert "docs/HANDOFF-vad-experiments.md" in report[0]["docs"]


def test_voice_service_unit_does_not_trigger_global_deploy_docs():
    docs_impact = load_docs_impact()
    subsystems = docs_impact.load_map(ROOT / "docs" / "doc-map.toml")

    report = docs_impact.impact_report(
        subsystems, ("deploy/systemd/jasper-voice.service",)
    )

    assert [item["id"] for item in report] == ["voice-runtime-and-providers"]


def test_install_script_routes_to_deploy_docs():
    docs_impact = load_docs_impact()
    subsystems = docs_impact.load_map(ROOT / "docs" / "doc-map.toml")

    report = docs_impact.impact_report(subsystems, ("deploy/install.sh",))

    assert [item["id"] for item in report] == ["deploy-and-onboarding"]
