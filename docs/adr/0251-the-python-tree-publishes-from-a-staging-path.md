# ADR-0251: The Python tree publishes from a staging path

- **Date:** 2026-09-07
- **Status:** Accepted (takes the cheaper option
  [ADR-0172](0172-full-a-b-install-generations-stay-deferred.md) deferred; full
  A-B generations stay deferred, and that ruling otherwise stands)

## Context

ADR-0172 shipped the cheap four and deferred full A-B until "a
reboot-inside-a-failed-update window is an observed failure mode", naming the
fallback to revisit first: "a cheaper atomic swap of just the Python tree via a
staging path".

#4123 is the observation. Both install profiles rsynced the checkout into
`/opt/jasper` with `--delete` *before* the venv and pip steps, with every daemon
still serving the old code from RAM. A dependency that would not resolve or
build therefore left the box with the new source tree on disk and the old
dependency set in the venv — a state ADR-0172's "no restart on failed install"
survives only until the next restart or reboot, which is exactly the window it
named.

## Decision

The Python tree publishes from `${INSTALL_DIR}/.staging`, under five rules:

1. The checkout rsyncs into a freshly created staging directory with
   `--link-dest="${INSTALL_DIR}"`, so a file this deploy does not change is a
   second link to the live inode rather than a copy, and a file differing only
   in attributes gets a fresh inode instead of rewriting the live one.
2. The dependencies the *staged* manifest declares install before anything in
   the live tree moves. A manifest that cannot be read, or that does not declare
   the extra the profile installs, fails the deploy there.
3. Each top-level staged entry is then renamed into place one at a time, the
   live entry moving aside to `<name>.prev` inside the staging tree first —
   a rename onto an existing directory would nest inside it.
4. The success path renames the staging tree to `.staging.done` before deleting
   it, so a delete cut off partway cannot leave a truncated `.prev` where the
   rollback would read it as a complete old copy.
5. Any failure runs `remove_staged_install_tree` from the installer's EXIT
   trap: every `.prev` is restored, including over an entry already published,
   and a restore that itself fails returns rather than reaching the delete. Each
   entry emits `event=install.staging_rollback entry=<name> restored=<yes|no>`.

The guarantee is **per entry, not per tree**: an entry is absent for the instant
between its two renames, is otherwise wholly old or wholly new, and any failure
rolls the published entries back — so what a re-deploy or a reboot finds is the
whole old tree or the whole new one.

## Consequences

- ADR-0172's named residual risk — a reboot inside a failed-update window —
  is retired *for the Python source tree only*. The venv, the Rust binaries,
  the systemd units and the nginx config are not covered by this and keep the
  cheap four's guarantees; that is why full A-B stays deferred rather than
  refuted.
- The staged tree costs no bytes for unchanged files, so the swap is affordable
  on the 415 MB Zero 2 W that motivated ADR-0172's cost objection.
- A top-level entry is replaced whole, so a sibling under one of them that the
  checkout does not ship is dropped — profile-switch leftovers, and stale
  `__pycache__` that Python regenerates. This replaces the per-file `--delete`
  the rsync used to perform against the live tree.
- Rejected: installing the staged tree editable and re-linking afterwards. It is
  shorter, but between the two pip calls the venv points into a directory the
  publish is dismantling, and a failed re-link leaves a box that cannot import
  `jasper` at all.
