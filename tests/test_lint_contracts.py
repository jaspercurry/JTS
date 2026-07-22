# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Static lint-policy guards.

These tests do not replace Ruff. They pin the project-level lint contract
that lets Ruff's `BLE001` suppressions be load-bearing while the existing
suppression debt is paid down over time.
"""
from __future__ import annotations

import re
import tomllib
from io import StringIO
from pathlib import Path
from tokenize import COMMENT, generate_tokens

REPO = Path(__file__).resolve().parent.parent
SCAN_ROOTS = ("jasper", "tests", "scripts", "deploy")

# Ratchet counts after enabling Ruff's BLE rules on 2026-06-18. Lowering
# either number is welcome; raising one means new suppression debt landed.
# 2026-06-20 (+4 suppression markers, all blind-except): distributed-active
# Slice 3 added fail-soft boundaries to the grouping reconcile path (the
# active-follower readiness gate, the active-solo restore, the CamillaDSP swap,
# and the defensive is_active_speaker_box topology probe) — each is a "never
# crash the reconcile / fail safe to solo" handler matching the existing
# reconciler idiom.
# 2026-06-21 (+1 suppression marker, blind-except): the bonded-leader AirPlay
# latency-fit /state snapshot (jasper/multiroom/airplay_latency.py) carries the
# same fail-soft "observability must never break /state" guard every sibling
# /state section does.
# 2026-06-21 (+1 suppression marker, blind-except): the OpenAI barge-in pack's
# truncate_assistant_audio wraps the conversation.item.truncate wire send so
# the LiveTurn seam can honour its "must never raise" contract while still
# surfacing the failure as event=barge.truncate_failed (WARN) — the same
# guarded-wire-send idiom as the adjacent _cancel_response.
# 2026-06-22 (+2 suppression markers, blind-except): distributed-active Stage B
# (active leader, Slice 5) added two fail-soft boundaries to the grouping
# reconcile path — the active-leader camilla#1 program-bake apply + camilla#2
# re-seed, and the unbond active-leader restore — each a "never crash the
# reconcile / fail safe to solo" handler matching the existing reconciler idiom.
# 2026-06-27 (+1 suppression marker, blind-except): PR #1051's /sound topology
# revision-compare-and-write TOCTOU fix wraps the critical section in a
# fail-soft `except BaseException` guard ("surface unexpected failures"). The
# baseline was not bumped when it merged, so main went red on this contract
# (count 622 vs ceiling 621) — reconcile the count here. The suppression is the
# established "never crash the critical write path" idiom; lowering the count by
# narrowing it later is welcome.
# 2026-06-27 (+2 suppression markers, blind-except): PR #1073's 4b-iv lean-lane
# mux wiring adds two fail-loud broad-except handlers in the enter/leave-lean
# ladders — catch-broad -> fall back to the buffered lane + log, the established
# "never crash the _tick / fail safe to buffered" idiom. Justified suppression
# debt for the new resilience path; the same two markers push BOTH ceilings by 2
# (each is a blind-except suppression). Narrowing later is welcome.
# 2026-06-27 (+5 suppression markers, of which +3 blind-except): the phone-mic
# capture relay (jasper/capture_relay/*) adds 2 urllib outbound-HTTPS-only
# suppressions (S310, guarded by an https-scheme check in client.py/health.py)
# and 3 blind-except suppressions in session.py — the no-silent-failure design:
# cue on ANY failure then re-raise, a best-effort cue that must not mask the real
# exception, and a best-effort purge (TTL is the backstop). All reviewed; one
# further best-effort handler was narrowed to a typed except rather than
# suppressed. (Marker strings are spelled out here, not written literally, so
# this very comment does not inflate the count it documents.)
# 2026-06-27 (+1 blind-except): the /correction/ relay-capture daemon adapter
# (jasper/web/correction_setup.py POST /relay/capture) adds one fail-loud
# never-crash-the-background-loop handler around the async capture runner —
# logs + surfaces the failure in /status, mirrors the existing
# _schedule_measurement_sweep idiom in the same file. Pushes BOTH ceilings by 1.
# 2026-06-27 (+1 blind-except): the fan-in coupling reconciler
# (jasper/fanin/coupling_reconcile.py _reconcile_camilla) wraps the CamillaDSP
# reconcile in a fail-safe handler — an UNEXPECTED reconcile exception must
# trigger the arm-failure rollback to loopback (return ok=False), never
# propagate and leave the box half-armed (fan-in on the pipe, camilla on the old
# config) with no recovery. Resilience-first on a production speaker. +1 BOTH.
# 2026-06-28 (no change): the usbsink-edge rate-match stage + its tests
# (jasper/usbsink/audio_bridge.py rate-match code, tests/test_usbsink_rate_match.py,
# tests/test_resampler_contract.py) were cut as the wrong tool for the observed
# USB drops. The removed code used only NARROW exception handlers (ImportError /
# ValueError / RuntimeError / OSError — no blind-except), and the deleted test
# files carried zero suppression markers, so the cut removed NO noqa / blind-
# except markers from the scanned roots. Both ceilings stay where they were;
# they cannot be lowered because the live count is still exactly at them.
# (Marker strings are spelled out here, not written literally, so this comment
# does not inflate the count it documents — same convention as the 2026-06-27
# phone-mic entry above.)
# 2026-07-02 (+3 suppression markers, none blind-except): the Stage-0
# route-latency click/capture harness's tap-contract test
# (tests/test_usbsink_impulse_tap_contract.py) stands up a tiny stdlib
# BaseHTTPRequestHandler stub for the tap's HTTP surface, which forces three
# unavoidable stdlib-override suppressions — the N802 non-snake-case method
# names do_POST/do_GET and the A002 `format` builtin-shadow in log_message are
# the handler base class's own required signatures, not project style debt.
# Only MAX_NOQA_MARKERS moves (these are N802/A002, not blind-except), so
# MAX_BLE001_MARKERS is unchanged.
# 2026-07-05 (P4 verify-acceptance loop, +1 blind-except suppression): exactly
# one new broad catch carries the suppression marker spelled B-L-E-0-0-1 —
# correction_setup._maybe_auto_revert, the top-level auto-revert side-action
# boundary of the verify upload. It is a genuine last resort: session.reset()
# re-raises the ORIGINAL exception of arbitrary type by contract (its own
# catch-and-re-raise after _fail), so the boundary's exception surface —
# pycamilladsp/websocket/transport errors, the response-timeout future,
# target-resolution raises — is unbounded, and any named tuple would leave an
# unenumerated class that 500s the verify upload after a partial revert, the
# precise outcome the mandate forbids ("leave the correction applied for
# manual undo, never fail the upload"). It is not a silent path: it
# logger.exceptions, stamps a failed auto_revert_outcome the envelope
# surfaces as "STILL APPLIED", and reset() itself fails the session loudly on
# a CamillaDSP rejection. The verdict computation in
# MeasurementSession._evaluate_acceptance deliberately carries NO such
# suppression — it catches the named RECOVERABLE_ERRORS family from
# jasper.audio_measurement.ramp (P2's precedent). The relocated catch in
# _resolve_reset_target_async moved verbatim out of _handle_reset (net-zero). Net
# effect on the ceilings: suppression-marker count +0, blind-except count +1.
# (Marker strings spelled out, not literal, so this comment does not inflate
# the counts it documents.)
# 2026-07-10 (-2 suppression markers, both blind-except): the USB dead-pipeline
# sweep (PR #1200) deletes the entire lean lane wholesale, including
# `Mux._enter_lean`/`_leave_lean` — the two fail-loud broad-except handlers the
# 2026-06-27 "+2 suppression markers" entry above added for that ladder are
# gone with them (their delivery mechanism, the Python usbsink FIFO bridge, was
# itself unreachable in production). Ratchets MAX_BLE001_MARKERS down by 2
# (630 -> 628) so the reclaimed slack is not silently reusable; MAX_NOQA_MARKERS
# is left alone this round even though the same two markers also counted
# against it, since the noqa ceiling already carries slack from other sources.
# 2026-07-14 (-7 broad-except suppressions): the summed commissioning runtime
# consolidates eight identical transaction-edge handlers into one explicit
# capture helper. The ratchet now counts suppression comment tokens instead of
# unrelated prose/string mentions of the rule name, so its value is the
# auditable live marker count after that consolidation (627 -> 620).
# 2026-07-18 (+1 suppression marker): the v2 crossover session runner's
# catch-all cleanup arm (correction_crossover_v2.build_v2_run_and_consume) —
# the W6.1 gate ruling. The play/analyze seams raise open-endedly
# (CamillaUnavailable is a bare Exception; the reviewer proved by probe it
# escaped the enumerated arms, leaving the measurement volume active, the
# relay session leaked, and the phone frozen). The arm is cleanup-and-reraise
# only: terminal host event + persisted failure + volume drain + purge, then
# the original exception propagates to the outer relay net unchanged. Never a
# silent path. Ceilings 620 -> 621 / 813 -> 814.
#
# 2026-07-20: +1 BLE001 for the crossover auto-apply background worker's
# last-resort arm (correction_crossover_v2): a thread with no caller to
# reraise to, where an escaped exception would strand the phone on the
# deferred hold and dishonestly time out as relay_timeout. Logs
# event=correction.crossover_v2_auto_apply_error + persists the failure —
# never a silent path. Ceilings 621 -> 622 / 814 -> 815.
MAX_NOQA_MARKERS = 815
MAX_BLE001_MARKERS = 622
# (Total reflects two independent +1 entries dated 2026-06-21: the AirPlay
# latency-fit /state snapshot and the barge-in truncate wire-send guard.)

_BROAD_EXCEPT = re.compile(
    r"^\s*except (?:BaseException|Exception)(?: as [A-Za-z_][A-Za-z0-9_]*)?:"
)


def _python_files() -> list[Path]:
    files: list[Path] = []
    for root in SCAN_ROOTS:
        base = REPO / root
        if not base.exists():
            continue
        files.extend(sorted(base.rglob("*.py")))
    return files


def test_ruff_ble_rule_is_enabled() -> None:
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text())
    selected = set(pyproject["tool"]["ruff"]["lint"]["select"])

    assert "BLE" in selected


def test_broad_exception_suppressions_are_explicit() -> None:
    missing: list[str] = []
    for path in _python_files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _BROAD_EXCEPT.match(line) and "# noqa: BLE001" not in line:
                missing.append(f"{path.relative_to(REPO)}:{lineno}: {line.strip()}")

    assert not missing, (
        "Broad Exception/BaseException handlers must either catch a narrower "
        "exception or carry an explicit `# noqa: BLE001` suppression marker:\n"
        + "\n".join(missing)
    )


def test_noqa_debt_does_not_grow() -> None:
    sources = [path.read_text(encoding="utf-8") for path in _python_files()]
    text = "\n".join(sources)
    ble_markers = sum(
        token.type == COMMENT and token.string.startswith("# noqa: BLE001")
        for source in sources
        for token in generate_tokens(StringIO(source).readline)
    )

    assert text.count("# noqa") <= MAX_NOQA_MARKERS
    assert ble_markers <= MAX_BLE001_MARKERS
