# Bottom-up sizing brief

Read /tmp/claude-0/-home-user-JTS/cf938fc0-997a-5915-a0d9-0d3bfa95c9c0/scratchpad/recon/BRIEF.md
for repo context, then this. Read-only. Do not edit the repo.

## The owner's operating model (verbatim spirit — size against THIS)

The user SSHes into their Pi and asks Claude to orchestrate making the speaker
sound better. As part of that the user fills in basic crossover configuration in
the web UI at jts.local (the /sound/ wizard: drivers, topology, protection).
The agent then has a toolbox of DISCRETE TOOLS (CLIs). One is `measure`, whose
mover is human or turntable; if there is no turntable it is human, and the
agent hands the user a URL where they walk through the measurement flow (the
web measurement page). The rest of the tools analyze banked evidence,
recommend, stage and apply candidates. There is documentation on each tool,
and one overall methodology document.

So the whole tuning side of the house should be: (1) a set of discrete tools,
(2) their docs, (3) the methodology, (4) the web component — the basic config
wizard and the human-mover measurement page. Things may have no direct code
caller and still be live because the LLM invokes them from the shell.

## The question

The top-down plan estimates the tuning scope lands at ~210k product lines
after cleanup (from 263k). The owner is shocked it is still that large and
wants an independent BOTTOM-UP estimate: enumerate the tools, size each,
explain why each is that big, find ghost tools, size the web component and
the docs, and derive from first principles how many lines each SHOULD be for
a clean 80/20 implementation. Then compare to the top-down number and explain
the delta. Be concrete and honest; a number with a breakdown beats a
narrative.

Reachability facts you can build on (script:
scratchpad/bottomup/reach.py, output in scratchpad/bottomup/reach.txt):
- 18 tools are on the LLM's generated tool menu (scripts/generate-tuning-tool-menu.py
  TUNING_TOOL_MODULES). 6 more tuning binaries are shipped but not on the menu.
- jasper-voice (a non-tuning daemon) transitively imports ~95k lines of the
  tuning scope — so a large substrate is shared with the product runtime.
- Only 419 lines of the scope are reachable from no entry point at all; the
  "dead code" is inside files, not whole modules.
- Transitive closures are near-total (jasper-measure reaches 230k of 261k), so
  closure size is NOT a measure of what a tool needs. Use judgment: read the
  tool, trace what it actually calls one or two levels down, and size that.

## Report format

For each unit you are assigned, a table row: name · what it does in one line ·
current lines (CLI file + engine modules it is the primary consumer of) ·
first-principles lines for a clean implementation (state your assumptions:
what stays shared substrate, what is prose, what is duplicate, what is
legitimate complexity) · the top reason for the gap. Then a bottom-up total
for your area with a breakdown: legit / prose / duplication / dead /
over-abstraction / speculative-or-parked. Then the 3 biggest single deltas
with file:line evidence. ≤ 300 lines. Return a ≤250-word summary with your
number.
