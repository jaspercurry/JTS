#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Include guard: a second `source` of this file must be harmless, since the
# `readonly` below would otherwise abort a caller running under
# `set -euo pipefail`. An INHERITED guard variable makes the lib define
# nothing, which the consumers' `declare -F` checks turn into a loud exit 66.
if [[ -n "${_JASPER_ENV_FILE_LIB_LOADED+x}" ]]; then
    return 0
fi
_JASPER_ENV_FILE_LIB_LOADED=1

# Shared env-file reader + quoting + atomic, locked single-key writer for the
# bash consumers of /etc/jasper/jasper.env and the wizard-owned
# /var/lib/jasper*/*.env files.
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

# The line parser both readers below share: `key` non-empty prints that
# key's LAST value (rc 1 when absent); `key` empty prints every key as
# NAME then value on the next line, in first-appearance order with the
# last assignment's value. One matched surrounding quote pair is
# stripped and jasper_env_file_set's own '\'' splice undone, so set ->
# get round trips an apostrophe-bearing value; nothing is evaluated.
# Otherwise this parses byte-for-byte like read_stash in
# jasper/wifi_guardian_persistence.py, which reads the same files.
# readonly: jasper_env_file_export already skips every `_JASPER_`-prefixed
# key it finds (below), so this is the backstop — nothing may reassign this
# shell global if that skip is ever weakened.
readonly _JASPER_ENV_FILE_AWK='
    BEGIN { sq = "\047"; dq = "\042"; splice = sq "\\" sq sq }
    function unquote(v,   len, q, out, i) {
        len = length(v)
        q = substr(v, 1, 1)
        if (len >= 2 && q == substr(v, len, 1) && (q == sq || q == dq)) {
            v = substr(v, 2, len - 2)
            if (q == sq) {
                # index/substr, never a dynamic regex: mawk and gawk
                # disagree on backslash escapes inside one.
                out = ""
                while ((i = index(v, splice)) > 0) {
                    out = out substr(v, 1, i - 1) sq
                    v = substr(v, i + 4)
                }
                v = out v
            }
        }
        return v
    }
    {
        line = $0
        sub(/^[ \t\r\v\f]+/, "", line)
        sub(/[ \t\r\v\f]+$/, "", line)
        if (line == "" || substr(line, 1, 1) == "#") next
        eq = index(line, "=")
        if (eq == 0) next
        k = substr(line, 1, eq - 1)
        sub(/^[ \t\r\v\f]+/, "", k)
        sub(/[ \t\r\v\f]+$/, "", k)
        if (key != "") {
            if (k != key) next
            found = 1
            val = substr(line, eq + 1)
            next
        }
        if (k !~ /^[A-Za-z_][A-Za-z0-9_]*$/) next
        # _JASPER_-prefixed keys belong to the lib itself (include guard,
        # parser); jasper_env_file_export must never re-export one, or a
        # child that later sources the lib would find the guard already set
        # and define nothing.
        if (k ~ /^_JASPER_/) next
        if (!(k in all)) order[++cnt] = k
        all[k] = substr(line, eq + 1)
    }
    END {
        if (key != "") {
            if (!found) exit 1
            print unquote(val)
            exit 0
        }
        for (i = 1; i <= cnt; i++) print order[i] "\n" unquote(all[order[i]])
    }
'

# jasper_env_file_get FILE KEY
# Print the LAST `KEY=` line's value; return 1 with no output when FILE
# or the key is absent.
jasper_env_file_get() {
    local file="$1" key="$2" value

    # An empty key is _JASPER_ENV_FILE_AWK's "print every key" sentinel;
    # forwarding one here would hand the caller the whole file with rc 0.
    [[ -n "$key" ]] || return 1
    [[ -r "$file" ]] || return 1
    value="$(awk -v key="$key" "$_JASPER_ENV_FILE_AWK" "$file")" || return 1
    printf '%s\n' "$value"
}

# jasper_env_file_export FILE
# Export every `KEY=value` assignment in FILE into the environment, the
# way `set -a; source FILE; set +a` did — but WITHOUT the shell ever
# seeing the values: a `$(…)`, backtick, space or `#` in an operator-
# pasted value is data, not code. No-op when FILE is absent. A line
# whose key is not a plain identifier is skipped (`source` would have
# run it as a command); so is a key beginning with `_JASPER_` (this lib's
# own guard/parser state).
# The locals are `_jef_`-prefixed: bash scopes dynamically, so a local named
# like a key in the parsed file would shadow it and `export KEY=` would land
# on the local instead of the environment.
jasper_env_file_export() {
    local _jef_file="$1" _jef_key _jef_value _jef_parsed

    [[ -r "$_jef_file" ]] || return 0
    # Captured, not process-substituted: `pipefail` cannot see into a process
    # substitution, so an awk that never ran (or died mid-file) would leave
    # the caller reconciling against a blank world with rc 0. The trailing
    # `printf x` survives $()'s newline strip so a LAST key whose value is
    # empty still arrives as two lines.
    _jef_parsed="$(awk -v key="" "$_JASPER_ENV_FILE_AWK" "$_jef_file" && printf x)" \
        || return 1
    while IFS= read -r _jef_key && IFS= read -r _jef_value; do
        export "${_jef_key}=${_jef_value}"
    done <<<"${_jef_parsed%x}"
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
