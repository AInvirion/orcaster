"""Body limit: isolated ASGI middleware, no DB."""

import re

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from orcaster_server.body_limit import BodyLimitMiddleware, BodyLimitRule
from orcaster_server.config import settings

LIMIT = 1000
BIG = 5000


async def _echo_len(request: Request) -> JSONResponse:
    return JSONResponse({"n": len(await request.body())})


def _client() -> httpx.AsyncClient:
    app = Starlette(
        routes=[
            Route("/any", _echo_len, methods=["POST"]),
            Route("/v1/tasks/1/output", _echo_len, methods=["POST"]),
        ]
    )
    # As in `orcaster_server.main`: via add_middleware, inside the ServerErrorMiddleware.
    app.add_middleware(
        BodyLimitMiddleware,
        default_max_bytes=LIMIT,
        rules=(BodyLimitRule(re.compile(r"/v1/tasks/\d+/output"), BIG),),
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


@pytest.mark.asyncio
async def test_body_at_the_limit_passes() -> None:
    async with _client() as c:
        r = await c.post("/any", content=b"x" * LIMIT)
    assert r.status_code == 200 and r.json() == {"n": LIMIT}


@pytest.mark.asyncio
async def test_declared_content_length_over_limit_is_413_without_reading() -> None:
    async with _client() as c:
        r = await c.post("/any", content=b"SECRETMARK" * 200)
    assert r.status_code == 413
    assert "SECRETMARK" not in r.text


@pytest.mark.asyncio
async def test_chunked_body_over_limit_is_413() -> None:
    async def gen():
        for _ in range(10):
            yield b"x" * 500

    async with _client() as c:
        r = await c.post("/any", content=gen())
    assert r.status_code == 413


@pytest.mark.asyncio
async def test_chunked_body_over_limit_is_413_also_on_a_fastapi_json_route() -> None:
    """FastAPI turns the read failure into a 400; the middleware changes it to 413."""
    from fastapi import FastAPI

    api = FastAPI()

    @api.post("/j")
    async def _j(d: dict) -> dict:
        return d

    api.add_middleware(BodyLimitMiddleware, default_max_bytes=LIMIT)

    async def gen():
        yield b'{"a": "' + b"x" * 5000 + b'"}'

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://t") as c:
        r = await c.post("/j", content=gen(), headers={"content-type": "application/json"})
    assert r.status_code == 413


@pytest.mark.asyncio
async def test_route_rule_raises_the_limit_only_for_that_route() -> None:
    async with _client() as c:
        ok = await c.post("/v1/tasks/1/output", content=b"x" * (LIMIT * 3))
        too_big = await c.post("/v1/tasks/1/output", content=b"x" * (BIG + 1))
        other = await c.post("/any", content=b"x" * (LIMIT * 3))
    assert ok.status_code == 200
    assert too_big.status_code == 413
    assert other.status_code == 413


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [b"abc", b"-1"])
async def test_invalid_content_length_is_400(value: bytes) -> None:
    app = BodyLimitMiddleware(Starlette(), default_max_bytes=LIMIT)
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/any",
        "headers": [(b"content-length", value)],
    }
    await app(scope, receive, send)
    assert sent[0]["status"] == 400


def _real_limits() -> tuple[int, tuple[BodyLimitRule, ...]]:
    """Default limit and route rules as `orcaster_server.main` registers them."""
    from orcaster_server.main import app

    mw = next(m for m in app.user_middleware if m.cls is BodyLimitMiddleware)
    return mw.kwargs["default_max_bytes"], mw.kwargs["rules"]


def _real_rules_client() -> httpx.AsyncClient:
    """Minimal app with the REAL rules of `orcaster_server.main` (no DB or auth)."""
    default_max_bytes, rules = _real_limits()
    app = Starlette(
        routes=[
            Route("/any", _echo_len, methods=["POST"]),
            Route("/v1/tasks/1/output", _echo_len, methods=["POST"]),
        ]
    )
    app.add_middleware(BodyLimitMiddleware, default_max_bytes=default_max_bytes, rules=rules)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


MIB = 1024 * 1024


@pytest.mark.asyncio
async def test_output_route_accepts_the_worst_legal_worker_batch() -> None:
    """A legal batch from the deployed worker weighs up to ~23.25 MiB (262144 events of
    1 byte x ~83 B of envelope); the real `/output` limit must let it through."""
    async with _real_rules_client() as c:
        r = await c.post("/v1/tasks/1/output", content=b"x" * (24 * MIB))
    assert r.status_code == 200 and r.json() == {"n": 24 * MIB}


@pytest.mark.asyncio
async def test_output_route_rejects_a_body_over_its_limit() -> None:
    async with _real_rules_client() as c:
        r = await c.post("/v1/tasks/1/output", content=b"x" * (32 * MIB + 1))
    assert r.status_code == 413


@pytest.mark.asyncio
async def test_output_sized_body_to_any_other_path_is_413_at_the_default() -> None:
    async with _real_rules_client() as c:
        r = await c.post("/any", content=b"x" * (24 * MIB))
    assert r.status_code == 413


def test_output_default_is_32_mib() -> None:
    from orcaster_server.config import Settings

    assert Settings(_env_file=None).max_output_body_bytes == 32 * MIB


def test_real_artifact_rule_covers_the_per_execution_total_plus_overhead() -> None:
    _, rules = _real_limits()
    middleware = BodyLimitMiddleware(Starlette(), default_max_bytes=1, rules=rules)
    limit = middleware._limit_for({"path": "/v1/tasks/7/artifacts"})
    assert limit == settings.max_artifact_upload_body_bytes
    assert limit > settings.artifact_max_total_bytes


@pytest.mark.asyncio
async def test_route_rule_lower_than_the_default_applies() -> None:
    """The rule REPLACES the default: a lower one also applies (Caddy applies the
    literal number of each `handle`; the app must match)."""
    app = Starlette(routes=[Route("/low", _echo_len, methods=["POST"])])
    app.add_middleware(
        BodyLimitMiddleware,
        default_max_bytes=LIMIT,
        rules=(BodyLimitRule(re.compile(r"/low"), 100),),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        at_rule = await c.post("/low", content=b"x" * 100)
        over_rule = await c.post("/low", content=b"x" * 101)
    assert at_rule.status_code == 200
    assert over_rule.status_code == 413


def test_effective_limit_is_the_max_over_matching_rules_or_the_default() -> None:
    mw = BodyLimitMiddleware(
        Starlette(),
        default_max_bytes=1000,
        rules=(
            BodyLimitRule(re.compile(r"/a/[0-9]+"), 50),
            BodyLimitRule(re.compile(r"/a/1"), 70),
            BodyLimitRule(re.compile(r"/b"), 5000),
        ),
    )
    assert mw._limit_for({"path": "/a/1"}) == 70  # two rules match: the larger of them
    assert mw._limit_for({"path": "/a/2"}) == 50  # only one, lower than the default
    assert mw._limit_for({"path": "/b"}) == 5000
    assert mw._limit_for({"path": "/c"}) == 1000  # no match: the default


def test_unicode_digits_do_not_match_the_route_rules() -> None:
    """`\\d` would match Unicode digits (e.g. Arabic-Indic) and give the large limit to
    a path that Caddy (`[0-9]`) treats with the default."""
    default_max_bytes, rules = _real_limits()
    mw = BodyLimitMiddleware(Starlette(), default_max_bytes=default_max_bytes, rules=rules)
    assert mw._limit_for({"path": "/v1/tasks/\u0661/artifacts"}) == default_max_bytes
    assert mw._limit_for({"path": "/v1/tasks/\u0661/output"}) == default_max_bytes
    assert mw._limit_for({"path": "/v1/tasks/1/artifacts"}) > default_max_bytes
    assert mw._limit_for({"path": "/v1/tasks/1/output"}) > default_max_bytes


@pytest.mark.asyncio
async def test_unicode_digit_artifacts_path_gets_the_default_limit_over_http() -> None:
    default_max_bytes, rules = _real_limits()
    app = Starlette(
        routes=[
            Route("/v1/tasks/{task_id}/artifacts", _echo_len, methods=["POST"]),
        ]
    )
    app.add_middleware(BodyLimitMiddleware, default_max_bytes=default_max_bytes, rules=rules)
    body = b"x" * (default_max_bytes + 1)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        unicode_digit = await c.post("/v1/tasks/%D9%A1/artifacts", content=body)
        ascii_digit = await c.post("/v1/tasks/1/artifacts", content=body)
    assert unicode_digit.status_code == 413
    assert ascii_digit.status_code == 200
