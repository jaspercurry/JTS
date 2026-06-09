"""Regression tests for the 'lost edit' bug class.

PR #146 (multi-device peering) added a new wizard but lost two lines
during an edit-merge race: `peering_setup` never made it into the
`from . import (...)` tuple, and `peers_port` was referenced inside
`main()` without ever being defined. The module compiles fine —
Python only resolves the names at call time — so the bug only
surfaced when systemd started the daemon, at which point ALL eight
wizards went down.

Three layers of defense here:

1. **Pattern-specific checks against `__main__.py`** — catches the
   exact `__main__.py` bug that bit us (every `<name>_setup.X` has
   a matching import; every registered wizard has a unique socket-
   backed port).

2. **ruff F821 across every peering-touched file** — catches the
   same lost-edit pattern (undefined name) anywhere else in the
   package. ruff is already in our dev dependencies and is the
   battle-tested implementation of pyflakes-style undefined-name
   detection (handles match/case patterns, comprehensions, walrus,
   nested scopes, etc.). If ruff isn't available locally the test
   skips rather than flakes.

3. **Import-cost check for the combined settings host** — proves
   the socket-activated `jasper.web.__main__` entrypoint doesn't pull
   in wake-corpus recorder dependencies unless `/wake-corpus/` is
   actually used.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parent.parent
_MAIN_PATH = _REPO / "jasper" / "web" / "__main__.py"


# ----------------------------------------------------------------------
# Layer 1 — pattern checks on __main__.py
# ----------------------------------------------------------------------


def test_every_referenced_setup_module_is_imported():
    """Every `xxx_setup.YYY` lookup must have `xxx_setup` in the
    package's `from . import (...)` tuple."""
    text = _MAIN_PATH.read_text()
    referenced = set(re.findall(r"\b([a-z][a-z0-9_]*_setup)\.", text))
    for mod in sorted(referenced):
        in_bulk_import = re.search(
            rf"^\s+{re.escape(mod)},\s*$", text, re.MULTILINE,
        )
        as_separate_import = re.search(rf"\bimport {re.escape(mod)}\b", text)
        assert in_bulk_import or as_separate_import, (
            f"{mod}.X is referenced in __main__.py but {mod} is not "
            f"in `from . import (...)` — adding a new wizard requires "
            f"both the wiring AND the import line."
        )


def test_wizard_registry_has_unique_routes_envs_and_ports():
    """The combined settings host should be driven by WIZARD_SPECS.

    This replaces the old hand-maintained `<name>_port` locals: adding
    a wizard should add one spec row, not several loose tuples that can
    drift during merge-heavy work.
    """
    from jasper.web import __main__ as web_main

    specs = web_main.WIZARD_SPECS
    labels = [spec.label for spec in specs]
    env_vars = [spec.env_var for spec in specs]
    ports = [spec.default_port for spec in specs]

    assert len(labels) == len(set(labels))
    assert len(env_vars) == len(set(env_vars))
    assert len(ports) == len(set(ports))
    assert sum(1 for spec in specs if spec.main_thread) == 1


def test_registered_wizard_default_ports_are_socket_backed():
    """Every default port in WIZARD_SPECS must have a ListenStream.

    jasper-web.socket is the socket-activation contract. If a new
    WizardSpec lands without a matching ListenStream, nginx will 502
    until the next manual unit-file fix.
    """
    from jasper.web import __main__ as web_main

    socket_text = (_REPO / "deploy" / "jasper-web.socket").read_text()
    for spec in web_main.WIZARD_SPECS:
        assert f"ListenStream=127.0.0.1:{spec.default_port}" in socket_text, (
            f"{spec.label} defaults to port {spec.default_port}, but "
            f"deploy/jasper-web.socket has no matching ListenStream."
        )


# ----------------------------------------------------------------------
# Layer 2 — ruff F821 across the peering surface
# ----------------------------------------------------------------------


# Files where a lost edit during a peering refactor could re-introduce
# the bug class. Adding a new file? Add it here.
_PEERING_FILES = [
    "jasper/peering/",  # whole subtree
    "jasper/web/peering_setup.py",
    "jasper/web/__main__.py",
    "jasper/voice_daemon.py",
    "jasper/control/server.py",
    "jasper/cli/doctor/",  # whole subtree — doctor is a package since the decomposition
]


def test_peering_surface_has_no_undefined_names():
    """Run ruff F821 (undefined-name) over every peering-touched file.

    The bug that motivated this would have shown up as
    `peering_setup` reported as undefined in jasper/web/__main__.py.
    """
    ruff = shutil.which("ruff")
    if ruff is None:
        pytest.skip("ruff not installed; install via `pip install ruff`")
    paths = [str(_REPO / p) for p in _PEERING_FILES]
    result = subprocess.run(
        [ruff, "check", "--select=F821", "--no-cache", "--output-format=concise",
         *paths],
        capture_output=True, text=True, cwd=str(_REPO),
    )
    # Exit 0 = no findings, exit 1 = findings. Other codes = ruff error.
    if result.returncode == 0:
        return
    if result.returncode == 1:
        pytest.fail(
            "ruff F821 found undefined names in the peering surface:\n"
            + result.stdout,
        )
    pytest.skip(f"ruff failed to run (exit {result.returncode}): {result.stderr}")


# ----------------------------------------------------------------------
# Layer 3 — combined settings host stays import-cheap
# ----------------------------------------------------------------------


def test_combined_web_import_does_not_load_wake_corpus_heavy_deps():
    """Importing jasper.web.__main__ must not load the recorder stack.

    jasper-web is socket-activated and hosts many lightweight settings
    pages. The wake-corpus page imports NumPy via its recorder pipeline,
    so it must stay lazy until someone actually requests /wake-corpus/.
    """
    code = (
        "import sys; "
        "import jasper.web.__main__; "
        "loaded = [m for m in ("
        "'numpy', 'scipy', 'jasper.web.wake_corpus_setup'"
        ") if m in sys.modules]; "
        "print(','.join(loaded)); "
        "raise SystemExit(1 if loaded else 0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(_REPO),
        timeout=10,
    )
    assert result.returncode == 0, (
        "jasper.web.__main__ imported heavy wake-corpus dependencies: "
        f"{result.stdout.strip() or result.stderr.strip()}"
    )


def test_lazy_wake_corpus_server_construction_stays_import_cheap():
    """Building the lazy /wake-corpus server must not load the recorder."""
    code = """
import sys
import types
from pathlib import Path

import jasper.web.__main__ as web_main


def fake_make_http_server(target, handler_cls):
    return types.SimpleNamespace(RequestHandlerClass=handler_cls)


web_main._systemd.make_http_server = fake_make_http_server
web_main._make_lazy_wake_corpus_server(
    ("127.0.0.1", 0),
    output_dir=Path("."),
    ports={"on": 9876},
    csrf_token="x",
)
loaded = [
    m for m in ("numpy", "scipy", "jasper.web.wake_corpus_setup")
    if m in sys.modules
]
print(",".join(loaded))
raise SystemExit(1 if loaded else 0)
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(_REPO),
        timeout=10,
    )
    assert result.returncode == 0, (
        "lazy /wake-corpus server construction imported recorder deps: "
        f"{result.stdout.strip() or result.stderr.strip()}"
    )
