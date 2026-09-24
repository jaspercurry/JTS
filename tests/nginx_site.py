# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The nginx site conf a profile actually gets, with its snippets resolved.

`deploy/nginx-jasper.conf` and `deploy/nginx-jasper-streambox.conf` carry
their listeners and a short set of `include` lines; the routes live once
under `deploy/nginx/`, which install.sh installs beside the site conf into
`/etc/nginx/snippets/`. A test that reasons about routes wants the assembled
text, so this resolves those includes against the repo exactly as nginx
resolves them against the Pi.
"""
import functools
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SNIPPET_SOURCE_DIR = REPO_ROOT / "deploy" / "nginx"
INSTALLED_SNIPPET_DIR = "/etc/nginx/snippets/"

PROFILE_CONFS = {
    "full": REPO_ROOT / "deploy" / "nginx-jasper.conf",
    "streambox": REPO_ROOT / "deploy" / "nginx-jasper-streambox.conf",
}

# The reverse-proxy header snippet stays an unresolved `include` line: it is
# what a proxying block carries INSTEAD of its own proxy_set_header
# directives, so the parity check for it reads that line, not the headers it
# would expand to.
_KEEP_AS_INCLUDE = frozenset({"jts-proxy-headers.conf"})

_INCLUDE_RX = re.compile(r"^\s*include\s+(\S+);\s*$")


@functools.cache
def conf_text(profile: str) -> str:
    """The profile's site conf with its route and server snippets inlined."""
    return _resolve(PROFILE_CONFS[profile].read_text(encoding="utf-8"))


def _resolve(text: str) -> str:
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        match = _INCLUDE_RX.match(line)
        target = match.group(1) if match else ""
        name = (
            target[len(INSTALLED_SNIPPET_DIR) :]
            if target.startswith(INSTALLED_SNIPPET_DIR)
            else ""
        )
        if not name or name in _KEEP_AS_INCLUDE:
            out.append(line)
            continue
        snippet = SNIPPET_SOURCE_DIR / name
        assert snippet.is_file(), f"no source for included snippet: {target}"
        out.append(_resolve(snippet.read_text(encoding="utf-8")))
    return "".join(out)


LOCATION_RX = re.compile(
    r"(?m)^    location +(?:(?P<mod>=|\^~|~\*?) +)?(?P<path>\S+) *\{"
)


def servers(conf: str) -> list[tuple[frozenset[int], dict]]:
    """Every top-level `server {}`: its listener ports and its locations.

    Locations are keyed `(modifier, path)` — `("=", "/sound/pair/sync")` for
    an exact block — and carry their brace-balanced body, so a caller reads
    structure rather than slicing the file on comment text or line order.
    Several callers pass a slice of a conf rather than the whole file; one
    that starts inside a server block (they cut at `listen 443` to separate
    the two) reads as that single server.
    """
    chunks = conf.split("\nserver {")
    out = []
    for chunk in (chunks[1:] or chunks):
        body = chunk[: chunk.index("\n}")] if "\n}" in chunk else chunk
        ports = frozenset(
            int(m.group(1))
            for m in re.finditer(r"(?m)^    listen +(?:\[::\]:)?(\d+)", body)
        )
        locations = {}
        for m in LOCATION_RX.finditer(body):
            start = body.index("{", m.start())
            depth, end = 0, start
            while True:
                depth += {"{": 1, "}": -1}.get(body[end], 0)
                if depth == 0:
                    break
                end += 1
            locations[(m.group("mod") or "", m.group("path"))] = body[start + 1 : end]
        out.append((ports, locations))
    # nginx refuses a duplicate location, which the dict would silently drop,
    # and LOCATION_RX only sees 4-space-indented ones.
    found = len(re.findall(r"(?m)^[ \t]*location\b", conf))
    parsed = sum(len(locations) for _, locations in out)
    assert found == parsed, f"{found} location lines, {parsed} parsed"
    return out
