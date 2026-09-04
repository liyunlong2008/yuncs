"""ADX 趋势过滤实验：能否把实盘连续语义转正。

窗口: 1年@5m / 3年@15m（缓存数据）；语义: compounding + margin_frac=0.25；
阈值: adx_min ∈ {0(基线), 18, 24, 30}。
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


def load(tf: str, start: str, end: str) -> list[dict]:
    bars = []
    for day in data.iter_days(start, end):
        b = data.load_candles_csv(data.day_cache_path(tf, day))
        if b:
            bars.extend(b)
    return sorted({x["ts"]: x for x in bars}.values(), key=lambda x: x["ts"])


def main() -> None:
    cfg = load_config("config.toml")
    cfg.strategy.name = "donchian"
    cfg.risk.margin_frac = 0.25
    for label, tf, start, end in WINDOWS:
        bars = load(tf, start, end)
        print(f"\n===== {label} ({len(bars)} 根) =====", flush=True)
        for adx in (0, 18, 24, 30):
            cfg.strategy.params = {"entry_len": 30, "exit_len": 15, "adx_min": adx}
            bt = Backtest(cfg, bars, [], compounding=True)
            r = bt.run()
            print(
                f"  ADX>={adx:>2}: 期末 {r['end_equity']:>8.2f}U 复合 {r['compounded_multiple']:>6.3f}x "
                f"峰值 {r['max_equity_seen']:>7.2f}U 周期{r['rounds_total']:>3} "
                f"正{r['rounds_positive']:>2}/{r['rounds_completed']:<3} 交易{r['trades']:>4}", flush=True)


if __name__ == "__main__":
    main()
