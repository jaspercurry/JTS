#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Shared env-file quoting + atomic, locked writers (single-key upsert/unset,
# multi-key seed). Callers today: jasper-aec-reconcile,
# jasper-audio-hardware-reconcile, deploy/install.sh; other bash writers of the
# same files still bypass it.
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

# _jasper_env_lock_release FD
# Close the descriptor _jasper_env_lock_acquire opened, releasing the lock.
_jasper_env_lock_release() {
    case "$1" in
        8) exec 8>&- ;;
        9) exec 9>&- ;;
        *) return 1 ;;
    esac
}

# _jasper_env_lock_acquire DIR FILE [FD]
# Hold FILE's advisory lock on descriptor FD (default 9; the caller closes it).
# Path and create-time mode/group are jasper/atomic_io.py's _env_lock_path /
# advisory_file_lock, whose group bit matters only where DIR carries group
# jasper (/var/lib/jasper).
#
# The descriptor is a LITERAL digit chosen by `case`, never bash's `{var}>` /
# `local -n` (4.1 / 4.3) and never an `eval` splice: macOS ships bash 3.2 as
# /bin/bash, which parses `exec {var}>FILE` as an exec of a command literally
# named `{var}`, and a non-interactive shell dies 127 there with the write
# unmade. Only 8 and 9 open at all; any other FD is refused. The writers below
# take 9 and jasper_env_file_hold takes 8, so a hold can span their writes to a
# DIFFERENT file. deploy/bin/jasper-airplay-volume holds its own lock on fd 9;
# it never sources this lib, and a caller that did would lose its fd 9 here.
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
    local dir="$1" lock="${1}/.${2##*/}.lock" fd="${3:-9}" rc=0 open=0
    if [[ ! -e "$lock" && ! -L "$lock" ]]; then
        set -C
        if {
            case "$fd" in
                8) exec 8>"$lock" ;;
                9) exec 9>"$lock" ;;
                *) false ;;
            esac
        } 2>/dev/null; then
            open=1
            if [[ -f "/dev/fd/${fd}" ]]; then
                chmod 0660 "/dev/fd/${fd}" 2>/dev/null || true
                chgrp --reference="$dir" "/dev/fd/${fd}" 2>/dev/null || true
            else
                _jasper_env_lock_release "$fd"
                open=0
            fi
        fi
        set +C
    fi
    if (( open == 0 )) && [[ ! -L "$lock" && -f "$lock" ]]; then
        {
            case "$fd" in
                8) exec 8<"$lock" ;;
                9) exec 9<"$lock" ;;
                *) false ;;
            esac
        } 2>/dev/null || rc=$?
        if (( rc != 0 )); then
            echo "event=env_file.lock_failed file=${lock} reason=open rc=${rc}" >&2
            return 1
        fi
        open=1
    fi
    if (( open == 0 )) || [[ ! -f "/dev/fd/${fd}" ]]; then
        echo "event=env_file.lock_failed file=${lock} reason=not_regular rc=1" >&2
        if (( open == 1 )); then
            _jasper_env_lock_release "$fd"
        fi
        return 1
    fi
    flock -w 10 "$fd" || rc=$?
    if (( rc != 0 )); then
        echo "event=env_file.lock_failed file=${lock} reason=flock rc=${rc} wait_s=10" >&2
        _jasper_env_lock_release "$fd"
        return 1
    fi
}

# jasper_env_file_set FILE KEY VALUE [FILE_MODE] [DIR_MODE]
# Atomic (tempfile + rename) single-key upsert under FILE's advisory lock:
# replaces the first KEY= line in FILE (dropping duplicates) or appends one.
# Returns 1 without writing when the lock is refused. Every caller passes its
# own modes; the defaults only keep the arguments optional.
jasper_env_file_set() {
    _jasper_env_file_upsert "$1" "$2" "$3" "${4:-0600}" "${5:-0750}"
}

# _jasper_env_file_publish TMP FILE FILE_MODE
# Give TMP the ownership FILE already carries — or, for a file being created,
# the parent directory's group — then the requested mode, then rename it over
# FILE. The lib's only publish, so upsert, unset and seed cannot drift.
_jasper_env_file_publish() {
    local tmp="$1" file="$2" file_mode="$3"
    if [[ -e "$file" ]]; then
        chown --reference="$file" "$tmp" 2>/dev/null || true
    else
        chgrp --reference="$(dirname "$file")" "$tmp" 2>/dev/null || true
    fi
    chmod "$file_mode" "$tmp"
    mv "$tmp" "$file"
}

# _jasper_env_file_ensure_dir DIR DIR_MODE
# Only CREATE an absent dir; never re-mode an existing one: the installer owns
# each env dir's mode/group and a blanket `install -d -m` on every boot/udev
# reconcile re-strips them (#827).
_jasper_env_file_ensure_dir() {
    [[ -d "$1" ]] || install -d -m "$2" "$1"
}

# jasper_env_file_seed_absent FILE FILE_MODE DIR_MODE KEY=VALUE...
# Append every KEY=VALUE whose KEY the file does not already state — one lock
# hold, one awk pass, one publish. A key the file states keeps the value its
# writer published. The presence test runs INSIDE the hold, so a seed can
# neither overwrite a value written since the caller last looked nor append a
# second line for a key that value already states.
#
# Returns 75 (EX_TEMPFAIL) when the lock was refused and nothing was written, 1
# when the write itself failed, 0 otherwise. The two are distinct codes because
# a caller whose defaults every reader also carries may tolerate the refusal and
# must not tolerate the failure.
jasper_env_file_seed_absent() {
    local file="$1" file_mode="$2" dir_mode="$3"
    shift 3
    local dir tmp pair src lines="" rc=0
    (( $# )) || return 0

    for pair in "$@"; do
        lines+="${pair%%=*}=$(jasper_env_quote_value "${pair#*=}")"$'\n'
    done
    dir="$(dirname "$file")"
    _jasper_env_file_ensure_dir "$dir" "$dir_mode"
    _jasper_env_lock_acquire "$dir" "$file" || return 75
    if ! tmp="$(mktemp "${dir}/.${file##*/}.seed.XXXXXX")"; then
        _jasper_env_lock_release 9
        return 1
    fi
    src=/dev/null
    [[ -f "$file" ]] && src="$file"
    # ENVIRON, never `awk -v` — same escape-processing trap as the upsert below.
    if JASPER_ENV_FILE_SEED_LINES="$lines" awk '
        BEGIN {
            count = split(ENVIRON["JASPER_ENV_FILE_SEED_LINES"], seed, "\n")
            for (i = 1; i <= count; i++) {
                at = index(seed[i], "=")
                if (at > 1) {
                    key[i] = substr(seed[i], 1, at - 1)
                }
            }
        }
        {
            print
            for (i = 1; i <= count; i++) {
                if (key[i] != "" &&
                    $0 ~ "^[[:space:]]*" key[i] "[[:space:]]*=") {
                    seen[i] = 1
                }
            }
        }
        END {
            for (i = 1; i <= count; i++) {
                if (key[i] != "" && !seen[i]) {
                    print seed[i]
                }
            }
        }
    ' "$src" > "$tmp"; then
        _jasper_env_file_publish "$tmp" "$file" "$file_mode" || rc=1
    else
        rc=1
        rm -f "$tmp"
    fi
    _jasper_env_lock_release 9
    return "$rc"
}

# _jasper_env_file_upsert FILE KEY VALUE FILE_MODE DIR_MODE
_jasper_env_file_upsert() {
    local file="$1" key="$2" value="$3"
    local file_mode="$4" dir_mode="$5"
    local dir tmp quoted rc=0

    dir="$(dirname "$file")"
    _jasper_env_file_ensure_dir "$dir" "$dir_mode"
    _jasper_env_lock_acquire "$dir" "$file" || return 1
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

    _jasper_env_file_publish "$tmp" "$file" "$file_mode" || rc=1
    _jasper_env_lock_release 9
    return "$rc"
}

# jasper_env_file_hold FILE / jasper_env_file_drop
# Take FILE's advisory lock and KEEP it across a sequence, so a caller that
# publishes FILE by building a candidate and renaming it excludes another
# holder of the same lock for the whole snapshot→rename window (whichever
# renamed second would discard the other's file entirely). It excludes BASH
# holders only: jasper/fanin/coupling_reconcile.py, the second writer of
# outputd.env, publishes through jasper/atomic_io.py's atomic_write_text, which
# takes no env-file lock at all (ADR-0235 G8, open until the Python side joins
# this path).
# The hold sits on fd 8, not the 9 the writers above take: it spans their
# writes to the CANDIDATE. It cannot span a set/unset of FILE ITSELF — flock(2)
# is per open file description, so the inner open blocks until `flock -w 10`
# gives up. drop is idempotent, so an exit trap may call it unconditionally.
# Removal condition: drop both when jasper-audio-hardware-reconcile's
# stage_outputd_env — the one caller — no longer stages a whole env file.
jasper_env_file_hold() {
    _jasper_env_lock_acquire "$(dirname "$1")" "$1" 8
}

jasper_env_file_drop() {
    { _jasper_env_lock_release 8; } 2>/dev/null || true
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
    local dir tmp rc=0

    [[ -f "$file" ]] || return 0
    dir="$(dirname "$file")"
    _jasper_env_lock_acquire "$dir" "$file" || return 1
    if ! grep -qE "^[[:space:]]*${key}[[:space:]]*=" "$file"; then
        _jasper_env_lock_release 9
        return 0
    fi
    tmp="$(mktemp "${dir}/.${key}.XXXXXX")"
    awk -v key="$key" '
        $0 ~ "^[[:space:]]*" key "[[:space:]]*=" { next }
        { print }
    ' "$file" > "$tmp"
    _jasper_env_file_publish "$tmp" "$file" "$file_mode" || rc=1
    _jasper_env_lock_release 9
    return "$rc"
}
