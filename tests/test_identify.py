import pytest

from lantern_bridge.identify import identify


def test_a_base_game() -> None:
    who = identify("sub/Frost Garden [0100ABCD00010000][v0].nsp")
    assert who is not None
    assert (who.type, who.title_id, who.app_id, who.version_code, who.name, who.format) == (
        "base",
        "0100ABCD00010000",
        "0100ABCD00010000",
        0,
        "Frost Garden",
        "nsp",
    )
    assert who.priority == 0


def test_an_update_belongs_to_its_base() -> None:
    who = identify("Frost Garden [0100ABCD00010800][v65536].nsz")
    assert who is not None
    assert (who.type, who.title_id, who.version_code, who.format, who.priority) == (
        "update",
        "0100ABCD00010000",
        65536,
        "nsz",
        1,
    )


def test_a_dlc_belongs_to_its_base() -> None:
    who = identify("Frost Garden - Winter [0100abcd00011001][v0].xci")
    assert who is not None
    assert (who.type, who.title_id, who.app_id, who.priority) == ("dlc", "0100ABCD00010000", "0100ABCD00011001", 2)


@pytest.mark.parametrize("name", ["readme.txt", "Frost Garden.nsp", "Frost [0100ABCD00010000].zip", "[xyz][v1].nsp"])
def test_names_that_do_not_say_enough_are_not_guessed(name: str) -> None:
    assert identify(name) is None
