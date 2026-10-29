#!/usr/bin/env python3
"""
爆量雷達 - 我的交易紀錄(data/journal.json,永久保存,網頁 ai.html「我的交易」顯示)

阿文跟 Claude 說「今天做多 2330,1050 買 1062 賣,1 張」,Claude 用這支程式記下來:
  python journal.py add --date 2026-10-30 --code 2330 --side long --entry 1050 --exit 1062 --qty 1 \
      [--in 09:35] [--out 10:20] [--note "照雷達做多"]
  python journal.py list [--date 2026-10-30]
  python journal.py remove <id>

- 損益:張數 × 1000 股;手續費 0.1425% × FEE_DISCOUNT(買賣各收一次,小數無條件捨去),
  當沖證交稅 0.15%(賣出那一邊收,無條件捨去);做空(先賣後買)一樣算法。股數用 --shares 可以記零股。
  已用 10/29 永豐對帳單驗證:4979 華星光 10/14 579 買、580 賣 1 張 → 手續費 231+231、稅 870、損益 -332,完全一致。
  分好幾次成交的單,手續費是每一筆分開算,對帳單只給平均價,算出來會差 1~2 元;照對帳單補記時用 --net 直接填對帳單的損益。
- 自動對照當天的推薦:全市場雷達(data/market/日期.json)和自選股觀測員(data/team/日期.json)
  有沒有推薦這檔、推薦方向是不是一樣、第一次推薦的時間/價格/結論,存在 rec 裡,網頁用來算「照推薦做」和「自己判斷」的勝率。
"""

import argparse
import json
import math
import os
import sys
from datetime import datetime

FILE = os.path.join("data", "journal.json")
FEE_RATE, FEE_DISCOUNT, FEE_MIN = 0.001425, 0.28, 1      # 阿文的券商手續費 2.8 折;對帳單 7781 每邊只收 11、9 元,沒有 20 元下限
DAYTRADE_TAX = 0.0015


def load():
    try:
        with open(FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"trades": []}


def save(j):
    os.makedirs(os.path.dirname(FILE), exist_ok=True)
    tmp = FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(j, f, ensure_ascii=False, indent=1)
    os.replace(tmp, FILE)


def costs(side, entry, exit_, shares):
    """回傳 (毛損益, 手續費, 證交稅, 淨損益)。做多:entry 買、exit 賣;做空:entry 賣、exit 買。"""
    buy, sell = (entry, exit_) if side == "long" else (exit_, entry)
    gross = (sell - buy) * shares
    fee = sum(max(FEE_MIN, math.floor(p * shares * FEE_RATE * FEE_DISCOUNT + 1e-9)) for p in (buy, sell))   # 券商無條件捨去
    tax = math.floor(sell * shares * DAYTRADE_TAX + 1e-9)
    return round(gross), fee, tax, round(gross - fee - tax)


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def match_rec(date, code, side):
    """當天有沒有推薦這檔(全市場雷達優先,再看自選股觀測員)。"""
    sg = 1 if side == "long" else -1
    for src, path in (("market", os.path.join("data", "market", f"{date}.json")), ("watch", os.path.join("data", "team", f"{date}.json"))):
        picks = (_load_json(path).get("picks") or {})
        cands = [(k, p) for k, p in picks.items() if k.split("#")[0] == code]
        if not cands:
            continue
        same = [(k, p) for k, p in cands if p["first"]["side"] == sg]
        k, p = (same or cands)[0]
        f = p["first"]
        return {"src": src, "same_side": f["side"] == sg, "first_t": f["t"], "first_price": f["price"], "vc": f.get("vc"),
                "score": f.get("score"), "best_vc": p.get("best_vc"), "close_net": (p.get("close") or {}).get("net")}
    return None


def name_of(date, code):
    for path in (os.path.join("data", "market", f"{date}.json"), os.path.join("data", "team", f"{date}.json")):
        p = (_load_json(path).get("picks") or {}).get(code)
        if p and p.get("name"):
            return p["name"]
    q = next((x for x in (_load_json(os.path.join("data", "intraday_latest.json")).get("watchlist_quotes") or []) if x.get("code") == code), None)
    if q:
        return q.get("name")
    h = (_load_json(os.path.join("data", "universe.json")).get("hlc") or {}).get(code)
    return h[3] if h and len(h) > 3 and h[3] else code


def add(a):
    shares = a.shares if a.shares else round(a.qty * 1000)
    gross, fee, tax, net = costs(a.side, a.entry, a.exit, shares)
    if a.net is not None:            # 對帳單上的實際損益(分次成交時比估算準)
        net = int(a.net)
    j = load()
    tid = max([t["id"] for t in j["trades"]], default=0) + 1
    t = {"id": tid, "date": a.date, "code": a.code, "name": a.name or name_of(a.date, a.code), "side": a.side,
         "entry": a.entry, "exit": a.exit, "shares": shares, "time_in": a.time_in, "time_out": a.time_out,
         "gross": gross, "fee": fee, "tax": tax, "net": net,
         "pct": round(((a.exit / a.entry - 1) if a.side == "long" else (1 - a.exit / a.entry)) * 100, 3),
         "note": a.note or "", "rec": match_rec(a.date, a.code, a.side), "added_at": datetime.now().isoformat(timespec="seconds"),
         "net_src": "對帳單" if a.net is not None else "估算"}
    j["trades"].append(t)
    save(j)
    r = t["rec"]
    print(f"已記錄 #{tid} {t['date']} {t['code']} {t['name']} {'做多' if t['side'] == 'long' else '做空'} "
          f"{t['entry']}→{t['exit']} {shares} 股:毛 {gross:+,} 元,手續費 {fee},稅 {tax},淨 {net:+,} 元({t['pct']:+.2f}%)")
    print("推薦對照:" + (f"{'全市場雷達' if r['src'] == 'market' else '自選股觀測員'} {r['first_t']} 推薦{'同方向' if r['same_side'] else '反方向'}"
                         f"(@{r['first_price']},結論 {r['vc']})" if r else "當天雷達沒有推薦這檔(算你自己的判斷)"))


def main(argv=None):
    ap = argparse.ArgumentParser(description="我的交易紀錄")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("add")
    p.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    p.add_argument("--code", required=True)
    p.add_argument("--name")
    p.add_argument("--side", choices=("long", "short"), required=True)
    p.add_argument("--entry", type=float, required=True)
    p.add_argument("--exit", type=float, required=True)
    p.add_argument("--qty", type=float, default=1, help="張數")
    p.add_argument("--shares", type=int, help="股數(零股用;有給就不看 --qty)")
    p.add_argument("--in", dest="time_in")
    p.add_argument("--out", dest="time_out")
    p.add_argument("--note")
    p.add_argument("--net", type=float, help="對帳單上的實際損益(元);有給就用它,不用估算的")
    ls = sub.add_parser("list")
    ls.add_argument("--date")
    rm = sub.add_parser("remove")
    rm.add_argument("id", type=int)
    a = ap.parse_args(argv)
    if a.cmd == "add":
        add(a)
    elif a.cmd == "list":
        for t in load()["trades"]:
            if not a.date or t["date"] == a.date:
                print(t["id"], t["date"], t["code"], t["name"], t["side"], t["entry"], t["exit"], t["shares"], f"{t['net']:+,}")
    elif a.cmd == "remove":
        j = load()
        n = len(j["trades"])
        j["trades"] = [t for t in j["trades"] if t["id"] != a.id]
        save(j)
        print("已刪除" if len(j["trades"]) < n else "找不到這筆")


if __name__ == "__main__":
    sys.exit(main())
