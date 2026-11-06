from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi.testclient import TestClient
from lantern.uid import new_uid

from lantern_bridge import SPEC_DRAFT
from lantern_bridge.app import make_app
from lantern_bridge.config import Config, FolderSource


def _config(root: Path | None = None, uid: str | None = None, *, public: bool = True) -> Config:
    """Public by default here, so that the tests read the library without pairing a device."""
    sources = [FolderSource("games", root)] if root is not None else []
    return Config(uid=uid or new_uid(), name="Test shelf", sources=sources, public=public)


def test_handshake_is_the_toolkits(tmp_path: Path) -> None:
    config = _config()
    client = TestClient(make_app(config, tmp_path / "state", run=False))
    reply = client.get("/.well-known/lantern")
    assert reply.status_code == 200
    document = reply.json()
    assert document["uid"] == config.uid
    assert document["name"] == "Test shelf"


def test_the_followed_draft_is_the_sdks() -> None:
    import lantern._version

    assert SPEC_DRAFT == lantern._version.SPEC_DRAFT


def test_a_folder_is_listed_once_hashed_and_served(tmp_path: Path) -> None:
    root = tmp_path / "games"
    root.mkdir()
    payload = b"frost" * 1000
    (root / "Frost Garden [0100ABCD00010000][v0].nsp").write_bytes(payload)
    (root / "Frost Garden [0100ABCD00010800][v65536].nsp").write_bytes(b"update")
    (root / "readme.txt").write_bytes(b"not a package")
    app = make_app(_config(root), tmp_path / "state", run=False)
    with TestClient(app) as client:
        before = client.get("/api/lantern/v1/apps?platform=switch")
        assert before.status_code == 200
        assert before.json()["items"] == []
        runtime = app.state.runtime
        assert runtime.cycle() == 2
        items = client.get("/api/lantern/v1/apps?platform=switch").json()["items"]
        assert sorted(i["type"] for i in items) == ["base", "update"]
        base = next(i for i in items if i["type"] == "base")
        assert base["title_id"] == "0100ABCD00010000"
        assert base["sha256"] == hashlib.sha256(payload).hexdigest()
        assert base["name"] == "Frost Garden"
        served = client.get(base["file"], headers={"Range": "bytes=0-4"})
        assert served.status_code == 206
        assert served.content == b"frost"


def test_a_removed_file_leaves_the_library(tmp_path: Path) -> None:
    root = tmp_path / "games"
    root.mkdir()
    file = root / "Moss [0100ABCD00020000][v0].nsp"
    file.write_bytes(b"moss")
    app = make_app(_config(root), tmp_path / "state", run=False)
    with TestClient(app) as client:
        app.state.runtime.cycle()
        assert len(client.get("/api/lantern/v1/apps?platform=switch").json()["items"]) == 1
        file.unlink()
        app.state.runtime.cycle()
        assert client.get("/api/lantern/v1/apps?platform=switch").json()["items"] == []


def test_accounts_and_index_are_kept_in_the_state_directory(tmp_path: Path) -> None:
    state = tmp_path / "state"
    with TestClient(make_app(_config(), state, run=False)):
        pass
    assert (state / "accounts.sqlite3").exists()
    assert (state / "index.sqlite3").exists()


def test_the_same_bytes_under_two_names_are_one_app(tmp_path: Path) -> None:
    root = tmp_path / "games"
    (root / "a").mkdir(parents=True)
    (root / "b").mkdir()
    for folder in ("a", "b"):
        (root / folder / "Moss [0100ABCD00020000][v0].nsp").write_bytes(b"moss")
    app = make_app(_config(root), tmp_path / "state", run=False)
    with TestClient(app) as client:
        app.state.runtime.cycle()
        assert len(client.get("/api/lantern/v1/apps?platform=switch").json()["items"]) == 1
        (root / "a" / "Moss [0100ABCD00020000][v0].nsp").unlink()
        app.state.runtime.cycle()
        assert len(client.get("/api/lantern/v1/apps?platform=switch").json()["items"]) == 1


def test_every_title_of_an_app_has_a_sheet_named_by_its_base(tmp_path: Path) -> None:
    root = tmp_path / "games"
    root.mkdir()
    (root / "Frost Garden [0100ABCD00010000][v0].nsp").write_bytes(b"base")
    (root / "Frost Garden - Winter [0100ABCD00011001][v0].nsp").write_bytes(b"dlc")
    app = make_app(_config(root), tmp_path / "state", run=False)
    with TestClient(app) as client:
        app.state.runtime.cycle()
        sheet = client.get("/api/lantern/v1/titles/0100ABCD00010000?platform=switch")
        assert sheet.status_code == 200
        assert sheet.json()["name"] == "Frost Garden"
        (root / "Frost Garden [0100ABCD00010000][v0].nsp").unlink()
        (root / "Frost Garden - Winter [0100ABCD00011001][v0].nsp").unlink()
        app.state.runtime.cycle()
        assert client.get("/api/lantern/v1/titles/0100ABCD00010000?platform=switch").status_code == 404


def test_pairing_is_required_by_default(tmp_path: Path) -> None:
    root = tmp_path / "games"
    root.mkdir()
    (root / "Moss [0100ABCD00020000][v0].nsp").write_bytes(b"moss")
    app = make_app(_config(root, public=False), tmp_path / "state", run=False)
    with TestClient(app) as client:
        app.state.runtime.cycle()
        assert client.get("/.well-known/lantern").status_code == 200  # the handshake is always open
        assert client.get("/api/lantern/v1/apps?platform=switch").status_code == 401


def test_a_public_bridge_can_be_read_without_pairing(tmp_path: Path) -> None:
    root = tmp_path / "games"
    root.mkdir()
    (root / "Moss [0100ABCD00020000][v0].nsp").write_bytes(b"moss")
    app = make_app(_config(root), tmp_path / "state", run=False)
    with TestClient(app) as client:
        app.state.runtime.cycle()
        assert len(client.get("/api/lantern/v1/apps?platform=switch").json()["items"]) == 1
