# Brief 14 — install.sh: split the two god-functions, de-heredoc the model staging

Mission: review §5.1 + the install dive (8/10 but two ~520-line functions,
triplicated untestable heredocs, and a permissions flip-flop). High blast
radius: this is the root installer. Every change must keep `--dry-run` output
semantically identical and the plan-coverage test green.

Branch: `codex/install-split`. File fence: `deploy/install.sh`,
`deploy/lib/install/**` (new files), `jasper/model_downloads.py`,
`tests/test_install_*.py`. SEQUENCING: land AFTER brief 17 (supply-chain
mirrors) if both are open — 17's URL edits are tiny and this PR moves the
code they live in.

## PR 1 — model staging out of heredocs

The three Python heredocs (~1235-1291, 1310-1343, 1360-1393 at review time)
are copies of exists/hash-check/unlink/download/failure-count logic that
pytest cannot reach. `jasper/model_downloads.py` already owns bounded
downloads — add a CLI entry point (`python -m jasper.model_downloads stage
--registry <wake|dtln|...> --required/--optional`) encapsulating that logic
once, called three times from install.sh with args. Unit-test the staging
logic (tmp_path, fake hashes, byte-cap, retry counting). install.sh keeps
only the invocation lines + pins.

## PR 2 — split the god-functions along the existing lib seam

`install_jasper` (~942-1497) → `deploy/lib/install/python-runtime.sh` +
`model-staging.sh`; `install_systemd_units` (~1499-2015) →
`systemd-units.sh` (and `web-tls.sh` if the TLS block is big enough).
install.sh keeps `main()`, the version/SHA pins block, and preflights.
Function names stay identical (tests source the script and call them).
While moving, fix the in-scope known issues:
- **`ensure_state_dir` once:** /var/lib/jasper mode is set to 0750 in eight
  places and reset to 0755 in four (install -d resets perms; last writer
  wins). One helper, one canonical mode — determine the right one from
  jasper-voice.service's StateDirectoryMode (0750) and align every site incl.
  deploy/lib/install/env-migrations.sh.
- **Hoist `camilla_config_has_safe_volume_limit`** (nested in
  install_camilladsp) to top level and add awk behavior tests: quoted values,
  commented-out lines, positive values must all be rejected — this is the
  0 dB loud-output floor's install-time check.
- **Dedupe the Rust builders** (`build_install_jasper_fanin` vs `_outputd`,
  ~45 identical lines; check if jasper-tts-protocol changed the build list)
  into one `build_install_rust_daemon name required-flag` helper.
- **WIZARD_UNITS array once** (the 5-unit list appears twice; comments still
  say "4 wizard" — fix counts).

Acceptance (non-negotiable):
- `tests/test_install_plan_covers_main.py` green — update plan markers as
  functions move, but every main() step keeps a dry-run marker.
- `tests/test_install_helpers.py` + all `test_install_*_migration.py` green.
- `bash -n` + shellcheck (severity=warning, matching CI) on install.sh and
  every new lib file.
- `bash deploy/install.sh --dry-run` before vs after: same steps in the same
  order (attach the diff -u of both outputs to the PR body — empty or
  whitespace-only).
- Flag needs-on-device: one full `bash scripts/deploy-to-pi.sh` by the
  maintainer after merge before trusting the next deploy.
