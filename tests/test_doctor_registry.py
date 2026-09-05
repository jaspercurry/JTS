# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Guard the jasper-doctor registry's ordering and output contracts.

Ordering (ADR-0233 rule 4): a check registers from the module of the subsystem
it observes, and `MODULE_ROSTER` alone decides display position, so the run
sequence must follow the roster, the roster must name exactly the modules that
register checks, a module the roster does not name must be rejected at
registration, and async / exclusive-lane metadata must be explicit.

Output (ADR-0233 rule 3): a status outside the closed set is rejected, every
warn/fail row carries a machine-stable `reason` drawn from its module's
REASON_* constants, and the rows the harness itself produces (crash, timeout,
profile skip, config error) share one shape and carry a harness reason.
"""
from __future__ import annotations

import ast
import asyncio
import collections
import functools
import importlib
import re
from pathlib import Path

import pytest

from jasper.cli import doctor
from jasper.cli.doctor import CheckResult, _registry, _shared
from jasper.cli.doctor._registry import (
    MODULE_ROSTER,
    STREAMBOX_OMITTED_DOCTOR_CHECKS,
    STREAMBOX_OMITTED_DOCTOR_MODULES,
    doctor_check,
    registered_checks,
)


def test_display_order_follows_the_module_roster():
    checks = registered_checks()
    assert checks, "registry is empty — the per-domain modules did not register"
    positions = {module: i for i, module in enumerate(MODULE_ROSTER)}
    seen = [positions[c.module] for c in checks]
    assert seen == sorted(seen), (
        "a module's checks must display as one contiguous block, in roster "
        f"order: {[c.module for c in checks]}"
    )


def test_the_roster_names_exactly_the_modules_that_register_checks():
    """The roster is the source of truth for order, so it goes stale in both
    directions: a new module with no entry has nowhere to display, and an entry
    whose module registers nothing is a dead row."""
    assert len(MODULE_ROSTER) == len(set(MODULE_ROSTER)), (
        f"duplicate roster rows: {MODULE_ROSTER}"
    )
    assert set(MODULE_ROSTER) == {c.module for c in registered_checks()}


def test_streambox_omissions_cannot_go_stale():
    """The two skip sets key off the roster and registered function names
    respectively, so either can drift silently if a module or check is
    renamed or removed without updating the set beside it."""
    assert STREAMBOX_OMITTED_DOCTOR_MODULES <= set(MODULE_ROSTER)
    registered_names = {c.func.__name__ for c in registered_checks()}
    assert STREAMBOX_OMITTED_DOCTOR_CHECKS <= registered_names


def test_async_checks_keep_explicit_registry_metadata():
    checks = registered_checks()
    async_checks = [c for c in checks if c.is_async]
    assert async_checks, "expected at least one async check"
    assert all(c.label for c in async_checks), (
        "async checks need explicit labels for timeout/crash rows"
    )


def test_hardware_sensitive_checks_are_marked_exclusive():
    by_name = {c.func.__name__: c for c in registered_checks()}

    assert by_name["check_mic_capture"].exclusive_group == "audio-probe"
    assert (
        by_name["check_aec_bridge_output_health"].exclusive_group
        == "audio-probe"
    )
    assert (
        by_name["check_renderer_device_resolvable"].exclusive_group
        == "audio-probe"
    )


def test_a_check_from_an_unrostered_module_is_rejected():
    """Roster membership is the structural guard: a module the roster does not
    name has no display position, so registering from one must fail loudly at
    import rather than land somewhere arbitrary."""
    saved = list(_registry._REGISTRY)
    try:
        with pytest.raises(ValueError):
            doctor_check()(lambda: None)
    finally:
        _registry._REGISTRY[:] = saved


@pytest.mark.parametrize("install_profile", ["full", "streambox"])
def test_every_built_check_is_named_by_one_rule(install_profile):
    """`entry.label or _check_name(entry.func)` — on every profile and every
    calling convention. A check named one thing on a full box and another on a
    streambox makes two rows out of one check for anything reading the report."""
    from types import SimpleNamespace

    from jasper.cli.doctor import _build_doctor_checks, _check_name

    built = _build_doctor_checks(SimpleNamespace(), install_profile)
    assert [c.name for c in built] == [
        entry.label or _check_name(entry.func) for entry in registered_checks()
    ]


_REASON_CODE_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")

# Infrastructure, not a subject: these build the harness rows whose vocabulary
# is `jasper.doctor_contract`, and they register no checks of their own.
_NON_DOMAIN_MODULES = frozenset({"__init__", "_registry", "_shared"})

_DOCTOR_PKG_DIR = Path(doctor.__file__).parent


def _module_tree(module_name: str) -> ast.Module:
    source = Path(importlib.import_module(module_name).__file__)
    return ast.parse(source.read_text(encoding="utf-8"))


def _doctor_check_modules() -> list[str]:
    """Every doctor module that CONSTRUCTS a CheckResult — the subjects of the
    reason contract.

    Derived from the source, not from the registry, so a module that builds
    rows outside a registered check is still bound by the contract."""
    names = []
    for path in sorted(_DOCTOR_PKG_DIR.glob("*.py")):
        if path.stem in _NON_DOMAIN_MODULES:
            continue
        module_name = f"jasper.cli.doctor.{path.stem}"
        if any(_check_result_calls(_module_tree(module_name))):
            names.append(module_name)
    return names


def _check_result_calls(tree: ast.Module):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.id if isinstance(func, ast.Name)
            else getattr(func, "attr", None)
        )
        if name == "CheckResult":
            yield node


def _status_arg(call: ast.Call) -> ast.expr | None:
    if len(call.args) > 1:
        return call.args[1]
    return next((k.value for k in call.keywords if k.arg == "status"), None)


def _reason_arg(call: ast.Call) -> ast.expr | None:
    return next((k.value for k in call.keywords if k.arg == "reason"), None)


def _is_symbolic(node: ast.expr | None) -> bool:
    """True for a reason expression built only out of names — the module's
    REASON_* constants — with no inline string anywhere in it."""
    if isinstance(node, (ast.Name, ast.Attribute)):
        return True
    if isinstance(node, ast.IfExp):
        return _is_symbolic(node.body) and _is_symbolic(node.orelse)
    return False


@functools.cache
def _reason_names_imported_elsewhere() -> frozenset[str]:
    """REASON_* names one doctor module imports from another.

    A code homed with its reader (ADR-0233 rule 1) is legitimately unused in
    the module that declares it, so the unused-constant check has to see the
    consumers."""
    names: set[str] = set()
    for path in sorted(_DOCTOR_PKG_DIR.glob("*.py")):
        tree = _module_tree(f"jasper.cli.doctor.{path.stem}")
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            names.update(
                alias.name for alias in node.names
                if alias.name.startswith("REASON_")
            )
    return frozenset(names)


def _reason_contract_violations(tree: ast.Module) -> list[str]:
    violations: list[str] = []
    for call in _check_result_calls(tree):
        status = _status_arg(call)
        reason = _reason_arg(call)
        settled_ok = (
            isinstance(status, ast.Constant)
            and status.value in ("ok", "skipped")
        )
        if not settled_ok and not _is_symbolic(reason):
            violations.append(
                f"line {call.lineno}: a result that can warn/fail needs "
                "reason=<REASON_* constant>"
            )
        if isinstance(reason, ast.Constant):
            violations.append(
                f"line {call.lineno}: reason= must be a REASON_* constant, "
                "not a string literal"
            )

    loads = collections.Counter(
        n.id for n in ast.walk(tree)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
    )
    declared: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or not target.id.startswith("REASON_"):
            continue
        value = node.value
        if not (
            isinstance(value, ast.Constant)
            and isinstance(value.value, str)
            and _REASON_CODE_RE.match(value.value)
        ):
            violations.append(f"line {node.lineno}: {target.id} is not lower_snake_case")
            continue
        if value.value in declared:
            violations.append(
                f"line {node.lineno}: {target.id} duplicates the code of "
                f"{declared[value.value]}"
            )
        declared[value.value] = target.id
        if not loads[target.id] and target.id not in _reason_names_imported_elsewhere():
            violations.append(f"line {node.lineno}: {target.id} is declared but unused")
    return violations


@pytest.mark.parametrize("module_name", _doctor_check_modules())
def test_reason_codes_are_a_closed_symbolic_vocabulary(module_name):
    """ADR-0233 rule 3: every warn/fail row carries a machine-stable reason
    drawn from the module's own REASON_* constants, and every declared
    constant is a used, unique lower_snake_case code."""
    violations = _reason_contract_violations(_module_tree(module_name))
    assert not violations, "\n".join(violations)


def _declared_reason_codes(tree: ast.Module) -> dict[str, str]:
    """Module-level ``REASON_X = "code"`` assignments, code -> constant name."""
    codes: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or not target.id.startswith("REASON_"):
            continue
        value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            codes[value.value] = target.id
    return codes


def test_a_reason_code_is_declared_in_exactly_one_module():
    """One code, one home. A code two domain modules each declare is a
    vocabulary that has already forked: the next edit changes one copy, and
    a consumer branching on the code sees two meanings behind one string.
    The shared ones belong in `_shared.py`, which the domain modules import."""
    homes: dict[str, list[str]] = collections.defaultdict(list)
    for module_name in _doctor_check_modules():
        for code, const in _declared_reason_codes(_module_tree(module_name)).items():
            homes[code].append(f"{module_name}.{const}")
    shared_homes = _declared_reason_codes(_module_tree("jasper.cli.doctor._shared"))
    duplicates = {
        code: sorted(sites) for code, sites in homes.items() if len(sites) > 1
    }
    assert not duplicates, (
        "reason codes declared in more than one module (home them in "
        f"_shared.py and import): {duplicates}"
    )
    collisions = {
        code: sorted(sites) for code, sites in homes.items()
        if code in shared_homes
    }
    assert not collisions, (
        f"reason codes redeclared beside their _shared.py home: {collisions}"
    )


def test_every_check_result_module_registers_a_check():
    """A module that builds rows but registers nothing is a check that fell
    out of the registry."""
    registered = {c.func.__module__ for c in registered_checks()}
    subjects = set(_doctor_check_modules())
    assert subjects <= registered, sorted(subjects - registered)


def _timed_out_row() -> CheckResult:
    async def slow() -> CheckResult:
        await asyncio.sleep(30)
        return CheckResult("slow check", "ok")

    runnable = doctor._RunnableDoctorCheck("slow check", slow, is_async=True)
    return asyncio.run(doctor._run_runnable_with_timeout(runnable, 0.01))


def test_a_check_that_overruns_its_timeout_becomes_a_reasoned_fail():
    row = _timed_out_row()
    assert (row.name, row.status, row.reason) == (
        "slow check",
        "fail",
        _shared.REASON_CHECK_TIMED_OUT,
    )


def test_harness_rows_carry_a_harness_reason():
    """Crash, timeout, profile-skip and the config-error payload are rows no
    check produced. `check_row` is the one row builder, so the shape needs no
    pinning here; what does need pinning is that each carries a reason of its
    own rather than an empty string."""
    rows = [
        _shared._crashed_check_result("boom", RuntimeError("synthetic")),
        _timed_out_row(),
        doctor._profile_skip_result(
            registered_checks()[0], detail="not installed (streambox profile)",
        ),
    ]
    error_row = doctor._error_payload(
        "config: synthetic",
        detail="synthetic",
        reason=_shared.REASON_CONFIG_ERROR,
    )["results"][0]

    assert [r["reason"] for r in doctor._json_payload(rows)["results"]] == [
        _shared.REASON_CHECK_CRASHED,
        _shared.REASON_CHECK_TIMED_OUT,
        _shared.REASON_NOT_INSTALLED,
    ]
    assert error_row["reason"] == _shared.REASON_CONFIG_ERROR


def test_a_status_outside_the_contract_is_rejected():
    with pytest.raises(ValueError):
        CheckResult("x", "unknown")
