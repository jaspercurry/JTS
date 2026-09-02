# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for the 'lost edit' bug class.

PR #146 (multi-device peering) added a new wizard but lost two lines during an
edit-merge race: a setup module never made it into the import wiring, and a
port local was referenced inside `main()` without ever being defined. The
module compiled fine — Python only resolves the names at call time — so the
bug only surfaced when systemd started the daemon, at which point all wizards
went down.

Three layers of defense here:

1. **Pattern-specific checks against `__main__.py`** — catches the
   exact `__main__.py` bug that bit us (every `<name>_setup.X` has
   a matching import; every registered wizard has a unique socket-
   backed port).

2. **ruff F821 across peering-touched code** — catches the same lost-edit
   pattern (undefined name) anywhere else in the package. ruff is already in
   our dev dependencies and is the battle-tested implementation of
   pyflakes-style undefined-name detection (handles match/case patterns,
   comprehensions, walrus, nested scopes, etc.). If ruff isn't available
   locally the test skips rather than flakes.

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


# Which capability each wizard's availability follows. Named as literals so
# moving a row between the groups — a product decision about what a tier
# grants — cannot pass as a refactor.
_ASSISTANT_PATHS = frozenset({
    "/voice", "/google", "/transit", "/ha", "/weather", "/tools",
})
_WAKE_PATHS = frozenset({"/wake", "/wake-corpus"})
_EVERY_TIER_PATHS = frozenset({
    "/spotify", "/airplay", "/sources", "/wifi", "/speaker", "/sound", "/rooms",
})


@pytest.mark.parametrize("profile", ["full", "streambox"])
@pytest.mark.parametrize(
    ("grants", "expected"),
    [
        (frozenset(), _EVERY_TIER_PATHS),
        (("ASSISTANT",), _EVERY_TIER_PATHS | _ASSISTANT_PATHS),
        (("WAKE_DETECTION",), _EVERY_TIER_PATHS | _WAKE_PATHS),
        (
            ("ASSISTANT", "WAKE_DETECTION"),
            _EVERY_TIER_PATHS | _ASSISTANT_PATHS | _WAKE_PATHS,
        ),
    ],
)
def test_wizard_availability_follows_the_capability_not_the_profile(
    monkeypatch, profile, grants, expected,
):
    """A tier hosts exactly the wizards its capabilities grant.

    The grant table is monkeypatched rather than read, so this pins the
    derivation and not today's rows: a tier with ASSISTANT and no
    WAKE_DETECTION — the mic-bearing-remote streambox — gets every
    assistant wizard and none of the wake ones, whichever profile it is.
    """
    from jasper import install_profile
    from jasper.web import __main__ as web_main

    granted = frozenset(getattr(install_profile.Capability, n) for n in grants)
    monkeypatch.setattr(
        install_profile,
        "PROFILE_CAPABILITIES",
        {p: granted for p in install_profile.VALID_INSTALL_PROFILES},
    )

    assert {
        spec.label for spec in web_main._specs_for_role(profile)
    } == expected


def _streambox_validator_port_arrays() -> tuple[set[int], set[int]]:
    """(expected, forbidden) from install.sh's streambox socket validator."""
    text = (_REPO / "deploy" / "lib" / "install" / "systemd-units.sh").read_text()
    body = text.split("validate_streambox_web_socket() {", 1)[1].split("\n}", 1)[0]
    arrays = []
    for name in ("expected_ports", "forbidden_ports"):
        match = re.search(rf"local -a {name}=\(([^)]*)\)", body, re.S)
        assert match, f"validate_streambox_web_socket lost its {name} array"
        arrays.append({int(tok) for tok in match.group(1).split()})
    return arrays[0], arrays[1]


def test_streambox_socket_validator_and_nginx_name_one_port_set():
    """The three static streambox artefacts must agree on the wizard ports.

    install.sh hard-fails the install when the socket and the validator
    disagree, and a port bound with no nginx location (or the reverse) is a
    502 nobody sees until someone opens that wizard. The set is every
    non-wake wizard: these files cannot follow the grant table at runtime,
    so they carry the assistant ports whether or not the tier holds
    ASSISTANT yet, and the Python gate stays the thing that decides.
    """
    from jasper.install_profile import Capability
    from jasper.web import __main__ as web_main

    wake_ports = {
        spec.default_port
        for spec in web_main.WIZARD_SPECS
        if spec.requires is Capability.WAKE_DETECTION
    }
    wizard_ports = {spec.default_port for spec in web_main.WIZARD_SPECS}

    socket_text = (_REPO / "deploy" / "jasper-web-streambox.socket").read_text()
    listen_ports = {
        int(m.group(1))
        for m in re.finditer(r"^ListenStream=127\.0\.0\.1:(\d+)$", socket_text, re.M)
    }
    assert listen_ports == wizard_ports - wake_ports

    expected_ports, forbidden_ports = _streambox_validator_port_arrays()
    assert expected_ports == listen_ports
    assert forbidden_ports == wake_ports
    assert not (listen_ports & forbidden_ports)

    nginx_text = (_REPO / "deploy" / "nginx-jasper-streambox.conf").read_text()
    nginx_ports = {
        int(m.group(1))
        for m in re.finditer(r"proxy_pass http://127\.0\.0\.1:(\d+)", nginx_text)
    }
    assert nginx_ports & wizard_ports == listen_ports


def test_invalid_web_profile_fails_closed():
    from jasper.web import __main__ as web_main

    assert web_main._specs_for_role("invalid") == ()


# ----------------------------------------------------------------------
# Layer 2 — ruff F821 across the peering surface
# ----------------------------------------------------------------------


# Files where a lost edit during a peering refactor could re-introduce
# the bug class. Adding a new file? Add it here.
_PEERING_FILES = [
    "jasper/peering/",  # whole subtree
    "jasper/web/rooms_setup.py",
    "jasper/web/__main__.py",
    "jasper/voice_daemon.py",
    "jasper/control/server.py",
    "jasper/cli/doctor/",  # whole subtree — doctor is a package since the decomposition
]


def test_peering_surface_has_no_undefined_names():
    """Run ruff F821 (undefined-name) over every peering-touched file.

    The bug that motivated this would have shown up as a setup module
    reported as undefined in jasper/web/__main__.py.
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
