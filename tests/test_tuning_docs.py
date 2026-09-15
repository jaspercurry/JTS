# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path

from jasper.active_speaker import tuning_docs


def test_the_reading_order_leads_with_the_docs_and_their_size():
    order = tuning_docs.reading_order()

    assert [entry["name"] for entry in order] == [
        name for _label, name, _gives in tuning_docs.READING_ORDER
    ]
    for entry, (label, name, gives) in zip(order, tuning_docs.READING_ORDER):
        path = Path(entry["path"])
        assert path.name == name
        assert entry["label"] == label
        assert entry["gives"] == gives
        assert entry["bytes"] == path.stat().st_size
        assert entry["lines"] == path.read_bytes().count(b"\n")


def test_the_reading_order_prefers_the_on_box_install_when_present(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    before = tuning_docs.reading_order()
    assert (tuning_docs._REPO_DOCS_DIR / "tuning-operator-runbook.md").is_file()
    assert before[0]["path"] == str(
        tuning_docs._REPO_DOCS_DIR / "tuning-operator-runbook.md"
    )

    installed = tmp_path / "installed-docs"
    installed.mkdir()
    (installed / "tuning-operator-runbook.md").write_text("x")
    monkeypatch.setattr(tuning_docs, "_INSTALLED_DOCS_DIR", installed)
    monkeypatch.setattr(tuning_docs, "_REPO_DOCS_DIR", tmp_path / "no-checkout-here")

    after = tuning_docs.reading_order()
    assert after[0]["path"] == str(installed / "tuning-operator-runbook.md")
    assert after[0]["bytes"] == 1
    assert after[1]["path"] == "docs/tuning-methodology.md"
    assert after[1]["bytes"] is None
    assert after[1]["lines"] is None
