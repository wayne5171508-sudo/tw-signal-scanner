#!/usr/bin/env python3
"""
爆量雷達 - 策略驗證室(收盤後由 intraday.py 呼叫;也可以自己在電腦上跑:python lab.py)

兩件事,結果都寫進 data/lab.json,網頁「更多 > 訊號實驗室」下方顯示:

1) RSI 候選策略(待辦12):用每天存下來的盤中走勢(約每5分鐘一個價格,data/intraday_archive/)
   算 RSI(7) 與 RSI(14),把 Threads 文章的說法寫成客觀規則逐一驗證:
     A1 超賣後黃金交叉:RSI7 向上穿過 RSI14,且最近 12 根內 RSI7 曾低於 30(低於30只當「準備」)
     A2 趨勢中黃金交叉:RSI7 向上穿過 RSI14,且 RSI14 在 50 以上(RSI 站上 50 當偏多過濾)
     B1 站上50:RSI14 由下往上穿過 50,下一根還站在 50 以上才進場(偏多)
     B2 跌破50:RSI14 由上往下跌破 50,下一根還在 50 以下才進場(偏空,放空方向)
     D1 底背離:兩個相鄰低點,價格創低但 RSI14 墊高,且第一個低點的 RSI14 低於 30
     D2 頂背離:兩個相鄰高點,價格創高但 RSI14 走低,且第一個高點的 RSI14 高於 70(放空方向)
   出場有三種,分開統計:
     收盤:抱到收盤最後一筆
     C 規則:RSI7 曾到 80 以上,之後 RSI7 向下穿過 RSI14(死亡交叉)才出場,沒出現就收盤出
     C+E:同 C,但 RSI14 連續 3 根以上卡在 80 以上(鈍化)時,改成「價格跌破 12 根均線」才出場
   每一筆都和「同一進場時間到同一出場時間,全部自選股(不含ETF)的平均漲跌」比較,並扣 0.4% 來回成本。

2) 參數掃描與樣本外驗證(待辦3):用訊號實驗室的快照(自選股 4 個時點、全市場 3 個時點),
   把四種策略的門檻(漲跌幅、量能倍數、拉回/反彈幅度)全部組合跑一遍。
   前 60% 的交易日拿來挑參數(訓練),後 40% 只拿來驗證(樣本外),避免「挑出來的參數只是剛好符合過去」。
   交易日少於 6 天時只顯示「樣本不足」。

這裡的數字都是歷史統計,不是預測,也不是買賣建議。
"""

import json
import math
import os
from datetime import datetime
from itertools import product
from zoneinfo import ZoneInfo

try:
    import review
except ImportError:      # 單獨執行時 review.py 應該在同一個資料夾
    review = None

TAIPEI = ZoneInfo("Asia/Taipei")
DATA_DIR = "data"
LAB_FILE = os.path.join(DATA_DIR, "lab.json")
SIGNAL_FILE = os.path.join(DATA_DIR, "signal_log.json")
MARKET_DIR = os.path.join(DATA_DIR, "mkt")
COST_PCT = 0.23            # 當沖來回成本(手續費 2.8 折 + 當沖證交稅),和 daytrade_lab 一致

RSI_S, RSI_L = 7, 14
OVERSOLD, OVERBOUGHT, HOT = 30, 70, 80
PREP_LOOKBACK = 12        # 「最近 12 根內曾低於 30」,5 分鐘一根約 1 小時
PIVOT_K = 3               # 轉折點:前後各 3 根都沒有更低(高)
DIV_MAX_GAP = 40          # 兩個轉折點最多相隔幾根
MA_EXIT_N = 12            # 鈍化時改看的均線長度
DULL_BARS = 3             # RSI14 連續幾根 >80 算鈍化
ENTRY_FROM_MIN, ENTRY_TO_MIN = 15, 240   # 只在 09:15~13:00 進場,太晚進場抱到收盤沒有意義
WARMUP_PREV_POINTS = 30   # 用前一天最後幾筆價格暖機,早盤的 RSI 才不會是空的
TRAIN_FRAC = 0.6
SWEEP_MIN_DAYS = 6
SWEEP_MIN_N_TRAIN = 15
SWEEP_MIN_N_TEST = 20
T_MIN = 2.0               # 差距要至少是標準誤的 2 倍,才不容易只是運氣(隨機資料測試過,門檻太鬆會把雜訊當優勢)

RSI_DESC = {
    "A1": ("long", "超賣後黃金交叉", "RSI7上穿RSI14,且最近約1小時RSI7曾低於30"),
    "A2": ("long", "趨勢中黃金交叉", "RSI7上穿RSI14,且RSI14在50以上"),
    "B1": ("long", "RSI站上50", "RSI14由下往上穿過50,下一根仍在50以上"),
    "B2": ("short", "RSI跌破50(空)", "RSI14由上往下跌破50,下一根仍在50以下"),
    "D1": ("long", "底背離", "價格創低但RSI14墊高,且前一個低點RSI14<30"),
    "D2": ("short", "頂背離(空)", "價格創高但RSI14走低,且前一個高點RSI14>70"),
}
EXITS = ("close", "c", "ce")
EXIT_DESC = {"close": "抱到收盤", "c": "C:80以上死叉出場", "ce": "C+E:鈍化改看均線"}


def _num(*xs):
    return all(isinstance(x, (int, float)) and not isinstance(x, bool) and x == x for x in xs)


def _load(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def _write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    os.replace(tmp, path)


# ---------------------------------------------------------------- RSI

def rsi(prices, n):
    """Wilder RSI。prices 可以有 None(缺值沿用前一筆)。前 n 根回傳 None。"""
    out = [None] * len(prices)
    last = None
    filled = []
    for p in prices:
        last = p if _num(p) else last
        filled.append(last)
    gains = losses = 0.0
    cnt = 0
    avg_g = avg_l = None
    for i in range(1, len(filled)):
        a, b = filled[i - 1], filled[i]
        if a is None or b is None:
            continue
        ch = b - a
        g, l = max(ch, 0.0), max(-ch, 0.0)
        if avg_g is None:
            gains += g
            losses += l
            cnt += 1
            if cnt == n:
                avg_g, avg_l = gains / n, losses / n
            else:
                continue
        else:
            avg_g = (avg_g * (n - 1) + g) / n
            avg_l = (avg_l * (n - 1) + l) / n
        if avg_l == 0:
            out[i] = 100.0 if avg_g > 0 else 50.0
        else:
            out[i] = 100 - 100 / (1 + avg_g / avg_l)
    return out


def _pivots(px, k, low=True):
    res = []
    for j in range(k, len(px) - k):
        win = [x for x in px[j - k:j + k + 1] if _num(x)]
        if not _num(px[j]) or len(win) < 2 * k + 1:
            continue
        if (low and px[j] <= min(win) and px[j] < max(win)) or (not low and px[j] >= max(win) and px[j] > min(win)):
            if res and j - res[-1] <= k:      # 平台:同一段連續相同價只取第一個
                continue
            res.append(j)
    return res


def entries_for(px, r7, r14, start, m):
    """回傳 {策略: [進場的索引]}。start=今天第一根的索引(前面是前一天的暖機資料)。"""
    out = {k: [] for k in RSI_DESC}
    ok = lambda i: start <= i < len(px) and m[i - start] is not None and ENTRY_FROM_MIN <= m[i - start] <= ENTRY_TO_MIN and _num(px[i])
    for i in range(max(start, 1), len(px)):
        a7, b7, a14, b14 = r7[i - 1], r7[i], r14[i - 1], r14[i]
        if not _num(a7, b7, a14, b14):
            continue
        cross_up = a7 <= a14 and b7 > b14
        if cross_up and ok(i):
            prev = [x for x in r7[max(0, i - PREP_LOOKBACK + 1):i + 1] if _num(x)]
            if prev and min(prev) < OVERSOLD:
                out["A1"].append(i)
            if b14 >= 50:
                out["A2"].append(i)
        if i + 1 < len(px) and _num(r14[i + 1]):
            if a14 < 50 <= b14 and r14[i + 1] >= 50 and ok(i + 1):
                out["B1"].append(i + 1)
            if a14 > 50 >= b14 and r14[i + 1] < 50 and ok(i + 1):
                out["B2"].append(i + 1)
    for low, key, cond in ((True, "D1", lambda r0: r0 < OVERSOLD), (False, "D2", lambda r0: r0 > OVERBOUGHT)):
        pv = [j for j in _pivots(px, PIVOT_K, low) if j >= start - PIVOT_K * 4]
        for j1, j2 in zip(pv, pv[1:]):
            if j2 - j1 > DIV_MAX_GAP or not _num(r14[j1], r14[j2]):
                continue
            price_ext = px[j2] < px[j1] if low else px[j2] > px[j1]
            rsi_div = r14[j2] > r14[j1] if low else r14[j2] < r14[j1]
            e = j2 + PIVOT_K              # 轉折點要等後面 3 根才確認,確認那一根才進場
            if price_ext and rsi_div and cond(r14[j1]) and ok(e):
                out[key].append(e)
    return out


def exit_index(kind, i, side, px, r7, r14):
    last = len(px) - 1
    while last > i and not _num(px[last]):
        last -= 1
    if kind == "close" or side != "long":
        # 放空方向:文章的出場規則是針對多單,空單一律收盤出,避免硬套
        return last
    hot = False
    dull = 0
    for k in range(i + 1, last + 1):
        if _num(r7[k]) and r7[k] >= HOT:
            hot = True
        if kind == "ce":
            dull = dull + 1 if (_num(r14[k]) and r14[k] > HOT) else 0
            if dull >= DULL_BARS:
                seg = [x for x in px[max(0, k - MA_EXIT_N + 1):k + 1] if _num(x)]
                if len(seg) >= MA_EXIT_N // 2 and _num(px[k]) and px[k] < sum(seg) / len(seg):
                    return k
                continue      # 鈍化中不看 RSI 死叉
        if hot and _num(r7[k - 1], r14[k - 1], r7[k], r14[k]) and r7[k - 1] >= r14[k - 1] and r7[k] < r14[k]:
            return k
    return last


def _tstat(xs):
    """平均值 ÷ 標準誤。同一天的多筆交易彼此高度相關,所以先把每天的差距平均成一個數字,再用「天」當樣本算,比較保守。"""
    n = len(xs)
    if n < 3:
        return None
    mu = sum(xs) / n
    var = sum((x - mu) ** 2 for x in xs) / (n - 1)
    return round(mu / math.sqrt(var / n), 2) if var > 0 else None


def _daily_t(items):
    """items: [(日期, 差距)]。"""
    by = {}
    for d, x in items:
        by.setdefault(d, []).append(x)
    return _tstat([sum(v) / len(v) for v in by.values()])


def _stats(trades):
    n = len(trades)
    if not n:
        return {"n": 0}
    avg = sum(t["ret"] for t in trades) / n
    base = sum(t["base"] for t in trades) / n
    return {"n": n, "win": sum(1 for t in trades if t["ret"] - COST_PCT > 0), "avg": round(avg, 2), "base": round(base, 2),
            "edge": round(avg - base, 2), "net": round(avg - COST_PCT, 2),
            "days": len({t["d"] for t in trades}), "t": _daily_t([(t["d"], t["ret"] - t["base"]) for t in trades])}


def run_rsi(archives, groups):
    """archives: {日期: 存檔},由舊到新。回傳 RSI 各策略 × 各出場方式的統計(全部、訓練段、驗證段)。"""
    dates = sorted(archives)
    trades = {(k, e): [] for k in RSI_DESC for e in EXITS}
    prev = None
    for d in dates:
        a = archives[d]
        m = a.get("m") or []
        n_today = len(m)
        pxs = a.get("px") or {}
        # 同一時間全部股票的價格(對照組用)
        stock_codes = [c for c in pxs if c in groups]
        base_cache = {}      # (進場位置, 出場位置) -> 全體平均,同一天很多筆共用
        for code, series in pxs.items():
            if code not in groups:      # ETF 不算
                continue
            warm = ((prev or {}).get("px") or {}).get(code) or []
            warm = [x for x in warm if _num(x)][-WARMUP_PREV_POINTS:]
            px = warm + list(series)
            start = len(warm)
            r7, r14 = rsi(px, RSI_S), rsi(px, RSI_L)
            ent = entries_for(px, r7, r14, start, m)
            for key, idxs in ent.items():
                side = RSI_DESC[key][0]
                sg = 1 if side == "long" else -1
                used = {ex: -1 for ex in EXITS}
                for i in idxs:
                    for ex in EXITS:
                        if i <= used[ex]:          # 同一檔前一筆還沒出場,不重複進場
                            continue
                        k = exit_index(ex, i, side, px, r7, r14)
                        used[ex] = k
                        if k <= i or not _num(px[i], px[k]) or px[i] <= 0:
                            continue
                        ii, kk = i - start, k - start
                        if (ii, kk) not in base_cache:
                            rs = []
                            for c2 in stock_codes:
                                s2 = pxs[c2]
                                if 0 <= ii < n_today and 0 <= kk < n_today and _num(s2[ii], s2[kk]) and s2[ii] > 0:
                                    rs.append((s2[kk] / s2[ii] - 1) * 100)
                            base_cache[(ii, kk)] = sum(rs) / len(rs) if len(rs) >= 10 else None
                        bm = base_cache[(ii, kk)]
                        if bm is None:
                            continue
                        trades[(key, ex)].append({"d": d, "code": code, "ret": sg * (px[k] / px[i] - 1) * 100,
                                                  "base": sg * bm})
        prev = a
    cut = dates[int(len(dates) * TRAIN_FRAC)] if len(dates) >= 2 else None
    out = {}
    for key, (side, name, desc) in RSI_DESC.items():
        row = {"side": side, "name": name, "desc": desc, "exits": {}}
        for ex in EXITS:
            ts = trades[(key, ex)]
            row["exits"][ex] = {"all": _stats(ts),
                                "train": _stats([t for t in ts if cut and t["d"] < cut]),
                                "test": _stats([t for t in ts if cut and t["d"] >= cut])}
        out[key] = row
    return {"days": len(dates), "first": dates[0] if dates else None, "last": dates[-1] if dates else None,
            "split": cut, "strats": out}


# ---------------------------------------------------------------- 參數掃描

FAMILIES = {
    # 名稱: (方向, 參數格點, 條件)  條件參數:漲跌幅門檻 P、量能門檻 V、第三參數 X
    "追強": ("long", ((1, 2, 3, 4, 5), (1.5, 2, 3, 4, 6), (0.5, 1, 2)), lambda pct, pace, dd, up, P, V, X: pct >= P and pace >= V and dd >= -X),
    "拉回買": ("long", ((1, 2, 3, 4, 5), (1.5, 2, 3, 4, 6), (1, 2, 3)), lambda pct, pace, dd, up, P, V, X: pct >= P and pace >= V and dd <= -X),
    "弱勢爆量": ("short", ((1, 2, 3, 4, 5), (1.5, 2, 3, 4, 6), (0,)), lambda pct, pace, dd, up, P, V, X: pct <= -P and pace >= V),
    "低檔反彈": ("long", ((1, 2, 3, 4, 5), (1.5, 2, 3, 4, 6), (1, 2, 3)), lambda pct, pace, dd, up, P, V, X: pct <= -P and pace >= V and up >= X),
}
DEFAULTS = {"追強": (2, 3, 0.5), "拉回買": (2, 3, 2), "弱勢爆量": (2, 3, 0), "低檔反彈": (2, 3, 2)}


def _sweep_days_watch(groups):
    log = _load(SIGNAL_FILE, {}) or {}
    out = []
    for d, day in sorted((log.get("days") or {}).items()):
        close = day.get("close") or {}
        if not close:
            continue
        snaps = {sn: {c: r for c, r in rows.items() if c in groups} for sn, rows in (day.get("snaps") or {}).items()}
        out.append((d, snaps, close))
    return out


def _sweep_days_market():
    out = []
    if not os.path.isdir(MARKET_DIR):
        return out
    for fn in sorted(os.listdir(MARKET_DIR)):
        if not fn.endswith(".json"):
            continue
        day = _load(os.path.join(MARKET_DIR, fn), None)
        if not isinstance(day, dict) or not day.get("close"):
            continue
        close = {c: v[0] for c, v in day["close"].items()}
        snaps = {sn: {c: [r[0], r[2], r[5], r[3], r[4]] for c, r in (sv.get("rows") or {}).items()} for sn, sv in (day.get("snaps") or {}).items()}
        out.append((fn[:-5], snaps, close))
    return out


def _prep(days):
    """把每天每個時點每檔股票先算好 (漲跌%, 量能, 離高點%, 離低點%, 抱到收盤報酬, 同時點平均),掃描時不用重算。"""
    rows = []
    for d, snaps, close in days:
        for sn, rs in snaps.items():
            rets = {c: (close[c] / r[0] - 1) * 100 for c, r in rs.items() if c in close and _num(r[0]) and r[0] > 0 and _num(close[c])}
            if not rets:
                continue
            base = sum(rets.values()) / len(rets)
            for c, r in rs.items():
                if c not in rets or not (_num(r[1], r[2]) and r[3] and r[4]):
                    continue
                rows.append((d, r[1], r[2], (r[0] / r[3] - 1) * 100, (r[0] / r[4] - 1) * 100, rets[c], base))
    return rows


def run_sweep(days):
    dates = sorted({d for d, _, _ in days})
    if len(dates) < SWEEP_MIN_DAYS:
        return {"days": len(dates), "need": SWEEP_MIN_DAYS, "ready": False}
    cut = dates[int(len(dates) * TRAIN_FRAC)]
    rows = _prep(days)
    out = {"days": len(dates), "ready": True, "split": cut, "train_days": sum(1 for d in dates if d < cut),
           "test_days": sum(1 for d in dates if d >= cut), "families": {}}
    for fam, (side, grid, cond) in FAMILIES.items():
        sg = 1 if side == "long" else -1
        res = []
        for P, V, X in product(*grid):
            tr, te = [], []
            for d, pct, pace, dd, up, ret, base in rows:
                if cond(pct, pace, dd, up, P, V, X):
                    (tr if d < cut else te).append((sg * ret, sg * base, d))
            if len(tr) < SWEEP_MIN_N_TRAIN:
                continue
            f = lambda xs: {"n": len(xs), "avg": round(sum(a for a, _, _ in xs) / len(xs), 2), "edge": round(sum(a - b for a, b, _ in xs) / len(xs), 2),
                            "win": round(sum(1 for a, _, _ in xs if a - COST_PCT > 0) / len(xs) * 100),
                            "t": _daily_t([(d, a - b) for a, b, d in xs])} if xs else {"n": 0}
            res.append({"P": P, "V": V, "X": X, "train": f(tr), "test": f(te), "default": (P, V, X) == DEFAULTS[fam]})
        res.sort(key=lambda r: -r["train"]["edge"])
        top = res[:5]
        dflt = next((r for r in res if r["default"]), None)
        if dflt and dflt not in top:
            top.append(dflt)
        # 樣本外是否站得住:驗證段筆數夠、差距大於成本,而且訓練段、驗證段的差距都至少是標準誤的2倍
        for r in res:
            t, tr_ = r["test"], r["train"]
            r["holds"] = bool(t.get("n", 0) >= SWEEP_MIN_N_TEST and t["edge"] > COST_PCT and tr_["edge"] > COST_PCT
                              and (t.get("t") or 0) >= T_MIN and (tr_.get("t") or 0) >= T_MIN)
        out["families"][fam] = {"side": side, "tested": len(res), "n_hold": sum(1 for r in res if r["holds"]), "top": top}
    return out


# ---------------------------------------------------------------- 進入點

def run_all(signal_strats=None, groups=None):
    """收盤後呼叫。groups = {代號: 族群}(用來排除 ETF)。"""
    if groups is None:
        try:
            from intraday import GROUP_OF as groups   # 單獨執行時
        except ImportError:
            groups = {}
    archives = review.load_archives() if review else {}
    result = {
        "updated": datetime.now(TAIPEI).strftime("%Y-%m-%d %H:%M"),
        "cost_pct": COST_PCT,
        "exit_desc": EXIT_DESC,
        "rsi": run_rsi(archives, groups),
        "sweep": {"watch": run_sweep(_sweep_days_watch(groups)), "market": run_sweep(_sweep_days_market())},
    }
    _write(LAB_FILE, result)
    return result


def load_summary():
    lab = _load(LAB_FILE, None)
    if not isinstance(lab, dict):
        return None
    sw = lab.get("sweep") or {}
    return {"updated": lab.get("updated"), "rsi_days": (lab.get("rsi") or {}).get("days"),
            "sweep_ready": any((v or {}).get("ready") for v in sw.values())}


if __name__ == "__main__":
    r = run_all()
    print(json.dumps({"rsi_days": r["rsi"]["days"], "sweep": {k: v.get("days") for k, v in r["sweep"].items()}}, ensure_ascii=False))
