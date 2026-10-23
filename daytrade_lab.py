#!/usr/bin/env python3
"""
爆量雷達 - 當沖規則回測(收盤後由 bear.yml 在 bear_signals.py 之後執行;也可以自己跑:python daytrade_lab.py)

資料:Yahoo 約 60 個交易日的 5 分K(自選股個股,不含 ETF),每天存進 data/bars5m/ 永久保存,
回測用「存檔 + Yahoo 最新」,樣本會一天一天變多。一天少於 45 根的(冷門股缺 K)不算。
前一天收盤用 Yahoo 日收盤(含 13:30 集合競價)。

規則(都只用「當下已經收完的 K 棒」判斷,下一根開盤價成交):
  orb_long   開盤區間突破做多:09:15 後第一根收盤站上 09:00-09:15 最高價
  orb_short  開盤區間跌破做空:09:15 後第一根收盤跌破 09:00-09:15 最低價
  gap_fade_s 開高放空:開盤比昨收高 2% 以上,09:05 放空
  gap_fade_l 開低承接:開盤比昨收低 2% 以上,09:05 買進
  vwap_long  站上均價線:09:30 後由下往上穿過當天均價線,且漲跌幅 > 0
  vwap_short 跌破均價線:09:30 後由上往下跌破當天均價線,且漲跌幅 < 0
  mom_long   強勢續攻:10:00 那根收盤漲 >= 3% 且在均價線之上
  mom_short  弱勢續跌:10:00 那根收盤跌 >= 3% 且在均價線之下
  bear_bounce_s 利空隔天反彈空:前一天收盤出現「有效/觀察」利空訊號,09:15-10:30 反彈到昨收 0.5% 內就放空
  score_long / score_short       多空分數(bias.py)09:15-12:30 第一次 >= 60 / <= -60
  score_long_80 / score_short_80 同上,門檻 80
  pb_long / pb_short   拉回均價線:分數先到 >= 60(<= -60),之後某根最低價回到均價線 0.2% 內、收盤仍在均價線同一側、分數仍同方向
  fade_long / fade_short 偏離反向:分數 <= -80 且現價低於均價線 2% 以上做多;分數 >= 80 且高於均價線 2% 以上放空
    (這四條是 2026-10-23 追價版回測沒過後,事先寫死條件的第二版,不再依回測結果調整)
    分數用到的大盤是 Yahoo ^TWII 5 分K;量能倍數 = 累積量 ÷ 前 20 天同一時間累積量的中位數(至少 5 天)。
出場:停損 1% / 1.5% / 2.5%,停利 1% / 2% / 3% / 不設(抱到 13:20 那根收盤出場),12 種組合。
  同一根 K 同時碰到停損和停利,一律當作先停損(保守);跳空越過停損,用該根開盤價出。
成本:來回 COST_PCT(手續費買賣各 0.1425% 打 6 折 + 當沖證交稅 0.15%,約 0.3%)。
判斷:前 TRAIN_FRAC 交易日挑參數(訓練),之後只用來驗證。
  「有優勢」= 訓練段和驗證段扣成本後平均都 > 0,而且以「天」為單位的 t 值都 >= 2,驗證段至少 30 筆。

產出:data/daytrade.json。這是歷史統計,不是預測,也不是買賣建議。
"""

import gzip
import json
import os
import statistics as stx
import sys
import time
from datetime import datetime

import requests

import bear_signals as bs
import bias
from intraday import BIAS_LOG_FILE, DATA_DIR, GROUP_OF, HISTORY_FILE, TAIPEI, load_json_safe, log_bias_day, write_json_atomic

OUT_FILE = os.path.join(DATA_DIR, "daytrade.json")
BARS_DIR = os.path.join(DATA_DIR, "bars5m")   # 每天一個 .json.gz,永久保存;Yahoo 只給最近 60 天,靠這裡讓樣本越累積越多
INDEX_KEY = "^TWII"
COST_PCT = 0.30
TRAIN_FRAC = 2 / 3
T_MIN = 2.0
MIN_TEST_N = 30
MIN_BARS_DAY = 45
STOPS = (1.0, 1.5, 2.5)
TARGETS = (1.0, 2.0, 3.0, None)
GAP_PCT = 2.0
MOM_PCT = 3.0
LAST_BAR = "13:20"         # 當沖要在 13:30 前了結,用 13:20 那根收盤出場
SCORE_FROM, SCORE_TO = "09:15", "12:30"   # 多空分數只在這段時間找進場點
PACE_DAYS, PACE_MIN_DAYS = 20, 5
PB_TOL_PCT = bias.PB_TOL_PCT              # 拉回均價線、偏離反向的門檻都定義在 bias.py,盤中共用
FADE_PCT = bias.FADE_PCT

RULES = {
    "orb_long": ("開盤區間突破做多", "long", "09:15 後收盤站上 09:00-09:15 最高價"),
    "orb_short": ("開盤區間跌破做空", "short", "09:15 後收盤跌破 09:00-09:15 最低價"),
    "gap_fade_s": ("開高放空", "short", f"開盤比昨收高 {GAP_PCT:g}% 以上,09:05 放空"),
    "gap_fade_l": ("開低承接", "long", f"開盤比昨收低 {GAP_PCT:g}% 以上,09:05 買進"),
    "vwap_long": ("站上均價線做多", "long", "09:30 後由下往上穿過均價線,且當天上漲"),
    "vwap_short": ("跌破均價線做空", "short", "09:30 後由上往下跌破均價線,且當天下跌"),
    "mom_long": ("強勢續攻", "long", f"10:00 那根收盤漲 >= {MOM_PCT:g}% 且在均價線之上"),
    "mom_short": ("弱勢續跌", "short", f"10:00 那根收盤跌 >= {MOM_PCT:g}% 且在均價線之下"),
    "bear_bounce_s": ("利空隔天反彈空", "short", "前一天出現有效/觀察利空訊號,09:15-10:30 反彈到昨收 0.5% 內放空"),
    "score_long": ("多空分數做多", "long", f"09:15-12:30 多空分數第一次 >= {bias.THRESHOLD}"),
    "score_short": ("多空分數放空", "short", f"09:15-12:30 多空分數第一次 <= -{bias.THRESHOLD}"),
    "score_long_80": ("多空分數做多(強)", "long", f"09:15-12:30 多空分數第一次 >= {bias.STRONG}"),
    "score_short_80": ("多空分數放空(強)", "short", f"09:15-12:30 多空分數第一次 <= -{bias.STRONG}"),
    "pb_long": ("拉回均價線做多", "long", f"分數先 >= {bias.THRESHOLD},之後拉回均價線 {PB_TOL_PCT:g}% 內且收在均價線上"),
    "pb_short": ("反彈均價線放空", "short", f"分數先 <= -{bias.THRESHOLD},之後反彈到均價線 {PB_TOL_PCT:g}% 內且收在均價線下"),
    "fade_long": ("超跌反向做多", "long", f"分數 <= -{bias.STRONG} 且低於均價線 {FADE_PCT:g}% 以上"),
    "fade_short": ("超漲反向放空", "short", f"分數 >= {bias.STRONG} 且高於均價線 {FADE_PCT:g}% 以上"),
}


def fetch_5m(codes, ev):
    """{code: {日期: [{"t":"09:00","o","h","l","c","v"}, ...]}},只留 K 棒數夠的日子。"""
    out, started = {}, time.time()
    for code in codes:
        if time.time() - started > bs.YAHOO_BUDGET_SECONDS:
            break
        _, res = bs._yahoo(code, {"interval": "5m", "range": "60d"}, (ev.get(code) or {}).get("suf"))
        if not res:
            continue
        q = res["indicators"]["quote"][0]
        days = {}
        for i, t in enumerate(res["timestamp"]):
            o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
            if not (o and h and l and c):
                continue
            dt_ = datetime.fromtimestamp(t, TAIPEI)
            days.setdefault(dt_.strftime("%Y-%m-%d"), []).append(
                {"t": dt_.strftime("%H:%M"), "o": o, "h": h, "l": l, "c": c, "v": q["volume"][i] or 0})
        out[code] = {d: sorted(v, key=lambda x: x["t"]) for d, v in days.items() if len(v) >= MIN_BARS_DAY}
        time.sleep(0.2)
    return out


def fetch_index_5m():
    """加權指數 5 分K:{日期: [{"t","o","h","l","c"}]};抓不到回傳 {}(多空分數的大盤項目就給 0)。"""
    try:
        r = requests.get(bs.YAHOO_URL.format(sym="%5ETWII"), params={"interval": "5m", "range": "60d"},
                         headers=bs.HEADERS, timeout=20)
        res = r.json()["chart"]["result"][0]
    except Exception as e:  # noqa: BLE001
        print(f"[警告] 加權指數5分K抓不到: {e}")
        return {}
    q = res["indicators"]["quote"][0]
    days = {}
    for i, t in enumerate(res.get("timestamp") or []):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if o and h and l and c:
            dt_ = datetime.fromtimestamp(t, TAIPEI)
            days.setdefault(dt_.strftime("%Y-%m-%d"), []).append({"t": dt_.strftime("%H:%M"), "o": o, "h": h, "l": l, "c": c})
    return {d: sorted(v, key=lambda x: x["t"]) for d, v in days.items()}


def score_context(data, prev_closes, idx_days):
    """預先算好多空分數要用的跨股資料:各檔每天每根的累積量、漲跌幅,以及大盤每根的漲跌幅。"""
    cum, pct = {}, {}
    for code, days in data.items():
        pc = prev_closes.get(code) or {}
        for d, bars in days.items():
            v, cv = 0, {}
            for b in bars:
                v += b["v"]
                cv[b["t"]] = v
            cum.setdefault(code, {})[d] = cv
            prev_d = max((x for x in pc if x < d), default=None)
            if pc.get(prev_d):
                pct.setdefault(d, {}).setdefault(code, {b["t"]: (b["c"] / pc[prev_d] - 1) * 100 for b in bars})
    idx = {}
    ds = sorted(idx_days)
    for j, d in enumerate(ds):
        if j == 0:
            continue
        prev_c = idx_days[ds[j - 1]][-1]["c"]
        bars = idx_days[d]
        idx[d] = {"open": bars[0]["o"], "t": {b["t"]: (b["c"], (b["c"] / prev_c - 1) * 100) for b in bars}}
    return {"cum": cum, "pct": pct, "idx": idx}


def _pace(ctx, code, d, t, vol):
    hist = ctx["cum"].get(code) or {}
    prev = sorted(x for x in hist if x < d)[-PACE_DAYS:]
    base = sorted(hist[x][t] for x in prev if hist[x].get(t))
    if len(base) < PACE_MIN_DAYS or not vol:
        return None
    return vol / base[len(base) // 2]


def _grp_pct(ctx, code, d, t):
    g = GROUP_OF.get(code)
    xs = [p[t] for c, p in (ctx["pct"].get(d) or {}).items() if c != code and GROUP_OF.get(c) == g and t in p]
    return sum(xs) / len(xs) if g and xs else None


def score_series(code, d, bars, prev_close, ctx):
    """回傳 [(K棒index, 這根收盤時的特徵 dict)],只在 SCORE_FROM ~ SCORE_TO 之間。"""
    if not (ctx and prev_close):
        return []
    vw = _vwap(bars)
    orb = [b for b in bars if b["t"] < "09:15"]
    if len(orb) < 2:
        return []
    orb_hi, orb_lo = max(b["h"] for b in orb), min(b["l"] for b in orb)
    ix = ctx["idx"].get(d) or {}
    cv = (ctx["cum"].get(code) or {}).get(d) or {}
    out, hi, lo = [], None, None
    for i, b in enumerate(bars):
        hi = b["h"] if hi is None else max(hi, b["h"])
        lo = b["l"] if lo is None else min(lo, b["l"])
        if b["t"] < SCORE_FROM or b["t"] > SCORE_TO or i + 1 >= len(bars):
            continue
        m = (ix.get("t") or {}).get(b["t"])
        out.append((i, {
            "last": b["c"], "prev_close": prev_close, "vwap": vw[i], "vwap_prev": vw[i - 3] if i >= 3 else None,
            "orb_hi": orb_hi, "orb_lo": orb_lo, "pace": _pace(ctx, code, d, b["t"], cv.get(b["t"])),
            "day_hi": hi, "day_lo": lo, "mkt_pct": m[1] if m else None, "mkt_last": m[0] if m else None,
            "mkt_open": ix.get("open"), "grp_pct": _grp_pct(ctx, code, d, b["t"]),
            "up_lim": prev_close * 1.1, "lo_lim": prev_close * 0.9, "bar_hi": b["h"], "bar_lo": b["l"],
        }))
    return out


# ---------- 5 分K 永久存檔 ----------

def _round_bar(b):
    return [b["t"], round(b["o"], 4), round(b["h"], 4), round(b["l"], 4), round(b["c"], 4)] + ([int(b["v"] or 0)] if "v" in b else [])


def _unpack(rows, with_vol=True):
    keys = ("t", "o", "h", "l", "c", "v") if with_vol else ("t", "o", "h", "l", "c")
    return [dict(zip(keys, r)) for r in rows]


def load_bars_archive():
    """回傳 {日期: {"stocks": {code: {"pd","pc","b"}}, "idx": [...]}}。壞掉的檔案跳過。"""
    out = {}
    if not os.path.isdir(BARS_DIR):
        return out
    for fn in sorted(os.listdir(BARS_DIR)):
        if not fn.endswith(".json.gz"):
            continue
        try:
            with gzip.open(os.path.join(BARS_DIR, fn), "rt", encoding="utf-8") as f:
                day = json.load(f)
            out[day["date"]] = day
        except (OSError, ValueError, KeyError) as e:
            print(f"[警告] 5分K存檔 {fn} 讀不到: {e}")
    return out


def save_bars_day(d, data, closes, idx_days):
    """把某一天(已收盤、K 棒完整)的 5 分K 存檔。每檔一併記下前一交易日日期與收盤(pd/pc),
    之後就算 Yahoo 因為除權息往回調整價格,存檔的那天仍然自己一致。已存在就不覆蓋。"""
    path = os.path.join(BARS_DIR, f"{d}.json.gz")
    if os.path.exists(path):
        return False
    stocks = {}
    for code, days in data.items():
        bars = days.get(d)
        pc = closes.get(code) or {}
        pd = max((x for x in pc if x < d), default=None)
        if bars and pd and pc.get(pd) and bars[-1]["t"] >= LAST_BAR:
            stocks[code] = {"pd": pd, "pc": pc[pd], "b": [_round_bar(b) for b in bars]}
    if len(stocks) < bs.MIN_UNIVERSE:
        return False
    os.makedirs(BARS_DIR, exist_ok=True)
    body = {"date": d, "source": "Yahoo Finance 5m", "stocks": stocks,
            "idx": [_round_bar({k: v for k, v in b.items() if k != "v"}) for b in idx_days.get(d) or []]}
    tmp = path + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(body, f, separators=(",", ":"))
    os.replace(tmp, path)
    return True


def merge_archive(data, closes, idx_days, archive):
    """把存檔合併進 Yahoo 抓到的資料(原地修改)。同一天以存檔為準(存檔不會再變),
    前一天收盤也改用存檔記下的值。回傳合併後多出來的天數。"""
    added = 0
    for d, day in archive.items():
        for code, x in (day.get("stocks") or {}).items():
            if d not in data.setdefault(code, {}):
                added += 1
            data[code][d] = _unpack(x["b"])
            closes.setdefault(code, {})[x["pd"]] = x["pc"]
        if day.get("idx") and d not in idx_days:
            idx_days[d] = _unpack(day["idx"], with_vol=False)
    return added


# ---------- 盤中觸發的實際成績 ----------

def evaluate_live(log, data, rules_now):
    """bias_log.json 裡還沒算過的日子:每筆觸發用當天 5 分K 模擬,和回測同樣規則
    (觸發時間之後的下一根開盤進場、同根碰停損停利算停損、13:20 出場、扣成本)。
    停損停利用當天記下的參數,沒有就用現在的最佳參數。原地修改 log,回傳這次算了幾天。"""
    n = 0
    for d, day in (log.get("days") or {}).items():
        if day.get("evaluated"):
            continue
        if not any(d in (data.get(t["code"]) or {}) for t in day.get("trig") or []):
            continue
        for t in day.get("trig") or []:
            bars = (data.get(t["code"]) or {}).get(d)
            snap = (day.get("rules") or {}).get(t["rule"]) or {}
            now = ((rules_now.get(t["rule"]) or {}).get("best") or {})
            stop, target = snap.get("stop") or now.get("stop"), snap.get("target") if snap.get("stop") else now.get("target")
            t["holds"] = bool(snap.get("holds"))
            if not bars or not stop or not t.get("side") or not t.get("t"):
                t["result"] = None
                continue
            i = next((k for k, b in enumerate(bars) if b["t"] > t["t"]), None)
            if i is None or bars[i]["t"] > LAST_BAR:
                t["result"] = None
                continue
            r, why = simulate(bars, i, t["side"], stop, target)
            sg = 1 if t["side"] == "long" else -1
            t["result"] = {"entry_t": bars[i]["t"], "entry": round(bars[i]["o"], 4), "stop": stop, "target": target,
                           "exit": why, "gross": round(r, 3), "net": round(r - COST_PCT, 3),
                           "slip": round(sg * (t["price"] / bars[i]["o"] - 1) * 100, 3) if t.get("price") else None}
        day["evaluated"] = True
        n += 1
    return n


def live_stats(log, rules_now):
    """依規則彙總盤中觸發的實際成績,並列出回測驗證段的預期,方便對照。"""
    rows = [(d, t) for d, day in (log.get("days") or {}).items() if day.get("evaluated")
            for t in day.get("trig") or [] if t.get("result")]
    out = {"days": len({d for d, _ in rows}), "n": len(rows),
           "first": min((d for d, _ in rows), default=None), "last": max((d for d, _ in rows), default=None), "rules": {}}
    for rk in sorted({t["rule"] for _, t in rows}):
        xs = [(d, t) for d, t in rows if t["rule"] == rk]
        nets = [t["result"]["net"] for _, t in xs]
        tt, nd = bs.day_t([(d, t["result"]["net"]) for d, t in xs])
        slips = [t["result"]["slip"] for _, t in xs if t["result"].get("slip") is not None]
        best = (rules_now.get(rk) or {}).get("best") or {}
        out["rules"][rk] = {"name": (rules_now.get(rk) or {}).get("name") or rk, "n": len(xs), "days": nd,
                            "avg": round(stx.mean(nets), 3), "win": round(sum(1 for x in nets if x > 0) / len(nets) * 100, 1),
                            "t": round(tt, 2) if tt is not None else None,
                            "slip": round(stx.mean(slips), 3) if slips else None,
                            "n_holds": sum(1 for _, t in xs if t.get("holds")),
                            "backtest_test_avg": (best.get("test") or {}).get("avg")}
    return out


def _vwap(bars):
    """逐根累積均價線;第一根常常沒有量,沒有量的根用前一個值。"""
    out, pv, vv = [], 0.0, 0.0
    for b in bars:
        vv += b["v"]
        pv += (b["h"] + b["l"] + b["c"]) / 3 * b["v"]
        out.append(pv / vv if vv else (b["h"] + b["l"] + b["c"]) / 3)
    return out


def entry_index(rule, bars, prev_close, bear_prev, series=None):
    """回傳進場的 K 棒 index(用這根的開盤價成交),沒有觸發回傳 None。
    series 是 score_series() 的結果,只有 score_* 規則用得到。"""
    if not prev_close:
        return None
    if rule in bias.RULE_SIDE:
        armed = {}
        for i, f in series or ():
            hits, armed = bias.rule_hits(f, armed)
            if rule in hits:
                return i + 1
        return None
    ts = [b["t"] for b in bars]
    vw = _vwap(bars)
    pct = lambda i: (bars[i]["c"] / prev_close - 1) * 100
    if rule in ("orb_long", "orb_short"):
        orb = [b for b in bars if b["t"] < "09:15"]
        if len(orb) < 2:
            return None
        hi, lo = max(b["h"] for b in orb), min(b["l"] for b in orb)
        for i, b in enumerate(bars):
            if b["t"] < "09:15" or i + 1 >= len(bars) or ts[i + 1] > LAST_BAR:
                continue
            if rule == "orb_long" and b["c"] > hi:
                return i + 1
            if rule == "orb_short" and b["c"] < lo:
                return i + 1
        return None
    if rule in ("gap_fade_s", "gap_fade_l"):
        g = (bars[0]["o"] / prev_close - 1) * 100
        if (rule == "gap_fade_s" and g >= GAP_PCT) or (rule == "gap_fade_l" and g <= -GAP_PCT):
            return 1 if len(bars) > 1 else None
        return None
    if rule in ("vwap_long", "vwap_short"):
        for i in range(1, len(bars) - 1):
            if bars[i]["t"] < "09:30" or ts[i + 1] > LAST_BAR:
                continue
            up = bars[i - 1]["c"] <= vw[i - 1] and bars[i]["c"] > vw[i]
            dn = bars[i - 1]["c"] >= vw[i - 1] and bars[i]["c"] < vw[i]
            if rule == "vwap_long" and up and pct(i) > 0:
                return i + 1
            if rule == "vwap_short" and dn and pct(i) < 0:
                return i + 1
        return None
    if rule in ("mom_long", "mom_short"):
        i = next((k for k, t in enumerate(ts) if t == "10:00"), None)
        if i is None or i + 1 >= len(bars):
            return None
        if rule == "mom_long" and pct(i) >= MOM_PCT and bars[i]["c"] > vw[i]:
            return i + 1
        if rule == "mom_short" and pct(i) <= -MOM_PCT and bars[i]["c"] < vw[i]:
            return i + 1
        return None
    if rule == "bear_bounce_s":
        if not bear_prev:
            return None
        for i, b in enumerate(bars):
            if "09:15" <= b["t"] <= "10:30" and i + 1 < len(bars) and b["h"] >= prev_close * 0.995:
                return i + 1
        return None
    return None


def simulate(bars, i, side, stop, target):
    """從第 i 根開盤進場,回傳扣成本前的報酬 %(做空已轉成正=賺)與出場原因。"""
    e = bars[i]["o"]
    sgn = 1 if side == "long" else -1
    sp = e * (1 - sgn * stop / 100)
    tp = e * (1 + sgn * target / 100) if target else None
    for b in bars[i:]:
        if b["t"] > LAST_BAR:
            break
        if side == "long":
            if b["o"] <= sp and b is not bars[i]:
                return (b["o"] / e - 1) * 100, "stop"
            if b["l"] <= sp:
                return -stop, "stop"
            if tp and b["h"] >= tp:
                return target, "target"
        else:
            if b["o"] >= sp and b is not bars[i]:
                return (e / b["o"] - 1) * 100, "stop"
            if b["h"] >= sp:
                return -stop, "stop"
            if tp and b["l"] <= tp:
                return target, "target"
        last = b
    return sgn * (last["c"] / e - 1) * 100, "close"


def trades_for(data, prev_closes, bear_prev, ctx=None):
    """每條規則每個參數組合的交易清單:{(rule, stop, target): [(日期, 扣成本後報酬%, 出場原因, code)]}"""
    out = {}
    for code, days in data.items():
        pc = prev_closes.get(code) or {}
        ds = sorted(days)
        for j, d in enumerate(ds):
            prev_d = max((x for x in pc if x < d), default=None)
            prev_close = pc.get(prev_d)
            bars = days[d]
            series = score_series(code, d, bars, prev_close, ctx)
            for rule, (_, side, _) in RULES.items():
                i = entry_index(rule, bars, prev_close, prev_d in bear_prev.get(code, ()), series)
                if i is None:
                    continue
                for stop in STOPS:
                    for target in TARGETS:
                        r, why = simulate(bars, i, side, stop, target)
                        out.setdefault((rule, stop, target), []).append((d, r - COST_PCT, why, code))
    return out


def _stats(rows):
    if not rows:
        return {"n": 0}
    xs = [r for _, r, _, _ in rows]
    t, nd = bs.day_t([(d, r) for d, r, _, _ in rows])
    wins = [x for x in xs if x > 0]
    losses = [-x for x in xs if x <= 0]
    out = {"n": len(xs), "days": nd, "avg": round(stx.mean(xs), 3), "win": round(len(wins) / len(xs) * 100, 1),
           "t": round(t, 2) if t is not None else None,
           "avg_win": round(stx.mean(wins), 2) if wins else None, "avg_loss": round(stx.mean(losses), 2) if losses else None}
    if wins and losses:
        out["payoff"] = round(stx.mean(wins) / stx.mean(losses), 2)   # 平均賺 ÷ 平均賠(損益比)
    out["exits"] = {k: sum(1 for _, _, w, _ in rows if w == k) for k in ("stop", "target", "close")}
    return out


def evaluate(trades):
    all_days = sorted({d for rows in trades.values() for d, _, _, _ in rows})
    if len(all_days) < 10:
        return None, all_days, None
    split = all_days[int(len(all_days) * TRAIN_FRAC)]
    res = {}
    for rule, (name, side, desc) in RULES.items():
        combos = []
        for stop in STOPS:
            for target in TARGETS:
                rows = trades.get((rule, stop, target)) or []
                tr = _stats([r for r in rows if r[0] < split])
                te = _stats([r for r in rows if r[0] >= split])
                combos.append({"stop": stop, "target": target, "train": tr, "test": te, "all": _stats(rows)})
        ok = [c for c in combos if c["train"].get("n", 0) >= 30]
        best = max(ok, key=lambda c: c["train"]["avg"]) if ok else None
        holds = bool(best and best["train"]["avg"] > 0 and (best["train"]["t"] or 0) >= T_MIN
                     and best["test"].get("n", 0) >= MIN_TEST_N and best["test"]["avg"] > 0 and (best["test"]["t"] or 0) >= T_MIN)
        res[rule] = {"name": name, "side": side, "desc": desc, "best": best, "holds": holds,
                     "tested": len(combos), "n_positive_train": sum(1 for c in ok if c["train"]["avg"] > 0)}
    return res, all_days, split


def main():
    run_time = datetime.now(TAIPEI)
    codes = [c for c in bs.WATCHLIST_NAMES if not bs.is_etf(c)]
    ev, _, closes = bs.fetch_corp_actions(codes, run_time)
    data = fetch_5m(codes, ev)
    idx_days = fetch_index_5m()
    if len(data) < bs.MIN_UNIVERSE:
        raise RuntimeError(f"5分K只抓到 {len(data)} 檔,不更新")
    # 先把 Yahoo 這次抓到、還沒存過的完整交易日存檔(第一次執行會把約 60 天全部補存),再合併所有存檔
    today = run_time.strftime("%Y-%m-%d")
    after_close = run_time.hour * 60 + run_time.minute >= 13 * 60 + 35
    yahoo_days = sorted({d for days in data.values() for d in days if d < today or after_close})
    saved = sum(1 for d in yahoo_days if save_bars_day(d, data, closes, idx_days))
    archive = load_bars_archive()
    added = merge_archive(data, closes, idx_days, archive)
    # 前一天是否出現有效/觀察利空訊號(直接讀 bear_signals.py 剛寫好的成績單,只用來決定 bear_bounce_s 能不能進場)
    sc = load_json_safe(bs.OUT_FILE, {}) or {}
    strong = {k for k, s in (sc.get("signals") or {}).items() if s.get("status") in ("active", "watch")}
    bars, _, _ = bs.load_bars(run_time)
    for c, b in bars.items():
        bs.adjust_bars(b, ev.get(c))
    bear_prev = {}
    for code, b in bars.items():
        for i in range(len(b)):
            if strong & set(bs.signals_on(b, i)):
                bear_prev.setdefault(code, set()).add(b[i]["d"])

    ctx = score_context(data, closes, idx_days)
    trades = trades_for(data, closes, bear_prev, ctx)
    res, all_days, split = evaluate(trades)
    out = {
        "as_of": run_time.isoformat(),
        "stocks": len(data),
        "first": all_days[0] if all_days else None,
        "last": all_days[-1] if all_days else None,
        "days": len(all_days),
        "train_until": split,
        "cost_pct": COST_PCT,
        "rules": res,
        "method": "5分K,觸發後下一根開盤成交;同根同時碰停損停利算停損;13:20 那根收盤強制出場;前 2/3 交易日挑參數,後 1/3 驗證",
        "note": "歷史統計,不是預測,不構成買賣建議。當沖風險高,實際成交會有滑價。",
    }
    # 盤中觸發的實際成績:今天的觸發紀錄先存進 bias_log.json(盤中換日時也會補存),再把還沒算過的日子算掉
    rules_prev = (load_json_safe(OUT_FILE, {}) or {}).get("rules") or {}
    log_bias_day(load_json_safe(HISTORY_FILE, {}) or {})
    log = load_json_safe(BIAS_LOG_FILE, {}) or {}
    n_eval = evaluate_live(log, data, rules_prev or res or {})
    if n_eval:
        write_json_atomic(BIAS_LOG_FILE, log, separators=(",", ":"))
    out["live"] = live_stats(log, res or {})
    out["archive"] = {"days": len(archive), "first": min(archive, default=None), "last": max(archive, default=None),
                      "saved_now": saved, "added_beyond_yahoo": added}
    write_json_atomic(OUT_FILE, out, indent=1)
    print(f"[完成] 當沖回測 {len(data)} 檔、{len(all_days)} 天({out['first']}~{out['last']}),訓練到 {split}")
    print(f"  5分K存檔 {len(archive)} 天(這次新存 {saved} 天,比 Yahoo 多 {added} 檔日);盤中觸發成績 {out['live']['n']} 筆 / {out['live']['days']} 天(這次新算 {n_eval} 天)")
    for k, r in (res or {}).items():
        b = r["best"] or {}
        tr, te = b.get("train") or {}, b.get("test") or {}
        print(f"  {'站得住' if r['holds'] else '      '} {r['name']:<10} 停損{b.get('stop')} 停利{b.get('target')} "
              f"訓練 n={tr.get('n')} 平均{tr.get('avg')}% t={tr.get('t')} | 驗證 n={te.get('n')} 平均{te.get('avg')}% "
              f"t={te.get('t')} 勝率{te.get('win')}% 損益比{te.get('payoff')}")


if __name__ == "__main__":
    sys.exit(main())
