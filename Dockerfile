# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT

# The SDK is a git submodule (`sdk/`): check out the repository with
# `git clone --recurse-submodules` before building.

FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv
# The environment is built where it will run: its scripts name their interpreter by path
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src
COPY pyproject.toml uv.lock README.md ./
COPY sdk/python ./sdk/python
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.13-slim
RUN useradd --system --uid 10001 --no-create-home --home-dir /config bridge \
    && mkdir /config && chown bridge /config
COPY --from=build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    LANTERN_BRIDGE_CONFIG=/config/config.toml
USER bridge
WORKDIR /config
VOLUME /config
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/.well-known/lantern', timeout=4)"]
ENTRYPOINT ["lantern-bridge"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8080"]
