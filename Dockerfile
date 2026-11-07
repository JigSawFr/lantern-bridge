# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT

# The SDK is a git submodule (`sdk/`): check out the repository with
# `git clone --recurse-submodules` before building.

# The base images are pinned by digest; Dependabot moves them (tag and digest).
FROM python:3.13-alpine@sha256:1e1f6334529d1fb26a2ef619d7cffb30a7c7d7cc1cc7338ebee283a58d1aeaca AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 /uv /usr/local/bin/uv
# The environment is built where it will run: its scripts name their interpreter by path
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src
# The dependencies first: a change to the code does not install them again
COPY pyproject.toml uv.lock ./
COPY sdk/python ./sdk/python
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --no-install-project
COPY README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable \
    && rm -f /opt/venv/bin/activate* /opt/venv/bin/*.bat /opt/venv/bin/*.ps1

FROM python:3.13-alpine@sha256:1e1f6334529d1fb26a2ef619d7cffb30a7c7d7cc1cc7338ebee283a58d1aeaca
# The run needs neither pip nor the GUI and teaching modules
RUN adduser -S -u 10001 -h /config -H bridge \
    && mkdir /config && chown bridge /config \
    && rm -rf /usr/local/bin/pip* /usr/local/lib/python3.13/ensurepip \
        /usr/local/lib/python3.13/site-packages/pip* /usr/local/lib/python3.13/idlelib \
        /usr/local/lib/python3.13/tkinter /usr/local/lib/python3.13/turtledemo
COPY --from=build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    LANTERN_BRIDGE_CONFIG=/config/config.toml
USER bridge
WORKDIR /config
VOLUME /config
EXPOSE 8080
# busybox's wget: no second Python interpreter every 30 seconds
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD ["wget", "-q", "-O", "/dev/null", "http://127.0.0.1:8080/.well-known/lantern"]
ENTRYPOINT ["lantern-bridge"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8080", "--json-logs"]
