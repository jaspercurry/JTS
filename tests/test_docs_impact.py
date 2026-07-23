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


def _exclude_gitignored(paths: set[str]) -> set[str]:
    if not paths:
        return paths
    proc = subprocess.run(
        ["git", "check-ignore", "--stdin"],
        cwd=ROOT,
        input="\n".join(sorted(paths)) + "\n",
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    ignored = set(proc.stdout.splitlines()) if proc.returncode == 0 else set()
    return paths - ignored


def test_doc_map_valid():
    docs_impact = load_docs_impact()

    subsystems = docs_impact.load_map(ROOT / "docs" / "doc-map.toml")

    assert not docs_impact.validate_map(subsystems)
    assert any(subsystem.id == "docs-governance" for subsystem in subsystems)


def test_root_and_top_level_docs_are_intentionally_mapped():
    docs_impact = load_docs_impact()
    subsystems = docs_impact.load_map(ROOT / "docs" / "doc-map.toml")
    classified_docs = docs_impact.load_classified_docs(ROOT / "docs" / "doc-map.toml")
    mapped_docs = {doc for subsystem in subsystems for doc in subsystem.docs}
    root_docs = _exclude_gitignored(
        {str(path.relative_to(ROOT)) for path in ROOT.glob("*.md")}
    )
    docs_top = _exclude_gitignored(
        {str(path.relative_to(ROOT)) for path in (ROOT / "docs").glob("*.md")}
    )

    assert sorted((root_docs | docs_top) - mapped_docs - set(classified_docs)) == []


def test_historical_docs_are_never_canonical_routes():
    """Archived docs must stay out of the routing layer entirely.

    docs/historical/ holds executed runbooks and other frozen records
    (the moOde removal, the 2026-06-10 hw-validation runbook). They are
    outside the orphan sweep by construction (it globs top-level
    docs/*.md only), and nothing in doc-map.toml — neither a subsystem
    route nor a session-artifact classification — may point future
    maintainers at them as current operational truth.
    """
    docs_impact = load_docs_impact()
    subsystems = docs_impact.load_map(ROOT / "docs" / "doc-map.toml")
    classified_docs = docs_impact.load_classified_docs(ROOT / "docs" / "doc-map.toml")

    historical = sorted(
        str(path.relative_to(ROOT))
        for path in (ROOT / "docs" / "historical").glob("*.md")
    )
    assert historical, "docs/historical/ is empty — archive layout moved?"
    for doc in historical:
        assert all(doc not in subsystem.docs for subsystem in subsystems), doc
        assert doc not in classified_docs, doc
        assert docs_impact.impact_report(subsystems, (doc,)) == [], doc


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


def test_state_aggregate_routes_to_state_surface_docs():
    """The /state.resilience producer must route to HANDOFF-resilience.md (the
    doc that describes the /state.resilience.* keys). /state.chat also lives
    in this file, and /state.audio (volume_policy, sound profile) lives here
    too, so the path intentionally maps to conversation-history and volume as
    well. Pins the routing intent: the stale-glob guard only catches a rename,
    not a re-route of this path to the wrong subsystem."""

    docs_impact = load_docs_impact()
    subsystems = docs_impact.load_map(ROOT / "docs" / "doc-map.toml")

    report = docs_impact.impact_report(
        subsystems, ("jasper/control/state_aggregate.py",)
    )

    assert [item["id"] for item in report] == [
        "conversation-history",
        "volume-and-sound",
        "resilience-and-system-dashboard",
    ]
    assert "docs/conversation-history-plan.md" in report[0]["docs"]
    assert "docs/HANDOFF-volume.md" in report[1]["docs"]
    assert "docs/HANDOFF-resilience.md" in report[2]["docs"]


def test_landing_page_routes_to_web_design_system_not_conversation_history():
    """The shared management entrypoint is not owned by the /chat feature."""

    docs_impact = load_docs_impact()
    subsystems = docs_impact.load_map(ROOT / "docs" / "doc-map.toml")

    report = docs_impact.impact_report(subsystems, ("deploy/index.html",))

    assert [item["id"] for item in report] == ["web-design-system"]
    assert "docs/HANDOFF-management-ui.md" in report[0]["docs"]
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


def test_s0_sync_tools_route_to_distributed_active_docs():
    docs_impact = load_docs_impact()
    subsystems = docs_impact.load_map(ROOT / "docs" / "doc-map.toml")

    for path in ("scripts/s0-sync-bench.sh", "scripts/s0-sync-measure.py"):
        report = docs_impact.impact_report(subsystems, (path,))

        assert [item["id"] for item in report] == ["multiroom-grouping"], path
        assert "docs/HANDOFF-distributed-active.md" in report[0]["docs"], path


def test_doc_map_code_globs_match_at_least_one_tracked_file():
    """Stale-glob guard: a moved/renamed file leaves a code glob in
    doc-map.toml matching nothing, which silently un-routes the mapped
    docs (the PR bot just stops mentioning them). Every code glob must
    match at least one git-tracked file, using the same fnmatch
    semantics scripts/docs-impact.py applies to changed paths."""
    import subprocess

    docs_impact = load_docs_impact()
    subsystems = docs_impact.load_map(ROOT / "docs" / "doc-map.toml")
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, check=True, text=True,
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
