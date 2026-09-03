# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pin the single-owner contract for commission-tone orchestration (L4-1).

PR #969 forked the commission-tone helpers between ``jasper/web/sound_setup.py``
and the new ``jasper/active_speaker/web_commissioning.py``; the two copies had
already begun to drift. The migration is finished by making
``web_commissioning`` the single owner and having ``/sound/`` import the helpers
rather than keep a hand-copied fork (the sibling ``/correction/`` surface already
routes through ``web_commissioning`` via ``correction_crossover_backend``).

These guards make a future re-fork fail CI: the shared helpers must be the *same
function objects* on both surfaces (i.e. imported, not re-defined), the tone's
timing/device constants must be bound by import rather than re-declared, and the
driver-test signal plan must come out identical from either surface.
"""

from __future__ import annotations

import ast

import pytest

import jasper.active_speaker.web_commissioning as web_commissioning
import jasper.mux as mux
import jasper.web.correction_crossover_backend as correction_backend
import jasper.web.sound_setup as sound_setup

OWNER_MODULE = "jasper.active_speaker.web_commissioning"

# The commission-tone helpers active-speaker now owns and both operator surfaces
# share. ``_stop_commission_tone_locked`` is intentionally NOT here: it is bound
# to each module's own ``_COMMISSION_TONE_SESSION`` global that the per-surface
# play orchestration owns, so it stays surface-local by design.
SHARED_COMMISSION_TONE_HELPERS = (
    "_combined_speech_stimulus_wav_path",
    "_commission_summed_stimulus_issue",
    "_commission_tone_issue",
    "_commission_tone_mux_command",
    "_commission_tone_payload",
    "_commission_tone_release_fanin_lane",
    "_commission_tone_select_fanin_lane",
    "_commission_tone_signal_plan",
    "_commission_tone_target_key",
    "_commission_tone_wav_path",
    "_summed_playback_with_issue",
)

# The commission-tone constants active-speaker owns. /sound/ re-declared all
# five (2026-07, issue #1950) and the function-shaped guards above never saw it:
# `def <name>(` does not match a `NAME = value` assignment.
#
# ``_SUMMED_TEST_ARM_REPORT`` is intentionally NOT here. It is a sixth
# byte-identical fork between the two modules, but it is a MUTABLE dict, so
# giving it one owner means two surfaces sharing one object — a coupling
# decision, not a de-duplication. Safe to defer today because its only
# consumer (``active_speaker.safe_playback.arm_safe_playback_session``) reads
# it and projects it into a flat scalar dict, never retaining the nested
# containers. To close it later: add the name below and the guard covers it.
#
# Identity (`is`) is NOT a sufficient guard for these. CPython interns
# identifier-like string literals, so two modules independently declaring
# SUMMED_COMMISSION_SPEECH_BACKEND compare `is`-identical and a re-fork of a
# string constant passes an identity assertion silently. The binding-shape
# guard below is the load-bearing one; identity is kept only as a cheap
# same-value cross-check.
# COMMISSION_TONE_ALSA_DEVICE left this family at P6c-ii: the lane's device
# stopped being a constant at all — both surfaces resolve it per call
# through the one transport reader
# (jasper.audio_measurement.correction_lane.correction_play_device), and a
# re-fork of the DEVICE now means re-declaring the lane literal, which
# tests/test_correction_substream_ssot.py's guard catches instead.
SHARED_COMMISSION_TONE_CONSTANTS = (
    "COMMISSION_TONE_DURATION_S",
    "COMMISSION_TONE_RESTART_MARGIN_S",
    "COMMISSION_TONE_STARTUP_CHECK_S",
    "SUMMED_COMMISSION_SPEECH_BACKEND",
)


def _forked_constant_bindings(source: str, names: tuple[str, ...]) -> list[str]:
    """Names in `names` that `source` binds by module-level assignment.

    Parsed rather than pattern-matched, mirroring tests/test_lint_contracts.py.
    A text scan is a trap here: the deleted block's replacement comment and this
    module's own prose both name the constants, and a `NAME = ` line-prefix grep
    reads them as violations. The AST sees only code.

    Scope is deliberately module level (`tree.body`, not `ast.walk`): a
    function-local variable that happens to share a name is shadowing, not a
    forked module constant, and flagging it would be a false positive.

    Known gap, accepted: iterating `tree.body` also skips assignments nested
    in module-level compound statements — `try: from owner import X / except
    ImportError: X = <literal>` is a plausible re-fork this will not catch,
    and unlike shadowing it is not a false positive. Unreachable today (the
    only module-level compound statement in either file is `if __name__ ==
    "__main__":`), and the runtime identity check below covers it from the
    other side. Closing it properly means pruning FunctionDef/AsyncFunctionDef/
    ClassDef and recursing the rest.
    """

    wanted = set(names)
    forked: set[str] = set()
    for node in ast.parse(source).body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        elif isinstance(node, ast.AugAssign):
            targets = [node.target]
        for target in targets:
            # `A = B = v` and `A, B = v` bind more than one name.
            leaves = (
                target.elts if isinstance(target, (ast.Tuple, ast.List)) else [target]
            )
            forked.update(
                leaf.id
                for leaf in leaves
                if isinstance(leaf, ast.Name) and leaf.id in wanted
            )
    return sorted(forked)


def _names_imported_from(source: str, module: str) -> set[str]:
    """Names `source` imports from `module` via `from <module> import ...`."""

    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module == module:
            imported.update(alias.asname or alias.name for alias in node.names)
    return imported


def test_sound_setup_shares_web_commissioning_tone_helpers():
    """/sound/ must import the helpers from the owner, not re-define them."""

    for name in SHARED_COMMISSION_TONE_HELPERS:
        owner_fn = getattr(web_commissioning, name)
        sound_fn = getattr(sound_setup, name)
        assert sound_fn is owner_fn, (
            f"{name} on /sound/ is not the web_commissioning owner object — "
            "the commission-tone helpers have re-forked (see L4-1)."
        )


def test_no_forked_commission_tone_helper_defs_in_sound_setup():
    """sound_setup must not carry its own copy of any shared helper (textual)."""

    src = sound_setup.__loader__.get_source(sound_setup.__name__)
    assert src is not None
    for name in SHARED_COMMISSION_TONE_HELPERS:
        assert f"def {name}(" not in src, (
            f"sound_setup re-defines {name}; it must import it from "
            "jasper.active_speaker.web_commissioning instead (L4-1)."
        )


def test_no_forked_commission_tone_constants_in_sound_setup():
    """sound_setup must import the tone constants, not re-declare them (#1950)."""

    src = sound_setup.__loader__.get_source(sound_setup.__name__)
    assert src is not None
    forked = _forked_constant_bindings(src, SHARED_COMMISSION_TONE_CONSTANTS)
    assert forked == [], (
        f"sound_setup re-declares {', '.join(forked)}; import them from "
        f"{OWNER_MODULE} instead (#1950). A forked "
        "COMMISSION_TONE_DURATION_S can drift above mux.FANIN_TEST_LEASE_SEC "
        "and readmit household music into a live sweep."
    )


def test_sound_setup_imports_commission_tone_constants_from_owner():
    """The positive half: each constant is bound by an import from the owner."""

    src = sound_setup.__loader__.get_source(sound_setup.__name__)
    assert src is not None
    imported = _names_imported_from(src, OWNER_MODULE)
    missing = [n for n in SHARED_COMMISSION_TONE_CONSTANTS if n not in imported]
    assert missing == [], (
        f"sound_setup does not import {', '.join(missing)} from {OWNER_MODULE}; "
        "the commission-tone constants have a single owner (#1950)."
    )
    for name in SHARED_COMMISSION_TONE_CONSTANTS:
        assert getattr(sound_setup, name) == getattr(web_commissioning, name)


_FORKED_FIXTURE = '''\
"""Prose that names COMMISSION_TONE_DURATION_S must not trip the guard."""
# nor may this comment mentioning SUMMED_COMMISSION_SPEECH_BACKEND = "x"
COMMISSION_TONE_DURATION_S = 35.0
COMMISSION_TONE_RESTART_MARGIN_S = COMMISSION_TONE_STARTUP_CHECK_S = 3.0


def _local_shadow():
    SUMMED_COMMISSION_SPEECH_BACKEND = "local-is-not-a-module-fork"
    return SUMMED_COMMISSION_SPEECH_BACKEND
'''

_IMPORTED_FIXTURE = '''\
"""COMMISSION_TONE_DURATION_S is imported, not declared."""
from jasper.active_speaker.web_commissioning import (
    COMMISSION_TONE_DURATION_S,
    COMMISSION_TONE_RESTART_MARGIN_S,
)

SOMETHING_ELSE = 1.0
'''


def test_constant_guard_detects_a_re_fork():
    """The guard must fail on what it exists to catch.

    The sibling `def`-shaped guard passed against the very tree that carried
    the fork for months (#1950); tests/test_lint_contracts.py records the same
    mistake shipping once before. Pin the catching direction so a future
    simplification cannot go quietly vacuous.
    """

    assert _forked_constant_bindings(
        _FORKED_FIXTURE, SHARED_COMMISSION_TONE_CONSTANTS
    ) == [
        "COMMISSION_TONE_DURATION_S",
        "COMMISSION_TONE_RESTART_MARGIN_S",
        "COMMISSION_TONE_STARTUP_CHECK_S",
    ]


def test_constant_guard_accepts_imports_and_ignores_prose():
    """And it must not cry wolf — which is why it parses instead of grepping."""

    assert (
        _forked_constant_bindings(_IMPORTED_FIXTURE, SHARED_COMMISSION_TONE_CONSTANTS)
        == []
    )
    assert _names_imported_from(_IMPORTED_FIXTURE, OWNER_MODULE) == {
        "COMMISSION_TONE_DURATION_S",
        "COMMISSION_TONE_RESTART_MARGIN_S",
    }


def test_correction_routes_through_web_commissioning_owner():
    """/correction/ keeps consuming the same owner module (no parallel fork)."""

    assert correction_backend.web_commissioning is web_commissioning


def test_commissioning_uses_its_owner_scoped_mux_gate(monkeypatch):
    """Commissioning cannot steal or release correction's diagnostic gate."""

    commands: list[str] = []

    def command(value: str):
        commands.append(value)
        return {"active_source": "correction", "test_source": "correction"}

    monkeypatch.setattr(web_commissioning, "_commission_tone_mux_command", command)

    web_commissioning._commission_tone_select_fanin_lane()
    web_commissioning._commission_tone_release_fanin_lane(reason="test")

    assert commands == [
        "TEST_SELECT correction active-speaker-commissioning",
        "TEST_RELEASE active-speaker-commissioning",
    ]


def test_commissioning_tone_fits_inside_mux_gate_lease():
    """A tone outliving mux's lease lets the gate expire mid-sweep.

    Pinned on the owner *and* on /sound/, because /sound/ is the surface that
    actually plays it. Post-#1950 the two are the same object and the second
    assertion is a tautology — which is the point: it goes red the moment
    /sound/ re-declares its own copy, catching drift the owner-only pin missed.
    """

    assert web_commissioning.COMMISSION_TONE_DURATION_S < mux.FANIN_TEST_LEASE_SEC
    assert sound_setup.COMMISSION_TONE_DURATION_S < mux.FANIN_TEST_LEASE_SEC
    assert (
        sound_setup.COMMISSION_TONE_DURATION_S
        is web_commissioning.COMMISSION_TONE_DURATION_S
    )


def test_indeterminate_commission_select_runs_owner_scoped_cleanup(monkeypatch):
    commands: list[str] = []

    def command(value: str):
        commands.append(value)
        if value.startswith("TEST_SELECT"):
            raise RuntimeError("response lost")
        return {"active_source": "idle", "test_source": None}

    monkeypatch.setattr(web_commissioning, "_commission_tone_mux_command", command)

    with pytest.raises(RuntimeError, match="response lost"):
        web_commissioning._commission_tone_select_fanin_lane()

    assert commands == [
        "TEST_SELECT correction active-speaker-commissioning",
        "TEST_RELEASE active-speaker-commissioning",
    ]


def test_driver_signal_plan_identical_across_surfaces(monkeypatch):
    """The shared driver-test signal plan is byte-identical from either surface.

    Both surfaces resolve the plan through the same owner object, so they cannot
    diverge; this exercises it end-to-end with a stub preset to prove the shared
    helper yields one plan regardless of which surface calls it.
    """

    class _Channel:
        role = "woofer"
        driver_style = "sealed"

    class _Group:
        id = "mono"
        channels = (_Channel(),)

    class _Topology:
        speaker_groups = (_Group(),)

    class _Preset:
        preset_id = "preset-test"
        name = "Test preset"

    sentinel_plan = {
        "artifact_schema_version": 1,
        "kind": "jts_active_speaker_driver_test_signal_plan",
        "status": "ready",
        "role": "woofer",
        "frequency_hz": 120.0,
        "allowed_band": {"highpass_hz": 80.0, "lowpass_hz": 400.0},
    }

    def _fake_driver_test_signal_plan(preset, role, **_kwargs):
        return dict(sentinel_plan)

    # Patch the lazy import target the shared helper reaches for.
    monkeypatch.setattr(
        "jasper.active_speaker.test_signal_plan.driver_test_signal_plan",
        _fake_driver_test_signal_plan,
    )

    kwargs = dict(
        role="woofer",
        group_id="mono",
        topology=_Topology(),
        preset=_Preset(),
    )
    plan_from_sound = sound_setup._commission_tone_signal_plan(**kwargs)
    plan_from_web = web_commissioning._commission_tone_signal_plan(**kwargs)

    assert plan_from_sound == plan_from_web
    assert plan_from_sound["status"] == "ready"
    assert plan_from_sound["preset_source"] == "explicit_preset"
    assert plan_from_sound["preset_id"] == "preset-test"
