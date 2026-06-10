#!/usr/bin/env bash
# Shared env-file quoting + atomic single-key writer for the JTS
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

# jasper_env_file_set FILE KEY VALUE [FILE_MODE] [DIR_MODE]
# Atomic (tempfile + rename) single-key upsert: replaces the first
# KEY= line in FILE (dropping duplicates) or appends one. Modes
# default to the historical jasper-aec-reconcile posture (0600 file,
# 0755 dir); callers with a different posture pass theirs explicitly.
jasper_env_file_set() {
    local file="$1" key="$2" value="$3"
    local file_mode="${4:-0600}" dir_mode="${5:-0755}"
    local dir tmp quoted

    dir="$(dirname "$file")"
    install -d -m "$dir_mode" "$dir"
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

    chmod "$file_mode" "$tmp"
    mv "$tmp" "$file"
}
