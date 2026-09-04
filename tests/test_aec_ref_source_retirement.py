"""The AEC bridge's ALSA reference fallback is retired (U4 / P7-1).

`JASPER_AEC_REF_SOURCE=alsa` used to point `jasper-aec-bridge` at the
summed snd-aloop tap `pcm.jasper_ref`. outputd's final speaker monitor is
now the bridge's only reference source, and the aloop tap itself is deleted
later in the same arc (P9). This file pins the three halves of that
retirement so none of them can quietly come back:

  1. the bridge has no ALSA reference reader left,
  2. a live box still carrying the retired value converges instead of
     going deaf, while a genuinely unknown value still fails loudly, and
  3. `jasper-aec-reconcile` — the single writer of that env var — never
     publishes the retired spelling from any branch.
"""
from __future__ import annotations

import ast
import logging
import re
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from jasper.cli import (
    aec_bridge,
    aec_bridge_capture,
    aec_bridge_config,
    aec_bridge_reference,
)
from jasper.cli.aec_bridge_reference import REF_CHANNELS, REF_RATE
from tests._sounddevice_stub import stub_sounddevice

REPO = Path(__file__).resolve().parents[1]
BRIDGE_SOURCE = REPO / "jasper" / "cli" / "aec_bridge.py"
# The reference reader the retirement removed spanned the bridge entry point
# and the modules cut out of it that open capture devices or query them, so
# all four are in scope for the guards.
BRIDGE_SOURCES = (
    BRIDGE_SOURCE,
    REPO / "jasper" / "cli" / "aec_bridge_capture.py",
    REPO / "jasper" / "cli" / "aec_bridge_config.py",
    REPO / "jasper" / "cli" / "aec_bridge_reference.py",
)
BRIDGE_MODULES = (
    aec_bridge, aec_bridge_capture, aec_bridge_config, aec_bridge_reference,
)
RECONCILE = REPO / "deploy" / "bin" / "jasper-aec-reconcile"

RETIRED = "alsa"


def _config(ref_source: str) -> aec_bridge.BridgeConfig:
    """A default bridge config with only `ref_source` varied."""
    return replace(aec_bridge.BridgeConfig.from_env(), ref_source=ref_source)


# ---------------------------------------------------------------------------
# 1. The bridge has no ALSA reference reader left.
# ---------------------------------------------------------------------------


def _docstring_node_ids(tree: ast.AST) -> set[int]:
    """Ids of every node that is a docstring rather than a value."""
    ids = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", None)
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            ids.add(id(body[0].value))
    return ids


def test_the_bridge_module_has_no_alsa_reference_reader():
    """No constant in the bridge names the retired `pcm.jasper_ref` tap.

    AST-walked over *values* only, with docstrings excluded: prose about a
    retirement must not satisfy — nor trip — the guard against it. A text
    scan would do both, and the repo has been fooled that way before (see
    the fd-leak row in docs/testing-tooling.md). Only a string the code
    actually uses can be a device name.
    """
    offenders = []
    for source in BRIDGE_SOURCES:
        tree = ast.parse(source.read_text())
        docstrings = _docstring_node_ids(tree)
        offenders += sorted(
            f"{source.name}:{node.lineno}: {node.value!r}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and "jasper_ref" in node.value
            and id(node) not in docstrings
        )
    assert not offenders, (
        "the AEC bridge must not name the retired pcm.jasper_ref tap:\n"
        + "\n".join(offenders)
    )
    for module in BRIDGE_MODULES:
        assert not hasattr(module, "REF_DEVICE"), (
            f"{module.__name__}.REF_DEVICE named the retired ALSA reference PCM"
        )
        assert not hasattr(module, "_ref_thread"), (
            f"{module.__name__}._ref_thread was the retired ALSA capture loop"
        )


def test_the_bridge_never_imports_alsaaudio():
    """`alsaaudio` existed in this module only for the retired ref reader.

    AST-walked, not grepped, so the docstring above (which names the module)
    cannot satisfy the guard.
    """
    trees = [ast.parse(source.read_text()) for source in BRIDGE_SOURCES]
    imported = {
        alias.name.split(".")[0]
        for tree in trees
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for tree in trees
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "alsaaudio" not in imported, (
        "the bridge reads its reference over UDP; an alsaaudio import means "
        "an ALSA reference reader came back"
    )


def test_the_reference_geometry_matches_outputd_the_producer():
    """48 kHz stereo is jasper-outputd's fact, not the bridge's free parameter.

    Cross-language pin against the producer's own source. Asserting the
    constants against each other would be self-referential — moving one would
    move both sides and stay green — so they are checked against
    `rust/jasper-outputd/src/types.rs`, where the value is fixed and
    `Config::from_env` refuses any other rate.
    """
    types_rs = (REPO / "rust" / "jasper-outputd" / "src" / "types.rs").read_text()

    assert f"pub const SAMPLE_RATE: u32 = {REF_RATE:_};" in types_rs, (
        "REF_RATE must equal jasper-outputd's core sample rate; "
        "the reference datagrams are that daemon's playout periods"
    )
    assert f"pub const CHANNELS: u16 = {REF_CHANNELS};" in types_rs, (
        "REF_CHANNELS must equal jasper-outputd's channel count; "
        "the reference is stereo whatever the sink's width"
    )


# ---------------------------------------------------------------------------
# 2. A retired value converges; an unknown one still fails loudly.
# ---------------------------------------------------------------------------


def test_the_supported_source_is_returned_untouched():
    config = _config(aec_bridge.REF_SOURCE)
    assert aec_bridge.resolved_reference_source(config) is config


def test_the_retired_source_warns_and_falls_back_to_outputd_udp(caplog):
    """A parked box's stale env must not cost the household wake detection.

    The pre-P7-1 reconciler wrote the retired value whenever it parked the
    bridge, so it is still on disk out there. Refusing to start would leave
    jasper-voice bound to a UDP mic nobody feeds — a silent failure — so the
    bridge converges and says so.
    """
    with caplog.at_level(logging.WARNING, logger="jasper.aec_bridge"):
        resolved = aec_bridge.resolved_reference_source(_config(RETIRED))

    assert resolved.ref_source == aec_bridge.REF_SOURCE
    assert "event=aec_ref_source_retired" in caplog.text
    assert "jasper-aec-reconcile" in caplog.text, (
        "the warning must name the command that converges the env file"
    )


@pytest.mark.parametrize("value", ["", "jasper_ref", "chip_ref_tee", "typo"])
def test_an_unknown_source_is_still_a_hard_failure(value):
    """Only the retired spelling is converged. Everything else still refuses.

    Guessing a transport for a name nobody recognises would put the AEC
    engine on a reference the operator did not ask for.
    """
    with pytest.raises(aec_bridge.UnsupportedReferenceSource) as excinfo:
        aec_bridge.resolved_reference_source(_config(value))
    assert repr(value) in str(excinfo.value)


def test_main_resolves_the_reference_source_before_publishing_provenance():
    """Ordering is the point, not just that the call exists.

    `_bridge_stats.reset` stores the value the snapshot publishes as
    `reference_input.source` — the field the doctor's exact-v4 assessment
    compares against the configured source. (`set_active_capture_plan`
    later republishes the same `config.ref_source` as
    `mic_reference_identity.ref_source`, the legacy provenance the doctor
    falls back to.) `reset` is the earlier of the two, so pinning it ahead
    of resolution keeps the retired spelling out of both.
    """
    tree = ast.parse(BRIDGE_SOURCE.read_text())
    main = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    resolve_line = min(
        node.lineno for node in ast.walk(main)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "resolved_reference_source"
    )
    reset_line = min(
        node.lineno for node in ast.walk(main)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "reset"
    )
    assert resolve_line < reset_line, (
        "main() must resolve ref_source before _bridge_stats.reset publishes it"
    )


def test_main_wires_the_ref_thread_to_the_process_stats_and_shutdown():
    """The one surviving transport reads its endpoint from the resolved
    config and shares the process's stats and shutdown Event. A throwaway
    pair leaves the reference running, unreportable and unstoppable — no
    unit test opens the real socket, so nothing else would notice.
    """
    tree = ast.parse(BRIDGE_SOURCE.read_text())
    thread = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and any(
            kw.arg == "target"
            and ast.unparse(kw.value) == "outputd_ref_udp_thread"
            for kw in node.keywords
        )
    )
    kwargs = next(kw.value for kw in thread.keywords if kw.arg == "kwargs")
    assert {
        key.value: ast.unparse(value)
        for key, value in zip(kwargs.keys, kwargs.values)
    } == {
        "host": "config.outputd_ref_udp_host",
        "port": "config.outputd_ref_udp_port",
        "stats": "_bridge_stats",
        "shutdown": "_shutdown",
    }


# ---------------------------------------------------------------------------
# 3. The single writer never publishes the retired spelling.
# ---------------------------------------------------------------------------


def test_the_reconciler_never_assigns_the_retired_ref_source():
    """`write_leg_env` is the only writer of JASPER_AEC_REF_SOURCE.

    Its parked branches (`bridge_running` of `"0"` and `"reference"`) used to
    write `alsa`; they publish a resting value, and the one supported source
    is what it has to be.
    """
    text = RECONCILE.read_text()
    assignments = re.findall(r'^\s*ref_source="([^"]*)"', text, flags=re.MULTILINE)
    assert assignments, "expected write_leg_env to assign ref_source"
    assert set(assignments) == {"outputd_udp"}, (
        f"jasper-aec-reconcile assigns ref_source={sorted(set(assignments))}; "
        "outputd_udp is the bridge's only reference source"
    )


# ---------------------------------------------------------------------------
# 4. The surviving chip-AEC precondition.
#
# Retiring the ALSA source made `main()`'s chip-AEC block a single guard:
# the `ref_source != outputd_udp` half became unreachable and was removed,
# leaving `JASPER_OUTPUTD_CHIP_REF_PCM` as the ONE thing standing between
# `JASPER_AEC_CHIP_AEC_ENABLED=1` and a bridge that forwards the chip beam
# while nothing feeds the chip's USB-IN reference — i.e. chip AEC running
# open-loop against the speaker. Nothing pinned it before.
# ---------------------------------------------------------------------------


def _arm_chip_aec(monkeypatch, tmp_path, *, chip_ref_pcm: str) -> None:
    """Env + seams that get `main()` as far as the chip-AEC guard."""
    monkeypatch.setenv("JASPER_AEC_CHIP_AEC_ENABLED", "1")
    monkeypatch.setenv("JASPER_OUTPUTD_CHIP_REF_PCM", chip_ref_pcm)
    # Never touch /run from a test.
    monkeypatch.setenv(
        "JASPER_AEC_BRIDGE_STATS_PATH", str(tmp_path / "aec_bridge_stats.json")
    )
    # A validated beam plan is checked BEFORE this guard and exits 1 with its
    # own message; stub it so a missing plan cannot masquerade as this guard
    # firing.
    monkeypatch.setattr(
        aec_bridge,
        "_chip_beam_plan",
        lambda: aec_bridge._mic_profile.SQUARE_FIXED_150_210_PLAN,
    )


def test_chip_aec_refuses_to_start_without_a_chip_reference_producer(
    monkeypatch, tmp_path, caplog
):
    """`JASPER_AEC_CHIP_AEC_ENABLED=1` requires JASPER_OUTPUTD_CHIP_REF_PCM.

    Without it, outputd never feeds the XVF's USB-IN reference, so the chip
    cancels against nothing while the bridge forwards its beam as the live
    mic — echo straight back into the session with no software AEC3 behind
    it (chip-AEC mode bypasses the engine). Fail closed instead.
    """
    _arm_chip_aec(monkeypatch, tmp_path, chip_ref_pcm="")
    sd_mod = MagicMock()
    stub_sounddevice(monkeypatch, sd_mod)

    with caplog.at_level(logging.ERROR, logger="jasper.aec_bridge"):
        assert aec_bridge.main() == 1

    assert "JASPER_OUTPUTD_CHIP_REF_PCM" in caplog.text
    # Failed at THIS guard, not incidentally at a later one: the guard sits
    # ahead of mic validation, so the mic was never even queried.
    sd_mod.query_devices.assert_not_called()


def test_the_chip_reference_guard_lets_a_configured_producer_through(
    monkeypatch, tmp_path
):
    """Positive control for the test above.

    Without this, deleting the guard's `return 1` would still leave the
    negative test green for the wrong reason — `main()` exits 1 on the
    missing mic a few lines later either way. Here the guard is satisfied,
    so reaching mic validation proves it passed rather than short-circuited.
    """
    _arm_chip_aec(monkeypatch, tmp_path, chip_ref_pcm="hw:Array,0")
    sd_mod = MagicMock()
    sd_mod.query_devices.side_effect = ValueError("no such device")
    stub_sounddevice(monkeypatch, sd_mod)

    assert aec_bridge.main() == 1
    sd_mod.query_devices.assert_called_once()
