"""Node-driven assertions for the control-token behaviour in the shared
deploy/assets/shared/js/http.js module.

http.js is the cross-page CSRF/JSON fetch layer; the opt-in control-token
gate lives here too. These assertions pin: csrfHeaders/jsonHeaders attach
X-JTS-Token from localStorage (and add nothing when storage is empty —
the default-off path), and isControlTokenRequired classifies control's
403 verdict. The full prompt-and-retry flow needs a real <dialog> +
fetch, exercised on-device; this is the static-logic guard.

Mirrors tests/test_local_web_host_js.py — strip `export`, eval the module
under a minimal browser-global stub, assert the outputs.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_NODE = shutil.which("node")
_REPO = Path(__file__).resolve().parent.parent
_MODULE_PATH = _REPO / "deploy" / "assets" / "shared" / "js" / "http.js"

pytestmark = pytest.mark.skipif(_NODE is None, reason="node not on PATH")


def _run(stored_token: str | None) -> dict:
    # localStorage stub returns `stored_token` for the control-token key.
    storage = (
        "null" if stored_token is None else json.dumps(stored_token)
    )
    script = f"""
import {{ readFileSync }} from "node:fs";
// Minimal browser globals the module touches at call time.
globalThis.document = {{
  querySelector: () => ({{ content: "csrf-xyz" }}),
}};
globalThis.localStorage = {{
  getItem: (k) => (k === "jts-control-token" ? {storage} : null),
  setItem: () => {{}},
}};
// Strip ESM `export ` and the dynamic import() (only used in the prompt path,
// which these static assertions don't exercise) so the body evals as a plain
// function returning the symbols we test.
let src = readFileSync({json.dumps(str(_MODULE_PATH))}, "utf8")
  .replace(/\\bexport\\s+/g, "");
const {{ csrfHeaders, jsonHeaders, isControlTokenRequired }} =
  new Function(src + "\\nreturn {{ csrfHeaders, jsonHeaders, isControlTokenRequired }};")();
const out = {{
  csrf: csrfHeaders(),
  json: jsonHeaders(),
  required_403: isControlTokenRequired(
    {{ status: 403, body: {{ error: "control_token_required" }} }}),
  required_other_403: isControlTokenRequired(
    {{ status: 403, body: {{ error: "host_not_allowed" }} }}),
  required_500: isControlTokenRequired(
    {{ status: 500, body: {{ error: "control_token_required" }} }}),
  required_null: isControlTokenRequired(null),
}};
console.log(JSON.stringify(out));
"""
    proc = subprocess.run(
        [_NODE, "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_attaches_token_when_stored():
    out = _run("household-secret")
    assert out["csrf"]["X-CSRF-Token"] == "csrf-xyz"
    assert out["csrf"]["X-JTS-Token"] == "household-secret"
    assert out["json"]["Content-Type"] == "application/json"
    assert out["json"]["X-JTS-Token"] == "household-secret"


def test_no_token_header_when_storage_empty():
    """Default-off path: empty localStorage -> no X-JTS-Token added, so a
    speaker without the gate enabled sees zero behaviour change."""
    out = _run(None)
    assert out["csrf"]["X-CSRF-Token"] == "csrf-xyz"
    assert "X-JTS-Token" not in out["csrf"]
    assert "X-JTS-Token" not in out["json"]


def test_is_control_token_required_classifier():
    out = _run(None)
    assert out["required_403"] is True
    assert out["required_other_403"] is False   # different 403 error
    assert out["required_500"] is False          # wrong status
    assert out["required_null"] is False         # no error object
