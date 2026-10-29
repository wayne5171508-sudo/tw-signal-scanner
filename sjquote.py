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

SNAP_BATCH = 500          # 永豐一次快照最多 500 檔
_api = None
_tried = False
STATS = {"login": None, "rows": 0, "asked": 0, "seconds": 0.0}


def _log(msg):
    print(f"[永豐] {msg}")


def _login():
    """一支程式只登入一次;失敗就記住,這一輪不再重試(交給鉅亨)。"""
    global _api, _tried
    if _tried:
        return _api
    _tried = True
    key, sec = os.environ.get("SJ_API_KEY"), os.environ.get("SJ_SECRET_KEY")
    if not (key and sec):
        STATS["login"] = "no_key"
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
        _log(f"登入失敗,這一輪改用鉅亨: {type(e).__name__}: {str(e)[:150]}")
    return _api


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
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 1:
                    _log(f"快照失敗({len(batch)} 檔),改用鉅亨: {type(e).__name__}: {str(e)[:120]}")
                else:
                    time.sleep(1)
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


if __name__ == "__main__":
    # 手動測試:python sjquote.py 2330 2317
    import sys
    rows = stock_rows(sys.argv[1:] or ["2330", "2317", "0050"])
    for c, r in rows.items():
        print(c, r["200009"], r["6"], r["56"], r["200013"], r["200067"], r["200007"])
    print(STATS)
