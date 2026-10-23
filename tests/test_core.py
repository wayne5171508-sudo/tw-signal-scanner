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
    assert st["n"] == 1 and st["rules"]["score_short"]["avg"] == pytest.approx(1.7) and st["rules"]["score_short"]["backtest_test_avg"] == -0.2


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
