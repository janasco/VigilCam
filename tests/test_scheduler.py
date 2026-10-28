import asyncio
from unittest.mock import MagicMock

from app.services.scheduler import (
    RetentionScheduler,
    scheduler,
    get_retention_scheduler,
    start_retention_scheduler,
    stop_retention_scheduler,
)


def test_scheduler_runs_retention():
    mock_rm = MagicMock()
    mock_rm.run.return_value = {"deleted_count": 1, "deleted": []}
    sched = RetentionScheduler(retention_manager=mock_rm, interval_seconds=10)
    res = sched.run_now()
    assert isinstance(res, dict)
    assert res.get("deleted_count") == 1
    mock_rm.run.assert_called()


def test_scheduler_dry_run_passthrough():
    mock_rm = MagicMock()
    mock_rm.run.return_value = {"dry_run": True, "deleted_count": 0}
    sched = RetentionScheduler(retention_manager=mock_rm, interval_seconds=10)
    res = sched.run(dry_run=True)
    assert res.get("dry_run") is True


def test_scheduler_start_stop_nonblocking():
    mock_rm = MagicMock()
    mock_rm.run.return_value = {"deleted_count": 0}
    sched = RetentionScheduler(retention_manager=mock_rm, interval_seconds=10_000)
    sched.start()
    assert sched.is_running is True
    sched.stop()
    assert sched.is_running is False


def test_shared_singleton():
    assert get_retention_scheduler() is scheduler


def test_startup_hook_callable():
    # Should not raise
    s = start_retention_scheduler()
    assert s is not None
    stop_retention_scheduler()
