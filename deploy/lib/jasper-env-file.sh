#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Shared env-file quoting + atomic, locked single-key writer. Callers today:
# jasper-aec-reconcile, jasper-audio-hardware-reconcile, deploy/install.sh;
# other bash writers of the same files still bypass it.
#
# Values are single-quote wrapped, never `printf %q`: bash 5.2 escapes commas
# (`hw:CARD=A\,DEV=0`), systemd's EnvironmentFile= parser keeps that backslash
# literally, and the corrupted read-back turns idempotence into restart churn
# (PR #534). EnvironmentFile= also does no shell quote-concatenation, so the
# '\'' idiom emitted for an apostrophe reads differently to `source` than to
# systemd — no value written today contains one.

# jasper_env_quote_value VALUE
# Print VALUE quoted for an env file. Safe-charset values pass through
# verbatim; anything else is single-quote wrapped with embedded single
# quotes escaped as '\''.
jasper_env_quote_value() {
    local value="$1" rest
    if [[ -z "$value" ]]; then
        printf "''"
        return
    fi
    case "$value" in
        *[!A-Za-z0-9_./:@,+=-]*)
            # Reaching here means a value shape production does not produce.
            echo "event=env_file.quote_splice_engaged value_class=non_safe_charset" >&2
            printf "'"
            local q="'"
            rest="$value"
            # The '\'' idiom goes in as a %s ARGUMENT, never in the FORMAT:
            # bash printf eats a format's backslashes, malforming the run.
            while [[ "$rest" == *"'"* ]]; do
                printf '%s%s' "${rest%%"$q"*}" "'\''"
                rest="${rest#*"$q"}"
            done
            printf "%s'" "$rest"
            ;;
        *)
            printf '%s' "$value"
            ;;
    esac
}

# _jasper_env_lock_acquire DIR FILE FDVAR
# Hold FILE's advisory lock in the descriptor FDVAR names (a nameref; the
# caller closes it). Path and create-time mode/group are jasper/atomic_io.py's
# _env_lock_path / advisory_file_lock, whose group bit matters only where DIR
# carries group jasper (/var/lib/jasper).
#
# NOTHING acts on the lock by name: a jasper-group process can swap a symlink
# into that 0770 directory between two lookups, and a by-name `touch`/`chmod`
# lands on its target (transit.env, control_token). So mode and group are only
# ever published on a descriptor, and only on one this call created: bash adds
# O_EXCL under noclobber solely when its pre-open stat FAILS, so a raced
# symlink to a DEVICE is opened and followed, and the descriptor is re-checked
# before anything touches it. An existing lock opens READ-ONLY, because `>>`
# would follow a raced symlink and CREATE its target while flock(2) ignores
# the open mode. A raced symlink is still opened before it is rejected, and
# closed without a chmod, chgrp, write or create; the one Pi device where a
# bare open has a side effect, /dev/watchdog0, is single-open and held by PID
# 1, so that open gets EBUSY. A pre-planted FIFO is refused without opening;
# one raced in blocks either open, which bash cannot make non-blocking.
_jasper_env_lock_acquire() {
    local dir="$1" file="$2" lock="${1}/.${2##*/}.lock" rc=0
    local -n fd_ref="$3"
    if [[ ! -e "$lock" && ! -L "$lock" ]]; then
        set -C
        if { exec {fd_ref}>"$lock"; } 2>/dev/null; then
            if [[ -f "/dev/fd/${fd_ref}" ]]; then
                chmod 0660 "/dev/fd/${fd_ref}" 2>/dev/null || true
                chgrp --reference="$dir" "/dev/fd/${fd_ref}" 2>/dev/null || true
            else
                exec {fd_ref}>&-
                fd_ref=''
            fi
        fi
        set +C
    fi
    if [[ -z "${fd_ref:-}" && ! -L "$lock" && -f "$lock" ]]; then
        { exec {fd_ref}<"$lock"; } 2>/dev/null || rc=$?
        if (( rc != 0 )); then
            echo "event=env_file.lock_failed file=${lock} reason=open rc=${rc}" >&2
            return 1
        fi
    fi
    if [[ -z "${fd_ref:-}" ]] || [[ ! -f "/dev/fd/${fd_ref}" ]]; then
        echo "event=env_file.lock_failed file=${lock} reason=not_regular rc=1" >&2
        if [[ -n "${fd_ref:-}" ]]; then
            exec {fd_ref}>&-
        fi
        return 1
    fi
    flock -w 10 "$fd_ref" || rc=$?
    if (( rc != 0 )); then
        echo "event=env_file.lock_failed file=${lock} reason=flock rc=${rc} wait_s=10" >&2
        exec {fd_ref}>&-
        return 1
    fi
}

# jasper_env_file_set FILE KEY VALUE [FILE_MODE] [DIR_MODE]
# Atomic (tempfile + rename) single-key upsert under FILE's advisory lock:
# replaces the first KEY= line in FILE (dropping duplicates) or appends one.
# Returns 1 without writing when the lock is refused. Every caller passes its
# own modes; the defaults only keep the arguments optional.
jasper_env_file_set() {
    local file="$1" key="$2" value="$3"
    local file_mode="${4:-0600}" dir_mode="${5:-0750}"
    local dir tmp quoted rc=0 lock_fd=''

    dir="$(dirname "$file")"
    # Only CREATE an absent dir; never re-mode an existing one: the installer
    # owns each env dir's mode/group and a blanket `install -d -m` on every
    # boot/udev reconcile re-strips them (#827).
    [[ -d "$dir" ]] || install -d -m "$dir_mode" "$dir"
    _jasper_env_lock_acquire "$dir" "$file" lock_fd || return 1
    tmp="$(mktemp "${dir}/.${key}.XXXXXX")"
    quoted="$(jasper_env_quote_value "$value")"

    if [[ -f "$file" ]]; then
        # ENVIRON, never `awk -v`: -v applies escape-sequence processing and
        # gawk/mawk disagree on unknown escapes like the \' inside a quoted
        # value, which corrupted apostrophe-bearing lines on CI's mawk.
        JASPER_ENV_FILE_LINE="${key}=${quoted}" awk -v key="$key" '
            $0 ~ "^[[:space:]]*" key "=" {
                if (!done) {
                    print ENVIRON["JASPER_ENV_FILE_LINE"]
                    done = 1
                }
                next
            }
            { print }
            END {
                if (!done) {
                    print ENVIRON["JASPER_ENV_FILE_LINE"]
                }
            }
        ' "$file" > "$tmp"
    else
        printf '%s=%s\n' "$key" "$quoted" > "$tmp"
    fi

    if [[ -e "$file" ]]; then
        chown --reference="$file" "$tmp" 2>/dev/null || true
    else
        chgrp --reference="$dir" "$tmp" 2>/dev/null || true
    fi
    chmod "$file_mode" "$tmp"
    mv "$tmp" "$file" || rc=1
    exec {lock_fd}>&-
    return "$rc"
}

# jasper_env_file_repair_permissions FILE [FILE_MODE]
# Repair mode + parent group on an existing generated env file without touching
# its contents: a content-current but root:root file is unreadable to the
# non-root status daemons and /state then drifts from root doctor.
jasper_env_file_repair_permissions() {
    local file="$1"
    local file_mode="${2:-0600}"
    local dir

    [[ -f "$file" ]] || return 0
    dir="$(dirname "$file")"
    chgrp --reference="$dir" "$file" 2>/dev/null || true
    chmod "$file_mode" "$file"
}

# jasper_env_file_unset FILE KEY [FILE_MODE]
# Atomically REMOVE every KEY= line from FILE under its advisory lock (no-op
# when FILE or the key is absent; 1 without writing if the lock is refused).
# Distinct from `jasper_env_file_set FILE KEY ""`: an explicit empty `KEY=` in
# a file systemd loads via EnvironmentFile= AFTER another OVERRIDES the earlier
# value rather than deferring, so an operator's jasper.env key only wins if the
# reconciler-owned outputd.env DROPS its own.
jasper_env_file_unset() {
    local file="$1" key="$2"
    local file_mode="${3:-0600}"
    local dir tmp rc=0 lock_fd=''

    [[ -f "$file" ]] || return 0
    dir="$(dirname "$file")"
    _jasper_env_lock_acquire "$dir" "$file" lock_fd || return 1
    if ! grep -qE "^[[:space:]]*${key}[[:space:]]*=" "$file"; then
        exec {lock_fd}>&-
        return 0
    fi
    tmp="$(mktemp "${dir}/.${key}.XXXXXX")"
    awk -v key="$key" '
        $0 ~ "^[[:space:]]*" key "[[:space:]]*=" { next }
        { print }
    ' "$file" > "$tmp"
    chown --reference="$file" "$tmp" 2>/dev/null || true
    chmod "$file_mode" "$tmp"
    mv "$tmp" "$file" || rc=1
    exec {lock_fd}>&-
    return "$rc"
}
