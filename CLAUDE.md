# 爆量雷達(tw-signal-scanner)給 Claude 的地圖

阿文的台股當沖輔助網站,GitHub Pages:https://wayne5171508-sudo.github.io/tw-signal-scanner/
回覆一律繁體中文、不用破折號。只推薦不下單,絕對不要寫自動下單。

## 先讀這裡,不要整包掃
- 檔案很大(intraday.py 12 萬字、classic.html 21 萬字、index.html 7 萬字):用 grep 找函式再局部讀,不要整檔 Read。
- 每支 .py 第一行 docstring 就是用途;README.md 有完整功能說明,需要時再看。
- data/ 是機器人產生的資料,不要整個讀;只讀需要的那一檔。

## 主要流程
- 盤中:intraday.yml 叫 loop.py 常駐(永豐只登入一次),每輪 intraday.py → bias.py 多空分數、market.py 全市場約 650 檔、team.py 六個觀測員、sjquote.py 永豐報價。結果 data/intraday_latest.json、data/market_radar.json。
- 盤前:overnight.yml(隔夜風向)、tradeflags.py(當沖限制)。
- 收盤後:bear.yml 14:30(bear_signals.py 利空回測 → daytrade_lab.py 當沖規則回測,1 分K 存 data-archive 分支);scan.yml 18:20 籌碼掃描(scan.py → data/日期.json、latest.json);sj_backfill.yml 永豐 1 分K 回補;watchdog.yml 補跑漏掉的排程。
- 網頁:index.html 是首頁 AI 版(阿文主要看,快狠準);classic.html 經典版細部分析;ai.html 只是轉址。

## 規則與坑
- 改程式前先 git pull --rebase:盤中機器人每 2~3 分鐘推 data/。
- 盤中改 sjquote.py / loop.py 要隔天迴圈重開才生效。
- repo 是公開的:workflow log 一定要遮身分證與帳號。永豐金鑰在 GitHub Secrets(SJ_API_KEY、SJ_SECRET_KEY)。
- 手續費 2.8 折、沒有 20 元下限、無條件捨去;當沖稅 0.15%;來回成本約 0.23%。
- 阿文報交易(做多/做空 代號、時間、買價、賣價、張數)就用 python journal.py add 記,推上 GitHub。
- 測試:python -m pytest -q tests(push 時 tests.yml 也會自動跑)。
