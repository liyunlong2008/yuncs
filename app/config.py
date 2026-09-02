"""配置加载：tomllib 读取 config.toml + secrets.toml，pydantic 校验合并。"""
from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, Field


class ChallengeConfig(BaseModel):
    """10u 战神玩法：翻倍目标 + 回撤出局，不限时（duration_hours=0 表示不限时）。"""
    initial_balance: float = 20
    target_multiple: float = 2.0    # 目标倍数：权益达初始资金×该值即挑战成功（2=翻倍）
    max_drawdown_pct: float = 30    # 回撤出局：权益从峰值回撤该百分比即失败停止
    duration_hours: float = 0       # 可选时长上限；0 = 不限时
    timeframe: str = "1m"


class ExchangeConfig(BaseModel):
    symbol: str = "ETH/USDT:USDT"
    mode: str = "paper"  # paper | live
    feed: str = "auto"   # ws=WebSocket | rest=REST轮询 | auto=先WS收不到行情自动降级REST
    proxy: str = ""      # 开发机 http://127.0.0.1:10808；VPS 直连留空
    fetch_fees_from_okx: bool = True
    taker_fee: float = 0.0005
    maker_fee: float = 0.0002


class RiskConfig(BaseModel):
    leverage: int = 10
    margin_per_trade: float = 5
    max_notional: float = 1000
    slippage_bps: float = 1.0
    liquidation_buffer: float = 0.05


class StrategyConfig(BaseModel):
    name: str = "trend_ema"
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
        data["secrets"] = tomllib.loads(sp.read_text(encoding="utf-8"))
    return Config(**data)
