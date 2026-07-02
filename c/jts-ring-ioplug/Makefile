# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0
#
# JTS Ring B — ALSA ioplug + writer core Makefile.
#
# Targets:
#   make test    — build + run the host core unit test (NO ALSA; any host).
#   make bench   — build the SHM ring writer bench (NO ALSA; any host).
#   make plugin  — build the ALSA ioplug .so (Pi/Linux ONLY; needs alsa-lib).
#   make install — install the .so into the ALSA plugin dir (Pi ONLY, needs root).
#   make clean
#
# The host-safe targets (test, bench) never link libasound, so they build on
# macOS + Linux. `plugin`/`install` are gated to Linux with alsa-lib headers;
# scripts/ring-proto/build-on-pi.sh drives them on the Pi.

CC ?= cc
CFLAGS ?= -std=c11 -O2 -Wall -Wextra -Werror -fPIC
LDFLAGS ?=

# ALSA plugin install dir (aarch64 Trixie). Override for other multiarch dirs.
ALSA_PLUGIN_DIR ?= /usr/lib/aarch64-linux-gnu/alsa-lib
PLUGIN_SO := libasound_module_pcm_jts_ring.so

CORE := jts_ring_shm.c

.PHONY: all test bench plugin install clean

# Default: the host-safe targets only (so `make` on a dev laptop just works).
all: test bench

# --- Host-safe (no ALSA) ---

test_ring_core: test_ring_core.c $(CORE) jts_ring_shm.h
	$(CC) $(CFLAGS) -o $@ test_ring_core.c $(CORE)

test: test_ring_core
	./test_ring_core

ring_writer_bench: ring_writer_bench.c $(CORE) jts_ring_shm.h
	$(CC) $(CFLAGS) -o $@ ring_writer_bench.c $(CORE) -lm

bench: ring_writer_bench

# --- Pi-only (needs alsa-lib) ---

plugin: pcm_jts_ring.c $(CORE) jts_ring_shm.h
	$(CC) $(CFLAGS) -shared -o $(PLUGIN_SO) pcm_jts_ring.c $(CORE) -lasound $(LDFLAGS)

install: plugin
	install -d $(ALSA_PLUGIN_DIR)
	install -m 0644 $(PLUGIN_SO) $(ALSA_PLUGIN_DIR)/$(PLUGIN_SO)

clean:
	rm -f test_ring_core ring_writer_bench $(PLUGIN_SO)
