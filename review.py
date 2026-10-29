#!/usr/bin/env python3
"""
爆量雷達 - 收盤後自動復盤 + 盤中走勢存檔(由 intraday.py 呼叫,不用單獨執行)

1) archive_day():把一天的盤中走勢(約每5分鐘一筆價格、累積量、成交金額)存進
   data/intraday_archive/日期.json。intraday_history.json 隔天就會被清掉,
   不存下來的話,之後要驗證 RSI 或任何盤中策略都沒有資料可以回測。
2) build_review():13:31 之後每一輪都重算一次(收盤資料、焦點成績、全市場收盤價陸續到齊),
   寫進 data/review.json,網頁「今日重點」收盤後最上面的「今日復盤」就是讀這個檔。

全部都是把當天已經發生的事情機械式整理出來,不做預測、不是買賣建議。
"""

import json
import os
from datetime import datetime

DATA_DIR = "data"
ARCHIVE_DIR = os.path.join(DATA_DIR, "intraday_archive")
REVIEW_FILE = os.path.join(DATA_DIR, "review.json")
FOCUS_FILE = os.path.join(DATA_DIR, "focus_log.json")
SIGNAL_FILE = os.path.join(DATA_DIR, "signal_log.json")
MARKET_DIR = os.path.join(DATA_DIR, "mkt")
MARKET_FILE = os.path.join(DATA_DIR, "market_scan.json")
ARCHIVE_KEEP_DAYS = 120     # 約半年;一天約60~90KB
REVIEW_KEEP_DAYS = 30
COST_PCT = 0.23         # 當沖來回成本(手續費 2.8 折 + 當沖證交稅),和訊號實驗室、daytrade_lab 一致
NEAR_HIGH_PCT = 1.0     # 收盤離今日最高/最低在 1% 以內算「收在高點/低點」


def _num(*xs):
    return all(isinstance(x, (int, float)) and not isinstance(x, bool) and x == x for x in xs)


def _load(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def _write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    os.replace(tmp, path)


def _c(x):
    """整數價格存成整數(506.0 -> 506),檔案小一點。"""
    return int(x) if isinstance(x, float) and x.is_integer() else x


def _median(vs):
    vs = sorted(vs)
    n = len(vs)
    if not n:
        return None
    return vs[n // 2] if n % 2 else (vs[n // 2 - 1] + vs[n // 2]) / 2


def _r(x, n=2):
    return round(x, n) if _num(x) else None


# ---------------------------------------------------------------- 盤中走勢存檔

def archive_day(history):
    """history = intraday_history.json 的內容。存成欄位式的精簡格式:
    {date, m:[開盤後第幾分鐘], px:{代號:[價格]}, v:{代號:[累積量]}, a:{代號:[累積成交金額(億)]}, idx:{代號:[點數]}}
    缺值用 null。少於10個時間點的日子(例如颱風假只跑了幾輪)不存。回傳是否有寫檔。"""
    if not isinstance(history, dict):
        return False
    date = history.get("date")
    pts = [p for p in (history.get("points") or []) if p.get("t") and p.get("wl")]
    if not date or len(pts) < 10:
        return False
    try:
        d0 = datetime.fromisoformat(pts[0]["t"]).replace(hour=9, minute=0, second=0, microsecond=0)
    except ValueError:
        return False
    ms = []
    for p in pts:
        try:
            ms.append(round((datetime.fromisoformat(p["t"]) - d0).total_seconds() / 60, 1))
        except ValueError:
            ms.append(None)
    codes = sorted({c for p in pts for c in (p.get("wl") or {})})
    idx_codes = sorted({c for p in pts for c in (p.get("idx") or {})})
    out = {"date": date, "m": ms,
           "px": {c: [_c((p.get("wl") or {}).get(c)) for p in pts] for c in codes},
           "v": {c: [_c((p.get("wlv") or {}).get(c)) for p in pts] for c in codes},
           "idx": {c: [_c((p.get("idx") or {}).get(c)) for p in pts] for c in idx_codes}}
    if any(p.get("wla") for p in pts):
        out["a"] = {c: [(p.get("wla") or {}).get(c) for p in pts] for c in codes}
    _write(os.path.join(ARCHIVE_DIR, date + ".json"), out)
    files = sorted(fn for fn in os.listdir(ARCHIVE_DIR) if fn.endswith(".json"))
    for fn in files[:-ARCHIVE_KEEP_DAYS]:
        try:
            os.remove(os.path.join(ARCHIVE_DIR, fn))
        except OSError:
            pass
    return True


def load_archives():
    """回傳 {日期: 存檔內容},由舊到新。"""
    out = {}
    if os.path.isdir(ARCHIVE_DIR):
        for fn in sorted(os.listdir(ARCHIVE_DIR)):
            if fn.endswith(".json"):
                d = _load(os.path.join(ARCHIVE_DIR, fn), None)
                if isinstance(d, dict) and d.get("m"):
                    out[fn[:-5]] = d
    return out


# ---------------------------------------------------------------- 復盤用的小計算

def _levels(q):
    """和網頁「自動」依據一致:CDP 可靠就用 CDP 近高/近低,否則用近5日高低(至少3天資料)。"""
    cdp, sw = q.get("cdp") or {}, q.get("swing") or {}
    if _num(cdp.get("nh"), cdp.get("nl")) and cdp.get("reliability") != "low":
        return cdp["nh"], cdp["nl"], "CDP"
    if _num(sw.get("h5"), sw.get("l5")) and (sw.get("d5") or 0) >= 3:
        return sw["h5"], sw["l5"], f"近{sw.get('d5')}日"
    if _num(cdp.get("nh"), cdp.get("nl")):
        return cdp["nh"], cdp["nl"], "CDP"
    return None, None, None


def _brief(q, **extra):
    d = {"code": q["code"], "name": q.get("name"), "last": q.get("last"), "pct": _r(q.get("pct")), "pace": _r(q.get("pace"))}
    d.update(extra)
    return d


def _group_flow(history, groups):
    """和網頁資金流向同一套算法:每段新增成交金額,價格漲歸買、跌歸賣、平盤沿用上一次方向。
    有實際成交金額(wla)就用,沒有就用「新增量 × 價格」估。回傳 {族群: 億}。"""
    pts = [p for p in (history or {}).get("points") or [] if p.get("wl") and p.get("wlv")]
    if len(pts) < 2:
        return {}
    st, flow = {}, {}
    for p in pts:
        wla = p.get("wla") or {}
        for code, v in p["wlv"].items():
            g = groups.get(code)
            px = p["wl"].get(code)
            if not g or not _num(v, px):
                continue
            s = st.get(code)
            if s is None:
                st[code] = {"v": v, "px": px, "a": wla.get(code), "sg": 0}
                continue
            dv = max(0.0, v - s["v"])
            a_now = wla.get(code)
            if _num(a_now, s["a"]) and a_now >= s["a"]:
                amt = a_now - s["a"]                      # 已經是億
            else:
                amt = dv * 1000 * px / 1e8
            d = px - s["px"]
            sg = 1 if d > 0 else (-1 if d < 0 else s["sg"])
            if sg and amt > 0:
                flow[g] = flow.get(g, 0.0) + sg * amt
            s.update({"v": v, "px": px, "a": a_now if _num(a_now) else s["a"]})
            if d:
                s["sg"] = sg
    return {g: round(v, 2) for g, v in flow.items()}


def _signals_today(day, strats):
    """訊號實驗室當天的結果:每個策略在每個時點挑到哪些股票、抱到收盤的報酬、同時點全體平均。"""
    close = (day or {}).get("close") or {}
    if not close:
        return None
    out = {}
    for sn, rows in sorted(((day.get("snaps") or {}).items()), key=lambda kv: int(kv[0])):
        rets = {c: (close[c] / r[0] - 1) * 100 for c, r in rows.items() if c in close and r[0]}
        if not rets:
            continue
        base = sum(rets.values()) / len(rets)
        for name, (side, cond) in strats.items():
            sg = 1 if side == "long" else -1
            for c, r in rows.items():
                if c not in rets or not (_num(r[1], r[2]) and r[3] and r[4]):
                    continue
                dd = (r[0] / r[3] - 1) * 100
                up = (r[0] / r[4] - 1) * 100
                if cond(r[1], r[2], dd, up):
                    s = out.setdefault(name, {"side": side, "picks": []})
                    s["picks"].append({"code": c, "snap": int(sn), "entry": r[0], "ret": _r(sg * rets[c]), "base": _r(sg * base)})
    for name, s in out.items():
        n = len(s["picks"])
        s["n"] = n
        s["avg"] = _r(sum(p["ret"] for p in s["picks"]) / n)
        s["edge"] = _r(sum(p["ret"] - p["base"] for p in s["picks"]) / n)
        s["net"] = _r(s["avg"] - COST_PCT)
        s["picks"] = sorted(s["picks"], key=lambda p: -p["ret"])[:8]
    return out


def _market_today(date, watch_codes, names):
    """全市場快照:每個掃描時點量能最大、又不在自選裡的股票,收盤後的表現(收盤價要等 close_market 抓到)。"""
    day = _load(os.path.join(MARKET_DIR, date + ".json"), None)
    if not isinstance(day, dict):
        return None
    names = dict(names)
    mk = _load(MARKET_FILE, {}) or {}
    if mk.get("date") == date:
        for sc in (mk.get("scans") or {}).values():
            for it in sc.get("items") or []:
                names.setdefault(it.get("code"), it.get("name"))
    close = day.get("close") or {}
    out = []
    for sn, sv in sorted((day.get("snaps") or {}).items(), key=lambda kv: int(kv[0])):
        rows = sv.get("rows") or {}
        cands = [(c, r) for c, r in rows.items() if c not in watch_codes and r[1] >= 1000 and _num(r[5]) and r[5] >= 3]
        cands.sort(key=lambda cr: -cr[1][5])
        for c, r in cands[:5]:
            cl = (close.get(c) or [None])[0]
            out.append({"code": c, "name": names.get(c) or c, "snap": int(sn), "entry": r[0], "pct": _r(r[2]), "pace": _r(r[5]),
                        "close": cl, "ret": _r((cl / r[0] - 1) * 100) if _num(cl) and r[0] else None})
    return {"items": out, "has_close": bool(close)}


def _sentence_list(rv):
    """幾句話的總結,全部是當天發生的事實。"""
    s = []
    rg, br = rv.get("regime") or {}, rv.get("breadth") or {}
    lab = {"strong": "偏強", "weak": "偏弱", "normal": "平穩"}.get(rg.get("label"), "-")
    taiex = next((x for x in rv.get("indices") or [] if x["code"] == "TWS:TSE01:INDEX"), None)
    s.append(f"盤勢{lab}:自選股漲跌中位數 {rg.get('median_pct', '-')}%,漲 {br.get('up', 0)} 檔、跌 {br.get('down', 0)} 檔"
             + (f",加權 {taiex['pct']:+.2f}%" if taiex and _num(taiex.get("pct")) else "")
             + (f",漲停 {br['limit_up']} 檔" if br.get("limit_up") else "") + (f",跌停 {br['limit_down']} 檔" if br.get("limit_down") else "") + "。")
    gs = [g for g in rv.get("groups") or [] if g.get("n", 0) >= 2 and g.get("g") != "其他"]
    if len(gs) >= 2:
        top, bot = gs[0], gs[-1]
        s.append(f"最強族群 {top['g']}({top['avg']:+.2f}%),最弱 {bot['g']}({bot['avg']:+.2f}%)。")
    fc = rv.get("focus") or {}
    if fc.get("n"):
        t = f"焦點名單 {fc['n']} 檔"
        if fc.get("in_n"):
            t += f",其中量大上漲 {fc['in_n']} 檔收盤平均 {fc['in_avg']:+.2f}%"
            if fc.get("in_excess") is not None:
                t += f"(比同時段平均{'強' if fc['in_excess'] >= 0 else '弱'} {abs(fc['in_excess']):.2f}%)"
        s.append(t + "。")
    lv = rv.get("levels") or {}
    if lv.get("breakout") or lv.get("breakdown"):
        s.append(f"收盤站上壓力 {len(lv.get('breakout') or [])} 檔、跌破支撐 {len(lv.get('breakdown') or [])} 檔。")
    sg = rv.get("signals") or {}
    if sg:
        best = max(sg.items(), key=lambda kv: kv[1]["edge"])
        s.append(f"訊號實驗室今天「{best[0]}」{best[1]['n']} 筆,平均 {best[1]['avg']:+.2f}%(扣成本 {best[1]['net']:+.2f}%)。單日結果不代表策略有效。")
    return s


# ---------------------------------------------------------------- 主函式

def build_review(run_time, watchlist_out, indices_out, regime, groups, strats, history=None):
    """收盤後呼叫(開盤後第271分鐘起每一輪都重算,資料陸續到齊)。寫入 data/review.json 並回傳當天摘要。"""
    date = run_time.strftime("%Y-%m-%d")
    qs = [q for q in watchlist_out if _num(q.get("last"), q.get("pct"))]
    stocks = [q for q in qs if q["code"] in groups]         # 不含ETF
    if len(stocks) < 10:
        return None
    names = {q["code"]: q.get("name") for q in watchlist_out}

    lim_up = [q for q in stocks if _num(q.get("upper_limit")) and q["last"] >= q["upper_limit"] - 1e-6]
    lim_dn = [q for q in stocks if _num(q.get("lower_limit")) and q["last"] <= q["lower_limit"] + 1e-6]
    breadth = {"up": sum(1 for q in stocks if q["pct"] > 0), "down": sum(1 for q in stocks if q["pct"] < 0),
               "flat": sum(1 for q in stocks if q["pct"] == 0), "limit_up": len(lim_up), "limit_down": len(lim_dn)}

    srt = sorted(stocks, key=lambda q: -q["pct"])
    movers = {"up": [_brief(q) for q in srt[:5] if q["pct"] > 0], "down": [_brief(q) for q in srt[::-1][:5] if q["pct"] < 0]}
    big_vol = sorted([q for q in stocks if _num(q.get("pace"))], key=lambda q: -q["pace"])[:5]

    by_g = {}
    for q in stocks:
        by_g.setdefault(groups[q["code"]], []).append(q["pct"])
    flow = _group_flow(history, groups) if history else {}
    gl = [{"g": g, "avg": _r(sum(v) / len(v)), "n": len(v), "flow": flow.get(g)} for g, v in by_g.items()]
    gl.sort(key=lambda x: -x["avg"])

    breakout, breakdown, new_hi, new_lo, strong_close, weak_close = [], [], [], [], [], []
    for q in qs:
        res, sup, basis = _levels(q)
        last, hi, lo = q["last"], q.get("high"), q.get("low")
        if _num(res) and last > res:
            breakout.append(_brief(q, line=_r(res), basis=basis))
        elif _num(sup) and last < sup:
            breakdown.append(_brief(q, line=_r(sup), basis=basis))
        sw = q.get("swing") or {}
        if _num(sw.get("h20"), hi) and (sw.get("d20") or 0) >= 20 and hi > sw["h20"]:
            new_hi.append(_brief(q, prev_h20=sw["h20"]))
        if _num(sw.get("l20"), lo) and (sw.get("d20") or 0) >= 20 and lo < sw["l20"]:
            new_lo.append(_brief(q, prev_l20=sw["l20"]))
        pace = q.get("pace") if _num(q.get("pace")) else 0
        if _num(hi) and hi > 0 and q["pct"] >= 3 and (hi - last) / hi * 100 <= NEAR_HIGH_PCT and pace >= 1.5:
            strong_close.append(_brief(q))
        if _num(lo) and lo > 0 and q["pct"] <= -3 and (last - lo) / lo * 100 <= NEAR_HIGH_PCT and pace >= 1.5:
            weak_close.append(_brief(q))
    for lst in (breakout, strong_close, new_hi):
        lst.sort(key=lambda x: -(x["pct"] or 0))
    for lst in (breakdown, weak_close, new_lo):
        lst.sort(key=lambda x: (x["pct"] or 0))

    # 焦點成績(focus_log 收盤評估完才有 ret)
    fday = ((_load(FOCUS_FILE, {}) or {}).get("days") or {}).get(date) or {}
    picks = [dict(pk, code=c) for c, pk in (fday.get("picks") or {}).items()]
    focus = {"n": len(picks), "evaluated": bool(fday.get("evaluated"))}
    ins = [p for p in picks if p.get("side") == "in" and "ret" in p]
    if ins:
        ex = [p["ret"] - p["mkt"] for p in ins if "mkt" in p]
        focus.update({"in_n": len(ins), "in_avg": _r(sum(p["ret"] for p in ins) / len(ins)),
                      "in_win": sum(1 for p in ins if p["ret"] > 0), "in_excess": _r(sum(ex) / len(ex)) if ex else None})
    focus["list"] = sorted([{"code": p["code"], "name": p.get("name"), "t": (p.get("t") or "")[11:16], "price": p.get("price"),
                             "side": p.get("side"), "pace": p.get("pace"), "ret": p.get("ret"), "mkt": p.get("mkt")} for p in picks],
                           key=lambda x: (x["ret"] is None, -(x["ret"] or 0)))[:12]

    sday = ((_load(SIGNAL_FILE, {}) or {}).get("days") or {}).get(date)
    signals = _signals_today(sday, strats) if sday else None

    watch = [x for x in strong_close if any(b["code"] == x["code"] for b in breakout)]
    rv = {
        "date": date, "as_of": run_time.strftime("%H:%M"),
        "regime": regime or {},
        "indices": [{"code": q["code"], "name": q.get("name"), "last": q.get("last"), "pct": _r(q.get("pct"))} for q in indices_out if _num(q.get("last"))],
        "breadth": breadth,
        "limit_up": [_brief(q) for q in lim_up], "limit_down": [_brief(q) for q in lim_dn],
        "movers": movers,
        "big_volume": [_brief(q) for q in big_vol],
        "groups": gl,
        "flow_source": "成交金額" if history and any(p.get("wla") for p in (history.get("points") or [])) else "量×價估算",
        "levels": {"breakout": breakout[:10], "breakdown": breakdown[:10], "new_high20": new_hi[:10], "new_low20": new_lo[:10]},
        "strong_close": strong_close[:8], "weak_close": weak_close[:8],
        "watch_next": {"strong": watch[:6], "weak": [x for x in weak_close if any(b["code"] == x["code"] for b in breakdown)][:6]},
        "focus": focus,
        "signals": signals,
        "market": _market_today(date, set(names), names),
    }
    rv["summary"] = _sentence_list(rv)

    store = _load(REVIEW_FILE, {}) or {}
    days = store.setdefault("days", {})
    days[date] = rv
    for d in sorted(days)[:-REVIEW_KEEP_DAYS]:
        days.pop(d, None)
    store["latest"] = max(days)
    _write(REVIEW_FILE, store)
    return rv
