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

import datetime as dt

from common import (POSITION_PATH, STATE_PATH, build_signal, fetch_daily,
                    fetch_realtime, load_config, load_json, market_holidays,
                    next_trading_day, push, save_json)


def dir_cn(direction):
    return "做多" if direction == "long" else "做空"


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

    md = build_markdown(cfg, today, plan_date, results, selected, active)
    title = "【期货组合】盘后信号 {} 生成{}条挂单".format(today, len(selected))
    push(title, md, cfg)
    print(md)


def build_markdown(cfg, today, plan_date, results, selected, active):
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
            stop_sp = round(abs(s["stop"] - s["entry"]), 1)
            tp2_sp = round(abs(s["tp2"] - s["entry"]), 1)
            lines.append("| {name}({prod}) | {d} | {e} | {sl} | {t1} | {t2} | {lots} | {mg} | {vr} | {ss} | {ts} |".format(
                name=s["name"], prod=s["product"], d=dir_cn(s["direction"]),
                e=s["entry"], sl=s["stop"], t1=s["tp1"], t2=s["tp2"],
                lots=s["lots"], mg=s["margin_per_lot"], vr=round(s["vol_ratio"], 2),
                ss=stop_sp, ts=tp2_sp))
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
            stop_sp = round(abs(s["stop"] - s["entry"]), 1)
            tp1_sp = round(abs(s["tp1"] - s["entry"]), 1)
            tp2_sp = round(abs(s["tp2"] - s["entry"]), 1)
            p = cfg["products"][s["product"]]
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
            lines.append("  - **止损价差 = {ss}**，委托价**市价**（自动兜底）。".format(ss=stop_sp))
            lines.append("  - **止盈价差 = {ts}**，委托价**市价**（到 TP2 自动平仓离场）。".format(ts=tp2_sp))
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

    lines.append("### 二、当前持仓/挂单（{}笔）".format(len(active)))
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

    lines.append("### 三、各品种过滤情况")
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


if __name__ == "__main__":
    main()
