# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the wake-word picker wizard at /wake/.

Two layers:
  1. `jasper.wake_models` — registry sanity. Entries can't all be
     bundled (we need at least one downloadable to install), the
     default has to be in the registry, lookup helpers behave.
  2. `jasper.web.wake_setup` — save validation (the registry is the
     allowlist; unavailable models can't be selected; "__custom__"
     can't be persisted) plus HTML render correctness (active row
     gets the active badge, unavailable rows get disabled).

The HTTP handler itself is exercised end-to-end via ThreadingHTTPServer
on a random port to match `tests/test_voice_setup.py`.
"""
from __future__ import annotations

import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from jasper import wake_models
from jasper.web import _common, wake_setup


def _stage_bundled_asset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    present: bool = True,
) -> None:
    def fake_path(entry: wake_models.WakeModelEntry) -> Path | None:
        asset = wake_models.openwakeword_asset_by_key(entry.key)
        if asset is None:
            return None
        path = tmp_path / asset.filename
        if present:
            path.write_bytes(b"model")
        return path

    monkeypatch.setattr(wake_setup, "_bundled_asset_path", fake_path)


# ---------- Registry sanity -----------------------------------------------


def test_registry_is_nonempty():
    assert len(wake_models.REGISTRY) > 0


def test_registry_keys_are_unique():
    keys = [e.key for e in wake_models.REGISTRY]
    assert len(keys) == len(set(keys))


def test_default_key_resolves():
    """DEFAULT_KEY has to point at an entry — install.sh seeds
    /var/lib/jasper/wake_model.env from it on fresh installs, and a
    missing entry would crash the install at runtime."""
    entry = wake_models.default()
    assert entry.key == wake_models.DEFAULT_KEY


def test_at_least_one_bundled_entry():
    """Keep at least one stock openWakeWord row in the user-facing
    registry. install.sh stages those package-resource ONNX files
    explicitly via OPENWAKEWORD_ASSETS."""
    assert any(e.bundled for e in wake_models.REGISTRY)


def test_at_least_one_downloadable_entry():
    """The whole point of the registry is to ship more than just the
    openWakeWord-bundled set. A no-downloadable registry means the
    PR's goal — drop-in Jarvis-without-Hey — wasn't actually wired."""
    assert any(e.download_url for e in wake_models.REGISTRY)


def test_downloadable_entries_use_absolute_paths():
    """Non-bundled entries must point at absolute paths under
    /var/lib/jasper/wake/ so the daemon and the wizard agree on
    where the file lives, regardless of cwd."""
    for entry in wake_models.REGISTRY:
        if entry.download_url:
            assert os.path.isabs(entry.model), (
                f"{entry.key} has download_url but non-absolute model path {entry.model!r}"
            )
            assert entry.model.startswith(wake_models.WAKE_MODELS_DIR), (
                f"{entry.key} model path {entry.model!r} is outside WAKE_MODELS_DIR"
            )


def test_bundled_entries_use_bare_names():
    """openWakeWord resolves bare names like `hey_jarvis` against its
    own package-resource bundle. install.sh owns those ONNX files via
    OPENWAKEWORD_ASSETS, while the user-facing env var stays stable."""
    for entry in wake_models.REGISTRY:
        if entry.bundled:
            assert "/" not in entry.model
            assert not entry.model.endswith(".onnx")
            assert entry.download_url is None


def test_openwakeword_assets_are_pinned():
    """Stock openWakeWord assets are local model files too. They should
    be explicit and hash-checked instead of fetched by the package
    helper at install time."""
    assets = list(wake_models.openwakeword_assets())
    assert assets
    filenames = [asset.filename for asset in assets]
    assert len(filenames) == len(set(filenames))
    assert "embedding_model.onnx" in filenames
    assert "melspectrogram.onnx" in filenames
    assert "silero_vad.onnx" in filenames
    required_filenames = {
        asset.filename
        for asset in wake_models.required_openwakeword_assets()
    }
    assert required_filenames == {
        "embedding_model.onnx",
        "melspectrogram.onnx",
        "silero_vad.onnx",
    }
    assert required_filenames.issubset(filenames)
    for asset in assets:
        assert asset.download_url.startswith(wake_models.OPENWAKEWORD_RELEASE_BASE)
        assert asset.filename.endswith(".onnx")
        assert len(asset.download_sha256) == 64


def test_openwakeword_fallback_asset_is_pinned():
    """The compiled Config fallback is a load-bearing runtime model.
    Keep it in the fail-fast install set even when the recommended
    external Jarvis model download is unavailable on first install."""
    fallback = list(wake_models.fallback_openwakeword_assets())
    assert [asset.key for asset in fallback] == ["hey_jarvis"]
    assert fallback[0].filename == "hey_jarvis_v0.1.onnx"


def test_openwakeword_assets_cover_stock_model_names():
    """Operator-set stock names should continue to work even when they
    are not surfaced in the curated picker."""
    assets_by_key = {asset.key for asset in wake_models.openwakeword_assets()}
    assert {
        "alexa",
        "hey_jarvis",
        "hey_mycroft",
        "hey_rhasspy",
        "timer",
        "weather",
    }.issubset(assets_by_key)


def test_bundled_registry_entries_have_package_assets():
    assets_by_key = {asset.key for asset in wake_models.openwakeword_assets()}
    for entry in wake_models.REGISTRY:
        if entry.bundled:
            assert entry.key in assets_by_key


def test_openwakeword_asset_for_model_maps_stock_names():
    assert wake_models.openwakeword_asset_for_model("hey_jarvis").filename == (
        "hey_jarvis_v0.1.onnx"
    )
    assert wake_models.openwakeword_asset_for_model("timer").filename == (
        "timer_v0.1.onnx"
    )
    assert wake_models.openwakeword_asset_for_model(
        "/var/lib/jasper/wake/jarvis_v2.onnx",
    ) is None


def test_lookup_by_key():
    entry = wake_models.by_key("hey_jarvis")
    assert entry is not None
    assert entry.label == "Hey Jarvis"
    assert wake_models.by_key("nonexistent") is None


def test_lookup_by_model():
    """Reverse lookup — given a JASPER_WAKE_MODEL string, find the
    registry entry. Used by the wizard to highlight the active row."""
    for entry in wake_models.REGISTRY:
        found = wake_models.by_model(entry.model)
        assert found is entry, f"by_model({entry.model!r}) didn't roundtrip {entry.key}"
    # Custom paths return None so the wizard knows to render a
    # "Custom: <path>" row instead of trying to highlight a built-in.
    assert wake_models.by_model("/some/hand-rolled/custom.onnx") is None


def test_only_one_recommended_entry():
    """More than one "recommended" badge would confuse the household.
    Constrain the design to exactly one recommended row."""
    recs = [e for e in wake_models.REGISTRY if e.recommended]
    assert len(recs) == 1, f"expected exactly 1 recommended entry, got {[e.key for e in recs]}"


# ---------- Pure helpers ---------------------------------------------------


def test_load_state_returns_empty_for_missing_file(tmp_path: Path):
    assert wake_setup._load_state(str(tmp_path / "nope.env")) == {}


def test_load_state_round_trips(tmp_path: Path):
    p = str(tmp_path / "w.env")
    _common.write_env_file(p, {"JASPER_WAKE_MODEL": "/var/lib/jasper/wake/jarvis_v2.onnx"})
    assert wake_setup._load_state(p) == {
        "JASPER_WAKE_MODEL": "/var/lib/jasper/wake/jarvis_v2.onnx",
    }


def test_active_model_prefers_state_over_env(monkeypatch):
    """When wake_model.env has a value AND JASPER_WAKE_MODEL is in
    the process env (operator set it in /etc/jasper/jasper.env), the
    wizard's view of "active" is the wake_model.env value — that's
    what the daemon will actually load."""
    monkeypatch.setenv("JASPER_WAKE_MODEL", "hey_mycroft")
    state = {"JASPER_WAKE_MODEL": "alexa"}
    assert wake_setup._active_model(state) == "alexa"


def test_active_model_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("JASPER_WAKE_MODEL", "hey_mycroft")
    assert wake_setup._active_model({}) == "hey_mycroft"


def test_active_model_falls_back_to_hey_jarvis(monkeypatch):
    """Empty wizard state + empty process env = the in-code default.
    install.sh treats the matching openWakeWord package asset as
    required, so this fallback does not depend on a best-effort
    optional stock-model download."""
    monkeypatch.delenv("JASPER_WAKE_MODEL", raising=False)
    assert wake_setup._active_model({}) == "hey_jarvis"


def test_is_available_for_present_bundled_asset(monkeypatch, tmp_path: Path):
    """Bundled openWakeWord names are available only when install.sh
    staged the matching package-resource ONNX file."""
    _stage_bundled_asset(monkeypatch, tmp_path)
    entry = wake_models.by_key("hey_jarvis")
    assert entry is not None
    assert wake_setup._is_available(entry) is True


def test_is_available_for_missing_bundled_asset(monkeypatch, tmp_path: Path):
    _stage_bundled_asset(monkeypatch, tmp_path, present=False)
    entry = wake_models.by_key("hey_jarvis")
    assert entry is not None
    assert wake_setup._is_available(entry) is False


def test_is_available_for_missing_external_file(tmp_path: Path):
    fake = wake_models.WakeModelEntry(
        key="fake",
        label="Fake",
        pronunciation="...",
        description="...",
        model=str(tmp_path / "nope.onnx"),
        fa_per_hour=None,
        source_url="https://example.invalid",
        download_url="https://example.invalid/x.onnx",
    )
    assert wake_setup._is_available(fake) is False


def test_is_available_for_present_external_file(tmp_path: Path):
    p = tmp_path / "real.onnx"
    p.write_bytes(b"\x00" * 100)
    fake = wake_models.WakeModelEntry(
        key="fake",
        label="Fake",
        pronunciation="...",
        description="...",
        model=str(p),
        fa_per_hour=None,
        source_url="https://example.invalid",
        download_url="https://example.invalid/x.onnx",
    )
    assert wake_setup._is_available(fake) is True


# ---------- Save logic -----------------------------------------------------


def test_apply_save_writes_registered_bundled_model(
    monkeypatch,
    tmp_path: Path,
):
    _stage_bundled_asset(monkeypatch, tmp_path)
    new, err = wake_setup._apply_save(
        {"model": "hey_jarvis"}, current={},
    )
    assert err is None
    assert new == {"JASPER_WAKE_MODEL": "hey_jarvis"}


def test_apply_save_rejects_unknown_key():
    new, err = wake_setup._apply_save(
        {"model": "totally-not-a-thing"}, current={},
    )
    assert err is not None
    assert "Unknown model" in err
    assert new == {}


def test_apply_save_rejects_empty_selection():
    new, err = wake_setup._apply_save({}, current={"existing": "x"})
    assert err is not None
    assert "No model selected" in err
    # Current state is preserved on rejection so a bad submit doesn't
    # wipe out a valid prior selection.
    assert new == {"existing": "x"}


def test_apply_save_rejects_custom_placeholder():
    """A crafted POST could submit value=__custom__ from a hand-edited
    page (the rendered radio is disabled). The save handler must
    refuse it explicitly — persisting `__custom__` as the wake model
    would crash the daemon at startup with no clear remedy in the UI."""
    new, err = wake_setup._apply_save({"model": "__custom__"}, current={})
    assert err is not None
    assert "read-only" in err.lower() or "custom" in err.lower()


def test_apply_save_rejects_unavailable_model(tmp_path: Path, monkeypatch):
    """A downloadable entry whose .onnx file isn't on disk yet is in
    the registry but not loadable. The save handler must refuse
    rather than land a config that crashes the daemon."""
    # Inject a registry that points at a definitely-missing file.
    fake = wake_models.WakeModelEntry(
        key="probe_missing",
        label="Probe Missing",
        pronunciation="...",
        description="...",
        model=str(tmp_path / "missing.onnx"),
        fa_per_hour=None,
        source_url="https://example.invalid",
        download_url="https://example.invalid/x.onnx",
    )
    monkeypatch.setattr(wake_models, "REGISTRY", (fake,))
    new, err = wake_setup._apply_save({"model": "probe_missing"}, current={})
    assert err is not None
    assert "isn't downloaded" in err
    assert new == {}


# ---------- Threshold logic -----------------------------------------------
# _parse_threshold + the threshold codepath in _apply_save were
# removed when the sensitivity slider became its own JSON POST through
# jasper/control/server.py POST /aec/threshold. _active_threshold stays
# as a clean "read what the daemon will load" helper independent of
# where the UI control is rendered.
#
# Threshold-preservation across a /wake/ model save is now covered
# by test_apply_save_preserves_threshold_in_state below; the daemon-
# facing JASPER_WAKE_THRESHOLD validation is in jasper/config.py and
# in jasper.control.server._write_wake_threshold.


def test_apply_save_preserves_threshold_in_state(monkeypatch, tmp_path: Path):
    """The sensitivity slider writes the same wake_model.env file. A
    model save must preserve any JASPER_WAKE_THRESHOLD already in the
    state dict, otherwise saving a new model would silently zap the
    slider's value."""
    current = {
        "JASPER_WAKE_MODEL": "hey_jarvis",
        "JASPER_WAKE_THRESHOLD": "0.35",
    }
    _stage_bundled_asset(monkeypatch, tmp_path)
    new, err = wake_setup._apply_save({"model": "alexa"}, current=current)
    assert err is None
    assert new == {
        "JASPER_WAKE_MODEL": "alexa",
        "JASPER_WAKE_THRESHOLD": "0.35",
    }


def test_active_threshold_falls_back_to_default(monkeypatch):
    monkeypatch.delenv("JASPER_WAKE_THRESHOLD", raising=False)
    assert wake_setup._active_threshold({}) == wake_setup.DEFAULT_WAKE_THRESHOLD


def test_active_threshold_reads_from_state(monkeypatch):
    monkeypatch.delenv("JASPER_WAKE_THRESHOLD", raising=False)
    assert wake_setup._active_threshold(
        {"JASPER_WAKE_THRESHOLD": "0.3"},
    ) == 0.3


def test_active_threshold_state_wins_over_env(monkeypatch):
    monkeypatch.setenv("JASPER_WAKE_THRESHOLD", "0.7")
    assert wake_setup._active_threshold(
        {"JASPER_WAKE_THRESHOLD": "0.4"},
    ) == 0.4


def test_active_threshold_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("JASPER_WAKE_THRESHOLD", "0.2")
    assert wake_setup._active_threshold({}) == 0.2


def test_active_threshold_ignores_malformed_value(monkeypatch):
    """A garbage value in the env file shouldn't break the page —
    fall through to the next source, ultimately the compiled default."""
    monkeypatch.delenv("JASPER_WAKE_THRESHOLD", raising=False)
    assert wake_setup._active_threshold(
        {"JASPER_WAKE_THRESHOLD": "not-a-float"},
    ) == wake_setup.DEFAULT_WAKE_THRESHOLD


def test_active_threshold_ignores_out_of_range_value(monkeypatch):
    monkeypatch.delenv("JASPER_WAKE_THRESHOLD", raising=False)
    assert wake_setup._active_threshold(
        {"JASPER_WAKE_THRESHOLD": "9.0"},
    ) == wake_setup.DEFAULT_WAKE_THRESHOLD


# ---------- Page render ----------------------------------------------------


def test_index_html_renders_all_registry_entries():
    html = wake_setup._index_html({}).decode()
    for entry in wake_models.REGISTRY:
        assert entry.label in html, f"{entry.label!r} missing from rendered page"
        assert f'value="{entry.key}"' in html


def test_index_html_marks_active_row(monkeypatch):
    monkeypatch.delenv("JASPER_WAKE_MODEL", raising=False)
    state = {"JASPER_WAKE_MODEL": "alexa"}
    html = wake_setup._index_html(state).decode()
    # The "alexa" radio must be checked, the others must not be.
    alexa_idx = html.find('value="alexa"')
    assert alexa_idx >= 0
    # Look just at the radio input tag for alexa.
    alexa_input_end = html.find(">", alexa_idx)
    alexa_input = html[alexa_idx - 100 : alexa_input_end + 1]
    assert "checked" in alexa_input
    # And an "active" badge appears in the same row.
    assert '<span class="badge">active</span>' in html


def test_index_html_renders_custom_row_for_unknown_active(monkeypatch):
    """Operator pointed JASPER_WAKE_MODEL at a hand-rolled .onnx. The
    page must surface that as a Custom row so the household can see
    the daemon's actual state, even if they can't pick it again from
    the UI."""
    custom_path = "/home/pi/hand-rolled/custom_wake.onnx"
    monkeypatch.delenv("JASPER_WAKE_MODEL", raising=False)
    state = {"JASPER_WAKE_MODEL": custom_path}
    html = wake_setup._index_html(state).decode()
    assert "Custom:" in html
    assert custom_path in html


def test_index_html_renders_echo_and_advanced_fusion_controls():
    """The primary screen presents echo choices, while low-level wake
    streams live under Advanced wake fusion."""
    html = wake_setup._index_html({}).decode()
    assert "Echo cancellation" in html
    assert 'id="profile-auto"' in html
    assert 'id="profile-xvf_chip_aec"' in html
    assert 'id="profile-xvf_software_aec3"' in html
    assert 'id="profile-direct_mic"' in html
    assert "Advanced wake fusion" in html
    assert 'id="profile-xvf_chip_aec_testing"' in html
    assert 'id="layer-aec"' not in html
    assert 'id="layer-raw"' in html
    assert 'id="layer-dtln"' in html
    assert 'id="layer-chip_aec"' in html
    # Toggle classes are styled by the shared app.css switch rules.
    assert 'class="toggle"' in html
    # Each row exposes a status element for the poll loop to fill.
    assert 'id="layer-status-raw"' in html
    assert 'id="layer-status-dtln"' in html
    assert 'id="layer-status-chip_aec"' in html


def test_index_html_renders_microphone_status_card():
    """The compact mic card gives a fresh install visibility into the
    detected mic/topology without making users read env files."""
    html = wake_setup._index_html({}).decode()
    assert "Microphone" in html
    for dom_id in (
        "mic-status-name",
        "mic-status-firmware",
        "mic-status-mode",
        "mic-status-session-source",
        "mic-status-wake-legs",
        "mic-status-wake-word",
        "mic-status-warning",
    ):
        assert f'id="{dom_id}"' in html


def test_index_html_chip_aec_controls_are_advanced_not_primary():
    """Chip beam scoring and validation stay available, but not as the
    primary household-facing echo UX."""
    html = wake_setup._index_html({}).decode()
    assert "Hardware beam scoring" in html
    assert "Hardware AEC validation mode" in html
    assert "Advanced wake fusion" in html


def test_index_html_includes_sensitivity_slider():
    """Sensitivity is on /wake/ as a native wake-word tuning control."""
    html = wake_setup._index_html({}).decode()
    assert 'type="range"' in html
    assert 'id="sensitivity-input"' in html
    assert 'id="sensitivity-save"' in html


def test_index_html_discloses_wake_event_recordings():
    html = wake_setup._index_html({}).decode()
    assert "Wake recordings and privacy" in html
    assert "/var/lib/jasper/wake-events/" in html
    assert "WAV audio" in html
    assert "metadata rows are kept" in html
    assert "does not leave the speaker automatically" in html
    assert "Reset archives the" in html
    assert "delete old archives manually" in html


def test_index_html_no_system_crosslink_for_wake_detection():
    """The stale system-dashboard crosslink must not return now that
    microphone and wake controls live together on /wake/."""
    html = wake_setup._index_html({}).decode()
    assert 'moved-panel' not in html
    assert '/system/' not in html


# ---------- End-to-end HTTP exercise ---------------------------------------


@pytest.fixture
def running_server(tmp_path: Path):
    """Spin up the actual ThreadingHTTPServer on a kernel-picked port
    so a request against it goes through the real handler — same
    coverage shape as test_voice_setup."""
    state_path = str(tmp_path / "wake_model.env")
    server = wake_setup.make_server(("127.0.0.1", 0), state_path=state_path)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", state_path
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_http_get_returns_html(running_server):
    base, _ = running_server
    with urllib.request.urlopen(base + "/") as resp:
        body = resp.read().decode()
    assert "Wake word" in body
    assert "Jarvis" in body


def test_http_post_save_persists_state(running_server, monkeypatch):
    from ._web_test_helpers import post_with_csrf
    base, state_path = running_server
    _stage_bundled_asset(monkeypatch, Path(state_path).parent)
    # Stub the systemctl shellout — we don't want a unit test to
    # actually try to restart jasper-voice.
    called = []
    monkeypatch.setattr(
        wake_setup, "restart_voice_daemon",
        lambda: called.append("restart"),
    )
    post_with_csrf(base, "/save", {"model": "alexa"})
    # Wizard wrote the env file at mode 0644 (path-only, no secret).
    assert os.path.exists(state_path)
    assert os.stat(state_path).st_mode & 0o777 == 0o644
    assert wake_setup._load_state(state_path) == {
        "JASPER_WAKE_MODEL": "alexa",
    }
    assert called == ["restart"]


def test_http_post_save_preserves_existing_threshold(running_server, monkeypatch):
    """Form save (model picker) and JSON POST /sensitivity write the
    same wake_model.env. A model-save must preserve any
    JASPER_WAKE_THRESHOLD the slider's /sensitivity handler previously
    wrote into the file — otherwise picking a new model would silently
    zap the user's sensitivity setting."""
    from ._web_test_helpers import post_with_csrf
    base, state_path = running_server
    _stage_bundled_asset(monkeypatch, Path(state_path).parent)
    called = []
    monkeypatch.setattr(
        wake_setup, "restart_voice_daemon",
        lambda: called.append("restart"),
    )
    # Seed wake_model.env as if /sensitivity had previously landed.
    with open(state_path, "w") as f:
        f.write("JASPER_WAKE_THRESHOLD=0.42\nJASPER_WAKE_MODEL=jarvis_v2\n")
    post_with_csrf(base, "/save", {"model": "alexa"})
    state = wake_setup._load_state(state_path)
    assert state["JASPER_WAKE_MODEL"] == "alexa"
    assert state["JASPER_WAKE_THRESHOLD"] == "0.42"
    assert called == ["restart"]


# ---------- Mic/wake proxy routes ------------------------------------------
# These cover the /detection.json poll plus the /layer/<name> and
# /sensitivity POST routes that forward to jasper-control with the
# wizard's user-facing vocabulary (legacy layer/aec, sensitivity) rewritten
# to jasper-control's internal vocabulary (/aec/{toggle,leg,threshold}).


@pytest.fixture
def fake_control():
    """Stand up a fake jasper-control HTTP server on a random port.
    Records each request as (method, path, parsed-json-or-None) and
    returns canned responses keyed by path."""
    import json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    received: list[tuple] = []
    # Default response is the AEC status shape jasper-control returns.
    responses = {
        "/aec": {
            "mode": "auto",
            "bridge_active": True,
            "legs": {"raw": {"configured": True}, "dtln": {"configured": False}},
            "threshold": 0.5,
        },
        "/aec/toggle": {"mode": "disabled", "bridge_active": True},
        "/aec/leg": {
            "mode": "auto",
            "bridge_active": True,
            "legs": {"raw": {"configured": False}, "dtln": {"configured": True}},
            "threshold": 0.5,
        },
        "/aec/threshold": {"threshold": 0.42},
    }

    class _UpHandler(BaseHTTPRequestHandler):
        def log_message(self, *a, **kw) -> None:
            pass

        def _reply(self, payload, status=200):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            received.append(("GET", self.path, None))
            payload = responses.get(self.path)
            if payload is None:
                self.send_error(404)
                return
            self._reply(payload)

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length) if length else b""
            try:
                parsed = json.loads(raw.decode()) if raw else None
            except (UnicodeDecodeError, json.JSONDecodeError):
                parsed = None
            received.append(("POST", self.path, parsed))
            payload = responses.get(self.path)
            if payload is None:
                self.send_error(404)
                return
            self._reply(payload)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _UpHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{srv.server_port}"
    try:
        yield base, received, responses
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=2)


@pytest.fixture
def wired_server(tmp_path: Path, fake_control):
    """The /wake/ server pointed at the fake jasper-control so layer
    + sensitivity POSTs flow through real handler logic and land
    against assertable upstream calls."""
    base, received, responses = fake_control
    state_path = str(tmp_path / "wake_model.env")
    server = wake_setup.make_server(
        ("127.0.0.1", 0), state_path=state_path, control_base=base,
    )
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", received, responses, state_path
    finally:
        server.shutdown()
        thread.join(timeout=2)


def _json_post_with_csrf(base_url: str, path: str, payload: dict):
    """POST JSON to `base_url + path` with the CSRF cookie + header set.
    Returns (status, parsed json body)."""
    import json as _json
    from ._web_test_helpers import make_csrf_session
    session = make_csrf_session(base_url, page_path="/")
    body = _json.dumps(payload).encode()
    req = urllib.request.Request(
        base_url + path, data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "X-CSRF-Token": session["token"],
        },
    )
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(session["jar"]),
    )
    try:
        with opener.open(req, timeout=5) as r:
            return r.status, _json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, _json.loads(e.read().decode())


def test_detection_json_proxies_aec(wired_server):
    base, received, _, _ = wired_server
    with urllib.request.urlopen(base + "/detection.json") as r:
        import json as _json
        payload = _json.loads(r.read().decode())
    assert payload["mode"] == "auto"
    assert payload["bridge_active"] is True
    assert ("GET", "/aec", None) in received


def test_layer_aec_no_op_when_already_in_state(wired_server):
    """Software AEC3 uses set-state semantics: setting `enabled=true` when
    the upstream is already in mode=auto must NOT call /aec/toggle
    (which would flip to disabled). The handler reads /aec first and
    short-circuits if the state already matches."""
    base, received, _, _ = wired_server
    status, body = _json_post_with_csrf(base, "/layer/aec", {"enabled": True})
    assert status == 200
    assert body["mode"] == "auto"
    posts = [r for r in received if r[0] == "POST"]
    assert posts == [], f"unexpected upstream POSTs: {posts}"


def test_layer_aec_toggles_when_state_differs(wired_server):
    """When the user asks for `enabled=false` and upstream reports
    mode=auto, the handler must POST /aec/toggle to flip the state."""
    base, received, _, _ = wired_server
    status, _ = _json_post_with_csrf(base, "/layer/aec", {"enabled": False})
    assert status == 200
    posts = [r for r in received if r[0] == "POST"]
    assert len(posts) == 1
    method, path, parsed = posts[0]
    assert path == "/aec/toggle"
    # /aec/toggle takes no body — the handler sent zero bytes upstream.
    assert parsed is None


def test_layer_raw_posts_aec_leg_with_body(wired_server):
    """Leg toggles rewrite to /aec/leg with `{leg, enabled}` so
    jasper-control's existing single-endpoint handler stays unchanged."""
    base, received, _, _ = wired_server
    status, body = _json_post_with_csrf(base, "/layer/raw", {"enabled": False})
    assert status == 200
    assert body["legs"]["raw"]["configured"] is False
    posts = [r for r in received if r[0] == "POST"]
    assert len(posts) == 1
    method, path, parsed = posts[0]
    assert path == "/aec/leg"
    assert parsed == {"leg": "raw", "enabled": False}


def test_layer_dtln_posts_aec_leg_with_body(wired_server):
    base, received, _, _ = wired_server
    _json_post_with_csrf(base, "/layer/dtln", {"enabled": True})
    posts = [r for r in received if r[0] == "POST"]
    assert any(
        path == "/aec/leg" and parsed == {"leg": "dtln", "enabled": True}
        for _, path, parsed in posts
    )


def test_layer_chip_aec_posts_aec_leg_with_body(wired_server):
    """The chip-AEC layer rewrites to /aec/leg with leg='chip_aec' — the
    single boolean the reconciler fans out to both fixed beams. Routing
    is identical to raw/dtln; jasper-control's handler stays unchanged."""
    base, received, _, _ = wired_server
    _json_post_with_csrf(base, "/layer/chip_aec", {"enabled": True})
    posts = [r for r in received if r[0] == "POST"]
    assert any(
        path == "/aec/leg" and parsed == {"leg": "chip_aec", "enabled": True}
        for _, path, parsed in posts
    )


def test_sensitivity_posts_aec_threshold(wired_server):
    """Wizard accepts `{value: 0.42}`; jasper-control wants
    `{threshold: 0.42}`. The proxy rewrites the body so the user-
    facing URL stops leaking AEC vocabulary."""
    base, received, _, _ = wired_server
    status, body = _json_post_with_csrf(base, "/sensitivity", {"value": 0.42})
    assert status == 200
    assert body["threshold"] == 0.42
    posts = [r for r in received if r[0] == "POST"]
    assert len(posts) == 1
    method, path, parsed = posts[0]
    assert path == "/aec/threshold"
    assert parsed == {"threshold": 0.42}


def test_layer_rejects_unknown_name(wired_server):
    base, _, _, _ = wired_server
    status, body = _json_post_with_csrf(base, "/layer/garbage", {"enabled": True})
    assert status == 400
    assert "unknown layer" in body["error"]


def test_layer_rejects_non_boolean_enabled(wired_server):
    base, _, _, _ = wired_server
    status, body = _json_post_with_csrf(base, "/layer/raw", {"enabled": "yes"})
    assert status == 400
    assert "boolean" in body["error"]


def test_sensitivity_rejects_non_numeric_value(wired_server):
    base, _, _, _ = wired_server
    status, body = _json_post_with_csrf(base, "/sensitivity", {"value": "loud"})
    assert status == 400
    assert "number" in body["error"]


def test_sensitivity_rejects_out_of_range(wired_server):
    base, _, _, _ = wired_server
    status, body = _json_post_with_csrf(base, "/sensitivity", {"value": 2.0})
    assert status == 400
    assert "between 0 and 1" in body["error"]


def test_layer_requires_csrf(wired_server):
    """JSON-bodied POSTs ride the X-CSRF-Token header; bare POSTs
    without a session must 403 before any upstream call is made."""
    base, received, _, _ = wired_server
    req = urllib.request.Request(
        base + "/layer/raw", data=b'{"enabled":false}', method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=2)
        status = 200
    except urllib.error.HTTPError as e:
        status = e.code
    assert status == 403
    # No upstream POST was issued.
    assert not [r for r in received if r[0] == "POST"]
