Orcaster @VERSION@ is one release of three components that share the version
@VERSION@. Each asset name says which component it belongs to. Verify any
download against `orcaster-@VERSION@-checksums.txt`, or with
`gh attestation verify <file> -R AInvirion/orcaster`.

## Server

The FastAPI server: task queue in PostgreSQL, task signing, web interface, JSON
API and the worker endpoint.

| Asset | What it is |
|---|---|
| `orcaster-server-@VERSION@.tar.gz` | Source bundle (`orcaster/` tree with a `RELEASE` manifest) that the Debian 13 installer runs from |

Install on Debian 13, as root:

```bash
sha256sum --ignore-missing -c orcaster-@VERSION@-checksums.txt
tar -xzf orcaster-server-@VERSION@.tar.gz -C /root
chown -R root:root /root/orcaster && chmod -R go-w /root/orcaster
cd /root/orcaster/setup-scripts/debian13
install -m 600 -o root -g root setup.env.example setup.env
$EDITOR setup.env        # set DOMAIN and ADMIN_EMAIL; leave DB_PASSWORD empty
sudo bash run-all.sh
```

Upgrade: from a checkout of the repository at the tag, run
`setup-scripts/debian13/deploy.sh --ref v@VERSION@`, or extract the bundle and
run `setup-scripts/debian13/deploy.sh --source <extracted orcaster/ directory>`.
See `setup-scripts/debian13/README.md`.

## Worker

The Python worker that runs tasks in a rootless Podman container.

| Asset | What it is |
|---|---|
| `orcaster-worker-@VERSION@.tar.gz` | Deploy bundle that `ops/release/install-release.sh` installs on a worker host (same tree as the server bundle) |
| `orcaster_worker-@VERSION@-py3-none-any.whl` | Python wheel of the worker (`pip install` name: `orcaster-worker`) |

Install or upgrade a worker host, as root:

```bash
grep ' orcaster-worker-@VERSION@.tar.gz$' orcaster-@VERSION@-checksums.txt > orcaster-worker-@VERSION@.tar.gz.sha256
tar -xzf orcaster-worker-@VERSION@.tar.gz orcaster/ops/release/install-release.sh
sudo bash orcaster/ops/release/install-release.sh --tarball orcaster-worker-@VERSION@.tar.gz
```

On a new host add `--no-restart` and continue with
`setup-scripts/debian13-worker/README.md`. The worker image is built on the
host from the installed tree.

## CLI

`orcaster`, the command-line client.

| Asset | What it is |
|---|---|
| `orcaster-cli-@VERSION@-<os>-<arch>.tar.gz` | Binary for Linux, macOS and FreeBSD (`.zip` for Windows), with LICENSE, NOTICE, THIRD_PARTY and README.md |
| `orcaster-cli-@VERSION@-<os>-<arch>.<ext>.sbom.json` | SPDX SBOM of each archive |

Platforms: linux, darwin and windows on amd64 and arm64; freebsd on amd64.

```bash
sha256sum --ignore-missing -c orcaster-@VERSION@-checksums.txt
tar -xzf orcaster-cli-@VERSION@-linux-amd64.tar.gz orcaster
sudo install orcaster /usr/local/bin/
```

Or `go install github.com/AInvirion/orcaster/orcaster@v@VERSION@`.

## Changes
