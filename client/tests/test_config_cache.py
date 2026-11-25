"""Tests for the aggressive cache defaults and the recover>min cross validator."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from orcaster_worker.config import Settings

# The fields use ORCASTER_CACHE_* aliases but Settings has env_prefix="ORCH_",
# so the effective environment variable is ORCH_<alias>.
_ENV_MAX_AGE = "ORCH_ORCASTER_CACHE_MAX_AGE_DAYS"
_ENV_MIN = "ORCH_ORCASTER_CACHE_MIN_FREE_FRACTION"
_ENV_RECOVER = "ORCH_ORCASTER_CACHE_RECOVER_FREE_FRACTION"
_ENV_DELETE_ON_SUCCESS = "ORCH_ORCASTER_CACHE_DELETE_ON_SUCCESS"
_ENV_CACHE_ROOT = "ORCH_ORCASTER_CACHE_ROOT"
_ENV_CLAIM_MIN = "ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION"
_ENV_CLAIM_GATE_MAX = "ORCH_ORCASTER_CACHE_CLAIM_GATE_MAX_SECONDS"
_ENV_SELF_FLUSH = "ORCH_ORCASTER_CACHE_SELF_FLUSH_ON_DISK_PREFLIGHT"


def _clean_env(monkeypatch):
    """Clears the cache vars to isolate the defaults test.

    Includes the claim gate floor: it takes part in the cross validator,
    so a value inherited from the environment can make the construction of
    `Settings()` fail in tests that do not deal with the gate.
    """
    monkeypatch.delenv(_ENV_MAX_AGE, raising=False)
    monkeypatch.delenv(_ENV_MIN, raising=False)
    monkeypatch.delenv(_ENV_RECOVER, raising=False)
    monkeypatch.delenv(_ENV_CLAIM_MIN, raising=False)
    monkeypatch.delenv(_ENV_CLAIM_GATE_MAX, raising=False)


class TestCacheDefaults:
    def test_new_defaults(self, monkeypatch):
        """Settings() without cache env vars uses the aggressive defaults."""
        _clean_env(monkeypatch)
        s = Settings()
        assert s.cache_max_age_days == 3
        assert s.cache_min_free_fraction == pytest.approx(0.20)
        assert s.cache_recover_free_fraction == pytest.approx(0.80)

    def test_invariant_recover_greater_than_min(self, monkeypatch):
        """recover_free_fraction > min_free_fraction with defaults."""
        _clean_env(monkeypatch)
        s = Settings()
        assert s.cache_recover_free_fraction > s.cache_min_free_fraction

    def test_uv_cache_dir_default_none(self, monkeypatch):
        """Without an override, uv_cache_dir is None -> per-job uv is decided by the worker."""
        _clean_env(monkeypatch)
        monkeypatch.delenv("ORCH_ORCASTER_UV_CACHE_DIR", raising=False)
        s = Settings()
        assert s.uv_cache_dir is None

    def test_uv_cache_dir_override_respected(self, monkeypatch):
        """An explicit override is respected (worker-wide, dev/tests)."""
        _clean_env(monkeypatch)
        monkeypatch.setenv("ORCH_ORCASTER_UV_CACHE_DIR", "/tmp/uv-shared")
        s = Settings()
        assert s.uv_cache_dir == Path("/tmp/uv-shared")

    def test_self_flush_on_disk_preflight_default_true(self, monkeypatch):
        """Without an override, the self-flush on the disk preflight is active."""
        _clean_env(monkeypatch)
        monkeypatch.delenv(_ENV_SELF_FLUSH, raising=False)
        s = Settings()
        assert s.cache_self_flush_on_disk_preflight is True

    def test_self_flush_on_disk_preflight_env_override(self, monkeypatch):
        """ORCH_ORCASTER_CACHE_SELF_FLUSH_ON_DISK_PREFLIGHT=false disables the self-flush."""
        _clean_env(monkeypatch)
        monkeypatch.setenv(_ENV_SELF_FLUSH, "false")
        s = Settings()
        assert s.cache_self_flush_on_disk_preflight is False


class TestCrossValidator:
    def test_invalid_combination_raises_error(self, monkeypatch):
        """recover < min raises ValidationError when constructing Settings."""
        monkeypatch.setenv(_ENV_MIN, "0.20")
        monkeypatch.setenv(_ENV_RECOVER, "0.10")
        with pytest.raises((ValueError, ValidationError)):
            Settings()

    def test_recover_equal_to_min_raises_error(self, monkeypatch):
        """recover == min is also invalid: the validator requires strictly greater."""
        monkeypatch.setenv(_ENV_MIN, "0.30")
        monkeypatch.setenv(_ENV_RECOVER, "0.30")
        with pytest.raises((ValueError, ValidationError)):
            Settings()

    def test_valid_override_respected(self, monkeypatch):
        """Valid env vars override the defaults."""
        monkeypatch.setenv(_ENV_MAX_AGE, "7")
        monkeypatch.setenv(_ENV_MIN, "0.10")
        monkeypatch.setenv(_ENV_RECOVER, "0.50")
        s = Settings()
        assert s.cache_max_age_days == 7
        assert s.cache_min_free_fraction == pytest.approx(0.10)
        assert s.cache_recover_free_fraction == pytest.approx(0.50)


class TestResolvedCacheRoot:
    def test_default_is_dedicated_cache_volume(self, monkeypatch):
        """Without an override, resolved_cache_root() points to /var/cache/orcaster.

        The default must NOT be <state_dir>/cache (the data volume), but the
        dedicated cache volume /var/cache/orcaster.
        """
        monkeypatch.delenv(_ENV_CACHE_ROOT, raising=False)
        s = Settings()
        assert s.cache_root is None
        assert s.resolved_cache_root() == Path("/var/cache/orcaster")
        # It must not hang off state_dir (data volume).
        assert s.resolved_cache_root() != s.state_dir / "cache"

    def test_env_override_respected(self, monkeypatch, tmp_path):
        """ORCH_ORCASTER_CACHE_ROOT overrides the default."""
        override = tmp_path / "custom-cache"
        monkeypatch.setenv(_ENV_CACHE_ROOT, str(override))
        s = Settings()
        assert s.cache_root == override
        assert s.resolved_cache_root() == override

    def test_attribute_override_respected(self, monkeypatch, tmp_path):
        """Setting cache_root directly (as the executor tests do)."""
        monkeypatch.delenv(_ENV_CACHE_ROOT, raising=False)
        s = Settings()
        s.cache_root = tmp_path / "scratch-cache"
        assert s.resolved_cache_root() == tmp_path / "scratch-cache"


class TestCacheDeleteOnSuccess:
    def test_default_on(self, monkeypatch):
        """cache_delete_on_success is True by default."""
        monkeypatch.delenv(_ENV_DELETE_ON_SUCCESS, raising=False)
        assert Settings().cache_delete_on_success is True

    def test_override_off(self, monkeypatch):
        """ORCH_ORCASTER_CACHE_DELETE_ON_SUCCESS=false turns it off."""
        monkeypatch.setenv(_ENV_DELETE_ON_SUCCESS, "false")
        assert Settings().cache_delete_on_success is False


class TestClaimMinFreeFraction:
    """Disk gate for claiming tasks."""

    def test_inherits_the_gc_floor_without_override(self, monkeypatch):
        """Without an override, the gate closes exactly where the GC declares
        the pressure unrecoverable: a single threshold to explain to the operator."""
        _clean_env(monkeypatch)
        monkeypatch.delenv(_ENV_CLAIM_MIN, raising=False)
        s = Settings()
        assert s.cache_claim_min_free_fraction is None
        assert s.resolved_claim_min_free_fraction == pytest.approx(s.cache_min_free_fraction)

    def test_explicit_override_wins(self, monkeypatch):
        _clean_env(monkeypatch)
        monkeypatch.setenv(_ENV_MIN, "0.20")
        monkeypatch.setenv(_ENV_CLAIM_MIN, "0.05")
        s = Settings()
        assert s.resolved_claim_min_free_fraction == pytest.approx(0.05)

    def test_zero_disables_the_gate(self, monkeypatch):
        """`0.0` is the operator's escape hatch: no free fraction is < 0."""
        _clean_env(monkeypatch)
        monkeypatch.setenv(_ENV_CLAIM_MIN, "0.0")
        s = Settings()
        assert s.resolved_claim_min_free_fraction == 0.0

    def test_floor_above_the_gc_floor_raises_error(self, monkeypatch):
        """A claim floor > `cache_min_free_fraction` parks the worker.

        The threshold that discriminates is the GC's floor, not its target: the pressure
        pass does not even start above `cache_min_free_fraction` (the
        condition in `task_cache.run_gc` is `free/total < min`), so no higher
        floor can be reopened by the GC. The worker stops claiming and
        the only thing that would unblock it is the age prune (3 days by default). It is
        rejected at startup instead of sitting idle silently.

        `0.20 / 0.80 / 0.50` is the concrete case that the previous version of the
        validator (which only checked against the target) let through.
        """
        _clean_env(monkeypatch)
        monkeypatch.setenv(_ENV_MIN, "0.20")
        monkeypatch.setenv(_ENV_RECOVER, "0.80")
        monkeypatch.setenv(_ENV_CLAIM_MIN, "0.50")
        with pytest.raises((ValueError, ValidationError)):
            Settings()

    def test_floor_above_the_gc_target_raises_error(self, monkeypatch):
        """The new check subsumes the old one: a floor above the GC's target
        is also above the GC's floor, so it is still invalid."""
        _clean_env(monkeypatch)
        monkeypatch.setenv(_ENV_RECOVER, "0.80")
        monkeypatch.setenv(_ENV_CLAIM_MIN, "0.85")
        with pytest.raises((ValueError, ValidationError)):
            Settings()

    def test_floor_equal_to_the_gc_floor_is_valid(self, monkeypatch):
        """Equality is exactly the inherited default: the gate closes where the
        GC declares the pressure unrecoverable."""
        _clean_env(monkeypatch)
        monkeypatch.setenv(_ENV_MIN, "0.20")
        monkeypatch.setenv(_ENV_RECOVER, "0.80")
        monkeypatch.setenv(_ENV_CLAIM_MIN, "0.20")
        assert Settings().resolved_claim_min_free_fraction == pytest.approx(0.20)

    def test_floor_below_the_gc_floor_is_valid(self, monkeypatch):
        """Stopping later than the GC is legitimate: the gate only closes once the
        GC has already tried (and failed) to recover the space."""
        _clean_env(monkeypatch)
        monkeypatch.setenv(_ENV_MIN, "0.20")
        monkeypatch.setenv(_ENV_RECOVER, "0.80")
        monkeypatch.setenv(_ENV_CLAIM_MIN, "0.10")
        assert Settings().resolved_claim_min_free_fraction == pytest.approx(0.10)

    def test_zero_passes_with_a_low_gc_floor(self, monkeypatch):
        """`0.0` disables the gate and falls outside the check: it is not a floor
        "higher than the GC's", it is the absence of a floor."""
        _clean_env(monkeypatch)
        monkeypatch.setenv(_ENV_MIN, "0.20")
        monkeypatch.setenv(_ENV_CLAIM_MIN, "0.0")
        assert Settings().resolved_claim_min_free_fraction == 0.0

    def test_default_none_passes(self, monkeypatch):
        """The `None` default cannot violate the check: it inherits the GC's floor."""
        _clean_env(monkeypatch)
        monkeypatch.setenv(_ENV_MIN, "0.20")
        s = Settings()
        assert s.cache_claim_min_free_fraction is None
        assert s.resolved_claim_min_free_fraction == pytest.approx(0.20)


class TestClaimGateMaxSeconds:
    """Temporal fail-open of the disk gate."""

    def test_default_30_min(self, monkeypatch):
        """Comfortable relative to the 300 s `cache_gc_interval_seconds`: it gives the GC
        several passes to recover before giving up."""
        _clean_env(monkeypatch)
        monkeypatch.delenv(_ENV_CLAIM_GATE_MAX, raising=False)
        s = Settings()
        assert s.cache_claim_gate_max_seconds == pytest.approx(1800.0)
        assert s.cache_claim_gate_max_seconds > 4 * s.cache_gc_interval_seconds

    def test_override_env(self, monkeypatch):
        _clean_env(monkeypatch)
        monkeypatch.setenv(_ENV_CLAIM_GATE_MAX, "60")
        assert Settings().cache_claim_gate_max_seconds == pytest.approx(60.0)

    def test_zero_disables_the_fail_open(self, monkeypatch):
        """`0` is the return to the gate without a deadline: it can stay closed forever."""
        _clean_env(monkeypatch)
        monkeypatch.setenv(_ENV_CLAIM_GATE_MAX, "0")
        assert Settings().cache_claim_gate_max_seconds == 0.0

    def test_negative_rejected(self, monkeypatch):
        _clean_env(monkeypatch)
        monkeypatch.setenv(_ENV_CLAIM_GATE_MAX, "-1")
        with pytest.raises((ValueError, ValidationError)):
            Settings()
