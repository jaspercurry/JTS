# ADR-0413: One resolver: the door resolves every run request

- **Date:** 2026-10-02
- **Status:** Accepted. Supersedes in part
  [ADR-0405](0405-every-level-probe-starts-at-minus-60-dbfs-at-the-output.md) §2,
  [ADR-0408](0408-over-a-timing-take-each-candidate-graph-probes-its-own-graph.md) §3,
  [ADR-0370](0370-each-run-purpose-declares-what-it-plays-and-a-bass-run-plays-with-room-off.md) §5,
  [ADR-0385](0385-the-program-charge-is-the-emitted-graphs-peak-with-one-margin.md) §5 and
  [ADR-0389](0389-the-jasper-round-action-verbs-answer-through-the-envelope.md) §4.
- **Context:** The page posted a run request, and the session door resolved it. `jasper-round run`
  resolved the plan itself and posted the plan document, which the door read back. So two processes
  resolved one run, and a plan wire format stayed alive. A hand-written plan (`--plan`) could also
  state a template ladder that skipped a run's probe, against ADR-0405. This is the deletion pass on
  [#5925](https://github.com/jaspercurry/JTS/issues/5925) (scout finding F2, item 2b).
- **Decision:**
  1. **One resolver.** `jasper-round run` and `trial` post the run as a request, keyed by the flags'
     names, as the page does. The session door resolves, admits and stages it, and answers with the
     staged run's subject, parameters and preflight, which the run answer prints. `--dry-run`
     resolves the plan with the same code on this speaker's facts and posts nothing. Before it posts,
     the CLI reads only the request's shape and its mover, because an arm run needs `--wait`.
  2. **What goes.** The `--plan` flag, the door's plan-document branch and the plan wire format
     (`AngleCaptureRequest.from_mapping` with its schema version, kind and two refusal codes,
     `MeasureSpec.from_mapping` and `_from_json`, `LevelPolicy.from_mapping`), and the CLI's
     arm-fact pre-refusal: the door reads the arm and the attestation. A request states no template,
     so no plan can state a ladder that skips a probe.
  3. **The room-off rise goes**, with its facts and the reads that fed only it. A request plays one
     preset, so its stops have one purpose. Only a bass run's takes clear the room layer, and no bass
     preset takes a timing take, so a bass run's probe clears it too. Only hand-staged plans fed it.
- **Hearing:** a scratch proof resolved 2,174 request cases (every preset, layout, candidate set,
  level, mover and repeats, plus driver and custom poses, each with a readable and an unreadable room
  layer). At main, the old CLI's posted plan equals the request path's in all 1,652 cases that
  resolve, margins included; on this branch the request path equals main's in all 2,174. The rise was
  0.0 in every case. The 85 dB stop, `volume_limit`, the graph doors, the clamp and the driver caps
  do not change.
- **Consequences:** A run's refusals after its shape and layout come from the door, with the same
  exit code, code and next action. `plan.json` drops `artifact_schema_version` and `kind`, so each
  walk's `request_fingerprint` moves once. The door refuses a session body with any key but `request`
  and `attest_rig_clear`, and its answer adds `staged`. The applied rear no longer reads unknown when
  the room charge cannot be computed. Rejected: keeping the CLI's local resolution for its answer, which
  could then disagree with what the door staged.
