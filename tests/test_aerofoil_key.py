# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""An AeroFoil that encrypts its index, read with the bridge's own key."""

from __future__ import annotations

import json
import secrets
import sys
from collections.abc import Mapping
from pathlib import Path

import httpx
import pytest
import zstandard
from Crypto.Cipher import AES, PKCS1_OAEP
from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA
from lantern.uid import new_uid

from lantern_bridge.__main__ import main
from lantern_bridge.config import AerofoilSource, Config, ConfigError, load, save
from lantern_bridge.sources import shopkey
from lantern_bridge.sources.aerofoil import AeroFoil
from lantern_bridge.sources.tinfoil import Shop, UpstreamError

BASE = "http://aerofoil.test:8465/"
INDEX = {"files": [{"url": "/api/get_game/1#Frost Garden [0100ABCD00010000][v0].nsp", "size": 2000}]}


def encrypt_like_aerofoil(shop: Mapping[str, object], public_pem: bytes) -> bytes:
    """AeroFoil's `encrypt_shop` (app/shop.py), step for step."""
    aes = secrets.token_bytes(16)
    packed = zstandard.ZstdCompressor(level=6).compress(json.dumps(shop).encode())
    size = len(packed)
    session = PKCS1_OAEP.new(RSA.import_key(public_pem), hashAlgo=SHA256, label=b"").encrypt(aes)
    body = AES.new(aes, AES.MODE_ECB).encrypt(packed + b"\x00" * (0x10 - (size % 0x10)))
    return b"TINFOIL" + (0xFD).to_bytes(1, "little") + session + size.to_bytes(8, "little") + body


def served(body: bytes) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body)))


def test_an_index_encrypted_for_the_bridge_key_is_read() -> None:
    private, public = shopkey.generate()
    key = RSA.import_key(private)
    shop = AeroFoil(BASE, client=served(encrypt_like_aerofoil(INDEX, public)), index_key=key)
    assert [listed.size for listed in shop.listing()] == [2000]


def test_an_encrypted_index_without_a_key_says_what_to_do() -> None:
    _, public = shopkey.generate()
    for shop in (
        AeroFoil(BASE, client=served(encrypt_like_aerofoil(INDEX, public))),
        Shop(BASE, client=served(encrypt_like_aerofoil(INDEX, public))),
    ):
        with pytest.raises(UpstreamError, match=r"encrypted.*index_key_file"):
            list(shop.listing())


def test_another_key_does_not_open_it() -> None:
    _, public = shopkey.generate()
    other, _ = shopkey.generate()
    shop = AeroFoil(BASE, client=served(encrypt_like_aerofoil(INDEX, public)), index_key=RSA.import_key(other))
    with pytest.raises(UpstreamError, match="cannot be opened"):
        list(shop.listing())


def test_a_plain_index_is_still_read_when_the_source_has_a_key() -> None:
    private, _ = shopkey.generate()
    shop = AeroFoil(BASE, client=served(json.dumps(INDEX).encode()), index_key=RSA.import_key(private))
    assert len(list(shop.listing())) == 1


@pytest.mark.parametrize("cut", [10, 40, 300])
def test_a_cut_or_damaged_index_is_an_error(cut: int) -> None:
    private, public = shopkey.generate()
    data = encrypt_like_aerofoil(INDEX, public)
    with pytest.raises(UpstreamError):
        shopkey.decrypt(data[:cut], RSA.import_key(private))


def test_a_flag_other_than_zstandard_is_refused() -> None:
    private, public = shopkey.generate()
    data = bytearray(encrypt_like_aerofoil(INDEX, public))
    data[7] = 0xFE
    with pytest.raises(UpstreamError, match="compressed"):
        shopkey.decrypt(bytes(data), RSA.import_key(private))


def test_an_index_that_inflates_past_the_limit_is_refused() -> None:
    private, public = shopkey.generate()
    big = {"files": [], "pad": "a" * (1 << 20)}
    with pytest.raises(UpstreamError, match="larger than"):
        shopkey.decrypt(encrypt_like_aerofoil(big, public), RSA.import_key(private), limit=1 << 16)


def test_the_key_command_writes_a_private_key_and_prints_the_public_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "index.pem"
    assert main(["aerofoil-key", "-o", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "BEGIN PUBLIC KEY" in printed
    assert "BEGIN" not in printed.replace("BEGIN PUBLIC KEY", "")
    if sys.platform != "win32":  # POSIX permissions
        assert out.stat().st_mode & 0o777 == 0o600
    assert shopkey.load(out).has_private()
    # it is not written over
    assert main(["aerofoil-key", "-o", str(out)]) == 1
    assert "not overwritten" in capsys.readouterr().err


def test_a_public_key_is_not_a_private_one(tmp_path: Path) -> None:
    _, public = shopkey.generate()
    path = tmp_path / "public.pem"
    path.write_bytes(public)
    with pytest.raises(ValueError, match="private"):
        shopkey.load(path)


def test_the_config_keeps_the_index_key_file(tmp_path: Path) -> None:
    path = tmp_path / "bridge.toml"
    source = AerofoilSource("af", BASE, index_key_file="/keys/index.pem")
    save(path, Config(uid=new_uid(), sources=[source]))
    (back,) = load(path).sources
    assert back == source
    path.write_text(
        f'uid = "{new_uid()}"\n[[sources]]\nid = "af"\nkind = "aerofoil"\nurl = "{BASE}"\nindex_key_file = ""\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="index_key_file"):
        load(path)
