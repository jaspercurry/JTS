# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Both ESP32 accessories consume one jasper-control discovery library."""

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMMON = ROOT / "firmware/common/jasper-control-discovery"
PROJECTS = (
    ROOT / "firmware/dial",
    ROOT / "firmware/satellite-amoled",
)


def test_discovery_has_one_platformio_library_owner() -> None:
    manifest = json.loads((COMMON / "library.json").read_text())
    assert manifest["name"] == "jasper-control-discovery"
    assert manifest["build"] == {"srcDir": "src", "includeDir": "include"}
    assert (COMMON / "src/discovery.cpp").is_file()
    assert (COMMON / "include/discovery.h").is_file()
    for project in PROJECTS:
        ini = (project / "platformio.ini").read_text()
        assert "lib_extra_dirs" not in ini
        assert (
            "jasper-control-discovery="
            "symlink://../common/jasper-control-discovery"
        ) in ini
        assert not (project / "src/discovery.cpp").exists()
        assert not (project / "src/discovery.h").exists()


def test_both_firmware_call_the_shared_public_contract() -> None:
    header = (COMMON / "include/discovery.h").read_text()
    implementation = (COMMON / "src/discovery.cpp").read_text()
    assert "struct ControlEndpoint" in header
    assert "ControlEndpoint discoverControlEndpoint();" in header
    assert "ControlEndpoint discoverControlEndpoint()" in implementation
    for project in PROJECTS:
        main = (project / "src/main.cpp").read_text()
        assert '#include "discovery.h"' in main
        assert "discoverControlEndpoint()" in main
