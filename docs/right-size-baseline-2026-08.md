# Right-size baseline — 2026-08

> **Campaign artifact, deleted with the campaign** — same lifecycle as
> [REFACTOR-2026-08.md](REFACTOR-2026-08.md) (wave 0.5).
> Verbatim output of `bash scripts/right-size-report.sh` computed at
> `810b0cd899a35f30377eca8eb3d03fa4a5fe93d2`; check that commit out to
> reproduce these numbers exactly.

```
JTS right-size report
commit: 810b0cd899a35f30377eca8eb3d03fa4a5fe93d2

zone              files      lines       code    py_code      prose prose_ratio
-------------------------------------------------------------------------------
tuning-product      251    242,725    145,075    144,943     74,976       0.517
tuning-tests        243    250,983    161,863    161,863     51,055       0.315
product             416    213,176    141,330    140,666     49,791       0.354
tests               763    378,336    266,928    224,624     53,088       0.236
rust                 68     68,164     64,020          0          0           -
c                     8      8,212      7,649          0          0           -
deploy              157     27,403     25,568        103        100       0.971
scripts             102     30,511     24,834     15,456      2,402       0.155
web-assets          103     41,887     39,633          0          0           -
docs                225    139,825    122,262         59         15       0.254
other                80     19,694     16,908      3,160        581       0.184
-------------------------------------------------------------------------------
TOTAL             2,416  1,420,916  1,016,070    690,874    232,008       0.336

legend: lines = code + prose + blank, over git-tracked UTF-8 text files.
        prose = Python comments + standalone docstrings, blanks excluded.
        code  = every other non-blank line; py_code is its Python subset.
        prose_ratio = prose / py_code.

binary or non-UTF-8 files (counted, lines not counted): 4
unreadable paths: 0
Python files tokenize refused (counted as code): 0

HEADLINE METRICS
                                                 this report       audit §1
  total tracked lines                              1,420,916     ~1,420,000
  test lines (tests + tuning-tests)                  629,319       ~617,000
  product lines (all of jasper/)                     455,901       ~490,000
  jasper/ code lines                                 285,609        274,456
  jasper/ prose lines                                124,767        135,886
  jasper/ prose ratio                                  0.437           0.50

TEST-TO-PRODUCT RATIOS (lines)
  whole repo                                           1.380
  tuning program (tuning-tests/tuning-product)         1.034
  platform (tests/product)                             1.775

DEAD CODE
---------
vulture: RAN (--min-confidence 90 over jasper/)
    findings: 12
    heaviest files:
         3  jasper/bluetooth/agent.py
         2  jasper/cli/aec_bridge.py
         2  jasper/output_topology.py
         1  jasper/audio_io.py
         1  jasper/bluetooth/handlers/base.py
         1  jasper/camilla.py
         1  jasper/voice_daemon.py
         1  jasper/wake_fusion.py

cargo: RAN (cargo check, dead-code lint family per crate)
    jasper-clock           dead-code: 0     all warnings: 0
    jasper-dual-dac-lab    dead-code: 0     all warnings: 0
    jasper-env             dead-code: 0     all warnings: 0
    jasper-fanin           dead-code: 0     all warnings: 0
    jasper-host-clock      dead-code: 0     all warnings: 0
    jasper-outputd         dead-code: 0     all warnings: 0
    jasper-resampler       dead-code: 0     all warnings: 0
    jasper-ring            dead-code: 0     all warnings: 0
    jasper-tts-protocol    dead-code: 0     all warnings: 0

javascript: SKIPPED (no vendored analyzer; adding a node dependency for
            knip is out of scope for a report script)
```

## Reading the deltas against the audit

The corpus agrees: 1,420,916 tracked lines reproduces
[DEEP-AUDIT-2026-08-25.md](DEEP-AUDIT-2026-08-25.md) §1's 1.42M, and
455,901 lines in `jasper/` reproduces its "455K" (§1 fact 1). The "~490K of
product code" in §1 fact 3 is the same paragraph's looser figure and does not
reconcile with either measurement; treat 455,901 as the product baseline.

The `jasper/` prose split differs by accounting, not by corpus: this report's
non-blank Python universe (285,609 + 124,767 = 410,376) lands within 34 lines
of the audit's (274,456 + 135,886 = 410,342), so both instruments see the same
lines and disagree only on where the prose/code boundary falls for ~11.1K of
them. This report's boundary is the one in the legend above and is
reproducible from the script; measured candidates for the difference are 1,393
lines carrying both code and a trailing comment and 3,981 rows spanned by
non-docstring multi-line strings (both counted as code here), plus 9,896 blank
lines inside docstring spans (counted as blank here). Deltas should be tracked
against 0.437, not against 0.50.

Zone membership is the plan's closed list, not a name regex — so
`jasper/web/active_speaker_flow.py` (90 lines) and
`jasper/cli/bass_extension_bench.py` (225 lines) sit in `product`. If the
tuning program wants them, amend the ownership section and re-run.
