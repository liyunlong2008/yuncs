"""回测引擎单测：确定性、轮次结构、超时自动开新一轮、强平、资金费。"""
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
        merged = {**cfg.strategy.model_dump(), **over["strategy"]}
        if "params" in over["strategy"]:
            merged["params"] = {**cfg.strategy.params, **over["strategy"]["params"]}
        cfg.strategy = cfg.strategy.__class__(**merged)
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
    r1 = Backtest(make_cfg(challenge={"initial_balance": 20.0}), bars, []).run()
    r2 = Backtest(make_cfg(challenge={"initial_balance": 20.0}), bars, []).run()
    assert r1 == r2  # 相同输入 -> 相同输出


def test_backtest_rounds_structure():
    bars = synth_bars(flat_series(80, 3000.0) + uptrend(200, 3000.0, 1.0))
    r = Backtest(make_cfg(challenge={"initial_balance": 20.0}), bars, []).run()
    for k in ("rounds_total", "rounds_completed", "round_positive_rate",
              "avg_end_multiple", "trades", "win_rate", "total_fees",
              "total_funding", "liquidation_count", "bars"):
        assert k in r
    assert r["rounds_total"] >= 1
    assert r["bars"] == 280


def test_backtest_timeout_opens_new_round():
    """单轮限时 1h，数据 6.7h -> 至少超时两轮，进程内自动重置继续。"""
    cfg = make_cfg(challenge={"initial_balance": 20.0, "duration_hours": 1.0})
    bars = synth_bars(flat_series(80, 3000.0) + uptrend(320, 3000.0, 0.5))
    bt = Backtest(cfg, bars, [])
    r = bt.run()
    assert r["rounds_completed"] >= 2
    statuses = [x["status"] for x in bt.rounds]
    assert statuses.count("timeout") >= 2
    assert r["last_round_status"] in ("timeout", "end_of_data")


def test_backtest_reports_end_of_data_round():
    bars = synth_bars(flat_series(80, 3000.0) + uptrend(100, 3000.0, 0.5))
    bt = Backtest(make_cfg(challenge={"initial_balance": 20.0}), bars, [])
    r = bt.run()
    assert bt.rounds[-1]["status"] == "end_of_data"


def test_backtest_liquidation_on_crash():
    """高杠杆 + 大 ATR 止损远离开仓价 + 单根暴跌 K 线 -> 强平保护触发。

    用 trend_ema（atr_sl_mult 放大让止损低于强平价），验证强平分支本身。
    """
    cfg = make_cfg(challenge={"initial_balance": 20.0},
                   risk={"leverage": 100, "margin_per_trade": 5},
                   strategy={"name": "trend_ema", "params": {"atr_sl_mult": 10.0}})
    prices = flat_series(80, 3000.0) + [3005.0, 3000.0] + flat_series(10, 3000.0)
    bars = synth_bars(prices)
    bars[82] = {"ts": bars[82]["ts"], "o": 3000.0, "h": 3001.0, "l": 2950.0, "c": 2955.0, "v": 10.0}
    bt = Backtest(cfg, bars, [])
    bt.run()
    reasons = [t["reason"] for t in bt.trades]
    assert any(x == "liquidation" for x in reasons), f"trades={bt.trades}"


def test_backtest_funding_deducted():
    """持仓跨过资金费结算点 -> 按 OKX 公式扣费（缓慢上行，通道止损不触发）。"""
    # 平盘 80 根 -> 跳涨 5U 触发突破开多 -> 缓步上行 200 根（止损线低于价格）
    # 用 donchian 制造确定持仓（默认策略 rsi_revert 在此序列可能不开仓）
    prices = flat_series(80, 3000.0) + [3005.0] + [3005.0 + 0.2 * i for i in range(200)]
    bars = synth_bars(prices)
    funding = [{"ts": bars[0]["ts"] + 150 * 60_000, "rate": 0.0002}]
    cfg = make_cfg(challenge={"initial_balance": 20.0},
                   strategy={"name": "donchian", "params": {"entry_len": 30, "exit_len": 15}})
    bt = Backtest(cfg, bars, funding)
    bt.run()
    assert bt.wallet.funding_paid > 0
    # 默认杠杆 5x: 名义=min(5×5,1000)=25U -> 仓位约 0.008 ETH
    # 资金费 = 0.008×标记价×0.0002（标记价≈3019）
    assert bt.wallet.funding_paid == pytest.approx(0.008 * 3019 * 0.0002, rel=0.15)


def test_backtest_requires_positive_initial():
    with pytest.raises(ValueError):
        Backtest(make_cfg(challenge={"initial_balance": 0.0}), synth_bars([3000.0] * 10), [])


def test_backtest_keeps_same_strategy_code():
    """回测与实盘用同一策略类（默认 rsi_revert，可通过配置切换）。"""
    from app.strategy import DonchianBreakout, RsiRevert, TrendEma
    cfg = make_cfg(challenge={"initial_balance": 20.0})
    bt = Backtest(cfg, synth_bars(flat_series(80, 3000.0) + uptrend(100, 3000.0)), [])
    assert isinstance(bt.strategy, RsiRevert)
    cfg2 = make_cfg(challenge={"initial_balance": 20.0}, strategy={"name": "trend_ema"})
    assert isinstance(Backtest(cfg2, synth_bars([3000.0] * 100), []).strategy, TrendEma)
    cfg3 = make_cfg(challenge={"initial_balance": 20.0}, strategy={"name": "donchian"})
    assert isinstance(Backtest(cfg3, synth_bars([3000.0] * 100), []).strategy, DonchianBreakout)


# ---------- 136 分批与部分平仓的账本测试（fake 策略驱动回测） ----------

from app.strategy import Signal, Strategy  # noqa: E402


class _LegsStrat(Strategy):
    """按固定 bar 序发信号：开首层(10%) -> 补 30% -> 补 60% -> 全平。"""

    name = "_legs"

    def on_bar(self, bar, ctx):
        n = len(ctx.history)
        if not ctx.position.is_open:
            if n == 10:
                self.sl_px = 95.0
                return Signal("open_long", "test L1", frac=0.1)
        else:
            if n == 26:
                return Signal("add_long", "test L2", frac=0.3)
            if n == 42:
                return Signal("add_long", "test L3", frac=0.6)
            if n == 60:
                return Signal("close", "test 全平")
        return Signal("none")


def test_backtest_legs_accounting_136():
    """三批 10/30/60 累加：加权均价、累计保证金、全平后账本守恒。

    价格恒 100、杠杆 5x、margin_per_trade=5 -> 计划名义 25U = 0.25 ETH，
    分批尺寸 = 计划 × 0.1/0.3/0.6（取整到 0.01 张后仍为 0.025/0.075/0.15）。
    """
    bars = synth_bars(flat_series(100, 100.0))
    cfg = make_cfg(challenge={"initial_balance": 20.0})
    bt = Backtest(cfg, bars, [])
    bt.strategy = _LegsStrat({})
    bt.run()

    assert len(bt.trades) == 1                     # 只有全平一条记录
    rec = bt.trades[0]
    assert rec["reason"] == "test 全平"
    assert rec["size_eth"] == pytest.approx(0.25, rel=1e-6)
    assert rec["side"] == "long"
    # 账本守恒：全平后无锁定保证金，余额 = 初始 + 已实现(含买卖滑点) - 总手续费
    assert bt.wallet.margin_locked == 0
    assert bt.wallet.balance == pytest.approx(
        20.0 + bt.wallet.realized_pnl - bt.wallet.fees_paid, abs=1e-9)
    assert bt.wallet.balance > 0
    assert bt._plan_eth == 0.0


class _PartialStrat(Strategy):
    """开 10% 首层 + 挂 110 的目标位部分止盈(50%)；其余交给 end_of_data。"""

    name = "_partial"

    def on_bar(self, bar, ctx):
        n = len(ctx.history)
        if not ctx.position.is_open:
            if n == 10:
                self.sl_px = 98.0
                self.partial_exits = [{"px": 110.0, "frac": 0.5,
                                       "reason": "目标位部分止盈"}]
                return Signal("open_long", "test L1", frac=0.1)
        return Signal("none")


def test_backtest_partial_close_accounting():
    """触及目标位 -> 部分平仓 50%（按比例解锁保证金/记账），剩余仓位继续。"""
    bars = synth_bars(uptrend(220, 100.0, 0.5))    # 涨到 ~209，必然穿过 110
    cfg = make_cfg(challenge={"initial_balance": 20.0})
    bt = Backtest(cfg, bars, [])
    bt.strategy = _PartialStrat({})
    bt.run()

    assert len(bt.trades) == 1
    rec = bt.trades[0]
    assert rec["reason"] == "目标位部分止盈"
    assert rec["exit"] == pytest.approx(110.0)     # 事件价精确成交
    assert rec["pnl"] > 0
    # 半仓离场：剩余仓位与已平记录各占一半；剩余保证金按同口径重算（margin=名义/杠杆）
    assert bt.position.is_open
    assert bt.position.size_eth == pytest.approx(rec["size_eth"], rel=1e-9)
    assert bt.position.margin == pytest.approx(
        bt.position.size_eth * bt.position.entry / 5.0, rel=1e-9)
    assert bt._plan_eth > 0                        # 部分平仓不清除分批计划
    # 守恒：balance + margin_locked + UPL = 初始 - 已付费用 + 已实现 + UPL
    upl = bt.position.unrealized(bars[-1]["c"])
    equity = bt.wallet.balance + bt.wallet.margin_locked + upl
    assert equity == pytest.approx(
        20.0 - bt.wallet.fees_paid + bt.wallet.realized_pnl + upl, abs=1e-9)
