#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Format, type-check, and lint every Rust crate in the CI Rust gate. On
# macOS, ALSA-backed crates are checked against a temporary pkg-config stub
# while targeting Linux; Cargo does not link binaries in this lane. Rust unit
# tests do link, so the complete Rust test gate still requires Linux or CI.

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"
workflow="${repo_root}/.github/workflows/tests.yml"

rust_crates=(
  rust/jasper-env
  rust/jasper-daemon
  rust/jasper-clock
  rust/jasper-resampler
  rust/jasper-ring
  rust/jasper-host-clock
  rust/jasper-tts-protocol
  rust/jasper-fanin
  rust/jasper-outputd
)
host_clock_crate="rust/jasper-host-clock"

die() {
  printf 'check-rust: %s\n' "$*" >&2
  exit 1
}

[[ -f "${workflow}" ]] || die "workflow not found: ${workflow}"

# RUST_TOOLCHAIN is deliberately owned by tests.yml. Fail closed if that
# single source of truth disappears or becomes ambiguous.
toolchain_matches="$(
  awk '
    $1 == "RUST_TOOLCHAIN:" {
      value = $2
      sub(/^\"/, "", value)
      sub(/\"$/, "", value)
      sub(/^\047/, "", value)
      sub(/\047$/, "", value)
      print value
    }
  ' "${workflow}"
)"
toolchain_count="$(printf '%s\n' "${toolchain_matches}" | awk 'NF { count += 1 } END { print count + 0 }')"
[[ "${toolchain_count}" == "1" ]] || \
  die "expected exactly one RUST_TOOLCHAIN pin in ${workflow}; found ${toolchain_count}"
rust_toolchain="$(printf '%s\n' "${toolchain_matches}" | awk 'NF { print; exit }')"
[[ -n "${rust_toolchain}" ]] || die "RUST_TOOLCHAIN pin in ${workflow} is empty"

command -v rustup >/dev/null 2>&1 || \
  die "rustup is not installed or not on PATH. Install Rust via https://rustup.rs/ and rerun scripts/check-rust.sh"
command -v pkg-config >/dev/null 2>&1 || \
  die "pkg-config is not installed. Install it with: brew install pkg-config (macOS) or apt-get install pkg-config (Debian/Ubuntu)"

if ! rustup which --toolchain "${rust_toolchain}" cargo >/dev/null 2>&1; then
  die "Rust toolchain ${rust_toolchain} is not installed. Install it with: rustup toolchain install ${rust_toolchain} --profile minimal --component rustfmt --component clippy"
fi

missing_components=""
if ! rustup which --toolchain "${rust_toolchain}" rustfmt >/dev/null 2>&1; then
  missing_components="rustfmt"
fi
if ! rustup which --toolchain "${rust_toolchain}" cargo-clippy >/dev/null 2>&1; then
  missing_components="${missing_components:+${missing_components} }clippy"
fi
if [[ -n "${missing_components}" ]]; then
  die "Rust toolchain ${rust_toolchain} is missing ${missing_components}. Install the required components with: rustup component add --toolchain ${rust_toolchain} rustfmt clippy"
fi

host_os="$(uname -s)"
host_arch="$(uname -m)"
linux_target=""
case "${host_os}" in
  Linux)
    if ! pkg-config --exists alsa; then
      die "ALSA development metadata is missing. Install libasound2-dev (Debian/Ubuntu) or alsa-lib-devel (Fedora), then rerun scripts/check-rust.sh"
    fi
    ;;
  Darwin)
    case "${host_arch}" in
      arm64|aarch64)
        linux_target="aarch64-unknown-linux-gnu"
        ;;
      x86_64|amd64)
        linux_target="x86_64-unknown-linux-gnu"
        ;;
      *)
        die "unsupported macOS architecture ${host_arch}; add its Linux Rust target mapping before using this script"
        ;;
    esac
    if ! rustup target list --toolchain "${rust_toolchain}" --installed | grep -Fxq "${linux_target}"; then
      die "Rust target ${linux_target} is not installed for ${rust_toolchain}. Install it with: rustup target add --toolchain ${rust_toolchain} ${linux_target}"
    fi
    ;;
  *)
    die "unsupported host ${host_os}/${host_arch}; run the Rust gate on Linux or add an explicit host mapping"
    ;;
esac

for crate in "${rust_crates[@]}"; do
  [[ -f "${repo_root}/${crate}/Cargo.toml" ]] || die "missing Rust crate manifest: ${crate}/Cargo.toml"
done

stub_dir=""
cleanup() {
  status=$?
  trap - EXIT
  if [[ -n "${stub_dir}" && -d "${stub_dir}" ]]; then
    if ! rm -rf -- "${stub_dir}"; then
      printf 'check-rust: warning: could not remove temporary directory %s\n' \
        "${stub_dir}" >&2
    fi
  fi
  exit "${status}"
}
trap cleanup EXIT

cargo_env=()
target_args=()
if [[ -n "${linux_target}" ]]; then
  stub_dir="$(mktemp -d "${TMPDIR:-/tmp}/jts-rust-check.XXXXXX")"
  cat > "${stub_dir}/alsa.pc" <<'EOF'
prefix=/nonexistent
exec_prefix=${prefix}
libdir=${exec_prefix}/lib
includedir=${prefix}/include

Name: alsa
Description: stub for Cargo check and Clippy only (no link)
Version: 1.2.11
Libs: -L${libdir} -lasound
Cflags: -I${includedir}
EOF

  cargo_env=(
    "PKG_CONFIG_ALLOW_CROSS=1"
    "PKG_CONFIG_ALLOW_CROSS_${linux_target}=1"
    "PKG_CONFIG_PATH_${linux_target}=${stub_dir}"
    "PKG_CONFIG_LIBDIR_${linux_target}=${stub_dir}"
  )
  target_args=(--target "${linux_target}")
fi

printf '==> Rust format + Clippy (toolchain %s, host %s/%s' \
  "${rust_toolchain}" "${host_os}" "${host_arch}"
if [[ -n "${linux_target}" ]]; then
  printf ', cross target %s' "${linux_target}"
fi
printf ')\n'

for crate in "${rust_crates[@]}"; do
  printf '==> rustfmt: %s\n' "${crate}"
  (
    cd "${repo_root}/${crate}"
    rustup run "${rust_toolchain}" cargo fmt --all -- --check
  )
done

for crate in "${rust_crates[@]}"; do
  printf '==> Clippy: %s\n' "${crate}"
  clippy_args=(
    clippy
    --release
    --locked
    --all-targets
  )
  if [[ "${crate}" == "${host_clock_crate}" ]]; then
    clippy_args+=(--all-features)
  fi
  clippy_args+=("${target_args[@]}" -- --no-deps -D warnings)
  (
    cd "${repo_root}/${crate}"
    if [[ "${#cargo_env[@]}" -gt 0 ]]; then
      env "${cargo_env[@]}" rustup run "${rust_toolchain}" cargo \
        "${clippy_args[@]}"
    else
      rustup run "${rust_toolchain}" cargo "${clippy_args[@]}"
    fi
  )
done

printf '%s\n' 'Rust formatting and Clippy passed.'
printf '%s\n' 'This lane type-checks and lints only; run linked Rust unit tests on Linux or in CI.'
