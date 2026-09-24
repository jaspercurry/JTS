# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Wake-word corpus recorder — backend + bridge orchestration.

This package holds the engine behind the operator-only `/wake-corpus/`
recorder page, whose HTTP adapter is ``jasper/web/wake_corpus_setup.py``:

  - :mod:`jasper.wake_corpus.runtime_probe` — corpus leg/profile
    vocabulary and the env + hardware probes over it. The package
    leaf: the modules below import it; it imports none of them.
  - :mod:`jasper.wake_corpus.capture_plan` — plan identity/hashing, the
    plan builder, and conformance validation against a running bridge.
  - :mod:`jasper.wake_corpus.bridge_session` — bridge env / leg-plan /
    capture-health / systemctl restart primitives + enter/exit corpus
    test mode. Pure-function + subprocess layer (no asyncio).
  - :mod:`jasper.wake_corpus.clip_capture` — ``RecordingTask``: one clip's
    multi-leg UDP capture into PCM buffers, with the live level meter.
  - :mod:`jasper.wake_corpus.recording_backend` — ``RecordingBackend``:
    session and clip lifecycle, clip/metadata writing, and the test-mode
    marker crash-recovery. Owns a background asyncio loop driven from sync
    HTTP handler threads.

Nothing is re-exported at the package root on purpose: the modules import
NumPy (and lazily ``jasper.mic_capture``), so importers reach for the
specific submodule only when the recorder is actually needed. Keeping the
package root empty preserves the lazy-import contract that
``tests/test_web_main_imports.py`` enforces.
"""
