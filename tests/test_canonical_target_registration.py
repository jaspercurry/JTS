# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Each daemon named below registers a canonical main_volume target.

`CamillaController._graph_mutation` ducks the main fader across a swap and
releases it to ``min(canonical, current + own depth)``. With no canonical
target registered, that release falls back to the entry snapshot — which an
interleaved `CueDuck` may already have ducked — and the fader strands tens of
dB quiet inside the band `maybe_reconcile_camilla` deliberately refuses to
heal.

A swap MAY pass its own ``held_target_db`` reference, which replaces canonical
for that one swap (#2929; only the crossover-v2 measurement path does). That
does not weaken this guard: those same processes still perform ordinary swaps,
and a measurement plan that has drained supplies no reference at all — so
every daemon below still needs its registration.

This is a lost-edit guard in the sense of `test_web_main_imports.py`: the
registration is one line at the top of a daemon's `main()`, deleting it
compiles fine, and nothing else in the process would notice. It also caught a
real miss — the registration first landed only in `jasper/web/__main__.py`,
while correction and crossover applies run in the separate
`jasper-correction-web` process (`jasper.web.correction_setup:main`).

**What this file does NOT establish.** `_ENTRY_POINTS` is hand-maintained and
nothing checks it against pyproject's `console_scripts` or the `ExecStart`
lines under `deploy/`, so it asserts only that the daemons LISTED register —
never that the list is every daemon that swaps. The frozen set below does not
close that gap either: it catches a mutator call appearing in a NEW module, but
a new CALLER of an already-frozen module is invisible to it. Both misses have
happened here — `jasper-active-speaker` swaps through
`commission_wiring.commission_load_config`, a module already in the frozen set,
and was found by review rather than by either guard. Adding a provenance check
is the tightening these two tables still want.
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
    # `jasper-active-speaker` — commissioning applies the candidate graph
    # inline. Its mutator call lives in `commission_wiring.py`, so the frozen
    # set below sees nothing new when a caller like this one is added; only
    # this table catches it.
    "jasper/cli/active_speaker.py": "main",
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


def test_the_env_registration_installs_the_process_fader_owner() -> None:
    """One call registers BOTH, so the two cannot drift apart.

    The AST tests above pin which daemons call the registrar. This pins what
    the registrar does: a process that swaps the graph needs a canonical
    target AND — now that the fader has one owner — an owner to arbitrate the
    writers that have no coordinator to be injected from. Making it one call is
    what keeps every existing call site correct with no edit of its own.
    """
    from jasper import camilla, volume_owner
    from jasper.volume_coordinator import install_env_canonical_target_provider

    assert volume_owner.volume_owner() is None

    install_env_canonical_target_provider()

    assert camilla._canonical_target_db_provider is not None
    assert volume_owner.volume_owner() is not None
