"""Bass-extension apply-intent path and payload shape; runtime-eligible adapters."""

from __future__ import annotations

from pathlib import Path

BASS_EXTENSION_RUNTIME_ADAPTER_IDS = frozenset({"sealed_v1"})
BASS_EXTENSION_APPLY_INTENT_PATH = Path(
    "/var/lib/jasper/bass_extension_apply_intent.json"
)
