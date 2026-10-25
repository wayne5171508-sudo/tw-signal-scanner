#!/usr/bin/env python3
"""
爆量雷達 - 利空訊號回測、損益比與進場時段(GitHub Actions 每個交易日收盤後跑一次)

做的事:
1) 抓自選股近 260 根正式日K(沿用 intraday.py 的抓法與自選股清單,清單只要維護一份)。
   ETF 不納入(分割、槓桿、追蹤指數,和個股的量價行為不同)。
2) 還原權值:cnyes 日K沒有還原除權息,除息、配股、減資那天的跳空會被誤判成利空,
   往後算報酬也會失真。用 Yahoo 的除權息/分割事件把之前的價格往回調整(配股/減資連成交量一起調),
   Yahoo 抓不到時,單日漲跌超過 ±50% 的也自動當成分割處理。有事件的那一天不判斷訊號。
3) 逐日只用「當天收盤前看得到的資料」判斷每一種利空訊號有沒有成立,
   看訊號後 T+1/T+3/T+5 收盤的「超額報酬」= 該股報酬 - 自選股當天平均報酬(扣掉大盤漲跌)。
4) 損益比:訊號後 5 天內「最多反彈幾%」÷「最多再跌幾%」(以抱著股票的人來看),和一般日子比較。
   今天出現訊號的個股另外用 20 日高低點算「現在買進」的損益比。
5) 進場時段:用 Yahoo 約兩年的 60 分K,看 09/10/11/12/13 點各時段買進,平均比當天收盤便宜還是貴,
   以及到隔天收盤的超額報酬;一般日子和「利空訊號隔天」分開算。
6) 每個訊號依實測結果自動評級(有效/觀察/無效),全部寫進 data/bear_scorecard.json。

評級規則(全部都要用數字過關,不是手動決定):
- 有效 active:全期間 T+5 超額 < 0、跑輸機率比基準高 3 個百分點以上、
  以「天」為單位算的 t 值 <= -2,而且後 1/3 期間(驗證段)超額也 < 0。
- 觀察 watch:全期間 T+5 超額 < 0 且 t 值 <= -1.5,但沒有全部過關。
- 無效 inactive:其餘。訊號照算,但網頁不該把它標成利空。
t 值的算法:同一天多檔同時出現的訊號先平均成一筆,避免同一天的大盤因素被重複計算。

產出:data/bear_scorecard.json、data/corp_actions.json(除權息事件快取)
用法:python bear_signals.py(加 --force 在非交易日也重算,手動測試用)
"""

import json
import os
import statistics as stx
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

from corp import adjust_bars  # noqa: F401  還原權值共用(盤中 intraday.py 也用這份)
from intraday import (DATA_DIR, DAILY_OFFICIAL_FILE, HEADERS, TAIPEI, WATCHLIST_NAMES, _EmptyHistory,
                      fetch_history_one, load_json_safe, write_json_atomic)

OUT_FILE = os.path.join(DATA_DIR, "bear_scorecard.json")
CORP_FILE = os.path.join(DATA_DIR, "corp_actions.json")
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
HORIZONS = (1, 3, 5)
MAIN_H = 5                 # 評級看第幾天
WARMUP = 61                # 前 61 根只拿來算均線/季線,不判斷訊號
MIN_BARS = 70              # 日K少於這個數的股票不納入
MIN_UNIVERSE = 20          # 某天至少要有幾檔股票才算得出「大盤平均」
RECENT_DAYS = 120          # 近期表現看最近幾個交易日
HOLDOUT_FRAC = 1 / 3       # 最後這段期間當驗證段
ACTIVE_T = -2.0
WATCH_T = -1.5
ACTIVE_LOSE_GAP = 3.0      # 跑輸機率至少比基準高幾個百分點
FETCH_BUDGET_SECONDS = 240
YAHOO_BUDGET_SECONDS = 300
WINDOWS = ("09", "10", "11", "12", "13")  # 60分K的開始時間;13 點那根只到 13:30
WINDOW_LABEL = {"09": "09:00-10:00", "10": "10:00-11:00", "11": "11:00-12:00", "12": "12:00-13:00", "13": "13:00-13:30"}
WINDOW_MIN_DAYS = 60       # 進場時段至少要有幾個交易日才下結論
LEVEL_DAYS = 20            # 今天個股損益比:停損看近 20 日最低、目標看近 20 日最高

# 訊號定義:名稱、說明。判斷式在 signals_on() 裡。
SIGNALS = {
    "big_black": ("爆量長黑", "量 >= 20日中位數 2 倍、跌 >= 3%、黑K實體 >= 振幅 60%"),
    "vol_down": ("量大下跌(現行出場)", "量 >= 2 倍且跌 >= 0.5%,也就是網頁現在的「出場」標籤"),
    "break_ma60": ("放量跌破季線", "收盤由季線上方跌到下方,量 >= 1.5 倍"),
    "close_20low": ("收盤跌破20日最低收", "收盤低於前20日最低收盤價"),
    "gap_down": ("跳空缺口未補", "開盤低於昨日最低,收盤仍低於昨日最低"),
    "ma_bear": ("均線空頭排列首日", "5日 < 10日 < 20日均線,且昨天還不是"),
    "break_20low_vol": ("放量跌破20日低", "收盤低於前20日最低價,量 >= 1.5 倍"),
    "top_no_rise": ("高檔爆量不漲", "量 >= 2.5 倍、漲跌 < 1%、最高價在60日高點 3% 內"),
    "top_upper_shadow": ("高檔爆量長上影", "量 >= 2 倍、最高價在60日高點 3% 內、上影線 >= 振幅 50%"),
    "vol_up_price_down3": ("連3日量增價跌", "連續3天成交量增加、收盤下跌"),
}


def is_etf(code):
    return str(code).startswith("00")


def _ma(b, n, k):
    return sum(x["c"] for x in b[k - n + 1:k + 1]) / n


def signals_on(b, i):
    """只用 b[:i+1](當天收盤為止)的資料,回傳當天成立的訊號 key。"""
    if i < WARMUP or b[i].get("event"):
        return []
    x, p = b[i], b[i - 1]
    vols = [y["v"] for y in b[i - 20:i] if y.get("v")]
    if len(vols) < 10 or not x.get("v") or not p["c"]:
        return []
    vm = x["v"] / stx.median(vols)
    pct = x["c"] / p["c"] - 1
    o = x.get("o") or x["c"]
    rng = x["h"] - x["l"]
    h60 = max(y["h"] for y in b[i - 60:i])
    l20 = min(y["l"] for y in b[i - 20:i])
    c20 = min(y["c"] for y in b[i - 20:i])
    out = []
    if vm >= 2 and pct <= -0.03 and rng > 0 and (o - x["c"]) >= 0.6 * rng:
        out.append("big_black")
    if vm >= 2 and pct <= -0.005:
        out.append("vol_down")
    if x["c"] < _ma(b, 60, i) and p["c"] >= _ma(b, 60, i - 1) and vm >= 1.5:
        out.append("break_ma60")
    if x["c"] < c20:
        out.append("close_20low")
    if x.get("o") and x["o"] < p["l"] and x["c"] < p["l"]:
        out.append("gap_down")
    m5, m10, m20 = _ma(b, 5, i), _ma(b, 10, i), _ma(b, 20, i)
    pm5, pm10, pm20 = _ma(b, 5, i - 1), _ma(b, 10, i - 1), _ma(b, 20, i - 1)
    if m5 < m10 < m20 and not (pm5 < pm10 < pm20):
        out.append("ma_bear")
    if x["c"] < l20 and vm >= 1.5:
        out.append("break_20low_vol")
    if vm >= 2.5 and abs(pct) < 0.01 and x["h"] >= 0.97 * h60:
        out.append("top_no_rise")
    if vm >= 2 and x["h"] >= 0.97 * h60 and rng > 0 and (x["h"] - max(o, x["c"])) >= 0.5 * rng:
        out.append("top_upper_shadow")
    if all(b[i - k].get("v") and b[i - k - 1].get("v") and b[i - k]["v"] > b[i - k - 1]["v"]
           and b[i - k]["c"] < b[i - k - 1]["c"] for k in range(3)):
        out.append("vol_up_price_down3")
    return out


# ---------- 資料:日K、除權息、60分K ----------

def load_bars(run_time):
    """每檔重抓一次日K(收盤後抓才是正式的今天那根)。抓不到的退回 daily_official.json 的舊記錄。"""
    fallback = (load_json_safe(DAILY_OFFICIAL_FILE, {}) or {}).get("bars") or {}
    bars, fresh, errors = {}, 0, []
    started = time.time()
    for code in WATCHLIST_NAMES:
        if is_etf(code):
            continue
        got = None
        if time.time() - started < FETCH_BUDGET_SECONDS:
            try:
                got = fetch_history_one(code, run_time)
                fresh += 1
            except _EmptyHistory:
                got = None
            except Exception as e:  # noqa: BLE001  單檔失敗改用舊記錄,不中斷
                errors.append(f"{code}: {type(e).__name__}")
            time.sleep(0.15)
        got = got or fallback.get(code)
        if got:
            clean = [dict(x) for x in got if x.get("d") and x.get("c") and x.get("h") and x.get("l") and x["h"] >= x["l"]]
            clean.sort(key=lambda x: x["d"])
            if len(clean) >= MIN_BARS:
                bars[code] = clean
    return bars, fresh, errors


def _yahoo(code, params, suf=None):
    """回傳 (suffix, result)。上市 .TW、上櫃 .TWO,不知道就兩個都試。"""
    for s in ([suf] if suf else [".TW", ".TWO"]):
        try:
            r = requests.get(YAHOO_URL.format(sym=code + s), params=params, headers=HEADERS, timeout=20)
            res = r.json()["chart"]["result"][0]
            if res.get("timestamp"):
                return s, res
        except Exception:  # noqa: BLE001  抓不到就試下一個後綴
            continue
    return None, None


def _tpe_date(ts):
    return datetime.fromtimestamp(int(ts), TAIPEI).strftime("%Y-%m-%d")


def fetch_corp_actions(codes, run_time):
    """除權息/分割事件,一天抓一次存進 data/corp_actions.json;抓不到的沿用舊快取。
    同一個請求順便拿 Yahoo 的正式日收盤(進場時段要比「當天收盤」用),只放記憶體不存檔。"""
    today = run_time.strftime("%Y-%m-%d")
    cache = load_json_safe(CORP_FILE, {}) or {}
    ev = cache.get("events") or {}
    closes = {}
    ok, started = 0, time.time()
    for code in codes:
        if time.time() - started > YAHOO_BUDGET_SECONDS:
            break
        suf, res = _yahoo(code, {"interval": "1d", "range": "2y", "events": "div,split"}, (ev.get(code) or {}).get("suf"))
        if not res:
            continue
        e = res.get("events") or {}
        q = res["indicators"]["quote"][0]
        closes[code] = {_tpe_date(t): c for t, c in zip(res["timestamp"], q["close"]) if c}
        ev[code] = {
            "suf": suf,
            "div": {_tpe_date(k): v["amount"] for k, v in (e.get("dividends") or {}).items() if v.get("amount")},
            "split": {_tpe_date(k): v["numerator"] / v["denominator"] for k, v in (e.get("splits") or {}).items()
                      if v.get("numerator") and v.get("denominator")},
        }
        ok += 1
        time.sleep(0.2)
    try:
        write_json_atomic(CORP_FILE, {"updated": today, "source": "Yahoo Finance chart events", "events": ev}, indent=1)
    except (OSError, ValueError):
        pass
    return ev, f"yahoo {ok}/{len(codes)}", closes




def fetch_hourly(codes, ev, closes):
    """Yahoo 約兩年的 60 分K,整理成 {code: {日期: {"09": 成交價, ..., "close": 當天正式收盤}}}。
    成交價用該小時 (開+收)/2,代表在這個時段內隨意買進的平均價。
    收盤優先用 Yahoo 日K收盤(含 13:30 收盤集合競價);沒有才用 13 點那根的收盤。"""
    out, started = {}, time.time()
    for code in codes:
        if time.time() - started > YAHOO_BUDGET_SECONDS:
            break
        _, res = _yahoo(code, {"interval": "60m", "range": "730d"}, (ev.get(code) or {}).get("suf"))
        if not res:
            continue
        q = res["indicators"]["quote"][0]
        days = {}
        for t, o, c in zip(res["timestamp"], q["open"], q["close"]):
            if not (o and c):
                continue
            dt_ = datetime.fromtimestamp(t, TAIPEI)
            hh = dt_.strftime("%H")
            if hh in WINDOWS:
                day = days.setdefault(dt_.strftime("%Y-%m-%d"), {})
                day[hh] = (o + c) / 2
                if hh == "13":
                    day["close"] = c
        dc = closes.get(code) or {}
        for d, day in days.items():
            if d in dc:
                day["close"] = dc[d]
        out[code] = {d: v for d, v in days.items() if all(w in v for w in WINDOWS) and v.get("close")}
        time.sleep(0.2)
    return out


# ---------- 統計 ----------

def day_t(rows):
    """rows=[(日期, 數值)]。同一天先平均成一筆再算 t 值,回傳 (t, 天數)。"""
    byd = {}
    for d, x in rows:
        byd.setdefault(d, []).append(x)
    m = [sum(v) / len(v) for v in byd.values()]
    if len(m) < 5:
        return None, len(m)
    sd = stx.stdev(m)
    return (stx.mean(m) / (sd / len(m) ** 0.5) if sd else None), len(m)


def summarize(rows, base_lose):
    if not rows:
        return {"n": 0}
    xs = [x for _, x in rows]
    t, nd = day_t(rows)
    lose = sum(1 for x in xs if x < 0) / len(xs) * 100
    return {"n": len(xs), "days": nd, "ex": round(stx.mean(xs) * 100, 2), "lose": round(lose, 1),
            "lose_gap": round(lose - base_lose, 1), "t": round(t, 2) if t is not None else None}


def rr_stats(ups, dns):
    """抱著股票的人來看:5 天內平均最多漲幾%、最多跌幾%,損益比 = 漲 ÷ 跌。"""
    if len(ups) < 10:
        return None
    up, dn = stx.mean(ups) * 100, -stx.mean(dns) * 100
    return {"up": round(up, 2), "dn": round(dn, 2), "rr": round(up / dn, 2) if dn > 0 else None}


def grade(full, test):
    if full.get("n", 0) < 30 or full.get("t") is None:
        return "inactive", "樣本太少"
    neg = full["ex"] < 0
    if neg and full["t"] <= ACTIVE_T and full["lose_gap"] >= ACTIVE_LOSE_GAP and test.get("n", 0) >= 10 and test["ex"] < 0:
        return "active", "全期間與驗證段都明顯跑輸大盤"
    if neg and full["t"] <= WATCH_T:
        return "watch", "有跑輸傾向,但還沒全部過關"
    return "inactive", "和一般日子差不多,不能當利空"


def backtest(bars):
    # 每天的大盤基準:自選股當天的 T+h 平均報酬
    fwd, by_day = {}, {}
    for code, b in bars.items():
        for i in range(len(b)):
            for h in HORIZONS:
                if i + h < len(b):
                    r = b[i + h]["c"] / b[i]["c"] - 1
                    fwd[(code, i, h)] = r
                    by_day.setdefault((b[i]["d"], h), []).append(r)
    mkt = {k: sum(v) / len(v) for k, v in by_day.items() if len(v) >= MIN_UNIVERSE}

    all_dates = sorted({d for d, h in mkt if h == MAIN_H})
    split = all_dates[int(len(all_dates) * (1 - HOLDOUT_FRAC))] if all_dates else None
    recent_from = all_dates[-RECENT_DAYS] if len(all_dates) >= RECENT_DAYS else (all_dates[0] if all_dates else None)

    base = {h: [] for h in HORIZONS}
    base_up, base_dn = [], []
    hits = {k: {h: [] for h in HORIZONS} for k in SIGNALS}
    exc = {k: ([], []) for k in SIGNALS}
    today_hits, flagged = {}, {}
    for code, b in bars.items():
        for i in range(WARMUP, len(b)):
            d = b[i]["d"]
            for h in HORIZONS:
                if (code, i, h) in fwd and (d, h) in mkt:
                    base[h].append(fwd[(code, i, h)] - mkt[(d, h)])
            ex5 = None
            if i + MAIN_H < len(b):
                seg = b[i + 1:i + 1 + MAIN_H]
                ex5 = (max(x["h"] for x in seg) / b[i]["c"] - 1, min(x["l"] for x in seg) / b[i]["c"] - 1)
                base_up.append(ex5[0])
                base_dn.append(ex5[1])
            sig = signals_on(b, i)
            if sig:
                flagged.setdefault(d, {})[code] = sig
            if i == len(b) - 1 and sig:
                today_hits[code] = {"d": d, "signals": sig, "i": i}
            for k in sig:
                for h in HORIZONS:
                    if (code, i, h) in fwd and (d, h) in mkt:
                        hits[k][h].append((d, fwd[(code, i, h)] - mkt[(d, h)]))
                if ex5:
                    exc[k][0].append(ex5[0])
                    exc[k][1].append(ex5[1])

    base_out = {}
    for h in HORIZONS:
        xs = base[h]
        base_out[f"t{h}"] = {"n": len(xs), "lose": round(sum(1 for x in xs if x < 0) / len(xs) * 100, 1) if xs else None}
    base_out["rr5"] = rr_stats(base_up, base_dn)

    sig_out = {}
    for k, (name, desc) in SIGNALS.items():
        rec = {"name": name, "desc": desc}
        for h in HORIZONS:
            rec[f"t{h}"] = summarize(hits[k][h], base_out[f"t{h}"]["lose"] or 50)
        rows = hits[k][MAIN_H]
        bl = base_out[f"t{MAIN_H}"]["lose"] or 50
        rec["train"] = summarize([r for r in rows if split and r[0] < split], bl)
        rec["test"] = summarize([r for r in rows if split and r[0] >= split], bl)
        rec["recent"] = summarize([r for r in rows if recent_from and r[0] >= recent_from], bl)
        rec["rr5"] = rr_stats(*exc[k])
        rec["status"], rec["status_reason"] = grade(rec[f"t{MAIN_H}"], rec["test"])
        sig_out[k] = rec

    period = (all_dates[0], all_dates[-1]) if all_dates else (None, None)
    return {"period": period, "holdout_from": split, "recent_from": recent_from,
            "baseline": base_out, "signals": sig_out}, today_hits, flagged


def entry_windows(hourly, flagged, sig_out, ev):
    """各時段買進:比當天收盤便宜多少(cheap,負=比收盤便宜)、到隔天收盤的平均報酬(nxt)。
    條件:一般日子(all)、前一天有「有效/觀察」利空訊號(after_bear)。當天或隔天是除權息日的不算。
    best=平均最便宜的時段;avoid=明顯比收盤貴(t>=2)的時段中最貴的;clear=各時段差距是否夠明確。"""
    strong = {k for k, s in sig_out.items() if s["status"] in ("active", "watch")}
    prev_flag = {}
    for d, codes in flagged.items():
        for code, sig in codes.items():
            if strong & set(sig):
                prev_flag.setdefault(code, set()).add(d)
    rows = []   # (code, date, window, cheap, nxt)
    for code, days in hourly.items():
        evd = set(((ev.get(code) or {}).get("div") or {})) | set(((ev.get(code) or {}).get("split") or {}))
        ds = sorted(days)
        for j in range(1, len(ds) - 1):
            d, nd, pd = ds[j], ds[j + 1], ds[j - 1]
            if nd in evd or d in evd:
                continue
            day, close_n = days[d], days[nd]["close"]
            if abs(close_n / day["close"] - 1) > 0.11:   # 跨日超過漲跌停,資料有問題就跳過
                continue
            after = pd in prev_flag.get(code, ())
            for w in WINDOWS:
                rows.append((code, d, w, day[w] / day["close"] - 1, close_n / day[w] - 1, after))
    if not rows:
        return None
    out = {"windows": [{"key": w, "label": WINDOW_LABEL[w]} for w in WINDOWS],
           "first": min(r[1] for r in rows), "last": max(r[1] for r in rows)}
    for cond in ("all", "after_bear"):
        sel = [r for r in rows if cond == "all" or r[5]]
        days_n = len({r[1] for r in sel})
        res = {"days": days_n, "stock_days": len(sel) // len(WINDOWS), "by_window": {}}
        for w in WINDOWS:
            ws = [r for r in sel if r[2] == w]
            if not ws:
                continue
            cheap_rows = [(r[1], r[3]) for r in ws]
            tc, _ = day_t(cheap_rows)
            res["by_window"][w] = {
                "n": len(ws),
                "cheap": round(stx.mean(x for _, x in cheap_rows) * 100, 2),
                "below_close": round(sum(1 for _, x in cheap_rows if x < 0) / len(ws) * 100, 1),
                "t_cheap": round(tc, 2) if tc is not None else None,
                "nxt": round(stx.mean(r[4] for r in ws) * 100, 2),
            }
        bw = res["by_window"]
        res["ready"] = days_n >= WINDOW_MIN_DAYS
        worst = [w for w in bw if bw[w]["t_cheap"] is not None and bw[w]["t_cheap"] >= 2 and bw[w]["cheap"] > 0]
        res["avoid"] = max(worst, key=lambda w: bw[w]["cheap"]) if (worst and res["ready"]) else None
        res["clear"] = res["avoid"] is not None
        res["best"] = min(bw, key=lambda w: bw[w]["cheap"]) if (bw and res["clear"]) else None
        out[cond] = res
    return out


def level_rr(b, i):
    """現在收盤買進:停損 = 近 20 日最低價、目標 = 近 20 日最高價(都不含今天)。已跌破就沒有停損參考。"""
    if i < LEVEL_DAYS:
        return None
    seg = b[i - LEVEL_DAYS:i]
    c, lo, hi = b[i]["c"], min(x["l"] for x in seg), max(x["h"] for x in seg)
    out = {"stop": round(lo, 2), "target": round(hi, 2)}
    if c <= lo:
        out["note"] = "已跌破近20日低點,沒有下方支撐可當停損"
        return out
    risk, reward = (c - lo) / c * 100, (hi - c) / c * 100
    out.update({"risk": round(risk, 2), "reward": round(max(reward, 0), 2), "rr": round(max(reward, 0) / risk, 2)})
    return out


def build_today(today_hits, sig_out, bars, latest_date):
    """只列最新一根K(今天收盤)成立的訊號。分數 = 成立的「有效」訊號 T+5 超額加總(越負越偏空)。"""
    items = []
    for code, th in today_hits.items():
        if th["d"] != latest_date:
            continue
        b, i = bars[code], th["i"]
        sigs = [{"key": k, "name": sig_out[k]["name"], "status": sig_out[k]["status"],
                 "ex5": sig_out[k][f"t{MAIN_H}"].get("ex")} for k in th["signals"]]
        score = sum(s["ex5"] for s in sigs if s["status"] == "active" and s["ex5"] is not None)
        n_active = sum(1 for s in sigs if s["status"] == "active")
        n_watch = sum(1 for s in sigs if s["status"] == "watch")
        level = "high" if n_active else ("mid" if n_watch else "low")
        items.append({"code": code, "name": WATCHLIST_NAMES.get(code), "close": round(b[i]["c"], 2),
                      "pct": round((b[i]["c"] / b[i - 1]["c"] - 1) * 100, 2),
                      "signals": sigs, "n_active": n_active, "n_watch": n_watch, "level": level,
                      "score": round(score, 2), "rr": level_rr(b, i)})
    rank = {"high": 0, "mid": 1, "low": 2}
    items.sort(key=lambda x: (rank[x["level"]], x["score"], -x["n_watch"]))
    return items


def main():
    force = "--force" in sys.argv
    run_time = datetime.now(TAIPEI)
    today = run_time.strftime("%Y-%m-%d")
    bars, fresh, errors = load_bars(run_time)
    if len(bars) < MIN_UNIVERSE:
        raise RuntimeError(f"可用日K只有 {len(bars)} 檔,少於 {MIN_UNIVERSE} 檔,不更新成績單")
    latest = max(b[-1]["d"] for b in bars.values())
    if latest != today and not force:
        print(f"[略過] 日K最新日期是 {latest},不是今天 {today}(休市),不更新")
        return

    ev, ev_src, closes = fetch_corp_actions(list(bars), run_time)
    adjustments = {}
    for code, b in bars.items():
        a = adjust_bars(b, ev.get(code))
        if a:
            adjustments[code] = a

    result, today_hits, flagged = backtest(bars)

    prev = load_json_safe(OUT_FILE, {}) or {}
    try:
        windows = entry_windows(fetch_hourly(list(bars), ev, closes), flagged, result["signals"], ev)
    except Exception as e:  # noqa: BLE001  進場時段是加分資料,失敗就沿用上次結果
        print(f"[警告] 進場時段計算失敗: {e}")
        windows = None
    windows_src = "fresh" if windows else ("previous" if prev.get("entry_windows") else "none")
    windows = windows or prev.get("entry_windows")

    out = {
        "as_of": run_time.isoformat(),
        "latest_bar": latest,
        "stocks": len(bars),
        "fetched_fresh": fresh,
        "errors": errors[:10],
        "horizon_main": MAIN_H,
        "method": "超額報酬 = 個股 T+h 收盤報酬 - 自選股當天平均;t 值以天為單位;後1/3期間為驗證段;價格已還原除權息",
        "note": "規則式統計,不是預測,不構成買賣建議。自選股是事後挑的,會偏向漲過的股票。",
        "data_quality": {
            "corp_actions_source": ev_src,
            "adjusted_stocks": len(adjustments),
            "adjusted_events": sum(len(v) for v in adjustments.values()),
            "adjustments": adjustments,
            "etf_excluded": sorted(c for c in WATCHLIST_NAMES if is_etf(c)),
            "entry_windows_source": windows_src,
        },
        **result,
        "entry_windows": windows,
        "today": build_today(today_hits, result["signals"], bars, latest),
    }
    os.makedirs(DATA_DIR, exist_ok=True)
    write_json_atomic(OUT_FILE, out, indent=1)
    try:   # 收盤後的重大訊息(財報、董事會多在盤後公告),隔天盤前網頁就看得到
        import news
        news.update_news(list(WATCHLIST_NAMES), run_time, load_json_safe, write_json_atomic, force=True)
    except Exception as e:  # noqa: BLE001
        print(f"[警告] 收盤後重大訊息抓取失敗: {e}")

    print(f"[完成] {latest} 共 {len(bars)} 檔(重抓 {fresh} 檔),期間 {result['period'][0]}~{result['period'][1]}")
    print(f"  還原權值:{ev_src},{out['data_quality']['adjusted_stocks']} 檔共 {out['data_quality']['adjusted_events']} 次事件")
    for k, r in sorted(result["signals"].items(), key=lambda kv: kv[1][f"t{MAIN_H}"].get("ex") or 0):
        s, rr = r[f"t{MAIN_H}"], r.get("rr5") or {}
        print(f"  {r['status']:<8} {r['name']:<14} n={s.get('n', 0):<5} T+{MAIN_H}超額={s.get('ex')}% "
              f"跑輸={s.get('lose')}% t={s.get('t')} 驗證段={r['test'].get('ex')}% 損益比={rr.get('rr')}")
    print(f"  基準損益比:{(result['baseline'].get('rr5') or {}).get('rr')}")
    if windows:
        for cond in ("all", "after_bear"):
            c = windows.get(cond) or {}
            bw = c.get("by_window") or {}
            print(f"  進場時段[{cond}] {c.get('days')} 天,最佳={c.get('best')} 避開={c.get('avoid')} " +
                  " ".join(f"{w}:{bw[w]['cheap']:+.2f}%/t{bw[w]['t_cheap']}/隔日{bw[w]['nxt']:+.2f}%" for w in bw))
    print(f"  今天成立訊號的股票:{len(out['today'])} 檔")


if __name__ == "__main__":
    main()
