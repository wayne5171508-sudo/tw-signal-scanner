#!/usr/bin/env python3
"""
爆量雷達 - 當沖資格與交易限制(每個交易日盤前由 intraday.py 抓一次,存 data/trade_flags.json)

來源都是證交所 / 櫃買中心 OpenAPI 的官方資料:
- 現股當沖標的與「暫停先賣後買」註記:上市 TWTB4U、上櫃 tpex_securities
- 處置股(期間內有分盤撮合、預收款券等限制):上市 announcement/punish、上櫃 tpex_disposal_information
- 注意股(當天公布):上市 announcement/notice、上櫃 tpex_trading_warning_information
- 注意累計次數可能達處置標準(明天可能被處置):上市 announcement/notetrans、上櫃 tpex_trading_warning_note
- 變更交易(分盤集合競價):上市 exchangeReport/TWT85U

任一來源失敗只會讓那一項標「未知」,不影響其他項目,也不影響盤中報價。
這些是交易規則上的限制提醒,不是買賣建議;實際以券商下單系統為準。
"""

import re
from datetime import datetime

import requests

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; personal-trading-dashboard/1.0)", "Accept": "application/json"}
TIMEOUT = 20
TWSE = "https://openapi.twse.com.tw/v1"
TPEX = "https://www.tpex.org.tw/openapi/v1"
SOURCES = {
    "twse_daytrade": f"{TWSE}/exchangeReport/TWTB4U",
    "tpex_daytrade": f"{TPEX}/tpex_securities",
    "twse_punish": f"{TWSE}/announcement/punish",
    "tpex_punish": f"{TPEX}/tpex_disposal_information",
    "twse_notice": f"{TWSE}/announcement/notice",
    "tpex_notice": f"{TPEX}/tpex_trading_warning_information",
    "twse_accum": f"{TWSE}/announcement/notetrans",
    "tpex_accum": f"{TPEX}/tpex_trading_warning_note",
    "twse_change": f"{TWSE}/exchangeReport/TWT85U",
}


def roc_to_iso(s):
    """'1151027'、'115/10/27'、'20261027' → '2026-10-27';看不懂回傳 None。"""
    digits = re.sub(r"\D", "", str(s or ""))
    if len(digits) == 8 and digits.startswith("20"):
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
    if len(digits) == 7:
        return f"{int(digits[:3]) + 1911}-{digits[3:5]}-{digits[5:]}"
    return None


def period_range(s):
    """處置期間 '115/10/21～115/10/28' 或 '1151022~1151029' → ('2026-10-21', '2026-10-28')。"""
    parts = [p for p in re.split(r"[~～至\-]", str(s or "")) if re.sub(r"\D", "", p)]
    if len(parts) < 2:
        return None, None
    return roc_to_iso(parts[0]), roc_to_iso(parts[-1])


def summarize_measure(text):
    """處置內容整理成當沖最需要知道的兩件事:每幾分鐘撮合一次、要不要預收款券。"""
    t = str(text or "")
    out = []
    m = re.search(r"每\s*(\d+|[一二三四五六七八九十]+)\s*分鐘", t)
    if m:
        n = m.group(1)
        if not n.isdigit():   # 證交所用國字:五、十、二十
            d = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
            n = str((d.get(n[0], 1) if n[0] != "十" else 1) * (10 if "十" in n else 1) + (d.get(n[-1], 0) if len(n) > 1 and n[-1] != "十" else 0)) if "十" in n else str(d.get(n, 0))
        out.append(f"每{n}分鐘撮合")
    elif "人工管制" in t:
        out.append("人工管制撮合")
    if "收取全部" in t or "預收" in t:
        out.append("預收款券")
    return "・".join(out)


def _get(url):
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def _code(row, *keys):
    for k in keys:
        v = str(row.get(k) or "").strip()
        if v:
            return v
    return ""


def build_flags(raw, codes, today):
    """raw={來源名: list 或 None(抓失敗)}。回傳 {code: {...}} 與每個來源的狀態。"""
    status = {k: ("ok" if v is not None else "fail") for k, v in raw.items()}
    flags = {c: {} for c in codes}

    # 當沖標的:兩個市場的名單合起來;名單本身抓失敗時不能說「不可當沖」,標成未知
    dt_rows = []
    for k, ck, sk in (("twse_daytrade", "Code", "Suspension"), ("tpex_daytrade", "證券代號", "暫停現股賣出後現款買進當沖註記")):
        for r in raw.get(k) or []:
            dt_rows.append((_code(r, ck), str(r.get(sk) or "").strip().upper() == "Y", k))
    dt = {c: s for c, s, _ in dt_rows if c}
    dt_known = raw.get("twse_daytrade") is not None and raw.get("tpex_daytrade") is not None
    dt_date = None
    for k, dk in (("twse_daytrade", "Date"), ("tpex_daytrade", "資料日期")):
        rows = raw.get(k) or []
        if rows:
            dt_date = dt_date or roc_to_iso(rows[0].get(dk))
    for c in codes:
        if c in dt:
            flags[c]["daytrade"] = True
            if dt[c]:
                flags[c]["no_sell_first"] = True
        elif dt_known:
            flags[c]["daytrade"] = False

    # 處置:今天落在處置期間內才算
    for k, ck, pk, mks in (("twse_punish", "Code", "DispositionPeriod", ("DispositionMeasures", "Detail")),
                           ("tpex_punish", "SecuritiesCompanyCode", "DispositionPeriod", ("DisposalCondition",))):
        for r in raw.get(k) or []:
            c = _code(r, ck)
            if c not in flags:
                continue
            start, end = period_range(r.get(pk))
            if start and end and start <= today <= end:
                measure = summarize_measure(" ".join(str(r.get(x) or "") for x in mks))
                flags[c]["disposal"] = {"start": start, "end": end, "measure": measure}

    # 注意股:公布日期要是最近一個交易日(證交所欄位 Date、櫃買 Date)
    for k, ck, rk in (("twse_notice", "Code", "TradingInfoForAttention"), ("tpex_notice", "SecuritiesCompanyCode", "TradingInformation")):
        for r in raw.get(k) or []:
            c = _code(r, ck)
            if c in flags:
                flags[c]["notice"] = {"date": roc_to_iso(r.get("Date")), "reason": str(r.get(rk) or "").strip()[:80]}

    # 注意累計次數可能達處置標準
    for k, ck, rk in (("twse_accum", "Code", "RecentlyMetAttentionSecuritiesCriteria"), ("tpex_accum", "SecuritiesCompanyCode", "AccumulationSituation")):
        for r in raw.get(k) or []:
            c = _code(r, ck)
            if c in flags:
                flags[c]["near_disposal"] = str(r.get(rk) or "").strip()[:60]

    # 變更交易(分盤集合競價)
    for r in raw.get("twse_change") or []:
        c = _code(r, "Code")
        if c in flags:
            flags[c]["change_trading"] = True

    return {c: f for c, f in flags.items() if f}, status, dt_date


def fetch_flags(codes, today):
    raw = {}
    for k, url in SOURCES.items():
        try:
            raw[k] = _get(url)
        except Exception:  # noqa: BLE001  單一來源失敗只標未知
            raw[k] = None
    flags, status, dt_date = build_flags(raw, codes, today)
    return {"date": today, "fetched_at": datetime.now().isoformat(timespec="seconds"), "daytrade_list_date": dt_date,
            "status": status, "flags": flags,
            "note": "官方交易限制提醒(證交所/櫃買 OpenAPI),實際以券商下單系統為準,不是買賣建議。"}
