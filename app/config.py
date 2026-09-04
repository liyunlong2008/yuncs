"""配置加载：tomllib 读取 config.toml + secrets.toml，pydantic 校验合并。"""
from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, Field


class ChallengeConfig(BaseModel):
    """无胜利点玩法：动态回撤线保护，进程内自动连续轮次。

    出局线 = 运营峰值 × (1 - 容忍率)；容忍率随权益倍数平滑收紧：
    1x→base_drawdown_pct，tight_start_multiple 倍→tight_drawdown_pct，之后保持。
    """
    initial_balance: float = 0     # 0 = auto：实盘启动拉 OKX 实际可用余额；纸盘/回测必须 >0
    base_drawdown_pct: float = 30  # 1x 时回撤容忍（起步容错）
    tight_drawdown_pct: float = 10  # 达到 tight_start_multiple 后收紧到该值（深盈利保护）
    tight_start_multiple: float = 1.5  # 从 1x 到该倍数容忍率线性收紧（约 1.25x 起出局线高于本金）
    duration_hours: float = 0      # 可选单轮时长上限；0 = 不限时
    timeframe: str = "15m"         # 策略 K 线周期（研究结论：均值回归类用 15m，5m 噪声过大）


class ExchangeConfig(BaseModel):
    symbol: str = "ETH/USDT:USDT"
    mode: str = "paper"  # paper | live
    feed: str = "auto"   # ws=WebSocket | rest=REST轮询 | auto=先WS收不到行情自动降级REST
    proxy: str = ""      # 开发机 http://127.0.0.1:10808；VPS 直连留空
    fetch_fees_from_okx: bool = True
    taker_fee: float = 0.0005
    maker_fee: float = 0.0002


class RiskConfig(BaseModel):
    leverage: int = 5        # 开仓杠杆（均值回归 5x 成立、10x+ 悬崖；趋势类另论）
    margin_per_trade: float = 5
    margin_frac: float = 0   # 0=固定 margin_per_trade；>0=保证金随余额按比例缩放（实盘防跌破固定保证金停摆）
    max_notional: float = 1000
    slippage_bps: float = 1.0
    liquidation_buffer: float = 0.05


class StrategyConfig(BaseModel):
    name: str = "rsi_revert"  # 研究结论：RSI(2)均值回归+SMA200 趋势过滤（跨窗口唯一全正）
    params: dict = Field(default_factory=dict)


class StorageConfig(BaseModel):
    db_path: str = "data/bot.db"


class LogConfig(BaseModel):
    level: str = "INFO"


class ApiConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000


class Secrets(BaseModel):
    api_key: str = ""
    api_secret: str = ""
    passphrase: str = ""


class Config(BaseModel):
    challenge: ChallengeConfig = Field(default_factory=ChallengeConfig)
    exchange: ExchangeConfig = Field(default_factory=ExchangeConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    log: LogConfig = Field(default_factory=LogConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    secrets: Secrets = Field(default_factory=Secrets)


def load_config(config_path: str = "config.toml", secrets_path: str = "secrets.toml") -> Config:
    data: dict = {}
    p = Path(config_path)
    if p.exists():
        data.update(tomllib.loads(p.read_text(encoding="utf-8")))
    sp = Path(secrets_path)
    if sp.exists():
        # secrets.toml 结构为 [okx] 段，解开一层映射到 Secrets 字段
        data["secrets"] = tomllib.loads(sp.read_text(encoding="utf-8")).get("okx", {})
    return Config(**data)
