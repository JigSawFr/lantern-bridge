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
import json
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


def diagram(catalog: dict) -> str:
    """Sources, the Bridge, the clients: three boxes joined by arrows, in HTML so that the words are the catalog's."""
    names = "".join(
        f"<li>{catalog[key]}</li>"
        for key in ("bridge_src_folders_t", "bridge_src_tinfoil_t", "bridge_src_ownfoil_t", "bridge_src_aerofoil_t")
    )
    return (
        f'<figure class="flow-bridge" role="group" aria-label="{html.escape(catalog["diag_alt"], quote=True)}">'
        f'<div class="fb-box"><p class="fb-title">{catalog["diag_sources"]}</p><ul>{names}</ul></div>'
        f'<span class="fb-arrow" aria-hidden="true"></span>'
        f'<div class="fb-box fb-core"><p class="fb-title">Lantern Bridge</p><p>{catalog["diag_bridge"]}</p></div>'
        f'<span class="fb-arrow" aria-hidden="true"></span>'
        f'<div class="fb-box"><p class="fb-title">{catalog["diag_clients"]}</p></div></figure>'
    )


# What each kind of source gives the library, by feature (README.md, "A Tinfoil shop / An Ownfoil / An AeroFoil as a
# source"): 1 where the source does it. The order of the columns is bridge_src_*_t's.
COMPAT_COLUMNS = ("folders", "tinfoil", "ownfoil", "aerofoil")
COMPAT_ROWS = (
    ("cmp_row_library", (1, 1, 1, 1)),
    ("cmp_row_place", (1, 0, 0, 0)),
    ("cmp_row_sheets", (0, 0, 1, 0)),
    ("cmp_row_live", (0, 0, 1, 0)),
    ("cmp_row_saves", (0, 0, 0, 1)),
    ("cmp_row_cheats", (0, 0, 0, 1)),
    ("cmp_row_requests", (0, 0, 0, 1)),
)


def compat(catalog: dict) -> str:
    """The sources against what they give, as a table: a tick and, for a screen reader, the word."""
    title = html.escape(catalog["cmp_title"])
    head = "".join(f'<th scope="col">{catalog[f"bridge_src_{c}_t"]}</th>' for c in COMPAT_COLUMNS)
    rows = []
    for key, marks in COMPAT_ROWS:
        cells = "".join(
            f'<td class="cmp-{"yes" if m else "no"}"><span aria-hidden="true">{"✓" if m else "&ndash;"}</span>'
            f'<span class="sr">{catalog["cmp_yes" if m else "cmp_no"]}</span></td>'
            for m in marks
        )
        rows.append(f'<tr><th scope="row">{catalog[key]}</th>{cells}</tr>')
    return (
        f'<div class="table compat" tabindex="0" role="region" aria-label="{title}"><table>'
        f'<caption class="sr">{title}</caption><thead><tr><td></td>{head}</tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )


def page_html(template: str, language: str, catalog: dict, lost: bool = False) -> str:
    root = "../" * (0 if language == ENGLISH else 1)
    if lost:
        root = boreale.urlparse(SITE_URL).path  # 404.html is served at any depth
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
            "compat": compat(catalog),
            "faq": faq(catalog),
            "repo": REPO,
            "sdk": SDK_URL,
            "hero": (
                f'<div class="wrap hero small hero-split">\n    <div class="hero-text"><span class="eyebrow">{catalog["bridge_eyebrow"]}</span>\n'
                f'    <h1>{catalog["bridge_title"]}</h1>\n    <p class="lede">{catalog["bridge_lede"]}</p></div>\n'
                f"    {diagram(catalog)}\n  </div>"
            ),
            "scripts": (
                f'<script src="{root}assets/count.js" defer></script>'
                f'<script src="{root}assets/code.js" defer data-copy="{html.escape(catalog["ui_copy"], quote=True)}" '
                f'data-copied="{html.escape(catalog["ui_copied"], quote=True)}" '
                f'data-failed="{html.escape(catalog["ui_copy_failed"], quote=True)}"></script>'
                f'<script src="{root}assets/sim.js" defer></script>'
                f'<script src="{root}assets/config.js" defer></script>'
                f'<script src="{root}assets/identify.js" defer></script>'
            ),
        }
    )
    values["structured"] = structured_data(language, catalog)
    if lost:
        # The root's 404.html (the English one) sends a visitor reading another language to that language's 404.html
        # (boreale-site's lang.js); the site's path tells it where.
        values.update(
            {
                "title": catalog["lost_page_title"],
                "description": html.escape(catalog["lost_text"], quote=True),
                "indexing": BRIDGE.indexing(language, "", True),
                "structured": "",
                "hero": "",
                "scripts": "",
                "lost": f' data-lost="{root}"' if language == ENGLISH else "",
            }
        )
        body = (SITE / "lost.html").read_text(encoding="utf-8")
    else:
        values["lost"] = ""
        body = (SITE / "home.html").read_text(encoding="utf-8")
    values["body"] = fill(body, values)
    return BRIDGE.links_out(fill(template, values))


FAQ = tuple((f"faq_{n}_q", f"faq_{n}_a") for n in range(1, 6))


def faq(catalog: dict) -> str:
    """The questions about trust, one fold each: native <details>, no script."""
    items = "".join(
        f'<details class="faq-item"><summary>{html.escape(catalog[q])}</summary><p>{html.escape(catalog[a])}</p></details>'
        for q, a in FAQ
    )
    return f'<div class="faq">{items}</div>'


def structured_data(language: str, catalog: dict) -> str:
    """What the page is, for search engines: schema.org's SoftwareApplication, in the page's language. A data block,
    which the Content-Security-Policy leaves alone."""
    data = {
        "@context": "https://schema.org",
        "@type": "SoftwareApplication",
        "name": "Lantern Bridge",
        "description": catalog["description_bridge"],
        "url": SITE_URL + folder(language),
        "codeRepository": REPO,
        "applicationCategory": "UtilitiesApplication",
        "operatingSystem": "Linux, macOS, Windows, Docker",
        "license": "https://spdx.org/licenses/MIT.html",
        "inLanguage": boreale.html_lang(language),
        "isPartOf": {"@type": "SoftwareSourceCode", "name": "Lantern", "url": LANTERN_URL},
    }
    questions = {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "inLanguage": boreale.html_lang(language),
        "mainEntity": [
            {
                "@type": "Question",
                "name": catalog[q],
                "acceptedAnswer": {"@type": "Answer", "text": catalog[a]},
            }
            for q, a in FAQ
        ],
    }
    return "".join(
        '<script type="application/ld+json">'
        + json.dumps(block, ensure_ascii=False).replace("<", "\\u003c")
        + "</script>"
        for block in (data, questions)
    )


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
        (target.parent / "404.html").write_text(
            page_html(template, language, catalogs[language], lost=True), encoding="utf-8"
        )
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
