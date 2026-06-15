from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


_NODE = shutil.which("node")
_REPO = Path(__file__).resolve().parent.parent
_MODULE_PATH = _REPO / "deploy" / "assets" / "shared" / "js" / "local-web-host.js"

pytestmark = pytest.mark.skipif(_NODE is None, reason="node not on PATH")


def test_local_web_host_module_matches_pair_link_contract() -> None:
    proc = subprocess.run(
        [
            _NODE,
            "--input-type=module",
            "-e",
            f"""
import {{ readFileSync }} from "node:fs";
const src = readFileSync({json.dumps(str(_MODULE_PATH))}, "utf8")
  .replace(/\\bexport\\s+/g, "");
const {{ localWebHost }} = new Function(src + "\\nreturn {{ localWebHost }};")();
const values = [
  "jts4",
  "jts4.local",
  "jts4.local.",
  "  jts3  ",
  "192.168.1.22",
  "bad/host",
  "",
  null,
];
console.log(JSON.stringify(values.map((value) => localWebHost(value))));
""",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == [
        "jts4.local",
        "jts4.local",
        "jts4.local",
        "jts3.local",
        "",
        "",
        "",
        "",
    ]
