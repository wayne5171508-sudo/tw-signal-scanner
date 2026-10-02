#!/usr/bin/env python3
"""
爆量雷達 - 盤中即時報價擷取

直接呼叫 cnyes(鉅亨網)的公開報價 JSON API,抓大盤指數 + 自選股即時成交價,
寫成 data/intraday_latest.json,交給 index.html(GitHub Pages)顯示。

跟 scan.py 的分工:
- scan.py:每天收盤後,抓 TWSE 官方資料算三大法人買賣超、技術訊號(全部是
  「收盤後」才看得到的資料)。
- intraday.py(這支):開盤期間每隔一段時間抓一次「現在的成交價」,
  給你隨時打開網頁就能看目前大盤跟自選股的即時報價,不是收盤資料。

這支程式刻意不透過 Claude 的 WebFetch 去讀網頁,而是用 Python 直接打 API、
解析真正的 JSON——WebFetch 是用小模型讀網頁摘要,資料量大或重複抓同一個
網址時曾經抓到過期的快取資料,直接打 API 沒有這個問題。

v2新增:順便維護 data/intraday_history.json,把每次抓到的價格疊加進去(只留當天的點、
最多留60筆),讓網頁可以畫出「今天到目前為止」的走勢小圖,不只是單一時間點的數字。

用法:
    python intraday.py
    (會自動寫到 data/intraday_latest.json 跟 data/intraday_history.json)
"""

import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

TAIPEI = ZoneInfo("Asia/Taipei")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; personal-trading-dashboard/1.0)"}
TIMEOUT = 20
DATA_DIR = "data"
OUT_FILE = os.path.join(DATA_DIR, "intraday_latest.json")
HISTORY_FILE = os.path.join(DATA_DIR, "intraday_history.json")
MAX_POINTS_PER_DAY = 60

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

# 自選股(代碼: 名稱)—— 來源:阿文國泰證券App自選股分類彙整,2026-09-25
WATCHLIST_NAMES = {
    "2455": "全新", "3374": "精材", "2408": "南亞科", "2344": "華邦電",
    "6147": "頎邦", "8021": "尖點", "8039": "台虹", "8358": "金居",
    "3264": "欣鈺", "4979": "華星光", "3105": "穩懋", "6173": "信昌電",
    "3016": "嘉晶", "1727": "中華化", "8042": "金山電", "3027": "盛達",
    "4303": "信立", "8111": "立碁", "1815": "富喬", "2630": "亞航",
    "2883": "凱基金", "2881": "富邦金", "2313": "華通", "4919": "新唐",
    "6217": "中探針", "1303": "南亞", "3051": "力特", "6937": "天虹",
    "2337": "旺宏", "6257": "矽格", "3090": "日電貿", "2303": "聯電",
    "2379": "瑞昱", "2609": "陽明", "3231": "緯創", "3707": "漢磊",
    "5425": "台半", "2481": "強茂", "3588": "通嘉", "5347": "世界",
    "6182": "合晶", "2351": "順德", "2449": "京元電子", "6239": "力成",
    "8150": "南茂", "4958": "臻鼎-KY", "3037": "欣興", "6488": "環球晶",
    "3490": "單井", "2409": "友達",
}


def taipei_now():
    return datetime.now(TAIPEI)


def fetch_quotes(codes):
    """codes: list of cnyes代碼字串。不帶 column 參數——帶了反而只會拿到精簡欄位,
    不帶才會拿到完整欄位(含高低開收、漲跌停價)。一次最多抓50筆,分批呼叫。"""
    out = {}
    batch_size = 50
    for i in range(0, len(codes), batch_size):
        batch = codes[i:i + batch_size]
        url = CNYES_QUOTE_URL.format(codes=",".join(batch))
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        body = resp.json()
        for row in body.get("data", []):
            out[row.get("0")] = row
    return out


def row_to_quote(row, fallback_code, fallback_name):
    if row is None:
        return {
            "code": fallback_code, "name": fallback_name,
            "last": None, "change": None, "pct": None,
            "open": None, "high": None, "low": None, "prev_close": None,
            "ts": None,
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
        "upper_limit": row.get("75"),
        "lower_limit": row.get("76"),
        "limit_flag": row.get("200025"),
        "ts": row.get("200007"),
    }


def update_history(run_time, indices_out, watchlist_out):
    """把這次抓到的價格疊加進 data/intraday_history.json。
    只留「今天」的點(用台北日期判斷,跨到隔天自動重置),最多留 MAX_POINTS_PER_DAY 筆,
    避免檔案越養越大。每筆只存 last 價(夠畫走勢小圖),不重複存整包報價。"""
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
    }
    history["points"].append(point)
    history["points"] = history["points"][-MAX_POINTS_PER_DAY:]

    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False)


def main():
    run_time = taipei_now()

    index_codes = [c for c, _ in INDICES]
    stock_codes = [f"TWS:{code}:STOCK" for code in WATCHLIST_NAMES]
    all_rows = fetch_quotes(index_codes + stock_codes)

    indices_out = []
    for code, name in INDICES:
        indices_out.append(row_to_quote(all_rows.get(code), code, name))

    watchlist_out = []
    for code, name in WATCHLIST_NAMES.items():
        cnyes_code = f"TWS:{code}:STOCK"
        q = row_to_quote(all_rows.get(cnyes_code), code, name)
        watchlist_out.append(q)

    # 用任一筆有拿到時間戳的資料,換算成「資料實際代表的時間」,跟「程式執行的時間」分開揭露
    data_ts = None
    for q in indices_out + watchlist_out:
        if q.get("ts"):
            data_ts = q["ts"]
            break
    data_as_of = (
        datetime.fromtimestamp(data_ts, TAIPEI).isoformat()
        if data_ts else None
    )

    result = {
        "checked_at": run_time.isoformat(),
        "data_as_of": data_as_of,
        "check_frequency_note": "開盤期間每約20分鐘由GitHub Actions排程抓一次,非逐秒即時報價",
        "indices": indices_out,
        "watchlist_quotes": watchlist_out,
    }

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    update_history(run_time, indices_out, watchlist_out)

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
