from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote

import httpx
import pytest
from fastapi.testclient import TestClient
from lantern._generated.models import Save
from lantern.server.http import ProblemError
from lantern.server.saves import UploadSession, new_save_id
from lantern.server.title_requests import StoredRequest
from lantern.uid import new_uid

from lantern_bridge.app import make_app
from lantern_bridge.config import AerofoilSource, Config, Login, load, save
from lantern_bridge.sources.aerofoil import AeroFoil
from lantern_bridge.sources.tinfoil import UpstreamError
from lantern_bridge.upstream import AeroRequests, AeroSaves, BridgeLibrary, Cheats, Ledger, Logins

BASE = "http://aerofoil.test:8465/"
GAME = "0100ABCD00010000"
BUILD = "0123456789ABCDEF"


def _zip(text: str = "save") -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr(zipfile.ZipInfo("main", (2026, 1, 1, 0, 0, 0)), text)  # a fixed date: the same bytes each time
    return out.getvalue()


class Fake:
    """A small AeroFoil: one set of saves, cheats and requests per user (the Basic login says which)."""

    def __init__(self) -> None:
        self.saves: dict[str, dict[str, dict[str, Any]]] = {}
        self.cheats: dict[tuple[str, str], tuple[str, str]] = {
            (GAME, BUILD): ("[Infinite hearts]\n04000000 00000001", "mine")
        }
        self.requests: dict[str, list[dict[str, Any]]] = {}
        self.calls: list[tuple[str, str, str]] = []
        self.broken = False
        self.sessions: dict[str, str] = {}  # session cookie -> user
        self.passwords: dict[str, str] = {"svc": "x", "alice": "pw"}
        self.logins = 0

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        header = request.headers.get("authorization", "")
        user = base64.b64decode(header[6:]).decode().split(":")[0] if header.startswith("Basic ") else "anonymous"
        path, method = request.url.path, request.method
        if path == "/login" and method == "POST":  # the web login: the way to the session-only calls below
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            if self.passwords.get(form.get("user", "")) != form.get("password"):
                return httpx.Response(302, headers={"location": "/login"})
            self.logins += 1
            cookie = f"s{self.logins}"
            self.sessions[cookie] = form["user"]
            return httpx.Response(302, headers={"location": "/", "set-cookie": f"session={cookie}; Path=/"})
        wants_session = (
            path.startswith("/api/requests")
            or (path == "/api/cheats")
            or (path.startswith("/api/cheats/") and method == "DELETE")
        )
        if wants_session:  # a stock AeroFoil does not read Basic here: it sends the caller to its login page
            match = re.search(r"session=(\w+)", request.headers.get("cookie", ""))
            user = self.sessions.get(match.group(1), "") if match else ""
            if not user:
                self.calls.append(("anonymous", method, path))
                return httpx.Response(302, headers={"location": "/login?next=" + path})
        self.calls.append((user, method, path))
        if self.broken:
            return httpx.Response(503)
        mine = self.saves.setdefault(user, {})
        if path == "/":
            return httpx.Response(200, json={"files": []})
        if path == "/api/cheats" and method == "GET":
            rows = [
                {"title_id": t, "build_id": b, "size": len(text.encode()), "note": note}
                for (t, b), (text, note) in self.cheats.items()
            ]
            return httpx.Response(200, json={"success": True, "cheats": rows})
        if path == "/api/cheats" and method == "POST":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.cheats[(form["title_id"], form["build_id"])] = (form["content"], form.get("note", ""))
            return httpx.Response(200, json={"success": True})
        if path.startswith("/api/cheats/"):
            title, build = path.split("/")[3:5]
            if method == "DELETE":
                self.cheats.pop((title, build), None)
                return httpx.Response(200, json={"success": True})
            return httpx.Response(200, text=self.cheats[(title, build)][0])
        if path == "/api/saves/list":
            rows = [
                {
                    "title_id": s["title"],
                    "save_id": i,
                    "save_type": s["type"],
                    "size": len(s["data"]),
                    "created_ts": 1700000000,
                    "note": s["note"],
                }
                for i, s in mine.items()
            ]
            rows.append({"title_id": GAME, "save_id": "latest", "save_type": "account", "latest_directory": True})
            return httpx.Response(200, json={"saves": rows})
        if path.startswith("/api/saves/upload/"):
            body = request.content
            kind = re.search(rb'name="save_type"\r\n\r\n(\w+)', body)
            note = re.search(rb'name="note"\r\n\r\n(.*?)\r\n', body)
            data = body[body.index(b"PK") : body.rindex(b"\r\n--")]
            ident = f"up{len(mine) + 1}"
            mine[ident] = {
                "title": path.rsplit("/", 1)[-1],
                "type": (kind.group(1) if kind else b"unknown").decode(),
                "note": note.group(1).decode() if note else "",
                "data": data,
            }
            return httpx.Response(200, json={"success": True, "save_id": ident})
        if path.startswith("/api/saves/download/"):
            ident = unquote(path.split("/")[-1]).removesuffix(".zip")
            return httpx.Response(200, content=mine[ident]["data"])
        if path.startswith("/api/saves/delete/"):
            mine.pop(path.split("/")[-1], None)
            return httpx.Response(200, json={"success": True})
        if path == "/api/requests" and method == "POST":
            doc = json.loads(request.content)
            self.requests.setdefault(user, []).append(
                {
                    "id": len(self.requests.get(user, [])) + 1,
                    "title_id": doc["title_id"],
                    "title_name": doc["title_name"],
                    "status": "open",
                    "created_at": 1700000000,
                }
            )
            return httpx.Response(200, json={"success": True})
        if path == "/api/requests":
            return httpx.Response(200, json={"success": True, "requests": self.requests.get(user, [])})
        return httpx.Response(404)


def _stores(
    tmp_path: Path, fake: Fake, *, logins: tuple[Login, ...] = ()
) -> tuple[AeroSaves, AeroRequests, Cheats, BridgeLibrary, Ledger]:
    source = AerofoilSource("af", BASE, user="svc", password_env="AF_SVC", logins=logins)
    shop = AeroFoil(BASE, user="svc", password="x", client=fake.client())
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    who = Logins(source, shop, {"AF_ALICE": "pw"})
    library = BridgeLibrary(base="/api/lantern")
    return (
        AeroSaves(tmp_path / "saves", who, ledger, every=0.0),
        AeroRequests(who, ledger),
        Cheats(library, "af"),
        library,
        ledger,
    )


def _save(kind: str = "account", data: bytes | None = None, title: str = GAME) -> tuple[Save, bytes]:
    data = data or _zip()
    return (
        Save(
            platform="switch",
            title_id=title,
            save_id=new_save_id(),
            created="2026-11-06T10:00:00Z",
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            kind=kind,
            note="before the boss",
        ),
        data,
    )


# -- the client -----------------------------------------------------------------------------------------


def test_the_client_reads_and_writes_what_aerofoil_keeps() -> None:
    fake = Fake()
    shop = AeroFoil(BASE, user="svc", password="x", client=fake.client())
    assert [(c.title_id, c.build_id, c.note) for c in shop.cheats()] == [(GAME, BUILD, "mine")]
    assert "Infinite hearts" in shop.cheat(GAME, BUILD)
    ident = shop.put_save(GAME, "device", "n", _zip())
    (kept,) = shop.saves()  # the `latest` directory is not a backup
    assert (kept.save_id, kept.save_type, kept.note) == (ident, "device", "n")
    assert shop.save(GAME, ident, "device") == _zip()
    shop.delete_save(GAME, ident, "device")
    assert shop.saves() == []
    shop.ask("0100ABCD00020000", "Moss Quest")
    assert [(r.title_id, r.name, r.status) for r in shop.requests()] == [("0100ABCD00020000", "Moss Quest", "open")]
    fake.broken = True
    with pytest.raises(UpstreamError, match="503"):
        shop.saves()


# -- saves ------------------------------------------------------------------------------------------------


def test_a_backup_made_here_is_kept_on_the_aerofoil_too(tmp_path: Path) -> None:
    fake = Fake()
    saves, _, _, _, ledger = _stores(tmp_path, fake)
    kept, data = _save()
    saves.keep("alice", kept, data)
    assert [s.save_id for s in saves.saves("alice")] == [kept.save_id]  # not imported a second time
    assert next(iter(fake.saves["svc"].values()))["data"] == data
    assert saves.archive("alice", kept.save_id) is not None
    assert saves.drop("alice", kept.save_id) is True
    assert fake.saves["svc"] == {} and saves.saves("alice") == []
    ledger.close()


def test_a_backup_only_the_aerofoil_has_comes_over_and_goes_with_it(tmp_path: Path) -> None:
    fake = Fake()
    saves, _, _, _, ledger = _stores(tmp_path, fake)
    shop = AeroFoil(BASE, user="svc", password="x", client=fake.client())
    ident = shop.put_save(GAME, "device", "from CyberFoil", _zip("other"))
    (came,) = saves.saves("alice")
    assert (came.title_id, came.kind, came.note, came.platform) == (GAME, "device", "from CyberFoil", "switch")
    assert came.sha256 == hashlib.sha256(_zip("other")).hexdigest() and came.size == len(_zip("other"))
    shop.delete_save(GAME, ident, "device")
    assert saves.saves("alice") == []
    ledger.close()


def test_a_file_that_is_not_a_zip_is_not_brought_over(tmp_path: Path) -> None:
    fake = Fake()
    saves, _, _, _, ledger = _stores(tmp_path, fake)
    fake.saves.setdefault("svc", {})["x1"] = {"title": GAME, "type": "account", "note": "", "data": b"not a zip"}
    assert saves.saves("alice") == []
    ledger.close()


def test_an_account_acts_as_its_own_aerofoil_user(tmp_path: Path) -> None:
    fake = Fake()
    saves, _, _, _, ledger = _stores(tmp_path, fake, logins=(Login("alice", "alice-af", "AF_ALICE"),))
    kept, data = _save()
    saves.keep("alice", kept, data)
    saves.keep("bob", *_save("device"))  # no login: the service account
    assert len(fake.saves["alice-af"]) == 1 and len(fake.saves["svc"]) == 1
    assert [s.kind for s in saves.saves("bob")] == ["device"]
    ledger.close()


def test_a_backup_kept_while_the_aerofoil_was_away_is_sent_later(tmp_path: Path) -> None:
    fake = Fake()
    saves, _, _, _, ledger = _stores(tmp_path, fake)
    fake.broken = True
    kept, data = _save()
    saves.keep("alice", kept, data)  # kept here, the mirror is for later
    assert fake.saves == {}
    fake.broken = False
    assert [s.save_id for s in saves.saves("alice")] == [kept.save_id]
    assert len(fake.saves["svc"]) == 1
    ledger.close()


def test_a_resumable_upload_ends_on_the_aerofoil(tmp_path: Path) -> None:
    fake = Fake()
    saves, _, _, _, ledger = _stores(tmp_path, fake)
    kept, data = _save("device")
    session = UploadSession(id="u_abc", user="alice", save=kept, expires="2099-01-01T00:00:00Z")
    saves.open_upload(session)
    saves.append("u_abc", data)
    saves.keep_upload("u_abc", kept)
    assert next(iter(fake.saves["svc"].values()))["type"] == "device"
    assert saves.archive("alice", kept.save_id) is not None
    ledger.close()


def test_a_kind_the_aerofoil_does_not_know_stays_here(tmp_path: Path) -> None:
    fake = Fake()
    saves, _, _, _, ledger = _stores(tmp_path, fake)
    saves.keep("alice", *_save("bcat"))
    assert fake.saves == {} and [s.kind for s in saves.saves("alice")] == ["bcat"]
    ledger.close()


def test_nothing_is_mirrored_without_a_login(tmp_path: Path) -> None:
    fake = Fake()
    source = AerofoilSource("af", BASE)  # no service account, no login
    shop = AeroFoil(BASE, client=fake.client())
    ledger = Ledger(tmp_path / "l.sqlite3")
    saves = AeroSaves(tmp_path / "saves", Logins(source, shop, {}), ledger, every=0.0)
    saves.keep("alice", *_save())
    assert fake.calls == [] and len(saves.saves("alice")) == 1
    ledger.close()


# -- requests ---------------------------------------------------------------------------------------------


def test_a_request_is_made_on_the_aerofoil_and_its_answer_comes_back(tmp_path: Path) -> None:
    fake = Fake()
    _, requests, _, _, ledger = _stores(tmp_path, fake)
    mine = StoredRequest(
        "r_1", "alice", "switch", "0100ABCD00020000", "open", "2026-11-06T10:00:00Z", name="Moss Quest"
    )
    requests.add(mine)
    assert fake.requests["svc"][0]["title_name"] == "Moss Quest"
    assert [r.id for r in requests.requests("alice")] == ["r_1"] and requests.requests("bob") == []
    answers: list[tuple[str, str, str | None]] = []
    requests.pull(lambda *a: answers.append(a))
    assert answers == []
    fake.requests["svc"][0]["status"] = "denied"
    requests.pull(lambda *a: answers.append(a))
    assert answers == [("r_1", "declined", "Declined on the AeroFoil.")]
    done = requests.set_status("r_1", "done", updated="2026-11-07T00:00:00Z")
    assert done is not None and done.status == "done"
    assert requests.remove("bob", "r_1") is None and requests.remove("alice", "r_1") is not None
    ledger.close()


# -- cheats -----------------------------------------------------------------------------------------------


def test_cheats_come_in_change_and_go(tmp_path: Path) -> None:
    fake = Fake()
    _, _, cheats, library, ledger = _stores(tmp_path, fake)
    shop = AeroFoil(BASE, user="svc", password="x", client=fake.client())
    cheats.refresh(shop)
    (one,) = library.cheats()
    assert (one.title_id, one.build_id, one.source, one.note) == (GAME, BUILD, "af", "mine")
    fake.cheats[(GAME, BUILD)] = ("[Other]\n04000000 00000002", "new")
    cheats.refresh(shop)
    (two,) = library.cheats()
    assert two.id != one.id and two.note == "new"
    cheats.refresh(shop)
    assert library.cheats()[0].id == two.id  # unchanged: not fetched again
    del fake.cheats[(GAME, BUILD)]
    cheats.refresh(shop)
    assert library.cheats() == []
    ledger.close()


def test_a_cheat_written_here_is_written_there(tmp_path: Path) -> None:
    fake = Fake()
    _, _, cheats, library, ledger = _stores(tmp_path, fake)
    shop = AeroFoil(BASE, user="svc", password="x", client=fake.client())
    library.shop = shop
    made = library.write_cheat(
        platform="switch",
        title_id=GAME,
        build_id=BUILD,
        source="alice",
        note="n",
        text="[Fly]\n04000000 00000003",
    )
    assert fake.cheats[(GAME, BUILD)][0].startswith("[Fly]")
    cheats.refresh(shop)
    assert [c.id for c in library.cheats()] == [made.id]  # not added a second time
    assert library.delete_cheat(made.id) is True and (GAME, BUILD) not in fake.cheats
    ledger.close()


def test_a_cheat_the_aerofoil_refuses_is_refused(tmp_path: Path) -> None:
    fake = Fake()
    _, _, _, library, ledger = _stores(tmp_path, fake)
    library.shop = AeroFoil(BASE, user="svc", password="x", client=fake.client())
    fake.broken = True
    with pytest.raises(ProblemError) as caught:
        library.write_cheat(
            platform="switch", title_id=GAME, build_id=BUILD, source="alice", note=None, text="[x]\n04000000 00000001"
        )
    assert caught.value.status == 502 and library.cheats() == []
    ledger.close()


# -- config and the bridge --------------------------------------------------------------------------------


def test_the_config_keeps_the_logins(tmp_path: Path) -> None:
    path = tmp_path / "bridge.toml"
    source = AerofoilSource(
        "af", BASE, user="svc", password_env="AF_SVC", logins=(Login("alice", "alice-af", "AF_ALICE"),)
    )
    save(path, Config(uid=new_uid(), sources=[source]))
    (back,) = load(path).sources
    assert back == source
    from lantern_bridge.config import ConfigError

    for bad in (
        'logins = "x"',
        'logins = [{account = "a"}]',
        'logins = [{account = "a", user = "u", password_env = "E"}, {account = "a", user = "v", password_env = "F"}]',
    ):
        path.write_text(f'uid = "{new_uid()}"\n[[sources]]\nid = "af"\nkind = "aerofoil"\nurl = "{BASE}"\n{bad}\n')
        with pytest.raises(ConfigError):
            load(path)


def test_the_bridge_serves_the_cheats_of_its_aerofoil(tmp_path: Path) -> None:
    fake = Fake()
    config = Config(
        uid=new_uid(),
        sources=[AerofoilSource("af", BASE, user="svc", password_env="AF_SVC")],
        public=True,
        remote_rate=0.0,
    )
    app = make_app(config, tmp_path / "state", run=False)
    runtime = app.state.runtime
    runtime.shops["af"] = AeroFoil(BASE, user="svc", password="x", client=fake.client())
    runtime.cheats.library.shop = runtime.shops["af"]
    runtime.cycle()
    client = TestClient(app)
    (cheat,) = client.get("/api/lantern/v1/cheats?platform=switch").json()["cheats"]
    assert (cheat["title_id"], cheat["build_id"], cheat["source"]) == (GAME, BUILD, "af")
    assert client.get(f"/api/lantern/v1/cheats/{cheat['id']}").status_code in (200, 404)
    runtime.stop()


# -- the session a stock AeroFoil wants for the cheat list, the cheat writes and the requests -------------------


def test_the_session_is_made_once_and_kept() -> None:
    fake = Fake()
    shop = AeroFoil(BASE, user="svc", password="x", client=fake.client())
    shop.cheats()
    shop.cheats()
    shop.requests()
    assert fake.logins == 1
    assert [c for c in fake.calls if c[2] == "/api/requests"] == [("svc", "GET", "/api/requests")]


def test_each_user_has_its_own_session_on_a_shared_connection() -> None:
    fake = Fake()
    client = fake.client()
    svc = AeroFoil(BASE, user="svc", password="x", client=client)
    alice = svc.as_user("alice", "pw")
    svc.ask(GAME, "A game")
    alice.requests()
    assert fake.logins == 2
    assert fake.calls[-1][0] == "alice"
    assert [r.title_id for r in svc.requests()] == [GAME]
    assert alice.requests() == []


def test_a_session_that_ended_is_made_again() -> None:
    fake = Fake()
    shop = AeroFoil(BASE, user="svc", password="x", client=fake.client())
    shop.cheats()
    fake.sessions.clear()
    assert len(shop.cheats()) == 1
    assert fake.logins == 2


def test_a_refused_login_is_an_error_not_a_loop() -> None:
    fake = Fake()
    shop = AeroFoil(BASE, user="svc", password="wrong", client=fake.client())
    with pytest.raises(UpstreamError, match="refused the login"):
        shop.requests()
    assert fake.logins == 0


def test_the_saves_and_the_cheat_files_stay_on_basic() -> None:
    fake = Fake()
    shop = AeroFoil(BASE, user="svc", password="x", client=fake.client())
    shop.saves()
    shop.cheat(GAME, BUILD)
    assert fake.logins == 0
