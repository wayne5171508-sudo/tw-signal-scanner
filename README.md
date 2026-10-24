# data-archive

爆量雷達的 1 分K 存檔(自選股 + 成交值前 200 大),每個交易日收盤後由 `.github/workflows/bear.yml` 新增一天,只新增不覆蓋。
不放在 main 是為了不讓 GitHub Pages 網站變大。格式:`bars1m/日期.json.gz` = `{"date", "source", "stocks": {代號: [[時間, 開, 高, 低, 收, 量], ...]}}`。
