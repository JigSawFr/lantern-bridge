from fastapi.testclient import TestClient
from lantern.uid import new_uid

from lantern_bridge import SPEC_DRAFT
from lantern_bridge.app import make_app


def test_handshake_is_the_toolkits() -> None:
    uid = new_uid()
    client = TestClient(make_app(name="Test shelf", uid=uid))
    reply = client.get("/.well-known/lantern")
    assert reply.status_code == 200
    document = reply.json()
    assert document["uid"] == uid
    assert document["name"] == "Test shelf"


def test_the_followed_draft_is_the_sdks() -> None:
    import lantern._version

    assert SPEC_DRAFT == lantern._version.SPEC_DRAFT
