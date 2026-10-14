# -*- coding: utf-8 -*-
"""
backtest.py —— 规则回测验证（对比“优化版”与“原始版”）
-----------------------------------------------------
用日线历史数据，按“信号 → 次日挂单成交 → 止损/止盈/时间止损”的顺序模拟，
输出交易笔数、胜率、总盈亏、盈亏比、最大回撤等指标，用于验证规则优化是否为正。

注意：仅用日线高低价近似盘中路径，无法还原日内先后顺序，结果为近似值；
      两个版本使用同一套成交假设，因此二者的“相对对比”仍有参考意义。

运行： python backtest.py
"""
from __future__ import annotations

import copy
import math

from common import build_signal, fetch_daily, load_config


def simulate(cfg, product, bars):
    f = cfg["filters"]
    ex = cfg["exit"]
    p = cfg["products"][product]
    mult = float(p["multiplier"])
    tick = float(p["tick"])
    comm = float(p["commission"])
    slip = float(ex["slippage_ticks"]) * tick

    n = len(bars)
    warm = max(f["ma_period"], f["atr_period"], f["ma60_period"]) + f["ma_slope_lookback"] + 5
    trades = []
    i = warm
    while i < n - 1:
        sig, _reason = build_signal(cfg, product, bars[:i + 1])
        if not sig or sig["lots"] < 1:
            i += 1
            continue
        d = sig["direction"]
        entry, stop, tp1, tp2, lots = sig["entry"], sig["stop"], sig["tp1"], sig["tp2"], sig["lots"]
        nb = bars[i + 1]

        # 次日挂单成交（缺口按开盘价成交，否则按挂单价）
        if d == "long":
            if nb["open"] >= entry:
                fill = nb["open"]
            elif nb["high"] >= entry:
                fill = entry
            else:
                i += 1
                continue
        else:
            if nb["open"] <= entry:
                fill = nb["open"]
            elif nb["low"] <= entry:
                fill = entry
            else:
                i += 1
                continue

        j = i + 1
        remaining = lots
        realized_pts = 0.0
        cur_stop = stop
        tp1_done = False
        exit_k, exit_reason, exit_price = n - 1, "eod", bars[-1]["close"]

        for k in range(j, n):
            b = bars[k]
            hi, lo = b["high"], b["low"]
            if d == "long":
                stop_hit, tp2_hit, tp1_hit = lo <= cur_stop, hi >= tp2, hi >= tp1
            else:
                stop_hit, tp2_hit, tp1_hit = hi >= cur_stop, lo <= tp2, lo <= tp1

            if stop_hit:
                realized_pts += ((cur_stop - fill) if d == "long" else (fill - cur_stop)) * remaining
                remaining = 0
                exit_k, exit_reason, exit_price = k, "stop", cur_stop
                break
            if tp2_hit:
                realized_pts += ((tp2 - fill) if d == "long" else (fill - tp2)) * remaining
                remaining = 0
                exit_k, exit_reason, exit_price = k, "tp2", tp2
                break
            if tp1_hit and not tp1_done:
                tp1_done = True
                if remaining > 1:
                    close = max(1, min(int(math.floor(remaining * float(ex["tp1_close_ratio"]))),
                                       remaining - 1))
                    realized_pts += ((tp1 - fill) if d == "long" else (fill - tp1)) * close
                    remaining -= close
                cur_stop = fill  # 止损上移保本
            if (k - j) >= int(ex["max_hold_days"]):
                realized_pts += ((b["close"] - fill) if d == "long" else (fill - b["close"])) * remaining
                remaining = 0
                exit_k, exit_reason, exit_price = k, "time", b["close"]
                break

        if remaining > 0:
            realized_pts += ((exit_price - fill) if d == "long" else (fill - exit_price)) * remaining

        gross = realized_pts * mult
        cost = lots * (2 * comm) + lots * (slip * mult)
        trades.append({
            "product": product, "dir": d, "entry_date": nb["date"],
            "exit_date": bars[exit_k]["date"], "reason": exit_reason,
            "days": exit_k - j, "pnl": round(gross - cost, 1),
        })
        i = exit_k + 1  # 平仓后才寻找下一次信号，避免重叠持仓
    return trades


def stats(trades):
    if not trades:
        return None
    wins = [t for t in trades if t["pnl"] > 0]
    total = sum(t["pnl"] for t in trades)
    gp = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gl = abs(sum(t["pnl"] for t in trades if t["pnl"] <= 0))
    eq = peak = mdd = 0.0
    for t in trades:
        eq += t["pnl"]
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)
    return {
        "n": len(trades),
        "win_rate": len(wins) / len(trades),
        "total": round(total, 1),
        "pf": round(gp / gl, 2) if gl > 0 else float("inf"),
        "mdd": round(mdd, 1),
        "avg": round(total / len(trades), 1),
        "avg_days": round(sum(t["days"] for t in trades) / len(trades), 1),
    }


def baseline_cfg(cfg):
    """还原“原始规则”：止损 1.2ATR、ATR门槛 0.8、实体 0.6%、无收盘位置/量能分位过滤、持仓 3 天。"""
    base = copy.deepcopy(cfg)
    f = base["filters"]
    f["require_ma_slope"] = False
    f["require_ma60"] = False
    f["vol_pct_rank_min"] = 0.0
    f["body_atr_ratio"] = 0.0
    f["body_min_pct"] = 0.006
    f["atr_mult"] = 0.8
    f["close_pos_min_long"] = 0.0
    f["close_pos_max_short"] = 1.0
    base["exit"]["max_hold_days"] = 3
    base["exit"]["stop_atr_mult"] = 1.2
    base["exit"]["tp1_atr_mult"] = 1.5
    base["exit"]["tp2_atr_mult"] = 2.5
    return base


def show(label, trades_by_product):
    all_tr = []
    print("\n=== {} ===".format(label))
    print("| 品种 | 笔数 | 胜率 | 总盈亏 | 盈亏比 | 最大回撤 | 均持仓天 |")
    print("| --- | --- | --- | --- | --- | --- | --- |")
    for prod, tr in trades_by_product.items():
        s = stats(tr)
        all_tr += tr
        if s:
            print("| {} | {} | {:.0%} | {} | {} | {} | {} |".format(
                prod, s["n"], s["win_rate"], s["total"], s["pf"], s["mdd"], s["avg_days"]))
        else:
            print("| {} | 0 | - | - | - | - | - |".format(prod))
    s = stats(all_tr)
    if s:
        print("| **合计** | {} | {:.0%} | **{}** | {} | {} | {} |".format(
            s["n"], s["win_rate"], s["total"], s["pf"], s["mdd"], s["avg_days"]))
    return s


def main():
    cfg = load_config()
    products = list(cfg["products"].keys())
    print("加载历史数据（约 900 个交易日）...")
    data = {}
    for p in products:
        data[p] = fetch_daily(cfg["products"][p]["sina"], 900)
        print("  {} {} 根K线：{} ~ {}".format(
            cfg["products"][p]["name"], len(data[p]), data[p][0]["date"], data[p][-1]["date"]))

    opt = {p: simulate(cfg, p, data[p]) for p in products}
    base = {p: simulate(baseline_cfg(cfg), p, data[p]) for p in products}

    show("优化版（止损1.5ATR + ATR波动率门槛1.0 + 实体0.8% + 量能分位0.75）", opt)
    show("原始版（止损1.2ATR + ATR门槛0.8 + 实体0.6%，无额外过滤）", base)


if __name__ == "__main__":
    main()
