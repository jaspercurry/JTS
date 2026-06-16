"""jasper-doctor checks — voice domain.

Re-homed verbatim from the original monolithic
``jasper/cli/doctor.py``; see ``jasper/cli/doctor/__init__.py``
for the package overview and ``_registry.py`` for how order is
preserved. No check logic changed in the split."""
from __future__ import annotations

import os
from pathlib import Path
from ...config import Config
from ...voice.catalog import (
    PROVIDER_IDS_MANIFEST_FILE,
    provider_by_id,
    provider_ids_manifest_text,
)
from ._registry import doctor_check
from ._shared import CheckResult

def _provider_api_key_attr(provider_id: str) -> str:
    return f"{provider_id.replace('-', '_')}_api_key"

@doctor_check(order=2, group="voice", label="provider key", needs_cfg=True)
def check_provider_key(cfg: Config) -> CheckResult:
    """Check that the active provider's API key is set and has the
    expected prefix. Other providers' keys are intentionally not
    checked — they may be set (so the wizard can switch without a
    re-paste) or not, and either is fine."""
    provider = provider_by_id(cfg.voice_provider)
    if provider is None:
        return CheckResult(
            "voice provider key", "fail",
            f"unsupported JASPER_VOICE_PROVIDER={cfg.voice_provider!r}",
        )
    env_name = provider.key_env
    prefix = provider.key_prefix_hint.rstrip(".")
    attr = _provider_api_key_attr(provider.id)
    key = getattr(cfg, attr, "")
    if not key:
        return CheckResult(
            env_name, "fail",
            f"not set; required because JASPER_VOICE_PROVIDER="
            f"{cfg.voice_provider!r}. Paste at http://jts.local/voice/ "
            f"or add to /etc/jasper/jasper.env.",
        )
    if not key.startswith(prefix):
        return CheckResult(
            env_name, "warn",
            f"doesn't start with '{prefix}' — may be a stale or wrong key",
        )
    return CheckResult(env_name, "ok", f"{key[:8]}...")

def _voice_provider_ids_manifest_path() -> Path:
    return Path(
        os.environ.get(
            "JASPER_VOICE_PROVIDER_IDS_FILE",
            PROVIDER_IDS_MANIFEST_FILE,
        ),
    )

@doctor_check(order=3, group="voice")
def check_voice_provider_ids_manifest() -> CheckResult:
    """Verify the shell-readable provider-id projection is in sync."""
    path = _voice_provider_ids_manifest_path()
    expected = provider_ids_manifest_text().splitlines()
    if not path.exists():
        return CheckResult(
            "voice provider ids",
            "fail",
            f"{path} missing — re-run install.sh to regenerate the catalog projection",
        )
    actual = path.read_text().splitlines()
    if actual == expected:
        return CheckResult(
            "voice provider ids",
            "ok",
            f"{path} matches catalog ({', '.join(expected)})",
        )
    if sorted(actual) == expected and len(actual) == len(expected):
        return CheckResult(
            "voice provider ids",
            "warn",
            f"{path} has the right ids but non-canonical order/format; re-run install.sh",
        )
    return CheckResult(
        "voice provider ids",
        "fail",
        f"{path} stale; expected {', '.join(expected)}, "
        f"got {', '.join(actual) or '<empty>'}",
    )

def _voice_tool_packs_runtime() -> "list[dict] | None":
    """Per-pack tool-registration outcomes jasper-voice actually produced,
    from jasper-control's /state.voice.tool_packs.

    None when jasper-control is unreachable or the field is absent (older
    daemon / voice down) — callers treat None as "can't tell" and fall
    back to reporting the static registry alone, rather than alarming.
    Mirrors _voice_wake_legs_runtime (wake.py)."""
    from ...control import client as control
    try:
        state = control.get_state(timeout=2)
    except (control.ControlError, ValueError):
        return None
    voice = state.get("voice")
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
    actually registered at startup. Pure (the runtime list is passed in)
    so it's unit-testable without the HTTP round-trip — mirrors
    _assess_wake_legs.

    - runtime None (control unreachable / older daemon): report the
      registry alone, ok. We can't see runtime, so we don't alarm.
    - any pack status=="failed": fail — that tool family silently
      vanished from voice.
    - a registry pack absent from the runtime report: warn (the daemon
      likely predates it; redeploy).
    - otherwise: ok with the active/gated/failed breakdown."""
    label = "Tool packs"
    if runtime is None:
        return CheckResult(
            label, "ok",
            f"{len(expected)} packs defined ({', '.join(expected)}); "
            "runtime status unavailable (jasper-control unreachable or "
            "daemon predates tool-pack telemetry).",
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
        )
    runtime_names = {p.get("name") for p in runtime}
    missing = [n for n in expected if n not in runtime_names]
    if missing:
        return CheckResult(
            label, "warn",
            f"runtime reported {len(runtime)} packs but the registry defines "
            f"{len(expected)}; not reported: {', '.join(missing)} "
            "(daemon may predate these packs — redeploy).",
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
    )


@doctor_check(order=44.5, group="voice")
def check_tool_packs() -> CheckResult:
    """Reports tool-pack registration health — registered vs. expected,
    flagging any pack that failed to build.

    Tool registration is fault-isolated per pack (jasper.tools.packs
    register_packs): a pack whose build raises contributes no tools but
    the daemon starts fine. Without this check that's observable only in
    the journal (event=tool_pack.build_failed); here a silently-missing
    tool family surfaces in jasper-doctor and the /system dashboard.

    Reads the static registry (jasper.tools.packs.TOOL_PACKS) for the
    expected set and cross-checks it against what jasper-voice actually
    registered (/state.voice.tool_packs). Fail-soft: if jasper-control is
    unreachable, reports the registry alone."""
    from ...tools.packs import TOOL_PACKS
    expected = [p.name for p in TOOL_PACKS]
    return _assess_tool_packs(expected, _voice_tool_packs_runtime())


@doctor_check(order=43, group="voice", label="daily spend cap", needs_cfg=True)
def check_spend_cap(cfg: Config) -> CheckResult:
    try:
        from ...usage import SpendCap, UsageStore
        store = UsageStore(cfg.usage_db)
        cap = SpendCap(
            store,
            cfg.daily_spend_cap_usd,
            cfg.daily_spend_cap_safety_multiplier,
        )
        if cap.disabled:
            return CheckResult(
                "daily spend cap", "ok",
                "disabled (JASPER_DAILY_SPEND_CAP_USD=0)",
            )
        remaining = cap.remaining_usd()
        if not cap.allowed():
            return CheckResult(
                "daily spend cap", "warn",
                f"24h spend reached cap (${cfg.daily_spend_cap_usd:.2f}). "
                "Voice will refuse new sessions until rollover.",
            )
        return CheckResult(
            "daily spend cap", "ok",
            f"${remaining:.4f} remaining of ${cfg.daily_spend_cap_usd:.2f}",
        )
    except Exception as e:  # noqa: BLE001
        return CheckResult("daily spend cap", "warn", str(e))

@doctor_check(order=44, group="voice", label="voice model pricing", needs_cfg=True)
def check_pricing(cfg: Config) -> CheckResult:
    """Spend estimates (and thus the cap) depend on the bundled rate data
    loading and the active model having a rate. Surface both, since a
    missing/corrupt model_pricing.json or an unpriced active model silently
    drops cost to $0 (the cap then can't bound anything)."""
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
            )
        model = cfg.active_voice_model
        if not model:
            return CheckResult(
                "voice model pricing", "ok",
                f"{len(defaults)} models priced (as of {as_of}); "
                "no active provider configured yet",
            )
        pricing = pricing_for_model(model, overrides=load_pricing_overrides())
        if pricing.label.startswith("unpriced:"):
            return CheckResult(
                "voice model pricing", "warn",
                f"active model {model!r} has no rate — cost reads $0 and the "
                "spend cap can't bound it until you set one at /voice "
                f"({len(defaults)} models priced as of {as_of})",
            )
        return CheckResult(
            "voice model pricing", "ok",
            f"active model {model} priced; {len(defaults)} bundled "
            f"(as of {as_of})",
        )
    except Exception as e:  # noqa: BLE001
        return CheckResult("voice model pricing", "warn", str(e))
