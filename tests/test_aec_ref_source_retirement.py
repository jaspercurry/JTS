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

import pytest

from jasper.cli import aec_bridge

REPO = Path(__file__).resolve().parents[1]
BRIDGE_SOURCE = REPO / "jasper" / "cli" / "aec_bridge.py"
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
    tree = ast.parse(BRIDGE_SOURCE.read_text())
    docstrings = _docstring_node_ids(tree)
    offenders = sorted(
        f"{BRIDGE_SOURCE.name}:{node.lineno}: {node.value!r}"
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
    assert not hasattr(aec_bridge, "REF_DEVICE"), (
        "REF_DEVICE named the retired ALSA reference PCM"
    )
    assert not hasattr(aec_bridge, "_ref_thread"), (
        "_ref_thread was the retired ALSA reference capture loop"
    )


def test_the_bridge_never_imports_alsaaudio():
    """`alsaaudio` existed in this module only for the retired ref reader.

    AST-walked, not grepped, so the docstring above (which names the module)
    cannot satisfy the guard.
    """
    tree = ast.parse(BRIDGE_SOURCE.read_text())
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "alsaaudio" not in imported, (
        "the bridge reads its reference over UDP; an alsaaudio import means "
        "an ALSA reference reader came back"
    )


# ---------------------------------------------------------------------------
# 2. A retired value converges; an unknown one still fails loudly.
# ---------------------------------------------------------------------------


def test_the_supported_source_is_returned_untouched():
    config = _config(aec_bridge.REF_SOURCE)
    assert aec_bridge._resolved_reference_source(config) is config


def test_the_retired_source_warns_and_falls_back_to_outputd_udp(caplog):
    """A parked box's stale env must not cost the household wake detection.

    The pre-P7-1 reconciler wrote the retired value whenever it parked the
    bridge, so it is still on disk out there. Refusing to start would leave
    jasper-voice bound to a UDP mic nobody feeds — a silent failure — so the
    bridge converges and says so.
    """
    with caplog.at_level(logging.WARNING, logger="jasper.aec_bridge"):
        resolved = aec_bridge._resolved_reference_source(_config(RETIRED))

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
        aec_bridge._resolved_reference_source(_config(value))
    assert repr(value) in str(excinfo.value)


def test_main_resolves_the_reference_source_before_publishing_provenance():
    """Ordering is the point, not just that the call exists.

    `jasper-doctor` trusts `mic_reference_identity.ref_source` out of the
    bridge stats snapshot to name the failed hop, so the retired spelling
    must never reach `_bridge_stats.reset`.
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
        and node.func.id == "_resolved_reference_source"
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
