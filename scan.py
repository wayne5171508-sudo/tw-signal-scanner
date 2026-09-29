#!/usr/bin/env python3
"""
爆量雷達 - 收盤後籌碼掃描 (real-code 版本)

直接呼叫台灣證交所(TWSE)官方 JSON API 抓資料,取代原本靠 Claude 的 WebFetch
(小模型讀網頁摘要)去讀大表格的做法 —— 那個做法在資料筆數大時會不穩定漏行,
這支程式用真正的 JSON 解析,不會有這個問題。

這支程式只負責「抓資料 + 算數字」,輸出一份乾淨的 JSON。
新聞面查核、多空判讀文字撰寫、寫入 Artifact 資料庫、推播通知,
還是交給 Claude 的排程去做(那些需要語言理解,程式做不到)。

用法:
    python scan.py > output.json

需要的套件:見 requirements.txt
"""

import json
import re
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

TAIPEI = ZoneInfo("Asia/Taipei")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; personal-trading-dashboard/1.0)"}
TIMEOUT = 20


# ---------- 基本工具 ----------

def taipei_now():
    return datetime.now(TAIPEI)


def is_plain_stock_code(code):
    """4碼純數字、不是00開頭(排除ETF/權證常見前綴)"""
    return isinstance(code, str) and len(code) == 4 and code.isdigit() and not code.startswith("00")


def clean_number(s):
    """把 '1,234' / '=\"1234\"' / '+12' 這類字串轉成 int,轉不了回傳 None"""
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


# ---------- 主流程 ----------

def main():
    today = taipei_now()
    date_str = today.strftime("%Y%m%d")
    iso_date = today.strftime("%Y-%m-%d")
    month_first = today.strftime("%Y%m") + "01"

    result = {
        "date": iso_date,
        "generated_at": today.isoformat(),
        "pipeline_version": "v10-realcode",
        "scope_decisions": [
            "這份JSON由GitHub Actions上的Python直接呼叫TWSE官方API算出,不經過Claude的WebFetch,"
            "所以不會有大表格漏行的問題。新聞面查核跟最終文字撰寫仍由Claude排程完成。",
        ],
    }

    # 步驟2: T86
    stat, t86_rows = fetch_t86(date_str)
    if stat != "OK":
        result["market_status"] = "data_not_ready_or_no_trading_day"
        result["t86_stat_raw"] = stat
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    result["market_status"] = "OK"

    # 步驟3: 候選名單來源一(法人買超前8, 4碼純數字, 排除00開頭)
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

    # 步驟4: 候選名單來源二(wantgoo 量能異常 + 漲幅排行, best-effort)
    wantgoo_codes = set()
    for path in ("volume-shocker", "top-gainer"):
        wantgoo_codes |= fetch_wantgoo_codes(path)

    candidate_codes = list({p["code"] for p in top8} | wantgoo_codes)[:20]
    result["candidate_pool_size"] = len(candidate_codes)

    name_lookup = {p["code"]: p["name"] for p in parsed}
    net_buy_lookup = {p["code"]: p["net_buy_lots"] for p in parsed}

    # 步驟5-6: 逐檔 STOCK_DAY,算量能倍數、當日漲跌%、技術面評分
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

        entry = {
            "code": code,
            "name": name_lookup.get(code, ""),
            "close": close,
            "change": change,
            "net_buy_lots": net_buy_lots,
            "volume_multiple": volume_multiple,
            "tech_score": score,
            "tech_breakdown": breakdown,
            "tag": tag,
            "price_volume_diverged": diverged,
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

    result["focus_stocks"] = [e for e in focus_stocks]
    result["surge_watch"] = sorted(surge_watch, key=lambda e: e.get("volume_multiple") or 0, reverse=True)[:5]
    result["excluded_incomplete_volume_data"] = excluded

    # 步驟7: 融資融券增減(只對爆量/放量的股票查)
    interesting_codes = {e["code"] for e in focus_stocks} | {e["code"] for e in result["surge_watch"]}
    if interesting_codes:
        try:
            margin_today = fetch_margin(date_str)
            yesterday = today - timedelta(days=1)
            # 往前找最近一個有資料的交易日(最多往前找5天)
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

    # 步驟8: 連續買超天數(查最近3天T86,比對同一批候選股)
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

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
