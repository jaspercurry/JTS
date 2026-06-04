"""Canonical-design-system regression for /wake-corpus/.

The wake-corpus recorder (jasper.web.wake_corpus_setup) migrated from a
hand-rolled single-file HTML page to the canonical design system: the
document shell now renders through ``canonical_page()`` (shared
``/assets/app.css``) with ``canonical_header()`` for the top bar, the page
behaviour lives in a static ES module at ``/assets/wake-corpus/js/main.js``,
and the bespoke recorder CSS in ``/assets/wake-corpus/wake-corpus.css``.

These checks are hardware-free: they exercise the render entrypoint
(``_render_index_html``, which does NOT import NumPy hardware or open any
UDP capture) and the static asset files. They assert the canonical shell,
the CSRF wiring (``<meta name="jts-csrf">`` read by the shared http.js
helpers), the module/stylesheet contract, and that the public surface +
key routes the lazy loader depends on are preserved.

The broader recorder behaviour (RecordingBackend state machine, metadata,
routing, the SSE level meter, bridge reconfiguration) is covered by
``tests/test_wake_corpus_setup.py``.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler
from pathlib import Path


from jasper.web import wake_corpus_setup as wc

_ASSETS = Path(__file__).resolve().parents[1] / "deploy" / "assets" / "wake-corpus"
_MODULE_JS = _ASSETS / "js" / "main.js"
_PAGE_CSS = _ASSETS / "wake-corpus.css"


# ---------------------------------------------------------------------------
# Document shell — canonical design system
# ---------------------------------------------------------------------------


def test_render_links_app_css_and_uses_canonical_header() -> None:
    """The page renders on the shared stylesheet with the canonical sticky
    header (the goal of the migration)."""
    html_text = wc._render_index_html("a" * 32)
    assert "/assets/app.css" in html_text
    assert "app-header" in html_text
    # Lowercase doctype is canonical_page()'s shell (not the old <!DOCTYPE>).
    assert "<!doctype html>" in html_text


def test_render_embeds_canonical_csrf_meta_and_token() -> None:
    """The CSRF token rides in the canonical <meta name="jts-csrf"> tag that
    the shared http.js jsonHeaders() helper reads — not the legacy
    csrf-token meta."""
    token = "deadbeefdeadbeefdeadbeefdeadbeef"
    html_text = wc._render_index_html(token)
    assert 'name="jts-csrf"' in html_text
    assert f'content="{token}"' in html_text
    assert 'name="csrf-token"' not in html_text


def test_render_links_page_css_and_module() -> None:
    html_text = wc._render_index_html("t")
    assert "/assets/wake-corpus/wake-corpus.css" in html_text
    assert '<script type="module" src="/assets/wake-corpus/js/main.js">' in html_text


def test_render_leaves_no_template_placeholders() -> None:
    """A leaked .replace() placeholder would render broken markup."""
    html_text = wc._render_index_html("t")
    for stale in (
        "{header}", "{config_json}", "{csrf_token}",
        "{nav_back_css}", "{nav_back_html}", "{dialog_helpers_js}",
        "{aec3_sweep_js_labels}", "{aec3_sweep_js_order}",
        "{usb_aec3_corpus_label}", "{usb_aec3_sweep_baseline_label}",
    ):
        assert stale not in html_text, stale


def test_render_returns_str() -> None:
    assert isinstance(wc._render_index_html("t"), str)


# ---------------------------------------------------------------------------
# Behaviour preserved — recorder body, routes, public surface
# ---------------------------------------------------------------------------


def test_render_preserves_recorder_body_ids() -> None:
    """The behaviour module binds by element id, so the migration must keep
    every recorder control + its id in the server-rendered body."""
    html_text = wc._render_index_html("t")
    for el_id in (
        "status-card", "session-card", "sessions-card", "record-card",
        "counts-card", "clips-card", "member", "include-chip-aec-profile",
        "include-raw-mic-0", "include-xvf-raw0-dtln", "include-dtln",
        "include-aec3-sweep", "include-usb-mic", "include-usb-dtln",
        "session-begin", "session-unload", "record-btn", "mic-level",
        "mic-level-fill", "mic-level-readout", "counts-matrix", "clips-list",
        "corpus-mode-status", "voice-status", "bridge-output-status",
        "session-id", "err", "recording-info", "elapsed",
    ):
        assert f'id="{el_id}"' in html_text, el_id
    # Condition + distance radios stay selectable.
    for value in ("quiet", "ambient", "music", "near", "mid", "far"):
        assert f'value="{value}"' in html_text, value


def test_config_island_carries_python_leg_data() -> None:
    """Python-built leg labels/order + USB AEC3 labels (which can't live in
    the cached ES module) ride in a JSON island the module reads."""
    import json

    html_text = wc._render_index_html("t")
    start = html_text.index('id="wake-corpus-config">') + len('id="wake-corpus-config">')
    end = html_text.index("</script>", start)
    config = json.loads(html_text[start:end])
    assert set(config) == {
        "aec3_sweep_labels", "aec3_sweep_order",
        "usb_aec3_corpus_label", "usb_aec3_sweep_baseline_label",
    }
    # AEC3 sweep legs + labels match the registry.
    for variant in wc.AEC3_SWEEP_VARIANTS:
        assert variant.leg in config["aec3_sweep_order"]
        assert config["aec3_sweep_labels"][variant.leg] == variant.label
    assert config["usb_aec3_corpus_label"] == wc.USB_AEC3_CORPUS_LABEL
    assert config["usb_aec3_sweep_baseline_label"] == wc.USB_AEC3_SWEEP_BASELINE_LABEL


def test_config_island_cannot_close_script_early() -> None:
    """A label containing '</' must not break out of the inline JSON
    <script> — the renderer escapes the solidus."""
    html_text = wc._render_index_html("t")
    start = html_text.index('id="wake-corpus-config">') + len('id="wake-corpus-config">')
    end = html_text.index("</script>", start)
    island = html_text[start:end]
    # No raw '</' survives inside the island content.
    assert "</" not in island


def test_public_surface_and_lazy_load_contract_preserved() -> None:
    """jasper.web.__main__'s lazy loader calls RecordingBackend +
    _make_handler_class(backend, csrf_token); make_server/main are CLI
    entrypoints. Migrating presentation must not move these names."""
    assert hasattr(wc, "RecordingBackend")
    assert callable(wc._make_handler_class)
    assert callable(wc.make_server)
    assert callable(wc.main)
    assert callable(wc._render_index_html)
    assert wc.CSRF_HEADER == "X-CSRF-Token"


def test_make_handler_class_binds_backend_and_token(tmp_path) -> None:
    """_make_handler_class returns a handler subclass with backend + token
    bound, without starting the asyncio loop / opening any UDP socket."""
    backend = wc.RecordingBackend(output_dir=tmp_path / "out")  # no .start()
    handler_cls = wc._make_handler_class(backend, "tok-123")
    assert issubclass(handler_cls, BaseHTTPRequestHandler)
    assert handler_cls.backend is backend
    assert handler_cls.csrf_token == "tok-123"


def test_get_routes_resolve_via_render_and_module() -> None:
    """The GET '/' route renders the canonical page, and the api/* GET +
    mutating routes the recorder relies on are still referenced by the
    behaviour module (relative paths, not absolute)."""
    html_text = wc._render_index_html("t")
    assert "<title>Wake-word corpus</title>" in html_text
    js = _MODULE_JS.read_text()
    for path in (
        "api/status", "api/clips", "api/sessions",
        "api/session", "api/session/load", "api/session/unload",
        "api/clip/start", "api/clip/stop", "api/corpus-test-mode",
        "api/recording/level",
    ):
        assert path in js, path
    # No accidental absolute-path regression (would 502 behind nginx). Check
    # the api() call sites + fetch/EventSource/audio src — not prose: the
    # module's own comment legitimately mentions '/api/...' to explain why it
    # avoids it. Scan only non-comment lines for a leading-slash API string.
    code_lines = [
        ln for ln in js.splitlines() if not ln.lstrip().startswith("//")
    ]
    code = "\n".join(code_lines)
    assert "'/api/" not in code
    assert '"/api/' not in code
    assert "(`/api/" not in code and "src=`/api/" not in code


# ---------------------------------------------------------------------------
# Static asset contract — ES module + page stylesheet
# ---------------------------------------------------------------------------


def test_static_assets_exist() -> None:
    assert _MODULE_JS.is_file(), f"missing {_MODULE_JS}"
    assert _PAGE_CSS.is_file(), f"missing {_PAGE_CSS}"


def test_module_uses_shared_helpers_not_inline_plumbing() -> None:
    """The module imports the shared http.js / dialog.js helpers rather than
    re-implementing CSRF headers or using native confirm()/alert()."""
    js = _MODULE_JS.read_text()
    assert 'import { jsonHeaders } from "/assets/shared/js/http.js"' in js
    assert 'import { jtsConfirm } from "/assets/shared/js/dialog.js"' in js
    # No raw JSON content-type header literal in CODE (gating-test rule) and no
    # native dialogs (the suppressible popups the shared helper replaces). Scan
    # only non-comment lines — the module's docstring legitimately names the
    # old hand-rolled header + native confirm() it replaced.
    import re as _re

    code_lines = [
        ln for ln in js.splitlines() if not ln.lstrip().startswith("//")
    ]
    code = "\n".join(code_lines)
    # The exact gating regex from tests/test_web_wizard_conventions.py.
    assert not _re.search(
        r"headers:\s*\{\s*['\"]Content-Type['\"]\s*:\s*"
        r"['\"]application/json['\"]\s*\}",
        code,
    )
    native_re = _re.compile(r"(?<![\w.$])(?:window\.)?(?:confirm|alert|prompt)\s*\(")
    assert not native_re.search(code), "native confirm/alert/prompt in module code"


def test_page_css_holds_relocated_recorder_visuals() -> None:
    """The bespoke recorder CSS moved into the page stylesheet (not app.css)."""
    css = _PAGE_CSS.read_text()
    for marker in (".mic-level", ".session-row", ".clip", ".matrix", "minmax(0,"):
        assert marker in css, marker
