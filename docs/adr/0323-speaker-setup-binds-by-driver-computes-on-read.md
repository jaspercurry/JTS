# ADR-0323: Speaker setup binds a pasted reply by driver, computes the safety profile on read, and builds the crossover preview on request

- **Date:** 2026-09-17
- **Status:** Accepted

## Context

The speaker-setup investigation (tracking issue #5270, audited at `25d049f37`) graded fifty gates on the path from a blank speaker to a playing one and found that the research loop, the stored safety profile and the persisted crossover preview are held together by fingerprints and revisions that protect nothing the owner has ever hit. All of them arrived in one PR (#1446) with no incident behind them. In one evening they caused three refusals of the owner's own input (#5259, #5262, and a reset that re-saved stale values) and none of them caught a real fault.

Three mechanisms are at issue:

1. **The research request.** "Copy prompt" mints a document that carries the drivers, the hardware block, the notes and every limit typed so far, hashes it, and the page holds it in memory. The reply must echo the hash; the save rebuilds the document from the current form and refuses on any difference; any field edit drops the held document, after which a pasted reply is silently discarded. The typed limits ride into the prompt as "operator declared context", which is how a stale two-way form polluted a three-way research on 2026-09-16.
2. **The stored safety profile.** The draft save derives a profile from the visible values, self-confirms it, stores it, then re-parses it through a 330-line shape validator and re-evaluates it as stale, malformed, incomplete or confirmed on every read. Apply refuses unless it reads confirmed, and "incomplete" is triggered by measurement-protocol fields (level and duration limits, measurement band, hard excitation band) that have nothing to do with playing the baseline.
3. **The persisted crossover preview.** The preview is written to a file with two fingerprints and three stale states computed on every load, yet Apply never reads the file: it rebuilds the preview from the draft in memory. The file exists only to produce stale states for the page to explain.

The owner's ruling on the underlying question: this is a one-owner box. Two people editing the same speaker at once is not a scenario the design covers.

## Decision

1. **A pasted reply binds by driver, not by request.** The research prompt is text built from the topology, the driver models and the build notes. Nothing is minted, held or hashed. A pasted reply is accepted when every driver it names is a current target (by `target_id`) with the same model. The reply's values fill the visible fields; the operator edits what is wrong; Save keeps both the reply and the edits. The server keeps exactly two refusals on this path: a reply for a different speaker (wrong targets or models), and an implausible low-frequency limit (ADR-0227 §1, incident #2874). The `driver_research_request` artefact, its fingerprint, `operator_declared_context`, the prefill comparison, the legacy re-stamp machinery, the design-draft `expected_revision`, and the client's held request and binding invalidation are deleted (#5274).
2. **The safety profile is computed on read.** One pure function of (draft, topology) produces the per-target values, the derived bands and filters, the provenance badges and the issue list, evaluated whenever it is asked for. Nothing about it is stored, fingerprinted, confirmed or re-validated. Apply refuses only when a tweeter has no declared floor; level and duration limits, the measurement band and the hard excitation band become measurement admission inputs, checked when a measurement is started (#5277).
3. **The crossover preview is built on request.** `GET crossover-preview` returns the preview computed from the current draft. There is no preview file, no preview fingerprint, no stale state and no POST to prepare one. The blocker "crossover frequency above the lower driver's usable range" becomes a warning, matching its sibling (#5278).

The page becomes a renderer of server documents (topology, design draft, computed safety issues, computed preview, commissioning view, baseline profile) with a small set of POSTs (save layout, reset, save values, apply, restore). Client state is form values only.

## Consequences

- Copy prompt, paste, edit, save works in any order and survives a reload. The prompt can no longer be polluted by a previous speaker's limits because it no longer carries limits.
- The server cannot tell that a reply was written for an older version of the prompt. Accepted: the driver binding catches a reply for the wrong speaker, and the plausibility door catches a reply that would harm a driver.
- A draft edit no longer invalidates a preview or a profile; both are recomputed from the draft at the next read, so there is nothing to invalidate.
- Concurrent editors are not protected. Accepted by the owner's ruling above. If that ever changes, the protection belongs in one place (a single draft revision on the design draft POST), not in five fingerprints.
- Roughly 1,800 product lines and their prose-asserting tests are deleted across #5274, #5277 and #5278.

Rejected: keeping the request fingerprint but excluding the declared context from the comparison (the state after #5262). It removed the worst refusal and kept every other one, plus the held-in-memory request and the silent drop.

Supersedes nothing in `docs/adr/`; the mechanisms it removes were introduced without an ADR.
