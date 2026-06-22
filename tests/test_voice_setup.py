# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

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
import sqlite3
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from jasper.voice import catalog
from jasper.voice import model_discovery
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
        "gemini_model": kwargs.pop(
            "gemini_model", catalog.default_model_id("gemini"),
        ),
        "gemini_voice": kwargs.pop(
            "gemini_voice", catalog.default_voice_id("gemini"),
        ),
        "openai_model": kwargs.pop(
            "openai_model", catalog.default_model_id("openai"),
        ),
        "openai_voice": kwargs.pop(
            "openai_voice", catalog.default_voice_id("openai"),
        ),
        "openai_reasoning_effort": kwargs.pop(
            "openai_reasoning_effort",
            catalog.default_extra_value("openai", "reasoning_effort"),
        ),
        "grok_model": kwargs.pop(
            "grok_model", catalog.default_model_id("grok"),
        ),
        "grok_voice": kwargs.pop(
            "grok_voice", catalog.default_voice_id("grok"),
        ),
        # Keys default blank (= no change).
        "gemini_key": "",
        "openai_key": "",
        "grok_key": "",
    }
    f.update(kwargs)
    return f


def test_catalog_defaults_are_listed_and_marked_tested():
    """The wizard defaults should be conscious, audited catalog entries.

    The catalog is still not an allow-list, but the built-in defaults
    should not drift into an unlabelled or fallback-only state.
    """
    defaults = _form_for()
    for provider in catalog.PROVIDERS:
        model_default = defaults[f"{provider.id}_model"]
        voice_default = defaults[f"{provider.id}_voice"]
        model = next((m for m in provider.models if m.default), None)
        assert model is not None, f"{provider.id} model default missing"
        assert model.id == model_default
        assert model.status is catalog.ModelStatus.TESTED
        voice = next((v for v in provider.voices if v.default), None)
        assert voice is not None, f"{provider.id} voice default missing"
        assert voice.id == voice_default


def test_provider_ids_manifest_is_shell_readable_catalog_projection():
    lines = catalog.provider_ids_manifest_text().splitlines()

    assert lines == sorted(catalog.VALID_PROVIDER_IDS)
    assert "" not in lines
    assert all("=" not in line and line.strip() == line for line in lines)


def test_index_model_options_show_catalog_statuses():
    page = voice_setup._index_html(
        {},
        "csrf-token-for-test-" + "x" * 32,
    ).decode()
    assert "3.1 Flash Live preview (tested; default)" in page
    assert (
        "2.5 Flash native-audio preview "
        "(fallback; silent-session recovery)"
    ) in page
    assert "gpt-realtime-mini (fallback; lower cost, no reasoning)" in page


def test_index_selects_catalog_defaults_when_unset():
    page = voice_setup._index_html(
        {},
        "csrf-token-for-test-" + "x" * 32,
    ).decode()
    for provider in catalog.PROVIDERS:
        for field, default in (
            ("model", catalog.default_model_id(provider.id)),
            ("voice", catalog.default_voice_id(provider.id)),
        ):
            select_idx = page.index(f'name="{provider.id}_{field}"')
            value_idx = page.index(f'value="{default}"', select_idx)
            option = page[
                page.rfind("<option", 0, value_idx): page.index(">", value_idx)
            ]
            assert "selected" in option


def test_index_preserves_unknown_model_as_custom_experimental():
    state = {
        "JASPER_VOICE_PROVIDER": "openai",
        "OPENAI_API_KEY": "sk-x",
        "JASPER_OPENAI_MODEL": "gpt-realtime-new-live",
    }
    page = voice_setup._index_html(
        state,
        "csrf-token-for-test-" + "x" * 32,
    ).decode()
    idx = page.index('value="gpt-realtime-new-live"')
    option = page[page.rfind("<option", 0, idx): page.index("</option>", idx)]
    assert "selected" in option
    assert "gpt-realtime-new-live (custom; experimental)" in option


def test_index_merges_discovered_models_as_experimental_options():
    state = {
        "JASPER_VOICE_PROVIDER": "openai",
        "OPENAI_API_KEY": "sk-x",
        "JASPER_OPENAI_MODEL": "gpt-realtime-new-live",
    }
    page = voice_setup._index_html(
        state,
        "csrf-token-for-test-" + "x" * 32,
        discovery={
            "openai": model_discovery.DiscoverySnapshot(
                provider_id="openai",
                fetched_at="2026-05-27T10:00:00Z",
                models=("gpt-realtime-2", "gpt-realtime-new-live"),
            ),
        },
    ).decode()
    idx = page.index('value="gpt-realtime-new-live"')
    option = page[page.rfind("<option", 0, idx): page.index("</option>", idx)]
    assert "selected" in option
    assert "gpt-realtime-new-live (experimental; discovered)" in option
    assert "custom; experimental" not in option
    assert "Last refreshed 2026-05-27T10:00:00Z" in page


def test_index_renders_manual_refresh_button_without_page_load_fetch():
    page = voice_setup._index_html(
        {"OPENAI_API_KEY": "sk-x"},
        "csrf-token-for-test-" + "x" * 32,
    ).decode()
    assert 'action="refresh-models"' in page
    assert "Refresh available models" in page
    assert "Refresh is manual" in page


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


def test_apply_save_keeps_unknown_model_value():
    """A newly released model can be submitted before the curated
    catalog knows about it. Saving must persist that explicit choice
    rather than collapsing back to the default."""
    form = _form_for(
        active="openai",
        openai_key="sk-x",
        openai_model="gpt-realtime-new-live",
    )
    new, err = voice_setup._apply_save(form, {})
    assert err is None
    assert new["JASPER_OPENAI_MODEL"] == "gpt-realtime-new-live"


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


# ---------- Spend cap ------------------------------------------------------


def _usage_db_with_cost(tmp_path: Path, cost_usd: float) -> Path:
    db = tmp_path / "usage.db"
    voice_setup.UsageStore(str(db))
    con = sqlite3.connect(db)
    now = datetime.now(timezone.utc).isoformat()
    con.execute(
        "INSERT INTO sessions "
        "(started_at, ended_at, input_tokens, output_tokens, cost_usd, provider) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (now, now, 1000, 200, cost_usd, "openai"),
    )
    con.commit()
    con.close()
    return db


def test_read_spend_cap_status_uses_rolling_spend_and_multiplier(tmp_path: Path):
    db = _usage_db_with_cost(tmp_path, 0.81)
    status = voice_setup._read_spend_cap_status({
        "JASPER_USAGE_DB": str(db),
        "JASPER_DAILY_SPEND_CAP_USD": "1.00",
        "JASPER_DAILY_SPEND_CAP_SAFETY_MULTIPLIER": "1.25",
    })

    assert status["usage_available"] is True
    assert status["spend_last_24h_usd"] == pytest.approx(0.81)
    assert status["padded_spend_usd"] == pytest.approx(1.0125)
    assert status["allowed"] is False
    assert status["remaining_usd"] == 0


def test_index_renders_spend_cap_status_and_save_form(tmp_path: Path):
    db = _usage_db_with_cost(tmp_path, 0.25)
    page = voice_setup._index_html(
        {"JASPER_USAGE_DB": str(db), "JASPER_DAILY_SPEND_CAP_USD": "2.00"},
        "csrf-token-for-test-" + "x" * 32,
    ).decode()

    assert "Voice spend cap" in page
    assert 'action="spend-cap"' in page
    assert 'name="daily_spend_cap_usd"' in page
    assert "Rolling 24h spend" in page
    assert "$0.2500" in page


def test_apply_spend_cap_writes_env_keys_and_preserves_provider_state():
    current = {"JASPER_VOICE_PROVIDER": "openai", "OPENAI_API_KEY": "sk-x"}
    new, err = voice_setup._apply_spend_cap({
        "daily_spend_cap_usd": "5",
        "daily_spend_cap_safety_multiplier": "1.1",
    }, current)

    assert err is None
    assert new["JASPER_VOICE_PROVIDER"] == "openai"
    assert new["OPENAI_API_KEY"] == "sk-x"
    assert new["JASPER_DAILY_SPEND_CAP_USD"] == "5.00"
    assert new["JASPER_DAILY_SPEND_CAP_SAFETY_MULTIPLIER"] == "1.1"


def test_apply_spend_cap_rejects_negative_or_weak_multiplier():
    current = {"JASPER_VOICE_PROVIDER": "openai"}
    new, err = voice_setup._apply_spend_cap({
        "daily_spend_cap_usd": "-1",
        "daily_spend_cap_safety_multiplier": "1.25",
    }, current)
    assert err is not None
    assert new == current

    new, err = voice_setup._apply_spend_cap({
        "daily_spend_cap_usd": "1",
        "daily_spend_cap_safety_multiplier": "0.5",
    }, current)
    assert err is not None
    assert new == current


# ---------- Page rendering -------------------------------------------------


def test_index_renders_active_radio_checked_for_active_provider():
    state = {"JASPER_VOICE_PROVIDER": "openai", "OPENAI_API_KEY": "sk-x"}
    page = voice_setup._index_html(state, "csrf-token-for-test-" + "x" * 32).decode()
    # The openai radio is checked.
    idx = page.index('value="openai"')
    nearby = page[idx - 200: idx + 200]
    assert "checked" in nearby
    # Title element is present so the page renders cleanly.
    assert "<title>Voice provider</title>" in page


def test_index_disables_radio_for_unconfigured_provider(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    state = {"JASPER_VOICE_PROVIDER": "openai", "OPENAI_API_KEY": "sk-x"}
    page = voice_setup._index_html(state, "csrf-token-for-test-" + "x" * 32).decode()
    # The grok radio input carries the `disabled` attribute, and its row is
    # marked is-disabled (canonical dimmed/dashed styling) + aria-disabled.
    idx = page.index('name="active" value="grok"')
    tag_start = page.rfind("<input", 0, idx)
    tag_end = page.index(">", idx)
    tag = page[tag_start: tag_end + 1]
    assert "disabled" in tag
    row_start = page.rfind("<label", 0, idx)
    assert "provider-radio is-disabled" in page[row_start:idx]


def test_index_masks_existing_key_in_card():
    state = {"OPENAI_API_KEY": "sk-proj-abcdef-tail9999"}
    page = voice_setup._index_html(state, "csrf-token-for-test-" + "x" * 32).decode()
    # Full key should never appear in the rendered HTML.
    assert "sk-proj-abcdef-tail9999" not in page
    # Masked prefix should.
    assert "sk-p" in page  # first 4 chars
    assert "9999" in page  # last 4 chars


def test_index_active_card_renders_controls_and_active_badge():
    """On the canonical design every provider card is an always-open flat
    .info-card (no collapse), so the active provider's controls are visible
    without a click. The active provider's card carries the 'active' badge."""
    state = {"JASPER_VOICE_PROVIDER": "openai", "OPENAI_API_KEY": "sk-x"}
    page = voice_setup._index_html(state, "csrf-token-for-test-" + "x" * 32).decode()
    # Find the openai card by its key input, then walk up to the card root.
    idx = page.index('name="openai_key"')
    head = page.rfind('class="info-card provider-card"', 0, idx)
    assert head != -1
    card = page[head: page.index('name="openai_key"', head) + 300]
    # The active provider's badge sits in the card head.
    head_block = page[head: idx]
    assert "active</span>" in head_block
    # The key input + model select are present (controls are visible).
    assert 'name="openai_key"' in card


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
    page = voice_setup._index_html(state, "csrf-token-for-test-" + "x" * 32).decode()
    save_form_open = page.index('id="save-form"')
    save_form_close = page.index("</form>", save_form_open)
    first_card = page.index('class="info-card provider-card"', save_form_close)
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
    page = voice_setup._index_html(state, "csrf-token-for-test-" + "x" * 32).decode()
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
    page = voice_setup._index_html(state, "csrf-token-for-test-" + "x" * 32).decode()
    idx = page.index("Save and restart voice")
    # Find the enclosing <button> tag.
    btn_start = page.rfind("<button", 0, idx)
    btn_end = page.index(">", btn_start)
    btn_tag = page[btn_start: btn_end + 1]
    assert 'form="save-form"' in btn_tag, (
        f"save button is missing form=\"save-form\" — "
        f"clicking it would do nothing. tag={btn_tag!r}"
    )


def test_index_save_and_test_button_posts_to_bounded_test_route():
    """The explicit provider-call path must be an operator action, not a
    page-load side effect or an implicit normal save."""
    page = voice_setup._index_html({}, "csrf-token-for-test-" + "x" * 32).decode()
    idx = page.index("Save and Test")
    btn_start = page.rfind("<button", 0, idx)
    btn_end = page.index(">", btn_start)
    btn_tag = page[btn_start: btn_end + 1]
    assert 'form="save-form"' in btn_tag
    assert 'formaction="save-test"' in btn_tag


def test_index_unconfigured_card_shows_paste_field(monkeypatch):
    """A card with no saved key still renders its paste field directly (the
    canonical cards are always-open flat .info-cards), so the user doesn't
    have to click to discover where to paste. The card shows the
    'not configured' badge and the empty key input."""
    # Make sure environment doesn't supply keys (could carry from
    # /etc/jasper/jasper.env on a developer's machine).
    for k in ("GEMINI_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    # Also neutralize the active-provider selection. CI sets
    # JASPER_VOICE_PROVIDER=gemini ambiently (.github/workflows/tests.yml),
    # which makes the gemini card render the "active" badge — that wins over
    # the key state (_provider_card_html: is_active before configured), so
    # "not configured" never appears even though the card still shows its
    # paste field. This test asserts the inactive-AND-unconfigured rendering,
    # so clear it (passes-local-fails-CI otherwise).
    monkeypatch.delenv("JASPER_VOICE_PROVIDER", raising=False)
    state = {}
    page = voice_setup._index_html(state, "csrf-token-for-test-" + "x" * 32).decode()
    idx = page.index('name="gemini_key"')
    head = page.rfind('class="info-card provider-card"', 0, idx)
    assert head != -1
    assert "not configured</span>" in page[head: idx]


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


def _start_server(
    tmp_path: Path,
    *,
    discovery_http_client=None,
    loudness_seed_fn=None,
) -> tuple[ThreadingHTTPServer, str, threading.Thread]:
    state_path = str(tmp_path / "voice_provider.env")
    server = voice_setup.make_server(
        ("127.0.0.1", 0),
        state_path=state_path,
        # WS1 Phase 4a — point the split-out keys file at the tempdir too, so the
        # e2e save/clear paths never touch the real /var/lib/jasper-secrets.
        keys_path=str(tmp_path / "voice_keys.env"),
        discovery_cache_path=str(tmp_path / "voice_model_discovery.json"),
        discovery_http_client=discovery_http_client,
        pricing_path=str(tmp_path / "pricing.json"),
        assistant_loudness_profile_path=str(
            tmp_path / "assistant_loudness_profiles.json",
        ),
        loudness_seed_fn=loudness_seed_fn,
    )
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{port}", thread


def _post(url: str, form: dict[str, str]) -> tuple[int, str, str]:
    """POST a urlencoded form. Don't follow redirects — the wizard's
    303-on-success now carries the flash text in a cookie (was `?msg=`
    on the redirect URL before T1.1) so the Location header itself is
    clean; assertions on this helper's return now treat `location` as
    just the redirect target. Returns (status, location_header, body).

    Mints the CSRF cookie via a GET to the wizard root first so the
    POST passes guard_mutating_request."""
    import http.cookiejar
    from ._web_test_helpers import CSRF_COOKIE_NAME, CSRF_FORM_FIELD

    # Strip the path back to "/" on the same host to find the wizard root
    # (e.g. base/save → base/). Mint the CSRF cookie via GET.
    parsed = urllib.parse.urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    jar = http.cookiejar.CookieJar()
    cookie_opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar),
    )
    cookie_opener.open(base + "/").read()
    token = ""
    for cookie in jar:
        if cookie.name == CSRF_COOKIE_NAME:
            token = cookie.value
            break
    assert token, "wizard GET / did not set csrf cookie"

    payload = dict(form)
    payload[CSRF_FORM_FIELD] = token
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    class _NoRedirect(urllib.request.HTTPErrorProcessor):
        def http_response(self, request, response):
            return response
        https_response = http_response

    opener = urllib.request.build_opener(
        _NoRedirect(),
        urllib.request.HTTPCookieProcessor(jar),
    )

    def _extract_flash(jar: http.cookiejar.CookieJar) -> str:
        # `Set-Cookie: jts_flash=…` lands in the jar; decode and combine
        # into the location string so tests that did
        # `"Saved" in location` keep working without per-test edits.
        for cookie in jar:
            if cookie.name == "jts_flash":
                return urllib.parse.unquote(cookie.value or "")
        return ""

    try:
        resp = opener.open(req)
        body = resp.read().decode("utf-8", errors="replace")
        flash = _extract_flash(jar)
        loc = resp.headers.get("Location", "")
        # Preserve the old "Saved in location" contract: append the flash
        # text to the location string so legacy tests stay readable.
        if flash:
            loc = f"{loc}#{flash}"
        return resp.status, loc, body
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        flash = _extract_flash(jar)
        loc = e.headers.get("Location", "")
        if flash:
            loc = f"{loc}#{flash}"
        return e.status, loc, body


def test_e2e_save_writes_file_and_redirects(
    tmp_path: Path, monkeypatch
):
    """Round-trip test: POST /save with a real OpenAI key, expect a
    303 to ?msg=Saved..., and verify the env file landed at mode 0640
    (group jasper — WS1 Phase 3b-2, so the non-root jasper-control's spawned
    jasper-doctor can read it) with the right keys."""
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
        assert (os.stat(state_path).st_mode & 0o777) == 0o640
        loaded = voice_setup._load_state(str(state_path))
        assert loaded["JASPER_VOICE_PROVIDER"] == "openai"
        # WS1 Phase 4a — the API key is SPLIT OUT into voice_keys.env; the broad
        # voice_provider.env must NOT carry it.
        assert "OPENAI_API_KEY" not in loaded
        keys = voice_setup._load_state(str(tmp_path / "voice_keys.env"))
        assert keys["OPENAI_API_KEY"] == "sk-fresh"
        # Restart was invoked.
        assert called == [True]
    finally:
        server.shutdown()
        server.server_close()


def test_e2e_spend_cap_save_writes_voice_env_and_restarts(
    tmp_path: Path, monkeypatch,
):
    called = []
    monkeypatch.setattr(
        voice_setup, "restart_voice_daemon", lambda: called.append(True),
    )
    state_path = tmp_path / "voice_provider.env"
    _common.write_env_file(str(state_path), {
        "JASPER_VOICE_PROVIDER": "openai",
        "OPENAI_API_KEY": "sk-keep",
    })
    server, base, _ = _start_server(tmp_path)
    try:
        status, location, _ = _post(f"{base}/spend-cap", {
            "daily_spend_cap_usd": "5",
            "daily_spend_cap_safety_multiplier": "1.1",
        })
        assert status == 303
        assert "Saved spend cap" in urllib.parse.unquote(location)
        loaded = voice_setup._load_state(str(state_path))
        # WS1 Phase 4a — the key is preserved across a spend-cap save, but lives
        # in the split-out keys file, not the broad voice_provider.env.
        assert "OPENAI_API_KEY" not in loaded
        keys = voice_setup._load_state(str(tmp_path / "voice_keys.env"))
        assert keys["OPENAI_API_KEY"] == "sk-keep"
        assert loaded["JASPER_DAILY_SPEND_CAP_USD"] == "5.00"
        assert loaded["JASPER_DAILY_SPEND_CAP_SAFETY_MULTIPLIER"] == "1.1"
        assert called == [True]
    finally:
        server.shutdown()
        server.server_close()


def test_e2e_refresh_models_writes_cache_without_restarting_voice(
    tmp_path: Path, monkeypatch
):
    called = []
    monkeypatch.setattr(
        voice_setup, "restart_voice_daemon", lambda: called.append(True),
    )
    state_path = tmp_path / "voice_provider.env"
    _common.write_env_file(str(state_path), {
        "JASPER_VOICE_PROVIDER": "openai",
        "OPENAI_API_KEY": "sk-existing",
    })

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.openai.com/v1/models"
        assert request.headers["Authorization"] == "Bearer sk-existing"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "gpt-5.2"},
                    {"id": "gpt-realtime-2"},
                    {"id": "gpt-realtime-new-live"},
                ],
            },
        )

    http = httpx.Client(transport=httpx.MockTransport(handler))
    server, base, _ = _start_server(tmp_path, discovery_http_client=http)
    try:
        status, location, _ = _post(
            f"{base}/refresh-models", {"provider": "openai"},
        )
        assert status == 303
        assert "Refreshed" in urllib.parse.unquote(location)
        assert called == []

        cache_path = tmp_path / "voice_model_discovery.json"
        assert cache_path.exists()
        cached = model_discovery.load_cache(str(cache_path))["openai"]
        assert cached.models == ("gpt-realtime-2", "gpt-realtime-new-live")

        body = urllib.request.urlopen(f"{base}/").read().decode()
        assert "gpt-realtime-new-live (experimental; discovered)" in body
    finally:
        http.close()
        server.shutdown()
        server.server_close()


def test_e2e_save_and_test_runs_one_bounded_loudness_seed(
    tmp_path: Path, monkeypatch,
):
    events = []
    monkeypatch.setattr(
        voice_setup,
        "restart_voice_daemon",
        lambda: events.append(("restart",)),
    )

    def seed_fn(cfg, *, path, force, max_attempts, retry_backoff_sec):
        events.append((
            "seed",
            cfg.voice_provider,
            cfg.openai_api_key,
            path,
            force,
            max_attempts,
            retry_backoff_sec,
        ))
        return SimpleNamespace(source_lufs=-18.7, confidence=0.65)

    server, base, _ = _start_server(tmp_path, loudness_seed_fn=seed_fn)
    try:
        form = _form_for(active="openai", openai_key="sk-fresh")
        status, location, _ = _post(f"{base}/save-test", form)
        assert status == 303
        assert "Saved and tested OpenAI" in urllib.parse.unquote(location)
        assert "sk-fresh" not in urllib.parse.unquote(location)

        state = voice_setup._load_state(str(tmp_path / "voice_provider.env"))
        assert state["JASPER_VOICE_PROVIDER"] == "openai"
        assert "OPENAI_API_KEY" not in state  # split into voice_keys.env (4a)
        keys = voice_setup._load_state(str(tmp_path / "voice_keys.env"))
        assert keys["OPENAI_API_KEY"] == "sk-fresh"
        assert events == [
            (
                "seed",
                "openai",
                "sk-fresh",
                str(tmp_path / "assistant_loudness_profiles.json"),
                True,
                1,
                0.0,
            ),
            ("restart",),
        ]
    finally:
        server.shutdown()
        server.server_close()


def test_e2e_save_and_test_redacts_provider_error_and_still_saves(
    tmp_path: Path, monkeypatch,
):
    restarted = []
    monkeypatch.setattr(
        voice_setup,
        "restart_voice_daemon",
        lambda: restarted.append(True),
    )

    def seed_fn(cfg, **_kwargs):
        raise RuntimeError(f"provider rejected API key {cfg.openai_api_key}")

    server, base, _ = _start_server(tmp_path, loudness_seed_fn=seed_fn)
    try:
        form = _form_for(active="openai", openai_key="sk-secret-tail9999")
        status, location, _ = _post(f"{base}/save-test", form)
        flash = urllib.parse.unquote(location)
        assert status == 303
        assert "Saved, but OpenAI Realtime voice test failed" in flash
        assert "sk-secret-tail9999" not in flash
        assert "sk-s" in flash and "9999" in flash

        state = voice_setup._load_state(str(tmp_path / "voice_provider.env"))
        assert "OPENAI_API_KEY" not in state  # split into voice_keys.env (4a)
        keys = voice_setup._load_state(str(tmp_path / "voice_keys.env"))
        assert keys["OPENAI_API_KEY"] == "sk-secret-tail9999"
        assert restarted == [True]
    finally:
        server.shutdown()
        server.server_close()


def test_e2e_save_and_test_handles_seed_skip_and_restarts(
    tmp_path: Path, monkeypatch,
):
    restarted = []
    monkeypatch.setattr(
        voice_setup,
        "restart_voice_daemon",
        lambda: restarted.append(True),
    )

    server, base, _ = _start_server(tmp_path, loudness_seed_fn=lambda *a, **k: None)
    try:
        form = _form_for(active="openai", openai_key="sk-fresh")
        status, location, _ = _post(f"{base}/save-test", form)
        flash = urllib.parse.unquote(location)
        assert status == 303
        assert "Saved, but OpenAI Realtime voice test failed" in flash
        assert "incomplete" in flash
        assert voice_setup._load_state(
            str(tmp_path / "voice_provider.env"),
        )["JASPER_VOICE_PROVIDER"] == "openai"
        assert restarted == [True]
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
        keys = voice_setup._load_state(str(tmp_path / "voice_keys.env"))
        # WS1 Phase 4a — OPENAI creds gone from BOTH files; the kept GEMINI key
        # lives in the split-out keys file; the non-secret model stays broad.
        assert "OPENAI_API_KEY" not in loaded and "OPENAI_API_KEY" not in keys
        assert "JASPER_OPENAI_MODEL" not in loaded
        assert keys["GEMINI_API_KEY"] == "AIza-keep"
    finally:
        server.shutdown()
        server.server_close()


# ---------- Pricing editor (/pricing) --------------------------------------
def test_index_renders_pricing_section_with_provider_buckets():
    page = voice_setup._index_html(
        {"JASPER_VOICE_PROVIDER": "openai"}, "tok", default_as_of="2026-05-30",
    ).decode()
    assert "Pricing rates" in page
    assert "Bundled rates as of 2026-05-30" in page
    # OpenAI shows the cached bucket; Gemini shows only audio (no text);
    # Grok shows the flat-rate bucket.
    assert "price__gpt-realtime-2__cached_input_per_million_usd" in page
    assert "price__gemini-3.1-flash-live-preview__audio_input_per_million_usd" in page
    assert "price__gemini-3.1-flash-live-preview__text_input_per_million_usd" not in page
    assert "price__grok-voice-think-fast-1.0__flat_per_hour_usd" in page


def test_index_prefills_custom_override_and_tags_it():
    page = voice_setup._index_html(
        {"JASPER_VOICE_PROVIDER": "openai"}, "tok",
        overrides={"gpt-realtime-2": {"text_output_per_million_usd": 28.0}},
    ).decode()
    assert 'value="28"' in page
    assert "custom" in page  # the custom chip


def test_apply_pricing_save_is_sparse_and_omits_defaults():
    openai = catalog.provider_by_id("openai")
    form = {
        "provider": "openai",
        "price__gpt-realtime-2__text_output_per_million_usd": "30",   # changed
        "price__gpt-realtime-2__audio_output_per_million_usd": "64",  # == default
        "price__gpt-realtime-2__audio_input_per_million_usd": "",     # blank
    }
    out = voice_setup._apply_pricing_save(form, openai, ["gpt-realtime-2"], {})
    assert out == {"gpt-realtime-2": {"text_output_per_million_usd": 30.0}}


def test_apply_pricing_save_preserves_other_providers():
    openai = catalog.provider_by_id("openai")
    existing = {"grok-voice-think-fast-1.0": {"flat_per_hour_usd": 5.0}}
    form = {
        "provider": "openai",
        "price__gpt-realtime-2__text_output_per_million_usd": "30",
    }
    out = voice_setup._apply_pricing_save(
        form, openai, ["gpt-realtime-2"], existing,
    )
    assert out["grok-voice-think-fast-1.0"] == {"flat_per_hour_usd": 5.0}
    assert out["gpt-realtime-2"] == {"text_output_per_million_usd": 30.0}


def test_apply_pricing_save_blank_resets_model():
    grok = catalog.provider_by_id("grok")
    out = voice_setup._apply_pricing_save(
        {"provider": "grok",
         "price__grok-voice-think-fast-1.0__flat_per_hour_usd": ""},
        grok, ["grok-voice-think-fast-1.0"],
        {"grok-voice-think-fast-1.0": {"flat_per_hour_usd": 5.0}},
    )
    assert out == {}


def test_apply_pricing_save_rejects_nonnumeric_and_negative():
    openai = catalog.provider_by_id("openai")
    form = {
        "provider": "openai",
        "price__gpt-realtime-2__text_output_per_million_usd": "abc",
        "price__gpt-realtime-2__audio_input_per_million_usd": "-5",
    }
    out = voice_setup._apply_pricing_save(form, openai, ["gpt-realtime-2"], {})
    assert out == {}


def test_pricing_round_trip_through_overrides_loader(tmp_path: Path):
    """A saved override file is read back by load_pricing_overrides and
    applied by pricing_for_model (the full daemon-facing contract)."""
    from jasper import usage
    openai = catalog.provider_by_id("openai")
    out = voice_setup._apply_pricing_save(
        {"provider": "openai",
         "price__gpt-realtime-2__text_output_per_million_usd": "30"},
        openai, ["gpt-realtime-2"], {},
    )
    f = tmp_path / "pricing.json"
    _common.write_json_file(str(f), {"as_of": "2026-08-01", "models": out})
    loaded = usage.load_pricing_overrides(str(f))
    eff = usage.pricing_for_model("gpt-realtime-2", overrides=loaded)
    assert eff.text_output_per_million_usd == 30.0
    assert eff.audio_input_per_million_usd == 32.0  # bundled default kept


# ---------- Pricing research prompt + import (Phase 3) ----------------------
def test_research_prompt_lists_current_models_and_schema():
    prompt = voice_setup._pricing_research_prompt({})
    assert "gpt-realtime-2" in prompt
    assert "gemini-3.1-flash-live-preview" in prompt
    assert "grok-voice-think-fast-1.0" in prompt
    assert "flat_per_hour_usd" in prompt   # grok bucket present
    assert '"models"' in prompt            # the output schema
    assert "ai.google.dev" in prompt and "x.ai" in prompt  # pricing pages


def test_research_prompt_includes_discovered_models():
    snap = model_discovery.DiscoverySnapshot(
        provider_id="openai",
        fetched_at="2026-05-30T00:00:00Z",
        models=("gpt-realtime-3",),
    )
    prompt = voice_setup._pricing_research_prompt({"openai": snap})
    assert "gpt-realtime-3" in prompt


def test_index_renders_research_prompt_and_import_form():
    page = voice_setup._index_html({"JASPER_VOICE_PROVIDER": "openai"}, "tok").decode()
    assert "Refresh all rates from a chatbot" in page
    assert 'action="pricing-import"' in page
    assert 'id="pricing-prompt"' in page


def test_pricing_import_parses_wrapped_json():
    models, _as_of, err = voice_setup._apply_pricing_paste(
        '{"models": {"gpt-realtime-2": {"text_output_per_million_usd": 30}}}'
    )
    assert err is None
    assert models == {"gpt-realtime-2": {"text_output_per_million_usd": 30.0}}


def test_pricing_import_strips_code_fence():
    models, _as_of, err = voice_setup._apply_pricing_paste(
        '```json\n{"models": {"gpt-realtime-2": {"audio_input_per_million_usd": 31}}}\n```'
    )
    assert err is None
    assert models == {"gpt-realtime-2": {"audio_input_per_million_usd": 31.0}}


def test_pricing_import_accepts_bare_model_map():
    models, _as_of, err = voice_setup._apply_pricing_paste(
        '{"gpt-realtime-mini": {"audio_output_per_million_usd": 19}}'
    )
    assert err is None
    assert models == {"gpt-realtime-mini": {"audio_output_per_million_usd": 19.0}}


def test_pricing_import_rejects_garbage_and_empty():
    assert voice_setup._apply_pricing_paste("not json")[0] is None
    assert voice_setup._apply_pricing_paste("")[0] is None
    # Valid JSON but no usable rate fields → rejected with a message.
    out, _as_of, err = voice_setup._apply_pricing_paste('{"models": {"x": {"bogus": 1}}}')
    assert out is None and err


def test_pricing_import_round_trips_to_pricing_for_model(tmp_path: Path):
    from jasper import usage
    models, _as_of, err = voice_setup._apply_pricing_paste(
        '{"models": {"gpt-realtime-2": {"text_output_per_million_usd": 33}}}'
    )
    assert err is None
    f = tmp_path / "pricing.json"
    _common.write_json_file(str(f), {"as_of": "2026-09-01", "models": models})
    loaded = usage.load_pricing_overrides(str(f))
    eff = usage.pricing_for_model("gpt-realtime-2", overrides=loaded)
    assert eff.text_output_per_million_usd == 33.0
    assert eff.audio_input_per_million_usd == 32.0  # bundled default kept


# ---------- Review fixes: catalog metadata, as_of, merge, write_json_file ----
def test_catalog_entries_carry_pricing_metadata():
    """Per-provider pricing knowledge lives on the catalog entry (single
    source), not in voice_setup maps. Buckets must be real Pricing fields."""
    from jasper.usage import _OVERRIDABLE_FIELDS
    for p in catalog.PROVIDERS:
        assert p.pricing_url, f"{p.id} missing pricing_url"
        assert p.pricing_buckets, f"{p.id} missing pricing_buckets"
        for bucket in p.pricing_buckets:
            assert bucket in _OVERRIDABLE_FIELDS, f"{p.id}: bad bucket {bucket}"


def test_apply_pricing_paste_preserves_as_of():
    models, as_of, err = voice_setup._apply_pricing_paste(
        '{"as_of": "2026-09-09", "models": '
        '{"gpt-realtime-2": {"text_output_per_million_usd": 30}}}'
    )
    assert err is None
    assert as_of == "2026-09-09"  # data vintage, not import date


def test_sparsify_overrides_drops_at_default_fields():
    sp = voice_setup._sparsify_overrides({
        "gpt-realtime-2": {
            "text_output_per_million_usd": 24.0,   # == bundled default → drop
            "audio_input_per_million_usd": 99.0,   # custom → keep
        },
    })
    assert sp == {"gpt-realtime-2": {"audio_input_per_million_usd": 99.0}}


def test_write_json_file_atomic_mode_0644(tmp_path: Path):
    import json
    p = tmp_path / "x.json"
    _common.write_json_file(str(p), {"models": {"m": {"a": 1.0}}})
    assert json.loads(p.read_text()) == {"models": {"m": {"a": 1.0}}}
    assert (os.stat(p).st_mode & 0o777) == 0o644  # no secrets → 0644 fine
    assert not (tmp_path / "x.json.tmp").exists()  # temp cleaned up


def test_pricing_import_route_merges_preserving_other_models(tmp_path: Path):
    """End-to-end: POST /pricing-import MERGES — a model the paste omits
    keeps its existing override (regression for the full-replace data-loss
    finding). Also exercises CSRF (the _post helper mints the token)."""
    import json
    pricing_path = tmp_path / "pricing.json"
    pricing_path.write_text(json.dumps(
        {"models": {"grok-voice-think-fast-1.0": {"flat_per_hour_usd": 5.0}}}
    ))
    server, base, thread = _start_server(tmp_path)
    try:
        status, _loc, _body = _post(base + "/pricing-import", {
            "payload": '{"models": {"gpt-realtime-2": '
                       '{"text_output_per_million_usd": 30}}}',
        })
        assert status == 303
        saved = json.loads(pricing_path.read_text())["models"]
        assert saved["gpt-realtime-2"] == {"text_output_per_million_usd": 30.0}
        # The pre-existing grok override the paste didn't mention survives.
        assert saved["grok-voice-think-fast-1.0"] == {"flat_per_hour_usd": 5.0}
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_pricing_save_route_writes_sparse_override(tmp_path: Path):
    """End-to-end: POST /pricing (editor) writes a sparse model-ID override;
    a blanked field stays at the bundled default."""
    import json
    pricing_path = tmp_path / "pricing.json"
    server, base, thread = _start_server(tmp_path)
    try:
        status, _loc, _body = _post(base + "/pricing", {
            "provider": "openai",
            "price__gpt-realtime-2__text_output_per_million_usd": "29",
            "price__gpt-realtime-2__audio_input_per_million_usd": "",  # default
        })
        assert status == 303
        saved = json.loads(pricing_path.read_text())["models"]
        assert saved["gpt-realtime-2"] == {"text_output_per_million_usd": 29.0}
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_pricing_import_route_replaces_same_model_override(tmp_path: Path):
    """Re-importing a model REPLACES that model's prior override (per-model
    replace); the cross-model merge that preserves *other* models is covered
    by test_pricing_import_route_merges_preserving_other_models."""
    import json
    pricing_path = tmp_path / "pricing.json"
    pricing_path.write_text(json.dumps(
        {"models": {"gpt-realtime-2": {"text_output_per_million_usd": 99.0}}}
    ))
    server, base, thread = _start_server(tmp_path)
    try:
        status, _loc, _body = _post(base + "/pricing-import", {
            "payload": '{"models": {"gpt-realtime-2": '
                       '{"audio_input_per_million_usd": 30}}}',
        })
        assert status == 303
        saved = json.loads(pricing_path.read_text())["models"]
        # Prior text_output override gone (replaced); new audio_input stands.
        assert saved["gpt-realtime-2"] == {"audio_input_per_million_usd": 30.0}
    finally:
        server.shutdown()
        thread.join(timeout=5)
