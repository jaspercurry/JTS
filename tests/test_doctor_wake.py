# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the jasper-doctor wake domain."""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from jasper import wake_models
from jasper.cli.doctor import wake

# -------------------------------------------------- openWakeWord assets

_GOOD = hashlib.sha256(b"model").hexdigest()


def _fake_asset(filename: str) -> SimpleNamespace:
    return SimpleNamespace(filename=filename, download_sha256=_GOOD)


_REQUIRED = (
    _fake_asset("embedding_model.onnx"),
    _fake_asset("melspectrogram.onnx"),
    _fake_asset("silero_vad.onnx"),
)

_PACKAGE = _REQUIRED + (
    SimpleNamespace(
        key="hey_jarvis", filename="hey_jarvis_v0.1.onnx", download_sha256=_GOOD
    ),
    SimpleNamespace(key="alexa", filename="alexa_v0.1.onnx", download_sha256=_GOOD),
)

_ALL_PRESENT = {
    "embedding_model.onnx": b"model",
    "melspectrogram.onnx": b"model",
    "silero_vad.onnx": b"model",
    "hey_jarvis_v0.1.onnx": b"model",
}


def _install_fake_openwakeword_package(
    monkeypatch, tmp_path: Path, files: dict[str, bytes]
) -> None:
    pkg = tmp_path / "openwakeword"
    models = pkg / "resources" / "models"
    models.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    for filename, payload in files.items():
        (models / filename).write_bytes(payload)
    monkeypatch.setitem(
        sys.modules,
        "openwakeword",
        SimpleNamespace(__file__=str(pkg / "__init__.py")),
    )
    monkeypatch.setattr(wake_models, "required_openwakeword_assets", lambda: _REQUIRED)
    monkeypatch.setattr(wake_models, "openwakeword_assets", lambda: _PACKAGE)


@pytest.mark.parametrize(
    "files, status, reason",
    [
        (_ALL_PRESENT, "ok", ""),
        # A required asset absent from the package tree.
        (
            {k: v for k, v in _ALL_PRESENT.items() if k != "silero_vad.onnx"},
            "fail",
            "REASON_REQUIRED_ASSET_MISSING",
        ),
        # The ACTIVE bundled model absent, every required asset present.
        (
            {k: v for k, v in _ALL_PRESENT.items() if k != "hey_jarvis_v0.1.onnx"},
            "fail",
            "REASON_ACTIVE_MODEL_MISSING",
        ),
        (
            {**_ALL_PRESENT, "silero_vad.onnx": b"wrong-model"},
            "fail",
            "REASON_REQUIRED_ASSET_HASH_MISMATCH",
        ),
        (
            {**_ALL_PRESENT, "hey_jarvis_v0.1.onnx": b"wrong-model"},
            "fail",
            "REASON_ACTIVE_MODEL_HASH_MISMATCH",
        ),
    ],
    ids=["healthy", "required-missing", "active-missing", "required-hash",
         "active-hash"],
)
def test_check_openwakeword_model_verdicts(
    monkeypatch, tmp_path: Path, files, status, reason
):
    _install_fake_openwakeword_package(monkeypatch, tmp_path, files)

    r = wake.check_openwakeword_model(SimpleNamespace(wake_model="hey_jarvis"))

    assert r.status == status
    if reason:
        assert r.reason == getattr(wake, reason)


def test_check_openwakeword_model_missing_custom_model_points_at_the_path(
    monkeypatch, tmp_path: Path
):
    _install_fake_openwakeword_package(
        monkeypatch,
        tmp_path,
        {k: v for k, v in _ALL_PRESENT.items() if k != "hey_jarvis_v0.1.onnx"},
    )
    missing = tmp_path / "custom-missing.onnx"

    r = wake.check_openwakeword_model(SimpleNamespace(wake_model=str(missing)))

    assert r.status == "fail"
    assert r.reason == wake.REASON_ACTIVE_MODEL_MISSING


def test_check_openwakeword_model_hashes_an_active_external_model(
    monkeypatch, tmp_path: Path
):
    active_model = tmp_path / "jarvis_v2.onnx"
    active_model.write_bytes(b"wrong-model")
    monkeypatch.setattr(
        wake_models,
        "by_model",
        lambda model: (
            SimpleNamespace(download_sha256=_GOOD)
            if model == str(active_model)
            else None
        ),
    )
    _install_fake_openwakeword_package(
        monkeypatch,
        tmp_path,
        {k: v for k, v in _ALL_PRESENT.items() if k != "hey_jarvis_v0.1.onnx"},
    )

    r = wake.check_openwakeword_model(SimpleNamespace(wake_model=str(active_model)))

    assert r.status == "fail"
    assert r.reason == wake.REASON_ACTIVE_MODEL_HASH_MISMATCH


# ---------------------------------------------------------------------------
# _assess_wake_legs — configured intent vs runtime-armed legs
# (the runtime cross-check added with /state.voice.wake_legs)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs, status, reason",
    [
        # AEC off: nothing to arm.
        ({"aec_mode": "disabled", "raw": True, "dtln": False,
          "armed_runtime": None}, "skipped", "REASON_WAKE_LEGS_AEC_MODE_OFF"),
        # jasper-control down: fall back to configured intent rather than
        # inventing a leg-skip warning.
        ({"aec_mode": "auto", "raw": True, "dtln": False,
          "armed_runtime": None}, "ok", "REASON_WAKE_LEGS_INTENT_ONLY"),
        ({"aec_mode": "auto", "raw": True, "dtln": True,
          "armed_runtime": {"on", "off", "dtln"}}, "ok", "REASON_WAKE_LEGS_MATCH"),
        # raw is configured on (it maps to the chip-direct "off" token) but the
        # daemon only opened the primary leg — a startup skip.
        ({"aec_mode": "auto", "raw": True, "dtln": False,
          "armed_runtime": {"on"}}, "warn", "REASON_WAKE_LEGS_MISSING"),
        # DTLN configured but not armed: model OOM, or the bridge is not
        # emitting on :9878.
        ({"aec_mode": "auto", "raw": True, "dtln": True,
          "armed_runtime": {"on", "off"}}, "warn", "REASON_WAKE_LEGS_MISSING"),
        # Chip-AEC mutual exclusion: the reconciler clears the raw/DTLN DEVICE
        # vars while preserving their booleans as wizard intent, so raw=True
        # coexists with chip_aec=True and the armed set is the primary beam
        # alone — no "off" leg, no extra beam detectors.
        ({"aec_mode": "auto", "raw": True, "dtln": False,
          "armed_runtime": {"on"}, "chip_aec": True},
         "ok", "REASON_WAKE_LEGS_MATCH"),
        ({"aec_mode": "auto", "raw": True, "dtln": False,
          "armed_runtime": {"on"}, "chip_aec": True, "chip_aec_150": True,
          "chip_aec_210": True},
         "warn", "REASON_WAKE_LEGS_MISSING"),
        # Default chip-AEC arms only the primary beam; extra armed beams are
        # resource burn.
        ({"aec_mode": "auto", "raw": True, "dtln": False,
          "armed_runtime": {"on", "chip_aec_150"}, "chip_aec": True},
         "warn", "REASON_WAKE_LEGS_UNEXPECTED"),
        ({"aec_mode": "auto", "raw": True, "dtln": False,
          "armed_runtime": None, "chip_aec": True},
         "ok", "REASON_WAKE_LEGS_INTENT_ONLY"),
        # An EMPTY armed set is the design on a push-to-talk-only speaker
        # (#2205), not a fault: no room mic, a paired remote, therefore zero
        # armed legs. Pointing an operator at a bridge, firmware, or
        # wake.leg_skipped would name causes that cannot exist there.
        ({"aec_mode": "auto", "raw": True, "dtln": False,
          "armed_runtime": set(), "push_to_talk_only": True},
         "skipped", "REASON_WAKE_LEGS_PUSH_TO_TALK_ONLY"),
        # The control: the same empty set on an ORDINARY speaker (a bridge
        # that died, a leg that failed to open) still warns.
        ({"aec_mode": "auto", "raw": True, "dtln": False,
          "armed_runtime": set()}, "warn", "REASON_WAKE_LEGS_MISSING"),
        # A leg the reconciler APPLIED (its device var is set, so the daemon
        # planned it) that is absent from the live set is a dead task, not a
        # config divergence — and it outranks the intent comparison.
        ({"aec_mode": "auto", "raw": True, "dtln": False,
          "armed_runtime": {"on"}, "applied": {"off"}},
         "fail", "REASON_WAKE_LEGS_DEAD"),
        # The same leg running: no dead verdict.
        ({"aec_mode": "auto", "raw": True, "dtln": False,
          "armed_runtime": {"on", "off"}, "applied": {"off"}},
         "ok", "REASON_WAKE_LEGS_MATCH"),
    ],
    ids=[
        "aec-disabled",
        "daemon-unreachable",
        "runtime-matches",
        "raw-leg-skipped",
        "dtln-skipped",
        "chip-primary-only",
        "chip-extra-beams-unarmed",
        "chip-unexpected-beam",
        "chip-intent-only",
        "push-to-talk",
        "empty-armed-set",
        "applied-leg-dead",
        "applied-leg-running",
    ],
)
def test_assess_wake_legs_verdicts(kwargs, status, reason):
    r = wake._assess_wake_legs(**kwargs)

    assert r.status == status
    assert r.reason == getattr(wake, reason)


@pytest.mark.parametrize(
    "local_mic, accessories, expected",
    [
        ("0", ("wiim_remote_2",), True),
        # No accessory: a box with no input at all, which must NOT be excused.
        ("0", (), False),
        # An accessory on a box whose mic is present, or whose verdict is not
        # an explicit "absent", still arms its legs normally.
        ("1", ("wiim_remote_2",), False),
        ("unknown", ("wiim_remote_2",), False),
        (None, ("wiim_remote_2",), False),
    ],
    ids=["both-facts", "no-accessory", "mic-present", "mic-unknown", "mic-unset"],
)
def test_push_to_talk_only_speaker_needs_both_published_facts(
    monkeypatch, local_mic, accessories, expected
):
    """The derivation ANDs the reconcilers' two published facts — the same
    pairing `_configured_wake_legs` requires."""
    from jasper import mic_presence as mic_presence_mod
    from jasper.mic_presence import MicPresence

    monkeypatch.setattr(
        mic_presence_mod,
        "read_mic_presence",
        lambda *a, **k: MicPresence(present=True, accessory_sources=accessories),
    )
    if local_mic is None:
        monkeypatch.delenv("JASPER_LOCAL_MIC_PRESENT", raising=False)
    else:
        monkeypatch.setenv("JASPER_LOCAL_MIC_PRESENT", local_mic)

    assert wake._push_to_talk_only_speaker() is expected
