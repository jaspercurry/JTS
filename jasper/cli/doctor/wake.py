# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-doctor checks — wake domain."""
from __future__ import annotations

from pathlib import Path
from ...audio_profile_state import (
    AecIntent,
    resolve_audio_input_intent,
)
from ...config import Config, local_mic_present_from_env
from ...openwakeword_guard import ensure_openwakeword_import_safe
from ._evidence import evidence
from ._registry import doctor_check
from ._shared import CheckResult, _sha256_file
from .aec import (
    _aec_mode_setting,
    _aec_profile_setting,
    _chip_aec_available_for_doctor,
    _wake_leg_setting,
)

# Machine-stable codes naming which branch of a wake check produced a result
# (AGENTS.md: tests pin status + reason, never detail prose).
REASON_MODELS_DIR_MISSING = "models_dir_missing"
REASON_REQUIRED_ASSET_MISSING = "required_asset_missing"
REASON_REQUIRED_ASSET_HASH_MISMATCH = "required_asset_hash_mismatch"
REASON_ACTIVE_MODEL_MISSING = "active_model_missing"
REASON_ACTIVE_MODEL_HASH_MISMATCH = "active_model_hash_mismatch"
REASON_MODEL_CHECK_CRASHED = "model_check_crashed"

REASON_WAKE_LEGS_PUSH_TO_TALK_ONLY = "wake_legs_push_to_talk_only"
REASON_WAKE_LEGS_AEC_MODE_OFF = "wake_legs_aec_mode_off"
REASON_WAKE_LEGS_INTENT_ONLY = "wake_legs_intent_only"
REASON_WAKE_LEGS_MISSING = "wake_legs_not_armed"
REASON_WAKE_LEGS_UNEXPECTED = "wake_legs_unexpected_armed"
REASON_WAKE_LEGS_MATCH = "wake_legs_armed_matches_configured"

@doctor_check(label="openWakeWord models", needs_cfg=True)
def check_openwakeword_model(cfg: Config) -> CheckResult:
    # Must precede the openwakeword import; see jasper/openwakeword_guard.py.
    # jasper-doctor never imports jasper.wake, so without this the check pays
    # the full scikit-learn cost on every run.
    ensure_openwakeword_import_safe()
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
                reason=REASON_MODELS_DIR_MISSING,
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
                reason=REASON_REQUIRED_ASSET_MISSING,
            )
        if mismatched_assets:
            return CheckResult(
                "openWakeWord models", "fail",
                "package asset hash mismatch: "
                f"{', '.join(sorted(mismatched_assets))}; re-run deploy/install.sh",
                reason=REASON_REQUIRED_ASSET_HASH_MISMATCH,
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
                    reason=REASON_ACTIVE_MODEL_MISSING,
                )
            return CheckResult(
                "openWakeWord models", "fail",
                f"active wake model '{cfg.wake_model}' has no file in "
                f"{models_dir}; re-run deploy/install.sh",
                reason=REASON_ACTIVE_MODEL_MISSING,
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
                reason=REASON_ACTIVE_MODEL_HASH_MISMATCH,
            )
        return CheckResult(
            "openWakeWord models", "ok",
            f"{cfg.wake_model} → {active_candidate.name}",
        )
    except Exception as e:  # noqa: BLE001
        return CheckResult(
            "openWakeWord models", "fail", str(e), reason=REASON_MODEL_CHECK_CRASHED,
        )

def _voice_wake_legs_runtime() -> "set[str] | None":
    """Wake-leg tokens jasper-voice actually opened, from jasper-control's
    /state.voice.wake_legs. None when jasper-control is unreachable or the
    field is absent (older daemon / voice down) — callers treat None as
    "can't tell", not "no legs", and report configured intent instead."""
    state = evidence.control_state().payload
    if not isinstance(state, dict):
        return None
    voice = state.get("voice")
    if not isinstance(voice, dict):
        return None
    legs = voice.get("wake_legs")
    if not isinstance(legs, list):
        return None
    return {str(t) for t in legs}

def _push_to_talk_only_speaker() -> bool:
    """This box has no microphone of its own but a push-to-talk accessory —
    the one shape where jasper-voice arms ZERO wake legs deliberately.

    Reads the same two published facts ``_configured_wake_legs``
    (jasper/voice_daemon.py) reads — the AEC reconciler's
    ``JASPER_LOCAL_MIC_PRESENT`` tri-state and the accessory owner's
    published source file — but fresh on every call rather than from the
    daemon's process-start ``Config`` snapshot, which is why this still
    works when jasper-voice is down.

    Only an explicit "no local mic" counts. ``unknown`` (a custom
    ``JASPER_MIC_DEVICE`` the reconciler declines to resolve) and an absent
    key mean the daemon still plans its primary leg, so an empty armed set
    there is a real fault and must still warn.
    """
    if local_mic_present_from_env() is not False:
        return False
    # The accessory half comes from its owner's published file, read fresh —
    # never os.environ, per jasper.mic_presence. mic_presence() never raises.
    return evidence.mic_presence().accessory_present

def _assess_wake_legs(
    aec_mode: str, raw: bool, dtln: bool, armed_runtime: "set[str] | None",
    *, chip_aec: bool = False, chip_aec_150: bool = False,
    chip_aec_210: bool = False, push_to_talk_only: bool = False,
) -> CheckResult:
    """Compare configured wake-leg intent against what jasper-voice actually
    opened. Pure — the runtime set is passed in.

    Maps the operator/config vocabulary to jasper.wake_legs tokens: the
    primary/session master is "on", the "raw" toggle is the chip-direct "off"
    leg, "dtln" is "dtln", and the optional XVF3800 fixed hardware-AEC beam
    detectors are "chip_aec_150" / "chip_aec_210". `armed_runtime` is None
    when the daemon is unreachable, and then only configured intent is
    reported.

    Chip-AEC is mutually exclusive with raw/DTLN: while it is on, the
    reconciler clears the raw/DTLN device vars regardless of their booleans
    (which it preserves as wizard intent), so the effective leg set is the
    primary "on" carrier plus only the chip beams enabled in advanced
    settings. Expecting "off"/"dtln" there would false-warn that legs
    intentionally off are "not running".

    `push_to_talk_only` is that shape one step further: a speaker with no room
    mic and a paired remote arms NO legs at all, so an empty armed set is the
    design. Reported ahead of the AEC-mode skip because it describes the
    speaker's permanent shape rather than a mode setting, and because the
    alternative is a permanent yellow naming causes that cannot exist on that
    box — `wake.leg_skipped` never fires for a leg never planned."""
    hint = "Toggle at http://jts.local/wake/ (Wake detection card)."
    if push_to_talk_only:
        return CheckResult(
            "Wake legs", "skipped",
            "no local microphone and a push-to-talk accessory is "
            "paired; jasper-voice arms no wake legs on this speaker and "
            "every turn is opened by the remote's button",
            reason=REASON_WAKE_LEGS_PUSH_TO_TALK_ONLY,
        )
    if aec_mode != "auto":
        return CheckResult(
            "Wake legs", "skipped",
            f"AEC mode is {aec_mode}; additive legs require AEC on",
            reason=REASON_WAKE_LEGS_AEC_MODE_OFF,
        )
    if chip_aec:
        configured = ["primary_chip_beam"]
        expected = {"on"}
        if chip_aec_150:
            configured.append("chip_aec_150")
            expected.add("chip_aec_150")
        if chip_aec_210:
            configured.append("chip_aec_210")
            expected.add("chip_aec_210")
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
            reason=REASON_WAKE_LEGS_INTENT_ONLY,
        )
    missing = expected - armed_runtime
    if missing:
        return CheckResult(
            "Wake legs", "warn",
            f"configured {sorted(expected)} but jasper-voice armed only "
            f"{sorted(armed_runtime)}; {sorted(missing)} not running "
            f"(bridge down, chip not on 6-ch firmware, or see `journalctl "
            f"-u jasper-voice | grep event=wake.leg_skipped`). {hint}",
            reason=REASON_WAKE_LEGS_MISSING,
        )
    unexpected = armed_runtime - expected
    if unexpected:
        return CheckResult(
            "Wake legs", "warn",
            f"configured {sorted(expected)} but jasper-voice also armed "
            f"unexpected wake legs {sorted(unexpected)}; this usually means "
            f"stale runtime env or an old daemon is burning extra wake-word "
            f"instances. {hint}",
            reason=REASON_WAKE_LEGS_UNEXPECTED,
        )
    return CheckResult(
        "Wake legs", "ok",
        f"{len(armed_runtime)} leg(s) armed: "
        f"{', '.join(sorted(armed_runtime))}. {hint}",
        reason=REASON_WAKE_LEGS_MATCH,
    )

@doctor_check
def check_wake_legs_configured() -> CheckResult:
    """Reports which additive wake-detection legs are armed (raw chip-direct,
    DTLN neural, and the XVF3800 chip-AEC beam legs); check_aec_bridge_running
    reports the AEC3 master leg.

    Cross-checks configured intent from aec_mode.env against what jasper-voice
    actually opened (/state.voice.wake_legs), so a startup leg-skip surfaces
    here rather than only in the journal. Fail-soft: with jasper-control
    unreachable, reports intent alone. Skips when AEC is disabled — leg
    booleans are meaningless without the bridge emitting on the UDP ports they
    consume — and on a push-to-talk-only speaker, which arms no legs by
    design."""
    aec_mode = _aec_mode_setting()
    raw = _wake_leg_setting("JASPER_WAKE_LEG_RAW", True)
    dtln = _wake_leg_setting("JASPER_WAKE_LEG_DTLN", False)
    chip_aec = _wake_leg_setting("JASPER_WAKE_LEG_CHIP_AEC", False)
    chip_aec_150 = _wake_leg_setting("JASPER_WAKE_LEG_CHIP_AEC_150", False)
    chip_aec_210 = _wake_leg_setting("JASPER_WAKE_LEG_CHIP_AEC_210", False)
    effective = resolve_audio_input_intent(
        AecIntent(
            mode=aec_mode,
            raw_enabled=raw,
            dtln_enabled=dtln,
            chip_aec_enabled=chip_aec,
            chip_aec_150_enabled=chip_aec_150,
            chip_aec_210_enabled=chip_aec_210,
            profile_selection=_aec_profile_setting(),
        ),
        chip_available=_chip_aec_available_for_doctor(),
    )
    # Only worth a control-plane round-trip when AEC (and thus the legs) is
    # actually on; _assess_wake_legs skips otherwise.
    armed_runtime = (
        _voice_wake_legs_runtime() if effective.mode == "auto" else None
    )
    return _assess_wake_legs(
        effective.mode,
        effective.raw_enabled,
        effective.dtln_enabled,
        armed_runtime,
        chip_aec=effective.chip_aec_enabled,
        chip_aec_150=effective.chip_aec_150_enabled,
        chip_aec_210=effective.chip_aec_210_enabled,
        push_to_talk_only=_push_to_talk_only_speaker(),
    )
