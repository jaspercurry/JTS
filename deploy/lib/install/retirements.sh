#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Reader-less library copies from earlier installs. REMOVAL CONDITION: every
# box has taken one install after this lands. build-sandbox.sh remains live:
# deploy/bin/jasper-contained-build sources it alone.
JASPER_RETIRED_FILES=(
    "/usr/local/lib/jasper/install/env-migrations.sh"
    "/usr/local/lib/jasper/install/first-party-runtime.sh"
    "/usr/local/lib/jasper/install/memory-resilience.sh"
    "/usr/local/lib/jasper/install/model-staging.sh"
    "/usr/local/lib/jasper/install/python-runtime.sh"
    "/usr/local/lib/jasper/install/renderers.sh"
    "/usr/local/lib/jasper/install/ring-platform.sh"
    "/usr/local/lib/jasper/install/rust-daemons.sh"
    "/usr/local/lib/jasper/install/service-users.sh"
    "/usr/local/lib/jasper/install/systemd-units.sh"
    "/usr/local/lib/jasper/install/web-assets.sh"
    "/usr/local/lib/jasper/jasper-core-graph-park-units.sh"
    "/usr/local/lib/jasper/jasper-apple-dongle.sh"
)

retire_leftovers() {
    rm -f -- "${JASPER_RETIRED_FILES[@]}" || true
}
