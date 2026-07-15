# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Keep the canonical audio-slice inventory aligned with systemd truth."""

from pathlib import Path
import re

from jasper import _oom_adj


ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "deploy/systemd"
SLICE = SYSTEMD / "jts-audio.slice"
RESILIENCE = ROOT / "docs/HANDOFF-resilience.md"

EXPECTED = {
    "bluealsa-aplay",
    "jasper-camilla-crossover",
    "jasper-camilla",
    "jasper-fanin",
    "jasper-outputd",
    "jasper-snapclient",
    "jasper-snapserver",
    "librespot",
    "shairport-sync",
}


def _audio_slice_members() -> set[str]:
    members = {
        path.stem
        for path in SYSTEMD.glob("*.service")
        if any(line.strip() == "Slice=jts-audio.slice" for line in path.read_text().splitlines())
    }
    for dropin in SYSTEMD.glob("*.service.d/*.conf"):
        if any(
            line.strip() == "Slice=jts-audio.slice"
            for line in dropin.read_text().splitlines()
        ):
            members.add(dropin.parent.name.removesuffix(".service.d"))
    return members


def _named_audio_units(text: str) -> set[str]:
    return set(re.findall(
        r"\b(?:jasper-[a-z0-9-]+|shairport-sync|librespot|bluealsa-aplay)\b",
        text,
    ))


def _explicit_oom_adjustments() -> dict[str, int]:
    adjustments: dict[str, int] = {}
    paths = [*SYSTEMD.glob("*.service"), *SYSTEMD.glob("*.service.d/*.conf")]
    for path in paths:
        name = (
            path.parent.name.removesuffix(".service.d")
            if path.suffix == ".conf"
            else path.stem
        )
        for line in path.read_text().splitlines():
            match = re.fullmatch(r"OOMScoreAdjust=([+-]?\d+)", line.strip())
            if match:
                adjustments[name] = int(match.group(1))
    return adjustments


def test_slice_header_and_resilience_diagram_name_every_member() -> None:
    assert _audio_slice_members() == EXPECTED
    header = SLICE.read_text().split("[Unit]", 1)[0]
    doc = RESILIENCE.read_text()
    stage_two = doc.split("**2. Carve audio + mic daemons", 1)[1]
    diagram = stage_two.split("```", 2)[1]
    assert _named_audio_units(header) == EXPECTED
    assert _named_audio_units(diagram.split("jts-mic.slice", 1)[0]) == EXPECTED


def test_resilience_oom_ladder_covers_every_explicit_adjustment() -> None:
    doc = RESILIENCE.read_text()
    explicit = _explicit_oom_adjustments()
    assert _oom_adj.EXPECTED == explicit
    ladder = doc.split("| Daemon | OOMScoreAdjust |", 1)[1].split(
        "Critical:", 1,
    )[0]
    documented: dict[str, int] = {}
    for line in ladder.splitlines():
        cells = [cell.strip() for cell in line.split("|")]
        if len(cells) < 4 or not re.fullmatch(r"[+-]?\d+", cells[2]):
            continue
        for daemon in re.findall(r"`([^`]+)`", cells[1]):
            documented["ssh" if daemon == "sshd" else daemon] = int(cells[2])
    assert documented == explicit
