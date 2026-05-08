"""Build the WebRTC AEC3 pybind11 extension.

Why this lives in setup.py and not pyproject.toml: setuptools' declarative
extension support in pyproject.toml is awkward when you need pkg-config
flags resolved at build time. A four-line setup.py is the clearest path.

System prereqs (handled by deploy/install.sh on the Pi):
- libwebrtc-audio-processing-dev (Debian Trixie ships v1.3-3 of the
  upstream pulseaudio/webrtc-audio-processing fork — this is the modern
  AEC3 API, despite the 1.x version number; the 1.x→2.x naming reflects
  package-API stability, not algorithm version)
- pkg-config (used here to discover headers + link flags)
- A C++17-capable compiler (g++ from build-essential)
"""
from __future__ import annotations

import shlex
import subprocess

from pybind11.setup_helpers import Pybind11Extension, build_ext
from setuptools import setup

PKG = "webrtc-audio-processing-1"


def pkg_config(*flags: str) -> list[str]:
    out = subprocess.check_output(
        ["pkg-config", *flags, PKG], text=True
    ).strip()
    return shlex.split(out)


cflags = pkg_config("--cflags")
libs = pkg_config("--libs")

include_dirs = [a[2:] for a in cflags if a.startswith("-I")]
extra_compile_args = [a for a in cflags if not a.startswith("-I")]
library_dirs = [a[2:] for a in libs if a.startswith("-L")]
libraries = [a[2:] for a in libs if a.startswith("-l")]
extra_link_args = [
    a for a in libs if not (a.startswith("-L") or a.startswith("-l"))
]

ext = Pybind11Extension(
    "jasper_aec3._aec3",
    sources=["src/aec3_binding.cpp"],
    include_dirs=include_dirs,
    library_dirs=library_dirs,
    libraries=libraries,
    extra_compile_args=extra_compile_args + ["-O3"],
    extra_link_args=extra_link_args,
    cxx_std=17,
)

setup(ext_modules=[ext], cmdclass={"build_ext": build_ext})
