"""Curated catalogue of wake-word models the speaker can run.

One source of truth, consumed by three callers:
  - `install.sh` decides which openWakeWord package assets and
    non-bundled `.onnx` files to fetch.
  - The `/wake/` web wizard (`jasper/web/wake_setup.py`) renders one
    row per entry so the household can flip models without SSH.
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

from dataclasses import dataclass
from typing import Iterable


# Persisted at /var/lib/jasper/wake_model.env. The systemd unit for
# jasper-voice sources this AFTER /etc/jasper/jasper.env, so wizard-
# written values win over operator-managed defaults — same pattern as
# voice_provider.env and spotify_credentials.env.
WAKE_MODEL_FILE = "/var/lib/jasper/wake_model.env"

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
        present, and the wizard's _available_models() filter hides
        rows whose file isn't downloaded yet).

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
    stages and hash-checks instead of delegating to
    `openwakeword.utils.download_models()`.
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


# ---- Registry ---------------------------------------------------------

# Order matters — this is the display order on the picker page. Put the
# recommended default first so a new household lands on it.
REGISTRY: tuple[WakeModelEntry, ...] = (
    WakeModelEntry(
        key="jarvis_v2",
        label="Jarvis",
        pronunciation='Say "Jarvis" — "Hey Jarvis" still works too',
        description=(
            "Community-trained model from the Home Assistant wake-words "
            "collection (MIT license). Trained on the phrase set "
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
