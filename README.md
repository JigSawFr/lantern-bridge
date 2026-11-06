<div align="center">

# Lantern Bridge

### Your Ownfoil, AeroFoil or Tinfoil library, spoken in Lantern

[![ci](https://github.com/JigSawFr/lantern-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/JigSawFr/lantern-bridge/actions/workflows/ci.yml)
[![Lantern draft 0.11](https://img.shields.io/badge/Lantern-draft%200.11-blue.svg)](https://lantern.boreale.dev/?utm_source=github&utm_medium=readme)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSES/MIT.txt)

[**Specification**](https://lantern.boreale.dev/?utm_source=github&utm_medium=readme) ·
[**SDK**](https://github.com/JigSawFr/lantern-sdk) ·
[**Contributing**](CONTRIBUTING.md) ·
[**Security**](SECURITY.md)

</div>

---

A gateway that presents a library which does not speak [Lantern](https://github.com/JigSawFr/lantern) yet (an [Ownfoil](https://github.com/a1ex4/ownfoil) or [AeroFoil](https://github.com/luketanti/aerofoil) server, any Tinfoil shop, or plain folders of games) as a Lantern server, so that Lantern clients can browse it, download from it and sync with it. It runs next to your library, changes nothing in it, and is built on the [Lantern SDK](https://github.com/JigSawFr/lantern-sdk)'s Python server toolkit.

> [!WARNING]
> **Under construction, nothing released yet.** The repository holds its foundations (packaging, checks, governance) and `lantern-bridge serve`, which runs the SDK's server over an empty library. The sources, the web interface and the image come in the following pull requests, and follow the latest draft of the specification (0.11) until 1.0.

## ✨ What it will do

- 🗂️ **Sources** — plain folders, any Tinfoil shop, Ownfoil (its GraphQL API and live changes) and AeroFoil (saves, cheats, requests, content rating), several at once, merged by SHA-256.
- #️⃣ **Hashes** — Lantern asks for the SHA-256 of every file. The recommended way is to mount the games read-only in the bridge, which hashes them on disk and serves them itself; a remote mode, slower and documented as a fallback, hashes what it downloads once.
- 🔐 **Pairing and tokens** — everything the SDK's toolkit does: pairing with a code or a QR code, device-bound tokens, invitations, a rating cap carried by the token.
- 🖥️ **A small web interface** — status of the sources and of the hashing queue, devices to approve, live logs, configuration.
- 🛡️ **Distrustful of its upstream** — it follows no URL outside the origin it was configured with, and caps what it reads.
- 🧪 **Tested against the specification's conformance suite**, over fake upstreams, in CI.

## 🧑‍💻 Development

```sh
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run lantern-bridge --version
uv run lantern-bridge serve --port 8080   # then: curl http://127.0.0.1:8080/.well-known/lantern
```

Python 3.12 or later. The SDK is taken from its repository, on a commit, until it has a release. [CONTRIBUTING.md](CONTRIBUTING.md) says how to propose a change; a security flaw is reported privately, as [SECURITY.md](SECURITY.md) says.

## 🧭 How it's made

[![Human-led, AI-assisted](https://raw.githubusercontent.com/JigSawFr/boreale/main/branding/badge-ai.svg)](https://boreale.dev/how/?utm_source=github&utm_medium=readme)

Lantern Bridge is built by [JigSawFr](https://github.com/JigSawFr), a full-stack
engineer who has shipped production software for years, with AI coding
assistants as part of the toolbox.

- **I decide**: architecture, scope, what ships and what doesn't.
- **AI helps write**: first drafts of code and tests, docs, and the translations.
- **I review everything**: every change is read, tested and signed before it
  reaches `main`.

Nothing in the bridge calls an AI service. More on [boreale.dev](https://boreale.dev/how/?utm_source=github&utm_medium=readme).

## 📜 License and credits

**MIT**, see [`LICENSES/MIT.txt`](LICENSES/MIT.txt); every file says its licence ([REUSE](https://reuse.software/)). Maintained by [JigSawFr](https://github.com/JigSawFr), part of [Boréale](https://boreale.dev/?utm_source=github&utm_medium=readme).
