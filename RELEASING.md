<!--
SPDX-FileCopyrightText: 2026 JigSawFr
SPDX-License-Identifier: MIT
-->

# Releasing the bridge

A release is made of the Python package (sdist and wheel), its SBOM, and the
container image for `linux/amd64` and `linux/arm64`. Every file is built from
the tag, attested, listed in `SHA256SUMS` and attached to the GitHub release.

## How a release happens

1. `release-please.yml` keeps a `chore(main): release X.Y.Z` pull request up to
   date from the Conventional Commits (`CHANGELOG.md`, `src/lantern_bridge/_version.py`).
2. **Merging it is the decision to release** and belongs to the maintainer.
   It creates the `vX.Y.Z` tag and the GitHub release.
3. `release.yml` starts on the published release:
   - `python`: `tools/release_build.sh` builds the sdist, the wheel and the
     CycloneDX SBOM of the locked environment, with the tools pinned by hash in
     `tools/release-requirements.txt`;
   - `image`: builds and pushes `ghcr.io/jigsawfr/lantern-bridge:X.Y.Z`, `:X.Y`
     and `:latest`, and attests the image digest;
   - `attest`: SLSA build provenance of every file (Sigstore, through GitHub's
     attestations), the SBOM linked to the package, `SHA256SUMS`, and the files
     attached to the release;
   - `pypi`: trusted publishing (no token), only when the repository variable
     `PUBLISH_TO_PYPI` is `true`.

`release-check.yml` (part of `ci.yml`) builds the Python files twice, in two
folders, and fails if a byte differs; the SBOM is validated against the
CycloneDX schema. A release cannot surprise the CI.

## Verifying a release

```sh
gh attestation verify lantern_bridge-X.Y.Z-py3-none-any.whl -R JigSawFr/lantern-bridge
gh attestation verify lantern_bridge-X.Y.Z-py3-none-any.whl -R JigSawFr/lantern-bridge \
    --predicate-type https://cyclonedx.org/bom
gh attestation verify oci://ghcr.io/jigsawfr/lantern-bridge:X.Y.Z -R JigSawFr/lantern-bridge
tools/reproduce.sh release vX.Y.Z      # builds the tag again, compares with SHA256SUMS
```

`reproduce.sh` needs the tools of `tools/release-requirements.txt` in
`$PYTHON` and `uv`; `build-info.txt` in the release names the versions used.

## Set up once (the maintainer)

- The release GitHub App: variables `RELEASE_APP_ID`, `RELEASE_APP_CLIENT_ID`
  and secret `RELEASE_APP_PRIVATE_KEY` (see `release-please.yml`).
- PyPI: a trusted publisher for `lantern-bridge`: owner `JigSawFr`, repository
  `lantern-bridge`, workflow `release.yml`, environment `pypi`; then the
  variable `PUBLISH_TO_PYPI=true`. The package requires `lantern-protocol`,
  which has to be on PyPI first: until then the release has no PyPI step, and
  the image is the way to run the bridge.
- GHCR: the package `lantern-bridge` is private the first time; make it
  public in the package's settings.

## Refreshing the tools

`tools/release-requirements.in` lists them; the `.txt` is generated (see the
command in the `.in` file) and must be regenerated after any change.
