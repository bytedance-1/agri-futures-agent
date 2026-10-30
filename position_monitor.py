# -*- coding: utf-8 -*-
"""
position_monitor.py —— 持仓监控
------------------------------
运行时机：交易日 9:30-15:00（北京时间）每 30 分钟一次，周一到周五。
流程：读取 position.json -> 拉取持仓品种实时行情 -> 判断触发条件
     -> 更新 position.json（成交/减仓/上移止损/平仓/删除）-> 触发时推送微信。

触发条件：
1. 挂单成交（价格触及挂单价）
2. 止损触发
3. 第一止盈触发（>=2手：减仓 60% + 止损上移保本；1手：止损上移保本并继续持有）
4. 第二止盈触发（全部平仓）
5. 持仓满 N 个交易日到期平仓
6. 周末/长假前强制平仓（持仓不过周末/节假日：在当日 14:30 后，若距下一交易日 >= 3 天，
   则市价平掉所有持仓，并撤销当日未成交挂单）

说明：为避免“滑点/先后顺序”无法还原导致的失真，本脚本采用保守判定：
- 已隔日持有的仓位：用当日最高/最低价判定是否触发（真实条件单会成交）；
- 当日新成交的仓位：仅用当前价判定，避免用当日 K 线做“事后判断”（前视偏差）。
- 挂单成交价与回测同口径：若成交日开盘已越过挂单价（跳空），成交价记为当日开盘价（更差的价格）；
  后续止损/止盈/盈亏均以该实际成交价为基准，避免实盘记账系统性低估跳空成本。
"""
from __future__ import annotations


# ---- BOOTSTRAP：确保无论从哪个目录执行，都能找到 common.py 和 json 文件 ----
import os as _os, sys as _sys
_HERE = _os.path.dirname(_os.path.abspath(__file__))
if _HERE not in _sys.path:
    _sys.path.insert(0, _HERE)
_os.chdir(_HERE)
# --------------------------------------------------------------------------
import math
import datetime as dt

from common import (POSITION_PATH, STATE_PATH, CLOSED_TRADES_PATH, append_closed_trade,
                    beijing_now, fetch_daily,
                    fetch_realtime, is_pre_closure_day, load_config, load_json,
                    market_holidays, push, save_json)


def dir_cn(d):
    return "做多" if d == "long" else "做空"


def add_days(date_str, n):
    d = dt.date.fromisoformat(date_str) + dt.timedelta(days=n)
    return d.isoformat()


def trading_days_held(cfg, product, open_date, today):
    """统计 open_date 之后、today(含)之前已过去的交易日数量。"""
    try:
        bars = fetch_daily(cfg["products"][product]["sina"], 40)
        return sum(1 for b in bars if open_date < b["date"] <= today)
    except Exception:
        return 0


def pnl_of(direction, entry, price, mult, lots):
    pts = (price - entry) if direction == "long" else (entry - price)
    return round(pts * mult * lots, 1)


def fill_of(p):
    """实际成交价：跳空时记录为当日开盘价，否则为挂单价；旧数据回退挂单价。"""
    return p.get("fill_price") or p["entry_price"]


def archive_one(p, close_price, pnl, exit_reason, close_date):
    """完整离场时把条目归档到 closed_trades.json（挂单未成交也记录为 pnl=0 的失效事件）。
    返回归档记录字典（方便调用方同步 state.last_close）。"""
    record = {
        "id": p["id"],
        "product": p["product"],
        "name": p.get("name", p["product"]),
        "direction": p["direction"],
        "status": "closed",
        "entry_price": p.get("fill_price") or p.get("entry_price"),
        "entry_planned": p.get("entry_price"),
        "open_date": p.get("open_date", p.get("plan_date")),
        "close_date": close_date,
        "close_price": close_price,
        "exit_reason": exit_reason,
        "lots": p.get("lots_open", p.get("lots")),
        "multiplier": p.get("multiplier"),
        "pnl": round(pnl, 1),
        "stop_loss": p.get("stop_loss"),
        "tp1": p.get("tp1"),
        "tp2": p.get("tp2"),
        "created_date": p.get("created_date"),
        "signal": p.get("signal"),
        "note": p.get("note", ""),
    }
    append_closed_trade(record, CLOSED_TRADES_PATH)
    return record


def main():
    cfg = load_config()
    positions = load_json(POSITION_PATH, [])
    state = load_json(STATE_PATH, {"cooldowns": {}})

    active = [p for p in positions if p.get("status") in ("pending", "open")]
    if not active:
        print("当前无持仓/挂单，退出")
        return

    syms = list(dict.fromkeys(cfg["products"][p["product"]]["sina"] for p in active))
    rt = fetch_realtime(syms)
    if not rt:
        print("未获取到实时行情，退出")
        return

    today = list(rt.values())[0]["date"]
    today_d = dt.date.fromisoformat(today)
    holidays = market_holidays(cfg, today_d)
    flatten_now = (cfg.get("flatten_before_holiday", True)
                   and is_pre_closure_day(today_d, holidays)
                   and beijing_now().strftime("%H:%M") >= cfg.get("flatten_time", "14:30"))

    events = []
    keep = []
    changed = False

    for p in positions:
        if p.get("status") not in ("pending", "open"):
            keep.append(p)
            continue

        q = rt.get(cfg["products"][p["product"]]["sina"])
        if not q:
            keep.append(p)
            continue

        today = q["date"]
        mult = float(p["multiplier"])
        direction = p["direction"]

        # ---------------- 挂单待成交 ----------------
        if p["status"] == "pending":
            if flatten_now and p["plan_date"] == today:
                events.append({
                    "product": p["name"], "direction": dir_cn(direction),
                    "action": "节前撤单", "price": q["last"], "pnl": 0.0,
                    "advice": "今日为周末/长假前最后一个交易日，撤销尚未成交的挂单，不新开仓。",
                })
                archive_one(p, q["last"], 0.0, "pre_flatten_cancel", today)
                changed = True
                continue  # 删除该挂单
            if today > p["plan_date"]:
                events.append({
                    "product": p["name"], "direction": dir_cn(direction),
                    "action": "挂单失效", "price": q["last"], "pnl": 0.0,
                    "advice": "挂单在 {} 未被触发，已自动撤销。".format(p["plan_date"]),
                })
                archive_one(p, q["last"], 0.0, "expired", today)
                changed = True
                continue  # 删除该挂单
            if today == p["plan_date"]:
                # 与回测同口径：开盘已越过挂单价则按开盘价成交（跳空），否则按挂单价成交
                if direction == "long":
                    gapped = q["open"] >= p["entry_price"]
                    filled = q["high"] >= p["entry_price"]
                else:
                    gapped = q["open"] <= p["entry_price"]
                    filled = q["low"] <= p["entry_price"]
                if filled:
                    fill = q["open"] if gapped else p["entry_price"]
                    p["status"] = "open"
                    p["open_date"] = today
                    p["fill_price"] = fill
                    advice = "已成交。止损 {}，止盈1 {}，止盈2 {}。".format(
                        p["stop_loss"], p["tp1"], p["tp2"])
                    if abs(fill - p["entry_price"]) > 1e-9:
                        advice = "开盘跳空成交：成交价 {}（挂单价 {}）。".format(
                            fill, p["entry_price"]) + advice
                    events.append({
                        "product": p["name"], "direction": dir_cn(direction),
                        "action": "挂单成交", "price": fill,
                        "pnl": 0.0,
                        "advice": advice,
                    })
                    changed = True
                keep.append(p)
                continue
            keep.append(p)
            continue

        # ---------------- 持仓中 ----------------
        p.setdefault("open_date", p.get("plan_date"))
        open_date = p["open_date"]
        # 隔日持仓用当日高低价（真实条件单从开盘就全天生效），当日成交只用当前价（保守）。
        # 但 TP1 触发后止损已被盘中上移保本 → 新止损是从那一刻才生效的条件单，
        # 不应再用"当日累计 high/low"判定（那包含了止损上移之前的历史行情），
        # 否则会出现"止损改到保本但开盘价就触发"的假阳性 bug。
        use_range = (open_date < today) and not p.get("tp1_done")
        hi = q["high"] if use_range else q["last"]
        lo = q["low"] if use_range else q["last"]
        cur = q["last"]
        lots_open = int(p.get("lots_open", p["lots"]))

        # 周末/长假前强制平仓（持仓不过周末/节假日）
        if flatten_now:
            pnl = pnl_of(direction, fill_of(p), cur, mult, lots_open)
            events.append({
                "product": p["name"], "direction": dir_cn(direction),
                "action": "周末/节前强平", "price": cur, "pnl": pnl,
                "advice": "今日为周末/长假前最后一个交易日，按市价强制平仓，持仓不过周末/节假日。",
            })
            archive_one(p, cur, pnl, "pre_flatten", today)
            changed = True
            continue  # 删除持仓

        if direction == "long":
            stop_hit = lo <= p["stop_loss"]
            tp2_hit = hi >= p["tp2"]
            tp1_hit = hi >= p["tp1"]
        else:
            stop_hit = hi >= p["stop_loss"]
            tp2_hit = lo <= p["tp2"]
            tp1_hit = lo <= p["tp1"]

        # 1. 止损（保守优先：同时触及止损与其他条件时按止损处理）
        if stop_hit:
            pnl = pnl_of(direction, fill_of(p), p["stop_loss"], mult, lots_open)
            cool = pnl <= 0
            advice = "已按规则离场。" + ("进入 {} 天冷却期。".format(cfg["money"]["cooldown_days"])
                                     if cool else "该笔未亏损，不设冷却。")
            events.append({
                "product": p["name"], "direction": dir_cn(direction),
                "action": ("止损保本离场" if p.get("tp1_done") else "止损离场"),
                "price": p["stop_loss"], "pnl": pnl,
                "advice": advice,
            })
            if cool:
                state["cooldowns"][p["product"]] = add_days(today, int(cfg["money"]["cooldown_days"]))
            archive_one(p, p["stop_loss"], pnl, "stop", today)
            changed = True
            continue  # 删除持仓

        # 2. 第二止盈（全平）
        if tp2_hit:
            pnl = pnl_of(direction, fill_of(p), p["tp2"], mult, lots_open)
            events.append({
                "product": p["name"], "direction": dir_cn(direction),
                "action": "第二止盈全平", "price": p["tp2"], "pnl": pnl,
                "advice": "已达止盈2，全部平仓落袋。",
            })
            archive_one(p, p["tp2"], pnl, "tp2", today)
            changed = True
            continue

        # 3. 第一止盈（减仓 + 止损上移保本）
        if tp1_hit and not p.get("tp1_done"):
            if lots_open <= 1:
                action = "第一止盈·上移保本"
                advice = ("单手持仓无法部分止盈：止损上移至保本 {}，继续持有博止盈2。"
                          .format(fill_of(p)))
                pnl = 0.0
            else:
                close_lots = int(math.floor(lots_open * float(cfg["exit"]["tp1_close_ratio"])))
                close_lots = max(1, min(close_lots, lots_open - 1))
                pnl = pnl_of(direction, fill_of(p), p["tp1"], mult, close_lots)
                p["lots_open"] = lots_open - close_lots
                action = "第一止盈·减仓{}手".format(close_lots)
                advice = ("已减仓{}手止盈，剩余{}手止损上移至保本 {}。"
                          .format(close_lots, p["lots_open"], fill_of(p)))
            p["stop_loss"] = fill_of(p)
            p["tp1_done"] = True
            events.append({
                "product": p["name"], "direction": dir_cn(direction),
                "action": action, "price": p["tp1"], "pnl": pnl, "advice": advice,
            })
            changed = True
            keep.append(p)
            continue

        # 4. 时间止损（满 N 个交易日）
        held = trading_days_held(cfg, p["product"], open_date, today)
        if held >= int(cfg["exit"]["max_hold_days"]):
            pnl = pnl_of(direction, fill_of(p), cur, mult, lots_open)
            events.append({
                "product": p["name"], "direction": dir_cn(direction),
                "action": "到期平仓", "price": cur, "pnl": pnl,
                "advice": "持仓满 {} 个交易日，按市价平仓。".format(cfg["exit"]["max_hold_days"]),
            })
            archive_one(p, cur, pnl, "time", today)
            changed = True
            continue

        # 无触发
        p["last_price"] = cur
        p["last_check"] = today
        keep.append(p)

    if changed:
        save_json(POSITION_PATH, keep)
        state["last_monitor_time"] = beijing_now().strftime("%Y-%m-%d %H:%M:%S")
        # 同步 last_close：取 closed_trades.json 最后一条
        all_closed = load_json(CLOSED_TRADES_PATH, [])
        if all_closed:
            last = all_closed[-1]
            state["last_close"] = {
                "id": last.get("id"),
                "product": last.get("product"),
                "pnl": last.get("pnl", 0),
                "close_price": last.get("close_price"),
                "exit_reason": last.get("exit_reason"),
            }
        save_json(STATE_PATH, state)

    if events:
        md = build_markdown(events, keep, rt, cfg)
        title = "【期货组合】持仓提醒 {} 项".format(len(events))
        push(title, md, cfg)
        print(md)
    else:
        print("无触发事件（{} 个持仓/挂单）".format(len(active)))


def build_markdown(events, keep, rt, cfg):
    lines = []
    lines.append("**时间**：{}".format(beijing_now().strftime("%Y-%m-%d %H:%M:%S")))
    lines.append("")
    lines.append("### 触发事件")
    lines.append("| 品种 | 方向 | 触发 | 价格 | 预估盈亏(元) | 操作建议 |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for e in events:
        lines.append("| {p} | {d} | {a} | {pr} | {pnl} | {ad} |".format(
            p=e["product"], d=e["direction"], a=e["action"], pr=e["price"],
            pnl=e["pnl"], ad=e["advice"]))

    open_pos = [p for p in keep if p.get("status") == "open"]
    pend = [p for p in keep if p.get("status") == "pending"]
    if open_pos or pend:
        lines.append("")
        lines.append("### 剩余持仓/挂单")
        lines.append("| 品种 | 方向 | 状态 | 成交/挂单价 | 现价 | 止损 | 止盈1 | 止盈2 | 剩余手数 | 浮动盈亏 |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for p in open_pos + pend:
            q = rt.get(cfg["products"][p["product"]]["sina"], {})
            cur = q.get("last", "")
            if p.get("status") == "open":
                fp = pnl_of(p["direction"], fill_of(p), cur, float(p["multiplier"]),
                            int(p.get("lots_open", p["lots"]))) if cur != "" else ""
            else:
                fp = "-"
            lines.append("| {n} | {d} | {st} | {e} | {c} | {sl} | {t1} | {t2} | {lo} | {fp} |".format(
                n=p["name"], d=dir_cn(p["direction"]),
                st="持仓中" if p["status"] == "open" else "待成交",
                e=p.get("fill_price", p.get("entry_price", "")), c=cur, sl=p["stop_loss"],
                t1=p["tp1"], t2=p["tp2"],
                lo=p.get("lots_open", p["lots"]), fp=fp))

    lines.append("")
    lines.append("> 风险提示：以上为机械规则触发结果，不构成投资建议。")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
