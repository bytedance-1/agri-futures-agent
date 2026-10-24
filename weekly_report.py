# -*- coding: utf-8 -*-
"""
weekly_report.py —— 每周盘后复盘报告
------------------------------------
运行时机：周六 00:00（北京时间），由 TRAE 定时任务触发。
内容：
  1. 上周真实盈亏统计（closed_trades.json 中 close_date 落在上周的条目）
  2. 上周信号复盘（created_date 落在上周的新信号，看后续走势方向命中率）
  3. 当前持仓/挂单汇总
  4. 历史累计 PnL 与组合收益率

数据源：position.json + closed_trades.json + 新浪日线（用于信号复盘的后续走势）。
"""
from __future__ import annotations


# ---- BOOTSTRAP：确保无论从哪个目录执行，都能找到 common.py 和 json 文件 ----
import os as _os, sys as _sys
_HERE = _os.path.dirname(_os.path.abspath(__file__))
if _HERE not in _sys.path:
    _sys.path.insert(0, _HERE)
_os.chdir(_HERE)
# --------------------------------------------------------------------------
import datetime as dt

from common import (CLOSED_TRADES_PATH, POSITION_PATH, beijing_now, fetch_daily,
                    load_closed_trades, load_config, load_json, push, week_of)


EXIT_CN = {
    "stop": "止损", "tp2": "止盈2", "time": "到期",
    "pre_flatten": "节前强平", "pre_flatten_cancel": "节前撤单", "expired": "挂单失效",
}


def dir_cn(d):
    return "做多" if d == "long" else "做空"


def last_complete_week(today):
    """返回「上一个完整周」的起止日期：(周一, 周日)。周一 08:30 跑周报时，上一个完整周是前 7 天内的 Mon-Sun。"""
    this_week_mon = week_of(today)
    last_week_mon = this_week_mon - dt.timedelta(days=7)
    last_week_sun = last_week_mon + dt.timedelta(days=6)
    return last_week_mon, last_week_sun


def pct(n, d):
    return "{:.0%}".format(n / d) if d else "-"


def stats(items, key):
    """按条件过滤后的统计：笔数 / 胜率 / 总盈亏 / 均值。"""
    n = len(items)
    if not n:
        return None
    wins = [x for x in items if x.get(key, 0) > 0]
    total = sum(x.get(key, 0) for x in items)
    return {
        "n": n, "wins": len(wins),
        "win_rate": len(wins) / n,
        "total": round(total, 1),
        "avg": round(total / n, 1),
    }


def signal_followup(cfg, product, bars_after_signal, direction):
    """信号后走势方向判断。bars_after_signal 是信号当日及之后的日线列表（由旧到新）。"""
    if len(bars_after_signal) < 2:
        return None
    entry_close = bars_after_signal[0]["close"]
    # 取后续最多 5 根（约 5 个交易日）的收盘价看最终方向
    lookback = bars_after_signal[min(len(bars_after_signal) - 1, 5)]["close"]
    if direction == "long":
        return (lookback - entry_close) / entry_close
    return (entry_close - lookback) / entry_close


def main():
    cfg = load_config()
    positions = load_json(POSITION_PATH, [])
    closed = load_closed_trades(CLOSED_TRADES_PATH)
    capital = float(cfg["money"]["initial_capital"])

    now = beijing_now()
    today = now.date()
    last_mon, last_sun = last_complete_week(today)

    # -------- 上周 closed --------
    week_closed = [
        t for t in closed
        if t.get("close_date", "")
        and last_mon.isoformat() <= t["close_date"] <= last_sun.isoformat()
    ]

    # -------- 上周新信号（created_date 落在上周的 pending/open + closed）--------
    week_new = [
        t for t in positions + closed
        if t.get("created_date", "")
        and last_mon.isoformat() <= t["created_date"] <= last_sun.isoformat()
    ]
    # 去重：同 id 取最新
    seen = {}
    for t in week_new:
        seen[t["id"]] = t
    week_new = list(seen.values())

    # -------- 真实成交的 PnL（pnl != 0）vs 挂单未成交（pnl == 0 的 expired/pre_flatten_cancel）--------
    real_pnl = [t for t in week_closed if t.get("pnl", 0) != 0]
    no_fill = [t for t in week_closed if t.get("pnl", 0) == 0 and t.get("exit_reason") in ("expired", "pre_flatten_cancel")]

    # -------- 各 exit_reason 细分 --------
    by_reason = {}
    for t in week_closed:
        r = t.get("exit_reason", "?")
        by_reason.setdefault(r, []).append(t)

    # -------- 当前持仓/挂单 --------
    active = [p for p in positions if p.get("status") in ("pending", "open")]

    # -------- 累计 PnL（全部历史 closed）--------
    all_real_pnl = [t for t in closed if t.get("pnl", 0) != 0]
    cum_pnl = sum(t.get("pnl", 0) for t in all_real_pnl)

    # -------- 信号复盘：拉上周新信号品种的后续走势 --------
    followups = []
    for t in week_new:
        prod = t.get("product")
        direction = t.get("direction")
        sig_date = t.get("created_date") or t.get("open_date")
        if not prod or not direction or not sig_date:
            continue
        try:
            bars = fetch_daily(cfg["products"][prod]["sina"], 60)
        except Exception:
            continue
        # 找到信号日在 bars 里的位置
        idx = None
        for i, b in enumerate(bars):
            if b["date"] == sig_date:
                idx = i
                break
        if idx is None:
            continue
        after = bars[idx:]
        fb = signal_followup(cfg, prod, after, direction)
        if fb is None:
            continue
        followups.append({
            "id": t["id"], "product": prod, "direction": direction,
            "sig_date": sig_date, "after_close_pct": round(fb * 100, 2),
            "hit": fb > 0,
        })

    hit_count = sum(1 for f in followups if f["hit"])

    # -------- 系统运行观察指标 --------
    sys_obs = build_system_obs(real_pnl + no_fill, cfg)

    # -------- 组合层统计 --------
    s_real = stats(real_pnl, "pnl")
    s_all = stats(closed, "pnl")
    all_real_pnl = [t for t in closed if t.get("pnl", 0) != 0]

    # -------- 拼 Markdown --------
    md = build_markdown(
        cfg, today, last_mon, last_sun,
        week_closed, no_fill, by_reason,
        real_pnl, active, cum_pnl,
        followups, hit_count, week_new,
        s_real, s_all, capital, sys_obs, all_real_pnl,
    )

    title = "【期货组合】周报 {} ~ {}".format(last_mon.isoformat(), last_sun.isoformat())
    push(title, md, cfg)
    print(md)


def build_system_obs(week_closed, cfg):
    """基于本周 closed_trades 计算系统运行观察指标。"""
    obs = {
        "gap_count": 0, "gap_cost_total": 0.0, "avg_gap_cost": 0.0,
        "holding_days": [], "avg_holding_days": 0.0,
        "no_fill_reasons": {},
        "advice": [],
    }
    gap_cases = []
    for t in week_closed:
        # 跳空成本：entry_planned 与 entry_price（实际成交价）的价差
        planned = t.get("entry_planned")
        actual = t.get("entry_price")
        if planned and actual and abs(planned - actual) > 1e-6:
            # 对做空：成交价低于挂单价意味着跳空亏损
            gap = abs(actual - planned)
            obs["gap_cost_total"] += gap
            obs["gap_count"] += 1
            gap_cases.append(t)

        # 未成交原因统计
        reason = t.get("exit_reason")
        if reason in ("expired", "pre_flatten_cancel"):
            obs["no_fill_reasons"][reason] = obs["no_fill_reasons"].get(reason, 0) + 1

        # 持仓天数（仅真实成交）
        if t.get("pnl", 0) != 0 and t.get("open_date") and t.get("close_date"):
            try:
                od = dt.date.fromisoformat(t["open_date"])
                cd = dt.date.fromisoformat(t["close_date"])
                obs["holding_days"].append((cd - od).days)
            except Exception:
                pass

    if obs["gap_count"]:
        obs["avg_gap_cost"] = obs["gap_cost_total"] / obs["gap_count"]
    if obs["holding_days"]:
        obs["avg_holding_days"] = sum(obs["holding_days"]) / len(obs["holding_days"])

    # 基于数据给出系统优化建议（文本建议，最终决策仍由进化脚本回测验证）
    if obs["gap_count"] >= 2 and obs["avg_gap_cost"] > 3:
        obs["advice"].append("本周出现 {} 次跳空成交，平均成本 {} 元，可考虑在回测中测试 'slippage_ticks' 参数或改用更宽的触发缓冲。".format(
            obs["gap_count"], round(obs["avg_gap_cost"], 1)))
    if obs["avg_holding_days"] >= 3.5:
        obs["advice"].append("平均持仓天数 {:.1f} 天，接近上限，可在进化脚本中测试 max_hold_days 缩短或增加移动止损。".format(
            obs["avg_holding_days"]))
    if obs["no_fill_reasons"].get("expired", 0) >= 2:
        obs["advice"].append("本周 {} 笔挂单因过期未成交，可考虑测试 entry_buffer_ticks 或放宽触发条件。".format(
            obs["no_fill_reasons"]["expired"]))
    if not obs["advice"]:
        obs["advice"].append("本周系统运行平稳，暂无显著异常。")
    return obs


def build_markdown(cfg, today, last_mon, last_sun,
                   week_closed, no_fill, by_reason,
                   real_pnl, active, cum_pnl,
                   followups, hit_count, week_new,
                   s_real, s_all, capital, sys_obs, all_real_pnl):
    lines = []
    lines.append("**统计周期**：{} ~ {}　**生成时间**：{}".format(
        last_mon.isoformat(), last_sun.isoformat(), beijing_now().strftime("%Y-%m-%d %H:%M")))
    lines.append("**本金**：{}元".format(capital))
    lines.append("")

    # ---- 一、本周真实盈亏 ----
    lines.append("### 一、本周真实盈亏（{}笔平仓）".format(len(week_closed)))
    if s_real:
        lines.append("| 指标 | 数值 |")
        lines.append("| --- | --- |")
        lines.append("| 真实成交笔数 | {} |".format(s_real["n"]))
        lines.append("| 胜率 | {:.0%} |".format(s_real["win_rate"]))
        lines.append("| 总盈亏 | **{:.0f} 元** |".format(s_real["total"]))
        lines.append("| 单笔均值 | {:.0f} 元 |".format(s_real["avg"]))
        lines.append("| 周收益率 | {:.2f}% |".format(s_real["total"] / capital * 100))
        lines.append("")
        lines.append("**累计历史 PnL**：{:.0f} 元（+{:.2f}%）".format(cum_pnl, cum_pnl / capital * 100))
    else:
        lines.append("（本周无真实成交盈亏）")
    lines.append("")

    # ---- 各 exit_reason ----
    if by_reason:
        lines.append("**按离场原因细分**：")
        for reason, trades in sorted(by_reason.items()):
            total = sum(t.get("pnl", 0) for t in trades)
            real = [t for t in trades if t.get("pnl", 0) != 0]
            s = stats(real, "pnl")
            extra = "（真实成交{}笔，总盈亏{:.0f}）".format(s["n"], s["total"]) if s else ""
            lines.append("- {}：共{}笔，{}".format(
                EXIT_CN.get(reason, reason), len(trades), extra))
        lines.append("")

    # ---- 未成交挂单 ----
    if no_fill:
        lines.append("**未成交挂单**：{}笔（挂单失效率）".format(len(no_fill)))
    lines.append("")

    # ---- 二、本周每笔交易明细 ----
    if real_pnl:
        lines.append("### 二、本周交易明细")
        lines.append("| 品种 | 方向 | 成交价 | 离场价 | 离场原因 | PnL |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for t in sorted(real_pnl, key=lambda x: x.get("close_date", "")):
            lines.append("| {name} | {d} | {e} | {c} | {r} | {pnl:.0f} |".format(
                name=t.get("name", t["product"]),
                d=dir_cn(t["direction"]),
                e=t.get("entry_price", "-"),
                c=t.get("close_price", "-"),
                r=EXIT_CN.get(t.get("exit_reason", "?"), t.get("exit_reason", "?")),
                pnl=t.get("pnl", 0)))
        lines.append("")

    # ---- 三、信号复盘 ----
    lines.append("### 三、本周信号复盘（后续走势方向命中率）")
    lines.append("*用新浪日线看信号日之后 5 根 K 线的最终方向，做多信号涨=命中、做空信号跌=命中*")
    if followups:
        lines.append("| 品种 | 方向 | 信号日 | 后续涨跌 | 命中 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for f in sorted(followups, key=lambda x: x.get("sig_date", "")):
            lines.append("| {p} | {d} | {s} | {c:+.2f}% | {h} |".format(
                p=cfg["products"][f["product"]]["name"],
                d=dir_cn(f["direction"]), s=f["sig_date"],
                c=f["after_close_pct"], h="✅" if f["hit"] else "❌"))
        lines.append("")
        hit_rate = hit_count / len(followups)
        lines.append("**方向命中率**：{:.0%}（{}/{}）".format(hit_rate, hit_count, len(followups)))
        if hit_rate < 0.5:
            lines.append("⚠️ 本周信号方向命中率低于 50%，建议警惕。")
    else:
        lines.append("（本周无新信号或信号品种无后续日线，无法复盘）")
    lines.append("")

    # ---- 四、当前持仓/挂单 ----
    lines.append("### 四、当前持仓/挂单（{}笔）".format(len(active)))
    if active:
        lines.append("| 品种 | 方向 | 状态 | 成交价 | 止损 | 止盈1 | 止盈2 | 手数 |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for p in active:
            st = "待成交" if p.get("status") == "pending" else "持仓中"
            price = p.get("fill_price") or p.get("entry_price", "-")
            lines.append("| {n} | {d} | {st} | {e} | {sl} | {t1} | {t2} | {lots} |".format(
                n=p["name"], d=dir_cn(p["direction"]), st=st,
                e=price, sl=p["stop_loss"], t1=p["tp1"], t2=p["tp2"],
                lots=p.get("lots_open", p["lots"])))
    else:
        lines.append("（空仓）")
    lines.append("")

    # ---- 五、系统运行观察 ----
    lines.append("### 五、系统运行观察")
    lines.append("*从真实成交数据里观察执行层效率，为系统级进化（监控/执行/风控）提供数据依据*")
    lines.append("| 指标 | 数值 |")
    lines.append("| --- | --- |")
    lines.append("| 本周平仓笔数 | {} |".format(len(week_closed)))
    lines.append("| 跳空成交次数 | {} |".format(sys_obs["gap_count"]))
    lines.append("| 平均跳空成本 | {} 元 |".format(round(sys_obs["avg_gap_cost"], 1) if sys_obs["gap_count"] else "-"))
    lines.append("| 平均持仓天数 | {} 天 |".format(round(sys_obs["avg_holding_days"], 1) if sys_obs["holding_days"] else "-"))
    lines.append("| 挂单过期未成交 | {} 笔 |".format(sys_obs["no_fill_reasons"].get("expired", 0)))
    lines.append("| 节前撤单 | {} 笔 |".format(sys_obs["no_fill_reasons"].get("pre_flatten_cancel", 0)))
    lines.append("")
    lines.append("**系统优化建议（待 rule_evolution.py 回测验证）**：")
    for adv in sys_obs["advice"]:
        lines.append("- {}".format(adv))
    lines.append("")

    # ---- 六、历史累计 ----
    lines.append("### 六、历史累计（启动以来）")
    if s_all:
        lines.append("| 指标 | 数值 |")
        lines.append("| --- | --- |")
        lines.append("| 累计平仓笔数 | {} |".format(s_all["n"]))
        lines.append("| 真实成交笔数 | {} |".format(len(all_real_pnl)))
        lines.append("| 真实胜率 | {:.0%} |".format(s_all["win_rate"]))
        lines.append("| 累计真实 PnL | **{:.0f} 元** |".format(cum_pnl))
        lines.append("| 累计收益率 | **{:.2f}%** |".format(cum_pnl / capital * 100))
    else:
        lines.append("（无历史平仓记录）")

    lines.append("")
    lines.append("> 风险提示：本报告基于机械规则与真实行情统计，不构成投资建议。")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
