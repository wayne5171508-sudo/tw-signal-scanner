#!/usr/bin/env python3
"""
爆量雷達 - 全市場雷達(盤中每一輪由 intraday.py 呼叫,資料來自永豐快照,一輪約 650 檔)

1. 全市場統計:漲跌家數、平均漲跌、爆量家數(量能 >= 3 倍)、漲停/跌停家數、多空分數分布、各產業強弱。
2. 每檔用 bias.py 同一套多空分數打分(大盤、均價線、開盤區間、量能、當日位置、產業同步),
   分數最高的做多、最低的做空各挑 TOP_N 檔(分數至少 ±PICK_MIN、今天量夠大、沒有被大盤反向或貼近漲跌停否決)。
3. 挑出來的再細查:永豐 1 分K 組的真實 5 分K(開盤區間、這一根高低)、全市場處置/注意/不可當沖名單、
   前一日高低收算的 CDP 支撐壓力,交給 team.py 的六個觀測員檢查,決策員給結論。
4. 寫 data/market_radar.json(網頁「盤中雷達」用),每天的推薦紀錄寫 data/market/YYYY-MM-DD.json(永久保存),
   收盤後算每檔「照第一次推薦方向、用當時價格」的事後結果,data/market/index.json 是每天的摘要。

和自選股的差別:全市場沒有逐檔的重大訊息(新聞分析員標「未查」)、也沒有近 5/10/20 日高低(支撐壓力只用 CDP)。
推薦只是機械式篩選,目前回測還沒有站得住的多空分數規則,決策員最多給到「僅觀察」;要不要做由你判斷,不構成買賣建議。
"""

import os

import bias
import team

DATA_DIR = "data"
OUT_FILE = os.path.join(DATA_DIR, "market_radar.json")
DAY_DIR = os.path.join(DATA_DIR, "market")
INDEX_FILE = os.path.join(DAY_DIR, "index.json")
STATE_FILE = os.path.join(DATA_DIR, "mkt_state_today.json")   # 均價線歷史、開盤區間(只放盤中迴圈那台機器,不進 repo)
TOP_N = 15                 # 做多、做空各挑幾檔
PICK_MIN = 40              # 分數至少 ±40 才列入(門檻 60 以上會標「達門檻」)
ROOM_MIN_PCT = 3.0         # 離漲停(做空是跌停)至少要有幾 % 才推
EARLY_MIN_VOL, MIN_VOL = 300, 1000   # 09:30 前累積量 >= 300 張、之後 >= 1000 張才列入推薦
VWAP_LAG = 14              # 均價線方向:和約 15 分鐘前比(和 intraday.BIAS_VWAP_LAG 一樣)
ORB_FROM, ORB_TO = 15, 25  # 09:15~09:25 第一輪記下當時的今日高低當開盤區間(挑出來的會再用真實 5 分K 校正)
MOOD_KEEP = 140
NO_NEW_AFTER = 265         # 13:25 起是收盤集合競價,這之後不新增推薦、不翻方向(價格成交不到),只更新既有的
COUNTER_N = 6              # 逆勢觀察名單最多幾檔
CAT_NAMES = {
    "01": "水泥", "02": "食品", "03": "塑膠", "04": "紡織", "05": "電機機械", "06": "電器電纜", "08": "玻璃陶瓷", "09": "造紙",
    "10": "鋼鐵", "11": "橡膠", "12": "汽車", "13": "電子", "14": "建材營造", "15": "航運", "16": "觀光餐旅", "17": "金融保險",
    "18": "貿易百貨", "19": "綜合", "20": "其他", "21": "化學", "22": "生技醫療", "23": "油電燃氣", "24": "半導體",
    "25": "電腦週邊", "26": "光電", "27": "通信網路", "28": "電子零組件", "29": "電子通路", "30": "資訊服務", "31": "其他電子",
    "32": "文化創意", "33": "農業科技", "34": "電子商務", "35": "綠能環保", "36": "數位雲端", "37": "運動休閒", "38": "居家生活",
    "80": "管理股票",
}


def _n(x):
    return team._n(x)


def _load(path, default):
    return team._load(path, default)


def cat_name(c):
    return CAT_NAMES.get(str(c or "").zfill(2), "未分類") if c else "未分類"


def quotes(live, watch):
    """永豐快照列 → 和自選股報價一樣格式的 dict(只留有成交的普通股)。"""
    out = []
    for code, r in (live.get("rows") or {}).items():
        last, prev, vol = _n(r.get("6")), _n(r.get("21")), _n(r.get("200013"))
        if not (last and prev and vol):
            continue
        amt = _n(r.get("200067"))
        out.append({"code": code, "name": r.get("200009") or code, "last": last, "prev_close": prev, "pct": _n(r.get("56")),
                    "open": _n(r.get("19")), "high": _n(r.get("12")), "low": _n(r.get("13")), "volume": vol,
                    "vwap": amt / (vol * 1000) if amt and vol else None,
                    "upper_limit": _n(r.get("75")), "lower_limit": _n(r.get("76")), "pace": (live.get("pace") or {}).get(code),
                    "cat": str(r.get("cat") or ""), "dt": r.get("dt") or "", "in_watch": code in watch})
    return out


def _cdp(hlc):
    """前一日高低收 → CDP(和 intraday.compute_cdp 同公式)。"""
    if not hlc:
        return None
    h, l, c = hlc[0], hlc[1], hlc[2]
    cdp = (h + l + 2 * c) / 4
    rng = (h - l) / c * 100 if c else 0
    return {"cdp": cdp, "nh": 2 * cdp - l, "nl": 2 * cdp - h, "ah": cdp + (h - l), "al": cdp - (h - l),
            "reliability": "low" if rng < 1.5 else "ok"}


def update_state(state, qs, m):
    """均價線歷史(每檔每輪一點,只留最近 30 分鐘)與開盤區間(09:15~09:25 第一輪的今日高低)。"""
    vw, orb = state.setdefault("vw", {}), state.setdefault("orb", {})
    for q in qs:
        if q["vwap"]:
            lst = vw.setdefault(q["code"], [])
            if not lst or lst[-1][0] != round(m, 1):
                lst.append([round(m, 1), round(q["vwap"], 4)])
            vw[q["code"]] = [x for x in lst if x[0] >= m - 30]
        if ORB_FROM <= m < ORB_TO and q["code"] not in orb and q["high"] and q["low"]:
            orb[q["code"]] = [q["high"], q["low"]]


def vwap_prev(state, code, m):
    lst = [v for t, v in (state.get("vw") or {}).get(code, []) if t <= m - VWAP_LAG]
    return lst[-1] if lst else None


def score_all(qs, state, m, tse):
    """全部股票打多空分數。產業同步用同產業其他股票的平均漲跌(全市場版的「族群」)。"""
    by_cat = {}
    for q in qs:
        if q["pct"] is not None:
            s = by_cat.setdefault(q["cat"], [0.0, 0])
            s[0] += q["pct"]
            s[1] += 1
    tse_open = _n(tse.get("open"))
    for q in qs:
        s = by_cat.get(q["cat"]) or [0.0, 0]
        n_other = s[1] - (1 if q["pct"] is not None else 0)
        grp = (s[0] - (q["pct"] or 0)) / n_other if q["cat"] and n_other >= 2 else None
        o = (state.get("orb") or {}).get(q["code"]) or [None, None]
        f = {"last": q["last"], "prev_close": q["prev_close"], "vwap": q["vwap"], "vwap_prev": vwap_prev(state, q["code"], m),
             "orb_hi": o[0], "orb_lo": o[1], "pace": q["pace"], "day_hi": q["high"], "day_lo": q["low"],
             "mkt_pct": _n(tse.get("pct")), "mkt_last": _n(tse.get("last")), "mkt_open": tse_open, "grp_pct": grp,
             "up_lim": q["upper_limit"], "lo_lim": q["lower_limit"]}
        q["_f"] = f
        r = bias.score(f)
        q.update({"score": r["score"], "side": r["side"], "parts": r["parts"], "veto": r["veto"], "grp_pct": grp})
    return by_cat


def stats(qs, by_cat, mood):
    pct = [q["pct"] for q in qs if q["pct"] is not None]
    pct_sorted = sorted(pct)
    cats = []
    for c, (tot, n) in by_cat.items():
        if n >= 3:
            members = [q for q in qs if q["cat"] == c and q["pct"] is not None]
            top = max(members, key=lambda q: q["pct"])
            bot = min(members, key=lambda q: q["pct"])
            cats.append({"cat": c, "name": cat_name(c), "n": n, "avg": round(tot / n, 2),
                         "score": round(sum(q["score"] for q in members) / len(members)),
                         "top": [top["code"], top["name"], top["pct"]], "bot": [bot["code"], bot["name"], bot["pct"]]})
    cats.sort(key=lambda x: -x["avg"])
    bands = {"≤-60": 0, "-60~-30": 0, "-30~30": 0, "30~60": 0, "≥60": 0}
    for q in qs:
        s = q["score"]
        bands["≤-60" if s <= -60 else "-60~-30" if s < -30 else "-30~30" if s <= 30 else "30~60" if s < 60 else "≥60"] += 1
    return {"n": len(qs), "up": sum(p > 0 for p in pct), "down": sum(p < 0 for p in pct), "flat": sum(p == 0 for p in pct),
            "avg_pct": round(sum(pct) / len(pct), 2) if pct else None, "median_pct": pct_sorted[len(pct_sorted) // 2] if pct else None,
            "hot": sum((q["pace"] or 0) >= 3 for q in qs),
            "limit_up": sum(bool(q["upper_limit"]) and q["last"] >= q["upper_limit"] for q in qs),
            "limit_down": sum(bool(q["lower_limit"]) and q["last"] <= q["lower_limit"] for q in qs),
            "n_long": sum(q["side"] == "long" for q in qs), "n_short": sum(q["side"] == "short" for q in qs),
            "bands": bands, "mood": mood, "cats": cats}


def _limit_room(q, sg):
    """離漲停(做多)/跌停(做空)還有幾 %;沒有漲跌停價回傳 None。"""
    last, lim = _n(q.get("last")), _n(q.get("upper_limit" if sg > 0 else "lower_limit"))
    return sg * (lim - last) / last * 100 if last and lim else None


def pick_ok(q, sg):
    """推薦的硬條件(bias.score 的否決只在分數過 60 才算,推薦門檻是 40,所以這裡對所有推薦再檢查一次):
    - 大盤分項和方向相反就不推(10/30 大盤 -76 還推了 29 筆做多,照計畫勝率 22%)
    - 離漲跌停不到 ROOM_MIN_PCT 不推(停利空間不夠,10/30 這組空單照計畫平均 -1.5%)"""
    if (q.get("parts") or {}).get("mkt", 0) * sg < 0:
        return False
    room = _limit_room(q, sg)
    return room is None or room >= ROOM_MIN_PCT


def shortlist(qs, m):
    min_vol = EARLY_MIN_VOL if m < 30 else MIN_VOL
    ok = [q for q in qs if q["volume"] >= min_vol and not q["veto"] and q["dt"] != "No"]
    longs = sorted([q for q in ok if q["score"] >= PICK_MIN and pick_ok(q, 1)], key=lambda q: (-q["score"], -(q["pace"] or 0)))[:TOP_N]
    shorts = sorted([q for q in ok if q["score"] <= -PICK_MIN and pick_ok(q, -1)], key=lambda q: (q["score"], -(q["pace"] or 0)))[:TOP_N]
    return longs, shorts


def plan_ok(p):
    """細查後還站得住才推:用真實開盤區間重算的分數仍過門檻、方向沒變;
    停損是「固定 %」(附近沒有關鍵價位,多半是在追跌深/漲多的)又損益比 < 1 的不推(10/30 這組平均 -1.6%)。"""
    sg = 1 if p["side"] == "long" else -1
    if p["score"] * sg < PICK_MIN:
        return False
    pl = p.get("plan") or {}
    return not ((pl.get("stop_src") or "").startswith("固定") and pl.get("rr") is not None and pl["rr"] < 1)


def counter_trend(qs, m, longs, shorts):
    """大盤很弱(或很強)時多方(空方)推薦常常是空的;另外列「逆勢」的:分數沒到門檻,但漲跌方向和大盤相反、量能夠的前 COUNTER_N 檔。
    只給你看盤勢裡誰比較強/弱,不算推薦、不記進每日成績。"""
    min_vol = EARLY_MIN_VOL if m < 30 else MIN_VOL
    ok = [q for q in qs if q["volume"] >= min_vol and not q["veto"] and q["dt"] != "No" and (q["pace"] or 0) >= 1.3]
    taken = {q["code"] for q in longs + shorts}
    up = sorted([q for q in ok if q["code"] not in taken and (q["pct"] or 0) >= 0.5 and 0 < q["score"] < PICK_MIN],
                key=lambda q: (-q["score"], -(q["pct"] or 0)))[:COUNTER_N] if len(longs) < 3 else []
    dn = sorted([q for q in ok if q["code"] not in taken and (q["pct"] or 0) <= -0.5 and -PICK_MIN < q["score"] < 0],
                key=lambda q: (q["score"], q["pct"] or 0))[:COUNTER_N] if len(shorts) < 3 else []
    return up, dn


def deep_check(q, side, k5, flags, hlc, mood, state):
    """挑出來的那檔:真實 5 分K 校正開盤區間、重算分數,再跑六個觀測員(方向用多空分數的方向)。"""
    f = dict(q["_f"])
    bars = (k5.get(q["code"]) or (None,))[0]
    if bars:
        orb = bias.orb_range(bars)
        if orb:
            f["orb_hi"], f["orb_lo"] = orb
            state.setdefault("orb", {})[q["code"]] = list(orb)
        f["bar_hi"], f["bar_lo"] = bars[-1]["h"], bars[-1]["l"]
    r = bias.score(f)
    fl = dict(flags.get(q["code"]) or {})
    if q["dt"] == "No":
        fl["daytrade"] = False
    elif q["dt"] == "OnlyBuy":
        fl["no_sell_first"] = True
    sg = 1 if side == "long" else -1
    obs_q = dict(q, flags=fl, news=[], news_checked=False, cdp=_cdp(hlc.get(q["code"])), swing=None, conf=None,
                 orb=[f.get("orb_hi"), f.get("orb_lo")])
    t = team.team_check(obs_q, mood, [], q["vwap"], side=sg)
    plan = (t or {}).get("plan") or team.trade_plan(q, sg, {"均價線": q["vwap"]})
    db = team.debate(dict(q, pct=q["pct"]), sg, r["parts"], plan, (t or {}).get("res"), (t or {}).get("sup"), mood)
    return {"code": q["code"], "name": q["name"], "cat": cat_name(q["cat"]), "side": side, "score": r["score"], "parts": r["parts"],
            "reasons": bias.reasons(r["parts"], side), "strong": abs(r["score"]) >= bias.THRESHOLD,
            "last": q["last"], "pct": q["pct"], "pace": q["pace"], "vol": q["volume"], "vwap": round(q["vwap"], 2) if q["vwap"] else None,
            "orb": [f["orb_hi"], f["orb_lo"]] if f.get("orb_hi") else None, "in_watch": q["in_watch"], "bars": bool(bars),
            "plan": plan, "stop": plan["stop"], "target": plan["target"], "stop_pct": plan["stop_pct"], "target_pct": plan["target_pct"],
            "ck": t["ck"] if t else [], "v": t["v"] if t else "", "vc": t["vc"] if t else "na", "rr": t["rr"] if t else None,
            "flags": fl, "flow": q.get("flow"), "book": q.get("book"), "debate": db}


def record(run_time, m, picks, qs, st, k5):
    """每天的推薦紀錄:第一次被推薦的時間/價格/方向/分數/觀測員結果、名單進出、之後漲跌、收盤結果。"""
    today, hm = run_time.strftime("%Y-%m-%d"), run_time.strftime("%H:%M")
    path = os.path.join(DAY_DIR, f"{today}.json")
    day = _load(path, {})
    if day.get("date") != today:
        day = {"date": today, "series": [], "picks": {}, "rounds": 0}
    day["rounds"] += 1
    day["as_of"] = hm
    if not day["series"] or day["series"][-1][0] != hm:
        day["series"].append([hm, st["up"], st["down"], st["avg_pct"], st["hot"], st["n_long"], st["n_short"], (st["mood"] or {}).get("score")])
        day["series"] = day["series"][-MOOD_KEEP:]
    day["stats"] = {k: v for k, v in st.items() if k != "cats"}
    day["cats"] = st["cats"][:5] + st["cats"][-5:] if len(st["cats"]) > 10 else st["cats"]
    sides = {}
    for p in picks:
        sg = 1 if p["side"] == "long" else -1
        sides[p["code"]] = sg
        row = {"t": hm, "price": p["last"], "side": sg, "score": p["score"], "vc": p["vc"], "s": [c["s"] for c in p["ck"]],
               "out15": team.flow_pct(p), "imb": (p.get("book") or {}).get("imb"), "plan": p.get("plan"),
               "db": {k: (p.get("debate") or {}).get(k) for k in ("vc", "for_w", "against_w", "top_against")}}
        pk = day["picks"].get(p["code"])
        if (pk is None or pk["first"]["side"] != sg) and m >= NO_NEW_AFTER:
            continue
        if pk is None or pk["first"]["side"] != sg:
            if pk is not None:                       # 同一天方向翻轉:舊的那筆用翻轉時的價格結算(不然做錯的那筆會從成績裡消失),另存
                mv = team._move(pk["first"]["side"], pk["first"]["price"], p["last"])
                pk["close"] = {"price": p["last"], "move": mv, "net": round(mv - team.COST_PCT, 3) if mv is not None else None,
                               "exit": "flip", "t": hm}
                pk["active"] = False
                day["picks"][f"{p['code']}#{pk['first']['t']}"] = pk
            pk = day["picks"][p["code"]] = {"name": p["name"], "cat": p["cat"], "in_watch": p["in_watch"],
                                            "first": dict(row, reasons=p["reasons"], ck=p["ck"], v=p["v"], pace=p["pace"], pct=p["pct"],
                                                          stop=p["stop"], target=p["target"]),
                                            "hist": [], "n": 0, "best_vc": p["vc"], "max_score": abs(p["score"])}
        if not pk["hist"] or pk["hist"][-1]["vc"] != p["vc"]:
            pk["hist"].append({"t": hm, "vc": p["vc"], "score": p["score"]})
        pk["n"] += 1
        pk["max_score"] = max(pk["max_score"], abs(p["score"]))
        if team.VC_RANK[p["vc"]] < team.VC_RANK[pk["best_vc"]]:
            pk["best_vc"] = p["vc"]
        pk["last"] = dict(row, reasons=p["reasons"], ck=p["ck"], v=p["v"], pace=p["pace"], pct=p["pct"])
    by_code = {q["code"]: q for q in qs}
    for key, pk in day["picks"].items():
        code = key.split("#")[0]
        pk["active"] = "#" not in key and sides.get(code) == pk["first"]["side"]
        fs, q = pk["first"], by_code.get(code)
        if "#" in key or not q:
            continue
        pk["now"] = {"t": hm, "price": q["last"], "move": team._move(fs["side"], fs["price"], q["last"])}
        bars = (k5.get(code) or (None,))[0]
        if bars and not pk.get("plan_exit"):
            pe = plan_exit(fs, bars)
            if pe:
                pk["plan_exit"] = pe
        if bars:
            after = [b for b in bars if b["t"] >= fs["t"]]
            if after:
                hi, lo = max(b["h"] for b in after), min(b["l"] for b in after)
                best, worst = (hi, lo) if fs["side"] > 0 else (lo, hi)
                pk["mfe"], pk["mae"] = team._move(fs["side"], fs["price"], best), team._move(fs["side"], fs["price"], worst)
        if m >= 270:
            mv = pk["now"]["move"]
            pk["close"] = {"price": q["last"], "move": mv, "net": round(mv - team.COST_PCT, 3) if mv is not None else None}
    if m >= 270:
        day["closed"] = True
    team._write(path, day)
    idx = _load(INDEX_FILE, {"days": {}})
    ps = [p for k, p in day["picks"].items()]
    def summ(side):
        xs = [p["close"]["net"] for p in ps if p["first"]["side"] == side and (p.get("close") or {}).get("net") is not None]
        ys = [p["plan_exit"]["net"] for p in ps if p["first"]["side"] == side and (p.get("plan_exit") or {}).get("net") is not None]
        return {"n": sum(p["first"]["side"] == side for p in ps), "n_close": len(xs),
                "avg_net": round(sum(xs) / len(xs), 3) if xs else None, "win": round(sum(x > 0 for x in xs) / len(xs) * 100, 1) if xs else None,
                # 照卡片停損/停利/13:20 出場的成績(收盤後才完整)
                "n_plan": len(ys), "plan_avg": round(sum(ys) / len(ys), 3) if ys else None,
                "plan_win": round(sum(y > 0 for y in ys) / len(ys) * 100, 1) if ys else None}
    idx["cost_pct"] = team.COST_PCT
    idx.setdefault("days", {})[today] = {"closed": bool(day.get("closed")), "long": summ(1), "short": summ(-1),
                                         "up": st["up"], "down": st["down"], "avg_pct": st["avg_pct"], "mood": (st["mood"] or {}).get("score")}
    team._write(INDEX_FILE, idx)
    return day


PLAN_OUT = "13:20"          # 交易計畫沒碰到停損停利時的出場時間


def plan_exit(fs, bars):
    """照卡片上的交易計畫做的結果:第一次推薦當下的價格進場,之後的 5 分K 先碰停損還是停利(同一根都碰到算停損,保守),
    都沒碰到就 13:20 那根開盤出場。還沒結束回傳 None。翻向不影響(停損就是停損)。"""
    pl, sg, e = fs.get("plan") or {}, fs["side"], fs["price"]
    stop, tgt = _n(pl.get("stop")), _n(pl.get("target"))
    if not e or stop is None:
        return None
    for b in bars:
        if b["t"] < fs["t"]:
            continue
        if b["t"] >= PLAN_OUT:
            px, how, t = b["o"], "time", b["t"]
        elif (b["l"] <= stop) if sg > 0 else (b["h"] >= stop):
            px, how, t = stop, "stop", b["t"]
        elif tgt is not None and ((b["h"] >= tgt) if sg > 0 else (b["l"] <= tgt)):
            px, how, t = tgt, "target", b["t"]
        else:
            continue
        mv = team._move(sg, e, px)
        return {"t": t, "price": px, "exit": how, "move": mv, "net": round(mv - team.COST_PCT, 3) if mv is not None else None}
    return None


def _day_codes(today):
    """今天已經推薦過的代號(掉出名單後,最大順向/逆向幅度也要繼續更新)。"""
    day = _load(os.path.join(DAY_DIR, f"{today}.json"), {})
    return [k for k in (day.get("picks") or {}) if "#" not in k] if day.get("date") == today else []


def run(run_time, live, indices, watch, k5_loader, flags_loader, flow_loader=None):
    m = (run_time - run_time.replace(hour=9, minute=0, second=0, microsecond=0)).total_seconds() / 60
    if m < 1 or m > 276:
        return None
    today = run_time.strftime("%Y-%m-%d")
    qs = quotes(live, watch)
    if len(qs) < 50:
        return None
    state = _load(STATE_FILE, {})
    if state.get("date") != today:
        state = {"date": today}
    update_state(state, qs, m)
    tse = next((x for x in indices or [] if x.get("code") == "TWS:TSE01:INDEX"), {})
    mood = team.market_mood(indices, qs)          # 市場情緒員:全市場版(漲跌家數用全市場)
    by_cat = score_all(qs, state, m, tse)
    st = stats(qs, by_cat, mood)
    longs, shorts = shortlist(qs, m)
    c_up, c_dn = counter_trend(qs, m, longs, shorts)
    codes = [q["code"] for q in longs + shorts + c_up + c_dn]
    # 已經掉出名單的(只用來更新最大順向/逆向和照計畫出場)每 15 分鐘才補抓一次 1 分K:10/30 這些代號讓重輪的 1 分K 從 129 檔長到 200 檔
    gone = set(_day_codes(today)) - set(codes) if int(m) % 15 < 3 or m >= 268 else set()
    k5 = k5_loader(sorted(set(codes) | gone)) or {}
    flags = flags_loader(list((live.get("uni") or {}).keys())) or {}
    flow = (flow_loader(codes) if flow_loader else None) or {}
    for q in longs + shorts + c_up + c_dn:
        x = flow.get(q["code"]) or {}
        q["flow"], q["book"] = x.get("flow"), x.get("book")
    hlc = live.get("hlc") or {}
    picks = [deep_check(q, "long", k5, flags, hlc, mood, state) for q in longs] + \
            [deep_check(q, "short", k5, flags, hlc, mood, state) for q in shorts]
    picks = [p for p in picks if plan_ok(p)]
    counter = [dict(deep_check(q, "long", k5, flags, hlc, mood, state), counter=True) for q in c_up] + \
              [dict(deep_check(q, "short", k5, flags, hlc, mood, state), counter=True) for q in c_dn]
    team._write(STATE_FILE, state)
    out = {"as_of": run_time.strftime("%H:%M"), "date": today, "stats": st,
           "longs": [p for p in picks if p["side"] == "long"], "shorts": [p for p in picks if p["side"] == "short"], "counter": counter,
           "params": {"top_n": TOP_N, "pick_min": PICK_MIN, "threshold": bias.THRESHOLD, "min_vol": EARLY_MIN_VOL if m < 30 else MIN_VOL},
           "note": "全市場用多空分數機械式篩選;目前回測沒有站得住的多空分數規則,只是觀察名單,不構成買賣建議。"}
    team._write(OUT_FILE, out)
    record(run_time, m, picks, qs, st, k5)
    return out
