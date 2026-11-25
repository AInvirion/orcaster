"""Pydantic schemas for `/v1/workers/*` endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from orcaster_server.eligibility import validate_worker_capabilities

OS = Literal["linux", "darwin", "freebsd", "windows"]


class WorkerRegisterRequest(BaseModel):
    enrollment_token: str = Field(min_length=1, max_length=256)
    hostname: str = Field(min_length=1, max_length=255)
    os: OS
    arch: str = Field(min_length=1, max_length=64)
    ram_mb: int | None = Field(default=None, ge=0)
    disk_mb: int | None = Field(default=None, ge=0)
    version: str | None = Field(default=None, max_length=64)
    tags: list[str] = Field(default_factory=list)
    capabilities: dict = Field(default_factory=dict)
    name: str | None = Field(default=None, max_length=128)

    @field_validator("capabilities")
    @classmethod
    def _validate_capabilities(cls, v: dict) -> dict:
        """Flat map name -> boolean|number (`orcaster_server/eligibility.py`).

        Registration and the poll are the two writers of the column and share
        this validator. Registration is one-shot, so here a worker that declares a
        string or a nested object fails loudly at enrollment (422), where the
        operator sees it, instead of being enrolled with capabilities that no task
        can request. The poll, a hot path, ignores the invalid value and keeps the
        stored one (`api/poll.py::_poll_capabilities`).
        """
        validate_worker_capabilities(v)  # ValueError -> 422
        return v

    @field_validator("version", mode="before")
    @classmethod
    def _normalize_version(cls, v: object) -> object:
        """Empty is absent, as in the poll.

        Defense against an older client: `register` runs only once per identity,
        so a `""` here leaves `workers.version` blank permanently. Unlike the
        poll, the `max_length` rejection is kept here: register is one-shot and its
        422 is a loud failure that the operator sees at enrollment, not a worker
        that goes offline.
        """
        if isinstance(v, str):
            v = v.strip()
            return v or None
        return v


class WorkerRegisterResponse(BaseModel):
    worker_id: UUID
    api_key: str


class WorkerHeartbeatRequest(BaseModel):
    status: Literal["idle", "busy"]
    current_task_id: int | None = None
    current_execution_progress: int | None = Field(default=None, ge=0, le=100)
    name: str | None = Field(default=None, max_length=128)
    ram_mb: int | None = Field(default=None, ge=0)
    ram_used_mb: int | None = Field(default=None, ge=0)
    disk_mb: int | None = Field(default=None, ge=0)
    disk_used_mb: int | None = Field(default=None, ge=0)


class PendingCommand(BaseModel):
    uuid: UUID
    command: str
    payload: dict | None = None
    signature: str
    expires_at: datetime


class WorkerHeartbeatResponse(BaseModel):
    commands: list[PendingCommand] = Field(default_factory=list)


class WorkerGoodbyeResponse(BaseModel):
    ok: bool = True
