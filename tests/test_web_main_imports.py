"""Regression test: jasper.web.__main__ must wire every wizard fully.

PR #146 (multi-device peering) added a new wizard but lost two lines
during an edit-merge race: `peering_setup` never made it into the
`from . import (...)` tuple, and `peers_port` was referenced inside
`main()` without ever being defined. The module compiles fine —
Python only resolves the names at call time — so the bug only
surfaced when systemd started the daemon, at which point ALL eight
wizards went down (jasper-web crash-looped, /spotify, /voice,
/wake, /wifi etc. all became unreachable).

These are static checks against the exact pattern that bit us: for
each `<name>_setup.X` and `<name>_port` reference in `__main__.py`,
the corresponding import / variable must be present.

A heavier dynamic check (mocking out socket-binding and running
main() under pytest) would catch more, but adds enough fragility
that it'd flake. The regex pattern-match below catches the bug
class that has actually happened.
"""
from __future__ import annotations

import re
from pathlib import Path


_MAIN_PATH = Path(__file__).resolve().parent.parent / "jasper" / "web" / "__main__.py"


def test_every_referenced_setup_module_is_imported():
    """Every `xxx_setup.YYY` lookup must have `xxx_setup` in the
    package's `from . import (...)` tuple."""
    text = _MAIN_PATH.read_text()
    # Find every `<word>_setup.` reference (the `.` rules out the
    # bare `_setup` from things like `_systemd.setup`).
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


def test_every_referenced_port_var_is_defined():
    """Every `xxx_port` reference inside `main()` must have a local
    `xxx_port = ...` assignment. Catches the missing-port-var half
    of the PR #146 bug."""
    text = _MAIN_PATH.read_text()
    referenced = set(re.findall(r"\b([a-z][a-z0-9_]*_port)\b", text))
    for var in sorted(referenced):
        assigned = re.search(rf"\b{re.escape(var)}\s*=", text)
        assert assigned, (
            f"{var} is referenced in __main__.py but never assigned. "
            f"Adding a new wizard port requires the line "
            f"`{var} = int(os.environ.get(\"JASPER_*_WEB_PORT\", \"NNNN\"))` "
            f"alongside the other port-var declarations."
        )
