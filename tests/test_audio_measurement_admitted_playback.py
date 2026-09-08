# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from jasper.audio_measurement.admitted_playback import GeneratedExcitationWav
from jasper.audio_measurement.evidence_identity import ArtifactIdentity


@pytest.fixture
def stimulus():
    return GeneratedExcitationWav(
        generation_artifact_fingerprint="1" * 64,
        excitation_plan_fingerprint="2" * 64,
        artifact=ArtifactIdentity(
            bundle_kind="jts_active_speaker_commissioning",
            bundle_id="evidence-session-1",
            relative_path="stimulus.wav",
            sha256="3" * 64,
            byte_size=64_044,
        ),
    )


def test_generated_stimulus_schema_round_trip(stimulus):
    stored = json.loads(json.dumps(stimulus.to_dict()))

    assert GeneratedExcitationWav.from_mapping(stored) == stimulus


@pytest.mark.parametrize("field_path,value", [
    (("generation_artifact_fingerprint",), "f" * 64),
    (("excitation_plan_fingerprint",), "f" * 64),
    (("artifact", "sha256"), "f" * 64),
    (("artifact", "byte_size"), 64_046),
    (("fingerprint",), "f" * 64),
    (("schema_version",), True),
    (("schema_version",), 2),
    (("kind",), "other"),
    (("extra",), True),
    (("artifact",), None),
    (("generation_artifact_fingerprint",), "F" * 64),
    (("excitation_plan_fingerprint",), "short"),
])
def test_generated_stimulus_rejects_changed_or_invalid_records(stimulus, field_path, value):
    stored = stimulus.to_dict()
    entry = stored
    for field in field_path[:-1]:
        entry = entry[field]
    entry[field_path[-1]] = value

    with pytest.raises(ValueError):
        GeneratedExcitationWav.from_mapping(stored)


def test_generated_stimulus_reader_does_not_import_runtime_hosts():
    forbidden = (
        "jasper.active_speaker",
        "jasper.camilla",
        "jasper.correction",
        "jasper.dsp_apply",
        "jasper.web",
    )
    probe = (
        "import sys, jasper.audio_measurement.admitted_playback;"
        f"print(sorted(m for m in sys.modules if any("
        f"m == p or m.startswith(p + '.') for p in {forbidden!r})))"
    )
    out = subprocess.check_output(
        [sys.executable, "-c", probe], text=True, stderr=subprocess.STDOUT,
    )
    assert json.loads(out) == []
