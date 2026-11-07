# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT

# The SDK is a git submodule (`sdk/`): check out the repository with
# `git clone --recurse-submodules` before building.

FROM python:3.13-slim@sha256:09a17cad02668ae61ece136b4fd36ac3849e14f670ebedb637ea3e15580525bc AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 /uv /usr/local/bin/uv
# The environment is built where it will run: its scripts name their interpreter by path
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src
COPY pyproject.toml uv.lock README.md ./
COPY sdk/python ./sdk/python
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.13-slim@sha256:09a17cad02668ae61ece136b4fd36ac3849e14f670ebedb637ea3e15580525bc
RUN useradd --system --uid 10001 --no-create-home --home-dir /config bridge \
    && mkdir /config && chown bridge /config
# pip is not needed to run the bridge, and its vendored libraries (urllib3, msgpack...) only add advisories
RUN python -m pip uninstall --yes pip \
    && rm -f /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.13
# zlib: Debian's fix (1.3.1-1+deb13u1) is not in the base image yet; remove this
# line when a bump of its digest (Dependabot) brings it
RUN apt-get update \
    && apt-get install --yes --no-install-recommends --only-upgrade zlib1g \
    && rm -rf /var/lib/apt/lists/*
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
CMD ["serve", "--host", "0.0.0.0", "--port", "8080", "--json-logs"]
