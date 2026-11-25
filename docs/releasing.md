# Releasing Orcaster

One release of Orcaster is one GitHub Release for one tag `vX.Y.Z`. It carries
every component, and each asset name says which component it belongs to. The
release notes are generated from `ops/release/release-body.md` (one section per
component: Server, Worker, CLI) followed by the `## [X.Y.Z]` section of
`CHANGELOG.md`.

## What a release contains

For a version `X.Y.Z`:

| Asset | Component | What it is | Who consumes it |
|---|---|---|---|
| `orcaster-server-X.Y.Z.tar.gz` | Server | The repository tree with the `orcaster/` prefix and a `RELEASE` manifest | The Debian 13 installer (`setup-scripts/debian13`) |
| `orcaster-worker-X.Y.Z.tar.gz` | Worker | The same bytes as the server bundle | `ops/release/install-release.sh` on a worker host |
| `orcaster_worker-X.Y.Z-py3-none-any.whl` | Worker | Python wheel of the worker (the pip name is `orcaster-worker`; PEP 427 file names use an underscore) | `pip install` |
| `orcaster-cli-X.Y.Z-<os>-<arch>.tar.gz` (`.zip` for Windows) | CLI | The `orcaster` binary with `LICENSE`, `NOTICE`, `THIRD_PARTY` and `README.md` | Operators and scripts |
| `orcaster-cli-X.Y.Z-<os>-<arch>.<ext>.sbom.json` | CLI | SPDX SBOM of each CLI archive | Supply-chain review |
| `orcaster-X.Y.Z-checksums.txt` | All | SHA-256 of every other asset: CLI archives, SBOMs, server and worker files | Download verification |

CLI platforms: linux, darwin and windows on amd64 and arm64; freebsd on amd64.

## Versioning

Server, worker and CLI share one version, `vX.Y.Z`.

- Set `version` in `server/pyproject.toml` and in `client/pyproject.toml` to
  `X.Y.Z`. `ops/release/build-components.sh` fails unless both equal the tag.
- `CHANGELOG.md` must have a section `## [X.Y.Z] - YYYY-MM-DD`; the release
  notes and `release-body.sh` fail without it.
- A release has a second tag at the same commit, `orcaster/vX.Y.Z`. The Go
  proxy versions a module that lives in a subdirectory only by tags prefixed
  with that directory, so `go install github.com/AInvirion/orcaster/orcaster@vX.Y.Z`
  needs it. `ops/release/tag-release.sh` creates and pushes both tags, and the
  workflow fails when the module tag is missing or points elsewhere. GoReleaser
  ignores the module tag (`git.ignore_tags`), so it never becomes the version
  of an asset.
- Because of that second tag, ask git for the release version with a pattern:
  `git describe --tags --match 'v*'`.

## Dry run

Build every asset and the release notes locally, without a tag and without
publishing:

```bash
make release-dry-run                       # version of client/pyproject.toml
make release-dry-run VERSION=vX.Y.Z
ops/release/dry-run.sh [vX.Y.Z] [--out DIR]
```

It needs `goreleaser`, `uv` and git >= 2.37. `syft` is optional: without it the
SBOMs are skipped with a warning (the workflow always builds them). The script
runs the same steps as the workflow, in this order: `build-components.sh`,
`release-body.sh`, `goreleaser release --snapshot` and `check-archives.sh`. At
the end it prints the release title, the asset list and the path of the notes.

`make snapshot-orcaster` builds only the CLI archives.

## Cutting a release

1. Set `version` to `X.Y.Z` in `server/pyproject.toml` and
   `client/pyproject.toml`.
2. In `CHANGELOG.md`, rename `## [Unreleased]` content into a section
   `## [X.Y.Z] - YYYY-MM-DD` and leave an empty `## [Unreleased]` above it.
3. Merge the change.
4. Run the dry run on the merge commit and read the notes it wrote.
5. Tag the merge commit and push both tags:

   ```bash
   ops/release/tag-release.sh vX.Y.Z --push
   ```

6. The workflow `.github/workflows/release.yml` does the rest. It runs only in
   `AInvirion/orcaster`; in a fork that carries the same tags the job is
   skipped. Its steps:
   1. check that `orcaster/vX.Y.Z` exists at the commit of the tag;
   2. check the licences of the Go module graph against the allowlist and the
      `THIRD_PARTY` notice;
   3. build the server and worker assets (`build-components.sh`);
   4. build the release notes (`release-body.sh`);
   5. build a CLI snapshot with GoReleaser and SBOMs with syft;
   6. check the assets (`check-archives.sh`);
   7. publish the release with GoReleaser, titled "Orcaster vX.Y.Z: server,
      worker and CLI";
   8. write a GitHub artifact attestation for every asset (skipped while the
      repository is private).

## What the workflow checks

Nothing is published unless all of these hold:

- the tag, `server/pyproject.toml` and `client/pyproject.toml` carry the same
  version;
- the module tag `orcaster/vX.Y.Z` points at the same commit;
- every Go module linked in the CLI has an allowed licence and an entry in
  `THIRD_PARTY`;
- the `## [X.Y.Z]` section of `CHANGELOG.md` exists and is not empty;
- every CLI archive is named `orcaster-cli-X.Y.Z-<os>-<arch>` and holds
  `LICENSE`, `NOTICE`, `THIRD_PARTY`, `README.md` and the binary at its root;
- the component directory holds exactly the server bundle, the worker bundle
  and the worker wheel;
- `orcaster-X.Y.Z-checksums.txt` lists every archive and component file with
  its SHA-256.

## Verifying downloads

Download the assets you need and `orcaster-X.Y.Z-checksums.txt` into one
directory, then:

```bash
sha256sum --ignore-missing -c orcaster-X.Y.Z-checksums.txt
```

On macOS use `shasum -a 256 -c` after filtering the file to the assets you
downloaded. Build provenance of any asset:

```bash
gh attestation verify <file> -R AInvirion/orcaster
```

## Installing each component

- Server: extract `orcaster-server-X.Y.Z.tar.gz` and follow
  [setup-scripts/debian13/README.md](../setup-scripts/debian13/README.md).
- Worker: install `orcaster-worker-X.Y.Z.tar.gz` with
  `ops/release/install-release.sh` and follow
  [setup-scripts/debian13-worker/README.md](../setup-scripts/debian13-worker/README.md).
- CLI: unpack the archive for your platform and put `orcaster` on your `PATH`;
  see the CLI section of the [README](../README.md#7-run-a-first-task).

The install commands for each component are also in the notes of every release.
