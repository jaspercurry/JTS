# Phase 4 item 5 — Completeness critic

Tree `2d571e6b8`. Read: 6 `p0-*.md`, 38 `p1-T*.md` (Coverage in full), 11 `p2-*.md`, `report-draft.md`,
`tiles/*`, `register/rows_{a,b,c}.py`. Scripts `P4-critic/{gap,gap2,cov_tab}.py`.

**Blocking process fact:** there is **no `register.md` and no `p3-*.md`**. Phase 3 left only scratch dirs
(`P3-blockers/` empty) and `register/rows_{a,b,c}.py` (172 uncompiled rows). The draft claims "Phase 3
adversarial verification (3 skeptic agents)" and "skeptic verdicts … recorded in the register"; **neither
artifact exists in the shared scratchpad**. Every row marked "pending skeptic" is *unverified, not pending*, and
§10's "the `p3` skeptics covered the Blockers, the deletions over 100 LOC, and the Phase 2 seam findings" is
unsupported by any file I can read.

---

## 1. What did no one open?

### 1a. Source files in **no tile file list** (`git ls-files` minus ∪ `tiles/*.txt`)
| bucket | files | lines | who touched it, if anyone |
|---|---:|---:|---|
| **`deploy/bin/`** (root-privileged executables) | 22 | **8,942** | no tile. Fragments only: L2, L4, L5, S1, S2, S4, T06, T23, p0-dup |
| `deploy/usbsink/` (9 of 10 scripts) | 9 | 1,148 | no tile; `uac2_name_patch.py`+`compose.sh` are in T24/T23 |
| `scripts/` extensionless | 5 | 2,128 | T26-1 self-reported the gap (`p1-T26-1.md:106`); T27 read 2 of 5 |
| `jasper_aec3/src/*.cpp` | 2 | 418 | none — T07 says so explicitly (only `process_10ms.h` was tiled) |
| `deploy/alsa/asoundrc.jasper`, `deploy/constraints-pi.pins` | 2 | 264 | S2 read the lane half of asoundrc |
| `.env.example`, `docs/doc-map.toml`, `c/…/Makefile`, `docs/examples/…` | 4 | 1,725 | p0-config/p0-docs/T22 read regions |
| `tests/` | **1,014** | **606,493** | **no tile at all** — p0-tests only (mechanical + sample) |
| **non-test total in no tile** | **44** | **~14,600** | |

**Named in none of the 55 reports** (grep, all p0/p1/p2): `deploy/bin/jasper-fanin-pitch-neutralize` (77),
`deploy/usbsink/jasper-usbgadget-{converge,up}` (397), `…usbsink-{name-patch,wait-card}` (327) — **≈800 lines
with zero eyes**. The two largest untiled files — `jasper-aec-reconcile` (2,307) and
`jasper-audio-hardware-reconcile` (2,197), **R-012's evidence base** — were read only in slices (L4
`:295-380,860-870`, `:555-660,1355-2000`; T06 "grepped … not read line by line"). **`jasper-deploy-health` (900)
was read in full by nobody** (T10: "not read; status asserted from the ADR"; S4: "header, `main`, tail") — yet
R-004/Wave 2 recommend **deleting it plus its 1,642-line test**, failing playbook hard-rule 6.

### 1b. Read-depth ledger (`P4-critic/cov_tab.py`)

Approximation: each tile's *self-stated* verbatim LOC ("read fully" + the verbatim part of "read structurally");
where only per-file altitudes were given I summed them (T13-2, T14-1 estimated). Denominator `tiles/INDEX.md`.
**573,044 tiled lines → ≈428,600 verbatim (75%) · ≈144,400 (25%) at signature/structural altitude only · 418
lines (2 `.cpp`) skipped outright.** No tile silently dropped a file. Thinnest tiles: **T15 17%** (11.7k of
14.3k signatures-only — and the draft's whole `bass_extension` boundary argument in §3.5 rests on it), **T14-4
48%**, **T01 51%** (`voice_daemon.py` — source of R-009), **T19-1 54%**, **T20 56%**, **T12-1 57%**, **T16-2
57%**, **T06 58%** (self-flags `output_topology.py`/`audio_runtime_plan.py` as possibly under-read), **T10
58%**. Largest single unread body: `deploy/assets/sound-profile/js/main.js` — ~390 of 403 function bodies read
by signature only (T25-1); the draft grades neither the file nor the page.

### 1c. Phase 2 seed scenarios
5 of 6 playbook seeds ran (S1, S2, S4, S5, S6). **S3 "hardware disappears → daemon degrades → hardware returns →
recovery" was folded into `p2-L2-resilience.md §B` and is the weakest link:**

- It is an 8-row **matrix**, not the numbered hop list with marked process boundaries and per-hop
  failure disposition BRIEF-LENS §B demands of a scenario — so the scenario premise ("only visible
  when you follow one request across boundaries") is not exercised.
- Eight rows **are** present: USB DAC · XVF mic · generic USB mic · network/WiFi · CamillaDSP ·
  fan-in ring · jasper-control socket · LLM provider. **The vanish half is well covered; the return
  half is not** — only the DAC row says what happens when the resource comes back (udev → un-park).
- Classes that vanish and are **absent**: the **HID/BLE accessory** (`accessories/bridge.py:643-663`,
  T09-C3 — the reader task dies permanently while `/run/jasper-input/status.json` reports
  `restarts:0, last_error:null`: exactly the "does not self-recover and the observability lies" case
  S3 exists to find), the **usbsink/USB-gadget card**, the **Bluetooth adapter**, **disk exhaustion**.

Also: the playbook's Phase-2 **duplication** and **doc-drift** lenses ran at *Phase 0* — **before any tile read
anything**. `p0-duplicates.md` Coverage says `transit, research, tools, accessories, bluetooth, xvf, chip_aec,
attribution, route_latency, wake_corpus` were "covered only by the mechanical scan; a targeted read … out of
budget". T03 then found two duplicate *subsystems* inside `research/`+`calibration_agent/`, and nothing re-swept
duplication afterwards (§3).

---

## 2. Which claims are still unverified?

### 2a. Draft §2 rows
| row | second source? | verdict |
|---|---|---|
| R-001 PSK unquoted | L4 (executed: `$(…)` PSK ran `id` as uid 0) + reviewer + `T16-3`/`T18` touch the file | **verified, 2 independent** |
| R-002 redactor | T18 (executed), T26-2 (executed), L4 (23-string probe), p0-tests | **verified, 3 independent** |
| R-003 `SKIP_INSTALL` | T23-C1, T26-1-C1, T26-2-C1, S4 (executed under stubbed ssh/rsync) | **verified, 4** — *except* the **interactive-sudo default-path bypass, which is S4 alone**. That half is what makes the grade C+ rather than B−. |
| R-004 nothing gates on health | S4 only (T10 corroborates `--core` unshipped) | **1 source; target file unread (§1a)** |
| R-005 polkit 200-and-lie | T08-C1 + S1 (set difference computed at HEAD) + reviewer | **verified, 3** |
| R-006 `set_active_config_raw` | T05-F1, S2, S6 | **door verified ×3.** Reachability of a `>0` limit from shipped inputs: draft says "pending skeptic" → **unverified** |
| R-007 `max_peak_dbfs` | T19-2-C8 (with a mitigation) → S2 re-grade | **1 source for the re-grade**; S2 itself lists exploitability as hardware-only |
| R-008 restart ladder inverted | L2 §C for the judgment; T24 recorded the same raw fields and graded them "shrink-prose" | **1 source for the finding** |
| R-009 wake silence ×4 | T01, S5, L2, L3 concur | **verified, 3–4** |
| R-010 election is not one | **S5 alone**. T09 read `peering/state.py` in full and graded it *earns-keep — "pure, testable, the actual arbitration"* | **1 source, contradicted by a tile** (see §4) |
| R-011 streambox volume | S6 (grep-derived caller counts, labelled as such) | **1 source, grep-based** |
| R-012 `outputd.env` two writers | L4 §3 (re-counted, corrected p0), T06-D, p0-config-3a | **verified ×3**, but both bash writers were never read whole |
| R-013 streambox web unit | T24 (Blocker, "delete the unit") vs S1 (Should-fix, "copy 11 directives") | **2 sources that disagree** (§4) |
| R-014 1% mute | T04-F2 → S6 re-grade | **2 sources** |

**Register-wide:** of 172 rows, **133 (77%) carry `verify='N'`**; **95 (55%) cite one report**.

### 2b. Load-bearing numbers from exactly one report, never re-measured
`1,309 event names` · `9 publish mechanisms` · `13 producers` · `5 of 25 /state sections carry freshness` (all
**L3**) · `1,708 function-local imports / 755 with no reason` · `23-package SCC / 15 edges` · `55 pairs / 109
chains` (all **L1**) · `155–210 forks/min`, `120–180 from VolumeObserver`, `14 units / MemoryMax 1,464 MB`, `967
modules / 1.14 s / 105 MB RSS` (all **L5**) · `19,461 test functions`, `1,645 private-name patches / 191 files`,
`209 source-reading test files`, `91 caplog files` (all **p0-tests**; **nothing re-swept `tests/`** — no tile,
no lens) · `157 ADRs / 79 dated one day / 3 batch-ADRs` (**p0-docs**, which also states 145 of 157 ADRs were
never opened and 7 named plan docs were status-line-read only) · `829 tokens / 227 live / 289 no-writer / 190
test-only / 42 dead` (**p0-config**, whose writer-counting method L4 §3 explicitly calls "over-counted … worse
where it matters") · `1,455 log_event sites` (**T18**) · `136 distinct /var/lib/jasper* literals` (**p0-dup**;
p0-config counted **281** distinct paths and L2 counted **137** — three numbers for one census, none
reconciled). Double-sourced and safe: `172 checks / 87 cannot fail` (T10, "confirmed" by L3:277); the 4-module
executed cycle (L1 + T15 found `bass_extension/adapters/base.py:109-111` independently).

### 2c. Draft wording stronger than its source
| draft claim | what the source says |
|---|---|
| §1 "the tree has **zero orphan modules**" | `p0-orphans.md:7` — "zero module-level orphans **outside the ADR-0018-parked bass-extension half**" (4,149 LOC). T15 adds `bench/{stimulus,live_proof,excitation}` (620) with zero importers *even inside the dead chain*. Draft §3.5 lists them; §1 denies them. |
| §4 "the cue manager … has **zero structured events**" | `p2-L3:100` — `AudioCueManager.play` has "5 `logger.warning` prose branches, **1 `log_event`**"; `p1-T02:87` counts 3 `log_event` in `cues/`. |
| §3.3 "The tree **does not have a duplicate-subsystem problem** — every large seam probed is genuinely converged" | refuted by ≥5 tiles (§3 below), including one the draft itself files as owner-decision #3. |
| §1 "zero unreferenced scripts" | T22-2: `c/…/ring_{writer,reader}_bench.c` are "invoked by no script, systemd unit, or doc" (T22 keeps them — but they are unreferenced). |
| §10 "Known gaps: `test-fast`, `test-merge`, `use`, `jasper-pipe-probe`, `rust-ci-needed` fell in no tile" | this is T26-1's self-report copied verbatim; the real untiled set is **44 files / ~14,600 non-test lines** incl. all of `deploy/bin/` (§1a). |
| §10 "each reported opening every file in its tile" | true, but 25% of tiled lines were opened at signature altitude only, and one tile (T15) read 17% verbatim. |
| §5 "585k test LOC vs **424k** product" vs §10 "**573,044** lines of product code were tiled" | two denominators for "product" three sections apart; neither is labelled. |

---

## 3. Corners we rationalised past

### 3a. Subsystems with real code that the draft barely names
| subsystem | tile verdict | draft treatment | gap? |
|---|---|---|---|
| `jasper/{research,calibration_agent}/` + `accounts`/`google_creds` | **T03: two Blockers** — "two independent OpenAI Responses clients" (~1,000 LOC parallel provider/store/surface) and "two registry classes, one concern, ~90 LOC verbatim, the docstring admits it" | **zero findings from T03 reach the draft.** `accounts`/`google_creds` appear only in a layer list and an `atomic_io` row | **YES — the whole 40-file/13k-LOC tile is silent in the report** |
| `jasper/accessories/` | T09-3 Should-fix: reader tasks die permanently, status file **lies** healthy | draft §1 praises `accessories/reconcile` as a template; the defect is absent | **YES** — and it is the missing S3 row (§1c) |
| `jasper/multiroom/cascade_timeline.py` | T09-5: 360 LOC + daemon thread + `journalctl -o json` every 15 s + an **unrequested `JASPER_*` knob** + duplicates `airplay_health.classify_journal_line` | absent from draft **and from the register** | **YES — dropped between tile and register with no reason** |
| `jasper/multiroom/snapcast_rpc.py` | T09-6: 3 methods, zero production callers, a documented "latent second authority" | absent from draft and register | **YES** |
| `jasper/transit/`, `jasper/bluetooth/` | T03: transit coherent but 4 files/mode + a documented cycle + 6 lazy imports (T03-8); T04-F9 rfkill reach into `source_intent` | layer list; `roles.py` + the 224-LOC handler framework | partial |
| `usbsink/`, `local_sources/`, `route_latency/`, `attribution/`, `scripts/` (27k) | earns-keep with named nits | present §3.1/§3.3 | **no gap** |
| `wake_training/`, `experiments/usb-turntable`, `c/jts-ring-ioplug` | T22 read fully; ring benches "keep", vs p0-orphans' tentative delete | §8-8 covers turntable; the bench re-grade is unrecorded | minor |
| `deploy/assets/*` (30k JS/CSS) | T25-1/T25-2 | §3.3 last row only; `sound-profile/js/main.js` (7,655) ungraded | partial |
| `tests/` (585–606k) | **no tile**; p0-tests only | §5 in full | **structural gap: every §5 number is single-source** |
| `docs/historical`, `docs/research`, 145 of 157 ADRs | not read | §10 discloses it | disclosed |

### 3b. Trace of 5 sampled tiles' top-5 findings into draft/register
| tile | top findings landing in draft | dropped |
|---|---|---|
| T02 | (none of the top 5) | **Blocker** `recording_backend.py:1404-1628` unbounded stop-retry (register keeps it, verify=Y) — **not in the draft**; `wake_events` retention appears but the retry does not |
| T07 | #1 `CLEAR_CONFIGURATION` ✓ §2; #2 `chip_aec/health` import cost ✓ §4 | #3 `aec_bridge_corpus_lanes` cycle; **#4 `_aec_loop` 708 LOC** (in register, not in the §3.2 god-file table); #5 `BRIDGE_STATS_PATH` ×4 |
| T09 | #1 peering dead half ✓ Wave 2; #2 `reconcile.main` ✓ §3.2; #4 eager peering import ✓ §4 | **#3 accessories**, **#5 cascade_timeline**, #6 `snapcast_rpc` |
| T15 | #1 `runtime_integrity` 4th STATUS reader ✓ §2; `status_socket` move ✓ §3.1; bass park boundary ✓ §3.5 | `tap_client.py` untested (265 LOC, hardware UDS client); the `bench/__init__` partial module map |
| T25-1 | clipboard ×3, `renderSection` ×2, poll loops, `el()` ✓ §3.3 (one line) | **C4/C5**: `wifi/js/main.js` is all-`innerHTML`, has **no JS harness**, and its only pin is `test_wifi_setup_ui.py`'s source-text asserts on the Wi-Fi lockout path — the draft cites the test in §5 but not the untested-page half |

Also dropped *after* the register: **T19-1-F1 (Blocker, verify=Y) — `info!`/`warn!` + a `String` allocation on
the SCHED_FIFO audio thread under `LimitRTTIME=200000` with no `StandardOutput=` — is in the register and absent
from the draft**, while §4 praises the very `sync_channel` pattern the finding says the mixer failed to use. The
report's largest single silent omission.

---

## 4. Contradictions the draft did not resolve

| # | conflict | draft adopted | justified? |
|---|---|---|---|
| 1 | **Streambox web unit.** T24-C1 **Blocker**, "4 of 14 directives", fix = *delete* the unit + `nginx-jasper-streambox.conf` (~700 dup lines). S1-10 **Should-fix**, "5 of 19", fix = copy 11 uid-independent directives, keep the `User=` deferral. The **register still carries T24's Blocker+delete**. | S1 (R-013) | **Half-justified.** S1's `User=` re-grade is evidenced (removal condition + a pin). But the draft silently drops T24's other half — the 94%-identical nginx conf and its ~700-line dedup — which S1 never examined, and never explains 4/14 vs 5/19. |
| 2 | **`/state` polling.** T08-C7 (pages poll `/state` at 4–7 s) vs L3:231 and L5:185, which **refute it independently**. | L3/L5 | **Yes** — 2 agents, mechanical (no nginx `/state` proxy; nothing in `deploy/assets/` fetches it). Listed in §10. Not an L3-vs-L5 conflict; they concur. |
| 3 | **Wake legs.** p0-config-278 "hard-seeded 0, nothing sets 1" → delete 4 legs. L4-K1 **REFUTED** (`audio_profile_state.py:209`; `POST /aec/leg` via `_TOGGLE_TO_ENV_KEY:89-94`; a test asserts each `=1`). | L4 | **Yes**, disclosed §3.4/§10. |
| 4 | **Env-file writer counts.** p0-config: `outputd.env` 3 writers, `wake_model.env` 2 unlocked, `wifi_guardian.env` 3. L4 §3: 2 / 2-both-locked / 2 — "p0 over-counted; the real bug is the lock mismatch, not the count". | L4 | **Yes**, disclosed. **But** the draft keeps p0-config's *knob* census (829/227/289/190/42) untouched in §3.4 while adopting L4's correction of the same agent's writer census — inconsistent trust in one source. |
| 5 | **SCC size.** p0-inventory **34** · L1 **72** · T14-3 **99** · T14-4 **98** · module-level-only **4**. | L1 (72), with a reconciliation table (`p2-L1:26-32`) explaining each | **Yes** — L1 reproduced p0's algorithm (`repro_p0.py` → `[34,…]`) and named the resolver bug. Best-verified reconciliation in the corpus. |
| 6 | **`max_peak_dbfs`.** T19-2-C8 Should-fix *with* the mitigation "CamillaDSP's clamped volume is downstream". S2-5 refutes the mitigation (`volume_limit` caps the fader, not the signal; `grep -c Limiter jasper/sound/camilla_yaml.py` → 0) and re-grades up. | S2 (R-007) | **Yes on evidence**, but single-source and never skeptic-checked, and S2 itself files exploitability as hardware-only — yet the draft lets it carry a third of the safety grade. |
| 7 | **`peering/state.py`.** T09 read it in full: *earns-keep — "pure, testable, the actual arbitration"*. S5 (R-010): three terminal paths return no action, ARBITRATE never resolves, the 0.5 s client timeout beats the 0.65 s daemon fail-open. | S5 | **Justified in principle** — the timeout inversion spans a process boundary a per-file read cannot see. **Unresolved in the report**: it never says a tile passed the file, and R-010 stays single-source. |
| 8 | **Ring benches.** p0-orphans: unreferenced → tentative delete. T22-2 read both in full → **keep** (cross-language interop proof CI cannot cover). | neither stated | §1's "zero unreferenced scripts" quietly takes p0's side of a question T22 answered the other way. |

---

## 5. What would move each grade, and is it double-sourced?

| grade | the single fact that carries it | sources | risk |
|---|---|---|---|
| Hardware/audio safety **B** | `/sound/live-draft` reaches `set_active_config_raw` on every slider move with no `volume_limit` parse; `check_camilla_volume_limit` reads the *persisted* file (R-006) | T05+S2+S6 | **low** (3 agents); the second leg (`max_peak_dbfs`) is 1 agent, hardware-gated |
| Secrets **C** | `redact_secrets` misses `JASPER_*_PSK` and is the sole guard on `/state.voice.connection_error` on `0.0.0.0:8780` | T18+T26-2+L4, all by **execution** | **low** |
| Deploy integrity **C+** | the **default interactive-sudo path** (not just `SKIP_INSTALL=1`) skips identity + direction + OOM + manifest + health | **S4 only** (pty execution) | **medium** — refute it and the grade is a flag-gated bypass, i.e. B− |
| Resilient **B−** | control + aec-bridge: `StartLimitBurst=4 × RestartSec=2` = 8 s to `StartLimitAction=reboot`, no config-error park | L2 §C judged it; T24 read the same fields and graded "shrink-prose" | **medium** — fields double-read, the *inversion* claim is not |
| Observable **B−** | 1,309 event names / no registry, 9 publish mechanisms, cue manager unobservable | **L3 alone**, and its own text says `play` has 1 `log_event` (draft says zero) | **high** — every number single-source, one already overstated |
| Clean **C+** | the 23-package SCC held by 15 module-level edges, 10 shelving mistakes | **L1 alone** — but it shipped a runnable `import-linter` config, ran it at HEAD, reproduced the rivals | **low** despite single-source |
| Right-sized **C** | tuning zone = 41% of `jasper/`; 289 knobs with no writer | p0-inventory (41%) unre-measured; 289 from p0-config, whose writer method L4 disputes | **medium** |
| Tests **B−** | 1,645 private-name patches / 209 source-reading files / 19,461 functions | **p0-tests alone; `tests/` was in no tile and no Phase-2 lens** | **high** |
| Docs **B** | governed corpus, claims verified, 157 ADRs with no index | **p0-docs alone**, which says 145 ADRs + 7 plan docs were never opened | **medium-high** |
| Newcomer followability **C** | "where things live is derivable from the graph, not the tree" | T18 proposed the regroup, L1 simulated and corrected it | **low** — 2 agents |

---

## 6. Next round — open these first

1. **`deploy/bin/` as its own tile** (22 files, 8,942 lines) — `jasper-aec-reconcile` and
   `jasper-audio-hardware-reconcile` line by line (R-012's evidence base) + the 5 never-mentioned scripts.
2. **`jasper-deploy-health` + its test (900 + 1,642)** before Wave 2 deletes them unread; same bar
   for the `crossover_v2_flow` barrel and the 143 lazy doors.
3. **A real S3 scenario** — resource *return* paths and the four classes L2 §B omits (HID accessory,
   usbsink card, Bluetooth adapter, disk), starting at `accessories/bridge.py:643-663`.
4. **Re-open T03**: two OpenAI Responses stacks, two account registries — then re-test §3.3's
   "no duplicate-subsystem problem" against T03/T11/T13-2/T14-4/T16-2.
5. **`rust/jasper-fanin/src/mixer.rs` RT-thread logging** (T19-1-F1; register Blocker, absent from the report).
6. **A tests lens**, not a cartography pass: `tests/` is 1,014 files in no tile.
7. **A duplication lens run *after* the tiles**, over the ten packages `p0-duplicates.md` skipped.
8. **The register→draft delta**: 133/172 rows `verify='N'`; ≥6 tile findings vanished with no reason.
9. **`sound-profile/js/main.js`** (~390 bodies unread) and `wifi/js/main.js` (untested, all-`innerHTML`).
10. **Reconcile the three path censuses** (136/281/137) and the two "product LOC" denominators (424k/573k).
