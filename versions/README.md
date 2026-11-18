# Pending versions

Each file in this directory, `versions/<product>`, holds the pending version of one product on
this branch. That is the version the next promote on this branch will tag. The file is one line,
`X.Y.Z`, no `v`, no suffix, no `+`. Every product in `release/config.json` has a file here. On a
product line only `versions/<that product>` is live. The other products' files ride along
unchanged.

`./run-kas lint` checks that the set of files equals the product list and that each file is one
`X.Y.Z` line. `scripts-release/version.py check` runs the same check on its own. Both ignore this
README.

## Who writes the files

The release jobs do. Promote bumps the file after it tags a release, and create-branch sets the
opening value of a new line. Both commit as the release bot with `[skip ci]`.

People edit a file by pull request in one case only: seeding a new product, where the file lands
next to its config and matrix entries.

Both products were seeded at `1.0.0` at the ENG-1968 cut-over on 2026-10-27. The first RC on each
product main is `1.0.0-rc.<N>.github-<sha7>` and the first promote tags `<product>/v1.0.0`. The
pilot before that ran vertex at `0.8.0` and aggregator at `0.0.1`, below `1.0.0` so that the tags,
RCs and releases burned while the pipeline was being proven never collide with real ones. Those
test releases stay as history.

## How the layer reads the file

The product fragment in `kas/include/product/<product>.yml` sets `DS_PRODUCT`, and meta-dsensos
reads `versions/${DS_PRODUCT}` as the base of the version the image reports.

- An ordinary build, local or CI, gets `X.Y.Z-dev.<N>.g<sha7>`, with `.dirty` appended when the
  checkout has uncommitted changes. meta-dsensos composes it from the file and the git state.
- A release candidate gets `X.Y.Z-rc.<N>.github-<sha7>`. CI passes it to `yocto-build.yml` as
  `build_version`.
- A promoted release gets `X.Y.Z`. CI passes that the same way.

`N` is `git rev-list --count` of the commit, so two RCs of one version order correctly for
`ds-ota`. [`../docs/release-lines.md`](../docs/release-lines.md) describes the lines, the jobs and
the bump rules.
