#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Optional, local-only consumption seam for the verified first-party ARM64
# runtime bundle. Image/bootstrap work can stage an extracted bundle and set
# JASPER_FIRST_PARTY_RUNTIME_BUNDLE without teaching install.sh a mutable URL.
#
# Contract:
#   - unset/empty: no-op; existing source builds remain the fallback;
#   - set: verification and installation are fail-closed (never silently fall
#     back to compiling different bytes after a bad/tampered bundle);
#   - all files stage next to their destinations before any existing file is
#     replaced; a replacement failure rolls already-replaced files back;
#   - the complete self-verifying bundle (including notices/licenses) is
#     preserved under /opt/jasper/share/first-party-runtime/bundle; an
#     INSTALLED marker appears atomically with that complete tree and is the
#     only success marker.
#
# Functions assume install.sh globals (REPO_DIR) and set -euo pipefail from the
# sourcing shell. python3, binutils/readelf, and install(1) are available after
# install_deps/install_streambox_deps, before the Rust build functions invoke
# this seam.

FIRST_PARTY_RUNTIME_PREPARED=0
FIRST_PARTY_RUNTIME_INSTALLED_IDS=""
# Test-only/internal path injection; product callers leave this unset.
FIRST_PARTY_RUNTIME_MARKER_PARENT="${FIRST_PARTY_RUNTIME_MARKER_PARENT:-/opt/jasper/share}"

first_party_runtime_artifact_installed() {
    local artifact_id="$1"
    [[ " ${FIRST_PARTY_RUNTIME_INSTALLED_IDS} " == *" ${artifact_id} "* ]]
}

_first_party_runtime_expected_source_sha() {
    local source_sha="${JASPER_DEPLOY_SHA_FULL:-}"
    if [[ -n "${source_sha}" ]]; then
        if [[ "${source_sha}" == *-dirty ]]; then
            echo "  ERROR: a first-party runtime bundle cannot install over a dirty source deployment (${source_sha})" >&2
            return 1
        fi
        if [[ ! "${source_sha}" =~ ^[0-9a-f]{40}$ ]]; then
            echo "  ERROR: JASPER_DEPLOY_SHA_FULL must be an exact lowercase 40-character Git SHA for bundle installation (got ${source_sha@Q})" >&2
            return 1
        fi
        printf '%s\n' "${source_sha}"
        return 0
    fi

    # A Pi-local install can derive identity from a clean checkout. Bootstrap
    # and image installs normally have no .git directory, so they MUST pass
    # their baked/pinned commit as JASPER_DEPLOY_SHA_FULL.
    if ! command -v git >/dev/null 2>&1; then
        echo "  ERROR: source revision is unknown; set JASPER_DEPLOY_SHA_FULL to the bundle's exact commit" >&2
        return 1
    fi
    if ! source_sha="$(git -C "${REPO_DIR}" rev-parse HEAD 2>/dev/null)"; then
        echo "  ERROR: source revision is unknown; set JASPER_DEPLOY_SHA_FULL to the bundle's exact commit" >&2
        return 1
    fi
    if [[ ! "${source_sha}" =~ ^[0-9a-f]{40}$ ]]; then
        echo "  ERROR: git returned an invalid source revision (${source_sha@Q})" >&2
        return 1
    fi
    local dirty
    if ! dirty="$(git -C "${REPO_DIR}" status --porcelain 2>/dev/null)"; then
        echo "  ERROR: could not verify that source revision ${source_sha} is clean" >&2
        return 1
    fi
    if [[ -n "${dirty}" ]]; then
        echo "  ERROR: a first-party runtime bundle cannot install over a dirty source checkout" >&2
        return 1
    fi
    printf '%s\n' "${source_sha}"
}

_first_party_runtime_cleanup_paths() {
    local path
    for path in "$@"; do
        if [[ -n "${path}" ]]; then
            rm -f -- "${path}"
        fi
    done
}

_first_party_runtime_retire_provenance_for_source_build() {
    local marker_parent="$1"
    local marker_dir="${marker_parent}/first-party-runtime"
    local retired_dir="${marker_parent}/first-party-runtime-superseded"
    if [[ ! -e "${marker_dir}" ]]; then
        return 0
    fi

    # Source compilation is about to replace some or all of the coupled files.
    # Atomically remove the old tree from the "current" path before that first
    # replacement so INSTALLED can never describe bytes from a prior bundle.
    rm -rf -- "${retired_dir}"
    if ! mv -f -- "${marker_dir}" "${retired_dir}"; then
        echo "  ERROR: could not retire stale first-party runtime provenance before source build" >&2
        return 1
    fi
    rm -f -- "${retired_dir}/INSTALLED"
    if ! printf '%s\n' \
            'Superseded: the installer selected source builds; bundle/BUILD-INFO.json is historical only.' \
            >"${retired_dir}/SUPERSEDED"; then
        echo "  ERROR: could not mark prior first-party runtime provenance as superseded" >&2
        return 1
    fi
    chmod 0644 "${retired_dir}/SUPERSEDED"
    if ! sync -f "${marker_parent}"; then
        echo "  ERROR: could not durably retire stale first-party runtime provenance" >&2
        return 1
    fi
}

_first_party_runtime_finish_committed_cleanup() {
    local marker_parent="$1"
    local committed="$2"
    local marker_stage="${marker_parent}/.first-party-runtime-stage"
    local marker_backup="${marker_parent}/.first-party-runtime-backup"
    local marker_seen=0
    local -a destinations=()
    local -a backups=()
    local kind first second third extra
    while IFS=$'\t' read -r kind first second third extra; do
        case "${kind}" in
            marker)
                if [[ "${marker_seen}" == "1" \
                      || ( "${first}" != "0" && "${first}" != "1" ) \
                      || -n "${second}${third}${extra}" ]]; then
                    echo "  ERROR: malformed marker row in ${committed}; refusing unsafe cleanup" >&2
                    return 1
                fi
                marker_seen=1
                ;;
            file)
                if [[ "${first}" != /* \
                      || "${second}" != "${first}.jts-runtime-backup" \
                      || ( "${third}" != "0" && "${third}" != "1" ) \
                      || -n "${extra}" ]]; then
                    echo "  ERROR: malformed file row in ${committed}; refusing unsafe cleanup" >&2
                    return 1
                fi
                destinations+=("${first}")
                backups+=("${second}")
                ;;
            *)
                echo "  ERROR: unknown row in ${committed}; refusing unsafe cleanup" >&2
                return 1
                ;;
        esac
    done <"${committed}"
    if [[ "${marker_seen}" != "1" || "${#destinations[@]}" -eq 0 ]]; then
        echo "  ERROR: incomplete ${committed}; refusing unsafe cleanup" >&2
        return 1
    fi

    local index
    for index in "${!destinations[@]}"; do
        rm -f -- \
            "${destinations[$index]}.jts-runtime-stage" \
            "${destinations[$index]}.jts-runtime-restore" \
            "${backups[$index]}"
        if ! sync -f "$(dirname "${destinations[$index]}")"; then
            echo "  ERROR: could not durably clean ${destinations[$index]} transaction files" >&2
            return 1
        fi
    done
    rm -rf -- "${marker_stage}" "${marker_backup}"
    if ! sync -f "${marker_parent}"; then
        echo "  ERROR: could not durably clean first-party runtime transaction state" >&2
        return 1
    fi
    rm -f -- "${committed}"
    if ! sync -f "${marker_parent}"; then
        # Cleanup is idempotent. If the deletion was not persisted, a later
        # invocation may see the committed marker again and safely repeat it.
        echo "  ERROR: could not durably finish first-party runtime cleanup" >&2
        return 1
    fi
}

_first_party_runtime_recover_transaction() {
    local marker_parent="$1"
    local marker_dir="${marker_parent}/first-party-runtime"
    local marker_stage="${marker_parent}/.first-party-runtime-stage"
    local marker_backup="${marker_parent}/.first-party-runtime-backup"
    local journal="${marker_parent}/.first-party-runtime-transaction"
    local committed="${journal}.committed"

    if [[ -f "${journal}" && -f "${committed}" ]]; then
        echo "  ERROR: both rollback and committed transaction records exist; refusing ambiguous recovery" >&2
        return 1
    fi
    if [[ ! -f "${journal}" && -f "${committed}" ]]; then
        echo "  finishing committed first-party runtime transaction cleanup"
        _first_party_runtime_finish_committed_cleanup \
            "${marker_parent}" "${committed}"
        return $?
    fi

    # No journal means no runtime destination was touched. Clean or recover
    # harmless leftovers from a crash before the transaction commit point.
    if [[ ! -f "${journal}" ]]; then
        rm -rf -- "${marker_stage}"
        if [[ -e "${marker_backup}" ]]; then
            if [[ -e "${marker_dir}" ]]; then
                rm -rf -- "${marker_backup}"
            else
                mv -f -- "${marker_backup}" "${marker_dir}"
            fi
        fi
        return 0
    fi

    echo "  recovering interrupted first-party runtime transaction"
    local marker_existed=""
    local -a destinations=()
    local -a backups=()
    local -a existed=()
    local kind first second third extra
    while IFS=$'\t' read -r kind first second third extra; do
        case "${kind}" in
            marker)
                if [[ -n "${marker_existed}" || ( "${first}" != "0" && "${first}" != "1" ) || -n "${second}${third}${extra}" ]]; then
                    echo "  ERROR: malformed marker row in ${journal}; refusing unsafe recovery" >&2
                    return 1
                fi
                marker_existed="${first}"
                ;;
            file)
                if [[ "${first}" != /* || "${second}" != "${first}.jts-runtime-backup" || ( "${third}" != "0" && "${third}" != "1" ) || -n "${extra}" ]]; then
                    echo "  ERROR: malformed file row in ${journal}; refusing unsafe recovery" >&2
                    return 1
                fi
                destinations+=("${first}")
                backups+=("${second}")
                existed+=("${third}")
                ;;
            *)
                echo "  ERROR: unknown row in ${journal}; refusing unsafe recovery" >&2
                return 1
                ;;
        esac
    done <"${journal}"
    if [[ -z "${marker_existed}" || "${#destinations[@]}" -eq 0 ]]; then
        echo "  ERROR: incomplete ${journal}; refusing unsafe recovery" >&2
        return 1
    fi

    local index failed=0 restore_path
    for ((index = "${#destinations[@]}" - 1; index >= 0; index--)); do
        rm -f -- "${destinations[$index]}.jts-runtime-stage"
        if [[ "${existed[$index]}" == "1" ]]; then
            if [[ ! -f "${backups[$index]}" ]]; then
                echo "  ERROR: transaction backup missing: ${backups[$index]}" >&2
                failed=1
                continue
            fi
            # Retain the backup until recovery itself is durably committed.
            # A power loss halfway through recovery can therefore retry the
            # same journal instead of finding that earlier backups were moved
            # away by the first attempt.
            restore_path="${destinations[$index]}.jts-runtime-restore"
            rm -f -- "${restore_path}"
            if ! cp -a -- "${backups[$index]}" "${restore_path}"; then
                echo "  ERROR: could not stage restore for ${destinations[$index]}" >&2
                failed=1
                continue
            fi
            if ! mv -f -- "${restore_path}" "${destinations[$index]}"; then
                echo "  ERROR: could not restore ${destinations[$index]}" >&2
                failed=1
            fi
        else
            rm -f -- "${destinations[$index]}" "${backups[$index]}"
        fi
    done
    if [[ "${marker_existed}" == "1" ]]; then
        if [[ ! -d "${marker_backup}" ]]; then
            echo "  ERROR: transaction provenance backup is missing: ${marker_backup}" >&2
            failed=1
        else
            local marker_restore="${marker_parent}/.first-party-runtime-restore"
            rm -rf -- "${marker_restore}"
            if ! cp -a -- "${marker_backup}" "${marker_restore}"; then
                echo "  ERROR: could not stage prior runtime provenance restore" >&2
                failed=1
            else
                rm -rf -- "${marker_dir}"
            fi
            if [[ "${failed}" == "0" ]] \
               && ! mv -f -- "${marker_restore}" "${marker_dir}"; then
                echo "  ERROR: could not restore prior runtime provenance" >&2
                failed=1
            fi
        fi
    else
        rm -rf -- "${marker_dir}" "${marker_backup}"
    fi
    rm -rf -- "${marker_stage}"
    if [[ "${failed}" == "1" ]]; then
        # Keep the journal: the next run must retry recovery, never interpret a
        # partially-restored set as committed.
        return 1
    fi

    # Persist the restored destinations and provenance before transitioning
    # the rollback journal into the same idempotent cleanup-only state used by
    # a successful install.
    for index in "${!destinations[@]}"; do
        if ! sync -f "$(dirname "${destinations[$index]}")"; then
            echo "  ERROR: could not durably restore ${destinations[$index]}" >&2
            return 1
        fi
    done
    if ! sync -f "${marker_parent}"; then
        echo "  ERROR: could not durably restore first-party runtime provenance" >&2
        return 1
    fi
    if ! mv -f -- "${journal}" "${committed}"; then
        echo "  ERROR: could not commit first-party runtime recovery cleanup" >&2
        return 1
    fi
    if ! sync -f "${marker_parent}"; then
        echo "  ERROR: could not durably commit first-party runtime recovery cleanup" >&2
        return 1
    fi
    _first_party_runtime_finish_committed_cleanup \
        "${marker_parent}" "${committed}"
}

prepare_first_party_runtime_bundle() {
    if [[ "${FIRST_PARTY_RUNTIME_PREPARED}" == "1" ]]; then
        return 0
    fi
    if [[ "${FIRST_PARTY_RUNTIME_PREPARED}" == "-1" ]]; then
        return 1
    fi

    local marker_parent="${FIRST_PARTY_RUNTIME_MARKER_PARENT}"
    local marker_dir="${marker_parent}/first-party-runtime"
    local marker_stage="${marker_parent}/.first-party-runtime-stage"
    local marker_backup="${marker_parent}/.first-party-runtime-backup"
    local journal="${marker_parent}/.first-party-runtime-transaction"
    local committed="${journal}.committed"
    install -d -m 0755 "${marker_parent}"
    if ! _first_party_runtime_recover_transaction "${marker_parent}"; then
        FIRST_PARTY_RUNTIME_PREPARED=-1
        return 1
    fi

    local input_bundle="${JASPER_FIRST_PARTY_RUNTIME_BUNDLE:-}"
    if [[ -z "${input_bundle}" ]]; then
        if ! _first_party_runtime_retire_provenance_for_source_build \
                "${marker_parent}"; then
            FIRST_PARTY_RUNTIME_PREPARED=-1
            return 1
        fi
        FIRST_PARTY_RUNTIME_PREPARED=1
        return 0
    fi
    # A configured bundle is fail-closed. Leave this sentinel in place on
    # every early return so even a caller that explicitly catches the first
    # failure cannot later fall through to source builds.
    FIRST_PARTY_RUNTIME_PREPARED=-1
    if [[ ! -d "${input_bundle}" ]]; then
        echo "  ERROR: JASPER_FIRST_PARTY_RUNTIME_BUNDLE is not an extracted bundle directory: ${input_bundle}" >&2
        return 1
    fi

    local verifier="${REPO_DIR}/scripts/verify-first-party-arm64-release.py"
    if [[ ! -x "${verifier}" ]]; then
        echo "  ERROR: first-party runtime verifier is missing or not executable: ${verifier}" >&2
        return 1
    fi
    local expected_source_sha
    if ! expected_source_sha="$(_first_party_runtime_expected_source_sha)"; then
        return 1
    fi

    # Snapshot the untrusted/mutable input ONCE into a root-only adjacent tree.
    # Verification, artifact installation, and preserved provenance all consume
    # this exact snapshot. Nothing reads input_bundle after the copy, closing
    # the verify -> consume TOCTOU window.
    rm -rf -- "${marker_stage}" "${marker_backup}"
    install -d -m 0700 "${marker_stage}/bundle"
    if ! cp -a -- "${input_bundle}/." "${marker_stage}/bundle/"; then
        rm -rf -- "${marker_stage}"
        echo "  ERROR: could not snapshot first-party runtime bundle" >&2
        return 1
    fi
    # cp -a SOURCE/. normally transfers the source-root attributes onto the
    # precreated destination. Make that contract explicit across coreutils
    # versions while preserving (not correcting) an invalid input mode; the
    # verifier below requires the canonical 0755 root.
    chmod --reference="${input_bundle}" "${marker_stage}/bundle"
    chown -R root:root "${marker_stage}"
    local bundle="${marker_stage}/bundle"

    echo "  verifying private first-party ARM64 runtime snapshot: ${bundle}"
    local plan_file
    plan_file="$(mktemp)"
    if ! "${verifier}" \
            --expected-source-sha "${expected_source_sha}" \
            --print-install-plan "${bundle}" >"${plan_file}"; then
        rm -f -- "${plan_file}"
        rm -rf -- "${marker_stage}"
        echo "  ERROR: staged first-party runtime bundle failed verification; refusing source-build fallback" >&2
        return 1
    fi

    local -a sources=()
    local -a destinations=()
    local -a modes=()
    local -a ids=()
    local relative destination mode artifact_id
    while IFS=$'\t' read -r relative destination mode artifact_id; do
        if [[ -z "${relative}" || -z "${destination}" || -z "${mode}" || -z "${artifact_id}" ]]; then
            rm -f -- "${plan_file}"
            rm -rf -- "${marker_stage}"
            echo "  ERROR: verified first-party runtime install plan contained an empty field" >&2
            return 1
        fi
        sources+=("${bundle}/${relative}")
        destinations+=("${destination}")
        modes+=("${mode}")
        ids+=("${artifact_id}")
    done <"${plan_file}"
    rm -f -- "${plan_file}"

    local -a staged=()
    local -a backups=()
    local -a existed=()
    local index staged_path backup_path
    for index in "${!ids[@]}"; do
        install -d -m 0755 "$(dirname "${destinations[$index]}")"
        staged_path="${destinations[$index]}.jts-runtime-stage"
        backup_path="${destinations[$index]}.jts-runtime-backup"
        rm -f -- "${staged_path}" "${backup_path}"
        if ! install -m "${modes[$index]}" -o root -g root \
                "${sources[$index]}" "${staged_path}"; then
            _first_party_runtime_cleanup_paths "${staged[@]}" "${staged_path}"
            rm -rf -- "${marker_stage}"
            echo "  ERROR: failed to stage ${ids[$index]} from verified runtime bundle" >&2
            return 1
        fi
        staged+=("${staged_path}")
        backups+=("${backup_path}")
        if [[ -e "${destinations[$index]}" ]]; then
            existed+=("1")
            if ! cp -a -- "${destinations[$index]}" "${backup_path}"; then
                _first_party_runtime_cleanup_paths "${staged[@]}" "${backups[@]}"
                rm -rf -- "${marker_stage}"
                echo "  ERROR: failed to back up ${destinations[$index]}" >&2
                return 1
            fi
        else
            existed+=("0")
        fi
    done

    # Back up the prior provenance before the journal commit. A crash before
    # the journal appears has changed no runtime destination and leaves only
    # harmless stage/backup files that the next run cleans.
    local marker_existed=0
    if [[ -e "${marker_dir}" ]]; then
        if ! cp -a -- "${marker_dir}" "${marker_backup}"; then
            _first_party_runtime_cleanup_paths "${staged[@]}" "${backups[@]}"
            rm -rf -- "${marker_stage}" "${marker_backup}"
            echo "  ERROR: failed to back up prior runtime provenance" >&2
            return 1
        fi
        marker_existed=1
    fi

    local journal_tmp="${journal}.tmp"
    rm -f -- "${journal_tmp}"
    {
        printf 'marker\t%s\n' "${marker_existed}"
        for index in "${!ids[@]}"; do
            printf 'file\t%s\t%s\t%s\n' \
                "${destinations[$index]}" "${backups[$index]}" "${existed[$index]}"
        done
    } >"${journal_tmp}"
    chown root:root "${journal_tmp}"
    chmod 0600 "${journal_tmp}"

    # Make staged replacements and backups durable before publishing a journal
    # that promises they are recoverable. sync -f flushes the filesystem
    # containing the named path; destinations may not share marker_parent's
    # filesystem on every supported layout.
    for index in "${!ids[@]}"; do
        if ! sync -f "${staged[$index]}"; then
            _first_party_runtime_cleanup_paths "${staged[@]}" "${backups[@]}"
            rm -rf -- "${marker_stage}" "${marker_backup}"
            rm -f -- "${journal_tmp}"
            echo "  ERROR: could not durably stage first-party runtime files" >&2
            return 1
        fi
        if [[ "${existed[$index]}" == "1" ]] \
           && ! sync -f "${backups[$index]}"; then
            _first_party_runtime_cleanup_paths "${staged[@]}" "${backups[@]}"
            rm -rf -- "${marker_stage}" "${marker_backup}"
            rm -f -- "${journal_tmp}"
            echo "  ERROR: could not durably back up first-party runtime files" >&2
            return 1
        fi
    done
    mv -f -- "${journal_tmp}" "${journal}"
    if ! sync -f "${journal}"; then
        echo "  ERROR: could not make the first-party runtime transaction journal durable" >&2
        _first_party_runtime_recover_transaction "${marker_parent}" || true
        return 1
    fi

    # The journal is durable before the first replacement. Any process/power
    # loss from here through provenance publication is deterministically
    # rolled back at the next invocation.
    for index in "${!ids[@]}"; do
        if ! mv -f -- "${staged[$index]}" "${destinations[$index]}"; then
            echo "  ERROR: failed to activate ${ids[$index]}; rolling back runtime bundle" >&2
            _first_party_runtime_recover_transaction "${marker_parent}" || true
            return 1
        fi
    done
    for index in "${!ids[@]}"; do
        if ! sync -f "${destinations[$index]}"; then
            echo "  ERROR: could not durably activate ${ids[$index]}; rolling back" >&2
            _first_party_runtime_recover_transaction "${marker_parent}" || true
            return 1
        fi
    done

    # The complete verified snapshot is already marker_stage/bundle. Write the
    # sole success marker only after all runtime files are active, then publish
    # that exact snapshot atomically. Nothing is recopied from input_bundle.
    if ! printf '%s\n' 'bundle/BUILD-INFO.json' >"${marker_stage}/INSTALLED"; then
        echo "  ERROR: failed to write runtime success marker; rolling back" >&2
        _first_party_runtime_recover_transaction "${marker_parent}" || true
        return 1
    fi
    chmod 0644 "${marker_stage}/INSTALLED"
    chmod 0755 "${marker_stage}"
    rm -rf -- "${marker_dir}"
    if ! mv -f -- "${marker_stage}" "${marker_dir}"; then
        echo "  ERROR: failed to publish complete runtime provenance; rolling back" >&2
        _first_party_runtime_recover_transaction "${marker_parent}" || true
        return 1
    fi
    if ! sync -f "${marker_parent}"; then
        echo "  ERROR: could not durably publish runtime provenance; rolling back" >&2
        _first_party_runtime_recover_transaction "${marker_parent}" || true
        return 1
    fi

    # Rename the rollback journal into an explicit committed-cleanup record.
    # The new runtime/provenance files were flushed above, so a power loss sees
    # either the old journal (roll back) or this record (keep new; clean up).
    # Artifact destination paths stay in that record rather than being
    # duplicated in shell cleanup code.
    if ! mv -f -- "${journal}" "${committed}"; then
        echo "  ERROR: could not commit first-party runtime transaction; rolling back" >&2
        _first_party_runtime_recover_transaction "${marker_parent}" || true
        return 1
    fi
    if ! sync -f "${marker_parent}"; then
        echo "  ERROR: runtime is active, but its committed cleanup state could not be durably flushed" >&2
        return 1
    fi
    if ! _first_party_runtime_finish_committed_cleanup \
            "${marker_parent}" "${committed}"; then
        return 1
    fi

    FIRST_PARTY_RUNTIME_INSTALLED_IDS="${ids[*]}"
    FIRST_PARTY_RUNTIME_PREPARED=1
    echo "  -> installed verified first-party runtime bundle (${FIRST_PARTY_RUNTIME_INSTALLED_IDS})"
}
