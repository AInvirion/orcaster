from fastapi import APIRouter

from orcaster_server.auth import Auth, AuthConfig, make_auth
from orcaster_server.auth.models import AuthBase


def test_make_auth_returns_auth_with_router_and_deps():
    cfg = AuthConfig(rp_id="localhost", rp_name="T", origin="https://localhost")
    auth = make_auth(AuthBase, cfg)
    assert isinstance(auth, Auth)
    assert isinstance(auth.router, APIRouter)
    assert callable(auth.require_session)
    assert callable(auth.require_session_optional)
    assert auth.config is cfg
