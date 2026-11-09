# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""An AeroFoil's encrypted index, opened with a key of the bridge's own.

AeroFoil encrypts the index it serves to shop clients for one RSA public key: Tinfoil's by default, whose private
half lives in the console, so only Tinfoil can read it. Given a public key of the bridge's own (``lantern-bridge
aerofoil-key`` makes the pair), AeroFoil encrypts for that one instead, and the bridge reads the index. The
price is that Tinfoil can no longer read that shop: the key is one for the whole AeroFoil.

The format is AeroFoil's (``app/shop.py``): ``TINFOIL``, a flag (0xFD: zstandard), the AES key encrypted with
RSA-OAEP (SHA-256), the length of the data (8 bytes, little endian), then the zstandard data, zero-padded,
encrypted with AES-128 in ECB mode.
That is the format Tinfoil reads, not a choice of the bridge's: it only ever decrypts it (the key is the
bridge's, and the shop is the owner's own).
"""

from __future__ import annotations

import os
from pathlib import Path

import zstandard
from Crypto.Cipher import AES, PKCS1_OAEP
from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA
from Crypto.PublicKey.RSA import RsaKey

from lantern_bridge.sources.tinfoil import MAX_INDEX, UpstreamError

MAGIC = b"TINFOIL"
ZSTD = 0xFD
KEY_BITS = 2048


def is_encrypted(data: bytes) -> bool:
    return data.startswith(MAGIC)


def generate() -> tuple[bytes, bytes]:
    """A new key pair: (the private key, the public key), both PEM."""
    key = RSA.generate(KEY_BITS)
    return key.export_key("PEM"), key.public_key().export_key("PEM")


def write_private(path: Path, pem: bytes) -> None:
    """Writes the private key, readable by its owner only; never over a file that exists."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(pem)


def load(path: str | Path) -> RsaKey:
    """The private key of a PEM file (ValueError if it is not one)."""
    key = RSA.import_key(Path(path).read_bytes())
    if not key.has_private():
        raise ValueError("this is a public key: the bridge needs the private one")
    return key


def decrypt(data: bytes, key: RsaKey, limit: int = MAX_INDEX) -> bytes:
    """The index inside `data`, which is AeroFoil's encrypted index (UpstreamError if the key does not open it)."""
    wrapped = key.size_in_bytes()
    head = len(MAGIC) + 1 + wrapped + 8
    if len(data) < head or not is_encrypted(data):
        raise UpstreamError("the encrypted index is cut short")
    if data[len(MAGIC)] != ZSTD:
        raise UpstreamError("the encrypted index is not compressed the way AeroFoil does")
    session = data[len(MAGIC) + 1 : len(MAGIC) + 1 + wrapped]
    size = int.from_bytes(data[head - 8 : head], "little")
    body = data[head:]
    if size > len(body) or len(body) % 16:
        raise UpstreamError("the encrypted index is malformed")
    try:
        aes = PKCS1_OAEP.new(key, hashAlgo=SHA256, label=b"").decrypt(session)
        packed = AES.new(aes, AES.MODE_ECB).decrypt(body)[:size]
    except (ValueError, TypeError) as error:
        raise UpstreamError(
            "the encrypted index cannot be opened with the index key: is it the one AeroFoil was given?"
        ) from error
    # Streamed and counted: a frame that declares its size would be inflated whole, whatever the limit says.
    try:
        with zstandard.ZstdDecompressor().stream_reader(packed) as reader:
            document = reader.read(limit + 1)
    except zstandard.ZstdError as error:
        raise UpstreamError("the encrypted index is not valid") from error
    if len(document) > limit:
        raise UpstreamError(f"the encrypted index is larger than {limit >> 20} MiB")
    return document
