"""The setup guide: four steps that tick themselves."""

from __future__ import annotations

import re
from pathlib import Path

from lantern_bridge.admin import attention
from lantern_bridge.admin.i18n import FR
from lantern_bridge.runtime import Runtime
from test_admin import _admin, _client, _csrf


def _state(runtime: Runtime) -> dict[str, int]:
    counts = runtime.index.counts()
    apps, titles = runtime.projection.served()
    return {
        "hashed": counts["hashed"],
        "pending": counts["pending"],
        "total": counts["hashed"] + counts["pending"],
        "apps": apps,
        "titles": titles,
    }


def test_an_empty_bridge_starts_with_the_form_and_leads_back_to_the_guide(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path))
    overview = client.get("/admin/").text
    assert 'href="/admin/start"' in overview and "Start the setup" in overview
    page = client.get("/admin/start")
    assert page.status_code == 200 and "Setup guide" in page.text and 'name="back" value="start"' in page.text
    assert 'aria-current="step"' in page.text and "Add a source" in page.text
    (tmp_path / "games" / "a.nsp").write_bytes(b"x")
    added = client.post(
        "/admin/config/sources",
        data={
            "csrf": _csrf(page.text),
            "back": "start",
            "kind": "folder",
            "id": "games",
            "where": str(tmp_path / "games"),
        },
    )
    assert added.status_code == 303 and added.headers["location"].startswith("/admin/start")
    after = client.get("/admin/start").text
    assert 'name="back"' not in after  # the form is gone once a source is saved
    assert re.search(r'class="done"[^>]*>\s*<span class="pill ok">done', after)
    assert "was not applied" not in after  # the source runs at once: no restart
    assert [x.id for x in client.app.state.runtime.config.sources] == ["games"]  # type: ignore[attr-defined]


def test_the_form_in_the_settings_still_leads_to_the_settings(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path))
    page = client.get("/admin/config")
    assert 'name="back"' not in page.text
    added = client.post(
        "/admin/config/sources",
        data={
            "csrf": _csrf(page.text),
            "kind": "folder",
            "id": "games",
            "where": str(tmp_path / "games"),
            "back": "elsewhere",
        },
    )
    assert added.headers["location"].startswith("/admin/config")  # only the guide is a way back


def test_the_steps_follow_the_bridge_and_the_block_comes_alone(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path, sources=True))
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    (tmp_path / "games" / "Frost Garden [0100ABCD00010000][v0].nsp").write_bytes(b"x" * 4096)
    steps = attention.guide(runtime, _state(runtime), paired=0, uid="u", saved=["games"], restart=False)
    assert [s["done"] for s in steps] == [True, False, False, False] and steps[1]["current"]
    runtime.cycle(batch=100)
    steps = attention.guide(runtime, _state(runtime), paired=1, uid="u", saved=["games"], restart=False)
    assert [s["done"] for s in steps][:2] == [True, True] and steps[3]["done"]
    fragment = client.get("/admin/start?fragment=1").text
    assert "<html" not in fragment and "Pair a device" in fragment
    assert 'data-live="/admin/start?fragment=1"' in client.get("/admin/start").text


def test_a_public_bridge_needs_no_pairing_and_a_private_one_is_told(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path, sources=True))
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    runtime.cycle(batch=100)
    assert "No device is paired yet" in client.get("/admin/").text
    runtime.config.public = True
    assert attention.guide(runtime, _state(runtime), paired=0, uid="u", saved=["games"], restart=False)[3]["done"]
    assert "No device is paired yet" not in client.get("/admin/").text


def test_every_text_of_the_guide_has_its_french() -> None:
    source = Path(attention.__file__).read_text(encoding="utf-8")
    texts = re.findall(r'"(?:title|text|link)": "([^"]+)"', source)
    texts += re.findall(r'else "([^"]+)",\n\s+"detail"', source)
    assert len(texts) >= 8 and all(t in FR for t in texts), [t for t in texts if t not in FR]
