from orcaster_server.models import (
    EXECUTION_STATUS_VALUES,
    TASK_STATUS_VALUES,
)

CNF = "completed_no_fingerprint"


def test_cnf_is_no_longer_a_status() -> None:
    """The enum retirement is total; the nuance lives in the `outcome` column."""
    assert CNF not in TASK_STATUS_VALUES
    assert CNF not in EXECUTION_STATUS_VALUES
