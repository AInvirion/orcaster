import asyncio
from pathlib import Path

from orcaster_worker.egress_proxy import ProxyPolicy, _serve


async def test_connect_denied_returns_403(tmp_path: Path, unused_tcp_port):
    pol = ProxyPolicy(base=[], allow_file=tmp_path / "a.txt")
    server_task = asyncio.create_task(_serve(unused_tcp_port, pol))
    await asyncio.sleep(0.1)
    r, w = await asyncio.open_connection("127.0.0.1", unused_tcp_port)
    w.write(b"CONNECT evil.com:443 HTTP/1.1\r\nHost: evil.com:443\r\n\r\n")
    await w.drain()
    resp = await r.readline()
    assert b"403" in resp
    w.close()
    server_task.cancel()


async def test_connect_control_char_host_returns_403(tmp_path: Path, unused_tcp_port):
    # Host with an embedded control byte that, if matched literally, would
    # satisfy the subdomain suffix rule (".pypi.org") even though it is not
    # a valid DNS hostname. The character guard (_HOST_RE) must reject it
    # with 403 before reaching the allowlist matching, instead of relying
    # on DNS resolution failing (502) as the only defense.
    pol = ProxyPolicy(base=["pypi.org"], allow_file=tmp_path / "a.txt")
    server_task = asyncio.create_task(_serve(unused_tcp_port, pol))
    await asyncio.sleep(0.1)
    r, w = await asyncio.open_connection("127.0.0.1", unused_tcp_port)
    w.write(b"CONNECT evil.com\x01.pypi.org:443 HTTP/1.1\r\nHost: evil.com\r\n\r\n")
    await w.drain()
    resp = await r.readline()
    assert b"403" in resp
    w.close()
    server_task.cancel()
