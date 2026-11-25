"""Human authentication module (passkeys + sessions + invitations).

Public API for the host. Internals (service.*, router internals) are private.
"""

from orcaster_server.auth.config import AuthConfig
from orcaster_server.auth.factory import Auth, make_auth
from orcaster_server.auth.models import Invitation, PasskeyCredential, Session, User

__all__ = [
    "Auth",
    "AuthConfig",
    "Invitation",
    "PasskeyCredential",
    "Session",
    "User",
    "make_auth",
]
