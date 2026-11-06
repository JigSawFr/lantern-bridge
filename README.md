<div align="center">

<img src=".github/readme/banner.png" alt="Lantern Bridge" width="100%">

### Your Ownfoil, AeroFoil or Tinfoil library, spoken in Lantern

**Ownfoil · AeroFoil · Tinfoil shops · Plain folders**

[![ci](https://github.com/JigSawFr/lantern-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/JigSawFr/lantern-bridge/actions/workflows/ci.yml)
[![Lantern draft 0.11](https://img.shields.io/badge/Lantern-draft%200.11-blue.svg)](https://lantern.boreale.dev/?utm_source=github&utm_medium=readme)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSES/MIT.txt)

[**Specification**](https://lantern.boreale.dev/?utm_source=github&utm_medium=readme) ·
[**SDK**](https://github.com/JigSawFr/lantern-sdk) ·
[**Features**](#-what-it-will-do) ·
[**Quick&nbsp;start**](#-quick-start) ·
[**Releases**](#-releases)

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

## 🚀 Quick start

Nothing is released yet, so the bridge runs from a checkout (Python 3.12 or later, [uv](https://docs.astral.sh/uv/)). It serves the Switch packages of the folders you list in `config.toml` (created on the first run, with the server's uid), once they are hashed; any Lantern client, or `curl`, can shake hands with it:

```sh
git clone --recurse-submodules https://github.com/JigSawFr/lantern-bridge
cd lantern-bridge && uv sync
uv run lantern-bridge serve --port 8080
curl http://127.0.0.1:8080/.well-known/lantern
```

A folder is a source in `config.toml`, and its files are named `Name [0100ABCD00010000][v0].nsp`:

```toml
[[sources]]
id = "games"
kind = "folder"
path = "/games"
```

`lantern-bridge status` shows the hashing progress, and `lantern-bridge rehash` computes it again.

A device has to be **paired** before it reads anything (only the handshake is open). `lantern-bridge invite` prints a code for a new device (`--user`, `--devices`, `--hours`, `--admin`), which the device enters in its Lantern client. To let anyone who can reach the server read it instead, set `public = true` in `config.toml`; that is not recommended outside a network you trust.

### 🛒 A Tinfoil shop as a source

```toml
[[sources]]
id = "shop"
kind = "tinfoil"
url = "http://nas.lan:8465/"
user = "me"                 # optional; the password is read from this variable, never from the file
password_env = "SHOP_PASSWORD"
```

The bridge reads the shop's JSON index (and the directories it names), then reads each file **once**, in the background and at `remote_rate` megabytes per second (10 by default, `0` for no limit), to compute its SHA-256, and relays the shop's bytes afterwards, ranges included. The first pass therefore moves the whole library through the bridge: **mounting the games read-only in the bridge and using a `folder` source is faster and recommended**. The shop is treated as untrusted: the bridge follows nothing outside its origin, caps what it reads and ignores names that would leave a folder. A file present in a folder and in a shop is one app, served by the folder.

### 🦊 An Ownfoil as a source

```toml
[[sources]]
id = "ownfoil"
kind = "ownfoil"
url = "http://nas.lan:8465/"
user = "me"                 # optional; the password is read from this variable, never from the file
password_env = "OWNFOIL_PASSWORD"
```

The bridge asks Ownfoil's GraphQL API (`/api/graphql`) for what it holds: each file (identified by Ownfoil, not guessed from a name), then the **sheets** (name, summary, description, developer, publisher, release date, genres, languages) and the **images** (icon, banner and screenshots Ownfoil stores itself), which the bridge serves on its own address. An image Ownfoil only points to elsewhere is not fetched, and a file is taken for an image only if its first bytes say so. The files themselves are read once to be hashed and relayed afterwards, like a Tinfoil shop's: **mounting the games read-only and adding a `folder` source is faster**, and the folder then serves the files while Ownfoil keeps giving the sheets.

If the account may read Ownfoil's live feed (`/api/ws`, administrators only), the bridge reads the shop again as soon as a task of Ownfoil ends, instead of waiting for the next regular read (every five minutes). Without it, nothing is lost.

### 🪂 An AeroFoil as a source

```toml
[[sources]]
id = "aero"
kind = "aerofoil"
url = "http://aerofoil:8465/"
user = "bridge"            # the service account
password_env = "AEROFOIL_PASSWORD"

[[sources.logins]]         # optional: Lantern account "alice" acts as this AeroFoil user
account = "alice"
user = "alice"
password_env = "AEROFOIL_ALICE"
```

The shop is read like any Tinfoil shop. On top of it, the first `aerofoil` source of the file also gives:

- **Saves.** The bridge keeps its own copy of each account's backups and mirrors it both ways: a backup made
  through Lantern is uploaded (`account` and `device` kinds, as AeroFoil's save types), and one that only
  AeroFoil has (from CyberFoil, say) is brought over, zip archives only. A backup deleted on one side is
  deleted on the other. The bridge turns encrypted backups off, since AeroFoil keeps plain archives.
- **Cheats.** The cheat sets of AeroFoil appear as Lantern cheat sets. A client with the `cheats.write` scope
  writes through to AeroFoil, whose login must then be an administrator's, or the write is refused.
- **Requests.** A request is also made on AeroFoil, and when AeroFoil closes it (`done`) or denies it
  (`declined`), the account is told.

A Lantern account with no `logins` entry acts as the service account; with neither, nothing is mirrored for it.
The age-rating ceiling is not read from AeroFoil: set it on the Lantern token.

### 🖥️ Administration pages

Open `http://<host>:8080/admin/`. The first time, the server writes a one-time **setup code** in its log (look for `setup code`); enter it with the name and password you want for the administrator. Then sign in to see the hashing progress and the sources, approve or refuse the devices that ask to pair, revoke a device, follow the logs live, and:

- **Settings**: the name, the hashing rate and the access mode; the sources (add, remove, reorder, test a folder or a shop). The page writes `config.toml` (keeping the keys it does not know); the bridge reads it at startup, and the pages say when a restart is due.
- **Accounts**: each account's scopes and devices, and, with an `aerofoil` source, the AeroFoil user it acts as (only the name of the variable holding the password is kept).
- **Invitations**: a code that pairs a device without approval, for 1, 7 or 30 days and a number of devices.
- **Diagnostic**: a JSON file with versions, sources, counts and recent warnings, and no secret.

The pages wear Boréale's night, in English and French (the browser's language, or the switch in the header), with the fonts served from the bridge.

The pages answer the local network only (private and loopback addresses). Behind a reverse proxy, start the server with uvicorn's proxy headers so it sees the real address, or set `admin_lan_only = false` in `config.toml` if the proxy does its own filtering. The session cookie is `HttpOnly` and `SameSite=Strict`, and `Secure` over HTTPS. Nothing is loaded from elsewhere.

### 🐳 In a container

Build the image from a checkout (it needs the `sdk/` submodule) and mount the folders read-only: the hashes are then computed in place, which is the recommended way. The configuration and the state (index, pairings) live in `/config`.

```sh
docker build -t lantern-bridge .
docker run -p 8080:8080 -v ./config:/config -v /srv/games:/games:ro lantern-bridge
```

To pair a device: `docker exec <container> lantern-bridge invite`. The image runs as an unprivileged user (uid 10001), so `./config` must be writable by it. For now it is built by the CI, not published.

## 📦 Releases

Nothing is released yet. A release will be made by this repository's CI from a tag, with release-please, and published on [the releases page](https://github.com/JigSawFr/lantern-bridge/releases).

## 🤝 Contributing

[CONTRIBUTING.md](CONTRIBUTING.md) · [Code of conduct](CODE_OF_CONDUCT.md) · [Security](SECURITY.md)

Issues and pull requests go on this repository. A change to the protocol itself is proposed in [the specification's repository](https://github.com/JigSawFr/lantern). A security flaw is reported privately, as [SECURITY.md](SECURITY.md) says.

## 🧑‍💻 Development

```sh
git clone --recurse-submodules https://github.com/JigSawFr/lantern-bridge   # sdk/ is the Lantern SDK
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run lantern-bridge --version
uv run lantern-bridge serve --port 8080   # then: curl http://127.0.0.1:8080/.well-known/lantern
```

Python 3.12 or later. The Lantern SDK is the `sdk/` submodule, fixed on a commit until it has a release (`git submodule update --init` after a pull). [CONTRIBUTING.md](CONTRIBUTING.md) says how to propose a change; a security flaw is reported privately, as [SECURITY.md](SECURITY.md) says.

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
