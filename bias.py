#!/usr/bin/env python3
"""
爆量雷達 - 盤中多空分數(盤中 intraday.py 每一輪算、收盤後 daytrade_lab.py 用同一套公式回測)

每一項給 -1 ~ +1,乘上權重加總成 -100 ~ +100:
  mkt   大盤濾網   加權指數漲跌 >= 0.2% 且在開盤價之上 = +1;只有漲跌幅條件成立 = ±0.5
  vwap  均價線     現價在當日均價線之上且均價線往上 = +1;只有位置成立 = ±0.5
  orb   開盤區間   站上 09:00-09:15 最高價 = +1,跌破最低價 = -1
  vol   量能       量能倍數 >= 2 依漲跌方向 ±1,>= 1.3 給 ±0.5(量大但沒方向不算)
  pos   當日位置   現價在今天高低區間的上 1/3 = +1,下 1/3 = -1
  grp   族群同步   同族群其他自選股平均漲 >= 0.5% = +1,跌 >= 0.5% = -1
結論:分數 >= THRESHOLD 做多、<= -THRESHOLD 放空,其他觀望;
大盤方向跟個股方向相反、或離漲跌停不到 LIMIT_GAP_PCT,一律改成觀望。
權重是起始值,要看 daytrade_lab.py 的回測結果(data/daytrade.json 的 score_* 規則)才知道有沒有優勢。
這是機械式分數,不是預測,也不是買賣建議。
"""

WEIGHTS = {"mkt": 20, "vwap": 25, "orb": 20, "vol": 15, "pos": 10, "grp": 10}
LABELS = {"mkt": "大盤", "vwap": "均價線", "orb": "開盤區間", "vol": "量能", "pos": "當日位置", "grp": "族群"}
THRESHOLD = 60
STRONG = 80
MKT_PCT = 0.2
GRP_PCT = 0.5
LIMIT_GAP_PCT = 1.0
ORB_END = 15          # 開盤區間:09:00 起前 15 分鐘
PB_TOL_PCT = 0.2      # 拉回均價線:最低價回到均價線多少 % 內
FADE_PCT = 2.0        # 偏離反向:離均價線至少多少 %
RULE_SIDE = {"score_long": "long", "score_short": "short", "score_long_80": "long", "score_short_80": "short",
             "pb_long": "long", "pb_short": "short", "fade_long": "long", "fade_short": "short"}


def _ok(*xs):
    return all(isinstance(x, (int, float)) and x == x for x in xs)


def _sgn(x):
    return (x > 0) - (x < 0)


def vwap_series(bars):
    """5 分K 逐根累積均價線(典型價 × 量);第一根常常沒有量,沒有量的根用前一個值。
    回測 daytrade_lab.py 和盤中 intraday.py(永豐 1 分K 組的 5 分K)共用,兩邊算法一致。"""
    out, pv, vv = [], 0.0, 0.0
    for b in bars:
        vv += b["v"]
        pv += (b["h"] + b["l"] + b["c"]) / 3 * b["v"]
        out.append(pv / vv if vv else (b["h"] + b["l"] + b["c"]) / 3)
    return out


def orb_range(bars):
    """開盤區間 = 09:15 以前各根 5 分K 的最高/最低;不到 2 根回傳 None。"""
    orb = [b for b in bars if b["t"] < f"09:{ORB_END:02d}"]
    if len(orb) < 2:
        return None
    return max(b["h"] for b in orb), min(b["l"] for b in orb)


def components(f):
    """f: last, prev_close, vwap, vwap_prev, orb_hi, orb_lo, pace, day_hi, day_lo, mkt_pct, mkt_last, mkt_open, grp_pct。
    缺資料的項目給 0(不加分也不扣分)。"""
    c = dict.fromkeys(WEIGHTS, 0.0)
    last, pc = f.get("last"), f.get("prev_close")
    if not _ok(last, pc) or pc <= 0:
        return c
    pct = (last / pc - 1) * 100
    mp, ml, mo = f.get("mkt_pct"), f.get("mkt_last"), f.get("mkt_open")
    if _ok(mp) and abs(mp) >= MKT_PCT:
        s = _sgn(mp)
        c["mkt"] = float(s) if _ok(ml, mo) and _sgn(ml - mo) == s else 0.5 * s
    vw, vp = f.get("vwap"), f.get("vwap_prev")
    if _ok(vw) and vw > 0 and last != vw:
        s = _sgn(last - vw)
        c["vwap"] = float(s) if _ok(vp) and _sgn(vw - vp) == s else 0.5 * s
    hi, lo = f.get("orb_hi"), f.get("orb_lo")
    if _ok(hi, lo):
        c["orb"] = 1.0 if last > hi else (-1.0 if last < lo else 0.0)
    pace = f.get("pace")
    if _ok(pace) and abs(pct) >= 0.3:
        c["vol"] = _sgn(pct) * (1.0 if pace >= 2 else (0.5 if pace >= 1.3 else 0.0))
    dh, dl = f.get("day_hi"), f.get("day_lo")
    if _ok(dh, dl) and dh > dl:
        p = (last - dl) / (dh - dl)
        c["pos"] = 1.0 if p >= 2 / 3 else (-1.0 if p <= 1 / 3 else 0.0)
    gp = f.get("grp_pct")
    if _ok(gp) and abs(gp) >= GRP_PCT:
        c["grp"] = float(_sgn(gp))
    return c


def score(f, threshold=THRESHOLD):
    """回傳 {"score", "side"(long/short/None), "parts", "veto"}。"""
    c = components(f)
    sc = round(sum(WEIGHTS[k] * v for k, v in c.items()))
    side = "long" if sc >= threshold else ("short" if sc <= -threshold else None)
    veto = None
    if side:
        s = 1 if side == "long" else -1
        if c["mkt"] * s < 0:
            veto = "大盤反向"
        last, up, dn = f.get("last"), f.get("up_lim"), f.get("lo_lim")
        if _ok(last, up) and up > 0 and (up / last - 1) * 100 < LIMIT_GAP_PCT:
            veto = "接近漲停"
        if _ok(last, dn) and dn > 0 and (last / dn - 1) * 100 < LIMIT_GAP_PCT:
            veto = "接近跌停"
        if veto:
            side = None
    return {"score": sc, "side": side, "parts": {k: round(WEIGHTS[k] * v) for k, v in c.items()}, "veto": veto}


def reasons(parts, side):
    """跟結論同方向、有加分的項目,給網頁顯示理由。"""
    s = 1 if side == "long" else -1
    return [LABELS[k] for k, v in sorted(parts.items(), key=lambda kv: -abs(kv[1])) if v * s > 0]


def rule_hits(f, armed):
    """這一根(回測)或這一輪(盤中)成立的分數類規則。回測 daytrade_lab.py 和盤中 intraday.py 共用這一份條件,
    避免兩邊寫法不一致。
    f 另外可以有 bar_hi / bar_lo(這一根的高低;盤中有永豐 1 分K 時用真實 5 分K,沒有才用前後兩次快照價格代替)。
    armed={"long":bool,"short":bool}:「之前」某根分數曾 >= THRESHOLD / <= -THRESHOLD,拉回規則要先成立這個。
    回傳 (成立的規則 set, 更新後的 armed)。"""
    hits = set()
    r, r80 = score(f), score(f, STRONG)
    sc = r["score"]
    if r["side"]:
        hits.add("score_" + r["side"])
    if r80["side"]:
        hits.add("score_" + r80["side"] + "_80")
    vw, last = f.get("vwap"), f.get("last")
    if _ok(vw, last) and vw > 0 and last > 0:
        if sc <= -STRONG and (vw / last - 1) * 100 >= FADE_PCT:
            hits.add("fade_long")
        if sc >= STRONG and (1 - vw / last) * 100 >= FADE_PCT:
            hits.add("fade_short")
        lo, hi = f.get("bar_lo", last), f.get("bar_hi", last)
        if armed.get("long") and sc > 0 and lo <= vw * (1 + PB_TOL_PCT / 100) and last > vw:
            hits.add("pb_long")
        if armed.get("short") and sc < 0 and hi >= vw * (1 - PB_TOL_PCT / 100) and last < vw:
            hits.add("pb_short")
    return hits, {"long": bool(armed.get("long")) or sc >= THRESHOLD, "short": bool(armed.get("short")) or sc <= -THRESHOLD}
