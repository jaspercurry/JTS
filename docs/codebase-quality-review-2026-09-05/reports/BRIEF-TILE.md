# Phase 1 tile read — rubric (read BRIEF.md first)

You own one tile: a file list at the path named in your task (format: `<LOC>  <path>`). Open EVERY file in it.
For files > 1,500 LOC you may read at structural altitude: imports, all class/def signatures, module docstring,
and the full body of every function > 40 LOC plus anything touching persistence, subprocess, threads, sockets,
or exceptions; say in Coverage which files you read that way. Never sample silently.

Judge each file, and the tile as a whole, against these nine lenses:
1. MANDATE — one line: what is this package/module FOR? Which files do not serve that mandate (belong in another
   package, should merge with a sibling, or should die)? Does the file hierarchy tell a newcomer where things live?
2. BOUNDARIES — what does the tile import and who imports it (grep). Flag: private-name reaching (`_foo` imported
   from another module), function-local imports (why each exists — cycle dodge, import cost, or habit), import cycles,
   reach-through (importing X to get X's re-export of Y), modules that talk to systemd/ALSA/CamillaDSP/files directly
   when a seam exists (restart_broker, atomic_io, env_load, sound/, camilla.py, log_event).
3. SINGLE SOURCE OF TRUTH — a fact, constant, path, vocabulary, parser, or formatter spelled in more than one place
   (inside the tile, or vs. a named sibling you can point at).
4. DEAD & ORPHANED — defs with no caller (verify per BRIEF before claiming), dead branches, compat shims whose
   migration already happened, params always passed the same value, flags nobody sets.
5. COMPLEXITY — god functions/classes/modules; name the concrete split that would help, or say "leave it" when a
   split would be astronaut engineering. Count functions > 100 LOC.
6. RESILIENCE — unbounded loops/queues/retries; subprocess or network calls without a timeout; `except Exception:
   pass`-style swallowing; restart loops; resources that can vanish (USB device, socket, peer) with no self-recovery;
   state that can go stale with no invalidation; races between writer and reader of a shared file.
7. OBSERVABILITY — are state transitions and failures emitted as stable `event=` logs (jasper.log_event) and surfaced
   to /state, doctor, or an audible cue, or are they swallowed? Any journal spam (per-tick logging)?
8. PROSE — comment + docstring lines that narrate history/process rather than state a non-derivable constraint.
   Give the count for the tile (rough is fine) and the 3 worst examples with file:line.
9. TESTS — for this tile's subjects only: are there tests pinning private names, source text, or log prose?
   Is there a real failure mode with NO behavior pin? (Look under tests/ by module name; don't read the whole suite.)

Deliver, in this order:
A. Tile mandate (1–2 lines) and a verdict on whether the tile is one coherent system or several tangled ones.
B. Per-file table: `file | LOC | verdict | one-line reason` with verdict in {earns-keep, merge-into:<X>, move-to:<X>,
   delete, split, shrink-prose, rewrite-smaller}. Every file in the tile appears once.
C. Top findings, ranked by impact, max 15: `severity | file:line(s) | what | evidence | cleanest fix`.
   Severities: Blocker / Should-fix / Nit. Include Earns-its-keep notes for anything you tried to cut and could not.
D. "If I could do one thing to this tile": ≤ 3 lines.
E. Cross-tile pointers: things you saw that belong to another package (file:line + which package), so the
   synthesis can dedupe.
F. Coverage: files read fully / structurally / skipped (with reason). Approximate LOC you actually read.
Keep the report under ~250 lines. Tables over prose. No process narration.
