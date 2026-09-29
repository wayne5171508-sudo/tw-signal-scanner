# 台股收盤後籌碼掃描(真正跑在GitHub上,不用開機)

這是把原本靠Claude對話裡WebFetch抓資料的部分,改成真正的Python程式直接打台灣證交所官方API。
好處:資料不會再因為表格太大被漏抓,而且跑起來只要幾秒鐘。

## 這裡面有什麼

- `scan.py`:主程式,抓資料、算量能倍數/技術面評分/融資增減/連續買超天數,印出一份JSON。
- `requirements.txt`:程式需要的套件清單。
- `.github/workflows/scan.yml`:排程設定,告訴GitHub「每個交易日台北時間18:20自動跑一次」。
- 跑完會自動把結果存成 `data/latest.json`(最新一次)跟 `data/2026-09-30.json`(當天存檔)兩個檔案,存回你自己的repo裡。

## 怎麼設定(全程網頁操作,不用打指令)

1. 登入 github.com,右上角「+」→「New repository」。取個名字,例如 `tw-signal-scanner`。Public或Private都可以,選Private比較好(資料只有你看得到)。
2. 進到新建好的空repo頁面,會看到「uploading an existing file」的連結,點下去。
3. 把 `scan.py`、`requirements.txt`、`README.md` 三個檔案直接拖進去上傳,按綠色的「Commit changes」。
4. 上傳 `.github/workflows/scan.yml` 這個要注意路徑:在repo頁面點「Add file」→「Create new file」,檔名那格直接打 `.github/workflows/scan.yml`(打斜線GitHub會自動幫你建資料夾),把內容貼進去,「Commit changes」。
5. 上方選單點「Actions」分頁,如果跳出提示問你要不要啟用workflow,按「I understand my workflows, go ahead and enable them」。
6. 在Actions頁面左邊會看到「收盤後籌碼掃描」這個workflow,點進去,右邊有個「Run workflow」按鈕,先手動按一次測試看看會不會成功(平常會自動跑,這是讓你現在就能看到結果,不用等到明天收盤)。
7. 跑完(大約1分鐘內)點進那次執行紀錄,可以直接看到印出來的JSON內容,確認有抓到資料。
8. 之後repo裡的 `data/latest.json` 就是最新結果,網址會是:
   `https://raw.githubusercontent.com/你的帳號/repo名稱/main/data/latest.json`

## 下一步(等你這邊設定完成再跟我說)

把你的GitHub帳號跟repo名稱告訴我,我會去改「爆量雷達・盤後籌碼掃描」這個Claude排程,
讓它改成先讀這份乾淨的JSON(用WebFetch讀一個小檔案,不會再有大表格漏抓的問題),
再由Claude接手做新聞查核、多空判讀文字、寫進Artifact網頁跟推播通知給你——
兩邊各做各自擅長的事:程式負責穩定抓數字,Claude負責讀懂新聞跟寫人話。

## 注意事項

- `scan.py` 裡對wantgoo排行頁的抓取(候選名單來源二)是用網頁表格解析,如果wantgoo改版可能會失效,
  失效時程式會自動跳過、不會整支程式壞掉,主力的候選名單(法人買超前8名)是直接打證交所官方API,很穩定。
- 如果哪天Actions執行失敗,GitHub會寄email通知到你註冊的信箱,到時候把錯誤訊息貼給我,我幫你看。
- 這是自動化籌碼/量能整理工具,不是投資建議。
