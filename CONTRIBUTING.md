# Contributing

The bridge follows the [Lantern specification](https://github.com/JigSawFr/lantern)
through the [Lantern SDK](https://github.com/JigSawFr/lantern-sdk): a change
to the protocol is proposed in the specification's repository first, and a
change to the toolkit in the SDK's.

## Questions and problems

Open an issue, saying the bridge's version, the source concerned (folder,
Tinfoil, Ownfoil, AeroFoil) and its version, and the specification's draft.
A security flaw is not an issue: see [SECURITY.md](SECURITY.md).

## Pull requests

- Titles and commits in [Conventional Commits](https://www.conventionalcommits.org/),
  with the source or area as scope: `feat(folder): …`, `fix(web): …`,
  `ci(python): …`.
- The checks pass before pushing (the README lists them). The CI
  (`.github/workflows/ci.yml`) runs them and sums them up in one check,
  `all green`: the one branch protection requires.
- A change of behaviour comes with its test. A source is tested against a
  fake upstream; a fix for a hostile or broken upstream also teaches the
  fake its case.
- Examples and tests use made-up games and names, and only reserved
  addresses (`example.org`, `192.0.2.0/24`): never a real person, game,
  server or e-mail address.
- The SDK is the `sdk/` submodule, fixed on a commit: moving it is its own
  pull request (`chore(sdk): …`), and a change to the toolkit is made in the
  SDK's repository first.
- Every file says its licence ([REUSE](https://reuse.software/)).

Pull requests are merged as one squashed commit.

## Code of conduct

Everyone taking part follows the [code of conduct](CODE_OF_CONDUCT.md).
