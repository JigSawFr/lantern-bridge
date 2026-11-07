"""Small things of the pages' look that a test can hold: moments, the folded server block."""

from __future__ import annotations

import time

from lantern_bridge.admin import _when


def test_a_moment_keeps_its_exact_time_and_gives_the_script_something_to_read() -> None:
    assert _when(None) == "-" and _when(0) == "-"
    html = str(_when(time.time() - 90))
    assert 'class="ago"' in html and 'datetime="' in html and 'title="' in html
    exact = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() - 90))
    assert exact in html  # without the script, the exact time is what is read
