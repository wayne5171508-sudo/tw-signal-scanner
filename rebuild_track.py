#!/usr/bin/env python3
"""
從 data/YYYY-MM-DD.json 全部存檔,重新建立 data/track_record.json。

每天的盤後掃描只會回頭檢查「上一份存檔」,中間漏跑或事後回補的日子就接不起來。
這支程式把所有存檔依資料日期排好,每一對相鄰存檔各檢查一次:
前一份標記的股票,在下一個交易日(必須真的相鄰,中間隔了交易日就不算)的開高低收。
結果去重後整份覆寫,並把統計同步寫回 data/latest.json 的 track_record_stats。

用法: python rebuild_track.py
"""

import glob
import json
import os
import sys
import time

import scan

FLAG_STATUSES = ("訊號成立", "放量觀察", "爆量但無法人買盤,風險較高")
_cache = {}


def month_rows(code, ym):
    key = (code, ym)
    if key not in _cache:
        try:
            _cache[key] = scan.fetch_stock_day(code, ym + "01")
        except Exception as e:  # noqa: BLE001
            print(f"[warn] {code} {ym} 抓取失敗: {e}", file=sys.stderr)
            _cache[key] = None
        time.sleep(0.5)
    return _cache[key] or []


def load_snapshots():
    snaps = []
    for f in glob.glob(os.path.join(scan.DATA_DIR, "20*-*-*.json")):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if d.get("market_status") == "OK" and d.get("date"):
            snaps.append(d)
    snaps.sort(key=lambda d: d["date"])
    return snaps


def check_pair(prev, cur_date):
    out = []
    sig_date = prev["date"]
    for e in prev.get("focus_stocks", []) + prev.get("surge_watch", []):
        if e.get("signal_status") not in FLAG_STATUSES or not e.get("code") or e.get("close") is None:
            continue
        code = e["code"]
        months = sorted({sig_date[:7].replace("-", ""), cur_date[:7].replace("-", "")})
        days = []
        for ym in months:
            days += month_rows(code, ym)
        dates = [scan.roc_to_iso(d.get("日期")) for d in days]
        if sig_date not in dates or cur_date not in dates:
            continue
        if dates.index(cur_date) != dates.index(sig_date) + 1:
            continue
        row = days[dates.index(cur_date)]
        sc = e["close"]
        o, h, l, c = (scan.clean_number(row.get(k)) for k in ("開盤價", "最高價", "最低價", "收盤價"))
        if c is None:
            continue

        def pct(p):
            return round((p - sc) / sc * 100, 2) if p is not None else None

        out.append({
            "signal_date": sig_date, "checked_date": cur_date, "code": code, "name": e.get("name"),
            "signal_status": e.get("signal_status"), "tech_score": e.get("tech_score"),
            "signal_close": sc, "next_open": o, "next_high": h, "next_low": l, "next_close": c,
            "overnight_return_pct": pct(o), "day_high_return_pct": pct(h), "close_return_pct": pct(c),
            "from_backfill": bool(prev.get("backfill")),
        })
    return out


def main():
    snaps = load_snapshots()
    history, seen = [], set()
    for prev, cur in zip(snaps, snaps[1:]):
        for r in check_pair(prev, cur["date"]):
            k = (r["signal_date"], r["code"])
            if k not in seen:
                seen.add(k)
                history.append(r)
        print(f"{prev['date']} → {cur['date']}: 累計 {len(history)} 筆", file=sys.stderr)

    with open(scan.TRACK_RECORD_FILE, "w", encoding="utf-8") as fh:
        json.dump(history, fh, ensure_ascii=False, indent=2)

    stats = scan.track_stats(history)
    latest = os.path.join(scan.DATA_DIR, "latest.json")
    if os.path.exists(latest):
        d = json.load(open(latest, encoding="utf-8"))
        d["track_record_stats"] = stats
        with open(latest, "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False, indent=2)
    print(json.dumps(stats, ensure_ascii=False))


if __name__ == "__main__":
    main()
