"""轮次引擎单测：动态出局线（容忍率线性收紧、无悬崖）、锁利/止损/超时/重置。"""
import pytest

from app.challenge import Challenge, ChallengeConfig, Status


def make(initial=20.0, base=30.0, tight=10.0, start_mult=2.0, duration=0.0):
    return Challenge(ChallengeConfig(initial, base, tight, start_mult, duration))


def test_tolerance_linear_no_cliff():
    cfg = ChallengeConfig(20.0, base_drawdown_pct=30.0, tight_drawdown_pct=10.0,
                          tight_start_multiple=2.0)
    assert cfg.tolerance(1.0) == pytest.approx(30.0)
    assert cfg.tolerance(1.5) == pytest.approx(20.0)   # 线性中点
    assert cfg.tolerance(1.29) == pytest.approx(30.0 - 20.0 * 0.29)  # 无开关阈值
    assert cfg.tolerance(1.30) == pytest.approx(30.0 - 20.0 * 0.30)
    assert cfg.tolerance(2.0) == pytest.approx(10.0)
    assert cfg.tolerance(5.0) == pytest.approx(10.0)   # 2x 后保持 10%，不再放松


def test_guard_level():
    c = make(initial=20.0)
    assert c.guard_level() == pytest.approx(14.0)  # 起步 30% 容忍
    c.update(30.0)                                 # 峰值 30 (1.5x)
    assert c.tolerance() == pytest.approx(20.0)
    assert c.guard_level() == pytest.approx(24.0)  # 收紧：反转最多回吐到 24


def test_guard_loss_below_start():
    c = make(initial=20.0)
    assert c.update(14.01) == Status.RUNNING
    assert c.update(13.99) == Status.GUARD
    assert "止损" in c.result


def test_guard_locks_profit_after_gain():
    c = make(initial=20.0)
    c.update(30.0)                                  # 1.5x，guard=24
    assert c.update(24.01) == Status.RUNNING
    assert c.update(23.9) == Status.GUARD
    assert "锁利" in c.result
    assert c.guard_equity == pytest.approx(24.0)


def test_no_cliff_around_threshold():
    """1.29x 与 1.3x 行为连续：保护线平滑，不存在"过线才有保护"的悬崖。"""
    c1, c2 = make(), make()
    c1.update(25.8)   # 1.29x
    c2.update(26.0)   # 1.30x
    # 两条保护线都应远高于 30% 固定网（18.06 / 18.2），保护力度连续
    assert c1.guard_level() == pytest.approx(25.8 * (1 - 0.242), rel=1e-9)
    assert c2.guard_level() == pytest.approx(26.0 * (1 - 0.24), rel=1e-9)
    # 同样的回撤幅度下二者同进退：都还剩 ~17% 空间时不触发
    assert c1.update(25.8 * 0.83) == Status.RUNNING
    assert c2.update(26.0 * 0.83) == Status.RUNNING


def test_guard_measured_from_peak():
    c = make(initial=20.0)
    c.update(26.0)      # peak 26 -> guard = 26×(1-0.24) = 19.76
    assert c.update(20.0) == Status.RUNNING
    assert c.update(19.5) == Status.GUARD


def test_timeout_when_duration_enabled():
    c = make(initial=20.0, duration=1.0)
    c.start_ts = 1000.0
    assert c.update(20.0, now=1000.0 + 3599.0) == Status.RUNNING
    assert c.update(20.0, now=1000.0 + 3601.0) == Status.TIMEOUT


def test_start_round_resets():
    c = make(initial=20.0)
    c.update(30.0)
    c.update(23.9)
    assert c.status == Status.GUARD
    c.start_round(20.0)
    assert c.status == Status.RUNNING
    assert c.peak_equity == 20.0
    assert c.result is None
    assert c.update(19.9) == Status.RUNNING


def test_terminal_stays_fixed():
    c = make(initial=20.0)
    c.update(13.0)
    assert c.status == Status.GUARD
    assert c.update(1.0) == Status.GUARD  # 终结后不再变化


def test_progress_fields():
    c = make(initial=20.0)
    c.update(30.0)  # 峰值 30 (1.5x) -> guard 24
    p = c.progress(30.0)
    assert p["multiple"] == pytest.approx(1.5)
    assert p["status"] == "running"
    assert p["guard_level"] == pytest.approx(24.0)
