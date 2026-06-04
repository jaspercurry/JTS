#!/usr/bin/env python3
"""Rewrite the UAC2 gadget module's AudioStreaming interface strings.

Stdlib-only. NO ``jasper`` imports, NO venv — this runs under the
system ``/usr/bin/python3`` at early boot (``jasper-usbsink-init``'s
``ExecStartPre``), before ``/opt/jasper/.venv`` is guaranteed usable.

Why this exists
---------------
The Linux UAC2 gadget driver
(``drivers/usb/gadget/function/f_uac2.c``) hardcodes the
AudioStreaming interface alt-setting strings::

    STR_AS_OUT_ALT0 = "Playback Inactive"   (17 chars)
    STR_AS_OUT_ALT1 = "Playback Active"     (15 chars)

macOS displays this interface string as the device name in its audio
output list, in preference to the (configfs-settable) ``iProduct``
string. As of Raspberry Pi OS Trixie's 6.12 kernel these strings are
NOT exposed through configfs (every *other* gadget string is —
``function_name``, ``c_it_name``, clock names, etc. — but not these),
so the compiled module is the only lever. We overwrite the bytes
in-place, preserving the total length of each string's region and
null-terminating, so a connected host shows the speaker's configured
name instead of "Playback Inactive".

The transform is deliberately offset-independent: it searches for the
null-terminated byte token wherever it lives in ``.rodata``, so it
survives the string moving between kernel builds. If a future kernel
*renames* the string, the search simply misses, ``patch_module_bytes``
reports it as missing, and the caller leaves the stock module in place
— USB audio keeps working, only the cosmetic name reverts to default.

The replacement is bounded by the shorter slot (15 chars) so both alt
settings can carry the *same* name and the label never flickers
between idle ("Playback Inactive") and streaming ("Playback Active").
"""

from __future__ import annotations

import sys

# Null-terminated tokens as they appear in the module's .rodata. The
# trailing NUL makes the match exact (no substring false-positives) and
# the slot we are allowed to overwrite is the token *including* that NUL
# — never one byte more, so we can't clobber the next string.
#
# "Playback Active" is not a substring of "Playback Inactive"
# ("...Inactive" vs "...Active"), so the two searches are independent.
_PRIMARY = b"Playback Inactive\x00"   # alt0 — the idle label macOS shows
_SECONDARY = b"Playback Active\x00"    # alt1 — streaming label
_TARGETS = (_PRIMARY, _SECONDARY)

# Bounded by the shorter slot ("Playback Active" = 15 chars), leaving
# room for the terminating NUL, so the same name fits both alt strings.
MAX_NAME_BYTES = 15

DEFAULT_NAME = "JTS"

# The speaker-name wizard (jasper/speaker_name.py) already constrains
# the charset to this ASCII set; we re-apply it defensively here so a
# hand-edited speaker_name.env can never inject control bytes or
# non-ASCII into a USB string descriptor.
_ALLOWED = set(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 .,'&()+-_#"
)


def sanitize_name(name: str) -> str:
    """Reduce an arbitrary speaker name to a safe, length-bounded label.

    Drops disallowed/non-ASCII characters, collapses whitespace, trims,
    truncates to ``MAX_NAME_BYTES``, and falls back to ``DEFAULT_NAME``
    if nothing usable remains. Pure function — easy to unit-test.
    """
    cleaned = "".join(ch for ch in (name or "") if ch in _ALLOWED)
    cleaned = " ".join(cleaned.split())  # collapse runs of whitespace
    cleaned = cleaned[:MAX_NAME_BYTES].strip()
    return cleaned or DEFAULT_NAME


class PatchResult:
    """Outcome of a patch attempt.

    ``blob`` is the (possibly unchanged) module bytes. ``replaced`` and
    ``missing`` list the human-readable string names so the caller can
    log precisely which slots took. ``ok`` is True iff the primary
    ("Playback Inactive", the label macOS actually displays) was
    replaced.
    """

    def __init__(self, blob: bytes, name: str, replaced, missing, ambiguous):
        self.blob = blob
        self.name = name
        self.replaced = replaced
        self.missing = missing
        self.ambiguous = ambiguous

    @property
    def ok(self) -> bool:
        return _PRIMARY.rstrip(b"\x00").decode() in self.replaced


def patch_module_bytes(blob: bytes, name: str) -> PatchResult:
    """Return a copy of ``blob`` with the AS strings set to ``name``.

    Each token is overwritten only if it appears *exactly once* — a
    zero or multiple match is reported (in ``missing``/``ambiguous``)
    and that slot is left untouched, never guessed.
    """
    safe = sanitize_name(name)
    name_bytes = safe.encode("ascii")
    out = bytearray(blob)
    replaced, missing, ambiguous = [], [], []

    for token in _TARGETS:
        label = token.rstrip(b"\x00").decode()
        first = out.find(token)
        if first == -1:
            missing.append(label)
            continue
        if out.find(token, first + 1) != -1:
            # More than one occurrence — refuse to guess which is the
            # interface string. Leave it; the doctor/log will flag it.
            ambiguous.append(label)
            continue
        slot = len(token)  # chars + the trailing NUL we may consume
        # name_bytes <= MAX_NAME_BYTES (15) and the smallest slot is
        # len("Playback Active\0") == 16, so a NUL terminator always fits.
        padded = name_bytes + b"\x00" * (slot - len(name_bytes))
        out[first : first + slot] = padded
        replaced.append(label)

    # Invariant: this transform only overwrites equal-length spans, so
    # the module's size — and therefore its ELF layout — is unchanged.
    # Assert it rather than trust it: a size change would mean a bug
    # that could produce an unloadable module, and the caller relies on
    # "same size as stock" as its integrity gate before publishing.
    if len(out) != len(blob):  # pragma: no cover - defensive
        raise AssertionError(
            f"patch changed module size {len(blob)} -> {len(out)}"
        )
    return PatchResult(bytes(out), safe, replaced, missing, ambiguous)


def _main(argv) -> int:
    """CLI: ``uac2_name_patch.py <raw-stock.ko> <name> <out.ko>``.

    Input is a *decompressed* module (the bash wrapper handles
    xz/zstd/gzip decompression so this stays pure-stdlib). Writes the
    patched module to ``<out.ko>`` only when the primary string was
    replaced. Prints a single machine-parseable summary line to stdout.

    Exit codes: 0 = primary replaced (success); 3 = primary not found
    (leave stock in place); 2 = usage/IO error.
    """
    if len(argv) != 4:
        print("usage: uac2_name_patch.py <raw-stock.ko> <name> <out.ko>",
              file=sys.stderr)
        return 2
    stock_path, name, out_path = argv[1], argv[2], argv[3]
    try:
        blob = open(stock_path, "rb").read()
    except OSError as exc:
        print(f"error reading {stock_path}: {exc}", file=sys.stderr)
        return 2

    res = patch_module_bytes(blob, name)
    # `applied_name` (not `name`) so the orchestrator's own `name=` key
    # isn't duplicated in one structured log line — and so a truncated
    # name is visibly distinct from the requested one.
    summary = (
        f"applied_name={res.name!r} "
        f"replaced={','.join(res.replaced) or '-'} "
        f"missing={','.join(res.missing) or '-'} "
        f"ambiguous={','.join(res.ambiguous) or '-'}"
    )
    print(summary)

    if not res.ok:
        # Primary string absent (or ambiguous) — do not write an
        # override; caller keeps the stock module + default name.
        return 3
    try:
        with open(out_path, "wb") as fh:
            fh.write(res.blob)
    except OSError as exc:
        print(f"error writing {out_path}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
