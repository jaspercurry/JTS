# Prompt: dispose of `/balance/` (delete) and `/sync/` (keep, fold into the pair page)

Hand Part 1 whole to an orchestrating agent as an independent deletion job.
Part 2 is a work spec that the web cleanup program executes as part of its
Sound/pair wave (ledger row C.S4); it is here because the two pages were
studied together. Evidence was gathered 2026-09-05 against `main` at
`c6329be2d` by a read-only Opus agent; re-verify every line at HEAD.

## Findings that decide it

Both pages are live, working, and consumed at runtime; neither is dead
code. Both are unreachable from the product: no landing row, no link from
`/rooms/`, HTTPS-only nginx blocks (`deploy/nginx-jasper.conf:586-614`).
The `/rooms/` → `/balance/` link was **deliberately deleted** on 2026-09-02
(commit `1e98ef108`, #1842: the self-signed origin fails with a cert
interstitial) and is pinned by `tests/js/rooms_bond_card_link_test.mjs` and
`tests/test_web_rooms_setup.py:3220`; the standing rule is at
`nginx-jasper.conf:425-430` (#2632): no journey step may redirect into the
self-signed origin. The only sanctioned shape is the `/sound/room/`
precedent: a port-80 non-capture landing that the household then
deliberately re-enters over HTTPS for the microphone step.

**`/balance/`** measures equal loudness with the phone mic and writes
`JASPER_GROUPING_TRIM_DB` through `/grouping/set`. `/rooms/`'s by-ear slider
(`rooms_setup.py:1490 _balance_trims_from_db`) produces the identical
normalised trims, writes the same key through the same door, hits the same
live-apply branch, and reads `/balance/`'s result back (`:1509`). Its whole
output is duplicated; only the measurement method is unique, for the one
axis a human sets by ear with immediate feedback. Last feature commit
2026-06-25; everything since is convergence sweeps. **Verdict: DELETE.**

**`/sync/`** measures L/R acoustic arrival at the seat with a chirp marker
and writes `JASPER_GROUPING_LEFT_DELAY_MS` / `_RIGHT_DELAY_MS`, which the
leader's CamillaDSP graph consumes as `room_delay_l/r` Delay filters
(`multiroom/member_config.py:69-75`, `camilla_stereo_prefix.py:138-146`).
It is the **only writer** of that knob in the tree; arrival offset cannot be
set by ear; `docs/dumb-endpoint-bringup.md:280-289` ratifies the leader
render graph as the owner of pair arrival delta. It has never been iterated
after first build (result surface is raw `JSON.stringify`,
`deploy/assets/sync/js/main.js:32-34`). Two correctness gaps: the applied
delay is surfaced nowhere (`multiroom/state.py:703-724` omits it, no doctor
check, `/rooms/` never shows it), and unbond clears trim but not delay
(`rooms_setup.py:1198`; `control/server.py:1436-1440` preserves omitted
keys), so re-bonding with left/right swapped applies a stale delay to the
wrong channel. **Verdict: KEEP, fold into the pair page, fix both gaps.**

---

## Part 1 — delete `/balance/` (one PR, Sonnet; ~2,800 lines removed)

Re-verify at HEAD, then delete. Run `scripts/test-fast`, `/simplify`,
`/code-review` medium; push. Verify no caller before each removal
(registries, entry points, systemd, `deploy/bin`, CI, `importlib`).

**Delete whole:** `jasper/web/balance_flow.py`, `jasper/web/balance_level.py`,
`jasper/web/balance_volume_guard.py`, `jasper/multiroom/balance.py` (callers
are only `balance_flow.py`), `deploy/assets/balance/` (directory),
`tests/test_web_balance_flow.py`, `tests/test_web_balance_volume_guard.py`,
`tests/test_multiroom_balance.py`.

**Edit:**
- `jasper/web/correction_setup.py` — `:101` drop `"/balance"` from
  `_FOLLOWER_DELEGATED_PAGE_PATHS`; `:362-368` POST allowlist entries;
  `:3979-4023` `_dispatch_balance`; `:4492-4493` GET allowlist; `:4594-4606`
  GET handlers; `:4689-4690` dispatch; `:796-808` the balance arm of the
  three-way measurement mutex (collapses to two-way).
- `jasper/web/active_speaker_flow.py:77,81-83` — remove `_balance_phase`.
- `jasper/web/pair_flow.py` — **keep**; `sync_flow.py:28` still imports it.
- `deploy/nginx-jasper.conf:581-598`; `deploy/nginx-jasper-streambox.conf:354-362`.
- `deploy/lib/install/web-assets.sh:66-73` — **add** `rm -rf --
  "${assets_root:?}/balance"` beside the existing upgrade-cleanup lines;
  page dirs are copied additively, so without this every deployed speaker
  keeps serving the deleted directory and `check_web_design_assets` sees an
  orphan.
- `deploy/install.sh:1827` — drop `balance` from the streambox banner.
- `pyproject.toml:433` — remove `jasper.web.balance_flow` from mypy `ignore_errors`.
- `docs/doc-map.toml:716` (`jasper/web/balance_*.py` glob), `:771`
  (verification command).
- Tests referencing it: `tests/test_active_speaker_flow.py:61,64,72,75`;
  `tests/test_correction_substream_ssot.py:131,170`;
  `tests/test_measurement_vocabulary.py:90`; `tests/test_web_correction_setup.py:549-551`;
  `tests/test_web_rooms_setup.py:74`; `tests/test_web_wizard_conventions.py:42`
  and `:726-737`; docstring/comment at `tests/test_correction_lane_play.py:219`,
  `tests/test_wake_events.py:1179`.
- The no-link pins `tests/js/rooms_bond_card_link_test.mjs` and
  `tests/test_web_rooms_setup.py:3220` **stay**; reword their docstrings
  ("the page is gone") and keep the assertion.
- Docs: `docs/tuning-operator-runbook.md:170`; `docs/dumb-endpoint-bringup.md:51,128,470`.

**Must not be touched (runtime consumers `/rooms/` still writes):**
`JASPER_GROUPING_TRIM_DB` and its chain (`control/server.py:1457-1462`,
`control/handlers/grouping.py`, `multiroom/config.py`, `multiroom/runtime_balance.py`,
`multiroom/reconcile.py:629`, `multiroom/state.py:711`, `active_leader_config.py:262`,
`follower_config.py:212`, `camilla_config_contract.DRIVER_DOMAIN_PAIR_TRIM_FILTER`,
outputd's `OUTPUTD_DAC_CONTENT_TRIM_ENV`); the `/rooms/` by-ear surface
(`rooms_setup.py:1482-1760`, `rooms/js/main.js:465-540`, `grouping-view.js`);
**`scripts/check-balance-trim-parity.mjs` + `tests/fixtures/balance_trim_parity_fixture.json`**
(despite the name, the `/rooms/` JS↔Python parity contract) and
`docs/testing-tooling.md:109`; `deploy/assets/shared/js/measurement-audio.js`
(its remaining consumer is `/sync/`, and the cleanup program moves the room
flow's capture onto it); `HeldWindow` / `measurement_window` /
`exec_correction_play` (shared with correction and commissioning).

Report: diffstat, `scripts/test-fast` sentinel, and confirmation that the
mutex in `correction_setup.py` now has two arms. Tick ledger row P.1 in
`docs/UX-AUDIT-2026-09-03.md`.

---

## Part 2 — fold `/sync/` into the pair page (program ledger row C.S4)

Lands with the Sound wave once the site map is approved; the URL becomes
whatever the manifest gives the pair page's child (proposal:
`/sound/pair/sync/`). Work, in one or two PRs (Opus for the surface, Sonnet
for the two fixes):

1. **Fix the two correctness gaps first (Sonnet, independent of IA):**
   add `left_delay_ms` / `right_delay_ms` to the grouping snapshot at
   `multiroom/state.py:711`; add `"left_delay_ms": 0.0, "right_delay_ms": 0.0`
   to the unbond body at `rooms_setup.py:1198`. One behaviour pin each in the
   existing test files.
2. **Reachability under the #2632 rule:** a port-80 nginx mirror for the
   non-capture landing (after `nginx-jasper.conf:445`, same shape and comment
   as `/sound/room/`); the capture step stays on 443 and is entered
   deliberately with the same secure-context handoff the Room page uses.
   A "Tune timing" row on the pair page's bonded face
   (`rooms/js/main.js` ~`:531-540`) as a same-origin relative link — amend
   `tests/js/rooms_bond_card_link_test.mjs` and `tests/test_web_rooms_setup.py:3220`
   to forbid `https://<hostname>` anchors specifically, not all anchors, and
   say so in the PR.
3. **Make the surface legible** (audit SY-2..SY-5, SY-H1, SY-H2):
   `canonical_header` with the pair page as parent; show the applied delay
   on the pair page and on this page's landing state; recommendation as a
   `defList` with the raw analysis behind a disclosure; `jtsConfirm` before
   Apply writes the render graph; precondition gate on load (no bonded
   leader → say so and link back); static stylesheet; drop the hand-rolled
   csrf const at `sync/js/main.js:19-20` for `shared/js/http.js`.
4. Copy: name what the user controls ("timing between the two speakers"),
   never "arrival", "marker", "cross-correlation".

If the owner later declines this work, delete `/sync/` with its consumer
chain (`multiroom/config.py:616-678`, `member_config.py:69-75`,
`control/server.py:1465-1468`, `sound/camilla_yaml.py:472-484`,
`camilla_stereo_prefix.py:138-150`) and amend
`docs/dumb-endpoint-bringup.md:280-289`, which currently asserts this knob
is how the leader owns pair arrival delta. That is the strictly worse
outcome: the capability is real and has no substitute.
