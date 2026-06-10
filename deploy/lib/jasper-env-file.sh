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
# restart churn. Single-quote wrapping is stable across bash versions
# and is read identically by `source` and EnvironmentFile=.
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
            printf "'"
            rest="$value"
            # Emit the '\'' idiom via %s ARGUMENTS, never via the printf
            # FORMAT string: bash printf interprets backslash escapes in
            # the format, so a format-embedded \' silently drops the
            # backslash and emits a malformed quote run (latent bug in
            # the pre-lib PR #534 copy of this loop).
            while [[ "$rest" == *"'"* ]]; do
                printf '%s%s' "${rest%%\'*}" "'\''"
                rest="${rest#*\'}"
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
        awk -v key="$key" -v line="${key}=${quoted}" '
            $0 ~ "^[[:space:]]*" key "=" {
                if (!done) {
                    print line
                    done = 1
                }
                next
            }
            { print }
            END {
                if (!done) {
                    print line
                }
            }
        ' "$file" > "$tmp"
    else
        printf '%s=%s\n' "$key" "$quoted" > "$tmp"
    fi

    chmod "$file_mode" "$tmp"
    mv "$tmp" "$file"
}
