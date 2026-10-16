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

v7新增(個股K線圖要用的成交量):
- data/intraday_history.json 每個時間點除了原本的成交價(wl),多存一份「當下累積成交量」
  (wlv)。網頁端用「這一點的量 - 上一點的量」算出每個5分鐘區間自己的成交量,疊成K線圖下方
  的量能長條圖。免費報價源沒有逐筆K棒資料,網頁是用每5分鐘一次的收盤價堆疊出近似K棒(開盤
  價=上一次的價,收盤價=這一次的價,沒有真正的分鐘內最高最低),圖上有誠實標註這件事。

v8新增(讓支撐壓力更可靠,三件事):
- 正式日K:每天第一次執行時,向cnyes的日K歷史API抓50檔最近約40天的開高低收量,存成
  data/daily_official.json(含status欄位,記錄成功/失敗幾檔)。「前一個交易日的高低收」優先用
  這份完整日K,不再只靠排程最後一次抓到的記錄。抓不到或資料對不上昨收(差超過3%)就自動退回
  舊的 daily_bars 記錄,頁面照常運作,不會壞。
- 波段高低:用最近5日、10日(不含今天)的最高/最低價,存在每檔的 swing 欄位。前一天鎖漲停/
  跌停或波動極小時,CDP的壓力支撐會縮成很窄一條失去參考價值,波段高低不受單日影響。
- CDP可靠度:cdp.reliability = "low" 代表前一日振幅小於1.5%(多為漲跌停鎖死),網頁會改用
  5日高低當主要壓力支撐,並標註原因。

v9新增(獨立審查後的修正,共9項):
- 非交易日判斷改看報價自己的時間戳:加權指數/櫃買/自選股的最新成交時間不是「今天」,就整輪略過,
  不寫入歷史、不發觸價通知。(原本的「成交量是0」判斷對cnyes無效,它假日會回傳前一天的完整數字,
  實際造成 10/03(六)、10/09(補假) 被當成交易日存進 daily_bars。)載入時會自動清掉這類重複日。
- 正式日K驗證加嚴:收盤價必須跟今天報價的「昨收」幾乎一致(差0.3%或0.05元以內),日期不能超過7天前,
  否則視為對不上,改用舊記錄。
- 正式日K改成逐檔追蹤:今天沒抓成功的檔,同一天會再補抓,不會因為有一檔成功就整天不重試。
- 檔案改成先寫暫存檔再覆蓋(原子寫入),中途中斷不會把歷史資料寫壞;不再因為 daily_bars 壞掉
  就退回v4舊檔把10天歷史洗成1天;NaN一律轉成null,不會讓網頁整個讀不出來。
- CDP可靠度改成講原因:limit_lock(昨日收在漲跌停且振幅小)、narrow_range(振幅小於1.5%)、
  basis_mismatch(備用記錄的收盤跟昨收對不上),網頁會依原因顯示正確文字。
- 新增 20日高低、5/10/20/60日均線、5日平均振幅(adr5,給網頁算「接近」的距離用)。
- CDP走勢改用正式日K算,跟今天的CDP同一個資料來源,不再前後打架。

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
DAILY_OFFICIAL_FILE = os.path.join(DATA_DIR, "daily_official.json")
CNYES_HISTORY_URL = "https://ws.api.cnyes.com/ws/api/v1/charting/history"
OFFICIAL_KEEP_DAYS = 260              # 每檔最多留幾根日K(約一年),回測樣本才夠多
LOW_RELIABILITY_RANGE_PCT = 1.5   # 前一日(高-低)/收 小於這個百分比,視為CDP參考性低
OFFICIAL_MISMATCH_PCT = 0.3       # 正式日K收盤和今天報價的昨收差超過這個百分比(且超過0.05元),視為對不上,不採用
OFFICIAL_MAX_AGE_DAYS = 7          # 正式日K最後一根距今超過幾天,視為過期
HISTORY_FETCH_DAYS = 100           # 備用:長區間抓不到時退回的日曆天數(約65個交易日)
HISTORY_FETCH_DAYS_LONG = 400      # 優先向日K API要的日曆天數(約260個交易日)
REFRESH_BUDGET_SECONDS = 90        # 每一輪抓日K最多花幾秒,超過就留給下一輪繼續
HISTORY_TIMEOUT = 8
HISTORY_RETRY_GAP_FAIL = 600       # 今天還有沒抓到的股票時,隔幾秒再重試
LIMIT_MOVE_PCT = 9.3               # 前一日漲跌幅超過這個百分比,視為漲停/跌停
MAX_POINTS_PER_DAY = 60
MAX_HISTORY_DAYS = 10
VOLUME_LOOKBACK_DAYS = 5
CDP_TREND_POINTS = 6  # 趨勢最多顯示幾個點(含今天)

ALERT_EMAIL_TO = "wayne5171508@gmail.com"
ALERT_EMAIL_FROM = "wayne5171508@gmail.com"
GMAIL_APP_PASSWORD_ENV = "GMAIL_APP_PASSWORD"
ALERTS_ENABLED = False   # 觸價提醒信:阿文決定關閉(盤中直接看網頁)。要重新開啟改成 True

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

# 自選股(代碼: 名稱)—— 來源:阿文國泰證券App自選股6個分頁彙整(皇太后、行動自選1~5),2026-10-15
WATCHLIST_NAMES = {
    "2455": "全新", "3374": "精材", "2408": "南亞科", "2344": "華邦電",
    "6147": "頎邦", "8021": "尖點", "8039": "台虹", "8358": "金居",
    "3264": "欣銓", "4979": "華星光", "3105": "穩懋", "6173": "信昌電",
    "3016": "嘉晶", "1727": "中華化", "8042": "金山電", "3027": "盛達",
    "4303": "信立", "8111": "立碁", "1815": "富喬", "2630": "亞航",
    "2883": "凱基金", "2881": "富邦金", "2313": "華通", "4919": "新唐",
    "6217": "中探針", "1303": "南亞", "3051": "力特", "6937": "天虹",
    "2337": "旺宏", "6257": "矽格", "3090": "日電貿", "2303": "聯電",
    "2379": "瑞昱", "2609": "陽明", "3231": "緯創", "3707": "漢磊",
    "5425": "台半", "2481": "強茂", "3588": "通嘉", "5347": "世界",
    "6182": "合晶", "2351": "順德", "2449": "京元電子", "6239": "力成",
    "8150": "南茂", "4958": "臻鼎-KY", "3037": "欣興", "6488": "環球晶",
    "3490": "單井", "2409": "友達", "2882": "國泰金", "2327": "國巨",
    "3624": "光頡", "3605": "宏致", "6456": "GIS-KY", "2492": "華新科",
    "4991": "環宇-KY", "6278": "台表科", "3042": "晶技", "2606": "裕民",
    "6140": "訊達", "1316": "上曜", "1802": "台玻", "3706": "神達",
    "2365": "昆盈", "7781": "昕力資*", "2887": "台新新光金", "2049": "上銀",
    "4576": "大銀微系統", "2359": "所羅門", "0050": "元大台灣50", "009816": "凱基台灣TOP50",
    "00981A": "主動統一台股增長", "00685L": "群益臺灣加權正2", "00631L": "元大台灣50正2",
}


def _clean(obj):
    """NaN/Infinity不是合法JSON,網頁JSON.parse會直接失敗,全部轉成null。"""
    if isinstance(obj, float):
        return obj if obj == obj and obj not in (float("inf"), float("-inf")) else None
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    return obj


def write_json_atomic(path, obj, **kw):
    """先寫到暫存檔再用 os.replace 覆蓋,中途中斷/磁碟錯誤時原檔案不會被寫壞。"""
    text = json.dumps(_clean(obj), ensure_ascii=False, allow_nan=False, **kw)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


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


# ---- 開盤爆量偵測(09:00~09:30)----
# 作法:今天累積成交量 / (近20日日成交量中位數 × 此刻「正常情況下已經成交的比例」)。
# 「正常已成交比例」是台股常見的U型量能估計曲線(開盤集中、尾盤再放大),不是每檔實測值,
# 所以倍數當「相對強弱」看,不是精確數字。方向只看漲跌幅:量大+上漲=疑似進場,量大+下跌=疑似出場
# (成交量本身分不出買方賣方,這是推測)。
OPENING_START_MIN = 0       # 09:00
OPENING_END_MIN = 31        # 09:31 之前的那一輪還算
OPENING_CURVE = [(0, 0.012), (1, 0.03), (5, 0.06), (10, 0.09), (15, 0.115), (20, 0.135), (30, 0.165), (60, 0.30), (120, 0.52), (270, 1.0)]
OPEN_MIN_VOL = 200          # 累積量低於200張不判斷(太容易被單筆撐起來)
OPEN_PACE_ALERT = 3.0       # 倍數>=3 列入(實測自選股約前10%)
OPEN_PACE_STRONG = 6.0      # 倍數>=6 標強烈
OPEN_FLAT_PCT = 0.5         # 漲跌幅絕對值<0.5%算「方向不明」
OPEN_AVG_DAYS = 20


def curve_frac(m):
    if m <= OPENING_CURVE[0][0]:
        return OPENING_CURVE[0][1]
    for (m0, f0), (m1, f1) in zip(OPENING_CURVE, OPENING_CURVE[1:]):
        if m <= m1:
            return f0 + (f1 - f0) * (m - m0) / (m1 - m0)
    return 1.0


def _avg_daily_volume(official_bars, code, today):
    bars = [b for b in (official_bars.get(code) or []) if b.get("d") and b["d"] < today and _num_ok(b.get("v")) and b["v"] > 0]
    bars = bars[-OPEN_AVG_DAYS:]
    if len(bars) < 5:
        return None
    vs = sorted(b["v"] for b in bars)
    return vs[len(vs) // 2]   # 用中位數,避免被前幾天的爆量日拉高基準


def compute_opening_alerts(run_time, watchlist_out, official):
    """回傳 (輸出給網頁的字典, 要存進歷史檔的狀態字典)。09:31 之後不再更新,只把已記錄的名單原樣回傳。"""
    today = run_time.strftime("%Y-%m-%d")
    state = {}
    prev_pt = None
    try:
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                ex = json.load(f)
            if ex.get("date") == today:
                state = ex.get("opening") or {}
                pts = [p for p in (ex.get("points") or []) if p.get("wlv")]
                prev_pt = pts[-1] if pts else None
    except (json.JSONDecodeError, OSError):
        state = {}
    m = (run_time - run_time.replace(hour=9, minute=0, second=0, microsecond=0)).total_seconds() / 60
    active = OPENING_START_MIN < m <= OPENING_END_MIN
    # 全天量能倍數:每一檔都算(網頁「今日焦點」用),不只開盤半小時
    if m > 2:
        _bars_all = (official or {}).get("bars") or {}
        for q in watchlist_out:
            vol_ = q.get("volume")
            avg_ = _avg_daily_volume(_bars_all, q["code"], today)
            if _num_ok(vol_) and vol_ >= OPEN_MIN_VOL and avg_:
                q["pace"] = round(vol_ / (avg_ * curve_frac(min(m, 270))), 2)
    if active:
        bars = (official or {}).get("bars") or {}
        pm = None
        if prev_pt:
            try:
                pt = datetime.fromisoformat(prev_pt["t"])
                pm = (pt - run_time.replace(hour=9, minute=0, second=0, microsecond=0)).total_seconds() / 60
            except (ValueError, KeyError, TypeError):
                pm = None
        for q in watchlist_out:
            code, vol = q["code"], q.get("volume")
            if not _num_ok(vol) or vol < OPEN_MIN_VOL:
                continue
            avg = _avg_daily_volume(bars, code, today)
            exp = (avg * curve_frac(m)) if avg else None
            if not exp:
                continue
            pace = vol / exp
            burst = None
            if prev_pt and pm is not None and pm >= 0 and m - pm >= 2:
                pv = (prev_pt.get("wlv") or {}).get(code)
                if _num_ok(pv) and vol >= pv:
                    e5 = avg * (curve_frac(m) - curve_frac(pm))
                    burst = (vol - pv) / e5 if e5 > 0 else None
            pct = q.get("pct")
            cur = {"pace": round(pace, 2), "burst": round(burst, 2) if burst is not None else None}
            st = state.get(code)
            if pace >= OPEN_PACE_ALERT or st:
                if not st:
                    st = {"first_t": run_time.strftime("%H:%M"), "peak_pace": 0}
                if pace >= st.get("peak_pace", 0):
                    st["peak_pace"] = round(pace, 2)
                    st["peak_t"] = run_time.strftime("%H:%M")
                st.update({"pace": cur["pace"], "burst": cur["burst"], "pct": pct, "last": q.get("last"),
                           "vol": vol, "avg": round(avg), "name": q["name"], "t": run_time.strftime("%H:%M")})
                if pace >= OPEN_PACE_ALERT:
                    st["hot_now"] = True
                else:
                    st["hot_now"] = False
                state[code] = st
    items = []
    for code, st in state.items():
        pct = st.get("pct")
        if pct is None or abs(pct) < OPEN_FLAT_PCT:
            side = "flat"
        else:
            side = "in" if pct > 0 else "out"
        items.append({"code": code, "name": st.get("name"), "side": side, "pace": st.get("pace"), "peak_pace": st.get("peak_pace"),
                      "burst": st.get("burst"), "pct": pct, "last": st.get("last"), "vol": st.get("vol"), "avg": st.get("avg"),
                      "first_t": st.get("first_t"), "peak_t": st.get("peak_t"), "hot_now": bool(st.get("hot_now")),
                      "strong": (st.get("peak_pace") or 0) >= OPEN_PACE_STRONG})
    items.sort(key=lambda x: -(x["peak_pace"] or 0))
    out = {"active": active, "window": "09:00-09:30", "as_of": run_time.strftime("%H:%M"), "items": items[:30]}
    return out, state


def update_history(run_time, indices_out, watchlist_out, opening_state=None):
    """把這次抓到的價格疊加進 data/intraday_history.json。
    只留「今天」的點(用台北日期判斷,跨到隔天自動重置),最多留 MAX_POINTS_PER_DAY 筆,
    避免檔案越養越大。每筆存 last 價(夠畫走勢小圖),自選股另外存當下的累積成交量(wlv),
    讓網頁可以用「這次量-上次量」算出每個5分鐘區間自己的量,疊出近似K線圖的量能長條。"""
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
        "wlv": {q["code"]: q["volume"] for q in watchlist_out if q.get("volume") is not None},
    }
    history["points"].append(point)
    history["points"] = history["points"][-MAX_POINTS_PER_DAY:]
    if opening_state is not None:
        history["opening"] = opening_state

    write_json_atomic(HISTORY_FILE, history)


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


def _dedupe_days(days):
    """days新到舊。若某一天跟「比它更舊的那一天」在90%以上的股票上高低收完全相同,
    代表那天其實沒開盤(cnyes假日會回傳前一天的完整數字),把這個假日移除。"""
    out = []
    for i, day in enumerate(days):
        bars = day.get("bars") or {}
        older = (days[i + 1].get("bars") or {}) if i + 1 < len(days) else {}
        common = [c for c in bars if c in older]
        if common:
            same = sum(1 for c in common if all(bars[c].get(k) == older[c].get(k) for k in ("h", "l", "c")))
            if same / len(common) >= 0.9:
                continue
        out.append(day)
    return out


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
    if stored is None and os.path.exists(DAILY_BARS_FILE):
        # 檔案存在但讀不出來(壞檔):不要退回v4舊檔把歷史洗成1天,直接中止這次,保留原檔讓人查
        raise RuntimeError("data/daily_bars.json 存在但無法解析,中止以保護歷史資料")

    if stored is None:
        # 第一次換成這個新格式:把v4那支舊檔 data/daily_hlc.json 的資料搬過來,
        # 不要把已經抓到的資料平白丟掉。舊檔沒有也沒關係,就當全新開始。
        old = load_json_safe(DAILY_HLC_FILE, {})
        stored = {"date": old.get("date"), "today": old.get("today", {}) or {}, "days": []}
        if old.get("prev"):
            stored["days"] = [{"date": None, "bars": old["prev"]}]

    stored["days"] = _dedupe_days(stored.get("days", []))

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

    write_json_atomic(DAILY_BARS_FILE, stored)

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


def _valid_bar(h, l, c):
    try:
        return h > 0 and l > 0 and c > 0 and h >= l and l - 1e-6 <= c <= h + 1e-6
    except TypeError:
        return False


_HIST_ALIASES = {"t": ("t", "time", "timestamp", "date"), "o": ("o", "open"), "h": ("h", "high"),
                 "l": ("l", "low"), "c": ("c", "close"), "v": ("v", "volume", "vol")}
_HIST_STATE = {"variant": 0, "win": 0, "debug": None, "fails": {}}   # variant:上次成功的參數寫法;debug:第一筆失敗回應的樣本(診斷用)


class _EmptyHistory(ValueError):
    """API 正常回應,但這檔股票沒有任何日K(不是格式問題,重試也沒用)。"""


def _to_date(ts):
    if isinstance(ts, str):
        return ts[:10]
    ts = float(ts)
    if ts > 1e11:   # 毫秒
        ts /= 1000.0
    return datetime.fromtimestamp(ts, TAIPEI).strftime("%Y-%m-%d")


def _parse_history(payload):
    """把各種可能的日K回傳格式整理成 {t,o,h,l,c,v} 欄位陣列。
    支援:data 是欄位陣列的字典(t/o/h/l/c/v 或 open/high/low/close/volume)、
    data 是一列一根K的字典清單、data 是 [時間,開,高,低,收,量] 的陣列清單。看不懂就丟 ValueError。"""
    data = payload.get("data") if isinstance(payload, dict) else payload
    cols = {}
    if isinstance(data, dict):
        for k, names in _HIST_ALIASES.items():
            for nm in names:
                if isinstance(data.get(nm), list):
                    cols[k] = data[nm]
                    break
    elif isinstance(data, list) and data and isinstance(data[0], dict):
        cols = {k: [] for k in _HIST_ALIASES}
        for row in data:
            for k, names in _HIST_ALIASES.items():
                cols[k].append(next((row[n] for n in names if n in row), None))
    elif isinstance(data, list) and data and isinstance(data[0], (list, tuple)) and len(data[0]) >= 5:
        cols = {k: [r[i] if i < len(r) else None for r in data] for i, k in enumerate("tohlcv")}
    t, h, l, c = (cols.get(k) or [] for k in "thlc")
    if cols and "t" in cols and not t and not h and not l and not c:
        raise _EmptyHistory("日K無資料")
    if not t or not (len(t) == len(h) == len(l) == len(c)):
        raise ValueError("日K格式不符")
    return cols


def fetch_history_one(code, run_time):
    """抓單一股票最近約100個日曆天的日K。回傳由舊到新排列的 [{d,o,h,l,c,v}],只留通過基本檢查的。
    參數 from/to 的先後順序不確定(有些平台是 from 比 to 大),兩種都試,記住成功的那種。
    格式不符就丟例外,並把第一筆失敗回應的前300字存起來(寫進 daily_official.json 的 debug),方便診斷。"""
    to_ts = int(run_time.timestamp())
    windows = [HISTORY_FETCH_DAYS_LONG, HISTORY_FETCH_DAYS]
    wins = [_HIST_STATE["win"]] + [i for i in range(len(windows)) if i != _HIST_STATE["win"]]
    vars_ = [_HIST_STATE["variant"]] + [i for i in range(2) if i != _HIST_STATE["variant"]]
    cols = None
    attempts = empties = 0
    for wi in wins:
        old_ts = to_ts - windows[wi] * 86400
        variants = [(old_ts, to_ts), (to_ts, old_ts)]
        for vi in vars_:
            frm, to = variants[vi]
            params = {"resolution": "D", "symbol": f"TWS:{code}:STOCK", "from": frm, "to": to}
            resp = requests.get(CNYES_HISTORY_URL, params=params, headers=HEADERS, timeout=HISTORY_TIMEOUT)
            resp.raise_for_status()
            attempts += 1
            try:
                cols = _parse_history(resp.json())
                _HIST_STATE["variant"], _HIST_STATE["win"] = vi, wi
                break
            except _EmptyHistory:
                empties += 1
            except (ValueError, TypeError, AttributeError):
                pass
            txt = getattr(resp, "text", "")
            sample = f"{code} win{windows[wi]} variant{vi} HTTP{getattr(resp, 'status_code', '?')} {str(txt)[:300]}"
            _HIST_STATE["last_sample"] = sample
            if _HIST_STATE["debug"] is None:
                _HIST_STATE["debug"] = sample
        if cols is not None:
            break
    if cols is None:
        if attempts and empties == attempts:
            raise _EmptyHistory("日K無資料")
        if len(_HIST_STATE["fails"]) < 12:
            _HIST_STATE["fails"][code] = _HIST_STATE.get("last_sample", "")
        raise ValueError("日K格式不符")
    t, o, h, l, c, v = (cols.get(k) or [] for k in "tohlcv")
    bars = []
    for i in range(len(t)):
        try:
            if not _valid_bar(h[i], l[i], c[i]):
                continue
            bars.append({
                "d": _to_date(t[i]),
                "o": o[i] if i < len(o) else None,
                "h": h[i], "l": l[i], "c": c[i],
                "v": v[i] if i < len(v) else None,
            })
        except (TypeError, ValueError):
            continue
    bars.sort(key=lambda b: b["d"])
    return bars[-OFFICIAL_KEEP_DAYS:]


def refresh_official_bars(run_time):
    """正式日K逐檔追蹤:每檔記錄「哪一天抓成功」,今天還沒成功的檔,同一天之內再補抓。
    每一輪最多花 REFRESH_BUDGET_SECONDS 秒,超過就留給下一輪;有失敗的話一小時內不重打。
    任何例外都不往外拋,日K是加分資料,抓不到不能影響即時報價主流程。"""
    today = run_time.strftime("%Y-%m-%d")
    stored = load_json_safe(DAILY_OFFICIAL_FILE, {}) or {}
    fetched = dict(stored.get("fetched") or {})
    no_data = set(stored.get("no_data") or [])
    schema_ok = stored.get("keep_days") == OFFICIAL_KEEP_DAYS
    if not schema_ok:   # 保留天數改版:今天全部重抓一次,把歷史補長
        fetched, no_data = {}, set()
    needed = [c for c in WATCHLIST_NAMES if fetched.get(c) != today]
    if not needed:
        return stored
    last_try = stored.get("last_attempt")
    if schema_ok and stored.get("last_attempt_date") == today and last_try:
        try:
            gap = HISTORY_RETRY_GAP_FAIL
            if (run_time - datetime.fromisoformat(last_try)).total_seconds() < gap:
                return stored
        except ValueError:
            pass

    merged = dict(stored.get("bars") or {})
    errors = []
    started = time.time()
    for code in needed:
        if time.time() - started > REFRESH_BUDGET_SECONDS:
            errors.append("本輪時間用完,其餘留給下一輪")
            break
        try:
            bars = fetch_history_one(code, run_time)
            if bars:
                merged[code] = bars
                fetched[code] = today
                no_data.discard(code)
            else:
                errors.append(f"{code}: 沒有可用資料")
        except _EmptyHistory:
            fetched[code] = today     # 來源明確回覆沒有這檔的日K,今天不再重試
            no_data.add(code)
        except Exception as e:  # noqa: BLE001  日K失敗不能中斷主流程
            errors.append(f"{code}: {type(e).__name__} {str(e)[:60]}")
        time.sleep(0.15)

    total = len(WATCHLIST_NAMES)
    ok = sum(1 for c in WATCHLIST_NAMES if fetched.get(c) == today)
    nd = sorted(c for c in no_data if c in WATCHLIST_NAMES)
    status = f"{'ok' if ok == total else ('partial' if ok else 'fail')} {ok}/{total}"
    if nd:
        status += "(來源無日K:" + ",".join(nd) + ")"
    result = {
        "fetched": fetched,
        "last_attempt": run_time.isoformat(),
        "last_attempt_date": today,
        "status": status,
        "keep_days": OFFICIAL_KEEP_DAYS,
        "no_data": nd,
        "source": "cnyes 日K歷史API (resolution=D)",
        "errors": errors[:10],
        "debug": _HIST_STATE["debug"],
        "fail_samples": _HIST_STATE["fails"],
        "bars": merged,
    }
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        write_json_atomic(DAILY_OFFICIAL_FILE, result)
    except (OSError, ValueError):
        pass
    return result


def compute_swing(bars):
    """bars: 由舊到新、不含今天的日K清單(每筆至少有 h、l,c可有可無)。
    回傳最近5/10/20日的最高/最低(實際用了幾天也一併回傳)、5日平均振幅(adr5,百分比)、
    以及5/10/20/60日均線(收盤價平均,天數不夠就不給)。少於3天資料回傳 None。"""
    if not bars or len(bars) < 3:
        return None
    out = {}
    for n in (5, 10, 20):
        seg = bars[-n:]
        out[f"h{n}"] = max(b["h"] for b in seg)
        out[f"l{n}"] = min(b["l"] for b in seg)
        out[f"d{n}"] = len(seg)
    ranges = [(b["h"] - b["l"]) / b["c"] * 100 for b in bars[-5:] if b.get("c")]
    out["adr5"] = round(sum(ranges) / len(ranges), 2) if ranges else None
    closes = [b["c"] for b in bars if b.get("c")]
    ma = {}
    for n in (5, 10, 20, 60):
        if len(closes) >= n:
            ma[f"ma{n}"] = round(sum(closes[-n:]) / n, 2)
    out["ma"] = ma
    return out


TOUCH_TOL_ADR = 0.06     # 「碰到」的距離 = 該檔近10日平均振幅的 6%(限制在 0.05%~0.5%)
TOUCH_REV_ADR = 0.25     # 「明確彈開/回落」= 收盤離線至少平均振幅的 25%(限制在 0.2%~2%)
CONF_ADR = 0.2           # 線疊加的距離 = 平均振幅的 20%(限制在 0.3%~1%)
TOUCH_BASES = ("cdp", "sw5", "sw10", "sw20", "c1", "c2")   # c2=多條不同類的線疊在一起(疊加強度>=2),c1=單一線
VP_DAYS = 60             # 量能密集區用最近幾個交易日
VP_BIN_PCT = 0.5         # 量能分格寬度(佔現價 %)
VP_NODE_MULT = 1.5       # 某一格成交量 >= 平均 1.5 倍才算密集區
VOL_HI_MULT = 1.2        # 碰線當天量 >= 前5日均量 1.2 倍算放量
REGIME_WEAK = -1.0       # 自選股當日漲跌幅中位數 <= -1% 算大盤偏弱
REGIME_STRONG = 1.0
LINE_FAMILY = {"CDP": "cdp", "5日": "swing", "10日": "swing", "20日": "swing",
               "週線": "ma", "10日線": "ma", "月線": "ma", "季線": "ma", "量能區": "vp"}


def _num_ok(*xs):
    return all(isinstance(x, (int, float)) and not isinstance(x, bool) and x == x for x in xs)


def avg_range_pct(hist, n=10):
    """最近 n 根日K的平均振幅 (高-低)/收 %,資料不足回傳預設 2.0。用來讓各項門檻跟著每檔股票自己的波動走。"""
    rs = [(x["h"] - x["l"]) / x["c"] * 100 for x in (hist or [])[-n:] if _num_ok(x.get("h"), x.get("l"), x.get("c")) and x["c"] > 0]
    return sum(rs) / len(rs) if len(rs) >= 3 else 2.0


def touch_params(adr):
    tol = min(max(TOUCH_TOL_ADR * adr, 0.05), 0.5)
    rev = min(max(TOUCH_REV_ADR * adr, 0.2), 2.0)
    conf = min(max(CONF_ADR * adr, 0.3), 1.0)
    return tol, rev, conf


def regime_label(median_pct):
    if median_pct is None:
        return None
    return "weak" if median_pct <= REGIME_WEAK else "strong" if median_pct >= REGIME_STRONG else "normal"


def compute_regime_map(official_bars):
    """每個交易日「自選股漲跌幅中位數」。用自選股自己的日K推出來,不必另外抓大盤日K。
    至少20檔都有資料的日子才算,回傳 {日期: 中位數%}。"""
    by = {}
    for bars in (official_bars or {}).values():
        prev = None
        for b in bars or []:
            if not _num_ok(b.get("c")) or b["c"] <= 0:
                prev = None
                continue
            if prev:
                by.setdefault(b["d"], []).append((b["c"] / prev - 1) * 100)
            prev = b["c"]
    out = {}
    for d, v in by.items():
        if len(v) >= 20:
            v.sort()
            m = len(v) // 2
            out[d] = v[m] if len(v) % 2 else (v[m - 1] + v[m]) / 2
    return out


def compute_volume_nodes(bars, ref_price):
    """成交量密集區:把最近 VP_DAYS 天每天的成交量平均攤到當天的高低價區間,
    累加後找出比平均高 VP_NODE_MULT 倍的局部高點。回傳 (現價上方最近的密集價, 現價下方最近的密集價)。
    沒有成交量或資料太少回傳 (None, None)。"""
    if not _num_ok(ref_price) or ref_price <= 0:
        return None, None
    seg = [b for b in (bars or [])[-VP_DAYS:] if _num_ok(b.get("h"), b.get("l"), b.get("v")) and b["v"] > 0 and b["h"] >= b["l"]]
    if len(seg) < 10:
        return None, None
    lo, hi = min(b["l"] for b in seg), max(b["h"] for b in seg)
    w = ref_price * VP_BIN_PCT / 100
    nb = int((hi - lo) / w) + 1
    if nb > 400:
        nb = 400
        w = (hi - lo) / nb
    vol = [0.0] * nb
    for b in seg:
        i0 = min(int((b["l"] - lo) / w), nb - 1)
        i1 = min(int((b["h"] - lo) / w), nb - 1)
        share = b["v"] / (i1 - i0 + 1)
        for i in range(i0, i1 + 1):
            vol[i] += share
    nz = [x for x in vol if x > 0]
    if not nz:
        return None, None
    mean = sum(nz) / len(nz)
    nodes = []
    for i in range(nb):
        left = vol[i - 1] if i > 0 else 0
        right = vol[i + 1] if i < nb - 1 else 0
        if vol[i] >= mean * VP_NODE_MULT and vol[i] > left and vol[i] >= right:
            nodes.append(lo + (i + 0.5) * w)
    up = [x for x in nodes if x > ref_price * 1.0005]
    dn = [x for x in nodes if x < ref_price * 0.9995]
    return (min(up) if up else None), (max(dn) if dn else None)


def level_candidates(hist, ref_price):
    """用 hist(由舊到新、不含評估當天)列出所有候選線:[(價位, 名稱)]。
    CDP 近高/近低(前一天振幅太小就不算)、5/10/20日高低、5/10/20/60日均線、量能密集區。"""
    out = []
    if not hist:
        return out
    last = hist[-1]
    if _num_ok(last.get("h"), last.get("l"), last.get("c")) and last["c"] > 0:
        if (last["h"] - last["l"]) / last["c"] * 100 >= LOW_RELIABILITY_RANGE_PCT:
            cd = compute_cdp(last)
            if cd:
                out += [(cd["nh"], "CDP"), (cd["nl"], "CDP")]
    for n in (5, 10, 20):
        seg = [x for x in hist[-n:] if _num_ok(x.get("h"), x.get("l"))]
        if len(seg) == n:
            out += [(max(x["h"] for x in seg), f"{n}日"), (min(x["l"] for x in seg), f"{n}日")]
    closes = [x["c"] for x in hist if _num_ok(x.get("c"))]
    for n, nm in ((5, "週線"), (10, "10日線"), (20, "月線"), (60, "季線")):
        if len(closes) >= n:
            out.append((sum(closes[-n:]) / n, nm))
    up, dn = compute_volume_nodes(hist, ref_price)
    for v in (up, dn):
        if v:
            out.append((v, "量能區"))
    return [(p, s) for p, s in out if _num_ok(p) and p > 0]


def cluster_levels(cands, tol_pct=0.5):
    """把相差 tol_pct(%)以內的線併成一區。strength=區內「不同類別」的線有幾類
    (CDP、波段高低、均線、量能區各算一類;5日高和10日高同價不會被算成兩條)。"""
    groups = []
    for p, s in sorted(cands):
        if groups and p - groups[-1]["start"] <= groups[-1]["start"] * tol_pct / 100:
            groups[-1]["items"].append((p, s))
        else:
            groups.append({"start": p, "items": [(p, s)]})
    out = []
    for g in groups:
        names = []
        for _, s in g["items"]:
            if s not in names:
                names.append(s)
        fam = {LINE_FAMILY.get(s, s) for s in names}
        out.append({"price": round(sum(p for p, _ in g["items"]) / len(g["items"]), 4),
                    "n": len(fam), "srcs": names})
    return out


def split_clusters(cands, ref_price, tol_pct=0.5):
    res = cluster_levels([(p, s) for p, s in cands if p > ref_price], tol_pct)
    sup = cluster_levels([(p, s) for p, s in cands if p < ref_price], tol_pct)
    return res, sup


def _empty_ns():
    return {"n": 0, "held": 0, "rev": 0}


def new_touch_stats():
    st = {k: {"res": _empty_ns(), "sup": _empty_ns()} for k in TOUCH_BASES}
    st["vol"] = {side: {"hi": {"n": 0, "rev": 0}, "lo": {"n": 0, "rev": 0}} for side in ("res", "sup")}
    st["reg"] = {r: {side: {"n": 0, "rev": 0} for side in ("res", "sup")} for r in ("weak", "normal", "strong")}
    st["fol"] = {side: {"n1": 0, "ok1": 0, "n3": 0, "ok3": 0} for side in ("res", "sup")}
    st["days"] = 0
    return st


def add_stats(dst, src):
    """把一檔股票的回測結果加進總計(巢狀字典裡的數字逐項相加)。"""
    for k, v in src.items():
        if isinstance(v, dict):
            add_stats(dst[k], v)
        elif k == "days":
            dst[k] = max(dst[k], v)
        else:
            dst[k] += v


def compute_touch_stats(series, regime=None):
    """觸及回測:用正式日K(由舊到新、不含今天)逐日檢驗「前一天收盤時就能算出來的線」。
    每一天只用當時看得到的資料算 CDP 近高/近低、5/10/20日高低(不含當天),再看當天:
      壓力:最高價進到線的 tol 內算碰到;收盤沒站上算 held;收盤低於線 rev 以上算 rev。
      支撐:最低價進到線內算碰到;收盤沒跌破算 held;收盤高於線 rev 以上算 rev。
      tol / rev / 疊加距離都依「該檔近10日平均振幅」換算(touch_params),高低波動的股票才有可比性。
    另外把所有候選線(含均線、量能密集區)併成「疊加區」:c2=不同類的線疊在一起,c1=單一線。
    並把每次碰線依「當天是否放量」「大盤強弱」分開統計(只記 n 與 rev)。
    開盤就跳空越過線的日子不算;CDP 前一天若振幅太小(漲跌停鎖死)也不算。
    回傳 None 表示可回測的天數少於5天。"""
    bars = [b for b in (series or []) if _num_ok(b.get("h"), b.get("l"), b.get("c"))]
    if len(bars) < 6:
        return None
    st = new_touch_stats()
    tol = rev = 0.0015

    def test(side, line, b, o):
        """回傳 (有碰到, 收盤守住, 明確彈開/回落);跳空越過回傳 None"""
        if side == "res":
            if o is not None and o >= line:
                return None
            if b["h"] < line * (1 - tol):
                return (False, False, False)
            return (True, b["c"] < line, b["c"] <= line * (1 - rev))
        if o is not None and o <= line:
            return None
        if b["l"] > line * (1 + tol):
            return (False, False, False)
        return (True, b["c"] > line, b["c"] >= line * (1 + rev))

    for i in range(1, len(bars)):
        b, pb = bars[i], bars[i - 1]
        o = b.get("o") if _num_ok(b.get("o")) else None
        tol_p, rev_p, conf_p = touch_params(avg_range_pct(bars[:i]))
        tol, rev = tol_p / 100, rev_p / 100
        lv = {}
        pr = (pb["h"] - pb["l"]) / pb["c"] * 100 if pb["c"] else 0
        if pr >= LOW_RELIABILITY_RANGE_PCT:
            cd = compute_cdp(pb)
            if cd:
                lv["cdp"] = (cd["nh"], cd["nl"])
        for n in (5, 10, 20):
            seg = bars[max(0, i - n):i]
            if len(seg) == n:
                lv[f"sw{n}"] = (max(x["h"] for x in seg), min(x["l"] for x in seg))
        for k, (R, S) in lv.items():
            for side, line in (("res", R), ("sup", S)):
                if line > 0:
                    r = test(side, line, b, o)
                    if r and r[0]:
                        x = st[k][side]
                        x["n"] += 1
                        x["held"] += 1 if r[1] else 0
                        x["rev"] += 1 if r[2] else 0
        # 疊加區 + 放量 + 大盤強弱
        hist = bars[:i]
        ref = pb["c"]
        vols = [x["v"] for x in bars[max(0, i - 5):i] if _num_ok(x.get("v")) and x["v"] > 0]
        vol_hi = None
        if len(vols) >= 3 and _num_ok(b.get("v")) and b["v"] > 0:
            vol_hi = b["v"] >= VOL_HI_MULT * (sum(vols) / len(vols))
        rg = regime_label(regime.get(b.get("d"))) if regime else None
        res_cl, sup_cl = split_clusters(level_candidates(hist, ref), ref, conf_p)
        for side, cls in (("res", res_cl), ("sup", sup_cl)):
            for cl in cls:
                r = test(side, cl["price"], b, o)
                if not (r and r[0]):
                    continue
                k = "c2" if cl["n"] >= 2 else "c1"
                x = st[k][side]
                x["n"] += 1
                x["held"] += 1 if r[1] else 0
                x["rev"] += 1 if r[2] else 0
                if r[1]:  # 碰線當天守住,看隔天、第3天收盤是否還在線的同一側
                    fy = st["fol"][side]
                    ln = cl["price"]
                    for gap, nk, ok_k in ((1, "n1", "ok1"), (3, "n3", "ok3")):
                        if i + gap < len(bars) and _num_ok(bars[i + gap].get("c")):
                            fy[nk] += 1
                            cc = bars[i + gap]["c"]
                            fy[ok_k] += 1 if ((cc < ln) if side == "res" else (cc > ln)) else 0
                if vol_hi is not None:
                    y = st["vol"][side]["hi" if vol_hi else "lo"]
                    y["n"] += 1
                    y["rev"] += 1 if r[2] else 0
                if rg:
                    y = st["reg"][rg][side]
                    y["n"] += 1
                    y["rev"] += 1 if r[2] else 0
    st["days"] = len(bars) - 1
    return st


def _close_matches(c, ref):
    return abs(c - ref) <= max(0.05, ref * OFFICIAL_MISMATCH_PCT / 100)


def compute_cdp_trend_official(bars, today_cdp):
    """用正式日K(由舊到新、不含今天)回推最近幾天「當時」的CDP近高/近低,最後接上今天的CDP。"""
    pts = []
    for i in range(1, len(bars)):
        c = compute_cdp(bars[i - 1])
        if c:
            pts.append({"date": bars[i]["d"], "nh": c["nh"], "nl": c["nl"], "cdp": c["cdp"]})
    if today_cdp:
        pts.append({"date": "今天", "nh": today_cdp["nh"], "nl": today_cdp["nl"], "cdp": today_cdp["cdp"]})
    return pts[-CDP_TREND_POINTS:]


def build_levels(watchlist_out, prev_bars, history_days, official, today):
    """為每檔股票填入 cdp(含資料來源/日期/可靠度與原因)、swing、cdp_trend、volume_multiple。
    前一日高低收優先用正式日K,但必須通過驗證:最後一根距今不超過7天、收盤價跟今天報價的昨收
    幾乎一致(0.3%或0.05元)。驗證不過就退回 daily_bars 的記錄,並在備用記錄的收盤也對不上
    昨收時把CDP標成不可靠(basis_mismatch)。"""
    official_bars = (official or {}).get("bars") or {}
    summary = new_touch_stats()
    summary["stocks"] = 0
    regime_map = compute_regime_map(official_bars)
    pcts = sorted(q["pct"] for q in watchlist_out if _num_ok(q.get("pct")))
    med_today = None
    if len(pcts) >= 20:
        m = len(pcts) // 2
        med_today = pcts[m] if len(pcts) % 2 else (pcts[m - 1] + pcts[m]) / 2
    summary["regime_today"] = {"median_pct": round(med_today, 2), "label": regime_label(med_today)} if med_today is not None else None
    today_date = datetime.strptime(today, "%Y-%m-%d").date()
    for q in watchlist_out:
        code = q["code"]
        pc = q.get("prev_close")
        ob = [b for b in (official_bars.get(code) or []) if b.get("d") and b["d"] < today]
        prev = prev_bars.get(code)
        src, basis_date, use_official = "sampled", None, False
        if ob:
            lastb = ob[-1]
            try:
                age = (today_date - datetime.strptime(lastb["d"], "%Y-%m-%d").date()).days
            except ValueError:
                age = 999
            ok_date = age <= OFFICIAL_MAX_AGE_DAYS
            # 不能比我們自己記錄到的「最近一個交易日」更舊,否則代表日K API還沒更新到昨天
            expected = (history_days[0].get("date") if history_days else None)
            if ok_date and expected and lastb["d"] < expected:
                ok_date = False
            ok_close = (pc is None) or _close_matches(lastb["c"], pc)
            if ok_date and ok_close:
                prev = {"h": lastb["h"], "l": lastb["l"], "c": lastb["c"]}
                src, basis_date, use_official = "official", lastb["d"], True
            else:
                q["official_mismatch"] = True

        # 取得「由舊到新」的日線序列,給波段高低/均線/漲跌停判斷用
        if use_official:
            series = ob
        else:
            series = []
            for day in reversed(history_days):
                b = (day.get("bars") or {}).get(code)
                if b and b.get("h") is not None and b.get("l") is not None:
                    series.append({"d": day.get("date"), "h": b["h"], "l": b["l"], "c": b.get("c")})

        cdp = compute_cdp(prev)
        if cdp:
            h, l, c = cdp["basis"]["h"], cdp["basis"]["l"], cdp["basis"]["c"]
            rng_pct = round((h - l) / c * 100, 2) if c else None
            cdp["basis"]["source"] = src
            if basis_date:
                cdp["basis"]["date"] = basis_date
            cdp["prev_range_pct"] = rng_pct

            prev_move = None
            if len(series) >= 2 and series[-1].get("c") and series[-2].get("c"):
                prev_move = (series[-1]["c"] - series[-2]["c"]) / series[-2]["c"] * 100
            reason = None
            if rng_pct is not None and rng_pct < LOW_RELIABILITY_RANGE_PCT:
                reason = "limit_lock" if (prev_move is not None and abs(prev_move) >= LIMIT_MOVE_PCT) else "narrow_range"
            elif src == "sampled" and pc and not _close_matches(c, pc):
                reason = "basis_mismatch"
            cdp["reliability"] = "low" if reason else "ok"
            if reason:
                cdp["low_reason"] = reason
            if prev_move is not None:
                cdp["prev_move_pct"] = round(prev_move, 2)
        q["cdp"] = cdp

        sw = compute_swing(series)
        if sw:
            sw["source"] = "official" if use_official else "sampled"
        q["swing"] = sw

        # 觸及回測:只用正式日K(取樣記錄太短、也沒有開盤價,不拿來回測)
        q["touch"] = None
        if use_official:
            try:
                ts_ = compute_touch_stats(series, regime_map)
            except Exception as e:  # noqa: BLE001  回測是加分資料,不能影響報價
                print(f"[警告] {code} 觸及回測失敗: {e}")
                ts_ = None
            q["touch"] = ts_
            if ts_:
                summary["stocks"] += 1
                add_stats(summary, ts_)

        # 疊加區與量能密集區:用「現價」把候選線分成上方壓力、下方支撐,各留最近2區
        q["conf"] = None
        q["vp"] = None
        try:
            ref = q.get("last") if _num_ok(q.get("last")) and q["last"] > 0 else None
            if ref and series:
                res_cl, sup_cl = split_clusters(level_candidates(series, ref), ref, touch_params(avg_range_pct(series))[2])
                res_cl = sorted(res_cl, key=lambda c: c["price"])[:2]
                sup_cl = sorted(sup_cl, key=lambda c: -c["price"])[:2]
                q["conf"] = {"res": res_cl, "sup": sup_cl}
                up, dn = compute_volume_nodes(series, ref)
                q["vp"] = {"res": round(up, 4) if up else None, "sup": round(dn, 4) if dn else None}
        except Exception as e:  # noqa: BLE001
            print(f"[警告] {code} 疊加區計算失敗: {e}")

        q["volume_multiple"] = compute_volume_multiple(q.get("volume"), code, history_days)
        if use_official:
            q["cdp_trend"] = compute_cdp_trend_official(ob, cdp)
        else:
            q["cdp_trend"] = compute_cdp_trend(code, history_days, cdp)
    return summary


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
        write_json_atomic(ALERTS_STATE_FILE, state)
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

    write_json_atomic(ALERTS_STATE_FILE, state)


def main():
    run_time = taipei_now()

    index_codes = [c for c, _ in INDICES]
    stock_codes = [f"TWS:{code}:STOCK" for code in WATCHLIST_NAMES]
    all_rows = fetch_quotes(index_codes + stock_codes)

    indices_out = []
    for code, name in INDICES:
        indices_out.append(row_to_quote(all_rows.get(code), code, name))

    # 非交易日/尚未開盤判斷:用報價自己的最新成交時間(台股指數+自選股),不是今天就整輪略過。
    # 不寫歷史、不發通知、不覆蓋 intraday_latest.json(假日打開網頁看到的就是上一個交易日的資料)。
    ts_list = []
    for code in ("TWS:TSE01:INDEX", "TWS:OTC01:INDEX", *stock_codes):
        row = all_rows.get(code)
        if row and row.get("200007"):
            ts_list.append(row["200007"])
    if ts_list:
        session_date = datetime.fromtimestamp(max(ts_list), TAIPEI).strftime("%Y-%m-%d")
        if session_date != run_time.strftime("%Y-%m-%d"):
            print(f"[略過] 報價時間是 {session_date},今天 {run_time.strftime('%Y-%m-%d')} 還沒有新成交(休市或尚未開盤),這一輪不更新")
            return
    else:
        print("[警告] 報價沒有時間戳,無法判斷是否為交易日,照常處理")

    watchlist_out = []
    for code, name in WATCHLIST_NAMES.items():
        cnyes_code = f"TWS:{code}:STOCK"
        q = row_to_quote(all_rows.get(cnyes_code), code, name)
        watchlist_out.append(q)

    # CDP壓力/支撐(用前一個交易日的高低收算)+ 量能倍率 + 壓力支撐多日趨勢,
    # 三個都是從同一份滾動歷史(data/daily_bars.json)算出來的,取代原本誤用今日漲跌停當壓力支撐的寫法
    prev_bars, history_days = update_daily_bars(run_time, watchlist_out)
    try:
        official = refresh_official_bars(run_time)
    except Exception as e:  # noqa: BLE001  日K是加分資料,出任何狀況都不能影響即時報價
        print(f"[警告] 正式日K更新失敗,改用舊記錄: {e}")
        official = {}
    touch_summary = build_levels(watchlist_out, prev_bars, history_days, official, run_time.strftime("%Y-%m-%d"))

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

    try:
        opening_out, opening_state = compute_opening_alerts(run_time, watchlist_out, official)
    except Exception as e:  # noqa: BLE001  開盤爆量是加分功能,不能影響報價
        print(f"[警告] 開盤爆量偵測失敗: {e}")
        opening_out, opening_state = None, None

    result = {
        "checked_at": run_time.isoformat(),
        "opening_alerts": opening_out,
        "data_as_of": data_as_of,
        "check_frequency_note": "開盤期間每約5分鐘由GitHub Actions排程抓一次,非逐秒即時報價",
        "official_bars_status": (official or {}).get("status"),
        "touch_summary": touch_summary if touch_summary and touch_summary.get("stocks") else None,
        "market_regime": (touch_summary or {}).get("regime_today"),
        "indices": indices_out,
        "watchlist_quotes": watchlist_out,
    }

    os.makedirs(DATA_DIR, exist_ok=True)
    write_json_atomic(OUT_FILE, result, separators=(",", ":"))

    update_history(run_time, indices_out, watchlist_out, opening_state)
    if ALERTS_ENABLED:
        check_and_send_alerts(run_time, watchlist_out)

    print(json.dumps(_clean(result), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

