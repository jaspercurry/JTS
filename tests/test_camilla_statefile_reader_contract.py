# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""One owner for the CamillaDSP statefile's ``config_path`` (issue #2848).

``jasper.active_speaker.environment`` owns three facts together: the
``JASPER_CAMILLA_STATEFILE`` override, the shipped default statefile path, and
the ``config_path:`` parse. Three private readers each held their own copy of
all three — ``jasper.cli.doctor.correction``'s
``_parse_camilla_statefile_config_path`` / ``_active_camilla_config_path``,
``jasper.audio_runtime_plan``'s ``_active_camilla_config_path_from_statefile``,
and ``jasper.multiroom.leader_config``'s ``active_leader_pipe_path`` — so a box
could be told three different things about which graph is loaded.

Two shapes of test, because either alone is insufficient:

- one fixture, every reader, ASSERTED EQUAL: catches a parser that drifts;
- delegation pinning per reader: catches a private parser reintroduced beside
  the canonical one, which the first test would not notice while the copy
  happened to agree.

Out of scope, and named so the floor is legible: ``jasper.fanin.converge``
resolves the statefile PATH to pass as ``--statefile`` and never parses
``config_path`` (it takes that path from this same owner). So are the FOUR shell
parses, which cannot import Python — ``jasper-apply-airplay-mode``'s awk and the
sed in ``jasper-camilla-pipe-guard`` and ``jasper-camilla-crossover-guard`` under
``deploy/bin``, plus a fourth in ``deploy/lib/jasper-camilla-guard-common.sh``
that both guards source. That last one is invisible to a grep for the env-var
name, because it takes the statefile as ``$STATEFILE``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jasper import audio_runtime_plan as audio_plan
from jasper.active_speaker import environment as env_mod
from jasper.cli.doctor import correction as doctor_correction
from jasper.multiroom import leader_config


def _pipe_wired_config(tmp_path: Path) -> Path:
    """A REAL emitted leader config whose playback sink is the snapserver pipe."""

    from jasper.multiroom.reconcile import SNAPFIFO
    from jasper.sound.camilla_yaml import emit_sound_config
    from jasper.sound.profile import SoundProfile

    config = tmp_path / "grouping_leader.yml"
    config.write_text(
        emit_sound_config(
            SoundProfile(enabled=False),
            enable_rate_adjust=False,
            playback_pipe_path=SNAPFIFO,
        )
    )
    return config


def test_every_folded_reader_resolves_one_statefile_fixture_identically(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """One statefile on disk; four readers; one answer.

    The doctor helper (``_active_camilla_config_path``, which ``doctor.audio``,
    ``doctor.audio_runtime_camilla``, and ``doctor.grouping`` import), the runtime plan,
    the bonded-leader pipe probe, and the canonical reader all read the SAME
    ``JASPER_CAMILLA_STATEFILE`` fixture here. Break the shared parse or the
    shared override lookup and the one assertion below reports all four at once
    — which is the point: before the fold these four could disagree.
    """

    from jasper.multiroom.reconcile import SNAPFIFO

    config = _pipe_wired_config(tmp_path)
    statefile = tmp_path / "outputd-statefile.yml"
    statefile.write_text(f"config_path: {config}\n")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))

    # The runtime plan does not carry `correction_config_path`, but
    # `camilla_config_hash` is derived from it, so the fixture's own content hash
    # is proof the plan resolved this statefile and not another.
    plan = audio_plan.build_audio_runtime_plan_from_system(
        base_env_path=str(tmp_path / "base.env"),
        outputd_env_path=str(tmp_path / "outputd.env"),
        fanin_env_path=str(tmp_path / "fanin.env"),
        grouping_env_path=str(tmp_path / "grouping.env"),
        overrides_path=str(tmp_path / "overrides.json"),
        output_hardware_state_path=str(tmp_path / "output_hardware.json"),
    )
    config_hash = audio_plan.camilla_config_hash_for_path(str(config))
    assert config_hash not in ("", "missing", "unreadable")

    # One assertion over all four, so a broken parse names every reader it broke
    # instead of stopping at whichever happens to be checked first.
    assert {
        "environment (canonical)": env_mod.read_camilla_statefile_config_path(),
        "doctor": doctor_correction._active_camilla_config_path(),
        "runtime plan": plan.camilla_config_hash,
        "leader pipe": leader_config.active_leader_pipe_path(),
    } == {
        "environment (canonical)": str(config),
        "doctor": (statefile, str(config)),
        "runtime plan": config_hash,
        "leader pipe": SNAPFIFO,
    }


def test_doctor_delegates_to_the_canonical_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reintroduced private parser in the doctor would not observe this fake.

    ``jasper.cli.doctor.correction`` imports both helpers at MODULE level, so
    the patch target is the name in that module's own namespace — patching
    ``environment`` would not reach the already-bound names.
    """

    calls: list[str | Path | None] = []

    def fake_reader(path: str | Path | None = None) -> str | None:
        calls.append(path)
        return "/from/canonical/reader.yml"

    monkeypatch.setattr(
        doctor_correction, "camilla_statefile_path", lambda path=None: Path("/fake/sf.yml")
    )
    monkeypatch.setattr(
        doctor_correction, "read_camilla_statefile_config_path", fake_reader
    )

    assert doctor_correction._active_camilla_config_path() == (
        Path("/fake/sf.yml"),
        "/from/canonical/reader.yml",
    )
    # The statefile the doctor names in its detail is the one it reads, not a
    # second resolution of the same override.
    assert calls == [Path("/fake/sf.yml")]


def test_runtime_plan_delegates_to_the_canonical_reader(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The plan imports the reader lazily, so the patch target is ``environment``.

    Pointing the fake at a file that exists but is NOT named by any statefile on
    this machine proves the plan took the reader's answer: only the reader can
    have produced this path.
    """

    config = tmp_path / "from-the-reader.yml"
    config.write_text("# only the canonical reader can name this file\n")
    monkeypatch.setattr(
        env_mod, "read_camilla_statefile_config_path", lambda path=None: str(config)
    )

    plan = audio_plan.build_audio_runtime_plan_from_system(
        base_env_path=str(tmp_path / "base.env"),
        outputd_env_path=str(tmp_path / "outputd.env"),
        fanin_env_path=str(tmp_path / "fanin.env"),
        grouping_env_path=str(tmp_path / "grouping.env"),
        overrides_path=str(tmp_path / "overrides.json"),
        output_hardware_state_path=str(tmp_path / "output_hardware.json"),
    )

    assert plan.camilla_config_hash == audio_plan.camilla_config_hash_for_path(
        str(config)
    )


def test_converge_passes_the_owner_resolved_statefile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """``--statefile`` and the check it guards must name ONE statefile.

    ``converge._reemit_graph_at_ring`` never parses ``config_path``, but it does
    have to name the statefile ``applied_profile_displacement`` reads — that
    identity is the entire reason the flag is passed instead of left to
    argparse's default. Both now resolve through ``camilla_statefile_path``, so
    an operator override moves them together.

    The empty override is here because it is the one input where the retired
    hand-rolled lookup and the owner disagreed: it sent ``""`` while the
    displacement check read ``"."``, so a re-emit could re-point one statefile
    while the divergence check had inspected another.
    """

    from jasper.fanin import converge

    seen: list[list[str]] = []

    def fake_main(argv: list[str]) -> int:
        seen.append(list(argv))
        return 0

    monkeypatch.setattr("jasper.cli.active_speaker.main", fake_main)

    override = str(tmp_path / "override.yml")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", override)
    converge._reemit_graph_at_ring()
    assert seen == [
        ["baseline-reemit", "--endpoint", "ring", "--statefile", override]
    ]

    seen.clear()
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", "")
    converge._reemit_graph_at_ring()
    assert seen == [
        ["baseline-reemit", "--endpoint", "ring", "--statefile", "."]
    ]
    assert str(env_mod.camilla_statefile_path()) == "."


def test_leader_pipe_path_delegates_to_the_canonical_reader(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Same lazy-import patch target as the plan, and the ``None`` contract.

    ``active_leader_pipe_path`` is total: the reader's ``None`` (unreadable or
    ``config_path``-less statefile) must stay the degraded ``""`` the /state
    grouping section and the doctor's ``leader pipe`` check expect, never an
    exception.
    """

    from jasper.multiroom.reconcile import SNAPFIFO

    config = _pipe_wired_config(tmp_path)
    monkeypatch.setattr(
        env_mod, "read_camilla_statefile_config_path", lambda path=None: str(config)
    )
    assert leader_config.active_leader_pipe_path() == SNAPFIFO

    monkeypatch.setattr(
        env_mod, "read_camilla_statefile_config_path", lambda path=None: None
    )
    assert leader_config.active_leader_pipe_path() == ""
