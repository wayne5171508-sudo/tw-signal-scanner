#!/usr/bin/env python3
"""
永豐歷史 1 分K 回補當沖回測樣本(sj_backfill.yml 每晚跑;盤中迴圈收盤後也會跑一次補今天)

為什麼要換:原本回測用 Yahoo 5 分K,只有最近 60 天,而且量不完整(10/28 實測 1316 只有正式成交量的 44%),
量能倍數會失真;盤中又已經改用永豐,回測和盤中資料來源不一樣。永豐 1 分K 有一年以上、量是完整的。

做法:
- 股票 = 自選股(不含 ETF)+ 成交值前 200 大(data/top_universe.json),寫進和原本一樣的存檔
  data/bars5m/日期.json.gz(自選股)、data/bars5m_top/日期.json.gz(其他),daytrade_lab.py 不用改就會讀到。
- 一檔一次查詢整段期間,1 分K 組成 5 分K(和盤中 sjquote.to_5m 同一套),量換成「股」(× 1000,和 Yahoo 存檔同單位)。
  每一天記下前一交易日收盤(pd/pc),同一檔所有日子都換成永豐,才不會同一檔前後天資料來源不同、量能倍數失真。
- 已經是永豐的(s == "sj")不重抓;Yahoo 的會被覆蓋。加權指數 5 分K 也換成永豐(IX0001)。
- 每天資料用量上限 500MB:剩下不到 --min-remaining-mb 就停,剩下的明天晚上繼續。
- 已知限制:價格沒有還原除權息,除權息當天的「前一日收盤」是未調整的價格(一檔一年只有一兩天)。

用法:python sj_backfill.py [--days 380] [--max-seconds 3000] [--min-remaining-mb 120]
"""

import argparse
import gzip
import json
import os
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import sjquote

TAIPEI = ZoneInfo("Asia/Taipei")
DATA_DIR = "data"
BARS_DIR = os.path.join(DATA_DIR, "bars5m")
BARS_DIR_TOP = os.path.join(DATA_DIR, "bars5m_top")
TOP_FILE = os.path.join(DATA_DIR, "top_universe.json")
STATUS_FILE = os.path.join(DATA_DIR, "backfill_status.json")
TRIED_FILE = os.path.join(DATA_DIR, "backfill_tried.json")   # {代號: [試過但永豐沒有完整資料的日子]}:停牌、還沒上市、冷門股根數不足,不再每晚重抓
RUN_FROM, RUN_TO = 14 * 60, 23 * 60 + 30   # 只在 14:00~23:30 跑:額度午夜重算,不能吃到隔天盤中要用的
LAST_BAR = "13:20"          # 和 daytrade_lab.LAST_BAR 一樣:要有 13:20 那根才算完整的一天
MIN_BARS_DAY = 45           # 和 daytrade_lab.MIN_BARS_DAY 一樣
SOURCE = "Shioaji 1m→5m"


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _round_bar(b, with_vol=True):
    return [b["t"], round(b["o"], 4), round(b["h"], 4), round(b["l"], 4), round(b["c"], 4)] + ([int(b["v"])] if with_vol else [])


def watch_codes():
    import intraday
    return [c for c in intraday.WATCHLIST_NAMES if len(c) == 4 and c.isdigit() and not c.startswith("00")]


def split_days(kb):
    """永豐 1 分K → {日期: [[第幾分鐘, 開, 高, 低, 收, 量], ...]}。"""
    days = {}
    for ts, o, h, l, c, v in zip(*(getattr(kb, f, None) or [] for f in ("ts", "Open", "High", "Low", "Close", "Volume"))):
        wm = sjquote._wall_minute(ts)
        if wm and all(sjquote._num(x) for x in (o, h, l, c, v)) and c > 0:
            days.setdefault(wm[0], []).append([wm[1], float(o), float(h), float(l), float(c), float(v)])
    return {d: sorted(r) for d, r in days.items()}


class Archive:
    """一個資料夾的每日存檔,用到才讀、最後一次寫回。"""

    def __init__(self, folder, idx):
        self.folder, self.days, self.dirty, self.idx = folder, {}, set(), idx   # idx = {日期: 永豐加權指數 5 分K}
        self.saved, self.broken = 0, set()      # broken:讀不到的存檔(截斷的 gz),不覆蓋,留給人看

    def path(self, d):
        return os.path.join(self.folder, f"{d}.json.gz")

    def get(self, d):
        if d not in self.days:
            day = None
            if os.path.exists(self.path(d)):
                try:
                    with gzip.open(self.path(d), "rt", encoding="utf-8") as f:
                        day = json.load(f)
                except (OSError, ValueError, EOFError) as e:
                    print(f"[警告] {self.path(d)} 讀不到({type(e).__name__}),這天不寫,避免蓋掉原本的資料")
                    self.broken.add(d)
                    day = None
            self.days[d] = day or {"date": d, "source": SOURCE, "stocks": {}, "idx": []}
        return self.days[d]

    def is_sj(self, d, code):
        x = (self.get(d).get("stocks") or {}).get(code)
        return bool(x and x.get("s") == "sj")

    def put(self, d, code, entry):
        day = self.get(d)
        if d in self.broken:
            return
        day["stocks"][code] = entry
        if self.idx.get(d):
            day["idx"] = self.idx[d]
        if day.get("source") != SOURCE and not all(x.get("s") == "sj" for x in day["stocks"].values()):
            day["source"] = f"{SOURCE}(部分 Yahoo Finance 5m)"
        elif all(x.get("s") == "sj" for x in day["stocks"].values()):
            day["source"] = SOURCE
        self.dirty.add(d)

    def save(self):
        os.makedirs(self.folder, exist_ok=True)
        for d in sorted(self.dirty):
            tmp = self.path(d) + ".tmp"
            with gzip.open(tmp, "wt", encoding="utf-8") as f:
                json.dump(self.days[d], f, separators=(",", ":"))
            os.replace(tmp, self.path(d))
        n = len(self.dirty)
        self.saved += n
        self.dirty.clear()
        return n


def remaining_mb(api):
    try:
        u = api.usage()
        return round(u.remaining_bytes / 1e6, 1), round(u.limit_bytes / 1e6, 1)
    except Exception:  # noqa: BLE001
        return None, None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=380, help="往回補幾個日曆天")
    ap.add_argument("--max-seconds", type=float, default=3000)
    ap.add_argument("--min-remaining-mb", type=float, default=120)
    ap.add_argument("--any-time", action="store_true", help="不管時間限制(手動測試用)")
    a = ap.parse_args(argv)
    t0 = time.time()
    hm = datetime.now(TAIPEI).hour * 60 + datetime.now(TAIPEI).minute
    if not a.any_time and not RUN_FROM <= hm <= RUN_TO:
        print("只在 14:00~23:30 回補(資料額度午夜重算,不能用掉隔天盤中的額度),這次不跑")
        return 0
    api = sjquote._login()
    if api is None:
        print("沒有永豐金鑰或登入失敗,不補")
        return 1
    now = datetime.now(TAIPEI)
    end = now.strftime("%Y-%m-%d") if now.hour * 60 + now.minute >= 13 * 60 + 35 else (now - timedelta(days=1)).strftime("%Y-%m-%d")
    start = (now - timedelta(days=a.days)).strftime("%Y-%m-%d")
    watch = watch_codes()
    top = [c for c in (_load(TOP_FILE, {}).get("codes") or []) if c not in watch]
    # 加權指數:決定有哪些交易日,也換掉存檔裡的大盤 5 分K
    ix = next((k for k in api.Contracts.Indexs.TSE if k.code == "IX0001"), None)
    idx_days = split_days(api.kbars(ix, start=start, end=end)) if ix else {}
    end_label = sjquote.guess_end_label(idx_days) if idx_days else True
    trade_days = sorted(d for d, r in idx_days.items() if len(r) >= 200)
    if not trade_days:
        print("永豐沒有回傳加權指數 1 分K,不補")
        return 1
    idx5 = {d: [_round_bar(b, False) for b in sjquote.to_5m(idx_days[d], end_label)] for d in trade_days}
    arch = {"watch": Archive(BARS_DIR, idx5), "top": Archive(BARS_DIR_TOP, idx5)}
    print(f"期間 {trade_days[0]} ~ {trade_days[-1]},{len(trade_days)} 個交易日;自選股 {len(watch)} 檔、前 200 大 {len(top)} 檔")

    tried = {c: set(v) for c, v in _load(TRIED_FILE, {}).items()}
    done, skipped, stop, n_days = 0, 0, None, 0
    for i, code in enumerate(watch + top):
        ar = arch["watch" if code in watch else "top"]
        need = [d for d in trade_days[1:] if not ar.is_sj(d, code) and d not in tried.get(code, ())]
        if not need:
            skipped += 1
            continue
        if time.time() - t0 > a.max_seconds:
            stop = f"超過 {a.max_seconds:.0f} 秒"
            break
        if True:                                       # 每檔都看額度(一檔一年約 2~3MB,每 10 檔才看會超用)
            rem, _lim = remaining_mb(api)
            if rem is not None and rem < a.min_remaining_mb:
                stop = f"今天的永豐資料額度只剩 {rem} MB"
                break
        k = sjquote._contract(api, code)
        if k is None:
            continue
        s0 = trade_days[max(0, trade_days.index(need[0]) - 1)]      # 多抓前一天,才有「前一日收盤」
        try:
            days = split_days(api.kbars(k, start=s0, end=need[-1]))
        except Exception as e:  # noqa: BLE001  單檔失敗跳過,明天再補
            print(f"{code} 1 分K 失敗: {type(e).__name__}: {str(e)[:100]}")
            time.sleep(1)
            continue
        el = sjquote.guess_end_label(days) if days else end_label
        prev_d, prev_c = None, None
        for d in sorted(days):
            bars = sjquote.to_5m(days[d], el)
            close = days[d][-1][4]
            if d in need and prev_c and len(bars) >= MIN_BARS_DAY and bars[-1]["t"] >= LAST_BAR:
                for b in bars:
                    b["v"] *= 1000                          # 張 → 股(和 Yahoo 存檔同單位)
                ar.put(d, code, {"pd": prev_d, "pc": prev_c, "b": [_round_bar(b) for b in bars], "s": "sj"})
                n_days += 1
            prev_d, prev_c = d, close
        # 這次查過、永豐沒有完整資料的日子(停牌、還沒上市、根數不足)記下來,之後不再整段重抓
        got = {d for d in need if ar.is_sj(d, code)}
        miss = [d for d in need if d not in got and d < end and d not in ar.broken]
        if miss:
            tried.setdefault(code, set()).update(miss)
        done += 1
        time.sleep(sjquote.KBAR_GAP)
        if done % 40 == 0:
            for x in arch.values():
                x.save()
            print(f"…{done} 檔({time.time() - t0:.0f} 秒)")
    for x in arch.values():
        x.save()
    saved = sum(x.saved for x in arch.values())
    os.makedirs(os.path.dirname(TRIED_FILE) or ".", exist_ok=True)
    with open(TRIED_FILE, "w", encoding="utf-8") as f:
        json.dump({c: sorted(v) for c, v in sorted(tried.items())}, f, separators=(",", ":"))
    rem, lim = remaining_mb(api)
    status = {"as_of": now.isoformat(timespec="seconds"), "window": [trade_days[0], trade_days[-1]], "trade_days": len(trade_days),
              "codes_total": len(watch) + len(top), "codes_done_now": done, "codes_already": skipped, "stock_days_now": n_days,
              "files_saved": saved, "stopped": stop, "remaining_mb": rem, "limit_mb": lim, "seconds": round(time.time() - t0)}
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=1)
    print(json.dumps(status, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
