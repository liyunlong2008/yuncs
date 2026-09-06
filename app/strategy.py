"""策略框架：on_bar 决策 + 内置策略（trend_ema / donchian）。

同一套策略代码跑 回测 / 纸盘 / 实盘。
开仓即声明止盈价（tp）与止损价（sl），由引擎按 K 线高低点精确撮合。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from . import okx_math


@dataclass
class Signal:
    action: str   # open_long / open_short / add_long / add_short / close / none
    reason: str = ""
    frac: float = 1.0  # 分批比例：open 首笔 / add 补批用（相对计划量）；1.0=整仓口径


def create_strategy(name: str, params: dict) -> "Strategy":
    """策略工厂：引擎/回测通过名字取策略。

    内置：ma_macd（默认/生产研究主线）；macd_1h（v6 实验：1H 交叉定趋势、
    15m 执行的陪跑族）。2026-09-06 已删除的旧策略族见 README 研究史。
    未知名字回退 ma_macd。
    """
    registry = {
        MaMacd.name: MaMacd,
        Macd1h.name: Macd1h,
    }
    return registry.get(name or MaMacd.name, MaMacd)(params or {})

class Strategy:
    name = "base"

    def __init__(self, params: dict):
        self.params = params
        self.sl_px: Optional[float] = None
        self.tp_px: Optional[float] = None
        # 部分止盈事件表：[{"px": float, "frac": float, "reason": str}]，命中即消费
        self.partial_exits: list[dict] = []

    def on_bar(self, bar: dict, ctx) -> Signal:
        raise NotImplementedError

    def on_open(self) -> None:
        """开仓后清空目标价与部分止盈事件（由子类在开仓信号时设置 sl/tp）。"""
        self.sl_px = None
        self.tp_px = None
        self.partial_exits = []

    def check_tp_sl(self, bar: dict, side: str) -> Optional[tuple[float, str]]:
        """按本根 K 线高低检查止盈止损，返回 (成交价, 原因)；先检查止损。"""
        if self.sl_px is None and self.tp_px is None:
            return None
        if side == "long":
            if self.sl_px is not None and bar["l"] <= self.sl_px:
                return self.sl_px, "止损"
            if self.tp_px is not None and bar["h"] >= self.tp_px:
                return self.tp_px, "止盈"
        else:
            if self.sl_px is not None and bar["h"] >= self.sl_px:
                return self.sl_px, "止损"
            if self.tp_px is not None and bar["l"] <= self.tp_px:
                return self.tp_px, "止盈"
        return None

    def evaluate_exits(self, bar: dict, side: str) -> Optional[tuple[float, str, float]]:
        """按本根 K 线检查离场事件，返回 (成交价, 原因, 平仓比例 frac)。

        约定（引擎与回测共用同一调用）：
        - 止损/硬止盈整仓先于部分止盈；
        - 部分止盈事件命中后从内部表中移除（剩余仓位继续管理，策略状态不清）；
        - 整仓离场事件由调用方在仓位平尽后调 on_open() 复位。
        基类默认 = 旧 check_tp_sl 语义（全平，frac=1.0），旧策略零改动。
        """
        if side == "long":
            if self.sl_px is not None and bar["l"] <= self.sl_px:
                return self.sl_px, "止损", 1.0
            if self.tp_px is not None and bar["h"] >= self.tp_px:
                return self.tp_px, "止盈", 1.0
        else:
            if self.sl_px is not None and bar["h"] >= self.sl_px:
                return self.sl_px, "止损", 1.0
            if self.tp_px is not None and bar["l"] <= self.tp_px:
                return self.tp_px, "止盈", 1.0
        return self._partial_exit_hit(bar, side)

    def _partial_exit_hit(self, bar: dict, side: str) -> Optional[tuple[float, str, float]]:
        """部分止盈事件表命中检查：命中即消费该事件，返回 (px, reason, frac)。"""
        for ev in self.partial_exits:
            if side == "long":
                hit = bar["h"] >= ev["px"]
            else:
                hit = bar["l"] <= ev["px"]
            if hit:
                self.partial_exits.remove(ev)
                return ev["px"], ev["reason"], ev["frac"]
        return None

    def arm_open_stop(self, history: list[dict], side: str, entry: float) -> None:
        """进程重启恢复持仓后重挂保护止损（子类按各自规则实现；基类默认不动作）。"""
        pass

    def describe(self, history: list[dict], position) -> dict:
        """看板用：策略当前状态（子类覆盖）。position 为 broker.Position。"""
        return {"pos": position.side if getattr(position, "is_open", False) else "flat"}


def ema(values: list[float], period: int) -> list[float]:
    """EMA 序列（长度与输入一致，预热期为 None 由调用方保证足够长度）。"""
    if len(values) < period:
        return []
    k = 2.0 / (period + 1.0)
    out = [sum(values[:period]) / period]
    for v in values[period:]:
        out.append(out[-1] + k * (v - out[-1]))
    return out


def calc_atr(bars: list[dict], period: int) -> Optional[float]:
    """简单平均 ATR（基于最近 period 根 K 线）。"""
    if len(bars) < period + 1:
        return None
    trs = []
    for i in range(len(bars) - period, len(bars)):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs)


def calc_sma(closes: list[float], n: int) -> Optional[float]:
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


# ---------- ma_macd：MA+MACD 两情相悦 × 1H 生命线（VC_kxs 体系机械版） ----------
H1_MS = 3_600_000
D1_MS = 86_400_000


def h1_closed_tail(history15: list[dict], max_h1: int = 150) -> list[dict]:
    """从 15m 已收盘历史尾部聚合出最多 max_h1 个已收盘 1H 桶（进行中桶丢弃）。"""
    need = max_h1 * 4 + 8
    tail = history15[-need:] if len(history15) > need else history15
    return okx_math.aggregate_closed(tail, H1_MS)[-max_h1:]


def d1_closed_tail(history15: list[dict], max_d1: int = 12) -> list[dict]:
    """从 15m 已收盘历史尾部聚合出最多 max_d1 个已收盘日线桶。"""
    need = max_d1 * 96 + 8
    tail = history15[-need:] if len(history15) > need else history15
    return okx_math.aggregate_closed(tail, D1_MS)[-max_d1:]


class MaMacd(Strategy):
    """MA+MACD 双确认 × 1H 生命线 × 四位置 × 136 分仓 × 锁利离场（机械版）。

    帖子规则 -> 代码映射（可回测解读，v2，参数可调）：
    - 周期栈：15m 已收盘 bar 决策；1H/日线 由 15m 内部聚合（h1/d1_closed_tail）
    - 两情相悦 = 同根 15m bar：MA快上穿MA慢 且 MACD DIF 上穿 DEA（做多；死叉镜像）
    - 四位置模板（enable_t1..t4 可关）：
      T1 底部反弹多：1H 收盘在生命线下、超跌带 -> 金叉做多（目标回生命线）
      T2 高位回调空：1H 收盘在生命线上、高位带 -> 死叉做空（镜像）
      T3 空中加油多：1H 收盘在生命线上、15m 回踩生命线带 -> 金叉做多（目标前高）
      T4 确认位空：  1H 收盘在生命线下、15m 反抽生命线带 -> 死叉做空（镜像）
    - 首次两情相悦只武装等第二次；二次确认须同向且未击穿首叉摆动极值（confirm=second）
    - 放量过滤（入场错开沿用帖子"放量观望"）；持仓反向放量全平默认开（vol_exit_opposite），
      依据 2026-09-06 回测证据（作者"日内放量是洗盘"需人工判别，机械版止盈式离场更优）；
      设 False 可关闭以贴近作者最新表述
    - div_filter=True 时：四位置入场前要求同方向 MACD 背离（B站教学模块：
      底背离的底部反弹 / 顶背离的高位回调）——**回测证据：15m 级别会把信号全部滤光
      （3年 0 笔），默认关**；若要用需换 1H 级别背离另行研究
    - d1_enable=True 时：日线形态离场（帖子："多次试探前高不破+日线新高针 -> 多单都走，
      布局回调空"）——v1 判定在 3y/近1y 无增益（触发放置次数极少），默认关，参数可调后另行验证
    - 136：首层 leg1(10%) 进场；持仓中再现金叉/死叉补 leg2(30%)；
      向有利方向延伸 ≥ l3_atr×ATR 补 leg3(60%)；条件不满足则放弃后续批次
    - 离场：初始 ATR 止损 -> 浮盈≥be_atr×ATR 移保本 -> chandelier 追踪；
      目标位（生命线/前高前低）部分止盈 pt_frac；顺线仓 1H 收盘反向穿越清仓
    """

    name = "ma_macd"

    def __init__(self, params: dict):
        super().__init__(params)
        f = lambda k, d: float(params.get(k, d))
        i = lambda k, d: int(params.get(k, d))
        self.ma_fast = i("ma_fast", 5)
        self.ma_slow = i("ma_slow", 10)
        self.macd_fast = i("macd_fast", 12)
        self.macd_slow = i("macd_slow", 26)
        self.macd_sig = i("macd_sig", 9)
        self.gate_ma = i("gate_ma", 60)      # 1H 生命线 MA 周期
        self.h1_max = i("h1_max", 150)
        self.sw_look = i("sw_look", 24)      # 支撑/压力/前高前低 用近 N 根 1H
        self.atr_p = i("atr_p", 14)
        self.zone_mult = f("zone_mult", 2.0)   # 位置带宽度 = atr1h × mult
        self.confirm = str(params.get("confirm", "second"))  # any | second
        self.vol_spike_mult = f("vol_spike_mult", 2.5)       # 0=关闭放量过滤
        # 持仓反向放量全平：默认开。作者 2026-09 帖称"日内放量是洗盘诱惑"，
        # 但 3y/近1y 回测证据表明机械止盈式离场更优(0.998/0.997 vs 0.976/0.995)——
        # 作者依赖人工区分洗盘与真转势，机械版无法区分，按数据保留止盈式离场
        self.vol_exit_opposite = params.get("vol_exit_opposite", True)
        self.enable_t1 = params.get("enable_t1", True)
        self.enable_t2 = params.get("enable_t2", True)
        self.enable_t3 = params.get("enable_t3", True)
        self.enable_t4 = params.get("enable_t4", True)
        # B站背离模块：入场前要求同向 MACD 背离（div_filter=True 开启）
        self.div_filter = params.get("div_filter", False)
        self.div_swing = i("div_swing", 3)      # 摆动点回看窗口（±N 根）
        self.div_win = i("div_win", 260)        # 背离扫描窗口（15m 根）
        # 日线形态（帖子：前高多次试探不破/新高针 -> 多单离场，布局回调空）
        self.d1_enable = params.get("d1_enable", False)
        self.d1_swing_days = i("d1_swing_days", 10)  # 近期日线高点的回看天数
        self.d1_tests = i("d1_tests", 2)             # 试探前高次数下限
        self.d1_tol_pct = f("d1_tol_pct", 0.002)     # "触及前高"容差（相对 X 的比例）
        self.d1_pin_mult = f("d1_pin_mult", 2.0)     # 新高针：上影 >= mult×实体
        self.d1_guard_days = i("d1_guard_days", 3)   # 空头布局态最长持续天数
        # 日线趋势偏置（帖子：日线定大方向）：0=关；>0 时按最近收盘日 vs D1 MA(N)
        # 只放行顺日线方向的位置（多头日=仅 t1/t3，空头日=仅 t2/t4）
        self.d1_bias_ma = i("d1_bias_ma", 0)
        self._d1_bias_up: Optional[bool] = None
        # 1H 结构位引擎（财道图面支撑/压力的机械化）：0=关
        # 摆动点聚类成水平位；T1/T2 要求站上 ≥level_min_touch 次的位；目标=最近结构位
        self.struct_levels = params.get("struct_levels", False)
        self.level_fractal = i("level_fractal", 2)     # 1H 摆动点 ±N 根
        self.level_min_touch = i("level_min_touch", 2)  # 有效位最少触碰次数
        self.level_tol_atr = f("level_tol_atr", 0.30)   # 聚类容差 = atr1h × N
        self._levels_cache: Optional[dict] = None       # {"supports":[(px,touches)], "resist":[...]}
        self.leg1 = f("leg1_pct", 0.10)
        self.leg2 = f("leg2_pct", 0.30)
        self.leg3 = f("leg3_pct", 0.60)
        self.l3_atr_mult = f("l3_atr_mult", 1.2)  # 主仓（第3批）确认距离
        self.stop_atr_mult = f("stop_atr_mult", 1.8)    # 初始保护止损
        self.be_atr_mult = f("be_atr_mult", 1.0)        # 浮盈达 X×ATR 移保本
        self.trail_atr_mult = f("trail_atr_mult", 1.5)  # 保本后 chandelier 距离
        self.pt_frac = f("pt_frac", 0.5)                # 目标位部分止盈比例
        self.armed_max_bars = i("armed_max_bars", 96)   # 武装存活上限（1天15m）
        self.armed_look = i("armed_look", 12)           # 武装水印回看（首推动摆动极值）
        self._reset_state()
        self._d1_guard: Optional[dict] = None           # 日线守卫跨持仓保持

    # ---- 持仓/等待期状态（on_open 复位） ----
    def _reset_state(self) -> None:
        self._armed: Optional[str] = None   # 等待二次确认的方向
        self._armed_tpl: str = ""           # 武装时的位置模板（决定失效规则）
        self._armed_swing: float = 0.0      # 首推动摆动极值（多=低/空=高），跌破即失效
        self._armed_bars: int = 0           # 武装存活 bar 数（防陈旧信号）
        self._legs: int = 0                 # 已进批次数
        self._tpl: str = ""                 # t1/t2/t3/t4/recovered
        self._hh: float = 0.0               # 持仓以来有利方向极值（多=高/空=低）
        self._be_done: bool = False
        self._recovered: bool = False       # 重启恢复仓：不再补批

    def on_open(self) -> None:
        super().on_open()
        self._reset_state()

    # ---------- B站背离模块（四位置入场过滤） ----------
    def _divergence(self, closes: list[float], side: str) -> bool:
        """MACD DIF 背离：价格创新低/新高而 DIF 反向（底背离做多/顶背离做空）。

        只在最近 div_win 根 15m 上找最近两个摆动极值（±div_swing 窗口），
        数据不足或形状不满足返回 False（中性，不构成拦截）。
        """
        n = min(len(closes), self.div_win)
        if n < self.macd_slow + self.macd_sig + 40:
            return False
        tail = closes[-n:]
        ef = ema(tail, self.macd_fast)
        es = ema(tail, self.macd_slow)
        off = self.macd_slow - self.macd_fast
        sw = self.div_swing

        def dif_at(i: int) -> float:  # closes 全局下标 -> DIF（es 下标对齐）
            return ef[i - self.macd_slow + 1 + off] - es[i - self.macd_slow + 1]

        if side == "long":  # 两个摆动低点：价格更低 + DIF 更高
            lows = []
            for i in range(sw, n - sw):
                if tail[i] <= min(tail[i - sw:i + sw + 1]):
                    lows.append((i, tail[i]))
            if len(lows) < 2:
                return False
            a, b = lows[-2], lows[-1]
            if b[0] < self.macd_slow - 1 or a[0] < self.macd_slow - 1:
                return False
            return b[1] < a[1] and dif_at(b[0]) > dif_at(a[0])
        highs = []
        for i in range(sw, n - sw):
            if tail[i] >= max(tail[i - sw:i + sw + 1]):
                highs.append((i, tail[i]))
        if len(highs) < 2:
            return False
        a, b = highs[-2], highs[-1]
        if b[0] < self.macd_slow - 1 or a[0] < self.macd_slow - 1:
            return False
        return b[1] > a[1] and dif_at(b[0]) < dif_at(a[0])

    # ---------- 日线形态（前高受阻 -> 多单离场，布局回调空） ----------
    def _update_d1_guard(self, history: list[dict]) -> None:
        if not self.d1_enable:
            return
        d1 = d1_closed_tail(history, self.d1_swing_days + 2)
        if len(d1) < self.d1_swing_days + 1:
            return  # 日线数据不足：维持现状（中性）
        last = d1[-1]
        # 前高 = 最近 swing_days 个"已收盘日"的最高价（不含今日）
        win = [b for b in d1 if b["ts"] <= last["ts"] - D1_MS][-self.d1_swing_days:]
        if len(win) < 2:
            return
        x = max(b["h"] for b in win)
        tol = x * self.d1_tol_pct
        touches = sum(1 for b in win if b["h"] >= x - tol and b["c"] < x)
        body = abs(last["c"] - last["o"])
        upper_wick = last["h"] - max(last["o"], last["c"])
        pin = body > 0 and upper_wick >= self.d1_pin_mult * body \
            and last["c"] < (last["h"] + last["l"]) / 2 \
            and last["h"] >= x * (1.0 - self.d1_tol_pct)  # 针出现在前高附近才有意义
        if self._d1_guard is None and (touches >= self.d1_tests or pin) \
                and last["c"] < x:
            self._d1_guard = {"x": x, "ts": last["ts"]}
            return
        if self._d1_guard is not None:
            if last["c"] > self._d1_guard["x"]:       # 日线收盘突破 -> 解除
                self._d1_guard = None
            elif last["ts"] - self._d1_guard["ts"] >= self.d1_guard_days * D1_MS:
                self._d1_guard = None                 # 超时解除

    def _update_d1_bias(self, history: list[dict]) -> None:
        """日线趋势偏置：最近收盘日 vs D1 MA(N)；数据不足置 None（中性）。"""
        if self.d1_bias_ma <= 0:
            return
        d1 = d1_closed_tail(history, self.d1_bias_ma + 2)
        if len(d1) < self.d1_bias_ma + 1:
            self._d1_bias_up = None
            return
        closes = [b["c"] for b in d1]
        ma = calc_sma(closes, self.d1_bias_ma)
        if ma is None:
            self._d1_bias_up = None
            return
        self._d1_bias_up = closes[-1] >= ma

    def _bias_ok(self, side: str) -> bool:
        """顺日线方向检查：多头日禁空、空头日禁多；bias 关闭/数据不足=放行。"""
        if self._d1_bias_up is None:
            return True
        return self._d1_bias_up if side == "long" else not self._d1_bias_up

    def _long_blocked(self) -> bool:
        return self._d1_guard is not None

    # ---------- 1H 结构位引擎（财道图面支撑/压力的机械化） ----------
    def _structure_levels(self, h1: list[dict]) -> dict:
        """1H 摆动点(±k 根)聚类成水平位；触碰次数不足 level_min_touch 的位不算有效。

        返回 {"ts": 最后1H ts, "supports": [(px, touches)...], "resist": [...]}
        按 ts 缓存（1H 未收盘前不重算）。
        """
        if not self.struct_levels:
            return {}
        key = h1[-1]["ts"]
        cache = getattr(self, "_levels_cache", None)
        if cache and cache.get("ts") == key:
            return cache
        k = self.level_fractal
        n = len(h1)
        lows, highs = [], []
        if n >= 2 * k + 3:
            for i in range(k, n - k):
                win = h1[i - k:i + k + 1]
                if h1[i]["l"] <= min(b["l"] for b in win):
                    lows.append(h1[i]["l"])
                if h1[i]["h"] >= max(b["h"] for b in win):
                    highs.append(h1[i]["h"])
        atr1 = calc_atr(h1, self.atr_p) or 0.0
        tol = max(atr1 * self.level_tol_atr, h1[-1]["c"] * 0.0008)

        def cluster(vals: list[float]) -> list[tuple[float, int]]:
            out: list[list] = []
            for v in sorted(vals):
                if out and v - out[-1][0] <= tol:
                    px, cnt = out[-1]
                    out[-1] = [(px * cnt + v) / (cnt + 1), cnt + 1]
                else:
                    out.append([v, 1])
            return [(px, cnt) for px, cnt in out
                    if cnt >= self.level_min_touch]

        self._levels_cache = {"ts": key,
                              "supports": cluster(lows), "resist": cluster(highs)}
        return self._levels_cache

    @staticmethod
    def _nearest_support(px: float, levels: dict) -> Optional[float]:
        below = [s[0] for s in levels.get("supports", []) if s[0] <= px * 1.0008]
        return max(below) if below else None

    @staticmethod
    def _nearest_resist(px: float, levels: dict) -> Optional[float]:
        above = [s[0] for s in levels.get("resist", []) if s[0] >= px * 0.9992]
        return min(above) if above else None

    # ---------- 指标工具 ----------
    def _smacross(self, closes: list[float]) -> tuple[bool, bool]:
        """15m MA 金叉/死叉（与上一根比较）。"""
        if len(closes) < self.ma_slow + 2:
            return False, False
        nf, ns = self.ma_fast, self.ma_slow
        fp = sum(closes[-(nf + 1):-1]) / nf
        fc = sum(closes[-nf:]) / nf
        sp = sum(closes[-(ns + 1):-1]) / ns
        sc = sum(closes[-ns:]) / ns
        return fc > sc and fp <= sp, fc < sc and fp >= sp

    def _macdcross(self, closes: list[float]) -> tuple[bool, bool]:
        """15m MACD DIF/DEA 金叉/死叉。"""
        need = self.macd_slow + self.macd_sig + 2
        if len(closes) < need:
            return False, False
        ef = ema(closes, self.macd_fast)
        es = ema(closes, self.macd_slow)
        n = len(es)
        dif = [ef[i + (self.macd_slow - self.macd_fast)] - es[i] for i in range(n)]
        dea = ema(dif, self.macd_sig)
        if len(dea) < 2:
            return False, False
        up = dif[-1] > dea[-1] and dif[-2] <= dea[-2]
        down = dif[-1] < dea[-1] and dif[-2] >= dea[-2]
        return up, down

    def _vol_spike(self, history: list[dict], bar: dict) -> bool:
        mult = self.vol_spike_mult
        if mult <= 0:
            return False
        tail = [b["v"] for b in history[-21:-1]]
        if len(tail) < 20:
            return False
        avg = sum(tail) / len(tail)
        return avg > 0 and bar["v"] > avg * mult

    # ---------- 1H 结构 ----------
    def _context(self, history: list[dict]):
        """(h1, closes15, line, atr1, support, resistance)；预热不足返回 None。

        closes15 只需覆盖 15m MACD 预热窗口（slow+sig+2≈37），
        强制有界尾部，避免每根 bar 全量扫描历史（回测 3 年 10 万根 bar 性能关键）。
        """
        h1 = h1_closed_tail(history, self.h1_max)
        if len(h1) < self.gate_ma + 3:
            return None
        need = self.macd_slow + self.macd_sig + 2
        closes = [b["c"] for b in history[-need:]]
        if len(closes) < need:
            return None
        line = calc_sma([b["c"] for b in h1], self.gate_ma)
        atr1 = calc_atr(h1, self.atr_p)
        if line is None or atr1 is None or atr1 <= 0:
            return None
        sw = h1[-self.sw_look:]
        support = min(b["l"] for b in sw)
        resistance = max(b["h"] for b in sw)
        return h1, closes, line, atr1, support, resistance

    def _zone(self, atr1: float, px: float) -> float:
        z = atr1 * self.zone_mult
        return z if z > px * 0.001 else px * 0.002

    def _template(self, c: float, h1last: float, line: float, support: float,
                  resistance: float, zone: float) -> str:
        """当前 15m 收盘价命中的位置模板（空串 = 不在任何位置，不交易）。

        按"距生命线远近 × 方向"做无歧义四分区（帖子四位置的可执行版本）：
        生命线下方：1.2×zone 内为反抽带(t4 死叉做空)，更深为超跌带(t1 金叉做多)；
        生命线上方：1.2×zone 内为回踩带(t3 金叉做多)，更高为高位带(t2 死叉做空)。
        交叉方向与带不匹配时不交易（如 t2 带里的金叉 = 追高，排除）。
        """
        if h1last < line:
            if self.enable_t1 and c <= line - zone * 1.2:
                return "t1"
            if self.enable_t4 and line - zone * 1.2 < c <= line + zone * 0.5:
                return "t4"
        else:
            if self.enable_t3 and line - zone * 0.5 <= c <= line + zone * 1.2:
                return "t3"
            if self.enable_t2 and c > line + zone * 1.2:
                return "t2"
        return ""

    @staticmethod
    def _long_target(entry: float, line: float, resistance: float) -> Optional[float]:
        cands = [x for x in (line, resistance) if x and x > entry * 1.0005]
        return min(cands) if cands else None

    @staticmethod
    def _short_target(entry: float, line: float, support: float) -> Optional[float]:
        cands = [x for x in (line, support) if x and 0 < x < entry * 0.9995]
        return max(cands) if cands else None

    # ---------- 主入口 ----------
    def on_bar(self, bar: dict, ctx) -> Signal:
        if self.d1_bias_ma > 0:
            self._update_d1_bias(ctx.history)
        self._update_d1_guard(ctx.history)
        ctx0 = self._context(ctx.history)
        if ctx0 is None:
            return Signal("none", "预热中(等1H)")
        h1, closes, line, atr1, support, resistance = ctx0
        if ctx.position.is_open:
            return self._manage(ctx.position, bar, ctx.history, h1, closes, line, atr1)
        return self._hunt(bar, ctx.history, closes, h1, line, atr1, support, resistance)

    def _hunt(self, bar, history, closes, h1, line, atr1, support, resistance) -> Signal:
        zone = self._zone(atr1, bar["c"])
        c = bar["c"]
        h1last = h1[-1]["c"]
        ma_up, ma_down = self._smacross(closes)
        macd_up, macd_down = self._macdcross(closes)
        gx = ma_up and macd_up          # 两情相悦金叉
        dx = ma_down and macd_down      # 两情相悦死叉
        spike = self._vol_spike(history, bar)

        # 武装失效（帖子语义：等"第二次同向"而非每次回落都重来）：
        # 中间小反向交叉不断言失败，只有 结构破坏 / 生命线翻向不利侧 / 超时 才失效
        if self._armed:
            self._armed_bars += 1
            dead = self._armed_bars > self.armed_max_bars
            if self._armed == "long":
                if self._armed_tpl == "t1":       # 底部反弹：涨到生命线=目标达成
                    dead = dead or h1last >= line
                else:                             # t3 空中加油：跌破生命线=方向证伪
                    dead = dead or h1last < line
                dead = dead or bar["l"] < self._armed_swing
            else:
                if self._armed_tpl == "t2":
                    dead = dead or h1last <= line
                else:                             # t4
                    dead = dead or h1last > line
                dead = dead or bar["h"] > self._armed_swing
            if dead:
                self._armed = None

        tpl = self._template(c, h1last, line, support, resistance, zone)
        # 1H 结构位引擎：T1 须贴近有效支撑、T2 须贴近有效压力（财道"只在支撑压力位交易"）
        if self.struct_levels:
            lv = self._structure_levels(h1)
            if tpl == "t1":
                sup = self._nearest_support(c, lv)
                if sup is None or c - sup > zone:
                    tpl = ""
            elif tpl == "t2":
                res = self._nearest_resist(c, lv)
                if res is None or res - c > zone:
                    tpl = ""
            s_res = self._nearest_resist(c, lv) or resistance
            s_sup = self._nearest_support(c, lv) or support
        else:
            s_res, s_sup = resistance, support
        # 日线前高受阻态：封锁多头模板（帖子"多单都走，布局回调空"）
        if self._d1_guard is not None and tpl in ("t1", "t3"):
            tpl = ""
            if self._armed == "long":
                self._armed = None

        def _div_ok(side: str) -> bool:
            """背离过滤（默认关）；开启时入场需同向 MACD 背离确认。"""
            if not self.div_filter:
                return True
            tail = [b["c"] for b in history[-self.div_win:]]
            return self._divergence(tail, side)

        if self.confirm == "second":
            # 二次确认进场：同向交叉 + 已武装。位置在"首叉"时定（武装需在模板带内），
            # 二次交叉允许价格已离开原带（帖子语境=在位置上等第二脚）；
            # 失效由 摆动击穿/生命线翻侧/超时 过滤，不因离带而过早放弃
            if gx and not spike and self._armed == "long" and _div_ok("long") \
                    and self._bias_ok("long"):
                epl = tpl if tpl in ("t1", "t3") else ("t3" if h1last >= line else "t1")
                return self._open("long", epl, bar, line, s_sup, s_res, atr1)
            if dx and not spike and self._armed == "short" and _div_ok("short") \
                    and self._bias_ok("short"):
                epl = tpl if tpl in ("t2", "t4") else ("t4" if h1last <= line else "t2")
                return self._open("short", epl, bar, line, s_sup, s_res, atr1)
            # 首次信号 -> 只武装（不重复覆盖既有武装）
            if self._armed is None and gx and not spike and tpl in ("t1", "t3") \
                    and _div_ok("long") and self._bias_ok("long"):
                self._armed, self._armed_tpl = "long", tpl
                window = history[-self.armed_look:]
                self._armed_swing = min(b["l"] for b in window)
                self._armed_bars = 0
            elif self._armed is None and dx and not spike and tpl in ("t2", "t4") \
                    and _div_ok("short") and self._bias_ok("short"):
                self._armed, self._armed_tpl = "short", tpl
                window = history[-self.armed_look:]
                self._armed_swing = max(b["h"] for b in window)
                self._armed_bars = 0
            return Signal("none")
        # confirm=any：模板内两情相悦直接进
        if gx and not spike and tpl in ("t1", "t3") and _div_ok("long") \
                and self._bias_ok("long"):
            return self._open("long", tpl, bar, line, s_sup, s_res, atr1)
        if dx and not spike and tpl in ("t2", "t4") and _div_ok("short") \
                and self._bias_ok("short"):
            return self._open("short", tpl, bar, line, s_sup, s_res, atr1)
        return Signal("none")

    def _open(self, side: str, tpl: str, bar, line, support, resistance,
              atr1: float) -> Signal:
        entry = bar["c"]
        self._reset_state()
        self._legs = 1
        self._tpl = tpl
        if side == "long":
            self.sl_px = entry - atr1 * self.stop_atr_mult
            self._hh = bar["h"]
            tgt = self._long_target(entry, line, resistance)
            if tgt is not None:
                self.partial_exits = [{"px": tgt, "frac": self.pt_frac,
                                       "reason": "目标位部分止盈"}]
            note = f"{tpl.upper()}金叉进场做多 生命线{line:.0f}" + \
                   (f" 目标{tgt:.0f}" if tgt else "")
            return Signal("open_long", note, frac=self.leg1)
        self.sl_px = entry + atr1 * self.stop_atr_mult
        self._hh = bar["l"]
        tgt = self._short_target(entry, line, support)
        if tgt is not None:
            self.partial_exits = [{"px": tgt, "frac": self.pt_frac,
                                   "reason": "目标位部分止盈"}]
        note = f"{tpl.upper()}死叉进场做空 生命线{line:.0f}" + \
               (f" 目标{tgt:.0f}" if tgt else "")
        return Signal("open_short", note, frac=self.leg1)

    def _manage(self, pos, bar, history, h1, closes, line, atr1: float) -> Signal:
        """持仓管理：保本/追踪移动止损、日线形态离场、顺线反向清仓、按批补仓。"""
        c, entry = bar["c"], pos.entry
        h1last = h1[-1]["c"]
        ma_up, ma_down = self._smacross(closes)
        macd_up, macd_down = self._macdcross(closes)
        spike = self._vol_spike(history, bar)

        if pos.side == "long":
            self._hh = max(self._hh, bar["h"])
            if not self._be_done and self.sl_px is not None and \
                    c - entry >= self.be_atr_mult * atr1:
                self.sl_px = max(self.sl_px, entry)
                self._be_done = True
            if self._be_done and self.sl_px is not None:
                cand = self._hh - self.trail_atr_mult * atr1
                if cand > self.sl_px:
                    self.sl_px = cand
            # 日线前高受阻/新高针（帖子：多单都走，布局回调空）——优先于其他离场
            if self._d1_guard is not None:
                return Signal("close", "日线前高受阻：多单离场，等回调空")
            # 日线趋势偏置翻转（多头日 -> 空头日）：顺大势原则离场
            if self.d1_bias_ma > 0 and self._d1_bias_up is False:
                return Signal("close", "日线转空：多单离场")
            # 持仓反向放量全平为可选旧口径（默认关：日内放量多为洗盘诱惑，勿被搞破防）
            if self.vol_exit_opposite and spike and bar["l"] <= c - atr1 * 0.5:
                return Signal("close", "放量大反向直接止盈")
            # 顺线仓（t3）：1H 收盘跌破生命线 -> 高周期反向清仓
            if self._tpl == "t3" and h1last < line:
                return Signal("close", "高周期反向：1H收盘跌破生命线清多")
            if self._legs == 1 and not self._recovered and not spike \
                    and ma_up and macd_up:
                return Signal("add_long", "再现金叉二次确认补仓", frac=self.leg2)
            if self._legs == 2 and not self._recovered and not spike \
                    and c - entry >= self.l3_atr_mult * atr1:
                return Signal("add_long", "趋势延伸确认补主仓", frac=self.leg3)
        else:
            self._hh = min(self._hh, bar["l"])
            if not self._be_done and self.sl_px is not None and \
                    entry - c >= self.be_atr_mult * atr1:
                self.sl_px = min(self.sl_px, entry)
                self._be_done = True
            if self._be_done and self.sl_px is not None:
                cand = self._hh + self.trail_atr_mult * atr1
                if cand < self.sl_px:
                    self.sl_px = cand
            if self.d1_bias_ma > 0 and self._d1_bias_up is True:
                return Signal("close", "日线转多：空单离场")
            if self.vol_exit_opposite and spike and bar["h"] >= c + atr1 * 0.5:
                return Signal("close", "放量大反向直接止盈")
            if self._tpl == "t4" and h1last > line:
                return Signal("close", "高周期反向：1H收盘升破生命线清空")
            if self._legs == 1 and not self._recovered and not spike \
                    and ma_down and macd_down:
                return Signal("add_short", "再现死叉二次确认补仓", frac=self.leg2)
            if self._legs == 2 and not self._recovered and not spike \
                    and entry - c >= self.l3_atr_mult * atr1:
                return Signal("add_short", "趋势延伸确认补主仓", frac=self.leg3)
        return Signal("none")

    def arm_open_stop(self, history: list[dict], side: str, entry: float) -> None:
        """重启恢复持仓：ATR 保护止损；恢复仓不补批、不带目标事件。"""
        h1 = h1_closed_tail(history, self.h1_max)
        atr1 = calc_atr(h1, self.atr_p) or entry * 0.005
        self.sl_px = entry - atr1 * self.stop_atr_mult if side == "long" \
            else entry + atr1 * self.stop_atr_mult
        self.tp_px = None
        self.partial_exits = []
        self._reset_state()
        self._recovered = True
        self._tpl = "recovered"
        self._hh = entry
        self._d1_guard = None
        if self.d1_enable:  # 恢复长仓也尊重日线形态（若已前高受阻则会被 manage 离场）
            self._update_d1_guard(history)

    def describe(self, history: list[dict], position) -> dict:
        if getattr(position, "is_open", False):
            return {"pos": position.side, "legs": self._legs}
        ctx0 = self._context(history)
        if ctx0 is None:
            return {"pos": "flat", "note": "预热中（等 1H MA60 数据足够）"}
        h1, _c, line, atr1, support, resistance = ctx0
        above = h1[-1]["c"] >= line
        armed_txt = {"long": "（首金叉已现，等二次确认做多）",
                     "short": "（首死叉已现，等二次确认做空）"}.get(self._armed, "")
        return {"pos": "flat",
                "note": f"1H生命线 {line:.1f}，现价在其{'上方' if above else '下方'}"
                        f" · 支撑 {support:.1f} 压力 {resistance:.1f}"
                        f" · 等两情相悦信号{armed_txt}"}




class Macd1h(Strategy):
    """1H 两情相悦交叉定趋势、15m 执行、陪跑式离场（财道"趋势单"语义，v6 实验）。

    与 ma_macd（15m 信号 × 1H 生命线）正交：这里是 **1H 信号 × 持仓陪跑**——
    - 每收盘一个 1H 桶：MA5/MA10 + MACD(12,26,9) 同根双交叉（两情相悦）
    - 金叉 -> 趋势多（首次交叉收盘确认后进场，不再等 15m 二次信号）；
      死叉 -> 清多（反向交叉即离场，无"减仓"档 v1）
    - 进场即带 ATR 保护止损；浮盈 ≥be_atr×ATR 移保本；随后按 1H 高点 chandelier
      只朝有利方向追踪（让利润陪跑）
    - 同一方向重复交叉不重复进场（单笔趋势内不加仓，v1）
    预期：频率≈1H 双交叉频次（0.5~2/日），持仓小时-天级，摩擦相对 ATR 占比低。
    """

    name = "macd_1h"

    def __init__(self, params: dict):
        super().__init__(params)
        f = lambda k, d: float(params.get(k, d))
        i = lambda k, d: int(params.get(k, d))
        self.ma_fast = i("ma_fast", 5)
        self.ma_slow = i("ma_slow", 10)
        self.macd_fast = i("macd_fast", 12)
        self.macd_slow = i("macd_slow", 26)
        self.macd_sig = i("macd_sig", 9)
        self.atr_p = i("atr_p", 14)
        self.stop_atr_mult = f("stop_atr_mult", 1.8)
        self.be_atr_mult = f("be_atr_mult", 1.0)
        self.trail_atr_mult = f("trail_atr_mult", 2.0)
        self._h1_ts: Optional[float] = None   # 已处理的最后 1H 桶起点
        self._trend: Optional[str] = None     # long/short（最近 1H 双交叉方向）
        self._hh: float = 0.0                 # 持仓以来有利方向极值（含当前 15m）
        self._atr1: float = 0.0
        self._be_done: bool = False

    def on_open(self) -> None:
        super().on_open()
        self._trend = None
        self._hh = 0.0
        self._be_done = False

    # ---------- 1H 两情相悦 ----------
    def _h1_love_cross(self, closes1h: list[float]) -> tuple[bool, bool]:
        """最新闭合 1H 桶上是否出现 金叉/死叉（两情相悦）。"""
        n = len(closes1h)
        if n < self.macd_slow + self.macd_sig + 2:
            return False, False
        nf, ns = self.ma_fast, self.ma_slow
        fp = sum(closes1h[-(nf + 1):-1]) / nf
        fc = sum(closes1h[-nf:]) / nf
        sp = sum(closes1h[-(ns + 1):-1]) / ns
        sc = sum(closes1h[-ns:]) / ns
        ma_up, ma_down = fc > sc and fp <= sp, fc < sc and fp >= sp
        ef = ema(closes1h, self.macd_fast)
        es = ema(closes1h, self.macd_slow)
        off = self.macd_slow - self.macd_fast
        dif = [ef[i + off] - es[i] for i in range(len(es))]
        dea = ema(dif, self.macd_sig)
        if len(dea) < 2:
            return False, False
        up = ma_up and dif[-1] > dea[-1] and dif[-2] <= dea[-2]
        down = ma_down and dif[-1] < dea[-1] and dif[-2] >= dea[-2]
        return up, down

    def on_bar(self, bar: dict, ctx) -> Signal:
        h1 = h1_closed_tail(history15=ctx.history, max_h1=90)
        need = self.macd_slow + self.macd_sig + 2
        if len(h1) < need:
            return Signal("none", "预热中(等1H)")
        fresh = h1[-1]["ts"] != self._h1_ts
        if fresh:
            self._h1_ts = h1[-1]["ts"]
            atr1 = calc_atr(h1, self.atr_p)
            if atr1 is not None and atr1 > 0:
                self._atr1 = atr1
            closes1h = [b["c"] for b in h1]
            up, down = self._h1_love_cross(closes1h)
            if up:
                self._trend = "long"
            elif down:
                self._trend = "short"
            if ctx.position.is_open:
                # 持仓中：反向 1H 双交叉 -> 清仓（趋势单结束；下一同向交叉自然重开）
                if ctx.position.side == "long" and down:
                    return Signal("close", "1H 死叉清多")
                if ctx.position.side == "short" and up:
                    return Signal("close", "1H 金叉清空")
            elif up:
                self._be_done = False
                self._hh = bar["h"]
                self.sl_px = bar["c"] - self._atr1 * self.stop_atr_mult
                return Signal("open_long", f"1H 两情相悦金叉 开多 @{bar['c']:.0f}")
            elif down:
                self._be_done = False
                self._hh = bar["l"]
                self.sl_px = bar["c"] + self._atr1 * self.stop_atr_mult
                return Signal("open_short", f"1H 两情相悦死叉 开空 @{bar['c']:.0f}")
        # 持仓管理（15m 级）：保本 + chandelier 追踪
        if ctx.position.is_open:
            c, entry = bar["c"], ctx.position.entry
            if ctx.position.side == "long":
                self._hh = max(self._hh, bar["h"])
                if not self._be_done and self.sl_px is not None and \
                        c - entry >= self.be_atr_mult * self._atr1:
                    self.sl_px = max(self.sl_px, entry)
                    self._be_done = True
                if self._be_done and self.sl_px is not None:
                    cand = self._hh - self.trail_atr_mult * self._atr1
                    if cand > self.sl_px:
                        self.sl_px = cand
            else:
                self._hh = min(self._hh, bar["l"])
                if not self._be_done and self.sl_px is not None and \
                        entry - c >= self.be_atr_mult * self._atr1:
                    self.sl_px = min(self.sl_px, entry)
                    self._be_done = True
                if self._be_done and self.sl_px is not None:
                    cand = self._hh + self.trail_atr_mult * self._atr1
                    if cand < self.sl_px:
                        self.sl_px = cand
        return Signal("none")

    def arm_open_stop(self, history: list[dict], side: str, entry: float) -> None:
        h1 = h1_closed_tail(history15=history, max_h1=90)
        atr1 = calc_atr(h1, self.atr_p) or entry * 0.005
        self.sl_px = entry - atr1 * self.stop_atr_mult if side == "long" \
            else entry + atr1 * self.stop_atr_mult
        self._atr1 = atr1
        self._be_done = False
        self._trend = side

    def describe(self, history: list[dict], position) -> dict:
        if getattr(position, "is_open", False):
            return {"pos": position.side, "legs": 1}
        h1 = h1_closed_tail(history15=history, max_h1=90)
        if len(h1) < self.macd_slow + self.macd_sig + 2:
            return {"pos": "flat", "note": "预热中（等 1H 两情相悦数据足够）"}
        trend_txt = {"long": "多头趋势中(金叉后)等反手信号",
                     "short": "空头趋势中(死叉后)等反手信号"}.get(self._trend, "空仓等 1H 两情相悦")
        return {"pos": "flat", "note": f"1H 趋势：{trend_txt}"}
