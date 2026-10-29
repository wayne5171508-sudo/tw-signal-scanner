#!/usr/bin/env python3
"""
永豐 Shioaji 即時報價(盤中報價的主來源)

- 金鑰從環境變數 SJ_API_KEY / SJ_SECRET_KEY 讀(GitHub Secrets),程式碼和 repo 裡沒有任何金鑰。
- 回傳格式刻意做成跟鉅亨(cnyes)報價一樣的欄位代碼,intraday.py 其他計算完全不用改。
- 任何狀況(沒設金鑰、登入失敗、某檔查不到、還沒有成交)都只回傳「抓得到的那些」,
  其餘由 intraday.py 自動改用鉅亨補,不會讓整輪報價失敗。

欄位對照(cnyes 代碼 → 意思):
  "0" 完整代碼  "200010" 股票代號  "200009" 名稱  "6" 成交價  "11" 漲跌  "21" 昨收(參考價)
  "56" 漲跌幅%  "19" 開  "12" 高  "13" 低  "200013" 累積量(張)  "200067" 累積成交金額(元)
  "75" 漲停價  "76" 跌停價  "200007" 最新成交時間(epoch 秒)
"""

import atexit
import os
import time
import warnings
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

SNAP_BATCH = 500          # 永豐一次快照最多 500 檔
KBAR_GAP = 0.12           # 每檔 1 分K 查詢之間停多久(永豐資料查詢有頻率上限,約每 5 秒 50 次)
TAIPEI = ZoneInfo("Asia/Taipei")
_api = None
_next_try = 0.0           # 登入失敗後,這個時間(epoch 秒)之前不再重試
_fails = 0                # 連續幾次查詢失敗;到 RELOGIN_FAILS 就當作連線斷了,下次重新登入
RELOGIN_GAP = 120         # 登入失敗後隔幾秒再試(盤中常駐迴圈一整天只用同一個連線)
RELOGIN_FAILS = 2
QUOTA_STOP_MB = 25        # 今天的資料額度剩不到這麼多就停用永豐到明天(改用鉅亨),不然查詢一直失敗、一直重新登入
QUOTA_CHECK_GAP = 60      # 幾秒查一次額度
_usage_at = 0.0
STATS = {"login": None, "rows": 0, "asked": 0, "seconds": 0.0}


def _log(msg):
    print(f"[永豐] {msg}")


def _login():
    """登入一次後一直沿用(盤中常駐迴圈 loop.py 一整天同一個連線)。
    失敗就交給鉅亨,RELOGIN_GAP 秒後才再試;查詢連續失敗(_broken)也會在下一次重新登入。"""
    global _api, _next_try, _usage_at
    if _api is not None and time.time() - _usage_at > QUOTA_CHECK_GAP:
        _usage_at = time.time()
        try:
            rem = _api.usage().remaining_bytes / 1e6
            STATS["quota_mb"] = round(rem, 1)
            if rem < QUOTA_STOP_MB:
                _log(f"今天的資料額度只剩 {rem:.0f} MB,停用永豐到明天,改用鉅亨")
                _drop()
                tomorrow = (datetime.now(TAIPEI) + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
                _next_try = tomorrow.timestamp()
                return None
        except Exception:  # noqa: BLE001  查不到額度不影響使用
            pass
    if _api is not None or time.time() < _next_try:
        return _api
    key, sec = os.environ.get("SJ_API_KEY"), os.environ.get("SJ_SECRET_KEY")
    if not (key and sec):
        STATS["login"] = "no_key"
        _next_try = float("inf")
        return None
    t0 = time.time()
    try:
        warnings.filterwarnings("ignore", category=DeprecationWarning)
        import shioaji as sj
        api = sj.Shioaji()
        api.login(api_key=key, secret_key=sec, subscribe_trade=False)
        _api = api
        STATS["login"] = "ok"
        atexit.register(_logout)
        _log(f"登入成功({time.time() - t0:.1f} 秒)")
    except Exception as e:  # noqa: BLE001  登入失敗就整輪用鉅亨
        STATS["login"] = "fail"
        _next_try = time.time() + RELOGIN_GAP
        _log(f"登入失敗,先改用鉅亨,{RELOGIN_GAP} 秒後再試: {type(e).__name__}: {str(e)[:150]}")
    return _api


def _ok_call():
    global _fails
    _fails = 0


def _drop():
    global _api, _fails
    old, _api, _fails = _api, None, 0
    try:
        if old is not None:
            old.logout()
    except Exception:  # noqa: BLE001
        pass


def _broken():
    """查詢失敗一次記一次;連續 RELOGIN_FAILS 次就丟掉這個連線,下一次 _login() 重新登入(至少隔 RELOGIN_GAP 秒,不會每輪狂登入)。"""
    global _fails, _next_try
    _fails += 1
    if _fails >= RELOGIN_FAILS and _api is not None:
        _log(f"連續 {_fails} 次查詢失敗,{RELOGIN_GAP} 秒後重新登入")
        _drop()
        _next_try = time.time() + RELOGIN_GAP


def _logout():
    try:
        if _api is not None:
            _api.logout()
    except Exception:  # noqa: BLE001
        pass


def _contract(api, code):
    try:
        return api.Contracts.Stocks[code]
    except Exception:  # noqa: BLE001  查不到(例如興櫃)就回 None
        return None


def _num(x):
    return isinstance(x, (int, float)) and x == x and x not in (float("inf"), float("-inf"))


def _epoch(ts_ns):
    """快照時間是奈秒。永豐的時間戳是「台北時間當成 UTC」寫的,換算後會比現在快 8 小時,
    這裡用「比現在晚超過 1 小時」自動判斷並扣回 8 小時,不寫死,將來官方改了也不會錯。"""
    if not _num(ts_ns) or ts_ns <= 0:
        return None
    t = ts_ns / 1e9
    if t - time.time() > 3600:
        t -= 8 * 3600
    return int(t)


def _to_row(s, k):
    code = getattr(s, "code", None) or getattr(k, "code", None)
    last = getattr(s, "close", None)
    vol = getattr(s, "total_volume", None)
    if not code or not _num(last) or last <= 0 or not _num(vol) or vol <= 0:
        return None   # 今天還沒成交(開盤前、整天沒量),讓鉅亨補,避免拿到 0 或舊價
    amt = getattr(s, "total_amount", None)
    if _num(amt) and amt > 0:
        # 興櫃等以「股」為單位的商品:金額 ÷ (量×1000×價) 會接近 0.001,把量換回「張」
        r = amt / (vol * 1000 * last)
        if 0.0005 < r < 0.002:
            vol = max(1, round(vol / 1000))
    ref = getattr(k, "reference", None)
    chg_api = getattr(s, "change_price", None)
    if _num(ref) and ref > 0:
        prev = float(ref)
    elif _num(chg_api):
        prev = round(last - chg_api, 4)
    else:
        prev = None
    change = round(last - prev, 4) if prev else None
    pct = round(change / prev * 100, 2) if prev else None
    up, dn = getattr(k, "limit_up", None), getattr(k, "limit_down", None)
    return {
        "0": f"TWS:{code}:STOCK",
        "200010": code,
        "200009": getattr(k, "name", None),
        "6": float(last),
        "11": change,
        "21": prev,
        "56": pct,
        "19": getattr(s, "open", None),
        "12": getattr(s, "high", None),
        "13": getattr(s, "low", None),
        "200013": int(vol),
        "200067": amt if _num(amt) and amt > 0 else None,
        "75": up if _num(up) and up > 0 else None,
        "76": dn if _num(dn) and dn > 0 else None,
        "200025": None,
        "200007": _epoch(getattr(s, "ts", None)),
        "src": "sj",
        "cat": getattr(k, "category", None),                                        # 產業別代碼(例 24 = 半導體)
        "dt": str(getattr(getattr(k, "day_trade", None), "value", getattr(k, "day_trade", "")) or ""),   # 現股當沖:Yes / OnlyBuy / No
    }


def stock_rows(codes):
    """codes: 股票代號清單(例 ["2330", "0050"])。回傳 {代號: 鉅亨格式的報價列},抓不到的不放。"""
    api = _login()
    if api is None or not codes:
        return {}
    t0 = time.time()
    contracts = []
    for c in codes:
        k = _contract(api, c)
        if k is not None:
            contracts.append(k)
    out = {}
    for i in range(0, len(contracts), SNAP_BATCH):
        batch = contracts[i:i + SNAP_BATCH]
        snaps = None
        for attempt in range(2):
            try:
                snaps = api.snapshots(batch)
                _ok_call()
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 1:
                    _log(f"快照失敗({len(batch)} 檔),改用鉅亨: {type(e).__name__}: {str(e)[:120]}")
                    _broken()
                else:
                    time.sleep(1)
        if _api is None:
            break
        if not snaps:
            continue
        by_code = {k.code: k for k in batch}
        for s in snaps:
            k = by_code.get(getattr(s, "code", None))
            if k is None:
                continue
            row = _to_row(s, k)
            if row:
                out[row["200010"]] = row
    STATS["asked"] += len(codes)
    STATS["rows"] += len(out)
    STATS["seconds"] += round(time.time() - t0, 1)
    return out


INDEX_CODES = {"IX0001": "TWS:TSE01:INDEX", "IX0043": "TWS:OTC01:INDEX"}   # 永豐指數代碼 → 鉅亨代碼(加權、櫃買)


def index_rows():
    """加權、櫃買指數快照,格式同鉅亨:{鉅亨代碼: 報價列}。抓不到回傳 {},由鉅亨補。"""
    api = _login()
    if api is None:
        return {}
    try:
        ks = [k for k in list(api.Contracts.Indexs.TSE) + list(api.Contracts.Indexs.OTC) if k.code in INDEX_CODES]
        snaps = api.snapshots(ks)
        _ok_call()
    except Exception as e:  # noqa: BLE001
        _log(f"指數快照失敗,改用鉅亨: {type(e).__name__}: {str(e)[:120]}")
        _broken()
        return {}
    out = {}
    for s in snaps:
        cc, last, chg = INDEX_CODES.get(getattr(s, "code", None)), getattr(s, "close", None), getattr(s, "change_price", None)
        if not cc or not _num(last) or last <= 0:
            continue
        prev = round(last - chg, 2) if _num(chg) else None
        out[cc] = {"0": cc, "6": float(last), "11": chg, "21": prev, "56": getattr(s, "change_rate", None),
                   "19": getattr(s, "open", None), "12": getattr(s, "high", None), "13": getattr(s, "low", None),
                   "200007": _epoch(getattr(s, "ts", None)), "src": "sj"}
    return out


BIG_AMOUNT = 5_000_000      # 單筆成交金額 >= 500 萬元算大單
TICK_GAP = 0.12             # 每檔逐筆查詢之間停多久(和 1 分K 共用查詢頻率上限)
BOOK_STALE = 120            # 五檔超過幾秒沒更新就不用
_BOOK = {}                  # 五檔訂閱收到的最新一筆:{代號: (收到時間, 委買 5 檔量合計, 委賣 5 檔量合計, 買一價, 賣一價)}
_SUBS = set()
_SUB_API = None
_SEEN = {}                  # 五檔:每檔最後一次被要求的時間;超過 BOOK_KEEP 秒沒人要才退訂
BOOK_KEEP = 600


def _wall_hms(ts_ns):
    """逐筆時間戳 → 台北時間 "HH:MM:SS"(時間戳寫法判斷同 _wall_minute)。"""
    wm = _wall_minute(ts_ns)
    if not wm:
        return None
    sec = int(float(ts_ns) / 1e9) % 60
    return f"{wm[1] // 60:02d}:{wm[1] % 60:02d}:{sec:02d}"


def tick_flow(codes, day, state, now_hms, budget=20.0):
    """逐筆成交的內外盤(主動買賣力道),每檔只抓上次之後的新逐筆,累積在 state(呼叫端負責存檔):
      state[代號] = {"ts": 已抓到的最後一筆時間戳, "out": 外盤量, "in": 內盤量, "bo": 大單外盤量, "bi": 大單內盤量,
                    "m": {"HH:MM"(每 5 分鐘): [外盤量, 內盤量]}}
    tick_type 1 = 外盤(成交在賣價,買方主動)、2 = 內盤(成交在買價,賣方主動)、0 = 無法判斷(不計)。超過 budget 秒的下一輪再抓。"""
    api = _login()
    if api is None or not codes:
        return state
    import shioaji as sj
    t0 = time.time()
    for code in codes:
        if time.time() - t0 > budget:
            break
        k = _contract(api, code)
        if k is None:
            continue
        st = state.setdefault(code, {"ts": 0, "out": 0, "in": 0, "bo": 0, "bi": 0, "m": {}})
        start = _wall_hms(st["ts"]) if st["ts"] else "09:00:00"
        try:
            tk = api.ticks(contract=k, date=day, query_type=sj.constant.TicksQueryType.RangeTime, time_start=start, time_end=now_hms)
            _ok_call()
        except Exception as e:  # noqa: BLE001
            _log(f"{code} 逐筆失敗: {type(e).__name__}: {str(e)[:100]}")
            _broken()
            if _api is None:
                break
            continue
        for ts, px, v, tt in zip(tk.ts, tk.close, tk.volume, tk.tick_type):
            if ts <= st["ts"] or tt not in (1, 2):
                st["ts"] = max(st["ts"], ts)
                continue
            st["ts"] = ts
            side = "out" if tt == 1 else "in"
            st[side] += v
            if px * v * 1000 >= BIG_AMOUNT:
                st["bo" if tt == 1 else "bi"] += v
            hm = _wall_minute(ts)
            if hm:
                b5 = hm[1] // 5 * 5
                b = st["m"].setdefault(f"{b5 // 60:02d}:{b5 % 60:02d}", [0, 0])
                b[0 if tt == 1 else 1] += v
        time.sleep(TICK_GAP)
    STATS["seconds"] += round(time.time() - t0, 1)
    return state


def flow_metrics(st, now_min):
    """state 一檔 → {"out": 全天外盤比 %, "out15": 最近 15 分鐘外盤比 %, "big": 大單淨買比 %(-100~100), "n15": 最近 15 分鐘內外盤量}。"""
    if not st:
        return None
    tot = st["out"] + st["in"]
    recent = [v for k, v in st["m"].items() if int(k[:2]) * 60 + int(k[3:]) >= now_min - 15]
    o15, i15 = sum(x[0] for x in recent), sum(x[1] for x in recent)
    big = st["bo"] + st["bi"]
    return {"out": round(st["out"] / tot * 100, 1) if tot else None, "out15": round(o15 / (o15 + i15) * 100, 1) if o15 + i15 else None,
            "n15": o15 + i15, "big": round((st["bo"] - st["bi"]) / big * 100) if big else None, "big_vol": big}


def _on_bidask(exchange, ba):
    try:
        _BOOK[ba.code] = (time.time(), sum(ba.bid_volume[:5]), sum(ba.ask_volume[:5]), ba.bid_price[0], ba.ask_price[0])
    except Exception:  # noqa: BLE001  回呼在永豐的執行緒裡,任何錯誤都不能往外丟
        pass


def watch_books(codes):
    """五檔即時訂閱(盤中常駐迴圈才有用,因為要一直開著連線收推播):訂閱 codes 裡還沒訂的;
    退訂超過 BOOK_KEEP 秒沒被要求的(同一輪自選股、全市場各呼叫一次,不能互相退訂)。
    回傳 {代號: {"imb": 委買委賣失衡 %(+ 委買多)、"bid","ask": 5 檔量合計, "age": 幾秒前}};還沒收到的不放。"""
    global _SUB_API
    api = _login()
    if api is None:
        return {}
    import shioaji as sj
    if _SUB_API is not api:                  # 第一次或重新登入過:訂閱要重來
        _SUBS.clear()
        _BOOK.clear()
        try:
            api.quote.set_on_bidask_stk_v1_callback(_on_bidask)
        except Exception as e:  # noqa: BLE001
            _log(f"五檔回呼設定失敗: {type(e).__name__}: {str(e)[:100]}")
            return {}
        _SUB_API = api
    want, now0 = set(codes), time.time()
    for code in want:
        _SEEN[code] = now0
    for code in sorted(want - _SUBS):
        k = _contract(api, code)
        if k is None:
            continue
        try:
            api.quote.subscribe(k, quote_type=sj.constant.QuoteType.BidAsk, version=sj.constant.QuoteVersion.v1)
            _SUBS.add(code)
        except Exception as e:  # noqa: BLE001
            _log(f"{code} 五檔訂閱失敗: {type(e).__name__}: {str(e)[:100]}")
    for code in sorted(c for c in _SUBS if now0 - _SEEN.get(c, 0) > BOOK_KEEP):
        k = _contract(api, code)
        try:
            if k is not None:
                api.quote.unsubscribe(k, quote_type=sj.constant.QuoteType.BidAsk, version=sj.constant.QuoteVersion.v1)
        except Exception:  # noqa: BLE001
            pass
        _SUBS.discard(code)
        _BOOK.pop(code, None)
    now, out = time.time(), {}
    for code in want:
        b = _BOOK.get(code)
        if b and now - b[0] <= BOOK_STALE and b[1] + b[2] > 0:
            out[code] = {"imb": round((b[1] - b[2]) / (b[1] + b[2]) * 100), "bid": b[1], "ask": b[2], "age": round(now - b[0])}
    return out


def _wall_minute(ts_ns):
    """1 分K 時間戳(奈秒)→ (台北日期, 台北時間從 00:00 起第幾分鐘)。
    永豐的時間戳是「台北時間當成 UTC」寫的;台股盤中 09:00-13:30 換成真正的 UTC 是 01:00-05:30,
    兩段不重疊,所以直接看 UTC 時鐘落在哪一段就知道是哪一種寫法,將來官方改了也不會錯。"""
    try:
        t = float(ts_ns) / 1e9
    except (TypeError, ValueError):
        return None
    if not _num(t) or t <= 0:
        return None
    u = datetime.fromtimestamp(t, timezone.utc)
    if not 9 * 60 <= u.hour * 60 + u.minute <= 13 * 60 + 35:
        u = datetime.fromtimestamp(t, TAIPEI)
    return u.strftime("%Y-%m-%d"), u.hour * 60 + u.minute


def kbars_1m(codes, day, budget=40.0):
    """今天(day="YYYY-MM-DD")的 1 分K:{代號: [[第幾分鐘, 開, 高, 低, 收, 量], ...]}。
    一檔一次查詢;超過 budget 秒就停,剩下的下一輪再抓。抓不到的不放。"""
    api = _login()
    if api is None or not codes:
        return {}
    t0, out = time.time(), {}
    for code in codes:
        if time.time() - t0 > budget:
            _log(f"1 分K 超過 {budget:.0f} 秒預算,抓了 {len(out)}/{len(codes)} 檔,其餘下一輪")
            break
        k = _contract(api, code)
        if k is None:
            continue
        try:
            kb = api.kbars(contract=k, start=day, end=day)
            _ok_call()
        except Exception as e:  # noqa: BLE001  單檔失敗不影響其他檔
            _log(f"{code} 1 分K 失敗: {type(e).__name__}: {str(e)[:100]}")
            _broken()
            if _api is None:
                break
            time.sleep(KBAR_GAP)
            continue
        rows = []
        for ts, o, h, l, c, v in zip(*(getattr(kb, f, None) or [] for f in ("ts", "Open", "High", "Low", "Close", "Volume"))):
            wm = _wall_minute(ts)
            if wm and wm[0] == day and all(_num(x) for x in (o, h, l, c, v)) and c > 0:
                rows.append([wm[1], float(o), float(h), float(l), float(c), float(v)])
        if rows:
            out[code] = sorted(rows)
        time.sleep(KBAR_GAP)
    STATS["kbars"] = STATS.get("kbars", 0) + len(out)
    STATS["seconds"] += round(time.time() - t0, 1)
    return out


def guess_end_label(rows_by_code):
    """1 分K 的時間是「這一分鐘結束」(09:01 = 09:00~09:01)還是「開始」(09:00 = 09:00~09:01)?
    任何一檔有 09:00 那一根就是「開始」寫法;都沒有就是「結束」寫法(大型股開盤第一分鐘一定有成交)。"""
    return not any(r[0] == 9 * 60 for rows in rows_by_code.values() for r in rows)


def to_5m(rows, end_label=True):
    """1 分K 組成 5 分K,格式和回測(daytrade_lab.py 的 Yahoo 5 分K)一樣:
    [{"t": "09:00"(這一根開始時間), "o", "h", "l", "c", "v"}, ...]。13:30 收盤集合競價併進 13:25 那根。"""
    out = {}
    for m, o, h, l, c, v in rows:
        start = m - (1 if end_label else 0)
        if start < 9 * 60 or start > 13 * 60 + 30:      # 盤前、盤後的資料不併進任何一根
            continue
        start = min(start, 13 * 60 + 29)               # 13:30 收盤集合競價併進 13:25 那根
        b5 = start // 5 * 5
        b = out.get(b5)
        if b is None:
            out[b5] = {"t": f"{b5 // 60:02d}:{b5 % 60:02d}", "o": o, "h": h, "l": l, "c": c, "v": v}
        else:
            b["h"], b["l"], b["c"], b["v"] = max(b["h"], h), min(b["l"], l), c, b["v"] + v
    return [out[k] for k in sorted(out)]


if __name__ == "__main__":
    # 手動測試:python sjquote.py 2330 2317
    import sys
    rows = stock_rows(sys.argv[1:] or ["2330", "2317", "0050"])
    for c, r in rows.items():
        print(c, r["200009"], r["6"], r["56"], r["200013"], r["200067"], r["200007"])
    today = datetime.now(TAIPEI).strftime("%Y-%m-%d")
    k1 = kbars_1m(list(rows)[:2], today)
    end_label = guess_end_label(k1)
    for c, r1 in k1.items():
        b5 = to_5m(r1, end_label)
        print(c, "1分K", len(r1), "根,第一根", r1[0], "最後", r1[-1], "| 時間寫法:", "結束" if end_label else "開始",
              "| 5分K", len(b5), "根,第一根", b5[0] if b5 else None, "| 1分K量合計", sum(x[5] for x in r1), "快照總量", rows[c]["200013"])
    print(STATS)
