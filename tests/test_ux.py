"""Small things of the pages' look that a test can hold: moments, the folded server block."""

from __future__ import annotations

import time
from pathlib import Path

from lantern_bridge.admin import _when
from lantern_bridge.admin.i18n import FR
from test_admin import _admin, _client


def test_a_moment_keeps_its_exact_time_and_gives_the_script_something_to_read() -> None:
    assert _when(None) == "-" and _when(0) == "-"
    html = str(_when(time.time() - 90))
    assert 'class="ago"' in html and 'datetime="' in html and 'title="' in html
    exact = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() - 90))
    assert exact in html  # without the script, the exact time is what is read


def test_settings_have_one_bar_for_every_edited_card_and_a_search_box(tmp_path: Path) -> None:
    page = _admin(_client(tmp_path, sources=True)).get("/admin/config").text
    assert "data-saveall" in page and 'id="settings-search"' in page and "Save all" in page
    assert "Save all" in FR and "Find a setting…" in FR
