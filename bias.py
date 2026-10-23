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


def _ok(*xs):
    return all(isinstance(x, (int, float)) and x == x for x in xs)


def _sgn(x):
    return (x > 0) - (x < 0)


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
