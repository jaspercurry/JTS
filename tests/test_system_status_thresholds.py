"""Threshold invariants for the /system/ dashboard status colours.

The page logic lives in browser ES modules, so these tests keep the risky
threshold math visible from Python CI. When Node is available we execute the
pure formatter module; static checks still guard the important wiring.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SYSTEM_JS = ROOT / "deploy" / "assets" / "system-status" / "js"
FORMAT_JS = SYSTEM_JS / "format.js"
SECTIONS_JS = SYSTEM_JS / "sections.js"


def test_system_vitals_use_named_threshold_helpers() -> None:
    sections = SECTIONS_JS.read_text()
    assert "toneForMemoryHeadroom(memAvail, memTotal)" in sections
    assert "loadPressureInfo(load, cores.length || 4)" in sections
    assert "cpuUsageInfo(cores)" in sections
    assert "temperatureInfo(temp, throttledNow, throttledHist)" in sections
    assert "toneForDiskUse(diskPct)" in sections

    # Regression guard for the old Pi Zero 2 W-hostile memory cutoffs.
    assert "memAvail < 150" not in sections
    assert "memAvail < 250" not in sections
    assert "swap > 150" not in sections


def test_system_threshold_helpers_document_the_colours() -> None:
    text = FORMAT_JS.read_text()
    assert "warn below max(100 MB, 10% total)" in text
    assert "danger below max(30 MB, 3% total)" in text
    assert "value > capacity" in text
    assert "value >= capacity * 0.75" in text
    assert "toneForPercent(Number(pct) || 0, 85, 95)" in text
    assert "temp >= 80 || throttledNow" in text


def test_system_threshold_helpers_scale_by_capacity_with_low_ram_floors() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js unavailable; static threshold tests still run")

    script = f"""
      import {{ readFileSync }} from "node:fs";
      const source = readFileSync({json.dumps(str(FORMAT_JS))}, "utf8");
      const mod = await import(
        "data:text/javascript;base64," + Buffer.from(source).toString("base64")
      );
      const tiers = [416, 900, 2048, 4096, 8192, 16384].map((totalMb) => ({{
        totalMb,
        limits: mod.memoryHeadroomLimits(totalMb),
        okAtWarn: mod.toneForMemoryHeadroom(mod.memoryHeadroomLimits(totalMb).warnMb, totalMb),
        warnBelowWarn: mod.toneForMemoryHeadroom(mod.memoryHeadroomLimits(totalMb).warnMb - 1, totalMb),
        dangerBelowDanger: mod.toneForMemoryHeadroom(mod.memoryHeadroomLimits(totalMb).dangerMb - 1, totalMb),
      }}));
      const out = {{
        tiers,
        lowRamLimits: mod.memoryHeadroomLimits(416),
        lowRamComfortable: mod.toneForMemoryHeadroom(142, 416),
        lowRamWarnFloor: mod.toneForMemoryHeadroom(99, 416),
        lowRamDangerFloor: mod.toneForMemoryHeadroom(29, 416),
        twoGbLimits: mod.memoryHeadroomLimits(2048),
        twoGbWarnByRatio: mod.toneForMemoryHeadroom(190, 2048),
        twoGbOkByRatio: mod.toneForMemoryHeadroom(230, 2048),
        eightGbLimits: mod.memoryHeadroomLimits(8192),
        eightGbWarnByRatio: mod.toneForMemoryHeadroom(700, 8192),
        eightGbDangerByRatio: mod.toneForMemoryHeadroom(200, 8192),
        loadOk: mod.loadPressureInfo(2.9, 4).tone,
        loadWarn: mod.loadPressureInfo(3.0, 4).tone,
        loadDanger: mod.loadPressureInfo(4.1, 4).tone,
        diskOk: mod.toneForDiskUse(84.9),
        diskWarn: mod.toneForDiskUse(85),
        diskDanger: mod.toneForDiskUse(95),
        cpuWarn: mod.cpuUsageInfo([75, 75, 75, 75]).tone,
        cpuDanger: mod.cpuUsageInfo([95, 95, 95, 95]).tone,
        tempWarn: mod.temperatureInfo(75, 0, 0).tone,
        tempDanger: mod.temperatureInfo(80, 0, 0).tone,
      }};
      console.log(JSON.stringify(out));
    """
    result = subprocess.run(
        [node, "--input-type=module"],
        input=script,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)

    assert out["lowRamLimits"] == {"warnMb": 100, "dangerMb": 30}
    assert out["lowRamComfortable"] == "ok"
    assert out["lowRamWarnFloor"] == "warn"
    assert out["lowRamDangerFloor"] == "danger"
    assert out["tiers"] == [
        {
            "totalMb": 416,
            "limits": {"warnMb": 100, "dangerMb": 30},
            "okAtWarn": "ok",
            "warnBelowWarn": "warn",
            "dangerBelowDanger": "danger",
        },
        {
            "totalMb": 900,
            "limits": {"warnMb": 100, "dangerMb": 30},
            "okAtWarn": "ok",
            "warnBelowWarn": "warn",
            "dangerBelowDanger": "danger",
        },
        {
            "totalMb": 2048,
            "limits": {"warnMb": 204, "dangerMb": 61},
            "okAtWarn": "ok",
            "warnBelowWarn": "warn",
            "dangerBelowDanger": "danger",
        },
        {
            "totalMb": 4096,
            "limits": {"warnMb": 409, "dangerMb": 122},
            "okAtWarn": "ok",
            "warnBelowWarn": "warn",
            "dangerBelowDanger": "danger",
        },
        {
            "totalMb": 8192,
            "limits": {"warnMb": 819, "dangerMb": 245},
            "okAtWarn": "ok",
            "warnBelowWarn": "warn",
            "dangerBelowDanger": "danger",
        },
        {
            "totalMb": 16384,
            "limits": {"warnMb": 1638, "dangerMb": 491},
            "okAtWarn": "ok",
            "warnBelowWarn": "warn",
            "dangerBelowDanger": "danger",
        },
    ]
    assert out["twoGbLimits"] == {"warnMb": 204, "dangerMb": 61}
    assert out["twoGbWarnByRatio"] == "warn"
    assert out["twoGbOkByRatio"] == "ok"
    assert out["eightGbLimits"] == {"warnMb": 819, "dangerMb": 245}
    assert out["eightGbWarnByRatio"] == "warn"
    assert out["eightGbDangerByRatio"] == "danger"
    assert out["loadOk"] == "ok"
    assert out["loadWarn"] == "warn"
    assert out["loadDanger"] == "danger"
    assert out["diskOk"] == "ok"
    assert out["diskWarn"] == "warn"
    assert out["diskDanger"] == "danger"
    assert out["cpuWarn"] == "warn"
    assert out["cpuDanger"] == "danger"
    assert out["tempWarn"] == "warn"
    assert out["tempDanger"] == "danger"
