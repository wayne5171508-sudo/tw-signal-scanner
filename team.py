#!/usr/bin/env python3
"""
爆量雷達 - 交易小組(六個觀測員)每日紀錄

六個角色跟網頁 index.html 的 marketMood / teamCheck 同一套判斷,搬到這裡讓盤中迴圈每一輪都算、都記下來:
  1 機會掃描員  量能倍數(>= 2 倍算有資金在動),剛觸發的多空訊號互相矛盾時給警示
  2 新聞分析員  近兩天有重大訊息就警示(事件可能放大波動)
  3 市場情緒員  加權、開盤後走勢、自選股漲跌家數、櫃買、台指期價差組成 -100~+100,方向和個股一致才通過
  4 圖表分析員  用支撐壓力算損益比與分區位置,損益比 >= MIN_RR 且不是追價位置才通過
  5 風險管理員  處置、不能當沖、暫停先賣後買是硬限制;貼近漲跌停、注意股、振幅過大、停損太遠/太近給警示
  6 決策員      有硬限制就拒絕;方向不明觀望;已驗證規則 + 3 項通過 + 最多 1 項警示才「可執行」;警示 >= 2 不建議;其他僅觀察

和網頁的差別:網頁的「今日風控」(每天最多虧多少、做幾筆)存在各自的手機/電腦裡,這裡拿不到,所以不算;
圖表分析員的最低損益比固定用網頁的預設值 MIN_RR。

每天一個檔 data/team/YYYY-MM-DD.json(永久保存),data/team/index.json 是每天的摘要:
  picks[代號] = 第一次被挑出來的時間/價格/方向/六個角色的結果、之後每次結論改變的時間、現在價格與之後的漲跌、
                第一次推薦之後的最大順向/逆向幅度(用永豐 5 分K)、收盤時的結果。
  「之後漲跌」都是「照第一次推薦的方向、用當時價格」算的事後結果,方便看哪個觀測員比較準,不代表真的有成交。
這是機械式整理,不是預測,也不構成買賣建議。
"""

import json
import os

TEAM_DIR = os.path.join("data", "team")
INDEX_FILE = os.path.join(TEAM_DIR, "index.json")
ZONE_BAND_PCT = 0.15      # 和 index.html 一樣:均價線上下 0.15% 算平衡區
MIN_RR = 1.5              # 和 index.html RISK_DEF.minRR 一樣
COST_PCT = 0.23           # 當沖來回成本(和 daytrade_lab 一致,手續費 2.8 折),只用在「收盤淨報酬」
MAX_PICKS = 12
NO_NEW_AFTER = 265        # 13:25 起是收盤集合競價(價格成交不到),之後不新增推薦,只更新既有的            # 每一輪最多記幾檔(網頁看板顯示 8 檔,多記一點)
ROLES = ("機會", "消息", "情緒", "圖表", "風險")
VC_RANK = {"ok": 0, "warn": 1, "na": 2, "bad": 3}


def _n(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v and v not in (float("inf"), float("-inf")) else None


def _clamp1(x):
    return max(-1.0, min(1.0, x))


def tick_of(code, p):
    """台股跳動檔位(和 index.html tickOf 一樣)。"""
    if not code or ":" in str(code):
        return 0
    if str(code).startswith("00"):
        return 0.01 if p < 50 else 0.05
    return 0.01 if p < 10 else 0.05 if p < 50 else 0.1 if p < 100 else 0.5 if p < 500 else 1 if p < 1000 else 5


def to_tick(p, code):
    v = _n(p)
    if v is None:
        return p
    t = tick_of(code, v)
    return round(round(v / t) * t, 2) if t else v


def _sw_ok(sw, n):
    if not sw or sw.get(f"h{n}") is None or sw.get(f"l{n}") is None:
        return False
    return (sw.get("d5") or 0) >= 3 if n == 5 else (sw.get(f"d{n}") or 0) >= n


def key_levels(q):
    """壓力/支撐(index.html keyLevels 的「自動」模式):CDP 可靠就用 CDP 近高近低,不然用近 5 日高低,再不然今日高低。"""
    cdp, sw = q.get("cdp") or {}, q.get("swing") or {}
    has_cdp = all(cdp.get(k) is not None for k in ("nh", "nl", "cdp"))
    mode = "cdp" if has_cdp and cdp.get("reliability") != "low" else ("5" if _sw_ok(sw, 5) else ("cdp" if has_cdp else "today"))
    if mode == "cdp":
        res, sup = cdp["nh"], cdp["nl"]
    elif mode == "today":
        res, sup = q.get("high") if q.get("high") is not None else q.get("upper_limit"), q.get("low") if q.get("low") is not None else q.get("lower_limit")
    else:
        res, sup = sw.get("h5"), sw.get("l5")
    res, sup = to_tick(res, q["code"]), to_tick(sup, q["code"])
    if _n(res) is not None and _n(q.get("upper_limit")) is not None:
        res = min(res, q["upper_limit"])
    if _n(sup) is not None and _n(q.get("lower_limit")) is not None:
        sup = max(sup, q["lower_limit"])
    mid = to_tick(cdp["cdp"] if has_cdp else q.get("prev_close"), q["code"])
    return _n(res), _n(sup), _n(mid)


def effective_levels(q):
    """已突破壓力或跌破支撐時,換成現價上下最近的一條線(index.html effectiveLevels)。"""
    res, sup, mid = key_levels(q)
    last, cf = _n(q.get("last")), q.get("conf") or {}
    if last and cf and ((res is not None and res < last) or (sup is not None and sup > last)):
        nr, ns = (cf.get("res") or [None])[0], (cf.get("sup") or [None])[0]
        if nr:
            res = _n(to_tick(nr.get("price"), q["code"]))
        if ns:
            sup = _n(to_tick(ns.get("price"), q["code"]))
    return res, sup, mid


def zone_info(q, vwap):
    """現價在做多區/平衡區/做空區/已突破/已跌破(index.html zoneInfo)。"""
    res, sup, mid = effective_levels(q)
    vw = vwap if _n(vwap) is not None else mid
    if res is None or sup is None or vw is None or not res > sup:
        return None
    c = vw if sup < vw < res else (sup + res) / 2
    lo, hi = max(sup, c * (1 - ZONE_BAND_PCT / 100)), min(res, c * (1 + ZONE_BAND_PCT / 100))
    last = _n(q.get("last"))
    if last is None:
        return None
    if last > res:
        return "over", "已突破壓力"
    if last < sup:
        return "under", "已跌破支撐"
    if last > hi:
        return "short", "做空區"
    if last < lo:
        return "long", "做多區"
    return "mid", "平衡區"


def market_mood(indices, watch):
    """市場情緒員:-100~+100,另算爆量熱度(量能 >= 3 倍的家數比例)。"""
    ix = {x["code"]: x for x in indices or []}
    tse, otc, txf = ix.get("TWS:TSE01:INDEX") or {}, ix.get("TWS:OTC01:INDEX") or {}, ix.get("TWF:TXF:FUTURES") or {}
    qs = [q for q in watch or [] if not str(q["code"]).startswith("00") and _n(q.get("pct")) is not None]
    if _n(tse.get("pct")) is None or not qs:
        return None
    up, dn = sum(q["pct"] > 0 for q in qs), sum(q["pct"] < 0 for q in qs)
    t_last, t_open = _n(tse.get("last")), _n(tse.get("open"))
    parts = [
        ("加權指數", 25, _clamp1(tse["pct"] / 1.5)),
        ("開盤後走勢", 20, _clamp1((t_last / t_open - 1) * 100 / 0.8) if t_last and t_open else 0),
        ("自選股漲跌家數", 25, (up - dn) / len(qs)),
        ("櫃買指數", 15, _clamp1(otc["pct"] / 1.5) if _n(otc.get("pct")) is not None else 0),
        ("台指期價差", 15, _clamp1((txf["last"] - t_last) / t_last * 100 / 0.5) if _n(txf.get("last")) is not None and t_last else 0),
    ]
    score = round(sum(w * v for _, w, v in parts))
    hot = sum((_n(q.get("pace")) or 0) >= 3 for q in qs) / len(qs)
    side = 1 if score >= 30 else -1 if score <= -30 else 0
    return {"score": score, "label": "偏多" if side > 0 else "偏空" if side < 0 else "中性", "side": side,
            "parts": {k: round(w * v) for k, w, v in parts}, "hot": round(hot, 3), "overheated": hot >= 0.15,
            "up": up, "down": dn}


FLOW_AGAINST = 40          # 做多時最近 15 分鐘外盤比 <= 40%(做空時 >= 60%)= 主動買賣方向和推薦相反,機會掃描員給警示


def flow_pct(q):
    """機會掃描員看的外盤比:最近 15 分鐘量夠(>= 50 張)就用最近 15 分鐘,不然用全天。每日紀錄也存這個值,分組才對得起來。"""
    f = q.get("flow") or {}
    return f.get("out15") if f.get("out15") is not None and (f.get("n15") or 0) >= 50 else f.get("out")


def _flow_check(c, q, side):
    """機會掃描員加看買賣力道(永豐逐筆內外盤、五檔掛單)。資料沒有就照舊。
    外盤比和方向相反才改成警示;大單、五檔只寫進說明(還沒累積成績驗證,不改結論)。"""
    f, b = q.get("flow") or {}, q.get("book") or {}
    o = flow_pct(q)
    notes = []
    if o is not None:
        notes.append(f"外盤 {o:.0f}%")
        if side and ((side > 0 and o <= FLOW_AGAINST) or (side < 0 and o >= 100 - FLOW_AGAINST)):
            c = {"r": c["r"], "s": "warn", "t": c["t"]}
            notes[-1] += f"(主動{'賣' if side > 0 else '買'}盤比較強,和方向相反)"
    if f.get("big") is not None and (f.get("big_vol") or 0) > 0:
        notes.append(f"大單淨{'買' if f['big'] > 0 else '賣'} {abs(f['big'])}%")
    if b.get("imb") is not None:
        notes.append(f"五檔委{'買' if b['imb'] > 0 else '賣'}多 {abs(b['imb'])}%")
    return dict(c, t=c["t"] + ("・" + "、".join(notes) if notes else "")) if notes else c


def team_check(q, mood, bias_items, vwap=None, side=None):
    """一檔的六個觀測員結果:{"side", "S", "ck": [{"r","s","t"}], "v", "vc", "rule"}。s/vc:ok 通過、warn 警示、bad 硬限制、na 中性。
    side 不給就照網頁的方式決定(剛觸發的多空判斷,沒有就看漲跌幅);全市場雷達用多空分數的方向直接指定。"""
    p = _n(q.get("last"))
    if p is None:
        return None
    fresh = [x for x in bias_items or [] if x.get("code") == q["code"] and x.get("fresh")]
    sides = {x["side"] for x in fresh}
    held = [x for x in fresh if x.get("holds")]
    conflict = len(sides) > 1
    pick = fresh if not conflict else (held if len({x["side"] for x in held}) == 1 else [])
    pct = _n(q.get("pct"))
    if side is None:
        side = (1 if pick[0]["side"] == "long" else -1) if pick else (1 if pct is not None and pct >= 0.5 else -1 if pct is not None and pct <= -0.5 else 0)
    ck = []
    # 1 機會掃描員
    pace = _n(q.get("pace"))
    c = {"r": "機會", "s": "ok" if pace is not None and pace >= 2 else "na",
         "t": "量能資料累積中" if pace is None else (f"量能 {pace:.1f} 倍,有資金在動" if pace >= 2 else f"量能 {pace:.1f} 倍,不算突出")}
    if conflict and not pick:
        c = {"r": "機會", "s": "warn", "t": c["t"] + ",而且剛觸發的多空訊號互相矛盾"}
    c = _flow_check(c, q, side)
    ck.append(c)
    # 2 新聞分析員
    nw = q.get("news") or []
    if q.get("news_checked") is False:
        ck.append({"r": "消息", "s": "na", "t": "全市場掃描沒有逐檔查重大訊息(下單前自己看一下公開資訊觀測站)"})
    else:
        ck.append({"r": "消息", "s": "warn" if nw else "ok",
                   "t": f"{nw[0].get('date', '')[5:]} {nw[0].get('cat', '')}:{(nw[0].get('title') or '')[:28]}(事件可能放大波動)" if nw else "近兩天沒有重大訊息"})
    # 3 市場情緒員
    if not mood or not side:
        ck.append({"r": "情緒", "s": "na", "t": f"大盤{mood['label']}({mood['score']:+d})" if mood else "沒有大盤資料"})
    else:
        s = "ok" if mood["side"] == side else "warn" if mood["side"] == -side else "na"
        ck.append({"r": "情緒", "s": s, "t": f"大盤{mood['label']}({mood['score']:+d})" + (",方向和個股相反" if s == "warn" else ",方向一致" if s == "ok" else "")
                   + (",爆量家數偏多、市場過熱" if mood["overheated"] else "")})
    # 4 圖表分析員
    res, sup, _mid = effective_levels(q)
    zi = zone_info(q, vwap)
    stop = (sup if side > 0 else res) if side else None
    if not side or res is None or sup is None:
        ck.append({"r": "圖表", "s": "na", "t": "方向不明或沒有支撐壓力"})
        rr = None
    else:
        risk, reward = (p - sup, res - p) if side > 0 else (res - p, p - sup)
        rr = reward / risk if risk > 0 else None
        chase = bool(zi) and ((side > 0 and zi[0] in ("short", "over")) or (side < 0 and zi[0] in ("long", "under")))
        s = "bad" if rr is None else ("ok" if rr >= MIN_RR and not chase else ("warn" if rr < 1 or chase else "na"))
        ck.append({"r": "圖表", "s": s, "t": "已越過停損位置" if rr is None else f"損益比 {rr:.1f}:1" + (f"(低於 {MIN_RR})" if rr < MIN_RR else "")
                   + (f",現價在{zi[1]}" if zi else "") + ("(追價位置)" if chase else "")})
    # 5 風險管理員
    f = q.get("flags") or {}
    adr, hi, lo, pc = _n((q.get("swing") or {}).get("adr5")), _n(q.get("high")), _n(q.get("low")), _n(q.get("prev_close"))
    used = (hi - lo) / pc * 100 / adr if adr and hi is not None and lo is not None and pc else None
    stop_pct = abs(p - stop) / p * 100 if _n(stop) is not None else None
    if f.get("disposal") or f.get("daytrade") is False or f.get("change_trading"):
        ck.append({"r": "風險", "s": "bad", "t": f"處置中({(f.get('disposal') or {}).get('measure') or '分盤撮合'})" if f.get("disposal")
                   else "不能現股當沖" if f.get("daytrade") is False else "分盤交易"})
    elif side < 0 and f.get("no_sell_first"):
        ck.append({"r": "風險", "s": "bad", "t": "暫停先賣後買,不能當沖做空"})
    else:
        w = []
        ul, ll = _n(q.get("upper_limit")), _n(q.get("lower_limit"))
        if side > 0 and ul and p >= ul * 0.99:
            w.append("貼近漲停")
        if side < 0 and ll and p <= ll * 1.01:
            w.append("貼近跌停")
        if f.get("notice") or f.get("near_disposal"):
            w.append("可能被處置" if f.get("near_disposal") else "注意股")
        if used is not None and used >= 1.5:
            w.append(f"振幅已達平常 {round(used * 100)}%")
        if stop_pct is not None and stop_pct > 3:
            w.append(f"停損距離 {stop_pct:.1f}% 偏遠")
        if stop_pct is not None and stop_pct < 0.5:
            w.append(f"停損只差 {stop_pct:.2f}%")
        ck.append({"r": "風險", "s": "warn" if w else "ok", "t": "、".join(w) if w else "沒有交易限制,振幅與停損距離正常"})
    # 6 決策員
    n_bad, n_warn, n_ok = (sum(c["s"] == k for c in ck) for k in ("bad", "warn", "ok"))
    valid = [x for x in pick if x.get("holds") and (1 if x["side"] == "long" else -1) == side]
    if n_bad:
        v, vc = "拒絕:" + "、".join(c["t"] for c in ck if c["s"] == "bad"), "bad"
    elif not side:
        v, vc = "觀望:方向不明", "na"
    elif valid and n_ok >= 3 and n_warn <= 1:
        v, vc = f"符合已驗證規則({valid[0].get('rule_name')}),可依部位試算執行", "ok"
    elif n_warn >= 2:
        v, vc = "不建議:警示項目太多,就算要做也要縮小部位", "warn"
    elif valid:
        v, vc = "已驗證規則觸發,但條件不夠完整:縮小部位", "warn"
    else:
        v, vc = "僅觀察:沒有回測站得住的規則觸發,只適合紙上交易", "na"
    return {"side": side, "S": "做多" if side > 0 else "做空" if side < 0 else "觀望", "ck": ck, "v": v, "vc": vc,
            "rule": (pick[0].get("rule_name") if pick else None), "rr": round(rr, 2) if rr is not None else None,
            "stop": _n(stop), "res": res, "sup": sup}


def candidates(result, vwaps=None):
    """機會掃描員挑標的(和 index.html renderTeam 一樣):量能 >= 2 倍、開盤爆量、剛觸發多空判斷、今天有重大訊息。
    回傳 [(q, check)],依結論(可執行 > 警示 > 觀察 > 拒絕)、量能排序。"""
    mood = market_mood(result.get("indices"), result.get("watchlist_quotes"))
    if not mood:
        return None, []
    items = (result.get("bias") or {}).get("items") or []
    hot = {x["code"] for x in ((result.get("opening_alerts") or {}).get("items") or []) if x.get("hot_now")}
    today = (result.get("data_as_of") or "")[:10]
    out = []
    for q in result.get("watchlist_quotes") or []:
        if str(q["code"]).startswith("00") or _n(q.get("last")) is None:
            continue
        if not ((_n(q.get("pace")) or 0) >= 2 or q["code"] in hot or any(x["code"] == q["code"] and x.get("fresh") for x in items)
                or any(n.get("date") == today for n in q.get("news") or [])):
            continue
        t = team_check(q, mood, items, (vwaps or {}).get(q["code"]))
        if t:
            out.append((q, t))
    out.sort(key=lambda x: (VC_RANK[x[1]["vc"]], -(_n(x[0].get("pace")) or 0)))
    return mood, out[:MAX_PICKS]


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def _move(side, base, px):
    return round(side * (px / base - 1) * 100, 3) if side and _n(base) and _n(px) else None


def record(run_time, result, k5=None, vwaps=None):
    """每一輪:把這一輪六個觀測員的結果疊進今天的紀錄檔,回傳今天的紀錄(給 intraday_latest.json 用)。
    k5 = intraday.load_k5() 的結果,用來算第一次推薦之後的最大順向/逆向幅度。"""
    m = (run_time - run_time.replace(hour=9, minute=0, second=0, microsecond=0)).total_seconds() / 60
    if m < 1 or m > 280:
        return None
    today, hm = run_time.strftime("%Y-%m-%d"), run_time.strftime("%H:%M")
    path = os.path.join(TEAM_DIR, f"{today}.json")
    day = _load(path, {})
    if day.get("date") != today:
        day = {"date": today, "mood": [], "picks": {}, "rounds": 0}
    mood, cands = candidates(result, vwaps)
    if mood:
        if not day["mood"] or day["mood"][-1][0] != hm:
            day["mood"].append([hm, mood["score"]])
        day["mood_now"] = mood
    day["rounds"] += 1
    day["as_of"] = hm
    now_pick = set()
    for q, t in cands:
        code = q["code"]
        now_pick.add(code)
        row = {"t": hm, "price": q["last"], "side": t["side"], "vc": t["vc"], "s": [c["s"] for c in t["ck"]],
               "out15": flow_pct(q), "imb": (q.get("book") or {}).get("imb")}
        pk = day["picks"].get(code)
        if pk is None and m >= NO_NEW_AFTER:          # 收盤集合競價之後不新增
            continue
        if pk is not None and not pk["first"]["side"] and t["side"]:
            pk = None                                 # 第一次挑出時是觀望(方向 0,不能算績效):等第一次有方向時才當起點
        if pk is None:
            pk = day["picks"][code] = {"name": q.get("name"), "first": dict(row, ck=t["ck"], v=t["v"], rule=t["rule"], rr=t["rr"],
                                                                         stop=t["stop"], pace=q.get("pace"), pct=q.get("pct")),
                                       "hist": [row], "n": 0, "best_vc": t["vc"]}
        elif (pk["hist"][-1]["vc"], pk["hist"][-1]["side"]) != (t["vc"], t["side"]):
            pk["hist"].append(row)
        pk["n"] += 1
        if VC_RANK[t["vc"]] < VC_RANK[pk["best_vc"]]:
            pk["best_vc"] = t["vc"]
        pk["last"] = dict(row, ck=t["ck"], v=t["v"], rule=t["rule"], rr=t["rr"], pace=q.get("pace"), pct=q.get("pct"))
    by_code = {q["code"]: q for q in result.get("watchlist_quotes") or []}
    for code, pk in day["picks"].items():
        pk["active"] = code in now_pick
        q, fs = by_code.get(code) or {}, pk["first"]
        px = _n(q.get("last"))
        if px is not None:
            pk["now"] = {"t": hm, "price": px, "move": _move(fs["side"], fs["price"], px)}
        bars = ((k5 or {}).get(code) or (None,))[0]
        if bars and fs["side"]:
            after = [b for b in bars if b["t"] >= fs["t"]]
            if after:
                hi, lo = max(b["h"] for b in after), min(b["l"] for b in after)
                best, worst = (hi, lo) if fs["side"] > 0 else (lo, hi)
                pk["mfe"], pk["mae"] = _move(fs["side"], fs["price"], best), _move(fs["side"], fs["price"], worst)
        if m >= 270 and px is not None:      # 13:30 收盤後:照第一次推薦方向、用當時價格,收盤出場的結果
            mv = _move(fs["side"], fs["price"], px)
            pk["close"] = {"price": px, "move": mv, "net": round(mv - COST_PCT, 3) if mv is not None else None}
    if m >= 270:
        day["closed"] = True
    _write(path, day)
    _update_index(day)
    return day


def _update_index(day):
    idx = _load(INDEX_FILE, {"days": {}})
    idx["cost_pct"] = COST_PCT            # 網頁顯示「扣多少成本」用,改手續費折扣時跟著變
    picks = list(day["picks"].values())
    moves = [p["close"]["net"] for p in picks if (p.get("close") or {}).get("net") is not None]
    idx.setdefault("days", {})[day["date"]] = {
        "picks": len(picks), "ok": sum(p["best_vc"] == "ok" for p in picks), "warn": sum(p["best_vc"] == "warn" for p in picks),
        "closed": bool(day.get("closed")), "mood_close": (day.get("mood") or [[None, None]])[-1][1],
        "n_close": len(moves), "avg_net": round(sum(moves) / len(moves), 3) if moves else None,
        "win": round(sum(x > 0 for x in moves) / len(moves) * 100, 1) if moves else None}
    _write(INDEX_FILE, idx)
