# Security

The bridge is a network service. It holds the credentials of the libraries
it reads, reads files from disk, serves them to devices on a network, and
reads the answers of upstream servers it does not control. A flaw in it
matters. Thank you for reporting one.

## Reporting a flaw

Report it privately, through GitHub's private vulnerability reporting:
the **Security** tab of this repository, then **Report a vulnerability**.
Please don't open a public issue or pull request for it.

Say which version and which source, what you found, how to reproduce it,
and what an attacker gains.

## What counts

- A credential of an upstream library leaking: into a log, a response, the
  web interface, a diagnostic export or a file readable by others.
- The bridge reading or serving a file outside the folders it was given, or
  fetching a URL outside the origin it was configured with, because an
  upstream said so (path traversal, SSRF, a redirect to another host).
- Reaching the web interface, the administration API or a device's data
  without being signed in or paired.
- A hostile or broken upstream making the bridge crash, hang, or use
  unbounded memory, disk or bandwidth.
- A served file whose bytes do not match the SHA-256 it was listed with.

## Known, accepted code-scanning alerts

- **AES in ECB mode** (CodeQL `py/weak-cryptographic-algorithm`, in `src/lantern_bridge/sources/shopkey.py`, and
  in the test helper that builds an encrypted index). AeroFoil encrypts its index the way Tinfoil reads it:
  AES-128 in ECB mode under a key wrapped with RSA-OAEP. The bridge only ever **decrypts** that document, with a
  private key of its own, to read a library its owner runs; it chooses neither the mode nor protects a secret
  with it. A different mode would make the index unreadable, so the alerts are dismissed as "used in tests" /
  "won't fix", with this note as the reason. The option is off unless a source names an `index_key_file`.

## What happens next

- The report is acknowledged, then confirmed or answered with why it is
  not a flaw.
- A confirmed flaw is fixed with a test, and the hostile fake upstream is
  taught its case so it cannot come back.
- The advisory is published with the fixed release, crediting you if you
  wish.

## The protocol and the SDK

A flaw in the protocol itself belongs to
[the specification's repository](https://github.com/JigSawFr/lantern/security),
one in the toolkit the bridge is built on to
[the SDK's](https://github.com/JigSawFr/lantern-sdk/security).
