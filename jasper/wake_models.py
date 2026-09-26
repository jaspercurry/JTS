# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Curated catalogue of wake-word models the speaker can run.

One source of truth, consumed by three callers:
  - `install.sh` decides which openWakeWord package assets and
    non-bundled `.onnx` files to fetch.
  - The `/assistant/wake/` web wizard (`jasper/web/wake_setup.py`) renders one
    row per entry, and it and `jasper-settings wake` switch models through
    `select_wake_model`, the selection's one writer (ADR-0350).
  - The voice daemon's `Config.wake_model` resolves the active
    selection (a registry key OR a raw path/stock name the operator
    set by hand) into something `WakeWordDetector` can load.

Entries are deliberately a small curated list — not every wake-word
file out there. The aim is "household member taps Settings, picks
between four options that we trust." Hand-rolled custom models still
work: set `JASPER_WAKE_MODEL=/abs/path/to/foo.onnx` directly in
`/etc/jasper/jasper.env`, and the wake daemon will load it. The
wizard surfaces such hand-rolled paths with a `Custom` row so the
operator's choice isn't silently overwritten.

Adding a model:
  1. Drop a new `WakeModelEntry` below. For openWakeWord-stock names
     like `alexa`, set `bundled=True` and leave `download_url` empty.
     If the stock model is not already listed in
     `OPENWAKEWORD_ASSETS`, add its ONNX asset there too.
  2. For external `.onnx` files, set `download_url` to a raw URL +
     `download_sha256` to the expected SHA-256, and `model` to the
     absolute path under `/var/lib/jasper/wake/`. install.sh will
     pull and verify it idempotently on the next deploy.
  3. Re-run `bash scripts/deploy-to-pi.sh` to install the new model
     on existing speakers. Existing households' active selections are
     preserved (the wizard only writes `wake_model.env` when the user
     picks something).
"""
from __future__ import annotations

import importlib.util
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from jasper.atomic_io import atomic_write_text, locked_update_env_file
from jasper.env_load import WAKE_MODEL_ENV_PATH
from jasper.log_event import log_event

if TYPE_CHECKING:
    from jasper.model_downloads import StageAsset

logger = logging.getLogger(__name__)


# The systemd unit for jasper-voice sources this AFTER /etc/jasper/jasper.env,
# so wizard-written values win over operator-managed defaults — same pattern
# as voice_provider.env and spotify_credentials.env.
WAKE_MODEL_FILE = WAKE_MODEL_ENV_PATH
#: Also the header of jasper-control's sensitivity-slider write
#: (JASPER_WAKE_THRESHOLD), which the /assistant/wake/ page drives too.
WAKE_MODEL_ENV_OWNER = "jasper.wake_models; change it at /assistant/wake/ or with jasper-settings"

# Where install.sh stages downloaded non-bundled models. Files here
# survive package reinstalls because they live under /var/lib, not
# /opt/jasper (which install.sh rewrites). Owner: root; mode 0644 so
# the voice daemon (also root) can mmap them at startup.
WAKE_MODELS_DIR = "/var/lib/jasper/wake"


@dataclass(frozen=True)
class WakeModelEntry:
    """One row in the wake-word picker.

    `model` is what gets passed to `WakeWordDetector(model_name=...)`:
      - bundled openWakeWord names (`hey_jarvis`, `alexa`, ...) are
        resolved by openwakeword.model.Model to its packaged ONNX
        bundle.
      - absolute paths to a `.onnx` file outside that bundle are
        loaded by file path. The path MUST exist at daemon startup
        or the daemon will fail to start (caught at install time:
        install.sh seeds wake_model.env only when the file is
        present, and `is_available()` keeps a row whose file isn't
        downloaded yet from being selected).

    `fa_per_hour` is the trainer/author's published self-report — not
    independently measured. Treat as ballpark, not guarantee.
    """

    key: str
    label: str
    pronunciation: str
    description: str
    model: str
    fa_per_hour: float | None
    source_url: str
    download_url: str | None = None
    download_sha256: str | None = None
    bundled: bool = False
    recommended: bool = False


@dataclass(frozen=True)
class OpenWakeWordAsset:
    """A stock ONNX file that openWakeWord expects under resources/models.

    JTS runs openWakeWord with `inference_framework="onnx"` because
    tflite-runtime does not ship a Python 3.13 wheel for PiOS Trixie.
    These are therefore the exact package-resource files install.sh
    stages through the hash-checked asset manifest below.
    """

    key: str
    filename: str
    download_url: str
    download_sha256: str


OPENWAKEWORD_RELEASE = "v0.5.1"
OPENWAKEWORD_RELEASE_BASE = (
    f"https://github.com/dscripka/openWakeWord/releases/download/{OPENWAKEWORD_RELEASE}"
)

OPENWAKEWORD_ASSETS: tuple[OpenWakeWordAsset, ...] = (
    OpenWakeWordAsset(
        key="embedding_model",
        filename="embedding_model.onnx",
        download_url=f"{OPENWAKEWORD_RELEASE_BASE}/embedding_model.onnx",
        download_sha256="70d164290c1d095d1d4ee149bc5e00543250a7316b59f31d056cff7bd3075c1f",
    ),
    OpenWakeWordAsset(
        key="melspectrogram",
        filename="melspectrogram.onnx",
        download_url=f"{OPENWAKEWORD_RELEASE_BASE}/melspectrogram.onnx",
        download_sha256="ba2b0e0f8b7b875369a2c89cb13360ff53bac436f2895cced9f479fa65eb176f",
    ),
    OpenWakeWordAsset(
        key="silero_vad",
        filename="silero_vad.onnx",
        download_url=f"{OPENWAKEWORD_RELEASE_BASE}/silero_vad.onnx",
        download_sha256="a35ebf52fd3ce5f1469b2a36158dba761bc47b973ea3382b3186ca15b1f5af28",
    ),
    OpenWakeWordAsset(
        key="alexa",
        filename="alexa_v0.1.onnx",
        download_url=f"{OPENWAKEWORD_RELEASE_BASE}/alexa_v0.1.onnx",
        download_sha256="6ff566a01d12670e8d9e3c59da32651db1575d17272a601b7f8a39283dfbae3e",
    ),
    OpenWakeWordAsset(
        key="hey_mycroft",
        filename="hey_mycroft_v0.1.onnx",
        download_url=f"{OPENWAKEWORD_RELEASE_BASE}/hey_mycroft_v0.1.onnx",
        download_sha256="c2a311e8fa1338de89c31b3b46dc4dffd4af2f9a8d6ddead48893c2d301b1f18",
    ),
    OpenWakeWordAsset(
        key="hey_jarvis",
        filename="hey_jarvis_v0.1.onnx",
        download_url=f"{OPENWAKEWORD_RELEASE_BASE}/hey_jarvis_v0.1.onnx",
        download_sha256="94a13cfe60075b132f6a472e7e462e8123ee70861bc3fb58434a73712ee0d2cb",
    ),
    OpenWakeWordAsset(
        key="hey_rhasspy",
        filename="hey_rhasspy_v0.1.onnx",
        download_url=f"{OPENWAKEWORD_RELEASE_BASE}/hey_rhasspy_v0.1.onnx",
        download_sha256="5a9b3ed3be2910e35780e097905aa9f35a9c10038df47914cf2b3ec4d670f6ea",
    ),
    OpenWakeWordAsset(
        key="timer",
        filename="timer_v0.1.onnx",
        download_url=f"{OPENWAKEWORD_RELEASE_BASE}/timer_v0.1.onnx",
        download_sha256="371e44535470a29248b3b8f1bbbbaf2525c86417fd8f75c67fcf02ae0b9626df",
    ),
    OpenWakeWordAsset(
        key="weather",
        filename="weather_v0.1.onnx",
        download_url=f"{OPENWAKEWORD_RELEASE_BASE}/weather_v0.1.onnx",
        download_sha256="8441da8e746899e8d969528d5bad5651cdd563079c05962788f77753041f60e7",
    ),
)

OPENWAKEWORD_REQUIRED_RUNTIME_ASSET_KEYS = frozenset({
    "embedding_model",
    "melspectrogram",
    "silero_vad",
})
OPENWAKEWORD_FALLBACK_ASSET_KEYS = frozenset({
    # Config.from_env falls back to "hey_jarvis" when no wizard/operator
    # wake model is configured. Keep that package asset fail-fast at
    # install time so a failed default-model download can still land on
    # a working fresh install.
    "hey_jarvis",
})


# ---- Registry ---------------------------------------------------------

# Order matters — this is the display order on the picker page. Put the
# recommended default first so a new household lands on it.
REGISTRY: tuple[WakeModelEntry, ...] = (
    # Upstream terms verified 2026-06-12: the fwartner
    # home-assistant-wakewords-collection repository is MIT-licensed
    # (copyright Florian Wartner), and this model is downloaded from the
    # pinned 8bcd2f20bb7b76c351b2eff871fa1ce873fe9be2 commit. The
    # jarvis_v2.onnx upstream blob at that commit is
    # b2995e5a5224582d3e15880fe6f1b716c9129b2f.
    WakeModelEntry(
        key="jarvis_v2",
        label="Jarvis",
        pronunciation='Say "Jarvis" — "Hey Jarvis" still works too',
        description=(
            "Community-trained MIT-licensed model from the Home "
            "Assistant wake-words collection. Trained on the phrase set "
            '“jarvis” / “hey jarvis” / “jarvis!” / “jarvis?”, '
            "so both forms trigger it. Author-reported ~0.18 false fires "
            "per hour, well inside openWakeWord's <0.5/hour target. "
            "Worth knowing: any MCU/Iron Man content nearby will trigger "
            'it — Tony Stark says "JARVIS" a lot.'
        ),
        model=f"{WAKE_MODELS_DIR}/jarvis_v2.onnx",
        fa_per_hour=0.18,
        source_url="https://github.com/fwartner/home-assistant-wakewords-collection",
        download_url=(
            "https://raw.githubusercontent.com/fwartner/"
            "home-assistant-wakewords-collection/"
            "8bcd2f20bb7b76c351b2eff871fa1ce873fe9be2/"
            "en/jarvis/jarvis_v2.onnx"
        ),
        download_sha256="dae408c0fa69ec888bf8e3a8b41a41f97677522be3b8163821e4105fa754b988",
        recommended=True,
    ),
    WakeModelEntry(
        key="hey_jarvis",
        label="Hey Jarvis",
        pronunciation='Say "Hey Jarvis"',
        description=(
            "Original openWakeWord-bundled model. Requires the "
            '“hey” precursor. Pre-2026-05 default for JTS.'
        ),
        model="hey_jarvis",
        fa_per_hour=0.5,
        source_url="https://github.com/dscripka/openWakeWord",
        bundled=True,
    ),
    WakeModelEntry(
        key="alexa",
        label="Alexa",
        pronunciation='Say "Alexa"',
        description=(
            "openWakeWord-bundled model. Highest accuracy of the stock "
            "set per dscripka's benchmarks. Warning: any Amazon Echo in "
            "earshot also triggers on this phrase, so don't pick this if "
            "you have one in the same room."
        ),
        model="alexa",
        fa_per_hour=0.5,
        source_url="https://github.com/dscripka/openWakeWord",
        bundled=True,
    ),
    WakeModelEntry(
        key="hey_mycroft",
        label="Hey Mycroft",
        pronunciation='Say "Hey Mycroft"',
        description=(
            "openWakeWord-bundled model from the Mycroft AI project. "
            "Stock alternative if Jarvis or Alexa don't suit."
        ),
        model="hey_mycroft",
        fa_per_hour=0.5,
        source_url="https://github.com/dscripka/openWakeWord",
        bundled=True,
    ),
)


DEFAULT_KEY = "jarvis_v2"


# ---- Lookup helpers ---------------------------------------------------

def by_key(key: str) -> WakeModelEntry | None:
    """Find a registry entry by its short id (e.g. "jarvis_v2")."""
    for entry in REGISTRY:
        if entry.key == key:
            return entry
    return None


def by_model(model: str) -> WakeModelEntry | None:
    """Reverse-lookup a registry entry from the `model` string the
    daemon was configured with. Returns None when the configured
    model isn't one of ours (e.g. operator pointed JASPER_WAKE_MODEL
    at a custom .onnx the wizard doesn't know about) — the caller
    should treat that as a "Custom" row."""
    for entry in REGISTRY:
        if entry.model == model:
            return entry
    return None


def downloadable() -> Iterable[WakeModelEntry]:
    """Iterate entries that install.sh has to fetch over the network.
    Bundled openWakeWord names are excluded; their package-resource
    ONNX files are tracked separately in `OPENWAKEWORD_ASSETS`."""
    for entry in REGISTRY:
        if entry.download_url:
            yield entry


def openwakeword_assets() -> Iterable[OpenWakeWordAsset]:
    """Iterate openWakeWord package-resource ONNX files install.sh owns."""
    return iter(OPENWAKEWORD_ASSETS)


def openwakeword_asset_by_key(key: str) -> OpenWakeWordAsset | None:
    """Find an openWakeWord package-resource asset by registry/stock key."""
    for asset in OPENWAKEWORD_ASSETS:
        if asset.key == key:
            return asset
    return None


def required_openwakeword_assets() -> Iterable[OpenWakeWordAsset]:
    """Iterate the ONNX assets openWakeWord needs before any wake model runs."""
    return (
        asset
        for asset in OPENWAKEWORD_ASSETS
        if asset.key in OPENWAKEWORD_REQUIRED_RUNTIME_ASSET_KEYS
    )


def fallback_openwakeword_assets() -> Iterable[OpenWakeWordAsset]:
    """Iterate stock assets needed for the compiled-in wake fallback."""
    return (
        asset
        for asset in OPENWAKEWORD_ASSETS
        if asset.key in OPENWAKEWORD_FALLBACK_ASSET_KEYS
    )


def openwakeword_asset_for_model(model: str) -> OpenWakeWordAsset | None:
    """Return the package asset for a bare stock wake-model string.

    ``JASPER_WAKE_MODEL`` may be a registry model value ("hey_jarvis")
    or an operator-set stock name that is not shown in the picker
    ("timer"). Absolute/external ONNX paths return ``None`` because
    those are staged under /var/lib/jasper/wake instead.
    """
    if "/" in model or model.endswith((".onnx", ".tflite")):
        return None
    entry = by_model(model)
    key = entry.key if entry is not None and entry.bundled else model
    return openwakeword_asset_by_key(key)


def default() -> WakeModelEntry:
    """The entry install.sh seeds into a fresh /var/lib/jasper/wake_model.env.
    Defined here (not as a constant) so changing DEFAULT_KEY is the
    single edit needed to retarget the default."""
    entry = by_key(DEFAULT_KEY)
    if entry is None:
        raise RuntimeError(
            f"DEFAULT_KEY {DEFAULT_KEY!r} not in REGISTRY — "
            "update jasper/wake_models.py"
        )
    return entry


# ---- Selecting a model (ADR-0350) ---------------------------------------

def is_available(entry: WakeModelEntry) -> bool:
    """Return whether the model can be selected without crashing voice.

    Bundled openWakeWord names are install-owned package resources. We
    check their resource path via importlib metadata rather than importing
    openwakeword on every page render. External files have to exist on
    disk to be loadable; a missing file means a failed install-time
    download, flagged in the UI so the household knows what's going on.
    """
    if entry.bundled:
        asset_path = _bundled_asset_path(entry)
        if asset_path is None:
            return False
        try:
            return asset_path.is_file() and asset_path.stat().st_size > 0
        except OSError:
            return False
    return os.path.exists(entry.model)


def _bundled_asset_path(entry: WakeModelEntry) -> Path | None:
    asset = openwakeword_asset_by_key(entry.key)
    if asset is None:
        return None
    spec = importlib.util.find_spec("openwakeword")
    if spec is None or spec.origin is None:
        return None
    return Path(spec.origin).resolve().parent / "resources" / "models" / asset.filename


class WakeModelRefused(ValueError):
    """:func:`select_wake_model` declined; ``reason`` is the slug a front end prints."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


@dataclass(frozen=True)
class WakeSelection:
    """The entry :func:`select_wake_model` selected, and the sensitivity the
    file still carries (``""`` when unset)."""

    entry: WakeModelEntry
    threshold: str


def select_wake_model(
    key: str, *, via: str, client: str | None = None, path: str | None = None,
) -> WakeSelection:
    """Select the registry entry ``key`` names for every front end.

    Refuses (:class:`WakeModelRefused`) a key outside the registry and a model
    not on this speaker. Only ``JASPER_WAKE_MODEL`` is written, under the lock
    the sensitivity slider's writer shares, so its threshold survives. ``via``
    names the front end on the ``event=wake.model`` line. Raises OSError when
    the file cannot be written.
    """
    entry = by_key(key)
    if entry is None:
        raise WakeModelRefused(
            "unknown_model",
            f"Unknown model: {key!r}. Choose one of: "
            f"{', '.join(e.key for e in REGISTRY)}.",
        )
    if not is_available(entry):
        raise WakeModelRefused(
            "not_downloaded",
            f"{entry.label} isn't downloaded yet on this speaker. Re-run "
            "`bash scripts/deploy-to-pi.sh` to fetch it, then try again.",
        )
    state = locked_update_env_file(
        path or WAKE_MODEL_FILE,
        {"JASPER_WAKE_MODEL": entry.model},
        mode=0o644,
        owner=WAKE_MODEL_ENV_OWNER,
    )
    log_event(logger, "wake.model", model=entry.model, via=via, client=client)
    return WakeSelection(entry, state.get("JASPER_WAKE_THRESHOLD", ""))


# ---- install.sh staging (jasper.model_downloads is a leaf; this registry
# builds its own StageAsset lists rather than being reached into) --------

def openwakeword_stage_assets(
    models_dir: str | os.PathLike[str],
    *,
    active_model: str | None = None,
) -> list[StageAsset]:
    from jasper.model_downloads import StageAsset  # lazy: pulls ssl/urllib into runtime importers

    required_by_key = {asset.key for asset in required_openwakeword_assets()}
    required_by_key.update(asset.key for asset in fallback_openwakeword_assets())
    if active_model:
        active_asset = openwakeword_asset_for_model(active_model)
        if active_asset is not None:
            required_by_key.add(active_asset.key)

    base = Path(models_dir)
    return [
        StageAsset(
            key=asset.key,
            label="openWakeWord asset",
            dest=base / asset.filename,
            url=asset.download_url,
            expected_sha256=asset.download_sha256,
            required=asset.key in required_by_key,
        )
        for asset in openwakeword_assets()
    ]


def wake_model_stage_assets(*, required: bool) -> list[StageAsset]:
    from jasper.model_downloads import StageAsset  # lazy: pulls ssl/urllib into runtime importers

    return [
        StageAsset(
            key=entry.key,
            label="wake model",
            dest=Path(entry.model),
            url=entry.download_url or "",
            expected_sha256=entry.download_sha256,
            required=required,
        )
        for entry in downloadable()
    ]


def seed_default_wake_model_env() -> None:
    if os.path.exists(WAKE_MODEL_FILE):
        return
    entry = default()
    if not os.path.exists(entry.model):
        print(f"  skipping wake_model.env seed: default file missing ({entry.model})")
        return
    atomic_write_text(WAKE_MODEL_FILE, f"JASPER_WAKE_MODEL={entry.model}\n")
    print(f"  seeded {WAKE_MODEL_FILE} -> {entry.key} ({entry.model})")
