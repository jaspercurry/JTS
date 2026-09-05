# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Guards for ``ProviderCatalogEntry.runtime_imports`` and the doctor check
that consumes it.

Background (issue #2197). Nothing verified that the *configured* voice
provider's code could be imported. The ``/voice`` wizard offers every
provider in the catalog and ``switch-voice-provider.sh`` will select any of
them; a venv missing one package surfaced only as a jasper-voice that would
not start. ``check_provider_importable`` closes that, and it is only as good
as the ``runtime_imports`` declaration it reads — so both halves of that
declaration are pinned here against the code they describe:

- ``runtime_imports[0]`` must be the adapter module
  ``daemon_main._make_connection`` actually imports for that provider id.
- ``runtime_imports[1:]`` must cover every third-party module an adapter
  imports *lazily* (inside a function body), because those are exactly the
  ones a plain ``import <adapter>`` does not prove are installed.

Both are checked by parsing the real source with ``ast`` rather than by
importing it, so the guards run on a machine that does not have the provider
SDKs installed.
"""
from __future__ import annotations

import ast
import dataclasses
import subprocess
import sys
from pathlib import Path

import pytest

import jasper.cli.doctor as doctor_pkg
from jasper.cli.doctor import voice as doctor_voice
from jasper.cli.doctor._evidence import evidence as doctor_evidence
from jasper.cli.doctor._registry import registered_checks
from jasper.voice.catalog import PROVIDERS, ProviderCatalogEntry
from jasper.voice.provider_state import ActiveProviderState

ROOT = Path(__file__).resolve().parents[1]
DAEMON_MAIN = ROOT / "jasper" / "voice" / "daemon_main.py"


def _function_def(path: Path, name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{path.name} no longer defines {name}()")


# ---------------------------------------------------------------------------
# Half 1 — the declared adapter module is the one the daemon imports.
# ---------------------------------------------------------------------------


def test_declared_adapter_modules_match_daemon_dispatch():
    """``runtime_imports[0]`` is the module ``_make_connection`` imports.

    The provider-id → adapter-module mapping is stated in two places by
    necessity: the daemon needs the concrete class (with per-provider
    constructor kwargs), the doctor needs only the module name. This test is
    what keeps them one fact. Rename an adapter module, or add a fourth
    provider without declaring it, and this fails.
    """
    node = _function_def(DAEMON_MAIN, "_make_connection")
    imported = {
        f"jasper.voice.{child.module}"
        for child in ast.walk(node)
        if isinstance(child, ast.ImportFrom) and child.level == 1 and child.module
    }
    declared = {entry.runtime_imports[0] for entry in PROVIDERS}
    assert imported == declared, (
        "jasper/voice/daemon_main.py::_make_connection imports "
        f"{sorted(imported)} but the catalog declares {sorted(declared)}. "
        "Update ProviderCatalogEntry.runtime_imports[0] to match — "
        "jasper-doctor's check_provider_importable reads it."
    )


# ---------------------------------------------------------------------------
# Half 2 — every lazily-imported third-party module is declared.
# ---------------------------------------------------------------------------


def _lazy_third_party_imports(module_path: Path) -> set[str]:
    """Root names of non-stdlib, non-first-party modules imported inside a
    function body in ``module_path``."""
    tree = ast.parse(module_path.read_text())
    top_level = set(tree.body)
    lazy: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if node in top_level:
            continue  # module-top import — covered by importing the adapter
        if isinstance(node, ast.ImportFrom):
            if node.level:  # relative — first-party
                continue
            names = [node.module or ""]
        else:
            names = [alias.name for alias in node.names]
        for name in names:
            root = name.split(".")[0]
            if not root or root == "jasper":
                continue
            # A lazily-imported stdlib module needs no declaration — it
            # cannot be missing from the venv, which is the only thing
            # runtime_imports exists to detect.
            if root in sys.stdlib_module_names:
                continue
            lazy.add(root)
    return lazy


@pytest.mark.parametrize("entry", PROVIDERS, ids=lambda e: e.id)
def test_lazy_sdk_imports_are_declared(entry: ProviderCatalogEntry):
    """Any third-party module an adapter imports lazily must be declared.

    A lazy SDK import is invisible to ``import <adapter>``: openai_session
    defers ``from openai import AsyncOpenAI`` into ``_resolve_connect_call``
    so a connection object can be built without the SDK. That is exactly the
    package a broken venv is missing, so it has to be named explicitly.
    """
    adapter = entry.runtime_imports[0]
    path = ROOT / Path(*adapter.split(".")).with_suffix(".py")
    assert path.exists(), f"{adapter} does not resolve to a file"
    lazy = _lazy_third_party_imports(path)
    declared = set(entry.runtime_imports[1:])
    assert lazy <= declared, (
        f"{adapter} lazily imports {sorted(lazy - declared)} but provider "
        f"{entry.id!r} declares {sorted(declared)}. Add the missing module(s) "
        "to runtime_imports so check_provider_importable verifies them."
    )


def test_grok_inherits_the_openai_adapters_lazy_sdk():
    """grok_session subclasses the OpenAI adapter, so it reaches the same
    deferred ``openai`` import through inherited code its own AST never
    shows. Named here so the parametrized guard above (which only reads
    grok_session's own source) is not mistaken for full coverage."""
    grok = next(e for e in PROVIDERS if e.id == "grok")
    openai = next(e for e in PROVIDERS if e.id == "openai")
    assert set(openai.runtime_imports[1:]) <= set(grok.runtime_imports[1:])


# ---------------------------------------------------------------------------
# The declaration itself
# ---------------------------------------------------------------------------


def test_runtime_imports_is_a_required_field():
    """No default. An empty default would make check_provider_importable
    silently vacuous for a newly added provider — the exact silent pass this
    declaration exists to prevent."""
    field = {f.name: f for f in dataclasses.fields(ProviderCatalogEntry)}[
        "runtime_imports"
    ]
    assert field.default is dataclasses.MISSING
    assert field.default_factory is dataclasses.MISSING


@pytest.mark.parametrize("entry", PROVIDERS, ids=lambda e: e.id)
def test_every_provider_declares_at_least_its_adapter(entry: ProviderCatalogEntry):
    assert entry.runtime_imports, f"{entry.id} declares no runtime_imports"
    assert entry.runtime_imports[0].startswith("jasper.voice."), (
        f"{entry.id}: runtime_imports[0] must be the adapter module, got "
        f"{entry.runtime_imports[0]!r}"
    )


# ---------------------------------------------------------------------------
# The doctor check
# ---------------------------------------------------------------------------


def _state(status: str, provider: str = "") -> ActiveProviderState:
    return ActiveProviderState(
        provider, None, status, "/var/lib/jasper/voice_provider.env",
    )


@pytest.fixture
def probe(monkeypatch):
    """Capture the argv the check hands its import probe, and control the
    result, without spawning a real interpreter."""
    calls: list[list[str]] = []
    timeouts: list[float] = []
    box: dict[str, object] = {
        "returncode": 0, "stdout": "", "stderr": "", "raises": None,
    }

    def fake_run(cmd, timeout=5.0):
        calls.append(cmd)
        timeouts.append(timeout)
        if box["raises"] is not None:
            raise box["raises"]
        return subprocess.CompletedProcess(
            cmd, box["returncode"], box["stdout"], box["stderr"],
        )

    monkeypatch.setattr(doctor_voice, "_run", fake_run)
    box["calls"] = calls
    box["timeouts"] = timeouts
    return box


def test_no_probe_when_no_provider_configured(monkeypatch, probe):
    monkeypatch.setattr(
        doctor_voice, "read_active_provider_state", lambda: _state("unset"),
    )
    result = doctor_voice.check_provider_importable()
    assert result.status == "ok"
    assert result.reason == doctor_voice.REASON_PROVIDER_IMPORTS_NOT_CONFIGURED
    assert probe["calls"] == [], "must not spawn a probe with nothing selected"


def test_no_probe_when_ssot_file_missing(monkeypatch, probe):
    monkeypatch.setattr(
        doctor_voice, "read_active_provider_state", lambda: _state("missing"),
    )
    assert doctor_voice.check_provider_importable().status == "ok"
    assert probe["calls"] == []


def test_does_not_probe_when_the_selection_is_unreadable(monkeypatch, probe):
    """A non-root doctor that cannot traverse /var/lib/jasper knows nothing
    about the provider's imports, so it must spawn nothing. The verdict
    itself is pinned in tests/test_doctor_voice.py."""
    monkeypatch.setattr(
        doctor_voice, "read_active_provider_state", lambda: _state("unreadable"),
    )
    doctor_voice.check_provider_importable()
    assert probe["calls"] == []


def test_probes_exactly_the_declared_modules(monkeypatch, probe):
    monkeypatch.setattr(
        doctor_voice,
        "read_active_provider_state",
        lambda: _state("configured", "openai"),
    )
    result = doctor_voice.check_provider_importable()
    assert result.status == "ok"
    openai = next(e for e in PROVIDERS if e.id == "openai")
    # argv is <python> <flags…> -c <probe source> <module…>; the module list
    # is everything after the probe source.
    argv = probe["calls"][0]
    assert argv[argv.index("-c") + 2:] == list(openai.runtime_imports)
    assert "not a live-session probe" in result.detail


def test_fails_and_names_the_missing_module(monkeypatch, probe):
    monkeypatch.setattr(
        doctor_voice,
        "read_active_provider_state",
        lambda: _state("configured", "openai"),
    )
    probe["returncode"] = 1
    probe["stdout"] = (
        "jasper.voice.openai_session\tModuleNotFoundError: "
        "No module named 'audioop'\n"
    )
    result = doctor_voice.check_provider_importable()
    assert result.status == "fail"
    assert "jasper.voice.openai_session" in result.detail
    assert "No module named 'audioop'" in result.detail
    assert "deploy-to-pi.sh" in result.detail


def test_falls_back_to_stderr_when_probe_dies_without_output(monkeypatch, probe):
    monkeypatch.setattr(
        doctor_voice,
        "read_active_provider_state",
        lambda: _state("configured", "gemini"),
    )
    probe["returncode"] = 1
    probe["stderr"] = "Traceback (most recent call last):\nSegmentationFault\n"
    result = doctor_voice.check_provider_importable()
    assert result.status == "fail"
    assert "SegmentationFault" in result.detail


def test_child_output_is_redacted_before_it_reaches_the_report(monkeypatch, probe):
    """The failure line is arbitrary text from a child's traceback. It goes
    through the doctor's own redaction policy, not straight into a report an
    operator pastes into an issue."""
    monkeypatch.setattr(
        doctor_voice,
        "read_active_provider_state",
        lambda: _state("configured", "openai"),
    )
    probe["returncode"] = 1
    probe["stdout"] = (
        "jasper.voice.openai_session\tRuntimeError: bad config "
        "api_key=sk-abcd1234efgh5678\n"
    )
    result = doctor_voice.check_provider_importable()
    assert result.status == "fail"
    assert "sk-abcd1234efgh5678" not in result.detail


def test_child_output_is_length_capped(monkeypatch, probe):
    """A runaway traceback line must not flood the flat report."""
    monkeypatch.setattr(
        doctor_voice,
        "read_active_provider_state",
        lambda: _state("configured", "gemini"),
    )
    probe["returncode"] = 1
    probe["stdout"] = "jasper.voice.gemini_session\tImportError: " + "x" * 5000
    result = doctor_voice.check_provider_importable()
    assert result.status == "fail"
    assert "x" * 5000 not in result.detail
    assert result.detail.count("x") <= doctor_voice._EXCEPTION_DETAIL_LIMIT


def test_timeout_is_a_warning_not_a_failure(monkeypatch, probe):
    monkeypatch.setattr(
        doctor_voice,
        "read_active_provider_state",
        lambda: _state("configured", "gemini"),
    )
    probe["raises"] = subprocess.TimeoutExpired(cmd=["python"], timeout=30.0)
    result = doctor_voice.check_provider_importable()
    assert result.status == "warn"
    assert "timed out" in result.detail


# ---------------------------------------------------------------------------
# The probe must die before the doctor's per-row guard does
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "row_timeout",
    [0.5, 1, 1.5, 3, 6, 7, 10, 15, 20, 120],
)
def test_probe_timeout_stays_under_the_row_guard(row_timeout):
    """The doctor's per-row guard is DOCTOR_CHECK_TIMEOUT_SECONDS (15 s by
    default), and the import probe must finish strictly inside it.

    It has to hold at *every* row timeout the harness could be given, not
    just the shipped 15 s: subtraction alone goes negative on a small row
    value, and a probe timeout at or above the row guard means the row is
    cancelled first — which makes this check's could-not-verify warn
    unreachable AND releases the memory-sample lock while subprocess.run's
    child is still resident.
    """
    row = float(row_timeout)
    doctor_evidence.set_check_timeout(row)
    probe = doctor_voice._import_probe_timeout()
    assert probe > 0, f"probe timeout must be positive, got {probe}"
    assert probe < row, (
        f"probe timeout {probe} is not below the row guard {row} — the row "
        "would be cancelled first and leave the child running"
    )


def test_probe_timeout_at_the_shipped_default_matches_the_doctor_ceiling():
    """At the shipped 15 s row guard the probe gets 10 s — the same ceiling
    the doctor's other subprocess probes already use."""
    assert doctor_pkg.DOCTOR_CHECK_TIMEOUT_SECONDS == 15.0
    assert doctor_voice._import_probe_timeout() == 10.0


def test_check_passes_the_derived_timeout_to_the_probe(monkeypatch, probe):
    """The value derived from *this run's* row guard is what reaches
    subprocess.run — not the shipped constant the check would otherwise
    recompute and ignore."""
    monkeypatch.setattr(
        doctor_voice,
        "read_active_provider_state",
        lambda: _state("configured", "gemini"),
    )
    doctor_evidence.set_check_timeout(20.0)
    doctor_voice.check_provider_importable()
    assert probe["timeouts"] == [15.0]


# ---------------------------------------------------------------------------
# The probe must not import from the caller's cwd
# ---------------------------------------------------------------------------


def test_probe_does_not_put_cwd_on_sys_path(tmp_path):
    """`python -c` sets sys.path[0] = '' — so without `-P` a doctor run
    started from the rsync checkout imports `jasper.voice.*` from there
    instead of /opt/jasper, and reports the checkout green while the daemon
    still loads the old runtime.

    Runs the real probe source with the real flags against a marker module
    that exists only in cwd, so this pins the behaviour and not just the
    presence of a flag in an argv list.
    """
    (tmp_path / "jts_probe_cwd_marker.py").write_text("VALUE = 1\n")

    def run(flags):
        return subprocess.run(
            [sys.executable, *flags, "-c", doctor_voice._IMPORT_PROBE,
             "jts_probe_cwd_marker"],
            cwd=tmp_path, capture_output=True, text=True, timeout=60,
        )

    # Control: without the flag the cwd module is importable, which is the
    # hazard. If this ever stops being true the guard below is vacuous.
    assert run(()).returncode == 0, (
        "control failed: cwd is not on sys.path, so this test proves nothing"
    )
    blocked = run(doctor_voice._PROBE_INTERPRETER_FLAGS)
    assert blocked.returncode == 1
    assert "ModuleNotFoundError" in blocked.stdout


def test_check_uses_the_cwd_isolating_flags(monkeypatch, probe):
    monkeypatch.setattr(
        doctor_voice,
        "read_active_provider_state",
        lambda: _state("configured", "gemini"),
    )
    doctor_voice.check_provider_importable()
    argv = probe["calls"][0]
    assert argv[0] == sys.executable
    # Without this the loop below is vacuous when the tuple is emptied.
    assert doctor_voice._PROBE_INTERPRETER_FLAGS, "no cwd-isolating flags left"
    for flag in doctor_voice._PROBE_INTERPRETER_FLAGS:
        assert flag in argv[: argv.index("-c")], (
            f"{flag} must precede -c to take effect; argv={argv}"
        )


def test_check_is_registered_in_the_doctor_registry():
    names = {c.func.__name__ for c in registered_checks()}
    assert "check_provider_importable" in names


def test_import_probe_cannot_perturb_the_memory_headroom_check():
    """The import child is the biggest allocation the doctor makes — measured
    at ~70 MB of MemAvailable for the gemini adapter, against a 100 MB warn
    threshold on a 1 GB Pi. The doctor runs checks concurrently, so without a
    shared exclusive lane the probe can trip the headroom check itself and
    report a shortage it created."""
    lanes = {
        c.func.__name__: c.exclusive_group
        for c in registered_checks()
        if c.func.__name__ in {"check_provider_importable", "check_memory_headroom"}
    }
    assert lanes == {
        "check_provider_importable": "memory-sample",
        "check_memory_headroom": "memory-sample",
    }, f"expected both in one exclusive lane, got {lanes}"


def test_import_probe_reports_the_first_failing_module_end_to_end():
    """The probe source itself, run for real: a good module then a bad one
    exits non-zero and names the bad one on stdout."""
    proc = subprocess.run(
        [sys.executable, "-c", doctor_voice._IMPORT_PROBE, "json", "jts_no_such_mod"],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 1
    assert proc.stdout.startswith("jts_no_such_mod\tModuleNotFoundError")
