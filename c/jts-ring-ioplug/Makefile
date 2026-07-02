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
# -std=c11 keeps the language strict (no silent GNU language extensions), but
# _GNU_SOURCE is required for the POSIX/GNU LIBRARY symbols the core uses under
# glibc's strict-c11 feature-macro gating: clock_gettime/CLOCK_MONOTONIC,
# nanosleep, ftruncate, O_CLOEXEC, PATH_MAX. It also resolves alsa-lib's
# global.h `struct timespec` fallback redefinition (glibc's is hidden without
# it under bare -std=c11). macOS exposes these by default, so the host targets
# built even without it; the Pi's glibc does not — `make plugin` failed with
# "implicit declaration of ftruncate" / "O_CLOEXEC undeclared" before this.
CFLAGS ?= -std=c11 -D_GNU_SOURCE -O2 -Wall -Wextra -Werror -fPIC
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

# -DPIC is load-bearing, not redundant with -fPIC: alsa/global.h keys
# SND_PCM_PLUGIN_SYMBOL on the PIC *preprocessor* macro (#ifdef PIC).
# -fPIC alone does not define it, so without -DPIC the macro expands to
# the static-build snd_dlsym_start registration path and the .so fails
# at dlopen with "undefined symbol: snd_dlsym_start" (observed on
# jts.local 2026-07-02). libtool passes -DPIC implicitly; hand-rolled
# Makefiles must do it themselves.
plugin: pcm_jts_ring.c $(CORE) jts_ring_shm.h
	$(CC) $(CFLAGS) -DPIC -shared -o $(PLUGIN_SO) pcm_jts_ring.c $(CORE) -lasound $(LDFLAGS)

install: plugin
	install -d $(ALSA_PLUGIN_DIR)
	install -m 0644 $(PLUGIN_SO) $(ALSA_PLUGIN_DIR)/$(PLUGIN_SO)

clean:
	rm -f test_ring_core ring_writer_bench $(PLUGIN_SO)
