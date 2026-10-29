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
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

SNAP_BATCH = 500          # 永豐一次快照最多 500 檔
KBAR_GAP = 0.12           # 每檔 1 分K 查詢之間停多久(永豐資料查詢有頻率上限,約每 5 秒 50 次)
TAIPEI = ZoneInfo("Asia/Taipei")
_api = None
_next_try = 0.0           # 登入失敗後,這個時間(epoch 秒)之前不再重試
_fails = 0                # 連續幾次查詢失敗;到 RELOGIN_FAILS 就當作連線斷了,下次重新登入
RELOGIN_GAP = 120         # 登入失敗後隔幾秒再試(盤中常駐迴圈一整天只用同一個連線)
RELOGIN_FAILS = 2
STATS = {"login": None, "rows": 0, "asked": 0, "seconds": 0.0}


def _log(msg):
    print(f"[永豐] {msg}")


def _login():
    """登入一次後一直沿用(盤中常駐迴圈 loop.py 一整天同一個連線)。
    失敗就交給鉅亨,RELOGIN_GAP 秒後才再試;查詢連續失敗(_broken)也會在下一次重新登入。"""
    global _api, _next_try
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


def _broken():
    """查詢失敗一次記一次;連續 RELOGIN_FAILS 次就丟掉這個連線,下一次 _login() 重新登入。"""
    global _api, _fails
    _fails += 1
    if _fails >= RELOGIN_FAILS and _api is not None:
        _log(f"連續 {_fails} 次查詢失敗,下一次重新登入")
        old, _api, _fails = _api, None, 0
        try:
            old.logout()
        except Exception:  # noqa: BLE001
            pass


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
        start = min(m - (1 if end_label else 0), 13 * 60 + 29)
        if start < 9 * 60:
            continue
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
