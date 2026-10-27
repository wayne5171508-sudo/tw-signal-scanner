#!/usr/bin/env python3
"""
爆量雷達 - 隔夜風向(開盤前看昨晚美股與台指期夜盤,判斷今天開盤是順風、逆風還是中性)

兩件事,都寫進 data/overnight.json,網頁「今日重點 > 隔夜風向」顯示:

1) 開盤前快照(update):台指期夜盤(主指標)、費半、那斯達克、台積電 ADR 換算溢價、輝達、美光。
   夜盤 15:00 交易到隔天 05:00,08:45 日盤開始後報價就換成日盤,所以一定要在 05:00~08:45 之間拍。
   排程在 06:10/07:10/08:15(overnight.yml)各試一次,盤中迴圈 08:40 開始前再補一次;今天拍過就跳過。
   錯過 08:45 才跑:夜盤欄位留空,只記美股(美股要到台北晚上才開盤,白天看到的都是昨晚收盤)。

2) 訊號實驗室驗證(record_outcome / stats):盤中迴圈開盤後記下自選股(不含 ETF)平均開盤跳空,
   收盤後記平均全天漲跌與開盤到收盤的漲跌;再對照焦點成績單(focus_log.json)當天的結果,
   依順風/中性/逆風分組統計。只觀察,不拿來下單;門檻是暫定的,樣本夠了再看要不要調。

資料來源:鉅亨網(cnyes)公開報價 API,和 intraday.py 同一個。這裡的數字都不是預測,也不是買賣建議。
"""

import json
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

TAIPEI = ZoneInfo("Asia/Taipei")
DATA_DIR = "data"
OVERNIGHT_FILE = os.path.join(DATA_DIR, "overnight.json")
FOCUS_FILE = os.path.join(DATA_DIR, "focus_log.json")
QUOTE_URL = "https://ws.api.cnyes.com/ws/api/v1/quote/quotes/{codes}"
HEADERS = {"User-Agent": "Mozilla/5.0 (tw-signal-scanner overnight)"}
TIMEOUT = 10
KEEP_DAYS = 120

TXF = "TWF:TXF:FUTURES"
FX = "FX:USDTWD:FOREX"
TW2330 = "TWS:2330:STOCK"
TSM = "USS:TSM:STOCK"
ADR_RATIO = 5                     # 1 股台積電 ADR = 5 股台積電
# (cnyes代碼, 顯示名稱, 跟自選股哪些族群比較有關)
ITEMS = [
    (TXF, "台指期夜盤", ""),
    ("GI:SOX:INDEX", "費城半導體", "半導體全族群"),
    ("GI:IXIC:INDEX", "那斯達克", ""),
    (TSM, "台積電ADR", "權值、晶圓"),
    ("USS:NVDA:STOCK", "輝達", "AI伺服器、PCB載板"),
    ("USS:MU:STOCK", "美光", "記憶體"),
]
PREOPEN_END = (8, 45)             # 台指期日盤開始,之後報價不再是夜盤
WIND_TXF_PCT = 0.3                # 夜盤漲跌超過 ±0.3% 算順風/逆風(暫定)
WIND_SOX_PCT = 1.0                # 沒有夜盤時改看費半,±1%(暫定)
DIVERGE_SOX_PCT = 1.0             # 費半和夜盤方向相反、且費半漲跌超過 1%,標示「分歧」
US_STALE_HOURS = 30               # 美股報價超過 30 小時沒更新(美國休市),標示「休市」
MIN_DAYS = 20                     # 累積到 20 個交易日才下結論


def taipei_now():
    return datetime.now(TAIPEI)


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def fetch(codes):
    resp = requests.get(QUOTE_URL.format(codes=",".join(codes)), headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    return {r.get("0"): r for r in (resp.json().get("data") or []) if isinstance(r, dict)}


def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


def _is_night(ts, now):
    """報價時間落在夜盤時段(15:00~05:00),而且是最近幾天內的。"""
    if not ts:
        return False
    t = datetime.fromtimestamp(ts, TAIPEI)
    return (t.hour >= 15 or t.hour < 5 or (t.hour == 5 and t.minute <= 5)) and now - t < timedelta(hours=80)


def classify(txf_pct, sox_pct):
    """回傳 (風向, 依據)。風向:順風/中性/逆風;沒有資料回傳 (None, None)。"""
    if txf_pct is not None:
        th, base, p = WIND_TXF_PCT, "夜盤", txf_pct
    elif sox_pct is not None:
        th, base, p = WIND_SOX_PCT, "費半", sox_pct
    else:
        return None, None
    return ("順風" if p >= th else ("逆風" if p <= -th else "中性")), base


def build_snapshot(rows, now):
    items = []
    for code, name, rel in ITEMS:
        r = rows.get(code) or {}
        ts = r.get("200007")
        it = {"code": code, "name": name, "rel": rel, "last": _num(r.get("6")), "pct": _num(r.get("56")),
              "ts": ts}
        if code == TXF:
            # 08:45 之後報價已換成日盤(15:00 之後又是新的夜盤),都不是「昨晚」的夜盤
            if (now.hour, now.minute) >= PREOPEN_END or not _is_night(ts, now):
                it.update(last=None, pct=None, note="開盤前沒記錄到夜盤")
            else:
                it["ref"] = _num(r.get("21"))
        elif ts and now - datetime.fromtimestamp(ts, TAIPEI) > timedelta(hours=US_STALE_HOURS):
            it["note"] = "美股休市,沿用前一次收盤"
        items.append(it)

    adr = None
    tsm, fx, tw = rows.get(TSM) or {}, rows.get(FX) or {}, rows.get(TW2330) or {}
    tsm_px, fx_px = _num(tsm.get("6")), _num(fx.get("6"))
    # 開盤前 2330 的「現價」就是昨收;萬一開盤後才跑,改用參考價(昨收)
    tw_ts = tw.get("200007")
    opened = tw_ts and datetime.fromtimestamp(tw_ts, TAIPEI).date() == now.date()
    tw_px = _num(tw.get("21") if opened else tw.get("6"))
    if tsm_px and fx_px and tw_px:
        eq = tsm_px * fx_px / ADR_RATIO
        adr = {"adr": tsm_px, "fx": fx_px, "tw_close": tw_px, "eq": round(eq, 1),
               "premium": round((eq / tw_px - 1) * 100, 2)}

    by = {it["code"]: it for it in items}
    wind, basis = classify(by[TXF]["pct"], by["GI:SOX:INDEX"]["pct"])
    txf_pct, sox_pct = by[TXF]["pct"], by["GI:SOX:INDEX"]["pct"]
    diverge = (txf_pct is not None and sox_pct is not None and abs(sox_pct) >= DIVERGE_SOX_PCT
               and txf_pct * sox_pct < 0)
    return {"date": now.strftime("%Y-%m-%d"), "taken_at": now.isoformat(timespec="minutes"),
            "preopen": (now.hour, now.minute) < PREOPEN_END,
            "wind": wind, "basis": basis, "diverge": diverge, "items": items, "adr": adr}


def update(now=None):
    """拍今天的開盤前快照。今天已經有含夜盤的快照就不重拍;只回傳是否有寫檔。"""
    now = now or taipei_now()
    if now.weekday() >= 5 or (now.hour, now.minute) >= (13, 30):
        print("[隔夜風向] 週末或收盤後不拍")
        return False
    log = load_json(OVERNIGHT_FILE, {}) or {}
    days = log.setdefault("days", {})
    today = now.strftime("%Y-%m-%d")
    old = days.get(today)
    if old and (old.get("preopen") or (old.get("outcome") or {}).get("gap") is not None):
        print("[隔夜風向] 今天已經記錄過")
        return False
    rows = fetch([c for c, _n, _r in ITEMS] + [FX, TW2330])
    snap = build_snapshot(rows, now)
    if old and old.get("outcome"):
        snap["outcome"] = old["outcome"]
    days[today] = snap
    for d in sorted(days)[:-KEEP_DAYS]:
        days.pop(d, None)
    log["latest"] = today
    log["stats"] = stats(log, load_json(FOCUS_FILE, {}))
    write_json(OVERNIGHT_FILE, log)
    txf = snap["items"][0]
    print(f"[隔夜風向] {today} {snap['wind'] or '無資料'}(依據{snap['basis'] or '-'}) 夜盤 {txf['pct']}%")
    return True


def record_outcome(run_time, watchlist_out, group_of):
    """盤中迴圈每一輪呼叫:開盤後第一輪記平均跳空,13:31 之後第一輪記全天與開盤到收盤。
    只算有族群分類的個股(ETF 不算,槓桿 ETF 會放大)。"""
    log = load_json(OVERNIGHT_FILE, {}) or {}
    day = (log.get("days") or {}).get(run_time.strftime("%Y-%m-%d"))
    if not day:
        return
    out = day.setdefault("outcome", {})
    m = (run_time.hour - 9) * 60 + run_time.minute
    gaps, days_, oc = [], [], []
    for q in watchlist_out:
        if q.get("code") not in group_of:
            continue
        o, pc, last = _num(q.get("open")), _num(q.get("prev_close")), _num(q.get("last"))
        if o and pc:
            gaps.append((o / pc - 1) * 100)
        if last and pc:
            days_.append((last / pc - 1) * 100)
        if last and o:
            oc.append((last / o - 1) * 100)
    avg = lambda xs: round(sum(xs) / len(xs), 2) if len(xs) >= 20 else None
    changed = False
    if m >= 2 and out.get("gap") is None and avg(gaps) is not None:
        out["gap"], out["n"] = avg(gaps), len(gaps)
        changed = True
    if m >= 271 and out.get("day") is None and avg(days_) is not None:
        out["day"], out["oc"] = avg(days_), avg(oc)
        changed = True
    if changed:
        log["stats"] = stats(log, load_json(FOCUS_FILE, {}))
        write_json(OVERNIGHT_FILE, log)


def _corr(xs, ys):
    n = len(xs)
    if n < 5:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sx = sum((x - mx) ** 2 for x in xs) ** 0.5
    sy = sum((y - my) ** 2 for y in ys) ** 0.5
    if not sx or not sy:
        return None
    return round(sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy), 2)


def stats(log, focus_log):
    """依風向分組:自選股平均跳空、跳空方向和風向一致的比例、開盤到收盤;焦點名單收盤成績。"""
    fdays = (focus_log or {}).get("days") or {}
    groups = {w: {"days": 0, "gap": [], "oc": [], "hit": 0, "f_in": [], "f_out": []} for w in ("順風", "中性", "逆風")}
    xs, ys = [], []
    for d, v in sorted((log.get("days") or {}).items()):
        w, o = v.get("wind"), v.get("outcome") or {}
        if w not in groups or o.get("gap") is None:
            continue
        g = groups[w]
        g["days"] += 1
        g["gap"].append(o["gap"])
        if o.get("oc") is not None:
            g["oc"].append(o["oc"])
        if (w == "順風" and o["gap"] > 0) or (w == "逆風" and o["gap"] < 0) or (w == "中性" and abs(o["gap"]) < 0.5):
            g["hit"] += 1
        txf = next((it.get("pct") for it in v.get("items") or [] if it.get("code") == TXF), None)
        if txf is not None:
            xs.append(txf)
            ys.append(o["gap"])
        fd = fdays.get(d) or {}
        if fd.get("evaluated"):
            for pk in (fd.get("picks") or {}).values():
                if pk.get("ret") is None:
                    continue
                if pk.get("side") == "in":
                    g["f_in"].append(pk["ret"])
                elif pk.get("side") == "out":
                    g["f_out"].append(-pk["ret"])
    avg = lambda xs_: round(sum(xs_) / len(xs_), 2) if xs_ else None
    out = {"days": sum(g["days"] for g in groups.values()), "min_days": MIN_DAYS, "corr_txf_gap": _corr(xs, ys),
           "th_txf": WIND_TXF_PCT, "th_sox": WIND_SOX_PCT, "by": {}}
    for w, g in groups.items():
        out["by"][w] = {"days": g["days"], "gap": avg(g["gap"]), "oc": avg(g["oc"]), "hit": g["hit"],
                        "f_in": {"n": len(g["f_in"]), "win": sum(1 for r in g["f_in"] if r > 0), "avg": avg(g["f_in"])},
                        "f_out": {"n": len(g["f_out"]), "win": sum(1 for r in g["f_out"] if r > 0), "avg": avg(g["f_out"])}}
    return out


if __name__ == "__main__":
    try:
        update()
    except Exception as e:  # noqa: BLE001  抓不到就等下一次排程,不要讓 workflow 亮紅燈
        print(f"[隔夜風向] 失敗: {e}")
        sys.exit(0)
