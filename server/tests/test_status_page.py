"""Public router /status/* : token flow, cross-tenant isolation, no leakage
of sensitive data, index anonymity/stability, and cache header.

The status page does NOT require auth: we use app_client directly (no session
cookie). To seed a token we call the service with the live engine.
"""

from __future__ import annotations

import html
import json
import re
from urllib.parse import quote

import pytest
from sqlalchemy import text

from orcaster_server import db as app_db
from orcaster_server.status.tokens import create_share_token
from orcaster_server.task_variant import VARIANT_TOKEN_MAX_LEN
from orcaster_server.workspaces.constants import DEFAULT_WORKSPACE_ID
from tests.conftest import EXAMPLE_KIND, example_payload

_WS = DEFAULT_WORKSPACE_ID


async def _mint_share_token(workspace_id: str = _WS, label: str | None = None) -> str:
    sm = app_db.get_sessionmaker()
    async with sm() as s:
        token, _ = await create_share_token(s, workspace_id=workspace_id, label=label)
        await s.commit()
    return token


async def _seed_worker(workspace_id: str, *, hostname: str, status: str = "online") -> str:
    sm = app_db.get_sessionmaker()
    async with sm() as s:
        row = await s.execute(
            text(
                """
                INSERT INTO workers (workspace_id, api_key_hash, hostname, os, arch,
                                     version, status, registered_at, last_heartbeat_at)
                VALUES (:ws, :hash, :host, 'linux', 'x86_64', '9.9.9-secret',
                        :status, now(), now())
                RETURNING id::text
                """
            ),
            {"ws": workspace_id, "hash": f"sp-{hostname}", "host": hostname, "status": status},
        )
        wid = row.scalar_one()
        await s.commit()
    return wid


async def _seed_task(workspace_id: str, *, status: str, outcome: str | None = None) -> int:
    sm = app_db.get_sessionmaker()
    async with sm() as s:
        row = await s.execute(
            text(
                """
                INSERT INTO tasks (workspace_id, command, payload, target_mode,
                                   target_count, priority, status, outcome, run_at,
                                   signed, signature)
                VALUES (:ws, 'secret-command', CAST(:p AS jsonb), 'count', 1, 0,
                        :status, :outcome, now(), true, decode(repeat('00', 64), 'hex'))
                RETURNING id
                """
            ),
            {
                "ws": workspace_id,
                "p": json.dumps({"secret": "payload-value"}),
                "status": status,
                "outcome": outcome,
            },
        )
        tid = int(row.scalar_one())
        await s.commit()
    return tid


async def _seed_variant_task(
    workspace_id: str, *, status: str, kind: str | None, label: str | None
) -> int:
    """Task with an explicit variant (columns the app populates on creation)."""
    sm = app_db.get_sessionmaker()
    async with sm() as s:
        row = await s.execute(
            text(
                """
                INSERT INTO tasks (workspace_id, command, payload, target_mode,
                                   target_count, priority, status, run_at, signed,
                                   signature, variant_kind, variant_label)
                VALUES (:ws, 'echo', CAST('{}' AS jsonb), 'count', 1, 0,
                        :status, now(), true, decode(repeat('00', 64), 'hex'),
                        :kind, :label)
                RETURNING id
                """
            ),
            {"ws": workspace_id, "status": status, "kind": kind, "label": label},
        )
        tid = int(row.scalar_one())
        await s.commit()
    return tid


async def _other_workspace(name: str = "Other", slug: str = "other") -> str:
    sm = app_db.get_sessionmaker()
    async with sm() as s:
        row = await s.execute(
            text("INSERT INTO workspaces (name, slug) VALUES (:n, :s) RETURNING id::text"),
            {"n": name, "s": slug},
        )
        ws = row.scalar_one()
        await s.commit()
    return ws


@pytest.mark.asyncio
async def test_valid_token_renders_snapshot(app_client) -> None:
    await _seed_worker(_WS, hostname="alpha")
    await _seed_task(_WS, status="completed")
    await _seed_task(_WS, status="queued")
    token = await _mint_share_token()
    resp = await app_client.get(f"/status/{token}")
    assert resp.status_code == 200
    body = resp.text
    assert "Worker 1" in body
    assert "Default" in body  # workspace name


@pytest.mark.asyncio
async def test_progress_hero_shows_processed_breakdown(app_client) -> None:
    """The hero shows "P of Y tasks processed (X completed · Z failed)"
    and the stacked bar has the three segments with their classes."""
    for _ in range(3):
        await _seed_task(_WS, status="completed")
    await _seed_task(_WS, status="failed")
    await _seed_task(_WS, status="queued")
    token = await _mint_share_token()

    resp = await app_client.get(f"/status/{token}")
    assert resp.status_code == 200
    body = resp.text
    assert "<strong>4</strong> of" in body
    assert "<strong>5</strong> tasks processed" in body
    assert "<strong>3</strong> completed" in body
    assert "<strong>1</strong> failed" in body
    # Stacked bar: one segment per bucket, semantic colors via class. The
    # layers scale with the CAPPED pcts: completed uses progress_pct (3/5=60%)
    # and the failed underlay uses processed_pct (4/5=80%).
    assert "segment-completed" in body
    assert "segment-failed" in body
    assert "segment-pending" in body
    assert re.search(r'class="progress-segment segment-completed"\s+style="--pct: 0.6;"', body)
    assert re.search(r'class="progress-segment segment-failed"\s+style="--pct: 0.8;"', body)
    # Accessibility: descriptive label on the progressbar.
    assert 'aria-label="Processed 4 of 5 tasks: 3 completed, 1 failed"' in body
    # There is still a queued task: closing note absent.
    assert "All tasks processed" not in body


@pytest.mark.asyncio
async def test_progress_bar_never_looks_full_with_pending(app_client) -> None:
    """9999 completed + 1 queued → 99.99% rounds to 100.0; the segments
    scale with the CAPPED pcts (99.9), so the bar never LOOKS full
    while a pending task remains."""
    sm = app_db.get_sessionmaker()
    async with sm() as s:
        await s.execute(
            text(
                """
                INSERT INTO tasks (workspace_id, command, payload, target_mode,
                                   target_count, priority, status, run_at,
                                   signed, signature)
                SELECT :ws, 'echo', CAST('{}' AS jsonb), 'count', 1, 0,
                       'completed', now(), true, decode(repeat('00', 64), 'hex')
                  FROM generate_series(1, 9999)
                """
            ),
            {"ws": _WS},
        )
        await s.commit()
    await _seed_task(_WS, status="queued")
    token = await _mint_share_token()

    resp = await app_client.get(f"/status/{token}")
    body = resp.text
    assert "99.9%" in body
    assert "--pct: 0.999;" in body
    assert "--pct: 1.0;" not in body


@pytest.mark.asyncio
async def test_progress_hero_all_processed_note(app_client) -> None:
    """With everything terminal (even with failures) the note says
    "All tasks processed"; the old copy "tasks completed" is gone."""
    for _ in range(2):
        await _seed_task(_WS, status="completed")
    await _seed_task(_WS, status="failed")
    token = await _mint_share_token()

    resp = await app_client.get(f"/status/{token}")
    body = resp.text
    assert "<strong>3</strong> tasks processed" in body
    assert "All tasks processed" in body
    assert "All tasks completed" not in body
    assert "tasks completed" not in body


@pytest.mark.asyncio
async def test_progress_hero_empty_keeps_no_tasks_yet(app_client) -> None:
    """With no tasks the message "No tasks yet" is kept."""
    token = await _mint_share_token()
    resp = await app_client.get(f"/status/{token}")
    assert "No tasks yet" in resp.text
    assert "tasks processed" not in resp.text


@pytest.mark.asyncio
async def test_status_page_default_current_vs_all_variant(app_client) -> None:
    """The public status page honors ?variant=: default=current rescales to
    the latest version; ?variant=all shows the global aggregate."""
    latest, old = "1.10.0", "1.2.0"
    await _seed_variant_task(_WS, status="completed", kind="example-tool", label=latest)
    for _ in range(3):
        await _seed_variant_task(_WS, status="completed", kind="example-tool", label=old)
    token = await _mint_share_token()

    # Default (no param) = current: only the latest (1 completed).
    default = await app_client.get(f"/status/{token}")
    assert default.status_code == 200
    assert "completed 1</span>" in default.text

    # ?variant=all: global aggregate (4 completed).
    all_v = await app_client.get(f"/status/{token}?variant=all")
    assert all_v.status_code == 200
    assert "completed 4</span>" in all_v.text


@pytest.mark.asyncio
async def test_status_summary_respects_variant_param(app_client) -> None:
    """The (polled) /summary partial also honors ?variant= and stays cacheable."""
    latest, old = "1.10.0", "1.2.0"
    await _seed_variant_task(_WS, status="completed", kind="example-tool", label=latest)
    for _ in range(3):
        await _seed_variant_task(_WS, status="completed", kind="example-tool", label=old)
    token = await _mint_share_token()

    summary_all = await app_client.get(f"/status/{token}/summary?variant=all")
    assert summary_all.status_code == 200
    assert "completed 4</span>" in summary_all.text
    assert summary_all.headers["cache-control"] == "public, max-age=10"

    summary_current = await app_client.get(f"/status/{token}/summary")
    assert "completed 1</span>" in summary_current.text


@pytest.mark.asyncio
async def test_revoked_token_is_404(app_client) -> None:
    from orcaster_server.status.tokens import revoke_share_token

    sm = app_db.get_sessionmaker()
    async with sm() as s:
        token, token_id = await create_share_token(s, workspace_id=_WS, label=None)
        await s.commit()
    async with sm() as s:
        await revoke_share_token(s, token_id=token_id, workspace_id=_WS)
        await s.commit()
    resp = await app_client.get(f"/status/{token}")
    assert resp.status_code == 404
    # The FULL PAGE returns generic HTML, not JSON, without revealing the reason.
    assert "text/html" in resp.headers["content-type"]
    assert "This link is no longer active or was revoked." in resp.text
    # The partial returns 200 + HX-Redirect so htmx navigates to the full 404.
    summary = await app_client.get(f"/status/{token}/summary")
    assert summary.status_code == 200
    assert summary.headers["hx-redirect"] == f"/status/{token}"


@pytest.mark.asyncio
async def test_nonexistent_token_is_404(app_client) -> None:
    resp = await app_client.get("/status/not-a-real-token")
    assert resp.status_code == 404
    # The FULL PAGE returns generic HTML, not JSON.
    assert "text/html" in resp.headers["content-type"]
    assert "This link is no longer active or was revoked." in resp.text
    # The partial returns 200 + HX-Redirect so htmx navigates to the full 404.
    summary = await app_client.get("/status/not-a-real-token/summary")
    assert summary.status_code == 200
    assert summary.headers["hx-redirect"] == "/status/not-a-real-token"


@pytest.mark.asyncio
async def test_summary_token_is_percent_encoded_in_hx_redirect(app_client) -> None:
    """The token reflected in HX-Redirect is percent-encoded (defense in depth).

    A token with an unexpected character (here a space: the route "/status/a b/summary"
    decodes the path param to "a b") must not be reflected raw in the header: it must go
    as "/status/a%20b". This neutralizes reflection of unvalidated input in a
    response header.
    """
    summary = await app_client.get("/status/a b/summary")
    assert summary.status_code == 200
    assert summary.headers["hx-redirect"] == "/status/a%20b"


@pytest.mark.asyncio
async def test_cross_tenant_isolation(app_client) -> None:
    # Workspace B with its own worker; A's token must NOT leak anything from B.
    ws_b = await _other_workspace()
    await _seed_worker(ws_b, hostname="bravo-host")
    await _seed_task(ws_b, status="failed")
    # A stays empty.
    token_a = await _mint_share_token(_WS)
    resp = await app_client.get(f"/status/{token_a}")
    assert resp.status_code == 200
    body = resp.text
    assert "bravo-host" not in body
    # A has no workers: the fleet is empty.
    assert "Worker 1" not in body


@pytest.mark.asyncio
async def test_summary_does_not_leak_sensitive_fields(app_client) -> None:
    await _seed_worker(_WS, hostname="leaky-hostname")
    await _seed_task(_WS, status="queued")  # "running" is not a task_status enum value
    token = await _mint_share_token()
    resp = await app_client.get(f"/status/{token}/summary")
    assert resp.status_code == 200
    body = resp.text
    for forbidden in ("leaky-hostname", "secret-command", "payload-value", "9.9.9-secret"):
        assert forbidden not in body
    assert "Worker 1" in body


@pytest.mark.asyncio
async def test_index_is_stable_when_worker_added(app_client) -> None:
    await _seed_worker(_WS, hostname="first")
    token = await _mint_share_token()
    r1 = await app_client.get(f"/status/{token}/summary")
    assert "Worker 1" in r1.text
    await _seed_worker(_WS, hostname="second")
    r2 = await app_client.get(f"/status/{token}/summary")
    # The second worker stays last (Worker 2), the first is still Worker 1.
    assert "Worker 1" in r2.text
    assert "Worker 2" in r2.text


@pytest.mark.asyncio
async def test_summary_has_cache_header(app_client) -> None:
    token = await _mint_share_token()
    resp = await app_client.get(f"/status/{token}/summary")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "public, max-age=10"


@pytest.mark.asyncio
async def test_status_page_200_has_cache_header(app_client) -> None:
    """The full page is cacheable with the same TTL as the summary.

    Without an explicit header, Cloudflare's Cache Rule would apply the edge default
    (too long).
    """
    token = await _mint_share_token()
    resp = await app_client.get(f"/status/{token}")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "public, max-age=10"


@pytest.mark.asyncio
async def test_status_404_is_no_store(app_client) -> None:
    """The edge must never cache a 404 (a freshly created token that
    had a cached 404 would stay broken until the TTL expired)."""
    resp = await app_client.get("/status/nope")
    assert resp.status_code == 404
    assert resp.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_summary_hx_redirect_is_no_store(app_client) -> None:
    """The 200 + HX-Redirect response (token revoked during
    polling) must not be cached: it would serve phantom redirects."""
    resp = await app_client.get("/status/nope/summary")
    assert resp.status_code == 200
    assert resp.headers["hx-redirect"] == "/status/nope"
    assert resp.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_summary_revoked_token_redirects_via_hx(app_client) -> None:
    """Token revoked while the client has the page open.

    The /summary partial must return 200 + HX-Redirect to /status/{token}
    so that htmx 2.x processes the navigation (a 4xx would not trigger it).
    """
    from orcaster_server.status.tokens import revoke_share_token

    sm = app_db.get_sessionmaker()
    async with sm() as s:
        token, token_id = await create_share_token(s, workspace_id=_WS, label=None)
        await s.commit()
    async with sm() as s:
        await revoke_share_token(s, token_id=token_id, workspace_id=_WS)
        await s.commit()

    resp = await app_client.get(f"/status/{token}/summary")
    assert resp.status_code == 200
    assert resp.headers["hx-redirect"] == f"/status/{token}"


@pytest.mark.asyncio
async def test_public_responses_carry_security_headers(app_client) -> None:
    """The public plane stamps Referrer-Policy/X-Frame-Options on EVERY response.

    The token is a bearer secret in the URL: no-referrer avoids leaking it via
    Referer; DENY prevents framing the page (clickjacking). Covers the four
    return points: valid page, valid summary, 404 and revoked summary.
    """
    from orcaster_server.status.tokens import revoke_share_token

    token = await _mint_share_token()
    page = await app_client.get(f"/status/{token}")
    assert page.status_code == 200
    assert page.headers["referrer-policy"] == "no-referrer"
    assert page.headers["x-frame-options"] == "DENY"

    summary = await app_client.get(f"/status/{token}/summary")
    assert summary.status_code == 200
    assert summary.headers["referrer-policy"] == "no-referrer"
    assert summary.headers["x-frame-options"] == "DENY"

    # Generic 404 (nonexistent token) also hardened.
    missing = await app_client.get("/status/not-a-real-token")
    assert missing.status_code == 404
    assert missing.headers["referrer-policy"] == "no-referrer"
    assert missing.headers["x-frame-options"] == "DENY"

    # Summary of a revoked token (200 + HX-Redirect) also hardened.
    sm = app_db.get_sessionmaker()
    async with sm() as s:
        rtok, rid = await create_share_token(s, workspace_id=_WS, label=None)
        await s.commit()
    async with sm() as s:
        await revoke_share_token(s, token_id=rid, workspace_id=_WS)
        await s.commit()
    revoked = await app_client.get(f"/status/{rtok}/summary")
    assert revoked.status_code == 200
    assert revoked.headers["referrer-policy"] == "no-referrer"
    assert revoked.headers["x-frame-options"] == "DENY"


@pytest.mark.asyncio
async def test_public_csp_allows_same_origin_form_action(app_client) -> None:
    """The version selector is a <form method="get"> to the same origin;
    `form-action 'none'` (historical, from when the page had no
    forms) blocked the submit in the browser and the select was dead.
    """
    token = await _mint_share_token()
    page = await app_client.get(f"/status/{token}")
    assert page.status_code == 200
    csp = page.headers["content-security-policy"]
    assert "form-action 'self'" in csp
    assert "form-action 'none'" not in csp


@pytest.mark.asyncio
async def test_public_responses_carry_csp(app_client) -> None:
    """The public plane stamps a strict Content-Security-Policy.

    The anonymous plane only loads same-origin resources (htmx, status.js,
    polling /summary); the CSP must declare that and also reinforce anti-framing
    with `frame-ancestors 'none'`. We verify the three responses the browser
    interprets: valid page, valid summary and the HTML 404.
    """
    token = await _mint_share_token()

    page = await app_client.get(f"/status/{token}")
    assert page.status_code == 200
    csp = page.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp

    summary = await app_client.get(f"/status/{token}/summary")
    assert summary.status_code == 200
    csp = summary.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp

    missing = await app_client.get("/status/not-a-real-token")
    assert missing.status_code == 404
    csp = missing.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp


@pytest.mark.asyncio
async def test_error_responses_carry_security_headers(app_client) -> None:
    """ERROR responses under /status also carry the security headers.

    Per-handler stamping left unhardened the routes that do not go through the
    router's handlers: a 405 (method not allowed), a validation 422 or an
    unexpected 500. Centralizing the headers in the `/status` plane middleware
    also covers those cases. Here we exercise the 405: `POST /status/{token}` is not
    a declared route (GET only), so Starlette answers 405 without touching the
    handler — before, that response did NOT carry the headers.
    """
    token = await _mint_share_token()
    resp = await app_client.post(f"/status/{token}")
    assert resp.status_code == 405
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert resp.headers["x-frame-options"] == "DENY"


@pytest.mark.asyncio
async def test_snapshot_cache_serves_second_request_without_rebuild(
    app_client, monkeypatch
) -> None:
    """With an active TTL, the second hit within the window does not
    rebuild the snapshot (it does not touch the DB again)."""
    from orcaster_server.api import status_ui
    from orcaster_server.config import settings

    token = await _mint_share_token()
    monkeypatch.setattr(settings, "status_snapshot_cache_ttl_s", 10.0)

    calls = 0
    real_build = status_ui.build_status_snapshot

    async def counting_build(session, workspace_id, variant_param=""):
        nonlocal calls
        calls += 1
        return await real_build(session, workspace_id, variant_param)

    monkeypatch.setattr(status_ui, "build_status_snapshot", counting_build)

    r1 = await app_client.get(f"/status/{token}/summary")
    r2 = await app_client.get(f"/status/{token}/summary")
    assert r1.status_code == r2.status_code == 200
    assert calls == 1
    assert r1.text == r2.text


@pytest.mark.asyncio
async def test_snapshot_cache_page_and_summary_share_entry(app_client, monkeypatch) -> None:
    """Full page and summary share the key (workspace, variant):
    a single build serves both handlers."""
    from orcaster_server.api import status_ui
    from orcaster_server.config import settings

    token = await _mint_share_token()
    monkeypatch.setattr(settings, "status_snapshot_cache_ttl_s", 10.0)

    calls = 0
    real_build = status_ui.build_status_snapshot

    async def counting_build(session, workspace_id, variant_param=""):
        nonlocal calls
        calls += 1
        return await real_build(session, workspace_id, variant_param)

    monkeypatch.setattr(status_ui, "build_status_snapshot", counting_build)

    page = await app_client.get(f"/status/{token}")
    summary = await app_client.get(f"/status/{token}/summary")
    # Without these assertions the test would pass vacuously if both handlers
    # returned a 404/422 before reaching the cache (calls == 0 ≠ 1, but a
    # future change that broke only ONE of the two could slip through).
    assert page.status_code == 200
    assert summary.status_code == 200
    assert calls == 1


@pytest.mark.asyncio
async def test_snapshot_cache_key_includes_variant(app_client, monkeypatch) -> None:
    """`variant` is part of the key (two lenses = two builds) and is
    normalized with strip() (` all ` reuses the `all` entry)."""
    from orcaster_server.api import status_ui
    from orcaster_server.config import settings

    token = await _mint_share_token()
    monkeypatch.setattr(settings, "status_snapshot_cache_ttl_s", 10.0)

    calls = 0
    real_build = status_ui.build_status_snapshot

    async def counting_build(session, workspace_id, variant_param=""):
        nonlocal calls
        calls += 1
        return await real_build(session, workspace_id, variant_param)

    monkeypatch.setattr(status_ui, "build_status_snapshot", counting_build)

    current = await app_client.get(f"/status/{token}/summary?variant=current")
    all_v = await app_client.get(f"/status/{token}/summary?variant=all")
    assert current.status_code == all_v.status_code == 200
    assert calls == 2  # different lenses: they do not share an entry

    padded = await app_client.get(f"/status/{token}/summary?variant=%20all%20")
    assert padded.status_code == 200
    assert calls == 2  # strip collapsed it with `all`
    assert padded.text == all_v.text


@pytest.mark.asyncio
async def test_variant_param_length_is_capped(app_client) -> None:
    """`variant` feeds the cache key, so its length is bounded.

    Without a cap, a client with a share-link could loop over giant variants and
    grow the memory retained by the process.
    """
    token = await _mint_share_token()

    ok = await app_client.get(f"/status/{token}/summary?variant={'x' * 64}")
    assert ok.status_code == 200

    for path in (f"/status/{token}", f"/status/{token}/summary"):
        too_long = await app_client.get(f"{path}?variant={'x' * 65}")
        assert too_long.status_code == 422


@pytest.mark.asyncio
async def test_long_variant_label_round_trips_without_422(
    authed_admin_client, example_plugin
) -> None:
    """The selector/polling round-trip never hits the param cap.

    The invariant is guaranteed at INGESTION: a task whose payload declares an
    absurdly long version is persisted with the pair already bounded, so the
    `value` of the `<option>` and the poll's `hx-get` fit in
    `Query(max_length=VARIANT_TOKEN_MAX_LEN)`. Without this, the HTML the page itself
    served produced requests that FastAPI rejected with 422.

    `authed_admin_client` is the same HTTP client as `app_client` (it only adds
    the session cookie), so it creates the task through the real API and then hits the
    public plane with the share token. The label is produced by the test plugin
    (`example_plugin`): the core ships no extractors.
    """
    huge_version = f"{'9' * 120}.0.0"
    created = await authed_admin_client.post(
        "/admin/tasks",
        json={
            "target": "1",
            "payload": {"requirements": [], **example_payload(version=huge_version)},
        },
    )
    assert created.status_code == 200, created.text
    token = await _mint_share_token()

    page = await authed_admin_client.get(f"/status/{token}?variant=all")
    assert page.status_code == 200
    options = re.findall(rf'<option value="({EXAMPLE_KIND}~[^"]+)"', page.text)
    assert options, page.text
    pair_token = html.unescape(options[0])
    assert len(pair_token) <= VARIANT_TOKEN_MAX_LEN

    # The selector is a native <form method="get">: selecting the pair reloads
    # the page with ?variant=<kind>~<label>.
    lens = await authed_admin_client.get(f"/status/{token}?variant={quote(pair_token, safe='')}")
    assert lens.status_code == 200

    # And the HTMX poll re-emits that same token in its hx-get: following it must give 200.
    poll_url = re.search(r'hx-get="(/status/[^"]+/summary\?variant=[^"]*)"', lens.text)
    assert poll_url, lens.text
    poll = await authed_admin_client.get(html.unescape(poll_url.group(1)))
    assert poll.status_code == 200
    assert poll.headers["cache-control"] == "public, max-age=10"


@pytest.mark.asyncio
async def test_revocation_is_immediate_despite_cache(app_client, monkeypatch) -> None:
    """Token resolution stays OUTSIDE the cache — revoking takes
    effect on the next request even if the snapshot is still valid."""
    from orcaster_server.config import settings
    from orcaster_server.status.tokens import revoke_share_token

    monkeypatch.setattr(settings, "status_snapshot_cache_ttl_s", 10.0)

    sm = app_db.get_sessionmaker()
    async with sm() as s:
        token, token_id = await create_share_token(s, workspace_id=_WS, label=None)
        await s.commit()

    assert (await app_client.get(f"/status/{token}")).status_code == 200

    async with sm() as s:
        await revoke_share_token(s, token_id=token_id, workspace_id=_WS)
        await s.commit()

    assert (await app_client.get(f"/status/{token}")).status_code == 404


@pytest.mark.asyncio
async def test_completed_outcomes_subbadge_in_html(app_client) -> None:
    """The completed_outcomes sub-badge is rendered in the HTML.

    Seeds 1 plain completed + 2 completed with outcome='no_result' + 1 failed.
    Verifies that GET /status/{token} contains the text "completed includes:" and
    the breakdown "no_result 2" in the chip-row section.
    """
    await _seed_worker(_WS, hostname="w1")
    await _seed_task(_WS, status="completed")  # no outcome
    for _ in range(2):
        await _seed_task(_WS, status="completed", outcome="no_result")
    await _seed_task(_WS, status="failed")
    token = await _mint_share_token()

    resp = await app_client.get(f"/status/{token}")
    assert resp.status_code == 200
    body = resp.text

    # Look for the sub-badge under "Tasks by status". `assert "2" in body` would be a
    # weak assertion (it matches any "2" in the HTML, e.g. a status code); the real
    # count is attached to the outcome name (`_status_summary.html`: `{{ name }} {{ n }}`).
    # outcome (`_status_summary.html`: `{{ name }} {{ n }}`).
    assert "completed includes:" in body
    assert "no_result 2" in body  # name + count together, unambiguous

    # Verify it is inside the right section (it must not be in Worker fleet)
    # simple search: the text must be in a plausible order
    completed_idx = body.find("Tasks by status")
    includes_idx = body.find("completed includes:")
    assert completed_idx < includes_idx  # the sub-badge comes after the heading
    assert includes_idx > 0  # exists
