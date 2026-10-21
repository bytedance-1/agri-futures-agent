# -*- coding: utf-8 -*-
"""
rule_evolution.py —— 交易规则与系统安全进化管道
--------------------------------------------------
运行时机：周一 08:35，紧跟 weekly_report.py 之后。
核心约束（用户反复强调）：
  1. 不能亏一次就草率改规则
  2. 必须长期学习、回测验证
  3. 只有明确正向收益才允许调整
  4. 改动要慎重

进化范围从"交易规则参数"扩展到"系统运行参数"：
  - 信号过滤参数（filter）
  - 出场参数（exit）
  - 系统执行参数（减仓比例、滑点、节前强平等）

四层闸门：
  闸门 1 - 冷启动期：closed_trades 中真实平仓 < MIN_SAMPLE 笔 → 只输出建议，不自动改
  闸门 2 - 候选约束：最多 3 个候选，仅调 filter/exit/系统执行参数，不动核心风控
  闸门 3 - 回测门槛：胜率 ↑ ≥2%、回撤 ≤ 原+1%、总 PnL ↑ ≥10%、笔数 ≥ 原 60%、样本 ≥30（全部满足）
  闸门 4 - 锁定期：上次升级后 28 天内不再升级

升级动作：
  - 先把 config.json 备份为 config_YYYYMMDD_prev.json
  - 再替换 config.json
  - 在 state.json 写 evolution_history.append({date, from, to, reason, backtest})
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import math
import os

from backtest import simulate, stats
from common import (CONFIG_PATH, CLOSED_TRADES_PATH, STATE_PATH, beijing_now,
                    fetch_daily, load_closed_trades, load_config, load_json,
                    market_holidays, push, save_json)


# ---- 闸门参数 ----
MIN_SAMPLE = 10                     # 冷启动期：真实平仓样本 ≥ 这个数才允许自动进化
LOCK_DAYS = 28                      # 升级后锁定期
WIN_RATE_MIN_DELTA = 0.02           # 胜率提升 ≥ 2%
MDD_MAX_DELTA = 0.01                # 允许的最大回撤恶化幅度
ANNUAL_RETURN_MIN_DELTA = 0.05      # 年化收益率提升 ≥ 5%
TRADE_COUNT_MIN_RATIO = 0.6         # 交易笔数 ≥ 原规则 60%
MIN_BACKTEST_TRADES = 30            # 回测样本至少 30 笔才有统计意义

# ---- 允许调整的参数路径（禁止改动 correlation_groups / max_positions / risk_per_trade 等核心风控）----
TUNABLES = [
    # filters
    ("filters", "vol_mult", [1.2, 1.3, 1.4, 1.5, 1.6]),
    ("filters", "vol_pct_rank_min", [0.5, 0.55, 0.6, 0.65, 0.7]),
    ("filters", "atr_mult", [0.8, 0.85, 0.9, 0.95, 1.0]),
    ("filters", "body_min_pct", [0.004, 0.005, 0.006, 0.007, 0.008]),
    ("filters", "entry_buffer_ticks", [0, 1, 2, 3]),
    # exit
    ("exit", "stop_atr_mult", [1.0, 1.2, 1.4, 1.5, 1.6, 1.8]),
    ("exit", "tp1_atr_mult", [1.2, 1.3, 1.5, 1.7, 1.8]),
    ("exit", "tp2_atr_mult", [2.0, 2.3, 2.5, 2.7, 3.0]),
    ("exit", "max_hold_days", [3, 4, 5, 6]),
    ("exit", "tp1_close_ratio", [0.3, 0.4, 0.5, 0.6, 0.7]),
    ("exit", "slippage_ticks", [0, 1, 2, 3]),
    # 系统执行参数
    (None, "flatten_before_holiday", [True, False]),
]


def getnested(cfg, path):
    # path 形如 ("filters", "vol_mult") 或 (None, "flatten_before_holiday")
    cur = cfg
    for k in path:
        if k is None:
            continue
        cur = cur[k]
    return cur


def setnested(cfg, path, val):
    cur = cfg
    keys = [k for k in path if k is not None]
    for k in keys[:-1]:
        cur = cur[k]
    cur[keys[-1]] = val


def apply_tune(base_cfg, tune):
    """把一个 tune 列表 [(path, val), ...] 应用到副本。"""
    new = copy.deepcopy(base_cfg)
    for path, val in tune:
        setnested(new, path, val)
    return new


def param_label(path, name):
    """把参数路径格式化成人类可读标签，支持顶层参数（path 含 None）。"""
    keys = [k for k in path if k is not None]
    if keys:
        return ".".join(keys)
    return name


def grid_search(cfg, products, data, baseline_stats, capital, holidays):
    """对每个 TUNABLE 做单参数网格搜索，返回 top-3 候选（含回测指标、是否过闸门）。"""
    candidates = []
    for path, name, options in TUNABLES:
        cur_val = getnested(cfg, path)
        if cur_val in options:
            continue  # 当前值已在网格里
        for opt in options:
            if opt == cur_val:
                continue
            tune = [(path, opt)]
            new_cfg = apply_tune(cfg, tune)
            trades_by_prod = {p: simulate(new_cfg, p, data[p], holidays) for p in products}
            all_tr = []
            for tr in trades_by_prod.values():
                all_tr += tr
            s = stats(all_tr)
            if not s:
                continue
            gate = check_gates(s, baseline_stats, capital)
            candidates.append({
                "path": path, "name": name,
                "from": cur_val, "to": opt,
                "stats": s, "pass": gate["pass"],
                "reasons": gate["reasons"],
            })
    # 优先过闸门，其次胜率提升最大
    candidates.sort(key=lambda c: (c["pass"], c["stats"]["win_rate"]), reverse=True)
    return candidates[:3]


def check_gates(new_stats, base_stats, capital):
    """四层闸门判断，返回 {pass: bool, reasons: [str]}。"""
    reasons = []
    # 闸门 3a：胜率提升
    if new_stats["win_rate"] < base_stats["win_rate"] + WIN_RATE_MIN_DELTA:
        reasons.append("胜率提升仅 {:.0%} < 要求 {:.0%}".format(
            new_stats["win_rate"] - base_stats["win_rate"], WIN_RATE_MIN_DELTA))
    # 闸门 3b：最大回撤
    if new_stats["mdd"] > base_stats["mdd"] * (1 + MDD_MAX_DELTA / 100):
        reasons.append("最大回撤 {:.0f} > 允许上限 {:.0f}".format(
            new_stats["mdd"], base_stats["mdd"] * (1 + MDD_MAX_DELTA / 100)))
    # 闸门 3c：总 PnL + 年化（近似：总 PnL 提升 ≥ 10%）
    if new_stats["total"] < base_stats["total"] * 1.1:
        reasons.append("总 PnL 提升 {:.0f} < 要求 10%（基线 {:.0f}）".format(
            new_stats["total"] - base_stats["total"], base_stats["total"]))
    # 闸门 3d：交易笔数
    if new_stats["n"] < base_stats["n"] * TRADE_COUNT_MIN_RATIO:
        reasons.append("交易笔数 {} < 要求 ≥ {}（基线 {} 的 60%）".format(
            new_stats["n"], math.ceil(base_stats["n"] * TRADE_COUNT_MIN_RATIO), base_stats["n"]))
    # 最低样本量
    if new_stats["n"] < MIN_BACKTEST_TRADES:
        reasons.append("回测样本 {} 笔 < 统计显著下限 {} 笔".format(new_stats["n"], MIN_BACKTEST_TRADES))
    return {"pass": len(reasons) == 0, "reasons": reasons}


def is_locked(state):
    last_upgrade = state.get("last_rule_upgrade")
    if not last_upgrade:
        return False
    last = dt.date.fromisoformat(last_upgrade)
    return (beijing_now().date() - last).days < LOCK_DAYS


def real_sample_count():
    closed = load_closed_trades(CLOSED_TRADES_PATH)
    return sum(1 for t in closed if t.get("pnl", 0) != 0)


def backup_config(cfg_path, capital="initial_capital"):
    today = beijing_now().strftime("%Y%m%d")
    backup_path = cfg_path.replace(".json", "_{}_prev.json".format(today))
    os.rename(cfg_path, backup_path)
    return backup_path


def main():
    cfg = load_config()
    state = load_json(STATE_PATH, {"evolution_history": []})
    capital = float(cfg["money"]["initial_capital"])

    now = beijing_now()
    sample_n = real_sample_count()
    locked = is_locked(state)

    md_parts = []
    md_parts.append("**进化检查时间**：{}".format(now.strftime("%Y-%m-%d %H:%M")))
    md_parts.append("**冷启动样本**：真实平仓 {} 笔（下限 {}）".format(sample_n, MIN_SAMPLE))
    md_parts.append("**锁定期状态**：{}".format("🔒 锁定中（{} 天内不升级）".format(LOCK_DAYS)
                                               if locked else "✅ 可升级"))
    md_parts.append("")

    # ---- 拉历史数据 ----
    products = list(cfg["products"].keys())
    holidays = market_holidays(cfg)
    try:
        data = {p: fetch_daily(cfg["products"][p]["sina"], 900) for p in products}
    except Exception as e:
        md_parts.append("❌ 历史数据拉取失败：{}".format(e))
        push("【规则进化】数据拉取失败", "\n".join(md_parts), cfg)
        return

    # ---- 基线回测 ----
    base_tr = []
    for p in products:
        base_tr += simulate(cfg, p, data[p], holidays)
    base_stats = stats(base_tr) or {"n": 0, "win_rate": 0, "total": 0, "mdd": 0}

    md_parts.append("### 基线回测（当前 config.json）")
    md_parts.append("| 笔数 | 胜率 | 总PnL | 最大回撤 | 盈亏比 |")
    md_parts.append("| --- | --- | --- | --- | --- |")
    pf = base_stats.get("pf", "-")
    if isinstance(pf, float) and math.isfinite(pf):
        pf = "{:.2f}".format(pf)
    md_parts.append("| {} | {:.0%} | {:.0f} 元 | {:.0f} 元 | {} |".format(
        base_stats["n"], base_stats["win_rate"], base_stats["total"],
        base_stats["mdd"], pf))
    md_parts.append("")

    # ---- 冷启动期：样本不足 ----
    if sample_n < MIN_SAMPLE:
        md_parts.append("### 冷启动期：样本不足")
        md_parts.append("当前真实平仓仅 {} 笔（需要 ≥ {} 笔才能进入候选生成）。".format(
            sample_n, MIN_SAMPLE))
        md_parts.append("规则进化暂缓执行，待样本积累后再评估。")
        md_parts.append("")
        md_parts.append("> 设计意图：样本太少时任何参数微调都是过拟合，必须先让规则跑起来积累真实数据。")
        push("【规则进化】冷启动期（{} 笔样本）".format(sample_n), "\n".join(md_parts), cfg)
        print("\n".join(md_parts))
        return

    # ---- 锁定期 ----
    if locked:
        md_parts.append("### 锁定期内")
        md_parts.append("上次升级日期：{}，{} 天内不再升级。".format(
            state["last_rule_upgrade"], LOCK_DAYS))
        push("【规则进化】锁定期", "\n".join(md_parts), cfg)
        print("\n".join(md_parts))
        return

    # ---- 候选生成 + 回测对比 ----
    candidates = grid_search(cfg, products, data, base_stats, capital, holidays)

    md_parts.append("### Top-3 候选参数")
    md_parts.append("| 参数 | 当前值 | 候选值 | 笔数 | 胜率 | 总PnL | 回撤 | 通过闸门 |")
    md_parts.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    best = None
    for c in candidates:
        pf = c["stats"].get("pf", "-")
        if isinstance(pf, float) and math.isfinite(pf):
            pf = "{:.2f}".format(pf)
        label = param_label(c["path"], c["name"])
        md_parts.append("| {} | {} → **{}** | {} | {:.0%} | {:.0f} | {:.0f} | {} |".format(
            label, c["from"], c["to"],
            c["stats"]["n"], c["stats"]["win_rate"], c["stats"]["total"],
            c["stats"]["mdd"], "✅ 全部通过" if c["pass"] else "❌"))
        if c["pass"]:
            if best is None or c["stats"]["win_rate"] > best["stats"]["win_rate"]:
                best = c
    md_parts.append("")

    # 候选失败的原因
    for c in candidates:
        if not c["pass"]:
            label = param_label(c["path"], c["name"])
            md_parts.append("**{} → {}** 未通过闸门：".format(label, c["to"]))
            for r in c["reasons"]:
                md_parts.append("  - ❌ {}".format(r))
    md_parts.append("")

    # ---- 执行升级 ----
    if best is None:
        md_parts.append("### 结论：无合格候选")
        md_parts.append("所有候选均未通过全部闸门，保持当前 config.json 不变。")
        push("【规则进化】本周无升级", "\n".join(md_parts), cfg)
        print("\n".join(md_parts))
        return

    # ---- 备份 → 更新 → 记录 ----
    backup = backup_config(CONFIG_PATH)
    setnested(cfg, best["path"], best["to"])
    save_json(CONFIG_PATH, cfg)

    state.setdefault("evolution_history", []).append({
        "date": beijing_now().strftime("%Y-%m-%d"),
        "from_param": best["path"],
        "from_value": best["from"],
        "to_value": best["to"],
        "reason": "网格搜索 top-1 候选，4 层闸门全部通过",
        "baseline": base_stats,
        "candidate": best["stats"],
    })
    state["last_rule_upgrade"] = beijing_now().date().isoformat()
    save_json(STATE_PATH, state)

    md_parts.append("### ✅ 自动升级已执行")
    label = param_label(best["path"], best["name"])
    md_parts.append("**升级项**：{} {} → {}".format(
        label, best["from"], best["to"]))
    md_parts.append("**备份**：{}".format(backup))
    md_parts.append("**锁定期**：{} 天内不再自动升级".format(LOCK_DAYS))
    md_parts.append("")
    md_parts.append("**回测对比**：")
    md_parts.append("| 版本 | 笔数 | 胜率 | 总PnL | 最大回撤 |")
    md_parts.append("| --- | --- | --- | --- | --- |")
    md_parts.append("| 升级前 | {} | {:.0%} | {:.0f} | {:.0f} |".format(
        base_stats["n"], base_stats["win_rate"], base_stats["total"], base_stats["mdd"]))
    md_parts.append("| **升级后** | **{}** | **{:.0%}** | **{:.0f}** | **{:.0f}** |".format(
        best["stats"]["n"], best["stats"]["win_rate"], best["stats"]["total"], best["stats"]["mdd"]))
    md_parts.append("")
    md_parts.append("> 下一步：请手动 review 备份文件，确认无异常后删除锁定期，或等待 {} 天后自动再次检查。".format(LOCK_DAYS))

    push("【规则进化】✅ 自动升级", "\n".join(md_parts), cfg)
    print("\n".join(md_parts))


if __name__ == "__main__":
    main()
