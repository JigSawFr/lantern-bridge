# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""The figures of the bridge in the Prometheus text format (https://prometheus.io/docs/instrumenting/exposition_formats/).

Only counters and gauges the pages already have: files hashed and waiting, sources up, requests and bytes
served, transfers in progress, health. No path, no address, no device name: a label is a source id or a
fixed word.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from lantern_bridge import __version__, health

if TYPE_CHECKING:
    from lantern_bridge.runtime import Runtime


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render(runtime: Runtime) -> str:
    lines: list[str] = []

    def metric(name: str, kind: str, text: str, *samples: tuple[str, float]) -> None:
        lines.append(f"# HELP lantern_bridge_{name} {text}")
        lines.append(f"# TYPE lantern_bridge_{name} {kind}")
        lines.extend(f"lantern_bridge_{name}{labels} {value:g}" for labels, value in samples)

    index, stats, traffic = runtime.index, runtime.stats, runtime.traffic
    counts, by_source = index.counts(), index.counts_by_source()
    apps, titles = runtime.projection.served()
    metric("info", "gauge", "The version of the bridge.", (f'{{version="{_label(__version__)}"}}', 1))
    metric("uptime_seconds", "gauge", "Seconds since the process started.", ("", time.time() - stats.started))
    metric(
        "files",
        "gauge",
        "Files of the index by state.",
        ('{state="hashed"}', counts["hashed"]),
        ('{state="pending"}', counts["pending"]),
    )
    metric("pending_bytes", "gauge", "Bytes still to hash.", ("", index.pending_bytes()))
    metric("apps_served", "gauge", "Apps the library offers.", ("", apps))
    metric("titles_served", "gauge", "Titles the library offers.", ("", titles))
    metric("hashed_bytes_total", "counter", "Bytes hashed since the start.", ("", stats.hashed_bytes))
    metric("hashing_seconds_total", "counter", "Seconds spent hashing since the start.", ("", stats.hashing_seconds))
    sources = runtime.config.sources
    up = [
        (f'{{source="{_label(s.id)}"}}', 1.0 if (stats.sources.get(s.id) or _NONE).reachable else 0.0) for s in sources
    ]
    metric("source_up", "gauge", "1 when the last read of the source worked.", *up)
    metric(
        "source_files",
        "gauge",
        "Files of a source by state.",
        *[
            (f'{{source="{_label(s.id)}",state="{state}"}}', by_source.get(s.id, {}).get(state, 0))
            for s in sources
            for state in ("hashed", "pending")
        ],
    )
    metric("requests_total", "counter", "Library requests answered since the start.", ("", traffic.totals.requests))
    metric(
        "simple_requests_total",
        "counter",
        "Health checks, browsers and scanners since the start (not in requests_total).",
        ("", traffic.totals.simple),
    )
    metric("sent_bytes_total", "counter", "Bytes sent since the start.", ("", traffic.totals.sent))
    metric("errors_total", "counter", "Answers of 5xx since the start.", ("", traffic.totals.errors))
    metric("refused_total", "counter", "Answers of 401 or 403 since the start.", ("", traffic.totals.refused))
    metric("transfers_active", "gauge", "Responses being sent now.", ("", len(traffic.active)))
    status = health.overall(health.run(runtime))
    metric(
        "health",
        "gauge",
        "1 for the current status (ok, warn, fail).",
        *[(f'{{status="{name}"}}', 1.0 if name == status else 0.0) for name in (health.OK, health.WARN, health.FAIL)],
    )
    return "\n".join(lines) + "\n"


class _Unknown:
    reachable: bool | None = None


_NONE = _Unknown()
