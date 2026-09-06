"""ma_macd（MA+MACD×1H 生命线×136，项目唯一内置策略）参数研究。

实盘连续语义(compounding)验证；验收红线见 AGENTS.md/README：
默认/宣称盈利前必须给 compounding 期末倍数，未过 1.0 一律按期望为负对待。
"""
import sys
sys.path.insert(0, ".")

from app import data
from app.backtest import Backtest
from app.config import load_config

WINDOWS = [
    ("3年@15m", "2023-09-01", "2026-09-05"),
    ("近1年@15m", "2025-09-01", "2026-09-05"),
]
VARIANTS = [
    ("默认", {}),
    ("仅顺线(t3/t4)", {"enable_t1": False, "enable_t2": False}),
    ("直进(confirm=any)", {"confirm": "any"}),
    ("关放量过滤", {"vol_spike_mult": 0}),
]


def load(tf: str, start: str, end: str) -> list[dict]:
    bars = []
    for day in data.iter_days(start, end):
        b = data.load_candles_csv(data.day_cache_path(tf, day))
        if b:
            bars.extend(b)
    return sorted({x["ts"]: x for x in bars}.values(), key=lambda x: x["ts"])


def run_variant(cfg, bars, label: str, params: dict) -> None:
    cfg.strategy.params = params
    bt = Backtest(cfg, bars, [], compounding=True)
    r = bt.run()
    print(f"  {label:<18} 复合 {r['compounded_multiple']:>7.3f}x "
          f"期末 {r['end_equity']:>8.2f}U 峰值 {r['max_equity_seen']:>8.2f}U "
          f"周期{r['rounds_total']:>4} 胜率{r['win_rate']:>5.1f}% 交易{r['trades']:>5} "
          f"强平{r['liquidation_count']}", flush=True)


def main() -> None:
    cfg = load_config("config.toml")
    cfg.challenge.timeframe = "15m"
    cfg.challenge.initial_balance = 20.0
    cfg.risk.leverage = 5
    cfg.risk.margin_per_trade = 5.0
    cfg.risk.max_notional = 1000.0
    cfg.risk.margin_frac = 0.0  # 固定 5U（≤20U 玩法口径；分批首层需 ≥最小单量）
    for label, start, end in WINDOWS:
        bars = load("15m", start, end)
        print(f"\n===== {label} ({len(bars)} 根) =====", flush=True)
        for vname, vp in VARIANTS:
            run_variant(cfg, bars, vname, vp)


if __name__ == "__main__":
    main()
