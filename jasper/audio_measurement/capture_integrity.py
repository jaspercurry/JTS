# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Gate a banked measurement round on its own capture-integrity counters.

Every measurement campaign has re-derived "is this run clean?" by eyeballing
a log or hand-rolling a throwaway checker — most recently
``captures/linearization-night-2026-08-19/tools/integrity_summary.py``. This
module is the product promotion of that script's ONE decision: does every
dump-ring sidecar in a directory agree with the wired capture chain's own
definition of clean. It computes no new number and re-derives nothing that
:mod:`~jasper.audio_measurement.wired_capture` and
:mod:`~jasper.audio_measurement.frame_ledger` did not already put in the
sidecar; it only reads their fields back and names what disagrees.

**Not the same "capture integrity" as the two other things in this repo
carrying that name.**
:class:`~jasper.audio_measurement.program_analysis.CaptureIntegrity` is
computed DURING a live VERIFY capture (schedule/locate/clip checks on the
signal itself), and
:data:`~jasper.active_speaker.crossover_v2.verification.CAPTURE_INTEGRITY_CLEAN`
/ :data:`~jasper.active_speaker.crossover_v2.verification.CAPTURE_INTEGRITY_FAILED`
are the live per-capture validity verdict a running session gates on. Both
run in-process, on the capture the session just took. This module runs
OFFLINE, afterwards, on a JSON sidecar someone already pulled off the Pi —
same vocabulary, unrelated code path, no shared state.

**What "clean" means**, checked against ``capture_integrity`` (the wired
recorder's own per-take account,
:func:`~jasper.audio_measurement.wired_capture.build_capture_integrity_report`)
and ``frame_ledger`` (the reconciled ledger,
:meth:`~jasper.audio_measurement.frame_ledger.FrameLedger.to_dict`), both read
back from the sidecar JSON exactly as the recorder wrote them:

* ``frames`` and ``encoded_frames`` are reported and agree — two independent
  counts of the same take must match.
* ``block_gaps``, ``block_gap_frames``, and ``zero_run_count`` are reported
  and zero — any of the three nonzero means an ALSA discontinuity or a
  capture-FIFO dropout landed in this take.
* ``truncated`` is absent — the key is written only when true.
* ``capture_chain`` is ``jasper.audio_measurement.wired_capture.WIRED_CAPTURE_CHAIN``
  — this checker only vouches for the WIRED chain a banked measurement
  campaign is built on, not a phone-relay capture, which does not stamp a
  chain at all.
* ``frame_ledger`` reconciles: ``received_frames == declared_frames ==
  encoded_frames``, no render-gap frames, and an empty ``lost_at``.

**Exit contract** (a caller-facing promise, pinned by
``tests/test_capture_integrity.py``):

* ``0`` — at least one sidecar was found and every one is clean.
* ``1`` — nothing could be checked: a given path is not a directory, or no
  ``*.json`` file was found directly inside any given directory. This is a
  "the checker could not do its job" outcome, distinct from a checked-and-
  failing capture.
* ``2`` — at least one sidecar was checked and found dirty, including a
  sidecar whose JSON could not even be parsed (``sidecar_unreadable`` — a
  capture nobody can read back is not a clean one) or whose top-level JSON
  value was not an object (``sidecar_not_an_object`` — the recorder always
  writes a mapping, so anything else is not this checker's sidecar).

Each defect prints its own line, ``DIRTY sidecar=<name> finding=<code>
detail=<text>``, so a caller can gate on the exit code and grep the findings
for the reason. Directories are read non-recursively (one level of ``*.json``)
so a caller points this at the specific sidecar directory a dump-ring pull
produced — never at a whole banked-round tree, which also holds
non-capture JSON (the round receipt, the flow state, findings) that this
checker has no business grading.

Usage::

    python -m jasper.audio_measurement.capture_integrity <sidecar-dir> [<dir> ...]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Mapping, NamedTuple, Sequence

from jasper.audio_measurement.frame_ledger import (
    REPORT_KEY_ENCODED_FRAMES,
    REPORT_KEY_FRAMES,
    REPORT_KEY_RENDER_GAP_FRAMES,
    REPORT_KEY_RENDER_GAPS,
)
from jasper.audio_measurement.wired_capture import WIRED_CAPTURE_CHAIN

__all__ = ["EXIT_CLEAN", "EXIT_DIRTY", "EXIT_UNREADABLE", "Finding", "check_sidecar", "main"]

EXIT_CLEAN = 0
EXIT_UNREADABLE = 1
EXIT_DIRTY = 2

#: The zero-run disclosure key. No shared constant exists for it (unlike the
#: frame counters above) because nothing else in the product reconciles it —
#: see :mod:`~jasper.audio_measurement.frame_ledger`'s own comment on why only
#: the counters it reconciles get named constants.
_KEY_ZERO_RUN_COUNT = "zero_run_count"


class Finding(NamedTuple):
    """One named defect, machine-greppable by ``code``."""

    code: str
    detail: str


def _counter_findings(ci: Mapping[str, Any]) -> list[Finding]:
    """The three zero-or-absent counters: gaps, gap frames, zero-runs."""
    findings: list[Finding] = []
    for key in (REPORT_KEY_RENDER_GAPS, REPORT_KEY_RENDER_GAP_FRAMES, _KEY_ZERO_RUN_COUNT):
        value = ci.get(key)
        if value is None:
            findings.append(Finding(f"{key}_unreported", f"capture_integrity.{key} not reported"))
        elif value != 0:
            findings.append(Finding(f"{key}_nonzero", f"capture_integrity.{key}={value!r}"))
    return findings


def _frame_ledger_findings(fl: Mapping[str, Any]) -> list[Finding]:
    """The ledger's own reconciliation: counts, render gaps, lost_at."""
    if not fl:
        return [Finding("frame_ledger_missing", "no frame_ledger reported")]

    findings: list[Finding] = []
    received = fl.get("received_frames")
    declared = fl.get("declared_frames")
    encoded = fl.get("encoded_frames")
    if not (received == declared == encoded):
        findings.append(Finding(
            "frame_ledger_mismatch",
            f"received={received!r} declared={declared!r} encoded={encoded!r}",
        ))
    for key in ("render_gaps", "render_gap_frames"):
        value = fl.get(key)
        if value:
            findings.append(Finding(f"frame_ledger_{key}_nonzero", f"frame_ledger.{key}={value!r}"))
    lost_at = fl.get("lost_at")
    if lost_at:
        findings.append(Finding("frame_ledger_lost_at", f"lost_at={lost_at!r}"))
    return findings


def check_sidecar(doc: Mapping[str, Any]) -> list[Finding]:
    """Every way one parsed sidecar document can disagree with "clean".

    Pure function — no filesystem, no clock — over the exact two blocks
    :func:`main` reads back out of a dump-ring sidecar. Empty return means
    clean.
    """
    ci_raw = doc.get("capture_integrity")
    ci: Mapping[str, Any] = ci_raw if isinstance(ci_raw, dict) else {}
    fl_raw = doc.get("frame_ledger")
    fl: Mapping[str, Any] = fl_raw if isinstance(fl_raw, dict) else {}

    findings: list[Finding] = []

    frames = ci.get(REPORT_KEY_FRAMES)
    encoded_frames = ci.get(REPORT_KEY_ENCODED_FRAMES)
    if frames is None or encoded_frames is None:
        findings.append(Finding(
            "counters_missing",
            f"capture_integrity.{REPORT_KEY_FRAMES}/{REPORT_KEY_ENCODED_FRAMES} not reported",
        ))
    elif frames != encoded_frames:
        findings.append(Finding(
            "frame_mismatch",
            f"{REPORT_KEY_FRAMES}={frames!r} != {REPORT_KEY_ENCODED_FRAMES}={encoded_frames!r}",
        ))

    findings.extend(_counter_findings(ci))

    if ci.get("truncated"):
        findings.append(Finding("truncated", "capture_integrity.truncated=True"))

    chain = ci.get("capture_chain")
    if chain != WIRED_CAPTURE_CHAIN:
        findings.append(Finding(
            "wrong_capture_chain",
            f"capture_chain={chain!r} (expected {WIRED_CAPTURE_CHAIN!r})",
        ))

    findings.extend(_frame_ledger_findings(fl))
    return findings


def _iter_sidecar_files(dirs: Sequence[Path]) -> list[Path]:
    files: list[Path] = []
    for one_dir in dirs:
        files.extend(sorted(one_dir.glob("*.json")))
    return files


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(__doc__)
        return EXIT_UNREADABLE

    dirs = [Path(a) for a in args]
    not_dirs = [d for d in dirs if not d.is_dir()]
    if not_dirs:
        for d in not_dirs:
            print(f"UNREADABLE dir={d} reason=not_a_directory")
        return EXIT_UNREADABLE

    files = _iter_sidecar_files(dirs)
    if not files:
        print(f"UNREADABLE reason=no_sidecar_json_found dirs={[str(d) for d in dirs]}")
        return EXIT_UNREADABLE

    n_dirty = 0
    for f in files:
        try:
            doc = json.loads(f.read_text())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            print(f"DIRTY sidecar={f.name} finding=sidecar_unreadable detail={exc!r}")
            n_dirty += 1
            continue
        if not isinstance(doc, dict):
            print(f"DIRTY sidecar={f.name} finding=sidecar_not_an_object detail={type(doc).__name__!r}")
            n_dirty += 1
            continue
        findings = check_sidecar(doc)
        if findings:
            n_dirty += 1
            for finding in findings:
                print(f"DIRTY sidecar={f.name} finding={finding.code} detail={finding.detail}")
        else:
            print(f"CLEAN sidecar={f.name}")

    n_clean = len(files) - n_dirty
    print(f"{len(files)} capture(s) checked; {n_clean} clean, {n_dirty} dirty")
    if n_dirty:
        print("VERDICT: DIRTY")
        return EXIT_DIRTY
    print("VERDICT: CLEAN")
    return EXIT_CLEAN


if __name__ == "__main__":
    sys.exit(main())
