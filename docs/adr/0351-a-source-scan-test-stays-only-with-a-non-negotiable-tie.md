# ADR-0351: A source-scan test stays only with a non-negotiable tie

- **Date:** 2026-09-23
- **Status:** Accepted

## Context

AGENTS.md forbids asserting on source text, and
[ADR-0001](0001-operating-model-reset.md) (rules 4 and 5) lets permanent
machinery stand only on a non-negotiable tie or a recurrence. Hundreds of
tests still assert on source text, and a pin on one file's exact text fails
on edits that keep behavior. The owner's decision SOURCE-SCANS on
[#5643](https://github.com/jaspercurry/JTS/issues/5643): tie it or drop it.

A test is a *source scan* when it, or a helper it calls, reads a repo code
file's text, asserts on that text, and executes nothing. A census at
`856143354` found 489 in about 170 files. It bucketed them by non-negotiable
keywords and by incident-plus-recurrence wording in their docstrings and
comments, then hand-checked the buckets: 23 tied, 242 that pin one file's
exact text, and 224 tree lints (122) or cross-language mirrors (102).

A second hand-check at `508c41fd9` read each of the 23 against AGENTS.md's
closed list and kept 20. Three guard neither a non-negotiable nor a
recurrence:

- `tests/test_crossover_v2_priors.py::test_no_class_in_the_flow_defines_a_method_twice`
  guards one incident, the first cut of #2291 phase 5a-iii leaving five
  methods shadowed. No PR records a second, and the migration that could
  repeat it ended when the conductor was deleted.
- `tests/test_doctor_secrets.py::test_member_units_are_non_root` keeps the
  doctor's availability probe meaningful. A root member moves no key out of
  its compartment and writes none to logs, `/state` or doctor output.
- `tests/test_xvf_host.py::test_production_xvf_callers_use_only_registered_commands`
  cannot see `SAVE_CONFIGURATION`, since its command-name pattern has no
  `SAVE` prefix. The runtime command table refuses an unregistered name, a
  wrong access mode or a wrong value count before any USB transfer.

## Decision

1. A source scan stays only when it guards a non-negotiable (AGENTS.md's
   closed list) or a recurrence, an incident that happened again rather
   than one that could. Any other is deleted, at the latest when it blocks
   a change.
2. These 20 are locked: the set changes only by a superseding ADR, and
   agents do not weaken or delete these tests.
   - **1 Hearing:** `tests/test_active_speaker_safety_envelope_ssot.py::test_no_production_site_restates_the_commissioning_stop`
   - **2 Hardware damage:** `tests/test_xvf_host.py::test_static_guard_keeps_unsafe_xvf_commands_out_of_command_table`
   - **3 Secrets:**
     - `tests/test_diagnostic_redaction_scripts.py::test_pi_bundle_redacts_unit_files_before_packaging`
     - `tests/test_diagnostic_redaction_scripts.py::test_fetch_logs_does_not_capture_all_sudo_commands`
     - `tests/test_doctor_secrets.py::test_compartment_groups_are_created_by_install`
     - `tests/test_google_creds.py::test_install_creates_google_dir_setgid`
     - `tests/test_logging_setup.py::test_configure_logging_is_the_only_logging_bootstrap`
     - `tests/test_secret_env_modes.py::test_install_widens_secret_env_on_upgrade`
     - `tests/test_systemd_hardening.py::test_secrets_compartment_phase4a`
     - `tests/test_systemd_hardening.py::test_secrets_compartment_phase4b`
     - `tests/test_wifi_guardian_script.py::test_guardian_recreate_stderr_uses_private_mktemp`
   - **4 Deploy integrity:**
     - `tests/test_install_helpers.py::test_write_build_manifest_is_atomic_tempfile_rename`
     - `tests/test_install_helpers.py::test_build_manifest_not_written_during_python_runtime_install`
   - **6 No silent deafness:**
     - `tests/test_cue_registry_coverage.py::test_every_registered_cue_is_played_somewhere`
     - `tests/test_cue_registry_coverage.py::test_every_played_slug_is_registered`
   - **7 Paid tests and 8 `main` is protected:** `tests/test_build_and_ci_contracts.py::test_ci_pytest_gate_is_parallel_and_hardware_free`
   - **8 `main` is protected** (the docs and landing lanes run only the tests
     these find):
     - `tests/test_ci_classifier.py::test_discovery_sees_reads_and_ignores_mentions`
     - `tests/test_ci_classifier.py::test_landing_bundle_is_exactly_the_tests_that_read_the_landing_page`
     - `tests/test_ci_classifier.py::test_docs_bundle_registers_every_discoverable_doc_reading_test`
     - `tests/test_ci_classifier.py::test_hand_registered_readers_are_exactly_what_discovery_cannot_see`
3. The 242 one-file text pins are deleted area by area, in the
   deletion-only PRs that follow under #5643. A pin that proves tied, or
   proves to be a mirror, stays and is named in its PR.
4. Every other source scan (the 224 tree lints and cross-language mirrors,
   and the three dropped above) stays unlocked: an agent deletes one when
   it blocks a change.
5. A new source scan names, in its docstring, the non-negotiable or the
   recurrence it guards.

## Consequences

An agent blocked by an unlisted source scan whose docstring names no
non-negotiable or recurrence deletes it in that change. A change that moves
what a locked test reads carries the test along with every assertion.
Wording overcounts ties (3 of 23 fell on reading), so a claimed tie is
checked against the closed list. Deleting the one-file pins gives up some
early warnings, such as a dropped flag in an install line; behavior tests,
CI and cheap deploys catch what matters, which is ADR-0001's trade.
Rejected: locking every scan whose prose names a non-negotiable (the
overcount); deleting all 489 at once (it would drop the non-negotiables'
guards); a lint for item 5 (a new guard against a hypothetical; review
enforces it).
