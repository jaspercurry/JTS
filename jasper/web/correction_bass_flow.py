# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""HTTPS bass-correction placeholder flow."""

from __future__ import annotations

import html

from ._common import canonical_header, canonical_page
from .correction_hub import section_tabs


def render_page(hostname: str, csrf_token: str = "") -> bytes:
    header = canonical_header(
        "Correction",
        back_href=f"http://{html.escape(hostname, quote=True)}/",
    )
    body = f"""
{header}
<main class="page correction-measurement bass-page">
  {section_tabs("bass")}

  <section class="info-card info-card--accent">
    <h2 class="section__title">Bass correction</h2>
    <p class="form-hint">Subwoofer and low-frequency tuning will live here.</p>
  </section>
</main>
"""
    return canonical_page(
        "Bass correction — JTS speaker",
        body,
        csrf_token=csrf_token,
        page_css_href="/assets/correction/crossover.css",
    )
