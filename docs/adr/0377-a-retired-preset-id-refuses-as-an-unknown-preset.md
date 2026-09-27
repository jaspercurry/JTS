# ADR-0377: A retired preset id is not kept; it refuses as an unknown preset

- **Date:** 2026-09-27
- **Status:** Accepted. Supersedes (partial) [ADR-0366](0366-one-pose-model-a-level-found-at-the-pose-and-a-band-stated-from-it.md)
  §6: "banked rounds that carry one stay readable", "Its layout stays a named layout", and the
  consequence "A retired id stays readable in banked rounds through one frozen table". Carries the
  owner's no-backward-support ruling ([#2902](https://github.com/jaspercurry/JTS/issues/2902)).
- **Context:** ADR-0366 §6 folded 28 registry rows into presets and kept every retired id readable.
  `RETIRED_PROGRAMS` mapped each retired id to its purpose. A new run naming one refused by name
  (`measurement_program_retired`) and named its replacement, the measure page opened a retired link's
  replacement, and a frozen test pinned every id banked before the fold. A retired row's layout stayed
  a named layout that no preset offered. On 2026-09-27 the owner ruled that old measurements need no
  reader: "We don't care about old speaker configs or old measurements; we're still in development"
  (#2902).
- **Decision:**
  1. The retired-id table and its refusal go. A run, a link or a banked round that names an id the
     registry does not hold refuses as an unknown preset (`UnknownProgramError`), as any unknown id does.
  2. A preset id resolves only through the registry. A bare program name reads as its default
     preset's purpose, and a bare purpose as itself. An id such as `rear/pair_mark`, or a `/custom` id
     that runs banked before layouts had their own field, no longer reads as `rear` because its prefix
     names a purpose.
  3. A layout that no preset offers is deleted.
- **Consequences:**
  - A round banked under a retired id (`baseline/express`, `seat/cube`, `nearfield/woofer`) stops
    loading in the views that resolve its purpose. Scans of banked rounds skip it
    (`packet_purposes`). The owner accepted this on #2902.
  - `nearfield_rear`, `nearfield_cardioid` and `drivers_cardioid` go. `--driver` narrows
    `nearfield/each` or `drivers/each` to one output instead.
  - Rejected: keeping the table as data with no reader. It would be a tolerant reader in waiting.
