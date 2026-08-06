# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The single owner of JTS's openWakeWord import guard.

openWakeWord's ``__init__.py`` unconditionally imports its
``custom_verifier_model`` submodule, which imports scikit-learn.
scikit-learn is a real runtime dependency on the full profile
(``deploy/lib/install/python-runtime.sh`` installs
``scikit-learn>=1,<2`` alongside openwakeword), so the import always
succeeds — it just costs a lot of memory.

Measured 2026-08-06 on two Pi 5 speakers (jts3.local and jts.local,
Python 3.13.5, openwakeword 0.6.0, scikit-learn 1.8.0), in a fresh
process each time:

===========================================  ==============  ================
scenario                                     RSS delta       sklearn modules
===========================================  ==============  ================
``import openwakeword``                      129,424-129,920 169
guard first, then ``import openwakeword``    49,328-49,344   0
===========================================  ==============  ================

(KiB, read from ``/proc/self/status`` ``VmRSS``.) So the guard is
worth roughly 80,000 KiB — about 78 MiB — of resident memory in every
process that touches openWakeWord. That is a meaningful fraction of a
1 GB Pi 5, and jasper-voice holds it for the life of the daemon.

Pre-populating ``sys.modules`` with a stub makes Python's import
system treat ``openwakeword.custom_verifier_model`` as already loaded,
so the scikit-learn pull-in never happens.

What this DOES break: openWakeWord's speaker-verification training
feature (``train_custom_verifier``), which fits a per-user verifier on
speaker samples to reduce false wakes for the wrong person. JTS does
not use it.

What this does NOT break: custom wake-word ``.onnx`` models. Those
load through ``openwakeword.model``, not ``custom_verifier_model``. If
you want to train your own "Hey Jasper" wake word and drop the
``.onnx`` file into ``JASPER_OPENWAKEWORD_MODELS_DIR``, this guard
does not get in your way.

**Call** ``ensure_openwakeword_import_safe()`` **immediately before
every** ``import openwakeword`` **— in the same function, every time.**
Not once at some module top, and never "the daemon already imported a
module that did it." The guard has to be in place in whichever process
reaches openWakeWord first, and the processes differ: jasper-voice,
jasper-doctor, and the offline wake-training tools each have their own
entry point. Relying on import order is what previously let
jasper-doctor and a standalone ``jasper.vad`` import pay the full
scikit-learn cost while looking protected.

``tests/test_lazy_imports.py`` discovers openWakeWord import sites
under ``jasper/`` and ``scripts/`` and fails on any that is not
guarded, so a new entry point cannot quietly reintroduce the cost.
"""
from __future__ import annotations

import logging
import sys
import types

from .log_event import log_event

__all__ = ["ensure_openwakeword_import_safe"]

logger = logging.getLogger(__name__)

_VERIFIER_MODULE = "openwakeword.custom_verifier_model"
# Marks a sys.modules entry as ours, so a late call can tell "the stub is
# already installed" (nothing to do) from "the real module is loaded"
# (we lost the race and sklearn is resident).
_STUB_MARKER = "_jasper_openwakeword_stub"


def ensure_openwakeword_import_safe() -> None:
    """Keep scikit-learn out of this process's openWakeWord import.

    Idempotent, cheap, and safe to call from any thread before the
    first ``import openwakeword``. See the module docstring for what
    the stub costs openWakeWord (speaker-verification training only)
    and what it saves.

    If openWakeWord was already imported by the time this runs, the
    saving is gone and the call cannot recover it. That case logs
    ``event=openwakeword.guard_late`` at WARNING rather than returning
    a silent no-op — a silently-lost guard is exactly the failure this
    module exists to make impossible.
    """
    existing = sys.modules.get(_VERIFIER_MODULE)
    if existing is not None:
        if not getattr(existing, _STUB_MARKER, False):
            log_event(
                logger,
                "openwakeword.guard_late",
                level=logging.WARNING,
                detail=(
                    "openwakeword was imported before the guard ran; "
                    "scikit-learn is already resident in this process"
                ),
            )
        return

    stub = types.ModuleType(_VERIFIER_MODULE)
    # Both attributes go on via setattr because mypy rejects attribute
    # assignment on a freshly-constructed ModuleType.
    #   train_custom_verifier — the name openwakeword's __init__ imports and
    #                           re-exports, so the stub must carry it.
    #   _STUB_MARKER          — lets a later call tell our stub apart from the
    #                           real module.
    setattr(stub, "train_custom_verifier", None)
    setattr(stub, _STUB_MARKER, True)
    sys.modules[_VERIFIER_MODULE] = stub
