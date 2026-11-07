# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""The pages in Boréale's fifteen languages: the catalogs, the choice, the menu with its flags."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from lantern_bridge.admin.i18n import CATALOGS, FLAGS, FR, HTML_LANG, LANGUAGES, NAMES, flag, match, pick, translator
from test_admin import _admin, _client

BASES = {"fr-CA": "fr", "es-419": "es", "pt-BR": "pt"}  # variants: only what differs from their base
WHOLE = [code for code in LANGUAGES if code not in ("en", *BASES)]


def _fields(text: str) -> list[str]:
    return sorted(re.findall(r"\{[^}]*\}", text))


def test_there_are_boreales_fifteen_languages_each_with_a_flag_and_a_name() -> None:
    assert len(LANGUAGES) == 15 and LANGUAGES[0] == "en"
    assert set(NAMES) == set(FLAGS) == set(HTML_LANG) == set(LANGUAGES)
    assert flag("fr") == "\U0001f1eb\U0001f1f7" and flag("zh-Hant") == "\U0001f1f9\U0001f1fc"  # France, Taiwan


@pytest.mark.parametrize("code", WHOLE)
def test_a_whole_catalog_has_every_text_the_french_has(code: str) -> None:
    catalog = CATALOGS[code]
    assert set(catalog) == set(FR), (code, sorted(set(FR) ^ set(catalog))[:5])
    for text, shown in catalog.items():
        assert shown.strip(), (code, text)
        assert _fields(shown) == _fields(text), (code, text)  # a {placeholder} is kept as it is
        assert "<" not in shown and ">" not in shown, (code, text)  # the pages are escaped text, never markup


@pytest.mark.parametrize("code", sorted(BASES))
def test_a_variant_only_holds_what_differs_from_its_base(code: str) -> None:
    base = CATALOGS[BASES[code]]
    for text, shown in CATALOGS[code].items():
        assert text in FR and _fields(shown) == _fields(text), (code, text)
        assert shown != base.get(text), (code, text)  # same as the base: it should not be here


def test_a_text_falls_back_to_the_base_then_to_english() -> None:
    assert translator("fr")("Settings") == FR["Settings"]
    assert translator("fr-CA")("Settings") == FR["Settings"]  # nothing of its own: French
    assert translator("pt-BR")("Settings") != "Settings"
    assert translator("de")("A text no catalog has") == "A text no catalog has"
    assert translator("en")("Settings") == "Settings"


@pytest.mark.parametrize(
    ("accept", "expected"),
    [
        ("fr-CA,fr;q=0.8", "fr-CA"),
        ("fr-FR", "fr"),
        ("pt-BR,pt;q=0.8", "pt-BR"),
        ("pt-PT", "pt"),
        ("es-MX", "es-419"),
        ("es-AR,es;q=0.9", "es-419"),
        ("es-ES", "es"),
        ("zh-TW", "zh-Hant"),
        ("zh-HK", "zh-Hant"),
        ("zh-Hant-TW", "zh-Hant"),
        ("zh-CN", "zh-Hans"),
        ("zh", "zh-Hans"),
        ("ja,en;q=0.5", "ja"),
        ("sv,ko;q=0.5", "ko"),
        ("sv", "en"),
        ("", "en"),
    ],
)
def test_the_browsers_wish_picks_the_nearest_language(accept: str, expected: str) -> None:
    assert pick(None, accept) == expected


def test_a_cookie_wins_over_the_browser_and_a_stranger_is_ignored() -> None:
    assert pick("de", "fr") == "de" and pick("xx", "fr") == "fr" and match("klingon") is None


def test_every_language_renders_the_pages_and_the_menu_lists_all_fifteen(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path, sources=True))
    page = client.get("/admin/").text
    menu = re.findall(r'<button name="lang" value="([^"]+)"', page)
    assert menu == list(LANGUAGES)
    assert page.count('class="flag"') == 16 and "Twemoji" not in page  # fifteen in the menu, one on the button
    for code in LANGUAGES:
        client.post("/admin/lang", data={"lang": code, "next": "/admin/config"})
        html = client.get("/admin/config").text
        assert f'<html lang="{HTML_LANG[code]}"' in html, code
        if code != "en":
            assert CATALOGS.get(code, FR).get("Settings", "Settings") in html or code in BASES, code
    client.post("/admin/lang", data={"lang": "ja", "next": "/admin/config"})
    assert CATALOGS["ja"] != FR and CATALOGS["ja"]["Settings"] in client.get("/admin/config").text


def test_the_flags_font_is_served_with_its_licence(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path, sources=True))
    reply = client.get("/admin/static/twemoji-flags.woff2")
    assert reply.status_code == 200 and reply.headers["content-type"] == "font/woff2" and len(reply.content) > 1000
    css = client.get("/admin/static/app.css").text
    assert "Twemoji Flags" in css and "U+1F1E6-1F1FF" in css


def test_the_root_page_speaks_the_browsers_language_too(tmp_path: Path) -> None:
    client = _client(tmp_path, sources=True)
    page = client.get("/", headers={"accept": "text/html", "accept-language": "de-DE,de;q=0.9"}).text
    assert 'lang="de"' in page and CATALOGS["de"]["Open the administration"] in page
