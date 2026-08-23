# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Static lint-policy guards.

These tests do not replace Ruff. They pin the project-level lint contract
that lets Ruff's `BLE001` suppressions be load-bearing while the existing
suppression debt is paid down over time.
"""
from __future__ import annotations

import ast
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
#   2026-07-29 (two-stage PR-T3): that worker is DELETED with auto-apply, and
#   the marker it held is now carried by ``_assert_stage_2_can_open`` in the
#   same module — the apply's stage-2 openability preflight, which must fail
#   CLOSED on any unexpected exception because "we could not check" and "we
#   checked and it is fine" must never produce the same outcome on the one
#   action that touches the speaker. Logs
#   event=correction.crossover_v2_apply_stage2_preflight_failed and refuses;
#   never a silent path. NET ZERO, so the ceilings below do not move — the
#   attribution is recorded here rather than left pointing at code that no
#   longer exists.
# 2026-07-27: +1 BLE001 for the enhanced-AEC native-extension activation
# transaction. Its catch-all is cleanup-and-reraise only: it atomically restores
# the prior extension (or removes the new one) for any import/probe failure,
# then propagates the original exception. Ceilings 622 -> 623 / 815 -> 816.
#
# 2026-07-30 (two-stage eager-fit rider): +1 BLE001 for the SPECULATIVE group
# close in crossover_v2_flow (``run_speculative_group_close``). It fits the
# pre-apply cloud on a background thread before the household has confirmed,
# so its failure is a failure of work nobody has asked about yet — and one the
# household may still moot by retaking. The arm therefore drops the result and
# leaves the bank empty; the confirm path then refits and raises the identical
# exception from the identical place, where the host maps it to a real
# terminal screen. It must not name a family: the PR-L4 accountability veto
# (``CaptureBeginRefused``) already raises outside the named families this file
# uses, and guessing the fit's raise surface is exactly how a swallowed
# exception becomes a hang. Logs
# event=correction.crossover_v2_speculative_close_failed at WARNING with the
# exception type + traceback — never a silent path, and never the household's
# only signal, since the confirm re-raises. Ceilings 623 -> 624 / 816 -> 817.
#
# 2026-08-02 (#1967 boost-evidence bound): +1 BLE001 for the cross-position
# variance check in crossover_v2_flow (``_boost_excluded_bands_hz``). The
# catch-all is a fail-OPEN disclosure boundary: the check only ever NARROWS
# boost permission, so an unexpected numeric failure must leave the
# permission where the gate already had it rather than blanket-refusing boost
# below 4 kHz on a hiccup. Never silent — it logs
# ``event=correction.crossover_v2_boost_variance_failed`` at WARNING with the
# band, and the outcome rides the same
# ``event=correction.crossover_v2_boost_evidence`` line as every other
# outcome, with ``variance_reason=variance_check_failed``. Mirrors the
# classify-only ``_crossover_region_null_registry`` catch three functions up.
# When #1967 landed, the tree carried 618 BLE001 markers and 798 suppression
# comments, so the new marker fit inside the then-existing slack.
#
# 2026-08-02 (deep-audit cleanup): ratchet both ceilings to the combined live
# counts after rebasing onto #1967. The audit replaces 14 fixture-import F401
# suppressions with explicit module references and consolidates or removes
# broad-exception boundaries elsewhere. Keeping the old slack would let nine
# total suppressions and five BLE001 suppressions return without tripping this
# contract.
#
# 2026-08-13 (#2386 exactly-once banking): +1 broad catch in crossover_v2_flow's
# ``_grade_verify_attempt``, on the ``record_model_error`` seam call. The arm is
# a fall-THROUGH boundary and the fall-through is the property: the rung that
# stops a second durable write is the attempt landing in ``_attempt_history``,
# which the method appends AFTER this call, so any exception that escapes the
# call skips the append and the next capture of the same applied candidate asks
# the seam again (measured: two runs, two writes, on every propagating class).
# It cannot name a family — the seam is a Protocol any host may implement, so
# enumerating what today's single binding raises makes the property a fact about
# one implementation instead of about the interface. Never silent: logs
# event=correction.crossover_v2_model_error_write_unexpected at ERROR with the
# traceback, deliberately a DIFFERENT event from the named-family arm above it
# so "the store had an outage" and "the seam raised something nobody
# enumerated" stay distinguishable. ``BaseException`` is still not caught.
# The tokenized B-L-E-0-0-1 count fits the existing ceiling (618 -> 619), so
# only the total moves: 808 -> 809.
#
# 809 -> 812 (#2285 P7, 2026-08-17), and the ceiling delta is NOT this PR's
# marker delta -- the two numbers are different things and conflating them is
# how a ratchet stops meaning anything. Measured, not inferred: origin/main
# stands at 806 with 3 free slots; this branch adds SIX markers -- five F401 on
# deliberate eager imports in tests/test_active_endpoint_convergence.py (they
# keep a monkeypatched module-level binding from freezing into five importers,
# and deleting them re-opens a cross-file failure; the argument is in that
# file), plus the ONE availability wrap argued below. 806 + 6 = 812, so the
# ceiling lands exactly on the count with no slack granted. An earlier revision
# of this entry said "+1 exactly", which was true of the ceiling only because
# main's then-headroom silently absorbed the five F401s.
#
# The ONE availability wrap in
# jasper/fanin/converge.py::_reemit_graph_at_ring, and the argument is made here
# rather than absorbed by the number. That frame invokes an ENTIRE CLI
# (jasper.cli.active_speaker.main). The CLI's own converter turns exactly three
# classes into parser.exit -- ActiveSpeakerConfigError, OutputTopologyError,
# OSError -- so every other class its whole tree can raise arrives at the caller
# live, and the failure set of a CLI is not enumerable the way a module's own
# reads are. The convergence step promises to cost a box its CONVERGENCE and
# never its RECONCILE, and a narrowed catch here breaks that promise. The
# evidence classes are kept apart deliberately: the abort is MEASURED; a
# type-confused applied record reaching that frame is DEMONSTRATED by the pin in
# tests/test_active_endpoint_convergence.py; a mid-deploy ImportError (this pass
# runs while install.sh rsyncs Python under it, and both modules lazy-import) is
# ARGUED from the deploy ordering and has never been reproduced. Same shape and
# same reason as coupling_reconcile.py's camilla-reconcile wrap. The step's
# OTHER THREE catches stay narrow: they guard that module's own reads, whose
# raise set IS derived. BaseException is still not caught. The B-L-E-0-0-1
# count is 615 BY THE TOKENIZED COUNTER BELOW, which is the only count this rule
# uses; a bare substring scan of the same tree answers 618, and the gap is
# exactly why the assertion tokenizes rather than greps. 615 sits under its
# unchanged ceiling, so only the total moves.
#
# 812 -> 810 (#2285 THE WAVE, 2026-08-17). This PR ADDS no marker; it deletes
# code that carried two, so the ratchet ratchets DOWN and the ceiling follows
# it exactly rather than being left as slack a later PR could spend without
# argument. Both numbers are MEASURED with this file's own methods, run over
# the post-deletion tree: the total with the substring assertion below (810),
# the B-L-E-0-0-1 count with the tokenizer below (613). Do not substitute a
# substring scan for the second -- it answers a different, larger number, which
# is the whole reason the assertion tokenizes.
#
# AND DO NOT SPELL EITHER MARKER LITERALLY IN THIS COMMENT. The total is a
# SUBSTRING count over these very files, so prose about the ratchet lands
# inside the thing it describes: an earlier revision of this entry wrote the
# marker out to explain the method and measured 810 while the tree then held
# 811. That is why the entry above spells it B-L-E-0-0-1.
#
# The B-L-E-0-0-1 ceiling drops by the same TWO the deletions struck (619 ->
# 617) rather than down to the measured 613. That constant has carried
# deliberate headroom since the P7 entry above, the headroom is not this PR's
# to spend or to revoke, and collapsing it would red a concurrent branch that
# had already argued for a slot. Deleting code earns the ratchet its own
# markers back and nothing more.
# 810 -> 813 (#2285 THE WAVE part 2, 2026-08-17), and the delta is THREE S-L-F-0-0-1
# in one new test, argued rather than absorbed. The test pins that
# ``baseline-reemit``'s `help=` AND `description=` never name an endpoint
# argparse rejects -- the drift it closes is that the two strings live in
# different objects (`help=` on the parent's pseudo-action, `description=` on
# the subparser) and exactly one of them got trued up.
#
# The private reads are the only way to compare the strings AS AUTHORED.
# argparse exposes no public accessor for either, and the obvious alternative --
# assert against rendered `--help` output -- is WORSE for this specific pin:
# the formatter wraps at terminal width, so the very substring being forbidden
# ("--endpoint aloop") can land split across two lines and the guard would pass
# on the text it exists to reject. A fragile guard on a string an operator reads
# is worth less than three suppression markers.
#
# The B-L-E-0-0-1 count is unchanged at 613 under its 617 ceiling: part 2's one
# broad catch is a MOVE, not an addition -- the /sound/ rollback teardown's
# catch was deleted when its five forked codes moved to their single owner, and
# the owner carries the same catch for the same reason.
# 813 -> 814 (topology commit rollback, 2026-08-18): this boundary must catch
# arbitrary callback exceptions so it can restore the prior graph before
# re-raising the original failure.
#
# 814 -> 815 (#2662 W2b, 2026-08-18): +1 BLE001 for the WIRED provider's
# catch-all cleanup arm (correction_crossover_v2_wired.build_v2_wired_run_
# and_consume) -- the SAME W6.1 gate ruling the relay runner's arm carries,
# for the same reason: the seams raise open-endedly, and a wired
# capture-chain fault (WiredCaptureError) must persist an honest code and
# drain the walked-away volume rather than escape with the measurement
# volume active. Cleanup-and-reraise only; never a silent path. The wired
# runner is the relay runner's sibling, so it inherits the slot the relay's
# arm already argued for, restated per-site as this ratchet requires.
#
# 815 -> 816 (capture level/graph provenance, 2026-08-20): +1 BLE001 for
# capture_provenance.record_capture_provenance -- the outer belt on the
# forensic capture-provenance block. Its contract is not "handle the known
# failures" (observe_capture_provenance already names, per read, the types a
# CamillaDSP read can actually meet) but "recording what a capture was taken
# through may never cost a household its measurement", and an exception type
# nobody predicted is exactly what that promise exists for: this runs INSIDE
# the writer lock, on the play path, immediately before the WAV handoff.
# Blind-and-log only, never a silent path -- it emits result=failed with
# exc_info -- and BaseException still passes, so a cancelled measurement stays
# cancelled. One site, one owner: the belt lives in the module rather than at
# each playback branch, so a second branch cannot forget it. Pinned by
# tests/test_capture_provenance.py's
# test_an_unforeseen_exception_type_still_cannot_reach_the_capture.
MAX_NOQA_MARKERS = 816
MAX_BLE001_MARKERS = 618
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


# Line ceilings for the four largest files of the crossover-commissioning
# program (#2662). Same ratchet contract as the marker counts above: LOWERING a
# number is a win anyone can bank without asking, RAISING one is a deliberate
# line in a diff that a reviewer gets to argue with.
#
# Why these four and not a repo-wide rule. Over the 21 days to 2026-08-17 the
# subsystem grew 51% while its AVERAGE file size did not move — growth went
# into new seams, which is the healthy shape and is not what this guards. It
# guards the exception: `web/correction_crossover_v2.py` went 3,464 -> 9,178
# monotonically, never once cut, while `crossover_v2_flow.py` was cut 14,314 ->
# 10,988. A file that only ever grows is the one that ends up owning things its
# filename does not describe.
#
# Set at the tree's own counts on 2026-08-17, AFTER this PR's own edits, so the
# caps bank what is actually here rather than a number from a report. Two of
# the four are below their measured start (`crossover_v2_flow` 12,512 ->
# 12,508; `program_analysis` 6,939 -> 6,932) because of this PR's deletions.
# The other two are ABOVE it, and the reason is worth stating rather than
# hiding: moving the four RESULT_* codes to their owner ADDED lines at both
# ends — an import block in each file, and symbol names long enough to reflow
# the dict literals they replaced. The win there is one owner for a vocabulary
# that had two, not a smaller file, and a ratchet that quietly booked it as a
# saving would be lying about which kind of win it was.
#
# `crossover_envelope_v2` sits at 4,076 rather than the 4,048 it was set at, and
# the 28 lines are #2656's: a capped MISSED series now ends in the adoption
# table, which mints an ending this screen had no sentence for. The lines are
# one household sentence, its rationale, and the branch that reaches it — the
# household-copy work this module exists to own, not a concern leaking in. The
# guard's question is whether a file is accreting things its filename does not
# describe; the honest answer here is no, and compressing correct prose to keep
# the number flat would be gaming it.
#
# `crossover_v2_flow` sits at 12,510 rather than the 12,508 the deletions left,
# and the two lines are the ratchet catching its own author: the fix round for
# this PR's gate review corrected a comment at `:2801` that named the wrong
# owner for the level datum, and stating the real owner plus a pointer to the
# account takes two lines more than the false claim did. Compressing correct
# prose to keep a number flat would be gaming the guard; raising it by exactly
# the two lines earned, in the diff that earned them, is the guard working.
#
# `program_analysis` sits at 6,969 rather than 6,932, and #2052 is the first
# change to meet this ceiling. The diff is +6 executable lines and +31 of
# prose. The six are the whole change: one shared tri-state fold where two
# `all(...)` reductions stood (`all()` folds `None` to False, which would have
# turned an unknown channel map into a hard stop telling a household to rewire
# its speaker), two widened signatures, and a four-line branch where a
# one-line `return <bool>` stood. The prose is the one-sidedness rule stated
# once at its owner, `_channel_map_ok` — a review round trimmed it there and
# at the two upstream sites that had begun restating it, which is where 10 of
# these lines went back. What is left is a safety rule whose two halves are
# each pinned by a fixture, and compressing that to keep a number flat would
# be gaming the guard rather than passing it.
#
# 2026-08-17 (#2637): `crossover_v2_flow` 12,510 -> 12,537, in two bumps in one
# PR. The caps were set at the tree's exact counts, so the file had zero
# headroom and ANY addition trips the guard — which is the guard working, not a
# verdict that the addition is wrong.
#
# The first +18 is one env reader (`v2_first_begin_timeout_s`) plus its import
# and export entry, buying a first-begin budget an operator can widen in
# jasper.env instead of a rebuild. The second +9 is that PR's own gate round:
# the reader advertised a range four times wider than a hand-walked stage's
# relay link can honour, and the correction is a derived ceiling plus the
# paragraph saying why the bound is not written here. Prose that stops an
# operator setting a value the link clock will kill is worth nine lines.
#
# Neither bump was clawed back out of correct prose elsewhere in the file.
#
# Decision 10 (#2600) raises three of the four, and the fourth is the point.
# The blend region's shape correction is a NEW capability, and the ratchet's
# question is not "did a file grow" but "did it grow things its filename does
# not describe":
#
#  * `crossover_v2_flow` 12,548 -> 12,645 (rebased over #2637's +27 and
#    #2603's +11). 97 lines: 49 of wiring, then 48 the panel's combined fix
#    round earned — the strict-reader route both lenses independently asked
#    for, and `_blend_prescription`, whose 26 lines are the argument for why
#    "no instruction" reads the applied graph rather than reverting to
#    nothing (the ruling that keeps a restored round from dropping an adopted
#    correction). The solve, the fit, the bounds, the
#    iteration and its refusals are ~470 lines in a NEW module,
#    `crossover_v2/blend_correction.py`, which is where a ratchet-respecting
#    change puts them. What landed here is 49 lines of wiring and no policy:
#    widening one existing sink so the graded curve travels with the verdict
#    that describes it, one reader for the applied incumbent (the shape
#    `applied_boosts` already has, for the reason it has it), and two argument
#    hand-offs. Every number the correction is bounded by lives in the new
#    module; this file learned no new fact about blends.
#  * `crossover_envelope_v2` 4,076 -> 4,096. One household sentence, its
#    rationale, and the branch that reaches it — the same shape as #2656's 28
#    lines directly above, and the same argument: this file's job is household
#    copy, and a screen that reports the blend defect round after round with
#    nothing saying a lever is aimed at it reads as a loop doing nothing.
#  * `program_analysis` 6,969 -> 6,978. Nine lines, all prose, zero executable:
#    `crossover_region_band_hz` now names its second READER. The correction
#    consumes that band through the claim that already calls it rather than
#    calling it again, which is what keeps this a one-caller function — and a
#    docstring is where "who reads this band" has to be answerable, since the
#    call graph no longer says it.
#  * `correction_crossover_v2` stays at 9,186. Decision 10 adds no endpoint, no
#    screen, and no state key the host must own, so the host that has only ever
#    grown does not grow here.
# 2026-08-18 (#2662, the explicit delay prescription). Three ceilings move,
# and the honest accounting is that most of the room is PROSE this repository
# charges for on purpose — the #2603 note below says compressing a reason out
# to keep a number flat is gaming the guard, and that rule does not stop
# applying when the number is inconvenient.
#  * `program_analysis` 6,978 -> 7,155. The largest bump, and the one owed the
#    most explanation. About a third is executable: two commitment objectives
#    and the two sets that classify them, one prior field, `half_period_us`,
#    the selector's prescribed-delay arms, and the disclosure that fires when a
#    prescription reaches no commitment. The rest is this file's own
#    convention — every objective constant here carries a paragraph saying
#    WHICH FACT produced the commitment, because a forensic reader of a
#    persisted candidate has nothing else to read, and the two new ones each
#    have a membership argument to make (one keeps its residual, one gives it
#    up with the rest of the low-SNR refusal). `half_period_us`'s docstring is
#    load-bearing for the same reason: it is the single geometry two modules
#    now share, and the sentence naming BOTH callers is what stops a third
#    spelling appearing.
#    THE SEAM, named rather than taken: the `ALIGNMENT_*` objective vocabulary
#    plus `half_period_us` and the alignment dataclasses are ~200 lines of pure
#    vocabulary with no logic, and extracting them to an `alignment` sibling
#    would pay this bump back several times over. It is not taken HERE because
#    two sibling implementers are live in this same file tonight (the
#    delta-probe axis and the capture-integrity work), and a 200-line move
#    under them is a merge collision, not a cleanup. It is the right next cut.
#  * `crossover_v2_flow` 12,645 -> 12,682. Fifteen executable lines and no
#    policy: one ctor argument, one field, one property, and three hand-offs —
#    the session HOLDS a prescription the boundary already validated and never
#    re-judges it. Every rule about what a prescription may be lives in the new
#    `crossover_v2/alignment_prescription.py`; this file learned no new fact
#    about delays.
#  * `correction_crossover_v2` 9,186 -> 9,258. Twenty-eight executable lines:
#    the request gate's call and its refusal translation, one durable key, one
#    reader beside its eight siblings, and two hand-offs. The comment block at
#    the gate is most of the rest, and it is where the ORDER is recorded — the
#    two speaker-level gates run first, because whether this speaker can be
#    measured at all is a prior question to whether this request is good.
#  * `crossover_envelope_v2` stays at 4,096. The prescription adds no household
#    screen: the fourth declared-polarity objective reuses the sentence the
#    other three already earned.
#
# 2026-08-18 (#2662, the two-lens panel's fix round). Three more, all paying for
# findings rather than for features — which is the ratchet doing exactly what it
# is for, since a fix round that could not afford its own explanation would ship
# the fix and lose the reason.
#  * `program_analysis` 7,155 -> 7,193. The cross-check's second derivation is
#    DELETED here and the answer is carried on the candidate instead, so the
#    executable count barely moves; what costs the lines is the field's own
#    paragraph (a reader has to know why it is carried and not computed, which
#    is the whole defect), the `not-committed` disclosure gaining the delay that
#    was committed instead, and the two Fc guards on the lobe tripwire finally
#    saying why neither covers the other.
#  * `crossover_v2_flow` 12,682 -> 12,720. One more ctor argument, one field,
#    one property and one read off the candidate's frozen evidence — the same
#    route `measure_proposal_fingerprint` already takes, and the comment says so
#    rather than re-deriving it.
#  * `correction_crossover_v2` 9,258 -> 9,282. One durable key, one rehydrate,
#    one hand-off, and the sentence recording that the preset's declared window
#    is now asked at the tap instead of ten minutes later at a screen that
#    blames the microphone.
MAX_LINES_BY_PATH = {
    # 2026-08-23 (#2879, gate rounds 2 and 3). Two files, and for once the two
    # numbers the entry below insists on reporting separately are the SAME
    # number: that entry left both at EXACTLY their ceilings, so there is no
    # slack left to spend and every line here is new. Round 3 moved NEITHER
    # ceiling — its journal field and docstring fixes were paid for out of its
    # own prose, in the same files, so the two figures below still hold.
    #  * `crossover_v2_flow` ceiling 13,056 -> 13,076, +20. SIX of the lines it
    #    touches are non-comment — FIVE added and one modified, counted rather
    #    than eyeballed: the ctor argument, the field's three lines (ORed with
    #    the tier's own answer so a caller that resolved no shape cannot drop
    #    the arm's gate), the refusal's `gated=` journal field, and the
    #    predicate itself moving from `tier_is_externally_positioned(self.
    #    _tier)` to `self._positions_gated`. (This note first said "ONE is
    #    executable", counting the predicate and forgetting everything that
    #    feeds it.) The rest is that branch's own enumeration, which had
    #    to become three numbered items because they no longer share an owner:
    #    two dishonesties are the ARM's (a pose it cannot reach, recorded as
    #    though it had been) and the third is the GATE's (the retry
    #    re-authorizes the same plan entry, so the published bearing and the
    #    screen name two different places). A reader who cannot tell those apart
    #    cannot tell why a person — who could walk to the wider spot — is
    #    refused too.
    #  * `correction_crossover_v2` ceiling 9,408 -> 9,430, +22, and all of it is
    #    the position gate saying what it now IS. Five surfaces still called the
    #    hold the remote tier's alone: the class docstring, the endpoint
    #    constant, the hold-budget rationale ("a machine move"), and two of
    #    `gate`'s own comments. The docstring costs the most, because the two
    #    gated shapes need a reason EACH — the arm has no hand to tap, the wired
    #    round has a hand but no capture page for it to tap on — and it is where
    #    a reader learns the gate never asks which of them is on the floor. Two
    #    lines are stage 2's `positions_gated=` and its note that that ctor is
    #    handed no tier at all, which is why its groups prompted for a 75 cm
    #    rung even on the arm.
    #
    # 2026-08-22 (#2879, the human release source). Three files, and the third
    # is the big one — a provider gaining a verb it did not have.
    #
    # TWO NUMBERS PER FILE, because they differ and only one of them is what
    # this diff actually wrote: the CEILING delta, and the FILE's own growth.
    # Two of these files sat under their ceilings at the merge base, so a
    # ceiling that barely moves is slack being spent, not restraint. Reporting
    # only the smaller number is how a ratchet gets quietly drained (this
    # entry's first draft did exactly that, and the review caught it).
    #  * `crossover_v2_flow` ceiling 13,008 -> 13,056; the FILE grew +69
    #    (12,987 -> 13,056), so 21 lines came out of existing slack. Three
    #    executable additions: one field with a default, one property that ORs
    #    two facts, and a `__post_init__` refusing the shape that claims both
    #    movers. Everything else is the paragraph each of the two facts now
    #    owes, because the single boolean they replace was read by sites with
    #    no way to say which of its two meanings they wanted — and the pair of
    #    docstrings is precisely where a reader learns that
    #    `externally_positioned` is the ADVANCE axis while `positions_gated` is
    #    the pose-statement one. The four reads that moved between them cost no
    #    lines; the stage-2 anchor gaining a second local (`positions_gated`
    #    beside `externally_positioned`, because that screen reads BOTH facts)
    #    costs one.
    #  * `correction_crossover_v2` ceiling 9,386 -> 9,408; the FILE grew +138
    #    (9,270 -> 9,408), so 116 lines came out of slack that deletions
    #    elsewhere had left. What is in them: `_hand_released_plan_shape`,
    #    `PositionGate.abandon_hold`, and the retake seam. The biggest single
    #    cost is `abandon_hold`'s own paragraph, which records the invariant
    #    that made it necessary — `gate` publishes a new `pending` only when no
    #    hold is open, so a caller that walks away from a held begin has to say
    #    so or the envelope keeps naming a position nothing is measuring.
    #  * `correction_crossover_v2_wired` ceiling 755 -> 940, and the file grew
    #    by the same +185: this one had no slack at all. The retake is this
    #    provider's own choreography and belongs nowhere else — the seam's rule
    #    is that a source's choreography stays private, and `_serve_retake`
    #    reads `plan`, `max_attempts`, `_authorize` and `_capture_one`, four
    #    closures over the conductor, the gate, the device and the recorder. A
    #    sibling module would take all four as parameters for one caller, which
    #    is a worse file, not a smaller one. What the lines buy is the relay
    #    contract restated where it is IMPLEMENTED (index == accepted, never
    #    `+ 1`; the count never rewinds; one ordinary attempt; a rejected
    #    replacement leaves the original standing), the three refusals that keep
    #    a refused bonus from ever being a session death, and the note on what a
    #    mid-capture ask resolves to. Stated once each: the duplicate copy in
    #    `_serve_retake`'s own docstring was cut in the same diff.
    #
    # 2026-08-22 (#2758/#2759, the headroom-ledger panel's second fix round).
    # Nine lines across two files, both paying for a defect the ratchet's own
    # sibling guards did not cover: `headroom_cost_basis` was stamped as the
    # CURRENT era unconditionally, so a candidate republished off disk under-
    # disclosed its cost wearing a current-era label.
    #  * `correction_crossover_v2` 9,393 -> 9,398. One keyword argument, one
    #    local, and the sentence saying why the era comes from the CALLER: only
    #    the caller knows whether it built the fits this process or read them
    #    off disk, and nothing on the candidate records that. A default that
    #    was right for one of its two callers is exactly how this shipped.
    #  * `crossover_envelope_v2` 4,289 -> 4,293. The reader now distinguishes
    #    THREE eras rather than two, and the lines are the tuple it checks
    #    against plus the one paragraph a household-copy file owes: the two
    #    peak eras disagree in the direction #2758 opened, so collapsing them
    #    would render a pre-widening number as a current figure.
    #
    # 2026-08-17 (#2603): +11 on top of the two bumps above, and the ratchet
    # catching its own author again. The representative RoleBand pair at
    # `_DISPLAY_ROLES_BANDS` was flagged as a fourth declaration of a driver's
    # low limit; it is not one, and the owner ruled it stays. Recording WHY it
    # is not derived -- a declaration exists by the time that screen renders,
    # but the resolution path is refuse-if-not-ready and regenerates the
    # preview file as a side effect, and a memoized read would go stale -- is
    # what costs the lines. Paying them here, in the diff that earned them, is
    # the guard working; compressing the reason out to keep a number flat would
    # be gaming it.
    #
    # 2026-08-18 (series-2 D1): `crossover_envelope_v2` 4,096 -> 4,103. Zero
    # executable lines. One household sentence changed from a claim that had
    # become FALSE -- "This check could compare loudness but not the
    # correction's shape this round", on a path that since D1 compares neither
    # -- and seven lines saying why the old wording was not merely stale but
    # was the incident's own confusion reaching a household screen: what that
    # copy called a loudness comparison was a comparison against the model.
    # This file's job is household copy, so the argument for a sentence belongs
    # beside it; and a caveat that overstates what was checked is the one kind
    # of copy defect this screen cannot afford.
    #
    # 2026-08-18 (series-2 D1, panel fix round): two more, and both are the
    # SAME defect class as the change that earned them -- an instrument's name
    # sitting over a quantity it did not measure.
    #
    #  * `crossover_v2_flow` 12,720 -> 12,766. +46 physical, ~11 logical. The
    #    delta probe's anchor became the input to a hard stop, so
    #    `_entry_delta_db` gained the two things an input to a hard stop owes:
    #    it says on the journal WHY there is no anchor (its most reachable arm
    #    -- a first-ever round -- returned None silently), and it refuses a
    #    baseline measured through another program, because an anchor is a
    #    subtraction and a foreign one cancels a real finding as readily as a
    #    phantom. Comparability keeps its single owner: this asks
    #    `round_evidence`'s two identity fields, it does not re-derive the rule.
    #    The probe's journal line also gained `safety_anchored`. The remaining
    #    lines are the argument for each, which is what this ratchet is for.
    #  * `correction_crossover_v2` 9,282 -> 9,292. +10, of which 1 executes.
    #    `_delta_probe_summary` carries `safety_anchored`, because the round
    #    receipt is write-once and `/state`, the doctor and the done screen read
    #    THIS record -- so a fact only the receipt held was a fact no live
    #    surface could show. The nine lines say why it sits here rather than
    #    only there. Decision 10's note above ("stays at 9,186") described a
    #    change that added no state key; this one adds exactly one, and it is
    #    the key that tells "nothing was found" from "nothing looked".
    #
    # 2026-08-18 (series-2 D1, panel delta): `crossover_v2_flow` 12,766 ->
    # 12,790. +24, ZERO executable. The panel's hearing lens rebuilt the phase
    # maps and found three comments in this PR attributing `_entry_delta_db`'s
    # baseline-is-None arm to "every first-ever round" -- which is false, and
    # falsely reassuring in the worst direction: a first-ever round DOES capture
    # an entry baseline, and never reaches that arm at all, because it takes the
    # `state_axis_only` branch first. Saying which route each round actually
    # takes, and why this asks ONE of comparability's two fields rather than
    # both, is what costs the lines. Prose that is confidently wrong about
    # reachability on a hearing-safety path is the same defect class this whole
    # PR is about, so it is paid rather than compressed.
    #
    # 2026-08-18 (series-2 D1, panel delta round 2): 12,790 -> 12,810. +20, of
    # which ~13 execute. Both lenses converged on the same ask: the anchor guard
    # claimed comparability's two identity fields and compared one. It now asks
    # BOTH through `verification.identity_mismatch` -- the identity half of
    # `_comparability_mismatch`, extracted so the order and the two reason
    # constants have one owner rather than a parallel spelling here -- and the
    # MARK is the field that earned it: a baseline captured at another position
    # is the same program on the same grid, so nothing else on this path would
    # catch it subtracting a different room bin by bin. The identity read also
    # moved INSIDE the fail-soft try, which is a line of nesting for a method
    # whose whole contract is that it never loses a verdict.
    # 2026-08-18 (#2699): 12,810 -> 12,813 earns stage-1 series-position
    # hydration here; 9,296 -> 9,333 below earns Undo clearing the banked blend
    # instruction it reverses. Both ceilings equal the measured current files.
    #
    # 2026-08-18 (lateral pause): 12,813 -> 12,874, +61 net (93 added, 32
    # removed), counted rather than estimated. Every line is PROSE — the flag
    # flip itself is one character:
    #   34  the `STAGE1_INCLUDES_LATERAL` comment, which is the canonical
    #       record of an owner ratification: four evidence findings with their
    #       numbers, the named re-enable condition, and the non-obvious
    #       consequence that R17's Fc sweep goes dormant with its producer
    #   13  the MEASURE non-deferring branch, whose comment claimed NO
    #       production caller builds that shape — the pause makes it the
    #       shipped one, and #2291's entry baseline needed saying why it
    #       follows the fit without deferring it
    #   11  the sweep-trigger and module-docstring notes: where a reader lands
    #       asking "why is there no fc_selection?"
    #    7  the relay-capacity arithmetic, now labelled as the WALK-ARMED case
    #       with a do-not-spend-this-slack rule, so a later round cannot raise
    #       N on the strength of a paused count and make re-arming a refusal
    #   -4  net trims where the old prose was simply replaced
    # No seam to cut: this diff adds no logic, and extracting comments from the
    # constants they explain is how a flag stops carrying its own reasons.
    # (+1 more in the gate fix round: the module docstring's OTHER stage-1
    # count, 56 lines below the one the pause corrected, still said "10 entries
    # at the full tier's shipped defaults" — stale since R15 turned the
    # pre-apply cloud off, and a docstring that contradicts itself twice on one
    # screen is worse than either number alone. 12,813 + 62 = 12,875.)
    #
    # 2026-08-18 (Fc-sweep compute budget, #2706): 12,875 -> 12,883. +11 added,
    # -3 removed, net +8, counted after rebasing onto the pause above rather
    # than added on paper:
    #    2  two imports — `fc_sweep_budget_s` and `FC_CORNER_COMPUTE_COST_S`,
    #       now that the budget is DERIVED from the corner count instead of
    #       being a bare constant this module re-exported
    #    6  net on `_fc_evaluation_budget_s`, which took a `planned` argument
    #       and a docstring saying why: a plan the household's declarations
    #       narrowed must ask for less wall than a full one, and the sizing
    #       rule itself lives one module over. A budget that silently ignored
    #       its plan size is the defect this PR fixes, so an arity with no
    #       stated reason is the shape most likely to be "simplified" back.
    # No seam: the delegate is three lines of dispatch, and its whole job is to
    # be the substitutable seam production binds (#2354).
    #
    # 2026-08-18 (session trims): 12,883 -> 12,974, this PR's own net of +91
    # (124 added, 33 removed) on top of the ceiling as it stands. Roughly a
    # sixth executes: both capture-plan builders compose the position groups'
    # unannounced summed sweep and size their cloud entries from it, the
    # session holds that program as a fourth, and `announced_capture_indexes`
    # threads into both session-spec builders — which is what lets the consent
    # screen state WHICH captures beep instead of asserting a shape. (The
    # asserted shape SHIPPED and was false: stage 1 announces two captures. A
    # hand-written sentence is exactly what cannot be checked against a plan.)
    # The rest is why: which phases the courtesy prelude announces and why the
    # entry baseline is one of them, and what `DEFAULT_CLOUD_VERIFY_POSITIONS`
    # at its floor gives up — the walk's only above/below-mark-height pose.
    # Both are decisions a reader has to be able to re-derive from the file,
    # and neither is visible in the code that implements it.
    #
    # 2026-08-19 (A9, the prescription door): 12,974 -> 13,041, +68 added / -1
    # removed, counted from the diff rather than estimated. Roughly a fifth
    # executes. Where the lines went:
    #    3  the `BlendPrescription` type-only import
    #    2  the two ctor arguments — the prescription, and the digest of the
    #       document that carried it
    #   12  the two fields and their contract: held-not-re-judged (the twin of
    #       `alignment_prescription` two lines above), and WHY the field is
    #       `_prescribed_blend` rather than `_blend_prescription` — the latter
    #       is already the METHOD that decides which source wins, and a field of
    #       that name would silently shadow it. That trap was live in this PR's
    #       first draft, so the sentence that stops it recurring is paid for.
    #   14  `_blend_prescription`'s docstring gaining source 0 (-1 where the
    #       "Two sources" line was replaced). This method's whole content is a
    #       precedence order, and an order with an unexplained first entry is
    #       the shape a later reader "simplifies" back — the argument is that a
    #       deterministic instruction quietly beating a staged one would make
    #       the staging step a no-op nobody could see
    #   14  the branch itself: the lazy seam import (5, this module's local
    #       convention for `crossover_v2` leaves) and 9 for the read, 6 of them
    #       saying why it goes THROUGH `blend_prescription_to_candidate_fields`
    #       rather than off `.filters`. That is the one-door property: the seam
    #       re-asks the route, so "a boost can never populate this field" stays
    #       true of the function rather than of today's call graph
    #   23  `blend_prescription_record`, the sibling of
    #       `alignment_prescription_record` — 5 executing, the rest the
    #       None-means-solved rule (without it a series cannot tell a prescribed
    #       round from a deterministic one, which is the comparison the whole
    #       prescriber loop exists to make possible) and why the digest travels
    #       beside the prescription rather than inside it
    # No seam to cut: the lifecycle this door opens — placing, taking,
    # consuming, withdrawing, re-validating — is ~540 lines and every one of
    # them is in a NEW module (`crossover_v2/prescription_spool.py`), which is
    # where a ratchet-respecting change puts them. What lands here is only what
    # the SESSION must know, and this file learned no new fact about
    # prescriptions beyond "one may arrive".
    #
    # ...and 13,041 -> 13,055 (A9 gate round 1, SF-2), +14 net. The provenance
    # record was ONE dict and is now a record plus a digest, because the gate
    # found stage 2 rehydrating `None`: the durable record has to round-trip
    # through `blend_prescription_from_mapping` for the grading stage to read
    # it, and that reader refuses an unknown field rather than ignoring it — so
    # the digest folded in beside the prescription made the whole record
    # unreadable. Splitting it costs a 13-line `blend_prescription_sha256`
    # property (5 execute) and 1 net on the record's own body; the remaining
    # lines are the paragraph recording that the strictness is CORRECT — it is
    # why a receipt cannot bank half a prescription — so the next author moves
    # the field rather than loosening the reader.
    #
    # ...and 13,055 -> 13,053 (#2732 P2, the angle walk's take), a LOWERING
    # across a change that added ~95 lines of behaviour. It is paid for by a
    # cut taken first: the R16 lateral-evidence block — `LateralPose`,
    # `LateralPoseCurve`, `_primary_sweep_bands`, the shared grid and its two
    # constants — moved verbatim to `crossover_v2/spatial.py`, whose charter
    # (what a take records) already covered it, leaving 17 re-export lines
    # here. 13,055 -> 12,958 -> 13,053. What the +95 buys: the walk's own pose
    # table and consumer threaded into the four builders and the session; the
    # ONE predicate (`_adjudicating_walk`) and the suppression guard that keep
    # #2711's paused statistic unreachable from an evidence walk; and per-pose
    # evidence retention, which did not exist at all. Partly offset by one
    # `_hand_to_retention` replacing what would have been a THIRD copy of the
    # fail-soft retention boundary.
    #
    # ...and 13,053 -> 13,072 (#2753 gate round 1), +19. The gate found the
    # angle walk's capacity gate carrying its OWN copy of the plan's retake
    # arithmetic, and wrong: it added the geometry-retry budget
    # unconditionally, while a plan budgets those only when a cloud group is
    # planned, so it refused the two largest LEGAL walks (23 and 24 stops on the
    # shipped shape — the relay accepts both). +21 is
    # `stage1_plan_max_attempts`, the one producer both the plan builder and the
    # seam now read, minus the 8-line inline expression it replaced; the rest
    # is the pause comment's mechanism sentence, which named one decider where
    # there are now two, and the comment saying why a pose's retention runs
    # outside the close lock. A second copy of a gate's arithmetic is the
    # defect; this is what removing it costs.
    #
    # 2026-08-20 (PR-B, the per-driver class's round wiring): 13,072 -> 13,225,
    # +170 / -17, and TWELVE of the added lines run. Hunk by hunk:
    #    13  the `_prescribed_driver` field, 1 executing. The rest is the two
    #        ways it differs from the blend field beside it, both of which a
    #        reader would otherwise have to derive: its door is the candidate's
    #        role-keyed `linearization` rather than a region list, so the merge
    #        lives where the fit is final and not in a reader here; and it
    #        carries NO digest twin, because the blend one exists solely for the
    #        receipt and this class has no receipt lane yet — a field nothing
    #        reads being the permissive-default trap this file's neighbours name.
    #     7  the hand-off in `_build_candidate`, 1 executing, saying why this
    #        one is passed RAW where its neighbour goes through a reader: the
    #        blend field has three sources to rank and this has none, so a
    #        method would be a pass-through with nothing to decide.
    #     3  `_blend_prescription`'s source 0 naming its CLASS. The enumeration
    #        read as "the prescription path" and there are now two; the
    #        deletion-adjacent trap is that the sentence never had to mention
    #        the other class to become misleading about it.
    #    32  `_mic_trust_ceiling_hz`, net +29, of which 2 execute. The gate's
    #        BLOCKER: the tier it scans for is what stops the delta probe
    #        grading above the microphone's own trust limit (#2649, ~90% of the
    #        squared error on the 2026-08-16 round), and a per-driver document
    #        naming every role used to leave no entry carrying one — ceiling
    #        `None`, silently. Two of the four `None` arms were silent before
    #        PR-B as well, on every ineligible or failed fit; all four now say
    #        which one it was through the slug that already existed. The lines
    #        are the four-case enumeration, why the tier is read off the
    #        CANDIDATE (the only carrier that crosses into the grading stage,
    #        so a session field could not answer on stage 2), and the reason a
    #        first-entry scan is sound (one round, one microphone). Four are
    #        given back by the inline `log_event` the helper replaced.
    #     4  the VERIFY-prediction coherence comment, net +4, zero executing:
    #        SF1 makes "the exact thing the emitted graph now carries" true for
    #        a prescribed round too, and the comment now says through what.
    #    90  the pre-Apply bar becoming TWO bars, ~10 executing — the ruling
    #        this file is the right home for, because the module docstring of
    #        the gate it feeds says the threshold and the reason code are the
    #        CALLER's to own and that it never branches on either. `_prescribed_
    #        roles` reads which branches a candidate carries by document rather
    #        than by fit (off the graph, not off session state);
    #        `PRESCRIBED_NON_WORSENING_DB` is its own named datum beside the
    #        reader that chooses; and `_assert_accountable` gains the branch. The
    #        rest is the argument, which is the whole point of the entry: 0.5 dB
    #        is a POOLED-RMS figure measured on the fit, a per-driver
    #        prescription is a narrow high-Q filter aimed at ONE banked feature,
    #        measuring 0.077-0.152 dB pooled
    #        even when it is exactly right, and applying the fitted bar to it
    #        would refuse the class before its first hardware exercise rather
    #        than judge it. A number that blocks a whole class needs its reason
    #        beside it or the next reader deletes the branch.
    #     3  the type-only import; 1 the ctor argument; 3 the second reason
    #        code's import and export.
    # The seam WAS cut, and it is where the behaviour actually went: the merge
    # and the prediction recomposition — the two new decisions in this change —
    # are `crossover_v2.planning.build_candidate`'s and
    # `crossover_v2.intervention.compose_linearized_prediction`'s, both uncapped
    # modules that already own candidate assembly and the summed model. This
    # file learned that it may HOLD a second prescription class, which is a fact
    # about the session, and that its ceiling reader has four ways to have no
    # answer rather than one; it learned nothing about linearization.
    # 2026-08-20 basin pin: 13,225 -> 13,232. Argued in the dated block at the
    # end of this dict, beside the `program_analysis` bump it moves with.
    # 2026-08-21 topology pin: 13,232 -> 13,282. Argued in the dated block at
    # the end of this dict, with the two files it moves with.
    #
    # 2026-08-21 (channel-map CROSS test becomes an isolation ratio):
    # 13,282 -> 13,299, stacked on the topology pin above rather than counted
    # on paper against the pre-pin number. +17 physical, split MECHANICALLY
    # (not estimated) into 8 code + 9 comment: two imports and six emit lines,
    # so `_log_check_diag` carries each role's `channel_map_isolation_db` plus
    # BOTH constants those ratios are read against — the bound, and the target
    # rise above which the ratio was judged at all. The 9 comment lines are why
    # both have to be on the line: household copy for this refusal is
    # number-free by design, so the diag IS the operator's record of it; and
    # below the threshold an isolation figure is published having decided
    # NOTHING, so the bound alone would let a sub-bound number read as the
    # cause of a refusal that never happened. Printing them rather than
    # implying them also stops a future retune silently re-reading old lines.
    # (The gate's fix round added the threshold and corrected this note's own
    # split, which claimed 3 + 6 for the pre-round +9 when it was 4 + 5.) The
    # metric itself is `program_analysis`'s, at the end of this dict; this file
    # only reports it.
    # 2026-08-22 corner-hunt deletion (plan ticket 2.3): 13,299 -> 12,996,
    # LOWERED with the cut rather than left as slack. Out went the Fc candidate
    # sweep's four session methods and its commit path, the candidate-set
    # delegate, the `_adjudicating_walk` predicate, the lateral walk's
    # adjudicating close, and `STAGE1_INCLUDES_LATERAL` with its evidence block.
    # The ratchet's rule is that room freed by a deletion is returned, so the
    # next diff that wants it has to say what earned it. 19 of the 303 lines
    # freed were spent back, all on prose the same deletion falsified and none
    # on behaviour: `_settled_group_verdict`'s R16 comment said a dropped last
    # pose must close the walk "or the session would end with no candidate at
    # all" (MEASURE publishes it now; what the close owes is the journal record
    # that the walk ENDED); this ceiling's own note quoted the assert-side
    # figure as though it were the binding one, when the walk-armed row lands ON
    # 32; `commit_intervention_proposal` described one method under two
    # contradictory names after the second commit route was deleted; the
    # previous-graph guard listed a dead door as live where the live second door
    # is an operator's topology pin; and four sites still narrated a sweep
    # scoring six corners. A ceiling that refused those would be buying a
    # smaller number with a falser file.
    #
    # 2026-08-22, the nanny burn-down, stacking on the cut above: 13,013 ->
    # 13,008. Deleting item 2's refusal takes the two-code selection branch, the
    # extra `assess_accountability` argument, two vocabulary re-exports and two
    # `__all__` entries with it; the account of WHY it went lives on the module
    # that owns the gate, and this file points at it rather than restating it.
    # The ratchet caught that PR TWICE — first when its explanation grew the
    # file its code was shrinking, and again when the fix round's own six prose
    # corrections did the same — so every one of them was rewritten in place.
    # Both are the guard refusing the trade it exists to refuse.
    "jasper/active_speaker/crossover_v2_flow.py": 13_076,
    # ...and 9,292 -> 9,296, +4 physical / 0 logical: the sweep caught that
    # comment overclaiming its own readership ("the surface /state, the doctor
    # and the done screen read" — no renderer reads it today). It is a forensic
    # state key, and saying which it is costs four lines on a surface whose
    # whole job this round is telling "measured" from "not measured".
    #
    # 2026-08-18 (#2662, capture-source seam slice 1): 9,333 -> 8,349, the
    # file's first cut since this ratchet was set — the wheels-report
    # direction ("the host sheds relay interleaving") finally moving. From
    # main's banked 9,333 baseline (the ratchet repaired directly in
    # cb3e4f462): the relay extraction moves 1,017 lines out (the plan-walk
    # hosting, the phone phase ladder, the purge grace, the link-TTL policy
    # — now `correction_crossover_v2_relay.py`, capped below so the relay
    # choreography cannot quietly re-accrete host concerns either) and adds
    # 25 back (the re-export block and the two seam comments), net −992; the
    # PR's gate fix round adds 8 more — the re-export block's PATCH
    # CONTRACT, stating which three names the host actually calls (a double
    # patched there reaches the preparers) and which three it merely
    # re-publishes (a double must patch the provider, or it rebinds a name
    # nothing reads). The reviewer probed exactly that trap; prose that
    # stops the next test author shipping a silent no-op patch is worth
    # eight lines. 9,333 − 992 + 8 = 8,349.
    #
    # ...and 8,349 -> 8,381 (lateral pause), +32 net (40 added, 8 removed),
    # counted rather than estimated:
    #   25  `_post_apply_grade`'s absent-vs-incomplete rule. The pause exposed
    #       a hidden coupling: with no candidate sweep there is no
    #       `fc_selection`, and two gates read its absence as an unfinished
    #       comparison — so every successful commission graded INCONCLUSIVE and
    #       told the household a tune that IS applied "changed nothing
    #       automatically", dropping the Undo pointer. The prose is the
    #       finding: it states why absence is exempt, why the exemption is
    #       sound (this grade asks "was it checked afterwards", which VERIFY
    #       answers alone), and that a sweep which RAN and did not finish is
    #       NOT exempt. Both directions are pinned.
    #    3  `authorized_winner` restructured to carry that distinction
    #    5  the remote hold budget, which quotes stage 1's wall-clock ceiling —
    #       the pause moves it 2520 -> 1800 s, leaving the ceiling at exactly 3
    #       holds of a 3-capture stage, which is the reader's next question
    #   -1  net from the gate-A line replaced in place
    #
    # ...and 8,381 -> 8,392 (Fc-sweep compute budget, #2706), +11 added / 0
    # removed, of which 3 execute:
    #    1  the `fc_sweep_result_wait_s` import
    #   10  the two relay mint sites, 5 each: one call passing the derived
    #       result wait onto the CaptureSpec, and four saying why THIS module
    #       is where it is minted. The capture page ships as a separately
    #       deployed bundle, so a page-side copy of a Pi-side budget is a
    #       cross-artifact drift whose failure mode is a TERMINAL sweepFailed
    #       — the household loses a completed capture rather than getting a
    #       degraded advisory. A reader who deletes the comment as noise is
    #       one step from deleting the call as redundant.
    # Both sites carry it because both mint a stage-1 session that sweeps; a
    # helper wrapping one call and one comment would be indirection, not a
    # seam.
    #
    # 2026-08-18 (session trims): 8,392 -> 8,395, +3. The `summed_program.wav`
    # fill-if-absent comment stopped claiming its content is byte-identical
    # across every summed-sweep phase — since the courtesy prelude rides only a
    # session's opening capture, the compared pair carries beeps the position
    # groups do not, and a diagnostic copy that says otherwise misleads the
    # replay it exists for.
    #
    # 2026-08-18 (#2662 W2b, the wired provider): 8,395 -> 8,571, +176 on the
    # session-trims baseline above. The slice-1 note records the host
    # shedding the relay's choreography; this bump is the OTHER half of the
    # same seam becoming real — the host must now CHOOSE a provider, and the
    # choosing is host policy no provider may own. What landed, counted:
    # `drive_group_close` (+27 with its docstring — the D1 group-close
    # sequence hoisted to ONE owner, so the relay's closure and the wired
    # runner drive the identical persist/confirm/persist instead of two
    # restatements); the per-source resolve/mint/build helpers
    # `_resolve_prepare_capture_source` / `_mint_source_session` /
    # `_build_source_run` (+106 — the fork stated once, called from both
    # preparers, so stage 1 and stage 2 cannot resolve the source
    # differently; #2706's `result_wait_s` threads through the mint helper's
    # relay branch, which is where 3 of the lines went); the two preparers'
    # fork call sites and the wired completion signal (+27); and
    # `V2PreparedSession`'s two new fields with their contracts (+16). The
    # wired choreography itself — the walk, the recorder, the integrity
    # accounting — is ~1,370 lines in its OWN modules
    # (`correction_crossover_v2_wired.py` and
    # `audio_measurement/wired_capture.py`, both capped below), which is
    # where a ratchet-respecting change puts them; this file learned no new
    # fact about ALSA. 8,395 + 176 = 8,571.
    #
    # ...and 8,571 -> 8,592 (#2720 gate round 1, S3): +21, the relay
    # precondition moved INTO the source gate — `_resolve_prepare_capture_
    # source` now asks correction_setup's `_require_relay_base` (the one
    # owner of the question and its message) for a relay-resolved session
    # BEFORE any evidence bundle opens, restoring refuse-before-side-effects
    # for a relay-less Pi. Ten of the lines are the docstring recording what
    # the dispatch-order reorder had silently cost (a refused start
    # abandoned the prior bundle) and why the dispatch's later read is now a
    # plain re-read. 8,571 + 21 = 8,592.
    # ...and 8,592 -> 8,595 (#2720 delta round): +3 net, the SAME gate moved
    # to the same position in `prepare_v2_verify` — the delta review found
    # stage 2 still opened its bundle 71 lines before resolving the source,
    # so a refused verify-start cost the side effect stage 1 had just been
    # cured of. The bundle-untouched pin now runs both preparers.
    # ...and 8,595 -> 8,609 (A4, the remote session-open count): +14, which
    # splits mechanically as prose +13 / code +1.
    # `crossover_v2_remote_session_open` announced the cloud-INCLUSIVE shape
    # target (10) where the shipped stage 1 walks 3, to a reader with no screen
    # to check it against; the emitter's own comment carries the incident and
    # the direction it fails in, and is deliberately not restated here. The
    # code +1 is a MOVE, not growth: the index->phase map `_open` used to build
    # inline is hoisted beside the three `STAGE1_INCLUDES_*` flags that decide
    # it (+8 at the hoist and the two call sites, -7 at the inline build it
    # replaced), so the announced count and the walked plan are one object. The
    # prose is what stops a future reader "simplifying" this back to the shape
    # target — the failure mode this entry exists to make expensive.
    #
    # 2026-08-19 (A9, the prescription door): 8,609 -> 8,706, +99 added / -2
    # removed, counted hunk by hunk. This file is the flow's untrusted-input
    # boundary and its only durable-state writer, so both halves of the door's
    # policy — when a document may be taken, and what a refusal costs — belong
    # here and nowhere else:
    #   19  `observe_restore` (-1 on the docstring line it extends). Two lines
    #       execute; the rest is why the withdrawal runs AHEAD of the no-state
    #       early return. A staged prescription is the one thing this function
    #       clears that does not live in `state`, and a lost state file resolves
    #       the next round's ordinal back to 1 — an ordinal a surviving document
    #       could legitimately match. Guarding it behind a readable state file
    #       would be the #2699 trap this docstring already calls standing,
    #       reintroduced one indirection further out.
    #   51  `_take_staged_blend_prescription` — renamed `_take_staged_
    #       prescription` by PR-B, when it learned the second class — the ONE
    #       place a staged prescription enters and the one place its refusal
    #       becomes a round that carries on. ~14 execute. The rest states the
    #       two directions and why they are not in tension: fail-CLOSED on
    #       content (a document that is stale, tampered, oversized, or aimed
    #       where its class may not correct never reaches the candidate) and
    #       fail-OPEN on transport (the round still runs, on its class's own
    #       deterministic answer, because an optional instruction must not cost
    #       a household a measurement session). Named and module-level on
    #       `alignment_prescription_prior_from_state`'s rule, which is what
    #       makes the seeding path drivable without a relay.
    #   11  the durable `verify_priors.blend_prescription` key, 3 executing. It
    #       crosses stages for `alignment_prescription`'s reason, read the same
    #       way — stage 1 TAKES the prescription and stage 2 banks the receipt,
    #       so durable state is the only channel it has. Without it the
    #       attribution dies in stage 1's process and no series can be read back
    #       as prescribed-versus-solved.
    #    7  the series-position hoist at the ctor, 2 executing. The ordinal is
    #       resolved ONCE and used twice — to take the prescription and to
    #       hydrate the session — because a second read could be answered by a
    #       state write in between and hand this round an instruction written
    #       for another one.
    #   11  the two ctor arguments and why they ride the MEASURING stage (-1
    #       where the inline `series_position=` call was replaced by the hoisted
    #       local): the door is `_blend_prescription`, which runs at
    #       candidate-build time, so a prescription handed to any other stage
    #       would be held by a session that never builds a candidate.
    # No seam to cut: the spool's own lifecycle is a new module, and the two
    # policy questions above are exactly the ones a capture provider may not
    # own — the same line slice 1 drew when the relay's choreography left and
    # the source CHOICE stayed. 8,609 + 97 = 8,706.
    #
    # ...and 8,706 -> 8,774 (A9 gate round 1, SF-2), +68, counted hunk by hunk.
    # The gate found that the feature's whole point — banking WHO prescribed a
    # round — was erased by stage 2 of that same round: `verify_priors` is
    # rebuilt from the conductor on every persist, `alignment_prescription` has
    # a stage-2 rehydration arm, and `blend_prescription` had none, so stage 2
    # wrote `None` over the stage-1 record BEFORE the receipt. The #2698 shape
    # exactly, one module over. What it cost:
    #   47  the two readers, `blend_prescription_prior_from_state` and
    #       `blend_prescription_sha256_from_state`, ~12 executing. They sit
    #       beside `alignment_prescription_prior_from_state` and mirror it
    #       exactly. The prose is the finding: that this arm is not merely how
    #       stage 2 LEARNS the prescription but the only thing stopping stage 2
    #       ERASING it, which is the sentence that stops it being deleted as a
    #       redundant read of a value stage 2 does not use.
    #   12  the `blend_prescription_sha256` persist key, 3 executing, with the
    #       reason it cannot live inside the record it describes.
    #    7  the two rehydration reads in `prepare_v2_verify`, 2 executing.
    #    2  the two ctor arguments on the stage-2 session.
    # No seam to cut: this file owns the state's read and write sides, and a
    # reader for one of its own keys placed anywhere else would be a second
    # owner of that key's shape — the exact drift the neighbouring reader/writer
    # pairs exist in this file to prevent. 8,706 + 68 = 8,774.
    #
    # ...and 8,774 -> 8,804 (#2464, ruled 2026-08-19), +30 net (37 added, 7
    # removed), counted hunk by hunk, of which 3 execute:
    #    3  the `verify_failed` predicate, plus +1 net on the `state` chain it
    #       reorders — the whole behavioural change is which arm runs first
    #    2  the docstring sentence saying a failed mark-VERIFY caps `state`,
    #       pointing AT the derivation rather than restating its argument
    #   24  that derivation's argument, in three paragraphs that each stop a
    #       different revert. The defect (a closed post-apply group made the
    #       fail and inconclusive arms unreachable, so a re-verify that failed
    #       reported `graded=True` and the doctor ticked green); why the two
    #       instruments are a UNION and not a fallback (`outcome` grades
    #       capture/tracking health only, so an absolute-claim miss rides a
    #       clean `pass`, and an absent or non-numeric tracking max is an
    #       `outcome` fail whose claim is `not_evaluated` — a reader who keeps
    #       one instrument reopens exactly one of those two cells); and #2160's
    #       ratified rider that geometry and k-of-N facts stay un-co-located,
    #       naming the three neighbouring fields this cap does not touch.
    #       3 of those 24 are the gate round's: WHY the predicate names two
    #       claims and not the record — a VERIFY's one summed sweep leaves both
    #       per-branch claims structurally `not_evaluated`, so the enumeration
    #       IS the graded claim set, not a subset of it (gate nit 1).
    # No seam to cut: this is one predicate and one ordering inside the single
    # function that owns the grade, and both #2098 and #2160 already ruled that
    # the producer — not its consumers — answers this question once.
    #
    # 2026-08-19 (Fc/slope apply path): 8,804 -> 8,889. +85 net (162 added, 77
    # removed), counted rather than estimated, and ~27 of the added lines
    # execute — the gate fix round that took it from 8,875 added prose only.
    # What earned them, in the order the function reads:
    #    22  the derivation that REPLACED the `fc_selection` read (which took 10
    #        of the 77 removed). The apply now asks the candidate what crossover
    #        it carries instead of asking a persisted record what it claims —
    #        one artifact answers, so the declaration and the emitted graph
    #        agree by construction rather than by cross-check. The extra lines
    #        over the read they replace are the retry resume (once the
    #        declaration carries the candidate's crossover, NOTHING live can
    #        still say what it displaced, so the inverse has to be persisted at
    #        the moment of the save) and the guard that refuses a review holding
    #        a revision without one.
    #    15  the hearing-safety BOUNDARY, 8 of them executing. This is the one
    #        deliberate duplication in the whole path: the L0 emit gate refuses
    #        the same condition, but it can only refuse AFTER the declaration
    #        write (`baseline_profile`'s staleness guard requires that
    #        ordering), so on its own it would leave `/sound` declaring a corner
    #        the speaker is not playing and cannot be made to play. The seven
    #        prose lines are that ordering argument, which is exactly the thing
    #        a later reader would delete as redundant.
    #    16  the two lines of the new review-scoped state key plus their
    #        argument — why it takes `accepted_sound_revision`'s session-gated
    #        shape rather than `sound_declaration_undo`'s unconditional one, and
    #        why it is cleared with the token it belongs to.
    #    14  (gate fix round) the disclosure that the boundary is scoped to the
    #        declaration-writing arm, and why the as-declared below-floor case
    #        is left to re-raise raw. The gate's nit was that this PR puts two
    #        below-floor refusals side by side and names only one; the argument
    #        for not closing it — that converting the re-raise would make this
    #        function the owner of how every L0 gate reads here, which is
    #        #2736's residual to widen with tests per gate — is the kind a later
    #        reader would otherwise "simplify" by making them symmetric.
    #    15  `_crossover_label` and its four call sites, which name a slope ONLY
    #        when the slope is what moved. The declaration write carries two
    #        parameters now, and every refusal on this path tells the household
    #        what is currently sitting in Sound — so on a slope-only accept the
    #        old copy would have named the one number that did not change. Used
    #        by the Undo sentences and the four apply refusals alike, because
    #        they are the same sentence about the same declaration.
    #     3  the remaining net across `_parse_sound_declaration_undo` (which
    #        SHRANK — it delegates the crossover half to the same reader the
    #        apply path resumes from) and `_restore_sound_declaration`'s swap.
    # A seam WAS cut, and it is where the growth would otherwise have been: the
    # geometry vocabulary, the difference between a declaration and a preset,
    # the record Undo reads back, and the floor predicate all went to the new
    # `jasper/active_speaker/crossover_declaration.py`, capped at birth just
    # below for the reason the relay and wired providers are: a module born to
    # keep a capped file from growing is the next place growth goes. This file
    # learned no new fact about crossovers beyond "this apply may owe the
    # declaration a write".
    #
    # 2026-08-19 (#2732 P2, the angle walk's take): 8,889 -> 8,984, +95, no
    # deletions. Hunk by hunk:
    #    68  `_take_staged_angle_walk`, the module-level twin of
    #        `_take_staged_prescription` beside it (then still spelled
    #        `_take_staged_blend_prescription`) and named for that
    #        function's reason (a take drivable in a test without a relay). 27
    #        of the 68 execute; the rest is its contract and the two journal
    #        lines, which carry the deciding numbers so the take is readable
    #        from the journal alone.
    #    22  the call site: one take, feeding the index map, the emitted spec
    #        and the conductor's two kwargs, plus the map rebuild a taken walk
    #        needs.
    #     5  threading `lateral_prompts` / `lateral_consumer` into the spec
    #        build and `hydrate`.
    # No seam to cut here, and the composition is not this module's: resolving
    # a request into poses and refusing an incompatible pair belongs to
    # `active_speaker/angle_capture.py` (uncapped, and the module that already
    # owns every other angle question), and the consumer vocabulary belongs to
    # `crossover_v2/journey.py`. What lands here is the take itself — which is
    # exactly the fact a session host owns — and this file learned no new fact
    # about angles, poses or programs.
    #
    # ...and 8,984 -> 9,022 (#2753 gate rounds 1-2), +38, all of it inside
    # `_take_staged_angle_walk` and its contract:
    #    19  the take was not fail-closed. `take_staged_angle_request` re-raises
    #        the seam's bare `CrossoverV2FlowError` for a banked stop it can no
    #        longer build (a hand-edited angle) — deliberately un-wrapped, so it
    #        carries no slug — and that class ESCAPED, killing the session open
    #        with no journal line. It is a third refusal class; it is now caught
    #        in its OWN except arm (mypy cannot narrow an isinstance flag),
    #        slugged, and journalled with the producer's own sentence.
    #    12  `consumed=true` was a literal the spool contradicts on its two
    #        unreadable arms, which deliberately do not consume so a permissions
    #        mistake cannot destroy the evidence of itself. It is read back now.
    #     6  `plans_cloud_group` threaded through, so the capacity gate asks the
    #        budget this session will actually emit.
    #     1  gate round 2, nit A: one 125-char comment line wrapped to the
    #        block's convention. No words changed, and E501 is not in the ruff
    #        set, so the wrap is the whole of it.
    #
    # ...and 9,022 -> 9,079 (PR-B, the per-driver class's round wiring), +57, no
    # deletions, ~14 executing. Every line is on the ONE take — this file's own
    # charter, since it is the flow's untrusted-input boundary — and the split
    # it now performs:
    #    19  the take's contract gaining the two-class paragraphs: that
    #        `accepts` is now `STAGEABLE_KINDS` and what each class lands in,
    #        and that the pair is returned already SPLIT, made here on the
    #        envelope's class field rather than by `isinstance` (the spool's own
    #        rule) and here rather than at the call site, so the class
    #        vocabulary has one reader instead of two to keep in step.
    #    17  the journal line and the split return, 8 executing. `roles` is the
    #        per-driver class's deciding number — WHICH branches stopped being
    #        fitted this round — and `prescription_kind` is the document's class
    #        beside a `prescription_class` that has meant cut-versus-boost since
    #        it shipped; the comment is there because one key carrying two facts
    #        is exactly the defect a reader would otherwise introduce, and the
    #        `cast` earns its own sentence because narrowing a type is not the
    #        same act as deciding a class.
    #     6  the ctor argument at the hydrate, and why the per-driver class
    #        rides the MEASURING stage for the blend argument's reason with a
    #        different door.
    #     4  the call site: one take, two locals.
    #     9  imports and the call itself — the `driver_prescription` block (4),
    #        `STAGEABLE_KINDS` (1), the take call spreading to carry `accepts=`
    #        (2), and the `typing` line wrapping to take `cast` (2).
    #     2  the fail-open/fail-closed paragraph, which named ONE class's
    #        deterministic fallback ("decision 10's instruction") where there
    #        are now two — the per-driver arm falls back to the Layer-1a fit.
    # No seam to cut: the take, the refusal-that-leaves-a-round-running, and the
    # split are one act, and it is exactly the act an untrusted-input boundary
    # owns. What did NOT land here is the merge — see the flow's entry above.
    # 2026-08-20 basin pin (gate fix round SF1): 9,079 -> 9,083. Argued in the
    # dated block at the end of this dict, with the two files it moves with.
    #
    # 2026-08-20 (capture level/graph provenance): 9,083 -> 9,182. +99, and the
    # extraction the ratchet asks for DID happen — every line of logic went to
    # the new `jasper/active_speaker/capture_provenance.py` (the block's shape,
    # the live reads, the per-field fail-soft, the never-raise outer belt, the
    # one-capture recorder). What stayed here is the wiring that only this file
    # can do, because only this file knows the facts:
    #   11  `capture_dump_enabled`, so "is retention on" has ONE reader now that
    #       the play seam asks it too — previously an inline `.exists()`.
    #   ~23 three optional `provenance` parameters and their docstring
    #       paragraphs, on `bind_production_play`, `bind_production_analyze`,
    #       and `bind_v2_stage_seams`. Each says what the recorder is FOR at the
    #       seam it reaches, because a bare `Any = None` parameter on three
    #       seams is the shape most likely to be deleted as unused.
    #   ~30 the two playback branches and the `play_wav` wrapper. This is the
    #       load-bearing part and it cannot move: `_play_body` is the ONE place
    #       that knows whether the transient routing graph was loaded, and the
    #       wrapper is the only point inside the writer lock where the loaded
    #       graph is still live. The comments say why the observation sits
    #       outside the phase-ladder wrapper and inside `play_program`; get
    #       either wrong and the recorded graph is the applied one.
    #   ~20 the sidecar write, the recorder constructions at the two session
    #       call sites, and the forwards.
    #   ~15 imports and the `cam` hoist.
    # Trimmed to this after a first pass measured +144: the incident narrative
    # and the fail-soft contract now live once, in the new module, and this
    # file points at them rather than restating them.
    # 2026-08-21 topology pin: 9,182 -> 9,350. Stacked on the capture-provenance
    # base directly above rather than re-based from 9,083: the two changes are
    # independent and both are in this file, so the ceiling owes room for each.
    # The largest of the three this pin moves, and the one the relocation pass
    # already shrank — argued in the dated block at the end of this dict.
    # 2026-08-22 Undo-evidence gate (#1863, + its review): 9,350 -> 9,393.
    # +66 added, -23 removed, tallied from the diff hunks —
    #   39  `restore_anchor_static_prefix_refusal`, extracted so the two
    #       preconditions a pure state read CAN answer have ONE owner. The
    #       review's SF2: the first revision inlined them into the status
    #       block, which made this file's own "one owner for a rule with two
    #       readers" docstring false in the very file the change edited. The
    #       new reader cannot call the full resolver — gate 3 loads the live
    #       output topology on every household status poll — so the prefix is
    #       what it asks, and the docstring now says three readers
    #   20  the delegation (`rollback_anchor_refusal` calls the extraction
    #       instead of carrying the two gates, -23 there), the trued-up
    #       docstring, and one narrowed local: the prefix call narrows for a
    #       reader but not for mypy, so the three later `state` reads bind
    #       once rather than each carrying its own `or {}`
    #    7  the `can_undo` key and the comment saying why this reader takes
    #       the prefix and not the five
    # Net +43 against 23 lines removed: the extraction pays for a third of
    # itself, which is what distinguishes it from a third transcription.
    # 2026-08-22 corner-hunt deletion (plan ticket 2.3): 9,398 -> 9,386,
    # LOWERED with the cut rather than left as slack. The Pi-minted per-capture
    # result wait went with the sweep whose compute ceiling it published — see
    # `_mint_source_session`, which now states why the page's own 90 s floor
    # governs instead.
    "jasper/web/correction_crossover_v2.py": 9_430,
    # Born 2026-08-19 (Fc/slope apply path) at exactly this size: what `/sound`
    # DECLARES a crossover to be, what a measured candidate's preset says the
    # same crossover is, and the difference between them — plus the declared-
    # floor boundary the apply path checks before it writes either. It exists so
    # `correction_crossover_v2` above could gain a two-parameter declaration
    # write without gaining the vocabulary behind it, and it should grow only
    # when that vocabulary does: a THIRD spelling of a crossover, or a second
    # protected role. Deriving-and-refusing only — the single durable writer
    # stays in `sound_setup`, in both directions.
    # 2026-08-21 (tuning master plan 2.5): 407 -> 436, +29, no extraction. The
    # version+kind envelope the other three prescription doors already carry
    # (`driver_prescription`/`blend_prescription`'s established shape): two
    # named constants (`CROSSOVER_DECLARATION_CHANGE_KIND` /
    # `_SCHEMA_VERSION`) with their own docstrings, two fields on
    # `change_to_record`'s output, and `change_from_record`'s envelope check.
    # No seam to cut: this is the file's one vocabulary gaining one more fact
    # about itself, not a second concern arriving.
    # 2026-08-22 (same ticket, gate delta): 436 -> 452, +16, no extraction.
    # The initial envelope check refused ANY record missing it — including
    # `sound_declaration_undo`, which #2743 shipped writing three days before
    # this envelope existed and which is carried UNCONDITIONALLY across a
    # deploy for as long as the applied graph is, so a live speaker could
    # already hold a pre-envelope record. `change_from_record` now treats a
    # record naming NEITHER envelope field as that legacy shape (reads as
    # this module's own kind and version 1) while still refusing one naming
    # EITHER field wrong — the retrofit posture the two request-time doors'
    # `_parse_prescription(..., read_back=True)` share, argued in each
    # function's own docstring rather than restated here.
    "jasper/active_speaker/crossover_declaration.py": 452,
    # Born 2026-08-18 (#2662 slice 1) at exactly this size: the relay capture
    # provider — the choreography only the phone-relay source has. It should
    # grow only when the RELAY grows; the wired provider is its own module.
    # (1,080 at birth; the gate fix round deleted a reader-less identity
    # constant, -4.) UNCHANGED by the lateral pause: `relay_link_ttl_s` moved
    # here with the extraction and its docstring quoted stage 1's old 2520 s
    # ceiling, so the pause corrects one number in place, +0.
    # 2026-08-18 (session trims): 1,076 -> 1,085, +9 (18 added, 9 removed) and
    # 0 executable growth — the `quiet_requested` derivation swapped one
    # expression for another. The comment IS the change: the flag used to be
    # read off where the courtesy beeps sit, which since the prelude trim would
    # tell a household to carry on through the one window that has to be quiet
    # (the pilot SNR guard's). A silent-wrong-answer site earns the lines that
    # stop it being "simplified" back.
    # 2026-08-18 (#2662 W2b): the group-close hoist to the host banked -4
    # (the closure now delegates to `drive_group_close`): 1,085 -> 1,081.
    "jasper/web/correction_crossover_v2_relay.py": 1_081,
    # Born 2026-08-18 (#2662 W2b) at exactly this size: the WIRED capture
    # provider — source resolution, the local plan walk, and the answer
    # mint. Its ALSA/scan/encode mechanics live in
    # `jasper/audio_measurement/wired_capture.py` (the measurement kernel,
    # importable by non-web callers, capped below), so this module should
    # grow only when the wired SESSION choreography grows. 747 at birth;
    # #2720 gate round 1 adds +8 (S1's refusal-code precedence argument —
    # the freshest fact wins over a stale rejection stamp, and the comment
    # carries why the relay's inverted twin is flagged-not-changed).
    # ...and 940 -> 938 on the same PR's gate round 2, giving back what the
    # module docstring stopped restating: the retake's four terms are stated
    # once, where they are implemented, and pointed at from here.
    "jasper/web/correction_crossover_v2_wired.py": 938,
    # Born 2026-08-18 (#2662 W2b, capped in the #2720 gate fix round) at
    # exactly this size: the wired capture ENGINE — the measurement-kernel
    # half (device probe, S32 recorder, gap accounting, zero-run scan, WAV
    # encode). It should grow only when the CAPTURE MECHANICS grow; session
    # choreography belongs in the provider above, and analysis belongs in
    # `program_analysis`.
    "jasper/audio_measurement/wired_capture.py": 646,
    # ...and 4,103 -> 4,107 (lateral pause), +4 net: the entry-baseline screen
    # said the household is "BACK on the mark", true only after a walk. With
    # the walk paused this capture follows MEASURE, where the microphone never
    # left, so the copy drops one word and the comment says why it has to read
    # correctly from either predecessor.
    #
    # ...and 4,107 -> 4,117 (#2464), +10: a third arm on the done screen's
    # ungraded copy, 5 of them the sentence itself. The two sentences there
    # already split "never finished" from "could not tell either way" on the
    # argument that the first is FALSE of a check that ran; a check that ran
    # and did not PASS is the third answer that argument covers, and #2464
    # made it reachable behind a closed post-apply group. The remaining lines
    # say which state file reaches it — one carrying no terminal result code,
    # since a result code overrides this copy outright.
    #
    # ...and 4,117 -> 4,127 (Fc/slope apply path, 2026-08-19), +10 and ZERO
    # executable. The stage-1 Apply button's `uses_alternative` is now a SECOND
    # reading of a question the apply path answers from the candidate itself.
    # The two agree on every shape reachable today, so the honest response is
    # neither to change the label logic nor to leave the coupling unwritten:
    # the comment says exactly which producer would make them disagree, and
    # that a third reading here is the wrong repair. A latent second source of
    # truth on a household-facing control is worth ten lines to keep visible.
    #
    # ...and 4,127 -> 4,158 (#2738), +31 net, counted rather than estimated —
    #    2  the cap itself, and the ONLY executable line change. The badge
    #       composition it feeds is net ZERO: 14 lines replaced by 14, an
    #       early return becoming one badge slot plus its caveats.
    #    8  `_done_nudges`' docstring — the result code takes the SLOT and
    #       does not take the caveats with it, and arrives already capped.
    #   21  the comment beside the cap: WHY only the one `ok` code is capped.
    # So +2 executable and +29 the two arguments a household-copy file cannot
    # lose. The twin of the #2464 cap one surface over, and the same shape of
    # payment: "a result code overrides this copy outright" — the sentence
    # directly above, written by that very entry — turned out to BE the
    # defect. `_verify_claims` always writes an `integration` entry, so the
    # override reached every post-R18 session whose VERIFY produced a tracking
    # analysis, and a group that closed FAILED at -4.63 dB renders "Target
    # verified." The 21 buy the part a reader cannot re-derive: the three
    # `warn` codes already refuse the claim, and swapping `keep_previous`'s
    # copy for "Your speaker is tuned, but…" would endorse a result its own
    # grade declined — so capping all four would be the defect pointed the
    # other way.
    # 2026-08-20 basin pin (gate fix round SF1): 4,158 -> 4,165. Argued in the
    # dated block at the end of this dict, with the two files it moves with.
    # 2026-08-21 topology pin: 4,165 -> 4,180. Same dated block, same shape of
    # payment as the basin pin one line up.
    # 2026-08-22 Undo-evidence gate (#1863, + its review): 4,180 -> 4,289.
    # +125 added, -16 removed, tallied from the diff hunks rather than
    # estimated (an earlier revision of this block asserted +51/-14 with an
    # itemization that summed to 45 — the review caught it, so these three
    # groups are the hunk counts and they add to 125) —
    #   78  one new module-level block: `_can_undo`, the `_UNDO_PROMISE_SWAPS`
    #       table, and `_honest_about_undo`. The table is the review's SF1:
    #       gating the BUTTON while the verdict still said "you can undo" left
    #       the first-commission success screen — the screen most new speakers
    #       ever see — naming a control that is not there, and worse off than
    #       before the gate, when pressing it at least returned the endpoint's
    #       honest refusal. FIVE promise shapes, three of which a grep for the
    #       obvious wording misses; the replacements are existing no-anchor
    #       copy, not new sentences
    #   25  nine call sites and the prose they falsified — three gates that
    #       stop asking `applied`, the two mint sites that now route through
    #       `_honest_about_undo`, and four claims this change made untrue
    #       ("Undo is owed the moment something is live", "must stay reachable
    #       regardless", "leave the household with Undo alone", "Undo survives")
    #   22  the done screen's restructure: the promotion if/else, plus hoisting
    #       `next_action` out of the `_envelope(...)` call it was inlined in,
    #       which the one-line rule change does not itself cost
    # Trimmed before being paid for, twice: a first pass measured +74 on the
    # comments alone and was cut by roughly two-thirds on the "point, never
    # re-teach" rule; the long forms live in PR #2836 and in
    # HANDOFF-crossover-measurement-v2.md, and this file points at them.
    #
    # 2026-08-22 corner-hunt deletion (plan ticket 2.3, gate round 1): 4,293 ->
    # 4,297, +4 and no executable line. The Undo button's second reading of
    # "does this apply change the declaration?" justified its own gap as
    # unreachable — "the only producer of a candidate that crosses somewhere
    # other than the declaration is the Fc sweep, which writes `fc_selection`
    # in the same breath". That producer was deleted; the live one is an
    # operator's topology pin, which writes no `fc_selection`, so the gap is
    # REACHABLE and the comment now says which reading can be short (this one,
    # not the apply -- `handle_v2_apply` derives from the candidate's own preset
    # and is the authority). A comment that talks a reader out of a live gap is
    # the one kind this file cannot afford to keep small.
    "jasper/active_speaker/crossover_envelope_v2.py": 4_297,
    # 2026-08-18 (D7, series-2 diagnosis): +82 net on `program_analysis.py`
    # (95 added, 13 removed), counted rather than estimated —
    #   40  the argument written next to `GLITCH_RESIDUAL_SAMPLES`
    #   26  correcting two now-FALSE claims the discontinuity block inherited
    #       as settled (D7's second clause: "every glitch is a step" and "a
    #       clean capture's integer-located residuals sit well under a sample",
    #       both falsified by series 2) plus the observable that correction
    #       leaves behind
    #   14  the residual block's own comment
    #   15  the sub-sample residual loop itself, against 13 removed: the
    #       EXECUTABLE change is +2 lines. Everything else is why.
    # The 40 is the load-bearing part. That absence WAS the defect:
    # a 1.5-sample threshold with nothing recording the resolution of the
    # estimator feeding it sat below its own instrument noise for two series,
    # rejected eight physically-clean captures, and took a round to exactly
    # its retake budget. A threshold in samples is not a fact on its own, so
    # the number and its instrument are now one comment; compressing that out
    # to keep this integer flat is precisely how it happens again.
    #
    # The seam this file wants is real and deliberately NOT cut here: the
    # drift/glitch estimator (`DriftEstimate`, `_sweep_occurrence*`,
    # `_repeat_epsilon`, `_subsample_separation`, `_locate_discontinuity`,
    # `_estimate_drift`) is one coherent "capture timing coherence" concern
    # with a clean boundary. Moving it is a ~300-line relocation inside a file
    # other live sessions of this fix wave are also editing, which is a
    # collision, not a cleanup. Take it in a quiet window and lower this back.
    #
    # …and #2662's prescription lands on top of D7's, in the same file and the
    # same night — the two sessions D7's own note above names as the reason its
    # seam was not cut. Both bumps are real and neither subsumes the other, so
    # the number carries both: 7,060 (D7) + 215 (#2662's own two entries above,
    # measured after the rebase rather than added on paper) = 7,275. The seam
    # both notes name is now named twice by two independent sessions in one
    # night, which is the strongest argument either could make for cutting it.
    # 2026-08-19 (capture slip guard): 7,275 -> 7,252, a CUT rather than a
    # bump, and the ceiling follows the file down so the room is not silently
    # available to the next diff. The single-step timeline model, its
    # admission rule and the 2026-07-27 forensics that motivated it moved out
    # to `jasper.audio_measurement.timeline_slip`, which also owns the
    # measured operating point of the new sub-sample slip gate; what stayed
    # here is the adapter that measures a capture's positions and feeds the
    # model. Net -23: about -50 moved out, +27 back for the adapter's
    # sub-sample placement (index-keyed, so no aliasing argument is owed),
    # the fourth `glitch_inputs` entry, the role-less grouping note the type
    # checker asked for, and the DriftEstimate docstring gaining the one thing
    # a reader of those two fields now has to know -- they populate exactly
    # when the gate fires, and their sign and segment id are ambiguous at an
    # even cut, so the magnitude is the part to read.
    # 2026-08-19 (crossover forward model, PR-2): 7,252 -> 7,262, +10, and the
    # plan that asked for it predicted +0. Its words were "the edit is
    # line-neutral (a literal becomes a name)", which is only true of the USE
    # site; the DEFINITION has to land somewhere, and this file had zero slack.
    # Counted rather than estimated: 9 for `CONFIGURED_PATH_PROTECTION_FLOOR_DB`
    # and the argument beside it, 1 for the refusal message re-reading the
    # constant instead of restating `-12` in prose. Executable change: zero —
    # the same float, the same refusal, the same rendered text.
    # Why it is worth ten lines. `crossover_v2.forward_model.driver_plants`
    # divides the SAME `P` out of the SAME measurements, in the
    # transfer-function domain, so an offline search can apply a different
    # crossover per candidate. Without a named owner the second reader spells
    # `-12.0` again, and a conditioning policy with two writers is the shape
    # this repo has already paid for. The cheaper-looking alternative — leave
    # the literal and let the new module restate it — buys this integer back by
    # creating exactly the drift the ratchet is downstream of.
    #
    # 2026-08-20 (basin pin): the `alignment_prescription` session key gained an
    # optional `polarity`, so a staged round can hold the basin still while it
    # measures something else. Earned by the 2026-08-19 linearization night,
    # where three successive stage-1 fits at ONE physical configuration solved
    # three different (delay, polarity, trim) basins — the measured-best one
    # (off-axis 2.37 vs 3.10 dB pooled) could not be held, and the round that
    # re-rolled into the anti-phase basin measured 3.86 on axis and was
    # auto-rolled back. A two-variable round is not a round.
    #
    #  * `crossover_v2_flow` 13,225 -> 13,232. +7, of which 4 execute: the
    #    conductor hands the record's `polarity_sign` down beside the delay it
    #    already hands down. Three comment lines say why the word->sign
    #    translation lives on the record rather than here — a second translation
    #    site is exactly how the candidate's `keep`/`invert` and the analysis
    #    frame's `normal`/`inverted` would drift.
    #  * `program_analysis` 7,262 -> 7,356. +94 net (115 added, 21 replaced), of
    #    which 30 execute: one prior, one `_build_candidate` kwarg, the selector
    #    kwarg and its one-line grid narrowing, the low-SNR arm's committed
    #    sign, one `AlignmentPairSelection` field with the two lines that read
    #    it, and the basin on two journal lines. The other 64 are prose, and two
    #    thirds of that is one argument the reviewer should get to see: this
    #    field makes `polarity_agrees_with_sum` answerable in a NEW way, because
    #    `_FLAT_SUM_POLARITY_OBJECTIVES` membership had silently meant "both
    #    polarities were scored" and no longer does. Recording that the
    #    membership rule is now necessary-but-not-sufficient, at the set, at the
    #    property, and at the selector, is what stops the next reader restoring
    #    a `True` that no search produced. Compressing it would keep the number
    #    flat and leave the trap.
    #
    # 2026-08-20 (basin pin, gate fix round SF1): the pin reopened #2607 S3 by a
    # NEW route — the household review row worded an operator-pinned polarity as
    # "Inverted (measured)". Both existing guards were structurally blind: a
    # pinned round commits the same `explicit_prescription_committed` an
    # unpinned prescription does, so it is in neither the declared-design list
    # the mirror guard compares nor the "no objective" case. The conductor ruled
    # the predicate moves to a payload BIT with one owner rather than a fifth
    # literal in the renderer's list. That bit is one line per hop, and the
    # comment at each hop says why it cannot be inferred from the two facts
    # already there — which is the whole finding, and the thing a later reader
    # will otherwise try to "simplify" back into an objective test.
    #
    #  * `program_analysis` 7,356 -> 7,374. +18 net, of which 2 execute: the
    #    `CrossoverCandidate.polarity_pinned` field and its carry in
    #    `_build_candidate`. It is carried, never re-derived, for the reason the
    #    `polarity_agrees_with_sum` field beside it records in its own comment —
    #    that one WAS re-derived once, and drifted the moment #2662 widened its
    #    rule. One line of the 18 is the SF2 fix: a comment inside
    #    `_select_alignment_pair` still said a prescription "leaves the polarity
    #    axis to the objective", 26 lines above the `signs` line that had
    #    stopped being true.
    #  * `correction_crossover_v2` 9,079 -> 9,083. +4, of which 1 executes:
    #    `_candidate_summary` reads the bit off the frozen analysis JSON. This
    #    at-cap file needed no room in the parent PR and needs 4 lines here for
    #    the reason the projection exists at all — the renderer sees only what
    #    this summary persists.
    #  * `crossover_envelope_v2` 4,158 -> 4,165. +7, of which 1 executes: the
    #    payload key the browser reads by name. The 6 comment lines are the
    #    ruling itself — why a separate bit and not a fifth member of the
    #    declared-design list, whose membership ALSO governs the anchor
    #    withdrawal a pinned round does not get.
    "jasper/audio_measurement/program_analysis.py": 7_538,
    #
    # 2026-08-21, the TOPOLOGY pin (#2795) — the basin pin's sibling one axis
    # over: a request-time prescription that names a crossover corner AND its
    # order, so a pre-registered Fc/slope tournament can measure a chosen arm.
    # Three at-cap files move, and the seam was cut FIRST rather than argued
    # around: the shapes the pin owns live in the new, uncapped
    # `crossover_v2/topology_prescription.py` (the gate, the value object, both
    # readers, the #2773 contract block) and the web host keeps only what a
    # host owns — gathering this speaker's declarations, translating a refusal
    # into the household's own refusal type, and persisting. Two blocks were
    # relocated out of the host during the gate's fix round for exactly that
    # reason (`candidate_topology`, which reads a candidate's own corner, and
    # `apply_topology_pin`, which was the SAME twelve lines in both stages),
    # taking the host's cost from +201 to +168.
    #
    #  * `correction_crossover_v2` 9,182 -> 9,350. 177 added, 9 removed, +168
    #    net; of the 177, 74 are comment and 4 blank, so 99 lines of code.
    #    Counted, not estimated. This is an untrusted-input boundary, and every
    #    one of those 99 is boundary work the module cannot do for itself —
    #    reading five DECLARATIONS off this speaker's context
    #    (two role bands, the intersected search band, the protected role's
    #    slope, the ka onset), short-circuiting when the request carries no pin
    #    so an ordinary round never depends on declarations it is not using,
    #    translating `TopologyPrescriptionRefused` into `CrossoverV2Refused` at
    #    the tap, persisting the record, and reading it back. It lands TWICE
    #    because there are two stages and both must open at the pin: stage 2
    #    re-opens the grading session there or VERIFY grades an applied graph
    #    for not being the crossover it deliberately replaced. The comment
    #    weight is concentrated in two places a reader cannot re-derive — WHY
    #    the topology gate is read before the delay gate (the delay bound is a
    #    half-period AT the corner, so the other order bounds a 4,000 Hz round
    #    by the incumbent's 303 us lobe instead of its own 125 us), and WHY the
    #    capture spec takes the round's corner rather than the context's (the
    #    announced program is fc-dependent twice over; see that comment).
    #  * `crossover_v2_flow` 13,232 -> 13,282. 51 added, 1 removed, +50 net; of
    #    the 51, 19 are comment and 3 blank, so 29 lines of code — and the
    #    shape is the #2354 door rule holding —
    #    a type-only import, ONE ctor argument, one field, two port arguments
    #    (`topology_pinned` to the candidate set and to the adjudication), one
    #    `RoundEvidence` field, and one null-guard. The only new property is
    #    `topology_prescription_record`, the exact twin of the two prescription
    #    records already beside it. No logic landed here; the file learned that
    #    a session MAY be pinned, which is a fact about the session, and
    #    nothing about crossovers.
    #  * `crossover_envelope_v2` 4,165 -> 4,180. +15, of which 5 are comment:
    #    10 execute, and they are the payload keys the browser reads by name
    #    (`crossover` — corner, order, derived slope — plus `crossover_pinned`).
    #    The 5 comment lines are the ruling the basin pin's own entry made one
    #    round earlier, applied one axis over: a pinned number is worded
    #    "(pinned for this round)" and never as something the round measured.
    #    This file is a pure `status -> envelope` renderer, so a key it does not
    #    project is a key the browser cannot render.
    #
    # 2026-08-21 (channel-map CROSS test becomes an isolation ratio):
    # `program_analysis` 7,374 -> 7,511. +137 net (162 added, 25 removed),
    # counted MECHANICALLY off the diff rather than estimated. Of the 162
    # added: 90 comment, 7 blank, 65 non-comment — and of those 65 only 18 are
    # executable statements (net +16 after the 2 this change deletes); the
    # other 47 are DOCSTRING prose. So about nine tenths of this bump is
    # argument, which for a threshold ending in a non-retriable "open your
    # speaker" hard stop is the right ratio. Where it went:
    #   ~41  the bound's derivation — the three-level hardware table, and the
    #        BASELINE-graph discriminator that ruled out crosstalk (the finding
    #        RESTS on that; a reader who cannot see it will re-tune the number).
    #   ~46  `CHANNEL_MAP_ISOLATION_JUDGED_ABOVE_DB` and its argument — the
    #        gate's SF1, and the sharpest thing in this file's diff. The CROSS
    #        test does not sit BESIDE the TARGET floor, it RAISES it to
    #        max(FLOOR, BOUND + cross_rise), so an ungated ratio newly refused a
    #        quiet-but-correct capture (target 13.50 / cross 1.72, isolation
    #        11.78) as the NON-retriable channel_map_mismatch where main gave
    #        the retriable snr_floor. An earlier draft argued a bound <= FLOOR
    #        prevented that; it only holds at cross_rise <= 0. The comment
    #        carries the mechanism, the measured case, what the guard buys (a
    #        refusal now implies cross_rise >= FLOOR) and the named residual.
    #   ~18  `channel_map_isolation_db`, the ONE definition of the metric — the
    #        verdict and both reporting surfaces read it, so the ratio an
    #        operator sees beside a refusal is the ratio that caused it. The
    #        alternative was three subtractions, which is the second-source-of-
    #        truth this file's neighbours keep warning about.
    #   ~32  the docstrings the change falsified: `_channel_map_ok`'s CROSS
    #        bullet (which called this rung the mis-wire discriminator — it is
    #        not; seven wiring shapes moved cross rise by <=0.4 dB, so the
    #        TARGET floor is the mis-wire catcher and this half guards abnormal
    #        cross-band ENERGY), its return contract, and `PilotObservation`'s
    #        note on why the two RAW rises stay published beside the ratio.
    # No seam to cut: this is one threshold, one derived threshold, and their
    # argument — and extracting a constant's reasons from the constant is how a
    # threshold stops carrying them. The two behavioural additions went to the
    # modules that own them: the diag field to
    # `capture_dispatch._pilot_diag_fields`, the emit to the flow (+17, above).
    # The topology pins above did not move this file, so this bump stacks on
    # nothing: 7,374 is still the number it started from.
    #
    # 2026-08-21 (the anchor's witness score was measuring the wrong thing):
    # `program_analysis` 7,511 -> 7,538. +27 net (116 added, 89 removed),
    # counted MECHANICALLY off the diff. Of the 116 added: 69 comment, 2 blank,
    # 32 docstring prose, 13 executable — and 11 of those 13 REPLACE the line
    # above them, so the executable net is **+2**: the two new fields on the
    # `program_analysis.anchor` event. Everything else is one seam returning a
    # second number it was already computing and throwing away.
    #
    # What earned it: a jts3 per-driver MEASURE round failed 3/3 with
    # `drift_baselines_disagree`, having re-anchored the timeline a full pilot
    # spacing (-1309.9 ms) on a 0.0076 lead. `_locate_in_window` returned only
    # `AlignmentResult.confidence` — a PEAKEDNESS margin — and over the ~61 ms
    # of lags a per-segment window spans that margin cannot tell an empty
    # window from an occupied one: the winner's `sweep_w` window held guard
    # silence and scored 0.7386 against 0.7310 for the window holding the
    # sweep. The aligner's OTHER score, the similarity it already returns,
    # separates the same two windows 214-fold.
    #
    # No seam to cut, and the ratchet's usual remedy would make this worse:
    # `_locate_in_window`'s own docstring is the "ONE place the per-segment
    # search geometry lives", and the whole defect was a caller reading the
    # wrong one of two scores at that seam. The prose is therefore concentrated
    # THERE — which of the aligner's two numbers answers which question, with
    # the measured pair — plus the constant it forced from a difference to a
    # ratio, whose reason is a measured population gap (and an explicit note on
    # what the ratio does NOT buy) that a reader cannot re-derive and will
    # otherwise "simplify" back into a subtraction. Every
    # other site that would have restated it is a cross-reference instead, which
    # is why the two largest hunks are near-swaps rather than growth: the retired
    # constant's block is 38 removed against 34 added, and the guard block 33
    # against 36. The +27 is concentrated in `_locate_in_window` (+13) — the
    # seam that was returning the wrong number.
}


def _over_line_cap(path: Path, cap: int) -> str | None:
    """The complaint for one file over its ceiling, or ``None``."""

    count = len(path.read_text(encoding="utf-8").splitlines())
    if count <= cap:
        return None
    return f"{path.name}: {count} lines, ceiling {cap} (+{count - cap})"


def test_the_line_ratchet_reports_a_file_over_its_ceiling(tmp_path) -> None:
    """The ratchet's own positive control.

    The marker ratchets above have none, and can afford it: they count over a
    tree that always has some markers, so a broken counter reads as zero and
    trivially passes a `<=`. This one compares per file, so a helper that
    returned a too-small count — a changed reader, a file it could not open —
    would report every file comfortably under its ceiling and read exactly like
    a codebase that had stopped growing.
    """

    planted = tmp_path / "_ratchet_probe.py"
    planted.write_text("one\ntwo\nthree\n", encoding="utf-8")

    assert _over_line_cap(planted, 3) is None

    # Asserted by MARKER, not by the whole formatted string: the message is
    # diagnostic prose, and a control that breaks when someone improves the
    # wording teaches the next person to loosen the control.
    complaint = _over_line_cap(planted, 2)
    assert complaint is not None
    assert "_ratchet_probe.py" in complaint
    assert "3" in complaint and "2" in complaint


def test_the_commissioning_files_do_not_grow_without_an_extraction() -> None:
    over = [
        complaint
        for rel, cap in sorted(MAX_LINES_BY_PATH.items())
        if (complaint := _over_line_cap(REPO / rel, cap)) is not None
    ]

    assert not over, (
        "These files may not grow without something moving out of them "
        "(#2662 G2). Cut a seam and lower the ceiling, or raise it here in the "
        "same diff and say what earned the room:\n" + "\n".join(over)
    )


def _unclosed_event_loops(source: str) -> list[str]:
    """Loops in `source` created by `new_event_loop()` that nothing closes.

    Parsed rather than pattern-matched. A text scan of this rule is a trap:
    comments and docstrings naming the anti-pattern read as violations, and
    Python 3.12 splits f-strings into sub-tokens so even a token filter leaks
    prose back in. The AST sees only code.
    """

    def _is_new_loop(node: ast.AST) -> bool:
        if not isinstance(node, ast.Call):
            return False
        func = node.func
        if isinstance(func, ast.Attribute):
            return func.attr == "new_event_loop"
        return isinstance(func, ast.Name) and func.id == "new_event_loop"

    bound: set[str] = set()
    closed: set[str] = set()
    unbound = 0
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assign) and _is_new_loop(node.value):
            bound.update(
                t.id for t in node.targets if isinstance(t, ast.Name)
            )
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "close" and isinstance(node.func.value, ast.Name):
                closed.add(node.func.value.id)
            if _is_new_loop(node.func.value):
                unbound += 1

    problems = [f"`{name}` is created but never closed" for name in sorted(bound - closed)]
    if unbound:
        problems.append(
            f"{unbound} loop(s) never bound to a name "
            "(nothing can close them — use asyncio.run)"
        )
    return problems


def test_test_event_loops_are_closed_not_just_stopped() -> None:
    """A loop from `new_event_loop()` must be closed, not merely stopped.

    `loop.stop()` ends `run_forever` but releases nothing: the selector
    descriptor and the self-pipe pair stay open until the loop object happens
    to be garbage-collected. A function-scoped fixture that stops without
    closing therefore leaks 3 fds per test — invisible on a dev box (soft
    limit ~1e6), and on a CI runner (soft limit 1024) the casualty would not
    be the leaker but whichever unlucky test next tries to spawn a
    subprocess.

    Measured, not theorised: three fixtures held a monotonically climbing fd
    count until this rule landed. Whether that ever actually exhausted a CI
    runner is NOT established — the `errno=24` lines that made it look that
    way turned out to be injected by two intentional negative tests in
    `tests/test_wifi_guardian_script.py`, and the whole suite's fd high-water
    is ~43. Close loops because leaking them is wrong, not because of a
    specific incident.
    """
    offenders: list[str] = []
    for path in sorted((REPO / "tests").glob("test_*.py")):
        source = path.read_text(encoding="utf-8")
        if "new_event_loop" not in source:
            continue
        rel = path.relative_to(REPO)
        offenders += [f"{rel}: {problem}" for problem in _unclosed_event_loops(source)]

    assert not offenders, (
        "Event loops created in tests must be closed, not just stopped — "
        "stop() leaves the selector and self-pipe descriptors open until GC. "
        "Let the thread that owns the loop close it: "
        "`def _run(): try: loop.run_forever() finally: loop.close()`, the "
        "shape jasper/control/supervisor_runtime.py already uses. A close() "
        "in fixture teardown is skipped whenever teardown raises first:\n"
        + "\n".join(offenders)
    )


_LEAKY_FIXTURE = """
import asyncio


def loop_thread():
    loop = asyncio.new_event_loop()
    yield loop
    loop.stop()
"""

_CLOSED_FIXTURE = """
import asyncio


def loop_thread():
    loop = asyncio.new_event_loop()
    try:
        yield loop
    finally:
        loop.close()
"""

_UNBOUND_LOOP = """
import asyncio

run = lambda coro: asyncio.new_event_loop().run_until_complete(coro)
"""


def test_loop_guard_detects_a_stopped_but_unclosed_loop() -> None:
    """The guard must fail on what it exists to catch. Four earlier versions
    of it were text-based and were fooled by comments, then docstrings, then
    f-string sub-tokens; a fifth passed against the unfixed tree. Pin the
    catching direction so a future simplification cannot go quietly vacuous.
    """
    assert _unclosed_event_loops(_LEAKY_FIXTURE) == [
        "`loop` is created but never closed"
    ]
    assert _unclosed_event_loops(_UNBOUND_LOOP) == [
        "1 loop(s) never bound to a name "
        "(nothing can close them — use asyncio.run)"
    ]


def test_loop_guard_accepts_a_closed_loop_and_ignores_prose() -> None:
    """And it must not cry wolf — including on prose that merely names the
    anti-pattern, which is why it walks the AST rather than the text."""
    assert _unclosed_event_loops(_CLOSED_FIXTURE) == []
    prose = (
        '"""Never write asyncio.new_event_loop().run_until_complete(x)."""\n'
        "# and never leave a new_event_loop() unclosed\n"
        'msg = f"{n} unbound new_event_loop().<call> is a leak"\n'
        "x = 1\n"
    )
    assert _unclosed_event_loops(prose) == []
