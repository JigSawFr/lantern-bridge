import pytest

from lantern_bridge import SPEC_DRAFT, __version__
from lantern_bridge.__main__ import main


def test_version_is_printed(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stop:
        main(["--version"])
    assert stop.value.code == 0
    assert capsys.readouterr().out.strip() == f"lantern-bridge {__version__} (Lantern draft {SPEC_DRAFT})"


def test_no_argument_prints_the_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "serve" in capsys.readouterr().out


def test_serve_refuses_an_invalid_uid() -> None:
    with pytest.raises(ValueError, match="uid"):
        main(["serve", "--uid", "not-a-uuid"])


def test_unknown_option_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stop:
        main(["--nope"])
    assert stop.value.code == 2
    assert "--nope" in capsys.readouterr().err
