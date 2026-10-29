#!/usr/bin/env python3
"""永豐 API 功能探測(手動跑「永豐功能探測」workflow):確認指數報價、逐筆內外盤、多日 1 分K、指數 1 分K、
資料用量額度、五檔訂閱的實際格式。只讀資料,不下單。輸出會遮掉身分證字號格式的字串。"""

import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import shioaji as sj

TPE = ZoneInfo("Asia/Taipei")
api = sj.Shioaji()
api.login(api_key=os.environ["SJ_API_KEY"], secret_key=os.environ["SJ_SECRET_KEY"], subscribe_trade=False)
today = datetime.now(TPE).strftime("%Y-%m-%d")


def show(title, fn):
    t0 = time.time()
    try:
        print(f"== {title}:", fn(), f"({time.time() - t0:.1f}s)")
    except Exception as e:  # noqa: BLE001
        print(f"== {title}: 失敗 {type(e).__name__}: {str(e)[:200]}")


def usage():
    u = api.usage()
    return {k: getattr(u, k, None) for k in ("connections", "bytes", "limit_bytes", "remaining_bytes")}


show("用量", usage)
show("指數合約 TSE", lambda: [(k.code, k.name) for k in list(api.Contracts.Indexs.TSE)[:5]])
show("指數合約 OTC", lambda: [(k.code, k.name) for k in list(api.Contracts.Indexs.OTC)[:5]])


def idx_snap():
    ks = [api.Contracts.Indexs.TSE["001"], api.Contracts.Indexs.OTC["101"]]
    return [(s.code, s.close, s.open, s.high, s.low, s.change_price, s.change_rate, s.ts) for s in api.snapshots(ks)]


show("指數快照", idx_snap)


def idx_kbars():
    kb = api.kbars(api.Contracts.Indexs.TSE["001"], start=today, end=today)
    return len(kb.ts), kb.ts[:2], kb.Close[:2], kb.Volume[:2]


show("加權指數 1 分K", idx_kbars)


def stock_snap_fields():
    s = api.snapshots([api.Contracts.Stocks["2330"]])[0]
    return {k: getattr(s, k) for k in dir(s) if not k.startswith("_") and not callable(getattr(s, k))}


show("個股快照全部欄位", stock_snap_fields)
k2330 = api.Contracts.Stocks["2330"]
show("合約欄位", lambda: {k: str(getattr(k2330, k)) for k in ("category", "day_trade", "reference", "limit_up", "unit", "margin_trading_balance", "short_selling_balance")})


def ticks_today():
    tk = api.ticks(k2330, today)
    n = len(tk.ts)
    tt = {}
    for t, v in zip(tk.tick_type, tk.volume):
        tt[t] = tt.get(t, 0) + v
    return {"n": n, "fields": [f for f in dir(tk) if not f.startswith("_")][:20], "first": (tk.ts[0], tk.close[0], tk.volume[0], tk.tick_type[0]) if n else None,
            "tick_type 量": tt, "total_vol": sum(tk.volume)}


show("2330 今日逐筆", ticks_today)


def ticks_range():
    tk = api.ticks(k2330, today, query_type=sj.constant.TicksQueryType.RangeTime, time_start="13:00:00", time_end="13:10:00")
    return len(tk.ts), tk.ts[:1], tk.ts[-1:]


show("逐筆 13:00~13:10", ticks_range)


def kbars_multi():
    start = (datetime.now(TPE) - timedelta(days=45)).strftime("%Y-%m-%d")
    kb = api.kbars(k2330, start=start, end=today)
    days = {}
    for ts, v in zip(kb.ts, kb.Volume):
        d = datetime.utcfromtimestamp(ts / 1e9).strftime("%Y-%m-%d")
        days[d] = days.get(d, 0) + v
    return {"rows": len(kb.ts), "days": len(days), "first_day": min(days), "last_day": max(days), "today_vol": days.get(today)}


show("2330 近 45 天 1 分K", kbars_multi)


def kbars_old():
    kb = api.kbars(k2330, start="2025-10-28", end="2025-10-31")
    return len(kb.ts), datetime.utcfromtimestamp(kb.ts[0] / 1e9) if kb.ts else None
show("一年前 1 分K", kbars_old)
show("用量(抓完之後)", usage)


def bidask():
    got = []

    @api.on_bidask_stk_v1()
    def cb(exchange, ba):
        got.append(ba)

    api.quote.subscribe(k2330, quote_type=sj.constant.QuoteType.BidAsk, version=sj.constant.QuoteVersion.v1)
    time.sleep(5)
    api.quote.unsubscribe(k2330, quote_type=sj.constant.QuoteType.BidAsk, version=sj.constant.QuoteVersion.v1)
    if not got:
        return "5 秒內沒有收到(收盤後正常)"
    b = got[-1]
    return {"bid_price": b.bid_price, "bid_volume": b.bid_volume, "ask_price": b.ask_price, "ask_volume": b.ask_volume}


show("五檔訂閱", bidask)
api.logout()
