"""Tests for the voice-provider config wizard at /voice/.

The wizard's risky bits are:
  1. Save logic — what gets written, what gets dropped, when does the
     'active provider needs a key' guard kick in?
  2. Atomic env-file IO with mode-0600 (an API key is in there).
  3. Page render correctness — the right card opens, the right radio
     is checked, the right key prefix shows up masked.

Driven through the pure-function seams (`_apply_save`,
`_apply_clear`, `_index_html`, `_load_state`) plus a tempdir for
env-file IO. The HTTP handler itself is exercised end-to-end by
spinning up the actual ThreadingHTTPServer on a random port, hitting
it with `urllib`, and inspecting the responses — same shape as the
Spotify wizard would be tested if it had its own test file.
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

from jasper.web import _common, voice_setup


# ---------- Pure helpers (no IO) -------------------------------------------


def test_load_state_returns_empty_for_missing_file(tmp_path: Path):
    assert voice_setup._load_state(str(tmp_path / "nope.env")) == {}


def test_load_state_round_trips_through_write_env_file(tmp_path: Path):
    p = str(tmp_path / "v.env")
    _common.write_env_file(p, {"OPENAI_API_KEY": "sk-abc", "JASPER_VOICE_PROVIDER": "openai"})
    assert voice_setup._load_state(p) == {
        "OPENAI_API_KEY": "sk-abc",
        "JASPER_VOICE_PROVIDER": "openai",
    }


def test_write_env_file_uses_mode_0600(tmp_path: Path):
    p = tmp_path / "v.env"
    _common.write_env_file(str(p), {"X": "y"})
    # API keys live in here; a too-permissive file would leak under a
    # daemon-readable path.
    mode = os.stat(p).st_mode & 0o777
    assert mode == 0o600


def test_write_env_file_rejects_value_with_newline(tmp_path: Path):
    """systemd's EnvironmentFile parser doesn't quote/escape, so a
    newline in a value would silently truncate the variable. We catch
    it client-side rather than land a broken file."""
    p = str(tmp_path / "v.env")
    with pytest.raises(ValueError, match="newline"):
        _common.write_env_file(p, {"K": "abc\ndef"})


def test_write_env_file_is_atomic_under_failure(tmp_path: Path):
    """If a value is rejected mid-write, the previous file content
    must remain intact. The temp-file + rename pattern is what
    enforces this — verifying it explicitly so a future refactor
    that 'simplifies' to direct-write gets caught."""
    p = str(tmp_path / "v.env")
    _common.write_env_file(p, {"OK": "first"})
    with pytest.raises(ValueError):
        _common.write_env_file(p, {"OK": "second", "BAD": "no\nline"})
    # File still has the original good content.
    assert voice_setup._load_state(p) == {"OK": "first"}


# ---------- Save logic -----------------------------------------------------


def _form_for(active="openai", **kwargs) -> dict[str, str]:
    """Build a save form with sensible defaults. Keys omitted means
    'leave blank' — i.e. preserve the saved value."""
    f = {
        "active": active,
        # All three providers' model + voice always submit (dropdowns).
        "gemini_model": kwargs.pop("gemini_model", "gemini-3.1-flash-live-preview"),
        "gemini_voice": kwargs.pop("gemini_voice", "Aoede"),
        "openai_model": kwargs.pop("openai_model", "gpt-realtime-2"),
        "openai_voice": kwargs.pop("openai_voice", "marin"),
        "openai_reasoning_effort": kwargs.pop("openai_reasoning_effort", "low"),
        "grok_model": kwargs.pop("grok_model", "grok-voice-think-fast-1.0"),
        "grok_voice": kwargs.pop("grok_voice", "eve"),
        # Keys default blank (= no change).
        "gemini_key": "",
        "openai_key": "",
        "grok_key": "",
    }
    f.update(kwargs)
    return f


def test_apply_save_blank_key_field_preserves_existing_value():
    """Leaving the password field blank means 'don't touch'; the
    user shouldn't have to re-paste a key just to flip the active
    provider."""
    current = {"OPENAI_API_KEY": "sk-old", "JASPER_VOICE_PROVIDER": "openai"}
    new, err = voice_setup._apply_save(_form_for(active="openai"), current)
    assert err is None
    assert new["OPENAI_API_KEY"] == "sk-old"


def test_apply_save_non_empty_key_replaces():
    current = {"OPENAI_API_KEY": "sk-old", "JASPER_VOICE_PROVIDER": "openai"}
    form = _form_for(active="openai", openai_key="sk-new")
    new, err = voice_setup._apply_save(form, current)
    assert err is None
    assert new["OPENAI_API_KEY"] == "sk-new"


def test_apply_save_rejects_active_provider_with_no_key(monkeypatch):
    """The 'set active' button should not let the user shoot
    themselves in the foot. Pasting an OpenAI key elsewhere on the
    page must NOT activate Grok."""
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    current = {}
    form = _form_for(active="grok", openai_key="sk-new")
    new, err = voice_setup._apply_save(form, current)
    assert err is not None
    assert "Grok" in err
    # State is unchanged on error.
    assert new == current


def test_apply_save_active_provider_via_existing_env(monkeypatch):
    """If the operator set GEMINI_API_KEY in /etc/jasper/jasper.env,
    the wizard sees it via os.environ and should let the user pick
    Gemini as active even without a wizard-saved key."""
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-from-etc")
    current = {}
    form = _form_for(active="gemini")
    new, err = voice_setup._apply_save(form, current)
    assert err is None
    assert new["JASPER_VOICE_PROVIDER"] == "gemini"
    # Wizard didn't shadow the env key — it stays sourced from /etc.
    assert "GEMINI_API_KEY" not in new


def test_apply_save_rejects_unknown_provider():
    new, err = voice_setup._apply_save(_form_for(active="anthropic"), {})
    assert err is not None
    assert "anthropic" in err


def test_apply_save_strips_leading_trailing_whitespace_from_pasted_key():
    """Trailing-newline pastes are the most common way for a key to
    arrive looking bad. We strip them silently — the alternative is
    bouncing the user back to re-paste, which is annoying for what is
    fundamentally a copy-paste artifact."""
    new, err = voice_setup._apply_save(
        _form_for(active="openai", openai_key="  sk-good\n"), {},
    )
    assert err is None
    assert new["OPENAI_API_KEY"] == "sk-good"


def test_apply_save_rejects_key_with_embedded_whitespace():
    """Whitespace inside a key (not just at the edges) is suspicious
    enough that we'd rather refuse than persist a broken value. The
    user almost certainly copied a chunk of surrounding text."""
    new, err = voice_setup._apply_save(
        _form_for(active="openai", openai_key="sk-good plus extra"), {},
    )
    assert err is not None
    assert "whitespace" in err


def test_apply_save_writes_active_provider_into_state():
    new, err = voice_setup._apply_save(
        _form_for(active="openai", openai_key="sk-fresh"), {},
    )
    assert err is None
    assert new["JASPER_VOICE_PROVIDER"] == "openai"
    assert new["OPENAI_API_KEY"] == "sk-fresh"
    # Model and voice picked up from form.
    assert new["JASPER_OPENAI_MODEL"] == "gpt-realtime-2"
    assert new["JASPER_OPENAI_VOICE"] == "marin"
    assert new["JASPER_OPENAI_REASONING_EFFORT"] == "low"


def test_apply_save_drops_blank_values_to_keep_file_tidy():
    """Blank model/voice fields would litter the env file with
    K= entries that systemd would interpret as empty-string values
    rather than 'unset'. Drop them at write time."""
    form = _form_for(active="openai", openai_key="sk-x")
    form["openai_model"] = ""
    new, _ = voice_setup._apply_save(form, {})
    assert "JASPER_OPENAI_MODEL" not in new


# ---------- Clear logic ----------------------------------------------------


def test_apply_clear_removes_key_model_voice_for_one_provider():
    current = {
        "OPENAI_API_KEY": "sk-x",
        "JASPER_OPENAI_MODEL": "gpt-realtime-2",
        "JASPER_OPENAI_VOICE": "marin",
        "JASPER_OPENAI_REASONING_EFFORT": "low",
        "GEMINI_API_KEY": "AIza-y",
        "JASPER_VOICE_PROVIDER": "openai",
    }
    new, err = voice_setup._apply_clear({"provider": "openai"}, current)
    assert err is None
    # All openai-owned keys gone.
    for k in (
        "OPENAI_API_KEY", "JASPER_OPENAI_MODEL",
        "JASPER_OPENAI_VOICE", "JASPER_OPENAI_REASONING_EFFORT",
    ):
        assert k not in new
    # Other providers untouched.
    assert new["GEMINI_API_KEY"] == "AIza-y"
    # Active is preserved — the page render will surface that the
    # active provider is now broken so the user can fix it.
    assert new["JASPER_VOICE_PROVIDER"] == "openai"


def test_apply_clear_unknown_provider_errors():
    new, err = voice_setup._apply_clear({"provider": "anthropic"}, {"X": "y"})
    assert err is not None
    assert new == {"X": "y"}


# ---------- Page rendering -------------------------------------------------


def test_index_renders_active_radio_checked_for_active_provider():
    state = {"JASPER_VOICE_PROVIDER": "openai", "OPENAI_API_KEY": "sk-x"}
    page = voice_setup._index_html(state).decode()
    # The openai radio is checked.
    idx = page.index('value="openai"')
    nearby = page[idx - 200: idx + 200]
    assert "checked" in nearby
    # Title element is present so the page renders cleanly.
    assert "<title>Voice provider on this speaker</title>" in page


def test_index_disables_radio_for_unconfigured_provider(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    state = {"JASPER_VOICE_PROVIDER": "openai", "OPENAI_API_KEY": "sk-x"}
    page = voice_setup._index_html(state).decode()
    idx = page.index('value="grok"')
    nearby = page[idx - 200: idx + 200]
    assert "disabled" in nearby


def test_index_masks_existing_key_in_card():
    state = {"OPENAI_API_KEY": "sk-proj-abcdef-tail9999"}
    page = voice_setup._index_html(state).decode()
    # Full key should never appear in the rendered HTML.
    assert "sk-proj-abcdef-tail9999" not in page
    # Masked prefix should.
    assert "sk-p" in page  # first 4 chars
    assert "9999" in page  # last 4 chars


def test_index_active_card_starts_open():
    """The active provider's card opens by default so the user lands
    on what's in flight without an extra click."""
    state = {"JASPER_VOICE_PROVIDER": "openai", "OPENAI_API_KEY": "sk-x"}
    page = voice_setup._index_html(state).decode()
    # Find the openai card by its input name (only appears inside the
    # details body — distinct from the description's label text).
    idx = page.index('name="openai_key"')
    head = page.rfind('<details', 0, idx)
    assert head != -1
    open_section = page[head: head + 200]
    assert "open" in open_section


def test_index_save_form_does_not_enclose_cards():
    """HTML forbids nested forms. Each provider card includes a per-card
    "Clear key" form, so the outer save form MUST close before the cards
    begin. Regression test: in the live deploy, an earlier version of
    this page nested the clear forms inside the save form, which made
    every browser silently close the outer form when it parsed the
    inner <form> — and the "Save and restart voice" button at the
    bottom was no longer associated with anything, so pressing it did
    literally nothing. Pin the structural invariant here."""
    state = {"JASPER_VOICE_PROVIDER": "gemini", "GEMINI_API_KEY": "AIza-x"}
    page = voice_setup._index_html(state).decode()
    save_form_open = page.index('id="save-form"')
    save_form_close = page.index("</form>", save_form_open)
    first_card = page.index("<details", save_form_close)
    # The first card must come AFTER the save form has closed.
    assert save_form_close < first_card, (
        "outer save form must close before the cards begin "
        "(otherwise the per-card clear-key forms nest inside it)"
    )


def test_index_card_inputs_associate_with_save_form_via_attribute():
    """Cards are no longer DOM-nested in the save form (see prior test),
    so each input/select inside a card MUST carry the HTML5
    `form="save-form"` attribute to participate in the save POST.
    Without it, pasting a key and pressing Save sends a POST with that
    field absent — a silent no-op that gave us "Save doesn't do
    anything" in the live deploy."""
    state = {"JASPER_VOICE_PROVIDER": "gemini", "GEMINI_API_KEY": "AIza-x"}
    page = voice_setup._index_html(state).decode()
    # Every key input must opt into the save form.
    for pid in ("gemini", "openai", "grok"):
        anchor = f'name="{pid}_key"'
        idx = page.index(anchor)
        # Look in the same tag (between the previous '<' and the next '>').
        tag_start = page.rfind("<", 0, idx)
        tag_end = page.index(">", idx)
        tag = page[tag_start: tag_end + 1]
        assert 'form="save-form"' in tag, (
            f"{pid}_key input is missing form=\"save-form\" — "
            f"submission will silently drop this field. tag={tag!r}"
        )
    # Same check for model + voice selects.
    for pid in ("gemini", "openai", "grok"):
        for field in ("model", "voice"):
            anchor = f'name="{pid}_{field}"'
            idx = page.index(anchor)
            tag_start = page.rfind("<", 0, idx)
            tag_end = page.index(">", idx)
            tag = page[tag_start: tag_end + 1]
            assert 'form="save-form"' in tag, (
                f"{pid}_{field} select is missing form=\"save-form\""
            )


def test_index_save_button_associates_with_save_form_via_attribute():
    """The submit button at the bottom of the page sits OUTSIDE the
    <form>...</form> tags (so the outer form can close before the
    cards). It must carry form="save-form" to actually submit."""
    state = {}
    page = voice_setup._index_html(state).decode()
    idx = page.index("Save and restart voice")
    # Find the enclosing <button> tag.
    btn_start = page.rfind("<button", 0, idx)
    btn_end = page.index(">", btn_start)
    btn_tag = page[btn_start: btn_end + 1]
    assert 'form="save-form"' in btn_tag, (
        f"save button is missing form=\"save-form\" — "
        f"clicking it would do nothing. tag={btn_tag!r}"
    )


def test_index_unconfigured_card_starts_open_to_invite_paste(monkeypatch):
    """A card with no saved key opens by default so the user doesn't
    have to click to discover where to paste."""
    # Make sure environment doesn't supply keys (could carry from
    # /etc/jasper/jasper.env on a developer's machine).
    for k in ("GEMINI_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    state = {}
    page = voice_setup._index_html(state).decode()
    idx = page.index('name="gemini_key"')
    head = page.rfind('<details', 0, idx)
    assert head != -1
    assert "open" in page[head: head + 200]


# ---------- Mask helper ----------------------------------------------------


def test_mask_secret_short_value_fully_hidden():
    assert "abc" not in _common.mask_secret("abc")
    assert _common.mask_secret("") == ""


def test_mask_secret_shows_prefix_and_suffix_for_real_keys():
    masked = _common.mask_secret("sk-proj-abc1234567xyz")
    assert masked.startswith("sk-p")
    assert masked.endswith("7xyz")
    assert "abc12" not in masked


# ---------- End-to-end via the actual HTTP server --------------------------


def _start_server(tmp_path: Path) -> tuple[ThreadingHTTPServer, str, threading.Thread]:
    state_path = str(tmp_path / "voice_provider.env")
    server = voice_setup.make_server("127.0.0.1", 0, state_path=state_path)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{port}", thread


def _post(url: str, form: dict[str, str]) -> tuple[int, str, str]:
    """POST a urlencoded form. Don't follow redirects — the wizard's
    303-on-success carries the status banner in `?msg=` and we want
    to assert against it. Returns (status, location_header, body)."""
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    class _NoRedirect(urllib.request.HTTPErrorProcessor):
        def http_response(self, request, response):
            return response
        https_response = http_response

    opener = urllib.request.build_opener(_NoRedirect())
    try:
        resp = opener.open(req)
        body = resp.read().decode("utf-8", errors="replace")
        return resp.status, resp.headers.get("Location", ""), body
    except urllib.error.HTTPError as e:
        return e.status, e.headers.get("Location", ""), e.read().decode(errors="replace")


def test_e2e_save_writes_file_and_redirects(
    tmp_path: Path, monkeypatch
):
    """Round-trip test: POST /save with a real OpenAI key, expect a
    303 to ?msg=Saved..., and verify the env file landed at mode 0600
    with the right keys."""
    # Prevent the test from actually shelling out to systemctl.
    called = []
    monkeypatch.setattr(
        _common, "restart_voice_daemon", lambda: called.append(True),
    )
    # The voice_setup module imported the symbol directly; patch it
    # there too.
    monkeypatch.setattr(
        voice_setup, "restart_voice_daemon", lambda: called.append(True),
    )

    server, base, _ = _start_server(tmp_path)
    try:
        form = _form_for(active="openai", openai_key="sk-fresh")
        status, location, _ = _post(f"{base}/save", form)
        assert status == 303
        assert "Saved" in urllib.parse.unquote(location)
        # File landed.
        state_path = tmp_path / "voice_provider.env"
        assert state_path.exists()
        assert (os.stat(state_path).st_mode & 0o777) == 0o600
        loaded = voice_setup._load_state(str(state_path))
        assert loaded["OPENAI_API_KEY"] == "sk-fresh"
        assert loaded["JASPER_VOICE_PROVIDER"] == "openai"
        # Restart was invoked.
        assert called == [True]
    finally:
        server.shutdown()
        server.server_close()


def test_e2e_save_rejects_active_without_key(tmp_path: Path, monkeypatch):
    """Server-side enforcement of the 'no key, no activate' rule.
    The radio is disabled in the UI, but a hand-crafted POST should
    still be rejected."""
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.setattr(voice_setup, "restart_voice_daemon", lambda: None)
    server, base, _ = _start_server(tmp_path)
    try:
        form = _form_for(active="grok")
        status, location, _ = _post(f"{base}/save", form)
        assert status == 303
        assert "Grok" in urllib.parse.unquote(location)
        assert "no API key" in urllib.parse.unquote(location)
        # File was not touched.
        assert not (tmp_path / "voice_provider.env").exists()
    finally:
        server.shutdown()
        server.server_close()


def test_e2e_get_index_renders_state(tmp_path: Path, monkeypatch):
    """Load the page from a populated state file. Confirms the GET
    path threads state through to the renderer."""
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    state_path = tmp_path / "voice_provider.env"
    _common.write_env_file(str(state_path), {
        "JASPER_VOICE_PROVIDER": "openai",
        "OPENAI_API_KEY": "sk-existing-12345abc",
        "JASPER_OPENAI_MODEL": "gpt-realtime-2",
    })
    server, base, _ = _start_server(tmp_path)
    try:
        body = urllib.request.urlopen(f"{base}/").read().decode()
        # Active radio reflects the saved state.
        idx = body.index('value="openai"')
        nearby = body[idx: idx + 200]
        assert "checked" in nearby
        # Mask shows up (prefix + suffix) but raw key does NOT.
        assert "sk-existing-12345abc" not in body
        assert "sk-e" in body and "5abc" in body
    finally:
        server.shutdown()
        server.server_close()


def test_e2e_clear_credentials_removes_provider_keys(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(voice_setup, "restart_voice_daemon", lambda: None)
    state_path = tmp_path / "voice_provider.env"
    _common.write_env_file(str(state_path), {
        "JASPER_VOICE_PROVIDER": "gemini",
        "GEMINI_API_KEY": "AIza-keep",
        "OPENAI_API_KEY": "sk-clear",
        "JASPER_OPENAI_MODEL": "gpt-realtime-2",
    })
    server, base, _ = _start_server(tmp_path)
    try:
        status, location, _ = _post(
            f"{base}/clear-credentials", {"provider": "openai"},
        )
        assert status == 303
        assert "Cleared" in urllib.parse.unquote(location)
        loaded = voice_setup._load_state(str(state_path))
        assert "OPENAI_API_KEY" not in loaded
        assert "JASPER_OPENAI_MODEL" not in loaded
        assert loaded["GEMINI_API_KEY"] == "AIza-keep"
    finally:
        server.shutdown()
        server.server_close()
