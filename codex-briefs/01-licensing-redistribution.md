# Brief 01 — Clear the redistribution blockers (licensing)

Mission: make every file in the repo legally redistributable, per
`docs/REVIEW-2026-06-12-oss-due-diligence.md` §4.1. The repo's own
`firmware/THIRD_PARTY.md` marks several shipped files "NOT cleared for
redistribution" — an open-source launch is redistribution.

Branch: `codex/licensing-redistribution`. File fence: `firmware/dial/**`,
`firmware/THIRD_PARTY.md`, `LICENSE-third-party.md`, `NOTICE`,
`jasper/xvf/xvf_host.py`, `jasper/aec_engines/dtln_models.py`,
`jasper/wake_models.py` (docs/comments only).

## PR 1 — delete the vendored CST816D touch driver (dormant code)

- `firmware/dial/src/CST816D.{h,cpp}` is the uncleared ELECROW driver. Touch is
  currently DISABLED: `firmware/dial/src/display.cpp:196` comments out
  `touch.begin()` — so deletion is zero functional change and instantly
  resolves the worst blocker. It also removes a real bug (the driver's
  `i2c_read` spins forever on NACK).
- Delete both files; remove the `#include` and the dormant `touch` object /
  commented call sites in display.cpp (and anywhere else `CST816D` appears —
  grep). Update `firmware/THIRD_PARTY.md` (remove the entry, note the date and
  reason) and `LICENSE-third-party.md` if it has a row.
- Leave a short comment where touch hardware would re-attach: future touch
  support needs a permissively-licensed driver (fbiego's MIT `CST816S` Arduino
  library is the known candidate — CST816D is a register-compatible sibling)
  or a clean-room ~100-line I2C implementation with bounded retries.
- Verify: firmware must still compile. If PlatformIO is available locally
  (AGENTS.md "Local PIO setup" — python3.11 venv), run
  `bash scripts/check-firmware-builds.sh`. If not feasible, say so explicitly
  in the PR and ask the maintainer to run it — do NOT claim it compiles.

## PR 2 — replace the four SquareLine bitmap assets with a procedural gauge

- The four `ui_img_*.c` assets (volume gauge art) are generated from artwork of
  unclear provenance. Replace the bitmap-based volume gauge scene in
  `firmware/dial/src/scenes.cpp` with an LVGL-procedural rendering (`lv_arc` /
  `lv_meter` + labels) so the firmware ships zero third-party image assets.
  Keep the same scene interface (state in / scene shown) so callers don't change.
- Delete the `ui_img_*.c` files and their declarations; update
  `firmware/THIRD_PARTY.md` and `LICENSE-third-party.md`.
- This changes a hardware-validated scene: mark the PR
  **needs-on-device-validation** and describe exactly what to look at after
  flashing (gauge sweep matches volume 0–100, no tearing, boot scene OK).
  Compile-check as in PR 1.

## PR 3 — notices for code/models we redistribute

- `jasper/xvf/xvf_host.py`: vendored XVF3800 helper, `LICENSE-third-party.md`
  marks its MIT status "needs verification". Find the upstream repo
  (respeaker/XMOS lineage — the file header may say), confirm its LICENSE,
  add the upstream copyright + MIT notice as a header block in the file, and
  flip the LICENSE-third-party row from "needs verification" to verified with
  the upstream URL + commit. If upstream turns out NOT permissive, stop and
  flag — do not guess.
- DTLN models: upstream `breizhn/DTLN-aec` is MIT. The converted ONNX files are
  re-hosted on this repo's `dtln-models-v1` release. Add the upstream MIT
  license text + attribution to the repo (e.g.
  `jasper/aec_engines/DTLN_LICENSE` referenced from `dtln_models.py`'s
  registry docstring), update LICENSE-third-party.md, and attach the license
  text to the GitHub release: `gh release upload dtln-models-v1 <file>` (if
  the release lives in a different repo, note it for the maintainer instead).
- `jarvis_v2.onnx`: NOT redistributed (install-time download), but document its
  upstream terms in `jasper/wake_models.py`'s registry entry +
  LICENSE-third-party.md. If the community model has no stated license, flag
  prominently in the PR — the maintainer may switch the default wake model to
  the Apache-2.0 stock `hey_jarvis`. Don't switch it yourself.

## Acceptance

- `grep -ri "not cleared" firmware/ LICENSE-third-party.md` returns nothing.
- No `ui_img_*.c` or `CST816D.*` files remain.
- `ruff check .` clean; `pytest tests/test_docs_impact.py -q` (doc-map globs)
  still green; firmware compile attempted or explicitly deferred.
