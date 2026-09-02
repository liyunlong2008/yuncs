# yuncs — OKX ETH-USDT-SWAP 永续量化机器人

> 开发/维护前先读 [AGENTS.md](AGENTS.md)——项目硬约束、已验证的 OKX 数据、踩坑记录都固化在里面。

单交易所（OKX）、单交易对（ETH-USDT-SWAP）、小资金（≤20U）挑战赛玩法量化机器人。
**无胜利点**：核心是持续正确操作、把权益做大；一条随盈利加深自动收紧的**动态出局线**
（起步峰值回撤 30% 出局 → 约 1.5 倍后最多回吐 10% 锁利，线性过渡无悬崖）在反转时保护利润，
出局线触发即结束本轮并**进程内自动开新一轮**（纸盘重置初始资金、实盘从当前真实余额复合）。

默认策略：**海龟式通道突破**（5m 周期，收盘突破前 30 根通道开仓，前 15 根移动止损），
EMA 交叉策略 `trend_ema` 保留可选。

同一套策略代码跑 **回测 / 纸盘 / 实盘** 三种模式，核心计算（强平价、资金费、费用、仓位换算）严格按 OKX 官方口径。

## 特性

- 纸盘 = 真实行情 + 本地撮合（按盘口深度成交 + 滑点 + 手续费 + 资金费 + 强平保护），**绝不影响真实账户**
- 实盘 = 真实下单，持仓/余额/强平价**一律以交易所返回为准**
- 行情源：WebSocket 优先，8 秒无数据自动降级 REST 轮询（本地代理环境可用）
- 玩法适配回测：按轮次逐 K 线回放 + 资金费结算 + 强平检查 + 动态出局线，
  报告输出**每轮结束倍数分布**（锁利轮/止损轮占比、平均/最高倍数）
- FastAPI + 单页 Web 看板：实时权益曲线、当前轮倍数/出局线、持仓（USDT 口径：可买/成本/预估成本价）、成交明细
- 配置全 TOML（无 env 文件），密钥独立 `secrets.toml`（gitignore）；`initial_balance=0` 实盘自动用实际余额

## 技术栈

Python ≥3.12 · uv · ccxt/ccxt.pro · FastAPI · loguru · aiosqlite · pydantic

## 快速开始

```bash
uv sync                       # 安装依赖
cp config.example.toml config.toml
cp secrets.example.toml secrets.toml   # 纸盘可不填 key；实盘必须填
uv run pytest                 # 跑单测
uv run python -m app.run --mode paper  # 纸盘跑起来（含看板 http://127.0.0.1:8000）
```

开发机（Windows）访问 OKX 需要代理：`config.toml` 里 `proxy = "http://127.0.0.1:10808"`；
VPS 直连留空，`feed = "auto"` 会自动用 WebSocket。

## 回测

```bash
# 下载 1m K 线（CSV 缓存到 data/，第二次跑不再下载）+ 资金费历史，回放挑战
uv run python -m app.backtest_cli --start 2026-08-25 --end 2026-09-01
uv run python -m app.backtest_cli --start 2026-08-01 --end 2026-08-31 --timeframe 5m --force
```

回测与纸盘/实盘共用策略、挑战引擎、撮合模型与 OKX 计算方法，结论可直接迁移到实盘。

## 实盘

```bash
# 1) secrets.toml 填 OKX API key（需开交易权限，建议仅子账户 + 单独提币白名单）
# 2) config.toml: mode = "live"，建议先在 paper 下跑通几天
# 3) 启动（VPS 用 systemd，见 deploy/）
uv run python -m app.run --mode live
```

安全设计：实盘模式无 key 拒绝启动；启动预检（余额/规格/杠杆）；每 10s 从交易所对账；
`POST /api/kill` 或 Ctrl+C 平仓结算；挑战回撤出局自动全平停止。

## 目录

```
app/
  config.py       TOML 配置加载校验
  okx_feed.py     行情订阅（WS/REST 自动切换）+ 合约规格/费率拉取
  okx_math.py     OKX 官方计算方法（强平价/资金费/成本/换算）
  fills.py        撮合与滑点模型（纸盘+回测共用）
  wallet.py       纸盘钱包（独立记账）
  broker.py       paper/live 统一接口，持仓以交易所为准
  strategy.py     策略框架 + trend_ema（EMA 交叉 + ATR 止损 + 盈亏比止盈）
  challenge.py    轮次引擎（动态出局线：容忍率线性收紧，无胜利点）
  engine.py       主循环（bar 驱动 + 资金费 + 强平保护 + 状态广播）
  backtest.py     玩法适配回测
  store.py        SQLite（挑战轮次/成交/权益曲线）
  api.py           REST + WebSocket + 静态看板
tests/            okx 计算方法/挑战规则/撮合/回测确定性
deploy/           VPS 部署（systemd + caddy）
```

## 部署（VPS Ubuntu）

见 `deploy/README.md`：`uv sync --frozen` + systemd 常驻 + caddy 反代看板（Basic Auth，对外端口 8765）。

## 风险提示

小资金高杠杆挑战玩法风险极高，翻倍过程中爆仓概率高。本机器人带强平保护与回撤熔断，
但历史回测不代表未来表现。**实盘前务必纸盘验证，资金损失风险自负。**
