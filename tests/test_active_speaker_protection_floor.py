# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contract pins for the declared tweeter protection floor (issue #2491).

One defect, three surfaces. Before this module existed, a crossover candidate
below the tweeter's own confirmed protective high-pass floor was invisible to
all three:

* the derived defense-in-depth high-pass was ``2.0 x fc``, so it followed the
  error DOWN (LR4 @ 2000 Hz on a 5000 Hz-floor tweeter emitted a 4000 Hz
  "protective" high-pass — below the floor, structurally);
* ``POST /active-speaker/check-path-safety`` returned ``status=pass``,
  ``blocker_count=0``, ``ok_to_load_active_config=true`` for that config;
* the crossover preview reported zero blockers at confirm time.

The pins below are deliberately split so no one of the three can mask another:
the clamp is proved without touching path safety, the gate is proved against a
staged candidate whose protective high-pass is ALREADY clamped, and the
never-nanny boundary (an undeclared floor, and a non-tweeter role) is pinned in
both directions.

A fourth family pins the gate's fail-closed rule. Staged metadata written
before this check existed carries neither compared fact and nothing in the
system forces a re-stage, so reading such a file as "no floor declared" handed
the #2491 config a green load gate — reproduced through the real handler by two
independent review lenses. Absence of the keys therefore refuses; an honest
undeclared driver, which staging writes as present-and-``None``, still passes.

A fifth family, section (g), pins the **apply-time** layer. All four families
above compare the floor only once a graph exists — none of them stood between a
chosen crossover frequency and an APPLIED graph, which was harmless only while
nothing varied Fc. ``camilla_yaml._assert_tweeter_crossover_honours_declared_floor``
closes that at the emitter, and section (g) keeps it honest in both directions:
it must refuse a below-floor corner, and it must not invent a floor where the
operator declared none.

The two real-box fixtures in ``tests/fixtures/active_speaker_protection_floor_20260814/``
are verbatim design drafts captured from jts.local's first sanctioned composite
arm (2026-08-14, issue #2491's closing comment): the 2000 Hz draft that armed
nothing and produced the 4000 Hz protective high-pass, and the honestly
re-issued 5000 Hz draft that did arm.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from jasper.active_speaker.calibration_level import calibration_level_payload
from jasper.active_speaker.camilla_yaml import (
    EMIT_GATE_TWEETER_CROSSOVER_BELOW_DECLARED_FLOOR,
    _assert_tweeter_crossover_honours_declared_floor,
    emit_active_speaker_baseline_config,
    emit_active_speaker_commissioning_config,
    emit_active_speaker_driver_domain_config,
    emit_active_speaker_program_config,
    emit_active_speaker_startup_config,
)
from jasper.active_speaker.crossover_preview import build_crossover_preview
from jasper.active_speaker.design_draft import (
    DRIVER_RESEARCH_KIND,
    build_design_draft,
)
from jasper.active_speaker.driver_protection import (
    declared_protection_highpass_floor_hz,
    protection_highpass_floor_satisfied,
)
from jasper.active_speaker.path_safety import (
    FLOOR_FACT_KEYS,
    _tweeter_protection_floor_verdict,
    build_startup_load_path_safety_evidence,
    evaluate_path_safety_evidence,
)
from jasper.active_speaker.profile import ActiveSpeakerConfigError
from jasper.active_speaker.staging import (
    compile_preset_from_crossover_preview,
    stage_protected_startup_config,
)
from jasper.active_speaker.test_signal_plan import (
    PROTECTIVE_TWEETER_HP_MULTIPLIER,
    protective_tweeter_highpass_frequency_hz,
)
from jasper.output_topology import OutputTopology
from tests.active_speaker_fixtures import (
    mono_output_topology,
    valid_camilla_config as _valid_config,
)

REAL_BOX_FIXTURES = Path(__file__).parent / "fixtures" / (
    "active_speaker_protection_floor_20260814"
)
BELOW_FLOOR_DRAFT = REAL_BOX_FIXTURES / "design-draft-2000hz-below-floor.json"
AT_FLOOR_DRAFT = REAL_BOX_FIXTURES / "design-draft-5000hz-at-floor.json"


def _highpass_filters(cutoff_hz: float) -> list[dict[str, Any]]:
    return [{
        "kind": "highpass",
        "cutoff_hz": cutoff_hz,
        "minimum_slope_db_per_octave": 24.0,
        "family_or_equivalent": "equivalent_or_steeper",
    }]


def _driver_research(
    *,
    fc_hz: float,
    tweeter_floor_hz: float | None = None,
    woofer_floor_hz: float | None = None,
) -> dict[str, Any]:
    # #2603: the declared floor has ONE owner now. This fixture used to state
    # it twice -- a recommended_highpass_hz of 1500 alongside a separately
    # typed required_protection_filters cutoff -- which is the exact shape the
    # 2026-08-17 ruling collapsed. The floor is now declared once, as the
    # driver's minimum recommended crossover, and the protective high-pass
    # DERIVES from it. "Undeclared" means the owner is genuinely absent, which
    # is what keeps the never-nanny cases below honest.
    tweeter: dict[str, Any] = {
        "role": "tweeter",
        "manufacturer": "Eminence",
        "model": "F110M-8",
        "sources": ["https://example.test/tweeter"],
    }
    if tweeter_floor_hz is not None:
        tweeter["recommended_highpass_hz"] = tweeter_floor_hz
    woofer: dict[str, Any] = {
        "role": "woofer",
        "manufacturer": "Dayton Audio",
        "model": "Epique E150HE-44",
        "usable_frequency_range_hz": [45, 8000],
        "recommended_lowpass_hz": fc_hz,
        "sources": ["https://example.test/woofer"],
    }
    if woofer_floor_hz is not None:
        woofer["recommended_highpass_hz"] = woofer_floor_hz
    return {
        "artifact_schema_version": 1,
        "kind": DRIVER_RESEARCH_KIND,
        "drivers": [woofer, tweeter],
        "crossover_candidates": [{
            "between_roles": ["woofer", "tweeter"],
            "frequency_hz": fc_hz,
            "filter_type": "Linkwitz-Riley",
            "slope_db_per_octave": 24,
            "confidence": "medium",
        }],
    }


def _preview(
    topology: OutputTopology,
    *,
    fc_hz: float,
    tweeter_floor_hz: float | None = None,
    woofer_floor_hz: float | None = None,
) -> dict[str, Any]:
    return build_crossover_preview(
        build_design_draft(
            topology,
            driver_research=_driver_research(
                fc_hz=fc_hz,
                tweeter_floor_hz=tweeter_floor_hz,
                woofer_floor_hz=woofer_floor_hz,
            ),
            created_at="2026-08-14T12:00:00Z",
        ),
        created_at="2026-08-14T12:30:00Z",
    )


def _preset(topology: OutputTopology, preview: dict[str, Any]):
    preset, issues, _gates = compile_preset_from_crossover_preview(topology, preview)
    assert issues == [], issues
    assert preset is not None
    return preset


def _stage(tmp_path: Path, topology: OutputTopology, preview: dict[str, Any]) -> dict:
    return stage_protected_startup_config(
        topology,
        crossover_preview=preview,
        config_path=tmp_path / "active_staged.yml",
        metadata_path=tmp_path / "active_staged.json",
        validate=_valid_config,
        created_at="2026-08-14T13:00:00Z",
    )


def _report(topology: OutputTopology, staged: dict[str, Any], tmp_path: Path) -> dict:
    rollback = tmp_path / "rollback.yml"
    rollback.write_text(
        (tmp_path / "active_staged.yml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    evidence = build_startup_load_path_safety_evidence(
        topology,
        staged_config=staged,
        calibration_level=calibration_level_payload(),
        current_config_path=rollback,
    )
    return evaluate_path_safety_evidence(evidence)


def _floor_blockers(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        issue
        for issue in report["issues"]
        if issue["code"] == "tweeter_protection_floor_honoured_not_verified"
    ]


def _strip_floor_facts(tmp_path: Path) -> dict[str, Any]:
    """Rewrite the staged metadata as a pre-PR build would have written it.

    Byte-faithful, and re-derivable::

        git show 9d5a9597d:jasper/active_speaker/staging.py \\
          | grep -n 'tweeter_protection_floor_hz\\|tweeter_crossover_highpass_hz'

    returns nothing at this branch's merge base — the pre-PR emitter wrote only
    ``tweeter_protective_highpass_hz``. Nothing else in the metadata, and
    nothing at all in the staged YAML, is touched. This is the exact artifact
    both review lenses used to prove that nothing forces a re-stage.
    """

    metadata_path = tmp_path / "active_staged.json"
    staged = json.loads(metadata_path.read_text(encoding="utf-8"))
    for key in FLOOR_FACT_KEYS:
        staged["config"].pop(key, None)
    metadata_path.write_text(json.dumps(staged), encoding="utf-8")
    assert all(key not in staged["config"] for key in FLOOR_FACT_KEYS)
    return staged


def _endpoint_report(
    tmp_path: Path,
    topology: OutputTopology,
    monkeypatch,
) -> dict[str, Any]:
    """Drive the real ``check-path-safety`` coroutine over persisted state."""

    from jasper.output_topology import save_output_topology
    from jasper.web.sound_setup import _active_speaker_check_path_safety_payload

    topology_path = tmp_path / "output_topology.json"
    save_output_topology(topology, topology_path)
    rollback = tmp_path / "rollback.yml"
    rollback.write_text(
        (tmp_path / "active_staged.yml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_STAGED_METADATA_PATH",
        str(tmp_path / "active_staged.json"),
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_PATH_SAFETY_EVIDENCE",
        str(tmp_path / "path_safety.json"),
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_STARTUP_LOAD_STATE",
        str(tmp_path / "startup_load.json"),
    )

    class _Camilla:
        async def get_config_file_path(self, *, best_effort: bool = False) -> str:
            return str(rollback)

    return asyncio.run(
        _active_speaker_check_path_safety_payload(camilla_factory=_Camilla)
    )


# --- (a) the clamp: a below-floor candidate is raised TO the floor -----------


def test_below_floor_candidate_clamps_protective_highpass_to_the_declared_floor(
    tmp_path: Path,
) -> None:
    topology = mono_output_topology()
    preview = _preview(topology, fc_hz=2000, tweeter_floor_hz=5000)
    preset = _preset(topology, preview)

    assert preset.drivers["tweeter"].protection_highpass_floor_hz == 5000.0
    # The pre-fix derivation was 2000 * 2.0 = 4000 -- below the floor.
    assert protective_tweeter_highpass_frequency_hz(preset, "tweeter") == 5000.0

    staged = _stage(tmp_path, topology, preview)
    text = (tmp_path / "active_staged.yml").read_text(encoding="utf-8")

    assert staged["config"]["tweeter_protective_highpass_hz"] == 5000.0
    assert "freq: 5000.0000" in text
    assert "freq: 4000.0000" not in text


# --- (b) no regression to clamp-always --------------------------------------


def test_at_or_above_floor_candidate_keeps_the_derived_multiple() -> None:
    topology = mono_output_topology()

    at_floor = _preset(topology, _preview(topology, fc_hz=5000, tweeter_floor_hz=5000))
    above = _preset(topology, _preview(topology, fc_hz=6000, tweeter_floor_hz=5000))

    # ``>=`` boundary: crossing exactly AT the floor honours it, and the derived
    # multiple (10000) wins over the floor (5000) because it is stricter.
    assert protective_tweeter_highpass_frequency_hz(at_floor, "tweeter") == (
        5000 * PROTECTIVE_TWEETER_HP_MULTIPLIER
    )
    assert protective_tweeter_highpass_frequency_hz(above, "tweeter") == (
        6000 * PROTECTIVE_TWEETER_HP_MULTIPLIER
    )


# --- (f) no declared floor -> current behaviour, unchanged ------------------


def test_undeclared_floor_keeps_the_unclamped_multiple(tmp_path: Path) -> None:
    topology = mono_output_topology()
    preview = _preview(topology, fc_hz=2000, tweeter_floor_hz=None)
    preset = _preset(topology, preview)

    assert preset.drivers["tweeter"].protection_highpass_floor_hz is None
    # The clamp must not invent a floor where the operator declared none: the
    # class-default code policy (5000 Hz for an undeclared HF style) is NOT
    # substituted here.
    assert protective_tweeter_highpass_frequency_hz(preset, "tweeter") == 4000.0

    staged = _stage(tmp_path, topology, preview)
    report = _report(topology, staged, tmp_path)

    # Present-and-null, not absent: this is what distinguishes an honest
    # undeclared driver from a staged file the floor check cannot read.
    assert all(key in staged["config"] for key in FLOOR_FACT_KEYS)
    assert staged["config"]["tweeter_protection_floor_hz"] is None
    assert _floor_blockers(report) == []
    assert report["status"] == "pass"


# --- (c) the gate: check-path-safety refuses, by name -----------------------


def test_path_safety_refuses_a_below_floor_staged_candidate(tmp_path: Path) -> None:
    topology = mono_output_topology()
    staged = _stage(tmp_path, topology, _preview(
        topology, fc_hz=2000, tweeter_floor_hz=5000,
    ))
    report = _report(topology, staged, tmp_path)

    assert staged["config"]["tweeter_crossover_highpass_hz"] == 2000.0
    assert staged["config"]["tweeter_protection_floor_hz"] == 5000.0
    assert report["status"] == "blocked"
    assert report["requirements_met"] is False
    assert report["ok_to_load_active_config"] is False
    assert report["load_gate"] == "requirements_blocked"

    blockers = _floor_blockers(report)
    assert len(blockers) == 1
    message = blockers[0]["message"]
    assert blockers[0]["severity"] == "blocker"
    assert blockers[0]["path_id"] == "startup_reload"
    # The refusal names the driver, the corner, the floor, and the remedy --
    # not a bare "<check> is not verified".
    assert "tweeter" in message
    assert "2000 Hz" in message
    assert "5000 Hz" in message
    assert "raise the crossover" in message

    startup_reload = next(
        path for path in report["paths"] if path["id"] == "startup_reload"
    )
    assert startup_reload["checks"]["tweeter_protection_floor_honoured"] is False


def test_check_path_safety_endpoint_payload_refuses_a_below_floor_candidate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The refusal reaches the HTTP surface, not just the evidence builder.

    ``POST /active-speaker/check-path-safety`` dispatches to
    ``_active_speaker_check_path_safety_payload``; this drives that coroutine
    over persisted state so the claim "check-path-safety refuses" is pinned at
    the response object the household's browser actually receives.
    """

    from jasper.output_topology import save_output_topology
    from jasper.web.sound_setup import _active_speaker_check_path_safety_payload

    topology = mono_output_topology()
    topology_path = tmp_path / "output_topology.json"
    save_output_topology(topology, topology_path)
    rollback = tmp_path / "rollback.yml"

    staged = _stage(tmp_path, topology, _preview(
        topology, fc_hz=2000, tweeter_floor_hz=5000,
    ))
    rollback.write_text(
        (tmp_path / "active_staged.yml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    assert staged["status"] == "staged"

    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_STAGED_METADATA_PATH",
        str(tmp_path / "active_staged.json"),
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_PATH_SAFETY_EVIDENCE",
        str(tmp_path / "path_safety.json"),
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_STARTUP_LOAD_STATE",
        str(tmp_path / "startup_load.json"),
    )

    class _Camilla:
        async def get_config_file_path(self, *, best_effort: bool = False) -> str:
            return str(rollback)

    payload = asyncio.run(
        _active_speaker_check_path_safety_payload(camilla_factory=_Camilla)
    )
    report = payload["report"]

    assert report["status"] == "blocked"
    assert report["ok_to_load_active_config"] is False
    assert report["blocker_count"] >= 1
    blockers = _floor_blockers(report)
    assert len(blockers) == 1
    assert "5000 Hz" in blockers[0]["message"]
    # The named blocker is also persisted on the evidence the gate binds to.
    assert any(
        issue["code"] == "tweeter_crossover_below_declared_protection_floor"
        for issue in payload["evidence"]["observed_issues"]
    )
    assert payload["startup_load"]["preflight"]["status"] != "ready"


def test_path_safety_passes_an_at_floor_staged_candidate(tmp_path: Path) -> None:
    topology = mono_output_topology()
    staged = _stage(tmp_path, topology, _preview(
        topology, fc_hz=5000, tweeter_floor_hz=5000,
    ))
    report = _report(topology, staged, tmp_path)

    assert staged["config"]["tweeter_protective_highpass_hz"] == 10000.0
    assert report["status"] == "pass"
    assert report["ok_to_load_active_config"] is True
    assert _floor_blockers(report) == []


# --- pre-PR staged metadata: unreadable means refuse, not "no floor" --------


def test_staging_always_writes_both_floor_facts_so_absence_discriminates(
    tmp_path: Path,
) -> None:
    """The premise the whole fail-closed rule rests on.

    A declared driver and an undeclared one BOTH get both keys written; the
    undeclared one gets an explicit ``None``. So *key absent* can only mean
    "written by a build that predates this check", never "declared nothing".
    """

    topology = mono_output_topology()

    declared = _stage(tmp_path / "a", topology, _preview(
        topology, fc_hz=5000, tweeter_floor_hz=5000,
    ))
    undeclared = _stage(tmp_path / "b", topology, _preview(
        topology, fc_hz=2000, tweeter_floor_hz=None,
    ))

    for staged in (declared, undeclared):
        assert all(key in staged["config"] for key in FLOOR_FACT_KEYS)
    assert declared["config"]["tweeter_protection_floor_hz"] == 5000.0
    assert undeclared["config"]["tweeter_protection_floor_hz"] is None
    assert undeclared["config"]["tweeter_crossover_highpass_hz"] == 2000.0


def test_pre_pr_staged_metadata_is_refused_through_the_real_endpoint(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The review lenses' exact repro: nothing forces a re-stage.

    Both lenses staged the below-floor design, deleted only the two keys this
    change added (a byte-faithful pre-PR artifact — the staged YAML still
    carries the 2000 Hz crossover), and got ``status=pass`` /
    ``ok_to_load_active_config=true`` / ``load_gate=ready`` back out of the real
    handler. Nothing re-stages on their behalf: SCHEMA_VERSION is unbumped, the
    loader applies no freshness test, the evidence binding hashes the YAML, no
    deploy step clears the file, and neither load route stages.
    """

    topology = mono_output_topology()
    _stage(tmp_path, topology, _preview(topology, fc_hz=2000, tweeter_floor_hz=5000))
    legacy = _strip_floor_facts(tmp_path)

    # The staged graph is untouched and still carries the below-floor design.
    assert "freq: 2000.0000" in (tmp_path / "active_staged.yml").read_text(
        encoding="utf-8"
    )
    assert legacy["status"] == "staged"

    payload = _endpoint_report(tmp_path, topology, monkeypatch)
    report = payload["report"]

    assert report["status"] == "blocked"
    assert report["ok_to_load_active_config"] is False
    assert report["load_gate"] == "requirements_blocked"
    blockers = _floor_blockers(report)
    assert len(blockers) == 1
    assert "stage the protected startup config again" in blockers[0]["message"]
    assert any(
        issue["code"] == "staged_tweeter_protection_floor_unverifiable"
        for issue in payload["evidence"]["observed_issues"]
    )
    assert payload["startup_load"]["preflight"]["load_allowed"] is False


def test_pre_pr_staged_metadata_is_refused_even_for_an_honest_candidate(
    tmp_path: Path,
) -> None:
    """The rule is "refuse unless provable", not "refuse below-floor".

    An at-floor design staged by a pre-PR build is equally unprovable, so it is
    equally refused. That is deliberate: the check cannot tell the two apart
    from the artifact, and guessing in the permissive direction is what the
    review lenses proved unsafe.
    """

    topology = mono_output_topology()
    _stage(tmp_path, topology, _preview(topology, fc_hz=5000, tweeter_floor_hz=5000))
    legacy = _strip_floor_facts(tmp_path)
    report = _report(topology, legacy, tmp_path)

    assert report["ok_to_load_active_config"] is False
    assert len(_floor_blockers(report)) == 1


def test_a_re_stage_clears_the_pre_pr_refusal(tmp_path: Path) -> None:
    """The named remedy actually works — one re-stage, no other operator step."""

    topology = mono_output_topology()
    preview = _preview(topology, fc_hz=5000, tweeter_floor_hz=5000)
    _stage(tmp_path, topology, preview)
    legacy = _strip_floor_facts(tmp_path)
    assert _report(topology, legacy, tmp_path)["ok_to_load_active_config"] is False

    restaged = _stage(tmp_path, topology, preview)
    report = _report(topology, restaged, tmp_path)

    assert all(key in restaged["config"] for key in FLOOR_FACT_KEYS)
    assert report["status"] == "pass"
    assert report["ok_to_load_active_config"] is True
    assert _floor_blockers(report) == []


def test_an_unreadable_staged_config_block_is_refused() -> None:
    """One coherent rule: refuse unless both facts are present in a mapping."""

    unreadable: tuple[dict[str, Any], ...] = (
        {},
        {"config": None},
        {"config": "active_staged.yml"},
        {"config": []},
        {"config": 5},
        {"config": {}},
        {"config": {"tweeter_protection_floor_hz": 5000.0}},
        {"config": {"tweeter_crossover_highpass_hz": 5000.0}},
    )
    for staged in unreadable:
        honoured, issue = _tweeter_protection_floor_verdict(staged)
        assert honoured is False, staged
        assert issue is not None and issue["code"] == (
            "staged_tweeter_protection_floor_unverifiable"
        ), staged

    # Both present, floor honestly null -> the pin-(f) undeclared case passes.
    honoured, issue = _tweeter_protection_floor_verdict({
        "config": {
            "tweeter_protection_floor_hz": None,
            "tweeter_crossover_highpass_hz": 2000.0,
        }
    })
    assert honoured is True
    assert issue is None


def test_missing_crossover_corner_against_a_real_floor_is_refused() -> None:
    """A present-but-null corner under a real floor fails closed and reads well."""

    honoured, issue = _tweeter_protection_floor_verdict({
        "config": {
            "tweeter_protection_floor_hz": 5000.0,
            "tweeter_crossover_highpass_hz": None,
        }
    })

    assert honoured is False
    assert issue is not None
    assert issue["code"] == "tweeter_crossover_below_declared_protection_floor"
    assert "records no tweeter crossover corner" in issue["message"]
    assert "5000 Hz" in issue["message"]


def test_both_surfaces_render_a_non_integer_floor_identically() -> None:
    """The preview disclosure and the gate refusal share one Hz formatter."""

    topology = mono_output_topology()
    preview = _preview(topology, fc_hz=2000, tweeter_floor_hz=5500.5)
    crossover = preview["groups"][0]["crossovers"][0]
    disclosure = next(
        issue
        for issue in crossover["issues"]
        if issue["code"] == "crossover_below_declared_protection_floor"
    )
    _honoured, refusal = _tweeter_protection_floor_verdict({
        "config": {
            "tweeter_protection_floor_hz": 5500.5,
            "tweeter_crossover_highpass_hz": 2000.0,
        }
    })

    assert refusal is not None
    assert "5500.5 Hz" in disclosure["message"]
    assert "5500.5 Hz" in refusal["message"]


# --- (g) never-nanny boundary: nothing grows where protection is not owed ----


def test_a_non_tweeter_declared_floor_grows_no_clamp_and_no_refusal(
    tmp_path: Path,
) -> None:
    """A woofer's declared floor must not gate the woofer/tweeter crossover.

    The live jts.local composite declares its woofer channels
    ``protection_status=not_required`` while the tweeter channels are
    ``software_guard_requested``. The derived protective high-pass and the load
    gate are both tweeter-scoped, so a floor declared on any other role changes
    nothing -- hard stops belong exactly and only to the protected role.
    """

    topology = mono_output_topology()
    woofer_channel = next(
        channel
        for group in topology.speaker_groups
        for channel in group.channels
        if channel.role == "woofer"
    )
    assert woofer_channel.protection_required is False
    assert woofer_channel.protection_status == "not_required"

    preview = _preview(
        topology,
        fc_hz=2000,
        tweeter_floor_hz=None,
        woofer_floor_hz=5000,
    )
    preset = _preset(topology, preview)
    staged = _stage(tmp_path, topology, preview)
    report = _report(topology, staged, tmp_path)

    assert preset.drivers["woofer"].protection_highpass_floor_hz == 5000.0
    assert protective_tweeter_highpass_frequency_hz(preset, "woofer") is None
    assert protective_tweeter_highpass_frequency_hz(preset, "tweeter") == 4000.0
    assert staged["config"]["tweeter_protection_floor_hz"] is None
    assert _floor_blockers(report) == []
    assert report["ok_to_load_active_config"] is True


# --- (d) preview disclosure -------------------------------------------------


def test_preview_discloses_a_below_floor_candidate_without_blocking() -> None:
    topology = mono_output_topology()
    preview = _preview(topology, fc_hz=2000, tweeter_floor_hz=5000)
    crossover = preview["groups"][0]["crossovers"][0]

    disclosures = [
        issue
        for issue in crossover["issues"]
        if issue["code"] == "crossover_below_declared_protection_floor"
    ]
    assert len(disclosures) == 1
    assert disclosures[0]["severity"] == "warning"
    assert "5000 Hz" in disclosures[0]["message"]
    assert "2000 Hz" in disclosures[0]["message"]
    assert "refuse" in disclosures[0]["message"]
    assert crossover["declared_protection_floor_hz"] == 5000.0
    # Preview stays advisory: the hard refusal is the load gate's, so the
    # candidate still compiles and the household still sees its own design.
    assert crossover["proposed_frequency_hz"] == 2000.0
    assert preview["status"] == "ready_for_protected_staging"


def test_preview_is_silent_when_the_candidate_honours_the_floor() -> None:
    topology = mono_output_topology()
    preview = _preview(topology, fc_hz=5000, tweeter_floor_hz=5000)
    crossover = preview["groups"][0]["crossovers"][0]

    assert [
        issue
        for issue in crossover["issues"]
        if issue["code"] == "crossover_below_declared_protection_floor"
    ] == []
    assert crossover["declared_protection_floor_hz"] == 5000.0


# --- (e) the clamp and the gate are independent -----------------------------


def test_gate_verdict_is_independent_of_the_clamp() -> None:
    """Refusing a below-floor candidate must not depend on the clamp working.

    Staged metadata whose protective high-pass is ALREADY at the floor (i.e.
    the clamp did its job) is still refused, because the CROSSOVER itself sits
    below the floor. Deleting the clamp cannot hide the gate, and satisfying
    the clamp cannot silence it.
    """

    honoured, issue = _tweeter_protection_floor_verdict({
        "config": {
            "tweeter_protective_highpass_hz": 5000.0,  # clamp applied
            "tweeter_crossover_highpass_hz": 2000.0,
            "tweeter_protection_floor_hz": 5000.0,
        }
    })

    assert honoured is False
    assert issue is not None
    assert issue["code"] == "tweeter_crossover_below_declared_protection_floor"


def test_clamp_is_independent_of_the_gate() -> None:
    """The clamp is pure preset arithmetic -- no path-safety input reaches it."""

    topology = mono_output_topology()
    preset = _preset(topology, _preview(topology, fc_hz=2000, tweeter_floor_hz=5000))

    assert protective_tweeter_highpass_frequency_hz(preset, "tweeter") == 5000.0


# --- the shared floor reader and comparison rule -----------------------------


def test_declared_floor_reader_takes_the_strictest_highpass_and_nothing_else() -> None:
    assert declared_protection_highpass_floor_hz({
        "required_protection_filters": [
            {"kind": "lowpass", "cutoff_hz": 9000.0},
            {"kind": "highpass", "cutoff_hz": 3000.0},
            {"kind": "highpass", "cutoff_hz": 5000.0},
        ]
    }) == 5000.0
    assert declared_protection_highpass_floor_hz({
        "required_protection_filters": [{"kind": "lowpass", "cutoff_hz": 9000.0}]
    }) is None
    assert declared_protection_highpass_floor_hz({}) is None
    assert declared_protection_highpass_floor_hz(None) is None
    assert declared_protection_highpass_floor_hz({
        "required_protection_filters": [{"kind": "highpass", "cutoff_hz": "nope"}]
    }) is None


def test_floor_comparison_rule_is_greater_or_equal_and_absent_floor_passes() -> None:
    assert protection_highpass_floor_satisfied(highpass_hz=5000, floor_hz=5000) is True
    assert protection_highpass_floor_satisfied(highpass_hz=5001, floor_hz=5000) is True
    assert protection_highpass_floor_satisfied(highpass_hz=4999, floor_hz=5000) is False
    assert protection_highpass_floor_satisfied(highpass_hz=1, floor_hz=None) is True
    assert protection_highpass_floor_satisfied(highpass_hz=None, floor_hz=None) is True
    assert protection_highpass_floor_satisfied(highpass_hz=None, floor_hz=5000) is False


# --- real-box regression: jts.local first composite arm, 2026-08-14 ----------


def test_real_box_below_floor_arm_is_now_clamped_and_refused(tmp_path: Path) -> None:
    draft = json.loads(BELOW_FLOOR_DRAFT.read_text(encoding="utf-8"))
    topology = OutputTopology.from_mapping(draft["topology"])
    preview = build_crossover_preview(draft)
    preset = _preset(topology, preview)
    staged = _stage(tmp_path, topology, preview)
    report = _report(topology, staged, tmp_path)

    # What the box actually declared and actually crossed at.
    assert preset.drivers["tweeter"].protection_highpass_floor_hz == 5000.0
    assert staged["config"]["tweeter_crossover_highpass_hz"] == 2000.0
    # The box emitted freq: 4000.0000 for as_tweeter_protective_hp.
    assert staged["config"]["tweeter_protective_highpass_hz"] == 5000.0
    assert "freq: 4000.0000" not in (tmp_path / "active_staged.yml").read_text(
        encoding="utf-8"
    )
    # The box returned status=pass / blocker_count=0 / ok_to_load=true.
    assert report["status"] == "blocked"
    assert report["ok_to_load_active_config"] is False
    assert len(_floor_blockers(report)) == 1
    # Every side of the stereo pair discloses at confirm time.
    assert len(preview["groups"]) == 2
    for group in preview["groups"]:
        assert any(
            issue["code"] == "crossover_below_declared_protection_floor"
            for issue in group["crossovers"][0]["issues"]
        )


def test_real_box_honest_redraft_at_the_floor_still_arms(tmp_path: Path) -> None:
    draft = json.loads(AT_FLOOR_DRAFT.read_text(encoding="utf-8"))
    topology = OutputTopology.from_mapping(draft["topology"])
    preview = build_crossover_preview(draft)
    staged = _stage(tmp_path, topology, preview)
    report = _report(topology, staged, tmp_path)

    assert staged["config"]["tweeter_crossover_highpass_hz"] == 5000.0
    # Matches the protective high-pass the box actually armed with.
    assert staged["config"]["tweeter_protective_highpass_hz"] == 10000.0
    assert report["status"] == "pass"
    assert report["ok_to_load_active_config"] is True
    assert _floor_blockers(report) == []
    for group in preview["groups"]:
        assert not any(
            issue["code"] == "crossover_below_declared_protection_floor"
            for issue in group["crossovers"][0]["issues"]
        )


# --- (g) the apply-time layer: the L0 emit gate -----------------------------
#
# The fifth surface, and the one that was missing until the crossover frequency
# stopped being frozen at commissioning. Sections (a)-(f) above cover a floor
# that is only ever compared AFTER a graph exists: the clamp raises the derived
# protective high-pass, staging publishes the two facts, and the startup-load
# gate refuses the staged artifact. None of that stands between a chosen
# crossover frequency and an APPLIED graph -- the routine apply transaction
# (build_baseline_profile_candidate -> apply_baseline_profile ->
# dsp_apply.apply_dsp_config) emitted whatever corner it was handed. Harmless
# while nothing varied Fc; a live hazard the moment Fc becomes a searched or
# prescribed parameter.
#
# camilla_yaml._assert_tweeter_crossover_honours_declared_floor closes it at the
# emitter, before a line of YAML is built. These pins exist to keep it honest in
# BOTH directions: it must refuse a below-floor corner, and it must not invent a
# floor where the operator declared none.

ACTIVE_PCM = "hw:CARD=DAC8x,DEV=0"


def _below_floor_preset(topology: OutputTopology):
    """A 2000 Hz crossover under a 5000 Hz declared tweeter floor (the #2491 shape)."""

    return _preset(topology, _preview(topology, fc_hz=2000, tweeter_floor_hz=5000))


def test_baseline_emit_refuses_a_below_floor_crossover_by_name(caplog) -> None:
    topology = mono_output_topology()
    preset = _below_floor_preset(topology)
    caplog.set_level(logging.ERROR, logger="jasper.active_speaker.camilla_yaml")

    with pytest.raises(ActiveSpeakerConfigError) as excinfo:
        emit_active_speaker_baseline_config(preset, playback_device=ACTIVE_PCM)

    # The operator sentence names BOTH numbers and the remedy -- the same three
    # things the startup-load gate's refusal names, because a household that
    # hits one should not get a worse message than one that hits the other.
    message = str(excinfo.value)
    assert "2000 Hz" in message
    assert "5000 Hz" in message
    assert "required_protection_filters" in message

    # ...and the refusal is never silent. The slug is the machine-readable half:
    # the sentence above may be reworded, this may not.
    assert "event=active_speaker.emit_gate" in caplog.text
    assert f"result={EMIT_GATE_TWEETER_CROSSOVER_BELOW_DECLARED_FLOOR}" in caplog.text
    assert "tweeter_crossover_highpass_hz=2000.0" in caplog.text
    assert "tweeter_protection_floor_hz=5000.0" in caplog.text


def test_baseline_emit_accepts_a_crossover_exactly_at_the_declared_floor() -> None:
    """The boundary, matched to the startup gate's rather than chosen afresh.

    ``>=``: a driver rated to cross at 5000 Hz may cross at 5000 Hz (owner
    ruling 2026-08-17). Both layers reach that answer through the SAME shared
    predicate, which is what makes the pin below meaningful -- were the emit
    gate to grow its own comparison, an apply could refuse a graph the startup
    load accepts, or worse, accept one it refuses.
    """

    topology = mono_output_topology()
    preset = _preset(topology, _preview(topology, fc_hz=5000, tweeter_floor_hz=5000))

    text = emit_active_speaker_baseline_config(preset, playback_device=ACTIVE_PCM)

    # Positive control: the emit really happened and really carries the corner
    # under test, so this test cannot pass by emitting nothing at all.
    assert "freq: 5000.0000" in text
    assert "as_tweeter_woofer_tweeter_5000hz_hp" in text
    # The rule both layers share, asserted directly at the boundary value.
    assert protection_highpass_floor_satisfied(highpass_hz=5000, floor_hz=5000) is True


def test_baseline_emit_accepts_a_crossover_above_the_declared_floor() -> None:
    topology = mono_output_topology()
    preset = _preset(topology, _preview(topology, fc_hz=6000, tweeter_floor_hz=5000))

    text = emit_active_speaker_baseline_config(preset, playback_device=ACTIVE_PCM)

    assert "freq: 6000.0000" in text


def test_baseline_emit_invents_no_floor_where_the_operator_declared_none() -> None:
    """The never-nanny direction, and the one place this gate could over-block.

    An undeclared floor is ``None``, and the shared predicate honours it. That
    is NOT a hole: it is the same present-and-``None`` case the startup-load
    gate calls "the honest undeclared case", and substituting the class-default
    code policy here would invent a bound the operator never declared (the
    2026-08-14 never-nanny ruling). A 2000 Hz crossover with no declared floor
    is exactly what it was before this gate existed.
    """

    topology = mono_output_topology()
    preset = _preset(topology, _preview(topology, fc_hz=2000, tweeter_floor_hz=None))

    assert preset.drivers["tweeter"].protection_highpass_floor_hz is None

    text = emit_active_speaker_baseline_config(preset, playback_device=ACTIVE_PCM)

    assert "freq: 2000.0000" in text


@pytest.mark.parametrize("emitter", ["baseline", "driver_domain"])
def test_both_household_emitters_share_the_declared_floor_gate(emitter: str) -> None:
    """Every graph that carries household program inherits the bound.

    Mirrors ``test_both_emitters_share_correction_safety_gate`` in
    tests/test_active_speaker_driver_domain.py: a bonded leader's driver domain
    runs the same protective chain on the same drivers as the solo baseline, so
    a gate one honours and the other does not is a hole with a hardware trigger
    (bond the speaker, and the refused graph applies).
    """

    topology = mono_output_topology()
    preset = _below_floor_preset(topology)

    with pytest.raises(ActiveSpeakerConfigError):
        if emitter == "baseline":
            emit_active_speaker_baseline_config(preset, playback_device=ACTIVE_PCM)
        else:
            emit_active_speaker_driver_domain_config(
                preset,
                playback_device=ACTIVE_PCM,
                program_channel="left",
            )


def test_the_gate_refuses_a_declared_floor_with_no_readable_crossover_corner(
    caplog,
) -> None:
    """Fail-closed: an unreadable corner must not pass as "nothing to compare".

    Unreachable through the emitters today -- ``ActiveSpeakerPreset.validate()``
    requires a region for every adjacent driver pair, so a validated 2-way
    preset always has a tweeter-upper corner, and the emitters validate before
    they call this gate. Pinned anyway because it is the direction that fails
    DANGEROUSLY: a future preset shape that could not name its tweeter corner
    must refuse, not sail through on a ``None``. The rule lives in the shared
    predicate (absent high-pass against a real floor is not satisfied), so this
    also pins that the gate reads its answer from there rather than
    short-circuiting on a missing value.
    """

    topology = mono_output_topology()
    preset = dataclasses.replace(_below_floor_preset(topology), crossover_regions=())
    caplog.set_level(logging.ERROR, logger="jasper.active_speaker.camilla_yaml")

    with pytest.raises(ActiveSpeakerConfigError) as excinfo:
        _assert_tweeter_crossover_honours_declared_floor(preset)

    assert "no crossover corner" in str(excinfo.value)
    assert f"result={EMIT_GATE_TWEETER_CROSSOVER_BELOW_DECLARED_FLOOR}" in caplog.text


def test_the_commissioning_flow_still_stages_a_below_floor_graph_on_purpose(
    tmp_path: Path,
) -> None:
    """The scope boundary, pinned so a later widening has to argue with a test.

    The gate deliberately does NOT run on the startup / commissioning / program
    emitters. Staging a below-floor graph is how the layer above it produces its
    actionable refusal: staging emits, publishes both facts onto the metadata,
    and the load gate refuses BY NAME with a remedy. Gating the emitter would
    replace that with a bare exception thrown from under the commissioning flow
    -- and would leave the load gate untestable against the one artifact it
    exists to catch, which is what sections (c)-(e) above depend on.

    Exercised through ``_stage``, which really runs an ungated emitter
    (``emit_active_speaker_commissioning_config`` — staging's emitter, NOT the
    startup one, despite the artifact's name). The companion test below covers
    the other two by calling them directly, because asserting a preset field
    here would pass just as happily on a build that HAD widened the gate,
    making this documentation rather than a pin.
    """

    topology = mono_output_topology()
    preview = _preview(topology, fc_hz=2000, tweeter_floor_hz=5000)
    preset = _preset(topology, preview)
    assert preset.drivers["tweeter"].protection_highpass_floor_hz == 5000.0

    staged = _stage(tmp_path, topology, preview)

    assert (tmp_path / "active_staged.yml").exists()
    # ...and it published both facts, which is what makes the load gate's
    # refusal possible at all.
    assert staged["config"]["tweeter_crossover_highpass_hz"] == 2000.0
    assert staged["config"]["tweeter_protection_floor_hz"] == 5000.0


@pytest.mark.parametrize("emitter", ["startup", "commissioning", "program"])
def test_the_ungated_emitters_still_emit_a_below_floor_graph(emitter: str) -> None:
    """The other half of the scope boundary — one case per ungated emitter.

    The test above reaches only the emitter STAGING happens to call. Widening
    the gate to either of the other two would leave it green, so each is called
    here directly with the same below-floor preset. All three must emit: the
    startup-load gate owns the refusal for these graphs, and it can only judge
    an artifact that was allowed to exist.
    """

    topology = mono_output_topology()
    preset = _below_floor_preset(topology)

    if emitter == "startup":
        text = emit_active_speaker_startup_config(
            preset, playback_device=ACTIVE_PCM,
        )
    elif emitter == "commissioning":
        text = emit_active_speaker_commissioning_config(
            preset, playback_device=ACTIVE_PCM,
        )
    else:
        text = emit_active_speaker_program_config(
            preset,
            role_channels={"woofer": 0, "tweeter": 1},
            playback_device=ACTIVE_PCM,
        )

    # Emitted, not refused -- and carrying the below-floor corner, so this
    # cannot pass by emitting some other graph.
    assert "freq: 2000.0000" in text
