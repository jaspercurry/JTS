#!/usr/bin/env python3
"""Concatenate the individual table files into the final 08-census.md report,
with a methodology header and closing notes.
"""
from pathlib import Path

S = Path(__file__).parent
OUT = Path("/tmp/claude-0/-home-user-JTS/cf938fc0-997a-5915-a0d9-0d3bfa95c9c0/scratchpad/recon/08-census.md")

HEADER = """# Tuning-scope census (mechanical, HEAD)

Repo: /home/user/JTS. Branch `claude/busy-goodall-mz0gvv`, rebased on
origin/main 2026-09-02 (see BRIEF.md). This is a **purely mechanical**
census — every number below comes from a small re-runnable Python script
(ast + tokenize, no judgment calls beyond the classification rules stated in
each script's docstring). Scripts live in `scratchpad/recon/census/` next to
this report; re-run any of them to reproduce a table.

## Scope definition and file counts

Scope = jasper/active_speaker/, jasper/audio_measurement/, jasper/correction/,
jasper/attribution/, jasper/calibration_agent/, jasper/web/correction_*.py +
active_speaker_flow.py + balance_*.py, the 25 tuning CLIs listed in
BRIEF.md under jasper/cli/, and experiments/usb-turntable/. Enumerated by
`census/scope_files.py` (deterministic glob, no exclusions).

```
python3 census/scope_files.py        # scope .py files, repo-relative, one per line
python3 census/scope_files.py tests  # scope's test files (see Table 10 methodology)
```

**316 Python files** in scope, by package:

| package | files |
|---|---:|
| jasper/active_speaker | 172 |
| jasper/audio_measurement | 38 |
| jasper/correction | 31 |
| jasper/cli (25 named tuning CLIs) | 25 |
| jasper/web (correction_*/active_speaker_flow/balance_*) | 19 |
| jasper/calibration_agent | 13 |
| experiments/usb-turntable | 10 |
| jasper/attribution | 8 |
| **TOTAL** | **316** |

This matches BRIEF.md's prior-analysis file/line counts closely (active_speaker
172 files/168k lines here vs. 167,841; audio_measurement 32k vs. 31,943;
correction 17k vs. 16,883; attribution 2.7k vs. 2,688) — the two analyses
agree on scope boundaries.

All 316 files parsed cleanly with `ast.parse` (zero syntax errors) at HEAD.

---

"""

FOOTER = """
---

## Cross-check against BRIEF.md's prior-analysis numbers

The prior analysis (a few days old) and this fresh mechanical census agree
closely, which cross-validates both:

| claim | prior analysis | this census |
|---|---:|---:|
| `_text` re-rolls | 11 | 11 (Table 4) |
| `_mapping` re-rolls | 8 | 8 (Table 4) |
| sha256 helpers | 15 (6 signatures) | 13 (`_sha256`×10 + `_sha256_fd`/`_sha256_file`/`_sha256_text`) |
| `_refuse` def files | 22 files | 22 defs = 13×`_refuse`+9×`_refused` across 22 files (Table 5a0) |
| `_gate` def files | 7 | 8 |
| `_issue` def files | 6 | 8 |
| `_blocked` def files | 5 | 5 (exact) |
| `#NNNN` issue citations | 1,763 | 1,854 (Table 7a) |
| `ADR-NNNN` citations | 132 | 140 (Table 7b) |
| to_dict/from_dict-ish methods | ~297 | 277 (204 to_dict + 49 from_mapping + 23 from_dict + 1 to_json) |
| to_dict vs from_dict | 190 vs 16 | 204 vs 23 |
| test functions | 10,770 | 10,667 (Table 10) |
| parametrized % | 9.5% | 10.2% (Table 10) |

Small deltas are expected (a few days of merged PRs, and this census's
patterns are defined verbatim from the task brief rather than the prior
analysis's exact wordlist) — the agreement is close enough that both counts
should be trusted as directionally accurate.

## Additional boundary observation (outside the three required Table 9 checks)

Table 9's three specified checks (jasper.web from the four truth-layer dirs;
jasper.active_speaker/jasper.correction from audio_measurement/; crossover_v2
from correction/) all returned **zero** violations — the layering rule holds
for those exact edges at HEAD.

Grepping more broadly turned up two lazy (function-body) imports **from
jasper/attribution/ into jasper.active_speaker** — not one of the three
specified checks, so not counted in Table 9, but worth flagging since
REFACTOR-TUNING's target architecture has the truth layer with "no upward
import" and attribution reads out of active_speaker's evidence store:

- `jasper/attribution/promotion.py:501` — `from jasper.active_speaker.crossover_v2.intervention import (...)`
- `jasper/attribution/storage.py:106` — `from jasper.active_speaker.commissioning_evidence_store import EVIDENCE_ROOT`

## Reproduction

```
cd /home/user/JTS
S=scratchpad/recon/census   # or wherever this directory was copied
python3 $S/scope_files.py > $S/scope_files.txt
python3 $S/scope_files.py tests > $S/scope_tests.txt
python3 $S/metrics.py $S/scope_files.txt > $S/metrics.json
python3 $S/report_tables.py > $S/tables_1_2.md      # Tables 1, 2
python3 $S/big_defs.py $S/scope_files.txt           # Table 3
python3 $S/dup_helpers.py $S/scope_files.txt        # Table 4
python3 $S/refusal_census.py $S/scope_files.txt     # Table 5
python3 $S/serialization_census.py $S/scope_files.txt  # Table 6
python3 $S/citation_census.py $S/scope_files.txt    # Table 7
python3 $S/all_census.py $S/scope_files.txt         # Table 8 (needs ripgrep)
python3 $S/boundary_check.py $S/scope_files.txt     # Table 9
python3 $S/test_census.py $S/scope_tests.txt        # Table 10
```

No file was edited; this was read-only recon.
"""


def main():
    parts = [HEADER]
    parts.append("## 1. Per-file line census\n\n")
    parts.append((S / "tables_1_2.md").read_text())
    parts.append("\n\n## 3. Functions > 150 lines, classes > 800 lines\n\n")
    parts.append((S / "table_3.md").read_text())
    parts.append("\n\n## 4. Duplicate-helper census\n\n")
    parts.append((S / "table_4.md").read_text())
    parts.append("\n\n## 5. Refusal vocabulary census\n\n")
    parts.append((S / "table_5.md").read_text())
    parts.append("\n\n## 6. Serialization census\n\n")
    parts.append((S / "table_6.md").read_text())
    parts.append("\n\n## 7. Citation and stale-language census\n\n")
    parts.append((S / "table_7.md").read_text())
    parts.append("\n\n## 8. `__all__` census\n\n")
    parts.append((S / "table_8.md").read_text())
    parts.append("\n\n## 9. Import-graph boundary check\n\n")
    parts.append((S / "table_9.md").read_text())
    parts.append("\n\n## 10. Test census\n\n")
    parts.append((S / "table_10.md").read_text())
    parts.append(FOOTER)
    OUT.write_text("".join(parts))
    print(f"wrote {OUT} ({sum(len(p) for p in parts)} bytes)")


if __name__ == "__main__":
    main()
