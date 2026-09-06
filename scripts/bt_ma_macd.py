"""ma_macd（MA+MACD×1H 生命线×136）研究：实盘连续语义(compounding)能否转正。

对照 rsi_revert 同窗同口径；验收红线见 AGENTS.md：期末复合倍数全窗/近窗全 >1.0
且优于 rsi_revert 才可切默认。
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
MAMACD_VARIANTS = [
    ("ma_macd 默认", {"name": "ma_macd", "params": {}}),
    ("ma_macd 仅顺线", {"name": "ma_macd",
                       "params": {"enable_t1": False, "enable_t2": False}}),
    ("ma_macd 直进", {"name": "ma_macd", "params": {"confirm": "any"}}),
    ("ma_macd 关放量", {"name": "ma_macd", "params": {"vol_spike_mult": 0}}),
]
BASELINES = [
    ("rsi_revert 基线", {"name": "rsi_revert", "params": {}}),
]


def load(tf: str, start: str, end: str) -> list[dict]:
    bars = []
    for day in data.iter_days(start, end):
        b = data.load_candles_csv(data.day_cache_path(tf, day))
        if b:
            bars.extend(b)
    return sorted({x["ts"]: x for x in bars}.values(), key=lambda x: x["ts"])


def run_variant(cfg, bars, strat: dict, fname: str) -> None:
    cfg.strategy.name = strat["name"]
    cfg.strategy.params = strat["params"]
    bt = Backtest(cfg, bars, [], compounding=True)
    r = bt.run()
    print(f"  {strat['name']:<18} {fname:<7} 复合 {r['compounded_multiple']:>7.3f}x "
          f"期末 {r['end_equity']:>8.2f}U 峰值 {r['max_equity_seen']:>8.2f}U "
          f"周期{r['rounds_total']:>4} 正{r['rounds_positive']:>3}/{r['rounds_completed']:<3} "
          f"胜率{r['win_rate']:>5.1f}% 交易{r['trades']:>5} 强平{r['liquidation_count']}",
          flush=True)


def main() -> None:
    cfg = load_config("config.toml")
    cfg.challenge.timeframe = "15m"
    cfg.challenge.initial_balance = 20.0
    cfg.risk.leverage = 5
    cfg.risk.margin_per_trade = 5.0
    cfg.risk.max_notional = 1000.0
    cfg.risk.margin_frac = 0.0  # 固定 5U（≤20U 玩法口径；分仓首层需 ≥最小单量）
    for label, start, end in WINDOWS:
        bars = load("15m", start, end)
        print(f"\n===== {label} ({len(bars)} 根) =====", flush=True)
        for _, strat in MAMACD_VARIANTS + BASELINES:
            run_variant(cfg, bars, strat, "固定5U")


if __name__ == "__main__":
    main()
