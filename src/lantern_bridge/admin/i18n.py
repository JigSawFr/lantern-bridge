# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""The pages' languages: Boréale's fifteen. English is the text itself (the keys); every other language has a
catalog in ``locales/<code>.json`` that maps an English text to its translation. A text a catalog does not have
falls back to the language it is a variant of (Canadian French to French, Latin American Spanish to Spanish,
Brazilian Portuguese to Portuguese), then to English."""

from __future__ import annotations

import json
from collections.abc import Callable
from importlib import resources

COOKIE = "lb_lang"

#: code, the name in its own words, the flag (a country), the html lang, the language it is a variant of.
_TABLE: tuple[tuple[str, str, str, str, str | None], ...] = (
    ("en", "English", "US", "en", None),
    ("fr", "Français", "FR", "fr", None),
    ("fr-CA", "Français (Canada)", "CA", "fr-CA", "fr"),
    ("de", "Deutsch", "DE", "de", None),
    ("es", "Español", "ES", "es", None),
    ("es-419", "Español (Latinoamérica)", "MX", "es-419", "es"),
    ("it", "Italiano", "IT", "it", None),
    ("nl", "Nederlands", "NL", "nl", None),
    ("pt", "Português", "PT", "pt-PT", None),
    ("pt-BR", "Português (Brasil)", "BR", "pt-BR", "pt"),
    ("ru", "Русский", "RU", "ru", None),
    ("ja", "日本語", "JP", "ja", None),
    ("ko", "한국어", "KR", "ko", None),
    ("zh-Hans", "简体中文", "CN", "zh-Hans", None),
    ("zh-Hant", "繁體中文", "TW", "zh-Hant", None),
)
LANGUAGES = tuple(code for code, *_ in _TABLE)
NAMES = {code: name for code, name, *_ in _TABLE}
FLAGS = {code: flag for code, _, flag, *_ in _TABLE}
HTML_LANG = {code: html for code, _, _, html, _ in _TABLE}
_BASE = {code: base for code, *_, base in _TABLE if base}


def flag(code: str) -> str:
    """A language's flag: two regional indicator letters, drawn by the bundled flags font."""
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in FLAGS.get(code, "US"))


def _load(code: str) -> dict[str, str]:
    try:
        text = (resources.files("lantern_bridge.admin") / "locales" / f"{code}.json").read_text("utf-8")
    except FileNotFoundError:
        return {}
    data = json.loads(text)
    return {str(k): str(v) for k, v in data.items()}


CATALOGS: dict[str, dict[str, str]] = {code: _load(code) for code in LANGUAGES if code != "en"}
#: Kept by name: the tests read the French catalog to check that the pages have no English left.
FR = CATALOGS["fr"]

#: What a browser's wish maps to when its tag is not one of ours as written.
_ALIASES = {
    "en": "en",
    "zh-tw": "zh-Hant",
    "zh-hk": "zh-Hant",
    "zh-mo": "zh-Hant",
    "zh-hant": "zh-Hant",
    "zh": "zh-Hans",
    "pt-pt": "pt",
    "es-es": "es",
    "fr-fr": "fr",
}
_LATAM = {
    "mx",
    "ar",
    "bo",
    "cl",
    "co",
    "cr",
    "cu",
    "do",
    "ec",
    "gt",
    "hn",
    "ni",
    "pa",
    "pe",
    "pr",
    "py",
    "sv",
    "us",
    "uy",
    "ve",
    "419",
}


def match(tag: str) -> str | None:
    """Our language for one browser tag (`pt-BR`, `zh-CN`, `es-AR`…), or None."""
    low = tag.strip().lower()
    for code in LANGUAGES:
        if code.lower() == low:
            return code
    if low in _ALIASES:
        return _ALIASES[low]
    primary, _, rest = low.partition("-")
    if primary == "es" and rest.split("-")[-1] in _LATAM:
        return "es-419"
    if primary == "zh":
        return "zh-Hant" if "hant" in rest else "zh-Hans"
    return next((code for code in LANGUAGES if code.lower() == primary), None)


def pick(cookie: str | None, accept: str | None) -> str:
    """The page's language: the cookie's, else the first of the browser's wishes that is offered, else English."""
    if cookie in LANGUAGES:
        return cookie or "en"
    for part in (accept or "").split(","):
        code = match(part.split(";")[0])
        if code is not None:
            return code
    return "en"


def translator(lang: str) -> Callable[..., str]:
    chain = [CATALOGS[lang]] if lang in CATALOGS else []
    base = _BASE.get(lang)
    if base is not None:
        chain.append(CATALOGS[base])

    def tr(text: str, **fields: object) -> str:
        shown = next((c[text] for c in chain if text in c), text)
        return shown.format(**fields) if fields else shown

    return tr
