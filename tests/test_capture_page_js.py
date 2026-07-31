# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Run the capture-page JS harnesses inside the pytest CI lane.

The static capture page (Cloudflare Pages) is JavaScript, but its security- and
contract-critical pieces are pure modules exercised by Node harnesses:

  - the fixed DATA renderer (XSS-inert: <script>/onerror=/javascript:/hostile
    component types render inert) — the plan §15 acceptance test;
  - the E2E crypto wire format (AES-256-GCM, IV-prepended, plaintext integrity);
  - the relay client request contract; and
  - the fragment parser.

Bridging them through pytest (mirroring ``tests/test_sound_setup.py``) keeps the
page covered by the existing Python CI matrix with no extra CI wiring.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from jasper.audio_measurement.calibration import SUPPORTED_MODELS

_JS_DIR = Path(__file__).resolve().parent / "js"
_NODE = shutil.which("node")
_REPO = Path(__file__).resolve().parents[1]

_HARNESSES = [
    "capture_render_test.mjs",
    "capture_crypto_test.mjs",
    "capture_relay_client_test.mjs",
    "capture_fragment_test.mjs",
    "capture_constraints_test.mjs",
    "capture_wakelock_test.mjs",
    "capture_return_url_test.mjs",
    "capture_level_events_test.mjs",
    "capture_setup_store_test.mjs",
    "capture_calibration_model_test.mjs",
    "capture_protocol_test.mjs",
    "capture_transport_integrity_test.mjs",
    "capture_host_stop_lifecycle_test.mjs",
    "capture_stop_and_ambient_countdown_test.mjs",
    "capture_ambient_stats_test.mjs",
    "capture_plan_loop_test.mjs",
    "capture_calibration_confirm_test.mjs",
    "capture_defect_fixes_test.mjs",
    "capture_time_budget_test.mjs",
]


@pytest.mark.parametrize("harness", _HARNESSES)
def test_capture_page_harness(harness: str):
    if _NODE is None:
        # A developer without node gets a skip; CI does not. Mirrors
        # ``tests/test_crossover_wizard_js.py`` verbatim, and for the same
        # reason: this lane is the ONLY place these harnesses execute (the
        # workflow's `js` job runs an explicit list that includes just one of
        # them), so "node disappeared from the runner" would silently turn the
        # whole family from covered into uncovered and still report green.
        if os.environ.get("CI"):
            pytest.fail(
                "node is not on PATH in CI — these harnesses are the only "
                "automated execution of capture-page/js/*, and skipping them "
                "would report a green build over an untested capture page"
            )
        pytest.skip("node not on PATH")
    proc = subprocess.run(
        [_NODE, str(_JS_DIR / harness)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["ok"] is True, out
    assert out["passed"] >= 1, out


def test_shared_runner_terminates_promptly_when_a_failed_test_leaks_a_handle():
    if _NODE is None:
        pytest.skip("node not on PATH")
    helper_uri = (_JS_DIR / "run_test_functions.mjs").resolve().as_uri()
    script = f"""
import {{ runTestFunctions }} from {json.dumps(helper_uri)};
await runTestFunctions(
  [function leaksHandle() {{
    setInterval(() => {{}}, 10_000);
    throw new Error("expected failure");
  }}],
  () => 0,
);
"""

    proc = subprocess.run(
        [_NODE, "--input-type=module", "--eval", script],
        capture_output=True,
        text=True,
        timeout=2,
    )

    assert proc.returncode == 1
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["ok"] is False
    assert out["test"] == "leaksHandle"


def test_capture_page_expired_link_message_points_back_to_speaker():
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert 'message === "not_found"' in main_js
    assert "This one-time capture link has expired." in main_js
    assert "Return to the speaker page" in main_js


def test_capture_page_distinguishes_invalid_link_from_network_failure():
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert "function relayBootFailureMessage(err)" in main_js
    assert "[401, 403, 404].includes(status)" in main_js
    assert 'message.includes("capture spec integrity")' in main_js
    assert "This authenticated measurement link is invalid" in main_js
    assert "Can't reach the measurement relay" in main_js
    assert "setStatus(relayBootFailureMessage(err), \"error\")" in main_js


def test_capture_page_version_contract_is_published_and_cache_busted():
    version = json.loads((_REPO / "capture-page/version.json").read_text())
    index_html = (_REPO / "capture-page/index.html").read_text(encoding="utf-8")
    build_sh = (_REPO / "capture-page/build.sh").read_text(encoding="utf-8")

    assert version == {
        "schema_version": 1,
        "capture_protocol_version": 3,
        # ONE protocol. The supported list is not a negotiation surface any
        # more — protocols 1 and 2 were deleted (the flow has never shipped
        # outside the lab and no lab Pi emits them), so a page advertising
        # anything else is stale. NOTE this is a REMOVAL: the currently
        # deployed page still advertises [1, 2, 3], so this page build must
        # publish AFTER the Pis stop emitting 1 and 2, not before.
        "supported_capture_protocol_versions": [3],
        "capture_page_build": "20260731.2",
    }
    # The ?v= query is the page's ONLY cache-invalidation mechanism, and the
    # Pi's build gate checks the stamp's FORMAT, not its value — so a phone
    # holding the previous bundle would be accepted silently. Bumping
    # version.json without bumping this is therefore a shipping hazard, not a
    # cosmetic mismatch: that is what this pairing exists to catch, and what it
    # caught for the flat-linearization PR-3b page fix.
    assert "main.js?v=20260731-2" in index_html
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")
    assert 'from "./render.js?v=20260711-1"' in main_js
    assert 'from "./measurement-audio.js?v=20260711-4"' in main_js
    # Bumped with #1941 R4: constraints.js's realized-constraint describe()
    # feeds household copy, so a warm-cache browser holding the old module
    # would keep attributing the browser's own track settings to the
    # microphone — the exact misattribution this sweep removed.
    assert 'from "./constraints.js?v=20260731-1"' in main_js
    # Bumped with #1824 B1: relay-client.js gained the machine-readable
    # timeout tag the page classifies on. A warm-cache phone holding the old
    # module would keep raising untagged timeouts, so the classifier would stay
    # broken for exactly the phones already in a household's hands.
    assert 'from "./relay-client.js?v=20260728-1"' in main_js
    # Both modules changed in the protocol-deletion PR, and both carry a
    # SECURITY tightening (mandatory spec MAC; a version-less spec is refused
    # rather than read as legacy protocol 1). An unstamped or stale-stamped
    # import means a warm-cache phone keeps the permissive module — the
    # tightening silently would not take effect. capture-protocol.js had no
    # stamp at all before this PR.
    assert 'from "./capture-protocol.js?v=20260727-1"' in main_js
    assert 'from "./transport-integrity.js?v=20260727-1"' in main_js
    assert 'from "./level-events.js?v=20260716-1"' in main_js
    assert 'from "./ambient-stats.js?v=20260717-1"' in main_js
    assert 'cp "${HERE}/version.json" "${DIST}/version.json"' in build_sh


def test_capture_page_existing_field_rollout_order_is_pinned():
    """A protocol handshake cannot see a semantic change where a new page
    consumes a field old pages ignored. DA-0005 is exactly that shape:
    20260729.1 consumes Room ui.screen copy. The Pi producer must land first,
    and rollback must restore the tolerant page first."""
    readme = (_REPO / "capture-page/README.md").read_text(encoding="utf-8")

    assert "Reinterpreting an existing spec field (Pi first)" in readme
    assert "**Forward rollout → Pi first, page second.**" in readme
    assert "**Rollback → page first, Pi second.**" in readme
    assert "build `20260729.1` starts\nrendering Room" in readme


def test_capture_page_new_phone_event_rollout_order_is_pinned():
    """The sharper class the two-stage split introduced: the page starts
    SENDING something the Pi requires, on a plan shape only the new Pi emits.
    Neither the protocol list nor the build stamp detects it, so the ordering
    (page first) and the tolerance requirement it rests on are documented and
    pinned — and the tolerance itself is a branch in the page, not a hope
    about timing."""
    readme = (_REPO / "capture-page/README.md").read_text(encoding="utf-8")
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert "A new phone event both sides need (page first)" in readme
    assert "**Page first, Pi second**" in readme
    assert "build `20260729.2` is the fixture" in readme.lower()
    # The branch the ordering rests on: an entry past the group means the
    # confirmation still rides that next begin (an older conductor's plan).
    assert "if (entryForIndex(ctx.spec, index + 1)) {" in main_js
    assert "complete_capture_set: true" in main_js


def test_capture_page_beep_copy_matches_the_composed_beep_count():
    """#1824 N3: the prelude line says "Listen for three beeps", which mirrors
    the composer's COURTESY_TONE_BEEP_COUNT. Spelled out rather than sent over
    the wire — a household counts beeps, it does not parse a field — so this is
    what stops the two from drifting into a page that miscounts the sound the
    speaker actually makes."""
    from jasper.audio_measurement.program import COURTESY_TONE_BEEP_COUNT

    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert COURTESY_TONE_BEEP_COUNT == 3, (
        "the composed beep count moved — update the capture page's prelude copy "
        "(main.js, 'Listen for three beeps') and the guided consent screen's "
        "orientation step (capture_relay/spec.py, 'three short beeps') to "
        "match, then this pin"
    )
    assert "Listen for three beeps" in main_js
    # The ORIENTATION screen says it too, before the first tone (work order D7
    # / issue #1804): an unexplained burst of beeps at measurement level is the
    # moment a first-time household stops the session. Same spelled-out
    # convention as the page's line, and pinned in the same place so the two
    # cannot drift apart from the composer or from each other.
    spec_py = (_REPO / "jasper/capture_relay/spec.py").read_text(encoding="utf-8")
    assert "three short beeps" in spec_py


def test_capture_page_classifies_relay_timeouts_by_tag_not_by_message():
    """#1824 B1. `_controlFetch` aborts with a NAMED reason (the run-19 fix), so
    per the AbortController spec fetch rejects with that value — an ordinary
    Error whose name is not "AbortError" and whose message says nothing about
    aborting. Classifying on either of those therefore stopped matching real
    timeouts, silently, and every connectivity branch on the page became
    unreachable in production. The tag is the contract; these pins keep the
    two halves of it wired together."""
    relay_js = (_REPO / "capture-page/js/relay-client.js").read_text(encoding="utf-8")
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    # Producer: the abort reason is the tagged class, not a bare Error.
    assert "export class RelayTimeoutError extends Error" in relay_js
    assert "this.relayTimeout = true;" in relay_js
    assert "controller.abort(\n        new RelayTimeoutError(" in relay_js
    # Consumer: the classifier keys on the tag FIRST; the name/text checks
    # remain only as the bare-abort fallback.
    assert "err.relayTimeout === true" in main_js


def test_capture_page_step_screens_render_one_instruction_grammar():
    """Flow-simplification §2.1: every page-owned plan screen renders the SAME
    grammar in the SAME DOM slots — the counter as a small eyebrow, the
    instruction as the headline, one supporting clause, a single full-width
    primary, and Stop demoted to a text link. The behavior is exercised in
    capture_plan_loop_test.mjs; these pins keep the wiring (and the styles the
    grammar depends on) from silently regressing."""
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")
    index_html = (_REPO / "capture-page/index.html").read_text(encoding="utf-8")

    assert "function renderStepScreen(ctx, {" in main_js
    # The counter is server-derived and rendered once, in the eyebrow.
    assert 'el("p", { class: "cap-eyebrow", text: String(progress) })' in main_js
    assert 'String(screenCopy.progress || "")' in main_js
    assert ".cap-eyebrow {" in index_html
    # The one primary label: the tap IS the placement confirmation.
    assert 'const STEP_PRIMARY_LABEL = "I’m there — play the tone";' in main_js
    # …and the D8 budget line rides UNDER the action, in its own quieter slot,
    # so "how long can I pause?" never competes with the instruction for the
    # headline/detail slots the grammar reserves.
    assert 'el("p", { class: "cap-note cap-budget", text: budget })' in main_js
    assert ".cap-budget {" in index_html


def test_capture_page_stop_is_a_text_link_behind_a_danger_confirm():
    """§2.1 reverses render.js's documented "Stop is the one danger button"
    styling for the PAGE-OWNED step screens only: Stop appears on all 16 of a
    full session's screens, and at equal weight with the primary a stray tap
    could abandon the session outright. The destructiveness moves to a
    page-local <dialog> (the capture page shares nothing with the Pi's
    dialog.js — different origin, different bundle, strict CSP), which the
    browser cannot suppress the way it can window.confirm()."""
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")
    index_html = (_REPO / "capture-page/index.html").read_text(encoding="utf-8")

    assert "function stopLinkEl() {" in main_js
    assert 'class: "cap-stop-link"' in main_js
    assert "function confirmStopMeasuring() {" in main_js
    assert "if (await confirmStopMeasuring()) await stopCapture();" in main_js
    assert '<dialog id="stop-confirm" class="cap-dialog">' in index_html
    assert 'id="stop-confirm-accept"' in index_html
    assert 'id="stop-confirm-cancel"' in index_html
    # Native popups stay out (they can be suppressed); no inline handlers
    # either — the CSP admits no inline script.
    assert "window.confirm(" not in main_js
    assert "onclick=" not in index_html
    # The page never renders relay-supplied text inside the dialog: its copy
    # is static markup.
    assert "Stop measuring?" in index_html


def test_capture_page_status_line_stops_counting_the_walk():
    """§2.1: `#status` used to number the same walk a second time, in its own
    vocabulary, and disagree with the screen's counter. It is the transient
    STATE channel now — the removed counter strings must not come back."""
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")
    index_html = (_REPO / "capture-page/index.html").read_text(encoding="utf-8")

    assert "function clearStatus() {" in main_js
    assert "Measurement ${index} of ${target} done. Tap Next measurement" not in main_js
    assert "Requesting measurement ${index} of ${target}" not in main_js
    assert "Next measurement starts in ${seconds}s" not in main_js
    assert '"Asking the speaker to start…"' in main_js
    # Transient state survives — those lines are the channel's whole job.
    assert '"Speaker is checking this measurement…"' in main_js
    assert "#status:empty {" in index_html


def test_capture_page_retake_offer_never_outlives_the_runners_window():
    """§2.6 + review finding N4. jasper/capture_relay/session.py's
    `_poll_capture_plan` admits a retake ONLY for the just-accepted index, on
    the next attempt, carrying the marker, and ONLY while the next entry's
    begin has not been seen (its `next_begin_seen`, which flips on an admitted
    OR merely deferred begin — including the VERIFY hold's auto-posted one).
    Past that the begin is refused as out-of-order, and ANY refusal ends the
    session, so an offer that outlived the window would be a button whose only
    outcome is killing the run. Behavior is exercised end-to-end against a fake
    relay that enforces the runner's ordering (capture_plan_loop_test.mjs);
    these pins keep the three mechanisms in place."""
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    # 1. Every begin this page posts shuts the window…
    start = main_js.index("async function runPlanCapture(ctx, { index, attempt, retake = false }) {")
    end = main_js.index("async function onPlanStart(ctx)", start)
    run_body = main_js[start:end]
    assert "shutRetakeWindow(ctx);" in run_body
    # …and shutting it disables the control that offered it, rather than
    # leaving a live-looking button that does nothing (review M1).
    assert "ctx.retakeButtonEl.disabled = true;" in main_js
    # …and only an accepted verdict re-arms it, within the plan's own budget.
    assert "armRetakeSlot(ctx, { index, attempt });" in run_body
    assert "planSupportsRetake(ctx.spec) && attempt + 1 <= maxAttempts" in main_js
    # 2. The tap re-checks (a countdown's auto-begin can win the race).
    assert "function canRetake(ctx, index) {" in main_js
    assert main_js.count("if (!canRetake(ctx, index)) return") >= 2
    # 3. The marker is a distinct wire shape, and a rejected retake keeps it.
    assert "function beginCapturePayload({ index, attempt, retake = false }) {" in main_js
    assert "return retake ? { index, attempt, retake: true } : { index, attempt };" in main_js
    assert "await runPlanCapture(ctx, { index, attempt: attempt + 1, retake });" in main_js


def test_capture_page_rejected_retake_can_keep_the_earlier_take():
    """Review blocker B1. A voluntary retake re-measures an ALREADY-ACCEPTED
    slot, and the design's fail-safe is that a rejected one leaves the original
    take standing — so the retry screen cannot offer only "Try again", or the
    household re-measures something that does not need it and can burn the
    attempt budget until the session dies with the fit and apply never fired.
    The forward begin is legal at that point (a rejected retake leaves
    `accepted_count` unchanged, `attempts_used` at this attempt, and
    `next_begin_seen` false), which is what makes the escape safe rather than
    merely kind. Exercised end-to-end in capture_plan_loop_test.mjs against a
    fake relay that enforces the runner's ordering."""
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert "function keepEarlierTakeControl(ctx, { index, attempt, target }) {" in main_js
    assert 'button("Keep the earlier measurement and continue"' in main_js
    # Offered only on a RETAKE's rejection — an ordinary failure retry has no
    # earlier take to keep.
    assert (
        "secondary: retake ? keepEarlierTakeControl(ctx, { index, attempt, target }) : null,"
        in main_js
    )
    # It returns to the screen the acceptance belonged on: the group-close
    # confirm keeps the fit behind the same tap it was behind. Since the
    # two-stage split (work order D1) the awaiting-confirm branch is FIRST in
    # the completion ladder rather than a nested else, so the flag is set from
    # inside it — the ordering itself is pinned by
    # tests/js/capture_plan_loop_test.mjs's
    # testTheFinalHeldCaptureRendersTheConfirmNotAllDone.
    assert "if (verdict.accepted && verdict.awaitingConfirm) {" in main_js
    assert "ctx.retakeAwaitingConfirm = true;" in main_js
    assert "if (ctx.retakeAwaitingConfirm) {" in main_js


def test_capture_page_pre_arm_failure_never_strands_a_fatal_affordance():
    """Review S2. A pre-arm failure leaves the previous screen up, which is
    only safe when its live control re-posts a pair the Pi still accepts. Two
    cases where it does not: during a RETAKE the visible primary is the forward
    path (posting it while the Pi sits in awaiting_arm on the retake is
    `begin_out_of_order` — fatal), and a countdown screen has no begin
    affordance at all (the copy would name a button that does not exist)."""
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert "function repairPreArmAffordance(ctx, { index, attempt, target, retake }) {" in main_js
    # The retake repair re-arms the offer whose closure re-posts the IDENTICAL
    # pair, and names that control instead of the forward primary.
    assert "const restore = { index, attempt: attempt - 1, target };" in main_js
    assert "armRetakeSlot(ctx, restore);" in main_js
    assert "return RETAKE_LABEL;" in main_js
    # …AND PUTS IT BACK ON SCREEN (gate blocker B1). Re-arming alone sufficed
    # only while a retake left the OFFERING screen up; since the retake round
    # renders its own affordance-free in-progress screen, this arm has to
    # re-render one that carries the control the copy names — the group-close
    # confirm when the retake was of the final position, the manual next screen
    # otherwise. Behaviour is pinned in capture_plan_loop_test.mjs against
    # ON-SCREEN labels; these keep the wiring visible here.
    assert "renderPlanGroupConfirm(ctx, restore);" in main_js
    assert "renderPlanNext(ctx, restore);" in main_js
    # …routed by the SAME flag keepEarlierTakeControl already returns on, so a
    # retake of the cloud's final position lands back on the confirm rather
    # than on a next-measurement screen the held set has no next entry for.
    assert main_js.count("if (ctx.retakeAwaitingConfirm) {") == 2
    # The no-affordance repair drops back to the manual screen the countdown's
    # own Cancel produces, which has one.
    assert "if (!hasBegin && index > 1) {" in main_js
    assert "renderPlanNext(ctx, { index: index - 1, attempt: attempt - 1, target });" in main_js


def test_capture_page_verify_confirms_after_the_hold_before_the_tone():
    """§2.2, the step-11 fix. VERIFY is BEGIN-FIRST, THEN CONFIRM: the begin
    posts immediately (each deferred re-post re-arms the host's hold clock —
    sitting tap-first in `awaiting_begin` would hit REVIEW_HOLD_BUDGET_S and
    kill the session as a relay timeout), the hold screen instructs the walk
    back, and the tone waits for the household's tap once authorization lands.
    The confirmation copy rides NEW screen keys, which is what keeps a cached
    pre-redesign bundle rendering today's exact flow."""
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert "function entryConfirmsBeforeArming(spec, index) {" in main_js
    assert "return Boolean(screenCopy.confirm_title);" in main_js
    assert "String(screenCopy.confirm_body || \"\")" in main_js
    assert "await awaitPlanConfirmation(ctx, { index });" in main_js
    # The gate sits AFTER admission and BEFORE the mic/ambient/armed leg.
    run_start = main_js.index("async function runPlanCapture(ctx, { index, attempt, retake = false }) {")
    gate = main_js.index("await awaitPlanConfirmation(ctx, { index });", run_start)
    assert main_js.index("const admission = await beginAndAwaitAuthorization(", run_start) < gate
    assert gate < main_js.index("armedPosted = true;", run_start)
    # A parked confirmation is released by Stop / teardown rather than left
    # suspended on a promise nothing resolves.
    assert "function resolvePendingConfirm(ctx) {" in main_js
    abort_start = main_js.index("function makePlanController(ctx) {")
    abort_end = main_js.index("async function endPlanSession(ctx)", abort_start)
    assert "resolvePendingConfirm(ctx);" in main_js[abort_start:abort_end]
    release_start = main_js.index("async function releasePlanSessionResources(ctx) {")
    release_end = main_js.index("async function reacquireSessionWakeLock", release_start)
    assert "resolvePendingConfirm(ctx);" in main_js[release_start:release_end]


def test_capture_page_consent_announces_the_plan_before_the_first_tone():
    """§2.3: the consent screen IS the announcement — how many measurements
    and how long, DERIVED from the signed plan (never hardcoded), above the
    placement instruction. The page derives it rather than only rendering the
    speaker's own consent line because the page ships FIRST (README "Release
    order"): against a speaker that predates the tier line this is the whole
    announcement. tests/test_capture_relay_spec.py pins the per-capture
    allowance across the two derivations."""
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")
    index_html = (_REPO / "capture-page/index.html").read_text(encoding="utf-8")

    assert "function planEstimatedMinutes(spec) {" in main_js
    assert "function planAnnouncementText(spec) {" in main_js
    assert 'const sentence = `${target} measurements, about ${minutes} minutes`;' in main_js
    # …and stands down when the speaker's own consent copy already carries that
    # exact derived sentence, so the household never reads it twice.
    assert "return specScreenSays(spec, sentence) ? \"\" : `${sentence}.`;" in main_js
    assert "insertPlanAnnouncement(screenEl, spec);" in main_js
    assert ".cap-announce {" in index_html
    # One derivation, shared with the wake-lock hint — not a second estimate.
    hint_start = main_js.index("function wakeLockHintText(spec) {")
    hint_end = main_js.index("function planAnnouncementText", hint_start)
    assert "planEstimatedMinutes(spec)" in main_js[hint_start:hint_end]
    # The mic picker collapses once the session's one mic stream exists — from
    # then on it cannot change anything.
    assert "function collapseMicPicker() {" in main_js
    assert "collapseMicPicker();" in main_js


def test_capture_page_announcement_matches_the_speakers_own_consent_line():
    """The de-dup in `planAnnouncementText` is a CROSS-BOUNDARY claim: the page
    stands its announcement down only because the speaker's consent copy
    already carries the identical derived sentence. Nothing else would notice
    if a server-side reword broke that match — the page would silently render
    both, and the household would read one sentence twice, two lines apart
    (which is exactly what a browser pass caught during PR-U2).

    So build a REAL guided consent screen with the REAL server builder, render
    the PAGE's announcement template against the same plan, and assert the
    speaker's copy contains it."""
    import re

    from jasper.capture_relay.spec import (
        CapturePlan,
        CapturePlanEntry,
        build_crossover_sweep_spec,
    )

    plan = CapturePlan(
        capture_target=7,
        max_attempts=14,
        schema_version=2,
        entries=tuple(
            CapturePlanEntry(index=i, kind_label="cloud_measure", duration_ms=ms)
            for i, ms in enumerate((23000, 41000, 16000, 16000, 16000, 16000, 16000))
        ),
    )
    spec = build_crossover_sweep_spec(
        driver_label="crossover",
        driver_role="summed",
        acknowledgement_binding="placement_abcdefghijklmnopqrstuv",
        capture_plan=plan,
        guided_captures=plan.capture_target,
        guided_tier="express",
    )
    steps = next(c for c in spec.screen if c["type"] == "steps")["items"]

    # The page's own template, read out of its source rather than restated.
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")
    template = re.search(
        r"const sentence = `([^`]+)`;", main_js
    )
    assert template is not None, "the capture page no longer derives an announcement"
    rendered = (
        template.group(1)
        .replace("${target}", str(plan.capture_target))
        .replace("${minutes}", str(plan.estimated_minutes()))
    )
    assert any(rendered in step for step in steps), (
        f"the speaker's consent copy no longer contains the page's announcement "
        f"({rendered!r}); the page will now render it a second time — either "
        f"restore the wording or revisit capture-page/js/main.js's de-dup"
    )


def test_capture_page_treats_host_stop_as_expected_control_flow():
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert 'phase === "sweep_cancelled"' in main_js
    assert "Measurement stopped safely. The speaker page shows what happens next." in main_js
    assert "if (sweepCompleted === false) return;" in main_js


def test_capture_page_csp_allows_version_handshake_and_relay():
    """The compatibility handshake is same-origin; relay traffic is not."""
    index_html = (_REPO / "capture-page/index.html").read_text(encoding="utf-8")
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert 'connect-src \'self\' https://relay.jasper.tech' in index_html
    assert 'new URL("../version.json", import.meta.url)' in main_js


def test_capture_page_completion_renders_return_cta():
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")
    index_html = (_REPO / "capture-page/index.html").read_text(encoding="utf-8")

    assert "safeReturnUrl" in main_js
    assert "Back to speaker" in main_js
    assert "renderCaptureComplete(ctx)" in main_js
    assert "display: inline-flex;" in index_html


def test_capture_page_waits_for_pi_sweep_completion():
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert 'phase === "ambient_started"' in main_js
    assert "Measuring room noise — stay quiet and keep the microphone still." in main_js
    assert "fetchPhoneStatus" in main_js
    assert 'phase === "sweep_complete"' in main_js
    assert "recordWindowMs" not in main_js


def test_capture_page_serial_models_match_pi_registry_keys():
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert "spec.calibration_models" in main_js
    for key in SUPPORTED_MODELS:
        assert f'value: "{key}"' not in main_js
        assert f'value: \'{key}\'' not in main_js
    for stale in (
        "minidsp_umik_1",
        "minidsp_umik_2",
        "dayton_imm_6c",
        "dayton_umm_6",
    ):
        assert stale not in main_js


def test_capture_page_upload_never_declares_a_sign_convention():
    """The phone's calibration upload posts exactly {mode, filename, content}.

    Because it declares no sign convention, the Pi does not read one from a
    relay setup: `_relay_calibration_from_setup` in
    ``jasper/web/correction_setup.py`` states the ecosystem convention
    outright (`DEFAULT_SIGN_CONVENTION`) instead of defaulting a key nobody
    sends. That pairing is the invariant this test guards — the day the page
    grows a sign control, this fails, and whoever adds it must wire the Pi
    side rather than have the phone's declaration silently ignored (the
    version-skew failure that would put a household's measurements back on
    the wrong sign with no signal).
    """
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    offenders = [
        f"capture-page/js/main.js:{n}: {line.strip()}"
        for n, line in enumerate(main_js.splitlines(), start=1)
        if "sign_convention" in line
    ]
    assert not offenders, (
        "the capture page now declares a sign convention; wire it through "
        "_relay_calibration_from_setup in jasper/web/correction_setup.py in "
        "the same change, or the household's declaration is silently ignored:"
        + "\n".join(["", *offenders])
    )


def test_capture_page_preflights_guided_setup_before_start():
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert "validateSetupBeforeContinue(ctx)" in main_js
    assert "setup_validate: true" in main_js
    assert "setup_token" in main_js
    assert 'event.phase === "setup_validation_failed"' in main_js
    assert 'event.phase === "setup_validated"' in main_js
    assert "renderPositionCount(screenEl, ctx)" in main_js


def test_capture_page_level_ramp_uses_meter_protocol_without_wav_upload():
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert (
        'import { runLevelRampProtocol } from "./level-events.js?v=20260716-1"'
        in main_js
    )
    assert 'spec.kind === "level_ramp"' in main_js
    assert "onLevelRampStart(ctx)" in main_js

    start = main_js.index("async function onLevelRampStart")
    end = main_js.index("async function waitForSweepComplete", start)
    level_path = main_js[start:end]
    assert "runLevelRampProtocol" in level_path
    assert "float32ToWavBlob" not in level_path
    assert "encryptWav" not in level_path
    assert "putBlob" not in level_path


def test_capture_page_compares_spec_to_normalized_mono_capture_width():
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")
    constraints_js = (_REPO / "capture-page/js/constraints.js").read_text(
        encoding="utf-8",
    )
    measurement_js = (
        _REPO / "deploy/assets/shared/js/measurement-audio.js"
    ).read_text(encoding="utf-8")

    assert "capturedChannelCount: 1" in measurement_js
    assert "var ch=inp[0]&&inp[0][0]" in measurement_js
    assert "recorder.capturedChannelCount" in main_js
    assert "source_channel_count: realized.sourceChannelCount" in main_js
    assert "captured_channel_count: realized.capturedChannelCount" in main_js
    assert "capturedChannelCount = null" in constraints_js
    assert "checkedChannelCount === wantChannels" in constraints_js


def test_capture_page_level_ramp_uses_guided_mic_calibration_setup():
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert 'spec.kind === "room_sweep" || spec.kind === "level_ramp"' in main_js
    assert 'ctx.spec.kind === "level_ramp"' in main_js
    assert "renderMicChoice(screenEl, ctx, inputs)" in main_js
    assert "renderCalibration(screenEl, ctx)" in main_js
    assert "renderLevelReady(screenEl, ctx)" in main_js
    level_ready_start = main_js.index("function renderLevelReady")
    level_ready_end = main_js.index("function renderRoomReady", level_ready_start)
    level_ready_path = main_js[level_ready_start:level_ready_end]
    assert "renderScreen(screenEl, ctx.spec" in level_ready_path
    assert "onLevelRampStart(ctx)" in level_ready_path
    assert "Place the microphone as shown" not in level_ready_path

    start = main_js.index("async function onLevelRampStart")
    end = main_js.index("async function waitForSweepComplete", start)
    level_path = main_js[start:end]
    assert "setup: setupWirePayload()" in level_path
    assert "device: capture.device" in level_path


def test_capture_page_supports_bound_and_pi_owned_capture_only_setup():
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")
    level_js = (_REPO / "capture-page/js/level-events.js").read_text(
        encoding="utf-8",
    )

    setup_store_js = (_REPO / "capture-page/js/setup-store.js").read_text(
        encoding="utf-8",
    )

    assert 'SETUP_STORAGE_KEY = "jts.capture.bound-setup.v2"' in setup_store_js
    assert "SETUP_IDLE_TTL_MS" in setup_store_js
    assert "SETUP_ABSOLUTE_TTL_MS" in setup_store_js
    assert "refreshBoundSetup(spec)" in main_js
    assert "setup_binding_id" in setup_store_js
    assert "setup_collect_positions" in main_js
    assert 'spec.kind === "room_sweep" && spec.setup_validation === false' in main_js
    assert "if (setupCaptureOnly)" in main_js
    assert "renderRoomReady(screenEl, ctx)" in main_js
    assert "setup_identity: identity" in main_js
    assert "persistBoundSetup(ctx.spec, identity)" in main_js
    assert "setup: setupWirePayload()" in main_js
    assert "Raw serials/calibration text are forbidden" in level_js
    assert "validated compact setup binding" in level_js


def test_capture_page_names_the_signed_room_trust_repeat():
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")
    spec_py = (_REPO / "jasper/capture_relay/spec.py").read_text(encoding="utf-8")

    assert 'ctx.spec.presentation_variant === "trust_repeat"' not in main_js
    assert "Ready to repeat the main seat" not in main_js
    assert 'if presentation_variant == "trust_repeat":' in spec_py
    assert "Ready to repeat the main seat" in spec_py
    assert "This extra capture checks that the result" in spec_py


def test_capture_page_rejects_oversize_calibration_and_unproven_agc():
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert "MAX_CALIBRATION_TEXT_BYTES" in main_js
    assert "file.size" in main_js
    assert "utf8Size(content)" in main_js
    assert "smaller than 256 KiB" in main_js
    assert 'reason: "agc_not_proven_off"' in main_js
    assert "JTS will not play the level tone" in main_js


def test_capture_page_level_ramp_agc_gate_only_refuses_explicit_on():
    """iOS/WebKit never reports autoGainControl (getSettings() omits the key),
    so gating on `!== false` refused every iPhone. Only an explicit `true`
    (the browser affirmatively reports AGC on) refuses now; undefined/null
    proceeds as unattested and is empirically verified server-side from the
    ramp's own staircase (jasper/audio_measurement/ramp.py) instead."""
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    start = main_js.index("async function onLevelRampStart")
    end = main_js.index("async function waitForSweepComplete", start)
    level_path = main_js[start:end]

    assert "capture.settings.autoGainControl !== false" not in level_path
    assert "const realizedAgc = capture.settings.autoGainControl;" in level_path
    assert "if (realizedAgc === true) {" in level_path
    assert "const agcAttested = realizedAgc === false;" in level_path
    assert "agcFrozen: agcAttested," in level_path
    assert "agcUnattested: !agcAttested," in level_path
    # The explicit-on refusal copy is unchanged — only the gate condition
    # narrowed from "not proven false" to "proven true".
    assert (
        "This browser cannot prove automatic microphone gain is off, so JTS "
        "will not play the level tone." in level_path
    )


def test_capture_page_level_ramp_shows_friendly_agc_suspected_copy():
    """The Pi's empirical slope-verification failure (agc_suspected) gets a
    phone-facing explanation instead of the raw server error code — and the
    INDETERMINATE outcome (agc_indeterminate: insufficient evidence, no AGC
    observed) gets its own honest copy that does not claim AGC was seen."""
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    start = main_js.index("function renderLevelRampComplete")
    end = main_js.index("async function enumerateAudioInputs", start)
    ramp_complete = main_js[start:end]

    assert 'terminalError === "agc_suspected"' in ramp_complete
    # "browser or device", not "browser" alone (#1941 delta-gate NIT-A): on iOS
    # every browser is WebKit, so browser-switching is not a remedy there and
    # the copy would otherwise leave an iPhone household with nothing to try.
    # The sibling agc_indeterminate line below already named the device — these
    # two now agree.
    assert (
        "This browser is adjusting the microphone level, which prevents "
        "accurate measurement. Try a different browser or device, or a USB "
        "measurement microphone." in ramp_complete
    )
    assert 'terminalError === "agc_indeterminate"' in ramp_complete
    assert (
        "JTS couldn't gather enough measurement evidence to verify this "
        "microphone's level accuracy. Try again, or use a different "
        "microphone or device." in ramp_complete
    )


def test_capture_page_infers_calibration_from_pi_registry_without_serial():
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert "inferCalibrationModel(" in main_js
    assert "calibrationModels," in main_js
    assert 'mode: "serial"' in main_js
    assert "model: inferred.key" in main_js
    assert "umik-2" not in main_js.lower()
    assert "minidsp_umik2" not in main_js
    assert 'serial: ""' in main_js
    assert "if (!setupState.calibration.serial)" in main_js
    assert "Enter the microphone serial number." in main_js
    assert "sessionStorage" not in main_js


def test_capture_page_level_completion_does_not_promise_wrong_next_step():
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert "ready for the measurement sweep" not in main_js
    assert "Level matched. The speaker continues on its own." in main_js


def test_capture_page_terminal_screens_describe_outcome_not_command_return():
    """Owner-directed reframe: terminal screens describe what happens next —
    the household never needs to physically return to the speaker, since the
    wizard auto-advances on its own. Pins the PHONE-1/XOVER-6 copy."""
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    start = main_js.index("function renderLevelRampComplete")
    end = main_js.index("async function enumerateAudioInputs", start)
    ramp_complete = main_js[start:end]
    assert "Return to the speaker" not in ramp_complete
    assert (
        "Level matched. The speaker will continue on its own — "
        "you can put the microphone down." in ramp_complete
    )
    assert "The speaker page shows what happens next." in ramp_complete

    assert (
        "Measurement uploaded. The speaker will continue automatically."
        in main_js
    )
    assert "You can close this tab." in main_js


def test_capture_page_sweep_failed_renders_terminal_screen_not_dead_start():
    """XOVER-6 interim: sweep_failed used to leave the Start-button screen
    visible with a retry that replays a stale spec/run_token. It must now
    render a terminal outcome screen instead, like ramp failures do."""
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert "function renderSweepFailed(ctx, err)" in main_js
    assert "failure.sweepFailed = true" in main_js
    assert "if (err && err.sweepFailed) {" in main_js
    assert "renderSweepFailed(ctx, err);" in main_js

    start = main_js.index("function renderSweepFailed")
    end = main_js.index("async function enumerateAudioInputs", start)
    sweep_failed_path = main_js[start:end]
    assert "Tap Start to try again" not in sweep_failed_path
    assert "The speaker page shows what happens next." in sweep_failed_path


def test_capture_page_no_return_link_falls_back_to_close_tab_copy():
    """PHONE-2: when safeReturnUrl() is empty, the terminal screens that
    otherwise render a Back-to-speaker button must not silently drop the CTA
    with no replacement copy. 3 pre-existing call sites (capture complete,
    ramp complete, bound-setup-expired), the XOVER-6 sweep_failed screen, the
    phone-initiated Stop terminal screen (renderStoppedScreen), the run-19
    dead-session terminal (renderSessionExpired), and the three new v3
    session-plan terminals (renderPlanAllDone, renderPlanRefused,
    renderPlanExhausted) all need the same fallback."""
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert main_js.count('linkButton("Back to speaker", returnUrl)') == 9
    assert main_js.count('text: "You can close this tab."') == 9


def test_capture_page_setup_continue_and_fragment_errors_use_friendly_helper():
    """PHONE-3: the calibration-continue, position-count-continue, and
    fragment-parse error paths used to surface raw exception text with their
    own ad hoc ternary instead of the shared captureFailureMessage() helper."""
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert (
        'setStatus(err && err.message ? String(err.message) : String(err), "error")'
        not in main_js
    )

    start = main_js.index("handle = parseFragment(")
    end = main_js.index("client = new RelayClient(", start)
    boot_fragment_path = main_js[start:end]
    assert "setStatus(captureFailureMessage(err), \"error\");" in boot_fragment_path


def test_capture_page_names_the_device_instead_of_ambiguous_this_page():
    """Item 6: backgrounded-abort copy said 'stay on this page', ambiguous
    about which device. Name the phone explicitly."""
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert "must stay on this page" not in main_js
    assert "this screen must stay on" in main_js


def test_crossover_candidate_review_collapses_provenance_hashes():
    """PHONE-4: renderCandidateReview() lives in the Pi-served /correction/
    crossover wizard (deploy/assets/correction/js/crossover/main.js), not
    capture-page/ — the reviewer's cited surface is what actually renders the
    candidate to the household. The raw fingerprint + alignment confidence move
    behind a collapsed <details> disclosure; the plain-language trims / delay /
    polarity rows stay primary (W6.10 blocker #2 reworked the shape)."""
    crossover_js = (
        _REPO / "deploy/assets/correction/js/crossover/main.js"
    ).read_text(encoding="utf-8")

    assert "el('details', {class: 'candidate-provenance'}" in crossover_js
    assert "el('summary', {text: 'Technical details'})" in crossover_js
    # The raw candidate fingerprint is provenance, behind the disclosure.
    assert "review.fingerprint" in crossover_js


# ---------------------------------------------------------------------------
# Wave 2 (SPEC W2.3 session-spanning relay + W2.1 ambient stats + W2.2
# one-tap mic confirm — the no-ping-pong batch)
# ---------------------------------------------------------------------------


def test_capture_page_plan_routes_begin_capture_to_the_plan_loop():
    """A `capture_plan` — and ONLY a `capture_plan` — wires the spec-rendered
    Start button to onPlanStart(); a plan-free spec keeps the single-capture
    onStart(). The protocol number is deliberately NOT part of this test: with
    one protocol, every spec carries the same version, so a
    `capture_protocol_version === 3` conjunct would be dead weight that reads
    as if plan-ness were still version-encoded."""
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert "const isPlanSpec = Boolean(spec.capture_plan);" in main_js
    assert "capture_protocol_version === 3" not in main_js
    # Phone-event signing is unconditional. The old
    # `requiredCaptureProtocol(spec) >= 2` let a protocol-1 spec — including a
    # version-less one — disable the authenticated envelope entirely.
    assert "client.setTransportIntegrity(verified.integrity, { required: true });" in main_js
    assert "requiredCaptureProtocol(spec) >= 2" not in main_js
    # The helper is no longer imported at all — nothing left branches on the
    # protocol number. (Checked on the import list, not the whole file, so the
    # explanatory comment above the call site does not satisfy it.)
    assert "  requiredCaptureProtocol," not in main_js
    assert "begin_capture: () => (isPlanSpec ? onPlanStart(ctx) : onStart(ctx))," in main_js
    # The single-capture path keeps its exact behavior — the "retry" action
    # (which a plan spec never emits) is untouched, still onStart.
    assert "retry: () => onStart(ctx)," in main_js


def test_capture_page_plan_loop_derives_named_screens_for_every_outcome():
    """Pins the plan loop's screen vocabulary: accepted-but-not-final (Next),
    rejected (Try again, SAME slot next attempt), refused (terminal, no
    retry), exhausted (terminal, distinct from success), and the final
    success terminal — matching SPEC W2.3's choreography."""
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    # §5.7 wraps these in a per-entry screen-copy fallback (heading/message
    # variables) rather than an inline `text:` template literal — the
    # fallback strings themselves are unchanged.
    assert '`Measurement ${index} of ${target} ✓`' in main_js
    # The retry screen's entry-less fallback headline no longer counts — the
    # eyebrow above it already carries "Measurement N of T — one more try", and
    # saying it twice was the §2.1 double-counter in miniature.
    assert '"Take that measurement again"' in main_js
    # (the only surviving mention is the comment recording what it replaced)
    assert main_js.count("needs another try") == 1
    # The same-slot retry keeps its slot; `retake` rides along so a rejected
    # VOLUNTARY retake's retry stays a retake (§2.6 — without the marker the
    # runner refuses it as out-of-order, which ends the session).
    assert "await runPlanCapture(ctx, { index, attempt: attempt + 1, retake });" in main_js
    assert "await runPlanCapture(ctx, { index: index + 1, attempt: attempt + 1 });" in main_js
    # RE-DERIVED for PR-T4 (work order D7): the shared completion screen's
    # fallback stopped promising an automatic continuation that a stage-1
    # session deliberately does not make.
    assert (
        '"All measurements done — the speaker page shows what happens next."'
        in main_js
    )
    # (the only surviving mention is the comment recording what it replaced)
    assert main_js.count("the speaker continues automatically") == 1
    assert 'text: "Measurement refused"' in main_js
    # The exhausted terminal keeps the attempt-limit copy for a genuine attempt
    # limit, but PR-T4 gave it a second, honest face: when the Pi says WHICH
    # clock expired, calling a timeout an attempt limit is simply false (work
    # order D8). The heading is now selected rather than literal.
    assert '"Reached the attempt limit"' in main_js
    assert "expiredBudgetCopy(ctx, verdict)" in main_js
    # Refusal and exhaustion never route through the success text.
    refused_start = main_js.index("function renderPlanRefused")
    refused_end = main_js.index("function renderPlanExhausted", refused_start)
    assert "All measurements done" not in main_js[refused_start:refused_end]


def test_capture_page_plan_loop_timeouts_are_terminal_not_stale_retries():
    """A begin-authorization or result-poll timeout in the plan loop must
    render a terminal screen (renderSweepFailed's shape — no button), not
    leave the previous "Next measurement"/"Try again" screen up with a
    button closure still bound to an (index, attempt) the Pi's own state may
    have already moved past. Retrying that stale pair risks a fatal
    begin_replayed refusal (run_capture_plan ends the whole session on ANY
    capture_refused)."""
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    start = main_js.index("async function waitForCaptureAuthorized(")
    end = main_js.index("async function waitForCaptureResult(", start)
    authorized_body = main_js[start:end]
    assert "failure.sweepFailed = true;" in authorized_body
    assert "throw failure;" in authorized_body

    start = main_js.index("async function waitForCaptureResult(")
    end = main_js.index("async function runPlanCapture(", start)
    result_body = main_js[start:end]
    assert "failure.sweepFailed = true;" in result_body
    assert "throw failure;" in result_body
    # The result wait scales with the recording window rather than reusing
    # the tight admission-latency budget — the Pi's own consume_capture()
    # analysis pass has no hard ceiling from run_capture_plan's poll loop.
    assert "Math.max(30000, Number(spec.duration_ms) || 30000)" in result_body


def test_capture_page_plan_loop_post_arm_errors_are_terminal_pre_arm_retries():
    """S1 (adversarial review of this PR): runPlanCapture's generic catch-all
    used to leave the previous "Next measurement"/"Try again" button live and
    bound to the SAME (index, attempt) already posted — a re-tap after e.g. a
    transient putBlob failure posts a begin the Pi refuses (begin_replayed /
    out_of_order → session-ending CaptureFailed), or worse re-records a
    sweep-less window. The catch now splits on whether `armed` was posted:
    post-arm generic errors render the terminal failure screen (mirroring
    the timeout paths); pre-arm errors (mic permission denied, a begin-post
    hiccup) keep the live retry — correct there, the round never started on
    the Pi — with Stop still wired and copy naming the ACTUAL on-screen
    affordance (N3). Behavior exercised in capture_plan_loop_test.mjs; these
    pins keep the wiring from silently regressing."""
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    start = main_js.index(
        "async function runPlanCapture(ctx, { index, attempt, retake = false }) {"
    )
    end = main_js.index("async function onPlanStart(ctx)", start)
    run_body = main_js[start:end]
    assert "let armedPosted = false;" in run_body
    assert "armedPosted = true;" in run_body
    # armedPosted is set BEFORE the armed post's await — a lost response may
    # still have armed the Pi, so a failed post must classify as post-arm.
    assert run_body.index("armedPosted = true;") < run_body.index("armed: true,")
    assert "} else if (armedPosted) {" in run_body
    assert "repairPreArmAffordance(ctx, { index, attempt, target, retake })" in run_body
    # The post-arm upload-cap refusal is terminal too (sweepFailed routing),
    # and the pre-arm clean-capture refusal keeps the session alive: exactly
    # the sweepFailed/deadSession/armedPosted terminal branches call
    # endPlanSession inside the catch, never the pre-arm else.
    assert "failure.sweepFailed = true;" in run_body
    assert "function planRetryAffordance(ctx) {" in main_js
    # captureFailureMessage's affordance parameter defaults to the v1/v2
    # flows' real Start button.
    assert 'function captureFailureMessage(err, retryAction = "Start") {' in main_js


def test_capture_page_plan_loop_blob_upload_carries_the_capture_index():
    """Each admitted attempt's blob rides capture_index = attempt - 1 (SPEC
    W2.3) — a retried slot must never clobber the prior attempt's upload."""
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert "await client.putBlob(blob, plaintextLen, sha256, attempt - 1);" in main_js


def test_capture_page_plan_loop_acknowledgement_captured_once_not_per_round():
    """The placement acknowledgement is derived ONCE at plan start (from the
    spec-rendered checkbox) and threaded through every round's armed event —
    there is no per-round checkbox on the page-owned Next/Try-again screens."""
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    start = main_js.index("async function onPlanStart(ctx)")
    end = main_js.index("// The whole capture leg, behind the single Start tap.", start)
    plan_start_body = main_js[start:end]
    assert "acceptedAcknowledgement(ctx.spec, ctx.captureRefs)" in plan_start_body
    assert "ctx.planAcknowledgement = acknowledgement;" in plan_start_body
    assert "acknowledgement: ctx.planAcknowledgement," in main_js


def test_capture_page_plan_loop_stop_stays_wired_across_rounds():
    """activeAbort is set ONCE in onPlanStart and persists across every
    round's async gaps (the idle time between "Next measurement" taps), only
    clearing at a genuine terminal outcome (endPlanSession) or Stop itself —
    never per-round, which would leave Stop dead while idling between
    captures."""
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert "activeAbort = controller.abort;" in main_js
    assert "function endPlanSession(ctx) {" in main_js
    assert "if (activeAbort === state.abort) activeAbort = null;" in main_js


def test_capture_page_ambient_stats_rides_the_armed_event_not_a_separate_post():
    """The relay's phone-event slot is last-write-wins: a standalone
    ambient_stats event posted before `armed` would almost always be
    overwritten before the Pi's ~0.75s poll ever saw it. ambientStatsFieldsFor
    is spread directly into the SAME already-awaited armed postEvent call in
    both onStart (v1/v2) and the plan loop (v3) — zero extra network round
    trips, "must not delay the capture sequence" for free."""
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert main_js.count("...ambientStatsFieldsFor(spec, noise),") == 2
    assert 'spec.kind !== "crossover_sweep"' in main_js


def test_capture_page_one_tap_mic_confirm_renders_when_hint_is_valid():
    """Wave-2 household-mic prefill hint (CaptureSpec.default_setup_calibration,
    #1540, adjudicated stored-submit amendment): the calibration screen shows
    "Using {label}{· serial}" as the primary action with a safe "Use a
    different microphone" fallback to today's full picker. Only offered when
    the Pi marked the hint `resolvable: true` (the stored-mode Pi build mints
    that only when the calibration_id currently resolves); a hint without the
    marker — an older Pi — renders the plain full picker (compat pin)."""
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert "function validDefaultSetupHint(spec) {" in main_js
    assert "if (hint.resolvable !== true) return null;" in main_js
    assert "function renderCalibrationConfirm(screenEl, ctx, hint) {" in main_js
    assert (
        "const heading = serialDisplay ? `Using ${label} · ${serialDisplay}` : `Using ${label}`;"
        in main_js
    )
    assert 'button("One tap to confirm", async () => {' in main_js
    assert 'button("Use a different microphone", () => {' in main_js
    assert "renderCalibration(screenEl, ctx, { skipHint: true });" in main_js
    # The gate: renderCalibration only shows the hint screen on a FRESH
    # visit (calibration.mode still "none"), never after the household has
    # already picked something (Back navigation from a later step).
    assert (
        'if (hint && String((setupState.calibration || {}).mode || "none") === "none") {'
        in main_js
    )


def test_capture_page_one_tap_confirm_submits_stored_and_falls_back_on_rejection():
    """Adjudicated stored-submit contract (amendment to the original
    stop-and-report): Confirm submits setup.calibration = {mode: "stored",
    calibration_id} (+ model, display-only) through the SAME shared
    post-calibration advance the picker's Continue uses
    (continueFromCalibration → validateSetupBeforeContinue /
    bindSetupBeforeLevel), and a Pi rejection (the household-mic record went
    stale between spec mint and submit) falls back to the full picker with a
    plain sentence — never a dead end — including the one DEFERRED-validation
    path (a position-collecting level_ramp validates at the position screen's
    bind). A failed one-tap is not re-offered within the page session
    (storedHintFailed). Behavior is exercised end-to-end in
    tests/js/capture_calibration_confirm_test.mjs; these pins keep the wiring
    from silently regressing."""
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    start = main_js.index("function renderCalibrationConfirm(screenEl, ctx, hint) {")
    end = main_js.index("function renderCalibration(screenEl, ctx", start)
    confirm_body = main_js[start:end]
    assert 'mode: "stored",' in confirm_body
    assert "calibration_id: String(hint.calibration_id)," in confirm_body
    assert 'model: String(hint.model || ""),' in confirm_body
    assert "await continueFromCalibration(screenEl, ctx);" in confirm_body
    assert "fallBackFromStoredCalibration(screenEl, ctx);" in confirm_body

    # One shared advance for both the picker Continue and the stored Confirm.
    assert "async function continueFromCalibration(screenEl, ctx) {" in main_js
    assert main_js.count("await continueFromCalibration(screenEl, ctx);") == 2

    # The rejection fallback: plain sentence, picker re-render, no re-offer.
    assert "function fallBackFromStoredCalibration(screenEl, ctx) {" in main_js
    assert (
        "The speaker couldn't use the saved microphone calibration. "
        "Set up the microphone manually instead." in main_js
    )
    assert "let storedHintFailed = false;" in main_js
    assert "storedHintFailed = true;" in main_js
    assert (
        "const hint = !skipHint && !storedHintFailed ? validDefaultSetupHint(ctx.spec) : null;"
        in main_js
    )

    # The deferred-validation path (position-collecting level_ramp) keeps the
    # same rejection contract at its bind.
    assert "if (usedStoredCalibration()) {" in main_js


def test_capture_page_upload_note_requires_actually_loaded_content():
    """The upload-mode picker's "Choose the file again only if you want to
    replace the current selection" note requires calibration.content, not
    just mode === "upload" — the note must only appear after a REAL upload
    landed in setupState.calibration this session, never for a mode value
    that arrived without file content (saveAndContinue's reuse branch also
    requires .content, so the two stay consistent)."""
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert (
        '(setupState.calibration || {}).mode === "upload" && (setupState.calibration || {}).content'
        in main_js
    )


def test_capture_page_mic_picker_never_erases_the_stored_preference():
    """Run-19 defect (a): renderMicChoice/buildMicPicker used to call
    rememberDeviceId("") the moment the remembered device wasn't in THIS
    render's enumerated list (unplugged right now, or a browser-rotated
    deviceId) — permanently erasing a good preference even though the same
    physical mic would have matched again next session. The in-memory
    fallback to Automatic stays (selectedDeviceId = ""); only the
    destructive localStorage write is gone."""
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert main_js.count('rememberDeviceId("");') == 0
    assert main_js.count("selectedDeviceId = \"\";") >= 2
    assert "never erase the stored" in main_js or "do NOT erase the stored" in main_js


def test_capture_page_reboot_clears_the_stale_mic_picker_not_appends():
    """W6.11 cosmetic fix: buildMicPicker() inserts its "Microphone:" selector
    as a SIBLING just before `screenEl`, not as a child of it, so it lives
    outside what setScreen()'s replaceChildren() clears on every fresh
    boot(). A hashchange re-boot (onHashChange -> bootFromHash -> boot) used
    to leave the PRIOR boot's picker in place and stack a second one beside
    it. boot() now removes the tracked picker before rendering the fresh
    loading screen, instead of appending on top of it."""
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert "let micPickerEl = null;" in main_js
    assert "micPickerEl = wrap;" in main_js
    boot_start = main_js.index("async function boot() {")
    boot_body = main_js[boot_start: boot_start + 1200]
    assert "micPickerEl.remove()" in boot_body
    # The removal must happen BEFORE setScreen() clears the loading screen,
    # not after.
    assert boot_body.index("micPickerEl.remove()") < boot_body.index("setScreen(screenEl")


def test_capture_page_dead_relay_session_never_offers_a_doomed_retry():
    """Run-19 defect (c): every phone-facing relay endpoint 404s "not_found"
    once a session's TTL lapses or the Pi purges it, so "Tap Start to try
    again" against a dead session is a guaranteed second failure.
    isDeadSessionError() is checked before the generic captureFailureMessage
    fallback in onStart, onLevelRampStart, and the plan loop's begin/result
    polls + top-level catch."""
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert "function isDeadSessionError(err) {" in main_js
    assert "function renderSessionExpired(ctx) {" in main_js
    assert (
        "This measurement link expired — return to the speaker page to start again."
        in main_js
    )
    assert main_js.count("isDeadSessionError(err)") >= 4
    assert main_js.count("renderSessionExpired(ctx);") >= 4


def test_capture_page_abort_signal_never_leaks_the_raw_dom_exception():
    """Run-19 defect (b): relay-client.js's _controlFetch now aborts with a
    named Error so a timed-out control request never surfaces the browser's
    default "signal is aborted without reason." text; main.js additionally
    normalizes ANY AbortError defensively (isRelayConnectivityAbort)."""
    relay_client_js = (_REPO / "capture-page/js/relay-client.js").read_text(encoding="utf-8")
    main_js = (_REPO / "capture-page/js/main.js").read_text(encoding="utf-8")

    assert "controller.abort(" in relay_client_js
    assert "new Error(" in relay_client_js
    assert "function isRelayConnectivityAbort(err, message) {" in main_js
    assert (
        "Lost the connection to the speaker's measurement relay for a moment."
        in main_js
    )


def test_capture_page_blob_put_supports_an_optional_capture_index():
    """relay-client.js's putBlob() gains an optional 4th `captureIndex` arg
    that appends `?index=N`; omitted stays byte-identical to the pre-Wave-2
    single-capture request (no query string at all)."""
    relay_client_js = (_REPO / "capture-page/js/relay-client.js").read_text(encoding="utf-8")

    assert "async putBlob(blob, plaintextLen, sha256Hex, captureIndex) {" in relay_client_js
    assert '`/blob?index=${captureIndex}`' in relay_client_js
