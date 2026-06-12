# Brief 17 — Mirror source archives as release assets (provenance TODO)

Mission: review §4.4 + deploy/provenance.toml's own `TODO(public-installer)`:
several install.sh fetches point at GitHub *commit archives*, which are NOT
byte-stable — GitHub may regenerate them with different bytes, and our
SHA-256 pins then fail closed, breaking every fresh install. Before public
launch the exact archive bytes must be mirrored as JTS release assets.
This brief needs NETWORK access and `gh` release permissions — confirm both
before starting; if release-asset upload is denied, prepare everything and
hand the maintainer a single upload command list.

Branch: `codex/supply-chain-mirrors`. File fence: `deploy/install.sh`
(fetch URLs only — no restructuring; brief 14 owns structure),
`deploy/provenance.toml`, `scripts/check-provenance.py` + its tests,
`docs/HANDOFF-supply-chain.md`.

SEQUENCING: land BEFORE brief 14's install.sh split (these URL edits are
tiny; the split moves the code they live in).

## One PR

1. **Read deploy/provenance.toml** and enumerate exactly which entries are
   flagged non-byte-stable / TODO-mirror (expect the source builds:
   shairport-sync, nqptp, the webrtc archive for jasper_aec3; check whether
   the camilladsp binary tarball and raspotify .deb are release assets
   upstream — release assets ARE byte-stable and may only need the TODO
   resolved as "no mirror needed", documented).
2. **Download each flagged archive from its pinned URL and verify SHA-256
   against provenance.toml.** Any mismatch = STOP and report (that means
   GitHub already regenerated the archive and the pin is already broken —
   the maintainer must re-verify provenance before mirroring).
3. **Create the mirror release:** `gh release create build-deps-v1 --notes
   "Byte-exact mirrors of pinned third-party source archives; see
   deploy/provenance.toml for upstream provenance"` (or append to it if it
   exists), `gh release upload build-deps-v1 <files>`. Asset filenames keep
   the upstream name + short-SHA suffix so multiple versions can coexist.
4. **Repoint install.sh** fetch URLs to the mirror assets, keeping the
   upstream URL as an adjacent provenance comment. SHA-256 pins unchanged
   (same bytes). Update provenance.toml per its schema: mirror URL as the
   fetch source, upstream URL retained as provenance (extend the schema +
   `scripts/check-provenance.py` if it doesn't have a mirror field — with a
   test).
5. **Docs:** update docs/HANDOFF-supply-chain.md's mirroring section +
   resolve the TODO comments; docs-impact note in the PR body.

Acceptance: `python3 scripts/check-provenance.py` green in CI;
`pytest tests/test_check_provenance*.py -q` (or wherever its tests live)
green; `bash deploy/install.sh --dry-run` unchanged except URLs;
`bash -n` + shellcheck clean; PR body lists each mirrored asset with its
SHA-256 and upstream URL so the maintainer can spot-audit in one glance.
