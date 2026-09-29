#!/usr/bin/env python3
"""
爆量雷達 - 收盤後籌碼掃描 (real-code 版本)

直接呼叫台灣證交所(TWSE)官方 JSON API 抓資料,取代原本靠 Claude 的 WebFetch
(小模型讀網頁摘要)去讀大表格的做法 —— 那個做法在資料筆數大時會不穩定漏行,
這支程式用真正的 JSON 解析,不會有這個問題。

這支程式只負責「抓資料 + 算數字」,輸出一份乾淨的 JSON。
新聞面查核、多空判讀文字撰寫、寫入 Artifact 資料庫、推播通知,
還是交給 Claude 的排程去做(那些需要語言理解,程式做不到)。

v11.2新增(阿文以「資深當沖分析師」角度複核後要求補上):
- 族群效應(sector_cluster)、損益平衡提醒(cost_breakeven_tag)、振幅(amplitude_tag)、
  開盤缺口(gap_tag):這些原本v9版本就有,改寫成Python時漏掉了,這次補回來。
- 大盤環境(market_regime):用當天加權指數漲跌幅判斷多頭/空頭/盤整,個股訊號要對照大盤環境看。
- 漲停可執行性(limit_up_locked):標記今天收在漲停附近、實際上可能買不到的股票,避免推薦追不到的標的。
- 戰績追蹤(track_record):每次執行都會回頭檢查「前一個交易日」標記過的focus_stocks/surge_watch,
  抓它們今天實際的開高低收,算出隔日報酬,累積寫進 data/track_record.json,並算出滾動勝率跟平均報酬,
  放進 track_record_stats。這是為了讓這套規則式評分未來有真實數據可以驗證,不是憑感覺信任它。

用法:
    python scan.py > output.json

需要的套件:見 requirements.txt
"""

import glob
import json
import os
import re
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

TAIPEI = ZoneInfo("Asia/Taipei")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; personal-trading-dashboard/1.0)"}
TIMEOUT = 20
DATA_DIR = "data"
TRACK_RECORD_FILE = os.path.join(DATA_DIR, "track_record.json")


# ---------- 基本工具 ----------

def taipei_now():
    return datetime.now(TAIPEI)


def is_plain_stock_code(code):
    """4碼純數字、不是00開頭(排除ETF/權證常見前綴)"""
    return isinstance(code, str) and len(code) == 4 and code.isdigit() and not code.startswith("00")


def clean_number(s):
    """把 '1,234' / '=\"1234\"' / '+12' 這類字串轉成 int/float,轉不了回傳 None"""
    if s is None:
        return None
    s = str(s).strip().strip('="').replace(",", "").replace("+", "")
    if s in ("", "--", "X"):
        return None
    try:
        return int(s)
    except ValueError:
        try:
            return float(s)
        except ValueError:
            return None


# ---------- TWSE 官方 API ----------

def fetch_t86(date_str):
    """三大法人買賣超日報。date_str: YYYYMMDD。回傳 (stat, rows) rows是dict list。"""
    url = f"https://www.twse.com.tw/rwd/zh/fund/T86?date={date_str}&selectType=ALL&response=json"
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    stat = data.get("stat", "")
    if stat != "OK":
        return stat, []
    fields = data.get("fields", [])
    rows = [dict(zip(fields, row)) for row in data.get("data", [])]
    return stat, rows


def fetch_stock_day(stock_no, month_first_day_str):
    """個股日成交資訊(整月)。month_first_day_str: YYYYMM01。"""
    url = (
        f"https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"
        f"?date={month_first_day_str}&stockNo={stock_no}&response=json"
    )
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if data.get("stat") != "OK":
        return []
    fields = data.get("fields", [])
    return [dict(zip(fields, row)) for row in data.get("data", [])]


def fetch_margin(date_str):
    """融資融券日報。回傳 dict: 股票代號 -> row"""
    url = f"https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date={date_str}&selectType=ALL&response=json"
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if data.get("stat") != "OK":
        return {}
    fields = data.get("fields", [])
    rows = [dict(zip(fields, row)) for row in data.get("data", [])]
    out = {}
    for row in rows:
        code = None
        for k in ("股票代號", "代號"):
            if k in row:
                code = str(row[k]).strip().strip('="')
                break
        if code:
            out[code] = row
    return out


def fetch_mi_index(date_str):
    """
    大盤加權指數當日漲跌幅(%),抓不到回傳None。
    用來判斷market_regime,個股訊號要對照大盤環境看,不是每檔都獨立判斷。

    注意:這個TWSE端點很特別,不是T86/STOCK_DAY那種單純{fields,data}格式。
    type=IND這個參數實測回傳data永遠是空的(不知道為什麼,可能是TWSE那邊本來就沒對這個
    type提供資料),必須用type=ALL,而且回傳結構是{tables:[{title,fields,data},...]},
    要自己去tables裡找title包含「價格指數」的那一個,再從它的data列裡找「指數」欄位等於
    「發行量加權股價指數」的那一列,取「漲跌百分比(%)」欄位。這是實測過的正確結構,
    不要改回type=IND或改回簡單的{fields,data}假設,之前v11.2版本就是這樣壞掉的。
    """
    url = f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={date_str}&type=ALL&response=json"
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        if data.get("stat") != "OK":
            return None
        for table in data.get("tables", []):
            title = table.get("title", "") or ""
            if "價格指數" not in title:
                continue
            fields = table.get("fields", [])
            try:
                idx_col = fields.index("指數")
                pct_col = fields.index("漲跌百分比(%)")
            except ValueError:
                continue
            for row in table.get("data", []):
                if len(row) > idx_col and "發行量加權股價指數" in str(row[idx_col]):
                    if len(row) > pct_col:
                        return clean_number(row[pct_col])
        return None
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 大盤指數抓取失敗,略過: {e}", file=sys.stderr)
        return None


# ---------- wantgoo 排行頁(候選名單來源二,best-effort)----------

def fetch_wantgoo_codes(path):
    """
    抓 wantgoo 排行頁(例如 volume-shocker、top-gainer),
    用 pandas.read_html 解析表格,回傳一組 4碼股票代號。
    這段是全程式最容易因為對方網頁改版而壞掉的部分,失敗就回傳空集合,
    不要讓整支程式因為這裡出錯而中斷 —— T86 的候選名單(來源一)才是主力,
    這裡只是加分項目。
    """
    import pandas as pd

    url = f"https://www.wantgoo.com/stock/ranking/{path}"
    codes = set()
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        tables = pd.read_html(r.text)
        for table in tables:
            for row in table.astype(str).values.tolist():
                for cell in row:
                    m = re.match(r"^\s*(\d{4})\b", cell)
                    if m and is_plain_stock_code(m.group(1)):
                        codes.add(m.group(1))
    except Exception as e:  # noqa: BLE001 - 這裡刻意吃掉所有例外,不讓爬蟲失敗拖垮全部
        print(f"[warn] wantgoo {path} 抓取失敗,略過: {e}", file=sys.stderr)
    return codes


def fetch_industry_map(codes):
    """
    上市公司產業別對照(族群效應用)。抓不到某檔就跳過,不編造。
    來源:TWSE ISIN公告網頁(公開資料,含代號/名稱/產業別)。
    """
    import pandas as pd

    url = "https://isin.twse.com.tw/isin/C_public.jsp?strMode=2"
    out = {}
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        r.encoding = "big5"
        tables = pd.read_html(r.text)
        if not tables:
            return out
        df = tables[0]
        for row in df.astype(str).values.tolist():
            if len(row) < 5:
                continue
            first_col = row[0].strip()
            m = re.match(r"^(\d{4})\s+(.+)$", first_col)
            if not m:
                continue
            code = m.group(1)
            if code in codes:
                industry = str(row[4]).strip()
                if industry and industry.lower() != "nan":
                    out[code] = industry
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 產業別對照抓取失敗,略過,族群效應標記為無法查核: {e}", file=sys.stderr)
    return out


# ---------- 多空判讀技術面評分 ----------

def tech_score(net_buy_lots, is_top_buyer, volume_multiple, price_change_pct, prev_close, close):
    """
    依阿文既有的多空判讀公式算技術面評分T(-5~+5)。
    買超方向±2分(規模全市場最大+0.5)、量能倍數爆量+1.5/放量+0.8、
    量價關係同步+1/背離-1.5/溫和+0.5。
    """
    score = 0.0
    breakdown = []

    if net_buy_lots > 0:
        score += 2
        breakdown.append("買超+2")
        if is_top_buyer:
            score += 0.5
            breakdown.append("規模最大+0.5")
    elif net_buy_lots < 0:
        score -= 2
        breakdown.append("賣超-2")

    if volume_multiple is not None:
        if volume_multiple >= 3.0:
            score += 1.5
            breakdown.append("爆量+1.5")
        elif volume_multiple >= 1.5:
            score += 0.8
            breakdown.append("放量+0.8")

    if price_change_pct is not None:
        up = price_change_pct > 0
        down = price_change_pct < 0
        volume_up = volume_multiple is not None and volume_multiple >= 1.5
        if up and volume_up:
            score += 1
            breakdown.append("量價同步上漲+1")
        elif down and volume_up and net_buy_lots > 0:
            score -= 1.5
            breakdown.append("買超但價跌背離-1.5")
        elif up and not volume_up:
            score += 0.5
            breakdown.append("溫和上漲+0.5")

    return round(score, 2), "、".join(breakdown)


def score_to_tag(score, diverged):
    if diverged:
        return "中性(警示)"
    if score >= 4:
        return "偏多(強)"
    if score >= 2.5:
        return "偏多"
    if score >= 1:
        return "中性偏多(力道待確認)"
    if score <= -4:
        return "偏空(強)"
    if score <= -2.5:
        return "偏空"
    if score <= -1:
        return "中性偏空(力道待確認)"
    return "中性(警示)"


def market_regime_from_pct(pct):
    if pct is None:
        return "無法確認"
    if pct >= 0.5:
        return "多頭"
    if pct <= -0.5:
        return "空頭"
    return "盤整"


# ---------- 戰績追蹤 ----------

def load_previous_flagged_stocks(today_iso):
    """
    找本地repo裡日期在今天之前、最新的一份 data/YYYY-MM-DD.json,
    取出它的focus_stocks + surge_watch(排除"候選未達標"),當作要回頭驗證的名單。
    找不到就回傳(None, [])。
    """
    files = glob.glob(os.path.join(DATA_DIR, "20*-*-*.json"))
    candidates = []
    for f in files:
        base = os.path.basename(f).replace(".json", "")
        if re.match(r"^\d{4}-\d{2}-\d{2}$", base) and base < today_iso:
            candidates.append(base)
    if not candidates:
        return None, []
    prev_date = sorted(candidates)[-1]
    try:
        with open(os.path.join(DATA_DIR, f"{prev_date}.json"), encoding="utf-8") as fh:
            prev_data = json.load(fh)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 讀取前一份存檔失敗,跳過戰績追蹤: {e}", file=sys.stderr)
        return None, []

    flagged = []
    for e in prev_data.get("focus_stocks", []) + prev_data.get("surge_watch", []):
        if e.get("signal_status") in ("訊號成立", "放量觀察", "爆量但無法人買盤,風險較高"):
            flagged.append({
                "code": e.get("code"),
                "name": e.get("name"),
                "signal_date": prev_date,
                "signal_close": e.get("close"),
                "signal_status": e.get("signal_status"),
                "tech_score": e.get("tech_score"),
            })
    return prev_date, flagged


def compute_track_record(today_iso, month_first):
    """
    回頭驗證前一個交易日標記過的股票,今天實際表現如何。
    回傳(本次新增的紀錄list, 累積統計dict)。找不到前一天資料或抓不到今天報價就盡量標記清楚,不編數字。
    """
    prev_date, flagged = load_previous_flagged_stocks(today_iso)
    new_records = []

    for item in flagged:
        code = item["code"]
        signal_close = item.get("signal_close")
        if not code or signal_close is None:
            continue
        try:
            days = fetch_stock_day(code, month_first)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 戰績追蹤{code}的STOCK_DAY抓取失敗: {e}", file=sys.stderr)
            continue

        today_row = None
        for d in days:
            if d.get("日期", "").strip().endswith(today_iso[5:].replace("-", "/")):
                today_row = d
                break
        if today_row is None and days:
            today_row = days[-1]  # 保底用最新一筆,通常就是今天
        if today_row is None:
            continue

        open_p = clean_number(today_row.get("開盤價"))
        high_p = clean_number(today_row.get("最高價"))
        low_p = clean_number(today_row.get("最低價"))
        close_p = clean_number(today_row.get("收盤價"))
        if close_p is None:
            continue

        def pct(p):
            return round((p - signal_close) / signal_close * 100, 2) if p is not None else None

        new_records.append({
            "signal_date": item["signal_date"],
            "checked_date": today_iso,
            "code": code,
            "name": item.get("name"),
            "signal_status": item.get("signal_status"),
            "tech_score": item.get("tech_score"),
            "signal_close": signal_close,
            "next_open": open_p,
            "next_high": high_p,
            "next_low": low_p,
            "next_close": close_p,
            "overnight_return_pct": pct(open_p),
            "day_high_return_pct": pct(high_p),
            "close_return_pct": pct(close_p),
        })

    # 讀取歷史累積檔,附加新紀錄
    history = []
    if os.path.exists(TRACK_RECORD_FILE):
        try:
            with open(TRACK_RECORD_FILE, encoding="utf-8") as fh:
                history = json.load(fh)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 讀取track_record.json失敗,視為空歷史: {e}", file=sys.stderr)
            history = []

    history.extend(new_records)

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(TRACK_RECORD_FILE, "w", encoding="utf-8") as fh:
        json.dump(history, fh, ensure_ascii=False, indent=2)

    # 滾動統計(近60筆)
    recent = [h for h in history if h.get("close_return_pct") is not None][-60:]
    if recent:
        wins = [h for h in recent if h["close_return_pct"] > 0]
        stats = {
            "sample_size": len(recent),
            "win_rate_close_pct": round(len(wins) / len(recent) * 100, 1),
            "avg_close_return_pct": round(sum(h["close_return_pct"] for h in recent) / len(recent), 2),
            "avg_day_high_return_pct": round(
                sum(h["day_high_return_pct"] for h in recent if h.get("day_high_return_pct") is not None)
                / max(1, len([h for h in recent if h.get("day_high_return_pct") is not None])),
                2,
            ),
            "note": "這是根據過去實際執行結果累積算出來的統計,不是理論值;樣本數還小時(例如<20)不具統計意義,僅供參考,不構成任何保證。",
        }
    else:
        stats = {"sample_size": 0, "note": "目前還沒有足夠的歷史資料可以計算戰績統計,需要連續執行幾天後才會累積出來。"}

    return new_records, stats


# ---------- 主流程 ----------

def main():
    today = taipei_now()
    date_str = today.strftime("%Y%m%d")
    iso_date = today.strftime("%Y-%m-%d")
    month_first = today.strftime("%Y%m") + "01"

    result = {
        "date": iso_date,
        "generated_at": today.isoformat(),
        "pipeline_version": "v11.2-realcode",
        "scope_decisions": [
            "這份JSON由GitHub Actions上的Python直接呼叫TWSE官方API算出,不經過Claude的WebFetch,"
            "所以不會有大表格漏行的問題。新聞面查核跟最終文字撰寫仍由Claude排程完成。",
            "v11.2補回族群效應/損益平衡/振幅/開盤缺口(改寫成Python時一度漏掉),"
            "新增大盤環境判斷、漲停可執行性標記、戰績追蹤(track_record),"
            "都是為了讓這套規則式評分未來有真實數據可以檢驗,不是純粹加功能。",
        ],
    }

    # T86
    stat, t86_rows = fetch_t86(date_str)
    if stat != "OK":
        result["market_status"] = "data_not_ready_or_no_trading_day"
        result["t86_stat_raw"] = stat
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    result["market_status"] = "OK"

    # 大盤環境
    taiex_change_pct = fetch_mi_index(date_str)
    result["market_regime"] = market_regime_from_pct(taiex_change_pct)
    result["taiex_change_pct"] = taiex_change_pct

    # 候選名單來源一(法人買超前8, 4碼純數字, 排除00開頭)
    parsed = []
    for row in t86_rows:
        code = str(row.get("證券代號", "")).strip().strip('="')
        name = str(row.get("證券名稱", "")).strip()
        net_buy_shares = clean_number(row.get("三大法人買賣超股數"))
        if not is_plain_stock_code(code) or net_buy_shares is None:
            continue
        parsed.append({"code": code, "name": name, "net_buy_lots": round(net_buy_shares / 1000)})

    parsed.sort(key=lambda x: x["net_buy_lots"], reverse=True)
    top8 = [p for p in parsed if p["net_buy_lots"] > 0][:8]
    result["institutional_buy_rank_top8"] = top8
    max_net_buy_code = top8[0]["code"] if top8 else None

    # 候選名單來源二(wantgoo 量能異常 + 漲幅排行, best-effort)
    wantgoo_codes = set()
    for path in ("volume-shocker", "top-gainer"):
        wantgoo_codes |= fetch_wantgoo_codes(path)

    candidate_codes = list({p["code"] for p in top8} | wantgoo_codes)[:20]
    result["candidate_pool_size"] = len(candidate_codes)

    name_lookup = {p["code"]: p["name"] for p in parsed}
    net_buy_lookup = {p["code"]: p["net_buy_lots"] for p in parsed}

    # 族群效應:候選名單的產業別
    industry_map = fetch_industry_map(set(candidate_codes))

    # 逐檔 STOCK_DAY,算量能倍數、當日漲跌%、技術面評分、振幅/缺口/漲停/損益平衡
    focus_stocks = []
    surge_watch = []
    excluded = []

    for code in candidate_codes:
        try:
            days = fetch_stock_day(code, month_first)
        except Exception as e:  # noqa: BLE001
            excluded.append({"code": code, "reason": f"STOCK_DAY抓取失敗: {e}"})
            continue

        if len(days) < 6:
            excluded.append({"code": code, "reason": f"本月資料只有{len(days)}筆,不足以算5日均量"})
            continue

        volumes = [clean_number(d.get("成交股數")) for d in days]
        volumes = [v for v in volumes if v is not None]
        if len(volumes) < 6:
            excluded.append({"code": code, "reason": "成交股數欄位不足"})
            continue

        today_volume = volumes[-1]
        prev5_avg = sum(volumes[-6:-1]) / 5 if len(volumes) >= 6 else None
        volume_multiple = round(today_volume / prev5_avg, 2) if prev5_avg else None

        last_row = days[-1]
        open_p = clean_number(last_row.get("開盤價"))
        high_p = clean_number(last_row.get("最高價"))
        low_p = clean_number(last_row.get("最低價"))
        close = clean_number(last_row.get("收盤價"))
        change = clean_number(last_row.get("漲跌價差"))
        prev_close = (close - change) if (close is not None and change is not None) else None
        change_pct = round(change / prev_close * 100, 2) if (change is not None and prev_close) else None

        net_buy_lots = net_buy_lookup.get(code, 0)
        is_top_buyer = code == max_net_buy_code

        score, breakdown = tech_score(net_buy_lots, is_top_buyer, volume_multiple, change_pct, prev_close, close)
        diverged = (
            change_pct is not None and change_pct < 0
            and volume_multiple is not None and volume_multiple >= 1.5
            and net_buy_lots > 0
        )
        tag = score_to_tag(score, diverged)

        # 振幅(當沖操作空間參考)
        amplitude_pct = round((high_p - low_p) / prev_close * 100, 2) if (high_p is not None and low_p is not None and prev_close) else None
        if amplitude_pct is None:
            amplitude_tag = "無法確認"
        elif amplitude_pct >= 4:
            amplitude_tag = "high_range"
        elif amplitude_pct < 1.5:
            amplitude_tag = "narrow_range"
        else:
            amplitude_tag = "normal"

        # 開盤缺口
        gap_pct = round((open_p - prev_close) / prev_close * 100, 2) if (open_p is not None and prev_close) else None
        gap_tag = "large_gap" if (gap_pct is not None and abs(gap_pct) >= 2) else "normal"

        # 損益平衡提醒:當沖來回手續費+證交稅大約0.5~0.6%,漲跌幅太小扣掉成本所剩不多
        cost_breakeven_tag = "below_cost" if (change_pct is not None and abs(change_pct) < 1.0) else "normal"

        # 漲停可執行性(概略估算,實際跳動點位以交易所公告為準,這裡只做粗略提醒)
        limit_up_locked = False
        if prev_close:
            approx_limit = round(prev_close * 1.1, 2)
            if close is not None and close >= approx_limit * 0.998:
                limit_up_locked = True

        entry = {
            "code": code,
            "name": name_lookup.get(code, ""),
            "open": open_p,
            "high": high_p,
            "low": low_p,
            "close": close,
            "change": change,
            "net_buy_lots": net_buy_lots,
            "volume_multiple": volume_multiple,
            "tech_score": score,
            "tech_breakdown": breakdown,
            "tag": tag,
            "price_volume_diverged": diverged,
            "amplitude_pct": amplitude_pct,
            "amplitude_tag": amplitude_tag,
            "gap_pct": gap_pct,
            "gap_tag": gap_tag,
            "cost_breakeven_tag": cost_breakeven_tag,
            "limit_up_locked": limit_up_locked,
            "sector": industry_map.get(code, "無法查核"),
        }

        if volume_multiple is not None and volume_multiple >= 3.0 and net_buy_lots > 0:
            entry["signal_status"] = "訊號成立"
            focus_stocks.append(entry)
        elif volume_multiple is not None and volume_multiple >= 3.0 and net_buy_lots <= 0:
            entry["signal_status"] = "爆量但無法人買盤,風險較高"
            focus_stocks.append(entry)
        elif volume_multiple is not None and 1.5 <= volume_multiple < 3.0 and net_buy_lots > 0:
            entry["signal_status"] = "放量觀察"
            surge_watch.append(entry)
        else:
            entry["signal_status"] = "候選未達標"
            surge_watch.append(entry)

    # 族群效應:候選名單裡≥2檔同產業才算成立
    sector_counts = {}
    for e in focus_stocks + surge_watch:
        sec = e.get("sector")
        if sec and sec != "無法查核":
            sector_counts[sec] = sector_counts.get(sec, 0) + 1
    for e in focus_stocks + surge_watch:
        sec = e.get("sector")
        if sec in sector_counts and sector_counts[sec] >= 2:
            e["sector_cluster"] = f"族群效應成立({sec},同族群{sector_counts[sec]}檔上榜)"
        elif sec == "無法查核":
            e["sector_cluster"] = "無法查核"
        else:
            e["sector_cluster"] = "無族群效應"

    result["focus_stocks"] = focus_stocks
    result["surge_watch"] = sorted(surge_watch, key=lambda e: e.get("volume_multiple") or 0, reverse=True)[:5]
    result["excluded_incomplete_volume_data"] = excluded

    # 融資融券增減(只對爆量/放量的股票查)
    interesting_codes = {e["code"] for e in focus_stocks} | {e["code"] for e in result["surge_watch"]}
    if interesting_codes:
        try:
            margin_today = fetch_margin(date_str)
            margin_prev = {}
            for i in range(1, 6):
                prev_date = (today - timedelta(days=i)).strftime("%Y%m%d")
                margin_prev = fetch_margin(prev_date)
                if margin_prev:
                    break
            for e in focus_stocks + result["surge_watch"]:
                code = e["code"]
                today_row = margin_today.get(code)
                prev_row = margin_prev.get(code)
                if today_row and prev_row:
                    today_bal = clean_number(today_row.get("融資今日餘額") or today_row.get("今日餘額"))
                    prev_bal = clean_number(prev_row.get("融資今日餘額") or prev_row.get("今日餘額"))
                    if today_bal is not None and prev_bal is not None:
                        e["margin_change"] = today_bal - prev_bal
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 融資融券抓取失敗,略過: {e}", file=sys.stderr)

    # 連續買超天數(查最近3天T86,比對同一批候選股)
    try:
        buy_history = {code: [] for code in interesting_codes}
        for i in range(0, 3):
            d = (today - timedelta(days=i)).strftime("%Y%m%d")
            s, rows = fetch_t86(d)
            if s != "OK":
                continue
            day_net = {}
            for row in rows:
                code = str(row.get("證券代號", "")).strip().strip('="')
                nb = clean_number(row.get("三大法人買賣超股數"))
                if code in interesting_codes and nb is not None:
                    day_net[code] = nb
            for code in interesting_codes:
                if code in day_net:
                    buy_history[code].append(day_net[code] > 0)

        for e in focus_stocks + result["surge_watch"]:
            hist = buy_history.get(e["code"], [])
            if not hist:
                e["consecutive_buy_days"] = "無法確認"
                continue
            streak = 0
            for is_buy in hist:  # hist[0]是今天
                if is_buy:
                    streak += 1
                else:
                    break
            e["consecutive_buy_days"] = streak if streak > 0 else "無法確認"
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 連續買超天數計算失敗,略過: {e}", file=sys.stderr)

    # 戰績追蹤:回頭驗證前一個交易日標記過的股票,今天實際表現如何
    try:
        track_new, track_stats = compute_track_record(iso_date, month_first)
        result["track_record_new_entries"] = track_new
        result["track_record_stats"] = track_stats
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 戰績追蹤計算失敗,略過: {e}", file=sys.stderr)
        result["track_record_stats"] = {"sample_size": 0, "note": "本次執行時戰績追蹤計算失敗,不影響其他資料"}

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
