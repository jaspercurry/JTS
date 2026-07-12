# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Node-driven assertions for the shared HTTP module's headers and JSON
response contract.

http.js is the cross-page CSRF/JSON fetch layer; the control-token gate
lives here too. These assertions pin the WS1 Phase-2 invisible delivery:
csrfHeaders/jsonHeaders attach X-JTS-Token from the page's
<meta name=jts-control-token> tag first (auto, no household action), fall
back to localStorage, and add nothing when neither is present (the gate-off
path). isControlTokenRequired classifies control's 403 verdict. The full
prompt-and-retry fallback needs a real <dialog> + fetch, exercised
on-device; this is the static-logic guard.

The header assertions mirror tests/test_local_web_host_js.py and evaluate the
module under a minimal browser-global stub. The GET assertions import the real
ES module and probe the fetch request plus success/error response shape.
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


def _run(stored_token: str | None, meta_token: str | None = None) -> dict:
    # localStorage stub returns `stored_token` for the control-token key;
    # the page may also embed the token in <meta name=jts-control-token>
    # (WS1 Phase 2 invisible delivery) — `meta_token` simulates that.
    storage = (
        "null" if stored_token is None else json.dumps(stored_token)
    )
    meta = "null" if meta_token is None else json.dumps(meta_token)
    script = f"""
import {{ readFileSync }} from "node:fs";
// Minimal browser globals the module touches at call time. querySelector is
// selector-aware: the control-token meta is distinct from the CSRF meta.
globalThis.document = {{
  querySelector: (sel) => {{
    if (String(sel).includes("jts-control-token")) {{
      const t = {meta};
      return t === null ? null : {{ content: t }};
    }}
    return {{ content: "csrf-xyz" }};  // meta[name=jts-csrf]
  }},
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


def _run_get_json(*, ok: bool, status: int, payload: dict | None) -> dict:
    payload_js = "null" if payload is None else json.dumps(payload)
    json_body = (
        "async () => { throw new Error('not json'); }"
        if payload is None
        else f"async () => ({payload_js})"
    )
    script = f"""
import {{ readFileSync }} from "node:fs";
const src = readFileSync({json.dumps(str(_MODULE_PATH))}, "utf8");
const url = "data:text/javascript;base64," + Buffer.from(src).toString("base64");
const http = await import(url);
let request = null;
globalThis.fetch = async (path, options) => {{
  request = {{ path, options }};
  return {{
    ok: {str(ok).lower()},
    status: {status},
    json: {json_body},
  }};
}};
try {{
  const body = await http.getJSON("/probe");
  console.log(JSON.stringify({{ ok: true, body, request }}));
}} catch (err) {{
  console.log(JSON.stringify({{
    ok: false,
    message: err.message,
    status: err.status ?? null,
    body: err.body ?? null,
    request,
  }}));
}}
"""
    proc = subprocess.run(
        [_NODE, "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_attaches_token_from_meta_invisible_delivery():
    """WS1 Phase 2: the token embedded in the page meta tag rides along with no
    stored value — the invisible path (the household never pasted anything)."""
    out = _run(None, meta_token="embedded-secret")
    assert out["csrf"]["X-CSRF-Token"] == "csrf-xyz"
    assert out["csrf"]["X-JTS-Token"] == "embedded-secret"
    assert out["json"]["X-JTS-Token"] == "embedded-secret"


def test_meta_token_wins_over_storage():
    """The server-embedded meta token is authoritative over a stale stored one."""
    out = _run("stale-stored", meta_token="fresh-embedded")
    assert out["csrf"]["X-JTS-Token"] == "fresh-embedded"


def test_attaches_token_from_storage_when_no_meta():
    """Fallback: no meta tag (older page / cross-page) -> the stored value."""
    out = _run("household-secret", meta_token=None)
    assert out["csrf"]["X-CSRF-Token"] == "csrf-xyz"
    assert out["csrf"]["X-JTS-Token"] == "household-secret"
    assert out["json"]["Content-Type"] == "application/json"
    assert out["json"]["X-JTS-Token"] == "household-secret"


def test_no_token_header_when_neither_present():
    """Gate-off path: no meta, empty storage -> no X-JTS-Token added, so a
    speaker without a token file sees zero behaviour change."""
    out = _run(None, None)
    assert out["csrf"]["X-CSRF-Token"] == "csrf-xyz"
    assert "X-JTS-Token" not in out["csrf"]
    assert "X-JTS-Token" not in out["json"]


def test_is_control_token_required_classifier():
    out = _run(None)
    assert out["required_403"] is True
    assert out["required_other_403"] is False   # different 403 error
    assert out["required_500"] is False          # wrong status
    assert out["required_null"] is False         # no error object


def test_get_json_returns_success_body():
    assert _run_get_json(ok=True, status=200, payload={"value": 7}) == {
        "ok": True,
        "body": {"value": 7},
        "request": {"path": "/probe", "options": {"cache": "no-store"}},
    }


def test_get_json_preserves_server_error_verdict():
    assert _run_get_json(ok=False, status=409, payload={"error": "not_ready"}) == {
        "ok": False,
        "message": "not_ready",
        "status": 409,
        "body": {"error": "not_ready"},
        "request": {"path": "/probe", "options": {"cache": "no-store"}},
    }


def test_get_json_falls_back_for_non_json_error():
    assert _run_get_json(ok=False, status=502, payload=None) == {
        "ok": False,
        "message": "HTTP 502",
        "status": 502,
        "body": None,
        "request": {"path": "/probe", "options": {"cache": "no-store"}},
    }
