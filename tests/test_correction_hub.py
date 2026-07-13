# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from jasper.web import correction_hub


def test_section_tabs_marks_only_the_active_section() -> None:
    rendered = correction_hub.section_tabs("crossover")

    assert rendered.startswith(
        '<nav class="segmented" aria-label="Correction measurement type">'
    )
    assert 'role="tab"' not in rendered
    assert 'role="tablist"' not in rendered
    assert rendered.count('aria-current="page"') == 1
    assert (
        'aria-current="page" href="/correction/crossover/">Crossover</a>'
        in rendered
    )


def test_section_tabs_escapes_registry_labels_and_links(monkeypatch) -> None:
    monkeypatch.setattr(
        correction_hub,
        "SECTIONS",
        (("unsafe", 'Room <script>', '/correction/?next="x"&mode=<raw>'),),
    )

    rendered = correction_hub.section_tabs("unsafe")

    assert "<script>" not in rendered
    assert "Room &lt;script&gt;" in rendered
    assert 'href="/correction/?next=&quot;x&quot;&amp;mode=&lt;raw&gt;"' in rendered
