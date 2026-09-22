# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import importlib.util
import subprocess
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


def test_historical_docs_are_never_canonical_routes():
    docs_impact = load_docs_impact()
    subsystems = docs_impact.load_map(ROOT / "docs" / "doc-map.toml")

    historical = sorted(
        str(path.relative_to(ROOT))
        for path in (ROOT / "docs" / "historical").glob("*.md")
    )
    assert historical, "docs/historical/ is empty — archive layout moved?"
    for doc in historical:
        assert all(doc not in subsystem.docs for subsystem in subsystems), doc
        assert docs_impact.impact_report(subsystems, (doc,)) == [], doc


def test_voice_file_routes_to_voice_docs():
    docs_impact = load_docs_impact()
    subsystems = docs_impact.load_map(ROOT / "docs" / "doc-map.toml")

    report = docs_impact.impact_report(subsystems, ("jasper/voice/openai_session.py",))

    assert [item["id"] for item in report] == ["voice-runtime-and-providers"]
    assert "docs/extensibility.md" in report[0]["docs"]
    assert "docs/tool-platform-plan.md" in report[0]["docs"]


def test_vad_file_routes_to_voice_and_vad_docs():
    docs_impact = load_docs_impact()
    subsystems = docs_impact.load_map(ROOT / "docs" / "doc-map.toml")

    report = docs_impact.impact_report(subsystems, ("jasper/vad.py",))

    assert [item["id"] for item in report] == ["voice-runtime-and-providers"]
    assert "docs/extensibility.md" in report[0]["docs"]


def test_state_aggregate_routes_to_state_surface_docs():
    """The /state.resilience producer must route to the resilience subsystem's
    mapped docs. /state.audio (volume_policy, sound profile) also lives here,
    so the path intentionally maps to volume as well. Pins the routing
    intent: the stale-glob guard only catches a rename, not a re-route of
    this path to the wrong subsystem."""

    docs_impact = load_docs_impact()
    subsystems = docs_impact.load_map(ROOT / "docs" / "doc-map.toml")

    report = docs_impact.impact_report(
        subsystems, ("jasper/control/state_aggregate.py",)
    )

    assert [item["id"] for item in report] == [
        "volume-and-sound",
        "resilience-and-system-dashboard",
    ]
    assert "docs/audio-paths.md" in report[0]["docs"]
    assert "AGENTS.md" in report[1]["docs"]


def test_landing_page_routes_to_web_design_system_not_conversation_history():
    """The shared management entrypoint is not owned by the /chat feature."""

    docs_impact = load_docs_impact()
    subsystems = docs_impact.load_map(ROOT / "docs" / "doc-map.toml")

    report = docs_impact.impact_report(subsystems, ("deploy/index.html",))

    assert [item["id"] for item in report] == ["web-design-system"]
    assert "docs/design-language.md" in report[0]["docs"]
    assert "docs/conversation-history-plan.md" not in report[0]["docs"]


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


def test_doc_map_code_globs_match_at_least_one_tracked_file():
    """Stale-glob guard: a moved/renamed file leaves a code glob in
    doc-map.toml matching nothing, which silently un-routes the mapped
    docs. Every code glob must match at least one git-tracked file, using
    the same fnmatch semantics scripts/docs-impact.py applies to changed
    paths."""
    docs_impact = load_docs_impact()
    subsystems = docs_impact.load_map(ROOT / "docs" / "doc-map.toml")
    tracked = subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.splitlines()

    stale = [
        f"{subsystem.id}: code glob matches no tracked file: {pattern}"
        for subsystem in subsystems
        # design-only entries deliberately pre-route ANTICIPATED code
        # paths (e.g. jasper/apple_music/**) to their design docs, so a
        # zero-match glob there is the point, not staleness.
        if subsystem.safety != "design-only"
        for pattern in subsystem.code
        if not any(docs_impact.pattern_matches(pattern, path) for path in tracked)
    ]
    assert stale == [], (
        "stale doc-map.toml code globs (file moved/renamed without "
        "updating the routing map?):\n" + "\n".join(stale)
    )
