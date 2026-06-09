"""jasper-doctor checks — wake domain.

Re-homed verbatim from the original monolithic
``jasper/cli/doctor.py``; see ``jasper/cli/doctor/__init__.py``
for the package overview and ``_registry.py`` for how order is
preserved. No check logic changed in the split."""
from __future__ import annotations

from pathlib import Path
from ...audio_profile_state import (
    AecIntent,
    resolve_audio_input_intent,
)
from ...config import Config
from ._registry import doctor_check
from ._shared import CheckResult, _sha256_file
from .aec import (
    _aec_mode_setting,
    _aec_profile_setting,
    _chip_aec_available_for_doctor,
    _wake_leg_setting,
)

@doctor_check(order=8, group="wake", label="openWakeWord models", needs_cfg=True)
def check_openwakeword_model(cfg: Config) -> CheckResult:
    try:
        import openwakeword
        from ...wake_models import (
            by_model,
            openwakeword_assets,
            required_openwakeword_assets,
        )
        pkg_dir = Path(openwakeword.__file__).parent
        models_dir = pkg_dir / "resources" / "models"
        if not models_dir.exists():
            return CheckResult(
                "openWakeWord models", "fail",
                f"{models_dir} missing — re-run deploy/install.sh to stage "
                "JTS's hash-checked OpenWakeWord ONNX assets.",
            )
        missing_assets: list[str] = []
        mismatched_assets: list[str] = []
        for asset in required_openwakeword_assets():
            path = models_dir / asset.filename
            if not path.is_file() or path.stat().st_size <= 0:
                missing_assets.append(asset.filename)
                continue
            if _sha256_file(path) != asset.download_sha256:
                mismatched_assets.append(asset.filename)
        if missing_assets:
            return CheckResult(
                "openWakeWord models", "fail",
                "missing package assets: "
                f"{', '.join(sorted(missing_assets))}; re-run deploy/install.sh",
            )
        if mismatched_assets:
            return CheckResult(
                "openWakeWord models", "fail",
                "package asset hash mismatch: "
                f"{', '.join(sorted(mismatched_assets))}; re-run deploy/install.sh",
            )
        wake_model = Path(cfg.wake_model)
        if wake_model.is_absolute():
            candidates = [wake_model] if wake_model.is_file() else []
        else:
            candidates = list(models_dir.glob(f"{cfg.wake_model}*.onnx")) + list(
                models_dir.glob(f"{cfg.wake_model}*.tflite")
            )
        if not candidates:
            if wake_model.is_absolute():
                return CheckResult(
                    "openWakeWord models", "fail",
                    f"active wake model path missing: {wake_model}; "
                    "restore the custom model or choose a registered model in /wake/",
                )
            return CheckResult(
                "openWakeWord models", "fail",
                f"active wake model '{cfg.wake_model}' has no file in "
                f"{models_dir}; re-run deploy/install.sh",
            )
        active_candidate = candidates[0]
        expected_model_sha: str | None = None
        active_entry = by_model(cfg.wake_model)
        if active_entry is not None and active_entry.download_sha256:
            expected_model_sha = active_entry.download_sha256
        elif not wake_model.is_absolute():
            active_key = (
                active_entry.key
                if active_entry is not None and active_entry.bundled
                else cfg.wake_model
            )
            for asset in openwakeword_assets():
                if (
                    getattr(asset, "key", None) == active_key
                    and active_candidate.name == asset.filename
                ):
                    expected_model_sha = asset.download_sha256
                    break
        if (
            expected_model_sha is not None
            and _sha256_file(active_candidate) != expected_model_sha
        ):
            return CheckResult(
                "openWakeWord models", "fail",
                "active wake model hash mismatch: "
                f"{active_candidate.name}; re-run deploy/install.sh",
            )
        return CheckResult(
            "openWakeWord models", "ok",
            f"{cfg.wake_model} → {active_candidate.name}",
        )
    except Exception as e:  # noqa: BLE001
        return CheckResult("openWakeWord models", "fail", str(e))

def _voice_wake_legs_runtime() -> "set[str] | None":
    """Wake-leg tokens jasper-voice actually opened, from jasper-control's
    /state.voice.wake_legs (added with the registry-driven leg wiring).
    None when jasper-control is unreachable or the field is absent (older
    daemon / voice down) — callers treat None as "can't tell", not "no
    legs", and fall back to reporting configured intent."""
    from ...control import client as control
    try:
        state = control.get_state(timeout=2)
    except (control.ControlError, ValueError):
        return None
    voice = state.get("voice")
    if not isinstance(voice, dict):
        return None
    legs = voice.get("wake_legs")
    if not isinstance(legs, list):
        return None
    return {str(t) for t in legs}

def _assess_wake_legs(
    aec_mode: str, raw: bool, dtln: bool, armed_runtime: "set[str] | None",
    *, chip_aec: bool = False,
) -> CheckResult:
    """Compare configured wake-leg intent against what jasper-voice
    actually opened. Pure (the runtime set is passed in) so it's
    unit-testable without the HTTP round-trip.

    Maps the operator/config vocabulary to jasper.wake_legs tokens: the
    aec3 master is "on", the "raw" toggle is the chip-direct "off" leg,
    "dtln" is "dtln", and "chip_aec" is the pair of XVF3800 hardware-AEC
    beam legs (chip_aec_150 + chip_aec_210). `armed_runtime` is None when
    the daemon is unreachable — then we report configured intent (the
    behaviour before the runtime cross-check existed).

    Chip-AEC is single-chip mutually exclusive with raw/DTLN: when it's on,
    the reconciler clears the raw/DTLN device vars *regardless* of their
    booleans (it preserves the booleans as wizard intent), so the effective
    leg set is the two chip beams + the primary "on" carrier. We must NOT
    expect "off"/"dtln" in that mode, or this check would false-warn that
    they're "not running" when they are intentionally off."""
    hint = "Toggle at http://jts.local/wake/ (Wake detection card)."
    if aec_mode != "auto":
        return CheckResult(
            "Wake legs", "ok",
            f"n/a — AEC mode is {aec_mode}; additive legs require AEC on",
        )
    if chip_aec:
        configured = ["aec3", "chip_aec_150", "chip_aec_210"]
        expected = {"on", "chip_aec_150", "chip_aec_210"}
    else:
        configured = [name for name, on in
                      (("aec3", True), ("raw", raw), ("dtln", dtln)) if on]
        expected = {"on"}
        if raw:
            expected.add("off")
        if dtln:
            expected.add("dtln")
    if armed_runtime is None:
        return CheckResult(
            "Wake legs", "ok",
            f"{len(configured)} leg(s) configured: "
            f"{', '.join(configured)}. {hint}",
        )
    missing = expected - armed_runtime
    if missing:
        return CheckResult(
            "Wake legs", "warn",
            f"configured {sorted(expected)} but jasper-voice armed only "
            f"{sorted(armed_runtime)}; {sorted(missing)} not running "
            f"(bridge down, chip not on 6-ch firmware, or see `journalctl "
            f"-u jasper-voice | grep event=wake.leg_skipped`). {hint}",
        )
    return CheckResult(
        "Wake legs", "ok",
        f"{len(armed_runtime)} leg(s) armed: "
        f"{', '.join(sorted(armed_runtime))}. {hint}",
    )

@doctor_check(order=53, group="wake")
def check_wake_legs_configured() -> CheckResult:
    """Reports which additive wake-detection legs are armed (raw
    chip-direct, DTLN neural, and the XVF3800 chip-AEC beam legs); the
    AEC3 master leg is reported separately by check_aec_bridge_running.

    Reads configured intent from aec_mode.env and cross-checks it against
    what jasper-voice actually opened (/state.voice.wake_legs), so a
    startup leg-skip surfaces here rather than only in the journal.
    Fail-soft: if jasper-control is unreachable, reports intent alone.
    Skips cleanly if AEC is disabled — leg booleans are meaningless
    without the bridge emitting on the UDP ports they consume."""
    aec_mode = _aec_mode_setting()
    raw = _wake_leg_setting("JASPER_WAKE_LEG_RAW", True)
    dtln = _wake_leg_setting("JASPER_WAKE_LEG_DTLN", False)
    chip_aec = _wake_leg_setting("JASPER_WAKE_LEG_CHIP_AEC", False)
    effective = resolve_audio_input_intent(
        AecIntent(
            mode=aec_mode,
            raw_enabled=raw,
            dtln_enabled=dtln,
            chip_aec_enabled=chip_aec,
            profile_selection=_aec_profile_setting(),
        ),
        chip_available=_chip_aec_available_for_doctor(),
    )
    # Only worth a control-plane round-trip when AEC (and thus the legs)
    # is actually on; _assess_wake_legs returns n/a otherwise.
    armed_runtime = (
        _voice_wake_legs_runtime() if effective.mode == "auto" else None
    )
    return _assess_wake_legs(
        effective.mode,
        effective.raw_enabled,
        effective.dtln_enabled,
        armed_runtime,
        chip_aec=effective.chip_aec_enabled,
    )
