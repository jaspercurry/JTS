# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the jasper-doctor voice domain."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from jasper.cli import doctor
from jasper.cli.doctor import voice as doctor_voice
from jasper.config import Config
from jasper.tools.packs import TOOL_PACKS
from jasper.voice.catalog import PROVIDERS, provider_ids_manifest_text

from .doctor_test_support import _fresh_cfg

# -------------------------------------------------- provider-aware key check
#
# "Which provider is active?" is answered by the wizard-owned SSOT file, not
# by Config/os.environ — the two can name different providers in one report
# (issue #2212), so every test here lays the file down explicitly.


def _ssot(monkeypatch, tmp_path: Path, provider: str = "") -> Path:
    """Point the active-provider reader at a tmp SSOT file. Empty
    ``provider`` writes a file that selects nothing."""
    path = tmp_path / "voice_provider.env"
    path.write_text(f"JASPER_VOICE_PROVIDER={provider}\n" if provider else "")
    monkeypatch.setenv("JASPER_VOICE_PROVIDER_FILE", str(path))
    return path


def _state(status: str, provider: str = "", raw: str = ""):
    from jasper.voice.provider_state import ActiveProviderState

    return ActiveProviderState(
        provider, None, status, "/tmp/voice_provider.env", raw_provider=raw,
    )


@pytest.mark.parametrize("provider", PROVIDERS, ids=lambda p: p.id)
def test_provider_key_accepts_each_catalog_provider(
    monkeypatch, tmp_path: Path, provider
):
    _ssot(monkeypatch, tmp_path, provider.id)
    key = provider.key_prefix_hint.rstrip(".") + "test-key"
    cfg = SimpleNamespace(
        voice_provider=provider.id,
        **{f"{provider.id.replace('-', '_')}_api_key": key},
    )

    r = doctor.check_provider_key(cfg)  # type: ignore[arg-type]

    assert r.status == "ok"
    assert r.name == provider.key_env


def test_provider_key_warns_on_wrong_prefix(monkeypatch, tmp_path: Path):
    _ssot(monkeypatch, tmp_path, "openai")
    cfg = _fresh_cfg(
        monkeypatch,
        JASPER_VOICE_PROVIDER="openai",
        OPENAI_API_KEY="WRONGPREFIX-1234",
    )

    assert doctor.check_provider_key(cfg).status == "warn"


def test_provider_key_ignores_dormant_providers(monkeypatch, tmp_path: Path):
    """Active=openai with GEMINI_API_KEY unset: gemini is dormant, not broken."""
    _ssot(monkeypatch, tmp_path, "openai")
    cfg = _fresh_cfg(
        monkeypatch,
        JASPER_VOICE_PROVIDER="openai",
        OPENAI_API_KEY="sk-active1234",
    )

    assert doctor.check_provider_key(cfg).status == "ok"


def test_provider_key_checks_the_ssot_provider_not_the_environments(
    monkeypatch, tmp_path: Path
):
    """The SSOT file selects gemini while this process's environment names
    openai (`sudo -E JASPER_VOICE_PROVIDER=openai jasper-doctor`). The row
    must be about gemini's key, and must not resolve the disagreement
    silently."""
    _ssot(monkeypatch, tmp_path, "gemini")
    cfg = _fresh_cfg(
        monkeypatch,
        JASPER_VOICE_PROVIDER="openai",
        OPENAI_API_KEY="sk-openai1234",
        GEMINI_API_KEY="AIzaSyGemini1234",
    )

    r = doctor.check_provider_key(cfg)

    assert r.name == "GEMINI_API_KEY"
    assert r.status == "warn"


@pytest.mark.parametrize(
    "state, env_provider, status",
    [
        (_state("invalid", raw="nope"), "gemini", "fail"),
        (_state("unreadable"), "gemini", "warn"),
        (_state("missing"), "gemini", "warn"),
        (_state("unset"), "gemini", "warn"),
        (_state("unset"), "", "ok"),
    ],
    ids=["invalid", "unreadable", "file-missing", "unset", "nothing-anywhere"],
)
def test_provider_key_adjudicates_the_selection_by_status(
    monkeypatch, state, env_provider, status
):
    """This check owns the verdict on the selection itself (its neighbours
    defer to it), gated on ActiveProviderState.status — never on the
    environment's opinion."""
    monkeypatch.setattr(
        doctor_voice, "read_active_provider_state", lambda: state,
    )
    cfg = SimpleNamespace(voice_provider=env_provider, gemini_api_key="AIzaSy1")

    r = doctor.check_provider_key(cfg)  # type: ignore[arg-type]

    assert r.status == status


@pytest.mark.parametrize(
    "body, status",
    [
        (None, "fail"),
        ("gemini\n", "fail"),
        ("openai\ngrok\ngemini\n", "warn"),
        (provider_ids_manifest_text(), "ok"),
    ],
    ids=["absent", "stale", "reordered", "current"],
)
def test_voice_provider_ids_manifest_verdicts(
    monkeypatch, tmp_path: Path, body, status
):
    manifest = tmp_path / "voice_provider_ids"
    if body is not None:
        manifest.write_text(body)
    monkeypatch.setenv("JASPER_VOICE_PROVIDER_IDS_FILE", str(manifest))

    assert doctor.check_voice_provider_ids_manifest().status == status


# ------------------------------------------------------ Spotify Connect check


def test_spotify_connect_device_consumes_build_result(monkeypatch, tmp_path: Path):
    """build_clients returns BuildResult, not a bare clients dict — a shape
    mismatch used to crash `jasper-doctor --json` before rendering, which the
    dashboard surfaced as "doctor output not JSON"."""
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


# --------------------------------------------------------- pricing / spend cap


@pytest.mark.parametrize(
    "model, status",
    [(None, "ok"), ("gemini-9.9-does-not-exist", "warn")],
    ids=["priced", "unpriced"],
)
def test_pricing_warns_only_for_an_unpriced_active_model(
    monkeypatch, tmp_path: Path, model, status
):
    """An unpriced active model reads $0 cost, so the spend cap cannot bound it."""
    _ssot(monkeypatch, tmp_path, "gemini")
    extra = {"JASPER_GEMINI_MODEL": model} if model else {}
    cfg = _fresh_cfg(monkeypatch, GEMINI_API_KEY="AIzaABCDEF12345", **extra)

    assert doctor.check_pricing(cfg).status == status


def test_pricing_prices_the_ssot_providers_model(monkeypatch, tmp_path: Path):
    """Which model gets a rate follows the SSOT provider; the model string
    itself still comes from Config, which merges the operator env with the
    wizard file exactly as jasper-voice does."""
    import jasper.usage as usage

    _ssot(monkeypatch, tmp_path, "gemini")
    cfg = _fresh_cfg(
        monkeypatch,
        JASPER_VOICE_PROVIDER="openai",
        OPENAI_API_KEY="sk-openai1234",
    )
    priced: list[str] = []
    real = usage.pricing_for_model
    monkeypatch.setattr(
        usage,
        "pricing_for_model",
        lambda m, **kw: (priced.append(m), real(m, **kw))[1],
    )

    doctor.check_pricing(cfg)

    assert cfg.gemini_model != cfg.openai_model
    assert priced == [cfg.gemini_model]


def _spend_cap_cfg(monkeypatch, tmp_path: Path, cap: str) -> Config:
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setenv("JASPER_VOICE_PROVIDER", "gemini")
    monkeypatch.setenv("JASPER_USAGE_DB", str(tmp_path / "usage.db"))
    monkeypatch.setenv("JASPER_DAILY_SPEND_CAP_USD", cap)
    return Config.from_env()


def test_check_spend_cap_reports_disabled_not_zero_remaining(
    tmp_path: Path, monkeypatch
):
    """Cap 0 means disabled (jasper.usage.SpendCap.disabled), not
    "$0.0000 remaining of $0.00"."""
    cfg = _spend_cap_cfg(monkeypatch, tmp_path, "0")

    result = doctor_voice.check_spend_cap(cfg)

    assert result.status == "ok"
    assert "disabled" in result.detail
    assert "remaining" not in result.detail


def test_check_spend_cap_detail_names_tuning_ledger_state(
    tmp_path: Path, monkeypatch
):
    """Household spend folds in correction-web's paid tuning calls, so the
    operator needs to know whether that sibling ledger is in the total."""
    from jasper.usage import UsageStore, tuning_usage_db_path

    cfg = _spend_cap_cfg(monkeypatch, tmp_path, "1.00")

    assert "tuning ledger absent" in doctor_voice.check_spend_cap(cfg).detail

    UsageStore(tuning_usage_db_path(str(tmp_path / "usage.db")))._conn.close()

    assert "includes tuning ledger" in doctor_voice.check_spend_cap(cfg).detail


# ------------------------------------------------------------- tool packs
#
# Tool registration is fault-isolated per pack (jasper.tools.packs): a pack
# whose build raises contributes no tools but the daemon starts fine, which
# used to be observable only as event=tool_pack.build_failed in the journal.
# check_tool_packs cross-checks the static registry against what jasper-voice
# actually registered (/state.voice.tool_packs).

EXPECTED = [p.name for p in TOOL_PACKS]


def _runtime(names, *, failed=(), skipped=()):
    """Build a /state.voice.tool_packs-shaped runtime list."""
    out = []
    for n in names:
        if n in failed:
            status, count, error = "failed", 0, "ImportError('boom')"
        elif n in skipped:
            status, count, error = "skipped", 0, None
        else:
            status, count, error = "registered", 2, None
        out.append(
            {"name": n, "status": status, "tool_count": count, "error": error}
        )
    return out


def _drop_last(rt):
    return rt[:-1]


@pytest.mark.parametrize(
    "runtime, status, must_name",
    [
        # Control unreachable / older daemon: report the registry alone.
        (None, "ok", ""),
        (_runtime(EXPECTED), "ok", ""),
        (_runtime(EXPECTED, failed={EXPECTED[2]}), "fail", EXPECTED[2]),
        # A registry pack absent from the runtime report (daemon predates it).
        (_drop_last(_runtime(EXPECTED)), "warn", EXPECTED[-1]),
        # A failed pack is the alarm even when another is also absent.
        (_drop_last(_runtime(EXPECTED, failed={EXPECTED[0]})), "fail", EXPECTED[0]),
        (_runtime(EXPECTED, skipped={EXPECTED[-1]}), "ok", EXPECTED[-1]),
    ],
    ids=[
        "runtime-unavailable",
        "all-registered",
        "build-failed",
        "pack-missing",
        "failed-beats-missing",
        "gated-off",
    ],
)
def test_assess_tool_packs_verdicts(runtime, status, must_name):
    r = doctor._assess_tool_packs(EXPECTED, runtime)

    assert r.status == status
    assert must_name in r.detail


def test_assess_tool_packs_failure_points_at_the_journal_line():
    r = doctor._assess_tool_packs(EXPECTED, _runtime(EXPECTED, failed={EXPECTED[2]}))

    assert "event=tool_pack.build_failed" in r.detail


@pytest.mark.parametrize(
    "state",
    [
        "raise",
        {"voice": {}},  # voice present, no tool_packs key (older daemon)
    ],
    ids=["control-unreachable", "field-absent"],
)
def test_tool_packs_runtime_reader_is_fail_soft(monkeypatch, state):
    import jasper.control.client as control

    if state == "raise":

        def reader(*a, **k):
            raise control.ControlError("connection refused")

    else:

        def reader(*a, **k):
            return state

    monkeypatch.setattr(control, "get_state", reader)

    assert doctor._voice_tool_packs_runtime() is None


def test_tool_packs_runtime_reader_parses_the_state_field(monkeypatch):
    import jasper.control.client as control

    payload = {"voice": {"tool_packs": _runtime(["audio", "timer"])}}
    monkeypatch.setattr(control, "get_state", lambda *a, **k: payload)

    assert [p["name"] for p in doctor._voice_tool_packs_runtime()] == [
        "audio",
        "timer",
    ]


@pytest.mark.parametrize(
    "runtime, status",
    [(None, "ok"), (_runtime(EXPECTED, failed={EXPECTED[1]}), "fail")],
    ids=["registry-only", "runtime-failure"],
)
def test_check_tool_packs_wires_the_runtime_reader(monkeypatch, runtime, status):
    monkeypatch.setattr(doctor_voice, "_voice_tool_packs_runtime", lambda: runtime)

    r = doctor.check_tool_packs()

    assert r.status == status
    if runtime is None:
        for name in EXPECTED:
            assert name in r.detail


def test_check_tool_packs_is_registered_at_its_reserved_order():
    by_order = {c.order: c for c in doctor.registered_checks()}

    assert by_order[44.5].func is doctor.check_tool_packs
    assert by_order[44.5].group == "voice"
