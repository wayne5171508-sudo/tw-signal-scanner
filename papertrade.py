#!/usr/bin/env python3
"""
每日模擬交易(紙上帳戶,不下單):本金 10 萬、當沖額度 90 萬,照自選股觀測員的推薦真的「做」一遍,看累積是賺是賠。

規則(全部是推薦當下就知道的事,不偷看後面走勢):
- 每檔取「第一次沒有警示」的那一輪(vc 是 ok 或 na、有方向);有警示(warn)、硬限制(bad)的輪次不做。
- 依出現時間先到先做,同一檔一天只做一次;每筆最多 30 萬(至少 1 張),當天進場金額合計不超過 90 萬。
- 出場照那一輪系統給的停損、停利:進場那根 5 分K 之後逐根檢查,同一根兩個都碰到算停損(保守);
  碰到就用停損/停利價成交(shortcut: 不算跳空滑價,急漲急跌時實際會更差);都沒碰到就用 13:25 那根收盤出場。停利在錯的一邊(例如貼漲停)就不設停利。
- 損益用 journal.costs(手續費 2.8 折、無條件捨去、當沖稅 0.15%)。
資料:data/team/日期.json(推薦)、data/bars5m(_top)/日期.json.gz(永豐 5 分K)。輸出 data/papertrade.json。

用法: python papertrade.py
"""

import glob
import gzip
import json
import os

import journal

START = "2026-11-13"
CAPITAL = 100_000
LIMIT = 900_000
PER_TRADE = 300_000
OUT = os.path.join("data", "papertrade.json")


def _mins(t):
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def _bars(day):
    out = {}
    for d in ("bars5m_top", "bars5m"):   # 自選股那份優先
        p = os.path.join("data", d, f"{day}.json.gz")
        if os.path.exists(p):
            with gzip.open(p, "rt", encoding="utf-8") as f:
                out.update({k: v["b"] for k, v in (json.load(f).get("stocks") or {}).items()})
    return out


def candidates(picks):
    """每檔第一個可做的輪次,依時間排序。"""
    out = []
    for code, p in picks.items():
        for h in p.get("hist") or [p["first"]]:
            pl = h.get("plan") or {}
            if h.get("vc") in ("ok", "na") and h.get("side") in (1, -1) and pl.get("stop"):
                out.append({"code": code, "name": p.get("name", ""), "t": h["t"], "side": h["side"],
                            "entry": h["price"], "stop": pl["stop"], "target": pl.get("target")})
                break
    return sorted(out, key=lambda c: (c["t"], c["code"]))


def simulate(c, bars):
    """回傳 (出場價, 出場時間, 原因);沒有進場後的 K 棒回傳 None。"""
    side, stop, tgt = c["side"], c["stop"], c["target"]
    if tgt is not None and (tgt - c["entry"]) * side <= 0:
        tgt = None
    start = _mins(c["t"]) // 5 * 5 + 5   # 跳過進場那一根(那根裡進場前的高低點不能用)
    after = [b for b in bars if _mins(b[0]) >= start]
    if not after:
        return None
    for t, _o, h, l, _c, _v in after:
        if side == 1:
            if l <= stop:
                return stop, t, "停損"
            if tgt is not None and h >= tgt:
                return tgt, t, "停利"
        else:
            if h >= stop:
                return stop, t, "停損"
            if tgt is not None and l <= tgt:
                return tgt, t, "停利"
    return after[-1][4], after[-1][0], "收盤"


def run_day(day, picks, bars):
    used, trades, seen = 0, [], set()
    for c in candidates(picks):
        code = c["code"].split("#")[0]
        if code in seen or code not in bars:
            continue
        lot_value = c["entry"] * 1000
        lots = max(1, int(PER_TRADE // lot_value))
        if used + lots * lot_value > LIMIT:
            continue
        res = simulate(c, bars[code])
        if not res:
            continue
        seen.add(code)
        used += lots * lot_value
        exit_, exit_t, why = res
        gross, fee, tax, net = journal.costs("long" if c["side"] == 1 else "short", c["entry"], exit_, lots * 1000)
        trades.append({**c, "code": code, "lots": lots, "exit": exit_, "exit_t": exit_t, "why": why,
                       "fee": fee + tax, "net": net})
    return {"date": day, "used": round(used), "trades": trades, "net": sum(t["net"] for t in trades)}


def build():
    days, equity = [], CAPITAL
    for f in sorted(glob.glob(os.path.join("data", "team", "20*-*-*.json"))):
        day = os.path.basename(f)[:10]
        with open(f, encoding="utf-8") as fh:
            team = json.load(fh)
        if day < START or not team.get("closed"):
            continue
        bars = _bars(day)
        if not bars:
            continue
        d = run_day(day, team.get("picks") or {}, bars)
        equity += d["net"]
        d["equity"] = equity
        days.append(d)
    n = [t for d in days for t in d["trades"]]
    return {"capital": CAPITAL, "limit": LIMIT, "per_trade": PER_TRADE, "start": START, "equity": equity,
            "net": equity - CAPITAL, "trades": len(n), "wins": sum(t["net"] > 0 for t in n), "days": days}


if __name__ == "__main__":
    r = build()
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(r, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, OUT)
    for d in r["days"]:
        print(f"{d['date']} 筆數 {len(d['trades']):2d} 用額度 {d['used']:>7,} 損益 {d['net']:>+8,} 帳戶 {d['equity']:>9,}")
    print(f"合計 {r['trades']} 筆,賺 {r['wins']} 筆,累積 {r['net']:+,} 元")
