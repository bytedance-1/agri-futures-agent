# -*- coding: utf-8 -*-
"""
daily_signal.py —— 每日盘后信号生成
-----------------------------------
运行时机：交易日 15:10（北京时间），周一到周五。
流程：拉取全品种日线 + 实时行情 -> 计算信号 -> 过滤/择优 -> 生成次日挂单计划
     -> 写入 position.json（status=pending，表示“挂单待成交”）-> 推送微信表格。

设计要点：
- 数据源为“主力连续”合约（如 RB0 / M0 / SR0），在新浪接口上即为拼接后的主力。
- 当日 K 线优先取日线接口；若日线尚未包含当日，则用实时行情合成当日 K 线。
- 只生成“挂单计划”，不会自动下单（GitHub Actions 无法下单），
  需要你在交易软件里按推送的“条件单/停损单”参数自行挂单。
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

from common import (POSITION_PATH, STATE_PATH, build_signal, fetch_daily,
                    fetch_realtime, load_config, load_json, market_holidays,
                    next_trading_day, push, save_json)


def dir_cn(direction):
    return "做多" if direction == "long" else "做空"


def round_to_tick(value, tick):
    """把价差 round 到最小变动单位 tick 的整数倍（取最近）。"""
    if tick is None or tick <= 0:
        return round(value)
    return round(value / tick) * tick


def fmt_int(v):
    """整数值去掉小数点展示。"""
    return int(v) if v == int(v) else v


def main():
    cfg = load_config()
    products = list(cfg["products"].keys())
    symbols = [cfg["products"][p]["sina"] for p in products]

    positions = load_json(POSITION_PATH, [])
    state = load_json(STATE_PATH, {"cooldowns": {}})
    cooldowns = state.get("cooldowns", {})

    rt = fetch_realtime(symbols)
    if not rt:
        print("未获取到实时行情，退出")
        return
    today = list(rt.values())[0]["date"]
    today_d = dt.date.fromisoformat(today)
    holidays = market_holidays(cfg, today_d)
    plan_date = next_trading_day(today_d, holidays).isoformat()

    active = [p for p in positions if p.get("status") in ("pending", "open")]
    held = {p["product"] for p in active}

    # 已占用的“关联品种组”（M 与 RM 高度相关，同时持仓等于重复风险）
    groups = [tuple(g) for g in cfg.get("correlation_groups", [])]
    occupied_groups = set()
    for g in groups:
        if any(p["product"] in g for p in active):
            occupied_groups.add(g)

    results = []   # (product, signal|None, reason)
    signals = []
    for prod in products:
        sym = cfg["products"][prod]["sina"]
        try:
            bars = fetch_daily(sym, cfg["history_days"])
            q = rt.get(sym)
            if not q:
                results.append((prod, None, "无实时行情"))
                continue
            # 如日线未含当日，用实时行情合成当日K线
            if not bars or bars[-1]["date"] != q["date"]:
                bars.append({
                    "date": q["date"], "open": q["open"], "high": q["high"],
                    "low": q["low"], "close": q["last"],
                    "volume": q["volume"], "oi": q["oi"], "settle": 0.0,
                })
            sig, reason = build_signal(cfg, prod, bars)
            results.append((prod, sig, reason))
            if sig:
                signals.append(sig)
        except Exception as e:  # noqa
            results.append((prod, None, "出错: {}".format(e)))

    # ---- 择优：同方向多信号保留成交量放大最高者；再应用组合约束 ----
    signals.sort(key=lambda s: s["vol_ratio"], reverse=True)
    selected = []
    for s in signals:
        prod = s["product"]
        if prod in held:
            s["skip"] = "该品种已持仓/已挂单"
            continue
        if cooldowns.get(prod, "") and cooldowns[prod] >= today:
            s["skip"] = "冷却期至 {}".format(cooldowns[prod])
            continue
        grp = next((g for g in groups if prod in g), None)
        if grp and grp in occupied_groups:
            s["skip"] = "关联品种已持仓(相关性约束)"
            continue
        if len(active) + len(selected) >= int(cfg["money"]["max_positions"]):
            s["skip"] = "已达最大持仓数"
            continue
        if s["lots"] < 1:
            s["skip"] = "手数为0（风险/保证金约束）"
            continue
        selected.append(s)
        if grp:
            occupied_groups.add(grp)

    # ---- 生成挂单计划写入 position.json ----
    new_positions = []
    for s in selected:
        new_positions.append({
            "id": "{}-{}-{}".format(s["product"], today_d.strftime("%Y%m%d"), "L" if s["direction"] == "long" else "S"),
            "product": s["product"],
            "name": s["name"],
            "direction": s["direction"],
            "status": "pending",
            "plan_date": plan_date,
            "entry_price": s["entry"],
            "lots": s["lots"],
            "lots_open": s["lots"],
            "stop_loss": s["stop"],
            "initial_stop": s["stop"],
            "tp1": s["tp1"],
            "tp2": s["tp2"],
            "tp1_done": False,
            "atr": s["atr20"],
            "multiplier": cfg["products"][s["product"]]["multiplier"],
            "margin_per_lot": s["margin_per_lot"],
            "risk_per_lot": s["risk_per_lot"],
            "created_date": today,
            "signal": {
                "vol_ratio": round(s["vol_ratio"], 2),
                "vol_rank": round(s["vol_rank"], 2),
                "oi_delta": round(s["oi_delta"], 0),
                "body_pct": round(s["body_pct"] * 100, 2),
                "close_pos": round(s["close_pos"], 2),
                "ma20": s["ma20"],
                "ma60": s["ma60"],
            },
        })
    if new_positions:
        positions.extend(new_positions)
        save_json(POSITION_PATH, positions)
    state["last_signal_date"] = today
    save_json(STATE_PATH, state)

    # 周四盘后：为 open 持仓生成"周五前止盈操作指引"
    open_positions = [p for p in active if p.get("status") == "open"]
    exit_proposals = build_friday_exit_proposals(cfg, open_positions, rt) \
        if (today_d.weekday() == 3 and open_positions) else []

    md = build_markdown(cfg, today, plan_date, results, selected, active, exit_proposals)
    title = "【期货组合】盘后信号 {} 生成{}条挂单".format(today, len(selected))
    push(title, md, cfg)
    print(md)


def build_markdown(cfg, today, plan_date, results, selected, active, exit_proposals=None):
    exit_proposals = exit_proposals or []
    lines = []
    lines.append("**信号日期**：{}　**挂单交易日**：{}".format(today, plan_date))
    lines.append("**资金**：{}元　单笔风险{}%　单品种保证金上限{}%".format(
        cfg["money"]["initial_capital"],
        int(cfg["money"]["risk_per_trade"] * 100),
        int(cfg["money"]["max_margin_ratio"] * 100)))
    lines.append("")

    # 计算挂单时机提醒：区分周五 vs 周一~周四
    today_d = dt.date.fromisoformat(today)
    is_friday = today_d.weekday() == 4  # Python weekday: Mon=0, Fri=4

    if selected:
        lines.append("### 一、新挂单计划（{}条）".format(len(selected)))
        lines.append("| 品种 | 方向 | 挂单价 | 止损 | 止盈1 | 止盈2 | 手数 | 保证金/手 | 量比 | 止损价差 | 止盈价差(TP2) |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for s in selected:
            tick = cfg["products"][s["product"]].get("tick", 1)
            raw_stop = abs(s["stop"] - s["entry"])
            raw_tp2 = abs(s["tp2"] - s["entry"])
            stop_sp = round_to_tick(raw_stop, tick)
            tp2_sp = round_to_tick(raw_tp2, tick)
            lines.append("| {name}({prod}) | {d} | {e} | {sl} | {t1} | {t2} | {lots} | {mg} | {vr} | {ss} | {ts} |".format(
                name=s["name"], prod=s["product"], d=dir_cn(s["direction"]),
                e=s["entry"], sl=s["stop"], t1=s["tp1"], t2=s["tp2"],
                lots=s["lots"], mg=s["margin_per_lot"], vr=round(s["vol_ratio"], 2),
                ss=fmt_int(stop_sp), ts=fmt_int(tp2_sp)))
        lines.append("")

        # ---- 挂单时机提醒 ----
        if is_friday:
            lines.append("⚠️ **今日为周五，不要现在挂单！**")
            lines.append("今晚夜盘直接触发会导致持仓过周末。")
            lines.append("请等待 **今晚 23:10 周五夜盘复核任务** 推送结论后，再按结论挂单（或放弃本信号）。")
        else:
            lines.append("✅ **现在就挂单**（周一到周四让夜盘/次日日盘触发均可，不会持仓过周末）。")
            for s in selected:
                p = cfg["products"][s["product"]]
                has_night = p.get("has_night", False)
                if not has_night:
                    lines.append("- {} 无夜盘，最早明日 09:00 开盘才可能触发。".format(s["name"]))
        lines.append("- 挂单仅当日({})有效；未被触发则次日自动作废。".format(plan_date))
        lines.append("")

        # ---- 每个品种的详细挂单参数（含附加止损止盈价差）----
        lines.append("**挂单操作明细**（交易软件中按以下参数创建条件单）：")
        for s in selected:
            p = cfg["products"][s["product"]]
            tick = p.get("tick", 1)
            raw_stop = abs(s["stop"] - s["entry"])
            raw_tp1 = abs(s["tp1"] - s["entry"])
            raw_tp2 = abs(s["tp2"] - s["entry"])
            stop_sp = round_to_tick(raw_stop, tick)
            tp1_sp = round_to_tick(raw_tp1, tick)
            tp2_sp = round_to_tick(raw_tp2, tick)
            if s["direction"] == "long":
                trigger_cond = "≥ {}".format(s["entry"])
                trigger_action = "买入开仓{}手".format(s["lots"])
            else:
                trigger_cond = "≤ {}".format(s["entry"])
                trigger_action = "卖出开仓{}手".format(s["lots"])

            lines.append("")
            lines.append("**{name}（{d}）**".format(name=s["name"], d=dir_cn(s["direction"])))
            lines.append("- 触发条件：价格 {cond}，{act}，委托价**对手价**，手数 {lots}。".format(
                cond=trigger_cond, act=trigger_action, lots=s["lots"]))
            lines.append("- 期限：当日有效（{pd} 15:15 前）。".format(pd=plan_date))
            lines.append("- 附加止损止盈：选「限价止损+限价止盈」或「限价止损」。")
            lines.append("  - **止损价差 = {ss}**，委托价**市价**（自动兜底）。".format(ss=fmt_int(stop_sp)))
            lines.append("  - **止盈价差 = {ts}**，委托价**市价**（到 TP2 自动平仓离场）。".format(ts=fmt_int(tp2_sp)))
            lines.append("- 到价提醒（附加单表达不了，需手动）：")
            if s["lots"] <= 1:
                lines.append("  - **TP1 = {}**：提醒你把止损从 {} 手动上移到保本 {}，继续持有博 TP2。".format(
                    s["tp1"], s["stop"], s["entry"]))
            else:
                close_lots = max(1, int(s["lots"] * float(cfg["exit"]["tp1_close_ratio"])))
                remain = s["lots"] - close_lots
                lines.append("  - **TP1 = {}**：提醒你手动减仓{}手止盈 + 剩余{}手止损上移保本 {}。".format(
                    s["tp1"], close_lots, remain, s["entry"]))
            lines.append("  - **TP2 = {}**：附加止盈自动平仓；可再设提醒双保险。".format(s["tp2"]))
    else:
        lines.append("### 一、新挂单计划：无（今日无满足全部条件的信号）")
        lines.append("")

    # ---- 周四：周五持仓止盈操作指引 ----
    if exit_proposals:
        lines.append("### 二、🔔 周五持仓止盈操作指引（周四专属）")
        lines.append("*无夜盘品种周五 14:30 代码强平，请按下方指引操作；有夜盘品种周五夜盘后可继续拿*")
        lines.append("")
        for prop in exit_proposals:
            render_exit_proposal(cfg, prop, lines)
        lines.append("")

    lines.append("### {} 当前持仓/挂单（{}笔）".format(
        "三" if exit_proposals else "二", len(active)))
    if active:
        lines.append("| 品种 | 方向 | 状态 | 挂单价/开仓价 | 止损 | 手数 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for p in active:
            st = "待成交" if p.get("status") == "pending" else "持仓中"
            lines.append("| {name} | {d} | {st} | {e} | {sl} | {lots} |".format(
                name=p["name"], d=dir_cn(p["direction"]), st=st,
                e=p.get("entry_price", p.get("fill_price", "")),
                sl=p["stop_loss"], lots=p.get("lots_open", p["lots"])))
    else:
        lines.append("（无）")
    lines.append("")

    lines.append("### {} 各品种过滤情况".format(
        "四" if exit_proposals else "三"))
    for prod, sig, reason in results:
        nm = cfg["products"][prod]["name"]
        if sig:
            extra = ""
            if sig.get("skip"):
                extra = "（未入选：{}）".format(sig["skip"])
            elif sig in selected:
                extra = "（已入选）"
            lines.append("- {}：通过全部过滤 {}".format(nm, extra))
        else:
            lines.append("- {}：未通过 —— {}".format(nm, reason))

    lines.append("")
    lines.append("> 风险提示：本信号为机械规则计算结果，不构成投资建议。请自行确认仓位与风险。")
    return "\n".join(lines)


# ============================================================
# 周四：周五持仓止盈操作指引
# ============================================================
def build_friday_exit_proposals(cfg, open_positions, rt):
    """周四盘后为每个 open 持仓生成"周五前如何止盈/离场"的操作指引参数。"""
    proposals = []
    for p in open_positions:
        prod = p["product"]
        pinfo = cfg["products"][prod]
        sym = pinfo["sina"]
        if sym not in rt:
            continue
        q = rt[sym]
        cur = float(q["last"])
        direction = p["direction"]
        fill = float(p.get("fill_price") or p.get("entry_price"))
        mult = float(p["multiplier"])
        lots = int(p.get("lots_open", p["lots"]))
        tp1_done = p.get("tp1_done", False)
        tp1 = float(p["tp1"])
        tp2_orig = float(p["tp2"])
        has_night = bool(pinfo.get("has_night", False))
        tick = float(pinfo.get("tick", 1))
        atr = float(p.get("atr") or 0)

        pnl_now = round(
            (fill - cur) * mult * lots if direction == "short" else
            (cur - fill) * mult * lots, 1)
        dist_to_tp2 = abs(tp2_orig - cur)   # 现价到原 TP2 的距离
        dist_to_tp2_atr = dist_to_tp2 / atr if atr > 0 else 99

        if not has_night:
            # 无夜盘品种：周五 14:30 代码强平（持仓不过周末）
            # ---- 方案 B：把 TP2 拉近 ----
            # tp1_done=True 且已近 TP1 → 保守拉近 0.3 ATR（确保大概率能摸到）
            # tp1_done=False 或离 TP1 还远 → 拉近 0.5 ATR
            if tp1_done and dist_to_tp2_atr <= 1.0:
                offset = 0.3 * atr
            else:
                offset = 0.5 * atr
            raw_tp2_new = cur - offset if direction == "short" else cur + offset
            tp2_new = round_to_tick(raw_tp2_new, tick)
            pnl_tp2_new = round(
                (fill - tp2_new) * mult * lots if direction == "short" else
                (tp2_new - fill) * mult * lots, 1)

            # 等 14:30 强平的预估：用现价，但盘中可能反抽 ±0.5 ATR
            pnl_flatten_minus = round(pnl_now - 0.5 * atr * mult * lots, 1)
            pnl_flatten_plus = round(pnl_now + 0.5 * atr * mult * lots, 1)

            proposals.append({
                "product": prod, "name": p["name"], "direction": direction,
                "has_night": False, "tp1_done": tp1_done,
                "fill": fill, "cur": cur, "atr": atr, "tick": tick,
                "tp1": tp1, "tp2_orig": tp2_orig, "tp2_new": tp2_new,
                "dist_to_tp2_atr": round(dist_to_tp2_atr, 1),
                "pnl_now": pnl_now, "pnl_tp2_new": pnl_tp2_new,
                "pnl_flatten_minus": pnl_flatten_minus, "pnl_flatten_plus": pnl_flatten_plus,
                "lots": lots, "mult": mult,
            })
        else:
            # 有夜盘品种：周五夜盘后仍可持有（代码不会强平，周末敞口可控）
            # ---- 方案 B：TP2 拉近到周五收盘前能摸到的位置（1.0 ATR）----
            offset = 1.0 * atr
            raw_tp2_new = cur - offset if direction == "short" else cur + offset
            tp2_new = round_to_tick(raw_tp2_new, tick)
            pnl_tp2_new = round(
                (fill - tp2_new) * mult * lots if direction == "short" else
                (tp2_new - fill) * mult * lots, 1)

            proposals.append({
                "product": prod, "name": p["name"], "direction": direction,
                "has_night": True, "tp1_done": tp1_done,
                "fill": fill, "cur": cur, "atr": atr, "tick": tick,
                "tp1": tp1, "tp2_orig": tp2_orig, "tp2_new": tp2_new,
                "dist_to_tp2_atr": round(dist_to_tp2_atr, 1),
                "pnl_now": pnl_now, "pnl_tp2_new": pnl_tp2_new,
                "lots": lots, "mult": mult,
            })
    return proposals


def render_exit_proposal(cfg, prop, lines):
    """把 build_friday_exit_proposals 产出的单条指引渲染成 Markdown 段落。"""
    nm = prop["name"]
    d = dir_cn(prop["direction"])
    pnl_now = prop["pnl_now"]
    pnl_sign = "+" if pnl_now >= 0 else ""

    lines.append("#### **{}（{}）— 现价 {} 元，浮动盈亏 {}{} 元**".format(
        nm, d, prop["cur"], pnl_sign, round(pnl_now, 1)))

    # 基础信息
    lines.append("- 开仓价 {}　原 TP2 = {}　现价距原 TP2 = {:.1f} ATR　ATR = {}".format(
        prop["fill"], prop["tp2_orig"], prop["dist_to_tp2_atr"], round(prop["atr"], 1)))
    lines.append("- TP1 状态：{}　{}夜盘".format(
        "✅ 已触发，止损已上移保本" if prop["tp1_done"] else "❌ 未触发，止损仍在初始位",
        "有" if prop["has_night"] else "**无（周五 14:30 代码强平）**"))
    lines.append("")

    if not prop["has_night"]:
        # 无夜盘品种
        lines.append("**周五 14:30 代码会强平**，以下 3 选 1：")
        lines.append("")
        lines.append("**🟢 方案 A（推荐）：现在市价全平 → 锁定 +{:.0f} 元**".format(pnl_now))
        lines.append("  - 操作：撤掉所有条件单，直接市价全平")
        lines.append("  - 适用：已接近 TP1，且离原 TP2 太远（>{:.1f} ATR），赌不到".format(prop["dist_to_tp2_atr"]))
        lines.append("")
        lines.append("**🟡 方案 B：把 TP2 改到 {}，赌周五能摸到 → 预估 +{:.0f} 元（比现价多赚 {:.0f} 元）**".format(
            prop["tp2_new"], prop["pnl_tp2_new"], prop["pnl_tp2_new"] - pnl_now))
        lines.append("  - 操作：撤掉原 TP2 条件单，挂新 TP2 = {}（限价，到价市价）".format(prop["tp2_new"]))
        lines.append("  - 注意：")
        if prop["tp1_done"]:
            lines.append("    - TP1 已触发（止损已上移保本 {}），保本条件单保留，到 TP2 自动平仓".format(prop["fill"]))
        else:
            lines.append("    - TP1 还没触发：先别改止损！到 TP1 再手动上移保本")
        lines.append("    - **如果周五 14:30 仍未摸到 {}，代码会市价强平**，实际收益可能在 [{} ~ {}] 之间".format(
            prop["tp2_new"],
            prop["pnl_flatten_minus"], prop["pnl_flatten_plus"]))
        lines.append("    - 适用：空头/多头动量仍强，且 {} 离现价 < 1 ATR（周五大概率能摸到）".format(prop["tp2_new"]))
        lines.append("")
        lines.append("**🔴 方案 C：不改任何条件单，等周五 14:30 代码强平**")
        lines.append("  - 风险：盘中可能反抽，真实强平价可能比现价差 ±0.5 ATR（约 ±{} 元）".format(
            round(0.5 * prop["atr"] * prop["mult"] * prop["lots"], 0)))
        lines.append("  - 收益区间：[{} ~ {}] 元，取决于当天走势".format(
            prop["pnl_flatten_minus"], prop["pnl_flatten_plus"]))
        lines.append("")
    else:
        # 有夜盘品种
        lines.append("**周五夜盘后仍可持有（代码不会强平）**，以下 3 选 1：")
        lines.append("")
        lines.append("**🟢 方案 A（默认）：不改任何条件单，继续按原 TP2 / 时间止损执行**")
        lines.append("  - 周五夜盘 + 周一至周四都能继续拿，时间止损（{} 交易日）还有缓冲".format(
            cfg["exit"]["max_hold_days"]))
        lines.append("  - 适用：空头/多头趋势未变，原 TP2 = {} 不远".format(prop["tp2_orig"]))
        lines.append("")
        lines.append("**🟡 方案 B：TP2 拉近到 {}，保守落袋 → 预估 +{:.0f} 元（比现价多赚 {:.0f} 元）**".format(
            prop["tp2_new"], prop["pnl_tp2_new"], prop["pnl_tp2_new"] - pnl_now))
        lines.append("  - 操作：撤掉原 TP2 条件单，挂新 TP2 = {}".format(prop["tp2_new"]))
        if prop["tp1_done"]:
            lines.append("    （保本止损 {} 保留）".format(prop["fill"]))
        lines.append("  - 适用：想让周五收盘前更大概率触达落袋为安")
        lines.append("")
        lines.append("**🔴 方案 C：周五 23:10 夜盘复核任务再决策**")
        lines.append("  - 等周五夜盘开出后看延续性，再决定是改 TP2、还是等周一继续")
        lines.append("  - 适用：对周五夜盘方向不确定，想等信号")
        lines.append("")


if __name__ == "__main__":
    main()
