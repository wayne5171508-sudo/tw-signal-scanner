#!/usr/bin/env python3
"""
爆量雷達 - 還原權值(除息、配股、減資、分割)共用函式。

cnyes 日K 沒有還原權值:除息、配股、減資那天的跳空會被當成真的漲跌,往前的高低點、均線、量能密集區也會偏掉。
盤中 intraday.py(支撐壓力、均線、觸及回測)和收盤後 bear_signals.py / daytrade_lab.py(利空回測、當沖回測)
都用這一份,兩邊算出來的價位才會一致。事件來源是 data/corp_actions.json(bear_signals.py 每天收盤後向 Yahoo 更新)。
"""

import json
import os

CORP_FILE = os.path.join("data", "corp_actions.json")
SELF_SPLIT_LO, SELF_SPLIT_HI = 0.5, 2.0   # 單日收盤比前一天低於一半或高於兩倍,當成分割
EX_TODAY_MIN, EX_TODAY_MAX = 0.4, 2.5     # 今天除權息:報價昨收(參考價)÷ 日K 最後收盤,在這個範圍內才當成除權息


def load_events(path=None):
    """回傳 {code: {"div": {...}, "split": {...}}};檔案不存在或壞掉回傳 {}。"""
    path = path or CORP_FILE
    try:
        with open(path, encoding="utf-8") as f:
            return (json.load(f) or {}).get("events") or {}
    except (OSError, ValueError):
        return {}


def adjust_bars(b, ev):
    """往回還原權值(原地修改,回傳事件清單)。ev={"div":{日期:每股現金}, "split":{日期:新股數/舊股數}}。
    除息:之前價格乘 (昨收-股息)/昨收。配股/減資/分割:之前價格除以股數比、成交量乘以股數比。
    事件不在 Yahoo 但單日收盤 < 一半或 > 兩倍的,也當分割。事件當天標 event=True,不判斷訊號。"""
    ev = ev or {}
    applied = []
    for i in range(1, len(b)):
        d, pc = b[i]["d"], b[i - 1]["c"]
        pf, vf = 1.0, 1.0
        if d in (ev.get("split") or {}):
            r = ev["split"][d]
            if r > 0:
                pf, vf = 1 / r, r
        if d in (ev.get("div") or {}):
            amt = ev["div"][d]
            if 0 < amt < pc:
                pf *= (pc - amt) / pc
        if pf == 1.0:
            move = b[i]["c"] / pc
            if move < SELF_SPLIT_LO or move > SELF_SPLIT_HI:
                pf, vf = move, 1 / move
        if pf != 1.0:
            for x in b[:i]:
                for k in ("o", "h", "l", "c"):
                    if x.get(k):
                        x[k] *= pf
                if vf != 1.0 and x.get("v"):
                    x["v"] *= vf
            b[i]["event"] = True
            applied.append({"d": d, "price_factor": round(pf, 4), "vol_factor": round(vf, 4)})
    return applied


def adjust_to_reference(b, ref_close):
    """今天除權息/減資時,報價的昨收是參考價,和日K 最後一根收盤對不上。用 參考價 ÷ 最後收盤 把整段往回調整
    (原地修改)。股數變動比較大(比例超過 10%,通常是配股、減資)時成交量反向調整。回傳調整比例,沒調整回傳 None。"""
    if not b or not ref_close or not b[-1].get("c"):
        return None
    f = ref_close / b[-1]["c"]
    if not (EX_TODAY_MIN <= f <= EX_TODAY_MAX):
        return None
    vf = 1 / f if abs(f - 1) > 0.1 else 1.0
    for x in b:
        for k in ("o", "h", "l", "c"):
            if x.get(k):
                x[k] *= f
        if vf != 1.0 and x.get("v"):
            x["v"] *= vf
    return f
