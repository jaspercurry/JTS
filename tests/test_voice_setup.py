# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Voice configuration, credential persistence, and setup form behavior."""
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

from jasper.control import service_restart
from jasper import atomic_io, env_file
from jasper.voice import catalog
from jasper.voice import model_discovery
from jasper.web import _common, voice_cost_page, voice_costs, voice_setup
from jasper.web._common import RESTART_CLAUSE, RestartOutcome


# ---------- Save logic -----------------------------------------------------


def _form_for(active="openai", **kwargs) -> dict[str, str]:
    form = {"active": active}
    provider = catalog.provider_by_id(active)
    if provider:
        form.update({
            f"{active}_key": "",
            f"{active}_model": catalog.default_model_id(active),
            f"{active}_voice": catalog.default_voice_id(active),
            **{f"{active}_{extra.name}": extra.default for extra in provider.extras},
        })
    return {**form, **kwargs}


@pytest.mark.parametrize("provider", catalog.PROVIDERS, ids=lambda p: p.id)
def test_saving_one_provider_keeps_other_provider_settings(provider):
    current = {p.model_env: f"custom-{p.id}" for p in catalog.PROVIDERS}
    current.update({p.key_env: "saved-key" for p in catalog.PROVIDERS})
    new, error = voice_setup._apply_save(_form_for(provider.id), current)
    assert error is None
    assert new["JASPER_VOICE_PROVIDER"] == provider.id
    for other in catalog.PROVIDERS:
        if other.id != provider.id:
            assert new[other.model_env] == current[other.model_env]
        assert new[other.key_env] == current[other.key_env]


def test_catalog_defaults_are_listed_with_their_validation_status():
    """The wizard defaults should be conscious, audited catalog entries.

    The catalog is still not an allow-list, but the built-in defaults
    should not drift into an unlabelled or fallback-only state.
    """
    for provider in catalog.PROVIDERS:
        defaults = _form_for(provider.id)
        model_default = defaults[f"{provider.id}_model"]
        voice_default = defaults[f"{provider.id}_voice"]
        model = next((m for m in provider.models if m.default), None)
        assert model is not None, f"{provider.id} model default missing"
        assert model.id == model_default
        assert model.status is (catalog.ModelStatus.EXPERIMENTAL if provider.id == "openai_live" else catalog.ModelStatus.TESTED)
        voice = next((v for v in provider.voices if v.default), None)
        assert voice is not None, f"{provider.id} voice default missing"
        assert voice.id == voice_default


def test_provider_ids_manifest_is_shell_readable_catalog_projection():
    lines = catalog.provider_ids_manifest_text().splitlines()

    assert lines == sorted(catalog.VALID_PROVIDER_IDS)
    assert "" not in lines
    assert all("=" not in line and line.strip() == line for line in lines)


@pytest.mark.parametrize("provider", catalog.PROVIDERS, ids=lambda p: p.id)
def test_index_offers_selected_provider_catalog_and_defaults(provider):
    page = voice_setup.index_html({}, "tok", selected=provider.id).decode()
    for model in provider.models:
        assert model.display_label in page
    for field, default in (
        ("model", catalog.default_model_id(provider.id)),
        ("voice", catalog.default_voice_id(provider.id)),
    ):
        select = page.split(f'name="{provider.id}_{field}"', 1)[1].split("</select>", 1)[0]
        assert f'value="{default}" selected' in select


def test_index_preserves_unknown_model_as_custom_experimental():
    state = {
        "JASPER_VOICE_PROVIDER": "openai",
        "OPENAI_API_KEY": "sk-x",
        "JASPER_OPENAI_MODEL": "gpt-realtime-new-live",
    }
    page = voice_setup.index_html(
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
    page = voice_setup.index_html(
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
    page = voice_setup.index_html(
        {"OPENAI_API_KEY": "sk-x", "JASPER_VOICE_PROVIDER": "openai"},
        "csrf-token-for-test-" + "x" * 32,
    ).decode()
    assert page.count('action="refresh-models"') == 1
    assert 'aria-label="Refresh models"' in page


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
    """A newly released model the wizard's Refresh discovered is not in
    the curated catalog yet. The form's explicit choice must survive
    rather than collapse back to the default."""
    form = _form_for(
        active="openai",
        openai_key="sk-x",
        openai_model="gpt-realtime-new-live",
    )
    new, err = voice_setup._apply_save(form, {})
    assert err is None
    assert new["JASPER_OPENAI_MODEL"] == "gpt-realtime-new-live"


# ---------- Clear logic ----------------------------------------------------


def test_apply_clear_removes_only_the_key():
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
    assert new == {key: value for key, value in current.items() if key != "OPENAI_API_KEY"}


def test_apply_clear_unknown_provider_errors():
    new, err = voice_setup._apply_clear({"provider": "anthropic"}, {"X": "y"})
    assert err is not None
    assert new == {"X": "y"}


# ---------- Spend cap ------------------------------------------------------


def _usage_db_with_cost(
    tmp_path: Path, cost_usd: float, *, name: str = "usage.db",
) -> Path:
    from jasper.usage import UsageStore

    db = tmp_path / name
    UsageStore(str(db))
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
    status = voice_costs.read_spend_cap_status({
        "JASPER_USAGE_DB": str(db),
        "JASPER_DAILY_SPEND_CAP_USD": "1.00",
        "JASPER_DAILY_SPEND_CAP_SAFETY_MULTIPLIER": "1.25",
    })

    assert status["usage_available"] is True
    assert status["spend_last_24h_usd"] == pytest.approx(0.81)
    assert status["padded_spend_usd"] == pytest.approx(1.0125)
    assert status["allowed"] is False
    assert status["remaining_usd"] == 0


def test_costs_renders_spend_cap_status_and_save_form(tmp_path: Path):
    db = _usage_db_with_cost(tmp_path, 0.25)
    page = voice_cost_page.costs_html(
        {"JASPER_USAGE_DB": str(db), "JASPER_DAILY_SPEND_CAP_USD": "2.00"},
        "csrf-token-for-test-" + "x" * 32,
    ).decode()

    assert 'action="spend-cap"' in page
    assert 'name="daily_spend_cap_usd"' in page
    assert "Rolling 24h spend" in page
    assert "$0.2500" in page
    # The card explains the household-spend semantics: dollars include the
    # tuning assistant, the turn count does not.
    assert "Turns today counts voice turns only" in page


def test_read_spend_cap_status_tuning_only_ledger_shows_dollars(tmp_path: Path):
    """Tuning-only box (usage.db absent, usage-tuning.db present): the card
    must render the real household dollars — the daemon's cap counts that
    spend, so 'no usage yet' would disagree with a cap that can block. And
    'Turns today' stays VOICE-only, so it reads 0 here."""
    usage_db = tmp_path / "usage.db"  # never created
    _usage_db_with_cost(tmp_path, 0.40, name="usage-tuning.db")
    status = voice_costs.read_spend_cap_status({
        "JASPER_USAGE_DB": str(usage_db),
        "JASPER_DAILY_SPEND_CAP_USD": "1.00",
        "JASPER_DAILY_SPEND_CAP_SAFETY_MULTIPLIER": "1.0",
    })

    assert status["usage_available"] is True
    assert status["spend_last_24h_usd"] == pytest.approx(0.40)
    assert status["sessions_today"] == 0  # voice-only count
    assert not usage_db.exists()  # the status read never creates the voice DB


def test_read_spend_cap_status_turns_today_counts_voice_only(tmp_path: Path):
    """Dollar figures are household (voice + tuning); the 'Turns today'
    figure counts only voice sessions."""
    db = _usage_db_with_cost(tmp_path, 0.10)  # 1 voice session
    _usage_db_with_cost(tmp_path, 0.05, name="usage-tuning.db")  # 1 tuning tap
    status = voice_costs.read_spend_cap_status({
        "JASPER_USAGE_DB": str(db),
        "JASPER_DAILY_SPEND_CAP_USD": "1.00",
        "JASPER_DAILY_SPEND_CAP_SAFETY_MULTIPLIER": "1.0",
    })

    assert status["spend_last_24h_usd"] == pytest.approx(0.15)  # household
    assert status["sessions_today"] == 1  # voice member only


def test_apply_spend_cap_writes_env_keys_and_preserves_provider_state():
    current = {"JASPER_VOICE_PROVIDER": "openai", "OPENAI_API_KEY": "sk-x"}
    new, err = voice_setup.apply_spend_cap({
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
    new, err = voice_setup.apply_spend_cap({
        "daily_spend_cap_usd": "-1",
        "daily_spend_cap_safety_multiplier": "1.25",
    }, current)
    assert err is not None
    assert new == current

    new, err = voice_setup.apply_spend_cap({
        "daily_spend_cap_usd": "1",
        "daily_spend_cap_safety_multiplier": "0.5",
    }, current)
    assert err is not None
    assert new == current


# ---------- Page rendering -------------------------------------------------


@pytest.mark.parametrize("provider", catalog.PROVIDERS, ids=lambda p: p.id)
@pytest.mark.parametrize("saved", [False, True])
def test_selected_provider_form_preserves_controls_and_masks_keys(provider, saved):
    key = "test-key-123456789-tail"
    state = {provider.key_env: key} if saved else {}
    page = voice_setup.index_html(state, "tok", selected=provider.id).decode()
    assert key not in page
    if saved:
        assert _common.mask_secret(key) in page
    assert f'name="active" value="{provider.id}"' in page
    for field in ("key", "model", "voice", *(e.name for e in provider.extras)):
        anchor = f'name="{provider.id}_{field}"'
        idx = page.index(anchor)
        tag = page[page.rfind("<", 0, idx):page.index(">", idx)]
        assert 'form="save-form"' in tag
        if field == "key":
            assert ("Key saved" in tag) is saved
            assert (" required" in tag) is not saved
    for other in catalog.PROVIDERS:
        if other.id != provider.id:
            assert f'name="{other.id}_key"' not in page
            assert f'name="{other.id}_model"' not in page
    assert 'formaction="save-test"' in page
    assert page.index("1. Select provider") < page.index("2. Enter API key") < page.index("3. Select model")


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
        service_restart, "restart_voice_daemon", lambda: called.append(True) or RestartOutcome.RAN,
    )
    # The voice_setup module imported the symbol directly; patch it
    # there too.
    monkeypatch.setattr(
        voice_setup, "restart_voice_daemon", lambda: called.append(True) or RestartOutcome.RAN,
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
        loaded = env_file.read_env_file(str(state_path))
        assert loaded["JASPER_VOICE_PROVIDER"] == "openai"
        # WS1 Phase 4a — the API key is SPLIT OUT into voice_keys.env; the broad
        # voice_provider.env must NOT carry it.
        assert "OPENAI_API_KEY" not in loaded
        keys = env_file.read_env_file(str(tmp_path / "voice_keys.env"))
        assert keys["OPENAI_API_KEY"] == "sk-fresh"
        # Restart was invoked.
        assert called == [True]
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("outcome", list(RestartOutcome))
@pytest.mark.parametrize("saver", ["save", "spend-cap"])
def test_e2e_a_saver_describes_the_restart_it_actually_got(
    tmp_path: Path, monkeypatch, saver, outcome,
):
    """The privileged restart's real verdict has to reach the household, and
    every saver on the page has to describe the same verdict the same way —
    otherwise /voice tells two stories about one daemon. The config IS saved
    either way, so the answer stays a 303."""
    monkeypatch.setattr(
        voice_setup, "restart_voice_daemon", lambda: outcome,
    )
    state_path = tmp_path / "voice_provider.env"
    atomic_io.write_env_file(str(state_path), {
        "JASPER_VOICE_PROVIDER": "openai",
        "OPENAI_API_KEY": "sk-keep",
    })
    bodies = {
        "save": lambda: _form_for(active="openai", openai_key="sk-fresh"),
        "spend-cap": lambda: {
            "daily_spend_cap_usd": "5",
            "daily_spend_cap_safety_multiplier": "1.1",
        },
    }

    server, base, _ = _start_server(tmp_path)
    try:
        status, location, _ = _post(f"{base}/{saver}", bodies[saver]())
        assert status == 303
        flash = urllib.parse.unquote(location)
        assert "Saved" in flash
        for candidate, clause in RESTART_CLAUSE.items():
            if not clause:
                continue
            assert (clause.strip() in flash) is (candidate is outcome)
        assert state_path.exists()
    finally:
        server.shutdown()
        server.server_close()


def test_e2e_spend_cap_save_writes_voice_env_and_restarts(
    tmp_path: Path, monkeypatch,
):
    called = []
    monkeypatch.setattr(
        voice_setup, "restart_voice_daemon", lambda: called.append(True) or RestartOutcome.RAN,
    )
    state_path = tmp_path / "voice_provider.env"
    atomic_io.write_env_file(str(state_path), {
        "JASPER_VOICE_PROVIDER": "openai",
        "OPENAI_API_KEY": "sk-keep",
    })
    server, base, _ = _start_server(tmp_path)
    try:
        status, location, _ = _post(f"{base}/spend-cap", {
            "provider": "openai_live",
            "daily_spend_cap_usd": "5",
            "daily_spend_cap_safety_multiplier": "1.1",
        })
        assert status == 303
        assert urllib.parse.urlsplit(location).path == "costs"
        assert urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)["provider"] == ["openai_live"]
        assert "Saved spend cap" in urllib.parse.unquote(location)
        loaded = env_file.read_env_file(str(state_path))
        # WS1 Phase 4a — the key is preserved across a spend-cap save, but lives
        # in the split-out keys file, not the broad voice_provider.env.
        assert "OPENAI_API_KEY" not in loaded
        keys = env_file.read_env_file(str(tmp_path / "voice_keys.env"))
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
        voice_setup, "restart_voice_daemon", lambda: called.append(True) or RestartOutcome.RAN,
    )
    state_path = tmp_path / "voice_provider.env"
    atomic_io.write_env_file(str(state_path), {
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
        lambda: events.append(("restart",)) or RestartOutcome.RAN,
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

        state = env_file.read_env_file(str(tmp_path / "voice_provider.env"))
        assert state["JASPER_VOICE_PROVIDER"] == "openai"
        assert "OPENAI_API_KEY" not in state  # split into voice_keys.env (4a)
        keys = env_file.read_env_file(str(tmp_path / "voice_keys.env"))
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


def test_redact_provider_error_scrubs_a_key_with_no_recognised_prefix(
    monkeypatch,
):
    """A pasted key the pattern rules cannot see must not reach the flash.

    `api_key_token_is_valid` accepts any `[A-Za-z0-9_.~-]+`, so the literal
    the wizard just wrote is the only thing that can remove it.
    """
    for key_env in voice_setup._SECRET_KEY_ENVS:
        monkeypatch.delenv(key_env, raising=False)
    key = "myk3y_abcdefgh"

    msg = voice_setup._redact_provider_error(
        RuntimeError(f"Incorrect API key provided: {key}"),
        {"OPENAI_API_KEY": key},
    )

    assert key not in msg
    assert "<redacted>" in msg


def test_e2e_save_and_test_redacts_provider_error_and_still_saves(
    tmp_path: Path, monkeypatch,
):
    restarted = []
    monkeypatch.setattr(
        voice_setup,
        "restart_voice_daemon",
        lambda: restarted.append(True) or RestartOutcome.RAN,
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
        assert "<redacted>" in flash

        state = env_file.read_env_file(str(tmp_path / "voice_provider.env"))
        assert "OPENAI_API_KEY" not in state  # split into voice_keys.env (4a)
        keys = env_file.read_env_file(str(tmp_path / "voice_keys.env"))
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
        lambda: restarted.append(True) or RestartOutcome.RAN,
    )

    server, base, _ = _start_server(tmp_path, loudness_seed_fn=lambda *a, **k: None)
    try:
        form = _form_for(active="openai", openai_key="sk-fresh")
        status, location, _ = _post(f"{base}/save-test", form)
        flash = urllib.parse.unquote(location)
        assert status == 303
        assert "Saved, but OpenAI Realtime voice test failed" in flash
        assert "incomplete" in flash
        assert env_file.read_env_file(
            str(tmp_path / "voice_provider.env"),
        )["JASPER_VOICE_PROVIDER"] == "openai"
        assert restarted == [True]
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("route", ["save", "save-test"])
@pytest.mark.parametrize(("fields", "saved_keys"), [
    pytest.param(
        {"grok_key": "invalid-key with-whitespace", "grok_model": "custom-model"}, {},
        id="malformed_key",
    ),
    pytest.param({"grok_model": "custom-model"}, {}, id="model_not_offered"),
    pytest.param({"grok_model": "grok-voice-think-fast-1.0"}, {}, id="no_key"),
    pytest.param(
        {"grok_key": "xai-valid-0123456789", "grok_model": "custom-model"},
        {"XAI_API_KEY": "xai-valid-0123456789"},
        id="key_saved_before_the_selection_refusal",
    ),
])
def test_e2e_rejected_save_keeps_choices_without_echoing_key(
    tmp_path, monkeypatch, route, fields, saved_keys,
):
    monkeypatch.setenv("JASPER_ENV_FILE", str(tmp_path / "jasper.env"))
    restarts = []
    monkeypatch.setattr(voice_setup, "restart_voice_daemon", lambda: restarts.append(True))
    server, base, _ = _start_server(tmp_path)
    try:
        status, _, body = _post(f"{base}/{route}", {
            "active": "grok", "grok_voice": "rex", **fields,
        })
        assert status == 422
        if "grok_key" in fields:
            assert fields["grok_key"] not in body
        assert f'value="{fields["grok_model"]}" selected' in body
        assert 'value="rex" selected' in body
        assert 'name="active" value="grok"' in body
        assert restarts == []
        assert not (tmp_path / "voice_provider.env").exists()
        assert env_file.read_env_file(str(tmp_path / "voice_keys.env")) == saved_keys
    finally:
        server.shutdown()
        server.server_close()


def test_e2e_save_accepts_a_key_only_the_operator_env_holds(tmp_path, monkeypatch):
    """A key an operator set in /etc/jasper/jasper.env lets the wizard select
    that provider with no wizard-saved key, and the save never copies it into
    the wizard's files."""
    operator_env = tmp_path / "jasper.env"
    operator_env.write_text("GEMINI_API_KEY=AIza-from-etc\n")
    monkeypatch.setenv("JASPER_ENV_FILE", str(operator_env))
    monkeypatch.setattr(voice_setup, "restart_voice_daemon", lambda: RestartOutcome.RAN)
    server, base, _ = _start_server(tmp_path)
    try:
        status, _, _ = _post(f"{base}/save", _form_for(active="gemini"))
        assert status == 303
        state = env_file.read_env_file(str(tmp_path / "voice_provider.env"))
        assert state["JASPER_VOICE_PROVIDER"] == "gemini"
        assert "GEMINI_API_KEY" not in state
        assert not (tmp_path / "voice_keys.env").exists()
    finally:
        server.shutdown()
        server.server_close()


def test_e2e_get_index_renders_state(tmp_path: Path, monkeypatch):
    """Load the page from a populated state file. Confirms the GET
    path threads state through to the renderer."""
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    state_path = tmp_path / "voice_provider.env"
    atomic_io.write_env_file(str(state_path), {
        "JASPER_VOICE_PROVIDER": "openai",
        "OPENAI_API_KEY": "sk-existing-12345abc",
        "JASPER_OPENAI_MODEL": "gpt-realtime-2",
    })
    server, base, _ = _start_server(tmp_path)
    try:
        body = urllib.request.urlopen(f"{base}/").read().decode()
        assert 'value="openai" selected' in body
        assert 'name="active" value="openai"' in body
        # Mask shows up (prefix + suffix) but raw key does NOT.
        assert "sk-existing-12345abc" not in body
        assert "sk-e" in body and "5abc" in body
    finally:
        server.shutdown()
        server.server_close()


def test_e2e_clear_credentials_removes_provider_keys(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(
        voice_setup, "restart_voice_daemon", lambda: RestartOutcome.RAN,
    )
    state_path = tmp_path / "voice_provider.env"
    atomic_io.write_env_file(str(state_path), {
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
        loaded = env_file.read_env_file(str(state_path))
        keys = env_file.read_env_file(str(tmp_path / "voice_keys.env"))
        # WS1 Phase 4a — OPENAI creds gone from BOTH files; the kept GEMINI key
        # lives in the split-out keys file; the non-secret model stays broad.
        assert "OPENAI_API_KEY" not in loaded and "OPENAI_API_KEY" not in keys
        assert loaded["JASPER_OPENAI_MODEL"] == "gpt-realtime-2"
        assert keys["GEMINI_API_KEY"] == "AIza-keep"
    finally:
        server.shutdown()
        server.server_close()


# ---------- Pricing editor (/pricing) --------------------------------------
@pytest.mark.parametrize("provider", catalog.PROVIDERS, ids=lambda p: p.id)
def test_costs_renders_only_selected_provider_pricing_buckets(provider):
    page = voice_cost_page.costs_html({}, "tok", selected=provider.id).decode()
    for model in provider.models:
        for bucket in provider.pricing_buckets:
            assert f'name="price__{model.id}__{bucket}"' in page
    for other in catalog.PROVIDERS:
        if other.id != provider.id:
            assert f'name="price__{other.models[0].id}__' not in page


def test_costs_prefills_custom_override_and_tags_it():
    page = voice_cost_page.costs_html(
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
    out = voice_setup.apply_pricing_save(form, openai, ["gpt-realtime-2"], {})
    assert out == {"gpt-realtime-2": {"text_output_per_million_usd": 30.0}}


def test_apply_pricing_save_preserves_other_providers():
    openai = catalog.provider_by_id("openai")
    existing = {"grok-voice-think-fast-1.0": {"flat_per_hour_usd": 5.0}}
    form = {
        "provider": "openai",
        "price__gpt-realtime-2__text_output_per_million_usd": "30",
    }
    out = voice_setup.apply_pricing_save(
        form, openai, ["gpt-realtime-2"], existing,
    )
    assert out["grok-voice-think-fast-1.0"] == {"flat_per_hour_usd": 5.0}
    assert out["gpt-realtime-2"] == {"text_output_per_million_usd": 30.0}


def test_apply_pricing_save_blank_resets_model():
    grok = catalog.provider_by_id("grok")
    out = voice_setup.apply_pricing_save(
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
    out = voice_setup.apply_pricing_save(form, openai, ["gpt-realtime-2"], {})
    assert out == {}


def test_pricing_round_trip_through_overrides_loader(tmp_path: Path):
    """A saved override file is read back by load_pricing_overrides and
    applied by pricing_for_model (the full daemon-facing contract)."""
    from jasper import usage
    openai = catalog.provider_by_id("openai")
    out = voice_setup.apply_pricing_save(
        {"provider": "openai",
         "price__gpt-realtime-2__text_output_per_million_usd": "30"},
        openai, ["gpt-realtime-2"], {},
    )
    f = tmp_path / "pricing.json"
    atomic_io.atomic_write_json(str(f), {"as_of": "2026-08-01", "models": out})
    loaded = usage.load_pricing_overrides(str(f))
    eff = usage.pricing_for_model("gpt-realtime-2", overrides=loaded)
    assert eff.text_output_per_million_usd == 30.0
    assert eff.audio_input_per_million_usd == 32.0  # bundled default kept


# ---------- Pricing research prompt + import (Phase 3) ----------------------
def test_research_prompt_lists_current_models_and_schema():
    prompt = voice_cost_page._pricing_research_prompt({})
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
    prompt = voice_cost_page._pricing_research_prompt({"openai": snap})
    assert "gpt-realtime-3" in prompt


def test_costs_renders_research_prompt_and_import_form():
    page = voice_cost_page.costs_html({"JASPER_VOICE_PROVIDER": "openai"}, "tok").decode()
    assert 'action="pricing-import"' in page
    assert 'id="pricing-prompt"' in page


@pytest.mark.parametrize(
    ("pasted", "expected_models"),
    [
        pytest.param(
            '{"models": {"gpt-realtime-2": {"text_output_per_million_usd": 30}}}',
            {"gpt-realtime-2": {"text_output_per_million_usd": 30.0}},
            id="parses_wrapped_json",
        ),
        pytest.param(
            '```json\n{"models": {"gpt-realtime-2": '
            '{"audio_input_per_million_usd": 31}}}\n```',
            {"gpt-realtime-2": {"audio_input_per_million_usd": 31.0}},
            id="strips_code_fence",
        ),
        pytest.param(
            '{"gpt-realtime-mini": {"audio_output_per_million_usd": 19}}',
            {"gpt-realtime-mini": {"audio_output_per_million_usd": 19.0}},
            id="accepts_bare_model_map",
        ),
    ],
)
def test_pricing_import_parses_various_input_shapes(pasted, expected_models):
    models, _as_of, err = voice_setup.apply_pricing_paste(pasted)
    assert err is None
    assert models == expected_models


def test_pricing_import_rejects_garbage_and_empty():
    assert voice_setup.apply_pricing_paste("not json")[0] is None
    assert voice_setup.apply_pricing_paste("")[0] is None
    # Valid JSON but no usable rate fields → rejected with a message.
    out, _as_of, err = voice_setup.apply_pricing_paste('{"models": {"x": {"bogus": 1}}}')
    assert out is None and err


def test_pricing_import_round_trips_to_pricing_for_model(tmp_path: Path):
    from jasper import usage
    models, _as_of, err = voice_setup.apply_pricing_paste(
        '{"models": {"gpt-realtime-2": {"text_output_per_million_usd": 33}}}'
    )
    assert err is None
    f = tmp_path / "pricing.json"
    atomic_io.atomic_write_json(str(f), {"as_of": "2026-09-01", "models": models})
    loaded = usage.load_pricing_overrides(str(f))
    eff = usage.pricing_for_model("gpt-realtime-2", overrides=loaded)
    assert eff.text_output_per_million_usd == 33.0
    assert eff.audio_input_per_million_usd == 32.0  # bundled default kept


# ---------- Review fixes: catalog metadata, as_of, merge --------------------
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
    models, as_of, err = voice_setup.apply_pricing_paste(
        '{"as_of": "2026-09-09", "models": '
        '{"gpt-realtime-2": {"text_output_per_million_usd": 30}}}'
    )
    assert err is None
    assert as_of == "2026-09-09"  # data vintage, not import date


def test_sparsify_overrides_drops_at_default_fields():
    sp = voice_setup.sparsify_overrides({
        "gpt-realtime-2": {
            "text_output_per_million_usd": 24.0,   # == bundled default → drop
            "audio_input_per_million_usd": 99.0,   # custom → keep
        },
    })
    assert sp == {"gpt-realtime-2": {"audio_input_per_million_usd": 99.0}}


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
