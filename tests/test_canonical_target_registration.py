# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Every process that swaps the CamillaDSP graph registers a canonical target.

`CamillaController._graph_mutation` ducks the main fader across a swap and
releases it to ``min(canonical, current + own depth)``. With no canonical
target registered, that release falls back to the entry snapshot — which an
interleaved `CueDuck` may already have ducked — and the fader strands tens of
dB quiet inside the band `maybe_reconcile_camilla` deliberately refuses to
heal.

This is a lost-edit guard in the sense of `test_web_main_imports.py`: the
registration is one line at the top of a daemon's `main()`, deleting it
compiles fine, and nothing else in the process would notice. It also caught a
real miss — the registration first landed only in `jasper/web/__main__.py`,
while correction and crossover applies run in the separate
`jasper-correction-web` process (`jasper.web.correction_setup:main`).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent

# The two ways a process supplies the target: build one from env, or hand over
# a long-lived coordinator's own reader. Both end at
# `camilla.set_canonical_target_db_provider`.
_REGISTRARS = {
    "install_env_canonical_target_provider",
    "set_canonical_target_db_provider",
}

# entry point module -> the function systemd actually enters.
# Keys are the console-script / `python -m` targets in pyproject.toml and the
# ExecStart lines under deploy/.
_ENTRY_POINTS = {
    # `python -m jasper.web` — hosts /eq/ and /sound/, which apply generated
    # DSP configs (sound_setup's `apply_dsp_config` calls).
    "jasper/web/__main__.py": "main",
    # `jasper-correction-web` — hosts /correction/ and the crossover-v2 flow.
    "jasper/web/correction_setup.py": "main",
    # `jasper-control` — the live pair-balance trim patches the graph.
    "jasper/control/server.py": "main",
    # `jasper-voice` — bass-extension reloads, and the process CueDuck runs in.
    "jasper/voice/daemon_main.py": "run",
    # `python -m jasper.multiroom.reconcile` — the bonded-pipe apply and the
    # solo restore both swap the graph (leader_config / follower_config /
    # active_leader_config).
    "jasper/multiroom/reconcile.py": "main",
    # `jasper-fanin-coupling-reconcile` — `reconcile_current_dsp` reloads the
    # profile config (jasper/sound/runtime.py).
    "jasper/fanin/coupling_reconcile.py": "main",
}

# Modules holding a call to one of `CamillaController`'s four graph mutators.
# Frozen so that adding a swap somewhere new fails here until someone has said
# which daemon hosts it and whether that daemon registers a target.
_GRAPH_SWAP_MODULES = {
    "jasper/active_speaker/commission_wiring.py",
    "jasper/active_speaker/commissioning_service.py",
    "jasper/active_speaker/crossover_v2_flow.py",
    "jasper/active_speaker/runtime_convergence.py",
    "jasper/active_speaker/web_commissioning.py",
    "jasper/bass_extension/__init__.py",
    "jasper/bass_extension/bench/activation.py",
    "jasper/camilla.py",
    "jasper/multiroom/active_leader_config.py",
    "jasper/multiroom/follower_config.py",
    "jasper/multiroom/leader_config.py",
    "jasper/multiroom/runtime_balance.py",
    "jasper/sound/runtime.py",
    "jasper/web/correction_crossover_backend.py",
    "jasper/web/correction_crossover_v2.py",
    "jasper/web/correction_setup.py",
    "jasper/web/sound_setup.py",
}

_MUTATORS = (
    "set_config_file_path",
    "set_active_config_raw",
    "patch_config",
    "reload",
)


def _function(tree: ast.Module, name: str) -> ast.AST:
    for node in tree.body:
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
        ):
            return node
    raise AssertionError(f"no top-level function named {name!r}")


@pytest.mark.parametrize("module, entry", sorted(_ENTRY_POINTS.items()))
def test_graph_swapping_daemon_registers_a_canonical_target(
    module: str, entry: str,
) -> None:
    tree = ast.parse((_REPO / module).read_text(encoding="utf-8"))
    called = {
        node.func.id if isinstance(node.func, ast.Name) else node.func.attr
        for node in ast.walk(_function(tree, entry))
        if isinstance(node, ast.Call)
        and isinstance(node.func, (ast.Name, ast.Attribute))
    }

    assert called & _REGISTRARS, (
        f"{module}:{entry} does not register a canonical main_volume target. "
        "Graph swaps in this process would release their duck against a stale "
        f"entry snapshot. Call one of {sorted(_REGISTRARS)}."
    )


def test_graph_swap_call_sites_stay_in_known_modules() -> None:
    """A swap in a new module needs a decision about which daemon hosts it."""
    found = set()
    for path in sorted((_REPO / "jasper").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _MUTATORS
            ):
                found.add(str(path.relative_to(_REPO)))
                break

    assert found == _GRAPH_SWAP_MODULES, (
        "the set of modules that mutate the live graph changed.\n"
        f"  added:   {sorted(found - _GRAPH_SWAP_MODULES)}\n"
        f"  removed: {sorted(_GRAPH_SWAP_MODULES - found)}\n"
        "For an addition: name the daemon that hosts it and confirm that "
        "daemon appears in _ENTRY_POINTS above, then add the module here."
    )
