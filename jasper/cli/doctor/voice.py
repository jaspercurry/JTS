# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-doctor checks — voice domain."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from ...config import Config
from ...voice.catalog import (
    PROVIDER_IDS_MANIFEST_FILE,
    provider_by_id,
    provider_ids_manifest_text,
)
from ...voice.provider_state import (
    read_active_model_from_env_files,
    read_active_provider_state,
)
from ._evidence import evidence
from ._registry import doctor_check
from ...secret_redaction import redact_secrets
from ._shared import (
    _EXCEPTION_DETAIL_LIMIT,
    CheckResult,
    REASON_VOICE_UNIT_NOT_FULL_PROFILE,
    _run,
)

# Machine-stable codes naming which branch of a voice check produced a
# result (AGENTS.md: tests pin status + reason, never detail prose).
REASON_PROVIDER_SELECTION_INVALID = "provider_key_selection_invalid"
REASON_PROVIDER_SELECTION_UNREADABLE = "provider_key_selection_unreadable"
REASON_PROVIDER_NOT_CONFIGURED = "provider_key_not_configured"
REASON_PROVIDER_KEY_MISSING = "provider_key_missing"
REASON_PROVIDER_KEY_WRONG_PREFIX = "provider_key_wrong_prefix"
REASON_PROVIDER_KEY_OK_ENV_DRIFT = "provider_key_ok_env_drift"

REASON_PROVIDER_IMPORTS_UNDETERMINED = "provider_imports_selection_undetermined"
REASON_PROVIDER_IMPORTS_NOT_CONFIGURED = "provider_imports_not_configured"
REASON_PROVIDER_IMPORTS_TIMEOUT = "provider_imports_timed_out"
REASON_PROVIDER_IMPORTS_FAILED = "provider_imports_failed"

REASON_MANIFEST_MISSING = "manifest_missing"
REASON_MANIFEST_CURRENT = "manifest_current"
REASON_MANIFEST_NONCANONICAL = "manifest_noncanonical_order"
REASON_MANIFEST_STALE = "manifest_stale"
REASON_MANIFEST_NOT_RENDERED = "manifest_not_rendered_for_profile"

REASON_TOOL_PACKS_RUNTIME_UNAVAILABLE = "tool_packs_runtime_unavailable"
REASON_TOOL_PACKS_BUILD_FAILED = "tool_packs_build_failed"
REASON_TOOL_PACKS_MISSING_FROM_RUNTIME = "tool_packs_missing_from_runtime"
REASON_TOOL_PACKS_HEALTHY = "tool_packs_healthy"

REASON_SPEND_CAP_DISABLED = "spend_cap_disabled"
REASON_SPEND_CAP_NO_USAGE = "spend_cap_no_usage_recorded"
REASON_SPEND_CAP_REACHED = "spend_cap_reached"
REASON_SPEND_CAP_OK = "spend_cap_within_budget"
REASON_SPEND_CAP_UNREADABLE = "spend_cap_unreadable"

REASON_PRICING_DATA_UNAVAILABLE = "pricing_data_unavailable"
REASON_PRICING_MODEL_UNDETERMINED = "pricing_active_model_undetermined"
REASON_PRICING_MODEL_NOT_CONFIGURED = "pricing_active_model_not_configured"
REASON_PRICING_MODEL_UNPRICED = "pricing_active_model_unpriced"
REASON_PRICING_MODEL_PRICED = "pricing_active_model_priced"
REASON_PRICING_UNREADABLE = "pricing_unreadable"

# check_provider_importable, check_tool_packs and check_pricing share
# resilience.check_voice_unit_running's ADR-0217 gate (check_provider_key
# and check_spend_cap need Config fields the streambox cfg lacks; see
# _registry.STREAMBOX_OMITTED_DOCTOR_CHECKS).
def _voice_gated_skip(label: str) -> CheckResult | None:
    if evidence.streambox_awaiting_accessory():
        return CheckResult(
            label, "skipped",
            "streambox tier with no mic-bearing remote paired — the "
            "assistant runs only while one is (ADR-0217)",
            reason=REASON_VOICE_UNIT_NOT_FULL_PROFILE,
        )
    return None


def _provider_api_key_attr(provider_id: str) -> str:
    return f"{provider_id.replace('-', '_')}_api_key"


# WHICH provider is active has exactly one authority here: the wizard-owned
# SSOT file via read_active_provider_state() — never Config/os.environ, where
# a calling-shell export outranks the file (env_load.load_env_files uses
# setdefault) and would name a different provider than jasper-voice runs. The
# model is read from the same files for the same reason; Config stays the
# authority for the API key GIVEN a provider.
#
# The statuses where the file cannot name a provider. check_provider_key
# adjudicates a bad or unreadable selection; its neighbours defer to it and
# only report "can't tell".
_PROVIDER_UNDETERMINED = frozenset({"unreadable", "invalid"})


@doctor_check(label="provider key", needs_cfg=True)
def check_provider_key(cfg: Config) -> CheckResult:
    """Check that the active provider's API key is set and has the expected
    prefix. Other providers' keys may be set (so the wizard can switch without
    a re-paste) or not, and either is fine.

    Also the module's adjudicator for the *selection* itself: an unreadable or
    unsupported SSOT value is reported here, once.

    Gated statically — see ``_registry.STREAMBOX_OMITTED_DOCTOR_CHECKS``."""
    state = read_active_provider_state()
    env_provider = cfg.voice_provider
    if state.status == "invalid":
        return CheckResult(
            "voice provider key", "fail", f"{state.detail} in {state.path}",
            reason=REASON_PROVIDER_SELECTION_INVALID,
        )
    if state.status == "unreadable":
        return CheckResult(
            "voice provider key", "warn",
            f"active provider undetermined ({state.detail}); no key checked",
            reason=REASON_PROVIDER_SELECTION_UNREADABLE,
        )
    if not state.configured:
        # The file is the canonical home for this selection, so a provider
        # that exists only in this process's environment is drift, not config.
        return CheckResult(
            "voice provider key", "warn",
            f"{state.detail}, but this process's environment names "
            f"{env_provider!r} — no key checked. Pick a provider at "
            f"http://jts.local/voice/ so every surface agrees.",
            reason=REASON_PROVIDER_NOT_CONFIGURED,
        )

    provider = provider_by_id(state.provider)
    assert provider is not None  # read_active_provider_state validated it
    env_name = provider.key_env
    # The key verdict is always about the SSOT provider; a disagreeing
    # environment is itself a finding rather than a silent tiebreak.
    drift = (
        ""
        if env_provider == state.provider
        else (
            f" — note {state.provider} is active per {state.path} while "
            f"this process's environment names {env_provider!r}; the key "
            f"checked is the active provider's"
        )
    )
    key = getattr(cfg, _provider_api_key_attr(provider.id), "")
    if not key:
        return CheckResult(
            env_name, "fail",
            f"not set; required because {state.provider} is the active "
            f"provider. Paste at http://jts.local/voice/ "
            f"or add to /etc/jasper/jasper.env.{drift}",
            reason=REASON_PROVIDER_KEY_MISSING,
        )
    prefix = provider.key_prefix_hint.rstrip(".")
    if not key.startswith(prefix):
        return CheckResult(
            env_name, "warn",
            f"doesn't start with '{prefix}' — may be a stale or wrong "
            f"key{drift}",
            reason=REASON_PROVIDER_KEY_WRONG_PREFIX,
        )
    if drift:
        return CheckResult(
            env_name, "warn", f"configured{drift}",
            reason=REASON_PROVIDER_KEY_OK_ENV_DRIFT,
        )
    return CheckResult(
        env_name, "ok", "configured",
    )

# Imports the module names handed to it on argv, in order, reporting the FIRST
# failure. A child interpreter rather than in-process for two reasons: a fresh
# process is what jasper-voice actually is, so a module that imports only
# because the doctor already loaded something is not mistaken for a working
# provider; and the adapters pull in heavy SDKs a child frees on exit instead
# of carrying through the rest of the run on a 1 GB Pi.
_IMPORT_PROBE = (
    "import importlib, sys\n"
    "for name in sys.argv[1:]:\n"
    "    try:\n"
    "        importlib.import_module(name)\n"
    "    except Exception as e:\n"
    "        print(f'{name}\\t{type(e).__name__}: {e}')\n"
    "        raise SystemExit(1)\n"
)

# `-P` (3.11+) stops the interpreter putting the caller's cwd on sys.path.
# `python -c` sets sys.path[0] = '' by default, so a doctor run started from
# the rsync checkout would resolve `jasper.voice.*` there instead of in the
# /opt/jasper runtime and report green on a box whose daemon still loads a
# broken adapter. The probe needs nothing from cwd.
_PROBE_INTERPRETER_FLAGS = ("-P",)

# The probe must finish inside the row guard this run is using: a row
# cancelled first releases the memory-sample lock with subprocess.run's child
# still resident (see check_provider_importable's exclusive_group below) and
# makes this check's own could-not-verify warn unreachable.
_IMPORT_PROBE_MARGIN_SEC = 5.0
_IMPORT_PROBE_MIN_TIMEOUT_SEC = 1.0


def _import_probe_timeout() -> float:
    """Subprocess timeout for the import probe: strictly below the doctor's
    per-row guard, so the child is always killed by its own timeout first."""
    row = evidence.check_timeout()
    return max(
        min(_IMPORT_PROBE_MIN_TIMEOUT_SEC, row / 2),
        row - _IMPORT_PROBE_MARGIN_SEC,
    )


# Shares the "memory-sample" exclusive lane with check_memory_headroom: the
# import child is the largest allocation jasper-doctor makes (the gemini
# adapter peaks near 83 MB RSS, dropping MemAvailable by ~70 MB for ~1.5 s),
# and check_memory_headroom warns below 100 MB available on a 1 GB Pi, so an
# unserialized probe could trip that threshold itself.
@doctor_check(exclusive_group="memory-sample")
def check_provider_importable() -> CheckResult:
    """Check that the *configured* voice provider's adapter and its
    lazily-imported SDK can actually be imported in this venv.

    Neither the ``/voice`` wizard nor ``switch-voice-provider.sh`` verifies
    that the selected provider's code loads, so a venv missing one package
    otherwise surfaces only as a jasper-voice that will not start.

    Precisely: a fresh interpreter can import each module in the provider's
    ``runtime_imports``, covering both ways the daemon dies on a missing
    package — ``_make_connection``'s adapter import and the adapter's own
    deferred SDK import. It does NOT prove jasper-voice starts (keys, mic,
    ALSA, network are separate) and opens no session.
    """
    if (skip := _voice_gated_skip("voice provider imports")) is not None:
        return skip
    state = read_active_provider_state()
    if state.status in _PROVIDER_UNDETERMINED:
        return CheckResult(
            "voice provider imports", "skipped",
            f"active provider undetermined ({state.detail}); imports not checked",
            reason=REASON_PROVIDER_IMPORTS_UNDETERMINED,
        )
    if not state.configured:
        return CheckResult(
            "voice provider imports", "ok",
            "no provider configured — pick one in the /voice wizard",
            reason=REASON_PROVIDER_IMPORTS_NOT_CONFIGURED,
        )

    provider = provider_by_id(state.provider)
    assert provider is not None  # read_active_provider_state validated it
    modules = list(provider.runtime_imports)
    joined = ", ".join(modules)
    timeout = _import_probe_timeout()
    try:
        proc = _run(
            [sys.executable, *_PROBE_INTERPRETER_FLAGS, "-c", _IMPORT_PROBE,
             *modules],
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            "voice provider imports", "warn",
            f"{state.provider}: import probe timed out after "
            f"{timeout:.0f}s ({joined}) — could not verify",
            reason=REASON_PROVIDER_IMPORTS_TIMEOUT,
        )
    if proc.returncode == 0:
        return CheckResult(
            "voice provider imports", "ok",
            f"{state.provider}: {joined} import cleanly "
            f"(import-only; not a live-session probe)",
        )
    reported = (proc.stdout or "").strip().splitlines()
    if reported:
        # The probe's own one-line report: "<module>\t<ExcType>: <message>".
        failure = reported[0].replace("\t", ": ")
    else:
        # The child died before it could report (import-time crash, signal).
        crash = (proc.stderr or "").strip().splitlines()
        failure = crash[-1] if crash else "unknown import failure"
    # Arbitrary text from a child's traceback goes through the doctor's
    # redaction + length policy, not straight into the report.
    failure = redact_secrets(failure)
    if len(failure) > _EXCEPTION_DETAIL_LIMIT:
        failure = failure[:_EXCEPTION_DETAIL_LIMIT - 3] + "..."
    return CheckResult(
        "voice provider imports", "fail",
        f"{state.provider} is the active provider but its code will not "
        f"import: {failure}. jasper-voice cannot start with this provider "
        f"selected — re-run the installer (bash scripts/deploy-to-pi.sh) to "
        f"rebuild the venv, or select a provider that loads in the /voice "
        f"wizard.",
        reason=REASON_PROVIDER_IMPORTS_FAILED,
    )


def _voice_provider_ids_manifest_path() -> Path:
    return Path(
        os.environ.get(
            "JASPER_VOICE_PROVIDER_IDS_FILE",
            PROVIDER_IDS_MANIFEST_FILE,
        ),
    )

@doctor_check()
def check_voice_provider_ids_manifest() -> CheckResult:
    """Verify the shell-readable provider-id projection is in sync.

    Missing or stale parks jasper-voice — `jasper-aec-reconcile` accepts the
    active provider only as an exact line here — so both `fail` (ADR-0165;
    non-negotiable 6). Non-canonical order still matches its `grep -Fxq`.

    On a streambox `jasper-aec-reconcile` is not the input-gate owner
    (ADR-0217) and `install_streambox_jasper` never renders this file
    (`deploy/lib/install/python-runtime.sh`), so its absence there is a
    profile fact, not a fault."""
    if evidence.install_profile_is_streambox():
        return CheckResult(
            "voice provider ids", "skipped",
            "not rendered on the streambox profile — only the full "
            "profile's installer writes this projection",
            reason=REASON_MANIFEST_NOT_RENDERED,
        )
    path = _voice_provider_ids_manifest_path()
    expected = provider_ids_manifest_text().splitlines()
    if not path.exists():
        return CheckResult(
            "voice provider ids",
            "fail",
            f"{path} missing — re-run install.sh to regenerate the catalog projection",
            reason=REASON_MANIFEST_MISSING,
        )
    actual = path.read_text().splitlines()
    if actual == expected:
        return CheckResult(
            "voice provider ids",
            "ok",
            f"{path} matches catalog ({', '.join(expected)})",
            reason=REASON_MANIFEST_CURRENT,
        )
    if sorted(actual) == expected and len(actual) == len(expected):
        return CheckResult(
            "voice provider ids",
            "ok",
            f"{path} has the right ids but non-canonical order/format; re-run install.sh",
            reason=REASON_MANIFEST_NONCANONICAL,
        )
    return CheckResult(
        "voice provider ids",
        "fail",
        f"{path} stale; expected {', '.join(expected)}, "
        f"got {', '.join(actual) or '<empty>'}",
        reason=REASON_MANIFEST_STALE,
    )

def _voice_tool_packs_runtime() -> "list[dict] | None":
    """Per-pack tool-registration outcomes jasper-voice actually produced,
    from jasper-control's /state.voice.tool_packs.

    None when jasper-control is unreachable or the field is absent (older
    daemon / voice down) — callers treat None as "can't tell" and report the
    static registry alone rather than alarming."""
    payload = evidence.control_state().payload
    if not isinstance(payload, dict):
        return None
    voice = payload.get("voice")
    if not isinstance(voice, dict):
        return None
    packs = voice.get("tool_packs")
    if not isinstance(packs, list):
        return None
    return packs


def _assess_tool_packs(
    expected: list[str], runtime: "list[dict] | None",
) -> CheckResult:
    """Compare the static tool-pack registry against what jasper-voice
    actually registered at startup. Pure — the runtime list is passed in.

    - runtime None (control unreachable / older daemon): registration was
      not observed at all, so skipped, naming the registry.
    - any pack status=="failed": fail — that tool family silently
      vanished from voice.
    - a registry pack absent from the runtime report: warn (the daemon
      likely predates it; redeploy).
    - otherwise: ok with the active/gated/failed breakdown."""
    label = "Tool packs"
    if runtime is None:
        return CheckResult(
            label, "skipped",
            f"{len(expected)} packs defined ({', '.join(expected)}); "
            "runtime status unavailable (jasper-control unreachable or "
            "daemon predates tool-pack telemetry).",
            reason=REASON_TOOL_PACKS_RUNTIME_UNAVAILABLE,
        )
    failed = [p for p in runtime if p.get("status") == "failed"]
    if failed:
        detail = "; ".join(
            f"{p.get('name')}: {p.get('error') or 'build failed'}"
            for p in failed
        )
        return CheckResult(
            label, "fail",
            f"{len(failed)} of {len(runtime)} tool pack(s) failed to build — "
            f"those tool families are silently missing from voice: {detail}. "
            "See `journalctl -u jasper-voice | grep event=tool_pack.build_failed`.",
            reason=REASON_TOOL_PACKS_BUILD_FAILED,
        )
    runtime_names = {p.get("name") for p in runtime}
    missing = [n for n in expected if n not in runtime_names]
    if missing:
        return CheckResult(
            label, "warn",
            f"runtime reported {len(runtime)} packs but the registry defines "
            f"{len(expected)}; not reported: {', '.join(missing)} "
            "(daemon may predate these packs — redeploy).",
            reason=REASON_TOOL_PACKS_MISSING_FROM_RUNTIME,
        )
    registered = [p for p in runtime if p.get("status") == "registered"]
    skipped = [p for p in runtime if p.get("status") == "skipped"]
    extra = (
        f"; {len(skipped)} gated off "
        f"({', '.join(str(p.get('name')) for p in skipped)})"
        if skipped else ""
    )
    return CheckResult(
        label, "ok",
        f"{len(registered)}/{len(runtime)} packs active, 0 failed{extra}.",
        reason=REASON_TOOL_PACKS_HEALTHY,
    )


@doctor_check()
def check_tool_packs() -> CheckResult:
    """Reports tool-pack registration health — registered vs. expected,
    flagging any pack that failed to build.

    Tool registration is fault-isolated per pack: a pack whose build raises
    contributes no tools but the daemon starts fine, which is otherwise
    observable only in the journal (event=tool_pack.build_failed).

    Cross-checks the static registry (jasper.tools.packs.TOOL_PACKS) against
    what jasper-voice actually registered (/state.voice.tool_packs).
    Fail-soft: with jasper-control unreachable, reports the registry alone."""
    if (skip := _voice_gated_skip("Tool packs")) is not None:
        return skip
    from ...tools.packs import TOOL_PACKS
    expected = [p.name for p in TOOL_PACKS]
    return _assess_tool_packs(expected, _voice_tool_packs_runtime())


@doctor_check(label="daily spend cap", needs_cfg=True)
def check_spend_cap(cfg: Config) -> CheckResult:
    """Gated statically — see ``_registry.STREAMBOX_OMITTED_DOCTOR_CHECKS``."""
    try:
        from ...usage import (
            SpendCap,
            household_usage_reader,
            tuning_usage_db_path,
        )
        if cfg.daily_spend_cap_usd <= 0:
            return CheckResult(
                "daily spend cap", "ok",
                "disabled (JASPER_DAILY_SPEND_CAP_USD=0)",
                reason=REASON_SPEND_CAP_DISABLED,
            )
        # The doctor runs as root: opening a usage DB read-write here would
        # create or re-own it and lock the owning daemon out of its own DB.
        # household_usage_reader opens every member read_only.
        tuning_db = tuning_usage_db_path(cfg.usage_db)
        # Household spend folds in jasper-correction-web's paid tuning calls;
        # the note says whether that sibling ledger exists yet.
        ledger_note = (
            "includes tuning ledger"
            if os.path.exists(tuning_db)
            else "tuning ledger absent"
        )
        if not os.path.exists(cfg.usage_db) and not os.path.exists(tuning_db):
            return CheckResult(
                "daily spend cap", "ok",
                f"no usage recorded yet; $0.00 of "
                f"${cfg.daily_spend_cap_usd:.2f} used ({ledger_note})",
                reason=REASON_SPEND_CAP_NO_USAGE,
            )
        cap = SpendCap(
            household_usage_reader(cfg.usage_db),
            cfg.daily_spend_cap_usd,
            cfg.daily_spend_cap_safety_multiplier,
        )
        remaining = cap.remaining_usd()
        if not cap.allowed():
            return CheckResult(
                "daily spend cap", "warn",
                f"24h spend reached cap (${cfg.daily_spend_cap_usd:.2f}). "
                "Voice will refuse new sessions until rollover.",
                reason=REASON_SPEND_CAP_REACHED,
            )
        return CheckResult(
            "daily spend cap", "ok",
            f"${remaining:.4f} remaining of ${cfg.daily_spend_cap_usd:.2f} "
            f"({ledger_note})",
            reason=REASON_SPEND_CAP_OK,
        )
    except Exception as e:  # noqa: BLE001
        return CheckResult(
            "daily spend cap", "warn", str(e), reason=REASON_SPEND_CAP_UNREADABLE,
        )

@doctor_check(label="voice model pricing")
def check_pricing() -> CheckResult:
    """Spend estimates (and thus the cap) depend on the bundled rate data
    loading and the active model having a rate. Surface both, since a
    missing/corrupt model_pricing.json or an unpriced active model silently
    drops cost to $0 (the cap then can't bound anything)."""
    if (skip := _voice_gated_skip("voice model pricing")) is not None:
        return skip
    try:
        from ...usage import (
            load_default_pricing,
            load_pricing_overrides,
            pricing_for_model,
        )
        defaults, as_of = load_default_pricing()
        if not defaults:
            return CheckResult(
                "voice model pricing", "warn",
                "model_pricing.json failed to load — every model is unpriced, "
                "so cost reads $0 and the spend cap can't bound it. Re-deploy.",
                reason=REASON_PRICING_DATA_UNAVAILABLE,
            )
        # Active provider AND model from the merged env files — never from
        # Config/os.environ, where a shell-exported JASPER_<PROVIDER>_MODEL
        # outranks both jasper.env and the wizard file (load_env_files uses
        # setdefault) and would name a model jasper-voice does not run.
        state = read_active_provider_state()
        model = (
            read_active_model_from_env_files(state.provider)
            if state.configured else ""
        )
        if not model:
            unchecked = (
                f"{len(defaults)} models priced (as of {as_of}); "
                f"active model not checked ({state.detail or 'no model'})"
            )
            if state.status in _PROVIDER_UNDETERMINED:
                return CheckResult(
                    "voice model pricing", "skipped", unchecked,
                    reason=REASON_PRICING_MODEL_UNDETERMINED,
                )
            return CheckResult(
                "voice model pricing", "ok", unchecked,
                reason=REASON_PRICING_MODEL_NOT_CONFIGURED,
            )
        pricing = pricing_for_model(model, overrides=load_pricing_overrides())
        if pricing.label.startswith("unpriced:"):
            return CheckResult(
                "voice model pricing", "warn",
                f"active model {model!r} has no rate — cost reads $0 and the "
                "spend cap can't bound it until you set one at /voice "
                f"({len(defaults)} models priced as of {as_of})",
                reason=REASON_PRICING_MODEL_UNPRICED,
            )
        return CheckResult(
            "voice model pricing", "ok",
            f"active model {model} priced; {len(defaults)} bundled "
            f"(as of {as_of})",
            reason=REASON_PRICING_MODEL_PRICED,
        )
    except Exception as e:  # noqa: BLE001
        return CheckResult(
            "voice model pricing", "warn", str(e), reason=REASON_PRICING_UNREADABLE,
        )
