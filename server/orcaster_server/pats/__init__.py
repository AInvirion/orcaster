"""User Personal Access Tokens.

Portable credential for machines (`orcaster` CLI), scoped to a workspace and
stored as a SHA-256 hash. The module exposes the documentary ORM model and the
service (mint/list/revoke/delete/resolve). The FastAPI dependency
`require_pat_workspace` lives in `orcaster_server/deps.py` (host level), like
`require_workspace`.
"""
