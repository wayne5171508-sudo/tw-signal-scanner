#!/usr/bin/env python3
"""
爆量雷達 - 利空訊號回測與成績單(GitHub Actions 每個交易日收盤後跑一次)

做的事:
1) 抓自選股近 260 根正式日K(沿用 intraday.py 的抓法與自選股清單,清單只要維護一份)。
2) 逐日只用「當天收盤前看得到的資料」判斷每一種利空訊號有沒有成立。
3) 看訊號後 T+1/T+3/T+5 收盤的「超額報酬」= 該股報酬 - 自選股當天平均報酬(扣掉大盤漲跌)。
4) 每個訊號依實測結果自動評級(有效/觀察/無效),寫進 data/bear_scorecard.json;
   今天收盤成立的訊號也一併列出,網頁直接讀這個檔顯示。

評級規則(全部都要用數字過關,不是手動決定):
- 有效 active:全期間 T+5 超額 < 0、跑輸機率比基準高 3 個百分點以上、
  以「天」為單位算的 t 值 <= -2,而且後 1/3 期間(驗證段)超額也 < 0。
- 觀察 watch:全期間 T+5 超額 < 0 且 t 值 <= -1.5,但沒有全部過關。
- 無效 inactive:其餘。訊號照算,但網頁不該把它標成利空。
t 值的算法:同一天多檔同時出現的訊號先平均成一筆,避免同一天的大盤因素被重複計算。

產出:data/bear_scorecard.json
用法:python bear_signals.py(加 --force 在非交易日也重算,手動測試用)
"""

import json
import os
import statistics as stx
import sys
import time
from datetime import datetime

from intraday import (DATA_DIR, DAILY_OFFICIAL_FILE, TAIPEI, WATCHLIST_NAMES, _EmptyHistory,
                      fetch_history_one, load_json_safe, write_json_atomic)

OUT_FILE = os.path.join(DATA_DIR, "bear_scorecard.json")
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


def _ma(b, n, k):
    return sum(x["c"] for x in b[k - n + 1:k + 1]) / n


def signals_on(b, i):
    """只用 b[:i+1](當天收盤為止)的資料,回傳當天成立的訊號 key。"""
    if i < WARMUP:
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


def load_bars(run_time):
    """每檔重抓一次日K(收盤後抓才是正式的今天那根)。抓不到的退回 daily_official.json 的舊記錄。"""
    fallback = (load_json_safe(DAILY_OFFICIAL_FILE, {}) or {}).get("bars") or {}
    bars, fresh, errors = {}, 0, []
    started = time.time()
    for code in WATCHLIST_NAMES:
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
            clean = [x for x in got if x.get("d") and x.get("c") and x.get("h") and x.get("l") and x["h"] >= x["l"]]
            clean.sort(key=lambda x: x["d"])
            if len(clean) >= MIN_BARS:
                bars[code] = clean
    return bars, fresh, errors


def day_t(rows):
    """rows=[(日期, 超額)]。同一天先平均成一筆再算 t 值,回傳 (t, 天數)。"""
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
    hits = {k: {h: [] for h in HORIZONS} for k in SIGNALS}
    today_hits = {}
    for code, b in bars.items():
        for i in range(WARMUP, len(b)):
            d = b[i]["d"]
            for h in HORIZONS:
                if (code, i, h) in fwd and (d, h) in mkt:
                    base[h].append(fwd[(code, i, h)] - mkt[(d, h)])
            sig = signals_on(b, i)
            if i == len(b) - 1 and sig:
                today_hits[code] = {"d": d, "signals": sig, "close": b[i]["c"],
                                    "pct": round((b[i]["c"] / b[i - 1]["c"] - 1) * 100, 2)}
            for k in sig:
                for h in HORIZONS:
                    if (code, i, h) in fwd and (d, h) in mkt:
                        hits[k][h].append((d, fwd[(code, i, h)] - mkt[(d, h)]))

    base_out = {}
    for h in HORIZONS:
        xs = base[h]
        base_out[f"t{h}"] = {"n": len(xs), "lose": round(sum(1 for x in xs if x < 0) / len(xs) * 100, 1) if xs else None}

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
        rec["status"], rec["status_reason"] = grade(rec[f"t{MAIN_H}"], rec["test"])
        sig_out[k] = rec

    period = (all_dates[0], all_dates[-1]) if all_dates else (None, None)
    return {"period": period, "holdout_from": split, "recent_from": recent_from,
            "baseline": base_out, "signals": sig_out}, today_hits


def build_today(today_hits, sig_out, latest_date):
    """只列最新一根K(今天收盤)成立的訊號。分數 = 成立的「有效」訊號 T+5 超額加總(越負越偏空)。"""
    items = []
    for code, th in today_hits.items():
        if th["d"] != latest_date:
            continue
        sigs = [{"key": k, "name": sig_out[k]["name"], "status": sig_out[k]["status"],
                 "ex5": sig_out[k][f"t{MAIN_H}"].get("ex")} for k in th["signals"]]
        score = sum(s["ex5"] for s in sigs if s["status"] == "active" and s["ex5"] is not None)
        items.append({"code": code, "name": WATCHLIST_NAMES.get(code), "close": th["close"], "pct": th["pct"],
                      "signals": sigs, "n_active": sum(1 for s in sigs if s["status"] == "active"),
                      "score": round(score, 2)})
    items.sort(key=lambda x: (x["score"], -x["n_active"]))
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

    result, today_hits = backtest(bars)
    out = {
        "as_of": run_time.isoformat(),
        "latest_bar": latest,
        "stocks": len(bars),
        "fetched_fresh": fresh,
        "errors": errors[:10],
        "horizon_main": MAIN_H,
        "method": "超額報酬 = 個股 T+h 收盤報酬 - 自選股當天平均;t 值以天為單位;後1/3期間為驗證段",
        "note": "規則式統計,不是預測,不構成買賣建議。自選股是事後挑的,會偏向漲過的股票。",
        **result,
        "today": build_today(today_hits, result["signals"], latest),
    }
    os.makedirs(DATA_DIR, exist_ok=True)
    write_json_atomic(OUT_FILE, out, indent=1)

    print(f"[完成] {latest} 共 {len(bars)} 檔(重抓 {fresh} 檔),期間 {result['period'][0]}~{result['period'][1]}")
    for k, r in sorted(result["signals"].items(), key=lambda kv: kv[1][f"t{MAIN_H}"].get("ex") or 0):
        s = r[f"t{MAIN_H}"]
        print(f"  {r['status']:<8} {r['name']:<14} n={s.get('n', 0):<5} T+{MAIN_H}超額={s.get('ex')}% "
              f"跑輸={s.get('lose')}% t={s.get('t')} 驗證段={r['test'].get('ex')}%")
    print(f"  今天成立訊號的股票:{len(out['today'])} 檔")


if __name__ == "__main__":
    main()
