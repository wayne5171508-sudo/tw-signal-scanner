#!/usr/bin/env python3
"""
爆量雷達 - 盤中即時報價擷取

直接呼叫 cnyes(鉅亨網)的公開報價 JSON API,抓大盤指數 + 自選股即時成交價,
寫成 data/intraday_latest.json,交給 index.html(GitHub Pages)顯示。

跟 scan.py 的分工:
- scan.py:每天收盤後,抓 TWSE 官方資料算三大法人買賣超、技術訊號(全部是
  「收盤後」才看得到的資料)。
- intraday.py(這支):開盤期間每隔一段時間抓一次「現在的成交價」,
  給你隨時打開網頁就能看目前大盤跟自選股的即時報價,不是收盤資料。

這支程式刻意不透過 Claude 的 WebFetch 去讀網頁,而是用 Python 直接打 API、
解析真正的 JSON——WebFetch 是用小模型讀網頁摘要,資料量大或重複抓同一個
網址時曾經抓到過期的快取資料,直接打 API 沒有這個問題。

v2新增:順便維護 data/intraday_history.json,把每次抓到的價格疊加進去(只留當天的點、
最多留60筆),讓網頁可以畫出「今天到目前為止」的走勢小圖,不只是單一時間點的數字。

v3新增(觸價通知):
- 自動比對每檔自選股現價相對「昨收」是否剛好跨過(由上轉下、由下轉上),以及有沒有碰到
  漲停/跌停,一天一檔只通知一次,寫在 data/alerts_state.json 裡避免重複推播。
- 讀 data/alerts.json 裡阿文自己設定的「某檔股票到某個價位通知我」,碰到一樣一天只推一次。
- 有新觸發就寄一封信到阿文的Gmail(用GitHub Actions secret裡的App密碼登入,沒設定就跳過
  寄信但不會讓整個排程失敗)。

v4新增(CDP壓力/支撐,取代原本誤用的今日高低/漲跌停):
- 原本網頁把「壓力位/支撐位」寫成漲停/跌停價,但台股正常股票每天都有漲跌停價,等於每天
  都釘死在同一個位置,沒有參考意義,是設計錯誤。查過實際的技術分析定義後,改用股市常見的
  CDP逆勢操作系統(定點轉向):用「前一個交易日」的最高/最低/收盤價算出一個樞紐價(CDP),
  再算出近高值(NH,當壓力參考)、近低值(NL,當支撐參考)。這個算法在台股看盤軟體(例如
  三竹股市)很常見,是真的有人在用的公式,不是我自己發明的。
- 這支程式本來就每天開盤期間重複抓同一批股票的「今日高/今日低/成交價」,所以不用額外呼叫
  證交所的歷史資料API,直接把每天最後一次抓到的高低收,存成 data/daily_hlc.json 當「明天
  算CDP要用的昨天資料」,隔天換日期時自動把這份存檔內容轉正式變成「昨天」,重新開始累積
  「今天」。好處是不必再去區分一檔股票是上市(TWSE)還是上櫃(TPEX)、不用多接一個官方API,
  壞處是剛上線的第一個交易日還沒有「昨天」資料可以算,那天網頁會先顯示今日高/今日低當替代,
  從第二個交易日開始才會自動換成CDP。
- CDP公式:CDP=(昨高+昨低+2*昨收)/4;近高值NH=2*CDP-昨低;近低值NL=2*CDP-昨高;
  最高值AH=CDP+(昨高-昨低);最低值AL=CDP-(昨高-昨低)。

v5新增(把v4的「前一天」擴充成「最近N個交易日」的滾動歷史,一次解決三件事):
- data/daily_hlc.json(只存前一天)換成 data/daily_bars.json(存最近10個交易日的高低收量),
  程式會自動把舊檔案的資料搬過來,不會把v4那天已經抓到的資料丟掉。
- 量能倍率(全部50檔自選股,不只scan.py那20檔候選股):今日成交量 / 最近5個交易日平均量,
  資料不足3天時回傳null,讓網頁知道還在累積中,不會顯示一個假的倍率。
- 壓力/支撐多日趨勢:用歷史裡每一天搭配「它的前一天」,回推出最近幾天「當時」的CDP近高/
  近低值,串成一條小時間序列,讓網頁可以畫出「壓力是不是越墊越高、支撐是不是越墊越低」這種
  短線偏多/偏空的味道,一樣是機械式算出來的,不是預測。

v6新增(職業操盤手複檢抓出的兩個資料正確性問題,加抓取韌性):
- 國定假日/颱風假等「週一到週五但沒開盤」的日子,排程還是會跑,但cnyes會回傳一整天沒變化
  的「死數字」(成交量是0、最高=最低=昨收)。這種假資料如果照樣疊進歷史,會讓CDP滾動歷史
  多一天「高低收都一樣」的假交易日,隔天算出來的壓力/支撐會跟著算錯;也會讓觸價通知的
  「昨收跨越方向」記錄被洗成「平盤」,隔天真的開盤時誤判成一次假的「跨越」而發錯通知。
  新增 _is_real_trading_day() 判斷「今天這批資料看起來像不像真的有在交易」,不像的話:
  不把這天寫進 daily_bars 的歷史、也不更新觸價通知的跨越方向記錄,等於當作沒抓到一樣跳過,
  不會汙染任何一份長期累積的資料。
- fetch_quotes() 加簡單重試(最多3次、間隔遞增),單純網路瞬斷不會讓整次排程直接失敗、
  漏掉一個資料點;真的連續失敗才會讓這次執行失敗(GitHub Actions會顯示紅叉),這是刻意的,
  因為「完全抓不到資料」時寧可讓這次排程明顯失敗,也不要默默寫出一份空的/半套資料蓋掉舊檔。

用法:
    python intraday.py
    (會自動寫到 data/intraday_latest.json、data/intraday_history.json、
     data/alerts_state.json、data/daily_bars.json)
"""

import json
import os
import smtplib
import time
from datetime import datetime
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

import requests

TAIPEI = ZoneInfo("Asia/Taipei")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; personal-trading-dashboard/1.0)"}
TIMEOUT = 20
DATA_DIR = "data"
OUT_FILE = os.path.join(DATA_DIR, "intraday_latest.json")
HISTORY_FILE = os.path.join(DATA_DIR, "intraday_history.json")
ALERTS_CONFIG_FILE = os.path.join(DATA_DIR, "alerts.json")
ALERTS_STATE_FILE = os.path.join(DATA_DIR, "alerts_state.json")
DAILY_HLC_FILE = os.path.join(DATA_DIR, "daily_hlc.json")  # v4的舊檔,只用來搬資料,之後不再寫入
DAILY_BARS_FILE = os.path.join(DATA_DIR, "daily_bars.json")
MAX_POINTS_PER_DAY = 60
MAX_HISTORY_DAYS = 10
VOLUME_LOOKBACK_DAYS = 5
CDP_TREND_POINTS = 6  # 趨勢最多顯示幾個點(含今天)

ALERT_EMAIL_TO = "wayne5171508@gmail.com"
ALERT_EMAIL_FROM = "wayne5171508@gmail.com"
GMAIL_APP_PASSWORD_ENV = "GMAIL_APP_PASSWORD"

CNYES_QUOTE_URL = "https://ws.api.cnyes.com/ws/api/v1/quote/quotes/{codes}"

# 6大盤指數(cnyes代碼, 顯示名稱)
INDICES = [
    ("TWS:TSE01:INDEX", "加權指數"),
    ("TWS:OTC01:INDEX", "櫃買指數"),
    ("GI:DJI:INDEX", "道瓊"),
    ("GI:IXIC:INDEX", "NASDAQ"),
    ("GI:INX:INDEX", "S&P500"),
    ("GI:SOX:INDEX", "費城半導體"),
]

# 自選股(代碼: 名稱)—— 來源:阿文國泰證券App自選股分類彙整,2026-09-25
WATCHLIST_NAMES = {
    "2455": "全新", "3374": "精材", "2408": "南亞科", "2344": "華邦電",
    "6147": "頎邦", "8021": "尖點", "8039": "台虹", "8358": "金居",
    "3264": "欣鈺", "4979": "華星光", "3105": "穩懋", "6173": "信昌電",
    "3016": "嘉晶", "1727": "中華化", "8042": "金山電", "3027": "盛達",
    "4303": "信立", "8111": "立碁", "1815": "富喬", "2630": "亞航",
    "2883": "凱基金", "2881": "富邦金", "2313": "華通", "4919": "新唐",
    "6217": "中探針", "1303": "南亞", "3051": "力特", "6937": "天虹",
    "2337": "旺宏", "6257": "矽格", "3090": "日電貿", "2303": "聯電",
    "2379": "瑞昱", "2609": "陽明", "3231": "緯創", "3707": "漢磊",
    "5425": "台半", "2481": "強茂", "3588": "通嘉", "5347": "世界",
    "6182": "合晶", "2351": "順德", "2449": "京元電子", "6239": "力成",
    "8150": "南茂", "4958": "臻鼎-KY", "3037": "欣興", "6488": "環球晶",
    "3490": "單井", "2409": "友達",
}


def taipei_now():
    return datetime.now(TAIPEI)


def fetch_quotes(codes):
    """codes: list of cnyes代碼字串。不帶 column 參數——帶了反而只會拿到精簡欄位,
    不帶才會拿到完整欄位(含高低開收、漲跌停價)。一次最多抓50筆,分批呼叫。
    單一批次失敗時重試最多3次(間隔遞增1秒/2秒),應付網路瞬斷;連續3次都失敗才真的
    往外拋例外,讓main()中止、這次排程失敗(寧可明顯失敗,也不要默默寫出半套資料)。"""
    out = {}
    batch_size = 50
    for i in range(0, len(codes), batch_size):
        batch = codes[i:i + batch_size]
        url = CNYES_QUOTE_URL.format(codes=",".join(batch))
        last_err = None
        body = None
        for attempt in range(3):
            try:
                resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
                resp.raise_for_status()
                body = resp.json()
                break
            except (requests.RequestException, ValueError) as e:
                last_err = e
                if attempt < 2:
                    time.sleep(attempt + 1)
        if body is None:
            raise last_err
        for row in body.get("data", []):
            out[row.get("0")] = row
    return out


def row_to_quote(row, fallback_code, fallback_name):
    if row is None:
        return {
            "code": fallback_code, "name": fallback_name,
            "last": None, "change": None, "pct": None,
            "open": None, "high": None, "low": None, "prev_close": None,
            "volume": None, "ts": None,
        }
    last = row.get("6")
    change = row.get("11")
    prev_close = row.get("21")
    if prev_close is None and last is not None and change is not None:
        prev_close = round(last - change, 4)
    return {
        "code": fallback_code,
        "name": row.get("200009") or fallback_name,
        "last": last,
        "change": change,
        "pct": row.get("56"),
        "open": row.get("19"),
        "high": row.get("12"),
        "low": row.get("13"),
        "prev_close": prev_close,
        "volume": row.get("200013"),
        "upper_limit": row.get("75"),
        "lower_limit": row.get("76"),
        "limit_flag": row.get("200025"),
        "ts": row.get("200007"),
    }


def update_history(run_time, indices_out, watchlist_out):
    """把這次抓到的價格疊加進 data/intraday_history.json。
    只留「今天」的點(用台北日期判斷,跨到隔天自動重置),最多留 MAX_POINTS_PER_DAY 筆,
    避免檔案越養越大。每筆只存 last 價(夠畫走勢小圖),不重複存整包報價。"""
    today_str = run_time.strftime("%Y-%m-%d")

    history = {"date": today_str, "points": []}
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                existing = json.load(f)
            if existing.get("date") == today_str:
                history = existing
        except (json.JSONDecodeError, OSError):
            pass

    point = {
        "t": run_time.isoformat(),
        "idx": {q["code"]: q["last"] for q in indices_out if q.get("last") is not None},
        "wl": {q["code"]: q["last"] for q in watchlist_out if q.get("last") is not None},
    }
    history["points"].append(point)
    history["points"] = history["points"][-MAX_POINTS_PER_DAY:]

    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False)


def _is_real_trading_day(bars):
    """bars是{code:{h,l,c,v}}這種「一天份」的資料。判斷依據:只要有任何一檔股票成交量
    是正的、或是最高不等於最低(代表當天價格真的有動),就當作是真的有開盤交易的一天。
    國定假日/颱風假這種排程照跑但其實沒開盤的日子,cnyes回傳的每一檔都會是量=0、
    高=低=昨收的死數字,全部檔位都符合這兩個條件才會判斷為「非交易日」。"""
    if not bars:
        return False
    for bar in bars.values():
        if bar.get("v"):
            return True
        if bar.get("h") is not None and bar.get("l") is not None and bar["h"] != bar["l"]:
            return True
    return False


def _looks_like_trading_session(watchlist_out):
    """判斷這次抓到的「這一次」報價看起來像不像真的在交易中(用來擋掉觸價通知誤判)。
    50檔自選股裡只要有10檔以上這次有成交量,就當作真的在交易;假日/非交易時段抓到的
    通常會是全部掛零或極少數有量,不會誤擋到正常開盤時間。"""
    traded = sum(1 for q in watchlist_out if q.get("volume"))
    return traded >= 10


def update_daily_bars(run_time, watchlist_out):
    """維護 data/daily_bars.json:存「今天目前為止」+「最近 MAX_HISTORY_DAYS 個交易日」的
    高低收量(近似值,收盤價用當天最後一次抓到的成交價,成交量同理)。換日期時自動把舊的
    「今天」疊進歷史清單最前面(新到舊排列)。

    只用這支程式本來就在抓的cnyes報價滾動累積,不必再多接證交所的歷史API、也不用分上市上櫃,
    壞處是剛上線前幾天資料還不夠多,CDP/量能倍率/多日趨勢都要等資料長出來才會生效,這是刻意
    的取捨,詳見檔案開頭的說明。

    回傳 (prev_bars, history_days):
      prev_bars: 前一個交易日的 {code:{h,l,c,v}},給 compute_cdp() 用。
      history_days: 新到舊排列、不含今天的歷史清單,給量能倍率、多日趨勢用。
    """
    today_str = run_time.strftime("%Y-%m-%d")
    stored = load_json_safe(DAILY_BARS_FILE, None)

    if stored is None:
        # 第一次換成這個新格式:把v4那支舊檔 data/daily_hlc.json 的資料搬過來,
        # 不要把已經抓到的資料平白丟掉。舊檔沒有也沒關係,就當全新開始。
        old = load_json_safe(DAILY_HLC_FILE, {})
        stored = {"date": old.get("date"), "today": old.get("today", {}) or {}, "days": []}
        if old.get("prev"):
            stored["days"] = [{"date": None, "bars": old["prev"]}]

    if stored.get("date") != today_str:
        # 只有「看起來真的有開盤交易」的一天才滾進歷史;國定假日排程照跑但沒開盤的話,
        # 那天的死數字直接丟棄不留痕跡,換日期後從頭累積「今天」,不會汙染CDP/量能歷史。
        if stored.get("today") and _is_real_trading_day(stored["today"]):
            stored.setdefault("days", []).insert(0, {"date": stored.get("date"), "bars": stored["today"]})
            stored["days"] = stored["days"][:MAX_HISTORY_DAYS]
        stored["today"] = {}
        stored["date"] = today_str

    for q in watchlist_out:
        if q.get("high") is not None and q.get("low") is not None and q.get("last") is not None:
            stored["today"][q["code"]] = {
                "h": q["high"], "l": q["low"], "c": q["last"], "v": q.get("volume"),
            }

    with open(DAILY_BARS_FILE, "w", encoding="utf-8") as f:
        json.dump(stored, f, ensure_ascii=False)

    days = stored.get("days", [])
    prev_bars = days[0]["bars"] if days else {}
    return prev_bars, days


def compute_volume_multiple(today_volume, code, history_days):
    """量能倍率 = 今日成交量 / 最近 VOLUME_LOOKBACK_DAYS 個交易日的平均成交量。
    不到3天歷史資料時回傳 None,網頁端要顯示「資料還在累積中」,不要硬算出一個不可靠的倍率。"""
    if today_volume is None:
        return None
    vols = []
    for day in history_days[:VOLUME_LOOKBACK_DAYS]:
        bar = (day.get("bars") or {}).get(code)
        if bar and bar.get("v"):
            vols.append(bar["v"])
    if len(vols) < 3:
        return None
    avg = sum(vols) / len(vols)
    return round(today_volume / avg, 2) if avg else None


def compute_cdp_trend(code, history_days, today_cdp):
    """回推最近幾天「當時」適用的CDP近高/近低值,串成一條由舊到新的小時間序列,
    讓網頁可以畫出壓力/支撐是不是越墊越高/越墊越低。history_days 新到舊排列、不含今天;
    每一天要搭配「它的前一天」才能算出當天的CDP,所以最多隻能往回推 len(history_days)-1 天。"""
    points = []
    for i in range(len(history_days) - 1, 0, -1):
        c = compute_cdp((history_days[i].get("bars") or {}).get(code))
        if c:
            points.append({"date": history_days[i - 1].get("date"), "nh": c["nh"], "nl": c["nl"], "cdp": c["cdp"]})
    if today_cdp:
        points.append({"date": "今天", "nh": today_cdp["nh"], "nl": today_cdp["nl"], "cdp": today_cdp["cdp"]})
    return points[-CDP_TREND_POINTS:]


def compute_cdp(prev_hlc_for_code):
    """CDP逆勢操作系統:用前一個交易日的高/低/收算今天的樞紐價跟近高/近低值。
    prev_hlc_for_code 是 {"h":前高, "l":前低, "c":前收} 或 None/缺值時回傳 None(表示還沒有
    足夠資料可以算,網頁端要自己 fallback 回今日高/今日低)。"""
    if not prev_hlc_for_code:
        return None
    h, l, c = prev_hlc_for_code.get("h"), prev_hlc_for_code.get("l"), prev_hlc_for_code.get("c")
    if h is None or l is None or c is None:
        return None
    cdp = (h + l + 2 * c) / 4
    rng = h - l
    return {
        "cdp": round(cdp, 4),
        "nh": round(2 * cdp - l, 4),   # 近高值,當壓力參考
        "nl": round(2 * cdp - h, 4),   # 近低值,當支撐參考
        "ah": round(cdp + rng, 4),     # 最高值
        "al": round(cdp - rng, 4),     # 最低值
        "basis": {"h": h, "l": l, "c": c},
    }


def load_json_safe(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def send_alert_email(subject, body):
    """寄一封純文字信到阿文的Gmail。沒設定GMAIL_APP_PASSWORD這個secret就只印警告、不中斷排程,
    避免「忘了設定密碼」變成每次排程都失敗。"""
    app_password = os.environ.get(GMAIL_APP_PASSWORD_ENV)
    if not app_password:
        print(f"[警告] 沒有設定 {GMAIL_APP_PASSWORD_ENV} 這個 GitHub secret,略過寄信。觸發內容:\n{body}")
        return
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = ALERT_EMAIL_FROM
    msg["To"] = ALERT_EMAIL_TO
    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=20) as server:
            server.starttls()
            server.login(ALERT_EMAIL_FROM, app_password)
            server.send_message(msg)
        print(f"[通知] 已寄出提醒信:{subject}")
    except Exception as e:  # 寄信失敗也不該讓整個排程中斷
        print(f"[警告] 寄信失敗:{e}\n觸發內容:\n{body}")


def check_and_send_alerts(run_time, watchlist_out):
    """兩種觸價通知:
    1. 自動:自選股現價「跨越昨收」(由空翻多/由多翻空)、或碰到漲停/跌停 —— 一天一檔一種只通知一次。
    2. 自訂:讀 data/alerts.json 裡阿文自己填的目標價,碰到也是一天一次。
    用 data/alerts_state.json 記錄「今天已經通知過什麼」,換日期自動重置。"""
    today_str = run_time.strftime("%Y-%m-%d")

    state = load_json_safe(ALERTS_STATE_FILE, {})
    if state.get("date") != today_str:
        state = {"date": today_str, "last_side": {}, "limit_fired": {}, "custom_fired": []}
    state.setdefault("last_side", {})
    state.setdefault("limit_fired", {})
    state.setdefault("custom_fired", [])

    if not _looks_like_trading_session(watchlist_out):
        # 國定假日/颱風假排程照跑但沒開盤:這次報價多半是量能掛零的死數字,
        # 跳過整個通知邏輯,不更新last_side(避免把「平盤」誤記成方向、隔天真的開盤時
        # 誤判成一次假的「跨越昨收」),也不會誤判漲跌停。狀態檔還是寫回去,讓日期有換就換。
        with open(ALERTS_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        return

    fires = []  # 這次要通知的文字清單

    for q in watchlist_out:
        code, name, last = q["code"], q["name"], q.get("last")
        prev = q.get("prev_close")
        if last is None:
            continue

        # -- 漲停/跌停,一天一次 --
        fired_limits = state["limit_fired"].setdefault(code, [])
        if q.get("upper_limit") is not None and last >= q["upper_limit"] and "up" not in fired_limits:
            fires.append(f"{code} {name} 觸及漲停 {q['upper_limit']}")
            fired_limits.append("up")
        if q.get("lower_limit") is not None and last <= q["lower_limit"] and "down" not in fired_limits:
            fires.append(f"{code} {name} 觸及跌停 {q['lower_limit']}")
            fired_limits.append("down")

        # -- 跨越昨收(交界位),由上一次記錄的方向比對,真的「跨過去」才通知,不是只要站上面就一直通知 --
        if prev is not None:
            side = "above" if last > prev else ("below" if last < prev else "equal")
            prev_side = state["last_side"].get(code)
            if prev_side and prev_side != side and side != "equal":
                direction = "由空翻多,站上昨收" if side == "above" else "由多翻空,跌破昨收"
                fires.append(f"{code} {name} {direction}(昨收{prev}, 現價{last})")
            state["last_side"][code] = side

    # -- 自訂目標價(data/alerts.json 裡阿文自己設定的) --
    alerts_config = load_json_safe(ALERTS_CONFIG_FILE, {"custom": []})
    by_code = {q["code"]: q for q in watchlist_out}
    for a in alerts_config.get("custom", []):
        code = a.get("code")
        target = a.get("target")
        direction = a.get("direction")  # "above" or "below"
        if code not in by_code or target is None or direction not in ("above", "below"):
            continue
        q = by_code[code]
        last = q.get("last")
        if last is None:
            continue
        alert_id = f"{code}:{direction}:{target}"
        hit = (direction == "above" and last >= target) or (direction == "below" and last <= target)
        if hit and alert_id not in state["custom_fired"]:
            word = "漲到" if direction == "above" else "跌到"
            note = f"({a['note']})" if a.get("note") else ""
            fires.append(f"{code} {q['name']} {word} {target}{note},現價 {last}")
            state["custom_fired"].append(alert_id)

    if fires:
        body = (
            f"爆量雷達 觸價通知 — {run_time.strftime('%Y-%m-%d %H:%M')}\n\n"
            + "\n".join(f"・{f}" for f in fires)
            + "\n\nhttps://wayne5171508-sudo.github.io/tw-signal-scanner/\n"
            + "(此為機械式價位比對,非買賣建議)"
        )
        send_alert_email(f"【爆量雷達】{len(fires)} 檔觸發觀察價位", body)

    with open(ALERTS_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


def main():
    run_time = taipei_now()

    index_codes = [c for c, _ in INDICES]
    stock_codes = [f"TWS:{code}:STOCK" for code in WATCHLIST_NAMES]
    all_rows = fetch_quotes(index_codes + stock_codes)

    indices_out = []
    for code, name in INDICES:
        indices_out.append(row_to_quote(all_rows.get(code), code, name))

    watchlist_out = []
    for code, name in WATCHLIST_NAMES.items():
        cnyes_code = f"TWS:{code}:STOCK"
        q = row_to_quote(all_rows.get(cnyes_code), code, name)
        watchlist_out.append(q)

    # CDP壓力/支撐(用前一個交易日的高低收算)+ 量能倍率 + 壓力支撐多日趨勢,
    # 三個都是從同一份滾動歷史(data/daily_bars.json)算出來的,取代原本誤用今日漲跌停當壓力支撐的寫法
    prev_bars, history_days = update_daily_bars(run_time, watchlist_out)
    for q in watchlist_out:
        q["cdp"] = compute_cdp(prev_bars.get(q["code"]))
        q["volume_multiple"] = compute_volume_multiple(q.get("volume"), q["code"], history_days)
        q["cdp_trend"] = compute_cdp_trend(q["code"], history_days, q["cdp"])

    # 用任一筆有拿到時間戳的資料,換算成「資料實際代表的時間」,跟「程式執行的時間」分開揭露
    data_ts = None
    for q in indices_out + watchlist_out:
        if q.get("ts"):
            data_ts = q["ts"]
            break
    data_as_of = (
        datetime.fromtimestamp(data_ts, TAIPEI).isoformat()
        if data_ts else None
    )

    result = {
        "checked_at": run_time.isoformat(),
        "data_as_of": data_as_of,
        "check_frequency_note": "開盤期間每約5分鐘由GitHub Actions排程抓一次,非逐秒即時報價",
        "indices": indices_out,
        "watchlist_quotes": watchlist_out,
    }

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    update_history(run_time, indices_out, watchlist_out)
    check_and_send_alerts(run_time, watchlist_out)

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
