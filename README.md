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

## 策略研究小结（2026-09，真实 OKX 数据）

系统性筛过 8+ 策略族（趋势突破/EMA 趋势/动量/布林/RSI 回归/随机RSI/SuperTrend/震荡趋势双模混合），
验证协议 = 15m 实盘连续语义 + 5x 杠杆 + 训练/样本外多窗口拆分。收敛结论：

- **唯一跨全部独立窗口为正：`rsi_revert`（RSI(2)+SMA200 趋势过滤，lo10/5x/15m）**
  （三窗口 1.11/1.03/1.66x + 此前 3 年逐年 2.29/2.24/1.15x 全正；胜率仅 6~9%，靠少数大反弹盈利）
- boll_revert 次之（2/3 窗口正）；ts_momentum 仅在趋势期窗口正；donchian/EMA/混合全负
- **5m 周期对此类策略噪声过大，一律用 15m**
- **20x 杠杆是均值回归的死亡区（盈亏比倒挂），5x 才成立；10x 亦是悬崖**

**当前状态**：rsi_revert 待 VPS 纸盘前向验证（1~2 周），通过前不投入实盘。
配置示例：`timeframe="15m"`、`strategy.name="rsi_revert"`、`params{lo=10,sma_len=200,exit_rsi=false}`、`leverage=5`。

### 补充研究（2026-09-06）：136 分批体系 ma_macd + 20U 玩法口径复核

按 VC_kxs 体系（MA5/10+MACD 两情相悦 × 1H 生命线 × 四位置 × 136 分仓 × 锁利离场）实现机械版 `ma_macd`，
并在 20U 玩法口径（15m、5x、固定 5U 与 25% 缩放保证金、`compounding=True`）下与 rsi_revert 同窗对照：

| 窗口@15m | ma_macd 默认 | 仅顺线 t3/t4 | confirm=any | 关放量过滤 | rsi_revert 同口径 |
|---|---|---|---|---|---|
| 3年（固定5U） | 0.998x | 0.993x | 0.980x | 0.956x | **0.015x** |
| 近1年（固定5U） | 0.997x | 0.999x | 0.966x | 0.976x | **0.015x** |
| 3年（25%缩放） | 0.995x | 0.993x | — | — | **0.062x** |
| 近1年（25%缩放） | 0.997x | 0.999x | — | — | **0.095x** |

- ma_macd 各变体稳定在 ~0.95~1.00x（含费后微亏、接近打平），但**无一窗口突破 1.0 验收红线**；
  且 3 年仅 33 笔（约 1~3 笔/月），统计上无法与 0 区分——继续调参只是过拟合这几十笔样本，故不切换默认。
- **重要发现：rsi_revert 在 20U + 固定 5U/25% 缩放口径下同样覆灭（0.015~0.095x）**——早年
  "唯一全正"结论依赖当时的小账户口径与窗口段；当前玩法口径下**所有候选策略均不成立**。
  这指向瓶颈在资金层而非信号层（单笔保证金占 25% + 动态出局线 30%/10% 的复利损耗组合），
  是下一步研究的重点，不是"再换一个策略"能解决。
- 结论：默认维持 `rsi_revert`（AGENTS.md 红线：未过 compounding 复核不换默认）；`ma_macd` 保留为
  可选手动启用（`strategy.name="ma_macd"`，机械规则与参数见 strategy.py `MaMacd` 文档串）。**不要投入实盘真钱**。

## 风险提示（务必阅读）

**当前内置策略（donchian）在真实资金语义下期望为负，本仓库定位是玩法/风控引擎与实验台，不是可盈利策略。**

- 回测语义警告：**"纸盘重置"回测（每周期重置初始资金）严重高估真实结果**。纸盘/VPS 运行与真实账户都是连续资金，
  周期亏损会复利叠加。**任何"回测盈利"的结论必须用实盘连续语义复核**（`Backtest(compounding=True)`）。
- 基于真实 OKX 数据的验证（2026-09 完成）：1年@5m、3年@15m，实盘连续语义下当前 donchian 30/15@20x
  无论有无 ADX 趋势过滤、固定或缩放保证金，均把 20U 磨到接近零（复合 0.01~0.24x，取决于是否触发最小下单停摆）。
  纸盘重置语义下 3 年正周期率 36%、单周期峰值 2.6x，但无法弥补多数亏损周期的复利损耗。
- 结论：**不要投入实盘真钱**，除非未来某策略通过 `compounding=True` 的实盘语义回测验证为正期望
  （验证代码已就绪：`scripts/long_backtest.py`、`scripts/bt_adx_experiment.py`）。
- 小资金高杠杆玩法本身风险极高；本机器人带强平保护、出局线与进程重启恢复，但**机械正确 ≠ 策略盈利**。
  资金损失风险自负。
