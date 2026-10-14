# -*- coding: utf-8 -*-
"""
daily_signal.py —— 每日盘后信号生成
-----------------------------------
运行时机：交易日 15:10（北京时间），周一到周五。
流程：拉取 M/C/RM 日线 + 实时行情 -> 计算信号 -> 过滤/择优 -> 生成次日挂单计划
     -> 写入 position.json（status=pending，表示“挂单待成交”）-> 推送微信表格。

设计要点：
- 数据源为“主力连续”合约（M0 / C0 / RM0），在新浪接口上即为拼接后的主力。
- 当日 K 线优先取日线接口；若日线尚未包含当日，则用实时行情合成当日 K 线。
- 只生成“挂单计划”，不会自动下单（GitHub Actions 无法下单），
  需要你在交易软件里按推送的“条件单/停损单”参数自行挂单。
"""
from __future__ import annotations

import datetime as dt

from common import (POSITION_PATH, STATE_PATH, build_signal, fetch_daily,
                    fetch_realtime, load_config, load_json, push, save_json)


def next_weekday(d):
    while True:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            return d


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
    plan_date = next_weekday(today_d).isoformat()

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
    title = "【农产品期货】盘后信号 {} 生成{}条挂单".format(today, len(selected))
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

    if selected:
        lines.append("### 一、新挂单计划（{}条）".format(len(selected)))
        lines.append("| 品种 | 方向 | 挂单价 | 止损 | 止盈1(平60%) | 止盈2 | 手数 | 保证金/手 | 量比 |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for s in selected:
            lines.append("| {name}({prod}) | {d} | {e} | {sl} | {t1} | {t2} | {lots} | {mg} | {vr} |".format(
                name=s["name"], prod=s["product"], d=dir_cn(s["direction"]),
                e=s["entry"], sl=s["stop"], t1=s["tp1"], t2=s["tp2"],
                lots=s["lots"], mg=s["margin_per_lot"], vr=round(s["vol_ratio"], 2)))
        lines.append("")
        lines.append("**挂单操作**：在交易软件中设置“条件单/停损单”（非普通限价单）：")
        for s in selected:
            if s["direction"] == "long":
                lines.append("- {}：价格 ≥ {} 触发买入开仓{}手".format(s["name"], s["entry"], s["lots"]))
            else:
                lines.append("- {}：价格 ≤ {} 触发卖出开仓{}手".format(s["name"], s["entry"], s["lots"]))
        lines.append("- 挂单仅当日({})有效；未被触发则次日自动作废。".format(plan_date))
        lines.append("")
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
