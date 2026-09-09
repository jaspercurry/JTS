# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the jasper-doctor voice domain."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import jasper.citibike as citibike_mod
import jasper.home_assistant as ha_mod
from jasper.cli import doctor
from jasper.cli.doctor import renderers
from jasper.cli.doctor import voice as doctor_voice
from jasper.cli.doctor._evidence import evidence
from jasper.config import Config
from jasper.mic_presence import MicPresence
from jasper.tools.packs import TOOL_PACKS
from jasper.voice.catalog import PROVIDERS, default_model_id, provider_ids_manifest_text

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

    r = doctor_voice.check_provider_key(cfg)  # type: ignore[arg-type]

    assert r.status == "ok"
    assert r.name == provider.key_env
    # Presence only — never a prefix or a character count of the key.
    assert r.detail == "configured"


def test_provider_key_warns_on_wrong_prefix(monkeypatch, tmp_path: Path):
    _ssot(monkeypatch, tmp_path, "openai")
    cfg = _fresh_cfg(
        monkeypatch,
        JASPER_VOICE_PROVIDER="openai",
        OPENAI_API_KEY="WRONGPREFIX-1234",
    )

    r = doctor_voice.check_provider_key(cfg)
    assert r.status == "warn"
    assert r.reason == doctor_voice.REASON_PROVIDER_KEY_WRONG_PREFIX


def test_provider_key_ignores_dormant_providers(monkeypatch, tmp_path: Path):
    """Active=openai with GEMINI_API_KEY unset: gemini is dormant, not broken."""
    _ssot(monkeypatch, tmp_path, "openai")
    cfg = _fresh_cfg(
        monkeypatch,
        JASPER_VOICE_PROVIDER="openai",
        OPENAI_API_KEY="sk-active1234",
    )

    r = doctor_voice.check_provider_key(cfg)
    assert r.status == "ok"


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

    r = doctor_voice.check_provider_key(cfg)

    assert r.name == "GEMINI_API_KEY"
    assert r.status == "warn"
    assert r.reason == doctor_voice.REASON_PROVIDER_KEY_OK_ENV_DRIFT
    assert "AIzaSyGemini1234"[:8] not in r.detail


@pytest.mark.parametrize(
    "state, status, reason",
    [
        (
            _state("invalid", raw="nope"), "fail",
            doctor_voice.REASON_PROVIDER_SELECTION_INVALID,
        ),
        (
            _state("unreadable"), "warn",
            doctor_voice.REASON_PROVIDER_SELECTION_UNREADABLE,
        ),
        (_state("missing"), "warn", doctor_voice.REASON_PROVIDER_NOT_CONFIGURED),
        (_state("unset"), "warn", doctor_voice.REASON_PROVIDER_NOT_CONFIGURED),
    ],
    ids=["invalid", "unreadable", "file-missing", "unset"],
)
def test_provider_key_adjudicates_the_selection_by_status(
    monkeypatch, state, status, reason
):
    """This check owns the verdict on the selection itself (its neighbours
    defer to it), gated on ActiveProviderState.status — never on the
    environment's opinion."""
    monkeypatch.setattr(
        doctor_voice, "read_active_provider_state", lambda: state,
    )
    # The environment always names a provider by the time any check runs:
    # Config.from_env() raises VoiceProviderNotConfigured for an unset one
    # and jasper-doctor exits on it before building the check list.
    cfg = SimpleNamespace(voice_provider="gemini", gemini_api_key="AIzaSy1")

    r = doctor_voice.check_provider_key(cfg)  # type: ignore[arg-type]

    assert r.status == status
    assert r.reason == reason


@pytest.mark.parametrize(
    "state, status, reason",
    [
        (
            _state("unreadable"), "skipped",
            doctor_voice.REASON_PROVIDER_IMPORTS_UNDETERMINED,
        ),
        (
            _state("invalid", raw="nope"), "skipped",
            doctor_voice.REASON_PROVIDER_IMPORTS_UNDETERMINED,
        ),
        (
            _state("missing"), "ok",
            doctor_voice.REASON_PROVIDER_IMPORTS_NOT_CONFIGURED,
        ),
        (
            _state("unset"), "ok",
            doctor_voice.REASON_PROVIDER_IMPORTS_NOT_CONFIGURED,
        ),
    ],
    ids=["unreadable", "invalid", "file-missing", "unset"],
)
def test_provider_imports_status_when_it_probed_nothing(
    monkeypatch, state, status, reason
):
    """An undetermined selection defers to check_provider_key's verdict, so
    it never offers a second opinion (skipped). No provider configured yet
    is operator intent, not a finding (ok)."""
    monkeypatch.setattr(
        doctor_voice, "read_active_provider_state", lambda: state,
    )

    r = doctor_voice.check_provider_importable()

    assert r.status == status
    assert r.reason == reason


# The manifest gates jasper-aec-reconcile's start of jasper-voice, so a
# missing or stale one is deafness (AGENTS.md non-negotiable 6), not a finding.
@pytest.mark.parametrize(
    "body, status, reason",
    [
        (None, "fail", doctor_voice.REASON_MANIFEST_MISSING),
        ("gemini\n", "fail", doctor_voice.REASON_MANIFEST_STALE),
        ("openai\ngrok\ngemini\n", "ok", doctor_voice.REASON_MANIFEST_NONCANONICAL),
        (provider_ids_manifest_text(), "ok", doctor_voice.REASON_MANIFEST_CURRENT),
    ],
    ids=["absent", "stale", "reordered", "current"],
)
def test_voice_provider_ids_manifest_verdicts(
    monkeypatch, tmp_path: Path, body, status, reason
):
    manifest = tmp_path / "voice_provider_ids"
    if body is not None:
        manifest.write_text(body)
    monkeypatch.setenv("JASPER_VOICE_PROVIDER_IDS_FILE", str(manifest))

    r = doctor_voice.check_voice_provider_ids_manifest()
    assert r.status == status
    assert r.reason == reason


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
        result = renderers.check_spotify_connect_device(cfg)

    assert result.status == "ok"
    assert "jasper" in result.detail


# --------------------------------------------------------- pricing / spend cap


@pytest.mark.parametrize(
    "model, status, reason",
    [
        (None, "ok", doctor_voice.REASON_PRICING_MODEL_PRICED),
        (
            "gemini-9.9-does-not-exist", "warn",
            doctor_voice.REASON_PRICING_MODEL_UNPRICED,
        ),
    ],
    ids=["priced", "unpriced"],
)
def test_pricing_warns_only_for_an_unpriced_active_model(
    monkeypatch, tmp_path: Path, model, status, reason
):
    """An unpriced active model reads $0 cost, so the spend cap cannot bound it."""
    _ssot(monkeypatch, tmp_path, "gemini")
    monkeypatch.setattr(
        doctor_voice,
        "read_active_model_from_env_files",
        lambda provider: model or default_model_id(provider),
    )

    r = doctor_voice.check_pricing()
    assert r.status == status
    assert r.reason == reason


@pytest.mark.parametrize(
    "status, verdict, reason",
    [
        ("unreadable", "skipped", "REASON_PRICING_MODEL_UNDETERMINED"),
        ("invalid", "skipped", "REASON_PRICING_MODEL_UNDETERMINED"),
        ("missing", "ok", "REASON_PRICING_MODEL_NOT_CONFIGURED"),
        ("unset", "ok", "REASON_PRICING_MODEL_NOT_CONFIGURED"),
    ],
)
def test_pricing_skips_when_it_could_not_ask_which_model_is_active(
    monkeypatch, status, verdict, reason
):
    """A row that could not resolve its own question observed nothing, so it
    skips rather than warning — first-time setup (missing/unset) stays ok."""
    monkeypatch.setattr(
        doctor_voice, "read_active_provider_state", lambda: _state(status),
    )

    r = doctor_voice.check_pricing()
    assert r.status == verdict
    assert r.reason == getattr(doctor_voice, reason)


def test_pricing_prices_the_model_the_ssot_provider_resolves_from_files(
    monkeypatch, tmp_path: Path,
):
    """Which model gets a rate follows the SSOT provider (not a stale
    JASPER_VOICE_PROVIDER this process's environment might carry), and
    the model string itself comes from
    provider_state.read_active_model_from_env_files — the merged env
    FILES, never Config/os.environ, where a shell-exported
    JASPER_GEMINI_MODEL would outrank the wizard file (issue #3133).
    File-vs-shell precedence itself is pinned in test_provider_state.py;
    this test pins the doctor's dispatch to that resolver."""
    import jasper.usage as usage

    _ssot(monkeypatch, tmp_path, "gemini")
    monkeypatch.setenv("JASPER_VOICE_PROVIDER", "openai")  # stale env; must lose
    seen_providers: list[str] = []

    def fake_model_from_files(provider: str) -> str:
        seen_providers.append(provider)
        return "gemini-3.1-flash-live-preview"

    monkeypatch.setattr(
        doctor_voice, "read_active_model_from_env_files", fake_model_from_files,
    )
    priced: list[str] = []
    real = usage.pricing_for_model
    monkeypatch.setattr(
        usage,
        "pricing_for_model",
        lambda m, **kw: (priced.append(m), real(m, **kw))[1],
    )

    result = doctor_voice.check_pricing()

    assert seen_providers == ["gemini"]
    assert priced == ["gemini-3.1-flash-live-preview"]
    assert result.status == "ok"
    assert result.reason == doctor_voice.REASON_PRICING_MODEL_PRICED


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
    assert result.reason == doctor_voice.REASON_SPEND_CAP_DISABLED


def test_check_spend_cap_reflects_tuning_ledger_state_in_its_reason(
    tmp_path: Path, monkeypatch
):
    """Household spend folds in correction-web's paid tuning calls: no usage
    at all (including no tuning ledger) is a distinct reason from ordinary
    remaining-budget reporting once the ledger exists."""
    from jasper.usage import UsageStore, tuning_usage_db_path

    cfg = _spend_cap_cfg(monkeypatch, tmp_path, "1.00")

    r = doctor_voice.check_spend_cap(cfg)
    assert r.reason == doctor_voice.REASON_SPEND_CAP_NO_USAGE

    UsageStore(tuning_usage_db_path(str(tmp_path / "usage.db")))._conn.close()

    r = doctor_voice.check_spend_cap(cfg)
    assert r.reason == doctor_voice.REASON_SPEND_CAP_OK


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
    "runtime, status, reason",
    [
        # Control unreachable / older daemon: registration never observed.
        (None, "skipped", doctor_voice.REASON_TOOL_PACKS_RUNTIME_UNAVAILABLE),
        (_runtime(EXPECTED), "ok", doctor_voice.REASON_TOOL_PACKS_HEALTHY),
        (
            _runtime(EXPECTED, failed={EXPECTED[2]}), "fail",
            doctor_voice.REASON_TOOL_PACKS_BUILD_FAILED,
        ),
        # A registry pack absent from the runtime report (daemon predates it).
        (
            _drop_last(_runtime(EXPECTED)), "warn",
            doctor_voice.REASON_TOOL_PACKS_MISSING_FROM_RUNTIME,
        ),
        # A failed pack is the alarm even when another is also absent.
        (
            _drop_last(_runtime(EXPECTED, failed={EXPECTED[0]})), "fail",
            doctor_voice.REASON_TOOL_PACKS_BUILD_FAILED,
        ),
        (
            _runtime(EXPECTED, skipped={EXPECTED[-1]}), "ok",
            doctor_voice.REASON_TOOL_PACKS_HEALTHY,
        ),
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
def test_assess_tool_packs_verdicts(runtime, status, reason):
    r = doctor_voice._assess_tool_packs(EXPECTED, runtime)

    assert r.status == status
    assert r.reason == reason


@pytest.mark.parametrize(
    "state",
    [
        "raise",
        {"voice": {}},  # voice present, no tool_packs key (older daemon)
    ],
    ids=["control-unreachable", "field-absent"],
)
def test_tool_packs_runtime_reader_is_fail_soft(monkeypatch, state):
    import jasper.platform.control_client as control

    if state == "raise":

        def reader(*a, **k):
            raise control.ControlError("connection refused")

    else:

        def reader(*a, **k):
            return state

    monkeypatch.setattr(control, "get_state", reader)

    assert doctor_voice._voice_tool_packs_runtime() is None


def test_tool_packs_runtime_reader_parses_the_state_field(monkeypatch):
    import jasper.platform.control_client as control

    payload = {"voice": {"tool_packs": _runtime(["audio", "timer"])}}
    monkeypatch.setattr(control, "get_state", lambda *a, **k: payload)

    assert [p["name"] for p in doctor_voice._voice_tool_packs_runtime()] == [
        "audio",
        "timer",
    ]


@pytest.mark.parametrize(
    "runtime, status, reason",
    [
        (None, "skipped", doctor_voice.REASON_TOOL_PACKS_RUNTIME_UNAVAILABLE),
        (
            _runtime(EXPECTED, failed={EXPECTED[1]}), "fail",
            doctor_voice.REASON_TOOL_PACKS_BUILD_FAILED,
        ),
    ],
    ids=["registry-only", "runtime-failure"],
)
def test_check_tool_packs_wires_the_runtime_reader(monkeypatch, runtime, status, reason):
    monkeypatch.setattr(doctor_voice, "_voice_tool_packs_runtime", lambda: runtime)

    r = doctor_voice.check_tool_packs()

    assert r.status == status
    assert r.reason == reason


def test_check_tool_packs_is_registered_in_the_voice_module():
    entry = next(
        c for c in doctor.registered_checks()
        if c.func is doctor_voice.check_tool_packs
    )

    assert entry.module == "voice"


# ADR-0217 streambox gate: 4 checks self-gate on evidence.streambox_awaiting_accessory().


def _mic(accessory: bool) -> MicPresence:
    return MicPresence(
        present=accessory, accessory_sources=("wiim_remote_2",) if accessory else (),
    )


def _gated_check_rows(monkeypatch, tmp_path: Path):
    """(check, not-gated status, not-gated reason), each pinned by stubbing
    its own first real dependency to a deterministic outcome."""
    monkeypatch.setattr(
        doctor_voice, "read_active_provider_state", lambda: _state("missing"),
    )
    monkeypatch.setattr(doctor_voice, "_voice_tool_packs_runtime", lambda: None)
    monkeypatch.setattr(doctor_voice, "_voice_runtime", lambda: None)
    manifest = tmp_path / "voice_provider_ids"
    manifest.write_text(provider_ids_manifest_text())
    monkeypatch.setenv("JASPER_VOICE_PROVIDER_IDS_FILE", str(manifest))
    return [
        (doctor_voice.check_provider_importable,
         "ok", doctor_voice.REASON_PROVIDER_IMPORTS_NOT_CONFIGURED),
        (doctor_voice.check_tool_packs,
         "skipped", doctor_voice.REASON_TOOL_PACKS_RUNTIME_UNAVAILABLE),
        (doctor_voice.check_wake_event_storage,
         "skipped", "wake_event_storage_unavailable"),
        (doctor_voice.check_pricing,
         "ok", doctor_voice.REASON_PRICING_MODEL_NOT_CONFIGURED),
        (doctor_voice.check_voice_provider_ids_manifest,
         "ok", doctor_voice.REASON_MANIFEST_CURRENT),
    ]


def _expected_gate_reason(check, profile: str, accessory: bool) -> str | None:
    streambox = profile == "streambox"
    if check is doctor_voice.check_voice_provider_ids_manifest:
        return doctor_voice.REASON_MANIFEST_NOT_RENDERED if streambox else None
    return (
        doctor_voice.REASON_VOICE_UNIT_NOT_FULL_PROFILE
        if streambox and not accessory else None
    )


@pytest.mark.parametrize(
    "profile, accessory",
    [("full", False), ("streambox", False), ("streambox", True)],
    ids=["full", "streambox-no-accessory", "streambox-with-accessory"],
)
def test_voice_checks_gate_on_streambox_without_accessory(
    monkeypatch, tmp_path: Path, profile, accessory,
):
    """A streambox with a paired accessory gets real rows from every
    self-gating voice check; one without gets `skipped` rows, never `warn`
    — no second gate."""
    evidence.seed("install_profile_is_streambox", profile == "streambox")
    evidence.seed("mic_presence", _mic(accessory))

    for check, off_status, off_reason in _gated_check_rows(monkeypatch, tmp_path):
        result = check()
        gate_reason = _expected_gate_reason(check, profile, accessory)
        expected = (
            ("skipped", gate_reason) if gate_reason else (off_status, off_reason)
        )
        assert (result.status, result.reason) == expected, check.__name__


@pytest.mark.parametrize("changes, status, reason", [
    ({}, "ok", "wake_event_storage_healthy"),
    ({"discarded_work": 1}, "warn", "wake_event_storage_degraded"),
    ({"write_errors": 1, "last_error": "OperationalError"}, "warn", "wake_event_storage_degraded"),
    ({"pending_work": 64}, "warn", "wake_event_storage_degraded"),
    ({"pending_bytes": 2 * 1024 * 1024}, "warn", "wake_event_storage_degraded"),
    ({"accepting": False}, "warn", "wake_event_storage_disabled"),
])
def test_doctor_reports_actual_wake_storage_pressure(monkeypatch, changes, status, reason):
    store = dict(accepting=True, pending_work=0, pending_bytes=0, max_pending_work=64,
                 max_pending_bytes=2 * 1024 * 1024, discarded_work=0, write_errors=0, last_error=None)
    monkeypatch.setattr(doctor_voice, "_voice_runtime", lambda: {"wake_event_store": store | changes})
    result = doctor_voice.check_wake_event_storage()
    assert (result.status, result.reason) == (status, reason)


@pytest.mark.parametrize("voice, status, reason", [
    (None, "skipped", "wake_event_storage_unavailable"),
    ({}, "skipped", "wake_event_storage_unavailable"),
    ({"wake_event_store": None}, "warn", "wake_event_storage_disabled"),
])
def test_doctor_distinguishes_disabled_storage_from_missing_status(monkeypatch, voice, status, reason):
    monkeypatch.setattr(doctor_voice, "_voice_runtime", lambda: voice)
    result = doctor_voice.check_wake_event_storage()
    assert (result.status, result.reason) == (status, reason)


# -------------------------------------------------- optional cloud integrations

# ------------------------------------------------------------ Google Routes

_ROUTES_VARS = (
    "GOOGLE_ROUTES_API_KEY",
    "JASPER_TRANSIT_LAT",
    "JASPER_TRANSIT_LON",
    "JASPER_TRANSIT_DISPLAY_NAME",
    "JASPER_TRAVEL_DEFAULT_MODE",
    "JASPER_HOSTNAME",
)

_ORIGIN = {"JASPER_TRANSIT_LAT": "40.758", "JASPER_TRANSIT_LON": "-73.985"}


def _routes_cfg(monkeypatch, **vars_) -> Config:
    for var in _ROUTES_VARS:
        monkeypatch.delenv(var, raising=False)
    return _fresh_cfg(monkeypatch, GEMINI_API_KEY="AIzaSyTest", **vars_)


@pytest.mark.parametrize(
    "vars_, status, reason",
    [
        ({}, "ok", "REASON_GOOGLE_ROUTES_NOT_CONFIGURED"),
        (
            {"GOOGLE_ROUTES_API_KEY": "AIzaSySynthetic"},
            "warn",
            "REASON_GOOGLE_ROUTES_MISSING_SETUP",
        ),
        (
            {
                "GOOGLE_ROUTES_API_KEY": "AIzaSySynthetic",
                **_ORIGIN,
                "JASPER_TRAVEL_DEFAULT_MODE": "hovercraft",
            },
            "warn",
            "REASON_GOOGLE_ROUTES_INVALID_MODE",
        ),
        (
            {
                "GOOGLE_ROUTES_API_KEY": "AIzaSySynthetic",
                **_ORIGIN,
                "JASPER_TRAVEL_DEFAULT_MODE": "drive",
            },
            "ok",
            "REASON_GOOGLE_ROUTES_CONFIGURED",
        ),
    ],
    ids=["unconfigured", "key-without-origin", "invalid-mode", "configured"],
)
def test_check_google_routes_verdicts(monkeypatch, vars_, status, reason):
    cfg = _routes_cfg(monkeypatch, **vars_)

    r = doctor_voice.check_google_routes(cfg)

    assert r.status == status
    assert r.reason == getattr(doctor_voice, reason)


# ---------------------------------------------------------------- Citi Bike


def _citibike_cfg(monkeypatch, *, stations: str = "", ebike_only: str = "") -> Config:
    return _fresh_cfg(
        monkeypatch,
        GEMINI_API_KEY="AIza-stub",
        JASPER_CITIBIKE_STATIONS=stations,
        JASPER_CITIBIKE_EBIKE_ONLY=ebike_only,
    )


def _gbfs(monkeypatch, station_ids):
    feed = {"data": {"stations": [{"station_id": s} for s in station_ids]}}
    monkeypatch.setattr(citibike_mod, "fetch_feed", lambda url, ttl, **kw: feed)


@pytest.mark.parametrize(
    "stations, live_ids, status, reason",
    [
        ("", (), "ok", "REASON_CITIBIKE_NOT_CONFIGURED"),
        (
            "abc|9 Av,def|Atlantic", ("abc", "def"), "ok",
            "REASON_CITIBIKE_CONNECTED",
        ),
        # One station retired by Lyft: named, but the rest still work.
        (
            "abc|9 Av,def|Gone Station", ("abc",), "ok",
            "REASON_CITIBIKE_STATIONS_RETIRED",
        ),
        # More than three missing: still the same retirement reason.
        ("a|A,b|B,c|C,d|D,e|E", (), "ok", "REASON_CITIBIKE_STATIONS_RETIRED"),
    ],
    ids=["unconfigured", "all-resolve", "one-retired", "missing-list-capped"],
)
def test_check_citibike_verdicts(
    monkeypatch, stations, live_ids, status, reason
):
    _gbfs(monkeypatch, live_ids)
    cfg = _citibike_cfg(monkeypatch, stations=stations)

    r = doctor_voice.check_citibike(cfg)

    assert r.status == status
    assert r.reason == getattr(doctor_voice, reason)


@pytest.mark.parametrize(
    "ebike_only, reason",
    [
        ("", "REASON_CITIBIKE_CONNECTED"),
        ("1", "REASON_CITIBIKE_CONNECTED_EBIKE_ONLY"),
    ],
    ids=["mixed", "ebike-only"],
)
def test_check_citibike_reports_the_ebike_only_mode(monkeypatch, ebike_only, reason):
    _gbfs(monkeypatch, ("abc",))
    cfg = _citibike_cfg(monkeypatch, stations="abc|9 Av", ebike_only=ebike_only)

    r = doctor_voice.check_citibike(cfg)

    assert r.reason == getattr(doctor_voice, reason)


def test_check_citibike_warns_when_gbfs_unreachable(monkeypatch):
    def _raise(url, ttl, **kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(citibike_mod, "fetch_feed", _raise)
    cfg = _citibike_cfg(monkeypatch, stations="abc|9 Av")

    r = doctor_voice.check_citibike(cfg)
    assert r.status == "warn"
    assert r.reason == doctor_voice.REASON_CITIBIKE_GBFS_UNREACHABLE


# ----------------------------------------------------------- Home Assistant


def _ha_cfg(monkeypatch, *, url: str = "", token: str = "") -> Config:
    return _fresh_cfg(
        monkeypatch,
        GEMINI_API_KEY="AIza-stub",
        JASPER_HA_URL=url,
        JASPER_HA_TOKEN=token,
    )


def _ha_probe(monkeypatch, result=None, raises: Exception | None = None):
    async def fake_probe(url, token, *, force=False, verify_ssl=True):
        if raises is not None:
            raise raises
        return dict(result or {}, url=url)

    monkeypatch.setattr(ha_mod, "probe_status", fake_probe)


_CONNECTED = {
    "configured": True, "connected": True,
    "instance_name": "Brooklyn House", "version": "2026.5.1", "error": None,
}
_UNREACHABLE = {
    "configured": True, "connected": False,
    "instance_name": None, "version": None, "error": "no route to host",
}


@pytest.mark.parametrize(
    "creds, probe, status, reason",
    [
        ({}, None, "ok", "REASON_HOME_ASSISTANT_NOT_CONFIGURED"),
        (
            {"url": "http://ha.local:8123", "token": "t"},
            {"result": _CONNECTED},
            "ok",
            "REASON_HOME_ASSISTANT_CONNECTED",
        ),
        (
            {"url": "http://ha.local:8123", "token": "t"},
            {"result": _UNREACHABLE},
            "warn",
            "REASON_HOME_ASSISTANT_UNREACHABLE",
        ),
        (
            {"url": "http://ha.local:8123", "token": "t"},
            {"raises": RuntimeError("network stack exploded")},
            "warn",
            "REASON_HOME_ASSISTANT_PROBE_RAISED",
        ),
    ],
    ids=["unconfigured", "connected", "unreachable", "probe-raised"],
)
def test_check_home_assistant_verdicts(
    monkeypatch, creds, probe, status, reason
):
    """An integration is not the speaker: an unreachable or exploding
    Home Assistant is a warn a healer can act on, and never configuring one
    is operator intent."""
    if probe is not None:
        _ha_probe(monkeypatch, **probe)

    r = doctor_voice.check_home_assistant(_ha_cfg(monkeypatch, **creds))

    assert r.status == status
    assert r.reason == getattr(doctor_voice, reason)


def test_check_home_assistant_probe_raised_redacts_credential_shaped_text(
    monkeypatch,
):
    """probe_status runs with the live ha_token; a raised exception's text
    must route through `_exception_detail` (redact + cap) like every other
    crash branch, not bare `{e}` — and its literal ha_token pass must catch
    the token even in a shape none of the keyword patterns recognise (no
    `token=`/`Bearer` neighbour), unlike the keyword-matched value alongside
    it. A realistic-length token: this file's usual "t" placeholder would
    make the literal pass replace every "t" in the message.
    """
    live_token = "ha-live-Zx9pQwErTy12345"
    _ha_probe(
        monkeypatch,
        raises=RuntimeError(
            f"token=abcdef0123456789 unreachable; presented {live_token}"
        ),
    )
    creds = {"url": "http://ha.local:8123", "token": live_token}

    r = doctor_voice.check_home_assistant(_ha_cfg(monkeypatch, **creds))

    assert r.status == "warn"
    assert r.reason == doctor_voice.REASON_HOME_ASSISTANT_PROBE_RAISED
    assert "abcdef0123456789" not in r.detail
    assert live_token not in r.detail
