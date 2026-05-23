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
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from jasper import wake_models
from jasper.web import _common, wake_setup


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
    """Bundled openWakeWord models are the always-available fallback.
    Without one, an offline install (no internet at deploy time)
    would have no working wake model — the daemon would crash."""
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
    own packaged bundle. A path here would either crash on load or
    point at a file that doesn't exist on the Pi."""
    for entry in wake_models.REGISTRY:
        if entry.bundled:
            assert "/" not in entry.model
            assert not entry.model.endswith(".onnx")
            assert entry.download_url is None


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
    "hey_jarvis" is always available because it's openWakeWord-
    bundled, so this fallback never produces a broken daemon."""
    monkeypatch.delenv("JASPER_WAKE_MODEL", raising=False)
    assert wake_setup._active_model({}) == "hey_jarvis"


def test_is_available_for_bundled():
    """Bundled openWakeWord names report available even when the
    underlying .onnx hasn't been auto-downloaded yet — the package
    handles that lazily."""
    entry = wake_models.by_key("hey_jarvis")
    assert entry is not None
    assert wake_setup._is_available(entry) is True


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


def test_apply_save_writes_registered_bundled_model():
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
# removed when the sensitivity slider moved to /system/'s Wake
# detection card (jasper/web/system_setup.py + jasper/control/server.py
# POST /aec/threshold). _active_threshold stays — it's a clean
# "read what the daemon will load" helper independent of the UI
# location.
#
# Threshold-preservation across a /wake/ model save is now covered
# by test_apply_save_preserves_threshold_in_state below; the daemon-
# facing JASPER_WAKE_THRESHOLD validation is in jasper/config.py and
# in jasper.control.server._write_wake_threshold.


def test_apply_save_preserves_threshold_in_state():
    """The sensitivity slider lives on /system/ but writes the same
    wake_model.env file. A save from /wake/ (model-only form) must
    preserve any JASPER_WAKE_THRESHOLD already in the state dict —
    otherwise saving a new model would silently zap the slider's
    value."""
    current = {
        "JASPER_WAKE_MODEL": "hey_jarvis",
        "JASPER_WAKE_THRESHOLD": "0.35",
    }
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


def test_index_html_omits_sensitivity_slider():
    """The slider moved to /system/'s Wake detection card. The /wake/
    page should no longer render a `type="range"` input — it carries
    a moved-link panel instead so users discover where it went."""
    html = wake_setup._index_html({}).decode()
    assert 'type="range"' not in html
    assert 'name="threshold"' not in html
    assert "Wake word sensitivity" not in html


def test_index_html_links_to_system_for_sensitivity():
    """The /wake/ page surfaces a small panel pointing at /system/'s
    Wake detection card so users tuning sensitivity find the new
    home rather than wondering where the slider went."""
    html = wake_setup._index_html({}).decode()
    assert '/system/' in html
    assert 'sensitivity' in html.lower()


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
    """The slider moved to /system/, but it writes the same file. A
    /wake/ model-save must preserve any JASPER_WAKE_THRESHOLD the
    slider previously wrote — otherwise picking a new model would
    silently zap the user's sensitivity setting."""
    from ._web_test_helpers import post_with_csrf
    base, state_path = running_server
    called = []
    monkeypatch.setattr(
        wake_setup, "restart_voice_daemon",
        lambda: called.append("restart"),
    )
    # Seed wake_model.env with a hand-set threshold (as if /system/
    # had previously written it).
    with open(state_path, "w") as f:
        f.write("JASPER_WAKE_THRESHOLD=0.42\nJASPER_WAKE_MODEL=jarvis_v2\n")
    post_with_csrf(base, "/save", {"model": "alexa"})
    state = wake_setup._load_state(state_path)
    assert state["JASPER_WAKE_MODEL"] == "alexa"
    assert state["JASPER_WAKE_THRESHOLD"] == "0.42"
    assert called == ["restart"]
