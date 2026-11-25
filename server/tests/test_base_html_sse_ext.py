import pytest


@pytest.mark.asyncio
async def test_base_html_loads_sse_extension(authed_admin_client):
    resp = await authed_admin_client.get("/admin/", headers={"HX-Request": "false"})
    assert resp.status_code == 200
    assert "htmx-ext-sse" in resp.text
