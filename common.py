# -*- coding: utf-8 -*-
"""
公共模块 common.py
------------------
职责：
1. 行情获取：新浪财经免费期货接口（日线 K 线 + 实时行情）
2. 技术指标：MA、ATR（Wilder）、量能、持仓量增量、收盘位置
3. 信号判定：build_signal()，返回信号与未通过原因
4. 仓位计算：calc_size()，风险倒推 + 保证金上限
5. 微信推送：Server酱³ / PushPlus / 企业微信群机器人（多通道，配了哪个用哪个）
6. 持仓文件读写：position.json / state.json

仅使用 Python 标准库，无需 pip 安装第三方依赖。
"""
from __future__ import annotations

import os
import re
import json
import math
import time
import datetime as dt
import urllib.request
import urllib.parse

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "config.json")
POSITION_PATH = os.path.join(ROOT, "position.json")
STATE_PATH = os.path.join(ROOT, "state.json")


# ============================================================
# 基础 IO
# ============================================================
def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_config():
    return load_json(CONFIG_PATH, {})


def beijing_now():
    """北京时间（UTC+8）。服务器/CI 通常为 UTC，用固定偏移避免时区问题。"""
    return dt.datetime.utcnow() + dt.timedelta(hours=8)


# ============================================================
# 交易日历（周末 + 法定休市日）
# ============================================================
def _fetch_holiday_year(year):
    """在线获取某年法定休市日（holiday=true 为放假休市）。失败返回空集合。"""
    out = set()
    try:
        txt = http_get("https://timor.tech/api/holiday/year/{}".format(year),
                       timeout=12, retries=2)
        data = json.loads(txt)
        for _k, v in (data.get("holiday") or {}).items():
            if v.get("holiday"):
                out.add(v["date"])
    except Exception:
        pass
    return out


def market_holidays(cfg, around=None):
    """法定休市日集合：在线获取今年/明年（自动更新），失败时回退 config['holidays']。"""
    base = set((cfg or {}).get("holidays", []))
    y = (around or beijing_now().date()).year
    base |= _fetch_holiday_year(y) | _fetch_holiday_year(y + 1)
    return base


def next_trading_day(start, holidays=None):
    """start 之后的下一个交易日（跳过周末与法定休市日）。"""
    holidays = holidays or set()
    d = start + dt.timedelta(days=1)
    while d.weekday() >= 5 or d.isoformat() in holidays:
        d += dt.timedelta(days=1)
    return d


def is_trading_day(day, cfg=None, holidays=None):
    """某日是否为期货交易日：周一~周五 且 非法定休市日。
    注：调休补班的周末期货市场仍休市，无需特殊处理。"""
    if day.weekday() >= 5:
        return False
    holidays = holidays if holidays is not None else market_holidays(cfg)
    return day.isoformat() not in holidays


def is_pre_closure_day(today, holidays=None, min_gap=3):
    """今日是否为「周末/长假前最后一个交易日」：距下一交易日 >= min_gap 天（周末为 3 天）。"""
    return (next_trading_day(today, holidays) - today).days >= min_gap


# ============================================================
# HTTP（标准库，自动走环境变量代理）
# ============================================================
def http_get(url, headers=None, timeout=20, encoding="utf-8", retries=3):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers or {"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
            try:
                return raw.decode(encoding)
            except Exception:
                return raw.decode(encoding, errors="ignore")
        except Exception as e:  # noqa
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError("请求失败 {}: {}".format(url, last))


def http_post(url, form=None, json_body=None, headers=None, timeout=20, retries=3):
    headers = dict(headers or {})
    if json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    else:
        body = urllib.parse.urlencode(form or {}).encode("utf-8")
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    headers.setdefault("User-Agent", "Mozilla/5.0")
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="ignore")
        except Exception as e:  # noqa
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError("POST 失败 {}: {}".format(url, last))


# ============================================================
# 行情数据
# ============================================================
def fetch_daily(symbol, days=160):
    """新浪期货日线。symbol 例如 M0 / C0 / RM0（主力连续）。"""
    url = ("https://stock.finance.sina.com.cn/futures/api/jsonp.php/"
           "var%20_{0}=/InnerFuturesNewService.getDailyKLine?symbol={0}".format(symbol))
    txt = http_get(url)
    m = re.search(r"\((\[.*\])\)", txt, re.S)
    if not m:
        raise RuntimeError("日线解析失败: {}".format(symbol))
    arr = json.loads(m.group(1))
    bars = []
    for r in arr:
        try:
            bars.append({
                "date": r["d"],
                "open": float(r["o"]),
                "high": float(r["h"]),
                "low": float(r["l"]),
                "close": float(r["c"]),
                "volume": float(r["v"]),
                "oi": float(r["p"]),
                "settle": float(r.get("s") or 0),
            })
        except Exception:
            continue
    return bars[-days:]


def fetch_realtime(symbols):
    """新浪实时行情。symbols 例如 ['M0','C0']。返回 {symbol: {...}}。"""
    if not symbols:
        return {}
    url = "https://hq.sinajs.cn/list=" + ",".join("nf_" + s for s in symbols)
    txt = http_get(url, headers={"Referer": "https://finance.sina.com.cn",
                                 "User-Agent": "Mozilla/5.0"}, encoding="gbk")
    out = {}
    for line in txt.splitlines():
        m = re.match(r'var hq_str_nf_(\w+)="(.*?)";', line.strip())
        if not m:
            continue
        sym, payload = m.group(1), m.group(2)
        f = payload.split(",")
        if len(f) < 15:
            continue
        try:
            out[sym] = {
                "name": f[0],
                "time": f[1],
                "open": float(f[2]),
                "high": float(f[3]),
                "low": float(f[4]),
                "last": float(f[8]),
                "oi": float(f[13]),
                "volume": float(f[14]),
                "date": f[17] if len(f) > 17 else dt.date.today().isoformat(),
            }
        except Exception:
            continue
    return out


# ============================================================
# 技术指标
# ============================================================
def sma(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else 0.0


def wilder_atr(bars, period=20):
    """Wilder 平滑 ATR，返回与 bars 等长的列表（atr[0]=None）。"""
    atr = [None] * len(bars)
    if len(bars) < 2:
        return atr
    a = None
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        a = tr if a is None else (a * (period - 1) + tr) / period
        atr[i] = a
    return atr


def pct_rank(v, arr):
    arr = [x for x in arr if x is not None]
    if not arr:
        return 0.0
    return sum(1 for x in arr if x <= v) / len(arr)


# ============================================================
# 仓位计算
# ============================================================
def calc_size(cfg, product, entry, stop):
    """按单笔最大亏损倒推手数，并受单品种保证金比例约束。"""
    money = cfg["money"]
    p = cfg["products"][product]
    mult = float(p["multiplier"])
    tick = float(p["tick"])
    slip = float(cfg["exit"]["slippage_ticks"]) * tick
    capital = float(money["initial_capital"])

    risk_budget = capital * float(money["risk_per_trade"])
    stop_dist = abs(entry - stop) + slip          # 含滑点
    risk_per_lot = stop_dist * mult
    lots_risk = int(math.floor(risk_budget / risk_per_lot)) if risk_per_lot > 0 else 0

    margin_per_lot = entry * mult * float(p["margin_rate"])
    lots_margin = int(math.floor(capital * float(money["max_margin_ratio"]) / margin_per_lot)) \
        if margin_per_lot > 0 else 0

    lots = min(lots_risk, lots_margin)
    note = "风险测算{}手 / 保证金上限{}手".format(lots_risk, lots_margin)

    if lots < 1 and money.get("allow_min_one_lot", True):
        if (risk_per_lot <= capital * float(money["max_risk_pct_hard"])
                and margin_per_lot <= capital * float(money["margin_hard_ratio"])):
            lots = 1
            note += "；按小资金最小1手开仓"

    return {
        "lots": lots,
        "risk_per_lot": round(risk_per_lot, 1),
        "margin_per_lot": round(margin_per_lot, 1),
        "risk_budget": round(risk_budget, 1),
        "note": note,
    }


# ============================================================
# 信号判定
# ============================================================
def build_signal(cfg, product, bars):
    """
    对某品种计算当日信号。
    bars: 由旧到新的日线列表，最后一根为“当日完整K线”。
    返回 (signal_dict 或 None, 原因字符串)
    """
    f = cfg["filters"]
    ex = cfg["exit"]
    p = cfg["products"][product]

    need = max(f["ma_period"], f["atr_period"], f["atr_ma_period"]) + f["ma_slope_lookback"] + 5
    if len(bars) < need:
        return None, "历史数据不足({}<{})".format(len(bars), need)

    closes = [b["close"] for b in bars]
    vols = [b["volume"] for b in bars]
    ois = [b["oi"] for b in bars]
    t = bars[-1]

    if t["open"] <= 0 or t["close"] <= 0 or t["volume"] <= 0:
        return None, "当日数据异常"

    # --- 均线体系 ---
    ma20 = sma(closes[-f["ma_period"]:])
    ma20_prev = sma(closes[-(f["ma_period"] + f["ma_slope_lookback"]):-f["ma_slope_lookback"]])
    ma60 = sma(closes[-f["ma60_period"]:]) if len(closes) >= f["ma60_period"] else None

    # --- 量能 ---
    vol_ma20 = sma(vols[-(f["vol_ma_period"] + 1):-1])   # 前20日均量（不含当日）
    vol_ratio = t["volume"] / vol_ma20 if vol_ma20 > 0 else 0.0
    vol_rank = pct_rank(t["volume"], vols[-60:])         # 近60日成交量分位

    # --- 持仓量增量 ---
    deltas = [ois[-1 - k] - ois[-2 - k] for k in range(1, f["oi_ma_period"] + 1)]
    oi_delta_today = ois[-1] - ois[-2]
    oi_delta_ma = sma(deltas)
    # 阈值：1.5倍均量，同时设绝对下限（避免均量过小/为负导致的失真）
    oi_thr = max(f["oi_mult"] * oi_delta_ma, f["oi_abs_floor"] * ois[-2])

    # --- 波动率 ---
    atr = wilder_atr(bars, f["atr_period"])
    atr20 = atr[-1] or 0.0
    atr_ma = sma(atr[-f["atr_ma_period"]:])

    # --- 实体与收盘位置 ---
    rng = t["high"] - t["low"]
    close_pos = (t["close"] - t["low"]) / rng if rng > 0 else 0.5
    body_pct = abs(t["close"] - t["open"]) / t["open"]
    body_thr = max(f["body_min_pct"], f["body_atr_ratio"] * atr20 / t["close"])

    # ---------- 过滤（必须同时满足）----------
    if vol_ma20 <= 0:
        return None, "无量能基线"
    vol_ok = (vol_ratio >= f["vol_mult"]) and (vol_rank >= f["vol_pct_rank_min"])
    if not vol_ok:
        return None, "量能不足(量比{:.2f} / 分位{:.2f})".format(vol_ratio, vol_rank)

    oi_ok = (oi_delta_today > 0) and (oi_delta_today >= oi_thr)
    if not oi_ok:
        return None, "持仓增量不足({:.0f} < {:.0f})".format(oi_delta_today, oi_thr)

    if body_pct < body_thr:
        return None, "K线实体不足({:.2f}% < {:.2f}%)".format(body_pct * 100, body_thr * 100)

    atr_ok = (atr_ma > 0) and (atr20 >= atr_ma * f["atr_mult"])
    if not atr_ok:
        return None, "波动率偏低(ATR低于20日均线{:.0f}%)".format(f["atr_mult"] * 100)

    # ---------- 方向判定 ----------
    require_slope = f.get("require_ma_slope", True)
    long_ok = (
        t["close"] > ma20
        and (ma20 > ma20_prev or not require_slope)
        and (not f["require_ma60"] or ma60 is None or t["close"] > ma60)
        and t["close"] > t["open"]
        and close_pos >= f["close_pos_min_long"]
    )
    short_ok = (
        t["close"] < ma20
        and (ma20 < ma20_prev or not require_slope)
        and (not f["require_ma60"] or ma60 is None or t["close"] < ma60)
        and t["close"] < t["open"]
        and close_pos <= f["close_pos_max_short"]
    )
    if not (long_ok or short_ok):
        return None, "趋势/收盘位置不满足"
    direction = "long" if long_ok else "short"

    # ---------- 价格计算 ----------
    tick = float(p["tick"]) * f["entry_buffer_ticks"]
    if direction == "long":
        entry = round(t["high"] + tick, 2)
        stop = round(entry - ex["stop_atr_mult"] * atr20, 2)
        tp1 = round(entry + ex["tp1_atr_mult"] * atr20, 2)
        tp2 = round(entry + ex["tp2_atr_mult"] * atr20, 2)
    else:
        entry = round(t["low"] - tick, 2)
        stop = round(entry + ex["stop_atr_mult"] * atr20, 2)
        tp1 = round(entry - ex["tp1_atr_mult"] * atr20, 2)
        tp2 = round(entry - ex["tp2_atr_mult"] * atr20, 2)

    size = calc_size(cfg, product, entry, stop)

    return {
        "product": product,
        "name": p["name"],
        "direction": direction,
        "date": t["date"],
        "close": t["close"],
        "open": t["open"],
        "high": t["high"],
        "low": t["low"],
        "entry": entry,
        "stop": stop,
        "tp1": tp1,
        "tp2": tp2,
        "atr20": round(atr20, 2),
        "lots": size["lots"],
        "risk_per_lot": size["risk_per_lot"],
        "margin_per_lot": size["margin_per_lot"],
        "size_note": size["note"],
        "vol_ratio": vol_ratio,
        "vol_rank": vol_rank,
        "oi_delta": oi_delta_today,
        "oi_delta_ma": oi_delta_ma,
        "body_pct": body_pct,
        "close_pos": close_pos,
        "ma20": round(ma20, 2),
        "ma60": round(ma60, 2) if ma60 else None,
    }, "OK"


# ============================================================
# 微信推送（多通道）
# ============================================================
def _read_secret_file(path):
    """读取密钥文件（去除首尾空白）。文件不存在或读取失败返回空串。"""
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
    except Exception:
        pass
    return ""


def _channels(cfg):
    ch = []
    # Server酱³：优先环境变量，其次本地 .serverchan_key 文件
    key = os.environ.get("SERVERCHAN_SENDKEY", "").strip() \
        or _read_secret_file(os.path.join(ROOT, ".serverchan_key"))
    if key:
        ch.append(("Server酱³", key))
    tok = os.environ.get("PUSHPLUS_TOKEN", "").strip()
    if tok:
        ch.append(("PushPlus", tok))
    hook = os.environ.get("WECOM_WEBHOOK", "").strip()
    if hook:
        ch.append(("企业微信", hook))
    return ch


def _check_api(resp, name):
    """校验推送接口返回码；非成功则抛异常，避免“未送达却报成功”。"""
    try:
        r = json.loads(resp)
    except Exception:
        return
    code = r.get("code", r.get("errcode", 0))
    if code not in (0, None):
        msg = r.get("message") or r.get("errmsg") or r.get("data") or ""
        raise RuntimeError("接口返回 code={} {}".format(code, msg))


def push(title, markdown, cfg=None):
    cfg = cfg or load_config()
    ch = _channels(cfg)
    if not ch:
        print("[push] 未配置任何推送渠道，以下为推送内容预览：")
        print("=" * 50)
        print(title)
        print(markdown)
        print("=" * 50)
        return False

    ok = False
    for name, val in ch:
        try:
            if name == "Server酱³":
                resp = http_post("https://sctapi.ftqq.com/{}.send".format(val),
                                 form={"title": title[:100], "desp": markdown})
                _check_api(resp, name)
            elif name == "PushPlus":
                resp = http_post("https://www.pushplus.plus/send",
                                 json_body={"token": val, "title": title,
                                            "content": markdown, "template": "markdown"})
                _check_api(resp, name)
            elif name == "企业微信":
                resp = http_post(val, json_body={"msgtype": "markdown",
                                                 "markdown": {"content": "**{}**\n{}".format(title, markdown)}})
                _check_api(resp, name)
            print("[push] {} 已发送".format(name))
            ok = True
        except Exception as e:  # noqa
            print("[push] {} 发送失败: {}".format(name, e))
    return ok
