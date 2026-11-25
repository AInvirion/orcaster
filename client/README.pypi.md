# orcaster-worker

The worker agent of Orcaster, an orchestrator for distributed tasks. A worker
registers with an Orcaster server, claims the tasks assigned to it, runs each
one as a subprocess and reports its output and exit status back to the server.

## Install

The worker needs Python 3.13 or later. Install the wheel into a virtual
environment:

```sh
python3.13 -m venv .venv
.venv/bin/pip install orcaster-worker-<version>-py3-none-any.whl
```

## Usage

```sh
orcaster-worker --help
orcaster-worker --version
orcaster-worker --config-path /path/to/worker.env
```

The worker reads its settings from environment variables with the `ORCH_`
prefix, or from the `.env` file given with `--config-path`. The server
address is `ORCH_BASE_URL`; on first start the worker registers itself with
the one-time `ORCH_ENROLLMENT_TOKEN` issued by the server.

## License

Apache License 2.0.
