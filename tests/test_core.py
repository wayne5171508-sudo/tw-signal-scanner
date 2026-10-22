"""爆量雷達核心計算的回歸測試。push 到 GitHub 時自動跑(.github/workflows/tests.yml),
改壞了會在 Actions 頁面亮紅燈,不用等到盤中才發現。全部離線,不連任何報價網站。"""
import datetime as dt
import json
import os
import random
import sys
from zoneinfo import ZoneInfo

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import health  # noqa: E402
import intraday  # noqa: E402
import lab  # noqa: E402
import rebuild_track  # noqa: E402
import scan  # noqa: E402

TPE = ZoneInfo("Asia/Taipei")


# ---------- scan.py ----------

def test_roc_to_iso():
    assert scan.roc_to_iso("115/10/22") == "2026-10-22"
    assert scan.roc_to_iso("115/1/5") == "2026-01-05"
    assert scan.roc_to_iso("abc") is None


def test_clean_number():
    assert scan.clean_number("1,234") == 1234
    assert scan.clean_number('="2330"') == 2330
    assert scan.clean_number("+12.5") == 12.5
    assert scan.clean_number("--") is None


def test_track_stats_window_and_rates():
    hist = [{"close_return_pct": v, "day_high_return_pct": v + 1} for v in [1, -1, 2, -2, 3]]
    s = scan.track_stats(hist)
    assert s["sample_size"] == 5
    assert s["win_rate_close_pct"] == 60.0
    assert s["avg_close_return_pct"] == 0.6
    many = [{"close_return_pct": 1.0, "day_high_return_pct": 1.0}] * 80
    assert scan.track_stats(many)["sample_size"] == 60
    assert scan.track_stats([])["sample_size"] == 0


# ---------- intraday.py ----------

def test_cdp_formula():
    r = intraday.compute_cdp({"h": 110, "l": 90, "c": 100})
    assert r["cdp"] == 100 and r["nh"] == 110 and r["nl"] == 90 and r["ah"] == 120 and r["al"] == 80
    assert intraday.compute_cdp(None) is None
    assert intraday.compute_cdp({"h": 1, "l": None, "c": 1}) is None


@pytest.mark.parametrize("unit", [1, 1e3, 1e4])
def test_normalize_amounts_detects_unit(unit):
    rnd = random.Random(1)
    qs = []
    for _ in range(30):
        vol, last = rnd.randint(100, 5000), rnd.uniform(20, 900)
        qs.append({"volume": vol, "last": last, "amount_raw": vol * 1000 * last / unit})
    assert intraday.normalize_amounts(qs) == unit
    for q in qs:
        assert q["amt"] == pytest.approx(q["volume"] * 1000 * q["last"] / 1e8, rel=1e-3)
        assert "amount_raw" not in q


def test_normalize_amounts_gives_up_on_garbage():
    qs = [{"volume": 100, "last": 50, "amount_raw": 1} for _ in range(10)]
    assert intraday.normalize_amounts(qs) is None
    assert all(q["amt"] is None for q in qs)


# ---------- lab.py ----------

def test_rsi_bounds_and_warmup():
    up = list(range(1, 40))
    r = lab.rsi(up, 14)
    assert all(v is None for v in r[:14])
    assert r[-1] == 100.0
    rnd = random.Random(3)
    px = [100.0]
    for _ in range(300):
        px.append(px[-1] * (1 + rnd.uniform(-0.01, 0.01)))
    vals = [v for v in lab.rsi(px, 14) if v is not None]
    assert vals and all(0 <= v <= 100 for v in vals)


def test_rsi_fills_gaps():
    r = lab.rsi([1, 2, None, 4, 5, 6, 7, 8, 9], 3)
    assert r[-1] == 100.0


# ---------- rebuild_track.py ----------

def _fake_days(code, ym01):
    y, m = int(ym01[:4]), int(ym01[4:6])
    d, out = dt.date(y, m, 1), []
    while d.month == m:
        if d.weekday() < 5:
            out.append({"日期": f"{d.year - 1911}/{d.month:02d}/{d.day:02d}",
                        "開盤價": "101", "最高價": "105", "最低價": "99", "收盤價": "102"})
        d += dt.timedelta(1)
    return out


def test_check_pair_only_counts_adjacent_day(monkeypatch):
    monkeypatch.setattr(scan, "fetch_stock_day", _fake_days)
    monkeypatch.setattr(rebuild_track.time, "sleep", lambda s: None)
    rebuild_track._cache.clear()
    prev = {"date": "2026-10-16", "focus_stocks": [
        {"code": "2330", "close": 100, "signal_status": "訊號成立"},
        {"code": "1101", "close": 100, "signal_status": "候選未達標"},
    ]}
    recs = rebuild_track.check_pair(prev, "2026-10-19")   # 週五 → 週一,相鄰
    assert [r["code"] for r in recs] == ["2330"]
    assert recs[0]["close_return_pct"] == 2.0 and recs[0]["day_high_return_pct"] == 5.0
    assert rebuild_track.check_pair(prev, "2026-10-20") == []   # 中間隔一天,不算


# ---------- health.py ----------

def _write(d, name, obj):
    p = os.path.join(d, name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False)


def test_health_flags_missing_scan_and_stale_quotes(tmp_path, monkeypatch):
    d = str(tmp_path)
    monkeypatch.setattr(health, "D", d)
    monkeypatch.setattr(health, "OUT", os.path.join(d, "health.json"))
    monkeypatch.setattr(health, "SCAN_SINCE", "2026-10-19")
    bars = [{"d": x} for x in ("2026-10-19", "2026-10-20", "2026-10-21")]
    _write(d, "daily_official.json", {"bars": {"2330": bars}, "status": "ok 1/1"})
    _write(d, "intraday_latest.json", {"data_as_of": "2026-10-22T10:00:00+08:00",
                                       "official_bars_status": "ok 1/1",
                                       "watchlist_quotes": [{"last": 1}]})
    _write(d, "market_scan.json", {"date": "2026-10-22", "scans": {"30": {}}})
    for day in ("2026-10-19", "2026-10-21"):
        _write(d, f"{day}.json", {"date": day, "market_status": "OK"})
    _write(d, "track_record.json", [])
    _write(d, "latest.json", {"track_record_stats": {"sample_size": 0}})

    r = health.run(dt.datetime(2026, 10, 22, 11, 0, tzinfo=TPE))
    items = {i["key"]: i for i in r["items"]}
    assert not r["ok"]
    assert not items["quotes"]["ok"]                 # 10:00 之後一小時沒更新
    assert not items["market"]["ok"] and "10:30" in items["market"]["detail"]
    assert not items["scan"]["ok"] and "10-20" in items["scan"]["detail"]
    assert items["track"]["ok"]
    assert "review" not in items                     # 盤中還不檢查收盤產出


def test_health_detects_duplicate_and_non_adjacent_track(tmp_path, monkeypatch):
    d = str(tmp_path)
    monkeypatch.setattr(health, "D", d)
    monkeypatch.setattr(health, "OUT", os.path.join(d, "health.json"))
    bars = [{"d": x} for x in ("2026-10-19", "2026-10-20", "2026-10-21")]
    _write(d, "daily_official.json", {"bars": {"2330": bars}})
    _write(d, "intraday_latest.json", {"data_as_of": "2026-10-21T13:30:00+08:00"})
    rec = {"signal_date": "2026-10-19", "checked_date": "2026-10-21", "code": "2330", "close_return_pct": 1}
    _write(d, "track_record.json", [rec, rec])
    _write(d, "latest.json", {"track_record_stats": {"sample_size": 5}})
    r = health.run(dt.datetime(2026, 10, 21, 20, 0, tzinfo=TPE))
    t = {i["key"]: i for i in r["items"]}["track"]
    assert not t["ok"]
    assert "重複" in t["detail"] and "不是隔日" in t["detail"] and "網頁顯示 5 筆" in t["detail"]
