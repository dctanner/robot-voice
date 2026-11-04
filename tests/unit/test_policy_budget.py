import pytest

from robot_voice.budget import Budget, BudgetExceeded
from robot_voice.supervisor import IdlePolicy


def test_idle_policy_does_not_extend_hold_on_job_chaining(config):
    p = IdlePolicy(0)
    assert not p.eligible(29, config, False, False, False)
    assert p.eligible(30, config, False, False, False)
    assert not p.eligible(119, config, False, False, True)
    assert p.eligible(120, config, False, False, True)
    assert not p.eligible(120, config, True, False, True)
    assert not p.eligible(120, config, False, True, True)


def test_speech_and_reply_grace_protect_close(config):
    p = IdlePolicy(0)
    p.user(100, True)
    assert not p.eligible(200, config, False, False, False)
    p.user(201, False)
    assert p.last_user == 201
    p.assistant_until = 240
    assert not p.eligible(235, config, False, False, False)
    assert p.eligible(240, config, False, False, False)


def test_explicit_sleep_ignores_task_hold_not_bookkeeping(config):
    p = IdlePolicy(0)
    assert p.eligible(1, config, False, False, True, sleep=True)
    assert not p.eligible(1, config, False, True, True, sleep=True)


def test_cumulative_usage_and_backend_dedupe():
    b = Budget()
    b.reserve("live")
    for seconds in [1, 2, 3]:
        b.record("live", "live", {"seconds": seconds}, False)
    b.record("live", "live", {"seconds": 4}, True)
    b.record("live", "live", {"seconds": 2}, False)
    assert b.active_seconds == 4
    for _ in range(2):
        b.record("backend", "backend", {"input_tokens": 1000, "output_tokens": 200})
    assert abs(b.estimate - (4 * 0.05 / 60 + 0.0002)) < 1e-9


def test_budget_unknown_requests_keep_reserve_and_caps():
    b = Budget(limit=0.2)
    b.reserve("unknown")
    with pytest.raises(BudgetExceeded):
        b.reserve("next")
    assert b.reservations["unknown"] == 0.1
    b = Budget(max_seconds=2)
    with pytest.raises(BudgetExceeded):
        b.record("live", "live", {"seconds": 2}, False)
    b = Budget(max_requests=1)
    b.reserve("a")
    with pytest.raises(BudgetExceeded):
        b.reserve("b")


def test_backend_cached_and_cache_write_usage():
    b = Budget()
    b.record(
        "r",
        "backend",
        {
            "input_tokens": 1000,
            "output_tokens": 200,
            "input_tokens_details": {"cached_tokens": 200, "cache_write_tokens": 300},
        },
    )
    expected = (500 * 0.10 + 200 * 0.01 + 300 * 0.125 + 200 * 0.50) / 1_000_000
    assert abs(b.estimate - expected) < 1e-12
