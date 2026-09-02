"""回测引擎单测：确定性、翻倍成功路径、强平路径。"""
import pytest

from app.backtest import Backtest
from app.config import Config
from app.okx_feed import InstrumentSpec

SPEC = InstrumentSpec()  # 默认即 OKX ETH-USDT-SWAP 当前规格


def make_cfg(**over):
    cfg = Config()
    if "risk" in over:
        cfg.risk = cfg.risk.__class__(**{**cfg.risk.model_dump(), **over["risk"]})
    if "challenge" in over:
        cfg.challenge = cfg.challenge.__class__(**{**cfg.challenge.model_dump(), **over["challenge"]})
    if "strategy" in over:
        cfg.strategy = cfg.strategy.__class__(**{**cfg.strategy.model_dump(), **over["strategy"]})
    return cfg


def synth_bars(prices: list[float], start_ts: int = 1_700_000_000_000, tf_ms: int = 60_000) -> list[dict]:
    bars = []
    for i, p in enumerate(prices):
        o = prices[i - 1] if i > 0 else p
        bars.append({"ts": start_ts + i * tf_ms, "o": o, "h": max(o, p) * 1.001,
                     "l": min(o, p) * 0.999, "c": p, "v": 10.0})
    return bars


def flat_series(n: int, price: float) -> list[float]:
    return [price] * n


def uptrend(n: int, start: float, step: float = 0.5) -> list[float]:
    return [start + i * step for i in range(n)]


def test_backtest_deterministic():
    bars = synth_bars(flat_series(80, 3000.0) + uptrend(200, 3000.0, 1.0))
    r1 = Backtest(make_cfg(), bars, []).run()
    r2 = Backtest(make_cfg(), bars, []).run()
    assert r1 == r2  # 相同输入 -> 相同输出


def test_backtest_won_on_strong_uptrend():
    # 目标倍数调小，保证确定性翻倍成功（翻倍路径本身由 test_challenge 覆盖）
    cfg = make_cfg(challenge={"target_multiple": 1.01})
    bars = synth_bars(flat_series(80, 3000.0) + uptrend(300, 3000.0, 2.0))
    r = Backtest(cfg, bars, []).run()
    assert r["challenge_status"] == "won"
    assert r["trades"] > 0
    assert r["final_equity"] > r["initial_balance"]


def test_backtest_reports_fields():
    bars = synth_bars(flat_series(80, 3000.0) + uptrend(100, 3000.0, 0.5))
    r = Backtest(make_cfg(), bars, []).run()
    for k in ("challenge_status", "final_equity", "trades", "wins", "losses",
              "win_rate", "total_fees", "peak_equity", "max_drawdown_pct", "bars"):
        assert k in r


def test_backtest_liquidation_on_crash():
    """高杠杆 + 大 ATR 止损远离开仓价 + 单根暴跌 K 线 -> 强平保护触发。

    止损距离 = ATR×atr_sl_mult，此处放大倍数让止损在强平价下方，
    暴跌先穿越强平价（而非止损）-> 走 liquidation 分支。
    """
    cfg = make_cfg(risk={"leverage": 100, "margin_per_trade": 5},
                   strategy={"params": {"atr_sl_mult": 10.0}})
    prices = flat_series(80, 3000.0) + [3005.0, 3000.0] + flat_series(10, 3000.0)
    bars = synth_bars(prices)
    # 暴跌 K 线：开 3000，低点 2950（穿越强平价 2989 附近，不穿越止损 2945）
    bars[82] = {"ts": bars[82]["ts"], "o": 3000.0, "h": 3001.0, "l": 2950.0, "c": 2955.0, "v": 10.0}
    bt = Backtest(cfg, bars, [])
    r = bt.run()
    reasons = [t["reason"] for t in bt.trades]
    assert any(x == "liquidation" for x in reasons), f"trades={bt.trades}"


def test_backtest_funding_deducted():
    """持仓跨过资金费结算点 -> 按 OKX 公式扣费。"""
    prices = flat_series(80, 3000.0) + [3005.0] * 200 + [3006.0] * 50
    bars = synth_bars(prices)
    funding = [{"ts": bars[0]["ts"] + 150 * 60_000, "rate": 0.0002}]
    bt = Backtest(make_cfg(), bars, funding)
    r = bt.run()
    assert r["challenge_status"] == "running"
    assert bt.wallet.funding_paid > 0
    # 仓位约 0.016 ETH @ 3005，资金费 = 0.016×3005×0.0002
    assert bt.wallet.funding_paid == pytest.approx(0.016 * 3005 * 0.0002, rel=0.1)


def test_backtest_keeps_same_strategy_code():
    """回测与实盘用同一策略类：直接验证 TrendEma 在回测中被实例化使用。"""
    from app.strategy import TrendEma
    cfg = make_cfg()
    bt = Backtest(cfg, synth_bars(flat_series(80, 3000.0) + uptrend(100, 3000.0)), [])
    assert isinstance(bt.strategy, TrendEma)
