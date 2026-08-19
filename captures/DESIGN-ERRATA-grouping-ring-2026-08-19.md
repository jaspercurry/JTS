# Design errata — Phase-1 grouping ring (to the design author)

From the cloud conductor session executing waves PR-2..PR-6, 2026-08-19.
Against: `captures/DESIGN-PROPOSAL-grouping-ring-2026-08-17.md` (v2.1, pin
6e569e8dc) and `captures/DESIGN-REVIEW-grouping-ring-2026-08-18.md` (sealed
0/0 at the confirm pass). Items 1–3 were sealed during the PR-1 panel and are
owed per the handoff; items 4–6 are conductor findings from this session's
pickup verification (recorded in the lifeline's 2026-08-19 entries). None
reopen the seal: 1–5 are text corrections, 6 is post-seal drift the execution
adapts to.

## 1. §3.4 — the unlink rationale is escalation asymmetry, not ordering

The §3.4 bounce/ordering rationale is true but NOT OPERATIVE (panel C-S1,
third layer): not a differentiator (the three unlinked rings also have live
writers at that instant), and the named harm is unreachable (the bounce is
park-writer → reader re-attaches → start-writer, which closes the window with
or without the unlink). The true reason is FAILURE-ESCALATION ASYMMETRY:
jasper-fanin carries `StartLimitAction=reboot` (stale ring = fatal attach =
reboot mid-install before the manifest → unlink MANDATORY, the documented
trap); snapclient carries NO StartLimitAction by explicit unit design (a
follower degrades, visibly; it never reboots the household) → a stale
grouping ring costs 4 retries + a failed unit, and the unlink buys nothing.
Durable unit property vs call-order accident. The ordering is already
mechanically pinned (reordering main() reds a pre-existing sequencing test),
which is the other reason ordering must not be the recorded rationale.
Adjudicated PLAN:696-703; finding PLAN:660-676. PR-1's fix round already
rewrote the code comment + T-3 docstring; §3.4's prose should carry the same
reason.

## 2. §8.2 T-3 — record the doctor-presence deviation

The builder's deviation is RIGHT and should be the documented shape: the
grouping conf.d presence check does NOT join `ring_asset_presence`, because
that check is the coupling ARM GATE — joining it would let a missing grouping
conf.d refuse the arm. 61- has no presence check either, and
`.install-manifest` is web-assets only. Deferred-loud is acceptable for the
inert phase; at PR-3 a missing conf.d → snapclient fails 4×, visible, never
reboots. Adjudicated PLAN:583-589; lens C confirm PLAN:688-690.

## 3. §3.2 N6 — the `[M]` on "fleet default is unarmed" is a mislabel, and
the truth STRENGTHENS the fallback

N6 tags fleet-default-unarmed `[M]` — a measured-tag on a claim the census
had explicitly flagged unmeasured one document earlier (an honesty-bar
failure independent of the claim's truth), and it is load-bearing for "256×16
is shipped and legal, not exercised." Measured truth (usage map L-05,
MAP:486-505): the 256×16 renderer-lane geometry IS exercised — eight
ring-lane PCMs across both boxes, fan-in logging `slot_frames=256 n_slots=16`
on every attach. So the fallback geometry runs in production today on this
fleet. Scope note: exercised by the renderer lanes — snapclient's own
negotiation against it remains S0's to demonstrate.

## 4. §10.1 PR-3 row — "four test modules" contradicts §8.2's own table

The PR-3 row says "Breaks four test modules (§8.2)"; §8.2's assigned table
(proposal ~:885-896) names SIX modules with PR-3 in the Breaks-on column:
test_multiroom_active_leader_config.py, test_active_speaker_driver_domain.py,
test_renderer_ring_lanes.py, test_env_vars_codified.py,
test_multiroom_follower_config.py, test_multiroom_reconcile.py. The detailed
table governs; the wave-row count is stale (likely pre-C-12). The handoff
prompt inherited "FOUR" from the row.

## 5. §10.1 PR-6 row — EG-4's home contradicts §12

The PR-6 row lists "EG-3/EG-4"; §12's per-item disposition (proposal
~:1138-1141) says EG-4 is "Fix in passing in PR-3, which already opens that
file" (camilla_yaml.py:137-153, the oscillation mis-attribution). §12
governs: EG-4 executes in PR-3; PR-6's sweep verifies it landed. EG-3's PR-6
disposition is consistent and unaffected.

## 6. §5.2(b) — post-seal drift: the writer now has a THIRD input

PR #2719 (merged 2026-08-18 21:39, after the seal) renamed
`_active_speaker_box_state()` → `_output_topology_state()` returning
`(active, flat_allowed)`, and gave `outputd_grouping_env` a
`flat_output_allowed` keyword — its clearing branch is now
`if active_endpoint or not flat_output_allowed:`. §5.2(b)'s "a pure predicate
over (GroupingConfig, active_endpoint) … derived from the same function that
writes the lane, so gate and writer cannot disagree" is therefore false at
HEAD as literally written: a two-input predicate structurally cannot mirror a
three-input writer (a passive member whose saved topology forbids a flat DAC
graph now also gets the cleared lane). Execution adaptation, carried in
PR-5's brief as required analysis: `dac_content_lane_armed` takes the third
input, sourced the same way as the writer's caller sources it; C-16's
import-direction constraint (plain bools cross the boundary, never the
predicate import) is preserved; T-5's fixtures widen to the third input.
Related citation drift, no action needed beyond re-anchoring at
implementation time: camilla_yaml.py ±182 lines (bake emitter def :3978,
clamp :3839-3843 — signature byte-identical, so T-8b's TypeError kill
holds); graph_carrier.py:414 → :440; setup_status.py grouping_allowed sites
→ :719/:836/:1210.

Last verified: 2026-08-19
