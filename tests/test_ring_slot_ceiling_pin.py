# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Cross-file pins for the SHM-ring geometry ACCEPT-SET.

Several ring constants are declared independently in C and in two Rust crates
and must stay in lockstep by hand:

- ``n_slots`` ceiling — ``JTS_RING_MAX_SLOTS`` (C header
  ``c/jts-ring-ioplug/jts_ring_shm.h``), ``MAX_N_SLOTS``
  (``rust/jasper-ring/src/layout.rs``), ``MAX_SHM_RING_SLOTS``
  (``rust/jasper-outputd/src/config.rs``).
- ``channels`` ceiling — ``JTS_RING_MAX_CHANNELS`` (C header),
  ``MAX_RING_CHANNELS`` (jasper-ring layout), and the upper bound outputd
  already enforces on ``JASPER_OUTPUTD_ACTIVE_CHANNELS``.
- the ``sample_format`` enum ids, which are HEADER FIELD VALUES on the wire.
- the create/attach transaction-lock contract (suffix, mode, budgets).

Unlike the header OFFSETS — which the golden-layout ``_Static_assert`` and the
Rust layout test pin bit-for-bit — nothing tied these constants together, so a
mismatch was caught only at RUNTIME on the Pi (the reader's geometry validation
rejects a geometry the writer created, failing loud on arm). These tests move
that failure to CI: if the declarations drift, they fail here instead of
on-device.

The two CEILINGS are asserted EQUAL, not against a literal, so raising or
lowering one deliberately (in the same commit as its siblings) keeps the pins
passing. The FORMAT ENUM is different: those ids are written into the shared
header and compared field-by-field on attach, so their numeric values are a wire
contract and are pinned to the literals 1 and 2.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]

_C_HEADER = _REPO_ROOT / "c" / "jts-ring-ioplug" / "jts_ring_shm.h"
_RING_LAYOUT_RS = _REPO_ROOT / "rust" / "jasper-ring" / "src" / "layout.rs"
_RING_LIB_RS = _REPO_ROOT / "rust" / "jasper-ring" / "src" / "lib.rs"
_OUTPUTD_CONFIG_RS = _REPO_ROOT / "rust" / "jasper-outputd" / "src" / "config.rs"


def _read(path: Path) -> str:
    if not path.exists():
        pytest.skip(f"source not present: {path}")
    return path.read_text(encoding="utf-8")


def _extract(pattern: str, text: str, label: str) -> int:
    m = re.search(pattern, text)
    assert m is not None, f"could not find {label} definition (pattern {pattern!r})"
    return int(m.group(1))


def _extract_text(pattern: str, text: str, label: str) -> str:
    m = re.search(pattern, text)
    assert m is not None, f"could not find {label} definition (pattern {pattern!r})"
    return m.group(1)


def _extract_only(pattern: str, text: str, label: str) -> re.Match[str]:
    """Extract a match that must be UNIQUE in the file.

    A second call site of the same shape would leave this pin silently guarding
    only the first one, so ambiguity fails loudly instead.
    """
    matches = list(re.finditer(pattern, text))
    assert matches, f"could not find {label} (pattern {pattern!r})"
    assert len(matches) == 1, (
        f"expected exactly one {label} declaration, found {len(matches)} — the pin "
        "would silently guard only the first. Narrow the pattern or pin each site."
    )
    return matches[0]


def test_ring_channel_ceiling_agrees_across_c_and_both_rust_crates():
    c_max = _extract(
        r"#define\s+JTS_RING_MAX_CHANNELS\s+(\d+)u",
        _read(_C_HEADER),
        "JTS_RING_MAX_CHANNELS",
    )
    ring_layout = _read(_RING_LAYOUT_RS)
    ring_match = re.search(r"pub const MAX_RING_CHANNELS:\s*u32\s*=\s*(\d+);", ring_layout)
    if ring_match is None:
        # The Rust half of this pin lands with the jasper-ring crate rung (R1),
        # which is developed in parallel with the C ioplug rung (R2). Until it
        # merges there is nothing to compare against, so report the missing
        # constant by name rather than erroring on a regex miss.
        pytest.xfail(
            "MAX_RING_CHANNELS is not declared in rust/jasper-ring/src/layout.rs yet "
            "(the jasper-ring ring-v2 rung has not landed in this checkout). This pin "
            f"activates as soon as it does; the C side already declares "
            f"JTS_RING_MAX_CHANNELS={c_max}."
        )
    ring_max = int(ring_match.group(1))
    # outputd does not name a constant: it bounds the env directly. The upper
    # bound of that range is the same ceiling — the widest active-lane channel
    # count outputd will accept must be a channel count the ring can carry.
    outputd_max = int(
        _extract_only(
            r'env_optional_u16\(\s*"JASPER_OUTPUTD_ACTIVE_CHANNELS"\s*,\s*\d+\s*,\s*(\d+)\s*\)',
            _read(_OUTPUTD_CONFIG_RS),
            "the JASPER_OUTPUTD_ACTIVE_CHANNELS bound",
        ).group(1)
    )
    assert c_max == ring_max == outputd_max, (
        "SHM-ring channel ceiling drifted across the three source-of-truth "
        f"definitions: JTS_RING_MAX_CHANNELS={c_max} (C header), "
        f"MAX_RING_CHANNELS={ring_max} (jasper-ring layout.rs), "
        f"JASPER_OUTPUTD_ACTIVE_CHANNELS upper bound={outputd_max} "
        "(jasper-outputd config.rs). Change all three in the same commit — the "
        "reader rejects a writer's out-of-range channel count at RUNTIME, so a "
        "mismatch only surfaces on-Pi arm."
    )


def test_ring_sample_format_enum_agrees_between_c_and_rust():
    c = _read(_C_HEADER)
    rust = _read(_RING_LAYOUT_RS)
    c_s16 = _extract(
        r"#define\s+JTS_RING_SAMPLE_FORMAT_S16LE\s+(\d+)u",
        c,
        "JTS_RING_SAMPLE_FORMAT_S16LE",
    )
    c_s32 = _extract(
        r"#define\s+JTS_RING_SAMPLE_FORMAT_S32LE\s+(\d+)u",
        c,
        "JTS_RING_SAMPLE_FORMAT_S32LE",
    )
    rust_s16 = _extract(
        r"pub const SAMPLE_FORMAT_S16LE:\s*u32\s*=\s*(\d+);",
        rust,
        "SAMPLE_FORMAT_S16LE",
    )
    rust_s32 = _extract(
        r"pub const SAMPLE_FORMAT_S32LE:\s*u32\s*=\s*(\d+);",
        rust,
        "SAMPLE_FORMAT_S32LE",
    )
    # Literal values, not just equality: these ids are written into the ring
    # header's sample_format field and compared field-by-field on attach, so
    # renumbering either side would make two binaries disagree about the bytes
    # already on the wire.
    assert (c_s16, rust_s16) == (1, 1), (
        f"S16LE format id drifted: C={c_s16}, Rust={rust_s16} (both must be 1 — it "
        "is a header field value, not a local enum)"
    )
    assert (c_s32, rust_s32) == (2, 2), (
        f"S32LE format id drifted: C={c_s32}, Rust={rust_s32} (both must be 2 — it "
        "is a header field value, not a local enum)"
    )


def test_ring_slot_ceiling_agrees_across_c_and_both_rust_crates():
    c_max = _extract(
        r"#define\s+JTS_RING_MAX_SLOTS\s+(\d+)u", _read(_C_HEADER), "JTS_RING_MAX_SLOTS"
    )
    ring_max = _extract(
        r"pub const MAX_N_SLOTS:\s*u32\s*=\s*(\d+);",
        _read(_RING_LAYOUT_RS),
        "MAX_N_SLOTS",
    )
    outputd_max = _extract(
        r"pub const MAX_SHM_RING_SLOTS:\s*u32\s*=\s*(\d+);",
        _read(_OUTPUTD_CONFIG_RS),
        "MAX_SHM_RING_SLOTS",
    )
    assert c_max == ring_max == outputd_max, (
        "SHM-ring slot-count ceiling drifted across the three source-of-truth "
        f"definitions: JTS_RING_MAX_SLOTS={c_max} (C header), "
        f"MAX_N_SLOTS={ring_max} (jasper-ring layout.rs), "
        f"MAX_SHM_RING_SLOTS={outputd_max} (jasper-outputd config.rs). "
        "Change all three in the same commit — the reader rejects a writer's "
        "out-of-range n_slots at RUNTIME, so a mismatch only surfaces on-Pi arm."
    )


def test_ring_open_transaction_lock_contract_agrees_between_c_and_rust():
    c = _read(_C_HEADER)
    rust = _read(_RING_LIB_RS)
    c_suffix = _extract_text(
        r'#define\s+JTS_RING_OPEN_LOCK_SUFFIX\s+"([^"]+)"',
        c,
        "JTS_RING_OPEN_LOCK_SUFFIX",
    )
    rust_suffix = _extract_text(
        r'const\s+OPEN_LOCK_SUFFIX:\s*&str\s*=\s*"([^"]+)";',
        rust,
        "OPEN_LOCK_SUFFIX",
    )
    c_mode = _extract_text(
        r"#define\s+JTS_RING_OPEN_LOCK_MODE\s+(0[0-7]+)",
        c,
        "JTS_RING_OPEN_LOCK_MODE",
    )
    rust_mode = _extract_text(
        r"const\s+OPEN_LOCK_MODE:\s*u32\s*=\s*(0o[0-7]+);",
        rust,
        "OPEN_LOCK_MODE",
    )
    c_timeout = _extract(
        r"#define\s+JTS_RING_OPEN_LOCK_WAIT_TIMEOUT_MS\s+(\d+)ull",
        c,
        "JTS_RING_OPEN_LOCK_WAIT_TIMEOUT_MS",
    )
    rust_timeout = _extract(
        r"const\s+OPEN_LOCK_WAIT_TIMEOUT_MS:\s*u64\s*=\s*(\d+);",
        rust,
        "OPEN_LOCK_WAIT_TIMEOUT_MS",
    )
    c_step = _extract(
        r"#define\s+JTS_RING_OPEN_LOCK_WAIT_STEP_US\s+(\d+)ull",
        c,
        "JTS_RING_OPEN_LOCK_WAIT_STEP_US",
    )
    rust_step = _extract(
        r"const\s+OPEN_LOCK_WAIT_STEP_US:\s*u64\s*=\s*([\d_]+);",
        rust,
        "OPEN_LOCK_WAIT_STEP_US",
    )
    c_attempts = _extract(
        r"#define\s+JTS_RING_OPEN_MAX_ATTEMPTS\s+(\d+)u",
        c,
        "JTS_RING_OPEN_MAX_ATTEMPTS",
    )
    rust_attempts = _extract(
        r"const\s+OPEN_MAX_ATTEMPTS:\s*usize\s*=\s*(\d+);",
        rust,
        "OPEN_MAX_ATTEMPTS",
    )

    assert c_suffix == rust_suffix == ".open.lock"
    assert int(c_mode, 8) == int(rust_mode.removeprefix("0o"), 8) == 0o660
    assert c_timeout == rust_timeout
    assert c_step == int(str(rust_step).replace("_", ""))
    assert c_attempts == rust_attempts


def test_ring_header_offsets_agree_between_c_and_python():
    """Python is a THIRD speller of the v1 header layout, and nothing pinned it.

    The C and Rust halves are already tied bit-for-bit — the ``_Static_assert``
    block in the C header and the Rust golden-layout test assert the same
    numbers. :mod:`jasper.ring_assets` then re-declares those offsets a third
    time, in a language neither of them can import, to read the same bytes for
    the stall alarm and (since #2786) the grouping ring's ``/state`` block. A
    drift there is silent in the worst possible way: the parser keeps returning a
    coherent-looking header with fields read out of the wrong words, so a live
    ring reads as idle or an idle one as stalled.

    Asserted field by field against the C header's own asserts rather than
    against literals, so moving a field deliberately (in the same commit as its
    siblings) keeps this passing.
    """
    from jasper import ring_assets

    c = _read(_C_HEADER)
    c_offsets = {
        field: int(offset)
        for field, offset in re.findall(
            r"_Static_assert\(offsetof\(jts_ring_header_t,\s*(\w+)\)\s*==\s*(\d+)",
            c,
        )
    }
    assert c_offsets, "could not read the golden-layout offsets from the C header"

    python_offsets = {
        "magic": ring_assets._RING_OFF_MAGIC,
        "version": ring_assets._RING_OFF_VERSION,
        "rate": ring_assets._RING_OFF_RATE,
        "channels": ring_assets._RING_OFF_CHANNELS,
        "sample_format": ring_assets._RING_OFF_SAMPLE_FORMAT,
        "period_frames": ring_assets._RING_OFF_PERIOD_FRAMES,
        "n_slots": ring_assets._RING_OFF_N_SLOTS,
        "writer_epoch": ring_assets._RING_OFF_WRITER_EPOCH,
        "write_seq": ring_assets._RING_OFF_WRITE_SEQ,
        "read_seq": ring_assets._RING_OFF_READ_SEQ,
        "writer_pid": ring_assets._RING_OFF_WRITER_PID,
        "writer_heartbeat_ns": ring_assets._RING_OFF_WRITER_HEARTBEAT_NS,
        "reader_pid": ring_assets._RING_OFF_READER_PID,
        "reader_heartbeat_ns": ring_assets._RING_OFF_READER_HEARTBEAT_NS,
    }
    for field, offset in python_offsets.items():
        assert field in c_offsets, (
            f"jasper.ring_assets reads a header field {field!r} the C header "
            "does not declare"
        )
        assert offset == c_offsets[field], (
            f"header offset drift on {field}: Python reads it at {offset}, the C "
            f"header pins it at {c_offsets[field]}"
        )

    # The header SIZE and the magic/version gate ride the same contract.
    assert ring_assets._RING_HEADER_BYTES == _extract(
        r"#define\s+JTS_RING_HEADER_BYTES\s+(\d+)u", c, "JTS_RING_HEADER_BYTES"
    )
    assert ring_assets._RING_HEADER_VERSION == _extract(
        r"#define\s+JTS_RING_VERSION\s+(\d+)u", c, "JTS_RING_VERSION"
    )
    c_magic = re.search(r"#define\s+JTS_RING_MAGIC\s+0x([0-9A-Fa-f]+)u", c)
    assert c_magic is not None, "could not find JTS_RING_MAGIC"
    assert ring_assets._RING_MAGIC == int(c_magic.group(1), 16)


def test_ring_writer_lock_suffix_agrees_between_c_and_python():
    """Python became a READER of the C writer lock (audio-graph consolidation
    #2285, P9-C): the grouping reconciler's active-content release barrier
    contends on it, and the doctor's exclusivity guard enumerates its holders.
    Both derive the path from :mod:`jasper.ring_assets`, so that suffix is a
    second declaration of a C constant and gets the same cross-file pin the
    open-lock contract has.

    Also pins the CONSTRUCTION, not just the suffix: ``acquire_writer_lock``
    appends the suffix to the ring path with no directory indirection
    (``snprintf(lock_path, ..., "%s%s", path, JTS_RING_WRITER_LOCK_SUFFIX)``),
    which is what makes a Python prober contend on the same inode the ioplug's
    writer holds rather than a lookalike beside it.
    """
    from jasper.ring_assets import (
        RING_ACTIVE_CONTENT_FILE,
        RING_WRITER_LOCK_SUFFIX,
        ring_writer_lock_path,
    )

    c = _read(_C_HEADER)
    c_suffix = _extract_text(
        r'#define\s+JTS_RING_WRITER_LOCK_SUFFIX\s+"([^"]+)"',
        c,
        "JTS_RING_WRITER_LOCK_SUFFIX",
    )

    assert c_suffix == RING_WRITER_LOCK_SUFFIX
    # …and it is NOT the transaction lock, which Rust also takes and which says
    # nothing about who owns the ring.
    open_suffix = _extract_text(
        r'#define\s+JTS_RING_OPEN_LOCK_SUFFIX\s+"([^"]+)"',
        c,
        "JTS_RING_OPEN_LOCK_SUFFIX",
    )
    assert RING_WRITER_LOCK_SUFFIX != open_suffix

    source = _read(_REPO_ROOT / "c" / "jts-ring-ioplug" / "jts_ring_shm.c")
    _extract_only(
        r'snprintf\(lock_path,\s*sizeof\(lock_path\),\s*"%s%s",\s*path,\s*\n?'
        r"\s*JTS_RING_WRITER_LOCK_SUFFIX\);",
        source,
        "acquire_writer_lock's lock-path construction",
    )
    assert ring_writer_lock_path(RING_ACTIVE_CONTENT_FILE) == (
        RING_ACTIVE_CONTENT_FILE + c_suffix
    )


def test_doctor_writer_lock_confirm_delay_outlasts_the_c_acquisition_budget():
    """``acquire_writer_lock`` OPENS the lock file and only THEN spins on
    ``flock`` until ``JTS_RING_OPEN_LOCK_WAIT_TIMEOUT_MS`` expires, so a healthy
    box legitimately has TWO processes holding an fd on one ``.writer.lock`` for
    up to that long — the live incumbent and a transient contender about to be
    refused. The doctor's two-live-writers guard confirms a suspected reading
    after ``_WRITER_LOCK_CONFIRM_DELAY_SEC``; if that delay did not OUTLAST the
    C budget, an ordinary create-or-attach race would be reported as the
    defect."""
    from jasper.cli.doctor.audio_runtime_ring import _WRITER_LOCK_CONFIRM_DELAY_SEC

    c = _read(_C_HEADER)
    budget_ms = _extract(
        r"#define\s+JTS_RING_OPEN_LOCK_WAIT_TIMEOUT_MS\s+(\d+)ull",
        c,
        "JTS_RING_OPEN_LOCK_WAIT_TIMEOUT_MS",
    )

    assert _WRITER_LOCK_CONFIRM_DELAY_SEC > budget_ms / 1000.0
