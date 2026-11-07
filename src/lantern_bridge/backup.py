# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""A backup of the settings and of what the bridge worked out, and its restoration.

The archive is a zip with `manifest.json` (`schema` 1: the bridge's version, the time, the id), `config.toml`, and
the two databases, copied through SQLite's own backup so they are consistent while the bridge runs:
`index.sqlite3` (the hashes: hours of work on a large library) and `activity.sqlite3` (the days and the devices
seen). Pairings and accounts are not in it: a device pairs again, and a copy of tokens is a secret to guard.

A restoration is done with the bridge stopped (`lantern-bridge restore FILE`): it refuses an archive of a newer
schema or with a name it does not know, and keeps what it replaces beside it as `<name>.before-restore`.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import tempfile
import time
import zipfile
from pathlib import Path

from lantern_bridge import __version__
from lantern_bridge.config import ConfigError, load

SCHEMA = 1
DATABASES = ("index.sqlite3", "activity.sqlite3")
MANIFEST = "manifest.json"
CONFIG = "config.toml"
#: What an archive may hold, nothing else is written to disk.
NAMES = frozenset({MANIFEST, CONFIG, *DATABASES})
#: The most an archive may unpack to (a sanity limit against a bad file, not a limit of the bridge).
MAX_BYTES = 64 << 30


class BackupError(Exception):
    """The archive cannot be used, said in a sentence."""


def _copy_database(source: Path, dest: Path) -> None:
    # `with connection` only ends a transaction: the connections are closed here, or Windows keeps the files locked.
    with (
        contextlib.closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True)) as src,
        contextlib.closing(sqlite3.connect(dest)) as out,
    ):
        src.backup(out)


def make(state_dir: Path, config_path: Path, dest: Path, now: float | None = None) -> list[str]:
    """Write the archive to `dest`; returns the names it holds."""
    config = load(config_path)
    names = [MANIFEST, CONFIG]
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as archive, tempfile.TemporaryDirectory() as scratch:
        manifest = {
            "schema": SCHEMA,
            "bridge": {"uid": config.uid, "version": __version__},
            "created": int(time.time() if now is None else now),
        }
        archive.writestr(MANIFEST, json.dumps(manifest, indent=2))
        archive.write(config_path, CONFIG)
        for name in DATABASES:
            source = state_dir / name
            if source.exists():
                copy = Path(scratch) / name
                _copy_database(source, copy)
                archive.write(copy, name)
                names.append(name)
    return names


def restore(archive_path: Path, state_dir: Path, config_path: Path) -> list[str]:
    """Put the archive's files in place; returns the names restored. Nothing is written if the archive is refused."""
    try:
        archive = zipfile.ZipFile(archive_path)
    except (zipfile.BadZipFile, OSError) as error:
        raise BackupError(f"not a backup: {error}") from error
    with archive:
        infos = {i.filename: i for i in archive.infolist()}
        if MANIFEST not in infos or CONFIG not in infos:
            raise BackupError("not a lantern-bridge backup: no manifest or no settings")
        if unknown := sorted(set(infos) - NAMES):
            raise BackupError(f"the archive holds files this version does not restore: {', '.join(unknown)}")
        if sum(i.file_size for i in infos.values()) > MAX_BYTES:
            raise BackupError("the archive is too large to be a backup")
        try:
            manifest = json.loads(archive.read(MANIFEST))
        except ValueError as error:
            raise BackupError("the manifest is not readable") from error
        schema = manifest.get("schema") if isinstance(manifest, dict) else None
        if not isinstance(schema, int) or isinstance(schema, bool) or schema < 1:
            raise BackupError("the manifest has no schema")
        if schema > SCHEMA:
            raise BackupError(
                f"the backup has schema {schema}, this version reads up to {SCHEMA}: update lantern-bridge"
            )
        # The settings must load before anything is replaced.
        with tempfile.TemporaryDirectory() as scratch:
            probe = Path(scratch) / CONFIG
            probe.write_bytes(archive.read(CONFIG))
            try:
                load(probe)
            except ConfigError as error:
                raise BackupError(f"the settings in the backup are not valid: {error}") from error
        done: list[str] = []
        for name in (CONFIG, *DATABASES):
            if name not in infos:
                continue
            target = config_path if name == CONFIG else state_dir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            staging = target.with_name(f".{target.name}.restoring")
            staging.write_bytes(archive.read(name))
            if target.exists():
                os.replace(target, target.with_name(f"{target.name}.before-restore"))
            os.replace(staging, target)
            for suffix in ("-wal", "-shm"):  # a leftover journal of the old database would be replayed on the new
                target.with_name(f"{target.name}{suffix}").unlink(missing_ok=True)
            done.append(name)
    return done
