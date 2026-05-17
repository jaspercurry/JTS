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


def test_parse_threshold_blank_returns_none():
    """No `threshold` field in the form is fine — it means "don't
    change the threshold", not "set threshold to 0"."""
    val, err = wake_setup._parse_threshold("")
    assert val is None and err is None


def test_parse_threshold_valid_midpoint():
    val, err = wake_setup._parse_threshold("0.5")
    assert err is None
    assert val == 0.5


def test_parse_threshold_rejects_non_numeric():
    val, err = wake_setup._parse_threshold("very sensitive")
    assert err is not None and "number" in err.lower()
    assert val is None


def test_parse_threshold_rejects_out_of_range():
    """Daemon validator only accepts 0.0..1.0; the wizard must reject
    the same range with a friendly message instead of silently
    persisting a value that crashes jasper-voice on its next restart."""
    for raw in ("-0.1", "1.1", "5", "-1"):
        val, err = wake_setup._parse_threshold(raw)
        assert err is not None, f"expected error for {raw!r}, got val={val}"
        assert "between 0.0 and 1.0" in err
        assert val is None


def test_apply_save_persists_threshold_alongside_model():
    new, err = wake_setup._apply_save(
        {"model": "hey_jarvis", "threshold": "0.35"}, current={},
    )
    assert err is None
    assert new == {
        "JASPER_WAKE_MODEL": "hey_jarvis",
        "JASPER_WAKE_THRESHOLD": "0.35",
    }


def test_apply_save_threshold_only_keeps_existing_custom_model():
    """User has a hand-rolled custom .onnx active and wants to tune
    sensitivity without giving it up. The custom row's radio is
    `disabled` so no `model` value is submitted; the save must keep
    the existing JASPER_WAKE_MODEL untouched."""
    current = {"JASPER_WAKE_MODEL": "/home/pi/hand-rolled/custom.onnx"}
    new, err = wake_setup._apply_save({"threshold": "0.25"}, current=current)
    assert err is None
    assert new == {
        "JASPER_WAKE_MODEL": "/home/pi/hand-rolled/custom.onnx",
        "JASPER_WAKE_THRESHOLD": "0.25",
    }


def test_apply_save_rejects_invalid_threshold_before_touching_model():
    """Threshold validation runs first, so a bad threshold doesn't
    half-apply a model change."""
    new, err = wake_setup._apply_save(
        {"model": "hey_jarvis", "threshold": "2.0"}, current={"existing": "x"},
    )
    assert err is not None
    assert new == {"existing": "x"}


def test_apply_save_normalises_threshold_to_two_decimal_places():
    """Browsers can ship `value="0.5000000001"` after float-roundtrip;
    we normalise to the same 0.05 step granularity the slider uses so
    the env file stays clean and diffable."""
    new, err = wake_setup._apply_save(
        {"model": "hey_jarvis", "threshold": "0.5000000001"}, current={},
    )
    assert err is None
    assert new["JASPER_WAKE_THRESHOLD"] == "0.50"


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


def test_index_html_renders_sensitivity_card_at_default(monkeypatch):
    monkeypatch.delenv("JASPER_WAKE_THRESHOLD", raising=False)
    html = wake_setup._index_html({}).decode()
    assert "Wake word sensitivity" in html
    assert 'name="threshold"' in html
    assert 'type="range"' in html
    # Slider's current value matches the compiled default.
    assert f'value="{wake_setup.DEFAULT_WAKE_THRESHOLD:.2f}"' in html
    # Readout pre-fills with the same number so the page is consistent
    # before any user input lands.
    assert (
        f'id="threshold-readout">{wake_setup.DEFAULT_WAKE_THRESHOLD:.2f}'
        in html
    )


def test_index_html_renders_sensitivity_card_at_persisted_value(monkeypatch):
    monkeypatch.delenv("JASPER_WAKE_THRESHOLD", raising=False)
    html = wake_setup._index_html(
        {"JASPER_WAKE_THRESHOLD": "0.30"},
    ).decode()
    assert 'value="0.30"' in html
    assert 'id="threshold-readout">0.30' in html


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
    base, state_path = running_server
    # Stub the systemctl shellout — we don't want a unit test to
    # actually try to restart jasper-voice.
    called = []
    monkeypatch.setattr(
        wake_setup, "restart_voice_daemon",
        lambda: called.append("restart"),
    )
    data = urllib.parse.urlencode({"model": "alexa"}).encode()
    req = urllib.request.Request(
        base + "/save", data=data, method="POST",
    )
    # 303 redirect after save — disable redirect handling so urllib
    # doesn't follow to ./ (which would try to GET the redirect target).
    opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler)
    opener.open  # touch attribute to avoid unused-var lint
    try:
        urllib.request.urlopen(req)
    except urllib.error.HTTPError as e:
        # urllib raises on 3xx by default for non-GET-following clients;
        # treat as success if it's the expected 303 See Other.
        assert e.code == 303, f"unexpected status: {e.code}"
    # Wizard wrote the env file at mode 0644 (path-only, no secret).
    assert os.path.exists(state_path)
    assert os.stat(state_path).st_mode & 0o777 == 0o644
    assert wake_setup._load_state(state_path) == {
        "JASPER_WAKE_MODEL": "alexa",
    }
    assert called == ["restart"]


def test_http_post_save_persists_threshold(running_server, monkeypatch):
    """Submitting both model and threshold writes both env vars to
    the same file in one save+restart cycle."""
    base, state_path = running_server
    called = []
    monkeypatch.setattr(
        wake_setup, "restart_voice_daemon",
        lambda: called.append("restart"),
    )
    data = urllib.parse.urlencode({
        "model": "alexa", "threshold": "0.65",
    }).encode()
    req = urllib.request.Request(base + "/save", data=data, method="POST")
    try:
        urllib.request.urlopen(req)
    except urllib.error.HTTPError as e:
        assert e.code == 303, f"unexpected status: {e.code}"
    assert wake_setup._load_state(state_path) == {
        "JASPER_WAKE_MODEL": "alexa",
        "JASPER_WAKE_THRESHOLD": "0.65",
    }
    assert called == ["restart"]
