# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Bench-only limiter-evidence campaign runner for Bass Extension.

This subpackage is the operator-run bench runner that executes the frozen
campaign in
``docs/bass-extension-waves/limiter-evidence-protocol.md`` and writes the
replayable evidence bundle a later Wave 4 revision consumes. It is bench-only,
operator-supervised, and fail-closed. It never wires the pure evidence producer
into any production path, never persists a profile, and calls no
``apply_bass_extension`` / ``bypass_bass_extension`` /
``recover_pending_bass_extension_apply`` writer.

Module map:

* :mod:`~jasper.bass_extension.bench.context` — the trusted measured-context +
  limiter-domain builder (bound to the emitter's validated clip_limit range).
* :mod:`~jasper.bass_extension.bench.manifest` — operator-authored
  ``campaign_manifest`` (refuses on a missing operator input; never defaults).
* :mod:`~jasper.bass_extension.bench.excitation` — the bass-owner
  ``ExcitationLimits`` + ``ProtectionEvidence`` derivation for admission.
* :mod:`~jasper.bass_extension.bench.activation` — the fail-closed
  temporary-graph-activation lifecycle (addendum
  ``limiter-bench-runner-activation.md``): mutate the *running* config only,
  prove read-back before unmute, restore via ``reload()`` on every exit.
* :mod:`~jasper.bass_extension.bench.analysis` — the campaign verdicts composed
  from the existing measurement kernels plus the paired-transparency,
  sag/corner-shift, and isolated digital-transfer analyses.
* :mod:`~jasper.bass_extension.bench.bundle` — shapes the exact frozen bundle
  schema via ``bundles.py`` / ``evidence_identity.py``.
* :mod:`~jasper.bass_extension.bench.runner` — the orchestrator that runs the
  discovery + candidate passes and emits the bundle.
"""

from __future__ import annotations
