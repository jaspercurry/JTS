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
# Wizard units live at the top of deploy/, not deploy/systemd/, but still
# carry OOMScoreAdjust= and so belong in the explicit-adjustment scan below.
DEPLOY = ROOT / "deploy"

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
    paths = [
        *SYSTEMD.glob("*.service"), *SYSTEMD.glob("*.service.d/*.conf"),
        *DEPLOY.glob("*.service"),
    ]
    for path in paths:
        name = (
            path.parent.name.removesuffix(".service.d")
            if path.suffix == ".conf"
            # The streambox web unit installs AS jasper-web.service
            # (systemd-units.sh), so it keys under that name too.
            else path.stem.removesuffix("-streambox")
        )
        for line in path.read_text().splitlines():
            match = re.fullmatch(r"OOMScoreAdjust=([+-]?\d+)", line.strip())
            if match:
                value = int(match.group(1))
                assert adjustments.get(name, value) == value, (
                    f"{name} OOMScoreAdjust diverges between its unit files"
                )
                adjustments[name] = value
    return adjustments


def test_slice_header_names_every_member() -> None:
    assert _audio_slice_members() == EXPECTED
    header = SLICE.read_text().split("[Unit]", 1)[0]
    assert _named_audio_units(header) == EXPECTED


def test_oom_constants_match_every_explicit_unit_adjustment() -> None:
    assert _oom_adj.EXPECTED == _explicit_oom_adjustments()
