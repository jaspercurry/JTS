# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""A package that enumerates its own modules must enumerate ALL of them.

Two packages open with a prose list of what lives inside them, and both are
load-bearing: they are the first thing a reader meets, and the only map of a
package too large to hold in the head. Both had silently gone partial —
``crossover_v2`` named twenty of its thirty-four modules and
``audio_measurement`` twenty-three of its thirty-four — because a list in a
docstring has nothing to keep it honest when a module arrives.

A partial list is worse than no list. A reader who finds no list goes looking;
a reader who finds a list and no entry for ``harmonic_evidence`` concludes
there is no such thing and writes it again.

The check is deliberately cheap and import-free: it globs the package
directory and reads the ``__init__``'s docstring out of the AST. Importing
either package would pull ``numpy`` and the measurement stack, which is the
same cost the ``crossover_v2`` docstring explains it avoids by not
re-exporting :mod:`~jasper.active_speaker.crossover_v2.forward_model`.

Scope is these two packages by name. This is not a repo-wide convention —
most packages have no such list and owe none. Extending it is one entry in
``ENUMERATING_PACKAGES``.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Package path → the dotted name its docstring uses for absolute references.
ENUMERATING_PACKAGES = {
    "jasper/audio_measurement": "jasper.audio_measurement",
    "jasper/active_speaker/crossover_v2": "jasper.active_speaker.crossover_v2",
}


def _package_docstring(package: Path) -> str:
    """The ``__init__``'s docstring, read without importing the package."""

    source = (package / "__init__.py").read_text(encoding="utf-8")
    docstring = ast.get_docstring(ast.parse(source))
    assert docstring, f"{package}/__init__.py has no module docstring"
    return docstring


def _modules_on_disk(package: Path) -> set[str]:
    return {p.stem for p in package.glob("*.py") if p.stem != "__init__"}


def _modules_named(docstring: str, dotted: str) -> set[str]:
    """Module names the docstring cross-references with Sphinx ``:mod:`` roles.

    Both the relative form (``:mod:`.journey```) and the absolute form
    (``:mod:`~jasper.audio_measurement.sweep```) count. The trailing dot in the
    absolute pattern is load-bearing: without it, the sibling package
    ``crossover_v2_flow`` would match the ``crossover_v2`` prefix and read as
    one of this package's own modules.
    """

    relative = re.findall(r":mod:`~?\.(\w+)`", docstring)
    absolute = re.findall(rf":mod:`~?{re.escape(dotted)}\.(\w+)`", docstring)
    return set(relative) | set(absolute)


@pytest.mark.parametrize(
    ("package_path", "dotted"), sorted(ENUMERATING_PACKAGES.items())
)
def test_package_docstring_names_every_module(package_path: str, dotted: str) -> None:
    """Every module on disk is named in the package docstring, or fail by name."""

    package = REPO_ROOT / package_path
    on_disk = _modules_on_disk(package)
    assert on_disk, f"{package_path} has no modules — the glob is looking in the wrong place"

    named = _modules_named(_package_docstring(package), dotted)
    unnamed = sorted(on_disk - named)

    assert not unnamed, (
        f"{package_path}/__init__.py does not name "
        f"{len(unnamed)} of its {len(on_disk)} modules: {', '.join(unnamed)}. "
        "Add a ':mod:' entry for each — the list promises to be complete."
    )


@pytest.mark.parametrize(
    ("package_path", "dotted"), sorted(ENUMERATING_PACKAGES.items())
)
def test_package_docstring_names_no_departed_module(
    package_path: str, dotted: str
) -> None:
    """The other half of honesty: no entry survives the module it describes.

    A deletion PR that leaves its entry behind sends the next reader looking
    for a file that is gone — the same drift as an omission, pointing the other
    way. ``crossover_v2`` lost three modules to the hunt deletion (2.4), which
    is exactly when this direction bites.
    """

    package = REPO_ROOT / package_path
    on_disk = _modules_on_disk(package)
    named = _modules_named(_package_docstring(package), dotted)
    departed = sorted(named - on_disk)

    assert not departed, (
        f"{package_path}/__init__.py names {len(departed)} module(s) that do "
        f"not exist: {', '.join(departed)}. Remove the entries."
    )
