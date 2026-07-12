# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Single-source-of-truth guard for the core-graph park list.

Before restarting the core DSP graph (CamillaDSP / outputd / fan-in), the
units that can hold a DAC / Camilla / renderer ALSA endpoint must be
stopped, or the graph start fails with "Device or resource busy" (EBUSY) —
the exact failure class the camilla EBUSY recovery handler exists to fix
(docs/HANDOFF-resilience.md). That list of "audio clients to park" was
duplicated BYTE-FOR-BYTE across two bash files with no shared source and
no test pinning them equal:

  1. runtime recovery  deploy/bin/jasper-camilla-recover  (stop loop)
  2. install-time      deploy/lib/install/systemd-units.sh
                       park_audio_clients_for_core_graph_restart()

A future edit to one (e.g. a new renderer that holds the DAC) would drift
the other and re-leak a holder. The list now lives once in
deploy/lib/jasper-core-graph-park-units.sh as JASPER_CORE_GRAPH_PARK_UNITS,
sourced by both consumers. These tests pin that:

  * the canonical fragment holds exactly the expected ordered set;
  * the recovery script, run end-to-end, issues `stop` for every unit in
    the SOURCED list (behaviour, not source text — a stale copy could not
    pass this);
  * neither consumer re-inlines a park list (the consolidation can't
    silently regress to a second hardcoded copy);
  * both install paths (full speaker + streambox) install the fragment to
    the runtime path the deployed recovery script sources.

Mirrors tests/test_lib_deploy_direction.py /
tests/test_wifi_profile_hardening_contract.py. See AGENTS.md
"Pin promises with tests".

Scope note: the multiroom-follower park set
(jasper.local_sources.registry.local_source_park_units) is a DIFFERENT,
legitimately-separate set (it parks bluealsa/bt-agent/usbsink and omits
the core daemons) and is intentionally NOT consolidated here.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRAGMENT = ROOT / "deploy" / "lib" / "jasper-core-graph-park-units.sh"
RECOVER = ROOT / "deploy" / "bin" / "jasper-camilla-recover"
SYSTEMD_UNITS = ROOT / "deploy" / "lib" / "install" / "systemd-units.sh"

# The canonical contract: the ordered set of audio clients to stop before a
# core-graph restart. Keep in sync with the fragment ONLY by editing the
# fragment — this literal is the assertion target, not a second source.
CANONICAL_PARK_UNITS = [
    "jasper-voice.service",
    "jasper-aec-bridge.service",
    "jasper-outputd.service",
    "jasper-camilla-crossover.service",
    "jasper-snapclient.service",
    "jasper-snapserver.service",
    "shairport-sync.service",
    "nqptp.service",
    "librespot.service",
    "bluealsa-aplay.service",
    "jasper-mux.service",
]


def _source_fragment_units() -> list[str]:
    """Source the fragment under bash and return JASPER_CORE_GRAPH_PARK_UNITS.

    Tests the real array the consumers see, not the source text — a typo
    that broke the array definition would fail here."""
    script = (
        f'source "{FRAGMENT}"\n'
        'printf "%s\\n" "${JASPER_CORE_GRAPH_PARK_UNITS[@]}"\n'
    )
    proc = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    return [line for line in proc.stdout.splitlines() if line]


def test_fragment_defines_canonical_ordered_park_list():
    assert _source_fragment_units() == CANONICAL_PARK_UNITS


def test_recover_script_stops_exactly_the_sourced_park_list():
    """End-to-end: the recovery handler parks every unit in the SOURCED list.

    Binds the runtime stop behaviour to the single source — a drifted /
    stale copy of the list could not produce these exact `stop` calls."""
    expected = _source_fragment_units()
    assert expected, "fragment produced no units"

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        calls = tmp_path / "systemctl.calls"
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        asound = tmp_path / "asound"
        (asound / "card0" / "pcm0p" / "sub0").mkdir(parents=True)
        dev_snd = tmp_path / "dev_snd"
        dev_snd.mkdir()

        fake_systemctl = bin_dir / "fake-systemctl"
        fake_systemctl.write_text(
            f"#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> {calls}\nexit 0\n",
            encoding="utf-8",
        )
        fake_systemctl.chmod(0o755)

        env = os.environ.copy()
        env.update(
            {
                "JASPER_SYSTEMCTL": str(fake_systemctl),
                "JASPER_CAMILLA_RECOVER_STATE_DIR": str(tmp_path / "state"),
                "JASPER_CAMILLA_RECOVER_RUN_DIR": str(run_dir),
                "JASPER_ASOUND_ROOT": str(asound),
                "JASPER_DEV_SND_ROOT": str(dev_snd),
                "PATH": f"{bin_dir}:{env.get('PATH', '')}",
            }
        )

        result = subprocess.run(
            [str(RECOVER), "--reason", "park-contract"],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr

        call_text = calls.read_text(encoding="utf-8")
        for unit in expected:
            assert f"stop {unit}" in call_text, (
                f"recovery handler did not stop {unit}; "
                "the sourced park list drifted from the runtime stop loop"
            )


# The full multi-line park stop-loop shape, present ONLY in the fragment.
# If either consumer re-inlines the list, this pattern reappears there and
# the no-re-inline tests below fail. (jasper-voice.service is the first park
# unit and the most distinctive head of the stop list.)
_INLINE_PARK_BLOCK = re.compile(
    r"jasper-voice\.service\s*\\?\s*\n\s*"
    r"jasper-aec-bridge\.service\s*\\?\s*\n\s*"
    r"jasper-outputd\.service",
)


def test_recover_consumer_sources_fragment_and_has_no_inline_park_list():
    text = RECOVER.read_text(encoding="utf-8")
    # Iterates the sourced array.
    assert 'for unit in "${JASPER_CORE_GRAPH_PARK_UNITS[@]}"' in text
    # Sources the canonical fragment (sibling-first) with a loud-fail loader.
    assert "jasper-core-graph-park-units.sh" in text
    assert "load_core_graph_park_units" in text
    # No re-inlined hardcoded park list.
    assert not _INLINE_PARK_BLOCK.search(text), (
        "jasper-camilla-recover re-inlined the core-graph park list; "
        "iterate JASPER_CORE_GRAPH_PARK_UNITS from the shared fragment instead"
    )


def test_installer_consumer_sources_fragment_and_has_no_inline_park_list():
    text = SYSTEMD_UNITS.read_text(encoding="utf-8")
    assert "source " in text and "jasper-core-graph-park-units.sh" in text
    assert 'for unit in "${JASPER_CORE_GRAPH_PARK_UNITS[@]}"' in text
    assert not _INLINE_PARK_BLOCK.search(text), (
        "park_audio_clients_for_core_graph_restart re-inlined the park list; "
        "iterate JASPER_CORE_GRAPH_PARK_UNITS from the shared fragment instead"
    )


def test_fragment_is_the_only_definition_of_the_inline_park_block():
    """The hardcoded stop-list shape exists in exactly one file."""
    matches = [
        path.name
        for path in (FRAGMENT, RECOVER, SYSTEMD_UNITS)
        if _INLINE_PARK_BLOCK.search(path.read_text(encoding="utf-8"))
    ]
    assert matches == [FRAGMENT.name], (
        f"the park-list literal should live only in {FRAGMENT.name}, "
        f"found in: {matches}"
    )


def test_recover_script_fails_loud_when_fragment_missing():
    """A missing fragment means a broken install, NOT a silent inline
    fallback list (which would reintroduce the duplication). The recovery
    handler must fail loud (exit 66) so the OnFailure= wiring surfaces it."""
    env = os.environ.copy()
    env["JASPER_CORE_GRAPH_PARK_UNITS_LIB"] = "/nonexistent/park-units.sh"
    result = subprocess.run(
        [str(RECOVER), "--reason", "missing-fragment"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 66, result.stderr
    assert "missing core-graph park-list library" in result.stderr


def test_installer_installs_fragment_to_runtime_lib_path():
    """Both install paths copy the fragment where the deployed recovery
    script sources it (/usr/local/lib/jasper/), since /usr/local/sbin has no
    ../lib sibling."""
    text = SYSTEMD_UNITS.read_text(encoding="utf-8")
    install_line = re.compile(
        r"install -m 0644\s*\\\s*\n\s*"
        r'"\$\{REPO_DIR\}/deploy/lib/jasper-core-graph-park-units\.sh"\s*\\\s*\n\s*'
        r"/usr/local/lib/jasper/jasper-core-graph-park-units\.sh",
    )
    install_count = len(install_line.findall(text))
    # Both profiles call install_jasper_support_files; the file has one owner.
    assert install_count == 1, (
        "expected one shared support-file install owner, "
        f"found {install_count} install line(s)"
    )
    for function in ("install_streambox_systemd_units", "install_systemd_units"):
        body = text.split(f"{function}() {{", 1)[1].split("\n}\n", 1)[0]
        assert "install_jasper_support_files" in body
    # The runtime path the deployed recovery script falls back to.
    assert (
        "/usr/local/lib/jasper/jasper-core-graph-park-units.sh"
        in RECOVER.read_text(encoding="utf-8")
    )
