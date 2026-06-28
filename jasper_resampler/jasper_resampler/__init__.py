# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Windowed-sinc resampler + spa_dll rate controller for jasper-usbsink.

`RateResampler` is the C++ mirror of the Rust crate
``rust/jasper-resampler`` (which the jasper-outputd daemon consumes via
content_bridge). There is no PyO3/maturin toolchain in this repo, so the
algorithm is duplicated in C++ for the Python/usbsink side; the two are
pinned to ≤1 LSB byte-identity by ``tests/test_resampler_contract.py``.

The capture-follower contract (the load-bearing sign convention, proven by
content_bridge's negative-feedback DLL):

  - Feed ``update(err)`` the buffer-fill error ``err = fill - target``
    (frames). It returns the resampler ratio, internally negating the
    error so a too-full buffer (``err > 0``) settles to ``ratio > 1``.
  - ``resample_block(pcm, ratio)`` consumes that ratio: ``ratio > 1``
    advances the read cursor by more than one input frame per output
    frame, so it emits FEWER output frames than input — draining the
    buffer (consuming the host faster). This is mathematically PipeWire's
    capture ``1.0 / corr``.

Usage::

    from jasper_resampler import RateResampler
    rc = RateResampler(bw=0.128, period_frames=480, rate=48000, channels=2)
    err = queue_fill_frames - target_frames
    ratio = rc.update(err)
    out_pcm = rc.resample_block(in_pcm, ratio)   # int16 LE by default

The default 16-bit path matches the Rust contract reference; pass
``bytes_per_sample=4`` (or construct with it) for the FIFO lean lane's
full-width S32_LE blocks so both usbsink output modes share one exact
resampling path.
"""
from ._resampler import RateResampler

__all__ = ["RateResampler"]
