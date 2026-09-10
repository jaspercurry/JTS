# Installation context guides bass trials

Date: 2026-09-10

## Decision

Microphone trials establish usable bass extension. Optional installed-hardware
facts help choose those trials, with unknowns and assumptions visible.

The existing design draft owns operator-entered installation facts under each
manual driver. Its existing save path remains the only writer. The installation
module owns the field vocabulary, validation and read-time estimates; the
component form and measurement evidence packet consume it. Research imports
cannot replace these operator facts.

An ideal amplifier voltage calculation is an upper bound, not a clean-output
rating or a measured amount of spare headroom. Cabinet and radiator facts do
not establish excursion limits by themselves. Estimates never write filters,
raise driver caps, or authorize playback. Native DSP comparisons and acoustic
measurements remain separate evidence about the requested and delivered bass.
