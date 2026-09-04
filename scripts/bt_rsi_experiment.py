"""RsiRevert 均值回归策略实验：实盘连续语义能否转正。

窗口: 1年@5m / 3年@15m；语义: compounding + margin_frac=0.25 与 固定5U 各一档。
"""
import sys
sys.path.insert(0, ".")

from app import data
from app.backtest import Backtest
from app.config import load_config

WINDOWS = [
    ("1年@5m", "5m", "2025-09-01", "2026-09-01"),
    ("3年@15m", "15m", "2023-09-01", "2026-09-01"),
]
VARIANTS = [
    ("rsi2 lo10 hi90", {"rsi_len": 2, "lo": 10, "hi": 90, "sma_len": 200}),
    ("rsi2 lo15 hi85", {"rsi_len": 2, "lo": 15, "hi": 85, "sma_len": 200}),
    ("rsi2 lo10 sma100", {"rsi_len": 2, "lo": 10, "hi": 90, "sma_len": 100}),
]


def load(tf: str, start: str, end: str) -> list[dict]:
    bars = []
    for day in data.iter_days(start, end):
        b = data.load_candles_csv(data.day_cache_path(tf, day))
        if b:
            bars.extend(b)
    return sorted({x["ts"]: x for x in bars}.values(), key=lambda x: x["ts"])


def main() -> None:
    cfg = load_config("config.toml")
    for label, tf, start, end in WINDOWS:
        bars = load(tf, start, end)
        print(f"\n===== {label} ({len(bars)} 根) =====", flush=True)
        for vname, vp in VARIANTS:
            cfg.strategy.name = "rsi_revert"
            cfg.strategy.params = vp
            for frac, fname in ((0.25, "缩放25%"), (0.0, "固定5U")):
                cfg.risk.margin_frac = frac
                bt = Backtest(cfg, bars, [], compounding=True)
                r = bt.run()
                print(
                    f"  {vname:<18} {fname}: 期末 {r['end_equity']:>8.2f}U "
                    f"复合 {r['compounded_multiple']:>7.3f}x 峰值 {r['max_equity_seen']:>7.2f}U "
                    f"周期{r['rounds_total']:>3} 正{r['rounds_positive']:>2}/{r['rounds_completed']:<3} "
                    f"胜率{r['win_rate']:>5.1f}% 交易{r['trades']:>5}", flush=True)


if __name__ == "__main__":
    main()
