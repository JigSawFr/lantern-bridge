# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""Logs: a ring in memory for the administration pages, JSON lines for the container's output."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
_ROOT = "lantern_bridge"


@dataclass(frozen=True, slots=True)
class Line:
    id: int
    time: float
    level: str
    logger: str
    message: str

    def to_json(self) -> dict[str, object]:
        return {"id": self.id, "time": self.time, "level": self.level, "logger": self.logger, "message": self.message}


class LogRing(logging.Handler):
    """The last `size` lines, numbered, so a page can ask for what came after the last one it holds."""

    def __init__(self, size: int = 2000) -> None:
        super().__init__(logging.INFO)
        self._lines: deque[Line] = deque(maxlen=size)
        self._next = 1
        self._guard = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except (TypeError, ValueError):  # a bad format string is logged as it is, never raised
            message = str(record.msg)
        if record.exc_info:
            message += "\n" + logging.Formatter().formatException(record.exc_info)
        with self._guard:
            self._lines.append(Line(self._next, record.created, record.levelname, record.name, message))
            self._next += 1

    def since(self, last: int = 0, level: str = "INFO", source: str | None = None, limit: int = 500) -> list[Line]:
        """The lines after `last` at `level` or above; when `source` is given, only those that mention it."""
        floor = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
        with self._guard:
            lines = [line for line in self._lines if line.id > last]
        out = [
            line
            for line in lines
            if logging.getLevelNamesMapping().get(line.level, 0) >= floor and (source is None or source in line.message)
        ]
        return out[-limit:]


class JsonFormatter(logging.Formatter):
    """One JSON object per line: what Docker and log collectors read."""

    def format(self, record: logging.LogRecord) -> str:
        doc: dict[str, object] = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            doc["exception"] = self.formatException(record.exc_info)
        return json.dumps(doc, ensure_ascii=False)


def install(ring: LogRing, *, json_lines: bool = False) -> None:
    """Send the bridge's logs to `ring`, and to standard error (as JSON lines when asked)."""
    logger = logging.getLogger(_ROOT)
    logger.setLevel(logging.INFO)
    if ring not in logger.handlers:
        logger.addHandler(ring)
    if not any(getattr(h, "_bridge_stream", False) for h in logger.handlers):
        stream = logging.StreamHandler()
        stream._bridge_stream = True  # type: ignore[attr-defined]
        stream.setFormatter(
            JsonFormatter() if json_lines else logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        )
        logger.addHandler(stream)
