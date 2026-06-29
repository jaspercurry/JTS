# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_aec3_pybind_wrappers_do_not_compile_at_o3() -> None:
    """The deploy-critical pybind glue must stay cheap on 1 GB Pis.

    The actual WebRTC DSP library is built separately. Reintroducing -O3 on
    these wrapper translation units caused deploy-time cc1plus OOM kills on
    JTS2, even with the build correctly isolated from production daemons.
    """
    tree = ast.parse((ROOT / "jasper_aec3" / "setup.py").read_text())
    assignments = {
        node.targets[0].id: ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "BINDING_COMPILE_ARGS"
    }

    assert assignments["BINDING_COMPILE_ARGS"] == ["-O0", "-g0"]
    assert "-O3" not in (ROOT / "jasper_aec3" / "setup.py").read_text()
