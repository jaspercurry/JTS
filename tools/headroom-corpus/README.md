# Headroom corpus harness (#5909)

An investigation tool, not product code. It is kept on this side branch only.

For every baseline graph that the test suite emits, it records two charges:
- `old_db`: today's emitted `active_baseline_headroom`.
- `new_db`: the one function, `headroom_charge_db(program_peak(graph)) + max(0, output_trim)`.

Run it at the repo root:

    rm -f /tmp/headroom-corpus.jsonl
    PYTHONPATH=$PWD:$PWD/tools/headroom-corpus HEADROOM_PROBE_OUT=/tmp/headroom-corpus.jsonl \
      .venv/bin/python -m pytest -p headroom_probe_plugin -q -n 8 $(cat tools/headroom-corpus/files.txt)
    .venv/bin/python tools/headroom-corpus/summarize.py /tmp/headroom-corpus.jsonl

`-n 8` needs pytest-xdist. Leave it out to run serially.

Result at 45e2b1417:
- 1,762 emits and 479 distinct graphs; 74 graphs have a charge.
- 25 charges are unchanged, 32 are lower (by 0.01 to 3.92 dB) and 17 are higher (by 0.69 to 1.00 dB).
- The highest charged peak of today's graphs under the one function is 0.0001 dB.

Scenario scripts (run them with the same PYTHONPATH):
- `case_oneway.py`: the one-way trims from #5909 (5.0/5.0/5.0 dB today, 5.0/3.0/0.0 dB with the one function).
- `cases.py`: the incident fixture, room cuts and boosts, and the cardioid seeds.

Use in #5909:
- H1's verifier must accept every graph (charged peak at most 1e-3 dB).
- H3's emitter must reproduce `new_db` for each graph to within 0.01 dB.
- The plugin imports `_branch_context` and `linearization_headroom_db`. H3 deletes them, so drop those two columns after H3.
