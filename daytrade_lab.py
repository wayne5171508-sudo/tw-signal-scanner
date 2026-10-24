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
  rsi_vol_now / rsi_vol_reclaim  RSI 超賣爆量(來自 Threads 串文):5 分K RSI(14) < 30 且量 >= 前 20 根平均 2 倍;
    now=下一根直接買,reclaim=之後 30 分鐘內收盤站上訊號 K 最高價才買、先跌破訊號 K 最低價就放棄
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
  「有優勢」= 訓練段和驗證段扣成本後平均都 > 0,而且以「天」為單位的 t 值都 >= 2,驗證段至少 30 筆;
  而且同一組參數拿到「成交值前 200 大裡不在自選股的股票」驗證段也要賺錢(t >= 1、至少 30 筆),避免只是剛好適合自選股。
  另外算最大回撤(每天資金平均分給當天訊號、依日期累加的資金曲線從高點回落多少 %)、最差一天、最長連虧天數。

產出:data/daytrade.json。這是歷史統計,不是預測,也不是買賣建議。
"""

import gzip
import json
import os
import re
import statistics as stx
import sys
import time
from datetime import datetime

import requests

import bear_signals as bs
import bias
import lab
from intraday import (BIAS_LOG_FILE, DATA_DIR, GROUP_OF, HISTORY_FILE, TAIPEI, TPEX_ALL_URL, TWSE_ALL_URL, UNIVERSE_HEADERS,
                      load_json_safe, log_bias_day, write_json_atomic)

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
RSI_N, RSI_LO = 14, 30          # 影片規則:5 分K RSI(14) 低於 30
VOL_AVG_N, VOL_MULT = 20, 2.0   # 這根量 >= 前 20 根平均的 2 倍
RECLAIM_BARS = 6                # 站回確認最多等幾根(30 分鐘)
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
    "rsi_vol_now": ("RSI超賣爆量立刻買", "long", f"09:15-12:30 第一次 5 分K RSI({RSI_N}) < {RSI_LO} 且量 >= 前 {VOL_AVG_N} 根平均 {VOL_MULT:g} 倍,下一根買"),
    "rsi_vol_reclaim": ("RSI超賣爆量站回才買", "long", f"同上的訊號 K 之後 {RECLAIM_BARS} 根內收盤站上訊號 K 最高價才買;先跌破訊號 K 最低價就放棄"),
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


def load_bars_archive(bars_dir=None):
    """回傳 {日期: {"stocks": {code: {"pd","pc","b"}}, "idx": [...]}}。壞掉的檔案跳過。"""
    bars_dir = bars_dir or BARS_DIR
    out = {}
    if not os.path.isdir(bars_dir):
        return out
    for fn in sorted(os.listdir(bars_dir)):
        if not fn.endswith(".json.gz"):
            continue
        try:
            with gzip.open(os.path.join(bars_dir, fn), "rt", encoding="utf-8") as f:
                day = json.load(f)
            out[day["date"]] = day
        except (OSError, ValueError, KeyError) as e:
            print(f"[警告] 5分K存檔 {fn} 讀不到: {e}")
    return out


def save_bars_day(d, data, closes, idx_days, bars_dir=None):
    """把某一天(已收盤、K 棒完整)的 5 分K 存檔。每檔一併記下前一交易日日期與收盤(pd/pc),
    之後就算 Yahoo 因為除權息往回調整價格,存檔的那天仍然自己一致。已存在就不覆蓋。"""
    bars_dir = bars_dir or BARS_DIR
    path = os.path.join(bars_dir, f"{d}.json.gz")
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
    os.makedirs(bars_dir, exist_ok=True)
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


# ---------- 成交值前 200 大:交叉驗證用的第二組股票 ----------
TOP_N = 200
TOP_FILE = os.path.join(DATA_DIR, "top_universe.json")
BARS_DIR_TOP = os.path.join(DATA_DIR, "bars5m_top")   # 前 200 大(不含自選股)的 5 分K,每天一檔,永久保存
BARS1M_DIR = os.path.join(DATA_DIR, "bars1m")         # 1 分K:只在排程機器上產生,由 bear.yml 推到 data-archive 分支(不進網站)
TOP_MIN_TEST_N = 30
TOP_T_MIN = 1.0       # 換一批股票只要求方向一致且不是雜訊(t >= 1),主要門檻已經在自選股驗證過


def rank_top(twse_rows, tpex_rows, n=TOP_N):
    """依前一交易日成交值排出前 n 名普通股(四碼、不是 0 開頭,也就是不含 ETF、權證)。回傳 [(code, name, 成交值)]。"""
    rows = []
    for data, ck, nk, vk in ((twse_rows, "Code", "Name", "TradeValue"), (tpex_rows, "SecuritiesCompanyCode", "CompanyName", "TransactionAmount")):
        for r in data or []:
            c = str(r.get(ck) or "").strip()
            try:
                v = float(str(r.get(vk) or "0").replace(",", ""))
            except ValueError:
                continue
            if re.fullmatch(r"[1-9]\d{3}", c) and v > 0:
                rows.append((c, str(r.get(nk) or "").strip(), v))
    rows.sort(key=lambda x: -x[2])
    return rows[:n]


def fetch_top_universe(today):
    """每天更新一次 data/top_universe.json;官方來源抓不到就沿用舊名單。"""
    cur = load_json_safe(TOP_FILE, {}) or {}
    if cur.get("date") == today and len(cur.get("codes") or []) >= TOP_N * 0.75:
        return cur
    got = {}
    for key, url in (("twse", TWSE_ALL_URL), ("tpex", TPEX_ALL_URL)):
        try:
            r = requests.get(url, headers=UNIVERSE_HEADERS, timeout=20)
            got[key] = r.json()
        except Exception:  # noqa: BLE001
            got[key] = None
    if got.get("twse") is None or got.get("tpex") is None:
        return cur
    top = rank_top(got["twse"], got["tpex"])
    if len(top) < TOP_N * 0.75:
        return cur
    out = {"date": today, "basis": "前一交易日成交值", "codes": [c for c, _, _ in top], "names": {c: nm for c, nm, _ in top}}
    write_json_atomic(TOP_FILE, out, indent=1)
    return out


def fetch_1m(codes, ev):
    """Yahoo 1 分K(只給最近約 7 天)。回傳 {日期: {code: [[t,o,h,l,c,v], ...]}},只留 K 棒數夠的日子(>= 240 根)。"""
    out, started = {}, time.time()
    for code in codes:
        if time.time() - started > bs.YAHOO_BUDGET_SECONDS:
            break
        _, res = bs._yahoo(code, {"interval": "1m", "range": "7d"}, (ev.get(code) or {}).get("suf"))
        if not res:
            continue
        q = res["indicators"]["quote"][0]
        days = {}
        for i, t in enumerate(res["timestamp"]):
            o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
            if o and h and l and c:
                dt_ = datetime.fromtimestamp(t, TAIPEI)
                days.setdefault(dt_.strftime("%Y-%m-%d"), []).append([dt_.strftime("%H:%M"), round(o, 4), round(h, 4), round(l, 4), round(c, 4), int(q["volume"][i] or 0)])
        for d, rows in days.items():
            if len(rows) >= 240:
                out.setdefault(d, {})[code] = rows
        time.sleep(0.15)
    return out


def save_1m(days, today, after_close):
    """每天一個 data/bars1m/日期.json.gz(排程機器上),已收盤的完整交易日才存。回傳存了哪些日期。"""
    os.makedirs(BARS1M_DIR, exist_ok=True)
    saved = []
    for d, stocks in sorted(days.items()):
        if (d < today or after_close) and len(stocks) >= bs.MIN_UNIVERSE:
            with gzip.open(os.path.join(BARS1M_DIR, f"{d}.json.gz"), "wt", encoding="utf-8") as f:
                json.dump({"date": d, "source": "Yahoo Finance 1m", "stocks": stocks}, f, separators=(",", ":"))
            saved.append(d)
    return saved


def cross_check(res, trades_top, split):
    """自選股選出的最佳參數,原封不動拿到「前 200 大裡不在自選股的股票」再測一次。
    寫進 res[rule]["top"],並把 holds 改成「自選股站得住 且 前 200 大驗證段也賺錢(t >= 1)」。"""
    for rule, r in (res or {}).items():
        r["holds_watch"] = r["holds"]
        b = r.get("best")
        if not b or trades_top is None:
            r["top"] = None
            continue
        rows = trades_top.get((rule, b["stop"], b["target"])) or []
        te = _stats([x for x in rows if split and x[0] >= split])
        r["top"] = {"all": _stats(rows), "test": te}
        ok = te.get("n", 0) >= TOP_MIN_TEST_N and te.get("avg", 0) > 0 and (te.get("t") or 0) >= TOP_T_MIN
        r["holds"] = bool(r["holds_watch"] and ok)
    return res


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
                            **drawdown([(d, t["result"]["net"]) for d, t in xs]),
                            "backtest_test_avg": (best.get("test") or {}).get("avg")}
    return out


# ---------- 盤後回放用的每日觸發與出場紀錄 ----------
REPLAY_DIR = os.path.join(DATA_DIR, "replay")


def replay_day(d, data, closes, bear_prev, ctx, params):
    """某一天每檔、每條規則的進出場(和回測同一套 entry_index / simulate)。
    params={rule: (stop, target)},用產生當下的最佳參數。回傳 {code: [[rule, 進場時間, 進場價, 出場時間, 出場價, 原因, 扣成本後%]]}。"""
    out = {}
    for code, days in data.items():
        bars = days.get(d)
        if not bars:
            continue
        pc = closes.get(code) or {}
        prev_d = max((x for x in pc if x < d), default=None)
        prev_close = pc.get(prev_d)
        series = score_series(code, d, bars, prev_close, ctx)
        for rule, (_, side, _) in RULES.items():
            i = entry_index(rule, bars, prev_close, prev_d in bear_prev.get(code, ()), series)
            if i is None or rule not in params:
                continue
            stop, target = params[rule]
            r, why, k, px = simulate_detail(bars, i, side, stop, target)
            out.setdefault(code, []).append([rule, bars[i]["t"], round(bars[i]["o"], 4), bars[k]["t"], round(px, 4), why,
                                             round(r - COST_PCT, 3)])
    return out


def write_replay(data, closes, bear_prev, ctx, res, days):
    """每個交易日存一份 data/replay/日期.json.gz(存好就不改,參數記在檔案裡),並更新 data/replay/index.json
    (可回放的日期與規則清單)。網頁「回放」分頁讀這兩個檔和 data/bars5m/。回傳這次新寫了幾天。"""
    params = {k: ((r.get("best") or {}).get("stop") or 1.0, (r.get("best") or {}).get("target")) for k, r in (res or {}).items()}
    os.makedirs(REPLAY_DIR, exist_ok=True)
    n = 0
    for d in days:
        path = os.path.join(REPLAY_DIR, f"{d}.json.gz")
        if os.path.exists(path) or not os.path.exists(os.path.join(BARS_DIR, f"{d}.json.gz")):
            continue
        body = {"date": d, "cost_pct": COST_PCT, "params": params, "trades": replay_day(d, data, closes, bear_prev, ctx, params)}
        with gzip.open(path + ".tmp", "wt", encoding="utf-8") as f:
            json.dump(body, f, separators=(",", ":"))
        os.replace(path + ".tmp", path)
        n += 1
    avail = sorted(fn[:10] for fn in os.listdir(REPLAY_DIR) if fn.endswith(".json.gz"))
    write_json_atomic(os.path.join(REPLAY_DIR, "index.json"), {
        "dates": avail,
        "rules": {k: {"name": v[0], "side": v[1], "desc": v[2], "holds": bool((res or {}).get(k, {}).get("holds"))} for k, v in RULES.items()},
    }, indent=1)
    return n


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
    if rule in ("rsi_vol_now", "rsi_vol_reclaim"):
        # Threads 串文(Claude + TradingView 台股短線)的規則:RSI 超賣 + 爆量只是候選,站回關鍵位置才進場
        rs = lab.rsi([b["c"] for b in bars], RSI_N)
        sig = None
        for i in range(VOL_AVG_N, len(bars) - 1):
            if not (SCORE_FROM <= bars[i]["t"] <= SCORE_TO) or rs[i] is None:
                continue
            avg = sum(b["v"] for b in bars[i - VOL_AVG_N:i]) / VOL_AVG_N
            if rs[i] < RSI_LO and avg > 0 and bars[i]["v"] >= VOL_MULT * avg:
                sig = i
                break
        if sig is None:
            return None
        if rule == "rsi_vol_now":
            return sig + 1
        for k in range(sig + 1, min(sig + 1 + RECLAIM_BARS, len(bars) - 1)):
            if bars[k]["l"] < bars[sig]["l"]:
                return None
            if bars[k]["c"] > bars[sig]["h"] and bars[k + 1]["t"] <= LAST_BAR:
                return k + 1
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


def simulate_detail(bars, i, side, stop, target):
    """從第 i 根開盤進場,回傳 (扣成本前報酬 %(做空已轉成正=賺), 出場原因, 出場那根 index, 出場價)。"""
    e = bars[i]["o"]
    sgn = 1 if side == "long" else -1
    sp = e * (1 - sgn * stop / 100)
    tp = e * (1 + sgn * target / 100) if target else None
    k = i
    for k in range(i, len(bars)):
        b = bars[k]
        if b["t"] > LAST_BAR:
            k -= 1
            break
        if side == "long":
            if b["o"] <= sp and k != i:
                return (b["o"] / e - 1) * 100, "stop", k, b["o"]
            if b["l"] <= sp:
                return -stop, "stop", k, sp
            if tp and b["h"] >= tp:
                return target, "target", k, tp
        else:
            if b["o"] >= sp and k != i:
                return (e / b["o"] - 1) * 100, "stop", k, b["o"]
            if b["h"] >= sp:
                return -stop, "stop", k, sp
            if tp and b["l"] <= tp:
                return target, "target", k, tp
    last = bars[k]
    return sgn * (last["c"] / e - 1) * 100, "close", k, last["c"]


def simulate(bars, i, side, stop, target):
    """從第 i 根開盤進場,回傳扣成本前的報酬 %(做空已轉成正=賺)與出場原因。"""
    r, why, _, _ = simulate_detail(bars, i, side, stop, target)
    return r, why


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
    out.update(drawdown([(d, r) for d, r, _, _ in rows]))
    return out


def drawdown(rows):
    """最大回撤:假設每天把資金平均分給當天所有訊號,每天損益 = 當天各筆扣成本後報酬的平均(%),
    依日期累加成資金曲線,從最高點往下掉最多多少 %(單利累加)。也回傳最差的一天與最長連續虧損天數。"""
    byd = {}
    for d, r in rows:
        byd.setdefault(d, []).append(r)
    daily = [sum(v) / len(v) for _, v in sorted(byd.items())]
    if not daily:
        return {}
    eq = peak = mdd = 0.0
    streak = worst_streak = 0
    for x in daily:
        eq += x
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)
        streak = streak + 1 if x < 0 else 0
        worst_streak = max(worst_streak, streak)
    return {"mdd": round(mdd, 2), "worst_day": round(min(daily), 2), "lose_streak": worst_streak, "cum": round(eq, 2)}


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

    # 交叉驗證:成交值前 200 大裡不在自選股的股票,用同一組參數再測一次(5 分K 也每天存檔,樣本會累積)
    top_info, trades_top, top_n, top_days, ev_top = fetch_top_universe(today), None, 0, 0, {}
    top_codes = [c for c in (top_info.get("codes") or []) if c not in bs.WATCHLIST_NAMES]
    try:
        if top_codes and res:
            ev_top, _, closes_top = bs.fetch_corp_actions(top_codes, run_time)
            data_top = fetch_5m(top_codes, ev_top)
            for d in sorted({d for days in data_top.values() for d in days if d < today or after_close}):
                save_bars_day(d, data_top, closes_top, {}, BARS_DIR_TOP)
            merge_archive(data_top, closes_top, {}, load_bars_archive(BARS_DIR_TOP))
            trades_top = trades_for(data_top, closes_top, {}, score_context(data_top, closes_top, idx_days))
            top_n, top_days = len(data_top), len({d for days in data_top.values() for d in days})
    except Exception as e:  # noqa: BLE001  交叉驗證失敗時只用自選股的結果,並在輸出標示
        print(f"[警告] 前 200 大交叉驗證失敗: {e}")
        trades_top = None
    cross_check(res, trades_top, split)

    # 1 分K:自選股 + 前 200 大,存在排程機器上,由 bear.yml 推到 data-archive 分支(Yahoo 只給 7 天,每天存才會累積)
    try:
        saved_1m = save_1m(fetch_1m(codes + top_codes, {**ev, **ev_top}), today, after_close)
    except Exception as e:  # noqa: BLE001
        print(f"[警告] 1 分K 存檔失敗: {e}")
        saved_1m = []
    out = {
        "as_of": run_time.isoformat(),
        "stocks": len(data),
        "first": all_days[0] if all_days else None,
        "last": all_days[-1] if all_days else None,
        "days": len(all_days),
        "train_until": split,
        "cost_pct": COST_PCT,
        "rules": res,
        "top_universe": {"date": top_info.get("date"), "basis": top_info.get("basis"), "stocks": top_n, "days": top_days,
                         "checked": trades_top is not None, "min_test_n": TOP_MIN_TEST_N, "t_min": TOP_T_MIN},
        "bars1m_saved": saved_1m,
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
    out["replay_new_days"] = write_replay(data, closes, bear_prev, ctx, res, sorted(archive))
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
