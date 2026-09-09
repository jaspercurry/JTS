# ADR index

One row per decision record; the file is the record, this is the map. Add a row when you add an
ADR (a test pins that every `docs/adr/NNNN-*.md` appears here). Numbers are reserved on
issue #4405 before a PR is opened. Status is `accepted`, `superseded by NNNN` (the record's own
Status line says so), or `amended by NNNN` (a later ADR narrows one clause and says "supersedes
(partial)").

## Operating model & docs

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-operating-model-reset.md) | Operating-model reset — hobbyist proportionality replaces production doctrine | accepted |
| [0101](0101-proven-once-disclose-on-change.md) | Proven once, disclose on change — validity-proof gates stop parking working systems | amended by 0185 |
| [0199](0199-the-handoff-doc-corpus-is-deleted.md) | The HANDOFF doc corpus is deleted | accepted |
| [0226](0226-constrained-hardware-doctrine-push-dont-pull-no-spawns-one-interpreter.md) | Constrained-hardware doctrine — push don't pull, no spawns, one interpreter | accepted |
| [0227](0227-owner-rulings-the-prose-pass-surfaced.md) | Owner rulings the tuning prose pass surfaced with no ADR home | accepted |
| [0228](0228-rulings-carried-out-of-refactor-tuning-on-its-retirement.md) | Rulings carried out of REFACTOR-TUNING-2026-08 on its retirement | amended by 0230 |
| [0229](0229-the-bass-extension-plan-is-exempt-from-the-handoff-deletion.md) | The bass-extension plan is exempt from the HANDOFF deletion | accepted |
| [0231](0231-four-rulings-that-lived-only-in-code-comments.md) | Four rulings that lived only in code comments are recorded here, and one boundary note | §5 superseded by 0259 |

## Deploy, install & system

| ADR | Decision | Status |
|---|---|---|
| [0105](0105-each-speaker-derives-its-own-usb-subnet.md) | Each speaker derives its own USB /30 from its CPU serial | accepted |
| [0145](0145-remote-updates-stay-a-laptop-deploy.md) | Remote updates stay a laptop deploy | accepted |
| [0163](0163-installer-builds-run-the-inverse-of-the-audio-daemon-memory-policy.md) | Installer builds run the inverse of the audio-daemon memory policy | accepted |
| [0164](0164-a-pi-image-is-a-cached-versioned-input-to-the-installer-not-a-second-installer.md) | A Pi image is a cached, versioned input to the installer, not a second installer | accepted |
| [0172](0172-full-a-b-install-generations-stay-deferred.md) | Full A-B install generations stay deferred | accepted |
| [0173](0173-post-deploy-health-is-surfaced-never-gating.md) | Post-deploy health is surfaced, never gating | accepted |
| [0174](0174-install-window-oom-kills-are-surfaced-not-gated.md) | Install-window OOM kills are surfaced, not gated | accepted |
| [0232](0232-studio-driver-stack-is-canonical-for-hifiberry-studio-silicon.md) | Studio driver stack is canonical for HiFiBerry Studio silicon | accepted |
| [0234](0234-detected-hardware-is-used-automatically.md) | Detected hardware is used automatically; only undetectable hardware gets a toggle | accepted |
| [0235](0235-attached-hardware-one-owner-per-fact-and-no-facts-in-shell.md) | Attached hardware has one owner per fact, and the shell holds no hardware facts | accepted |
| [0241](0241-the-install-runs-as-a-transient-unit-and-the-deploy-exits-on-its-status.md) | The install runs as a transient unit and the deploy exits on its status | accepted |
| [0242](0242-post-deploy-health-is-the-core-doctor-run-in-a-transient-unit.md) | Post-deploy health is the core doctor, run in a transient unit | accepted |
| [0248](0248-post-deploy-health-gates-the-deploy.md) | Post-deploy health gates the deploy | accepted |
| [0252](0252-the-python-tree-publishes-from-a-staging-path.md) | The Python tree publishes from a staging path | accepted |

## Audio path & output (ring/fanin/outputd/DAC)

| ADR | Decision | Status |
|---|---|---|
| [0100](0100-one-audio-transport.md) | One audio transport — the loopback route and its transition machinery are deleted | accepted |
| [0114](0114-a-jts-native-output-owner-not-pipewire.md) | A JTS-native output owner, not PipeWire — and not a fan-in content mirror | accepted |
| [0122](0122-an-endpoints-layer-a-crossover-runs-in-camilladsp-not-outputd.md) | An endpoint's Layer-A crossover runs in CamillaDSP, not in outputd | accepted |
| [0123](0123-an-active-leaders-crossover-runs-after-the-round-trip.md) | An active leader's crossover runs after the round-trip, in a second CamillaDSP | accepted |
| [0124](0124-one-rate-loop-and-the-summer-never-merges-with-the-reference-publisher.md) | One rate loop, and the summer never merges with the reference publisher | accepted |
| [0125](0125-leader-tts-and-the-follower-cue-inject-pre-crossover.md) | Leader TTS and the follower fail-closed cue inject pre-crossover | accepted |
| [0126](0126-a-subwoofer-crossover-executes-on-the-receiver.md) | A subwoofer's crossover executes on the receiver, on the one shared stereo stream | superseded by 0236 |
| [0141](0141-outputd-parks-out-of-band-rather-than-riding-its-restart-limit-to-a-reboot.md) | outputd parks out-of-band rather than riding its restart limit to a reboot | accepted |
| [0169](0169-the-outputd-ordering-guard-compares-recorded-instants-not-computed-ages.md) | the outputd ordering guard compares recorded instants, not computed ages | accepted |
| [0175](0175-a-failed-camilla-recovery-parks-the-core-graph-once.md) | a failed Camilla recovery parks the core graph once | amended by 0264 |
| [0178](0178-every-shape-the-ring-cannot-serve-parks-under-its-own-name.md) | Every shape the ring cannot serve parks under its own name | amended by 0187 |
| [0184](0184-a-resolvable-width-with-no-armed-endpoint-signals-rather-than-parks.md) | A resolvable width with no armed endpoint signals, it does not park | accepted |
| [0186](0186-the-endpoint-gate-stays.md) | The endpoint gate stays | superseded by 0262 |
| [0189](0189-an-armed-endpoint-under-no-active-modes-discloses-on-non-composite-sinks.md) | An armed endpoint under no active modes discloses, on non-composite sinks | accepted |
| [0220](0220-the-dac-content-marker-is-served-and-its-contradiction-parks.md) | The dac-content marker is served, and its contradiction parks | amended by 0262 |
| [0236](0236-independent-subwoofers-are-deleted-a-dac-channel-sub-stays.md) | Independent subwoofers are deleted; a subwoofer on a DAC channel stays the active-speaker crossover's concern | accepted |
| [0261](0261-the-grouping-and-dac-content-rings-are-128-frame-16-slot-s16-and-governed.md) | The grouping and dac-content rings are 128-frame, 16-slot, S16, and governed | accepted |
| [0262](0262-the-fifo-leg-and-the-snd-aloop-pairing-gate-retire-without-a-metal-run.md) | The FIFO leg and the snd-aloop pairing gate retire without a metal run | accepted |
| [0264](0264-the-camilla-recovery-is-evidence-and-one-bounded-restart.md) | The Camilla recovery is evidence and one bounded restart | accepted |

## Volume & hearing

| ADR | Decision | Status |
|---|---|---|
| [0004](0004-duck-release-algebra-and-reference.md) | The duck release algebra — `min(reference, current + own depth)`, and the reference is a reader | accepted |
| [0121](0121-preference-boosts-boost-room-boosts-are-compensated.md) | Preference boosts boost; room-correction boosts are headroom-compensated | accepted |
| [0176](0176-the-airplay-sender-slider-is-not-a-control-surface.md) | The AirPlay sender slider is not a control surface — AirPlay 2 took the back-channel away | accepted |
| [0177](0177-duck-ownership-is-asked-of-the-owner-never-inferred-from-a-db-gap.md) | Duck ownership is asked of the owner, never inferred from a dB gap | accepted |
| [0206](0206-the-airplay-sender-slider-is-an-inbound-control-surface.md) | The AirPlay sender slider is an inbound control surface — shairport's volume hook drives the master fader | accepted |
| [0211](0211-a-live-eq-edit-ducks-only-when-camilladsp-rebuilds.md) | A live EQ edit ducks only when CamillaDSP rebuilds | accepted |
| [0213](0213-the-reconciler-asks-the-dsp-writer-lock-before-it-corrects-the-fader.md) | The reconciler asks the DSP writer lock before it corrects the fader | accepted |

## Local sources & renderers

| ADR | Decision | Status |
|---|---|---|
| [0107](0107-usb-gadget-audio-has-one-capture-pipeline.md) | USB gadget audio has one capture pipeline, and no hidden fallback | accepted |
| [0108](0108-a-latency-claim-is-earned-by-a-measured-artifact.md) | A low-latency route claim is earned by a measured artifact, never by configuration | amended by 0185 |
| [0109](0109-the-combo-host-clock-servo-observes-resampler-correction.md) | The combo host-clock servo observes resampler correction, not gadget fill | amended by 0250 |
| [0118](0118-the-airplay-latency-offset-is-derived-never-hand-set.md) | The AirPlay backend latency offset is derived from the live chain, never hand-set | accepted |
| [0119](0119-dlna-is-the-phone-casting-surface.md) | DLNA/UPnP is the phone-casting surface; Google Cast is closed | accepted |
| [0147](0147-one-source-coordinator-with-three-appliers-no-lifecycle-daemon.md) | The local-source lifecycle is one coordinator with three appliers — no resident daemon, no plugin API | accepted |
| [0148](0148-every-source-unit-re-reads-canonical-intent-at-its-own-start-boundary.md) | Every source-owned unit re-reads canonical intent at its own start boundary; derived enablement is never household preference | superseded by 0221 |
| [0149](0149-a-blocking-unit-start-client-waits-past-pid-1s-legal-maximum.md) | A blocking unit-start client waits past PID 1's legal maximum, not a generic ceiling | accepted |
| [0150](0150-source-adapters-hint-the-mux-reconciler-decides.md) | Source adapters hint; the mux reconciler decides | accepted |
| [0151](0151-a-new-source-is-camilla-master-until-it-proves-an-observable-volume-surface.md) | A new source is Camilla-master until it proves an observable volume surface | accepted |
| [0185](0185-latency-is-monitored-and-adapted-never-certified.md) | Latency is monitored and adapted, never certified | accepted |
| [0191](0191-usb-transport-is-not-gated-on-derived-state.md) | USB transport is not gated on derived state | accepted |
| [0205](0205-the-airplay-offset-ledger-is-four-terms-not-three.md) | The AirPlay offset ledger is four terms, not three | amended by 0266 |
| [0221](0221-source-start-gates-are-marker-files-published-by-the-coordinator.md) | Source start gates are marker files published by the coordinator | accepted |
| [0250](0250-the-host-clock-dll-block-is-deleted-not-ticked.md) | The host-clock `dll` block is deleted, not ticked | accepted |
| [0254](0254-runtime-buffers-are-bounded-and-drop-and-count.md) | Runtime buffers are bounded and drop-and-count | amended by 0266 |
| [0266](0266-fan-in-publishes-only-evidence-that-has-a-reader.md) | Fan-in publishes only evidence that has a reader | accepted |

## Multiroom & grouping

| ADR | Decision | Status |
|---|---|---|
| [0110](0110-buy-the-sync-engine-one-stream-leader-bakes.md) | Buy the sync engine — one stereo stream, leader bakes, receivers pick a channel | accepted |
| [0111](0111-one-fixed-leader-no-election.md) | One fixed, config-declared leader per bond — no election | accepted |
| [0112](0112-assistant-audio-never-rides-the-synced-stream.md) | Assistant audio never rides the synced stream; a bonded member mixes its own, post-round-trip | accepted |
| [0113](0113-the-leader-self-loop-is-never-a-single-point-of-failure.md) | The leader's self-loop is never a single point of failure for its own music | accepted |
| [0218](0218-rate-adjust-follows-the-sink.md) | `enable_rate_adjust` follows the sink, not grouping membership | accepted |

## Voice loop & providers

| ADR | Decision | Status |
|---|---|---|
| [0115](0115-provider-truncation-is-driven-by-the-playout-ledger.md) | Provider truncation is driven by the local playout ledger | accepted |
| [0116](0116-smart-home-relays-through-has-conversation-api.md) | Smart-home control relays through Home Assistant's conversation API, not MCP | accepted |
| [0117](0117-consequential-actions-confirm-only-inside-the-taint-window.md) | A consequential Home Assistant action confirms only inside the untrusted-content window | accepted |
| [0155](0155-per-tool-conditional-rules-live-in-the-tool-description.md) | Per-tool conditional rules live in the tool's model-facing description, never in the system prompt | accepted |
| [0156](0156-the-gemini-system-instruction-token-ceiling-is-folklore.md) | The ~500-token Gemini system-instruction ceiling is folklore — trim for adherence and cost, never for resumption | accepted |
| [0157](0157-untrusted-tool-text-is-fenced-and-consequential-actions-are-confirmed.md) | Untrusted tool-result text is fenced and consequential actions are confirmed — two layers, neither sufficient alone | accepted |
| [0158](0158-per-provider-prompt-divergence-is-a-shared-base-plus-an-additive-delta.md) | Per-provider prompt divergence is a shared base plus an additive delta, never separate prompts | accepted |
| [0159](0159-a-provider-failure-never-falls-back-to-another-provider.md) | A provider failure never falls back to another provider — the speaker says it cannot reach the cloud and stays put | accepted |
| [0160](0160-the-model-catalog-is-curated-metadata-not-a-runtime-allow-list.md) | The model catalog is curated metadata, not a runtime allow-list | accepted |
| [0161](0161-an-unpriced-model-costs-zero-and-says-so.md) | An unpriced model costs zero and says so — a rate is never inferred | accepted |
| [0162](0162-the-pre-response-idle-anchor-stays-turn-open.md) | The pre-response idle anchor stays turn-open; a new endpointer derives its own bound instead | accepted |
| [0165](0165-the-active-voice-provider-lives-in-one-file-and-unconfigured-parks.md) | The active voice provider lives in exactly one file, has no default, and an unconfigured speaker parks | accepted |
| [0166](0166-a-resumption-handle-is-dropped-on-the-first-failure.md) | A session-resumption handle is dropped on the first failure of any kind | accepted |
| [0167](0167-each-transit-network-is-its-own-provider-module.md) | Each transit network is its own provider module | accepted |
| [0168](0168-voice-model-rates-are-entered-by-hand-never-fetched.md) | Voice-model rates are entered by hand, never fetched | accepted |
| [0215](0215-a-broken-cloud-connection-is-announced-once-and-only-when-a-human-must-act.md) | A broken cloud connection is announced once, and only when a human must act | accepted |
| [0238](0238-the-first-provider-connect-is-one-attempt-and-every-retry-is-the-supervisors.md) | The first provider connect is one attempt, and every retry is the supervisor's | accepted |

## Wake, mic & AEC

| ADR | Decision | Status |
|---|---|---|
| [0102](0102-mic-transport-is-udp-localhost.md) | The bridge→voice microphone transport is UDP localhost, not a second snd-aloop card | accepted |
| [0106](0106-a-verification-artifact-is-never-migrated-in-place.md) | A verification artifact's identity is never migrated in place — a changed edge re-measures | amended by 0190 |
| [0127](0127-wake-arbitration-is-hubless-and-costs-a-solo-speaker-nothing.md) | Wake arbitration is hubless, and costs a solo speaker nothing | accepted |
| [0128](0128-peering-fails-open-so-arbitration-can-never-silence-a-speaker.md) | Peering fails open, so arbitration can never silence a speaker | accepted |
| [0129](0129-wake-models-are-trained-per-leg-against-the-chain-they-run-on.md) | Wake models are trained per leg, against the chain that leg runs on | accepted |
| [0130](0130-nothing-gets-veto-power-upstream-of-the-wake-or-gate.md) | Nothing gets veto power upstream of the wake OR-gate | accepted |
| [0131](0131-the-wake-training-experiment-commits-its-revert-bars-in-advance.md) | The wake-training experiment commits its revert bars in advance | accepted |
| [0132](0132-the-wake-or-gate-fires-now-and-the-false-positive-cost-is-measured.md) | The wake OR-gate fires now; the false-positive cost is measured, not pre-empted | accepted |
| [0133](0133-wake-event-rows-are-permanent-and-the-audio-is-a-small-ring.md) | Wake-event rows are permanent; the wake-event audio is a small ring | accepted |
| [0134](0134-wake-labelling-is-post-hoc-never-real-time.md) | Wake labelling is post-hoc, never real-time | accepted |
| [0135](0135-corpus-quality-analysis-ranks-for-review-it-never-rejects-a-clip.md) | Corpus quality analysis ranks clips for review; it never rejects one | accepted |
| [0136](0136-waveform-fusion-must-beat-score-fusion-before-it-is-a-candidate.md) | Waveform fusion must beat score fusion before it is a candidate | accepted |
| [0137](0137-wake-training-data-prep-is-a-chain-of-hash-bound-stages-none-of-which-trains.md) | Wake-training data prep is a chain of hash-bound stages, none of which trains | accepted |
| [0138](0138-household-audio-leaves-the-house-only-on-explicit-consent.md) | Household audio leaves the house only on explicit consent | accepted |
| [0139](0139-the-voice-input-gate-is-one-reconciler-owned-negative-marker.md) | The voice-input gate is one reconciler-owned negative marker | accepted |
| [0140](0140-a-missing-microphone-degrades-aec-never-output.md) | A missing microphone degrades AEC, never output | accepted |
| [0142](0142-the-ble-connection-event-reservation-is-re-requested-never-watched.md) | The BLE connection-event reservation is re-requested, never watched | accepted |
| [0152](0152-local-silero-on-the-aec-stream-is-the-endpointer-server-vad-stays-off.md) | Local Silero on the AEC stream is the endpointer; provider server VAD stays off | accepted |
| [0153](0153-failure-cues-are-pre-rendered-and-content-addressed-never-streamed.md) | Failure cues are pre-rendered and content-addressed on disk, never streamed at play time | accepted |
| [0154](0154-reactive-cues-never-cool-down-proactive-cues-are-rate-limited.md) | Reactive cues never cool down; proactive cues are rate-limited | amended by 0215 |
| [0170](0170-a-selectable-audio-input-profile-owns-its-whole-wake-leg-set.md) | A selectable audio-input profile owns its whole wake-leg set | accepted |
| [0190](0190-chip-aec-identity-keys-only-physics.md) | Chip-AEC alignment identity compares only physics | amended by 0223 |
| [0217](0217-a-streambox-runs-the-assistant-only-while-a-mic-bearing-remote-is-paired.md) | A streambox runs the assistant only while a mic-bearing remote is paired | accepted |
| [0223](0223-a-moved-reference-queue-is-what-k-absorbs.md) | A moved reference queue is what K absorbs, not a staleness signal | accepted |
| [0224](0224-the-aec-bridge-starts-on-a-reconciler-published-ready-marker.md) | The AEC bridge starts on a reconciler-published ready marker | accepted |
| [0239](0239-the-voice-daemon-not-jasper-control-plays-the-mic-loss-cue-at-shutdown.md) | The voice daemon plays the mic-loss cue at shutdown; jasper-control has no player | amended by 0240 |
| [0240](0240-mic-absence-reason-is-a-code-vocabulary.md) | The voice-input-absent marker's `reason=` is a closed code vocabulary; `detail=` carries the prose | accepted |
| [0244](0244-the-server-vad-path-is-deleted-not-kept-as-a-knob.md) | The server-VAD path is deleted, not kept as a knob | accepted |
| [0246](0246-arbitration-is-the-whole-of-peering.md) | Arbitration is the whole of peering | accepted |

## Control plane, state & observability

| ADR | Decision | Status |
|---|---|---|
| [0103](0103-config-apply-restarts-clear-the-flap-counter.md) | A deliberate config-apply restart clears `NRestarts`, and that erasure is accepted | accepted |
| [0104](0104-per-daemon-memory-caps-stay-deferred.md) | Per-daemon memory caps and systemd-oomd stay deferred until a named trigger fires | accepted |
| [0143](0143-observability-has-three-planes-and-debug-verbosity-is-additive-only.md) | Observability has three planes, and debug verbosity is additive only | accepted |
| [0144](0144-diagnostics-leave-the-box-over-ssh-not-over-the-lan.md) | Diagnostics leave the box over SSH, not over the LAN | accepted |
| [0146](0146-userspace-liveness-is-two-software-layers-and-three-deferred-dials.md) | Userspace liveness is two software layers, and three deferred dials | accepted |
| [0225](0225-accessory-bridges-share-one-interpreter.md) | Accessory bridges share one interpreter | accepted |
| [0233](0233-one-reader-per-fact-two-surfaces-one-doctor.md) | One reader per fact, two surfaces, one doctor | accepted |
| [0243](0243-a-secret-is-replaced-whole-by-one-redactor-per-language.md) | A secret is replaced whole, by one redactor per language | accepted |
| [0245](0245-state-audio-graph-section-deleted.md) | `/state.audio_graph` section deleted | accepted |
| [0251](0251-jasper-control-parks-on-a-bind-failure.md) | jasper-control parks on a bind failure instead of rebooting the box | accepted |

## Web & UI

| ADR | Decision | Status |
|---|---|---|
| [0120](0120-one-management-frontend-gated-by-capability.md) | One management frontend for every install profile, gated by capability | accepted |
| [0171](0171-rarely-viewed-dashboard-probes-run-in-short-lived-child-processes.md) | Rarely-viewed dashboard probes run in short-lived child processes | accepted |
| [0187](0187-park-presentation-is-the-system-screen-only.md) | Park presentation is the system screen, not a banner | accepted |
| [0253](0253-web-ia-manifest-and-url-policy.md) | Web IA — manifest ownership, hub scope, and URL-move policy | accepted |

## Tuning & measurement

| ADR | Decision | Status |
|---|---|---|
| [0002](0002-measure-again-discriminator.md) | "Would measuring again plausibly fix it?" separates a capture defect from a description of the world | accepted |
| [0003](0003-prediction-gate-frame.md) | A gate's two terms must be the same instrument at the same position — the prediction gate's frame | accepted |
| [0005](0005-fader-bound-asymmetric-record-point.md) | A fader position tracked across a fallible write is a LOWER BOUND, recorded on an asymmetric point | accepted |
| [0006](0006-staged-walk-refuses-the-open.md) | A staged request the session cannot honour refuses the open — it never degrades to a different session | accepted |
| [0007](0007-refuse-dont-mislead-when-begins-are-gated.md) | Refuse, don't mislead — a gated session never prompts a pose its mover cannot reach | accepted |
| [0008](0008-every-begin-is-gated-no-release-order-coupling.md) | Every begin is gated, including the on-axis ones — and a gated shape may not introduce release-order coupling | accepted |
| [0009](0009-measurement-volume-hold-is-not-gated-on-observation.md) | The measurement-volume hold is the safety ledger's integrity, not forensics — it is never gated on a diagnostics flag | accepted |
| [0010](0010-candidate-build-commits-nothing.md) | A candidate build commits nothing — and the accountability gate lives outside the builder | accepted |
| [0011](0011-prescription-is-never-inherited.md) | A prescription is one round's explicit instruction — never inherited, and validated against its own round's corner | accepted |
| [0012](0012-design-axis-is-a-member-of-the-walk.md) | The design axis is a MEMBER of the post-apply walk, not just the anchor in front of it | accepted |
| [0013](0013-poses-are-absolute-and-the-actor-is-the-microphone.md) | Every prompted pose is ABSOLUTE, measured from the mark — and the actor is the microphone | accepted |
| [0014](0014-speculative-work-may-never-apply.md) | Speculative background work may never apply — and its failure is a non-event, never a capture failure | accepted |
| [0015](0015-only-an-accepted-verdict-grades-the-round.md) | Only an accepted verdict grades the round — a session ending on a terminal rejection writes no receipt | accepted |
| [0016](0016-reference-mark-is-an-identity-not-a-coordinate.md) | The reference mark is a stable identity, not a coordinate — and it has exactly one owner | accepted |
| [0017](0017-retention-keeps-the-raw-capture.md) | Position retention keeps the RAW capture, never a derived summary — and the fail-soft boundary sits at the caller | accepted |
| [0018](0018-bass-extension-stays-parked.md) | `jasper/bass_extension/` stays PARKED — neither wired up nor deleted | superseded by 0257 |
| [0019](0019-declared-metadata-gap-never-refuses-a-session.md) | A declared-metadata gap on an optional surface never refuses the session — it degrades to the default | accepted |
| [0179](0179-the-tuning-engines-seams-are-async-and-a-release-completes-before-cancellation-propagates.md) | The tuning engine's seams are async, and a release completes before cancellation propagates | accepted |
| [0180](0180-the-alignment-trust-floor-discloses-it-does-not-refuse.md) | The alignment trust floor discloses; it stopped refusing at the nanny burn-down | accepted |
| [0181](0181-the-ripple-disclosure-corpus-is-thirteen-captures-counted-once.md) | The ripple disclosure's corpus is thirteen captures, counted in one place | accepted |
| [0182](0182-the-verify-pilot-transfer-ceiling-rests-on-one-clean-session.md) | The VERIFY pilot-transfer ceiling rests on one clean multi-attempt session | accepted |
| [0183](0183-the-verify-repeat-floor-is-twice-a-measured-consecutive-pair-p95.md) | The VERIFY repeat floor is twice a measured consecutive-pair p95 | accepted |
| [0188](0188-wired-first-measurement-relay-parked.md) | Wired-first measurement; relay parked | amended by 0222 |
| [0192](0192-the-campaign-is-the-validation.md) | The campaign is the validation | accepted |
| [0193](0193-the-audition-door-is-a-runtime-only-swap.md) | The audition door is a runtime-only swap | accepted |
| [0194](0194-the-flat-spec-frame-and-its-ceiling.md) | The flat-spec reference is the low-mid band, and the graded ceiling follows the microphone | accepted |
| [0195](0195-a-rebuild-that-knows-less-is-not-a-supersede.md) | A rebuild that knows less is not a supersede | accepted |
| [0196](0196-the-commissioning-record-read-path-takes-no-lock.md) | The commissioning record's read path takes no lock, and says what it found | accepted |
| [0197](0197-the-commissioning-capture-stack-is-deleted.md) | The commissioning capture stack is deleted | accepted |
| [0198](0198-the-unwired-engine-verb-half-is-deleted.md) | The unwired engine verb half is deleted | accepted |
| [0200](0200-the-measurement-toolbox-is-microphone-only.md) | The measurement toolbox is microphone-only | accepted |
| [0201](0201-fdw-stays-out-of-the-correction-path-funded-as-diagnostic-evidence.md) | FDW stays out of the correction path; funded as diagnostic evidence | accepted |
| [0202](0202-audibility-weighted-co-metrics-beside-the-band-grade.md) | Audibility-weighted co-metrics beside the band grade | accepted |
| [0203](0203-the-incumbent-tune-retires-recommissioning-is-structure-first.md) | The incumbent tune retires; recommissioning is structure-first | accepted |
| [0204](0204-per-tool-contracts-live-in-the-tool-the-operator-surface-is-tiered.md) | Per-tool contracts live in the tool; the operator surface is tiered | accepted |
| [0207](0207-tier-1-prescription-bounds-demote-a-cut-is-the-prescribers-to-spend.md) | Tier-1 prescription bounds demote — a cut is the prescriber's to spend | accepted |
| [0208](0208-the-correction-observable-subtracts-the-cushion-decay-demand.md) | The correction observable subtracts the cushion-decay demand | accepted |
| [0209](0209-the-quieter-direction-relaxer-follows-the-claim-not-the-verdict-name.md) | The quieter-direction relaxer follows the claim, not the verdict name | accepted |
| [0210](0210-polarity-has-two-frames-and-one-conversion-owner.md) | Polarity has two frames, and one conversion owner | accepted |
| [0212](0212-way-1-reuses-the-existing-layers-it-does-not-fork-them.md) | Way-1 reuses the existing layers; it does not fork them | accepted |
| [0214](0214-a-raised-cushion-target-is-a-declared-window-not-a-measurement.md) | A raised cushion target is a declared window, not a measurement | amended by 0250 |
| [0216](0216-curve-slots-are-fixed-so-a-quiet-save-takes-the-live-edit-path.md) | Curve slots are fixed, so a quiet save takes the live-edit path | accepted |
| [0219](0219-a-durable-save-that-moves-only-a-trim-writes-in-place.md) | A durable save that moves only a trim writes in place | accepted |
| [0222](0222-the-relay-is-deleted-the-wired-microphone-is-the-only-capture-path.md) | The relay is deleted; the wired microphone on jts.local is the only capture path | amended by 0255 |
| [0230](0230-the-summed-graph-commissioning-lane-is-deleted.md) | The summed-graph commissioning lane is deleted | accepted |
| [0237](0237-a-tuning-tools-stdout-is-its-answer.md) | A tuning tool's stdout is its answer | accepted |
| [0255](0255-every-product-measures-through-the-wired-microphone.md) | Every product measures through the wired microphone | accepted |
| [0256](0256-the-room-ceiling-follows-the-applied-tunes-trusted-floor-and-room-correction-is-per-cabinet.md) | The room ceiling follows the applied tune's trusted floor, and room correction is per cabinet | §4 seat default amended by 0260 |
| [0257](0257-bass-extension-resumes-rebased-on-wired-capture-and-validated-in-room-below-the-ceiling.md) | Bass extension resumes, rebased on wired capture and validated in-room below the ceiling | §1 amended by 0259, §3 superseded by 0260 |
| [0258](0258-the-topology-vocabulary-is-sides-by-driver-roles-and-cardioid-is-a-variant-of-the-bass-role.md) | The topology vocabulary is sides × driver roles, and cardioid is a variant of the bass role | accepted |
| [0259](0259-room-correction-and-bass-extension-are-layers-of-the-one-tuning-toolbox.md) | Room correction and bass extension are layers of the one tuning toolbox | §4 amended by 0265 |
| [0260](0260-poses-are-flexible-and-categorized-and-bass-extension-has-no-nearfield-rung.md) | Poses are flexible and categorized, and bass extension has no nearfield rung | accepted |
| [0263](0263-a-ring-ended-camilladsp-graph-takes-the-ring-geometry.md) | A ring-ended CamillaDSP graph takes the ring geometry | accepted |
| [0265](0265-the-mic-calibration-door-is-a-cli-verb-and-the-daemons-root-mounted-routes-are-gone.md) | The mic calibration door is a CLI verb; the daemon's root-mounted routes are gone | accepted |
