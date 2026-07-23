## Summary

(1-3 sentences on what changes and why. Focus on the why — the diff
shows the what.)

## Documentation impact

`docs-impact.yml` will comment with mapped canonical docs from
`docs/doc-map.toml`. Use that list as the starting point; add anything
the bot missed.

- [ ] No canonical doc impact — rationale:
- [ ] I scanned the mapped canonical doc(s) and they are still accurate.
- [ ] I updated the mapped canonical doc(s) in this PR.
- [ ] Follow-up doc issue is acceptable here — link:

Docs scanned / evidence:

- (docs/commands/source files checked, or "none")

See `scripts/doc-freshness.sh` for HANDOFFs overdue for a verification
pass.

## Test plan

- [ ] Relevant hardware-free validation passes (commands/evidence below)
- [ ] Hardware/Pi evidence is included when this change affects
  hardware, deployment, boot, audio, or runtime-sensitive behavior;
  otherwise `N/A` is sufficient
- [ ] If voice-eval was run: cost was approximately $___ — see
  AGENTS.md "Voice-eval cost discipline" for the discipline. Don't
  run voice-eval if you can't justify the dollar figure.

Validation evidence:

- (commands/results)

Hardware/Pi evidence:

- (results, or `N/A`)

## Notes for reviewer

(Anything non-obvious: design trade-offs, things you intentionally
didn't do, follow-up work, surprising files in the diff.)
