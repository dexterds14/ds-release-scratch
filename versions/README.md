# Pending version

`versions/portal` holds the pending version of sensor-portal on this branch. That is the version
the next promote on this branch will tag. The file is one line, `X.Y.Z`, no `v`, no suffix, no
`+`. There is one product, `portal`, so there is one file.

`scripts-release/version.py check` checks that the set of files equals the product list in
`release/config.json` and that each file is one `X.Y.Z` line. `pr-checks` runs it on every pull
request. It ignores this README.

## Who writes the file

The release jobs do. Promote bumps the file after it tags a release, and create-branch sets the
opening value of a new line. Both commit as the release bot with `[skip ci]`. Promote bumps the
major number on `main`, the minor on `N.x.x` and the patch on `N.M.x`, unless `bump` says
otherwise.

People edit the file by pull request in one case only: setting the number a line starts from. At
go-live on 2026-11-02, PR #284 set `main` to `4.0.0`, the major after the hand-made `v3.0.0`, so
the first promote on `main` tags `v4.0.0` and the file becomes `5.0.0`. The `3.x.x` line opened at
`main`'s tip with `3.1.0` pending, `3.0.x` opened from `v3.0.0` with `3.0.1` pending, and `v3.0.0`
stays the 3.0.0 release as it is. The pilot the design planned on `0.0.1` never ran.

Merge-up never carries the file across lines. Each line keeps its own value.

## How the build reads the file

`scripts-ci/build-version.sh` turns the file and the git state into the string the binary and the
deb report. It prints `DS_VERSION`, `DS_DEB_VERSION`, `DS_RELEASE_LINE` and `GIT_HASH` for
`eval`. `build.sh`, `build-deb.yml` and `crossbuild/container-build.sh` call it. The same file
lives in ds. The rule below does not apply on an image build. There the sensor-portal recipe in
meta-dsensos stamps `handlers.GitTag` from the nearest reachable `vX.Y.Z` tag and
`handlers.GitBranch` from `SENSOR_PORTAL_BRANCH`, and takes the package version from its `PV`.

The rule, first match wins:

- `DS_BUILD_VERSION` is set: that string, as given. CI passes `build_version` to `build-deb.yml`,
  which sets `DS_BUILD_VERSION` and `DS_RELEASE_LINE` for `build.sh` and nfpm. A release
  candidate gets `X.Y.Z-rc.<N>.github-<sha7>` and a promoted release gets `X.Y.Z`. Locally,
  `DS_BUILD_VERSION=... ./crossbuild/run.sh` passes both into the container.
- HEAD carries an exact `vX.Y.Z` tag and the checkout is clean: `X.Y.Z`. That is a checkout of a
  release, and it reports the release's version without being told. A pinned release commit
  stamps the same version in a local build, in CI and in the OS image.
- Anything else: `X.Y.Z-dev.<N>.g<sha7>`, with `.dirty` appended when the checkout has
  uncommitted changes. The script composes it from this file, `git rev-list --count HEAD` and
  the short sha.

`N` is `git rev-list --count` of the commit, so two RCs of one version order correctly. The only
tag the script reads is an exact `vX.Y.Z` on HEAD. No build derives a version from `git describe`
distance, and CI checks out the full history (`fetch-depth: 0`) so the count and the tag check
work. `build.sh` stamps `handlers.GitTag=v<DS_VERSION>` into the binary, which the sidebar shows
and `-version` prints, and `handlers.GitBranch` with `DS_RELEASE_LINE`, else
`SENSOR_PORTAL_BRANCH`, else `main`. `build-deb.yml` exports `DS_DEB_VERSION` as `NFPM_VERSION`
for `nfpm.yaml`, and `crossbuild/container-build.sh` does the same for a local container build,
so the deb's `Version` field is the dpkg form, `3.1.0~rc.5.github.abc1234` for
`3.1.0-rc.5.github-abc1234`, which sorts before `3.1.0`.
[`../docs/release-lines.md`](../docs/release-lines.md) describes the lines, the jobs and the bump
rules.
