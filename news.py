#!/usr/bin/env python3
"""
爆量雷達 - 新聞分析員:公開資訊觀測站「重大訊息」(盤中 intraday.py 每 15 分鐘、收盤後 bear_signals.py 各抓一次)

來源是證交所 / 櫃買中心 OpenAPI 的官方重大訊息(上市 t187ap04_L、上櫃 mopsfin_t187ap04_O)。
這兩個來源只給「當天」的公告,所以每次抓到的都併進 data/news.json,保留最近 KEEP_DAYS 天,才看得到前幾天的消息。
只留自選股;依主旨關鍵字粗分類,方便網頁標示事件風險。不是新聞解讀,也不是買賣建議。
"""

import os
import re
from datetime import datetime, timedelta

import requests

NEWS_FILE = os.path.join("data", "news.json")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; personal-trading-dashboard/1.0)", "Accept": "application/json"}
SOURCES = (("https://openapi.twse.com.tw/v1/opendata/t187ap04_L", "公司代號"),
           ("https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap04_O", "SecuritiesCompanyCode"))
KEEP_DAYS = 7
REFRESH_MIN = 15
CATEGORIES = (   # 依序比對,第一個符合的就是分類
    ("財報營收", ("財務報告", "財報", "營收", "自結", "獲利", "盈餘", "損益")),
    ("重大交易", ("處分", "取得", "併購", "收購", "合併", "出售", "投資", "簽訂", "合約", "訂單", "背書", "資金貸與")),
    ("股利增減資", ("股利", "配息", "除權", "除息", "增資", "減資", "庫藏股", "可轉換公司債", "私募")),
    ("澄清報導", ("澄清", "媒體報導", "報載", "說明")),
    ("董事會/人事", ("董事會", "股東會", "董事", "經理人", "總經理", "財務主管", "會計主管", "發言人")),
)


def roc_to_iso(s):
    digits = re.sub(r"\D", "", str(s or ""))
    if len(digits) == 7:
        return f"{int(digits[:3]) + 1911}-{digits[3:5]}-{digits[5:]}"
    if len(digits) == 8 and digits.startswith("20"):
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
    return None


def categorize(title):
    for name, keys in CATEGORIES:
        if any(k in title for k in keys):
            return name
    return "其他"


def normalize(row, code_key):
    """官方欄位名稱有時帶空白(例如「主旨 」),先去掉空白再取值。"""
    r = {str(k).strip(): v for k, v in row.items()}
    code = str(r.get(code_key) or "").strip()
    title = re.sub(r"\s+", " ", str(r.get("主旨") or "")).strip()
    t = re.sub(r"\D", "", str(r.get("發言時間") or "")).zfill(6)
    date = roc_to_iso(r.get("發言日期"))
    if not (code and title and date):
        return None
    return {"code": code, "name": str(r.get("公司名稱") or r.get("CompanyName") or "").strip(), "date": date,
            "time": f"{t[:2]}:{t[2:4]}", "title": title[:120], "clause": str(r.get("符合條款") or "").strip(),
            "cat": categorize(title)}


def merge(existing, new_items, codes, today):
    """去重(代號+日期+時間+主旨)、只留自選股與最近 KEEP_DAYS 天,新的在前。"""
    keep_from = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    seen, out = set(), []
    for it in list(new_items) + list(existing):
        if not it or it["code"] not in codes or it["date"] < keep_from:
            continue
        key = (it["code"], it["date"], it["time"], it["title"])
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    out.sort(key=lambda x: (x["date"], x["time"]), reverse=True)
    return out


def update_news(codes, now, load, write, force=False):
    """每 REFRESH_MIN 分鐘最多抓一次。load/write 由呼叫端提供(沿用 intraday 的安全讀寫)。回傳整份 news 資料。"""
    cur = load(NEWS_FILE, {}) or {}
    try:
        last = datetime.fromisoformat(cur.get("updated")) if cur.get("updated") else None
    except ValueError:
        last = None
    if not force and last and (now - last).total_seconds() < REFRESH_MIN * 60:
        return cur
    new, status = [], {}
    for url, ck in SOURCES:
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            r.raise_for_status()
            rows = r.json() if isinstance(r.json(), list) else []
            new += [normalize(x, ck) for x in rows]
            status[url.rsplit("/", 1)[-1]] = "ok"
        except Exception:  # noqa: BLE001  單一來源失敗不影響其他
            status[url.rsplit("/", 1)[-1]] = "fail"
    items = merge(cur.get("items") or [], [x for x in new if x], set(codes), now.strftime("%Y-%m-%d"))
    out = {"updated": now.isoformat(timespec="seconds"), "status": status, "keep_days": KEEP_DAYS, "items": items,
           "source": "公開資訊觀測站重大訊息(證交所/櫃買 OpenAPI)"}
    write(NEWS_FILE, out, indent=1)
    return out


def recent_for(news, code, today, days=2):
    """某檔最近 days 天(含今天)的重大訊息,最多 3 則。"""
    since = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=days)).strftime("%Y-%m-%d")
    return [x for x in (news or {}).get("items") or [] if x["code"] == code and x["date"] >= since][:3]
