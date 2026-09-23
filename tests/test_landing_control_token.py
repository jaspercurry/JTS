# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import re

from . import nginx_site

# Both nginx sites serve the same token-baked index.html at `/`
# (install_management_static_assets runs for the full and streambox profiles).
NGINX_PROFILES = tuple(nginx_site.PROFILE_CONFS)


def test_nginx_serves_landing_no_store():
    # Both the full and streambox sites serve the token-bearing index.html at
    # `/`, so each `location = /` block must carry no-store (never cached by a
    # browser or intermediary).
    for profile in NGINX_PROFILES:
        conf = nginx_site.conf_text(profile)
        m = re.search(r"location\s*=\s*/\s*\{(.*?)\}", conf, flags=re.S)
        assert m, f"{profile} conf missing the `location = /` landing block"
        block = m.group(1)
        assert "no-store" in block, \
            f"{profile} conf `location = /` must set Cache-Control no-store"
