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
MAX_NOQA_MARKERS = 814
MAX_BLE001_MARKERS = 617
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
    "jasper/active_speaker/crossover_v2_flow.py": 12_875,
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
    "jasper/web/correction_crossover_v2.py": 8_381,
    # Born 2026-08-18 (#2662 slice 1) at exactly this size: the relay capture
    # provider — the choreography only the phone-relay source has. It should
    # grow only when the RELAY grows; the wired provider is its own module.
    # (1,080 at birth; the gate fix round deleted a reader-less identity
    # constant, -4.) UNCHANGED by the lateral pause: `relay_link_ttl_s` moved
    # here with the extraction and its docstring quoted stage 1's old 2520 s
    # ceiling, so the pause corrects one number in place, +0.
    "jasper/web/correction_crossover_v2_relay.py": 1_076,
    # ...and 4,103 -> 4,107 (lateral pause), +4 net: the entry-baseline screen
    # said the household is "BACK on the mark", true only after a walk. With
    # the walk paused this capture follows MEASURE, where the microphone never
    # left, so the copy drops one word and the comment says why it has to read
    # correctly from either predecessor.
    "jasper/active_speaker/crossover_envelope_v2.py": 4_107,
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
    "jasper/audio_measurement/program_analysis.py": 7_275,
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
