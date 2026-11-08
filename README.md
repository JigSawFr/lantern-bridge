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

The shop is read like any Tinfoil shop (the bridge introduces itself as a Tinfoil client, which AeroFoil needs to
answer its index). An AeroFoil reached over HTTPS that checks its host (Tinfoil's `Hauth`) also needs
`hauth_env = "AEROFOIL_HAUTH"`: the variable holds the value AeroFoil stored from its first administrator
connection (*Settings › Shop*), and the `url` must use the host name AeroFoil is set up with. The *Add a source* form has a field for it too. On top of it, the first `aerofoil` source of the file also gives:

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

Open `http://<host>:8080/admin/`. The first time, the server writes a one-time **setup code** in its log (look for `setup code`); enter it with the name and password you want for the owner. Then sign in to see the hashing progress and the sources, approve or refuse the devices that ask to pair, revoke a device, follow the logs live, and:

- **Library**: every title the bridge knows, as a shelf or a dense list (search, kind: games, updates, DLC and, when an AeroFoil gives some, the titles that have cheats; source, hashing state, sort) and a page per title: its games, updates and DLC, its files by source with their SHA-256, its cheats, what Atlas says of it, and a button to hash one file or the whole title again (`lantern-bridge rehash --title <id>` does the same). In the list, the Name, Version and Size headers sort both ways, and ticking titles opens a bar to hash them again together or to export them (CSV; the page's filters can be exported too, `/admin/library/export?format=csv&q=…`). The pages are paged, so a catalog of tens of thousands of titles stays light. A folder source can be opened to changes with `writable = true` (a checkbox when adding it, a button on the Sources page; off by default, and then the volume can stay read-only): the title page then offers to delete a file from the disk, with a confirmation, and the journal notes it (a count, never a name). Only a plain file of that folder, listed for that title, can be deleted: a link is never followed out of the folder. In the list, the Name, Version and Size headers sort both ways, and ticking titles opens a bar to hash them again together or to export them (CSV; the page's filters can be exported too, `/admin/library/export?format=csv&q=…`). The pages are paged, so a catalog of tens of thousands of titles stays light.
- **Activity**: the transfers in progress, the machines that called the library, the rates and the hashing, live. Simple requests (the container's health check on `/.well-known/lantern`, a browser on `/` or `/favicon.ico`, a scanner's guesses) are counted apart and never make a machine or a rate; a request with a token is always the library's. (The live blocks arrive over one server-sent stream each, `/admin/live`, sent when the content changes; the server looks again at once when something happens (a pass ends, a file is hashed, a journal line is written, a request starts or ends), every second while a transfer or a rate is moving, and every five seconds otherwise; a browser without `EventSource` asks again every two seconds.)
- **Developer** (off unless `developer = true`): the last requests, the slowest hashing, a title as raw JSON, a few actions.
- **Sources**: add, remove, reorder and test a folder or a shop.
- **Settings**: the name, the hashing rate and the access mode, the notifications, the integrations, MQTT and a backup, with a contents bar, a search box, and one Save all bar that appears when cards were edited. The page writes `config.toml` (keeping the keys it does not know); the bridge applies it as soon as it is saved, **with no restart**: it builds a new server from the file, swaps it in (the administrator stays signed in, a download in progress finishes on the old one), and the settings file is watched, so a hand edit is applied within a few seconds too. A file that does not load, or a server that cannot start, leaves the running one as it is and the pages say why. Only the address and port the server listens on (`--host`, `--port`) need a restart.
- **Dots on the menu**: a small count marks where something needs a look (the overview, a source that does not answer, a device waiting to pair, titles to check, a failed conversion; red when broken, amber when worth a look) or is going on (transfers, conversions in progress). The Manage button sums its pages. They update live; the read-only account sees only its own pages' dots.
- **Sessions** (under Manage): who is signed in to the pages, from where (address and browser) and since when, with a button to end one (yours included) or all the others at once; a session ends by itself after a while without use. A sign-in that is blocked after too many wrong passwords is a line of the journal (no address in it), and so is a session ended from the page.
- **Export and import** (in *Settings*): the settings as one TOML file to keep or give to another bridge (no password in it: a source only names the variable that holds it). To import, choose a file or paste its text: the page lists what would change, and nothing is written until you confirm. The bridge keeps its own identity (`uid`) whatever the file says, and the import is a line of the journal like any save.
- **Install and alerts**: the pages carry a manifest and icons, so a browser can install them as an app (from its menu), and a small service worker that keeps nothing (the pages are private and always read from the bridge). In *Settings*, *Alerts on this device* turns on notifications for this browser: when a source stops answering, a device waits to pair or a conversion fails, the page that is open (a tab, or the installed app) shows an alert. A browser that supports Web Push also gets them while no page is open (a source down or back, a newer release, a conversion done): the bridge sends a message encrypted for that browser (RFC 8291) and signed with its own key (VAPID, RFC 8292) through the push service of the browser (Google, Mozilla, Apple or Microsoft), which sees that a message goes by but not what it says; nothing else is reached (a subscription for any other address is refused), and a title or a path never travels. It needs the pages to be served over HTTPS (or from `localhost`), and it is opt-in per browser: turning alerts off there removes its subscription. The key and the subscriptions live in `push.json` in the configuration folder (0600, not in the backup: a copy of them would be a secret to guard); delete the file and every browser has to turn alerts on again.
- **Speed** (in *Settings*): hashing already uses the processor's own SHA-256 instructions when it has them (OpenSSL picks them), and `hash_threads` sets how many files are hashed at once (0: half the cores, one to four; one or two is kinder to a spinning disk or a share). `compress_threads` caps the threads nsz converts with (0: its own choice, every core for a block compression). *Diagnostic* says what the machine offers. There is no graphics-card path: neither SHA-256 nor nsz has one.
- **Homebrew** (under Manage): add the address of a GitHub repository (or `owner/name`) and the bridge serves its latest release as homebrew, like Sphaira does: it reads the repository through GitHub's API with the ETag of the last answer (an unchanged release costs one light read, and a repository is read every six hours), downloads the `.nro` (or the only `.zip`, or the asset a pattern names) once, keeps it by its SHA-256, and serves it again after a restart without reading GitHub. A project can describe itself with a `lantern.json` among the assets of its release (`id`, `name`, `kind`, `summary`, `description`, `author`, `license`, `tags`, `genres`, `language`); without it the bridge takes the name from the repository, the summary, licence and topics from GitHub, and the version from the tag. A zip carries its own manifest of files, and one that writes outside `/switch/<id>/`, `/atmosphere/` or `/switch/.overlays/` is refused with a sentence, as the specification asks. Only public repositories and addresses of GitHub are read, and an asset over 256 MiB is refused. Without a token GitHub allows 60 reads an hour; name an environment variable holding one with `github_token_env` for 5 000.
- **Diagnostic**: the state of the bridge as text to paste in a support request (versions, access, sources and their state, integrations on or off, health, the number of warnings), with no password, token, address or path unless you tick *Include addresses, paths and messages*; even then the webhook and notification addresses (they may carry a secret) and any login in an address are left out. `/admin/diagnostic.json` is the same for a machine.
- **Accounts**: add an account, change its rights, freeze it or delete it, and see its devices and invitations; with an `aerofoil` source, the AeroFoil user it acts as (only the name of the variable holding the password is kept). See *Accounts and rights* below.
- **Invitations**: a code that pairs a device without approval, for 1, 7 or 30 days and a number of devices.
- **Diagnostic**: a JSON file with versions, sources, counts and recent warnings, and no secret.

The pages wear Boréale's night, in the fifteen languages of Boréale (the browser's language, or the menu with flags in the header: English, French and Canadian French, German, Spanish and Latin American Spanish, Italian, Dutch, Portuguese and Brazilian Portuguese, Russian, Japanese, Korean, Simplified and Traditional Chinese; a text a language lacks falls back to its base, then to English), with the fonts served from the bridge. The theme is automatic (it follows the browser's own light or dark preference, and when that changes), with a light and a dark choice one click away in the header (a choice of that browser, kept in a cookie). The header keeps the everyday pages in view and puts the settings ones under "Manage", and both keep to WCAG AA contrast.

The pages answer the local network only (private and loopback addresses). Three settings in `config.toml` fit them to a proxy or a stricter network:

- `admin_allow = ["192.168.1.0/24"]` narrows the allowed addresses to those networks (the machine itself always may).
- `trusted_proxies = ["172.18.0.0/16"]` names the reverse proxies whose `X-Forwarded-For` and `X-Forwarded-Proto` are believed (read from the right, so a client cannot write its own address); the checks above, the sign-in limits and the cookie's `Secure` flag then follow the real client and HTTPS. Without it those headers are ignored.
- `admin_hosts = ["bridge.example.org"]` lists the names the pages answer to (the `Host` header); without it they answer to addresses, one-word names and local ones (`bridge.lan`), which a DNS rebinding page cannot be called. Behind a proxy with a real domain, list it.

Mind that Docker's userland proxy (and Docker Desktop) can show every request as coming from the gateway, a private address: `admin_allow` then protects nothing, and the password is the defence. Publish the port with the default bridge network on Linux, or put a proxy in front and name it in `trusted_proxies`. Wrong passwords are counted per address (5 in 5 minutes) and per name (30 in 15 minutes); a session ends after 30 idle minutes (8 hours at most) and keeps at most 8 live streams. The session cookie is `HttpOnly` and `SameSite=Strict`, and `Secure` over HTTPS. `/metrics`, when the settings turn it on, follows the same rule (the local network only, whatever `admin_lan_only` says). The browser loads nothing from elsewhere.

What an administrator types is trusted: a source's address, the webhook and the notification address are called from the bridge, `localhost` and a cloud's metadata address included (redirects are not followed). Keep the pages on a network you trust. The backup holds the settings (names of environment variables, never their values), the index and the journal, nothing secret.

**Atlas.** By default (`atlas = true`) the bridge fetches one public file once a day, [Atlas](https://atlas.boreale.dev)'s `platforms/switch/games.json` (CC0: names, developers, publishers, genres, series and modes from Wikidata, Steam's rating), from `raw.githubusercontent.com`, keeps it in its state directory (`atlas.json`) and uses it to fill in the genres, developer and publisher a source leaves empty. Nothing about your library is sent: it is a plain `GET`. Set `atlas = false` (or untick it in the settings) and the bridge contacts nothing outside your sources.

**MQTT.** Give the bridge a broker (*Settings › MQTT broker*, or a `[mqtt]` table: `host`, `port`, `user`, `password_env`, `prefix`, `tls`) and it publishes under `<prefix>/<uid>` (`lantern-bridge` unless you change it): `state`, a retained JSON document of `schema` 1 every 30 seconds (status and checks, titles, files hashed and waiting, traffic, each source up or not; no path and no name of a device), `problems` (how many checks are not good), `event` (one JSON line per journal entry as it happens, not retained: no path, no title), and `availability` (`online`, and `offline` as its last will). An order on `order/rescan` reads every source again now. Home Assistant's [Boréale integration](https://github.com/JigSawFr/boreale-ha) reads these topics.

**Webhook.** Set an address in *Settings* (or `webhook` in `config.toml`) and the bridge sends it one POST of JSON when scans found changes, at most every ten seconds: `schema` 1, `event` `scan`, the bridge (id, name, version) and, per source, how many files were `added`, `changed` or `removed`. No path and no title. A call that fails is logged and not repeated. A secret the receiver wants goes in the address's path (no credentials in the address).

**Notifications.** Set an address in *Settings* (or `notify` in `config.toml`) and the bridge sends it a short text when a source stops answering (priority high), when it is back, and when a newer release is out: a POST of plain text with ntfy-style `Title`, `Priority` and `Tags` headers, which ntfy reads as is (`https://ntfy.sh/your-topic`). No path and no title of your library is in it; a call that fails is logged and not repeated.

**Compressing and decompressing.** In a folder open to changes (`writable = true`), a file of a title can be converted from its page (*Convert…*): NSP to NSZ and XCI to XCZ, or back. The bridge does not compress anything itself: it runs the [`nsz`](https://github.com/nicoboss/nsz) tool, one task at a time, which needs your own console keys. It comes with the bridge (a dependency, and so in the image); you only put your `prod.keys` in the configuration folder (the one with `config.toml`); the bridge never reads them, and the page says what is missing. Choose the level (3 to 22, 18 by default) and the mode (solid, the smallest; or block, readable without decompressing). The result is written next to the original, never over a file of the same name, and the original is kept unless you tick *Delete the original afterwards*: it then goes only once the tool finished and verified the contents. The form shows the size the result should have for the level and mode chosen (from the conversions already done here when there are some, else typical figures, which vary a lot with the game). The *Tasks* page (under Manage) and the Activity page show the queue with a real progress bar (the tool's own, read from its machine-readable output), the percent, the time left, the speed and the step, then what was saved; a Cancel button stops one; a task that fails or is cancelled leaves the folder as it was, and a settings reload does not stop it.

**Backup.** *Settings › Backup* downloads a zip, and `lantern-bridge backup -o file.zip` writes one (the bridge may run): the settings (`config.toml`, no secret in it: passwords are read from the environment), the index with the hashes already computed and the activity history. Pairings are not in it, so a device pairs again. To restore, stop the bridge and run `lantern-bridge restore file.zip` (in a container, with the file in the `./config` volume: `docker run --rm -v ./config:/config lantern-bridge restore file.zip`); it refuses an archive of a newer schema, and keeps what it replaces beside it as `.before-restore`.

**Updates.** On by default; *Settings › Updates* (`update_check = false`) turns it off. The bridge reads GitHub's latest release once a day and says on the overview when it is newer (and `update` in the MQTT state). It sends only the request, so GitHub sees the machine's address as for any download, and it downloads and installs nothing.

**Disk space.** The overview shows the room left on the volume of each folder, and the health warns when the volume of the state (the index) has less than `low_disk_mb` free (500 MiB by default, *Settings*; it fails below 20 MiB). A folder full of games is not a fault, so a folder's room is shown and not judged.

**Changing the settings safely.** Each save keeps the file it replaces beside it (`config.toml.previous`, one level, same permissions); *Settings › Undo the last change* brings it back, and pressing it again brings the change back. *Try it first*, under *Add a source*, reads a folder or asks a shop for its listing without saving anything, and a refused form keeps what was typed. *Setup guide* (`/admin/start`) walks an empty bridge through its first source, its first read and its first pairing.

**Journal.** *Journal* lists what happened to the bridge, newest first: it started, a scan found changes (`+added ~changed -removed` for a source), a source stopped answering and came back (the turn, not every pass), a newer release came out, settings were saved or taken back, a device was paired, refused or revoked, a backup was restored (`lantern-bridge restore` writes that line itself). Each line made from the pages carries the operator who did it (shown after the line, with its own column in the downloads and an `actor` key in the MQTT event); the bridge's own lines, a scan or a source going down, have none. A device paired, refused, changed or revoked names its account and its device. It can be downloaded as CSV or JSON (cells that a spreadsheet would run as a formula are quoted). It is kept in `activity.sqlite3` for 90 days (1000 lines at most), holds no path and no title, and is for managers and above. It is in the backup with the rest of that file. Every save of the settings (and every undo) leaves a line that says who made it (the account and its role), from which address, and what changed: a switch or a number as `public: false -> true`, a text (a name, an address, a path) only by its name, and a source that comes or goes by its id, so that no value that may be private reaches the journal.

**Operators and roles.** The first account, made with the setup code, is the owner. *Operators* (the owner's page) adds the others, each with one of four roles, every role able to do what the ones before it can:

| Role | Opens |
|---|---|
| Read-only | the overview, the library (with its checks and exports) and the activity |
| Manager | also the journal, the pairings, the devices, the accounts and the invitations: who may use the bridge and with which consoles |
| Administrator | also the sources, the settings, the tasks, the logs, the diagnostic and the developer page, and the AeroFoil login of an account (it names a secret) |
| Owner | also the operators, the backup, and the export and import of the settings |

A role is checked on the server for every request, not only hidden in the menu, and a page that nothing names needs an administrator. Changing a role, setting a password or removing an operator acts on the sessions already open, at once. Everybody changes their own password from *My account* (a link in the footer), which ends their other sessions; too many wrong guesses of the current password are slowed down like a wrong sign-in. An owner who lost the password runs `lantern-bridge operator reset <name>` on the machine that runs the bridge (it asks for the new password twice, without showing it, or reads one line on the standard input; `lantern-bridge operator list` lists the operators). The last owner cannot be demoted or removed.

**Signing in through a proxy or a provider.** Besides the operators' own passwords, the pages can let in the people of an authentication proxy (Authelia, Authentik, oauth2-proxy…) or of an OpenID Connect provider. Both are set in `config.toml` (the pages do not edit them), and both turn a person's **groups** into a role; someone none of whose groups is listed does not get in (the journal says *Sign-in refused*, with their name):

```toml
[sign_in]
local = true                      # false: no password form (the proxy or the provider must be on)
logout_url = "https://sso.example.org/logout"   # where "Sign out" goes for those signed in from outside

[sign_in.roles]                   # group -> owner | admin | manager | observer; the highest one counts
"lantern-owners" = "owner"
"lantern-family" = "manager"

[sign_in.proxy]                   # the proxy puts the user and the groups in headers
enabled = true
user_header = "Remote-User"       # the defaults
groups_header = "Remote-Groups"   # "a,b,c"

[sign_in.oidc]                    # or, without a proxy, the provider itself (authorization code with PKCE)
enabled = true
issuer = "https://sso.example.org"
client_id = "lantern-bridge"
secret_env = "LANTERN_OIDC_SECRET"   # the variable holding the client secret (none: a public client)
public_url = "https://lantern.example.org"   # this bridge as the browser sees it; the provider's redirect address is <public_url>/admin/login/oidc/callback
groups_claim = "groups"           # and name_claim = "preferred_username", scopes = "openid profile email groups"
```

The proxy's headers are believed **only** from an address of `trusted_proxies` (and the mode refuses to start without one): keep the bridge reachable only through the proxy, and have the proxy remove any `Remote-User` a client sends. They are read again at every request, so a person whose groups change, or who leaves, loses the role at once. With a provider, the role is the one of the sign-in, until the session ends (the owner can end sessions from *Sessions*); groups the identity token does not carry are read from the provider's user information. The identity token comes straight from the provider's token endpoint over TLS and is not checked for a signature (OpenID Connect Core 3.1.3.7); its issuer, audience, expiry and nonce are. A person signed in from outside is not an operator of the bridge: they have no password here, and a name they share with an operator gives them nothing of that operator's. Whatever is set, `lantern-bridge operator reset` stays the way back for an owner who locked themselves out.

**Accounts and consoles from a terminal.** For an installation without a browser, or a script, the same store as the pages (the bridge may run): `lantern-bridge account list|create|freeze|unfreeze|delete` (`create <name> --preset full|child|guest --age 12 --hide-unrated`; `delete <name> --yes` takes the account's consoles, invitations and what is kept for it, and its AeroFoil login, with it) and `lantern-bridge device list|suspend|resume|revoke` (`device list --account <name>`; `suspend|resume|revoke <id>` with the id the list shows). `list` prints a table, or JSON of `schema` 1 with `--json`; neither shows a token or its hash. Each change is a line of the journal whose actor is `(command line)`. Add `--config <file>` when the bridge's files are not in the current folder (in a container: `docker exec <container> lantern-bridge account list`).

**Accounts and rights.** An account is what the devices paired with it act as: none of them goes beyond it. A form sets its rights as a preset, or scope by scope:

| Preset | Scopes |
|---|---|
| Full | library, saves, cheats, requests, sync |
| Child | the same without cheats, and an age limit (12 when the field is left empty) |
| Guest | the library only |
| Custom | the scopes ticked, `cheats.write` and `admin` only for an administrator |

The age limit is a rating cap for the whole account, applied at once to its devices already paired (where a device has a cap of its own, the stricter holds); *Hide what has no rating* hides the unrated. Changing the rights cuts the devices' scopes back with it. *Freeze* keeps the devices paired and answers them as a frozen account until you thaw it. *Delete* removes the account with its devices, invitations, save backups, requests and sync data, and its AeroFoil login. A manager may do all this, but neither give nor change a scope it may not give: an account holding `admin` or `cheats.write` is for an administrator. When a pairing is approved, or an invitation made, for an account that does not exist yet, the form's preset makes it; an account that exists keeps its own rights.

**Consoles.** *Devices* lists the consoles paired with the bridge, and each has a page: its account, its scopes, the age limit in force (the stricter of its own and its account's), when it paired and was last used, the short fingerprint of its key, the address and the program it last called from, and how much it asked for and was sent. From there you can give it a name of your own, take rights away (untick a scope, or give it an age limit of its own: it never gains more than it was paired with), suspend it, or revoke it. A suspended console stays paired and is answered as a frozen account until you lift the suspension, which a revocation does not allow (it has to pair again). When a pairing waits for approval, its code and the fingerprint of the console's key are shown to compare with the ones the console displays.

### 🐳 In a container

Build the image from a checkout (it needs the `sdk/` submodule) and mount the folders read-only: the hashes are then computed in place, which is the recommended way. The configuration and the state (index, pairings) live in `/config`.

```sh
docker build -t lantern-bridge .
docker run -p 8080:8080 -v ./config:/config -v /srv/games:/games:ro lantern-bridge
```

To pair a device: `docker exec <container> lantern-bridge invite`. The image runs as an unprivileged user (uid and gid 10001), so `./config` must be writable by it. For now it is built by the CI, not published.

The bridge writes nowhere but `/config`, so the container can be locked down; the CI runs the image this way on every change:

```yaml
# compose.yaml
services:
  bridge:
    image: lantern-bridge
    ports: [ "8080:8080" ]
    volumes:
      - ./config:/config
      - /srv/games:/games:ro
    read_only: true
    tmpfs: [ /tmp ]
    cap_drop: [ ALL ]
    security_opt: [ "no-new-privileges:true" ]
    restart: unless-stopped
```

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
