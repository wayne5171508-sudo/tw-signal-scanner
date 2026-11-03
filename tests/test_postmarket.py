"""跨午夜的交易日、tech_score 標籤邊界、永豐查詢截止、盤後寫檔不再覆蓋。全部離線。"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import daytrade_lab  # noqa: E402
import health
import intraday
import scan
import sjquote
import tradingday

TPE = ZoneInfo("Asia/Taipei")


def _write(d, name, obj):
    p = os.path.join(d, name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False)


def at(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=TPE)


# ---------- 交易日(Asia/Taipei) ----------

def test_clock_must_be_taipei():
    with pytest.raises(ValueError):
        tradingday.day_is_closed("2026-11-02", datetime(2026, 11, 3, 0, 23))


def test_completed_session_survives_midnight():
    late = at(2026, 11, 3, 0, 23)          # 11/02 的 bear 實際是這時才跑
    assert tradingday.day_is_closed("2026-11-02", late)
    assert tradingday.completed_bar_date(late, "2026-11-02") == "2026-11-02"
    assert not tradingday.day_is_closed("2026-11-03", late)
    assert tradingday.completed_bar_date(late, "2026-11-03") is None
    # 13:30 收盤;當沖沿用 13:35 才把今天的 5 分K當成收完
    assert not tradingday.day_is_closed("2026-11-02", at(2026, 11, 2, 13, 29))
    assert tradingday.day_is_closed("2026-11-02", at(2026, 11, 2, 13, 30))
    assert not tradingday.day_is_closed("2026-11-02", at(2026, 11, 2, 13, 34), tradingday.DAYTRADE_CLOSE_MINUTE)
    assert tradingday.day_is_closed("2026-11-02", at(2026, 11, 2, 13, 35), tradingday.DAYTRADE_CLOSE_MINUTE)


def test_month_boundary_and_fmtqik_parse():
    assert tradingday.month_starts(at(2026, 12, 1, 2, 0)) == ["20261201", "20261101"]
    days = tradingday.parse_fmtqik({"data": [["115/11/02", "1"], ["115/10/30", "1"], ["不是日期"]]})
    assert days == ["2026-11-02", "2026-10-30"]
    assert tradingday.parse_roc_date("115/11/2") == "2026-11-02"
    assert tradingday.latest_completed(at(2026, 11, 3, 6, 26), days) == "2026-11-02"
    assert tradingday.latest_completed(at(2026, 11, 2, 10, 0), days) == "2026-10-30"
    assert tradingday.latest_completed(at(2026, 11, 2, 14, 30), days) == "2026-11-02"


def test_fmtqik_fetch_looks_at_previous_month():
    seen = []

    def fetch(url):
        seen.append(url)
        if "date=20261101" in url:
            return {"data": [["115/11/02", "9"]]}
        return {"data": [["115/10/30", "9"]]}

    assert tradingday.latest_completed_trading_day(at(2026, 11, 3, 6, 26), fetch=fetch) == "2026-11-02"
    assert any("date=20261101" in u for u in seen)
    assert any("date=20261001" in u for u in seen)


def test_postmarket_plan_late_run_and_holiday_idempotent():
    days = ["2026-10-30", "2026-11-02"]
    # 11/03 00:23:成績單還停在 10/30,要重算 11/02,不能因為「今天不是交易日」略過
    plan = tradingday.postmarket_plan(at(2026, 11, 3, 0, 23), days, "2026-10-30", "2026-10-30", True)
    assert plan == {"target": "2026-11-02", "bear_skip": "0", "daytrade_skip": "0"}
    # 非交易平日,前一天已經算完:不再寫
    holiday = tradingday.postmarket_plan(at(2026, 10, 23, 23, 10), ["2026-10-22"], "2026-10-22", "2026-10-22", True)
    assert holiday["target"] == "2026-10-22"
    assert holiday["bear_skip"] == "1" and holiday["daytrade_skip"] == "1"
    # 成績單好了但 5 分K 還沒存,當沖還是要跑
    partial = tradingday.postmarket_plan(at(2026, 11, 3, 0, 23), days, "2026-11-02", "2026-10-30", False)
    assert partial["bear_skip"] == "1" and partial["daytrade_skip"] == "0"
    forced = tradingday.postmarket_plan(at(2026, 11, 3, 0, 23), days, "2026-11-02", "2026-11-02", True, force=True)
    assert forced["bear_skip"] == "0" and forced["daytrade_skip"] == "0"


def test_bear_decide_update_across_midnight():
    import bear_signals
    late = at(2026, 11, 3, 0, 23)
    assert bear_signals.decide_update(late, "2026-11-02", "2026-10-30")[0]
    assert not bear_signals.decide_update(late, "2026-11-02", "2026-11-02")[0]
    assert not bear_signals.decide_update(at(2026, 11, 2, 10, 0), "2026-11-02", "2026-10-30")[0]
    assert bear_signals.decide_update(at(2026, 11, 2, 14, 30), "2026-11-02", "2026-10-30")[0]
    assert bear_signals.decide_update(late, "2026-11-02", "2026-11-02", force=True)[0]


def test_save_1m_after_midnight_keeps_finished_session(tmp_path, monkeypatch):
    monkeypatch.setattr(daytrade_lab, "BARS1M_DIR", str(tmp_path))
    monkeypatch.setattr(daytrade_lab.bs, "MIN_UNIVERSE", 1)
    saved = daytrade_lab.save_1m(
        {"2026-11-02": {"2330": [[1]]}, "2026-11-03": {"2330": [[1]]}},
        at(2026, 11, 3, 0, 23),
    )
    assert saved == ["2026-11-02"]
    assert os.path.exists(tmp_path / "2026-11-02.json.gz")
    assert not os.path.exists(tmp_path / "2026-11-03.json.gz")


def test_session_already_saved_needs_bars_and_score(tmp_path, monkeypatch):
    monkeypatch.setattr(daytrade_lab, "BARS_DIR", str(tmp_path / "bars"))
    monkeypatch.setattr(daytrade_lab, "OUT_FILE", str(tmp_path / "daytrade.json"))
    assert not daytrade_lab._session_already_saved("2026-11-02")
    os.makedirs(tmp_path / "bars")
    (tmp_path / "bars" / "2026-11-02.json.gz").write_bytes(b"x")
    (tmp_path / "daytrade.json").write_text(json.dumps({"archive": {"last": "2026-10-30"}}), encoding="utf-8")
    assert not daytrade_lab._session_already_saved("2026-11-02")
    (tmp_path / "daytrade.json").write_text(json.dumps({"archive": {"last": "2026-11-02"}}), encoding="utf-8")
    assert daytrade_lab._session_already_saved("2026-11-02")


# ---------- tech_score -5..+5 ----------

def test_tech_score_bullish_path_unchanged_and_bearish_reaches_minus_5():
    hi, hb = scan.tech_score(100, True, 3.0, 2.0, 100, 102)
    assert hi == 5.0
    assert hb == "買超+2、規模最大+0.5、爆量+1.5、量價同步上漲+1"
    # 上漲又賣超:維持舊公式(賣超 -2、放量 +0.8、同步上漲 +1)
    up_sell, ub = scan.tech_score(-10, False, 1.8, 1.0, 100, 101)
    assert up_sell == -0.2
    assert "放量+0.8" in ub and "量價同步上漲+1" in ub
    # 買超但價跌:量能仍加分,再扣背離,數字跟舊公式一樣
    div, db = scan.tech_score(10, False, 3.0, -2.0, 100, 98)
    assert div == 2.0 and "爆量+1.5" in db and "買超但價跌背離-1.5" in db
    lo, lb = scan.tech_score(-100, False, 3.0, -3.0, 100, 97, is_top_seller=True)
    assert lo == -5.0
    assert "賣超-2" in lb and "規模最大賣超-0.5" in lb and "下跌爆量-1.5" in lb and "量價同步下跌-1" in lb
    mild, mb = scan.tech_score(-5, False, None, -0.4, 100, 99.6)
    assert mild == -2.5 and "溫和下跌-0.5" in mb
    mid, _ = scan.tech_score(-5, False, 1.5, -1.0, 100, 99)
    assert mid == -3.8
    assert scan.score_to_tag(lo, False) == "偏空(強)"
    assert scan.score_to_tag(mild, False) == "偏空"
    assert scan.score_to_tag(mid, False) == "偏空"
    assert scan.score_to_tag(div, True) == "中性(警示)"


@pytest.mark.parametrize("score,diverged,tag", [
    (5, False, "偏多(強)"),
    (4, False, "偏多(強)"),
    (3.99, False, "偏多"),
    (2.5, False, "偏多"),
    (2.49, False, "中性偏多(力道待確認)"),
    (1, False, "中性偏多(力道待確認)"),
    (0.99, False, "中性(警示)"),
    (0, False, "中性(警示)"),
    (-0.99, False, "中性(警示)"),
    (-1, False, "中性偏空(力道待確認)"),
    (-2.49, False, "中性偏空(力道待確認)"),
    (-2.5, False, "偏空"),
    (-3.99, False, "偏空"),
    (-4, False, "偏空(強)"),
    (-5, False, "偏空(強)"),
    (5, True, "中性(警示)"),
    (-5, True, "中性(警示)"),
])
def test_score_to_tag_boundaries(score, diverged, tag):
    assert scan.score_to_tag(score, diverged) == tag


# ---------- 盤後掃描不覆蓋、證交所重試 ----------

def test_published_scan_only_when_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(scan, "DATA_DIR", str(tmp_path))
    assert scan.published_scan_text("2026-11-02") is None
    raw = json.dumps({"date": "2026-11-02", "market_status": "OK", "keep": 1}, ensure_ascii=False)
    (tmp_path / "2026-11-02.json").write_text(raw, encoding="utf-8")
    assert json.loads(scan.published_scan_text("2026-11-02"))["keep"] == 1
    (tmp_path / "2026-11-02.json").write_text(
        json.dumps({"date": "2026-11-02", "market_status": "data_not_ready_or_no_trading_day"}), encoding="utf-8")
    assert scan.published_scan_text("2026-11-02") is None


def test_main_does_not_rewrite_existing_scan(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(scan, "DATA_DIR", str(tmp_path))
    body = json.dumps({"date": "2026-11-02", "market_status": "OK", "keep": 7}, ensure_ascii=False, indent=2)
    (tmp_path / "2026-11-02.json").write_text(body, encoding="utf-8")

    def resolve(run_time, max_lookback_days=7):
        return at(2026, 11, 2, 18, 0), [], "OK"

    monkeypatch.setattr(scan, "resolve_latest_trading_day", resolve)
    monkeypatch.setattr(scan, "fetch_mi_index", lambda d: (_ for _ in ()).throw(AssertionError("不該再打 TWSE")))
    scan.main()
    out = capsys.readouterr().out
    assert json.loads(out)["keep"] == 7
    assert json.loads((tmp_path / "2026-11-02.json").read_text(encoding="utf-8"))["keep"] == 7


def test_twse_get_retries_then_succeeds(monkeypatch):
    scan.TWSE_STATS.update({"attempts": 0, "retries": 0, "failures": 0})
    monkeypatch.setattr(scan, "_last_twse_at", 0.0)
    calls = {"n": 0}

    class Resp:
        status_code = 200

        def raise_for_status(self):
            return None

    def get(url, headers=None, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise requests.ConnectionError("reset")
        return Resp()

    monkeypatch.setattr(scan.requests, "get", get)
    monkeypatch.setattr(scan.time, "sleep", lambda s: None)
    assert scan.twse_get("https://example.test").status_code == 200
    assert calls["n"] == 3
    assert scan.TWSE_STATS["retries"] == 2 and scan.TWSE_STATS["failures"] == 0


def test_twse_get_counts_exhausted_failure(monkeypatch):
    scan.TWSE_STATS.update({"attempts": 0, "retries": 0, "failures": 0})
    monkeypatch.setattr(scan, "_last_twse_at", 0.0)
    monkeypatch.setattr(scan.requests, "get", lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("x")))
    monkeypatch.setattr(scan.time, "sleep", lambda s: None)
    with pytest.raises(requests.ConnectionError):
        scan.twse_get("https://example.test")
    assert scan.TWSE_STATS["attempts"] == 3
    assert scan.TWSE_STATS["retries"] == 2 and scan.TWSE_STATS["failures"] == 1


def test_health_records_twse_failures(tmp_path, monkeypatch):
    d = str(tmp_path)
    monkeypatch.setattr(health, "D", d)
    monkeypatch.setattr(health, "OUT", os.path.join(d, "health.json"))
    _write(d, "latest.json", {"twse_fetch": {"attempts": 8, "retries": 2, "failures": 1}, "track_record_stats": {"sample_size": 0}})
    _write(d, "track_record.json", [])
    items = {i["key"]: i for i in health.run(at(2026, 11, 3, 8, 0))["items"]}
    assert not items["twse"]["ok"] and "失敗 1" in items["twse"]["detail"]


# ---------- 永豐查詢截止、開盤略過逐筆 ----------

def test_query_many_abandons_inflight_at_deadline(monkeypatch):
    monkeypatch.setattr(sjquote, "_contract", lambda api, c: c)
    monkeypatch.setattr(sjquote, "_serial_until", 0.0)
    monkeypatch.setattr(sjquote, "ROUND_DEADLINE", None)

    def call(code, k):
        time.sleep(1.5)
        return code

    t0 = time.time()
    got, cut = sjquote._query_many(object(), ["A", "B", "C", "D"], call, 0.35, 0)
    assert cut and len(got) < 4
    assert time.time() - t0 < 1.0


def test_round_deadline_caps_phase_budget(monkeypatch):
    monkeypatch.setattr(sjquote, "_contract", lambda api, c: c)
    monkeypatch.setattr(sjquote, "_serial_until", 0.0)
    monkeypatch.setattr(sjquote, "ROUND_DEADLINE", time.time() + 0.3)

    def call(code, k):
        time.sleep(1.5)
        return code

    try:
        t0 = time.time()
        _got, cut = sjquote._query_many(object(), ["A", "B"], call, 30, 0)
        assert cut and time.time() - t0 < 1.0
    finally:
        sjquote.ROUND_DEADLINE = None


def test_load_flow_skips_ticks_until_0903(monkeypatch):
    called = {"n": 0}

    def tick_flow(*a, **k):
        called["n"] += 1

    monkeypatch.setattr(intraday, "sjquote", type("S", (), {
        "tick_flow": staticmethod(tick_flow),
        "watch_books": staticmethod(lambda codes: {}),
        "flow_metrics": staticmethod(lambda st, m: None),
    })())
    monkeypatch.setattr(intraday, "load_json_safe", lambda *a, **k: {})
    monkeypatch.setattr(intraday, "write_json_atomic", lambda *a, **k: None)
    intraday.load_flow(at(2026, 11, 2, 9, 2), ["2330"])
    assert called["n"] == 0
    intraday.load_flow(at(2026, 11, 2, 8, 55), ["2330"])
    assert called["n"] == 0
    intraday.load_flow(at(2026, 11, 2, 9, 3), ["2330"])
    assert called["n"] == 1


def test_round_budget_is_under_one_minute_slot():
    import loop
    assert loop.ROUND_BUDGET_SECONDS <= 50
    assert loop.ROUND_BUDGET_SECONDS < 60


# ---------- 共用 commit:缺檔不失敗、沒變更不提交 ----------

def test_commit_data_ignores_missing_and_noop(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    env = {**os.environ, "COMMIT_DATA_NO_PUSH": "1"}
    script = os.path.join(ROOT, ".github", "scripts", "commit_data.sh")
    missing = subprocess.run(["bash", script, "nope", "data/missing.json"], cwd=repo, env=env, capture_output=True, text=True)
    assert missing.returncode == 0
    assert "略過不存在" in missing.stdout
    assert subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True).returncode != 0
    (repo / "data").mkdir()
    (repo / "data" / "a.json").write_text("{}\n", encoding="utf-8")
    first = subprocess.run(
        ["bash", script, "first", "data/a.json", "data/nope.json"],
        cwd=repo, env=env, capture_output=True, text=True,
    )
    assert first.returncode == 0, first.stderr
    log = subprocess.run(["git", "log", "--oneline"], cwd=repo, capture_output=True, text=True)
    assert "first" in log.stdout
    again = subprocess.run(["bash", script, "second", "data/a.json"], cwd=repo, env=env, capture_output=True, text=True)
    assert again.returncode == 0
    assert "沒有變更" in again.stdout
    count = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=repo, capture_output=True, text=True)
    assert count.stdout.strip() == "1"
