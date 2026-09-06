#!/bin/sh

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# This fragment is sourced, never executed. The `#!/bin/sh` shebang, where its
# deploy/lib siblings declare bash, holds the body to POSIX under the static
# linter.

# sed_inplace FILE EXPRESSION...
#
# For substitutions that are not key operations — /etc/bluetooth/main.conf,
# the landing page's version stamp, whole-line deletes. Atomic KEY=VALUE
# writes belong to jasper_env_file_set / jasper_env_file_unset in
# deploy/lib/jasper-env-file.sh, whose quoting and mode semantics differ.
#
# The single in-place spelling GNU and BSD sed parse identically. GNU takes
# `-i`'s backup suffix ATTACHED to the flag; BSD takes it as the NEXT
# ARGUMENT, so a bare `-i` makes BSD read the sed program as the suffix and
# then the file path as the program (issue #3021 — every macOS lane red).
# Both preserve the edited file's owner, group and mode (the backup IS the
# original, relinked), so /etc/jasper/jasper.env survives the edit as
# root:jasper 0640.
sed_inplace() {
    _sed_inplace_file="$1"
    shift
    # `|| return` so a failed edit is the function's status, not `rm`'s
    # (sed leaves no backup behind when it fails before rewriting).
    sed -i.bak "$@" "$_sed_inplace_file" || return $?
    rm -f "${_sed_inplace_file}.bak"
}
