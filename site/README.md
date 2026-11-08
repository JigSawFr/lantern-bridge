# The Bridge's website

`bridge.lantern.boreale.dev`: one page, in the fifteen languages of Boréale's
sites (English at the root, each other language in its folder), on GitHub
Pages (Settings › Pages › Source: GitHub Actions, custom domain
`bridge.lantern.boreale.dev`).

It says what the bridge will read (folders, Tinfoil shops, Ownfoil, AeroFoil),
how it works, who may do what (operators and their roles, accounts with presets, a page for each console, signing in through a proxy or a provider), how to run it from a checkout, and what makes it distrustful.
It says plainly that nothing is released yet, and names no draft: the text
changes with the bridge, not with the specification.

| Path | What it is |
| --- | --- |
| `site/template.html`, `site/home.html` | The page around the Bridge page, and the page. `{{key}}` is the key's text in the language's catalog. |
| `site/i18n/<language>.json` | The page's texts, in the fifteen languages. What Boréale's sites share (the menus, the footer) is `library/boreale-site`'s. |
| `site/assets/` | `site.css`, `code.js` (the copy buttons), `lang-key.js`. The fonts, marks and the chrome come from `library/boreale-site`. |
| `tools/build_site.py` | `python3 tools/build_site.py build`; `SITE_OFFLINE=1` skips boreale.dev's `versions.json`. Standard library only. |
| `tools/check_site.py` | `python3 tools/check_site.py build`: the checks every Boréale site passes (links, catalogs, Content-Security-Policy, UTM, no e-mail address). |

A text added goes in all fifteen catalogs, translated. The site's marks and
social preview are the brand kit's (`boreale/branding/bridge/`).
