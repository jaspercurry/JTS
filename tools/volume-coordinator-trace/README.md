# volume_coordinator split: byte-identity harness (R-0VC, #4806)

This branch is never merged. It keeps the trace harness that proves each
`volume_coordinator.py` split PR changes no behaviour. Every later split PR
(4 to 11) runs it against main and against its head.

Run it from a neutral directory against a checkout:

    cd "$(mktemp -d)"
    PYTHONPATH=<checkout> <venv>/bin/python <this dir>/volume_coordinator_trace.py \
        --expect-root <checkout> [--dump FILE] [--names FILE]

It prints `digest=<sha256>`. A split PR must give the same digest as main.
Logger names are not in the digest; `--names` lists them so a move can
state each rename.

Digest at main `04664da02` (after #5895 PR 7 cut the tokenless mute latch),
with every latch seed tokened and one scenario that seeds a tokenless latch:
`6846affeebe098476d26ffb0f0c9c2a7bef76bde45825e97fa91ccd9c49162a9`.
It changes when main changes the volume path; re-take it at the new base.

To check that the harness sees a change, plant one bug at a time:

    <venv>/bin/python plant.py <checkout> volume_coordinator_trace.py plants_base.json

Each plant is `[file, old, new]`, and every plant must print `CHANGED`.
`plants_pr2.json` and `plants_pr3.json` plant into the code that PR 2 and
PR 3 moved.
