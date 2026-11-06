#!/usr/bin/env python3
"""Builds the Lantern Bridge's website into <out-dir> (site/README.md).

    python3 tools/build_site.py <out-dir>

One page, in the fifteen languages of Boréale's sites (English at the root,
each other language in its folder): what the bridge reads, how it works, how
to run it from a checkout and what makes it distrustful. site/template.html
is the page around site/home.html; each {{key}} is the key's text in the
language's catalog, site/i18n/<language>.json. What Boréale's sites share
(the languages, the header's two menus, the links out and their UTM tags, the
audience, the fonts and marks) is boreale-site's, the submodule
library/boreale-site. Served at https://bridge.lantern.boreale.dev/
(SITE_URL for another address). Python's standard library only.
"""

import html
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "library" / "boreale-site"))
import boreale_site as boreale  # noqa: E402  (library/boreale-site)
from boreale_site import CODES, LANGUAGES, OG_LOCALES, fill, folder  # noqa: E402

SITE = ROOT / "site"
REPO = "https://github.com/JigSawFr/lantern-bridge"
LANTERN_URL = "https://lantern.boreale.dev/"
SDK_URL = "https://sdk.lantern.boreale.dev/"
# This site in boreale-site's sites.json: its address, its utm_source, its place in the menu.
BRIDGE = boreale.Site("bridge", os.environ.get("SITE_URL"))
SITE_URL = BRIDGE.url
ENGLISH = CODES[0]
VERSIONS: dict = {}


# ---- The page.


def page_header(language: str, catalog: dict, root: str) -> str:
    return BRIDGE.header(
        language,
        catalog,
        root,
        "",
        links=[
            ("nav_spec", f"{LANTERN_URL}spec/"),
            ("footer_conformance", f"{LANTERN_URL}conformance/"),
            ("nav_sdk", SDK_URL),
        ],
        cta=("bridge_cta_link", REPO),
        github=REPO,
        label=catalog["nav_label"],
        latest=VERSIONS,
    )


def page_footer(language: str, catalog: dict, root: str) -> str:
    return BRIDGE.footer(
        language,
        catalog,
        root,
        columns=[
            (
                catalog["nav_project"],
                [
                    (catalog["nav_spec"], f"{LANTERN_URL}spec/"),
                    (catalog["footer_conformance"], f"{LANTERN_URL}conformance/"),
                    (catalog["nav_sdk"], SDK_URL),
                ],
            ),
            (catalog["footer_label"], [("GitHub", REPO), (catalog["footer_issues"], f"{REPO}/issues")]),
        ],
        fine=catalog["footer_trademarks"],
    )


def page_html(template: str, language: str, catalog: dict) -> str:
    root = "../" * (0 if language == ENGLISH else 1)
    values = dict(catalog)
    values.update(
        {
            "lang": boreale.html_lang(language),
            "root": root,
            "home": folder(language),
            "site": SITE_URL,
            "page": "bridge",
            "url": SITE_URL + folder(language),
            "title": catalog["title_bridge"],
            "description": html.escape(catalog["description_bridge"], quote=True),
            "og_locale": OG_LOCALES[language],
            "first_visit": "",
            "indexing": BRIDGE.indexing(language, ""),
            "skip": BRIDGE.skip(catalog),
            "sky": BRIDGE.sky(home=False),
            "header": page_header(language, catalog, root),
            "crumbs": "",
            "main_class": ' class="wrap"',
            "footer": page_footer(language, catalog, root),
            "audience": BRIDGE.audience(root),
            "repo": REPO,
            "sdk": SDK_URL,
            "hero": (
                f'<div class="wrap hero small">\n    <span class="eyebrow">{catalog["bridge_eyebrow"]}</span>\n'
                f'    <h1>{catalog["bridge_title"]}</h1>\n    <p class="lede">{catalog["bridge_lede"]}</p>\n  </div>'
            ),
            "scripts": (
                f'<script src="{root}assets/code.js" defer data-copy="{html.escape(catalog["ui_copy"], quote=True)}" '
                f'data-copied="{html.escape(catalog["ui_copied"], quote=True)}" '
                f'data-failed="{html.escape(catalog["ui_copy_failed"], quote=True)}"></script>'
            ),
        }
    )
    body = (SITE / "home.html").read_text(encoding="utf-8")
    values["body"] = fill(body, values)
    return BRIDGE.links_out(fill(template, values))


def main() -> None:
    global VERSIONS
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    out = Path(sys.argv[1])
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    shutil.copytree(SITE / "assets", out / "assets")
    boreale.copy_assets(out)
    catalogs = boreale.catalogs(SITE / "i18n")
    template = (SITE / "template.html").read_text(encoding="utf-8")
    VERSIONS = boreale.latest_versions()
    urls = []
    for language, _, _, _ in LANGUAGES:
        target = out / folder(language) / "index.html"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(page_html(template, language, catalogs[language]), encoding="utf-8")
        urls.append(SITE_URL + folder(language))
    (out / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "".join(f"  <url><loc>{html.escape(u)}</loc></url>\n" for u in urls)
        + "</urlset>\n",
        encoding="utf-8",
    )
    (out / "robots.txt").write_text(f"User-agent: *\nAllow: /\nSitemap: {SITE_URL}sitemap.xml\n", encoding="utf-8")
    host = boreale.urlparse(SITE_URL).hostname if hasattr(boreale, "urlparse") else SITE_URL.split("/")[2]
    if host and not host.endswith("github.io"):
        (out / "CNAME").write_text(host + "\n", encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")
    print(f"build_site: {len(urls)} pages in {out}")


if __name__ == "__main__":
    main()
