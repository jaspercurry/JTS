# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the jasper-doctor voice domain."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


from jasper.cli import doctor
from jasper.config import Config
from jasper.voice.catalog import PROVIDERS, provider_ids_manifest_text


from .doctor_test_support import (
    _fresh_cfg,
)

# -------------------------------------------------- provider-aware key check


def test_provider_key_gemini_ok(monkeypatch):
    cfg = _fresh_cfg(monkeypatch, GEMINI_API_KEY="AIzaSyABCDEF12345")
    r = doctor.check_provider_key(cfg)
    assert r.status == "ok"
    assert r.name == "GEMINI_API_KEY"


def test_provider_key_openai_ok(monkeypatch):
    cfg = _fresh_cfg(
        monkeypatch,
        JASPER_VOICE_PROVIDER="openai",
        OPENAI_API_KEY="sk-realkey1234",
    )
    r = doctor.check_provider_key(cfg)
    assert r.status == "ok"
    assert r.name == "OPENAI_API_KEY"


def test_provider_key_grok_ok(monkeypatch):
    cfg = _fresh_cfg(
        monkeypatch,
        JASPER_VOICE_PROVIDER="grok",
        XAI_API_KEY="xai-realkey1234",
    )
    r = doctor.check_provider_key(cfg)
    assert r.status == "ok"
    assert r.name == "XAI_API_KEY"


def test_provider_key_warns_on_wrong_prefix(monkeypatch):
    cfg = _fresh_cfg(
        monkeypatch,
        JASPER_VOICE_PROVIDER="openai",
        OPENAI_API_KEY="WRONGPREFIX-1234",
    )
    r = doctor.check_provider_key(cfg)
    assert r.status == "warn"


def test_provider_key_other_providers_keys_unchecked(monkeypatch):
    """Active=openai. GEMINI_API_KEY is intentionally unset; the doctor
    must NOT flag that as a problem — gemini is dormant."""
    cfg = _fresh_cfg(
        monkeypatch,
        JASPER_VOICE_PROVIDER="openai",
        OPENAI_API_KEY="sk-active1234",
    )
    r = doctor.check_provider_key(cfg)
    assert r.status == "ok"


def test_provider_key_accepts_each_catalog_provider():
    for provider in PROVIDERS:
        key = provider.key_prefix_hint.rstrip(".") + "test-key"
        cfg = SimpleNamespace(
            voice_provider=provider.id,
            **{f"{provider.id.replace('-', '_')}_api_key": key},
        )

        r = doctor.check_provider_key(cfg)  # type: ignore[arg-type]

        assert r.status == "ok"
        assert r.name == provider.key_env


def test_voice_provider_ids_manifest_ok(monkeypatch, tmp_path: Path):
    manifest = tmp_path / "voice_provider_ids"
    manifest.write_text(provider_ids_manifest_text())
    monkeypatch.setenv("JASPER_VOICE_PROVIDER_IDS_FILE", str(manifest))

    r = doctor.check_voice_provider_ids_manifest()

    assert r.status == "ok"


def test_voice_provider_ids_manifest_missing_fails(monkeypatch, tmp_path: Path):
    monkeypatch.setenv(
        "JASPER_VOICE_PROVIDER_IDS_FILE",
        str(tmp_path / "missing_provider_ids"),
    )

    r = doctor.check_voice_provider_ids_manifest()

    assert r.status == "fail"
    assert "missing" in r.detail


def test_voice_provider_ids_manifest_stale_fails(monkeypatch, tmp_path: Path):
    manifest = tmp_path / "voice_provider_ids"
    manifest.write_text("gemini\n")
    monkeypatch.setenv("JASPER_VOICE_PROVIDER_IDS_FILE", str(manifest))

    r = doctor.check_voice_provider_ids_manifest()

    assert r.status == "fail"
    assert "stale" in r.detail


def test_voice_provider_ids_manifest_reordered_warns(monkeypatch, tmp_path: Path):
    manifest = tmp_path / "voice_provider_ids"
    manifest.write_text("openai\ngrok\ngemini\n")
    monkeypatch.setenv("JASPER_VOICE_PROVIDER_IDS_FILE", str(manifest))

    r = doctor.check_voice_provider_ids_manifest()

    assert r.status == "warn"


# ------------------------------------------------------ Spotify Connect check


def test_spotify_connect_device_consumes_build_result(monkeypatch, tmp_path: Path):
    """build_clients returns BuildResult, not a bare clients dict.

    The dashboard runs `jasper-doctor --json` through jasper-control; a
    shape mismatch here used to crash before JSON rendering, which made
    /system/diagnostics report "doctor output not JSON".
    """
    accounts_path = tmp_path / "accounts.json"
    accounts_path.write_text(
        '{"accounts": [{"name": "jasper", "cache_path": "/tmp/cache"}], '
        '"default": "jasper"}'
    )
    cfg = _fresh_cfg(
        monkeypatch,
        GEMINI_API_KEY="AIzaSyTest",
        SPOTIFY_CLIENT_ID="a" * 32,
        JASPER_SPOTIFY_ACCOUNTS_PATH=str(accounts_path),
        JASPER_SPEAKER_NAME="JTS",
    )

    from jasper.spotify_router import ACCOUNT_OK, AccountStatus, BuildResult

    fake_client = SimpleNamespace(
        sp=SimpleNamespace(devices=lambda: {"devices": [{"name": "Kitchen JTS"}]}),
    )

    def fake_build_clients(_registry, *, client_id, redirect_uri):  # noqa: ARG001
        return BuildResult(
            clients={"jasper": fake_client},
            statuses=[AccountStatus(name="jasper", state=ACCOUNT_OK)],
            default_name="jasper",
        )

    with patch("jasper.spotify_router.build_clients", side_effect=fake_build_clients):
        result = doctor.check_spotify_connect_device(cfg)

    assert result.status == "ok"
    assert "jasper" in result.detail


def test_pricing_ok_when_active_model_priced(monkeypatch):
    """The active model (gemini default) is in the bundled rates → ok."""
    cfg = _fresh_cfg(monkeypatch, GEMINI_API_KEY="AIzaABCDEF12345")
    assert doctor.check_pricing(cfg).status == "ok"


def test_pricing_warns_when_active_model_unpriced(monkeypatch):
    """An active model with no bundled/override rate → warn (cost reads $0,
    the spend cap can't bound it)."""
    cfg = _fresh_cfg(
        monkeypatch,
        GEMINI_API_KEY="AIzaABCDEF12345",
        JASPER_GEMINI_MODEL="gemini-9.9-does-not-exist",
    )
    assert doctor.check_pricing(cfg).status == "warn"


# ---------------------------------------------------------------------------
# check_spend_cap — disabled cap renders as "disabled", not "$0.00 remaining"


def test_check_spend_cap_reports_disabled_not_zero_remaining(
    tmp_path: Path, monkeypatch
):
    """With JASPER_DAILY_SPEND_CAP_USD=0 the cap is disabled (see
    jasper.usage.SpendCap.disabled); doctor must say so instead of the
    misleading "$0.0000 remaining of $0.00"."""
    from jasper.cli.doctor.voice import check_spend_cap

    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setenv("JASPER_VOICE_PROVIDER", "gemini")
    monkeypatch.setenv("JASPER_USAGE_DB", str(tmp_path / "usage.sqlite3"))
    monkeypatch.setenv("JASPER_DAILY_SPEND_CAP_USD", "0")
    cfg = Config.from_env()
    result = check_spend_cap(cfg)
    assert result.status == "ok"
    assert "disabled" in result.detail
    assert "remaining" not in result.detail


def test_check_spend_cap_detail_names_tuning_ledger_state(tmp_path: Path, monkeypatch):
    """The detail string surfaces whether the tuning sibling ledger exists, so
    the operator knows household spend now folds in correction-web's paid
    tuning calls. Absent by default; present once the sibling file exists."""
    from jasper.cli.doctor.voice import check_spend_cap
    from jasper.usage import tuning_usage_db_path

    usage_db = tmp_path / "usage.db"
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setenv("JASPER_VOICE_PROVIDER", "gemini")
    monkeypatch.setenv("JASPER_USAGE_DB", str(usage_db))
    monkeypatch.setenv("JASPER_DAILY_SPEND_CAP_USD", "1.00")
    cfg = Config.from_env()

    # Neither ledger exists yet.
    result = check_spend_cap(cfg)
    assert result.status == "ok"
    assert "tuning ledger absent" in result.detail

    # Create the tuning sibling; now the detail says it is included.
    from jasper.usage import UsageStore

    UsageStore(tuning_usage_db_path(str(usage_db)))._conn.close()
    result = check_spend_cap(cfg)
    assert result.status == "ok"
    assert "includes tuning ledger" in result.detail
