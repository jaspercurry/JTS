# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Build the WebRTC AEC3 pybind11 extension(s).

Builds two parallel pybind11 modules:

- `jasper_aec3._aec3` — links against Debian Trixie's
  apt-installed `libwebrtc-audio-processing-1` (v1.3-3). Used by
  jasper-aec-bridge as the legacy AEC engine. Always built.

- `jasper_aec3._aec3_v2` — links statically against vendored
  webrtc-audio-processing v2.1 (Pulseaudio's upstream fork). Used
  by jasper-aec-bridge as the BEST_A AEC engine — exposes the deep
  `EchoCanceller3Config` knobs the v1 binding can't reach.
  Conditionally built when `WEBRTC_AEC3_V2_PREFIX` env var is set
  (install.sh sets it after a successful vendored build); skipped
  on systems where the v2 build hasn't happened (e.g. dev laptops
  running pytest, fresh Pis before install.sh has reached the
  vendored-build step).

The v2 binding is the production target post-2026-05-22; the v1
binding is preserved as a fallback / sanity-check engine. See
docs/HANDOFF-mic-quality-v2.md "Triple-stream architecture plan"
for context, and experiments/aec3-v2-deep-tune-spike/README.md
for per-knob rationale.

System prereqs (apt-installed by deploy/install.sh):
- v1: libwebrtc-audio-processing-dev, pkg-config, build-essential
- v2: meson, ninja-build, build-essential (vendored source is
      cloned + built by install.sh into WEBRTC_AEC3_V2_PREFIX)
"""
from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

from pybind11.setup_helpers import Pybind11Extension, build_ext
from setuptools import setup


def _pkg_config(*flags: str, pkg: str = "webrtc-audio-processing-1") -> list[str]:
    out = subprocess.check_output(
        ["pkg-config", *flags, pkg], text=True
    ).strip()
    return shlex.split(out)


def _build_v1_extension() -> Pybind11Extension:
    """Legacy binding linking against the apt-installed v1.3-3 library."""
    cflags = _pkg_config("--cflags")
    libs = _pkg_config("--libs")
    include_dirs = [a[2:] for a in cflags if a.startswith("-I")]
    extra_compile_args = [a for a in cflags if not a.startswith("-I")]
    library_dirs = [a[2:] for a in libs if a.startswith("-L")]
    libraries = [a[2:] for a in libs if a.startswith("-l")]
    extra_link_args = [
        a for a in libs if not (a.startswith("-L") or a.startswith("-l"))
    ]
    return Pybind11Extension(
        "jasper_aec3._aec3",
        sources=["src/aec3_binding.cpp"],
        include_dirs=include_dirs,
        library_dirs=library_dirs,
        libraries=libraries,
        extra_compile_args=extra_compile_args + ["-O3"],
        extra_link_args=extra_link_args,
        cxx_std=17,
    )


_ABSL_MODULES = [
    # Top-level absl modules webrtc-audio-processing-2 depends on.
    # pkg-config will expand each to its transitive subpackages.
    "absl_base", "absl_strings", "absl_synchronization",
    "absl_time", "absl_hash", "absl_log", "absl_flags",
    "absl_container", "absl_debugging", "absl_status",
    "absl_numeric", "absl_random",
]


def _absl_via_pkgconfig() -> tuple[list[str], list[str]]:
    """Discover system abseil link flags via pkg-config.

    Returns (library_dirs, library_names) — both already stripped of
    the leading -L / -l prefixes so they fit setuptools' parameters.
    On Pi (Debian Trixie), abseil-cpp is shipped as libabsl-dev and
    meson detects it via pkg-config, so its subproject build isn't
    triggered → no static archives to link directly. We discover the
    flags ourselves and rely on the system shared libs at runtime.
    """
    lib_dirs: list[str] = []
    libs: list[str] = []
    extra_link: list[str] = []
    for module in _ABSL_MODULES:
        try:
            out = subprocess.check_output(
                ["pkg-config", "--libs", module],
                text=True, stderr=subprocess.DEVNULL,
            ).strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
        for tok in shlex.split(out):
            if tok.startswith("-L"):
                d = tok[2:]
                if d and d not in lib_dirs:
                    lib_dirs.append(d)
            elif tok.startswith("-l"):
                name = tok[2:]
                if name and name not in libs:
                    libs.append(name)
            else:
                # E.g. -Wl,--push-state, -latomic comes through cleanly above.
                if tok not in extra_link:
                    extra_link.append(tok)
    return lib_dirs, libs, extra_link


def _build_v2_extension(prefix: Path) -> Pybind11Extension:
    """BEST_A binding linking statically against vendored v2.1.

    `prefix` is the WEBRTC_AEC3_V2_PREFIX directory containing both
    the source tree and the meson `builddir`. install.sh clones +
    builds it; we just compile the binding against the static archive
    + source-tree headers (internal EchoCanceller3 header is NOT
    installed by meson, so we read it directly from the source tree).

    Abseil handling — two cases:
      - meson built abseil as a subproject (laptop case, when no system
        absl is available) → static archives at
        `${bld}/subprojects/abseil-cpp-*/libabsl_*.a`. Link those.
      - system absl is available via pkg-config (Pi case via apt
        libabsl-dev) → meson skipped the subproject build. Link via
        `-labsl_*` flags.

    Layout assumed:
      ${prefix}/src/                  — webrtc-audio-processing v2.1 source clone
      ${prefix}/src/builddir/         — meson build dir with static archive
    """
    src = prefix / "src"
    bld = src / "builddir"
    static_lib = bld / "webrtc" / "modules" / "audio_processing" / "libwebrtc-audio-processing-2.a"
    if not static_lib.is_file():
        raise RuntimeError(
            f"vendored v2.1 static archive not found at {static_lib} — "
            f"WEBRTC_AEC3_V2_PREFIX={prefix} appears incomplete. "
            "install.sh's vendored build step should have produced this."
        )

    # Did meson build abseil as a subproject? Glob for any version.
    absl_subprojects = sorted(bld.glob("subprojects/abseil-cpp-*"))
    static_absl_libs: list[str] = []
    extra_include: list[str] = []
    extra_link_args: list[str] = ["-lpthread"]
    library_dirs: list[str] = []
    library_names: list[str] = []

    if absl_subprojects:
        # Laptop case — link static archives.
        absl_dir = absl_subprojects[0]
        static_absl_libs = sorted(str(p) for p in absl_dir.glob("libabsl_*.a"))
        if not static_absl_libs:
            raise RuntimeError(
                f"absl subproject dir exists at {absl_dir} but no static "
                "archives inside — meson build looks incomplete"
            )
        # Include paths for absl headers (subproject)
        extra_include += [str(absl_dir), str(src / "subprojects" / absl_dir.name)]
    else:
        # Pi case — system absl via pkg-config.
        lib_dirs, libs, pc_extra = _absl_via_pkgconfig()
        if not libs:
            raise RuntimeError(
                "no abseil static archives under "
                f"{bld}/subprojects/abseil-cpp-* and pkg-config has no "
                "system absl either — meson v2.1 build looks incomplete "
                "(or libabsl-dev is missing)"
            )
        library_dirs.extend(lib_dirs)
        library_names.extend(libs)
        extra_link_args.extend(pc_extra)

    include_dirs = [
        str(bld),
        str(src),
        str(bld / "webrtc"),
        str(src / "webrtc"),
    ] + extra_include

    extra_compile_args = [
        "-O3",
        # Required by libwebrtc internal headers:
        "-DWEBRTC_LIBRARY_IMPL",
        "-DWEBRTC_POSIX",
        "-DWEBRTC_LINUX",
        # AEC3 debug-dump must be explicitly 0 or 1 (we don't need it):
        "-DWEBRTC_APM_DEBUG_DUMP=0",
    ]

    return Pybind11Extension(
        "jasper_aec3._aec3_v2",
        sources=["src/aec3_binding_v2.cpp"],
        include_dirs=include_dirs,
        library_dirs=library_dirs,
        libraries=library_names,
        extra_compile_args=extra_compile_args,
        # Static archives (webrtc + optionally subproject absl)
        extra_objects=[str(static_lib), *static_absl_libs],
        extra_link_args=extra_link_args,
        cxx_std=17,
    )


def _build_extensions() -> list[Pybind11Extension]:
    extensions = [_build_v1_extension()]

    v2_prefix = os.environ.get("WEBRTC_AEC3_V2_PREFIX", "").strip()
    if v2_prefix:
        v2_prefix_path = Path(v2_prefix)
        if v2_prefix_path.is_dir():
            try:
                extensions.append(_build_v2_extension(v2_prefix_path))
                print(f"setup.py: building _aec3_v2 from {v2_prefix_path}")
            except RuntimeError as exc:
                print(
                    f"setup.py: WEBRTC_AEC3_V2_PREFIX is set but v2 build "
                    f"prereqs not met — skipping _aec3_v2: {exc}"
                )
        else:
            print(
                f"setup.py: WEBRTC_AEC3_V2_PREFIX={v2_prefix} not a "
                "directory — skipping _aec3_v2 extension"
            )
    else:
        print(
            "setup.py: WEBRTC_AEC3_V2_PREFIX not set — building v1 binding "
            "only. (install.sh sets this after vendored v2.1 build.)"
        )

    return extensions


setup(ext_modules=_build_extensions(), cmdclass={"build_ext": build_ext})
