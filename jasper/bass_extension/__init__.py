"""Bass-extension natural-graph commit and crash-recovery ownership."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import tempfile
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from jasper.atomic_io import atomic_write_text

BASS_EXTENSION_RUNTIME_ADAPTER_IDS = frozenset({"sealed_v1"})
BASS_EXTENSION_APPLY_INTENT_PATH = Path(
    "/var/lib/jasper/bass_extension_apply_intent.json"
)


class BassExtensionApplyError(RuntimeError):
    """The two-authority natural graph/profile commit was refused or restored."""


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _profile_bytes(profile) -> bytes:
    return (
        json.dumps(profile.to_dict(), indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _durable_unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path.parent, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _profile_entry(raw: bytes | None) -> dict[str, Any]:
    return {
        "present": raw is not None,
        "bytes": raw.decode("utf-8") if raw is not None else None,
        "sha256": _sha256(raw) if raw is not None else None,
    }


def _parse_profile_entry(value: Any) -> bytes | None:
    if not isinstance(value, Mapping) or type(value.get("present")) is not bool:
        raise BassExtensionApplyError("bass-extension intent profile entry is invalid")
    text = value.get("bytes")
    digest = value.get("sha256")
    if value["present"] is False:
        if text is not None or digest is not None:
            raise BassExtensionApplyError("absent predecessor profile marker is invalid")
        return None
    if not isinstance(text, str):
        raise BassExtensionApplyError("bass-extension intent profile bytes are invalid")
    raw = text.encode("utf-8")
    if digest != _sha256(raw):
        raise BassExtensionApplyError("bass-extension intent profile fingerprint is invalid")
    return raw


def _selected_path(statefile_path: Path) -> Path:
    from jasper.active_speaker.environment import parse_camilla_statefile_config_path

    try:
        raw = statefile_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BassExtensionApplyError("CamillaDSP boot selector is unavailable") from exc
    selected = parse_camilla_statefile_config_path(raw)
    if not selected:
        raise BassExtensionApplyError("CamillaDSP boot selector has no config_path")
    return Path(selected)


def _normal_fingerprint(text: str) -> str:
    import yaml

    from jasper.audio_measurement.evidence_identity import NormalizedActiveRawIdentity

    try:
        parsed = yaml.safe_load(text)
        if not isinstance(parsed, dict) or not parsed:
            raise ValueError
        return NormalizedActiveRawIdentity(parsed).active_raw_fingerprint
    except (ValueError, yaml.YAMLError) as exc:
        raise BassExtensionApplyError("CamillaDSP graph cannot be normalized") from exc


def _load_applied(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BassExtensionApplyError("immutable applied baseline is unavailable") from exc
    if not isinstance(raw, dict) or raw.get("status") != "applied":
        raise BassExtensionApplyError("immutable applied baseline is not applied")
    return raw


def _bonded_or_driver_carrier(selected_text: str) -> bool:
    from jasper.active_speaker.environment import (
        CAMILLA_CLASS_PROGRAM_BAKE,
        classify_camilla_config_text,
    )
    from jasper.active_speaker.runtime_contract import ACTIVE_DRIVER_DOMAIN_SOURCE
    from jasper.multiroom.config import is_active_member, load_config

    if is_active_member(load_config()):
        return True
    summary = classify_camilla_config_text(selected_text)
    return (
        summary.get("classification") == CAMILLA_CLASS_PROGRAM_BAKE
        or summary.get("source") == ACTIVE_DRIVER_DOMAIN_SOURCE
    )


async def _active_proof(
    *,
    topology,
    controller,
    statefile_path: Path,
    applied_baseline_path: Path,
    profile_path: Path,
    intent_path: Path,
    staged_metadata_path: Path,
) -> None:
    from jasper.active_speaker.runtime_contract import (
        classify_active_bass_extension_graph,
    )

    proof = await classify_active_bass_extension_graph(
        topology,
        statefile_path=statefile_path,
        read_active_graph_text=lambda: controller.get_active_config_raw(
            best_effort=False
        ),
        applied_baseline_path=applied_baseline_path,
        profile_path=profile_path,
        intent_path=intent_path,
        staged_metadata_path=staged_metadata_path,
    )
    if not proof.allowed:
        code = proof.issues[0]["code"] if proof.issues else proof.classification
        raise BassExtensionApplyError(f"bass-extension graph proof failed: {code}")


async def _reload_and_match(
    controller,
    *,
    selected_path: Path,
    expected_bytes: bytes,
    expected_graph_fingerprint: str,
    statefile_path: Path,
) -> None:
    if not await controller.reload(best_effort=False):
        raise BassExtensionApplyError("CamillaDSP rejected the selected graph reload")
    live_path = await controller.get_config_file_path(best_effort=False)
    live_text = await controller.get_active_config_raw(best_effort=False)
    if (
        live_path is None
        or Path(live_path) != selected_path
        or not isinstance(live_text, str)
        or _selected_path(statefile_path) != selected_path
        or selected_path.read_bytes() != expected_bytes
        or _normal_fingerprint(live_text) != expected_graph_fingerprint
    ):
        raise BassExtensionApplyError("CamillaDSP graph/path readback did not match")


def _intent_payload(
    *,
    predecessor_identity,
    predecessor_profile_bytes: bytes | None,
    desired_profile_bytes: bytes,
    selected_path: Path,
    selected_mode: int,
    predecessor_graph_bytes: bytes,
    desired_graph_bytes: bytes,
    selector_target: Path,
) -> dict[str, Any]:
    predecessor_fp = _normal_fingerprint(predecessor_graph_bytes.decode("utf-8"))
    desired_fp = _normal_fingerprint(desired_graph_bytes.decode("utf-8"))
    return {
        "kind": "jts_bass_extension_apply_intent",
        "schema_version": 1,
        "operation_id": uuid.uuid4().hex,
        "predecessor_identity": predecessor_identity.to_dict(),
        "profiles": {
            "predecessor": _profile_entry(predecessor_profile_bytes),
            "desired": _profile_entry(desired_profile_bytes),
        },
        "graphs": {"predecessor": predecessor_fp, "desired": desired_fp},
        "config": {
            "path": str(selected_path),
            "mode": selected_mode,
            "predecessor_bytes": predecessor_graph_bytes.decode("utf-8"),
            "predecessor_sha256": _sha256(predecessor_graph_bytes),
            "desired_bytes": desired_graph_bytes.decode("utf-8"),
            "desired_sha256": _sha256(desired_graph_bytes),
        },
        "boot_selector_target": str(selector_target),
    }


def _read_intent(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BassExtensionApplyError("pending bass-extension intent is unreadable") from exc
    if (
        not isinstance(value, dict)
        or value.get("kind") != "jts_bass_extension_apply_intent"
        or type(value.get("schema_version")) is not int
        or value.get("schema_version") != 1
    ):
        raise BassExtensionApplyError("pending bass-extension intent is malformed")
    return value


async def _restore_locked(
    intent: Mapping[str, Any],
    *,
    topology,
    controller,
    statefile_path: Path,
    applied_baseline_path: Path,
    profile_path: Path,
    intent_path: Path,
    staged_metadata_path: Path,
    config_dir: Path,
) -> None:
    from jasper.audio_measurement.evidence_identity import ExactDspStateIdentity

    config = intent.get("config")
    profiles = intent.get("profiles")
    graphs = intent.get("graphs")
    operation_id = intent.get("operation_id")
    if (
        not isinstance(config, Mapping)
        or not isinstance(profiles, Mapping)
        or not isinstance(graphs, Mapping)
        or not isinstance(operation_id, str)
        or len(operation_id) != 32
        or any(ch not in "0123456789abcdef" for ch in operation_id)
    ):
        raise BassExtensionApplyError("pending bass-extension intent payload is invalid")
    try:
        config_path = config["path"]
        mode = config["mode"]
        predecessor_text = config["predecessor_bytes"]
        desired_text = config["desired_bytes"]
        if (
            not isinstance(config_path, str)
            or not config_path
            or config_path.strip() != config_path
            or type(mode) is not int
            or mode < 0
            or mode > 0o7777
            or not isinstance(predecessor_text, str)
            or not isinstance(desired_text, str)
        ):
            raise TypeError
        selected = Path(config_path)
        predecessor_bytes = predecessor_text.encode("utf-8")
        desired_bytes = desired_text.encode("utf-8")
        predecessor_fp = graphs["predecessor"]
        desired_fp = graphs["desired"]
        if not isinstance(predecessor_fp, str) or not isinstance(desired_fp, str):
            raise TypeError
        ExactDspStateIdentity.from_mapping(intent["predecessor_identity"])
    except (KeyError, TypeError, ValueError) as exc:
        raise BassExtensionApplyError("pending bass-extension intent payload is invalid") from exc
    if (
        config.get("predecessor_sha256") != _sha256(predecessor_bytes)
        or config.get("desired_sha256") != _sha256(desired_bytes)
        or _normal_fingerprint(predecessor_text) != predecessor_fp
        or _normal_fingerprint(desired_text) != desired_fp
        or selected.resolve().parent != config_dir.resolve()
        or _selected_path(statefile_path) != selected
        or intent.get("boot_selector_target") != str(selected)
    ):
        raise BassExtensionApplyError("pending bass-extension predecessor identity is invalid")
    predecessor_profile = _parse_profile_entry(profiles.get("predecessor"))
    desired_profile = _parse_profile_entry(profiles.get("desired"))
    if desired_profile is None:
        raise BassExtensionApplyError("pending desired bass-extension profile is absent")
    atomic_write_text(
        selected,
        predecessor_text,
        mode=mode,
        group_from_parent=True,
        durable=True,
    )
    await _reload_and_match(
        controller,
        selected_path=selected,
        expected_bytes=predecessor_bytes,
        expected_graph_fingerprint=predecessor_fp,
        statefile_path=statefile_path,
    )
    if predecessor_profile is None:
        _durable_unlink(profile_path)
    else:
        atomic_write_text(
            profile_path,
            predecessor_profile.decode("utf-8"),
            mode=0o640,
            group_from_parent=True,
            durable=True,
        )
    await _active_proof(
        topology=topology,
        controller=controller,
        statefile_path=statefile_path,
        applied_baseline_path=applied_baseline_path,
        profile_path=profile_path,
        intent_path=intent_path,
        staged_metadata_path=staged_metadata_path,
    )
    _durable_unlink(intent_path)


async def recover_pending_bass_extension_apply(
    *,
    topology=None,
    controller=None,
    statefile_path: str | Path = "/var/lib/camilladsp/outputd-statefile.yml",
    applied_baseline_path: str | Path = "/var/lib/jasper/active_speaker_baseline_profile.json",
    profile_path: str | Path = "/var/lib/jasper/bass_extension_profile.json",
    intent_path: str | Path = BASS_EXTENSION_APPLY_INTENT_PATH,
    staged_metadata_path: str | Path = "/var/lib/jasper/active_speaker_staged_config.json",
    config_dir: str | Path = "/var/lib/camilladsp/configs",
) -> bool:
    """Idempotently restore one surviving predecessor and clear its intent."""

    from jasper.camilla import CamillaController
    from jasper.dsp_apply import dsp_writer_lock
    from jasper.output_topology import load_output_topology_strict

    intent_target = Path(intent_path)
    topology = topology or load_output_topology_strict()
    controller = controller or CamillaController("127.0.0.1", 1234)
    async with dsp_writer_lock(
        config_dir,
        source="bass_extension.recovery",
        allow_pending_bass_extension_recovery=True,
        bass_extension_intent_path=intent_target,
    ):
        intent = _read_intent(intent_target)
        if intent is None:
            return False
        await _restore_locked(
            intent,
            topology=topology,
            controller=controller,
            statefile_path=Path(statefile_path),
            applied_baseline_path=Path(applied_baseline_path),
            profile_path=Path(profile_path),
            intent_path=intent_target,
            staged_metadata_path=Path(staged_metadata_path),
            config_dir=Path(config_dir),
        )
    return True


async def apply_bass_extension(
    desired_profile,
    *,
    topology=None,
    controller=None,
    statefile_path: str | Path = "/var/lib/camilladsp/outputd-statefile.yml",
    applied_baseline_path: str | Path = "/var/lib/jasper/active_speaker_baseline_profile.json",
    profile_path: str | Path = "/var/lib/jasper/bass_extension_profile.json",
    intent_path: str | Path = BASS_EXTENSION_APPLY_INTENT_PATH,
    staged_metadata_path: str | Path = "/var/lib/jasper/active_speaker_staged_config.json",
    config_dir: str | Path = "/var/lib/camilladsp/configs",
    preference_profile_path: str | Path | None = None,
    sound_settings_path: str | Path | None = None,
    validate=None,
) -> None:
    """Commit one desired profile and its natural-at-rest graph atomically."""

    from jasper.active_speaker.runtime_contract import (
        GRAPH_APPROVED_ACTIVE_RUNTIME,
        classify_bass_extension_graph,
    )
    from jasper.audio_measurement.evidence_identity import ExactDspStateIdentity
    from jasper.bass_extension.profile import (
        BassExtensionProfile,
        evaluate_loaded_bass_extension_profile,
        load_bass_extension_profile,
        save_bass_extension_profile,
    )
    from jasper.camilla import CamillaController
    from jasper.dsp_apply import dsp_writer_lock, validate_camilla_config
    from jasper.output_topology import load_output_topology_strict
    from jasper.sound.graph_carrier import (
        recompose_active_baseline_for_bass_extension,
    )

    if not isinstance(desired_profile, BassExtensionProfile):
        raise BassExtensionApplyError("desired profile must be a BassExtensionProfile")
    if (
        desired_profile.status == "accepted"
        and desired_profile.enclosure["adapter_id"] == "sealed_v1"
        and any(target.subsonic is None for target in desired_profile.targets)
    ):
        raise BassExtensionApplyError(
            "sealed bass-extension targets all require subsonic protection"
        )
    topology = topology or load_output_topology_strict()
    controller = controller or CamillaController("127.0.0.1", 1234)
    validator = validate or validate_camilla_config
    statefile = Path(statefile_path)
    applied_path = Path(applied_baseline_path)
    profile_target = Path(profile_path)
    intent_target = Path(intent_path)
    staged_path = Path(staged_metadata_path)
    configs = Path(config_dir)
    rollback_after_lock = None
    forward_failure: BaseException | None = None

    async with dsp_writer_lock(
        configs,
        source="bass_extension.apply",
        allow_pending_bass_extension_recovery=True,
        bass_extension_intent_path=intent_target,
    ):
        # Bonded program-bake/driver-domain roles are a hard owner-boundary
        # refusal.  Check them before even an older rollback can mutate either
        # authority; the correction host will retry recovery once the local
        # speaker is solo again.
        selected_before_recovery = _selected_path(statefile)
        try:
            selected_before_recovery_text = selected_before_recovery.read_text(
                encoding="utf-8"
            )
        except (OSError, UnicodeError) as exc:
            raise BassExtensionApplyError(
                "selected CamillaDSP config is not readable"
            ) from exc
        if _bonded_or_driver_carrier(selected_before_recovery_text):
            raise BassExtensionApplyError(
                "bass-extension apply is unavailable while an active speaker is bonded"
            )

        older = _read_intent(intent_target)
        if older is not None:
            await _restore_locked(
                older,
                topology=topology,
                controller=controller,
                statefile_path=statefile,
                applied_baseline_path=applied_path,
                profile_path=profile_target,
                intent_path=intent_target,
                staged_metadata_path=staged_path,
                config_dir=configs,
            )
        selected = _selected_path(statefile)
        try:
            if selected.resolve().parent != configs.resolve():
                raise BassExtensionApplyError(
                    "selected CamillaDSP config is outside the writable config directory"
                )
            predecessor_graph_bytes = selected.read_bytes()
            selected_mode = stat.S_IMODE(selected.stat().st_mode)
            predecessor_text = predecessor_graph_bytes.decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise BassExtensionApplyError("selected CamillaDSP config is not restorable") from exc
        live_path = await controller.get_config_file_path(best_effort=False)
        if live_path is None or Path(live_path) != selected:
            raise BassExtensionApplyError("live graph path differs from the boot selector")
        if _bonded_or_driver_carrier(predecessor_text):
            raise BassExtensionApplyError(
                "bass-extension apply is unavailable while an active speaker is bonded"
            )

        applied = _load_applied(applied_path)
        predecessor_profile_bytes = (
            profile_target.read_bytes() if profile_target.exists() else None
        )
        predecessor_profile = load_bass_extension_profile(profile_target)
        current_runtime_profile = None
        if predecessor_profile is not None:
            current_eval = evaluate_loaded_bass_extension_profile(
                predecessor_profile,
                topology=topology,
                applied_baseline_state=applied,
            )
            if current_eval.status == "accepted":
                current_runtime_profile = predecessor_profile
        desired_eval = evaluate_loaded_bass_extension_profile(
            desired_profile,
            topology=topology,
            applied_baseline_state=applied,
        )
        if desired_profile.status == "accepted" and desired_eval.status != "accepted":
            raise BassExtensionApplyError("desired bass-extension profile is not current")

        natural_text = recompose_active_baseline_for_bass_extension(
            topology,
            applied_profile=applied,
            desired_profile=current_runtime_profile,
            current_config_path=selected,
            preference_profile_path=preference_profile_path,
            sound_settings_path=sound_settings_path,
        )
        natural_bytes = natural_text.encode("utf-8")
        atomic_write_text(
            selected,
            natural_text,
            mode=selected_mode,
            group_from_parent=True,
            durable=True,
        )
        await _reload_and_match(
            controller,
            selected_path=selected,
            expected_bytes=natural_bytes,
            expected_graph_fingerprint=_normal_fingerprint(natural_text),
            statefile_path=statefile,
        )
        await _active_proof(
            topology=topology,
            controller=controller,
            statefile_path=statefile,
            applied_baseline_path=applied_path,
            profile_path=profile_target,
            intent_path=intent_target,
            staged_metadata_path=staged_path,
        )
        predecessor_graph_bytes = selected.read_bytes()
        predecessor_text = predecessor_graph_bytes.decode("utf-8")
        active_raw = await controller.get_active_config_raw(best_effort=False)
        if not isinstance(active_raw, str):
            raise BassExtensionApplyError("natural predecessor active graph is unavailable")
        predecessor_identity = ExactDspStateIdentity({
            "active_raw": active_raw,
            "normalized_active_raw_fingerprint": _normal_fingerprint(active_raw),
            "config_path": str(selected),
        })

        desired_text = recompose_active_baseline_for_bass_extension(
            topology,
            applied_profile=applied,
            desired_profile=desired_profile,
            current_config_path=selected,
            preference_profile_path=preference_profile_path,
            sound_settings_path=sound_settings_path,
        )
        desired_proof = classify_bass_extension_graph(
            topology,
            evidence_source="desired",
            graph_text=desired_text,
            applied_baseline_state=applied,
            desired_profile=desired_profile,
        )
        if (
            not desired_proof.allowed
            or desired_proof.classification != GRAPH_APPROVED_ACTIVE_RUNTIME
        ):
            raise BassExtensionApplyError("desired natural graph failed pre-publication proof")
        fd, scratch_name = tempfile.mkstemp(
            prefix=".bass-extension-validate-", suffix=".yml", dir=configs
        )
        os.close(fd)
        scratch = Path(scratch_name)
        try:
            atomic_write_text(scratch, desired_text, mode=selected_mode)
            validation = validator(scratch)
            if not validation.ok_to_apply:
                raise BassExtensionApplyError("desired natural graph failed CamillaDSP validation")
            desired_graph_bytes = scratch.read_bytes()
        finally:
            try:
                scratch.unlink()
            except FileNotFoundError:
                pass
        desired_profile_bytes = _profile_bytes(desired_profile)
        intent = _intent_payload(
            predecessor_identity=predecessor_identity,
            predecessor_profile_bytes=predecessor_profile_bytes,
            desired_profile_bytes=desired_profile_bytes,
            selected_path=selected,
            selected_mode=selected_mode,
            predecessor_graph_bytes=predecessor_graph_bytes,
            desired_graph_bytes=desired_graph_bytes,
            selector_target=_selected_path(statefile),
        )
        # Reassert predecessor durability before publishing the rollback record.
        atomic_write_text(
            selected,
            predecessor_text,
            mode=selected_mode,
            group_from_parent=True,
            durable=True,
        )
        if selected.read_bytes() != predecessor_graph_bytes:
            raise BassExtensionApplyError("predecessor durability reassertion failed")
        atomic_write_text(
            intent_target,
            json.dumps(intent, indent=2, sort_keys=True) + "\n",
            mode=0o640,
            group_from_parent=True,
            durable=True,
        )

        async def rollback() -> None:
            async with dsp_writer_lock(
                configs,
                source="bass_extension.apply_rollback",
                allow_pending_bass_extension_recovery=True,
                bass_extension_intent_path=intent_target,
            ):
                current_intent = _read_intent(intent_target)
                if current_intent is None:
                    return
                if current_intent != intent:
                    raise BassExtensionApplyError(
                        "bass-extension rollback intent ownership changed"
                    )
                await _restore_locked(
                    current_intent,
                    topology=topology,
                    controller=controller,
                    statefile_path=statefile,
                    applied_baseline_path=applied_path,
                    profile_path=profile_target,
                    intent_path=intent_target,
                    staged_metadata_path=staged_path,
                    config_dir=configs,
                )

        rollback_after_lock = rollback
        try:
            graph_changed = desired_graph_bytes != predecessor_graph_bytes
            if graph_changed:
                atomic_write_text(
                    selected,
                    desired_graph_bytes.decode("utf-8"),
                    mode=selected_mode,
                    group_from_parent=True,
                    durable=True,
                )
                await _reload_and_match(
                    controller,
                    selected_path=selected,
                    expected_bytes=desired_graph_bytes,
                    expected_graph_fingerprint=_normal_fingerprint(desired_text),
                    statefile_path=statefile,
                )
            save_bass_extension_profile(desired_profile, profile_target)
            await _active_proof(
                topology=topology,
                controller=controller,
                statefile_path=statefile,
                applied_baseline_path=applied_path,
                profile_path=profile_target,
                intent_path=intent_target,
                staged_metadata_path=staged_path,
            )
            _durable_unlink(intent_target)
        except BaseException as exc:  # noqa: BLE001 - preserve cancellation/failure
            forward_failure = exc

    if forward_failure is None:
        return
    if rollback_after_lock is None:  # pragma: no cover - intent pins this closure
        raise forward_failure
    restore = asyncio.create_task(rollback_after_lock())
    cancelled_during_restore = isinstance(forward_failure, asyncio.CancelledError)
    while not restore.done():
        try:
            await asyncio.shield(restore)
        except asyncio.CancelledError:
            cancelled_during_restore = True
    restore.result()
    if cancelled_during_restore:
        raise asyncio.CancelledError()
    raise forward_failure


async def bypass_bass_extension(*, profile_path: str | Path = "/var/lib/jasper/bass_extension_profile.json", **kwargs) -> None:
    """Construct a bypassed profile and delegate to the sole commit owner."""

    from jasper.bass_extension.profile import load_bass_extension_profile

    current = load_bass_extension_profile(profile_path)
    if current is None:
        raise BassExtensionApplyError("there is no bass-extension profile to bypass")
    await apply_bass_extension(
        replace(current, status="bypassed"),
        profile_path=profile_path,
        **kwargs,
    )
