# 外匯看盤分析

## 使用方式
直接用瀏覽器開啟 `forex/index.html`（或部署到 GitHub Pages）即可，網頁會自動抓取最新匯率。

## 功能
- **自選匯率清單**：USD/TWD、EUR/TWD、JPY/TWD、CNY/TWD 等 12 組，含迷你走勢圖，紅漲綠跌
- **走勢圖**：價格 + SMA20 / SMA50 均線 + 布林通道，十字線顯示每日數值
- **技術指標**：RSI(14)、MACD(12/26/9)
- **技術分析訊號**：自動判讀均線、RSI、MACD、布林、動能，綜合為「偏多／偏空／盤整」
- **區間績效**：1 週、1 月、3 月、6 月、1 年、今年以來
- **貨幣強弱**：十種主要貨幣相對強弱排行
- **兌新台幣相關性熱圖**
- **換匯試算**：可設定銀行價差，估算進口採購付款金額
- **歷史資料表 / 下載 CSV**
- 20 種貨幣（含黃金 XAU），任意兩種可組成交叉匯率；支援 1 月～2 年區間、深淺色、手機版面

## 資料收集
```bash
python3 forex/collect.py            # 下載近 365 天每日匯率
python3 forex/collect.py --days 730 # 回補兩年
```
產生 `forex/data/rates.json`（網頁優先讀取）與 `forex/data/rates.csv`（可用 Excel 開啟）。
`.github/workflows/forex-collect.yml` 會每天台灣時間 09:17 自動收集並提交。

資料來源：[fawazahmed0/exchange-api](https://github.com/fawazahmed0/exchange-api)（每日國際中價，最早至 2024-03-02）。
僅供參考，不構成投資建議；實際換匯以銀行牌告為準。
