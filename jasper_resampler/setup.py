# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Build the windowed-sinc resampler pybind11 extension.

A single pybind11 module, `jasper_resampler._resampler`, exposing the
`RateResampler` class: a spa_dll rate controller wired to a buffer-fill
error plus a streaming Blackman-Harris windowed-sinc resampler. Used by
jasper-usbsink's capture path to rate-match the USB host clock to the DAC.

Unlike the sibling jasper_aec3 binding — which LINKS an external library
(libwebrtc-audio-processing) and therefore needs pkg-config / abseil /
vendored-static machinery — this binding links NOTHING beyond libstdc++.
The math is header-only (sinc + window coefficients + a 20-line DLL), so
this mirrors only jasper_aec3's *simple* extension path: one
Pybind11Extension at -O3, cxx_std=17, no include_dirs beyond pybind11's,
no link libraries.

Self-contained on purpose: it compiles on a dev laptop with only pybind11
(so pytest's contract test can build + import it) and on the Pi during
deploy/install.sh. No apt/pip runtime dependency is introduced.
"""
from __future__ import annotations

from pybind11.setup_helpers import Pybind11Extension, build_ext
from setuptools import setup

ext = Pybind11Extension(
    "jasper_resampler._resampler",
    sources=["src/resampler_binding.cpp"],
    extra_compile_args=["-O3"],
    cxx_std=17,
)

setup(ext_modules=[ext], cmdclass={"build_ext": build_ext})
