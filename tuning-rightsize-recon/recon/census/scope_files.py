#!/usr/bin/env python3
"""Enumerate the tuning-scope Python files at HEAD. Re-runnable; prints one path per line.

Scope per BRIEF.md:
  jasper/active_speaker/**.py
  jasper/audio_measurement/**.py
  jasper/correction/**.py
  jasper/attribution/**.py
  jasper/calibration_agent/**.py
  jasper/web/correction_*.py
  jasper/web/active_speaker_flow.py
  jasper/web/balance_*.py
  jasper/cli/<listed names>.py
  experiments/usb-turntable/**.py
"""
import subprocess
from pathlib import Path

REPO = Path("/home/user/JTS")
assert (REPO / "AGENTS.md").exists(), f"bad REPO guess: {REPO}"

CLI_NAMES = [
    "active_speaker", "audition", "active_speaker_attempts_replay",
    "crossover_prescriber", "project_ring", "classify_features",
    "read_distortion", "round_views", "round_bank", "round",
    "angle_capture", "arm_walk", "active_speaker_emit_bench",
    "basic_profile", "seat_level", "delay_sweep", "forward_model",
    "gate_sweep", "close_reference", "measure", "null_door",
    "bass_extension_bench", "declare_geometry", "correction_bundle",
    "measurement_mic",
]


def package_of(path: Path) -> str:
    rel = path.relative_to(REPO).as_posix()
    if rel.startswith("jasper/active_speaker/"):
        return "jasper/active_speaker"
    if rel.startswith("jasper/audio_measurement/"):
        return "jasper/audio_measurement"
    if rel.startswith("jasper/correction/"):
        return "jasper/correction"
    if rel.startswith("jasper/attribution/"):
        return "jasper/attribution"
    if rel.startswith("jasper/calibration_agent/"):
        return "jasper/calibration_agent"
    if rel.startswith("jasper/web/"):
        return "jasper/web"
    if rel.startswith("jasper/cli/"):
        return "jasper/cli"
    if rel.startswith("experiments/usb-turntable/"):
        return "experiments/usb-turntable"
    return "other"


def gather():
    files = set()
    for pkg in ["active_speaker", "audio_measurement", "correction", "attribution", "calibration_agent"]:
        base = REPO / "jasper" / pkg
        if base.exists():
            files.update(base.rglob("*.py"))

    web = REPO / "jasper" / "web"
    for p in web.glob("correction_*.py"):
        files.add(p)
    for p in web.glob("balance_*.py"):
        files.add(p)
    flow = web / "active_speaker_flow.py"
    if flow.exists():
        files.add(flow)

    cli = REPO / "jasper" / "cli"
    for name in CLI_NAMES:
        p = cli / f"{name}.py"
        if p.exists():
            files.add(p)
        else:
            print(f"WARNING: missing CLI file {p}", flush=True)

    turntable = REPO / "experiments" / "usb-turntable"
    if turntable.exists():
        files.update(turntable.rglob("*.py"))

    return sorted(files)


def gather_tests():
    """Test files whose name matches scope module stems, or that import scope packages."""
    files = gather()
    stems = {f.stem for f in files}
    tests_dir = REPO / "tests"
    matched = set()
    all_tests = list(tests_dir.rglob("test_*.py")) + list(tests_dir.rglob("*_test.py"))
    scope_import_prefixes = (
        "jasper.active_speaker", "jasper.audio_measurement", "jasper.correction",
        "jasper.attribution", "jasper.calibration_agent",
    )
    scope_web_web = ("jasper.web.correction_", "jasper.web.active_speaker_flow", "jasper.web.balance_")
    for t in all_tests:
        name_stem = t.stem
        # name match: test_<modulename> or <modulename>_test (exact, not substring —
        # short scope stems like "cli"/"loop"/"tools" would false-positive under substring).
        bare = name_stem
        if bare.startswith("test_"):
            bare = bare[len("test_"):]
        elif bare.endswith("_test"):
            bare = bare[: -len("_test")]
        matched_by_name = bare in stems
        if matched_by_name:
            matched.add(t)
            continue
        try:
            text = t.read_text(errors="replace")
        except Exception:
            continue
        for prefix in scope_import_prefixes + scope_web_web:
            if f"import {prefix}" in text or f"from {prefix}" in text:
                matched.add(t)
                break
    return sorted(matched)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "tests":
        for f in gather_tests():
            print(f.relative_to(REPO))
    else:
        for f in gather():
            print(f.relative_to(REPO))
