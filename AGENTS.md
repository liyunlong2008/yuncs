# AGENTS.md — yuncs 开发约束（每次会话必读）

本文件约束本项目的一切后续开发。**违反本文件 = 项目事故**。若有改动需求，先说明理由并经用户确认，不得擅自修改。

## 1. 项目定位（不可偏离）

OKX ETH-USDT-SWAP 小资金挑战赛量化机器人，**10u 战神玩法**，个人自用。

- 初始资金 ≤20U，只交易 `ETH-USDT-SWAP`，只接 OKX
- **核心规则：翻倍目标（target_multiple，默认 2 倍）+ 回撤出局（max_drawdown_pct，默认 30%）+ 不限时（duration_hours=0）**
- **不是"限时达到多少"的比赛**。禁止把挑战引擎改回限时目标型；duration_hours 只是可选参数，默认必须为 0
- 过度设计是失败：单交易对、单交易所、单进程、SQLite。**任何引入多交易对/多交易所/微服务/Docker/前端框架/ORM/消息队列的提议直接拒绝**

## 2. 数据准确性（最高优先级，仓位与交易所不一致 = 事故）

- **纸盘/回测**：所有计算必须用 OKX 官方口径，公式集中在 `app/okx_math.py`，禁止在别处自创公式
  - 强平价（逐仓线性）：多仓 `(保证金−面值×张数×开仓均价)/(面值×张数×(MMR+费率−1))`，空仓把 `−` 换 `+`（分母 `MMR+费率+1`）
  - 资金费 = 持仓ETH × 标记价 × 资金费率，UTC 00:00/08:00/16:00 每 8 小时结算
  - 费用 = 名义价值 × 用户实际费率（启动时拉 `/api/v5/account/trade-fee`，失败才回退默认）
- **实盘**：持仓/余额/强平价/资金费**一律以交易所返回为准**（`LiveBroker.refresh_position` 每 10 秒对账），本地只作展示与决策
- **纸盘绝不影响真实账户**：独立 `Wallet` 记账，真实行情 + 本地撮合（盘口深度 + 滑点 + 手续费 + 资金费 + 强平保护）；**禁用 OKX 官方模拟盘**（`x-simulated-trading` 头）
- 数量展示一律 **USDT 口径**：可买（可用×杠杆）、成本（含手续费）、预估成本价；接口/看板字段见 `Position.to_dict`

## 3. 已验证的 OKX 事实（禁止重新推导或"修正"）

| 项目 | 值 |
|---|---|
| 合约面值 | 0.1 ETH/张 |
| 最小下单 | 0.01 张（=0.001 ETH），步进 0.01 张，tick 0.01 |
| isolated 最大杠杆 | 第 1 档 100x（IMR 1%，MMR 0.4%） |
| 默认费率 | taker 0.05% / maker 0.02% |
| 资金费 | 8 小时一次（UTC 00/08/16），±0.75% 上限 |
| spec 来源 | `GET /api/v5/public/instruments` + `position-tiers` **运行时拉取，禁止硬编码** |

## 4. 已知坑（踩过，禁止再踩）

- **ccxt `fetch_ohlcv` 的 `since` 分页对 OKX 有兼容问题**（`before=since-1` 破坏 K 线对齐 → 51000）。下载历史必须用 `app/data.py` 的 OKX 原生 `after` 反向翻页
- OKX 的 `after`/`before` 参数**必须是整数 ms 时间戳**，float 报 `51000 Parameter after error`
- OKX 历史 K 线返回**数组** `[ts,o,h,l,c,vol,...]`，不是 dict
- **ccxt.pro 的 WS 客户端不支持代理**：本地代理环境（Windows + 10808）WS 连不上，`feed="auto"` 8 秒无数据自动降级 REST 轮询（`okx_feed._watchdog`），此行为是特性不是 bug，VPS 直连时 WS 正常
- REST 降级模式下盘口按需拉取：`OkxFeed.ensure_order_book()`，纸盘开平仓前必须经过它

## 5. 架构与依赖方向（禁止打乱）

```
okx_math / okx_feed       地基：OKX 计算方法 + 行情/规格（无业务依赖）
fills / wallet            撮合模型 + 纸盘钱包
broker                    paper/live 统一接口（依赖 feed/okx_math/wallet）
strategy / challenge      策略接口 + trend_ema / 挑战引擎（被 engine 与 backtest 复用）
engine                    主循环（paper/live）
backtest                  玩法适配回测（复用 strategy/challenge/fills/okx_math）
store / api / static      持久化 / FastAPI / 看板
```

- **同一套策略代码跑 回测/纸盘/实盘**（`strategy.py`），挑战引擎三模式共用（`challenge.py`）——新策略只加类、新规则只改 engine，不许复制逻辑到各模式
- 依赖单向：engine/backtest → strategy/challenge/broker → okx_math/fills/wallet → okx_feed
- 行情源：WS 优先，REST 降级；策略是 bar 驱动（1m 默认），不要改成 tick 级实时框架

## 6. 工程约定

- 技术栈固定：Python ≥3.12 + uv + ccxt/ccxt.pro + FastAPI + loguru + aiosqlite + pydantic
- 配置全 TOML：`config.toml`（运行配置）+ `secrets.toml`（密钥，gitignore）。**禁止引入 .env 文件**
- 日志用 loguru（`app/log.py`），看板/API 见 `app/api.py`，紧急停止 `POST /api/kill`
- 合约规格/费率/杠杆档位在 `OkxFeed.load_spec_and_fees()` 运行时拉取，回测的 spec 由 CLI 传入（`InstrumentSpec` 默认值与 OKX 当前一致，仅作离线兜底）

## 7. 测试与验收

- 改动必须跑 `uv run pytest`，全过才算完成；新增/修改 `okx_math`、`challenge`、`fills`、`strategy` 必须同步改单测
- 关键不变量：强平价公式对照 OKX 官方口径（`test_margin_consistency_at_liquidation`）、挑战三态转换、回测确定性（`test_backtest_deterministic`：相同输入 → 相同输出）
- 实盘路径不做自动化测试，靠启动预检 + 纸盘先跑通验证

## 8. 实盘安全红线

- 实盘模式：无 `secrets.toml` key 拒绝启动；改动实盘逻辑必须保留：启动预检、10s 对账、回撤自动全平、kill 双路停止
- 看板只绑 `127.0.0.1`，公网必须走 `deploy/yuncs.nginx` 反代 + Basic Auth
- 改配置/部署先读 `deploy/README.md`（systemd 模板单元 `yuncs@<user>`）

## 9. 开发流程

- 收到新需求先对照本文件：若与硬约束冲突，向用户说明冲突点并给出取舍，**不得静默偏离**
- 改动核心数学（okx_math）前，用 OKX 官方文档/公开 API 验证，并在单测里留验算依据
- 每次会话结束保证：pytest 全过、配置示例与代码一致、README/AGENTS.md 与实际行为一致
