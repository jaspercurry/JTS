# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Wake-word corpus recorder — backend + bridge orchestration.

This package holds the engine behind the operator-only `/wake-corpus/`
recorder page. It was extracted verbatim from
``jasper/web/wake_corpus_setup.py`` (which is now a thin HTTP adapter that
imports and re-exports everything here):

  - :mod:`jasper.wake_corpus.bridge_session` — bridge env / leg-plan /
    capture-health / systemctl restart primitives + enter/exit corpus
    test mode. Pure-function + subprocess layer (no asyncio).
  - :mod:`jasper.wake_corpus.recording_backend` — ``RecordingBackend`` and
    its capture task, clip/metadata writing, and the test-mode marker
    crash-recovery. Owns a background asyncio loop driven from sync HTTP
    handler threads.

Nothing is re-exported at the package root on purpose: the modules import
NumPy (and lazily ``jasper.audio_io``), so importers reach for the
specific submodule (or the thin ``jasper.web.wake_corpus_setup`` shim)
only when the recorder is actually needed. Keeping the package root empty
preserves the lazy-import contract that
``tests/test_web_main_imports.py`` enforces.
"""
