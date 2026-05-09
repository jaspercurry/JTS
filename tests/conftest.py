"""Shared test setup.

`camilladsp` is a Pi-side runtime dep not installed in the local
venv. Several tests transitively import `jasper.camilla` (which
imports camilladsp at module level), so stub it once here in
conftest before any test module loads. Without this, test
behaviour depends on collection order — a test file that
indirectly imports jasper.camilla without doing its own stub
fails in isolation but passes when collected alongside a file
that does stub it.

The stub is intentionally minimal: just enough to satisfy
`from camilladsp import CamillaClient` at import time. Tests
that exercise CamillaController-flavored paths still pass a fake
camilla in via the constructor or monkey-patch the route, so
this stub is never actually called.
"""
from __future__ import annotations

import sys
import types

if "camilladsp" not in sys.modules:
    stub = types.ModuleType("camilladsp")
    stub.CamillaClient = object  # type: ignore[attr-defined]
    sys.modules["camilladsp"] = stub
