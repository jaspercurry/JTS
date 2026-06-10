"""Wizard import-chain contract: every setup page imports light.

The pages under ``jasper/web/*_setup.py`` are socket-activated stdlib
servers on a 1 GB Pi. Importing a heavy dependency at module top costs
RAM + startup latency on every page open, and — worse — makes the
wizard process hard-require a package it only needs for one optional
network action. The repo convention (documented in
``jasper/transit/providers/nyc_bus.py`` and modeled on
``tests/test_config.py::test_config_import_chain_does_not_require_httpx``)
is to lazy-import such deps at the I/O point.

Each wizard module is imported in a subprocess with the heavy modules
poisoned in ``sys.modules`` (``None`` makes ``import X`` raise), so an
installed copy can't mask a regression.

Poisoned modules:

- ``httpx`` — only needed when an operator actually triggers a network
  action (HA verify, geocode, model discovery).
- ``onnxruntime`` — wake-model scoring belongs to the voice daemon,
  never to a settings page.

``numpy`` is intentionally NOT poisoned (yet): ``wake_corpus_setup``
pulls it at module top through ``jasper.cli.wake_enroll`` and
``jasper.wake_corpus.bridge_session`` (the recorder's audio pipeline).
The combined settings host already keeps that page lazy
(``tests/test_web_main_imports.py``); making the recorder module itself
numpy-free is a separate burn-down. Add ``numpy`` here once it lands.

Deliberately OUT of scope: daemon-side top-level ``import httpx``
(``jasper/mux.py``, ``jasper/subway.py``, ``jasper/weather.py``, …).
Those modules run inside long-lived daemons where httpx is a hard
runtime dependency of the module's whole purpose — failing fast at
daemon startup with a clean ImportError is correct, and lazifying them
would only move the crash to the first request. Do not "fix" them to
satisfy a broader version of this test.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent

# Modules whose import must fail inside a wizard module's import chain.
# Extend deliberately — each addition is a contract for every wizard.
_POISONED_MODULES = ("httpx", "onnxruntime")

_WIZARD_MODULES = sorted(
    f"jasper.web.{p.stem}"
    for p in (_REPO / "jasper" / "web").glob("*_setup.py")
)


def test_wizard_glob_found_modules():
    """If the glob silently breaks, every case below vacuously passes."""
    assert len(_WIZARD_MODULES) >= 15, _WIZARD_MODULES


@pytest.mark.parametrize("module", _WIZARD_MODULES)
def test_wizard_module_imports_without_heavy_deps(module: str):
    poison = "".join(
        f"sys.modules[{name!r}] = None\n" for name in _POISONED_MODULES
    )
    code = (
        "import sys\n"
        f"{poison}"
        f"import {module}\n"
        "print('ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(_REPO),
    )
    assert result.returncode == 0, (
        f"{module} cannot be imported with {_POISONED_MODULES} poisoned.\n"
        "A wizard module (or something it imports at top level) grew a "
        "top-level import of a heavy dep. Make it lazy at the I/O point — "
        "see the documented convention in "
        "jasper/transit/providers/nyc_bus.py.\n\n"
        f"{result.stderr}"
    )
    assert result.stdout.strip() == "ok"
