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


def _build_v2_extension(prefix: Path) -> Pybind11Extension:
    """BEST_A binding linking statically against vendored v2.1.

    `prefix` is the WEBRTC_AEC3_V2_PREFIX directory containing both
    the source tree and the meson `builddir`. install.sh clones +
    builds it; we just compile the binding against the static archive
    + source-tree headers (internal EchoCanceller3 header is NOT
    installed by meson, so we read it directly from the source tree).

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

    # Absl static archives (subproject of the webrtc-audio-processing build).
    # Globbed at build time so an absl version bump doesn't break us.
    absl_dir = bld / "subprojects" / "abseil-cpp-20240722.0"
    absl_libs = sorted(str(p) for p in absl_dir.glob("libabsl_*.a"))
    if not absl_libs:
        raise RuntimeError(
            f"absl static archives missing under {absl_dir} — "
            "v2.1 build looks incomplete"
        )

    include_dirs = [
        str(bld),
        str(src),
        str(bld / "webrtc"),
        str(src / "webrtc"),
        str(bld / "subprojects" / "abseil-cpp-20240722.0"),
        str(src / "subprojects" / "abseil-cpp-20240722.0"),
    ]

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
        extra_compile_args=extra_compile_args,
        # Static archive paths go in extra_objects (libraries=[...] expects
        # -lname format and a library search path).
        extra_objects=[str(static_lib), *absl_libs],
        extra_link_args=["-lpthread"],
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
