#!/usr/bin/env python3
"""
爆量雷達 健康檢查:直接打開各資料檔檢查,不採信任何排程或 AI 的「我做完了」。

輸出 data/health.json:
  {"checked_at", "trade_date", "ok", "items": [{"key","name","ok","detail"}]}
網頁頂端會顯示一顆狀態燈,有問題的項目點開就看得到。

盤中(intraday.py 收盤後那輪)與盤後掃描(scan.yml)結束時都會跑一次,也可以手動:
    python health.py
"""

import glob
import json
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TAIPEI = ZoneInfo("Asia/Taipei")
D = "data"
OUT = os.path.join(D, "health.json")
SCAN_SINCE = "2026-09-29"   # 盤後掃描存檔從這天開始
NEW_SINCE = "2026-10-23"    # 成交金額單位、全市場快照是 10/22 收盤後才上線,從下一個交易日起才檢查


def load(name, default=None):
    try:
        with open(os.path.join(D, name), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return default


def trading_calendar(official, trade_date):
    """用自選股日K出現過的日期當交易日曆(官方資料,不用自己維護假日表)。"""
    days = set()
    for bars in (official or {}).get("bars", {}).values():
        for b in bars or []:
            if b.get("d"):
                days.add(b["d"])
    if trade_date:
        days.add(trade_date)
    return sorted(days)


def run(now=None):
    now = now or datetime.now(TAIPEI)
    today = now.strftime("%Y-%m-%d")
    minute = now.hour * 60 + now.minute
    weekday = now.weekday() < 5
    items = []

    def add(key, name, ok, detail):
        items.append({"key": key, "name": name, "ok": bool(ok), "detail": detail})

    latest = load("intraday_latest.json", {}) or {}
    as_of = str(latest.get("data_as_of") or "")
    trade_date = as_of[:10] or None
    official = load("daily_official.json", {}) or {}
    cal = trading_calendar(official, trade_date)
    market_day_today = trade_date == today
    after_close = (not market_day_today) or minute >= 13 * 60 + 40

    # 1. 盤中報價
    if weekday and market_day_today and 9 * 60 + 5 <= minute <= 13 * 60 + 35:
        try:
            age = (now - datetime.fromisoformat(as_of)).total_seconds() / 60
        except Exception:  # noqa: BLE001
            age = 999
        add("quotes", "盤中報價", age <= 15, f"最後報價 {as_of[11:16]},距今 {age:.0f} 分鐘" + ("" if age <= 15 else "(超過15分鐘沒更新)"))
    else:
        n = sum(1 for q in latest.get("watchlist_quotes", []) if q.get("last") is not None)
        tot = len(latest.get("watchlist_quotes", []))
        add("quotes", "盤中報價", trade_date is not None and n >= tot - 2, f"{trade_date} 最後一輪 {as_of[11:16]},有報價 {n}/{tot} 檔")

    # 2. 日K
    st = str(latest.get("official_bars_status") or official.get("status") or "")
    ok_bars = st.startswith("ok")
    add("bars", "日K資料", ok_bars, st or "沒有狀態")

    # 3. 全市場爆量榜(09:30 / 10:30 / 12:00 三次)
    ms = load("market_scan.json", {}) or {}
    if trade_date:
        expect = [k for k, t in (("30", 9 * 60 + 40), ("90", 10 * 60 + 40), ("180", 12 * 60 + 10)) if (not market_day_today) or minute >= t]
        got = [k for k in expect if ms.get("date") == trade_date and k in (ms.get("scans") or {})]
        label = {"30": "09:30", "90": "10:30", "180": "12:00"}
        miss = [label[k] for k in expect if k not in got]
        add("market", "全市場爆量榜", not miss, f"{trade_date} 應有 {len(expect)} 次,缺 {'、'.join(miss)}" if miss else f"{trade_date} {len(got)} 次都有")

    # 3b. 成交金額單位(欄位200067):判斷失敗時資金流向會退回「量×價」估算
    if trade_date and trade_date >= NEW_SINCE:
        au = latest.get("amount_unit")
        add("amount", "成交金額單位", au is not None, f"單位 {au:g}" if au is not None else "判斷失敗,資金流向改用量×價估算(看 debug_fields.json)")

    # 4~6. 收盤後的產出
    if trade_date and after_close:
        rv = load("review.json", {}) or {}
        add("review", "收盤復盤", rv.get("latest") == trade_date, f"最新一份是 {rv.get('latest')}")
        arch = os.path.exists(os.path.join(D, "intraday_archive", f"{trade_date}.json"))
        add("archive", "盤中走勢存檔", arch, f"{trade_date}.json " + ("有" if arch else "不存在"))
        lb = load("lab.json", {}) or {}
        mk = load(os.path.join("mkt", f"{trade_date}.json"), {}) or {}
        nsn, has_close = len(mk.get("snaps") or {}), bool(mk.get("close"))
        if trade_date >= NEW_SINCE:
            add("mkt", "全市場快照", nsn >= 3 and has_close, f"快照 {nsn}/3 次,收盤價" + ("有" if has_close else "沒有"))
        add("lab", "策略驗證室", str(lb.get("updated", ""))[:10] == trade_date, f"最後更新 {lb.get('updated')}")

    # 7. 盤後掃描:交易日曆上每一天都要有存檔(當天的要等隔天早上才算漏)
    files = {}
    for f in glob.glob(os.path.join(D, "20*-*-*.json")):
        x = load(os.path.basename(f), {}) or {}
        if x.get("market_status") == "OK" and x.get("date"):
            files[x["date"]] = f
    cutoff = today if minute >= 8 * 60 else (now - timedelta(days=1)).strftime("%Y-%m-%d")
    need = [d for d in cal if SCAN_SINCE <= d < cutoff]
    missing = [d for d in need if d not in files]
    add("scan", "盤後掃描存檔", not missing, f"缺 {'、'.join(m[5:] for m in missing)}" if missing else f"{len(need)} 個交易日都有")

    # 8. 戰績:不重複、真的是隔一個交易日
    tr = load("track_record.json", []) or []
    keys = [(r.get("signal_date"), r.get("code")) for r in tr]
    dup = len(keys) - len(set(keys))
    idx = {d: i for i, d in enumerate(cal)}
    bad = [r for r in tr if r.get("signal_date") in idx and r.get("checked_date") in idx
           and idx[r["checked_date"]] != idx[r["signal_date"]] + 1]
    sc = load("latest.json", {}) or {}
    shown = (sc.get("track_record_stats") or {}).get("sample_size")
    real = len([r for r in tr if r.get("close_return_pct") is not None][-60:])
    problems = []
    if dup:
        problems.append(f"{dup} 筆重複")
    if bad:
        problems.append(f"{len(bad)} 筆不是隔日")
    if shown is not None and shown != real:
        problems.append(f"網頁顯示 {shown} 筆,實際 {real} 筆")
    add("track", "隔日戰績", not problems, "、".join(problems) if problems else f"{len(tr)} 筆,無重複、皆為隔日")

    out = {
        "checked_at": now.isoformat(timespec="seconds"),
        "trade_date": trade_date,
        "ok": all(i["ok"] for i in items),
        "items": items,
    }
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    return out


if __name__ == "__main__":
    r = run()
    for i in r["items"]:
        print(("✓" if i["ok"] else "✗"), i["name"], i["detail"])
