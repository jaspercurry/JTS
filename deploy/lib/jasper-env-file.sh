#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Shared env-file quoting + atomic, locked single-key writer for every
# bash writer of a JTS env file: deploy/install.sh and the two
# reconcilers (jasper-aec-reconcile, jasper-audio-hardware-reconcile).
#
# Why this exists — and why NOT `printf %q`: bash 5.2 (Trixie) quotes
# values containing commas with backslash escaping, so
# `printf %q 'hw:CARD=A,DEV=0'` emits `hw:CARD=A\,DEV=0`. systemd's
# EnvironmentFile= parser keeps that backslash literally, corrupting
# ALSA device specs, and the reconcilers' own read-back no longer
# matches the intended value — breaking idempotence and causing
# restart churn. Single-quote wrapping is stable across bash versions.
#
# source/EnvironmentFile= parity caveat: bash `source` round-trips
# every value this writer emits, but systemd's EnvironmentFile= parser
# does NOT do shell quote-concatenation, so the '\'' idiom used for
# embedded single quotes diverges between the two readers. That is
# fine for every value written today (ALSA pcm specs, profile ids,
# udp:PORT — none contain apostrophes); do not route apostrophe-
# bearing values through this writer into a file systemd reads via
# EnvironmentFile= without revisiting the quoting.
# The %q bug was first fixed in jasper-audio-hardware-reconcile
# (PR #534); this lib is the single shared implementation so the bug
# class cannot fork between the reconcilers again.

# jasper_env_quote_value VALUE
# Print VALUE quoted for an env file. Safe-charset values pass
# through verbatim; anything else is single-quote wrapped with
# embedded single quotes escaped as '\''.
jasper_env_quote_value() {
    local value="$1" rest
    if [[ -z "$value" ]]; then
        printf "''"
        return
    fi
    case "$value" in
        *[!A-Za-z0-9_./:@,+=-]*)
            # Production values (ALSA pcm specs, profile ids, ports) are
            # all in the safe charset above; reaching this splice path
            # means an unexpected value shape. Quote it correctly anyway
            # (defense in depth), but say so — quoting subtleties are
            # where every bug in this lib's history has lived.
            echo "event=env_file.quote_splice_engaged value_class=non_safe_charset" >&2
            printf "'"
            # Quote-in-variable pattern ("$q") rather than an escaped \'
            # in the expansion pattern — both work on the bashes tested
            # (5.2.21, 5.3.0), but the variable form needs no reasoning
            # about escape parsing inside ${...%%pattern} at all.
            local q="'"
            rest="$value"
            # Emit the '\'' idiom via %s ARGUMENTS, never via the printf
            # FORMAT string: bash printf interprets backslash escapes in
            # the format, so a format-embedded \' silently drops the
            # backslash and emits a malformed quote run (latent bug in
            # the pre-lib PR #534 copy of this loop).
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

# _jasper_env_lock_create DIR LOCK
# Create an env file's advisory lock with DIR's group and mode 0660 — the
# provisioning jasper/atomic_io.py's advisory_file_lock applies to the very
# same path, so a root bash writer and a group-jasper Python writer contend
# for one inode instead of two. Best effort: a non-owner can repair neither
# bit, and the install heal (deploy/lib/install/env-migrations.sh) owns that.
_jasper_env_lock_create() {
    if touch "$2" 2>/dev/null; then
        chgrp --reference="$1" "$2" 2>/dev/null || true
        chmod 0660 "$2" 2>/dev/null || true
    fi
}

# jasper_env_file_set FILE KEY VALUE [FILE_MODE] [DIR_MODE]
# Atomic (tempfile + rename) single-key upsert, serialized against every
# other writer by the advisory lock jasper/atomic_io.py's _env_lock_path
# names for FILE: replaces the first KEY= line in FILE (dropping
# duplicates) or appends one. Returns 1 when the lock is not granted
# within 10 s. Modes default to 0600 file / 0750 dir (what the installer
# leaves /etc/jasper at); callers with a different posture pass theirs
# explicitly.
jasper_env_file_set() {
    local file="$1" key="$2" value="$3"
    local file_mode="${4:-0600}" dir_mode="${5:-0750}"
    local dir tmp quoted lock lock_fd rc=0

    dir="$(dirname "$file")"
    # Only CREATE an absent dir; never re-mode an EXISTING one. The installer
    # owns each env dir's canonical mode/group (/var/lib/jasper is 0770
    # root:jasper so the now-non-root daemons can write group-shared state;
    # /etc/jasper is 0750 so the group-jasper doctor-json oneshot can traverse)
    # and this writer runs on every boot / udev reconcile — a blanket
    # `install -d -m $dir_mode` re-strips those bits (the trap #827 closed for
    # the audio-hardware reconciler's own writers; closing it here covers every
    # caller of the shared lib, e.g. jasper-aec-reconcile).
    [[ -d "$dir" ]] || install -d -m "$dir_mode" "$dir"
    lock="${dir}/.${file##*/}.lock"
    [[ -e "$lock" ]] || _jasper_env_lock_create "$dir" "$lock"
    exec {lock_fd}>>"$lock" || return 1
    if ! flock -w 10 "$lock_fd"; then
        exec {lock_fd}>&-
        return 1
    fi
    tmp="$(mktemp "${dir}/.${key}.XXXXXX")"
    quoted="$(jasper_env_quote_value "$value")"

    if [[ -f "$file" ]]; then
        # The replacement line goes in via ENVIRON, never `awk -v`:
        # -v values get escape-sequence processing (gawk and mawk
        # disagree on unknown escapes like the \' inside a quoted
        # value), which corrupted apostrophe-bearing lines on CI's
        # mawk while passing on others. ENVIRON is escape-free by
        # POSIX on every awk.
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

# jasper_env_file_repair_permissions FILE [FILE_MODE] [DIR_MODE]
# Repair mode + parent-directory group on an existing generated env file without
# changing its contents. This closes the no-op reconcile case: if an old file is
# already content-current but root:root, non-root status daemons cannot read it
# and /state drifts from root doctor.
jasper_env_file_repair_permissions() {
    local file="$1"
    local file_mode="${2:-0600}" dir_mode="${3:-0750}"
    local dir

    dir="$(dirname "$file")"
    [[ -d "$dir" ]] || install -d -m "$dir_mode" "$dir"
    [[ -f "$file" ]] || return 0
    chgrp --reference="$dir" "$file" 2>/dev/null || true
    chmod "$file_mode" "$file"
}

# jasper_env_file_unset FILE KEY [FILE_MODE] [DIR_MODE]
# Atomically REMOVE every KEY= line from FILE (no-op when FILE or the key is
# absent). Distinct from `jasper_env_file_set FILE KEY ""`, which leaves an
# explicit `KEY=` empty assignment: that empty assignment, in a file systemd
# loads via EnvironmentFile= AFTER an earlier file, OVERRIDES the earlier
# value with empty rather than deferring to it. When an operator-set key in an
# earlier-loaded file (jasper.env) must win, the reconciler-owned later file
# (outputd.env) must DROP the key entirely so systemd never sees a shadowing
# assignment — that is what this helper provides. Sets nothing new; a missing
# FILE or key is a no-op. Holds FILE's advisory lock like jasper_env_file_set
# and returns 1 when it is not granted within 10 s. Modes default to 0600/0750.
jasper_env_file_unset() {
    local file="$1" key="$2"
    local file_mode="${3:-0600}" dir_mode="${4:-0750}"
    local dir tmp lock lock_fd rc=0

    [[ -f "$file" ]] || return 0
    dir="$(dirname "$file")"
    [[ -d "$dir" ]] || install -d -m "$dir_mode" "$dir"
    lock="${dir}/.${file##*/}.lock"
    [[ -e "$lock" ]] || _jasper_env_lock_create "$dir" "$lock"
    exec {lock_fd}>>"$lock" || return 1
    if ! flock -w 10 "$lock_fd"; then
        exec {lock_fd}>&-
        return 1
    fi
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
