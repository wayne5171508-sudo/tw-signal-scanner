"""爆量雷達核心計算的回歸測試。push 到 GitHub 時自動跑(.github/workflows/tests.yml),
改壞了會在 Actions 頁面亮紅燈,不用等到盤中才發現。全部離線,不連任何報價網站。"""
import datetime as dt
import gzip
import json
import os
import random
import sys
from zoneinfo import ZoneInfo

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import bear_signals  # noqa: E402
import bias  # noqa: E402
import daytrade_lab  # noqa: E402
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



# ---------- bear_signals.py ----------

def _flat_bars(n=80, px=100.0, vol=1000):
    return [{"d": f"d{i:03d}", "o": px, "h": px * 1.01, "l": px * 0.99, "c": px, "v": vol} for i in range(n)]


def test_bear_big_black_fires():
    b = _flat_bars()
    b[-1] = {"d": "d079", "o": 100.0, "h": 100.5, "l": 95.0, "c": 95.5, "v": 3000}
    sig = bear_signals.signals_on(b, len(b) - 1)
    assert "big_black" in sig and "vol_down" in sig
    assert "big_black" not in bear_signals.signals_on(b, len(b) - 2)


def test_bear_signals_ignore_future_bars():
    rnd = random.Random(7)
    b, px = [], 100.0
    for i in range(120):
        nxt = px * (1 + rnd.uniform(-0.04, 0.04))
        b.append({"d": f"d{i:03d}", "o": px, "h": max(px, nxt) * 1.01, "l": min(px, nxt) * 0.99,
                  "c": nxt, "v": rnd.randint(500, 5000)})
        px = nxt
    before = [bear_signals.signals_on(b, i) for i in range(100)]
    for x in b[100:]:
        x["c"] *= 0.5
        x["l"] *= 0.5
    assert [bear_signals.signals_on(b, i) for i in range(100)] == before


def test_bear_grade_rules():
    good = {"n": 300, "ex": -1.5, "t": -2.5, "lose_gap": 5.0}
    assert bear_signals.grade(good, {"n": 50, "ex": -1.0})[0] == "active"
    assert bear_signals.grade(good, {"n": 50, "ex": 0.3})[0] == "watch"
    assert bear_signals.grade({**good, "ex": 0.4, "t": 1.0}, {"n": 50, "ex": 0.3})[0] == "inactive"
    assert bear_signals.grade({**good, "n": 10}, {"n": 5, "ex": -1})[0] == "inactive"



def test_bear_adjust_bars_dividend_split_and_self_detect():
    b = [{"d": "d0", "o": 100, "h": 101, "l": 99, "c": 100, "v": 1000},
         {"d": "d1", "o": 95, "h": 96, "l": 94, "c": 95, "v": 1000},      # 除息 5 元
         {"d": "d2", "o": 47.5, "h": 48, "l": 47, "c": 47.5, "v": 2000},  # 1 股變 2 股
         {"d": "d3", "o": 4.75, "h": 4.8, "l": 4.7, "c": 4.75, "v": 20000}]  # 沒有事件紀錄的 1 拆 10
    applied = bear_signals.adjust_bars(b, {"div": {"d1": 5.0}, "split": {"d2": 2.0}})
    assert [a["d"] for a in applied] == ["d1", "d2", "d3"]
    assert [round(x["c"], 4) for x in b] == [4.75] * 4
    assert round(b[0]["v"]) == 20000 and round(b[1]["v"]) == 20000
    assert all(x.get("event") for x in b[1:]) and not b[0].get("event")
    assert bear_signals.signals_on(b, 3) == []


def test_bear_entry_windows_finds_expensive_open():
    hourly, flagged = {}, {}
    for k in range(30):
        days = {}
        for j in range(80):
            d = f"2026-{1 + j // 28:02d}-{1 + j % 28:02d}"
            days[d] = {"09": 101.0 + (j % 3) * 0.1, "10": 100.6, "11": 100.3, "12": 100.1, "13": 100.0, "close": 100.0}
        hourly[f"{1000 + k}"] = days
    sig = {"big_black": {"status": "active"}}
    w = bear_signals.entry_windows(hourly, flagged, sig, {})
    a = w["all"]
    assert a["ready"] and a["avoid"] == "09" and a["best"] == "13"
    assert a["by_window"]["09"]["cheap"] > a["by_window"]["13"]["cheap"]
    assert w["after_bear"]["days"] == 0 and w["after_bear"]["best"] is None


def test_bear_level_rr():
    b = _flat_bars(30)
    for x in b[-21:-1]:
        x["h"], x["l"] = 110.0, 95.0
    b[-1]["c"] = 100.0
    rr = bear_signals.level_rr(b, len(b) - 1)
    assert rr["stop"] == 95.0 and rr["target"] == 110.0 and rr["rr"] == 2.0
    b[-1]["c"] = 94.0
    assert "note" in bear_signals.level_rr(b, len(b) - 1)


def _5m_day(prices):
    bars, t = [], dt.datetime(2026, 1, 5, 9, 0)
    for p in prices:
        bars.append({"t": t.strftime("%H:%M"), "o": p, "h": p * 1.001, "l": p * 0.999, "c": p, "v": 100})
        t += dt.timedelta(minutes=5)
    return bars


def test_daytrade_orb_entry_uses_next_bar_and_no_lookahead():
    px = [100, 100.5, 100.2] + [100.3] * 3 + [101.0] + [101.5] * 47
    bars = _5m_day(px)
    i = daytrade_lab.entry_index("orb_long", bars, 99.0, False)
    assert bars[i - 1]["c"] == 101.0 and bars[i]["t"] == "09:35"
    assert daytrade_lab.entry_index("orb_long", bars[:7], 99.0, False) is None


def test_daytrade_simulate_stop_target_close_and_same_bar_rule():
    bars = _5m_day([100] * 54)
    bars[5] = {"t": bars[5]["t"], "o": 100, "h": 102.5, "l": 98.5, "c": 100, "v": 1}   # 同一根碰到停損與停利
    assert daytrade_lab.simulate(bars, 1, "long", 1.0, 2.0) == (-1.0, "stop")
    bars[5] = {"t": bars[5]["t"], "o": 100, "h": 102.5, "l": 99.9, "c": 102, "v": 1}
    assert daytrade_lab.simulate(bars, 1, "long", 1.0, 2.0) == (2.0, "target")
    r, why = daytrade_lab.simulate(bars, 1, "short", 5.0, None)
    assert why == "close" and r == 0.0
    gap = _5m_day([100] * 54)
    gap[3] = {"t": gap[3]["t"], "o": 96, "h": 96, "l": 95, "c": 95.5, "v": 1}         # 跳空越過停損,用開盤價出
    assert daytrade_lab.simulate(gap, 1, "long", 1.0, None) == (pytest.approx(-4.0), "stop")

# ---------- bias.py(多空分數)----------

_BULL = {"last": 105, "prev_close": 100, "vwap": 103, "vwap_prev": 102, "orb_hi": 104, "orb_lo": 101, "pace": 2.5,
         "day_hi": 105.5, "day_lo": 100.5, "mkt_pct": 0.8, "mkt_last": 101, "mkt_open": 100, "grp_pct": 1.2}


def test_bias_full_long_and_mirror_short():
    r = bias.score(_BULL)
    assert r["score"] == 100 and r["side"] == "long" and r["veto"] is None
    bear = dict(_BULL, last=95, vwap=97, vwap_prev=98, orb_hi=99, orb_lo=96, day_hi=99.5, day_lo=94.5,
                mkt_pct=-0.8, mkt_last=99, grp_pct=-1.2)
    assert bias.score(bear)["score"] == -100 and bias.score(bear)["side"] == "short"


def test_bias_missing_data_scores_zero_and_vetoes():
    assert bias.score({"last": 100, "prev_close": 100})["score"] == 0
    r = bias.score(dict(_BULL, mkt_pct=-0.8, mkt_last=99))       # 大盤反向
    assert r["side"] is None and r["veto"] == "大盤反向"
    r = bias.score(dict(_BULL, up_lim=105.5))                    # 離漲停不到 1%
    assert r["side"] is None and r["veto"] == "接近漲停"


def test_approx_vwap_from_snapshots():
    pts = [{"wl": {"A": 10}, "wlv": {"A": 100}}, {"wl": {"A": 12}, "wlv": {"A": 200}}]
    assert intraday._approx_vwap(pts, "A", 10) == pytest.approx((100 * 10 + 100 * 11) / 200)


def test_daytrade_score_entry_next_bar_and_no_lookahead():
    px = [100, 100.2, 100.1] + [100.3] * 3 + [102.0] + [102.5] * 47
    bars = [dict(b, v=300) for b in _5m_day(px)]                # 量是前幾天的 3 倍,沒有大盤和族群資料 → 均價線+開盤區間+量能+位置=70
    days = {"2026-08-0%d" % k: _5m_day([100] * 54) for k in range(1, 7)}
    days["2026-08-07"] = bars
    data = {"2455": days}
    closes = {"2455": {"2026-07-31": 100, **{d: 100 for d in days}}}
    ctx = daytrade_lab.score_context(data, closes, {})
    ser = daytrade_lab.score_series("2455", "2026-08-07", bars, 100, ctx)
    assert all(bars[i]["t"] >= "09:15" for i, _ in ser)
    i = daytrade_lab.entry_index("score_long", bars, 100, False, ser)
    assert i is not None and bars[i - 1]["t"] >= "09:30"     # 09:30 那根收盤才站上開盤區間
    cut = daytrade_lab.score_series("2455", "2026-08-07", bars[:7], 100, ctx)
    assert daytrade_lab.entry_index("score_long", bars[:7], 100, False, cut) is None

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


# ---------- scan.market_rank ----------

def test_market_rank_from_official_tables(monkeypatch):
    base = {"2330": (20000, 1000.0), "1101": (2000, 30.0), "9999": (500, 50.0), "2603": (3000, 5.0)}
    def fake(date_str):
        if date_str == "20261024":            # 週六沒開盤
            return None
        today = date_str == "20261023"
        out = {}
        for c, (lots, px) in base.items():
            vol = lots * 1000 * (5 if (today and c == "1101") else 1)
            close = px * (1.08 if (today and c == "2330") else 1)
            out[c] = {"name": c, "vol": vol, "close": close}
        return out
    monkeypatch.setattr(scan, "fetch_all_quotes", fake)
    monkeypatch.setattr(scan.time, "sleep", lambda s: None)
    r = scan.market_rank(dt.datetime(2026, 10, 23, 23, 0, tzinfo=TPE))
    assert r["volume_top"][0] == {"code": "1101", "name": "1101", "mult": 5.0}
    assert r["gainer_top"][0]["code"] == "2330" and r["gainer_top"][0]["pct"] == 8.0
    codes = {x["code"] for x in r["volume_top"] + r["gainer_top"]}
    assert "9999" not in codes and "2603" not in codes     # 量太小、股價太低的排除
    assert len(r["days_used"]) == 6


def test_market_rank_none_when_today_missing(monkeypatch):
    monkeypatch.setattr(scan, "fetch_all_quotes", lambda d: None)
    monkeypatch.setattr(scan.time, "sleep", lambda s: None)
    assert scan.market_rank(dt.datetime(2026, 10, 24, 23, 0, tzinfo=TPE)) is None


def test_health_amount_and_mkt_checks(tmp_path, monkeypatch):
    d = str(tmp_path)
    monkeypatch.setattr(health, "D", d)
    monkeypatch.setattr(health, "OUT", os.path.join(d, "health.json"))
    _write(d, "intraday_latest.json", {"data_as_of": "2026-10-27T13:33:00+08:00", "amount_unit": None})
    _write(d, "mkt/2026-10-27.json", {"snaps": {"30": {}, "90": {}, "180": {}}, "close": {"2330": [1, 2]}})
    items = {i["key"]: i for i in health.run(dt.datetime(2026, 10, 27, 20, 0, tzinfo=TPE))["items"]}
    assert not items["amount"]["ok"]
    assert items["mkt"]["ok"]
    _write(d, "intraday_latest.json", {"data_as_of": "2026-10-22T13:33:00+08:00", "amount_unit": None})
    items = {i["key"]: i for i in health.run(dt.datetime(2026, 10, 22, 20, 0, tzinfo=TPE))["items"]}
    assert "amount" not in items and "mkt" not in items      # 上線前的日子不檢查


def test_daytrade_pullback_needs_setup_first():
    f = lambda sc_last, lo, vw: {"last": sc_last, "prev_close": 100, "vwap": vw, "vwap_prev": vw - 0.5, "orb_hi": 101, "orb_lo": 99,
                                 "pace": 3, "day_hi": sc_last + 0.1, "day_lo": 99, "grp_pct": 1, "bar_hi": sc_last + 0.1, "bar_lo": lo}
    touch = (7, f(102.1, 102.05, 102.0))          # 碰到均價線、收在上面
    assert daytrade_lab.entry_index("pb_long", [None] * 20, 100, False, [touch]) is None   # 沒有先達到門檻不進場
    setup = (5, f(104, 103.5, 102.0))
    assert daytrade_lab.entry_index("pb_long", [None] * 20, 100, False, [setup, touch]) == 8


def test_bias_rule_hits_shared_conditions():
    hits, armed = bias.rule_hits(_BULL, {})
    assert {"score_long", "score_long_80"} <= hits and "pb_long" not in hits and armed["long"]
    touch = dict(_BULL, last=103.1, vwap=103, bar_lo=103.05, bar_hi=103.2, orb_hi=None, orb_lo=None)
    assert "pb_long" not in bias.rule_hits(touch, {})[0]          # 沒有先達到門檻
    assert "pb_long" in bias.rule_hits(touch, armed)[0]
    far = dict(_BULL, last=106, vwap=103)                         # 分數 >= 80 且高於均價線 2% 以上
    assert "fade_short" in bias.rule_hits(far, {})[0]


def _bias_quote(code, last, vol, high, low):
    return {"code": code, "name": code, "last": last, "prev_close": 100, "pct": (last / 100 - 1) * 100, "open": 100,
            "high": high, "low": low, "volume": vol, "pace": 3.0, "upper_limit": 110, "lower_limit": 90}


def test_compute_bias_records_first_trigger_and_goes_stale(tmp_path, monkeypatch):
    hist_f, dt_f, cv_f = tmp_path / "h.json", tmp_path / "d.json", tmp_path / "c.json"
    monkeypatch.setattr(intraday, "HISTORY_FILE", str(hist_f))
    monkeypatch.setattr(intraday, "DAYTRADE_FILE", str(dt_f))
    monkeypatch.setattr(intraday, "CURVE_FILE", str(cv_f))
    dt_f.write_text(json.dumps({"rules": {"score_long": {"name": "多空分數做多", "holds": True, "best": {"stop": 1.0, "target": 2.0}}}}))
    idx = [{"code": "TWS:TSE01:INDEX", "last": 101, "open": 100, "pct": 0.8}]
    t0 = dt.datetime(2026, 10, 26, 9, 30, tzinfo=TPE)
    pts = [{"t": (t0 - dt.timedelta(minutes=k)).isoformat(), "wl": {"1111": 103}, "wlv": {"1111": 5000 - k * 100}} for k in (20, 15, 10, 5, 0)]
    hist_f.write_text(json.dumps({"date": "2026-10-26", "points": pts, "orb": {"1111": [104, 101]}}))
    b = intraday.compute_bias(t0, idx, [_bias_quote("1111", 105, 5000, 105.5, 100.5)])
    it = next(x for x in b["items"] if x["rule"] == "score_long")
    assert it["trig_t"] == "09:30" and it["fresh"] and it["stop"] == 103.95 and it["target"] == 107.1
    later = t0 + dt.timedelta(minutes=15)
    h = json.loads(hist_f.read_text())
    h["points"].append({"t": later.isoformat(), "wl": {"1111": 106}, "wlv": {"1111": 6000}})
    hist_f.write_text(json.dumps(h))
    b2 = intraday.compute_bias(later, idx, [_bias_quote("1111", 106, 6000, 106.5, 100.5)])
    it2 = next(x for x in b2["items"] if x["rule"] == "score_long")
    assert it2["trig_t"] == "09:30" and it2["trig_price"] == 105 and not it2["fresh"] and it2["move_pct"] == pytest.approx(0.95)
    assert b2["n_holds"] == 1


def test_same_time_pace_matches_backtest_definition():
    grid = intraday.CURVE_GRID
    curves = {f"2026-10-{d:02d}": {"A": [min(1.0, g / 270) for g in grid]} for d in range(10, 16)}
    bars = {"A": [{"d": f"2026-10-{d:02d}", "v": 2700} for d in range(10, 16)]}
    assert intraday.same_time_pace("A", 30, "2026-10-20", 600, curves, bars) == pytest.approx(2.0)
    assert intraday.same_time_pace("A", 30, "2026-10-20", 600, dict(list(curves.items())[:4]), bars) is None


def test_bars5m_archive_roundtrip_and_merge(tmp_path, monkeypatch):
    monkeypatch.setattr(daytrade_lab, "BARS_DIR", str(tmp_path / "bars5m"))
    day = _5m_day([100.0] * 54)
    data = {f"{1000 + k}": {"2026-10-26": [dict(b) for b in day]} for k in range(25)}
    closes = {c: {"2026-10-23": 99.0} for c in data}
    assert daytrade_lab.save_bars_day("2026-10-26", data, closes, {"2026-10-26": [{"t": "09:00", "o": 1, "h": 1, "l": 1, "c": 1}]})
    assert not daytrade_lab.save_bars_day("2026-10-26", data, closes, {})         # 已存在不覆蓋
    arc = daytrade_lab.load_bars_archive()
    new_data, new_closes, idx = {"1000": {}}, {"1000": {"2026-10-23": 50.0}}, {}
    assert daytrade_lab.merge_archive(new_data, new_closes, idx, arc) == 25          # Yahoo 已經沒有這天,靠存檔補回
    assert new_data["1000"]["2026-10-26"][3]["c"] == 100.0 and new_closes["1000"]["2026-10-23"] == 99.0
    assert idx["2026-10-26"][0]["t"] == "09:00"


def test_bias_log_and_live_evaluation(tmp_path, monkeypatch):
    monkeypatch.setattr(intraday, "BIAS_LOG_FILE", str(tmp_path / "log.json"))
    hist = {"date": "2026-10-26", "bias_rules": {"score_short": {"holds": False, "stop": 1.0, "target": 2.0}},
            "bias_state": {"1111": {"armed": {}, "trig": {"score_short": {"t": "10:16", "m": 76, "price": 100.0, "score": -70}}}}}
    assert intraday.log_bias_day(hist) and not intraday.log_bias_day(hist)
    log = json.loads((tmp_path / "log.json").read_text())
    px = [100.0] * 54
    for k in range(25, 54):
        px[k] = 97.5                                                                  # 10:20 進場後一路下跌
    data = {"1111": {"2026-10-26": _5m_day(px)}}
    data["1111"]["2026-10-26"][16]["o"] = 100.2                                       # 10:20 那根(第 16 根)開盤
    assert daytrade_lab.evaluate_live(log, data, {}) == 1
    r = log["days"]["2026-10-26"]["trig"][0]["result"]
    assert r["entry_t"] == "10:20" and r["exit"] == "target" and r["net"] == pytest.approx(2.0 - daytrade_lab.COST_PCT)
    assert r["slip"] == pytest.approx(-(100 / 100.2 - 1) * 100, abs=1e-3)
    st = daytrade_lab.live_stats(log, {"score_short": {"name": "多空分數放空", "best": {"test": {"avg": -0.2}}}})
    assert st["n"] == 1 and st["rules"]["score_short"]["avg"] == pytest.approx(2.0 - daytrade_lab.COST_PCT) and st["rules"]["score_short"]["backtest_test_avg"] == -0.2


def _rsi_vol_day(reclaim=True):
    px = [100.0 - k * 0.3 for k in range(26)] + [92.0] * 28          # 一路跌,RSI 壓到 30 以下
    bars = _5m_day(px)
    for b in bars:
        b["v"] = 100
    bars[22]["v"] = 500                                               # 10:50 爆量(>= 前 20 根平均 2 倍)
    bars[22].update(h=93.6, l=93.0)
    if reclaim:
        bars[24].update(c=93.8, h=93.9, l=93.2)                       # 11:00 收盤站上訊號 K 最高價
    return bars


def test_daytrade_rsi_vol_rules():
    bars = _rsi_vol_day()
    assert bars[22]["t"] == "10:50"
    assert daytrade_lab.entry_index("rsi_vol_now", bars, 100, False) == 23
    assert daytrade_lab.entry_index("rsi_vol_reclaim", bars, 100, False) == 25
    lost = _rsi_vol_day()
    lost[23].update(l=92.5)                                           # 站回之前先跌破訊號 K 低點,放棄
    assert daytrade_lab.entry_index("rsi_vol_reclaim", lost, 100, False) is None


def test_daytrade_drawdown():
    rows = [("d1", 1.0), ("d1", 3.0), ("d2", -1.5), ("d3", -1.0), ("d4", 2.0)]
    dd = daytrade_lab.drawdown(rows)
    assert dd == {"mdd": 2.5, "worst_day": -1.5, "lose_streak": 2, "cum": 1.5}
    assert daytrade_lab.drawdown([]) == {}


def test_simulate_detail_exit_time_and_price():
    bars = _5m_day([100.0] * 54)
    bars[5] = {"t": bars[5]["t"], "o": 100, "h": 102.5, "l": 99.9, "c": 102, "v": 1}
    r, why, k, px = daytrade_lab.simulate_detail(bars, 1, "long", 1.0, 2.0)
    assert (r, why, bars[k]["t"], px) == (2.0, "target", "09:25", 102.0)
    r, why, k, px = daytrade_lab.simulate_detail(_5m_day([100.0] * 54), 1, "short", 5.0, None)
    assert why == "close" and bars[k]["t"] == "13:20" and px == 100.0
    assert daytrade_lab.simulate(bars, 1, "long", 1.0, 2.0) == (2.0, "target")


def test_write_replay_is_append_only(tmp_path, monkeypatch):
    monkeypatch.setattr(daytrade_lab, "BARS_DIR", str(tmp_path / "bars5m"))
    monkeypatch.setattr(daytrade_lab, "REPLAY_DIR", str(tmp_path / "replay"))
    day = _5m_day([100.0, 100.5, 100.2] + [100.3] * 3 + [101.0] + [101.5] * 47)
    data = {f"{1000 + k}": {"2026-10-26": [dict(b) for b in day]} for k in range(25)}
    closes = {c: {"2026-10-23": 99.0} for c in data}
    assert daytrade_lab.save_bars_day("2026-10-26", data, closes, {})
    res = {"orb_long": {"holds": False, "best": {"stop": 1.0, "target": 2.0}}}
    assert daytrade_lab.write_replay(data, closes, {}, None, res, ["2026-10-26"]) == 1
    assert daytrade_lab.write_replay(data, closes, {}, None, res, ["2026-10-26"]) == 0
    import gzip as _gz
    body = json.load(_gz.open(tmp_path / "replay" / "2026-10-26.json.gz", "rt"))
    t = body["trades"]["1000"][0]
    assert t[0] == "orb_long" and t[1] == "09:35" and t[2] == 101.5 and body["params"]["orb_long"] == [1.0, 2.0]
    idx = json.loads((tmp_path / "replay" / "index.json").read_text())
    assert idx["dates"] == ["2026-10-26"] and idx["rules"]["orb_long"]["side"] == "long"


def test_corp_adjust_to_reference_and_build_levels_ex_today(tmp_path, monkeypatch):
    import corp
    b = [{"d": f"2026-10-{d:02d}", "o": 100, "h": 102, "l": 98, "c": 100, "v": 1000} for d in range(1, 23)]
    assert corp.adjust_to_reference([dict(x) for x in b], 300) is None            # 比例太誇張,不當成除權息
    bb = [dict(x) for x in b]
    assert corp.adjust_to_reference(bb, 95) == pytest.approx(0.95) and bb[-1]["c"] == pytest.approx(95) and bb[0]["v"] == 1000
    monkeypatch.setattr(corp, "CORP_FILE", str(tmp_path / "none.json"))
    off = {"bars": {"1111": b}}
    q = {"code": "1111", "name": "x", "last": 96, "prev_close": 95, "high": 97, "low": 94, "volume": 500}
    intraday.build_levels([q], {}, [{"date": "2026-10-22", "bars": {}}], off, "2026-10-23")
    assert q["ex_today"] == pytest.approx(0.95)
    assert q["cdp"]["basis"]["source"] == "official" and q["cdp"]["basis"]["c"] == pytest.approx(95)
    assert q["swing"]["h20"] == pytest.approx(102 * 0.95) and q["swing"]["adjusted"]
    assert b[-1]["c"] == 100                                                          # 原始日K沒有被改到


def test_health_new_pipelines(tmp_path, monkeypatch):
    d = str(tmp_path)
    monkeypatch.setattr(health, "D", d)
    monkeypatch.setattr(health, "OUT", os.path.join(d, "health.json"))
    _write(d, "intraday_latest.json", {"data_as_of": "2026-10-27T10:00:00+08:00", "bias": None})
    items = {i["key"]: i for i in health.run(dt.datetime(2026, 10, 27, 10, 5, tzinfo=TPE))["items"]}
    assert not items["bias"]["ok"] and "bear" not in items          # 盤中只檢查多空判斷
    _write(d, "intraday_latest.json", {"data_as_of": "2026-10-27T13:33:00+08:00"})
    _write(d, "bear_scorecard.json", {"latest_bar": "2026-10-27"})
    _write(d, "daytrade.json", {"archive": {"last": "2026-10-26"}})
    items = {i["key"]: i for i in health.run(dt.datetime(2026, 10, 27, 18, 30, tzinfo=TPE))["items"]}
    assert items["bear"]["ok"] and not items["daytrade"]["ok"]       # 5分K 沒存到當天
    os.makedirs(os.path.join(d, "bars5m")); os.makedirs(os.path.join(d, "replay"))
    open(os.path.join(d, "bars5m", "2026-10-27.json.gz"), "w").close(); open(os.path.join(d, "replay", "2026-10-27.json.gz"), "w").close()
    _write(d, "daytrade.json", {"archive": {"last": "2026-10-27"}})
    items = {i["key"]: i for i in health.run(dt.datetime(2026, 10, 27, 18, 30, tzinfo=TPE))["items"]}
    assert items["daytrade"]["ok"]


def test_tradeflags_parsing_and_build():
    import tradeflags as tf
    assert tf.roc_to_iso("1151027") == "2026-10-27" and tf.roc_to_iso("115/10/21") == "2026-10-21" and tf.roc_to_iso("20261022") == "2026-10-22"
    assert tf.period_range("115/10/21～115/10/28") == ("2026-10-21", "2026-10-28")
    assert tf.period_range("1151022~1151029") == ("2026-10-22", "2026-10-29")
    raw = {
        "twse_daytrade": [{"Date": "1151027", "Code": "2303", "Suspension": ""}, {"Date": "1151027", "Code": "0056", "Suspension": "Y"}],
        "tpex_daytrade": [{"資料日期": "1151027", "證券代號": "8042", "暫停現股賣出後現款買進當沖註記": ""}],
        "twse_punish": [{"Code": "2424", "DispositionPeriod": "115/10/21～115/10/28", "DispositionMeasures": "人工管制撮合"},
                        {"Code": "2303", "DispositionPeriod": "115/09/01～115/09/08", "DispositionMeasures": "人工管制撮合"}],
        "tpex_punish": [], "twse_notice": [], "tpex_notice": [{"Date": "1151022", "SecuritiesCompanyCode": "1815", "TradingInformation": "週轉率過高"}],
        "twse_accum": None, "tpex_accum": [], "twse_change": [],
    }
    flags, status, d = tf.build_flags(raw, ["2303", "0056", "8042", "2424", "1815", "9999"], "2026-10-27")
    assert d == "2026-10-27" and status["twse_accum"] == "fail"
    assert flags["2303"] == {"daytrade": True}                      # 過期的處置不算
    assert flags["0056"]["no_sell_first"] and flags["2424"]["disposal"]["end"] == "2026-10-28"
    assert flags["2424"]["daytrade"] is False and flags["1815"]["notice"]["reason"] == "週轉率過高"
    assert flags["9999"] == {"daytrade": False}
    assert flags["2424"]["disposal"]["measure"] == "人工管制撮合"
    assert tf.summarize_measure("約每五分鐘撮合一次") == "每5分鐘撮合" and tf.summarize_measure("每二十分鐘") == "每20分鐘撮合" and tf.summarize_measure("每十分鐘") == "每10分鐘撮合"
    assert tf.summarize_measure("* 處置措施：(1)以人工管制之撮合終端機執行撮合作業(約每2分鐘撮合一次)；(2)證券商應就投資人每日已委託買賣該有價證券,向該投資人收取全部之買進價金") == "每2分鐘撮合・預收款券"
    raw["tpex_daytrade"] = None                                      # 名單抓不到時不能說「不可當沖」
    flags, _, _ = tf.build_flags(raw, ["9999"], "2026-10-27")
    assert "9999" not in flags


def test_rank_top_and_cross_check():
    twse = [{"Code": "2330", "Name": "台積電", "TradeValue": "9,000"}, {"Code": "0050", "Name": "ETF", "TradeValue": "99999"},
            {"Code": "2303", "Name": "聯電", "TradeValue": "500"}, {"Code": "030001", "Name": "權證", "TradeValue": "88888"}]
    tpex = [{"SecuritiesCompanyCode": "8042", "CompanyName": "金山電", "TransactionAmount": "700"}]
    assert [c for c, _, _ in daytrade_lab.rank_top(twse, tpex, 2)] == ["2330", "8042"]
    good = [("2026-10-%02d" % (1 + k % 28), 0.5 + (k % 3) * 0.1, "target", "X") for k in range(60)]
    res = {"orb_long": {"holds": True, "best": {"stop": 1.0, "target": 2.0}}, "orb_short": {"holds": True, "best": {"stop": 1.0, "target": 2.0}}}
    trades_top = {("orb_long", 1.0, 2.0): good, ("orb_short", 1.0, 2.0): [(d, -r, w, c) for d, r, w, c in good]}
    daytrade_lab.cross_check(res, trades_top, "2026-10-01")
    assert res["orb_long"]["holds"] and res["orb_long"]["top"]["test"]["n"] == 60
    assert res["orb_short"]["holds_watch"] and not res["orb_short"]["holds"]     # 前 200 大是虧的就不算站得住
    res2 = {"orb_long": {"holds": True, "best": {"stop": 1.0, "target": 2.0}}}
    daytrade_lab.cross_check(res2, None, "2026-10-01")
    assert res2["orb_long"]["holds"] and res2["orb_long"]["top"] is None           # 交叉驗證失敗時保留原結果


def test_news_normalize_merge_and_recent():
    import news
    row = {"出表日期": "1151027", "發言日期": "1151027", "發言時間": "93005", "公司代號": "2303", "公司名稱": "聯電",
           "主旨 ": "公告本公司董事會決議\r\n發放現金股利", "符合條款": "第14款"}
    it = news.normalize(row, "公司代號")
    assert it["date"] == "2026-10-27" and it["time"] == "09:30" and it["title"] == "公告本公司董事會決議 發放現金股利"
    assert it["cat"] == "股利增減資" and news.categorize("自結九月營收") == "財報營收" and news.categorize("其他事項") == "其他"
    old = [dict(it, date="2026-10-10"), dict(it)]
    merged = news.merge(old, [dict(it), news.normalize(dict(row, 公司代號="9999"), "公司代號")], {"2303"}, "2026-10-27")
    assert len(merged) == 1                                       # 去重、過舊與非自選股都去掉
    assert news.recent_for({"items": merged}, "2303", "2026-10-28")[0]["title"].startswith("公告")
    assert news.recent_for({"items": merged}, "2303", "2026-11-05") == []


def test_web_pages_javascript_syntax(tmp_path):
    """網頁的 JavaScript 有語法錯誤時整頁會空白,Python 測試抓不到;有 node 就用 node --check 檢查每一段 <script>。"""
    import re
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("沒有安裝 node(GitHub Actions 的 ubuntu 環境有)")
    for page in ("index.html", "game.html"):
        html = open(os.path.join(ROOT, page), encoding="utf-8").read()
        scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
        assert scripts, page
        for i, js in enumerate(scripts):
            f = tmp_path / f"{page}.{i}.js"
            f.write_text(js, encoding="utf-8")
            r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
            assert r.returncode == 0, f"{page} 第 {i + 1} 段 script 語法錯誤:\n{r.stderr[-800:]}"


# ---------- overnight.py ----------

import overnight  # noqa: E402


def test_overnight_classify():
    assert overnight.classify(0.5, -2) == ("順風", "夜盤")
    assert overnight.classify(-0.3, 3) == ("逆風", "夜盤")
    assert overnight.classify(0.1, None) == ("中性", "夜盤")
    assert overnight.classify(None, 1.2) == ("順風", "費半")
    assert overnight.classify(None, None) == (None, None)


def _on_rows(txf_ts):
    us_ts = int(dt.datetime(2026, 10, 28, 4, 0, tzinfo=TPE).timestamp())
    return {
        overnight.TXF: {"6": 46300, "56": -0.5, "21": 46532, "200007": txf_ts},
        "GI:SOX:INDEX": {"6": 11000, "56": 1.5, "200007": us_ts},
        overnight.TSM: {"6": 400, "56": 1, "200007": us_ts},
        overnight.FX: {"6": 32.5},
        overnight.TW2330: {"6": 2400, "21": 2380, "200007": int(dt.datetime(2026, 10, 27, 13, 30, tzinfo=TPE).timestamp())},
    }


def test_overnight_snapshot_preopen_uses_night_session():
    night = int(dt.datetime(2026, 10, 28, 5, 0, tzinfo=TPE).timestamp())
    s = overnight.build_snapshot(_on_rows(night), dt.datetime(2026, 10, 28, 7, 0, tzinfo=TPE))
    assert s["wind"] == "逆風" and s["basis"] == "夜盤"
    assert s["diverge"] is True                      # 費半 +1.5% 和夜盤 -0.5% 方向相反
    assert s["adr"]["premium"] == round((400 * 32.5 / 5 / 2400 - 1) * 100, 2)


def test_overnight_snapshot_after_open_drops_txf():
    # 08:45 之後(或下午新的夜盤)台指期報價不是昨晚的夜盤,不能拿來判斷
    evening = int(dt.datetime(2026, 10, 28, 16, 0, tzinfo=TPE).timestamp())
    s = overnight.build_snapshot(_on_rows(evening), dt.datetime(2026, 10, 28, 9, 10, tzinfo=TPE))
    assert s["items"][0]["pct"] is None
    assert s["basis"] == "費半" and s["wind"] == "順風"


def test_overnight_stats_groups_by_wind():
    log = {"days": {
        "2026-10-27": {"wind": "順風", "items": [{"code": overnight.TXF, "pct": 0.8}], "outcome": {"gap": 1.0, "oc": -0.2}},
        "2026-10-28": {"wind": "逆風", "items": [{"code": overnight.TXF, "pct": -0.6}], "outcome": {"gap": 0.3}},
        "2026-10-29": {"wind": "中性", "items": []},   # 沒有開盤結果(休市)不算
    }}
    focus = {"days": {"2026-10-27": {"evaluated": True, "picks": {"2330": {"side": "in", "ret": 1.2}, "2303": {"side": "out", "ret": 0.5}}}}}
    st = overnight.stats(log, focus)
    assert st["days"] == 2
    assert st["by"]["順風"]["hit"] == 1 and st["by"]["逆風"]["hit"] == 0
    assert st["by"]["順風"]["f_in"] == {"n": 1, "win": 1, "avg": 1.2}
    assert st["by"]["順風"]["f_out"]["win"] == 0
    assert st["by"]["中性"]["days"] == 0


# ---------- 永豐 1 分K → 5 分K(盤中和回測同一套資料) ----------

import sjquote  # noqa: E402


def test_sj_wall_minute_handles_both_timestamp_styles():
    real = dt.datetime(2026, 10, 29, 9, 1, tzinfo=TPE).timestamp()
    fake_utc = dt.datetime(2026, 10, 29, 9, 1, tzinfo=dt.timezone.utc).timestamp()   # 永豐:台北時間當成 UTC
    assert sjquote._wall_minute(real * 1e9) == ("2026-10-29", 541)
    assert sjquote._wall_minute(fake_utc * 1e9) == ("2026-10-29", 541)
    assert sjquote._wall_minute(None) is None


def test_sj_to_5m_end_and_start_labels():
    rows = [[541, 10, 11, 9.5, 10.5, 3], [545, 10.5, 12, 10, 11, 2], [546, 11, 11.2, 10.8, 11.1, 4], [810, 11, 11, 11, 11, 9]]
    assert sjquote.guess_end_label({"A": rows})
    b = sjquote.to_5m(rows, True)
    assert [x["t"] for x in b] == ["09:00", "09:05", "13:25"]     # 09:05 那分鐘(09:04~09:05)還屬於 09:00 那根;13:30 收盤併進 13:25
    assert b[0] == {"t": "09:00", "o": 10, "h": 12, "l": 9.5, "c": 11, "v": 5}
    assert not sjquote.guess_end_label({"A": [[540, 1, 1, 1, 1, 1]]})
    assert [x["t"] for x in sjquote.to_5m([[540, 1, 1, 1, 1, 1], [545, 1, 1, 1, 1, 1]], False)] == ["09:00", "09:05"]


def test_load_k5_only_completed_bars_and_refetch_on_new_bar(tmp_path, monkeypatch):
    monkeypatch.setattr(intraday, "K1M_CACHE_FILE", str(tmp_path / "k1m.json"))
    calls = []

    def fake(codes, day, budget=40):
        calls.append(list(codes))
        return {c: [[m, 100, 101, 99, 100, 10] for m in range(541, 541 + 22)] for c in codes}   # 09:01~09:22 的 1 分K

    monkeypatch.setattr(intraday.sjquote, "kbars_1m", fake)
    t = dt.datetime(2026, 10, 29, 9, 20, 40, tzinfo=TPE)
    out = intraday.load_k5(t, ["A"])
    bars, nxt, ksum = out["A"]
    assert [b["t"] for b in bars] == ["09:00", "09:05", "09:10", "09:15"] and nxt == 100 and ksum == 220
    intraday.load_k5(t + dt.timedelta(minutes=2), ["A"])            # 09:22:40 還沒有新的收完的根 → 用暫存
    assert len(calls) == 1
    intraday.load_k5(t + dt.timedelta(minutes=5), ["A"])            # 09:25:40 → 09:20 那根收完,重抓
    assert len(calls) == 2
    assert intraday.load_k5(dt.datetime(2026, 10, 29, 9, 4, tzinfo=TPE), ["A"]) == {}   # 第一根還沒收完


def _k5_case(tmp_path, monkeypatch):
    for k in ("HISTORY_FILE", "DAYTRADE_FILE", "CURVE_FILE"):
        monkeypatch.setattr(intraday, k, str(tmp_path / f"{k}.json"))
    (tmp_path / "DAYTRADE_FILE.json").write_text(json.dumps({"rules": {}}))
    (tmp_path / "HISTORY_FILE.json").write_text(json.dumps({"date": "2026-01-05", "points": []}))
    px = [100, 100.2, 100.1] + [100.3] * 3 + [102.0] + [102.5] * 8 + [101.4, 101.0, 101.3] + [101.9] * 6 + [99.0] * 6 + [98.0] * 20
    return [dict(b, v=300) for b in _5m_day(px)]


def _q(last, vol):
    return {"code": "2455", "name": "全新", "last": last, "prev_close": 100, "pct": last - 100, "open": 100, "high": 103,
            "low": 97, "volume": vol, "pace": None, "upper_limit": 110, "lower_limit": 90}


def test_compute_bias_k5_matches_backtest_triggers(tmp_path, monkeypatch):
    bars = _k5_case(tmp_path, monkeypatch)
    prev = [f"2025-12-{d}" for d in range(22, 28)]                # 前 6 天每根 100 張,今天 300 張 → 兩邊量能倍數都是 3
    ctx = daytrade_lab.score_context({"2455": {**{d: _5m_day([100] * 54) for d in prev}, "2026-01-05": bars}},
                                     {"2455": {"2025-12-21": 100, **{d: 100 for d in prev}}}, {})
    curves = {d: {"2455": [min(1.0, g / 270) for g in intraday.CURVE_GRID]} for d in prev}
    (tmp_path / "CURVE_FILE.json").write_text(json.dumps({"days": curves}))
    official = {"bars": {"2455": [{"d": d, "v": 5400} for d in prev]}}
    ser = daytrade_lab.score_series("2455", "2026-01-05", bars, 100, ctx)
    want = {}
    for rk in bias.RULE_SIDE:
        i = daytrade_lab.entry_index(rk, bars, 100, False, ser)
        if i is not None:
            want[rk] = (bars[i - 1]["t"], bars[i]["o"])
    assert want, "測試資料至少要觸發一條規則"
    # 盤中分兩次跑(中間斷一次),結果要和回測一次跑完一樣
    t1 = dt.datetime(2026, 1, 5, 10, 30, 40, tzinfo=TPE)
    n1 = next(i for i, b in enumerate(bars) if b["t"] == "10:25") + 1
    intraday.compute_bias(t1, [], [_q(bars[n1 - 1]["c"], 300 * n1)], official, {"2455": (bars[:n1], bars[n1]["o"], 300 * n1)})
    t2 = dt.datetime(2026, 1, 5, 13, 0, 40, tzinfo=TPE)
    b = intraday.compute_bias(t2, [], [_q(bars[-1]["c"], 300 * len(bars))], official, {"2455": (bars, None, 300 * len(bars))})
    st = json.loads((tmp_path / "HISTORY_FILE.json").read_text())["bias_state"]["2455"]
    got = {rk: (tg["t"], tg["price"]) for rk, tg in st["trig"].items()}
    assert got == want
    it = b["items"][0]
    assert it["trig_src"] == "k5" and b["bar_src"]["k5"] == 1
    hh, mm = map(int, st["trig"][it["rule"]]["t"].split(":"))
    assert it["trig_t"] == f"{hh + (mm + 5) // 60:02d}:{(mm + 5) % 60:02d}"    # 畫面顯示那根收盤的時間


def test_compute_bias_k5_orb_replaces_snapshot_orb(tmp_path, monkeypatch):
    bars = _k5_case(tmp_path, monkeypatch)
    t = dt.datetime(2026, 1, 5, 9, 40, 40, tzinfo=TPE)
    intraday.compute_bias(t, [], [_q(102.5, 2400)], None, {"2455": (bars[:8], None, 2400)})
    h = json.loads((tmp_path / "HISTORY_FILE.json").read_text())
    assert h["orb"]["2455"] == [pytest.approx(100.2 * 1.001), pytest.approx(100 * 0.999)] and h["orb_src"] == "k5"


# ---------- loop.py(盤中常駐迴圈) ----------

import loop  # noqa: E402


def test_loop_slots_align_to_5m_close():
    at = lambda h, m, s=0: dt.datetime(2026, 10, 29, h, m, s, tzinfo=TPE)
    assert loop.next_slot(at(9, 3, 10)) == at(9, 3, 35)               # 09:15 前每分鐘
    assert loop.next_slot(at(9, 3, 40)) == at(9, 4, 35)
    assert loop.next_slot(at(10, 10, 40)) == at(10, 12, 35)           # 之後:每根 5 分K 收盤後 35 秒 + 中間一輪
    assert loop.next_slot(at(10, 12, 40)) == at(10, 15, 35)
    assert loop.next_slot(at(10, 15, 35)) == at(10, 17, 35)
    assert loop.next_slot(at(13, 39, 0)) == at(13, 40, 35)            # 超過 13:40,呼叫端會結束迴圈
    assert loop.OFFSET_SEC > intraday.K5_SETTLE_SEC                    # 要等 5 分K 收完才跑


def test_loop_fresh_intraday_keeps_sjquote_connection():
    sjquote._api = marker = object()
    try:
        old = sys.modules["intraday"]
        new = loop.fresh_intraday()
        assert new is not old and sys.modules["sjquote"] is sjquote and new.sjquote._api is marker
    finally:
        sjquote._api = None


def test_loop_stamp_only_changes_with_new_data(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "intraday_latest.json").write_text(json.dumps({"checked_at": "a", "data_as_of": "b", "x": 1}))
    loop.write_stamp()
    p = tmp_path / "data" / "stamp.json"
    assert json.loads(p.read_text()) == {"checked_at": "a", "data_as_of": "b"}
    m = p.stat().st_mtime_ns
    loop.write_stamp()
    assert p.stat().st_mtime_ns == m


def test_sjquote_relogin_after_repeated_failures():
    class Api:
        out = False
        def logout(self): Api.out = True
    sjquote._api, sjquote._fails = Api(), 0
    try:
        sjquote._broken()
        assert sjquote._api is not None
        sjquote._broken()
        assert sjquote._api is None and Api.out
    finally:
        sjquote._api, sjquote._fails = None, 0


# ---------- team.py(六個觀測員每日紀錄) ----------

import team  # noqa: E402


def _team_result(last, pace=3.0, news=None, flags=None):
    q = {"code": "2455", "name": "全新", "last": last, "prev_close": 100, "pct": last - 100, "open": 100, "high": max(last, 101),
         "low": min(last, 99), "volume": 5000, "pace": pace, "upper_limit": 110, "lower_limit": 90, "news": news or [], "flags": flags,
         "cdp": {"cdp": 100, "nh": 104, "nl": 98, "reliability": "ok"}, "swing": {"adr5": 4.0}}
    return {"data_as_of": "2026-10-29T10:00:00+08:00", "watchlist_quotes": [q, dict(q, code="2330", pct=0.8, last=100.8, pace=1.0)],
            "indices": [{"code": "TWS:TSE01:INDEX", "last": 101, "open": 100, "pct": 1.2}], "bias": {"items": []}, "opening_alerts": {}}


def test_team_check_hard_limit_and_verdicts():
    r = _team_result(101)
    mood = team.market_mood(r["indices"], r["watchlist_quotes"])
    t = team.team_check(r["watchlist_quotes"][0], mood, [])
    assert t["side"] == 1 and [c["r"] for c in t["ck"]] == list(team.ROLES) and t["vc"] == "na"   # 沒有站得住的規則 → 僅觀察
    bad = team.team_check(dict(r["watchlist_quotes"][0], flags={"daytrade": False}), mood, [])
    assert bad["vc"] == "bad" and bad["ck"][4]["s"] == "bad"
    ok = team.team_check(r["watchlist_quotes"][0], mood, [{"code": "2455", "fresh": True, "holds": True, "side": "long", "rule_name": "X"}])
    assert ok["vc"] == "ok"


def test_team_record_keeps_first_pick_and_logs_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(team, "TEAM_DIR", str(tmp_path))
    monkeypatch.setattr(team, "INDEX_FILE", str(tmp_path / "index.json"))
    at = lambda h, m: dt.datetime(2026, 10, 29, h, m, tzinfo=TPE)
    team.record(at(10, 0), _team_result(101))
    team.record(at(10, 5), _team_result(101.5))
    day = team.record(at(10, 10), _team_result(102, flags={"disposal": {"measure": "分盤"}}))
    pk = day["picks"]["2455"]
    assert "2330" not in day["picks"]                                     # 量能 1 倍、沒事件 → 機會掃描員不挑
    assert pk["first"]["t"] == "10:00" and pk["first"]["price"] == 101 and pk["n"] == 3
    vcs = [h["vc"] for h in pk["hist"]]                                   # 結論有變才記一筆
    assert vcs[0] == "na" and vcs[-1] == "bad" and len(vcs) == len(set(vcs))
    assert pk["now"]["move"] == pytest.approx((102 / 101 - 1) * 100, abs=1e-3)
    k5 = {"2455": ([{"t": "10:00", "h": 103, "l": 100.5}, {"t": "10:05", "h": 102.5, "l": 100}], None, 0)}
    day = team.record(at(13, 31), _team_result(102.5), k5)
    pk = day["picks"]["2455"]
    assert pk["mfe"] == pytest.approx((103 / 101 - 1) * 100, abs=1e-3) and pk["mae"] == pytest.approx(-(1 - 100 / 101) * 100, abs=1e-3)
    assert pk["close"]["net"] == pytest.approx((102.5 / 101 - 1) * 100 - team.COST_PCT, abs=1e-3) and day["closed"]
    idx = json.loads((tmp_path / "index.json").read_text())["days"]["2026-10-29"]
    assert idx["picks"] == 1 and idx["n_close"] == 1 and idx["closed"]


# ---------- market.py(全市場雷達)與 journal.py(我的交易) ----------

import journal  # noqa: E402
import market  # noqa: E402


def _mkt_live(n=80):
    rows, pace = {}, {}
    for i in range(n):
        c = str(1101 + i)
        pct = (i - n / 2) / 10                                    # -4% ~ +4%
        last = round(100 * (1 + pct / 100), 2)
        hi, lo = (last, 99.0) if pct > 0 else (101.0, last)       # 漲的收在最高、跌的收在最低
        rows[c] = {"6": last, "21": 100, "56": pct, "19": 100, "12": hi, "13": lo, "200013": 5000, "200009": f"股{c}",
                   "200067": 5000 * 1000 * 100, "75": 110, "76": 90, "cat": "24" if i % 2 else "28", "dt": "No" if i == n - 1 else "Yes"}
        pace[c] = 3.0
    return {"rows": rows, "uni": {c: 1000 for c in rows}, "hlc": {c: [101, 99, 100, ""] for c in rows}, "pace": pace}


def test_market_run_stats_picks_and_record(tmp_path, monkeypatch):
    for k, v in (("OUT_FILE", "r.json"), ("DAY_DIR", "m"), ("INDEX_FILE", "m/index.json"), ("STATE_FILE", "s.json")):
        monkeypatch.setattr(market, k, str(tmp_path / v))
    idx = [{"code": "TWS:TSE01:INDEX", "last": 100.5, "open": 100, "pct": 0.5}]
    at = lambda h, m: dt.datetime(2026, 10, 29, h, m, 35, tzinfo=TPE)
    flags = {"1102": {"disposal": {"measure": "分盤"}}}
    out = market.run(at(10, 0), _mkt_live(), idx, {"1101"}, lambda c: {}, lambda c: flags)
    st = out["stats"]
    assert st["n"] == 80 and st["up"] + st["down"] + st["flat"] == 80 and sum(st["bands"].values()) == 80
    assert out["longs"] and all(p["score"] >= market.PICK_MIN for p in out["longs"])
    assert all(p["score"] <= -market.PICK_MIN for p in out["shorts"])
    assert "1180" not in {p["code"] for p in out["longs"] + out["shorts"]}          # 不能當沖的不推薦
    assert all(len(p["ck"]) == 5 and p["ck"][1]["s"] == "na" for p in out["longs"])  # 全市場沒查新聞 → 消息員中性
    out2 = market.run(at(13, 31), _mkt_live(), idx, {"1101"}, lambda c: {}, lambda c: flags)
    day = json.loads((tmp_path / "m" / "2026-10-29.json").read_text())
    top = out2["longs"][0]["code"]
    assert day["closed"] and day["picks"][top]["first"]["t"] == "10:00" and day["picks"][top]["close"]["net"] == pytest.approx(-team.COST_PCT)
    assert json.loads((tmp_path / "m" / "index.json").read_text())["days"]["2026-10-29"]["long"]["n_close"] >= 1


def test_journal_costs_and_recommendation_match(tmp_path, monkeypatch):
    gross, fee, tax, net = journal.costs("long", 100, 102, 1000)
    assert gross == 2000 and fee == 39 + 40 and tax == 153 and net == 2000 - 79 - 153       # 0.1425% 打 2.8 折,買賣各一次,無條件捨去
    assert journal.costs("long", 579, 580, 1000) == (1000, 462, 870, -332)                 # 永豐對帳單:4979 華星光 10/14
    assert journal.costs("long", 10, 10.1, 1000)[1] == 3 + 4                                # 小金額照實收(對帳單沒有 20 元下限)
    g2, f2, t2, n2 = journal.costs("short", 102, 100, 1000)                                 # 做空:102 賣、100 買回
    assert g2 == 2000 and t2 == 153 and n2 == 2000 - f2 - t2
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "market").mkdir(parents=True)
    (tmp_path / "data" / "market" / "2026-10-29.json").write_text(json.dumps({"picks": {"2330": {"name": "台積電", "first": {"t": "09:40", "price": 1000, "side": 1, "vc": "na"}}}}))
    monkeypatch.setattr(journal, "FILE", str(tmp_path / "data" / "journal.json"))
    journal.main(["add", "--date", "2026-10-29", "--code", "2330", "--side", "long", "--entry", "1000", "--exit", "1010", "--qty", "1"])
    journal.main(["add", "--date", "2026-10-29", "--code", "2317", "--side", "short", "--entry", "200", "--exit", "199", "--qty", "2"])
    assert journal.match_rec("2026-10-29", "2330", "long", "09:30") is None                 # 09:30 進場時推薦還沒出現(09:40)
    assert journal.match_rec("2026-10-29", "2330", "long", "09:45")["same_side"]
    t = json.loads((tmp_path / "data" / "journal.json").read_text())["trades"]
    assert t[0]["name"] == "台積電" and t[0]["rec"]["src"] == "market" and t[0]["rec"]["same_side"] and t[1]["rec"] is None
    assert t[1]["shares"] == 2000 and t[0]["net"] == journal.costs("long", 1000, 1010, 1000)[3]
    journal.main(["remove", "1"])
    assert [x["id"] for x in json.loads((tmp_path / "data" / "journal.json").read_text())["trades"]] == [2]


# ---------- 永豐逐筆內外盤、指數、歷史 1 分K 回補(用假的 API,不連線) ----------

import types  # noqa: E402

import sj_backfill  # noqa: E402


def _ns(day, hh, mm, ss=0):
    """永豐時間戳寫法:台北時間當成 UTC。"""
    y, m, d = map(int, day.split("-"))
    return int(dt.datetime(y, m, d, hh, mm, ss, tzinfo=dt.timezone.utc).timestamp() * 1e9)


def test_tick_flow_incremental_and_metrics(monkeypatch):
    day = "2026-10-29"
    ticks = [(_ns(day, 9, 0, 5), 100, 10, 1), (_ns(day, 9, 1, 0), 100, 600, 1), (_ns(day, 9, 2, 0), 99.5, 20, 2), (_ns(day, 9, 3, 0), 99.5, 5, 0)]
    calls = []

    class Api:
        def ticks(self, contract, date, query_type, time_start, time_end):
            calls.append(time_start)
            got = [t for t in ticks if time_start <= sjquote._wall_hms(t[0]) <= time_end]
            return types.SimpleNamespace(ts=[t[0] for t in got], close=[t[1] for t in got], volume=[t[2] for t in got], tick_type=[t[3] for t in got])

    monkeypatch.setattr(sjquote, "_login", lambda: Api())
    monkeypatch.setattr(sjquote, "_contract", lambda api, c: object())
    monkeypatch.setattr(sjquote.time, "sleep", lambda s: None)
    fake_sj = types.SimpleNamespace(constant=types.SimpleNamespace(TicksQueryType=types.SimpleNamespace(RangeTime="RangeTime")))
    monkeypatch.setitem(sys.modules, "shioaji", fake_sj)                 # 測試環境沒有裝永豐套件
    st = sjquote.tick_flow(["A"], day, {}, "09:02:30")
    sjquote.tick_flow(["A"], day, st, "09:03:30")                      # 第二次只補新的,不重複算
    a = st["A"]
    assert calls == ["09:00:00", "09:02:00"] and a["out"] == 610 and a["in"] == 20   # tick_type 0 不計
    assert a["bo"] == 600 and a["bi"] == 0                              # 100 元 × 600 張 = 6000 萬 >= 500 萬算大單
    f = sjquote.flow_metrics(a, 9 * 60 + 4)
    assert f["out"] == pytest.approx(610 / 630 * 100, abs=0.1) and f["big"] == 100


def test_flow_check_warns_only_against_direction():
    base = {"r": "機會", "s": "ok", "t": "量能 3.0 倍"}
    q = {"flow": {"out": 70, "out15": 30, "n15": 500, "big": -40, "big_vol": 100}, "book": {"imb": -55}}
    c = team._flow_check(base, q, 1)                                     # 做多但最近 15 分鐘外盤只有 30%
    assert c["s"] == "warn" and "外盤 30%" in c["t"] and "大單淨賣 40%" in c["t"] and "五檔委賣多 55%" in c["t"]
    assert team._flow_check(base, q, -1)["s"] == "ok"                    # 做空時同一筆資料是順向
    assert team._flow_check(base, {}, 1) == base


def test_backfill_writes_sj_days_with_prev_close_and_shares(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for k in ("BARS_DIR", "BARS_DIR_TOP"):
        monkeypatch.setattr(sj_backfill, k, str(tmp_path / k))
    monkeypatch.setattr(sj_backfill, "TOP_FILE", str(tmp_path / "top.json"))
    monkeypatch.setattr(sj_backfill, "STATUS_FILE", str(tmp_path / "status.json"))
    monkeypatch.setattr(sj_backfill, "TRIED_FILE", str(tmp_path / "tried.json"))
    monkeypatch.setattr(sj_backfill, "watch_codes", lambda: ["2330"])
    (tmp_path / "top.json").write_text(json.dumps({"codes": ["2330", "2317"]}))
    days = ["2026-10-27", "2026-10-28", "2026-10-29"]

    def kb(price):
        ts, o = [], []
        for di, d in enumerate(days):
            for m in range(541, 811):                                   # 09:01 ~ 13:30(結束時間寫法)
                ts.append(_ns(d, m // 60, m % 60))
                o.append(price + di)
        return types.SimpleNamespace(ts=ts, Open=o, High=[x + 0.5 for x in o], Low=[x - 0.5 for x in o], Close=o, Volume=[2] * len(o))

    class Api:
        Contracts = types.SimpleNamespace(Indexs=types.SimpleNamespace(TSE=[types.SimpleNamespace(code="IX0001")]))
        def kbars(self, contract, start, end):
            return kb(20000 if getattr(contract, "code", "") == "IX0001" else 100)
        def usage(self):
            return types.SimpleNamespace(remaining_bytes=400e6, limit_bytes=500e6)

    monkeypatch.setattr(sjquote, "_login", lambda: Api())
    monkeypatch.setattr(sjquote, "_contract", lambda api, c: types.SimpleNamespace(code=c))
    monkeypatch.setattr(sj_backfill.time, "sleep", lambda s: None)
    real_dt = sj_backfill.datetime
    monkeypatch.setattr(sj_backfill, "datetime", types.SimpleNamespace(now=lambda tz: real_dt(2026, 10, 29, 14, 0, tzinfo=tz)))
    assert sj_backfill.main(["--days", "10"]) == 0
    w = json.load(gzip.open(tmp_path / "BARS_DIR" / "2026-10-28.json.gz", "rt"))
    x = w["stocks"]["2330"]
    assert x["s"] == "sj" and x["pd"] == "2026-10-27" and x["pc"] == 100 and len(x["b"]) == 54
    assert x["b"][0][0] == "09:00" and x["b"][0][5] == 5 * 2 * 1000                # 5 根 1 分K × 2 張 → 股
    assert w["idx"][0][0] == "09:00" and len(w["idx"][0]) == 5
    assert not (tmp_path / "BARS_DIR" / "2026-10-27.json.gz").exists()             # 第一天沒有前一日收盤,不存
    assert "2317" in json.load(gzip.open(tmp_path / "BARS_DIR_TOP" / "2026-10-29.json.gz", "rt"))["stocks"]
    st = json.loads((tmp_path / "status.json").read_text())
    assert st["codes_done_now"] == 2 and st["stock_days_now"] == 4
    assert sj_backfill.main(["--days", "10"]) == 0                                  # 已經是永豐的不重抓
    assert json.loads((tmp_path / "status.json").read_text())["codes_already"] == 2
    assert json.loads((tmp_path / "tried.json").read_text()) == {}                 # 每天都有資料,沒有要記的缺天
    archive = daytrade_lab.load_bars_archive(str(tmp_path / "BARS_DIR"))           # 回測讀得到
    assert set(archive) == {"2026-10-28", "2026-10-29"}


# ---------- 三個角色審查後修的 Bug(回歸測試) ----------

def test_market_flip_settles_old_pick_and_no_new_after_1325(tmp_path, monkeypatch):
    for k, v in (("OUT_FILE", "r.json"), ("DAY_DIR", "m"), ("INDEX_FILE", "m/index.json"), ("STATE_FILE", "s.json")):
        monkeypatch.setattr(market, k, str(tmp_path / v))
    at = lambda h, m: dt.datetime(2026, 10, 29, h, m, 35, tzinfo=TPE)
    st = {"up": 1, "down": 1, "avg_pct": 0, "hot": 0, "n_long": 0, "n_short": 0, "mood": None, "cats": []}
    pick = lambda side, px: {"code": "A", "name": "A", "cat": "x", "in_watch": False, "side": side, "last": px, "score": 50 if side == "long" else -50,
                             "vc": "na", "ck": [], "reasons": [], "v": "", "pace": 2, "pct": 0, "stop": 1, "target": 2}
    qs = lambda px: [{"code": "A", "last": px}]
    market.record(at(9, 30), 30, [pick("long", 100)], qs(100), st, {})
    day = market.record(at(12, 0), 180, [pick("short", 95)], qs(95), st, {})
    old = day["picks"]["A#09:30"]
    assert old["close"]["exit"] == "flip" and old["close"]["move"] == pytest.approx(-5.0)     # 做錯的那筆照翻轉價結算
    day = market.record(at(13, 26), 266, [pick("long", 96)], qs(96), st, {})                  # 13:25 後不翻方向
    assert day["picks"]["A"]["first"]["side"] == -1
    day = market.record(at(13, 26), 266, [dict(pick("long", 50), code="B")], qs(96), st, {})
    assert "B" not in day["picks"]                                                           # 13:25 後不新增


def test_team_first_side_zero_restarts_when_direction_appears(tmp_path, monkeypatch):
    monkeypatch.setattr(team, "TEAM_DIR", str(tmp_path))
    monkeypatch.setattr(team, "INDEX_FILE", str(tmp_path / "index.json"))
    at = lambda h, m: dt.datetime(2026, 10, 29, h, m, tzinfo=TPE)
    team.record(at(10, 0), _team_result(100.2))                   # 漲 0.2% → 觀望(方向 0)
    day = team.record(at(10, 5), _team_result(101))
    assert day["picks"]["2455"]["first"]["side"] == 1 and day["picks"]["2455"]["first"]["t"] == "10:05"


def test_short_gap_stop_return_formula():
    bars = [{"t": "09:00", "o": 100, "h": 100, "l": 100, "c": 100}, {"t": "09:05", "o": 103, "h": 103, "l": 103, "c": 103}] + \
           [{"t": f"13:{m:02d}", "o": 103, "h": 103, "l": 103, "c": 103} for m in (15, 20)]
    r, why, _k, _px = daytrade_lab.simulate_detail(bars, 0, "short", 1.0, None)
    assert why == "stop" and r == pytest.approx(-3.0)              # 100 賣、跳空 103 買回 = -3%


def test_to_5m_drops_rows_outside_session():
    rows = [[539, 1, 1, 1, 1, 9], [541, 1, 1, 1, 1, 1], [810, 2, 2, 2, 2, 1], [816, 9, 9, 9, 9, 9], [870, 9, 9, 9, 9, 9]]
    b = sjquote.to_5m(rows, True)
    assert [x["t"] for x in b] == ["09:00", "13:25"] and sum(x["v"] for x in b) == 2


def test_loop_git_timeout_returns_failure_and_timeout_not_swallowed(monkeypatch):
    def boom(*a, **k):
        raise loop.subprocess.TimeoutExpired("git", 90)
    monkeypatch.setattr(loop.subprocess, "run", boom)
    assert loop.git("pull") == 1
    assert issubclass(loop.RoundTimeout, BaseException) and not issubclass(loop.RoundTimeout, Exception)


def test_watch_books_does_not_unsubscribe_other_callers(monkeypatch):
    subs, unsubs = [], []

    class Quote:
        def set_on_bidask_stk_v1_callback(self, cb): pass
        def subscribe(self, k, **kw): subs.append(k.code)
        def unsubscribe(self, k, **kw): unsubs.append(k.code)

    api = types.SimpleNamespace(quote=Quote())
    monkeypatch.setattr(sjquote, "_login", lambda: api)
    monkeypatch.setattr(sjquote, "_contract", lambda a, c: types.SimpleNamespace(code=c))
    fake_sj = types.SimpleNamespace(constant=types.SimpleNamespace(QuoteType=types.SimpleNamespace(BidAsk=1), QuoteVersion=types.SimpleNamespace(v1=1)))
    monkeypatch.setitem(sys.modules, "shioaji", fake_sj)
    monkeypatch.setattr(sjquote, "_SUB_API", None)
    sjquote.watch_books(["A", "B"])                               # 自選股候選
    sjquote.watch_books(["C"])                                    # 同一輪全市場推薦
    assert sorted(subs) == ["A", "B", "C"] and unsubs == []        # 不能把自選股的退訂掉
    sjquote._SEEN["A"] -= sjquote.BOOK_KEEP + 1
    sjquote.watch_books(["C"])
    assert unsubs == ["A"]                                         # 太久沒人要才退訂
    sjquote._SUBS.clear(); sjquote._SEEN.clear(); sjquote._BOOK.clear()


def test_curve_from_k1m_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(intraday, "K1M_CACHE_FILE", str(tmp_path / "k1m.json"))
    rows = [[m, 1, 1, 1, 1, 10] for m in range(541, 811)]          # 09:01~13:30 每分鐘 10 張
    (tmp_path / "k1m.json").write_text(json.dumps({"date": "2026-10-29", "rows": {"A": rows}, "upto": {"A": 270}}))
    c = intraday.curves_from_k1m("2026-10-29")["A"]
    g = intraday.CURVE_GRID
    assert c[g.index(30)] == pytest.approx(30 / 270, abs=1e-3) and c[-1] == 1.0


def test_trade_plan_ticks_structure_stop_and_target(monkeypatch):
    monkeypatch.setattr(team, "_rule_params", lambda side: (1.0, 2.0) if side > 0 else (1.5, 3.0))
    q = {"code": "2330", "last": 1050}
    long = team.trade_plan(q, 1, {"均價線": 1042, "開盤區間": 1030, "支撐": 1000, "壓力": 1070})
    assert long["stop"] == 1040 and long["stop_src"] == "固定 1%"             # 1039.5 往現價取到 5 元檔位,虧損不放大
    assert long["target"] == 1070 and long["target_src"] == "前面的壓力"      # 固定 2% 是 1071,前面 1070 有壓力先到那
    assert long["chase"] == 1055 and long["rr"] == pytest.approx(2.0)          # 至少多一檔
    short = team.trade_plan(q, -1, {"均價線": 1056, "支撐": 1035})
    assert short["stop"] == 1060 and short["stop_src"] == "均價線上一檔"        # 1056 + 一檔 5 = 1061 比固定 1.5% 近,取到 1060
    assert short["target"] == 1035 and short["chase"] == 1045
    near = team.trade_plan({"code": "3105", "last": 487.5}, 1, {"均價線": 486})  # 均價線只差 0.3%,太近不拿來當停損
    assert near["stop_src"] == "固定 1%" and near["stop"] == 483
    assert team.trade_plan(q, 0, {}) is None
