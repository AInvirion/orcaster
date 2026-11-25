"""Lease defaults: the worker asks for 120 s and honors the env alias."""

from orcaster_worker.config import Settings


class TestLeaseConfig:
    def test_default_lease_seconds_is_120(self, monkeypatch):
        """Without env vars, the default lease is 120 s."""
        monkeypatch.delenv("ORCH_LEASE_SECONDS", raising=False)
        s = Settings()
        assert s.lease_seconds == 120

    def test_lease_seconds_alias_is_read_from_the_environment(self, monkeypatch):
        """ORCH_LEASE_SECONDS overrides the default (alias LEASE_SECONDS)."""
        monkeypatch.setenv("ORCH_LEASE_SECONDS", "200")
        s = Settings()
        assert s.lease_seconds == 200


class TestPollRequestDefault:
    def test_pollrequest_lease_default_is_120(self):
        """The PollRequest dataclass default is aligned with the new lease."""
        from orcaster_worker.poll import PollRequest

        req = PollRequest(status="idle")
        assert req.lease_seconds == 120
