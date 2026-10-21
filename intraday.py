#!/usr/bin/env python3
"""
爆量雷達 - 盤中資料擷取(GitHub Actions 開盤期間每 3~5 分鐘跑一次)

資料來源:鉅亨網(cnyes)公開 JSON API。直接用 Python 打 API,不透過會讀網頁摘要的工具。
產出(都放在 data/,由 index.html 在 GitHub Pages 顯示):
- intraday_latest.json   最新報價、CDP/波段高低、觸及回測、量能倍數、開盤爆量、焦點成績單
- intraday_history.json  今天每個時間點的價格與累積成交量(畫走勢小圖、算量能曲線用,隔天重置)
- daily_official.json    每檔近 260 根正式日K(逐檔追蹤,收盤後 13:32 起再整批重抓一次)
- daily_bars.json        用報價滾動累積的近似日K(正式日K抓不到時的備援)
- volume_curve.json      每檔每天「累積量占全天量」曲線,累積 5 天後取代內建估計曲線
- focus_log.json         焦點成績單:量能達標的標的與收盤時的表現

主要計算:
- CDP 壓力/支撐:CDP=(高+低+2*收)/4,近高 NH=2*CDP-低,近低 NL=2*CDP-高,用前一個交易日
  的正式日K。前一日振幅太小(漲跌停鎖死;ETF 另訂較低門檻)視為不可靠,改用 5 日高低。
- 波段高低(5/10/20日)、均線、量能密集區、疊加區(不同類的線落在同一區)。
- 觸及回測:逐日只用當時看得到的資料,統計碰到線後收盤明確離開的比例;
  另外算「隨機價位」對照組、碰線後隔天與第3天、放量與大盤強弱的分組。
- 量能倍數 = 累積量 / (近20日日成交量中位數 x 此時間點一般已成交比例)。
- 非交易日判斷:報價自己的最新成交時間不是今天,整輪略過、不覆蓋上一個交易日的資料。

用法:python intraday.py
"""

import json
import os
import random
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

TAIPEI = ZoneInfo("Asia/Taipei")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; personal-trading-dashboard/1.0)"}
TIMEOUT = 20
DATA_DIR = "data"
OUT_FILE = os.path.join(DATA_DIR, "intraday_latest.json")
HISTORY_FILE = os.path.join(DATA_DIR, "intraday_history.json")
CURVE_FILE = os.path.join(DATA_DIR, "volume_curve.json")     # 每檔每天的「累積量占全天量比例」曲線(越累積越準)
UNIVERSE_FILE = os.path.join(DATA_DIR, "universe.json")       # 全市場代號與前一交易日成交量(每天更新一次)
MARKET_FILE = os.path.join(DATA_DIR, "market_scan.json")      # 全市場爆量榜(試跑)
FIELDS_FILE = os.path.join(DATA_DIR, "debug_fields.json")     # 報價原始欄位樣本(查有沒有內外盤)
SIGNAL_FILE = os.path.join(DATA_DIR, "signal_log.json")     # 訊號實驗室:各策略在固定時間點的候選名單與收盤結果
FOCUS_FILE = os.path.join(DATA_DIR, "focus_log.json")       # 焦點成績單:每天哪些標的達標、之後表現如何
DAILY_BARS_FILE = os.path.join(DATA_DIR, "daily_bars.json")
DAILY_OFFICIAL_FILE = os.path.join(DATA_DIR, "daily_official.json")
CNYES_HISTORY_URL = "https://ws.api.cnyes.com/ws/api/v1/charting/history"
OFFICIAL_KEEP_DAYS = 260              # 每檔最多留幾根日K(約一年),回測樣本才夠多
ETF_LOW_RELIABILITY_RANGE_PCT = 0.5   # ETF 單日振幅本來就小,CDP 可靠度門檻另外放寬
LOW_RELIABILITY_RANGE_PCT = 1.5   # 前一日(高-低)/收 小於這個百分比,視為CDP參考性低
OFFICIAL_MISMATCH_PCT = 0.3       # 正式日K收盤和今天報價的昨收差超過這個百分比(且超過0.05元),視為對不上,不採用
OFFICIAL_MAX_AGE_DAYS = 7          # 正式日K最後一根距今超過幾天,視為過期
HISTORY_FETCH_DAYS = 100           # 備用:長區間抓不到時退回的日曆天數(約65個交易日)
HISTORY_FETCH_DAYS_LONG = 400      # 優先向日K API要的日曆天數(約260個交易日)
REFRESH_BUDGET_SECONDS = 90        # 每一輪抓日K最多花幾秒,超過就留給下一輪繼續
HISTORY_TIMEOUT = 8
HISTORY_RETRY_GAP_FAIL = 600       # 今天還有沒抓到的股票時,隔幾秒再重試
LIMIT_MOVE_PCT = 9.3               # 前一日漲跌幅超過這個百分比,視為漲停/跌停
MAX_POINTS_PER_DAY = 80
MAX_HISTORY_DAYS = 10
VOLUME_LOOKBACK_DAYS = 5
CDP_TREND_POINTS = 6  # 趨勢最多顯示幾個點(含今天)


CNYES_QUOTE_URL = "https://ws.api.cnyes.com/ws/api/v1/quote/quotes/{codes}"

# 6大盤指數(cnyes代碼, 顯示名稱)
INDICES = [
    ("TWS:TSE01:INDEX", "加權指數"),
    ("TWS:OTC01:INDEX", "櫃買指數"),
    ("GI:DJI:INDEX", "道瓊"),
    ("GI:IXIC:INDEX", "NASDAQ"),
    ("GI:INX:INDEX", "S&P500"),
    ("GI:SOX:INDEX", "費城半導體"),
]

# 自選股(代碼: 名稱)—— 來源:阿文國泰證券App自選股6個分頁彙整(皇太后、行動自選1~5),2026-10-15
WATCHLIST_NAMES = {
    "2455": "全新", "3374": "精材", "2408": "南亞科", "2344": "華邦電",
    "6147": "頎邦", "8021": "尖點", "8039": "台虹", "8358": "金居",
    "3264": "欣銓", "4979": "華星光", "3105": "穩懋", "6173": "信昌電",
    "3016": "嘉晶", "1727": "中華化", "8042": "金山電", "3027": "盛達",
    "4303": "信立", "8111": "立碁", "1815": "富喬", "2630": "亞航",
    "2883": "凱基金", "2881": "富邦金", "2313": "華通", "4919": "新唐",
    "6217": "中探針", "1303": "南亞", "3051": "力特", "6937": "天虹",
    "2337": "旺宏", "6257": "矽格", "3090": "日電貿", "2303": "聯電",
    "2379": "瑞昱", "2609": "陽明", "3231": "緯創", "3707": "漢磊",
    "5425": "台半", "2481": "強茂", "3588": "通嘉", "5347": "世界",
    "6182": "合晶", "2351": "順德", "2449": "京元電子", "6239": "力成",
    "8150": "南茂", "4958": "臻鼎-KY", "3037": "欣興", "6488": "環球晶",
    "3490": "單井", "2409": "友達", "2882": "國泰金", "2327": "國巨",
    "3624": "光頡", "3605": "宏致", "6456": "GIS-KY", "2492": "華新科",
    "4991": "環宇-KY", "6278": "台表科", "3042": "晶技", "2606": "裕民",
    "6140": "訊達", "1316": "上曜", "1802": "台玻", "3706": "神達",
    "2365": "昆盈", "7781": "昕力資*", "2887": "台新新光金", "2049": "上銀",
    "4576": "大銀微系統", "2359": "所羅門", "0050": "元大台灣50", "009816": "凱基台灣TOP50",
    "00981A": "主動統一台股增長", "00685L": "群益臺灣加權正2", "00631L": "元大台灣50正2",
    "8033": "雷虎", "6505": "台塑化", "2368": "金像電", "6209": "今國光",
}

# 資金流向用的族群分類(只影響「資金流向」區塊的分組,不影響其他功能;ETF不計入,避免槓桿ETF的大量成交蓋過個股)
WATCH_GROUPS = {
    "記憶體": ["2408", "2344", "2337"],
    "晶圓與材料": ["2303", "5347", "6488", "6182", "3016", "3707", "3105"],
    "IC設計": ["2379", "4919", "3588"],
    "封測": ["3264", "6257", "2449", "6239", "8150", "6147", "3374", "2351"],
    "設備探針": ["6217", "8021", "6937", "3490"],
    "PCB載板": ["3037", "4958", "2313", "8039", "8358", "1815", "2368", "1802"],
    "被動元件": ["2327", "3624", "2492", "6173", "3042", "5425", "2481", "3090", "8042"],
    "光通訊": ["4979", "4991", "8111"],
    "AI伺服器": ["3231", "3706", "2365", "3605", "6278"],
    "機器人": ["2049", "4576", "2359"],
    "面板光電": ["2409", "6456", "3051"],
    "金融": ["2883", "2881", "2882", "2887"],
    "航運航空": ["2609", "2606", "2630"],
    "塑化傳產": ["1303", "6505", "1727", "4303", "1316"],
    "國防無人機": ["8033", "6209"],
    "其他": ["3027", "6140", "2455", "7781"],
}
GROUP_OF = {c: g for g, cs in WATCH_GROUPS.items() for c in cs}


def _clean(obj):
    """NaN/Infinity不是合法JSON,網頁JSON.parse會直接失敗,全部轉成null。"""
    if isinstance(obj, float):
        return obj if obj == obj and obj not in (float("inf"), float("-inf")) else None
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    return obj


def write_json_atomic(path, obj, **kw):
    """先寫到暫存檔再用 os.replace 覆蓋,中途中斷/磁碟錯誤時原檔案不會被寫壞。"""
    text = json.dumps(_clean(obj), ensure_ascii=False, allow_nan=False, **kw)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def taipei_now():
    return datetime.now(TAIPEI)


def fetch_quotes(codes):
    """codes: list of cnyes代碼字串。不帶 column 參數——帶了反而只會拿到精簡欄位,
    不帶才會拿到完整欄位(含高低開收、漲跌停價)。一次最多抓50筆,分批呼叫。
    單一批次失敗時重試最多3次(間隔遞增1秒/2秒),應付網路瞬斷;連續3次都失敗才真的
    往外拋例外,讓main()中止、這次排程失敗(寧可明顯失敗,也不要默默寫出半套資料)。"""
    out = {}
    batch_size = 50
    for i in range(0, len(codes), batch_size):
        batch = codes[i:i + batch_size]
        url = CNYES_QUOTE_URL.format(codes=",".join(batch))
        last_err = None
        body = None
        for attempt in range(3):
            try:
                resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
                resp.raise_for_status()
                body = resp.json()
                break
            except (requests.RequestException, ValueError) as e:
                last_err = e
                if attempt < 2:
                    time.sleep(attempt + 1)
        if body is None:
            raise last_err
        for row in body.get("data", []):
            out[row.get("0")] = row
    return out


def row_to_quote(row, fallback_code, fallback_name):
    if row is None:
        return {
            "code": fallback_code, "name": fallback_name,
            "last": None, "change": None, "pct": None,
            "open": None, "high": None, "low": None, "prev_close": None,
            "volume": None, "ts": None,
        }
    last = row.get("6")
    change = row.get("11")
    prev_close = row.get("21")
    if prev_close is None and last is not None and change is not None:
        prev_close = round(last - change, 4)
    return {
        "code": fallback_code,
        "name": row.get("200009") or fallback_name,
        "last": last,
        "change": change,
        "pct": row.get("56"),
        "open": row.get("19"),
        "high": row.get("12"),
        "low": row.get("13"),
        "prev_close": prev_close,
        "volume": row.get("200013"),
        "upper_limit": row.get("75"),
        "lower_limit": row.get("76"),
        "limit_flag": row.get("200025"),
        "ts": row.get("200007"),
    }


# ---- 開盤爆量偵測(09:00~09:30)----
# 作法:今天累積成交量 / (近20日日成交量中位數 × 此刻「正常情況下已經成交的比例」)。
# 「正常已成交比例」是台股常見的U型量能估計曲線(開盤集中、尾盤再放大),不是每檔實測值,
# 所以倍數當「相對強弱」看,不是精確數字。方向只看漲跌幅:量大+上漲=疑似進場,量大+下跌=疑似出場
# (成交量本身分不出買方賣方,這是推測)。
OPENING_START_MIN = 0       # 09:00
OPENING_END_MIN = 31        # 09:31 之前的那一輪還算
OPENING_CURVE = [(0, 0.012), (1, 0.03), (5, 0.06), (10, 0.09), (15, 0.115), (20, 0.135), (30, 0.165), (60, 0.30), (120, 0.52), (270, 1.0)]
OPEN_MIN_VOL = 200          # 累積量低於200張不判斷(太容易被單筆撐起來)
OPEN_PACE_ALERT = 3.0       # 倍數>=3 列入(實測自選股約前10%)
OPEN_PACE_STRONG = 6.0      # 倍數>=6 標強烈
OPEN_FLAT_PCT = 0.5         # 漲跌幅絕對值<0.5%算「方向不明」
OPEN_AVG_DAYS = 20


def curve_frac(m):
    if m <= OPENING_CURVE[0][0]:
        return OPENING_CURVE[0][1]
    for (m0, f0), (m1, f1) in zip(OPENING_CURVE, OPENING_CURVE[1:]):
        if m <= m1:
            return f0 + (f1 - f0) * (m - m0) / (m1 - m0)
    return 1.0


CURVE_GRID = [1, 5, 10, 15, 20, 30, 45, 60, 90, 120, 180, 240, 270]   # 開盤後第幾分鐘
CURVE_KEEP_DAYS = 15
CURVE_MIN_DAYS = 5          # 單檔至少累積幾天才開始採用自己的曲線
CURVE_SHRINK_N = 5          # 採用比例 = n/(n+5);天數越多越信自己的曲線
FOCUS_LOG_PACE = 2.5        # 量能倍數>=2.5 才記進成績單
FOCUS_KEEP_DAYS = 60


def _interp(points, m):
    """points=[(minute, value)] 由小到大;線性內插,超出範圍取端點。"""
    if m <= points[0][0]:
        return points[0][1]
    for (m0, v0), (m1, v1) in zip(points, points[1:]):
        if m <= m1:
            return v0 + (v1 - v0) * (m - m0) / (m1 - m0) if m1 > m0 else v1
    return points[-1][1]


def _median(vals):
    vs = sorted(vals)
    n = len(vs)
    return vs[n // 2] if n % 2 else (vs[n // 2 - 1] + vs[n // 2]) / 2


def load_curves():
    """回傳 (每檔曲線字典, 全體合併曲線)。合併曲線用所有股票所有天的中位數,資料不夠(<100筆)就是 None,改用內建估計曲線。"""
    data = load_json_safe(CURVE_FILE, {}) or {}
    per = {}
    for _d, day in (data.get("days") or {}).items():
        for code, fr in day.items():
            if isinstance(fr, list) and len(fr) == len(CURVE_GRID):
                per.setdefault(code, []).append(fr)
    pooled = None
    allv = [fr for lst in per.values() for fr in lst]
    if len(allv) >= 100:
        pooled = [_median([fr[i] for fr in allv]) for i in range(len(CURVE_GRID))]
        for i in range(1, len(pooled)):
            pooled[i] = max(pooled[i], pooled[i - 1])
    return per, pooled


def frac_for(code, m, per, pooled):
    m = min(m, 270)
    if pooled:
        gen = _interp([(0, 0.0)] + list(zip(CURVE_GRID, pooled)), m)
    else:
        gen = curve_frac(m)
    lst = (per or {}).get(code) or []
    if len(lst) >= CURVE_MIN_DAYS:
        med = [_median([fr[i] for fr in lst]) for i in range(len(CURVE_GRID))]
        for i in range(1, len(med)):
            med[i] = max(med[i], med[i - 1])
        mine = _interp([(0, 0.0)] + list(zip(CURVE_GRID, med)), m)
        n = len(lst)
        return max(0.003, (n * mine + CURVE_SHRINK_N * gen) / (n + CURVE_SHRINK_N))
    return max(0.003, gen)


def _minute_of(iso, day_start):
    try:
        return (datetime.fromisoformat(iso) - day_start).total_seconds() / 60
    except (ValueError, TypeError):
        return None


def _day_start(run_time):
    return run_time.replace(hour=9, minute=0, second=0, microsecond=0)


def record_focus(run_time, watchlist_out, regime=None):
    """盤中每一輪:量能倍數>=FOCUS_LOG_PACE 的標的,當天第一次出現就記下時間、價格、方向。
    regime=當下大盤判定(weak/normal/strong),每輪覆蓋,收盤那輪的值就是當天的最終判定,成績單依此分組。"""
    today = run_time.strftime("%Y-%m-%d")
    m = (run_time - _day_start(run_time)).total_seconds() / 60
    if not (2 < m <= 271):
        return
    log = load_json_safe(FOCUS_FILE, {}) or {}
    days = log.setdefault("days", {})
    day = days.setdefault(today, {"picks": {}, "evaluated": False})
    if day.get("evaluated"):
        return
    changed = False
    if regime and day.get("regime") != regime:
        day["regime"] = regime
        changed = True
    for q in watchlist_out:
        pace, last = q.get("pace"), q.get("last")
        if not (_num_ok(pace) and _num_ok(last)) or pace < FOCUS_LOG_PACE or q["code"] in day["picks"]:
            continue
        pct = q.get("pct")
        side = "flat" if (not _num_ok(pct) or abs(pct) < 0.5) else ("in" if pct > 0 else "out")
        day["picks"][q["code"]] = {"name": q.get("name"), "t": run_time.isoformat(), "price": last, "pct": pct,
                                   "pace": pace, "side": side}
        changed = True
    if changed:
        for d in sorted(days)[:-FOCUS_KEEP_DAYS]:
            days.pop(d, None)
        write_json_atomic(FOCUS_FILE, log, separators=(",", ":"))


def process_end_of_day(run_time):
    """13:31 之後第一輪(歷史檔已含收盤前最後一筆):
    1) 把今天每檔的累積量曲線存進 volume_curve.json;2) 評估今天焦點名單收盤時的結果。"""
    today = run_time.strftime("%Y-%m-%d")
    ds = _day_start(run_time)
    m_now = (run_time - ds).total_seconds() / 60
    if m_now < 271:
        return
    hist = load_json_safe(HISTORY_FILE, {}) or {}
    if hist.get("date") != today:
        return
    pts = []
    for p in hist.get("points") or []:
        mm = _minute_of(p.get("t"), ds)
        if mm is not None and p.get("wlv"):
            pts.append((mm, p))
    if len(pts) < 20 or pts[-1][0] < 268:
        return
    # 1) 量能曲線
    cur = load_json_safe(CURVE_FILE, {}) or {}
    days = cur.setdefault("days", {})
    if today not in days:
        day = {}
        codes = set()
        for _m, p in pts:
            codes.update(p["wlv"].keys())
        for code in codes:
            series = [(mm, p["wlv"][code]) for mm, p in pts if _num_ok(p["wlv"].get(code))]
            if len(series) < 20:
                continue
            final = series[-1][1]
            if final < OPEN_MIN_VOL:
                continue
            xs = [(0.0, 0.0)] + [(mm, v) for mm, v in series]
            day[code] = [round(min(1.0, _interp(xs, g) / final), 4) for g in CURVE_GRID]
        days[today] = day
        for d in sorted(days)[:-CURVE_KEEP_DAYS]:
            days.pop(d, None)
        write_json_atomic(CURVE_FILE, cur, separators=(",", ":"))
    # 2) 焦點成績單
    log = load_json_safe(FOCUS_FILE, {}) or {}
    d = (log.get("days") or {}).get(today)
    if d and not d.get("evaluated"):
        for code, pk in d["picks"].items():
            t0 = _minute_of(pk["t"], ds)
            prices = [(mm, p["wl"][code]) for mm, p in pts if t0 is not None and mm >= t0 and _num_ok((p.get("wl") or {}).get(code))]
            if len(prices) < 2:
                continue
            first = pk["price"]
            close = prices[-1][1]
            up = max((v - first) / first * 100 for _m, v in prices)
            dn = min((v - first) / first * 100 for _m, v in prices)
            pk["ret"] = round((close - first) / first * 100, 2)
            pk["up"] = round(up, 2)
            pk["dn"] = round(dn, 2)
        d["evaluated"] = True
        write_json_atomic(FOCUS_FILE, log, separators=(",", ":"))


def focus_stats():
    """彙總焦點成績單:依方向、依量能倍數分組。進場=收盤高於記錄價算對,出場=收盤低於記錄價算對。"""
    log = load_json_safe(FOCUS_FILE, {}) or {}
    ev_days = [d for d, v in (log.get("days") or {}).items() if v.get("evaluated")]
    rows = []
    for d in ev_days:
        rg = log["days"][d].get("regime")
        for code, pk in log["days"][d]["picks"].items():
            if "ret" in pk:
                rows.append(dict(pk, regime=rg))

    def agg(rs, side):
        if not rs:
            return {"n": 0}
        n = len(rs)
        if side == "in":
            win = sum(1 for r in rs if r["ret"] > 0)
            fav = [r["up"] for r in rs]
            adv = [r["dn"] for r in rs]
            rets = [r["ret"] for r in rs]
        else:
            win = sum(1 for r in rs if r["ret"] < 0)
            fav = [-r["dn"] for r in rs]
            adv = [-r["up"] for r in rs]
            rets = [-r["ret"] for r in rs]
        return {"n": n, "win": win, "avg_ret": round(sum(rets) / n, 2), "avg_fav": round(sum(fav) / n, 2),
                "avg_adv": round(sum(adv) / n, 2)}

    out = {"days": len(ev_days), "n": len(rows)}
    for side in ("in", "out"):
        out[side] = agg([r for r in rows if r["side"] == side], side)
    out["flat"] = {"n": sum(1 for r in rows if r["side"] == "flat")}
    buckets = {}
    for nm, lo, hi in (("2.5-4", 2.5, 4), ("4-6", 4, 6), ("6+", 6, 1e9)):
        sub = [r for r in rows if r["side"] in ("in", "out") and lo <= r["pace"] < hi]
        win = sum(1 for r in sub if (r["ret"] > 0 if r["side"] == "in" else r["ret"] < 0))
        buckets[nm] = {"n": len(sub), "win": win}
    out["buckets"] = buckets
    # 依當天大盤強弱分組(弱勢日追高容易被拉回,混在一起平均會失真)
    by = {}
    for rg in ("strong", "normal", "weak"):
        sub = [r for r in rows if r.get("regime") == rg]
        if sub:
            by[rg] = {"days": len({r["t"][:10] for r in sub}), "in": agg([r for r in sub if r["side"] == "in"], "in"),
                      "out": agg([r for r in sub if r["side"] == "out"], "out")}
    out["by_regime"] = by
    return out


# ---- 訊號實驗室 ----
# 每天在 09:30/10:30/12:00/13:00 各拍一張全部自選股的快照,收盤後用「該時點買進(或放空)持有到收盤」
# 的報酬評估每個策略,並和「同一時點全部自選股的平均」比較。edge=策略平均報酬-全體平均。
# 不預設任何策略有效,累積夠多樣本之後才看得出哪些有優勢。
SIGNAL_SNAPS = (30, 90, 180, 240)      # 開盤後第幾分鐘拍快照
SIGNAL_KEEP_DAYS = 90
SIGNAL_STRATS = {                       # 名稱: (方向, 條件(pct, pace, dd_hi, up_lo))
    "追強": ("long", lambda pct, pace, dd, up: pct >= 2 and pace >= 3 and dd >= -0.5),
    "拉回買": ("long", lambda pct, pace, dd, up: pct >= 2 and pace >= 3 and dd <= -2),
    "弱勢爆量": ("short", lambda pct, pace, dd, up: pct <= -2 and pace >= 3),
    "低檔反彈": ("long", lambda pct, pace, dd, up: pct <= -2 and pace >= 3 and up >= 2),
}


def record_signals(run_time, watchlist_out, regime=None):
    """每個快照時點第一輪:存每檔[現價, 漲跌%, 量能倍數, 今日高, 今日低]。13:31 之後第一輪存收盤價。"""
    m = (run_time - _day_start(run_time)).total_seconds() / 60
    if m < SIGNAL_SNAPS[0]:
        return
    today = run_time.strftime("%Y-%m-%d")
    log = load_json_safe(SIGNAL_FILE, {}) or {}
    days = log.setdefault("days", {})
    day = days.setdefault(today, {"snaps": {}, "close": None, "regime": None})
    changed = False
    if regime and day.get("regime") != regime and not day.get("close"):
        day["regime"] = regime
        changed = True
    for sn in SIGNAL_SNAPS:
        if m >= sn and str(sn) not in day["snaps"] and m < sn + 12:
            rows = {}
            for q in watchlist_out:
                last, hi, lo = q.get("last"), q.get("high"), q.get("low")
                if _num_ok(last, hi, lo) and q["code"] in GROUP_OF:
                    rows[q["code"]] = [last, q.get("pct"), q.get("pace"), hi, lo]
            day["snaps"][str(sn)] = rows
            changed = True
    if m >= 271 and not day.get("close"):
        day["close"] = {q["code"]: q["last"] for q in watchlist_out if _num_ok(q.get("last"))}
        changed = True
    if changed:
        for d in sorted(days)[:-SIGNAL_KEEP_DAYS]:
            days.pop(d, None)
        write_json_atomic(SIGNAL_FILE, log, separators=(",", ":"))


def signal_stats():
    """各策略的累積成績:筆數、勝率、平均報酬、全體對照、edge;另列大盤偏弱日/其他日的拆分。"""
    log = load_json_safe(SIGNAL_FILE, {}) or {}
    acc = {k: {"n": 0, "win": 0, "ret": 0.0, "base": 0.0, "by": {}} for k in SIGNAL_STRATS}
    ndays = 0
    for d, day in (log.get("days") or {}).items():
        close = day.get("close")
        if not close:
            continue
        ndays += 1
        rg = day.get("regime") or "unknown"
        for sn, rows in (day.get("snaps") or {}).items():
            rets = {c: (close[c] / r[0] - 1) * 100 for c, r in rows.items() if c in close and r[0]}
            if not rets:
                continue
            base = sum(rets.values()) / len(rets)
            for name, (side, cond) in SIGNAL_STRATS.items():
                sg = 1 if side == "long" else -1
                for c, r in rows.items():
                    if c not in rets or not (_num_ok(r[1], r[2]) and r[3] and r[4]):
                        continue
                    dd = (r[0] / r[3] - 1) * 100
                    up = (r[0] / r[4] - 1) * 100
                    if cond(r[1], r[2], dd, up):
                        a = acc[name]
                        x = sg * rets[c]
                        a["n"] += 1
                        a["win"] += 1 if x > 0 else 0
                        a["ret"] += x
                        a["base"] += sg * base
                        b = a["by"].setdefault(rg, {"n": 0, "ret": 0.0, "base": 0.0})
                        b["n"] += 1
                        b["ret"] += x
                        b["base"] += sg * base
    out = {"days": ndays, "cost_pct": 0.4, "strats": {}}
    for name, a in acc.items():
        n = a["n"]
        row = {"side": SIGNAL_STRATS[name][0], "n": n}
        if n:
            row.update({"win": a["win"], "avg": round(a["ret"] / n, 2), "base": round(a["base"] / n, 2),
                        "edge": round((a["ret"] - a["base"]) / n, 2),
                        "by": {k: {"n": v["n"], "edge": round((v["ret"] - v["base"]) / v["n"], 2)} for k, v in a["by"].items() if v["n"]}})
        out["strats"][name] = row
    return out


# ---- 全市場爆量榜(試跑)----
# 股票清單與前一交易日成交量來自證交所/櫃買中心公開資料,報價沿用同一個報價API,
# 倍數=今日累積量/(前一交易日全天量×正常已成交比例),比自選股用的20日中位數粗糙,只拿來「找出沒在自選的爆量股」。
# 試跑期只在 09:30 後第一輪掃一次,整段限時,批次之間有間隔,連續失敗就停,失敗只記錄診斷、不影響主流程。
MARKET_SNAPS = (30,)         # 試跑期只在 09:30 後掃一次,確認不會被限流再增加
MARKET_TIME_BUDGET = 45
MARKET_GAP = 0.4            # 每批之間間隔秒數,避免短時間大量請求
MARKET_MAX_FAIL = 2         # 連續失敗幾批就停(可能被限流,不要硬撐影響主流程)
MARKET_MIN_PREV = 500       # 前一交易日量低於500張的不掃
MARKET_MIN_VOL = 1000       # 今日累積量低於1000張不列入
TWSE_ALL_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_ALL_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"


def _to_float(x):
    try:
        return float(str(x).replace(",", ""))
    except (TypeError, ValueError):
        return None


def load_universe(run_time, errors):
    """回傳 {代號: 前一交易日成交量(張)}。每天只抓一次,存 universe.json。"""
    today = run_time.strftime("%Y-%m-%d")
    cached = load_json_safe(UNIVERSE_FILE, {}) or {}
    if cached.get("date") == today and cached.get("stocks"):
        return cached["stocks"]
    stocks = {}
    for url, ck, vk, div in ((TWSE_ALL_URL, "Code", "TradeVolume", 1000), (TPEX_ALL_URL, "SecuritiesCompanyCode", "TradingShares", 1000)):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            resp.raise_for_status()
            rows = resp.json()
            n0 = len(stocks)
            for r in rows if isinstance(rows, list) else []:
                code = str(r.get(ck) or "").strip()
                vol = _to_float(r.get(vk))
                if re.fullmatch(r"[1-9]\d{3}", code) and vol:
                    stocks[code] = round(vol / div)
            if len(stocks) == n0:
                errors.append(f"{url.split('/')[2]} 回傳 {len(rows) if isinstance(rows, list) else type(rows).__name__} 筆但沒有可用資料,欄位: {sorted(rows[0].keys()) if isinstance(rows, list) and rows else '-'}")
        except (requests.RequestException, ValueError) as e:
            errors.append(f"{url.split('/')[2]} 失敗: {str(e)[:120]}")
    if stocks:
        write_json_atomic(UNIVERSE_FILE, {"date": today, "stocks": stocks}, separators=(",", ":"))
    return stocks


def scan_market(run_time, watch_codes):
    m = (run_time - _day_start(run_time)).total_seconds() / 60
    sn = next((x for x in MARKET_SNAPS if x <= m < x + 12), None)
    if sn is None:
        return
    today = run_time.strftime("%Y-%m-%d")
    log = load_json_safe(MARKET_FILE, {}) or {}
    if log.get("date") != today:
        log = {"date": today, "scans": {}}
    if str(sn) in log["scans"]:
        return
    t0 = time.time()
    errors = []
    uni = load_universe(run_time, errors)
    diag = {"universe": len(uni), "errors": errors}
    items = []
    if uni:
        codes = [c for c, v in uni.items() if v >= MARKET_MIN_PREV]
        _per, pooled = load_curves()
        n_rows = n_batch_fail = streak = 0
        for i in range(0, len(codes), 50):
            if time.time() - t0 > MARKET_TIME_BUDGET:
                errors.append(f"超過 {MARKET_TIME_BUDGET} 秒預算,只掃了前 {i} 檔")
                break
            batch = [f"TWS:{c}:STOCK" for c in codes[i:i + 50]]
            try:
                resp = requests.get(CNYES_QUOTE_URL.format(codes=",".join(batch)), headers=HEADERS, timeout=TIMEOUT)
                resp.raise_for_status()
                rows = resp.json().get("data", [])
            except (requests.RequestException, ValueError) as e:
                n_batch_fail += 1
                streak += 1
                if len(errors) < 5:
                    errors.append(f"報價批次失敗: {str(e)[:100]}")
                if streak >= MARKET_MAX_FAIL:
                    errors.append("連續失敗,中止掃描")
                    break
                time.sleep(MARKET_GAP)
                continue
            streak = 0
            time.sleep(MARKET_GAP)
            for row in rows:
                code = row.get("0")
                vol, last, prev = row.get("200013"), row.get("6"), uni.get(code)
                if not (_num_ok(vol, last) and prev) or vol < MARKET_MIN_VOL:
                    continue
                n_rows += 1
                pace = vol / (prev * frac_for(code, m, {}, pooled))
                items.append({"code": code, "name": row.get("200009") or code, "last": last, "pct": row.get("56"),
                              "vol": vol, "prev": prev, "pace": round(pace, 2), "in_watch": code in watch_codes})
        diag.update({"codes": len(codes), "rows": n_rows, "batch_fail": n_batch_fail})
    items.sort(key=lambda x: -x["pace"])
    diag["seconds"] = round(time.time() - t0, 1)
    log["scans"][str(sn)] = {"as_of": run_time.strftime("%H:%M"), "items": items[:40], "diag": diag}
    write_json_atomic(MARKET_FILE, log, separators=(",", ":"))


def dump_fields(all_rows, run_time):
    """每天存一次幾檔報價的原始欄位,用來查有沒有內盤/外盤等可用欄位。"""
    today = run_time.strftime("%Y-%m-%d")
    if (load_json_safe(FIELDS_FILE, {}) or {}).get("date") == today:
        return
    keys = ("TWS:TSE01:INDEX", "TWS:2455:STOCK", "TWS:2379:STOCK", "TWS:0050:STOCK")
    write_json_atomic(FIELDS_FILE, {"date": today, "rows": {k: all_rows[k] for k in keys if k in all_rows}})


def focus_first(run_time):
    """今天每檔「第一次達標」的時間與價格,網頁用來顯示『發現後漲跌』。"""
    log = load_json_safe(FOCUS_FILE, {}) or {}
    day = (log.get("days") or {}).get(run_time.strftime("%Y-%m-%d")) or {}
    return {c: {"t": pk["t"][11:16], "price": pk["price"], "pct": pk.get("pct")} for c, pk in (day.get("picks") or {}).items()}


def _avg_daily_volume(official_bars, code, today):
    bars = [b for b in (official_bars.get(code) or []) if b.get("d") and b["d"] < today and _num_ok(b.get("v")) and b["v"] > 0]
    bars = bars[-OPEN_AVG_DAYS:]
    if len(bars) < 5:
        return None
    vs = sorted(b["v"] for b in bars)
    return vs[len(vs) // 2]   # 用中位數,避免被前幾天的爆量日拉高基準


def compute_opening_alerts(run_time, watchlist_out, official):
    """回傳 (輸出給網頁的字典, 要存進歷史檔的狀態字典)。09:31 之後不再更新,只把已記錄的名單原樣回傳。"""
    today = run_time.strftime("%Y-%m-%d")
    state = {}
    prev_pt = None
    try:
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                ex = json.load(f)
            if ex.get("date") == today:
                state = ex.get("opening") or {}
                pts = [p for p in (ex.get("points") or []) if p.get("wlv")]
                prev_pt = pts[-1] if pts else None
    except (json.JSONDecodeError, OSError):
        state = {}
    m = (run_time - run_time.replace(hour=9, minute=0, second=0, microsecond=0)).total_seconds() / 60
    active = OPENING_START_MIN < m <= OPENING_END_MIN
    curves_per, curves_pooled = load_curves()
    # 全天量能倍數:每一檔都算(網頁「今日焦點」用),不只開盤半小時
    if m > 2:
        _bars_all = (official or {}).get("bars") or {}
        for q in watchlist_out:
            vol_ = q.get("volume")
            avg_ = _avg_daily_volume(_bars_all, q["code"], today)
            if _num_ok(vol_) and vol_ >= OPEN_MIN_VOL and avg_:
                q["pace"] = round(vol_ / (avg_ * frac_for(q["code"], m, curves_per, curves_pooled)), 2)
    if active:
        bars = (official or {}).get("bars") or {}
        pm = None
        if prev_pt:
            try:
                pt = datetime.fromisoformat(prev_pt["t"])
                pm = (pt - run_time.replace(hour=9, minute=0, second=0, microsecond=0)).total_seconds() / 60
            except (ValueError, KeyError, TypeError):
                pm = None
        for q in watchlist_out:
            code, vol = q["code"], q.get("volume")
            if not _num_ok(vol) or vol < OPEN_MIN_VOL:
                continue
            avg = _avg_daily_volume(bars, code, today)
            exp = (avg * frac_for(code, m, curves_per, curves_pooled)) if avg else None
            if not exp:
                continue
            pace = vol / exp
            burst = None
            if prev_pt and pm is not None and pm >= 0 and m - pm >= 2:
                pv = (prev_pt.get("wlv") or {}).get(code)
                if _num_ok(pv) and vol >= pv:
                    e5 = avg * (frac_for(code, m, curves_per, curves_pooled) - frac_for(code, pm, curves_per, curves_pooled))
                    burst = (vol - pv) / e5 if e5 > 0 else None
            pct = q.get("pct")
            cur = {"pace": round(pace, 2), "burst": round(burst, 2) if burst is not None else None}
            st = state.get(code)
            if pace >= OPEN_PACE_ALERT or st:
                if not st:
                    st = {"first_t": run_time.strftime("%H:%M"), "peak_pace": 0}
                if pace >= st.get("peak_pace", 0):
                    st["peak_pace"] = round(pace, 2)
                    st["peak_t"] = run_time.strftime("%H:%M")
                st.update({"pace": cur["pace"], "burst": cur["burst"], "pct": pct, "last": q.get("last"),
                           "vol": vol, "avg": round(avg), "name": q["name"], "t": run_time.strftime("%H:%M")})
                if pace >= OPEN_PACE_ALERT:
                    st["hot_now"] = True
                else:
                    st["hot_now"] = False
                state[code] = st
    items = []
    for code, st in state.items():
        pct = st.get("pct")
        if pct is None or abs(pct) < OPEN_FLAT_PCT:
            side = "flat"
        else:
            side = "in" if pct > 0 else "out"
        items.append({"code": code, "name": st.get("name"), "side": side, "pace": st.get("pace"), "peak_pace": st.get("peak_pace"),
                      "burst": st.get("burst"), "pct": pct, "last": st.get("last"), "vol": st.get("vol"), "avg": st.get("avg"),
                      "first_t": st.get("first_t"), "peak_t": st.get("peak_t"), "hot_now": bool(st.get("hot_now")),
                      "strong": (st.get("peak_pace") or 0) >= OPEN_PACE_STRONG})
    items.sort(key=lambda x: -(x["peak_pace"] or 0))
    out = {"active": active, "window": "09:00-09:30", "as_of": run_time.strftime("%H:%M"), "items": items[:30]}
    return out, state


def update_history(run_time, indices_out, watchlist_out, opening_state=None):
    """把這次抓到的價格疊加進 data/intraday_history.json。
    只留「今天」的點(用台北日期判斷,跨到隔天自動重置),最多留 MAX_POINTS_PER_DAY 筆,
    避免檔案越養越大。每筆存 last 價(夠畫走勢小圖),自選股另外存當下的累積成交量(wlv),
    讓網頁可以用「這次量-上次量」算出每個5分鐘區間自己的量,疊出近似K線圖的量能長條。"""
    today_str = run_time.strftime("%Y-%m-%d")

    history = {"date": today_str, "points": []}
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                existing = json.load(f)
            if existing.get("date") == today_str:
                history = existing
        except (json.JSONDecodeError, OSError):
            pass

    point = {
        "t": run_time.isoformat(),
        "idx": {q["code"]: q["last"] for q in indices_out if q.get("last") is not None},
        "wl": {q["code"]: q["last"] for q in watchlist_out if q.get("last") is not None},
        "wlv": {q["code"]: q["volume"] for q in watchlist_out if q.get("volume") is not None},
    }
    history["points"].append(point)
    history["points"] = history["points"][-MAX_POINTS_PER_DAY:]
    if opening_state is not None:
        history["opening"] = opening_state

    write_json_atomic(HISTORY_FILE, history)


def _is_real_trading_day(bars):
    """bars是{code:{h,l,c,v}}這種「一天份」的資料。判斷依據:只要有任何一檔股票成交量
    是正的、或是最高不等於最低(代表當天價格真的有動),就當作是真的有開盤交易的一天。
    國定假日/颱風假這種排程照跑但其實沒開盤的日子,cnyes回傳的每一檔都會是量=0、
    高=低=昨收的死數字,全部檔位都符合這兩個條件才會判斷為「非交易日」。"""
    if not bars:
        return False
    for bar in bars.values():
        if bar.get("v"):
            return True
        if bar.get("h") is not None and bar.get("l") is not None and bar["h"] != bar["l"]:
            return True
    return False


def _dedupe_days(days):
    """days新到舊。若某一天跟「比它更舊的那一天」在90%以上的股票上高低收完全相同,
    代表那天其實沒開盤(cnyes假日會回傳前一天的完整數字),把這個假日移除。"""
    out = []
    for i, day in enumerate(days):
        bars = day.get("bars") or {}
        older = (days[i + 1].get("bars") or {}) if i + 1 < len(days) else {}
        common = [c for c in bars if c in older]
        if common:
            same = sum(1 for c in common if all(bars[c].get(k) == older[c].get(k) for k in ("h", "l", "c")))
            if same / len(common) >= 0.9:
                continue
        out.append(day)
    return out


def update_daily_bars(run_time, watchlist_out):
    """維護 data/daily_bars.json:存「今天目前為止」+「最近 MAX_HISTORY_DAYS 個交易日」的
    高低收量(近似值,收盤價用當天最後一次抓到的成交價,成交量同理)。換日期時自動把舊的
    「今天」疊進歷史清單最前面(新到舊排列)。

    只用這支程式本來就在抓的cnyes報價滾動累積,不必再多接證交所的歷史API、也不用分上市上櫃,
    壞處是剛上線前幾天資料還不夠多,CDP/量能倍率/多日趨勢都要等資料長出來才會生效,這是刻意
    的取捨,詳見檔案開頭的說明。

    回傳 (prev_bars, history_days):
      prev_bars: 前一個交易日的 {code:{h,l,c,v}},給 compute_cdp() 用。
      history_days: 新到舊排列、不含今天的歷史清單,給量能倍率、多日趨勢用。
    """
    today_str = run_time.strftime("%Y-%m-%d")
    stored = load_json_safe(DAILY_BARS_FILE, None)
    if stored is None and os.path.exists(DAILY_BARS_FILE):
        # 檔案存在但讀不出來(壞檔):不要退回v4舊檔把歷史洗成1天,直接中止這次,保留原檔讓人查
        raise RuntimeError("data/daily_bars.json 存在但無法解析,中止以保護歷史資料")

    if stored is None:
        stored = {"date": None, "today": {}, "days": []}

    stored["days"] = _dedupe_days(stored.get("days", []))

    if stored.get("date") != today_str:
        # 只有「看起來真的有開盤交易」的一天才滾進歷史;國定假日排程照跑但沒開盤的話,
        # 那天的死數字直接丟棄不留痕跡,換日期後從頭累積「今天」,不會汙染CDP/量能歷史。
        if stored.get("today") and _is_real_trading_day(stored["today"]):
            stored.setdefault("days", []).insert(0, {"date": stored.get("date"), "bars": stored["today"]})
            stored["days"] = stored["days"][:MAX_HISTORY_DAYS]
        stored["today"] = {}
        stored["date"] = today_str

    for q in watchlist_out:
        if q.get("high") is not None and q.get("low") is not None and q.get("last") is not None:
            stored["today"][q["code"]] = {
                "h": q["high"], "l": q["low"], "c": q["last"], "v": q.get("volume"),
            }

    write_json_atomic(DAILY_BARS_FILE, stored)

    days = stored.get("days", [])
    prev_bars = days[0]["bars"] if days else {}
    return prev_bars, days


def compute_volume_multiple(today_volume, code, history_days):
    """量能倍率 = 今日成交量 / 最近 VOLUME_LOOKBACK_DAYS 個交易日的平均成交量。
    不到3天歷史資料時回傳 None,網頁端要顯示「資料還在累積中」,不要硬算出一個不可靠的倍率。"""
    if today_volume is None:
        return None
    vols = []
    for day in history_days[:VOLUME_LOOKBACK_DAYS]:
        bar = (day.get("bars") or {}).get(code)
        if bar and bar.get("v"):
            vols.append(bar["v"])
    if len(vols) < 3:
        return None
    avg = sum(vols) / len(vols)
    return round(today_volume / avg, 2) if avg else None


def compute_cdp_trend(code, history_days, today_cdp):
    """回推最近幾天「當時」適用的CDP近高/近低值,串成一條由舊到新的小時間序列,
    讓網頁可以畫出壓力/支撐是不是越墊越高/越墊越低。history_days 新到舊排列、不含今天;
    每一天要搭配「它的前一天」才能算出當天的CDP,所以最多隻能往回推 len(history_days)-1 天。"""
    points = []
    for i in range(len(history_days) - 1, 0, -1):
        c = compute_cdp((history_days[i].get("bars") or {}).get(code))
        if c:
            points.append({"date": history_days[i - 1].get("date"), "nh": c["nh"], "nl": c["nl"], "cdp": c["cdp"]})
    if today_cdp:
        points.append({"date": "今天", "nh": today_cdp["nh"], "nl": today_cdp["nl"], "cdp": today_cdp["cdp"]})
    return points[-CDP_TREND_POINTS:]


def compute_cdp(prev_hlc_for_code):
    """CDP逆勢操作系統:用前一個交易日的高/低/收算今天的樞紐價跟近高/近低值。
    prev_hlc_for_code 是 {"h":前高, "l":前低, "c":前收} 或 None/缺值時回傳 None(表示還沒有
    足夠資料可以算,網頁端要自己 fallback 回今日高/今日低)。"""
    if not prev_hlc_for_code:
        return None
    h, l, c = prev_hlc_for_code.get("h"), prev_hlc_for_code.get("l"), prev_hlc_for_code.get("c")
    if h is None or l is None or c is None:
        return None
    cdp = (h + l + 2 * c) / 4
    rng = h - l
    return {
        "cdp": round(cdp, 4),
        "nh": round(2 * cdp - l, 4),   # 近高值,當壓力參考
        "nl": round(2 * cdp - h, 4),   # 近低值,當支撐參考
        "ah": round(cdp + rng, 4),     # 最高值
        "al": round(cdp - rng, 4),     # 最低值
        "basis": {"h": h, "l": l, "c": c},
    }


def _valid_bar(h, l, c):
    try:
        return h > 0 and l > 0 and c > 0 and h >= l and l - 1e-6 <= c <= h + 1e-6
    except TypeError:
        return False


_HIST_ALIASES = {"t": ("t", "time", "timestamp", "date"), "o": ("o", "open"), "h": ("h", "high"),
                 "l": ("l", "low"), "c": ("c", "close"), "v": ("v", "volume", "vol")}
_HIST_STATE = {"variant": 0, "win": 0, "debug": None, "fails": {}}   # variant:上次成功的參數寫法;debug:第一筆失敗回應的樣本(診斷用)


class _EmptyHistory(ValueError):
    """API 正常回應,但這檔股票沒有任何日K(不是格式問題,重試也沒用)。"""


def _to_date(ts):
    if isinstance(ts, str):
        return ts[:10]
    ts = float(ts)
    if ts > 1e11:   # 毫秒
        ts /= 1000.0
    return datetime.fromtimestamp(ts, TAIPEI).strftime("%Y-%m-%d")


def _parse_history(payload):
    """把各種可能的日K回傳格式整理成 {t,o,h,l,c,v} 欄位陣列。
    支援:data 是欄位陣列的字典(t/o/h/l/c/v 或 open/high/low/close/volume)、
    data 是一列一根K的字典清單、data 是 [時間,開,高,低,收,量] 的陣列清單。看不懂就丟 ValueError。"""
    data = payload.get("data") if isinstance(payload, dict) else payload
    cols = {}
    if isinstance(data, dict):
        for k, names in _HIST_ALIASES.items():
            for nm in names:
                if isinstance(data.get(nm), list):
                    cols[k] = data[nm]
                    break
    elif isinstance(data, list) and data and isinstance(data[0], dict):
        cols = {k: [] for k in _HIST_ALIASES}
        for row in data:
            for k, names in _HIST_ALIASES.items():
                cols[k].append(next((row[n] for n in names if n in row), None))
    elif isinstance(data, list) and data and isinstance(data[0], (list, tuple)) and len(data[0]) >= 5:
        cols = {k: [r[i] if i < len(r) else None for r in data] for i, k in enumerate("tohlcv")}
    t, h, l, c = (cols.get(k) or [] for k in "thlc")
    if cols and "t" in cols and not t and not h and not l and not c:
        raise _EmptyHistory("日K無資料")
    if not t or not (len(t) == len(h) == len(l) == len(c)):
        raise ValueError("日K格式不符")
    return cols


def fetch_history_one(code, run_time):
    """抓單一股票最近約100個日曆天的日K。回傳由舊到新排列的 [{d,o,h,l,c,v}],只留通過基本檢查的。
    參數 from/to 的先後順序不確定(有些平台是 from 比 to 大),兩種都試,記住成功的那種。
    格式不符就丟例外,並把第一筆失敗回應的前300字存起來(寫進 daily_official.json 的 debug),方便診斷。"""
    to_ts = int(run_time.timestamp())
    windows = [HISTORY_FETCH_DAYS_LONG, HISTORY_FETCH_DAYS]
    wins = [_HIST_STATE["win"]] + [i for i in range(len(windows)) if i != _HIST_STATE["win"]]
    vars_ = [_HIST_STATE["variant"]] + [i for i in range(2) if i != _HIST_STATE["variant"]]
    cols = None
    attempts = empties = 0
    for wi in wins:
        old_ts = to_ts - windows[wi] * 86400
        variants = [(old_ts, to_ts), (to_ts, old_ts)]
        for vi in vars_:
            frm, to = variants[vi]
            params = {"resolution": "D", "symbol": f"TWS:{code}:STOCK", "from": frm, "to": to}
            resp = requests.get(CNYES_HISTORY_URL, params=params, headers=HEADERS, timeout=HISTORY_TIMEOUT)
            resp.raise_for_status()
            attempts += 1
            try:
                cols = _parse_history(resp.json())
                _HIST_STATE["variant"], _HIST_STATE["win"] = vi, wi
                break
            except _EmptyHistory:
                empties += 1
            except (ValueError, TypeError, AttributeError):
                pass
            txt = getattr(resp, "text", "")
            sample = f"{code} win{windows[wi]} variant{vi} HTTP{getattr(resp, 'status_code', '?')} {str(txt)[:300]}"
            _HIST_STATE["last_sample"] = sample
            if _HIST_STATE["debug"] is None:
                _HIST_STATE["debug"] = sample
        if cols is not None:
            break
    if cols is None:
        if attempts and empties == attempts:
            raise _EmptyHistory("日K無資料")
        if len(_HIST_STATE["fails"]) < 12:
            _HIST_STATE["fails"][code] = _HIST_STATE.get("last_sample", "")
        raise ValueError("日K格式不符")
    t, o, h, l, c, v = (cols.get(k) or [] for k in "tohlcv")
    bars = []
    for i in range(len(t)):
        try:
            if not _valid_bar(h[i], l[i], c[i]):
                continue
            bars.append({
                "d": _to_date(t[i]),
                "o": o[i] if i < len(o) else None,
                "h": h[i], "l": l[i], "c": c[i],
                "v": v[i] if i < len(v) else None,
            })
        except (TypeError, ValueError):
            continue
    bars.sort(key=lambda b: b["d"])
    return bars[-OFFICIAL_KEEP_DAYS:]


def refresh_official_bars(run_time):
    """正式日K逐檔追蹤:每檔記錄「哪一天抓成功」,今天還沒成功的檔,同一天之內再補抓。
    每一輪最多花 REFRESH_BUDGET_SECONDS 秒,超過就留給下一輪;有失敗的話一小時內不重打。
    任何例外都不往外拋,日K是加分資料,抓不到不能影響即時報價主流程。"""
    today = run_time.strftime("%Y-%m-%d")
    stored = load_json_safe(DAILY_OFFICIAL_FILE, {}) or {}
    fetched = dict(stored.get("fetched") or {})
    no_data = set(stored.get("no_data") or [])
    schema_ok = stored.get("keep_days") == OFFICIAL_KEEP_DAYS
    if not schema_ok:   # 保留天數改版:今天全部重抓一次,把歷史補長
        fetched, no_data = {}, set()
    # 收盤後(13:32起)再把今天的日K整批重抓一次:盤中抓到的今天那根是「抓當下」的半成品,
    # 收盤後才是正式的高低收量。每檔收盤後抓成功就記進 finalized。
    after_close = run_time.hour * 60 + run_time.minute >= 13 * 60 + 32
    finalized = dict(stored.get("finalized") or {}) if schema_ok else {}
    needed = [c for c in WATCHLIST_NAMES if fetched.get(c) != today or (after_close and finalized.get(c) != today)]
    if not needed:
        return stored
    last_try = stored.get("last_attempt")
    if schema_ok and stored.get("last_attempt_date") == today and last_try:
        try:
            gap = HISTORY_RETRY_GAP_FAIL
            if (run_time - datetime.fromisoformat(last_try)).total_seconds() < gap:
                return stored
        except ValueError:
            pass

    merged = {c: v for c, v in (stored.get("bars") or {}).items() if c in WATCHLIST_NAMES}   # 不在自選股的舊代號順手清掉
    errors = []
    started = time.time()
    for code in needed:
        if time.time() - started > REFRESH_BUDGET_SECONDS:
            errors.append("本輪時間用完,其餘留給下一輪")
            break
        try:
            bars = fetch_history_one(code, run_time)
            if bars:
                merged[code] = bars
                fetched[code] = today
                if after_close:
                    finalized[code] = today
                no_data.discard(code)
            else:
                errors.append(f"{code}: 沒有可用資料")
        except _EmptyHistory:
            fetched[code] = today     # 來源明確回覆沒有這檔的日K,今天不再重試
            if after_close:
                finalized[code] = today
            no_data.add(code)
        except Exception as e:  # noqa: BLE001  日K失敗不能中斷主流程
            errors.append(f"{code}: {type(e).__name__} {str(e)[:60]}")
        time.sleep(0.15)

    total = len(WATCHLIST_NAMES)
    ok = sum(1 for c in WATCHLIST_NAMES if fetched.get(c) == today)
    nd = sorted(c for c in no_data if c in WATCHLIST_NAMES)
    status = f"{'ok' if ok == total else ('partial' if ok else 'fail')} {ok}/{total}"
    if nd:
        status += "(來源無日K:" + ",".join(nd) + ")"
    result = {
        "fetched": fetched,
        "finalized": finalized,
        "last_attempt": run_time.isoformat(),
        "last_attempt_date": today,
        "status": status,
        "keep_days": OFFICIAL_KEEP_DAYS,
        "no_data": nd,
        "source": "cnyes 日K歷史API (resolution=D)",
        "errors": errors[:10],
        "debug": _HIST_STATE["debug"],
        "fail_samples": _HIST_STATE["fails"],
        "bars": merged,
    }
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        write_json_atomic(DAILY_OFFICIAL_FILE, result)
    except (OSError, ValueError):
        pass
    return result


def compute_swing(bars):
    """bars: 由舊到新、不含今天的日K清單(每筆至少有 h、l,c可有可無)。
    回傳最近5/10/20日的最高/最低(實際用了幾天也一併回傳)、5日平均振幅(adr5,百分比)、
    以及5/10/20/60日均線(收盤價平均,天數不夠就不給)。少於3天資料回傳 None。"""
    if not bars or len(bars) < 3:
        return None
    out = {}
    for n in (5, 10, 20):
        seg = bars[-n:]
        out[f"h{n}"] = max(b["h"] for b in seg)
        out[f"l{n}"] = min(b["l"] for b in seg)
        out[f"d{n}"] = len(seg)
    ranges = [(b["h"] - b["l"]) / b["c"] * 100 for b in bars[-5:] if b.get("c")]
    out["adr5"] = round(sum(ranges) / len(ranges), 2) if ranges else None
    closes = [b["c"] for b in bars if b.get("c")]
    ma = {}
    for n in (5, 10, 20, 60):
        if len(closes) >= n:
            ma[f"ma{n}"] = round(sum(closes[-n:]) / n, 2)
    out["ma"] = ma
    return out


TOUCH_TOL_ADR = 0.06     # 「碰到」的距離 = 該檔近10日平均振幅的 6%(限制在 0.05%~0.5%)
TOUCH_REV_ADR = 0.25     # 「明確彈開/回落」= 收盤離線至少平均振幅的 25%(限制在 0.2%~2%)
CONF_ADR = 0.2           # 線疊加的距離 = 平均振幅的 20%(限制在 0.3%~1%)
TOUCH_BASES = ("cdp", "sw5", "sw10", "sw20", "c1", "c2")   # c2=多條不同類的線疊在一起(疊加強度>=2),c1=單一線
VP_DAYS = 60             # 量能密集區用最近幾個交易日
VP_BIN_PCT = 0.5         # 量能分格寬度(佔現價 %)
VP_NODE_MULT = 1.5       # 某一格成交量 >= 平均 1.5 倍才算密集區
VOL_HI_MULT = 1.2        # 碰線當天量 >= 前5日均量 1.2 倍算放量
REGIME_WEAK = -1.0       # 自選股當日漲跌幅中位數 <= -1% 算大盤偏弱
REGIME_STRONG = 1.0
LINE_FAMILY = {"CDP": "cdp", "5日": "swing", "10日": "swing", "20日": "swing",
               "週線": "ma", "10日線": "ma", "月線": "ma", "季線": "ma", "量能區": "vp"}


def _num_ok(*xs):
    return all(isinstance(x, (int, float)) and not isinstance(x, bool) and x == x for x in xs)


def avg_range_pct(hist, n=10):
    """最近 n 根日K的平均振幅 (高-低)/收 %,資料不足回傳預設 2.0。用來讓各項門檻跟著每檔股票自己的波動走。"""
    rs = [(x["h"] - x["l"]) / x["c"] * 100 for x in (hist or [])[-n:] if _num_ok(x.get("h"), x.get("l"), x.get("c")) and x["c"] > 0]
    return sum(rs) / len(rs) if len(rs) >= 3 else 2.0


def touch_params(adr):
    tol = min(max(TOUCH_TOL_ADR * adr, 0.05), 0.5)
    rev = min(max(TOUCH_REV_ADR * adr, 0.2), 2.0)
    conf = min(max(CONF_ADR * adr, 0.3), 1.0)
    return tol, rev, conf


def regime_label(median_pct):
    if median_pct is None:
        return None
    return "weak" if median_pct <= REGIME_WEAK else "strong" if median_pct >= REGIME_STRONG else "normal"


def compute_regime_map(official_bars):
    """每個交易日「自選股漲跌幅中位數」。用自選股自己的日K推出來,不必另外抓大盤日K。
    至少20檔都有資料的日子才算,回傳 {日期: 中位數%}。"""
    by = {}
    for bars in (official_bars or {}).values():
        prev = None
        for b in bars or []:
            if not _num_ok(b.get("c")) or b["c"] <= 0:
                prev = None
                continue
            if prev:
                by.setdefault(b["d"], []).append((b["c"] / prev - 1) * 100)
            prev = b["c"]
    out = {}
    for d, v in by.items():
        if len(v) >= 20:
            v.sort()
            m = len(v) // 2
            out[d] = v[m] if len(v) % 2 else (v[m - 1] + v[m]) / 2
    return out


def compute_volume_nodes(bars, ref_price):
    """成交量密集區:把最近 VP_DAYS 天每天的成交量平均攤到當天的高低價區間,
    累加後找出比平均高 VP_NODE_MULT 倍的局部高點。回傳 (現價上方最近的密集價, 現價下方最近的密集價)。
    沒有成交量或資料太少回傳 (None, None)。"""
    if not _num_ok(ref_price) or ref_price <= 0:
        return None, None
    seg = [b for b in (bars or [])[-VP_DAYS:] if _num_ok(b.get("h"), b.get("l"), b.get("v")) and b["v"] > 0 and b["h"] >= b["l"]]
    if len(seg) < 10:
        return None, None
    lo, hi = min(b["l"] for b in seg), max(b["h"] for b in seg)
    w = ref_price * VP_BIN_PCT / 100
    nb = int((hi - lo) / w) + 1
    if nb > 400:
        nb = 400
        w = (hi - lo) / nb
    vol = [0.0] * nb
    for b in seg:
        i0 = min(int((b["l"] - lo) / w), nb - 1)
        i1 = min(int((b["h"] - lo) / w), nb - 1)
        share = b["v"] / (i1 - i0 + 1)
        for i in range(i0, i1 + 1):
            vol[i] += share
    nz = [x for x in vol if x > 0]
    if not nz:
        return None, None
    mean = sum(nz) / len(nz)
    nodes = []
    for i in range(nb):
        left = vol[i - 1] if i > 0 else 0
        right = vol[i + 1] if i < nb - 1 else 0
        if vol[i] >= mean * VP_NODE_MULT and vol[i] > left and vol[i] >= right:
            nodes.append(lo + (i + 0.5) * w)
    up = [x for x in nodes if x > ref_price * 1.0005]
    dn = [x for x in nodes if x < ref_price * 0.9995]
    return (min(up) if up else None), (max(dn) if dn else None)


def low_range_pct(code):
    return ETF_LOW_RELIABILITY_RANGE_PCT if str(code).startswith("00") else LOW_RELIABILITY_RANGE_PCT


def level_candidates(hist, ref_price, low_pct=None):
    """用 hist(由舊到新、不含評估當天)列出所有候選線:[(價位, 名稱)]。
    CDP 近高/近低(前一天振幅太小就不算)、5/10/20日高低、5/10/20/60日均線、量能密集區。"""
    out = []
    if not hist:
        return out
    last = hist[-1]
    if _num_ok(last.get("h"), last.get("l"), last.get("c")) and last["c"] > 0:
        if (last["h"] - last["l"]) / last["c"] * 100 >= (LOW_RELIABILITY_RANGE_PCT if low_pct is None else low_pct):
            cd = compute_cdp(last)
            if cd:
                out += [(cd["nh"], "CDP"), (cd["nl"], "CDP")]
    for n in (5, 10, 20):
        seg = [x for x in hist[-n:] if _num_ok(x.get("h"), x.get("l"))]
        if len(seg) == n:
            out += [(max(x["h"] for x in seg), f"{n}日"), (min(x["l"] for x in seg), f"{n}日")]
    closes = [x["c"] for x in hist if _num_ok(x.get("c"))]
    for n, nm in ((5, "週線"), (10, "10日線"), (20, "月線"), (60, "季線")):
        if len(closes) >= n:
            out.append((sum(closes[-n:]) / n, nm))
    up, dn = compute_volume_nodes(hist, ref_price)
    for v in (up, dn):
        if v:
            out.append((v, "量能區"))
    return [(p, s) for p, s in out if _num_ok(p) and p > 0]


def cluster_levels(cands, tol_pct=0.5):
    """把相差 tol_pct(%)以內的線併成一區。strength=區內「不同類別」的線有幾類
    (CDP、波段高低、均線、量能區各算一類;5日高和10日高同價不會被算成兩條)。"""
    groups = []
    for p, s in sorted(cands):
        if groups and p - groups[-1]["start"] <= groups[-1]["start"] * tol_pct / 100:
            groups[-1]["items"].append((p, s))
        else:
            groups.append({"start": p, "items": [(p, s)]})
    out = []
    for g in groups:
        names = []
        for _, s in g["items"]:
            if s not in names:
                names.append(s)
        fam = {LINE_FAMILY.get(s, s) for s in names}
        out.append({"price": round(sum(p for p, _ in g["items"]) / len(g["items"]), 4),
                    "n": len(fam), "srcs": names})
    return out


def split_clusters(cands, ref_price, tol_pct=0.5):
    res = cluster_levels([(p, s) for p, s in cands if p > ref_price], tol_pct)
    sup = cluster_levels([(p, s) for p, s in cands if p < ref_price], tol_pct)
    return res, sup


def _empty_ns():
    return {"n": 0, "held": 0, "rev": 0}


def new_touch_stats():
    st = {k: {"res": _empty_ns(), "sup": _empty_ns()} for k in TOUCH_BASES}
    st["vol"] = {side: {"hi": {"n": 0, "rev": 0}, "lo": {"n": 0, "rev": 0}} for side in ("res", "sup")}
    st["reg"] = {r: {side: {"n": 0, "rev": 0} for side in ("res", "sup")} for r in ("weak", "normal", "strong")}
    st["base"] = {"res": _empty_ns(), "sup": _empty_ns()}   # 對照組:隨機價位的碰線結果
    st["fol"] = {side: {"n1": 0, "ok1": 0, "n3": 0, "ok3": 0} for side in ("res", "sup")}
    st["days"] = 0
    return st


def add_stats(dst, src):
    """把一檔股票的回測結果加進總計(巢狀字典裡的數字逐項相加)。"""
    for k, v in src.items():
        if isinstance(v, dict):
            add_stats(dst[k], v)
        elif k == "days":
            dst[k] = max(dst[k], v)
        else:
            dst[k] += v


def compute_touch_stats(series, regime=None, low_pct=None):
    """觸及回測:用正式日K(由舊到新、不含今天)逐日檢驗「前一天收盤時就能算出來的線」。
    每一天只用當時看得到的資料算 CDP 近高/近低、5/10/20日高低(不含當天),再看當天:
      壓力:最高價進到線的 tol 內算碰到;收盤沒站上算 held;收盤低於線 rev 以上算 rev。
      支撐:最低價進到線內算碰到;收盤沒跌破算 held;收盤高於線 rev 以上算 rev。
      tol / rev / 疊加距離都依「該檔近10日平均振幅」換算(touch_params),高低波動的股票才有可比性。
    另外把所有候選線(含均線、量能密集區)併成「疊加區」:c2=不同類的線疊在一起,c1=單一線。
    並把每次碰線依「當天是否放量」「大盤強弱」分開統計(只記 n 與 rev)。
    開盤就跳空越過線的日子不算;CDP 前一天若振幅太小(漲跌停鎖死)也不算。
    回傳 None 表示可回測的天數少於5天。"""
    bars = [b for b in (series or []) if _num_ok(b.get("h"), b.get("l"), b.get("c"))]
    if len(bars) < 6:
        return None
    st = new_touch_stats()
    rnd = random.Random(20261019)
    tol = rev = 0.0015

    def test(side, line, b, o):
        """回傳 (有碰到, 收盤守住, 明確彈開/回落);跳空越過回傳 None"""
        if side == "res":
            if o is not None and o >= line:
                return None
            if b["h"] < line * (1 - tol):
                return (False, False, False)
            return (True, b["c"] < line, b["c"] <= line * (1 - rev))
        if o is not None and o <= line:
            return None
        if b["l"] > line * (1 + tol):
            return (False, False, False)
        return (True, b["c"] > line, b["c"] >= line * (1 + rev))

    for i in range(1, len(bars)):
        b, pb = bars[i], bars[i - 1]
        o = b.get("o") if _num_ok(b.get("o")) else None
        tol_p, rev_p, conf_p = touch_params(avg_range_pct(bars[:i]))
        tol, rev = tol_p / 100, rev_p / 100
        lv = {}
        pr = (pb["h"] - pb["l"]) / pb["c"] * 100 if pb["c"] else 0
        if pr >= (LOW_RELIABILITY_RANGE_PCT if low_pct is None else low_pct):
            cd = compute_cdp(pb)
            if cd:
                lv["cdp"] = (cd["nh"], cd["nl"])
        for n in (5, 10, 20):
            seg = bars[max(0, i - n):i]
            if len(seg) == n:
                lv[f"sw{n}"] = (max(x["h"] for x in seg), min(x["l"] for x in seg))
        # 對照組:離昨收 0.3~1.5 倍平均振幅的隨機價位(固定種子,每次算出來一樣),用同樣的規則統計
        adr_ = avg_range_pct(bars[:i]) / 100
        if adr_ > 0 and pb["c"] > 0:
            for _ in range(2):
                dist = rnd.uniform(0.3, 1.5) * adr_
                for side, line in (("res", pb["c"] * (1 + dist)), ("sup", pb["c"] * (1 - dist))):
                    r_ = test(side, line, b, o)
                    if r_ and r_[0]:
                        x_ = st["base"][side]
                        x_["n"] += 1
                        x_["held"] += 1 if r_[1] else 0
                        x_["rev"] += 1 if r_[2] else 0
        for k, (R, S) in lv.items():
            for side, line in (("res", R), ("sup", S)):
                if line > 0:
                    r = test(side, line, b, o)
                    if r and r[0]:
                        x = st[k][side]
                        x["n"] += 1
                        x["held"] += 1 if r[1] else 0
                        x["rev"] += 1 if r[2] else 0
        # 疊加區 + 放量 + 大盤強弱
        hist = bars[:i]
        ref = pb["c"]
        vols = [x["v"] for x in bars[max(0, i - 5):i] if _num_ok(x.get("v")) and x["v"] > 0]
        vol_hi = None
        if len(vols) >= 3 and _num_ok(b.get("v")) and b["v"] > 0:
            vol_hi = b["v"] >= VOL_HI_MULT * (sum(vols) / len(vols))
        rg = regime_label(regime.get(b.get("d"))) if regime else None
        res_cl, sup_cl = split_clusters(level_candidates(hist, ref, low_pct), ref, conf_p)
        for side, cls in (("res", res_cl), ("sup", sup_cl)):
            for cl in cls:
                r = test(side, cl["price"], b, o)
                if not (r and r[0]):
                    continue
                k = "c2" if cl["n"] >= 2 else "c1"
                x = st[k][side]
                x["n"] += 1
                x["held"] += 1 if r[1] else 0
                x["rev"] += 1 if r[2] else 0
                if r[1]:  # 碰線當天守住,看隔天、第3天收盤是否還在線的同一側
                    fy = st["fol"][side]
                    ln = cl["price"]
                    for gap, nk, ok_k in ((1, "n1", "ok1"), (3, "n3", "ok3")):
                        if i + gap < len(bars) and _num_ok(bars[i + gap].get("c")):
                            fy[nk] += 1
                            cc = bars[i + gap]["c"]
                            fy[ok_k] += 1 if ((cc < ln) if side == "res" else (cc > ln)) else 0
                if vol_hi is not None:
                    y = st["vol"][side]["hi" if vol_hi else "lo"]
                    y["n"] += 1
                    y["rev"] += 1 if r[2] else 0
                if rg:
                    y = st["reg"][rg][side]
                    y["n"] += 1
                    y["rev"] += 1 if r[2] else 0
    st["days"] = len(bars) - 1
    return st


def _close_matches(c, ref):
    return abs(c - ref) <= max(0.05, ref * OFFICIAL_MISMATCH_PCT / 100)


def compute_cdp_trend_official(bars, today_cdp):
    """用正式日K(由舊到新、不含今天)回推最近幾天「當時」的CDP近高/近低,最後接上今天的CDP。"""
    pts = []
    for i in range(1, len(bars)):
        c = compute_cdp(bars[i - 1])
        if c:
            pts.append({"date": bars[i]["d"], "nh": c["nh"], "nl": c["nl"], "cdp": c["cdp"]})
    if today_cdp:
        pts.append({"date": "今天", "nh": today_cdp["nh"], "nl": today_cdp["nl"], "cdp": today_cdp["cdp"]})
    return pts[-CDP_TREND_POINTS:]


def build_levels(watchlist_out, prev_bars, history_days, official, today):
    """為每檔股票填入 cdp(含資料來源/日期/可靠度與原因)、swing、cdp_trend、volume_multiple。
    前一日高低收優先用正式日K,但必須通過驗證:最後一根距今不超過7天、收盤價跟今天報價的昨收
    幾乎一致(0.3%或0.05元)。驗證不過就退回 daily_bars 的記錄,並在備用記錄的收盤也對不上
    昨收時把CDP標成不可靠(basis_mismatch)。"""
    official_bars = (official or {}).get("bars") or {}
    summary = new_touch_stats()
    summary["stocks"] = 0
    regime_map = compute_regime_map(official_bars)
    pcts = sorted(q["pct"] for q in watchlist_out if _num_ok(q.get("pct")))
    med_today = None
    if len(pcts) >= 20:
        m = len(pcts) // 2
        med_today = pcts[m] if len(pcts) % 2 else (pcts[m - 1] + pcts[m]) / 2
    summary["regime_today"] = {"median_pct": round(med_today, 2), "label": regime_label(med_today)} if med_today is not None else None
    today_date = datetime.strptime(today, "%Y-%m-%d").date()
    for q in watchlist_out:
        code = q["code"]
        pc = q.get("prev_close")
        ob = [b for b in (official_bars.get(code) or []) if b.get("d") and b["d"] < today]
        prev = prev_bars.get(code)
        src, basis_date, use_official = "sampled", None, False
        if ob:
            lastb = ob[-1]
            try:
                age = (today_date - datetime.strptime(lastb["d"], "%Y-%m-%d").date()).days
            except ValueError:
                age = 999
            ok_date = age <= OFFICIAL_MAX_AGE_DAYS
            # 不能比我們自己記錄到的「最近一個交易日」更舊,否則代表日K API還沒更新到昨天
            expected = (history_days[0].get("date") if history_days else None)
            if ok_date and expected and lastb["d"] < expected:
                ok_date = False
            ok_close = (pc is None) or _close_matches(lastb["c"], pc)
            if ok_date and ok_close:
                prev = {"h": lastb["h"], "l": lastb["l"], "c": lastb["c"]}
                src, basis_date, use_official = "official", lastb["d"], True
            else:
                q["official_mismatch"] = True

        # 取得「由舊到新」的日線序列,給波段高低/均線/漲跌停判斷用
        if use_official:
            series = ob
        else:
            series = []
            for day in reversed(history_days):
                b = (day.get("bars") or {}).get(code)
                if b and b.get("h") is not None and b.get("l") is not None:
                    series.append({"d": day.get("date"), "h": b["h"], "l": b["l"], "c": b.get("c")})

        cdp = compute_cdp(prev)
        if cdp:
            h, l, c = cdp["basis"]["h"], cdp["basis"]["l"], cdp["basis"]["c"]
            rng_pct = round((h - l) / c * 100, 2) if c else None
            cdp["basis"]["source"] = src
            if basis_date:
                cdp["basis"]["date"] = basis_date
            cdp["prev_range_pct"] = rng_pct

            prev_move = None
            if len(series) >= 2 and series[-1].get("c") and series[-2].get("c"):
                prev_move = (series[-1]["c"] - series[-2]["c"]) / series[-2]["c"] * 100
            reason = None
            if rng_pct is not None and rng_pct < low_range_pct(code):
                reason = "limit_lock" if (prev_move is not None and abs(prev_move) >= LIMIT_MOVE_PCT) else "narrow_range"
            elif src == "sampled" and pc and not _close_matches(c, pc):
                reason = "basis_mismatch"
            cdp["reliability"] = "low" if reason else "ok"
            if reason:
                cdp["low_reason"] = reason
            if prev_move is not None:
                cdp["prev_move_pct"] = round(prev_move, 2)
        q["cdp"] = cdp

        sw = compute_swing(series)
        if sw:
            sw["source"] = "official" if use_official else "sampled"
        q["swing"] = sw

        # 觸及回測:只用正式日K(取樣記錄太短、也沒有開盤價,不拿來回測)
        q["touch"] = None
        if use_official:
            try:
                ts_ = compute_touch_stats(series, regime_map, low_range_pct(code))
            except Exception as e:  # noqa: BLE001  回測是加分資料,不能影響報價
                print(f"[警告] {code} 觸及回測失敗: {e}")
                ts_ = None
            q["touch"] = ts_
            if ts_:
                summary["stocks"] += 1
                add_stats(summary, ts_)

        # 疊加區與量能密集區:用「現價」把候選線分成上方壓力、下方支撐,各留最近2區
        q["conf"] = None
        q["vp"] = None
        try:
            ref = q.get("last") if _num_ok(q.get("last")) and q["last"] > 0 else None
            if ref and series:
                res_cl, sup_cl = split_clusters(level_candidates(series, ref, low_range_pct(code)), ref, touch_params(avg_range_pct(series))[2])
                res_cl = sorted(res_cl, key=lambda c: c["price"])[:2]
                sup_cl = sorted(sup_cl, key=lambda c: -c["price"])[:2]
                q["conf"] = {"res": res_cl, "sup": sup_cl}
                up, dn = compute_volume_nodes(series, ref)
                q["vp"] = {"res": round(up, 4) if up else None, "sup": round(dn, 4) if dn else None}
        except Exception as e:  # noqa: BLE001
            print(f"[警告] {code} 疊加區計算失敗: {e}")

        q["volume_multiple"] = compute_volume_multiple(q.get("volume"), code, history_days)
        if use_official:
            q["cdp_trend"] = compute_cdp_trend_official(ob, cdp)
        else:
            q["cdp_trend"] = compute_cdp_trend(code, history_days, cdp)
    return summary


def load_json_safe(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def main():
    run_time = taipei_now()

    index_codes = [c for c, _ in INDICES]
    stock_codes = [f"TWS:{code}:STOCK" for code in WATCHLIST_NAMES]
    all_rows = fetch_quotes(index_codes + stock_codes)

    indices_out = [row_to_quote(all_rows.get(code), code, name) for code, name in INDICES]

    # 非交易日/尚未開盤判斷:用報價自己的最新成交時間(台股指數+自選股),不是今天就整輪略過。
    # 不寫歷史、不覆蓋 intraday_latest.json(假日打開網頁看到的就是上一個交易日的資料)。
    ts_list = []
    for code in ("TWS:TSE01:INDEX", "TWS:OTC01:INDEX", *stock_codes):
        row = all_rows.get(code)
        if row and row.get("200007"):
            ts_list.append(row["200007"])
    if ts_list:
        session_date = datetime.fromtimestamp(max(ts_list), TAIPEI).strftime("%Y-%m-%d")
        if session_date != run_time.strftime("%Y-%m-%d"):
            print(f"[略過] 報價時間是 {session_date},今天 {run_time.strftime('%Y-%m-%d')} 還沒有新成交(休市或尚未開盤),這一輪不更新")
            return
    else:
        print("[警告] 報價沒有時間戳,無法判斷是否為交易日,照常處理")

    watchlist_out = [row_to_quote(all_rows.get(f"TWS:{code}:STOCK"), code, name) for code, name in WATCHLIST_NAMES.items()]

    # CDP壓力/支撐(用前一個交易日的高低收算)+ 量能倍率 + 壓力支撐多日趨勢,
    # 三個都是從同一份滾動歷史(data/daily_bars.json)算出來的,取代原本誤用今日漲跌停當壓力支撐的寫法
    prev_bars, history_days = update_daily_bars(run_time, watchlist_out)
    try:
        official = refresh_official_bars(run_time)
    except Exception as e:  # noqa: BLE001  日K是加分資料,出任何狀況都不能影響即時報價
        print(f"[警告] 正式日K更新失敗,改用舊記錄: {e}")
        official = {}
    touch_summary = build_levels(watchlist_out, prev_bars, history_days, official, run_time.strftime("%Y-%m-%d"))

    # 資料實際代表的時間(取所有報價裡最新的時間戳;指數報價偶爾會停在幾分鐘前),跟程式執行時間分開揭露
    data_ts = None
    for q in indices_out + watchlist_out:
        if q.get("ts") and (data_ts is None or q["ts"] > data_ts):
            data_ts = q["ts"]
    data_as_of = (
        datetime.fromtimestamp(data_ts, TAIPEI).isoformat()
        if data_ts else None
    )

    try:
        opening_out, opening_state = compute_opening_alerts(run_time, watchlist_out, official)
    except Exception as e:  # noqa: BLE001  開盤爆量是加分功能,不能影響報價
        print(f"[警告] 開盤爆量偵測失敗: {e}")
        opening_out, opening_state = None, None

    result = {
        "checked_at": run_time.isoformat(),
        "opening_alerts": opening_out,
        "focus_stats": None,
        "focus_first": None,
        "signal_stats": None,
        "groups": GROUP_OF,
        "data_as_of": data_as_of,
        "check_frequency_note": "開盤期間每約5分鐘由GitHub Actions排程抓一次,非逐秒即時報價",
        "official_bars_status": (official or {}).get("status"),
        "touch_summary": touch_summary if touch_summary and touch_summary.get("stocks") else None,
        "market_regime": (touch_summary or {}).get("regime_today"),
        "indices": indices_out,
        "watchlist_quotes": watchlist_out,
    }

    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        update_history(run_time, indices_out, watchlist_out, opening_state)
        record_focus(run_time, watchlist_out, ((touch_summary or {}).get("regime_today") or {}).get("label"))
        process_end_of_day(run_time)
        result["focus_stats"] = focus_stats()
        result["focus_first"] = focus_first(run_time)
        record_signals(run_time, watchlist_out, ((touch_summary or {}).get("regime_today") or {}).get("label"))
        result["signal_stats"] = signal_stats()
    except Exception as e:  # noqa: BLE001  成績單/量能曲線是加分功能,不能影響報價
        print(f"[警告] 歷史/成績單/量能曲線處理失敗: {e}")
    write_json_atomic(OUT_FILE, result, separators=(",", ":"))
    try:
        dump_fields(all_rows, run_time)
        scan_market(run_time, set(WATCHLIST_NAMES))
    except Exception as e:  # noqa: BLE001  試跑功能,任何錯誤都不能影響主流程
        print(f"[警告] 全市場掃描/欄位樣本失敗: {e}")

    n_quote = sum(1 for q in watchlist_out if q.get("last") is not None)
    print(f"[完成] {run_time:%Y-%m-%d %H:%M} 報價 {n_quote}/{len(watchlist_out)} 檔,日K {result['official_bars_status']}")


if __name__ == "__main__":
    main()
