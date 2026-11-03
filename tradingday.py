#!/usr/bin/env python3
"""
台北時間的「最近一個已收盤交易日」。

GitHub Actions 的 cron 常常晚好幾個小時,盤後工作會在午夜之後才跑。
這時候若用「日曆上的今天」去對日K,會對到還沒開盤的下一天,整份成績單被略過。
這裡一律用 Asia/Taipei:比今天早的交易日已經收盤;等於今天時,要過了收盤才算。

不連網的函式給 bear、daytrade、測試用。--print / --github-output 才會查證交所 FMTQIK。
"""

import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TAIPEI = ZoneInfo("Asia/Taipei")
CLOSE_MINUTE = 13 * 60 + 30            # 證交所 13:30 收盤
DAYTRADE_CLOSE_MINUTE = 13 * 60 + 35   # 當沖回測沿用原本的 13:35,才把「今天」的 5 分K當成收完
OPEN_HM = (8, 50)                      # 盤中迴圈對齊的開盤前時間
SESSION_END_HM = (13, 40)
RELAY_RUN_WITHIN = 40 * 60             # 離開盤不到這段就直接跑
RELAY_NAP_MAX = 5 * 3600               # 一棒最多睡 5 小時(job 上限 6 小時)
# 證交所 115 年開休市表裡「沒有交易」的平日(不含週末,週末另外跳過)。不含颱風臨時休市。
CLOSED_DATES = frozenset({
    "2026-01-01",
    "2026-02-12", "2026-02-13", "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20", "2026-02-27",
    "2026-04-03", "2026-04-06",
    "2026-05-01",
    "2026-06-19",
    "2026-09-25", "2026-09-28",
    "2026-10-09", "2026-10-26",
    "2026-12-25",
})
FMTQIK = "https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK?date={ym}&response=json"
_ROC = re.compile(r"^\s*(\d{2,3})/(\d{1,2})/(\d{1,2})")


def _require_taipei(run_time):
    if getattr(getattr(run_time, "tzinfo", None), "key", None) != "Asia/Taipei":
        raise ValueError("run_time 必須是 Asia/Taipei")


def day_is_closed(day, run_time, close_minute=CLOSE_MINUTE):
    """day(YYYY-MM-DD) 的盤是否已經收完。跨過午夜之後,昨天算已收盤。"""
    _require_taipei(run_time)
    today = run_time.strftime("%Y-%m-%d")
    if day < today:
        return True
    if day > today:
        return False
    return run_time.hour * 60 + run_time.minute >= close_minute


def completed_bar_date(run_time, latest_bar, close_minute=CLOSE_MINUTE):
    """日K最新那根如果已經收盤,回傳那個日期;還在盤中就回傳 None。"""
    if not latest_bar:
        return None
    if day_is_closed(latest_bar, run_time, close_minute):
        return latest_bar
    return None


def latest_completed(run_time, trading_days, close_minute=CLOSE_MINUTE):
    """trading_days 裡最近一個已收盤的日期。沒有就 None。"""
    _require_taipei(run_time)
    done = [d for d in trading_days if d and day_is_closed(d, run_time, close_minute)]
    return max(done) if done else None


def month_starts(run_time):
    """這個月和上個月的 1 號(YYYYMM01)。月初跨午夜時,交易日在上個月的 FMTQIK。"""
    _require_taipei(run_time)
    prev = run_time.replace(day=1) - timedelta(days=1)
    return [run_time.strftime("%Y%m") + "01", prev.strftime("%Y%m") + "01"]


def parse_roc_date(text):
    m = _ROC.match(str(text or ""))
    if not m:
        return None
    return f"{int(m.group(1)) + 1911:04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def parse_fmtqik(payload):
    days = []
    for row in (payload or {}).get("data") or []:
        if not row:
            continue
        d = parse_roc_date(row[0])
        if d:
            days.append(d)
    return days


def _fetch_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; personal-trading-dashboard/1.0)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def latest_completed_trading_day(run_time, fetch=None):
    """向證交所查最近一個已收盤交易日。fetch(url) 回傳 JSON,測試可換掉。"""
    _require_taipei(run_time)
    fetch = fetch or _fetch_json
    days = []
    errors = []
    for ym in month_starts(run_time):
        try:
            days.extend(parse_fmtqik(fetch(FMTQIK.format(ym=ym))))
        except Exception as e:  # noqa: BLE001  單月失敗還有另一個月
            errors.append(e)
    if not days and errors:
        raise errors[-1]
    return latest_completed(run_time, days)


def is_trading_day(day, closed=None):
    """day 是 datetime.date。週六日和 CLOSED_DATES 不算交易日。"""
    closed = CLOSED_DATES if closed is None else closed
    return day.weekday() < 5 and day.isoformat() not in closed


def next_open(now, closed=None):
    """下一個還趕得上的交易日 08:50(台北)。今天已過 13:30 就從明天開始找。"""
    _require_taipei(now)
    for i in range(0, 16):
        day = (now + timedelta(days=i)).date()
        if not is_trading_day(day, closed):
            continue
        start = datetime(day.year, day.month, day.day, OPEN_HM[0], OPEN_HM[1], tzinfo=TAIPEI)
        end = datetime(day.year, day.month, day.day, SESSION_END_HM[0], SESSION_END_HM[1], tzinfo=TAIPEI)
        if end - timedelta(minutes=10) > now:
            return start
    return None


def relay_plan(now, closed=None):
    """盤中接力該直接跑、睡一段再叫醒,還是把週末和連假留給 cron。
    回傳 action: run / nap / wait_cron,seconds,next_open(ISO 或空字串)。"""
    nxt = next_open(now, closed)
    if nxt is None:
        return {"action": "wait_cron", "seconds": 0, "next_open": ""}
    wait = (nxt - now).total_seconds()
    if wait <= RELAY_RUN_WITHIN:
        return {"action": "run", "seconds": max(0, int(wait)), "next_open": nxt.isoformat()}
    # 下一個交易日比「明天」還遠:週末或連假。不從收盤一路睡到開盤,交給 08:10 / 01:00 的 cron。
    # 明天就開盤的話仍睡一段再叫醒,cron 晚到時平日早上還是接得上。
    if nxt.date() > (now + timedelta(days=1)).date():
        return {"action": "wait_cron", "seconds": int(wait), "next_open": nxt.isoformat()}
    nap = min(wait - 20 * 60, RELAY_NAP_MAX)
    return {"action": "nap", "seconds": int(max(0, nap)), "next_open": nxt.isoformat()}


def postmarket_plan(run_time, trading_days, scorecard_bar, daytrade_last, bars_exist, force=False):
    """盤後工作要不要重跑。已經算過同一個已收盤交易日就跳過,跨午夜也對得到那一天。"""
    target = latest_completed(run_time, trading_days) or ""
    if force or not target:
        return {"target": target, "bear_skip": "0", "daytrade_skip": "0"}
    bear_skip = "1" if scorecard_bar == target else "0"
    daytrade_skip = "1" if daytrade_last == target and bars_exist else "0"
    return {"target": target, "bear_skip": bear_skip, "daytrade_skip": daytrade_skip}


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh) or {}
    except (OSError, ValueError):
        return {}


def github_status(run_time=None, force=False):
    run_time = run_time or datetime.now(TAIPEI)
    target = ""
    try:
        target = latest_completed_trading_day(run_time) or ""
    except Exception as e:  # noqa: BLE001
        print(f"[警告] 查不到交易日曆,這次照跑: {type(e).__name__}: {e}", file=sys.stderr)
    score = _read_json(os.path.join("data", "bear_scorecard.json"))
    daytrade = _read_json(os.path.join("data", "daytrade.json"))
    last = (daytrade.get("archive") or {}).get("last") or ""
    bars = bool(target) and os.path.exists(os.path.join("data", "bars5m", f"{target}.json.gz"))
    # 交易日曆暫時查不到時,不在這裡跳過,交給 bear_signals / daytrade_lab 看日K自己決定
    days = [target] if target else []
    return postmarket_plan(run_time, days, score.get("latest_bar") or "", last, bars, force=force)


def _emit_github(plan):
    path = os.environ.get("GITHUB_OUTPUT")
    lines = "".join(f"{k}={plan[k]}\n" for k in ("target", "bear_skip", "daytrade_skip"))
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(lines)
    sys.stdout.write(lines)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    force = os.environ.get("FORCE") == "true" or "--force" in argv
    if "--github-output" in argv:
        plan = github_status(force=force)
        _emit_github(plan)
        print(f"目標交易日 {plan['target'] or '未知'} 利空跳過={plan['bear_skip']} 當沖跳過={plan['daytrade_skip']}", file=sys.stderr)
        return 0
    if "--relay" in argv:
        plan = relay_plan(datetime.now(TAIPEI))
        print(f"action={plan['action']}")
        print(f"seconds={plan['seconds']}")
        print(f"next={plan['next_open']}")
        return 0
    if "--print" in argv:
        try:
            print(latest_completed_trading_day(datetime.now(TAIPEI)) or "")
        except Exception as e:  # noqa: BLE001
            print(f"[警告] {type(e).__name__}: {e}", file=sys.stderr)
            print("")
        return 0
    print("用法: tradingday.py --print | --github-output", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
