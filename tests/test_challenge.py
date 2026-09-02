"""挑战规则单测：翻倍目标 / 回撤出局 / 可选时长。"""
import pytest

from app.challenge import Challenge, ChallengeConfig, Status


def make(initial=20.0, target=2.0, dd=30.0, duration=0.0):
    return Challenge(ChallengeConfig(initial, target, dd, duration))


def test_initial_running():
    c = make()
    assert c.update(20.0) == Status.RUNNING


def test_won_at_target_multiple():
    c = make(initial=20.0, target=2.0)  # 目标 40
    assert c.update(39.9) == Status.RUNNING
    assert c.update(40.0) == Status.WON
    assert "翻倍" in c.result


def test_lost_by_drawdown():
    c = make(initial=20.0, dd=30.0)  # 峰值 20 -> 出局线 14
    assert c.update(14.01) == Status.RUNNING
    assert c.update(13.99) == Status.LOST


def test_drawdown_measured_from_peak():
    c = make(initial=20.0, dd=30.0)
    c.update(25.0)   # 峰值抬到 25, 出局线 17.5
    assert c.update(17.6) == Status.RUNNING
    assert c.update(17.4) == Status.LOST


def test_timeout_when_duration_enabled():
    c = make(initial=20.0, duration=1.0)  # 1 小时
    c.start_ts = 1000.0
    assert c.update(20.0, now=1000.0 + 3599.0) == Status.RUNNING
    assert c.update(20.0, now=1000.0 + 3601.0) == Status.TIMEOUT


def test_no_timeout_when_duration_zero():
    c = make(initial=20.0, duration=0.0)
    c.start_ts = 1000.0
    assert c.update(20.0, now=1000.0 + 999999.0) == Status.RUNNING


def test_status_terminal_stays_fixed():
    c = make(target=1.5)
    c.update(100.0)
    assert c.status == Status.WON
    assert c.update(1.0) == Status.WON  # 终结后不再变化


def test_progress_fields():
    c = make(initial=20.0, target=2.0, dd=30.0)
    p = c.progress(30.0)
    assert p["target_equity"] == 40.0
    assert p["progress_pct"] == pytest.approx(50.0)  # (30-20)/(40-20)
    assert p["status"] == "running"
