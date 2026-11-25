# Vendored third-party JS bundles

These third-party JavaScript files are served locally from `/static` (not from
an external CDN) to harden the Content-Security-Policy of the public `/status/*`
plane and of the dashboard: since nothing depends on external hosts, the CSP
can restrict `script-src` to `self` without allowing CDN domains.

Because they enter the repository as opaque minified blobs, this table records
the provenance and integrity (sha256) of each bundle so future version bumps
can be audited.

## When bumping a version

1. Re-download the file from its canonical URL (column "Source").
2. Recompute the sha256:
   `shasum -a 256 htmx.min.js idiomorph-ext.min.js htmx-ext-sse.js`
3. Update the version and the sha256 in this table.
4. Update the `?v=` cache-busting parameters in the templates that load these
   scripts (`server/orcaster_server/templates/base.html` and
   `server/orcaster_server/templates/status.html`).

## Provenance table

| Local file | Package | Version | sha256 | Source |
| --- | --- | --- | --- | --- |
| `htmx.min.js` | htmx (core) | 2.0.10 | `71ea67185bfa8c98c39d31717c6fce5d852370fcdfd129db4543774d3145c0de` | Download: <https://unpkg.com/htmx.org@2.0.10> — Repo: <https://github.com/bigskysoftware/htmx> |
| `idiomorph-ext.min.js` | idiomorph | 0.7.4 | `a6437e55b1b6a07bc421f0d230266a39399b6826c6ed19e0ed9c63b707444a5f` | Download: <https://unpkg.com/idiomorph@0.7.4/dist/idiomorph-ext.min.js> — Repo: <https://github.com/bigskysoftware/idiomorph> |
| `htmx-ext-sse.js` | htmx-ext-sse (htmx SSE extension) | 2.2.2 | `83eca6fa0611fe2b0bf1700b424b88b5eced38ef448ef9760a2ea08fbc875611` | Download: <https://unpkg.com/htmx-ext-sse@2.2.2/sse.js> — Repo: <https://github.com/bigskysoftware/htmx-extensions> |
