"""Guards that the memory-diet lazy-imports stay lazy.

Each test runs in its own Python subprocess so module-cache state
doesn't leak between cases (sys.modules is process-global). On a
Pi 5, the savings these guards protect are:

- openwakeword stub → sklearn doesn't load (~67 MB resident)
- gemini_session lazy → google.genai doesn't load unless provider=gemini (~49 MB)
- openai_session lazy → openai SDK doesn't load unless provider=openai (~11 MB)

A regression in any of these would silently re-inflate jasper-voice's
RSS by tens of MB. CI catches the import-graph change, not the bytes,
but the import-graph IS the cost on Python.
"""
from __future__ import annotations

import subprocess
import sys


def _run_probe(probe: str) -> dict[str, bool]:
    """Run `probe` in a fresh subprocess; parse `key=true|false` lines."""
    out = subprocess.check_output(
        [sys.executable, "-c", probe], stderr=subprocess.STDOUT, text=True,
    )
    result: dict[str, bool] = {}
    for line in out.splitlines():
        if "=" in line and line.split("=", 1)[1].strip() in {"true", "false"}:
            k, v = line.split("=", 1)
            result[k.strip()] = v.strip() == "true"
    return result


def test_wake_does_not_load_sklearn() -> None:
    """The openwakeword stub in jasper.wake should keep sklearn out
    of sys.modules. sklearn is ~67 MB resident; we never train custom
    verifier models, so it's pure dead weight."""
    probe = (
        "import sys\n"
        "import jasper.wake  # noqa: F401\n"
        "loaded = any(m == 'sklearn' or m.startswith('sklearn.') for m in sys.modules)\n"
        "print(f'sklearn_loaded={str(loaded).lower()}')\n"
    )
    result = _run_probe(probe)
    assert result.get("sklearn_loaded") is False, (
        "sklearn was loaded into sys.modules after importing jasper.wake. "
        "The custom_verifier_model stub at the top of jasper/wake.py was "
        "either removed or stopped working. ~67 MB regression."
    )


def test_voice_daemon_import_does_not_load_genai() -> None:
    """Importing jasper.voice_daemon must not eagerly load google.genai.
    The Gemini adapter is now lazy-imported inside _make_connection so
    non-Gemini users don't pay the ~49 MB cost."""
    probe = (
        "import sys\n"
        "import jasper.voice_daemon  # noqa: F401\n"
        "loaded = 'google.genai' in sys.modules\n"
        "print(f'genai_loaded={str(loaded).lower()}')\n"
    )
    result = _run_probe(probe)
    assert result.get("genai_loaded") is False, (
        "google.genai was loaded into sys.modules just by importing "
        "jasper.voice_daemon. The Gemini adapter must stay lazy in "
        "_make_connection so non-Gemini users avoid the cost."
    )


def test_voice_daemon_import_does_not_load_openai() -> None:
    """openai SDK should also stay out at module-import time. The
    openai_session adapter's class definition is module-top, but the
    SDK import is already inside _resolve_connect_call. Belt-and-
    suspenders: with voice_daemon's adapter imports now lazy, the
    openai_session module itself shouldn't load either unless the
    active provider is openai or grok."""
    probe = (
        "import sys\n"
        "import jasper.voice_daemon  # noqa: F401\n"
        "loaded = 'openai' in sys.modules\n"
        "print(f'openai_loaded={str(loaded).lower()}')\n"
    )
    result = _run_probe(probe)
    assert result.get("openai_loaded") is False, (
        "openai was loaded into sys.modules just by importing "
        "jasper.voice_daemon. Voice adapter imports should be lazy "
        "(inside _make_connection branches)."
    )
