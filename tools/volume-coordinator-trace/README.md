# volume_coordinator split: byte-identity harness (R-0VC, #4806)

This branch is never merged. It keeps the trace harness that proves each
`volume_coordinator.py` split PR changes no behaviour. Every later split PR
(4 to 7, the hearing tier) runs it against main and against its head.

Run it from a neutral directory against a checkout:

    cd "$(mktemp -d)"
    PYTHONPATH=<checkout> <venv>/bin/python <this dir>/volume_coordinator_trace.py \
        --expect-root <checkout> [--dump FILE] [--names FILE]

It prints `digest=<sha256>`. A split PR must give the same digest as main.
Logger names are not in the digest; `--names` lists them so a move can
state each rename.

Digest at main `c476da5d2` and at PR 1a, 1b, 2 and 3 (#5902, #5904, #5905,
#5906): `1cf990c218b0ea9f35bb456a25195c728963d94180320b1fe56ff09dc973d5b1`.
It changes when main changes the volume path; re-take it at the new base.

To check that the harness sees a change, plant one bug at a time:

    <venv>/bin/python plant.py <checkout> volume_coordinator_trace.py plants_base.json

Each plant is `[file, old, new]`, and every plant must print `CHANGED`.
`plants_pr2.json` and `plants_pr3.json` plant into the code that PR 2 and
PR 3 moved.
