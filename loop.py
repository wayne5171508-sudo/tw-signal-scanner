#!/usr/bin/env python3
"""
盤中常駐迴圈(intraday.yml 用它取代原本「每輪重開一支 python」的 bash 迴圈)

- 一整天只登入永豐一次:sjquote 的連線一直留著,每輪不用重開程式、重新登入、重新下載合約。
  連線斷了 sjquote 會自己重新登入;登入不了就照舊用鉅亨。
- 每輪對齊 5 分K 收盤:每根收完後 35 秒跑一輪(多空規則馬上用剛收完的那根判斷),兩根中間再補一輪
  (所以是 2、3 分鐘交錯);09:15 前每分鐘一輪(開盤爆量最需要快)。
- 每輪先 git pull 拿最新程式與資料,重新載入 intraday 等模組(sjquote 不重載,保住連線;
  sjquote.py 有改的話隔天才生效),跑完 commit + push。
- 每輪另外寫 data/stamp.json(只有更新時間),網頁每 15 秒看一次這個小檔,有新資料才整包重抓。

手動測試:python loop.py --once   (只跑一輪,不 commit)
"""

import importlib
import json
import os
import signal
import subprocess
import sys
import time
import traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TAIPEI = ZoneInfo("Asia/Taipei")
ROOT = os.path.dirname(os.path.abspath(__file__))
START_HM, END_HM = (8, 55), (13, 40)
WAIT_MAX_SEC = 2400        # 離開盤超過 40 分鐘就不等(intraday.yml 的接力步驟會先睡到差不多時間)
OFFSET_SEC = 35            # 5 分K 收盤後幾秒跑(intraday.K5_SETTLE_SEC 是 30 秒,要比它晚一點)
FAST_UNTIL = 9 * 60 + 15   # 這個時間(分鐘)之前每分鐘一輪
ROUND_TIMEOUT = 200        # 一輪最多幾秒,超過就放棄這一輪
STAMP_FILE = os.path.join("data", "stamp.json")
KEEP_MODULES = {"sjquote", "loop", "__main__"}


class RoundTimeout(Exception):
    pass


def now():
    return datetime.now(TAIPEI)


def next_slot(t):
    """t 之後的下一個排程時間:每分鐘的第 35 秒,09:15 前每分鐘都跑,之後只在分鐘尾數 0、2、5、7 跑。"""
    c = t.replace(second=OFFSET_SEC, microsecond=0)
    if c <= t:
        c += timedelta(minutes=1)
    while c.hour * 60 + c.minute >= FAST_UNTIL and c.minute % 5 not in (0, 2):
        c += timedelta(minutes=1)
    return c


def git(*args):
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=90)
    return r.returncode


def pull():
    if git("pull", "--rebase", "--autostash", "origin", "main") != 0:
        git("rebase", "--abort")
        print("pull失敗,沿用目前版本")


def commit_push(t):
    git("add", "-A", "data/")
    if git("diff", "--quiet", "--cached") == 0:
        return
    git("commit", "-m", f"盤中報價更新 {t:%Y-%m-%d %H:%M}")
    if git("push") != 0:
        if git("pull", "--rebase", "origin", "main") != 0 or git("push") != 0:
            git("rebase", "--abort")
            print("push失敗,下一輪再推")


def fresh_intraday():
    """把 repo 裡的模組(sjquote 以外)全部丟掉重新 import,效果等於每輪重開程式,但永豐連線留著。"""
    for name, mod in list(sys.modules.items()):
        f = getattr(mod, "__file__", None)
        if name not in KEEP_MODULES and f and os.path.dirname(os.path.abspath(f)) == ROOT:
            del sys.modules[name]
    importlib.invalidate_caches()
    import intraday
    return intraday


def write_stamp():
    """網頁輪詢用的小檔:intraday_latest.json 的更新時間。沒有新資料(休市略過)就不改,不會多出 commit。"""
    try:
        with open(os.path.join("data", "intraday_latest.json"), encoding="utf-8") as f:
            d = json.load(f)
        stamp = {"checked_at": d.get("checked_at"), "data_as_of": d.get("data_as_of")}
        old = None
        if os.path.exists(STAMP_FILE):
            with open(STAMP_FILE, encoding="utf-8") as f:
                old = json.load(f)
        if stamp != old:
            with open(STAMP_FILE, "w", encoding="utf-8") as f:
                json.dump(stamp, f, ensure_ascii=False)
    except (OSError, ValueError) as e:
        print(f"[警告] stamp 寫入失敗: {e}")


def _alarm(signum, frame):
    raise RoundTimeout()


def run_round():
    t0 = time.time()
    try:
        import sjquote
        sjquote.STATS.update({"rows": 0, "asked": 0, "seconds": 0.0, "kbars": 0})
    except ImportError:
        sjquote = None
    signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(ROUND_TIMEOUT)
    try:
        fresh_intraday().main()
    except RoundTimeout:
        print(f"這一輪超過 {ROUND_TIMEOUT} 秒,放棄,下一輪再試")
    except Exception:  # noqa: BLE001  任何錯誤都不能讓迴圈停掉
        print("這一輪抓取失敗,下一輪再試:")
        traceback.print_exc(limit=5)
    finally:
        signal.alarm(0)
    write_stamp()
    if sjquote is not None:
        print(f"[永豐] 這一輪 {sjquote.STATS},整輪 {time.time() - t0:.1f} 秒")


def main():
    os.chdir(ROOT)
    sys.path.insert(0, ROOT)
    if "--once" in sys.argv:
        run_round()
        return
    t = now()
    start = t.replace(hour=START_HM[0], minute=START_HM[1], second=0, microsecond=0)
    end = t.replace(hour=END_HM[0], minute=END_HM[1], second=0, microsecond=0)
    if t > end:
        print("已過收盤時間,結束")
        return
    if t < start:
        wait = (start - t).total_seconds()
        if wait > WAIT_MAX_SEC:
            print("離開盤太久,結束")
            return
        print(f"還沒到開盤時間,等待 {wait:.0f} 秒")
        time.sleep(wait)
    while True:
        rt = now()
        pull()
        run_round()
        commit_push(rt)
        slot = next_slot(now())
        if slot > end:
            break
        time.sleep(max(0.0, (slot - now()).total_seconds()))
    print("收盤,迴圈結束")


if __name__ == "__main__":
    main()
