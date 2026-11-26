import papertrade as P

BARS = [["09:00", 100, 101, 99, 100, 0], ["09:05", 100, 100.5, 98.9, 99, 0], ["09:10", 99, 103.5, 99, 103, 0],
        ["13:25", 103, 103, 102, 102.5, 0]]


def test_long_stop_skips_entry_bar():
    # 09:00 那根低點 99 在進場前,不算;09:05 跌破 99 才停損
    assert P.simulate({"t": "09:02", "side": 1, "entry": 100, "stop": 99, "target": 103}, BARS) == (99, "09:05", "停損")


def test_short_target_and_wrong_side_target_holds_to_close():
    assert P.simulate({"t": "09:02", "side": -1, "entry": 100, "stop": 104, "target": 99}, BARS) == (99, "09:05", "停利")
    assert P.simulate({"t": "09:02", "side": 1, "entry": 100, "stop": 90, "target": 99.5}, BARS) == (102.5, "13:25", "收盤")


def test_run_day_respects_limit_and_one_per_code():
    pick = lambda t, px: {"name": "", "first": {}, "hist": [{"t": t, "vc": "warn", "side": 1, "price": px, "plan": {"stop": 1}},
                                                            {"t": t, "vc": "na", "side": 1, "price": px, "plan": {"stop": px * 0.99, "target": px * 1.03}}]}
    picks = {"A": pick("09:02", 100), "B": pick("09:03", 400), "C": pick("09:04", 250), "D": pick("09:05", 200)}
    d = P.run_day("x", picks, {k: BARS for k in picks})
    assert [t["code"] for t in d["trades"]] == ["A", "B", "D"]   # C 會讓額度超過 90 萬就跳過,D 剛好補滿
    assert d["used"] <= P.LIMIT and d["trades"][0]["lots"] == 3
