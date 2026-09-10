# ADR-0277: The seat cloud adds eleven positions without renaming saved cubes

- **Date:** 2026-09-09
- **Status:** Accepted. Amends [ADR-0260](0260-poses-are-flexible-and-categorized-and-bass-extension-has-no-nearfield-rung.md)
  decision 2, the default seat program.
- **Context:** The owner chose an eleven-position cloud around the listening
  position. Existing rounds already name `seat/cube` and retain its seven
  ordered offsets. Changing that program in place would make one saved name
  refer to two different walks.
- **Decision:** Add `seat/cloud` to the existing measurement-program registry.
  Its first nine positions are a three-by-three grid at ear height, walked
  front to back and left to right within each row. The last two positions are
  above and below the head centre. Every spacing is 0.30 m; offsets remain
  `(right, forward, up)`. The registry owns the ordered poses, and the existing
  request and prompt code derives the walk and counts from them. An omitted
  `jasper-angle-capture --size` selects `cloud` for `seat`; other program
  defaults remain unchanged. Explicit `seat/cube` and `seat/express` keep their
  names, coordinates and order. No stored round is relabelled.
- **Consequences:** The new mono program has eleven placements and eleven
  summed captures per candidate. This change does not add separate left/right
  capture or prescribe twenty-two captures. Stereo capture needs its own
  output selection and evidence. The cloud is a useful default, not a required
  pose count or a claim that eleven positions are optimal in every room.
  Retakes and other supported pose programs remain available.
