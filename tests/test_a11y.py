"""What the pages promise to people who read them differently: contrast, focus, motion, colour modes, landmarks."""

from __future__ import annotations

import re
from importlib import resources
from pathlib import Path

from test_admin import _admin, _client

CSS = resources.files("lantern_bridge.admin").joinpath("static/app.css").read_text(encoding="utf-8")


def _colours() -> dict[str, str]:
    root = CSS[CSS.index(":root{") : CSS.index("}", CSS.index(":root{"))]
    return dict(re.findall(r"--([a-z]+):(#[0-9a-fA-F]{6})", root))


def _luminance(colour: str) -> float:
    r, g, b = (int(colour[i : i + 2], 16) / 255 for i in (1, 3, 5))

    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def _ratio(a: str, b: str) -> float:
    high, low = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_the_text_colours_clear_aa_on_every_surface() -> None:
    colours = _colours()
    for text in ("snow", "mist", "dusk", "aurora", "ice", "danger", "warn"):
        for surface in ("abyss", "night"):
            assert _ratio(colours[text], colours[surface]) >= 4.5, (text, surface)


def test_the_button_text_is_readable_on_its_fill() -> None:
    colours = _colours()
    assert _ratio(colours["abyss"], colours["snow"]) >= 7


def test_the_stylesheet_keeps_focus_motion_and_colour_modes() -> None:
    assert ":focus-visible{outline" in CSS
    assert "prefers-reduced-motion:no-preference" in CSS and "prefers-reduced-motion:reduce" in CSS
    assert "prefers-contrast:more" in CSS and "forced-colors:active" in CSS
    assert "min-height:44px" in CSS  # fields and buttons are big enough to touch


def test_every_page_has_its_landmarks_and_a_way_past_the_menu(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path, sources=True))
    for path in ("/admin/", "/admin/library", "/admin/activity", "/admin/config", "/admin/pairings", "/admin/start"):
        page = client.get(path).text
        assert '<html lang="en">' in page and '<a class="skip" href="#content">' in page, path
        assert '<main id="content"' in page and '<nav aria-label="Pages">' in page, path
        assert page.count("<h1") == 1, path
        assert 'name="viewport"' in page, path


def test_a_field_always_has_its_label(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path, sources=True))
    for path in ("/admin/library", "/admin/config", "/admin/start"):
        page = client.get(path).text
        inputs = re.findall(r"<(?:input|select)\b[^>]*>", page)
        for tag in inputs:
            if 'type="hidden"' in tag or 'type="submit"' in tag:
                continue
            name = re.search(r'name="([^"]+)"', tag)
            assert name, tag
            before = page[: page.index(tag)]
            assert before.rfind("<label") > before.rfind("</label>") or "aria-label" in tag, (path, tag)
